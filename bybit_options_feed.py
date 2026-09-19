"""
bybit_options_feed.py
----------------------
Real options context for XAUT (Tether Gold) via Bybit's public v5 API -
free, no key, no auth.

WHY BYBIT AND NOT DERIBIT FOR THIS ONE: Deribit has no options market on
XAUT at all (it only added options on PAXG, a different gold token, in
Dec 2024). Bybit launched a dedicated, market-maker-backed XAUT options
market in June 2026, settled in USDT. That's the only real, live XAUT
options venue this bot knows of - if that ever changes, add the right
currency/base-coin mapping below.

Same philosophy as deribit_options_feed.py: this does NOT score the
option. It fetches the real nearest at-the-money contract matching the
spot signal's direction and reports its actual live premium and implied
vol as context, never a synthetic number.

DELIBERATELY SINGLE-ENDPOINT: everything needed - which contracts exist,
their strike/expiry/type, live mark price, underlying price, and IV -
comes back from ONE call to GET /v5/market/tickers?category=option. The
strike/expiry/type are parsed out of Bybit's option symbol string itself
(e.g. "XAUT-29AUG26-4400-C"), so this never needs a second endpoint (like
instruments-info) or a guess at what Bybit calls the XAUT spot/perp
symbol just to find a current price - underlyingPrice is already on every
option ticker. Fewer calls, fewer places for a wrong guess to silently
break the feature.

Key difference from Deribit's contracts: Bybit's XAUT options are
USDT-settled, so the premium Bybit quotes (markPrice) is already in USD
terms - unlike Deribit's BTC/ETH options, which quote premium in units of
the underlying coin. mark_price_underlying is therefore left as None here
(nothing meaningful to put there) - telegram_notify.py handles that.
"""

import logging
import time
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger("bybit_options_feed")

BYBIT_API_BASE = "https://api.bybit.com/v5/market"

# Maps your Binance spot pairs to Bybit's option base-coin codes. Only
# coins with a genuine, currently-listed options market belong here.
SYMBOL_TO_BYBIT_BASECOIN = {
    "XAUTUSDT": "XAUT",
}


def _get(endpoint: str, params: dict, retry: bool = True):
    if config.DRY_RUN:
        return None  # handled by mock functions below
    try:
        resp = requests.get(f"{BYBIT_API_BASE}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") != 0:
            log.warning(f"[BYBIT] {endpoint} returned retCode={data.get('retCode')}: {data.get('retMsg')}")
            return None
        return data.get("result")
    except Exception as e:
        if retry:
            log.warning(f"[BYBIT] {endpoint} failed, retrying once in 2s: {e}")
            time.sleep(2)
            return _get(endpoint, params, retry=False)
        log.error(f"[BYBIT] {endpoint} failed after retry: {e}")
        return None


def _parse_symbol(sym: str):
    """
    Bybit option symbols look like "XAUT-29AUG26-4400-C": base, expiry
    (DDMMMYY), strike, C/P. Returns (expiry_dt, strike, option_letter) or
    None if the symbol doesn't match that shape (defensive - a malformed
    or unexpected symbol is just skipped, never crashes the scan).
    """
    parts = sym.split("-")
    if len(parts) != 4:
        return None
    _, expiry_str, strike_str, opt_letter = parts
    if opt_letter not in ("C", "P"):
        return None
    try:
        # Bybit options expire 08:00 UTC on the listed date.
        expiry_dt = datetime.strptime(expiry_str, "%d%b%y").replace(
            tzinfo=timezone.utc, hour=8
        )
        strike = float(strike_str)
    except ValueError:
        return None
    return expiry_dt, strike, opt_letter


