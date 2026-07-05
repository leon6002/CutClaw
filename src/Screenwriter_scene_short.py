import os
import json
import re
import time
import argparse
import random
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated as A
from src import config
from src.func_call_shema import doc as D
from src.prompt import GENERATE_STRUCTURE_PROPOSAL_PROMPT, GENERATE_SHOT_PLAN_PROMPT, SELECT_AUDIO_SEGMENT_PROMPT, SELECT_HOOK_DIALOGUE_PROMPT
from src.utils.media_utils import (
    hhmmss_to_seconds,
    load_scene_summaries,
    parse_srt_file,
    parse_structure_proposal_output,
    parse_shot_plan_output,
)
import litellm



HOOK_DIALOGUE_MAX_SUBTITLE_CHARS = 20000


class HookDialogueSelectionError(RuntimeError):
    """Raised when hook dialogue selection should fail the pipeline."""


def _has_meaningful_value(value) -> bool:
    """Check whether a JSON field is present with usable content."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def get_missing_shot_plan_parts(output_data: dict) -> list[str]:
    """Return operationally important shot-plan parts that are missing."""
    if not isinstance(output_data, dict):
        return ["root"]

    missing_parts: list[str] = []

    for key in ("instruction", "overall_theme", "narrative_logic"):
        if not _has_meaningful_value(output_data.get(key)):
            missing_parts.append(key)

    metadata = output_data.get("metadata")
    if not isinstance(metadata, dict):
        missing_parts.append("metadata")
    else:
        for key in ("selected_audio_start", "selected_audio_end"):
            if not _has_meaningful_value(metadata.get(key)):
                missing_parts.append(f"metadata.{key}")

    video_structure = output_data.get("video_structure")
    if not isinstance(video_structure, list) or not video_structure:
        missing_parts.append("video_structure")
        return missing_parts

    first_section = video_structure[0]
    if not isinstance(first_section, dict):
        missing_parts.append("video_structure[0]")
        return missing_parts

    for key in ("overall_theme", "narrative_logic", "start_time", "end_time"):
        if not _has_meaningful_value(first_section.get(key)):
            missing_parts.append(f"video_structure[0].{key}")

    section_shot_plan = first_section.get("shot_plan")
    if not isinstance(section_shot_plan, dict):
        missing_parts.append("video_structure[0].shot_plan")
    elif not isinstance(section_shot_plan.get("shots"), list) or not section_shot_plan.get("shots"):
        missing_parts.append("video_structure[0].shot_plan.shots")

    return missing_parts


# Call counter so the UI trace can number the Screenwriter's LLM calls
_SW_CALLS = {"n": 0}


def _sw_emit(**payload):
    try:
        from src.utils.progress import emit_progress
        emit_progress("screenwriter_llm", 1, 0, "step", **payload)
    except Exception:
        pass


def _sw_state(event: str):
    try:
        from src.utils.progress import emit_progress
        emit_progress("screenwriter_llm", 1, 0, event)
    except Exception:
        pass


def _sw_stage(label: str):
    """Coarse 'which sub-step' label for the screenwriter phase (音乐段→结构→
    分镜→开场白→保存). Surfaced on the canvas so the phase isn't a black box
    between LLM calls. Separate 'stage' event — does NOT pollute the LLM call
    trace / prompt-reply workbench."""
    try:
        from src.utils.progress import emit_progress
        emit_progress("screenwriter_llm", 1, 0, "stage", note=label)
    except Exception:
        pass


def _call_agent_litellm(messages: list, max_tokens: int = None) -> str | None:
    """Call the agent LLM via litellm. Returns content string or None on failure."""
    import time as _time

    kwargs = dict(
        model=config.AGENT_LITELLM_MODEL,
        messages=messages,
        max_tokens=max_tokens or config.AGENT_MODEL_MAX_TOKEN,
        api_key=config.AGENT_LITELLM_API_KEY,
        # Shot-plan generation is a large prompt + long structured output;
        # reasoning models routinely exceed 60s. Configurable via config.
        timeout=getattr(config, "AGENT_LLM_TIMEOUT_SEC", 300),
    )
    if config.AGENT_LITELLM_URL:
        kwargs["api_base"] = config.AGENT_LITELLM_URL

    _SW_CALLS["n"] += 1
    _n = _SW_CALLS["n"]
    _prompt_text = "\n\n".join(
        f"[{m.get('role', '?')}]\n{m.get('content', '')}" for m in messages
    )[:15000]
    _sw_state("start")
    _sw_emit(phase="calling", iter=_n, elapsed=0)
    _t0 = _time.time()

    # Transient SSL/network hiccups are common on large requests — retry with
    # backoff instead of dying on the first failure (parity with the editor).
    _max_retries = int(getattr(config, "AGENT_LLM_MAX_RETRIES", 3))
    resp = None
    for _attempt in range(1, _max_retries + 1):
        try:
            resp = litellm.completion(**kwargs)
            # DETERMINISTIC failure — don't blind-retry, retry CHANGED:
            # finish_reason=length means the reply was cut off by max_tokens
            # (reasoning models spend thinking tokens from the same budget).
            # Re-sending the identical request loses identically; feedback
            # loops even make it worse (longer prompt → more thinking →
            # less output). Double the budget and go again instead.
            _fr = getattr(resp.choices[0], "finish_reason", "") or ""
            if _fr == "length" and kwargs.get("max_tokens", 0) < 65536:
                kwargs["max_tokens"] = min(int(kwargs["max_tokens"] * 2), 65536)
                print(f"⚠️ [Screenwriter] reply truncated at max_tokens "
                      f"(finish_reason=length) — retrying with {kwargs['max_tokens']} tokens",
                      flush=True)
                continue
            break
        except Exception as _e:
            _msg = str(_e)[:300]
            print(f"⚠️ [Screenwriter] LLM call failed (attempt {_attempt}/{_max_retries}): {_msg}", flush=True)
            _sw_emit(
                phase="result", tool="screenwriter", iter=_n,
                elapsed=round(_time.time() - _t0),
                verdict=("warn" if _attempt < _max_retries else "fail"),
                result=f"第 {_attempt}/{_max_retries} 次调用失败：{_msg}",
            )
            if _attempt < _max_retries:
                _time.sleep(min(2 ** _attempt, 15))

    if resp is None:
        _sw_emit(phase="action", tool="screenwriter", iter=_n,
                 elapsed=round(_time.time() - _t0), args=_prompt_text, reply="")
        _sw_state("fail")
        return None

    try:
        content = resp.choices[0].message.content
        _reasoning = getattr(resp.choices[0].message, "reasoning_content", None) or ""

        def _emit_ok(final_text: str | None):
            _sw_emit(
                phase="action", tool="screenwriter", iter=_n,
                elapsed=round(_time.time() - _t0),
                args=_prompt_text,
                reply=(( _reasoning + "\n\n" if _reasoning else "") + (final_text or "(空回复)"))[:15000],
            )
            _sw_state("done")

        if content is None:
            _emit_ok(None)
            return None
        if isinstance(content, str):
            content = content.strip()
            _emit_ok(content)
            return content or None
        # Some providers may return structured content blocks.
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")).strip())
                elif isinstance(item, str):
                    text_parts.append(item.strip())
            merged = "\n".join([p for p in text_parts if p]).strip()
            _emit_ok(merged)
            return merged or None
        merged = str(content).strip()
        _emit_ok(merged)
        return merged or None
    except Exception as e:
        _sw_emit(
            phase="result", tool="screenwriter", iter=_n,
            elapsed=round(_time.time() - _t0),
            verdict="fail", result=f"LLM 调用失败：{str(e)[:600]}",
        )
        _sw_emit(phase="action", tool="screenwriter", iter=_n,
                 elapsed=round(_time.time() - _t0), args=_prompt_text, reply="")
        _sw_state("fail")
        return None


def _to_audio_seconds(value) -> float:
    """Normalize section timestamps into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    return hhmmss_to_seconds(str(value))


