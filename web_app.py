#!/usr/bin/env python3
import json
import mimetypes
import os
import random
import re
import threading
import time
import traceback
import uuid
import zipfile
import argparse
import csv
import hashlib
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

import yt_dlp

from youtube_subtitles_to_md import (
    clean_scalar,
    enrich_youtube_oembed,
    extract_info,
    extract_video_duration_seconds,
    fetch_transcript,
    format_date,
    looks_rate_limited_error,
    markdown_for_video,
    parse_duration_seconds,
    rate_limit_detected_since,
    youtube_id_from_url,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
OUTPUT_DIR = ROOT / "web_outputs"
DISCOVERY_DIR = OUTPUT_DIR / "discoveries"
CHANNELS_DIR = OUTPUT_DIR / "channels"
CHANNEL_INDEX_PATH = CHANNELS_DIR / "index.json"
MAX_BODY_BYTES = 128 * 1024


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_URLS_PER_JOB = max(1, env_int("SUBTITLE_MAX_URLS_PER_JOB", 1000))
TASK_MAX_ATTEMPTS = max(1, env_int("SUBTITLE_TASK_MAX_ATTEMPTS", 3))
TASK_RETRY_BASE_SECONDS = max(0.0, env_float("SUBTITLE_TASK_RETRY_BASE_SECONDS", 15.0))
TASK_RETRY_MAX_SECONDS = max(0.0, env_float("SUBTITLE_TASK_RETRY_MAX_SECONDS", 180.0))
TASK_RETRY_JITTER_SECONDS = max(0.0, env_float("SUBTITLE_TASK_RETRY_JITTER_SECONDS", 10.0))
AUTO_BATCH_SIZE = max(0, env_int("SUBTITLE_AUTO_BATCH_SIZE", 30))
AUTO_BATCH_COOLDOWN_SECONDS = max(0.0, env_float("SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS", 300.0))
IP_BLOCK_COOLDOWN_SECONDS = max(0.0, env_float("SUBTITLE_IP_BLOCK_COOLDOWN_SECONDS", 900.0))
DISCOVERY_MAX_SOURCES = max(1, env_int("SUBTITLE_DISCOVERY_MAX_SOURCES", 20))
DISCOVERY_DEFAULT_MAX_PER_SOURCE = max(1, env_int("SUBTITLE_DISCOVERY_MAX_PER_SOURCE", 120))
DISCOVERY_HARD_MAX_PER_SOURCE = max(
    DISCOVERY_DEFAULT_MAX_PER_SOURCE,
    env_int("SUBTITLE_DISCOVERY_HARD_MAX_PER_SOURCE", 1000),
)


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
    video_duration_seconds: Optional[float] = None
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
    message: str = ""
    cooldown_until: Optional[float] = None
    files: List[JobFile] = field(default_factory=list)
    errors: List[Dict[str, object]] = field(default_factory=list)


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()
cache_lock = threading.Lock()
channels_lock = threading.Lock()
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


def parse_discovery_sources(raw: str) -> List[str]:
    sources = parse_urls(raw)
    if len(sources) > DISCOVERY_MAX_SOURCES:
        raise ValueError(f"Too many channel URLs. Limit is {DISCOVERY_MAX_SOURCES} per discovery.")
    return sources


def subtract_months(value: date, months: int) -> date:
    if months <= 0:
        return value
    month_index = value.year * 12 + value.month - 1 - months
    year = month_index // 12
    month = month_index % 12 + 1
    month_lengths = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, month_lengths[month - 1])
    return date(year, month, day)


def parse_discovery_range(value: str) -> date:
    text = clean_scalar(value or "1年").lower()
    unit_pattern = (
        r"(\d+)\s*"
        r"(years?|yrs?|y|年|months?|mos?|mo|m|个月|月|weeks?|w|周|天|日|days?|d)"
    )
    matches = re.findall(unit_pattern, text, flags=re.IGNORECASE)
    if not matches:
        raise ValueError("时间范围格式无效，请输入类似 1年、13个月、90天 或 1y 1m 1w。")

    years = months = weeks = days = 0
    for amount_text, unit in matches:
        amount = int(amount_text)
        unit = unit.lower()
        if unit in {"year", "years", "yr", "yrs", "y", "年"}:
            years += amount
        elif unit in {"month", "months", "mos", "mo", "m", "个月", "月"}:
            months += amount
        elif unit in {"week", "weeks", "w", "周"}:
            weeks += amount
        elif unit in {"day", "days", "d", "天", "日"}:
            days += amount

    cutoff = subtract_months(date.today(), years * 12 + months)
    return cutoff - timedelta(weeks=weeks, days=days)


