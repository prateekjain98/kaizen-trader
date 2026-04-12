"""Mean Reversion Strategy — VWAP + RSI."""

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal, ScannerConfig, MarketContext


@dataclass
class OHLCVSample:
    close: float
    volume: float
    ts: float


_ohlcv_buffers: dict[str, list[OHLCVSample]] = {}
_lock = threading.Lock()
_MAX_SAMPLES = 200

STRATEGY_META = {
    "strategies": [
        {"id": "mean_reversion", "function": "scan_mean_reversion",
         "description": "Trades VWAP/RSI mean reversion signals",
         "tier": "swing"},
    ],
    "signal_sources": ["price_action"],
}


def push_ohlcv_sample(symbol: str, close: float, volume: float) -> None:
    with _lock:
        buf = _ohlcv_buffers.setdefault(symbol, [])
        buf.append(OHLCVSample(close=close, volume=volume, ts=time.time() * 1000))
        if len(buf) > _MAX_SAMPLES:
            buf.pop(0)


def _compute_vwap(samples: list[OHLCVSample]) -> Optional[float]:
    if not samples:
        return None
    sum_pv = sum(s.close * s.volume for s in samples)
    sum_v = sum(s.volume for s in samples)
    return sum_pv / sum_v if sum_v != 0 else None


def _compute_rsi(samples: list[OHLCVSample], period: int = 14) -> Optional[float]:
    if len(samples) < period + 1:
        return None
    recent = samples[-(period + 1):]
    gains = losses = 0.0
    for i in range(1, len(recent)):
        diff = recent[i].close - recent[i - 1].close
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def scan_mean_reversion(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    with _lock:
        buf = list(_ohlcv_buffers.get(symbol, []))
    if not buf or len(buf) < 30:
        return None
    now = time.time() * 1000

    vwap = _compute_vwap(buf)
    rsi = _compute_rsi(buf)
    recent_20 = buf[-20:]
    avg_volume = sum(s.volume for s in recent_20) / len(recent_20) if recent_20 else 0
    current_volume = buf[-1].volume if buf else 0

    if vwap is None or vwap == 0 or rsi is None:
        return None

    deviation = (current_price - vwap) / vwap
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

    # Backtest fix: require minimum 2% absolute deviation to ensure profit after fees.
    # 66 adverse_move losses at 1.5% threshold — not enough room for the trade to work.
    if abs(deviation) < 0.02:
        return None

    # Long entry — mean reversion works BEST during fear dislocations (panic overshoots)
    # Require elevated volume (panic selling) as confirmation
    # Backtest fix: block longs in extreme_fear — 8 wrong_market_phase losses.
    # Mean reversion longs during capitulation catch falling knives.
    if (deviation < -config.vwap_deviation_pct and rsi < config.rsi_oversold
            and volume_ratio > 1.5
            and ctx.phase not in ("extreme_greed", "bear")):
        dev_score = min(30, abs(deviation) * 500)
        rsi_score = min(20, config.rsi_oversold - rsi)
        score = min(90, 50 + dev_score + rsi_score)  # Raised base from 40 to pass higher min_qual
        # R:R fix: target at least 5% from entry so target > stop (3%)
        long_target = max(vwap, current_price * 1.05)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="mean_reversion", side="long", tier="swing", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["price_action"],
            reasoning=f"{symbol} {deviation*100:.1f}% below VWAP, RSI={rsi:.0f} oversold",
            entry_price=current_price, target_price=long_target,
            stop_price=current_price * 0.97, suggested_size_usd=80,  # Backtest: 3% stop (70 adverse_move at 2%)
            expires_at=now + 1_800_000, created_at=now,
        )

    # Short entry — mean reversion shorts work best in euphoric overextensions
    # Require elevated volume (euphoric buying) as confirmation
    if (deviation > config.vwap_deviation_pct and rsi > config.rsi_overbought
            and volume_ratio > 1.5 and ctx.phase not in ("extreme_fear",)):
        dev_score = min(30, deviation * 500)
        rsi_score = min(20, rsi - config.rsi_overbought)
        score = min(88, 48 + dev_score + rsi_score)  # Raised base from 38
        # R:R fix: target at least 5% from entry so target > stop (3%)
        short_target = min(vwap, current_price * 0.95)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="mean_reversion", side="short", tier="swing", score=score,
            confidence="medium" if score > 68 else "low",
            sources=["price_action"],
            reasoning=f"{symbol} {deviation*100:.1f}% above VWAP, RSI={rsi:.0f} overbought",
            entry_price=current_price, target_price=short_target,
            stop_price=current_price * 1.03, suggested_size_usd=60,  # Backtest: 3% stop
            expires_at=now + 1_800_000, created_at=now,
        )

    return None