def _seconds_to_mmss(seconds: float) -> str:
    """Convert seconds to MM:SS.f format (e.g. 90.5 → '1:30.5')."""
    seconds = max(0.0, seconds)
    total_tenths = int(round(seconds * 10))
    tenths = total_tenths % 10
    total_secs = total_tenths // 10
    mm = total_secs // 60
    ss = total_secs % 60
    return f"{mm}:{ss:02d}.{tenths}"


def _parse_audio_segment_selection_response(content: str) -> dict | None:
    """Parse JSON response for audio section selection."""
    if not content:
        return None
    clean = content.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
    try:
        parsed = json.loads(clean)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def select_audio_segment(audio_db: dict, instruction: str) -> tuple[str, str]:
    """Use LLM to select the best music section, then trim to target duration if needed."""
    sections = audio_db.get('sections', [])
    summary = audio_db.get('overall_analysis', {}).get('summary', '')
    min_dur = config.AUDIO_SEGMENT_MIN_DURATION_SEC
    max_dur = config.AUDIO_SEGMENT_MAX_DURATION_SEC
    target_dur = (min_dur + max_dur) / 2

    if not sections:
        return '0:00', _seconds_to_mmss(target_dur)

    # Build sections_info for LLM — measured stats included so the choice is
    # grounded in real energy/tempo data, not the LLM's prose impressions
    facts = audio_db.get('facts') or {}
    sections_info = []
    for i, sec in enumerate(sections):
        sec_start = _to_audio_seconds(sec.get('Start_Time', 0))
        sec_end = _to_audio_seconds(sec.get('End_Time', 0))
        dur = round(max(0.0, sec_end - sec_start), 1)
        sections_info.append({
            "section_index": i,
            "name": sec.get('name', ''),
            "description": sec.get('description', ''),
            **({"measured": sec.get('measured')} if sec.get('measured') else {}),
            "Start_Time": sec.get('Start_Time', ''),
            "End_Time": sec.get('End_Time', ''),
            "duration_seconds": dur,
            # the chosen section anchors the START of the window; the window
            # extends across following sections up to the target duration
            "enough_music_after_start": "✓" if (
                max((_to_audio_seconds(s.get('End_Time', 0)) for s in sections), default=0.0)
                - _to_audio_seconds(sec.get('Start_Time', 0)) >= min_dur
            ) else "✗ near track end",
        })

    def _apply_section(idx: int) -> tuple[str, str]:
        """The chosen section is an ANCHOR, not a cage: music is continuous, so a
        target longer than one section extends across the following sections.
        (Previously the window was clamped to the section end — a 100s target on
        a track segmented into ~30s sections silently produced a ~30s video.)"""
        sec = sections[idx]
        sec_start = _to_audio_seconds(sec.get('Start_Time', 0))
        sec_end = _to_audio_seconds(sec.get('End_Time', 0))
        sec_dur = max(0.0, sec_end - sec_start)
        if min_dur <= sec_dur <= max_dur:
            return str(sec.get('Start_Time', _seconds_to_mmss(sec_start))), str(sec.get('End_Time', _seconds_to_mmss(sec_end)))
        track_end = max((_to_audio_seconds(s.get('End_Time', 0)) for s in sections), default=sec_end)
        if sec_dur > max_dur:
            # single long section: trim to target
            return _seconds_to_mmss(sec_start), _seconds_to_mmss(sec_start + target_dur)
        # section shorter than target: extend forward across following sections
        trim_end = min(sec_start + target_dur, track_end)
        if trim_end - sec_start < min_dur:
            # not enough music left after this anchor — slide the window back
            sec_start = max(0.0, track_end - target_dur)
            trim_end = track_end
            print(f"🎵 [Audio] anchor too late for {target_dur:.0f}s target — window slid back to "
                  f"{sec_start:.1f}s–{trim_end:.1f}s")
        return _seconds_to_mmss(sec_start), _seconds_to_mmss(trim_end)

    feedback = None
    for attempt in range(1, config.AUDIO_SEGMENT_SELECTION_MAX_RETRIES + 1):
        _facts_line = ""
        if facts.get("bpm_felt"):
            _facts_line = (f"\nMEASURED FACTS (signal analysis, trust these numbers): "
                           f"felt tempo {facts['bpm_felt']} BPM, bar ≈ {facts.get('bar_sec')}s"
                           + (f", global energy climax around {facts['climax_sec']}s"
                              if facts.get('climax_sec') is not None else "") + "\n")
        prompt = SELECT_AUDIO_SEGMENT_PROMPT.format(
            summary=summary + _facts_line,
            sections_json=json.dumps(sections_info, indent=2, ensure_ascii=False),
            instruction=instruction,
            min_duration_sec=min_dur,
            max_duration_sec=max_dur,
            feedback_block=(
                f"\nValidation feedback from previous attempt: {feedback}\n"
                if feedback else ""
            ),
        )
        content = _call_agent_litellm([{"role": "user", "content": prompt}], max_tokens=512)
        if not content:
            feedback = "No response returned. Return valid JSON with section_index."
            continue

        result = _parse_audio_segment_selection_response(content)
        if not isinstance(result, dict):
            feedback = "Response is not a JSON object. Return {\"section_index\": N, \"reason\": \"...\"}"
            continue

        raw_idx = result.get('section_index')
        if not isinstance(raw_idx, int) or raw_idx < 0 or raw_idx >= len(sections):
            feedback = (
                f"Invalid section_index: {raw_idx!r}. "
                f"Must be an integer between 0 and {len(sections) - 1}."
            )
            continue

        return _apply_section(raw_idx)

    # Fallback: pick section with duration closest to target_dur
    best_idx = 0
    best_diff = float('inf')
    for i, sec in enumerate(sections):
        sec_start = _to_audio_seconds(sec.get('Start_Time', 0))
        sec_end = _to_audio_seconds(sec.get('End_Time', 0))
        diff = abs((sec_end - sec_start) - target_dur)
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return _apply_section(best_idx)


def filter_sub_segments_by_range(
    sections: list, start_time_str: str, end_time_str: str
) -> list:
    """Collect all sub-segments whose time range overlaps with [start_time_str, end_time_str].

    Returns a flat list of sub-segment dicts.
    """
    def _to_sec(t):
        if isinstance(t, (int, float)):
            return float(t)
        parts = str(t).split(':')
        if len(parts) == 3:
            h, m, s = [float(x) for x in parts]
            return h * 3600 + m * 60 + s
        elif len(parts) == 2:
            m, s = [float(x) for x in parts]
            return m * 60 + s
        else:
            try:
                return float(parts[0])
            except ValueError:
                return 0.0

    range_start = _to_sec(start_time_str)
    range_end = _to_sec(end_time_str)

    result = []
    for section in sections:
        section_start = _to_sec(section.get('Start_Time', section.get('start_time', 0)))
        for sub in section.get('detailed_analysis', {}).get('sections', []):
            sub_start = section_start + _to_sec(sub.get('Start_Time', sub.get('start_time', 0)))
            sub_end = section_start + _to_sec(sub.get('End_Time', sub.get('end_time', 0)))
            if sub_end > range_start and sub_start < range_end:
                sub_abs = dict(sub)
                sub_abs['Start_Time'] = sub_start
                sub_abs['End_Time'] = sub_end
                result.append(sub_abs)

    # Fill gaps: extend each section's End_Time to the next section's Start_Time
    for i in range(len(result) - 1):
        gap = result[i + 1]['Start_Time'] - result[i]['End_Time']
        if gap > 0:
            result[i]['End_Time'] = result[i + 1]['Start_Time']

    return result


