#!/usr/bin/env python3
"""
Render video from shot_point.json

This script reads a shot_point.json file containing clip timestamps and
renders them into a single video file using ffmpeg.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap


# Force UTF-8 stdout/stderr so print() never crashes on non-GBK characters
# (ø/é/中文/emoji in file paths) — the Windows console defaults to GBK, which
# can't encode them and raises UnicodeEncodeError mid-render.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _ensure_ffmpeg_on_path() -> None:
    """Make ffmpeg/ffprobe discoverable on Windows when not activated via conda.

    Searches the same locations as local_run.py so ffmpeg subprocess calls
    don't fail with FileNotFoundError (WinError 2).
    """
    project_ffmpeg = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "tools", "ffmpeg"
    )
    candidates = [
        project_ffmpeg,
        os.path.join(sys.prefix, "Library", "bin"),
        os.path.join(sys.prefix, "Library", "mingw-w64", "bin"),
        os.path.join(sys.prefix, "Library", "usr", "bin"),
        os.path.join(sys.prefix, "Scripts"),
        sys.prefix,
    ]
    existing = os.environ.get("PATH", "")
    parts = existing.split(os.pathsep)
    prepend = [p for p in candidates if os.path.isdir(p) and p not in parts]
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + parts)
from typing import List, Dict, Any


def hhmmss_to_seconds(time_str: str) -> float:
    """Convert HH:MM:SS.s or MM:SS.s to seconds."""
    parts = time_str.strip().split(':')
    if len(parts) == 3:
        h, m = int(parts[0]), int(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s
    elif len(parts) == 2:
        m = int(parts[0])
        s = float(parts[1])
        return m * 60 + s
    else:
        return float(parts[0])


def srt_time_to_seconds(time_str: str) -> float:
    """Convert SRT-style HH:MM:SS,mmm to seconds."""
    clean = time_str.strip().replace(',', '.')
    return hhmmss_to_seconds(clean)


def strip_speaker_prefix(text: str) -> str:
    """Remove leading speaker tag like [Mia Dolan] from subtitle text."""
    if not text:
        return text
    return re.sub(r'^\s*\[[^\]]+\]\s*', '', text).strip()


def build_hook_timed_clips(hook_dialogue: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a single intro clip from hook_dialogue; timed_lines carry per-line subtitle timing."""
    source_start = hook_dialogue.get('source_start')
    source_end = hook_dialogue.get('source_end')

    timed_lines = hook_dialogue.get('timed_lines') or []
    if not source_start and timed_lines:
        source_start = timed_lines[0].get('source_start')
    if not source_end and timed_lines:
        source_end = timed_lines[-1].get('source_end')

    if not source_start or not source_end:
        return []

    try:
        start_sec = srt_time_to_seconds(source_start)
        end_sec = srt_time_to_seconds(source_end)
    except Exception:
        return []

    duration = max(0.0, end_sec - start_sec)
    if duration <= 0.0:
        return []

    # Build per-line subtitle entries with times relative to clip start
    subtitle_lines = []
    for line in timed_lines:
        text = strip_speaker_prefix((line.get('text') or '').strip())
        if not text:
            continue
        try:
            line_start_sec = srt_time_to_seconds(line['source_start']) - start_sec
            line_end_sec = srt_time_to_seconds(line['source_end']) - start_sec
        except Exception:
            continue
        subtitle_lines.append({
            'text': text,
            'start': max(0.0, line_start_sec),
            'end': min(duration, line_end_sec),
        })

    return [{
        'section_idx': -1,
        'shot_idx': -1,
        'start_sec': start_sec,
        'end_sec': end_sec,
        'duration': duration,
        'start_str': f"{start_sec:.3f}",
        'end_str': f"{end_sec:.3f}",
        'original_start': start_sec,
        'original_end': end_sec,
        'adjusted': False,
        'crop_center': None,
        'scaled_detections': None,
        'overlay_text': None,
        'subtitle_lines': subtitle_lines,
        'show_labels': False,
        'is_intro': True
    }]


def escape_drawtext(text: str) -> str:
    """Escape text for ffmpeg drawtext filter."""
    escaped = text.replace('\\', r'\\')
    escaped = escaped.replace(':', r'\:')
    escaped = escaped.replace("'", r"\'")
    escaped = escaped.replace("%", r"\%")
    escaped = escaped.replace("\n", "\\n")
    return escaped


def escape_drawtext_path(path: str) -> str:
    """Escape file path for ffmpeg drawtext arguments."""
    escaped = path.replace('\\', r'\\')
    escaped = escaped.replace(':', r'\:')
    escaped = escaped.replace("'", r"\'")
    return escaped


def escape_ffmpeg_expr(expr: str) -> str:
    """Escape commas in ffmpeg expressions to avoid filtergraph parsing issues."""
    return expr.replace(',', r'\,')


def get_video_framerate(video_path: str) -> float:
    """Get video framerate using ffprobe."""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=r_frame_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            # Parse fraction like "24000/1001" or "24/1"
            fps_str = result.stdout.strip()
            if '/' in fps_str:
                num, denom = fps_str.split('/')
                return float(num) / float(denom)
            else:
                return float(fps_str)
    except Exception as e:
        print(f"Warning: Could not get video framerate: {e}")
    return 24.0  # Default fallback


def get_video_dimensions(video_path: str) -> tuple:
    """Get video width and height using ffprobe."""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'csv=p=0',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            # iPhone files append an empty csv field ("1920,1080,") — a strict
            # 2-tuple unpack silently fell back to 1920x1080 for them
            vals = [v for v in result.stdout.strip().split(',') if v.strip()]
            return (int(vals[0]), int(vals[1]))
    except Exception as e:
        print(f"Warning: Could not get video dimensions ({video_path}): {e}")
    return (1920, 1080)  # Default fallback


def get_audio_samplerate(video_path: str) -> int:
    """Get audio sample rate using ffprobe.

    Sources without an audio stream (drone footage) make ffprobe exit 0 with
    EMPTY stdout — that is a normal condition, not an error: return the 48 kHz
    default quietly (silent clips get an anullsrc track at 48 kHz anyway)."""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=sample_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        raw = (result.stdout or "").strip()
        if result.returncode == 0 and raw:
            return int(raw)
        if result.returncode == 0:
            print("Source has no audio stream — using 48000 Hz for the mix (normal for drone footage)")
    except Exception as e:
        print(f"Warning: Could not get audio sample rate: {e}")
    return 48000  # Default fallback


def round_to_even(value: float) -> int:
    """Round a dimension to the nearest positive even integer for video encoding."""
    rounded = int(round(value))
    if rounded % 2 != 0:
        rounded += 1
    return max(2, rounded)


