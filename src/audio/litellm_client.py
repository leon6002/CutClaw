"""
Lightweight litellm wrapper for audio analysis via cloud API.

Reads configuration from src/audio/.env:
  AUDIO_MODEL    - LiteLLM model string (e.g. openai/Qwen3-Omni-30B-A3B-Instruct)
  AUDIO_API_KEY  - API key (use EMPTY for no auth)
  AUDIO_BASE_URL - Base URL for OpenAI-compatible endpoints
"""

import os
import asyncio
import base64
import subprocess
import tempfile
from pathlib import Path
from typing import List

import litellm
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

try:
    from .. import config as project_config
except Exception:
    project_config = None

# Load .env from the same directory as this file
load_dotenv(Path(__file__).parent / ".env")


def _get_setting(config_key: str, env_key: str, default=None):
    """Read setting from src/config.py first, then fallback to environment/.env."""
    if project_config is not None and hasattr(project_config, config_key):
        value = getattr(project_config, config_key)
        if value is not None and value != "":
            return value
    env_value = os.getenv(env_key)
    if env_value is not None and env_value != "":
        return env_value
    return default


AUDIO_MODEL = _get_setting(
    config_key="AUDIO_LITELLM_MODEL",
    env_key="AUDIO_MODEL",
    default="openai/Qwen3-Omni-30B-A3B-Instruct",
)
AUDIO_API_KEY = _get_setting(
    config_key="AUDIO_LITELLM_API_KEY",
    env_key="AUDIO_API_KEY",
    default="EMPTY",
) or "EMPTY"
AUDIO_BASE_URL = _get_setting(
    config_key="AUDIO_LITELLM_BASE_URL",
    env_key="AUDIO_BASE_URL",
    default=None,
)


def _audio_to_base64_mp3(audio_path: str) -> str:
    """Convert audio file to base64-encoded MP3 for cloud API submission."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ac", "1", "-ar", "16000", "-ab", "32k", tmp_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(tmp_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _build_messages(prompt: str, audio_b64: str) -> list:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:audio/mp3;base64,{audio_b64}"}},
        ],
    }]


@retry(
    reraise=True,
    stop=stop_after_attempt(4),  # 1 normal + up to 3 retries
    wait=wait_exponential(multiplier=2, min=1, max=30),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda rs: print(
        f"[litellm_client] Retry {rs.attempt_number}/3 "
        f"after {rs.outcome.exception()} — sleeping {rs.next_action.sleep:.1f}s",
        flush=True,
    ),
)
def _call_audio_api_sync(
    audio_path: str,
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_tokens: int = 4096,
) -> str:
    """Sync API call — safe to run from ThreadPoolExecutor workers.

    Uses litellm.completion (sync) instead of acompletion: repeated
    asyncio.run() calls create/destroy event loops, which breaks litellm's
    async logging worker under concurrency ("coroutine never awaited"
    warnings and stalls). The sync path has no such issue.
    """
    audio_b64 = _audio_to_base64_mp3(audio_path)
    response = litellm.completion(
        model=AUDIO_MODEL,
        messages=_build_messages(prompt, audio_b64),
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout=300,
        api_key=AUDIO_API_KEY,
        **({"api_base": AUDIO_BASE_URL} if AUDIO_BASE_URL else {}),
    )
    return response.choices[0].message.content


def call_audio_api(
    audio_path: str,
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_tokens: int = 4096,
) -> str:
    """Sync call for a single audio file (retried with exponential backoff)."""
    return _call_audio_api_sync(audio_path, prompt, temperature, top_p, max_tokens)


def call_audio_api_batch(
    audio_paths: List[str],
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_tokens: int = 4096,
    max_workers: int = 5,
) -> List[str]:
    """
    Concurrent batch captioning via ThreadPoolExecutor + sync litellm calls.

    Prints per-completion progress with ETA so long runs are visibly alive.

    Returns:
        List of response texts (same order as audio_paths, "" on error)
    """
    if not audio_paths:
        return []

    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(audio_paths)
    results: List[str] = [""] * total
    done_count = 0
    count_lock = threading.Lock()
    t0 = time.time()

    def _worker(idx: int, path: str):
        nonlocal done_count
        ts = time.time()
        try:
            text = _call_audio_api_sync(path, prompt, temperature, top_p, max_tokens)
            ok = True
        except Exception as e:
            print(f"[litellm_client] Failed after retries: {path}: {e}", flush=True)
            text, ok = "", False
        results[idx] = text
        with count_lock:
            done_count += 1
            n = done_count
        elapsed = time.time() - t0
        avg = elapsed / n
        eta = avg * (total - n) / max(max_workers, 1)
        mark = "✓" if ok else "✗"
        print(
            f"  [{n}/{total}] {mark} segment {idx + 1} in {time.time() - ts:.1f}s"
            f" | avg {avg:.1f}s | ETA ~{eta / 60:.1f}min",
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = [pool.submit(_worker, i, p) for i, p in enumerate(audio_paths)]
        for f in as_completed(futures):
            f.result()  # propagate unexpected errors

    return results


# --------------------------------------------------------------------------- #
# Legacy async API (kept for compatibility; the sync wrappers above no longer
# use it — see _call_audio_api_sync for why).
# --------------------------------------------------------------------------- #

@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=1, max=30),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda rs: print(
        f"[litellm_client] Retry {rs.attempt_number}/3 "
        f"after {rs.outcome.exception()} — sleeping {rs.next_action.sleep:.1f}s"
    ),
)
async def acall_audio_api(
    audio_path: str,
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_tokens: int = 4096,
) -> str:
    """Async: call the cloud audio API for one file (retried with backoff)."""
    loop = asyncio.get_running_loop()
    audio_b64 = await loop.run_in_executor(None, _audio_to_base64_mp3, audio_path)
    response = await litellm.acompletion(
        model=AUDIO_MODEL,
        messages=_build_messages(prompt, audio_b64),
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout=300,
        api_key=AUDIO_API_KEY,
        **({"api_base": AUDIO_BASE_URL} if AUDIO_BASE_URL else {}),
    )
    return response.choices[0].message.content


async def acall_audio_api_batch(
    audio_paths: List[str],
    prompt: str,
    temperature: float = 0.7,
    top_p: float = 0.95,
    max_tokens: int = 4096,
    max_concurrent: int = 5,
) -> List[str]:
    """Async: concurrent captioning with a semaphore (legacy)."""
    if not audio_paths:
        return []

    sem = asyncio.Semaphore(max_concurrent)

    async def _limited(path: str) -> str:
        async with sem:
            return await acall_audio_api(path, prompt, temperature, top_p, max_tokens)

    results = await asyncio.gather(
        *[_limited(p) for p in audio_paths],
        return_exceptions=True,
    )

    processed = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"[litellm_client] Failed after retries: {audio_paths[i]}: {result}")
            processed.append("")
        else:
            processed.append(result)
    return processed
