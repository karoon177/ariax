# -*- coding: utf-8 -*-
"""
M8/M12 (klines): OHLCV aggregation + Kraken OHLC passthrough.

* Internal 1m candles aggregate every last-price tick; larger intervals
  (3/5/15/30/60/120/240/360/720/D/W/M) are folded from the 1m series —
  identical to how Bybit serves composite intervals.
* For spot symbols, Kraken's real OHLC endpoint is preferred (8 s cache);
  internal candles are the always-available fallback (`stale=true`).
* Output rows follow Bybit v5 kline shape:
  [startTimeMs, open, high, low, close, volume, turnover]
  in *newest-first* order.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .. import config, util
from ..state import STATE

INTERVAL_MIN = {
    "1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120,
    "240": 240, "360": 360, "720": 720, "D": 1440, "W": 10080, "M": 43200,
}

_ohlc_cache: dict[tuple, tuple[float, list]] = {}
_ohlc_lock = asyncio.Lock()


async def kraken_ohlc(symbol: str, interval_min: int, limit: int = 500) -> list | None:
    """Fetch real Kraken OHLC for a spot symbol (None when unavailable)."""
    cfg = config.MARKETS.get(symbol)
    pair = cfg.kraken_spot if cfg else None
    if not pair:
        pair = config.MARKETS.get(
            config.PERP_UNDERLYING.get(symbol, "")).kraken_spot if config.PERP_UNDERLYING.get(symbol) else None
    if not pair:
        return None
    key = (symbol, interval_min)
    now = time.time()
    async with _ohlc_lock:
        cached = _ohlc_cache.get(key)
        if cached and now - cached[0] < 8:
            return cached[1]
    try:
        async with httpx.AsyncClient(timeout=7.0) as client:
            r = await client.get(
                f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval_min}")
            r.raise_for_status()
            payload = r.json()
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        rows = next(v for k, v in payload["result"].items() if k != "last")
        data = [[int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]),
                 float(x[4]), float(x[6]), float(x[5]) * float(x[4])]
                for x in rows[-limit:]]
        async with _ohlc_lock:
            _ohlc_cache[key] = (now, data)
        return data
    except Exception:
        return None


def fold(candles_1m: list[list], interval_min: int, limit: int) -> list[list]:
    """Aggregate 1m candles into the requested interval."""
    out: list[list] = []
    step = interval_min * 60_000
    for c in candles_1m:
        b = c[0] // step * step
        if out and out[-1][0] == b:
            row = out[-1]
            row[2] = max(row[2], c[2])
            row[3] = min(row[3], c[3])
            row[4] = c[4]
            row[5] += c[5]
            row[6] = row[5] * row[4] if len(row) > 5 else 0
        else:
            out.append([b, c[1], c[2], c[3], c[4], c[5], c[1] * c[5]])
    return out[-limit:]


async def get_klines(symbol: str, interval: str, limit: int,
                     source: str = "last") -> tuple[list[list], str, bool]:
    """Return (rows_newest_first, source, stale).

    source: 'last'  -> trade-price candles (default);
            'mark'  -> mark-price candles;
            'index' -> index-price candles.
    """
    if interval not in INTERVAL_MIN:
        raise ValueError(f"unsupported interval {interval}")
    n = INTERVAL_MIN[interval]
    limit = max(1, min(int(limit), 1000))
    t = STATE.tick(symbol)

    if source == "last":
        # real Kraken OHLC for spot pairs AND for linear perps via their
        # spot underlying (mark ≈ spot; keeps long-interval history deep)
        spot_symbol = symbol if config.MARKETS[symbol].kind == "spot" \
            else config.PERP_UNDERLYING.get(symbol)
        if spot_symbol:
            real = await kraken_ohlc(spot_symbol, n, limit)
            if real:
                return real[-limit:][::-1], "Kraken", False

    base = list(t.candles1m) + ([t.cur] if t.cur else [])
    if source != "last":
        base = [c for c in base]  # mark/index fold from same 1m series
    rows = fold(base, n, limit)
    rows.reverse()  # newest first (Bybit order)
    return rows, "internal", not STATE.reference.get("updated")


def seed_all() -> None:
    """Bootstrap every market's candle history at startup."""
    for symbol in config.MARKETS:
        STATE.tick(symbol).seed_history()
