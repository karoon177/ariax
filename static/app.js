/* آریا‌اکس — کلاینت معاملاتی */
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);

const S = {
  token: localStorage.getItem('ax_token') || null,
  uid: null, cfg: {}, tickers: {},
  symbol: localStorage.getItem('ax_sym') || 'BTC/USDT',
  kind: 'spot', side: 'buy', otype: 'limit', interval: localStorage.getItem('ax_interval') || '1m',
  candles: [], book: null, trades: [],
  orders: [], positions: [], fills: [], wallet: null,
  botActive: false,
};

/* ---------------- helpers ---------------- */
function fmt(n, d = 2) {
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
function pfmt(sym, v) {
  const t = S.cfg[sym] && S.cfg[sym].tick;
  const d = t ? Math.min(8, Math.max(0, Math.ceil(-Math.log10(t)))) : 2;
  return fmt(v, d);
}
function tfmt(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString('en-GB'); }
function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + type; el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
async function api(path, body = null) {
  const opt = { method: body ? 'POST' : 'GET', headers: {} };
  if (S.token) opt.headers['Authorization'] = 'Bearer ' + S.token;
  if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let j = {}; try { j = await r.json(); } catch (e) {}
  if (r.status === 401 && S.token) { logout(false); }
  return j;
}

/* ---------------- WebSocket ---------------- */
let ws = null, wsRetry = 0;
const subs = new Set();
function wsConnect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => {
    wsRetry = 0; $('#conn-dot').classList.add('on');
    if (S.token) ws.send(JSON.stringify({ op: 'auth', token: S.token }));
    subs.forEach(c => ws.send(JSON.stringify({ op: 'sub', ch: c })));
    subSymbol(S.symbol);
  };
  ws.onmessage = e => {
    let m; try { m = JSON.parse(e.data); } catch { return; }
    onWS(m.ch, m.data);
  };
  ws.onclose = () => {
    $('#conn-dot').classList.remove('on');
    setTimeout(wsConnect, Math.min(5000, 300 * (++wsRetry + 1)));
  };
  ws.onerror = () => ws.close();
}
function sub(ch) {
  if (!subs.has(ch)) { subs.add(ch); if (ws && ws.readyState === 1) ws.send(JSON.stringify({ op: 'sub', ch })); }
}
function subSymbol(sym) {
  ['book:', 'trades:', 'candle:'].forEach(p => sub(p + sym));
  sub('tickers');
  if (S.token) sub('user');
}

function onWS(ch, data) {
  if (ch === 'ping') return;
  if (ch === 'tickers') {
    S.tickers = data; renderTickerStrip(); renderSymBar(); renderPositions(); renderMarkets();
    if (document.getElementById('view-wallet').classList.contains('hidden') === false) renderWalletValues();
    return;
  }
  if (ch === 'book:' + S.symbol) { S.book = data; renderBook(); return; }
  if (ch === 'trades:' + S.symbol) {
    S.trades = data.concat(S.trades).slice(0, 60); renderTrades(); return;
  }
  if (ch === 'candle:' + S.symbol) {
    if (S.interval === '1m') mergeCandle(data); return;
  }
  if (ch === 'user' && data.uid === S.uid) { onUserEvent(data); return; }
}

function mergeCandle(c) {
  if (!S.candles.length) { S.candles.push(c.slice()); chart.setData(S.candles); return; }
  const last = S.candles[S.candles.length - 1];
  if (c[0] === last[0]) S.candles[S.candles.length - 1] = c.slice();
  else if (c[0] > last[0]) { S.candles.push(c.slice()); if (S.candles.length > 600) S.candles.shift(); }
  chart.setData(S.candles);
}

/* ---------------- auth ---------------- */
function setAuthUI() {
  if (S.uid) {
    $('#btn-auth').classList.add('hidden');
    $('#user-chip').classList.remove('hidden');
    $('#user-chip').innerHTML = `<span>👤 کاربر #${S.uid}</span><button onclick="logout(true)">خروج</button>`;
  } else {
    $('#btn-auth').classList.remove('hidden');
    $('#user-chip').classList.add('hidden');
  }
}
async function logout(callApi) {
  if (callApi) await api('/api/auth/logout', {});
  S.token = null; S.uid = null; localStorage.removeItem('ax_token');
  setAuthUI(); toast('از حساب خارج شدید');
}
window.logout = logout;

async function tryAutoLogin() {
  if (!S.token) return;
  const w = await api('/api/wallet');
  if (w.ok) { S.uid = parseInt(localStorage.getItem('ax_uid') || '0') || null; if (!S.uid) { S.uid = 0; } setAuthUI(); sub('user'); }
  else { S.token = null; localStorage.removeItem('ax_token'); setAuthUI(); }
}

/* ---------------- views ---------------- */
function switchView(v) {
  $$('.view').forEach(x => x.classList.add('hidden'));
  $('#view-' + v).classList.remove('hidden');
  $$('.navbtn').forEach(b => b.classList.toggle('active', b.dataset.v === v));
  if (v === 'ai') loadAI();
  if (v === 'wallet') loadWallet();
  if (v === 'markets') renderMarkets();
}
window.switchView = switchView;

