import os
import json
import asyncio
import time as _time
import litellm
from typing import List, Dict, Optional, Tuple
from src import config
import copy
from tqdm import tqdm

# Import scene merge functions
from .scene_merge import OptimizedSceneSegmenter, load_shots, save_scenes
from src.prompt import SHOT_CAPTION_PROMPT
from src.utils.media_utils import (
    parse_json_safely,
    parse_srt_to_dict,
    parse_shot_scenes,
    array_to_base64,
    seconds_to_hhmmss,
)

# 定义核心 Prompt (保持之前设计的结构化指令)
messages = [
    {
        "role": "system",
        "content": ""
    },
    {
        "role": "user",
        "content": "",
    },
]

SYSTEM_PROMPT = "You are a helpful assistant."


def local_vlm_fallback() -> Optional[Dict]:
    """The local ollama VLM entry — ONLY when the daemon is actually up.

    Used as a last-resort captioner when the cloud VLM keeps disconnecting
    (100%-success rule): the affected clips get local captions (provenance
    recorded via _captioner) instead of stalling the whole pipeline."""
    try:
        import urllib.request
        entry = None
        pool_p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "api_pool.json")
        try:
            with open(pool_p, "r", encoding="utf-8") as f:
                for e in json.load(f):
                    if "11434" in str(e.get("endpoint", "")):
                        entry = e
                        break
        except Exception:  # noqa: BLE001
            pass
        entry = entry or {"model": "openai/qwen2.5vl:latest",
                          "endpoint": "http://127.0.0.1:11434/v1", "api_key": "sk-local"}
        base = str(entry["endpoint"]).split("/v1")[0]
        urllib.request.urlopen(base + "/api/tags", timeout=3)
        return {"model": entry["model"], "endpoint": entry["endpoint"],
                "api_key": entry.get("api_key") or "sk-local"}
    except Exception:  # noqa: BLE001
        return None




def gather_clip_frames(
    video_frame_folder, clip_secs: int, subtitle_file_path: str = None
) -> Dict[str, Dict]:
    # Fix possible typo in the earlier list-comprehension and gather frames again
    frame_files = sorted(
        [
            f for f in os.listdir(video_frame_folder)
            if f.startswith("frame") and (f.endswith(".jpg") or f.endswith(".png"))
        ],
        key=lambda x: float(x.split("_")[-1].rstrip(".jpg").rstrip(".png")),
    )
    if not frame_files:
        return {}

    # Optional subtitle information
    subtitle_map = (
        parse_srt_to_dict(subtitle_file_path) if subtitle_file_path else {}
    )

    # Map timestamps → file names for quick lookup
    frame_ts = [float(f.split("_")[-1].rstrip(".jpg").rstrip(".png")) / config.VIDEO_FPS for f in frame_files]
    ts_to_file = dict(zip(frame_ts, frame_files))
    last_ts = int(max(frame_ts))

    result = []

    # Iterate over fixed-length clips
    clip_start = 0
    while clip_start <= last_ts:
        clip_end = min(clip_start + clip_secs - 1, last_ts)

        # Collect frames that fall inside the current clip
        clip_files = [
            os.path.join(video_frame_folder, ts_to_file[t])
            for t in frame_ts
            if clip_start <= t <= clip_end
        ]

        # Aggregate transcript text overlapping the clip interval
        transcript_parts: List[str] = []
        for key, text in subtitle_map.items():
            s, e = map(int, key.split("_"))
            if s <= clip_end and e >= clip_start:  # overlap check
                transcript_parts.append(text)
        transcript = " ".join(transcript_parts).strip() or "No transcript."

        result.append((
                f"{clip_start}_{clip_end}", 
                {"files": clip_files, "transcript": transcript}
        ))

        clip_start += clip_secs
    return result

def gather_clip_frames_from_long_shots(
    video_reader,
    frame_indices: List[int],
    long_shot_boundaries_path: str,
    clip_secs: int,
    subtitle_file_path: str = None,
) -> List[Tuple[str, Dict]]:
    """
    Gather frames based on physical shot boundaries (long_shots).
    Reads frames directly from a decord VideoReader (in-memory, no disk I/O).
    If a long_shot is longer than clip_secs, it is split into sub-clips.

    Returns:
        List of (timestamp_key, {
            "arrays": [np.ndarray, ...],
            "transcript": "...",
            "long_shot_id": int,
            "is_sub_clip": bool,
            "frame_range": (start, end)
        })
    """
    return list(_iter_clip_frames(video_reader, frame_indices, long_shot_boundaries_path, clip_secs, subtitle_file_path))


