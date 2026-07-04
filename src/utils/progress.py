"""Structured fine-grained progress events for the web UI execution monitor.

Emitters call ``emit_progress(task, total, idx, event, **extra)``:
  - In a pipeline subprocess, events print as ``@@PROGRESS {json}`` stdout
    lines; the web backend parses them into per-segment states.
  - In-process consumers (e.g. annotation jobs in the server) set ``HOOK``
    to receive event dicts directly.

Events: "start" (segment picked up by a worker), "done", "fail",
"retry" (goes back to pending, e.g. shot conflict re-run).
"""

import json

HOOK = None  # optional callable(dict) — set by in-process consumers


def emit_progress(task: str, total: int, idx: int, event: str, **extra):
    ev = {"task": task, "total": int(total), "idx": int(idx), "event": event}
    if extra:
        ev.update(extra)
    hook = HOOK
    if hook is not None:
        try:
            hook(ev)
            return
        except Exception:
            pass
    try:
        print("@@PROGRESS " + json.dumps(ev, ensure_ascii=False), flush=True)
    except Exception:
        pass
