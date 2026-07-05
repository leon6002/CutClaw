"""Per-file analysis scheduler — the core of the refactored pipeline.

Each source video/audio is analyzed ONCE and cached by content hash.
Reusing assets across projects does not re-run expensive AI analysis.

Usage:
    from src.analyzer import analyze_video, analyze_audio, get_analysis_path

    video_hash = analyze_video("resource/imports/DJI_001.mp4")
    audio_hash = analyze_audio("resource/imports/Sand.mp3")
    # Results cached at Output/analyzed/{hash}/
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

from src.asset_manager.scanner import compute_content_hash


def _fix_console_encoding():
    """Avoid UnicodeEncodeError on Windows GBK consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_fix_console_encoding()

# Where all per-file analysis results live.
ANALYSIS_ROOT = os.path.join("Output", "analyzed")


# ── Path helpers ───────────────────────────────────────────────────────────

def get_analysis_path(content_hash: str) -> str:
    """Return the cache directory for a given content hash."""
    return os.path.join(ANALYSIS_ROOT, content_hash)


def _ensure_ffmpeg_on_path() -> None:
    """Make ffmpeg/ffprobe discoverable on Windows."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "ffmpeg"),
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


def _load_metadata(cache_dir: str) -> dict | None:
    """Load cached analysis metadata."""
    path = os.path.join(cache_dir, "metadata.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _save_metadata(cache_dir: str, metadata: dict):
    """Save analysis metadata."""
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)


# ── Video analysis ─────────────────────────────────────────────────────────

def _dense_caption_clip(video_path: str, start_sec: float, end_sec: float,
                         video_reader, video_fps: float, config,
                         reader_lock=None) -> list[dict] | None:
    """Run dense captioning on a time range: extract frames → VLM → segments.

    reader_lock: when clips are processed in parallel threads, decord readers
    are NOT thread-safe — frame extraction is serialized under this lock while
    the (dominant) VLM network calls overlap freely.
    """
    try:
        import litellm
        from src.utils.media_utils import array_to_base64, seconds_to_hhmmss
        from src.video.deconstruction.video_caption import SYSTEM_PROMPT, messages as caption_msgs
        # One unified, content-adaptive prompt — describes people OR scenery, no toggle.
        from src.prompt import DENSE_CAPTION_PROMPT_FILM as _dense_tmpl
        import contextlib

        vr = video_reader
        if vr is None:
            return None

        # Extract evenly-spaced frames (decord access serialized when parallel)
        max_frames = 12
        start_f = max(0, int(start_sec * video_fps))
        end_f = min(int(end_sec * video_fps), len(vr) - 1)
        if end_f <= start_f:
            return None
        total_frames = end_f - start_f + 1
        step = max(1, total_frames // max_frames)
        indices = list(range(start_f, end_f + 1, step))[:max_frames]
        if indices[-1] != end_f:
            indices.append(end_f)
        with (reader_lock if reader_lock is not None else contextlib.nullcontext()):
            frames = vr.get_batch(indices).asnumpy()
        b64_frames = [array_to_base64(frames[i]) for i in range(len(frames))]

        # Build prompt
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        req_dur = end_sec - start_sec
        prompt = _dense_tmpl.replace(
            "MAIN_CHARACTER_NAME_PLACEHOLDER", "the subject"
        ).replace("MIN_SEGMENT_DURATION_PLACEHOLDER", str(max(2.0, req_dur / 10)))
        prompt += (
            f"\n\n[Clip Timing Constraints]\n"
            f"- Requested clip duration: {req_dur:.2f}s\n"
            f"- Relative timeline MUST start at 00:00:00 and end at {seconds_to_hhmmss(req_dur)}\n"
            f"- Produce 3-8 segments covering the full duration\n"
        )
        user_content = [{"type": "text", "text": prompt}]
        for b64 in b64_frames:
            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        msgs.append({"role": "user", "content": user_content})

        kwargs = dict(model=config.VIDEO_ANALYSIS_MODEL, messages=msgs,
                      max_tokens=config.VIDEO_ANALYSIS_MODEL_MAX_TOKEN, temperature=0.0)
        if config.VIDEO_ANALYSIS_ENDPOINT:
            kwargs["api_base"] = config.VIDEO_ANALYSIS_ENDPOINT
        if config.VIDEO_ANALYSIS_API_KEY:
            kwargs["api_key"] = config.VIDEO_ANALYSIS_API_KEY

        raw = litellm.completion(**kwargs)
        content = raw.choices[0].message.content or ""
        # Parse JSON from response
        import re as _re3
        m = _re3.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, _re3.DOTALL)
        if m:
            content = m.group(1).strip()
        result = json.loads(content)
        raw_segments = result.get("segments", []) if isinstance(result, dict) else []
        # Convert relative timestamps to absolute, with a scale-correction. The VLM
        # sometimes writes seconds in MM:SS form ("12 seconds" -> "00:12:00" =
        # 720s), which blows up the timeline: the real 12-18s of an 18s clip get
        # labeled 720-1080s, so the editor can't find them and only ever picks
        # from the (correctly-timed) start. Our shots are always < 60s, so any
        # relative time that overshoots the clip by a large factor is a ×60
        # mis-slotting -- RECOVER it by /60 (keeping the real description) rather
        # than discarding the segment. Only genuinely tiny overshoots get clamped.
        _clip_dur = max(0.0, end_sec - start_sec)

        def _rescale(v: float) -> float:
            if _clip_dur <= 0:
                return max(0.0, v)
            if v <= _clip_dur + 0.5:
                return max(0.0, v)                       # already valid
            if v >= _clip_dur * 2 and v / 60.0 <= _clip_dur + 0.5:
                return v / 60.0                          # ×60 mis-slot -> recover
            return _clip_dur                             # minor overshoot -> clamp

        segments = []
        for seg in raw_segments:
            ts = seg.get("timestamp", "")
            rm = _re3.search(r'([\d:.]+)\s+to\s+([\d:.]+)', ts, _re3.IGNORECASE)
            if not rm:
                continue
            s_rel = _rescale(sum(float(x) * m2 for x, m2 in zip(reversed(rm.group(1).split(":")), [1, 60, 3600])))
            e_rel = _rescale(sum(float(x) * m2 for x, m2 in zip(reversed(rm.group(2).split(":")), [1, 60, 3600])))
            if e_rel - s_rel < 0.1:
                continue  # still degenerate after correction -> drop
            seg["timestamp_absolute"] = f"{seconds_to_hhmmss(start_sec + s_rel)} to {seconds_to_hhmmss(start_sec + e_rel)}"
            seg["start_sec_abs"] = round(start_sec + s_rel, 2)
            seg["end_sec_abs"] = round(start_sec + e_rel, 2)
            segments.append(seg)

        # Cap segment length: a very long uniform segment (e.g. a static landscape
        # held for 10s+) is split into equal ~MAX-second anchors carrying the same
        # description. Cheap (no extra VLM calls) and guarantees the editor's cache
        # lookup always finds coverage, so it never needs a fresh call.
        _max_seg = float(getattr(config, "DENSE_CAPTION_MAX_SEGMENT_SEC", 6.0))
        if _max_seg > 0 and segments:
            import math as _math_cap
            capped = []
            for seg in segments:
                try:
                    _s = float(seg.get("start_sec_abs")); _e = float(seg.get("end_sec_abs"))
                except (TypeError, ValueError):
                    capped.append(seg); continue
                _dur = _e - _s
                if _dur <= _max_seg or _dur <= 0:
                    capped.append(seg); continue
                _n = int(_math_cap.ceil(_dur / _max_seg))
                _step = _dur / _n
                for _k in range(_n):
                    _cs = _s + _k * _step
                    _ce = _e if _k == _n - 1 else _s + (_k + 1) * _step
                    sub = dict(seg)
                    sub["start_sec_abs"] = round(_cs, 2)
                    sub["end_sec_abs"] = round(_ce, 2)
                    sub["timestamp_absolute"] = f"{seconds_to_hhmmss(_cs)} to {seconds_to_hhmmss(_ce)}"
                    sub["timestamp"] = f"{seconds_to_hhmmss(_cs - start_sec)} to {seconds_to_hhmmss(_ce - start_sec)}"
                    if _n > 1:
                        sub["_split_part"] = f"{_k + 1}/{_n}"
                    capped.append(sub)
            segments = capped
        return segments
    except Exception as e:
        print(f"[DenseCaption] Failed for {video_path} [{start_sec:.1f}-{end_sec:.1f}]: {e}")
        return None


def _analyze_video_inner(video_path: str, cache_dir: str, video_type: str = "film",
                         progress_callback=None):
    """Run the full per-video analysis pipeline (no cache check — caller guards).

    progress_callback(stage_key, status, detail) where:
      stage_key: "shot_detection" | "captioning" | "scene_merge" | "scene_analysis"
      status: "start" | "done" | "skip" | "api_call"
      detail: str (elapsed time, model name, file count, etc.)
    """
    from src import config

    _ensure_ffmpeg_on_path()

    abs_path = os.path.abspath(video_path)
    fn = os.path.basename(video_path)
    frames_dir = os.path.join(cache_dir, "frames")
    captions_dir = os.path.join(cache_dir, "captions")
    shots_dir = os.path.join(captions_dir, "ckpt")
    scenes_dir = os.path.join(captions_dir, "scenes")
    scene_summaries_dir = os.path.join(captions_dir, "scene_summaries_video")
    shot_scenes_file = os.path.join(frames_dir, "shot_scenes.txt")
    caption_file = os.path.join(captions_dir, "captions.json")
    scenes_output = os.path.join(scenes_dir, "scene_0.json")

    def _emit(stage, status, detail=""):
        if progress_callback:
            progress_callback(stage, status, detail)
        if status == "start":
            print(f"▶ [Analyze] {stage}: {fn} {detail}")
        elif status == "done":
            print(f"✅ [Analyze] {stage} done {detail}")
        elif status == "skip":
            print(f"⏭️  [Analyze] {stage} skipped {detail}")

    # Step 1: Shot detection + frame extraction
    _emit("shot_detection", "start", f"PySceneDetect scanning frames @ {config.VIDEO_FPS}fps, threshold={config.SHOT_DETECTION_THRESHOLD}")
    t0 = time.time()
    from src.video.preprocess.video_utils import decode_video_to_frames

    vr = decode_video_to_frames(
        abs_path, frames_dir,
        config.VIDEO_FPS, config.VIDEO_RESOLUTION,
        max_minutes=getattr(config, "VIDEO_MAX_MINUTES", None),
        shot_detection_threshold=config.SHOT_DETECTION_THRESHOLD,
        shot_detection_min_scene_len=config.SHOT_DETECTION_MIN_SCENE_LEN,
        save_frames_to_disk=getattr(config, "VIDEO_SAVE_DEBUG_FRAMES", False),
        image_format="jpg", jpeg_quality=80,
    )
    num_scenes = len(vr.get("scenes", [])) if isinstance(vr, dict) else 0

    # If 0 shot boundaries detected (single continuous shot, common for drone clips),
    # create a synthetic boundary covering the entire video so it gets captioned.
    if num_scenes == 0 and os.path.exists(shot_scenes_file):
        with open(shot_scenes_file, "r") as _sf:
            _scontent = _sf.read().strip()
        if not _scontent:
            # shot_scenes.txt is in SAMPLED-frame space — video_caption converts
            # frames→seconds via `frame / SHOT_DETECTION_FPS`. Use the SAMPLED
            # frame count (num_frames), NOT len(video_reader) (SOURCE frames):
            # writing source frames here divided by SHOT_DETECTION_FPS inflated a
            # short single-shot clip to a phantom 30s+ span (e.g. an 11.3s drone
            # clip → "0-30s"), so the editor sliced one continuous take into
            # repetitive adjacent windows.
            _sampled = 0
            if isinstance(vr, dict):
                _sampled = vr.get("num_frames") or len(vr.get("frame_indices") or [])
            if not _sampled:
                # NOTE: `metadata` belongs to the OUTER function — referencing it
                # here was a latent NameError. Derive duration from the reader.
                try:
                    _rd = vr.get("video_reader") if isinstance(vr, dict) else None
                    if _rd is not None and len(_rd) > 0 and float(_rd.get_avg_fps() or 0) > 0:
                        _sampled = int(len(_rd) / float(_rd.get_avg_fps()) * config.VIDEO_FPS)
                except Exception:
                    _sampled = 0
            if _sampled > 0:
                with open(shot_scenes_file, "w") as _sf:
                    _sf.write(f"0 {_sampled - 1}\n")
                # Update vr dict so process_video picks it up
                if isinstance(vr, dict) and "scenes" in vr:
                    vr["scenes"] = [[0, _sampled - 1]]
                num_scenes = 1
                print(f"🔧 [Analyze] No shot boundaries — treating entire video as 1 clip ({_sampled} sampled frames)")

    _emit("shot_detection", "done", f"{num_scenes} shot boundaries · {time.time() - t0:.1f}s")

    # Step 2: Clip captioning (VLM per clip)
    if not os.path.exists(caption_file):
        # Count expected clips from shot boundaries
        _clip_count = "?"
        if os.path.exists(shot_scenes_file):
            try:
                with open(shot_scenes_file) as f:
                    _clip_count = str(len([l for l in f if l.strip() and not l.startswith("#")]))
            except Exception:
                pass
        _emit("captioning", "start", f"{_clip_count} clips → {config.VIDEO_ANALYSIS_MODEL}")
        _emit("captioning", "progress", f"Extracting frames + encoding to base64 → sending to VLM ({config.VIDEO_ANALYSIS_ENDPOINT})")
        t0 = time.time()
        from src.video.deconstruction.video_caption import process_video
        process_video(
            video=vr, output_caption_folder=captions_dir,
            subtitle_file_path=None,
            long_shots_path=shot_scenes_file if os.path.exists(shot_scenes_file) else None,
            video_type=video_type, frames_dir=frames_dir,
        )
        # Count actual results
        _ckpt_files = len([f for f in os.listdir(shots_dir) if f.endswith(".json")]) if os.path.isdir(shots_dir) else 0
        _emit("captioning", "done", f"{_ckpt_files} clips captioned · {time.time() - t0:.1f}s")
    else:
        _emit("captioning", "skip", "cached")

    # Step 2.5: Dense captioning — break each clip into time segments
    _ckpt_files_now = [f for f in os.listdir(shots_dir) if f.endswith(".json")] if os.path.isdir(shots_dir) else []
    _need_dense = []
    for _cf in _ckpt_files_now:
        _cp = os.path.join(shots_dir, _cf)
        try:
            with open(_cp, "r", encoding="utf-8") as _f2:
                _cd = json.load(_f2)
            if not _cd.get("dense_segments"):
                _need_dense.append(_cf)
        except Exception:
            pass

    if _need_dense:
        _emit("dense_caption", "start", f"{len(_need_dense)} clips → {config.VIDEO_ANALYSIS_MODEL}")
        _dense_t0 = time.time()
        _reader = vr.get("video_reader") if isinstance(vr, dict) else None
        _video_fps = float(_reader.get_avg_fps()) if _reader else 24.0

        # Parallel: VLM latency dominates (5-60s per clip) while frame
        # extraction is fast — serialize decord access under a lock and let
        # the network calls overlap. Sequentially this step took N×latency.
        import threading as _threading
        from concurrent.futures import ThreadPoolExecutor as _TPE
        _rd_lock = _threading.Lock()
        _workers = max(1, min(int(getattr(config, "CAPTION_BATCH_SIZE", 4) or 4), len(_need_dense)))

        def _dense_one(_cf: str):
            _cp = os.path.join(shots_dir, _cf)
            try:
                with open(_cp, "r", encoding="utf-8") as _f2:
                    _cd = json.load(_f2)
            except Exception:
                return
            _dr = _cd.get("duration", {})
            _clip_start = _dr.get("clip_start_time", "00:00:00")
            _clip_end = _dr.get("clip_end_time", "00:00:05")
            _start_sec = sum(float(x) * m for x, m in zip(reversed(str(_clip_start).split(":")), [1, 60, 3600]))
            _end_sec = sum(float(x) * m for x, m in zip(reversed(str(_clip_end).split(":")), [1, 60, 3600]))
            if _end_sec <= _start_sec:
                return
            _emit("dense_caption", "progress", f"{_cf}: {_clip_start}-{_clip_end}")
            _segments = _dense_caption_clip(abs_path, _start_sec, _end_sec, _reader,
                                            _video_fps, config, reader_lock=_rd_lock)
            if _segments:
                _cd["dense_segments"] = _segments
                with open(_cp, "w", encoding="utf-8") as _f2:
                    json.dump(_cd, _f2, ensure_ascii=False, indent=2)

        if _workers > 1:
            with _TPE(max_workers=_workers) as _ex:
                list(_ex.map(_dense_one, _need_dense))
        else:
            for _cf in _need_dense:
                _dense_one(_cf)
        _emit("dense_caption", "done", f"{len(_need_dense)} clips · {time.time() - _dense_t0:.1f}s")

    # Step 3: Scene merge
    if os.path.exists(shots_dir) and not os.path.exists(scenes_output):
        _emit("scene_merge", "start", "Loading shots, computing embeddings (all-MiniLM-L6-v2)...")
        t0 = time.time()
        from src.video.deconstruction.scene_merge import OptimizedSceneSegmenter, load_shots, save_scenes
        shots = load_shots(shots_dir)
        _emit("scene_merge", "progress", f"{len(shots)} shots loaded · computing semantic similarity...")
        if shots:
            segmenter = OptimizedSceneSegmenter()
            merged = segmenter.segment(
                shots,
                threshold=getattr(config, "SCENE_SIMILARITY_THRESHOLD", 0.5),
                max_scene_duration_secs=getattr(config, "MAX_SCENE_DURATION_SECS", 300),
            )
            save_scenes(merged, scenes_dir)
            _emit("scene_merge", "done", f"{len(shots)} shots → {len(merged)} scenes · {time.time() - t0:.1f}s")
        else:
            _emit("scene_merge", "done", "0 shots")
    elif os.path.exists(scenes_output):
        _emit("scene_merge", "skip", "cached")
    else:
        _emit("scene_merge", "skip", "no shots dir")

    # Step 4: Scene analysis (VLM per scene)
    if os.path.exists(scenes_dir) and os.path.exists(scenes_output):
        scene_files = [f for f in os.listdir(scenes_dir) if f.startswith("scene_") and f.endswith(".json")]
        existing = len([f for f in os.listdir(scene_summaries_dir) if f.endswith(".json")]) if os.path.isdir(scene_summaries_dir) else 0
        if existing < len(scene_files):
            to_do = len(scene_files) - existing
            _emit("scene_analysis", "start", f"{to_do}/{len(scene_files)} scenes new → {config.VIDEO_ANALYSIS_MODEL}")
            _emit("scene_analysis", "progress", f"Extracting scene frames + VLM analysis ({config.VIDEO_ANALYSIS_ENDPOINT})")
            t0 = time.time()
            from src.video.deconstruction.scene_analysis_video import SceneVideoAnalyzer
            sa = SceneVideoAnalyzer(vr=vr, subtitle_file=None)
            result = sa.analyze_scenes_dir(scenes_dir=scenes_dir, output_dir=scene_summaries_dir,
                                           max_workers=config.CAPTION_BATCH_SIZE, overwrite=False)
            _emit("scene_analysis", "done", f"{result.get('success', 0)} scenes · {time.time() - t0:.1f}s")
        else:
            _emit("scene_analysis", "skip", f"all {existing} cached")
    else:
        _emit("scene_analysis", "skip", "")

    vr.clear() if hasattr(vr, "clear") else None


def analyze_video(
    video_path: str,
    video_type: str = "film",
    force: bool = False,
    progress_callback=None,
) -> str:
    """Analyze a single video: shot detection → captions → scenes → scene summaries.

    Results are cached at ``Output/analyzed/{content_hash}/``.
    Subsequent calls with the same file (by content hash) are no-ops.

    Args:
        video_path: Path to the video file.
        video_type: ``"film"`` or ``"vlog"``.
        force: If True, re-analyze even if cached.

    Returns:
        The content_hash (cache key) for this video.
    """
    _ensure_ffmpeg_on_path()

    abs_path = os.path.abspath(video_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Video not found: {abs_path}")

    content_hash = compute_content_hash(abs_path)
    if not content_hash:
        raise RuntimeError(f"Could not compute hash for: {abs_path}")

    cache_dir = get_analysis_path(content_hash)

    # Gather metadata regardless
    from src.asset_manager.scanner import probe_video_metadata
    file_size = os.path.getsize(abs_path)
    meta = probe_video_metadata(abs_path, file_size)
    metadata = {
        "file_path": video_path,
        "absolute_path": abs_path,
        "file_name": os.path.basename(abs_path),
        "content_hash": content_hash,
        "file_size_bytes": file_size,
        "file_size_mb": meta.file_size_mb,
        "duration_sec": meta.duration_sec,
        "width": meta.width,
        "height": meta.height,
        "fps": meta.fps,
        "codec": meta.codec,
        "video_type": video_type,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    existing = _load_metadata(cache_dir)
    # "Already analyzed" must mean COMPLETED, not merely started: metadata.json
    # is written before the pipeline runs, so checking bare existence turned a
    # crashed/killed run into a permanently "done" cache (the per-step resume
    # inside the pipeline never got a chance to fire). Require the completion
    # marker; legacy caches (pre-marker) count as complete only when their end
    # artifacts actually exist.
    def _looks_complete(md: dict) -> bool:
        if md.get("analysis_complete"):
            return True
        _sums = os.path.join(cache_dir, "captions", "scene_summaries_video")
        _caps = os.path.join(cache_dir, "captions", "captions.json")
        return (os.path.exists(_caps) and os.path.isdir(_sums)
                and any(f.endswith(".json") for f in os.listdir(_sums)))

    if existing and not force:
        if _looks_complete(existing):
            print(f"♻️  [Analyze] Video already analyzed: {os.path.basename(video_path)} (hash={content_hash[:12]})")
            return content_hash
        print(f"🔁 [Analyze] Previous run incomplete — resuming: {os.path.basename(video_path)} (hash={content_hash[:12]})")

    _save_metadata(cache_dir, metadata)
    # If forcing, clear ALL cached analysis artifacts so every step re-runs.
    # Removing only shot_scenes.txt left captions/scenes/summaries in place, and
    # each step skips when its output exists — so a "re-annotate" silently kept
    # the old captions/scene analysis. Nuke captions/ + shot_scenes for a true
    # full re-analysis (frames aren't persisted, so nothing else to clear).
    if force:
        import shutil as _shutil
        _cap_dir = os.path.join(cache_dir, "captions")
        if os.path.isdir(_cap_dir):
            _shutil.rmtree(_cap_dir, ignore_errors=True)
        old_scenes = os.path.join(cache_dir, "frames", "shot_scenes.txt")
        if os.path.exists(old_scenes):
            os.unlink(old_scenes)

    _analyze_video_inner(abs_path, cache_dir, video_type=video_type, progress_callback=progress_callback)

    # Fallback: if no scene summaries produced (common for short continuous drone shots),
    # create a synthetic scene_0.json so the video is still usable by Screenwriter.
    summaries_dir = os.path.join(cache_dir, "captions", "scene_summaries_video")
    if not os.path.isdir(summaries_dir) or not any(
        f.endswith(".json") for f in os.listdir(summaries_dir)
    ):
        os.makedirs(summaries_dir, exist_ok=True)
        caption_file = os.path.join(cache_dir, "captions", "captions.json")
        caption_summary = ""
        if os.path.exists(caption_file):
            try:
                with open(caption_file, "r", encoding="utf-8") as f:
                    cap = json.load(f)
                if isinstance(cap, dict):
                    caption_summary = cap.get("summary", "")
                elif isinstance(cap, list) and cap:
                    caption_summary = cap[0].get("summary", "") if isinstance(cap[0], dict) else ""
            except Exception:
                pass
        fallback = {
            "scene_id": 0,
            "shot_count": 1,
            "time_range": {"start_seconds": "00:00:00", "end_seconds": f"{metadata['duration_sec']:.1f}"},
            "shots_data": [],
            "video_analysis": {
                "scene_caption": {
                    "scene_summary": {
                        "summary": caption_summary or f"Continuous shot: {os.path.basename(abs_path)}",
                        "narrative": "",
                        "key_event": "",
                        "location": "",
                        "time": "",
                    },
                    "scene_classification": {"is_usable": True, "importance_score": 5},
                },
            },
            "_source_hash": content_hash,
        }
        with open(os.path.join(summaries_dir, "scene_0.json"), "w", encoding="utf-8") as f:
            json.dump(fallback, f, ensure_ascii=False, indent=2)
        print(f"🔧 [Analyze] No scenes detected — created fallback scene_0.json")

    # Update metadata with completion marker — only NOW does the cache count
    # as "already analyzed" (see the resume check above)
    metadata["analyzed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    metadata["analysis_complete"] = True
    _save_metadata(cache_dir, metadata)

    print(f"💾 [Analyze] Results cached: {cache_dir}")
    return content_hash


# ── Audio analysis ─────────────────────────────────────────────────────────

def analyze_audio(
    audio_path: str,
    force: bool = False,
) -> str:
    """Analyze a single audio file with madmom + LLM.

    Results cached at ``Output/analyzed/{content_hash}/``.

    Returns:
        The content_hash (cache key) for this audio file.
    """
    abs_path = os.path.abspath(audio_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"Audio not found: {abs_path}")

    content_hash = compute_content_hash(abs_path)
    if not content_hash:
        raise RuntimeError(f"Could not compute hash for: {abs_path}")

    cache_dir = get_analysis_path(content_hash)
    caption_path = os.path.join(cache_dir, "captions.json")

    from src import config

    # Cache is param-aware: fine-grained captions depend on segment bounds
    # (derived from the project's shot length). Same file + different params
    # → re-analyze instead of silently reusing the old granularity.
    seg_min = float(getattr(config, "AUDIO_MIN_SEGMENT_DURATION", 3.0))
    seg_max = float(getattr(config, "AUDIO_MAX_SEGMENT_DURATION", 30.0))
    params_sig = f"seg{seg_min:g}-{seg_max:g}"

    if os.path.exists(caption_path) and not force:
        old_sig = None
        try:
            with open(os.path.join(cache_dir, "metadata.json"), "r", encoding="utf-8") as f:
                old_sig = json.load(f).get("caption_params")
        except Exception:
            pass
        # old_sig is None for legacy caches (params unknown) — grandfather them
        if old_sig is None or old_sig == params_sig:
            print(f"♻️  [Analyze] Audio already analyzed: {os.path.basename(audio_path)} (hash={content_hash[:12]})")
            return content_hash
        print(f"🔁 [Analyze] Segment params changed ({old_sig} → {params_sig}), re-analyzing audio captions...")

    os.makedirs(cache_dir, exist_ok=True)

    from src.asset_manager.scanner import probe_audio_metadata

    file_size = os.path.getsize(abs_path)
    meta = probe_audio_metadata(abs_path, file_size)

    metadata = {
        "file_path": audio_path,
        "absolute_path": abs_path,
        "file_name": os.path.basename(abs_path),
        "content_hash": content_hash,
        "file_size_bytes": file_size,
        "file_size_mb": meta.file_size_mb,
        "duration_sec": meta.duration_sec,
        "sample_rate": meta.sample_rate,
        "channels": meta.channels,
        "caption_params": params_sig,
        "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_metadata(cache_dir, metadata)

    print(f"🎵 [Analyze] Audio analysis: {os.path.basename(audio_path)}")
    t0 = time.time()

    from src.audio.audio_caption_madmom import caption_audio_with_madmom_segments

    caption_audio_with_madmom_segments(
        audio_path=abs_path,
        output_path=caption_path,
        max_tokens=getattr(config, "AUDIO_KEYPOINT_MAX_TOKENS", 4096),
        temperature=getattr(config, "AUDIO_KEYPOINT_TEMPERATURE", 0.7),
        top_p=getattr(config, "AUDIO_KEYPOINT_TOP_P", 0.95),
        max_workers=getattr(config, "AUDIO_BATCH_SIZE", 4),
        detection_methods=getattr(config, "AUDIO_DETECTION_METHODS", ["downbeat", "pitch", "mel_energy"]),
        beats_per_bar=[getattr(config, "AUDIO_BEATS_PER_BAR", 4)],
        min_bpm=getattr(config, "AUDIO_MIN_BPM", 60),
        max_bpm=getattr(config, "AUDIO_MAX_BPM", 200),
        pitch_tolerance=getattr(config, "AUDIO_PITCH_TOLERANCE", 0.5),
        pitch_threshold=getattr(config, "AUDIO_PITCH_THRESHOLD", 0.1),
        pitch_min_distance=getattr(config, "AUDIO_PITCH_MIN_DISTANCE", 1.0),
        pitch_nms_method=getattr(config, "AUDIO_PITCH_NMS_METHOD", "gaussian"),
        pitch_max_points=getattr(config, "AUDIO_PITCH_MAX_POINTS", 50),
        mel_win_s=getattr(config, "AUDIO_MEL_WIN_S", 0.1),
        mel_n_filters=getattr(config, "AUDIO_MEL_N_FILTERS", 128),
        mel_threshold_ratio=getattr(config, "AUDIO_MEL_THRESHOLD_RATIO", 0.3),
        mel_min_distance=getattr(config, "AUDIO_MEL_MIN_DISTANCE", 1.0),
        mel_nms_method=getattr(config, "AUDIO_MEL_NMS_METHOD", "gaussian"),
        mel_max_points=getattr(config, "AUDIO_MEL_MAX_POINTS", 50),
        merge_close=getattr(config, "AUDIO_MERGE_CLOSE", 1.0),
        min_interval=getattr(config, "AUDIO_MIN_INTERVAL", 2.0),
        top_k_keypoints=getattr(config, "AUDIO_TOP_K", 30),
        energy_percentile=getattr(config, "AUDIO_ENERGY_PERCENTILE", 80),
        min_segment_duration=getattr(config, "AUDIO_MIN_SEGMENT_DURATION", 3.0),
        max_segment_duration=getattr(config, "AUDIO_MAX_SEGMENT_DURATION", 30.0),
        use_stage1_sections=getattr(config, "AUDIO_USE_STAGE1_SECTIONS", True),
        section_min_interval=getattr(config, "AUDIO_SECTION_MIN_INTERVAL", 0),
    )

    metadata["analyzed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save_metadata(cache_dir, metadata)

    print(f"✅ [Analyze] Audio done in {time.time() - t0:.1f}s → {cache_dir}")
    return content_hash


# ── Multi-source summary merging ───────────────────────────────────────────

def merge_scene_summaries(
    content_hashes: list[str],
) -> list[dict]:
    """Load scene summary JSONs from multiple analyzed videos and tag them.

    Each returned dict has an extra ``_source_hash`` field so downstream
    code knows which source video a scene belongs to.

    Returns a flat list of scene dicts sorted by source order.
    """
    all_scenes: list[dict] = []
    for ch in content_hashes:
        cache_dir = get_analysis_path(ch)
        summaries_dir = os.path.join(cache_dir, "captions", "scene_summaries_video")
        if not os.path.isdir(summaries_dir):
            continue
        for fn in sorted(os.listdir(summaries_dir)):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(summaries_dir, fn)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    scene = json.load(f)
            except Exception:
                continue
            scene["_source_hash"] = ch
            all_scenes.append(scene)
    return all_scenes


def get_video_metadata(content_hash: str) -> dict | None:
    """Load the metadata.json for an analyzed video."""
    return _load_metadata(get_analysis_path(content_hash))


def get_scene_summaries_dir(content_hash: str) -> str:
    """Return path to scene_summaries_video/ for an analyzed video."""
    return os.path.join(get_analysis_path(content_hash), "captions", "scene_summaries_video")


def get_audio_caption_path(content_hash: str) -> str:
    """Return path to captions.json for an analyzed audio."""
    return os.path.join(get_analysis_path(content_hash), "captions.json")
