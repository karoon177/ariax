#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AriaX Testnet Exchange v2 — ASGI entrypoint.

Designed for zero-configuration platforms (Render free tier with
"No build required"): on first boot it installs its Python
dependencies, then re-execs itself and serves the app.

Locally (or with a proper CI build) the bootstrap is skipped when
dependencies are already importable.
"""
import importlib
import os
import subprocess
import sys

REQUIREMENTS = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "pydantic>=2.7",
    "sqlalchemy>=2.0.30",
    "aiosqlite>=0.20",
    "asyncpg>=0.29",
    "httpx>=0.27",
    "websockets>=12.0",
    "cryptography>=42.0",
]

REQUIRED_MODULES = [
    "fastapi", "uvicorn", "pydantic", "sqlalchemy", "aiosqlite",
    "asyncpg", "httpx", "websockets", "cryptography",
]


def _missing() -> list[str]:
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    return missing


def _bootstrap() -> None:
    """Install missing dependencies once, then re-exec the process."""
    if os.environ.get("ARIAX_BOOTSTRAPPED") == "1":
        # Second pass and still missing -> surface a clear error.
        missing = _missing()
        if missing:
            print(f"FATAL: dependencies still missing after install: {missing}",
                  file=sys.stderr)
            raise SystemExit(1)
        return
    missing = _missing()
    if not missing:
        return
    print(f"[ariax-bootstrap] installing dependencies ({len(missing)} module"
          f" group(s) missing)...", flush=True)
    cmd = [sys.executable, "-m", "pip", "install", "--quiet",
           "--disable-pip-version-check"] + REQUIREMENTS
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        print(f"FATAL: pip install failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    env = dict(os.environ, ARIAX_BOOTSTRAPPED="1")
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)] +
              sys.argv[1:], env)


_bootstrap()

# ---- dependencies ready: serve ----------------------------------------- #
import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        workers=1,          # MUST stay 1: single in-memory matching engine
        log_level="info",
        access_log=False,
    )
