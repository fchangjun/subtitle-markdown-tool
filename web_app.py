#!/usr/bin/env python3
import json
import mimetypes
import os
import random
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile
import argparse
import csv
import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests

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
ENV_PATH = ROOT / ".env"
DISCOVERY_DIR = OUTPUT_DIR / "discoveries"
CHANNELS_DIR = OUTPUT_DIR / "channels"
TRANSLATION_DIR = OUTPUT_DIR / "translations"
CHANNEL_INDEX_PATH = CHANNELS_DIR / "index.json"
MAX_BODY_BYTES = 128 * 1024


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_PATH)


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


def env_bool(name: str, default: bool = False) -> bool:
    value = clean_scalar(os.environ.get(name))
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


MAX_UPLOAD_BYTES = max(1, env_int("SUBTITLE_TRANSLATION_MAX_UPLOAD_BYTES", 200 * 1024 * 1024))
TRANSLATION_SUPPORTED_SUFFIXES = {".md", ".txt", ".srt", ".vtt"}


def normalize_discovery_provider(value: object) -> str:
    value = clean_scalar(value).lower().replace("-", "_")
    if value in {"youtube_api", "api", "youtube"}:
        return "youtube_api"
    if value in {"yt_dlp", "ytdlp", "yt-dlp"}:
        return "yt_dlp"
    return ""


def int_or_default(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
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
DISCOVERY_DEFAULT_MAX_PER_SOURCE = max(1, env_int("SUBTITLE_DISCOVERY_MAX_PER_SOURCE", 300))
DISCOVERY_HARD_MAX_PER_SOURCE = max(
    DISCOVERY_DEFAULT_MAX_PER_SOURCE,
    env_int("SUBTITLE_DISCOVERY_HARD_MAX_PER_SOURCE", 1000),
)
DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT = max(
    0,
    env_int("SUBTITLE_DISCOVERY_DETAIL_LOOKUP_LIMIT", DISCOVERY_DEFAULT_MAX_PER_SOURCE),
)
DISCOVERY_DETAIL_LOOKUP_HARD_LIMIT = max(
    DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT,
    env_int("SUBTITLE_DISCOVERY_DETAIL_LOOKUP_HARD_LIMIT", 1000),
)
DISCOVERY_DETAIL_LOOKUP_WORKERS = max(1, env_int("SUBTITLE_DISCOVERY_DETAIL_LOOKUP_WORKERS", 4))
DISCOVERY_DETAIL_TIMEOUT_SECONDS = max(3.0, env_float("SUBTITLE_DISCOVERY_DETAIL_TIMEOUT_SECONDS", 18.0))
DISCOVERY_DETAIL_SOCKET_TIMEOUT_SECONDS = max(3, env_int("SUBTITLE_DISCOVERY_DETAIL_SOCKET_TIMEOUT_SECONDS", 8))
DISCOVERY_YTDLP_LIST_TIMEOUT_SECONDS = max(5.0, env_float("SUBTITLE_DISCOVERY_YTDLP_LIST_TIMEOUT_SECONDS", 600.0))
DISCOVERY_YTDLP_BATCH_SIZE = max(1, min(500, env_int("SUBTITLE_DISCOVERY_YTDLP_BATCH_SIZE", 100)))
YOUTUBE_DATA_API_KEY = clean_scalar(os.environ.get("YOUTUBE_DATA_API_KEY") or os.environ.get("YOUTUBE_API_KEY"))
YOUTUBE_API_TIMEOUT_SECONDS = max(3.0, env_float("YOUTUBE_API_TIMEOUT_SECONDS", 20.0))
YOUTUBE_API_MAX_ATTEMPTS = max(1, env_int("YOUTUBE_API_MAX_ATTEMPTS", 3))
YOUTUBE_API_RETRY_BASE_SECONDS = max(0.0, env_float("YOUTUBE_API_RETRY_BASE_SECONDS", 2.0))
DISCOVERY_API_FALLBACK_TO_YTDLP = env_bool("SUBTITLE_DISCOVERY_API_FALLBACK_TO_YTDLP", False)
DISCOVERY_PROVIDER = normalize_discovery_provider(os.environ.get("SUBTITLE_DISCOVERY_PROVIDER")) or (
    "youtube_api" if YOUTUBE_DATA_API_KEY else "yt_dlp"
)
TRANSLATION_API_KEY = clean_scalar(
    os.environ.get("TRANSLATION_API_KEY")
    or os.environ.get("ONEAPI_API_KEY")
    or os.environ.get("ONEAPI_COMATE_API_KEY")
)
TRANSLATION_API_URL = clean_scalar(
    os.environ.get("TRANSLATION_API_URL")
    or "https://oneapi-comate.baidu-int.com/v1/chat/completions"
)
TRANSLATION_MODEL = clean_scalar(os.environ.get("TRANSLATION_MODEL") or "gpt-5.5")
TRANSLATION_API_TIMEOUT_SECONDS = max(10.0, env_float("TRANSLATION_API_TIMEOUT_SECONDS", 120.0))
TRANSLATION_API_MAX_ATTEMPTS = max(1, env_int("TRANSLATION_API_MAX_ATTEMPTS", 3))
TRANSLATION_RETRY_BASE_SECONDS = max(0.0, env_float("TRANSLATION_RETRY_BASE_SECONDS", 2.0))
TRANSLATION_CHUNK_CHARS = max(1500, env_int("TRANSLATION_CHUNK_CHARS", 8000))
TRANSLATION_FILE_MAX_ATTEMPTS = max(1, env_int("TRANSLATION_FILE_MAX_ATTEMPTS", 2))
DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API = max(
    1,
    min(
        DISCOVERY_HARD_MAX_PER_SOURCE,
        env_int("SUBTITLE_DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API", DISCOVERY_HARD_MAX_PER_SOURCE),
    ),
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
    archive_title: str = ""
    status: str = "queued"
    current: int = 0
    total: int = 0
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    message: str = ""
    cooldown_until: Optional[float] = None
    files: List[JobFile] = field(default_factory=list)
    errors: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class DiscoveryTask:
    id: str
    sources: List[str]
    range_text: str
    max_per_source: int
    detail_lookup_limit: int
    kind: str = "discover"
    channel_id: str = ""
    status: str = "queued"
    current: int = 0
    total: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    message: str = ""
    record_id: str = ""
    result: Optional[Dict[str, object]] = None
    error: str = ""
    source_progress: List[Dict[str, object]] = field(default_factory=list)


@dataclass
class TranslationFile:
    filename: str
    title: str
    source_filename: str
    size_bytes: int
    elapsed_seconds: float
    chunk_count: int
    retry_count: int = 0


@dataclass
class TranslationJob:
    id: str
    original_filename: str
    source_root_name: str
    output_folder_name: str
    input_zip_path: Path
    output_dir: Path
    status: str = "queued"
    current: int = 0
    total: int = 0
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    message: str = ""
    files: List[TranslationFile] = field(default_factory=list)
    errors: List[Dict[str, object]] = field(default_factory=list)
    retry_source_names: List[str] = field(default_factory=list)


jobs: Dict[str, Job] = {}
discovery_tasks: Dict[str, DiscoveryTask] = {}
translation_jobs: Dict[str, TranslationJob] = {}
jobs_lock = threading.Lock()
discovery_tasks_lock = threading.Lock()
translation_jobs_lock = threading.Lock()
settings_lock = threading.Lock()
job_queue: List[str] = []
job_queue_condition = threading.Condition()
job_worker_thread: Optional[threading.Thread] = None
translation_job_queue: List[str] = []
translation_job_queue_condition = threading.Condition()
translation_job_worker_thread: Optional[threading.Thread] = None


TERMINAL_JOB_STATUSES = {"completed", "completed_with_errors", "failed", "cancelled"}


class JobCancelled(Exception):
    pass


def mask_secret(value: str) -> str:
    value = clean_scalar(value)
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:6]}{'•' * 8}{value[-4:]}"


def reload_runtime_settings_from_env() -> None:
    global DISCOVERY_DEFAULT_MAX_PER_SOURCE
    global DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT
    global YOUTUBE_DATA_API_KEY
    global YOUTUBE_API_TIMEOUT_SECONDS
    global YOUTUBE_API_MAX_ATTEMPTS
    global YOUTUBE_API_RETRY_BASE_SECONDS
    global DISCOVERY_API_FALLBACK_TO_YTDLP
    global DISCOVERY_PROVIDER
    global TRANSLATION_API_KEY
    global TRANSLATION_API_URL
    global TRANSLATION_MODEL
    global TRANSLATION_API_TIMEOUT_SECONDS
    global TRANSLATION_API_MAX_ATTEMPTS
    global TRANSLATION_RETRY_BASE_SECONDS
    global TRANSLATION_CHUNK_CHARS
    global TRANSLATION_FILE_MAX_ATTEMPTS

    DISCOVERY_DEFAULT_MAX_PER_SOURCE = max(1, env_int("SUBTITLE_DISCOVERY_MAX_PER_SOURCE", 300))
    DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT = max(0, env_int("SUBTITLE_DISCOVERY_DETAIL_LOOKUP_LIMIT", DISCOVERY_DEFAULT_MAX_PER_SOURCE))
    YOUTUBE_DATA_API_KEY = clean_scalar(os.environ.get("YOUTUBE_DATA_API_KEY") or os.environ.get("YOUTUBE_API_KEY"))
    YOUTUBE_API_TIMEOUT_SECONDS = max(3.0, env_float("YOUTUBE_API_TIMEOUT_SECONDS", 20.0))
    YOUTUBE_API_MAX_ATTEMPTS = max(1, env_int("YOUTUBE_API_MAX_ATTEMPTS", 3))
    YOUTUBE_API_RETRY_BASE_SECONDS = max(0.0, env_float("YOUTUBE_API_RETRY_BASE_SECONDS", 2.0))
    DISCOVERY_API_FALLBACK_TO_YTDLP = env_bool("SUBTITLE_DISCOVERY_API_FALLBACK_TO_YTDLP", False)
    DISCOVERY_PROVIDER = normalize_discovery_provider(os.environ.get("SUBTITLE_DISCOVERY_PROVIDER")) or (
        "youtube_api" if YOUTUBE_DATA_API_KEY else "yt_dlp"
    )
    TRANSLATION_API_KEY = clean_scalar(
        os.environ.get("TRANSLATION_API_KEY")
        or os.environ.get("ONEAPI_API_KEY")
        or os.environ.get("ONEAPI_COMATE_API_KEY")
    )
    TRANSLATION_API_URL = clean_scalar(
        os.environ.get("TRANSLATION_API_URL")
        or "https://oneapi-comate.baidu-int.com/v1/chat/completions"
    )
    TRANSLATION_MODEL = clean_scalar(os.environ.get("TRANSLATION_MODEL") or "gpt-5.5")
    TRANSLATION_API_TIMEOUT_SECONDS = max(10.0, env_float("TRANSLATION_API_TIMEOUT_SECONDS", 120.0))
    TRANSLATION_API_MAX_ATTEMPTS = max(1, env_int("TRANSLATION_API_MAX_ATTEMPTS", 3))
    TRANSLATION_RETRY_BASE_SECONDS = max(0.0, env_float("TRANSLATION_RETRY_BASE_SECONDS", 2.0))
    TRANSLATION_CHUNK_CHARS = max(1500, env_int("TRANSLATION_CHUNK_CHARS", 8000))
    TRANSLATION_FILE_MAX_ATTEMPTS = max(1, env_int("TRANSLATION_FILE_MAX_ATTEMPTS", 2))


