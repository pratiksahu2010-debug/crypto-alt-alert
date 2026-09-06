"""
telegram_notify.py
-------------------
Same Bot API approach as the NSE version, adapted for crypto:
  - USD ($) instead of INR (₹)
  - Dynamic decimal precision, since BTC ($60,000+) and SHIB ($0.00002) both
    appear in the same symbol list and need very different formatting.
"""

import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("telegram")

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_price(p: float) -> str:
    """Dynamic precision: more decimals for sub-$1 assets, fewer for BTC-scale."""
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def _send(token: str, chat_id: str, text: str, retries: int = 2) -> str:
    if not token or not chat_id:
        log.warning("[TELEGRAM] Missing token/chat_id - message not sent (DRY RUN?)")
        return ""
    url = TELEGRAM_API_BASE.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                return str(data["result"]["message_id"])
            last_err = data
        except Exception as e:
            last_err = e
        log.warning(f"[TELEGRAM] send attempt {attempt+1} failed: {last_err}")
    log.error(f"[TELEGRAM] All attempts failed: {last_err}")
    return ""


def send_trade_alert(token, chat_id, bot_name, signal_result, symbol) -> str:
    r = signal_result
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    display_symbol = symbol.replace("USDT", "/USDT")

    text = (
        f"📊 *{bot_name}*\n"
        f"🚨 TRADE ALERT: {display_symbol}\n"
        f"📈 Signal: {r.direction}\n"
        f"💰 Price: ${_fmt_price(r.price)}\n"
        f"📊 RSI: {r.rsi:.1f} ✅\n"
        f"📈 ADX: {r.adx:.1f} ✅\n"
        f"📉 VWAP: ${_fmt_price(r.vwap)} (MANDATORY ✓)\n"
        f"🔴 9 EMA: ${_fmt_price(r.ema9)}\n"
        f"🟡 21 EMA: ${_fmt_price(r.ema21)}\n"
        f"📊 Volume: {r.volume:,.0f} (Above Avg: {'YES' if r.volume > r.vol_avg20 else 'NO'})\n"
        f"📏 Distance from VWAP: {r.vwap_distance_pct:.2f}%\n"
        f"⭐ Confidence: {r.confidence}\n"
        f"🎯 Score: {r.score}/10\n"
        f"⏰ Time: {now_utc}"
    )
    return _send(token, chat_id, text)


def send_early_signal(token, chat_id, bot_name, signal_result, symbol) -> str:
    """
    Sent when a setup is building (score 6-7/10) but not yet at the full
    8/10 confirmation threshold - a heads-up so a fast-moving crypto trade
    isn't missed while waiting for full confirmation.
    """
    r = signal_result
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    display_symbol = symbol.replace("USDT", "/USDT")

    text = (
        f"👀 *{bot_name}*\n"
        f"⚡ EARLY MOMENTUM SIGNAL: {display_symbol}\n"
        f"📈 Building: {r.direction}\n"
        f"💰 Price: ${_fmt_price(r.price)}\n"
        f"📉 VWAP: ${_fmt_price(r.vwap)} (MANDATORY ✓)\n"
        f"📊 RSI: {r.rsi:.1f} | ADX: {r.adx:.1f}\n"
        f"🔴 9 EMA: ${_fmt_price(r.ema9)} | 🟡 21 EMA: ${_fmt_price(r.ema21)}\n"
        f"📏 Distance from VWAP: {r.vwap_distance_pct:.2f}%\n"
        f"🎯 Score: {r.score}/10 (confirmation needs 8+)\n"
        f"🔔 Not yet a confirmed trade - monitor for full setup\n"
        f"⏰ Time: {now_utc}"
    )
    return _send(token, chat_id, text)


def send_daily_summary(token, chat_id, bot_name, total_alerts, top_symbols, total_early=None):
    lines = [f"📋 *{bot_name} - DAILY SUMMARY (UTC day)*", f"🚨 Confirmed alerts: {total_alerts}"]
    if total_early is not None:
        lines.append(f"👀 Early/watch signals: {total_early}")
    if top_symbols:
        lines.append("🔥 Most active symbols:")
        for sym, count in top_symbols:
            lines.append(f"   • {sym}: {count} alert(s)")
    _send(token, chat_id, "\n".join(lines))


def send_error_summary(token, chat_id, bot_name, total_errors, breakdown):
    lines = [f"⚠️ *{bot_name} - ERROR SUMMARY*", f"Total errors today: {total_errors}"]
    for err_type, count in breakdown:
        lines.append(f"   • {err_type}: {count}")
    if not breakdown:
        lines.append("No errors today ✅")
    _send(token, chat_id, "\n".join(lines))


def send_health_check(token, chat_id, bot_name, symbol_count):
    text = (
        f"🤖 *{bot_name} INITIALIZED*\n"
        f"📊 Monitoring: {symbol_count} symbols (24/7)\n"
        f"⏰ Schedule: Every 5 minutes, all day, every day\n"
        f"📋 VWAP: MANDATORY (resets 00:00 UTC)\n"
        f"🔒 Strict Mode: score ≥ 8/10 required\n"
        f"✅ Bot is LIVE and scanning!"
    )
    _send(token, chat_id, text)
