# Crypto Alt-Coin Alert Bot

24/7 Telegram alert bot for 39 crypto pairs, scanning every 5 minutes to
match a 5-min trading timeframe. Same VWAP-mandatory, 10-point scoring
system as the NSE bots, adapted for crypto's always-on market.

Not investment advice - this is a technical screening tool. Crypto is
significantly more volatile than NSE large/mid-caps; an 8/10 score here
does not mean the same thing statistically as an 8/10 on a Nifty stock.
Backtest before trusting this with real capital.

## Data source: Binance public API (free, no signup, no API key)

Market data (candles, live prices) is completely public on Binance - no
account, no key, no cost. This is different from the NSE version, which
needed a broker login (Angel One). Nothing to sign up for here.

## ⚠️ Check these tickers before relying on the full 39-symbol list

Several tickers in your original list are uncertain as real, currently-
tradeable Binance pairs:

- **MATIC** — Polygon rebranded its token to **POL** in 2024. `MATICUSDT`
  may still work on Binance as a legacy pair, or may not — verify.
- **BDAG** (BlockDAG), **VNI**, **AKE**, **ESPORTS** — these don't match
  any major, currently-listed Binance pair as far as I can confirm; they
  may be presale tokens, listed only on other exchanges, or a ticker I
  don't recognize. Double-check the exact project and correct ticker.
- **WLFI** (World Liberty Financial), **LEO** (UNUS SED LEO), **LIT**
  (Litentry) — may or may not be listed on Binance specifically (some
  trade on other exchanges only).

**The code does not trust this list blindly.** At boot, `binance_feed.py`
calls Binance's `exchangeInfo` endpoint and checks every symbol against
what's actually live and tradeable right now. Anything that doesn't match
is logged clearly and skipped — it will never crash the app, but it also
won't silently alert on a symbol that doesn't exist. **Check the boot log
after your first deploy** for a line like:
```
[BINANCE] 7 symbol(s) are NOT currently valid/tradeable Binance pairs and will be SKIPPED: [...]
```
Fix any real mistakes in `bots_config/symbols.json` (or just leave them —
they're harmlessly skipped either way).

## Project structure

```
config.py            # bot identity, thresholds, built-in symbol list, env vars
binance_feed.py       # Binance REST candles + websocket live price (NEW - replaces angel_one_feed.py)
indicators.py          # VWAP/EMA/RSI/ADX - identical to the NSE version
scoring.py               # 10-point scoring - identical logic, wider VWAP bands
storage.py                # SQLite Settings/AlertLog/ErrorLog - identical
cooldown_manager.py        # 2h cooldown policy - identical
telegram_notify.py          # Alert formatting - adapted for USD + dynamic decimals
main.py                       # 24/7 Flask + scheduler (no market-hours gate)
bots_config/symbols.json       # optional override for the symbol list
```

## What's different from the NSE version

| | NSE bots | This crypto bot |
|---|---|---|
| Data source | Angel One SmartAPI (needs broker login) | Binance public API (no login) |
| Operating hours | 9:15–15:30 IST, Mon–Fri only | 24/7/365 |
| Scan interval | 15 minutes | 5 minutes (matches your timeframe) |
| VWAP reset | Daily at market close (IST) | Daily at 00:00 UTC |
| VWAP band / confidence thresholds | 2% / 0.5% / 1.5% | 3% / 1.0% / 2.0% (wider — crypto is more volatile) |
| Timezone | Asia/Kolkata | UTC throughout |

## Setup

**1. Telegram bot:** same as before — @BotFather → `/newbot` → save token;
message the bot once → `https://api.telegram.org/bot<TOKEN>/getUpdates`
(or message @userinfobot) → get your chat id.

**2. No exchange account or API key needed** — Binance's public market
data endpoints require nothing.

**3. Environment variables on Render:**
```
DRY_RUN=false
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
```
Start with `DRY_RUN=true` to test the full pipeline against synthetic data
first — same as the NSE bots.

**4. Deploy:** push this folder to its own repo → Render → New Web Service
(or Blueprint via `render.yaml`) → Build command `pip install -r
requirements.txt` → Start command `gunicorn main:app --bind 0.0.0.0:$PORT
--workers 1 --threads 4 --timeout 120`. Set `PYTHON_VERSION=3.12.7` if
deploying manually (not via Blueprint).

**5. Verify:**
```
/health           - confirms service up, shows symbol count
/telegram_test    - sends a test message, shows Telegram's raw API response
/status           - full diagnostic: valid Binance symbols, recent errors
/trigger          - runs a scan immediately (no force flag needed - always "market hours")
```

## Editing the symbol list

Edit `bots_config/symbols.json` — a flat JSON array of Binance pair
strings like `"BTCUSDT"`, `"DOGEUSDT"`. No code changes needed. If the
file is missing entirely, the built-in 39-symbol list in `config.py` is
used automatically (see the ticker caveats above).

## Rate limits

Binance's public API allows 1200 request-weight/minute; each `klines`
call costs ~1-2 weight. At 39 symbols scanned every 5 minutes, this uses
a small fraction of that budget — no special throttling needed beyond the
light 0.5s pause between calls already in `main.py`. If you significantly
expand the symbol list (100+), consider switching entirely to the
websocket kline stream for indicator data instead of polling REST, to cut
request volume further.

## Known limitations

- **No persistent disk on Render's free/Starter tier** — `data/alerts.db`
  resets on every redeploy/restart unless you add a paid disk.
- **Websocket auto-reconnects** on disconnect, but if Render restarts the
  whole process (e.g. deploy, crash), the connection re-establishes at
  boot — expect brief live-price gaps around restarts; REST candle data
  is unaffected since it's fetched fresh every scan regardless.
- **DAI is a stablecoin** (~$1 always) — technical signals like RSI/ADX on
  a pegged asset are not meaningful in the way they are for a volatile
  asset. Consider removing it from the list.
- No backtesting included — validate the 10-point rule set against
  historical crypto data before trusting it with real capital; crypto's
  volatility profile is different enough from equities that the same
  score threshold may not perform the same way.
