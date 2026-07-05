"""Env-backed secrets for src/config.py.

API keys must never live as literals in config.py (it is tracked by git —
keys were leaked this way once already). Instead config.py declares them as
``KEY = os.getenv("KEY", "")`` and the real values live in the gitignored
project-root ``.env``.

The two regex-based config readers/writers (server/main.py and
src/ui/helpers.py) parse config.py line-by-line and cannot evaluate that
expression, so they use this module:

- ``resolve_env_expr(raw)``  — cfg(): turn the raw RHS text into the actual
  env value (returns None if the RHS is not an env expression).
- ``env_expr_name(raw)`` + ``write_env_var(name, value)`` — save_config():
  redirect a UI edit of an env-backed key into .env instead of materializing
  the literal secret back into the tracked config.py.
"""

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

_ENV_EXPR = re.compile(
    r'^os\.(?:getenv|environ\.get)\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']'
    r'(?:\s*,\s*["\']([^"\']*)["\'])?\s*\)\s*(?:#.*)?$'
)


def env_expr_name(raw: str):
    """Env var name if `raw` is an os.getenv(...) expression, else None."""
    m = _ENV_EXPR.match((raw or "").strip())
    return m.group(1) if m else None


def resolve_env_expr(raw: str):
    """Resolved value if `raw` is an os.getenv(...) expression, else None."""
    m = _ENV_EXPR.match((raw or "").strip())
    if not m:
        return None
    return os.getenv(m.group(1), m.group(2) or "")


def write_env_var(name: str, value: str):
    """Set `name` in both the current process env and the .env file."""
    os.environ[name] = value
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    pat = re.compile(rf"^\s*{re.escape(name)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
