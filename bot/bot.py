#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Master Quant Engine v19.3 – AriaX Testnet (FIXED)
اجرا روی AriaX | کندل: AriaX خود صرافی → Bybit → OKX → Binance
مستندات: https://dryclean-app-1.onrender.com/docs
Env: ARIAX_KEY, ARIAX_SECRET, ARIAX_BASE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

FIXES v19.3:
  1) ARIAX_BASE → https://dryclean-app-1.onrender.com  (ariax-1.onrender.com مرده است)
  2) امضای Bybit v5 صحیح: X-BAPI-SIGNATURE (نه X-BAPI-SIGN) + امضای query-string برای GET
  3) پارس /api/config برای minq/step (کوانتیزه دقیق مقدار سفارش)
  4) انتقال خودکار وجه از کیف اسپات به کیف فیوچرز (free_margin از کیف فیوچرز خوانده می‌شود)
  5) کندل از خود AriaX (/v5/market/kline عمومی) به‌عنوان منبع اول — بدون وابستگی به IP بیرونی
  6) پارس کیف پول دوگانه (اسپات/فیوچرز)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from threading import Thread, Lock
from typing import Dict, Any, Optional, List

import aiohttp
import aiosqlite
import ccxt.async_support as ccxt
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

load_dotenv()

ARIAX_KEY    = os.getenv("ARIAX_KEY", "")
ARIAX_SECRET = os.getenv("ARIAX_SECRET", "")
ARIAX_BASE   = os.getenv("ARIAX_BASE", "https://dryclean-app-1.onrender.com").rstrip("/")
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT      = os.getenv("TELEGRAM_CHAT_ID", "")

# نمادهای AriaX (فرمت API) → جفت عمومی برای کندل
SYMBOL_MAP = {
    "ETHUSD":  "ETH/USDT",
    "SOLUSD":  "SOL/USDT",
    "XRPUSD":  "XRP/USDT",
    "AVAXUSD": "AVAX/USDT",
    "DOTUSD":  "DOT/USDT",
    "LINKUSD": "LINK/USDT",
    "ADAUSD":  "ADA/USDT",
    "DOGEUSD": "DOGE/USDT",
}
# FIX 5: نماد v5 برای کندل از خود صرافی (ETHUSD → ETHUSDT)
V5_SYMBOL = {k: k[:-3] + "USDT" for k in SYMBOL_MAP}
TF_V5 = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240"}
SYMBOLS = list(SYMBOL_MAP.keys())  # لیست معاملات روی AriaX

STRATEGY_PARAMS = {
    "Breakout_Momentum":   {"sl_m": 1.50, "tp_m": 3.6, "tp1_m": 1.9},
    "SuperTrend_Pullback": {"sl_m": 1.45, "tp_m": 3.1, "tp1_m": 1.65},
    "Volume_Surge":        {"sl_m": 1.40, "tp_m": 2.9, "tp1_m": 1.50},
    "RSI_Divergence":      {"sl_m": 1.55, "tp_m": 3.4, "tp1_m": 1.8},
    "RSI_Extreme_Bounce":  {"sl_m": 1.35, "tp_m": 2.6, "tp1_m": 1.40},
}

TIMEFRAME = "5m"
HTF_TIMEFRAME = "1h"
LEVERAGE = 5
MAX_POS = 5
MAX_DD = 10.0
MAX_DAILY_LOSS = 5.0
RISK_PCT = 0.40
MAX_NOTIONAL_USD = 80.0
MIN_ORDER_USD = 8.0
TEST_USD = 15.0
TAKER_FEE = 0.0005
FEE_BUFFER = 1.2
TRAIL_ACT = 3.2
TRAIL_STEP = 1.0
PARTIAL_TP = True
MIN_HOLD_FOR_PARTIAL = 720
MIN_HOLD_FOR_TRAIL = 1080
MIN_PROFIT_FOR_BE = 0.75
MAX_HOLD_SECONDS = 4 * 3600
TEST_SYMBOL = "ETHUSD"
POST_CLOSE_COOLDOWN = 1200
SCAN_INTERVAL = 70
SYMBOL_DELAY = 2.0
TREND_STRENGTH_THRESHOLD = 0.01
SYNC_INTERVAL = 60
GHOST_MISS_LIMIT = 3
OHLCV_PAUSE = 1.2  # فاصله بین درخواست کندل برای جلوگیری از بن

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("QuantV19.3")

SHARED_STATE: Dict[str, Any] = {
    "is_active": True, "dd_halted": False, "daily_halted": False,
    "balance": 0.0, "free_balance": 0.0, "peak_balance": 0.0,
    "day_start_balance": 0.0, "current_dd": 0.0, "daily_pnl": 0.0,
    "active_positions": {}, "last_scan": "Never", "last_sync": "Never",
    "stats": {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0},
    "fetch_stats": defaultdict(lambda: {"ok_5m": 0, "fail_5m": 0, "ok_1h": 0, "fail_1h": 0}),
    "recent_errors": [], "signal_but_not_executed": [], "trend_strengths": [],
    "exchange": "ariax-testnet",
}
STATE_LOCK = Lock()
SYMBOL_ERROR_COOLDOWN: Dict[str, float] = {}
SYMBOL_ERROR_COUNT: Dict[str, int] = {}
SYMBOL_POST_CLOSE_COOLDOWN: Dict[str, float] = {}
POSITION_MISS_COUNT: Dict[str, int] = {}


