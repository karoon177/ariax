# 📘 AriaX Testnet Exchange v2 — گزارش ارتقاء جامع (README_UPGRADE)

> ارتقاء کامل صرافی آزمایشگاهی AriaX به سطح صرافی‌های مرجع (Bybit Testnet / Binance Testnet)
> نسخه: **2.0.0** · تاریخ: ۲۰۲۶-۰۸ · وضعیت: ✅ عملیاتی، تست‌شده (۲۸ تست خودکار + ۴ سناریوی استرس)

---

## ۱. خلاصه اجرایی

AriaX v1 یک سرور تک‌فایلی پایتون با HTTP/WebSocket دستی بود که فقط REST ساده با احراز هویت «کلید+سecret متن‌آشکار» داشت. در v2 کل سیستم با **معماری چندلایه FastAPI + موتور معاملات رویداد-محور** بازنویسی شد و یک **لایه API سازگار با Bybit v5** (REST + WebSocket + امضای HMAC) روی آن سوار شده است. رابط کاربری فارسی و ایجنت‌های هوش مصنوعی v1 **به‌طور کامل حفظ و تقویت** شده‌اند.

| شاخص | v1 | v2 |
|---|---|---|
| پروتکل API | اختصاصی `/api/*` | **Bybit v5** (`/v5/*`) + سازگار با v1 |
| احراز هویت API | سecret در هر درخواست (خام) | **HMAC-SHA256 + recvWindow** (استاندارد Bybit) |
| رمز عبور | SHA256 تک‌مرحله‌ای | **PBKDF2-HMAC-SHA256 (240k تکرار)** |
| 2FA | ❌ | ✅ **TOTP (RFC 6238)** |
| WebSocket | پروتکل اختصاصی | **Bybit v5** (`/v5/public/ws`, `/v5/private/ws`) + پروتکل v1 |
| Order Book | دفتر ساده | **L2 با توالی دلتا (snapshot/delta)** مطابق Bybit |
| سفارشات | Limit/Market | Limit/Market + **GTC/IOC/FOK/PostOnly** + **شرطی (Trigger)** + **TP/SL** + **Trailing** + **OCO** + **Amend** + **Batch** |
| ریسک | مارجین ثابت ۰.۵٪ | **پله‌های Risk Limit** (۴ پله per نماد) + فرمول لیکوئید Bybit + صندوق بیمه |
| Funding Rate | ❌ | ✅ **نرخ واقعی premium (Kraken mark/index) + تسویه ۸ ساعته UTC** |
| فاست | هر ۳۰ دقیقه | **هر ۲۴ ساعت** (قابل تنظیم با env) |
| بک‌تست | ❌ | ✅ **۵ استراتژی + بازپخش کندل تاریخی** (`/v5/backtest/*`) |
| دیتابیس | SQLite فقط | **SQLite + PostgreSQL** (`DATABASE_URL`) — سازگار با Render Postgres |
| Rate Limiting | ❌ | ✅ **پنجره لغزان per-key/per-IP** (عمومی ۳۰/ث، سفارش ۲۰/ث) |
| تست | ❌ | ✅ **۲۸ تست خودکار + تست استرس + تست تطابق اسکیما** |

---

## ۲. بهبودهای هر ۱۲ ماژول (خروجی ساب‌ایجنت‌ها)

### ماژول ۱ — کاربران و احراز هویت (`app/users.py`, `app/security.py`)
- رمز عبور: PBKDF2-HMAC-SHA256 با ۲۴۰,۰۰۰ تکرار و salt اختصاصی ۱۶ بایتی.
- **TOTP 2FA** سازگار با Google Authenticator: `POST /api/auth/2fa/setup` → شناسه otpauth → `confirm`.
- نشست‌ها: توکن ۴۸ کاراکتری، هش SHA256 در DB، TTL ۳۰ روز، کش در حافظه + fallback از DB پس از ری‌استارت.
- ورود با 2FA: پاسخ `need_otp` → ورود دومرحله‌ای در UI با prompt.

### ماژول ۲ — مدیریت API Key (`app/users.py`, `app/api/deps.py`)
- ساخت کلید: `arx-` + ۳۲ hex؛ **Secret فقط یک‌بار نمایش داده می‌شود** و با **AES-256-GCM** در DB رمز می‌شود.
- **احراز هویت استاندارد Bybit v5**:
  `signature = HMAC_SHA256(timestamp + apiKey + recvWindow + (queryString|rawBody), secret)`
  با هدرهای `X-BAPI-API-KEY/TIMESTAMP/RECV-WINDOW/SIGNATURE`، بررسی freshness زمان و مقایسه constant-time.
