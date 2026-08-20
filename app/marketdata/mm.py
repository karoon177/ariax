# -*- coding: utf-8 -*-
"""
M5/M8 (liquidity): market-maker agent.

Quotes 7 levels per side around the reference mid every 2 s on every
symbol, mirroring Bybit testnet depth behaviour. Quotes are PostOnly and
never cross the book. `STATE.mm_intensity` (stress suite) scales quote
size to simulate volume bursts.
"""
from __future__ import annotations

import asyncio
import random

from .. import config, events, util
from ..state import Order, STATE
from ..engine import orderbook


def agent_quotes() -> list[Order]:
    """Snapshot of the MM's current resting quotes (uid=0)."""
    return [o for o in STATE.open_orders.values() if o.uid == 0]


def cancel_all_quotes() -> None:
    """Pull every MM quote (liquidity dries up when the agent is off)."""
    for o in agent_quotes():
        orderbook.book(o.symbol).remove(o)
        STATE.open_orders.pop(o.id, None)


def refresh_quotes() -> int:
    """Re-quote every market; returns the number of levels quoted."""
    cancel_all_quotes()
    count = 0
    for symbol, cfg in config.MARKETS.items():
        t = STATE.tick(symbol)
        if t.last <= 0:
            continue
        mid = t.mark or t.last
        spread = cfg.tick * random.uniform(2, 4)
        book = orderbook.book(symbol)
        for i in range(7):
            for side, sgn in (("Buy", -1), ("Sell", 1)):
                px = util.snap_to_step(
                    mid + sgn * spread * (i + 1) * random.uniform(0.9, 1.15),
                    cfg.tick)
                if px <= 0:
                    continue
                qty = util.snap_to_step(
                    max(cfg.qty_step, cfg.qbase * random.uniform(0.15, 1.2)
                        * STATE.mm_intensity * _decay(i)),
                    cfg.qty_step)
                if qty <= 0:
                    continue
                # never cross existing levels
                best = book.opposite_best(side)
                if best is not None and (
                        (side == "Buy" and px >= best) or (side == "Sell" and px <= best)):
                    continue
                STATE.order_seq += 1
                o = Order(
                    uid=0, symbol=symbol, category=cfg.kind, side=side,
                    order_type="Limit", qty=qty, price=px, tif="PostOnly",
                    id=STATE.order_seq, order_id=f"agent-{STATE.order_seq}",
                    status="New", created_ms=util.now_ms(), is_agent=True,
                    est_price=px,
                )
                book.add(o)
                STATE.open_orders[o.id] = o
                count += 1
    return count


def _decay(level: int) -> float:
    """Exponential size decay away from mid (realistic depth profile)."""
    import math
    return math.exp(-level * 0.22)


async def mm_loop() -> None:
    """Re-quote cycle; disabled when the MM agent is toggled off."""
    while True:
        try:
            if STATE.agents.get("mm", {}).get("enabled", True):
                n = refresh_quotes()
                events.BUS.emit("agent_mm", {
                    "msg": f"Quoted {n} levels across {len(config.MARKETS)} markets"})
            else:
                cancel_all_quotes()
        except Exception:
            import logging
            logging.getLogger("ariax.mm").exception("mm loop error")
        await asyncio.sleep(2.0)
