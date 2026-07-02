#!/usr/bin/env python3
import argparse
import html
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi


PREFERRED_LANGS = [
    "zh-Hans",
    "zh-CN",
    "zh",
    "en",
    "en-US",
    "en-GB",
]

PREFERRED_FORMATS = ["json3", "srv3", "srv2", "srv1", "vtt", "srt", "ttml"]


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


REQUEST_MIN_INTERVAL_SECONDS = max(0.0, env_float("SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS", 8.0))
REQUEST_JITTER_SECONDS = max(0.0, env_float("SUBTITLE_REQUEST_JITTER_SECONDS", 4.0))
LIMIT_BACKOFF_SECONDS = max(0.0, env_float("SUBTITLE_LIMIT_BACKOFF_SECONDS", 180.0))
TRANSCRIPT_MAX_ATTEMPTS = max(1, env_int("SUBTITLE_TRANSCRIPT_MAX_ATTEMPTS", 3))
TRANSCRIPT_RETRY_BASE_SECONDS = max(0.0, env_float("SUBTITLE_RETRY_BASE_SECONDS", 8.0))
TRANSCRIPT_RETRY_MAX_SECONDS = max(0.0, env_float("SUBTITLE_RETRY_MAX_SECONDS", 90.0))
RATE_LIMIT_STATUS_CODES = {403, 429}
RATE_LIMIT_PATTERNS = [
    "too many requests",
    "http error 429",
    "rate limit",
    "requestblocked",
    "ipblocked",
    "captcha",
    "not a bot",
    "unusual traffic",
    "temporarily blocked",
    "blocking requests from your ip",
    "blocked by youtube",
    "access denied",
    "forbidden",
]


class RequestPacer:
    def __init__(self, min_interval_seconds: float, jitter_seconds: float):
        self.min_interval_seconds = min_interval_seconds
        self.jitter_seconds = jitter_seconds
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            scheduled_at = max(now, self._next_at)
            self._next_at = scheduled_at + self.min_interval_seconds + random.uniform(0, self.jitter_seconds)
        if delay:
            time.sleep(delay)

    def backoff(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds + random.uniform(0, self.jitter_seconds))


request_pacer = RequestPacer(REQUEST_MIN_INTERVAL_SECONDS, REQUEST_JITTER_SECONDS)
rate_limit_lock = threading.Lock()
last_rate_limit_at = 0.0


def looks_rate_limited_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(pattern in text for pattern in RATE_LIMIT_PATTERNS)


def note_rate_limit(reason: str) -> None:
    global last_rate_limit_at
    with rate_limit_lock:
        last_rate_limit_at = time.monotonic()
    request_pacer.backoff(LIMIT_BACKOFF_SECONDS)
    if LIMIT_BACKOFF_SECONDS:
        print(
            f"Rate-limit/block signal detected ({reason}); backing off for about {LIMIT_BACKOFF_SECONDS:.0f}s.",
            file=sys.stderr,
        )


def rate_limit_detected_since(started_at: float) -> bool:
    with rate_limit_lock:
        return last_rate_limit_at >= started_at


def paced_get(url: str, **kwargs) -> requests.Response:
    request_pacer.wait()
    try:
        response = requests.get(url, **kwargs)
    except Exception as error:
        if looks_rate_limited_error(error):
            note_rate_limit(str(error))
        raise
    if response.status_code in RATE_LIMIT_STATUS_CODES:
        note_rate_limit(f"HTTP {response.status_code} {url}")
    return response


def retry_sleep(attempt: int, error: Optional[Exception] = None) -> None:
    if error and looks_rate_limited_error(error):
        note_rate_limit(str(error))
    delay = min(TRANSCRIPT_RETRY_MAX_SECONDS, TRANSCRIPT_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    time.sleep(delay + random.uniform(0, REQUEST_JITTER_SECONDS))


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 45)
        request_pacer.wait()
        try:
            response = super().request(method, url, **kwargs)
        except Exception as error:
            if looks_rate_limited_error(error):
                note_rate_limit(str(error))
            raise
        if response.status_code in RATE_LIMIT_STATUS_CODES:
            note_rate_limit(f"HTTP {response.status_code} {url}")
        return response


def clean_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def slug_part(value: str) -> str:
    value = re.sub(r"\s+", "_", clean_scalar(value))
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_.")
    return value or "unknown"


