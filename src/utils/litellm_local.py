"""Make LiteLLM read its model-cost map from a local file, never the network.

By default LiteLLM fetches ``model_prices_and_context_window.json`` from GitHub
at ``import litellm`` time. On a slow or blocked connection that request times
out (5s) and logs a WARNING every run:

    LiteLLM: Failed to fetch remote model cost map from https://raw.github... :
    _ssl.c:1000: The handshake operation timed out. Falling back to local backup.

Two-part fix, so there is no network call at all:

  1. Set ``LITELLM_LOCAL_MODEL_COST_MAP=True`` *before* litellm is first
     imported. LiteLLM then skips the fetch and loads its bundled backup.
  2. Overlay the fresher copy the user downloaded to
     ``asset/model_prices_and_context_window.json`` onto ``litellm.model_cost``
     (falls back silently to litellm's bundled backup if that file is missing).

IMPORT THIS MODULE BEFORE ``import litellm`` in every process entry point. Only
the *first* litellm import in a process reads the env var, so this must win the
race — hence it is imported at the very top of server/main.py, local_run.py,
render/render_video.py and app.py.
"""

import json
import os
from datetime import datetime
from pathlib import Path

# Must be set before litellm's __init__ runs -> skips the remote fetch entirely.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# <root>/asset/model_prices_and_context_window.json  (this file is <root>/src/utils/)
_LOCAL_MAP = Path(__file__).resolve().parents[2] / "asset" / "model_prices_and_context_window.json"

# Where to grab a fresh copy when the pricing/context data goes stale.
_UPDATE_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"

_applied = False


def apply() -> None:
    """Overlay the local cost map onto ``litellm.model_cost`` (idempotent)."""
    global _applied
    if _applied:
        return
    _applied = True

    try:
        data = json.loads(_LOCAL_MAP.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # No local file -> the env var already forced litellm's bundled backup.
        return
    except Exception as e:  # corrupt JSON etc. — keep whatever litellm loaded
        print(f"[litellm_local] could not read {_LOCAL_MAP}: {e}", flush=True)
        return

    if not isinstance(data, dict) or not data:
        return

    # Tell the user how fresh this file is and where to refresh it — the data
    # is a local snapshot and won't self-update while the network is skipped.
    try:
        mtime = datetime.fromtimestamp(_LOCAL_MAP.stat().st_mtime)
        age_days = (datetime.now() - mtime).days
        stale = "  ⚠ 建议手动更新" if age_days >= 90 else ""
        print(
            f"[litellm_local] 使用本地模型价格表（{len(data)} 个模型），"
            f"最近更新 {mtime:%Y-%m-%d}（{age_days} 天前）{stale}\n"
            f"[litellm_local] 如需更新，下载覆盖 {_LOCAL_MAP.name}：{_UPDATE_URL}",
            flush=True,
        )
    except Exception:
        pass

    import litellm

    # Match litellm's own load path: expand any `aliases` lists into entries.
    try:
        from litellm.litellm_core_utils.get_model_cost_map import _expand_model_aliases
        data = _expand_model_aliases(data)
    except Exception:
        pass  # older litellm without alias expansion — raw dict is still fine

    # Mutate in place: other modules hold references to this same dict.
    litellm.model_cost.clear()
    litellm.model_cost.update(data)


apply()
