# -*- coding: utf-8 -*-
"""
M6 (matching engine): price-time priority execution + settlement.

The engine is deterministic and synchronous on the event loop:
`execute()` walks the opposite side of the book level-by-level, FIFO
inside each level (strict price-time priority, industry standard).
Every fill settles balances/margins immediately (no post-trade batch),
records an execution, and emits events for WebSocket + persistence.

Settlement math mirrors Bybit linear/spot behaviour:
  * spot  : base/quote exchange, fee in quote (USDT);
  * linear: one-way isolated position, margin escrow, average entry on
            increase, realized PnL on reduce, taker/maker fee in USDT.
"""
from __future__ import annotations

from .. import config, events, util
from ..errors import ApiError
from ..state import Order, STATE
from . import orderbook

EPS = 1e-12


# --------------------------------------------------------------------------- #
# Order snapshots (persist + API friendly)                                     #
# --------------------------------------------------------------------------- #
def order_snapshot(o: Order) -> dict:
    """Immutable view of an order (captured eagerly for async writers)."""
    return dict(
        id=o.id, order_id=o.order_id, uid=o.uid, symbol=o.symbol,
        category=o.category, side=o.side, order_type=o.order_type, tif=o.tif,
        price=o.price, qty=o.qty, filled_qty=o.filled_qty,
        avg_price=o.avg_price, status=o.status, reduce_only=o.reduce_only,
        close_on_trigger=o.close_on_trigger, trigger_price=o.trigger_price,
        trigger_by=o.trigger_by, tp_price=o.tp_price, sl_price=o.sl_price,
        oco_id=o.oco_id, leverage=o.leverage, order_link_id=o.order_link_id,
        canceled_reason=o.canceled_reason, created_ms=o.created_ms,
        updated_ms=o.updated_ms,
    )


def _persist_order(o: Order) -> None:
    if o.uid <= 0:
        return
    snap = order_snapshot(o)
    events.BUS.emit("persist", lambda s, snap=snap: _write_order(s, snap))


async def _write_order(session, snap: dict) -> None:
    from .. import db
    await session.execute(
        db.t_orders.update().where(db.t_orders.c.id == snap["id"]).values(
            filled_qty=snap["filled_qty"], avg_price=snap["avg_price"],
            status=snap["status"], price=snap["price"], qty=snap["qty"],
            tp_price=snap["tp_price"], sl_price=snap["sl_price"],
            canceled_reason=snap["canceled_reason"],
            updated_ms=snap["updated_ms"], order_link_id=snap["order_link_id"],
        ))


def _persist_execution(uid: int, order_id: str, symbol: str, category: str,
                       side: str, px: float, q: float, fee: float,
                       is_maker: bool, exec_type: str) -> None:
    exec_id = util.gen_hex(12)
    ts = util.now_ms()
    events.BUS.emit("persist", lambda s: _write_exec(
        s, exec_id, order_id, uid, symbol, category, side, px, q, fee,
        is_maker, exec_type, ts))
    return None


async def _write_exec(session, exec_id, order_id, uid, symbol, category, side,
                      px, q, fee, is_maker, exec_type, ts) -> None:
    from .. import db
    await session.execute(db.t_executions.insert().values(
        exec_id=exec_id, order_id=order_id, uid=uid, symbol=symbol,
        category=category, side=side, price=px, qty=q, fee=fee,
        is_maker=1 if is_maker else 0, exec_type=exec_type, created_ms=ts))


def _persist_balances(uid: int, assets: list[str]) -> None:
    """Persist BOTH wallet buckets (spot + futures) for the touched assets."""
    acct = STATE.accounts.get(uid)
    if not acct:
        return
    spot_vals = {a: acct.free(a) for a in assets}
    fut_vals = {a: acct.ffree(a) for a in assets}
    events.BUS.emit("persist", lambda s: _write_balances(s, uid, spot_vals,
                                                         fut_vals))


async def _write_balances(session, uid: int, vals: dict, fvals: dict) -> None:
    from .. import db
    for asset, free in vals.items():
        await session.execute(
            db.t_balances.update()
            .where((db.t_balances.c.uid == uid) & (db.t_balances.c.asset == asset))
            .values(free=free))
    for asset, free in fvals.items():
        res = await session.execute(
            db.t_futures_balances.update()
            .where((db.t_futures_balances.c.uid == uid) &
                   (db.t_futures_balances.c.asset == asset))
            .values(free=free))
        if res.rowcount == 0 and free != 0.0:
            await session.execute(db.t_futures_balances.insert().values(
                uid=uid, asset=asset, free=free))


