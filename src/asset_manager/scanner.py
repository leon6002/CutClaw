"""Asset directory scanner — discover media files and extract metadata."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import threading
from typing import Callable, Optional

from .models import (
    AssetMetadata,
    AudioAssetMetadata,
    ImageAssetMetadata,
    VideoAssetMetadata,
)

# ── Extension sets (aligned with app.py) ───────────────────────────────────

VIDEO_EXTS: set[str] = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
IMAGE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".heif"}
AUDIO_EXTS: set[str] = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"}

# Known MIME type overrides (mimetypes isn't always accurate on Windows)
_EXT_MIME_MAP: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".wma": "audio/x-ms-wma",
}


def _ensure_ffprobe_on_path() -> None:
    """Make ffprobe discoverable on Windows (same logic as local_run.py)."""
    project_ffmpeg = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "ffmpeg"
    )
    candidates = [
        project_ffmpeg,
        os.path.join(sys.prefix, "Library", "bin"),
        os.path.join(sys.prefix, "Library", "mingw-w64", "bin"),
        os.path.join(sys.prefix, "Library", "usr", "bin"),
        os.path.join(sys.prefix, "Scripts"),
        sys.prefix,
    ]
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep)
    prepend = [p for p in candidates if os.path.isdir(p) and p not in parts]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + parts)


# ── Content hashing ────────────────────────────────────────────────────────

def compute_content_hash(file_path: str, chunk_size: int = 1_048_576) -> str:
    """SHA-256 of the first *chunk_size* bytes of a file.

    Using the first 1 MB is fast enough for large media libraries while
    being sufficient for content deduplication (media files are immutable).
    """
    sha = hashlib.sha256()
    try:
        with open(file_path, "rb") as fh:
            data = fh.read(chunk_size)
            sha.update(data)
    except OSError:
        return ""
    return sha.hexdigest()


# ── Metadata extraction ────────────────────────────────────────────────────

def _probe_via_ffprobe(file_path: str, entries: str = "format=duration:stream=width,height,r_frame_rate,codec_name,codec_type,sample_rate,channels") -> dict[str, str]:
    """Run ffprobe and return key=value pairs from the default output."""
    _ensure_ffprobe_on_path()
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-show_entries", entries,
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        # Keys come from -show_entries order — we parse positionally below.
        return {"raw": "|".join(lines)}
    except Exception:
        return {}


def _probe_structured(file_path: str) -> dict:
    """Run individual ffprobe calls for reliable metadata extraction."""
    _ensure_ffprobe_on_path()
    result: dict = {}

    def _run(entry: str, select_stream: str = "v:0") -> str:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet",
                 "-select_streams", select_stream,
                 "-show_entries", f"stream={entry}",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 file_path],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    def _run_format(entry: str) -> str:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "quiet",
                 "-show_entries", f"format={entry}",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 file_path],
                capture_output=True, text=True, timeout=15,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    # Video stream: individual calls (avoids CSV ordering issues)
    w = _run("width")
    h = _run("height")
    if w:
        try:
            result["width"] = int(w)
        except ValueError:
            pass
    if h:
        try:
            result["height"] = int(h)
        except ValueError:
            pass

    fps_raw = _run("r_frame_rate")
    if fps_raw and "/" in fps_raw:
        try:
            num, den = fps_raw.split("/")
            result["fps"] = float(num) / float(den) if float(den) != 0 else 0.0
        except (ValueError, ZeroDivisionError):
            pass
    elif fps_raw:
        try:
            result["fps"] = float(fps_raw)
        except ValueError:
            pass

    codec = _run("codec_name")
    if codec:
        result["codec"] = codec

    # Format duration
    dur = _run_format("duration")
    if dur:
        try:
            result["duration"] = float(dur)
        except ValueError:
            pass

    # Audio stream check
    a_codec = _run("codec_type", select_stream="a:0")
    if a_codec and "audio" in a_codec.lower():
        result["has_audio"] = True
        sr = _run("sample_rate", select_stream="a:0")
        if sr:
            try:
                result["sample_rate"] = int(sr)
            except ValueError:
                pass
        ch = _run("channels", select_stream="a:0")
        if ch:
            try:
                result["channels"] = int(ch)
            except ValueError:
                pass

    return result


def probe_video_metadata(file_path: str, file_size_bytes: int) -> VideoAssetMetadata:
    """Extract video metadata via ffprobe. Graceful fallback for missing fields."""
    info = _probe_structured(file_path)
    width = info.get("width", 0) or 0
    height = info.get("height", 0) or 0
    duration = info.get("duration", 0) or 0
    codec = str(info.get("codec", ""))
    has_audio = bool(info.get("has_audio", False))
    fps = info.get("fps", 0.0) or 0.0

    return VideoAssetMetadata(
        file_path="",
        absolute_path=os.path.abspath(file_path),
        file_name=os.path.basename(file_path),
        content_hash="",
        file_size_bytes=file_size_bytes,
        file_size_mb=round(file_size_bytes / (1024 * 1024), 2),
        mime_type=_EXT_MIME_MAP.get(os.path.splitext(file_path)[1].lower(), "video/mp4"),
        duration_sec=round(float(duration), 2),
        width=int(width),
        height=int(height),
        fps=round(float(fps), 2),
        codec=codec,
        has_audio=has_audio,
    )


def probe_image_metadata(file_path: str, file_size_bytes: int) -> ImageAssetMetadata:
    """Extract image metadata via PIL."""
    width, height, fmt = 0, 0, ""
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            width, height = img.size
            fmt = img.format or ""
    except Exception:
        pass

    return ImageAssetMetadata(
        file_path="",
        absolute_path=os.path.abspath(file_path),
        file_name=os.path.basename(file_path),
        content_hash="",
        file_size_bytes=file_size_bytes,
        file_size_mb=round(file_size_bytes / (1024 * 1024), 2),
        mime_type=_EXT_MIME_MAP.get(os.path.splitext(file_path)[1].lower(), "image/jpeg"),
        width=width,
        height=height,
        format=fmt,
    )


def probe_audio_metadata(file_path: str, file_size_bytes: int) -> AudioAssetMetadata:
    """Extract audio metadata via ffprobe (or soundfile as fallback)."""
    duration, sr, channels = 0.0, 0, 0
    try:
        import soundfile as sf
        info = sf.info(file_path)
        duration = float(info.duration)
        sr = int(info.samplerate)
        channels = int(info.channels)
    except Exception:
        # Fallback to ffprobe
        info = _probe_structured(file_path)
        duration = info.get("duration", 0) or 0
        sr = info.get("sample_rate", 0) or 0
        channels = info.get("channels", 0) or 0

    return AudioAssetMetadata(
        file_path="",
        absolute_path=os.path.abspath(file_path),
        file_name=os.path.basename(file_path),
        content_hash="",
        file_size_bytes=file_size_bytes,
        file_size_mb=round(file_size_bytes / (1024 * 1024), 2),
        mime_type=_EXT_MIME_MAP.get(os.path.splitext(file_path)[1].lower(), "audio/mpeg"),
        duration_sec=round(duration, 2),
        sample_rate=sr,
        channels=channels,
    )


# ── Metadata cache (path → probed metadata, keyed by size+mtime) ────────────
#
# Probing every file on every scan is the slow part: SHA-256 of 1 MB plus
# ~8 ffprobe subprocess spawns per video (≈0.5-1s each on Windows). Files are
# immutable in practice, so we cache the fully-probed metadata keyed by
# (size, mtime) and skip all of it when a file is unchanged. First scan is
# still full cost; every scan after is near-instant for unchanged files.

_CACHE_VERSION = 1
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCAN_CACHE_PATH = os.path.join(_PROJECT_ROOT, "Output", "asset_index", "scan_cache.json")
_cache_lock = threading.Lock()

_META_CLASSES = {
    "video": VideoAssetMetadata,
    "image": ImageAssetMetadata,
    "audio": AudioAssetMetadata,
}


def _load_scan_cache() -> dict:
    try:
        with open(_SCAN_CACHE_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _CACHE_VERSION:
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_scan_cache(entries: dict) -> None:
    with _cache_lock:
        os.makedirs(os.path.dirname(_SCAN_CACHE_PATH), exist_ok=True)
        tmp = _SCAN_CACHE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": _CACHE_VERSION, "entries": entries},
                          fh, ensure_ascii=False)
            os.replace(tmp, _SCAN_CACHE_PATH)
        except OSError:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _dump_meta(meta) -> dict:
    """Serialize a typed metadata object to a JSON-safe dict."""
    if hasattr(meta, "model_dump"):
        return meta.model_dump(mode="json")
    return meta.dict()


def _meta_from_cache(entry: dict):
    """Rebuild a typed metadata object from a cached dict, or None if invalid."""
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        return None
    cls = _META_CLASSES.get(meta.get("asset_type"))
    if cls is None:
        return None
    try:
        return cls(**meta)
    except Exception:
        return None


# ── Main scan function ─────────────────────────────────────────────────────

def _classify_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def scan_asset_directory(
    asset_root: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    use_cache: bool = True,
) -> list[AssetMetadataUnion]:
    """Walk *asset_root*, discover media files, compute hashes and metadata.

    Args:
        asset_root: Absolute path to scan.
        progress_callback: Optional (current, total, file_name) callback for UI.
        use_cache: Reuse cached metadata for files whose (size, mtime) are
            unchanged, skipping hashing + ffprobe. Pass ``False`` to force a
            full re-probe of every file.

    Returns:
        List of typed metadata objects, each with *file_path* set to the
        relative path from *asset_root*.
    """
    if not os.path.isdir(asset_root):
        return []

    cache = _load_scan_cache() if use_cache else {}
    fresh_cache: dict = {}
    cache_dirty = False

    # Collect all relevant files
    file_paths: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(asset_root):
        for fn in filenames:
            # Skip derived wav copies produced by older audio-analysis runs
            # (they are conversion caches, not source assets).
            if "__vca" in fn:
                continue
            ext = os.path.splitext(fn)[1].lower()
            if _classify_ext(ext) is not None:
                file_paths.append(os.path.join(dirpath, fn))

    if not file_paths:
        return []

    total = len(file_paths)
    results: list[AssetMetadataUnion] = []

    for i, abs_path in enumerate(file_paths):
        fn = os.path.basename(abs_path)
        if progress_callback:
            progress_callback(i + 1, total, fn)

        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        file_size = st.st_size
        mtime = int(st.st_mtime)

        ext = os.path.splitext(abs_path)[1].lower()
        kind = _classify_ext(ext)
        if kind is None:
            continue
        rel_path = os.path.relpath(abs_path, asset_root)

        # Cache hit: file unchanged since last scan → skip hash + ffprobe.
        cached = cache.get(abs_path)
        if (cached and cached.get("size") == file_size
                and cached.get("mtime") == mtime):
            meta = _meta_from_cache(cached)
            if meta is not None:
                meta.file_path = rel_path            # root may have moved
                results.append(meta)
                fresh_cache[abs_path] = cached
                continue

        # Cache miss / changed file → full probe.
        content_hash = compute_content_hash(abs_path)
        if kind == "video":
            meta = probe_video_metadata(abs_path, file_size)
        elif kind == "image":
            meta = probe_image_metadata(abs_path, file_size)
        else:  # audio
            meta = probe_audio_metadata(abs_path, file_size)

        meta.file_path = rel_path
        meta.content_hash = content_hash
        results.append(meta)
        fresh_cache[abs_path] = {
            "size": file_size, "mtime": mtime, "meta": _dump_meta(meta),
        }
        cache_dirty = True

    # Persist if anything changed or stale entries were dropped (files removed).
    if use_cache and (cache_dirty or len(fresh_cache) != len(cache)):
        _save_scan_cache(fresh_cache)

    return results
