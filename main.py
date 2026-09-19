"""
main.py
-------
Standalone crypto alert bot entry point. Unlike the NSE version, this runs
24/7/365 - no market-hours gate, no weekday check, scans every 5 minutes
around the clock to match your 5-min trading timeframe.

Run locally:  python main.py
Deploy:       gunicorn main:app  (see Procfile)
"""

import logging
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import telegram_notify
from storage import BotStorage
from cooldown_manager import CooldownManager
from binance_feed import feed
from indicators import enrich_dataframe
from scoring import evaluate, should_alert, is_early_signal, detect_big_momentum, is_big_momentum
from options_feed import get_atm_option

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

app = Flask(__name__)

UTC_TZ = ZoneInfo(config.TIMEZONE)  # this bot's timezone IS UTC by design (24/7, no
                                      # single "home" timezone) - defined at module
                                      # level so storage.py's "today" bucketing and
                                      # cooldown timestamps use it consistently too.

storage = BotStorage(config.SQLITE_PATH, config.SYMBOLS, tz=UTC_TZ)
cooldown = CooldownManager(storage)


# ---------------------------------------------------------------------- #
# Core per-symbol processing
# ---------------------------------------------------------------------- #
def process_symbol(symbol: str):
    try:
        if symbol not in feed.valid_symbols and not config.DRY_RUN:
            return  # skipped at validation time, already logged once at boot

        df_raw = feed.get_candles(symbol)
        if df_raw.empty or len(df_raw) < 21:
            storage.log_error(symbol, "NO_DATA", "Insufficient candle data returned")
            storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                    config.MAX_CONSECUTIVE_FAILS_DISABLE)
            return

        df = enrich_dataframe(df_raw)

        live_price = feed.get_live_price(symbol)
        if live_price:
            df.iloc[-1, df.columns.get_loc("close")] = live_price

        result = evaluate(df)

        if result.reject_reason == "VWAP_UNAVAILABLE":
            storage.log_error(symbol, "VWAP_MISSING", "VWAP is N/A - alert skipped (mandatory rule)")
            return

        any_alert_sent = False

        # --- Tier 1: Confirmed (score >= SCORE_ALERT_THRESHOLD) ---
        if should_alert(result):
            if cooldown.can_alert(symbol):
                option_ctx = get_atm_option(symbol, result.direction) if config.OPTIONS_CONTEXT_ENABLED else None
                message_id = telegram_notify.send_trade_alert(
                    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, result, symbol,
                    option_ctx=option_ctx,
                )
                storage.log_alert(
                    symbol, result.direction, result.price, result.vwap, result.rsi,
                    result.adx, result.ema9, result.ema21, result.volume,
                    result.confidence, result.score, message_id, signal_type="CONFIRMED",
                )
                cooldown.start_cooldown(symbol)
                log.info(f"ALERT SENT: {symbol} {result.direction} score={result.score}/10")
                any_alert_sent = True

        # --- Tier 2: Early/building (EARLY_SCORE_MIN <= score < SCORE_ALERT_THRESHOLD) ---
        elif config.EARLY_SIGNAL_ENABLED and is_early_signal(result):
            if cooldown.can_alert_early(symbol):
                option_ctx = get_atm_option(symbol, result.direction) if config.OPTIONS_CONTEXT_ENABLED else None
                early_message_id = telegram_notify.send_early_signal(
                    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, result, symbol,
                    option_ctx=option_ctx,
                )
                storage.log_alert(
                    symbol, result.direction, result.price, result.vwap, result.rsi,
                    result.adx, result.ema9, result.ema21, result.volume,
                    result.confidence, result.score, early_message_id, signal_type="EARLY",
                )
                cooldown.start_early_cooldown(symbol)
                log.info(f"EARLY SIGNAL: {symbol} {result.direction} score={result.score}/10 (building)")
                any_alert_sent = True

        # --- Tier 3: Big momentum - runs INDEPENDENTLY of tiers 1/2 above ---
        if config.MOMENTUM_ENABLED:
            momentum_result = detect_big_momentum(df)
            if is_big_momentum(momentum_result) and cooldown.can_alert_momentum(symbol):
                momentum_message_id = telegram_notify.send_momentum_alert(
                    config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, momentum_result, symbol
                )
                storage.log_alert(
                    symbol, momentum_result.direction, momentum_result.price, momentum_result.vwap,
                    0, momentum_result.adx, 0, 0, momentum_result.volume,
                    "HIGH", 10, momentum_message_id, signal_type="MOMENTUM",
                )
                cooldown.start_momentum_cooldown(symbol)
                log.info(f"BIG MOMENTUM: {symbol} {momentum_result.direction} "
                         f"{momentum_result.pct_move:+.2f}% over {momentum_result.lookback_candles} candles")
                any_alert_sent = True

        storage.reset_fail_counts_if_healthy(symbol)

    except Exception as e:
        log.exception(f"Unhandled error processing {symbol}")
        storage.log_error(symbol, "UNHANDLED_EXCEPTION", str(e))
        storage.record_failure(symbol, config.MAX_CONSECUTIVE_FAILS_BROKEN,
                                config.MAX_CONSECUTIVE_FAILS_DISABLE)


def check_all_symbols():
    """No market-hours gate - crypto trades 24/7/365. Processes 5 symbols
    concurrently per batch instead of sequentially - Binance's public rate
    limits are generous enough that this is safe, and it substantially
    reduces total scan time plus the risk of a long scan starving
    gunicorn's worker heartbeat (see Procfile)."""
    active_rows = storage.get_active_symbols()
    symbols = [r["symbol"] for r in active_rows]
    log.info(f"Scanning {len(symbols)} active symbols (5 concurrent per batch)...")
    batch_size = 5
    with ThreadPoolExecutor(max_workers=batch_size) as executor:
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            futures = [executor.submit(process_symbol, sym) for sym in batch]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    log.exception("Unhandled exception in concurrent batch processing")
            time.sleep(0.5)  # brief pause BETWEEN batches, not between every symbol