def _persist_position(uid: int, symbol: str) -> None:
    pos = STATE.positions.get((uid, symbol))
    ts = util.now_ms()
    if pos is None:
        events.BUS.emit("persist", lambda s: _delete_position(s, uid, symbol))
        return
    snap = dict(size=pos.size, entry=pos.entry, leverage=pos.leverage,
                margin=pos.margin, tp=pos.tp, sl=pos.sl,
                trailing=pos.trailing, updated_ms=ts)
    events.BUS.emit("persist", lambda s: _write_position(s, uid, symbol, snap))


async def _write_position(session, uid, symbol, snap) -> None:
    from .. import db
    q = db.t_positions.update().where(
        (db.t_positions.c.uid == uid) & (db.t_positions.c.symbol == symbol))
    result = await session.execute(q.values(**snap))
    if result.rowcount == 0:
        await session.execute(db.t_positions.insert().values(
            uid=uid, symbol=symbol, **snap))


async def _delete_position(session, uid, symbol) -> None:
    from .. import db
    await session.execute(db.t_positions.delete().where(
        (db.t_positions.c.uid == uid) & (db.t_positions.c.symbol == symbol)))


def ledger(uid: int, typ: str, asset: str, amount: float, note: str) -> None:
    """Append a wallet ledger row (write-behind)."""
    ts = util.now_ms()
    events.BUS.emit("persist", lambda s: _write_ledger(
        s, uid, typ, asset, amount, note, ts))


async def _write_ledger(session, uid, typ, asset, amount, note, ts) -> None:
    from .. import db
    await session.execute(db.t_ledger.insert().values(
        uid=uid, type=typ, asset=asset, amount=amount, note=note, ts_ms=ts))


def persist_all_user_state(uid: int) -> None:
    """Snapshot wallet + positions for a user (used after volatile ops)."""
    acct = STATE.accounts.get(uid)
    assets = list((acct.balances if acct else {}).keys()) or ["USDT"]
    _persist_balances(uid, assets)
    for (u, s) in list(STATE.positions):
        if u == uid:
            _persist_position(uid, s)


# --------------------------------------------------------------------------- #
# Settlement primitives                                                        #
# --------------------------------------------------------------------------- #
def _unit_est(o: Order) -> float:
    """Per-unit quote amount reserved while this order rests/executes."""
    maker_r, taker_r = config.fees_for(config.MARKETS[o.symbol].kind)
    if o.category == "spot":
        if o.side == "Buy":
            return o.est_price * (1 + taker_r)
        return 0.0  # sell reserves base units instead
    # linear: margin estimate (5% buffer) + worst-case taker fee
    lev = max(1, o.leverage or 10)
    return o.est_price / lev * 1.05 + o.est_price * taker_r


def release_est_hold(o: Order, qty: float) -> None:
    """Release the reserved estimate for `qty` filled/cancelled units."""
    if o.uid <= 0:
        return
    acct = STATE.accounts.get(o.uid)
    if not acct:
        return
    if o.category == "spot":
        if o.side == "Sell":
            acct.release_hold(o.id, config.MARKETS[o.symbol].base, qty,
                              bucket="spot")
        else:
            acct.release_hold(o.id, "USDT", _unit_est(o) * qty,
                              bucket="spot")
    else:
        acct.release_hold(o.id, "USDT", _unit_est(o) * qty,
                          bucket="futures")


def settle_fill(o: Order, px: float, q: float, fee_rate: float,
                is_taker: bool) -> None:
    """Apply one fill to the owner's wallet / position."""
    if o.uid <= 0:
        return  # agent liquidity (market maker): no wallet
    cfg = config.MARKETS[o.symbol]
    fee = px * q * fee_rate
    release_est_hold(o, q)
    if o.category == "spot":
        _settle_spot(o, cfg, px, q, fee)
    else:
        _settle_linear(o, cfg, px, q, fee)
    _persist_balances(o.uid, ["USDT", cfg.base])
    _persist_execution(o.uid, o.order_id, o.symbol, o.category, o.side,
                       px, q, fee, not is_taker, "Taker" if is_taker else "Maker")
    o.cum_fee += fee
    events.BUS.emit("wallet", {"uid": o.uid})
    events.BUS.emit("execution", {"uid": o.uid, "order": order_snapshot(o),
                                  "px": px, "qty": q, "fee": fee,
                                  "is_taker": is_taker})


