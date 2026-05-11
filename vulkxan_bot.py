"""
╔══════════════════════════════════════════════════════════════════╗
║         🎯  VULKXAN FOREX SIGNAL BOT v2.0                       ║
║         @vulkxan_bot — EUR/USD Smart Signals                    ║
║         ✅ Pandas yo'q (Render Free bilan mos)                  ║
║         ✅ O'z-o'zini uyg'otadi (15 daqiqada bir)               ║
║         ✅ Faqat siz ishlatа olasiz                              ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import sqlite3
import os
import sys
import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass

import aiohttp
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ApplicationBuilder,
)
from telegram.constants import ParseMode

# ══════════════════════════════════════════════
#              CONFIGURATION
# ══════════════════════════════════════════════

class Config:
    TELEGRAM_TOKEN: str = os.getenv(
        "TELEGRAM_TOKEN",
        "8799555582:AAETKBrTm6IxXmwkB55YxHA71B1CLjsQIC0"
    )
    OWNER_CHAT_ID: int = int(os.getenv("CHAT_ID", "7837984652"))
    RENDER_URL: str = os.getenv("RENDER_URL", "")  # Deploy bo'lgach to'ldiriladi

    SYMBOL: str = "EURUSD=X"
    DISPLAY: str = "EUR/USD"
    PIP: float = 0.0001

    # Kapital
    ACTIVE_CAPITAL: float = 1000.0
    RESERVE_CAPITAL: float = 9000.0

    # Strategiya
    MIN_SCORE: int = 75
    SESSION_START: int = 13   # UTC
    SESSION_END: int = 17     # UTC
    ATR_MIN: float = 5.0
    ATR_MAX: float = 30.0
    SL_MULT: float = 1.0
    TP_MULT: float = 3.0
    EARLY_EXIT: float = 2.0
    SL_BUFFER: float = 2.0

    # Timing
    LOOP_SEC: int = 60
    COOLDOWN_MIN: int = 30
    SELF_PING_MIN: int = 14   # Har 14 daqiqada o'zini ping qiladi

    DB: str = "vulkxan.db"
    PORT: int = int(os.getenv("PORT", "10000"))


# ══════════════════════════════════════════════
#              LOGGER
# ══════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("VulkxanBot")


# ══════════════════════════════════════════════
#              DATABASE
# ══════════════════════════════════════════════

class DB:
    def __init__(self):
        self.path = Config.DB
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                sl_pips REAL,
                tp_pips REAL,
                lot REAL,
                score INTEGER,
                confirms TEXT,
                status TEXT DEFAULT 'OPEN',
                open_time TEXT,
                close_time TEXT,
                close_price REAL,
                pnl_pips REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                val TEXT
            );
            """)
            for k, v in [
                ("running", "0"),
                ("active", str(Config.ACTIVE_CAPITAL)),
                ("last_signal", ""),
            ]:
                c.execute("INSERT OR IGNORE INTO kv VALUES(?,?)", (k, v))

    def get(self, k, default=""):
        try:
            with self._conn() as c:
                r = c.execute("SELECT val FROM kv WHERE key=?", (k,)).fetchone()
                return r["val"] if r else default
        except:
            return default

    def set(self, k, v):
        try:
            with self._conn() as c:
                c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v)))
        except:
            pass

    def save_trade(self, d) -> int:
        try:
            with self._conn() as c:
                cur = c.execute("""
                    INSERT INTO trades
                    (direction,entry,sl,tp,sl_pips,tp_pips,lot,score,confirms,status,open_time)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    d["direction"], d["entry"], d["sl"], d["tp"],
                    d["sl_pips"], d["tp_pips"], d["lot"], d["score"],
                    d["confirms"], "OPEN",
                    datetime.now(timezone.utc).isoformat()
                ))
                return cur.lastrowid
        except Exception as e:
            log.error(f"Save trade: {e}")
            return -1

    def close_trade(self, tid, status, price, pips, usd):
        try:
            with self._conn() as c:
                c.execute("""
                    UPDATE trades SET status=?,close_time=?,close_price=?,
                    pnl_pips=?,pnl_usd=? WHERE id=?
                """, (status, datetime.now(timezone.utc).isoformat(),
                      price, pips, usd, tid))
        except Exception as e:
            log.error(f"Close trade: {e}")

    def open_trades(self):
        try:
            with self._conn() as c:
                return [dict(r) for r in c.execute(
                    "SELECT * FROM trades WHERE status='OPEN'"
                ).fetchall()]
        except:
            return []

    def stats(self):
        try:
            with self._conn() as c:
                total = c.execute("SELECT COUNT(*) FROM trades WHERE status!='OPEN'").fetchone()[0]
                wins = c.execute("SELECT COUNT(*) FROM trades WHERE pnl_usd>0").fetchone()[0]
                losses = c.execute("SELECT COUNT(*) FROM trades WHERE pnl_usd<0").fetchone()[0]
                pnl = c.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM trades").fetchone()[0]
                pips = c.execute("SELECT COALESCE(SUM(pnl_pips),0) FROM trades").fetchone()[0]
                opens = c.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
                wr = wins / total * 100 if total > 0 else 0
                return dict(total=total, wins=wins, losses=losses,
                            pnl=pnl, pips=pips, opens=opens, wr=wr)
        except:
            return dict(total=0, wins=0, losses=0, pnl=0, pips=0, opens=0, wr=0)


# ══════════════════════════════════════════════
#         MARKET DATA (pandas yo'q!)
# ══════════════════════════════════════════════

class Market:
    """Yahoo Finance dan ma'lumot oladi — pandas ishlatmaydi."""

    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"

    @staticmethod
    async def fetch(symbol: str, interval: str, count: int) -> Optional[Dict]:
        """Raw JSON ma'lumot oladi."""
        try:
            period = "5d" if interval in ("5m", "15m", "1m") else "60d"
            url = (
                f"{Market.BASE_URL}{symbol}"
                f"?interval={interval}&range={period}"
                f"&includePrePost=false"
            )
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            }
            async with aiohttp.ClientSession() as s:
                async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    result = data.get("chart", {}).get("result", [])
                    if not result:
                        return None
                    return result[0]
        except Exception as e:
            log.warning(f"Market fetch: {e}")
            return None

    @staticmethod
    def parse_ohlc(data: Dict, count: int = 200) -> Optional[Dict]:
        """OHLC ma'lumotini parse qiladi (pandas o'rniga oddiy list)."""
        try:
            q = data.get("indicators", {}).get("quote", [{}])[0]
            opens = q.get("open", [])
            highs = q.get("high", [])
            lows = q.get("low", [])
            closes = q.get("close", [])

            # None larni tozalash
            valid = [
                (o, h, l, c) for o, h, l, c in zip(opens, highs, lows, closes)
                if all(x is not None for x in (o, h, l, c))
            ]

            if len(valid) < 50:
                return None

            # Oxirgi count ta yozuv
            valid = valid[-count:]

            return {
                "open":  [x[0] for x in valid],
                "high":  [x[1] for x in valid],
                "low":   [x[2] for x in valid],
                "close": [x[3] for x in valid],
                "n":     len(valid),
            }
        except Exception as e:
            log.warning(f"Parse OHLC: {e}")
            return None

    @staticmethod
    async def current_price() -> Optional[Tuple[float, float]]:
        """Joriy bid/ask narxini oladi."""
        try:
            data = await Market.fetch(Config.SYMBOL, "1m", 5)
            if not data:
                return None
            ohlc = Market.parse_ohlc(data, 5)
            if not ohlc or not ohlc["close"]:
                return None
            mid = ohlc["close"][-1]
            spread = Config.PIP * 0.5
            return (mid - spread / 2, mid + spread / 2)
        except Exception as e:
            log.warning(f"Current price: {e}")
            return None


