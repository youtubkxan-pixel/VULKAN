"""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║         🎯  VULKXAN FOREX SIGNAL BOT — FINAL                    ║
║         @vulkxan_bot — EUR/USD Smart Signals                    ║
║                                                                  ║
║         Architecture: Structure + Confirmation + Filter         ║
║         Strategy: ATR-based with Stop-Hunt Protection           ║
║         Win Rate Target: 55-62%                                 ║
║                                                                  ║
║         ✅ Python 3.11 compatible                                ║
║         ✅ No pandas/numpy (Render Free compatible)              ║
║         ✅ Self-ping (no sleep)                                  ║
║         ✅ Only owner can use                                    ║
║         ✅ Full trading logic preserved                          ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import sqlite3
import os
import sys
import json
import urllib.request
import urllib.error
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


# ════════════════════════════════════════════════════════════════
#                       CONFIGURATION
# ════════════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.getenv(
    "TELEGRAM_TOKEN",
    "8799555582:AAETKBrTm6IxXmwkB55YxHA71B1CLjsQIC0"
)
OWNER_CHAT_ID = int(os.getenv("CHAT_ID", "7837984652"))
RENDER_URL = os.getenv("RENDER_URL", "")
PORT = int(os.getenv("PORT", "10000"))

# Instrument
SYMBOL = "EURUSD=X"
DISPLAY = "EUR/USD"
PIP = 0.0001

# Strategy
MIN_SCORE = 75              # Faqat KUCHLI signallar
SESSION_START = 13          # UTC (London-NY overlap)
SESSION_END = 17
ATR_MIN = 5.0
ATR_MAX = 30.0
SL_MULT = 1.0               # SL = 1 × ATR
TP_MULT = 3.0               # TP = 3 × ATR (1:3 RR)
EARLY_EXIT = 2.0            # TP dan 2 pip oldin chiqish
SL_BUFFER = 2.0             # SL ni 2 pip kengaytirish (stop hunt)

# Timing
LOOP_SEC = 60
COOLDOWN_MIN = 30
SELF_PING_MIN = 14

# Capital
ACTIVE_CAPITAL = 1000.0
RESERVE_CAPITAL = 9000.0

# Indicators
ATR_PERIOD = 14
RSI_PERIOD = 14
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MA_FAST = 50
MA_SLOW = 200
SWING_LOOKBACK = 5

DB_PATH = "vulkxan.db"


# ════════════════════════════════════════════════════════════════
#                          LOGGER
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
#                          DATABASE
# ════════════════════════════════════════════════════════════════

def db_conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    try:
        with db_conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                sl_pips REAL NOT NULL,
                tp_pips REAL NOT NULL,
                lot REAL NOT NULL,
                score INTEGER NOT NULL,
                confirms TEXT,
                status TEXT DEFAULT 'OPEN',
                open_time TEXT NOT NULL,
                close_time TEXT,
                close_price REAL,
                pnl_pips REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                val TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_status ON trades(status);
            """)
            for k, v in [
                ("running", "0"),
                ("active", str(ACTIVE_CAPITAL)),
                ("reserve", str(RESERVE_CAPITAL)),
                ("last_signal", ""),
            ]:
                c.execute("INSERT OR IGNORE INTO kv VALUES(?,?)", (k, v))
        log.info("✅ Database tayyor")
    except Exception as e:
        log.error(f"DB init: {e}")
        raise


def db_get(k: str, default: str = "") -> str:
    try:
        with db_conn() as c:
            r = c.execute("SELECT val FROM kv WHERE key=?", (k,)).fetchone()
            return r["val"] if r else default
    except Exception as e:
        log.warning(f"db_get({k}): {e}")
        return default


def db_set(k: str, v):
    try:
        with db_conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kv VALUES(?,?)",
                (k, str(v))
            )
    except Exception as e:
        log.warning(f"db_set({k}): {e}")


