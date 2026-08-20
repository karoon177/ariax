#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AriaX Testnet Exchange v2 — ASGI entrypoint.

Kept as a thin launcher so existing Render deployments
(`python3 server.py`) keep working unchanged.
"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        workers=1,          # MUST stay 1: single in-memory matching engine
        log_level="info",
        access_log=False,
    )