def write_env_values(updates: Dict[str, str]) -> None:
    existing = []
    if ENV_PATH.exists():
        existing = ENV_PATH.read_text(encoding="utf-8").splitlines()

    remaining = {key: str(value) for key, value in updates.items()}
    lines = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def settings_payload() -> Dict[str, object]:
    return {
        "youtube_api_key_configured": bool(YOUTUBE_DATA_API_KEY),
        "youtube_api_key_masked": mask_secret(YOUTUBE_DATA_API_KEY),
        "translation_api_key_configured": bool(TRANSLATION_API_KEY),
        "translation_api_key_masked": mask_secret(TRANSLATION_API_KEY),
        "translation_model": TRANSLATION_MODEL,
        "discovery_provider": DISCOVERY_PROVIDER,
        "discovery_check_limit": DISCOVERY_DEFAULT_MAX_PER_SOURCE,
        "api_fallback_to_ytdlp": DISCOVERY_API_FALLBACK_TO_YTDLP,
        "youtube_api_timeout_seconds": YOUTUBE_API_TIMEOUT_SECONDS,
        "youtube_api_max_attempts": YOUTUBE_API_MAX_ATTEMPTS,
        "youtube_api_retry_base_seconds": YOUTUBE_API_RETRY_BASE_SECONDS,
    }


def update_settings_from_payload(payload: Dict[str, object]) -> Dict[str, object]:
    updates: Dict[str, str] = {}

    if "youtube_api_key" in payload:
        api_key = clean_scalar(payload.get("youtube_api_key") or "")
        if api_key and "•" not in api_key:
            updates["YOUTUBE_DATA_API_KEY"] = api_key
        elif payload.get("clear_youtube_api_key"):
            updates["YOUTUBE_DATA_API_KEY"] = ""

    if "translation_api_key" in payload:
        api_key = clean_scalar(payload.get("translation_api_key") or "")
        if api_key and "•" not in api_key:
            updates["TRANSLATION_API_KEY"] = api_key
        elif payload.get("clear_translation_api_key"):
            updates["TRANSLATION_API_KEY"] = ""

    if "discovery_provider" in payload:
        provider = normalize_discovery_provider(payload.get("discovery_provider"))
        if not provider:
            raise ValueError("发现方式只能选择 YouTube API 或 yt-dlp。")
        updates["SUBTITLE_DISCOVERY_PROVIDER"] = provider

    if "api_fallback_to_ytdlp" in payload:
        updates["SUBTITLE_DISCOVERY_API_FALLBACK_TO_YTDLP"] = "1" if payload.get("api_fallback_to_ytdlp") else "0"

    if "discovery_check_limit" in payload:
        limit = max(1, min(DISCOVERY_HARD_MAX_PER_SOURCE, int(payload.get("discovery_check_limit") or 120)))
        updates["SUBTITLE_DISCOVERY_MAX_PER_SOURCE"] = str(limit)
        updates["SUBTITLE_DISCOVERY_DETAIL_LOOKUP_LIMIT"] = str(limit)

    if "youtube_api_timeout_seconds" in payload:
        timeout = max(3.0, float(payload.get("youtube_api_timeout_seconds") or 20.0))
        updates["YOUTUBE_API_TIMEOUT_SECONDS"] = str(round(timeout, 3))

    if "youtube_api_max_attempts" in payload:
        attempts = max(1, int(payload.get("youtube_api_max_attempts") or 3))
        updates["YOUTUBE_API_MAX_ATTEMPTS"] = str(attempts)

    if "youtube_api_retry_base_seconds" in payload:
        retry_base = max(0.0, float(payload.get("youtube_api_retry_base_seconds") or 2.0))
        updates["YOUTUBE_API_RETRY_BASE_SECONDS"] = str(round(retry_base, 3))

    with settings_lock:
        if updates:
            write_env_values(updates)
            for key, value in updates.items():
                os.environ[key] = value
            reload_runtime_settings_from_env()
        return settings_payload()
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


DiscoveryProgressCallback = Optional[Callable[[Dict[str, object]], None]]


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
    for key in ["upload_date", "release_date", "modified_date"]:
        raw_date = clean_scalar(entry.get(key))
        if not raw_date:
            continue
        for candidate, fmt in [(raw_date[:8], "%Y%m%d"), (raw_date[:10], "%Y-%m-%d")]:
            try:
                return datetime.strptime(candidate, fmt).date()
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


def date_from_iso_text(value: object) -> Optional[date]:
    text = clean_scalar(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def video_url_from_entry(entry: Dict[str, object]) -> str:
    video_id = clean_scalar(entry.get("id"))
    value = clean_scalar(entry.get("webpage_url") or entry.get("url"))
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return value


def thumbnail_url_from_entry(entry: Dict[str, object], video_url: str = "") -> str:
    for key in ["thumbnail", "thumbnail_url"]:
        value = clean_scalar(entry.get(key))
        if value.startswith("http://") or value.startswith("https://"):
            return value

    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        candidates = []
        for thumbnail in thumbnails:
            if not isinstance(thumbnail, dict):
                continue
            url = clean_scalar(thumbnail.get("url"))
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            width = number_or_none(thumbnail.get("width")) or 0
            height = number_or_none(thumbnail.get("height")) or 0
            candidates.append((width * height, url))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]

    video_id = clean_scalar(entry.get("id") or youtube_id_from_url(video_url))
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def number_or_none(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_youtube_url(value: str) -> bool:
    host = urlparse(value).netloc.lower()
    return "youtube.com" in host or host.endswith("youtu.be")


def parse_api_datetime(value: object) -> Optional[date]:
    text = clean_scalar(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return date_from_iso_text(text)


def thumbnail_url_from_api(thumbnails: object) -> str:
    if not isinstance(thumbnails, dict):
        return ""
    candidates = []
    for thumb in thumbnails.values():
        if not isinstance(thumb, dict):
            continue
        url = clean_scalar(thumb.get("url"))
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        width = number_or_none(thumb.get("width")) or 0
        height = number_or_none(thumb.get("height")) or 0
        candidates.append((width * height, url))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return ""


def youtube_api_get(endpoint: str, params: Dict[str, object]) -> Dict[str, object]:
    if not YOUTUBE_DATA_API_KEY:
        raise ValueError("YOUTUBE_DATA_API_KEY is not configured.")
    query = dict(params)
    query["key"] = YOUTUBE_DATA_API_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}"
    last_error = ""
    for attempt in range(1, YOUTUBE_API_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url,
                params=query,
                timeout=YOUTUBE_API_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            last_error = f"YouTube API {endpoint} request failed: {error}"
            retryable = True
        else:
            if response.status_code < 400:
                return response.json()
            try:
                payload = response.json()
                message = clean_scalar((payload.get("error") or {}).get("message") or response.text)
            except Exception:
                message = clean_scalar(response.text)
            last_error = f"YouTube API {endpoint} failed: HTTP {response.status_code} {message}"
            retryable = response.status_code in {408, 429, 500, 502, 503, 504}

        if not retryable or attempt >= YOUTUBE_API_MAX_ATTEMPTS:
            raise ValueError(last_error)
        delay = YOUTUBE_API_RETRY_BASE_SECONDS * attempt + random.uniform(0, 0.5)
        time.sleep(delay)
    raise ValueError(last_error or f"YouTube API {endpoint} failed")


def youtube_channel_lookup_params(source_url: str) -> Dict[str, object]:
    parsed = urlparse(source_url)
    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel":
        return {"id": parts[1]}
    if len(parts) >= 2 and parts[0] == "user":
        return {"forUsername": parts[1]}
    if parts and parts[0].startswith("@"):
        return {"forHandle": parts[0]}
    if parts and parts[0] not in {"videos", "streams", "shorts"}:
        return {"forHandle": parts[0]}
    raise ValueError("无法从频道链接识别 YouTube channel id 或 handle。")


def youtube_api_channel_for_source(source_url: str) -> Dict[str, object]:
    params = youtube_channel_lookup_params(source_url)
    payload = youtube_api_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            **params,
        },
    )
    items = payload.get("items") or []
    if not items:
        raise ValueError("YouTube API 未找到该频道。")
    return items[0]


def youtube_api_discover_source_videos(
    source_url: str,
    cutoff_date: date,
    max_per_source: int,
    progress_callback: DiscoveryProgressCallback = None,
    source_index: int = 1,
    source_count: int = 1,
) -> Dict[str, object]:
    channel = youtube_api_channel_for_source(source_url)
    snippet = channel.get("snippet") or {}
    uploads_playlist_id = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not uploads_playlist_id:
        raise ValueError("YouTube API 未返回 uploads playlist。")

    items = []
    scanned_count = 0
    unknown_date_count = 0
    out_of_range_count = 0
    api_request_count = 1
    next_page_token = ""
    stopped_by_cutoff = False
    page_index = 0
    while scanned_count < max_per_source:
        page_size = min(50, max_per_source - scanned_count)
        page_index += 1
        if progress_callback:
            progress_callback(
                {
                    "event": "api_page_start",
                    "source_url": source_url,
                    "source_index": source_index,
                    "source_count": source_count,
                    "page_index": page_index,
                    "page_size": page_size,
                    "scanned_count": scanned_count,
                }
            )
        payload = youtube_api_get(
            "playlistItems",
            {
                "part": "snippet,contentDetails,status",
                "playlistId": uploads_playlist_id,
                "maxResults": page_size,
                **({"pageToken": next_page_token} if next_page_token else {}),
            },
        )
        api_request_count += 1
        page_items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        if not page_items:
            break

        for playlist_item in page_items:
            scanned_count += 1
            item_snippet = playlist_item.get("snippet") or {}
            item_details = playlist_item.get("contentDetails") or {}
            resource_id = item_snippet.get("resourceId") or {}
            video_id = clean_scalar(item_details.get("videoId") or resource_id.get("videoId"))
            if not video_id:
                unknown_date_count += 1
                continue

            publish_date = parse_api_datetime(item_details.get("videoPublishedAt") or item_snippet.get("publishedAt"))
            if publish_date is None:
                unknown_date_count += 1
                continue
            if publish_date < cutoff_date:
                out_of_range_count += 1
                stopped_by_cutoff = True
                break

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            channel_id = clean_scalar(item_snippet.get("videoOwnerChannelId") or item_snippet.get("channelId"))
            channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
            items.append(
                {
                    "id": video_id,
                    "url": video_url,
                    "title": clean_scalar(item_snippet.get("title") or video_url),
                    "channel": clean_scalar(item_snippet.get("videoOwnerChannelTitle") or item_snippet.get("channelTitle") or snippet.get("title") or ""),
                    "channel_url": channel_url,
                    "publish_date": publish_date.isoformat(),
                    "date_known": True,
                    "in_range": True,
                    "duration_seconds": None,
                    "view_count": None,
                    "thumbnail_url": thumbnail_url_from_api(item_snippet.get("thumbnails")) or thumbnail_url_from_entry({"id": video_id}, video_url),
                    "description": clean_scalar(item_snippet.get("description") or ""),
                    "source_url": source_url,
                    "date_source": "youtube_api",
                }
            )

        next_page_token = clean_scalar(payload.get("nextPageToken"))
        if progress_callback:
            progress_callback(
                {
                    "event": "api_page_done",
                    "source_url": source_url,
                    "source_index": source_index,
                    "source_count": source_count,
                    "page_index": page_index,
                    "scanned_count": scanned_count,
                    "included_count": len(items),
                    "stopped_by_cutoff": stopped_by_cutoff,
                }
            )
        if stopped_by_cutoff:
            break
        if not next_page_token:
            break

    return {
        "source_url": source_url,
        "listing_url": discovery_listing_url(source_url),
        "title": clean_scalar(snippet.get("title") or source_url),
        "description": clean_scalar(snippet.get("description") or ""),
        "provider": "youtube_api",
        "api_request_count": api_request_count,
        "scanned_count": scanned_count,
        "included_count": len(items),
        "unknown_date_count": unknown_date_count,
        "out_of_range_count": out_of_range_count,
        "detail_lookup_count": 0,
        "detail_lookup_success_count": 0,
        "detail_lookup_error_count": 0,
        "detail_lookup_limit_reached": False,
        "stopped_by_cutoff": stopped_by_cutoff,
        "limit_reached": scanned_count >= max_per_source and bool(next_page_token),
        "items": items,
    }


