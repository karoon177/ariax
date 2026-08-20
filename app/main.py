# -*- coding: utf-8 -*-
"""
AriaX Testnet Exchange v2 — application assembly.

Boot sequence (lifespan):
  1. create database schema (SQLite or PostgreSQL via DATABASE_URL);
  2. load durable state (wallets, positions, open orders -> books,
     API keys, order sequence, insurance pool, leverage settings);
  3. start the ordered write-behind Persister;
  4. launch engine loops: reference feed, tape, market maker,
     risk/triggers, funding, bots, WebSocket delta pump, agents.

Transports:
  /v5/*  — Bybit v5 compatible REST (signed where private)
  /v5/public/ws, /v5/private/ws — Bybit v5 WebSocket protocol
  /api/*, /ws — v1 legacy UI protocol (full compatibility)
  /      — static trading UI (RTL Persian)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import agents, config, db as dbm, events, security, users, util
from . import runtime
from .api import compat, v5_account, v5_extra, v5_market, v5_trade, v5_transfer
from .api.deps import SESSION_DB_LOADER
from .engine import orderbook
from .engine.funding import funding_loop
from .engine.risk import risk_loop
from .errors import ApiError
from .marketdata import feeds, mm
from .marketdata.klines import seed_all
from .state import Order, STATE
from .ws import hub as ws_hub

log = logging.getLogger("ariax")


# --------------------------------------------------------------------------- #
# Durable state loader                                                         #
# --------------------------------------------------------------------------- #
async def load_state(database: dbm.Database) -> None:
    """Rehydrate the engine from the database after a restart/deploy."""
    from sqlalchemy import func, select

    async with database.session() as sess:
        # --- users count ------------------------------------------------- #
        n_users = (await sess.execute(select(func.count())
                                      .select_from(dbm.t_users))).scalar() or 1
        STATE.stats["users"] = n_users

        # --- wallets ------------------------------------------------------ #
        res = await sess.execute(select(dbm.t_balances))
        for row in res.mappings():
            acct = STATE.account(row["uid"])
            if row["free"]:
                acct.balances[row["asset"]] = row["free"]
        res = await sess.execute(select(dbm.t_futures_balances))
        for row in res.mappings():
            acct = STATE.account(row["uid"])
            if row["free"]:
                acct.fbalances[row["asset"]] = row["free"]

        # --- positions ---------------------------------------------------- #
        res = await sess.execute(select(dbm.t_positions))
        for row in res.mappings():
            if row["size"] and row["size"] != 0:
                STATE.positions[(row["uid"], row["symbol"])] = \
                    type("P", (), {})() if False else _pos_from_row(row)

        # --- leverage map -------------------------------------------------- #
        res = await sess.execute(select(dbm.t_meta))
        for row in res.mappings():
            if row["k"] == "insurance_pool":
                STATE.insurance_pool = float(row["v"])
            elif row["k"].startswith("leverage_"):
                try:
                    for key, lev in json.loads(row["v"]).items():
                        u, s = key.split("|")
                        STATE.leverage[(int(u), s)] = int(lev)
                except Exception:
                    pass

        # --- open orders ---------------------------------------------------- #
        res = await sess.execute(
            select(dbm.t_orders)
            .where(dbm.t_orders.c.status.in_(
                ("New", "PartiallyFilled", "Untriggered"))))
        max_id = 0
        for row in res.mappings():
            max_id = max(max_id, row["id"])
            o = _order_from_row(row)
            if o.status == "Untriggered":
                STATE.conditional[o.id] = o
            else:
                orderbook.book(o.symbol).add(o)
                STATE.open_orders[o.id] = o
                _rehypothecate(o)
            if o.order_link_id:
                STATE.orders_by_link[(o.uid, o.order_link_id)] = o.id
        STATE.order_seq = max(STATE.order_seq, max_id)

        # --- order sequence high-water mark --------------------------------- #
        seq = (await sess.execute(select(func.max(dbm.t_orders.c.id)))).scalar()
        STATE.order_seq = max(STATE.order_seq, seq or 0)

        # --- api keys --------------------------------------------------------- #
        res = await sess.execute(select(dbm.t_api_keys)
                                 .where(dbm.t_api_keys.c.revoked == 0))
        for row in res.mappings():
            try:
                secret = security.decrypt_secret(row["secret_enc"])
                STATE.api_keys[row["key_hash"]] = dict(
                    id=row["id"], uid=row["uid"], key_hash=row["key_hash"],
                    key=row["key_plain"], secret=secret, label=row["label"],
                    permissions=json.loads(row["permissions"] or '["readTrade"]'),
                    ips=row["ips"], created_ms=row["created_ms"], revoked=False)
            except Exception:
                log.warning("failed to decrypt api key id=%s", row["id"])

        # --- faucet timestamps ------------------------------------------------- #
        res = await sess.execute(select(dbm.t_faucet))
        for row in res.mappings():
            STATE.faucet[row["uid"]] = row["last_ms"] / 1000.0

    # One-time migration for legacy unified accounts: users with open
    # positions move all USDT to the futures wallet so margin/fees/funding
    # keep settling correctly under the new dual-wallet layout.
    users_with_positions = {u for (u, _) in STATE.positions}
    for uid in users_with_positions:
        acct = STATE.accounts.get(uid)
        if not acct:
            continue
        if acct.ffree("USDT") <= 0 and acct.free("USDT") > 0:
            moved = acct.transfer(acct.available("USDT"), "USDT",
                                  "spot_to_futures")
            if moved > 0:
                log.info("migrated %s USDT to futures wallet for uid %s",
                         moved, uid)


def _pos_from_row(row):
    from .state import Position
    return Position(uid=row["uid"], symbol=row["symbol"], size=row["size"],
                    entry=row["entry"], leverage=row["leverage"],
                    margin=row["margin"], tp=row["tp"], sl=row["sl"],
                    trailing=row["trailing"], updated_ms=row["updated_ms"])


def _order_from_row(row) -> Order:
    return Order(
        uid=row["uid"], symbol=row["symbol"], category=row["category"],
        side=row["side"], order_type=row["order_type"], qty=row["qty"],
        price=row["price"], tif=row["tif"] or "GTC",
        reduce_only=bool(row["reduce_only"]),
        close_on_trigger=bool(row["close_on_trigger"]),
        trigger_price=row["trigger_price"], trigger_by=row["trigger_by"] or "LastPrice",
        tp_price=row["tp_price"], sl_price=row["sl_price"],
        leverage=row["leverage"] or 0, order_link_id=row["order_link_id"],
        id=row["id"], order_id=row["order_id"],
        filled_qty=row["filled_qty"], avg_price=row["avg_price"],
        status=row["status"], created_ms=row["created_ms"],
        updated_ms=row["updated_ms"],
        est_price=row["price"] if row["price"] else 0.0,
        mkt_cap=(row["price"] * 1.05) if row["order_type"] == "Market" and row["price"]
        else 0.0,
    )


def _rehypothecate(o: Order) -> None:
    """Re-reserve balances for a resting order after restart."""
    if o.uid <= 0:
        return
    from .engine import orders as oms
    if o.category == "linear":
        pos = STATE.position(o.uid, o.symbol)
        opening = not pos or pos.size == 0 or (pos.size > 0) == (o.side == "Buy")
        if opening and not o.reduce_only:
            lev = max(1, o.leverage or STATE.leverage_for(o.uid, o.symbol))
            need = o.price * o.leaves / lev * 1.05 + \
                o.price * o.leaves * config.LINEAR_TAKER_FEE
            STATE.account(o.uid).add_hold(o.id, "USDT", need)
        else:
            STATE.close_locks[(o.uid, o.symbol)] = \
                STATE.close_locks.get((o.uid, o.symbol), 0.0) + o.leaves
    else:
        if o.side == "Buy":
            STATE.account(o.uid).add_hold(
                o.id, "USDT", o.price * o.leaves * (1 + config.SPOT_TAKER_FEE))
        else:
            STATE.account(o.uid).add_hold(
                o.id, config.MARKETS[o.symbol].base, o.leaves)
    _ = oms


async def _session_loader(token: str) -> int | None:
    """DB-backed session resolution for the legacy WS + REST layers."""
    from sqlalchemy import select
    database = runtime.get_db()
    if not database:
        return None
    try:
        async with database.session() as sess:
            row = (await sess.execute(
                select(dbm.t_sessions.c.uid)
                .where(dbm.t_sessions.c.token_hash == users._tok_hash(token))
            )).first()
            return row[0] if row else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# App factory                                                                  #
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    app = FastAPI(title="AriaX Testnet Exchange", version="2.0",
                  docs_url="/docs", redoc_url=None,
                  openapi_url="/openapi.json")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"], expose_headers=["*"])

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        path = request.url.path
        if path.startswith("/v5"):
            return JSONResponse(
                status_code=exc.http_status,
                content={"retCode": exc.ret_code, "retMsg": exc.ret_msg,
                         "result": {}, "retExtInfo": {},
                         "time": util.now_ms()})
        return JSONResponse(status_code=exc.http_status,
                            content={"ok": False, "error": exc.ret_msg})

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        if request.url.path.startswith("/v5"):
            return JSONResponse(
                status_code=500,
                content={"retCode": 10016, "retMsg": "server error",
                         "result": {}, "retExtInfo": {},
                         "time": util.now_ms()})
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": "خطای سرور"})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.on_event("startup")
    async def startup() -> None:
        database = dbm.Database(config.DATABASE_URL)
        runtime.set_db(database)
        await database.create_all()
        orderbook.build_books()
        seed_all()
        agents.init()
        await load_state(database)
        persister = dbm.Persister(database)
        persister.start()
        runtime.set_persister(persister)
        # engine 'persist' events -> ordered write-behind queue
        events.BUS.on("persist", lambda fn: persister.submit(fn))
        ws_hub.wire()
        ws_hub.SESSION_LOADER = _session_loader
        import app.api.deps as _deps
        _deps.SESSION_DB_LOADER = _session_loader
        for coro, name in (
                (feeds.kraken_feed_loop(), "kraken-feed"),
                (feeds.tape_loop(), "tape"),
                (feeds.thinktank_loop(), "thinktank"),
                (feeds.oversight_loop(), "oversight"),
                (mm.mm_loop(), "mm"),
                (risk_loop(), "risk"),
                (funding_loop(), "funding"),
                (agents.bot_loop(), "bot"),
                (ws_hub.book_delta_pump(), "ws-delta")):
            asyncio.get_running_loop().create_task(coro, name=name)
        log.info("AriaX v2 started: %s markets, %s users",
                 len(config.MARKETS), STATE.stats["users"])

    @app.on_event("shutdown")
    async def shutdown() -> None:
        persister = runtime.get_persister()
        if persister:
            await persister.stop()

    # ---- REST routers ---------------------------------------------------- #
    app.include_router(v5_market.router)
    app.include_router(v5_trade.router)
    app.include_router(v5_account.router)
    app.include_router(v5_transfer.router)
    app.include_router(v5_extra.router)
    app.include_router(compat.router)

    # ---- WebSocket endpoints ---------------------------------------------- #
    @app.websocket("/v5/public/ws")
    async def ws_public(ws: WebSocket):
        await ws_hub.serve(ws, legacy=False)

    @app.websocket("/v5/private/ws")
    async def ws_private(ws: WebSocket):
        await ws_hub.serve(ws, legacy=False)

    @app.websocket("/ws")
    async def ws_legacy(ws: WebSocket):
        await ws_hub.serve(ws, legacy=True)

    # ---- health & static --------------------------------------------------- #
    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "time": util.now_ms()}

    static_dir = Path(__file__).resolve().parent.parent / "static"

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/app.js")
    async def app_js():
        return FileResponse(static_dir / "app.js",
                            media_type="application/javascript; charset=utf-8")

    @app.get("/style.css")
    async def style_css():
        return FileResponse(static_dir / "style.css",
                            media_type="text/css; charset=utf-8")

    @app.get("/favicon.ico")
    async def favicon():
        return JSONResponse({}, status_code=204)

    return app


app = create_app()
