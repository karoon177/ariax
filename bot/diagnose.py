#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تشخیص فوری مشکل اتصال ربات به صرافی AriaX (۳۰ ثانیه).
اجرا:  python3 diagnose.py
متغیرها از .env یا Environment خوانده می‌شود (ARIAX_BASE/KEY/SECRET).
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time

try:
    import aiohttp
except ImportError:
    print("pip install aiohttp"); sys.exit(1)

if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = os.getenv("ARIAX_BASE", "").rstrip("/")
KEY = os.getenv("ARIAX_KEY", "")
SECRET = os.getenv("ARIAX_SECRET", "")
CORRECT_BASE = "https://dryclean-app-1.onrender.com"
ETERNAL_KEY = "arx-karoon177-fixed-9d4e7c1a"

print("=" * 62)
print("  تشخیص اتصال ربات → صرافی AriaX")
print("=" * 62)

# ── [۱] آدرس ──
print(f"\n[۱] ARIAX_BASE = {BASE or '(خالی — پیش‌فرض استفاده نشده!)'}")
if not BASE:
    print("   ❌ ARIAX_BASE تنظیم نشده؛ از پیش‌فرض فایل استفاده می‌شود")
    BASE = CORRECT_BASE
    print(f"   → تلاش با: {BASE}")
elif "ariax-1.onrender.com" in BASE:
    print("   ❌❌ این آدرس دیگری است! (ariax-1 حذف شده و زیردامنه رندر به اپ")
    print(f"        دیگری واگذار شده) → آدرس صحیح: {CORRECT_BASE}")
elif BASE != CORRECT_BASE:
    print(f"   ⚠️ غیرمعمول — اگر عمدی نیست، صحیح: {CORRECT_BASE}")
else:
    print("   ✅ آدرس صحیح است")

# ── [۲] کلیدها ──
print(f"\n[۲] ARIAX_KEY = {KEY[:14] + '…' if KEY else '(خالی!)'}")
if not KEY or not SECRET:
    print("   ❌ کلید/سcret تنظیم نشده — مقادیر ابدی:")
    print(f"      ARIAX_KEY={ETERNAL_KEY}")
    print("      ARIAX_SECRET=Kf9mWx2vQz7RtN4pZb8LhY3jT6sEgUa5cHn1oPzIkM0yBvVXe")
elif KEY != ETERNAL_KEY:
    print("   ⚠️ این کلیدِ ابدی نیست — کلیدهای تصادفی با هر ری‌استارت می‌میرند!")
    print(f"      کلید ابدی: {ETERNAL_KEY}")
else:
    print("   ✅ کلید ابدی است")


async def main() -> int:
    score = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        # [۳] سرور
        try:
            h = await (await s.request("GET", f"{BASE}/healthz")).json()
            print(f"\n[۳] سرور: {'✅ در دسترس' if h.get('ok') else '❌ ' + str(h)[:80]}"
                  f" | users={h.get('users')} | uptime={h.get('uptime_s')}s")
            score += 1 if h.get("ok") else 0
        except Exception as e:
            print(f"\n[۳] سرور: ❌ {type(e).__name__} — آدرس/اینترنت را چک کنید")
            return 1
        # [۴] احراز هویت legacy
        HK = {"X-API-Key": KEY, "X-API-Secret": SECRET}
        w = await (await s.request("GET", f"{BASE}/api/wallet", headers=HK)).json()
        if w.get("ok"):
            print(f"[۴] کلید (legacy): ✅ معتبر | equity={w.get('equity')}")
            score += 1
        else:
            print(f"[۴] کلید (legacy): ❌ {w.get('error','')[:90]}")
            print("    → کلید/سcret در Environment ربات را با مقادیر ابدی عوض کنید")
        # [۵] احراز هویت v5 (امضای صحیح)
        if KEY and SECRET:
            ts = str(int(time.time() * 1000))
            qs = "accountType=UNIFIED"
            sig = hmac.new(SECRET.encode(), f"{ts}{KEY}10000{qs}".encode(),
                           hashlib.sha256).hexdigest()
            r = await (await s.request("GET", f"{BASE}/v5/account/wallet-balance?{qs}",
                       headers={"X-BAPI-API-KEY": KEY, "X-BAPI-SIGNATURE": sig,
                                "X-BAPI-TIMESTAMP": ts,
                                "X-BAPI-RECV-WINDOW": "10000"})).json()
            ok5 = r.get("retCode") == 0
            print(f"[۵] کلید (امضای v5): {'✅ معتبر' if ok5 else '❌ ' + r.get('retMsg','')[:80]}")
            score += 1 if ok5 else 0
        # [۶] سفارش آزمایشی کوچک (اگر احراز هویت OK)
        if w.get("ok"):
            o = await (await s.request("POST", f"{BASE}/api/order", headers=HK,
                       json={"symbol": "ETHUSD", "side": "buy", "type": "market",
                             "qty": 0.01, "lev": 5, "strategy": "diagnose"})).json()
            if o.get("ok"):
                c = await (await s.request("POST", f"{BASE}/api/order", headers=HK,
                           json={"symbol": "ETHUSD", "side": "sell",
                                 "type": "market", "qty": 0.01})).json()
                print(f"[۶] معامله آزمایشی: {'✅ باز و بسته شد' if c.get('ok') else '⚠️ باز شد، بستن ناموفق: ' + str(c.get('error',''))[:60]}")
                score += 1 if c.get("ok") else 0
            else:
                print(f"[۶] معامله آزمایشی: ❌ {o.get('error','')[:90]}")
    print("\n" + "=" * 62)
    print(f"  نتیجه: {score}/4 — "
          + ("✅ ربات می‌تواند معامله کند" if score >= 3
             else "❌ موارد بالا را اصلاح کنید (محتمل‌ترین: Environment ربات)"))
    print("=" * 62)
    return 0 if score >= 3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
