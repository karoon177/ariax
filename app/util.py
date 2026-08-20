# -*- coding: utf-8 -*-
"""
Shared numeric / formatting helpers used across the exchange.

Precision policy: prices and quantities are always snapped to the
instrument tick / step *before* entering the engine, and rendered as
strings via `fmt_price` / `fmt_qty` in API responses (Bybit convention).
"""
from __future__ import annotations

import secrets
import string
import time
from decimal import ROUND_HALF_UP, Decimal


def now_ms() -> int:
    """Current UNIX time in milliseconds (all API timestamps use ms)."""
    return int(time.time() * 1000)


def gen_hex(n: int = 16) -> str:
    """Random hex identifier (used for orderId / execId)."""
    return secrets.token_hex(n)


def gen_order_link_id() -> str:
    """Bybit-style user orderLinkId (alphanumeric, 36 chars max)."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def decimals_of(step: float) -> int:
    """Number of decimal places implied by a tick/step value."""
    d = Decimal(str(step)).normalize()
    exp = d.as_tuple().exponent
    return max(0, -exp)


def snap_to_step(value: float, step: float) -> float:
    """Round `value` to the nearest multiple of `step` (Decimal, no drift)."""
    if step <= 0:
        return value
    v = Decimal(str(value)) / Decimal(str(step))
    return float(int(v.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * Decimal(str(step)))


def fmt(value: float, step: float) -> str:
    """Format a number with exactly the decimals implied by `step`."""
    return f"{value:.{decimals_of(step)}f}"


def fmt_price(value: float, tick: float) -> str:
    return fmt(value, tick)


def fmt_qty(value: float, step: float) -> str:
    return fmt(value, step)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def ema(values: list[float], period: int) -> float:
    """Exponential moving average of a price series (seeded with first value)."""
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float:
    """Wilder's RSI over the last `period` changes of `values`."""
    if len(values) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-period, 0):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)