# ─────────────────────────────────────────────
# AriaX REST Client  (FIXED)
# ─────────────────────────────────────────────
class AriaXClient:
    """
    کلاینت AriaX Testnet v2.1
    - هدرهای legacy: X-API-Key / X-API-Secret (برای /api/*)
    - هدرهای Bybit v5 صحیح: X-BAPI-SIGNATURE + امضای query برای GET
    """

    def __init__(self, base: str, key: str, secret: str):
        self.base = base.rstrip("/")
        self.key = key or ""
        self.secret = secret or ""
        self._session: Optional[aiohttp.ClientSession] = None
        self.config: Dict[str, Any] = {}
        self.symbol_meta: Dict[str, Dict] = {}
        # FIX 4: موجودی دو کیف
        self.spot_avail = 0.0
        self.futures_avail = 0.0

    def _sign_headers(self, method: str, path: str, body_str: str = "") -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        recv = "5000"
        # FIX 2: برای GET امضا روی query-string است؛ برای POST روی raw body
        if method.upper() == "GET":
            payload = path.split("?", 1)[1] if "?" in path else ""
        else:
            payload = body_str
        sign = hmac.new(
            self.secret.encode("utf-8"),
            f"{ts}{self.key}{recv}{payload}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            # legacy (برای اندپوینت‌های /api/*)
            "X-API-Key": self.key,
            "X-API-Secret": self.secret,
            # Bybit v5 style (برای اندپوینت‌های /v5/*)
            "X-BAPI-API-KEY": self.key,
            "X-BAPI-SIGNATURE": sign,          # FIX: نام صحیح هدر
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
        }

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=55, connect=25)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _req(self, method: str, path: str, json_body=None) -> Any:
        s = await self.session()
        url = f"{self.base}{path}"
        body_str = ""
        if json_body is not None:
            body_str = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
        headers = self._sign_headers(method, path, body_str)
        for attempt in range(4):
            try:
                if method.upper() == "GET":
                    async with s.request(method, url, headers=headers) as r:
                        text = await r.text()
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {"raw": text[:500], "status": r.status}
                        if r.status >= 400:
                            raise Exception(f"HTTP {r.status}: {text[:300]}")
                        return data
                else:
                    async with s.request(method, url, headers=headers, data=body_str.encode("utf-8")) as r:
                        text = await r.text()
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {"raw": text[:500], "status": r.status}
                        if r.status >= 400:
                            raise Exception(f"HTTP {r.status}: {text[:300]}")
                        return data
            except asyncio.TimeoutError:
                wait = 4 + attempt * 4
                log.warning(f"AriaX timeout {path} try={attempt+1}/4 sleep={wait}s")
                if attempt == 3:
                    raise
                headers = self._sign_headers(method, path, body_str)
                await asyncio.sleep(wait)
            except aiohttp.ClientError as e:
                wait = 3 + attempt * 3
                log.warning(f"AriaX net {path}: {e} try={attempt+1}")
                if attempt == 3:
                    raise
                headers = self._sign_headers(method, path, body_str)
                await asyncio.sleep(wait)
        return {}

    # ---------- اندپوینت‌ها ----------
    async def get_markets(self):
        return await self._req("GET", "/api/markets")

    async def get_wallet(self):
        return await self._req("GET", "/api/wallet")

    async def get_positions(self):
        return await self._req("GET", "/api/positions")

    async def get_orders(self):
        return await self._req("GET", "/api/orders")

    async def get_fills(self):
        return await self._req("GET", "/api/fills")

    async def get_performance(self):
        return await self._req("GET", "/api/performance")

    async def get_config(self):
        try:
            data = await self._req("GET", "/api/config")
            self.config = data if isinstance(data, dict) else {}
            # FIX 3: فرمت واقعی پاسخ: {ok, data: {SYM: {minq, step, ...}}, ...}
            meta = (self.config.get("data") or self.config.get("symbols")
                    or self.config.get("markets") or self.config)
            if isinstance(meta, dict):
                for k, v in meta.items():
                    if isinstance(v, dict) and ("minq" in v or "step" in v):
                        self.symbol_meta[str(k).upper()] = v
            log.info(f"AriaX config: {len(self.symbol_meta)} symbols meta")
            return data
        except Exception as e:
            log.warning(f"config: {e}")
            return {}

    async def transfer_to_futures(self, amount: float) -> dict:
        """FIX 4: انتقال واقعی وجه اسپات→فیوچرز (API رابط کاربری)."""
        return await self._req("POST", "/api/transfer", json_body={
            "from": "spot", "to": "futures", "amount": float(amount)})

    async def ensure_futures_margin(self, min_free: float = 30.0) -> None:
        """اگر کیف فیوچرز خالی بود، به‌طور خودکار از اسپات شارژ کن."""
        try:
            w = await self.get_wallet()
            if not isinstance(w, dict) or not w.get("ok"):
                return
            fut = (w.get("futures") or {}).get("balances", {}).get("USDT", 0.0)
            flocks = (w.get("futures") or {}).get("locks", {}).get("USDT", 0.0)
            spot_free = (w.get("balances", {}).get("USDT", 0.0)
                         - w.get("locks", {}).get("USDT", 0.0))
            self.spot_avail = max(0.0, spot_free)
            self.futures_avail = max(0.0, fut - flocks)
            if self.futures_avail < min_free and self.spot_avail > min_free:
                move = round(min(self.spot_avail * 0.9, 5000.0), 2)
                if move >= 10:
                    r = await self.transfer_to_futures(move)
                    if r.get("ok"):
                        log.info(f"💸 auto-transfer spot→futures ${move:.2f}")
                    else:
                        log.warning(f"auto-transfer failed: {r.get('error')}")
        except Exception as e:
            log.warning(f"ensure_futures_margin: {e}")

    async def place_order(self, symbol: str, side: str, qty: float, lev: int = 5,
                          order_type: str = "market", price: float = None,
                          strategy: str = "") -> dict:
        body = {
            "symbol": symbol,
            "side": side.lower(),
            "type": order_type.lower(),
            "qty": float(qty),
            "lev": int(lev),
        }
        if strategy:
            body["strategy"] = str(strategy)[:40]
        if order_type.lower() == "limit" and price is not None:
            body["price"] = float(price)
        log.info(f"ARIAX ORDER {body}")
        return await self._req("POST", "/api/order", json_body=body)

    async def cancel_order(self, order_id) -> dict:
        return await self._req("POST", "/api/cancel", json_body={"id": order_id})

    def quantize_qty(self, symbol: str, qty: float) -> float:
        meta = self.symbol_meta.get(symbol.upper(), {})
        step = float(meta.get("step") or meta.get("qtyStep") or meta.get("stepSize") or 0)
        minq = float(meta.get("minq") or meta.get("minQty") or meta.get("min") or 0)
        if step > 0:
            qty = (int(qty / step)) * step
        if minq > 0 and qty < minq:
            qty = minq
        if qty >= 1:
            qty = round(qty, 4)
        elif qty >= 0.01:
            qty = round(qty, 6)
        else:
            qty = round(qty, 8)
        return max(qty, 0.0)

    # FIX 5: کندل از خود AriaX (عمومی، بدون کلید) — جدیدترین اول → قدیمی اول
    async def fetch_ariax_klines(self, ariax_sym: str, timeframe: str, limit: int = 100):
        v5 = V5_SYMBOL.get(ariax_sym)
        iv = TF_V5.get(timeframe)
        if not v5 or not iv:
            return []
        path = f"/v5/market/kline?category=linear&symbol={v5}&interval={iv}&limit={min(limit, 1000)}"
        try:
            data = await self._req("GET", path)
            rows = ((data or {}).get("result") or {}).get("list") or []
            # Bybit شکل: [ts, o, h, l, c, volume, turnover] جدیدترین اول
            out = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                    float(r[4]), float(r[5])] for r in reversed(rows)]
            return out
        except Exception as e:
            log.warning(f"ariax klines {ariax_sym}: {e}")
            return []


# ─────────────────────────────────────────────
# Database (unchanged)
# ─────────────────────────────────────────────
class Database:
    def __init__(self, path="bot.db"):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, symbol TEXT, side TEXT, strategy TEXT,
                entry_price REAL, qty REAL, original_qty REAL,
                sl REAL, tp1 REAL, tp REAL, is_partial INTEGER DEFAULT 0,
                highest_pnl_pct REAL DEFAULT 0, status TEXT DEFAULT 'open',
                pnl REAL DEFAULT 0, fees_est REAL DEFAULT 0,
                exit_reason TEXT, hold_seconds REAL DEFAULT 0,
                opened_at TEXT DEFAULT CURRENT_TIMESTAMP, closed_at TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT, action TEXT, strategy TEXT, reason TEXT,
                price REAL, rsi REAL, atr REAL, htf_trend TEXT, extra TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS equity (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT DEFAULT CURRENT_TIMESTAMP,
                balance REAL, peak REAL, dd REAL)""")
            await db.commit()

    async def insert_trade(self, t):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO trades
                (id,symbol,side,strategy,entry_price,qty,original_qty,sl,tp1,tp)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (t["id"], t["symbol"], t["side"], t["strategy"], t["entry"],
                 t["qty"], t["qty"], t["sl"], t["tp1"], t["tp"]))
            await db.commit()

    async def update_trade(self, tid, qty, sl, partial, hp):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE trades SET qty=?,sl=?,is_partial=?,highest_pnl_pct=? WHERE id=?",
                (qty, sl, partial, hp, tid))
            await db.commit()

    async def close_trade(self, tid, pnl, fees=0.0, reason="", hold=0.0):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE trades SET status='closed', pnl=?, fees_est=?, exit_reason=?,
                hold_seconds=?, closed_at=CURRENT_TIMESTAMP WHERE id=?""",
                (pnl, fees, reason, hold, tid))
            await db.commit()

    async def log_decision(self, symbol, action, strategy, reason, price=0, rsi=0, atr=0, htf="", extra=""):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO decisions (symbol,action,strategy,reason,price,rsi,atr,htf_trend,extra)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (symbol, action, strategy, reason, price, rsi, atr, htf, str(extra)[:500]))
            await db.commit()

    async def log_equity(self, balance, peak, dd):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO equity (balance,peak,dd) VALUES (?,?,?)", (balance, peak, dd))
            await db.commit()

    async def get_open_trades(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM trades WHERE status='open'") as c:
                return [dict(r) for r in await c.fetchall()]

    async def get_closed_trades(self, limit=40):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?", (limit,)) as c:
                return [dict(r) for r in await c.fetchall()]

    async def get_recent_decisions(self, limit=250):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,)) as c:
                return [dict(r) for r in await c.fetchall()]

    async def update_analytics(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                """SELECT pnl FROM trades WHERE status='closed'
                AND IFNULL(exit_reason,'') NOT LIKE 'ghost%'
                AND IFNULL(exit_reason,'') NOT LIKE 'startup%'""") as c:
                rows = await c.fetchall()
                if not rows:
                    with STATE_LOCK:
                        SHARED_STATE["stats"] = {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0}
                    return
                pnls = [r[0] for r in rows]
                wins = sum(1 for p in pnls if p > 0)
                with STATE_LOCK:
                    SHARED_STATE["stats"] = {
                        "total_trades": len(pnls),
                        "win_rate": round(wins / len(pnls) * 100, 1),
                        "total_pnl": round(sum(pnls), 2),
                    }

    async def generate_txt_report(self, prices=None, open_times=None):
        prices = prices or {}
        open_times = open_times or {}
        decisions = await self.get_recent_decisions(200)
        closed = await self.get_closed_trades(20)
        with STATE_LOCK:
            st = dict(SHARED_STATE)
            fetch_stats = dict(st.get("fetch_stats", {}))
            recent_errors = list(st.get("recent_errors", []))[-8:]
            gaps = list(st.get("signal_but_not_executed", []))[-8:]
            active = dict(st.get("active_positions", {}))
        now = time.time()
        lines = [
            "=" * 78,
            "     MASTER QUANT ENGINE v19.3  |  ARIAX TESTNET",
            f"     Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "=" * 78, "",
            "┌─ 1. SUMMARY ───────────────────────────────────────────────────────────",
            f"│  Total ${st.get('balance',0):,.2f}  Free ${st.get('free_balance',0):,.2f}",
            f"│  Risk {RISK_PCT}%  MaxNotional ${MAX_NOTIONAL_USD:.0f}  Lev {LEVERAGE}x",
            f"│  DD {st.get('current_dd',0):.2f}%  Open {len(active)}/{MAX_POS}",
            f"│  Trades {st.get('stats',{}).get('total_trades',0)}  WR {st.get('stats',{}).get('win_rate',0)}%  PnL ${st.get('stats',{}).get('total_pnl',0):+.2f}",
            f"│  Scan {st.get('last_scan')}  Sync {st.get('last_sync')}",
            "└────────────────────────────────────────────────────────────────────────", "",
            "┌─ 2. OPEN ──────────────────────────────────────────────────────────────",
        ]
        if not active:
            lines.append("│  (flat)")
        else:
            for pid, p in active.items():
                pr = prices.get(p["symbol"], p["entry"])
                upnl = (pr - p["entry"]) * p["qty"] * (1 if p["side"] == "buy" else -1)
                hold_h = (now - open_times.get(pid, now)) / 3600
                lines.append(f"│  {p['symbol']} {p['side'].upper()} {p.get('strategy','')} ${upnl:+.3f} {hold_h:.1f}h")
        lines += ["└────────────────────────────────────────────────────────────────────────", "",
                  "┌─ 3. DATA (candles: AriaX → Bybit → OKX → Binance) ─────────────────────"]
        for sym in SYMBOLS:
            s = fetch_stats.get(sym, {"ok_5m": 0, "fail_5m": 0, "ok_1h": 0, "fail_1h": 0})
            lines.append(f"│  {sym:<12} 5m {s['ok_5m']}/{s['fail_5m']}  1h {s['ok_1h']}/{s['fail_1h']}")
        lines += ["└────────────────────────────────────────────────────────────────────────", "",
                  "┌─ 4. CLOSED ────────────────────────────────────────────────────────────"]
        if not closed:
            lines.append("│  (none)")
        else:
            for t in closed[:10]:
                tag = "WIN" if t["pnl"] > 0 else "LOSS"
                lines.append(f"│  [{tag}] {t['symbol']:<12} ${t['pnl']:+.3f}  {t.get('exit_reason','')[:28]}")
        reasons = Counter()
        sigs = 0
        for d in decisions:
            if d["action"] in ("neutral", "rejected"):
                reasons[(d["reason"] or "?")[:50]] += 1
            else:
                sigs += 1
        lines += ["└────────────────────────────────────────────────────────────────────────", "",
                  "┌─ 5. DECISIONS ─────────────────────────────────────────────────────────",
                  f"│  Total {len(decisions)} | Sig {sigs} | Rej {len(decisions)-sigs}"]
        for reason, count in reasons.most_common(8):
            lines.append(f"│    {count:4d} × {reason}")
        lines += ["└────────────────────────────────────────────────────────────────────────", "",
                  "┌─ 6. GAPS / ERRORS ─────────────────────────────────────────────────────"]
        for g in gaps:
            lines.append(f"│  {g}")
        for e in recent_errors:
            lines.append(f"│  ERR {e}")
        if not gaps and not recent_errors:
            lines.append("│  (none)")
        lines += ["└────────────────────────────────────────────────────────────────────────", "",
                  "┌─ 7. LAST ──────────────────────────────────────────────────────────────"]
        for d in (decisions or [])[:12]:
            icon = "SIG" if d["action"] not in ("neutral", "rejected") else "REJ"
            lines.append(f"│  [{icon}] {(d.get('ts') or '')[:19]} {d['symbol']:<12} {d.get('reason','')[:40]}")
        lines += ["└────────────────────────────────────────────────────────────────────────", "=" * 78]
        return "\n".join(lines)


