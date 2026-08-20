# -*- coding: utf-8 -*-
"""
M4 (v5 trade REST): Bybit-compatible order endpoints.

POST /v5/order/create      — Limit/Market + conditional (StopOrder filter)
                             + entry TP/SL + tpslMode + batch variant
POST /v5/order/amend       — modify resting order price/qty
POST /v5/order/cancel      — by orderId or orderLinkId
POST /v5/order/cancel-all  — sweep open orders (optionally per symbol)
GET  /v5/order/realtime    — open + untriggered orders
GET  /v5/order/history     — DB-backed history with cursor pagination
GET  /v5/execution/list    — fill history

Signed via X-BAPI-* headers (deps.verify_v5_signature), requires the
`trade` permission for mutating endpoints.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from .. import config, util
from ..api import deps
from ..api.serializers import order_v5
from ..engine import orders as oms
from ..errors import ApiError, E_PARAM, E_SYMBOL_INVALID
from ..runtime import get_db, get_persister

router = APIRouter(prefix="/v5", tags=["v5-trade"])


async def _json(request: Request) -> dict:
    try:
        body = await request.body()
        return json.loads(body.decode() or "{}")
    except Exception:
        return {}


def _map_symbol(category: str, symbol: str) -> str:
    internal = config.resolve_symbol(category, symbol)
    if not internal:
        raise ApiError(E_SYMBOL_INVALID,
                       f"symbol {symbol} invalid for category {category}")
    return internal


@router.post("/order/create")
async def order_create(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = _map_symbol(b.get("category", "linear"), b.get("symbol", ""))
    order_filter = b.get("orderFilter", "Order")
    trigger_price = _f_or_none(b.get("triggerPrice"))
    if order_filter == "StopOrder" and trigger_price is None:
        raise ApiError(E_PARAM, "StopOrder requires triggerPrice")
    o = oms.place_order(
        uid=rec["uid"],
        symbol=symbol,
        side=b.get("side", ""),
        order_type=b.get("orderType", "Limit"),
        qty=float(b.get("qty", 0)),
        price=_f_or_none(b.get("price")),
        tif=b.get("timeInForce", "GTC"),
        reduce_only=bool(b.get("reduceOnly", False)),
        close_on_trigger=bool(b.get("closeOnTrigger", False)),
        trigger_price=trigger_price,
        trigger_by=b.get("triggerBy", "LastPrice"),
        tp=_f_or_none(b.get("takeProfit")),
        sl=_f_or_none(b.get("stopLoss")),
        tpsl_mode=b.get("tpslMode", "Full"),
        order_link_id=b.get("orderLinkId") or None,
    )
    from ..engine import matching
    return _ok({"orderId": o.order_id, "orderLinkId": o.order_link_id or ""})


@router.post("/order/create-batch")
async def order_create_batch(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    category = b.get("category", "linear")
    results = []
    for req in b.get("request", [])[:20]:
        try:
            symbol = _map_symbol(category, req.get("symbol", ""))
            o = oms.place_order(
                uid=rec["uid"], symbol=symbol, side=req.get("side", ""),
                order_type=req.get("orderType", "Limit"),
                qty=float(req.get("qty", 0)), price=_f_or_none(req.get("price")),
                tif=req.get("timeInForce", "GTC"),
                trigger_price=_f_or_none(req.get("triggerPrice")),
                order_link_id=req.get("orderLinkId") or None)
            results.append({"orderId": o.order_id,
                            "orderLinkId": o.order_link_id or ""})
        except ApiError as exc:
            results.append({"error": {"code": exc.ret_code,
                                      "msg": exc.ret_msg}})
    return _ok({"list": results})


@router.post("/order/amend")
async def order_amend(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = _map_symbol(b.get("category", "linear"), b.get("symbol", ""))
    o = oms.amend_order(rec["uid"], order_id=b.get("orderId"),
                        link_id=b.get("orderLinkId"),
                        price=_f_or_none(b.get("price")),
                        qty=_f_or_none(b.get("qty")))
    return _ok({"orderId": o.order_id, "orderLinkId": o.order_link_id or ""})


@router.post("/order/cancel")
async def order_cancel(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    o = oms.cancel_order(rec["uid"], order_id=b.get("orderId"),
                         link_id=b.get("orderLinkId"))
    return _ok({"orderId": o.order_id, "orderLinkId": o.order_link_id or ""})


@router.post("/order/cancel-batch")
async def order_cancel_batch(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    results = []
    for req in b.get("request", [])[:20]:
        try:
            o = oms.cancel_order(rec["uid"], order_id=req.get("orderId"),
                                 link_id=req.get("orderLinkId"))
            results.append({"orderId": o.order_id})
        except ApiError as exc:
            results.append({"error": {"code": exc.ret_code,
                                      "msg": exc.ret_msg}})
    return _ok({"list": results})


@router.post("/order/cancel-all")
async def order_cancel_all(request: Request):
    rec = await deps.verify_v5_signature(request, require="trade")
    b = await _json(request)
    symbol = None
    if b.get("symbol"):
        symbol = _map_symbol(b.get("category", "linear"), b["symbol"])
    n = oms.cancel_all(rec["uid"], symbol)
    return _ok({"list": [{"symbol": b.get("symbol", "ALL"),
                          "cancelQty": n}]})


@router.api_route("/order/realtime", methods=["GET", "POST"])
async def order_realtime(request: Request):
    rec = await deps.verify_v5_signature(request, require="readTrade")
    b = await _json(request) if request.method == "POST" else {}
    category = b.get("category", request.query_params.get("category", "linear"))
    symbol = b.get("symbol") or request.query_params.get("symbol")
    syms = _category_symbols(category, symbol)
    from ..engine import matching
    rows = []
    for o in list(STATE_OPEN().values()) + list(STATE_COND().values()):
        if o.uid != rec["uid"] or o.symbol not in syms:
            continue
        rows.append(order_v5(matching.order_snapshot(o)))
    return _ok({"category": category, "list": rows})


@router.get("/order/history")
async def order_history(request: Request, category: str = "linear",
                        symbol: str | None = None, baseCoin: str | None = None,
                        limit: int = 20, cursor: str | None = None):
    rec = await deps.verify_v5_signature(request, require="readTrade")
    limit = max(1, min(limit, 50))
    syms = _category_symbols(category, symbol)
    from .. import db
    from sqlalchemy import select
    database = get_db()
    after = int(cursor) if cursor and cursor.isdigit() else 1 << 62
    async with database.session() as sess:
        res = await sess.execute(
            select(db.t_orders)
            .where((db.t_orders.c.uid == rec["uid"]) &
                   (db.t_orders.c.id < after) &
                   db.t_orders.c.category == category)
            .order_by(db.t_orders.c.id.desc()).limit(limit))
        rows = res.mappings().all()
    from ..api.serializers import order_v5 as ser
    out = [ser(dict(r)) for r in rows if r["symbol"] in syms]
    next_cursor = str(rows[-1]["id"]) if len(rows) == limit else ""
    return _ok({"category": category, "list": out, "nextPageCursor": next_cursor})


@router.get("/execution/list")
async def execution_list(request: Request, category: str = "linear",
                         symbol: str | None = None, limit: int = 50,
                         cursor: str | None = None):
    rec = await deps.verify_v5_signature(request, require="readTrade")
    limit = max(1, min(limit, 100))
    syms = _category_symbols(category, symbol)
    from .. import db
    from sqlalchemy import select
    database = get_db()
    after = int(cursor) if cursor and cursor.isdigit() else 1 << 62
    async with database.session() as sess:
        res = await sess.execute(
            select(db.t_executions)
            .where((db.t_executions.c.uid == rec["uid"]) &
                   (db.t_executions.c.id < after))
            .order_by(db.t_executions.c.id.desc()).limit(limit))
        rows = res.mappings().all()
    out = []
    for r in rows:
        if r["symbol"] not in syms:
            continue
        cfg = config.MARKETS[r["symbol"]]
        out.append({
            "execId": r["exec_id"], "orderId": r["order_id"],
            "symbol": cfg.v5_symbol, "side": r["side"],
            "orderQty": util.fmt(r["qty"], cfg.qty_step),
            "orderPrice": util.fmt(r["price"], cfg.tick),
            "execPrice": util.fmt(r["price"], cfg.tick),
            "execQty": util.fmt(r["qty"], cfg.qty_step),
            "execType": r["exec_type"], "execFee": f"{r['fee']:.6f}",
            "isMaker": bool(r["is_maker"]),
            "execTime": str(r["created_ms"]),
            "closedSize": "0",
        })
    next_cursor = str(rows[-1]["id"]) if len(rows) == limit else ""
    return _ok({"category": category, "list": out, "nextPageCursor": next_cursor})


# --------------------------------------------------------------------------- #
def _category_symbols(category: str, symbol: str | None) -> set[str]:
    if symbol:
        return {_map_symbol(category, symbol)}
    if category == "spot":
        return {s for s, m in config.MARKETS.items() if m.kind == "spot"}
    if category == "linear":
        return {s for s, m in config.MARKETS.items() if m.kind == "linear"}
    raise ApiError(E_PARAM, "invalid category")


def STATE_OPEN():
    from ..state import STATE
    return STATE.open_orders


def STATE_COND():
    from ..state import STATE
    return STATE.conditional


def _f_or_none(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _ok(result: dict) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result,
            "retExtInfo": {}, "time": util.now_ms()}
