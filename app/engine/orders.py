# -*- coding: utf-8 -*-
r"""
M4 (order management system): validation, placement, conditional orders,
TP/SL (attached & positional), OCO linking, amend, cancel.

Implements the Bybit v5 order lifecycle:
  Created -> (Untriggered -> Triggered)? -> New/PartiallyFilled -> Filled
                                         \-> Cancelled/Deactivated/Rejected

Conditional orders (`orderFilter=StopOrder` equivalent: trigger_price set)
are stored in `STATE.conditional` and armed by `check_triggers()` against
LastPrice / MarkPrice / IndexPrice. Position-bound TP/SL (trading-stop)
live on the Position itself and are evaluated by the risk loop.
"""
from __future__ import annotations

from .. import config, events, util
from ..errors import (ApiError, E_DUPLICATE_LINK_ID, E_INSUFFICIENT_BALANCE,
                      E_INVALID_QTY, E_INVALID_SIDE, E_ORDER_NOT_FOUND,
                      E_PARAM, E_PRICE_DEVIATION, E_QTY_EXCEEDS_POSITION,
                      E_SYMBOL_INVALID)
from ..state import Order, STATE
from . import matching, orderbook

EPS = 1e-12
VALID_TIF = ("GTC", "IOC", "FOK", "PostOnly")
TRIGGER_SOURCES = ("LastPrice", "MarkPrice", "IndexPrice")


def _flag(msg: str) -> None:
    """Feed the fraud-watch AI agent (kept from v1)."""
    STATE.stats["flags"] += 1
    events.BUS.emit("agent_watch", {"msg": msg})