# ─────────────────────────────────────────────
# Indicators & Strategy (unchanged logic)
# ─────────────────────────────────────────────
class Indicators:
    @staticmethod
    def rsi(series, n=14):
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        ma_up = up.ewm(com=n - 1, adjust=False).mean()
        ma_down = down.ewm(com=n - 1, adjust=False).mean()
        rs = ma_up / ma_down.replace(0, 1e-10)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def atr(df, n=14):
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        return tr.ewm(com=n - 1, adjust=False).mean()

    @staticmethod
    def supertrend(df, period=10, mult=3.0):
        atr = Indicators.atr(df, period)
        hl2 = (df["high"] + df["low"]) / 2
        upper = hl2 + mult * atr
        lower = hl2 - mult * atr
        direction = pd.Series(1, index=df.index)
        for i in range(1, len(df)):
            if df["close"].iloc[i] > upper.iloc[i - 1]:
                direction.iloc[i] = 1
            elif df["close"].iloc[i] < lower.iloc[i - 1]:
                direction.iloc[i] = -1
            else:
                direction.iloc[i] = direction.iloc[i - 1]
                if direction.iloc[i] == 1 and lower.iloc[i] < lower.iloc[i - 1]:
                    lower.iloc[i] = lower.iloc[i - 1]
                if direction.iloc[i] == -1 and upper.iloc[i] > upper.iloc[i - 1]:
                    upper.iloc[i] = upper.iloc[i - 1]
        return direction, upper, lower

    @staticmethod
    def sma(s, p):
        return s.rolling(p).mean()

    @staticmethod
    def highest(s, p):
        return s.rolling(p).max()

    @staticmethod
    def lowest(s, p):
        return s.rolling(p).min()

    @staticmethod
    def detect_rsi_divergence(df, lookback=28):
        if len(df) < lookback + 5:
            return None
        close = df["close"].iloc[-lookback:]
        rsi = Indicators.rsi(df["close"]).iloc[-lookback:]
        price_lows, rsi_lows, price_highs, rsi_highs = [], [], [], []
        for i in range(3, len(close) - 3):
            if (close.iloc[i] < close.iloc[i-1] and close.iloc[i] < close.iloc[i-2] and
                close.iloc[i] < close.iloc[i-3] and close.iloc[i] < close.iloc[i+1] and close.iloc[i] < close.iloc[i+2]):
                price_lows.append((i, float(close.iloc[i])))
                rsi_lows.append((i, float(rsi.iloc[i])))
            if (close.iloc[i] > close.iloc[i-1] and close.iloc[i] > close.iloc[i-2] and
                close.iloc[i] > close.iloc[i-3] and close.iloc[i] > close.iloc[i+1] and close.iloc[i] > close.iloc[i+2]):
                price_highs.append((i, float(close.iloc[i])))
                rsi_highs.append((i, float(rsi.iloc[i])))
        if len(price_lows) >= 2 and len(rsi_lows) >= 2:
            p1, p2 = price_lows[-2], price_lows[-1]
            r1, r2 = rsi_lows[-2], rsi_lows[-1]
            if (p2[0] - p1[0]) >= 4 and p2[1] < p1[1] * 0.998 and r2[1] > r1[1] + 1.5:
                return "bullish"
        if len(price_highs) >= 2 and len(rsi_highs) >= 2:
            p1, p2 = price_highs[-2], price_highs[-1]
            r1, r2 = rsi_highs[-2], rsi_highs[-1]
            if (p2[0] - p1[0]) >= 4 and p2[1] > p1[1] * 1.002 and r2[1] < r1[1] - 1.5:
                return "bearish"
        return None