/* ---------------- chart ---------------- */
class Chart {
  constructor(cv) {
    this.cv = cv; this.ctx = cv.getContext('2d');
    this.data = []; this.vis = 120; this.mouse = null;
    cv.addEventListener('wheel', e => {
      e.preventDefault();
      this.vis = Math.max(40, Math.min(400, this.vis + (e.deltaY > 0 ? 15 : -15)));
      this.draw();
    }, { passive: false });
    cv.addEventListener('mousemove', e => {
      const r = cv.getBoundingClientRect();
      this.mouse = { x: e.clientX - r.left, y: e.clientY - r.top };
      this.draw();
    });
    cv.addEventListener('mouseleave', () => { this.mouse = null; this.draw(); });
    window.addEventListener('resize', () => this.draw());
    this._raf = null;
  }
  setData(d) {
    this.data = d;
    if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = null; this.draw(); });
  }
  draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = this.cv.clientWidth, H = this.cv.clientHeight;
    if (!W || !H) return;
    this.cv.width = W * dpr; this.cv.height = H * dpr;
    const c = this.ctx; c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.fillStyle = '#101527'; c.fillRect(0, 0, W, H);
    const data = this.data.slice(-this.vis);
    if (data.length < 2) { c.fillStyle = '#8b94b3'; c.font = '13px sans-serif'; c.textAlign = 'center'; c.fillText('در حال بارگذاری داده…', W / 2, H / 2); return; }
    const padL = 8, padR = 64, padT = 12;
    const priceH = H * 0.74, volH = H * 0.18, volY = H * 0.78;
    let lo = Infinity, hi = -Infinity, vmax = 0;
    data.forEach(k => { lo = Math.min(lo, k[3]); hi = Math.max(hi, k[2]); vmax = Math.max(vmax, k[5]); });
    const pad = (hi - lo) * 0.08 || hi * 0.01; lo -= pad; hi += pad;
    const X = i => padL + (i + 0.5) * (W - padL - padR) / data.length;
    const Y = p => padT + (hi - p) / (hi - lo) * (priceH - padT);
    // grid
    c.strokeStyle = '#1a2138'; c.fillStyle = '#576084'; c.font = '10.5px sans-serif'; c.textAlign = 'left'; c.lineWidth = 1;
    for (let g = 0; g <= 5; g++) {
      const p = hi - (hi - lo) * g / 5, y = Y(p);
      c.beginPath(); c.moveTo(padL, y); c.lineTo(W - padR, y); c.stroke();
      c.fillText(fmt(p, p > 1000 ? 0 : p > 10 ? 2 : 4), W - padR + 6, y + 3);
    }
    const cw = Math.max(1.5, (W - padL - padR) / data.length * 0.66);
    data.forEach((k, i) => {
      const up = k[4] >= k[1];
      c.strokeStyle = c.fillStyle = up ? '#16c784' : '#ea3943';
      const x = X(i);
      c.beginPath(); c.moveTo(x, Y(k[2])); c.lineTo(x, Y(k[3])); c.stroke();
      const yO = Y(k[1]), yC = Y(k[4]);
      c.fillRect(x - cw / 2, Math.min(yO, yC), cw, Math.max(1, Math.abs(yC - yO)));
      // volume
      const vh = vmax ? k[5] / vmax * volH : 0;
      c.globalAlpha = 0.45; c.fillRect(x - cw / 2, volY + volH - vh, cw, vh); c.globalAlpha = 1;
    });
    // last price line
    const lp = data[data.length - 1][4];
    const ylp = Y(lp);
    c.setLineDash([4, 4]); c.strokeStyle = lp >= data[data.length - 1][1] ? '#16c784' : '#ea3943';
    c.beginPath(); c.moveTo(padL, ylp); c.lineTo(W - padR, ylp); c.stroke(); c.setLineDash([]);
    c.fillStyle = lp >= data[data.length - 1][1] ? '#16c784' : '#ea3943';
    c.fillRect(W - padR, ylp - 9, padR, 18);
    c.fillStyle = '#fff'; c.fillText(fmt(lp, lp > 1000 ? 1 : lp > 10 ? 2 : 5), W - padR + 4, ylp + 3.5);
    // crosshair
    if (this.mouse) {
      const i = Math.round((this.mouse.x - padL) / ((W - padL - padR) / data.length) - 0.5);
      if (i >= 0 && i < data.length) {
        const k = data[i], x = X(i);
        c.strokeStyle = '#3b466e'; c.setLineDash([3, 3]);
        c.beginPath(); c.moveTo(x, 0); c.lineTo(x, H); c.stroke();
        c.beginPath(); c.moveTo(padL, this.mouse.y); c.lineTo(W - padR, this.mouse.y); c.stroke();
        c.setLineDash([]);
        const d = new Date(k[0] * 1000);
        $('#ohlc-line').innerHTML =
          `${d.toLocaleString('en-GB', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })} &nbsp; O <b style="color:#e7ecf8">${fmt(k[1], 6)}</b> &nbsp; H <b class="g">${fmt(k[2], 6)}</b> &nbsp; L <b class="r">${fmt(k[3], 6)}</b> &nbsp; C <b style="color:${k[4] >= k[1] ? '#16c784' : '#ea3943'}">${fmt(k[4], 6)}</b> &nbsp; V ${fmt(k[5], 1)}`;
      }
    }
  }
}
const chart = new Chart($('#chart'));

/* ---------------- renders ---------------- */
function renderTickerStrip() {
  const el = $('#ticker-strip');
  el.innerHTML = Object.entries(S.tickers).map(([s, t]) =>
    `<div class="ts-item" onclick="selectSymbol('${s}')"><b>${s}</b><span class="p num">${pfmt(s, t.last)}</span><span class="${t.chg >= 0 ? 'g' : 'r'} num">${t.chg >= 0 ? '+' : ''}${t.chg}%</span></div>`).join('');
}
window.selectSymbol = function (sym) {
  if (!S.cfg[sym]) return;
  S.symbol = sym; localStorage.setItem('ax_sym', sym);
  S.kind = S.cfg[sym].kind === 'perp' ? 'perp' : 'spot';
  setKindUI(); switchView('trade');
  loadSymbolData();
};

function renderSymBar() {
  const t = S.tickers[S.symbol]; if (!t) return;
  $('#sb-symbol').textContent = S.symbol;
  $('#sb-kind').textContent = t.kind === 'perp' ? 'فیوچرز' : 'اسپات';
  $('#sb-price').textContent = pfmt(S.symbol, t.last);
  $('#sb-price').className = 'sym-price ' + (t.chg >= 0 ? 'g' : 'r');
  $('#sb-chg').textContent = (t.chg >= 0 ? '+' : '') + t.chg + '%';
  $('#sb-chg').className = 'sym-chg ' + (t.chg >= 0 ? 'g' : 'r');
  $('#sb-high').textContent = pfmt(S.symbol, t.high);
  $('#sb-low').textContent = pfmt(S.symbol, t.low);
  $('#sb-vol').textContent = '$' + fmt(t.vol, 0);
  // v2: mark price + funding rate strip (linear only)
  const fr = $('#sb-funding');
  if (fr) {
    if (t.kind === 'perp') {
      fr.parentElement.classList.remove('hidden');
      fr.textContent = (t.funding >= 0 ? '+' : '') + fmt(t.funding, 4) + '%';
      fr.className = 'num ' + (t.funding >= 0 ? 'r' : 'g');
    } else fr.parentElement.classList.add('hidden');
  }
  const mkEl = $('#sb-mark');
  if (mkEl) {
    if (t.kind === 'perp') { mkEl.parentElement.classList.remove('hidden'); mkEl.textContent = pfmt(S.symbol, t.mark); }
    else mkEl.parentElement.classList.add('hidden');
  }
  updateAvail();
}

