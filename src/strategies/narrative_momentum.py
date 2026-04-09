"""Narrative Momentum Strategy — sector rotation play."""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.types import TradeSignal, ScannerConfig, MarketContext

NarrativeId = str

NARRATIVE_MEMBERS: dict[str, list[str]] = {
    "ai_tokens":        ["FET", "AGIX", "OCEAN", "RENDER", "TAO", "WLD"],
    "defi_bluechip":    ["UNI", "AAVE", "CRV", "MKR", "SNX", "COMP"],
    "layer2":           ["ARB", "OP", "MATIC", "IMX", "STRK", "MANTA"],
    "rwa":              ["ONDO", "POLYX", "CFG", "TRU", "MPL"],
    "gaming_metaverse": ["AXS", "SAND", "MANA", "GALA", "ILV", "YGG"],
    "meme":             ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"],
    "depin":            ["HNT", "MOBILE", "IOT", "RNDR", "FIL", "AR"],
    "liquid_staking":   ["LDO", "RPL", "SFRXETH", "ANKR", "PENDLE"],
    "btc_ecosystem":    ["STX", "ORDI", "SATS", "RATS", "MUBI"],
    "privacy":          ["XMR", "ZEC", "SCRT", "DUSK", "CTXC"],
}


@dataclass
class NarrativeState:
    id: str
    social_velocity: float = 1.0
    baseline_velocity: float = 0
    member_price_changes: dict[str, float] = field(default_factory=dict)
    last_updated: float = 0


_narrative_states: dict[str, NarrativeState] = {}
_lock = threading.Lock()
_MAX_NARRATIVE_STATES = 500

STRATEGY_META = {
    "strategies": [
        {"id": "narrative_momentum", "function": "scan_narrative_momentum",
         "description": "Detects narratives gaining momentum and trades laggard catch-up",
         "tier": "swing"},
    ],
    "signal_sources": ["social", "news"],
}


def _evict_lru_if_needed() -> None:
    """Evict least-recently-updated entries when _narrative_states exceeds max size."""
    if len(_narrative_states) <= _MAX_NARRATIVE_STATES:
        return
    sorted_keys = sorted(_narrative_states, key=lambda k: _narrative_states[k].last_updated)
    to_remove = len(_narrative_states) - _MAX_NARRATIVE_STATES
    for k in sorted_keys[:to_remove]:
        del _narrative_states[k]


def update_narrative_social_data(narrative_id: str, current_mentions: int, baseline_mentions: int) -> None:
    with _lock:
        if narrative_id not in _narrative_states:
            _narrative_states[narrative_id] = NarrativeState(id=narrative_id)
        _evict_lru_if_needed()
        state = _narrative_states[narrative_id]
    state.social_velocity = current_mentions / baseline_mentions if baseline_mentions > 0 else 1
    state.baseline_velocity = baseline_mentions
    state.last_updated = time.time() * 1000


def update_narrative_member_price(narrative_id: str, symbol: str, price_change_pct: float) -> None:
    with _lock:
        state = _narrative_states.get(narrative_id)
    if state:
        state.member_price_changes[symbol] = price_change_pct


def _find_laggard(state: NarrativeState, product_id_map: dict[str, str]) -> Optional[dict]:
    laggard = None
    for sym, pct in state.member_price_changes.items():
        pid = product_id_map.get(sym)
        if not pid:
            continue
        if laggard is None or pct < laggard["price_change_pct"]:
            laggard = {"symbol": sym, "product_id": pid, "price_change_pct": pct}
    return laggard


def scan_narrative_momentum(
    product_id_map: dict[str, str],
    config: ScannerConfig,
    current_prices: dict[str, float],
    ctx: Optional[MarketContext] = None,
) -> Optional[TradeSignal]:
    now = time.time() * 1000
    best_signal = None
    best_score = 0

    # Regime-adjust velocity threshold: require stronger signal in bear markets
    velocity_threshold = config.narrative_velocity_threshold
    if ctx and ctx.phase == "bear":
        velocity_threshold *= 1.5

    with _lock:
        snapshot = list(_narrative_states.items())

    for narrative_id, state in snapshot:
        if state.social_velocity < velocity_threshold:
            continue
        if now - state.last_updated > 1_800_000:
            continue

        laggard = _find_laggard(state, product_id_map)
        if not laggard:
            continue
        current_price = current_prices.get(laggard["symbol"])
        if not current_price:
            continue

        velocity_score = min(35, (state.social_velocity - velocity_threshold) * 10)
        laggard_score = min(20, max(0, -laggard["price_change_pct"] * 100))
        score = min(88, 48 + velocity_score + laggard_score)

        if score > best_score:
            best_score = score
            best_signal = TradeSignal(
                id=str(uuid.uuid4()), symbol=laggard["symbol"],
                product_id=laggard["product_id"],
                strategy="narrative_momentum", side="long", tier="swing", score=score,
                confidence="medium" if score > 72 else "low",
                sources=["social"],
                reasoning=f"{narrative_id.replace('_', ' ')} narrative velocity {state.social_velocity:.1f}x; {laggard['symbol']} lagging by {laggard['price_change_pct']*100:.1f}%",
                entry_price=current_price, stop_price=current_price * 0.94,
                suggested_size_usd=80,
                expires_at=now + 7_200_000, created_at=now,
            )

    return best_signal
