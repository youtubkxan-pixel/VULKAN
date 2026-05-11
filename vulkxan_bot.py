"""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║         🎯  VULKXAN FOREX SIGNAL BOT                            ║
║         @vulkxan_bot — EURUSD Smart Signals                     ║
║                                                                  ║
║         Architecture: Structure + Confirmation + Filter         ║
║         Strategy: ATR-based with Stop-Hunt Protection           ║
║                                                                  ║
║         🌐 Platform: Render.com (BEPUL, VPS kerak emas)         ║
║         📱 Mobile: Faqat Telegram orqali boshqariladi           ║
║         💰 Broker: Kerak emas (yfinance + Twelve Data)          ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝

ISHLATISH:
  1. Telegramda @vulkxan_bot ga kiring
  2. /start bosing → "▶️ START" tugmasini bosing
  3. Bot bozorni kuzatadi va kuchli signallarda xabar yuboradi
  4. Savdo yopilganda natija haqida xabar keladi

RENDER.COM'DA O'RNATISH (BEPUL):
  1. github.com'da yangi repo yarating (public)
  2. Bu faylni `bot.py` deb yuklang
  3. `requirements.txt` faylini yuklang (pastda berilgan)
  4. render.com → New → Background Worker
  5. GitHub repo'ni ulang
  6. Environment Variables: TELEGRAM_TOKEN, CHAT_ID
  7. Deploy → Bot ishlaydi 24/7 BEPUL
"""

import asyncio
import logging
import sqlite3
import os
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Tuple, Any
from pathlib import Path

import numpy as np
import pandas as pd
import aiohttp
import yfinance as yf

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ApplicationBuilder,
)
from telegram.constants import ParseMode


# ════════════════════════════════════════════════════════════════════
#                          CONFIGURATION
# ════════════════════════════════════════════════════════════════════

class Config:
    # ─── TELEGRAM ───────────────────────────────────────────────────
    TELEGRAM_TOKEN: str = os.getenv(
        "TELEGRAM_TOKEN",
        "8799555582:AAETKBrTm6IxXmwkB55YxHA71B1CLjsQIC0"
    )
    OWNER_CHAT_ID: int = int(os.getenv("CHAT_ID", "7837984652"))

    # ─── INSTRUMENT ─────────────────────────────────────────────────
    SYMBOL: str = "EURUSD=X"            # Yahoo Finance format
    DISPLAY_SYMBOL: str = "EUR/USD"
    PIP_SIZE: float = 0.0001
    PIP_DIGITS: int = 5

    # ─── CAPITAL (Virtual) ──────────────────────────────────────────
    TOTAL_CAPITAL: float = 10_000.00
    ACTIVE_RATIO: float = 0.10
    RESERVE_RATIO: float = 0.90

    # ─── RISK ───────────────────────────────────────────────────────
    RISK_PER_TRADE: float = 0.01
    DAILY_LOSS_LIMIT: float = 0.05
    MAX_OPEN_POSITIONS: int = 3
    MAX_SPREAD_PIPS: float = 3.0

    # ─── INDICATORS ─────────────────────────────────────────────────
    ATR_PERIOD: int = 14
    RSI_PERIOD: int = 14
    RSI_OVERSOLD: float = 35.0
    RSI_OVERBOUGHT: float = 65.0
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9
    MA_FAST: int = 50
    MA_SLOW: int = 200
    SWING_LOOKBACK: int = 5

    # ─── ATR FILTER ─────────────────────────────────────────────────
    ATR_MIN_PIPS: float = 5.0
    ATR_MAX_PIPS: float = 30.0

    # ─── TP/SL (ATR Multipliers) ────────────────────────────────────
    SL_ATR_MULT: float = 1.0
    TP_ATR_MULT: float = 3.0
    EARLY_EXIT_PIPS: float = 2.0        # TP dan 2 pip oldin chiqish
    SL_BUFFER_PIPS: float = 2.0         # SL ni 2 pip kengaytirish

    # ─── SESSION (UTC) ──────────────────────────────────────────────
    SESSION_START_HOUR: int = 13         # London-NY overlap
    SESSION_END_HOUR: int = 17

    # ─── SIGNAL THRESHOLD ───────────────────────────────────────────
    MIN_SIGNAL_SCORE: int = 75           # KUCHLI signallar uchun yuqori bar

    # ─── KELLY ──────────────────────────────────────────────────────
    KELLY_DIVISOR: float = 2.5
    KELLY_MIN_TRADES: int = 30
    KELLY_DEFAULT: float = 0.01

    # ─── TIMING ─────────────────────────────────────────────────────
    LOOP_SECONDS: int = 60               # Har 60 soniyada tekshirish
    SIGNAL_COOLDOWN_MIN: int = 30        # Bir signal'dan keyin 30 min kutish
    DATA_BARS: int = 200

    # ─── DATABASE ───────────────────────────────────────────────────
    DB_PATH: str = os.getenv("DB_PATH", "vulkxan_bot.db")

    # ─── LOG ────────────────────────────────────────────────────────
    LOG_LEVEL: int = logging.INFO


# ════════════════════════════════════════════════════════════════════
#                              LOGGER
# ════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=Config.LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
# Telegram lib loglarini kamaytirish
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("VulkxanBot")


# ════════════════════════════════════════════════════════════════════
#                              ENUMS
# ════════════════════════════════════════════════════════════════════

