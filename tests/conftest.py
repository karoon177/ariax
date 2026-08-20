# -*- coding: utf-8 -*-
"""Shared test configuration: isolated DB + env before app import."""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./data/test_exchange.db"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["FAUCET_COOLDOWN_HOURS"] = "24"
os.environ.setdefault("ARIAX_MASTER_KEY", "")

for stale in (ROOT / "data" / "test_exchange.db",):
    if stale.exists():
        stale.unlink()


def reset_state():
    """Clear all in-memory trading state and rebuild books (unit tests)."""
    from app.engine import orderbook
    from app.state import STATE
    STATE.accounts.clear()
    STATE.positions.clear()
    STATE.open_orders.clear()
    STATE.conditional.clear()
    STATE.orders_by_link.clear()
    STATE.leverage.clear()
    STATE.close_locks.clear()
    STATE.bots.clear()
    STATE.api_keys.clear()
    STATE.sessions.clear()
    STATE.force_price.clear()
    orderbook.build_books()
    # anchor reference prices near the values used by the tests
    anchors = {"BTC/USDT": 100_000.0, "BTCUSD": 100_000.0,
               "ETH/USDT": 3_000.0, "ETHUSD": 3_000.0,
               "SOL/USDT": 150.0, "SOLUSD": 150.0,
               "XRP/USDT": 1.0, "XRPUSD": 1.0,
               "DOGE/USDT": 0.1, "DOGEUSD": 0.1}
    for symbol, t in STATE.markets.items():
        px = anchors.get(symbol, t.last)
        t.last = t.mark = t.index = t.open24 = px
        t.high24 = t.low24 = px
    return STATE
