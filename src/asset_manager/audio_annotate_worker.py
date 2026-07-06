"""Plain-subprocess worker: annotate ONE audio track in a truly clean process.

Why not multiprocessing: on Windows, a ProcessPoolExecutor child re-imports
the parent's __main__ module — for the web backend that is server/main.py,
which drags the server's whole heavy import graph (torch et al.) into the
"isolated" child. madmom + numpy/LAPACK on top of that aborts natively
(0xc06d007f), which is exactly what the isolation was supposed to prevent.
A `python -m` subprocess starts from THIS module: clean DLL slate, same as
the pipeline subprocess where the identical code is proven to work.

Protocol:
  argv: <meta_pickle_in> <result_pickle_out>
  stdout: "@@ASTAGE {json}" lines = live stage events (stage/status/detail)
  exit 0 + result pickle written = success; anything else = failure
"""
import json
import pickle
import sys


def main() -> int:
    # 0xC06D007F is the Windows DELAY-LOAD failure code: librosa/numba's
    # native deps live in conda's Library\bin, which is NOT on PATH when the
    # server spawns us — the lazy `import librosa` inside compute_audio_facts
    # then aborts natively (uncatchable). _ensure_ffmpeg_on_path prepends
    # exactly those conda dirs; call it BEFORE any heavy import.
    from src.analyzer import _ensure_ffmpeg_on_path
    _ensure_ffmpeg_on_path()
    try:
        import librosa  # noqa: F401  # warm the native stack early
    except Exception:  # noqa: BLE001
        pass

    meta_in, result_out = sys.argv[1], sys.argv[2]
    with open(meta_in, "rb") as f:
        meta = pickle.load(f)

    def _cb(stage, status, detail):
        try:
            print("@@ASTAGE " + json.dumps(
                {"stage": stage, "status": status, "detail": str(detail or "")[:80]},
                ensure_ascii=False), flush=True)
        except Exception:  # noqa: BLE001
            pass

    from src.asset_manager.annotator import annotate_asset
    result = annotate_asset(meta, progress_callback=_cb)
    with open(result_out, "wb") as f:
        pickle.dump(result, f)
    return 0


if __name__ == "__main__":
    try:
        for _s in (sys.stdout, sys.stderr):
            _r = getattr(_s, "reconfigure", None)
            if callable(_r):
                _r(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
