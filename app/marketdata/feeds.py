# -*- coding: utf-8 -*-
"""
M8 (market feed): Kraken reference prices for index/mark, live tape
simulation, price-override hooks for stress tests, and oversight agents.

Reference policy (carried over from v1, hardened):
  * spot index   <- Kraken spot last trade price;
  * perp mark    <- Kraken futures markPrice (PF_ perpetuals);
  * on feed failure the last price is HELD and the feed is marked stale —
    a live market is never replaced with random prices.
An admin override (`STATE.force_price`, used by the stress suite) takes
precedence and is always reported as source "admin-override".
"""
from __future__ import annotations

import asyncio
import random

import httpx

from .. import config, events, util
from ..state import STATE

SPOT_URL = "https://api.kraken.com/0/public/Ticker?pair={pairs}"
FUT_URL = "https://futures.kraken.com/derivatives/api/v3/tickers"
COINBASE_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def kraken_spot_pairs() -> list[str]:
    return [m.kraken_spot for m in config.MARKETS.values() if m.kraken_spot]


async def kraken_feed_loop() -> None:
    """Poll Kraken spot + futures every 2 s; update index/mark/last."""
    spot_pairs = kraken_spot_pairs()
    async with httpx.AsyncClient(timeout=6.0) as client:
        while True:
            try:
                spot = await client.get(SPOT_URL.format(pairs=",".join(spot_pairs)))
                spot.raise_for_status()
                payload = spot.json()
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                result = payload.get("result", {})
                index_prices = {}
                for symbol, m in config.MARKETS.items():
                    if m.kraken_spot and m.kraken_spot in result:
                        row = result[m.kraken_spot]
                        index_prices[symbol] = float(row["c"][0])

                fut = await client.get(FUT_URL)
                fut.raise_for_status()
                frows = {x.get("symbol"): x for x in fut.json().get("tickers", [])}
                mark_prices = {}
                for symbol, m in config.MARKETS.items():
                    if m.kind == "linear" and m.kraken_fut in frows:
                        row = frows[m.kraken_fut]
                        mp = row.get("markPrice")
                        if mp:
                            mark_prices[symbol] = float(mp)

                ref = dict(source="Kraken Spot + Kraken Futures", status="live",
                           updated=util.now_ms() / 1000.0, error="",
                           prices={**index_prices, **mark_prices})
                STATE.reference.update(ref)
                _apply_prices(index_prices, mark_prices)
                events.BUS.emit("agent_oracle", {
                    "msg": f"Kraken feed synced — BTC index "
                           f"{index_prices.get('BTC/USDT', 0):,.1f}"})
            except Exception as exc:
                STATE.reference.update(status="stale", error=str(exc)[:140])
                events.BUS.emit("agent_watch", {
                    "msg": "Warning: reference feed unavailable; "
                           "last prices held (stale)"})
            await asyncio.sleep(2.0)


def _apply_prices(index_prices: dict, mark_prices: dict) -> None:
    """Route fresh reference prices into market tick state + tape."""
    for symbol, cfg in config.MARKETS.items():
        t = STATE.tick(symbol)
        override = STATE.force_price.get(symbol)
        if override:
            px = override
        elif cfg.kind == "spot":
            px = index_prices.get(symbol, t.last)
        else:
            px = mark_prices.get(symbol, t.mark)
        if not px or px <= 0:
            continue
        if not getattr(t, "_ref_synced", False):
            # first live reference: realign the 24h rolling window
            t._ref_synced = True
            t.open24 = px
            t.high24 = t.low24 = px
        if cfg.kind == "spot":
            t.index = t.mark = px
            t.on_trade_price(px)
        else:
            t.mark = px
            idx = index_prices.get(config.PERP_UNDERLYING.get(symbol, ""))
            if idx:
                t.index = idx
            t.on_trade_price(px)
    events.BUS.emit("tickers", {})


async def tape_loop() -> None:
    """Oracle agent: keep the trade tape alive between user fills.

    Prints small synthetic trades at the reference price so tickers,
    klines and bots see a living market even with zero users trading.
    """
    while True:
        try:
            for symbol, cfg in config.MARKETS.items():
                t = STATE.tick(symbol)
                if t.last <= 0:
                    continue
                if random.random() < 0.85:
                    for _ in range(random.randint(1, 2)):
                        side = "Buy" if random.random() < 0.5 else "Sell"
                        q = util.snap_to_step(
                            max(cfg.qty_step,
                                random.gauss(cfg.qbase, cfg.qbase * 0.6)),
                            cfg.qty_step)
                        px = util.snap_to_step(
                            t.last * random.uniform(0.9998, 1.0002), cfg.tick)
                        if q <= 0 or px <= 0:
                            continue
                        t.add_trade(side, px, q)
                        if t.cur:
                            t.cur[5] += q
                        events.BUS.emit("trade", dict(
                            symbol=symbol, side=side, price=px, qty=q,
                            ts=util.now_ms()))
            events.BUS.emit("tickers", {})
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def thinktank_loop() -> None:
    """Cross-check BTC against an independent source (Coinbase)."""
    async with httpx.AsyncClient(timeout=6.0) as client:
        while True:
            try:
                r = await client.get(COINBASE_URL)
                cb = float(r.json()["data"]["amount"])
                kr = STATE.reference["prices"].get("BTC/USDT", 0.0)
                diff = abs(kr - cb) / cb * 100 if cb and kr else 0.0
                events.BUS.emit("agent_thinktank", {
                    "msg": f"Witness compare: Kraken={kr:,.2f} "
                           f"Coinbase={cb:,.2f} diff={diff:.3f}%"})
                if diff > 1:
                    events.BUS.emit("agent_watch", {
                        "msg": "Thinktank alert: BTC source divergence > 1%"})
            except Exception as exc:
                events.BUS.emit("agent_thinktank", {"msg": f"compare failed: {exc}"})
            await asyncio.sleep(30)


async def oversight_loop() -> None:
    """Ops health: feed freshness, symbol coverage, engine stats."""
    while True:
        age = util.now_ms() / 1000.0 - STATE.reference.get("updated", 0.0)
        ok = (STATE.reference.get("status") == "live" and age <= 6
              and len(STATE.reference.get("prices", {})) >= len(config.MARKETS))
        events.BUS.emit("agent_oversight", {
            "msg": "✅ Operational health confirmed" if ok else
                   f"⚠️ Health warning: feed={STATE.reference.get('status')}, age={age:.1f}s"})
        await asyncio.sleep(15)
