"""Cross-Exchange Divergence Strategy — detect and trade price discrepancies between exchanges.

When the same asset trades at materially different prices on Coinbase vs Binance,
it signals a temporary dislocation that tends to mean-revert. This is the simplest
form of cross-exchange arbitrage adapted for a single-execution environment.

We don't do simultaneous buy+sell (true arb) because we can't short on Coinbase spot.
Instead, we detect when one exchange leads the other and trade the lagging one.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import requests

from src.types import TradeSignal, ScannerConfig, MarketContext
from src.storage.database import log
from src.signals._circuit_breaker import CircuitBreaker
from src.utils.binance_symbols import BINANCE_SYMBOL_MAP as _BINANCE_MAP
from src.utils.cache import TTLCache
from src.utils.safe_math import compute_zscore

_BINANCE_SPOT = "https://api.binance.com/api/v3"

_lock = threading.Lock()
_breaker = CircuitBreaker("binance_spot_price", failure_threshold=3, reset_timeout_s=120)
_binance_price_cache: TTLCache[str, float] = TTLCache(ttl_s=10)


@dataclass
class PriceSnapshot:
    """Price snapshot from both exchanges."""
    symbol: str
    coinbase_price: float
    binance_price: float
    divergence_pct: float  # (coinbase - binance) / binance * 100
    timestamp: float


_divergence_history: dict[str, list[PriceSnapshot]] = {}
_HISTORY_WINDOW_MS = 3_600_000
_MAX_HISTORY_PER_SYMBOL = 120
_cooldowns: dict[str, float] = {}
_COOLDOWN_MS = 300_000  # 5 minutes between signals for same symbol


def _fetch_binance_price(symbol: str) -> Optional[float]:
    """Fetch current spot price from Binance."""
    binance_ticker = _BINANCE_MAP.get(symbol.upper())
    if not binance_ticker:
        return None

    cached = _binance_price_cache.get(symbol)
    if cached is not None:
        return cached

    if not _breaker.can_call():
        raw = _binance_price_cache.get_raw(symbol)
        return raw[0] if raw else None

    try:
        resp = requests.get(
            f"{_BINANCE_SPOT}/ticker/price",
            params={"symbol": binance_ticker},
            timeout=5,
        )
        resp.raise_for_status()
        price = float(resp.json().get("price", 0))
        _breaker.record_success()

        if price > 0:
            _binance_price_cache.set(symbol, price)
            return price
        return None
    except Exception as err:
        _breaker.record_failure()
        return None


def record_price_snapshot(symbol: str, coinbase_price: float) -> Optional[PriceSnapshot]:
    """Record prices from both exchanges and compute divergence.

    Called on each tick to build up divergence history.
    """
    binance_price = _fetch_binance_price(symbol)
    if binance_price is None or binance_price <= 0 or coinbase_price <= 0:
        return None

    divergence_pct = ((coinbase_price - binance_price) / binance_price) * 100
    now = time.time() * 1000

    snap = PriceSnapshot(
        symbol=symbol,
        coinbase_price=coinbase_price,
        binance_price=binance_price,
        divergence_pct=divergence_pct,
        timestamp=now,
    )

    with _lock:
        history = _divergence_history.setdefault(symbol, [])
        history.append(snap)
        cutoff = now - _HISTORY_WINDOW_MS
        _divergence_history[symbol] = [
            s for s in history if s.timestamp >= cutoff
        ][-_MAX_HISTORY_PER_SYMBOL:]

    return snap


def _get_divergence_stats(symbol: str) -> Optional[dict]:
    """Compute divergence statistics from recent history."""
    with _lock:
        history = _divergence_history.get(symbol, [])
        if len(history) < 10:
            return None
        recent = list(history)

    divs = [s.divergence_pct for s in recent]
    current = divs[-1]
    z_score = compute_zscore(divs, current)

    # compute_zscore returns 0.0 for zero variance; also reject near-zero std
    avg_div = sum(divs) / len(divs)
    # Use sample std (n-1) not population std (n) for small sample sizes
    std_div = (sum((d - avg_div) ** 2 for d in divs) / (len(divs) - 1)) ** 0.5
    if std_div < 0.001:
        return None  # no variance = no signal

    return {
        "current_div_pct": current,
        "avg_div_pct": avg_div,
        "std_div_pct": std_div,
        "z_score": z_score,
        "sample_count": len(divs),
    }


def scan_cross_exchange_divergence(
    symbol: str, product_id: str, current_price: float,
    config: ScannerConfig, ctx: MarketContext,
) -> Optional[TradeSignal]:
    """Scan for cross-exchange price divergence trading opportunities.

    Signal logic:
    - If Coinbase price is significantly ABOVE Binance (z > 2.0):
      → Coinbase is overpriced, expect mean reversion → SHORT signal
    - If Coinbase price is significantly BELOW Binance (z < -2.0):
      → Coinbase is underpriced, expect mean reversion → LONG signal

    We trade on Coinbase (or paper), so we're fading the dislocation on
    the exchange where our capital sits.
    """
    if symbol.upper() not in _BINANCE_MAP:
        return None

    # Per-symbol cooldown to prevent rapid-fire signals
    now_cd = time.time() * 1000
    if symbol in _cooldowns and now_cd < _cooldowns[symbol]:
        # Still record the price snapshot to keep history fresh
        record_price_snapshot(symbol, current_price)
        return None

    snap = record_price_snapshot(symbol, current_price)
    if snap is None:
        return None

    stats = _get_divergence_stats(symbol)
    if stats is None:
        return None

    z = stats["z_score"]
    current_div = stats["current_div_pct"]
    now = time.time() * 1000

    # Divergence must exceed round-trip fees (~1.0-1.5% across exchanges) to have positive expected value.
    # Require at least 1.0% absolute divergence as minimum edge.
    _MIN_DIVERGENCE_PCT = 1.0
    if abs(current_div) < _MIN_DIVERGENCE_PCT:
        return None

    # Backtest fix: skip signals in extreme market phases where divergence persists.
    # 46 adverse_move + 35 low_volatility_chop losses in trending/extreme markets.
    if ctx.phase in ("extreme_fear", "extreme_greed"):
        return None

    # Coinbase significantly overpriced → SHORT (expect price to drop to Binance level)
    if z >= 2.5 and current_div > _MIN_DIVERGENCE_PCT:
        score = min(95, 50 + abs(z) * 6 + abs(current_div) * 10)
        _cooldowns[symbol] = now + _COOLDOWN_MS
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="cross_exchange_divergence", side="short", tier="swing",
            score=score, confidence="high",
            sources=["price_action", "correlation"],
            reasoning=(
                f"Coinbase {current_div:+.2f}% above Binance (z={z:.1f}), "
                f"avg spread {stats['avg_div_pct']:.3f}% ± {stats['std_div_pct']:.3f}%"
            ),
            entry_price=current_price,
            stop_price=current_price * 1.008,  # R:R fix: 0.8% stop vs ~1% target — tight stop, rely on mean reversion
            target_price=snap.binance_price,
            suggested_size_usd=50,
            expires_at=now + 3_600_000,
            created_at=now,
        )

    # Coinbase significantly underpriced → LONG (expect price to rise to Binance level)
    if z <= -2.5 and current_div < -_MIN_DIVERGENCE_PCT:
        score = min(95, 50 + abs(z) * 6 + abs(current_div) * 10)
        _cooldowns[symbol] = now + _COOLDOWN_MS
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=product_id,
            strategy="cross_exchange_divergence", side="long", tier="swing",
            score=score, confidence="high",
            sources=["price_action", "correlation"],
            reasoning=(
                f"Coinbase {current_div:+.2f}% below Binance (z={z:.1f}), "
                f"avg spread {stats['avg_div_pct']:.3f}% ± {stats['std_div_pct']:.3f}%"
            ),
            entry_price=current_price,
            stop_price=current_price * 0.992,  # R:R fix: 0.8% stop vs ~1% target — tight stop, rely on mean reversion
            target_price=snap.binance_price,
            suggested_size_usd=50,
            expires_at=now + 3_600_000,
            created_at=now,
        )

    return None


def get_divergence_stats() -> dict:
    """Get current divergence statistics for all tracked symbols."""
    with _lock:
        symbols = list(_divergence_history.keys())

    result = {}
    for sym in symbols:
        stats = _get_divergence_stats(sym)
        if stats:
            result[sym] = stats
    return result
