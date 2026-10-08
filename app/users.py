# -*- coding: utf-8 -*-
"""
M1/M3 (users & wallet service): registration, login (+TOTP 2FA), sessions,
faucet policy, simulated deposit/withdraw, API-key management.

All write paths go through the ordered Persister; read paths use the
in-memory caches. Passwords never leave PBKDF2 land; API secrets are
AES-GCM encrypted at rest and returned exactly once at creation.
"""
from __future__ import annotations

import time
from typing import Optional

from . import config, security, util
from .db import Persister
from .errors import ApiError, E_PARAM
from .engine import matching
from .state import STATE

SESSION_TTL_MS = 90 * 86400 * 1000   # 90 days — logins survive long outages


# --------------------------------------------------------------------------- #
# Registration / login                                                         #
# --------------------------------------------------------------------------- #
async def register_user(db, persist: Persister, email: str, password: str,
                        name: str) -> int:
    """Canonical registration with an awaited DB insert (safe ids)."""
    email = email.strip().lower()
    if "@" not in email or len(email) > 190:
        raise ApiError(E_PARAM, "invalid email")
    if len(password) < 6:
        raise ApiError(E_PARAM, "password must be at least 6 characters")
    salt = security.new_salt()
    pw_hash = security.hash_password(password, salt)
    ts = util.now_ms()
    async with db.session() as sess:
        async with sess.begin():
            try:
                cur = await sess.execute(
                    (await _users_table()).insert().values(
                        email=email, name=name or email.split("@")[0],
                        pass_hash=pw_hash, salt=salt, created_ms=ts))
                uid = cur.inserted_primary_key[0]
            except Exception:
                raise ApiError(E_PARAM, "email already registered")
            from . import db as _db
            await sess.execute(_db.t_balances.insert().values(
                uid=uid, asset="USDT", free=config.SIGNUP_BONUS_USDT))
            await sess.execute(_db.t_ledger.insert().values(
                uid=uid, type="bonus", asset="USDT",
                amount=config.SIGNUP_BONUS_USDT,
                note="پاداش ثبت‌نام در تست‌نت", ts_ms=ts))
    acct = STATE.account(uid)
    acct.balances["USDT"] = config.SIGNUP_BONUS_USDT
    STATE.stats["users"] += 1
    return uid


async def _users_table():
    from . import db
    return db.t_users


async def authenticate(db, email: str, password: str,
                       otp: str | None = None) -> Optional[dict]:
    """Verify credentials (+TOTP when enabled); returns the user row."""
    from . import db as _db
    from sqlalchemy import select
    async with db.session() as sess:
        res = await sess.execute(
            select(_db.t_users).where(_db.t_users.c.email == email.strip().lower()))
        row = res.mappings().first()
    if not row:
        return None
    if not security.constant_eq(security.hash_password(password, row["salt"]),
                                row["pass_hash"]):
        return None
    if row["totp_enabled"]:
        if not otp or not security.totp_verify(row["totp_secret"], otp):
            raise ApiError(10013, "2FA code required or invalid", http_status=401)
    return dict(row)


def audit(kind: str, detail: str = "", ip: str = "") -> None:
    """Append a security/audit event (write-behind, never blocks trading)."""
    from . import db as _db
    from . import util as _util

    async def _write(session) -> None:
        await session.execute(_db.t_security.insert().values(
            kind=kind[:32], detail=detail[:200], ip=ip[:64],
            ts_ms=_util.now_ms()))

    from .runtime import get_persister
    p = get_persister()
    if p:
        p.submit(_write)


