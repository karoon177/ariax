# -*- coding: utf-8 -*-
"""
M5 (order book): aggregated L2 book with Bybit-style update sequencing.

Internally each price level is a FIFO deque of resting `Order` objects —
this is what gives strict *price-time priority* in the matching engine.
The public face is an aggregated depth view plus a monotonic `u` update
counter and a dirty-level set, consumed by the WebSocket delta pump
(``snapshot``/``delta`` messages equivalent to Bybit v5 orderbook stream).
"""
from __future__ import annotations

from collections import deque

from ..state import Order, STATE
from .. import config


class OrderBook:
    """One side-agnostic L2 book for a single instrument."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[float, deque] = {}   # price -> deque[Order] (FIFO)
        self.asks: dict[float, deque] = {}
        self.seq: int = 0                    # Bybit `u` (update id, monotonic)
        self.dirty: dict[str, set[float]] = {"b": set(), "a": set()}

    # ------------------------------------------------------------------ #
    # Mutation                                                            #
    # ------------------------------------------------------------------ #
    def add(self, order: Order) -> None:
        """Append a resting order to its price level (time priority)."""
        side = self.bids if order.side == "Buy" else self.asks
        dq = side.get(order.price)
        if dq is None:
            dq = side[order.price] = deque()
        dq.append(order)
        self._touch("b" if order.side == "Buy" else "a", order.price)

    def remove(self, order: Order) -> bool:
        """Remove a resting order; returns False when not found."""
        side = self.bids if order.side == "Buy" else self.asks
        dq = side.get(order.price)
        if not dq:
            return False
        try:
            dq.remove(order)
        except ValueError:
            return False
        if not dq:
            side.pop(order.price, None)
        self._touch("b" if order.side == "Buy" else "a", order.price)
        return True

    def _touch(self, side_key: str, price: float) -> None:
        self.seq += 1
        self.dirty[side_key].add(price)

    # ------------------------------------------------------------------ #
    # Queries                                                             #
    # ------------------------------------------------------------------ #
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def opposite(self, side: str) -> dict[float, deque]:
        """The book side an incoming `side` order matches against."""
        return self.asks if side == "Buy" else self.bids

    def opposite_best(self, side: str) -> float | None:
        """Best price on the opposite side for an incoming order."""
        if side == "Buy":
            return self.best_ask()
        return self.best_bid()

    def level_qty(self, book: dict[float, deque], price: float) -> float:
        dq = book.get(price)
        return sum(o.leaves for o in dq) if dq else 0.0

    def liquidity_at(self, side: str, limit_price: float | None) -> float:
        """Total opposite-side qty available within `limit_price` (FOK check)."""
        book = self.opposite(side)
        want_ascending = side == "Buy"
        prices = sorted(book)
        if not want_ascending:
            prices = prices[::-1]
        total = 0.0
        for p in prices:
            crosses = True
            if limit_price is not None:
                crosses = (p <= limit_price) if side == "Buy" else (p >= limit_price)
            if not crosses:
                break
            total += self.level_qty(book, p)
        return total

    def depth(self, levels: int = 50) -> dict:
        """Aggregated depth snapshot in Bybit response shape."""
        cfg = config.MARKETS[self.symbol]

        def agg(side: dict[float, deque], reverse: bool):
            rows = []
            for p in sorted(side, reverse=reverse)[:levels]:
                q = self.level_qty(side, p)
                if q > 0:
                    rows.append([config_f(p, cfg.tick), config_f(q, cfg.qty_step)])
            return rows

        return {
            "s": self.symbol,
            "b": agg(self.bids, True),
            "a": agg(self.asks, False),
            "u": self.seq,
            "ts": _now_ms(),
        }

    def drain_dirty(self) -> dict:
        """Return and clear dirty levels (used by the WS delta pump)."""
        cfg = config.MARKETS[self.symbol]
        out: dict = {"u": self.seq, "ts": _now_ms(), "b": [], "a": []}
        for key, side in (("b", self.bids), ("a", self.asks)):
            for p in self.dirty[key]:
                q = self.level_qty(side, p)
                out[key].append([config_f(p, cfg.tick), config_f(q, cfg.qty_step)])
            self.dirty[key].clear()
        return out


def config_f(value: float, step: float) -> str:
    """Format a price/qty exactly per step decimals (local import avoids cycle)."""
    from .. import util
    return util.fmt(value, step)


def _now_ms() -> int:
    from .. import util
    return util.now_ms()


def build_books() -> None:
    """Create the book + market-tick registry for every listed market."""
    from ..state import BOOKS, MarketTick, STATE
    for symbol, cfg in config.MARKETS.items():
        BOOKS[symbol] = OrderBook(symbol)
        STATE.markets[symbol] = MarketTick(cfg=cfg)


def book(symbol: str) -> OrderBook:
    from ..state import BOOKS
    return BOOKS[symbol]