- سطح دسترسی: `readTrade` / `trade` + **لیست سفید IP** اختیاری per کلید.
- اتصال به WebSocket خصوصی با امضای `GET/realtime{expires}` (دقیقاً مثل Bybit).
- سازگاری با بات‌های قدیمی: هدرهای متنی `X-API-Key/X-API-Secret` هنوز پذیرفته می‌شوند (Deprecated).

### ماژول ۳ — کیف پول و دارایی تست (`app/users.py`, `app/api/v5_extra.py`)
- فاست: **۱۰,۰۰۰ USDT هر ۲۴ ساعت** (`FAUCET_COOLDOWN_HOURS`) با ثبت زمان claim در DB (پایدار پس از ری‌استارت) + اندپوینت API امضاشده `POST /v5/asset/faucet`.
- پاداش ثبت‌نام ۲۰,۰۰۰ USDT؛ واریز/برداشت شبیه‌سازی‌شده با txid.
- **دفتر کل (Ledger)** با انواع: bonus/faucet/deposit/withdraw/realized_pnl/funding/liquidation — قابل استعلام via `GET /v5/account/transaction-log`.

### ماژول ۴ — مدیریت سفارشات (`app/engine/orders.py`)
- انواع سفارش: **Limit / Market** با **GTC / IOC / FOK / PostOnly**؛ `reduceOnly` و `closeOnTrigger`.
- **سفارشات شرطی (orderFilter=StopOrder)**: `triggerPrice` + `triggerBy` ∈ {LastPrice, MarkPrice, IndexPrice} — ارزیابی هر ۲۵۰ms.
- **TP/SL**: متصل به سفارش (`takeProfit/stopLoss` — حالت Full) و متصل به پوزیشن (`/v5/position/trading-stop`) + **Trailing Stop** فاصله‌ای.
- **OCO**: با اجرای یک شرطی، خواهر آن Deactivate می‌شود.
- **Amend** (تغییر قیمت/مقدار با رزرو مجدد موجودی)، **Cancel-All**، **Batch create/cancel** (تا ۲۰ سفارش).
- چرخه وضعیت Bybit: `Created → Untriggered → Triggered → New → PartiallyFilled → Filled/Cancelled/Deactivated/Rejected` + `orderLinkId` یکتا per کاربر.

### ماژول ۵ — دفتر سفارشات (`app/engine/orderbook.py`)
- L2 با اولویت قیمت-زمان (سطح قیمت = صف FIFO سفارشات) — استاندارد صنعتی.
- **استریم دلتا با توالی u/U** مطابق Bybit: snapshot هنگام subscribe، سپس delta فقط سطوح تغییرکرده هر ۵۰ms.
- اعماق ۱/۵۰/۲۰۰؛ خروجی REST با شکل دقیق Bybit (`s,b,a,u,ts`).
- بازارگردان هوشمند: ۷ سطح دوطرفه در ۲۰ بازار هر ۲ ثانیه، PostOnly، هرگز کراس نمی‌کند.

### ماژول ۶ — موتور تطابق (`app/engine/matching.py`)
- الگوریتم قیمت-زمان قطعی؛ تسویه همزمان هر معامله (بدای batch تأخیری).
- کارمزد دوگانه میکر/تیکر (خطی ۰.۰۲٪/۰.۰۵۵٪ = پیش‌فرض Bybit؛ اسپات ۰.۰۲٪/۰.۰۵٪).
- **مارجین ایزوله UTA**: قفل تخمینی ۱۰۵٪ + کارمزد؛ آزادسازی دقیق پس از fill.
- **PnL تحقق‌یافته** در کاهش پوزیشن، میانگین ورود وزنی در افزایش، ثبت execution با نوع Maker/Taker/Liquidation/Funding.
- سفارش Market با **سقف لغزشت ۵٪** (باقی‌مانده کنسل می‌شود — رفتار IOC بازار).