def check_scene_distribution(
    structure_proposal: dict,
    available_ids: list[int],
) -> tuple[bool, str]:
    """Validate the scene proposal against the scenes the model was ACTUALLY
    shown (`available_ids` — sparse: skipped/low-importance scenes leave gaps).

    The previous version compared against a dense 0..N-1 range derived from the
    raw file count, so with usable ids like [1, 2] it demanded coverage of
    "scenes 0/3" the model never saw → unsatisfiable → the Screenwriter burned
    every retry. Everything here is now keyed to the real available ids.

    Returns (passed, feedback_message).
    """
    if not structure_proposal or not isinstance(structure_proposal, dict):
        return False, "Invalid structure proposal format."

    related_scenes = structure_proposal.get('related_scenes', [])
    if not related_scenes:
        return False, "No related_scenes found in proposal."

    avail = sorted({int(s) for s in available_ids})
    avail_set = set(avail)
    total = len(avail)
    if total == 0:
        return False, "No usable scenes available."

    # every pick must be a scene the model was actually shown
    for scene_id in related_scenes:
        if not isinstance(scene_id, int):
            return False, f"Invalid scene index (not an integer): {scene_id}"
        if scene_id not in avail_set:
            return False, (f"Scene index {scene_id} is not an available scene. "
                           f"Select ONLY from these scene ids: {avail}.")

    min_scenes = min(8, total)
    if len(related_scenes) < min_scenes:
        return False, (
            f"Too few scenes selected: {len(related_scenes)}. "
            f"Need at least {min_scenes} picks (available scene ids: {avail}); "
            f"reuse and spread across them."
        )

    # Timeline spread over the ACTUAL available scenes (ids are chronological).
    # ≥3 scenes: bucket the available ids into thirds BY POSITION (robust to
    # sparse ids) and require a pick from each. <3 scenes: just require every
    # available scene is used — no three-thirds split is possible or meaningful.
    used = set(related_scenes)
    if total >= 3:
        c1, c2 = total // 3, (2 * total) // 3
        groups = {"early": avail[:c1], "middle": avail[c1:c2], "late": avail[c2:]}
        missing = [name for name, g in groups.items() if g and not (used & set(g))]
        if missing:
            return False, (
                "Scene distribution is too concentrated. Missing coverage in: "
                + ", ".join(f"{name} section (scene ids {groups[name]})" for name in missing)
                + f". Available scene ids: {avail}. Add a pick from each missing section."
            )
    else:
        unused = [s for s in avail if s not in used]
        if unused:
            return False, (
                f"Only {total - len(unused)}/{total} available scenes used — "
                f"include scene id(s) {unused} too so the montage isn't stuck on one location."
            )

    print(f"[Scene Check] {len(related_scenes)} picks over {total} available scenes "
          f"{avail}; used {sorted(used)}.")
    return True, f"Scene selection looks good - {len(related_scenes)} picks across {total} scenes."


def generate_structure_proposal(
    video_scene_path: A[str, D("Path to scene_summaries_video folder containing scene JSON files.")],
    audio_caption_path: A[str, D("Path to captions.json describing the audio segments.")],
    user_instruction: A[str, D("Editing brief provided by the user.")],
    selected_start_str: A[str | None, D("Start time of the selected audio segment.")] = None,
    selected_end_str: A[str | None, D("End time of the selected audio segment.")] = None,
    feedback: A[str | None, D("Validation feedback from previous attempt, injected to guide retry.")] = None,
    main_character: A[str | None, D("Name of the main character to focus on.")] = None,
) -> str | None:
    """Generate a structure proposal for the video editing based on scene summaries."""
    video_summary, usable_ids = load_scene_summaries(video_scene_path)
    scene_count = len(usable_ids)
    # ids are SPARSE (skipped scenes leave gaps) — the model must select from the
    # exact ids it was shown, not a dense 0..N-1 range.
    max_scene_index = max(usable_ids) if usable_ids else 0

    if isinstance(audio_caption_path, str):
        with open(audio_caption_path, 'r', encoding='utf-8') as f:
            audio_caption_data = json.load(f)
    else:
        audio_caption_data = audio_caption_path

    audio_summary = audio_caption_data.get('overall_analysis', {}).get('summary', '')
    sections = audio_caption_data.get('sections', [])

    # If a selected audio range is provided, filter sections to only those overlapping it
    if selected_start_str and selected_end_str:
        def _to_sec(t):
            if isinstance(t, (int, float)):
                return float(t)
            parts = str(t).split(':')
            if len(parts) == 3:
                h, m, s = [float(x) for x in parts]
                return h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = [float(x) for x in parts]
                return m * 60 + s
            else:
                try:
                    return float(parts[0])
                except ValueError:
                    return 0.0

        range_start = _to_sec(selected_start_str)
        range_end = _to_sec(selected_end_str)
        sections = [
            s for s in sections
            if _to_sec(s.get('End_Time', 0)) > range_start and _to_sec(s.get('Start_Time', 0)) < range_end
        ]

    filtered_sections = [
        {
            'name': s.get('name', ''),
            'description': s.get('description', ''),
            'Start_Time': s.get('Start_Time', ''),
            'End_Time': s.get('End_Time', ''),
        }
        for s in sections
    ]
    audio_structure = json.dumps(filtered_sections, indent=2, ensure_ascii=False)

    prompt = GENERATE_STRUCTURE_PROPOSAL_PROMPT
    prompt = prompt.replace("TOTAL_SCENE_COUNT_PLACEHOLDER", str(scene_count))
    prompt = prompt.replace("MAX_SCENE_INDEX_PLACEHOLDER", str(max_scene_index))
    prompt = prompt.replace("AVAILABLE_SCENE_IDS_PLACEHOLDER", str(usable_ids))
    prompt = prompt.replace("VIDEO_SUMMARY_PLACEHOLDER", video_summary)
    prompt = prompt.replace("AUDIO_SUMMARY_PLACEHOLDER", audio_summary)
    prompt = prompt.replace("AUDIO_STRUCTURE_PLACEHOLDER", audio_structure)
    prompt = prompt.replace("INSTRUCTION_PLACEHOLDER", user_instruction)
    prompt = prompt.replace("MAIN_CHARACTER_PLACEHOLDER", main_character or "the main character")

    if feedback:
        prompt += f"\n\n**IMPORTANT - PREVIOUS ATTEMPT FAILED:**\n{feedback}\nPlease fix this issue in your response."

    return _call_agent_litellm([{"role": "user", "content": prompt}], max_tokens=config.AGENT_MODEL_MAX_TOKEN)