def _iter_clip_frames(
    video_reader,
    frame_indices: List[int],
    long_shot_boundaries_path: str,
    clip_secs: int,
    subtitle_file_path: str = None,
    caption_ckpt_folder: str = None,
):
    """
    Generator version of gather_clip_frames_from_long_shots.
    Yields clips one at a time as frames are read, enabling overlap with captioning.
    """
    sampled_count = len(frame_indices)

    # Optional subtitle information
    subtitle_map = parse_srt_to_dict(subtitle_file_path) if subtitle_file_path else {}

    # Parse long shots (in sampled-frame space)
    long_shots = parse_shot_scenes(long_shot_boundaries_path)

    if not long_shots:
        print("⚠️  [VideoCaption] Warning: No shot boundaries found")
        return

    clip_specs = []

    def _append_clip_spec(timestamp_key, sampled_start, sampled_end, transcript_start_sec, transcript_end_sec, shot_id, is_sub_clip, sub_clip_idx=None):
        if sampled_end <= sampled_start:
            return
        clip_specs.append({
            "timestamp": timestamp_key,
            "sampled_start": sampled_start,
            "sampled_end": sampled_end,
            "transcript_start_sec": transcript_start_sec,
            "transcript_end_sec": transcript_end_sec,
            "long_shot_id": shot_id,
            "is_sub_clip": is_sub_clip,
            "sub_clip_idx": sub_clip_idx,
        })

    def _get_transcript(start_sec, end_sec):
        parts = [
            text for key, text in subtitle_map.items()
            for s, e in [map(int, key.split("_"))]
            if s <= end_sec and e >= start_sec
        ]
        return " ".join(parts).strip() or "No transcript."

    for shot_id, (start_frame, end_frame) in enumerate(long_shots):
        # long_shots must be in sampled-frame space; a boundary can never exceed
        # the number of frames we actually sampled. Legacy caches wrote some
        # boundaries in SOURCE-frame units, which inflated shots to a phantom
        # 30s+; clamp so the shot always maps onto real footage.
        start_frame = max(0, min(int(start_frame), sampled_count))
        end_frame = max(0, min(int(end_frame), sampled_count))
        if end_frame <= start_frame:
            continue
        shot_start_sec = start_frame / config.SHOT_DETECTION_FPS
        shot_end_sec = end_frame / config.SHOT_DETECTION_FPS
        shot_duration = shot_end_sec - shot_start_sec

        if shot_duration <= clip_secs:
            _append_clip_spec(
                timestamp_key=f"{int(shot_start_sec)}_{int(shot_end_sec)}",
                sampled_start=start_frame,
                sampled_end=end_frame,
                transcript_start_sec=shot_start_sec,
                transcript_end_sec=shot_end_sec,
                shot_id=shot_id,
                is_sub_clip=False,
            )
        else:
            clip_start_sec = shot_start_sec
            sub_clip_idx = 0
            while clip_start_sec < shot_end_sec:
                clip_end_sec = min(clip_start_sec + clip_secs, shot_end_sec)
                clip_start_frame = int(clip_start_sec * config.SHOT_DETECTION_FPS)
                clip_end_frame = int(clip_end_sec * config.SHOT_DETECTION_FPS)
                _append_clip_spec(
                    timestamp_key=f"{int(clip_start_sec)}_{int(clip_end_sec)}_shot{shot_id}_sub{sub_clip_idx}",
                    sampled_start=clip_start_frame,
                    sampled_end=clip_end_frame,
                    transcript_start_sec=clip_start_sec,
                    transcript_end_sec=clip_end_sec,
                    shot_id=shot_id,
                    is_sub_clip=True,
                    sub_clip_idx=sub_clip_idx,
                )
                clip_start_sec = clip_end_sec
                sub_clip_idx += 1

    if not clip_specs:
        return

    # Yield clips one by one, reading only the frames needed for each clip
    for spec in clip_specs:
        timestamp = spec["timestamp"]
        if caption_ckpt_folder and os.path.exists(os.path.join(caption_ckpt_folder, f"{timestamp}.json")):
            continue

        sampled_start = spec["sampled_start"]
        sampled_end = spec["sampled_end"]
        valid_indices = [idx for idx in range(sampled_start, sampled_end) if 0 <= idx < sampled_count]
        if not valid_indices:
            continue

        orig_indices = [frame_indices[idx] for idx in valid_indices]
        chunk_frames = video_reader.get_batch(orig_indices).asnumpy()

        # Motion-aware frame selection. The full 2fps sample (60 frames / 30s
        # clip) measured ~57k input tokens per call; but UNIFORM subsampling
        # is lossy — sub-2s action peaks (a jump apex, a splash) can vanish
        # between anchors. Instead: decode everything (cheap at 240p), score
        # inter-frame motion, and keep uniform anchors + the highest-motion
        # frames. Every visual event above noise survives; only redundant
        # near-duplicate frames are dropped (which also sharpens VLM focus —
        # walls of identical frames dilute attention).
        # VIDEO_CAPTION_MAX_FRAMES=0 disables the cap entirely.
        _max_f = 24
        try:
            from src import config as _cfg
            _max_f = int(getattr(_cfg, "VIDEO_CAPTION_MAX_FRAMES", 24) or 0)
        except Exception:
            pass
        if _max_f and len(chunk_frames) > _max_f:
            import numpy as _np
            _diffs = _np.abs(
                chunk_frames[1:].astype(_np.int16) - chunk_frames[:-1].astype(_np.int16)
            ).mean(axis=(1, 2, 3))  # motion score for frames 1..N-1
            _n = len(chunk_frames)
            _keep = {0, _n - 1}
            _n_anchor = max(6, _max_f // 3)          # uniform coverage floor
            for _i in range(_n_anchor):
                _keep.add(int(_i * (_n - 1) / max(1, _n_anchor - 1)))
            for _di in _np.argsort(_diffs)[::-1]:    # then motion peaks
                if len(_keep) >= _max_f:
                    break
                _keep.add(int(_di) + 1)
            _sel = sorted(_keep)[:_max_f]
            arrays = [chunk_frames[_i] for _i in _sel]
        else:
            arrays = list(chunk_frames)

        clip_data = {
            "arrays": arrays,
            "transcript": _get_transcript(spec["transcript_start_sec"], spec["transcript_end_sec"]),
            "long_shot_id": spec["long_shot_id"],
            "is_sub_clip": spec["is_sub_clip"],
            "frame_range": (sampled_start, sampled_end - 1),
        }
        if spec["is_sub_clip"]:
            clip_data["sub_clip_idx"] = spec["sub_clip_idx"]

        yield (timestamp, clip_data)


def _build_clip_request(task: Tuple[str, Dict]) -> Tuple[str, List[Dict], Tuple, str, str]:
    """Build the litellm message list for one clip (no LLM call)."""
    timestamp, info = task
    arrays, transcript, frame_range = info["arrays"], info["transcript"], info["frame_range"]

    timestamp_parts = timestamp.split("_scene")[0] if "_scene" in timestamp else timestamp
    clip_start_time = seconds_to_hhmmss(float(timestamp_parts.split("_")[0]))
    clip_end_time = seconds_to_hhmmss(float(timestamp_parts.split("_")[1]))

    send_messages = copy.deepcopy(messages)
    send_messages[0]["content"] = SYSTEM_PROMPT
    send_messages[1]["content"] = SHOT_CAPTION_PROMPT.replace("TRANSCRIPT_PLACEHOLDER", transcript)

    # Encode images and inject into user message
    _images_b64 = [array_to_base64(arr) for arr in arrays]
    processed_messages = []
    for msg in send_messages:
        if msg["role"] == "user" and _images_b64:
            content_array = [{"type": "text", "text": msg["content"]}] if msg["content"] else []
            for img_b64 in _images_b64:
                content_array.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                })
            processed_messages.append({"role": "user", "content": content_array})
        else:
            processed_messages.append({"role": msg["role"], "content": msg["content"]})

    return timestamp, processed_messages, frame_range, clip_start_time, clip_end_time