# --------------------------------------------------------------------------- #
# Placement                                                                    #
# --------------------------------------------------------------------------- #
def place_order(
    uid: int,
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    price: float | None = None,
    tif: str = "GTC",
    reduce_only: bool = False,
    close_on_trigger: bool = False,
    trigger_price: float | None = None,
    trigger_by: str = "LastPrice",
    tp: float | None = None,
    sl: float | None = None,
    tpsl_mode: str = "Full",
    leverage: int | None = None,
    order_link_id: str | None = None,
    is_agent: bool = False,
) -> Order:
    """Validate and submit an order. Raises ApiError on rejection."""
    cfg = config.MARKETS.get(symbol)
    if cfg is None:
        raise ApiError(E_SYMBOL_INVALID, f"symbol {symbol} not found")
    side = side.capitalize()
    order_type = order_type.capitalize()
    if side not in ("Buy", "Sell"):
        raise ApiError(E_INVALID_SIDE, "side must be Buy or Sell")
    if order_type not in ("Limit", "Market"):
        raise ApiError(E_PARAM, "orderType must be Limit or Market")
    if tif not in VALID_TIF:
        raise ApiError(E_PARAM, f"timeInForce must be one of {VALID_TIF}")
    if trigger_by not in TRIGGER_SOURCES:
        raise ApiError(E_PARAM, f"triggerBy must be one of {TRIGGER_SOURCES}")

    # ---- quantity / price normalisation -------------------------------- #
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        raise ApiError(E_INVALID_QTY, "qty is not a number")
    if qty != qty or qty <= 0:  # NaN guard
        raise ApiError(E_INVALID_QTY, "qty must be positive")
    qty = util.snap_to_step(qty, cfg.qty_step)
    if qty < cfg.min_qty:
        raise ApiError(E_INVALID_QTY, f"qty below minimum {cfg.min_qty}")
    if order_type == "Limit":
        try:
            price = float(price or 0)
        except (TypeError, ValueError):
            raise ApiError(E_PARAM, "price is not a number")
        if price <= 0:
            raise ApiError(E_PARAM, "price required for Limit orders")
        price = util.snap_to_step(price, cfg.tick)
    else:
        price = 0.0

    tick_state = STATE.tick(symbol)
    book = orderbook.book(symbol)
    ref_price = tick_state.mark or tick_state.last or cfg.seed_price

    # ---- price guardrails (watch agent thresholds from v1) -------------- #
    if order_type == "Limit" and uid > 0:
        dev = abs(price - ref_price) / ref_price
        if dev > config.PRICE_DEVIATION_REJECT:
            raise ApiError(E_PRICE_DEVIATION,
                           "price deviates more than 20% from reference")
        if dev > config.PRICE_DEVIATION_FLAG:
            _flag(f"Suspicious order: {side} {qty} {symbol} @ {price} "
                  f"(deviation {dev:.1%}) flagged")
        notional = price * qty
    elif order_type == "Limit":
        notional = price * qty
    else:
        best = book.opposite_best(side)
        ref = best or ref_price
        est_price = ref * (1.05 if side == "Buy" else 0.95)
        notional = est_price * qty
    if notional > config.MAX_ORDER_NOTIONAL and uid > 0:
        _flag(f"Whale detected: {notional:,.0f} USD order on {symbol}")
        raise ApiError(E_PARAM, "order notional exceeds risk cap 2,000,000 USD")
    if uid > 0 and notional < cfg.min_notional:
        raise ApiError(E_PARAM, f"notional below minimum {cfg.min_notional} USD")

    # ---- leverage & risk-limit tier ------------------------------------- #
    if cfg.kind == "linear" and uid > 0:
        if leverage is None:
            leverage = STATE.leverage_for(uid, symbol)
        leverage = int(max(1, min(leverage, cfg.max_lev)))
        max_notional, tier_max_lev, _ = config.tier_for_notional(cfg, notional)
        if leverage > tier_max_lev:
            raise ApiError(E_PARAM,
                           f"leverage {leverage} exceeds tier max {tier_max_lev} "
                           f"for notional {notional:,.0f}")
    else:
        leverage = 1 if cfg.kind == "spot" else (leverage or 10)

    # ---- duplicate orderLinkId ------------------------------------------ #
    if uid > 0 and order_link_id:
        if (uid, order_link_id) in STATE.orders_by_link:
            raise ApiError(E_DUPLICATE_LINK_ID,
                           f"orderLinkId {order_link_id} already exists")

    # ---- build order ----------------------------------------------------- #
    STATE.order_seq += 1
    ts = util.now_ms()
    o = Order(
        uid=uid, symbol=symbol, category=cfg.kind, side=side,
        order_type=order_type, qty=qty, price=price, tif=tif,
        reduce_only=reduce_only, close_on_trigger=close_on_trigger,
        trigger_price=trigger_price, trigger_by=trigger_by,
        tp_price=tp, sl_price=sl, leverage=leverage,
        order_link_id=order_link_id, id=STATE.order_seq,
        order_id=util.gen_hex(16) if uid > 0 else f"agent-{STATE.order_seq}",
        status="Created", created_ms=ts, updated_ms=ts, is_agent=is_agent,
    )
    if order_type == "Market":
        o.mkt_cap = est_price if uid > 0 else float("inf")
        o.est_price = est_price if uid > 0 else ref
    else:
        o.est_price = price
    if uid > 0:
        STATE.orders_by_link[(uid, order_link_id)] = o.id
    STATE.stats["orders"] += 1

    # ---- conditional arm-and-wait ---------------------------------------- #
    if trigger_price is not None:
        o.status = "Untriggered"
        STATE.conditional[o.id] = o
        if uid > 0:
            _persist_new_order(o)
            events.BUS.emit("order", {"uid": uid, "order": matching.order_snapshot(o)})
        return o

    # ---- balance / position checks & reservation ------------------------- #
    if uid > 0:
        _reserve(o)
    elif not is_agent:
        raise ApiError(E_PARAM, "invalid caller")

    # ---- FOK / PostOnly pre-checks --------------------------------------- #
    if tif == "FOK":
        need = o.qty
        have = book.liquidity_at(o.side, o.price if o.order_type == "Limit" else o.mkt_cap)
        if have + EPS < need:
            _release_all(o)
            o.status = "Cancelled"
            o.canceled_reason = "FOK not fillable"
            if uid > 0:
                _persist_new_order(o)
                events.BUS.emit("order", {"uid": uid, "order": matching.order_snapshot(o)})
            return o
    if tif == "PostOnly" and uid > 0:
        best = book.opposite_best(o.side)
        if best is not None and ((o.side == "Buy" and best <= o.price) or
                                 (o.side == "Sell" and best >= o.price)):
            _release_all(o)
            raise ApiError(E_PARAM, "PostOnly order would cross the book")

    if uid > 0:
        _persist_new_order(o)
    matching.execute(o)
    if uid > 0:
        events.BUS.emit("order", {"uid": uid, "order": matching.order_snapshot(o)})
    return o