def generate_structure_proposal_with_retry(
    video_scene_path: str,
    audio_caption_path: str,
    user_instruction: str,
    max_retries: int = 2,
    selected_start_str: str | None = None,
    selected_end_str: str | None = None,
    main_character: str | None = None,
) -> str | None:
    """Generate structure proposal with basic validation and retry."""
    _, usable_ids = load_scene_summaries(video_scene_path)
    content = generate_structure_proposal(
        video_scene_path, audio_caption_path, user_instruction,
        selected_start_str, selected_end_str, main_character=main_character,
    )
    if content is None:
        return None

    last_feedback = None
    for retry in range(max_retries):
        try:
            parsed = parse_structure_proposal_output(content)
            if parsed is None:
                content = generate_structure_proposal(
                    video_scene_path, audio_caption_path, user_instruction,
                    selected_start_str, selected_end_str, last_feedback, main_character=main_character,
                )
                continue

            passed, last_feedback = check_scene_distribution(parsed, usable_ids)
            if passed:
                return content

            if retry < max_retries - 1:
                content = generate_structure_proposal(
                    video_scene_path, audio_caption_path, user_instruction,
                    selected_start_str, selected_end_str, last_feedback, main_character=main_character,
                )
            else:
                return content

        except Exception as e:
            if retry < max_retries - 1:
                content = generate_structure_proposal(
                    video_scene_path, audio_caption_path, user_instruction,
                    selected_start_str, selected_end_str, last_feedback, main_character=main_character,
                )
            else:
                return content

    return content


def generate_shot_plan(
    music_detailed_structure: A[list | dict | str, D("Detailed per-segment music analysis for current section.")],
    video_section_proposal: A[dict, D("Section brief extracted from structure proposal.")],
    scene_folder_path: A[str | None, D("Path to scene summaries folder.")] = None,
    user_instruction: A[str, D("User's editing instruction.")] = "",
    main_character: str | None = None,
    feedback: str | None = None,
) -> str | None:
    """Generate a one-to-one shot mapping for each music segment."""
    if isinstance(music_detailed_structure, (dict, list)):
        music_json = json.dumps(music_detailed_structure, ensure_ascii=False, indent=2)
    else:
        music_json = str(music_detailed_structure or '')

    prompt = GENERATE_SHOT_PLAN_PROMPT
    prompt = prompt.replace("AUDIO_SUMMARY_PLACEHOLDER", music_json)
    prompt = prompt.replace("VIDEO_SECTION_INFO_PLACEHOLDER", str(video_section_proposal))
    prompt = prompt.replace("INSTRUCTION_PLACEHOLDER", user_instruction)
    prompt = prompt.replace("MAIN_CHARACTER_PLACEHOLDER", main_character or "the main character")
    if feedback:
        prompt += (
            "\n\n**IMPORTANT — YOUR PREVIOUS ATTEMPT WAS REJECTED:**\n"
            f"{feedback}\nFix this in your new plan."
        )

    related_video_context = ""
    related_scenes = video_section_proposal.get("related_scenes", []) if isinstance(video_section_proposal, dict) else []
    if related_scenes and scene_folder_path:
        scene_descriptions = []
        for scene_idx in related_scenes:
            scene_file = os.path.join(scene_folder_path, f"scene_{scene_idx}.json")
            if os.path.exists(scene_file):
                try:
                    with open(scene_file, 'r', encoding='utf-8') as f:
                        scene_data = json.load(f)
                    scene_summary = (
                        scene_data.get('video_analysis', {})
                        .get('scene_caption', {})
                        .get('scene_summary', '')
                    )
                    if scene_summary:
                        _ct = scene_data.get('capture_time')
                        _when = f" (shot at {_ct.replace('T', ' ')})" if _ct else ""
                        scene_descriptions.append(f"Scene {scene_idx}{_when}: {scene_summary}")
                except Exception:
                    pass
        related_video_context = "\n".join(scene_descriptions)

    prompt = prompt.replace("RELATED_VIDEO_PLACEHOLDER", related_video_context)

    # ── Curation-first: anchor shots on REAL measured moments ──────────────
    # The pool file sits next to the merged_scenes dir (project convention).
    anchors_block = ""
    if scene_folder_path and getattr(config, "CURATION_FIRST", True):
        pool_path = os.path.join(os.path.dirname(scene_folder_path), "highlight_pool.json")
        if os.path.exists(pool_path):
            try:
                with open(pool_path, "r", encoding="utf-8") as f:
                    _pool = json.load(f).get("moments", [])
            except Exception:  # noqa: BLE001
                _pool = []
            _rel = set()
            for _x in related_scenes or []:
                try:
                    _rel.add(int(_x))
                except (TypeError, ValueError):
                    pass
            _cands = [m for m in _pool if not _rel or m.get("scene") in _rel]
            if not _cands:
                _cands = _pool
            _cands = sorted(_cands, key=lambda m: -m.get("score", 0))[:25]
            if _cands:
                _lines = []
                for m in _cands:
                    _v = " | REAL VOICES" if m.get("sound") else ""
                    _t = (f" | shot at {m['capture_time'][5:16].replace('T', ' ')}"
                          if m.get("capture_time") else "")
                    _lines.append(
                        f"- {m['id']} | scene {m.get('scene', '?')} | "
                        f"{m['start']:.1f}-{m['end']:.1f}s ({m['duration']:.1f}s) | "
                        f"quality {m.get('score', 0) * 10:.1f}/10{_v}{_t} | {m.get('desc', '')[:160]}")
                _n_voice = sum(1 for m in _cands if m.get("sound"))
                _voice_rule = (
                    f"- {_n_voice} moment(s) are marked REAL VOICES (companions talking/laughing in "
                    "the original audio — the renderer ducks the music and lets them play). These are "
                    "the emotional core of a memory montage: you MUST anchor at least "
                    f"{min(2, _n_voice)} shot(s) on REAL VOICES moments.\n" if _n_voice else "")
                anchors_block = (
                    "\n\n[CURATED REAL MOMENTS — measured from the actual footage]\n"
                    "MANDATORY RULES:\n"
                    "- EVERY shot MUST carry an \"anchor_id\" field naming one moment from the list "
                    "below. Anchoring is the DEFAULT, not the exception — these are the best real "
                    "moments this footage has; your job is to ARRANGE them to fit the music, not to "
                    "imagine better ones.\n"
                    "- Describe the CHOSEN moment's visible imagery in \"content\" and set "
                    "\"related_scene\" to the moment's scene.\n"
                    "- Never assign the same moment to two shots.\n"
                    + _voice_rule +
                    "- \"anchor_id\": null is allowed ONLY when every listed moment is already used "
                    "or truly none fits the segment — scarcity is the only excuse, not preference.\n"
                    "quality is MEASURED (blur/shake + content). Capture times give the journey order.\n"
                    + "\n".join(_lines)
                )
    prompt = prompt + anchors_block

    return _call_agent_litellm([{"role": "user", "content": prompt}], max_tokens=config.AGENT_MODEL_MAX_TOKEN)


def _validate_shot_plan_result(shot_plan: dict | None, expect_non_empty: bool = True) -> tuple[bool, str]:
    """Validate parsed shot plan structure."""
    if not isinstance(shot_plan, dict):
        return False, "shot_plan is not a dict"

    shots = shot_plan.get("shots")
    if not isinstance(shots, list):
        return False, "missing or invalid 'shots' list"

    if expect_non_empty and len(shots) == 0:
        return False, "'shots' is empty"

    for idx, shot in enumerate(shots):
        if not isinstance(shot, dict):
            return False, f"shot at index {idx} is not an object"

    return True, "ok"