def parse_shot_scenes(shot_scenes_path: str, fps: float) -> List[float]:
    """
    Parse shot_scenes.txt and return list of scene cut timestamps in seconds.

    Args:
        shot_scenes_path: Path to shot_scenes.txt file
        fps: Video framerate

    Returns:
        List of timestamps (in seconds) where scene cuts occur
    """
    cut_points = []

    try:
        with open(shot_scenes_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = line.split()
                if len(parts) >= 2:
                    # Each line has start_frame and end_frame
                    # The end_frame of one scene is the cut point
                    end_frame = int(parts[1])
                    timestamp = end_frame / fps
                    cut_points.append(timestamp)
    except Exception as e:
        print(f"Warning: Could not parse shot_scenes.txt: {e}")
        return []

    # Sort and remove duplicates
    cut_points = sorted(set(cut_points))
    return cut_points


def adjust_clip_for_scene_cuts(
    start_sec: float,
    end_sec: float,
    cut_points: List[float],
    tolerance: float = 0.5
) -> tuple:
    """
    Adjust clip start time if there's a scene cut within the clip.

    If a scene cut point exists within the clip (excluding the exact start/end),
    snap the start time to the nearest cut point and maintain duration.

    Args:
        start_sec: Original start time in seconds
        end_sec: Original end time in seconds
        cut_points: List of scene cut timestamps
        tolerance: Minimum distance from start/end to consider a cut point (seconds)

    Returns:
        Tuple of (adjusted_start_sec, adjusted_end_sec)
    """
    if not cut_points:
        return start_sec, end_sec

    duration = end_sec - start_sec

    # Find cut points that are inside the clip (with tolerance)
    internal_cuts = [
        cp for cp in cut_points
        if start_sec + tolerance < cp < end_sec - tolerance
    ]

    if internal_cuts:
        # Snap to the first internal cut point
        new_start = internal_cuts[0]
        new_end = new_start + duration
        return new_start, new_end

    return start_sec, end_sec


def calculate_optimal_crop_center(
    protagonist_detection: Dict[str, Any],
    detection_width: int = None,
    detection_height: int = None,
    video_width: int = None,
    video_height: int = None
) -> tuple:
    """
    Calculate the optimal crop center based on protagonist bounding boxes.

    VLM bounding boxes are in 1000x1000 coordinate space and are scaled to
    actual video dimensions.

    Args:
        protagonist_detection: Dictionary containing frame_detections with bounding_box info
        detection_width: Unused, kept for API compatibility (VLM uses 1000x1000)
        detection_height: Unused, kept for API compatibility (VLM uses 1000x1000)
        video_width: Original video width
        video_height: Original video height

    Returns:
        Tuple of (center_x, center_y) in original video coordinates,
        or None if no valid detections found
    """
    if not protagonist_detection or 'frame_detections' not in protagonist_detection:
        return None

    valid_boxes = []
    for frame_det in protagonist_detection['frame_detections']:
        if frame_det.get('protagonist_detected') and frame_det.get('bounding_box'):
            bbox = frame_det['bounding_box']
            # Defensive: VLM may return bounding boxes with missing or malformed keys.
            try:
                bx = float(bbox.get('x', bbox.get('x1', 0)))
                by = float(bbox.get('y', bbox.get('y1', 0)))
                bw = float(bbox.get('width', 0))
                bh = float(bbox.get('height', 0))
            except (TypeError, ValueError):
                continue
            if bw <= 0 or bh <= 0:
                continue
            center_x = bx + bw / 2
            center_y = by + bh / 2
            valid_boxes.append((center_x, center_y, bw, bh))

    if not valid_boxes:
        return None

    # Weighted average center (larger boxes have more weight)
    total_weight = 0
    weighted_x = 0
    weighted_y = 0
    for center_x, center_y, width, height in valid_boxes:
        weight = width * height
        weighted_x += center_x * weight
        weighted_y += center_y * weight
        total_weight += weight

    if total_weight == 0:
        return None

    avg_x = weighted_x / total_weight
    avg_y = weighted_y / total_weight

    # VLM uses 1000x1000 coordinate space — scale to actual video dimensions
    if video_width and video_height:
        avg_x = avg_x * video_width / 1000
        avg_y = avg_y * video_height / 1000

    return (avg_x, avg_y)


def extract_all_clips(
    shot_data: List[Dict[str, Any]],
    cut_points: List[float] = None,
    video_width: int = None,
    video_height: int = None,
) -> List[Dict[str, Any]]:
    """
    Extract all clips from shot_point.json in order.

    Args:
        shot_data: List of shot data from shot_point.json
        cut_points: Optional list of scene cut timestamps for adjustment
        video_width: Original video width (for scaling detection coordinates)
        video_height: Original video height (for scaling detection coordinates)

    Returns:
        A flat list of clips with start/end times in seconds.
    """
    all_clips = []

    # VLM returns bounding boxes in 1000x1000 coordinate space
    detection_width = 1000
    detection_height = 1000
    scale_x = 1.0
    scale_y = 1.0
    if video_width and video_height:
        scale_x = video_width / detection_width
        scale_y = video_height / detection_height
        print(f"Scaling bounding boxes from {detection_width}x{detection_height} to {video_width}x{video_height} (scale: {scale_x:.3f}x, {scale_y:.3f}y)")

    # Sort by section_idx, then shot_idx to ensure correct order
    sorted_data = sorted(shot_data, key=lambda x: (x.get('section_idx', 0), x.get('shot_idx', 0)))

    for shot in sorted_data:
        if shot.get('status') != 'success':
            continue

        section_idx = shot.get('section_idx', -1)
        shot_idx = shot.get('shot_idx', -1)

        # Calculate optimal crop center from protagonist detection
        crop_center = None
        if 'protagonist_detection' in shot:
            crop_center = calculate_optimal_crop_center(
                shot['protagonist_detection'],
                detection_width=detection_width,
                detection_height=detection_height,
                video_width=video_width,
                video_height=video_height
            )

        # Scale bounding boxes to video coordinates for visualization
        scaled_detections = None
        if 'protagonist_detection' in shot and shot['protagonist_detection'].get('frame_detections'):
            scaled_detections = []
            for frame_det in shot['protagonist_detection']['frame_detections']:
                if frame_det.get('protagonist_detected') and frame_det.get('bounding_box'):
                    bbox = frame_det['bounding_box']
                    try:
                        bx = float(bbox.get('x', bbox.get('x1', 0)))
                        by = float(bbox.get('y', bbox.get('y1', 0)))
                        bw = float(bbox.get('width', 0))
                        bh = float(bbox.get('height', 0))
                    except (TypeError, ValueError):
                        continue
                    if bw <= 0 or bh <= 0:
                        continue
                    scaled_bbox = {
                        'x': int(bx * scale_x),
                        'y': int(by * scale_y),
                        'width': int(bw * scale_x),
                        'height': int(bh * scale_y)
                    }
                    scaled_detections.append({
                        'time_sec': frame_det['time_sec'],
                        'bounding_box': scaled_bbox
                    })

        for clip in shot.get('clips', []):
            start_sec = hhmmss_to_seconds(clip['start'])
            end_sec = hhmmss_to_seconds(clip['end'])

            # Adjust for scene cuts if cut_points provided
            original_start = start_sec
            original_end = end_sec
            if cut_points:
                start_sec, end_sec = adjust_clip_for_scene_cuts(start_sec, end_sec, cut_points)

            # Convert back to string format if adjusted
            def sec_to_hhmmss(sec: float) -> str:
                h = int(sec // 3600)
                m = int((sec % 3600) // 60)
                s = sec % 60
                return f"{h:02d}:{m:02d}:{s:06.3f}"

            all_clips.append({
                'section_idx': section_idx,
                'shot_idx': shot_idx,
                'start_sec': start_sec,
                'end_sec': end_sec,
                'duration': end_sec - start_sec,
                'start_str': sec_to_hhmmss(start_sec),
                'end_str': sec_to_hhmmss(end_sec),
                'original_start': original_start,
                'original_end': original_end,
                'adjusted': (start_sec != original_start or end_sec != original_end),
                'crop_center': crop_center,
                'scaled_detections': scaled_detections,
                'video_path': clip.get('video_path', shot.get('video_path', '')),
            })

    return all_clips


def _ai_pick_transitions(clips, shot_plan) -> list:
    """Ask the agent LLM to choose a transition per cut, based on the outgoing/
    incoming shot descriptions from the shot_plan. Returns a list of
    "name:duration" strings (len = n-1), or None when there are no boundaries.

    Never falls back to bare hard cuts: unknown picks, missing entries and a
    failed LLM call all become the soft fallback (TRANSITION_FALLBACK,
    default fade:0.4) — a mellow memory montage must not degrade to a
    transition-less slideshow of jumps."""
    main_clips = [c for c in clips if not c.get("is_ending") and not c.get("is_intro")]
    if len(main_clips) < 2:
        return None
    _fb = "fade:0.4"
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import src.utils.litellm_local  # noqa: F401  — local cost map, before litellm
        import src.config as config
        import litellm

        _fb = str(getattr(config, "TRANSITION_FALLBACK", _fb) or _fb).strip().lower()

        # per-shot descriptions keyed by (section_idx, shot_idx)
        plan_shots = {}
        for si, sec in enumerate((shot_plan or {}).get("video_structure", []) or []):
            for hi, sh in enumerate((sec.get("shot_plan") or {}).get("shots", []) or []):
                plan_shots[(si, hi)] = sh
        theme = ((shot_plan or {}).get("video_structure") or [{}])[0].get("overall_theme", "")

        boundaries = []
        for k in range(1, len(main_clips)):
            a = plan_shots.get((main_clips[k - 1].get("section_idx", 0), main_clips[k - 1].get("shot_idx", 0)), {})
            b = plan_shots.get((main_clips[k].get("section_idx", 0), main_clips[k].get("shot_idx", 0)), {})
            boundaries.append({
                "cut_index": k,
                "outgoing": {"content": a.get("content", ""), "emotion": a.get("emotion", ""),
                             "visual_beat": a.get("visual_beat", ""),
                             "camera_motion": (a.get("camera_motion") or {}).get("type", "")},
                "incoming": {"content": b.get("content", ""), "emotion": b.get("emotion", ""),
                             "visual_beat": b.get("visual_beat", ""),
                             "camera_motion": (b.get("camera_motion") or {}).get("type", "")},
            })

        palette_desc = "\n".join(f"- {k}: {v}" for k, v in TRANSITION_PALETTE.items())
        prompt = (
            "You are a seasoned editor choosing the transition for EACH cut of a cinematic "
            "travel-memory montage: mellow and emotional — scenery, ambient moments, candid "
            "voices and laughter, cut to music. This is a memory reel, NOT a fast beat-drop MV.\n\n"
            f"Overall theme: {theme}\n\n"
            "Available transitions (name: when to use):\n"
            "- cut: instant hard cut — right ON a strong musical accent, or when the two shots "
            "contrast clearly (wide→close, scenery→people, motion→still)\n"
            f"{palette_desc}\n\n"
            "Craft rules (emotion first — pick what the moment feels like):\n"
            "- Between two calm, similar shots (scenery→scenery, drone→drone) a bare cut feels "
            "abrupt — prefer fade / dissolve / hblur there. In a mellow montage, soft transitions "
            "are the workhorse and will usually be the majority.\n"
            "- Choose cut only where the music clearly accents the boundary or the incoming shot "
            "is a deliberate energy spike.\n"
            "- fadeblack: only for a chapter-level reset or right before the finale (1-2 max).\n"
            "- fadewhite: only at a bright euphoric peak (1 max).\n"
            "- Stylized accents (zoomin, radial, circleopen, hlslice, distance, smoothleft/right): "
            "1-2 in the WHOLE video, never on quiet moments.\n"
            "- Never use the same non-cut transition twice in a row.\n"
            "- MOTION MATCHING: camera_motion gives each side's measured move. When both sides "
            "continue the same direction (pan_left → pan_left, push_in → push_in) the flow is "
            "already seamless — a plain cut or gentle fade preserves it. smoothleft/smoothright "
            "may ONLY move WITH the shots' shared direction, never against it. Between two "
            "static/calm shots, fade or dissolve carries the stillness.\n"
            "- duration: 0.3-0.6 seconds (0.5-0.6 for calm dissolves, 0.3-0.4 for accents).\n\n"
            f"Cuts to decide (between consecutive shots):\n{json.dumps(boundaries, ensure_ascii=False, indent=1)}\n\n"
            'Reply with ONLY a JSON array, one entry per cut, e.g.: '
            '["fade:0.5", "cut", "dissolve:0.5", "fadeblack:0.4"]'
        )
        def _ask(model, api_base, api_key):
            kwargs = dict(model=model,
                          messages=[{"role": "user", "content": prompt}],
                          # reasoning models spend tokens thinking BEFORE the
                          # reply; a small cap truncates the answer to empty
                          temperature=0.4, max_tokens=8000, timeout=90)
            if api_base:
                kwargs["api_base"] = api_base
            if api_key:
                kwargs["api_key"] = api_key
            resp = litellm.completion(**kwargs)
            msg = resp.choices[0].message
            content = (msg.content or "").strip()
            if not content:  # some reasoning models leave the array in the thinking text
                content = str(getattr(msg, "reasoning_content", "") or "").strip()
            if content.startswith("```"):
                content = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", content)
            m = re.search(r"\[[^\[\]]*\]", content, re.DOTALL)
            parsed = json.loads(m.group(0) if m else content)
            return parsed if isinstance(parsed, list) else None

        # primary: the agent model; fallback: the cheap flash model (empty
        # replies happen when a reasoning model burns its budget thinking)
        candidates = [
            (config.AGENT_LITELLM_MODEL,
             getattr(config, "AGENT_LITELLM_URL", ""),
             getattr(config, "AGENT_LITELLM_API_KEY", "")),
            (getattr(config, "TRANSLATE_MODEL", ""),
             getattr(config, "TRANSLATE_ENDPOINT", ""),
             getattr(config, "TRANSLATE_API_KEY", "")),
        ]
        arr = None
        for _model, _base, _key in candidates:
            if not _model:
                continue
            try:
                arr = _ask(_model, _base, _key)
                if arr:
                    break
            except Exception as _e:  # noqa: BLE001
                print(f"⚠️ transition pick via {_model} failed ({str(_e)[:120]}) — trying fallback")
        if not arr:
            raise RuntimeError("no usable reply from any model")
        out = []
        for item in arr[: len(main_clips) - 1]:
            s = str(item).strip().lower()
            name = s.split(":")[0]
            out.append(s if (name == "cut" or name in TRANSITION_PALETTE) else _fb)
        while len(out) < len(main_clips) - 1:
            out.append(_fb)
        print(f"🎞️  [AI transitions] {out}")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ AI transition selection failed ({e}) — falling back to uniform {_fb}")
        return [_fb] * (len(main_clips) - 1)


# Curated xfade transitions with editorial intent — the AI picker (and any
# manual caller) may only choose from these. All are built into ffmpeg's xfade
# filter, no custom build needed.
TRANSITION_PALETTE = {
    "fade":       "classic crossfade — gentle, safe default for lyrical moments",
    "fadeblack":  "dip to black — chapter break, dramatic reset, big scene change",
    "fadewhite":  "dip to white — dreamy, bright, euphoric peak",
    "dissolve":   "grainy organic dissolve — nostalgic, textured",
    "zoomin":     "punch zoom-in — energy spike, beat accent",
    "radial":     "clock-sweep reveal — stylish, rhythmic",
    "circleopen": "iris reveal — playful, spotlight on a new subject",
    "hblur":      "blur-through — dreamy premium feel, time passing",
    "smoothleft": "smooth directional wipe left — continues leftward motion",
    "smoothright": "smooth directional wipe right — continues rightward motion",
    "hlslice":    "sliced wipe — glitchy high-energy accent",
    "distance":   "stretch morph — abstract premium accent (use sparingly)",
}


# ── Cinematic looks — pure ffmpeg filter chains, no external LUT files ──────
COLOR_GRADES = {
    # 青橙电影感: shadows toward teal, highlights toward orange, gentle punch
    "teal_orange": ("colorbalance=rm=0.05:bm=-0.04:rs=-0.06:bs=0.08,"
                    "eq=saturation=1.06:contrast=1.05:brightness=0.01"),
    # 胶片柔和: lifted blacks, rolled-off highlights, muted saturation
    "film": ("curves=master='0/0.03 0.5/0.52 1/0.96',"
             "eq=saturation=0.9:contrast=1.06"),
    # 暖阳回忆: warmer temperature, light glow
    "warm": "colortemperature=temperature=5400,eq=saturation=1.03:contrast=1.03",
}

# 2.35:1 cinematic letterbox INSIDE a 16:9 frame (platform-friendly container)
LETTERBOX_FILTER = ("crop=iw:floor(iw/2.35/2)*2,"
                    "pad=iw:floor(iw*9/16/2)*2:0:(oh-ih)/2:black")

_HAS_AUDIO_CACHE: dict = {}
_DIMS_CACHE: dict = {}


def _dims_cached(path: str) -> tuple:
    """Per-source width/height (multi-source renders mix 4K drone + 1080p phone)."""
    if path not in _DIMS_CACHE:
        _DIMS_CACHE[path] = get_video_dimensions(path)
    return _DIMS_CACHE[path]


_NVENC_OK = None


def _video_codec_args() -> list:
    """['-c:v', encoder, quality args…] — NVENC (GPU) when the driver accepts
    a session, else libx264. Probed once per process with a REAL tiny encode:
    the encoder being listed proves nothing, the driver must open a session
    (this exact mismatch cost a driver-version hunt: API 13.1 vs 13.0)."""
    global _NVENC_OK
    if _NVENC_OK is None:
        try:
            r = subprocess.run(
                ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 'testsrc=duration=0.1:size=320x180:rate=30',
                 '-c:v', 'h264_nvenc', '-f', 'null', '-'],
                capture_output=True, timeout=20)
            _NVENC_OK = (r.returncode == 0)
        except Exception:  # noqa: BLE001
            _NVENC_OK = False
        print(f"🎛️  Video encoder: {'h264_nvenc (GPU)' if _NVENC_OK else 'libx264 (CPU)'}")
    if _NVENC_OK:
        # p5 + vbr cq19 ≈ libx264 crf18 visually; -b:v 0 lets cq govern.
        # profile high matches what libx264 emitted before (concat-friendly).
        return ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr',
                '-cq', '19', '-b:v', '0', '-profile:v', 'high']
    return ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18']


def _source_has_audio(path: str) -> bool:
    """Whether a media file has an audio stream (drone footage often doesn't)."""
    if path in _HAS_AUDIO_CACHE:
        return _HAS_AUDIO_CACHE[path]
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20)
        ok = "audio" in (r.stdout or "")
    except Exception:  # noqa: BLE001
        ok = False
    _HAS_AUDIO_CACHE[path] = ok
    return ok