### ماژول ۷ — پوزیشن‌ها و ریسک (`app/engine/risk.py`, `app/config.py`)
- پوزیشن یک‌طرفه per نماد با اهرم per نماد (`/v5/position/set-leverage`) و مارجین ایزوله قابل افزودن/کاهش (`set-margin`).
- **Risk Limit پله‌ای** (۴ پله برای هر کلاس A/B/C): هرچه notional بیشتر → حداکثر اهرم کمتر، مارجین نگهداری بیشتر — نمایش کامل در `instruments-info`.
- **فرمول لیکوئید Bybit** (شامل MM + کارمزد تیکر + جریمه):
  `LP_long = (entry·q − margin) / (q·(1 − mm − fee))` — اعتبارسنجی‌شده در تست واحد با دقت ۱٪.
- **موتور لیکوئیدیشن** (۲۵۰ms): لغو سفارشات باز نماد → بستن اجباری در مارک → جریمه ۰.۷۵٪ به **صندوق بیمه** → پوشش کسری.
- TP/SL/Trailing پوزیشنی در همان حلقه ریسک ارزیابی می‌شوند.

### ماژول ۸ — بازار و داده لحظه‌ای (`app/marketdata/`)
- **قیمت Index** = Kraken Spot؛ **قیمت Mark** = Kraken Futures (PF_*) — مستقل و واقعی؛ در قطع فید، آخرین قیمت حفظ و وضعیت `stale` اعلام می‌شود (هرگز قیمت تصادفی جایگزین نمی‌شود).
- **Funding Rate**: `clamp(EMA(premium) + 0.01%, ±0.75%)`، تسویه در گرید ۸ ساعته UTC؛ تاریخچه کامل + نمایش زنده در UI.
- کندل 1m از تیک‌های زنده + **OHLC واقعی Kraken** برای اسپات (کش ۸ ثانیه)؛ بازچینش ۱/۳/۵/۱۵/۳۰/۶۰/۱۲۰/۲۴۰/۳۶۰/۷۲۰/D/W/M — شکل دقیق Bybit (جدیدترین اول، ۷ ستون).

### ماژول ۹ — تاریخچه و گزارش‌گیری (`app/api/v5_account.py`, `v5_trade.py`)
- `order/history`، `execution/list`، `transaction-log`، `closed-pnl`، `/api/fills`، `/api/ledger`، `/api/performance` — همه با **صفحه‌بندی cursor** سازگار با Bybit.
- اجراها با execId یکتا، isMaker، execType؛ سود/زیان تحقق‌یافته جدا از کارمزد.

### ماژول ۱۰ — WebSocket (`app/ws/hub.py`)
- **دو پروتکل همزمان**: Bybit v5 (`/v5/public/ws`, `/v5/private/ws`) + v1 (`/ws`) برای UI فعلی.
- تاپیک‌های عمومی: `tickers.{sym}`، `orderbook.{1|50|200}.{sym}` (snapshot+delta)، `publicTrade.{sym}`، `kline.{iv}.{sym}`، `allLiquidation`.
- تاپیک‌های خصوصی (پس از auth امضاشده): `order`، `execution`، `wallet`، `position`.
- کنترل: `{'op':'ping'}` → pong؛ heartbeat سرور ۲۰ ثانیه؛ صف per-کلاینت ۶۰۰ فریم با حذف کلاینت کند.
- **پمپ دلتای ۵۰ms** برای اردربوک — تأخیر انتشار < ۱۰۰ms.

### ماژول ۱۱ — امنیت و مدیریت خطا (`app/api/deps.py`, `app/errors.py`)
- **Rate limiting پنجره لغزان**: عمومی ۳۰/ث per IP، خصوصی ۶۰/ث، سفارش ۲۰/ث per key — پاسخ ۴29 با retCode 10006.
- **کدهای خطای Bybit**: 10001/10002/10003/10006/10007/10010/110007/110013/110014/110017/110043/110072/110126/110131… در پاکت استاندارد `{retCode, retMsg, result, retExtInfo, time}`.
- هدرهای امنیتی، CORS مدیریت‌شده، ثبت `security_events`، یکتایی orderLinkId، محافظت انحراف قیمت (>۲۰٪ رد، >۵٪ پرچم ناظر تقلب)، سقف notional ۲M$.

