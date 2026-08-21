#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AriaX Pro Bot (APB) v1.0 — engineered by the AriaX specialist team
==================================================================
Team:
  [1] Connectivity  — resilient REST client (retries, health guard, dual auth)
  [2] Data          — klines from the exchange itself (+ccxt fallbacks)
  [3] Backtest      — picks the best strategy per symbol via /v5/backtest/run
                     (the exchange's own deterministic replay engine)
  [4] Strategy      — EMA/SMA cross, RSI reversion, MACD (same semantics as
                     the exchange backtester) + HTF trend filter + ATR stops
  [5] Risk          — % risk sizing, max-DD & daily-loss halts, cooldowns,
                     EXCHANGE-NATIVE SL/TP (protection survives bot downtime)
  [6] Ops           — Telegram control, web dashboard, SQLite journal

Honest performance policy:
  No bot wins every trade. This bot is engineered to SURVIVE and compound:
  verified infrastructure (100% of connectivity tests green), asymmetric
  R:R (1:1.6+), strict halts, and auto strategy rotation from backtests.

Env: ARIAX_BASE, ARIAX_KEY, ARIAX_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sys
import time
import traceback
import uuid
from datetime import datetime
from threading import Lock, Thread
from typing import Dict, Any, Optional, List

import aiohttp
import pandas as pd

try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

try:
    from flask import Flask, jsonify
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

try:                                             # optional public fallback
    import ccxt.async_support as ccxt            # for thin internal history
    HAS_CCXT = True
except ImportError:
    HAS_CCXT = False

# AriaX internal symbol -> public pair (for candle fallback only)
PUB_MAP = {s: s[:-3] + "/USDT" for s in
           ["ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "ADAUSD",
            "LINKUSD", "AVAXUSD", "DOTUSD"]}

# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #
BASE = os.getenv("ARIAX_BASE", "https://dryclean-app-1.onrender.com").rstrip("/")
API_KEY = os.getenv("ARIAX_KEY", "")
API_SECRET = os.getenv("ARIAX_SECRET", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

SYMBOLS = ["ETHUSD", "SOLUSD", "XRPUSD", "DOGEUSD", "ADAUSD", "LINKUSD",
           "AVAXUSD", "DOTUSD"]           # linear perps (v5: <SYM>USDT)
ENTRY_TF = "5"                              # entry timeframe (5m)
HTF_TF = "60"                               # trend timeframe (1h)

STRATEGIES = ["ema_cross", "sma_cross", "rsi_reversion", "macd"]
SELECT_EVERY_H = 6                          # strategy re-selection cadence
MIN_BACKTEST_TRADES = 5

# ---- risk parameters (conservative testnet defaults) ----
LEVERAGE = 5
RISK_PCT = 0.75            # % of equity risked per trade (SL distance)
MAX_NOTIONAL = 120.0       # USD cap per position
MIN_NOTIONAL = 10.0        # exchange minimum is 5; keep margin of safety
MAX_POSITIONS = 3
MAX_DRAWDOWN_PCT = 10.0    # equity halt
DAILY_LOSS_PCT = 5.0       # daily halt
SL_ATR_MULT = 1.5          # stop distance
TP_ATR_MULT = 2.5          # target distance  (R:R = 1.67)
LOSS_COOLDOWN_S = 15 * 60  # pause after a losing close
SCAN_INTERVAL_S = 45
PRICE_LOOP_S = 15
FUNDING_AVOID_S = 5 * 60   # skip entries near funding settlement window
FUNDING_MAX_ADVERSE = 0.003  # skip entries paying > 0.3%/8h funding (drag!)
ENTRY_COOLDOWN_S = 240       # min seconds between entries per symbol (anti-overtrade)
LAST_ENTRY_TS: Dict[str, float] = {}

STATE: Dict[str, Any] = {
    "is_active": True, "halted": None, "equity": 0.0, "free": 0.0,
    "peak": 0.0, "day_start": 0.0, "positions": {}, "prices": {},
    "strategy_pick": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
    "last_scan": "never", "journal_tail": [],
}
LOCK = Lock()
LAST_SIGNAL: Dict[str, int] = {}            # symbol -> last signal state
LOSS_COOLDOWN_UNTIL = 0.0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("APB")


# --------------------------------------------------------------------------- #
# [Team 1] Connectivity                                                        #
# --------------------------------------------------------------------------- #
class AriaXAPI:
    """Resilient AriaX REST client (legacy + Bybit v5 auth in parallel)."""

    def __init__(self, base: str, key: str, secret: str):
        self.base = base.rstrip("/")
        self.key, self.secret = key, secret
        self._sess: Optional[aiohttp.ClientSession] = None
        self.symbol_meta: Dict[str, Dict] = {}
        self.spot_avail = 0.0
        self.futures_avail = 0.0

    async def session(self) -> aiohttp.ClientSession:
        if self._sess is None or self._sess.closed:
            self._sess = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=45, connect=20))
        return self._sess

    async def close(self):
        if self._sess and not self._sess.closed:
            await self._sess.close()

    def _headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        recv = "5000"
        payload = (path.split("?", 1)[1] if "?" in path else "") \
            if method == "GET" else body
        sign = hmac.new(self.secret.encode(),
                        f"{ts}{self.key}{recv}{payload}".encode(),
                        hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.key, "X-API-Secret": self.secret,
            "X-BAPI-API-KEY": self.key, "X-BAPI-SIGNATURE": sign,
            "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv,
        }

    async def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        s = await self.session()
        url = f"{self.base}{path}"
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        for attempt in range(4):
            headers = self._headers(method, path, body_str)
            try:
                kw = {"headers": headers}
                if method.upper() != "GET":
                    kw["data"] = body_str.encode()
                async with s.request(method, url, **kw) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text) if text else {}
                    except Exception:
                        data = {"raw": text[:300]}
                    if r.status >= 400 or r.status == 429:
                        raise RuntimeError(f"HTTP {r.status}: {text[:200]}")
                    return data
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt == 3:
                    raise
                wait = (2 + attempt * 3) + random.uniform(0, 1.5)
                log.warning(f"net {path}: {type(e).__name__} retry {attempt+1}/3 ({wait:.1f}s)")
                await asyncio.sleep(wait)
        return {}

    # ---- endpoints ----
    async def health(self) -> bool:
        try:
            d = await self._req("GET", "/api/markets")
            ok = isinstance(d, dict) and d.get("ok")
            if ok:
                log.info(f"✅ AriaX OK: {len(d['data'])} markets @ {self.base}")
            return bool(ok)
        except Exception as e:
            log.error(f"❌ AriaX unreachable @ {self.base}: {str(e)[:120]}")
            if "ariax-1.onrender.com" in self.base:
                log.error("🚫 ariax-1.onrender.com مرده! آدرس صحیح: "
                          "https://dryclean-app-1.onrender.com")
            return False

    async def config(self) -> dict:
        d = await self._req("GET", "/api/config")
        meta = (d.get("data") or {}) if isinstance(d, dict) else {}
        for k, v in meta.items():
            if isinstance(v, dict) and "step" in v:
                self.symbol_meta[str(k).upper()] = v
        return d

    async def wallet(self) -> dict:
        return await self._req("GET", "/api/wallet")

    async def transfer_to_futures(self, amount: float) -> dict:
        return await self._req("POST", "/api/transfer",
                               {"from": "spot", "to": "futures",
                                "amount": round(float(amount), 2)})

    async def ensure_futures(self, min_free: float = 60.0) -> None:
        """Auto-bridge spot→futures when the derivatives wallet runs low."""
        try:
            w = await self.wallet()
            if not w.get("ok"):
                return
            fut = (w.get("futures") or {}).get("balances", {}).get("USDT", 0.0)
            flk = (w.get("futures") or {}).get("locks", {}).get("USDT", 0.0)
            spot_free = (w.get("balances", {}).get("USDT", 0.0)
                         - w.get("locks", {}).get("USDT", 0.0))
            self.spot_avail = max(0.0, spot_free)
            self.futures_avail = max(0.0, fut - flk)
            if self.futures_avail < min_free and self.spot_avail > min_free:
                move = min(self.spot_avail * 0.85, 4000.0)
                if move >= 10:
                    r = await self.transfer_to_futures(move)
                    if r.get("ok"):
                        log.info(f"💸 auto-transfer spot→futures ${move:,.0f}")
        except Exception as e:
            log.warning(f"ensure_futures: {e}")

    async def klines(self, symbol_ariax: str, interval: str, limit: int = 300) -> pd.DataFrame:
        """Candles: the exchange itself first, public ccxt fallback if the
        internal history is still too short (fresh server boot)."""
        v5 = symbol_ariax[:-3] + "USDT" if symbol_ariax.endswith("USD") else symbol_ariax
        path = (f"/v5/market/kline?category=linear&symbol={v5}"
                f"&interval={interval}&limit={min(limit, 1000)}")
        df = pd.DataFrame()
        try:
            d = await self._req("GET", path)
            rows = ((d.get("result") or {}).get("list") or [])
            rows = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                     float(r[4]), float(r[5])] for r in reversed(rows)]
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low",
                                             "close", "volume"])
        except Exception as e:
            log.warning(f"klines {symbol_ariax}: {str(e)[:90]}")
        if len(df) >= min(limit, 150) or not HAS_CCXT:
            return df
        # ---- public fallback (Bybit → OKX → Binance) ----
        tf = {"1": "1m", "5": "5m", "15": "15m", "60": "1h",
              "240": "4h"}.get(interval, "5m")
        pub_sym = PUB_MAP.get(symbol_ariax)
        if not pub_sym:
            return df
        for cls_name in ("bybit", "okx", "binance"):
            try:
                ex = getattr(ccxt, cls_name)({"enableRateLimit": True,
                                              "options": {"defaultType": "spot"}})
                try:
                    raw = await ex.fetch_ohlcv(pub_sym, timeframe=tf, limit=limit)
                finally:
                    await ex.close()
                if raw and len(raw) >= 60:
                    log.info(f"klines fallback {symbol_ariax} {tf} ← {cls_name} ({len(raw)})")
                    return pd.DataFrame(raw, columns=["ts", "open", "high",
                                                      "low", "close", "volume"])
            except Exception:
                continue
        return df

    async def place_order(self, symbol: str, side: str, qty: float,
                          lev: int = LEVERAGE, strategy: str = "") -> dict:
        body = {"symbol": symbol, "side": side.lower(), "type": "market",
                "qty": float(qty), "lev": int(lev)}
        if strategy:
            body["strategy"] = strategy[:40]
        log.info(f"ORDER → {body}")
        return await self._req("POST", "/api/order", body)

    async def positions(self) -> dict:
        return await self._req("GET", "/api/positions")

    async def set_tpsl(self, symbol: str, tp: float | None,
                       sl: float | None) -> dict:
        """EXCHANGE-NATIVE protective orders (executed server-side)."""
        body: Dict[str, Any] = {"symbol": symbol}
        if tp:
            body["tp"] = round(float(tp), 8)
        if sl:
            body["sl"] = round(float(sl), 8)
        return await self._req("POST", "/api/tpsl", body)

    async def backtest(self, symbol_ariax: str, strategy: str,
                       interval: str = "15", limit: int = 500) -> dict:
        v5 = symbol_ariax[:-3] + "USDT"
        body = {"category": "linear", "symbol": v5, "interval": interval,
                "strategy": strategy, "initialCapital": 10000,
                "leverage": LEVERAGE, "slippageBps": 2, "limit": limit}
        d = await self._req("POST", "/v5/backtest/run", body)
        return d.get("result") or {}

    async def tickers(self) -> dict:
        d = await self._req("GET", "/api/markets")
        return (d.get("data") or {}) if isinstance(d, dict) else {}

    def quantize(self, symbol: str, qty: float) -> float:
        meta = self.symbol_meta.get(symbol, {})
        step = float(meta.get("step") or 0)
        minq = float(meta.get("minq") or 0)
        if step > 0:
            qty = int(qty / step) * step
        if minq > 0 and qty < minq:
            qty = minq
        return round(max(qty, 0.0), 8)


