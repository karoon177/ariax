#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live WebSocket smoke test against a running AriaX v2 instance."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import websockets

BASE = "http://localhost:8000"
WS_PUBLIC = "ws://localhost:8000/v5/public/ws"
WS_PRIVATE = "ws://localhost:8000/private".replace("/private", "/v5/private/ws")
WS_LEGACY = "ws://localhost:8000/ws"


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        # user + api key
        email = f"ws-smoke-{int(time.time())}@example.com"
        r = await http.post("/api/auth/register",
                            json={"email": email, "password": "secret123"})
        token = r.json()["token"]
        r = await http.post("/api/api-keys/create",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"label": "smoke", "trade": True})
        key, secret = r.json()["key"], r.json()["secret"]

    # ---- public v5 ws: ping + orderbook snapshot + delta ---------- #
    async with websockets.connect(WS_PUBLIC) as ws:
        await ws.send(json.dumps({"op": "ping"}))
        pong = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert pong["op"] == "pong", pong
        await ws.send(json.dumps({"op": "subscribe",
                                  "args": ["orderbook.50.BTCUSDT",
                                           "tickers.BTCUSDT"]}))
        sub = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert sub["success"], sub
        frames = []
        for _ in range(4):
            frames.append(json.loads(await asyncio.wait_for(ws.recv(), 5)))
        topics = {f["topic"] for f in frames}
        assert "orderbook.50.BTCUSDT" in topics, topics
        book = next(f for f in frames if f["topic"].startswith("orderbook"))
        assert book["data"]["b"] and book["data"]["a"]
        print("✅ public v5 WS: ping/pong, subscribe, snapshot OK")

    # ---- private v5 ws: auth + order push -------------------------- #
    async with websockets.connect(WS_PRIVATE) as ws:
        expires = int(time.time() * 1000) + 30_000
        sig = hmac.new(secret.encode(),
                       f"GET/realtime{expires}".encode(),
                       hashlib.sha256).hexdigest()
        await ws.send(json.dumps({"op": "auth", "args": [key, expires, sig]}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert auth["success"], auth
        await ws.send(json.dumps({"op": "subscribe", "args": ["order"]}))
        sub = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert sub["success"], sub

        async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
            ob = (await http.get("/v5/market/orderbook",
                                 params={"category": "linear",
                                         "symbol": "BTCUSDT",
                                         "limit": 1})).json()
            bid = float(ob["result"]["b"][0][0]) if ob["result"]["b"] else 60000.0
            px = round(bid * 0.90, 1)  # deep but inside the deviation guard
            ts = str(int(time.time() * 1000))
            body = json.dumps({"category": "linear", "symbol": "BTCUSDT",
                               "side": "Buy", "orderType": "Limit",
                               "qty": "0.001", "price": f"{px:.1f}",
                               "timeInForce": "GTC"},
                              separators=(",", ":"))
            sig2 = hmac.new(secret.encode(),
                            f"{ts}{key}10000{body}".encode(),
                            hashlib.sha256).hexdigest()
            r = await http.post("/v5/order/create", content=body, headers={
                "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": "10000", "X-BAPI-SIGNATURE": sig2,
                "Content-Type": "application/json"})
            assert r.json()["retCode"] == 0, r.text
        got_order = False
        for _ in range(6):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if msg.get("topic") == "order":
                got_order = True
                assert msg["data"]["symbol"] == "BTCUSDT"
                break
        assert got_order, "no private order push received"
        print("✅ private v5 WS: HMAC auth + order push OK")

    # ---- legacy /ws UI protocol ------------------------------------ #
    async with websockets.connect(WS_LEGACY) as ws:
        await ws.send(json.dumps({"op": "auth", "token": token}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert auth["data"]["ok"], auth
        await ws.send(json.dumps({"op": "sub", "ch": "tickers"}))
        await asyncio.sleep(1.2)
        msg = json.loads(await asyncio.wait_for(ws.recv(), 6))
        assert msg["ch"] == "tickers" and "BTC/USDT" in msg["data"]
        print("✅ legacy /ws UI protocol OK")


if __name__ == "__main__":
    asyncio.run(main())
