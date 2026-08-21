#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تست اتصال ربات به صرافی AriaX — ۶ مرحله، با پیام‌های فارسی دقیق.

اجرا:  python3 test_connection.py
نیازمند:  pip install aiohttp
متغیرها (یا فایل .env کنار همین فایل):
  ARIAX_BASE  (پیش‌فرض: https://dryclean-app-1.onrender.com)
  ARIAX_KEY , ARIAX_SECRET
"""
import asyncio
import os
import sys

try:
    import aiohttp
except ImportError:
    print("ابتدا نصب کنید:  pip install aiohttp"); sys.exit(1)

# --- بارگذاری .env ساده (بدون وابستگی) ---
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

BASE = os.getenv("ARIAX_BASE", "https://dryclean-app-1.onrender.com").rstrip("/")
KEY = os.getenv("ARIAX_KEY", "")
SECRET = os.getenv("ARIAX_SECRET", "")


async def main() -> int:
    print("=" * 60)
    print("  تست اتصال ربات → صرافی AriaX")
    print("=" * 60)
    score = 0

    # 0) آدرس
    print(f"\n[0] آدرس فعال: {BASE}")
    if "ariax-1.onrender.com" in BASE:
        print("  ❌ این آدرس سرویس مرده است!")
        print("     ✔ آدرس صحیح: https://dryclean-app-1.onrender.com")
        print("     (متغیر ARIAX_BASE را در .env یا تنظیمات Render اصلاح/حذف کنید)")
        return 1
    print("  ✅ آدرس صحیح است")
    score += 1

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        # 1) دامنه و سرور
        try:
            async with s.get(f"{BASE}/healthz") as r:
                ok = r.status == 200
                print(f"\n[1] سرور: {'✅ در دسترس' if ok else '❌ پاسخ ' + str(r.status)}")
                score += ok
        except Exception as e:
            print(f"\n[1] سرور: ❌ در دسترس نیست — {type(e).__name__}: {str(e)[:100]}")
            print("    (اینترنت/فایروال/غلط‌بودن دامنه را چک کنید)")
            return 1

        # 2) بازارها (عمومی)
        try:
            async with s.get(f"{BASE}/api/markets") as r:
                d = await r.json()
                ok = d.get("ok")
                n = len(d.get("data", {}))
                print(f"[2] بازارها: {'✅ ' + str(n) + ' نماد' if ok else '❌ ' + str(d)[:80]}")
                score += bool(ok)
        except Exception as e:
            print(f"[2] بازارها: ❌ {str(e)[:100]}")

        # 3) اعتبارنامه‌ها موجود؟
        print(f"\n[3] کلید: {'✅ تنظیم شده (' + KEY[:10] + '…)' if KEY else '❌ ARIAX_KEY تنظیم نشده!'}")
        score += bool(KEY)
        if not SECRET:
            print("    ❌ ARIAX_SECRET تنظیم نشده!")
        else:
            score += 1

        if not KEY or not SECRET:
            print("\n→ از سایت صرافی: «🔑 API ربات» → ساخت کلید")
            return 1

        # 4) احراز هویت legacy (مسیر ربات)
        h = {"X-API-Key": KEY, "X-API-Secret": SECRET}
        try:
            async with s.get(f"{BASE}/api/wallet", headers=h) as r:
                d = await r.json()
                if r.status == 200 and d.get("ok"):
                    sp = d.get("balances", {}).get("USDT", 0)
                    fu = (d.get("futures") or {}).get("balances", {}).get("USDT", 0)
                    print(f"[4] کیف پول: ✅ اسپات={sp:,.0f} | فیوچرز={fu:,.0f} USDT")
                    score += 1
                    if fu < 20 and sp > 40:
                        print("    ⚠ کیف فیوچرز خالی است — ربات خودش منتقل می‌کند؛ یا از سایت «⇄ انتقال» کنید")
                else:
                    print(f"[4] کیف پول: ❌ HTTP {r.status} — {str(d)[:120]}")
                    if r.status == 401:
                        print("    کلید/سcret معتبر نیست یا مربوط به این صرافی نیست؛ کلید جدید بسازید")
        except Exception as e:
            print(f"[4] کیف پول: ❌ {str(e)[:100]}")

        # 5) پوزیشن‌ها
        try:
            async with s.get(f"{BASE}/api/positions", headers=h) as r:
                d = await r.json()
                n = len(d.get("data", []))
                print(f"[5] پوزیشن‌ها: {'✅ ' + str(n) + ' باز' if d.get('ok') else '❌ ' + str(d)[:80]}")
                score += bool(d.get("ok"))
        except Exception as e:
            print(f"[5] پوزیشن‌ها: ❌ {str(e)[:100]}")

        # 6) کندل از خود صرافی
        try:
            async with s.get(f"{BASE}/v5/market/kline?category=linear&symbol=ETHUSDT&interval=5&limit=50") as r:
                d = await r.json()
                rows = ((d.get("result") or {}).get("list") or [])
                print(f"[6] کندل AriaX: {'✅ ' + str(len(rows)) + ' کندل ETHUSDT' if rows else '❌ ' + str(d)[:80]}")
                score += bool(rows)
        except Exception as e:
            print(f"[6] کندل AriaX: ❌ {str(e)[:100]}")

    print("\n" + "=" * 60)
    print(f"  نتیجه: {score}/7 مرحله موفق", "— ✅ ربات می‌تواند وصل شود" if score >= 6 else "— ❌ مراحل شکست‌خورده را اصلاح کنید")
    print("=" * 60)
    return 0 if score >= 6 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