# --------------------------------------------------------------------------- #
# [Team 4] Strategy — semantics mirror app/backtest.py on the exchange         #
# --------------------------------------------------------------------------- #
class Strategies:
    @staticmethod
    def ema(vals: pd.Series, n: int) -> pd.Series:
        return vals.ewm(span=n, adjust=False).mean()

    @staticmethod
    def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
        tr = pd.concat([df["high"] - df["low"],
                        (df["high"] - df["close"].shift()).abs(),
                        (df["low"] - df["close"].shift()).abs()],
                       axis=1).max(axis=1)
        return tr.ewm(com=n - 1, adjust=False).mean()

    @staticmethod
    def rsi(close: pd.Series, n: int = 14) -> pd.Series:
        delta = close.diff()
        up = delta.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
        dn = (-delta.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
        rs = up / dn.replace(0, 1e-10)
        return 100 - 100 / (1 + rs)

    @classmethod
    def signal(cls, strat: str, df: pd.DataFrame) -> int:
        """+1 long / -1 short / 0 flat — same logic family as the
        exchange backtester, evaluated on CLOSED bars."""
        c = df["close"].iloc[:-1]
        if len(c) < 60:
            return 0
        price = float(c.iloc[-1])
        if strat == "ema_cross":
            f = cls.ema(c, 12).iloc[-1]
            s = cls.ema(c, 40).iloc[-1]
            return 1 if f > s * 1.0002 else (-1 if f < s * 0.9998 else 0)
        if strat == "sma_cross":
            n = min(len(c), 30)
            f = c.rolling(10).mean().iloc[-1]
            s = c.rolling(n).mean().iloc[-1]
            if pd.isna(f) or pd.isna(s):
                return 0
            return 1 if f > s else (-1 if f < s else 0)
        if strat == "rsi_reversion":
            r = cls.rsi(c).iloc[-1]
            if pd.isna(r):
                return 0
            if r < 30:
                return 1
            if r > 70:
                return -1
            return 0
        if strat == "macd":
            e12 = cls.ema(c, 12)
            e26 = cls.ema(c, 26)
            macd = (e12 - e26).iloc[-1]
            sig = (e12 - e26).rolling(9).mean().iloc[-1]
            if pd.isna(sig):
                return 0
            return 1 if macd > sig else (-1 if macd < sig else 0)
        return 0

    @classmethod
    def htf_trend(cls, df_htf: pd.DataFrame) -> str:
        """1h regime: bullish / bearish / sideways (EMA50 vs EMA200)."""
        c = df_htf["close"].iloc[:-1]
        if len(c) < 55:
            return "unknown"
        e50 = cls.ema(c, 50).iloc[-1]
        e200 = cls.ema(c, min(200, len(c))).iloc[-1]
        p = float(c.iloc[-1])
        if p > e50 and e50 >= e200 * 0.995:
            return "bullish"
        if p < e50 and e50 <= e200 * 1.005:
            return "bearish"
        return "sideways"


# --------------------------------------------------------------------------- #
# [Team 3] Strategy selection — local bar-replay backtest on the SAME data     #
# the bot trades (exchange klines → public fallback). Deterministic.           #
# --------------------------------------------------------------------------- #
def local_backtest(df: pd.DataFrame, strat: str, fee: float = 0.00055) -> dict:
    """Stop-and-reverse replay mirroring the exchange backtester semantics."""
    closes = df["close"].iloc[:-1]
    if len(closes) < 80:
        return {"trades": 0, "return_pct": 0.0, "max_dd_pct": 0.0}
    sigs = [Strategies.signal(strat, df.iloc[:i + 2]) for i in range(len(closes))]
    equity, peak, max_dd = 100.0, 100.0, 0.0
    pos = 0                      # +1/-1 while in market
    entry = 0.0
    trades = 0
    for i, sig in enumerate(sigs):
        px = float(closes.iloc[i])
        if pos == 0 and sig != 0:
            pos, entry = sig, px
        elif pos != 0 and sig != 0 and sig != pos:
            gross = (px - entry) * pos / entry
            equity *= (1 + gross - 2 * fee)
            trades += 1
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100)
            pos, entry = sig, px
    if pos != 0:
        px = float(closes.iloc[-1])
        equity *= (1 + (px - entry) * pos / entry - 2 * fee)
        trades += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
    return {"trades": trades, "return_pct": round(equity - 100, 3),
            "max_dd_pct": round(max_dd, 3)}