def _save_caption_result(
    timestamp: str,
    resp_content,
    frame_range,
    clip_start_time: str,
    clip_end_time: str,
    caption_ckpt_folder: str,
    clip_info: Optional[Dict] = None,
    is_last_attempt: bool = False,
) -> Optional[str]:
    """Parse model response and save to disk. Returns error string or None on success."""
    timestamp_parts = timestamp.split("_scene")[0] if "_scene" in timestamp else timestamp
    json_data = parse_json_safely(resp_content)
    if json_data:
        json_data["duration"] = {"clip_start_time": clip_start_time, "clip_end_time": clip_end_time}
        json_data["frame_range"] = frame_range
        # provenance: which model produced this caption (cloud vs local fallback)
        json_data["_captioner"] = getattr(config, "VIDEO_ANALYSIS_MODEL", "")
        if clip_info is not None:
            json_data["long_shot_id"] = clip_info.get("long_shot_id")
            json_data["is_sub_clip"] = bool(clip_info.get("is_sub_clip", False))
            if clip_info.get("sub_clip_idx") is not None:
                json_data["sub_clip_idx"] = clip_info.get("sub_clip_idx")
        save_path = os.path.join(caption_ckpt_folder, f"{timestamp}.json")
        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        return None  # Success
    else:
        err_preview = resp_content[:200] if resp_content else 'No response from model'
        level = "❌" if is_last_attempt else "⚠️ "
        print(f"{level} [VideoCaption] JSON Parse Error in {timestamp_parts}: {err_preview}")
        return f"JSON Parse Error in {timestamp_parts}.json"

