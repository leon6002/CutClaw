"""Detailed logging of every LLM request/response via a global LiteLLM callback.

Registering one callback captures ALL litellm.completion / acompletion calls
across the whole codebase (editor core, screenwriter, reviewer, video/scene
captioning, audio) without touching each call site.

Each call is written as one JSON line to a per-run JSONL file containing the
full (sanitized) request messages, the response content + tool calls, token
usage and latency. Base64 image payloads are stripped so the log stays readable.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading

try:  # Prefer subclassing the official base so all callback hooks are honored.
    from litellm.integrations.custom_logger import CustomLogger as _CallbackBase
except Exception:  # pragma: no cover - fallback if litellm layout changes
    class _CallbackBase:  # type: ignore
        pass

_LOCK = threading.Lock()
_STATE = {
    "log_path": None,
    "registered": False,
    "calls": 0,
    "success": 0,
    "failure": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def _sanitize_content(content):
    """Strip base64 image payloads from message content to keep logs readable."""
    if isinstance(content, list):
        cleaned = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype in ("image_url", "image", "input_image"):
                    url = ""
                    if isinstance(part.get("image_url"), dict):
                        url = part["image_url"].get("url", "")
                    elif isinstance(part.get("image_url"), str):
                        url = part["image_url"]
                    if isinstance(url, str) and url.startswith("data:"):
                        cleaned.append({"type": ptype, "image": f"<base64 image, {len(url)} chars>"})
                    else:
                        cleaned.append({"type": ptype, "image": url[:200]})
                else:
                    cleaned.append(part)
            else:
                cleaned.append(part)
        return cleaned
    return content


def _sanitize_messages(messages):
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append({"repr": str(m)[:500]})
            continue
        entry = {"role": m.get("role")}
        if "content" in m:
            entry["content"] = _sanitize_content(m.get("content"))
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("tool_call_id"):
            entry["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            entry["name"] = m["name"]
        out.append(entry)
    return out


def _extract_usage(response_obj):
    usage = None
    if response_obj is None:
        return {}
    try:
        usage = getattr(response_obj, "usage", None)
        if usage is None and isinstance(response_obj, dict):
            usage = response_obj.get("usage")
    except Exception:
        usage = None
    if usage is None:
        return {}
    def _g(key):
        if isinstance(usage, dict):
            return usage.get(key)
        return getattr(usage, key, None)
    return {
        "prompt_tokens": _g("prompt_tokens"),
        "completion_tokens": _g("completion_tokens"),
        "total_tokens": _g("total_tokens"),
    }


def _extract_response(response_obj):
    if response_obj is None:
        return {"content": None, "tool_calls": None}
    try:
        choices = getattr(response_obj, "choices", None)
        if choices is None and isinstance(response_obj, dict):
            choices = response_obj.get("choices")
        if not choices:
            return {"content": None, "tool_calls": None}
        msg = choices[0].get("message") if isinstance(choices[0], dict) else getattr(choices[0], "message", None)
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        raw_tcs = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
        tool_calls = None
        if raw_tcs:
            tool_calls = []
            for tc in raw_tcs:
                fn = tc.get("function") if isinstance(tc, dict) else getattr(tc, "function", None)
                name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
                tool_calls.append({"name": name, "arguments": args})
        return {"content": content, "tool_calls": tool_calls}
    except Exception as e:
        return {"content": f"<parse error: {e}>", "tool_calls": None}


def _latency_seconds(start_time, end_time):
    try:
        return (end_time - start_time).total_seconds()
    except Exception:
        try:
            return float(end_time) - float(start_time)
        except Exception:
            return None


def _write_record(record: dict):
    path = _STATE["log_path"]
    if not path:
        return
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _LOCK:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _record_call(kwargs, response_obj, start_time, end_time, ok: bool, error=None):
    usage = _extract_usage(response_obj) if ok else {}
    with _LOCK:
        _STATE["calls"] += 1
        if ok:
            _STATE["success"] += 1
            _STATE["prompt_tokens"] += usage.get("prompt_tokens") or 0
            _STATE["completion_tokens"] += usage.get("completion_tokens") or 0
            _STATE["total_tokens"] += usage.get("total_tokens") or 0
        else:
            _STATE["failure"] += 1

    record = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "status": "success" if ok else "failure",
        "model": (kwargs or {}).get("model"),
        "latency_s": _latency_seconds(start_time, end_time),
        "request": {
            "messages": _sanitize_messages((kwargs or {}).get("messages")),
            "num_tools": len((kwargs or {}).get("tools") or []),
            "temperature": (kwargs or {}).get("temperature"),
            "max_tokens": (kwargs or {}).get("max_tokens"),
        },
    }
    if ok:
        record["response"] = _extract_response(response_obj)
        record["usage"] = usage
    else:
        record["error"] = str(error) if error is not None else str(response_obj)
    _write_record(record)


class _LiteLLMFileLogger(_CallbackBase):
    """LiteLLM CustomLogger-compatible handler (sync + async)."""
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            _record_call(kwargs, response_obj, start_time, end_time, ok=True)
        except Exception:
            pass

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            err = (kwargs or {}).get("exception") if isinstance(kwargs, dict) else None
            _record_call(kwargs, response_obj, start_time, end_time, ok=False, error=err)
        except Exception:
            pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            _record_call(kwargs, response_obj, start_time, end_time, ok=True)
        except Exception:
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            err = (kwargs or {}).get("exception") if isinstance(kwargs, dict) else None
            _record_call(kwargs, response_obj, start_time, end_time, ok=False, error=err)
        except Exception:
            pass


def setup_llm_logging(log_dir: str = None) -> str | None:
    """Register the global LiteLLM callback. Returns the log file path.

    Idempotent: safe to call multiple times (only registers once per process).
    """
    if _STATE["registered"]:
        return _STATE["log_path"]
    try:
        import litellm
    except Exception as e:
        print(f"⚠️  [LLM Log] litellm not available, request logging disabled: {e}")
        return None

    log_dir = log_dir or os.path.join("Output", "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"llm_calls_{stamp}.jsonl")
    _STATE["log_path"] = log_path

    handler = _LiteLLMFileLogger()
    try:
        existing = list(getattr(litellm, "callbacks", []) or [])
        existing.append(handler)
        litellm.callbacks = existing
    except Exception as e:
        print(f"⚠️  [LLM Log] Failed to register callback: {e}")
        return None

    _STATE["registered"] = True
    print(f"📝 [LLM Log] Logging every LLM request/response to: {log_path}")
    return log_path


def print_llm_summary():
    """Print a token-usage summary (call at end of run)."""
    if not _STATE["registered"]:
        return
    with _LOCK:
        calls = _STATE["calls"]
        success = _STATE["success"]
        failure = _STATE["failure"]
        pt = _STATE["prompt_tokens"]
        ct = _STATE["completion_tokens"]
        tt = _STATE["total_tokens"]
        path = _STATE["log_path"]
    print(f"\n{'='*60}")
    print("📊 LLM Usage Summary")
    print(f"  calls              : {calls} ({success} ok, {failure} failed)")
    print(f"  prompt tokens      : {pt:,}")
    print(f"  completion tokens  : {ct:,}")
    print(f"  total tokens       : {tt:,}")
    print(f"  detailed log       : {path}")
    print(f"{'='*60}\n")