function renderBook() {
  if (!S.book) return;
  const mk = (rows, cls) => {
    let cum = 0; const out = [];
    rows.forEach(r => { cum += r[1]; out.push([r[0], r[1], cum]); });
    const mx = out.length ? out[out.length - 1][2] : 1;
    return out.map(r => `<div class="brow ${cls}"><span class="depth" style="width:${r[2] / mx * 100}%"></span><b class="num">${pfmt(S.symbol, r[0])}</b><span class="num">${fmt(r[1], 6)}</span><span class="num">${fmt(r[2], 6)}</span></div>`).join('');
  };
  $('#asks').innerHTML = mk(S.book.asks.slice().reverse(), 'ask');
  $('#bids').innerHTML = mk(S.book.bids, 'bid');
  const spread = S.book.asks[0] && S.book.bids[0] ? S.book.asks[0][0] - S.book.bids[0][0] : 0;
  $('#book-spread').textContent = 'اسپرد: ' + fmt(spread, 6);
  $('#book-last').textContent = pfmt(S.symbol, S.book.last);
}

function renderTrades() {
  $('#recent-trades').innerHTML = S.trades.slice(0, 30).map(t =>
    `<div class="trow"><b class="${t[1] === 'buy' ? 'g' : 'r'} num">${pfmt(S.symbol, t[2])}</b><span class="num">${fmt(t[3], 6)}</span><span class="num">${tfmt(t[0])}</span></div>`).join('');
}

function renderOrders() {
  $('#cnt-orders').textContent = S.orders.length;
  $('#tbl-orders tbody').innerHTML = S.orders.map(o =>
    `<tr><td>${o.symbol}</td><td class="${o.side === 'buy' ? 'g' : 'r'}">${o.side === 'buy' ? 'خرید' : 'فروش'}</td><td>${o.type === 'limit' ? 'لیمیت' : 'مارکت'}</td><td class="num">${pfmt(o.symbol, o.price)}</td><td class="num">${fmt(o.qty, 6)}</td><td class="num">${fmt(o.rem, 6)}</td><td><button class="mini-x" onclick="cancelOrder(${o.id})">✕</button></td></tr>`).join('') || '<tr><td colspan="7" style="text-align:center;color:var(--mut)">سفارش بازی ندارید</td></tr>';
}

window.closePosition = async (symbol, size, lev) => {
  if (!confirm(`پوزیشن ${symbol} به‌طور کامل با سفارش مارکت بسته شود؟`)) return;
  const t = S.tickers[symbol];
  const r = await api('/api/order', { symbol, side: size > 0 ? 'sell' : 'buy', type: 'market', price: t ? t.last : 0, qty: Math.abs(size), lev });
  if (r.ok) { toast('درخواست بستن پوزیشن ثبت شد', 'ok'); setTimeout(loadUser, 500); }
  else toast(r.error || 'بستن پوزیشن ناموفق بود', 'err');
};

window.cancelOrder = async id => {
  const r = await api('/api/cancel', { id });
  if (!r.ok) toast(r.error || 'خطا', 'err');
};

// v2: set Take-Profit / Stop-Loss on an open position (Bybit trading-stop)
window.promptTPSL = async (symbol, tp, sl) => {
  const ntp = prompt('قیمت حد سود (TP) — خالی = بدون تغییر' + (tp ? `\nفعلی: ${tp}` : ''), tp || '');
  if (ntp === null) return;
  const nsl = prompt('قیمت حد ضرر (SL) — خالی = بدون تغییر' + (sl ? `\nفعلی: ${sl}` : ''), sl || '');
  if (nsl === null) return;
  const body = { symbol };
  if (ntp.trim() !== '') body.tp = parseFloat(ntp) || 0;
  if (nsl.trim() !== '') body.sl = parseFloat(nsl) || 0;
  const r = await api('/api/tpsl', body);
  if (r.ok) { toast('TP/SL ثبت شد ✅', 'ok'); loadUser(); }
  else toast(r.error || 'خطا', 'err');
};

function renderPositions() {
  $('#cnt-pos').textContent = S.positions.length;
  $('#tbl-positions tbody').innerHTML = S.positions.map(p => {
    const t = S.tickers[p.symbol] || { last: p.mark };
    const upnl = (t.last - p.entry) * p.size;
    const notional = Math.abs(p.size) * t.last;
    const roe = p.margin ? upnl / p.margin * 100 : 0;
    return `<tr><td>${p.symbol}</td><td class="${p.size > 0 ? 'g' : 'r'}">${p.size > 0 ? 'لانگ' : 'شورت'}</td><td class="num">${fmt(Math.abs(p.size), 6)}</td><td class="num">${pfmt(p.symbol, p.entry)}</td><td class="num">${pfmt(p.symbol, t.last)}</td><td>${p.lev}x</td><td class="num">${fmt(p.margin, 2)}</td><td class="num ${upnl >= 0 ? 'g' : 'r'}">${fmt(upnl, 2)} (${fmt(roe, 1)}%)</td><td class="num">${pfmt(p.symbol, p.liq)}</td><td><button class="mini danger-ghost" onclick="closePosition('${p.symbol}',${p.size},${p.lev})">بستن</button> <button class="mini ghost" onclick="promptTPSL('${p.symbol}',${p.tp || 'null'},${p.sl || 'null'})">TP/SL</button></td></tr>`;
  }).join('') || '<tr><td colspan="10" style="text-align:center;color:var(--mut)">پوزیشن بازی ندارید</td></tr>';
}

function renderAssets() {
  if (!S.wallet) return;
  const prices = { USDT: 1 };
  Object.keys(S.cfg).forEach(s => { prices[S.cfg[s].base] = S.tickers[s] ? S.tickers[s].last : prices[S.cfg[s].base]; });
  $('#tbl-assets tbody').innerHTML = ASSET_LIST.map(a => {
    const bal = S.wallet.balances[a] || 0, lk = S.wallet.locks[a] || 0;
    return `<tr><td><b>${a}</b></td><td class="num">${fmt(bal, 8)}</td><td class="num">${fmt(lk, 8)}</td><td class="num">${fmt(bal * (prices[a] || 0), 2)}</td></tr>`;
  }).join('');
}

