# -*- coding: utf-8 -*-
"""
In-memory trading state (single source of truth inside the event loop).

Everything the matching engine touches lives here: accounts, positions,
order books, open orders, conditional triggers, market tick state.
The database (via db.Persister) is the durable shadow of this state.

Thread-safety: the app runs a single asyncio worker; every mutation
happens on the event loop without awaits in the critical section, so no
explicit locks are required (documented invariant for future changes).
"""
from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import config, util
from .config import MarketCfg


# --------------------------------------------------------------------------- #
# Order / position records                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    """OMS order object (Bybit v5 semantics)."""

    uid: int
    symbol: str
    category: str                # spot | linear
    side: str                    # Buy | Sell
    order_type: str              # Limit | Market
    qty: float
    price: float = 0.0
    tif: str = "GTC"             # GTC | IOC | FOK | PostOnly
    reduce_only: bool = False
    close_on_trigger: bool = False
    trigger_price: Optional[float] = None     # conditional (StopOrder) fields
    trigger_by: str = "LastPrice"
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    oco_id: Optional[str] = None
    leverage: int = 0            # captured for linear (0 = use symbol setting)
    order_link_id: Optional[str] = None
    # runtime
    id: int = 0                  # legacy integer id (also DB PK)
    order_id: str = ""
    filled_qty: float = 0.0
    avg_price: float = 0.0
    status: str = "Created"      # Created|New|PartiallyFilled|Filled|Cancelled|
                                 # Rejected|Triggered|Untriggered|Deactivated
    created_ms: int = 0
    updated_ms: int = 0
    canceled_reason: str = ""
    is_agent: bool = False       # market-maker orders (uid<=0) are not persisted
    est_price: float = 0.0       # reference price used for balance reservation
    mkt_cap: float = 0.0         # slippage cap for Market orders (acts as limit)
    cum_fee: float = 0.0

    @property
    def is_conditional(self) -> bool:
        return self.trigger_price is not None

    @property
    def leaves(self) -> float:
        return max(0.0, self.qty - self.filled_qty)


@dataclass
class Position:
    """One-way isolated position on a linear market (Bybit UTA style)."""

    uid: int
    symbol: str
    size: float = 0.0            # signed qty (base units); >0 long
    entry: float = 0.0
    leverage: int = 10
    margin: float = 0.0          # isolated margin escrowed from USDT
    tp: Optional[float] = None
    sl: Optional[float] = None
    trailing: Optional[float] = None
    trail_extreme: float = 0.0   # best price seen since trailing armed
    created_ms: int = 0
    updated_ms: int = 0

    def side(self) -> str:
        return "Buy" if self.size > 0 else ("Sell" if self.size < 0 else "None")

    def tier(self) -> tuple[float, int, float]:
        return config.tier_for_notional(config.MARKETS[self.symbol],
                                        abs(self.size) * self.entry)

    def mm_rate(self) -> float:
        return self.tier()[2]

    def unrealised(self, mark: float) -> float:
        if self.size == 0:
            return 0.0
        return (mark - self.entry) * self.size

    def liquidation_price(self, taker_fee: float = config.LINEAR_TAKER_FEE) -> float:
        """Bybit-style isolated liquidation price (incl. MM + close fee).

        Long : LP = (entry*q - margin) / (q * (1 - mm - fee))
        Short: LP = (entry*q + margin) / (q * (1 + mm + fee))
        """
        if self.size == 0:
            return 0.0
        q = abs(self.size)
        mmr = self.mm_rate()
        fee = taker_fee + config.LIQUIDATION_FEE_RATE
        if self.size > 0:
            denom = q * (1.0 - mmr - fee)
            lp = (self.entry * q - self.margin) / denom if denom > 0 else 0.0
        else:
            lp = (self.entry * q + self.margin) / (q * (1.0 + mmr + fee))
        return max(0.0, lp)