async def ensure_seed_user(db) -> dict | None:
    """Idempotent: guarantee the single lock-down account + FIXED API key
    exist (called at every boot and after admin resets). Never recreates
    what is already there, so credentials never change."""
    from . import config, security, util
    from .state import STATE
    from . import db as _db
    from sqlalchemy import select
    if not config.SEED_USER_EMAIL:
        return None
    email = config.SEED_USER_EMAIL.strip().lower()
    key_hash = security.api_key_hash(config.SEED_API_KEY)
    async with db.session() as sess:
        row = (await sess.execute(
            select(_db.t_users)
            .where(_db.t_users.c.email == email))).mappings().first()
        if row is None:
            salt = security.new_salt()
            pw = security.hash_password(config.SEED_USER_PASSWORD, salt)
            cur = await sess.execute(_db.t_users.insert().values(
                email=email, name="Karoon", pass_hash=pw, salt=salt,
                created_ms=util.now_ms()))
            uid = cur.inserted_primary_key[0]
            await sess.execute(_db.t_balances.insert().values(
                uid=uid, asset="USDT", free=config.SIGNUP_BONUS_USDT))
            await sess.execute(_db.t_ledger.insert().values(
                uid=uid, type="bonus", asset="USDT",
                amount=config.SIGNUP_BONUS_USDT,
                note="پاداش حساب اصلی (تست‌نت)", ts_ms=util.now_ms()))
        else:
            uid = row["id"]
        krow = (await sess.execute(
            select(_db.t_api_keys)
            .where(_db.t_api_keys.c.key_hash == key_hash))).mappings().first()
        if krow is None:
            await sess.execute(_db.t_api_keys.insert().values(
                uid=uid, key_hash=key_hash,
                key_plain=config.SEED_API_KEY,
                key_prefix=config.SEED_API_KEY[:10],
                secret_enc=security.encrypt_secret(config.SEED_API_SECRET),
                permissions='["readTrade", "trade"]', ips="",
                label="ثابت اصلی", created_ms=util.now_ms()))
        elif krow["revoked"] or krow["uid"] != uid:
            await sess.execute(
                _db.t_api_keys.update()
                .where(_db.t_api_keys.c.key_hash == key_hash)
                .values(revoked=0, uid=uid,
                        secret_enc=security.encrypt_secret(
                            config.SEED_API_SECRET),
                        permissions='["readTrade", "trade"]'))
        b = (await sess.execute(
            select(_db.t_balances.c.free)
            .where((_db.t_balances.c.uid == uid) &
                   (_db.t_balances.c.asset == "USDT")))).first()
        await sess.commit()
    # ---- mirror into in-memory state ----
    acct = STATE.account(uid)
    if not acct.balances.get("USDT"):
        acct.balances["USDT"] = b[0] if b else 0.0
    STATE.api_keys[key_hash] = dict(
        id=0, uid=uid, key_hash=key_hash, key=config.SEED_API_KEY,
        secret=config.SEED_API_SECRET, label="ثابت اصلی",
        permissions=["readTrade", "trade"], ips="", revoked=False)
    STATE.stats["users"] = max(STATE.stats.get("users", 0), 1)
    return {"uid": uid, "email": email, "api_key": config.SEED_API_KEY}


def new_session_token() -> str:
    import secrets
    return secrets.token_hex(24)


def session_persist_fn(token: str, uid: int):
    from . import db

    async def _write(session) -> None:
        await session.execute(db.t_sessions.insert().values(
            token_hash=_tok_hash(token), uid=uid,
            expires_ms=util.now_ms() + SESSION_TTL_MS))

    return _write