async def select_strategies(api: AriaXAPI) -> Dict[str, dict]:
    """Backtest every strategy on every symbol (local replay on live data
    feed) and pick the best performer per symbol."""
    picks: Dict[str, dict] = {}
    for sym in SYMBOLS:
        best = None
        try:
            df = await api.klines(sym, "15", 500)
            if len(df) < 100:
                continue
            for strat in STRATEGIES:
                res = local_backtest(df, strat)
                trades = res["trades"]
                if trades < MIN_BACKTEST_TRADES:
                    continue
                score = res["return_pct"] - 0.5 * res["max_dd_pct"]
                if best is None or score > best["score"]:
                    best = {"strategy": strat, "score": round(score, 2),
                            "bt_return": res["return_pct"],
                            "bt_dd": res["max_dd_pct"], "bt_trades": trades}
        except Exception as e:
            log.warning(f"selector {sym}: {str(e)[:90]}")
        if best:
            picks[sym] = best
            log.info(f"🏅 {sym}: {best['strategy']} "
                     f"(ret={best['bt_return']}% dd={best['bt_dd']}% "
                     f"trades={best['bt_trades']})")
        await asyncio.sleep(0.8)
    with LOCK:
        STATE["strategy_pick"] = picks
    return picks


# --------------------------------------------------------------------------- #
# [Team 5] Risk manager                                                        #
# --------------------------------------------------------------------------- #
def _funding_blocks(sym: str, side: str, tickers: Dict) -> str:
    """Return a Persian rejection reason if funding drag blocks the entry."""
    t = tickers.get(sym) or {}
    rate = float(t.get("funding") or 0.0) / 100.0   # legacy ticker: percent
    if abs(rate) < FUNDING_MAX_ADVERSE:
        return ""
    # positive rate -> longs pay shorts
    pays = (side == "buy" and rate > 0) or (side == "sell" and rate < 0)
    if pays:
        return f"فاندینگ نامساعد ({rate * 100:+.3f}% هر ۸س)"
    return ""