// v2.1: dual-wallet cards (Spot / Futures) with real transfers
function renderDualWallets() {
  if (!S.wallet || !S.wallet.futures) return;
  const spotFree = S.wallet.spot ? S.wallet.spot.balances.USDT : (S.wallet.balances.USDT || 0);
  const spotLock = S.wallet.spot ? (S.wallet.spot.locks.USDT || 0) : (S.wallet.locks.USDT || 0);
  const futFree = S.wallet.futures.balances.USDT || 0;
  const futLock = S.wallet.futures.locks.USDT || 0;
  const card = (title, icon, free, lock, dir, btnLabel, color) => `
    <div style="flex:1;min-width:240px;border:1px solid var(--line,var(--mut));border-radius:12px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${icon} ${title}</b>
        <button class="mini ghost" onclick="transferModal('${dir}')">${btnLabel}</button>
      </div>
      <div class="num" style="font-size:1.35rem;margin:8px 0 2px;color:${color}">${fmt(free, 4)} <span style="font-size:.75rem;opacity:.7">USDT</span></div>
      <div style="font-size:.8rem;opacity:.75">در سفارش/مارجین: ${fmt(lock, 4)} · قابل استفاده: ${fmt(Math.max(0, free - lock), 4)}</div>
    </div>`;
  $('#dual-wallets').innerHTML =
    card('کیف اسپات', '🟢', spotFree, spotLock, 'spot_to_futures', '⇄ انتقال به فیوچرز', 'var(--g, #16a34a)') +
    card('کیف فیوچرز', '🟠', futFree, futLock, 'futures_to_spot', '⇄ انتقال به اسپات', 'var(--r, #dc2626)');
}

window.transferModal = async (direction) => {
  const toName = direction === 'spot_to_futures' ? 'فیوچرز' : 'اسپات';
  const fromName = direction === 'spot_to_futures' ? 'اسپات' : 'فیوچرز';
  const src = direction === 'spot_to_futures' ? S.wallet.spot : S.wallet.futures;
  const avail = Math.max(0, (src.balances.USDT || 0) - (src.locks.USDT || 0));
  const inp = prompt(`انتقال از کیف ${fromName} به کیف ${toName}\nقابل انتقال: ${fmt(avail, 4)} USDT\nمبلغ را وارد کنید:`);
  if (inp === null || inp === '') return;
  const amount = parseFloat(inp);
  if (!amount || amount <= 0) return toast('مبلغ نامعتبر است', 'err');
  const r = await api('/api/transfer', { from: direction === 'spot_to_futures' ? 'spot' : 'futures', to: direction === 'spot_to_futures' ? 'futures' : 'spot', amount });
  if (r.ok) { toast(`✅ ${fmt(r.moved, 4)} USDT به کیف ${toName} منتقل شد`, 'ok'); loadUser(); }
  else toast(r.error || 'انتقال ناموفق بود', 'err');
};