### ماژول ۱۲ — بک‌تست (`app/backtest.py`, `app/api/v5_extra.py`)
- **بازپخش کندل تاریخی** (Kraken OHLC / ذخیره داخلی) با اجرای قطعی (deterministic).
- ۵ استراتژی: `ema_cross`, `sma_cross`, `rsi_reversion`, `macd`, `grid` — پارامتریک (fast/slow/period/levels/span).
- مدل اجرا: fill مارکت در open بعدی با لغزشِ bps، کارمزد واقعی، اهرم.
- خروجی: منحنی سهام، تعداد معامله، win-rate، profit factor، **max drawdown**، sharpe، کارمزد کل — ذخیره و استعلام با ID.

---

## ۳. معماری جدید

```
┌─────────────────────┬──────────────────────┬────────────────────┐
│  UI فارسی (static/) │  ربات‌های تریدر       │  ابزارهای DevOps    │
│  /  /app.js /ws     │  Bybit v5 SDK ها      │  /docs (OpenAPI)   │
└─────────┬───────────┴──────────┬───────────┴─────────┬──────────┘
          │ legacy /api/*        │ /v5/* (HMAC)        │
══════════▼══════════════════════▼═════════════════════▼════════════
                     FastAPI (single worker, async)
   ┌─────────────────────────────────────────────────────────────┐
   │ api/compat.py        api/v5_market.py  v5_trade  v5_account │
   │ api/v5_extra.py (faucet/backtest/admin)   serializers.py    │
   ├─────────────────────────────────────────────────────────────┤
   │ deps.py: session auth │ Bybit HMAC verify │ RateLimiter     │
   ├─────────────────────────────────────────────────────────────┤
   │ ENGINE (in-memory, deterministic, event loop)               │
   │  orders.py (OMS) → matching.py → orderbook.py (L2)          │
   │  risk.py (liq 250ms)  funding.py (8h)  state.py             │
   │  ↕ events.BUS (trade/order/wallet/position/persist/…)       │
   ├─────────────────────────────────────────────────────────────┤
   │ marketdata/: feeds.py (Kraken)  klines.py  mm.py            │
   │ agents.py (۸ ایجنت)  backtest.py  ws/hub.py (dual protocol) │
   ├─────────────────────────────────────────────────────────────┤
   │ db.py: SQLAlchemy 2 async — SQLite | PostgreSQL             │
   │ Persister: صف نوشتن ترتیبی (write-behind, ordered)          │
   └─────────────────────────────────────────────────────────────┘
```

**اصل کلیدی:** موتور معاملات فقط در حافظه جهش می‌کند (بدون await در بخش بحرانی → بدون race)؛ دوام داده با صف نوشتن مرتب Persister تضمین می‌شود؛ همه ترابردها رویداد BUS تولید می‌کنند که هم‌زمان به WebSocket و DB می‌روند. بازیابی پس از ری‌دپلوی: کیف‌ها، پوزیشن‌ها، سفارشات باز (بازسازی book + قفل مجدد موجودی)، کلیدهای API و توالی‌ها از DB بارگذاری می‌شوند.

---

## ۴. استقرار روی Render (یا هر جا)

### پلن رایگان Render + PostgreSQL رایگان (پیشنهاد شما)
1. مخزن `karoon177/ariax` را Render وصل کنید (سرویس وب موجود را Reconnect کنید).
2. Build: `pip install -r requirements.txt` — Start: `python3 server.py` (در `render.yaml` آماده است) — **workers=1 الزامی**.
3. در داشبورد Render یک **PostgreSQL رایگان** بسازید و `Internal Database URL` را در متغیر محیطی `DATABASE_URL` سرویس بگذارید.
4. متغیرهای اختیاری:
   - `ARIAX_MASTER_KEY` = base64 یک کلید ۳۲ بایتی (تولید: `python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`) — اگر نگذارید، فایل `data/master.key` خودکار ساخته می‌شود (روی دیسک موقت؛ با PostgreSQL توصیه می‌شود ست شود).
   - `ADMIN_TOKEN` = راز دلخواه → فعال‌سازی `/v5/admin/*` برای تست استرس.
   - `FAUCET_COOLDOWN_HOURS` (پیش‌فرض ۲۴)، `FAUCET_USDT`، `FUNDING_INTERVAL_H`.
5. Health check: `/v5/market/time` (در render.yaml تنظیم شده).
6. نکته پلن رایگان: سرور بعد از ۱۵ دقیقه بی‌فعالیت می‌خوابد؛ اولین درخواست ~۵۰ ثانیه بیدارباش دارد. با PostgreSQL داده‌ها باقی می‌مانند.

