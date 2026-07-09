"""CutClaw web backend — FastAPI wrapper around the existing pipeline.

Run:  python server/main.py   (from project root, inside the cutclaw env)
Serves the built React UI from web/dist at http://127.0.0.1:8765
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

# Force UTF-8 stdout/stderr so the server's own print() never crashes on non-GBK
# characters (ø/é/中文 in paths, project names) on a Windows GBK console.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Point LiteLLM at the local cost map (no GitHub fetch) — must run before any
# `import litellm` in this process. See src/utils/litellm_local.py.
import src.utils.litellm_local  # noqa: F401,E402

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.utils.env_keys import env_expr_name, resolve_env_expr, write_env_var
from src.utils.ui_state import UI_STATE_KEYS, read_state, write_state

# env-backed secrets (cfg() resolves os.getenv exprs) need .env in THIS
# process from the start — previously they only worked after some request
# happened to lazily import src.config, which load_dotenv's as a side effect
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

CONFIG_PATH = os.path.join(PROJECT_ROOT, "src", "config.py")

app = FastAPI(title="CutClaw API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Config helpers (regex-based, same as Streamlit UI) ─────────────────────

def _read_config() -> dict:
    vals = {}
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(CONFIG_PATH, "r", encoding=enc) as f:
                for line in f:
                    m = re.match(r'^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)', line)
                    if m:
                        vals[m.group(1)] = m.group(2).strip()
            return vals
        except UnicodeDecodeError:
            vals.clear()
    return vals


def cfg(key: str, fallback: str = "") -> str:
    # UI-remembered inputs live in Output/ui_state.json, not the tracked config.py.
    if key in UI_STATE_KEYS:
        v = read_state(key)
        if v is not None:
            return v
    raw = _read_config().get(key, fallback)
    resolved = resolve_env_expr(raw)
    if resolved is not None:
        return resolved
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def save_config(key: str, value: str):
    # UI-remembered input → gitignored state file, never back into config.py.
    if key in UI_STATE_KEYS:
        write_state(key, value)
        return
    # Env-backed secret (KEY = os.getenv(...)): write the value into .env so
    # the literal key never lands in the git-tracked config.py.
    env_name = env_expr_name(_read_config().get(key, ""))
    if env_name:
        if value != os.getenv(env_name, ""):
            write_env_var(env_name, value)
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        float(value)
        new_val = value
    except ValueError:
        # bools/None must stay bare literals — a quoted "False" is truthy
        new_val = value if value in ("True", "False", "None") \
            else json.dumps(value, ensure_ascii=False)
    pattern = rf'^({re.escape(key)}\s*=\s*).*'
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, lambda m: f"{m.group(1)}{new_val}", content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n\n{key} = {new_val}\n"
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def _resolve(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def _reload_runtime_config():
    """Reload config-derived modules so in-process jobs pick up model switches.

    save_config() edits config.py on disk, but already-imported modules cache
    values (src.config attributes, litellm_client's AUDIO_* constants). Reload
    mutates the module objects in place, so every existing reference updates.
    """
    import importlib
    try:
        import src.config as _c
        importlib.reload(_c)
    except Exception:
        pass
    try:
        import src.audio.litellm_client as _lc
        importlib.reload(_lc)
    except Exception:
        pass


# ── Job registry (annotate / pipeline / render run as jobs) ────────────────

JOBS_DIR = os.path.join(PROJECT_ROOT, "Output", "jobs")


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.lines: list[str] = []
        self.status = "running"          # running | done | error
        self.returncode: int | None = None
        self.meta: dict = {}
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self._last_save = 0.0

    def add(self, line: str):
        with self.lock:
            self.lines.append(line)
        self.save()

    def save(self, force: bool = False):
        """Persist a snapshot so job views survive backend restarts (throttled)."""
        now = time.time()
        if not force and now - self._last_save < 3.0:
            return
        self._last_save = now
        try:
            os.makedirs(JOBS_DIR, exist_ok=True)
            with self.lock:
                data = {
                    "id": self.id, "kind": self.kind, "status": self.status,
                    "returncode": self.returncode, "meta": self.meta,
                    "lines": self.lines[-3000:], "saved_at": now,
                }
                payload = json.dumps(data, ensure_ascii=False)
            with open(os.path.join(JOBS_DIR, f"{self.id}.json"), "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception:
            pass

    def to_dict(self, since: int = 0):
        import copy
        with self.lock:
            # traces (full agent step details) are fetched on demand, not polled
            meta_light = {k: v for k, v in self.meta.items() if k != "traces"}
            return {
                "id": self.id, "kind": self.kind, "status": self.status,
                "returncode": self.returncode, "meta": copy.deepcopy(meta_light),
                "lines": self.lines[since:], "total": len(self.lines),
            }


JOBS: dict[str, Job] = {}
PIPELINE_JOB_ID: str | None = None


def _load_persisted_jobs():
    """Restore job snapshots after a backend restart (views survive; keep last 12)."""
    global PIPELINE_JOB_ID
    if not os.path.isdir(JOBS_DIR):
        return
    files = []
    for fn in os.listdir(JOBS_DIR):
        if fn.endswith(".json"):
            fp = os.path.join(JOBS_DIR, fn)
            try:
                files.append((os.path.getmtime(fp), fp))
            except OSError:
                pass
    files.sort()
    for _, fp in files[:-12]:   # prune old snapshots
        try:
            os.remove(fp)
        except OSError:
            pass
    for _, fp in files[-12:]:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f)
            job = Job(d.get("kind", "job"))
            job.id = d.get("id") or job.id
            job.lines = d.get("lines", [])
            job.meta = d.get("meta", {})
            job.returncode = d.get("returncode")
            st = d.get("status", "done")
            if st == "running":
                st = "error"
                job.lines.append(
                    "[服务重启] 任务跟踪中断 — 子进程可能仍在后台完成并把结果写盘；重跑会走缓存跳过已完成部分。")
            job.status = st
            JOBS[job.id] = job
            if job.kind == "pipeline":
                PIPELINE_JOB_ID = job.id   # files sorted old→new, last wins
        except Exception:
            pass


_load_persisted_jobs()


def _reader_thread(job: Job, on_line=None):
    try:
        for line in job.proc.stdout:
            line = line.rstrip()
            if on_line:
                on_line(job, line)
            if "@@PROGRESS" not in line:   # progress events go to meta, not the log
                job.add(line)
    finally:
        job.proc.wait()
        job.returncode = job.proc.returncode
        job.status = "done" if job.proc.returncode == 0 else "error"
        pid = job.meta.get("project_id")
        if pid:
            try:
                p = _load_project(pid)
                p["last_run_status"] = job.status
                _save_project(p)
            except Exception:
                pass
        job.save(force=True)


def _spawn(job: Job, cmd: list[str], on_line=None):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    job.proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        bufsize=1, cwd=PROJECT_ROOT, env=env, **kwargs)
    threading.Thread(target=_reader_thread, args=(job, on_line), daemon=True).start()
    JOBS[job.id] = job


def _kill(job: Job):
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
    job.status = "error"
    job.add("■ Stopped by user.")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, since: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_dict(since)


@app.get("/api/jobs/current/{kind}")
def current_job_of_kind(kind: str):
    """Latest running job of a kind — lets the UI reattach after a page refresh."""
    for job in reversed(list(JOBS.values())):
        if job.kind == kind and job.status == "running":
            return {"job": {"id": job.id, "kind": job.kind, "status": job.status}}
    return {"job": None}


@app.get("/api/jobs/latest/{kind}")
def latest_job_of_kind(kind: str):
    """Most recent job of a kind regardless of status — surfaces batches that
    were killed by a backend restart (their assets silently stay 未标注
    otherwise). For annotate jobs, include how many files never finished."""
    for job in reversed(list(JOBS.values())):
        if job.kind != kind:
            continue
        info: dict = {"id": job.id, "kind": job.kind, "status": job.status}
        files = (job.meta or {}).get("files") or {}
        if files:
            info["unfinished"] = sum(1 for v in files.values() if v in ("p", "r"))
            info["total"] = len(files)
        return {"job": info}
    return {"job": None}


@app.get("/api/jobs/{job_id}/traces")
def get_job_traces(job_id: str):
    """ALL unit traces, slimmed for the canvas (one poll instead of N).

    Heavy fields (args/reply) are stripped to flags + snippets; the canvas
    fetches full detail for a single unit via /trace when a node expands.
    """
    import copy
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    out: dict = {}
    with job.lock:
        for task, units in (job.meta.get("traces", {}) or {}).items():
            tu: dict = {}
            for idx, steps in units.items():
                slim = []
                for s in steps:
                    t = {k: s.get(k) for k in ("phase", "iter", "max_iter", "elapsed", "tool", "verdict", "note") if s.get(k) is not None}
                    r = s.get("result")
                    if r:
                        t["result"] = str(r)[:160]
                    if s.get("args"):
                        t["has_args"] = True
                    if s.get("reply"):
                        t["has_reply"] = True
                    slim.append(t)
                tu[idx] = slim
            out[task] = tu
        out = copy.deepcopy(out)
    return {"traces": out}


@app.get("/api/jobs/{job_id}/trace")
def get_job_trace(job_id: str, task: str, idx: int):
    """Full agent step trace for one work unit (model replies, tool args).

    Checkpoint-skipped units have no steps in the current run — fall back to
    the most recent earlier job that traced the same unit.
    """
    import copy
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    with job.lock:
        steps = copy.deepcopy(job.meta.get("traces", {}).get(task, {}).get(str(idx), []))
    if steps:
        return {"steps": steps, "from_previous_run": False}

    cur_sp = job.meta.get("shot_point")
    for other in reversed(list(JOBS.values())):
        if other.id == job_id or other.kind != job.kind:
            continue
        # same pipeline result target (when known) — avoids cross-project mixups
        if cur_sp and other.meta.get("shot_point") and other.meta.get("shot_point") != cur_sp:
            continue
        with other.lock:
            s2 = copy.deepcopy(other.meta.get("traces", {}).get(task, {}).get(str(idx), []))
        if s2:
            return {"steps": s2, "from_previous_run": True}
    return {"steps": [], "from_previous_run": False}


# ── Config endpoints ────────────────────────────────────────────────────────

CONFIG_KEYS = [
    "VISION_POOL_REF", "AUDIO_POOL_REF", "AGENT_POOL_REF",
    "VIDEO_PATH", "AUDIO_PATH", "INSTRUCTION", "SRT_PATH", "MAIN_CHARACTER_NAME",
    "ASSET_ROOT_DIR", "ASSET_IMAGE_DURATION_SEC",
    "AUDIO_SEGMENT_MIN_DURATION_SEC", "AUDIO_SEGMENT_MAX_DURATION_SEC",
    "AUDIO_MIN_SEGMENT_DURATION", "AUDIO_MAX_SEGMENT_DURATION",
    "VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY",
    "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY",
    "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY",
    # concurrency knobs — maximize hardware/API utilization during annotation
    "ANNOTATE_VIDEO_WORKERS", "CAPTION_BATCH_SIZE", "VIDEO_CAPTION_MAX_FRAMES",
    "AUDIO_BATCH_SIZE",
    # Immich integration
    "IMMICH_URL", "IMMICH_API_KEY", "IMMICH_PATH_MAP",
    # pipeline tuning (参数设置 UI)
    "SOUND_HIGHLIGHT_THRESHOLD",
    "STABILITY_CHECK_ENABLED", "STABILITY_MIN_SCORE",
    "AGENT_MAX_ITERATIONS", "PARALLEL_SHOT_MAX_WORKERS", "PARALLEL_SHOT_MAX_RERUNS",
    "SHOT_MIN_GAP_SEC", "ALLOW_DURATION_TOLERANCE", "MIN_ACCEPTABLE_SHOT_DURATION",
    "MAX_SHOTS_PER_CLIP",
    # visual dedup / anchor budget (LOGIC.md §14)
    "VISUAL_CLUSTER_MAX_USES", "SOURCE_VIDEO_MAX_USES",
    "VISUAL_CLUSTER_HAMMING", "VISUAL_CLUSTER_MIN_GAP_SHOTS",
    # voice-highlight ducking (render mix)
    "DUCK_BGM_LEVEL", "DUCK_VOICE_LEVEL", "DUCK_MERGE_GAP_SEC",
]


@app.get("/api/config")
def get_config():
    return {k: cfg(k, "") for k in CONFIG_KEYS}


class ConfigUpdate(BaseModel):
    values: dict[str, str]


@app.put("/api/config")
def put_config(body: ConfigUpdate):
    for k, v in body.values.items():
        if k in CONFIG_KEYS:
            save_config(k, v)
    return {"ok": True}


@app.get("/api/api-pool")
def get_api_pool():
    p = os.path.join(PROJECT_ROOT, "src", "api_pool.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


class PoolSave(BaseModel):
    pool: list


_ROLE_CONFIG_KEYS = [
    ("VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"),
    ("AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"),
    ("AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"),
]


@app.put("/api/api-pool")
def save_api_pool(body: PoolSave):
    p = os.path.join(PROJECT_ROOT, "src", "api_pool.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(body.pool, f, ensure_ascii=False, indent=2)

    # The role→entry REFERENCE is authoritative; the config triplets are just a
    # materialized cache for subprocess/legacy readers. Re-materialize them from
    # the (possibly edited) pool on every save so they can never drift.
    _ROLE_REFS = [
        ("VISION_POOL_REF", "VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY"),
        ("AUDIO_POOL_REF", "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY"),
        ("AGENT_POOL_REF", "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"),
    ]
    by_name = {(e.get("name") or e.get("model") or ""): e for e in body.pool}
    synced = []
    for rk, mk, ek, kk in _ROLE_REFS:
        e = by_name.get(cfg(rk, ""))
        if not e:
            # legacy fallback: no ref stored yet — match by model+endpoint
            cur_m, cur_e = cfg(mk, ""), cfg(ek, "")
            e = next((x for x in body.pool
                      if (x.get("model") or "") == cur_m
                      and (x.get("endpoint") or x.get("api_base") or "") == cur_e), None)
            if not e:
                continue
        vals = {
            mk: e.get("model", "") or "",
            ek: e.get("endpoint") or e.get("api_base") or "",
            kk: e.get("api_key", "") or "",
        }
        for k, v in vals.items():
            if cfg(k, "") != v:
                save_config(k, v)
                if rk not in synced:
                    synced.append(rk)
    return {"ok": True, "count": len(body.pool), "synced_roles": synced}


class PoolTestRequest(BaseModel):
    model: str
    endpoint: str = ""
    api_key: str = ""


@app.post("/api/api-pool/test")
def api_pool_test(body: PoolTestRequest):
    """Fire a tiny completion at the given model config and report latency."""
    import time as _t
    import litellm

    if not body.model.strip():
        raise HTTPException(400, "model 不能为空")
    kwargs: dict = dict(
        model=body.model,
        messages=[{"role": "user", "content": "Reply with exactly one word: pong"}],
        max_tokens=1024,   # reasoning models need headroom for thought tokens
        timeout=30,
    )
    if body.endpoint.strip():
        kwargs["api_base"] = body.endpoint.strip()
    if body.api_key.strip():
        kwargs["api_key"] = body.api_key.strip()

    t0 = _t.time()
    try:
        r = litellm.completion(**kwargs)
        msg = r.choices[0].message
        content = (msg.content or getattr(msg, "reasoning_content", None) or "").strip()
        usage = getattr(r, "usage", None)
        return {
            "ok": True,
            "latency_s": round(_t.time() - t0, 2),
            "reply": content[:200],
            "tokens": getattr(usage, "total_tokens", None) if usage else None,
        }
    except Exception as e:
        return {"ok": False, "latency_s": round(_t.time() - t0, 2), "error": str(e)[:300]}


# ── Resource file listing ───────────────────────────────────────────────────

_EXTS = {
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
    "srt": {".srt", ".vtt", ".ass", ".ssa"},
}
_DIRS = {"video": "resource/video", "audio": "resource/audio", "srt": "resource/subtitle"}


@app.get("/api/files")
def list_files(kind: str = Query(..., pattern="^(video|audio|srt)$")):
    base = os.path.join(PROJECT_ROOT, _DIRS[kind])
    if not os.path.isdir(base):
        return []
    return sorted(
        os.path.join(_DIRS[kind], f).replace("\\", "/")
        for f in os.listdir(base)
        if os.path.splitext(f)[1].lower() in _EXTS[kind])


# ── Media preview ───────────────────────────────────────────────────────────

@app.get("/api/media")
def media(path: str):
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    return FileResponse(abs_path, filename=os.path.basename(abs_path))


@app.get("/api/assets/thumb")
def asset_thumb(hash: str = "", path: str = ""):
    """Poster thumbnail for a video asset — extracted once with ffmpeg and
    cached on disk, so the grid never embeds heavyweight <video> elements.

    `hash` names the on-disk cache; when it's absent (e.g. an un-annotated
    project asset), derive it from the file content so any video path works."""
    if not hash or any(c in hash for c in "\\/.:"):
        from src.asset_manager.scanner import compute_content_hash
        _src = _resolve(path)
        hash = compute_content_hash(_src) if (_src and os.path.isfile(_src)) else ""
    if not hash or any(c in hash for c in "\\/.:"):
        raise HTTPException(400, "bad hash")
    tdir = os.path.join(PROJECT_ROOT, "Output", "asset_index", "thumbs")
    os.makedirs(tdir, exist_ok=True)
    tp = os.path.join(tdir, f"{hash}.jpg")
    if not os.path.exists(tp):
        src = _resolve(path)
        if not (src and os.path.isfile(src)):
            raise HTTPException(404, "source not found")
        import subprocess
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", src, "-frames:v", "1",
             "-vf", "scale=480:-2", "-q:v", "4", tp],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0 or not os.path.exists(tp):
            # very short clips: retry from 0s
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", tp],
                capture_output=True, timeout=30,
            )
        if not os.path.exists(tp):
            raise HTTPException(500, "thumbnail extraction failed")
    return FileResponse(tp, media_type="image/jpeg")


# ── Immich integration ──────────────────────────────────────────────────────
# The user's real library (6k+ videos) lives in Immich, which already provides
# transcoded 1080p playback proxies, thumbnails, checksums and CLIP semantic
# search. Analysis runs on the FEW-MB proxy (originals are never copied);
# originals are fetched only for the handful of clips that reach the render.

def _immich_req(path: str, method: str = "GET", body: dict | None = None,
                raw: bool = False, timeout: int = 60):
    import urllib.request
    base = str(cfg("IMMICH_URL", "http://127.0.0.1:2284")).rstrip("/")
    key = str(cfg("IMMICH_API_KEY", ""))
    if not key:
        raise HTTPException(400, "IMMICH_API_KEY 未配置")
    req = urllib.request.Request(
        base + "/api" + path, method=method,
        headers={"x-api-key": key, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data if raw else json.loads(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Immich 请求失败: {e}")


def _slim_immich_asset(a: dict) -> dict:
    exif = a.get("exifInfo") or {}
    return {
        "id": a.get("id"),
        "name": a.get("originalFileName", ""),
        "duration": a.get("duration", ""),
        "width": exif.get("exifImageWidth"),
        "height": exif.get("exifImageHeight"),
        "taken_at": a.get("fileCreatedAt", ""),
        "thumb": f"/api/immich/thumb/{a.get('id')}",
    }


@app.get("/api/immich/status")
def immich_status():
    about = _immich_req("/server/about")
    stats = _immich_req("/server/statistics")
    return {"version": about.get("version"), "videos": stats.get("videos"),
            "photos": stats.get("photos"), "usage": stats.get("usage")}


class ImmichSearch(BaseModel):
    query: str = ""
    size: int = 24
    page: int = 1


@app.post("/api/immich/search")
def immich_search(body: ImmichSearch):
    size = max(1, min(60, body.size))
    if body.query.strip():
        r = _immich_req("/search/smart", "POST",
                        {"query": body.query.strip(), "type": "VIDEO",
                         "size": size, "page": body.page})
    else:
        r = _immich_req("/search/metadata", "POST",
                        {"type": "VIDEO", "size": size, "page": body.page,
                         "withExif": True, "order": "desc"})
    items = (r.get("assets") or {}).get("items", [])
    # flag assets already imported (by Immich id + original checksum) so the UI
    # can show 已导入 and block accidental re-imports — same-name distinct
    # assets (iPhone reuses IMG_xxxx) otherwise look like duplicates.
    imap = _load_immich_map()
    imported_ids = {v.get("id") for v in imap.values() if v.get("id")}
    imported_cks = {v.get("checksum") for v in imap.values() if v.get("checksum")}
    slim = []
    for a in items:
        d = _slim_immich_asset(a)
        d["imported"] = (d.get("id") in imported_ids) or (a.get("checksum") in imported_cks)
        slim.append(d)
    return {"items": slim, "next": (r.get("assets") or {}).get("nextPage")}


@app.get("/api/immich/albums")
def immich_albums():
    """The user organizes Immich by DESTINATION albums — that's the mental
    model for finding footage, so the Immich tab browses albums first."""
    albums = _immich_req("/albums")
    out = []
    for al in albums or []:
        out.append({
            "id": al.get("id"),
            "name": al.get("albumName", ""),
            "count": al.get("assetCount", 0),
            "thumb": (f"/api/immich/thumb/{al.get('albumThumbnailAssetId')}"
                      if al.get("albumThumbnailAssetId") else None),
            "start": (al.get("startDate") or "")[:10],
            "end": (al.get("endDate") or "")[:10],
        })
    out.sort(key=lambda x: x.get("end") or "", reverse=True)
    return {"albums": out}


@app.get("/api/immich/albums/{album_id}")
def immich_album_assets(album_id: str):
    """One album's VIDEO assets with local status inline: imported? annotated?
    quality score? — the decision signals live where the picking happens,
    instead of only appearing after import+annotate."""
    safe = "".join(c for c in album_id if c.isalnum() or c == "-")
    al = _immich_req(f"/albums/{safe}")
    # Immich v3 album payloads no longer embed assets — pull them via
    # metadata search filtered by albumIds (paginated, capped at ~600)
    assets: list = []
    page = 1
    while page and len(assets) < 600:
        r = _immich_req("/search/metadata", "POST",
                        {"albumIds": [safe], "type": "VIDEO", "size": 200,
                         "page": page, "withExif": True, "order": "asc"})
        chunk = (r.get("assets") or {}).get("items", [])
        assets.extend(chunk)
        page = (r.get("assets") or {}).get("nextPage")
    imap = _load_immich_map()
    by_id = {v.get("id"): k for k, v in imap.items() if v.get("id")}
    by_ck = {v.get("checksum"): k for k, v in imap.items() if v.get("checksum")}
    # proxy file name → quality score from the annotation index
    ann_q: dict = {}
    try:
        from src.asset_manager.index_store import load_index
        for ann in load_index().values():
            fn = os.path.basename(getattr(ann.metadata, "absolute_path", "") or "")
            if fn:
                try:
                    ann_q[fn] = (_dump(ann.annotation) or {}).get("quality_score")
                except Exception:  # noqa: BLE001
                    ann_q[fn] = None
    except Exception:  # noqa: BLE001
        pass
    items = []
    for a in assets:
        d = _slim_immich_asset(a)
        fname = by_id.get(d.get("id")) or by_ck.get(a.get("checksum"))
        d["imported"] = bool(fname)
        if fname and fname in ann_q:
            d["annotated"] = True
            d["quality"] = ann_q[fname]
        items.append(d)
    items.sort(key=lambda x: x.get("taken_at") or "")
    return {"name": al.get("albumName", ""), "items": items,
            "total": int(al.get("assetCount") or 0), "videos": len(items)}


@app.get("/api/immich/thumb/{asset_id}")
def immich_thumb(asset_id: str):
    tdir = os.path.join(PROJECT_ROOT, "Output", "asset_index", "immich_thumbs")
    os.makedirs(tdir, exist_ok=True)
    safe = "".join(c for c in asset_id if c.isalnum() or c == "-")
    tp = os.path.join(tdir, f"{safe}.jpg")
    if not os.path.exists(tp):
        data = _immich_req(f"/assets/{safe}/thumbnail?size=preview", raw=True)
        with open(tp, "wb") as f:
            f.write(data)
    return FileResponse(tp, media_type="image/jpeg")


class ImmichImport(BaseModel):
    ids: list[str] = []


@app.post("/api/immich/import")
def immich_import(body: ImmichImport):
    """Download playback PROXIES (a few MB each — never the 4K originals) into
    the asset root so the normal scan → annotate flow picks them up. The
    Immich asset id is embedded in the filename for render-time original swap."""
    root = str(cfg("ASSET_ROOT_DIR", "resource/imports") or "resource/imports")
    dest_dir = os.path.join(_resolve(root), "immich")
    os.makedirs(dest_dir, exist_ok=True)

    # Persistent binding: proxy filename → Immich identity. `id` is the API
    # handle; `checksum` (SHA-1 of the ORIGINAL's content) is the stable
    # content identity — it survives re-uploads AND proxy re-transcodes, so
    # our VLM analysis stays attached to the right video forever.
    map_path = os.path.join(PROJECT_ROOT, "Output", "asset_index", "immich_map.json")
    try:
        with open(map_path, "r", encoding="utf-8") as f:
            imap = json.load(f)
    except Exception:  # noqa: BLE001
        imap = {}
    known_checksums = {v.get("checksum"): k for k, v in imap.items() if v.get("checksum")}

    imported, skipped, errors = [], [], []
    for aid in body.ids[:50]:
        safe = "".join(c for c in aid if c.isalnum() or c == "-")
        try:
            info = _immich_req(f"/assets/{safe}")
            checksum = info.get("checksum", "")
            # content-level dedup: same original already imported under any name.
            # But only skip if the bound proxy still EXISTS on disk — if the user
            # deleted it to reclaim space, fall through and re-download (analysis
            # stays reusable, keyed by the original checksum, not the proxy).
            if checksum and checksum in known_checksums:
                bound = known_checksums[checksum]
                bound_fp = os.path.join(dest_dir, bound)
                if os.path.exists(bound_fp) and os.path.getsize(bound_fp) > 0:
                    skipped.append(bound)
                    continue
            stem = os.path.splitext(str(info.get("originalFileName") or safe))[0]
            stem = "".join(c for c in stem if c not in '\\/:*?"<>|')
            fname = f"{stem}__im-{safe[:8]}.mp4"
            fp = os.path.join(dest_dir, fname)
            if not (os.path.exists(fp) and os.path.getsize(fp) > 0):
                data = _immich_req(f"/assets/{safe}/video/playback", raw=True, timeout=300)
                with open(fp, "wb") as f:
                    f.write(data)
                imported.append(fname)
            else:
                skipped.append(fname)
            imap[fname] = {
                "id": info.get("id", safe),
                "checksum": checksum,
                "original_name": info.get("originalFileName", ""),
                "original_path": info.get("originalPath", ""),
                "duration": info.get("duration", ""),
            }
            if checksum:
                known_checksums[checksum] = fname
        except HTTPException as e:
            errors.append(f"{aid[:8]}: {e.detail}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{aid[:8]}: {e}")

    os.makedirs(os.path.dirname(map_path), exist_ok=True)
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(imap, f, ensure_ascii=False, indent=2)
    return {"imported": imported, "skipped": skipped, "errors": errors,
            "dest": dest_dir}


# ── Workspace ops: cleanup / migrate / link local originals ────────────────
# The imports dir is a WORKSPACE (the active working set), not a library —
# these keep it small, relocatable and connected back to Immich.

_WS_TASK: dict = {"kind": None, "running": False, "note": "", "done": 0,
                  "total": 0, "result": None, "error": ""}


def _ws_start(kind: str) -> bool:
    if _WS_TASK["running"]:
        return False
    _WS_TASK.update({"kind": kind, "running": True, "note": "", "done": 0,
                     "total": 0, "result": None, "error": ""})
    return True


@app.get("/api/workspace/task")
def workspace_task():
    return dict(_WS_TASK)


class CleanupRequest(BaseModel):
    dry_run: bool = True


@app.post("/api/workspace/cleanup")
def workspace_cleanup(body: CleanupRequest):
    """Delete UNUSED Immich proxies: not referenced by any project, annotated
    (analysis cached by hash), and re-downloadable (bound in immich_map).
    Deleting loses nothing — re-import restores the file, analysis reattaches."""
    root = _resolve(str(cfg("ASSET_ROOT_DIR", "resource/imports") or "resource/imports"))
    proxy_dir = os.path.join(root, "immich")
    if not os.path.isdir(proxy_dir):
        return {"count": 0, "bytes": 0, "files": [], "dry_run": body.dry_run}

    ref_names: set = set()
    import glob as _g
    for pj in _g.glob(os.path.join(PROJECTS_DIR, "*", "project.json")):
        try:
            with open(pj, "r", encoding="utf-8") as f:
                p = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        paths = list(p.get("videos") or []) + list(p.get("audios") or [])
        paths += [p.get("audio") or "", p.get("srt") or ""]
        for x in paths:
            if x:
                ref_names.add(os.path.basename(str(x)).lower())

    from src.asset_manager.index_store import load_index
    annotated_paths = set()
    for ann in load_index().values():
        ap = getattr(ann.metadata, "absolute_path", "") or ""
        if ap:
            annotated_paths.add(os.path.normcase(os.path.abspath(ap)))

    imap = _load_immich_map()
    victims = []
    for fn in sorted(os.listdir(proxy_dir)):
        fp = os.path.join(proxy_dir, fn)
        if not os.path.isfile(fp):
            continue
        if fn not in imap:
            continue                      # not re-downloadable → never touch
        if fn.lower() in ref_names:
            continue                      # a project still uses it
        if os.path.normcase(os.path.abspath(fp)) not in annotated_paths:
            continue                      # not annotated yet → deleting wastes the download
        victims.append((fp, os.path.getsize(fp)))

    if not body.dry_run:
        for fp, _sz in victims:
            try:
                os.remove(fp)
            except OSError:
                pass
    return {"count": len(victims), "bytes": sum(s for _f, s in victims),
            "files": [os.path.basename(f) for f, _s in victims][:80],
            "dry_run": body.dry_run}


def _norm_prefix(p: str) -> str:
    return os.path.normcase(p.replace("/", "\\").rstrip("\\/"))


def _swap_path(s: str, olds: list, new_root: str):
    """Rewrite one string if it starts with any old workspace prefix.
    Handles both absolute paths and repo-relative 'resource/imports/…' forms;
    result is always absolute under the new root."""
    if not isinstance(s, str) or len(s) < 4:
        return s, False
    cand = s.replace("/", "\\")
    lc = os.path.normcase(cand)
    for old in olds:
        if lc == old or lc.startswith(old + "\\"):
            return new_root + cand[len(old):], True
    return s, False


def _rewrite_store(path: str, olds: list, new_root: str, dry: bool) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return 0
    n = 0

    def _walk(o):
        nonlocal n
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, str):
            new, hit = _swap_path(o, olds, new_root)
            if hit:
                n += 1
            return new
        return o

    data = _walk(data)
    if n and not dry:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    return n


def _migrate_worker(old_abs: str, new_abs: str):
    import glob as _g
    import shutil
    try:
        files = []
        for dp, _dn, fns in os.walk(old_abs):
            for fn in fns:
                files.append(os.path.join(dp, fn))
        _WS_TASK["total"] = len(files) + 1
        moved = 0
        for i, fp in enumerate(files):
            rel = os.path.relpath(fp, old_abs)
            dst = os.path.join(new_abs, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _WS_TASK.update({"done": i, "note": f"移动 {rel}"})
            if not os.path.exists(dst):
                shutil.move(fp, dst)
                moved += 1
        # sweep now-empty dirs (best effort)
        for dp, _dn, fns in os.walk(old_abs, topdown=False):
            try:
                os.rmdir(dp)
            except OSError:
                pass

        _WS_TASK["note"] = "改写数据存储中的路径…"
        olds = [_norm_prefix(old_abs)]
        rel_root = os.path.relpath(old_abs, PROJECT_ROOT)
        if not rel_root.startswith(".."):
            olds.append(_norm_prefix(rel_root))
        stores = (
            _g.glob(os.path.join(PROJECT_ROOT, "Output", "asset_index", "*.json"))
            + _g.glob(os.path.join(PROJECTS_DIR, "*", "project.json"))
            + _g.glob(os.path.join(PROJECT_ROOT, "Output", "analyzed", "*", "metadata.json"))
            + _g.glob(os.path.join(PROJECT_ROOT, "Output", "analyzed", "*", "highlight_pool.json"))
            + _g.glob(os.path.join(PROJECT_ROOT, "Output", "Output", "*", "*.json"))
        )
        rewritten = 0
        for sp in stores:
            rewritten += _rewrite_store(sp, olds, new_abs, dry=False)
        save_config("ASSET_ROOT_DIR", new_abs)
        SCANNED["assets"] = []

        # verify: annotated paths under the new root must exist
        from src.asset_manager.index_store import load_index
        missing = 0
        checked = 0
        for ann in load_index().values():
            ap = getattr(ann.metadata, "absolute_path", "") or ""
            if ap and os.path.normcase(ap).startswith(os.path.normcase(new_abs)):
                checked += 1
                if not os.path.exists(ap):
                    missing += 1
        _WS_TASK.update({
            "running": False, "done": _WS_TASK["total"],
            "result": {"moved": moved, "rewritten": rewritten,
                       "verified": checked, "missing": missing,
                       "new_root": new_abs},
        })
    except Exception as e:  # noqa: BLE001
        _WS_TASK.update({"running": False, "error": str(e)[:400]})


class MigrateRequest(BaseModel):
    new_root: str
    dry_run: bool = True


@app.post("/api/workspace/migrate")
def workspace_migrate(body: MigrateRequest):
    """One-click workspace relocation: move every file, then rewrite the old
    root prefix (absolute AND repo-relative forms) across every data store —
    annotation index, projects, per-source metadata/pools, project outputs."""
    old_abs = _resolve(str(cfg("ASSET_ROOT_DIR", "resource/imports") or "resource/imports"))
    new_abs = os.path.abspath(body.new_root.strip().strip('"'))
    if not new_abs or _norm_prefix(new_abs) == _norm_prefix(old_abs):
        raise HTTPException(400, "新路径为空或与当前相同")
    if _norm_prefix(new_abs).startswith(_norm_prefix(old_abs) + "\\"):
        raise HTTPException(400, "新路径不能在当前工作区内部")
    if not os.path.isdir(old_abs):
        raise HTTPException(400, f"当前工作区不存在: {old_abs}")

    if body.dry_run:
        n_files, n_bytes = 0, 0
        for dp, _dn, fns in os.walk(old_abs):
            for fn in fns:
                n_files += 1
                try:
                    n_bytes += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
        same_drive = os.path.splitdrive(old_abs)[0].lower() == os.path.splitdrive(new_abs)[0].lower()
        return {"dry_run": True, "files": n_files, "bytes": n_bytes,
                "same_drive": same_drive, "old_root": old_abs, "new_root": new_abs}

    if not _ws_start("migrate"):
        raise HTTPException(409, "已有工作区任务在运行")
    os.makedirs(new_abs, exist_ok=True)
    threading.Thread(target=_migrate_worker, args=(old_abs, new_abs), daemon=True).start()
    return {"started": True}


def _link_local_worker():
    import base64
    import hashlib as _hl
    try:
        root = _resolve(str(cfg("ASSET_ROOT_DIR", "resource/imports") or "resource/imports"))
        imap = _load_immich_map()
        known = set(imap.keys())
        exts = (".mp4", ".mov", ".m4v", ".avi", ".mkv")
        files = []
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith(exts) and "__im-" not in fn and fn not in known:
                    files.append(os.path.join(dp, fn))
        _WS_TASK["total"] = len(files)
        checks = []
        for i, fp in enumerate(files):
            _WS_TASK.update({"done": i, "note": f"计算校验 {os.path.basename(fp)}"})
            h = _hl.sha1()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
                    h.update(chunk)
            checks.append({"fp": fp, "hex": h.hexdigest(),
                           "b64": base64.b64encode(h.digest()).decode()})
        linked = 0
        _WS_TASK["note"] = "查询 Immich…"
        for k in range(0, len(checks), 50):
            batch = checks[k:k + 50]
            r = _immich_req("/assets/bulk-upload-check", "POST",
                            {"assets": [{"id": c["fp"], "checksum": c["hex"]} for c in batch]})
            for res in (r.get("results") or []):
                aid = res.get("assetId")
                if not aid:
                    continue
                c = next((x for x in batch if x["fp"] == res.get("id")), None)
                if not c:
                    continue
                imap[os.path.basename(c["fp"])] = {
                    "id": aid, "checksum": c["b64"],
                    "original_name": os.path.basename(c["fp"]),
                    "linked": "local-original",
                }
                linked += 1
        map_path = os.path.join(PROJECT_ROOT, "Output", "asset_index", "immich_map.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(imap, f, ensure_ascii=False, indent=2)
        _WS_TASK.update({"running": False, "done": _WS_TASK["total"],
                         "result": {"scanned": len(files), "linked": linked}})
    except Exception as e:  # noqa: BLE001
        _WS_TASK.update({"running": False, "error": str(e)[:400]})


@app.post("/api/workspace/link_local")
def workspace_link_local():
    """Match manually-copied originals to their Immich assets by SHA-1 (the
    checksum Immich stores) via bulk-upload-check, and bind them into
    immich_map — album badges and 在 Immich 中查看 then work for them too."""
    if not _ws_start("link_local"):
        raise HTTPException(409, "已有工作区任务在运行")
    threading.Thread(target=_link_local_worker, daemon=True).start()
    return {"started": True}


# ── Immich annotation write-back ────────────────────────────────────────────
# Push CutClaw's VLM annotation into the Immich asset description, making the
# analysis searchable inside Immich itself. Our block is delimited so repeated
# syncs REPLACE it while any human-written description above is preserved.

_CUTCLAW_MARK = "─── CutClaw AI 标注 ───"


# ── per-file capture time & location (cached — computed once per file) ─────
_MEDIA_META_PATH = os.path.join(PROJECT_ROOT, "Output", "cache", "media_meta.json")
_MEDIA_META: dict | None = None
_MEDIA_META_LOCK = threading.Lock()


def _media_meta_for(abs_path: str, file_name: str) -> dict:
    """{"capture_time", "location", "camera"} for one media file.

    capture_time: filename pattern → container creation_time (capture_time.py).
    location: Immich exifInfo city/state/country when the file is an Immich
    proxy (reverse-geocoded by Immich), else the QuickTime GPS tag as coords.
    camera: Immich exifInfo make/model, else QuickTime make/model tags.
    Cached by file name in Output/cache/media_meta.json — all values come
    from the ORIGINAL recording, so re-downloads/transcodes don't change them.
    """
    global _MEDIA_META
    with _MEDIA_META_LOCK:
        if _MEDIA_META is None:
            try:
                with open(_MEDIA_META_PATH, "r", encoding="utf-8") as f:
                    _MEDIA_META = json.load(f)
            except Exception:  # noqa: BLE001
                _MEDIA_META = {}
        cached = _MEDIA_META.get(file_name)
        # entries cached before the camera field existed refresh once
        if cached is not None and "camera" in cached:
            return cached

    meta: dict = {"capture_time": None, "location": None, "camera": None}
    try:
        from src.utils.capture_time import get_capture_time
        ct = get_capture_time(abs_path or file_name)
        if ct is not None:
            meta["capture_time"] = ct.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    # location + camera: Immich-bound files get the reverse-geocoded city and
    # the EXIF make/model straight from the original
    try:
        entry = _load_immich_map().get(file_name)
        if entry and entry.get("id"):
            info = _immich_req(f"/assets/{entry['id']}", timeout=15)
            ex = info.get("exifInfo") or {}
            parts = [p for p in (ex.get("city"), ex.get("state") or ex.get("country")) if p]
            if parts:
                meta["location"] = " · ".join(dict.fromkeys(parts))
            elif ex.get("latitude") is not None and ex.get("longitude") is not None:
                meta["location"] = f"{float(ex['latitude']):.3f}, {float(ex['longitude']):.3f}"
            make = str(ex.get("make") or "").strip()
            model = str(ex.get("model") or "").strip()
            if model:
                # drop a redundant vendor prefix ("DJI" + "DJI OsmoPocket3")
                cam = model if (not make or model.lower().startswith(make.lower())) \
                    else f"{make} {model}"
                meta["camera"] = cam
    except Exception:  # noqa: BLE001
        pass
    # fallback: QuickTime/MP4 tags (phones embed ISO6709 + make/model)
    if (not meta["location"] or not meta["camera"]) and abs_path and os.path.exists(abs_path):
        try:
            fp = os.path.join(PROJECT_ROOT, "tools", "ffmpeg", "ffprobe.exe")
            if not os.path.exists(fp):
                fp = "ffprobe"
            r = subprocess.run(
                [fp, "-v", "error", "-show_entries",
                 "format_tags=location,com.apple.quicktime.location.ISO6709,"
                 "com.apple.quicktime.make,com.apple.quicktime.model,make,model",
                 "-of", "default=nw=1", abs_path],
                capture_output=True, text=True, timeout=15)
            out = r.stdout or ""
            if not meta["location"]:
                m = re.search(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)", out)
                if m:
                    meta["location"] = f"{float(m.group(1)):.3f}, {float(m.group(2)):.3f}"
            if not meta["camera"]:
                tags = dict(re.findall(r"TAG:([\w.]+)=(.+)", out))
                make = (tags.get("com.apple.quicktime.make") or tags.get("make") or "").strip()
                model = (tags.get("com.apple.quicktime.model") or tags.get("model") or "").strip()
                if model:
                    meta["camera"] = model if (not make or model.lower().startswith(make.lower())) \
                        else f"{make} {model}"
        except Exception:  # noqa: BLE001
            pass

    with _MEDIA_META_LOCK:
        _MEDIA_META[file_name] = meta
        try:
            os.makedirs(os.path.dirname(_MEDIA_META_PATH), exist_ok=True)
            with open(_MEDIA_META_PATH, "w", encoding="utf-8") as f:
                json.dump(_MEDIA_META, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return meta


def _load_immich_map() -> dict:
    p = os.path.join(PROJECT_ROOT, "Output", "asset_index", "immich_map.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _compose_cutclaw_block(ann: dict) -> str:
    lines = [_CUTCLAW_MARK]
    q = ann.get("quality_score")
    emo = ann.get("emotion") or ann.get("mood") or ""
    head = []
    if q not in (None, ""):
        head.append(f"质量分 {q}")
    if emo:
        head.append(f"情绪 {emo}")
    if ann.get("camera_movement"):
        head.append(f"运镜 {ann['camera_movement']}")
    if head:
        lines.append(" · ".join(str(x) for x in head))
    if ann.get("summary"):
        lines.append(str(ann["summary"]))
    tags = [str(t) for t in (ann.get("tags") or []) + (ann.get("visual_tags") or [])]
    if tags:
        lines.append("标签: " + ", ".join(dict.fromkeys(tags)))
    if ann.get("suggested_use"):
        lines.append("建议用途: " + str(ann["suggested_use"]))
    lines.append(f"(CutClaw 同步于 {time.strftime('%Y-%m-%d %H:%M')})")
    return "\n".join(lines)


def _immich_writeback_one(immich_id: str, ann: dict) -> None:
    info = _immich_req(f"/assets/{immich_id}")
    cur = ((info.get("exifInfo") or {}).get("description")
           or info.get("description") or "")
    # strip any previous CutClaw block (keep the human part above it)
    if _CUTCLAW_MARK in cur:
        cur = cur.split(_CUTCLAW_MARK)[0].rstrip()
    block = _compose_cutclaw_block(ann)
    new_desc = (cur + "\n\n" + block).strip() if cur else block
    _immich_req(f"/assets/{immich_id}", "PUT", {"description": new_desc[:4000]})


def _writeback_annotations(file_names: list | None = None) -> dict:
    """Sync annotations of Immich-bound assets back to Immich descriptions.

    file_names: restrict to these local proxy filenames; None = all bound.
    Returns {synced, skipped, errors}.
    """
    imap = _load_immich_map()
    if not imap:
        return {"synced": 0, "skipped": 0, "errors": []}
    ann_path = os.path.join(PROJECT_ROOT, "Output", "asset_index", "annotations.json")
    try:
        with open(ann_path, "r", encoding="utf-8") as f:
            store = json.load(f)
    except Exception:  # noqa: BLE001
        return {"synced": 0, "skipped": 0, "errors": ["annotations.json 不可读"]}
    wanted = set(file_names) if file_names else None
    synced, skipped, errors = 0, 0, []
    for entry in store.values():
        meta = entry.get("metadata") or {}
        fname = meta.get("file_name", "")
        bind = imap.get(fname)
        if not bind:
            continue
        if wanted is not None and fname not in wanted:
            continue
        ann = entry.get("annotation") or {}
        if not ann.get("summary") and not ann.get("tags"):
            skipped += 1
            continue
        try:
            _immich_writeback_one(bind["id"], ann)
            synced += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fname}: {str(e)[:80]}")
    return {"synced": synced, "skipped": skipped, "errors": errors}


class WritebackRequest(BaseModel):
    file_names: list[str] = []


@app.post("/api/immich/writeback")
def immich_writeback(body: WritebackRequest):
    return _writeback_annotations(body.file_names or None)


# ── AI photo scoring: professional critique of Immich PHOTOS ───────────────
# The user never rates manually (thousands of photos), so the Immich star
# rating is machine-owned: grade → stars (filter/sort inside Immich), full
# critique → a delimited description block. Runs on the CURRENT vision model
# (local Qwen = free). v1 is VLM-judged only — no cv2 in the resident server
# process (铁律10: heavy-DLL deps stay out; measured sharpness can join later
# via a worker subprocess if the scores feel off).

_PHOTO_MARK = "─── CutClaw 摄影评分 ───"
_PHOTO_SCORES_PATH = os.path.join(PROJECT_ROOT, "Output", "asset_index", "photo_scores.json")
_GRADE_STARS = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1}

_PHOTO_SCORE_PROMPT = """你是一位专业摄影评审(风光/旅拍/人文方向)。从专业摄影角度评价这张照片,诚实、克制,不要客套。

