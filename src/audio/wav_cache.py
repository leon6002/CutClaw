"""Location helper for derived (converted) WAV files.

Audio analysis needs RIFF/WAV input for aubio. Converted copies used to be
written next to the source file, which polluted the user's asset folder and
got re-scanned as new assets. They now live under ``Output/tmp_wav/``.
"""

import hashlib
import os
from pathlib import Path


def derived_wav_path(src: Path, tag: str = "") -> Path:
    """Return the cache path for a converted wav of *src*.

    The name embeds an md5 of the absolute source path so different files
    with the same stem never collide.
    """
    try:
        from src import config
        base = Path(getattr(config, "VIDEO_DATABASE_FOLDER", "./Output/"))
    except Exception:
        base = Path("./Output/")
    cache_dir = base / "tmp_wav"
    cache_dir.mkdir(parents=True, exist_ok=True)

    key = hashlib.md5(str(src.resolve()).encode("utf-8")).hexdigest()[:12]
    suffix = f"_{tag}" if tag else ""
    return cache_dir / f"{src.stem}_{key}{suffix}.wav"
