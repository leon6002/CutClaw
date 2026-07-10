"""选材决策链记账(LOGIC.md §18)— 让"为什么选这段"全程可审计。

单例模式:Screenwriter.run 开跑时 start(trace_path),之后预算器/换锚修复
把决策事件写进来;剪辑落点方式直接记在 shot_point 条目上(随现有管道走)。
纯记账零 API;未 start 时所有调用都是空操作(预算器也被别的路径复用)。
"""

from __future__ import annotations

import json
import os
import threading

_LOCK = threading.Lock()
_CUR: dict = {"path": None, "data": None}


def _blank() -> dict:
    return {"version": 1, "menu": [], "rejects": [], "repairs": [], "meta": {}}


def start(trace_path: str) -> None:
    """开始为一次编剧运行记账(覆盖旧 trace——决策以最新一次为准)。"""
    with _LOCK:
        _CUR["path"] = trace_path
        _CUR["data"] = _blank()
        _save_locked()


def active() -> bool:
    return bool(_CUR["path"])


def _save_locked() -> None:
    try:
        with open(_CUR["path"], "w", encoding="utf-8") as f:
            json.dump(_CUR["data"], f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass


def set_meta(**kv) -> None:
    if not active():
        return
    with _LOCK:
        _CUR["data"]["meta"].update(kv)
        _save_locked()


def record_menu(chosen: list, rejects: list) -> None:
    """预算器结果:入选菜单的时刻 id 序列 + 被拒清单(id+原因)。"""
    if not active():
        return
    with _LOCK:
        _CUR["data"]["menu"] = [m.get("id") for m in chosen]
        _CUR["data"]["rejects"] = rejects
        _save_locked()


def record_repair(pos: int, original: str | None, reason: str, result: str) -> None:
    """换锚修复事件:第 pos 个镜头,原锚 original 因 reason 改为 result
    (result 为时刻 id 或 "agent")。"""
    if not active():
        return
    with _LOCK:
        _CUR["data"]["repairs"].append(
            {"shot": pos, "original": original, "reason": str(reason)[:120],
             "result": result})
        _save_locked()


def finish() -> None:
    with _LOCK:
        _CUR["path"] = None
        _CUR["data"] = None