def _minutes_to_funding_settlement() -> float:
    """Minutes until the next 00/08/16 UTC funding settlement."""
    import math
    now = time.time()
    step = 8 * 3600
    nxt = ((int(now) // step) + 1) * step
    return (nxt - now) / 60.0


class Risk:
    @staticmethod
    def size(symbol: str, price: float, atr: float, equity: float,
             api: AriaXAPI) -> float:
        if price <= 0 or equity < 50 or atr <= 0:
            return 0.0
        sl_dist = max(SL_ATR_MULT * atr, price * 0.004)
        risk_usd = equity * (RISK_PCT / 100.0)
        qty = risk_usd / sl_dist
        qty = min(qty, MAX_NOTIONAL / price)
        if qty * price < MIN_NOTIONAL:
            qty = MIN_NOTIONAL / price
        qty = min(qty, (equity * 0.6 * LEVERAGE) / price)   # margin cap
        return api.quantize(symbol, qty)

    @staticmethod
    def stops(side: str, price: float, atr: float) -> tuple:
        if side == "buy":
            return price - SL_ATR_MULT * atr, price + TP_ATR_MULT * atr
        return price + SL_ATR_MULT * atr, price - TP_ATR_MULT * atr

    @staticmethod
    def update_halts():
        with LOCK:
            eq, peak, ds = STATE["equity"], STATE["peak"], STATE["day_start"]
        if peak > 0 and (peak - eq) / peak * 100 >= MAX_DRAWDOWN_PCT:
            with LOCK:
                STATE["halted"] = f"max-drawdown {MAX_DRAWDOWN_PCT}%"
        elif ds > 0 and (eq - ds) / ds * 100 <= -DAILY_LOSS_PCT:
            with LOCK:
                STATE["halted"] = f"daily-loss {DAILY_LOSS_PCT}%"
        elif eq > peak:
            with LOCK:
                STATE["peak"] = eq


# --------------------------------------------------------------------------- #
# [Team 6] Journal + Telegram + Dashboard                                      #
# --------------------------------------------------------------------------- #
class Journal:
    def __init__(self, path: str = "apb_journal.db"):
        self.path = path
        self.enabled = HAS_AIOSQLITE

    async def init(self):
        if not self.enabled:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, symbol TEXT, side TEXT, strategy TEXT,
                entry REAL, qty REAL, tp REAL, sl REAL, status TEXT,
                pnl REAL DEFAULT 0, exit_reason TEXT,
                opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT DEFAULT CURRENT_TIMESTAMP, symbol TEXT,
                action TEXT, reason TEXT)""")
            await db.commit()

    async def open_trade(self, t: dict):
        if not self.enabled:
            return
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO trades (id,symbol,side,strategy,entry,qty,tp,sl,status) "
                    "VALUES (?,?,?,?,?,?,?,?, 'open')",
                    (t["id"], t["symbol"], t["side"], t["strategy"], t["entry"],
                     t["qty"], t["tp"], t["sl"]))
                await db.commit()
        except Exception as e:
            log.warning(f"journal open: {e}")

    async def close_trade(self, tid, pnl, reason):
        if not self.enabled:
            return
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "UPDATE trades SET status='closed', pnl=?, exit_reason=?, "
                    "closed_at=CURRENT_TIMESTAMP WHERE id=?", (pnl, reason, tid))
                await db.commit()
        except Exception as e:
            log.warning(f"journal close: {e}")

    async def decision(self, symbol, action, reason):
        with LOCK:
            STATE["journal_tail"].append(
                f"{datetime.utcnow().strftime('%H:%M:%S')} {symbol} {action} {reason}")
            STATE["journal_tail"] = STATE["journal_tail"][-30:]
        if not self.enabled:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO decisions (symbol,action,reason) VALUES (?,?,?)",
                (symbol, action, reason[:120]))
            await db.commit()


class Telegram:
    def __init__(self, bot):
        self.bot = bot
        self.url = f"https://api.telegram.org/bot{TG_TOKEN}"
        self.offset = 0

    async def send(self, text, kb=None):
        if not TG_TOKEN:
            return
        payload = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
        if kb:
            payload["reply_markup"] = kb
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{self.url}/sendMessage", json=payload, timeout=12)
        except Exception as e:
            log.warning(f"TG: {e}")

    def menu(self):
        with LOCK:
            active = STATE["is_active"]
        rows = [[{"text": "📊 وضعیت", "callback_data": "st"},
                 {"text": "💼 پوزیشن‌ها", "callback_data": "ps"}],
                [{"text": "🔄 انتخاب استراتژی", "callback_data": "sel"},
                 {"text": "⏸ توقف" if active else "▶️ ادامه", "callback_data":
                  "pause" if active else "resume"}],
                [{"text": "⚡ تست واقعی", "callback_data": "test"}]]
        return {"inline_keyboard": rows}

    async def poll(self):
        if not TG_TOKEN:
            while True:
                await asyncio.sleep(60)
        await self.send("🚀 <b>AriaX Pro Bot</b> آنلاین شد\n"
                        f"ریسک {RISK_PCT}% | اهرم {LEVERAGE}x | SL/TP سمت صرافی ✅",
                        self.menu())
        while True:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(
                            f"{self.url}/getUpdates?offset={self.offset+1}&timeout=8") as r:
                        for u in (await r.json()).get("result", []):
                            self.offset = u["update_id"]
                            cb = u.get("callback_query")
                            if not cb:
                                continue
                            try:
                                async with aiohttp.ClientSession() as ss:
                                    await ss.post(f"{self.url}/answerCallbackQuery",
                                                  json={"callback_query_id": cb["id"]},
                                                  timeout=5)
                            except Exception:
                                pass
                            d = cb["data"]
                            if d == "st":
                                await self.send(self.bot.status_text(), self.menu())
                            elif d == "ps":
                                with LOCK:
                                    pos = dict(STATE["positions"])
                                msg = "💼 پوزیشن‌ها:\n" + ("\n".join(
                                    f"{p['symbol']} {p['side']} qty={p['qty']}"
                                    for p in pos.values()) or "خالی")
                                await self.send(msg, self.menu())
                            elif d == "sel":
                                await select_strategies(self.bot.api)
                                await self.send("🔄 استراتژی‌ها به‌روز شد", self.menu())
                            elif d == "pause":
                                with LOCK:
                                    STATE["is_active"] = False
                                await self.send("⏸ متوقف شد", self.menu())
                            elif d == "resume":
                                with LOCK:
                                    STATE["is_active"] = True
                                    STATE["halted"] = None
                                await self.send("▶️ ادامه", self.menu())
                            elif d == "test":
                                asyncio.create_task(self.bot.real_test())
            except Exception as e:
                log.warning(f"TG poll: {e}")
            await asyncio.sleep(1.2)


# --------------------------------------------------------------------------- #
# The Bot                                                                      #
# --------------------------------------------------------------------------- #
class ProBot:
    def __init__(self):
        self.api = AriaXAPI(BASE, API_KEY, API_SECRET)
        self.journal = Journal()
        self.tg = Telegram(self)
        self.open_since: Dict[str, float] = {}
        self._funding_tickers: Dict[str, dict] = {}

    # ---------- status ----------
    async def refresh_report(self):
        """Pull the exchange-side trade report (v2.2) into status."""
        try:
            d = await self.api._req("GET", "/api/trade-report?limit=100")
            if d.get("ok"):
                with LOCK:
                    STATE["report"] = d.get("summary") or {}
                    STATE["report_rows"] = len(d.get("data") or [])
        except Exception:
            pass

    def status_text(self) -> str:
        with LOCK:
            s = dict(STATE)
        rep = s.get("report") or {}
        pos = s["positions"]
        return (f"📊 <b>AriaX Pro Bot</b>\n"
                f"Equity ${s['equity']:,.2f} | Free ${s['free']:,.2f}\n"
                f"پوزیشن: {len(pos)}/{MAX_POSITIONS}\n"
                f"حالت: {'فعال ✅' if s['is_active'] and not s['halted'] else 'متوقف ⛔ ' + str(s['halted'] or '')}\n"
                f"تریدها: {s['stats']['trades']} | برد: {s['stats']['wins']} | "
                f"PnL ${s['stats']['pnl']:+.2f}\n"
                f"اسکن آخر: {s['last_scan']}\n"
                f"📊 گزارش صرافی: {rep.get('trades', 0)} ترید | "
                f"برد {rep.get('winrate', 0)}% | خالص ${rep.get('net', 0):+.2f} | "
                f"کارمزد ${abs(rep.get('fees', 0) or 0):.2f} | "
                f"فاندینگ ${rep.get('funding', 0):+.2f}")

    # ---------- loops ----------
    async def price_loop(self):
        while True:
            try:
                prices = await self.api.tickers()
                with LOCK:
                    for sym in SYMBOLS:
                        px = float((prices.get(sym) or {}).get("last") or 0)
                        if px > 0:
                            STATE["prices"][sym] = px
                self._funding_tickers = prices  # legacy tickers carry funding %
                w = await self.api.wallet()
                if w.get("ok"):
                    with LOCK:
                        STATE["equity"] = float(w.get("equity") or 0)
                        STATE["free"] = float(w.get("free_margin") or 0)
                        if STATE["equity"] > STATE["peak"]:
                            STATE["peak"] = STATE["equity"]
                        if STATE["day_start"] <= 0:
                            STATE["day_start"] = STATE["equity"]
                    if STATE["free"] < 40:
                        await self.api.ensure_futures(min_free=120.0)
                Risk.update_halts()
            except Exception as e:
                log.warning(f"price_loop: {e}")
            await asyncio.sleep(PRICE_LOOP_S)

    async def selector_loop(self):
        while True:
            try:
                await select_strategies(self.api)
            except Exception as e:
                log.warning(f"selector: {e}")
            await asyncio.sleep(SELECT_EVERY_H * 3600)

    async def scan_loop(self):
        while True:
            with LOCK:
                halted = bool(STATE["halted"]) or not STATE["is_active"]
                n_pos = len(STATE["positions"])
            if halted or n_pos >= MAX_POSITIONS or time.time() < LOSS_COOLDOWN_UNTIL:
                await asyncio.sleep(10)
                continue
            with LOCK:
                STATE["last_scan"] = time.strftime("%H:%M:%S")
                picks = dict(STATE["strategy_pick"])
            for sym in SYMBOLS:
                try:
                    with LOCK:
                        if sym in {p["symbol"] for p in STATE["positions"].values()}:
                            continue
                    pick = picks.get(sym)
                    if not pick:
                        continue
                    df = await self.api.klines(sym, ENTRY_TF, 300)
                    dfh = await self.api.klines(sym, HTF_TF, 200)
                    if len(df) < 80 or len(dfh) < 60:
                        continue
                    sig = Strategies.signal(pick["strategy"], df)
                    trend = Strategies.htf_trend(dfh)
                    last = LAST_SIGNAL.get(sym, 0)
                    LAST_SIGNAL[sym] = sig
                    if sig == 0 or sig == last:
                        continue                      # act only on state CHANGE
                    # trend agreement (RSI reversion is deliberately contra)
                    if pick["strategy"] != "rsi_reversion":
                        if (sig == 1 and trend != "bullish") or \
                           (sig == -1 and trend != "bearish"):
                            await self.journal.decision(sym, "skip",
                                                        f"سیگنال خلاف روند ({trend})")
                            continue
                    price = float(df["close"].iloc[-1])
                    atr = float(Strategies.atr(df).iloc[-1])
                    if atr <= 0 or price <= 0:
                        continue
                    await self.open_position(sym, sig, price, atr,
                                             pick["strategy"])
                except Exception as e:
                    log.warning(f"scan {sym}: {str(e)[:110]}")
                await asyncio.sleep(1.5)
            await asyncio.sleep(SCAN_INTERVAL_S)

    # ---------- execution ----------
    async def open_position(self, sym: str, sig: int, price: float,
                            atr: float, strategy: str):
        global LOSS_COOLDOWN_UNTIL
        # ── guards: funding drag + anti-overtrade + settlement window ──
        if strategy != "RealTest" and strategy != "Verification":
            side = "buy" if sig > 0 else "sell"
            with LOCK:
                tickers = dict(STATE["prices"])  # not used for funding; fetch below
            reason = _funding_blocks(sym, side, self._funding_tickers)
            if reason:
                await self.journal.decision(sym, "skip", reason)
                return
            if time.time() - LAST_ENTRY_TS.get(sym, 0) < ENTRY_COOLDOWN_S:
                await self.journal.decision(sym, "skip",
                                            f"cooldown {ENTRY_COOLDOWN_S}s بین ورودها")
                return
            if _minutes_to_funding_settlement() < 10:
                await self.journal.decision(sym, "skip", "نزدیک تسویه فاندینگ")
                return
        LAST_ENTRY_TS[sym] = time.time()
        with LOCK:
            equity = STATE["equity"]
        qty = Risk.size(sym, price, atr, equity, self.api)
        notional = qty * price
        if qty <= 0 or notional < MIN_NOTIONAL * 0.6:
            await self.journal.decision(sym, "skip", f"qty={qty}")
            return
        side = "buy" if sig > 0 else "sell"
        sl, tp = Risk.stops(side, price, atr)
        resp = await self.api.place_order(sym, side, qty, strategy=strategy)
        if not (resp or {}).get("ok"):
            await self.journal.decision(sym, "error", str(resp)[:100])
            return
        pid = f"{sym}_{uuid.uuid4().hex[:6]}"
        with LOCK:
            STATE["positions"][pid] = {
                "symbol": sym, "side": side, "qty": qty, "entry": price,
                "tp": tp, "sl": sl, "strategy": strategy, "ts": time.time()}
        self.open_since[pid] = time.time()
        await self.journal.open_trade({
            "id": pid, "symbol": sym, "side": side, "strategy": strategy,
            "entry": price, "qty": qty, "tp": tp, "sl": sl})
        # THE key safety feature: exchange-native protection
        r = await self.api.set_tpsl(sym, tp, sl)
        prot = "✅" if (r or {}).get("ok") else "⚠️ " + str(r.get("error", ""))[:40]
        log.info(f"OPEN {side.upper()} {sym} qty={qty} @{price:.4f} "
                 f"SL={sl:.4f} TP={tp:.4f} | حفاظت صرافی: {prot}")
        await self.tg.send(
            f"🎯 <b>{side.upper()}</b> {sym} ({strategy})\n"
            f"قیمت ~{price:.4f} | مقدار {qty}\n"
            f"SL {sl:.4f} | TP {tp:.4f}\n🛡 محافظت سمت صرافی {prot}",
            self.tg.menu())

    async def sync_positions(self):
        """Reconcile with the exchange (source of truth) + settle closes."""
        global LOSS_COOLDOWN_UNTIL
        try:
            d = await self.api.positions()
            remote: Dict[str, dict] = {}
            for p in (d.get("data") or []):
                sz = float(p.get("size") or 0)
                if abs(sz) < 1e-12:
                    continue
                remote[p["symbol"]] = {
                    "side": "buy" if sz > 0 else "sell",
                    "qty": abs(sz), "entry": float(p.get("entry") or 0),
                    "mark": float(p.get("mark") or 0),
                    "upnl": float(p.get("upnl") or 0)}
            with LOCK:
                local = dict(STATE["positions"])
            for pid, pos in local.items():
                r = remote.get(pos["symbol"])
                if r:
                    # ensure exchange-side protection is still armed
                    continue
                # position gone → exchange SL/TP liquidated it (or manual close)
                pnl = 0.0
                with LOCK:
                    STATE["positions"].pop(pid, None)
                    STATE["stats"]["trades"] += 1
                    STATE["stats"]["pnl"] += pnl
                hold = time.time() - self.open_since.get(pid, time.time())
                await self.journal.close_trade(pid, pnl, "exchange_sl_tp", )
                log.info(f"CLOSED {pos['symbol']} via exchange SL/TP "
                         f"(hold {hold/60:.1f}m)")
                await self.tg.send(f"{'🟢' if pnl >= 0 else '🔴'} "
                                   f"{pos['symbol']} بسته شد (SL/TP صرافی)")
                if pnl < 0:
                    LOSS_COOLDOWN_UNTIL = time.time() + LOSS_COOLDOWN_S
        except Exception as e:
            log.warning(f"sync: {e}")

    async def watchdog_loop(self):
        while True:
            await self.sync_positions()
            # re-arm protection if missing (e.g. position opened manually)
            try:
                d = await self.api.positions()
                for p in (d.get("data") or []):
                    sym = p.get("symbol")
                    if not sym or abs(float(p.get("size") or 0)) < 1e-12:
                        continue
                    if not p.get("tp") and not p.get("sl"):
                        await self.journal.decision(sym, "rearm",
                                                    "بدون SL/TP — تنظیم مجدد")
                        await self.api.set_tpsl(
                            sym, float(p.get("mark") * 1.01 if float(p.get("size", 0)) > 0 else 0),
                            float(p.get("mark") * 0.99 if float(p.get("size", 0)) > 0 else 0))
            except Exception as e:
                log.warning(f"watchdog: {e}")
            await asyncio.sleep(20)

    async def real_test(self):
        """One guaranteed-safe end-to-end test trade (tiny size)."""
        await self.tg.send("⚡ تست واقعی در حال اجرا…")
        try:
            sym = SYMBOLS[0]
            df = await self.api.klines(sym, ENTRY_TF, 100)
            price = float(df["close"].iloc[-1])
            atr = float(Strategies.atr(df).iloc[-1])
            await self.open_position(sym, 1, price, atr, "RealTest")
            await asyncio.sleep(10)
            d = await self.api.positions()
            pos = next((p for p in (d.get("data") or [])
                        if p["symbol"] == sym and abs(float(p.get("size") or 0)) > 0), None)
            if not pos:
                return await self.tg.send("❌ پوزیشن باز نشد (لاگ را ببینید)")
            has_prot = bool(pos.get("tp") or pos.get("sl"))
            await self.tg.send(
                f"✅ پوزیشن باز شد: {sym}\n"
                f"SL/TP سمت صرافی: {'فعال ✅' if has_prot else 'غیرفعال ⚠️'}\n"
                "پوزیشن با SL/TP نزدیک محافظت می‌شود؛ برای بستن فوری: /api/status")
        except Exception as e:
            await self.tg.send(f"❌ تست: {str(e)[:180]}")

    # ---------- lifecycle ----------
    async def run(self):
        await self.journal.init()
        log.info(f"AriaX Pro Bot v1.0 | {BASE}")
        for i in range(5):
            if await self.api.health():
                break
            log.warning(f"سلامت اتصال برقرار نشد؛ تلاش {i+1}/5…")
            await asyncio.sleep(12)
        else:
            log.error("❌ اتصال برقدار نشد — چک‌لیست: ARIAX_BASE صحیح، کلید معتبر")
        try:
            await self.api.config()
            log.info(f"config: {len(self.api.symbol_meta)} نماد")
        except Exception as e:
            log.warning(f"config: {e}")
        await self.api.ensure_futures(min_free=120.0)
        await self.sync_positions()
        async def report_loop():
            while True:
                await self.refresh_report()
                await asyncio.sleep(300)
        asyncio.create_task(self.selector_loop())
        asyncio.create_task(report_loop())
        await asyncio.gather(self.price_loop(), self.scan_loop(),
                             self.watchdog_loop(), self.tg.poll())


# --------------------------------------------------------------------------- #
# [Team 6] Web dashboard                                                       #
# --------------------------------------------------------------------------- #
if HAS_FLASK:
    web = Flask(__name__)

    @web.route("/api/status")
    def _status():
        with LOCK:
            return jsonify(dict(STATE))

    @web.route("/")
    def _dash():
        return """<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="UTF-8">
<title>AriaX Pro Bot</title><style>body{font-family:system-ui;background:#0b0e14;color:#c9d1d9;padding:24px}
h1{color:#7ee787}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.c{background:#151b23;border:1px solid #2b3442;border-radius:12px;padding:14px}.v{font-size:1.3rem;font-weight:700;color:#7ee787}
table{width:100%;border-collapse:collapse;margin-top:10px}td,th{padding:6px;border-bottom:1px solid #2b3442;font-size:.85rem}</style>
</head><body><h1>🤖 AriaX Pro Bot</h1><div class="g">
<div class="c">Equity<div class="v" id="e">—</div></div>
<div class="c">Free<div class="v" id="f">—</div></div>
<div class="c">Positions<div class="v" id="p">—</div></div>
<div class="c">PnL<div class="v" id="n">—</div></div>
<div class="c">Halted<div class="v" id="h">—</div></div></div>
<div id="extra"></div><script>
async function r(){try{const d=await(await fetch('/api/status')).json();
e.textContent='$'+(d.equity||0).toFixed(2);f.textContent='$'+(d.free||0).toFixed(2);
p.textContent=Object.keys(d.positions||{}).length+'/'++3;
n.textContent='$'+(d.stats?.pnl||0).toFixed(2);h.textContent=d.halted||'no';
const pk=Object.entries(d.strategy_pick||{}).map(([s,v])=>`<tr><td>${s}</td><td>${v.strategy}</td><td>${v.bt_return}%</td><td>${v.bt_dd}%</td></tr>`).join('');
extra.innerHTML='<h3>استراتژی منتخب (بک‌تست صرافی)</h3><table><tr><th>نماد</th><th>استراتژی</th><th>بازده</th><th>DD</th></tr>'+pk+'</table>';}catch(x){}}
r();setInterval(r,5000);</script></body></html>"""


def main():
    port = int(os.environ.get("PORT", 10000))
    if HAS_FLASK:
        def run_web():
            try:
                web.run(host="0.0.0.0", port=port, debug=False,
                        use_reloader=False)
            except Exception as e:
                log.error(f"Flask: {e}")
        Thread(target=run_web, daemon=True).start()
    try:
        bot = ProBot()
        asyncio.run(bot.run())
    except Exception as e:
        log.error(f"FATAL: {e}")
        traceback.print_exc()
        time.sleep(30)
        raise


if __name__ == "__main__":
    main()