只输出 JSON(不要 markdown 代码块):
{"clarity": <0-10 画质:对焦是否实、噪点、曝光是否准、有无糊>,
 "lighting": <0-10 光影:光线方向与质感、明暗层次、氛围、是否死黑死白>,
 "composition": <0-10 构图:主体位置、三分/引导线/框架、平衡、地平线、裁切>,
 "subject": <0-10 主体与瞬间:有没有明确主体、故事感、抓拍时机>,
 "overall": <0-10 综合分,可有小数>,
 "grade": "<S|A|B|C|D — S=作品级(罕见) A=优秀可出片 B=合格记录 C=有明显缺陷 D=废片>",
 "strengths": "<一句话:这张最出色的地方;若乏善可陈就直说>",
 "improve": "<一句话:下次拍摄最该改进的一点,要具体可执行,如'降低机位让地平线落在下三分线'>"}"""


def _extract_json_generic(text: str):
    """Fenced JSON → whole text → balanced-brace objects (LAST first —
    reasoning models bury the answer at the end of thinking text)."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except Exception:  # noqa: BLE001
        pass
    objs, depth, start = [], 0, -1
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                objs.append(t[start:i + 1])
    for b in reversed(objs):
        try:
            return json.loads(b)
        except Exception:  # noqa: BLE001
            continue
    return None


