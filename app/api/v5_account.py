# -*- coding: utf-8 -*-
"""
M7/M9 (v5 account & position REST): wallet, positions, leverage,
trading-stop (TP/SL/trailing), margin, closed PnL, transaction log,
API-key introspection.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from .. import config, util
from ..api import deps
from ..api.serializers import position_v5, wallet_event_v5, wallet_v5
from ..engine import matching, orders as oms
from ..errors import ApiError, E_PARAM, E_SYMBOL_INVALID
from ..runtime import get_db, get_persister
from ..state import STATE

router = APIRouter(prefix="/v5", tags=["v5-account"])


async def _json(request: Request) -> dict:
    try:
        body = await request.body()
        return json.loads(body.decode() or "{}")
    except Exception:
        return {}


def _map_symbol(category: str, symbol: str) -> str:
    internal = config.resolve_symbol(category, symbol)
    if not internal:
        raise ApiError(E_SYMBOL_INVALID, f"symbol {symbol} invalid")
    return internal


# --------------------------------------------------------------------------- #
# Account                                                                      #
# --------------------------------------------------------------------------- #
@router.get("/account/wallet-balance")
async def wallet_balance(request: Request, accountType: str = "UNIFIED",
                         coin: str | None = None):
    rec = await deps.verify_v5_signature(request)
    accountType = accountType.upper()
    if accountType not in ("UNIFIED", "FUND", "CONTRACT", "SPOT"):
        accountType = "UNIFIED"
    data = wallet_v5(rec["uid"])
    data.pop("list", None)
    # SPOT/CONTRACT views show their own bucket; UNIFIED shows combined
    coins = wallet_event_v5(rec["uid"], accountType)["balances"]
    if coin:
        coins = [c for c in coins if c["coin"] == coin.upper()]
    data["list"] = [{
        "accountType": accountType,
        "totalEquity": data["totalEquity"],
        "totalMarginBalance": data.get("totalMarginBalance", "0"),
        "totalAvailableBalance": data["totalAvailableBalance"],
        "coin": coins}]
    return _ok(data)


@router.get("/account/info")
async def account_info(request: Request):
    rec = await deps.verify_v5_signature(request)
    return _ok({"marginMode": "ISOLATED_MARGIN",
                "dcpStatus": "AccountNormal",
                "totalEquity": f"{STATE.equity_usdt(rec['uid']):.4f}",
                "unifiedMarginStatus": 1,
                "accountIMRate": "0",
                "accountMMRate": "0"})


@router.get("/account/transaction-log")
async def transaction_log(request: Request, limit: int = 50,
                          cursor: str | None = None, symbol: str | None = None,
                          currency: str | None = None):
    rec = await deps.verify_v5_signature(request)
    from .. import db
    from sqlalchemy import select
    limit = max(1, min(limit, 100))
    after = int(cursor) if cursor and cursor.isdigit() else 1 << 62
    database = get_db()
    async with database.session() as sess:
        res = await sess.execute(
            select(db.t_ledger)
            .where((db.t_ledger.c.uid == rec["uid"]) & (db.t_ledger.c.id < after))
            .order_by(db.t_ledger.c.id.desc()).limit(limit))
        rows = res.mappings().all()
    out = [{
        "symbol": r["type"], "type": r["type"], "changeCoin": r["asset"],
        "changeAmount": f"{r['amount']:.8f}", "fee": "0",
        "cashFlow": f"{r['amount']:.8f}", "note": r["note"],
        "transactionTime": str(r["ts_ms"]),
    } for r in rows]
    next_cursor = str(rows[-1]["id"]) if len(rows) == limit else ""
    return _ok({"list": out, "nextPageCursor": next_cursor})


# --------------------------------------------------------------------------- #
# Position                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/position/list")
async def position_list(request: Request, category: str = "linear",
                        symbol: str | None = None):
    rec = await deps.verify_v5_signature(request)
    if category != "linear":
        raise ApiError(E_PARAM, "positions exist for linear category only")
    syms = {_map_symbol(category, symbol)} if symbol else \
        {s for s, m in config.MARKETS.items() if m.kind == "linear"}
    out = []
    for (uid, sym), pos in sorted(STATE.positions.items()):
        if uid == rec["uid"] and pos.size != 0 and sym in syms:
            out.append(position_v5(uid, sym, pos))
    return _ok({"category": category, "list": out, "nextPageCursor": ""})


@router.post("/position/set-leverage")
async def set_leverage(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = _map_symbol(b.get("category", "linear"), b.get("symbol", ""))
    lev = int(float(b.get("buyLeverage") or b.get("sellLeverage") or
                    b.get("leverage") or 0))
    cfg = config.MARKETS[symbol]
    pos = STATE.position(rec["uid"], symbol)
    notional = abs(pos.size) * (pos.entry or STATE.tick(symbol).mark) \
        if pos and pos.size else 0.0
    _, tier_max_lev, _ = config.tier_for_notional(cfg, notional) \
        if notional else (0, cfg.max_lev, 0)
    if lev < 1 or lev > min(cfg.max_lev, tier_max_lev):
        raise ApiError(E_PARAM,
                       f"leverage must be within 1..{min(cfg.max_lev, tier_max_lev)}")
    STATE.leverage[(rec["uid"], symbol)] = lev
    if pos and pos.size != 0:
        pos.leverage = lev
        pos.updated_ms = util.now_ms()
        matching._persist_position(rec["uid"], symbol)
    return _ok({"leverage": str(lev)})


@router.post("/position/trading-stop")
async def trading_stop(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = _map_symbol(b.get("category", "linear"), b.get("symbol", ""))

    def fv(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    oms.set_trading_stop(rec["uid"], symbol,
                         tp=fv(b.get("takeProfit")),
                         sl=fv(b.get("stopLoss")),
                         trailing=fv(b.get("trailingStop")))
    return _ok({"stopOrderId": "", "user":{"uid": rec["uid"]}})


@router.post("/position/set-margin")
async def set_margin(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = _map_symbol(b.get("category", "linear"), b.get("symbol", ""))
    mode = (b.get("mode") or "ADD").upper()
    try:
        amount = float(b.get("margin", 0))
    except (TypeError, ValueError):
        raise ApiError(E_PARAM, "invalid margin amount")
    pos = STATE.position(rec["uid"], symbol)
    if not pos or pos.size == 0:
        raise ApiError(110131, "position does not exist")
    acct = STATE.account(rec["uid"])
    if mode == "ADD":
        if acct.available("USDT") < amount or amount <= 0:
            raise ApiError(110007, "insufficient available USDT")
        acct.balances["USDT"] = acct.free("USDT") - amount
        pos.margin += amount
    elif mode == "REDUCE":
        amount = min(amount, pos.margin)
        pos.margin -= amount
        acct.balances["USDT"] = acct.free("USDT") + amount
    else:
        raise ApiError(E_PARAM, "mode must be ADD or REDUCE")
    pos.updated_ms = util.now_ms()
    matching._persist_position(rec["uid"], symbol)
    matching._persist_balances(rec["uid"], ["USDT"])
    return _ok({"newMargin": f"{pos.margin:.4f}"})


@router.get("/position/closed-pnl")
async def closed_pnl(request: Request, category: str = "linear",
                     symbol: str | None = None, limit: int = 50):
    rec = await deps.verify_v5_signature(request)
    from .. import db
    from sqlalchemy import select
    syms = {_map_symbol(category, symbol)} if symbol else \
        {s for s, m in config.MARKETS.items() if m.kind == "linear"}
    database = get_db()
    async with database.session() as sess:
        res = await sess.execute(
            select(db.t_ledger)
            .where((db.t_ledger.c.uid == rec["uid"]) &
                   (db.t_ledger.c.type == "realized_pnl"))
            .order_by(db.t_ledger.c.id.desc()).limit(max(1, min(limit, 200))))
        rows = res.mappings().all()
    out = []
    for r in rows:
        try:
            sym = r["note"].split()[2]
        except IndexError:
            sym = ""
        if sym in syms:
            out.append({"symbol": sym, "orderType": "Market",
                        "leverage": "", "closedPnl": f"{r['amount']:.4f}",
                        "avgEntryPrice": "0", "avgExitPrice": "0",
                        "createdTime": str(r["ts_ms"])})
    return _ok({"category": category, "list": out, "nextPageCursor": ""})


# --------------------------------------------------------------------------- #
# User / API keys                                                              #
# --------------------------------------------------------------------------- #
@router.get("/user/query-api")
async def query_api(request: Request):
    rec = await deps.verify_v5_signature(request)
    from .. import users
    keys = await users.list_api_keys(get_db(), rec["uid"])
    return _ok({"list": [{
        "id": str(k["id"]), "note": k["label"],
        "permissions": {"ContractTrade": ["Read", "Trade"] if "trade" in
                       k["permissions"] else ["Read"]},
        "ips": [], "type": "classic", "limit": "",
        "expiredAt": "", "createdAt": str(k["created"]),
    } for k in keys if not k["revoked"]]})


def _ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result,
            "retExtInfo": {}, "time": util.now_ms()}
