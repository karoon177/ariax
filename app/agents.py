# -*- coding: utf-8 -*-
"""
AI agent team (v1 feature parity, now event-driven).

The agents are observability wrappers around real engine subsystems:
  oracle     -> marketdata.feeds (index/mark reference)
  mm         -> marketdata.mm (liquidity)
  risk       -> engine.risk (liquidations, funding notices)
  watch      -> fraud/anomaly flags from the OMS
  bot        -> per-user EMA cross auto-trader
  support    -> Persian knowledge-base chat bot
  thinktank  -> independent price witness (Coinbase)
  oversight  -> ops health monitor

All UI endpoints (/api/ai/*) keep their v1 response shapes.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from . import config, events, util
from .state import STATE
from .engine import orders

AGENTS: dict[str, dict] = {}


def _mk_agent(aid: str, name: str, role: str, icon: str) -> dict:
    return dict(id=aid, name=name, role=role, icon=icon, enabled=True,
                last=0.0, actions=0, logs=deque(maxlen=40))


def init_agents() -> None:
    AGENTS.update({
        "oracle": _mk_agent("oracle", "اوراکل بازار", "خوراک قیمت مرجع Kraken برای ۲۰ بازار (اسپات + دائمی)", "🔮"),
        "mm": _mk_agent("mm", "بازارگردان هوشمند", "نقل‌قیمت‌دهی دوطرفه و تأمین نقدینگی در دفتر سفارشات همه‌ی نمادها", "💧"),
        "risk": _mk_agent("risk", "مدیر ریسک", "پایش مارجین، لیکوئیدیشن خودکار و نرخ تأمین مالی", "🛡️"),
        "watch": _mk_agent("watch", "ناظر تقلب", "رصد سفارش‌های مشکوک، قیمت‌های پرت و نهنگ‌های ناهنجار", "🚨"),
        "bot": _mk_agent("bot", "معامله‌گر خودکار", "ربات معامله‌گر حساب کاربر بر پایه‌ی تقاطع EMA (روند بازار)", "📈"),
        "support": _mk_agent("support", "پشتیبان هوشمند", "پاسخ‌گویی فارسی به سؤالات کاربران در چت زنده", "💬"),
        "thinktank": _mk_agent("thinktank", "اتاق فکر داده", "مقایسهٔ فیدهای مستقل، تحلیل اختلاف قیمت و پیشنهاد اقدام اصلاحی", "🧠"),
        "oversight": _mk_agent("oversight", "گروه ناظر عملیات", "کنترل سلامت سرویس، تازگی فید، پوشش نمادها و ثبت هشدارهای عملیاتی", "🔎"),
    })
    STATE.agents = AGENTS


def agent_log(aid: str, msg: str) -> None:
    a = AGENTS.get(aid)
    if not a:
        return
    a["logs"].appendleft(dict(t=round(time.time(), 1), msg=msg))
    a["last"] = time.time()
    a["actions"] += 1


def _wire_events() -> None:
    """Engine events -> agent dashboard logs."""
    events.BUS.on("agent_oracle", lambda p: agent_log("oracle", p["msg"]))
    events.BUS.on("agent_mm", lambda p: agent_log("mm", p["msg"]))
    events.BUS.on("agent_risk", lambda p: agent_log("risk", p["msg"]))
    events.BUS.on("agent_watch", lambda p: agent_log("watch", p["msg"]))
    events.BUS.on("agent_thinktank", lambda p: agent_log("thinktank", p["msg"]))
    events.BUS.on("agent_oversight", lambda p: agent_log("oversight", p["msg"]))


def agents_payload() -> dict:
    return [dict(id=a["id"], name=a["name"], role=a["role"], icon=a["icon"],
                 enabled=a["enabled"], actions=a["actions"], last=a["last"],
                 logs=list(a["logs"])[:12]) for a in AGENTS.values()]


def stats_payload() -> dict:
    return dict(
        uptime=round(time.time() - STATE.stats["start"]),
        orders=STATE.stats["orders"], fills=STATE.stats["fills"],
        liqs=STATE.stats["liqs"], flags=STATE.stats["flags"],
        chats=STATE.stats["chats"], users=STATE.stats["users"],
        ws=STATE.stats.get("ws_clients", 0),
        open_orders=len([o for o in STATE.open_orders.values() if o.uid > 0]),
        positions=len([p for p in STATE.positions.values() if p.size != 0]),
        insurance=round(STATE.insurance_pool, 2),
    )


# --------------------------------------------------------------------------- #
# Support chat (v1 knowledge base, extended with v2 topics)                    #
# --------------------------------------------------------------------------- #
CHAT_KB = [
    (["فیس", "کارمزد", "fee"], "کارمزد معاملات: فیوچرز میکر ۰٫۰۲٪ / تیکر ۰٫۰۵۵٪ و اسپات میکر ۰٫۰۲٪ / تیکر ۰٫۰۵٪. جریمه لیکوئیدیشن ۰٫۷۵٪ ارزش پوزیشن است."),
    (["فاست", "تست", "سرمایه آزمایشی", "پاداش"], "دریافت سرمایه تستی: هر ۲۴ ساعت یک‌بار ۱۰,۰۰۰ USDT از دکمه «💧 سرمایه تستی». پاداش ثبت‌نام هم ۲۰,۰۰۰ USDT است."),
    (["فاندینگ", "تأمین مالی", "funding"], "نرخ تأمین مالی هر ۸ ساعت (۰۰:۰۰/۰۸:۰۰/۱۶:۰۰ UTC) تسویه می‌شود؛ مثبت یعنی لانگ‌ها به شورت‌ها می‌پردازند. نرخ لحظه‌ای در نوار نماد نمایش داده می‌شود."),
    (["اهرم", "لوریج", "leverage"], "اهرم تا ۱۰۰x برای BTC و ۵۰x برای ETH و ۲۰x برای آلت‌کوین‌ها؛ محدود به سقف ریسک هر پله (Risk Limit)."),
    (["ریسک لیمیت", "risk limit", "پله"], "هر نماد پله‌های ریسک دارد: هرچه حجم پوزیشن بزرگ‌تر، حداکثر اهرم کمتر و مارجین نگهداری بیشتر. جدول کامل در instruments-info است."),
    (["لیکوئید", "liquidat", "کال مارجین"], "وقتی مارجین پوزیشن + سود/زیان شناور از مارجین نگهداری کمتر شود، پوزیشن به‌صورت خودکار در قیمت مارک بسته می‌شود. قیمت لیکوئید در جدول پوزیشن‌ها دیده می‌شود."),
    (["توقف", "حد ضرر", "stop loss", "tp", "sl", "حد سود"], "برای هر پوزیشن می‌توانید TP/SL تعیین کنید (دکمه TP/SL در جدول پوزیشن‌ها) یا هنگام ثبت سفارش وارد کنید. سفارش شرطی (Trigger) هم پشتیبانی می‌شود."),
    (["واریز", "deposit"], "کیف پول → واریز: دارایی و مبلغ را انتخاب کنید؛ واریز شبیه‌سازی می‌شود."),
    (["برداشت", "withdraw"], "کیف پول → برداشت: دارایی، مبلغ و آدرس را وارد کنید؛ در تست‌نت بلافاصله کسر می‌شود."),
    (["api", "کی", "کلید", "ربات", "بات"], "از دکمه «🔑 API ربات» کلید بسازید. احراز هویت به سبک Bybit v5 با امضای HMAC-SHA256 است؛ مستندات در /docs و API_REFERENCE.md."),
    (["ایجنت", "هوش مصنوعی", "ai", "مدیریت"], "این صرافی توسط ۸ ایجنت هوش مصنوعی اداره می‌شود: اوراکل، بازارگردان، مدیر ریسک، ناظر تقلب، معامله‌گر خودکار، پشتیبان، اتاق فکر و گروه ناظر."),
    (["websocket", "سوکت", "ws"], "WebSocket عمومی روی /v5/public/ws و خصوصی روی /v5/private/ws (پروتکل Bybit v5) و رابط کاربری روی /ws."),
    (["بک‌تست", "backtest", "آزمایش استراتژی"], "با POST /v5/backtest/run استراتژی‌های ema_cross، sma_cross، rsi، macd و grid روی داده‌های تاریخی Kraken آزمایش کنید."),
    (["سلام", "درود", "hi", "hello"], "سلام! 👋 من پشتیبان هوشمند آریاکس هستم. درباره کارمزدها، فاندینگ، اهرم، لیکوئیدیشن، API، WebSocket یا بک‌تست بپرسید."),
    (["قیمت", "بیتکوین", "btc"], "قیمت‌ها از مرجع زنده Kraken (اسپات و فیوچرز) دریافت می‌شوند و مبنای معامله، دفتر سفارشات زنده است."),
    (["اسپات", "spot", "نقدی"], "در اسپات دارایی واقعی مبادله می‌شود؛ در فیوچرز قرارداد دائمی با مارجین ایزوله و اهرم معامله می‌کنید."),
]


def chat_reply(msg: str) -> str:
    STATE.stats["chats"] += 1
    low = msg.lower()
    for keys, ans in CHAT_KB:
        if any(k in low for k in keys):
            return ans
    return "سؤال شما ثبت شد 🤝 می‌توانید درباره: کارمزد، فاندینگ، واریز/برداشت، فاست، اهرم، لیکوئیدیشن، API، WebSocket یا بک‌تست بپرسید."


# --------------------------------------------------------------------------- #
# Per-user auto-trading bot (v1 parity: EMA(12/40) on tick history)            #
# --------------------------------------------------------------------------- #
async def bot_loop() -> None:
    while True:
        try:
            if AGENTS.get("bot", {}).get("enabled", True):
                for uid, st in list(STATE.bots.items()):
                    if not st.get("active"):
                        continue
                    symbol = st.get("sym", "BTCUSD")
                    t = STATE.tick(symbol)
                    if len(t.tickhist) < 60:
                        continue
                    vals = list(t.tickhist)
                    f = util.ema(vals[-60:], 12)
                    s = util.ema(vals[-120:], 40)
                    sig = 1 if f > s * 1.0002 else (-1 if f < s * 0.9998 else 0)
                    if sig and sig != st.get("last_sig"):
                        st["last_sig"] = sig
                        pos = STATE.position(uid, symbol)
                        want_long = sig > 0
                        if pos and pos.size != 0 and (pos.size > 0) != want_long:
                            side = "Sell" if pos.size > 0 else "Buy"
                            try:
                                orders.place_order(uid, symbol, side, "Market",
                                                   abs(pos.size), leverage=pos.leverage)
                                agent_log("bot", f"Bot user #{uid}: closed {symbol} ({side})")
                            except Exception as e:
                                agent_log("bot", f"Bot user #{uid}: close failed: {e}")
                            pos = None
                        if not STATE.position(uid, symbol):
                            px = t.last
                            cfg = config.MARKETS[symbol]
                            qty = util.snap_to_step(
                                max(cfg.min_qty, 400 * st.get("lev", 5) / px),
                                cfg.qty_step)
                            side = "Buy" if want_long else "Sell"
                            try:
                                orders.place_order(uid, symbol, side, "Market", qty,
                                                   leverage=st.get("lev", 5))
                                agent_log("bot", f"Bot user #{uid}: EMA signal "
                                           f"{'long' if want_long else 'short'} -> "
                                           f"{side} {qty} {symbol}")
                                events.BUS.emit("bot_msg", {"uid": uid,
                                                            "msg": f"سیگنال {'صعودی' if want_long else 'نزولی'} — {side} {qty}"})
                            except Exception as e:
                                agent_log("bot", f"Bot user #{uid}: open failed: {e}")
        except Exception:
            import logging
            logging.getLogger("ariax.bot").exception("bot loop error")
        await asyncio.sleep(2.5)


def init() -> None:
    init_agents()
    _wire_events()