def _tok_hash(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def cache_session(token: str, uid: int) -> None:
    STATE.sessions[token] = uid


# --------------------------------------------------------------------------- #
# 2FA                                                                         #
# --------------------------------------------------------------------------- #
async def start_2fa(db, uid: int) -> tuple[str, str]:
    """Generate a pending TOTP secret (not enabled until confirmed)."""
    from . import db as _db
    secret = security.totp_generate_secret()
    async with db.session() as sess:
        async with sess.begin():
            await sess.execute(_db.t_users.update()
                               .where(_db.t_users.c.id == uid)
                               .values(totp_secret=secret, totp_enabled=0))
    email = await _email_of(db, uid)
    return secret, security.totp_uri(secret, email or f"user{uid}")


async def confirm_2fa(db, uid: int, code: str) -> bool:
    """Enable 2FA after a valid TOTP check."""
    from . import db as _db
    from sqlalchemy import select
    async with db.session() as sess:
        row = (await sess.execute(
            select(_db.t_users.c.totp_secret)
            .where(_db.t_users.c.id == uid))).first()
        if not row or not row[0]:
            raise ApiError(E_PARAM, "2FA setup not started")
        if not security.totp_verify(row[0], code):
            raise ApiError(E_PARAM, "invalid TOTP code")
        await sess.execute(_db.t_users.update()
                           .where(_db.t_users.c.id == uid)
                           .values(totp_enabled=1))
        await sess.commit()
    return True


async def _email_of(db, uid: int) -> Optional[str]:
    from . import db as _db
    from sqlalchemy import select
    async with db.session() as sess:
        row = (await sess.execute(
            select(_db.t_users.c.email).where(_db.t_users.c.id == uid))).first()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Faucet / wallet simulation                                                   #
# --------------------------------------------------------------------------- #
def faucet_ready(uid: int) -> tuple[bool, float]:
    """(claimable?, seconds-until-next)."""
    last = STATE.faucet.get(uid, 0.0)
    cooldown = config.FAUCET_COOLDOWN_HOURS * 3600.0
    elapsed = time.time() - last
    return elapsed >= cooldown, max(0.0, cooldown - elapsed)


def grant_faucet(uid: int, persist: Persister,
                 asset: str = "USDT", amount: float | None = None) -> float:
    """Credit test funds if the 24h cooldown passed; returns amount."""
    ok, _wait = faucet_ready(uid)
    if not ok:
        raise ApiError(E_PARAM, "faucet cooldown active")
    if asset != "USDT":
        raise ApiError(E_PARAM, "faucet currently dispenses USDT only")
    amount = float(amount or config.FAUCET_USDT)
    STATE.faucet[uid] = time.time()
    acct = STATE.account(uid)
    acct.balances[asset] = acct.free(asset) + amount
    matching.ledger(uid, "faucet", asset, amount, "دریافت سرمایه تستی")
    persist.submit(_persist_balance_fn(uid, asset))
    persist.submit(_persist_faucet_fn(uid))
    return amount


def deposit(uid: int, asset: str, amount: float, network: str,
            persist: Persister) -> str:
    """Simulated deposit for any listed asset."""
    if asset not in config.LISTED_ASSETS or amount <= 0 or amount > 1e9:
        raise ApiError(E_PARAM, "invalid asset or amount")
    acct = STATE.account(uid)
    acct.balances[asset] = acct.free(asset) + amount
    matching.ledger(uid, "deposit", asset, amount,
                    f"واریز آزمایشی ({network or 'TRC20'})")
    persist.submit(_persist_balance_fn(uid, asset))
    import secrets
    return secrets.token_hex(32)


def withdraw(uid: int, asset: str, amount: float, address: str,
             persist: Persister) -> str:
    """Simulated withdrawal (available balance only)."""
    if asset not in config.LISTED_ASSETS or amount <= 0:
        raise ApiError(E_PARAM, "invalid asset or amount")
    if len(address or "") < 8:
        raise ApiError(E_PARAM, "address must be at least 8 characters")
    acct = STATE.account(uid)
    if acct.available(asset) < amount:
        raise ApiError(110007, "insufficient withdrawable balance")
    acct.balances[asset] = acct.free(asset) - amount
    matching.ledger(uid, "withdraw", asset, -amount,
                    f"برداشت به {address[:12]}…")
    persist.submit(_persist_balance_fn(uid, asset))
    import secrets
    return secrets.token_hex(32)


def _persist_balance_fn(uid: int, asset: str):
    from . import db

    async def _write(session) -> None:
        acct = STATE.accounts.get(uid)
        free = acct.free(asset) if acct else 0.0
        await session.execute(
            db.t_balances.update()
            .where((db.t_balances.c.uid == uid) &
                   (db.t_balances.c.asset == asset))
            .values(free=free))

    return _write


def _persist_faucet_fn(uid: int):
    from . import db

    async def _write(session) -> None:
        await session.execute(
            db.t_faucet.insert().values(uid=uid,
                                        last_ms=int(STATE.faucet[uid] * 1000)))

    return _write


# --------------------------------------------------------------------------- #
# API keys                                                                     #
# --------------------------------------------------------------------------- #
async def create_api_key(db, uid: int, label: str,
                         permissions: list[str] | None = None,
                         ips: str = "") -> dict:
    """Create a key pair; the secret is shown exactly once."""
    from . import db as _db
    key, secret, key_hash, secret_enc = security.generate_api_key()
    perms = list(dict.fromkeys(permissions or ["readTrade"]))
    if not set(perms) <= {"readTrade", "trade"}:
        raise ApiError(E_PARAM, "permissions must be readTrade and/or trade")
    ts = util.now_ms()
    async with db.session() as sess:
        async with sess.begin():
            cur = await sess.execute(_db.t_api_keys.insert().values(
                uid=uid, key_hash=key_hash, key_plain=key, key_prefix=key[:8],
                secret_enc=secret_enc, permissions=__import__("json").dumps(perms),
                ips=ips, label=label[:40], created_ms=ts))
            kid = cur.inserted_primary_key[0]
    rec = dict(id=kid, uid=uid, key=key, secret=secret, key_hash=key_hash,
               permissions=perms, ips=ips, label=label, revoked=False)
    STATE.api_keys[key_hash] = rec
    return rec


async def revoke_api_key(db, uid: int, key_id: int) -> None:
    from . import db as _db
    async with db.session() as sess:
        async with sess.begin():
            await sess.execute(
                _db.t_api_keys.update()
                .where((_db.t_api_keys.c.id == key_id) &
                       (_db.t_api_keys.c.uid == uid))
                .values(revoked=1))
    for rec in STATE.api_keys.values():
        if rec.get("id") == key_id and rec["uid"] == uid:
            rec["revoked"] = True


async def list_api_keys(db, uid: int) -> list[dict]:
    from . import db as _db
    from sqlalchemy import select
    async with db.session() as sess:
        res = await sess.execute(
            select(_db.t_api_keys).where(_db.t_api_keys.c.uid == uid)
            .order_by(_db.t_api_keys.c.id.desc()))
        rows = res.mappings().all()
    out = []
    for r in rows:
        rec = STATE.api_keys.get(r["key_hash"]) or {}
        out.append(dict(id=r["id"], label=r["label"], created=r["created_ms"],
                        revoked=bool(r["revoked"]),
                        permissions=rec.get("permissions",
                                            __import__("json").loads(
                                                r["permissions"] or '["readTrade"]'))))
    return out
