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
        return None


def select_assets(
    instruction: str,
    index_dir: str | None = None,
    target_duration_sec: float = 30.0,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
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
    import litellm

    model = model or _default_agent_model()
    endpoint = endpoint or _default_agent_endpoint()
    api_key = api_key or _default_agent_api_key()

    summaries = get_all_summaries(index_dir)

    if summaries.startswith("(No annotated"):
        return AssetSelection(
            instruction=instruction,
            rationale="No annotated assets available. Please scan and annotate assets first.",
            target_duration_sec=target_duration_sec,
        )

    prompt = SELECTOR_PROMPT.format(
        instruction=instruction,
        asset_summaries=summaries,
        target_duration_sec=int(target_duration_sec),
    )

    for attempt in range(2):
        try:
            kwargs: dict = dict(
                model=model,
                messages=[
                    {"role": "system", "content": SELECTOR_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=2048,
            )
            if endpoint:
                kwargs["api_base"] = endpoint
            if api_key:
                kwargs["api_key"] = api_key
            raw = litellm.completion(**kwargs)
            content = raw.choices[0].message.content
            parsed = _parse_json_strict(content)
            if parsed and isinstance(parsed, dict):
                return AssetSelection(
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
        except Exception as e:
            if attempt == 1:
                print(f"[AssetSelector] LLM call failed: {e}")

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
