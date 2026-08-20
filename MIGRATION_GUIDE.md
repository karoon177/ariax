# 🔄 راهنمای مهاجرت به AriaX v2 (برای کاربران فعلی)

این سند برای **کاربران فعلی AriaX** و **سازندگان ربات** نوشته شده است تا بدون شکستگی به نسخه جدید مهاجرت کنند.

---

## ۱. خبر خوب: هیچ کاری لازم نیست ✅

- **رابط کاربری فارسی** دقیقاً همان قبلی است و همه‌ی بخش‌ها (معامله، چارت، کیف پول، فاست، داشبورد ایجنت‌ها، چت، ربات) روی همان آدرس قبلی کار می‌کنند.
- **حساب‌های فعلی** (در صورت اتصال PostgreSQL) حفظ می‌شوند. اگر SQLite بماند، با هر ری‌دپلوی حساب‌ها ریست می‌شوند (مثل قبل، ولی حالا گزینه دائمی دارید — بخش ۴).
- بات‌هایی که با `X-API-Key` / `X-API-Secret` کار می‌کنند **همچنان کار می‌کنند** (حالت Legacy حفظ شده).

---

## ۲. تغییرات مهم رفتاری (حتماً بخوانید)

| موضوع | قبل (v1) | حالا (v2) |
|---|---|---|
| فاست | هر ۳۰ دقیقه ۱۰,۰۰۰ USDT | **هر ۲۴ ساعت** ۱۰,۰۰۰ USDT |
| کارمزد فیوچرز | تیکر ۰.۰۵٪ / میکر ۰.۰۲٪ | **تیکر ۰.۰۵۵٪** / میکر ۰.۰۲٪ (پیش‌فرض Bybit) |
| قیمت لیکوئید | فرمول ساده | فرمول کامل Bybit (مارجین نگهداری پله‌ای + کارمزد بستن) — لیکوئید کمی **زودتر** و واقعی‌تر رخ می‌دهد |
| سقف اهرم | ثابت per نماد | **وابسته به پله ریسک**: حجم بزرگ = اهرم کمتر (مثل Bybit) |
| مارکت اردر | fill کامل با قیمت ۳٪ بدتر | fill تا سقف لغزشت ۵٪؛ باقی کنسل |
| نشست ورود | توکن همیشگی | توکن ۳۰ روزه (کافی است؛ دوباره وارد شوید) |
| رمز عبور | حداقل ۴ کاراکتر | حداقل ۶ کاراکتر برای ثبت‌نام جدید |

---

## ۳. مهاجرت ربات: از API قدیمی به Bybit v5 (اختیاری ولی توصیه‌شده)

API قدیمی (`/api/*`) فعال مانده است؛ اما برای هم‌خوانی کامل نتایج با Bybit/Binance Testnet، انتقال به لایه v2 را توصیه می‌کنیم:

### گام ۱ — کلید جدید بسازید
دکمه «🔑 API ربات» → کلید با دسترسی Trade. Secret فقط یک بار نشان داده می‌شود.

### گام ۲ — امضای درخواست‌ها (استاندارد Bybit v5)
```python
import hashlib, hmac, json, time, requests

def signed_request(method, path, payload: dict, key: str, secret: str):
    ts = str(int(time.time() * 1000))
    recv = "5000"
    if method == "GET":
        qs = "&".join(f"{k}={v}" for k, v in payload.items())
        body, url = "", f"{path}?{qs}"
        sign_payload = qs
    else:
        body = json.dumps(payload, separators=(",", ":"))
        url, sign_payload = path, body
    sig = hmac.new(secret.encode(),
                   f"{ts}{key}{recv}{sign_payload}".encode(),
                   hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": ts,
               "X-BAPI-RECV-WINDOW": recv, "X-BAPI-SIGNATURE": sig}
    return requests.request(method, url, headers=headers,
                            data=body if body else None)
```

### گام ۳ — نگاشت اندپوینت‌ها