def db_save_trade(d: Dict) -> int:
    try:
        with db_conn() as c:
            cur = c.execute("""
                INSERT INTO trades
                (direction, entry, sl, tp, sl_pips, tp_pips, lot,
                 score, confirms, status, open_time)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d["direction"], d["entry"], d["sl"], d["tp"],
                d["sl_pips"], d["tp_pips"], d["lot"], d["score"],
                d["confirms"], "OPEN",
                datetime.now(timezone.utc).isoformat()
            ))
            return cur.lastrowid
    except Exception as e:
        log.error(f"db_save_trade: {e}")
        return -1


def db_close_trade(tid: int, status: str, price: float,
                   pips: float, usd: float):
    try:
        with db_conn() as c:
            c.execute("""
                UPDATE trades
                SET status=?, close_time=?, close_price=?,
                    pnl_pips=?, pnl_usd=?
                WHERE id=?
            """, (
                status, datetime.now(timezone.utc).isoformat(),
                price, pips, usd, tid
            ))
    except Exception as e:
        log.error(f"db_close_trade: {e}")


def db_open_trades() -> List[Dict]:
    try:
        with db_conn() as c:
            rows = c.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"db_open_trades: {e}")
        return []


def db_stats() -> Dict:
    try:
        with db_conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status!='OPEN'"
            ).fetchone()[0]
            wins = c.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl_usd>0"
            ).fetchone()[0]
            losses = c.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl_usd<0"
            ).fetchone()[0]
            pnl = c.execute(
                "SELECT COALESCE(SUM(pnl_usd),0) FROM trades"
            ).fetchone()[0]
            pips = c.execute(
                "SELECT COALESCE(SUM(pnl_pips),0) FROM trades"
            ).fetchone()[0]
            opens = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status='OPEN'"
            ).fetchone()[0]
            wr = wins / total * 100 if total > 0 else 0
            return dict(
                total=total, wins=wins, losses=losses,
                pnl=pnl, pips=pips, opens=opens, wr=wr
            )
    except Exception as e:
        log.error(f"db_stats: {e}")
        return dict(total=0, wins=0, losses=0,
                    pnl=0, pips=0, opens=0, wr=0)


# ════════════════════════════════════════════════════════════════
#                       MARKET DATA
# ════════════════════════════════════════════════════════════════

def _fetch_yahoo_sync(symbol: str, interval: str) -> Optional[Dict]:
    """Yahoo Finance dan OHLC ma'lumotini sinxron oladi."""
    try:
        period = "5d" if interval in ("1m", "5m", "15m") else "60d"
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?interval={interval}&range={period}"
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))

        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        q = result[0].get("indicators", {}).get("quote", [{}])[0]
        opens = q.get("open", [])
        highs = q.get("high", [])
        lows = q.get("low", [])
        closes = q.get("close", [])

        valid = [
            (o, h, l, c)
            for o, h, l, c in zip(opens, highs, lows, closes)
            if all(x is not None for x in (o, h, l, c))
        ]
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


async def fetch_yahoo(symbol: str, interval: str) -> Optional[Dict]:
    """Async wrapper."""
    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _fetch_yahoo_sync, symbol, interval
        )
    except Exception as e:
        log.warning(f"fetch_yahoo async: {e}")
        return None


async def get_current_price() -> Optional[Tuple[float, float]]:
    """Returns (bid, ask)."""
    data = await fetch_yahoo(SYMBOL, "1m")
    if not data or not data["close"]:
        return None
    mid = float(data["close"][-1])
    spread = PIP * 0.5
    return (mid - spread / 2, mid + spread / 2)


# ════════════════════════════════════════════════════════════════
#                       INDICATORS
# ════════════════════════════════════════════════════════════════

def calc_atr(high: List[float], low: List[float],
             close: List[float], period: int = ATR_PERIOD) -> Optional[float]:
    if len(close) < period + 1:
        return None
    trs = []
    for i in range(1, len(close)):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i-1]),
            abs(low[i] - close[i-1])
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def calc_rsi(close: List[float], period: int = RSI_PERIOD) -> Optional[float]:
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


def calc_rsi_series(close: List[float], period: int = RSI_PERIOD) -> List[float]:
    """RSI qiymatlari ketma-ketligi (divergence aniqlash uchun)."""
    rsi_values = []
    for i in range(period + 1, len(close) + 1):
        r = calc_rsi(close[:i], period)
        if r is not None:
            rsi_values.append(r)
    return rsi_values


