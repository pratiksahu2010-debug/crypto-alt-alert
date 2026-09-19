"""
options_feed.py
----------------
Single entry point main.py calls for options context, routing each symbol
to whichever venue actually has a real market for it:

  - BTC, ETH  -> Deribit (deribit_options_feed.py)
  - XAUT      -> Bybit (bybit_options_feed.py - Deribit has no XAUT options)
  - anything else -> None, same as before (no fabricated data)

Adding a new covered symbol is a one-line change in the relevant feed
module's SYMBOL_TO_*_* mapping - nothing here needs to change unless the
symbol needs a brand-new venue.
"""

import deribit_options_feed
import bybit_options_feed


def get_atm_option(symbol: str, direction: str):
    option = deribit_options_feed.get_atm_option(symbol, direction)
    if option:
        return option
    return bybit_options_feed.get_atm_option(symbol, direction)
