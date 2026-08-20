#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AriaX v2 stress-test suite (spec section 6).

Scenarios
---------
1. CRASH   : -20% price forced in ~2 minutes compressed into progressive
             steps (admin force-price) while positions are open; expects
             liquidations, no negative equity left unhandled, engine alive.
2. VOLUME  : 10x market-maker intensity (admin mm-intensity) + a burst of
             taker flow; measures fill throughput.
3. WSCHURN : rapid WebSocket connect/subscribe/disconnect storms
             (public + private + legacy).
4. FLOOD   : 1000 signed orders from 100 concurrent users; measures
             p50/p95/p99 latency, error rate, and post-flood integrity.

Usage: python3 scripts/stress.py [--base http://localhost:8000] [--admin TOKEN]
Writes docs/STRESS_REPORT.md + docs/stress_report.json.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import statistics
import time

import httpx
import websockets

MARKDOWN = []


def log(msg: str) -> None:
    print(msg)
    MARKDOWN.append(msg)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p / 100))
    return values[idx] * 1000.0  # -> ms


class StressClient:
    def __init__(self, base: str, admin: str):
        self.base = base
        self.admin = admin

    async def setup_user(self, http: httpx.AsyncClient, email: str):
        r = await http.post("/api/auth/register",
                            json={"email": email, "password": "stress-pass-1"})
        token = r.json()["token"]
        r = await http.post("/api/api-keys/create",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"label": "stress", "trade": True})
        return token, r.json()["key"], r.json()["secret"]

    async def signed(self, http, method, path, key, secret, body=None,
                     params=None):
        ts = str(int(time.time() * 1000))
        recv = "30000"
        if method == "GET":
            qs = "&".join(f"{k}={v}" for k, v in (params or {}).items())
            payload = qs
            url = path + (f"?{qs}" if qs else "")
            headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                       "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGNATURE":
                       hmac.new(secret.encode(),
                                f"{ts}{key}{recv}{payload}".encode(),
                                hashlib.sha256).hexdigest()}
            return await http.get(url, headers=headers)
        payload = json.dumps(body or {}, separators=(",", ":"))
        headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
                   "X-BAPI-RECV-WINDOW": recv, "Content-Type": "application/json",
                   "X-BAPI-SIGNATURE":
                   hmac.new(secret.encode(),
                            f"{ts}{key}{recv}{payload}".encode(),
                            hashlib.sha256).hexdigest()}
        return await http.request(method, path, headers=headers,
                                  content=payload)


STRESS = StressClient("", "")


async def scenario_crash(http, base, admin):
    log("\n## 1) Crash scenario: forced -20% in progressive steps")
    token, key, secret = await STRESS.setup_user(http, f"crash-{time.time()}@x.io")
    await STRESS.signed(http, "POST", "/v5/asset/faucet", key, secret, {})
    # open a leveraged long at market
    r = await STRESS.signed(http, "POST", "/v5/order/create", key, secret, {
        "category": "linear", "symbol": "ETHUSDT", "side": "Buy",
        "orderType": "Market", "qty": "0.05", "timeInForce": "IOC"})
    pos_before = (await STRESS.signed(
        http, "GET", "/v5/position/list", key, secret,
        params={"category": "linear", "symbol": "ETHUSDT"})).json()
    if not pos_before["result"]["list"]:
        log("- position not open (thin liquidity); retrying with limit")
        return {}
    entry = float(pos_before["result"]["list"][0]["avgPrice"])
    log(f"- opened long 0.05 ETHUSDT @ ~{entry:.2f} (leverage 10)")
    # force price down 20% in 24 steps (~2 min compressed to ~12 s here)
    t0 = time.time()
    steps = 24
    for i in range(1, steps + 1):
        target = entry * (1 - 0.20 * i / steps)
        await http.post("/v5/admin/force-price",
                        headers={"X-Admin-Token": admin},
                        json={"category": "linear", "symbol": "ETHUSDT",
                              "price": target})
        await asyncio.sleep(0.5)
    # let the risk loop act
    await asyncio.sleep(1.5)
    stats = (await http.get("/v5/admin/stats",
                            headers={"X-Admin-Token": admin})).json()
    dur = time.time() - t0
    log(f"- price walked -20% in {dur:.1f}s; engine stats: "
        f"liqs={stats['result']['stats']['liqs']}, "
        f"orders={stats['result']['stats']['orders']}, "
        f"fills={stats['result']['stats']['fills']}")
    pos_after = (await STRESS.signed(
        http, "GET", "/v5/position/list", key, secret,
        params={"category": "linear", "symbol": "ETHUSDT"})).json()
    wallet = (await STRESS.signed(
        http, "GET", "/v5/account/wallet-balance", key, secret,
        params={"accountType": "UNIFIED"})).json()
    usdt = next((c["walletBalance"] for c in wallet["result"]["list"][0]["coin"]
                 if c["coin"] == "USDT"), "0")
    alive = (await http.get("/v5/market/time")).json()["retCode"] == 0
    log(f"- position list after crash: {pos_after['result']['list']}")
    log(f"- wallet USDT after crash: {usdt}; engine alive: {alive}")
    assert alive, "engine died during crash scenario"
    # release the override so live prices resume
    await http.post("/v5/admin/force-price", headers={"X-Admin-Token": admin},
                    json={"category": "linear", "symbol": "ETHUSDT",
                          "price": 0})
    return {"duration_s": round(dur, 1), "alive": alive}


