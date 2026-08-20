# -*- coding: utf-8 -*-
"""
M8 (v5 market data REST): public endpoints mirroring Bybit v5.

All endpoints are public (rate-limited per IP, no signature required),
responses use the standard envelope {retCode, retMsg, result, time}.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Query, Request

from .. import config, util
from ..api.deps import RL, client_ip
from ..api.serializers import instrument_v5, ticker_v5
from ..engine import orderbook
from ..errors import ApiError, E_PARAM, E_SYMBOL_INVALID
from ..marketdata import klines
from ..state import STATE

router = APIRouter(prefix="/v5/market", tags=["v5-market"])

ALLOWED_DEPTHS = (1, 50, 200)


def _rate_public(request: Request) -> None:
    RL.check(client_ip(request), "public", 30, 1.0)


def _resolve(category: str, symbol: str | None) -> list[str]:
    """Category+symbol -> list of internal symbols."""
    if symbol:
        internal = config.resolve_symbol(category, symbol)
        if not internal:
            raise ApiError(E_SYMBOL_INVALID, f"symbol {symbol} not found "
                                             f"in category {category}")
        return [internal]
    if category == "spot":
        return [s for s, m in config.MARKETS.items() if m.kind == "spot"]
    if category == "linear":
        return [s for s, m in config.MARKETS.items() if m.kind == "linear"]
    raise ApiError(E_PARAM, "category must be spot or linear")


@router.get("/time")
async def market_time(request: Request):
    """Server time (unix ms)."""
    _rate_public(request)
    return {"retCode": 0, "retMsg": "OK",
            "result": {"timeSecond": str(int(util.now_ms() / 1000)),
                       "timeNano": str(util.now_ms() * 1_000_000)},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/kline")
async def market_kline(request: Request, category: str = Query("linear"),
                       symbol: str = Query(...), interval: str = Query("1"),
                       start: int | None = Query(None),
                       end: int | None = Query(None),
                       limit: int = Query(200, ge=1, le=1000)):
    """Candlesticks (newest first): [ts, o, h, l, c, volume, turnover]."""
    _rate_public(request)
    symbols = _resolve(category, symbol)
    rows, source, stale = await klines.get_klines(
        symbols[0], interval, limit, source="last")
    if start:
        rows = [r for r in rows if r[0] >= start]
    if end:
        rows = [r for r in rows if r[0] <= end]
    out = [[str(int(r[0])), f"{r[1]:g}", f"{r[2]:g}", f"{r[3]:g}", f"{r[4]:g}",
            f"{r[5]:.8g}", f"{r[6]:.2f}"] for r in rows]
    return {"retCode": 0, "retMsg": "OK", "result": {"category": category,
                                                     "symbol": symbol,
                                                     "list": out,
                                                     "source": source,
                                                     "stale": stale},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/mark-price-kline")
async def mark_kline(request: Request, category: str = Query("linear"),
                     symbol: str = Query(...), interval: str = Query("1"),
                     limit: int = Query(200, ge=1, le=1000)):
    """Mark-price candlesticks (perp only)."""
    _rate_public(request)
    symbols = _resolve(category, symbol)
    rows, _, _ = await klines.get_klines(symbols[0], interval, limit,
                                         source="mark")
    out = [[str(int(r[0])), f"{r[1]:g}", f"{r[2]:g}", f"{r[3]:g}", f"{r[4]:g}",
            f"{r[5]:.8g}", f"{r[6]:.2f}"] for r in rows]
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category, "symbol": symbol, "list": out},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/index-price-kline")
async def index_kline(request: Request, category: str = Query("linear"),
                      symbol: str = Query(...), interval: str = Query("1"),
                      limit: int = Query(200, ge=1, le=1000)):
    """Index-price candlesticks."""
    _rate_public(request)
    symbols = _resolve(category, symbol)
    rows, _, _ = await klines.get_klines(symbols[0], interval, limit,
                                         source="index")
    out = [[str(int(r[0])), f"{r[1]:g}", f"{r[2]:g}", f"{r[3]:g}", f"{r[4]:g}",
            f"{r[5]:.8g}", f"{r[6]:.2f}"] for r in rows]
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category, "symbol": symbol, "list": out},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/instruments-info")
async def instruments_info(request: Request, category: str = Query("linear"),
                           symbol: str | None = Query(None),
                           limit: int = Query(50, ge=1, le=1000),
                           cursor: str | None = Query(None)):
    """Instrument specifications (filters, leverage, risk limits)."""
    _rate_public(request)
    syms = _resolve(category, symbol)
    page = syms[:limit]
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category,
                       "list": [instrument_v5(s) for s in page],
                       "nextPageCursor": ""},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/orderbook")
async def market_orderbook(request: Request, category: str = Query("linear"),
                           symbol: str = Query(...),
                           limit: int = Query(25)):
    """Aggregated orderbook snapshot (Bybit shape: s, b, a, u, ts)."""
    _rate_public(request)
    if limit not in (1, 25, 50, 200):
        limit = min(ALLOWED_DEPTHS, key=lambda x: abs(x - limit))
    symbols = _resolve(category, symbol)
    snap = orderbook.book(symbols[0]).depth(limit)
    snap["s"] = symbol
    return {"retCode": 0, "retMsg": "OK", "result": snap,
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/tickers")
async def market_tickers(request: Request, category: str = Query("linear"),
                         symbol: str | None = Query(None)):
    """24h tickers + funding/mark fields for linear."""
    _rate_public(request)
    syms = _resolve(category, symbol)
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category,
                       "list": [ticker_v5(s, STATE.tick(s)) for s in syms]},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/recent-trade")
async def recent_trade(request: Request, category: str = Query("linear"),
                       symbol: str = Query(...),
                       limit: int = Query(50, ge=1, le=1000),
                       window: str | None = Query(None)):
    """Public trade history (last N tape prints)."""
    _rate_public(request)
    symbols = _resolve(category, symbol)
    t = STATE.tick(symbols[0])
    trades = list(t.trades)[:limit]
    out = [{"execId": util.gen_hex(8), "time": int(tr[0]), "price": f"{tr[2]:g}",
            "size": f"{tr[3]:.8g}", "side": tr[1]} for tr in trades]
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category, "symbol": symbol, "list": out},
            "retExtInfo": {}, "time": util.now_ms()}


@router.get("/funding/history")
async def funding_history(request: Request, category: str = Query("linear"),
                          symbol: str = Query(...),
                          limit: int = Query(50, ge=1, le=200)):
    """Historical funding rates (from funding_history table)."""
    _rate_public(request)
    symbols = _resolve(category, symbol)
    rows = await _funding_rows(symbols[0], limit)
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": category, "symbol": symbol, "list": rows},
            "retExtInfo": {}, "time": util.now_ms()}


async def _funding_rows(symbol: str, limit: int) -> list:
    from .. import db
    from ..runtime import get_db
    database = get_db()
    try:
        async with database.session() as sess:
            q = (db.t_funding.select()
                 .where(db.t_funding.c.symbol == symbol)
                 .order_by(db.t_funding.c.id.desc()).limit(limit))
            res = await sess.execute(q)
            return [{"symbol": symbol, "fundingRate": f"{r.rate:.6f}",
                     "fundingRateTimestamp": str(r.ts_ms),
                     "markPrice": f"{r.price:g}"} for r in res]
    except Exception:
        return []


_ = math  # reserved
