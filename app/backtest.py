# -*- coding: utf-8 -*-
"""
M12 (backtest): deterministic historical replay + built-in strategies.

Data source: Kraken OHLC (spot pairs, cached) with the internal candle
store as fallback. Strategies run bar-by-bar through a simplified
execution model:

  * market fills at next-bar open +/- slippage (bps);
  * limit fills when the bar range crosses the level;
  * linear taker/maker fees (config), isolated position sizing by
    leverage.

The same engine powers POST /v5/backtest/run so bots can validate a
strategy against AriaX history before live-testing it.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from . import config, util
from .marketdata import klines

STRATEGIES = ("ema_cross", "sma_cross", "rsi_reversion", "macd", "grid")


def _sma(vals: list[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _macd(vals: list[float]) -> tuple[Optional[float], Optional[float]]:
    if len(vals) < 35:
        return None, None
    ema12, ema26 = vals[0], vals[0]
    for v in vals[1:]:
        ema12 = v * (2 / 13) + ema12 * (11 / 13)
        ema26 = v * (2 / 27) + ema26 * (25 / 27)
    prev12, prev26 = None, None
    hist = []
    series12, series26 = [], []
    ema12, ema26 = vals[0], vals[0]
    for v in vals[1:]:
        ema12 = v * (2 / 13) + ema12 * (11 / 13)
        ema26 = v * (2 / 27) + ema26 * (25 / 27)
        series12.append(ema12)
        series26.append(ema26)
    if len(series12) < 9:
        return None, None
    diffs = [a - b for a, b in zip(series12, series26)]
    signal = sum(diffs[-9:]) / 9
    macd = diffs[-1]
    return macd, signal


class BacktestEngine:
    """Event-driven bar replay over one symbol/interval."""

    def __init__(self, symbol: str, interval: str, strategy: str,
                 initial_capital: float, leverage: int,
                 slippage_bps: float, params: dict):
        self.symbol = symbol
        self.interval = interval
        self.strategy = strategy
        self.capital = float(initial_capital)
        self.initial = float(initial_capital)
        self.leverage = max(1, int(leverage))
        self.slippage = slippage_bps / 10_000.0
        self.params = params or {}
        self.maker, self.taker = config.fees_for(
            config.MARKETS[symbol].kind)
        self.position: Optional[dict] = None   # {side, qty, entry}
        self.trades: list[dict] = []
        self.equity_curve: list[list] = []
        self.peak = self.initial
        self.max_dd = 0.0

    # ------------------------------------------------------------------ #
    def run(self, bars: list[list]) -> dict:
        """bars: oldest-first [ts, o, h, l, c, v, turnover]."""
        closes = [b[4] for b in bars]
        warmup = 40
        for i in range(warmup, len(bars)):
            bar = bars[i]
            self._maybe_exit(bar, closes[:i])
            self._maybe_enter(bar, closes[:i])
            self._mark_equity(bar)
        # close the open position at the last close
        if self.position and bars:
            self._close(bars[-1], reason="eod")
            self._mark_equity(bars[-1])
        return self._summary(len(bars))

    # ------------------------------------------------------------------ #
    def _signal(self, closes: list[float], bar: list) -> int:
        """1 long / -1 short / 0 flat based on the chosen strategy."""
        p = self.params
        if self.strategy == "ema_cross":
            f = util.ema(closes[-60:], int(p.get("fast", 12)))
            s = util.ema(closes[-120:], int(p.get("slow", 40)))
            return 1 if f > s else (-1 if f < s else 0)
        if self.strategy == "sma_cross":
            f = _sma(closes, int(p.get("fast", 10)))
            s = _sma(closes, int(p.get("slow", 30)))
            if f is None or s is None:
                return 0
            return 1 if f > s else (-1 if f < s else 0)
        if self.strategy == "rsi_reversion":
            r = util.rsi(closes, int(p.get("period", 14)))
            if r < 30 - p.get("buffer", 0):
                return 1
            if r > 70 + p.get("buffer", 0):
                return -1
            return 0
        if self.strategy == "macd":
            m, sig = _macd(closes)
            if m is None:
                return 0
            return 1 if m > sig else (-1 if m < sig else 0)
        if self.strategy == "grid":
            return 0  # grid handled in _maybe_enter via levels
        return 0

    def _maybe_enter(self, bar: list, closes: list[float]) -> None:
        if self.strategy == "grid":
            self._grid(bar)
            return
        sig = self._signal(closes, bar)
        if sig == 0 or self.position:
            return
        if self.position is None and sig != 0:
            side = "Buy" if sig > 0 else "Sell"
            self._open(side, bar)

    def _grid(self, bar: list) -> None:
        p = self.params
        levels = int(p.get("levels", 6))
        span = p.get("span_pct", 4.0) / 100.0
        lookback = closes = None
        anchor = bar[1]
        hi, lo = anchor * (1 + span / 2), anchor * (1 - span / 2)
        step = (hi - lo) / max(1, levels - 1)
        c = bar[4]
        grid_i = round((c - lo) / step) if step else 0
        # buy near lower band, sell near upper band
        if self.position is None and grid_i <= 1:
            self._open("Buy", bar, qty_frac=1 / levels)
        elif self.position is None and grid_i >= levels - 2:
            self._open("Sell", bar, qty_frac=1 / levels)

    def _maybe_exit(self, bar: list, closes: list[float]) -> None:
        if not self.position:
            return
        if self.strategy == "grid":
            # exit at mid band
            p = self.params
            span = p.get("span_pct", 4.0) / 100.0
            mid = bar[1] * (1 - span / 2) + \
                (bar[1] * span) / 2 * 0  # mid recompute below
            anchor = bar[1]
            hi, lo = anchor * (1 + span / 2), anchor * (1 - span / 2)
            mid = (hi + lo) / 2
            c = bar[4]
            if self.position["side"] == "Buy" and c >= mid:
                self._close(bar, "grid_mid")
            elif self.position["side"] == "Sell" and c <= mid:
                self._close(bar, "grid_mid")
            return
        sig = self._signal(closes, bar)
        if sig != 0 and self.position:
            want = "Buy" if sig > 0 else "Sell"
            if want != self.position["side"]:
                self._close(bar, "signal_flip")

    # ------------------------------------------------------------------ #
    def _open(self, side: str, bar: list, qty_frac: float = 1.0) -> None:
        px = bar[1] * (1 + self.slippage) if side == "Buy" \
            else bar[1] * (1 - self.slippage)
        riskable = self.capital * self.leverage * qty_frac
        qty = riskable / px if px else 0.0
        if qty <= 0 or riskable <= 1e-9:
            return
        fee = qty * px * self.taker
        self.capital -= fee
        self.position = dict(side=side, qty=qty, entry=px, fee=fee)

    def _close(self, bar: list, reason: str) -> None:
        pos = self.position
        if not pos:
            return
        px = bar[4] * (1 - self.slippage) if pos["side"] == "Buy" \
            else bar[4] * (1 + self.slippage)
        gross = (px - pos["entry"]) * pos["qty"] * \
            (1 if pos["side"] == "Buy" else -1)
        fee = px * pos["qty"] * self.taker
        self.capital += gross - fee
        self.trades.append(dict(
            ts=int(bar[0]), side=pos["side"], entry=pos["entry"], exit=px,
            qty=pos["qty"], pnl=round(gross - fee - pos["fee"], 6),
            fee=round(fee + pos["fee"], 6), reason=reason))
        self.position = None

    def _mark_equity(self, bar: list) -> None:
        eq = self.capital
        if self.position:
            pos = self.position
            eq += (bar[4] - pos["entry"]) * pos["qty"] * \
                (1 if pos["side"] == "Buy" else -1)
        self.peak = max(self.peak, eq)
        self.max_dd = max(self.max_dd, (self.peak - eq) / self.peak
                          if self.peak else 0.0)
        if not self.equity_curve or \
                int(bar[0]) != self.equity_curve[-1][0]:
            self.equity_curve.append([int(bar[0]), round(eq, 4)])

    def _summary(self, n_bars: int) -> dict:
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        # Sharpe on per-trade returns (annualisation-free, comparative)
        rets = [t["pnl"] / self.initial for t in self.trades]
        sharpe = 0.0
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            sharpe = mean / math.sqrt(var) if var > 0 else 0.0
        return dict(
            symbol=self.symbol, interval=self.interval,
            strategy=self.strategy, bars=n_bars,
            initial_capital=self.initial,
            final_equity=round(self.capital, 4),
            net_pnl=round(self.capital - self.initial, 4),
            return_pct=round((self.capital / self.initial - 1) * 100, 4),
            total_trades=len(self.trades),
            win_rate=round(len(wins) / len(self.trades) * 100, 2)
            if self.trades else 0.0,
            profit_factor=round(gross_win / gross_loss, 4) if gross_loss else None,
            max_drawdown_pct=round(self.max_dd * 100, 4),
            sharpe=round(sharpe, 4),
            fees=round(sum(t["fee"] for t in self.trades), 6),
            trades=self.trades[-200:],
            equity_curve=self.equity_curve[-500:],
        )


async def run_backtest(symbol: str, interval: str, strategy: str,
                       initial: float, leverage: int, slippage_bps: float,
                       params: dict, limit: int = 500) -> dict:
    """Fetch data and execute; deterministic for identical inputs."""
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {STRATEGIES}")
    rows, source, _ = await klines.get_klines(symbol, interval, limit)
    bars = list(reversed(rows))  # oldest first
    engine = BacktestEngine(symbol, interval, strategy, initial,
                            leverage, slippage_bps, params)
    result = engine.run(bars)
    result["data_source"] = source
    return result


def dumps(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