let ASSET_LIST = ['USDT', 'BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];

function renderMarkets() {
  const tb = $('#tbl-markets tbody');
  tb.innerHTML = Object.entries(S.tickers).map(([s, t]) => {
    const fund = t.kind === 'perp'
      ? `<td class="num ${t.funding >= 0 ? 'r' : 'g'}" style="font-size:.78rem">${t.funding >= 0 ? '+' : ''}${fmt(t.funding, 3)}%</td>`
      : '<td style="color:var(--mut)">—</td>';
    return `<tr onclick="selectSymbol('${s}')"><td><b>${s}</b></td><td>${t.kind === 'perp' ? 'فیوچرز' : 'اسپات'}</td><td class="num">${pfmt(s, t.last)}</td><td class="num ${t.chg >= 0 ? 'g' : 'r'}">${t.chg >= 0 ? '+' : ''}${t.chg}%</td><td class="num">${pfmt(s, t.high)}</td><td class="num">${pfmt(s, t.low)}</td><td class="num">$${fmt(t.vol, 0)}</td>${fund}<td><span style="color:var(--blue)">معامله ←</span></td></tr>`;
  }).join('');
}

/* ---------------- wallet ---------------- */
async function loadWallet() {
  if (!S.token) { openModal('auth'); return; }
  const [w, l] = await Promise.all([api('/api/wallet'), api('/api/ledger')]);
  if (!w.ok) return;
  S.wallet = w;
  renderWalletValues();
  $('#tbl-ledger tbody').innerHTML = (l.data || []).map(r => {
    const names = { bonus: 'پاداش', faucet: 'فاست', deposit: 'واریز', withdraw: 'برداشت' };
    return `<tr><td class="num">${new Date(r.ts * 1000).toLocaleString('en-GB')}</td><td>${names[r.type] || r.type}</td><td>${r.asset}</td><td class="num ${r.amount >= 0 ? 'g' : 'r'}">${fmt(r.amount, 6)}</td><td>${r.note}</td></tr>`;
  }).join('') || '<tr><td colspan="5" style="text-align:center;color:var(--mut)">تراکنشی ثبت نشده</td></tr>';
}

function renderWalletValues() {
  if (!S.wallet) return;
  const prices = { USDT: 1 };
  Object.keys(S.cfg).forEach(s => { prices[S.cfg[s].base] = S.tickers[s] ? S.tickers[s].last : prices[S.cfg[s].base]; });
  let eq = 0;
  $('#tbl-wallet tbody').innerHTML = ASSET_LIST.map(a => {
    const bal = S.wallet.balances[a] || 0, lk = S.wallet.locks[a] || 0;
    const val = bal * (prices[a] || 0); eq += val;
    return `<tr><td><b>${a}</b></td><td class="num">${fmt(bal, 8)}</td><td class="num">${fmt(lk, 8)}</td><td class="num">${fmt(bal - lk, 8)}</td><td class="num">${fmt(val, 2)}</td></tr>`;
  }).join('');
  eq += (S.wallet.equity || 0) - (S.wallet.balances.USDT || 0); // upnl
  $('#wl-equity').textContent = fmt(eq, 2) + ' USDT';
  $('#wl-margin').textContent = fmt(S.wallet.margin_used, 2);
  $('#wl-free').textContent = fmt(S.wallet.free_margin, 2);
  renderAssets();
  renderDualWallets();
  updateAvail();
}

/* ---------------- order form ---------------- */
function setKindUI() {
  $('#kind-spot').classList.toggle('active', S.kind === 'spot');
  $('#kind-perp').classList.toggle('active', S.kind === 'perp');
  $('#lev-row').classList.toggle('hidden', S.kind !== 'perp');
}
function setSideUI() {
  $('#side-buy').classList.toggle('active', S.side === 'buy');
  $('#side-sell').classList.toggle('active', S.side === 'sell');
  const b = $('#of-submit');
  b.className = 'btn big ' + (S.side === 'buy' ? 'buy-btn' : 'sell-btn');
  b.textContent = S.side === 'buy' ? 'خرید ' + S.symbol : 'فروش ' + S.symbol;
}
function updateAvail() {
  const el = $('#of-avail');
  if (!S.wallet) { el.textContent = 'برای معامله وارد شوید'; return; }
  if (S.kind === 'spot') {
    if (S.side === 'buy') el.textContent = fmt(S.wallet.balances.USDT - (S.wallet.locks.USDT || 0), 2) + ' USDT';
    else el.textContent = fmt((S.wallet.balances[S.cfg[S.symbol].base] || 0) - (S.wallet.locks[S.cfg[S.symbol].base] || 0), 6) + ' ' + S.cfg[S.symbol].base;
  } else {
    el.textContent = fmt(S.wallet.free_margin, 2) + ' USDT (مارجین آزاد)';
  }
}
function updateTotal() {
  const p = parseFloat($('#of-price').value) || (S.tickers[S.symbol] ? S.tickers[S.symbol].last : 0);
  const q = parseFloat($('#of-qty').value) || 0;
  let notional = p * q;
  if (S.kind === 'perp') notional /= (parseInt($('#of-lev').value) || 10);
  $('#of-total').textContent = fmt(notional, 2) + ' USDT' + (S.kind === 'perp' ? ' (مارجین)' : '');
}
async function submitOrder() {
  if (!S.token) { openModal('auth'); return; }
  const body = {
    symbol: S.symbol, side: S.side, type: S.otype,
    price: S.otype === 'limit' ? parseFloat($('#of-price').value) : 0,
    qty: parseFloat($('#of-qty').value), lev: parseInt($('#of-lev').value) || 10,
  };
  if (!body.qty || body.qty <= 0) { toast('مقدار سفارش را وارد کنید', 'warn'); return; }
  const r = await api('/api/order', body);
  if (r.ok) toast('✅ سفارش ثبت شد (#' + r.id + ')', 'ok');
  else if (S.kind === 'perp' && /مارجین|margin/i.test(r.error || ''))
    toast((r.error || '') + ' — از کیف پول ⇄ وجه به فیوچرز منتقل کنید', 'warn');
  else toast(r.error || 'خطا در ثبت سفارش', 'err');
  refreshUser();
}

/* ---------------- user data ---------------- */
async function refreshUser() {
  if (!S.token) return;
  const [o, p, w, f, perf] = await Promise.all([api('/api/orders'), api('/api/positions'), api('/api/wallet'), api('/api/fills'), api('/api/performance')]);
  if (f.ok) { fillsCache = f.data; renderFills(); }
  if (perf.ok) { S.performance = perf; renderPerformance(); }
  if (o.ok) S.orders = o.data;
  if (p.ok) S.positions = p.data;
  if (w.ok) S.wallet = w;
  renderOrders(); renderPositions(); renderWalletValues();
}
let fillsCache = [];
function onUserEvent(d) {
  if (d.type === 'fill') {
    fillsCache.unshift(d); fillsCache = fillsCache.slice(0, 80);
    renderFills();
    toast(`✅ معامله: ${d.side === 'buy' ? 'خرید' : 'فروش'} ${fmt(d.qty, 6)} ${d.symbol} @ ${fmt(d.price, 4)}`, 'ok');
    refreshUser();
  } else if (d.type === 'order') refreshUser();
  else if (d.type === 'wallet') refreshUser();
  else if (d.type === 'liquidation') toast(`⚠️ پوزیشن ${d.symbol} شما لیکوئید شد!`, 'err');
  else if (d.type === 'position') refreshUser();
  else if (d.type === 'bot') toast('🤖 ' + d.msg, 'warn');
  else if (d.type === 'pnl') toast(`PnL realise شد: ${fmt(d.pnl, 2)} USDT`, d.pnl >= 0 ? 'ok' : 'warn');
}
function renderPerformance() {
  const p = S.performance; const el = $('#performance-summary'); if (!el || !p) return;
  const cls = p.net_pnl >= 0 ? 'g' : 'r';
  el.innerHTML = `<div><span>سود/زیان تحقق‌یافته</span><b class="${p.realized_pnl >= 0 ? 'g':'r'}">${fmt(p.realized_pnl, 4)} USDT</b></div><div><span>کارمزد کل</span><b>${fmt(p.fees, 4)} USDT</b></div><div><span>تعداد اجرا</span><b>${p.trades}</b></div><div><span>سود خالص</span><b class="${cls}">${fmt(p.net_pnl, 4)} USDT</b></div>`;
}
function renderFills() {
  $('#tbl-history tbody').innerHTML = fillsCache.map(f =>
    `<tr><td class="num">${tfmt(f.ts || Date.now() / 1000)}</td><td>${f.symbol}</td><td class="${f.side === 'buy' ? 'g' : 'r'}">${f.side === 'buy' ? 'خرید' : 'فروش'}</td><td class="num">${pfmt(f.symbol, f.price)}</td><td class="num">${fmt(f.qty, 6)}</td><td class="num">${fmt(f.fee, 6)}</td></tr>`).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--mut)">هنوز معامله‌ای ثبت نشده است</td></tr>';
}