def get_atm_option(symbol: str, direction: str):
    """
    Returns the nearest-expiry, closest-to-the-money XAUT option matching
    the signal direction (CALL for LONG, PUT for SHORT), or None if this
    symbol has no options market on Bybit (any coin outside
    SYMBOL_TO_BYBIT_BASECOIN, or a quiet period with no active listings).

    Returns the same dict shape as deribit_options_feed.get_atm_option,
    plus a "venue" key: {instrument_name, option_type, strike,
    expiry_date, days_to_expiry, mark_price_underlying, mark_price_usd,
    mark_iv, underlying_price, venue}
    """
    base_coin = SYMBOL_TO_BYBIT_BASECOIN.get(symbol)
    if not base_coin:
        return None  # no real options market for this coin - not an error

    if config.DRY_RUN:
        return _mock_option(base_coin, direction)

    option_letter = "C" if direction == "LONG" else "P"

    tickers = _get("tickers", {"category": "option", "baseCoin": base_coin})
    if not tickers or not tickers.get("list"):
        log.info(f"[BYBIT] No active option tickers listed for {base_coin} right now")
        return None

    now = datetime.now(timezone.utc)
    candidates = []  # (ticker_dict, expiry_dt, strike)
    for t in tickers["list"]:
        parsed = _parse_symbol(t.get("symbol", ""))
        if not parsed:
            continue
        expiry_dt, strike, opt_letter = parsed
        if opt_letter != option_letter or expiry_dt < now:
            continue
        candidates.append((t, expiry_dt, strike))

    if not candidates:
        return None

    nearest_expiry = min(c[1] for c in candidates)
    same_expiry = [c for c in candidates if c[1] == nearest_expiry]

    def _underlying(t):
        try:
            v = t.get("underlyingPrice")
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    priced = [(t, exp, strike, _underlying(t)) for t, exp, strike in same_expiry]
    with_underlying = [row for row in priced if row[3] is not None]

    if with_underlying:
        chosen_t, _, chosen_strike, spot_price = min(
            with_underlying, key=lambda row: abs(row[2] - row[3])
        )
    else:
        same_expiry.sort(key=lambda row: row[2])
        chosen_t, _, chosen_strike = same_expiry[len(same_expiry) // 2]
        spot_price = None

    def _f(key):
        v = chosen_t.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None

    days_to_expiry = (nearest_expiry - now).days
    mark_price_usd = _f("markPrice")
    underlying_price = _f("underlyingPrice") or spot_price
    mark_iv_raw = _f("markPriceIv")
    if mark_iv_raw is None:
        mark_iv_raw = _f("markIv")
    mark_iv = mark_iv_raw * 100 if mark_iv_raw is not None else None  # Bybit reports IV as a fraction

    return {
        "instrument_name": chosen_t["symbol"],
        "option_type": "CALL" if option_letter == "C" else "PUT",
        "strike": chosen_strike,
        "expiry_date": nearest_expiry.strftime("%d %b %Y"),
        "days_to_expiry": max(days_to_expiry, 0),
        "mark_price_underlying": None,  # USDT-settled - premium is already USD, nothing to put here
        "mark_price_usd": mark_price_usd,
        "mark_iv": mark_iv,
        "underlying_price": underlying_price,
        "venue": "Bybit",
    }


def _mock_option(base_coin: str, direction: str):
    """DRY_RUN synthetic option data - no network calls."""
    import random
    base = {"XAUT": 4370}.get(base_coin, 100)
    spot = base * random.uniform(0.98, 1.02)
    strike = round(spot / 25) * 25
    return {
        "instrument_name": f"{base_coin}-MOCKEXP-{strike}-{'C' if direction == 'LONG' else 'P'}",
        "option_type": "CALL" if direction == "LONG" else "PUT",
        "strike": strike,
        "expiry_date": "mock expiry",
        "days_to_expiry": random.randint(1, 14),
        "mark_price_underlying": None,
        "mark_price_usd": round(spot * random.uniform(0.005, 0.03), 2),
        "mark_iv": round(random.uniform(10, 25), 1),
        "underlying_price": round(spot, 2),
        "venue": "Bybit",
    }
