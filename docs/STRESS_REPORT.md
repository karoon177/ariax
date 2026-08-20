# AriaX v2 — Stress Test Report

- target: `http://localhost:8000`  
- date: 2026-08-20 18:55:53 UTC

## 1) Crash scenario: forced -20% in progressive steps
- opened long 0.05 ETHUSDT @ ~2318.75 (leverage 10)
- price walked -20% in 13.6s; engine stats: liqs=2, orders=24, fills=24
- position list after crash: []
- wallet USDT after crash: 29989.24931470; engine alive: True

## 2) Volume burst: 10x market-maker intensity + taker flood
- 60 taker orders in 3.86s, ok=60, rejected=0 
- engine totals: fills=84, orders=84

## 3) WebSocket churn: 120 rapid connect/subscribe/disconnect
- churn done in 0.3s: ok=120, failed=0

## 4) Order flood: 1000 signed orders from 100 users
- 1000 orders / 100 users in 2.76s (362 ops/s sustained)
- latency p50=89.0ms p95=325.0ms p99=482.1ms max=840.7ms
- errors: 16 {'qty exceeds position size': 16}
- engine totals: orders=1084, fills=570, open=498

## Final health: OK