@dataclass
class Account:
    """Per-user dual wallet (classic exchange layout).

    SPOT bucket    — spot trading, deposits, faucet, signup bonus;
    FUTURES bucket — linear derivatives: margin, fees, funding, PnL.
    A transparent auto-bridge moves funds spot→futures on demand when a
    linear order lacks futures balance (keeps UTA-style bots working),
    while explicit transfers (UI / inter-transfer) move real funds.
    """

    uid: int
    balances: dict[str, float] = field(default_factory=dict)    # SPOT
    fbalances: dict[str, float] = field(default_factory=dict)   # FUTURES
    holds: dict[int, dict[str, float]] = field(default_factory=dict)    # spot order holds
    fholds: dict[int, dict[str, float]] = field(default_factory=dict)   # linear order holds

    # ---- SPOT bucket ---------------------------------------------------- #
    def free(self, asset: str = "USDT") -> float:
        return self.balances.get(asset, 0.0)

    def held(self, asset: str = "USDT") -> float:
        return sum(h.get(asset, 0.0) for h in self.holds.values())

    def available(self, asset: str = "USDT") -> float:
        return self.free(asset) - self.held(asset)

    # ---- FUTURES bucket --------------------------------------------------#
    def ffree(self, asset: str = "USDT") -> float:
        return self.fbalances.get(asset, 0.0)

    def fheld(self, asset: str = "USDT") -> float:
        return sum(h.get(asset, 0.0) for h in self.fholds.values())

    def favailable(self, asset: str = "USDT") -> float:
        return self.ffree(asset) - self.fheld(asset)

    # ---- holds ----------------------------------------------------------- #
    def add_hold(self, order_id: int, asset: str, amount: float,
                 bucket: str = "spot") -> None:
        h = self.holds if bucket == "spot" else self.fholds
        h.setdefault(order_id, {})
        h[order_id][asset] = h[order_id].get(asset, 0.0) + amount

    def release_hold(self, order_id: int, asset: str, amount: float,
                     bucket: str = "spot") -> None:
        h = self.holds if bucket == "spot" else self.fholds
        rec = h.get(order_id)
        if not rec:
            return
        rec[asset] = max(0.0, rec.get(asset, 0.0) - amount)
        if all(v <= 1e-12 for v in rec.values()):
            h.pop(order_id, None)

    def clear_holds(self, order_id: int) -> None:
        self.holds.pop(order_id, None)
        self.fholds.pop(order_id, None)

    # ---- real transfer --------------------------------------------------- #
    def transfer(self, amount: float, asset: str = "USDT",
                 direction: str = "spot_to_futures") -> float:
        """Move real funds between buckets; returns amount moved."""
        if direction == "spot_to_futures":
            amount = min(amount, self.available(asset))
            if amount <= 0:
                return 0.0
            self.balances[asset] = self.free(asset) - amount
            self.fbalances[asset] = self.ffree(asset) + amount
        else:
            amount = min(amount, self.favailable(asset))
            if amount <= 0:
                return 0.0
            self.fbalances[asset] = self.ffree(asset) - amount
            self.balances[asset] = self.free(asset) + amount
        return amount