def process_video(
    video: Dict,
    output_caption_folder: str,
    subtitle_file_path: str = None,
    long_shots_path: str = None,
    video_type: str = "film",
    frames_dir: str = None,
):
    """
    Process video and generate captions (Step 1: clip captioning, Step 2: scene merge).
    Scene video analysis (Step 3) is handled separately by the caller.
    """
    caption_ckpt_folder = os.path.join(output_caption_folder, "ckpt")
    os.makedirs(caption_ckpt_folder, exist_ok=True)

    video_reader = video["video_reader"]
    frame_indices = video["frame_indices"]

    # Resolve shot_scenes path
    if long_shots_path is None:
        long_shots_path = video.get("shot_scenes_path")

    # ---------------- Async captioning with overlapped frame reading ---------- #
    CONCURRENCY = config.CAPTION_BATCH_SIZE

    try:
        from src.utils.progress import emit_progress
    except Exception:
        def emit_progress(*a, **k):  # type: ignore
            pass
    _prog = {"idx": 0}
    emit_progress("video_clips", 0, -1, "reset")

    async def _caption_one(clip, clip_idx, semaphore, timeout, is_last_attempt=False):
        """Send one acompletion request with timeout, return (clip, meta, content_or_None)."""
        meta = _build_clip_request(clip)
        timestamp, messages_for_clip, frame_range, clip_start_time, clip_end_time = meta
        # workbench trace: surface the actual prompt/reply per clip (frames as count)
        _ptext, _n_imgs = "", 0
        for _m in messages_for_clip:
            _c = _m.get("content")
            if isinstance(_c, list):
                for _p in _c:
                    if _p.get("type") == "text":
                        _ptext += _p.get("text") or ""
                    elif _p.get("type") == "image_url":
                        _n_imgs += 1
        kwargs = dict(
            model=config.VIDEO_ANALYSIS_MODEL,
            messages=messages_for_clip,
            max_tokens=config.VIDEO_ANALYSIS_MODEL_MAX_TOKEN,
            temperature=0.7,
            response_format={"type": "json_object"},
        )
        if config.VIDEO_ANALYSIS_ENDPOINT:
            kwargs["api_base"] = config.VIDEO_ANALYSIS_ENDPOINT
        if config.VIDEO_ANALYSIS_API_KEY:
            kwargs["api_key"] = config.VIDEO_ANALYSIS_API_KEY

        async with semaphore:
            emit_progress("video_clips", 0, clip_idx, "step", phase="action", iter=1, max_iter=1,
                          tool=f"VLM 片段描述 · {_n_imgs} 帧 → {config.VIDEO_ANALYSIS_MODEL}",
                          args=_ptext[:2000])
            _t0 = _time.time()
            _err = ""
            try:
                resp = await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout)
                content = resp.choices[0].message.content
                if content is None:
                    _err = f"空响应 finish_reason={resp.choices[0].finish_reason}"
                    print(f"  ⚠️  [VideoCaption] [Null content] {timestamp}: finish_reason={resp.choices[0].finish_reason}")
            except Exception as e:
                level = "❌" if is_last_attempt else "⚠️ "
                _err = f"{type(e).__name__}: {e}"
                print(f"  {level} [VideoCaption] [Error] {timestamp}: {type(e).__name__}: {e}")
                content = None
            emit_progress("video_clips", 0, clip_idx, "step", phase="result", iter=1, max_iter=1,
                          elapsed=round(_time.time() - _t0, 1),
                          verdict="ok" if content else "fail",
                          result=(_err or (content or "").strip().split("\n")[0])[:160],
                          reply=(content or "")[:4000])
        return clip, meta, content, clip_idx

    async def _run_overlapped(clip_iter, pbar, timeout, is_last_attempt=False, concurrency=None):
        """
        Producer-consumer: read frames synchronously, caption concurrently.
        decord VideoReader is not thread-safe, so get_batch is called directly
        in the event loop thread (blocks briefly per clip, but safe).
        """
        semaphore = asyncio.Semaphore(concurrency or CONCURRENCY)
        failed_clips = []
        pending_tasks = set()

        def _next_clip():
            return next(clip_iter, None)

        while True:
            # decord VideoReader is not thread-safe; call get_batch directly (blocks briefly but safe)
            clip = _next_clip()
            if clip is None:
                break
            pbar.total += 1
            pbar.refresh()
            _idx = _prog["idx"]
            _prog["idx"] += 1
            emit_progress("video_clips", _prog["idx"], _idx, "start", label=str(clip[0]))
            task = asyncio.create_task(_caption_one(clip, _idx, semaphore, timeout, is_last_attempt=is_last_attempt))
            pending_tasks.add(task)
            await asyncio.sleep(0)  # yield to event loop so caption tasks can start

            # Drain completed tasks to avoid unbounded accumulation
            done = {t for t in pending_tasks if t.done()}
            for t in done:
                pending_tasks.discard(t)
                clip_r, meta, content, _cidx = t.result()
                ts, _, fr, cst, cet = meta
                err = _save_caption_result(ts, content, fr, cst, cet, caption_ckpt_folder, clip_info=clip_r[1], is_last_attempt=is_last_attempt)
                if err is not None:
                    failed_clips.append(clip_r)
                    if content:  # network was fine — surface the parse failure in the trace
                        emit_progress("video_clips", 0, _cidx, "step", phase="result",
                                      iter=1, max_iter=1, verdict="fail", result=str(err)[:160])
                    emit_progress("video_clips", _prog["idx"], _cidx, "fail")
                else:
                    pbar.update(1)
                    emit_progress("video_clips", _prog["idx"], _cidx, "done")

        # Wait for remaining tasks
        for t in asyncio.as_completed(pending_tasks):
            clip_r, meta, content, _cidx = await t
            ts, _, fr, cst, cet = meta
            err = _save_caption_result(ts, content, fr, cst, cet, caption_ckpt_folder, clip_info=clip_r[1], is_last_attempt=is_last_attempt)
            if err is not None:
                failed_clips.append(clip_r)
                if content:
                    emit_progress("video_clips", 0, _cidx, "step", phase="result",
                                  iter=1, max_iter=1, verdict="fail", result=str(err)[:160])
                emit_progress("video_clips", _prog["idx"], _cidx, "fail")
            else:
                pbar.update(1)
                emit_progress("video_clips", _prog["idx"], _cidx, "done")

        return failed_clips

    # First pass: overlapped read + caption (tqdm total unknown upfront, grows as clips arrive)
    clip_iter = _iter_clip_frames(
        video_reader, frame_indices, long_shots_path, config.CLIP_SECS, subtitle_file_path,
        caption_ckpt_folder=caption_ckpt_folder,
    )
    pbar = tqdm(total=0, desc="Captioning clips")
    loop = asyncio.new_event_loop()
    try:
        # Single cloud pass, timeout 300s, NO retries. The proxied endpoint's
        # normal latency approaches the old 90s limit, so timeouts mostly hit
        # requests the server had already processed — every retry re-billed the
        # full clip. One generous window; whatever still fails goes to the
        # local-VLM fallback below or stays resumable via per-clip ckpt.
        failed = loop.run_until_complete(_run_overlapped(clip_iter, pbar, timeout=300))
    finally:
        loop.close()

    pbar.close()

    if failed:
        # LAST RESORT: the cloud VLM kept failing (disconnects/rate limits) —
        # caption just the survivors with the LOCAL VLM when ollama is up.
        # Slower and coarser, but the pipeline finishes (100%-success rule);
        # provenance is recorded per clip via _captioner.
        _lv = local_vlm_fallback()
        if _lv:
            print(f"🛟 [VideoCaption] {len(failed)} clip(s) still failing on the cloud VLM — "
                  f"falling back to LOCAL {_lv['model']} for just these clips")
            _prev = (config.VIDEO_ANALYSIS_MODEL, config.VIDEO_ANALYSIS_ENDPOINT,
                     config.VIDEO_ANALYSIS_API_KEY)
            config.VIDEO_ANALYSIS_MODEL = _lv["model"]
            config.VIDEO_ANALYSIS_ENDPOINT = _lv["endpoint"]
            config.VIDEO_ANALYSIS_API_KEY = _lv["api_key"]
            loop2 = asyncio.new_event_loop()
            pbar2 = tqdm(total=len(failed), desc="Local VLM fallback")
            try:
                failed = loop2.run_until_complete(
                    _run_overlapped(iter(failed), pbar2, timeout=300,
                                    is_last_attempt=True, concurrency=2))
            finally:
                pbar2.close()
                loop2.close()
                (config.VIDEO_ANALYSIS_MODEL, config.VIDEO_ANALYSIS_ENDPOINT,
                 config.VIDEO_ANALYSIS_API_KEY) = _prev

    if failed:
        # HARD STOP before scene merge. Proceeding with a partial clip set used
        # to bake incomplete scenes and mark the whole analysis "complete" —
        # the failed clips were then never retried (e.g. API outage mid-run).
        # Raising keeps the analysis in "incomplete" state; the per-clip ckpt
        # files mean the next run retries ONLY the missing clips.
        raise RuntimeError(
            f"{len(failed)} clip(s) failed captioning (no auto-retry — 重试纪律) — "
            f"analysis left resumable; re-run to retry only the missing clips"
        )


    # ============ Step 2: Scene Merge ============
    print("\n" + "="*50)
    print("🧩 [VideoCaption] Step 2: Merging shots into scenes...")
    print("="*50)

    scenes_dir = os.path.join(output_caption_folder, "scenes")
    scenes_output = os.path.join(scenes_dir, "scene_0.json")

    if not os.path.exists(scenes_output):
        # Load shots from ckpt folder
        shots = load_shots(caption_ckpt_folder)
        print(f"📂 [VideoCaption] Loaded {len(shots)} shots from {caption_ckpt_folder}")

        if shots:
            # Initialize segmenter
            segmenter = OptimizedSceneSegmenter()

            # Merge shots into scenes
            merged_scenes = segmenter.segment(
                shots,
                threshold=config.SCENE_SIMILARITY_THRESHOLD,
                max_scene_duration_secs=config.MAX_SCENE_DURATION_SECS
            )

            print(f"✅ [VideoCaption] Merged {len(shots)} shots into {len(merged_scenes)} scenes")

            # Save scenes
            save_scenes(merged_scenes, scenes_dir)
            print(f"💾 [VideoCaption] Scenes saved to {scenes_dir}")
        else:
            print("⚠️  [VideoCaption] Warning: No shots found to merge")
    else:
        print(f"⏭️  [VideoCaption] Scenes already exist at {scenes_dir}, skipping merge")

    print("\n" + "="*50)
    print("🎉 [VideoCaption] Video processing complete!")
    print("="*50)