class StrategyEngine:
    def analyze(self, df_5m, df_1h, symbol=""):
        df = df_5m.iloc[:-1].copy()
        htf = df_1h.iloc[:-1].copy() if len(df_1h) > 30 else df
        if len(df) < 55 or float(df["close"].iloc[-1]) <= 0:
            return {"action": "neutral", "reason": "داده ناکافی", "strat": "", "rsi": 0, "atr": 0, "htf": ""}

        hclose = htf["close"]
        e50 = hclose.ewm(span=50, adjust=False).mean().iloc[-1]
        e200 = hclose.ewm(span=min(200, len(htf)), adjust=False).mean().iloc[-1]
        hp = float(hclose.iloc[-1])
        trend_strength = abs(e50 - e200) / (e200 + 1e-9) * 100

        with STATE_LOCK:
            SHARED_STATE["trend_strengths"].append({
                "ts": datetime.utcnow().strftime("%H:%M:%S"),
                "symbol": symbol, "value": round(trend_strength, 3),
            })
            if len(SHARED_STATE["trend_strengths"]) > 60:
                SHARED_STATE["trend_strengths"] = SHARED_STATE["trend_strengths"][-60:]

        if hp > e50 * 0.993 and e50 >= e200 * 0.990:
            htf_trend = "bullish"
        elif hp < e50 * 1.007 and e50 <= e200 * 1.010:
            htf_trend = "bearish"
        else:
            htf_trend = "sideways"

        c, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        price = float(c.iloc[-1])
        atr = float(Indicators.atr(df, 14).iloc[-1])
        if atr <= 0 or pd.isna(atr):
            return {"action": "neutral", "reason": "ATR صفر", "strat": "", "rsi": 0, "atr": 0, "htf": htf_trend}
        rsi = float(Indicators.rsi(c).iloc[-1])
        if pd.isna(rsi) or rsi <= 3 or rsi >= 97:
            return {"action": "neutral", "reason": f"RSI نامعتبر ({rsi:.1f})", "strat": "", "rsi": rsi, "atr": atr, "htf": htf_trend}

        ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        st_d, st_u, st_l = Indicators.supertrend(df)
        vsma = float(Indicators.sma(vol, 20).iloc[-1]) or 1e-9
        vcur = float(vol.iloc[-1])
        h12 = float(Indicators.highest(high, 12).iloc[-1])
        l12 = float(Indicators.lowest(low, 12).iloc[-1])
        vol_ok = vcur > vsma * 1.15
        candle_bull = c.iloc[-1] > c.iloc[-2]
        candle_bear = c.iloc[-1] < c.iloc[-2]

        if rsi < 22 and candle_bull and vcur > vsma * 0.9:
            return self._build("buy", "RSI_Extreme_Bounce", price, atr, rsi, htf_trend)
        if rsi > 78 and candle_bear and vcur > vsma * 0.9:
            return self._build("sell", "RSI_Extreme_Bounce", price, atr, rsi, htf_trend)

        if trend_strength < TREND_STRENGTH_THRESHOLD:
            return {"action": "neutral", "reason": f"روند ضعیف ({trend_strength:.3f}%)", "strat": "", "rsi": rsi, "atr": atr, "htf": "weak"}
        if htf_trend == "sideways":
            return {"action": "neutral", "reason": "روند HTF نامشخص", "strat": "", "rsi": rsi, "atr": atr, "htf": "sideways"}

        if htf_trend == "bullish" and price > ema20 * 1.0005 and price >= h12 * 0.997 and 42 < rsi < 70 and vol_ok:
            return self._build("buy", "Breakout_Momentum", price, atr, rsi, htf_trend)
        if htf_trend == "bearish" and price < ema20 * 0.9995 and price <= l12 * 1.003 and 30 < rsi < 58 and vol_ok:
            return self._build("sell", "Breakout_Momentum", price, atr, rsi, htf_trend)
        if htf_trend == "bullish" and st_d.iloc[-1] == 1 and low.iloc[-1] <= st_l.iloc[-1] * 1.008 and candle_bull and 38 < rsi < 68 and price > ema20:
            return self._build("buy", "SuperTrend_Pullback", price, atr, rsi, htf_trend)
        if htf_trend == "bearish" and st_d.iloc[-1] == -1 and high.iloc[-1] >= st_u.iloc[-1] * 0.992 and candle_bear and 32 < rsi < 62 and price < ema20:
            return self._build("sell", "SuperTrend_Pullback", price, atr, rsi, htf_trend)
        if htf_trend == "bullish" and price > ema20 and vcur > vsma * 1.35 and candle_bull and 43 < rsi < 70:
            return self._build("buy", "Volume_Surge", price, atr, rsi, htf_trend)
        if htf_trend == "bearish" and price < ema20 and vcur > vsma * 1.35 and candle_bear and 30 < rsi < 57:
            return self._build("sell", "Volume_Surge", price, atr, rsi, htf_trend)

        div = Indicators.detect_rsi_divergence(df, 28)
        if div == "bullish" and htf_trend == "bullish" and rsi < 45 and candle_bull and vcur > vsma * 1.05:
            return self._build("buy", "RSI_Divergence", price, atr, rsi, htf_trend)
        if div == "bearish" and htf_trend == "bearish" and rsi > 55 and candle_bear and vcur > vsma * 1.05:
            return self._build("sell", "RSI_Divergence", price, atr, rsi, htf_trend)

        return {"action": "neutral", "reason": f"بدون سیگنال (RSI={rsi:.1f})", "strat": "", "rsi": rsi, "atr": atr, "htf": htf_trend}

    def _build(self, side, strat, price, atr, rsi, htf):
        p = STRATEGY_PARAMS.get(strat, {"sl_m": 1.5, "tp_m": 3.2, "tp1_m": 1.7})
        if side == "buy":
            return {"action": side, "strat": strat,
                    "sl": price - atr * p["sl_m"], "tp": price + atr * p["tp_m"],
                    "tp1": price + atr * p["tp1_m"], "reason": f"سیگنال {strat}",
                    "rsi": rsi, "atr": atr, "htf": htf}
        return {"action": side, "strat": strat,
                "sl": price + atr * p["sl_m"], "tp": price - atr * p["tp_m"],
                "tp1": price - atr * p["tp1_m"], "reason": f"سیگنال {strat}",
                "rsi": rsi, "atr": atr, "htf": htf}


class RiskManager:
    @staticmethod
    def calculate_qty(ariax: AriaXClient, symbol: str, free: float, price: float, sl: float) -> float:
        if price <= 0 or free < 15:
            return 0.0
        dist = abs(price - sl)
        if dist <= 0 or dist / price < 0.003:
            dist = price * 0.008
        risk_usd = free * (RISK_PCT / 100.0)
        raw_qty = risk_usd / dist
        raw_qty = min(raw_qty, MAX_NOTIONAL_USD / price)
        if raw_qty * price < MIN_ORDER_USD:
            raw_qty = MIN_ORDER_USD / price
        max_by_margin = (free * LEVERAGE * 0.65) / price
        raw_qty = min(raw_qty, max_by_margin)
        qty = ariax.quantize_qty(symbol, raw_qty)
        if qty * price < MIN_ORDER_USD * 0.5:
            return 0.0
        if (qty * price) / LEVERAGE > free * 0.85:
            qty = ariax.quantize_qty(symbol, (free * 0.65 * LEVERAGE) / price)
        return max(qty, 0.0)


# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────
class TelegramController:
    def __init__(self, engine):
        self.engine = engine
        self.base = f"https://api.telegram.org/bot{TG_TOKEN}"
        self.offset = 0

    def menu(self):
        btn = "⏸️ Pause" if SHARED_STATE["is_active"] else "▶️ Start"
        act = "cmd_pause" if SHARED_STATE["is_active"] else "cmd_start"
        rows = [
            [{"text": "📊 Dashboard", "callback_data": "cmd_dash"}, {"text": "💼 Positions", "callback_data": "cmd_pos"}],
            [{"text": "🔄 Sync", "callback_data": "cmd_sync"}, {"text": btn, "callback_data": act}],
            [{"text": "📄 Report", "callback_data": "cmd_txt"}, {"text": "🚫 Rejections", "callback_data": "cmd_rej"}],
            [{"text": "⚡ REAL TEST", "callback_data": "cmd_realtest"}],
        ]
        with STATE_LOCK:
            positions = list(SHARED_STATE["active_positions"].items())
        for pid, p in positions[:5]:
            rows.append([{"text": f"❌ Close {p['symbol']} {p['side'].upper()}", "callback_data": f"close_{pid}"}])
        return {"inline_keyboard": rows}

    async def send(self, text, markup=None):
        if not TG_TOKEN:
            return
        if len(text) > 4000:
            text = text[:3900] + "\n..."
        payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
        if markup:
            payload["reply_markup"] = markup
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{self.base}/sendMessage", json=payload, timeout=12)
        except Exception as e:
            log.error(f"TG: {e}")

    async def send_document(self, path, caption=""):
        if not os.path.exists(path):
            return
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", TG_CHAT)
            form.add_field("caption", caption)
            form.add_field("document", open(path, "rb"), filename=os.path.basename(path))
            async with aiohttp.ClientSession() as s:
                await s.post(f"{self.base}/sendDocument", data=form, timeout=60)
        except Exception as e:
            await self.send(f"❌ {e}")

    async def poll(self):
        if not TG_TOKEN:
            while True:
                await asyncio.sleep(60)
            return
        await self.send(
            f"🚀 <b>v19.3 AriaX (FIXED)</b>\n"
            f"Base: {ARIAX_BASE}\n"
            f"Candles: AriaX → Bybit → OKX → Binance\n"
            f"Risk {RISK_PCT}% | Lev {LEVERAGE}x | Max ${MAX_NOTIONAL_USD:.0f}",
            self.menu())
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(f"{self.base}/getUpdates?offset={self.offset+1}&timeout=8") as r:
                        data = await r.json()
                        for u in data.get("result", []):
                            self.offset = u["update_id"]
                            if "callback_query" not in u:
                                continue
                            cb = u["callback_query"]
                            d = cb["data"]
                            try:
                                async with aiohttp.ClientSession() as ss:
                                    await ss.post(f"{self.base}/answerCallbackQuery",
                                                  json={"callback_query_id": cb["id"], "text": "OK"}, timeout=4)
                            except Exception:
                                pass
                            if d.startswith("close_"):
                                await self.engine.force_close(d.replace("close_", "", 1), "Manual_TG")
                                await self.send("✅ Close sent", self.menu())
                            elif d == "cmd_start":
                                with STATE_LOCK:
                                    SHARED_STATE["is_active"] = True
                                await self.send("▶️ Started", self.menu())
                            elif d == "cmd_pause":
                                with STATE_LOCK:
                                    SHARED_STATE["is_active"] = False
                                await self.send("⏸️ Paused", self.menu())
                            elif d == "cmd_dash":
                                with STATE_LOCK:
                                    st = dict(SHARED_STATE)
                                await self.send(
                                    f"📊 <b>v19.3 AriaX</b>\nTotal ${st['balance']:.2f}\nFree ${st.get('free_balance',0):.2f}\n"
                                    f"Pos {len(st['active_positions'])}/{MAX_POS}",
                                    self.menu())
                            elif d == "cmd_pos":
                                with STATE_LOCK:
                                    pos = dict(SHARED_STATE["active_positions"])
                                if not pos:
                                    await self.send("💤 flat", self.menu())
                                else:
                                    msg = "💼\n"
                                    for p in pos.values():
                                        pr = self.engine.prices.get(p["symbol"], p["entry"])
                                        pnl = (pr - p["entry"]) * p["qty"] * (1 if p["side"] == "buy" else -1)
                                        msg += f"{p['symbol']} {p['side']} ${pnl:+.2f}\n"
                                    await self.send(msg, self.menu())
                            elif d == "cmd_sync":
                                await self.engine.smart_sync()
                                await self.send("🔄 Sync done", self.menu())
                            elif d == "cmd_txt":
                                report = await self.engine.db.generate_txt_report(
                                    self.engine.prices, self.engine.open_times)
                                with open("report.txt", "w", encoding="utf-8") as f:
                                    f.write(report)
                                await self.send_document("report.txt", "📄 v19.3 AriaX")
                            elif d == "cmd_rej":
                                decs = await self.engine.db.get_recent_decisions(12)
                                msg = "🚫\n"
                                for x in decs:
                                    icon = "✅" if x["action"] not in ("neutral", "rejected") else "⛔"
                                    msg += f"{icon} {x['symbol']}\n{x['reason'][:60]}\n\n"
                                await self.send(msg, self.menu())
                            elif d == "cmd_realtest":
                                asyncio.create_task(self.engine.real_test_trade())
            except Exception as e:
                log.error(f"TG: {e}")
            await asyncio.sleep(1)


