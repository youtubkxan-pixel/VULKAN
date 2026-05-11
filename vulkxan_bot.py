"""
VULKXAN FOREX SIGNAL BOT v3 - Soddalashtirilgan
@vulkxan_bot — EUR/USD Telegram Signals
"""

import asyncio
import logging
import sqlite3
import os
import sys
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode


# ─── CONFIG ───────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8799555582:AAETKBrTm6IxXmwkB55YxHA71B1CLjsQIC0")
OWNER_CHAT_ID = int(os.getenv("CHAT_ID", "7837984652"))
RENDER_URL = os.getenv("RENDER_URL", "")
PORT = int(os.getenv("PORT", "10000"))

SYMBOL = "EURUSD=X"
PIP = 0.0001
MIN_SCORE = 75
SESSION_START = 13
SESSION_END = 17
ATR_MIN = 5.0
ATR_MAX = 30.0
SL_MULT = 1.0
TP_MULT = 3.0
EARLY_EXIT = 2.0
SL_BUFFER = 2.0
LOOP_SEC = 60
COOLDOWN_MIN = 30
SELF_PING_MIN = 14
ACTIVE_CAPITAL = 1000.0
DB_PATH = "vulkxan.db"


# ─── LOGGER ───────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
log = logging.getLogger("Vulkxan")


# ─── DATABASE ─────────────────────────────────────────────

def db_conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    with db_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT, entry REAL, sl REAL, tp REAL,
            sl_pips REAL, tp_pips REAL, lot REAL,
            score INTEGER, confirms TEXT,
            status TEXT DEFAULT 'OPEN',
            open_time TEXT, close_time TEXT,
            close_price REAL, pnl_pips REAL DEFAULT 0,
            pnl_usd REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY, val TEXT
        );
        """)
        for k, v in [("running", "0"), ("active", str(ACTIVE_CAPITAL)), ("last_signal", "")]:
            c.execute("INSERT OR IGNORE INTO kv VALUES(?,?)", (k, v))


def db_get(k, default=""):
    try:
        with db_conn() as c:
            r = c.execute("SELECT val FROM kv WHERE key=?", (k,)).fetchone()
            return r["val"] if r else default
    except Exception as e:
        log.warning(f"db_get: {e}")
        return default


def db_set(k, v):
    try:
        with db_conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
    except Exception as e:
        log.warning(f"db_set: {e}")


def db_save_trade(d) -> int:
    try:
        with db_conn() as c:
            cur = c.execute("""
                INSERT INTO trades (direction,entry,sl,tp,sl_pips,tp_pips,lot,score,confirms,status,open_time)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (d["direction"], d["entry"], d["sl"], d["tp"],
                  d["sl_pips"], d["tp_pips"], d["lot"], d["score"],
                  d["confirms"], "OPEN", datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid
    except Exception as e:
        log.error(f"db_save_trade: {e}")
        return -1


def db_close_trade(tid, status, price, pips, usd):
    try:
        with db_conn() as c:
            c.execute("""
                UPDATE trades SET status=?,close_time=?,close_price=?,pnl_pips=?,pnl_usd=? WHERE id=?
            """, (status, datetime.now(timezone.utc).isoformat(), price, pips, usd, tid))
    except Exception as e:
        log.error(f"db_close_trade: {e}")


def db_open_trades():
    try:
        with db_conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM trades WHERE status='OPEN'"
            ).fetchall()]
    except Exception as e:
        log.error(f"db_open_trades: {e}")
        return []


def db_stats():
    try:
        with db_conn() as c:
            total = c.execute("SELECT COUNT(*) FROM trades WHERE status!='OPEN'").fetchone()[0]
            wins = c.execute("SELECT COUNT(*) FROM trades WHERE pnl_usd>0").fetchone()[0]
            losses = c.execute("SELECT COUNT(*) FROM trades WHERE pnl_usd<0").fetchone()[0]
            pnl = c.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM trades").fetchone()[0]
            pips = c.execute("SELECT COALESCE(SUM(pnl_pips),0) FROM trades").fetchone()[0]
            opens = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
            wr = wins / total * 100 if total > 0 else 0
            return dict(total=total, wins=wins, losses=losses, pnl=pnl, pips=pips, opens=opens, wr=wr)
    except Exception as e:
        log.error(f"db_stats: {e}")
        return dict(total=0, wins=0, losses=0, pnl=0, pips=0, opens=0, wr=0)