def _check_scene_load(shot_plan: dict, scene_folder_path: str | None) -> tuple[bool, str]:
    """Per-scene material budget: total shot seconds assigned to a scene must fit
    inside that scene's real footage (with headroom for spacing/unusable parts).
    The Screenwriter tends to pile shots onto its favorite scene — a 19s scene
    was asked for 16s across 3 shots, structurally starving the last one."""
    if not scene_folder_path or not os.path.isdir(scene_folder_path):
        return True, "ok"

    def _ts(v) -> float:
        try:
            s = str(v).strip()
            if ":" in s:
                parts = [float(x) for x in s.split(":")]
                return sum(p * m for p, m in zip(reversed(parts), [1, 60, 3600]))
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    scene_dur: dict = {}
    for f in os.listdir(scene_folder_path):
        m = re.search(r"scene_(\d+)\.json$", f)
        if not m:
            continue
        try:
            with open(os.path.join(scene_folder_path, f), "r", encoding="utf-8") as fh:
                tr = (json.load(fh).get("time_range") or {})
            d = _ts(tr.get("end_seconds")) - _ts(tr.get("start_seconds"))
            if d > 0:
                scene_dur[int(m.group(1))] = d
        except Exception:
            continue
    if not scene_dur:
        return True, "ok"

    load: dict = {}
    for shot in shot_plan.get("shots", []):
        try:
            sc = int(shot.get("related_scene"))
            load.setdefault(sc, [0, 0.0])
            load[sc][0] += 1
            load[sc][1] += float(shot.get("time_duration") or 0.0)
        except (TypeError, ValueError):
            continue

    over = []
    for sc, (n, secs) in load.items():
        cap = scene_dur.get(sc)
        if cap and secs > cap * 0.7:
            over.append(f"scene {sc}: {n} shots totaling {secs:.1f}s but only {cap:.0f}s of footage "
                        f"exists (budget {cap * 0.7:.0f}s)")
    if over:
        # Only scenes the plan actually references are valid redistribution
        # targets — suggesting an unreferenced (likely skipped/low-importance)
        # scene just feeds the model a scene it can't use. Budget over those too.
        usable = {sc: scene_dur[sc] for sc in load if sc in scene_dur}
        spare = sorted(((sc, d) for sc, d in usable.items()
                        if load.get(sc, [0, 0.0])[1] < d * 0.4), key=lambda x: -x[1])
        # Retrying only helps if there's somewhere to move shots TO. When the
        # total requested shot-seconds already exceed the referenced scenes'
        # budget (Σ 0.7·footage), or no scene is under-used, no redistribution
        # can satisfy the per-scene cap — the material is simply too thin for the
        # target. Retrying then just burns 80s+ reasoning calls and lands on the
        # same over-subscribed plan anyway. Accept it; the orchestrator capacity
        # guard + deterministic fallback rebalance what they can at dispatch.
        total_budget = sum(d * 0.7 for d in usable.values())
        total_requested = sum(secs for _n, secs in load.values())
        if total_requested > total_budget or not spare:
            print(f"⚖️ [Screenwriter] scene over-subscription is material-limited "
                  f"(requested {total_requested:.0f}s vs budget {total_budget:.0f}s, "
                  f"spare scenes: {len(spare)}) — accepting instead of retrying")
            return True, "ok (material-limited)"
        hint = ", ".join(f"scene {sc} ({d:.0f}s free)" for sc, d in spare[:4])
        return False, ("Scene over-subscription: " + "; ".join(over)
                       + f". Redistribute shots to under-used scenes: {hint}. "
                       f"Never assign more shot-seconds to a scene than ~70% of its footage.")
    return True, "ok"


def _attach_anchors(shot_plan: dict, scene_folder_path: str | None):
    """Resolve each shot's anchor_id into the real moment's time range.

    Anchored shots skip the per-shot agent entirely — the orchestrator trims
    deterministically inside the curated moment."""
    if not scene_folder_path or not isinstance(shot_plan, dict):
        return
    pool_path = os.path.join(os.path.dirname(scene_folder_path), "highlight_pool.json")
    if not os.path.exists(pool_path):
        return
    try:
        with open(pool_path, "r", encoding="utf-8") as f:
            by_id = {m.get("id"): m for m in json.load(f).get("moments", [])}
    except Exception:  # noqa: BLE001
        return
    used = set()
    n = 0
    for shot in shot_plan.get("shots", []) or []:
        aid = shot.get("anchor_id")
        m = by_id.get(str(aid)) if aid else None
        if not m or aid in used:      # unknown or duplicated anchor → agent path
            shot.pop("anchor_id", None)
            continue
        used.add(aid)
        shot["anchor"] = {
            "video_path": m.get("video_path", ""),
            "start": float(m.get("start", 0.0)),
            "end": float(m.get("end", 0.0)),
        }
        n += 1
    if n:
        print(f"✨ [Curation] {n}/{len(shot_plan.get('shots', []) or [])} shots anchored on real moments")


def generate_shot_plan_with_retry(
    music_detailed_structure: list | dict | str,
    video_section_proposal: dict,
    scene_folder_path: str | None = None,
    user_instruction: str = "",
    max_retries: int | None = None,
    main_character: str | None = None,
) -> dict | None:
    """Generate and parse shot plan with validation + retry."""
    retries = max(1, int(max_retries or getattr(config, "AGENT_MODEL_MAX_RETRIES", 3)))
    base_backoff = float(getattr(config, "AGENT_RATE_LIMIT_BACKOFF_BASE", 1.0))
    max_backoff = float(getattr(config, "AGENT_RATE_LIMIT_MAX_BACKOFF", 8.0))
    expected_non_empty = bool(music_detailed_structure) if isinstance(music_detailed_structure, list) else True
    last_error = "unknown_error"

    best_plan = None   # structurally valid but scene-overloaded — usable fallback
    for attempt in range(1, retries + 1):
        raw_shot_plan = generate_shot_plan(
            music_detailed_structure,
            video_section_proposal,
            scene_folder_path,
            user_instruction,
            main_character=main_character,
            feedback=(last_error if attempt > 1 else None),
        )
        if not raw_shot_plan:
            last_error = "empty response from shot plan request"
        else:
            parsed_shot_plan = parse_shot_plan_output(raw_shot_plan)
            is_valid, reason = _validate_shot_plan_result(
                parsed_shot_plan,
                expect_non_empty=expected_non_empty,
            )
            if is_valid:
                _attach_anchors(parsed_shot_plan, scene_folder_path)
                load_ok, load_reason = _check_scene_load(parsed_shot_plan, scene_folder_path)
                if load_ok:
                    return parsed_shot_plan
                best_plan = parsed_shot_plan
                last_error = load_reason
                print(f"⚖️ [Screenwriter] shot plan rejected: {load_reason}")
            else:
                last_error = f"invalid shot plan format: {reason}"
                _tail = (raw_shot_plan or "")[-120:].replace("\n", " ")
                print(f"⚠️ [Screenwriter] shot plan attempt {attempt} invalid: {reason} "
                      f"(response ends with: …{_tail}) — a mid-JSON cutoff means the reply "
                      f"hit AGENT_MODEL_MAX_TOKEN", flush=True)

        if attempt < retries:
            wait_seconds = min(max_backoff, base_backoff * (2 ** (attempt - 1)))
            print(f"🔄 [Screenwriter: Shot Plan] Retrying in {wait_seconds:.1f}s...")
            time.sleep(wait_seconds)

    if best_plan is not None:
        # scene-overloaded but structurally sound — the orchestrator's capacity
        # guard will rebalance at dispatch; better than failing the pipeline
        print("⚖️ [Screenwriter] scene load still unbalanced after retries — "
              "proceeding; orchestrator capacity guard will rebalance")
        return best_plan
    return None


