"""Position sizing — Kelly Criterion + fixed fractional hybrid."""

import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.config import env
from src.storage.database import get_closed_trades
from src.types import Position
from src.utils.safe_math import safe_score

_MIN_SIZE_USD = 10
_MIN_HISTORY = 30
_KELLY_FRACTION = 0.25


@dataclass
class StrategyStats:
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    sample_size: int


_lock = threading.Lock()
_stats_cache: dict[str, tuple[StrategyStats, float]] = {}
_STATS_TTL_MS = 600_000


def _compute_strategy_stats(strategy: str) -> StrategyStats:
    now = time.time() * 1000
    with _lock:
        cached = _stats_cache.get(strategy)
        if cached and now - cached[1] < _STATS_TTL_MS:
            return cached[0]

    trades = [t for t in get_closed_trades(200) if t.strategy == strategy and t.pnl_pct is not None]

    if len(trades) < _MIN_HISTORY:
        stats = StrategyStats(win_rate=0.5, avg_win_pct=0.04, avg_loss_pct=0.03, sample_size=len(trades))
        with _lock:
            _stats_cache[strategy] = (stats, now)
        return stats

    wins = [t for t in trades if (t.pnl_pct or 0) > 0]
    losses = [t for t in trades if (t.pnl_pct or 0) <= 0]

    win_rate = len(wins) / len(trades)
    avg_win_pct = sum(t.pnl_pct or 0 for t in wins) / len(wins) if wins else 0.04
    avg_loss_pct = abs(sum(t.pnl_pct or 0 for t in losses) / len(losses)) if losses else 0.03

    stats = StrategyStats(win_rate=win_rate, avg_win_pct=avg_win_pct, avg_loss_pct=avg_loss_pct, sample_size=len(trades))
    with _lock:
        _stats_cache[strategy] = (stats, now)
    return stats


def kelly_size(strategy: str, portfolio_usd: float, qual_score: float) -> float:
    stats = _compute_strategy_stats(strategy)

    if stats.sample_size < _MIN_HISTORY:
        fraction = 0.01
    else:
        b = stats.avg_win_pct / stats.avg_loss_pct if stats.avg_loss_pct > 0 else 1
        p = stats.win_rate
        q = 1 - p
        raw_kelly = (b * p - q) / b
        if raw_kelly <= 0:
            return 0
        fraction = raw_kelly * _KELLY_FRACTION

    # Cap qual_multiplier at 1.0 so high-qual trades never exceed quarter-Kelly
    qual_multiplier = min(1.0, 0.5 + (qual_score / 100))
    raw_usd = fraction * portfolio_usd * qual_multiplier

    # Apply drawdown scaling
    raw_usd = apply_drawdown_scaling(raw_usd, portfolio_usd)

    return safe_score(raw_usd, _MIN_SIZE_USD, env.max_position_usd)


# ─── Graduated drawdown reduction ────────────────────────────────────────────

_peak_lock = threading.Lock()
_peak_portfolio_usd: float = 0.0


def update_peak(portfolio_usd: float) -> None:
    """Update peak portfolio value for drawdown calculation."""
    global _peak_portfolio_usd
    with _peak_lock:
        if portfolio_usd > _peak_portfolio_usd:
            _peak_portfolio_usd = portfolio_usd


def apply_drawdown_scaling(base_size_usd: float, portfolio_usd: float) -> float:
    """Reduce position size based on drawdown from peak.

    Drawdown tiers:
      0%  -> 100% size
      5%  -> 75% size
      10% -> 50% size
      15% -> 25% size
      20%+ -> 10% size

    Uses linear interpolation between tiers.
    """
    with _peak_lock:
        peak = _peak_portfolio_usd

    if peak <= 0 or portfolio_usd >= peak:
        return base_size_usd

    drawdown_pct = (peak - portfolio_usd) / peak

    # Define tiers: (drawdown_threshold, size_multiplier)
    tiers = [
        (0.00, 1.00),
        (0.05, 0.75),
        (0.10, 0.50),
        (0.15, 0.25),
        (0.20, 0.10),
    ]

    # Find the tier bracket
    if drawdown_pct >= tiers[-1][0]:
        multiplier = tiers[-1][1]
    else:
        for i in range(len(tiers) - 1):
            if tiers[i][0] <= drawdown_pct < tiers[i + 1][0]:
                # Linear interpolation
                t0, m0 = tiers[i]
                t1, m1 = tiers[i + 1]
                ratio = (drawdown_pct - t0) / (t1 - t0)
                multiplier = m0 + ratio * (m1 - m0)
                break
        else:
            multiplier = 1.0

    return base_size_usd * multiplier


