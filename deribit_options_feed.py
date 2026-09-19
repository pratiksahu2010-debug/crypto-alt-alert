"""
deribit_options_feed.py
-------------------------
Real crypto OPTIONS context via Deribit's public API - free, no key, no
auth, globally accessible. Deribit is the dominant crypto options venue.

SCOPE, DELIBERATELY LIMITED: crypto options only have meaningful liquidity
for BTC and ETH (SOL is listed on Deribit only intermittently, and this
code treats it as best-effort/optional - everything else in your 39-coin
spot list has no real options market to reference at all). This module
is never used for the other coins; they keep working exactly as before,
just without an options section in their alerts.

WHAT THIS DOES NOT DO: it does not run VWAP/RSI/ADX on an option's
premium. An option's price is driven by strike distance, time to expiry,
and implied volatility - not simple price-trending the way a spot/futures
price is. Instead, this fetches the REAL nearest at-the-money option
matching your existing spot signal's direction (call for LONG, put for
SHORT) and reports its actual live premium and implied volatility as
context alongside the spot signal - it doesn't invent a synthetic score.
"""

import logging
import time
from datetime import datetime, timezone

import requests

import config

log = logging.getLogger("deribit_feed")

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"

# Maps your Binance spot pairs to Deribit's currency codes. Only currencies
# with genuine, reliably-listed options markets belong here.
SYMBOL_TO_DERIBIT_CURRENCY = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
}


def _get(endpoint: str, params: dict, retry: bool = True):
    if config.DRY_RUN:
        return None  # handled by mock functions below
    try:
        resp = requests.get(f"{DERIBIT_API_BASE}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result")
    except Exception as e:
        if retry:
            log.warning(f"[DERIBIT] {endpoint} failed, retrying once in 2s: {e}")
            time.sleep(2)
            return _get(endpoint, params, retry=False)
        log.error(f"[DERIBIT] {endpoint} failed after retry: {e}")
        return None


def get_atm_option(symbol: str, direction: str):
    """
    Returns the nearest-expiry, closest-to-the-money option matching the
    signal direction (CALL for LONG, PUT for SHORT), or None if this
    symbol has no options market on Deribit (any coin outside
    SYMBOL_TO_DERIBIT_CURRENCY, or a currency Deribit doesn't currently
    list options for - e.g. SOL during a period with no active listings).

    Returns a dict: {instrument_name, strike, expiry_date, days_to_expiry,
    mark_price_underlying, mark_price_usd, mark_iv, underlying_price}
    """
    currency = SYMBOL_TO_DERIBIT_CURRENCY.get(symbol)
    if not currency:
        return None  # no real options market for this coin - not an error

    if config.DRY_RUN:
        return _mock_option(currency, direction)

    option_type = "call" if direction == "LONG" else "put"

    instruments = _get("get_instruments", {
        "currency": currency, "kind": "option", "expired": "false",
    })
    if not instruments:
        log.info(f"[DERIBIT] No active option instruments listed for {currency} right now")
        return None

    matching = [i for i in instruments if i.get("option_type") == option_type]
    if not matching:
        return None

    # Nearest expiry first
    matching.sort(key=lambda i: i["expiration_timestamp"])
    nearest_expiry = matching[0]["expiration_timestamp"]
    same_expiry = [i for i in matching if i["expiration_timestamp"] == nearest_expiry]

    # Need current spot to find the ATM strike - use the index price endpoint
    index = _get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
    spot_price = index.get("index_price") if index else None
    if spot_price is None:
        # Fall back to the middle-strike contract if index price is unavailable
        same_expiry.sort(key=lambda i: i["strike"])
        chosen = same_expiry[len(same_expiry) // 2]
    else:
        chosen = min(same_expiry, key=lambda i: abs(i["strike"] - spot_price))

    ticker = _get("ticker", {"instrument_name": chosen["instrument_name"]})
    if not ticker:
        return None

    expiry_dt = datetime.fromtimestamp(chosen["expiration_timestamp"] / 1000, tz=timezone.utc)
    days_to_expiry = (expiry_dt - datetime.now(timezone.utc)).days

    mark_price_underlying = ticker.get("mark_price")  # Deribit quotes options in units of the underlying (e.g. BTC)
    underlying_price = ticker.get("underlying_price") or spot_price
    mark_price_usd = (mark_price_underlying * underlying_price) if (mark_price_underlying and underlying_price) else None

    return {
        "instrument_name": chosen["instrument_name"],
        "option_type": option_type.upper(),
        "strike": chosen["strike"],
        "expiry_date": expiry_dt.strftime("%d %b %Y"),
        "days_to_expiry": max(days_to_expiry, 0),
        "mark_price_underlying": mark_price_underlying,
        "mark_price_usd": mark_price_usd,
        "mark_iv": ticker.get("mark_iv"),
        "underlying_price": underlying_price,
        "venue": "Deribit",
    }


def _mock_option(currency: str, direction: str):
    """DRY_RUN synthetic option data - no network calls."""
    import random
    base = {"BTC": 60000, "ETH": 3000}.get(currency, 100)
    spot = base * random.uniform(0.97, 1.03)
    strike = round(spot / 500) * 500 if currency == "BTC" else round(spot / 50) * 50
    return {
        "instrument_name": f"{currency}-MOCKEXP-{strike}-{'C' if direction == 'LONG' else 'P'}",
        "option_type": "CALL" if direction == "LONG" else "PUT",
        "strike": strike,
        "expiry_date": "mock expiry",
        "days_to_expiry": random.randint(1, 14),
        "mark_price_underlying": round(random.uniform(0.01, 0.08), 4),
        "mark_price_usd": round(spot * random.uniform(0.01, 0.08), 2),
        "mark_iv": round(random.uniform(40, 90), 1),
        "underlying_price": round(spot, 2),
        "venue": "Deribit",
    }