@dataclass
class MarketTick:
    """Live market state (prices, rolling stats, candle aggregation)."""

    cfg: MarketCfg
    last: float = 0.0
    index: float = 0.0           # Kraken spot reference
    mark: float = 0.0            # Kraken futures mark / derived
    open24: float = 0.0
    high24: float = 0.0
    low24: float = 0.0
    vbase24: float = 0.0
    vquote24: float = 0.0
    funding_rate: float = 0.0    # predicted funding (per interval)
    next_funding_ms: int = 0
    prev_funding_rate: float = 0.0
    trades: deque = field(default_factory=lambda: deque(maxlen=250))
    candles1m: deque = field(default_factory=lambda: deque(maxlen=3000))
    cur: Optional[list] = None   # [ts, o, h, l, c, v] forming minute
    tickhist: deque = field(default_factory=lambda: deque(maxlen=600))

    def on_trade_price(self, px: float, ts_ms: int | None = None) -> None:
        """Update last/high/low and the forming 1m candle from a price tick."""
        self.last = px
        self.high24 = max(self.high24, px)
        self.low24 = min(self.low24 or px, px)
        self.tickhist.append(px)
        minute = (ts_ms or util.now_ms()) // 60000 * 60000
        if not self.cur or self.cur[0] != minute:
            if self.cur:
                self.candles1m.append(self.cur)
            self.cur = [minute, px, px, px, px, 0.0]
        else:
            self.cur[2] = max(self.cur[2], px)
            self.cur[3] = min(self.cur[3], px)
            self.cur[4] = px

    def add_trade(self, side: str, px: float, qty: float) -> None:
        self.trades.appendleft([util.now_ms(), side, px, qty])
        self.vbase24 += qty
        self.vquote24 += px * qty
        if self.cur:
            self.cur[5] += qty

    def seed_history(self) -> None:
        """Prefill 1m candles with a GBM walk so charts never open empty.

        Clearly a synthetic bootstrap: the live tape immediately takes over
        from the Kraken reference feed.
        """
        p = self.cfg.seed_price * random.uniform(0.985, 1.015)
        t0 = (util.now_ms() // 60000) * 60000 - 300 * 60000
        for i in range(300):
            o = p
            hi, lo, v = p, p, 0.0
            for _ in range(12):
                p *= math.exp(random.gauss(0, 0.0012))
                hi, lo = max(hi, p), min(lo, p)
                v += math.exp(random.gauss(math.log(self.cfg.qbase), 0.8))
            self.candles1m.append([t0 + i * 60000, o, hi, lo, p, v])
        self.last = self.cfg.seed_price
        self.mark = self.cfg.seed_price
        self.index = self.cfg.seed_price
        self.open24 = self.candles1m[0][1]
        self.high24 = max(x[2] for x in self.candles1m)
        self.low24 = min(x[3] for x in self.candles1m)


# --------------------------------------------------------------------------- #
# Global registries                                                            #
# --------------------------------------------------------------------------- #
class ExchangeState:
    """Aggregate root holding every mutable object of the exchange."""

    def __init__(self) -> None:
        self.accounts: dict[int, Account] = {}
        self.positions: dict[tuple[int, str], Position] = {}
        self.open_orders: dict[int, Order] = {}        # resting user orders
        self.conditional: dict[int, Order] = {}        # untriggered conditionals
        self.orders_by_link: dict[tuple[int, str], int] = {}  # (uid, linkId) -> id
        self.order_seq: int = 1
        self.markets: dict[str, MarketTick] = {}
        self.sessions: dict[str, int] = {}             # token -> uid (cache)
        self.api_keys: dict[str, dict] = {}            # key_hash -> record
        self.leverage: dict[tuple[int, str], int] = {} # per user-symbol setting
        self.close_locks: dict[tuple[int, str], float] = {}  # qty in closing orders
        self.faucet: dict[int, float] = {}             # uid -> last claim (unix s)
        self.insurance_pool: float = 0.0
        self.stats: dict = dict(start=time.time(), orders=0, fills=0, liqs=0,
                                flags=0, chats=0, users=0)
        self.agents: dict = {}
        self.bots: dict = {}                            # uid -> bot state
        self.reference: dict = dict(source="Kraken Spot + Kraken Futures",
                                    status="starting", updated=0.0, error="",
                                    prices={})
        self.mm_intensity: float = 1.0                  # stress-test amplifier
        self.force_price: dict[str, float] = {}         # admin overrides

    # ---- account helpers -------------------------------------------------- #
    def account(self, uid: int) -> Account:
        if uid not in self.accounts:
            self.accounts[uid] = Account(uid=uid)
        return self.accounts[uid]

    def position(self, uid: int, symbol: str) -> Optional[Position]:
        return self.positions.get((uid, symbol))

    def get_or_init_position(self, uid: int, symbol: str) -> Position:
        pos = self.positions.get((uid, symbol))
        if pos is None:
            pos = Position(uid=uid, symbol=symbol, created_ms=util.now_ms())
            self.positions[(uid, symbol)] = pos
        return pos

    def leverage_for(self, uid: int, symbol: str, default: int = 10) -> int:
        return self.leverage.get((uid, symbol), default)

    def equity_usdt(self, uid: int) -> float:
        """Total equity: spot USDT + futures USDT + escrowed margins + uPnL."""
        acct = self.accounts.get(uid)
        eq = 0.0
        if acct:
            eq += acct.free("USDT") + acct.ffree("USDT")
        eq += sum(p.margin for (u, _), p in self.positions.items() if u == uid)
        eq += sum(p.unrealised(m.mark) for (u, s), p in self.positions.items()
                  if u == uid for m in [self.markets.get(s)] if m)
        return eq

    def margin_used(self, uid: int) -> float:
        return sum(p.margin for (u, _), p in self.positions.items() if u == uid)

    def free_margin(self, uid: int) -> float:
        """Spendable margin: futures-bucket USDT minus linear order holds.

        (Position margin was already escrowed OUT of the futures bucket at
        open, so it must not be subtracted again here.)
        """
        acct = self.accounts.get(uid)
        if not acct:
            return 0.0
        return acct.favailable("USDT")

    # ---- book/market ------------------------------------------------------ #
    def tick(self, symbol: str) -> MarketTick:
        return self.markets[symbol]


STATE = ExchangeState()

# Books live next to the state (populated by engine.orderbook at startup).
BOOKS: dict[str, "object"] = {}   # symbol -> OrderBook (engine.orderbook)
