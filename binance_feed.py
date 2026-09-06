"""
binance_feed.py
----------------
Real-time crypto data via Binance's PUBLIC API - completely free, no API
key, no signup, no rate-limit tier to worry about at this scale (39 symbols
polled every 5 minutes is a tiny fraction of Binance's generous public
rate limits: 1200 request-weight/min, klines cost ~1-2 weight each).

Two pieces:
  - REST `/api/v3/klines` for historical 5-min candles (needed to compute
    VWAP/EMA/RSI/ADX - a full intraday series, not just the latest price).
  - A combined websocket stream (`<symbol>@kline_5m` for every symbol,
    multiplexed into ONE connection) for live, sub-second price updates
    between candle closes - this is the "real-time" piece.

No login/auth needed anywhere in this file, unlike the NSE/Angel One
version - public market data on Binance requires none.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import requests

import config

log = logging.getLogger("binance_feed")

try:
    import websocket  # from websocket-client package
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    log.warning("websocket-client not installed - live tick updates disabled, "
                "falling back to REST-only (still works, just slightly less real-time).")

BINANCE_REST_BASE = "https://api.binance.com"
BINANCE_WS_BASE = "wss://stream.binance.com:9443"


class BinanceFeed:
    def __init__(self):
        self.valid_symbols = set()       # confirmed-tradeable Binance pairs, e.g. {"BTCUSDT", ...}
        self.live_price = {}             # "BTCUSDT" -> latest price (float)
        self._lock = threading.Lock()
        self.ws = None
        self.ws_thread = None

    # ------------------------------------------------------------------ #
    # Startup validation - confirms every requested symbol is a real,
    # currently-tradeable Binance pair BEFORE we try to poll it. Anything
    # not found is logged clearly and simply skipped, never crashes the app.
    # ------------------------------------------------------------------ #
    def validate_symbols(self, pairs: list) -> list:
        """
        pairs: list of Binance trading pair strings, e.g. ["BTCUSDT", "WLFIUSDT"]
        Returns the subset that Binance currently lists as TRADING.
        """
        try:
            resp = requests.get(f"{BINANCE_REST_BASE}/api/v3/exchangeInfo", timeout=15)
            resp.raise_for_status()
            data = resp.json()
            all_symbols = {
                s["symbol"] for s in data["symbols"] if s.get("status") == "TRADING"
            }
        except Exception as e:
            log.error(f"[BINANCE] Could not fetch exchangeInfo ({e}) - "
                      f"skipping validation, will attempt all requested pairs as-is")
            self.valid_symbols = set(pairs)
            return pairs

        valid = [p for p in pairs if p in all_symbols]
        invalid = [p for p in pairs if p not in all_symbols]
        self.valid_symbols = set(valid)

        if invalid:
            log.warning(
                f"[BINANCE] {len(invalid)} symbol(s) are NOT currently valid/tradeable "
                f"Binance pairs and will be SKIPPED: {invalid}. Common reasons: wrong "
                f"ticker (e.g. a coin renamed - MATIC -> POL in 2024), not listed on "
                f"Binance at all, or a presale/unlisted token (e.g. BDAG). Fix by "
                f"correcting the ticker in bots_config/symbols.json, or removing it."
            )
        log.info(f"[BINANCE] {len(valid)}/{len(pairs)} symbols validated and active")
        return valid

    # ------------------------------------------------------------------ #
    # Historical candles (REST) - used to compute all indicators.
    # VWAP for crypto (no single "market session") is anchored to the
    # current UTC calendar day, resetting at 00:00 UTC - the same
    # convention most crypto charting platforms use.
    # ------------------------------------------------------------------ #
    def get_candles(self, symbol: str, interval: str = None, retry: bool = True) -> pd.DataFrame:
        """
        Returns a DataFrame [timestamp, open, high, low, close, volume] for
        `symbol`, covering from the start of the current UTC day through
        now, sorted ascending. Empty DataFrame on failure (caller logs +
        skips - never raises).
        """
        interval = interval or config.CANDLE_INTERVAL

        if config.DRY_RUN:
            return self._mock_candles(symbol)

        try:
            now_utc = datetime.now(timezone.utc)
            day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            start_ms = int(day_start.timestamp() * 1000)

            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "limit": 1000,  # plenty for a full day of 5-min candles (288 max)
            }
            resp = requests.get(f"{BINANCE_REST_BASE}/api/v3/klines", params=params, timeout=10)
            resp.raise_for_status()
            rows = resp.json()

            if not rows:
                if retry:
                    log.warning(f"[BINANCE] Empty candle response for {symbol}, retrying once in 3s")
                    time.sleep(3)
                    return self.get_candles(symbol, interval, retry=False)
                log.error(f"[BINANCE] No candle data for {symbol} after retry")
                return pd.DataFrame()

            # Binance kline format: [openTime, open, high, low, close, volume, closeTime, ...]
            df = pd.DataFrame(rows, columns=[
                "timestamp", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_base", "taker_quote", "ignore"
            ])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            return df.sort_values("timestamp").reset_index(drop=True)

        except Exception as e:
            if retry:
                log.warning(f"[BINANCE] Exception fetching candles for {symbol}, retrying once in 3s: {e}")
                time.sleep(3)
                return self.get_candles(symbol, interval, retry=False)
            log.error(f"[BINANCE] Exception fetching candles for {symbol} after retry: {e}")
            return pd.DataFrame()

    def _mock_candles(self, symbol: str) -> pd.DataFrame:
        """Synthetic candle series for DRY_RUN testing - no network calls at all."""
        import numpy as np
        n = 80
        base = 1 + (hash(symbol) % 50000) / 100.0
        rng = np.random.default_rng(abs(hash(symbol)) % (2**32))
        closes = base + np.cumsum(rng.normal(0, base * 0.002, n))
        highs = closes + rng.uniform(0, base * 0.002, n)
        lows = closes - rng.uniform(0, base * 0.002, n)
        opens = closes - rng.normal(0, base * 0.001, n)
        volumes = rng.uniform(1000, 500000, n)
        now = datetime.now(timezone.utc)
        timestamps = [int((now.timestamp() - 300 * (n - i)) * 1000) for i in range(n)]
        return pd.DataFrame({
            "timestamp": timestamps, "open": opens, "high": highs,
            "low": lows, "close": closes, "volume": volumes,
        })

    # ------------------------------------------------------------------ #
    # Live websocket price feed - one multiplexed connection for all
    # symbols, using Binance's combined stream endpoint. Free, no auth.
    # ------------------------------------------------------------------ #
    def start_websocket(self, pairs: list):
        if config.DRY_RUN or not WEBSOCKET_AVAILABLE or not pairs:
            log.info("[BINANCE WS] Websocket not started (DRY_RUN, library missing, or no symbols)")
            return

        streams = "/".join(f"{p.lower()}@kline_{config.CANDLE_INTERVAL}" for p in pairs)
        url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

        def on_message(ws, message):
            try:
                data = json.loads(message)
                payload = data.get("data", {})
                k = payload.get("k", {})
                symbol = payload.get("s")
                close_price = k.get("c")
                if symbol and close_price:
                    with self._lock:
                        self.live_price[symbol] = float(close_price)
            except Exception as e:
                log.error(f"[BINANCE WS] on_message error: {e}")

        def on_error(ws, error):
            log.error(f"[BINANCE WS] Error: {error}")

        def on_close(ws, code, msg):
            log.warning(f"[BINANCE WS] Connection closed (code={code}), reconnecting in 5s...")
            time.sleep(5)
            self.start_websocket(pairs)  # auto-reconnect

        def on_open(ws):
            log.info(f"[BINANCE WS] Connected, streaming {len(pairs)} symbols")

        self.ws = websocket.WebSocketApp(
            url, on_message=on_message, on_error=on_error,
            on_close=on_close, on_open=on_open,
        )
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()
        log.info(f"[BINANCE WS] Websocket thread started for {len(pairs)} symbols")

    def get_live_price(self, symbol: str):
        with self._lock:
            return self.live_price.get(symbol)


feed = BinanceFeed()