def _load_sound_highlights_for_source(video_path: str) -> list:
    """Voice/laughter segments for a source video, from its analysis cache.

    Located by file name (same mapping the web clip-map uses). If the analysis
    dir exists but has no highlights yet (older annotation), they are computed
    once here and cached there."""
    try:
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        analyzed = os.path.join(_root, "Output", "analyzed")
        base = os.path.basename(video_path or "")
        if not base or not os.path.isdir(analyzed) or not os.path.exists(video_path):
            return []
        import glob as _g
        for md in _g.glob(os.path.join(analyzed, "*", "metadata.json")):
            try:
                with open(md, "r", encoding="utf-8") as f:
                    if json.load(f).get("file_name") != base:
                        continue
            except Exception:  # noqa: BLE001
                continue
            if _root not in sys.path:
                sys.path.insert(0, _root)
            from src.audio.sound_highlights import detect_sound_highlights
            return detect_sound_highlights(
                video_path,
                cache_path=os.path.join(os.path.dirname(md), "sound_highlights.json"))
    except Exception as e:  # noqa: BLE001
        print(f"(sound-highlight lookup failed for {os.path.basename(video_path or '')}: {e})")
    return []


def _boundary_durations(clips, transitions, transition_duration) -> list:
    """Per-boundary visual overlap seconds (0 for hard cuts), len = n-1.
    Must mirror _build_video_join_cmd's interpretation exactly."""
    n = len(clips)
    T = float(transition_duration or 0.0)
    out = []
    for k in range(n - 1):
        d = 0.0
        if transitions:
            item = transitions[k] if k < len(transitions) else "cut"
            if isinstance(item, dict):
                name = str(item.get("transition", "cut")).strip() or "cut"
                d = float(item.get("duration", 0.4) or 0.4) if name != "cut" else 0.0
            else:
                s = str(item).strip()
                name, _, dd = s.partition(":")
                if name and name != "cut":
                    try:
                        d = float(dd) if dd else 0.4
                    except ValueError:
                        d = 0.4
        elif T > 0:
            d = T
        out.append(d)
    return out


def _compute_duck_windows(clips, transitions, transition_duration) -> list:
    """Output-timeline windows where a clip's ORIGINAL audio carries voices —
    the BGM ducks there and the real sound plays through. Mirrors the xfade
    timeline math so windows stay aligned when transitions shorten the video."""
    bdur = _boundary_durations(clips, transitions, transition_duration)
    pos = []
    S = 0.0
    for k, c in enumerate(clips):
        pos.append(S)
        S += float(c.get("duration", 0.0)) - (bdur[k] if k < len(bdur) else 0.0)

    windows = []
    hl_cache: dict = {}
    for k, c in enumerate(clips):
        if c.get("is_ending") or c.get("is_intro"):
            continue
        src = c.get("video_path") or ""
        if src not in hl_cache:
            hl_cache[src] = _load_sound_highlights_for_source(src)
        if not hl_cache[src]:
            continue
        s0, e0 = float(c.get("start_sec", 0.0)), float(c.get("end_sec", 0.0))
        clip_out_s = pos[k]
        clip_out_e = pos[k] + max(0.0, float(c.get("duration", e0 - s0)))
        for h in hl_cache[src]:
            ov_s = max(s0, float(h.get("start", 0)))
            ov_e = min(e0, float(h.get("end", 0)))
            # sub-second voice slivers aren't worth a duck dip — feels like a
            # glitch, not a moment (user feedback)
            if ov_e - ov_s < 1.0:
                continue
            # breathing room: open the duck slightly before the first word and
            # close slightly after the last, clamped to this clip's span
            w_s = max(clip_out_s, pos[k] + (ov_s - s0) - 0.4)
            w_e = min(clip_out_e, pos[k] + (ov_e - s0) + 0.4)
            windows.append([round(w_s, 2), round(w_e, 2)])
    windows.sort()
    merged: list = []
    for a, b in windows:
        if merged and a - merged[-1][1] < 0.4:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _build_video_join_cmd(concat_file, clip_files, clips, out_path, video_fps, transition_duration,
                          transitions=None, with_audio=False):
    """ffmpeg command to join the per-clip files into one video.

    Default: hard-cut concat demuxer (stream copy) — unchanged behavior.
    transition_duration > 0: crossfade (dissolve) between clips via chained xfade.
    Note: xfade overlaps adjacent clips, so the total shortens by ~duration per cut
    and cut points drift slightly off the beat. (Beat-exact transitions would need
    each clip to grab an extra tail; kept out of v1 to avoid touching extraction.)
    xfade emits video only — the BGM step maps 0:v:0 so that's fine for music-only.
    """
    T = float(transition_duration or 0.0)
    n = len(clip_files)

    # Normalize per-boundary spec: list of (name, duration), len n-1.
    # name "cut" → hard cut at that boundary. None → uniform behavior.
    boundary: list = []
    if transitions:
        for k in range(n - 1):
            item = transitions[k] if k < len(transitions) else "cut"
            if isinstance(item, dict):
                name = str(item.get("transition", "cut")).strip() or "cut"
                dur = float(item.get("duration", 0.4) or 0.4)
            else:
                s = str(item).strip()
                if ":" in s:
                    name, _, d = s.partition(":")
                    try:
                        dur = float(d)
                    except ValueError:
                        dur = 0.4
                else:
                    name, dur = s, 0.4
            name = name.strip().lower() or "cut"
            if name != "cut" and name not in TRANSITION_PALETTE:
                name = "fade"          # unknown → safe default
            boundary.append((name, max(0.2, min(dur, 1.0))))
    elif T > 0:
        boundary = [("fade", T)] * (n - 1)

    if boundary and n > 1 and any(name != "cut" for name, _ in boundary):
        inputs = []
        for cf in clip_files:
            inputs += ['-i', cf]
        # settb=AVTB unifies every stream's timebase: the concat filter outputs
        # 1/1000000 while raw h264 inputs use e.g. 1/12800, and xfade hard-fails
        # when its two inputs disagree ("timebase do not match").
        parts = [f"[{i}:v]settb=AVTB[v{i}]" for i in range(n)]
        prev = '[v0]'
        S = float(clips[0].get('duration', 0) or 0)   # running duration of the joined stream
        for k in range(1, n):
            name, dur = boundary[k - 1]
            dk = float(clips[k].get('duration', 0) or 0)
            out = '[vout]' if k == n - 1 else f'[vx{k}]'
            if name == "cut":
                parts.append(f"{prev}[v{k}]concat=n=2:v=1:a=0{out}")
                S += dk
            else:
                offset = max(0.05, S - dur)
                parts.append(f"{prev}[v{k}]xfade=transition={name}:duration={dur}:offset={offset:.4f}{out}")
                S += dk - dur
            prev = out
        maps = ['-map', '[vout]']
        acodec = []
        if with_audio:
            # audio timeline must match the xfade-shortened video: trim each
            # clip's audio tail by its boundary's overlap, then hard-concat.
            # Clips are extracted with a guaranteed audio stream (silent
            # sources get anullsrc), so [k:a] always exists.
            for k in range(n):
                d_next = 0.0
                if k < n - 1:
                    b_name, b_dur = boundary[k]
                    d_next = b_dur if b_name != "cut" else 0.0
                adur = max(0.1, float(clips[k].get('duration', 0) or 0) - d_next)
                parts.append(f"[{k}:a]atrim=duration={adur:.4f},asetpts=PTS-STARTPTS[ax{k}]")
            parts.append("".join(f"[ax{k}]" for k in range(n)) + f"concat=n={n}:v=0:a=1[aout_src]")
            maps += ['-map', '[aout_src]']
            acodec = ['-c:a', 'aac', '-ar', '48000']
        return [
            'ffmpeg', '-y', *inputs,
            '-filter_complex', ";".join(parts),
            *maps,
            *_video_codec_args(), '-pix_fmt', 'yuv420p',
            *acodec,
            '-r', str(video_fps), out_path,
        ]
    # default: fast hard-cut concat
    return ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_file, '-c', 'copy', out_path]