class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TrendType(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGING = "RANGING"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED_TP = "CLOSED_TP"
    CLOSED_SL = "CLOSED_SL"


@dataclass
class Signal:
    direction: Direction
    score: int
    trend: TrendType
    confirmations: List[str]
    entry: float
    real_sl: float
    real_tp: float
    sl_pips: float
    tp_pips: float
    rsi: float
    atr_pips: float
    lot_size: float
    timestamp: datetime


# ════════════════════════════════════════════════════════════════════
#                            DATABASE
# ════════════════════════════════════════════════════════════════════

class Database:
    def __init__(self, path: str = Config.DB_PATH):
        self.path = path
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
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                sl_pips REAL NOT NULL,
                tp_pips REAL NOT NULL,
                lot_size REAL NOT NULL,
                score INTEGER NOT NULL,
                confirmations TEXT,
                status TEXT NOT NULL,
                open_time TEXT NOT NULL,
                close_time TEXT,
                close_price REAL,
                pnl_pips REAL DEFAULT 0,
                pnl_usd REAL DEFAULT 0,
                message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_status ON trades(status);
            """)

            # Default state
            c.execute("""
                INSERT OR IGNORE INTO state(key,value) VALUES
                ('running','0'),
                ('active_capital', ?),
                ('reserve_capital', ?),
                ('last_signal_time','')
            """, (
                str(Config.TOTAL_CAPITAL * Config.ACTIVE_RATIO),
                str(Config.TOTAL_CAPITAL * Config.RESERVE_RATIO),
            ))

    def get(self, key: str, default: str = "") -> str:
        try:
            with self._conn() as c:
                r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
                return r["value"] if r else default
        except Exception as e:
            log.error(f"DB get: {e}")
            return default

    def set(self, key: str, value: str):
        try:
            with self._conn() as c:
                c.execute("""
                    INSERT INTO state(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """, (key, value))
        except Exception as e:
            log.error(f"DB set: {e}")

    def save_trade(self, s: Signal, message_id: Optional[int]) -> int:
        try:
            with self._conn() as c:
                cur = c.execute("""
                    INSERT INTO trades (direction, entry, sl, tp, sl_pips, tp_pips,
                                       lot_size, score, confirmations, status,
                                       open_time, message_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    s.direction.value, s.entry, s.real_sl, s.real_tp,
                    s.sl_pips, s.tp_pips, s.lot_size, s.score,
                    ",".join(s.confirmations), TradeStatus.OPEN.value,
                    s.timestamp.isoformat(), message_id
                ))
                return cur.lastrowid
        except Exception as e:
            log.error(f"Save trade: {e}")
            return -1

    def close_trade(self, trade_id: int, status: TradeStatus,
                    close_price: float, pnl_pips: float, pnl_usd: float):
        try:
            with self._conn() as c:
                c.execute("""
                    UPDATE trades
                    SET status=?, close_time=?, close_price=?,
                        pnl_pips=?, pnl_usd=?
                    WHERE id=?
                """, (status.value, datetime.now(timezone.utc).isoformat(),
                      close_price, pnl_pips, pnl_usd, trade_id))
        except Exception as e:
            log.error(f"Close trade: {e}")

    def get_open_trades(self) -> List[Dict]:
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT * FROM trades WHERE status='OPEN' ORDER BY id"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            log.error(f"Open trades: {e}")
            return []

    def get_stats(self) -> Dict:
        try:
            with self._conn() as c:
                total = c.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE status!='OPEN'"
                ).fetchone()["n"]
                wins = c.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE pnl_usd>0"
                ).fetchone()["n"]
                losses = c.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE pnl_usd<0"
                ).fetchone()["n"]
                pnl = c.execute(
                    "SELECT COALESCE(SUM(pnl_usd),0) as s FROM trades"
                ).fetchone()["s"]
                pnl_pips = c.execute(
                    "SELECT COALESCE(SUM(pnl_pips),0) as s FROM trades"
                ).fetchone()["s"]
                open_count = c.execute(
                    "SELECT COUNT(*) as n FROM trades WHERE status='OPEN'"
                ).fetchone()["n"]
                wr = (wins / total * 100) if total > 0 else 0
                return {
                    "total": total, "wins": wins, "losses": losses,
                    "pnl_usd": pnl, "pnl_pips": pnl_pips,
                    "open": open_count, "win_rate": wr
                }
        except Exception as e:
            log.error(f"Stats: {e}")
            return {"total": 0, "wins": 0, "losses": 0, "pnl_usd": 0,
                    "pnl_pips": 0, "open": 0, "win_rate": 0}


# ════════════════════════════════════════════════════════════════════
#                          DATA PROVIDER
# ════════════════════════════════════════════════════════════════════

class DataProvider:
    """Yahoo Finance dan EURUSD ma'lumotlari (BEPUL, broker kerak emas)."""

    @staticmethod
    async def get_rates(symbol: str = Config.SYMBOL,
                        interval: str = "5m",
                        bars: int = Config.DATA_BARS) -> Optional[pd.DataFrame]:
        """Yahoo Finance dan OHLC data oladi."""
        try:
            # Async wrapper for yfinance
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(
                None,
                lambda: yf.download(
                    symbol,
                    period="5d" if interval in ("5m", "15m") else "60d",
                    interval=interval,
                    progress=False,
                    auto_adjust=False,
                )
            )
            if df is None or df.empty:
                log.warning("yfinance: bo'sh DataFrame")
                return None

            # MultiIndex'ni tekshirish
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Standartlash
            df = df.rename(columns=str.lower)
            if "adj close" in df.columns:
                df = df.drop(columns=["adj close"])

            required = ["open", "high", "low", "close"]
            for col in required:
                if col not in df.columns:
                    log.error(f"Ustun yo'q: {col}")
                    return None

            df = df[required].dropna()
            df = df.tail(bars).reset_index(drop=True)
            return df
        except Exception as e:
            log.error(f"Data fetch: {e}")
            return None

    @staticmethod
    async def get_current_price() -> Optional[Tuple[float, float]]:
        """Returns (bid, ask) — sintetik 0.5 pip spread."""
        try:
            df = await DataProvider.get_rates(interval="1m", bars=5)
            if df is None or df.empty:
                return None
            mid = float(df["close"].iloc[-1])
            spread = Config.PIP_SIZE * 0.5
            return (mid - spread / 2, mid + spread / 2)
        except Exception as e:
            log.error(f"Current price: {e}")
            return None


