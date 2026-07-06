"""AI annotation orchestrator — calls VLMs and audio analysis per asset.

Key design decisions:
- Video annotation runs the FULL analyze_video() pipeline (shot detection →
  clip captions → dense captions → scene merge → scene analysis), cached by
  content hash — so the editing stage never needs fresh VLM calls; the
  annotation is then distilled into a compact catalog entry with one LLM call.
- Image annotation: single VLM call per image.
- Audio annotation: delegates to the madmom pipeline with coarse parameters
  (Level 1 structure + measured facts; Level 2 per-segment captions skipped).
  Measured values (BPM etc.) always come from signal analysis, never the LLM.
"""

from __future__ import annotations

import copy
import gc
import json
import os
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import numpy as np

from .models import (
    AssetAnnotation,
    AudioAnnotation,
    AudioAssetMetadata,
    ImageAnnotation,
    ImageAssetMetadata,
    VideoAnnotation,
    VideoAssetMetadata,
)
from .prompts import (
    AUDIO_ANNOTATION_PROMPT,
    AUDIO_ANNOTATION_SYSTEM,
    IMAGE_ANNOTATION_PROMPT,
    IMAGE_ANNOTATION_SYSTEM,
    VIDEO_ANNOTATION_PROMPT,
    VIDEO_ANNOTATION_SYSTEM,
)

# ── Helpers ────────────────────────────────────────────────────────────────

def _default_vlm_model() -> str:
    try:
        from src import config
        return getattr(config, "ASSET_ANNOTATION_MODEL", None) or config.VIDEO_ANALYSIS_MODEL
    except Exception:
        return "openai/gpt-4o"


def _default_vlm_endpoint() -> str:
    try:
        from src import config
        return config.VIDEO_ANALYSIS_ENDPOINT
    except Exception:
        return ""


def _default_vlm_api_key() -> str:
    try:
        from src import config
        return config.VIDEO_ANALYSIS_API_KEY
    except Exception:
        return ""


def _default_audio_model() -> str:
    try:
        from src import config
        return getattr(config, "AUDIO_LITELLM_MODEL", "openai/gpt-4o")
    except Exception:
        return "openai/gpt-4o"


def _default_audio_endpoint() -> str:
    try:
        from src import config
        return config.AUDIO_LITELLM_BASE_URL
    except Exception:
        return ""


def _default_audio_api_key() -> str:
    try:
        from src import config
        return config.AUDIO_LITELLM_API_KEY
    except Exception:
        return ""