/* ---------------- AI dashboard ---------------- */
let aiTimer = null;
async function loadAI() {
  const r = await api('/api/ai');
  if (!r.ok) return;
  $('#ai-stats').innerHTML = `
    <div class="stat-card"><b>${fmt(Math.floor(r.stats.uptime / 60))} دقیقه</b><span>آپ‌تایم</span></div>
    <div class="stat-card"><b>${fmt(r.stats.orders)}</b><span>سفارش‌ها</span></div>
    <div class="stat-card"><b>${fmt(r.stats.fills)}</b><span>معاملات</span></div>
    <div class="stat-card"><b>${fmt(r.stats.liqs)}</b><span>لیکوئیدیشن</span></div>
    <div class="stat-card"><b>${fmt(r.stats.flags)}</b><span>هشدار تقلب</span></div>
    <div class="stat-card"><b>${fmt(r.stats.users)}</b><span>کاربران</span></div>
    <div class="stat-card"><b>${fmt(r.stats.ws)}</b><span>اتصال زنده</span></div>`;
  $('#agents-grid').innerHTML = r.agents.map(a => `
    <div class="agent-card ${a.enabled ? '' : 'off'}">
      <div class="ac-head"><span class="ac-icon">${a.icon}</span><b>${a.name}</b>
        <span class="ac-status ${a.enabled ? 'on' : 'off'}">${a.enabled ? '● فعال' : '⛔ غیرفعال'}</span>
        <label class="switch"><input type="checkbox" ${a.enabled ? 'checked' : ''} onchange="toggleAgent('${a.id}', this.checked)"><span class="slider-sw"></span></label>
      </div>
      <div class="ac-role">${a.role}</div>
      <div class="ac-meta"><span>فعالیت‌ها: <b class="num">${fmt(a.actions)}</b></span><span>آخرین: <b class="num">${a.last ? tfmt(a.last) : '—'}</b></span></div>
      <div class="ac-logs">${a.logs.length ? a.logs.map(l => `<div><span class="lt">${new Date(l.t * 1000).toLocaleTimeString('en-GB')}</span> ${l.msg}</div>`).join('') : '<div style="color:var(--mut)">لاگی ثبت نشده</div>'}</div>
      ${a.id === 'bot' ? `<div class="ac-foot"><button onclick="openBotModal()">⚙️ تنظیم ربات برای حساب من</button></div>` : ''}
    </div>`).join('');
  const st = await api('/api/bot', { action: 'status' });
  if (st.ok) S.botActive = st.active;
  if (!$('#view-ai').classList.contains('hidden')) {
    clearTimeout(aiTimer); aiTimer = setTimeout(loadAI, 4000);
  }
}
window.toggleAgent = async (id, en) => {
  await api('/api/ai/toggle', { id, enabled: en });
  toast(`ایجنت «${id}» ${en ? 'فعال شد ✅' : 'غیرفعال شد ⛔'}`, en ? 'ok' : 'warn');
  loadAI();
};
window.openBotModal = async function () {
  openModal('bot');
  const st = await api('/api/bot', { action: 'status' });
  $('#bot-status').textContent = st.ok && st.active ? `🟢 ربات روی ${st.sym} فعال است` : '⚪ ربات غیرفعال است';
};

/* ---------------- chat ---------------- */
function chatAdd(text, me) {
  const d = document.createElement('div');
  d.className = 'cmsg ' + (me ? 'me' : 'bot'); d.textContent = text;
  $('#chat-box').appendChild(d); $('#chat-box').scrollTop = 1e9;
}
async function chatSend() {
  const inp = $('#chat-msg'); const msg = inp.value.trim();
  if (!msg) return; inp.value = '';
  chatAdd(msg, true);
  const r = await api('/api/chat', { msg });
  setTimeout(() => chatAdd(r.reply || '…', false), 350);
}

/* ---------------- modals ---------------- */
function openModal(id) {
  $('#modal-backdrop').classList.remove('hidden');
  $$('.modal').forEach(m => m.classList.add('hidden'));
  $('#modal-' + id).classList.remove('hidden');
}
function closeModal() { $('#modal-backdrop').classList.add('hidden'); }

/* ---------------- data loading ---------------- */
async function loadSymbolData() {
  const c = await api(`/api/candles?symbol=${encodeURIComponent(S.symbol)}&interval=${S.interval}`);
  if (c.ok) { S.candles = c.data; chart.setData(S.candles); }
  const b = await api(`/api/book?symbol=${encodeURIComponent(S.symbol)}`);
  if (b.ok) { S.book = b.data; renderBook(); }
  const t = await api(`/api/trades?symbol=${encodeURIComponent(S.symbol)}`);
  if (t.ok) { S.trades = t.data; renderTrades(); }
  const tkr = await api('/api/markets');
  if (tkr.ok) { S.tickers = tkr.data; renderSymBar(); renderTickerStrip(); }
  if (tkr.db) {
    const b = $('#db-badge');
    if (b) {
      b.textContent = '🗄 دیتابیس: ' + tkr.db.label + (tkr.db.persistent ? ' ✓' : ' ⚠️');
      b.style.color = tkr.db.persistent ? '#16a34a' : '#ef4444';
    }
  }
  if (!parseFloat($('#of-price').value) && S.tickers[S.symbol]) $('#of-price').value = S.tickers[S.symbol].last;
  setSideUI(); updateAvail();
}