# ─── MARKET DATA (urllib — built-in) ─────────────────────

async def fetch_yahoo(symbol: str, interval: str) -> Optional[Dict]:
    """Yahoo Finance dan OHLC ma'lumotini oladi."""
    try:
        period = "5d" if interval in ("1m", "5m", "15m") else "60d"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval={interval}&range={period}"

        def _fetch():
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _fetch)
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        q = result[0].get("indicators", {}).get("quote", [{}])[0]
        opens = q.get("open", [])
        highs = q.get("high", [])
        lows = q.get("low", [])
        closes = q.get("close", [])

        valid = [(o, h, l, c) for o, h, l, c in zip(opens, highs, lows, closes)
                 if all(x is not None for x in (o, h, l, c))]
        if len(valid) < 50:
            return None

        valid = valid[-200:]
        return {
            "open":  [x[0] for x in valid],
            "high":  [x[1] for x in valid],
            "low":   [x[2] for x in valid],
            "close": [x[3] for x in valid],
        }
    except Exception as e:
        log.warning(f"fetch_yahoo {interval}: {e}")
        return None


async def get_current_price() -> Optional[Tuple[float, float]]:
    data = await fetch_yahoo(SYMBOL, "1m")
    if not data or not data["close"]:
        return None
    mid = data["close"][-1]
    spread = PIP * 0.5
    return (mid - spread / 2, mid + spread / 2)


# ─── INDICATORS ──────────────────────────────────────────