def render_video_ffmpeg(
    video_path: str,
    clips: List[Dict[str, Any]],
    output_path: str,
    audio_path: str = None,
    audio_start_time: float = None,
    audio_duration: float = None,
    verbose: bool = False,
    show_labels: bool = True,
    label_position: str = "top-left",
    font_size: int = 32,
    font_color: str = "white",
    bg_color: str = "black@0.6",
    crop_ratio: str = None,
    original_audio_volume: float = 0.0,
    video_width: int = None,
    video_height: int = None,
    visualize_detections: bool = False,
    dialogue_font_file: str = None,
    dialogue_font_size: int = 48,
    dialogue_font_color: str = "white",
    dialogue_box_color: str = "black@0.6",
    dialogue_y_position: str = "bottom",
    bgm_dialogue_volume: float = 0.5,
    ending_duration: float = 0.0,
    ending_fade_target: float = 0.0,
    hook_dialogue_duration: float = 0.0,
    auto_loudness_match: bool = True,
    target_lufs: float = -18.0,
    target_lra: float = 11.0,
    target_tp: float = -1.5,
    transition_duration: float = 0.0,
    transitions: list = None,
    duck_windows: list = None,
    color_grade: str = "",
    letterbox: bool = False,
    fades: bool = False,
) -> bool:
    """
    Render video clips using ffmpeg concat demuxer.

    Args:
        video_path: Path to source video file
        clips: List of clip dictionaries with start_sec and end_sec
        output_path: Path for output video
        audio_path: Optional path to audio file to mix with video
        audio_start_time: Optional start time (in seconds) to crop audio from
        audio_duration: Optional duration (in seconds) to crop audio to
        verbose: Print ffmpeg output
        show_labels: Whether to overlay Section/Shot labels on video
        label_position: Position of labels ("top-left", "top-right", "bottom-left", "bottom-right")
        font_size: Font size for labels
        font_color: Font color for labels
        bg_color: Background color for label box (with opacity, e.g., "black@0.6")
        crop_ratio: Optional aspect ratio for center cropping (e.g., "9:16", "16:9", "1:1").
                   Keeps height unchanged and crops width to match the ratio.
                   If clips have crop_center info, uses dynamic crop centers instead of fixed center.
        original_audio_volume: Volume level for original video audio (0.0 or higher).
                              0.0 = muted (default), 1.0 = full volume, >1.0 = amplified.
                              Only applies when mixing with external audio.
        auto_loudness_match: Auto-match loudness between original audio and BGM before scaling.
        target_lufs: Loudness target (LUFS) used by ffmpeg loudnorm.
        target_lra: Loudness range target used by ffmpeg loudnorm.
        target_tp: True peak target (dBTP) used by ffmpeg loudnorm.
        video_width: Video width in pixels (auto-detected if None)
        video_height: Video height in pixels (auto-detected if None)
        visualize_detections: Whether to draw bounding boxes and crop area on video

    Returns:
        True if successful, False otherwise
    """
    if not clips:
        print("Error: No clips to render")
        return False

    # Get video dimensions if not provided
    if video_width is None or video_height is None:
        video_width, video_height = get_video_dimensions(video_path)
        print(f"Detected video dimensions: {video_width}x{video_height}")
    else:
        print(f"Using provided video dimensions: {video_width}x{video_height}")

    video_fps = get_video_framerate(video_path)
    audio_ar = get_audio_samplerate(video_path)
    print(f"Detected audio sample rate: {audio_ar} Hz")
    # Cap AAC sample rate at 48 kHz: a 96 kHz music source propagates into
    # 96 kHz AAC output, which Windows Media Player / Films & TV cannot open
    # ("unsupported encoding settings") even though browsers play it fine.
    if audio_ar > 48000:
        print(f"  → capping output sample rate {audio_ar} Hz → 48000 Hz (player compatibility)")
        audio_ar = 48000

    # Determine label position coordinates
    position_map = {
        "top-left": ("10", "10"),
        "top-right": ("w-tw-10", "10"),
        "bottom-left": ("10", "h-th-10"),
        "bottom-right": ("w-tw-10", "h-th-10"),
    }
    label_x, label_y = position_map.get(label_position, ("10", "10"))

    # Determine dialogue y position expression
    _dialogue_y_map = {
        "bottom": "h-text_h-40",
        "top": "40",
        "center": "(h-text_h)/2",
    }
    dialogue_y_expr = _dialogue_y_map.get(dialogue_y_position, dialogue_y_position)

    # Parse crop ratio if provided
    crop_w_ratio = None
    crop_h_ratio = None
    if crop_ratio:
        try:
            parts = crop_ratio.split(':')
            if len(parts) == 2:
                crop_w_ratio, crop_h_ratio = int(parts[0]), int(parts[1])
                print(f"Crop ratio: {crop_ratio} (dynamic crop centers will be used if available)")
            else:
                print(f"Warning: Invalid crop ratio format '{crop_ratio}', expected format like '9:16'. Ignoring.")
        except ValueError:
            print(f"Warning: Could not parse crop ratio '{crop_ratio}'. Ignoring.")

    # Common output size ALL clips are normalized to (concat demuxer stream-copies,
    # so every clip must decode to identical dimensions). Derived from the primary
    # video; per-clip crops are computed in each SOURCE's own pixel space below.
    if crop_w_ratio and crop_h_ratio:
        _r_out = crop_w_ratio / crop_h_ratio
        target_w = round_to_even(min(video_width, video_height * _r_out))
        target_h = round_to_even(min(video_height, video_width / _r_out))
    else:
        target_w, target_h = video_width, video_height

    # Create temporary directory for intermediate files
    with tempfile.TemporaryDirectory() as temp_dir:
        clip_files = []

        # Extract each clip to a temporary file
        print(f"Extracting {len(clips)} clips...")
        # last MAIN clip gets the fade-to-black ending (never the outro card)
        _last_main_idx = max(
            (idx for idx, c in enumerate(clips)
             if not c.get('is_ending') and not c.get('is_intro')), default=-1)
        for i, clip in enumerate(clips):
            clip_file = os.path.join(temp_dir, f"clip_{i:04d}.mp4")
            clip_files.append(clip_file)

            duration = clip['duration']
            section_idx = clip.get('section_idx', 0)
            shot_idx = clip.get('shot_idx', 0)
            show_labels_for_clip = clip.get('show_labels', show_labels)
            overlay_text = clip.get('overlay_text')
            subtitle_lines = clip.get('subtitle_lines') or []

            # Ending clips are identified by the explicit is_ending flag — NOT by
            # having a video_path. In multi-source mode EVERY clip carries its own
            # video_path; keying on it sent all clips down this branch, which has
            # no -ss (always extracts from 0s). That silently replaced every clip
            # with the first `duration` seconds of its source: "只取开头几秒" +
            # identical duplicate clips whenever two picks shared a source.
            ending_video_path = clip.get('video_path') if clip.get('is_ending') else None
            if ending_video_path:
                # Transcode ending video to match main video format exactly.
                # If crop_ratio is set, scale+pad to the cropped output size.
                if crop_w_ratio and crop_h_ratio:
                    ending_out_w = round_to_even(video_height * crop_w_ratio / crop_h_ratio)
                    ending_out_h = video_height
                else:
                    ending_out_w = video_width
                    ending_out_h = video_height
                ending_filter = (
                    f"scale='if(gt(a,{ending_out_w}/{ending_out_h}),{ending_out_w},-2)':"
                    f"'if(gt(a,{ending_out_w}/{ending_out_h}),-2,{ending_out_h})',"
                    f"pad={ending_out_w}:{ending_out_h}:(ow-iw)/2:(oh-ih)/2:black,"
                    "format=yuv420p"
                )
                cmd = [
                    'ffmpeg',
                    '-y',
                    '-i', ending_video_path,
                    '-f', 'lavfi',
                    '-i', f'anullsrc=channel_layout=stereo:sample_rate={audio_ar}',
                    '-vf', ending_filter,
                    '-r', str(video_fps),
                    *_video_codec_args(),
                    '-c:a', 'aac',
                    '-ar', str(audio_ar),
                    '-ac', '2',
                    '-t', str(duration),
                    clip_file
                ]
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE if not verbose else None,
                    stderr=subprocess.PIPE if not verbose else None
                )
                if result.returncode != 0:
                    print(f"Error creating ending clip from video: {ending_video_path}")
                    if not verbose and result.stderr:
                        print(result.stderr.decode()[-500:])
                    return False
                # Update clip duration to actual extracted duration (fps conversion may change it)
                probe = subprocess.run(
                    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', clip_file],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                if probe.stdout.strip():
                    actual_duration = float(probe.stdout.strip())
                    if abs(actual_duration - duration) > 0.01:
                        print(f"Ending clip duration adjusted: {duration:.4f}s -> {actual_duration:.4f}s")
                    clip['duration'] = actual_duration
                continue

            start = clip['start_sec']
            source_video = clip.get('video_path') or video_path

            # Multi-source: crop in THIS source's pixel space, then normalize to
            # the common output size. Using the primary video's dimensions for
            # every clip put a 4K crop window on 1080p phone footage — the crop
            # overran the frame, the filter chain failed, and libx264 died with
            # EINVAL before writing a single packet.
            src_w, src_h = _dims_cached(source_video)

            # Generate crop filter for this clip (if crop ratio is provided)
            crop_filter = None
            crop_x_px = None
            crop_width_px = None
            if crop_w_ratio and crop_h_ratio:
                # Largest crop of the target aspect that fits inside THIS source
                _r = crop_w_ratio / crop_h_ratio
                crop_width_px = round_to_even(min(src_w, src_h * _r))
                crop_height_px = round_to_even(min(src_h, src_w / _r))
                crop_y_px = max(0, int((src_h - crop_height_px) / 2))

                # Check if this clip has a crop_center from protagonist detection
                crop_center = clip.get('crop_center')
                if crop_center:
                    # Use dynamic crop center based on protagonist position
                    center_x, center_y = crop_center
                    crop_x_px = int(center_x - crop_width_px / 2)
                    crop_x_px = max(0, min(crop_x_px, src_w - crop_width_px))
                    if verbose:
                        print(f"  Clip {i}: Using dynamic crop center at ({center_x:.1f}, {center_y:.1f}) -> crop_x={crop_x_px}")
                else:
                    # No crop center info, use default center crop
                    crop_x_px = max(0, int((src_w - crop_width_px) / 2))
                crop_filter = f"crop={crop_width_px}:{crop_height_px}:{crop_x_px}:{crop_y_px}"
                if (crop_width_px, crop_height_px) != (target_w, target_h):
                    crop_filter += f",scale={target_w}:{target_h},setsar=1"
            elif (src_w, src_h) != (target_w, target_h):
                # No crop requested but this source differs from the primary —
                # letterbox it into the common frame so concat stays homogeneous
                crop_filter = (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                               f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1")

            # Build visualization filters if requested
            viz_filters = []
            if visualize_detections:
                det_display_duration = max(2.0 / video_fps, 0.06)
                # Draw crop area boundary (before cropping)
                if crop_x_px is not None and crop_width_px is not None:
                    # Draw crop area as a green rectangle
                    viz_filters.append(
                        f"drawbox=x={crop_x_px}:y=0:w={crop_width_px}:h={video_height}:color=green@0.3:t=fill"
                    )
                    # Draw crop area border
                    viz_filters.append(
                        f"drawbox=x={crop_x_px}:y=0:w={crop_width_px}:h={video_height}:color=green:t=4"
                    )

                # Draw crop center point
                crop_center = clip.get('crop_center')
                if crop_center:
                    center_x, center_y = crop_center
                    # Draw a crosshair at crop center
                    viz_filters.append(
                        f"drawbox=x={int(center_x-10)}:y={int(center_y)}:w=20:h=1:color=red:t=fill"
                    )
                    viz_filters.append(
                        f"drawbox=x={int(center_x)}:y={int(center_y-10)}:w=1:h=20:color=red:t=fill"
                    )

                # Draw bounding boxes for all detected frames in this clip
                scaled_detections = clip.get('scaled_detections', [])
                if scaled_detections:
                    for det in scaled_detections:
                        det_time_abs = det.get('time_sec')
                        if det_time_abs is None:
                            continue

                        det_start = det_time_abs - start
                        det_end = det_start + det_display_duration

                        if det_end <= 0 or det_start >= duration:
                            continue

                        det_start = max(0.0, det_start)
                        det_end = min(duration, det_end)

                        bbox = det.get('bounding_box', {})
                        if not all(k in bbox for k in ('x', 'y', 'width', 'height')):
                            continue
                        viz_filters.append(
                            f"drawbox=x={bbox['x']}:y={bbox['y']}:w={bbox['width']}:h={bbox['height']}:color=yellow:t=3:enable='between(t,{det_start:.3f},{det_end:.3f})'"
                        )

            # Build ffmpeg command
            if show_labels_for_clip:
                # Create label text
                label_text = f"Shot {shot_idx + 1}"

                # Use drawtext filter to overlay label
                # Note: Need to escape special characters for ffmpeg filter
                # fontfile is required - try common system fonts
                drawtext_filter = (
                    f"drawtext=text='{label_text}':"
                    f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
                    f"fontsize={font_size}:"
                    f"fontcolor={font_color}:"
                    f"x={label_x}:y={label_y}:"
                    f"box=1:boxcolor={bg_color}:boxborderw=8"
                )

                # Combine filters: viz first, then crop (if enabled), then drawtext
                filter_chain = []
                if viz_filters:
                    filter_chain.extend(viz_filters)
                if crop_filter:
                    filter_chain.append(crop_filter)
                if subtitle_lines:
                    dialogue_font = dialogue_font_file or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    out_w = crop_width_px if crop_width_px else video_width
                    max_chars = max(10, int(out_w / (dialogue_font_size * 0.6)))
                    for j, sl in enumerate(subtitle_lines):
                        sl_text_path = os.path.join(temp_dir, f"dialogue_{i:04d}_{j:03d}.txt")
                        wrapped = "\n".join(textwrap.wrap(sl['text'], width=max_chars))
                        with open(sl_text_path, "w", encoding="utf-8") as tf:
                            tf.write(wrapped)
                        filter_chain.append(
                            f"drawtext=textfile='{escape_drawtext_path(sl_text_path)}':"
                            f"fontfile='{escape_drawtext_path(dialogue_font)}':"
                            f"fontsize={dialogue_font_size}:"
                            f"fontcolor={dialogue_font_color}:"
                            f"x=(w-text_w)/2:y={dialogue_y_expr}:"
                            f"box=1:boxcolor={dialogue_box_color}:boxborderw=12:"
                            f"enable='between(t,{sl['start']:.3f},{sl['end']:.3f})'"
                        )
                elif overlay_text:
                    dialogue_font = dialogue_font_file or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    dialogue_text_path = os.path.join(temp_dir, f"dialogue_{i:04d}.txt")
                    out_w = crop_width_px if crop_width_px else video_width
                    max_chars = max(10, int(out_w / (dialogue_font_size * 0.6)))
                    wrapped = "\n".join(textwrap.wrap(overlay_text, width=max_chars))
                    with open(dialogue_text_path, "w", encoding="utf-8") as text_file:
                        text_file.write(wrapped)
                    filter_chain.append(
                        f"drawtext=textfile='{escape_drawtext_path(dialogue_text_path)}':"
                        f"fontfile='{escape_drawtext_path(dialogue_font)}':"
                        f"fontsize={dialogue_font_size}:"
                        f"fontcolor={dialogue_font_color}:"
                        f"x=(w-text_w)/2:y={dialogue_y_expr}:"
                        f"box=1:boxcolor={dialogue_box_color}:boxborderw=12"
                    )
                filter_chain.append(drawtext_filter)

                cmd = [
                    'ffmpeg',
                    '-y',  # Overwrite output
                    '-ss', str(start),
                    '-i', source_video,
                    '-t', str(duration),
                    '-vf', video_filter,
                    '-r', str(video_fps),
                    *_video_codec_args(),  # NVENC when driver allows, else libx264
                    # 8-bit is REQUIRED: 10-bit sources (e.g. DJI D-Log) would
                    # otherwise emit High10, and a concat-copy stream that
                    # switches profile mid-file kills every hardware
                    # decoder (browser/NVDEC) exactly at that clip boundary.
                    '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac',
                    '-ar', str(audio_ar),
                    '-ac', '2',
                    '-avoid_negative_ts', 'make_zero',
                    clip_file
                ]
            else:
                # No label overlay, but may have crop and/or viz
                filter_chain = []
                if viz_filters:
                    filter_chain.extend(viz_filters)
                if crop_filter:
                    filter_chain.append(crop_filter)

                if subtitle_lines:
                    dialogue_font = dialogue_font_file or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    out_w = crop_width_px if crop_width_px else video_width
                    max_chars = max(10, int(out_w / (dialogue_font_size * 0.6)))
                    for j, sl in enumerate(subtitle_lines):
                        sl_text_path = os.path.join(temp_dir, f"dialogue_{i:04d}_{j:03d}.txt")
                        wrapped = "\n".join(textwrap.wrap(sl['text'], width=max_chars))
                        with open(sl_text_path, "w", encoding="utf-8") as tf:
                            tf.write(wrapped)
                        filter_chain.append(
                            f"drawtext=textfile='{escape_drawtext_path(sl_text_path)}':"
                            f"fontfile='{escape_drawtext_path(dialogue_font)}':"
                            f"fontsize={dialogue_font_size}:"
                            f"fontcolor={dialogue_font_color}:"
                            f"x=(w-text_w)/2:y={dialogue_y_expr}:"
                            f"box=1:boxcolor={dialogue_box_color}:boxborderw=12:"
                            f"enable='between(t,{sl['start']:.3f},{sl['end']:.3f})'"
                        )
                elif overlay_text:
                    dialogue_font = dialogue_font_file or "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    dialogue_text_path = os.path.join(temp_dir, f"dialogue_{i:04d}.txt")
                    out_w = crop_width_px if crop_width_px else video_width
                    max_chars = max(10, int(out_w / (dialogue_font_size * 0.6)))
                    wrapped = "\n".join(textwrap.wrap(overlay_text, width=max_chars))
                    with open(dialogue_text_path, "w", encoding="utf-8") as text_file:
                        text_file.write(wrapped)
                    filter_chain.append(
                        f"drawtext=textfile='{escape_drawtext_path(dialogue_text_path)}':"
                        f"fontfile='{escape_drawtext_path(dialogue_font)}':"
                        f"fontsize={dialogue_font_size}:"
                        f"fontcolor={dialogue_font_color}:"
                        f"x=(w-text_w)/2:y={dialogue_y_expr}:"
                        f"box=1:boxcolor={dialogue_box_color}:boxborderw=12"
                    )

                if filter_chain:
                    video_filter = ",".join(filter_chain)
                    cmd = [
                        'ffmpeg',
                        '-y',  # Overwrite output
                        '-ss', str(start),
                        '-i', source_video,
                        '-t', str(duration),
                        '-vf', video_filter,
                        # Uniform fps is REQUIRED: clips are joined with the concat
                        # demuxer in stream-copy mode, and mixed frame rates (e.g. a
                        # 59.94fps source among 50fps ones) corrupt the timeline —
                        # segments play at the wrong speed and the total duration
                        # inflates. Every extraction branch must emit the same -r.
                        '-r', str(video_fps),
                        *_video_codec_args(),  # NVENC when driver allows, else libx264
                        '-pix_fmt', 'yuv420p',  # force 8-bit (10-bit source → High10 breaks hw decoders)
                        '-c:a', 'aac',
                        '-ar', str(audio_ar),
                        '-ac', '2',
                        '-avoid_negative_ts', 'make_zero',
                        clip_file
                    ]
                else:
                    cmd = [
                        'ffmpeg',
                        '-y',  # Overwrite output
                        '-ss', str(start),
                        '-i', source_video,
                        '-t', str(duration),
                        '-r', str(video_fps),
                        *_video_codec_args(),  # NVENC when driver allows, else libx264
                        '-pix_fmt', 'yuv420p',  # force 8-bit (10-bit source → High10 breaks hw decoders)
                        '-c:a', 'aac',
                        '-ar', str(audio_ar),
                        '-ac', '2',
                        '-avoid_negative_ts', 'make_zero',
                        clip_file
                    ]

            # cinematic layer — applied per clip (clips are re-encoded anyway,
            # so grade/letterbox/fades cost zero extra passes)
            _extra_vf = []
            _is_extra = clip.get('is_ending') or clip.get('is_intro')
            if color_grade and not _is_extra:
                _g = COLOR_GRADES.get(color_grade)
                if _g:
                    _extra_vf.append(_g)
            if letterbox and not _is_extra:
                _extra_vf.append(LETTERBOX_FILTER)
            if fades:
                if i == 0:
                    _extra_vf.append("fade=t=in:st=0:d=0.5")
                if i == _last_main_idx and duration > 1.5:
                    _extra_vf.append(f"fade=t=out:st={duration - 1.2:.2f}:d=1.2")
            if _extra_vf:
                if '-vf' in cmd:
                    _vi = cmd.index('-vf') + 1
                    cmd[_vi] = cmd[_vi] + "," + ",".join(_extra_vf)
                else:
                    _ti = cmd.index('-t') + 2
                    cmd = cmd[:_ti] + ['-vf', ",".join(_extra_vf)] + cmd[_ti:]

            if duck_windows and not _source_has_audio(source_video):
                # silent source (drone footage) — give the clip a REAL silent
                # track so the audio concat/xfade chains stay homogeneous
                j = cmd.index(source_video) + 1
                # 0:v:0 (not 0:v): DJI/GoPro MP4s embed an MJPEG cover thumbnail
                # as a SECOND video stream. "0:v" maps both, and ffmpeg then fails
                # trying to re-encode the attached-picture stream through libx264.
                # Map only the first (real) video stream.
                cmd = (cmd[:j]
                       + ['-f', 'lavfi', '-t', str(duration), '-i',
                          f'anullsrc=channel_layout=stereo:sample_rate={audio_ar}']
                       + cmd[j:-1] + ['-map', '0:v:0', '-map', '1:a'] + [clip_file])

            if verbose:
                label_info = f" [S{section_idx + 1}-Shot{shot_idx + 1}]" if show_labels_for_clip else ""
                print(f"  [{i+1}/{len(clips)}] {clip['start_str']} - {clip['end_str']} ({duration:.2f}s){label_info}")

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE if not verbose else None,
                stderr=subprocess.PIPE if not verbose else None
            )

            if result.returncode != 0:
                print(f"Error extracting clip {i}: {clip}")
                if not verbose and result.stderr:
                    print(result.stderr.decode()[-500:])
                return False

            # Update clip duration to actual extracted duration (fps conversion or cutting may change it)
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', clip_file],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            if probe.stdout.strip():
                try:
                    actual_dur = float(probe.stdout.strip())
                    clip['duration'] = actual_dur
                except ValueError:
                    pass

        # Create concat list file
        concat_file = os.path.join(temp_dir, 'concat_list.txt')
        with open(concat_file, 'w') as f:
            for clip_file in clip_files:
                f.write(f"file '{clip_file}'\n")

        # Concatenate all clips
        print("Concatenating clips...")

        if audio_path and os.path.exists(audio_path):
            # First join video clips (hard cut, or crossfade if transition_duration>0)
            temp_video = os.path.join(temp_dir, 'temp_video.mp4')
            cmd = _build_video_join_cmd(concat_file, clip_files, clips, temp_video, video_fps,
                                        transition_duration, transitions,
                                        with_audio=bool(duck_windows))

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE if not verbose else None,
                stderr=subprocess.PIPE if not verbose else None
            )

            if result.returncode != 0:
                print("Error concatenating clips")
                if not verbose and result.stderr:
                    print(result.stderr.decode()[-500:])
                return False

            # Get exact duration of temp_video
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', temp_video],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            temp_video_actual_duration = None
            if probe.stdout.strip():
                try:
                    temp_video_actual_duration = float(probe.stdout.strip())
                except ValueError:
                    pass

            # Then mix with audio (with optional cropping)
            print(f"Mixing with audio: {audio_path}")

            # duck windows need the temp video's own audio track; the hook-
            # dialogue flow prepends a clip (shifting the timeline), so the
            # two are mutually exclusive — dialogue mode wins.
            if hook_dialogue_duration > 0:
                duck_windows = []
            include_original_audio = (original_audio_volume > 0) or (hook_dialogue_duration > 0) \
                or bool(duck_windows)
            total_duration = temp_video_actual_duration if temp_video_actual_duration else sum(c['duration'] for c in clips)
            actual_outro_duration = sum(c['duration'] for c in clips if c.get('is_ending'))
            outro_duration = max(0.0, actual_outro_duration if actual_outro_duration > 0 else ending_duration)
            outro_start = max(0.0, total_duration - outro_duration)
            temp_video_duration = total_duration
            # Ensure BGM crop covers the full render timeline (ending clip duration
            # may be adjusted by ffprobe after fps conversion, making total_duration
            # larger than the audio_duration computed before that correction).
            if audio_duration is not None and audio_duration < total_duration:
                print(
                    f"BGM crop extended to cover full timeline: "
                    f"{audio_duration:.2f}s -> {total_duration:.2f}s"
                )
                audio_duration = total_duration
            bgm_crop_duration = audio_duration
            bgm_crop_label = f"{bgm_crop_duration:.2f}s" if bgm_crop_duration is not None else "full"

            print(
                "Final duration alignment: "
                f"render={total_duration:.2f}s, "
                f"temp_video={temp_video_duration:.2f}s, "
                f"bgm_crop={bgm_crop_label}"
            )

            bgm_dialogue_volume = max(0.0, min(1.0, bgm_dialogue_volume))
            ending_fade_target = max(0.0, min(1.0, ending_fade_target))
            bgm_base_volume_expr = f"if(lt(t,{hook_dialogue_duration}),{bgm_dialogue_volume},1.0)"
            if outro_duration > 0 and ending_fade_target < 1.0:
                bgm_outro_fade_expr = (
                    f"if(lt(t,{outro_start}),1.0,"
                    f"if(gte(t,{outro_start + outro_duration}),{ending_fade_target},"
                    f"1.0-(1.0-{ending_fade_target})*(t-{outro_start})/{outro_duration}))"
                )
            else:
                bgm_outro_fade_expr = "1.0"
            # voice-highlight ducking: inside each window the BGM drops to 25%
            # and the ORIGINAL audio fades in, with 0.3s ramps on both sides —
            # real voices/laughter play over quiet music, then music returns
            _DUCK_R = 0.3
            duck_env = None
            if duck_windows:
                _terms = [
                    f"max(0,min(1,min((t-{a - _DUCK_R:.2f})/{_DUCK_R},({b + _DUCK_R:.2f}-t)/{_DUCK_R})))"
                    for a, b in duck_windows
                ]
                duck_env = "min(1,(" + "+".join(_terms) + "))"
                print("Voice-highlight ducking at: "
                      + ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in duck_windows))

            bgm_volume_expr = f"({bgm_base_volume_expr})*({bgm_outro_fade_expr})"
            if duck_env:
                bgm_volume_expr = f"({bgm_volume_expr})*(1-0.75*{duck_env})"

            if audio_start_time is not None and audio_duration is not None:
                print(f"Audio crop: {audio_start_time:.2f}s - {audio_start_time + audio_duration:.2f}s (duration: {audio_duration:.2f}s)")

            bgm_trim_filter = None
            if audio_start_time is not None and audio_duration is not None:
                bgm_trim_filter = (
                    f"atrim=start={audio_start_time}:duration={audio_duration},"
                    "asetpts=PTS-STARTPTS"
                )
            elif audio_start_time is not None:
                bgm_trim_filter = f"atrim=start={audio_start_time},asetpts=PTS-STARTPTS"
            elif audio_duration is not None:
                bgm_trim_filter = f"atrim=duration={audio_duration},asetpts=PTS-STARTPTS"

            if include_original_audio:
                print("Mixing original video audio with background music")
                if hook_dialogue_duration > 0:
                    print(f"Background music ducking during dialogue: {bgm_dialogue_volume:.2f}x for {hook_dialogue_duration:.2f}s")
                if outro_duration > 0:
                    print(f"Background music fade to {ending_fade_target:.2f} over last {outro_duration:.2f}s")
                if auto_loudness_match:
                    print(
                        "Auto loudness matching enabled: "
                        f"I={target_lufs:.1f} LUFS, LRA={target_lra:.1f}, TP={target_tp:.1f} dBTP"
                    )

                orig_volume_expr = f"if(lt(t,{hook_dialogue_duration}),1.0,{original_audio_volume})"
                if duck_env:
                    # original audio rises to full inside duck windows
                    orig_volume_expr = f"min(1,({orig_volume_expr})+{duck_env})"
                filter_parts = []

                orig_input_label = "0:a"
                bgm_input_label = "1:a"

                bgm_stage_label = bgm_input_label
                if bgm_trim_filter:
                    filter_parts.append(f"[{bgm_stage_label}]{bgm_trim_filter}[a1t]")
                    bgm_stage_label = "a1t"

                if auto_loudness_match:
                    filter_parts.append(
                        f"[0:a]loudnorm=I={target_lufs}:LRA={target_lra}:TP={target_tp}[a0n]"
                    )
                    filter_parts.append(
                        f"[{bgm_stage_label}]loudnorm=I={target_lufs}:LRA={target_lra}:TP={target_tp}[a1n]"
                    )
                    orig_input_label = "a0n"
                    bgm_stage_label = "a1n"

                filter_parts.append(
                    f"[{orig_input_label}]volume={escape_ffmpeg_expr(orig_volume_expr)}:eval=frame,"
                    f"apad=whole_dur={total_duration + 1.0}[a0p]"
                )
                bgm_filters = f"[{bgm_stage_label}]volume={escape_ffmpeg_expr(bgm_volume_expr)}:eval=frame"
                bgm_filters += f",apad=whole_dur={total_duration + 1.0}[a1p]"
                filter_parts.append(bgm_filters)
                # Normalize=0 prevents amix from dynamically changing BGM volume when original audio ends.
                # Padding inputs ensures they don't unexpectedly drop out early.
                filter_parts.append("[a0p][a1p]amix=inputs=2:duration=longest:normalize=0[amix]")
                _afade = (f",afade=t=out:st={max(0.0, total_duration - 1.5):.2f}:d=1.5"
                          if fades else "")
                filter_parts.append(
                    f"[amix]atrim=duration={total_duration},asetpts=PTS-STARTPTS{_afade}[aout]"
                )
                filter_complex = ";".join(filter_parts)
                print(f"DEBUG filter_complex: {filter_complex}")

                # Use re-encode when ending clip is present so -t can cut precisely
                # (copy mode can only cut at keyframe boundaries, truncating the ending).
                video_codec = [*_video_codec_args()[1:], '-pix_fmt', 'yuv420p'] if outro_duration > 0 else ['copy']
                cmd = [
                    'ffmpeg',
                    '-y',
                    '-i', temp_video,
                    '-stream_loop', '-1',
                    '-i', audio_path,
                    '-filter_complex', filter_complex,
                    '-map', '0:v:0',
                    '-map', '[aout]',
                    '-c:v', *video_codec,
                    '-c:a', 'aac',
                    '-ar', str(audio_ar),
                    '-t', str(total_duration),
                    output_path
                ]
            else:
                if outro_duration > 0:
                    print(f"Background music fade to {ending_fade_target:.2f} over last {outro_duration:.2f}s")
                if auto_loudness_match:
                    print(
                        "Auto loudness matching enabled (BGM only): "
                        f"I={target_lufs:.1f} LUFS, LRA={target_lra:.1f}, TP={target_tp:.1f} dBTP"
                    )

                filter_parts = []
                bgm_input_label = "1:a"

                bgm_stage_label = bgm_input_label
                if bgm_trim_filter:
                    filter_parts.append(f"[{bgm_stage_label}]{bgm_trim_filter}[a1t]")
                    bgm_stage_label = "a1t"

                if auto_loudness_match:
                    filter_parts.append(
                        f"[{bgm_stage_label}]loudnorm=I={target_lufs}:LRA={target_lra}:TP={target_tp}[a1n]"
                    )
                    bgm_stage_label = "a1n"

                _afade = (f",afade=t=out:st={max(0.0, total_duration - 1.5):.2f}:d=1.5"
                          if fades else "")
                filter_parts.append(
                    f"[{bgm_stage_label}]volume={escape_ffmpeg_expr(bgm_volume_expr)}:eval=frame,"
                    f"apad=whole_dur={total_duration + 1.0},"
                    f"atrim=duration={total_duration},asetpts=PTS-STARTPTS{_afade}[aout]"
                )
                filter_complex = ";".join(filter_parts)

                video_codec = [*_video_codec_args()[1:], '-pix_fmt', 'yuv420p'] if outro_duration > 0 else ['copy']
                cmd = [
                    'ffmpeg',
                    '-y',
                    '-i', temp_video,
                    '-stream_loop', '-1',
                    '-i', audio_path,
                    '-filter_complex', filter_complex,
                    '-c:v', *video_codec,
                    '-c:a', 'aac',
                    '-ar', str(audio_ar),
                    '-map', '0:v:0',
                    '-map', '[aout]',
                    '-t', str(total_duration),
                    output_path
                ]
        else:
            # Just join clips without additional audio (hard cut or crossfade)
            cmd = _build_video_join_cmd(concat_file, clip_files, clips, output_path, video_fps, transition_duration, transitions)

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if not verbose else None,
            stderr=subprocess.PIPE if not verbose else None
        )

        if result.returncode != 0:
            print("Error creating final video")
            if not verbose and result.stderr:
                print(result.stderr.decode()[-500:])
            return False

    return True