/* ---------------- init ---------------- */
async function init() {
  const cfg = await api('/api/config');
  if (cfg.ok) S.cfg = cfg.data;
  if (localStorage.getItem('ax_uid')) S.uid = parseInt(localStorage.getItem('ax_uid'));
  setAuthUI();
  await tryAutoLogin();

  // nav
  $$('.navbtn').forEach(b => b.onclick = () => switchView(b.dataset.v));
  // kind & side
  $('#kind-spot').onclick = () => { S.kind = 'spot'; const s = S.symbol.replace('USD', '/USDT'); if (S.cfg[s]) { S.symbol = s; setKindUI(); loadSymbolData(); } };
  $('#kind-perp').onclick = () => { S.kind = 'perp'; const s = S.symbol.replace('/USDT', 'USD'); if (S.cfg[s]) { S.symbol = s; setKindUI(); loadSymbolData(); } };
  $('#side-buy').onclick = () => { S.side = 'buy'; setSideUI(); };
  $('#side-sell').onclick = () => { S.side = 'sell'; setSideUI(); };
  $$('.tabs-mini button').forEach(b => b.onclick = () => {
    S.otype = b.dataset.t;
    $$('.tabs-mini button').forEach(x => x.classList.toggle('active', x === b));
    $('#of-price').disabled = S.otype === 'market';
    if (S.otype === 'market' && S.tickers[S.symbol]) $('#of-price').value = S.tickers[S.symbol].last;
  });
  $$('.iv-btns button').forEach(b => b.classList.toggle('active', b.dataset.iv === S.interval));
  $$('.iv-btns button').forEach(b => b.onclick = async () => {
    S.interval = b.dataset.iv; localStorage.setItem('ax_interval', S.interval);
    $$('.iv-btns button').forEach(x => x.classList.toggle('active', x === b));
    const c = await api(`/api/candles?symbol=${encodeURIComponent(S.symbol)}&interval=${S.interval}`);
    if (c.ok) { S.candles = c.data; chart.setData(S.candles); }
  });
  // bottom tabs
  $$('.bp-tabs button[data-bp]').forEach(b => b.onclick = () => {
    $$('.bp-tabs button[data-bp]').forEach(x => x.classList.toggle('active', x === b));
    ['orders', 'positions', 'history', 'assets'].forEach(k => $('#tbl-' + k).classList.toggle('hidden', k !== b.dataset.bp));
    const rw = $('#report-wrap');
    if (rw) rw.classList.toggle('hidden', b.dataset.bp !== 'report');
    if (b.dataset.bp === 'report') loadTradeReport();
  });
  $('#btn-cancelall').onclick = async () => { await api('/api/cancelall', {}); toast('همه سفارش‌ها لغو شدند', 'ok'); refreshUser(); };
  // order form
  $('#of-qty').oninput = updateTotal; $('#of-price').oninput = updateTotal;
  $('#of-lev').oninput = () => { $('#lev-val').textContent = $('#of-lev').value + 'x'; updateTotal(); };
  $('#of-pct').oninput = () => {
    const pct = parseInt($('#of-pct').value) / 100;
    const t = S.tickers[S.symbol]; if (!t || !S.wallet) return;
    let q = 0;
    if (S.kind === 'spot') {
      if (S.side === 'buy') q = (S.wallet.balances.USDT - (S.wallet.locks.USDT || 0)) * pct / t.last;
      else q = ((S.wallet.balances[S.cfg[S.symbol].base] || 0) - (S.wallet.locks[S.cfg[S.symbol].base] || 0)) * pct;
    } else {
      q = S.wallet.free_margin * pct * (parseInt($('#of-lev').value) || 10) / t.last;
    }
    const step = S.cfg[S.symbol].step;
    $('#of-qty').value = Math.max(0, Math.floor(q / step) * step).toFixed(8).replace(/\.?0+$/, '');
    updateTotal();
  };
  $('#of-submit').onclick = submitOrder;
  // auth
  $('#btn-auth').onclick = () => openModal('auth');
  $('#btn-api').onclick = () => { if (!S.token) return openModal('auth'); openModal('api'); };
  $('#api-create').onclick = async () => {
    const r = await api('/api/api-keys/create', {label: $('#api-label').value || 'Trading bot'});
    if (!r.ok) return toast(r.error || 'ساخت کلید ناموفق بود', 'err');
    $('#api-result').innerHTML = `<b>API Key:</b><code>${r.key}</code><br><b>Secret:</b><code>${r.secret}</code><br>اکنون در جای امن ذخیره کنید؛ Secret دوباره نمایش داده نمی‌شود.<br><small style="color:var(--mut)">احراز هویت به سبک Bybit v5 (هدرهای X-BAPI-*) — نمونه پایتون: <code>scripts/ws_smoke_test.py</code> و مستندات: <code>API_REFERENCE.md</code></small>`;
  };
  $('#tab-login').onclick = () => { $('#tab-login').classList.add('active'); $('#tab-register').classList.remove('active'); $('#name-field').classList.add('hidden'); $('#auth-submit').textContent = 'ورود'; };
  $('#tab-register').onclick = () => { $('#tab-register').classList.add('active'); $('#tab-login').classList.remove('active'); $('#name-field').classList.remove('hidden'); $('#auth-submit').textContent = 'ثبت‌نام'; };
  $('#auth-submit').onclick = async () => {
    const reg = $('#tab-register').classList.contains('active');
    let r = await api('/api/auth/' + (reg ? 'register' : 'login'), {
      email: $('#auth-email').value, password: $('#auth-pass').value, name: $('#auth-name').value
    });
    if (!r.ok && r.need_otp) {
      const otp = prompt('حساب شما با تأیید دومرحله‌ای محافظت شده است.\nکد ۶ رقمی اپلیکیشن احراز هویت را وارد کنید:');
      if (!otp) return toast('کد 2FA لازم است', 'err');
      r = await api('/api/auth/login', {
        email: $('#auth-email').value, password: $('#auth-pass').value, otp
      });
    }
    if (r.ok) {
      S.token = r.token; S.uid = r.uid;
      localStorage.setItem('ax_token', r.token); localStorage.setItem('ax_uid', r.uid);
      setAuthUI(); closeModal(); sub('user');
      toast(reg ? '🎁 خوش آمدید! ۲۰,۰۰۰ USDT هدیه گرفتید' : 'خوش آمدید 👋', 'ok');
      refreshUser();
    } else toast(r.error, 'err');
  };
  $$('.m-close').forEach(b => b.onclick = closeModal);
  $('#modal-backdrop').onclick = e => { if (e.target.id === 'modal-backdrop') closeModal(); };
  // faucet
  const faucet = async () => {
    if (!S.token) { openModal('auth'); return; }
    const r = await api('/api/faucet', {});
    if (r.ok) toast('💧 ۱۰,۰۰۰ USDT تستی دریافت شد!', 'ok'); else toast(r.error, 'warn');
    refreshUser();
  };
  $('#btn-faucet').onclick = faucet; $('#btn-faucet2').onclick = faucet;
  // deposit / withdraw
  function txModal(mode) {
    if (!S.token) { openModal('auth'); return; }
    openModal('tx');
    $('#tx-title').textContent = mode === 'deposit' ? 'واریز آزمایشی' : 'برداشت آزمایشی';
    $('#tx-addr-wrap').classList.toggle('hidden', mode === 'deposit');
    $('#tx-note').textContent = mode === 'deposit' ? 'واریز به‌صورت شبیه‌سازی‌شده فوراً تأیید می‌شود.' : 'برداشت در تست‌نت شبیه‌سازی است؛ مبلغ بلافاصله کسر می‌شود.';
    $('#tx-asset').innerHTML = ASSET_LIST.map(a => `<option>${a}</option>`).join('');
    $('#tx-submit').onclick = async () => {
      const body = { asset: $('#tx-asset').value, amount: parseFloat($('#tx-amount').value), address: $('#tx-addr').value };
      const r = await api('/api/wallet/' + mode, body);
      if (r.ok) { toast(`✅ ${mode === 'deposit' ? 'واریز' : 'برداشت'} انجام شد — txid: ${r.txid.slice(0, 16)}…`, 'ok'); closeModal(); loadWallet(); }
      else toast(r.error, 'err');
    };
  }
  $('#btn-deposit').onclick = () => txModal('deposit');
  $('#btn-withdraw').onclick = () => txModal('withdraw');
  // bot modal
  $('#bot-lev').oninput = () => $('#bot-lev-val').textContent = $('#bot-lev').value + 'x';
  $('#bot-start').onclick = async () => {
    if (!S.token) { openModal('auth'); return; }
    const r = await api('/api/bot', { action: 'start', symbol: $('#bot-sym').value, lev: parseInt($('#bot-lev').value) });
    if (r.ok) { toast('🤖 ربات معامله‌گر فعال شد', 'ok'); $('#bot-status').textContent = '🟢 ربات فعال است'; } else toast(r.error, 'err');
  };
  $('#bot-stop').onclick = async () => {
    await api('/api/bot', { action: 'stop' });
    toast('ربات متوقف شد', 'warn'); $('#bot-status').textContent = '⚪ ربات غیرفعال است';
  };
  // chat
  $('#chat-send').onclick = chatSend;
  $('#chat-msg').onkeydown = e => { if (e.key === 'Enter') chatSend(); };
  chatAdd('سلام! 👋 من پشتیبان هوشمند آریاکس هستم. درباره کارمزد، فاست، اهرم، لیکوئیدیشن یا ایجنت‌ها بپرسید.', false);

  setKindUI();
  wsConnect();
  await loadSymbolData();
  if (S.token) refreshUser();
  setInterval(() => { if (S.token) refreshUser(); }, 15000);
  setInterval(async () => {  // تازه‌سازی کندل‌ها برای تایم‌فریم‌های بزرگ‌تر از ۱ دقیقه
    if (S.interval !== '1m' && !$('#view-trade').classList.contains('hidden')) {
      const c = await api(`/api/candles?symbol=${encodeURIComponent(S.symbol)}&interval=${S.interval}`);
      if (c.ok) { S.candles = c.data; chart.setData(S.candles); }
    }
  }, 20000);
}
init();