def discovery_listing_url(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if "youtube.com" not in host and not host.endswith("youtu.be"):
        return value
    if youtube_id_from_url(value):
        return value

    path = parsed.path.rstrip("/")
    if not path:
        return value
    if path.endswith(("/videos", "/streams", "/shorts")):
        return parsed._replace(path=path, query="", fragment="").geturl()
    if path.startswith(("/@", "/channel/", "/c/", "/user/")):
        return parsed._replace(path=f"{path}/videos", query="", fragment="").geturl()
    return parsed._replace(query="", fragment="").geturl()


def date_from_video_entry(entry: Dict[str, object]) -> Optional[date]:
    upload_date = clean_scalar(entry.get("upload_date"))
    if upload_date:
        try:
            return datetime.strptime(upload_date, "%Y%m%d").date()
        except ValueError:
            pass

    for key in ["timestamp", "release_timestamp", "modified_timestamp"]:
        timestamp = entry.get(key)
        if timestamp is None:
            continue
        try:
            return datetime.fromtimestamp(float(timestamp)).date()
        except (TypeError, ValueError, OSError):
            continue
    return None


def video_url_from_entry(entry: Dict[str, object]) -> str:
    video_id = clean_scalar(entry.get("id"))
    value = clean_scalar(entry.get("webpage_url") or entry.get("url"))
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return value


def number_or_none(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def discover_source_videos(source_url: str, cutoff_date: date, max_per_source: int) -> Dict[str, object]:
    listing_url = discovery_listing_url(source_url)
    is_single_video = bool(youtube_id_from_url(listing_url))
    if is_single_video:
        video_url = normalize_url(listing_url)
        entry = {
            "id": youtube_id_from_url(listing_url),
            "url": video_url,
            "title": video_url,
            "channel": "",
            "channel_url": "",
            "publish_date": "",
            "date_known": False,
            "in_range": None,
            "duration_seconds": None,
            "view_count": None,
            "description": "",
            "source_url": source_url,
        }
        return {
            "source_url": source_url,
            "listing_url": listing_url,
            "title": entry["title"],
            "scanned_count": 1,
            "included_count": 1,
            "unknown_date_count": 1,
            "limit_reached": False,
            "items": [entry],
        }

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": max_per_source,
        "ignoreerrors": True,
        "socket_timeout": 20,
        "extractor_args": {"youtube": {"approximate_date": ["1"]}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(listing_url, download=False)

    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    items = []
    unknown_date_count = 0
    for entry in entries:
        video_url = video_url_from_entry(entry)
        if not video_url:
            continue

        publish_date = date_from_video_entry(entry)
        date_known = publish_date is not None
        in_range = publish_date >= cutoff_date if date_known else None
        if date_known and not in_range:
            continue
        if not date_known:
            unknown_date_count += 1

        duration_seconds = extract_video_duration_seconds(entry)
        items.append(
            {
                "id": clean_scalar(entry.get("id") or youtube_id_from_url(video_url)),
                "url": video_url,
                "title": clean_scalar(entry.get("title") or video_url),
                "channel": clean_scalar(entry.get("channel") or entry.get("uploader") or info.get("channel") or info.get("uploader") or ""),
                "channel_url": clean_scalar(entry.get("channel_url") or entry.get("uploader_url") or info.get("channel_url") or ""),
                "publish_date": publish_date.isoformat() if publish_date else "",
                "date_known": date_known,
                "in_range": in_range,
                "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
                "view_count": number_or_none(entry.get("view_count")),
                "description": clean_scalar(entry.get("description") or ""),
                "source_url": source_url,
            }
        )

    return {
        "source_url": source_url,
        "listing_url": listing_url,
        "title": clean_scalar(info.get("title") or listing_url),
        "description": clean_scalar(info.get("description") or info.get("channel_description") or ""),
        "scanned_count": len(entries),
        "included_count": len(items),
        "unknown_date_count": unknown_date_count,
        "limit_reached": len(entries) >= max_per_source,
        "items": items,
    }


def discover_videos(sources: List[str], range_text: str, max_per_source: int) -> Dict[str, object]:
    cutoff_date = parse_discovery_range(range_text)
    max_per_source = max(1, min(max_per_source, DISCOVERY_HARD_MAX_PER_SOURCE))

    all_items = []
    source_results = []
    seen_urls = set()
    for source_url in sources:
        started = time.monotonic()
        try:
            result = discover_source_videos(source_url, cutoff_date, max_per_source)
            result["elapsed_seconds"] = round(time.monotonic() - started, 3)
            unique_items = []
            for item in result.pop("items"):
                key = normalize_url(item["url"])
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                item["source_title"] = result.get("title", "")
                unique_items.append(item)
            result["included_count"] = len(unique_items)
            all_items.extend(unique_items)
            source_results.append(result)
        except Exception as error:
            source_results.append(
                {
                    "source_url": source_url,
                    "listing_url": discovery_listing_url(source_url),
                    "title": "",
                    "scanned_count": 0,
                    "included_count": 0,
                    "unknown_date_count": 0,
                    "limit_reached": False,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": str(error),
                }
            )

    all_items.sort(key=lambda item: item.get("publish_date") or "0000-00-00", reverse=True)
    return {
        "range": {
            "input": range_text,
            "cutoff_date": cutoff_date.isoformat(),
        },
        "max_per_source": max_per_source,
        "items": all_items,
        "sources": source_results,
        "summary": {
            "source_count": len(sources),
            "video_count": len(all_items),
            "scanned_count": sum(int(source.get("scanned_count") or 0) for source in source_results),
            "unknown_date_count": sum(int(source.get("unknown_date_count") or 0) for source in source_results),
            "error_count": sum(1 for source in source_results if source.get("error")),
            "limit_reached_count": sum(1 for source in source_results if source.get("limit_reached")),
        },
    }


def channel_listing_key(source_url: str) -> str:
    return normalize_url(discovery_listing_url(source_url))


def channel_id_for_key(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def safe_channel_id(value: str) -> str:
    value = clean_scalar(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", value):
        raise ValueError("Channel not found.")
    return value


def channel_content_path(channel_id: str) -> Path:
    return CHANNELS_DIR / f"{safe_channel_id(channel_id)}.json"


def load_channel_index() -> Dict[str, Dict[str, object]]:
    if not CHANNEL_INDEX_PATH.exists():
        return {}
    try:
        data = json.loads(CHANNEL_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    channels = {}
    for item in data.get("channels") or []:
        if isinstance(item, dict) and item.get("id"):
            channels[str(item["id"])] = item
    return channels


def save_channel_index(channels: Dict[str, Dict[str, object]]) -> None:
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    items = sorted(channels.values(), key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    CHANNEL_INDEX_PATH.write_text(
        json.dumps({"channels": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def channel_items_for(channel_id: str) -> List[Dict[str, object]]:
    path = channel_content_path(channel_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


def save_channel_items(channel_id: str, items: List[Dict[str, object]]) -> None:
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    channel_content_path(channel_id).write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def channel_summary(channel: Dict[str, object]) -> Dict[str, object]:
    return {
        "id": channel.get("id", ""),
        "source_url": channel.get("source_url", ""),
        "listing_url": channel.get("listing_url", ""),
        "title": channel.get("title") or channel.get("listing_url") or "",
        "description": channel.get("description", ""),
        "created_at": channel.get("created_at"),
        "updated_at": channel.get("updated_at"),
        "last_discovered_at": channel.get("last_discovered_at"),
        "last_discovery_id": channel.get("last_discovery_id", ""),
        "video_count": int(channel.get("video_count") or 0),
        "known_date_count": int(channel.get("known_date_count") or 0),
        "unknown_date_count": int(channel.get("unknown_date_count") or 0),
        "scanned_count": int(channel.get("scanned_count") or 0),
        "limit_reached": bool(channel.get("limit_reached", False)),
        "last_error": clean_scalar(channel.get("last_error") or ""),
    }


def list_followed_channels() -> List[Dict[str, object]]:
    with channels_lock:
        return [channel_summary(channel) for channel in load_channel_index().values()]


def upsert_followed_channel(source_url: str, channels: Optional[Dict[str, Dict[str, object]]] = None) -> Dict[str, object]:
    listing_url = discovery_listing_url(source_url)
    if youtube_id_from_url(listing_url):
        raise ValueError("关注频道请输入博主主页或频道链接，不要输入单个视频链接。")
    key = channel_listing_key(source_url)
    channel_id = channel_id_for_key(key)
    now = time.time()
    target = channels if channels is not None else load_channel_index()
    channel = target.get(channel_id)
    if channel:
        channel["updated_at"] = now
        if source_url and source_url != channel.get("source_url"):
            channel.setdefault("aliases", [])
            aliases = list(channel.get("aliases") or [])
            if source_url not in aliases:
                aliases.append(source_url)
            channel["aliases"] = aliases
        return channel

    channel = {
        "id": channel_id,
        "source_url": source_url,
        "listing_url": key,
        "title": key,
        "description": "",
        "created_at": now,
        "updated_at": now,
        "last_discovered_at": None,
        "last_discovery_id": "",
        "video_count": 0,
        "known_date_count": 0,
        "unknown_date_count": 0,
        "scanned_count": 0,
        "limit_reached": False,
        "last_error": "",
    }
    target[channel_id] = channel
    return channel


def add_followed_channels(sources: List[str]) -> Dict[str, object]:
    added = []
    existing = []
    with channels_lock:
        channels = load_channel_index()
        before = set(channels.keys())
        for source_url in sources:
            channel = upsert_followed_channel(source_url, channels)
            if channel["id"] in before:
                existing.append(channel_summary(channel))
            else:
                added.append(channel_summary(channel))
                before.add(channel["id"])
        save_channel_index(channels)
    return {
        "added_count": len(added),
        "existing_count": len(existing),
        "added": added,
        "existing": existing,
        "channels": list_followed_channels(),
    }


def update_followed_channels_from_discovery(record: Dict[str, object]) -> None:
    with channels_lock:
        channels = load_channel_index()
        items = [item for item in (record.get("items") or []) if isinstance(item, dict)]
        for source in record.get("sources") or []:
            if not isinstance(source, dict):
                continue
            source_url = clean_scalar(source.get("source_url") or "")
            if not source_url:
                continue
            try:
                channel = upsert_followed_channel(source_url, channels)
            except ValueError:
                continue

            source_key = channel_listing_key(source_url)
            channel_items = [
                item for item in items
                if channel_listing_key(clean_scalar(item.get("source_url") or source_url)) == source_key
            ]
            channel["title"] = clean_scalar(source.get("title") or channel.get("title") or source_url)
            if source.get("description"):
                channel["description"] = clean_scalar(source.get("description"))
            channel["updated_at"] = time.time()
            channel["last_discovered_at"] = record.get("updated_at") or time.time()
            channel["last_discovery_id"] = record.get("id", "")
            channel["video_count"] = len(channel_items)
            channel["known_date_count"] = sum(1 for item in channel_items if item.get("date_known"))
            channel["unknown_date_count"] = int(source.get("unknown_date_count") or 0)
            channel["scanned_count"] = int(source.get("scanned_count") or 0)
            channel["limit_reached"] = bool(source.get("limit_reached", False))
            channel["last_error"] = clean_scalar(source.get("error") or "")
            save_channel_items(str(channel["id"]), channel_items)
        save_channel_index(channels)


def get_followed_channel(channel_id: str) -> Dict[str, object]:
    with channels_lock:
        channels = load_channel_index()
        channel = channels.get(safe_channel_id(channel_id))
        if not channel:
            raise ValueError("Channel not found.")
        payload = channel_summary(channel)
        payload["items"] = channel_items_for(str(payload["id"]))
        return payload


def refresh_followed_channel(channel_id: str, range_text: str, max_per_source: int) -> Dict[str, object]:
    channel = get_followed_channel(channel_id)
    source = clean_scalar(channel.get("source_url") or channel.get("listing_url"))
    record = create_discovery_record([source], range_text, max_per_source)
    return {
        "record": record,
        "channel": get_followed_channel(channel_id),
        "channels": list_followed_channels(),
    }


def safe_discovery_id(value: str) -> str:
    value = clean_scalar(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", value):
        raise ValueError("Discovery record not found.")
    return value


def discovery_record_path(record_id: str) -> Path:
    return DISCOVERY_DIR / f"{safe_discovery_id(record_id)}.json"


def discovery_record_summary(record: Dict[str, object]) -> Dict[str, object]:
    summary = dict(record.get("summary") or {})
    return {
        "id": record.get("id", ""),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "range": record.get("range"),
        "max_per_source": record.get("max_per_source"),
        "source_count": summary.get("source_count", 0),
        "video_count": summary.get("video_count", 0),
        "scanned_count": summary.get("scanned_count", 0),
        "unknown_date_count": summary.get("unknown_date_count", 0),
        "error_count": summary.get("error_count", 0),
        "limit_reached_count": summary.get("limit_reached_count", 0),
        "sources": record.get("sources_input") or [],
        "source_titles": [
            clean_scalar(source.get("title") or source.get("source_url") or "")
            for source in (record.get("sources") or [])[:4]
            if isinstance(source, dict)
        ],
        "refresh_count": record.get("refresh_count", 0),
    }


def persist_discovery_record(record: Dict[str, object]) -> None:
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)
    discovery_record_path(str(record["id"])).write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_discovery_record(record_id: str) -> Dict[str, object]:
    path = discovery_record_path(record_id)
    if not path.exists():
        raise ValueError("Discovery record not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def list_discovery_records() -> List[Dict[str, object]]:
    if not DISCOVERY_DIR.exists():
        return []
    records = []
    for path in DISCOVERY_DIR.glob("*.json"):
        try:
            records.append(discovery_record_summary(json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            continue
    records.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    return records


def create_discovery_record(sources: List[str], range_text: str, max_per_source: int) -> Dict[str, object]:
    record = discover_videos(sources, range_text, max_per_source)
    now = time.time()
    record.update(
        {
            "id": uuid.uuid4().hex[:12],
            "created_at": now,
            "updated_at": now,
            "sources_input": sources,
            "range_input": range_text,
            "refresh_count": 0,
        }
    )
    persist_discovery_record(record)
    update_followed_channels_from_discovery(record)
    return record


def refresh_discovery_record(record_id: str) -> Dict[str, object]:
    previous = load_discovery_record(record_id)
    sources = [clean_scalar(source) for source in (previous.get("sources_input") or []) if clean_scalar(source)]
    range_text = clean_scalar(previous.get("range_input") or (previous.get("range") or {}).get("input") or "1年")
    max_per_source = int(previous.get("max_per_source") or DISCOVERY_DEFAULT_MAX_PER_SOURCE)
    record = discover_videos(sources, range_text, max_per_source)
    now = time.time()
    record.update(
        {
            "id": safe_discovery_id(record_id),
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
            "sources_input": sources,
            "range_input": range_text,
            "refresh_count": int(previous.get("refresh_count") or 0) + 1,
            "previous_updated_at": previous.get("updated_at"),
        }
    )
    persist_discovery_record(record)
    update_followed_channels_from_discovery(record)
    return record


def job_to_dict(job: Job) -> Dict:
    files = sorted(job.files, key=lambda file: file.filename)
    elapsed_values = [file.elapsed_seconds for file in files]
    elapsed_values.extend(float(error.get("elapsed_seconds", 0)) for error in job.errors)
    video_duration_values = [
        file.video_duration_seconds
        for file in files
        if file.video_duration_seconds is not None
    ]
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
        "message": job.message,
        "cooldown_until": job.cooldown_until,
        "urls": job.urls,
        "files": [file.__dict__ for file in files],
        "errors": job.errors,
        "summary": {
            "completed_count": completed_count,
            "success_count": len(job.files),
            "error_count": len(job.errors),
            "average_elapsed_seconds": round(sum(elapsed_values) / completed_count, 3) if completed_count else 0,
            "average_video_duration_seconds": (
                round(sum(video_duration_values) / len(video_duration_values), 3)
                if video_duration_values
                else None
            ),
            "average_retry_count": round(sum(retry_values) / completed_count, 3) if completed_count else 0,
            "total_item_elapsed_seconds": round(sum(elapsed_values), 3),
            "total_video_duration_seconds": (
                round(sum(video_duration_values), 3) if video_duration_values else None
            ),
            "video_duration_known_count": len(video_duration_values),
            "wall_elapsed_seconds": wall_time,
            "worker_count": 1 if job.total else 0,
            "cache_hit_count": cache_hit_count,
            "auto_batch_size": AUTO_BATCH_SIZE,
            "auto_batch_cooldown_seconds": AUTO_BATCH_COOLDOWN_SECONDS,
            "ip_block_cooldown_seconds": IP_BLOCK_COOLDOWN_SECONDS,
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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if rest_minutes:
        return f"{hours} 小时 {rest_minutes} 分钟"
    return f"{hours} 小时"


def set_job_notice(job_id: str, message: str, cooldown_until: Optional[float] = None) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        job.message = message
        job.cooldown_until = cooldown_until
        job.updated_at = time.time()
    persist_job_summary(job)


def clear_job_notice(job_id: str) -> None:
    set_job_notice(job_id, "", None)


def pause_job(job_id: str, message: str, seconds: float) -> None:
    if seconds <= 0:
        return
    set_job_notice(job_id, message, time.time() + seconds)
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(5, remaining))
    clear_job_notice(job_id)


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
                "video_duration_seconds",
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
                    "video_duration_seconds": file.video_duration_seconds if file.video_duration_seconds is not None else "",
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
                    "video_duration_seconds": "",
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
                    video_duration_seconds=parse_duration_seconds(item.get("video_duration_seconds")),
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
        message=clean_scalar(data.get("message") or ""),
        cooldown_until=data.get("cooldown_until"),
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
    video_duration_seconds = extract_video_duration_seconds(info)

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
        video_duration_seconds=round(video_duration_seconds, 3) if video_duration_seconds is not None else None,
    )


def task_retry_sleep(attempt: int) -> None:
    delay = min(TASK_RETRY_MAX_SECONDS, TASK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    time.sleep(delay + random.uniform(0, TASK_RETRY_JITTER_SECONDS))


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
        video_duration_seconds=source_file.video_duration_seconds,
        from_cache=True,
    )


def process_url_with_retry(job_id: str, url: str, index: int, output_dir: Path):
    item_start = time.monotonic()
    cached_file = process_cached_url(url, index, output_dir)
    if cached_file:
        cached_file.elapsed_seconds = round(time.monotonic() - item_start, 3)
        return cached_file, None, False

    retry_counter = [0]
    for attempt in range(1, TASK_MAX_ATTEMPTS + 1):
        attempt_start = time.monotonic()
        try:
            file = process_url(url, index, output_dir, retry_counter)
            file.elapsed_seconds = round(time.monotonic() - item_start, 3)
            file.retry_count = retry_counter[0]
            return file, None, rate_limit_detected_since(attempt_start)
        except Exception as error:
            was_rate_limited = looks_rate_limited_error(error) or rate_limit_detected_since(attempt_start)
            if attempt >= TASK_MAX_ATTEMPTS:
                return None, {
                    "url": url,
                    "message": str(error),
                    "elapsed_seconds": round(time.monotonic() - item_start, 3),
                    "retry_count": retry_counter[0],
                }, was_rate_limited
            retry_counter[0] += 1
            if was_rate_limited:
                pause_job(
                    job_id,
                    f"检测到 YouTube 限流，自动冷却 {format_duration(IP_BLOCK_COOLDOWN_SECONDS)} 后重试。",
                    IP_BLOCK_COOLDOWN_SECONDS,
                )
            else:
                task_retry_sleep(attempt)


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

    processed_since_pause = 0
    for index, url in enumerate(urls, start=1):
        file = None
        error_record = None
        was_rate_limited = False
        try:
            file, error_record, was_rate_limited = process_url_with_retry(job_id, url, index, output_dir)
            if file:
                add_file(job_id, file)
            elif error_record:
                add_error_record(job_id, error_record)
        except Exception as error:
            error_record = {
                "url": url,
                "message": str(error),
                "elapsed_seconds": 0,
                "retry_count": 0,
            }
            was_rate_limited = looks_rate_limited_error(error)
            add_error_record(job_id, error_record)
            traceback.print_exc()

        with jobs_lock:
            job = jobs[job_id]
            job.current += 1
            job.updated_at = time.time()
            progress_job = job
        persist_job_summary(progress_job)

        if index >= len(urls):
            continue

        if was_rate_limited:
            processed_since_pause = 0
            pause_job(
                job_id,
                f"检测到 YouTube 限流，自动冷却 {format_duration(IP_BLOCK_COOLDOWN_SECONDS)} 后继续。",
                IP_BLOCK_COOLDOWN_SECONDS,
            )
            continue

        used_network = not (file and file.from_cache)
        if used_network:
            processed_since_pause += 1

        if AUTO_BATCH_SIZE and processed_since_pause >= AUTO_BATCH_SIZE:
            processed_since_pause = 0
            pause_job(
                job_id,
                f"已处理 {AUTO_BATCH_SIZE} 条，自动休息 {format_duration(AUTO_BATCH_COOLDOWN_SECONDS)} 后继续。",
                AUTO_BATCH_COOLDOWN_SECONDS,
            )

    with jobs_lock:
        job = jobs[job_id]
        job.status = "completed_with_errors" if job.errors else "completed"
        job.current = job.total
        job.completed_at = time.time()
        job.message = ""
        job.cooldown_until = None
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
        if path == "/api/channels":
            return self.handle_channels_list()
        if path.startswith("/api/channels/"):
            return self.handle_channel_get(path)
        if path == "/api/discoveries":
            return self.handle_discoveries_list()
        if path.startswith("/api/discoveries/"):
            return self.handle_discovery_get(path)
        if path.startswith("/api/jobs/"):
            return self.handle_job_get(path)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        self.head_only = False
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            return self.handle_create_job()
        if parsed.path == "/api/channels":
            return self.handle_add_channels()
        if parsed.path.startswith("/api/channels/"):
            return self.handle_channel_post(parsed.path)
        if parsed.path == "/api/discover":
            return self.handle_discover()
        if parsed.path.startswith("/api/discoveries/"):
            return self.handle_discovery_post(parsed.path)
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

    def read_json_body(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large.")
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8") or "{}")

    def handle_create_job(self):
        try:
            payload = self.read_json_body()
            raw_urls = payload.get("urls", "")
            if isinstance(raw_urls, list):
                raw_urls = "\n".join(str(item) for item in raw_urls)
            urls = parse_urls(str(raw_urls))
        except ValueError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if "too large" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
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

    def handle_channels_list(self):
        return self.send_json({"channels": list_followed_channels()})

    def handle_channel_get(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3:
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            return self.send_json(get_followed_channel(parts[2]))
        except ValueError as error:
            return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def handle_add_channels(self):
        try:
            payload = self.read_json_body()
            raw_sources = payload.get("sources", "")
            if isinstance(raw_sources, list):
                raw_sources = "\n".join(str(item) for item in raw_sources)
            sources = parse_discovery_sources(str(raw_sources))
            result = add_followed_channels(sources)
        except ValueError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if "too large" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_json(result, HTTPStatus.CREATED)

    def handle_channel_post(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] == "refresh":
            try:
                payload = self.read_json_body()
                range_text = clean_scalar(payload.get("range") or "1年")
                max_per_source = int(payload.get("max_per_source") or DISCOVERY_DEFAULT_MAX_PER_SOURCE)
                return self.send_json(refresh_followed_channel(parts[2], range_text, max_per_source))
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def handle_discover(self):
        try:
            payload = self.read_json_body()
            raw_sources = payload.get("sources", "")
            if isinstance(raw_sources, list):
                raw_sources = "\n".join(str(item) for item in raw_sources)
            sources = parse_discovery_sources(str(raw_sources))
            range_text = clean_scalar(payload.get("range") or "1年")
            max_per_source = int(payload.get("max_per_source") or DISCOVERY_DEFAULT_MAX_PER_SOURCE)
            result = create_discovery_record(sources, range_text, max_per_source)
        except ValueError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if "too large" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_json(result)

    def handle_discoveries_list(self):
        return self.send_json({"records": list_discovery_records()[:80]})

    def handle_discovery_get(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3:
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            record = load_discovery_record(parts[2])
        except ValueError as error:
            return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_json(record)

    def handle_discovery_post(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] == "refresh":
            try:
                return self.send_json(refresh_discovery_record(parts[2]))
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_error(HTTPStatus.NOT_FOUND)

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