def detail_lookup_limit_for(max_per_source: int, requested_limit: int) -> int:
    return max(0, min(max_per_source, requested_limit, DISCOVERY_DETAIL_LOOKUP_HARD_LIMIT))


def fetch_video_detail_fields(video_url: str) -> Dict[str, object]:
    output_template = "\t".join(
        [
            "%(upload_date|)s",
            "%(release_date|)s",
            "%(modified_date|)s",
            "%(timestamp|)s",
            "%(release_timestamp|)s",
            "%(modified_timestamp|)s",
            "%(duration|)s",
            "%(view_count|)s",
        ]
    )
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--quiet",
        "--no-warnings",
        "--skip-download",
        "--no-playlist",
        "--socket-timeout",
        str(DISCOVERY_DETAIL_SOCKET_TIMEOUT_SECONDS),
        "--extractor-retries",
        "1",
        "--print",
        output_template,
        video_url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=DISCOVERY_DETAIL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"补日期超时（>{DISCOVERY_DETAIL_TIMEOUT_SECONDS:.0f}s）"}
    except Exception as error:
        return {"error": str(error)}

    if completed.returncode != 0:
        message = clean_scalar(completed.stderr or completed.stdout or "补日期失败")
        return {"error": message}

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return {"error": "补日期无返回"}

    values = lines[-1].split("\t")
    keys = [
        "upload_date",
        "release_date",
        "modified_date",
        "timestamp",
        "release_timestamp",
        "modified_timestamp",
        "duration",
        "view_count",
    ]
    detail = {}
    for key, value in zip(keys, values):
        value = clean_scalar(value)
        if value and value not in {"NA", "None", "null"}:
            detail[key] = value
    return detail


def fetch_detail_fields_for(entries: List[Dict[str, object]], limit: int) -> Dict[str, Dict[str, object]]:
    targets = []
    seen = set()
    for entry in entries:
        if date_from_video_entry(entry) is not None:
            continue
        video_url = video_url_from_entry(entry)
        key = normalize_url(video_url)
        if not video_url or key in seen:
            continue
        seen.add(key)
        targets.append((key, video_url))
        if len(targets) >= limit:
            break

    if not targets:
        return {}

    results: Dict[str, Dict[str, object]] = {}
    worker_count = min(DISCOVERY_DETAIL_LOOKUP_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_key = {
            executor.submit(fetch_video_detail_fields, video_url): key
            for key, video_url in targets
        }
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as error:
                results[key] = {"error": str(error)}
    return results


def merged_entry_with_detail(entry: Dict[str, object], detail: Dict[str, object]) -> Dict[str, object]:
    if not detail or detail.get("error"):
        return entry
    merged = dict(entry)
    for key, value in detail.items():
        if key == "error" or value in (None, ""):
            continue
        if merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def ytdlp_extract_listing_range(listing_url: str, start_index: int, end_index: int) -> Dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--flat-playlist",
        "--extractor-args",
        "youtubetab:approximate_date=1",
        "--dump-single-json",
        listing_url,
    ]
    if start_index <= 1:
        command[4:4] = ["--playlist-end", str(end_index)]
    else:
        command[4:4] = ["--playlist-items", f"{start_index}-{end_index}"]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=DISCOVERY_YTDLP_LIST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"yt-dlp 列表抓取超过 {int(DISCOVERY_YTDLP_LIST_TIMEOUT_SECONDS)} 秒") from error

    if completed.returncode != 0:
        message = "\n".join(completed.stderr.splitlines()[-6:]).strip()
        raise RuntimeError(message or "yt-dlp 列表抓取失败")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("yt-dlp 列表返回内容无法解析") from error

    if not isinstance(payload, dict):
        raise RuntimeError("yt-dlp 列表返回内容格式异常")
    return payload


def discover_source_videos(
    source_url: str,
    cutoff_date: date,
    max_per_source: int,
    detail_lookup_limit: int,
    progress_callback: DiscoveryProgressCallback = None,
    source_index: int = 1,
    source_count: int = 1,
) -> Dict[str, object]:
    listing_url = discovery_listing_url(source_url)
    is_single_video = bool(youtube_id_from_url(listing_url))
    if is_single_video:
        video_url = normalize_url(listing_url)
        detail = fetch_video_detail_fields(video_url) if detail_lookup_limit > 0 else {}
        publish_date = date_from_video_entry(detail)
        entry = {
            "id": youtube_id_from_url(listing_url),
            "url": video_url,
            "title": video_url,
            "channel": "",
            "channel_url": "",
            "publish_date": publish_date.isoformat() if publish_date else "",
            "date_known": publish_date is not None,
            "in_range": publish_date >= cutoff_date if publish_date else None,
            "duration_seconds": parse_duration_seconds(detail.get("duration")),
            "view_count": number_or_none(detail.get("view_count")),
            "thumbnail_url": thumbnail_url_from_entry({"id": youtube_id_from_url(listing_url)}, video_url),
            "description": "",
            "source_url": source_url,
        }
        return {
            "source_url": source_url,
            "listing_url": listing_url,
            "title": entry["title"],
            "provider": "yt_dlp",
            "scanned_count": 1,
            "included_count": 1 if publish_date and publish_date >= cutoff_date else 0,
            "unknown_date_count": 0 if publish_date else 1,
            "out_of_range_count": 0 if not publish_date or publish_date >= cutoff_date else 1,
            "detail_lookup_count": 1 if detail_lookup_limit > 0 else 0,
            "detail_lookup_success_count": 1 if publish_date else 0,
            "detail_lookup_error_count": 1 if detail.get("error") else 0,
            "detail_lookup_limit_reached": False,
            "limit_reached": False,
            "items": [entry] if publish_date and publish_date >= cutoff_date else [],
        }

    api_error = ""
    if DISCOVERY_PROVIDER == "youtube_api" and is_youtube_url(listing_url):
        if not YOUTUBE_DATA_API_KEY:
            raise RuntimeError("已选择 YouTube API 发现方式，请先在右上角设置里填写 YOUTUBE_DATA_API_KEY。")
        try:
            return youtube_api_discover_source_videos(
                source_url,
                cutoff_date,
                max_per_source,
                progress_callback=progress_callback,
                source_index=source_index,
                source_count=source_count,
            )
        except Exception as error:
            api_error = str(error)
            if not DISCOVERY_API_FALLBACK_TO_YTDLP:
                raise RuntimeError(
                    f"YouTube API 查询失败，未降级到 yt-dlp 以避免结果不一致：{api_error}"
                ) from error

    requested_max_per_source = max_per_source
    yt_dlp_max_per_source = min(max_per_source, DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API)
    detail_lookup_limit = detail_lookup_limit_for(max_per_source, detail_lookup_limit)
    items = []
    source_title = listing_url
    source_description = ""
    scanned_count = 0
    unknown_date_count = 0
    out_of_range_count = 0
    detail_lookup_count = 0
    detail_lookup_success_count = 0
    detail_lookup_error_count = 0
    detail_lookup_limit_reached = False
    stopped_by_cutoff = False
    ytdlp_batch_count = 0
    seen_source_video_urls = set()
    batch_start = 1
    while batch_start <= yt_dlp_max_per_source:
        batch_end = min(yt_dlp_max_per_source, batch_start + DISCOVERY_YTDLP_BATCH_SIZE - 1)
        if progress_callback:
            progress_callback(
                {
                    "event": "yt_dlp_batch_start",
                    "source_url": source_url,
                    "source_index": source_index,
                    "source_count": source_count,
                    "batch_index": ytdlp_batch_count + 1,
                    "batch_start": batch_start,
                    "batch_end": batch_end,
                    "scanned_count": scanned_count,
                    "included_count": len(items),
                }
            )
        info = ytdlp_extract_listing_range(listing_url, batch_start, batch_end)
        ytdlp_batch_count += 1
        if ytdlp_batch_count == 1:
            source_title = clean_scalar(info.get("title") or listing_url)
            source_description = clean_scalar(info.get("description") or info.get("channel_description") or "")

        entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
        if not entries:
            break

        remaining_detail_limit = max(0, detail_lookup_limit - detail_lookup_count)
        detail_by_url = fetch_detail_fields_for(entries, remaining_detail_limit)
        detail_lookup_count += len(detail_by_url)
        detail_lookup_success_count += sum(
            1
            for detail in detail_by_url.values()
            if not detail.get("error") and date_from_video_entry(detail) is not None
        )
        detail_lookup_error_count += sum(1 for detail in detail_by_url.values() if detail.get("error"))
        detail_lookup_limit_reached = detail_lookup_limit_reached or (
            sum(1 for entry in entries if date_from_video_entry(entry) is None) > len(detail_by_url)
        )

        for entry in entries:
            scanned_count += 1
            video_url = video_url_from_entry(entry)
            if not video_url:
                continue

            normalized_video_url = normalize_url(video_url)
            if normalized_video_url in seen_source_video_urls:
                continue
            seen_source_video_urls.add(normalized_video_url)

            detail = detail_by_url.get(normalized_video_url, {})
            entry = merged_entry_with_detail(entry, detail)
            publish_date = date_from_video_entry(entry)
            date_known = publish_date is not None
            in_range = publish_date >= cutoff_date if date_known else None
            if not date_known:
                unknown_date_count += 1
                continue
            if not in_range:
                out_of_range_count += 1
                stopped_by_cutoff = True
                break

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
                    "thumbnail_url": thumbnail_url_from_entry(entry, video_url),
                    "description": clean_scalar(entry.get("description") or ""),
                    "source_url": source_url,
                    "date_source": "yt_dlp_approximate",
                }
            )

        if stopped_by_cutoff or len(entries) < (batch_end - batch_start + 1):
            if progress_callback:
                progress_callback(
                    {
                        "event": "yt_dlp_batch_done",
                        "source_url": source_url,
                        "source_index": source_index,
                        "source_count": source_count,
                        "batch_index": ytdlp_batch_count,
                        "batch_start": batch_start,
                        "batch_end": batch_end,
                        "scanned_count": scanned_count,
                        "included_count": len(items),
                        "stopped_by_cutoff": stopped_by_cutoff,
                    }
                )
            break
        if progress_callback:
            progress_callback(
                {
                    "event": "yt_dlp_batch_done",
                    "source_url": source_url,
                    "source_index": source_index,
                    "source_count": source_count,
                    "batch_index": ytdlp_batch_count,
                    "batch_start": batch_start,
                    "batch_end": batch_end,
                    "scanned_count": scanned_count,
                    "included_count": len(items),
                    "stopped_by_cutoff": stopped_by_cutoff,
                }
            )
        batch_start = batch_end + 1

    return {
        "source_url": source_url,
        "listing_url": listing_url,
        "title": source_title,
        "description": source_description,
        "provider": "yt_dlp",
        "provider_fallback_reason": api_error,
        "scanned_count": scanned_count,
        "included_count": len(items),
        "unknown_date_count": unknown_date_count,
        "out_of_range_count": out_of_range_count,
        "detail_lookup_count": detail_lookup_count,
        "detail_lookup_success_count": detail_lookup_success_count,
        "detail_lookup_error_count": detail_lookup_error_count,
        "detail_lookup_limit_reached": detail_lookup_limit_reached,
        "yt_dlp_batch_count": ytdlp_batch_count,
        "yt_dlp_batch_size": DISCOVERY_YTDLP_BATCH_SIZE,
        "stopped_by_cutoff": stopped_by_cutoff,
        "yt_dlp_capped": yt_dlp_max_per_source < requested_max_per_source,
        "requested_max_per_source": requested_max_per_source,
        "limit_reached": scanned_count >= yt_dlp_max_per_source,
        "items": items,
    }


