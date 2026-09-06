"""
config.py
---------
Crypto alert bot - runs 24/7 (no market-hours gate, unlike the NSE bots),
scanning every 5 minutes to match your 5-min trading timeframe.

Data source: Binance public API - free, no API key, no signup required for
market data. See binance_feed.py.
"""

import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

BOT_ID = "CRYPTO1"
BOT_NAME = "CRYPTO ALT-COIN ALERT BOT"
TELEGRAM_DISPLAY_NAME = "@CryptoAltAlertBot"

# ---------------------------------------------------------------------------
# Symbol universe - Binance trading pairs (base asset + USDT).
# Built directly into this file so a missing/uncommitted symbols.json can
# never silently shrink your monitored list (this bit the NSE version
# twice - fixed here from day one). bots_config/symbols.json, if present,
# OVERRIDES this list, so you can still edit symbols without touching code.
#
# NOTE: a handful of these tickers are uncertain as real Binance pairs at
# the time this was written (BDAG, VNI, AKE, ESPORTS, WLFI, LEO, LIT -
# some are unlisted/presale tokens, renamed, or exchange-specific). The
# app does NOT trust this list blindly - binance_feed.validate_symbols()
# checks every pair against Binance's live exchangeInfo at boot and
# automatically skips anything not currently tradeable, logging exactly
# which ones and why. Check the boot log after first deploy.
# ---------------------------------------------------------------------------
_BASE_TICKERS = [
    "TRX", "DOGE", "ADA", "XLM", "HBAR", "SUI", "SHIB", "CRO", "WLFI", "VET",
    "XEC", "GALA", "CELR", "RVN", "BDAG", "NEAR", "AAVE", "VNI", "DAI", "XMR",
    "MNT", "DOT", "LTC", "LEO", "BCH", "BTC", "ETH", "SOL", "XRP", "AVAX",
    "BNB", "LIT", "ENA", "ZEC", "AKE", "ESPORTS", "UNI", "LINK", "MATIC",
]
_BUILT_IN_SYMBOLS = [f"{t}USDT" for t in _BASE_TICKERS]

_SYMBOLS_PATH = BASE_DIR / "bots_config" / "symbols.json"
SYMBOLS = _BUILT_IN_SYMBOLS
try:
    with open(_SYMBOLS_PATH) as f:
        _override = json.load(f)
    if isinstance(_override, list) and len(_override) > 0:
        SYMBOLS = _override
        print(f"[config] Loaded {len(SYMBOLS)} symbols from bots_config/symbols.json (override)")
    else:
        print(f"[config] bots_config/symbols.json empty/invalid, using built-in list ({len(SYMBOLS)} symbols)")
except (FileNotFoundError, json.JSONDecodeError):
    print(f"[config] bots_config/symbols.json not found, using built-in list ({len(SYMBOLS)} symbols) - this is fine")

# ---------------------------------------------------------------------------
# Strict rule thresholds. Crypto is materially more volatile than NSE
# large/mid-caps, so the VWAP-distance bands are widened vs. the equity
# version - 2% on a large-cap stock and 2% on SHIB in a single 5-min
# candle are not comparable events. Tune these to taste; consider
# backtesting before trusting them with capital.
# ---------------------------------------------------------------------------
RSI_LONG_MIN, RSI_LONG_MAX = 40, 65
RSI_SHORT_MIN, RSI_SHORT_MAX = 35, 60
ADX_MIN = 25
VWAP_MAX_DISTANCE_PCT = 3.0          # widened from 2.0 (equities) for crypto volatility
CONFIDENCE_HIGH_PCT = 1.0            # widened from 0.5
CONFIDENCE_MEDIUM_PCT = 2.0          # widened from 1.5
VOLUME_LOOKBACK = 20
EMA_FAST, EMA_SLOW = 9, 21
RSI_PERIOD = 14
ADX_PERIOD = 14

SCORE_ALERT_THRESHOLD = 8
EARLY_SIGNAL_ENABLED = True
EARLY_SCORE_MIN = 6            # score 6-7/10 = building momentum, not yet confirmed (8+)
EARLY_COOLDOWN_HOURS = 0.5      # shorter than confirmed 2h - crypto moves fast on a 5-min
                                 # timeframe, so a developing setup can re-notify sooner
COOLDOWN_HOURS = 2
CANDLE_INTERVAL = "5m"                # Binance interval string (5-min timeframe as requested)
SCAN_INTERVAL_MINUTES = 5             # matches your trading timeframe

TIMEZONE = "UTC"                      # crypto has no single home timezone - UTC throughout
DAILY_SUMMARY_TIME = "23:55"          # UTC
ERROR_SUMMARY_TIME = "23:58"          # UTC
DAILY_RESET_TIME = "00:00"            # UTC - cooldown reset + fresh VWAP session

MAX_CONSECUTIVE_FAILS_BROKEN = 3
MAX_CONSECUTIVE_FAILS_DISABLE = 5

SQLITE_PATH = str(BASE_DIR / "data" / "alerts.db")

# ---------------------------------------------------------------------------
# Telegram - set in Render's Environment tab, NEVER in this file
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
PORT = int(os.environ.get("PORT", "10000"))


def validate_and_report():
    """Loud boot-time diagnostic - visible in Render's Logs tab immediately."""
    print("=" * 60)
    print(f"[config] BOT: {BOT_NAME}")
    print(f"[config] DRY_RUN: {DRY_RUN}")
    print(f"[config] Scan interval: every {SCAN_INTERVAL_MINUTES} min, 24/7 (no market-hours gate)")
    checks = [("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)]
    any_missing = False
    for name, value in checks:
        if value:
            print(f"[config]   {name}: SET (length {len(value)})")
        else:
            print(f"[config]   {name}: *** MISSING OR EMPTY *** - set this in Render > Environment")
            any_missing = True
    if any_missing and not DRY_RUN:
        print("[config] WARNING: Telegram env vars missing and DRY_RUN is false - alerts will fail until fixed.")
    print("=" * 60)


validate_and_report()