// v2.2: structured trade report (bot debugging)
const REASON_FA = { StopLoss: 'حد ضرر', TakeProfit: 'حد سود', TrailingStop: 'دنبال‌کننده',
  Liquidation: 'لیکوئید', manual: 'دستی/ربات', Conditional: 'شرطی' };
async function loadTradeReport() {
  if (!S.token) { $('#report-chips').innerHTML = '<span style="color:var(--mut)">برای مشاهده گزارش وارد شوید</span>'; return; }
  const r = await api('/api/trade-report?limit=100');
  if (!r.ok) return;
  const chip = (t, v, cls) => `<span style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:4px 10px;font-size:.8rem">${t}: <b class="${cls||''} num">${v}</b></span>`;
  const s = r.summary || {};
  $('#report-chips').innerHTML =
    chip('تریدها', s.trades || 0) +
    chip('نرخ برد', (s.winrate || 0) + '%', (s.winrate >= 50 ? 'g' : 'r')) +
    chip('خالص', (s.net >= 0 ? '+' : '') + fmt(s.net, 4) + '$', s.net >= 0 ? 'g' : 'r') +
    chip('کارمزد', fmt(s.fees, 4) + '$') +
    chip('فاندینگ', fmt(s.funding, 4) + '$', (s.funding <= 0 ? 'r' : 'g')) +
    chip('میانگین نگهداری', (s.avg_hold_min || 0) + 'د') +
    chip('بدترین', fmt(s.worst, 3) + '$', 'r') + chip('بهترین', fmt(s.best, 3) + '$', 'g');
  $('#report-chips').innerHTML += `<button class="mini ghost" onclick="downloadTradesCSV()" style="cursor:pointer">⬇️ دانلود CSV</button>`;
  $('#tbl-report tbody').innerHTML = (r.data || []).map(t => {
    const dt = new Date((t.ts || 0) * 1000).toLocaleString('fa-IR', { hour12: false });
    return `<tr><td class="num" style="font-size:.75rem">${dt}</td><td><b>${t.symbol}</b></td>
      <td class="${t.side === 'long' ? 'g' : 'r'}">${t.side === 'long' ? 'لانگ' : 'شورت'}${t.partial ? ' <small>(جزئی)</small>' : ''}</td>
      <td class="num">${fmt(t.qty, 6)}</td><td class="num">${fmt(t.entry, 6)}</td><td class="num">${fmt(t.exit, 6)}</td>
      <td class="num">${fmt(t.fees, 4)}</td><td class="num ${t.funding <= 0 ? 'r' : 'g'}">${fmt(t.funding, 4)}</td>
      <td class="num ${t.net >= 0 ? 'g' : 'r'}"><b>${fmt(t.net, 4)}</b></td>
      <td class="num">${fmt(t.hold_min, 0)}د</td>
      <td>${REASON_FA[t.reason] || t.reason}</td>
      <td style="font-size:.75rem">${t.strategy || '—'}</td></tr>`;
  }).join('') || '<tr><td colspan="12" style="text-align:center;color:var(--mut)">هنوز معامله بسته‌شده‌ای ندارید</td></tr>';
}

window.downloadTradesCSV = async () => {
  const r = await fetch('/api/trade-report?csv=1&limit=300', {
    headers: { 'Authorization': 'Bearer ' + S.token } });
  if (!r.ok) return toast('دانلود CSV ناموفق بود', 'err');
  const blob = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ariax_trades.csv';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('📥 araix_trades.csv دانلود شد', 'ok');
};