def _photo_scores_load() -> dict:
    try:
        with open(_PHOTO_SCORES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _score_photo_vlm(img_b64: str) -> dict | None:
    import litellm
    kwargs = dict(
        model=str(cfg("VIDEO_ANALYSIS_MODEL", "")),
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PHOTO_SCORE_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ]}],
        temperature=0.3, max_tokens=8192, timeout=180,
    )
    base = str(cfg("VIDEO_ANALYSIS_ENDPOINT", ""))
    key = str(cfg("VIDEO_ANALYSIS_API_KEY", ""))
    if base:
        kwargs["api_base"] = base
    if key:
        kwargs["api_key"] = key
    resp = litellm.completion(**kwargs)
    msg = resp.choices[0].message
    content = (msg.content or "").strip() \
        or str(getattr(msg, "reasoning_content", "") or "").strip()
    d = _extract_json_generic(content)
    return d if isinstance(d, dict) and d.get("overall") is not None else None


def _photo_block(s: dict) -> str:
    dims = " · ".join(f"{lab} {s.get(k)}" for k, lab in
                      (("clarity", "画质"), ("lighting", "光影"),
                       ("composition", "构图"), ("subject", "主体")) if s.get(k) is not None)
    lines = [_PHOTO_MARK,
             f"评级 {s.get('grade', '?')} · 综合 {s.get('overall', '?')}/10"]
    if dims:
        lines.append(dims)
    if s.get("strengths"):
        lines.append(f"亮点: {s['strengths']}")
    if s.get("improve"):
        lines.append(f"建议: {s['improve']}")
    lines.append(f"(CutClaw 摄影评分 · {time.strftime('%Y-%m-%d %H:%M')})")
    return "\n".join(lines)