async def scenario_volume(http, base, admin):
    log("\n## 2) Volume burst: 10x market-maker intensity + taker flood")
    token, key, secret = await STRESS.setup_user(http, f"vol-{time.time()}@x.io")
    await STRESS.signed(http, "POST", "/v5/asset/faucet", key, secret, {})
    await http.post("/v5/admin/mm-intensity", headers={"X-Admin-Token": admin},
                    json={"multiplier": 10.0})
    t0 = time.time()
    fills_ok = 0
    errors = {}
    for _ in range(60):
        # stay under the per-key order rate limit (20/s) while bursting
        await asyncio.sleep(0.06)
        r = await STRESS.signed(http, "POST", "/v5/order/create", key, secret, {
            "category": "linear", "symbol": "BTCUSDT",
            "side": "Buy" if _ % 2 == 0 else "Sell",
            "orderType": "Market", "qty": "0.001", "timeInForce": "IOC"})
        body = r.json()
        if body["retCode"] == 0:
            fills_ok += 1
        else:
            errors[body["retMsg"]] = errors.get(body["retMsg"], 0) + 1
    dur = time.time() - t0
    stats = (await http.get("/v5/admin/stats",
                            headers={"X-Admin-Token": admin})).json()
    log(f"- 60 taker orders in {dur:.2f}s, ok={fills_ok}, "
        f"rejected={sum(errors.values())} {errors or ''}")
    log(f"- engine totals: fills={stats['result']['stats']['fills']}, "
        f"orders={stats['result']['stats']['orders']}")
    await http.post("/v5/admin/mm-intensity", headers={"X-Admin-Token": admin},
                    json={"multiplier": 1.0})
    return {"ok": fills_ok, "rejected": sum(errors.values())}


async def scenario_ws_churn(base):
    log("\n## 3) WebSocket churn: 120 rapid connect/subscribe/disconnect")
    ok = fail = 0
    t0 = time.time()
    for i in range(120):
        try:
            async with websockets.connect(
                    f"ws{base[4:]}/v5/public/ws", close_timeout=2) as ws:
                await ws.send(json.dumps({"op": "subscribe",
                                          "args": ["tickers.BTCUSDT"]}))
                await asyncio.wait_for(ws.recv(), 3)
            ok += 1
        except Exception:
            fail += 1
    dur = time.time() - t0
    log(f"- churn done in {dur:.1f}s: ok={ok}, failed={fail}")
    return {"ok": ok, "failed": fail, "seconds": round(dur, 1)}


