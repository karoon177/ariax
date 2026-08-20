# -*- coding: utf-8 -*-
"""
M10 (realtime): unified WebSocket hub speaking BOTH protocols:

  Bybit v5 (new, for bots):
    /v5/public/ws   -> topics tickers.SYM, orderbook.{depth}.SYM,
                       publicTrade.SYM, kline.{iv}.SYM, allLiquidation
    /v5/private/ws  -> auth {op:'auth',args:[key,expires,sig]} then
                       topics order, execution, wallet, position
    control frames: {'op':'ping'} -> {'op':'pong','success':true,'ts':..}
                    {'op':'subscribe'/'unsubscribe','args':[topics]}

  Legacy v1 (existing UI):
    /ws             -> {'op':'auth','token':...}, {'op':'sub','ch':...}
                       channels: tickers | candle:{sym} | trades:{sym} | user
                       server pushes {'ch':..., 'data':...} + 20 s pings.

Delta orderbook stream follows Bybit semantics: snapshot on subscribe,
then delta frames carrying only changed levels with monotonic `u`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time

from fastapi import WebSocket

from .. import config, events, security, util
from ..state import STATE

MAX_QUEUE = 600


class Client:
    """One live WebSocket connection (either protocol)."""

    def __init__(self, ws: WebSocket, legacy: bool):
        self.ws = ws
        self.legacy = legacy
        self.uid: int | None = None
        self.perms: set[str] = set()
        self.topics: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.alive = True
        self.last_pong = time.time()

    def send(self, payload: dict) -> None:
        """Enqueue a frame; a full queue marks the client dead (slow consumer)."""
        if not self.alive:
            return
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.alive = False

    def wants(self, topic: str) -> bool:
        return topic in self.topics


class Hub:
    """Topic registry + event-to-topic translation."""

    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._book_subs: dict[str, set[Client]] = {}   # "SYM:depth" -> clients

    # ------------------------------------------------------------------ #
    def add(self, client: Client) -> None:
        self.clients.add(client)
        STATE.stats["ws_clients"] = len(self.clients)

    def drop(self, client: Client) -> None:
        client.alive = False
        self.clients.discard(client)
        for subs in self._book_subs.values():
            subs.discard(client)
        STATE.stats["ws_clients"] = len(self.clients)

    # ------------------------------------------------------------------ #
    # Bybit v5 topic pushes                                               #
    # ------------------------------------------------------------------ #
    def push_topic(self, topic: str, data: dict) -> None:
        msg = {"topic": topic, "data": data, "ts": util.now_ms(),
               "type": "snapshot"}
        for c in list(self.clients):
            if c.alive and (not c.legacy) and c.wants(topic):
                c.send(msg)

    def push_private(self, uid: int, topic: str, data: dict) -> None:
        msg = {"topic": topic, "data": data, "ts": util.now_ms(),
               "type": "snapshot"}
        for c in list(self.clients):
            if c.alive and (not c.legacy) and c.uid == uid and c.wants(topic):
                c.send(msg)

    # ------------------------------------------------------------------ #
    # Legacy v1 UI channel                                                #
    # ------------------------------------------------------------------ #
    def push_legacy(self, ch: str, data, uid: int | None = None) -> None:
        msg = {"ch": ch, "data": data}
        for c in list(self.clients):
            if c.alive and c.legacy and c.wants(ch):
                if ch == "user" and c.uid != uid:
                    continue
                c.send(msg)

    def broadcast_legacy(self, ch: str, data) -> None:
        self.push_legacy(ch, data)

    # ------------------------------------------------------------------ #
    def book_subscribe(self, client: Client, key: str) -> None:
        self._book_subs.setdefault(key, set()).add(client)

    def book_subscribers(self, key: str) -> set[Client]:
        return set(self._book_subs.get(key, ()))


HUB = Hub()

# Async token->uid resolver injected by app.main (DB-backed session lookup).
SESSION_LOADER = None


# --------------------------------------------------------------------------- #
# Topic name helpers (internal symbol <-> Bybit v5 symbol)                     #
# --------------------------------------------------------------------------- #
def v5_sym(symbol: str) -> str:
    return config.MARKETS[symbol].v5_symbol


def resolve_topic(topic: str) -> str | None:
    """Validate & normalize a v5 topic; None when malformed."""
    parts = topic.split(".")
    if parts[0] == "tickers" and len(parts) == 2:
        for m in config.MARKETS.values():
            if m.v5_symbol == parts[1]:
                return topic
    elif parts[0] == "orderbook" and len(parts) == 3 and parts[1] in ("1", "50", "200"):
        for m in config.MARKETS.values():
            if m.v5_symbol == parts[2]:
                return topic
    elif parts[0] == "publicTrade" and len(parts) == 2:
        for m in config.MARKETS.values():
            if m.v5_symbol == parts[1]:
                return topic
    elif parts[0] in ("allLiquidation", "order", "execution", "wallet", "position"):
        return topic
    elif parts[0] == "kline" and len(parts) == 3:
        return topic
    elif parts[0] == "liquidation" and len(parts) == 2:
        return topic
    return None


# --------------------------------------------------------------------------- #
# Connection handler (shared by both protocols)                                #
# --------------------------------------------------------------------------- #
async def serve(ws: WebSocket, legacy: bool) -> None:
    """Accept and pump one WebSocket connection."""
    await ws.accept()
    client = Client(ws, legacy)
    HUB.add(client)
    sender = asyncio.create_task(_sender(client))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            op = msg.get("op")
            if legacy:
                await _legacy_op(client, msg, op)
            else:
                await _v5_op(client, msg, op)
    except Exception:
        pass
    finally:
        client.alive = False
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        HUB.drop(client)


async def _sender(client: Client) -> None:
    """Drain the client queue into the socket."""
    while client.alive:
        try:
            payload = await asyncio.wait_for(client.queue.get(), timeout=20.0)
            await client.ws.send_json(payload)
        except asyncio.TimeoutError:
            # protocol-level keepalive
            try:
                if client.legacy:
                    await client.ws.send_json({"ch": "ping", "data": time.time()})
                else:
                    await client.ws.send_json({"op": "pong", "success": True,
                                               "ts": util.now_ms()})
            except Exception:
                client.alive = False
                return
        except Exception:
            client.alive = False
            return


async def _v5_op(client: Client, msg: dict, op: str | None) -> None:
    if op == "ping":
        client.send({"op": "pong", "success": True, "ts": util.now_ms()})
    elif op == "auth":
        args = msg.get("args") or []
        if len(args) != 3:
            client.send({"op": "auth", "success": False,
                         "retMsg": "args must be [apiKey, expires, signature]"})
            return
        api_key, expires, sig = args
        rec = None
        for key_hash, r in STATE.api_keys.items():
            if r["key"] == api_key:
                rec = r
                break
        if rec is None or rec.get("revoked"):
            client.send({"op": "auth", "success": False, "retMsg": "invalid api key"})
            return
        if int(expires) < util.now_ms():
            client.send({"op": "auth", "success": False, "retMsg": "expires elapsed"})
            return
        expect = security.ws_auth_signature(rec["secret"], int(expires))
        if expect != sig:
            client.send({"op": "auth", "success": False, "retMsg": "signature mismatch"})
            return
        client.uid = rec["uid"]
        client.perms = set(rec.get("permissions", []))
        client.send({"op": "auth", "success": True, "retMsg": "",
                     "conn_id": util.gen_hex(8)})
    elif op in ("subscribe", "unsubscribe"):
        args = [str(a) for a in (msg.get("args") or [])]
        ok_topics, failed = [], []
        for topic in args:
            norm = resolve_topic(topic)
            private = topic.split(".")[0] in ("order", "execution", "wallet", "position")
            if norm is None or (private and client.uid is None):
                failed.append(topic)
                continue
            if op == "subscribe":
                client.topics.add(norm)
                if norm.startswith("orderbook."):
                    HUB.book_subscribe(client, norm)
            else:
                client.topics.discard(norm)
            ok_topics.append(norm)
        client.send({"op": op, "success": not failed,
                     "retMsg": f"subscribe {len(failed)} topic(s) failed" if failed else "",
                     "conn_id": util.gen_hex(8)})
        # Bybit pushes an initial orderbook snapshot right after subscribe
        for topic in ok_topics:
            if topic.startswith("orderbook."):
                await _book_snapshot_for(client, topic, send=True)


async def _book_snapshot_for(client: Client, topic: str, send: bool = False):
    """Build (and optionally push) the initial orderbook snapshot."""
    _, depth, v5s = topic.split(".")
    symbol = config.resolve_symbol("linear", v5s) or config.resolve_symbol("spot", v5s)
    if not symbol:
        return None
    from ..engine import orderbook
    snap = orderbook.book(symbol).depth(int(depth))
    snap["s"] = v5s
    if send:
        client.send({"topic": topic, "type": "snapshot", "ts": util.now_ms(),
                     "data": snap})
    return snap


async def _legacy_op(client: Client, msg: dict, op: str | None) -> None:
    if op == "auth":
        token = msg.get("token", "")
        uid = STATE.sessions.get(token)
        if uid is None and token and SESSION_LOADER is not None:
            uid = await SESSION_LOADER(token)
            if uid:
                STATE.sessions[token] = uid
        client.uid = uid
        client.send({"ch": "auth", "data": dict(ok=bool(uid), uid=uid)})
    elif op == "sub":
        client.topics.add(msg.get("ch", ""))
    elif op == "unsub":
        client.topics.discard(msg.get("ch", ""))
    elif op == "ping":
        client.send({"ch": "pong", "data": time.time()})


# --------------------------------------------------------------------------- #
# Book delta pump (50 ms): flush dirty levels to orderbook subscribers         #
# --------------------------------------------------------------------------- #
async def book_delta_pump() -> None:
    from ..engine import orderbook
    while True:
        try:
            for key, subs in list(HUB._book_subs.items()):
                live = [c for c in subs if c.alive]
                if not live:
                    continue
                _, depth, v5s = key.split(".")
                symbol = config.resolve_symbol("linear", v5s) or \
                    config.resolve_symbol("spot", v5s)
                if not symbol:
                    continue
                book = orderbook.book(symbol)
                if not book.dirty["b"] and not book.dirty["a"]:
                    continue
                delta = book.drain_dirty()
                delta["s"] = v5s
                frame = {"topic": key, "type": "delta", "ts": delta["ts"],
                         "data": delta}
                for c in live:
                    c.send(frame)
        except Exception:
            import logging
            logging.getLogger("ariax.ws").exception("delta pump error")
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# Event bus wiring: engine events -> WS topics                                 #
# --------------------------------------------------------------------------- #
def wire() -> None:
    """Subscribe the hub to engine domain events."""
    from ..engine import orderbook

    def on_trade(payload: dict) -> None:
        symbol, side = payload["symbol"], payload["side"]
        v5 = v5_sym(symbol)
        data = {"T": payload["ts"], "s": v5, "S": side,
                "v": payload["qty"], "p": payload["price"]}
        HUB.push_topic(f"publicTrade.{v5}", data)
        HUB.push_legacy(f"trades:{symbol}",
                        [[payload["ts"] / 1000.0, side.lower(),
                          payload["price"], payload["qty"]]])
        # kline topic: push the forming 1m candle (Bybit pushes on update)
        t = STATE.tick(symbol)
        if t.cur:
            k = list(t.cur)
            HUB.push_topic(f"kline.1.{v5}",
                           {"start": k[0], "end": k[0] + 60_000,
                            "interval": "1",
                            "o": k[1], "h": k[2], "l": k[3], "c": k[4],
                            "v": k[5],
                            "confirm": False, "symbol": v5, "turnover": k[5] * k[4]})
            HUB.push_legacy(f"candle:{symbol}", k)

    def on_tickers(_: dict) -> None:
        """Broadcast ticker snapshots (public + legacy strip)."""
        from ..api import serializers as ser
        for symbol in config.MARKETS:
            t = STATE.tick(symbol)
            v5 = v5_sym(symbol)
            HUB.push_topic(f"tickers.{v5}", ser.ticker_v5(symbol, t))
        HUB.push_legacy("tickers",
                        {s: ser.ticker_legacy(s, STATE.tick(s))
                         for s in config.MARKETS})

    def on_order(payload: dict) -> None:
        uid, snap = payload["uid"], payload["order"]
        if uid is None or uid <= 0:
            return
        from ..api import serializers as ser
        data = ser.order_event_v5(snap)
        HUB.push_private(uid, "order", data)
        HUB.push_legacy("user", dict(
            uid=uid, type="order",
            action={"New": "new", "PartiallyFilled": "partial", "Filled": "filled",
                    "Cancelled": "cancelled", "Untriggered": "new",
                    "Triggered": "triggered", "Deactivated": "cancelled",
                    "Rejected": "rejected"}.get(snap["status"], "update"),
            id=snap["id"], order=ser.order_legacy(snap)), uid=uid)

    def on_execution(payload: dict) -> None:
        uid, snap = payload["uid"], payload["order"]
        if uid is None or uid <= 0:
            return
        HUB.push_private(uid, "execution", {
            "execId": util.gen_hex(12), "orderId": snap["order_id"],
            "symbol": v5_sym(snap["symbol"]), "side": snap["side"],
            "orderPrice": snap["price"], "orderQty": snap["qty"],
            "execPrice": payload["px"], "execQty": payload["qty"],
            "execFee": payload["fee"],
            "isMaker": not payload["is_taker"],
            "execType": "Taker" if payload["is_taker"] else "Maker",
            "execTime": util.now_ms()})

    def on_wallet(payload: dict) -> None:
        uid = payload["uid"]
        from ..api import serializers as ser
        HUB.push_private(uid, "wallet", ser.wallet_event_v5(uid))
        HUB.push_legacy("user", dict(uid=uid, type="wallet"), uid=uid)

    def on_position(payload: dict) -> None:
        uid, symbol = payload["uid"], payload["symbol"]
        from ..api import serializers as ser
        HUB.push_private(uid, "position", ser.position_event_v5(uid, symbol))
        HUB.push_legacy("user", dict(uid=uid, type="position", symbol=symbol), uid=uid)

    def on_pnl(payload: dict) -> None:
        HUB.push_legacy("user", dict(uid=payload["uid"], type="pnl",
                                     symbol=payload["symbol"],
                                     pnl=round(payload["pnl"], 4)),
                        uid=payload["uid"])

    def on_fill(payload: dict) -> None:
        HUB.push_legacy("user", dict(uid=payload["uid"], type="fill",
                                     symbol=payload["symbol"],
                                     side=payload["side"],
                                     price=payload["price"],
                                     qty=payload["qty"]), uid=payload["uid"])

    def on_liquidation(payload: dict) -> None:
        v5 = v5_sym(payload["symbol"])
        HUB.push_topic("allLiquidation", {
            "T": util.now_ms(), "s": v5, "S": payload["side"],
            "v": payload["qty"], "p": payload["price"]})
        HUB.push_legacy("user", dict(uid=payload["uid"], type="liquidation",
                                     symbol=payload["symbol"],
                                     price=payload["price"]), uid=payload["uid"])

    def on_bot_msg(payload: dict) -> None:
        HUB.push_legacy("user", dict(uid=payload["uid"], type="bot",
                                     msg=payload["msg"]), uid=payload["uid"])

    events.BUS.on("trade", on_trade)
    events.BUS.on("tickers", on_tickers)
    events.BUS.on("order", on_order)
    events.BUS.on("execution", on_execution)
    events.BUS.on("wallet", on_wallet)
    events.BUS.on("position", on_position)
    events.BUS.on("pnl", on_pnl)
    events.BUS.on("fill", on_fill)
    events.BUS.on("liquidation", on_liquidation)
    events.BUS.on("bot_msg", on_bot_msg)
    _ = orderbook  # imported for parity
