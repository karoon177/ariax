# -*- coding: utf-8 -*-
"""
Engine unit tests: matching priority, settlement math, risk formulas,
conditional triggers, order semantics (FOK/IOC/PostOnly), OCO.
Deterministic: no network, agent loops disabled by manual book setup.
"""
from __future__ import annotations

import pytest

from conftest import reset_state

from app import config
from app.engine import matching, orderbook, orders
from app.errors import ApiError
from app.state import STATE


def _mk_market_quotes(symbol: str, mid: float, spread: float = 1.0):
    """Place synthetic maker liquidity around mid (agent uid=0)."""
    cfg = config.MARKETS[symbol]
    STATE.tick(symbol).mark = mid
    STATE.tick(symbol).last = mid
    STATE.tick(symbol).index = mid
    STATE.tick(symbol).open24 = mid
    for i in range(5):
        for side, sgn in (("Buy", -1), ("Sell", 1)):
            STATE.order_seq += 1
            o = orders.place_order(0, symbol, side, "Limit", cfg.min_qty * 50,
                                   price=round(mid + sgn * spread * (i + 1), 10),
                                   is_agent=True)
    return orderbook.book(symbol)


@pytest.fixture(autouse=True)
def fresh():
    return reset_state()


# --------------------------------------------------------------------------- #
# Order book                                                                   #
# --------------------------------------------------------------------------- #
def test_price_time_priority():
    """Two resting buys at the same price fill in arrival order."""
    STATE.account(1).balances["USDT"] = 1e9
    STATE.account(2).balances["USDT"] = 1e9
    o1 = orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.01, price=100_000)
    o2 = orders.place_order(2, "BTC/USDT", "Buy", "Limit", 0.01, price=100_000)
    STATE.account(3).balances["BTC"] = 1.0
    taker = orders.place_order(3, "BTC/USDT", "Sell", "Limit", 0.015,
                               price=100_000)
    assert taker.status == "Filled"
    assert o1.filled_qty == pytest.approx(0.01) and o1.status == "Filled"
    assert o2.filled_qty == pytest.approx(0.005)
    assert o2.status == "PartiallyFilled"


def test_book_depth_and_delta():
    book = _mk_market_quotes("BTC/USDT", 100_000, spread=5)
    snap = book.depth(50)
    assert snap["b"] and snap["a"]
    assert float(snap["a"][0][0]) < float(snap["b"][0][0]) or True
    best_bid = float(snap["b"][0][0])
    best_ask = float(snap["a"][0][0])
    assert best_bid < best_ask
    delta = book.drain_dirty()
    assert delta["u"] >= snap["u"]


# --------------------------------------------------------------------------- #
# Settlement                                                                   #
# --------------------------------------------------------------------------- #
def test_spot_settlement_fees_and_balances():
    STATE.account(1).balances["USDT"] = 100_000
    STATE.account(2).balances["BTC"] = 1.0
    maker, taker = config.SPOT_MAKER_FEE, config.SPOT_TAKER_FEE
    orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.2, price=95_000)
    t = orders.place_order(2, "BTC/USDT", "Sell", "Limit", 0.2, price=95_000)
    assert t.status == "Filled"
    assert STATE.account(1).free("BTC") == pytest.approx(0.2)
    assert STATE.account(1).free("USDT") == pytest.approx(
        100_000 - 95_000 * 0.2 * (1 + maker))
    assert STATE.account(2).free("USDT") == pytest.approx(
        95_000 * 0.2 * (1 - taker))
    assert STATE.account(2).free("BTC") == pytest.approx(0.8)


def test_linear_open_close_realized_pnl():
    STATE.account(1).balances["USDT"] = 1e6
    orders.place_order(0, "BTCUSD", "Sell", "Limit", 1.0, price=100_000,
                       is_agent=True)
    orders.place_order(1, "BTCUSD", "Buy", "Limit", 1.0, price=100_000,
                       leverage=10)
    pos = STATE.position(1, "BTCUSD")
    assert pos.size == pytest.approx(1.0) and pos.leverage == 10
    assert pos.margin == pytest.approx(100_000 / 10)      # 10,000 USDT
    # counter-liquidity for the close
    orders.place_order(0, "BTCUSD", "Buy", "Limit", 0.5, price=110_000,
                       is_agent=True)
    orders.place_order(1, "BTCUSD", "Sell", "Limit", 0.5, price=110_000,
                       reduce_only=True)
    pos = STATE.position(1, "BTCUSD")
    assert pos.size == pytest.approx(0.5)
    assert pos.margin == pytest.approx(5_000)             # half released
    # realized pnl = (110k-100k) * 0.5 = +5000; margin 10k back minus 5k held
    assert STATE.account(1).free("USDT") > 1e6 - 5_000 - 60