def print_clip_summary(clips: List[Dict[str, Any]]):
    """Print a summary of all clips to be rendered."""
    total_duration = sum(c['duration'] for c in clips)
    adjusted_count = sum(1 for c in clips if c.get('adjusted', False))
    crop_center_count = sum(1 for c in clips if c.get('crop_center') is not None)

    print("\n" + "=" * 60)
    print("Clip Summary")
    print("=" * 60)

    current_section = -1
    for clip in clips:
        if clip.get('is_intro'):
            print("\n[Intro]")
            current_section = clip['section_idx']
        elif clip.get('is_ending'):
            print("\n[Ending]")
            current_section = clip['section_idx']
        elif clip['section_idx'] != current_section:
            current_section = clip['section_idx']
            print(f"\n[Section {current_section}]")

        adjustment_marker = " [ADJUSTED]" if clip.get('adjusted', False) else ""
        crop_marker = ""
        if clip.get('crop_center'):
            cx, cy = clip['crop_center']
            crop_marker = f" [CROP_CENTER: ({cx:.1f}, {cy:.1f})]"
        if clip.get('is_intro'):
            print(f"  Dialogue: {clip['start_str']} - {clip['end_str']} ({clip['duration']:.2f}s){adjustment_marker}")
        elif clip.get('is_ending'):
            if clip.get('video_path'):
                print(f"  Ending video: {clip['duration']:.2f}s")
            else:
                print(f"  Ending image: {clip['duration']:.2f}s")
        else:
            print(f"  Shot {clip['shot_idx']}: {clip['start_str']} - {clip['end_str']} ({clip['duration']:.2f}s){adjustment_marker}{crop_marker}")

    print("\n" + "-" * 60)
    print(f"Total clips: {len(clips)}")
    print(f"Total duration: {total_duration:.2f}s ({total_duration/60:.2f} min)")
    if adjusted_count > 0:
        print(f"Adjusted clips (snapped to scene cuts): {adjusted_count}")
    if crop_center_count > 0:
        print(f"Clips with dynamic crop center: {crop_center_count}")
    print("=" * 60 + "\n")


