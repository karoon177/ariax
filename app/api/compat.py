# -*- coding: utf-8 -*-
"""
Legacy v1 HTTP API — byte-for-byte compatible with the original UI.

Every endpoint the shipped UI (static/app.js) calls is reimplemented on
top of the new engine with identical response shapes, so existing users
see no breakage. New capabilities are exposed additively:
    /api/auth/2fa/{setup,confirm}, /api/tpsl, /api/leverage
The UI protocol on /ws is handled by ws.hub (legacy mode).
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request

from .. import agents, config, security, users, util
from ..api import deps
from ..api.serializers import position_legacy
from ..engine import matching, orders as oms
from ..engine import orderbook
from ..errors import ApiError
from ..runtime import get_db, get_persister
from ..state import STATE

router = APIRouter(prefix="/api", tags=["legacy"])


async def _json(request: Request) -> dict:
    try:
        return json.loads((await request.body()).decode() or "{}")
    except Exception:
        return {}


async def _uid(request: Request) -> int:
    return await deps.require_session(request)


def _err(msg: str, status: int = 200):
    return {"ok": False, "error": msg}


INTERVAL_MAP = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}


# --------------------------------------------------------------------------- #
# Public market data                                                           #
# --------------------------------------------------------------------------- #
@router.get("/markets")
async def markets():
    from .. import runtime
    from ..api.serializers import ticker_legacy
    ref = dict(STATE.reference)
    ref["prices"] = len(ref.get("prices", {}))
    if ref.get("updated"):
        ref["age"] = round(time.time() - ref["updated"], 2)
    return {"ok": True,
            "data": {s: ticker_legacy(s, STATE.tick(s))
                     for s in config.MARKETS},
            "reference": ref,
            "db": runtime.db_info()}


@router.get("/config")
async def config_endpoint():
    data = {}
    for s, m in config.MARKETS.items():
        data[s] = dict(base=m.base, kind=m.kind, price=m.seed_price,
                       vol=0.002, tick=m.tick, step=m.qty_step,
                       minq=m.min_qty, qbase=m.qbase, maxlev=m.max_lev)
    return {"ok": True, "data": data, "assets": config.LISTED_ASSETS,
            "fees": dict(taker=config.LINEAR_TAKER_FEE,
                         maker=config.LINEAR_MAKER_FEE,
                         liq=config.LIQUIDATION_FEE_RATE,
                         spot_taker=config.SPOT_TAKER_FEE,
                         spot_maker=config.SPOT_MAKER_FEE),
            "fundingIntervalHours": config.FUNDING_INTERVAL_H,
            "faucetCooldownHours": config.FAUCET_COOLDOWN_HOURS}


@router.get("/book")
async def book(symbol: str = "BTC/USDT"):
    if symbol not in config.MARKETS:
        return _err("نماد نامعتبر")
    snap = orderbook.book(symbol).depth(15)
    return {"ok": True, "data": {
        "bids": [[float(p), float(q)] for p, q in snap["b"]],
        "asks": [[float(p), float(q)] for p, q in snap["a"]],
        "ts": snap["ts"] / 1000.0, "last": STATE.tick(symbol).last}}


@router.get("/trades")
async def trades(symbol: str = "BTC/USDT"):
    m = STATE.markets.get(symbol)
    if not m:
        return _err("نماد نامعتبر")
    return {"ok": True, "data": [[round(t[0] / 1000.0, 2),
                                  str(t[1]).lower(), t[2], t[3]]
                                 for t in list(m.trades)[:100]]}


@router.get("/candles")
async def candles(symbol: str = "BTC/USDT", interval: str = "1m"):
    if symbol not in config.MARKETS:
        return _err("نماد نامعتبر")
    iv = INTERVAL_MAP.get(interval, "1")
    from ..marketdata import klines
    rows, source, stale = await klines.get_klines(symbol, iv, 500)
    data = [[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in reversed(rows)]
    return {"ok": True, "data": data, "source": source, "stale": stale}


# --------------------------------------------------------------------------- #
# Auth                                                                         #
# --------------------------------------------------------------------------- #
@router.post("/auth/register")
async def auth_register(request: Request):
    b = await _json(request)
    email = (b.get("email") or "").strip()
    password = b.get("password") or ""
    name = (b.get("name") or "").strip()
    if "@" not in email or len(password) < 4:
        return _err("ایمیل معتبر و رمز حداقل ۴ کاراکتر لازم است")
    if len(password) < 6:
        return _err("برای امنیت بیشتر، رمز حداقل ۶ کاراکتر باشد")
    try:
        uid = await users.register_user(get_db(), get_persister(),
                                        email, password, name)
    except ApiError:
        return _err("این ایمیل قبلاً ثبت شده است")
    token = users.new_session_token()
    users.cache_session(token, uid)
    get_persister().submit(users.session_persist_fn(token, uid))
    agents.agent_log("support",
                     f"کاربر جدید #{uid} ثبت‌نام کرد — پاداش ۲۰,۰۰۰ USDT واریز شد")
    return {"ok": True, "token": token, "uid": uid}


@router.post("/auth/login")
async def auth_login(request: Request):
    b = await _json(request)
    email = b.get("email") or ""
    password = b.get("password") or ""
    try:
        row = await users.authenticate(get_db(), email, password,
                                       b.get("otp"))
    except ApiError:
        return {"ok": False, "need_otp": True,
                "error": "کد تأیید دومرحله‌ای (2FA) لازم است یا نامعتبر است"}
    if not row:
        return _err("ایمیل یا رمز عبور اشتباه است")
    token = users.new_session_token()
    users.cache_session(token, row["id"])
    get_persister().submit(users.session_persist_fn(token, row["id"]))
    return {"ok": True, "token": token, "uid": row["id"]}


@router.post("/auth/logout")
async def auth_logout(request: Request):
    token = deps.extract_token(request)
    STATE.sessions.pop(token, None)
    if token:
        from .. import db

        async def _delete(session) -> None:
            await session.execute(
                db.t_sessions.delete().where(
                    db.t_sessions.c.token_hash == users._tok_hash(token)))

        get_persister().submit(_delete)
    return {"ok": True}


@router.post("/auth/2fa/setup")
async def twofa_setup(request: Request):
    uid = await _uid(request)
    secret, uri = await users.start_2fa(get_db(), uid)
    return {"ok": True, "secret": secret, "uri": uri}


@router.post("/auth/2fa/confirm")
async def twofa_confirm(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    try:
        await users.confirm_2fa(get_db(), uid, b.get("code") or "")
    except ApiError as exc:
        return _err(exc.ret_msg)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Trading                                                                      #
# --------------------------------------------------------------------------- #
@router.post("/order")
async def place_order(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    symbol = b.get("symbol")
    side = (b.get("side") or "").lower()
    typ = (b.get("type") or "").lower()
    lev = b.get("lev", None)
    try:
        o = oms.place_order(
            uid=uid, symbol=symbol, side=side, order_type=typ,
            qty=float(b.get("qty", 0) or 0),
            price=float(b["price"]) if b.get("price") not in (None, "", 0, "0")
            else None,
            leverage=int(lev) if lev else None)
        return {"ok": True, "id": o.id}
    except (ApiError, KeyError, ValueError, TypeError) as exc:
        msg = exc.ret_msg if isinstance(exc, ApiError) else "پارامترهای سفارش نامعتبر است"
        return _err(msg)


@router.post("/cancel")
async def cancel(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    try:
        oid = int(b.get("id", 0))
    except (TypeError, ValueError):
        return _err("شناسه سفارش نامعتبر")
    target = STATE.open_orders.get(oid) or STATE.conditional.get(oid)
    if not target or target.uid != uid:
        return _err("سفارش یافت نشد")
    try:
        o = oms.cancel_order(uid, order_id=target.order_id)
        return {"ok": True, "id": o.id}
    except ApiError as exc:
        return _err(exc.ret_msg)


@router.post("/cancelall")
async def cancelall(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    n = oms.cancel_all(uid, b.get("symbol"))
    return {"ok": True, "n": n}


@router.post("/tpsl")
async def tpsl(request: Request):
    """New: set TP/SL on an open position (UI helper)."""
    uid = await _uid(request)
    b = await _json(request)

    def fv(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    try:
        oms.set_trading_stop(uid, b.get("symbol"), tp=fv(b.get("tp")),
                             sl=fv(b.get("sl")), trailing=fv(b.get("trailing")))
    except ApiError as exc:
        return _err(exc.ret_msg)
    return {"ok": True}


@router.post("/leverage")
async def set_leverage(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    symbol = b.get("symbol")
    if symbol not in config.MARKETS or config.MARKETS[symbol].kind != "linear":
        return _err("نماد فیوچرز انتخاب کنید")
    try:
        lev = int(b.get("lev", 0))
    except (TypeError, ValueError):
        return _err("اهرم نامعتبر")
    cfg = config.MARKETS[symbol]
    if not 1 <= lev <= cfg.max_lev:
        return _err(f"اهرم باید بین ۱ تا {cfg.max_lev} باشد")
    STATE.leverage[(uid, symbol)] = lev
    pos = STATE.position(uid, symbol)
    if pos and pos.size != 0:
        pos.leverage = lev
        matching._persist_position(uid, symbol)
    _persist_leverage(uid)
    return {"ok": True, "leverage": lev}


def _persist_leverage(uid: int) -> None:
    from .. import db
    snap = json.dumps({f"{u}|{s}": lev for (u, s), lev in STATE.leverage.items()
                       if u == uid})

    async def _write(session) -> None:
        res = await session.execute(
            db.t_meta.update().where(db.t_meta.c.k == f"leverage_{uid}")
            .values(v=snap))
        if res.rowcount == 0:
            await session.execute(db.t_meta.insert().values(
                k=f"leverage_{uid}", v=snap))

    get_persister().submit(_write)


@router.get("/orders")
async def open_orders(request: Request):
    uid = await _uid(request)
    from ..api.serializers import order_legacy
    rows = []
    for o in list(STATE.open_orders.values()) + list(STATE.conditional.values()):
        if o.uid == uid:
            rows.append(order_legacy(matching.order_snapshot(o)))
    return {"ok": True, "data": rows}


@router.get("/positions")
async def positions(request: Request):
    uid = await _uid(request)
    rows = []
    for (u, s), pos in sorted(STATE.positions.items()):
        if u == uid and pos.size != 0:
            row = position_legacy(u, s, pos)
            rows.append(row)
    return {"ok": True, "data": rows}


# --------------------------------------------------------------------------- #
# Wallet                                                                       #
# --------------------------------------------------------------------------- #
@router.get("/wallet")
async def wallet(request: Request):
    uid = await _uid(request)
    acct = STATE.account(uid)
    bals = {a: round(acct.free(a), 8) for a in config.LISTED_ASSETS}
    locks = {a: round(acct.held(a), 8) for a in config.LISTED_ASSETS}
    locks["MARGIN"] = round(STATE.margin_used(uid), 4)
    fbals = {"USDT": round(acct.ffree("USDT"), 8)}
    flocks = {"USDT": round(acct.fheld("USDT"), 8)}
    equity = STATE.equity_usdt(uid)
    return {"ok": True, "balances": bals, "locks": locks,
            "spot": {"balances": bals, "locks": locks},
            "futures": {"balances": fbals, "locks": flocks},
            "margin_used": round(STATE.margin_used(uid), 4),
            "free_margin": round(STATE.free_margin(uid), 4),
            "equity": round(equity, 4)}


@router.post("/transfer")
async def transfer_funds(request: Request):
    """Move real funds between the Spot and Futures wallets (UI)."""
    uid = await _uid(request)
    b = await _json(request)
    frm = (b.get("from") or "").lower()
    to = (b.get("to") or "").lower()
    if {frm, to} != {"spot", "futures"}:
        return _err("انتقال فقط بین اسپات و فیوچرز ممکن است")
    try:
        amount = float(b.get("amount", 0))
    except (TypeError, ValueError):
        return _err("مبلغ نامعتبر است")
    if amount <= 0:
        return _err("مبلغ باید مثبت باشد")
    acct = STATE.account(uid)
    direction = "spot_to_futures" if frm == "spot" else "futures_to_spot"
    source_avail = acct.available("USDT") if frm == "spot" else acct.favailable("USDT")
    if source_avail < amount - 1e-9:
        wallet_name = "اسپات" if frm == "spot" else "فیوچرز"
        return _err(f"موجودی قابل انتقال کیف {wallet_name} کافی نیست")
    moved = acct.transfer(amount, "USDT", direction)
    if moved <= 0:
        return _err("انتقال انجام نشد")
    from ..engine import matching
    matching.ledger(uid, "transfer", "USDT", moved,
                    f"{'اسپات' if frm == 'spot' else 'فیوچرز'}→"
                    f"{'اسپات' if to == 'spot' else 'فیوچرز'} {moved:.4f} USDT")
    matching._persist_balances(uid, ["USDT"])
    return {"ok": True, "moved": round(moved, 8),
            "spot": round(acct.free("USDT"), 8),
            "futures": round(acct.ffree("USDT"), 8)}


@router.post("/faucet")
async def faucet(request: Request):
    uid = await _uid(request)
    try:
        amount = users.grant_faucet(uid, get_persister())
    except ApiError:
        ok, wait = users.faucet_ready(uid)
        hours = int(wait // 3600)
        mins = int((wait % 3600) // 60)
        return _err(f"فاست هر ۲۴ ساعت یک‌بار فعال است؛ "
                    f"حدود {hours} ساعت و {mins} دقیقه صبر کنید")
    return {"ok": True, "amount": amount}


@router.post("/wallet/deposit")
async def deposit(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    try:
        txid = users.deposit(uid, b.get("asset"), float(b.get("amount", 0)),
                             b.get("network", "TRC20"), get_persister())
    except (ApiError, TypeError, ValueError) as exc:
        msg = exc.ret_msg if isinstance(exc, ApiError) else "دارایی یا مبلغ نامعتبر"
        return _err(msg)
    return {"ok": True, "txid": txid}


@router.post("/wallet/withdraw")
async def withdraw(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    try:
        txid = users.withdraw(uid, b.get("asset"), float(b.get("amount", 0)),
                              b.get("address", ""), get_persister())
    except (ApiError, TypeError, ValueError) as exc:
        msg = exc.ret_msg if isinstance(exc, ApiError) else "دارایی یا مبلغ نامعتبر"
        return _err(msg)
    return {"ok": True, "txid": txid}


@router.get("/ledger")
async def ledger_rows(request: Request):
    uid = await _uid(request)
    from .. import db
    from sqlalchemy import select
    async with get_db().session() as sess:
        res = await sess.execute(
            select(db.t_ledger).where(db.t_ledger.c.uid == uid)
            .order_by(db.t_ledger.c.id.desc()).limit(60))
        rows = res.mappings().all()
    return {"ok": True, "data": [dict(type=r["type"], asset=r["asset"],
                                      amount=r["amount"], note=r["note"],
                                      ts=r["ts_ms"] / 1000.0) for r in rows]}


@router.get("/fills")
async def fills(request: Request):
    uid = await _uid(request)
    from .. import db
    from sqlalchemy import select
    async with get_db().session() as sess:
        res = await sess.execute(
            select(db.t_executions).where(db.t_executions.c.uid == uid)
            .order_by(db.t_executions.c.id.desc()).limit(100))
        rows = res.mappings().all()
    return {"ok": True, "data": [dict(id=r["id"], symbol=r["symbol"],
                                      side=r["side"].lower(), price=r["price"],
                                      qty=r["qty"], fee=r["fee"],
                                      ts=r["created_ms"] / 1000.0)
                                 for r in rows]}


@router.get("/performance")
async def performance(request: Request):
    uid = await _uid(request)
    from .. import db
    from sqlalchemy import func, select
    async with get_db().session() as sess:
        fees = (await sess.execute(
            select(func.coalesce(func.sum(db.t_executions.c.fee), 0),
                   func.count())
            .where(db.t_executions.c.uid == uid))).one()
        pnl = (await sess.execute(
            select(func.coalesce(func.sum(db.t_ledger.c.amount), 0))
            .where((db.t_ledger.c.uid == uid) &
                   (db.t_ledger.c.type == "realized_pnl")))).scalar()
    return {"ok": True, "realized_pnl": round(pnl, 6),
            "fees": round(fees[0], 6), "trades": fees[1],
            "net_pnl": round(pnl - fees[0], 6)}


# --------------------------------------------------------------------------- #
# API keys                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/api-keys")
async def api_keys_list(request: Request):
    uid = await _uid(request)
    keys = await users.list_api_keys(get_db(), uid)
    return {"ok": True, "data": keys,
            "auth": "Bybit v5 HMAC: X-BAPI-API-KEY/TIMESTAMP/RECV-WINDOW/SIGNATURE "
                    "(legacy X-API-Key + X-API-Secret still accepted)"}


@router.post("/api-keys/create")
async def api_keys_create(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    perms = ["readTrade", "trade"] if b.get("trade", True) else ["readTrade"]
    rec = await users.create_api_key(get_db(), uid,
                                     b.get("label") or "Trading bot",
                                     perms, b.get("ips") or "")
    return {"ok": True, "key": rec["key"], "secret": rec["secret"],
            "permissions": rec["permissions"],
            "warning": "Secret فقط همین بار نمایش داده می‌شود؛ فقط Testnet"}


@router.post("/api-keys/revoke")
async def api_keys_revoke(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    try:
        await users.revoke_api_key(get_db(), uid, int(b.get("id", 0)))
    except (TypeError, ValueError):
        return _err("شناسه نامعتبر")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# AI dashboard / chat / bot                                                    #
# --------------------------------------------------------------------------- #
@router.get("/ai")
async def ai_dashboard():
    return {"ok": True, "agents": agents.agents_payload(),
            "stats": agents.stats_payload()}


@router.post("/ai/toggle")
async def ai_toggle(request: Request):
    uid = await _uid(request)
    del uid
    b = await _json(request)
    aid = b.get("id")
    if aid in agents.AGENTS:
        agents.AGENTS[aid]["enabled"] = bool(b.get("enabled"))
        agents.agent_log(aid, f"وضعیت ایجنت تغییر کرد: "
                              f"{'فعال ✅' if agents.AGENTS[aid]['enabled'] else 'غیرفعال ⛔'}")
        return {"ok": True}
    return _err("ایجنت یافت نشد")


@router.post("/chat")
async def chat(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    reply = agents.chat_reply(b.get("msg", ""))
    agents.agent_log("support", f"پاسخ به کاربر #{uid}: «{(b.get('msg') or '')[:40]}»")
    return {"ok": True, "reply": reply}


@router.post("/bot")
async def bot(request: Request):
    uid = await _uid(request)
    b = await _json(request)
    action = b.get("action")
    symbol = b.get("symbol", "BTCUSD")
    if symbol not in config.MARKETS or config.MARKETS[symbol].kind != "linear":
        return _err("نماد فیوچرز انتخاب کنید")
    try:
        lev = max(1, min(int(b.get("lev", 5)), 20))
    except (TypeError, ValueError):
        lev = 5
    if action == "start":
        STATE.bots[uid] = dict(active=True, sym=symbol, lev=lev, last_sig=0)
        agents.AGENTS["bot"]["enabled"] = True
        agents.agent_log("bot", f"ربات معامله‌گر برای کاربر #{uid} روی {symbol} "
                                f"با اهرم {lev} فعال شد")
        return {"ok": True}
    if action == "stop":
        if uid in STATE.bots:
            STATE.bots[uid]["active"] = False
        agents.agent_log("bot", f"ربات معامله‌گر کاربر #{uid} متوقف شد")
        return {"ok": True}
    if action == "status":
        st = STATE.bots.get(uid)
        return {"ok": True, "active": bool(st and st.get("active")),
                "sym": st and st["sym"]}
    return _err("اکشن نامعتبر")