def _settle_spot(o: Order, cfg, px: float, q: float, fee: float) -> None:
    acct = STATE.account(o.uid)
    base = cfg.base
    if o.side == "Buy":
        acct.balances["USDT"] = acct.free("USDT") - px * q - fee
        acct.balances[base] = acct.free(base) + q
    else:
        acct.balances[base] = acct.free(base) - q
        acct.balances["USDT"] = acct.free("USDT") + px * q - fee


def _settle_linear(o: Order, cfg, px: float, q: float, fee: float) -> None:  # noqa: C901
    uid = o.uid
    acct = STATE.account(uid)
    acct.fbalances["USDT"] = acct.ffree("USDT") - fee
    signed = q if o.side == "Buy" else -q
    key = (uid, o.symbol)
    pos = STATE.positions.get(key)
    closeq = 0.0
    if pos and pos.size != 0 and (pos.size > 0) != (signed > 0):
        size0 = abs(pos.size)
        was_long = pos.size > 0
        closeq = min(size0, q)
        direction = 1.0 if pos.size > 0 else -1.0
        pnl = (px - pos.entry) * closeq * direction
        released = pos.margin * (closeq / size0)
        pos.size -= direction * closeq
        pos.margin -= released
        acct.fbalances["USDT"] = acct.ffree("USDT") + pnl + released
        # ---- attributed costs for this (partial) close ----
        share = closeq / size0
        close_fee = fee * (closeq / max(q, 1e-12))
        entry_fee_share = pos.fees_acc * share
        pos.fees_acc -= entry_fee_share
        funding_share = pos.funding_acc * share
        pos.funding_acc -= funding_share
        partial_close = pos.size != 0
        _record_closed_trade(o, "long" if was_long else "short", pos.entry,
                             px, closeq, pnl, close_fee + entry_fee_share,
                             funding_share, pos.created_ms, partial_close,
                             pos.strategy)
        ledger(uid, "realized_pnl", "USDT", pnl,
               f"Realized PnL {o.symbol} {'long' if direction > 0 else 'short'} "
               f"qty={closeq:.8g} entry={pos.entry:.8g} exit={px:.8g} "
               f"fee={close_fee + entry_fee_share:.6f} "
               f"funding={funding_share:.6f} "
               f"net={pnl - close_fee - entry_fee_share - funding_share:.6f}")
        events.BUS.emit("pnl", {"uid": uid, "symbol": o.symbol, "pnl": pnl})
        if abs(pos.size) < 1e-9:
            pos.size = 0.0
            STATE.positions.pop(key, None)
            STATE.close_locks.pop(key, None)
            events.BUS.emit("position", {"uid": uid, "symbol": o.symbol,
                                         "closed": True})
    remq = q - closeq
    if remq > 1e-12:
        pos = STATE.get_or_init_position(uid, o.symbol)
        lev = max(1, o.leverage or STATE.leverage_for(uid, o.symbol))
        add_margin = px * remq / lev
        new_size = pos.size + (remq if signed > 0 else -remq)
        pos.entry = (pos.entry * abs(pos.size) + px * remq) / abs(new_size)
        pos.size = new_size
        pos.leverage = lev
        pos.margin += add_margin
        acct.fbalances["USDT"] = acct.ffree("USDT") - add_margin
        # attribute the opening share of this fill's fee to the position
        pos.fees_acc += fee * (remq / max(q, 1e-12))
        if o.strategy and not pos.strategy:
            pos.strategy = o.strategy[:40]
        # Entry-attached TP/SL (Bybit tpslMode=Full equivalent)
        if o.tp_price and (pos.tp is None or o.tp_price != pos.tp):
            pos.tp = o.tp_price
        if o.sl_price and (pos.sl is None or o.sl_price != pos.sl):
            pos.sl = o.sl_price
    _persist_position(uid, o.symbol)


def _record_closed_trade(o: Order, side: str, entry: float, exit_px: float,
                         qty: float, gross: float, fees: float,
                         funding: float, opened_ms: int, partial: bool,
                         strategy: str) -> None:
    """Persist one structured (partial) close row — the bot-debugging report."""
    if o.uid <= 0:
        return
    row = dict(
        uid=o.uid, symbol=o.symbol, side=side, qty=qty, entry=entry,
        exit=exit_px, gross_pnl=gross, fees=fees, funding=funding,
        net_pnl=gross - fees - funding,
        hold_seconds=max(0.0, (util.now_ms() - opened_ms) / 1000.0),
        close_reason=(o.close_reason or "manual")[:24],
        strategy=(strategy or "")[:40], partial=1 if partial else 0,
        ts_ms=util.now_ms(),
    )
    events.BUS.emit("persist", lambda s: _write_closed_trade(s, row))
    events.BUS.emit("trade_closed", {"uid": o.uid, "row": row})


