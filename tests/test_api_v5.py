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


# --------------------------------------------------------------------------- #
# v2.0.2: Bybit inter-transfer compatibility (futures wallet funding)          #
# --------------------------------------------------------------------------- #
def test_inter_transfer_compatibility(client):
    token, _ = _register(client, "transfer@example.com")
    key, secret = _make_key(client, token)
    # 1) classic bot flow: fund the CONTRACT (futures) wallet from SPOT
    r = _signed(client, "POST", "/v5/asset/transfer/inter-transfer", key, secret,
                body={"transferId": "tr-test-1", "coin": "USDT",
                      "amount": "5000", "fromAccountType": "SPOT",
                      "toAccountType": "CONTRACT"})
    body = r.json()
    assert body["retCode"] == 0 and body["result"]["status"] == "SUCCESS", body
    # 2) idempotent replay of the same transferId
    r2 = _signed(client, "POST", "/v5/asset/transfer/inter-transfer", key,
                 secret, body={"transferId": "tr-test-1", "coin": "USDT",
                               "amount": "5000", "fromAccountType": "SPOT",
                               "toAccountType": "CONTRACT"})
    assert r2.json()["retCode"] == 0
    # 3) CONTRACT wallet shows the REAL transferred amount (dual wallet)
    q = _signed(client, "GET", "/v5/asset/transfer/query-account-coins-balance",
                key, secret, params={"accountType": "CONTRACT", "coin": "USDT"})
    res = q.json()["result"]
    assert res["accountType"] == "CONTRACT"
    assert float(res["list"][0]["walletBalance"]) == 5_000
    # spot wallet decreased by the real transfer
    s = _signed(client, "GET", "/v5/asset/transfer/query-account-coins-balance",
                key, secret, params={"accountType": "SPOT", "coin": "USDT"})
    assert float(s.json()["result"]["list"][0]["walletBalance"]) == 15_000
    # 4) transferable amount from CONTRACT equals futures availability
    t = _signed(client, "GET", "/v5/account/transferable-amount", key, secret,
                params={"accountType": "CONTRACT", "coin": "USDT"})
    assert float(t.json()["result"]["transferableAmount"]) == 5_000
    # 5) history lists the transfer
    h = _signed(client, "GET", "/v5/asset/transfer/query-inter-transfer-list",
                key, secret, params={"limit": "10"})
    rows = h.json()["result"]["list"]
    assert rows and rows[0]["transferId"] == "tr-test-1"
    assert rows[0]["status"] == "SUCCESS"
    # 6) overdraft transfer rejected
    bad = _signed(client, "POST", "/v5/asset/transfer/inter-transfer", key,
                  secret, body={"transferId": "tr-test-2", "coin": "USDT",
                                "amount": "99999999",
                                "fromAccountType": "SPOT",
                                "toAccountType": "CONTRACT"})
    assert bad.json()["retCode"] == 110007
    # 7) reverse transfer moves funds back to spot
    back = _signed(client, "POST", "/v5/asset/transfer/inter-transfer", key,
                   secret, body={"transferId": "tr-test-3", "coin": "USDT",
                                 "amount": "2000", "fromAccountType": "CONTRACT",
                                 "toAccountType": "SPOT"})
    assert back.json()["retCode"] == 0
    s2 = _signed(client, "GET", "/v5/asset/transfer/query-account-coins-balance",
                 key, secret, params={"accountType": "SPOT", "coin": "USDT"})
    assert float(s2.json()["result"]["list"][0]["walletBalance"]) == 17_000
    # 8) wallet-balance answers for CONTRACT account type too
    w = _signed(client, "GET", "/v5/account/wallet-balance", key, secret,
                params={"accountType": "CONTRACT", "coin": "USDT"})
    wl = w.json()["result"]["list"][0]
    assert wl["accountType"] == "CONTRACT"
    assert float(wl["coin"][0]["walletBalance"]) == 3_000


