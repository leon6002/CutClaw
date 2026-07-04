"""Utilities for combining several source videos into a single timeline.

The editing pipeline operates on ONE source video (a single continuous
timeline of shot timestamps). To support editing from multiple selected
clips, we transparently concatenate them into one cached file first.

This module is intentionally dependency-light (no torch / decord) so it can
be imported from both the Streamlit UI (app.py) and the CLI (local_run.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import List, Optional, Sequence, Tuple

DEFAULT_MERGED_DIR = os.path.join("resource", "video")


def _clean(video_paths: Optional[Sequence[str]]) -> List[str]:
    return [str(p).strip() for p in (video_paths or []) if p and str(p).strip()]


def compute_merged_video_id(video_paths: Sequence[str]) -> str:
    """Deterministic id for an ordered set of source videos.

    Order matters (it is the montage order), so it is part of the hash.
    Uses basenames only so the id is stable regardless of cwd / absolute
    vs relative paths.
    """
    key = "||".join(os.path.basename(p) for p in video_paths)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    return f"merged_{digest}"


def plan_effective_video_path(
    video_paths: Optional[Sequence[str]],
    merged_dir: str = DEFAULT_MERGED_DIR,
) -> str:
    """Return the single effective video path WITHOUT doing any work.

    - 0 inputs  -> "" (caller decides)
    - 1 input   -> that path
    - N inputs  -> deterministic merged file path (may not exist yet)
    """
    paths = _clean(video_paths)
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    return os.path.join(merged_dir, compute_merged_video_id(paths) + ".mp4")


def _probe_video(path: str) -> Tuple[int, int, float]:
    """Return (width, height, fps) of the first video stream via ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    stream = (json.loads(out).get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    fps = 30.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key)
        if rate and rate != "0/0":
            num, _, den = rate.partition("/")
            try:
                den_val = float(den) if den else 1.0
                if den_val:
                    fps = float(num) / den_val
                    break
            except ValueError:
                continue
    if width <= 0 or height <= 0:
        raise ValueError(f"Could not probe dimensions for video: {path}")
    return width, height, round(fps, 3)


def concat_videos(video_paths: Sequence[str], output_path: str) -> str:
    """Concatenate multiple videos into one, normalizing to the first clip's
    resolution/fps. Re-encodes for robustness against mixed codecs, and adds a
    silent stereo audio track so downstream audio extraction never fails.

    Returns the output path.
    """
    paths = _clean(video_paths)
    if not paths:
        raise ValueError("concat_videos requires at least one input path")
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"Source video not found: {p}")

    width, height, fps = _probe_video(paths[0])
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    inputs: List[str] = []
    for p in paths:
        inputs += ["-i", p]
    # Silent audio source (input index == len(paths))
    inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    n = len(paths)
    filters: List[str] = []
    labels: List[str] = []
    for idx in range(n):
        filters.append(
            f"[{idx}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{idx}]"
        )
        labels.append(f"[v{idx}]")
    filters.append("".join(labels) + f"concat=n={n}:v=1:a=0[outv]")
    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", f"{n}:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        output_path,
    ]

    print(f"🎞️  Concatenating {n} videos -> {output_path} ({width}x{height}@{fps}fps)")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg concat failed:\n" + (result.stderr or "")[-2000:]
        )
    print(f"✅ Merged video saved: {output_path}")
    return output_path


def resolve_video_input(
    video_paths: Optional[Sequence[str]],
    merged_dir: str = DEFAULT_MERGED_DIR,
) -> str:
    """Return a single usable video path, concatenating on demand.

    - 1 input  -> returned as-is.
    - N inputs -> concatenated into a deterministic cached file (reused if it
      already exists, so re-runs skip the merge).
    """
    paths = _clean(video_paths)
    if not paths:
        raise ValueError("resolve_video_input requires at least one input path")
    if len(paths) == 1:
        return paths[0]
    out = plan_effective_video_path(paths, merged_dir)
    if not os.path.exists(out):
        concat_videos(paths, out)
    else:
        print(f"♻️  Reusing cached merged video: {out}")
    return out


def create_slideshow_video(
    image_paths: list[str],
    duration_per_image: float = 3.0,
    output_path: str | None = None,
    fps: int = 30,
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Create a video from a sequence of still images using ffmpeg.

    Each image is displayed for *duration_per_image* seconds with a smooth
    zoom effect (Ken Burns style). Images are scaled to fill the frame
    using center-crop.

    Args:
        image_paths: Absolute paths to images, in desired order.
        duration_per_image: Seconds each image is displayed.
        output_path: Where to save the video. Auto-generated if None.
        fps: Output frame rate.
        width: Output video width.
        height: Output video height.

    Returns:
        Absolute path to the created video file, or "" on failure.
    """
    import hashlib
    import subprocess
    import sys

    if not image_paths:
        return ""

    # Deterministic output name based on input paths
    if output_path is None:
        key = "|".join(os.path.basename(p) for p in image_paths) + f"|{duration_per_image}"
        h = hashlib.md5(key.encode()).hexdigest()[:10]
        output_path = os.path.join("resource", "video", f"slideshow_{h}.mp4")

    output_path = os.path.abspath(output_path)

    if os.path.exists(output_path):
        print(f"♻️  Reusing cached slideshow: {output_path}")
        return output_path

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Ensure ffmpeg is on PATH (same logic as local_run.py)
    project_ffmpeg = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "ffmpeg"
    )
    env = os.environ.copy()
    candidates = [
        project_ffmpeg,
        os.path.join(sys.prefix, "Library", "bin"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            env["PATH"] = c + os.pathsep + env.get("PATH", "")

    # Build a concat file listing each image
    concat_list_path = output_path + ".concat.txt"
    try:
        with open(concat_list_path, "w", encoding="utf-8") as fh:
            for p in image_paths:
                abs_p = os.path.abspath(p)
                fh.write(f"file '{abs_p}'\n")
                fh.write(f"duration {duration_per_image}\n")
            # Repeat last image entry for ffmpeg concat quirk
            if image_paths:
                fh.write(f"file '{os.path.abspath(image_paths[-1])}'\n")

        # For static slideshow with zoom effect per image, we need filter_complex.
        # Simpler approach: concat images → video with zoompan filter per input.
        # Build filter_complex: each input gets a zoompan, then concat.
        n = len(image_paths)
        filter_parts = []
        for i in range(n):
            # zoompan: smooth zoom from 1.0 to 1.15 over the image duration
            filter_parts.append(
                f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                f"zoompan=z='min(zoom+0.0008,1.15)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
                f"trim=duration={duration_per_image},setpts=PTS-STARTPTS[v{i}];"
            )

        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        filter_str = "".join(filter_parts) + f"{concat_inputs}concat=n={n}:v=1:a=0,format=yuv420p[vout]"

        cmd = [
            "ffmpeg",
            "-y",
        ]
        for p in image_paths:
            cmd += ["-loop", "1", "-i", os.path.abspath(p)]
        cmd += [
            "-filter_complex", filter_str,
            "-map", "[vout]",
            "-t", str(n * duration_per_image),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

        print(f"🎞️  Creating slideshow: {n} images → {output_path} "
              f"({n * duration_per_image:.0f}s @ {fps}fps {width}x{height})")

        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
        if result.returncode != 0:
            print(f"❌ Slideshow ffmpeg error:\n{result.stderr[-500:]}")
            return ""
    except Exception as e:
        print(f"❌ Failed to create slideshow: {e}")
        return ""
    finally:
        if os.path.exists(concat_list_path):
            os.unlink(concat_list_path)

    print(f"✅ Slideshow saved: {output_path}")
    return output_path