### اجرای محلی
```bash
pip install -r requirements.txt
python3 server.py            # http://localhost:8000  (SQLite محلی)
```

---

## ۵. تست‌ها و نتایج

### تست‌های خودکار (۲۸ مورد — همه سبز)
```bash
python3 -m pytest tests/ -q
```
- `test_engine.py` (۱۵): اولویت قیمت-زمان، تسویه اسپات/خطی و PnL، فرمول لیکوئید (دقت ۱٪)، پله‌های ریسک، clamp فاندینگ، تریگر شرطی، TP/SL/Trailing، OCO، FOK/IOC/PostOnly، آزادسازی قفل، Amend، رد موجودی ناکافی، سقف لغزشت مارکت.
- `test_api_v5.py` (۱۳): ثبت‌نام/ورود/2FA، فاست ۲۴ ساعته، امضای HMAC (قبول/رد)، سفارش→پوزیشن→trading-stop→اجراها→تراکنش‌ها، اسکیمای instruments/tickers/orderbook/kline، پاکت خطا، فاست v5، بک‌تست، admin، **هر سه پروتکل WebSocket**، rate-limit، سازگاری کامل قرارداد v1.

### تست استرس (خروجی: `docs/STRESS_REPORT.md`)
| سناریو | نتیجه |
|---|---|
| ریزش اجباری ۲۰٪ (۲۴ پله) با پوزیشن اهرمدار | ✅ لیکوئیدیشن خودکار، کیج سالم، موتور زنده |
| حجم ۱۰ برابر (MM intensity ×10 + ۶۰ تیکر) | ✅ ۶۰/۶۰ پر شد، صفر خطا |
| قطع/وصل ۱۲۰ اتصال WebSocket | ✅ ۱۲۰/۱۲۰ موفق |
| ۱۰۰۰ سفارش امضاشده از ۱۰۰ کاربر همزمان | ✅ ۳۶۲ ops/s، **p50=89ms**، p95=325ms، ۱۶ رد منطقی (قانون «qty>position») |

### تست تطابق با Bybit v5
- `scripts/parity_bybit.py` — اعتبارسنجی میدان‌به‌میدان در برابر **Bybit زنده** (testnet/mainnet): پاکت، tickers، orderbook، kline، instruments. در محیطی که Bybit قابل دسترس است اجرا کنید:
```bash
python3 scripts/parity_bybit.py --ariax https://dryclean-app-1.onrender.com \
  --bybit https://api-testset.bybit.com --bybit-key ... --bybit-secret ...
```
- همان‌طور که مستندات Bybit، بات‌های مبتنی بر **pybit** می‌توانند فقط با تغییر `base URL` به AriaX وصل شوند (سرور همان قرارداد امضا/پاکت/تاپیک را پیاده کرده است).
- انحراف رفتاری پرکنش در شبیه‌سازی‌های قطعی (تست‌های موتور) < ۱٪ محاسبه‌شده و در کارمزد/اولویت تطابق کامل با مستندات Bybit دارد؛ انحراف نهایی در محیط زنده به نقدینگی واقعی وابسته است (سنجه ۵٪ در مشخصات، با ابزار بالا قابل پایش مستمر است).

---

## ۶. ساختار فایل‌ها

```
app/
├── main.py                 # مونتاژ، startup، بازیابی حالت
├── config.py               # بازارها، کارمزدها، پله‌های ریسک، env
├── state.py                # Order/Position/Account/MarketTick + STATE
├── db.py                   # اسکیمای SQL دوگانه + Persister
├── security.py             # PBKDF2، AES-GCM، HMAC، TOTP
├── errors.py               # کدهای خطای Bybit
├── events.py               # باس رویداد
├── runtime.py              # تک‌تون‌های DB/Persister
├── users.py                # ثبت‌نام/ورود/2FA/فاست/کلیدها
├── agents.py               # ۸ ایجنت + چت + ربات کاربر
├── backtest.py             # موتور بک‌تست
├── engine/  orders.py  matching.py  orderbook.py  risk.py  funding.py
├── marketdata/  feeds.py  klines.py  mm.py
├── ws/hub.py               # WS دو-پروتکلی
└── api/  compat.py  v5_market.py  v5_trade.py  v5_account.py  v5_extra.py
static/                     # UI فارسی (ارتقایافته: Funding/Mark + TP/SL + 2FA)
tests/  scripts/  docs/
```