# ══════════════════════════════════════════════
#         INDICATORS (pandas yo'q!)
# ══════════════════════════════════════════════

class TA:
    @staticmethod
    def atr(high, low, close, period=14):
        if len(close) < period + 1:
            return None
        tr_list = []
        for i in range(1, len(close)):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
            tr_list.append(tr)
        if len(tr_list) < period:
            return None
        return sum(tr_list[-period:]) / period

    @staticmethod
    def rsi(close, period=14):
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
        rs = ag / al
        return 100 - 100 / (1 + rs)

    @staticmethod
    def ema(data, period):
        if len(data) < period:
            return None
        k = 2 / (period + 1)
        ema = sum(data[:period]) / period
        for v in data[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    @staticmethod
    def macd(close, fast=12, slow=26, signal=9):
        if len(close) < slow + signal:
            return None, None, None
        ema_fast = TA.ema(close, fast)
        ema_slow = TA.ema(close, slow)
        if ema_fast is None or ema_slow is None:
            return None, None, None
        macd_line = ema_fast - ema_slow

        # Signal line (soddalashtirilgan)
        macd_values = []
        for i in range(slow - 1, len(close)):
            ef = TA.ema(close[:i+1], fast)
            es = TA.ema(close[:i+1], slow)
            if ef and es:
                macd_values.append(ef - es)

        if len(macd_values) < signal:
            return macd_line, None, None

        sig = sum(macd_values[-signal:]) / signal
        hist = macd_line - sig
        prev_hist = macd_values[-2] - sig if len(macd_values) >= 2 else hist
        return macd_line, sig, (hist, prev_hist)

    @staticmethod
    def sma(data, period):
        if len(data) < period:
            return None
        return sum(data[-period:]) / period

    @staticmethod
    def swings(high, low, lookback=5):
        sh, sl = [], []
        for i in range(lookback, len(high) - lookback):
            w_h = high[i-lookback:i+lookback+1]
            w_l = low[i-lookback:i+lookback+1]
            if high[i] == max(w_h):
                sh.append(i)
            if low[i] == min(w_l):
                sl.append(i)
        return sh, sl

    @staticmethod
    def bull_pin(o, h, l, c):
        body = abs(c - o)
        upper = h - max(c, o)
        lower = min(c, o) - l
        rng = h - l
        if rng <= 0 or body <= 0:
            return False
        return lower >= 2 * body and lower / rng >= 0.6 and upper / rng <= 0.2

    @staticmethod
    def bear_pin(o, h, l, c):
        body = abs(c - o)
        upper = h - max(c, o)
        lower = min(c, o) - l
        rng = h - l
        if rng <= 0 or body <= 0:
            return False
        return upper >= 2 * body and upper / rng >= 0.6 and lower / rng <= 0.2

    @staticmethod
    def bull_engulf(po, ph, pl, pc, co, ch, cl, cc):
        return (pc < po and cc > co and cc > po and co < pc)

    @staticmethod
    def bear_engulf(po, ph, pl, pc, co, ch, cl, cc):
        return (pc > po and cc < co and cc < po and co > pc)


# ══════════════════════════════════════════════
#         TREND & CONFIRMATIONS
# ══════════════════════════════════════════════

def detect_trend(ohlc) -> str:
    """UPTREND / DOWNTREND / RANGING"""
    try:
        sh, sl = TA.swings(ohlc["high"], ohlc["low"], 5)
        if len(sh) < 2 or len(sl) < 2:
            return "RANGING"
        h = ohlc["high"]
        l = ohlc["low"]
        highs = [h[sh[-2]], h[sh[-1]]]
        lows = [l[sl[-2]], l[sl[-1]]]
        if highs[1] > highs[0] and lows[1] > lows[0]:
            return "UPTREND"
        if highs[1] < highs[0] and lows[1] < lows[0]:
            return "DOWNTREND"
        return "RANGING"
    except:
        return "RANGING"


def confirms_buy(ohlc) -> List[str]:
    conf = []
    try:
        c = ohlc["close"]
        h = ohlc["high"]
        l = ohlc["low"]
        o = ohlc["open"]

        rsi = TA.rsi(c)
        if rsi is not None:
            if rsi < 35:
                conf.append("RSI_OVERSOLD")
            elif rsi < 50:
                # Bullish divergence (sodda tekshiruv)
                if len(c) >= 10 and min(c[-5:]) < min(c[-10:-5]):
                    rsi_now = TA.rsi(c[-5:])
                    rsi_prev = TA.rsi(c[-10:-5])
                    if rsi_now and rsi_prev and rsi_now > rsi_prev:
                        conf.append("RSI_BULL_DIV")

        _, _, hist_data = TA.macd(c)
        if hist_data:
            hist, prev = hist_data
            if prev < 0 < hist:
                conf.append("MACD_BULL_CROSS")
            elif hist > 0 and hist > prev:
                conf.append("MACD_BULL_MOM")

        if len(c) >= 2:
            if TA.bull_pin(o[-1], h[-1], l[-1], c[-1]):
                conf.append("BULL_PIN")
            if TA.bull_engulf(o[-2], h[-2], l[-2], c[-2],
                              o[-1], h[-1], l[-1], c[-1]):
                conf.append("BULL_ENGULF")

        ma50 = TA.sma(c, 50)
        ma200 = TA.sma(c, 200)
        if ma50 and ma200:
            if ma50 > ma200:
                conf.append("MA_BULLISH")
    except Exception as e:
        log.warning(f"Confirms buy: {e}")
    return conf


def confirms_sell(ohlc) -> List[str]:
    conf = []
    try:
        c = ohlc["close"]
        h = ohlc["high"]
        l = ohlc["low"]
        o = ohlc["open"]

        rsi = TA.rsi(c)
        if rsi is not None:
            if rsi > 65:
                conf.append("RSI_OVERBOUGHT")
            elif rsi > 50:
                if len(c) >= 10 and max(c[-5:]) > max(c[-10:-5]):
                    rsi_now = TA.rsi(c[-5:])
                    rsi_prev = TA.rsi(c[-10:-5])
                    if rsi_now and rsi_prev and rsi_now < rsi_prev:
                        conf.append("RSI_BEAR_DIV")

        _, _, hist_data = TA.macd(c)
        if hist_data:
            hist, prev = hist_data
            if prev > 0 > hist:
                conf.append("MACD_BEAR_CROSS")
            elif hist < 0 and hist < prev:
                conf.append("MACD_BEAR_MOM")

        if len(c) >= 2:
            if TA.bear_pin(o[-1], h[-1], l[-1], c[-1]):
                conf.append("BEAR_PIN")
            if TA.bear_engulf(o[-2], h[-2], l[-2], c[-2],
                              o[-1], h[-1], l[-1], c[-1]):
                conf.append("BEAR_ENGULF")

        ma50 = TA.sma(c, 50)
        ma200 = TA.sma(c, 200)
        if ma50 and ma200:
            if ma50 < ma200:
                conf.append("MA_BEARISH")
    except Exception as e:
        log.warning(f"Confirms sell: {e}")
    return conf


# ══════════════════════════════════════════════
#         SCORING (100-BALL)
# ══════════════════════════════════════════════

def score_signal(ohlc, direction, trend, conf, atr_pips) -> int:
    s = 0
    try:
        # Trend (25)
        if (direction == "BUY" and trend == "UPTREND") or \
           (direction == "SELL" and trend == "DOWNTREND"):
            s += 25
        else:
            return 0

        # Confirmations (30)
        n = len(conf)
        if n >= 4:
            s += 30
        elif n == 3:
            s += 22
        elif n == 2:
            s += 15
        else:
            return 0

        # RSI (15)
        rsi = TA.rsi(ohlc["close"])
        if rsi:
            if direction == "BUY":
                if rsi < 35: s += 15
                elif rsi < 50: s += 10
                elif rsi < 60: s += 5
            else:
                if rsi > 65: s += 15
                elif rsi > 50: s += 10
                elif rsi > 40: s += 5

        # MACD (15)
        _, _, hist_data = TA.macd(ohlc["close"])
        if hist_data:
            hist, prev = hist_data
            if direction == "BUY":
                if hist > 0 and hist > prev: s += 15
                elif hist > 0: s += 10
                elif hist > prev: s += 5
            else:
                if hist < 0 and hist < prev: s += 15
                elif hist < 0: s += 10
                elif hist < prev: s += 5

        # ATR (10)
        if 8 <= atr_pips <= 20: s += 10
        elif 5 <= atr_pips < 8 or 20 < atr_pips <= 25: s += 5

        # Session (5)
        h = datetime.now(timezone.utc).hour
        if 13 <= h < 15: s += 5
        elif 15 <= h < 17: s += 3

    except Exception as e:
        log.warning(f"Score: {e}")
    return min(s, 100)


# ══════════════════════════════════════════════
#         SIGNAL ENGINE
# ══════════════════════════════════════════════

def calc_levels(entry, atr, direction):
    buf = Config.SL_BUFFER * Config.PIP
    early = Config.EARLY_EXIT * Config.PIP
    digits = 5

    if direction == "BUY":
        sl = round(entry - atr * Config.SL_MULT - buf, digits)
        tp = round(entry + atr * Config.TP_MULT - early, digits)
    else:
        sl = round(entry + atr * Config.SL_MULT + buf, digits)
        tp = round(entry - atr * Config.TP_MULT + early, digits)

    sl_pips = abs(entry - sl) / Config.PIP
    tp_pips = abs(tp - entry) / Config.PIP
    return sl, tp, sl_pips, tp_pips


def lot_size(active, sl_pips):
    if sl_pips <= 0:
        return 0.01
    risk = active * 0.01
    lot = risk / (sl_pips * 10)
    return max(0.01, min(round(lot, 2), 5.0))


async def generate_signal(db: 'DB') -> Optional[Dict]:
    # 1. Session
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return None
    if not (Config.SESSION_START <= now.hour < Config.SESSION_END):
        return None

    # 2. NFP filter
    if now.weekday() == 4 and now.day <= 7 and 11 <= now.hour <= 14:
        return None

    # 3. Cooldown
    last = db.get("last_signal")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() / 60 < Config.COOLDOWN_MIN:
                return None
        except:
            pass

    # 4. Ma'lumot olish
    data_h1 = await Market.fetch(Config.SYMBOL, "1h", 200)
    data_m5 = await Market.fetch(Config.SYMBOL, "5m", 200)
    if not data_h1 or not data_m5:
        log.warning("Ma'lumot olishda muammo")
        return None

    ohlc_h1 = Market.parse_ohlc(data_h1, 200)
    ohlc_m5 = Market.parse_ohlc(data_m5, 200)
    if not ohlc_h1 or not ohlc_m5:
        return None

    # 5. ATR
    atr = TA.atr(ohlc_m5["high"], ohlc_m5["low"], ohlc_m5["close"])
    if atr is None:
        return None
    atr_pips = atr / Config.PIP
    if not (Config.ATR_MIN <= atr_pips <= Config.ATR_MAX):
        return None

    # 6. Trend (H1)
    trend = detect_trend(ohlc_h1)
    if trend == "RANGING":
        return None

    # 7. Confirmations (M5)
    if trend == "UPTREND":
        conf = confirms_buy(ohlc_m5)
        direction = "BUY"
    else:
        conf = confirms_sell(ohlc_m5)
        direction = "SELL"

    if len(conf) < 2:
        return None

    # 8. Score
    sc = score_signal(ohlc_m5, direction, trend, conf, atr_pips)
    if sc < Config.MIN_SCORE:
        return None

    # 9. Narx
    price = await Market.current_price()
    if not price:
        return None
    bid, ask = price
    entry = ask if direction == "BUY" else bid

    # 10. Levels
    sl, tp, sl_pips, tp_pips = calc_levels(entry, atr, direction)
    active = float(db.get("active", str(Config.ACTIVE_CAPITAL)))
    lot = lot_size(active, sl_pips)
    rsi = TA.rsi(ohlc_m5["close"]) or 50.0

    return dict(
        direction=direction,
        score=sc,
        trend=trend,
        entry=entry,
        sl=sl,
        tp=tp,
        sl_pips=round(sl_pips, 1),
        tp_pips=round(tp_pips, 1),
        lot=lot,
        rsi=round(rsi, 1),
        atr_pips=round(atr_pips, 1),
        confirms=",".join(conf),
        time=now.strftime("%H:%M"),
    )


# ══════════════════════════════════════════════
#         MESSAGE FORMATTING
# ══════════════════════════════════════════════

def fmt_signal(s: Dict) -> str:
    em = "🟢" if s["direction"] == "BUY" else "🔴"
    ar = "📈" if s["direction"] == "BUY" else "📉"
    rr = round(s["tp_pips"] / s["sl_pips"], 2) if s["sl_pips"] > 0 else 0
    return (
        f"{em} <b>{s['direction']} SIGNAL — EUR/USD</b> {ar}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💯 <b>Score:</b> {s['score']}/100 (KUCHLI)\n"
        f"📊 <b>Trend:</b> {s['trend']}\n"
        f"✅ <b>Confirms:</b> {s['confirms'].replace(',', ', ')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Entry:</b>  <code>{s['entry']:.5f}</code>\n"
        f"🛡 <b>SL:</b>     <code>{s['sl']:.5f}</code> ({s['sl_pips']} pip)\n"
        f"🎯 <b>TP:</b>     <code>{s['tp']:.5f}</code> ({s['tp_pips']} pip)\n"
        f"⚖️ <b>RR:</b>     1 : {rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Lot:</b> {s['lot']}\n"
        f"📈 <b>RSI:</b> {s['rsi']}\n"
        f"🌊 <b>ATR:</b> {s['atr_pips']} pip\n"
        f"⏰ <b>Time:</b> {s['time']} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>MT5 ilovangizda shu darajalarda savdo oching</i>"
    )


def fmt_close(t: Dict, price, pips, usd, status) -> str:
    em = "✅" if status == "CLOSED_TP" else "❌"
    title = "TP HIT — FOYDA" if status == "CLOSED_TP" else "SL HIT — ZARAR"
    dur = ""
    try:
        opened = datetime.fromisoformat(t["open_time"])
        delta = datetime.now(timezone.utc) - opened
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        dur = f"{h}s {m}d" if h else f"{m}d"
    except:
        pass
    return (
        f"{em} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Trade #{t['id']} | {t['direction']}</b>\n"
        f"📍 Entry: <code>{t['entry']:.5f}</code>\n"
        f"🚪 Exit:  <code>{price:.5f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>P/L: {pips:+.1f} pip | ${usd:+.2f}</b>\n"
        f"⏱ Davomiyligi: {dur}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════
#              TELEGRAM BOT
# ══════════════════════════════════════════════

class VulkxanBot:
    def __init__(self):
        self.db = DB()
        self.app: Optional[Application] = None
        self._monitor_task = None
        self._ping_task = None

    def _kb(self):
        running = self.db.get("running") == "1"
        row1 = [InlineKeyboardButton("⏸ STOP", callback_data="stop")] \
               if running else \
               [InlineKeyboardButton("▶️ START", callback_data="start")]
        return InlineKeyboardMarkup([
            row1,
            [InlineKeyboardButton("📊 STATS", callback_data="stats"),
             InlineKeyboardButton("📋 OPEN", callback_data="open")],
            [InlineKeyboardButton("ℹ️ HELP", callback_data="help")],
        ])

    async def cmd_start(self, update: Update, ctx):
        if update.effective_user.id != Config.OWNER_CHAT_ID:
            await update.message.reply_text(
                f"⛔ Bu bot faqat egasi uchun.\n"
                f"Sizning ID: <code>{update.effective_user.id}</code>",
                parse_mode=ParseMode.HTML
            )
            return
        running = self.db.get("running") == "1"
        status = "🟢 ISHLAMOQDA" if running else "🔴 TO'XTAGAN"
        await update.message.reply_text(
            f"🎯 <b>VULKXAN FOREX SIGNAL BOT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Juftlik: EUR/USD\n"
            f"💯 Min Score: {Config.MIN_SCORE}/100\n"
            f"⏰ Sessiya: {Config.SESSION_START}:00–{Config.SESSION_END}:00 UTC\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Holat: {status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=self._kb(),
            parse_mode=ParseMode.HTML
        )

    async def cb(self, update: Update, ctx):
        q = update.callback_query
        if q.from_user.id != Config.OWNER_CHAT_ID:
            await q.answer("⛔ Ruxsat yo'q", show_alert=True)
            return
        await q.answer()
        a = q.data

        if a == "start":
            self.db.set("running", "1")
            await q.edit_message_text(
                "🟢 <b>BOT ISHGA TUSHDI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Bozorni kuzatish boshlandi\n"
                f"📊 Min score: {Config.MIN_SCORE}/100\n"
                f"⏰ Sessiya: {Config.SESSION_START}:00–{Config.SESSION_END}:00 UTC\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔔 Kuchli signal topilsa xabar yuboraman.",
                reply_markup=self._kb(), parse_mode=ParseMode.HTML
            )

        elif a == "stop":
            self.db.set("running", "0")
            await q.edit_message_text(
                "🔴 <b>BOT TO'XTATILDI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏸ Yangi signallar to'xtatildi\n"
                "▶️ Qayta yoqish uchun START bosing",
                reply_markup=self._kb(), parse_mode=ParseMode.HTML
            )

        elif a == "stats":
            s = self.db.stats()
            active = float(self.db.get("active", str(Config.ACTIVE_CAPITAL)))
            await q.edit_message_text(
                f"📊 <b>STATISTIKA</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 Jami savdolar: {s['total']}\n"
                f"✅ G'alabalar: {s['wins']}\n"
                f"❌ Mag'lubiyatlar: {s['losses']}\n"
                f"🎯 Win Rate: {s['wr']:.1f}%\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Jami P/L: ${s['pnl']:+.2f}\n"
                f"📏 Jami pips: {s['pips']:+.1f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💼 Aktiv kapital: ${active:.2f}\n"
                f"📋 Ochiq: {s['opens']}",
                reply_markup=self._kb(), parse_mode=ParseMode.HTML
            )

        elif a == "open":
            trades = self.db.open_trades()
            if not trades:
                msg = "📋 <b>OCHIQ SAVDOLAR</b>\n━━━━━━━━━━━━━━━━━━━━━━\n📭 Ochiq savdolar yo'q"
            else:
                lines = [f"📋 <b>OCHIQ SAVDOLAR ({len(trades)})</b>",
                         "━━━━━━━━━━━━━━━━━━━━━━"]
                for t in trades:
                    em = "🟢" if t["direction"] == "BUY" else "🔴"
                    lines.append(
                        f"{em} #{t['id']} {t['direction']}\n"
                        f"   Entry: <code>{t['entry']:.5f}</code>\n"
                        f"   SL: <code>{t['sl']:.5f}</code> | TP: <code>{t['tp']:.5f}</code>"
                    )
                msg = "\n".join(lines)
            await q.edit_message_text(msg, reply_markup=self._kb(), parse_mode=ParseMode.HTML)

        elif a == "help":
            await q.edit_message_text(
                f"ℹ️ <b>YO'RIQNOMA</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>▶️ START</b> — kuzatishni boshlash\n"
                f"<b>⏸ STOP</b> — kuzatishni to'xtatish\n"
                f"<b>📊 STATS</b> — statistika\n"
                f"<b>📋 OPEN</b> — ochiq savdolar\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>QANDAY ISHLAYDI:</b>\n"
                f"1. START bosing\n"
                f"2. Bot London-NY sessiyasida kuzatadi\n"
                f"3. Kuchli signal → Telegram xabar\n"
                f"4. MT5 ilovada savdo ochasiz\n"
                f"5. TP/SL → natija xabari keladi\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Bot o'zini har {Config.SELF_PING_MIN} daqiqada\n"
                f"uyg'otadi (uxlab qolmaydi)",
                reply_markup=self._kb(), parse_mode=ParseMode.HTML
            )

    # ─── Self-ping (uxlab qolmaslik uchun) ────

    async def self_ping_loop(self):
        """Har 14 daqiqada o'zini ping qiladi — Render uxlatmaydi."""
        log.info(f"🔔 Self-ping loop boshlandi ({Config.SELF_PING_MIN} daqiqada bir)")
        while True:
            try:
                await asyncio.sleep(Config.SELF_PING_MIN * 60)
                url = Config.RENDER_URL
                if url:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(f"{url}/health",
                                         timeout=aiohttp.ClientTimeout(total=10)) as r:
                            log.info(f"🏓 Self-ping: {r.status}")
                else:
                    log.debug("RENDER_URL yo'q, ping o'tkazib yuborildi")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Self-ping xato: {e}")

    # ─── Monitor ──────────────────────────────

    async def monitor_loop(self):
        log.info("📡 Monitor loop boshlandi")
        while True:
            try:
                if self.db.get("running") == "1":
                    await self._check_open()
                    if len(self.db.open_trades()) < 3:
                        sig = await generate_signal(self.db)
                        if sig:
                            await self._send_signal(sig)
                await asyncio.sleep(Config.LOOP_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"Monitor: {e}")
                await asyncio.sleep(30)

    async def _send_signal(self, s: Dict):
        try:
            msg = fmt_signal(s)
            await self.app.bot.send_message(
                Config.OWNER_CHAT_ID, msg, parse_mode=ParseMode.HTML
            )
            tid = self.db.save_trade(s)
            self.db.set("last_signal", datetime.now(timezone.utc).isoformat())
            log.info(f"📨 Signal #{tid} | {s['direction']} | Score:{s['score']}")
        except Exception as e:
            log.error(f"Send signal: {e}")

    async def _check_open(self):
        trades = self.db.open_trades()
        if not trades:
            return
        price = await Market.current_price()
        if not price:
            return
        bid, ask = price
        mid = (bid + ask) / 2

        for t in trades:
            try:
                d = t["direction"]
                status = None
                exit_price = mid

                if d == "BUY":
                    if mid >= t["tp"]:
                        status, exit_price = "CLOSED_TP", t["tp"]
                    elif mid <= t["sl"]:
                        status, exit_price = "CLOSED_SL", t["sl"]
                else:
                    if mid <= t["tp"]:
                        status, exit_price = "CLOSED_TP", t["tp"]
                    elif mid >= t["sl"]:
                        status, exit_price = "CLOSED_SL", t["sl"]

                if not status:
                    continue

                pips = (exit_price - t["entry"]) / Config.PIP if d == "BUY" \
                    else (t["entry"] - exit_price) / Config.PIP
                usd = pips * t["lot"] * 10

                self.db.close_trade(t["id"], status, exit_price, pips, usd)
                active = float(self.db.get("active", str(Config.ACTIVE_CAPITAL)))
                self.db.set("active", active + usd)

                msg = fmt_close(t, exit_price, pips, usd, status)
                await self.app.bot.send_message(
                    Config.OWNER_CHAT_ID, msg, parse_mode=ParseMode.HTML
                )
                log.info(f"Trade #{t['id']} yopildi | {status} | {pips:+.1f}pip | ${usd:+.2f}")
            except Exception as e:
                log.error(f"Check trade: {e}")

    # ─── Startup / Shutdown ───────────────────

    async def on_startup(self, app):
        await app.bot.set_my_commands([
            BotCommand("start", "Botni ochish / asosiy menyu"),
        ])
        self._monitor_task = asyncio.create_task(self.monitor_loop())
        self._ping_task = asyncio.create_task(self.self_ping_loop())
        try:
            await app.bot.send_message(
                Config.OWNER_CHAT_ID,
                "🎯 <b>VULKXAN BOT ONLAYN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Server ishlamoqda\n"
                "📊 EUR/USD kuzatuvga tayyor\n"
                f"🔄 O'z-o'zini har {Config.SELF_PING_MIN} daqiqada uyg'otadi\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "👇 /start bosing",
                parse_mode=ParseMode.HTML
            )
            log.info("✅ Welcome xabari yuborildi")
        except Exception as e:
            log.warning(f"Welcome: {e}")

    async def on_shutdown(self, app):
        for t in [self._monitor_task, self._ping_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    # ─── Health Server ────────────────────────

    async def health_server(self):
        """Render Free uchun HTTP server."""
        from aiohttp import web

        async def health(req):
            return web.Response(
                text=json.dumps({
                    "status": "ok",
                    "bot": "vulkxan",
                    "running": self.db.get("running") == "1",
                    "time": datetime.now(timezone.utc).isoformat(),
                }),
                content_type="application/json"
            )

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
        await site.start()
        log.info(f"✅ Health server: port {Config.PORT}")
        return runner

    # ─── Run ──────────────────────────────────

    async def run(self):
        runner = await self.health_server()

        log.info("═" * 55)
        log.info("  🎯 VULKXAN FOREX SIGNAL BOT v2.0")
        log.info(f"  📊 {Config.DISPLAY}")
        log.info(f"  💯 Min Score: {Config.MIN_SCORE}/100")
        log.info(f"  ⏰ Session: {Config.SESSION_START}:00-{Config.SESSION_END}:00 UTC")
        log.info(f"  🏓 Self-ping: har {Config.SELF_PING_MIN} daqiqada")
        log.info("═" * 55)

        self.app = ApplicationBuilder().token(Config.TELEGRAM_TOKEN).build()
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CallbackQueryHandler(self.cb))

        try:
            await self.app.initialize()
            await self.app.start()
            await self.on_startup(self.app)
            await self.app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("✅ Bot ishlamoqda...")
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            await self.on_shutdown(self.app)
            try:
                if self.app.updater.running:
                    await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception:
                pass
            await runner.cleanup()


# ══════════════════════════════════════════════
#              ENTRY POINT
# ══════════════════════════════════════════════

async def main():
    bot = VulkxanBot()
    try:
        await bot.run()
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("👋 Bot to'xtadi")
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