def calc_atr(high, low, close, period=14):
    if len(close) < period + 1:
        return None
    trs = []
    for i in range(1, len(close)):
        tr = max(high[i] - low[i],
                 abs(high[i] - close[i-1]),
                 abs(low[i] - close[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def calc_rsi(close, period=14):
    if len(close) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(close)):
        d = close[i] - close[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return None
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def calc_ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for v in data[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_macd_hist(close):
    if len(close) < 35:
        return None, None
    ef = calc_ema(close, 12)
    es = calc_ema(close, 26)
    if ef is None or es is None:
        return None, None
    macd = ef - es

    # Previous bar
    ef_p = calc_ema(close[:-1], 12)
    es_p = calc_ema(close[:-1], 26)
    if ef_p is None or es_p is None:
        return macd, macd
    macd_p = ef_p - es_p

    return macd, macd_p


def calc_sma(data, period):
    if len(data) < period:
        return None
    return sum(data[-period:]) / period


def find_swings(high, low, lookback=5):
    sh, sl = [], []
    for i in range(lookback, len(high) - lookback):
        if high[i] == max(high[i-lookback:i+lookback+1]):
            sh.append(i)
        if low[i] == min(low[i-lookback:i+lookback+1]):
            sl.append(i)
    return sh, sl


def is_bull_pin(o, h, l, c):
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    return lower >= 2 * body and lower / rng >= 0.6 and upper / rng <= 0.2


def is_bear_pin(o, h, l, c):
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    return upper >= 2 * body and upper / rng >= 0.6 and lower / rng <= 0.2


def is_bull_engulf(po, pc, co, cc):
    return pc < po and cc > co and cc > po and co < pc


def is_bear_engulf(po, pc, co, cc):
    return pc > po and cc < co and cc < po and co > pc


# ─── TREND & SIGNAL ──────────────────────────────────────

def detect_trend(ohlc):
    sh, sl = find_swings(ohlc["high"], ohlc["low"], 5)
    if len(sh) < 2 or len(sl) < 2:
        return "RANGING"
    h, l = ohlc["high"], ohlc["low"]
    if h[sh[-1]] > h[sh[-2]] and l[sl[-1]] > l[sl[-2]]:
        return "UPTREND"
    if h[sh[-1]] < h[sh[-2]] and l[sl[-1]] < l[sl[-2]]:
        return "DOWNTREND"
    return "RANGING"


def get_confirms_buy(ohlc):
    conf = []
    c, h, l, o = ohlc["close"], ohlc["high"], ohlc["low"], ohlc["open"]
    rsi = calc_rsi(c)
    if rsi is not None and rsi < 35:
        conf.append("RSI_OVERSOLD")
    macd, macd_p = calc_macd_hist(c)
    if macd and macd_p is not None:
        if macd_p < 0 < macd:
            conf.append("MACD_BULL_CROSS")
        elif macd > 0 and macd > macd_p:
            conf.append("MACD_BULL_MOM")
    if len(c) >= 2:
        if is_bull_pin(o[-1], h[-1], l[-1], c[-1]):
            conf.append("BULL_PIN")
        if is_bull_engulf(o[-2], c[-2], o[-1], c[-1]):
            conf.append("BULL_ENGULF")
    ma50, ma200 = calc_sma(c, 50), calc_sma(c, 200)
    if ma50 and ma200 and ma50 > ma200:
        conf.append("MA_BULLISH")
    return conf


def get_confirms_sell(ohlc):
    conf = []
    c, h, l, o = ohlc["close"], ohlc["high"], ohlc["low"], ohlc["open"]
    rsi = calc_rsi(c)
    if rsi is not None and rsi > 65:
        conf.append("RSI_OVERBOUGHT")
    macd, macd_p = calc_macd_hist(c)
    if macd and macd_p is not None:
        if macd_p > 0 > macd:
            conf.append("MACD_BEAR_CROSS")
        elif macd < 0 and macd < macd_p:
            conf.append("MACD_BEAR_MOM")
    if len(c) >= 2:
        if is_bear_pin(o[-1], h[-1], l[-1], c[-1]):
            conf.append("BEAR_PIN")
        if is_bear_engulf(o[-2], c[-2], o[-1], c[-1]):
            conf.append("BEAR_ENGULF")
    ma50, ma200 = calc_sma(c, 50), calc_sma(c, 200)
    if ma50 and ma200 and ma50 < ma200:
        conf.append("MA_BEARISH")
    return conf


def calc_score(ohlc, direction, trend, conf, atr_pips):
    s = 0
    if (direction == "BUY" and trend == "UPTREND") or \
       (direction == "SELL" and trend == "DOWNTREND"):
        s += 25
    else:
        return 0
    n = len(conf)
    if n >= 4: s += 30
    elif n == 3: s += 22
    elif n == 2: s += 15
    else: return 0

    rsi = calc_rsi(ohlc["close"])
    if rsi:
        if direction == "BUY":
            if rsi < 35: s += 15
            elif rsi < 50: s += 10
            elif rsi < 60: s += 5
        else:
            if rsi > 65: s += 15
            elif rsi > 50: s += 10
            elif rsi > 40: s += 5

    macd, macd_p = calc_macd_hist(ohlc["close"])
    if macd is not None and macd_p is not None:
        if direction == "BUY":
            if macd > 0 and macd > macd_p: s += 15
            elif macd > 0: s += 10
            elif macd > macd_p: s += 5
        else:
            if macd < 0 and macd < macd_p: s += 15
            elif macd < 0: s += 10
            elif macd < macd_p: s += 5

    if 8 <= atr_pips <= 20: s += 10
    elif 5 <= atr_pips < 8 or 20 < atr_pips <= 25: s += 5

    h = datetime.now(timezone.utc).hour
    if 13 <= h < 15: s += 5
    elif 15 <= h < 17: s += 3

    return min(s, 100)


def calc_levels(entry, atr, direction):
    buf = SL_BUFFER * PIP
    early = EARLY_EXIT * PIP
    if direction == "BUY":
        sl = round(entry - atr * SL_MULT - buf, 5)
        tp = round(entry + atr * TP_MULT - early, 5)
    else:
        sl = round(entry + atr * SL_MULT + buf, 5)
        tp = round(entry - atr * TP_MULT + early, 5)
    return sl, tp, abs(entry - sl) / PIP, abs(tp - entry) / PIP


def calc_lot(active, sl_pips):
    if sl_pips <= 0:
        return 0.01
    risk = active * 0.01
    lot = risk / (sl_pips * 10)
    return max(0.01, min(round(lot, 2), 5.0))


async def generate_signal():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return None
    if not (SESSION_START <= now.hour < SESSION_END):
        return None
    if now.weekday() == 4 and now.day <= 7 and 11 <= now.hour <= 14:
        return None

    last = db_get("last_signal")
    if last:
        try:
            ld = datetime.fromisoformat(last)
            if (now - ld).total_seconds() / 60 < COOLDOWN_MIN:
                return None
        except: pass

    h1 = await fetch_yahoo(SYMBOL, "1h")
    m5 = await fetch_yahoo(SYMBOL, "5m")
    if not h1 or not m5:
        log.warning("Ma'lumot olishda muammo")
        return None

    atr = calc_atr(m5["high"], m5["low"], m5["close"])
    if atr is None:
        return None
    atr_pips = atr / PIP
    if not (ATR_MIN <= atr_pips <= ATR_MAX):
        return None

    trend = detect_trend(h1)
    if trend == "RANGING":
        return None

    if trend == "UPTREND":
        conf = get_confirms_buy(m5)
        direction = "BUY"
    else:
        conf = get_confirms_sell(m5)
        direction = "SELL"

    if len(conf) < 2:
        return None

    sc = calc_score(m5, direction, trend, conf, atr_pips)
    if sc < MIN_SCORE:
        return None

    price = await get_current_price()
    if not price:
        return None
    bid, ask = price
    entry = ask if direction == "BUY" else bid

    sl, tp, sl_pips, tp_pips = calc_levels(entry, atr, direction)
    active = float(db_get("active", str(ACTIVE_CAPITAL)))
    lot = calc_lot(active, sl_pips)
    rsi = calc_rsi(m5["close"]) or 50.0

    return dict(direction=direction, score=sc, trend=trend,
                entry=entry, sl=sl, tp=tp,
                sl_pips=round(sl_pips, 1), tp_pips=round(tp_pips, 1),
                lot=lot, rsi=round(rsi, 1), atr_pips=round(atr_pips, 1),
                confirms=",".join(conf), time=now.strftime("%H:%M"))


# ─── FORMATTING ──────────────────────────────────────────

def fmt_signal(s):
    em = "🟢" if s["direction"] == "BUY" else "🔴"
    ar = "📈" if s["direction"] == "BUY" else "📉"
    rr = round(s["tp_pips"] / s["sl_pips"], 2) if s["sl_pips"] > 0 else 0
    return (
        f"{em} <b>{s['direction']} SIGNAL — EUR/USD</b> {ar}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💯 <b>Score:</b> {s['score']}/100\n"
        f"📊 <b>Trend:</b> {s['trend']}\n"
        f"✅ <b>Confirms:</b> {s['confirms'].replace(',', ', ')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: <code>{s['entry']:.5f}</code>\n"
        f"🛡 SL:    <code>{s['sl']:.5f}</code> ({s['sl_pips']} pip)\n"
        f"🎯 TP:    <code>{s['tp']:.5f}</code> ({s['tp_pips']} pip)\n"
        f"⚖️ RR:    1 : {rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Lot: {s['lot']} | RSI: {s['rsi']} | ATR: {s['atr_pips']}p\n"
        f"⏰ {s['time']} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>MT5 ilovangizda shu darajalarda savdo oching</i>"
    )


def fmt_close(t, price, pips, usd, status):
    em = "✅" if status == "CLOSED_TP" else "❌"
    title = "TP HIT — FOYDA" if status == "CLOSED_TP" else "SL HIT — ZARAR"
    return (
        f"{em} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Trade #{t['id']} | {t['direction']}\n"
        f"📍 Entry: <code>{t['entry']:.5f}</code>\n"
        f"🚪 Exit:  <code>{price:.5f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>P/L: {pips:+.1f} pip | ${usd:+.2f}</b>"
    )


# ─── KEYBOARD ────────────────────────────────────────────

def make_kb():
    running = db_get("running") == "1"
    row1 = [InlineKeyboardButton("⏸ STOP", callback_data="stop")] if running \
           else [InlineKeyboardButton("▶️ START", callback_data="start")]
    return InlineKeyboardMarkup([
        row1,
        [InlineKeyboardButton("📊 STATS", callback_data="stats"),
         InlineKeyboardButton("📋 OPEN", callback_data="open")],
    ])


# ─── TELEGRAM HANDLERS ───────────────────────────────────

async def cmd_start(update, ctx):
    if update.effective_user.id != OWNER_CHAT_ID:
        await update.message.reply_text(
            f"⛔ Faqat egasi uchun.\nID: <code>{update.effective_user.id}</code>",
            parse_mode=ParseMode.HTML
        )
        return
    running = db_get("running") == "1"
    status = "🟢 ISHLAMOQDA" if running else "🔴 TO'XTAGAN"
    await update.message.reply_text(
        f"🎯 <b>VULKXAN BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 EUR/USD\n"
        f"💯 Min Score: {MIN_SCORE}/100\n"
        f"⏰ {SESSION_START}:00–{SESSION_END}:00 UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Holat: {status}",
        reply_markup=make_kb(),
        parse_mode=ParseMode.HTML
    )


async def cb_handler(update, ctx):
    q = update.callback_query
    if q.from_user.id != OWNER_CHAT_ID:
        await q.answer("⛔", show_alert=True)
        return
    await q.answer()
    a = q.data

    if a == "start":
        db_set("running", "1")
        await q.edit_message_text(
            "🟢 <b>BOT ISHGA TUSHDI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Bozorni kuzatish boshlandi\n"
            f"📊 Min score: {MIN_SCORE}/100\n"
            f"⏰ {SESSION_START}:00–{SESSION_END}:00 UTC\n"
            "🔔 Kuchli signal topilsa xabar yuboraman",
            reply_markup=make_kb(), parse_mode=ParseMode.HTML
        )

    elif a == "stop":
        db_set("running", "0")
        await q.edit_message_text(
            "🔴 <b>BOT TO'XTATILDI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▶️ Qayta yoqish uchun START bosing",
            reply_markup=make_kb(), parse_mode=ParseMode.HTML
        )

    elif a == "stats":
        s = db_stats()
        active = float(db_get("active", str(ACTIVE_CAPITAL)))
        await q.edit_message_text(
            f"📊 <b>STATISTIKA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Jami: {s['total']}\n"
            f"✅ G'alaba: {s['wins']}\n"
            f"❌ Mag'lubiyat: {s['losses']}\n"
            f"🎯 Win Rate: {s['wr']:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 P/L: ${s['pnl']:+.2f}\n"
            f"📏 Pips: {s['pips']:+.1f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 Aktiv: ${active:.2f}\n"
            f"📋 Ochiq: {s['opens']}",
            reply_markup=make_kb(), parse_mode=ParseMode.HTML
        )

    elif a == "open":
        trades = db_open_trades()
        if not trades:
            msg = "📋 Ochiq savdolar yo'q"
        else:
            lines = [f"📋 <b>OCHIQ ({len(trades)})</b>", ""]
            for t in trades:
                em = "🟢" if t["direction"] == "BUY" else "🔴"
                lines.append(
                    f"{em} #{t['id']} {t['direction']}\n"
                    f"   Entry: <code>{t['entry']:.5f}</code>\n"
                    f"   SL: <code>{t['sl']:.5f}</code>\n"
                    f"   TP: <code>{t['tp']:.5f}</code>"
                )
            msg = "\n".join(lines)
        await q.edit_message_text(msg, reply_markup=make_kb(), parse_mode=ParseMode.HTML)


# ─── BACKGROUND TASKS ────────────────────────────────────

async def monitor_loop(bot):
    log.info("📡 Monitor loop boshlandi")
    while True:
        try:
            if db_get("running") == "1":
                # Check open trades
                trades = db_open_trades()
                if trades:
                    price = await get_current_price()
                    if price:
                        mid = (price[0] + price[1]) / 2
                        for t in trades:
                            try:
                                d = t["direction"]
                                status, exit_p = None, mid
                                if d == "BUY":
                                    if mid >= t["tp"]:
                                        status, exit_p = "CLOSED_TP", t["tp"]
                                    elif mid <= t["sl"]:
                                        status, exit_p = "CLOSED_SL", t["sl"]
                                else:
                                    if mid <= t["tp"]:
                                        status, exit_p = "CLOSED_TP", t["tp"]
                                    elif mid >= t["sl"]:
                                        status, exit_p = "CLOSED_SL", t["sl"]
                                if status:
                                    pips = (exit_p - t["entry"]) / PIP if d == "BUY" \
                                        else (t["entry"] - exit_p) / PIP
                                    usd = pips * t["lot"] * 10
                                    db_close_trade(t["id"], status, exit_p, pips, usd)
                                    active = float(db_get("active", str(ACTIVE_CAPITAL)))
                                    db_set("active", active + usd)
                                    msg = fmt_close(t, exit_p, pips, usd, status)
                                    await bot.send_message(OWNER_CHAT_ID, msg, parse_mode=ParseMode.HTML)
                                    log.info(f"Trade #{t['id']} {status} {pips:+.1f}pip ${usd:+.2f}")
                            except Exception as e:
                                log.error(f"Check trade: {e}")

                # Generate new signal
                if len(db_open_trades()) < 3:
                    sig = await generate_signal()
                    if sig:
                        try:
                            await bot.send_message(OWNER_CHAT_ID, fmt_signal(sig), parse_mode=ParseMode.HTML)
                            tid = db_save_trade(sig)
                            db_set("last_signal", datetime.now(timezone.utc).isoformat())
                            log.info(f"📨 Signal #{tid} {sig['direction']} score:{sig['score']}")
                        except Exception as e:
                            log.error(f"Send signal: {e}")

            await asyncio.sleep(LOOP_SEC)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Monitor: {e}")
            await asyncio.sleep(30)


async def self_ping_loop():
    if not RENDER_URL:
        log.info("⚠️ RENDER_URL yo'q, self-ping ishlamaydi")
        return
    log.info(f"🏓 Self-ping har {SELF_PING_MIN} daqiqada")
    while True:
        try:
            await asyncio.sleep(SELF_PING_MIN * 60)
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{RENDER_URL}/health",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    log.info(f"🏓 Ping: {r.status}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Ping: {e}")


async def health_server():
    from aiohttp import web

    async def handler(req):
        return web.Response(
            text=json.dumps({
                "status": "ok",
                "running": db_get("running") == "1",
                "time": datetime.now(timezone.utc).isoformat(),
            }),
            content_type="application/json"
        )

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/health", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ Health server port {PORT}")
    return runner


# ─── MAIN ────────────────────────────────────────────────

async def main():
    db_init()

    log.info("═" * 50)
    log.info("  🎯 VULKXAN BOT v3")
    log.info(f"  📊 EUR/USD | Score >= {MIN_SCORE}")
    log.info(f"  ⏰ {SESSION_START}:00-{SESSION_END}:00 UTC")
    log.info("═" * 50)

    runner = await health_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))

    await app.initialize()
    await app.start()

    # Welcome message
    try:
        await app.bot.send_message(
            OWNER_CHAT_ID,
            "🎯 <b>VULKXAN BOT ONLAYN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Server ishlamoqda\n"
            "👇 /start bosing",
            parse_mode=ParseMode.HTML
        )
        log.info("✅ Welcome yuborildi")
    except Exception as e:
        log.warning(f"Welcome: {e}")

    # Start polling
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    log.info("✅ Polling boshlandi")

    # Background tasks
    monitor_task = asyncio.create_task(monitor_loop(app.bot))
    ping_task = asyncio.create_task(self_ping_loop())

    log.info("✅ Bot to'liq ishlamoqda")

    try:
        # Keep alive forever
        await asyncio.Event().wait()
    finally:
        monitor_task.cancel()
        ping_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("👋 Bot to'xtadi")
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
