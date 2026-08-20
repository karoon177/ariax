# -*- coding: utf-8 -*-
"""
M8 (funding): perpetual funding-rate engine.

Predicted rate (Bybit formula, simplified):
    premium  = (mark - index) / index
    funding  = clamp(premium * premium_smoothing + base_rate, ±cap)
Settlement occurs every `FUNDING_INTERVAL_H` hours on the UTC grid
(00:00/08:00/16:00 by default). Longs pay shorts when the rate is
positive. Payments touch only the USDT wallet and the transaction log.
"""
from __future__ import annotations

import asyncio
import math

from .. import config, events, util
from ..state import STATE
from . import matching

_PREMIUM_EMA: dict[str, float] = {}


def next_funding_ts(now_ms: int, interval_h: float) -> int:
    """Next UTC grid timestamp for the funding interval."""
    step = int(interval_h * 3600_000)
    return (now_ms // step + 1) * step


def predicted_rate(symbol: str, mark: float, index: float) -> float:
    """EMA-smoothed clamped premium + static interest component."""
    if index <= 0 or mark <= 0:
        return 0.0
    premium = (mark - index) / index
    prev = _PREMIUM_EMA.get(symbol, premium)
    smooth = prev * 0.8 + premium * 0.2
    _PREMIUM_EMA[symbol] = smooth
    rate = smooth + config.FUNDING_BASE_RATE
    return max(-config.FUNDING_CAP, min(config.FUNDING_CAP, rate))


def settle_funding(symbol: str, rate: float, mark: float) -> int:
    """Charge/credit funding for every open position on `symbol`."""
    n = 0
    ts = util.now_ms()
    for (uid, sym), pos in list(STATE.positions.items()):
        if sym != symbol or pos.size == 0:
            continue
        payment = rate * mark * abs(pos.size)   # >0: longs pay shorts
        acct = STATE.account(uid)
        if pos.size > 0:
            acct.balances["USDT"] = acct.free("USDT") - payment
        else:
            acct.balances["USDT"] = acct.free("USDT") + payment
        matching.ledger(uid, "funding", "USDT", -payment if pos.size > 0 else payment,
                        f"Funding {symbol} rate={rate:.6f}")
        events.BUS.emit("wallet", {"uid": uid})
        matching._persist_balances(uid, ["USDT"])
        n += 1
    events.BUS.emit("persist", lambda s: _write_funding(s, symbol, rate, mark, ts))
    return n


async def _write_funding(session, symbol, rate, mark, ts) -> None:
    from .. import db
    await session.execute(db.t_funding.insert().values(
        symbol=symbol, rate=rate, price=mark, ts_ms=ts))


async def funding_loop() -> None:
    """Continuous: refresh predicted rates; settle on the UTC grid."""
    while True:
        try:
            now = util.now_ms()
            for symbol, cfg in config.MARKETS.items():
                if cfg.kind != "linear":
                    continue
                t = STATE.tick(symbol)
                if not t.next_funding_ms:
                    t.next_funding_ms = next_funding_ts(now, config.FUNDING_INTERVAL_H)
                t.funding_rate = predicted_rate(symbol, t.mark, t.index)
                if now >= t.next_funding_ms:
                    mark = t.mark or t.last
                    rate = t.funding_rate
                    count = settle_funding(symbol, rate, mark)
                    t.prev_funding_rate = rate
                    t.next_funding_ms = next_funding_ts(now, config.FUNDING_INTERVAL_H)
                    if count:
                        events.BUS.emit("agent_risk", {
                            "msg": f"Funding settled {symbol} "
                                   f"rate={rate * 100:.4f}% for {count} positions"})
        except Exception:
            import logging
            logging.getLogger("ariax.funding").exception("funding loop error")
        await asyncio.sleep(1.0)


def funding_info(symbol: str) -> dict:
    """Bybit tickers funding fields for one linear symbol."""
    t = STATE.tick(symbol)
    return dict(
        fundingRate=t.funding_rate,
        nextFundingTime=t.next_funding_ms,
        prevFundingRate=t.prev_funding_rate,
    )


_ = math  # keep import lint-clean (math reserved for future tier math)
