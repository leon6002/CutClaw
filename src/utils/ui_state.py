"""Gitignored persistence for UI-remembered inputs (application *state*).

The last-used video/audio/instruction/subtitle selections are per-machine,
per-session STATE — not configuration. They must never live in the git-tracked
src/config.py, which would commit machine-specific absolute paths on every UI
interaction. They are stored here in Output/ui_state.json (Output/ is
gitignored) instead.

config.py keeps only empty defaults for these keys, and the two regex-based
config readers/writers (server/main.py, src/ui/helpers.py) route them through
this module:

- ``read_state(key)``  — cfg(): value from ui_state.json, or None if unset.
- ``write_state(key, value)`` — save_config(): persist to ui_state.json instead
  of materializing the value back into the tracked config.py.
"""

import json
import os
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATE_PATH = os.path.join(PROJECT_ROOT, "Output", "ui_state.json")

# UI-remembered inputs that live in ui_state.json, not config.py. These are the
# "last used" form fields the pipeline never reads from config (the UI passes
# them to local_run.py as CLI args); config.py only held them to repopulate the
# sidebar, which dirtied a tracked source file with local paths.
UI_STATE_KEYS = frozenset({"VIDEO_PATH", "AUDIO_PATH", "INSTRUCTION", "SRT_PATH"})

_lock = threading.Lock()


def _read() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:  # noqa: BLE001
        return {}


def read_state(key: str):
    """Value for a UI-state key from ui_state.json, or None if unset."""
    v = _read().get(key)
    return v if isinstance(v, str) else None


def write_state(key: str, value: str):
    """Persist a UI-state key to Output/ui_state.json (thread-safe, atomic)."""
    with _lock:
        d = _read()
        if d.get(key) == value:
            return
        d[key] = value
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