def _parse_json_strict(content: str | None) -> dict | None:
    """Robust JSON parsing with markdown fence stripping."""
    if not content:
        return None
    text = content.strip()
    # Strip ```json ... ``` fences
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _bpm_from_caption_dict(caption_data: dict) -> float | None:
    try:
        facts = caption_data.get("facts") or {}
        if facts.get("bpm_felt"):
            return float(facts["bpm_felt"])
        mt = caption_data.get("measured_tempo") or {}
        if mt.get("bpm_felt"):
            return float(mt["bpm_felt"])
        db = sorted(k["time"] for k in (caption_data.get("_keypoints_detail") or [])
                    if k.get("type") == "Downbeat")
        gaps = sorted(b - a for a, b in zip(db, db[1:]) if 0.8 <= b - a <= 8.0)
        if len(gaps) >= 4:
            return round(240.0 / gaps[len(gaps) // 2], 1)
    except Exception:
        pass
    return None


def _measured_bpm_from_caption(caption_data: dict, content_hash: str = "") -> float | None:
    """Felt-pulse BPM from the madmom analysis. Never trusts LLM-written text
    (which routinely answers genre clichés like '128 BPM').

    Order: explicit measured_tempo (new caches) → downbeat gaps in this caption
    → the PIPELINE's full analysis for the same content hash (the annotator's
    coarse cache keeps only ~top-30 keypoints, often too few downbeats)."""
    bpm = _bpm_from_caption_dict(caption_data)
    if bpm:
        return bpm
    if content_hash:
        full = os.path.join("Output", "analyzed", content_hash, "captions.json")
        if os.path.exists(full):
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    return _bpm_from_caption_dict(json.load(fh))
            except Exception:
                pass
    return None


# ── Video annotator ────────────────────────────────────────────────────────

def _extract_key_frames(video_path: str, num_frames: int = 5) -> list[np.ndarray]:
    """Open video with decord and extract *num_frames* evenly-spaced frames.

    Returns a list of numpy uint8 RGB arrays.
    """
    from src.video.preprocess.video_utils import _create_decord_reader

    reader = None
    frames: list[np.ndarray] = []
    try:
        reader = _create_decord_reader(video_path)
        if reader is None or len(reader) == 0:
            return frames

        total = len(reader)
        indices = [int(i * total / (num_frames + 1)) for i in range(1, num_frames + 1)]
        indices = [max(0, min(i, total - 1)) for i in indices]
        # Deduplicate
        indices = sorted(set(indices))

        batch = reader.get_batch(indices).asnumpy()
        for i in range(len(batch)):
            frames.append(batch[i])
    except Exception:
        pass
    finally:
        del reader
        gc.collect()

    return frames


def annotate_video_asset(
    metadata: VideoAssetMetadata,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    progress_callback=None,
    variant: str = "",
) -> VideoAnnotation:
    """Run full per-video analysis and distill into a VideoAnnotation.

    Uses ``src.analyzer.analyze_video()`` — the same full pipeline
    (shot detection → captions → scene merge → scene analysis).
    Cached permanently by content hash.
    """
    from src.analyzer import analyze_video, get_analysis_path

    # Deliberately NOT caught: swallowing an analysis failure into a fake
    # "analysis_failed" annotation marked the asset as annotated, so the next
    # batch run would SKIP it forever — the per-step resume never got a
    # chance. Let it propagate; the asset stays 未标注 and the next run
    # resumes from the per-clip checkpoints.
    # Key the analysis cache by metadata.content_hash rather than the file
    # bytes. For local files these are identical; for Immich proxies the server
    # re-keys content_hash to the ORIGINAL's stable checksum, so a re-downloaded
    # proxy (new bytes) still hits the same cache instead of re-analyzing.
    content_hash = analyze_video(metadata.absolute_path, progress_callback=progress_callback,
                                 variant=variant, content_hash=metadata.content_hash or None)

    # Distill scene summaries into compact annotation
    try:
        import json
        import os
        from .prompts import VIDEO_ANNOTATION_SYSTEM, VIDEO_ANNOTATION_PROMPT
        import litellm

        cache_dir = get_analysis_path(content_hash, variant)
        summaries_dir = os.path.join(cache_dir, "captions", "scene_summaries_video")

        # Build summary from scene analysis
        scene_summaries_text = ""
        if os.path.isdir(summaries_dir):
            parts = []
            for fn in sorted(os.listdir(summaries_dir)):
                if fn.endswith(".json"):
                    with open(os.path.join(summaries_dir, fn), "r", encoding="utf-8") as f:
                        scene = json.load(f)
                    v = scene.get("video_analysis", {}).get("scene_caption", {})
                    sc = v.get("scene_summary", {}) or v.get("visual_analysis", {})
                    if sc:
                        parts.append(json.dumps(sc, ensure_ascii=False)[:500])
            scene_summaries_text = "\n".join(parts[:20])

        if not scene_summaries_text:
            return VideoAnnotation(
                summary=f"Video: {metadata.file_name} ({metadata.duration_sec:.0f}s)",
                tags=["analyzed"],
                quality_score=5.0,
                suggested_use="",
            )

        prompt = VIDEO_ANNOTATION_PROMPT.format(frame_count=0) + (
            f"\n\n**Full scene analysis of this video**:\n{scene_summaries_text[:3000]}\n\n"
            "Distill the above into the catalog entry JSON."
        )

        m = model or _default_vlm_model()
        ep = endpoint or _default_vlm_endpoint()
        key = api_key or _default_vlm_api_key()

        for attempt in range(2):
            try:
                kwargs: dict = dict(
                    model=m,
                    messages=[
                        {"role": "system", "content": VIDEO_ANNOTATION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=1024,
                )
                if ep:
                    kwargs["api_base"] = ep
                if key:
                    kwargs["api_key"] = key
                raw = litellm.completion(**kwargs)
                content = raw.choices[0].message.content
                parsed = _parse_json_strict(content)
                if parsed and isinstance(parsed, dict):
                    return VideoAnnotation(**parsed)
            except Exception:
                if attempt == 1:
                    import traceback
                    traceback.print_exc()
                continue

        return VideoAnnotation(
            summary=f"Video: {metadata.file_name} ({metadata.duration_sec:.0f}s)",
            tags=["analyzed"],
            quality_score=5.0,
        )
    except Exception as e:
        print(f"[AssetAnnotator] Distillation failed for {metadata.file_name}: {e}")
        return VideoAnnotation(
            summary=f"Video: {metadata.file_name} ({metadata.duration_sec:.0f}s)",
            tags=["analyzed"],
            quality_score=5.0,
        )


# ── Image annotator ────────────────────────────────────────────────────────

def annotate_image_asset(
    metadata: ImageAssetMetadata,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> ImageAnnotation:
    """Single image → VLM → ImageAnnotation."""
    import litellm

    model = model or _default_vlm_model()
    endpoint = endpoint or _default_vlm_endpoint()
    api_key = api_key or _default_vlm_api_key()

    # Load and encode image
    b64 = ""
    try:
        from PIL import Image
        from src.utils.media_utils import pil_to_base64
        img = Image.open(metadata.absolute_path)
        img = img.convert("RGB")
        b64 = pil_to_base64(img, quality=85)
    except Exception as e:
        print(f"[AssetAnnotator] Could not open image {metadata.file_name}: {e}")
        return ImageAnnotation(
            summary=f"Image: {metadata.file_name}",
            tags=["unreadable"],
            quality_score=1.0,
        )

    messages = [
        {"role": "system", "content": IMAGE_ANNOTATION_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": IMAGE_ANNOTATION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]},
    ]

    for attempt in range(2):
        try:
            kwargs: dict = dict(
                model=model,
                messages=messages,
                temperature=0.3,
                max_tokens=1024,
            )
            if endpoint:
                kwargs["api_base"] = endpoint
            if api_key:
                kwargs["api_key"] = api_key
            raw = litellm.completion(**kwargs)
            content = raw.choices[0].message.content
            parsed = _parse_json_strict(content)
            if parsed and isinstance(parsed, dict):
                return ImageAnnotation(**parsed)
        except Exception as e:
            if attempt == 1:
                print(f"[AssetAnnotator] Image VLM failed for {metadata.file_name}: {e}")
            continue

    return ImageAnnotation(
        summary=f"Image: {metadata.file_name}",
        tags=["unanalyzed"],
        quality_score=5.0,
    )


# ── Audio annotator ────────────────────────────────────────────────────────

def annotate_audio_asset(
    metadata: AudioAssetMetadata,
    output_dir: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    force: bool = False,
    progress_callback=None,
) -> AudioAnnotation:
    """Delegate to the existing madmom pipeline, then distill to AudioAnnotation.

    Uses coarse segmentation parameters (min 15s, max 45s segments) so the
    analysis runs quickly — only Level 1 structure is needed, not per-segment
    captions.
    """
    model = model or _default_audio_model()
    endpoint = endpoint or _default_audio_endpoint()
    api_key = api_key or _default_audio_api_key()

    # Determine output path for the audio caption JSON
    if output_dir is None:
        output_dir = os.path.join("Output", "asset_index", "audio_captions")
    os.makedirs(output_dir, exist_ok=True)
    caption_path = os.path.join(output_dir, f"{metadata.content_hash}.json")

    # Run the madmom pipeline if the caption doesn't exist
    if not os.path.exists(caption_path) or force:
        try:
            from src.audio.audio_caption_madmom import caption_audio_with_madmom_segments
            from src import config as cfg

            # Override config temporarily for coarse analysis
            # Save originals
            orig_min = getattr(cfg, "AUDIO_MIN_SEGMENT_DURATION", 3.0)
            orig_max = getattr(cfg, "AUDIO_MAX_SEGMENT_DURATION", 30.0)

            try:
                cfg.AUDIO_MIN_SEGMENT_DURATION = 15.0
                cfg.AUDIO_MAX_SEGMENT_DURATION = 45.0

                caption_audio_with_madmom_segments(
                    audio_path=metadata.absolute_path,
                    output_path=caption_path,
                    max_tokens=getattr(cfg, "AUDIO_KEYPOINT_MAX_TOKENS", 4096),
                    temperature=getattr(cfg, "AUDIO_KEYPOINT_TEMPERATURE", 0.7),
                    top_p=getattr(cfg, "AUDIO_KEYPOINT_TOP_P", 0.95),
                    # was 1 — serialized LLM calls made annotation feel stuck
                    max_workers=getattr(cfg, "AUDIO_BATCH_SIZE", 4),
                    progress_callback=progress_callback,
                    detection_methods=getattr(cfg, "AUDIO_DETECTION_METHODS", ["downbeat", "pitch", "mel_energy"]),
                    beats_per_bar=[getattr(cfg, "AUDIO_BEATS_PER_BAR", 4)],
                    min_bpm=getattr(cfg, "AUDIO_MIN_BPM", 60),
                    max_bpm=getattr(cfg, "AUDIO_MAX_BPM", 200),
                    pitch_tolerance=getattr(cfg, "AUDIO_PITCH_TOLERANCE", 0.5),
                    pitch_threshold=getattr(cfg, "AUDIO_PITCH_THRESHOLD", 0.1),
                    pitch_min_distance=getattr(cfg, "AUDIO_PITCH_MIN_DISTANCE", 1.0),
                    pitch_nms_method=getattr(cfg, "AUDIO_PITCH_NMS_METHOD", "gaussian"),
                    pitch_max_points=getattr(cfg, "AUDIO_PITCH_MAX_POINTS", 50),
                    mel_win_s=getattr(cfg, "AUDIO_MEL_WIN_S", 0.1),
                    mel_n_filters=getattr(cfg, "AUDIO_MEL_N_FILTERS", 128),
                    mel_threshold_ratio=getattr(cfg, "AUDIO_MEL_THRESHOLD_RATIO", 0.3),
                    mel_min_distance=getattr(cfg, "AUDIO_MEL_MIN_DISTANCE", 1.0),
                    mel_nms_method=getattr(cfg, "AUDIO_MEL_NMS_METHOD", "gaussian"),
                    mel_max_points=getattr(cfg, "AUDIO_MEL_MAX_POINTS", 50),
                    merge_close=getattr(cfg, "AUDIO_MERGE_CLOSE", 1.0),
                    min_interval=getattr(cfg, "AUDIO_MIN_INTERVAL", 2.0),
                    top_k_keypoints=getattr(cfg, "AUDIO_TOP_K", 30),
                    energy_percentile=getattr(cfg, "AUDIO_ENERGY_PERCENTILE", 80),
                    min_segment_duration=15.0,
                    max_segment_duration=45.0,
                    use_stage1_sections=True,
                    section_min_interval=15.0,
                )
            finally:
                cfg.AUDIO_MIN_SEGMENT_DURATION = orig_min
                cfg.AUDIO_MAX_SEGMENT_DURATION = orig_max
        except Exception as e:
            print(f"[AssetAnnotator] Audio analysis failed for {metadata.file_name}: {e}")
            traceback.print_exc()
            return AudioAnnotation(
                summary=f"Audio: {metadata.file_name} ({metadata.duration_sec:.0f}s)",
                tags=["analysis_failed"],
                quality_score=3.0,
                duration_sec=metadata.duration_sec,
            )

    # Distill the full caption JSON into an AudioAnnotation
    if os.path.exists(caption_path):
        try:
            with open(caption_path, "r", encoding="utf-8") as fh:
                caption_data = json.load(fh)

            overall = caption_data.get("overall_analysis", {})
            sections = caption_data.get("sections", [])

            # Build a compact sections summary
            sec_lines = []
            for sec in sections:
                name = sec.get("name", "")
                start = sec.get("Start_Time", "")
                end = sec.get("End_Time", "")
                if name and start and end:
                    sec_lines.append(f"{name} {start}-{end}")
            sections_summary = ", ".join(sec_lines) if sec_lines else ""

            # Ask LLM to distill the analysis into our compact format
            import litellm
            distill_prompt = AUDIO_ANNOTATION_PROMPT.format(
                audio_analysis_json=json.dumps(
                    {"overall_analysis": overall, "sections_summary": sections_summary},
                    ensure_ascii=False, indent=2,
                )[:3000]  # Truncate to avoid excessive tokens
            )

            for attempt in range(2):
                try:
                    kwargs: dict = dict(
                        model=model,
                        messages=[
                            {"role": "system", "content": AUDIO_ANNOTATION_SYSTEM},
                            {"role": "user", "content": distill_prompt},
                        ],
                        temperature=0.3,
                        max_tokens=1024,
                    )
                    if endpoint:
                        kwargs["api_base"] = endpoint
                    if api_key:
                        kwargs["api_key"] = api_key
                    raw = litellm.completion(**kwargs)
                    content = raw.choices[0].message.content
                    parsed = _parse_json_strict(content)
                    if parsed and isinstance(parsed, dict):
                        parsed["duration_sec"] = metadata.duration_sec
                        # LLM-guessed bpm is unreliable (anchors on genre clichés);
                        # override with the madmom-measured felt pulse
                        _bpm = _measured_bpm_from_caption(caption_data, metadata.content_hash)
                        if _bpm:
                            parsed["bpm"] = _bpm
                        # BOTH representations are kept: the LLM's prose telling
                        # of the musical journey moves to structure_notes; the
                        # MEASURED "Name start-end, ..." string owns
                        # sections_summary (the UI timeline parses it — an LLM
                        # rewrite into prose used to silently kill that view)
                        _llm_sections = str(parsed.get("sections_summary") or "").strip()
                        if _llm_sections and _llm_sections != sections_summary \
                                and not parsed.get("structure_notes"):
                            parsed["structure_notes"] = _llm_sections
                        if sections_summary:
                            parsed["sections_summary"] = sections_summary
                        return AudioAnnotation(**parsed)
                except Exception:
                    if attempt == 1:
                        traceback.print_exc()
                    continue

            # Fallback: build from caption data directly
            return AudioAnnotation(
                summary=overall.get("summary", f"Audio: {metadata.file_name}"),
                genre="",
                emotion="",
                energy_level="medium",
                bpm=_measured_bpm_from_caption(caption_data, metadata.content_hash) or 0.0,
                sections_summary=sections_summary,
                tags=[],
                quality_score=5.0,
                suggested_use="",
                duration_sec=metadata.duration_sec,
            )
        except Exception as e:
            print(f"[AssetAnnotator] Failed to load audio caption: {e}")

    return AudioAnnotation(
        summary=f"Audio: {metadata.file_name} ({metadata.duration_sec:.0f}s)",
        tags=["unanalyzed"],
        quality_score=5.0,
        duration_sec=metadata.duration_sec,
    )


# ── Dispatcher ─────────────────────────────────────────────────────────────

def annotate_asset(
    metadata,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    progress_callback=None,
    variant: str = "",
) -> AssetAnnotation:
    """Route to the correct annotator based on asset_type."""
    m = model or _default_vlm_model()
    ep = endpoint or _default_vlm_endpoint()
    key = api_key or _default_vlm_api_key()

    if isinstance(metadata, VideoAssetMetadata):
        ann = annotate_video_asset(metadata, model=m, endpoint=ep, api_key=key,
                                   progress_callback=progress_callback, variant=variant)
    elif isinstance(metadata, ImageAssetMetadata):
        ann = annotate_image_asset(metadata, model=m, endpoint=ep, api_key=key)
    elif isinstance(metadata, AudioAssetMetadata):
        ann = annotate_audio_asset(metadata, model=m, endpoint=ep, api_key=key,
                                   progress_callback=progress_callback)
    else:
        raise TypeError(f"Unknown asset type: {type(metadata)}")

    return AssetAnnotation(
        content_hash=metadata.content_hash,
        file_path=metadata.file_path,
        asset_type=metadata.asset_type,
        metadata=metadata,
        annotation=ann,
        model_used=m,
        model_endpoint=ep,
    )


def _annotate_video_in_process(meta, model, endpoint, api_key, q=None):
    """Child-process entry point: annotate ONE video end-to-end.

    Separate processes (not threads) because decord readers are not
    thread-safe and the GIL serializes CPU-bound decode anyway. Stage events
    stream back to the parent through the queue for live per-card UI."""
    try:
        if q is not None:
            try:
                q.put({"type": "begin", "file": meta.file_name, "hash": meta.content_hash})
            except Exception:
                pass

        def _cb(stage, status, detail):
            if q is not None:
                try:
                    q.put({"type": "stage", "file": meta.file_name, "hash": meta.content_hash,
                           "stage": stage, "status": status, "detail": str(detail or "")[:80]})
                except Exception:
                    pass
        return annotate_asset(meta, model=model, endpoint=endpoint, api_key=api_key,
                              progress_callback=_cb)
    except Exception:
        traceback.print_exc()
        return None


def _annotate_audio_subprocess(meta, stage_cb=None):
    """Annotate ONE audio track in a PLAIN python subprocess.

    multiprocessing children re-import the parent's __main__ on Windows —
    for the web backend that's server/main.py with its whole heavy import
    graph, and madmom + numpy/LAPACK on top of it aborts natively
    (0xc06d007f). A `python -m` subprocess starts clean, same as the
    pipeline subprocess where this exact code is proven stable (铁律10).

    Returns the AssetAnnotation, or None on failure (resumable)."""
    import pickle
    import subprocess
    import sys as _sys
    import tempfile

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    with tempfile.TemporaryDirectory(prefix="aud_ann_") as td:
        meta_p = os.path.join(td, "meta.pkl")
        res_p = os.path.join(td, "result.pkl")
        with open(meta_p, "wb") as f:
            pickle.dump(meta, f)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # 0xC06D007F at compute_audio_facts = two OpenMP runtimes loaded
        # (madmom's numpy/MKL + librosa's numba both pull one in). The
        # classic Windows escape hatch — scoped to THIS worker only:
        env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
        proc = subprocess.Popen(
            [_sys.executable, "-m", "src.asset_manager.audio_annotate_worker", meta_p, res_p],
            cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@ASTAGE "):
                try:
                    ev = json.loads(line[len("@@ASTAGE "):])
                    if stage_cb:
                        stage_cb(ev.get("stage", ""), ev.get("status", ""), ev.get("detail", ""))
                except Exception:  # noqa: BLE001
                    pass
            elif line:
                print(f"  [audio-worker] {line}")
        rc = proc.wait()
        if rc == 0 and os.path.exists(res_p):
            with open(res_p, "rb") as f:
                return pickle.load(f)
        print(f"[AssetAnnotator] audio worker exited rc={rc} for {meta.file_name}")
        return None


def batch_annotate(
    new_assets: list,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    stage_callback: Optional[Callable[[str, str, str], None]] = None,
    start_callback: Optional[Callable[[str], None]] = None,
    max_concurrent: int = 3,
    model: str | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> list[AssetAnnotation]:
    """Annotate multiple assets with concurrency control.

    Videos run one at a time (decord is not thread-safe across videos).
    Images can run in parallel.
    Audio runs sequentially (madmom is not thread-safe).
    """
    if not new_assets:
        return []

    videos = [a for a in new_assets if isinstance(a, VideoAssetMetadata)]
    images = [a for a in new_assets if isinstance(a, ImageAssetMetadata)]
    audios = [a for a in new_assets if isinstance(a, AudioAssetMetadata)]

    total = len(new_assets)
    completed = 0
    results: list[AssetAnnotation] = []

    def _report(fn: str):
        nonlocal completed
        completed += 1
        if progress_callback:
            progress_callback(completed, total, fn)

    def _starting(fn: str):
        # progress_callback fires on COMPLETION only — without a start signal
        # the UI cannot know which file is actually being worked on (a 1-file
        # job would show no activity at all until it finished)
        if start_callback:
            try:
                start_callback(fn)
            except Exception:
                pass

    # Videos: parallel PROCESSES when configured (decord isn't thread-safe and
    # the GIL blocks CPU-bound threads — processes give true file-level
    # parallelism); sequential fallback otherwise.
    try:
        from src import config as _cfg
        _video_workers = max(1, int(getattr(_cfg, "ANNOTATE_VIDEO_WORKERS", 1) or 1))
    except Exception:
        _video_workers = 1

    if videos and _video_workers > 1 and len(videos) > 1:
        import multiprocessing as _mp
        import threading as _threading
        from concurrent.futures import ProcessPoolExecutor as _PPE

        _mgr = _mp.Manager()
        _q = _mgr.Queue()
        _stop = _threading.Event()

        def _drain():
            while not (_stop.is_set() and _q.empty()):
                try:
                    ev = _q.get(timeout=0.5)
                except Exception:
                    continue
                try:
                    if ev.get("type") == "begin":
                        _starting(ev.get("file", ""))
                    elif ev.get("type") == "stage" and stage_callback:
                        try:
                            stage_callback(ev["stage"], ev["status"], ev["detail"],
                                           filename=ev.get("file"))
                        except TypeError:
                            stage_callback(ev["stage"], ev["status"], ev["detail"])
                except Exception:
                    pass

        _dt = _threading.Thread(target=_drain, daemon=True)
        _dt.start()
        print(f"[AssetAnnotator] Annotating {len(videos)} videos in {min(_video_workers, len(videos))} parallel processes")
        try:
            with _PPE(max_workers=min(_video_workers, len(videos))) as _ex:
                _futs = {_ex.submit(_annotate_video_in_process, m, model, endpoint, api_key, _q): m
                         for m in videos}
                for _fut in as_completed(_futs):
                    _m = _futs[_fut]
                    _failed_video = False
                    try:
                        _r = _fut.result()
                        if _r is not None:
                            results.append(_r)
                        else:
                            _failed_video = True
                    except Exception as e:
                        print(f"[AssetAnnotator] video worker failed for {_m.file_name}: {e}")
                        _failed_video = True
                    if _failed_video and stage_callback:
                        try:
                            stage_callback("annotate", "fail", "analysis failed — resumable", filename=_m.file_name)
                        except TypeError:
                            pass
                    _report(_m.file_name)
        finally:
            _stop.set()
            _dt.join(timeout=3)
    else:
        for meta in videos:
            _starting(meta.file_name)
            try:
                result = annotate_asset(meta, model=model, endpoint=endpoint, api_key=api_key, progress_callback=stage_callback)
                results.append(result)
            except Exception as e:
                print(f"[AssetAnnotator] Failed to annotate video {meta.file_name}: {e}")
                if stage_callback:
                    try:
                        stage_callback("annotate", "fail", str(e)[:120], filename=meta.file_name)
                    except TypeError:
                        stage_callback("annotate", "fail", str(e)[:120])
            _report(meta.file_name)

    # Images: parallel in small batches
    if images:
        batch_size = min(max_concurrent, len(images))
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            def _run_image(meta):
                _starting(meta.file_name)
                return annotate_asset(meta, model, endpoint, api_key, stage_callback)
            futures = {executor.submit(_run_image, meta): meta for meta in images}
            for future in as_completed(futures):
                meta = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    print(f"[AssetAnnotator] Failed to annotate image {meta.file_name}: {e}")
                _report(meta.file_name)

    # Audio: sequential, each track in its OWN plain `python -m` subprocess.
    # NOT multiprocessing: on Windows its children re-import the parent's
    # __main__ (server/main.py + the whole heavy import graph), and madmom +
    # numpy/LAPACK on top of that aborts natively — verified: the identical
    # code completes fine in a clean plain process (铁律10).
    for meta in audios:
        _starting(meta.file_name)
        _failed = False

        def _scb(stage, status, detail, _fn=meta.file_name):
            if stage_callback:
                try:
                    stage_callback(stage, status, detail, filename=_fn)
                except TypeError:
                    stage_callback(stage, status, detail)

        try:
            _r = _annotate_audio_subprocess(meta, stage_cb=_scb)
            if _r is not None:
                results.append(_r)
            else:
                _failed = True
        except Exception as e:
            print(f"[AssetAnnotator] audio worker failed for {meta.file_name}: {e}")
            _failed = True
        if _failed and stage_callback:
            try:
                stage_callback("annotate", "fail", "analysis failed — resumable", filename=meta.file_name)
            except TypeError:
                pass
        _report(meta.file_name)

    return results