def _persist_new_order(o: Order) -> None:
    snap = matching.order_snapshot(o)
    events.BUS.emit("persist", lambda s: _write_new_order(s, snap))


async def _write_new_order(session, snap: dict) -> None:
    from .. import db
    await session.execute(db.t_orders.insert().values(
        id=snap["id"], order_id=snap["order_id"], uid=snap["uid"],
        symbol=snap["symbol"], category=snap["category"], side=snap["side"],
        order_type=snap["order_type"], tif=snap["tif"], price=snap["price"],
        qty=snap["qty"], filled_qty=snap["filled_qty"],
        avg_price=snap["avg_price"], status=snap["status"],
        reduce_only=int(snap["reduce_only"]),
        close_on_trigger=int(snap["close_on_trigger"]),
        trigger_price=snap["trigger_price"], trigger_by=snap["trigger_by"],
        tp_price=snap["tp_price"], sl_price=snap["sl_price"],
        oco_id=snap["oco_id"], leverage=snap["leverage"],
        order_link_id=snap["order_link_id"],
        canceled_reason=snap["canceled_reason"],
        created_ms=snap["created_ms"], updated_ms=snap["updated_ms"]))


# --------------------------------------------------------------------------- #
# Reservations                                                                 #
# --------------------------------------------------------------------------- #
def _reserve(o: Order) -> None:
    """Reserve balances for an order from the correct wallet bucket.

    Linear orders draw margin/fees from the FUTURES wallet; when it runs
    short, a transparent auto-bridge tops it up from the SPOT wallet
    (keeps UTA-style bots working without manual transfers).
    """
    cfg = config.MARKETS[o.symbol]
    acct = STATE.account(o.uid)
    if o.category == "spot":
        if o.side == "Buy":
            need = o.est_price * o.qty * (1 + config.SPOT_TAKER_FEE)
            if acct.available("USDT") < need:
                raise ApiError(E_INSUFFICIENT_BALANCE,
                               "insufficient USDT balance in Spot wallet")
            acct.add_hold(o.id, "USDT", need, bucket="spot")
        else:
            if acct.available(cfg.base) < o.qty:
                raise ApiError(E_INSUFFICIENT_BALANCE,
                               f"insufficient {cfg.base} balance")
            acct.add_hold(o.id, cfg.base, o.qty, bucket="spot")
        return
    # linear (FUTURES wallet with spot auto-bridge)
    pos = STATE.position(o.uid, o.symbol)
    opening = not pos or pos.size == 0 or (pos.size > 0) == (o.side == "Buy")
    if opening and not o.reduce_only:
        lev = o.leverage
        need_margin = o.est_price * o.qty / lev * 1.05
        need_fee = o.est_price * o.qty * config.LINEAR_TAKER_FEE
        shortfall = (need_margin + need_fee) - STATE.free_margin(o.uid)
        if shortfall > 1e-9:
            moved = _auto_bridge(o.uid, shortfall)
            if STATE.free_margin(o.uid) < need_margin + need_fee - 1e-9:
                raise ApiError(
                    E_INSUFFICIENT_BALANCE,
                    "insufficient futures margin (transfer funds to the "
                    "Futures wallet via /v5/asset/transfer/inter-transfer "
                    "or the wallet page)")
        acct.add_hold(o.id, "USDT", need_margin + need_fee, bucket="futures")
    else:
        avail_size = abs(pos.size) - STATE.close_locks.get((o.uid, o.symbol), 0.0) \
            if pos else 0.0
        if o.qty > avail_size + 1e-9:
            raise ApiError(E_QTY_EXCEEDS_POSITION, "qty exceeds position size")
        STATE.close_locks[(o.uid, o.symbol)] = \
            STATE.close_locks.get((o.uid, o.symbol), 0.0) + o.qty


