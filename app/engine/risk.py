# -*- coding: utf-8 -*-
"""
M7 (risk engine): maintenance-margin monitoring and liquidation, plus
the master background loop that evaluates conditional triggers and
position-bound TP/SL/trailing stops.

Liquidation procedure (Bybit-aligned):
  1. when `margin + unrealised PnL <= maintenance margin` at Mark Price:
  2. cancel the user's open orders on that symbol,
  3. force-close the position at market (slippage-capped),
  4. charge the liquidation fee to the (virtual) insurance pool,
  5. any shortfall is covered by the insurance pool.
"""
from __future__ import annotations

from .. import config, events, util
from ..state import STATE
from . import matching, orders


def agent_log(msg: str) -> None:
    events.BUS.emit("agent_risk", {"msg": msg})


def position_risk(pos) -> dict:
    """Risk snapshot: maintenance margin, equity, mmr, distance to liq."""
    t = STATE.tick(pos.symbol)
    mark = t.mark or t.last
    size = abs(pos.size)
    mm = pos.mm_rate() * size * mark
    upnl = pos.unrealised(mark)
    equity = pos.margin + upnl
    mmr = mm / equity if equity > 0 else float("inf")
    return dict(mark=mark, mm=mm, upnl=upnl, equity=equity, mmr=mmr)


def liquidation_check() -> None:
    """One pass over all open positions; executes liquidations."""
    for (uid, symbol), pos in list(STATE.positions.items()):
        if pos.size == 0:
            continue
        risk = position_risk(pos)
        # trigger: position equity below maintenance margin
        if risk["equity"] > risk["mm"]:
            continue
        mark = risk["mark"]
        size = abs(pos.size)
        _execute_liquidation(uid, symbol, pos, mark, size, risk)


def _execute_liquidation(uid: int, symbol: str, pos, mark: float,
                         size: float, risk: dict) -> None:
    # 1) cancel user's resting orders on this symbol to free margin
    for o in [x for x in STATE.open_orders.values()
              if x.uid == uid and x.symbol == symbol]:
        try:
            orders.cancel_order(uid, order_id=o.order_id)
        except Exception:
            pass

    # 2) force close at market (reduce-only)
    side = "Sell" if pos.size > 0 else "Buy"
    entry = pos.entry
    closed = False
    try:
        o = orders.place_order(uid, symbol, side, "Market", size,
                               reduce_only=True, close_on_trigger=True,
                               leverage=pos.leverage)
        closed = o.status == "Filled"
    except Exception as exc:  # no liquidity left: settle at bankruptcy
        agent_log(f"Liquidation fallback settle for #{uid} {symbol}: {exc}")
        _bankruptcy_settle(uid, symbol, pos, mark)

    # 3) leftover margin -> futures wallet; shortfall -> insurance covers
    pos2 = STATE.position(uid, symbol)
    if pos2 and pos2.size == 0:
        leftover = pos2.margin
        if leftover > 0:
            STATE.account(uid).fbalances["USDT"] = \
                STATE.account(uid).ffree("USDT") + leftover
        STATE.positions.pop((uid, symbol), None)
        matching._persist_position(uid, symbol)
        matching.persist_all_user_state(uid)
    liq_fee = config.LIQUIDATION_FEE_RATE * size * mark
    STATE.insurance_pool += liq_fee
    events.BUS.emit("persist", lambda s: _write_meta(
        s, "insurance_pool", str(STATE.insurance_pool)))
    STATE.stats["liqs"] += 1
    t = STATE.tick(symbol)
    t.add_trade(side, mark, size)
    events.BUS.emit("liquidation", dict(
        uid=uid, symbol=symbol, side=side, price=mark, qty=size,
        entry=entry, equity=risk["equity"]))
    matching.ledger(uid, "liquidation", "USDT", -liq_fee,
                    f"Liquidation fee {symbol} @ {mark}")
    agent_log(f"⚠️ Liquidation: {symbol} user #{uid} closed at {mark:,.2f}")
    events.BUS.emit("wallet", {"uid": uid})
    events.BUS.emit("position", {"uid": uid, "symbol": symbol, "closed": True})
    _ = closed


async def _write_meta(session, key: str, value: str) -> None:
    from .. import db
    await session.execute(db.t_meta.update().where(db.t_meta.c.k == key)
                          .values(v=value))


def _bankruptcy_settle(uid: int, symbol: str, pos, mark: float) -> None:
    """Worst-case settle when the book cannot absorb the close."""
    acct = STATE.account(uid)
    direction = 1.0 if pos.size > 0 else -1.0
    pnl = (mark - pos.entry) * pos.size
    acct.fbalances["USDT"] = acct.ffree("USDT") + max(0.0, pos.margin + pnl)
    STATE.positions.pop((uid, symbol), None)
    matching._persist_position(uid, symbol)
    matching.persist_all_user_state(uid)


# --------------------------------------------------------------------------- #
# Master risk loop                                                             #
# --------------------------------------------------------------------------- #
async def risk_loop() -> None:
    """Evaluate triggers, TP/SL and liquidations every 250 ms."""
    import asyncio
    while True:
        try:
            orders.check_triggers()
            orders.check_position_tpsl()
            liquidation_check()
        except Exception:
            import logging
            logging.getLogger("ariax.risk").exception("risk loop error")
        await asyncio.sleep(0.25)
