"""Config helpers, path derivation, and formatting utilities."""

import hashlib
import os
import re

import streamlit as st

from src.utils.env_keys import env_expr_name, resolve_env_expr, write_env_var
from src.utils.ui_state import UI_STATE_KEYS, read_state, write_state

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "src", "config.py")


# ── Config read/write ──────────────────────────────────────────────────────

def _read_config() -> dict:
    """Read key=value pairs from config.py as strings."""
    vals = {}
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    last_error = None
    for enc in encodings:
        try:
            with open(CONFIG_PATH, "r", encoding=enc) as f:
                for line in f:
                    m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)', line)
                    if m:
                        vals[m.group(1)] = m.group(2).strip()
            return vals
        except UnicodeDecodeError as exc:
            last_error = exc
            vals.clear()
            continue
    if last_error:
        raise last_error
    return vals


def cfg(key: str, fallback: str = "") -> str:
    """Get a config value as a plain string (strips quotes)."""
    # UI-remembered inputs live in Output/ui_state.json, not the tracked config.py.
    if key in UI_STATE_KEYS:
        v = read_state(key)
        if v is not None:
            return v
    raw = _read_config().get(key, fallback)
    resolved = resolve_env_expr(raw)
    if resolved is not None:
        return resolved
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def save_config(key: str, value: str):
    """Overwrite a single key in config.py (secrets → .env, UI inputs → ui_state.json)."""
    # UI-remembered input → gitignored state file, never back into config.py.
    if key in UI_STATE_KEYS:
        write_state(key, value)
        return
    # Env-backed secret (KEY = os.getenv(...)): write the value into .env so
    # the literal key never lands in the git-tracked config.py.
    env_name = env_expr_name(_read_config().get(key, ""))
    if env_name:
        if value != os.getenv(env_name, ""):
            write_env_var(env_name, value)
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        float(value)
        new_val = value
    except ValueError:
        new_val = f'"{value}"'
    pattern = rf'^({re.escape(key)}\s*=\s*).*'
    content = re.sub(pattern, lambda m: f"{m.group(1)}{new_val}", content, flags=re.MULTILINE)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


# ── Path derivation ────────────────────────────────────────────────────────

def derive_shot_point_path(video_path: str, audio_path: str, instruction: str) -> str:
    import src.config as config
    video_id = os.path.splitext(os.path.basename(video_path))[0].replace('.', '_').replace(' ', '_')
    audio_id = os.path.splitext(os.path.basename(audio_path))[0].replace('.', '_').replace(' ', '_')
    instruction_hash = hashlib.md5(instruction.encode('utf-8')).hexdigest()[:8]
    instruction_safe = re.sub(r'[^\w\s-]', '', instruction)[:50].strip().replace(' ', '_')
    instruction_id = f"{instruction_safe}_{instruction_hash}" if instruction_safe else f"instruction_{instruction_hash}"
    return os.path.join(config.VIDEO_DATABASE_FOLDER, 'Output', f"{video_id}_{audio_id}", f"shot_point_{instruction_id}.json")


def derive_shot_plan_path(video_path: str, audio_path: str, instruction: str) -> str:
    return derive_shot_point_path(video_path, audio_path, instruction).replace("shot_point_", "shot_plan_")


def _resolve_path(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def resolve_hook_subtitle_path(video_path: str, srt_path: str) -> str:
    import src.config as config
    video_id = os.path.splitext(os.path.basename(video_path))[0].replace('.', '_').replace(' ', '_')
    video_dir = os.path.join(config.VIDEO_DATABASE_FOLDER, 'Video', video_id)
    candidates = [
        os.path.join(video_dir, "subtitles_with_characters.srt"),
        os.path.join(video_dir, "subtitles.srt"),
        _resolve_path(srt_path.strip()) if srt_path.strip() else "",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


# ── Log formatting ──────────────────────────────────────────────────────────

STAGE_KEYWORDS = ["[Thread A]", "[Thread B]", "[Thread C]", "Shot detection", "ASR", "Captioning", "Scene", "Audio", "Screenwriter", "Editor", "Processing"]


def format_log_line(line: str) -> str:
    escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lower = line.lower()
    if any(k.lower() in lower for k in STAGE_KEYWORDS):
        return f'<span class="vca-stage">{escaped}</span>'
    if "error" in lower or "traceback" in lower or "exception" in lower:
        return f'<span class="vca-error">{escaped}</span>'
    if "complete" in lower or "done" in lower or "finished" in lower or "success" in lower:
        return f'<span class="vca-success">{escaped}</span>'
    return escaped


# ── Status badge ────────────────────────────────────────────────────────────

def status_badge() -> str:
    if st.session_state.pipeline_failed:
        return '<span class="vca-badge vca-badge-error">Failed</span>'
    if st.session_state.running:
        return '<span class="vca-badge vca-badge-running">Running…</span>'
    if st.session_state.log_lines:
        return '<span class="vca-badge vca-badge-done">Done</span>'
    return '<span class="vca-badge vca-badge-idle">Idle</span>'


# ── Duration bounds ─────────────────────────────────────────────────────────

MIN_TARGET_SHOT_LENGTH_SEC = 0.2
MIN_SEGMENT_DURATION_FLOOR_SEC = 0.1
SHOT_LENGTH_RANGE_CAP_SEC = 1.0


def _persist_target_output_length(target_length: float):
    min_duration = max(5.0, target_length - 5.0)
    max_duration = target_length + 5.0
    save_config("AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_duration))
    save_config("AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_duration))


def _derive_shot_duration_bounds(shot_length: float) -> tuple[float, float]:
    shot_length = max(MIN_TARGET_SHOT_LENGTH_SEC, float(shot_length))
    range_radius = min(SHOT_LENGTH_RANGE_CAP_SEC, max(MIN_SEGMENT_DURATION_FLOOR_SEC, shot_length))
    min_seg_duration = max(MIN_SEGMENT_DURATION_FLOOR_SEC, shot_length - range_radius)
    max_seg_duration = shot_length + range_radius
    return round(min_seg_duration, 3), round(max_seg_duration, 3)


def _derive_target_shot_length_from_config() -> float:
    min_seg_duration = float(cfg("AUDIO_MIN_SEGMENT_DURATION", "3.0"))
    max_seg_duration = float(cfg("AUDIO_MAX_SEGMENT_DURATION", "5.0"))
    return round(max(MIN_TARGET_SHOT_LENGTH_SEC, (min_seg_duration + max_seg_duration) / 2.0), 3)


def _persist_target_shot_length(shot_length: float):
    min_seg_duration, max_seg_duration = _derive_shot_duration_bounds(shot_length)
    save_config("AUDIO_MIN_SEGMENT_DURATION", str(min_seg_duration))
    save_config("AUDIO_MAX_SEGMENT_DURATION", str(max_seg_duration))
