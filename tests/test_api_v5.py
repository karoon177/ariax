# -*- coding: utf-8 -*-
"""
Integration tests: full user journey over REST + WebSocket.

Covers: registration/login(+2FA), session & signed v5 auth, faucet
policy, API keys, spot/linear orders through the v5 API, positions,
trading-stop, market data endpoints, admin stress hooks, backtest,
and both WebSocket protocols.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time

import pytest

from conftest import reset_state


def _signature(secret, ts, key, recv, payload):
    msg = f"{ts}{key}{recv}{payload}"
    return hmac_mod.new(secret.encode(), msg.encode(),
                        hashlib.sha256).hexdigest()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    reset_state()
    with TestClient(app) as c:
        yield c


def _register(client, email="trader@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "secret123",
                          "name": "Trader"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    return r.json()["token"], r.json()["uid"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _make_key(client, token, perms=("readTrade", "trade")):
    r = client.post("/api/api-keys/create",
                    headers=_hdr(token),
                    json={"label": "pytest", "trade": "trade" in perms})
    d = r.json()
    assert d["ok"], d
    return d["key"], d["secret"]


def _signed(client, method, path, key, secret, body=None, params=None):
    ts = str(int(time.time() * 1000))
    recv = "10000"
    if method == "GET":
        qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        payload = qs
        url = path + (f"?{qs}" if qs else "")
        headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                   "X-BAPI-RECV-WINDOW": recv,
                   "X-BAPI-SIGNATURE": _signature(secret, ts, key, recv, payload)}
        return client.get(url, headers=headers)
    payload = json.dumps(body or {}, separators=(",", ":"))
    headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-RECV-WINDOW": recv,
               "X-BAPI-SIGNATURE": _signature(secret, ts, key, recv, payload),
               "Content-Type": "application/json"}
    return client.request(method, path, headers=headers, content=payload)


# --------------------------------------------------------------------------- #
# Users, sessions, 2FA, faucet                                                 #
# --------------------------------------------------------------------------- #
def test_register_login_flow(client):
    token, uid = _register(client)
    r = client.post("/api/auth/login",
                    json={"email": "trader@example.com",
                          "password": "secret123"})
    assert r.json()["ok"]
    bad = client.post("/api/auth/login",
                      json={"email": "trader@example.com", "password": "wrong"})
    assert not bad.json()["ok"]
    wallet = client.get("/api/wallet", headers=_hdr(token)).json()
    assert wallet["ok"] and wallet["balances"]["USDT"] == 20_000


def test_2fa_flow(client):
    token, _ = _register(client, "twofa@example.com")
    setup = client.post("/api/auth/2fa/setup", headers=_hdr(token)).json()
    assert setup["ok"] and setup["secret"]
    from app import security
    code = security.totp_code(setup["secret"])
    assert client.post("/api/auth/2fa/confirm", headers=_hdr(token),
                       json={"code": code}).json()["ok"]
    no_otp = client.post("/api/auth/login",
                         json={"email": "twofa@example.com",
                               "password": "secret123"})
    assert no_otp.json().get("need_otp") is True
    ok_login = client.post("/api/auth/login",
                           json={"email": "twofa@example.com",
                                 "password": "secret123",
                                 "otp": security.totp_code(setup["secret"])})
    assert ok_login.json()["ok"]


def test_faucet_24h_policy(client):
    token, _ = _register(client, "faucet@example.com")
    first = client.post("/api/faucet", headers=_hdr(token)).json()
    assert first["ok"] and first["amount"] == 10_000
    second = client.post("/api/faucet", headers=_hdr(token)).json()
    assert not second["ok"] and "۲۴" in second["error"]


# --------------------------------------------------------------------------- #
# v5 signed API                                                                #
# --------------------------------------------------------------------------- #
def test_v5_signed_end_to_end(client):
    token, uid = _register(client, "v5@example.com")
    key, secret = _make_key(client, token)

    # wallet
    r = _signed(client, "GET", "/v5/account/wallet-balance", key, secret,
                params={"accountType": "UNIFIED"})
    body = r.json()
    assert body["retCode"] == 0, body
    coins = body["result"]["list"][0]["coin"]
    assert any(c["coin"] == "USDT" for c in coins)

    # bad signature rejected
    bad = client.get("/v5/account/wallet-balance",
                     headers={"X-BAPI-API-KEY": key,
                              "X-BAPI-TIMESTAMP": str(int(time.time() * 1000)),
                              "X-BAPI-RECV-WINDOW": "10000",
                              "X-BAPI-SIGNATURE": "deadbeef"})
    assert bad.json()["retCode"] == 10003

    # place limit buy on linear BTCUSDT (crosses MM quotes)
    for _ in range(30):
        r = _signed(client, "POST", "/v5/order/create", key, secret, body={
            "category": "linear", "symbol": "BTCUSDT", "side": "Buy",
            "orderType": "Limit", "qty": "0.002", "price": "78000",
            "timeInForce": "GTC", "orderLinkId": f"py-{int(time.time()*1000)}"})
        if r.json()["retCode"] == 0:
            break
        time.sleep(0.2)  # wait for MM quotes / feed prices to settle
    assert r.json()["retCode"] == 0, r.text
    order_id = r.json()["result"]["orderId"]

    # realtime list contains it
    rr = _signed(client, "GET", "/v5/order/realtime", key, secret,
                 params={"category": "linear", "symbol": "BTCUSDT"})
    ids = [o["orderId"] for o in rr.json()["result"]["list"]]
    assert order_id in ids

    # cancel it
    rc = _signed(client, "POST", "/v5/order/cancel", key, secret, body={
        "category": "linear", "symbol": "BTCUSDT", "orderId": order_id})
    assert rc.json()["retCode"] == 0

    # position: market open + set leverage + trading stop + close
    _signed(client, "POST", "/v5/position/set-leverage", key, secret, body={
        "category": "linear", "symbol": "ETHUSDT", "leverage": "5"})
    r2 = _signed(client, "POST", "/v5/order/create", key, secret, body={
        "category": "linear", "symbol": "ETHUSDT", "side": "Buy",
        "orderType": "Market", "qty": "0.02", "timeInForce": "IOC"})
    assert r2.json()["retCode"] == 0, r2.text
    pl = _signed(client, "GET", "/v5/position/list", key, secret,
                 params={"category": "linear", "symbol": "ETHUSDT"})
    positions = pl.json()["result"]["list"]
    assert positions and positions[0]["side"] == "Buy"
    assert float(positions[0]["leverage"]) == 5
    liq = float(positions[0]["liqPrice"])
    assert 0 < liq < float(positions[0]["avgPrice"])
    ts = _signed(client, "POST", "/v5/position/trading-stop", key, secret, body={
        "category": "linear", "symbol": "ETHUSDT",
        "takeProfit": "999999", "stopLoss": "1"})
    assert ts.json()["retCode"] == 0

    # executions recorded (write-behind: allow the queue a beat to flush)
    time.sleep(0.25)
    ex = _signed(client, "GET", "/v5/execution/list", key, secret,
                 params={"category": "linear", "limit": "20"})
    assert ex.json()["retCode"] == 0 and len(ex.json()["result"]["list"]) >= 1

    # transaction log
    tl = _signed(client, "GET", "/v5/account/transaction-log", key, secret,
                 params={"limit": "10"})
    assert tl.json()["retCode"] == 0


def test_v5_market_data(client):
    # time
    t = client.get("/v5/market/time").json()
    assert t["retCode"] == 0 and "timeSecond" in t["result"]
    # instruments
    ins = client.get(
        "/v5/market/instruments-info",
        params={"category": "linear", "symbol": "BTCUSDT"}).json()
    assert ins["retCode"] == 0
    item = ins["result"]["list"][0]
    assert item["symbol"] == "BTCUSDT"
    assert item["lotSizeFilter"]["qtyStep"]
    assert len(item["riskLimits"]) >= 3
    # tickers
    tk = client.get("/v5/market/tickers",
                    params={"category": "linear", "symbol": "BTCUSDT"}).json()
    assert tk["retCode"] == 0 and tk["result"]["list"][0]["fundingRate"]
    # orderbook
    ob = client.get("/v5/market/orderbook",
                    params={"category": "linear", "symbol": "BTCUSDT",
                            "limit": 50}).json()
    assert ob["retCode"] == 0 and ob["result"]["b"] and ob["result"]["a"]
    # kline
    kl = client.get("/v5/market/kline",
                    params={"category": "spot", "symbol": "BTCUSDT",
                            "interval": "1", "limit": 50}).json()
    assert kl["retCode"] == 0 and len(kl["result"]["list"]) > 0
    row = kl["result"]["list"][0]
    assert len(row) == 7


def test_v5_error_envelope(client):
    r = client.get("/v5/market/instruments-info",
                   params={"category": "linear", "symbol": "NOPEUSDT"})
    body = r.json()
    assert body["retCode"] == 10019
    assert set(body) >= {"retCode", "retMsg", "result", "time"}


def test_v5_faucet_endpoint(client):
    token, _ = _register(client, "v5faucet@example.com")
    key, secret = _make_key(client, token)
    r = _signed(client, "POST", "/v5/asset/faucet", key, secret,
                body={"asset": "USDT"})
    assert r.json()["retCode"] == 0
    assert r.json()["result"]["cooldownHours"] == 24


def test_admin_force_price_and_backtest(client):
    token, _ = _register(client, "admin@example.com")
    key, secret = _make_key(client, token)
    # backtest over internal candles (deterministic fallback)
    r = _signed(client, "POST", "/v5/backtest/run", key, secret, body={
        "category": "linear", "symbol": "BTCUSDT", "interval": "1",
        "strategy": "ema_cross", "initialCapital": 10000, "leverage": 5,
        "limit": 300})
    body = r.json()
    assert body["retCode"] == 0, body
    res = body["result"]
    for field in ("net_pnl", "win_rate", "max_drawdown_pct", "equity_curve",
                  "total_trades"):
        assert field in res
    # admin: disabled without token, works with token
    bad = client.post("/v5/admin/force-price",
                      json={"category": "linear", "symbol": "BTCUSDT",
                            "price": 50000})
    assert bad.json()["retCode"] != 0
    ok = client.post("/v5/admin/force-price",
                     headers={"X-Admin-Token": "test-admin-token"},
                     json={"category": "linear", "symbol": "BTCUSDT",
                           "price": 50000})
    assert ok.json()["retCode"] == 0
    stats = client.get("/v5/admin/stats",
                       headers={"X-Admin-Token": "test-admin-token"}).json()
    assert stats["result"]["stats"]["orders"] >= 1


# --------------------------------------------------------------------------- #
# WebSocket                                                                    #
# --------------------------------------------------------------------------- #
def test_ws_public_v5(client):
    with client.websocket_connect("/v5/public/ws") as ws:
        ws.send_json({"op": "ping"})
        pong = ws.receive_json()
        assert pong["op"] == "pong" and pong["success"]
        ws.send_json({"op": "subscribe",
                      "args": ["orderbook.50.BTCUSDT", "tickers.BTCUSDT"]})
        sub = ws.receive_json()
        assert sub["op"] == "subscribe" and sub["success"]
        snap = ws.receive_json()
        assert snap["topic"] == "orderbook.50.BTCUSDT"
        assert snap["type"] == "snapshot" and snap["data"]["b"]


def test_ws_private_v5(client):
    token, _ = _register(client, "ws@example.com")
    key, secret = _make_key(client, token)
    with client.websocket_connect("/v5/private/ws") as ws:
        expires = int(time.time() * 1000) + 10_000
        from app import security
        sig = security.ws_auth_signature(secret, expires)
        ws.send_json({"op": "auth", "args": [key, expires, sig]})
        auth = ws.receive_json()
        assert auth["op"] == "auth" and auth["success"], auth
        ws.send_json({"op": "subscribe", "args": ["order", "wallet"]})
        sub = ws.receive_json()
        assert sub["success"]


def test_ws_legacy_ui_protocol(client):
    token, _ = _register(client, "legacy-ws@example.com")
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"op": "auth", "token": token})
        auth = ws.receive_json()
        assert auth["ch"] == "auth" and auth["data"]["ok"]
        ws.send_json({"op": "sub", "ch": "tickers"})
        time.sleep(0.1)
        client.get("/api/markets")
        time.sleep(0.6)  # tape loop pushes tickers twice/sec
        msg = ws.receive_json()
        assert msg["ch"] == "tickers" and "BTC/USDT" in msg["data"]


# --------------------------------------------------------------------------- #
# Rate limiting & security                                                     #
# --------------------------------------------------------------------------- #
def test_rate_limit_public(client):
    codes = []
    for _ in range(45):
        codes.append(client.get("/v5/market/time").status_code)
    assert codes.count(429) >= 1


def test_legacy_api_shapes(client):
    """The v1 UI contract must stay intact."""
    token, _ = _register(client, "legacy@example.com")
    h = _hdr(token)
    assert client.get("/api/markets").json()["reference"]["source"]
    cfg = client.get("/api/config").json()
    assert cfg["data"]["BTC/USDT"]["minq"] == 0.0005
    assert cfg["fees"]["taker"] > 0
    book = client.get("/api/book",
                      params={"symbol": "BTC/USDT"}).json()["data"]
    assert "bids" in book and "asks" in book and "last" in book
    candles = client.get("/api/candles",
                         params={"symbol": "BTC/USDT",
                                 "interval": "1m"}).json()
    assert candles["ok"] and len(candles["data"]) > 0
    perf = client.get("/api/performance", headers=h).json()
    assert perf["ok"] and "net_pnl" in perf
    keys = client.get("/api/api-keys", headers=h).json()
    assert keys["ok"] and keys["data"] == []
    r = client.post("/api/order", headers=h, json={
        "symbol": "BTC/USDT", "side": "buy", "type": "limit",
        "price": 2000, "qty": 1})
    assert not r.json()["ok"]  # price deviation guard
    orders_list = client.get("/api/orders", headers=h).json()
    assert orders_list["ok"] and orders_list["data"] == []
