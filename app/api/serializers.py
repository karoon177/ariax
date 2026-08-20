# -*- coding: utf-8 -*-
"""
Serializers: internal state -> Bybit v5 JSON shapes (and legacy v1 shapes
for the existing UI). All price/qty fields are strings (Bybit convention).
"""
from __future__ import annotations

from .. import config, util
from ..state import STATE


def _f(value: float, step: float) -> str:
    return util.fmt(value, step)


# --------------------------------------------------------------------------- #
# Market data                                                                  #
# --------------------------------------------------------------------------- #
def ticker_v5(symbol: str, t) -> dict:
    cfg = config.MARKETS[symbol]
    chg = (t.last / t.open24 - 1) if t.open24 else 0.0
    out = {
        "symbol": cfg.v5_symbol,
        "lastPrice": _f(t.last, cfg.tick),
        "highPrice24h": _f(t.high24, cfg.tick),
        "lowPrice24h": _f(t.low24, cfg.tick),
        "prevPrice24h": _f(t.open24, cfg.tick),
        "volume24h": _f(t.vbase24, cfg.qty_step),
        "turnover24h": f"{t.vquote24:.2f}",
        "price24hPcnt": f"{chg:.4f}",
        "nextFundingTime": str(int(t.next_funding_ms)) if cfg.kind == "linear" else "0",
    }
    if cfg.kind == "linear":
        out.update({
            "markPrice": _f(t.mark, cfg.tick),
            "indexPrice": _f(t.index, cfg.tick),
            "fundingRate": f"{t.funding_rate:.6f}",
            "prevFundingRate": f"{t.prev_funding_rate:.6f}",
            "openInterest": _f(sum(o.leaves for o in STATE.open_orders.values()
                                   if o.symbol == symbol), cfg.qty_step),
            "openInterestValue": f"{sum(o.leaves * o.price for o in STATE.open_orders.values() if o.symbol == symbol) :.2f}",
            "bid1Price": "", "bid1Size": "", "ask1Price": "", "ask1Size": "",
        })
        from ..engine import orderbook
        bb = orderbook.book(symbol).best_bid()
        ba = orderbook.book(symbol).best_ask()
        if bb:
            out["bid1Price"] = _f(bb, cfg.tick)
        if ba:
            out["ask1Price"] = _f(ba, cfg.tick)
    return out


def ticker_legacy(symbol: str, t) -> dict:
    chg = (t.last / t.open24 - 1) * 100 if t.open24 else 0.0
    cfg = config.MARKETS[symbol]
    out = dict(last=float(_f(t.last, cfg.tick)), chg=round(chg, 2),
               high=t.high24, low=t.low24, vol=round(t.vquote24, 0),
               kind=cfg.kind, mark=float(_f(t.mark, cfg.tick)),
               funding=round(t.funding_rate * 100, 4))
    return out


def instrument_v5(symbol: str) -> dict:
    cfg = config.MARKETS[symbol]
    contract = "Spot" if cfg.kind == "spot" else "LinearPerpetual"
    out = {
        "symbol": cfg.v5_symbol,
        "contractType": contract,
        "status": "Trading",
        "baseCoin": cfg.base,
        "quoteCoin": cfg.quote,
        "launchTime": "1700000000000",
        "deliveryTime": "0",
        "deliveryFeeRate": "",
        "priceScale": str(util.decimals_of(cfg.tick)),
        "leverageFilter": {
            "minLeverage": "1",
            "maxLeverage": str(cfg.max_lev),
            "leverageStep": "1",
        },
        "priceFilter": {
            "minPrice": _f(cfg.tick, cfg.tick),
            "maxPrice": _f(cfg.seed_price * 100, cfg.tick),
            "tickSize": _f(cfg.tick, cfg.tick),
        },
        "lotSizeFilter": {
            "minOrderQty": _f(cfg.min_qty, cfg.qty_step),
            "maxOrderQty": _f(cfg.min_qty * 1_000_000, cfg.qty_step),
            "qtyStep": _f(cfg.qty_step, cfg.qty_step),
            "minNotionalValue": _f(cfg.min_notional, "0.01"),
            "maxMktOrderQty": _f(cfg.min_qty * 100_000, cfg.qty_step),
        },
    }
    if cfg.kind == "linear":
        out["riskLimits"] = [
            {"riskLimitValue": str(int(n)), "maxLeverage": str(lev),
             "initialMarginRate": f"{1.0 / lev:.4f}",
             "maintenanceMarginRate": f"{mmr:.4f}"}
            for n, lev, mmr in config.tiers_for(cfg)
        ]
    return out


# --------------------------------------------------------------------------- #
# Orders / positions / wallet                                                  #
# --------------------------------------------------------------------------- #
def order_v5(snap: dict) -> dict:
    cfg = config.MARKETS[snap["symbol"]]
    out = {
        "symbol": cfg.v5_symbol,
        "orderId": snap["order_id"],
        "orderLinkId": snap["order_link_id"] or "",
        "side": snap["side"],
        "orderType": snap["order_type"],
        "price": _f(snap["price"], cfg.tick) if snap["price"] else "0",
        "qty": _f(snap["qty"], cfg.qty_step),
        "timeInForce": snap["tif"],
        "orderStatus": snap["status"],
        "cumExecQty": _f(snap["filled_qty"], cfg.qty_step),
        "cumExecValue": f"{snap['filled_qty'] * snap['avg_price']:.4f}" if snap["avg_price"] else "0",
        "avgPrice": _f(snap["avg_price"], cfg.tick) if snap["avg_price"] else "0",
        "reduceOnly": snap["reduce_only"],
        "closeOnTrigger": snap["close_on_trigger"],
        "createdTime": str(snap["created_ms"]),
        "updatedTime": str(snap["updated_ms"]),
        "triggerPrice": _f(snap["trigger_price"], cfg.tick) if snap["trigger_price"] else "",
        "triggerBy": snap["trigger_by"] or "",
        "takeProfit": _f(snap["tp_price"], cfg.tick) if snap["tp_price"] else "",
        "stopLoss": _f(snap["sl_price"], cfg.tick) if snap["sl_price"] else "",
        "leverage": str(snap["leverage"] or ""),
        "category": snap["category"],
    }
    return out