def format_date(upload_date: Optional[str]) -> str:
    if not upload_date:
        return ""
    try:
        return datetime.strptime(upload_date, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return upload_date


def parse_duration_seconds(value: object) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None

    text = clean_scalar(value)
    if not text:
        return None
    try:
        number = float(text)
        return number if number >= 0 else None
    except ValueError:
        pass

    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return None
    total = 0.0
    for part in parts:
        if not re.fullmatch(r"\d+(?:\.\d+)?", part):
            return None
        total = total * 60 + float(part)
    return total


def extract_video_duration_seconds(info: Dict) -> Optional[float]:
    for key in ["duration", "duration_seconds"]:
        seconds = parse_duration_seconds(info.get(key))
        if seconds is not None:
            return seconds

    approx_duration_ms = parse_duration_seconds(info.get("approx_duration_ms"))
    if approx_duration_ms is not None:
        return approx_duration_ms / 1000

    return parse_duration_seconds(info.get("duration_string"))


def canonical_youtube_url(info: Dict, fallback_url: str) -> str:
    video_id = info.get("id") or youtube_id_from_url(fallback_url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return info.get("webpage_url") or fallback_url


def is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("youtube.com") or host in {"youtu.be", "www.youtu.be"}


def extract_youtube_oembed_info(url: str) -> Dict:
    video_id = youtube_id_from_url(url)
    canonical_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    info = {
        "id": video_id,
        "webpage_url": canonical_url,
        "original_url": url,
        "extractor_key": "Youtube",
        "title": "",
        "uploader": "unknown",
        "channel": "unknown",
        "upload_date": "",
    }
    try:
        response = paced_get(
            "https://www.youtube.com/oembed",
            params={"url": canonical_url, "format": "json"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return info

    if data.get("title"):
        info["title"] = data["title"]
    if data.get("author_name"):
        info["uploader"] = data["author_name"]
        info["channel"] = data["author_name"]
    if data.get("author_url"):
        info["uploader_url"] = data["author_url"]
    return info


def enrich_youtube_oembed(info: Dict, url: str) -> Dict:
    if not is_youtube_url(url):
        return info
    needs_title = not clean_scalar(info.get("title"))
    uploader = clean_scalar(info.get("uploader") or info.get("channel"))
    needs_author = not uploader or uploader == "unknown"
    if not needs_title and not needs_author:
        return info

    try:
        response = paced_get(
            "https://www.youtube.com/oembed",
            params={"url": canonical_youtube_url(info, url), "format": "json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return info

    if needs_title and data.get("title"):
        info["title"] = data["title"]
    if needs_author and data.get("author_name"):
        info["uploader"] = data["author_name"]
        info["channel"] = data["author_name"]
    if data.get("author_url") and not info.get("uploader_url"):
        info["uploader_url"] = data["author_url"]
    return info


def youtube_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/")
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    return query_id


def sort_langs(langs: Iterable[str]) -> List[str]:
    langs = list(langs)
    preferred = [lang for lang in PREFERRED_LANGS if lang in langs]
    remaining = sorted(lang for lang in langs if lang not in preferred)
    return preferred + remaining


def choose_track(info: Dict) -> Tuple[Optional[str], Optional[Dict], str]:
    for source_name, source in [
        ("manual", info.get("subtitles") or {}),
        ("automatic", info.get("automatic_captions") or {}),
    ]:
        for lang in sort_langs(source.keys()):
            tracks = source.get(lang) or []
            for ext in PREFERRED_FORMATS:
                for track in tracks:
                    if track.get("ext") == ext and track.get("url"):
                        return lang, track, source_name
    return None, None, ""


def parse_json3(text: str) -> str:
    data = json.loads(text)
    lines = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        line = "".join(seg.get("utf8", "") for seg in segs)
        line = html.unescape(line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return merge_caption_lines(lines)


def parse_vtt_or_srt(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if "-->" in line:
            continue
        if line.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return merge_caption_lines(lines)


def merge_caption_lines(lines: List[str]) -> str:
    cleaned = []
    previous = None
    for line in lines:
        if line == previous:
            continue
        cleaned.append(line)
        previous = line

    paragraphs = []
    buffer = []
    char_count = 0
    for line in cleaned:
        buffer.append(line)
        char_count += len(line)
        if char_count >= 500 or re.search(r"[.!?。！？]$", line):
            paragraphs.append(" ".join(buffer))
            buffer = []
            char_count = 0
    if buffer:
        paragraphs.append(" ".join(buffer))
    return "\n\n".join(paragraphs)


def fetch_subtitle(track: Dict) -> str:
    response = paced_get(track["url"], timeout=60)
    response.raise_for_status()
    text = response.text
    ext = track.get("ext")
    if ext == "json3":
        return parse_json3(text)
    return parse_vtt_or_srt(text)


def fetch_youtube_transcript(video_id: str, retry_counter: Optional[List[int]] = None) -> Tuple[str, str, str]:
    last_error = None
    transcript_list = []
    for attempt in range(1, TRANSCRIPT_MAX_ATTEMPTS + 1):
        try:
            api = YouTubeTranscriptApi(http_client=TimeoutSession())
            transcript_list = list(api.list(video_id))
            break
        except Exception as error:
            last_error = error
            if attempt == TRANSCRIPT_MAX_ATTEMPTS:
                raise
            if retry_counter is not None:
                retry_counter[0] += 1
            retry_sleep(attempt, error)

    if not transcript_list:
        if last_error:
            raise RuntimeError(f"No transcript found for YouTube video {video_id}: {last_error}")
        raise RuntimeError(f"No transcript found for YouTube video {video_id}")

    def lang_rank(code: str) -> int:
        return PREFERRED_LANGS.index(code) if code in PREFERRED_LANGS else len(PREFERRED_LANGS)

    transcript_list.sort(key=lambda item: (item.is_generated, lang_rank(item.language_code), item.language_code))
    transcript = transcript_list[0]
    last_error = None
    for attempt in range(1, TRANSCRIPT_MAX_ATTEMPTS + 1):
        try:
            snippets = transcript.fetch()
            break
        except Exception as error:
            last_error = error
            if attempt == TRANSCRIPT_MAX_ATTEMPTS:
                raise
            if retry_counter is not None:
                retry_counter[0] += 1
            retry_sleep(attempt, error)
    else:
        raise RuntimeError(f"Could not fetch transcript for YouTube video {video_id}: {last_error}")
    lines = []
    for snippet in snippets:
        text = getattr(snippet, "text", None)
        if text is None and isinstance(snippet, dict):
            text = snippet.get("text", "")
        text = html.unescape(text or "").replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append(text)
    source_name = "automatic" if transcript.is_generated else "manual"
    return merge_caption_lines(lines), transcript.language_code, source_name


def fetch_transcript(info: Dict, url: str, retry_counter: Optional[List[int]] = None) -> Tuple[str, str, str]:
    video_id = clean_scalar(info.get("id") or youtube_id_from_url(url))
    if (info.get("extractor_key") == "Youtube" or is_youtube_url(url)) and video_id:
        try:
            return fetch_youtube_transcript(video_id, retry_counter=retry_counter)
        except Exception as transcript_error:
            print(f"youtube-transcript-api fallback needed for {video_id}: {transcript_error}", file=sys.stderr)

    lang, track, source_name = choose_track(info)
    if not track:
        raise RuntimeError(f"No subtitles found for {url}")
    return fetch_subtitle(track), lang or "", source_name


def markdown_for_video(info: Dict, url: str, index: int, transcript: str) -> str:
    uploader = clean_scalar(info.get("uploader") or info.get("channel") or "unknown")
    publish_date = format_date(info.get("upload_date"))
    date_for_id = (publish_date or "unknown-date").replace("-", "")
    article_id = f"{slug_part(uploader)}_{date_for_id}_{index:04d}"
    source_url = canonical_youtube_url(info, url)
    title = clean_scalar(info.get("title") or info.get("fulltitle") or source_url)
    duration_seconds = extract_video_duration_seconds(info)

    header = [
        f"article_id: {article_id}",
        f"title: {title}",
        f"account_name: {uploader}",
        f"publish_date: {publish_date}",
        f"source_url: {source_url}",
    ]
    if duration_seconds is not None:
        header.append(f"video_duration_seconds: {round(duration_seconds, 3)}")
    body = transcript.strip() or "未抓取到字幕正文。"
    return "\n".join(header) + "\n\n# 字幕逐字稿\n\n" + body + "\n"


def extract_info(url: str) -> Dict:
    fallback_info = extract_youtube_oembed_info(url) if is_youtube_url(url) else {}
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }
    try:
        request_pacer.wait()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as error:
        if looks_rate_limited_error(error):
            note_rate_limit(str(error))
        if fallback_info:
            return fallback_info
        raise

    if fallback_info:
        merged = dict(fallback_info)
        merged.update({key: value for key, value in info.items() if value not in (None, "")})
        return merged
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description="Export YouTube subtitles as one Markdown file per URL.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("-o", "--output-dir", default="markdown")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for index, url in enumerate(args.urls, start=1):
        info = extract_info(url)
        info = enrich_youtube_oembed(info, url)
        transcript, lang, source_name = fetch_transcript(info, url)
        video_id = clean_scalar(info.get("id") or youtube_id_from_url(url) or f"{index:04d}")
        filename = out_dir / f"{index:03d}_{video_id}.md"
        filename.write_text(markdown_for_video(info, url, index, transcript), encoding="utf-8")
        print(f"Wrote {filename} ({source_name}:{lang})", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