| کار | v1 (قدیمی) | v2 (Bybit v5) |
|---|---|---|
| ثبت سفارش | `POST /api/order` | `POST /v5/order/create` |
| لغو | `POST /api/cancel {id}` | `POST /v5/order/cancel {orderId}` |
| سفارشات باز | `GET /api/orders` | `GET /v5/order/realtime?category=linear&symbol=BTCUSDT` |
| پوزیشن‌ها | `GET /api/positions` | `GET /v5/position/list` |
| کیف پول | `GET /api/wallet` | `GET /v5/account/wallet-balance` |
| تاریخچه fill | `GET /api/fills` | `GET /v5/execution/list` |
| دفتر سفارش | `GET /api/book` | `GET /v5/market/orderbook` |
| قیمت‌ها | `GET /api/markets` | `GET /v5/market/tickers` |
| کندل | `GET /api/candles` | `GET /v5/market/kline` |
| فاست | `POST /api/faucet` | `POST /v5/asset/faucet` |
| حد ضرر/سود | — | `POST /v5/position/trading-stop` یا `takeProfit/stopLoss` در سفارش |
| سفارش شرطی | — | `POST /v5/order/create` با `triggerPrice` |
| اهرم | در سفارش (`lev`) | `POST /v5/position/set-leverage` (یک‌بار per نماد) |
| WebSocket | `/ws` (پروتکل قدیمی) | `/v5/public/ws` و `/v5/private/ws` |

### گام ۴ — نمادها در v5
- اسپات: `BTC/USDT` → `BTCUSDT` با `category=spot`
- فیوچرز: `BTCUSD` → `BTCUSDT` با `category=linear` (مثل Bybit!)

### گام ۵ — WebSocket به سبک Bybit
```json
{"op": "ping"}                                     → {"op":"pong", ...}
{"op": "subscribe", "args": ["tickers.BTCUSDT", "orderbook.50.BTCUSDT"]}
// خصوصی: اول auth سپس subscribe
{"op": "auth", "args": ["API_KEY", expires_ms, HMAC_SHA256(secret, "GET/realtime"+expires_ms)]}
{"op": "subscribe", "args": ["order", "execution", "wallet", "position"]}
```

### گام ۶ — قبول نتیجه‌ها را با Bybit مقایسه کنید
```bash
python3 scripts/parity_bybit.py --ariax https://<آدرس-شما> --bybit https://api-testnet.bybit.com
```
و قبل از تست زنده، استراتژی را با `POST /v5/backtest/run` بازپخش کنید.

---

## ۴. برای ماندگاری حساب‌ها (مدیر سرویس)

- **PostgreSQL رایگان Render** بسازید و `DATABASE_URL` را ست کنید (دقت کنید internal URL). از آن پس کاربران/موجودی‌ها در ری‌دپلوی باقی می‌مانند.
- اگر SQLite بماند: بعد از هر ری‌دپلوی همه حساب‌ها ریست می‌شوند (رفتار v1) — برای تست خالص کافی است.

## ۵. سؤالات متداول

**2FA را از کجا فعال کنم؟** از API: `POST /api/auth/2fa/setup` (با توکن نشست) → QR/secret را در Google Authenticator اضافه کنید → `POST /api/auth/2fa/confirm {code}`. (پنل UI برای آن به‌زودی؛ فعلاً با curl.)

**چرا سفارشم رد شد؟** علت‌ها در `retMsg` می‌آید: انحراف قیمت >۲۰٪، notional زیر حد (۵$ خطی)، موجودی/مارجین کافی نیست، اهرم بالاتر از پله ریسک، یا rate limit (۴۲۹).

**نرخ فاندینگ چیست؟** هر ۸ ساعت (۰۰:۰۰/۰۸:۰۰/۱۶:۰۰ UTC) لانگ‌ها به شورت‌ها (یا برعکس) پرداخت می‌کنند؛ نرخ لحظه‌ای در نوار نماد دیده می‌شود.

**چطور همه سفارش‌ها را یکجا لغو کنم؟** `POST /v5/order/cancel-all` یا دکمه «لغو همه» در UI.