def _seconds_to_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hh = total_ms // 3600000
    mm = (total_ms % 3600000) // 60000
    ss = (total_ms % 60000) // 1000
    ms = total_ms % 1000
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def _subtitle_line_text(sub: dict) -> str:
    text = (sub.get('text') or '').strip()
    speaker = (sub.get('speaker') or '').strip()
    if speaker:
        return f"[{speaker}] {text}"
    return text


def _normalize_dialogue_text(text: str) -> str:
    """Normalize dialogue text for robust subtitle matching."""
    if not text:
        return ""
    clean = str(text).lower().strip()
    clean = re.sub(r"\[[^\]]+\]", " ", clean)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"[^\w]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def _dialogue_similarity(a: str, b: str) -> float:
    """Compute fuzzy similarity between two normalized subtitle lines."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq_score = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        seq_score = max(seq_score, 0.9)

    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return seq_score
    jaccard = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
    return max(seq_score, 0.65 * seq_score + 0.35 * jaccard)


def _match_dialogue_lines_to_subtitles(
    lines: list[str],
    subtitles: list[dict],
    min_score: float = 0.55,
) -> list[dict]:
    """Match model-selected lines back to original SRT subtitle entries."""
    if not lines or not subtitles:
        return []

    subtitle_norm = [_normalize_dialogue_text(_subtitle_line_text(s)) for s in subtitles]
    matched_indices = []
    last_idx = -1

    for raw_line in lines:
        norm_line = _normalize_dialogue_text(str(raw_line))
        if not norm_line:
            continue

        best_idx = None
        best_score = 0.0
        for idx in range(last_idx + 1, len(subtitles)):
            score = _dialogue_similarity(norm_line, subtitle_norm[idx])
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx is not None and best_score >= min_score:
            matched_indices.append(best_idx)
            last_idx = best_idx

    if not matched_indices:
        return []

    unique_sorted = sorted(set(matched_indices))
    return [subtitles[i] for i in unique_sorted]


def _build_timed_lines(subtitles: list[dict], clip_start_sec: float) -> list[dict]:
    """Build per-line absolute and relative timing records."""
    timed_lines = []
    for sub in subtitles:
        abs_start = float(sub.get('start_sec', 0.0))
        abs_end = float(sub.get('end_sec', 0.0))
        rel_start = max(0.0, abs_start - clip_start_sec)
        rel_end = max(rel_start, abs_end - clip_start_sec)
        timed_lines.append({
            "text": _subtitle_line_text(sub),
            "start": _seconds_to_srt_time(rel_start),
            "end": _seconds_to_srt_time(rel_end),
            "source_start": _seconds_to_srt_time(abs_start),
            "source_end": _seconds_to_srt_time(abs_end),
        })
    return timed_lines



def _format_subtitles_for_prompt(
    subtitles: list[dict],
    max_chars: int = HOOK_DIALOGUE_MAX_SUBTITLE_CHARS,
    window_mode: str = "tail",
    start_index: int | None = None,
) -> tuple[str, int]:
    all_blocks = []
    for idx, sub in enumerate(subtitles, start=1):
        text = _subtitle_line_text(sub).strip()
        if not text:
            continue
        dur = max(0.0, sub.get('end_sec', 0.0) - sub.get('start_sec', 0.0))
        block = (
            f"{idx}\n"
            f"{_seconds_to_srt_time(sub.get('start_sec', 0.0))} --> {_seconds_to_srt_time(sub.get('end_sec', 0.0))} [{dur:.1f}s]\n"
            f"{text}"
        )
        all_blocks.append(block)

    if not all_blocks:
        return "", 0

    used = 0
    selected = []

    if window_mode == "random_window":
        if start_index is None:
            start_index = random.randrange(len(all_blocks))
        start_index = max(0, min(start_index, len(all_blocks) - 1))
        iterable = all_blocks[start_index:]
    elif window_mode == "head":
        iterable = all_blocks
    else:
        iterable = reversed(all_blocks)

    for block in iterable:
        if selected and used + len(block) + 2 > max_chars:
            break
        selected.append(block)
        used += len(block) + 2

    if window_mode == "tail":
        selected.reverse()

    return "\n\n".join(selected), len(selected)


def _extract_first_balanced_json_object(text: str) -> str | None:
    """Extract the first balanced {...} JSON object from mixed text."""
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_str = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def _parse_llm_json_object(raw_content: str) -> tuple[dict | None, Exception | None]:
    """Parse LLM output into a JSON object with light normalization."""
    if not raw_content:
        return None, ValueError("empty_response")

    clean = raw_content.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean)
    clean = clean.replace("\ufeff", "").strip()
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", clean)

    candidates: list[str] = [clean]
    extracted = _extract_first_balanced_json_object(clean)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    # Some providers output non-standard \' escapes; normalize them.
    for c in list(candidates):
        fixed = c.replace("\\'", "'")
        if fixed not in candidates:
            candidates.append(fixed)

    last_error: Exception | None = None
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed, None
            last_error = TypeError(f"json_root_not_object: {type(parsed).__name__}")
        except Exception as e:
            last_error = e

    return None, last_error


def select_hook_dialogue(
    subtitle_path: str,
    shot_plan: dict,
    instruction: str,
    target_duration_sec: float = 10.0,
    main_character: str | None = None,
    prompt_window_mode: str = "tail_then_head",
    random_window_attempts: int = 3,
) -> dict:
    """Select ONE opening dialogue clip for the whole video (target ~10s)."""
    subtitles_all = parse_srt_file(subtitle_path)
    if not subtitles_all:
        raise HookDialogueSelectionError(
            f"No subtitle entries found in subtitle file: {subtitle_path}"
        )

    shots_info = []
    for section in shot_plan.get('video_structure', []):
        for shot in section.get('shot_plan', {}).get('shots', []):
            content = shot.get('content', '')
            emotion = shot.get('emotion', '')
            if content or emotion:
                shots_info.append(f"- {content} [{emotion}]")
    shot_plan_summary = "\n".join(shots_info[:10]) if shots_info else instruction

    subtitle_candidates = subtitles_all
    min_duration_sec = max(6.0, target_duration_sec - 5.0)
    max_duration_sec = target_duration_sec + 5.0
    failure_reasons: list[str] = []

    def _try_select(window_mode: str, attempt_side: str, start_index: int | None = None) -> dict | None:
        subtitle_context, _ = _format_subtitles_for_prompt(
            subtitle_candidates,
            max_chars=HOOK_DIALOGUE_MAX_SUBTITLE_CHARS,
            window_mode=window_mode,
            start_index=start_index,
        )
        if not subtitle_context:
            failure_reasons.append(
                f"from_{attempt_side}: subtitle context is empty after formatting"
            )
            return None

        prompt = SELECT_HOOK_DIALOGUE_PROMPT.format(
            instruction=instruction,
            main_character=main_character or "the main character",
            shot_plan_summary=shot_plan_summary,
            subtitles=subtitle_context,
            target_duration_sec=int(round(target_duration_sec)),
            min_duration_sec=int(round(min_duration_sec)),
            max_duration_sec=int(round(max_duration_sec)),
        )

        llm_result = None
        for attempt in range(2):
            attempt_prompt = prompt
            if attempt == 1:
                attempt_prompt += (
                    "\n\nIMPORTANT: Your previous answer was invalid. "
                    "Return ONLY a valid JSON object with keys lines,start,end,reason."
                )
            raw_content = _call_agent_litellm([{"role": "user", "content": attempt_prompt}], max_tokens=16000)
            if not raw_content:
                continue
            parsed, _ = _parse_llm_json_object(raw_content)
            if parsed is not None:
                llm_result = parsed
                break

        if llm_result is None:
            failure_reasons.append(
                f"from_{attempt_side}: LLM did not return a valid JSON selection"
            )
            return None

        start_sec = hhmmss_to_seconds(str(llm_result.get('start', '')).strip())
        end_sec = hhmmss_to_seconds(str(llm_result.get('end', '')).strip())
        lines = llm_result.get('lines') if isinstance(llm_result.get('lines'), list) else []

        matched_subtitles = _match_dialogue_lines_to_subtitles(lines, subtitle_candidates)
        if not matched_subtitles and not (start_sec <= 0 and end_sec <= 0):
            matched_subtitles = [
                s for s in subtitle_candidates
                if s.get('end_sec', 0.0) >= start_sec and s.get('start_sec', 0.0) <= end_sec
            ]
        if not matched_subtitles:
            failure_reasons.append(
                f"from_{attempt_side}: no subtitles matched selected dialogue lines={lines!r}, "
                f"start={llm_result.get('start')!r}, end={llm_result.get('end')!r}"
            )
            return None

        matched_subtitles = sorted(matched_subtitles, key=lambda s: float(s.get('start_sec', 0.0)))
        source_start_sec = float(matched_subtitles[0].get('start_sec', 0.0))
        source_end_sec = float(matched_subtitles[-1].get('end_sec', 0.0))
        duration_sec = max(0.0, source_end_sec - source_start_sec)

        if duration_sec < min_duration_sec or duration_sec > max_duration_sec:
            reason = (
                f"from_{attempt_side}: duration {duration_sec:.2f}s out of range "
                f"[{min_duration_sec:.0f}, {max_duration_sec:.0f}]s"
            )
            print(f"[Hook Dialogue] Rejected: {reason}")
            failure_reasons.append(reason)
            return None

        timed_lines = _build_timed_lines(matched_subtitles, source_start_sec)
        return {
            "lines": [item["text"] for item in timed_lines if item.get("text")],
            "timed_lines": timed_lines,
            "start": _seconds_to_srt_time(0.0),
            "end": _seconds_to_srt_time(duration_sec),
            "source_start": _seconds_to_srt_time(source_start_sec),
            "source_end": _seconds_to_srt_time(source_end_sec),
            "reason": llm_result.get('reason', ''),
            "duration_seconds": round(duration_sec, 3),
            "selection_method": "llm_srt_matched",
        }

    result = None
    if prompt_window_mode == "random_window":
        total_subtitles = len(subtitle_candidates)
        attempt_count = max(1, min(int(random_window_attempts), total_subtitles))
        random_start_indices = random.sample(range(total_subtitles), k=attempt_count)
        for attempt_number, start_index in enumerate(random_start_indices, start=1):
            attempt_side = f"random_window_{attempt_number}_start_{start_index + 1}"
            print(
                f"[Hook Dialogue] Trying random subtitle window "
                f"{attempt_number}/{attempt_count} starting at subtitle #{start_index + 1}..."
            )
            result = _try_select(
                window_mode="random_window",
                attempt_side=attempt_side,
                start_index=start_index,
            )
            if result is not None:
                break
    else:
        # First attempt from end; retry from beginning if rejected
        result = _try_select(window_mode="tail", attempt_side="end")
        if result is None:
            print("[Hook Dialogue] Retrying from beginning of subtitles...")
            result = _try_select(window_mode="head", attempt_side="beginning")
        if result is None and random_window_attempts > 0:
            total_subtitles = len(subtitle_candidates)
            attempt_count = max(1, min(int(random_window_attempts), total_subtitles))
            random_start_indices = random.sample(range(total_subtitles), k=attempt_count)
            for attempt_number, start_index in enumerate(random_start_indices, start=1):
                attempt_side = f"fallback_random_window_{attempt_number}_start_{start_index + 1}"
                print(
                    f"[Hook Dialogue] Retrying with random subtitle window "
                    f"{attempt_number}/{attempt_count} starting at subtitle #{start_index + 1}..."
                )
                result = _try_select(
                    window_mode="random_window",
                    attempt_side=attempt_side,
                    start_index=start_index,
                )
                if result is not None:
                    break

    if result is None:
        failure_detail = " | ".join(failure_reasons) if failure_reasons else "unknown reason"
        raise HookDialogueSelectionError(
            "Failed to select hook dialogue from subtitles. "
            f"subtitle_path={subtitle_path}. Details: {failure_detail}"
        )

    print(f"\n[Hook Dialogue Selected]")
    print(f"  Lines: {result.get('lines')}")
    print(f"  Relative Time: {result.get('start')} --> {result.get('end')} ({result.get('duration_seconds')}s)")
    print(f"  Source Time: {result.get('source_start')} --> {result.get('source_end')}")
    print(f"  Reason: {result.get('reason')}\n")
    return result


def refresh_hook_dialogue_in_shot_plan(
    shot_plan_path: str,
    subtitle_path: str,
    instruction: str | None = None,
    main_character: str | None = None,
    target_duration_sec: float = 10.0,
    prompt_window_mode: str = "tail_then_head",
    random_window_attempts: int = 3,
) -> dict:
    """Refresh hook dialogue in an existing shot-plan JSON file and save it in place."""
    if not os.path.exists(shot_plan_path):
        raise FileNotFoundError(f"Shot plan file not found: {shot_plan_path}")
    if not subtitle_path or not os.path.exists(subtitle_path):
        raise FileNotFoundError(f"Subtitle file not found: {subtitle_path}")

    with open(shot_plan_path, 'r', encoding='utf-8') as f:
        shot_plan_data = json.load(f)

    instruction_to_use = (
        instruction
        or shot_plan_data.get("instruction")
        or shot_plan_data.get("narrative_logic")
    )
    if not instruction_to_use:
        raise ValueError(
            f"Cannot refresh hook dialogue because instruction is missing in {shot_plan_path}"
        )

    shot_plan_data["hook_dialogue"] = select_hook_dialogue(
        subtitle_path,
        shot_plan_data,
        instruction_to_use,
        target_duration_sec=target_duration_sec,
        main_character=main_character,
        prompt_window_mode=prompt_window_mode,
        random_window_attempts=random_window_attempts,
    )
    shot_plan_data["instruction"] = instruction_to_use

    with open(shot_plan_path, 'w', encoding='utf-8') as f:
        json.dump(shot_plan_data, f, indent=2, ensure_ascii=False)

    return shot_plan_data


class Screenwriter:
    def __init__(self, video_scene_path, audio_caption_path, output_path, video_path=None, subtitle_path=None, main_character=None, **kwargs):
        self.video_scene_path = video_scene_path
        self.audio_caption_path = audio_caption_path
        self.audio_db = json.load(open(audio_caption_path, 'r', encoding='utf-8'))
        self.video_path = video_path
        self.subtitle_path = subtitle_path
        self.output_path = output_path
        self.main_character = main_character

    def run(self, instruction) -> dict:
        """Run the screenwriter pipeline to generate a shot plan."""
        if self.output_path and os.path.exists(self.output_path):
            try:
                with open(self.output_path, 'r', encoding='utf-8') as f:
                    existing_output = json.load(f)
            except Exception as exc:
                print(f"⚠️  [Screenwriter] Warning: failed to load existing output {self.output_path}: {exc}. Regenerating...")
            else:
                missing_parts = get_missing_shot_plan_parts(existing_output)
                if missing_parts:
                    print(
                        f"⚠️  [Screenwriter] Existing shot plan is incomplete. "
                        f"Missing parts: {', '.join(missing_parts)}. Regenerating..."
                    )
                elif self.subtitle_path and os.path.exists(self.subtitle_path) and not existing_output.get("hook_dialogue"):
                    print(
                        f"⚠️  [Screenwriter] Existing shot plan is missing hook_dialogue. "
                        f"Retrying hook dialogue selection for {self.output_path}..."
                    )
                    _sw_stage("补全开场对白")
                    existing_output = refresh_hook_dialogue_in_shot_plan(
                        self.output_path,
                        self.subtitle_path,
                        instruction=instruction,
                        main_character=self.main_character,
                        target_duration_sec=10.0,
                    )
                    print(f"💾 [Screenwriter] Updated hook dialogue saved to {self.output_path}")
                    return existing_output
                else:
                    _sw_stage("复用已有分镜")
                    return existing_output

        # Step 1: Select the audio segment first
        _sw_stage("选择音乐段落")
        selected_start_str, selected_end_str = select_audio_segment(self.audio_db, instruction)

        print(
            f"\n🎵 [Screenwriter] Audio segment selected: "
            f"{selected_start_str} → {selected_end_str}\n"
        )

        # Step 2: Generate structure proposal scoped to the selected audio segment
        _sw_stage("生成结构提案")
        structure_proposal = generate_structure_proposal_with_retry(
            self.video_scene_path, self.audio_caption_path, instruction,
            selected_start_str=selected_start_str,
            selected_end_str=selected_end_str,
            main_character=self.main_character,
        )
        if structure_proposal is None:
            raise RuntimeError("generate_structure_proposal_with_retry returned None — check API connectivity and model config")
        structure_proposal = parse_structure_proposal_output(structure_proposal)

        audio_sections = self.audio_db.get('sections', [])

        selected_sub_segments = filter_sub_segments_by_range(
            audio_sections, selected_start_str, selected_end_str
        )

        def _to_sec(t):
            if isinstance(t, (int, float)):
                return float(t)
            parts = str(t).split(':')
            if len(parts) == 3:
                h, m, s = [float(x) for x in parts]
                return h * 3600 + m * 60 + s
            elif len(parts) == 2:
                m, s = [float(x) for x in parts]
                return m * 60 + s
            else:
                try:
                    return float(parts[0])
                except ValueError:
                    return 0

        start_time = _to_sec(selected_start_str)
        end_time = _to_sec(selected_end_str)
        duration = end_time - start_time

        # Determine display name from first overlapping top-level section
        segment_name = "audio segment"
        for sec in audio_sections:
            sec_start = _to_sec(sec.get('Start_Time', 0))
            sec_end = _to_sec(sec.get('End_Time', 0))
            if sec_end > start_time and sec_start < end_time:
                segment_name = sec.get('name', segment_name)
                break

        _sw_stage("生成分镜脚本")
        shot_plan = generate_shot_plan_with_retry(
            selected_sub_segments,
            structure_proposal,
            self.video_scene_path,
            instruction,
            main_character=self.main_character,
        )
        if shot_plan is None:
            raise RuntimeError(
                "Failed to generate a valid shot plan after retries — "
                "check API connectivity / model availability / prompt output format."
            )
        # Select hook dialogue — only when the video actually has dialogue subtitles.
        # Scenery / no-speech videos produce an empty SRT (0 bytes); skip the hook
        # gracefully instead of failing the whole pipeline.
        hook_dialogue = None
        if (
            self.subtitle_path
            and os.path.exists(self.subtitle_path)
            and os.path.getsize(self.subtitle_path) > 0
        ):
            partial_output = {
                "video_structure": [{**structure_proposal, "shot_plan": shot_plan}]
            }
            try:
                _sw_stage("挑选开场对白")
                hook_dialogue = select_hook_dialogue(
                    self.subtitle_path,
                    partial_output,
                    instruction,
                    target_duration_sec=15.0,
                    main_character=self.main_character,
                )
            except HookDialogueSelectionError as e:
                print(f"⚠️ [Screenwriter] No usable dialogue for hook, skipping: {e}")
                hook_dialogue = None
        else:
            hook_dialogue = None

        import datetime
        output_data = {
            "instruction": instruction,
            "metadata": {
                "created_at": datetime.datetime.now().isoformat(),
                "video_path": self.video_path,
                "audio_path": self.audio_caption_path,
                "video_scene_path": self.video_scene_path,
                "selected_audio_start": selected_start_str,
                "selected_audio_end": selected_end_str,
            },
            "overall_theme": f"Short video for {segment_name}",
            "narrative_logic": instruction,
            "hook_dialogue": hook_dialogue,
            "video_structure": [{
                **structure_proposal,
                "start_time": start_time,
                "end_time": end_time,
                "shot_plan": shot_plan,
            }],
        }

        print("\n✅ [Screenwriter] Short video shot plan generated successfully!")
        _sw_stage("保存分镜脚本")

        if self.output_path:
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
            with open(self.output_path, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            print(f"\n💾 [Screenwriter] Complete shot plan saved to {self.output_path}")

        return output_data


def main():
    def _norm_name(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    def _resolve_video_assets(
        video_path: str,
        video_scene_path: str | None,
    ) -> str:
        """Resolve video scene path from a raw video path."""
        if video_scene_path:
            return video_scene_path

        if not video_path:
            raise ValueError("--video-path is required")

        repo_root = Path(__file__).resolve().parents[1]
        video_db_root = repo_root / "video_database" / "Video"
        if not video_db_root.exists():
            raise FileNotFoundError(
                f"Cannot find video database root at: {video_db_root}. "
                "Run from the repo workspace or pass --video-scene-path manually."
            )

        stem = Path(video_path).stem
        target_norm = _norm_name(stem)

        match_dir: Path | None = None
        if (video_db_root / stem).is_dir():
            match_dir = video_db_root / stem
        else:
            for child in video_db_root.iterdir():
                if child.is_dir() and _norm_name(child.name) == target_norm:
                    match_dir = child
                    break

        if match_dir is None:
            raise FileNotFoundError(
                f"Cannot resolve video database folder for '{stem}'. "
                "Please pass --video-scene-path manually."
            )

        captions_dir = match_dir / "captions"
        for candidate in ("scene_summaries_video", "scene_summaries"):
            cand = captions_dir / candidate
            if cand.is_dir():
                return str(cand)

        raise FileNotFoundError(
            f"Cannot find scene summaries folder under: {captions_dir}. "
            "Please pass --video-scene-path manually."
        )

    parser = argparse.ArgumentParser(
        description="Generate a short-video shot plan from video scene summaries and audio captions."
    )
    parser.add_argument("--video-scene-path", default=None,
                        help="Path to scene summaries folder. If omitted, inferred from --video-path.")
    parser.add_argument("--audio-caption-path", required=True,
                        help="Path to captions.json describing the audio segments.")
    parser.add_argument("--video-path", required=True,
                        help="Path to the source video file.")
    parser.add_argument("--output-path", required=True,
                        help="Output path to save the generated shot plan JSON.")
    parser.add_argument("--instruction", default="A dynamic montage.",
                        help="User instruction / creative brief.")
    args = parser.parse_args()

    resolved_video_scene_path = _resolve_video_assets(args.video_path, args.video_scene_path)

    agent = Screenwriter(
        video_scene_path=resolved_video_scene_path,
        audio_caption_path=args.audio_caption_path,
        output_path=args.output_path,
        video_path=args.video_path,
    )
    agent.run(args.instruction)


if __name__ == "__main__":
    main()
