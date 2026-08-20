# -*- coding: utf-8 -*-
"""
Module M8 (config layer): global settings, market catalog, risk-limit tiers.

All numeric trading parameters live here so that the engine stays
declarative and easy to audit. Reference standards: Bybit API v5
instrument metadata (lot size / price filters, leverage filter,
risk limits) mirrored on https://bybit-exchange.github.io/docs/v5/.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# Runtime settings (env-driven; safe defaults for local dev)                   #
# --------------------------------------------------------------------------- #
PORT = int(os.environ.get("PORT", 8000))
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite+aiosqlite:///./data/exchange.db"
)
# Master key used to encrypt API-key secrets at rest (AES-256-GCM).
# Base64 of 32 raw bytes. When empty, a key file under data/ is used.
MASTER_KEY_B64 = os.environ.get("ARIAX_MASTER_KEY", "")
# When set, enables /v5/admin/* endpoints used by the stress-test suite.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
# Test-faucet policy (spec: claim once every 24 hours).
FAUCET_COOLDOWN_HOURS = float(os.environ.get("FAUCET_COOLDOWN_HOURS", 24))
FAUCET_USDT = float(os.environ.get("FAUCET_USDT", 10_000))
SIGNUP_BONUS_USDT = float(os.environ.get("SIGNUP_BONUS_USDT", 20_000))
# Funding settlement cadence (hours). Bybit default: 8h (00:00/08:00/16:00 UTC).
FUNDING_INTERVAL_H = float(os.environ.get("FUNDING_INTERVAL_H", 8))
FUNDING_CAP = 0.0075          # |funding rate| hard cap per interval
FUNDING_BASE_RATE = 0.0001    # static interest component (0.01% / 8h)
# Bybit-style recv-window for signed requests (ms).
DEFAULT_RECV_WINDOW = 5000
MAX_RECV_WINDOW = 60000


# Fee schedule (mirror of Bybit non-VIP defaults for linear; spot kept at
# AriaX v1 values to avoid surprising existing UI users — see MIGRATION_GUIDE).
LINEAR_MAKER_FEE = 0.0002
LINEAR_TAKER_FEE = 0.00055
SPOT_MAKER_FEE = 0.0002
SPOT_TAKER_FEE = 0.0005
LIQUIDATION_FEE_RATE = 0.0075  # charged on forced-close notional, to insurance

LISTED_ASSETS = ["USDT", "BTC", "ETH", "SOL", "XRP", "DOGE"]

MAX_ORDER_NOTIONAL = 2_000_000     # AML-style whale guard (flags + rejects >)
PRICE_DEVIATION_REJECT = 0.20      # limit orders farther than 20% are rejected
PRICE_DEVIATION_FLAG = 0.05        # >5% deviation is flagged by the watch agent


# --------------------------------------------------------------------------- #
# Risk-limit tiers (linear contracts)                                          #
# (max_notional_usd, max_leverage, maintenance_margin_rate)                    #
# Modelled after Bybit linear risk limits (BTC class A, ETH class B, alt C).   #
# --------------------------------------------------------------------------- #
RISK_TIERS: dict[str, list[tuple[float, int, float]]] = {
    "A": [(150_000, 100, 0.005), (600_000, 50, 0.010),
          (2_000_000, 25, 0.025), (6_000_000, 10, 0.050)],
    "B": [(100_000, 50, 0.005), (400_000, 25, 0.010),
          (1_500_000, 12, 0.025), (5_000_000, 6, 0.050)],
    "C": [(50_000, 20, 0.005), (200_000, 10, 0.010),
          (800_000, 5, 0.025), (3_000_000, 3, 0.050)],
}


@dataclass(frozen=True)
class MarketCfg:
    """Static instrument definition (Bybit instruments-info equivalent)."""

    symbol: str          # internal symbol, e.g. "BTC/USDT" (spot) / "BTCUSD" (linear)
    kind: str            # "spot" | "linear"
    base: str
    tick: float          # min price increment
    qty_step: float      # min qty increment
    min_qty: float
    qbase: float         # market-maker base quote size
    max_lev: int
    tier_class: str      # A | B | C (linear only)
    seed_price: float
    kraken_spot: str = ""     # Kraken REST pair for index price
    kraken_fut: str = ""      # Kraken futures symbol for mark price
    min_notional: float = 1.0
    quote: str = "USDT"

    # ---- Bybit v5 symbol translation ------------------------------------- #
    @property
    def v5_symbol(self) -> str:
        """Map internal symbol to Bybit v5 naming (BTCUSD -> BTCUSDT)."""
        if self.kind == "spot":
            return self.symbol.replace("/", "")
        return self.symbol[:-3] + "USDT"


_SPOT_SEEDS = [
    # symbol, base, price, tick, step, min_qty, qbase
    ("BTC/USDT", "BTC", 115250.0, 0.1, 0.0001, 0.0005, 0.35, "XBTUSDT"),
    ("ETH/USDT", "ETH", 4310.0, 0.01, 0.001, 0.005, 4.0, "ETHUSDT"),
    ("SOL/USDT", "SOL", 186.4, 0.01, 0.01, 0.05, 45.0, "SOLUSDT"),
    ("XRP/USDT", "XRP", 2.24, 0.0001, 0.1, 1.0, 900.0, "XRPUSDT"),
    ("DOGE/USDT", "DOGE", 0.238, 0.00001, 1.0, 10.0, 12000.0, "XDGUSDT"),
]
_LINEAR_SEEDS = [
    # symbol, base, price, tick, step, min_qty, qbase, maxlev, tier, kraken fut
    ("BTCUSD", "BTC", 115265.0, 0.1, 0.0001, 0.0005, 0.35, 100, "A", "PF_XBTUSD"),
    ("ETHUSD", "ETH", 4311.2, 0.01, 0.001, 0.005, 4.0, 50, "B", "PF_ETHUSD"),
    ("SOLUSD", "SOL", 186.45, 0.01, 0.01, 0.05, 45.0, 20, "C", "PF_SOLUSD"),
    ("XRPUSD", "XRP", 1.02, 0.0001, 1.0, 1.0, 800.0, 20, "C", "PF_XRPUSD"),
    ("DOGEUSD", "DOGE", 0.07, 0.00001, 1.0, 10.0, 8000.0, 20, "C", "PF_DOGEUSD"),
    ("ADAUSD", "ADA", 0.19, 0.0001, 1.0, 10.0, 4000.0, 20, "C", "PF_ADAUSD"),
    ("AVAXUSD", "AVAX", 6.45, 0.001, 0.01, 0.1, 80.0, 20, "C", "PF_AVAXUSD"),
    ("LINKUSD", "LINK", 8.27, 0.001, 0.01, 0.1, 60.0, 20, "C", "PF_LINKUSD"),
    ("DOTUSD", "DOT", 0.81, 0.0001, 0.1, 1.0, 700.0, 20, "C", "PF_DOTUSD"),
    ("LTCUSD", "LTC", 45.1, 0.01, 0.01, 0.01, 12.0, 20, "C", "PF_LTCUSD"),
    ("BCHUSD", "BCH", 212.9, 0.01, 0.001, 0.001, 3.0, 20, "C", "PF_BCHUSD"),
    ("TRXUSD", "TRX", 0.331, 0.00001, 1.0, 10.0, 3000.0, 20, "C", "PF_TRXUSD"),
    ("XLMUSD", "XLM", 0.163, 0.0001, 1.0, 10.0, 2000.0, 20, "C", "PF_XLMUSD"),
    ("AAVEUSD", "AAVE", 89.8, 0.01, 0.001, 0.001, 4.0, 20, "C", "PF_AAVEUSD"),
    ("UNIUSD", "UNI", 3.96, 0.001, 0.01, 0.01, 70.0, 20, "C", "PF_UNIUSD"),
]

MARKETS: dict[str, MarketCfg] = {}
for _s, _b, _p, _tk, _st, _mq, _qb, _kp in _SPOT_SEEDS:
    MARKETS[_s] = MarketCfg(
        symbol=_s, kind="spot", base=_b, tick=_tk, qty_step=_st,
        min_qty=_mq, qbase=_qb, max_lev=1, tier_class="-", seed_price=_p,
        kraken_spot=_kp, min_notional=1.0,
    )
for _s, _b, _p, _tk, _st, _mq, _qb, _ml, _tc, _kf in _LINEAR_SEEDS:
    MARKETS[_s] = MarketCfg(
        symbol=_s, kind="linear", base=_b, tick=_tk, qty_step=_st,
        min_qty=_mq, qbase=_qb, max_lev=_ml, tier_class=_tc, seed_price=_p,
        kraken_fut=_kf, min_notional=5.0,
        kraken_spot={"BTC": "XBTUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
                     "XRP": "XRPUSDT", "DOGE": "XDGUSDT"}.get(_b, ""),
    )

# Fast lookup tables for the v5 layer.
SPOT_V5_MAP = {m.v5_symbol: s for s, m in MARKETS.items() if m.kind == "spot"}
LINEAR_V5_MAP = {m.v5_symbol: s for s, m in MARKETS.items() if m.kind == "linear"}

PERP_UNDERLYING = {  # linear -> spot market used for kline/index fallback
    "BTCUSD": "BTC/USDT", "ETHUSD": "ETH/USDT", "SOLUSD": "SOL/USDT",
    "XRPUSD": "XRP/USDT", "DOGEUSD": "DOGE/USDT",
}


def resolve_symbol(category: str, v5_symbol: str) -> str | None:
    """Resolve a Bybit-style (category, symbol) pair to the internal symbol."""
    if category == "spot":
        return SPOT_V5_MAP.get(v5_symbol)
    if category in ("linear",):
        return LINEAR_V5_MAP.get(v5_symbol)
    return None


def fees_for(kind: str) -> tuple[float, float]:
    """Return (maker_rate, taker_rate) for a market kind."""
    if kind == "spot":
        return SPOT_MAKER_FEE, SPOT_TAKER_FEE
    return LINEAR_MAKER_FEE, LINEAR_TAKER_FEE


def tiers_for(m: MarketCfg) -> list[tuple[float, int, float]]:
    """Risk-limit tiers for a market (spot has none)."""
    return RISK_TIERS.get(m.tier_class, RISK_TIERS["C"])


def tier_for_notional(m: MarketCfg, notional: float) -> tuple[float, int, float]:
    """Return the (maxNotional, maxLev, mmRate) tier that covers `notional`."""
    for t in tiers_for(m):
        if notional <= t[0]:
            return t
    return tiers_for(m)[-1]
