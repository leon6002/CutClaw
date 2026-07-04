"""Asset selection Agent — picks the best media combination for a montage.

A single LLM call (no ReAct loop) because the decision space is small:
pick top-K from a catalog of typically <100 annotated assets.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .index_store import get_all_summaries
from .models import AssetSelection
from .prompts import SELECTOR_PROMPT, SELECTOR_SYSTEM


def _default_agent_model() -> str:
    try:
        from src import config
        return getattr(config, "ASSET_SELECTOR_MODEL", None) or config.AGENT_LITELLM_MODEL
    except Exception:
        return "openai/gpt-4o"


def _default_agent_endpoint() -> str:
    try:
        from src import config
        return config.AGENT_LITELLM_URL
    except Exception:
        return ""


def _default_agent_api_key() -> str:
    try:
        from src import config
        return config.AGENT_LITELLM_API_KEY
    except Exception:
        return ""


def _parse_json_strict(content: str | None) -> dict | None:
    if not content:
        return None
    text = content.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: extract the outermost {...} block (models often wrap JSON in prose)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


def select_assets(
    instruction: str,
    index_dir: str | None = None,
    target_duration_sec: float = 30.0,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    stage_callback=None,
) -> AssetSelection:
    """Select the best asset combination for the given instruction.

    Args:
        instruction: User's creative brief (e.g. "新疆草原旅行混剪").
        index_dir: Path to the annotation index directory.
        target_duration_sec: Desired output duration in seconds.
        model: LLM model to use (defaults to config.ASSET_SELECTOR_MODEL).
        endpoint: API endpoint.
        api_key: API key.

    Returns:
        AssetSelection with the chosen files and rationale.
    """
    import time as _time

    import litellm

    def _st(stage: str, status: str, detail: str = ""):
        if stage_callback:
            try:
                stage_callback(stage, status, detail)
            except Exception:
                pass

    model = model or _default_agent_model()
    endpoint = endpoint or _default_agent_endpoint()
    api_key = api_key or _default_agent_api_key()

    _st("load_index", "running")
    summaries = get_all_summaries(index_dir)

    if summaries.startswith("(No annotated"):
        _st("load_index", "error", "没有已标注的素材")
        return AssetSelection(
            instruction=instruction,
            rationale="No annotated assets available. Please scan and annotate assets first.",
            target_duration_sec=target_duration_sec,
        )

    try:
        from .index_store import count_by_type
        counts = count_by_type(index_dir)
        counts_str = f"{counts.get('video', 0)} 视频 · {counts.get('image', 0)} 图片 · {counts.get('audio', 0)} 音乐"
    except Exception:
        counts_str = ""
    _st("load_index", "done", counts_str)

    _st("build_prompt", "running")
    prompt = SELECTOR_PROMPT.format(
        instruction=instruction,
        asset_summaries=summaries,
        target_duration_sec=int(target_duration_sec),
    )
    _st("build_prompt", "done", f"目标时长 {int(target_duration_sec)}s · 素材目录 {len(summaries)} 字符")

    for attempt in range(2):
        try:
            _st("llm_select", "running",
                f"{model}" + (f"（第 {attempt + 1} 次尝试）" if attempt > 0 else ""))
            t0 = _time.time()
            kwargs: dict = dict(
                model=model,
                messages=[
                    {"role": "system", "content": SELECTOR_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                # reasoning models spend budget on thought tokens first —
                # too small a max_tokens returns EMPTY content
                max_tokens=8192,
            )
            if endpoint:
                kwargs["api_base"] = endpoint
            if api_key:
                kwargs["api_key"] = api_key
            raw = litellm.completion(**kwargs)
            _msg = raw.choices[0].message
            content = _msg.content or getattr(_msg, "reasoning_content", None) or ""
            _st("llm_select", "done", f"{model} · {_time.time() - t0:.1f}s")

            _st("parse", "running")
            parsed = _parse_json_strict(content)
            if parsed and isinstance(parsed, dict):
                sel = AssetSelection(
                    instruction=instruction,
                    selected_videos=parsed.get("selected_videos", []),
                    selected_images=parsed.get("selected_images", []),
                    selected_audio=parsed.get("selected_audio", []),
                    rationale=parsed.get("rationale", ""),
                    alternative_videos=parsed.get("alternative_videos", []),
                    alternative_audio=parsed.get("alternative_audio", []),
                    target_duration_sec=float(parsed.get("target_duration_sec", target_duration_sec)),
                    narrative_idea=parsed.get("narrative_idea", ""),
                )
                _st("parse", "done",
                    f"{len(sel.selected_videos)} 视频 · {len(sel.selected_images)} 图片 · {len(sel.selected_audio)} 音乐")
                return sel
            _st("parse", "error", f"LLM 返回的不是有效 JSON：{(content or '')[:150]}")
            print(f"[AssetSelector] Unparseable LLM response (attempt {attempt + 1}): {(content or '')[:500]}")
        except Exception as e:
            _st("llm_select", "error", f"第 {attempt + 1} 次调用失败：{str(e)[:150]}")
            print(f"[AssetSelector] LLM call failed (attempt {attempt + 1}): {e}")

    # Fallback: empty selection
    return AssetSelection(
        instruction=instruction,
        rationale="Selection failed — please choose assets manually.",
        target_duration_sec=target_duration_sec,
    )


def quick_select_by_quality(
    index_dir: str | None = None,
    video_count: int = 5,
    image_count: int = 5,
    audio_count: int = 1,
) -> AssetSelection:
    """Simple fallback: pick top-N by quality_score, no LLM call."""
    from .index_store import load_index

    index = load_index(index_dir)
    if not index:
        return AssetSelection(
            instruction="(quality-based fallback)",
            rationale="No annotated assets.",
        )

    def _score(ann):
        a = ann.annotation
        return getattr(a, "quality_score", 5.0)

    videos = sorted(
        [a for a in index.values() if a.asset_type == "video"],
        key=_score, reverse=True,
    )
    images = sorted(
        [a for a in index.values() if a.asset_type == "image"],
        key=_score, reverse=True,
    )
    audios = sorted(
        [a for a in index.values() if a.asset_type == "audio"],
        key=_score, reverse=True,
    )

    return AssetSelection(
        instruction="(quality-based fallback)",
        selected_videos=[v.file_path for v in videos[:video_count]],
        selected_images=[i.file_path for i in images[:image_count]],
        selected_audio=[a.file_path for a in audios[:audio_count]],
        rationale=f"Top {video_count} videos, {image_count} images, {audio_count} audio by quality score.",
        target_duration_sec=30.0,
    )