def calc_ema(data: List[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for v in data[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_macd_hist(close: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """MACD histogram (hozirgi, oldingi)."""
    if len(close) < MACD_SLOW + MACD_SIGNAL:
        return None, None
    ef = calc_ema(close, MACD_FAST)
    es = calc_ema(close, MACD_SLOW)
    if ef is None or es is None:
        return None, None
    macd_now = ef - es

    if len(close) < MACD_SLOW + MACD_SIGNAL + 1:
        return macd_now, macd_now
    ef_p = calc_ema(close[:-1], MACD_FAST)
    es_p = calc_ema(close[:-1], MACD_SLOW)
    if ef_p is None or es_p is None:
        return macd_now, macd_now
    macd_prev = ef_p - es_p

    return macd_now, macd_prev


def calc_sma(data: List[float], period: int) -> Optional[float]:
    if len(data) < period:
        return None
    return sum(data[-period:]) / period


def find_swings(high: List[float], low: List[float],
                lookback: int = SWING_LOOKBACK) -> Tuple[List[int], List[int]]:
    sh, sl = [], []
    for i in range(lookback, len(high) - lookback):
        window_h = high[i-lookback:i+lookback+1]
        window_l = low[i-lookback:i+lookback+1]
        if high[i] == max(window_h):
            sh.append(i)
        if low[i] == min(window_l):
            sl.append(i)
    return sh, sl


# ════════════════════════════════════════════════════════════════
#                    CANDLESTICK PATTERNS
# ════════════════════════════════════════════════════════════════

def is_bull_pin(o: float, h: float, l: float, c: float) -> bool:
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    return (lower >= 2 * body and lower / rng >= 0.6 and upper / rng <= 0.2)


def is_bear_pin(o: float, h: float, l: float, c: float) -> bool:
    body = abs(c - o)
    upper = h - max(c, o)
    lower = min(c, o) - l
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    return (upper >= 2 * body and upper / rng >= 0.6 and lower / rng <= 0.2)


def is_bull_engulf(po: float, pc: float, co: float, cc: float) -> bool:
    return pc < po and cc > co and cc > po and co < pc


def is_bear_engulf(po: float, pc: float, co: float, cc: float) -> bool:
    return pc > po and cc < co and cc < po and co > pc


# ════════════════════════════════════════════════════════════════
#                    STRUCTURE ANALYZER
# ════════════════════════════════════════════════════════════════

def detect_trend(ohlc: Dict) -> str:
    """HH/HL = UPTREND, LH/LL = DOWNTREND, else RANGING."""
    try:
        sh, sl = find_swings(ohlc["high"], ohlc["low"])
        if len(sh) < 2 or len(sl) < 2:
            return "RANGING"
        h, l = ohlc["high"], ohlc["low"]
        hh = h[sh[-1]] > h[sh[-2]]
        hl = l[sl[-1]] > l[sl[-2]]
        lh = h[sh[-1]] < h[sh[-2]]
        ll = l[sl[-1]] < l[sl[-2]]
        if hh and hl:
            return "UPTREND"
        if lh and ll:
            return "DOWNTREND"
        return "RANGING"
    except Exception as e:
        log.warning(f"detect_trend: {e}")
        return "RANGING"


# ════════════════════════════════════════════════════════════════
#                  CONFIRMATIONS (BUY/SELL)
# ════════════════════════════════════════════════════════════════

def get_confirms_buy(ohlc: Dict) -> List[str]:
    conf = []
    try:
        c = ohlc["close"]
        h = ohlc["high"]
        l = ohlc["low"]
        o = ohlc["open"]

        # 1. RSI Oversold yoki Bullish Divergence
        rsi = calc_rsi(c)
        if rsi is not None:
            if rsi < RSI_OVERSOLD:
                conf.append("RSI_OVERSOLD")
            elif rsi < 50 and len(c) >= 20:
                # Bullish divergence
                price_low_now = min(c[-5:])
                price_low_prev = min(c[-15:-5])
                rsi_series = calc_rsi_series(c)
                if len(rsi_series) >= 15:
                    rsi_low_now = min(rsi_series[-5:])
                    rsi_low_prev = min(rsi_series[-15:-5])
                    if (price_low_now < price_low_prev and
                            rsi_low_now > rsi_low_prev):
                        conf.append("RSI_BULL_DIV")

        # 2. MACD bullish
        macd_now, macd_prev = calc_macd_hist(c)
        if macd_now is not None and macd_prev is not None:
            if macd_prev < 0 < macd_now:
                conf.append("MACD_BULL_CROSS")
            elif macd_now > 0 and macd_now > macd_prev:
                conf.append("MACD_BULL_MOM")

        # 3. Bullish patterns
        if len(c) >= 2:
            if is_bull_pin(o[-1], h[-1], l[-1], c[-1]):
                conf.append("BULL_PIN")
            if is_bull_engulf(o[-2], c[-2], o[-1], c[-1]):
                conf.append("BULL_ENGULF")

        # 4. MA bullish
        ma_fast = calc_sma(c, MA_FAST)
        ma_slow = calc_sma(c, MA_SLOW)
        if ma_fast is not None and ma_slow is not None:
            if ma_fast > ma_slow:
                # Golden cross check
                if len(c) >= 2:
                    ma_fast_p = calc_sma(c[:-1], MA_FAST)
                    ma_slow_p = calc_sma(c[:-1], MA_SLOW)
                    if (ma_fast_p is not None and ma_slow_p is not None
                            and ma_fast_p <= ma_slow_p):
                        conf.append("MA_GOLDEN")
                    else:
                        conf.append("MA_BULLISH")
                else:
                    conf.append("MA_BULLISH")
    except Exception as e:
        log.warning(f"confirms_buy: {e}")
    return conf


def get_confirms_sell(ohlc: Dict) -> List[str]:
    conf = []
    try:
        c = ohlc["close"]
        h = ohlc["high"]
        l = ohlc["low"]
        o = ohlc["open"]

        # 1. RSI Overbought yoki Bearish Divergence
        rsi = calc_rsi(c)
        if rsi is not None:
            if rsi > RSI_OVERBOUGHT:
                conf.append("RSI_OVERBOUGHT")
            elif rsi > 50 and len(c) >= 20:
                price_high_now = max(c[-5:])
                price_high_prev = max(c[-15:-5])
                rsi_series = calc_rsi_series(c)
                if len(rsi_series) >= 15:
                    rsi_high_now = max(rsi_series[-5:])
                    rsi_high_prev = max(rsi_series[-15:-5])
                    if (price_high_now > price_high_prev and
                            rsi_high_now < rsi_high_prev):
                        conf.append("RSI_BEAR_DIV")

        # 2. MACD bearish
        macd_now, macd_prev = calc_macd_hist(c)
        if macd_now is not None and macd_prev is not None:
            if macd_prev > 0 > macd_now:
                conf.append("MACD_BEAR_CROSS")
            elif macd_now < 0 and macd_now < macd_prev:
                conf.append("MACD_BEAR_MOM")

        # 3. Bearish patterns
        if len(c) >= 2:
            if is_bear_pin(o[-1], h[-1], l[-1], c[-1]):
                conf.append("BEAR_PIN")
            if is_bear_engulf(o[-2], c[-2], o[-1], c[-1]):
                conf.append("BEAR_ENGULF")

        # 4. MA bearish
        ma_fast = calc_sma(c, MA_FAST)
        ma_slow = calc_sma(c, MA_SLOW)
        if ma_fast is not None and ma_slow is not None:
            if ma_fast < ma_slow:
                if len(c) >= 2:
                    ma_fast_p = calc_sma(c[:-1], MA_FAST)
                    ma_slow_p = calc_sma(c[:-1], MA_SLOW)
                    if (ma_fast_p is not None and ma_slow_p is not None
                            and ma_fast_p >= ma_slow_p):
                        conf.append("MA_DEATH")
                    else:
                        conf.append("MA_BEARISH")
                else:
                    conf.append("MA_BEARISH")
    except Exception as e:
        log.warning(f"confirms_sell: {e}")
    return conf


# ════════════════════════════════════════════════════════════════
#                    FILTERS
# ════════════════════════════════════════════════════════════════

def is_session_active() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:                  # Shanba/Yakshanba
        return False
    return SESSION_START <= now.hour < SESSION_END


def is_news_ok() -> bool:
    now = datetime.now(timezone.utc)
    # NFP — har oyning 1-jumasi 12:30-13:30 UTC
    if now.weekday() == 4 and now.day <= 7:
        if 11 <= now.hour <= 14:
            return False
    return True


def is_atr_ok(atr_pips: float) -> bool:
    return ATR_MIN <= atr_pips <= ATR_MAX


# ════════════════════════════════════════════════════════════════
#                  100-BALL SIGNAL SCORING
# ════════════════════════════════════════════════════════════════

def calc_score(ohlc: Dict, direction: str, trend: str,
               conf: List[str], atr_pips: float) -> int:
    """
    100-ballik scoring:
      Trend mos:        25
      Confirmations:    30
      RSI position:     15
      MACD momentum:    15
      ATR sifati:       10
      Session activity:  5
    """
    s = 0
    try:
        # 1. Trend (25)
        if direction == "BUY" and trend == "UPTREND":
            s += 25
        elif direction == "SELL" and trend == "DOWNTREND":
            s += 25
        else:
            return 0

        # 2. Confirmations (30)
        n = len(conf)
        if n >= 4:
            s += 30
        elif n == 3:
            s += 22
        elif n == 2:
            s += 15
        else:
            return 0

        # 3. RSI (15)
        rsi = calc_rsi(ohlc["close"])
        if rsi is not None:
            if direction == "BUY":
                if rsi < RSI_OVERSOLD:
                    s += 15
                elif rsi < 50:
                    s += 10
                elif rsi < 60:
                    s += 5
            else:
                if rsi > RSI_OVERBOUGHT:
                    s += 15
                elif rsi > 50:
                    s += 10
                elif rsi > 40:
                    s += 5

        # 4. MACD (15)
        macd_now, macd_prev = calc_macd_hist(ohlc["close"])
        if macd_now is not None and macd_prev is not None:
            if direction == "BUY":
                if macd_now > 0 and macd_now > macd_prev:
                    s += 15
                elif macd_now > 0:
                    s += 10
                elif macd_now > macd_prev:
                    s += 5
            else:
                if macd_now < 0 and macd_now < macd_prev:
                    s += 15
                elif macd_now < 0:
                    s += 10
                elif macd_now < macd_prev:
                    s += 5

        # 5. ATR (10)
        if 8 <= atr_pips <= 20:
            s += 10
        elif 5 <= atr_pips < 8 or 20 < atr_pips <= 25:
            s += 5

        # 6. Session activity (5)
        h = datetime.now(timezone.utc).hour
        if 13 <= h < 15:
            s += 5
        elif 15 <= h < 17:
            s += 3
    except Exception as e:
        log.warning(f"calc_score: {e}")
    return min(s, 100)


# ════════════════════════════════════════════════════════════════
#                    TP/SL CALCULATION
# ════════════════════════════════════════════════════════════════

def calc_levels(entry: float, atr: float,
                direction: str) -> Tuple[float, float, float, float]:
    """
    Smart TP/SL:
      BUY:
        SL = Entry - ATR×1.0 - 2 pip   (KENGAYTIRILGAN - stop hunt himoyasi)
        TP = Entry + ATR×3.0 - 2 pip   (OLDIN CHIQISH - foyda saqlash)
      SELL:
        SL = Entry + ATR×1.0 + 2 pip   (KENGAYTIRILGAN)
        TP = Entry - ATR×3.0 + 2 pip   (OLDIN CHIQISH)
    """
    buf = SL_BUFFER * PIP
    early = EARLY_EXIT * PIP

    if direction == "BUY":
        sl = round(entry - atr * SL_MULT - buf, 5)
        tp = round(entry + atr * TP_MULT - early, 5)
    else:
        sl = round(entry + atr * SL_MULT + buf, 5)
        tp = round(entry - atr * TP_MULT + early, 5)

    sl_pips = abs(entry - sl) / PIP
    tp_pips = abs(tp - entry) / PIP
    return sl, tp, sl_pips, tp_pips


def calc_lot_size(active: float, sl_pips: float) -> float:
    """Position size = active × 1% / risk_pips × pip_value."""
    if sl_pips <= 0:
        return 0.01
    risk_amount = active * 0.01           # 1% of active
    pip_value = 10.0                      # 1 lot EURUSD = $10/pip
    lot = risk_amount / (sl_pips * pip_value)
    return max(0.01, min(round(lot, 2), 5.0))


# ════════════════════════════════════════════════════════════════
#                    SIGNAL GENERATION
# ════════════════════════════════════════════════════════════════

async def generate_signal() -> Optional[Dict]:
    """Asosiy signal generator — Structure + Confirmation + Filter."""
    try:
        now = datetime.now(timezone.utc)

        # ─── FILTERS ───
        if not is_session_active():
            return None
        if not is_news_ok():
            log.debug("News zonasi")
            return None

        # Cooldown
        last = db_get("last_signal")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                mins = (now - last_dt).total_seconds() / 60
                if mins < COOLDOWN_MIN:
                    return None
            except Exception:
                pass

        # ─── DATA ───
        h1 = await fetch_yahoo(SYMBOL, "1h")
        m5 = await fetch_yahoo(SYMBOL, "5m")
        if not h1 or not m5:
            log.debug("Ma'lumot olishda muammo")
            return None
        if len(m5["close"]) < 100:
            log.debug("M5 ma'lumot kam")
            return None

        # ─── ATR FILTER ───
        atr = calc_atr(m5["high"], m5["low"], m5["close"])
        if atr is None:
            return None
        atr_pips = atr / PIP
        if not is_atr_ok(atr_pips):
            log.debug(f"ATR filter: {atr_pips:.1f}")
            return None

        # ─── STRUCTURE (H1) ───
        trend = detect_trend(h1)
        if trend == "RANGING":
            return None

        # ─── CONFIRMATIONS (M5) ───
        if trend == "UPTREND":
            conf = get_confirms_buy(m5)
            direction = "BUY"
        else:
            conf = get_confirms_sell(m5)
            direction = "SELL"

        if len(conf) < 2:
            return None

        # ─── SCORING ───
        score = calc_score(m5, direction, trend, conf, atr_pips)
        if score < MIN_SCORE:
            log.debug(f"Score past: {score}")
            return None

        # ─── PRICE ───
        price = await get_current_price()
        if not price:
            return None
        bid, ask = price
        entry = ask if direction == "BUY" else bid

        # ─── LEVELS ───
        sl, tp, sl_pips, tp_pips = calc_levels(entry, atr, direction)
        active = float(db_get("active", str(ACTIVE_CAPITAL)))
        lot = calc_lot_size(active, sl_pips)
        rsi = calc_rsi(m5["close"]) or 50.0

        return dict(
            direction=direction,
            score=score,
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
    except Exception as e:
        log.error(f"generate_signal: {e}")
        return None


# ════════════════════════════════════════════════════════════════
#                    MESSAGE FORMATTING
# ════════════════════════════════════════════════════════════════

def fmt_signal(s: Dict) -> str:
    em = "🟢" if s["direction"] == "BUY" else "🔴"
    ar = "📈" if s["direction"] == "BUY" else "📉"
    rr = round(s["tp_pips"] / s["sl_pips"], 2) if s["sl_pips"] > 0 else 0
    return (
        f"{em} <b>{s['direction']} SIGNAL — EUR/USD</b> {ar}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💯 <b>Score:</b> {s['score']}/100 (KUCHLI)\n"
        f"📊 <b>Trend:</b> {s['trend']}\n"
        f"✅ <b>Confirms:</b> {s['confirms'].replace(',', ', ')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Entry:</b>  <code>{s['entry']:.5f}</code>\n"
        f"🛡 <b>SL:</b>     <code>{s['sl']:.5f}</code> ({s['sl_pips']} pip)\n"
        f"🎯 <b>TP:</b>     <code>{s['tp']:.5f}</code> ({s['tp_pips']} pip)\n"
        f"⚖️ <b>RR:</b>     1 : {rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Lot:</b> {s['lot']}\n"
        f"📈 <b>RSI:</b> {s['rsi']}\n"
        f"🌊 <b>ATR:</b> {s['atr_pips']} pip\n"
        f"⏰ <b>Time:</b> {s['time']} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>MT5 ilovasida shu darajalarda savdo oching</i>"
    )


def fmt_close(t: Dict, exit_price: float, pips: float,
              usd: float, status: str) -> str:
    em = "✅" if status == "CLOSED_TP" else "❌"
    title = "TP HIT — FOYDA" if status == "CLOSED_TP" else "SL HIT — ZARAR"
    duration = ""
    try:
        opened = datetime.fromisoformat(t["open_time"])
        delta = datetime.now(timezone.utc) - opened
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        duration = f"{h}s {m}d" if h else f"{m}d"
    except Exception:
        pass
    return (
        f"{em} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Trade #{t['id']} | {t['direction']}</b>\n"
        f"📍 Entry: <code>{t['entry']:.5f}</code>\n"
        f"🚪 Exit:  <code>{exit_price:.5f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>P/L: {pips:+.1f} pip | ${usd:+.2f}</b>\n"
        f"⏱ Davomiyligi: {duration}"
    )


# ════════════════════════════════════════════════════════════════
#                    TELEGRAM KEYBOARD
# ════════════════════════════════════════════════════════════════

def make_kb() -> InlineKeyboardMarkup:
    running = db_get("running") == "1"
    row1 = ([InlineKeyboardButton("⏸ STOP", callback_data="stop")]
            if running else
            [InlineKeyboardButton("▶️ START", callback_data="start")])
    return InlineKeyboardMarkup([
        row1,
        [
            InlineKeyboardButton("📊 STATS", callback_data="stats"),
            InlineKeyboardButton("📋 OPEN", callback_data="open"),
        ],
        [InlineKeyboardButton("ℹ️ HELP", callback_data="help")],
    ])


# ════════════════════════════════════════════════════════════════
#                    TELEGRAM HANDLERS
# ════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_CHAT_ID:
        await update.message.reply_text(
            f"⛔ Bu bot faqat egasi uchun.\n"
            f"Sizning ID: <code>{user_id}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    running = db_get("running") == "1"
    status = "🟢 ISHLAMOQDA" if running else "🔴 TO'XTAGAN"
    await update.message.reply_text(
        f"🎯 <b>VULKXAN FOREX SIGNAL BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Juftlik: EUR/USD\n"
        f"⚙️ Strategiya: Structure + Confirmation + ATR\n"
        f"💯 Min Score: {MIN_SCORE}/100\n"
        f"⏰ Sessiya: {SESSION_START}:00–{SESSION_END}:00 UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Holat: {status}\n"
        f"━━━━━━━━━━━━━━━━━━━━",
        reply_markup=make_kb(),
        parse_mode=ParseMode.HTML
    )


async def cb_handler(update: Update, ctx) -> None:
    q = update.callback_query
    if q.from_user.id != OWNER_CHAT_ID:
        await q.answer("⛔ Sizga ruxsat yo'q", show_alert=True)
        return
    await q.answer()
    a = q.data

    if a == "start":
        db_set("running", "1")
        await q.edit_message_text(
            "🟢 <b>BOT ISHGA TUSHDI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Bozorni kuzatish boshlandi\n"
            f"📊 Min score: {MIN_SCORE}/100\n"
            f"⏰ Sessiya: {SESSION_START}:00–{SESSION_END}:00 UTC\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 Kuchli signal topilsa xabar yuboraman\n"
            "💼 Savdo yopilganda natija habar keladi",
            reply_markup=make_kb(),
            parse_mode=ParseMode.HTML
        )

    elif a == "stop":
        db_set("running", "0")
        await q.edit_message_text(
            "🔴 <b>BOT TO'XTATILDI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⏸ Yangi signallar yuborilmaydi\n"
            "📊 Ochiq savdolar baribir kuzatiladi\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "▶️ Qayta yoqish uchun START bosing",
            reply_markup=make_kb(),
            parse_mode=ParseMode.HTML
        )

    elif a == "stats":
        s = db_stats()
        active = float(db_get("active", str(ACTIVE_CAPITAL)))
        reserve = float(db_get("reserve", str(RESERVE_CAPITAL)))
        await q.edit_message_text(
            f"📊 <b>STATISTIKA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Jami savdolar: {s['total']}\n"
            f"✅ G'alabalar: {s['wins']}\n"
            f"❌ Mag'lubiyatlar: {s['losses']}\n"
            f"🎯 Win Rate: {s['wr']:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Jami P/L: ${s['pnl']:+.2f}\n"
            f"📏 Jami pips: {s['pips']:+.1f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 Aktiv kapital: ${active:.2f}\n"
            f"🔒 Rezerv: ${reserve:.2f}\n"
            f"📋 Ochiq savdolar: {s['opens']}",
            reply_markup=make_kb(),
            parse_mode=ParseMode.HTML
        )

    elif a == "open":
        trades = db_open_trades()
        if not trades:
            msg = (
                "📋 <b>OCHIQ SAVDOLAR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📭 Ochiq savdolar yo'q"
            )
        else:
            lines = [f"📋 <b>OCHIQ SAVDOLAR ({len(trades)})</b>",
                     "━━━━━━━━━━━━━━━━━━━━"]
            for t in trades:
                em = "🟢" if t["direction"] == "BUY" else "🔴"
                lines.append(
                    f"{em} #{t['id']} {t['direction']}\n"
                    f"   Entry: <code>{t['entry']:.5f}</code>\n"
                    f"   SL: <code>{t['sl']:.5f}</code> | "
                    f"TP: <code>{t['tp']:.5f}</code>"
                )
            msg = "\n".join(lines)
        await q.edit_message_text(
            msg, reply_markup=make_kb(), parse_mode=ParseMode.HTML
        )

    elif a == "help":
        await q.edit_message_text(
            f"ℹ️ <b>YO'RIQNOMA</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>▶️ START</b> — kuzatishni boshlash\n"
            f"<b>⏸ STOP</b> — kuzatishni to'xtatish\n"
            f"<b>📊 STATS</b> — statistika\n"
            f"<b>📋 OPEN</b> — ochiq savdolar\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>QANDAY ISHLAYDI:</b>\n"
            f"1. START bosing\n"
            f"2. Bot {SESSION_START}:00-{SESSION_END}:00 UTC da kuzatadi\n"
            f"3. Kuchli signal topilsa Telegram'ga xabar\n"
            f"4. MT5 ilovangizda savdoni qo'lda ochasiz\n"
            f"5. TP/SL ga yetganda natija xabari\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>KUCHLI SIGNAL =</b>\n"
            f"• Trend mos (HH/HL yoki LH/LL)\n"
            f"• Kamida 2 ta tasdiqlash\n"
            f"• Score ≥ {MIN_SCORE}/100\n"
            f"• London-NY overlap\n"
            f"• ATR normal range",
            reply_markup=make_kb(),
            parse_mode=ParseMode.HTML
        )


# ════════════════════════════════════════════════════════════════
#                    BACKGROUND TASKS
# ════════════════════════════════════════════════════════════════

async def check_open_trades(bot) -> None:
    """Ochiq savdolarni tekshirib, TP/SL ga yetgan bo'lsa yopadi."""
    try:
        trades = db_open_trades()
        if not trades:
            return

        price = await get_current_price()
        if not price:
            return
        bid, ask = price
        mid = (bid + ask) / 2

        for t in trades:
            try:
                d = t["direction"]
                status = None
                exit_p = mid

                if d == "BUY":
                    if mid >= t["tp"]:
                        status = "CLOSED_TP"
                        exit_p = t["tp"]
                    elif mid <= t["sl"]:
                        status = "CLOSED_SL"
                        exit_p = t["sl"]
                else:
                    if mid <= t["tp"]:
                        status = "CLOSED_TP"
                        exit_p = t["tp"]
                    elif mid >= t["sl"]:
                        status = "CLOSED_SL"
                        exit_p = t["sl"]

                if not status:
                    continue

                # P/L
                if d == "BUY":
                    pips = (exit_p - t["entry"]) / PIP
                else:
                    pips = (t["entry"] - exit_p) / PIP
                usd = pips * t["lot"] * 10

                db_close_trade(t["id"], status, exit_p, pips, usd)

                # Capital update
                active = float(db_get("active", str(ACTIVE_CAPITAL)))
                db_set("active", active + usd)

                # Send notification
                msg = fmt_close(t, exit_p, pips, usd, status)
                await bot.send_message(
                    OWNER_CHAT_ID, msg, parse_mode=ParseMode.HTML
                )
                log.info(
                    f"Trade #{t['id']} yopildi | {status} | "
                    f"{pips:+.1f}pip | ${usd:+.2f}"
                )
            except Exception as e:
                log.error(f"check_trade #{t.get('id')}: {e}")
    except Exception as e:
        log.error(f"check_open_trades: {e}")


async def try_send_signal(bot) -> None:
    """Yangi signal qidirib yuboradi."""
    try:
        if len(db_open_trades()) >= 3:
            return

        sig = await generate_signal()
        if not sig:
            return

        msg = fmt_signal(sig)
        await bot.send_message(
            OWNER_CHAT_ID, msg, parse_mode=ParseMode.HTML
        )
        tid = db_save_trade(sig)
        db_set("last_signal", datetime.now(timezone.utc).isoformat())
        log.info(
            f"📨 Signal #{tid} | {sig['direction']} | "
            f"Score: {sig['score']}/100"
        )
    except Exception as e:
        log.error(f"try_send_signal: {e}")


async def monitor_loop(bot) -> None:
    """Asosiy monitoring loop."""
    log.info("📡 Monitor loop boshlandi")
    while True:
        try:
            if db_get("running") == "1":
                await check_open_trades(bot)
                await try_send_signal(bot)
            await asyncio.sleep(LOOP_SEC)
        except asyncio.CancelledError:
            log.info("Monitor loop bekor qilindi")
            break
        except Exception as e:
            log.error(f"Monitor loop: {e}")
            await asyncio.sleep(30)


async def self_ping_loop() -> None:
    """Render uxlatmaslik uchun har 14 daqiqada o'zini ping qiladi."""
    if not RENDER_URL:
        log.info("⚠️ RENDER_URL yo'q, self-ping o'chirilgan")
        return
    log.info(f"🏓 Self-ping har {SELF_PING_MIN} daqiqada")
    while True:
        try:
            await asyncio.sleep(SELF_PING_MIN * 60)
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{RENDER_URL}/health",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    log.info(f"🏓 Self-ping: HTTP {r.status}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Self-ping: {e}")


# ════════════════════════════════════════════════════════════════
#                    HEALTH SERVER (HTTP)
# ════════════════════════════════════════════════════════════════

async def start_health_server():
    from aiohttp import web

    async def health(req):
        return web.Response(
            text=json.dumps({
                "status": "ok",
                "bot": "vulkxan",
                "running": db_get("running") == "1",
                "time": datetime.now(timezone.utc).isoformat(),
            }),
            content_type="application/json"
        )

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"✅ Health server: port {PORT}")
    return runner


# ════════════════════════════════════════════════════════════════
#                         MAIN
# ════════════════════════════════════════════════════════════════

async def main() -> None:
    db_init()

    log.info("═" * 55)
    log.info("  🎯 VULKXAN FOREX SIGNAL BOT")
    log.info(f"  📊 Symbol: {DISPLAY}")
    log.info(f"  💯 Min Score: {MIN_SCORE}/100")
    log.info(f"  ⏰ Session: {SESSION_START}:00-{SESSION_END}:00 UTC")
    log.info("═" * 55)

    webhook_url = f"{RENDER_URL}/{TELEGRAM_TOKEN}"

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))

    await app.initialize()
    await app.start()

    # Welcome
    try:
        await app.bot.send_message(
            OWNER_CHAT_ID,
            "🎯 <b>VULKXAN BOT ONLAYN</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Server ishlamoqda\n"
            "📊 EUR/USD kuzatuvga tayyor\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Boshlash uchun /start bosing",
            parse_mode=ParseMode.HTML
        )
        log.info("✅ Welcome yuborildi")
    except Exception as e:
        log.warning(f"Welcome: {e}")

    # Background tasks
    monitor_task = asyncio.create_task(monitor_loop(app.bot))
    ping_task = asyncio.create_task(self_ping_loop())

    # Webhook yoki Polling
    if RENDER_URL:
        log.info(f"🌐 Webhook rejimi: {webhook_url}")
        await app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        from aiohttp import web

        async def telegram_webhook(req):
            try:
                data = await req.json()
                update = Update.de_json(data, app.bot)
                await app.process_update(update)
            except Exception as e:
                log.error(f"Webhook: {e}")
            return web.Response(text="ok")

        async def health(req):
            return web.Response(
                text=json.dumps({
                    "status": "ok",
                    "running": db_get("running") == "1",
                    "time": datetime.now(timezone.utc).isoformat(),
                }),
                content_type="application/json"
            )

        web_app = web.Application()
        web_app.router.add_get("/", health)
        web_app.router.add_get("/health", health)
        web_app.router.add_post(f"/{TELEGRAM_TOKEN}", telegram_webhook)

        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"✅ Web server port {PORT}")
        log.info("✅ Bot to'liq ishlamoqda (Webhook)")

        try:
            await asyncio.Event().wait()
        finally:
            monitor_task.cancel()
            ping_task.cancel()
            await app.bot.delete_webhook()
            await app.stop()
            await app.shutdown()
            await runner.cleanup()
    else:
        log.info("📡 Polling rejimi (RENDER_URL yo'q)")
        try:
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("✅ Bot to'liq ishlamoqda (Polling)")
            await asyncio.Event().wait()
        finally:
            monitor_task.cancel()
            ping_task.cancel()
            try:
                await app.updater.stop()
            except Exception:
                pass
            await app.stop()
            await app.shutdown()
        log.info("👋 Bot to'xtadi")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