# ════════════════════════════════════════════════════════════════════
#                          INDICATORS
# ════════════════════════════════════════════════════════════════════

class TA:
    @staticmethod
    def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high, low, close = df["high"], df["low"], df["close"]
        pc = close.shift(1)
        tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()],
                       axis=1).max(axis=1)
        return tr.rolling(period, min_periods=period).mean()

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        d = close.diff()
        g = d.clip(lower=0)
        l = -d.clip(upper=0)
        ag = g.ewm(alpha=1/period, adjust=False).mean()
        al = l.ewm(alpha=1/period, adjust=False).mean()
        rs = ag / al.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(close: pd.Series, fast=12, slow=26, signal=9):
        ef = close.ewm(span=fast, adjust=False).mean()
        es = close.ewm(span=slow, adjust=False).mean()
        line = ef - es
        sig = line.ewm(span=signal, adjust=False).mean()
        return line, sig, line - sig

    @staticmethod
    def sma(close: pd.Series, period: int) -> pd.Series:
        return close.rolling(period, min_periods=period).mean()

    @staticmethod
    def swings(df: pd.DataFrame, lookback: int = 5) -> Tuple[List[int], List[int]]:
        highs = df["high"].values
        lows = df["low"].values
        sh, sl = [], []
        for i in range(lookback, len(df) - lookback):
            window_h = highs[i - lookback:i + lookback + 1]
            window_l = lows[i - lookback:i + lookback + 1]
            if highs[i] == window_h.max():
                sh.append(i)
            if lows[i] == window_l.min():
                sl.append(i)
        return sh, sl


# ════════════════════════════════════════════════════════════════════
#                          PATTERNS
# ════════════════════════════════════════════════════════════════════

class Patterns:
    @staticmethod
    def bull_pin(c: pd.Series) -> bool:
        body = abs(c["close"] - c["open"])
        upper = c["high"] - max(c["close"], c["open"])
        lower = min(c["close"], c["open"]) - c["low"]
        rng = c["high"] - c["low"]
        if rng <= 0 or body <= 0:
            return False
        return lower >= 2 * body and lower / rng >= 0.6 and upper / rng <= 0.2

    @staticmethod
    def bear_pin(c: pd.Series) -> bool:
        body = abs(c["close"] - c["open"])
        upper = c["high"] - max(c["close"], c["open"])
        lower = min(c["close"], c["open"]) - c["low"]
        rng = c["high"] - c["low"]
        if rng <= 0 or body <= 0:
            return False
        return upper >= 2 * body and upper / rng >= 0.6 and lower / rng <= 0.2

    @staticmethod
    def bull_engulf(prev: pd.Series, curr: pd.Series) -> bool:
        return (prev["close"] < prev["open"] and
                curr["close"] > curr["open"] and
                curr["close"] > prev["open"] and
                curr["open"] < prev["close"])

    @staticmethod
    def bear_engulf(prev: pd.Series, curr: pd.Series) -> bool:
        return (prev["close"] > prev["open"] and
                curr["close"] < curr["open"] and
                curr["close"] < prev["open"] and
                curr["open"] > prev["close"])


# ════════════════════════════════════════════════════════════════════
#                       STRUCTURE / CONFIRMATION
# ════════════════════════════════════════════════════════════════════

class Structure:
    @staticmethod
    def trend(df: pd.DataFrame, lookback: int = Config.SWING_LOOKBACK) -> TrendType:
        try:
            sh, sl = TA.swings(df, lookback)
            if len(sh) < 2 or len(sl) < 2:
                return TrendType.RANGING

            highs = [df["high"].iloc[i] for i in sh[-2:]]
            lows = [df["low"].iloc[i] for i in sl[-2:]]

            hh = highs[1] > highs[0]
            hl = lows[1] > lows[0]
            lh = highs[1] < highs[0]
            ll = lows[1] < lows[0]

            if hh and hl:
                return TrendType.UPTREND
            if lh and ll:
                return TrendType.DOWNTREND
            return TrendType.RANGING
        except Exception as e:
            log.error(f"Trend: {e}")
            return TrendType.RANGING


