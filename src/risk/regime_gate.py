"""Regime-based strategy gating — hard block strategies in unfavorable regimes.

Unlike the score-based adjustment in scorer.py which penalizes by -8 points,
this module completely blocks a strategy from generating signals when the
regime is strongly incompatible.
"""

from src.indicators.regime import classify_regime, RegimeSnapshot
from src.storage.database import log
from src.types import StrategyId
from src.utils.cache import TTLCache

_REGIME_BLOCKS: dict[StrategyId, list[dict]] = {
    "mean_reversion": [
        {"trend": "trending_up", "min_strength": 35},
        {"trend": "trending_down", "min_strength": 35},
    ],
    "fear_greed_contrarian": [
        {"trend": "trending_up", "min_strength": 35},
        {"trend": "trending_down", "min_strength": 35},
    ],
    "momentum_swing": [
        {"trend": "ranging", "volatility": "low_vol"},
    ],
    "momentum_scalp": [
        {"trend": "ranging", "volatility": "low_vol"},
    ],
    "funding_extreme": [
        {"phase": "distribution"},
    ],
}

_gate_cache: TTLCache[str, bool] = TTLCache(ttl_s=15)


def is_regime_blocked(strategy: str, symbol: str = "BTC") -> bool:
    """Check if a strategy should be blocked based on current market regime."""
    cached = _gate_cache.get(strategy)
    if cached is not None:
        return cached

    blocks = _REGIME_BLOCKS.get(strategy)
    if not blocks:
        _gate_cache.set(strategy, False)
        return False

    regime = classify_regime(symbol)
    blocked = _check_blocks(regime, blocks)

    _gate_cache.set(strategy, blocked)

    if blocked:
        log("info", f"Regime gate: {strategy} blocked "
            f"(trend={regime.trend} str={regime.trend_strength:.0f} "
            f"vol={regime.volatility} phase={regime.phase})",
            strategy=strategy)

    return blocked


def _check_blocks(regime: RegimeSnapshot, blocks: list[dict]) -> bool:
    """Check if any block condition matches the current regime."""
    for block in blocks:
        match = True
        if "trend" in block and regime.trend != block["trend"]:
            match = False
        if "min_strength" in block and regime.trend_strength < block["min_strength"]:
            match = False
        if "volatility" in block and regime.volatility != block["volatility"]:
            match = False
        if "phase" in block and regime.phase != block["phase"]:
            match = False
        if match:
            return True
    return False


def get_blocked_strategies(symbol: str = "BTC") -> list[StrategyId]:
    """Get list of currently blocked strategies."""
    return [s for s in _REGIME_BLOCKS if is_regime_blocked(s, symbol)]
