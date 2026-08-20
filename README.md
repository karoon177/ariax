# آریا‌اکس | AriaX Testnet Exchange v2 🏦⚡

صرافی آزمایشگاهی ارز دیجیتال در **سطح Bybit Testnet** — API سازگار با Bybit v5 (REST + WebSocket + امضای HMAC) + رابط کاربری فارسی + تیم ایجنت‌های هوش مصنوعی.

> 📘 مستندات کامل: [`README_UPGRADE.md`](README_UPGRADE.md) (گزارش ارتقاء ۱۲ ماژول) · [`API_REFERENCE.md`](API_REFERENCE.md) · [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md) · گزارش استرس: [`docs/STRESS_REPORT.md`](docs/STRESS_REPORT.md)

## امکانات کلیدی v2

- ✅ **API سازگار با Bybit v5**: `/v5/order/create`, `/v5/position/list`, `/v5/market/orderbook`, … با پاکت استاندارد و امضای HMAC (`X-BAPI-*`)
- ✅ **WebSocket دو-پروتکلی**: `tickers/orderbook(snapshot+delta)/publicTrade/kline/allLiquidation` + خصوصی `order/execution/wallet/position`
- ✅ **سفارشات حرفه‌ای**: Limit/Market، GTC/IOC/FOK/PostOnly، سفارش شرطی (Trigger by Last/Mark/Index)، TP/SL، Trailing Stop، OCO، Amend، Batch
- ✅ **ریسک صنعتی**: Risk Limit پله‌ای، فرمول لیکوئید Bybit، صندوق بیمه، Funding واقعی (Kraken mark/index) با تسویه ۸ ساعته
- ✅ **امنیت**: PBKDF2، AES-256-GCM برای secret ها، TOTP 2FA، Rate Limiting، IP whitelist
- ✅ **حساب و کیف پول**: ثبت‌نام/ورود، API Key، فاست ۲۴ ساعته، Ledger کامل
- ✅ **بک‌تست**: ۵ استراتژی روی داده تاریخی واقعی (`/v5/backtest/run`)
- ✅ **UI فارسی v1 حفظ شده** + نمایش Funding/Mark + دکمه TP/SL + ورود دومرحله‌ای

## اجرا

```bash
pip install -r requirements.txt
python3 server.py                    # http://localhost:8000 (SQLite)
DATABASE_URL=postgres://… python3 server.py   # با PostgreSQL
```

تست‌ها:

```bash
python3 -m pytest tests/ -q          # ۲۸ تست واحد + یکپارچه
python3 scripts/stress.py            # ۴ سناریوی استرس
python3 scripts/parity_bybit.py      # تطابق اسکیما با Bybit زنده
```

## معماری

```
app/
├── engine/       # OMS، موتور تطابق قیمت-زمان، اردربوک L2، ریسک/لیکوئید، فاندینگ
├── marketdata/   # فید Kraken (index/mark)، کندل، بازارگردان
├── api/          # v5 (market/trade/account/extra) + سازگار v1
├── ws/           # هاب WebSocket دو-پروتکلی
├── agents.py     # ۸ ایجنت هوش مصنوعی (اوراکل، بازارگردان، ریسک، ناظر تقلب، …)
├── backtest.py   # بازپخش تاریخی
└── db.py         # SQLAlchemy (SQLite/PostgreSQL) + نوشتن ترتیبی
```

## تیم ایجنت‌های هوش مصنوعی 🤖

| ایجنت | نقش در v2 |
|---|---|
| 🔮 اوراکل بازار | خوراک مرجع Kraken Spot + Futures (قیمت index/mark واقعی) |
| 💧 بازارگردان هوشمند | نقدینگی ۷ سطحی دوطرفه در ۲۰ بازار (PostOnly) |
| 🛡️ مدیر ریسک | پایش مارجین ۲۵۰ms، لیکوئیدیشن خودکار، تسویه فاندینگ |
| 🚨 ناظر تقلب | پرچم انحراف >۵٪ و نهنگ >۲M$ |
| 📈 معامله‌گر خودکار | ربات EMA(12/40) حساب کاربر |
| 💬 پشتیبان هوشمند | چت‌بات فارسی (دانش‌نامه v2) |
| 🧠 اتاق فکر داده | مقایسه شاهد مستقل Coinbase |
| 🔎 گروه ناظر عملیات | سلامت فید و SLA |

## استقرار (Render)

`render.yaml` آماده است: `pip install -r requirements.txt` → `python3 server.py` (workers=1) — برای ماندگاری داده، `DATABASE_URL` را به PostgreSQL رایگان Render متصل کنید. جزئیات در `README_UPGRADE.md` بخش ۴.