class Confirmations:
    @staticmethod
    def for_buy(df: pd.DataFrame) -> List[str]:
        conf = []
        try:
            close = df["close"]
            rsi = TA.rsi(close, Config.RSI_PERIOD)
            _, _, hist = TA.macd(close, Config.MACD_FAST, Config.MACD_SLOW,
                                  Config.MACD_SIGNAL)
            ma_f = TA.sma(close, Config.MA_FAST)
            ma_s = TA.sma(close, Config.MA_SLOW)

            # RSI
            if not rsi.empty and not pd.isna(rsi.iloc[-1]):
                r = rsi.iloc[-1]
                if r < Config.RSI_OVERSOLD:
                    conf.append("RSI_OVERSOLD")
                elif r < 50 and len(close) >= 10:
                    # Bullish divergence
                    pl_now = close.iloc[-5:].min()
                    pl_prev = close.iloc[-10:-5].min()
                    rl_now = rsi.iloc[-5:].min()
                    rl_prev = rsi.iloc[-10:-5].min()
                    if pl_now < pl_prev and rl_now > rl_prev:
                        conf.append("RSI_BULL_DIV")

            # MACD
            if len(hist) >= 2 and not pd.isna(hist.iloc[-1]) and not pd.isna(hist.iloc[-2]):
                if hist.iloc[-2] < 0 < hist.iloc[-1]:
                    conf.append("MACD_BULL_CROSS")
                elif hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]:
                    conf.append("MACD_BULL_MOMENTUM")

            # Patterns
            if Patterns.bull_pin(df.iloc[-1]):
                conf.append("BULL_PIN")
            if len(df) >= 2 and Patterns.bull_engulf(df.iloc[-2], df.iloc[-1]):
                conf.append("BULL_ENGULF")

            # MA
            if (not ma_f.empty and not ma_s.empty
                    and not pd.isna(ma_f.iloc[-1]) and not pd.isna(ma_s.iloc[-1])):
                if (len(ma_f) >= 2 and not pd.isna(ma_f.iloc[-2])
                        and not pd.isna(ma_s.iloc[-2])
                        and ma_f.iloc[-1] > ma_s.iloc[-1]
                        and ma_f.iloc[-2] <= ma_s.iloc[-2]):
                    conf.append("MA_GOLDEN")
                elif ma_f.iloc[-1] > ma_s.iloc[-1]:
                    conf.append("MA_BULLISH")
        except Exception as e:
            log.error(f"Buy confirm: {e}")
        return conf

    @staticmethod
    def for_sell(df: pd.DataFrame) -> List[str]:
        conf = []
        try:
            close = df["close"]
            rsi = TA.rsi(close, Config.RSI_PERIOD)
            _, _, hist = TA.macd(close, Config.MACD_FAST, Config.MACD_SLOW,
                                  Config.MACD_SIGNAL)
            ma_f = TA.sma(close, Config.MA_FAST)
            ma_s = TA.sma(close, Config.MA_SLOW)

            if not rsi.empty and not pd.isna(rsi.iloc[-1]):
                r = rsi.iloc[-1]
                if r > Config.RSI_OVERBOUGHT:
                    conf.append("RSI_OVERBOUGHT")
                elif r > 50 and len(close) >= 10:
                    ph_now = close.iloc[-5:].max()
                    ph_prev = close.iloc[-10:-5].max()
                    rh_now = rsi.iloc[-5:].max()
                    rh_prev = rsi.iloc[-10:-5].max()
                    if ph_now > ph_prev and rh_now < rh_prev:
                        conf.append("RSI_BEAR_DIV")

            if len(hist) >= 2 and not pd.isna(hist.iloc[-1]) and not pd.isna(hist.iloc[-2]):
                if hist.iloc[-2] > 0 > hist.iloc[-1]:
                    conf.append("MACD_BEAR_CROSS")
                elif hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2]:
                    conf.append("MACD_BEAR_MOMENTUM")

            if Patterns.bear_pin(df.iloc[-1]):
                conf.append("BEAR_PIN")
            if len(df) >= 2 and Patterns.bear_engulf(df.iloc[-2], df.iloc[-1]):
                conf.append("BEAR_ENGULF")

            if (not ma_f.empty and not ma_s.empty
                    and not pd.isna(ma_f.iloc[-1]) and not pd.isna(ma_s.iloc[-1])):
                if (len(ma_f) >= 2 and not pd.isna(ma_f.iloc[-2])
                        and not pd.isna(ma_s.iloc[-2])
                        and ma_f.iloc[-1] < ma_s.iloc[-1]
                        and ma_f.iloc[-2] >= ma_s.iloc[-2]):
                    conf.append("MA_DEATH")
                elif ma_f.iloc[-1] < ma_s.iloc[-1]:
                    conf.append("MA_BEARISH")
        except Exception as e:
            log.error(f"Sell confirm: {e}")
        return conf


# ════════════════════════════════════════════════════════════════════
#                          FILTERS
# ════════════════════════════════════════════════════════════════════

class Filters:
    @staticmethod
    def session_active() -> bool:
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:           # Shanba/Yakshanba
            return False
        return Config.SESSION_START_HOUR <= now.hour < Config.SESSION_END_HOUR

    @staticmethod
    def atr_ok(atr_pips: float) -> bool:
        return Config.ATR_MIN_PIPS <= atr_pips <= Config.ATR_MAX_PIPS

    @staticmethod
    def news_ok() -> bool:
        now = datetime.now(timezone.utc)
        # NFP — har oyning 1-jumasi 12:30-13:30 UTC
        if now.weekday() == 4 and now.day <= 7 and 11 <= now.hour <= 14:
            return False
        return True


# ════════════════════════════════════════════════════════════════════
#                       SCORING (100-BALL)
# ════════════════════════════════════════════════════════════════════

