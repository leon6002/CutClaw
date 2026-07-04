"""JSON-based annotation persistence, content-addressed by file hash.

Thread-safe for the typical Streamlit single-session usage pattern.
Atomic writes prevent corruption on crash.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from .models import AssetAnnotation, AssetMetadataUnion

# Default location relative to PROJECT_ROOT (set at module load or overridden).
DEFAULT_INDEX_DIR = os.path.join("Output", "asset_index")
DEFAULT_INDEX_FILE = "annotations.json"

_lock = threading.Lock()


def _index_path(index_dir: Optional[str] = None) -> str:
    d = index_dir or DEFAULT_INDEX_DIR
    return os.path.join(d, DEFAULT_INDEX_FILE)


def load_index(index_dir: Optional[str] = None) -> dict[str, AssetAnnotation]:
    """Load the full annotation index.

    Returns:
        dict keyed by content_hash — may be empty if the index file does not
        exist or is corrupted.
    """
    path = _index_path(index_dir)
    if not os.path.exists(path):
        return {}

    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    if not isinstance(raw, dict):
        return {}

    index: dict[str, AssetAnnotation] = {}
    for content_hash, data in raw.items():
        try:
            index[content_hash] = AssetAnnotation(**data)
        except Exception:
            # Skip corrupted entries silently — they'll be re-annotated.
            continue
    return index


def save_index(index: dict[str, AssetAnnotation], index_dir: Optional[str] = None):
    """Atomically write the index to disk."""
    path = _index_path(index_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    payload: dict[str, dict] = {}
    for content_hash, annotation in index.items():
        payload[content_hash] = annotation.model_dump(mode="json")

    with _lock:
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix="asset_index_", dir=os.path.dirname(path)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise


def get_annotation(content_hash: str, index_dir: Optional[str] = None) -> Optional[AssetAnnotation]:
    """Look up a single annotation by content hash."""
    return load_index(index_dir).get(content_hash)


def upsert_annotations(
    annotations: list[AssetAnnotation],
    index_dir: Optional[str] = None,
):
    """Insert or update annotations in the index."""
    index = load_index(index_dir)
    for ann in annotations:
        if ann.content_hash:
            index[ann.content_hash] = ann
    save_index(index, index_dir)


def find_new_assets(
    scanned: list[AssetMetadataUnion],
    index_dir: Optional[str] = None,
) -> list[AssetMetadataUnion]:
    """Return only assets whose *content_hash* is not yet in the index."""
    index = load_index(index_dir)
    return [a for a in scanned if a.content_hash and a.content_hash not in index]


def get_all_summaries(index_dir: Optional[str] = None) -> str:
    """Build a compact one-line-per-asset text summary for the selector Agent.

    Format (one line per asset):
      [video] | rel/path.mp4 | Q:8.5 | golden hour drone over grasslands | tags: aerial, landscape
    """
    index = load_index(index_dir)
    if not index:
        return "(No annotated assets available)"

    lines: list[str] = []
    for content_hash, ann in sorted(index.items(), key=lambda kv: kv[1].file_path):
        a = ann.annotation
        tags_str = ", ".join(getattr(a, "tags", [])[:5])
        score = getattr(a, "quality_score", 5.0)
        summary = getattr(a, "summary", "") or ""
        # Truncate summary to keep each line compact
        if len(summary) > 80:
            summary = summary[:77] + "..."

        lines.append(
            f"[{ann.asset_type}] | {ann.file_path} | Q:{score:.1f} | {summary}"
            + (f" | tags: {tags_str}" if tags_str else "")
        )

    return "\n".join(lines)


def get_summaries_by_type(
    asset_type: str, index_dir: Optional[str] = None
) -> str:
    """Get summaries filtered to one asset type."""
    index = load_index(index_dir)
    filtered = {
        h: a for h, a in index.items() if a.asset_type == asset_type
    }
    if not filtered:
        return f"(No {asset_type} assets)"

    lines: list[str] = []
    for content_hash, ann in sorted(filtered.items(), key=lambda kv: kv[1].file_path):
        a = ann.annotation
        tags_str = ", ".join(getattr(a, "tags", [])[:5])
        score = getattr(a, "quality_score", 5.0)
        summary = getattr(a, "summary", "") or ""
        if len(summary) > 80:
            summary = summary[:77] + "..."
        lines.append(
            f"[{ann.asset_type}] | {ann.file_path} | Q:{score:.1f} | {summary}"
            + (f" | tags: {tags_str}" if tags_str else "")
        )
    return "\n".join(lines)


def count_by_type(index_dir: Optional[str] = None) -> dict[str, int]:
    """Return counts of annotated assets by type."""
    index = load_index(index_dir)
    counts: dict[str, int] = {"video": 0, "image": 0, "audio": 0}
    for ann in index.values():
        t = ann.asset_type
        if t in counts:
            counts[t] += 1
    return counts


def purge_orphaned_annotations(
    valid_hashes: set[str],
    index_dir: Optional[str] = None,
):
    """Remove annotations for files that no longer exist on disk."""
    index = load_index(index_dir)
    removed = 0
    for h in list(index):
        if h not in valid_hashes:
            del index[h]
            removed += 1
    if removed > 0:
        save_index(index, index_dir)