def _photo_score_worker(album_id: str, limit: int, write_rating: bool):
    import base64
    try:
        # album photos (IMAGE assets), paginated
        assets, page = [], 1
        while page and len(assets) < 2000:
            r = _immich_req("/search/metadata", "POST",
                            {"albumIds": [album_id], "type": "IMAGE", "size": 200,
                             "page": page, "withExif": True})
            a = r.get("assets") or {}
            assets.extend(a.get("items", []))
            page = a.get("nextPage")

        store = _photo_scores_load()
        todo = [a for a in assets
                if a.get("id") not in store
                and (a.get("exifInfo") or {}).get("rating") in (None, 0)][:max(1, limit)]
        _WS_TASK["total"] = len(todo)
        scored = failed = 0
        grades: dict = {}
        for i, a in enumerate(todo):
            aid = a["id"]
            _WS_TASK.update({"done": i, "note": f"评审 {a.get('originalFileName', aid[:8])}"})
            try:
                img = _immich_req(f"/assets/{aid}/thumbnail?size=preview", raw=True)
                s = _score_photo_vlm(base64.b64encode(img).decode())
                if not s:
                    failed += 1
                    continue
                grade = str(s.get("grade", "")).strip().upper()[:1]
                if grade not in _GRADE_STARS:
                    ov = float(s.get("overall") or 0)
                    grade = "S" if ov >= 9 else "A" if ov >= 8 else "B" if ov >= 6.5 \
                        else "C" if ov >= 5 else "D"
                    s["grade"] = grade
                # description block (strip previous photo block, keep the rest)
                info = _immich_req(f"/assets/{aid}")
                cur = ((info.get("exifInfo") or {}).get("description")
                       or info.get("description") or "")
                if _PHOTO_MARK in cur:
                    cur = cur.split(_PHOTO_MARK)[0].rstrip()
                new_desc = (cur + "\n\n" + _photo_block(s)).strip() if cur else _photo_block(s)
                body: dict = {"description": new_desc[:4000]}
                if write_rating:
                    body["rating"] = _GRADE_STARS[grade]
                _immich_req(f"/assets/{aid}", "PUT", body)
                s["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                s["name"] = a.get("originalFileName", "")
                store[aid] = s
                grades[grade] = grades.get(grade, 0) + 1
                scored += 1
                if scored % 10 == 0:
                    os.makedirs(os.path.dirname(_PHOTO_SCORES_PATH), exist_ok=True)
                    with open(_PHOTO_SCORES_PATH, "w", encoding="utf-8") as f:
                        json.dump(store, f, ensure_ascii=False, indent=1)
            except Exception:  # noqa: BLE001
                failed += 1
        os.makedirs(os.path.dirname(_PHOTO_SCORES_PATH), exist_ok=True)
        with open(_PHOTO_SCORES_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
        _WS_TASK.update({"running": False, "done": _WS_TASK["total"],
                         "result": {"scored": scored, "failed": failed,
                                    "remaining": max(0, len(assets) - len(store)),
                                    "grades": grades}})
    except Exception as e:  # noqa: BLE001
        _WS_TASK.update({"running": False, "error": str(e)[:400]})


class PhotoScoreRequest(BaseModel):
    album_id: str
    limit: int = 200
    write_rating: bool = True


@app.post("/api/immich/score_photos")
def immich_score_photos(body: PhotoScoreRequest):
    """Batch-score an album's photos with the current vision model. Already-
    scored (store) and already-rated photos are skipped, so repeated runs
    walk through a big album incrementally."""
    if not body.album_id:
        raise HTTPException(400, "album_id required")
    if not _ws_start("score_photos"):
        raise HTTPException(409, "已有工作区任务在运行")
    threading.Thread(target=_photo_score_worker,
                     args=(body.album_id, body.limit, body.write_rating),
                     daemon=True).start()
    return {"started": True}


def _immich_stream_original(immich_id: str, dst: str):
    """Stream an original (can be hundreds of MB) to disk without buffering."""
    import shutil as _sh
    import urllib.request
    base = str(cfg("IMMICH_URL", "http://127.0.0.1:2284")).rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/assets/{immich_id}/original",
        headers={"x-api-key": str(cfg("IMMICH_API_KEY", ""))})
    tmp = dst + ".part"
    with urllib.request.urlopen(req, timeout=1800) as resp, open(tmp, "wb") as f:
        _sh.copyfileobj(resp, f, length=1 << 20)
    os.replace(tmp, dst)


def _materialize_immich_originals(abs_point: str) -> tuple[str, list[str]]:
    """4K source swap for original-quality renders: rewrite Immich PROXY paths
    in shot_point.json to the ORIGINALS. Resolution order per clip:
    IMMICH_PATH_MAP (zero-copy read from the mounted volume) → cached
    download → keep proxy (with a note). Times are seconds — they map 1:1
    between proxy and original. Returns (shot_json_to_use, notes)."""
    imap = _load_immich_map()
    notes: list[str] = []
    if not imap:
        return abs_point, notes
    with open(abs_point, "r", encoding="utf-8") as f:
        data = json.load(f)
    entries = data if isinstance(data, list) else []

    pm = str(cfg("IMMICH_PATH_MAP", "") or "").strip()
    cpfx, hpfx = (pm.split("::", 1) if "::" in pm else ("", ""))
    dl_dir = os.path.join(PROJECT_ROOT, "Output", "asset_index", "immich_originals")

    resolved: dict = {}

    def _orig_for(fname: str):
        if fname in resolved:
            return resolved[fname]
        bind = imap.get(fname)
        out = None
        if bind:
            # zero-copy via host-mounted Immich volume
            op = str(bind.get("original_path") or "")
            if cpfx and op.startswith(cpfx):
                hp = hpfx + op[len(cpfx):]
                if os.path.exists(hp):
                    out = hp
                    notes.append(f"零拷贝: {fname} → {hp}")
            if out is None:
                os.makedirs(dl_dir, exist_ok=True)
                ext = os.path.splitext(str(bind.get("original_name") or ""))[1] or ".mp4"
                dst = os.path.join(dl_dir, f"{bind['id']}{ext}")
                if not (os.path.exists(dst) and os.path.getsize(dst) > 0):
                    notes.append(f"拉取原片: {bind.get('original_name', fname)}")
                    _immich_stream_original(bind["id"], dst)
                else:
                    notes.append(f"原片缓存命中: {fname}")
                out = dst
        resolved[fname] = out
        return out

    changed = False
    for e in entries:
        if not isinstance(e, dict):
            continue
        for holder in ([e] + list(e.get("clips") or [])):
            vp = str(holder.get("video_path") or "")
            if not vp:
                continue
            op = _orig_for(os.path.basename(vp))
            if op:
                holder["video_path"] = op
                changed = True
    if not changed:
        return abs_point, notes
    swapped = abs_point[:-5] + ".orig.json" if abs_point.endswith(".json") else abs_point + ".orig"
    with open(swapped, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return swapped, notes


# ── Local VLM / GPU panel ───────────────────────────────────────────────────
# Manual, observable control over the local model: is the GPU actually in
# use, is the model resident in VRAM, how long does a real call take.

_OLLAMA = "http://127.0.0.1:11434"


def _ollama_req(path: str, method: str = "GET", body: dict | None = None, timeout: int = 600):
    import urllib.request
    req = urllib.request.Request(
        _OLLAMA + path, method=method,
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


@app.get("/api/local/gpu")
def local_gpu():
    import subprocess
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[:200]}
        name, util, mu, mt, temp = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
        return {"ok": True, "name": name, "util": int(util),
                "mem_used_mb": int(mu), "mem_total_mb": int(mt), "temp": int(temp)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/local/ollama")
def local_ollama():
    try:
        tags = _ollama_req("/api/tags", timeout=5)
        ps = _ollama_req("/api/ps", timeout=5)
        loaded = {m.get("name"): m for m in (ps.get("models") or [])}
        models = []
        for m in (tags.get("models") or []):
            nm = m.get("name", "")
            ld = loaded.get(nm)
            models.append({
                "name": nm,
                "size_gb": round((m.get("size") or 0) / 1e9, 1),
                "loaded": bool(ld),
                "vram_gb": round((ld.get("size_vram") or 0) / 1e9, 1) if ld else None,
                "until": (ld or {}).get("expires_at", ""),
            })
        return {"ok": True, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Ollama 未运行? {str(e)[:150]}"}


class OllamaModelReq(BaseModel):
    model: str
    keep_alive: str = "2h"


@app.post("/api/local/ollama/load")
def local_ollama_load(body: OllamaModelReq):
    t0 = time.time()
    try:
        _ollama_req("/api/generate", "POST",
                    {"model": body.model, "prompt": "", "keep_alive": body.keep_alive},
                    timeout=600)
        return {"ok": True, "seconds": round(time.time() - t0, 1),
                "message": f"模型已加载并常驻 {body.keep_alive}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/local/ollama/unload")
def local_ollama_unload(body: OllamaModelReq):
    try:
        _ollama_req("/api/generate", "POST",
                    {"model": body.model, "prompt": "", "keep_alive": 0}, timeout=60)
        return {"ok": True, "message": "已请求卸载（VRAM 将在数秒内释放）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


@app.post("/api/local/ollama/test")
def local_ollama_test(body: OllamaModelReq):
    """One REAL single-frame vision call with timing + GPU snapshots, so the
    user can verify the card is doing the work."""
    import base64
    import glob as _g
    # test frame: reuse a cached asset thumbnail; else a generated color frame
    frame = None
    for f in _g.glob(os.path.join(PROJECT_ROOT, "Output", "asset_index", "thumbs", "*.jpg")):
        frame = f
        break
    if frame is None:
        import subprocess
        frame = os.path.join(PROJECT_ROOT, "Output", "asset_index", "vlm_test.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi",
                        "-i", "testsrc2=size=426x240:d=1", "-frames:v", "1", frame],
                       capture_output=True, timeout=30)
    b64 = base64.b64encode(open(frame, "rb").read()).decode()
    gpu_before = local_gpu()
    t0 = time.time()
    try:
        r = _ollama_req("/v1/chat/completions", "POST", {
            "model": body.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Describe this frame in one sentence for a video editor."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
            "max_tokens": 100, "temperature": 0.0,
        }, timeout=600)
        dt = round(time.time() - t0, 2)
        gpu_after = local_gpu()
        reply = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return {"ok": True, "seconds": dt, "reply": reply[:300],
                "gpu_before": gpu_before, "gpu_after": gpu_after}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "seconds": round(time.time() - t0, 2)}


@app.get("/api/local/vlm-stats")
def local_vlm_stats(model: str = ""):
    """Recent real VLM call latencies from the LLM call logs."""
    import glob as _g
    files = sorted(_g.glob(os.path.join(PROJECT_ROOT, "Output", "logs", "llm_calls_*.jsonl")),
                   key=os.path.getmtime)[-2:]
    lat = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if model and model.split("/")[-1] not in str(d.get("model", "")):
                        continue
                    v = d.get("latency_s")
                    if isinstance(v, (int, float)):
                        lat.append(float(v))
        except Exception:
            continue
    lat = lat[-50:]
    return {"count": len(lat),
            "avg_s": round(sum(lat) / len(lat), 2) if lat else None,
            "last_s": round(lat[-1], 2) if lat else None}


# ── Assets ──────────────────────────────────────────────────────────────────

def _dump(model) -> dict:
    for attr in ("model_dump", "dict"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {k: v for k, v in vars(model).items() if not k.startswith("_")}


class ScanRequest(BaseModel):
    root: str = ""


SCANNED: dict[str, list] = {"assets": [], "root": ""}


def _immich_identity(checksum: str) -> str:
    """Stable analysis key for an Immich asset, derived from the ORIGINAL's
    checksum (Immich's base64 SHA-1). The proxy's own bytes-hash changes across
    re-downloads/re-transcodes; the original's checksum does not — so keying by
    it makes analysis survive proxy churn. base64 → base64url for a path-safe,
    collision-free directory name."""
    safe = checksum.replace("+", "-").replace("/", "_").rstrip("=")
    return f"im-{safe}"


def _migrate_analysis_identity(old_hash: str, new_key: str) -> None:
    """One-time self-heal: move analysis + annotations from a volatile proxy
    bytes-hash key to the stable Immich checksum key, so pre-existing analysis
    isn't orphaned by the switch. No-op once already migrated."""
    from src.analyzer import get_analysis_path
    from src.asset_manager.index_store import load_index, save_index
    # analysis cache dirs (cloud + local variant)
    for variant in ("", "local"):
        old_dir = get_analysis_path(old_hash, variant)
        new_dir = get_analysis_path(new_key, variant)
        if os.path.isdir(old_dir) and not os.path.isdir(new_dir):
            try:
                os.replace(old_dir, new_dir)
            except OSError:
                pass
    # cloud annotation index
    try:
        idx = load_index()
        if old_hash in idx and new_key not in idx:
            ann = idx.pop(old_hash)
            ann.content_hash = new_key
            try:
                ann.metadata.content_hash = new_key
            except Exception:  # noqa: BLE001
                pass
            idx[new_key] = ann
            save_index(idx)
    except Exception:  # noqa: BLE001
        pass
    # local annotation store
    try:
        store = _load_local_annotations()
        if old_hash in store and new_key not in store:
            store[new_key] = store.pop(old_hash)
            with open(_LOCAL_ANN_PATH, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _apply_immich_identity(assets: list) -> list:
    """Re-key Immich-bound proxies to the ORIGINAL's stable checksum in place.

    The scanner keys every file by its bytes-hash; for an Immich proxy that hash
    is volatile (a re-download can re-transcode → new bytes). We rewrite
    content_hash to `im-<checksum>` so analysis/annotations stay attached to the
    ORIGINAL forever, and migrate any analysis that was cached under the old
    bytes-hash the first time we see each asset."""
    imap = _load_immich_map()
    if not imap:
        return assets
    from src.analyzer import get_analysis_path
    for meta in assets:
        fname = getattr(meta, "file_name", "") or os.path.basename(
            getattr(meta, "absolute_path", "") or getattr(meta, "file_path", ""))
        bind = imap.get(fname)
        if not bind:
            continue
        checksum = bind.get("checksum")
        if not checksum:
            continue
        stable = _immich_identity(checksum)
        old = getattr(meta, "content_hash", "")
        # Migrate only when analysis actually sits under the old bytes-hash key
        # — a cheap isdir check keeps steady-state scans from re-reading the
        # index once everything's already migrated.
        if old and old != stable and (
                os.path.isdir(get_analysis_path(old))
                or os.path.isdir(get_analysis_path(old, "local"))):
            _migrate_analysis_identity(old, stable)
        meta.content_hash = stable
    return assets


def _assets_with_annotations(assets: list) -> list:
    """Merge each scanned asset with its cloud + local annotation, if any."""
    from src.asset_manager.index_store import load_index
    idx = load_index()
    local_store = _load_local_annotations()
    # Immich back-link: workspace file name → Immich asset (proxies always,
    # manual originals after 识别本地原片 ran)
    _imap = _load_immich_map()
    _im_base = str(cfg("IMMICH_URL", "") or "").rstrip("/")
    out = []
    for meta in assets:
        d = _dump(meta)
        _iid = (_imap.get(d.get("file_name") or "") or {}).get("id")
        if _iid and _im_base:
            d["immich_url"] = f"{_im_base}/photos/{_iid}"
        h = getattr(meta, "content_hash", "")
        ann = idx.get(h)
        d["annotated"] = ann is not None
        if ann is not None:
            d["annotation"] = _dump(ann.annotation)
        _loc = local_store.get(h)
        d["annotated_local"] = _loc is not None
        if _loc is not None:
            d["annotation_local"] = _loc.get("annotation")
        # journey metadata from the ORIGINAL recording (cached per file)
        try:
            mm = _media_meta_for(d.get("absolute_path") or "", d.get("file_name") or "")
            d["capture_time"] = mm.get("capture_time")
            d["location"] = mm.get("location")
            d["camera"] = mm.get("camera")
        except Exception:  # noqa: BLE001
            pass
        # AI-synthesized BGM mixes carry a .bgmmix.json recipe sidecar —
        # flagged so the UI shows them apart from original music
        try:
            _ap = d.get("absolute_path") or ""
            if _ap and d.get("asset_type") == "audio" \
                    and os.path.exists(os.path.splitext(_ap)[0] + ".bgmmix.json"):
                d["bgmmix"] = True
        except Exception:  # noqa: BLE001
            pass
        out.append(d)
    return out


@app.post("/api/assets/scan")
def scan_assets(body: ScanRequest):
    from src.asset_manager.scanner import scan_asset_directory
    root = body.root.strip() or cfg("ASSET_ROOT_DIR", "resource/imports/")
    abs_root = _resolve(root)
    if not os.path.isdir(abs_root):
        raise HTTPException(400, f"Folder not found: {abs_root}")
    assets = _apply_immich_identity(scan_asset_directory(abs_root))
    SCANNED["assets"] = assets
    SCANNED["root"] = abs_root
    return {"root": abs_root, "assets": _assets_with_annotations(assets)}


@app.get("/api/assets/scan")
def last_scan():
    """Restore the last scan without a manual re-scan (page refresh / restart).

    Returns the in-memory scan if the server still holds one; otherwise runs a
    scan of the configured root — cheap now that scanner.py caches probed
    metadata on disk, so unchanged files are not re-hashed/re-probed.
    """
    from src.asset_manager.scanner import scan_asset_directory
    if SCANNED["assets"]:
        abs_root = SCANNED["root"]
        assets = SCANNED["assets"]
    else:
        abs_root = _resolve(cfg("ASSET_ROOT_DIR", "resource/imports/"))
        if not os.path.isdir(abs_root):
            return {"root": abs_root, "assets": [], "scanned": False}
        assets = _apply_immich_identity(scan_asset_directory(abs_root))
        SCANNED["assets"] = assets
        SCANNED["root"] = abs_root
    return {"root": abs_root, "assets": _assets_with_annotations(assets),
            "scanned": True}


class ByPathsRequest(BaseModel):
    paths: list[str] = []


_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"}


@app.post("/api/assets/by_paths")
def assets_by_paths(body: ByPathsRequest):
    """Resolve a list of project asset PATHS to their cached annotations (matched
    via the annotation index by absolute path / basename). Powers the pipeline
    canvas: shows which assets are in play + opens each one's annotation. Assets
    with no annotation still come back (type inferred from extension)."""
    from src.asset_manager.index_store import load_index
    idx = load_index()
    by_path: dict = {}
    for ann in idx.values():
        ap = getattr(ann.metadata, "absolute_path", "") or ""
        if ap:
            by_path[os.path.normcase(os.path.abspath(ap))] = ann
            by_path[os.path.basename(ap)] = ann

    def _infer_type(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        return "audio" if ext in _AUDIO_EXTS else "video" if ext in _VIDEO_EXTS else "video"

    out = []
    seen = set()
    for path in body.paths:
        if not path or path in seen:
            continue
        seen.add(path)
        ann = (by_path.get(os.path.normcase(os.path.abspath(_resolve(path))))
               or by_path.get(os.path.basename(path)))
        d = {
            "path": path,
            "file_name": os.path.basename(path),
            "asset_type": ann.asset_type if ann else _infer_type(path),
            "content_hash": ann.content_hash if ann else "",
            "annotated": ann is not None,
        }
        if ann is not None:
            d["annotation"] = _dump(ann.annotation)
        # journey metadata (cached per file name) — the canvas clusters the
        # video column by location/capture date to avoid a mile-high stack
        try:
            mm = _media_meta_for(os.path.abspath(_resolve(path)), d["file_name"])
            d["capture_time"] = mm.get("capture_time")
            d["location"] = mm.get("location")
        except Exception:  # noqa: BLE001
            pass
        out.append(d)
    return {"assets": out}


def _analysis_details(content_hash: str, variant: str = "") -> dict:
    """Per-clip captions + scene summaries from the analyzed cache."""
    from src.analyzer import get_analysis_path
    cache_dir = get_analysis_path(content_hash, variant)
    result = {"clips": [], "scenes": []}
    ckpt = os.path.join(cache_dir, "captions", "ckpt")
    if os.path.isdir(ckpt):
        for fn in sorted(os.listdir(ckpt)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(ckpt, fn), "r", encoding="utf-8") as f:
                        result["clips"].append(json.load(f))
                except Exception:
                    pass
    sm = os.path.join(cache_dir, "captions", "scene_summaries_video")
    if os.path.isdir(sm):
        for fn in sorted(os.listdir(sm)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(sm, fn), "r", encoding="utf-8") as f:
                        result["scenes"].append(json.load(f))
                except Exception:
                    pass
    # measured voice/laughter segments in the ORIGINAL audio (may be absent
    # for annotations that predate the feature — the UI offers on-demand
    # detect). Signal-only, so any track's cache dir is equally valid.
    from src.audio.sound_highlights import _CACHE_VERSION as _SHL_V
    for _d in (cache_dir, get_analysis_path(content_hash),
               get_analysis_path(content_hash, "local")):
        shl = os.path.join(_d, "sound_highlights.json")
        if os.path.exists(shl):
            try:
                with open(shl, "r", encoding="utf-8") as f:
                    _sd = json.load(f)
                # stale detector version → pretend absent so the UI offers
                # re-detection instead of showing outdated results
                if int(_sd.get("version", 1)) >= _SHL_V:
                    result["sound_highlights"] = _sd.get("segments", [])
                    break
            except Exception:  # noqa: BLE001
                pass
    # highlight-pool scores (curation-first) — any version renders; missing
    # rationale fields simply hide in the UI
    hp = os.path.join(get_analysis_path(content_hash), "highlight_pool.json")
    if os.path.exists(hp):
        try:
            with open(hp, "r", encoding="utf-8") as f:
                _hd = json.load(f)
            _moments = _hd.get("moments", [])
            # 细评结论随行(标注时逐片段精细化评判 → 详情页图形化)
            try:
                from src.fine_review import load_fine_review, apply_measured_caps
                _fr = load_fine_review(content_hash)
                for _m in _moments:
                    _k = f"{float(_m.get('start') or 0):.1f}:{float(_m.get('end') or 0):.1f}"
                    if _k in _fr:
                        _m["fine"] = apply_measured_caps(_fr[_k], _m)
            except Exception:  # noqa: BLE001
                pass
            result["highlight_pool"] = sorted(
                _moments, key=lambda m: -m.get("score", 0))
            result["highlight_pool_version"] = _hd.get("version", 1)
        except Exception:  # noqa: BLE001
            pass
        # 细评进行中的进度(UI 轮询)
        _frp = os.path.join(get_analysis_path(content_hash), "fine_review.progress.json")
        if os.path.exists(_frp):
            try:
                with open(_frp, "r", encoding="utf-8") as f:
                    result["fine_review_progress"] = json.load(f)
            except Exception:  # noqa: BLE001
                pass
    else:
        # live progress while the background scorer runs
        pp = os.path.join(get_analysis_path(content_hash), "highlight_pool.progress.json")
        if os.path.exists(pp):
            try:
                with open(pp, "r", encoding="utf-8") as f:
                    result["highlight_pool_progress"] = json.load(f)
            except Exception:  # noqa: BLE001
                pass
    return result


@app.get("/api/assets/{content_hash}/details")
def asset_details(content_hash: str, variant: str = ""):
    return _analysis_details(content_hash, variant)


class FineReviewRequest(BaseModel):
    content_hash: str


@app.post("/api/assets/fine_review")
def asset_fine_review(body: FineReviewRequest):
    """对一个素材的全部池时刻做 VLM 细评(后台子进程,详情页轮询进度)。

    计费:约 (池时刻数/4) 次视觉调用;结论按区间键控缓存,可断点续评。"""
    from src.analyzer import get_analysis_path
    cache_dir = get_analysis_path(body.content_hash)
    if not os.path.isdir(cache_dir):
        raise HTTPException(400, "该素材还没有分析缓存 — 先标注一次")
    prog_path = os.path.join(cache_dir, "fine_review.progress.json")
    try:
        if os.path.exists(prog_path) and time.time() - os.path.getmtime(prog_path) < 120:
            return {"status": "reviewing"}
    except OSError:
        pass
    code = (
        "import sys\n"
        f"sys.path.insert(0, {PROJECT_ROOT!r})\n"
        "from src.analyzer import _ensure_ffmpeg_on_path\n"
        "_ensure_ffmpeg_on_path()\n"
        "from src.fine_review import fine_review_source\n"
        f"fine_review_source({body.content_hash!r})\n"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return {"status": "started"}


# 批量细评:一个后台线程串行跑(并发多素材会触发 Gemini 503),全局状态轮询
_FR_BATCH = {"running": False, "current": "", "done": 0, "total": 0, "errors": 0}


class FineReviewBatchRequest(BaseModel):
    content_hashes: list[str]


@app.post("/api/assets/fine_review_batch")
def asset_fine_review_batch(body: FineReviewBatchRequest):
    """批量细评选中的素材(串行,断点续评——已评过的段落自动跳过)。"""
    if _FR_BATCH["running"]:
        raise HTTPException(409, "已有批量细评在跑")
    hashes = [h for h in body.content_hashes if h]
    if not hashes:
        raise HTTPException(400, "没有可细评的素材(需先标注)")
    _FR_BATCH.update({"running": True, "current": "", "done": 0,
                      "total": len(hashes), "errors": 0})

    def _run():
        from src.analyzer import _ensure_ffmpeg_on_path
        _ensure_ffmpeg_on_path()
        from src.fine_review import fine_review_source
        for ch in hashes:
            _FR_BATCH["current"] = ch
            try:
                fine_review_source(ch)
            except Exception as e:  # noqa: BLE001
                _FR_BATCH["errors"] += 1
                print(f"⚠️ [FineReview] batch: {ch[:16]} failed: {str(e)[:100]}")
            _FR_BATCH["done"] += 1
        _FR_BATCH.update({"running": False, "current": ""})

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started", "total": len(hashes)}


@app.get("/api/assets/fine_review_batch/status")
def asset_fine_review_batch_status():
    return dict(_FR_BATCH)


class SoundHighlightRequest(BaseModel):
    content_hash: str
    path: str          # media path the player already uses
    threshold: float | None = None   # VAD sensitivity; lower = wider segments


@app.post("/api/assets/sound_highlights")
def asset_sound_highlights(body: SoundHighlightRequest):
    """Detect voice/laughter segments for one asset on demand (backfill for
    annotations made before the feature). Cached in the analysis dir, so this
    runs the signal analysis at most once per asset."""
    from src.analyzer import get_analysis_path
    abs_path = _resolve(body.path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {body.path}")
    # highlights are signal-only — either annotation track's cache dir works
    cache_dir = get_analysis_path(body.content_hash)
    if not os.path.isdir(cache_dir):
        cache_dir = get_analysis_path(body.content_hash, "local")
    if not os.path.isdir(cache_dir):
        raise HTTPException(400, "该素材还没有分析缓存 — 先标注一次")
    from src.audio.sound_highlights import detect_sound_highlights
    segs = detect_sound_highlights(
        abs_path, cache_path=os.path.join(cache_dir, "sound_highlights.json"),
        threshold=body.threshold)
    return {"segments": segs}


class HighlightPoolRequest(BaseModel):
    content_hash: str
    force: bool = False


@app.post("/api/assets/highlight_pool")
def asset_highlight_pool(body: HighlightPoolRequest):
    """Build (or rebuild) one source's highlight-pool scores in the background.

    First build measures footage quality per dense segment (1-3 min for a
    long video) — runs detached; the UI polls details until the file lands."""
    from src.analyzer import get_analysis_path
    from src.curation import _POOL_VERSION
    cache_dir = get_analysis_path(body.content_hash)
    if not os.path.isdir(cache_dir):
        raise HTTPException(400, "该素材还没有分析缓存 — 先标注一次")
    pool_path = os.path.join(cache_dir, "highlight_pool.json")
    if os.path.exists(pool_path) and not body.force:
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                if int(json.load(f).get("version", 0)) >= _POOL_VERSION:
                    return {"status": "ready"}
        except Exception:  # noqa: BLE001
            pass
    # already building? (progress file heartbeats every segment) — don't spawn twins
    prog_path = os.path.join(cache_dir, "highlight_pool.progress.json")
    try:
        if os.path.exists(prog_path) and time.time() - os.path.getmtime(prog_path) < 120:
            return {"status": "building"}
    except OSError:
        pass
    if os.path.exists(pool_path):
        try:
            os.remove(pool_path)   # stale version / force → rebuild
        except OSError:
            pass
    code = (
        "import sys\n"
        f"sys.path.insert(0, {PROJECT_ROOT!r})\n"
        "from src.curation import _source_pool\n"
        f"_source_pool({body.content_hash!r})\n"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    subprocess.Popen([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    return {"status": "building"}


class AnnotateRequest(BaseModel):
    content_hashes: list[str] = []   # empty → all new assets
    force: bool = False
    provider: str = "cloud"          # "cloud" | "local" — parallel annotation tracks


# Requests arriving while a batch is running are QUEUED and chained into a
# follow-up job automatically — clicking 标注 on other cards mid-batch just
# adds them to the line instead of being rejected/disabled.
_ANNOTATE_QUEUE: list = []
_ANNOTATE_LOCK = threading.Lock()

_LOCAL_ANN_PATH = os.path.join(PROJECT_ROOT, "Output", "asset_index", "annotations_local.json")


def _load_local_annotations() -> dict:
    try:
        with open(_LOCAL_ANN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_local_annotations(results: list) -> int:
    """Persist local-VLM annotations SEPARATELY from the cloud track."""
    store = _load_local_annotations()
    n = 0
    for r in results:
        if r is None:
            continue
        h = getattr(r, "content_hash", "") or getattr(getattr(r, "metadata", None), "content_hash", "")
        if not h:
            continue
        store[h] = {"metadata": _dump(r.metadata), "annotation": _dump(r.annotation)}
        n += 1
    os.makedirs(os.path.dirname(_LOCAL_ANN_PATH), exist_ok=True)
    with open(_LOCAL_ANN_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    return n


def _local_vlm_entry() -> dict:
    """The local model entry from the API pool (endpoint on 11434)."""
    try:
        with open(os.path.join(PROJECT_ROOT, "src", "api_pool.json"), "r", encoding="utf-8") as f:
            for e in json.load(f):
                if "11434" in str(e.get("endpoint", "")):
                    return e
    except Exception:  # noqa: BLE001
        pass
    return {"model": "openai/qwen2.5vl:latest",
            "endpoint": "http://127.0.0.1:11434/v1", "api_key": "sk-local"}


def _annotate_running_job():
    for j in reversed(list(JOBS.values())):
        if j.kind == "annotate" and j.status == "running":
            return j
    return None


@app.post("/api/assets/annotate")
def annotate(body: AnnotateRequest):
    """Annotate assets in a background thread (in-process, like Streamlit batch)."""
    from src.asset_manager.index_store import find_new_assets
    if not SCANNED["assets"]:
        raise HTTPException(400, "Scan first.")
    with _ANNOTATE_LOCK:
        running = _annotate_running_job()
        if running is not None:
            _ANNOTATE_QUEUE.append({"content_hashes": list(body.content_hashes or []),
                                    "force": bool(body.force),
                                    "provider": body.provider or "cloud"})
            running.add(f"[queue] +1 请求已排队（队列 {len(_ANNOTATE_QUEUE)}）")
            return {"job_id": None, "queued": True, "position": len(_ANNOTATE_QUEUE),
                    "message": f"已加入标注队列（第 {len(_ANNOTATE_QUEUE)} 位），当前批次完成后自动开始。"}
    if body.content_hashes:
        targets = [a for a in SCANNED["assets"] if a.content_hash in set(body.content_hashes)]
    else:
        targets = find_new_assets(SCANNED["assets"])
    if not targets:
        return {"job_id": None, "message": "All assets already annotated."}

    job = Job("annotate")
    # per-asset states keyed by content hash — the ONLY reliable way for the
    # UI to mark cards (filename matching breaks: the completion callback
    # reports the file that just FINISHED, and names aren't stable keys)
    _files_state = {t.content_hash: "p" for t in targets}          # p/r/d
    _name_to_hash = {getattr(t, "file_name", ""): t.content_hash for t in targets}
    job.meta.update({"current": 0, "total": len(targets), "filename": "",
                     "files": _files_state,
                     "names": {t.content_hash: getattr(t, "file_name", "") for t in targets},
                     "file_stages": {}})
    JOBS[job.id] = job

    def _reset_file_view():
        """Segment grids & stage stepper are scoped to the CURRENT file — old
        files' units (and their failures) must not bleed into the new one."""
        job.meta["tasks"] = {}
        job.meta["file_stages"] = {}
        job.meta["stage"] = ""
        job.meta["stage_detail"] = ""

    def _run():
        _reload_runtime_config()
        # feed fine-grained segment events (e.g. audio captioning) into this job
        from src.utils import progress as _progress
        _progress.HOOK = lambda ev: (_apply_progress_ev(job, ev), job.save())
        try:
            from src.asset_manager.annotator import batch_annotate, annotate_asset
            from src.asset_manager.index_store import upsert_annotations
            from src.analyzer import analyze_video, analyze_audio

            def _stage_cb(stage, status, detail, filename=None):
                # per-card stage line ("镜头检测 42% / 片段理解 …") — surfaced live
                # on the running asset's card + the stage stepper.
                # filename is set when videos annotate in PARALLEL processes:
                # stages route per-hash instead of the single global stepper.
                if filename:
                    h = _name_to_hash.get(filename)
                    if h:
                        if stage == "annotate" and status == "fail":
                            # analysis failed but is RESUMABLE — mark the file
                            # red; the asset stays 未标注 so the next batch run
                            # picks it up and resumes from the checkpoints
                            _files_state[h] = "f"
                            (job.meta.get("stage_by_hash") or {}).pop(h, None)
                        else:
                            _sbh = job.meta.setdefault("stage_by_hash", {})
                            if status in ("start", "progress"):
                                _sbh[h] = {"stage": stage, "detail": str(detail or "")[:60]}
                            else:
                                _sbh[h] = {"stage": "", "detail": ""}
                    job.add(f"[stage] {filename} · {stage} {status} {str(detail or '')[:80]}".rstrip())
                    return
                _fs = job.meta.setdefault("file_stages", {})
                if status in ("start", "progress"):
                    _fs[stage] = "running"
                    job.meta.update({"stage": stage,
                                     "stage_detail": str(detail or "")[:60]})
                elif status in ("done", "skip"):
                    _fs[stage] = "done" if status == "done" else "skip"
                    job.meta.update({"stage": "", "stage_detail": ""})
                job.add(f"[stage] {stage} {status} {detail or ''}".rstrip())

            if body.provider == "local":
                # LOCAL VLM track: sequential (single GPU), analysis cached
                # under {hash}@local/, annotation persisted in
                # annotations_local.json — parallel to the cloud track.
                from src import config as _cfg
                _lv = _local_vlm_entry()
                _prev = (_cfg.VIDEO_ANALYSIS_MODEL, _cfg.VIDEO_ANALYSIS_ENDPOINT,
                         _cfg.VIDEO_ANALYSIS_API_KEY,
                         getattr(_cfg, "CAPTION_BATCH_SIZE", 64))
                _cfg.VIDEO_ANALYSIS_MODEL = _lv["model"]
                _cfg.VIDEO_ANALYSIS_ENDPOINT = _lv["endpoint"]
                _cfg.VIDEO_ANALYSIS_API_KEY = _lv.get("api_key") or "sk-local"
                _cfg.CAPTION_BATCH_SIZE = 4      # ollama parallel slots
                job.add(f"[local] 使用本地模型 {_lv['model']} @ {_lv['endpoint']}")
                results = []
                try:
                    for i, meta in enumerate(targets, 1):
                        _files_state[meta.content_hash] = "r"
                        _reset_file_view()
                        job.meta.update({"current": i - 1, "total": len(targets),
                                         "filename": getattr(meta, "file_name", "")})
                        job.add(f"[file] {i}/{len(targets)} {getattr(meta, 'file_name', '')} (local)")
                        _t0 = time.time()
                        try:
                            if body.force and getattr(meta, "asset_type", "video") == "video":
                                analyze_video(getattr(meta, "absolute_path", ""),
                                              force=True, progress_callback=_stage_cb,
                                              variant="local",
                                              content_hash=getattr(meta, "content_hash", "") or None)
                            results.append(annotate_asset(
                                meta, model=_lv["model"], endpoint=_lv["endpoint"],
                                api_key=_lv.get("api_key") or "sk-local",
                                progress_callback=_stage_cb, variant="local"))
                            _files_state[meta.content_hash] = "d"
                            job.add(f"[file] done in {time.time() - _t0:.0f}s")
                        except Exception as e:  # noqa: BLE001
                            _files_state[meta.content_hash] = "f"
                            job.add(f"[file] 失败: {str(e)[:150]}")
                        job.meta.update({"current": i, "stage": "", "stage_detail": ""})
                finally:
                    (_cfg.VIDEO_ANALYSIS_MODEL, _cfg.VIDEO_ANALYSIS_ENDPOINT,
                     _cfg.VIDEO_ANALYSIS_API_KEY, _cfg.CAPTION_BATCH_SIZE) = _prev
                _n = _save_local_annotations(results)
                job.add(f"[local] 本地标注已持久化 {_n} 条 (annotations_local.json)")
            elif body.force:
                results = []
                for i, meta in enumerate(targets, 1):
                    _files_state[meta.content_hash] = "r"
                    _reset_file_view()
                    job.meta.update({"current": i - 1, "total": len(targets),
                                     "filename": getattr(meta, "file_name", "")})
                    job.add(f"[file] {i}/{len(targets)} {getattr(meta, 'file_name', '')}")
                    ap = getattr(meta, "absolute_path", "")
                    if getattr(meta, "asset_type", "video") == "video":
                        analyze_video(ap, force=True, progress_callback=_stage_cb,
                                      content_hash=getattr(meta, "content_hash", "") or None)
                    elif getattr(meta, "asset_type", "") == "audio":
                        analyze_audio(ap, force=True)
                    results.append(annotate_asset(meta))
                    _files_state[meta.content_hash] = "d"
                    job.meta.update({"current": i, "stage": ""})
                upsert_annotations(results)
            else:
                def _start_cb(filename):
                    h = _name_to_hash.get(filename)
                    if h:
                        _files_state[h] = "r"
                    _reset_file_view()
                    job.meta.update({"filename": filename})
                    job.add(f"[file] start {filename}")
                def _file_cb(current, total, filename):
                    h = _name_to_hash.get(filename)
                    if h:
                        _files_state[h] = "d"
                        (job.meta.get("stage_by_hash") or {}).pop(h, None)
                    job.meta.update({"current": current, "total": total})
                    job.add(f"[file] {current}/{total} {filename} done")
                results = batch_annotate(targets, progress_callback=_file_cb,
                                         stage_callback=_stage_cb, start_callback=_start_cb)
                upsert_annotations(results)
            # auto write-back: freshly annotated Immich-bound assets get their
            # description updated in Immich (searchable there too).
            # Local-track results are a parallel experiment — not written back.
            try:
                if body.provider == "local":
                    raise StopIteration
                _wb = _writeback_annotations(
                    [getattr(r.metadata, "file_name", "") for r in results if r is not None])
                if _wb.get("synced"):
                    job.add(f"[immich] 标注已回写 {_wb['synced']} 个资产描述")
                for _e2 in _wb.get("errors", [])[:3]:
                    job.add(f"[immich] 回写失败: {_e2}")
            except StopIteration:
                pass
            except Exception as _e:  # noqa: BLE001
                job.add(f"[immich] 回写跳过: {_e}")

            job.add(f"DONE — {len(results)} assets annotated")
            job.status = "done"
        except Exception as e:
            import traceback
            job.add(f"ERROR: {e}\n{traceback.format_exc()[-800:]}")
            job.status = "error"
        finally:
            _progress.HOOK = None
            job.save(force=True)
            # chain: start the next queued annotate request (merge all queued
            # hashes into ONE follow-up batch; a queued "annotate new" request
            # (empty hashes) makes the follow-up scan for everything new).
            # Drain under the lock, but CALL annotate() outside it — annotate()
            # acquires the same non-reentrant lock.
            _merged: list = []
            _force_any = False
            _scan_new = False
            _next_provider = "cloud"
            with _ANNOTATE_LOCK:
                if _ANNOTATE_QUEUE:
                    _next_provider = _ANNOTATE_QUEUE[0].get("provider", "cloud")
                    _rest = []
                    for _q in _ANNOTATE_QUEUE:
                        if _q.get("provider", "cloud") != _next_provider:
                            _rest.append(_q)
                            continue
                        if _q.get("content_hashes"):
                            _merged.extend(_q["content_hashes"])
                        else:
                            _scan_new = True
                        _force_any = _force_any or bool(_q.get("force"))
                    _ANNOTATE_QUEUE[:] = _rest
            if _merged or _scan_new:
                try:
                    annotate(AnnotateRequest(
                        content_hashes=[] if _scan_new else list(dict.fromkeys(_merged)),
                        force=_force_any,
                        provider=_next_provider,
                    ))
                    job.add("[queue] 队列中的请求已作为新批次启动")
                except Exception as _e:  # noqa: BLE001
                    job.add(f"[queue] 启动排队批次失败: {_e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id}


class SelectRequest(BaseModel):
    instruction: str = ""
    project_id: str = ""
    target_length: float = 0.0


@app.post("/api/assets/auto-select")
def auto_select(body: SelectRequest):
    """Agent asset selection as a staged background job (poll via /api/jobs)."""
    root = SCANNED["root"] or _resolve(cfg("ASSET_ROOT_DIR", "resource/imports/"))
    instr = body.instruction.strip() or cfg("INSTRUCTION", "").strip() or "travel montage"
    target_dur = body.target_length or (float(cfg("AUDIO_SEGMENT_MAX_DURATION_SEC", "20.0")) - 5.0)

    job = Job("select")
    job.meta["stages"] = {}
    JOBS[job.id] = job

    _STATUS_MAP = {"start": "running"}

    def _st(stage: str, status: str, detail: str = ""):
        status = _STATUS_MAP.get(status, status)
        job.meta["stages"] = {**job.meta.get("stages", {}), stage: {"status": status, "detail": detail}}
        job.add(f"[{stage}] {status} {detail}".rstrip())

    def _run():
        _reload_runtime_config()
        try:
            from src.asset_manager.selector import select_assets
            from src.utils.video_concat import create_slideshow_video

            sel = select_assets(
                instruction=instr,
                target_duration_sec=max(15.0, target_dur),
                stage_callback=_st,
            )

            if not (sel.selected_videos or sel.selected_images or sel.selected_audio):
                job.add(f"选材失败：{sel.rationale}")
                job.meta["selection"] = _dump(sel)
                job.status = "error"
                return

            def _abs(p):
                return p if os.path.isabs(p) else os.path.join(root, p)

            abs_videos = [_abs(p) for p in sel.selected_videos]

            if sel.selected_images:
                _st("slideshow", "running", f"{len(sel.selected_images)} 张图片")
                ss = create_slideshow_video(
                    [_abs(p) for p in sel.selected_images],
                    duration_per_image=float(cfg("ASSET_IMAGE_DURATION_SEC", "3.0")))
                if ss:
                    abs_videos.append(ss)
                    _st("slideshow", "done", "Ken Burns 幻灯片已生成")
                else:
                    _st("slideshow", "error", "幻灯片生成失败")
            else:
                _st("slideshow", "skip", "没有选中图片")

            _st("apply", "running")
            if abs_videos:
                save_config("VIDEO_PATH", "||".join(abs_videos))
            audio_abs = _abs(sel.selected_audio[0]) if sel.selected_audio else ""
            if audio_abs:
                save_config("AUDIO_PATH", audio_abs)
            if body.project_id:
                try:
                    p = _load_project(body.project_id)
                    if abs_videos:
                        p["videos"] = abs_videos
                    if audio_abs:
                        p["audio"] = audio_abs
                    p["selection_rationale"] = sel.rationale or ""
                    _save_project(p)
                except Exception:
                    pass
            _st("apply", "done", "已写入当前项目")

            job.meta["selection"] = _dump(sel)
            job.meta["videos"] = abs_videos
            job.meta["audio"] = audio_abs
            job.status = "done"
        except Exception as e:
            import traceback
            job.add(f"ERROR: {e}\n{traceback.format_exc()[-600:]}")
            job.status = "error"
        finally:
            job.save(force=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job.id}


# ── AI instruction suggestions ─────────────────────────────────────────────

class SuggestRequest(BaseModel):
    project_id: str = ""


@app.post("/api/instruction/suggestions")
def instruction_suggestions(body: SuggestRequest):
    """Generate a few diverse editing-instruction suggestions from asset annotations."""
    import re as _re
    import litellm
    from src.asset_manager.index_store import get_all_summaries

    summaries = get_all_summaries()
    if summaries.startswith("(No annotated"):
        raise HTTPException(400, "没有已标注素材 — 先在素材库扫描并标注。")

    project_ctx = ""
    if body.project_id:
        try:
            p = _load_project(body.project_id)
            names = [os.path.basename(v) for v in p.get("videos", [])]
            if names:
                project_ctx = "当前项目已选视频素材：" + "、".join(names)
            if p.get("audio"):
                project_ctx += f"；音乐：{os.path.basename(p['audio'])}"
        except HTTPException:
            pass

    prompt = (
        "你是短视频混剪的创意顾问。根据下面的素材标注摘要"
        + ("和当前项目已选素材" if project_ctx else "")
        + "，提出 4 条风格差异明显的剪辑指令建议"
        "（每条一句话、20~40 个字、中文；风格覆盖如：情感叙事、快节奏卡点、氛围沉浸、旅行记录等）。\n\n"
        + (project_ctx + "\n\n" if project_ctx else "")
        + f"素材库摘要：\n{summaries[:6000]}\n\n"
        '只返回 JSON 数组，例如 ["建议一","建议二","建议三","建议四"]，不要其他内容。'
    )

    kwargs: dict = dict(
        model=cfg("AGENT_LITELLM_MODEL", ""),
        messages=[{"role": "user", "content": prompt}],
        # reasoning models (e.g. gemini-flash thinking) burn budget on thought
        # tokens first — a small max_tokens yields EMPTY content
        temperature=0.9, max_tokens=8192,
    )
    if cfg("AGENT_LITELLM_URL", ""):
        kwargs["api_base"] = cfg("AGENT_LITELLM_URL", "")
    if cfg("AGENT_LITELLM_API_KEY", ""):
        kwargs["api_key"] = cfg("AGENT_LITELLM_API_KEY", "")

    finish_reason = ""
    try:
        raw = litellm.completion(**kwargs)
        msg = raw.choices[0].message
        finish_reason = str(getattr(raw.choices[0], "finish_reason", "") or "")
        content = msg.content or getattr(msg, "reasoning_content", None) or ""
    except Exception as e:
        raise HTTPException(502, f"LLM 调用失败：{str(e)[:200]}")

    text = content.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()
    arr: list = []
    try:
        arr = json.loads(text)
    except Exception:
        s, e2 = text.find("["), text.rfind("]")
        if s >= 0 and e2 > s:
            try:
                arr = json.loads(text[s:e2 + 1])
            except Exception:
                arr = []
    arr = [str(x).strip() for x in arr if str(x).strip()][:6]
    if not arr:
        raise HTTPException(
            502,
            f"无法解析模型返回（finish_reason={finish_reason or '?'}，长度={len(content)}）：{content[:150]}")
    return {"suggestions": arr}


@app.post("/api/params/suggestions")
def params_suggestions(body: SuggestRequest):
    """Suggest target_length + shot_length from the project's assets + instruction."""
    import re as _re
    import litellm
    from src.asset_manager.index_store import load_index

    _reload_runtime_config()
    if not body.project_id:
        raise HTTPException(400, "缺少 project_id")
    p = _load_project(body.project_id)
    instr = (p.get("instruction") or "").strip()

    idx = load_index()
    by_path: dict = {}
    for ann in idx.values():
        ap = getattr(ann.metadata, "absolute_path", "") or ""
        if ap:
            by_path[os.path.normcase(os.path.abspath(ap))] = ann
            by_path[os.path.basename(ap)] = ann

    def _find(path: str):
        if not path:
            return None
        return (by_path.get(os.path.normcase(os.path.abspath(_resolve(path))))
                or by_path.get(os.path.basename(path)))

    video_lines = []
    total_video_dur = 0.0
    for v in p.get("videos", []):
        a = _find(v)
        d = float(getattr(a.metadata, "duration_sec", 0) or 0) if a else 0.0
        total_video_dur += d
        summ = str(getattr(a.annotation, "summary", "") or "")[:80] if a else ""
        video_lines.append(f"- {os.path.basename(v)}（{d:.0f}s）{summ}")

    audio_desc = "（未选择音乐）"
    audio_dur = 0.0
    aa = _find(p.get("audio", ""))
    # MEASURED tempo beats any annotation: annotation.bpm historically came from
    # an LLM guess (it would answer genre clichés like "128" for a 70 BPM song).
    # Derive the felt pulse from the madmom downbeat grid cached in the audio's
    # analyzed captions — works for old caches too.
    measured_bpm = None
    try:
        _ap = os.path.normcase(os.path.abspath(_resolve(p.get("audio", "") or "")))
        if _ap:
            for _md in glob.glob(os.path.join(PROJECT_ROOT, "Output", "analyzed", "*", "metadata.json")):
                try:
                    with open(_md, "r", encoding="utf-8") as fh:
                        _meta = json.load(fh)
                except Exception:
                    continue
                if os.path.normcase(str(_meta.get("absolute_path", ""))) != _ap:
                    continue
                _cj = os.path.join(os.path.dirname(_md), "captions.json")
                if os.path.exists(_cj):
                    with open(_cj, "r", encoding="utf-8") as fh:
                        _cap = json.load(fh)
                    measured_bpm = ((_cap.get("facts") or {}).get("bpm_felt")
                                    or (_cap.get("measured_tempo") or {}).get("bpm_felt"))
                    if not measured_bpm:  # older caches: derive from downbeat gaps
                        _db = sorted(k["time"] for k in (_cap.get("_keypoints_detail") or [])
                                     if k.get("type") == "Downbeat")
                        _gaps = sorted(b - a for a, b in zip(_db, _db[1:]) if 0.8 <= b - a <= 8.0)
                        if len(_gaps) >= 4:
                            _bar = _gaps[len(_gaps) // 2]
                            measured_bpm = round(240.0 / _bar, 1)
                break
    except Exception:
        measured_bpm = None
    if aa:
        audio_dur = float(getattr(aa.metadata, "duration_sec", 0) or 0)
        bpm = measured_bpm or getattr(aa.annotation, "bpm", None)
        bpm_tag = "（实测律动）" if measured_bpm else "（估计，未实测）"
        energy = getattr(aa.annotation, "energy_level", None)
        genre = getattr(aa.annotation, "genre", None)
        audio_desc = (f"时长 {audio_dur:.0f}s"
                      + (f"，BPM {bpm}{bpm_tag}" if bpm else "")
                      + (f"，能量 {energy}" if energy else "")
                      + (f"，曲风 {genre}" if genre else ""))
    elif p.get("audio"):
        audio_desc = f"{os.path.basename(p['audio'])}（未标注，无节奏信息）"

    prompt = (
        "你是短视频混剪参数顾问。根据剪辑指令和素材情况，推荐两项参数：\n"
        "1. target_length_sec：成片目标时长（秒，10~120 之间，不得超过音乐时长的 90%，"
        "也要考虑视频素材总量是否足够填满）\n"
        "2. shot_length_sec：单镜头平均长度（秒，0.5~8 之间。快节奏卡点应对齐节拍——"
        "例如 1 拍 = 60/BPM 秒、1 小节 = 240/BPM 秒；情感叙事可以更长）\n\n"
        f"剪辑指令：{instr or '（未填写）'}\n"
        f"音乐：{audio_desc}\n"
        f"视频素材（共 {total_video_dur:.0f}s）：\n" + ("\n".join(video_lines) or "（未选择视频）") + "\n\n"
        '只返回 JSON，例如 {"target_length_sec": 35, "shot_length_sec": 2.0, "rationale": "一句话理由"}'
    )

    kwargs: dict = dict(
        model=cfg("AGENT_LITELLM_MODEL", ""),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4, max_tokens=8192,
    )
    if cfg("AGENT_LITELLM_URL", ""):
        kwargs["api_base"] = cfg("AGENT_LITELLM_URL", "")
    if cfg("AGENT_LITELLM_API_KEY", ""):
        kwargs["api_key"] = cfg("AGENT_LITELLM_API_KEY", "")

    try:
        raw = litellm.completion(**kwargs)
        msg = raw.choices[0].message
        content = msg.content or getattr(msg, "reasoning_content", None) or ""
    except Exception as e:
        raise HTTPException(502, f"LLM 调用失败：{str(e)[:200]}")

    text = content.strip()
    m = _re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, _re.DOTALL)
    if m:
        text = m.group(1).strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        s, e2 = text.find("{"), text.rfind("}")
        if s >= 0 and e2 > s:
            try:
                parsed = json.loads(text[s:e2 + 1])
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        raise HTTPException(502, f"无法解析模型返回：{content[:150]}")

    try:
        target = float(parsed.get("target_length_sec", 30))
        shot = float(parsed.get("shot_length_sec", 3))
    except Exception:
        raise HTTPException(502, f"模型返回的参数不是数字：{parsed}")

    # clamp to sane / feasible bounds
    target = max(10.0, min(300.0, target))
    if audio_dur > 5:
        target = min(target, round(audio_dur * 0.9, 1))
    shot = max(0.3, min(15.0, round(shot, 2)))

    return {
        "target_length": round(target, 1),
        "shot_length": shot,
        "rationale": str(parsed.get("rationale", ""))[:300],
    }


# ── Analysis data (for chart visualizations) ───────────────────────────────

@app.get("/api/json")
def get_json_file(path: str):
    """Serve a JSON artifact (shot_point / captions / …) under Output/."""
    abs_path = os.path.abspath(_resolve(path))
    out_root = os.path.abspath(os.path.join(PROJECT_ROOT, "Output"))
    if not abs_path.startswith(out_root):
        raise HTTPException(403, "only files under Output/ are allowed")
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    with open(abs_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/audio/keypoints")
def audio_keypoints(path: str):
    """Raw madmom keypoints for an audio file (from the shared keypoint cache)."""
    abs_path = _resolve(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(404, f"not found: {path}")
    from src.asset_manager.scanner import compute_content_hash
    import src.config as config
    ch = compute_content_hash(abs_path)
    kp_dir = os.path.join(_resolve(getattr(config, "VIDEO_DATABASE_FOLDER", "./Output/")),
                          "analyzed", "keypoints")
    out: dict = {}
    if os.path.isdir(kp_dir):
        for fn in os.listdir(kp_dir):
            if fn.startswith(ch[:16]) and fn.endswith(".json"):
                parts = fn[:-5].split("_")
                method = parts[1] if len(parts) > 1 else fn
                try:
                    with open(os.path.join(kp_dir, fn), "r", encoding="utf-8") as f:
                        out[method] = json.load(f)
                except Exception:
                    pass
    return out


# ── Projects ────────────────────────────────────────────────────────────────
# A project = one edit: selected assets, instruction, params, pipeline result.
# Asset annotations stay asset-level (hash-cached), independent of projects.

PROJECTS_DIR = os.path.join(PROJECT_ROOT, "Output", "projects")

_PROJECT_DEFAULTS = {
    "name": "",
    "videos": [], "audio": "", "audios": [], "instruction": "",
    "has_dialogue": False, "main_character": "", "srt": "",
    "target_length": 30.0, "shot_length": 4.0,
    "selection_rationale": "",
    "shot_point": "", "effective_video": "",
    "last_run_at": None, "last_run_status": "",
}


def _project_file(pid: str) -> str:
    return os.path.join(PROJECTS_DIR, pid, "project.json")


def _load_project(pid: str) -> dict:
    fp = _project_file(pid)
    if not os.path.exists(fp):
        raise HTTPException(404, f"project not found: {pid}")
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_project(p: dict) -> dict:
    p["updated_at"] = time.time()
    os.makedirs(os.path.dirname(_project_file(p["id"])), exist_ok=True)
    with open(_project_file(p["id"]), "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    return p


@app.get("/api/projects")
def list_projects():
    out = []
    if os.path.isdir(PROJECTS_DIR):
        for pid in os.listdir(PROJECTS_DIR):
            fp = _project_file(pid)
            if os.path.exists(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        out.append(json.load(f))
                except Exception:
                    pass
    out.sort(key=lambda p: p.get("updated_at", 0), reverse=True)
    return out


class ProjectCreate(BaseModel):
    name: str = ""
    from_config: bool = False   # seed with legacy config.py values


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    pid = "p_" + uuid.uuid4().hex[:8]
    p = dict(_PROJECT_DEFAULTS)
    p["id"] = pid
    p["name"] = body.name.strip() or f"项目 {time.strftime('%m-%d %H:%M')}"
    p["created_at"] = time.time()
    if body.from_config:
        p["videos"] = [v for v in cfg("VIDEO_PATH", "").split("||") if v]
        p["audio"] = cfg("AUDIO_PATH", "")
        p["instruction"] = cfg("INSTRUCTION", "")
    return _save_project(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _load_project(pid)


class ProjectPatch(BaseModel):
    patch: dict


@app.put("/api/projects/{pid}")
def update_project(pid: str, body: ProjectPatch):
    p = _load_project(pid)
    allowed = set(_PROJECT_DEFAULTS.keys())
    for k, v in body.patch.items():
        if k in allowed:
            p[k] = v
    return _save_project(p)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    fp = _project_file(pid)
    if os.path.exists(fp):
        os.remove(fp)
        try:
            os.rmdir(os.path.dirname(fp))
        except OSError:
            pass
    return {"ok": True}


# ── Pipeline ────────────────────────────────────────────────────────────────

_STAGE_NAMES = ["shot_detection", "asr", "video_captioning", "audio_analysis", "screenwriter", "editor"]
_STAGE_STARTS = {
    "shot_detection": "[Step 1] Extracting video frames",
    "asr": "[Thread A: ASR]", "video_captioning": "[Thread B: Video]",
    "audio_analysis": "[Thread C: Audio]",
    "screenwriter": "Running Screenwriter", "editor": "Running EditorCoreAgent",
}
_STAGE_ENDS = {
    "shot_detection": "[Step 1] Shot detection completed",
    "asr": "[Thread A] ✨ Completed", "video_captioning": "[Thread B] ✨ Completed",
    "audio_analysis": "[Thread C] ✨ Completed",
    "screenwriter": "Shot plan generated successfully", "editor": "Video clip selection completed",
}
_STAGE_ERRORS = {"asr": "[Thread A] ❌", "video_captioning": "[Thread B] ❌", "audio_analysis": "[Thread C] ❌"}
_TIME_RE = re.compile(r"[Cc]ompleted in ([\d.]+)s")


def _parse_stage(job: Job, line: str):
    ss = job.meta["stages"]
    times = job.meta["stage_times"]
    for stage, kw in _STAGE_STARTS.items():
        if kw in line and ss.get(stage) == "pending":
            ss[stage] = "running"
    for stage, kw in _STAGE_ENDS.items():
        if kw in line and ss.get(stage) == "running":
            ss[stage] = "done"
            m = _TIME_RE.search(line)
            if m:
                times[stage] = float(m.group(1))
    for stage, kw in _STAGE_ERRORS.items():
        if kw in line:
            ss[stage] = "error"
    if "❌ Pipeline stage" in line:
        for stage, status in ss.items():
            if status == "running":
                ss[stage] = "error"


_EVENT_STATE = {"start": "r", "done": "d", "fail": "f"}


def _apply_progress_ev(job: Job, ev: dict):
    """Fold one fine-grained progress event into job.meta['tasks']."""
    with job.lock:
        tasks = job.meta.setdefault("tasks", {})
        name = str(ev.get("task", "task"))
        t = tasks.setdefault(name, {"total": 0, "states": {}})
        total = ev.get("total")
        if isinstance(total, int) and total > 0:
            t["total"] = total
        idx = ev.get("idx", -1)
        event = ev.get("event", "")
        if event == "stage":
            # coarse 'which sub-step' label for a phase (e.g. screenwriter:
            # 选择音乐段落 → 生成分镜脚本). Lives on the task, not in the LLM
            # call trace, so it never pollutes the prompt/reply workbench.
            t["stage_label"] = str(ev.get("note", ""))[:40]
            return
        if event == "step" and isinstance(idx, int) and idx >= 0:
            # detailed agent iteration step — stored in traces, fetched on demand
            traces = job.meta.setdefault("traces", {}).setdefault(name, {})
            lst = traces.setdefault(str(idx), [])
            lst.append({k: ev[k] for k in ("phase", "iter", "max_iter", "elapsed", "tool", "args", "reply", "verdict", "result", "note", "stage") if k in ev})
            if len(lst) > 80:
                del lst[:len(lst) - 80]
            t.setdefault("iters", {})[str(idx)] = f"{ev.get('iter', '?')}/{ev.get('max_iter', '?')}"
            return
        if event == "reset":
            t["states"] = {}
            t["labels"] = {}
        if isinstance(idx, int) and idx >= 0:
            if "label" in ev:
                t.setdefault("labels", {})[str(idx)] = str(ev["label"])[:60]
            if event == "retry":
                t["states"].pop(str(idx), None)   # back to pending
            elif event in _EVENT_STATE:
                t["states"][str(idx)] = _EVENT_STATE[event]
        t["done"] = sum(1 for v in t["states"].values() if v == "d")
        t["fail"] = sum(1 for v in t["states"].values() if v == "f")
        for k in ("avg", "eta"):
            if k in ev:
                t[k] = ev[k]


def _parse_progress_line(job: Job, line: str):
    pos = line.find("@@PROGRESS ")
    if pos < 0:
        return
    try:
        ev = json.loads(line[pos + len("@@PROGRESS "):])
    except Exception:
        return
    _apply_progress_ev(job, ev)
    job.save()


def _on_pipeline_line(job: Job, line: str):
    _parse_stage(job, line)
    _parse_progress_line(job, line)


def _derive_shot_point_path(video_paths: list, audio_path: str, instruction: str) -> str:
    """Mirror local_run.py exactly: project dir = {primary_hash[:12]}_{audio_id}."""
    import src.config as config
    audio_id = os.path.splitext(os.path.basename(audio_path))[0].replace('.', '_').replace(' ', '_') if audio_path else "no_audio"
    ih = hashlib.md5(instruction.encode("utf-8")).hexdigest()[:8]
    # Hash-only id (must match local_run.py exactly): always ASCII, no sanitizing.
    iid = f"instruction_{ih}"
    primary_hash = ""
    try:
        from src.asset_manager.scanner import compute_content_hash
        primary_hash = compute_content_hash(_resolve(video_paths[0]))[:12]
    except Exception:
        pass
    proj = f"{primary_hash or 'unknown'}_{audio_id}"
    return os.path.join(config.VIDEO_DATABASE_FOLDER, 'Output', proj, f"shot_point_{iid}.json")


@app.get("/api/pipeline/shots")
def pipeline_shots(project_id: str):
    """Final selected shots (shot_point.json): each shot's source video + time
    slices. Powers the canvas's per-shot clip nodes + preview. Returns [] until
    the shot plan exists."""
    try:
        p = _load_project(project_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "project not found")
    sp = _derive_shot_point_path(p.get("videos", []), p.get("audio", ""), p.get("instruction", ""))
    if not os.path.isfile(sp):
        return {"shots": []}
    try:
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {"shots": []}
    out = []
    for s in (data if isinstance(data, list) else []):
        if not isinstance(s, dict):
            continue
        default_src = s.get("video_path") or ""
        clips = [{
            "video_path": c.get("video_path") or default_src,
            "start": c.get("start"), "end": c.get("end"), "duration": c.get("duration"),
        } for c in (s.get("clips") or []) if isinstance(c, dict)]
        out.append({
            "section_idx": s.get("section_idx"), "shot_idx": s.get("shot_idx"),
            "video_path": default_src, "is_stitched": bool(s.get("is_stitched")),
            "fallback": bool(s.get("fallback")), "clips": clips,
        })
    return {"shots": out}


class PipelineRequest(BaseModel):
    video_paths: list[str]
    audio_path: str
    instruction: str
    has_dialogue: bool = False
    main_character: str = ""
    srt_path: str = ""
    target_length: float = 30.0
    shot_length: float = 4.0
    project_id: str = ""


MIN_SHOT = 0.2
SEG_FLOOR = 0.1
SHOT_RANGE_CAP = 1.0


def _shot_bounds(shot_length: float):
    shot_length = max(MIN_SHOT, float(shot_length))
    r = min(SHOT_RANGE_CAP, max(SEG_FLOOR, shot_length))
    return round(max(SEG_FLOOR, shot_length - r), 3), round(shot_length + r, 3)


@app.post("/api/pipeline/start")
def pipeline_start(body: PipelineRequest):
    global PIPELINE_JOB_ID
    if PIPELINE_JOB_ID and JOBS.get(PIPELINE_JOB_ID) and JOBS[PIPELINE_JOB_ID].status == "running":
        raise HTTPException(409, "Pipeline already running.")
    videos = [p for p in body.video_paths if p and p.strip()]
    if not videos:
        raise HTTPException(400, "No videos selected.")
    if not body.instruction.strip():
        raise HTTPException(400, "剪辑指令为空 — 请先填写你想要的剪辑效果。")
    video_type = "film" if body.has_dialogue else "vlog"
    min_d = max(5.0, body.target_length - 5.0)
    max_d = body.target_length + 5.0
    # Shot length is no longer a user setting — pacing is decided automatically
    # from the music's energy (self-calibrating). AUDIO_MIN/MAX_SEGMENT_DURATION
    # stay at their config defaults (the perceptual fast/slow bounds) and are NOT
    # overridden here.

    # persist UI choices like the Streamlit app does
    save_config("VIDEO_PATH", "||".join(videos))
    save_config("AUDIO_PATH", body.audio_path)
    save_config("INSTRUCTION", body.instruction)
    save_config("AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d))
    save_config("AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d))

    cmd = [
        sys.executable, "local_run.py",
        "--Video_Path", *[_resolve(v) for v in videos],
        "--Audio_Path", _resolve(body.audio_path),
        "--Instruction", body.instruction,
        "--type", video_type, "--instruction_type", "object",
        "--config.AUDIO_SEGMENT_MIN_DURATION_SEC", str(min_d),
        "--config.AUDIO_SEGMENT_MAX_DURATION_SEC", str(max_d),
    ]
    for key in ("VIDEO_ANALYSIS_MODEL", "VIDEO_ANALYSIS_ENDPOINT", "VIDEO_ANALYSIS_API_KEY",
                "AUDIO_LITELLM_MODEL", "AUDIO_LITELLM_BASE_URL", "AUDIO_LITELLM_API_KEY",
                "AGENT_LITELLM_MODEL", "AGENT_LITELLM_URL", "AGENT_LITELLM_API_KEY"):
        v = cfg(key, "")
        if v:
            cmd += [f"--config.{key}", v]
    if body.main_character.strip():
        cmd += ["--config.MAIN_CHARACTER_NAME", body.main_character.strip()]
    if body.srt_path.strip():
        cmd += ["--SRT_Path", _resolve(body.srt_path.strip())]

    # "effective video" for downstream use = the primary (first) REAL video;
    # multi-source projects never pre-merge, so a planned merged path is a lie
    effective = _resolve(videos[0])
    shot_point = _derive_shot_point_path(videos, body.audio_path, body.instruction)

    job = Job("pipeline")
    job.meta.update({
        "stages": {s: "pending" for s in _STAGE_NAMES},
        "stage_times": {}, "start_time": time.time(),
        "shot_point": shot_point, "effective_video": effective,
        "project_id": body.project_id,
    })
    # snapshot settings + result location into the project record
    if body.project_id:
        try:
            p = _load_project(body.project_id)
            p.update(
                videos=videos, audio=body.audio_path, instruction=body.instruction,
                has_dialogue=body.has_dialogue, main_character=body.main_character,
                srt=body.srt_path, target_length=body.target_length,
                shot_length=body.shot_length, shot_point=shot_point,
                effective_video=effective,
                last_run_at=time.time(), last_run_status="running",
            )
            _save_project(p)
        except HTTPException:
            pass
    try:
        _spawn(job, cmd, on_line=_on_pipeline_line)
    except Exception as e:
        raise HTTPException(500, str(e))
    PIPELINE_JOB_ID = job.id
    return {"job_id": job.id, "shot_point": shot_point, "effective_video": effective}


class RetryShotRequest(BaseModel):
    project_id: str
    section_idx: int
    shot_idx: int


def _remove_shot_from_point(shot_point_path: str, section_idx: int, shot_idx: int) -> bool:
    """Drop one shot's committed pick so the next run re-selects it. True if removed."""
    if not shot_point_path or not os.path.exists(shot_point_path):
        return False
    try:
        with open(shot_point_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if not isinstance(data, list):
        return False
    kept = [r for r in data if not (
        isinstance(r, dict)
        and r.get("section_idx") == section_idx
        and r.get("shot_idx") == shot_idx
    )]
    if len(kept) == len(data):
        return False
    with open(shot_point_path, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
    return True


@app.post("/api/pipeline/retry_shot")
def retry_shot(body: RetryShotRequest):
    """Re-generate a single shot: drop its committed pick from shot_point, then
    relaunch the pipeline with the project's saved params. Analysis + shot plan
    are cached, so the run resumes and only the dropped shot is re-selected. For
    a shot that FAILED (never committed) this is just a resume — nothing to drop."""
    if PIPELINE_JOB_ID and JOBS.get(PIPELINE_JOB_ID) and JOBS[PIPELINE_JOB_ID].status == "running":
        raise HTTPException(409, "流水线正在运行，请先停止再重试单个镜头。")
    p = _load_project(body.project_id)
    removed = _remove_shot_from_point(p.get("shot_point", ""), body.section_idx, body.shot_idx)
    preq = PipelineRequest(
        video_paths=p.get("videos", []) or [],
        audio_path=p.get("audio", "") or "",
        instruction=p.get("instruction", "") or "",
        has_dialogue=bool(p.get("has_dialogue", False)),
        main_character=p.get("main_character", "") or "",
        srt_path=p.get("srt", "") or "",
        target_length=float(p.get("target_length", 30.0) or 30.0),
        shot_length=float(p.get("shot_length", 4.0) or 4.0),
        project_id=body.project_id,
    )
    result = pipeline_start(preq)
    return {**result, "removed": removed,
            "section_idx": body.section_idx, "shot_idx": body.shot_idx}


@app.post("/api/pipeline/stop")
def pipeline_stop():
    job = JOBS.get(PIPELINE_JOB_ID or "")
    if not job or job.status != "running":
        return {"ok": False, "message": "Not running."}
    _kill(job)
    return {"ok": True}


@app.get("/api/pipeline/current")
def pipeline_current(since: int = 0, project_id: str = ""):
    if project_id:
        # latest pipeline job belonging to THIS project (running or finished)
        for job in reversed(list(JOBS.values())):
            if job.kind == "pipeline" and job.meta.get("project_id") == project_id:
                return {"job": job.to_dict(since)}
        return {"job": None}
    job = JOBS.get(PIPELINE_JOB_ID or "")
    if not job:
        return {"job": None}
    return {"job": job.to_dict(since)}


# ── API 成本可视化 ──────────────────────────────────────────────────────────
# 流水线子进程经 llm_logger 把每次 LLM 调用(token/媒体体积/阶段/成本)写进
# llm_calls_*.jsonl;这里聚合成实时账单。预估端点按"缓存命中→¥0,未命中→
# 历史单价×预期调用数"给跑前报价。

_USAGE_CACHE: dict = {}   # path -> (size, parsed_totals) 避免每次轮询重读全文件

_CNY = 7.2  # 展示用近似汇率


def _price_per_token(model: str) -> tuple[float, float]:
    """价格表按 provider/model 键控;日志里常只有裸模型名 → 逐个前缀试."""
    try:
        import litellm
    except Exception:
        return 0.0, 0.0
    candidates = [model] if "/" in model else [
        model, f"gemini/{model}", f"deepseek/{model}", f"openai/{model}", f"ollama/{model}"]
    for cand in candidates:
        try:
            i, o = litellm.cost_per_token(model=cand, prompt_tokens=1_000_000,
                                          completion_tokens=1_000_000)
            if i or o:
                return (i or 0.0) / 1e6, (o or 0.0) / 1e6
        except Exception:
            continue
    return 0.0, 0.0


def _llm_log_path_for_job(job) -> str | None:
    import re as _re
    for l in job.lines[:200]:
        m = _re.search(r"\[LLM Log\].*?to:\s*(.+llm_calls_\S+\.jsonl)", str(l))
        if m:
            p = m.group(1).strip()
            return p if os.path.isabs(p) else _resolve(p)
    return None


def _aggregate_llm_log(path: str) -> dict:
    """Sum one llm_calls jsonl → totals + by_stage + by_model (增量缓存)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    cached = _USAGE_CACHE.get(path)
    if cached and cached[0] == size:
        return cached[1]
    totals = {"calls": 0, "ok": 0, "fail": 0, "prompt_tokens": 0,
              "completion_tokens": 0, "cost_usd": 0.0, "images": 0, "media_bytes": 0}
    stages: dict = {}
    models: dict = {}
    _prices: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                u = r.get("usage") or {}
                pt, ct = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
                model = r.get("model") or "?"
                cost = r.get("cost_usd")
                if cost is None:  # 旧格式日志:按价格表现算
                    if model not in _prices:
                        _prices[model] = _price_per_token(model)
                    pi, po = _prices[model]
                    cost = pt * pi + ct * po
                media = r.get("media") or {}
                ok = r.get("status") == "success"
                totals["calls"] += 1
                totals["ok" if ok else "fail"] += 1
                totals["prompt_tokens"] += pt
                totals["completion_tokens"] += ct
                totals["cost_usd"] += cost or 0.0
                totals["images"] += media.get("images") or 0
                totals["media_bytes"] += media.get("bytes") or 0
                for key, bucket in ((r.get("stage") or "(未标注)", stages), (model, models)):
                    b = bucket.setdefault(key, {"calls": 0, "prompt_tokens": 0,
                                                "completion_tokens": 0, "cost_usd": 0.0,
                                                "images": 0, "media_bytes": 0})
                    b["calls"] += 1
                    b["prompt_tokens"] += pt
                    b["completion_tokens"] += ct
                    b["cost_usd"] += cost or 0.0
                    b["images"] += media.get("images") or 0
                    b["media_bytes"] += media.get("bytes") or 0
    except OSError:
        return {}
    out = {"totals": totals,
           "by_stage": [{"stage": k, **v} for k, v in stages.items()],
           "by_model": [{"model": k, **v} for k, v in models.items()]}
    _USAGE_CACHE[path] = (size, out)
    if len(_USAGE_CACHE) > 8:
        _USAGE_CACHE.pop(next(iter(_USAGE_CACHE)))
    return out


@app.get("/api/pipeline/usage")
def pipeline_usage(job_id: str = ""):
    """当前(或指定)流水线运行的实时 API 账单."""
    job = JOBS.get(job_id or PIPELINE_JOB_ID or "")
    if not job:
        return {"available": False, "reason": "no pipeline job"}
    path = _llm_log_path_for_job(job)
    if not path or not os.path.exists(path):
        return {"available": False, "reason": "log not started yet",
                "running": job.status == "running"}
    agg = _aggregate_llm_log(path)
    if not agg:
        return {"available": False, "reason": "log unreadable"}
    return {"available": True, "running": job.status == "running",
            "job_id": job.id, "log_path": path, "cny_rate": _CNY, **agg}


class EstimateRequest(BaseModel):
    video_paths: list[str] = []
    audio_path: str = ""
    target_length: float = 180.0


def _analysis_cached(ch: str) -> bool:
    """素材分析是否已完整缓存(镜头/描述/场景都在)."""
    base = _resolve(os.path.join("Output", "analyzed", ch))
    return (os.path.isdir(os.path.join(base, "captions", "scenes"))
            and os.path.isdir(os.path.join(base, "captions", "ckpt")))


def _recent_call_rate(model: str, default: float) -> float:
    """该模型近期日志的平均单次调用成本(自校准;无历史时用保守常数)."""
    import glob as _glob
    logs = sorted(_glob.glob(_resolve(os.path.join("Output", "logs", "llm_calls_*.jsonl"))),
                  key=os.path.getmtime)[-5:]
    tot, n = 0.0, 0
    pi, po = _price_per_token(model)
    short = model.split("/")[-1]
    for lp in logs:
        try:
            with open(lp, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if (r.get("model") or "").split("/")[-1] != short or r.get("status") != "success":
                        continue
                    u = r.get("usage") or {}
                    c = r.get("cost_usd")
                    if c is None:
                        c = (u.get("prompt_tokens") or 0) * pi + (u.get("completion_tokens") or 0) * po
                    tot += c
                    n += 1
        except OSError:
            continue
    return (tot / n) if n >= 10 else default


@app.post("/api/pipeline/estimate")
def pipeline_estimate(body: EstimateRequest):
    """跑前成本预估:缓存命中的素材 ¥0,未命中按时长推调用数 × 历史单价."""
    import src.config as config
    from src.asset_manager.scanner import compute_content_hash
    from src.asset_manager.index_store import load_index

    alias: dict[str, str] = {}
    try:
        for ch, ann in load_index().items():
            ap = getattr(getattr(ann, "metadata", None), "absolute_path", "")
            if ap:
                alias[os.path.normcase(os.path.abspath(ap))] = ch
    except Exception:
        pass

    vlm_rate = _recent_call_rate(cfg("VIDEO_ANALYSIS_MODEL", config.VIDEO_ANALYSIS_MODEL), 0.011)
    agent_rate = _recent_call_rate(cfg("AGENT_LITELLM_MODEL", config.AGENT_LITELLM_MODEL), 0.0012)

    files = []
    vlm_calls = 0
    for vp in body.video_paths:
        ap = _resolve(vp)
        ch = alias.get(os.path.normcase(os.path.abspath(ap))) or compute_content_hash(ap)
        cached = bool(ch) and _analysis_cached(ch)
        dur = 0.0
        calls = 0
        if not cached and os.path.exists(ap):
            try:
                from src.asset_manager.scanner import _probe_via_ffprobe
                dur = float(_probe_via_ffprobe(ap, "format=duration").get("duration") or 0)
            except Exception:
                dur = 0.0
            # 经验模型:片段≈27s 一段;每段 1 次描述 + 1 次密集描述;每 ~220s 一次场景分析
            clips = max(1, int(dur / 27 + 0.5))
            calls = clips * 2 + max(1, int(dur / 220 + 0.5))
            vlm_calls += calls
        files.append({"name": os.path.basename(vp), "cached": cached,
                      "duration_sec": round(dur), "est_vlm_calls": calls})

    # 音频:命中缓存 ¥0,否则约 8 次文本调用(能量段落 + LLM 描述)
    audio_calls = 0
    if body.audio_path:
        ach = compute_content_hash(_resolve(body.audio_path))
        acached = bool(ach) and os.path.exists(
            _resolve(os.path.join("Output", "analyzed", ach, "captions.json")))
        if not acached:
            audio_calls = 8

    # 编剧 ~6 次 + 剪辑每分镜 ~3 次(密集描述走缓存,剪辑 Agent 是纯文本模型)
    shots = max(1, int(body.target_length / 3.3))
    agent_calls = 6 + shots * 3 + audio_calls

    est = vlm_calls * vlm_rate + agent_calls * agent_rate
    return {
        "files": files,
        "cached_files": sum(1 for f in files if f["cached"]),
        "uncached_files": sum(1 for f in files if not f["cached"]),
        "est_vlm_calls": vlm_calls, "est_agent_calls": agent_calls,
        "vlm_rate_usd": round(vlm_rate, 5), "agent_rate_usd": round(agent_rate, 5),
        "est_cost_usd": round(est, 4),
        "est_cost_usd_high": round(est * 1.4, 4),   # ±40%:镜头数只能按时长猜
        "est_cost_cny": round(est * _CNY, 2),
        "est_cost_cny_high": round(est * 1.4 * _CNY, 2),
        "cny_rate": _CNY,
    }


@app.get("/api/project/recent")
def project_recent(limit: int = 10):
    """Most recent shot_point results on disk — works regardless of id scheme."""
    import src.config as config
    base = os.path.join(_resolve(config.VIDEO_DATABASE_FOLDER), "Output")
    found = []
    if os.path.isdir(base):
        for proj in os.listdir(base):
            pdir = os.path.join(base, proj)
            if not os.path.isdir(pdir):
                continue
            for fn in os.listdir(pdir):
                if fn.startswith("shot_point_") and fn.endswith(".json"):
                    fp = os.path.join(pdir, fn)
                    found.append({
                        "shot_point": fp, "project": proj,
                        "instruction_id": fn[len("shot_point_"):-len(".json")],
                        "mtime": os.path.getmtime(fp),
                    })
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:limit]


# ── Render ──────────────────────────────────────────────────────────────────

class RenderRequest(BaseModel):
    shot_point: str
    video_path: str
    audio_path: str
    ratio: str = "9:16"
    add_ending: bool = False
    has_dialogue: bool = False
    transition: float = 0.0   # crossfade seconds between clips (0 = hard cuts)
    transition_mode: str = ""  # "" | "uniform" | "ai" (LLM picks per-cut transitions)
    source_quality: str = "proxy"  # "proxy" (1080p 代理, 快) | "original" (拉取/直读 4K 原片)
    color_grade: str = ""      # "" | "teal_orange" | "film" | "warm"
    letterbox: bool = False    # 2.35:1 cinematic bars inside the 16:9 frame
    fades: bool = True         # fade in from black + fade out to black w/ music
    narration: bool = True     # 混入 AI 旁白(需先生成 narration sidecar 且其 enabled=true)
    title_text: str = ""       # 片头字幕(叠在第一个镜头上)
    end_text: str = ""         # 片尾字幕(随最后一个镜头淡出浮现)


# ── AI 旁白 ─────────────────────────────────────────────────────────────────

class NarrationGenRequest(BaseModel):
    shot_point: str
    voice: str = "yunxi"
    instruction: str = ""


class NarrationSaveRequest(BaseModel):
    shot_point: str
    voice: str = "yunxi"
    enabled: bool = True
    lines: list[dict] = []


@app.get("/api/narration")
def narration_get(shot_point: str):
    from src.narration import narration_paths, VOICES
    npath, _ = narration_paths(_resolve(shot_point))
    if not os.path.exists(npath):
        return {"exists": False, "voices": list(VOICES.keys())}
    try:
        with open(npath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"narration.json 损坏: {e}")
    return {"exists": True, "voices": list(VOICES.keys()), **data}


@app.post("/api/narration/generate")
def narration_generate(body: NarrationGenRequest):
    """LLM 写稿(1 次 agent 调用)+ edge-tts 配音(免费)。同步,约 20-60s。"""
    from src.narration import generate
    abs_point = _resolve(body.shot_point)
    if not os.path.exists(abs_point):
        raise HTTPException(404, "shot_point 不存在")
    try:
        data = generate(abs_point, voice=body.voice, instruction=body.instruction)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"旁白生成失败: {e}")
    return data


@app.post("/api/narration/save")
def narration_save(body: NarrationSaveRequest):
    """UI 编辑后保存;只有文本变化的句子会重新配音。"""
    from src.narration import save
    abs_point = _resolve(body.shot_point)
    try:
        data = save(abs_point, body.lines, body.voice, body.enabled)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"旁白保存失败: {e}")
    return data


@app.post("/api/render")
def render(body: RenderRequest):
    abs_point = _resolve(body.shot_point)
    abs_plan = abs_point.replace("shot_point_", "shot_plan_")
    if not os.path.exists(abs_point):
        raise HTTPException(400, f"shot_point not found: {abs_point}")
    # Isolate the render per shot_point (≈ per project/instruction). The project
    # DIR is keyed only by primary-video-hash + audio, so different instructions
    # share it; without a per-instruction tag every project overwrote the same
    # output_{ratio}.mp4 and saw each other's renders. Tag = hash of shot_point name.
    _sp_tag = hashlib.md5(os.path.basename(abs_point).encode("utf-8")).hexdigest()[:8]
    out = os.path.join(os.path.dirname(abs_point), f"output_{body.ratio.replace(':', 'x')}_{_sp_tag}.mp4")
    # 同名成片不覆盖丢历史:上一版按其渲染时刻改名归档(连同 .render.json),
    # 渲染页「历史版本」可回看/对比(比如 无调色 vs teal_orange)。
    if os.path.exists(out):
        _stamp = time.strftime("%m%d_%H%M%S", time.localtime(os.path.getmtime(out)))
        _keep = os.path.splitext(out)[0] + f"_v{_stamp}.mp4"
        try:
            if not os.path.exists(_keep):
                os.replace(out, _keep)
                _side = os.path.splitext(out)[0] + ".render.json"
                if os.path.exists(_side):
                    os.replace(_side, os.path.splitext(_keep)[0] + ".render.json")
        except OSError:
            pass  # 被播放器占用等 → 维持旧行为(覆盖),不阻塞渲染
    ending = os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4")
    font = os.path.join(PROJECT_ROOT, "resource", "font", "Pulp Fiction Italic M54.ttf")

    # --video is only a fallback in multi-source mode (clips carry their own
    # video_path). Never pass a non-existent path (e.g. a planned-but-never-
    # created merged file) — resolve a real source from the shot_point instead.
    vid = _resolve(body.video_path)
    if not vid or not os.path.exists(vid):
        try:
            with open(abs_point, "r", encoding="utf-8") as f:
                _data = json.load(f)
            for _r in (_data if isinstance(_data, list) else []):
                for _c in ([_r] + (_r.get("clips") or [])):
                    vp = _c.get("video_path") or ""
                    if vp and os.path.exists(vp):
                        vid = vp
                        break
                if vid and os.path.exists(vid):
                    break
        except Exception:
            pass
    if not vid or not os.path.exists(vid):
        raise HTTPException(400, "找不到任何存在的源视频（项目视频与 shot_point 中的 clip 路径均无效）")

    # 4K option: swap Immich proxy paths for originals in a sibling json.
    # Output filename/tag stays keyed to the ORIGINAL shot_point name.
    shot_json_for_render = abs_point
    _orig_notes: list = []
    if body.source_quality == "original":
        try:
            shot_json_for_render, _orig_notes = _materialize_immich_originals(abs_point)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"原片替换失败: {e}")

    cmd = [
        sys.executable, "render/render_video.py",
        "--shot-plan", abs_plan, "--shot-json", shot_json_for_render,
        "--video", vid, "--audio", _resolve(body.audio_path),
        "--output", out, "--crop-ratio", body.ratio, "--no-labels",
    ]
    if body.has_dialogue:
        cmd += ["--render-hook-dialogue"]
    if body.transition_mode == "ai":
        cmd += ["--transition-mode", "ai"]
    elif body.transition and body.transition > 0:
        cmd += ["--transition", str(body.transition)]
    if body.color_grade in ("teal_orange", "film", "warm"):
        cmd += ["--color-grade", body.color_grade]
    if body.letterbox and body.ratio == "16:9":
        cmd += ["--letterbox"]
    if body.fades:
        cmd += ["--fades"]
    if body.title_text.strip():
        cmd += ["--title-text", body.title_text.strip()]
    if body.end_text.strip():
        cmd += ["--end-text", body.end_text.strip()]
    if body.add_ending and os.path.exists(ending):
        cmd += ["--ending-video", ending]
    if os.path.exists(font):
        cmd += ["--dialogue-font", font]
    # AI 旁白:sidecar 存在且启用时自动混入(narration=False 可强制关)
    if body.narration:
        from src.narration import narration_paths
        _npath, _ = narration_paths(abs_point)
        if os.path.exists(_npath):
            try:
                _nd = json.load(open(_npath, encoding="utf-8"))
                if _nd.get("enabled") and _nd.get("lines"):
                    cmd += ["--narration", _npath]
            except Exception:  # noqa: BLE001
                pass
    job = Job("render")
    job.meta.update({"output": out, "ratio": body.ratio,
                     "source_quality": body.source_quality})
    for _n in _orig_notes:
        job.add(f"[原片] {_n}")
    try:
        _spawn(job, cmd)
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"job_id": job.id, "output": out}


class ShotReplaceRequest(BaseModel):
    shot_point: str
    section_idx: int
    shot_idx: int
    reason: str = ""     # user's words: 太晃 / 和上一个重复 / 太暗 …


@app.post("/api/shots/replace")
def replace_shot(body: ShotReplaceRequest):
    """Swap ONE shot the user dislikes for the best unused highlight-pool
    moment. The rejected range goes into the project's rejections.json —
    a persistent taste memory future picks must avoid too."""
    from src.utils.time_format_convert import hhmmss_to_seconds as _ts
    from src.utils.media_utils import seconds_to_hhmmss as _hh
    import src.config as _cfg
    abs_point = _resolve(body.shot_point)
    if not os.path.exists(abs_point):
        raise HTTPException(404, "shot_point 不存在")
    with open(abs_point, "r", encoding="utf-8") as f:
        shots = json.load(f)
    tgt = next((s for s in shots if s.get("section_idx") == body.section_idx
                and s.get("shot_idx") == body.shot_idx), None)
    if not tgt:
        raise HTTPException(404, "找不到该镜头")
    proj = os.path.dirname(abs_point)
    try:
        with open(os.path.join(proj, "highlight_pool.json"), "r", encoding="utf-8") as f:
            pool = json.load(f).get("moments", [])
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "该项目没有高光池(旧流水线产物)— 重跑一次流水线后可用")

    old = tgt["clips"][0]
    old_src, old_s, old_e = old.get("video_path", ""), _ts(old["start"]), _ts(old["end"])
    # GLOBAL taste memory — rejections apply across all projects; the pool
    # builder turns them into score penalties, "不再使用" wording = permanent ban
    from src.curation import REJECTIONS_PATH, load_rejections
    rej_path = os.path.join(PROJECT_ROOT, REJECTIONS_PATH)
    rejections = load_rejections()
    _rtxt = body.reason or ""
    _ban = _rtxt.startswith("!") or any(k in _rtxt for k in ("不再", "别再", "拉黑", "永久", "never"))
    rejections.append({"video_path": old_src, "start": old_s, "end": old_e,
                       "reason": body.reason, "ban": _ban,
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    gap = float(getattr(_cfg, "SHOT_MIN_GAP_SEC", 2.0) or 2.0)
    need = float(tgt.get("target_duration") or (old_e - old_s) or 3.0)

    def _norm(p):
        return os.path.normcase(os.path.normpath(p or ""))

    forbidden = [(r["video_path"], float(r["start"]), float(r["end"])) for r in rejections]
    for s in shots:
        if s is tgt:
            continue
        for c in s.get("clips", []):
            forbidden.append((c.get("video_path", ""), _ts(c["start"]), _ts(c["end"])))

    _reason = body.reason or ""
    _want_steady = any(k in _reason for k in ("晃", "抖", "快", "晕", "歪"))
    _hate_similar = any(k in _reason for k in ("重复", "一样", "相似", "雷同"))

    pick = None
    for m in sorted(pool, key=lambda x: -x.get("score", 0)):
        if float(m.get("duration", 0)) < need - 1.0:
            continue
        if _want_steady and 0 <= float(m.get("stability", -1)) < 7.0:
            continue
        _sim_r = 60.0 if _hate_similar else 20.0
        if _norm(m.get("video_path")) == _norm(old_src) and \
                abs(float(m["start"]) - old_s) < _sim_r:
            continue
        c0 = (float(m["start"]) + float(m["end"])) / 2.0
        w_s = max(0.0, c0 - need / 2.0)
        w_e = w_s + need
        clash = False
        for (fsrc, fs, fe) in forbidden:
            if _norm(fsrc) == _norm(m.get("video_path")) and w_s < fe + gap and w_e > fs - gap:
                clash = True
                break
        if not clash:
            pick = (m, w_s, w_e)
            break
    if not pick:
        raise HTTPException(409, "高光池里找不到不冲突的替代镜头 — 换个说法或重跑流水线扩充素材")

    m, w_s, w_e = pick
    import shutil
    shutil.copy2(abs_point, abs_point + ".bak")
    tgt["clips"] = [{"shot": 1, "start": _hh(round(w_s, 2)), "end": _hh(round(w_e, 2)),
                     "duration": round(w_e - w_s, 2), "video_path": m["video_path"]}]
    tgt["video_path"] = m["video_path"]
    tgt["total_duration"] = round(w_e - w_s, 2)
    tgt["replaced"] = True
    tgt["replace_reason"] = body.reason
    with open(abs_point, "w", encoding="utf-8") as f:
        json.dump(shots, f, ensure_ascii=False, indent=2)
    with open(rej_path, "w", encoding="utf-8") as f:
        json.dump(rejections, f, ensure_ascii=False, indent=2)
    return {"ok": True, "new_clip": tgt["clips"][0], "banned": _ban,
            "moment": {"id": m.get("id"), "score": m.get("score"), "desc": m.get("desc", "")[:120]}}


@app.get("/api/bgm/recipe")
def bgm_recipe(path: str):
    """Recipe of an AI-synthesized BGM (segments/roles/rationale from the
    .bgmmix.json sidecar) + which projects currently use it."""
    ap = _resolve(path)
    sc = os.path.splitext(ap)[0] + ".bgmmix.json"
    recipe = None
    if os.path.exists(sc):
        try:
            with open(sc, "r", encoding="utf-8") as f:
                recipe = json.load(f)
        except Exception:  # noqa: BLE001
            pass
    used = []
    try:
        import glob as _g
        for pj in _g.glob(os.path.join(PROJECTS_DIR, "*", "project.json")):
            try:
                with open(pj, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                pa = _resolve(str(pdata.get("audio") or ""))
                if pa and os.path.normcase(os.path.normpath(pa)) == os.path.normcase(os.path.normpath(ap)):
                    used.append(pdata.get("name") or os.path.basename(os.path.dirname(pj)))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return {"recipe": recipe, "used_in": used}


class ShotLikeRequest(BaseModel):
    shot_point: str
    section_idx: int
    shot_idx: int
    reason: str = ""


def _analyze_like(reason: str, desc: str, motion: dict) -> dict | None:
    """Distill the user's praise into reusable editing principles (LLM).

    The point is UNDERSTANDING, not storage: '很稳的平移,3-5秒,像专业旅拍'
    becomes concrete selection rules the Screenwriter can follow forever."""
    import litellm
    from src import config as _cfg
    _mo = ""
    if motion:
        _mo = (f"{motion.get('type', '?')} (dx={motion.get('dx')}, "
               f"dy={motion.get('dy')}, zoom={motion.get('zoom')})")
    prompt = (
        "你是旅拍混剪的剪辑助手。用户点赞了成片中的一个镜头。请把这次点赞提炼成"
        "可复用的剪辑偏好原则,供未来自动选材/编排时遵循。\n\n"
        f"镜头画面描述: {desc or '(无)'}\n"
        f"实测相机运动: {_mo or '(未测)'}\n"
        f"用户给出的理由: {reason or '(未给出——请从镜头特征推断用户可能欣赏的点)'}\n\n"
        '只输出一个 JSON 对象,不要其他内容: {"summary": "一句话总结这条偏好", '
        '"principles": ["可执行的选材/编排原则,最多4条"], '
        '"applies_to": "适用范围,如: 航拍 / 手持 / 所有镜头"}'
    )
    candidates = [
        (cfg("AGENT_LITELLM_MODEL", ""), cfg("AGENT_LITELLM_URL", ""), cfg("AGENT_LITELLM_API_KEY", "")),
        (getattr(_cfg, "TRANSLATE_MODEL", ""), getattr(_cfg, "TRANSLATE_ENDPOINT", ""),
         getattr(_cfg, "TRANSLATE_API_KEY", "")),
    ]
    for model, base, key in candidates:
        if not model:
            continue
        try:
            kwargs = dict(model=model, messages=[{"role": "user", "content": prompt}],
                          temperature=0.3, max_tokens=2000, timeout=60)
            if base:
                kwargs["api_base"] = base
            if key:
                kwargs["api_key"] = key
            r = litellm.completion(**kwargs)
            raw = (r.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            parsed = json.loads(m.group(0) if m else raw)
            if isinstance(parsed, dict) and parsed.get("principles"):
                return parsed
        except Exception as e:  # noqa: BLE001
            print(f"[like] analysis via {model} failed: {str(e)[:150]}")
    return None


@app.post("/api/shots/like")
def like_shot(body: ShotLikeRequest):
    """Positive taste memory: record WHY the user liked a shot.

    The liked range gets a global score bonus in every future pool build
    (curation.load_likes), and the LLM-distilled principles ride into the
    Screenwriter prompt as a learned taste profile."""
    from src.utils.time_format_convert import hhmmss_to_seconds as _ts
    abs_point = _resolve(body.shot_point)
    if not os.path.exists(abs_point):
        raise HTTPException(404, "shot_point 不存在")
    with open(abs_point, "r", encoding="utf-8") as f:
        shots = json.load(f)
    tgt = next((s for s in shots if s.get("section_idx") == body.section_idx
                and s.get("shot_idx") == body.shot_idx), None)
    if not tgt:
        raise HTTPException(404, "找不到该镜头")
    clip = tgt["clips"][0]
    src, c_s, c_e = clip.get("video_path", ""), _ts(clip["start"]), _ts(clip["end"])

    # context from the pool moment this clip came from (desc + measured motion)
    desc, motion = "", {}
    try:
        with open(os.path.join(os.path.dirname(abs_point), "highlight_pool.json"),
                  "r", encoding="utf-8") as f:
            for m in json.load(f).get("moments", []):
                if os.path.normcase(os.path.normpath(m.get("video_path", ""))) == \
                        os.path.normcase(os.path.normpath(src)) \
                        and min(c_e, float(m.get("end", 0))) - max(c_s, float(m.get("start", 0))) > 0.5:
                    desc, motion = m.get("desc", ""), m.get("motion") or {}
                    break
    except Exception:  # noqa: BLE001
        pass

    analysis = _analyze_like(body.reason, desc, motion)

    from src.curation import LIKES_PATH, load_likes
    likes = load_likes()
    likes.append({"video_path": src, "start": c_s, "end": c_e,
                  "reason": body.reason, "analysis": analysis,
                  "desc": desc[:200], "motion": motion,
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    likes_path = os.path.join(PROJECT_ROOT, LIKES_PATH)
    os.makedirs(os.path.dirname(likes_path), exist_ok=True)
    with open(likes_path, "w", encoding="utf-8") as f:
        json.dump(likes, f, ensure_ascii=False, indent=2)

    tgt["liked"] = True
    if body.reason:
        tgt["like_reason"] = body.reason
    with open(abs_point, "w", encoding="utf-8") as f:
        json.dump(shots, f, ensure_ascii=False, indent=2)
    return {"ok": True, "analysis": analysis}


def _ts_to_sec(v) -> float:
    """Accept '00:00:13.2' | '13.2' | 13.2 → seconds."""
    if v is None:
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        if ":" in s:
            parts = [float(x) for x in s.split(":")]
            mult = [1, 60, 3600]
            return sum(p * m for p, m in zip(reversed(parts), mult))
        return float(s)
    except (ValueError, TypeError):
        return float("nan")


# ── UI translation (English analysis text → Chinese), disk-cached ──────────
# Each unique string is billed exactly once (cheap flash model), then served
# from Output/cache/translations_zh.json forever.
_TRANSLATE_PATH = os.path.join(PROJECT_ROOT, "Output", "cache", "translations_zh.json")
_TRANSLATE_LOCK = threading.Lock()


class TranslateRequest(BaseModel):
    texts: list[str] = []


def _tr_load() -> dict:
    try:
        with open(_TRANSLATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _tr_key(t: str) -> str:
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


@app.post("/api/translate")
def translate_texts(body: TranslateRequest):
    texts = [t for t in (body.texts or []) if isinstance(t, str) and t.strip()]
    if not texts:
        return {"translations": []}
    from src import config as _cfg
    with _TRANSLATE_LOCK:
        cache = _tr_load()
    out: dict[str, str] = {}
    todo: list[str] = []
    for t in texts:
        k = _tr_key(t)
        if k in cache:
            out[t] = cache[k]
        elif t not in out and t not in todo:
            todo.append(t)

    if todo:
        import litellm
        new_entries: dict[str, str] = {}
        CHUNK = 25
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i:i + CHUNK]
            prompt = (
                "把下面 JSON 数组中的每段英文视频/音频描述翻译成简洁自然的中文。"
                "保留时间戳标记（如 [47-51s]、00:01:20）原样不动。"
                "只输出一个 JSON 数组，元素与输入一一对应，不要输出任何其他内容。\n\n"
                + json.dumps(chunk, ensure_ascii=False)
            )
            try:
                r = litellm.completion(
                    model=getattr(_cfg, "TRANSLATE_MODEL", "deepseek/deepseek-v4-flash"),
                    api_base=getattr(_cfg, "TRANSLATE_ENDPOINT", "https://api.deepseek.com/v1"),
                    api_key=getattr(_cfg, "TRANSLATE_API_KEY", ""),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2, max_tokens=4000, timeout=90,
                )
                raw = (r.choices[0].message.content or "").strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
                arr = json.loads(raw)
                if isinstance(arr, list) and len(arr) == len(chunk):
                    for src, zh in zip(chunk, arr):
                        if isinstance(zh, str) and zh.strip():
                            out[src] = zh.strip()
                            new_entries[_tr_key(src)] = zh.strip()
            except Exception as e:  # noqa: BLE001
                print(f"[translate] chunk failed: {str(e)[:200]}")
        if new_entries:
            with _TRANSLATE_LOCK:
                cache = _tr_load()
                cache.update(new_entries)
                os.makedirs(os.path.dirname(_TRANSLATE_PATH), exist_ok=True)
                with open(_TRANSLATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cache, f, ensure_ascii=False)

    return {"translations": [out.get(t, "") for t in texts]}


class BgmStitchRequest(BaseModel):
    tracks: list  # [{"path": str, "start": float?, "end": float?}] in play order
    crossfade: float = 0.0   # seconds; 0 = auto (2 bars of the outgoing track)
    name: str = ""
    mode: str = "manual"     # "manual" | "ai" (AI picks the best sections per track)
    target_sec: float = 180.0  # ai mode: desired total duration
    video_paths: list = []   # ai mode: the footage this mix must serve (arc matching)


@app.post("/api/bgm/stitch")
def bgm_stitch(body: BgmStitchRequest):
    """Standalone BGM stitcher — no pipeline run needed. The result lands in
    the asset imports dir as a normal audio file: scan it and it's a BGM.

    mode=ai: the LLM arranges MEASURED sections of each track into an energy
    arc (open→build→peak→resolve); all timestamps are signal-derived, joins
    snap to bar lines, deterministic fallback if the LLM misbehaves."""
    from src.audio.bgm_stitch import stitch_bgm, plan_bgm_mix
    from src.analyzer import _ensure_ffmpeg_on_path
    _ensure_ffmpeg_on_path()
    tracks = [{"path": _resolve(str(t.get("path", ""))),
               "start": t.get("start"), "end": t.get("end")}
              for t in (body.tracks or []) if t.get("path")]
    if len(tracks) < 2:
        raise HTTPException(400, "请选择至少两首音乐(按播放顺序)")

    plan_why, plan_view = "", []
    if body.mode == "ai":
        try:
            from src.audio.bgm_stitch import footage_brief
            _brief = footage_brief([_resolve(str(v)) for v in (body.video_paths or []) if v])
            picks, plan_why = plan_bgm_mix([t["path"] for t in tracks],
                                           target_sec=float(body.target_sec or 180.0),
                                           brief=_brief)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"AI 编排失败: {e}")
        tracks = [{"path": p["path"], "start": p.get("start"), "end": p.get("end")}
                  for p in picks]
        plan_view = [{"track": p.get("track_name", os.path.basename(p["path"])),
                      "start": p.get("start"), "end": p.get("end"),
                      "role": p.get("role", "")} for p in picks]

    root = cfg("ASSET_ROOT_DIR", "resource/imports")
    # AI products live in their own workspace subfolder, apart from raw footage
    out_dir = os.path.join(_resolve(root), "products")
    os.makedirs(out_dir, exist_ok=True)
    base = (body.name or "").strip() or ("BGMmix_" + time.strftime("%m%d_%H%M%S"))
    base = re.sub(r'[\\/:*?"<>|]', "_", base)
    out_path = os.path.join(out_dir, base + ".mp3")
    try:
        meta = stitch_bgm(tracks, out_path, crossfade=body.crossfade)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"拼接失败: {e}")
    # enrich the recipe sidecar with the ARRANGEMENT so the asset detail page
    # can explain the mix (which segments, what roles, the AI's rationale)
    if plan_view or plan_why:
        try:
            _sc = os.path.splitext(out_path)[0] + ".bgmmix.json"
            with open(_sc, "r", encoding="utf-8") as f:
                _d = json.load(f)
            _d.update({"plan": plan_view, "why": plan_why, "mode": body.mode,
                       "target_sec": float(body.target_sec or 0)})
            with open(_sc, "w", encoding="utf-8") as f:
                json.dump(_d, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
    rel = os.path.relpath(out_path, PROJECT_ROOT).replace("\\", "/")
    return {"ok": True, "path": rel, "meta": meta, "plan": plan_view, "why": plan_why}


# ── Asset hearts: asset-level "我喜欢这个素材/这首歌" (global, by hash) ────
_HEARTS_PATH = os.path.join(PROJECT_ROOT, "Output", "asset_index", "asset_hearts.json")


def _hearts_load() -> dict:
    try:
        with open(_HEARTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


class HeartRequest(BaseModel):
    content_hash: str
    hearted: bool = True


@app.get("/api/assets/hearts")
def assets_hearts():
    return {"hearts": sorted(h for h, v in _hearts_load().items() if v.get("hearted"))}


def _hearts_save(hearts: dict) -> None:
    os.makedirs(os.path.dirname(_HEARTS_PATH), exist_ok=True)
    with open(_HEARTS_PATH, "w", encoding="utf-8") as f:
        json.dump(hearts, f, ensure_ascii=False, indent=2)


def _hash_to_immich_id(content_hash: str) -> str | None:
    """content hash → workspace file → Immich asset id (via immich_map)."""
    try:
        from src.asset_manager.index_store import load_index
        ann = load_index().get(content_hash)
        fn = os.path.basename(getattr(ann.metadata, "absolute_path", "") or "") if ann else ""
        if fn:
            return (_load_immich_map().get(fn) or {}).get("id")
    except Exception:  # noqa: BLE001
        pass
    return None


@app.post("/api/assets/heart")
def asset_heart(body: HeartRequest):
    """Toggle the ❤️ on an asset. Hearted videos get a modest pool bonus and
    a +1 source quota; hearted music is preferred by the BGM planner.
    Linked assets push the heart to Immich as a favorite (one heart universe:
    the user's natural gesture is ❤️, in either app)."""
    if not body.content_hash:
        raise HTTPException(400, "content_hash required")
    hearts = _hearts_load()
    hearts[body.content_hash] = {"hearted": bool(body.hearted), "source": "user",
                                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _hearts_save(hearts)
    synced = False
    aid = _hash_to_immich_id(body.content_hash)
    if aid:
        try:
            _immich_req(f"/assets/{aid}", "PUT", {"isFavorite": bool(body.hearted)})
            synced = True
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "hearted": bool(body.hearted), "immich_synced": synced}


@app.post("/api/immich/sync_hearts")
def immich_sync_hearts():
    """Pull Immich FAVORITES into CutClaw hearts — the user's natural taste
    gesture is ❤️ while browsing memories on the phone, not rating stars.
    Additive for new favorites; mirrors un-favorites ONLY for hearts that
    came from Immich (source=immich), never touching manual CutClaw hearts
    (audio tracks etc. live only here)."""
    fav_ids = set()
    page = 1
    while page:
        r = _immich_req("/search/metadata", "POST",
                        {"isFavorite": True, "type": "VIDEO", "size": 200, "page": page})
        a = r.get("assets") or {}
        for it in a.get("items", []):
            if it.get("id"):
                fav_ids.add(it["id"])
        page = a.get("nextPage")

    # linked workspace files: immich id ↔ basename ↔ content hash
    imap = _load_immich_map()
    from src.asset_manager.index_store import load_index
    hash_by_name = {}
    for h, ann in load_index().items():
        fn = os.path.basename(getattr(ann.metadata, "absolute_path", "") or "")
        if fn:
            hash_by_name[fn] = h

    hearts = _hearts_load()
    added = removed = 0
    for fname, entry in imap.items():
        aid = entry.get("id")
        h = hash_by_name.get(fname)
        if not aid or not h:
            continue
        cur = hearts.get(h) or {}
        if aid in fav_ids:
            if not cur.get("hearted"):
                hearts[h] = {"hearted": True, "source": "immich",
                             "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
                added += 1
        elif cur.get("hearted") and cur.get("source") == "immich":
            hearts[h] = {"hearted": False, "source": "immich",
                         "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
            removed += 1
    if added or removed:
        _hearts_save(hearts)
    return {"favorites": len(fav_ids), "linked_added": added, "linked_removed": removed}


@app.get("/api/render/beats")
def render_beats(path: str, start: float = 0.0, duration: float = 0.0):
    """Measured music keypoints (madmom) mapped into a render's music window —
    the beat grid the pacing engine cuts on, for timeline visualization.
    (Distinct from /api/audio/keypoints, which returns the raw per-method
    dict for the asset-detail drawer.)"""
    ap = _resolve(path)
    if not os.path.exists(ap):
        return {"beats": []}
    try:
        from src.asset_manager.scanner import compute_content_hash
        from src.analyzer import get_analysis_path
        h = compute_content_hash(ap)
        with open(os.path.join(PROJECT_ROOT, get_analysis_path(h), "captions.json"),
                  "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return {"beats": []}
    end = start + duration if duration and duration > 0 else float("inf")
    beats = []
    for k in (d.get("_keypoints_detail") or []):
        try:
            t = float(k.get("time", -1))
        except (TypeError, ValueError):
            continue
        if start <= t <= end:
            beats.append({"t": round(t - start, 2), "type": str(k.get("type", "")),
                          "w": round(float(k.get("normalized_intensity", 0) or 0), 2)})
    return {"beats": beats, "bar_sec": (d.get("facts") or {}).get("bar_sec")}


@app.get("/api/render/clip_map")
def render_clip_map(shot_point: str):
    """Map the finished-video timeline → each source clip + the AI description
    that drove its selection. Joins shot_point.json (coordinates the renderer
    actually uses) with shot_plan.json (Screenwriter's per-shot intent)."""
    abs_point = _resolve(shot_point)
    if not os.path.exists(abs_point):
        return {"clips": [], "total": 0.0, "error": "shot_point not found"}
    try:
        entries = json.load(open(abs_point, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"clips": [], "total": 0.0, "error": f"parse: {e}"}
    if not isinstance(entries, list):
        entries = entries.get("shots") or entries.get("clips") or []

    # sibling shot_plan.json (same instruction id) → descriptions
    plan_path = abs_point.replace("shot_point_", "shot_plan_")
    plan_shots = {}  # (section_idx, shot_idx) -> desc dict
    try:
        plan = json.load(open(plan_path, encoding="utf-8"))
        for si, sec in enumerate(plan.get("video_structure", []) or []):
            for shi, shot in enumerate((sec.get("shot_plan") or {}).get("shots", []) or []):
                plan_shots[(si, shi)] = shot
    except Exception:  # noqa: BLE001
        pass

    # basename → dense_segments (what the VLM actually saw, from the analysis cache).
    # This is the ground truth the editor selected against; comparing it to the
    # Screenwriter's intent shows whether a clip's pick actually matches its slot.
    dense_by_video: dict = {}
    analyzed_root = os.path.join(PROJECT_ROOT, "Output", "analyzed")
    for md in glob.glob(os.path.join(analyzed_root, "*", "metadata.json")):
        try:
            meta = json.load(open(md, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        name = meta.get("file_name")
        if not name:
            continue
        ck = os.path.join(os.path.dirname(md), "captions", "ckpt")
        if not os.path.isdir(ck):
            continue
        segs: dict = {}  # (s,e) -> content, dedup across ckpt files
        for f in os.listdir(ck):
            if not f.endswith(".json"):
                continue
            try:
                dd = json.load(open(os.path.join(ck, f), encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            for sg in dd.get("dense_segments") or []:
                s, e = sg.get("start_sec_abs"), sg.get("end_sec_abs")
                if s is None or e is None:
                    continue
                segs[(round(float(s), 1), round(float(e), 1))] = sg.get("content_description", "")
        if segs:
            dense_by_video[name] = sorted(
                ([s, e, txt] for (s, e), txt in segs.items()), key=lambda x: x[0])

    def _analysis_at(video_name: str, a: float, b: float) -> str:
        """VLM descriptions of segments overlapping [a,b] in the source video."""
        segs = dense_by_video.get(video_name)
        if not segs or a != a or b != b:  # NaN guard
            return ""
        parts = []
        for s, e, txt in segs:
            if e > a and s < b and txt:  # overlap
                parts.append(f"[{s:g}-{e:g}s] {txt}")
        return "  ".join(parts)

    out, cursor = [], 0.0
    for e in entries:
        if not isinstance(e, dict):
            continue
        sec_i = e.get("section_idx", 0)
        shot_i = e.get("shot_idx", 0)
        desc = plan_shots.get((sec_i, shot_i), {})
        for c in e.get("clips", []) or []:
            src_s = _ts_to_sec(c.get("start"))
            src_e = _ts_to_sec(c.get("end"))
            dur = c.get("duration")
            dur = float(dur) if isinstance(dur, (int, float)) else (src_e - src_s)
            if not (dur > 0):
                continue
            out.append({
                "out_start": round(cursor, 2),
                "out_end": round(cursor + dur, 2),
                "duration": round(dur, 2),
                "video": os.path.basename(str(c.get("video_path") or e.get("video_path") or "")),
                "src_start": round(src_s, 2) if src_s == src_s else None,
                "src_end": round(src_e, 2) if src_e == src_e else None,
                "section_idx": sec_i,
                "shot_idx": shot_i,
                "content": desc.get("content", ""),
                "visuals": desc.get("visuals", ""),
                "emotion": desc.get("emotion", ""),
                "visual_beat": desc.get("visual_beat", ""),
                # ground truth: what the VLM actually saw at this source range
                "analysis": _analysis_at(
                    os.path.basename(str(c.get("video_path") or e.get("video_path") or "")),
                    src_s, src_e),
            })
            cursor += dur
    return {"clips": out, "total": round(cursor, 2)}


_DUR_CACHE: dict = {}


def _media_duration(path: str) -> float:
    """Container duration in seconds (ffprobe), cached by (path, mtime)."""
    try:
        key = (path, os.path.getmtime(path))
        if key in _DUR_CACHE:
            return _DUR_CACHE[key]
        fp = os.path.join(PROJECT_ROOT, "tools", "ffmpeg", "ffprobe.exe")
        if not os.path.exists(fp):
            fp = "ffprobe"
        r = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=20)
        dur = round(float(r.stdout.strip()), 2)
        _DUR_CACHE[key] = dur
        return dur
    except Exception:  # noqa: BLE001
        return 0.0


@app.get("/api/render/outputs")
def render_outputs(shot_point: str):
    abs_point = _resolve(shot_point)
    d = os.path.dirname(abs_point)
    _sp_tag = hashlib.md5(os.path.basename(abs_point).encode("utf-8")).hexdigest()[:8]
    out = []
    for ratio in ("9:16", "16:9", "1:1"):
        p = os.path.join(d, f"output_{ratio.replace(':', 'x')}_{_sp_tag}.mp4")
        if os.path.exists(p):
            entry = {"ratio": ratio, "path": p, "size_mb": round(os.path.getsize(p) / 1e6, 1),
                     "mtime": os.path.getmtime(p)}
            # sidecar from the renderer: per-cut transitions + music window
            mp = os.path.splitext(p)[0] + ".render.json"
            if os.path.exists(mp):
                try:
                    with open(mp, "r", encoding="utf-8") as f:
                        rm = json.load(f)
                    ap = (rm.get("audio") or {}).get("path") or ""
                    if ap and os.path.exists(ap):
                        rm["audio"]["total"] = _media_duration(ap)
                        rm["audio"]["name"] = os.path.basename(ap)
                    entry["render_meta"] = rm
                except Exception:  # noqa: BLE001
                    pass
            out.append(entry)
    # 被新渲染顶替的旧版(见 /api/render 的归档逻辑)
    history = []
    try:
        import glob as _glob
        for p in _glob.glob(os.path.join(d, f"output_*_{_sp_tag}_v*.mp4")):
            _m = re.search(r"output_(\w+)_%s_v(\d+_\d+)\.mp4$" % _sp_tag, os.path.basename(p))
            history.append({
                "ratio": (_m.group(1).replace("x", ":") if _m else "?"),
                "version": (_m.group(2) if _m else ""),
                "path": p, "size_mb": round(os.path.getsize(p) / 1e6, 1),
                "mtime": os.path.getmtime(p),
            })
        history.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception:  # noqa: BLE001
        pass
    return {"outputs": out, "history": history,
            "shot_point_exists": os.path.exists(abs_point),
            "has_ending_video": os.path.exists(os.path.join(PROJECT_ROOT, "resource", "ending", "ending.mp4"))}


# ── Static frontend (web/dist) ──────────────────────────────────────────────

DIST = os.path.join(PROJECT_ROOT, "web", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    # keep ffmpeg discoverable, same trick as local_run.py
    tools_ffmpeg = os.path.join(PROJECT_ROOT, "tools", "ffmpeg")
    if os.path.isdir(tools_ffmpeg):
        os.environ["PATH"] = tools_ffmpeg + os.pathsep + os.environ.get("PATH", "")
    if "--reload" in sys.argv:
        # dev mode: auto-restart on backend code changes
        # (in-memory jobs are lost on reload — don't use mid-pipeline)
        uvicorn.run("server.main:app", host="127.0.0.1", port=8765,
                    reload=True, reload_dirs=[os.path.join(PROJECT_ROOT, "server")])
    else:
        uvicorn.run(app, host="127.0.0.1", port=8765)
