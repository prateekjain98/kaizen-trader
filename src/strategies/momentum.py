"""Momentum Breakout Strategy (swing + scalp tiers)."""

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.indicators.core import get_atr
from src.types import TradeSignal, ScannerConfig, MarketContext


@dataclass
class PriceSample:
    price: float
    volume_24h: float
    ts: float


_swing_buffers: dict[str, list[PriceSample]] = {}
_scalp_buffers: dict[str, list[PriceSample]] = {}
_cooldowns: dict[str, float] = {}
_lock = threading.Lock()

_MAX_SYMBOLS = 500  # prevent unbounded growth of per-symbol buffers

STRATEGY_META = {
    "strategies": [
        {"id": "momentum_swing", "function": "scan_momentum",
         "description": "Detects momentum breakouts on 1h timeframe",
         "tier": "swing"},
        {"id": "momentum_scalp", "function": "scan_momentum",
         "description": "Detects momentum breakouts on 5m timeframe",
         "tier": "scalp"},
    ],
    "signal_sources": ["price_action"],
}


def push_price_sample(symbol: str, price: float, volume_24h: float) -> None:
    now = time.time() * 1000
    sample = PriceSample(price=price, volume_24h=volume_24h, ts=now)

    with _lock:
        # Evict oldest symbols if we exceed the max to prevent unbounded growth
        if symbol not in _swing_buffers and len(_swing_buffers) >= _MAX_SYMBOLS:
            oldest_key = min(_swing_buffers, key=lambda k: _swing_buffers[k][-1].ts if _swing_buffers[k] else 0)
            del _swing_buffers[oldest_key]
        if symbol not in _scalp_buffers and len(_scalp_buffers) >= _MAX_SYMBOLS:
            oldest_key = min(_scalp_buffers, key=lambda k: _scalp_buffers[k][-1].ts if _scalp_buffers[k] else 0)
            del _scalp_buffers[oldest_key]

        sw = _swing_buffers.setdefault(symbol, [])
        sw.append(sample)
        cutoff = now - 3_600_000
        while sw and sw[0].ts < cutoff:
            sw.pop(0)

        sc = _scalp_buffers.setdefault(symbol, [])
        sc.append(sample)
        cutoff = now - 300_000
        while sc and sc[0].ts < cutoff:
            sc.pop(0)


def _compute_momentum(samples: list[PriceSample]) -> Optional[dict]:
    if len(samples) < 5:
        return None
    first = samples[0]
    last = samples[-1]
    if first.price == 0:
        return None
    pct = (last.price - first.price) / first.price
    avg_volume = sum(s.volume_24h for s in samples) / len(samples)
    if avg_volume == 0:
        return None
    return {"pct": pct, "avg_volume": avg_volume, "current_volume": last.volume_24h}


def _has_cooldown(symbol: str) -> bool:
    with _lock:
        expiry = _cooldowns.get(symbol)
        return bool(expiry and expiry > time.time() * 1000)


def _set_cooldown(symbol: str, ms: float) -> None:
    with _lock:
        # Purge expired cooldowns to prevent unbounded growth
        if len(_cooldowns) > _MAX_SYMBOLS:
            now = time.time() * 1000
            expired = [k for k, v in _cooldowns.items() if v <= now]
            for k in expired:
                del _cooldowns[k]
        _cooldowns[symbol] = time.time() * 1000 + ms


def _vol_adjusted_threshold(symbol: str, base_threshold: float, current_price: float) -> float:
    """Adjust momentum threshold by realized volatility.

    If ATR/price > 3% (high vol coin), raise the threshold proportionally.
    If ATR/price < 1% (low vol coin), lower it.
    Returns the adjusted threshold.
    """
    atr = get_atr(symbol)
    if atr is None or atr <= 0 or current_price <= 0:
        return base_threshold
    # ATR as fraction of price (typical: 1-5% for crypto)
    atr_pct = atr / current_price
    # Normalize: base assumption is 2% ATR/price
    vol_factor = atr_pct / 0.02 if atr_pct > 0 else 1.0
    # Scale threshold: higher vol -> higher threshold (harder to trigger)
    # Clamp factor to [0.5, 2.5] to avoid extremes
    vol_factor = max(0.5, min(2.5, vol_factor))
    return base_threshold * vol_factor