def test_dual_wallet_auto_bridge_and_ui_transfer(client):
    """UI transfer endpoint + transparent spot→futures auto-bridge."""
    token, _ = _register(client, "dual@example.com")
    h = {"Authorization": f"Bearer {token}"}
    # 1) manual UI transfer: spot -> futures
    t0 = client.post("/api/transfer", headers=h,
                     json={"from": "spot", "to": "futures", "amount": 5000})
    assert t0.json()["ok"] and t0.json()["futures"] == 5000
    # 2) linear order uses the futures wallet (no bridge needed)
    r = client.post("/api/order", headers=h, json={
        "symbol": "BTCUSD", "side": "buy", "type": "limit",
        "price": 95_000, "qty": 0.01, "lev": 10})
    assert r.json()["ok"], r.text
    w = client.get("/api/wallet", headers=h).json()
    assert w["futures"]["balances"]["USDT"] == 5000          # free untouched
    assert w["futures"]["locks"]["USDT"] > 0                  # margin held
    # 3) transfer back what is available
    avail = w["futures"]["balances"]["USDT"] - w["futures"]["locks"]["USDT"]
    t = client.post("/api/transfer", headers=h,
                    json={"from": "futures", "to": "spot", "amount": avail})
    assert t.json()["ok"] and t.json()["moved"] == pytest.approx(avail)
    # 4) auto-bridge: an order larger than the futures wallet pulls from spot
    w2 = client.get("/api/wallet", headers=h).json()
    spot_before = w2["balances"]["USDT"]
    r2 = client.post("/api/order", headers=h, json={
        "symbol": "BTCUSD", "side": "buy", "type": "limit",
        "price": 95_000, "qty": 0.2, "lev": 10})   # needs ~1995 margin
    assert r2.json()["ok"], r2.text
    w3 = client.get("/api/wallet", headers=h).json()
    assert w3["balances"]["USDT"] < spot_before     # bridge moved real funds
    # 5) overdraft UI transfer rejected with a Persian error
    bad = client.post("/api/transfer", headers=h,
                      json={"from": "futures", "to": "spot", "amount": 10**9})
    assert not bad.json()["ok"]


# --------------------------------------------------------------------------- #
# v2.2: structured trade report (bot debugging)                                #
# --------------------------------------------------------------------------- #
def test_trade_report_structured(client):
    token, _ = _register(client, "report@example.com")
    h = {"Authorization": f"Bearer {token}"}
    key, secret = _make_key(client, token)
    # open a tagged long that crosses MM liquidity
    ob = client.get("/v5/market/orderbook",
                    params={"category": "linear", "symbol": "BTCUSDT",
                            "limit": 1}).json()
    ask = float(ob["result"]["a"][0][0])
    r = client.post("/api/order", headers=h, json={
        "symbol": "BTCUSD", "side": "buy", "type": "market",
        "qty": 0.002, "lev": 5, "strategy": "apb-ema_cross"})
    assert r.json()["ok"], r.text
    # arm a stop-loss just below mark → risk loop closes it with reason
    mark = client.get("/v5/market/tickers",
                      params={"category": "linear", "symbol": "BTCUSDT"}
                      ).json()["result"]["list"][0]
    sl = float(mark["markPrice"]) * 0.999
    t = client.post("/api/tpsl", headers=h, json={"symbol": "BTCUSD",
                                                  "sl": sl})
    assert t.json()["ok"], t.text
    import time as _t
    _t.sleep(1.5)   # let the 250ms risk loop fire the StopLoss
    rep = client.get("/api/trade-report", headers=h).json()
    assert rep["ok"]
    row = next((r for r in rep["data"] if r["symbol"] == "BTCUSD"), None)
    assert row is not None, rep["data"]
    for field in ("entry", "exit", "qty", "fees", "funding", "net",
                  "hold_min", "reason", "strategy"):
        assert field in row
    assert row["reason"] == "StopLoss"
    assert row["strategy"] == "apb-ema_cross"
    assert row["side"] == "long"
    assert row["entry"] > 0 and row["exit"] > 0
    s = rep["summary"]
    assert s["trades"] >= 1 and "winrate" in s and "funding" in s
    assert "by_reason" in s and "StopLoss" in s["by_reason"]
    assert "by_symbol" in s and "BTCUSD" in s["by_symbol"]