class Scorer:
    """
    100-ball:
      Trend:          25
      Confirmations:  30
      RSI:            15
      MACD:           15
      ATR:            10
      Session:         5
    """

    @staticmethod
    def score(df: pd.DataFrame, direction: Direction, trend: TrendType,
              conf: List[str], atr_pips: float) -> int:
        s = 0
        try:
            # Trend
            if direction == Direction.BUY and trend == TrendType.UPTREND:
                s += 25
            elif direction == Direction.SELL and trend == TrendType.DOWNTREND:
                s += 25
            else:
                return 0

            # Confirmations
            n = len(conf)
            if n >= 4:
                s += 30
            elif n == 3:
                s += 22
            elif n == 2:
                s += 15
            else:
                return 0

            # RSI
            close = df["close"]
            rsi = TA.rsi(close, Config.RSI_PERIOD).iloc[-1]
            if direction == Direction.BUY:
                if rsi < Config.RSI_OVERSOLD:
                    s += 15
                elif rsi < 50:
                    s += 10
                elif rsi < 60:
                    s += 5
            else:
                if rsi > Config.RSI_OVERBOUGHT:
                    s += 15
                elif rsi > 50:
                    s += 10
                elif rsi > 40:
                    s += 5

            # MACD
            _, _, hist = TA.macd(close)
            h_now = hist.iloc[-1]
            h_prev = hist.iloc[-2] if len(hist) >= 2 else 0
            if direction == Direction.BUY:
                if h_now > 0 and h_now > h_prev:
                    s += 15
                elif h_now > 0:
                    s += 10
                elif h_now > h_prev:
                    s += 5
            else:
                if h_now < 0 and h_now < h_prev:
                    s += 15
                elif h_now < 0:
                    s += 10
                elif h_now < h_prev:
                    s += 5

            # ATR
            if 8 <= atr_pips <= 20:
                s += 10
            elif 5 <= atr_pips < 8 or 20 < atr_pips <= 25:
                s += 5

            # Session
            h = datetime.now(timezone.utc).hour
            if 13 <= h < 15:
                s += 5
            elif 15 <= h < 17:
                s += 3
        except Exception as e:
            log.error(f"Score: {e}")
        return min(s, 100)


# ════════════════════════════════════════════════════════════════════
#                     KELLY + POSITION SIZER
# ════════════════════════════════════════════════════════════════════

class Kelly:
    @staticmethod
    def compute(trades: List[Dict]) -> float:
        try:
            if len(trades) < Config.KELLY_MIN_TRADES:
                return Config.KELLY_DEFAULT
            wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
            losses = [t for t in trades if t.get("pnl_usd", 0) < 0]
            if not wins or not losses:
                return Config.KELLY_DEFAULT
            p = len(wins) / len(trades)
            q = 1 - p
            aw = sum(t["pnl_usd"] for t in wins) / len(wins)
            al = abs(sum(t["pnl_usd"] for t in losses) / len(losses))
            if al == 0:
                return Config.KELLY_DEFAULT
            b = aw / al
            f = (b * p - q) / b
            safe = f / Config.KELLY_DIVISOR
            return max(0.005, min(safe, 0.025))
        except Exception as e:
            log.error(f"Kelly: {e}")
            return Config.KELLY_DEFAULT


class Sizer:
    @staticmethod
    def lot(active_capital: float, risk_pips: float,
            kelly: float = 0.01) -> float:
        try:
            if risk_pips <= 0:
                return 0.01
            risk_amount = active_capital * kelly
            pip_value = 10.0    # 1 standart lot EURUSD = $10/pip
            lot = risk_amount / (risk_pips * pip_value)
            lot = round(lot, 2)
            return max(0.01, min(lot, 5.00))
        except Exception:
            return 0.01


# ════════════════════════════════════════════════════════════════════
#                       SIGNAL ENGINE
# ════════════════════════════════════════════════════════════════════