def _auto_bridge(uid: int, need: float) -> float:
    """Move `need` USDT spot→futures when possible; ledger-recorded."""
    acct = STATE.account(uid)
    movable = min(need, acct.available("USDT"))
    if movable <= 1e-9:
        return 0.0
    moved = acct.transfer(movable, "USDT", "spot_to_futures")
    if moved > 0:
        matching.ledger(uid, "auto_transfer", "USDT", moved,
                        f"Auto-bridge spot→futures {moved:.4f} USDT")
        _persist_balances_fn(uid)
    return moved


def _persist_balances_fn(uid: int) -> None:
    from . import matching
    acct = STATE.accounts.get(uid)
    assets = list(set(list((acct.balances if acct else {}).keys()) +
                      list((acct.fbalances if acct else {}).keys()))) or ["USDT"]
    matching._persist_balances(uid, assets)


def _release_all(o: Order) -> None:
    """Release every reservation still held by an order (cancel/kill)."""
    acct = STATE.accounts.get(o.uid)
    if acct:
        acct.clear_holds(o.id)
    _unlock(o, o.leaves)


def _unlock(o: Order, qty: float) -> None:
    key = (o.uid, o.symbol)
    cur = STATE.close_locks.get(key, 0.0)
    if cur - qty <= 1e-12:
        STATE.close_locks.pop(key, None)
    else:
        STATE.close_locks[key] = cur - qty


# --------------------------------------------------------------------------- #
# Cancel / amend                                                               #
# --------------------------------------------------------------------------- #
def cancel_order(uid: int, order_id: str | None = None,
                 link_id: str | None = None) -> Order:
    """Cancel an open or untriggered order; returns the cancelled order."""
    o = _find_order(uid, order_id, link_id)
    if o is None:
        raise ApiError(E_ORDER_NOT_FOUND, "order not found")
    if o.status in ("Filled", "Cancelled", "Deactivated"):
        raise ApiError(E_ORDER_NOT_FOUND, f"order already {o.status}")
    if o.id in STATE.conditional:
        STATE.conditional.pop(o.id, None)
        o.status = "Cancelled"
        o.canceled_reason = "Cancelled by user"
    else:
        orderbook.book(o.symbol).remove(o)
        STATE.open_orders.pop(o.id, None)
        matching.release_est_hold(o, o.leaves)
        _unlock(o, o.leaves)
        o.status = "Cancelled"
        o.canceled_reason = "Cancelled by user"
    o.updated_ms = util.now_ms()
    events.BUS.emit("persist", lambda s: _finalize_order(s, o.id, o.status,
                                                         o.canceled_reason,
                                                         o.updated_ms))
    events.BUS.emit("order", {"uid": o.uid, "order": matching.order_snapshot(o)})
    _cancel_oco_sibling(o)
    return o


async def _finalize_order(session, oid: int, status: str, reason: str,
                          updated_ms: int) -> None:
    from .. import db
    await session.execute(db.t_orders.update().where(db.t_orders.c.id == oid)
                          .values(status=status, canceled_reason=reason,
                                  updated_ms=updated_ms))


def cancel_all(uid: int, symbol: str | None = None) -> int:
    """Cancel every open + conditional order of a user (optionally one symbol)."""
    n = 0
    for o in [x for x in list(STATE.open_orders.values())
              if x.uid == uid and (symbol is None or x.symbol == symbol)]:
        cancel_order(uid, order_id=o.order_id)
        n += 1
    for o in [x for x in list(STATE.conditional.values())
              if x.uid == uid and (symbol is None or x.symbol == symbol)]:
        cancel_order(uid, order_id=o.order_id)
        n += 1
    return n