def order_event_v5(snap: dict) -> dict:
    return order_v5(snap)


def order_legacy(snap: dict) -> dict:
    return dict(id=snap["id"], symbol=snap["symbol"],
                side=snap["side"].lower(),
                type=snap["order_type"].lower(), price=snap["price"],
                qty=snap["qty"], rem=round(max(0.0, snap["qty"] - snap["filled_qty"]), 10),
                ts=snap["created_ms"] / 1000.0, lev=snap["leverage"] or None)


def position_v5(uid: int, symbol: str, pos) -> dict:
    cfg = config.MARKETS[symbol]
    t = STATE.tick(symbol)
    mark = t.mark or t.last
    upnl = pos.unrealised(mark)
    lev = pos.leverage
    return {
        "positionIdx": 0,
        "riskId": "1",
        "riskLimitValue": str(int(pos.tier()[0])),
        "symbol": cfg.v5_symbol,
        "side": pos.side(),
        "size": _f(abs(pos.size), cfg.qty_step),
        "avgPrice": _f(pos.entry, cfg.tick),
        "positionValue": f"{abs(pos.size) * pos.entry:.4f}",
        "tradeMode": 1,  # isolated
        "positionStatus": "Normal",
        "autoAddMargin": 0,
        "adlRankIndicator": 0,
        "leverage": str(lev),
        "positionBalance": f"{pos.margin:.4f}",
        "markPrice": _f(mark, cfg.tick),
        "liqPrice": _f(pos.liquidation_price(), cfg.tick),
        "bustPrice": _f(_bust(pos), cfg.tick),
        "positionIM": f"{abs(pos.size) * pos.entry / lev:.4f}",
        "positionMM": f"{pos.mm_rate() * abs(pos.size) * mark:.4f}",
        "unrealisedPnl": f"{upnl:.4f}",
        "curRealisedPnl": "0",
        "cumRealisedPnl": "0",
        "takeProfit": _f(pos.tp, cfg.tick) if pos.tp else "",
        "stopLoss": _f(pos.sl, cfg.tick) if pos.sl else "",
        "trailingStop": _f(pos.trailing, cfg.tick) if pos.trailing else "",
        "createdTime": str(pos.created_ms),
        "updatedTime": str(pos.updated_ms),
    }


def position_event_v5(uid: int, symbol: str) -> dict:
    pos = STATE.position(uid, symbol)
    if not pos or pos.size == 0:
        cfg = config.MARKETS[symbol]
        return {"symbol": cfg.v5_symbol, "side": "None", "size": "0",
                "positionStatus": "Closed"}
    return position_v5(uid, symbol, pos)


def _bust(pos) -> float:
    if pos.size == 0:
        return 0.0
    q = abs(pos.size)
    if pos.size > 0:
        return max(0.0, pos.entry - pos.margin / q)
    return pos.entry + pos.margin / q


def position_legacy(uid: int, symbol: str, pos) -> dict:
    cfg = config.MARKETS[symbol]
    t = STATE.tick(symbol)
    px = t.mark or t.last
    upnl = pos.unrealised(px)
    return dict(symbol=symbol, size=pos.size, entry=pos.entry, lev=pos.leverage,
                margin=round(pos.margin, 4), upnl=round(upnl, 4), mark=px,
                liq=round(pos.liquidation_price(), 2),
                tp=pos.tp, sl=pos.sl)


def wallet_event_v5(uid: int, account_type: str = "UNIFIED") -> dict:
    """Wallet balances per account type.

    UNIFIED  -> spot + futures combined (UTA view);
    SPOT     -> spot bucket only;
    CONTRACT -> futures bucket only (classic derivatives wallet).
    """
    acct = STATE.account(uid)
    coins = []
    for asset in config.LISTED_ASSETS:
        if account_type == "CONTRACT":
            if asset != "USDT":
                continue
            free = acct.ffree(asset)
            avail = max(0.0, acct.favailable(asset))
        elif account_type == "SPOT":
            free = acct.free(asset)
            avail = max(0.0, acct.available(asset))
        else:  # UNIFIED
            free = acct.free(asset) + (acct.ffree(asset)
                                       if asset == "USDT" else 0.0)
            avail = max(0.0, acct.available(asset)) + \
                (max(0.0, acct.favailable(asset)) if asset == "USDT" else 0.0)
        if free <= 0 and asset != "USDT":
            continue
        coins.append({"coin": asset, "equity": f"{free:.8f}",
                      "walletBalance": f"{free:.8f}",
                      "availableToWithdraw": f"{avail:.8f}",
                      "totalPositionIM": f"{STATE.margin_used(uid) if asset == 'USDT' else 0:.4f}",
                      "totalOrderIM": f"{(acct.held('USDT') + acct.fheld('USDT')) if asset == 'USDT' else 0:.4f}"})
    return {"accountType": account_type, "balances": coins}


def wallet_v5(uid: int) -> dict:
    data = wallet_event_v5(uid, "UNIFIED")
    data["totalEquity"] = f"{STATE.equity_usdt(uid):.4f}"
    data["totalAvailableBalance"] = \
        f"{STATE.account(uid).available('USDT') + STATE.free_margin(uid):.4f}"
    data["totalMarginBalance"] = f"{STATE.equity_usdt(uid):.4f}"
    return data