class SignalEngine:
    def __init__(self, db: Database):
        self.db = db

    def _calc_levels(self, entry: float, atr: float, direction: Direction
                     ) -> Tuple[float, float, float, float]:
        """
        BUY:  SL = Entry - ATR×1.0 - 2pip (kengaytirilgan)
              TP = Entry + ATR×3.0 - 2pip (oldin chiqish)
        SELL: SL = Entry + ATR×1.0 + 2pip
              TP = Entry - ATR×3.0 + 2pip
        """
        buf = Config.SL_BUFFER_PIPS * Config.PIP_SIZE
        early = Config.EARLY_EXIT_PIPS * Config.PIP_SIZE
        digits = Config.PIP_DIGITS

        if direction == Direction.BUY:
            atr_sl = entry - atr * Config.SL_ATR_MULT
            atr_tp = entry + atr * Config.TP_ATR_MULT
            real_sl = round(atr_sl - buf, digits)
            real_tp = round(atr_tp - early, digits)
        else:
            atr_sl = entry + atr * Config.SL_ATR_MULT
            atr_tp = entry - atr * Config.TP_ATR_MULT
            real_sl = round(atr_sl + buf, digits)
            real_tp = round(atr_tp + early, digits)

        sl_pips = abs(entry - real_sl) / Config.PIP_SIZE
        tp_pips = abs(real_tp - entry) / Config.PIP_SIZE
        return real_sl, real_tp, sl_pips, tp_pips

    async def generate(self) -> Optional[Signal]:
        # 1. Filters
        if not Filters.session_active():
            return None
        if not Filters.news_ok():
            return None

        # 2. Cooldown
        last = self.db.get("last_signal_time", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                mins = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if mins < Config.SIGNAL_COOLDOWN_MIN:
                    return None
            except Exception:
                pass

        # 3. Data
        df_h1 = await DataProvider.get_rates(interval="1h", bars=200)
        df_m5 = await DataProvider.get_rates(interval="5m", bars=200)
        if df_h1 is None or df_m5 is None or len(df_m5) < 100:
            return None

        # 4. ATR
        atr_series = TA.atr(df_m5, Config.ATR_PERIOD)
        if atr_series.empty or pd.isna(atr_series.iloc[-1]):
            return None
        atr = float(atr_series.iloc[-1])
        atr_pips = atr / Config.PIP_SIZE
        if not Filters.atr_ok(atr_pips):
            return None

        # 5. Trend (H1)
        trend = Structure.trend(df_h1)
        if trend == TrendType.RANGING:
            return None

        # 6. Confirmations (M5)
        if trend == TrendType.UPTREND:
            conf = Confirmations.for_buy(df_m5)
            direction = Direction.BUY
        else:
            conf = Confirmations.for_sell(df_m5)
            direction = Direction.SELL

        if len(conf) < 2:
            return None

        # 7. Score
        score = Scorer.score(df_m5, direction, trend, conf, atr_pips)
        if score < Config.MIN_SIGNAL_SCORE:
            return None

        # 8. Entry & Levels
        price = await DataProvider.get_current_price()
        if not price:
            return None
        bid, ask = price
        entry = ask if direction == Direction.BUY else bid

        real_sl, real_tp, sl_pips, tp_pips = self._calc_levels(entry, atr, direction)

        # 9. Lot
        active = float(self.db.get("active_capital",
                                   str(Config.TOTAL_CAPITAL * Config.ACTIVE_RATIO)))
        lot = Sizer.lot(active, sl_pips, Config.KELLY_DEFAULT)

        # 10. RSI for display
        rsi = float(TA.rsi(df_m5["close"]).iloc[-1])

        return Signal(
            direction=direction,
            score=score,
            trend=trend,
            confirmations=conf,
            entry=entry,
            real_sl=real_sl,
            real_tp=real_tp,
            sl_pips=sl_pips,
            tp_pips=tp_pips,
            rsi=rsi,
            atr_pips=atr_pips,
            lot_size=lot,
            timestamp=datetime.now(timezone.utc),
        )


# ════════════════════════════════════════════════════════════════════
#                       MESSAGE FORMATTING
# ════════════════════════════════════════════════════════════════════

def format_signal(s: Signal) -> str:
    emoji = "🟢" if s.direction == Direction.BUY else "🔴"
    arrow = "📈" if s.direction == Direction.BUY else "📉"
    rr = round(s.tp_pips / s.sl_pips, 2) if s.sl_pips > 0 else 0

    return (
        f"{emoji} <b>{s.direction.value} SIGNAL — EUR/USD</b> {arrow}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💯 <b>Score:</b> {s.score}/100 (KUCHLI)\n"
        f"📊 <b>Trend:</b> {s.trend.value}\n"
        f"✅ <b>Confirms:</b> {', '.join(s.confirmations)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Entry:</b>  <code>{s.entry:.5f}</code>\n"
        f"🛡 <b>SL:</b>      <code>{s.real_sl:.5f}</code> ({s.sl_pips:.1f} pip)\n"
        f"🎯 <b>TP:</b>      <code>{s.real_tp:.5f}</code> ({s.tp_pips:.1f} pip)\n"
        f"⚖️ <b>RR:</b>      1 : {rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Lot:</b> {s.lot_size}\n"
        f"📈 <b>RSI:</b> {s.rsi:.1f}\n"
        f"🌊 <b>ATR:</b> {s.atr_pips:.1f} pip\n"
        f"⏰ <b>Time:</b> {s.timestamp.strftime('%H:%M')} UTC\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>MT5 ilovangizda shu darajalarda savdo oching</i>"
    )


def format_close(trade: Dict, exit_price: float, pnl_pips: float,
                 pnl_usd: float, status: TradeStatus) -> str:
    if status == TradeStatus.CLOSED_TP:
        emoji, title = "✅", "TP HIT — FOYDA"
    else:
        emoji, title = "❌", "SL HIT — ZARAR"

    duration = "?"
    try:
        opened = datetime.fromisoformat(trade["open_time"])
        delta = datetime.now(timezone.utc) - opened
        h = int(delta.total_seconds() // 3600)
        m = int((delta.total_seconds() % 3600) // 60)
        duration = f"{h}s {m}d" if h else f"{m}d"
    except Exception:
        pass

    return (
        f"{emoji} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Trade #{trade['id']}</b>\n"
        f"🔄 <b>Direction:</b> {trade['direction']}\n"
        f"📍 <b>Entry:</b>  <code>{trade['entry']:.5f}</code>\n"
        f"🚪 <b>Exit:</b>   <code>{exit_price:.5f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>P/L:</b> {pnl_pips:+.1f} pip | ${pnl_usd:+.2f}\n"
        f"⏱ <b>Davomiyligi:</b> {duration}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )


# ════════════════════════════════════════════════════════════════════
#                       TELEGRAM BOT
# ════════════════════════════════════════════════════════════════════

class VulkxanBot:
    def __init__(self):
        self.db = Database()
        self.engine = SignalEngine(self.db)
        self.app: Optional[Application] = None
        self.monitor_task: Optional[asyncio.Task] = None

    # ─── Keyboards ──────────────────────────────────────────────────

    def _main_kb(self) -> InlineKeyboardMarkup:
        running = self.db.get("running", "0") == "1"
        if running:
            row1 = [InlineKeyboardButton("⏸ STOP", callback_data="stop")]
        else:
            row1 = [InlineKeyboardButton("▶️ START", callback_data="start")]

        row2 = [
            InlineKeyboardButton("📊 STATS", callback_data="stats"),
            InlineKeyboardButton("📋 OPEN", callback_data="open"),
        ]
        row3 = [
            InlineKeyboardButton("ℹ️ HELP", callback_data="help"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3])

    # ─── Handlers ───────────────────────────────────────────────────

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != Config.OWNER_CHAT_ID:
            await update.message.reply_text(
                "⛔ Bu bot faqat egasi uchun.\n"
                f"Sizning Chat ID: <code>{user_id}</code>",
                parse_mode=ParseMode.HTML
            )
            return

        running = self.db.get("running", "0") == "1"
        status = "🟢 ISHLAMOQDA" if running else "🔴 TO'XTAGAN"

        msg = (
            f"🎯 <b>VULKXAN FOREX SIGNAL BOT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Juftlik:</b> EUR/USD\n"
            f"⚙️ <b>Strategiya:</b> Structure + Confirmation + ATR\n"
            f"💯 <b>Min Score:</b> {Config.MIN_SIGNAL_SCORE}/100 (faqat KUCHLI signallar)\n"
            f"⏰ <b>Sessiya:</b> {Config.SESSION_START_HOUR}:00–{Config.SESSION_END_HOUR}:00 UTC\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Holat:</b> {status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⬇️ Quyidagi tugmalardan birini bosing:"
        )
        await update.message.reply_text(
            msg, reply_markup=self._main_kb(), parse_mode=ParseMode.HTML
        )

    async def cb_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id

        if user_id != Config.OWNER_CHAT_ID:
            await query.answer("⛔ Sizga ruxsat yo'q", show_alert=True)
            return

        await query.answer()
        action = query.data

        if action == "start":
            self.db.set("running", "1")
            await query.edit_message_text(
                "🟢 <b>BOT ISHGA TUSHDI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Bozorni kuzatish boshlandi\n"
                f"📊 Min score: {Config.MIN_SIGNAL_SCORE}/100\n"
                f"⏰ Sessiya: {Config.SESSION_START_HOUR}:00–{Config.SESSION_END_HOUR}:00 UTC\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔔 Kuchli signal topilsa, xabar yuboraman.\n"
                "💼 Savdo ochilganda va yopilganda xabar olasiz.",
                reply_markup=self._main_kb(),
                parse_mode=ParseMode.HTML
            )

        elif action == "stop":
            self.db.set("running", "0")
            await query.edit_message_text(
                "🔴 <b>BOT TO'XTATILDI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "⏸ Yangi signallar yuborilmaydi\n"
                "📊 Ochiq savdolar baribir kuzatiladi\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "▶️ Qayta yoqish uchun START bosing",
                reply_markup=self._main_kb(),
                parse_mode=ParseMode.HTML
            )

        elif action == "stats":
            s = self.db.get_stats()
            active = float(self.db.get("active_capital", "1000"))
            reserve = float(self.db.get("reserve_capital", "9000"))
            msg = (
                f"📊 <b>STATISTIKA</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📈 <b>Jami savdolar:</b> {s['total']}\n"
                f"✅ <b>G'alabalar:</b> {s['wins']}\n"
                f"❌ <b>Mag'lubiyatlar:</b> {s['losses']}\n"
                f"🎯 <b>Win Rate:</b> {s['win_rate']:.1f}%\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Jami P/L:</b> ${s['pnl_usd']:+.2f}\n"
                f"📏 <b>Jami pips:</b> {s['pnl_pips']:+.1f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💼 <b>Aktiv kapital:</b> ${active:.2f}\n"
                f"🔒 <b>Rezerv:</b> ${reserve:.2f}\n"
                f"📋 <b>Ochiq savdolar:</b> {s['open']}"
            )
            await query.edit_message_text(
                msg, reply_markup=self._main_kb(), parse_mode=ParseMode.HTML
            )

        elif action == "open":
            trades = self.db.get_open_trades()
            if not trades:
                msg = (
                    f"📋 <b>OCHIQ SAVDOLAR</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📭 Ochiq savdolar yo'q"
                )
            else:
                lines = [f"📋 <b>OCHIQ SAVDOLAR ({len(trades)})</b>"]
                lines.append("━━━━━━━━━━━━━━━━━━━━━━")
                for t in trades:
                    em = "🟢" if t["direction"] == "BUY" else "🔴"
                    lines.append(
                        f"{em} #{t['id']} {t['direction']} | "
                        f"Entry: <code>{t['entry']:.5f}</code>\n"
                        f"   SL: <code>{t['sl']:.5f}</code> | "
                        f"TP: <code>{t['tp']:.5f}</code>"
                    )
                msg = "\n".join(lines)
            await query.edit_message_text(
                msg, reply_markup=self._main_kb(), parse_mode=ParseMode.HTML
            )

        elif action == "help":
            msg = (
                f"ℹ️ <b>YO'RIQNOMA</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>▶️ START</b> — bozorni kuzatishni boshlash\n"
                f"<b>⏸ STOP</b> — kuzatishni to'xtatish\n"
                f"<b>📊 STATS</b> — statistika ko'rish\n"
                f"<b>📋 OPEN</b> — ochiq savdolar ro'yxati\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>QANDAY ISHLAYDI:</b>\n"
                f"1. START bosing\n"
                f"2. Bot bozorni kuzatadi (London-NY session)\n"
                f"3. Kuchli signal topilsa, xabar yuboradi\n"
                f"4. Siz MT5 ilovasida savdo ochasiz\n"
                f"5. TP/SL ga yetganda yopiladi → xabar keladi\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>KUCHLI SIGNAL = ?</b>\n"
                f"• Trend mos (HH/HL yoki LH/LL)\n"
                f"• Kamida 2 ta tasdiqlash\n"
                f"• Score ≥ {Config.MIN_SIGNAL_SCORE}/100\n"
                f"• London-NY overlap sessiyasi\n"
                f"• ATR normal range"
            )
            await query.edit_message_text(
                msg, reply_markup=self._main_kb(), parse_mode=ParseMode.HTML
            )

    # ─── Monitor Loop ───────────────────────────────────────────────

    async def monitor_loop(self):
        """Asosiy bozor monitoring loop."""
        log.info("📡 Monitor loop ishga tushdi")
        while True:
            try:
                running = self.db.get("running", "0") == "1"

                if running:
                    # 1. Open trade'larni tekshirish
                    await self._check_open_trades()

                    # 2. Yangi signal qidirish (agar slot bo'sh bo'lsa)
                    open_count = len(self.db.get_open_trades())
                    if open_count < Config.MAX_OPEN_POSITIONS:
                        signal = await self.engine.generate()
                        if signal:
                            await self._send_signal(signal)

                await asyncio.sleep(Config.LOOP_SECONDS)
            except asyncio.CancelledError:
                log.info("Monitor loop bekor qilindi")
                break
            except Exception as e:
                log.error(f"Monitor loop: {e}")
                await asyncio.sleep(30)

    async def _send_signal(self, signal: Signal):
        try:
            msg = format_signal(signal)
            sent = await self.app.bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            trade_id = self.db.save_trade(signal, sent.message_id)
            self.db.set("last_signal_time", signal.timestamp.isoformat())
            log.info(f"📨 Signal yuborildi #{trade_id} | {signal.direction.value} | "
                     f"Score: {signal.score}")
        except Exception as e:
            log.error(f"Send signal: {e}")

    async def _check_open_trades(self):
        try:
            open_trades = self.db.get_open_trades()
            if not open_trades:
                return

            price = await DataProvider.get_current_price()
            if not price:
                return
            bid, ask = price
            mid = (bid + ask) / 2

            for t in open_trades:
                await self._check_trade(t, mid)
        except Exception as e:
            log.error(f"Check trades: {e}")

    async def _check_trade(self, t: Dict, price: float):
        try:
            direction = t["direction"]
            entry = t["entry"]
            sl = t["sl"]
            tp = t["tp"]
            lot = t["lot_size"]

            status: Optional[TradeStatus] = None
            exit_price = price

            if direction == "BUY":
                if price >= tp:
                    status = TradeStatus.CLOSED_TP
                    exit_price = tp
                elif price <= sl:
                    status = TradeStatus.CLOSED_SL
                    exit_price = sl
            else:
                if price <= tp:
                    status = TradeStatus.CLOSED_TP
                    exit_price = tp
                elif price >= sl:
                    status = TradeStatus.CLOSED_SL
                    exit_price = sl

            if not status:
                return

            # PnL hisoblash
            if direction == "BUY":
                pnl_pips = (exit_price - entry) / Config.PIP_SIZE
            else:
                pnl_pips = (entry - exit_price) / Config.PIP_SIZE
            pnl_usd = pnl_pips * lot * 10

            self.db.close_trade(t["id"], status, exit_price, pnl_pips, pnl_usd)

            # Capital update
            active = float(self.db.get("active_capital", "1000"))
            new_active = active + pnl_usd
            self.db.set("active_capital", str(new_active))

            # Xabar yuborish
            msg = format_close(t, exit_price, pnl_pips, pnl_usd, status)
            await self.app.bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            log.info(f"Trade #{t['id']} yopildi | {status.value} | "
                     f"{pnl_pips:+.1f} pip | ${pnl_usd:+.2f}")
        except Exception as e:
            log.error(f"Check trade: {e}")

    # ─── Startup ────────────────────────────────────────────────────

    async def on_startup(self, app: Application):
        log.info("🚀 Bot startup")

        # Bot commands menyusi
        await app.bot.set_my_commands([
            BotCommand("start", "Botni ochish / asosiy menyu"),
        ])

        # Welcome xabari
        try:
            await app.bot.send_message(
                chat_id=Config.OWNER_CHAT_ID,
                text=(
                    "🎯 <b>VULKXAN BOT ONLAYN</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "✅ Server ishlamoqda\n"
                    "📊 EUR/USD kuzatuvga tayyor\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "👇 Boshlash uchun /start bosing"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning(f"Welcome msg: {e}")

        # Monitor loop ishga tushirish
        self.monitor_task = asyncio.create_task(self.monitor_loop())

    async def on_shutdown(self, app: Application):
        log.info("🛑 Bot shutdown")
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass

    # ─── Main ───────────────────────────────────────────────────────

    def run(self):
        if not Config.TELEGRAM_TOKEN:
            log.error("TELEGRAM_TOKEN yo'q!")
            sys.exit(1)
        if not Config.OWNER_CHAT_ID:
            log.error("CHAT_ID yo'q!")
            sys.exit(1)

        self.app = (
            ApplicationBuilder()
            .token(Config.TELEGRAM_TOKEN)
            .post_init(self.on_startup)
            .post_shutdown(self.on_shutdown)
            .build()
        )

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CallbackQueryHandler(self.cb_handler))

        log.info("═" * 60)
        log.info("  🎯 VULKXAN FOREX SIGNAL BOT")
        log.info(f"  📊 Symbol: {Config.DISPLAY_SYMBOL}")
        log.info(f"  💯 Min Score: {Config.MIN_SIGNAL_SCORE}/100")
        log.info(f"  ⏰ Session: {Config.SESSION_START_HOUR}:00-{Config.SESSION_END_HOUR}:00 UTC")
        log.info("═" * 60)

        self.app.run_polling(allowed_updates=Update.ALL_TYPES)


# ════════════════════════════════════════════════════════════════════
#                          ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        bot = VulkxanBot()
        bot.run()
    except KeyboardInterrupt:
        log.info("👋 Bot to'xtadi")
    except Exception as e:
        log.error(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