class _SafeStream:
    """stdout/stderr wrapper that swallows write errors. When the API server
    (our parent) restarts, our piped stdout closes and the next print() would
    raise BrokenPipe — killing an otherwise healthy render minutes before it
    writes the output file. Losing log lines beats losing the render."""

    def __init__(self, s):
        self._s = s

    def write(self, x):
        try:
            return self._s.write(x)
        except (OSError, ValueError):
            return len(x)

    def flush(self):
        try:
            self._s.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._s, name)


def main():
    sys.stdout = _SafeStream(sys.stdout)
    sys.stderr = _SafeStream(sys.stderr)
    parser = argparse.ArgumentParser(description='Render video from shot_point.json')
    parser.add_argument(
        '--shot-json',
        type=str,
        required=True,
        help='Path to shot_point.json file'
    )
    parser.add_argument(
        '--shot-plan',
        type=str,
        default=None,
        help='Optional path to shot_plan.json file (for audio time range detection in short videos)'
    )
    parser.add_argument(
        '--video',
        type=str,
        default="",
        help='Path to source video file (optional if clips carry per-clip video_path)'
    )
    parser.add_argument(
        '--audio',
        type=str,
        default=None,
        help='Optional path to audio file to mix with video'
    )
    parser.add_argument(
        '--output',
        type=str,
        required=True,
        help='Path for output video file'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show ffmpeg output'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Only print clip summary without rendering'
    )
    parser.add_argument(
        '--no-labels',
        default=True,
        action='store_true',
        help='Disable Section/Shot label overlay on video'
    )
    parser.add_argument(
        '--label-position',
        type=str,
        default='top-left',
        choices=['top-left', 'top-right', 'bottom-left', 'bottom-right'],
        help='Position of Section/Shot labels (default: top-left)'
    )
    parser.add_argument(
        '--font-size',
        type=int,
        default=32,
        help='Font size for labels (default: 32)'
    )
    parser.add_argument(
        '--font-color',
        type=str,
        default='white',
        help='Font color for labels (default: white)'
    )
    parser.add_argument(
        '--bg-color',
        type=str,
        default='black@0.3',
        help='Background color for label box with opacity (default: black@0.3)'
    )
    parser.add_argument(
        '--shot-scenes',
        type=str,
        default=None,
        help='Optional path to shot_scenes.txt file for scene cut detection and adjustment'
    )
    parser.add_argument(
        '--crop-ratio',
        type=str,
        default=None,
        help='Optional aspect ratio for center cropping (e.g., "9:16", "16:9", "1:1"). Keeps height unchanged and crops width to match the ratio.'
    )
    parser.add_argument(
        '--transition',
        type=float,
        default=0.0,
        help='Crossfade (dissolve) duration in seconds between clips. 0 = hard cuts (default).'
    )
    parser.add_argument(
        '--transition-mode',
        type=str,
        default='',
        choices=['', 'uniform', 'ai'],
        help='"ai" = LLM picks a transition per cut from the built-in palette '
             '(uses shot_plan descriptions); empty/"uniform" = legacy --transition behavior.'
    )
    parser.add_argument(
        '--transitions',
        type=str,
        default='',
        help='Explicit per-cut list, comma-separated, e.g. "cut,fadeblack:0.4,zoomin:0.35". '
             'Overrides --transition-mode.'
    )
    parser.add_argument(
        '--color-grade',
        type=str,
        default='',
        choices=['', 'teal_orange', 'film', 'warm'],
        help='Cinematic color grade applied per clip (pure ffmpeg filters).'
    )
    parser.add_argument(
        '--letterbox',
        action='store_true',
        help='2.35:1 cinematic letterbox inside the 16:9 frame.'
    )
    parser.add_argument(
        '--fades',
        action='store_true',
        help='Fade in from black (0.5s), fade out to black + music fade at the end.'
    )
    parser.add_argument(
        '--visualize-detections',
        action='store_true',
        help='Visualize protagonist detection results on video (draw bounding boxes, crop center, and crop area)'
    )
    parser.add_argument(
        '--original-audio-volume',
        type=float,
        default=0.0,
        help='Volume level for original video audio when mixing with external audio (0.0 or higher). 0.0 = muted (default), 1.0 = full volume, >1.0 = amplified (e.g., 2.0 = double volume).'
    )
    parser.add_argument(
        '--render-hook-dialogue',
        default=False,
        action='store_true',
        help='Render hook dialogue as an intro clip with centered text (requires shot_plan with hook_dialogue)'
    )
    parser.add_argument(
        '--dialogue-font',
        type=str,
        default='resource/font/Pulp Fiction M54.ttf',
        help='Font file path for dialogue text (defaults to DejaVuSans-Bold)'
    )
    parser.add_argument(
        '--dialogue-font-size',
        type=int,
        default=48,
        help='Font size for dialogue text (default: 48)'
    )
    parser.add_argument(
        '--dialogue-font-color',
        type=str,
        default='white',
        help='Font color for dialogue text (default: white)'
    )
    parser.add_argument(
        '--dialogue-box-color',
        type=str,
        default='black@0.3',
        help='Background color for dialogue box with opacity (default: black@0.3)'
    )
    parser.add_argument(
        '--dialogue-y-position',
        type=str,
        default='bottom',
        help='Vertical position of dialogue/subtitle text: "bottom" (default), "top", "center", or a raw ffmpeg y-expression (e.g. "h-text_h-100")'
    )
    parser.add_argument(
        '--bgm-dialogue-volume',
        type=float,
        default=0.5,
        help='Background music volume during intro dialogue (0.0-1.0, default: 0.5)'
    )
    parser.add_argument(
        '--ending-video',
        type=str,
        default=None,
        help='Ending video path to display as final clip (omit = no ending appended)'
    )
    parser.add_argument(
        '--ending-fade-target',
        type=float,
        default=0.0,
        help='Target background music volume at end of outro (0.0-1.0, default: 0.0)'
    )
    parser.add_argument(
        '--disable-auto-loudness-match',
        action='store_true',
        help='Disable automatic loudness matching between original audio and BGM before volume scaling.'
    )
    parser.add_argument(
        '--target-lufs',
        type=float,
        default=-18.0,
        help='Target LUFS for automatic loudness matching (default: -18.0)'
    )
    parser.add_argument(
        '--target-lra',
        type=float,
        default=11.0,
        help='Target LRA for automatic loudness matching (default: 11.0)'
    )
    parser.add_argument(
        '--target-tp',
        type=float,
        default=-1.5,
        help='Target true peak (dBTP) for automatic loudness matching (default: -1.5)'
    )

    args = parser.parse_args()

    # Ensure ffmpeg/ffprobe are discoverable on Windows
    _ensure_ffmpeg_on_path()

    # Validate original audio volume
    if args.original_audio_volume < 0.0:
        print(f"Error: --original-audio-volume must be 0.0 or higher (got {args.original_audio_volume})")
        return 1
    if args.target_lra <= 0:
        print(f"Error: --target-lra must be > 0 (got {args.target_lra})")
        return 1

    # Check input files exist
    if not os.path.exists(args.shot_json):
        print(f"Error: Shot JSON file not found: {args.shot_json}")
        return 1

    if not os.path.exists(args.video):
        print(f"Error: Video file not found: {args.video}")
        return 1

    # Load shot data
    print(f"Loading shot data from: {args.shot_json}")
    with open(args.shot_json, 'r', encoding='utf-8') as f:
        shot_data = json.load(f)

    # Parse scene cut points if provided
    cut_points = None
    if args.shot_scenes and os.path.exists(args.shot_scenes):
        print(f"Loading scene cut points from: {args.shot_scenes}")
        fps = 2
        print(f"Video framerate: {fps:.2f} fps")
        cut_points = parse_shot_scenes(args.shot_scenes, fps)
        print(f"Loaded {len(cut_points)} scene cut points")

    # Get video dimensions for coordinate scaling
    video_width, video_height = get_video_dimensions(args.video)
    print(f"Video dimensions: {video_width}x{video_height}")

    # Extract all clips (with coordinate scaling if detection_short_side provided)
    clips = extract_all_clips(
        shot_data,
        cut_points,
        video_width=video_width,
        video_height=video_height,
    )

    if not clips:
        print("Error: No valid clips found in shot data")
        return 1

    # Detect audio time range and hook dialogue from shot_plan if provided
    audio_start_time = None
    audio_duration = None
    core_music_duration = None
    hook_dialogue_duration = 0.0
    hook_dialogue_text = None
    hook_dialogue_range = None

    if args.shot_plan and os.path.exists(args.shot_plan):
        print(f"Loading shot plan from: {args.shot_plan}")
        with open(args.shot_plan, 'r', encoding='utf-8') as f:
            shot_plan = json.load(f)

        # Extract time range from video_structure
        if 'video_structure' in shot_plan and len(shot_plan['video_structure']) > 0:
            # For short videos, typically there's one audio section
            section = shot_plan['video_structure'][0]
            start_time_str = section.get('start_time', '0')
            end_time_str = section.get('end_time', '0')

            audio_start_time = float(start_time_str)
            audio_end_time = float(end_time_str)
            core_music_duration = max(0.0, audio_end_time - audio_start_time)
            audio_duration = core_music_duration

            print(f"Detected audio time range from shot_plan:")
            print(f"  Start: {audio_start_time:.1f}s")
            print(f"  End: {audio_end_time:.1f}s")
            print(f"  Duration: {core_music_duration:.1f}s")

        if args.render_hook_dialogue:
            hook_dialogue = shot_plan.get('hook_dialogue')
            if hook_dialogue and hook_dialogue.get('lines') and hook_dialogue.get('start') and hook_dialogue.get('end'):
                hook_dialogue_text = "\n".join(strip_speaker_prefix(line) for line in hook_dialogue['lines'])
                source_start_ts = hook_dialogue.get('source_start')
                source_end_ts = hook_dialogue.get('source_end')
                if source_start_ts and source_end_ts:
                    hook_start = srt_time_to_seconds(source_start_ts)
                    hook_end = srt_time_to_seconds(source_end_ts)
                else:
                    hook_start = srt_time_to_seconds(hook_dialogue['start'])
                    hook_end = srt_time_to_seconds(hook_dialogue['end'])
                hook_dialogue_range = (hook_start, hook_end)
                hook_dialogue_duration = max(0.0, hook_end - hook_start)
                print(f"Hook dialogue detected: {hook_dialogue_duration:.2f}s")
            else:
                print("Warning: --render-hook-dialogue set but hook_dialogue is missing in shot_plan")

    if args.render_hook_dialogue and args.shot_plan and os.path.exists(args.shot_plan):
        hook_dialogue = shot_plan.get('hook_dialogue') if 'shot_plan' in locals() else None
        if hook_dialogue:
            timed_intro_clips = build_hook_timed_clips(hook_dialogue)
            if timed_intro_clips:
                hook_dialogue_duration = sum(c['duration'] for c in timed_intro_clips)
                print(f"Using timed hook dialogue lines: {len(timed_intro_clips)} clips")
                clips = timed_intro_clips + clips
            elif hook_dialogue_range and hook_dialogue_text:
                hook_start, hook_end = hook_dialogue_range
                hook_clip = {
                    'section_idx': -1,
                    'shot_idx': -1,
                    'start_sec': hook_start,
                    'end_sec': hook_end,
                    'duration': hook_dialogue_duration,
                    'start_str': f"{hook_start:.3f}",
                    'end_str': f"{hook_end:.3f}",
                    'original_start': hook_start,
                    'original_end': hook_end,
                    'adjusted': False,
                    'crop_center': None,
                    'scaled_detections': None,
                    'overlay_text': hook_dialogue_text,
                    'show_labels': False,
                    'is_intro': True
                }
                clips = [hook_clip] + clips

    if args.ending_video:
        try:
            probe = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', args.ending_video],
                capture_output=True, text=True
            )
            ending_video_duration = float(probe.stdout.strip()) if probe.stdout.strip() else 4.0
        except Exception as e:
            print(f"Warning: Could not probe ending video: {e}")
            ending_video_duration = 4.0
        ending_clip = {
            'section_idx': 9999,
            'shot_idx': -1,
            'start_sec': 0.0,
            'end_sec': 0.0,
            'duration': ending_video_duration,
            'start_str': "",
            'end_str': "",
            'original_start': 0.0,
            'original_end': 0.0,
            'adjusted': False,
            'crop_center': None,
            'scaled_detections': None,
            'video_path': args.ending_video,
            'show_labels': False,
            'is_ending': True
        }
        clips = clips + [ending_clip]

    # Print summary
    print_clip_summary(clips)

    if args.dry_run:
        print("Dry run mode - skipping render")
        return 0

    # Create output directory if needed
    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Render video
    print(f"Rendering video to: {args.output}")
    if not args.no_labels:
        print(f"Labels enabled: position={args.label_position}, font_size={args.font_size}")
    if args.crop_ratio:
        print(f"Crop ratio specified: {args.crop_ratio}")
    if args.visualize_detections:
        print(f"Detection visualization enabled")
    if args.audio and args.original_audio_volume > 0:
        print(f"Original audio volume: {args.original_audio_volume:.2f}")
        if not args.disable_auto_loudness_match:
            print(
                "Auto loudness match: "
                f"I={args.target_lufs:.1f} LUFS, LRA={args.target_lra:.1f}, TP={args.target_tp:.1f} dBTP"
            )
    if hook_dialogue_duration > 0:
        print(f"Hook dialogue duration: {hook_dialogue_duration:.2f}s")
    if args.ending_video:
        print(f"Ending video: {args.ending_video}")

    total_video_duration = sum(c['duration'] for c in clips)
    duration_safety_margin = 0.5
    if audio_start_time is not None:
        intro_duration = sum(c['duration'] for c in clips if c.get('is_intro'))
        ending_duration = sum(c['duration'] for c in clips if c.get('is_ending'))
        middle_duration = max(0.0, total_video_duration - intro_duration - ending_duration)
        base_core_duration = core_music_duration if core_music_duration is not None else middle_duration

        # Extend the music window to cover intro dialogue + core section + ending,
        # and shift start earlier so the core section still aligns with shot points.
        adjusted_audio_start = max(0.0, audio_start_time - intro_duration)
        actual_intro_extension = audio_start_time - adjusted_audio_start
        audio_start_time = adjusted_audio_start
        audio_duration = max(0.0, base_core_duration + actual_intro_extension + ending_duration)

        # Ensure cropped BGM fully covers rendered timeline. This does NOT change
        # alignment of the core shot-plan region because audio_start_time is kept.
        minimum_audio_duration = total_video_duration + duration_safety_margin
        if audio_duration < minimum_audio_duration:
            print(
                "Extending audio crop to cover full render timeline: "
                f"{audio_duration:.2f}s -> {minimum_audio_duration:.2f}s"
            )
            audio_duration = minimum_audio_duration

        print("Adjusted music window for intro/ending:")
        print(f"  Intro extension: {actual_intro_extension:.2f}s")
        print(f"  Core duration: {base_core_duration:.2f}s")
        print(f"  Ending extension: {ending_duration:.2f}s")
        print(f"  Audio crop start: {audio_start_time:.2f}s")
        print(f"  Audio crop duration: {audio_duration:.2f}s")

    # Per-cut transition plan: explicit list > AI selection > legacy uniform
    transitions = None
    if args.transitions.strip():
        transitions = [t.strip() for t in args.transitions.split(",") if t.strip()]
        print(f"Using explicit transitions: {transitions}")
    elif args.transition_mode == "ai":
        _plan = shot_plan if ('shot_plan' in locals() and isinstance(shot_plan, dict)) else None
        transitions = _ai_pick_transitions(clips, _plan)
        if transitions is None:
            print("Fewer than 2 main clips — no transitions to decide")

    # Voice highlights → BGM ducking windows (real voices/laughter play through)
    duck_windows = []
    try:
        duck_windows = _compute_duck_windows(clips, transitions, args.transition)
        if duck_windows:
            print(f"🎙️  {len(duck_windows)} voice-highlight window(s) — BGM will duck there: "
                  + ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in duck_windows))
    except Exception as _e:  # noqa: BLE001
        print(f"(voice-highlight ducking skipped: {_e})")

    success = render_video_ffmpeg(
        video_path=args.video,
        clips=clips,
        output_path=args.output,
        audio_path=args.audio,
        audio_start_time=audio_start_time,
        audio_duration=audio_duration,
        verbose=args.verbose,
        show_labels=not args.no_labels,
        label_position=args.label_position,
        font_size=args.font_size,
        font_color=args.font_color,
        bg_color=args.bg_color,
        crop_ratio=args.crop_ratio,
        original_audio_volume=args.original_audio_volume,
        video_width=video_width,
        video_height=video_height,
        visualize_detections=args.visualize_detections,
        dialogue_font_file=args.dialogue_font,
        dialogue_font_size=args.dialogue_font_size,
        dialogue_font_color=args.dialogue_font_color,
        dialogue_box_color=args.dialogue_box_color,
        dialogue_y_position=args.dialogue_y_position,
        bgm_dialogue_volume=args.bgm_dialogue_volume,
        ending_duration=sum(c['duration'] for c in clips if c.get('is_ending')),
        ending_fade_target=args.ending_fade_target,
        hook_dialogue_duration=hook_dialogue_duration,
        auto_loudness_match=not args.disable_auto_loudness_match,
        target_lufs=args.target_lufs,
        target_lra=args.target_lra,
        target_tp=args.target_tp,
        transition_duration=args.transition,
        transitions=transitions,
        duck_windows=duck_windows,
        color_grade=args.color_grade,
        letterbox=args.letterbox,
        fades=args.fades,
    )

    if success:
        # Sidecar meta for the UI: which transitions were actually used at
        # each cut + the music window — the timeline visualizes both.
        try:
            _t_uniform = float(args.transition or 0)
            _mode = ("ai" if (args.transition_mode == "ai" and transitions)
                     else ("uniform" if _t_uniform > 0 else "none"))
            _meta = {
                "transition_mode": _mode,
                "transitions": (transitions if transitions else
                                ([f"fade:{_t_uniform:.2f}"] * max(0, len(clips) - 1)
                                 if _t_uniform > 0 else [])),
                "audio": {
                    "path": args.audio or "",
                    "start": round(float(audio_start_time or 0.0), 2),
                    "duration": round(float(audio_duration or 0.0), 2),
                },
                "duck_windows": [[round(a, 2), round(b, 2)] for a, b in (duck_windows or [])],
                "color_grade": args.color_grade,
                "letterbox": bool(args.letterbox),
                "fades": bool(args.fades),
                "clips": len(clips),
            }
            with open(os.path.splitext(args.output)[0] + ".render.json", "w", encoding="utf-8") as _mf:
                json.dump(_meta, _mf, ensure_ascii=False, indent=2)
        except Exception as _e:  # noqa: BLE001
            print(f"(render meta sidecar skipped: {_e})")
        print(f"\nSuccess! Video saved to: {args.output}")
        return 0
    else:
        print("\nFailed to render video")
        return 1


if __name__ == '__main__':
    exit(main())

