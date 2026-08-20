# -*- coding: utf-8 -*-
"""
M2/M11 (auth & rate limiting): request dependencies.

* `v5_auth`  — Bybit v5 HMAC signature verification + permission check
               + sliding-window rate limit per API key.
* `session_auth` — v1 UI sessions (Bearer token / ?token= / legacy
               X-API-Key + X-API-Secret pair, kept for old bots).
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import Request

from .. import config, security, util
from ..errors import (ApiError, E_AUTH_REQUIRED, E_IP_MISMATCH, E_KEY_INVALID,
                      E_PERMISSION, E_RATE_LIMIT, E_TIMESTAMP)
from ..state import STATE


# --------------------------------------------------------------------------- #
# Sliding-window rate limiter (per key or IP, per endpoint group)              #
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Fixed-window counters; groups: public | order | private | auth."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], list[int]] = {}

    def check(self, key: str, group: str, limit: int, window_s: float = 1.0) -> None:
        now = time.monotonic()
        bucket = self._hits.setdefault((key, group), [])
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) >= limit:
            raise ApiError(E_RATE_LIMIT,
                           f"rate limit exceeded ({limit}/{window_s:.0f}s) for {group}",
                           http_status=429)
        bucket.append(now)


LIMITS = {"public": (30, 1.0), "order": (20, 1.0), "private": (60, 1.0),
          "auth": (10, 60.0)}
RL = RateLimiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


# --------------------------------------------------------------------------- #
# Body cache (signature verification needs the raw body twice)                 #
# --------------------------------------------------------------------------- #
async def raw_body(request: Request) -> bytes:
    if not hasattr(request.state, "body_cache"):
        request.state.body_cache = await request.body()
    return request.state.body_cache


# --------------------------------------------------------------------------- #
# Bybit v5 signed-request verification                                         #
# --------------------------------------------------------------------------- #
def _find_key(api_key: str) -> Optional[dict]:
    for rec in STATE.api_keys.values():
        if rec.get("key") == api_key and not rec.get("revoked"):
            return rec
    return None


async def verify_v5_signature(request: Request, require: str = "readTrade") -> dict:
    """Full v5 auth; returns the key record. Raises ApiError on failure."""
    ip = client_ip(request)
    api_key = request.headers.get("x-bapi-api-key", "")
    ts = request.headers.get("x-bapi-timestamp", "")
    recv = request.headers.get("x-bapi-recv-window", str(config.DEFAULT_RECV_WINDOW))
    sig = request.headers.get("x-bapi-signature", "")
    if not api_key or not sig:
        raise ApiError(E_KEY_INVALID, "missing X-BAPI-* auth headers")
    rec = _find_key(api_key)
    if rec is None:
        raise ApiError(E_KEY_INVALID, "api key not found or revoked")
    try:
        ts_int = int(ts)
    except ValueError:
        raise ApiError(E_TIMESTAMP, "invalid X-BAPI-TIMESTAMP")
    recv_int = min(int(recv), config.MAX_RECV_WINDOW)
    if abs(util.now_ms() - ts_int) > recv_int:
        raise ApiError(E_TIMESTAMP,
                       "timestamp outside recvWindow (check clock sync)")
    # payload: query string for GET, raw body for others
    if request.method == "GET":
        payload = request.url.query or ""
    else:
        payload = (await request.body()).decode()
    if not security.verify_bybit_signature(rec["secret"], ts, api_key, recv,
                                           payload, sig):
        raise ApiError(E_KEY_INVALID, "signature mismatch")
    perms = rec.get("permissions", [])
    if require == "trade" and "trade" not in perms:
        raise ApiError(E_PERMISSION, "api key lacks 'trade' permission")
    if require == "readTrade" and not ({"readTrade", "trade"} & set(perms)):
        raise ApiError(E_PERMISSION, "api key lacks 'readTrade' permission")
    ips = [x.strip() for x in (rec.get("ips") or "").split(",") if x.strip()]
    if ips and ip not in ips:
        raise ApiError(E_IP_MISMATCH, "request IP not in key whitelist")
    group = "order" if require == "trade" else "private"
    limit, window = LIMITS[group]
    RL.check(api_key, group, limit, window)
    rec["last_used_ms"] = util.now_ms()
    return rec


# --------------------------------------------------------------------------- #
# v1 UI session auth                                                           #
# --------------------------------------------------------------------------- #
# Async token->uid DB resolver injected by app.main.
SESSION_DB_LOADER = None


def extract_token(request: Request) -> str:
    """Bearer token / ?token= / query string."""
    token = request.headers.get("authorization", "").replace("Bearer ", "")
    if not token:
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(str(request.url)).query).get("token", [""])[0]
    return token


def session_uid_sync(request: Request) -> Optional[int]:
    """Cache-only lookup (+ deprecated legacy plaintext API pair)."""
    token = extract_token(request)
    uid = STATE.sessions.get(token) if token else None
    if uid:
        return uid
    key = request.headers.get("X-API-Key", "")
    secret = request.headers.get("X-API-Secret", "")
    if key and secret:
        rec = _find_key(key)
        if rec and rec["secret"] == secret:
            return rec["uid"]
    return None


async def require_session(request: Request) -> int:
    """Session auth with DB fallback for post-restart tokens."""
    uid = session_uid_sync(request)
    if uid:
        return uid
    token = extract_token(request)
    if token and SESSION_DB_LOADER is not None:
        uid = await SESSION_DB_LOADER(token)
        if uid:
            STATE.sessions[token] = uid
            return uid
    raise ApiError(E_AUTH_REQUIRED, "authentication required", http_status=401)


def _raise_auth() -> int:
    raise ApiError(E_AUTH_REQUIRED, "authentication required", http_status=401)


def require_session_sync(request: Request) -> int:
    uid = session_uid_sync(request)
    if uid is None:
        raise ApiError(E_AUTH_REQUIRED, "authentication required", http_status=401)
    return uid