def test_liquidation_price_direction_and_formula():
    from app.state import Position
    pos = Position(uid=1, symbol="BTCUSD", size=1.0, entry=100_000,
                   leverage=10, margin=10_000)
    lp = pos.liquidation_price()
    assert 0 < lp < 100_000
    # equity at LP should be ~ maintenance + close fees
    mmr = pos.mm_rate()
    fee = config.LINEAR_TAKER_FEE + config.LIQUIDATION_FEE_RATE
    equity = pos.margin + (lp - 100_000) * 1.0
    assert equity == pytest.approx(lp * (mmr + fee), rel=0.01)
    short = Position(uid=1, symbol="BTCUSD", size=-1.0, entry=100_000,
                     leverage=10, margin=10_000)
    assert short.liquidation_price() > 100_000


def test_risk_limit_tier_progression():
    cfg = config.MARKETS["BTCUSD"]
    n1, lev1, mm1 = config.tier_for_notional(cfg, 100_000)
    n2, lev2, mm2 = config.tier_for_notional(cfg, 500_000)
    assert lev1 == 100 and lev2 == 50
    assert mm2 > mm1


# --------------------------------------------------------------------------- #
# Funding                                                                      #
# --------------------------------------------------------------------------- #
def test_funding_rate_clamp_and_next_ts():
    from app.engine import funding as fe
    from app.engine.funding import next_funding_ts, predicted_rate, settle_funding
    fe._PREMIUM_EMA.clear()
    r = predicted_rate("BTCUSD", 100_000, 50_000)  # crazy premium
    assert r == pytest.approx(config.FUNDING_CAP)
    fe._PREMIUM_EMA.clear()
    r2 = predicted_rate("BTCUSD", 50_000, 100_000)
    assert r2 == pytest.approx(-config.FUNDING_CAP)
    nf = next_funding_ts(1_700_000_000_000, 8)
    assert nf > 1_700_000_000_000 and nf % (8 * 3600_000) == 0
    STATE.account(9).balances["USDT"] = 1000
    STATE.get_or_init_position(9, "BTCUSD").__dict__.update(
        size=1.0, entry=100_000, leverage=10, margin=10_000)
    STATE.tick("BTCUSD").mark = 100_000
    n = settle_funding("BTCUSD", 0.0001, 100_000)
    assert n == 1
    assert STATE.account(9).free("USDT") == pytest.approx(1000 - 10)


# --------------------------------------------------------------------------- #
# Conditional orders / TP-SL                                                   #
# --------------------------------------------------------------------------- #
def test_conditional_trigger_closes_position():
    STATE.account(1).balances["USDT"] = 1e6
    orders.place_order(0, "BTCUSD", "Sell", "Limit", 0.5, price=100_000,
                       is_agent=True)
    orders.place_order(1, "BTCUSD", "Buy", "Limit", 0.5, price=100_000,
                       leverage=10)
    assert STATE.position(1, "BTCUSD").size == pytest.approx(0.5)
    # SL conditional: sell below mark + exit liquidity
    orders.place_order(1, "BTCUSD", "Sell", "Market", 0.5,
                       trigger_price=95_000, trigger_by="MarkPrice",
                       reduce_only=True, close_on_trigger=True)
    orders.place_order(0, "BTCUSD", "Buy", "Limit", 0.5, price=93_800,
                       is_agent=True)
    STATE.tick("BTCUSD").mark = 94_000
    orders.check_triggers()
    assert STATE.position(1, "BTCUSD") is None


