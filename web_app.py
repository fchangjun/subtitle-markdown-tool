#!/usr/bin/env python3
import json
import mimetypes
import re
import threading
import time
import traceback
import uuid
import zipfile
import argparse
import csv
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

from youtube_subtitles_to_md import (
    clean_scalar,
    enrich_youtube_oembed,
    extract_info,
    fetch_transcript,
    format_date,
    markdown_for_video,
    youtube_id_from_url,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "web_outputs"
MAX_URLS_PER_JOB = 1000
MAX_BODY_BYTES = 128 * 1024
TASK_MAX_ATTEMPTS = 3
JOB_WORKERS = 4


@dataclass
class JobFile:
    filename: str
    title: str
    source_url: str
    account_name: str
    publish_date: str
    subtitle_source: str
    subtitle_language: str
    size_bytes: int
    elapsed_seconds: float
    retry_count: int
    from_cache: bool = False


@dataclass
class Job:
    id: str
    urls: List[str]
    output_dir: Path
    status: str = "queued"
    current: int = 0
    total: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    files: List[JobFile] = field(default_factory=list)
    errors: List[Dict[str, object]] = field(default_factory=list)


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()
cache_lock = threading.Lock()
url_cache: Dict[str, Dict[str, object]] = {}


def normalize_url(value: str) -> str:
    value = clean_scalar(value)
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    video_id = youtube_id_from_url(value) if ("youtube.com" in host or host.endswith("youtu.be")) else ""
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return parsed._replace(fragment="").geturl().rstrip("/")


def cache_keys_for(url: str, source_url: str = "") -> List[str]:
    keys = []
    for value in [url, source_url]:
        if value:
            key = normalize_url(value)
            if key and key not in keys:
                keys.append(key)
    return keys


def parse_urls(raw: str) -> List[str]:
    candidates = re.split(r"[\s,]+", raw.strip())
    urls = []
    seen = set()
    for value in candidates:
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Invalid URL: {value}")
        if value not in seen:
            urls.append(value)
            seen.add(value)
    if not urls:
        raise ValueError("No URLs provided.")
    if len(urls) > MAX_URLS_PER_JOB:
        raise ValueError(f"Too many URLs. Limit is {MAX_URLS_PER_JOB} per job.")
    return urls


def job_to_dict(job: Job) -> Dict:
    files = sorted(job.files, key=lambda file: file.filename)
    elapsed_values = [file.elapsed_seconds for file in files]
    elapsed_values.extend(float(error.get("elapsed_seconds", 0)) for error in job.errors)
    retry_values = [file.retry_count for file in files]
    retry_values.extend(int(error.get("retry_count", 0)) for error in job.errors)
    completed_count = len(elapsed_values)
    cache_hit_count = sum(1 for file in files if file.from_cache)
    wall_time = None
    if job.started_at:
        end_time = job.completed_at or time.time()
        wall_time = round(end_time - job.started_at, 3)

    return {
        "id": job.id,
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "urls": job.urls,
        "files": [file.__dict__ for file in files],
        "errors": job.errors,
        "summary": {
            "completed_count": completed_count,
            "success_count": len(job.files),
            "error_count": len(job.errors),
            "average_elapsed_seconds": round(sum(elapsed_values) / completed_count, 3) if completed_count else 0,
            "average_retry_count": round(sum(retry_values) / completed_count, 3) if completed_count else 0,
            "total_item_elapsed_seconds": round(sum(elapsed_values), 3),
            "wall_elapsed_seconds": wall_time,
            "worker_count": min(JOB_WORKERS, job.total) if job.total else 0,
            "cache_hit_count": cache_hit_count,
        },
    }


def update_job(job_id: str, **changes) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = time.time()
    persist_job_summary(job)


def add_file(job_id: str, file: JobFile) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        job.files.append(file)
        job.updated_at = time.time()
    register_cache_entry(job, file)
    persist_job_summary(job)


def add_error_record(job_id: str, error: Dict[str, object]) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        job.errors.append(error)
        job.updated_at = time.time()
    persist_job_summary(job)


def persist_job_summary(job: Optional[Job]) -> None:
    if not job:
        return
    job.output_dir.mkdir(parents=True, exist_ok=True)
    (job.output_dir / "job_summary.json").write_text(
        json.dumps(job_to_dict(job), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def source_index_from_filename(filename: str) -> Optional[int]:
    match = re.match(r"^(\d+)_", filename)
    if not match:
        return None
    return int(match.group(1))


def register_cache_entry(job: Job, file: JobFile) -> None:
    path = job.output_dir / file.filename
    if not path.exists():
        return
    raw_url = ""
    source_index = source_index_from_filename(file.filename)
    if source_index and 1 <= source_index <= len(job.urls):
        raw_url = job.urls[source_index - 1]
    entry = {"job_id": job.id, "path": path, "file": file}
    with cache_lock:
        for key in cache_keys_for(raw_url, file.source_url):
            url_cache[key] = entry


def write_job_artifacts(job: Job) -> None:
    job.output_dir.mkdir(parents=True, exist_ok=True)
    persist_job_summary(job)
    with (job.output_dir / "job_metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "status",
                "filename",
                "title",
                "source_url",
                "account_name",
                "publish_date",
                "subtitle_source",
                "subtitle_language",
                "size_bytes",
                "elapsed_seconds",
                "retry_count",
                "from_cache",
                "error_message",
            ],
        )
        writer.writeheader()
        for file in sorted(job.files, key=lambda item: item.filename):
            writer.writerow(
                {
                    "status": "success",
                    "filename": file.filename,
                    "title": file.title,
                    "source_url": file.source_url,
                    "account_name": file.account_name,
                    "publish_date": file.publish_date,
                    "subtitle_source": file.subtitle_source,
                    "subtitle_language": file.subtitle_language,
                    "size_bytes": file.size_bytes,
                    "elapsed_seconds": file.elapsed_seconds,
                    "retry_count": file.retry_count,
                    "from_cache": file.from_cache,
                    "error_message": "",
                }
            )
        for error in job.errors:
            writer.writerow(
                {
                    "status": "error",
                    "filename": "",
                    "title": "",
                    "source_url": error.get("url", ""),
                    "account_name": "",
                    "publish_date": "",
                    "subtitle_source": "",
                    "subtitle_language": "",
                    "size_bytes": "",
                    "elapsed_seconds": error.get("elapsed_seconds", 0),
                    "retry_count": error.get("retry_count", 0),
                    "from_cache": "",
                    "error_message": error.get("message", ""),
                }
            )


def job_from_manifest(path: Path) -> Optional[Job]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    job_id = clean_scalar(data.get("id") or path.parent.name)
    if not job_id:
        return None
    status = clean_scalar(data.get("status") or "completed")
    errors = list(data.get("errors") or [])
    if status in {"queued", "running"}:
        status = "completed_with_errors"
        errors.append(
            {
                "url": "",
                "message": "服务重启后，未完成任务已中断；已生成文件仍可下载。",
                "elapsed_seconds": 0,
                "retry_count": 0,
            }
        )

    files = []
    for item in data.get("files") or []:
        try:
            files.append(
                JobFile(
                    filename=clean_scalar(item.get("filename")),
                    title=clean_scalar(item.get("title")),
                    source_url=clean_scalar(item.get("source_url")),
                    account_name=clean_scalar(item.get("account_name")),
                    publish_date=clean_scalar(item.get("publish_date")),
                    subtitle_source=clean_scalar(item.get("subtitle_source")),
                    subtitle_language=clean_scalar(item.get("subtitle_language")),
                    size_bytes=int(item.get("size_bytes") or 0),
                    elapsed_seconds=float(item.get("elapsed_seconds") or 0),
                    retry_count=int(item.get("retry_count") or 0),
                    from_cache=bool(item.get("from_cache", False)),
                )
            )
        except Exception:
            continue

    return Job(
        id=job_id,
        urls=list(data.get("urls") or []),
        output_dir=path.parent,
        status=status,
        current=int(data.get("current") or len(files) + len(errors)),
        total=int(data.get("total") or len(files) + len(errors)),
        created_at=float(data.get("created_at") or path.stat().st_mtime),
        updated_at=float(data.get("updated_at") or path.stat().st_mtime),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        files=files,
        errors=errors,
    )


def load_existing_jobs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    loaded = 0
    for summary_path in OUTPUT_DIR.glob("*/job_summary.json"):
        job = job_from_manifest(summary_path)
        if not job:
            continue
        with jobs_lock:
            jobs[job.id] = job
        for file in job.files:
            register_cache_entry(job, file)
        loaded += 1
    if loaded:
        print(f"Loaded {loaded} historical jobs", flush=True)


def process_url(url: str, index: int, output_dir: Path, retry_counter: List[int]) -> JobFile:
    info = extract_info(url)
    info = enrich_youtube_oembed(info, url)
    transcript, lang, source_name = fetch_transcript(info, url, retry_counter=retry_counter)

    video_id = clean_scalar(info.get("id") or youtube_id_from_url(url) or f"{index:04d}")
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", video_id).strip("._") or f"{index:04d}"
    filename = f"{index:03d}_{safe_id}.md"
    path = output_dir / filename
    path.write_text(markdown_for_video(info, url, index, transcript), encoding="utf-8")

    return JobFile(
        filename=filename,
        title=clean_scalar(info.get("title") or info.get("fulltitle") or url),
        source_url=clean_scalar(info.get("webpage_url") or url),
        account_name=clean_scalar(info.get("uploader") or info.get("channel") or "unknown"),
        publish_date=format_date(info.get("upload_date")),
        subtitle_source=source_name,
        subtitle_language=lang,
        size_bytes=path.stat().st_size,
        elapsed_seconds=0,
        retry_count=retry_counter[0],
    )


def rewrite_article_id(markdown: str, index: int) -> str:
    match = re.search(r"^article_id:\s*(.+)$", markdown, flags=re.MULTILINE)
    if not match:
        return markdown
    article_id = match.group(1).strip()
    if re.search(r"_\d{4}$", article_id):
        next_id = re.sub(r"_\d{4}$", f"_{index:04d}", article_id)
    else:
        next_id = f"{article_id}_{index:04d}"
    return markdown[: match.start(1)] + next_id + markdown[match.end(1) :]


def cached_file_for_url(url: str) -> Optional[Dict[str, object]]:
    with cache_lock:
        for key in cache_keys_for(url):
            entry = url_cache.get(key)
            if entry and Path(entry["path"]).exists():
                return entry
    return None


def process_cached_url(url: str, index: int, output_dir: Path) -> Optional[JobFile]:
    entry = cached_file_for_url(url)
    if not entry:
        return None
    source_path = Path(entry["path"])
    source_file = entry["file"]
    suffix = re.sub(r"^\d+_", "", source_file.filename) or source_file.filename
    filename = f"{index:03d}_{suffix}"
    target_path = output_dir / filename
    markdown = source_path.read_text(encoding="utf-8")
    target_path.write_text(rewrite_article_id(markdown, index), encoding="utf-8")
    return JobFile(
        filename=filename,
        title=source_file.title,
        source_url=source_file.source_url,
        account_name=source_file.account_name,
        publish_date=source_file.publish_date,
        subtitle_source=source_file.subtitle_source,
        subtitle_language=source_file.subtitle_language,
        size_bytes=target_path.stat().st_size,
        elapsed_seconds=0,
        retry_count=0,
        from_cache=True,
    )


def process_url_with_retry(url: str, index: int, output_dir: Path):
    item_start = time.monotonic()
    cached_file = process_cached_url(url, index, output_dir)
    if cached_file:
        cached_file.elapsed_seconds = round(time.monotonic() - item_start, 3)
        return cached_file, None

    retry_counter = [0]
    for attempt in range(1, TASK_MAX_ATTEMPTS + 1):
        try:
            file = process_url(url, index, output_dir, retry_counter)
            file.elapsed_seconds = round(time.monotonic() - item_start, 3)
            file.retry_count = retry_counter[0]
            return file, None
        except Exception as error:
            if attempt >= TASK_MAX_ATTEMPTS:
                return None, {
                    "url": url,
                    "message": str(error),
                    "elapsed_seconds": round(time.monotonic() - item_start, 3),
                    "retry_count": retry_counter[0],
                }
            retry_counter[0] += 1
            time.sleep(min(10, 2 * attempt))


def process_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        urls = list(job.urls)
        output_dir = job.output_dir
        job.status = "running"
        job.total = len(urls)
        job.started_at = time.time()
        job.updated_at = time.time()
        started_job = job
    persist_job_summary(started_job)

    output_dir.mkdir(parents=True, exist_ok=True)

    max_workers = min(JOB_WORKERS, len(urls))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(process_url_with_retry, url, index, output_dir)
            for index, url in enumerate(urls, start=1)
        ]
        for future in as_completed(futures):
            try:
                file, error = future.result()
                if file:
                    add_file(job_id, file)
                elif error:
                    add_error_record(job_id, error)
            except Exception as error:
                add_error_record(
                    job_id,
                    {
                        "url": "unknown",
                        "message": str(error),
                        "elapsed_seconds": 0,
                        "retry_count": 0,
                    },
                )
                traceback.print_exc()
            with jobs_lock:
                job = jobs[job_id]
                job.current += 1
                job.updated_at = time.time()

    with jobs_lock:
        job = jobs[job_id]
        job.status = "completed_with_errors" if job.errors else "completed"
        job.current = job.total
        job.completed_at = time.time()
        job.updated_at = time.time()
        final_job = job
    write_job_artifacts(final_job)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "SubtitleMarkdownTool/1.0"
    head_only = False

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self):
        self.head_only = False
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path.removeprefix("/static/"))
        if path == "/api/jobs":
            return self.handle_jobs_list()
        if path.startswith("/api/jobs/"):
            return self.handle_job_get(path)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        self.head_only = False
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            return self.handle_create_job()
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        self.head_only = True
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path.removeprefix("/static/"))
        return self.send_error(HTTPStatus.NOT_FOUND)

    def handle_create_job(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            return self.send_json({"error": "Request body is too large."}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8") or "{}")
            raw_urls = payload.get("urls", "")
            if isinstance(raw_urls, list):
                raw_urls = "\n".join(str(item) for item in raw_urls)
            urls = parse_urls(str(raw_urls))
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, urls=urls, output_dir=OUTPUT_DIR / job_id, total=len(urls))
        with jobs_lock:
            jobs[job_id] = job
        persist_job_summary(job)

        thread = threading.Thread(target=process_job, args=(job_id,), daemon=True)
        thread.start()
        return self.send_json(job_to_dict(job), HTTPStatus.CREATED)

    def handle_jobs_list(self):
        with jobs_lock:
            job_list = sorted(jobs.values(), key=lambda item: item.updated_at, reverse=True)
            payload = {
                "jobs": [
                    {
                        "id": job.id,
                        "status": job.status,
                        "current": job.current,
                        "total": job.total,
                        "updated_at": job.updated_at,
                        "summary": job_to_dict(job)["summary"],
                    }
                    for job in job_list[:50]
                ]
            }
        return self.send_json(payload)

    def handle_job_get(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) < 3:
            return self.send_error(HTTPStatus.NOT_FOUND)
        job_id = parts[2]
        with jobs_lock:
            job = jobs.get(job_id)
        if not job:
            return self.send_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)

        if len(parts) == 3:
            with jobs_lock:
                payload = job_to_dict(jobs[job_id])
            return self.send_json(payload)

        if len(parts) == 4 and parts[3] == "download":
            return self.serve_zip(job)

        if len(parts) == 5 and parts[3] == "files":
            filename = parts[4]
            return self.serve_job_file(job, filename)

        return self.send_error(HTTPStatus.NOT_FOUND)

    def serve_job_file(self, job: Job, filename: str):
        if "/" in filename or "\\" in filename:
            return self.send_error(HTTPStatus.BAD_REQUEST)
        path = job.output_dir / filename
        if not path.exists() or not path.is_file():
            return self.send_json({"error": "File not found."}, HTTPStatus.NOT_FOUND)
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)

    def serve_zip(self, job: Job):
        zip_path = job.output_dir / f"{job.id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(job.files, key=lambda item: item.filename):
                path = job.output_dir / file.filename
                if path.exists():
                    archive.write(path, arcname=file.filename)
            for extra_name in ["job_summary.json", "job_metrics.csv"]:
                path = job.output_dir / extra_name
                if path.exists():
                    archive.write(path, arcname=extra_name)
        data = zip_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="subtitles_{job.id}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)

    def serve_static(self, relative_path: str):
        safe_path = relative_path.strip("/") or "index.html"
        if ".." in Path(safe_path).parts:
            return self.send_error(HTTPStatus.BAD_REQUEST)
        path = STATIC_DIR / safe_path
        if not path.exists() or not path.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)

    def send_json(self, payload: Dict, status: HTTPStatus = HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Run the subtitle Markdown web tool.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    load_existing_jobs()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Subtitle Markdown Tool running at http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
