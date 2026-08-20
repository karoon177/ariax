# 🛠 AriaX v2 API Reference (Bybit v5 Compatible)

Base URL: `https://dryclean-app-1.onrender.com` (or your deployment)
Interactive docs: `GET /docs` (OpenAPI/Swagger)
All timestamps are **unix milliseconds**; prices/qty are strings.

---

## Authentication (private endpoints)

Bybit v5 HMAC scheme:

```
signature = HEX( HMAC_SHA256( timestamp + apiKey + recvWindow + payload, secret ) )
```

Headers:

| Header | Description |
|---|---|
| `X-BAPI-API-KEY` | your API key (`arx-…`) |
| `X-BAPI-TIMESTAMP` | unix ms |
| `X-BAPI-RECV-WINDOW` | ms (default `5000`, max `60000`) |
| `X-BAPI-SIGNATURE` | hex HMAC — payload = raw query string (GET) or raw body (POST) |

Response envelope: `{"retCode":0,"retMsg":"OK","result":{…},"retExtInfo":{},"time":…}`

Key error codes: `10001` param, `10002` timestamp, `10003` key invalid/sig mismatch,
`10006` rate limit (HTTP 429), `10007` permission, `10019` symbol invalid,
`110007` insufficient balance, `110014` price deviation, `110017` qty invalid,
`110043` order not found, `110072` duplicate orderLinkId, `110126` qty exceeds position.

Rate limits (sliding window): public `30/s` per IP · private `60/s` · order group `20/s` per key.

---

## REST endpoints

### Market (public)

| Method | Path | Notes |
|---|---|---|
| GET | `/v5/market/time` | server time |
| GET | `/v5/market/kline` | `category, symbol, interval(1..720,D,W,M), start, end, limit≤1000` → rows `[ts,o,h,l,c,v,turnover]` newest-first |
| GET | `/v5/market/mark-price-kline` | perp mark candles |
| GET | `/v5/market/index-price-kline` | index candles |
| GET | `/v5/market/instruments-info` | filters + `riskLimits` tiers |
| GET | `/v5/market/orderbook` | `limit` 1/25/50/200 → `{s,b,a,u,ts}` |
| GET | `/v5/market/tickers` | 24h stats + `markPrice, indexPrice, fundingRate, nextFundingTime` (linear) |
| GET | `/v5/market/recent-trade` | tape |
| GET | `/v5/market/funding/history` | settled funding rows |

### Trade (signed, `trade` permission)

| Method | Path | Body |
|---|---|---|
| POST | `/v5/order/create` | `category(spot|linear), symbol, side(Buy|Sell), orderType(Limit|Market), qty, price?, timeInForce(GTC|IOC|FOK|PostOnly), reduceOnly?, closeOnTrigger?, triggerPrice? (conditional), triggerBy?, takeProfit?, stopLoss?, orderLinkId?` |
| POST | `/v5/order/amend` | `orderId|orderLinkId, price?, qty?` |
| POST | `/v5/order/cancel` | `orderId|orderLinkId` |
| POST | `/v5/order/cancel-all` | `symbol?` |
| POST | `/v5/order/create-batch` | `request: [ ≤20 order dicts ]` |
| POST | `/v5/order/cancel-batch` | `request: [ ≤20 ]` |
| GET/POST | `/v5/order/realtime` | open + untriggered orders |
| GET | `/v5/order/history` | cursor pagination (`nextPageCursor`) |
| GET | `/v5/execution/list` | fill history |

### Account & Position (signed)

| Method | Path | Notes |
|---|---|---|
| GET | `/v5/account/wallet-balance` | `accountType=UNIFIED`, per-coin |
| GET | `/v5/account/transaction-log` | ledger (faucet/pnl/funding/…) |
| GET | `/v5/account/info` | margin mode, equity |
| GET | `/v5/position/list` | size, entry, liqPrice, MM/IM, TP/SL |
| POST | `/v5/position/set-leverage` | `leverage` (risk-tier capped) |
| POST | `/v5/position/trading-stop` | `takeProfit, stopLoss, trailingStop` |
| POST | `/v5/position/set-margin` | `mode: ADD|REDUCE, margin` |
| GET | `/v5/position/closed-pnl` | realized rows |
| GET | `/v5/user/query-api` | your keys |

### Asset extensions (AriaX, signed or session)

| Method | Path | Notes |
|---|---|---|
| POST | `/v5/asset/faucet` | 10,000 USDT per 24h |

### Backtest (AriaX extension, signed or session)

| Method | Path | Notes |
|---|---|---|
| GET | `/v5/backtest/strategies` | `ema_cross, sma_cross, rsi_reversion, macd, grid` |
| POST | `/v5/backtest/run` | `symbol, interval, strategy, initialCapital, leverage, slippageBps, params, limit` → metrics + equity curve + trades |
| GET | `/v5/backtest/results` | saved runs |
| GET | `/v5/backtest/result/{id}` | one run |

### Admin / stress (header `X-Admin-Token`, only if `ADMIN_TOKEN` env set)

`POST /v5/admin/force-price {category,symbol,price}` · `POST /v5/admin/mm-intensity {multiplier}` · `GET /v5/admin/stats`

---

## WebSocket

### Public — `/v5/public/ws`

```json
{"op":"ping"}
{"op":"subscribe","args":["tickers.BTCUSDT","orderbook.50.BTCUSDT","publicTrade.BTCUSDT","kline.1.BTCUSDT","allLiquidation"]}
```
Orderbook: first a `snapshot`, then `delta` frames with monotonic `u` (changed levels only; empty size deletes a level). Kline pushes carry the forming candle (`confirm:false`).

### Private — `/v5/private/ws`

```json
{"op":"auth","args":["API_KEY", expires_ms, HMAC_SHA256(secret, "GET/realtime"+expires_ms)]}
{"op":"subscribe","args":["order","execution","wallet","position"]}
```
Pushes match Bybit shapes (order status changes, executions with `isMaker/execType`, wallet balances, position risk fields).

### Legacy UI — `/ws` (v1 protocol, unchanged)

`{"op":"auth","token":…}` then `{"op":"sub","ch":"tickers|candle:SYMBOL|trades:SYMBOL|user"}`.

---

## Legacy v1 REST (`/api/*`) — kept for the current UI & old bots

register/login/logout, order/cancel/cancelall, wallet, faucet, deposit/withdraw,
orders/positions, ledger/fills/performance, api-keys create/revoke, ai toggle/chat/bot —
identical response shapes to v1 (see `MIGRATION_GUIDE.md` for the v5 equivalents).

## Symbols

| Internal | v5 (spot) | v5 (linear) |
|---|---|---|
| BTC/USDT | BTCUSDT | — |
| BTCUSD | — | BTCUSDT |
| ETHUSD / SOLUSD / … | — | ETHUSDT / SOLUSDT / … |

Spot markets: BTC, ETH, SOL, XRP, DOGE vs USDT. Linear perps: 15 markets, up to 100x (BTC) — see `instruments-info`.
