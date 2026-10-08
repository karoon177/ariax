# -*- coding: utf-8 -*-
"""
AriaX extensions on the v5 API surface:

  POST /v5/asset/faucet          — claim test funds (24h cooldown)
  GET  /v5/backtest/strategies   — available strategies
  POST /v5/backtest/run          — deterministic replay
  GET  /v5/backtest/results      — user's saved runs
  GET  /v5/backtest/result/{id}  — one saved run
  POST /v5/admin/force-price     — stress: pin a reference price
  POST /v5/admin/mm-intensity    — stress: scale MM quote sizes
  GET  /v5/admin/stats           — engine stats (orders, latency, ws)

Admin endpoints require header `X-Admin-Token` == env ADMIN_TOKEN and are
disabled entirely when ADMIN_TOKEN is unset.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from .. import backtest, config, util
from ..api import deps
from ..errors import ApiError, E_PARAM
from ..runtime import get_db, get_persister
from ..state import STATE
from .. import users

router = APIRouter(prefix="/v5", tags=["v5-extra"])


async def _json(request: Request) -> dict:
    try:
        return json.loads((await request.body()).decode() or "{}")
    except Exception:
        return {}


async def _auth_any(request: Request):
    """Accept either a signed API key or a UI session token."""
    try:
        return await deps.verify_v5_signature(request)
    except ApiError:
        uid = await deps.require_session(request)
        return {"uid": uid, "permissions": ["readTrade", "trade"]}


@router.post("/asset/faucet")
async def faucet(request: Request):
    rec = await _auth_any(request)
    b = await _json(request)
    amount = users.grant_faucet(rec["uid"], get_persister(),
                                asset=b.get("asset", "USDT"),
                                amount=b.get("amount"))
    return _ok({"asset": "USDT", "amount": amount,
                "cooldownHours": config.FAUCET_COOLDOWN_HOURS})


@router.get("/backtest/strategies")
async def strategies(request: Request):
    return _ok({"strategies": list(backtest.STRATEGIES),
                "intervals": list(klines_intervals())})


def klines_intervals():
    from ..marketdata.klines import INTERVAL_MIN
    return INTERVAL_MIN.keys()


@router.post("/backtest/run")
async def backtest_run(request: Request):
    rec = await _auth_any(request)
    b = await _json(request)
    symbol = b.get("symbol", "")
    internal = config.resolve_symbol(b.get("category", "linear"), symbol)
    if not internal:
        raise ApiError(E_PARAM, "invalid symbol")
    try:
        result = await backtest.run_backtest(
            internal,
            interval=str(b.get("interval", "15")),
            strategy=b.get("strategy", "ema_cross"),
            initial=float(b.get("initialCapital", 10_000)),
            leverage=int(b.get("leverage", 5)),
            slippage_bps=float(b.get("slippageBps", 2)),
            params=b.get("params") or {},
            limit=int(b.get("limit", 500)))
    except ValueError as exc:
        raise ApiError(E_PARAM, str(exc))
    bt_id = util.gen_hex(6)
    result["id"] = bt_id
    from .. import db
    async with get_db().session() as sess:
        async with sess.begin():
            await sess.execute(db.t_backtests.insert().values(
                id=bt_id, uid=rec["uid"],
                params=json.dumps({k: b.get(k) for k in
                                   ("symbol", "category", "interval",
                                    "strategy", "initialCapital",
                                    "leverage", "slippageBps", "limit")}),
                result=backtest.dumps(result), created_ms=util.now_ms()))
    return _ok(result)


@router.get("/backtest/results")
async def backtest_results(request: Request, limit: int = 20):
    rec = await _auth_any(request)
    from .. import db
    from sqlalchemy import select
    async with get_db().session() as sess:
        res = await sess.execute(
            select(db.t_backtests.c.id, db.t_backtests.c.params,
                   db.t_backtests.c.created_ms)
            .where(db.t_backtests.c.uid == rec["uid"])
            .order_by(db.t_backtests.c.created_ms.desc())
            .limit(max(1, min(limit, 100))))
        rows = res.all()
    return _ok({"list": [{"id": r[0], "params": json.loads(r[1]),
                          "created": r[2]} for r in rows]})


@router.get("/backtest/result/{bt_id}")
async def backtest_result(bt_id: str, request: Request):
    rec = await _auth_any(request)
    from .. import db
    from sqlalchemy import select
    async with get_db().session() as sess:
        res = await sess.execute(
            select(db.t_backtests)
            .where((db.t_backtests.c.id == bt_id) &
                   (db.t_backtests.c.uid == rec["uid"])))
        row = res.mappings().first()
    if not row:
        raise ApiError(E_PARAM, "backtest not found")
    return _ok(json.loads(row["result"]))


# --------------------------------------------------------------------------- #
# Admin / stress-test helpers                                                  #
# --------------------------------------------------------------------------- #
def _require_admin(request: Request) -> None:
    if not config.ADMIN_TOKEN:
        raise ApiError(E_PARAM, "admin endpoints disabled (set ADMIN_TOKEN)")
    if request.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
        raise ApiError(E_PARAM, "invalid admin token", http_status=403)


@router.post("/admin/force-price")
async def force_price(request: Request):
    _require_admin(request)
    b = await _json(request)
    symbol = config.resolve_symbol(b.get("category", "linear"), b.get("symbol", ""))
    if not symbol:
        raise ApiError(E_PARAM, "invalid symbol")
    try:
        px = float(b["price"])
    except (KeyError, TypeError, ValueError):
        raise ApiError(E_PARAM, "invalid price")
    if px <= 0:
        # release the override; the live reference feed resumes control
        STATE.force_price.pop(symbol, None)
        return _ok({"symbol": b.get("symbol"), "price": None,
                    "released": True})
    STATE.force_price[symbol] = px
    STATE.tick(symbol).on_trade_price(px)
    STATE.tick(symbol).mark = px
    return _ok({"symbol": b.get("symbol"), "price": px})


@router.post("/admin/mm-intensity")
async def mm_intensity(request: Request):
    _require_admin(request)
    b = await _json(request)
    try:
        mult = float(b.get("multiplier", 1.0))
    except (TypeError, ValueError):
        raise ApiError(E_PARAM, "invalid multiplier")
    STATE.mm_intensity = max(0.0, min(mult, 100.0))
    return _ok({"mmIntensity": STATE.mm_intensity})


@router.post("/admin/reset-users")
async def reset_users(request: Request):
    """Wipe ALL users/orders/positions/history, then recreate the single
    seed account with its FIXED API key. Admin-token guarded."""
    _require_admin(request)
    from .. import db as dbm, users as users_mod
    from ..engine import orders as oms
    from ..state import STATE
    from .runtime import get_db
    database = get_db()
    if database is None:
        raise ApiError(E_PARAM, "database unavailable")
    # 1) cancel every open/conditional user order
    cancelled = 0
    for o in list(STATE.open_orders.values()) + list(STATE.conditional.values()):
        if o.uid > 0:
            try:
                oms.cancel_order(o.uid, order_id=o.order_id)
                cancelled += 1
            except Exception:
                pass
    # 2) force-close every open position at market
    closed = 0
    for (uid, sym), pos in list(STATE.positions.items()):
        if pos.size == 0:
            continue
        side = "Sell" if pos.size > 0 else "Buy"
        try:
            oms.place_order(uid, sym, side, "Market", abs(pos.size),
                            reduce_only=True, close_on_trigger=True,
                            leverage=pos.leverage,
                            close_reason="AdminReset",
                            strategy=pos.strategy)
            closed += 1
        except Exception:
            pass
    # 3) clear in-memory trading state
    STATE.positions.clear()
    STATE.open_orders.clear()
    STATE.conditional.clear()
    STATE.accounts.clear()
    STATE.sessions.clear()
    STATE.api_keys.clear()
    STATE.leverage.clear()
    STATE.close_locks.clear()
    STATE.bots.clear()
    STATE.faucet.clear()
    STATE.insurance_pool = 0.0
    # 4) wipe user-related tables (market data tables untouched)
    async with database.session() as sess:
        async with sess.begin():
            for t in (dbm.t_faucet, dbm.t_closed_trades, dbm.t_executions,
                      dbm.t_orders, dbm.t_positions, dbm.t_ledger,
                      dbm.t_futures_balances, dbm.t_balances,
                      dbm.t_api_keys, dbm.t_sessions, dbm.t_users,
                      dbm.t_security):
                await sess.execute(t.delete())
    # 5) recreate the single seed account + fixed key
    seed = await users_mod.ensure_seed_user(database)
    STATE.stats["users"] = 1
    return _ok({"cancelled_orders": cancelled, "closed_positions": closed,
                "users_now": 1,
                "seed_user": (seed or {}).get("email"),
                "fixed_api_key": (seed or {}).get("api_key")})


@router.get("/admin/stats")
async def admin_stats(request: Request):
    _require_admin(request)
    from .. import agents
    return _ok({"stats": agents.stats_payload(),
                "reference": dict(STATE.reference, prices=len(STATE.reference.get("prices", {}))),
                "insurance": STATE.insurance_pool,
                "force_price": STATE.force_price})


def _ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result,
            "retExtInfo": {}, "time": util.now_ms()}