if __name__ == "__main__":
    import argparse
    import time
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../.."))

    from src import config
    from decord import VideoReader, cpu as decord_cpu

    parser = argparse.ArgumentParser(description="Benchmark / debug video_caption.process_video")
    parser.add_argument("--video",       required=True, help="Path to video file (e.g. Dataset/Video/VLOG/Lisbon.mp4)")
    parser.add_argument("--shot_scenes", required=True, help="Path to existing shot_scenes.txt")
    parser.add_argument("--out",         required=True, help="Output caption folder (e.g. /tmp/caption_test)")
    parser.add_argument("--subtitle",    default=None,  help="Optional path to .srt subtitle file")
    parser.add_argument("--type",        default="vlog", choices=["film", "vlog"], help="Video type")
    args = parser.parse_args()

    # ── Build vr dict from existing shot_scenes (no scenedetect) ─────────────
    print("\n" + "="*60)
    print("🎞️  [VideoCaption] Step 0: Loading video with decord (no shot detection)")
    print("="*60)
    t0 = time.perf_counter()
    from src.video.preprocess.video_utils import _create_decord_reader
    video_reader = _create_decord_reader(args.video, [180, 230])
    native_fps = float(video_reader.get_avg_fps())
    step = max(1, int(native_fps / config.VIDEO_FPS))
    frame_indices = list(range(0, len(video_reader), step))
    first = video_reader[0].asnumpy()
    height, width = first.shape[:2]
    t_load = time.perf_counter() - t0
    print(f"  ✅ [VideoCaption] done: {t_load:.1f}s  |  sampled={len(frame_indices)} frames  |  resolution={width}x{height}")

    vr = {
        "video_reader":    video_reader,
        "frame_indices":   frame_indices,
        "shot_scenes_path": args.shot_scenes,
        "num_frames":      len(frame_indices),
        "height":          height,
        "width":           width,
    }

    # # ── Step 1: gather_clip_frames_from_long_shots ───────────────────────────
    # print("\n" + "="*60)
    # print("Step 1: gather_clip_frames_from_long_shots  (get_batch)")
    # print("="*60)
    # t0 = time.perf_counter()
    # clips = gather_clip_frames_from_long_shots(
    #     video_reader, frame_indices, args.shot_scenes,
    #     config.CLIP_SECS, args.subtitle
    # )
    # t_gather = time.perf_counter() - t0
    # total_frames_gathered = sum(len(c[1]["arrays"]) for c in clips)
    # print(f"  done: {t_gather:.1f}s  |  clips={len(clips)}  |  total_frames={total_frames_gathered}")
    # if clips:
    #     print(f"  avg frames/clip:          {total_frames_gathered/len(clips):.1f}")
    #     print(f"  avg ms/frame (get_batch): {t_gather/max(total_frames_gathered,1)*1000:.1f} ms")

    # # ── Step 2: _build_clip_request (base64 encode) ──────────────────────────
    # print("\n" + "="*60)
    # print("Step 2: _build_clip_request  (base64 encode)")
    # print("="*60)
    # t0 = time.perf_counter()
    # built = [_build_clip_request(c) for c in clips]
    # t_build = time.perf_counter() - t0
    # print(f"  done: {t_build:.1f}s  |  avg ms/clip: {t_build/max(len(clips),1)*1000:.1f} ms")
    # if built:
    #     user_content = built[0][1][-1]["content"]
    #     n_imgs = sum(1 for x in user_content if x.get("type") == "image_url")
    #     kb = sum(len(x["image_url"]["url"].encode()) for x in user_content if x.get("type") == "image_url") / 1024
    #     print(f"  first clip: {n_imgs} images, {kb:.0f} KB payload")

    # ── Step 3: full process_video (LLM + scene merge + scene analysis) ──────
    print("\n" + "="*60)
    print("🧠 [VideoCaption] Step 3: process_video (LLM captioning + scene merge + scene analysis)")
    print("="*60)
    caption_folder = os.path.join(args.out, "captions")
    frames_dir = os.path.dirname(args.shot_scenes)
    t0 = time.perf_counter()
    process_video(
        video=vr,
        output_caption_folder=caption_folder,
        subtitle_file_path=args.subtitle,
        long_shots_path=args.shot_scenes,
        video_type=args.type,
        frames_dir=frames_dir,
    )
    t_process = time.perf_counter() - t0
    print(f"  ✅ [VideoCaption] done: {t_process:.1f}s")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("📊 [VideoCaption] Summary")
    print("="*60)
    total = t_load + t_gather + t_build + t_process
    for name, t in [("load video (decord)",  t_load),
                    ("gather (get_batch)",    t_gather),
                    ("build_request (b64)",   t_build),
                    ("LLM + scene merge",     t_process)]:
        print(f"  {name:<25} {t:>7.1f}s  ({t/total*100:.0f}%)")