"""
scoring.py
----------
Turns the last row of an indicator-enriched dataframe into a 0-10 score
for LONG and for SHORT, and decides whether to fire an alert.

VWAP is a HARD GATE (not just a scored point): if VWAP is missing/NaN or
price is >2% away from it, we bail out immediately with score=None and no
alert — regardless of everything else. This matches the "VWAP MANDATORY,
NO alert without it" rule.

Everything else contributes 1 point each toward a 10-point score. An alert
fires only when score >= config.SCORE_ALERT_THRESHOLD (default 8).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Dict

import config

log = logging.getLogger("scoring")


@dataclass
class SignalResult:
    direction: Optional[str] = None       # "LONG" | "SHORT" | None
    score: int = 0
    max_score: int = 10
    confidence: Optional[str] = None      # "HIGH" | "MEDIUM" | "LOW"
    conditions: Dict[str, bool] = field(default_factory=dict)
    price: float = 0.0
    vwap: float = 0.0
    rsi: float = 0.0
    adx: float = 0.0
    ema9: float = 0.0
    ema21: float = 0.0
    volume: float = 0.0
    vol_avg20: float = 0.0
    vwap_distance_pct: float = 0.0
    reject_reason: Optional[str] = None


def _confidence_from_distance(distance_pct: float) -> str:
    if distance_pct <= config.CONFIDENCE_HIGH_PCT:
        return "HIGH"
    if distance_pct <= config.CONFIDENCE_MEDIUM_PCT:
        return "MEDIUM"
    return "LOW"


def evaluate(df) -> SignalResult:
    """
    df: indicator-enriched dataframe from indicators.enrich_dataframe().
    Returns a SignalResult. Caller decides whether to alert based on
    result.direction is not None and result.score >= SCORE_ALERT_THRESHOLD.
    """
    last = df.iloc[-1]
    prev2 = df.iloc[-2] if len(df) >= 2 else None

    price = float(last["close"])
    vwap = float(last["vwap"]) if last["vwap"] == last["vwap"] else None  # NaN check

    result = SignalResult(price=price)

    # ---- HARD GATE: VWAP mandatory --------------------------------------
    if vwap is None or vwap <= 0:
        result.reject_reason = "VWAP_UNAVAILABLE"
        return result

    result.vwap = vwap
    distance_pct = abs(price - vwap) / vwap * 100
    result.vwap_distance_pct = round(distance_pct, 3)

    if distance_pct > config.VWAP_MAX_DISTANCE_PCT:
        result.reject_reason = "OUTSIDE_VWAP_BAND"
        return result

    rsi = float(last["rsi14"])
    adx = float(last["adx14"])
    ema9 = float(last["ema9"])
    ema21 = float(last["ema21"])
    volume = float(last["volume"])
    vol_avg20 = float(last["vol_avg20"])

    result.rsi, result.adx, result.ema9, result.ema21 = rsi, adx, ema9, ema21
    result.volume, result.vol_avg20 = volume, vol_avg20

    # Candle direction helpers (last 2 candles both same color)
    def is_bullish(row):
        return row["close"] > row["open"]

    def is_bearish(row):
        return row["close"] < row["open"]

    last_bullish = is_bullish(last)
    last_bearish = is_bearish(last)
    prev_bullish = is_bullish(prev2) if prev2 is not None else False
    prev_bearish = is_bearish(prev2) if prev2 is not None else False

    ema9_prev = float(df.iloc[-2]["ema9"]) if len(df) >= 2 else ema9

    # ---- LONG conditions (10 points) ------------------------------------
    long_conditions = {
        "price_above_vwap": price > vwap,
        "vwap_within_2pct": distance_pct <= config.VWAP_MAX_DISTANCE_PCT,
        "rsi_in_range": config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX,
        "adx_above_25": adx > config.ADX_MIN,
        "ema9_above_ema21": ema9 > ema21,
        "last_2_candles_bullish": last_bullish and prev_bullish,
        "volume_above_avg20": volume > vol_avg20,
        "tight_to_vwap_bonus": distance_pct <= config.CONFIDENCE_HIGH_PCT,
        "ema9_rising": ema9 > ema9_prev,
        "adx_strong_trend": adx > (config.ADX_MIN + 5),  # extra momentum point
    }
    long_score = sum(long_conditions.values())

    # ---- SHORT conditions (10 points) ------------------------------------
    short_conditions = {
        "price_below_vwap": price < vwap,
        "vwap_within_2pct": distance_pct <= config.VWAP_MAX_DISTANCE_PCT,
        "rsi_in_range": config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX,
        "adx_above_25": adx > config.ADX_MIN,
        "ema9_below_ema21": ema9 < ema21,
        "last_2_candles_bearish": last_bearish and prev_bearish,
        "volume_above_avg20": volume > vol_avg20,
        "tight_to_vwap_bonus": distance_pct <= config.CONFIDENCE_HIGH_PCT,
        "ema9_falling": ema9 < ema9_prev,
        "adx_strong_trend": adx > (config.ADX_MIN + 5),
    }
    short_score = sum(short_conditions.values())

    # Pick whichever direction is plausible and higher-scoring.
    # A candidate direction must at minimum have price on the correct side
    # of VWAP with EMA alignment agreeing (mirrors the "hard rules" set),
    # otherwise we don't call it a direction at all.
    long_viable = long_conditions["price_above_vwap"] and long_conditions["ema9_above_ema21"]
    short_viable = short_conditions["price_below_vwap"] and short_conditions["ema9_below_ema21"]

    if long_viable and (not short_viable or long_score >= short_score):
        result.direction = "LONG"
        result.score = long_score
        result.conditions = long_conditions
    elif short_viable:
        result.direction = "SHORT"
        result.score = short_score
        result.conditions = short_conditions
    else:
        result.reject_reason = "NO_DIRECTIONAL_BIAS"
        return result

    result.confidence = _confidence_from_distance(distance_pct)
    return result


def should_alert(result: SignalResult) -> bool:
    return (
        result.direction is not None
        and result.reject_reason is None
        and result.score >= config.SCORE_ALERT_THRESHOLD
    )


def is_early_signal(result: SignalResult) -> bool:
    """
    True when a setup is building - a clear directional bias with a decent
    score, but not yet at the full confirmation threshold. Lets you see a
    trade forming instead of only finding out once it's already 8/10+.
    VWAP is still mandatory here: reject_reason is None already guarantees
    VWAP was present and within band, same hard gate as confirmed alerts.
    """
    return (
        result.direction is not None
        and result.reject_reason is None
        and config.EARLY_SCORE_MIN <= result.score < config.SCORE_ALERT_THRESHOLD
    )


@dataclass
class MomentumResult:
    """
    Separate from SignalResult on purpose. A big momentum breakout is, by
    definition, often a price that has ALREADY moved meaningfully away
    from VWAP - the confirmed/early tiers above deliberately reject that
    (VWAP_MAX_DISTANCE_PCT is a hard gate meant to catch fair-value setups,
    not breakouts). Without a separate path, a genuinely big move gets
    silently discarded by that same gate - the opposite of what you want.
    This path only requires VWAP to exist at all (still mandatory), not
    to be close to it.
    """
    direction: Optional[str] = None
    price: float = 0.0
    vwap: float = 0.0
    pct_move: float = 0.0
    adx: float = 0.0
    volume: float = 0.0
    vol_avg20: float = 0.0
    volume_multiple: float = 0.0
    lookback_candles: int = 0
    reject_reason: Optional[str] = None


def detect_big_momentum(df) -> MomentumResult:
    """
    Independent of evaluate()/should_alert()/is_early_signal() above -
    reads the dataframe directly rather than reusing SignalResult, so it
    is never subject to the VWAP-distance hard gate. VWAP presence is
    still mandatory (the one rule that never bends); VWAP *closeness* is
    not required here, since a real breakout has usually already moved
    away from it.

    Fires when ALL of:
      - price moved >= MOMENTUM_MIN_PCT_MOVE over the last
        MOMENTUM_LOOKBACK_CANDLES candles (a fast, large move - not
        a slow drift)
      - ADX >= MOMENTUM_MIN_ADX (a strong trend, not noise)
      - volume >= MOMENTUM_MIN_VOLUME_MULT x its 20-period average
        (the move is backed by real participation, not a thin print)
    """
    result = MomentumResult()
    if df is None or len(df) < config.MOMENTUM_LOOKBACK_CANDLES + 1:
        result.reject_reason = "INSUFFICIENT_DATA"
        return result

    last = df.iloc[-1]
    vwap = last["vwap"] if last["vwap"] == last["vwap"] else None  # NaN check
    if vwap is None or vwap <= 0:
        result.reject_reason = "VWAP_UNAVAILABLE"  # mandatory rule still applies
        return result

    price = float(last["close"])
    lookback_idx = -1 - config.MOMENTUM_LOOKBACK_CANDLES
    price_then = float(df.iloc[lookback_idx]["close"])
    if price_then <= 0:
        result.reject_reason = "INVALID_PRICE_HISTORY"
        return result

    pct_move = (price - price_then) / price_then * 100
    adx = float(last["adx14"])
    volume = float(last["volume"])
    vol_avg20 = float(last["vol_avg20"]) if last["vol_avg20"] == last["vol_avg20"] else 0.0
    volume_multiple = (volume / vol_avg20) if vol_avg20 > 0 else 0.0

    result.price, result.vwap = price, float(vwap)
    result.pct_move, result.adx = pct_move, adx
    result.volume, result.vol_avg20, result.volume_multiple = volume, vol_avg20, volume_multiple
    result.lookback_candles = config.MOMENTUM_LOOKBACK_CANDLES

    strong_trend = adx >= config.MOMENTUM_MIN_ADX
    strong_volume = volume_multiple >= config.MOMENTUM_MIN_VOLUME_MULT

    if pct_move >= config.MOMENTUM_MIN_PCT_MOVE and strong_trend and strong_volume:
        result.direction = "LONG"
    elif pct_move <= -config.MOMENTUM_MIN_PCT_MOVE and strong_trend and strong_volume:
        result.direction = "SHORT"
    else:
        result.reject_reason = "THRESHOLD_NOT_MET"

    return result


def is_big_momentum(result: MomentumResult) -> bool:
    return result.direction is not None and result.reject_reason is None