async def _write_closed_trade(session, row: dict) -> None:
    from .. import db
    await session.execute(db.t_closed_trades.insert().values(**row))


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #
def apply_fill(taker: Order, maker: Order, px: float, q: float) -> None:
    """Execute one taker-maker match and update both sides."""
    maker_rate, taker_rate = config.fees_for(
        config.MARKETS[taker.symbol].kind)
    ts = util.now_ms()
    tick = STATE.tick(taker.symbol)
    tick.add_trade(taker.side, px, q)
    tick.on_trade_price(px)
    STATE.stats["fills"] += 1

    for o, is_taker in ((taker, True), (maker, False)):
        o.filled_qty += q
        if o.leaves > EPS:
            o.status = "PartiallyFilled"
        else:
            o.status = "Filled"
        if o.avg_price <= 0:
            o.avg_price = px
        else:
            o.avg_price = (o.avg_price * (o.filled_qty - q) + px * q) / o.filled_qty
        o.updated_ms = ts
    settle_fill(taker, px, q, taker_rate, True)
    settle_fill(maker, px, q, maker_rate, False)
    events.BUS.emit("fill", dict(uid=taker.uid if taker.uid > 0 else maker.uid,
                                 symbol=taker.symbol,
                                 side=taker.side.lower(), price=px, qty=q))
    events.BUS.emit("trade", dict(symbol=taker.symbol, side=taker.side,
                                  price=px, qty=q, ts=ts))
    events.BUS.emit("order", {"uid": taker.uid, "order": order_snapshot(taker)})
    events.BUS.emit("order", {"uid": maker.uid, "order": order_snapshot(maker)})
    if taker.uid > 0:
        _persist_order(taker)
    if maker.uid > 0:
        _persist_order(maker)


def execute(o: Order) -> Order:
    """Match an active order against the book (price-time priority).

    Returns the order in its terminal or resting state:
      * Limit (GTC) leftovers rest on the book;
      * Market / IOC leftovers are cancelled;
      * FOK is all-or-nothing (pre-checked in orders.place_order).
    """
    book = orderbook.book(o.symbol)
    cap = o.mkt_cap if o.order_type == "Market" else o.price
    opp = book.opposite(o.side)
    while o.leaves > EPS:
        bp = book.opposite_best(o.side)
        if bp is None:
            break
        crosses_cap = (o.side == "Buy" and bp <= cap + EPS) or \
                      (o.side == "Sell" and bp >= cap - EPS)
        if not crosses_cap:
            break
        dq = opp[bp]
        while dq and o.leaves > EPS:
            maker = dq[0]
            q = min(o.leaves, maker.leaves)
            apply_fill(o, maker, bp, q)
            if maker.leaves <= EPS:
                dq.popleft()
                if maker.uid > 0:
                    STATE.open_orders.pop(maker.id, None)
        if not dq:
            opp.pop(bp, None)
    leftover = o.leaves
    if leftover > EPS and o.order_type == "Limit" and o.tif in ("GTC", "PostOnly"):
        orderbook.book(o.symbol).add(o)
        if o.uid > 0:
            STATE.open_orders[o.id] = o
        o.status = "New" if o.filled_qty <= EPS else "PartiallyFilled"
        o.updated_ms = util.now_ms()
        if o.uid > 0:
            _persist_order(o)
            events.BUS.emit("order", {"uid": o.uid, "order": order_snapshot(o)})
    elif leftover > EPS:
        # Market/IOC remainder: cancel and release reservations.
        release_est_hold(o, leftover)
        _unlock_close_qty(o, leftover)
        o.status = "Cancelled"
        o.canceled_reason = "IOC/Market unfilled remainder"
        o.updated_ms = util.now_ms()
        if o.uid > 0:
            _persist_order(o)
            events.BUS.emit("order", {"uid": o.uid, "order": order_snapshot(o)})
    else:
        o.status = "Filled"
        o.updated_ms = util.now_ms()
    return o


def _unlock_close_qty(o: Order, qty: float) -> None:
    key = (o.uid, o.symbol)
    cur = STATE.close_locks.get(key, 0.0)
    STATE.close_locks[key] = max(0.0, cur - qty)
    if STATE.close_locks[key] <= EPS:
        STATE.close_locks.pop(key, None)