# ─── Correlation-aware sizing ─────────────────────────────────────────────────

# Correlation groups: assets that move together get reduced sizing when stacked
_CORRELATION_GROUPS: dict[str, str] = {
    "BTC": "btc",
    "ETH": "eth_l1",
    "SOL": "alt_l1", "AVAX": "alt_l1", "NEAR": "alt_l1", "SUI": "alt_l1", "APT": "alt_l1",
    "MATIC": "eth_l2", "ARB": "eth_l2", "OP": "eth_l2", "BASE": "eth_l2",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "WIF": "meme", "BONK": "meme",
    "LINK": "defi_infra", "AAVE": "defi_infra", "UNI": "defi_infra", "MKR": "defi_infra",
}

# Discount per additional same-group same-side position
_CORRELATION_DISCOUNT_PER_POS = 0.30  # 30% reduction per correlated position
_MIN_CORRELATION_MULTIPLIER = 0.25    # never reduce below 25% of original size


def _normalize_symbol(symbol: str) -> str:
    """Strip exchange suffixes to get base symbol (e.g. 'SOL-USD' -> 'SOL')."""
    return symbol.replace("-USD", "").replace("-USDT", "").replace("USDT", "").replace("USD", "")


def _sector_exposure(group: str, open_positions: list[Position]) -> float:
    """Sum current USD exposure in a correlation group across open positions."""
    return sum(
        pos.size_usd for pos in open_positions
        if _CORRELATION_GROUPS.get(_normalize_symbol(pos.symbol)) == group
    )


def apply_correlation_discount(base_size_usd: float, symbol: str, side: str,
                               open_positions: list[Position]) -> float:
    """Reduce position size when entering a correlated asset on the same side."""
    base_sym = _normalize_symbol(symbol)
    group = _CORRELATION_GROUPS.get(base_sym)
    if not group:
        return base_size_usd

    correlated_count = sum(
        1 for pos in open_positions
        if _normalize_symbol(pos.symbol) != base_sym
        and _CORRELATION_GROUPS.get(_normalize_symbol(pos.symbol)) == group
        and pos.side == side
    )

    if correlated_count == 0:
        return base_size_usd

    multiplier = max(_MIN_CORRELATION_MULTIPLIER,
                     1.0 - correlated_count * _CORRELATION_DISCOUNT_PER_POS)
    return base_size_usd * multiplier


# ─── Sector exposure limits ──────────────────────────────────────────────────

_MAX_SECTOR_EXPOSURE_PCT = 0.30  # max 30% of portfolio in one correlation group


def check_sector_exposure(symbol: str, side: str, proposed_size_usd: float,
                          portfolio_usd: float, open_positions: list[Position]) -> float:
    """Cap position size so total sector exposure doesn't exceed 30% of portfolio."""
    base_sym = _normalize_symbol(symbol)
    group = _CORRELATION_GROUPS.get(base_sym)
    if not group or portfolio_usd <= 0:
        return proposed_size_usd

    max_sector_usd = portfolio_usd * _MAX_SECTOR_EXPOSURE_PCT
    current_exposure = _sector_exposure(group, open_positions)

    remaining = max_sector_usd - current_exposure
    if remaining <= 0:
        return 0.0

    return min(proposed_size_usd, remaining)


def log_kelly_rationale(strategy: str) -> str:
    stats = _compute_strategy_stats(strategy)
    if stats.sample_size < _MIN_HISTORY:
        return f"{strategy}: insufficient history ({stats.sample_size}/{_MIN_HISTORY}) — using 1% fixed-fractional"
    b = stats.avg_win_pct / stats.avg_loss_pct if stats.avg_loss_pct > 0 else 1
    p = stats.win_rate
    q = 1 - p
    raw_kelly = (b * p - q) / b
    return (f"{strategy}: win_rate={p*100:.0f}% avg_win={stats.avg_win_pct*100:.1f}% "
            f"avg_loss={stats.avg_loss_pct*100:.1f}% kelly={raw_kelly*100:.1f}% "
            f"-> quarter_kelly={raw_kelly*_KELLY_FRACTION*100:.2f}%")