# ─────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────
class QuantEngine:
    def __init__(self):
        self.db = Database()
        self.strategy = StrategyEngine()
        self.risk = RiskManager()
        self.tg = TelegramController(self)
        self.ariax = AriaXClient(ARIAX_BASE, ARIAX_KEY, ARIAX_SECRET)
        # کندل عمومی: Bybit اول (IPهای Render اغلب روی Binance بن می‌شوند)
        self.pub_sources = []
        for cls_name, opts in (
            ("bybit", {"enableRateLimit": True, "options": {"defaultType": "spot"}}),
            ("okx", {"enableRateLimit": True}),
            ("binance", {"enableRateLimit": True, "options": {"defaultType": "spot"}}),
        ):
            try:
                cls = getattr(ccxt, cls_name)
                self.pub_sources.append(cls(opts))
            except Exception as e:
                log.warning(f"init {cls_name}: {e}")
        self.pub = self.pub_sources[0] if self.pub_sources else None
        self.prices: Dict[str, float] = {}
        self.open_times: Dict[str, float] = {}
        self._last_entry: Dict[str, float] = {}

    def _record_error(self, msg):
        with STATE_LOCK:
            errs = SHARED_STATE["recent_errors"]
            errs.append(f"{datetime.utcnow().strftime('%H:%M:%S')} {msg[:120]}")
            SHARED_STATE["recent_errors"] = errs[-25:]

    def _record_fetch(self, symbol, timeframe, success):
        with STATE_LOCK:
            s = SHARED_STATE["fetch_stats"][symbol]
            if timeframe == "5m":
                s["ok_5m" if success else "fail_5m"] += 1
            else:
                s["ok_1h" if success else "fail_1h"] += 1

    def _parse_wallet(self, data) -> tuple:
        """FIX 6: استخراج total/free از پاسخ دوکیفی AriaX v2.1"""
        if not isinstance(data, dict) or not data.get("ok"):
            return 0.0, 0.0
        # total = equity کل (اسپات + فیوچرز + مارجین + PnL شناور)
        total = float(data.get("equity") or data.get("balance") or 0)
        # free = مارجین آزاد کیف فیوچرز (ربات فقط فیوچرز معامله می‌کند)
        free = float(data.get("free_margin") or 0)
        if free <= 0 and total > 0:
            free = total
        return total, free

    def _parse_positions(self, data) -> Dict[str, dict]:
        """نرمال‌سازی لیست پوزیشن‌ها (فرمت AriaX: {ok, data:[{symbol,size,entry,...}]})"""
        out = {}
        rows = data
        if isinstance(data, dict):
            rows = data.get("data") or data.get("positions") or data.get("items") or []
            if not rows and data.get("symbol"):
                rows = [data]
        if not isinstance(rows, list):
            return out
        for p in rows:
            if not isinstance(p, dict):
                continue
            sym = str(p.get("symbol") or p.get("pair") or "").upper().replace("/", "").replace(":", "")
            if not sym:
                continue
            qty = float(p.get("qty") or p.get("size") or p.get("quantity") or p.get("contracts") or 0)
            if abs(qty) < 1e-12:
                continue
            side_raw = str(p.get("side") or p.get("positionSide") or "").lower()
            if side_raw in ("sell", "short", "s"):
                side = "sell"
                qty = abs(qty)
            elif side_raw in ("buy", "long", "b"):
                side = "buy"
                qty = abs(qty)
            else:
                side = "buy" if qty > 0 else "sell"   # AriaX: size علامت‌دار است
                qty = abs(qty)
            entry = float(p.get("entry") or p.get("entryPrice") or p.get("avgPrice") or p.get("price") or 0)
            out[sym] = {"symbol": sym, "side": side, "qty": qty, "entry": entry, "raw": p}
        return out

    def _parse_markets_price(self, data, symbol: str) -> float:
        if not data:
            return 0.0
        if isinstance(data, dict):
            if symbol in data:
                item = data[symbol]
                if isinstance(item, (int, float)):
                    return float(item)
                if isinstance(item, dict):
                    return float(item.get("price") or item.get("last") or item.get("mark") or item.get("close") or 0)
            markets = data.get("markets") or data.get("data") or data.get("prices") or data
            if isinstance(markets, dict) and symbol in markets:
                item = markets[symbol]
                if isinstance(item, (int, float)):
                    return float(item)
                if isinstance(item, dict):
                    return float(item.get("price") or item.get("last") or item.get("mark") or 0)
            if isinstance(markets, list):
                for m in markets:
                    if isinstance(m, dict) and str(m.get("symbol", "")).upper() == symbol:
                        return float(m.get("price") or m.get("last") or m.get("mark") or 0)
        if isinstance(data, list):
            for m in data:
                if isinstance(m, dict) and str(m.get("symbol", "")).upper() == symbol:
                    return float(m.get("price") or m.get("last") or m.get("mark") or 0)
        return 0.0

    async def fetch_ohlcv_public(self, ariax_sym: str, timeframe: str, limit: int = 100):
        pub_sym = SYMBOL_MAP.get(ariax_sym)
        if not pub_sym:
            self._record_fetch(ariax_sym, timeframe, False)
            return []
        actual = 50 if timeframe == "1h" else limit
        # FIX 5: اول خود AriaX (همان داده صرافی، بدون ریسک بن)
        try:
            candles = await self.ariax.fetch_ariax_klines(ariax_sym, timeframe, actual)
            if candles and len(candles) >= 30 and candles[-1][4] > 0:
                self._record_fetch(ariax_sym, timeframe, True)
                return candles
        except Exception as e:
            log.warning(f"ariax ohlcv {ariax_sym}: {str(e)[:80]}")
        last_err = ""
        for ex in self.pub_sources:
            try:
                candles = await ex.fetch_ohlcv(pub_sym, timeframe=timeframe, limit=actual)
                await asyncio.sleep(OHLCV_PAUSE)
                if candles and len(candles) >= 30 and candles[-1][4] > 0:
                    self._record_fetch(ariax_sym, timeframe, True)
                    return candles
            except Exception as e:
                last_err = str(e)[:80]
                log.warning(f"ohlcv {ex.id} {ariax_sym}: {last_err}")
                await asyncio.sleep(1.5)
                continue
        self._record_fetch(ariax_sym, timeframe, False)
        if last_err:
            self._record_error(f"ohlcv {ariax_sym}: {last_err}")
        return []

    async def get_live_price(self, symbol: str) -> float:
        if self.prices.get(symbol, 0) > 0:
            return self.prices[symbol]
        try:
            markets = await self.ariax.get_markets()
            px = self._parse_markets_price(markets, symbol)
            if px > 0:
                self.prices[symbol] = px
                return px
        except Exception as e:
            self._record_error(f"markets price: {e}")
        pub_sym = SYMBOL_MAP.get(symbol)
        if pub_sym:
            for ex in self.pub_sources:
                try:
                    t = await ex.fetch_ticker(pub_sym)
                    px = float(t.get("last") or t.get("close") or 0)
                    if px > 0:
                        self.prices[symbol] = px
                        return px
                except Exception:
                    continue
        return 0.0

    async def start(self):
        await self.db.init()
        log.info(f"v19.3 AriaX Testnet starting | base={ARIAX_BASE}")
        if not ARIAX_KEY or not ARIAX_SECRET:
            log.error("ARIAX_KEY / ARIAX_SECRET missing!")
        # GUARD: سلامت اتصال قبل از هر چیز — پیام خطای شفاف
        if not await self._health_check():
            for i in range(3):
                log.warning(f"اتصال برقرار نشد؛ تلاش مجدد {i+1}/3 تا ۱۵ ثانیه دیگر...")
                await asyncio.sleep(15)
                if await self._health_check():
                    break
            else:
                log.error("════════════════════════════════════════════════════════")
                log.error("❌ صرافی در دسترس نیست! چک‌لیست:")
                log.error("  ۱) ARIAX_BASE باید = https://dryclean-app-1.onrender.com")
                log.error("     (اگر در فایل .env یا تنظیمات Render مقدار قدیمی")
                log.error("      ariax-1.onrender.com هست، آن را حذف/اصلاح کنید)")
                log.error("  ۲) ARIAX_KEY / ARIAX_SECRET را از سایت اصلی بسازید")
                log.error("  ۳) این ربات به‌هرحال ادامه می‌دهد و هر ۱۵ ثانیه_RETRY می‌کند")
                log.error("════════════════════════════════════════════════════════")
        for ex in self.pub_sources:
            try:
                await ex.load_markets()
                log.info(f"Public candles source OK: {ex.id}")
                break
            except Exception as e:
                log.warning(f"public markets {ex.id}: {e}")
        await self._start_rest()

    async def _health_check(self) -> bool:
        """بررسی سلامت اتصال به صرافی با پیام دقیق خطا."""
        try:
            data = await self.ariax.get_markets()
            if isinstance(data, dict) and data.get("ok"):
                log.info(f"✅ AriaX OK: {len(data.get('data', {}))} نماد @ {ARIAX_BASE}")
                return True
            log.error(f"❌ پاسخ غیرمنتظره از {ARIAX_BASE}: {str(data)[:120]}")
        except Exception as e:
            log.error(f"❌ اتصال به {ARIAX_BASE} برقرار نشد: {str(e)[:140]}")
        if "ariax-1.onrender.com" in ARIAX_BASE:
            log.error("🚫 آدرس ariax-1.onrender.com سرویس مرده است!")
            log.error("   آدرس صحیح: https://dryclean-app-1.onrender.com")
        return False

    async def _start_rest(self):

        async def _warmup():
            for name, fn in (
                ("markets", self.ariax.get_markets),
                ("config", self.ariax.get_config),
                ("wallet", self.ariax.get_wallet),
            ):
                try:
                    data = await fn()
                    keys = list(data.keys())[:10] if isinstance(data, dict) else type(data)
                    log.info(f"AriaX {name} OK keys={keys}")
                    if name == "wallet":
                        total, free = self._parse_wallet(data)
                        with STATE_LOCK:
                            SHARED_STATE["balance"] = total
                            SHARED_STATE["free_balance"] = free
                            if total > SHARED_STATE["peak_balance"]:
                                SHARED_STATE["peak_balance"] = total
                        log.info(f"Wallet total=${total:.2f} free=${free:.2f}")
                except Exception as e:
                    log.warning(f"AriaX warmup {name}: {e}")
                    await asyncio.sleep(3)
            # FIX 4: اگر کیف فیوچرز خالی بود، خودمان شارژ کن
            await self.ariax.ensure_futures_margin(min_free=60.0)

        asyncio.create_task(_warmup())
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(self.smart_sync(startup=True), timeout=40)
        except Exception as e:
            log.warning(f"startup sync skip: {e}")
        await asyncio.gather(
            self.price_loop(), self.scan_loop(), self.watchdog_loop(),
            self.sync_loop(), self.tg.poll(),
        )

    async def update_balance(self):
        try:
            data = await self.ariax.get_wallet()
            total, free = self._parse_wallet(data)
            with STATE_LOCK:
                SHARED_STATE["balance"] = total
                SHARED_STATE["free_balance"] = free
                if total > SHARED_STATE["peak_balance"]:
                    SHARED_STATE["peak_balance"] = total
                if SHARED_STATE["day_start_balance"] <= 0 and total > 0:
                    SHARED_STATE["day_start_balance"] = total
            log.info(f"Wallet total=${total:.2f} free=${free:.2f}")
            # FIX 4: شارژ خودکار کیف فیوچرز اگر خالی شد (هر 15 ثانیه چک می‌شود)
            if free < 20 and self.ariax.spot_avail > 40:
                await self.ariax.ensure_futures_margin(min_free=60.0)
        except Exception as e:
            self._record_error(f"Wallet: {e}")
            log.error(f"Wallet: {e}")

    async def price_loop(self):
        while True:
            try:
                markets = await self.ariax.get_markets()
                for sym in SYMBOLS:
                    px = self._parse_markets_price(markets, sym)
                    if px > 0:
                        self.prices[sym] = px
                await self.update_balance()
                with STATE_LOCK:
                    cur = SHARED_STATE["balance"]
                    peak = SHARED_STATE["peak_balance"]
                    if peak > 0:
                        dd = (peak - cur) / peak * 100
                        SHARED_STATE["current_dd"] = dd
                        SHARED_STATE["dd_halted"] = dd >= MAX_DD
                    ds = SHARED_STATE["day_start_balance"]
                    if ds > 0:
                        SHARED_STATE["daily_pnl"] = cur - ds
                        SHARED_STATE["daily_halted"] = ((cur - ds) / ds * 100) <= -MAX_DAILY_LOSS
                await self.db.log_equity(cur, peak, SHARED_STATE.get("current_dd", 0))
            except Exception as e:
                log.error(f"price_loop: {e}")
            await asyncio.sleep(15)

    async def sync_loop(self):
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                await self.smart_sync()
            except Exception as e:
                log.error(f"sync: {e}")

    async def scan_loop(self):
        while True:
            with STATE_LOCK:
                can = (SHARED_STATE["is_active"] and not SHARED_STATE["dd_halted"] and
                       not SHARED_STATE["daily_halted"] and len(SHARED_STATE["active_positions"]) < MAX_POS)
                open_syms = {p["symbol"] for p in SHARED_STATE["active_positions"].values()}
            if not can:
                await asyncio.sleep(12)
                continue
            with STATE_LOCK:
                SHARED_STATE["last_scan"] = time.strftime("%H:%M:%S")
            for sym in SYMBOLS:
                if sym in open_syms:
                    continue
                if sym in SYMBOL_ERROR_COOLDOWN and time.time() < SYMBOL_ERROR_COOLDOWN[sym]:
                    continue
                if sym in SYMBOL_POST_CLOSE_COOLDOWN and time.time() < SYMBOL_POST_CLOSE_COOLDOWN[sym]:
                    continue
                try:
                    raw5 = await self.fetch_ohlcv_public(sym, TIMEFRAME, 100)
                    await asyncio.sleep(0.4)
                    raw1 = await self.fetch_ohlcv_public(sym, HTF_TIMEFRAME, 50)
                    await asyncio.sleep(SYMBOL_DELAY)
                    if not raw5 or len(raw5) < 50:
                        continue
                    df5 = pd.DataFrame(raw5, columns=["ts", "open", "high", "low", "close", "volume"])
                    df1 = pd.DataFrame(raw1, columns=["ts", "open", "high", "low", "close", "volume"]) if raw1 and len(raw1) > 30 else df5.copy()
                    last_close = float(df5["close"].iloc[-1])
                    if last_close > 0 and self.prices.get(sym, 0) <= 0:
                        self.prices[sym] = last_close
                    sig = self.strategy.analyze(df5, df1, symbol=sym)
                    price = self.prices.get(sym) or last_close
                    if price <= 0:
                        continue
                    await self.db.log_decision(sym, sig["action"], sig.get("strat", ""), sig.get("reason", ""),
                                               price, sig.get("rsi", 0), sig.get("atr", 0), sig.get("htf", ""))
                    if sig["action"] != "neutral":
                        atr = sig.get("atr", 0)
                        if atr > 0:
                            p = STRATEGY_PARAMS.get(sig.get("strat", ""), {"sl_m": 1.5, "tp_m": 3.2, "tp1_m": 1.7})
                            if sig["action"] == "buy":
                                sig["sl"] = price - atr * p["sl_m"]
                                sig["tp"] = price + atr * p["tp_m"]
                                sig["tp1"] = price + atr * p["tp1_m"]
                            else:
                                sig["sl"] = price + atr * p["sl_m"]
                                sig["tp"] = price - atr * p["tp_m"]
                                sig["tp1"] = price - atr * p["tp1_m"]
                        await self.execute_trade(sym, sig)
                except Exception as e:
                    log.error(f"scan {sym}: {e}")
                    self._record_error(f"scan {sym}: {e}")
                await asyncio.sleep(0.3)
            await asyncio.sleep(SCAN_INTERVAL)

    async def execute_trade(self, sym, sig):
        def record_miss(reason):
            with STATE_LOCK:
                lst = SHARED_STATE["signal_but_not_executed"]
                lst.append(f"{datetime.utcnow().strftime('%H:%M:%S')} {sym} {sig.get('strat')} → {reason}")
                SHARED_STATE["signal_but_not_executed"] = lst[-20:]

        if sym in SYMBOL_ERROR_COOLDOWN and time.time() < SYMBOL_ERROR_COOLDOWN[sym]:
            return

        price = await self.get_live_price(sym)
        if not price or price <= 0:
            record_miss("قیمت موجود نیست")
            return

        with STATE_LOCK:
            free = SHARED_STATE.get("free_balance") or SHARED_STATE["balance"]
            open_count = len(SHARED_STATE["active_positions"])
        if free < 15 or open_count >= MAX_POS:
            return

        try:
            await self.update_balance()
            with STATE_LOCK:
                free = SHARED_STATE.get("free_balance") or SHARED_STATE["balance"]

            qty = self.risk.calculate_qty(self.ariax, sym, free, price, sig["sl"])
            notional = qty * price
            margin = notional / LEVERAGE
            log.info(f"ORDER PREP {sym} {sig['action']} qty={qty} notional=${notional:.2f} margin=${margin:.2f} free=${free:.2f}")

            if qty <= 0 or notional < MIN_ORDER_USD * 0.5:
                record_miss(f"qty={qty}")
                return
            if margin > free * 0.9:
                record_miss(f"margin ${margin:.2f} > free")
                return
            # ── v19.3-fix: funding-drag guard (لانگ وقتی فاندینگ مثبت سنگین است نگیر) ──
            tkr = None
            try:
                mk = await self.ariax.get_markets()
                tkr = (mk.get("data") or {}).get(sym) or {}
            except Exception:
                tkr = {}
            fr_pct = float(tkr.get("funding") or 0)   # percent per 8h
            if abs(fr_pct) >= 0.30:
                pays = (sig["action"] == "buy" and fr_pct > 0) or (sig["action"] == "sell" and fr_pct < 0)
                if pays:
                    record_miss(f"funding {fr_pct:+.2f}%")
                    log.warning(f"SKIP {sym}: funding drag {fr_pct:+.2f}%/8h")
                    return
            # ── v19.3-fix: anti-overtrade cooldown per symbol ──
            now_ts = time.time()
            if now_ts - self._last_entry.get(sym, 0) < 240:
                record_miss("entry cooldown 240s")
                return
            self._last_entry[sym] = now_ts

            resp = await self.ariax.place_order(sym, sig["action"], qty, lev=LEVERAGE, order_type="market", strategy=sig.get("strat", ""))
            log.info(f"ORDER RESP {sym}: {str(resp)[:200]}")

            fill = price
            filled = qty
            if isinstance(resp, dict):
                fill = float(resp.get("avgPrice") or resp.get("price") or resp.get("fill_price")
                             or resp.get("entry") or price)
                filled = float(resp.get("qty") or resp.get("filled") or resp.get("size") or qty)
                err = resp.get("error") or resp.get("msg") or resp.get("message")
                if err and str(err).lower() not in ("ok", "success", "none", ""):
                    if resp.get("ok") is False or resp.get("success") is False:
                        raise Exception(str(err))

            pid = f"pos_{uuid.uuid4().hex[:8]}"
            pos = {
                "id": pid, "symbol": sym, "side": sig["action"], "strategy": sig["strat"],
                "entry": fill, "qty": filled, "sl": sig["sl"], "tp": sig["tp"], "tp1": sig["tp1"],
                "is_partial": 0, "highest_pnl_pct": 0.0,
            }
            with STATE_LOCK:
                SHARED_STATE["active_positions"][pid] = pos
            self.open_times[pid] = time.time()
            POSITION_MISS_COUNT[pid] = 0
            await self.db.insert_trade(pos)
            SYMBOL_ERROR_COUNT.pop(sym, None)
            SYMBOL_ERROR_COOLDOWN.pop(sym, None)
            await self.tg.send(
                f"🎯 <b>{sig['action'].upper()}</b> {sig['strat']}\n"
                f"{sym} @ {fill:.4f}\nQty {filled} | ~${filled*fill:.1f}\n🧪 AriaX Testnet",
                self.tg.menu())
            log.info(f"TRADE OPENED {sym} @ {fill:.4f}")
        except Exception as e:
            err = str(e)
            SYMBOL_ERROR_COUNT[sym] = SYMBOL_ERROR_COUNT.get(sym, 0) + 1
            count = SYMBOL_ERROR_COUNT[sym]
            SYMBOL_ERROR_COOLDOWN[sym] = time.time() + min(120 * count, 1800)
            await self.db.log_decision(sym, "rejected", sig.get("strat", ""), f"API: {err[:80]}")
            record_miss(err[:80])
            self._record_error(f"EXECUTE {sym}: {err[:80]}")
            await self.tg.send(f"❌ {sym}\n{err[:140]}")

    async def real_test_trade(self):
        await self.tg.send("⚡ Real test AriaX...")
        try:
            await self.update_balance()
            with STATE_LOCK:
                free = SHARED_STATE.get("free_balance") or SHARED_STATE["balance"]
            await self.tg.send(f"💰 Free ${free:.2f}")
            if free < 15:
                await self.tg.send("❌ موجودی کم")
                return
            price = await self.get_live_price(TEST_SYMBOL)
            if not price:
                await self.tg.send("❌ قیمت یافت نشد")
                return
            fake_sl = price * 0.99
            qty = self.risk.calculate_qty(self.ariax, TEST_SYMBOL, free, price, fake_sl)
            qty = self.ariax.quantize_qty(TEST_SYMBOL, min(qty, TEST_USD / price))
            notional = qty * price
            await self.tg.send(f"🧪 {TEST_SYMBOL} qty={qty}\n~${notional:.1f}")
            if qty <= 0:
                await self.tg.send("❌ qty=0")
                return
            resp = await self.ariax.place_order(TEST_SYMBOL, "buy", qty, lev=LEVERAGE)
            log.info(f"TEST RESP: {resp}")
            fill = price
            if isinstance(resp, dict):
                fill = float(resp.get("avgPrice") or resp.get("price") or price)
            filled = qty
            pid = f"test_{uuid.uuid4().hex[:6]}"
            pos = {
                "id": pid, "symbol": TEST_SYMBOL, "side": "buy", "strategy": "RealTest",
                "entry": fill, "qty": filled, "sl": fill * 0.97, "tp": fill * 1.03, "tp1": fill * 1.015,
                "is_partial": 0, "highest_pnl_pct": 0.0,
            }
            with STATE_LOCK:
                SHARED_STATE["active_positions"][pid] = pos
            self.open_times[pid] = time.time()
            await self.tg.send(f"🧪 opened @ {fill:.5f}")
            await asyncio.sleep(12)
            await self.force_close(pid, "RealTest")
            await self.tg.send("✅ test closed", self.tg.menu())
        except Exception as e:
            await self.tg.send(f"❌ Test:\n{str(e)[:220]}")
            self._record_error(f"real_test: {e}")

    async def smart_sync(self, startup=False):
        try:
            data = await self.ariax.get_positions()
            remote_map = self._parse_positions(data)
            log.info(f"Remote positions: {list(remote_map.keys()) or '(none)'}")

            with STATE_LOCK:
                local_items = list(SHARED_STATE["active_positions"].items())
            for pid, pos in local_items:
                if pos["strategy"] == "RealTest":
                    continue
                if pos["symbol"] in remote_map:
                    POSITION_MISS_COUNT[pid] = 0
                    continue
                POSITION_MISS_COUNT[pid] = POSITION_MISS_COUNT.get(pid, 0) + 1
                if POSITION_MISS_COUNT[pid] >= GHOST_MISS_LIMIT:
                    await self.db.close_trade(pid, 0.0, reason="ghost_confirmed", hold=0)
                    with STATE_LOCK:
                        SHARED_STATE["active_positions"].pop(pid, None)
                    self.open_times.pop(pid, None)
                    POSITION_MISS_COUNT.pop(pid, None)

            with STATE_LOCK:
                known = {p["symbol"] for p in SHARED_STATE["active_positions"].values()}
            for sym, rpos in remote_map.items():
                if sym in known:
                    continue
                entry = rpos["entry"] if rpos["entry"] > 0 else self.prices.get(sym, 0)
                if entry <= 0:
                    continue
                atr_est = entry * 0.012
                if rpos["side"] == "buy":
                    sl, tp, tp1 = entry - atr_est * 1.5, entry + atr_est * 3.2, entry + atr_est * 1.7
                else:
                    sl, tp, tp1 = entry + atr_est * 1.5, entry - atr_est * 3.2, entry - atr_est * 1.7
                pid = f"recovered_{uuid.uuid4().hex[:8]}"
                pos = {"id": pid, "symbol": sym, "side": rpos["side"], "strategy": "Recovered",
                       "entry": entry, "qty": rpos["qty"], "sl": sl, "tp": tp, "tp1": tp1,
                       "is_partial": 0, "highest_pnl_pct": 0.0}
                with STATE_LOCK:
                    SHARED_STATE["active_positions"][pid] = pos
                self.open_times[pid] = time.time()
                POSITION_MISS_COUNT[pid] = 0
                await self.db.insert_trade(pos)
                await self.tg.send(f"🔄 Recovered {sym}")

            if startup:
                for t in await self.db.get_open_trades():
                    if t["symbol"] not in remote_map:
                        await self.db.close_trade(t["id"], 0.0, reason="startup_ghost", hold=0)

            with STATE_LOCK:
                SHARED_STATE["last_sync"] = time.strftime("%H:%M:%S")
            log.info(f"Sync done active={len(SHARED_STATE['active_positions'])}")
        except Exception as e:
            log.error(f"smart_sync: {e}")
            self._record_error(f"smart_sync: {e}")

    async def force_close(self, pid, reason):
        with STATE_LOCK:
            pos = SHARED_STATE["active_positions"].get(pid)
        if not pos:
            return
        price = self.prices.get(pos["symbol"]) or await self.get_live_price(pos["symbol"]) or pos["entry"]
        hold = time.time() - self.open_times.get(pid, time.time())
        try:
            close_side = "sell" if pos["side"] == "buy" else "buy"
            await self.ariax.place_order(pos["symbol"], close_side, pos["qty"], lev=LEVERAGE, order_type="market")
            raw_pnl = (price - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "buy" else -1)
            fees = abs(price * pos["qty"]) * TAKER_FEE * 2 * FEE_BUFFER
            net = raw_pnl - fees
            if pos["strategy"] != "RealTest":
                await self.db.close_trade(pid, net, fees, reason, hold)
            with STATE_LOCK:
                SHARED_STATE["active_positions"].pop(pid, None)
            self.open_times.pop(pid, None)
            POSITION_MISS_COUNT.pop(pid, None)
            await self.db.update_analytics()
            SYMBOL_POST_CLOSE_COOLDOWN[pos["symbol"]] = time.time() + POST_CLOSE_COOLDOWN
            await self.tg.send(f"{'🟢' if net >= 0 else '🔴'} closed ({reason}) ${net:.2f}", self.tg.menu())
        except Exception as e:
            err = str(e)
            if any(x in err.lower() for x in ("not found", "no position", "already", "404")):
                await self.db.close_trade(pid, 0.0, 0, f"ghost_{reason}", hold)
                with STATE_LOCK:
                    SHARED_STATE["active_positions"].pop(pid, None)
                self.open_times.pop(pid, None)
                POSITION_MISS_COUNT.pop(pid, None)
            else:
                log.error(f"force_close: {e}")
                self._record_error(f"force_close: {e}")

    async def watchdog_loop(self):
        while True:
            with STATE_LOCK:
                items = list(SHARED_STATE["active_positions"].items())
            now = time.time()
            for pid, pos in items:
                if pos["strategy"] == "RealTest":
                    continue
                price = self.prices.get(pos["symbol"]) or await self.get_live_price(pos["symbol"])
                if not price:
                    continue
                hold = now - self.open_times.get(pid, now)
                if hold >= MAX_HOLD_SECONDS:
                    await self.force_close(pid, "MaxHold_4h")
                    continue
                can_partial = hold >= MIN_HOLD_FOR_PARTIAL
                can_trail = hold >= MIN_HOLD_FOR_TRAIL
                pnl_pct = ((price - pos["entry"]) / pos["entry"] * 100 if pos["side"] == "buy"
                           else (pos["entry"] - price) / pos["entry"] * 100)
                if can_trail and pnl_pct > TRAIL_ACT and pnl_pct > pos["highest_pnl_pct"]:
                    pos["highest_pnl_pct"] = pnl_pct
                    new_sl = price * (1 - TRAIL_STEP / 100) if pos["side"] == "buy" else price * (1 + TRAIL_STEP / 100)
                    if (pos["side"] == "buy" and new_sl > pos["sl"]) or (pos["side"] == "sell" and new_sl < pos["sl"]):
                        pos["sl"] = new_sl
                        await self.db.update_trade(pid, pos["qty"], pos["sl"], pos["is_partial"], pos["highest_pnl_pct"])
                if PARTIAL_TP and pos["is_partial"] == 0 and can_partial:
                    hit = ((pos["side"] == "buy" and price >= pos["tp1"]) or (pos["side"] == "sell" and price <= pos["tp1"]))
                    if hit and pnl_pct >= MIN_PROFIT_FOR_BE:
                        try:
                            half = self.ariax.quantize_qty(pos["symbol"], pos["qty"] / 2)
                            if half > 0:
                                cs = "sell" if pos["side"] == "buy" else "buy"
                                await self.ariax.place_order(pos["symbol"], cs, half, lev=LEVERAGE)
                                pos["qty"] -= half
                                pos["is_partial"] = 1
                                pos["sl"] = pos["entry"]
                                await self.db.update_trade(pid, pos["qty"], pos["sl"], 1, pos["highest_pnl_pct"])
                                await self.tg.send(f"🔹 Partial {pos['symbol']}")
                        except Exception as e:
                            log.error(f"partial: {e}")
                sl_hit = (pos["side"] == "buy" and price <= pos["sl"]) or (pos["side"] == "sell" and price >= pos["sl"])
                tp_hit = (pos["side"] == "buy" and price >= pos["tp"]) or (pos["side"] == "sell" and price <= pos["tp"])
                if sl_hit or tp_hit:
                    reason = "SL" if sl_hit else "TP"
                    if sl_hit and pos.get("is_partial") == 1 and abs(pos["sl"] - pos["entry"]) < 1e-8:
                        reason = "BE"
                    elif sl_hit and pos.get("highest_pnl_pct", 0) > TRAIL_ACT:
                        reason = "Trail"
                    await self.force_close(pid, reason)
            await asyncio.sleep(2.0)