def scan_momentum(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    if _has_cooldown(symbol):
        return None
    now = time.time() * 1000

    with _lock:
        swing_samples = list(_swing_buffers.get(symbol, []))
        scalp_samples = list(_scalp_buffers.get(symbol, []))

    # Swing tier — use volatility-adjusted threshold
    swing = _compute_momentum(swing_samples)
    adj_threshold_swing = _vol_adjusted_threshold(symbol, config.momentum_pct_swing, current_price)
    adj_threshold_swing = max(0.02, adj_threshold_swing)
    if swing and swing["pct"] >= adj_threshold_swing:
        # Backtest fix: reject pump tops — moves >8% in 1h are usually exhausted
        if swing["pct"] > 0.08:
            return None
        # Backtest fix: skip longs during bear phases (38 adverse_move losses)
        # extreme_fear removed — momentum with volume confirmation is valid in fear
        if ctx.phase == "bear":
            return None
        volume_ok = swing["current_volume"] >= swing["avg_volume"] * config.volume_multiplier_swing
        if volume_ok:
            market_bonus = 5 if ctx.phase == "bull" else (-15 if ctx.phase == "bear" else 0)
            # Clip momentum pct to avoid all extreme moves scoring identically
            clipped_pct = min(swing["pct"], 0.15)
            score = min(95, 55 + clipped_pct * 200 + market_bonus)
            if score >= config.min_qual_score_swing:
                _set_cooldown(symbol, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                    strategy="momentum_swing", side="long", tier="swing",
                    score=score,
                    confidence="high" if score > 75 else ("medium" if score > 60 else "low"),
                    sources=["price_action"],
                    reasoning=f"{symbol} +{swing['pct']*100:.1f}% in 1h with {swing['current_volume']/swing['avg_volume']:.1f}x volume spike",
                    entry_price=current_price,
                    # Target = 1.5x stop distance — realistic for 1h momentum
                    stop_price=current_price * (1 - config.base_trail_pct_swing),
                    target_price=current_price * (1 + config.base_trail_pct_swing * 1.5),
                    suggested_size_usd=100,
                    expires_at=now + 300_000, created_at=now,
                )

    # Scalp tier — use volatility-adjusted threshold
    scalp = _compute_momentum(scalp_samples)
    adj_threshold_scalp = _vol_adjusted_threshold(symbol, config.momentum_pct_scalp, current_price)
    adj_threshold_scalp = max(0.015, adj_threshold_scalp)
    if scalp and scalp["pct"] >= adj_threshold_scalp:
        volume_ok = scalp["current_volume"] >= scalp["avg_volume"] * config.volume_multiplier_scalp
        recent_2m = [s for s in scalp_samples if s.ts >= now - 120_000]
        freshness_pct = (
            (recent_2m[-1].price - recent_2m[0].price) / recent_2m[0].price
            if len(recent_2m) >= 2 and recent_2m[0].price > 0
            else 0
        )
        # Check the move is accelerating: recent 2m contributes >40% of 5m move
        # AND the recent move is positive (still going up, not reversing)
        fresh_enough = (freshness_pct > 0 and freshness_pct / scalp["pct"] >= 0.4) if scalp["pct"] > 0 else False

        if volume_ok and fresh_enough:
            clipped_pct_s = min(scalp["pct"], 0.10)
            score = min(90, 50 + clipped_pct_s * 150)
            if score >= config.min_qual_score_scalp:
                _set_cooldown(symbol, config.cooldown_ms_scalp)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
                    strategy="momentum_scalp", side="long", tier="scalp",
                    score=score,
                    confidence="medium" if score > 70 else "low",
                    sources=["price_action"],
                    reasoning=f"{symbol} +{scalp['pct']*100:.1f}% in 5m with {scalp['current_volume']/scalp['avg_volume']:.1f}x volume, fresh move",
                    entry_price=current_price,
                    stop_price=current_price * (1 - config.base_trail_pct_scalp),
                    target_price=current_price * (1 + config.base_trail_pct_scalp * 1.5),
                    suggested_size_usd=40,
                    expires_at=now + 60_000, created_at=now,
                )

    return None