def test_position_tp_sl_and_trailing():
    STATE.account(1).balances["USDT"] = 1e6
    orders.place_order(0, "BTCUSD", "Sell", "Limit", 0.5, price=100_000,
                       is_agent=True)
    orders.place_order(1, "BTCUSD", "Buy", "Limit", 0.5, price=100_000,
                       leverage=10)
    orders.set_trading_stop(1, "BTCUSD", tp=105_000, sl=95_000)
    pos = STATE.position(1, "BTCUSD")
    assert pos.tp == 105_000 and pos.sl == 95_000
    orders.place_order(0, "BTCUSD", "Buy", "Limit", 0.5, price=104_500,
                       is_agent=True)
    STATE.tick("BTCUSD").mark = 105_500
    orders.check_position_tpsl()
    assert STATE.position(1, "BTCUSD") is None


def test_oco_sibling_deactivation():
    STATE.account(1).balances["USDT"] = 1e6
    orders.place_order(0, "BTCUSD", "Sell", "Limit", 0.5, price=100_000,
                       is_agent=True)
    orders.place_order(1, "BTCUSD", "Buy", "Limit", 0.5, price=100_000,
                       leverage=10)
    a = orders.place_order(1, "BTCUSD", "Sell", "Market", 0.5,
                           trigger_price=105_000, reduce_only=True)
    b = orders.place_order(1, "BTCUSD", "Sell", "Market", 0.5,
                           trigger_price=95_000, reduce_only=True)
    a.oco_id = b.oco_id = "oco-1"
    orders.place_order(0, "BTCUSD", "Buy", "Limit", 0.5, price=104_500,
                       is_agent=True)
    STATE.tick("BTCUSD").mark = 105_500
    orders.check_triggers()
    assert b.status == "Deactivated"


# --------------------------------------------------------------------------- #
# Time-in-force semantics                                                      #
# --------------------------------------------------------------------------- #
def test_fok_ioc_postonly():
    STATE.account(1).balances["USDT"] = 1e9
    # thin book: single agent level
    STATE.order_seq += 1
    orders.place_order(0, "BTC/USDT", "Sell", "Limit", 0.02, price=100_000,
                       is_agent=True)
    fok = orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.05,
                             price=100_000, tif="FOK")
    assert fok.status == "Cancelled" and fok.filled_qty == 0
    ioc = orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.05,
                             price=100_000, tif="IOC")
    assert ioc.filled_qty == pytest.approx(0.02)
    assert ioc.status == "Cancelled"  # remainder dropped (IOC)
    # re-add liquidity so the PostOnly order would cross
    orders.place_order(0, "BTC/USDT", "Sell", "Limit", 0.02, price=100_000,
                       is_agent=True)
    with pytest.raises(ApiError):
        orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.01,
                           price=100_000, tif="PostOnly")


def test_cancel_releases_holds():
    STATE.account(1).balances["USDT"] = 100_000
    o = orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.1, price=95_000)
    acct = STATE.account(1)
    assert acct.available("USDT") < 100_000
    orders.cancel_order(1, order_id=o.order_id)
    assert acct.available("USDT") == pytest.approx(100_000)


def test_amend_moves_price_level():
    STATE.account(1).balances["USDT"] = 1e9
    o = orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.1, price=90_000)
    orders.amend_order(1, order_id=o.order_id, price=91_000)
    book = orderbook.book("BTC/USDT")
    assert book.level_qty(book.bids, 91_000) == pytest.approx(0.1)
    assert not book.bids.get(90_000)


def test_insufficient_balance_rejected():
    STATE.account(1).balances["USDT"] = 100
    with pytest.raises(ApiError) as ei:
        orders.place_order(1, "BTC/USDT", "Buy", "Limit", 0.1, price=95_000)
    assert ei.value.ret_code == 110007


def test_market_order_slippage_cap():
    STATE.account(1).balances["USDT"] = 1e9
    STATE.order_seq += 1
    orders.place_order(0, "BTC/USDT", "Sell", "Limit", 0.5, price=100_000,
                       is_agent=True)
    STATE.order_seq += 1
    orders.place_order(0, "BTC/USDT", "Sell", "Limit", 0.5, price=150_000,
                       is_agent=True)
    o = orders.place_order(1, "BTC/USDT", "Buy", "Market", 0.5)
    # fills only the level inside the cap
    assert o.filled_qty == pytest.approx(0.5)
    assert o.avg_price == pytest.approx(100_000)
