"""Market regime detection — composite classifier for trend, volatility, and phase.

Uses multiple signals to classify the current market regime:
- Trend: trending_up, trending_down, ranging
- Volatility: low_vol, normal_vol, high_vol
- Phase: accumulation, markup, distribution, markdown (Wyckoff)

Strategies should adapt behavior based on regime:
- Mean reversion works in ranging/low_vol markets
- Momentum works in trending/high_vol markets
- Avoid mean reversion in strong trends (ADX > 30)
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.indicators.core import get_snapshot, IndicatorSnapshot
from src.signals.fear_greed import fetch_fear_greed
from src.utils.safe_math import safe_ratio


@dataclass
class RegimeSnapshot:
    """Current market regime classification."""
    ts: float
    # Trend classification
    trend: str = "unknown"          # trending_up, trending_down, ranging
    trend_strength: float = 0.0     # 0-100 (based on ADX)
    # Volatility classification
    volatility: str = "normal_vol"  # low_vol, normal_vol, high_vol
    bb_squeeze: bool = False        # Bollinger Band squeeze (low vol -> breakout imminent)
    # Wyckoff-inspired phase
    phase: str = "unknown"          # accumulation, markup, distribution, markdown
    # Composite regime score (-100 to +100, negative = bearish, positive = bullish)
    regime_score: float = 0.0
    # Component signals
    ema_alignment: str = "neutral"  # bullish (20>50>200), bearish (200>50>20), neutral
    macd_signal: str = "neutral"    # bullish, bearish, neutral
    rsi_zone: str = "neutral"       # oversold, overbought, neutral
    fear_greed: Optional[float] = None
    funding_rate: Optional[float] = None


_lock = threading.Lock()
_regime_cache: dict[str, tuple[RegimeSnapshot, float]] = {}
_REGIME_TTL_MS = 10_000  # recompute every 10 seconds


def classify_regime(symbol: str = "BTC") -> RegimeSnapshot:
    """Classify the current market regime for a symbol."""
    now = time.time() * 1000

    with _lock:
        cached = _regime_cache.get(symbol)
        if cached and (now - cached[1]) < _REGIME_TTL_MS:
            return cached[0]

    snap = get_snapshot(symbol)
    regime = RegimeSnapshot(ts=now)

    if snap is None:
        with _lock:
            _regime_cache[symbol] = (regime, now)
        return regime

    # --- Trend classification via ADX ---
    if snap.adx is not None:
        regime.trend_strength = snap.adx
        if snap.adx >= 25:
            # Strong trend — determine direction from +DI vs -DI
            if snap.plus_di is not None and snap.minus_di is not None:
                if snap.plus_di > snap.minus_di:
                    regime.trend = "trending_up"
                else:
                    regime.trend = "trending_down"
            else:
                regime.trend = "trending_up"  # default if DI unavailable
        else:
            regime.trend = "ranging"

    # --- EMA alignment ---
    regime.ema_alignment = _classify_ema_alignment(snap)

    # --- MACD signal ---
    if snap.macd_histogram is not None:
        if snap.macd_histogram > 0 and snap.macd_line is not None and snap.macd_line > 0:
            regime.macd_signal = "bullish"
        elif snap.macd_histogram < 0 and snap.macd_line is not None and snap.macd_line < 0:
            regime.macd_signal = "bearish"
        else:
            regime.macd_signal = "neutral"

    # --- RSI zone ---
    if snap.rsi_14 is not None:
        if snap.rsi_14 <= 30:
            regime.rsi_zone = "oversold"
        elif snap.rsi_14 >= 70:
            regime.rsi_zone = "overbought"
        else:
            regime.rsi_zone = "neutral"

    # --- Volatility classification via Bollinger Band width ---
    if snap.bb_width is not None:
        if snap.bb_width < 0.03:
            regime.volatility = "low_vol"
            regime.bb_squeeze = True
        elif snap.bb_width > 0.08:
            regime.volatility = "high_vol"
        else:
            regime.volatility = "normal_vol"

    # --- External signals ---
    fg = fetch_fear_greed()
    if fg is not None:
        regime.fear_greed = fg.value if hasattr(fg, "value") else None

    # --- Wyckoff phase ---
    regime.phase = _classify_wyckoff_phase(regime, snap)

    # --- Composite regime score ---
    regime.regime_score = _compute_regime_score(regime, snap)

    with _lock:
        _regime_cache[symbol] = (regime, now)

    return regime


def _classify_ema_alignment(snap: IndicatorSnapshot) -> str:
    """Classify EMA alignment: bullish (20>50>200), bearish (200>50>20), or neutral."""
    if snap.ema_20 is None or snap.ema_50 is None:
        return "neutral"

    if snap.ema_200 is not None:
        if snap.ema_20 > snap.ema_50 > snap.ema_200:
            return "bullish"
        elif snap.ema_20 < snap.ema_50 < snap.ema_200:
            return "bearish"
    else:
        if snap.ema_20 > snap.ema_50:
            return "bullish"
        elif snap.ema_20 < snap.ema_50:
            return "bearish"

    return "neutral"


def _classify_wyckoff_phase(regime: RegimeSnapshot, snap: IndicatorSnapshot) -> str:
    """Approximate Wyckoff market phase from indicators."""
    # Accumulation: ranging + low vol + oversold RSI
    if regime.trend == "ranging" and regime.volatility == "low_vol" and regime.rsi_zone == "oversold":
        return "accumulation"

    # Markup: trending up + bullish EMA + bullish MACD
    if regime.trend == "trending_up" and regime.ema_alignment == "bullish":
        return "markup"

    # Distribution: ranging + high vol + overbought RSI
    if regime.trend == "ranging" and regime.rsi_zone == "overbought":
        return "distribution"

    # Markdown: trending down + bearish EMA + bearish MACD
    if regime.trend == "trending_down" and regime.ema_alignment == "bearish":
        return "markdown"

    return "unknown"


def _compute_regime_score(regime: RegimeSnapshot, snap: IndicatorSnapshot) -> float:
    """Compute a composite regime score from -100 (extremely bearish) to +100 (extremely bullish)."""
    score = 0.0

    # Trend component (weight: 30)
    if regime.trend == "trending_up":
        score += 30
    elif regime.trend == "trending_down":
        score -= 30

    # EMA alignment (weight: 20)
    if regime.ema_alignment == "bullish":
        score += 20
    elif regime.ema_alignment == "bearish":
        score -= 20

    # MACD (weight: 15)
    if regime.macd_signal == "bullish":
        score += 15
    elif regime.macd_signal == "bearish":
        score -= 15

    # RSI (weight: 15)
    if snap.rsi_14 is not None:
        # Normalize RSI to -15..+15 (50 = neutral)
        rsi_component = ((snap.rsi_14 - 50) / 50) * 15
        score += rsi_component

    # Fear & Greed (weight: 10)
    if regime.fear_greed is not None:
        # Normalize 0-100 to -10..+10
        fg_component = ((regime.fear_greed - 50) / 50) * 10
        score += fg_component

    # Volume confirmation via OBV trend (weight: 10)
    # Positive OBV with uptrend = confirmation, divergence = warning
    # Simple: if OBV is positive and trend is up, add; if diverging, subtract
    if snap.obv is not None and snap.obv > 0 and regime.trend == "trending_up":
        score += 10
    elif snap.obv is not None and snap.obv < 0 and regime.trend == "trending_down":
        score -= 10

    return max(-100.0, min(100.0, safe_ratio(score)))


def is_trending(symbol: str = "BTC") -> bool:
    """Quick check: is the market currently trending (ADX > 25)?"""
    regime = classify_regime(symbol)
    return regime.trend in ("trending_up", "trending_down")


def is_high_volatility(symbol: str = "BTC") -> bool:
    """Quick check: is the market in a high-volatility regime?"""
    regime = classify_regime(symbol)
    return regime.volatility == "high_vol"


def is_squeeze(symbol: str = "BTC") -> bool:
    """Quick check: is there a Bollinger Band squeeze (low vol, breakout imminent)?"""
    regime = classify_regime(symbol)
    return regime.bb_squeeze


def get_regime_score(symbol: str = "BTC") -> float:
    """Get the composite regime score (-100 to +100)."""
    return classify_regime(symbol).regime_score