app = Flask(__name__)


@app.route("/api/status")
def api_status():
    with STATE_LOCK:
        return jsonify(dict(SHARED_STATE))


@app.route("/")
def dashboard():
    return render_template_string("""
<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8"><title>v19.3 AriaX</title>
<style>body{font-family:system-ui;background:#0d1117;color:#c9d1d9;padding:20px}
h1{color:#7ee787}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px}
.value{font-size:1.2rem;font-weight:700;color:#7ee787}</style></head><body>
<h1>🚀 Quant v19.3 AriaX Testnet (FIXED)</h1>
<div class="grid">
<div class="card">Total<div class="value" id="bal">0</div></div>
<div class="card">Free<div class="value" id="free">0</div></div>
<div class="card">Pos<div class="value" id="pos">0</div></div>
<div class="card">PnL<div class="value" id="pnl">0</div></div>
</div>
<script>
async function r(){try{const d=await(await fetch('/api/status')).json();
document.getElementById('bal').textContent=(d.balance||0).toFixed(2);
document.getElementById('free').textContent=(d.free_balance||0).toFixed(2);
document.getElementById('pos').textContent=Object.keys(d.active_positions||{}).length;
document.getElementById('pnl').textContent=(d.stats?.total_pnl||0).toFixed(2);}catch(e){}}
r();setInterval(r,5000);</script></body></html>
""")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    def run_web():
        try:
            print(f"Flask on 0.0.0.0:{port}", flush=True)
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except Exception as e:
            print("Flask error:", e, flush=True)
            traceback.print_exc()

    try:
        print("=== Master Quant v19.3 AriaX Testnet (FIXED) starting ===", flush=True)
        print(f"ARIAX_BASE={ARIAX_BASE}", flush=True)
        Thread(target=run_web, daemon=True).start()
        time.sleep(1)
        engine = QuantEngine()
        print("Engine ready", flush=True)
        asyncio.run(engine.start())
    except Exception as e:
        print("FATAL:", e, flush=True)
        traceback.print_exc()
        time.sleep(20)
        raise
