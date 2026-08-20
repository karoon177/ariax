# -*- coding: utf-8 -*-
"""
M9/M11 (persistence): SQLAlchemy Core schema + ordered write-behind queue.

Design notes
------------
* One schema works on both SQLite (dev / ephemeral free tier) and
  PostgreSQL (Render free Postgres) — no dialect-specific SQL is used.
* The trading engine mutates in-memory state only; durability is provided
  by `Persister`, a single ordered consumer that applies write-through
  callbacks sequentially. This keeps REST latency in-memory (<1 ms) while
  preserving a consistent commit order.
* Historical queries (order history, executions, transaction log) read
  the database directly.
"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from . import config

meta = sa.MetaData()

t_users = sa.Table(
    "users", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("email", sa.Text, unique=True, nullable=False),
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column("pass_hash", sa.Text, nullable=False),
    sa.Column("salt", sa.Text, nullable=False),
    sa.Column("totp_secret", sa.Text, nullable=True),
    sa.Column("totp_enabled", sa.Integer, nullable=False, default=0),
    sa.Column("created_ms", sa.BigInteger, nullable=False),
)

t_sessions = sa.Table(
    "sessions", meta,
    sa.Column("token_hash", sa.Text, primary_key=True),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("expires_ms", sa.BigInteger, nullable=False),
)

t_api_keys = sa.Table(
    "api_keys", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("key_hash", sa.Text, unique=True, nullable=False),
    sa.Column("key_plain", sa.Text, nullable=False, default=""),
    sa.Column("key_prefix", sa.Text, nullable=False),
    sa.Column("secret_enc", sa.Text, nullable=False),   # AES-256-GCM payload
    sa.Column("permissions", sa.Text, nullable=False, default='["readTrade"]'),
    sa.Column("ips", sa.Text, nullable=False, default=""),
    sa.Column("label", sa.Text, nullable=False, default=""),
    sa.Column("created_ms", sa.BigInteger, nullable=False),
    sa.Column("revoked", sa.Integer, nullable=False, default=0),
    sa.Column("last_used_ms", sa.BigInteger, nullable=True),
)

t_balances = sa.Table(
    "balances", meta,
    sa.Column("uid", sa.Integer, primary_key=True),
    sa.Column("asset", sa.Text, primary_key=True),
    sa.Column("free", sa.Float, nullable=False, default=0.0),
)

t_futures_balances = sa.Table(
    "futures_balances", meta,
    sa.Column("uid", sa.Integer, primary_key=True),
    sa.Column("asset", sa.Text, primary_key=True),
    sa.Column("free", sa.Float, nullable=False, default=0.0),
)

t_orders = sa.Table(
    "orders", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),  # legacy int id
    sa.Column("order_id", sa.Text, unique=True, nullable=False),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("order_type", sa.Text, nullable=False),
    sa.Column("tif", sa.Text, nullable=False, default="GTC"),
    sa.Column("price", sa.Float, nullable=False, default=0.0),
    sa.Column("qty", sa.Float, nullable=False),
    sa.Column("filled_qty", sa.Float, nullable=False, default=0.0),
    sa.Column("avg_price", sa.Float, nullable=False, default=0.0),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("reduce_only", sa.Integer, nullable=False, default=0),
    sa.Column("close_on_trigger", sa.Integer, nullable=False, default=0),
    sa.Column("trigger_price", sa.Float, nullable=True),
    sa.Column("trigger_by", sa.Text, nullable=True),
    sa.Column("tp_price", sa.Float, nullable=True),
    sa.Column("sl_price", sa.Float, nullable=True),
    sa.Column("oco_id", sa.Text, nullable=True),
    sa.Column("leverage", sa.Integer, nullable=False, default=0),
    sa.Column("order_link_id", sa.Text, nullable=True),
    sa.Column("canceled_reason", sa.Text, nullable=True),
    sa.Column("created_ms", sa.BigInteger, nullable=False),
    sa.Column("updated_ms", sa.BigInteger, nullable=False),
)

t_executions = sa.Table(
    "executions", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("exec_id", sa.Text, unique=True, nullable=False),
    sa.Column("order_id", sa.Text, nullable=False),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("qty", sa.Float, nullable=False),
    sa.Column("fee", sa.Float, nullable=False),
    sa.Column("is_maker", sa.Integer, nullable=False),
    sa.Column("exec_type", sa.Text, nullable=False),    # Taker|Maker|Liquidation|Funding|Settlement
    sa.Column("created_ms", sa.BigInteger, nullable=False),
)

t_positions = sa.Table(
    "positions_persist", meta,
    sa.Column("uid", sa.Integer, primary_key=True),
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("size", sa.Float, nullable=False),
    sa.Column("entry", sa.Float, nullable=False),
    sa.Column("leverage", sa.Integer, nullable=False),
    sa.Column("margin", sa.Float, nullable=False),
    sa.Column("tp", sa.Float, nullable=True),
    sa.Column("sl", sa.Float, nullable=True),
    sa.Column("trailing", sa.Float, nullable=True),
    sa.Column("updated_ms", sa.BigInteger, nullable=False),
)

t_ledger = sa.Table(
    "ledger", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("asset", sa.Text, nullable=False),
    sa.Column("amount", sa.Float, nullable=False),
    sa.Column("note", sa.Text, nullable=False, default=""),
    sa.Column("ts_ms", sa.BigInteger, nullable=False),
)

t_funding = sa.Table(
    "funding_history", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("rate", sa.Float, nullable=False),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("ts_ms", sa.BigInteger, nullable=False),
)

t_klines = sa.Table(
    "kline_cache", meta,
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("interval", sa.Integer, primary_key=True),
    sa.Column("ts", sa.BigInteger, primary_key=True),
    sa.Column("o", sa.Float), sa.Column("h", sa.Float),
    sa.Column("l", sa.Float), sa.Column("c", sa.Float),
    sa.Column("v", sa.Float),
)

t_backtests = sa.Table(
    "backtests", meta,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("uid", sa.Integer, nullable=False),
    sa.Column("params", sa.Text, nullable=False),
    sa.Column("result", sa.Text, nullable=False),
    sa.Column("created_ms", sa.BigInteger, nullable=False),
)

t_security = sa.Table(
    "security_events", meta,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("detail", sa.Text, nullable=False, default=""),
    sa.Column("ip", sa.Text, nullable=False, default=""),
    sa.Column("ts_ms", sa.BigInteger, nullable=False),
)

t_meta = sa.Table(
    "meta", meta,
    sa.Column("k", sa.Text, primary_key=True),
    sa.Column("v", sa.Text, nullable=False),
)

t_faucet = sa.Table(
    "faucet_claims", meta,
    sa.Column("uid", sa.Integer, primary_key=True),
    sa.Column("last_ms", sa.BigInteger, nullable=False),
)


class Database:
    """Async database facade shared by every module."""

    def __init__(self, url: str):
        if url.startswith("sqlite"):
            os.makedirs("data", exist_ok=True)
        self.engine = create_async_engine(url, echo=False, pool_pre_ping=True)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(meta.create_all)

    def session(self) -> AsyncSession:
        return self.sessionmaker()


class Persister:
    """Ordered single-consumer write-behind queue (engine -> database).

    Callbacks are sync functions receiving an `AsyncSession`-free raw
    connection via sqlalchemy sync-style execution on the async engine's
    connection. Keeping a single consumer guarantees that balance /
    position writes are applied in the same order the engine produced
    them, which matters after crash recovery.
    """

    def __init__(self, db: Database):
        self.db = db
        self.queue: asyncio.Queue[Callable[[AsyncSession], Awaitable[None]] | None] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="persister")

    async def stop(self) -> None:
        await self.queue.put(None)
        if self._task:
            await asyncio.wait_for(self._task, timeout=10)

    def submit(self, fn: Callable[[AsyncSession], Awaitable[None]]) -> None:
        """Enqueue a write callback (never blocks; drops oldest on overflow)."""
        try:
            self.queue.put_nowait(fn)
        except asyncio.QueueFull:  # pragma: no cover - safety valve
            pass

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                break
            try:
                async with self.db.session() as sess:
                    async with sess.begin():
                        await item(sess)
            except Exception as exc:  # keep the writer alive on bad writes
                import logging
                logging.getLogger("ariax.db").exception("persist error: %s", exc)