def amend_order(uid: int, order_id: str | None = None, link_id: str | None = None,
                price: float | None = None, qty: float | None = None) -> Order:
    """Amend a resting limit order (Bybit v5 order/amend)."""
    o = _find_order(uid, order_id, link_id)
    if o is None or o.status not in ("New", "PartiallyFilled"):
        raise ApiError(E_ORDER_NOT_FOUND, "amendable order not found")
    cfg = config.MARKETS[o.symbol]
    old_price, old_qty = o.price, o.qty
    if price is not None:
        price = util.snap_to_step(float(price), cfg.tick)
        if price <= 0:
            raise ApiError(E_PARAM, "invalid amend price")
    if qty is not None:
        qty = util.snap_to_step(float(qty), cfg.qty_step)
        if qty <= o.filled_qty:
            raise ApiError(E_PARAM, "amended qty below filled qty")
    # Re-reserve against the new parameters
    _release_all(o)
    orderbook.book(o.symbol).remove(o)
    if price is not None:
        o.price = price
    if qty is not None:
        o.qty = qty
    o.est_price = o.price
    try:
        _reserve(o)
    except ApiError:
        # roll back
        o.price, o.qty, o.est_price = old_price, old_qty, old_price
        _reserve(o)
        orderbook.book(o.symbol).add(o)
        STATE.open_orders[o.id] = o
        raise
    o.updated_ms = util.now_ms()
    orderbook.book(o.symbol).add(o)
    STATE.open_orders[o.id] = o
    events.BUS.emit("persist", lambda s: _amend_write(s, o))
    events.BUS.emit("order", {"uid": o.uid, "order": matching.order_snapshot(o)})
    return o


async def _amend_write(session, o: Order) -> None:
    from .. import db
    await session.execute(db.t_orders.update().where(db.t_orders.c.id == o.id)
                          .values(price=o.price, qty=o.qty, updated_ms=o.updated_ms))


def _find_order(uid: int, order_id: str | None, link_id: str | None) -> Order | None:
    if order_id:
        for o in list(STATE.open_orders.values()) + list(STATE.conditional.values()):
            if o.uid == uid and o.order_id == order_id:
                return o
    if link_id:
        oid = STATE.orders_by_link.get((uid, link_id))
        if oid:
            for o in list(STATE.open_orders.values()) + list(STATE.conditional.values()):
                if o.id == oid:
                    return o
    return None


def _cancel_oco_sibling(o: Order) -> None:
    """OCO: cancel the sibling conditional when one side triggers/cancels."""
    if not o.oco_id:
        return
    for other in list(STATE.conditional.values()):
        if other.oco_id == o.oco_id and other.id != o.id:
            STATE.conditional.pop(other.id, None)
            other.status = "Deactivated"
            other.canceled_reason = "OCO sibling executed"
            other.updated_ms = util.now_ms()
            events.BUS.emit("persist", lambda s, x=other: _finalize_order(
                s, x.id, x.status, x.canceled_reason, x.updated_ms))
            events.BUS.emit("order", {"uid": other.uid,
                                      "order": matching.order_snapshot(other)})


# --------------------------------------------------------------------------- #
# Conditional trigger evaluation (called from the risk loop, ~250 ms)          #
# --------------------------------------------------------------------------- #
def trigger_ref(symbol: str, source: str) -> float:
    t = STATE.tick(symbol)
    if source == "MarkPrice":
        return t.mark
    if source == "IndexPrice":
        return t.index or t.mark
    return t.last or t.mark


def check_triggers() -> None:
    """Arm/execute conditional orders and positional TP/SL + trailing."""
    for o in list(STATE.conditional.values()):
        ref = trigger_ref(o.symbol, o.trigger_by)
        if ref <= 0:
            continue
        hit = (o.side == "Buy" and ref >= o.trigger_price) or \
              (o.side == "Sell" and ref <= o.trigger_price)
        if not hit:
            continue
        # closeOnTrigger conditionals die with the position
        pos = STATE.position(o.uid, o.symbol)
        if (o.reduce_only or o.close_on_trigger) and (not pos or pos.size == 0):
            STATE.conditional.pop(o.id, None)
            o.status = "Deactivated"
            o.canceled_reason = "Position closed"
            events.BUS.emit("persist", lambda s, x=o: _finalize_order(
                s, x.id, x.status, x.canceled_reason, x.updated_ms))
            events.BUS.emit("order", {"uid": o.uid, "order": matching.order_snapshot(o)})
            continue
        STATE.conditional.pop(o.id, None)
        o.status = "Triggered"
        o.updated_ms = util.now_ms()
        _cancel_oco_sibling(o)
        # Re-submit the child as an active order (market or limit at trigger).
        child = _spawn_child(o)
        events.BUS.emit("order", {"uid": o.uid, "order": matching.order_snapshot(o)})
        if child is not None:
            events.BUS.emit("order", {"uid": o.uid,
                                      "order": matching.order_snapshot(child)})


