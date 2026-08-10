#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
آریا‌اکس | AriaX Testnet Exchange
صرافی آزمایشی ارز دیجیتال — اداره‌شده توسط تیم ایجنت‌های هوش مصنوعی
بک‌اند: سرور HTTP + WebSocket با کتابخانه استاندارد پایتون (بدون وابستگی خارجی)
"""
import json, os, time, math, random, threading, hashlib, secrets, sqlite3, struct, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from collections import defaultdict, deque

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, 'data', 'exchange.db')
os.makedirs(os.path.join(ROOT, 'data'), exist_ok=True)
WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
PORT = int(os.environ.get('PORT', 8000))

TAKER_FEE = 0.0005
MAKER_FEE = 0.0002
MAINT_RATE = 0.005          # مارجین نگهداری
LIQ_FEE = 0.0075            # جریمه لیکوئیدیشن
ASSETS = ['USDT', 'BTC', 'ETH', 'SOL', 'XRP', 'DOGE']

# ------------------------------ بازارها ------------------------------
MARKETS = {
    'BTC/USDT':  dict(base='BTC',  kind='spot', price=115250.0, vol=0.0009, tick=0.1,    step=0.0001, minq=0.0005, qbase=0.35),
    'ETH/USDT':  dict(base='ETH',  kind='spot', price=4310.0,   vol=0.0011, tick=0.01,   step=0.001,  minq=0.005,  qbase=4.0),
    'SOL/USDT':  dict(base='SOL',  kind='spot', price=186.4,    vol=0.0016, tick=0.01,   step=0.01,   minq=0.05,   qbase=45.0),
    'XRP/USDT':  dict(base='XRP',  kind='spot', price=2.24,     vol=0.0014, tick=0.0001, step=0.1,    minq=1.0,    qbase=900.0),
    'DOGE/USDT': dict(base='DOGE', kind='spot', price=0.238,    vol=0.0018, tick=0.00001,step=1.0,    minq=10.0,   qbase=12000.0),
    'BTCUSD':    dict(base='BTC',  kind='perp', price=115265.0, vol=0.0009, tick=0.1,    step=0.0001, minq=0.0005, qbase=0.35, maxlev=100),
    'ETHUSD':    dict(base='ETH',  kind='perp', price=4311.2,   vol=0.0011, tick=0.01,   step=0.001,  minq=0.005,  qbase=4.0,  maxlev=50),
    'SOLUSD':    dict(base='SOL',  kind='perp', price=186.45,   vol=0.0016, tick=0.01,   step=0.01,   minq=0.05,   qbase=45.0, maxlev=20),
    'XRPUSD':    dict(base='XRP', kind='perp', price=1.02, vol=.002, tick=.0001, step=1, minq=1, qbase=800, maxlev=20),
    'DOGEUSD':   dict(base='DOGE',kind='perp', price=.07, vol=.002, tick=.00001,step=1, minq=10,qbase=8000,maxlev=20),
    'ADAUSD':    dict(base='ADA', kind='perp', price=.19, vol=.002, tick=.0001, step=1, minq=10,qbase=4000,maxlev=20),
    'AVAXUSD':   dict(base='AVAX',kind='perp', price=6.45, vol=.002, tick=.001, step=.01,minq=.1,qbase=80,maxlev=20),
    'LINKUSD':   dict(base='LINK',kind='perp', price=8.27, vol=.002, tick=.001, step=.01,minq=.1,qbase=60,maxlev=20),
    'DOTUSD':    dict(base='DOT', kind='perp', price=.81, vol=.002, tick=.0001,step=.1,minq=1,qbase=700,maxlev=20),
    'LTCUSD':    dict(base='LTC', kind='perp', price=45.1, vol=.002, tick=.01, step=.01,minq=.01,qbase=12,maxlev=20),
    'BCHUSD':    dict(base='BCH', kind='perp', price=212.9,vol=.002,tick=.01, step=.001,minq=.001,qbase=3,maxlev=20),
    'TRXUSD':    dict(base='TRX', kind='perp', price=.331,vol=.002,tick=.00001,step=1,minq=10,qbase=3000,maxlev=20),
    'XLMUSD':    dict(base='XLM', kind='perp', price=.163,vol=.002,tick=.0001,step=1,minq=10,qbase=2000,maxlev=20),
    'AAVEUSD':   dict(base='AAVE',kind='perp', price=89.8, vol=.002, tick=.01, step=.001,minq=.001,qbase=4,maxlev=20),
    'UNIUSD':    dict(base='UNI', kind='perp', price=3.96, vol=.002, tick=.001,step=.01,minq=.01,qbase=70,maxlev=20),
}

PERP_UNDERLYING = {'BTCUSD': 'BTC/USDT', 'ETHUSD': 'ETH/USDT', 'SOLUSD': 'SOL/USDT'}
# قیمت مرجع زنده: Kraken (جفت‌های USDT). در صورت اختلال، قیمت آخر حفظ و کهنگی گزارش می‌شود؛
# هرگز برای بازار زنده قیمت تصادفی جایگزین نمی‌شود.
REFERENCE = dict(source='Kraken', status='starting', updated=0.0, error='', prices={})
KRAKEN_PAIRS = {'BTC/USDT':'XBTUSDT', 'ETH/USDT':'ETHUSDT', 'SOL/USDT':'SOLUSDT',
                'XRP/USDT':'XRPUSDT', 'DOGE/USDT':'XDGUSDT'}
# قراردادهای دائمی از بازار Futures همان مرجع دریافت می‌شوند (mark price).
KRAKEN_FUTURES = {'BTCUSD':'PF_XBTUSD', 'ETHUSD':'PF_ETHUSD', 'SOLUSD':'PF_SOLUSD',
                  'XRPUSD':'PF_XRPUSD', 'DOGEUSD':'PF_DOGEUSD', 'ADAUSD':'PF_ADAUSD', 'AVAXUSD':'PF_AVAXUSD',
                  'LINKUSD':'PF_LINKUSD', 'DOTUSD':'PF_DOTUSD', 'LTCUSD':'PF_LTCUSD', 'BCHUSD':'PF_BCHUSD',
                  'TRXUSD':'PF_TRXUSD', 'XLMUSD':'PF_XLMUSD', 'AAVEUSD':'PF_AAVEUSD', 'UNIUSD':'PF_UNIUSD'}
REF_LOCK = threading.Lock()
OHLC_CACHE = {}
OHLC_LOCK = threading.Lock()

def rstep(v, step):
    return round(round(v / step) * step, 10)
def rtick(v, tick):
    return round(round(v / tick) * tick, 12)

# ------------------------------ وضعیت بازار ------------------------------
class Market:
    def __init__(self, sym, cfg):
        self.sym, self.cfg = sym, cfg
        self.price = cfg['price']
        self.open24 = cfg['price'] * random.uniform(0.965, 1.035)
        self.high24, self.low24 = self.price, self.price
        self.vbase24, self.vquote24 = 0.0, 0.0
        self.bids = defaultdict(deque)   # price -> deque[order]
        self.asks = defaultdict(deque)
        self.trades = deque(maxlen=250)
        self.candles1m = deque(maxlen=1600)
        self.cur = None
        self.tickhist = deque(maxlen=600)
        self.seed_history()

    def seed_history(self):
        p = self.price * random.uniform(0.985, 1.015)
        t0 = int(time.time() // 60) * 60 - 300 * 60
        for i in range(300):
            o = p
            hi, lo, v = p, p, 0.0
            for _ in range(12):
                p = p * math.exp(random.gauss(0, self.cfg['vol']))
                hi, lo = max(hi, p), min(lo, p)
                v += math.exp(random.gauss(math.log(self.cfg['qbase']), 0.8))
            c = [t0 + i * 60, o, hi, lo, p, v]
            self.candles1m.append(c)
        self.price = p
        self.high24 = max(x[2] for x in list(self.candles1m)[-1440:])
        self.low24 = min(x[3] for x in list(self.candles1m)[-1440:])
        self.open24 = list(self.candles1m)[-1440][1] if len(self.candles1m) >= 1440 else self.candles1m[0][1]
        minute = int(time.time() // 60) * 60
        self.cur = [minute, self.price, self.price, self.price, self.price, 0.0]

MARKET_STATE = {}
for _s, _c in MARKETS.items():
    if _c['kind'] == 'perp' and _s in PERP_UNDERLYING:
        _c = dict(_c)
        _c['price'] = MARKET_STATE[PERP_UNDERLYING[_s]].price * random.uniform(0.9995, 1.0005)
    MARKET_STATE[_s] = Market(_s, _c)

# ------------------------------ ایجنت‌های هوش مصنوعی ------------------------------
def _mk_agent(id, name, role, icon):
    return dict(id=id, name=name, role=role, icon=icon, enabled=True, last=0.0,
                actions=0, logs=deque(maxlen=40))

AGENTS = {
    'oracle':  _mk_agent('oracle',  'اوراکل بازار',      'شبیه‌سازی قیمت لحظه‌ای و خوراک داده‌ی بازارها (GBM + رژیم نوسان)', '🔮'),
    'mm':      _mk_agent('mm',      'بازارگردان هوشمند', 'نقل‌قیمت‌دهی دوطرفه و تأمین نقدینگی در دفتر سفارشات همه‌ی نمادها', '💧'),
    'risk':    _mk_agent('risk',    'مدیر ریسک',         'پایش لحظه‌ای مارجین پوزیشن‌ها و اجرای لیکوئیدیشن خودکار', '🛡️'),
    'watch':   _mk_agent('watch',   'ناظر تقلب',         'رصد سفارش‌های مشکوک، قیمت‌های پرت و نهنگ‌های ناهنجار', '🚨'),
    'bot':     _mk_agent('bot',     'معامله‌گر خودکار',  'ربات معامله‌گر حساب کاربر بر پایه‌ی تقاطع EMA (روند بازار)', '📈'),
    'support': _mk_agent('support', 'پشتیبان هوشمند',    'پاسخ‌گویی فارسی به سؤالات کاربران در چت زنده', '💬'),
    'thinktank': _mk_agent('thinktank', 'اتاق فکر داده', 'مقایسهٔ فیدهای مستقل، تحلیل اختلاف قیمت و پیشنهاد اقدام اصلاحی', '🧠'),
    'oversight': _mk_agent('oversight', 'گروه ناظر عملیات', 'کنترل سلامت سرویس، تازگی فید، پوشش نمادها و ثبت هشدارهای عملیاتی', '🔎'),
}

def agent_log(aid, msg):
    a = AGENTS[aid]
    a['logs'].appendleft(dict(t=round(time.time(), 1), msg=msg))
    a['last'] = time.time()
    a['actions'] += 1

STATS = dict(start=time.time(), orders=0, fills=0, liqs=0, flags=0, chats=0, users=1)

# ------------------------------ دیتابیس و حساب‌ها ------------------------------
_db_lock = threading.Lock()
def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with _db_lock, db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, name TEXT, pass TEXT, salt TEXT, created REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS balances(uid INTEGER, asset TEXT, amount REAL, PRIMARY KEY(uid, asset))')
        c.execute('CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, type TEXT, asset TEXT, amount REAL, note TEXT, ts REAL)')

def hash_pw(pw, salt):
    return hashlib.sha256((salt + pw).encode()).hexdigest()

def create_user(email, name, pw):
    salt = secrets.token_hex(8)
    with _db_lock, db() as c:
        cur = c.execute('INSERT INTO users(email,name,pass,salt,created) VALUES(?,?,?,?,?)',
                        (email.lower(), name, hash_pw(pw, salt), salt, time.time()))
        uid = cur.lastrowid
        c.execute('INSERT OR REPLACE INTO balances VALUES(?,?,?)', (uid, 'USDT', 20000.0))
        c.execute('INSERT INTO ledger(uid,type,asset,amount,note,ts) VALUES(?,?,?,?,?,?)',
                  (uid, 'bonus', 'USDT', 20000.0, 'پاداش ثبت‌نام در تست‌نت', time.time()))
    return uid

def auth_user(email, pw):
    with _db_lock, db() as c:
        r = c.execute('SELECT id,pass,salt,name,email FROM users WHERE email=?', (email.lower(),)).fetchone()
    if not r:
        return None
    return r[0] if hash_pw(pw, r[2]) == r[1] else None

BAL, LOCKS, POSLOCKS = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float)), defaultdict(float)
POS = {}          # (uid,sym) -> dict(size,entry,lev,margin)
OPEN_ORDERS = {}  # id -> order
ORDER_SEQ = [1]
SESSIONS = {}     # token -> uid
FAUCET_TS = {}

def load_bal(uid):
    with _db_lock, db() as c:
        for a, v in c.execute('SELECT asset,amount FROM balances WHERE uid=?', (uid,)):
            BAL[uid][a] = v

def save_bal(uid):
    with _db_lock, db() as c:
        for a, v in BAL[uid].items():
            c.execute('INSERT OR REPLACE INTO balances VALUES(?,?,?)', (uid, a, v))

def ledger(uid, typ, asset, amount, note):
    with _db_lock, db() as c:
        c.execute('INSERT INTO ledger(uid,type,asset,amount,note,ts) VALUES(?,?,?,?,?,?)',
                  (uid, typ, asset, amount, note, time.time()))

def available(uid, asset='USDT'):
    return BAL[uid].get(asset, 0.0) - LOCKS[uid].get(asset, 0.0)

def margin_used(uid):
    return sum(p['margin'] for (u, s), p in POS.items() if u == uid) + LOCKS[uid].get('MARGIN', 0.0)

def free_margin(uid):
    return BAL[uid].get('USDT', 0.0) - margin_used(uid)

def lock(uid, asset, amt): LOCKS[uid][asset] = LOCKS[uid].get(asset, 0.0) + amt
def unlock(uid, asset, amt):
    LOCKS[uid][asset] = max(0.0, LOCKS[uid].get(asset, 0.0) - amt)

# ------------------------------ هاب WebSocket ------------------------------
class Hub:
    def __init__(self):
        self.clients = []
        self.lk = threading.Lock()
    def add(self, c):
        with self.lk: self.clients.append(c)
    def drop(self, c):
        with self.lk:
            if c in self.clients: self.clients.remove(c)
    def publish(self, ch, data):
        msg = json.dumps(dict(ch=ch, data=data), ensure_ascii=False)
        with self.lk: cl = list(self.clients)
        for c in cl:
            if c.subs and not any(c.match(s) for s in c.subs):
                continue
            if ch == 'user' and c.uid is None:
                continue
            c.send(msg)
    def count(self):
        with self.lk: return len(self.clients)

HUB = Hub()

class WSClient:
    def __init__(self, conn):
        self.conn = conn
        self.wlock = threading.Lock()
        self.subs = set()
        self.uid = None
        self.last_pong = time.time()
    def match(self, s):
        return True  # فیلتر دقیق در publish ساده‌سازی شد؛ همه‌ی کانال‌ها ارسال و سمت کلاینت فیلتر می‌شوند
    def send(self, text):
        try:
            data = text.encode()
            n = len(data)
            if n < 126: hdr = struct.pack('!BB', 0x81, n)
            elif n < 65536: hdr = struct.pack('!BBH', 0x81, 126, n)
            else: hdr = struct.pack('!BBQ', 0x81, 127, n)
            with self.wlock:
                self.conn.sendall(hdr + data)
        except Exception:
            HUB.drop(self)

def ws_frames(conn, rfile):
    """جنریتور فریم‌های ورودی (opcode, payload)"""
    while True:
        h = rfile.read(2)
        if len(h) < 2: return
        b1, b2 = h[0], h[1]
        op = b1 & 0x0F
        ln = b2 & 0x7F
        if ln == 126: ln = struct.unpack('!H', rfile.read(2))[0]
        elif ln == 127: ln = struct.unpack('!Q', rfile.read(8))[0]
        mask = b2 & 0x80
        mk = rfile.read(4) if mask else None
        pl = rfile.read(ln) if ln else b''
        if mask and pl:
            pl = bytes(x ^ mk[i % 4] for i, x in enumerate(pl))
        yield op, pl

def send_ws(conn, opcode, payload=b''):
    n = len(payload)
    if n < 126: hdr = struct.pack('!BB', 0x80 | opcode, n)
    elif n < 65536: hdr = struct.pack('!BBH', 0x80 | opcode, 126, n)
    else: hdr = struct.pack('!BBQ', 0x80 | opcode, 127, n)
    conn.sendall(hdr + payload)

# ------------------------------ موتور معاملات ------------------------------
def user_event(uid, payload):
    HUB.publish('user', dict(uid=uid, **payload))

def notify_fill(m, taker, maker, px, q):
    t = [round(time.time(), 2), taker['side'], px, q]
    m.trades.appendleft(t)
    m.vbase24 += q; m.vquote24 += px * q
    STATS['fills'] += 1
    HUB.publish(f"trades:{m.sym}", [t])

def settle_spot_fill(uid, m, side, px, q, fee_rate):
    bal = BAL[uid]; base = m.cfg['base']
    cost, fee = px * q, px * q * fee_rate
    if side == 'buy':
        bal['USDT'] = bal.get('USDT', 0) - cost - fee
        bal[base] = bal.get(base, 0) + q
    else:
        bal[base] = bal.get(base, 0) - q
        bal['USDT'] = bal.get('USDT', 0) + cost - fee
    save_bal(uid)
    user_event(uid, dict(type='fill', symbol=m.sym, side=side, price=px, qty=q, fee=round(fee, 6)))
    user_event(uid, dict(type='wallet'))

def perp_fill(uid, m, side, px, q, fee_rate, lev=10):
    bal = BAL[uid]
    fee = px * q * fee_rate
    bal['USDT'] = bal.get('USDT', 0) - fee
    signed = q if side == 'buy' else -q
    key = (uid, m.sym)
    pos = POS.get(key)
    rem = signed
    # بستن پوزیشن مخالف
    if pos and pos['size'] != 0 and (pos['size'] > 0) != (signed > 0):
        closeq = min(abs(pos['size']), abs(rem))
        pnl = (px - pos['entry']) * closeq * (1 if pos['size'] > 0 else -1)
        released = pos['margin'] * (closeq / abs(pos['size']))
        pos['size'] += -closeq if pos['size'] > 0 else closeq
        pos['margin'] -= released
        bal['USDT'] += pnl + released
        rem += closeq if signed > 0 else -closeq
        user_event(uid, dict(type='pnl', symbol=m.sym, pnl=round(pnl, 4)))
        if abs(pos['size']) < 1e-12:
            POS.pop(key, None); pos = None
    # بازکردن / افزودن
    if abs(rem) > 1e-12:
        if not pos:
            pos = POS[key] = dict(size=0.0, entry=0.0, lev=lev, margin=0.0)
        newsize = pos['size'] + rem
        pos['entry'] = (pos['entry'] * abs(pos['size']) + px * abs(rem)) / abs(newsize)
        pos['size'] = newsize
        pos['lev'] = lev
        pos['margin'] += px * abs(rem) / lev
    save_bal(uid)
    user_event(uid, dict(type='fill', symbol=m.sym, side=side, price=px, qty=q, fee=round(fee, 6)))
    user_event(uid, dict(type='wallet'))
    user_event(uid, dict(type='position', symbol=m.sym))

def exec_fill(m, taker, maker, px, q):
    notify_fill(m, taker, maker, px, q)
    for o, is_taker in ((taker, True), (maker, False)):
        uid = o['uid']
        if uid <= 0: continue
        rate = TAKER_FEE if is_taker else MAKER_FEE
        if m.cfg['kind'] == 'spot':
            settle_spot_fill(uid, m, o['side'], px, q, rate)
            # آزادسازی قفل سفارش لیمیت
            if o['side'] == 'buy':
                unlock(uid, 'USDT', o['price'] * q * (1 + TAKER_FEE))
            else:
                unlock(uid, o['cfg_base'], q)
        else:
            perp_fill(uid, m, o['side'], px, q, rate, o.get('lev', 10))
            if o.get('intent') == 'open':
                unlock(uid, 'MARGIN', o['price'] * q / o.get('lev', 10) * 1.05)
            else:
                POSLOCKS[(uid, m.sym)] = max(0.0, POSLOCKS.get((uid, m.sym), 0.0) - q)

def match_order(m, o):
    """مچ سفارش ورودی با دفتر سفارشات"""
    opp = m.asks if o['side'] == 'buy' else m.bids
    while o['rem'] > 1e-12:
        levels = sorted((p for p, dq in opp.items() if dq), reverse=(o['side'] == 'sell'))
        if not levels: break
        bp = levels[0]
        if o['type'] == 'limit':
            if o['side'] == 'buy' and bp > o['price'] + 1e-12: break
            if o['side'] == 'sell' and bp < o['price'] - 1e-12: break
        dq = opp[bp]
        while dq and o['rem'] > 1e-12:
            mk = dq[0]
            q = min(o['rem'], mk['rem'])
            exec_fill(m, o, mk, bp, q)
            o['rem'] -= q; mk['rem'] -= q
            if mk['rem'] <= 1e-12: dq.popleft()
            for x in (o, mk):
                if x['uid'] > 0 and x['rem'] <= 1e-12 and x['id'] in OPEN_ORDERS:
                    OPEN_ORDERS.pop(x['id'], None)
        if not dq: opp.pop(bp, None)

def new_order(o):
    m = MARKET_STATE[o['sym']]
    book = m.bids if o['side'] == 'buy' else m.asks
    o['rem'] = o['qty']
    ORDER_SEQ[0] += 1
    o['id'] = ORDER_SEQ[0]
    o['ts'] = time.time()
    o['cfg_base'] = m.cfg['base']
    match_order(m, o)
    if o['rem'] > 1e-12 and o['type'] == 'limit':
        o['qty'] = o['rem']
        book[o['price']].append(o)
        if o['uid'] > 0:
            OPEN_ORDERS[o['id']] = o
            user_event(o['uid'], dict(type='order', action='new', order=order_json(o)))
    elif o['rem'] > 1e-12:
        # باقیمانده‌ی سفارش مارکت کنسل و قفل آزاد می‌شود
        refund_locks(o, o['rem'])
        if o['uid'] > 0:
            user_event(o['uid'], dict(type='order', action='partial_cancel', id=o['id']))
    elif o['uid'] > 0:
        user_event(o['uid'], dict(type='order', action='filled', id=o['id']))
    STATS['orders'] += 1
    return o

def refund_locks(o, qty_left):
    uid = o['uid']
    if uid <= 0: return
    m = MARKET_STATE[o['sym']]
    if m.cfg['kind'] == 'spot':
        if o['side'] == 'buy':
            unlock(uid, 'USDT', o['price'] * qty_left * (1 + TAKER_FEE))
        else:
            unlock(uid, m.cfg['base'], qty_left)
    else:
        if o.get('intent') == 'open':
            unlock(uid, 'MARGIN', o['price'] * qty_left / o.get('lev', 10) * 1.05)
        else:
            POSLOCKS[(uid, m.sym)] = max(0.0, POSLOCKS.get((uid, m.sym), 0.0) - qty_left)

def order_json(o):
    return dict(id=o['id'], symbol=o['sym'], side=o['side'], type=o.get('mtype', o['type']),
                price=o['price'], qty=o['qty'], rem=round(o.get('rem', o['qty']), 10),
                ts=o['ts'], lev=o.get('lev'))

def cancel_order(uid, oid):
    o = OPEN_ORDERS.get(oid)
    if not o or o['uid'] != uid:
        return False, 'سفارش یافت نشد'
    m = MARKET_STATE[o['sym']]
    book = m.bids if o['side'] == 'buy' else m.asks
    dq = book.get(o['price'])
    if dq and o in dq: dq.remove(o)
    refund_locks(o, o.get('rem', o['qty']))
    OPEN_ORDERS.pop(oid, None)
    user_event(uid, dict(type='order', action='cancelled', id=oid))
    return True, 'انجام شد'

# ------------------------------ اعتبارسنجی و ثبت سفارش کاربر ------------------------------
def place_user_order(uid, sym, side, typ, price, qty, lev=10):
    if sym not in MARKETS: return dict(ok=False, error='نماد نامعتبر است')
    if side not in ('buy', 'sell') or typ not in ('limit', 'market'):
        return dict(ok=False, error='پارامترهای سفارش نامعتبر است')
    cfg, m = MARKETS[sym], MARKET_STATE[sym]
    try: qty = float(qty)
    except Exception: return dict(ok=False, error='مقدار نامعتبر است')
    if qty < cfg['minq']:
        return dict(ok=False, error=f'حداقل مقدار سفارش {cfg["minq"]} است')
    qty = rstep(qty, cfg['step'])
    if typ == 'limit':
        try: price = float(price)
        except Exception: return dict(ok=False, error='قیمت نامعتبر است')
        if price <= 0: return dict(ok=False, error='قیمت نامعتبر است')
        price = rtick(price, cfg['tick'])
        dev = abs(price - m.price) / m.price
        if dev > 0.20:
            return dict(ok=False, error='قیمت بیش از ۲۰٪ با قیمت بازار فاصله دارد')
        if dev > 0.05:
            STATS['flags'] += 1
            agent_log('watch', f'سفارش مشکوک: {side} {qty} {sym} در قیمت {price} (انحراف {dev:.1%}) — علامت‌گذاری شد')
    else:
        ref = best_price(m, 'ask' if side == 'buy' else 'bid') or m.price
        price = ref * (1.03 if side == 'buy' else 0.97)
    notional = price * qty
    if notional > 2_000_000:
        STATS['flags'] += 1
        agent_log('watch', f'نهنگ شناسایی شد: سفارش {notional:,.0f} دلاری روی {sym} — زیر نظر گرفته شد')

    if cfg['kind'] == 'spot':
        if side == 'buy':
            need = price * qty * (1 + TAKER_FEE)
            if available(uid, 'USDT') < need:
                return dict(ok=False, error='موجودی USDT کافی نیست')
            lock(uid, 'USDT', need)
        else:
            if available(uid, cfg['base']) < qty:
                return dict(ok=False, error=f'موجودی {cfg["base"]} کافی نیست')
            lock(uid, cfg['base'], qty)
        o = dict(uid=uid, sym=sym, side=side, type='limit', mtype=typ, price=price, qty=qty)
        new_order(o)
    else:
        lev = max(1, min(int(lev), cfg.get('maxlev', 20)))
        pos = POS.get((uid, sym))
        opening = not pos or pos['size'] == 0 or (pos['size'] > 0) == (side == 'buy')
        if opening:
            mest = price * qty / lev * 1.05
            if free_margin(uid) < mest + notional * TAKER_FEE:
                return dict(ok=False, error='مارجین آزاد کافی نیست')
            lock(uid, 'MARGIN', mest)
            o = dict(uid=uid, sym=sym, side=side, type='limit', mtype=typ, price=price, qty=qty, lev=lev, intent='open')
        else:
            avail_sz = abs(pos['size']) - POSLOCKS.get((uid, sym), 0.0)
            if qty > avail_sz + 1e-12:
                return dict(ok=False, error='مقدار از اندازه‌ی پوزیشن باز بیشتر است')
            POSLOCKS[(uid, sym)] = POSLOCKS.get((uid, sym), 0.0) + qty
            o = dict(uid=uid, sym=sym, side=side, type='limit', mtype=typ, price=price, qty=qty, lev=pos['lev'], intent='close')
        new_order(o)
    return dict(ok=True, id=o['id'])

def best_price(m, which):
    book = m.asks if which == 'ask' else m.bids
    ps = [p for p, dq in book.items() if dq]
    if not ps: return None
    return min(ps) if which == 'ask' else max(ps)

def book_snapshot(m, depth=15):
    def agg(book, reverse):
        lv = defaultdict(float)
        for p, dq in book.items():
            s = sum(x['rem'] for x in dq)
            if s > 0: lv[p] += s
        items = sorted(lv.items(), key=lambda kv: kv[0], reverse=reverse)[:depth]
        return [[round(p, 12), round(q, 10)] for p, q in items]
    return dict(bids=agg(m.bids, True), asks=agg(m.asks, False), ts=time.time(), last=m.price)

def ticker_json(m):
    chg = (m.price / m.open24 - 1) * 100 if m.open24 else 0
    return dict(last=round(m.price, 12), chg=round(chg, 2), high=m.high24, low=m.low24,
                vol=round(m.vquote24, 0), kind=m.cfg['kind'])

def kraken_candles(sym, interval):
    """OHLC واقعی Kraken با cache کوتاه؛ ساختار خروجی با نمودار داخلی یکسان است."""
    spot = PERP_UNDERLYING.get(sym, sym)
    pair = KRAKEN_PAIRS.get(spot)
    n = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240}.get(interval, 1)
    key = (spot, n); now = time.time()
    with OHLC_LOCK:
        cached = OHLC_CACHE.get(key)
        if cached and now - cached[0] < 8: return cached[1]
    try:
        url = f'https://api.kraken.com/0/public/OHLC?pair={pair}&interval={n}'
        with urlopen(url, timeout=7) as r: payload = json.loads(r.read().decode('utf-8'))
        if payload.get('error'): raise RuntimeError(str(payload['error']))
        rows = next(v for k,v in payload['result'].items() if k != 'last')
        data = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6])] for x in rows[-500:]]
        with OHLC_LOCK: OHLC_CACHE[key] = (now, data)
        return data
    except Exception:
        return None

def agg_candles(m, interval):
    n = {'1m': 1, '5m': 5, '15m': 15, '1h': 60, '4h': 240}.get(interval, 1)
    src = list(m.candles1m) + ([m.cur] if m.cur else [])
    if n == 1:
        return [list(map(lambda x: round(x, 12) if isinstance(x, float) else x, c)) for c in src[-500:]]
    out = []
    for c in src:
        b = int(c[0] // (n * 60)) * (n * 60)
        if out and out[-1][0] == b:
            o = out[-1]
            o[2] = max(o[2], c[2]); o[3] = min(o[3], c[3]); o[4] = c[4]; o[5] += c[5]
        else:
            out.append([b, c[1], c[2], c[3], c[4], c[5]])
    return out[-500:]

# ------------------------------ حلقه‌های ایجنت‌ها ------------------------------
def reference_feed_loop():
    """خوراک عمومی Kraken؛ آخرین معامله هر ۲ ثانیه و گزارش صریح وضعیت منبع."""
    endpoint = 'https://api.kraken.com/0/public/Ticker?pair=' + ','.join(KRAKEN_PAIRS.values())
    while True:
        try:
            with urlopen(endpoint, timeout=6) as r:
                payload = json.loads(r.read().decode('utf-8'))
            if payload.get('error'): raise RuntimeError(str(payload['error']))
            result = payload.get('result', {})
            prices = {}
            for symbol, pair in KRAKEN_PAIRS.items():
                # Kraken نام BTC/DOGE را XBT/XDG برمی‌گرداند.
                row = result.get(pair) or result.get('XBTUSDT' if pair == 'XBTUSDT' else 'XDGUSDT' if pair == 'XDGUSDT' else pair)
                if not row: raise RuntimeError('missing ' + pair)
                prices[symbol] = float(row['c'][0])
            # قیمت mark قراردادهای دائمی از Kraken Futures؛ مستقل از قیمت اسپات.
            with urlopen('https://futures.kraken.com/derivatives/api/v3/tickers', timeout=6) as r:
                futures_payload = json.loads(r.read().decode('utf-8'))
            frows = {x.get('symbol'): x for x in futures_payload.get('tickers', [])}
            for symbol, future_symbol in KRAKEN_FUTURES.items():
                row = frows.get(future_symbol)
                if not row or not row.get('markPrice'): raise RuntimeError('missing future ' + future_symbol)
                prices[symbol] = float(row['markPrice'])
            with REF_LOCK:
                REFERENCE.update(source='Kraken Spot + Kraken Futures', status='live', updated=time.time(), error='', prices=prices)
            agent_log('oracle', f'خوراک مرجع Kraken همگام شد — BTC: {prices["BTC/USDT"]:,.1f}')
        except Exception as e:
            with REF_LOCK:
                REFERENCE['status'] = 'stale'; REFERENCE['error'] = str(e)[:140]
            agent_log('watch', 'هشدار: خوراک قیمت مرجع در دسترس نیست؛ آخرین قیمت حفظ شد')
        time.sleep(2)

def oracle_loop():
    """ایجنت اوراکل: همگام‌سازی بازار با خوراک مرجع."""
    while True:
        with REF_LOCK:
            ref_prices, ref_status = dict(REFERENCE['prices']), REFERENCE['status']
        for sym, m in MARKET_STATE.items():
            cfg = m.cfg
            if AGENTS['oracle']['enabled']:
                if cfg['kind'] == 'perp':
                    # قرارداد دائمی: mark price واقعی از بازار Futures مرجع.
                    if sym in ref_prices:
                        m.price = ref_prices[sym]
                else:
                    # قیمت اسپات فقط از خوراک مرجع دریافت می‌شود، نه شبیه‌سازی.
                    if sym in ref_prices:
                        m.price = ref_prices[sym]
            if random.random() < 0.85:
                for _ in range(random.randint(1, 2)):
                    side = 'buy' if random.random() < 0.5 else 'sell'
                    q = rstep(math.exp(random.gauss(math.log(cfg['qbase']), 0.9)), cfg['step']) or cfg['step']
                    px = rtick(m.price * random.uniform(0.9998, 1.0002), cfg['tick'])
                    t = [round(time.time(), 2), side, px, q]
                    m.trades.appendleft(t)
                    m.vbase24 += q; m.vquote24 += px * q
                    if m.cur: m.cur[5] += q
            m.high24 = max(m.high24, m.price); m.low24 = min(m.low24, m.price)
            m.tickhist.append(m.price)
            minute = int(time.time() // 60) * 60
            if not m.cur or m.cur[0] != minute:
                if m.cur: m.candles1m.append(m.cur)
                m.cur = [minute, m.price, m.price, m.price, m.price, 0.0]
            else:
                m.cur[2] = max(m.cur[2], m.price); m.cur[3] = min(m.cur[3], m.price); m.cur[4] = m.price
        HUB.publish('tickers', {s: ticker_json(m) for s, m in MARKET_STATE.items()})
        for sym, m in MARKET_STATE.items():
            HUB.publish(f"candle:{sym}", m.cur)
        if AGENTS['oracle']['enabled'] and int(time.time()) % 30 == 0:
            agent_log('oracle', f'به‌روزرسانی قیمت {len(MARKET_STATE)} بازار — BTC: {MARKET_STATE["BTC/USDT"].price:,.1f}')
        time.sleep(0.5)

def thinktank_loop():
    """اتاق فکر: قیمت BTC مرجع را با Coinbase به‌عنوان شاهد مستقل مقایسه می‌کند."""
    while True:
        if AGENTS['thinktank']['enabled']:
            try:
                with urlopen('https://api.coinbase.com/v2/prices/BTC-USD/spot', timeout=6) as r:
                    cb = float(json.loads(r.read().decode())['data']['amount'])
                with REF_LOCK: kr = REFERENCE['prices'].get('BTC/USDT', 0)
                diff = abs(kr-cb) / cb * 100 if cb else 0
                msg = f'مقایسهٔ شاهد مستقل: Kraken={kr:,.2f} / Coinbase={cb:,.2f} / اختلاف={diff:.3f}%'
                agent_log('thinktank', msg)
                if diff > 1: agent_log('watch', 'هشدار اتاق فکر: اختلاف منابع BTC از ۱٪ بیشتر است')
            except Exception as e: agent_log('thinktank', 'مقایسهٔ مستقل ناموفق: ' + str(e)[:90])
        time.sleep(30)

def oversight_loop():
    """گروه ناظر: SLA فید و تمام نمادهای مرجع را بدون تغییر خودسرانه کنترل می‌کند."""
    while True:
        if AGENTS['oversight']['enabled']:
            with REF_LOCK:
                age = time.time() - REFERENCE['updated'] if REFERENCE['updated'] else 99999
                ok = REFERENCE['status'] == 'live' and age <= 6 and len(REFERENCE['prices']) == len(MARKET_STATE)
            agent_log('oversight', ('✅ سلامت عملیاتی تایید شد' if ok else f'⚠️ هشدار سلامت: فید={REFERENCE["status"]}، سن={age:.1f}ث'))
            if not ok: agent_log('watch', 'هشدار گروه ناظر: فید قیمت نیازمند رسیدگی است')
        time.sleep(15)

def mm_loop():
    """ایجنت بازارگردان: نقدینگی‌سازی"""
    mm_uid = 0
    my_orders = []
    while True:
        enabled = AGENTS['mm']['enabled']
        # لغو نقل‌قیمت‌های قبلی
        for o in my_orders:
            m = MARKET_STATE[o['sym']]
            book = m.bids if o['side'] == 'buy' else m.asks
            dq = book.get(o['price'])
            if dq and o in dq: dq.remove(o)
        my_orders = []
        if enabled:
            for sym, m in MARKET_STATE.items():
                cfg = m.cfg
                mid = m.price
                spread = cfg['tick'] * random.uniform(2, 4)
                for i in range(7):
                    for side, sgn in (('buy', -1), ('sell', 1)):
                        px = rtick(mid + sgn * spread * (i + 1) * random.uniform(0.9, 1.15), cfg['tick'])
                        q = rstep(cfg['qbase'] * random.uniform(0.15, 1.2) * math.exp(-i * 0.22), cfg['step']) or cfg['step']
                        o = dict(uid=mm_uid, sym=sym, side=side, type='limit', price=px, qty=q, rem=q, ts=time.time(), cfg_base=cfg['base'])
                        book = m.bids if side == 'buy' else m.asks
                        book[px].append(o)
                        my_orders.append(o)
                        # اگر نقل‌قیمت با سفارش کاربر کراس شد، مچ کن
                        match_order(m, dict(uid=-9, sym=sym, side='sell' if side == 'buy' else 'buy',
                                            type='limit', price=px, qty=0, rem=0, cfg_base=cfg['base']))
            agent_log('mm', f'نقل‌قیمت‌گذاری {len(my_orders)} سطح در {len(MARKET_STATE)} بازار انجام شد')
        time.sleep(2.0)

def risk_loop():
    """ایجنت مدیر ریسک: پایش مارجین و لیکوئیدیشن"""
    while True:
        if AGENTS['risk']['enabled']:
            for key in list(POS.keys()):
                uid, sym = key
                pos = POS.get(key)
                if not pos or pos['size'] == 0: continue
                m = MARKET_STATE.get(sym)
                if not m: continue
                px = m.price
                upnl = (px - pos['entry']) * pos['size']
                maint = MAINT_RATE * abs(pos['size']) * px
                if pos['margin'] + upnl <= maint:
                    # اجرای لیکوئیدیشن: بستن اجباری در بازار
                    side = 'sell' if pos['size'] > 0 else 'buy'
                    q = abs(pos['size'])
                    lpx = px * (0.99 if side == 'sell' else 1.01)
                    o = dict(uid=uid, sym=sym, side=side, type='market', price=lpx, qty=q, rem=q,
                             lev=pos['lev'], intent='close', cfg_base=m.cfg['base'], ts=time.time())
                    ORDER_SEQ[0] += 1; o['id'] = ORDER_SEQ[0]
                    POSLOCKS[(uid, sym)] = 0.0
                    match_order(m, o)
                    # تسویه نهایی با جریمه
                    pos2 = POS.get(key)
                    if pos2:
                        BAL[uid]['USDT'] = max(0.0, BAL[uid].get('USDT', 0) + pos2['margin'] - abs(pos2['size']) * px * LIQ_FEE)
                        POS.pop(key, None)
                    save_bal(uid)
                    STATS['liqs'] += 1
                    agent_log('risk', f'⚠️ لیکوئیدیشن: پوزیشن {sym} کاربر #{uid} در قیمت {px:,.2f} بسته شد')
                    user_event(uid, dict(type='liquidation', symbol=sym, price=px))
                    user_event(uid, dict(type='wallet'))
        time.sleep(1.0)

BOTS = {}  # uid -> bot state

def ema(vals, n):
    k = 2 / (n + 1); e = vals[0]
    for v in vals[1:]: e = v * k + e * (1 - k)
    return e

def bot_loop():
    """ایجنت معامله‌گر خودکار برای حساب کاربران"""
    while True:
        for uid in list(BOTS.keys()):
            st = BOTS.get(uid)
            if not st or not st.get('active'): continue
            sym = st['sym']
            m = MARKET_STATE.get(sym)
            if not m or len(m.tickhist) < 60: continue
            vals = list(m.tickhist)
            f, s = ema(vals[-60:], 12), ema(vals[-120:], 40)
            sig = 1 if f > s * 1.0002 else (-1 if f < s * 0.9998 else 0)
            if sig and sig != st.get('last_sig'):
                st['last_sig'] = sig
                pos = POS.get((uid, sym))
                want_long = sig > 0
                if pos and ((pos['size'] > 0) != want_long):
                    side = 'sell' if pos['size'] > 0 else 'buy'
                    r = place_user_order(uid, sym, side, 'market', 0, abs(pos['size']), pos['lev'])
                    agent_log('bot', f'ربات کاربر #{uid}: بستن پوزیشن {sym} ({side}) — {"موفق" if r["ok"] else r["error"]}')
                    pos = None
                if not POS.get((uid, sym)):
                    px = m.price
                    qty = rstep(max(MARKETS[sym]['minq'], 400 * st['lev'] / px), MARKETS[sym]['step'])
                    side = 'buy' if want_long else 'sell'
                    r = place_user_order(uid, sym, side, 'market', 0, qty, st['lev'])
                    agent_log('bot', f'ربات کاربر #{uid}: سیگنال EMA {"صعودی" if want_long else "نزولی"} → {side} {qty} {sym} — {"موفق" if r["ok"] else r["error"]}')
                    user_event(uid, dict(type='bot', msg=f'سیگنال {"صعودی" if want_long else "نزولی"} — {side} {qty}'))
        time.sleep(2.5)

def ping_loop():
    while True:
        time.sleep(20)
        with HUB.lk: cl = list(HUB.clients)
        for c in cl:
            c.send(json.dumps(dict(ch='ping', data=time.time())))

# ------------------------------ چت‌بات پشتیبانی ------------------------------
CHAT_KB = [
    (['فیس', 'کارمزد', 'fee'], 'کارمزد معاملات در آریاکس: میکر ۰.۰۲٪ و تیکر ۰.۰۵٪ است. جریمه لیکوئیدیشن هم ۰.۷۵٪ ارزش پوزیشن است.'),
    (['فاست', 'تست', 'سرمایه آزمایشی', 'پاداش'], 'از دکمه «دریافت سرمایه تستی» در بالای صفحه استفاده کنید؛ هر ۳۰ دقیقه ۱۰,۰۰۰ USDT آزمایشی دریافت می‌کنید. پاداش ثبت‌نام هم ۲۰,۰۰۰ USDT است.'),
    (['اهرم', 'لوریج', 'leverage'], 'در بخش فیوچرز می‌توانید تا ۱۰۰ برابر (BTC) اهرم استفاده کنید. اهرم سود و زیان را بزرگ می‌کند؛ مراقب لیکوئیدیشن باشید!'),
    (['لیکوئید', 'liquidat', 'کال مارجین'], 'وقتی مارجین پوزیشن شما به‌همراه سود/زیان شناور از مارجین نگهداری (۰.۵٪) کمتر شود، ایجنت مدیر ریسک پوزیشن را به‌صورت خودکار می‌بندد.'),
    (['واریز', 'deposit'], 'برای واریز آزمایشی به کیف پول → واریز بروید، دارایی و مبلغ را انتخاب کنید؛ پس از تأیید شبیه‌سازی‌شده به موجودی شما اضافه می‌شود.'),
    (['برداشت', 'withdraw'], 'در کیف پول → برداشت، دارایی، مبلغ و آدرس را وارد کنید. در تست‌نت برداشت شبیه‌سازی می‌شود و بلافاصله کسر می‌گردد.'),
    (['ربات', 'معامله‌گر', 'bot', 'خودکار'], 'در داشبورد هوش مصنوعی می‌توانید «معامله‌گر خودکار» را فعال کنید تا ایجنت معامله‌گر بر اساس تقاطع EMA برای شما معامله کند.'),
    (['ایجنت', 'هوش مصنوعی', 'ai', 'مدیریت'], 'این صرافی توسط ۶ ایجنت هوش مصنوعی اداره می‌شود: اوراکل بازار، بازارگردان، مدیر ریسک، ناظر تقلب، معامله‌گر خودکار و پشتیبان. وضعیت همه را در داشبورد هوش مصنوعی ببینید.'),
    (['سلام', 'درود', 'hi', 'hello'], 'سلام! 👋 من پشتیبان هوشمند آریاکس هستم. درباره کارمزدها، واریز/برداشت، اهرم، لیکوئیدیشن یا ربات معامله‌گر بپرسید.'),
    (['قیمت', 'بیتکوین', 'btc'], 'قیمت‌ها در تست‌نت توسط ایجنت اوراکل شبیه‌سازی می‌شوند و مبنای معامله، دفتر سفارشات زنده است.'),
    (['اسپات', 'spot', 'نقدی'], 'در بازار اسپات دارایی واقعی خریدوفروش می‌شود؛ در فیوچرز قرارداد دائمی با اهرم معامله می‌کنید.'),
]

def chat_reply(msg):
    STATS['chats'] += 1
    low = msg.lower()
    for keys, ans in CHAT_KB:
        if any(k in low for k in keys):
            return ans
    return 'سؤال شما ثبت شد 🤝 می‌توانید درباره: کارمزد، واریز/برداشت، فاست، اهرم، لیکوئیدیشن، ربات معامله‌گر یا ایجنت‌های هوش مصنوعی بپرسید.'

# ------------------------------ HTTP / WS Handler ------------------------------
STATIC = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
CT = {'.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8'}

class Handler(BaseHTTPRequestHandler):
    server_version = 'AriaX/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a): pass

    # ---------- WebSocket ----------
    def _handle_ws(self):
        key = self.headers.get('Sec-WebSocket-Key', '')
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.wfile.write((
            'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n').encode())
        self.wfile.flush()
        conn = self.connection
        conn.settimeout(75)
        client = WSClient(conn)
        HUB.add(client)
        try:
            for op, pl in ws_frames(conn, self.rfile):
                if op == 8: break
                if op == 9:
                    send_ws(conn, 10, pl); continue
                if op == 10:
                    client.last_pong = time.time(); continue
                if op != 1: continue
                try: msg = json.loads(pl.decode())
                except Exception: continue
                op_ = msg.get('op')
                if op_ == 'auth':
                    tok = msg.get('token')
                    if tok in SESSIONS:
                        client.uid = SESSIONS[tok]
                        client.send(json.dumps(dict(ch='auth', data=dict(ok=True, uid=client.uid))))
                elif op_ == 'sub':
                    client.subs.add(msg.get('ch', ''))
                elif op_ == 'unsub':
                    client.subs.discard(msg.get('ch', ''))
        except Exception:
            pass
        finally:
            HUB.drop(client)
            try: conn.close()
            except Exception: pass

    # ---------- ابزارها ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            return json.loads(self.rfile.read(n).decode() or '{}') if n else {}
        except Exception:
            return {}

    def _auth_uid(self):
        tok = self.headers.get('Authorization', '').replace('Bearer ', '') or parse_qs(urlparse(self.path).query).get('token', [''])[0]
        return SESSIONS.get(tok), tok

    def _static(self, path):
        name = STATIC.get(path, path.lstrip('/'))
        fp = os.path.join(ROOT, 'static', name)
        if not os.path.isfile(fp):
            return self._json(dict(ok=False, error='not found'), 404)
        data = open(fp, 'rb').read()
        ext = os.path.splitext(fp)[1]
        self.send_response(200)
        self.send_header('Content-Type', CT.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path); p, q = u.path, parse_qs(u.query)
        if u.path == '/ws':
            return self._handle_ws()
        if p.startswith('/api/'):
            return self._api_get(p, q)
        return self._static(p)

    def _api_get(self, p, q):
        uid, _ = self._auth_uid()
        if p == '/api/markets':
            with REF_LOCK:
                reference = dict(source=REFERENCE['source'], status=REFERENCE['status'], updated=REFERENCE['updated'],
                                 age=round(time.time()-REFERENCE['updated'], 2) if REFERENCE['updated'] else None,
                                 error=REFERENCE['error'])
            return self._json(dict(ok=True, data={s: ticker_json(m) for s, m in MARKET_STATE.items()}, reference=reference))
        if p == '/api/config':
            return self._json(dict(ok=True, data=MARKETS, assets=ASSETS,
                                   fees=dict(taker=TAKER_FEE, maker=MAKER_FEE, liq=LIQ_FEE)))
        if p == '/api/book':
            sym = q.get('symbol', ['BTC/USDT'])[0]
            if sym not in MARKET_STATE: return self._json(dict(ok=False, error='نماد نامعتبر'), 400)
            return self._json(dict(ok=True, data=book_snapshot(MARKET_STATE[sym])))
        if p == '/api/trades':
            sym = q.get('symbol', ['BTC/USDT'])[0]
            m = MARKET_STATE.get(sym)
            return self._json(dict(ok=True, data=list(m.trades)[:100]) if m else dict(ok=False, error='نماد نامعتبر'))
        if p == '/api/candles':
            sym = q.get('symbol', ['BTC/USDT'])[0]
            iv = q.get('interval', ['1m'])[0]
            m = MARKET_STATE.get(sym)
            if not m: return self._json(dict(ok=False, error='نماد نامعتبر'))
            live = kraken_candles(sym, iv)
            return self._json(dict(ok=True, data=live or agg_candles(m, iv), source='Kraken' if live else 'internal-fallback',
                                   stale=not bool(live)))
        if p == '/api/ai':
            return self._json(dict(ok=True, agents=[dict(id=a['id'], name=a['name'], role=a['role'], icon=a['icon'],
                                                           enabled=a['enabled'], actions=a['actions'], last=a['last'],
                                                           logs=list(a['logs'])[:12]) for a in AGENTS.values()],
                                   stats=dict(uptime=round(time.time() - STATS['start']), orders=STATS['orders'],
                                              fills=STATS['fills'], liqs=STATS['liqs'], flags=STATS['flags'],
                                              chats=STATS['chats'], users=STATS['users'], ws=HUB.count(),
                                              open_orders=len(OPEN_ORDERS),
                                              positions=len(POS))))
        if p == '/api/wallet':
            if not uid: return self._json(dict(ok=False, error='auth'), 401)
            bals = {a: round(BAL[uid].get(a, 0.0), 8) for a in ASSETS}
            locks = {a: round(LOCKS[uid].get(a, 0.0), 8) for a in ASSETS}
            locks['MARGIN'] = round(LOCKS[uid].get('MARGIN', 0.0), 4)
            return self._json(dict(ok=True, balances=bals, locks=locks,
                                   margin_used=round(margin_used(uid), 4),
                                   free_margin=round(free_margin(uid), 4),
                                   equity=round(BAL[uid].get('USDT', 0) + sum(
                                       (MARKET_STATE[s].price - ps['entry']) * ps['size']
                                       for (u, s), ps in POS.items() if u == uid), 4)))
        if p == '/api/ledger':
            if not uid: return self._json(dict(ok=False, error='auth'), 401)
            with _db_lock, db() as c:
                rows = [dict(type=r[0], asset=r[1], amount=r[2], note=r[3], ts=r[4])
                        for r in c.execute('SELECT type,asset,amount,note,ts FROM ledger WHERE uid=? ORDER BY id DESC LIMIT 60', (uid,))]
            return self._json(dict(ok=True, data=rows))
        if p == '/api/orders':
            if not uid: return self._json(dict(ok=False, error='auth'), 401)
            rows = [order_json(o) for o in OPEN_ORDERS.values() if o['uid'] == uid]
            return self._json(dict(ok=True, data=rows))
        if p == '/api/positions':
            if not uid: return self._json(dict(ok=False, error='auth'), 401)
            rows = []
            for (u, s), ps in POS.items():
                if u != uid or ps['size'] == 0: continue
                px = MARKET_STATE[s].price
                upnl = (px - ps['entry']) * ps['size']
                rows.append(dict(symbol=s, size=ps['size'], entry=ps['entry'], lev=ps['lev'],
                                 margin=round(ps['margin'], 4), upnl=round(upnl, 4), mark=px,
                                 liq=round(ps['entry'] * (1 - (1 / ps['lev']) + MAINT_RATE) if ps['size'] > 0
                                           else ps['entry'] * (1 + (1 / ps['lev']) - MAINT_RATE), 2)))
            return self._json(dict(ok=True, data=rows))
        return self._json(dict(ok=False, error='not found'), 404)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        if u.path != '/ws' and u.path.startswith('/api/'):
            return self._api_post(u.path, self._body())
        return self._json(dict(ok=False, error='not found'), 404)

    def _api_post(self, p, b):
        uid, tok = self._auth_uid()
        if p == '/api/auth/register':
            email, pw, name = (b.get('email') or '').strip(), b.get('password') or '', (b.get('name') or '').strip()
            if '@' not in email or len(pw) < 4:
                return self._json(dict(ok=False, error='ایمیل معتبر و رمز حداقل ۴ کاراکتر لازم است'))
            try:
                nuid = create_user(email, name or email.split('@')[0], pw)
            except sqlite3.IntegrityError:
                return self._json(dict(ok=False, error='این ایمیل قبلاً ثبت شده است'))
            token = secrets.token_hex(24)
            SESSIONS[token] = nuid
            load_bal(nuid)
            STATS['users'] += 1
            agent_log('support', f'کاربر جدید #{nuid} ثبت‌نام کرد — پاداش ۲۰,۰۰۰ USDT واریز شد')
            return self._json(dict(ok=True, token=token, uid=nuid))
        if p == '/api/auth/login':
            nuid = auth_user(b.get('email') or '', b.get('password') or '')
            if not nuid: return self._json(dict(ok=False, error='ایمیل یا رمز عبور اشتباه است'))
            token = secrets.token_hex(24)
            SESSIONS[token] = nuid
            load_bal(nuid)
            return self._json(dict(ok=True, token=token, uid=nuid))
        if p == '/api/auth/logout':
            SESSIONS.pop(tok, None)
            return self._json(dict(ok=True))
        if not uid:
            return self._json(dict(ok=False, error='ابتدا وارد شوید'), 401)
        if p == '/api/order':
            r = place_user_order(uid, b.get('symbol'), b.get('side'), b.get('type'),
                                 b.get('price'), b.get('qty'), b.get('lev', 10))
            return self._json(r)
        if p == '/api/cancel':
            ok, msg = cancel_order(uid, int(b.get('id', 0)))
            return self._json(dict(ok=ok, error=None if ok else msg))
        if p == '/api/cancelall':
            n = 0
            for o in [x for x in OPEN_ORDERS.values() if x['uid'] == uid]:
                if not b.get('symbol') or o['sym'] == b['symbol']:
                    cancel_order(uid, o['id']); n += 1
            return self._json(dict(ok=True, n=n))
        if p == '/api/faucet':
            last = FAUCET_TS.get(uid, 0)
            if time.time() - last < 1800:
                return self._json(dict(ok=False, error=f'فاست هر ۳۰ دقیقه فعال است؛ {int(1800-(time.time()-last))} ثانیه صبر کنید'))
            FAUCET_TS[uid] = time.time()
            BAL[uid]['USDT'] = BAL[uid].get('USDT', 0) + 10000
            save_bal(uid)
            ledger(uid, 'faucet', 'USDT', 10000, 'دریافت سرمایه تستی')
            user_event(uid, dict(type='wallet'))
            return self._json(dict(ok=True, amount=10000))
        if p == '/api/wallet/deposit':
            asset, amt = b.get('asset'), float(b.get('amount', 0))
            if asset not in ASSETS or amt <= 0 or amt > 1e9:
                return self._json(dict(ok=False, error='دارایی یا مبلغ نامعتبر'))
            BAL[uid][asset] = BAL[uid].get(asset, 0) + amt
            save_bal(uid)
            ledger(uid, 'deposit', asset, amt, f'واریز آزمایشی ({b.get("network","TRC20")})')
            txid = secrets.token_hex(32)
            user_event(uid, dict(type='wallet'))
            return self._json(dict(ok=True, txid=txid))
        if p == '/api/wallet/withdraw':
            asset, amt = b.get('asset'), float(b.get('amount', 0))
            addr = b.get('address', '')
            if asset not in ASSETS or amt <= 0:
                return self._json(dict(ok=False, error='دارایی یا مبلغ نامعتبر'))
            if len(addr) < 8:
                return self._json(dict(ok=False, error='آدرس شبیه‌سازی‌شده باید حداقل ۸ کاراکتر باشد'))
            if available(uid, asset) < amt:
                return self._json(dict(ok=False, error='موجودی قابل‌برداشت کافی نیست'))
            BAL[uid][asset] = BAL[uid].get(asset, 0) - amt
            save_bal(uid)
            ledger(uid, 'withdraw', asset, -amt, f'برداشت به {addr[:12]}…')
            user_event(uid, dict(type='wallet'))
            return self._json(dict(ok=True, txid=secrets.token_hex(32)))
        if p == '/api/ai/toggle':
            aid = b.get('id')
            if aid in AGENTS:
                AGENTS[aid]['enabled'] = bool(b.get('enabled'))
                agent_log(aid, f'وضعیت ایجنت توسط مدیر تغییر کرد: {"فعال ✅" if AGENTS[aid]["enabled"] else "غیرفعال ⛔"}')
                return self._json(dict(ok=True))
            return self._json(dict(ok=False, error='ایجنت یافت نشد'), 404)
        if p == '/api/chat':
            ans = chat_reply(b.get('msg', ''))
            agent_log('support', f'پاسخ به کاربر #{uid}: «{b.get("msg","")[:40]}»')
            return self._json(dict(ok=True, reply=ans))
        if p == '/api/bot':
            act = b.get('action')
            sym = b.get('symbol', 'BTCUSD')
            if sym not in MARKETS or MARKETS[sym]['kind'] != 'perp':
                return self._json(dict(ok=False, error='نماد فیوچرز انتخاب کنید'))
            lev = max(1, min(int(b.get('lev', 5)), 20))
            if act == 'start':
                BOTS[uid] = dict(active=True, sym=sym, lev=lev, last_sig=0)
                AGENTS['bot']['enabled'] = True
                agent_log('bot', f'ربات معامله‌گر برای کاربر #{uid} روی {sym} با اهرم {lev} فعال شد')
                return self._json(dict(ok=True))
            if act == 'stop':
                if uid in BOTS: BOTS[uid]['active'] = False
                agent_log('bot', f'ربات معامله‌گر کاربر #{uid} متوقف شد')
                return self._json(dict(ok=True))
            if act == 'status':
                st = BOTS.get(uid)
                return self._json(dict(ok=True, active=bool(st and st.get('active')), sym=st and st['sym']))
            return self._json(dict(ok=False, error='اکشن نامعتبر'), 400)
        return self._json(dict(ok=False, error='not found'), 404)

# ------------------------------ راه‌اندازی ------------------------------
def main():
    init_db()
    with _db_lock, db() as c:
        STATS['users'] = c.execute('SELECT COUNT(*) FROM users').fetchone()[0] or 1
    agent_log('oracle', 'ایجنت اوراکل راه‌اندازی شد — خوراک قیمت ۸ بازار فعال است')
    agent_log('mm', 'بازارگردان آماده‌باش: ۷ سطح نقل‌قیمت در هر سمت بازار')
    agent_log('risk', 'پایش مارجین فعال — نرخ نگهداری ۰.۵٪')
    agent_log('watch', 'ناظر تقلب آنلاین — آستانه انحراف قیمت ۵٪')
    threading.Thread(target=reference_feed_loop, daemon=True).start()
    threading.Thread(target=oracle_loop, daemon=True).start()
    threading.Thread(target=thinktank_loop, daemon=True).start()
    threading.Thread(target=oversight_loop, daemon=True).start()
    threading.Thread(target=mm_loop, daemon=True).start()
    threading.Thread(target=risk_loop, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=ping_loop, daemon=True).start()
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    print(f'✅ AriaX Testnet Exchange running on http://0.0.0.0:{PORT}')
    srv.serve_forever()

if __name__ == '__main__':
    main()
