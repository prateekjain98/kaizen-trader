"""Mean Reversion Strategy — VWAP + RSI."""

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
_MAX_SAMPLES = 200


def push_ohlcv_sample(symbol: str, close: float, volume: float) -> None:
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
    buf = _ohlcv_buffers.get(symbol)
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

    # Long entry
    if (deviation < -config.vwap_deviation_pct and rsi < config.rsi_oversold
            and volume_ratio < 1.5 and ctx.phase not in ("bear", "extreme_fear")):
        dev_score = min(30, abs(deviation) * 500)
        rsi_score = min(20, config.rsi_oversold - rsi)
        score = min(90, 40 + dev_score + rsi_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="mean_reversion", side="long", tier="swing", score=score,
            confidence="medium" if score > 70 else "low",
            sources=["price_action"],
            reasoning=f"{symbol} {deviation*100:.1f}% below VWAP, RSI={rsi:.0f} oversold",
            entry_price=current_price, target_price=vwap,
            stop_price=current_price * 0.98, suggested_size_usd=80,
            expires_at=now + 1_800_000, created_at=now,
        )

    # Short entry
    if (deviation > config.vwap_deviation_pct and rsi > config.rsi_overbought
            and volume_ratio < 1.5 and ctx.phase not in ("bull", "extreme_greed")):
        dev_score = min(30, deviation * 500)
        rsi_score = min(20, rsi - config.rsi_overbought)
        score = min(88, 38 + dev_score + rsi_score)
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="mean_reversion", side="short", tier="swing", score=score,
            confidence="medium" if score > 68 else "low",
            sources=["price_action"],
            reasoning=f"{symbol} {deviation*100:.1f}% above VWAP, RSI={rsi:.0f} overbought",
            entry_price=current_price, target_price=vwap,
            stop_price=current_price * 1.02, suggested_size_usd=60,
            expires_at=now + 1_800_000, created_at=now,
        )

    return None