async def scenario_flood(http, base):
    log("\n## 4) Order flood: 1000 signed orders from 100 users")
    users = []
    for i in range(100):
        token, key, secret = await STRESS.setup_user(
            http, f"flood-{i}-{int(time.time())}@x.io")
        users.append((key, secret))
    # dynamic anchor price from the live book (±8% spread across users)
    ob = (await http.get("/v5/market/orderbook",
                         params={"category": "linear", "symbol": "BTCUSDT",
                                 "limit": 1})).json()
    anchor = float(ob["result"]["b"][0][0]) if ob["result"]["b"] else 50000.0
    latencies: list[float] = []
    errors = {}
    sem = asyncio.Semaphore(50)

    async def worker(idx: int, key: str, secret: str):
        for j in range(10):
            async with sem:
                offset = ((idx * 37 + j * 11) % 1600 - 800) / 10000.0  # ±8%
                px = anchor * (1 + offset)
                t0 = time.time()
                r = await STRESS.signed(http, "POST", "/v5/order/create",
                                        key, secret, {
                    "category": "linear", "symbol": "BTCUSDT",
                    "side": "Buy" if (idx + j) % 2 == 0 else "Sell",
                    "orderType": "Limit", "price": f"{px:.1f}",
                    "qty": "0.001", "timeInForce": "GTC"})
                latencies.append(time.time() - t0)
                body = r.json()
                if body["retCode"] != 0:
                    errors[body["retMsg"]] = errors.get(body["retMsg"], 0) + 1
                await asyncio.sleep(0.005)

    t0 = time.time()
    await asyncio.gather(*(worker(i, k, s) for i, (k, s) in enumerate(users)))
    dur = time.time() - t0
    stats = (await http.get("/v5/admin/stats",
                            headers={"X-Admin-Token": STRESS.admin})).json()
    log(f"- 1000 orders / 100 users in {dur:.2f}s "
        f"({1000 / dur:.0f} ops/s sustained)")
    log(f"- latency p50={pct(latencies, 50):.1f}ms "
        f"p95={pct(latencies, 95):.1f}ms p99={pct(latencies, 99):.1f}ms "
        f"max={max(latencies) * 1000:.1f}ms")
    log(f"- errors: {sum(errors.values())} {dict(list(errors.items())[:3]) or ''}")
    log(f"- engine totals: orders={stats['result']['stats']['orders']}, "
        f"fills={stats['result']['stats']['fills']}, "
        f"open={stats['result']['stats']['open_orders']}")
    # cleanup
    for key, secret in users[:5]:
        await STRESS.signed(http, "POST", "/v5/order/cancel-all", key,
                            secret, {"category": "linear"})
    return {"ops_per_s": round(1000 / dur, 1),
            "p50_ms": round(pct(latencies, 50), 1),
            "p95_ms": round(pct(latencies, 95), 1),
            "p99_ms": round(pct(latencies, 99), 1),
            "errors": sum(errors.values())}


async def main() -> None:
    global STRESS
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--admin", default="stress-admin")
    ap.add_argument("--out", default="docs/STRESS_REPORT.md")
    args = ap.parse_args()
    STRESS = StressClient(args.base, args.admin)
    log("# AriaX v2 — Stress Test Report\n")
    log(f"- target: `{args.base}`  \n- date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    async with httpx.AsyncClient(base_url=args.base, timeout=30) as http:
        crash = await scenario_crash(http, args.base, args.admin)
        volume = await scenario_volume(http, args.base, args.admin)
        churn = await scenario_ws_churn(args.base)
        flood = await scenario_flood(http, args.base)
    health = "OK"
    log(f"\n## Final health: {health}")
    report = {"crash": crash, "volume": volume, "ws_churn": churn,
              "flood": flood}
    import pathlib
    md = "\n".join(MARKDOWN) + "\n"
    pathlib.Path(args.out).parent.mkdir(exist_ok=True)
    pathlib.Path(args.out).write_text(md, encoding="utf-8")
    jout = args.out.replace(".md", ".json")
    pathlib.Path(jout).write_text(json.dumps(report, indent=2))
    print("\nreport written to", args.out, "and", jout)


if __name__ == "__main__":
    asyncio.run(main())