def discover_videos(
    sources: List[str],
    range_text: str,
    max_per_source: int,
    detail_lookup_limit: int,
    progress_callback: DiscoveryProgressCallback = None,
) -> Dict[str, object]:
    cutoff_date = parse_discovery_range(range_text)
    max_per_source = max(1, min(max_per_source, DISCOVERY_HARD_MAX_PER_SOURCE))
    detail_lookup_limit = detail_lookup_limit_for(max_per_source, detail_lookup_limit)

    all_items = []
    source_results = []
    seen_urls = set()
    source_count = len(sources)
    for source_index, source_url in enumerate(sources, start=1):
        started = time.monotonic()
        if progress_callback:
            progress_callback(
                {
                    "event": "source_start",
                    "source_url": source_url,
                    "source_index": source_index,
                    "source_count": source_count,
                }
            )
        try:
            result = discover_source_videos(
                source_url,
                cutoff_date,
                max_per_source,
                detail_lookup_limit,
                progress_callback=progress_callback,
                source_index=source_index,
                source_count=source_count,
            )
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
            if progress_callback:
                progress_callback(
                    {
                        "event": "source_done",
                        "source_url": source_url,
                        "source_index": source_index,
                        "source_count": source_count,
                        "scanned_count": result.get("scanned_count", 0),
                        "included_count": result.get("included_count", 0),
                        "stopped_by_cutoff": result.get("stopped_by_cutoff", False),
                    }
                )
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
            if progress_callback:
                progress_callback(
                    {
                        "event": "source_error",
                        "source_url": source_url,
                        "source_index": source_index,
                        "source_count": source_count,
                        "error": str(error),
                }
            )

    if source_results and all(source.get("error") for source in source_results):
        first_error = clean_scalar(source_results[0].get("error") or "未知错误")
        raise RuntimeError(f"所有频道扫描失败：{first_error}")

    all_items.sort(key=lambda item: item.get("publish_date") or "0000-00-00", reverse=True)
    provider_counts = {}
    for source in source_results:
        provider = clean_scalar(source.get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
    return {
        "range": {
            "input": range_text,
            "cutoff_date": cutoff_date.isoformat(),
        },
        "max_per_source": max_per_source,
        "detail_lookup_limit": detail_lookup_limit,
        "items": all_items,
        "sources": source_results,
        "summary": {
            "source_count": len(sources),
            "video_count": len(all_items),
            "scanned_count": sum(int(source.get("scanned_count") or 0) for source in source_results),
            "unknown_date_count": sum(int(source.get("unknown_date_count") or 0) for source in source_results),
            "out_of_range_count": sum(int(source.get("out_of_range_count") or 0) for source in source_results),
            "detail_lookup_count": sum(int(source.get("detail_lookup_count") or 0) for source in source_results),
            "detail_lookup_success_count": sum(int(source.get("detail_lookup_success_count") or 0) for source in source_results),
            "detail_lookup_error_count": sum(int(source.get("detail_lookup_error_count") or 0) for source in source_results),
            "detail_lookup_limit_reached_count": sum(1 for source in source_results if source.get("detail_lookup_limit_reached")),
            "api_request_count": sum(int(source.get("api_request_count") or 0) for source in source_results),
            "yt_dlp_batch_count": sum(int(source.get("yt_dlp_batch_count") or 0) for source in source_results),
            "yt_dlp_batch_size": DISCOVERY_YTDLP_BATCH_SIZE,
            "stopped_by_cutoff_count": sum(1 for source in source_results if source.get("stopped_by_cutoff")),
            "provider_counts": provider_counts,
            "fallback_count": sum(1 for source in source_results if source.get("provider_fallback_reason")),
            "yt_dlp_capped_count": sum(1 for source in source_results if source.get("yt_dlp_capped")),
            "yt_dlp_cap": DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API,
            "youtube_api_configured": bool(YOUTUBE_DATA_API_KEY),
            "error_count": sum(1 for source in source_results if source.get("error")),
            "limit_reached_count": sum(1 for source in source_results if source.get("limit_reached")),
        },
    }


def strict_discovery_record(record: Dict[str, object]) -> Dict[str, object]:
    cutoff_date = date_from_iso_text((record.get("range") or {}).get("cutoff_date"))
    if cutoff_date is None:
        return record

    original_items = [item for item in (record.get("items") or []) if isinstance(item, dict)]
    filtered_items = []
    skipped_unknown = 0
    for item in original_items:
        publish_date = date_from_iso_text(item.get("publish_date"))
        if publish_date is None:
            skipped_unknown += 1
            continue
        if publish_date < cutoff_date:
            continue
        filtered_item = dict(item)
        filtered_item["date_known"] = True
        filtered_item["in_range"] = True
        filtered_items.append(filtered_item)

    if len(filtered_items) == len(original_items) and not skipped_unknown:
        return record

    normalized = dict(record)
    normalized["items"] = filtered_items
    source_counts: Dict[str, int] = {}
    for item in filtered_items:
        source_url = clean_scalar(item.get("source_url"))
        if not source_url:
            continue
        source_key = channel_listing_key(source_url)
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
    normalized_sources = []
    for source in record.get("sources") or []:
        if not isinstance(source, dict):
            continue
        normalized_source = dict(source)
        source_key = channel_listing_key(clean_scalar(source.get("source_url") or source.get("listing_url")))
        normalized_source["included_count"] = source_counts.get(source_key, 0)
        normalized_sources.append(normalized_source)
    normalized["sources"] = normalized_sources
    summary = dict(normalized.get("summary") or {})
    summary["video_count"] = len(filtered_items)
    summary["unknown_date_count"] = max(int(summary.get("unknown_date_count") or 0), skipped_unknown)
    normalized["summary"] = summary
    return normalized


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
    payload = {
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
    if payload.get("last_discovery_id"):
        try:
            record = load_discovery_record(str(payload["last_discovery_id"]))
            source_key = channel_listing_key(clean_scalar(payload.get("source_url") or payload.get("listing_url")))
            items = [
                item for item in (record.get("items") or [])
                if isinstance(item, dict)
                and channel_listing_key(clean_scalar(item.get("source_url") or payload.get("source_url"))) == source_key
            ]
            payload["video_count"] = len(items)
            payload["known_date_count"] = sum(1 for item in items if item.get("date_known"))
        except Exception:
            pass
    return payload


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
        items = channel_items_for(str(payload["id"]))
        if payload.get("last_discovery_id"):
            try:
                record = load_discovery_record(str(payload["last_discovery_id"]))
                allowed_urls = {
                    normalize_url(clean_scalar(item.get("url")))
                    for item in (record.get("items") or [])
                    if isinstance(item, dict) and clean_scalar(item.get("url"))
                }
                items = [item for item in items if normalize_url(clean_scalar(item.get("url"))) in allowed_urls]
            except Exception:
                pass
        payload["items"] = items
        payload["video_count"] = len(items)
        payload["known_date_count"] = sum(1 for item in items if item.get("date_known"))
        return payload


def refresh_followed_channel(
    channel_id: str,
    range_text: str,
    max_per_source: int,
    detail_lookup_limit: int,
) -> Dict[str, object]:
    channel = get_followed_channel(channel_id)
    source = clean_scalar(channel.get("source_url") or channel.get("listing_url"))
    record = create_discovery_record([source], range_text, max_per_source, detail_lookup_limit)
    return {
        "record": record,
        "channel": get_followed_channel(channel_id),
        "channels": list_followed_channels(),
    }


def discovery_units_per_source(max_per_source: int) -> int:
    limit = max(1, min(max_per_source, DISCOVERY_HARD_MAX_PER_SOURCE))
    if not YOUTUBE_DATA_API_KEY:
        limit = min(limit, DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API)
        unit_size = DISCOVERY_YTDLP_BATCH_SIZE
    else:
        unit_size = 50
    return max(1, (limit + unit_size - 1) // unit_size)


def discovery_task_to_dict(task: DiscoveryTask) -> Dict[str, object]:
    payload = {
        "id": task.id,
        "kind": task.kind,
        "channel_id": task.channel_id,
        "status": task.status,
        "current": task.current,
        "total": task.total,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "message": task.message,
        "record_id": task.record_id,
        "error": task.error,
        "sources": task.sources,
        "range": task.range_text,
        "max_per_source": task.max_per_source,
        "detail_lookup_limit": task.detail_lookup_limit,
        "source_progress": task.source_progress,
    }
    if task.result is not None and task.status in {"completed", "completed_with_errors"}:
        payload["result"] = task.result
    return payload


def update_discovery_task(task_id: str, **changes) -> None:
    with discovery_tasks_lock:
        task = discovery_tasks[task_id]
        for key, value in changes.items():
            setattr(task, key, value)
        task.updated_at = time.time()


def discovery_progress_callback(task_id: str) -> Callable[[Dict[str, object]], None]:
    def callback(event: Dict[str, object]) -> None:
        with discovery_tasks_lock:
            task = discovery_tasks.get(task_id)
            if not task:
                return
            units_per_source = max(1, discovery_units_per_source(task.max_per_source))
            source_index = max(1, int(event.get("source_index") or 1))
            source_count = max(1, int(event.get("source_count") or len(task.sources) or 1))
            source_url = clean_scalar(event.get("source_url") or "")
            base = (source_index - 1) * units_per_source
            event_name = clean_scalar(event.get("event") or "")
            source_row = None
            if 1 <= source_index <= len(task.source_progress):
                source_row = task.source_progress[source_index - 1]

            if event_name == "source_start":
                task.current = max(task.current, min(task.total, base))
                task.message = f"频道 {source_index}/{source_count}：准备扫描 {source_url}"
                if source_row is not None:
                    source_row.update(
                        {
                            "status": "running",
                            "current": 0,
                            "message": "准备扫描",
                        }
                    )
            elif event_name in {"yt_dlp_batch_start", "api_page_start"}:
                batch_index = max(1, int(event.get("batch_index") or event.get("page_index") or 1))
                start = event.get("batch_start")
                end = event.get("batch_end")
                task.current = max(task.current, min(task.total, base + batch_index - 1))
                if start and end:
                    task.message = f"频道 {source_index}/{source_count}：扫描第 {batch_index} 批（{start}-{end}）"
                else:
                    task.message = f"频道 {source_index}/{source_count}：读取第 {batch_index} 页"
                if source_row is not None:
                    source_row.update(
                        {
                            "status": "running",
                            "current": max(0, batch_index - 1),
                            "message": task.message,
                        }
                    )
            elif event_name in {"yt_dlp_batch_done", "api_page_done"}:
                batch_index = max(1, int(event.get("batch_index") or event.get("page_index") or 1))
                scanned_count = int(event.get("scanned_count") or 0)
                included_count = int(event.get("included_count") or 0)
                task.current = max(task.current, min(task.total, base + batch_index))
                suffix = "，已到时间边界" if event.get("stopped_by_cutoff") else ""
                task.message = f"频道 {source_index}/{source_count}：已扫 {scanned_count} 条，候选 {included_count} 条{suffix}"
                if source_row is not None:
                    source_row.update(
                        {
                            "status": "running",
                            "current": min(units_per_source, batch_index),
                            "scanned_count": scanned_count,
                            "included_count": included_count,
                            "message": f"已扫 {scanned_count} 条，候选 {included_count} 条{suffix}",
                        }
                    )
            elif event_name == "source_done":
                included_count = int(event.get("included_count") or 0)
                scanned_count = int(event.get("scanned_count") or 0)
                task.current = max(task.current, min(task.total, source_index * units_per_source))
                task.message = f"频道 {source_index}/{source_count} 完成：已扫 {scanned_count} 条，候选 {included_count} 条"
                if source_row is not None:
                    source_row.update(
                        {
                            "status": "completed",
                            "current": units_per_source,
                            "scanned_count": scanned_count,
                            "included_count": included_count,
                            "message": f"完成：已扫 {scanned_count} 条，候选 {included_count} 条",
                        }
                    )
            elif event_name == "source_error":
                task.current = max(task.current, min(task.total, source_index * units_per_source))
                task.message = f"频道 {source_index}/{source_count} 失败：{clean_scalar(event.get('error') or '')}"
                if source_row is not None:
                    source_row.update(
                        {
                            "status": "failed",
                            "current": units_per_source,
                            "message": clean_scalar(event.get("error") or "扫描失败"),
                        }
                    )
            task.updated_at = time.time()
    return callback


def process_discovery_task(task_id: str) -> None:
    with discovery_tasks_lock:
        task = discovery_tasks[task_id]
        task.status = "running"
        task.started_at = time.time()
        task.message = "扫描任务已开始"
        task.updated_at = time.time()
        sources = list(task.sources)
        range_text = task.range_text
        max_per_source = task.max_per_source
        detail_lookup_limit = task.detail_lookup_limit
        kind = task.kind
        channel_id = task.channel_id

    try:
        record = create_discovery_record(
            sources,
            range_text,
            max_per_source,
            detail_lookup_limit,
            progress_callback=discovery_progress_callback(task_id),
        )
        if kind == "channel_refresh" and channel_id:
            result = {
                "record": record,
                "channel": get_followed_channel(channel_id),
                "channels": list_followed_channels(),
            }
        else:
            result = record
        with discovery_tasks_lock:
            task = discovery_tasks[task_id]
            task.status = "completed"
            task.current = task.total
            task.completed_at = time.time()
            task.record_id = clean_scalar(record.get("id") or "")
            task.result = result
            task.message = "扫描完成"
            task.updated_at = time.time()
    except Exception as error:
        with discovery_tasks_lock:
            task = discovery_tasks[task_id]
            task.status = "failed"
            task.completed_at = time.time()
            task.error = str(error)
            task.message = f"扫描失败：{error}"
            task.updated_at = time.time()
        traceback.print_exc()


def create_discovery_task(
    sources: List[str],
    range_text: str,
    max_per_source: int,
    detail_lookup_limit: int,
    kind: str = "discover",
    channel_id: str = "",
) -> Dict[str, object]:
    max_per_source = max(1, min(max_per_source, DISCOVERY_HARD_MAX_PER_SOURCE))
    detail_lookup_limit = detail_lookup_limit_for(max_per_source, detail_lookup_limit)
    task_id = uuid.uuid4().hex[:12]
    units_per_source = discovery_units_per_source(max_per_source)
    total = max(1, len(sources) * units_per_source)
    source_progress = [
        {
            "source_url": source,
            "index": index,
            "status": "queued",
            "current": 0,
            "total": units_per_source,
            "scanned_count": 0,
            "included_count": 0,
            "message": "等待扫描",
        }
        for index, source in enumerate(sources, start=1)
    ]
    task = DiscoveryTask(
        id=task_id,
        sources=sources,
        range_text=range_text,
        max_per_source=max_per_source,
        detail_lookup_limit=detail_lookup_limit,
        kind=kind,
        channel_id=channel_id,
        total=total,
        message="扫描任务已排队",
        source_progress=source_progress,
    )
    with discovery_tasks_lock:
        discovery_tasks[task_id] = task
    thread = threading.Thread(target=process_discovery_task, args=(task_id,), daemon=True)
    thread.start()
    return discovery_task_to_dict(task)


def safe_discovery_id(value: str) -> str:
    value = clean_scalar(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", value):
        raise ValueError("Discovery record not found.")
    return value


def discovery_record_path(record_id: str) -> Path:
    return DISCOVERY_DIR / f"{safe_discovery_id(record_id)}.json"


def discovery_record_summary(record: Dict[str, object]) -> Dict[str, object]:
    record = strict_discovery_record(record)
    summary = dict(record.get("summary") or {})
    return {
        "id": record.get("id", ""),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "range": record.get("range"),
        "max_per_source": record.get("max_per_source"),
        "detail_lookup_limit": record.get("detail_lookup_limit"),
        "source_count": summary.get("source_count", 0),
        "video_count": summary.get("video_count", 0),
        "scanned_count": summary.get("scanned_count", 0),
        "unknown_date_count": summary.get("unknown_date_count", 0),
        "out_of_range_count": summary.get("out_of_range_count", 0),
        "detail_lookup_count": summary.get("detail_lookup_count", 0),
        "detail_lookup_success_count": summary.get("detail_lookup_success_count", 0),
        "detail_lookup_error_count": summary.get("detail_lookup_error_count", 0),
        "detail_lookup_limit_reached_count": summary.get("detail_lookup_limit_reached_count", 0),
        "api_request_count": summary.get("api_request_count", 0),
        "yt_dlp_batch_count": summary.get("yt_dlp_batch_count", 0),
        "yt_dlp_batch_size": summary.get("yt_dlp_batch_size", DISCOVERY_YTDLP_BATCH_SIZE),
        "stopped_by_cutoff_count": summary.get("stopped_by_cutoff_count", 0),
        "provider_counts": summary.get("provider_counts", {}),
        "fallback_count": summary.get("fallback_count", 0),
        "yt_dlp_capped_count": summary.get("yt_dlp_capped_count", 0),
        "yt_dlp_cap": summary.get("yt_dlp_cap", DISCOVERY_YTDLP_MAX_PER_SOURCE_WITHOUT_API),
        "youtube_api_configured": summary.get("youtube_api_configured", bool(YOUTUBE_DATA_API_KEY)),
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
    return strict_discovery_record(json.loads(path.read_text(encoding="utf-8")))


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


def create_discovery_record(
    sources: List[str],
    range_text: str,
    max_per_source: int,
    detail_lookup_limit: int,
    progress_callback: DiscoveryProgressCallback = None,
) -> Dict[str, object]:
    record = discover_videos(sources, range_text, max_per_source, detail_lookup_limit, progress_callback=progress_callback)
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
    detail_lookup_limit = int_or_default(previous.get("detail_lookup_limit"), DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT)
    record = discover_videos(sources, range_text, max_per_source, detail_lookup_limit)
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


def safe_download_name_part(value: str) -> str:
    value = clean_scalar(value)
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:100].strip(" .")


def zip_download_filename(job: Job) -> str:
    title = safe_download_name_part(job.archive_title)
    if not title:
        return f"subtitles_{job.id}.zip"
    timestamp = datetime.fromtimestamp(job.created_at).strftime("%Y%m%d_%H%M")
    return f"{title}_{timestamp}.zip"


def ascii_download_fallback(filename: str) -> str:
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    fallback = re.sub(r"_+", "_", fallback).strip("._")
    return fallback or "subtitles.zip"


def attachment_content_disposition(filename: str) -> str:
    fallback = ascii_download_fallback(filename).replace('"', "_")
    encoded = quote(filename, safe="")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


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
        "archive_title": job.archive_title,
        "download_filename": zip_download_filename(job),
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "cancel_requested": job.cancel_requested,
        "queue_position": job_queue_position(job.id),
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


def job_queue_position(job_id: str) -> int:
    with job_queue_condition:
        try:
            return job_queue.index(job_id) + 1
        except ValueError:
            return 0


def remove_queued_job(job_id: str) -> bool:
    with job_queue_condition:
        before = len(job_queue)
        job_queue[:] = [queued_id for queued_id in job_queue if queued_id != job_id]
        return len(job_queue) != before


def enqueue_job(job_id: str) -> None:
    with job_queue_condition:
        if job_id not in job_queue:
            job_queue.append(job_id)
        job_queue_condition.notify()
    ensure_job_worker()


def ensure_job_worker() -> None:
    global job_worker_thread
    with job_queue_condition:
        if job_worker_thread and job_worker_thread.is_alive():
            return
        job_worker_thread = threading.Thread(target=job_worker_loop, daemon=True)
        job_worker_thread.start()


def job_worker_loop() -> None:
    while True:
        with job_queue_condition:
            while not job_queue:
                job_queue_condition.wait()
            job_id = job_queue.pop(0)

        with jobs_lock:
            job = jobs.get(job_id)
            should_run = bool(job and job.status == "queued" and not job.cancel_requested)
        if not should_run:
            continue

        try:
            process_job(job_id)
        except Exception as error:
            traceback.print_exc()
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    continue
                job.status = "failed"
                job.message = str(error)
                job.cooldown_until = None
                job.completed_at = time.time()
                job.updated_at = time.time()
                failed_job = job
            write_job_artifacts(failed_job)


def is_job_cancel_requested(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.cancel_requested)


def request_job_stop(job_id: str) -> Job:
    remove_from_queue = False
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise ValueError("Job not found.")
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        job.cancel_requested = True
        job.cooldown_until = None
        if job.status == "queued":
            remove_from_queue = True
            message = "已停止，任务尚未开始。"
        else:
            message = "已停止，当前未完成视频已忽略。"
        job.status = "cancelled"
        job.message = message
        job.completed_at = time.time()
        job.updated_at = time.time()
        stopped_job = job

    if remove_from_queue:
        remove_queued_job(job_id)
    write_job_artifacts(stopped_job)
    return stopped_job


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


def sleep_with_cancel(job_id: str, seconds: float) -> None:
    deadline = time.monotonic() + max(0, seconds)
    while True:
        if is_job_cancel_requested(job_id):
            raise JobCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def pause_job(job_id: str, message: str, seconds: float) -> None:
    if seconds <= 0:
        return
    set_job_notice(job_id, message, time.time() + seconds)
    try:
        sleep_with_cancel(job_id, seconds)
    except JobCancelled:
        set_job_notice(job_id, "正在停止，当前任务将结束。", None)
        raise
    clear_job_notice(job_id)


def add_file(job_id: str, file: JobFile) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        if job.status in TERMINAL_JOB_STATUSES or job.cancel_requested:
            return
        job.files.append(file)
        job.updated_at = time.time()
    register_cache_entry(job, file)
    persist_job_summary(job)


def add_error_record(job_id: str, error: Dict[str, object]) -> None:
    job = None
    with jobs_lock:
        job = jobs[job_id]
        if job.status in TERMINAL_JOB_STATUSES or job.cancel_requested:
            return
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
        archive_title=clean_scalar(data.get("archive_title") or ""),
        status=status,
        current=int(data.get("current") or len(files) + len(errors)),
        total=int(data.get("total") or len(files) + len(errors)),
        cancel_requested=bool(data.get("cancel_requested", False)) if status not in TERMINAL_JOB_STATUSES else False,
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


def task_retry_sleep(job_id: str, attempt: int) -> None:
    delay = min(TASK_RETRY_MAX_SECONDS, TASK_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    sleep_with_cancel(job_id, delay + random.uniform(0, TASK_RETRY_JITTER_SECONDS))


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


def run_process_url_cancellable(
    job_id: str,
    url: str,
    index: int,
    output_dir: Path,
    retry_counter: List[int],
) -> JobFile:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}_{index:03d}_", dir=str(output_dir)))
    result_queue: Queue = Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((process_url(url, index, temp_dir, retry_counter), None))
        except BaseException as error:
            result_queue.put((None, error))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    while True:
        if is_job_cancel_requested(job_id):
            thread.join()
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise JobCancelled()
        try:
            file, error = result_queue.get(timeout=0.5)
            break
        except Empty:
            continue

    if error:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if isinstance(error, JobCancelled):
            raise error
        raise error
    if is_job_cancel_requested(job_id):
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise JobCancelled()

    source_path = temp_dir / file.filename
    target_path = output_dir / file.filename
    if source_path.exists():
        shutil.move(str(source_path), str(target_path))
        file.size_bytes = target_path.stat().st_size
    if is_job_cancel_requested(job_id):
        target_path.unlink(missing_ok=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise JobCancelled()
    shutil.rmtree(temp_dir, ignore_errors=True)
    return file


def process_url_with_retry(job_id: str, url: str, index: int, output_dir: Path):
    if is_job_cancel_requested(job_id):
        raise JobCancelled()
    item_start = time.monotonic()
    cached_file = process_cached_url(url, index, output_dir)
    if cached_file:
        if is_job_cancel_requested(job_id):
            (output_dir / cached_file.filename).unlink(missing_ok=True)
            raise JobCancelled()
        cached_file.elapsed_seconds = round(time.monotonic() - item_start, 3)
        return cached_file, None, False

    retry_counter = [0]
    for attempt in range(1, TASK_MAX_ATTEMPTS + 1):
        if is_job_cancel_requested(job_id):
            raise JobCancelled()
        attempt_start = time.monotonic()
        try:
            file = run_process_url_cancellable(job_id, url, index, output_dir, retry_counter)
            file.elapsed_seconds = round(time.monotonic() - item_start, 3)
            file.retry_count = retry_counter[0]
            return file, None, rate_limit_detected_since(attempt_start)
        except Exception as error:
            if isinstance(error, JobCancelled):
                raise
            was_rate_limited = looks_rate_limited_error(error) or rate_limit_detected_since(attempt_start)
            if attempt >= TASK_MAX_ATTEMPTS:
                return None, {
                    "url": url,
                    "message": str(error),
                    "elapsed_seconds": round(time.monotonic() - item_start, 3),
                    "retry_count": retry_counter[0],
                }, was_rate_limited
            retry_counter[0] += 1
            if is_job_cancel_requested(job_id):
                raise JobCancelled()
            if was_rate_limited:
                pause_job(
                    job_id,
                    f"检测到 YouTube 限流，自动冷却 {format_duration(IP_BLOCK_COOLDOWN_SECONDS)} 后重试。",
                    IP_BLOCK_COOLDOWN_SECONDS,
                )
            else:
                task_retry_sleep(job_id, attempt)


def process_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        if job.cancel_requested or job.status == "cancelled":
            job.status = "cancelled"
            job.message = "已停止，任务尚未开始。"
            job.completed_at = time.time()
            job.updated_at = time.time()
            cancelled_job = job
            write_job = True
        elif job.status != "queued":
            return
        else:
            urls = list(job.urls)
            output_dir = job.output_dir
            job.status = "running"
            job.total = len(urls)
            job.started_at = time.time()
            job.updated_at = time.time()
            started_job = job
            write_job = False
    if write_job:
        write_job_artifacts(cancelled_job)
        return
    persist_job_summary(started_job)

    output_dir.mkdir(parents=True, exist_ok=True)

    processed_since_pause = 0
    cancelled = False
    for index, url in enumerate(urls, start=1):
        if is_job_cancel_requested(job_id):
            cancelled = True
            break
        file = None
        error_record = None
        was_rate_limited = False
        try:
            file, error_record, was_rate_limited = process_url_with_retry(job_id, url, index, output_dir)
            if file:
                add_file(job_id, file)
            elif error_record:
                add_error_record(job_id, error_record)
        except JobCancelled:
            cancelled = True
            break
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

        if is_job_cancel_requested(job_id):
            cancelled = True
            break

        if index >= len(urls):
            continue

        if was_rate_limited:
            processed_since_pause = 0
            try:
                pause_job(
                    job_id,
                    f"检测到 YouTube 限流，自动冷却 {format_duration(IP_BLOCK_COOLDOWN_SECONDS)} 后继续。",
                    IP_BLOCK_COOLDOWN_SECONDS,
                )
            except JobCancelled:
                cancelled = True
                break
            continue

        used_network = not (file and file.from_cache)
        if used_network:
            processed_since_pause += 1

        if AUTO_BATCH_SIZE and processed_since_pause >= AUTO_BATCH_SIZE:
            processed_since_pause = 0
            try:
                pause_job(
                    job_id,
                    f"已处理 {AUTO_BATCH_SIZE} 条，自动休息 {format_duration(AUTO_BATCH_COOLDOWN_SECONDS)} 后继续。",
                    AUTO_BATCH_COOLDOWN_SECONDS,
                )
            except JobCancelled:
                cancelled = True
                break

    with jobs_lock:
        job = jobs[job_id]
        if cancelled or job.cancel_requested:
            job.status = "cancelled"
            job.cancel_requested = True
            job.message = "已停止，当前未完成视频已忽略。"
        else:
            job.status = "completed_with_errors" if job.errors else "completed"
            job.current = job.total
            job.message = ""
        job.completed_at = time.time()
        job.cooldown_until = None
        job.updated_at = time.time()
        final_job = job
    write_job_artifacts(final_job)


def translation_download_filename(job: TranslationJob) -> str:
    return f"{safe_download_name_part(job.output_folder_name) or 'subtitles_zh'}.zip"


def translated_output_dir(job: TranslationJob) -> Path:
    return job.output_dir / job.output_folder_name


def translation_job_queue_position(job_id: str) -> int:
    with translation_job_queue_condition:
        try:
            return translation_job_queue.index(job_id) + 1
        except ValueError:
            return 0


def translation_job_to_dict(job: TranslationJob) -> Dict[str, object]:
    elapsed_values = [file.elapsed_seconds for file in job.files]
    elapsed_values.extend(float(error.get("elapsed_seconds", 0)) for error in job.errors)
    completed_count = len(elapsed_values)
    wall_time = None
    if job.started_at:
        wall_time = round((job.completed_at or time.time()) - job.started_at, 3)
    return {
        "id": job.id,
        "original_filename": job.original_filename,
        "source_root_name": job.source_root_name,
        "output_folder_name": job.output_folder_name,
        "download_filename": translation_download_filename(job),
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "cancel_requested": job.cancel_requested,
        "queue_position": translation_job_queue_position(job.id),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "message": job.message,
        "files": [file.__dict__ for file in sorted(job.files, key=lambda item: item.filename)],
        "errors": job.errors,
        "retry_source_names": job.retry_source_names,
        "summary": {
            "completed_count": completed_count,
            "success_count": len(job.files),
            "error_count": len(job.errors),
            "average_elapsed_seconds": round(sum(elapsed_values) / completed_count, 3) if completed_count else 0,
            "total_item_elapsed_seconds": round(sum(elapsed_values), 3),
            "wall_elapsed_seconds": wall_time,
        },
    }


def persist_translation_summary(job: Optional[TranslationJob]) -> None:
    if not job:
        return
    job.output_dir.mkdir(parents=True, exist_ok=True)
    (job.output_dir / "translation_summary.json").write_text(
        json.dumps(translation_job_to_dict(job), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def translation_job_from_manifest(path: Path) -> Optional[TranslationJob]:
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
                "source_filename": "",
                "message": "服务重启后，未完成翻译已中断；已生成文件仍可下载。",
                "elapsed_seconds": 0,
            }
        )

    files = []
    for item in data.get("files") or []:
        try:
            files.append(
                TranslationFile(
                    filename=clean_scalar(item.get("filename")),
                    title=clean_scalar(item.get("title")),
                    source_filename=clean_scalar(item.get("source_filename")),
                    size_bytes=int(item.get("size_bytes") or 0),
                    elapsed_seconds=float(item.get("elapsed_seconds") or 0),
                    chunk_count=int(item.get("chunk_count") or 0),
                    retry_count=int(item.get("retry_count") or 0),
                )
            )
        except Exception:
            continue

    return TranslationJob(
        id=job_id,
        original_filename=clean_scalar(data.get("original_filename") or ""),
        source_root_name=clean_scalar(data.get("source_root_name") or ""),
        output_folder_name=clean_scalar(data.get("output_folder_name") or f"{job_id}_zh"),
        input_zip_path=path.parent / "input.zip",
        output_dir=path.parent,
        status=status,
        current=int(data.get("current") or len(files) + len(errors)),
        total=int(data.get("total") or len(files) + len(errors)),
        cancel_requested=bool(data.get("cancel_requested", False)) if status not in TERMINAL_JOB_STATUSES else False,
        created_at=float(data.get("created_at") or path.stat().st_mtime),
        updated_at=float(data.get("updated_at") or path.stat().st_mtime),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        message=clean_scalar(data.get("message") or ""),
        files=files,
        errors=errors,
        retry_source_names=[
            clean_scalar(item)
            for item in (data.get("retry_source_names") or [])
            if clean_scalar(item)
        ],
    )


def load_existing_translation_jobs() -> None:
    TRANSLATION_DIR.mkdir(parents=True, exist_ok=True)
    loaded = 0
    for summary_path in TRANSLATION_DIR.glob("*/translation_summary.json"):
        job = translation_job_from_manifest(summary_path)
        if not job:
            continue
        with translation_jobs_lock:
            translation_jobs[job.id] = job
        loaded += 1
    if loaded:
        print(f"Loaded {loaded} historical translation jobs", flush=True)


def translation_zip_parts(name: str) -> List[str]:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return []
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return []
    if parts[0] == "__MACOSX" or any(part == ".DS_Store" for part in parts):
        return []
    return parts


def is_translation_member(info: zipfile.ZipInfo) -> bool:
    if info.is_dir():
        return False
    parts = translation_zip_parts(info.filename)
    if not parts:
        return False
    suffix = Path(parts[-1]).suffix.lower()
    return suffix in TRANSLATION_SUPPORTED_SUFFIXES


def inspect_translation_zip(zip_path: Path, uploaded_filename: str) -> Tuple[List[str], str]:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [info.filename for info in archive.infolist() if is_translation_member(info)]
    except zipfile.BadZipFile:
        raise ValueError("上传文件不是有效的 ZIP 包。")

    if not members:
        supported = "、".join(sorted(TRANSLATION_SUPPORTED_SUFFIXES))
        raise ValueError(f"ZIP 包里没有可翻译的字幕文件（支持 {supported}）。")

    member_parts = [translation_zip_parts(name) for name in members]
    first_parts = [parts[0] for parts in member_parts if parts]
    if first_parts and len(set(first_parts)) == 1 and all(len(parts) > 1 for parts in member_parts):
        root_name = first_parts[0]
    else:
        root_name = Path(uploaded_filename).stem
    root_name = safe_download_name_part(root_name) or "subtitles"
    return members, root_name


def split_text_for_translation(text: str) -> List[str]:
    max_chars = max(1500, TRANSLATION_CHUNK_CHARS)
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    current = ""
    blocks = re.split(r"(\n{2,})", text)
    for index in range(0, len(blocks), 2):
        block = blocks[index]
        separator = blocks[index + 1] if index + 1 < len(blocks) else ""
        piece = block + separator
        if not piece:
            continue
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(piece), max_chars):
                chunks.append(piece[start : start + max_chars])
            continue
        if current and len(current) + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks or [text]


def clean_translation_response(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:markdown|md|text)?\s*\n(.*)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def translation_error_message(response: requests.Response) -> str:
    text = response.text.strip()
    if not text:
        return f"HTTP {response.status_code}"
    try:
        data = response.json()
        message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("error")
        if message:
            return f"HTTP {response.status_code}: {clean_scalar(message)}"
    except Exception:
        pass
    return f"HTTP {response.status_code}: {clean_scalar(text)[:300]}"


def call_translation_api(markdown_chunk: str) -> str:
    if not TRANSLATION_API_KEY:
        raise ValueError("请先在设置里填写翻译 API Key。")

    system_prompt = (
        "你是专业字幕翻译。把用户提供的英文 Markdown 字幕翻译成简体中文。"
        "必须保留 Markdown 结构、空行、列表、说话人标记、时间戳、URL 和元数据键名。"
        "article_id、source_url、publish_date 这类标识值不要翻译；title 的值需要翻译成中文。"
        "不要总结，不要添加解释，只返回翻译后的内容。"
    )
    payload = {
        "model": TRANSLATION_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": markdown_chunk},
        ],
    }
    headers = {
        "Authorization": f"Bearer {TRANSLATION_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error: Optional[Exception] = None
    for attempt in range(1, TRANSLATION_API_MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                TRANSLATION_API_URL,
                headers=headers,
                json=payload,
                timeout=TRANSLATION_API_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                raise RuntimeError(translation_error_message(response))
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("翻译接口没有返回有效内容。")
            return clean_translation_response(content)
        except Exception as error:
            last_error = error
            if attempt >= TRANSLATION_API_MAX_ATTEMPTS:
                break
            delay = min(30.0, TRANSLATION_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0, 0.6))
    raise RuntimeError(str(last_error) if last_error else "翻译接口调用失败。")


def extract_markdown_title(markdown: str) -> str:
    patterns = [
        r"^title\s*:\s*(.+)$",
        r"^标题\s*[：:]\s*(.+)$",
        r"^#\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, markdown, flags=re.MULTILINE | re.IGNORECASE)
        if match:
            title = clean_scalar(match.group(1).strip().strip('"').strip("'"))
            if title:
                return title
    return ""


def safe_translated_filename(title: str, fallback: str, suffix: str, existing: set) -> str:
    base = safe_download_name_part(title) or safe_download_name_part(Path(fallback).stem) or "subtitle"
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = f"{base}{suffix}"
    counter = 2
    while candidate in existing:
        candidate = f"{base}_{counter}{suffix}"
        counter += 1
    existing.add(candidate)
    return candidate


def decode_subtitle_text(data: bytes) -> str:
    for encoding in ["utf-8-sig", "utf-8", "cp1252"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def is_translation_cancel_requested(job_id: str) -> bool:
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        return bool(job and job.cancel_requested)


def update_translation_job(job_id: str, **changes) -> None:
    job = None
    with translation_jobs_lock:
        job = translation_jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = time.time()
    persist_translation_summary(job)


def add_translation_file(job_id: str, file: TranslationFile) -> None:
    job = None
    with translation_jobs_lock:
        job = translation_jobs[job_id]
        if job.status in TERMINAL_JOB_STATUSES or job.cancel_requested:
            return
        job.files.append(file)
        job.updated_at = time.time()
    persist_translation_summary(job)


def add_translation_error(job_id: str, error: Dict[str, object]) -> None:
    job = None
    with translation_jobs_lock:
        job = translation_jobs[job_id]
        if job.status in TERMINAL_JOB_STATUSES or job.cancel_requested:
            return
        job.errors.append(error)
        job.updated_at = time.time()
    persist_translation_summary(job)


def remove_queued_translation_job(job_id: str) -> bool:
    with translation_job_queue_condition:
        before = len(translation_job_queue)
        translation_job_queue[:] = [queued_id for queued_id in translation_job_queue if queued_id != job_id]
        return len(translation_job_queue) != before


def enqueue_translation_job(job_id: str) -> None:
    with translation_job_queue_condition:
        if job_id not in translation_job_queue:
            translation_job_queue.append(job_id)
        translation_job_queue_condition.notify()
    ensure_translation_job_worker()


def ensure_translation_job_worker() -> None:
    global translation_job_worker_thread
    with translation_job_queue_condition:
        if translation_job_worker_thread and translation_job_worker_thread.is_alive():
            return
        translation_job_worker_thread = threading.Thread(target=translation_job_worker_loop, daemon=True)
        translation_job_worker_thread.start()


def translation_job_worker_loop() -> None:
    while True:
        with translation_job_queue_condition:
            while not translation_job_queue:
                translation_job_queue_condition.wait()
            job_id = translation_job_queue.pop(0)

        with translation_jobs_lock:
            job = translation_jobs.get(job_id)
            should_run = bool(job and job.status == "queued" and not job.cancel_requested)
        if not should_run:
            continue

        try:
            process_translation_job(job_id)
        except Exception as error:
            traceback.print_exc()
            with translation_jobs_lock:
                job = translation_jobs.get(job_id)
                if not job:
                    continue
                job.status = "failed"
                job.message = str(error)
                job.completed_at = time.time()
                job.updated_at = time.time()
                failed_job = job
            persist_translation_summary(failed_job)


def request_translation_stop(job_id: str) -> TranslationJob:
    remove_from_queue = False
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        if not job:
            raise ValueError("Translation job not found.")
        if job.status in TERMINAL_JOB_STATUSES:
            return job
        job.cancel_requested = True
        if job.status == "queued":
            remove_from_queue = True
            job.message = "已停止，任务尚未开始。"
        else:
            job.message = "已停止，当前未完成文件已忽略。"
        job.status = "cancelled"
        job.completed_at = time.time()
        job.updated_at = time.time()
        stopped_job = job
    if remove_from_queue:
        remove_queued_translation_job(job_id)
    persist_translation_summary(stopped_job)
    return stopped_job


def request_translation_retry(job_id: str) -> TranslationJob:
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        if not job:
            raise ValueError("Translation job not found.")
        if job.status not in TERMINAL_JOB_STATUSES:
            raise ValueError("当前任务还未结束，不能重试。")
        if not job.input_zip_path.exists():
            raise ValueError("原始 ZIP 文件不存在，无法重试。")

        failed_sources = [
            clean_scalar(error.get("source_filename") or "")
            for error in job.errors
            if clean_scalar(error.get("source_filename") or "")
        ]
        if not failed_sources and job.status != "failed":
            raise ValueError("当前任务没有可重试的失败文件。")

        job.retry_source_names = failed_sources
        if not failed_sources:
            job.files = []
        job.errors = []
        job.status = "queued"
        job.cancel_requested = False
        job.current = len(job.files)
        job.total = len(job.files) + len(failed_sources) if failed_sources else 0
        job.started_at = None
        job.completed_at = None
        job.message = "等待重试"
        job.updated_at = time.time()
        retry_job = job
    persist_translation_summary(retry_job)
    enqueue_translation_job(job_id)
    return retry_job


def set_translation_message(job_id: str, message: str) -> None:
    job = None
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        if not job or job.status in TERMINAL_JOB_STATUSES:
            return
        job.message = message
        job.updated_at = time.time()
    persist_translation_summary(job)


def translate_member(
    job_id: str,
    archive: zipfile.ZipFile,
    member_name: str,
    output_dir: Path,
    existing_names: set,
) -> TranslationFile:
    started = time.monotonic()
    source_parts = translation_zip_parts(member_name)
    source_filename = "/".join(source_parts) or member_name
    source_basename = source_parts[-1] if source_parts else Path(member_name).name
    suffix = Path(source_basename).suffix or ".md"

    raw = archive.read(member_name)
    markdown = decode_subtitle_text(raw)
    chunks = split_text_for_translation(markdown)
    translated_chunks = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        if is_translation_cancel_requested(job_id):
            raise JobCancelled()
        set_translation_message(
            job_id,
            f"正在翻译 {source_basename}（{chunk_index}/{len(chunks)}）",
        )
        translated_chunks.append(call_translation_api(chunk))

    translated = "\n\n".join(part.strip() for part in translated_chunks if part.strip()).strip() + "\n"
    title = extract_markdown_title(translated) or extract_markdown_title(markdown) or Path(source_basename).stem
    filename = safe_translated_filename(title, source_basename, suffix, existing_names)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    path.write_text(translated, encoding="utf-8")
    return TranslationFile(
        filename=filename,
        title=title,
        source_filename=source_filename,
        size_bytes=path.stat().st_size,
        elapsed_seconds=round(time.monotonic() - started, 3),
        chunk_count=len(chunks),
    )


def translate_member_with_retry(
    job_id: str,
    archive: zipfile.ZipFile,
    member_name: str,
    output_dir: Path,
    existing_names: set,
) -> TranslationFile:
    last_error: Optional[Exception] = None
    source_basename = Path(member_name.replace("\\", "/")).name or member_name
    for attempt in range(1, TRANSLATION_FILE_MAX_ATTEMPTS + 1):
        if is_translation_cancel_requested(job_id):
            raise JobCancelled()
        try:
            if attempt > 1:
                set_translation_message(job_id, f"正在重试 {source_basename}（第 {attempt} 次）")
            file = translate_member(job_id, archive, member_name, output_dir, existing_names)
            file.retry_count = attempt - 1
            return file
        except JobCancelled:
            raise
        except Exception as error:
            last_error = error
            if attempt >= TRANSLATION_FILE_MAX_ATTEMPTS:
                break
            delay = min(30.0, TRANSLATION_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0, 0.6))
    raise RuntimeError(str(last_error) if last_error else "翻译文件失败。")


def process_translation_job(job_id: str) -> None:
    with translation_jobs_lock:
        job = translation_jobs.get(job_id)
        if not job:
            return
        if job.cancel_requested or job.status == "cancelled":
            job.status = "cancelled"
            job.message = "已停止，任务尚未开始。"
            job.completed_at = time.time()
            job.updated_at = time.time()
            cancelled_job = job
            write_cancelled = True
        elif job.status != "queued":
            return
        else:
            job.status = "running"
            job.started_at = time.time()
            job.updated_at = time.time()
            started_job = job
            write_cancelled = False
    if write_cancelled:
        persist_translation_summary(cancelled_job)
        return
    persist_translation_summary(started_job)

    cancelled = False
    try:
        with zipfile.ZipFile(started_job.input_zip_path) as archive:
            all_member_names, _root_name = inspect_translation_zip(started_job.input_zip_path, started_job.original_filename)
            output_dir = translated_output_dir(started_job)
            retry_source_names = list(started_job.retry_source_names)
            if retry_source_names:
                retry_set = set(retry_source_names)
                member_names = [
                    name
                    for name in all_member_names
                    if name in retry_set or "/".join(translation_zip_parts(name)) in retry_set
                ]
                if not member_names:
                    raise ValueError("没有找到可重试的失败文件。")
                existing_names: set = {file.filename for file in started_job.files}
            else:
                member_names = all_member_names
                existing_names = set()

            if output_dir.exists() and not retry_source_names:
                shutil.rmtree(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            with translation_jobs_lock:
                job = translation_jobs[job_id]
                if retry_source_names:
                    job.current = len(job.files)
                    job.total = len(job.files) + len(member_names)
                else:
                    job.current = 0
                    job.total = len(member_names)
                job.updated_at = time.time()
                progress_job = job
            persist_translation_summary(progress_job)

            for member_name in member_names:
                if is_translation_cancel_requested(job_id):
                    cancelled = True
                    break
                item_start = time.monotonic()
                try:
                    file = translate_member_with_retry(job_id, archive, member_name, output_dir, existing_names)
                    add_translation_file(job_id, file)
                except JobCancelled:
                    cancelled = True
                    break
                except Exception as error:
                    traceback.print_exc()
                    add_translation_error(
                        job_id,
                        {
                            "source_filename": member_name,
                            "message": str(error),
                            "elapsed_seconds": round(time.monotonic() - item_start, 3),
                            "retry_count": max(0, TRANSLATION_FILE_MAX_ATTEMPTS - 1),
                        },
                    )

                with translation_jobs_lock:
                    job = translation_jobs[job_id]
                    job.current += 1
                    job.updated_at = time.time()
                    progress_job = job
                persist_translation_summary(progress_job)
    except JobCancelled:
        cancelled = True
    except Exception as error:
        with translation_jobs_lock:
            job = translation_jobs[job_id]
            job.status = "failed"
            job.message = str(error)
            job.completed_at = time.time()
            job.updated_at = time.time()
            failed_job = job
        persist_translation_summary(failed_job)
        return

    with translation_jobs_lock:
        job = translation_jobs[job_id]
        if cancelled or job.cancel_requested:
            job.status = "cancelled"
            job.cancel_requested = True
            job.message = "已停止，当前未完成文件已忽略。"
        else:
            job.status = "completed_with_errors" if job.errors else "completed"
            job.current = job.total
            job.message = "翻译完成。"
        job.retry_source_names = []
        job.completed_at = time.time()
        job.updated_at = time.time()
        final_job = job
    persist_translation_summary(final_job)


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
        if path == "/api/translation-jobs":
            return self.handle_translation_jobs_list()
        if path == "/api/settings":
            return self.handle_settings_get()
        if path == "/api/channels":
            return self.handle_channels_list()
        if path.startswith("/api/channels/"):
            return self.handle_channel_get(path)
        if path.startswith("/api/discovery-tasks/"):
            return self.handle_discovery_task_get(path)
        if path == "/api/discoveries":
            return self.handle_discoveries_list()
        if path.startswith("/api/discoveries/"):
            return self.handle_discovery_get(path)
        if path.startswith("/api/jobs/"):
            return self.handle_job_get(path)
        if path.startswith("/api/translation-jobs/"):
            return self.handle_translation_job_get(path)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        self.head_only = False
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            return self.handle_create_job()
        if parsed.path == "/api/translation-jobs":
            return self.handle_create_translation_job()
        if parsed.path.startswith("/api/jobs/"):
            return self.handle_job_post(parsed.path)
        if parsed.path.startswith("/api/translation-jobs/"):
            return self.handle_translation_job_post(parsed.path)
        if parsed.path == "/api/settings":
            return self.handle_settings_post()
        if parsed.path == "/api/channels":
            return self.handle_add_channels()
        if parsed.path.startswith("/api/channels/"):
            return self.handle_channel_post(parsed.path)
        if parsed.path == "/api/discovery-tasks":
            return self.handle_create_discovery_task()
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

    def read_multipart_uploads(self) -> List[Tuple[str, bytes]]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("请使用表单上传 ZIP 文件。")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("上传内容为空。")
        if length > MAX_UPLOAD_BYTES:
            raise ValueError("上传文件太大。")
        body = self.rfile.read(length)
        raw_message = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8") + body
        message = BytesParser(policy=email_policy).parsebytes(raw_message)
        if not message.is_multipart():
            raise ValueError("上传表单格式不正确。")
        uploads: List[Tuple[str, bytes]] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            filename = clean_scalar(part.get_filename() or "")
            if not filename:
                continue
            data = part.get_payload(decode=True) or b""
            if not data:
                raise ValueError("上传文件为空。")
            uploads.append((filename, data))
        if not uploads:
            raise ValueError("没有找到上传的 ZIP 文件。")
        return uploads

    def read_multipart_upload(self) -> Tuple[str, bytes]:
        return self.read_multipart_uploads()[0]

    def handle_create_job(self):
        try:
            payload = self.read_json_body()
            raw_urls = payload.get("urls", "")
            archive_title = safe_download_name_part(clean_scalar(payload.get("archive_title") or ""))
            if isinstance(raw_urls, list):
                raw_urls = "\n".join(str(item) for item in raw_urls)
            urls = parse_urls(str(raw_urls))
        except ValueError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if "too large" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, urls=urls, output_dir=OUTPUT_DIR / job_id, archive_title=archive_title, total=len(urls))
        with jobs_lock:
            jobs[job_id] = job
        persist_job_summary(job)
        enqueue_job(job_id)
        return self.send_json(job_to_dict(job), HTTPStatus.CREATED)

    def handle_create_translation_job(self):
        try:
            if not TRANSLATION_API_KEY:
                raise ValueError("请先在设置里填写翻译 API Key。")
            uploads = self.read_multipart_uploads()
            created_jobs = []
            upload_errors = []
            for filename, data in uploads:
                try:
                    if Path(filename).suffix.lower() != ".zip":
                        raise ValueError("请上传 ZIP 文件。")
                    job_id = uuid.uuid4().hex[:12]
                    output_dir = TRANSLATION_DIR / job_id
                    output_dir.mkdir(parents=True, exist_ok=True)
                    input_zip_path = output_dir / "input.zip"
                    input_zip_path.write_bytes(data)
                    members, root_name = inspect_translation_zip(input_zip_path, filename)
                    output_folder_name = f"{root_name}_zh"
                    job = TranslationJob(
                        id=job_id,
                        original_filename=safe_download_name_part(filename) or "subtitles.zip",
                        source_root_name=root_name,
                        output_folder_name=output_folder_name,
                        input_zip_path=input_zip_path,
                        output_dir=output_dir,
                        total=len(members),
                        message="等待翻译",
                    )
                    created_jobs.append(job)
                except Exception as item_error:
                    upload_errors.append({"filename": filename, "message": str(item_error)})
            if not created_jobs:
                message = upload_errors[0]["message"] if upload_errors else "没有可用的 ZIP 文件。"
                raise ValueError(message)
        except ValueError as error:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if "太大" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        for job in created_jobs:
            with translation_jobs_lock:
                translation_jobs[job.id] = job
            persist_translation_summary(job)
            enqueue_translation_job(job.id)

        jobs_payload = [translation_job_to_dict(job) for job in created_jobs]
        payload = dict(jobs_payload[0])
        payload["jobs"] = jobs_payload
        payload["upload_errors"] = upload_errors
        return self.send_json(payload, HTTPStatus.CREATED)

    def handle_job_post(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] in {"stop", "cancel"}:
            try:
                job = request_job_stop(parts[2])
                return self.send_json(job_to_dict(job))
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def handle_translation_job_post(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] in {"stop", "cancel"}:
            try:
                job = request_translation_stop(parts[2])
                return self.send_json(translation_job_to_dict(job))
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
            except Exception as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if len(parts) == 4 and parts[3] == "retry":
            try:
                job = request_translation_retry(parts[2])
                return self.send_json(translation_job_to_dict(job))
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_error(HTTPStatus.NOT_FOUND)

    def handle_settings_get(self):
        return self.send_json(settings_payload())

    def handle_settings_post(self):
        try:
            payload = self.read_json_body()
            result = update_settings_from_payload(payload)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_json(result)

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

    def handle_create_discovery_task(self):
        try:
            payload = self.read_json_body()
            channel_id = clean_scalar(payload.get("channel_id") or "")
            if channel_id:
                channel = get_followed_channel(channel_id)
                source = clean_scalar(channel.get("source_url") or channel.get("listing_url"))
                if not source:
                    raise ValueError("Channel has no source URL.")
                sources = [source]
                kind = "channel_refresh"
            else:
                raw_sources = payload.get("sources", "")
                if isinstance(raw_sources, list):
                    raw_sources = "\n".join(str(item) for item in raw_sources)
                sources = parse_discovery_sources(str(raw_sources))
                kind = "discover"
            range_text = clean_scalar(payload.get("range") or "1年")
            max_per_source = int_or_default(payload.get("max_per_source"), DISCOVERY_DEFAULT_MAX_PER_SOURCE)
            detail_lookup_limit = int_or_default(
                payload.get("detail_lookup_limit"),
                DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT,
            )
            result = create_discovery_task(
                sources,
                range_text,
                max_per_source,
                detail_lookup_limit,
                kind=kind,
                channel_id=channel_id,
            )
        except ValueError as error:
            status = HTTPStatus.NOT_FOUND if "Channel" in str(error) else HTTPStatus.BAD_REQUEST
            return self.send_json({"error": str(error)}, status)
        except Exception as error:
            return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        return self.send_json(result, HTTPStatus.CREATED)

    def handle_discovery_task_get(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) != 3:
            return self.send_error(HTTPStatus.NOT_FOUND)
        task_id = parts[2]
        with discovery_tasks_lock:
            task = discovery_tasks.get(task_id)
            payload = discovery_task_to_dict(task) if task else None
        if not payload:
            return self.send_json({"error": "Discovery task not found."}, HTTPStatus.NOT_FOUND)
        return self.send_json(payload)

    def handle_channel_post(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[3] == "refresh":
            try:
                payload = self.read_json_body()
                range_text = clean_scalar(payload.get("range") or "1年")
                max_per_source = int_or_default(payload.get("max_per_source"), DISCOVERY_DEFAULT_MAX_PER_SOURCE)
                detail_lookup_limit = int_or_default(
                    payload.get("detail_lookup_limit"),
                    DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT,
                )
                return self.send_json(refresh_followed_channel(parts[2], range_text, max_per_source, detail_lookup_limit))
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
            max_per_source = int_or_default(payload.get("max_per_source"), DISCOVERY_DEFAULT_MAX_PER_SOURCE)
            detail_lookup_limit = int_or_default(
                payload.get("detail_lookup_limit"),
                DISCOVERY_DEFAULT_DETAIL_LOOKUP_LIMIT,
            )
            result = create_discovery_record(sources, range_text, max_per_source, detail_lookup_limit)
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
                        "archive_title": job.archive_title,
                        "download_filename": zip_download_filename(job),
                        "status": job.status,
                        "current": job.current,
                        "total": job.total,
                        "cancel_requested": job.cancel_requested,
                        "queue_position": job_queue_position(job.id),
                        "updated_at": job.updated_at,
                        "summary": job_to_dict(job)["summary"],
                    }
                    for job in job_list[:50]
                ]
            }
        return self.send_json(payload)

    def handle_translation_jobs_list(self):
        with translation_jobs_lock:
            job_list = sorted(translation_jobs.values(), key=lambda item: item.updated_at, reverse=True)
            payload = {
                "jobs": [
                    {
                        "id": job.id,
                        "original_filename": job.original_filename,
                        "source_root_name": job.source_root_name,
                        "output_folder_name": job.output_folder_name,
                        "download_filename": translation_download_filename(job),
                        "status": job.status,
                        "current": job.current,
                        "total": job.total,
                        "cancel_requested": job.cancel_requested,
                        "queue_position": translation_job_queue_position(job.id),
                        "updated_at": job.updated_at,
                        "summary": translation_job_to_dict(job)["summary"],
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

    def handle_translation_job_get(self, path: str):
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) < 3:
            return self.send_error(HTTPStatus.NOT_FOUND)
        job_id = parts[2]
        with translation_jobs_lock:
            job = translation_jobs.get(job_id)
        if not job:
            return self.send_json({"error": "Translation job not found."}, HTTPStatus.NOT_FOUND)

        if len(parts) == 3:
            with translation_jobs_lock:
                payload = translation_job_to_dict(translation_jobs[job_id])
            return self.send_json(payload)

        if len(parts) == 4 and parts[3] == "download":
            return self.serve_translation_zip(job)

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
        self.send_header("Content-Disposition", attachment_content_disposition(zip_download_filename(job)))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not self.head_only:
            self.wfile.write(data)

    def serve_translation_zip(self, job: TranslationJob):
        source_dir = translated_output_dir(job)
        zip_path = job.output_dir / f"{job.id}_zh.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            if source_dir.exists():
                for path in sorted(source_dir.glob("*")):
                    if path.is_file():
                        archive.write(path, arcname=f"{job.output_folder_name}/{path.name}")
            summary_path = job.output_dir / "translation_summary.json"
            if summary_path.exists():
                archive.write(summary_path, arcname=f"{job.output_folder_name}/translation_summary.json")
        data = zip_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", attachment_content_disposition(translation_download_filename(job)))
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
    load_existing_translation_jobs()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Subtitle Markdown Tool running at http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