def _spawn_child(parent: Order) -> Order | None:
    """Materialise the market/limit child of a triggered conditional."""
    cfg = config.MARKETS[parent.symbol]
    try:
        child = Order(
            uid=parent.uid, symbol=parent.symbol, category=parent.category,
            side=parent.side,
            order_type=parent.order_type if parent.order_type == "Market" else "Limit",
            qty=parent.qty,
            price=parent.price if parent.order_type == "Limit" else 0.0,
            tif="GTC", reduce_only=parent.reduce_only,
            close_on_trigger=parent.close_on_trigger,
            leverage=parent.leverage, order_link_id=parent.order_link_id,
            tp_price=parent.tp_price, sl_price=parent.sl_price,
        )
        STATE.order_seq += 1
        child.id = STATE.order_seq
        child.order_id = util.gen_hex(16)
        child.created_ms = child.updated_ms = util.now_ms()
        ref = trigger_ref(parent.symbol, parent.trigger_by)
        child.est_price = ref
        child.mkt_cap = ref * (1.05 if child.side == "Buy" else 0.95)
        _reserve(child)
        _persist_new_order(child)
        matching.execute(child)
        return child
    except ApiError as exc:
        parent.canceled_reason = f"Trigger failed: {exc.ret_msg}"
        events.BUS.emit("persist", lambda s: _finalize_order(
            s, parent.id, "Rejected", parent.canceled_reason, util.now_ms()))
        return None


# --------------------------------------------------------------------------- #
# Positional TP / SL / trailing (Bybit position/trading-stop)                  #
# --------------------------------------------------------------------------- #
def set_trading_stop(uid: int, symbol: str, tp: float | None = None,
                     sl: float | None = None,
                     trailing: float | None = None) -> None:
    pos = STATE.position(uid, symbol)
    if not pos or pos.size == 0:
        raise ApiError(110131, "position does not exist")
    if tp is not None:
        pos.tp = float(tp) if tp else None
    if sl is not None:
        pos.sl = float(sl) if sl else None
    if trailing is not None:
        pos.trailing = float(trailing) if trailing else None
        pos.trail_extreme = STATE.tick(symbol).mark
    pos.updated_ms = util.now_ms()
    matching._persist_position(uid, symbol)
    events.BUS.emit("position", {"uid": uid, "symbol": symbol})


def check_position_tpsl() -> None:
    """Evaluate TP/SL/trailing stops bound to open positions."""
    for (uid, symbol), pos in list(STATE.positions.items()):
        if pos.size == 0:
            continue
        mark = STATE.tick(symbol).mark
        if mark <= 0:
            continue
        long = pos.size > 0
        # trailing stop tracks the extreme and fires on retrace
        if pos.trailing:
            extreme = max(pos.trail_extreme, mark) if long else min(pos.trail_extreme, mark)
            pos.trail_extreme = extreme
            retrace = (extreme - mark) if long else (mark - extreme)
            if retrace >= pos.trailing:
                _force_close(uid, symbol, "TrailingStop")
                continue
        if pos.tp and ((long and mark >= pos.tp) or (not long and mark <= pos.tp)):
            if _force_close(uid, symbol, "TakeProfit"):
                continue
        if pos.sl and ((long and mark <= pos.sl) or (not long and mark >= pos.sl)):
            _force_close(uid, symbol, "StopLoss")


def _force_close(uid: int, symbol: str, reason: str) -> bool:
    """Market-close a whole position (TP/SL/trailing/liquidation helper)."""
    pos = STATE.position(uid, symbol)
    if not pos or pos.size == 0:
        return False
    side = "Sell" if pos.size > 0 else "Buy"
    try:
        o = place_order(
            uid, symbol, side, "Market", abs(pos.size),
            reduce_only=True, close_on_trigger=True, leverage=pos.leverage,
        )
        o.canceled_reason = ""  # it's a fill, not a cancel
        events.BUS.emit("tpsl", {"uid": uid, "symbol": symbol, "reason": reason})
        return True
    except ApiError:
        return False