# ---------------------------------------------------------------------- #
# Scheduled jobs
# ---------------------------------------------------------------------- #
def job_scan():
    check_all_symbols()


def job_daily_reset():
    """00:00 UTC - fresh cooldown state + VWAP naturally resets since
    get_candles() re-anchors to the new UTC day automatically."""
    cooldown.reset_all()
    telegram_notify.send_health_check(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                                       config.BOT_NAME, len(config.SYMBOLS))
    log.info("Daily UTC reset complete")


def job_daily_summary():
    total = storage.count_alerts_today()
    total_early = storage.count_early_signals_today()
    total_momentum = storage.count_momentum_alerts_today()
    top = storage.top_symbols_today()
    telegram_notify.send_daily_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID,
                                        config.BOT_NAME, total, top,
                                        total_early=total_early, total_momentum=total_momentum)


def job_error_summary():
    total, breakdown = storage.errors_today_summary()
    telegram_notify.send_error_summary(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.BOT_NAME, total, breakdown)


# ---------------------------------------------------------------------- #
# Startup
# ---------------------------------------------------------------------- #
def bootstrap():
    log.info(f"Booting {config.BOT_NAME} (24/7 crypto mode)...")

    if config.DRY_RUN:
        feed.valid_symbols = set(config.SYMBOLS)
        log.info("[BINANCE] DRY_RUN=true, skipping live symbol validation")
    else:
        feed.validate_symbols(config.SYMBOLS)
        feed.start_websocket(list(feed.valid_symbols))

    scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

    # Every N minutes, 24/7 - no day-of-week or hour restriction at all.
    # IntervalTrigger fires every N minutes from start regardless of
    # timezone, so it's unaffected by the CronTrigger timezone issue below.
    scheduler.add_job(job_scan, IntervalTrigger(minutes=config.SCAN_INTERVAL_MINUTES), id="scan")

    # BUG FIX (defensive): CronTrigger does NOT inherit the scheduler's
    # timezone automatically (confirmed empirically). This bot's intended
    # timezone is UTC, which happens to coincidentally match Render's
    # server clock - but explicit timezone= here means these jobs stay
    # correct even if run somewhere with a different system timezone
    # (e.g. local hosting, Termux).
    reset_h, reset_m = map(int, config.DAILY_RESET_TIME.split(":"))
    scheduler.add_job(job_daily_reset, CronTrigger(hour=reset_h, minute=reset_m, timezone=UTC_TZ), id="daily_reset")

    sum_h, sum_m = map(int, config.DAILY_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_daily_summary, CronTrigger(hour=sum_h, minute=sum_m, timezone=UTC_TZ), id="daily_summary")

    err_h, err_m = map(int, config.ERROR_SUMMARY_TIME.split(":"))
    scheduler.add_job(job_error_summary, CronTrigger(hour=err_h, minute=err_m, timezone=UTC_TZ), id="error_summary")

    scheduler.start()
    log.info(f"Scheduler started - scanning every {config.SCAN_INTERVAL_MINUTES} min, 24/7")
    return scheduler


_scheduler = bootstrap()


# ---------------------------------------------------------------------- #
# HTTP endpoints
# ---------------------------------------------------------------------- #
@app.route("/")
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_name": config.BOT_NAME,
        "symbol_count": len(config.SYMBOLS),
        "valid_symbols_on_binance": len(feed.valid_symbols),
        "dry_run": config.DRY_RUN,
        "time_utc": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/status")
def status():
    active_rows = storage.get_active_symbols()
    broken = [r["symbol"] for r in active_rows if r["status"] == "BROKEN"]
    total_errors, error_breakdown = storage.errors_today_summary()
    with storage._conn() as conn:
        recent_errors = [dict(r) for r in conn.execute(
            "SELECT * FROM error_log ORDER BY id DESC LIMIT 10"
        ).fetchall()]

    skipped = sorted(set(config.SYMBOLS) - feed.valid_symbols)

    return jsonify({
        "bot_name": config.BOT_NAME,
        "dry_run": config.DRY_RUN,
        "valid_binance_symbols": len(feed.valid_symbols),
        "skipped_invalid_symbols": skipped,
        "telegram_token_configured": bool(config.TELEGRAM_TOKEN),
        "telegram_chat_id_configured": bool(config.TELEGRAM_CHAT_ID),
        "active_symbols": len(active_rows),
        "broken_symbols": broken,
        "alerts_sent_today": storage.count_alerts_today(),
        "early_signals_sent_today": storage.count_early_signals_today(),
        "momentum_alerts_sent_today": storage.count_momentum_alerts_today(),
        "errors_today_total": total_errors,
        "errors_today_by_type": error_breakdown,
        "most_recent_errors": recent_errors,
    })


@app.route("/trigger")
def manual_trigger():
    check_all_symbols()
    return jsonify({"status": "scan triggered"})


@app.route("/telegram_test")
def telegram_test():
    import requests as _requests
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        return jsonify({"sent": False, "reason": "TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env var not set"}), 400
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": f"✅ Test message from {config.BOT_NAME}"}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        data = resp.json()
    except Exception as e:
        return jsonify({"sent": False, "reason": f"Request failed: {e}"}), 500
    return jsonify({"sent": bool(data.get("ok")), "http_status": resp.status_code, "telegram_response": data})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT)
