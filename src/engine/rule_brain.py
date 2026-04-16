"""Rule-based trading brain — zero API cost fallback.

Encodes hard-won lessons from a full day of live Binance Futures trading.
Used automatically when no ANTHROPIC_API_KEY is set, giving the same
interface as ClaudeBrain but with deterministic, rule-based decisions.

Key principles:
    1. 1h acceleration is the primary signal — fresh breakouts only
    2. Funding squeeze + acceleration = highest conviction setup
    3. Don't chase late-stage pumps (>100% 24h already)
    4. Strategy-specific risk management (stops + targets)
    5. Cut choppy trades fast (>60 min sideways)
    6. Never re-enter same token at higher price after a loss
    7. Position sizing: min($20, balance * 0.3), max 80% deployed
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from src.engine.claude_brain import TradeDecision
from src.engine.signal_detector import SignalPacket
from src.engine.log import log


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_SCORE_TO_TRADE = 40
MAX_POSITIONS = 4
MAX_BALANCE_DEPLOYED_PCT = 0.80
MAX_DECISIONS_PER_TICK = 3
CHOP_TIMEOUT_MS = 60 * 60 * 1000  # 60 min
PUMP_DURATION_LIMIT_HOURS = 8
RE_ENTRY_COOLDOWN_MS = 30 * 60 * 1000  # 30 min cooldown before re-entry

BLUE_CHIP_ALTS = frozenset({
    "ETHUSDT", "SOLUSDT", "DOTUSDT", "LINKUSDT", "AVAXUSDT",
    "AAVEUSDT", "UNIUSDT", "MATICUSDT", "ATOMUSDT", "NEARUSDT",
})

# Strategy-specific stop/target percentages
STRATEGY_RISK = {
    "correlation_break":   {"stop_pct": 0.08, "target_pct": 0.15},
    "funding_squeeze":     {"stop_pct": 0.10, "target_pct": 0.25},
    "momentum_breakout":   {"stop_pct": 0.12, "target_pct": 0.30},
    "listing_pump":        {"stop_pct": 0.10, "target_pct": 0.20},
    "fgi_contrarian":      {"stop_pct": 0.08, "target_pct": 0.15},
    "trending_breakout":   {"stop_pct": 0.10, "target_pct": 0.20},
}
DEFAULT_RISK = {"stop_pct": 0.10, "target_pct": 0.20}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class ScoredSignal:
    """A signal with its computed score and reasoning breakdown."""
    signal: SignalPacket
    score: int
    factors: list[str]
    strategy_type: str
    stop_pct: float
    target_pct: float
    side: str


def _score_signal(
    signal: SignalPacket,
    funding_rates: dict[str, float],
    fgi: int,
    positions: list[dict],
    recently_closed: dict[str, dict],
    balance: float,
    total_deployed: float,
) -> Optional[ScoredSignal]:
    """Score a signal using multi-factor rules. Returns None if hard-filtered."""

    symbol = signal.symbol
    factors: list[str] = []
    score = 0

    # ------------------------------------------------------------------
    # Hard filters — reject before scoring
    # ------------------------------------------------------------------

    # Already in a position on this symbol
    open_symbols = {p.get("symbol", "") for p in positions}
    if symbol in open_symbols:
        log("info", f"[RuleBrain] SKIP {symbol}: already in position")
        return None

    # Balance too low
    if balance < 5:
        log("info", f"[RuleBrain] SKIP {symbol}: balance too low (${balance:.2f})")
        return None

    # Max positions reached
    if len(positions) >= MAX_POSITIONS:
        log("info", f"[RuleBrain] SKIP {symbol}: max positions ({MAX_POSITIONS}) reached")
        return None

    # Max balance deployed
    if total_deployed >= balance * MAX_BALANCE_DEPLOYED_PCT:
        log("info", f"[RuleBrain] SKIP {symbol}: >{MAX_BALANCE_DEPLOYED_PCT*100:.0f}% balance deployed")
        return None

    # ------------------------------------------------------------------
    # Extract signal data
    # ------------------------------------------------------------------

    data = signal.data or {}
    price_change_24h = signal.price_change_24h or 0
    volume_24h = signal.volume_24h or 0
    funding_rate = funding_rates.get(symbol, signal.funding_rate or 0)
    accel_1h = float(data.get("acceleration_1h", 0))
    pump_hours = float(data.get("pump_duration_hours", 0))
    btc_divergence = float(data.get("btc_divergence_4h", 0))
    listing_age_hours = float(data.get("listing_age_hours", 999))

    # Determine strategy type from signal
    signal_type = signal.signal_type or ""
    strategy_type = signal_type  # default to signal type

    # ------------------------------------------------------------------
    # Scoring factors
    # ------------------------------------------------------------------

    # 1h acceleration — THE key signal
    if accel_1h > 10:
        score += 50
        factors.append(f"1h accel {accel_1h:+.1f}% (strong) +50")
    elif accel_1h > 5:
        score += 30
        factors.append(f"1h accel {accel_1h:+.1f}% +30")

    # Funding squeeze
    if funding_rate < -0.002:  # < -0.2%
        score += 40
        factors.append(f"extreme neg funding {funding_rate*100:+.3f}% +40")
        strategy_type = "funding_squeeze"
    elif funding_rate < -0.001:  # < -0.1%
        score += 25
        factors.append(f"neg funding {funding_rate*100:+.3f}% +25")
        if accel_1h > 5:
            strategy_type = "funding_squeeze"

    # Correlation break — alts underperforming BTC
    if abs(btc_divergence) > 1.5 and symbol in BLUE_CHIP_ALTS:
        score += 20
        factors.append(f"corr break vs BTC {btc_divergence:+.1f}% +20")
        strategy_type = "correlation_break"

    # Volume
    if volume_24h > 500_000_000:
        score += 25
        factors.append(f"vol ${volume_24h/1e6:.0f}M (massive) +25")
    elif volume_24h > 100_000_000:
        score += 15
        factors.append(f"vol ${volume_24h/1e6:.0f}M +15")

    # New listing bonus
    if listing_age_hours < 6:
        score += 35
        factors.append(f"new listing {listing_age_hours:.1f}h old (77% WR) +35")
        strategy_type = "listing_pump"

    # FGI extreme fear — contrarian BTC/ETH buy
    if fgi < 15 and symbol in ("BTCUSDT", "ETHUSDT"):
        score += 30
        factors.append(f"FGI={fgi} extreme fear on {symbol} +30")
        strategy_type = "fgi_contrarian"

    # ------------------------------------------------------------------
    # Penalties
    # ------------------------------------------------------------------

    # Late-stage pump penalty
    if price_change_24h > 200:
        score -= 40
        factors.append(f"24h pump {price_change_24h:+.0f}% (very late) -40")
    elif price_change_24h > 100:
        # Only allow if fresh acceleration AND pump hasn't lasted too long
        if accel_1h > 5 and pump_hours < PUMP_DURATION_LIMIT_HOURS:
            score -= 20
            factors.append(f"24h pump {price_change_24h:+.0f}% (late but accelerating) -20")
        else:
            score -= 40
            factors.append(f"24h pump {price_change_24h:+.0f}% (stale, no accel) -40")

    # Revenge trading penalty — recently closed at a loss
    recent = recently_closed.get(symbol)
    if recent:
        closed_at = recent.get("closed_at", 0)
        pnl_pct = recent.get("pnl_pct", 0)
        ms_since = time.time() * 1000 - closed_at

        if pnl_pct < 0 and ms_since < RE_ENTRY_COOLDOWN_MS:
            score -= 30
            factors.append(f"recently closed at loss ({pnl_pct:+.1f}%) -30")

        # Don't re-enter at higher price after closing
        last_exit = recent.get("exit_price", 0)
        if last_exit and signal.price_usd > last_exit:
            score -= 20
            factors.append(f"price above last exit (${last_exit:.4f} -> ${signal.price_usd:.4f}) -20")

    # ------------------------------------------------------------------
    # Determine side and risk
    # ------------------------------------------------------------------

    # Default to long; short only for specific setups
    side = signal.suggested_side or "long"

    # Correlation break: if alt is underperforming (negative divergence), go long
    if strategy_type == "correlation_break" and btc_divergence < -1.5:
        side = "long"

    risk = STRATEGY_RISK.get(strategy_type, DEFAULT_RISK)

    return ScoredSignal(
        signal=signal,
        score=score,
        factors=factors,
        strategy_type=strategy_type,
        stop_pct=risk["stop_pct"],
        target_pct=risk["target_pct"],
        side=side,
    )


# ---------------------------------------------------------------------------
# RuleBrain
# ---------------------------------------------------------------------------

class RuleBrain:
    """Deterministic rule-based trading brain.

    Drop-in replacement for ClaudeBrain when no API key is available.
    Encodes lessons from live Binance Futures trading into a scoring system.
    """

    def __init__(self, balance: float = 10_000):
        self.balance = balance
        self.open_positions: list[dict] = []
        self.daily_pnl: float = 0
        self.pending_signals: list[SignalPacket] = []

        # Market state (set by runner before each tick)
        self.fgi: int = 50
        self.fgi_class: str = "Neutral"
        self.funding_rates: dict[str, float] = {}
        self.trending_tokens: list[str] = []
        self.recent_listings: list[dict] = []
        self.latest_news: list[dict] = []

        # Social (same interface as ClaudeBrain)
        self.reddit_sentiment: float = 0.0
        self.reddit_post_count: int = 0

        # Internal state for learning from results
        self._recently_closed: dict[str, dict] = {}  # symbol -> last closed trade info
        self._lessons: list[str] = []
        self._win_count: int = 0
        self._loss_count: int = 0

    # ------------------------------------------------------------------
    # Public interface (matches ClaudeBrain)
    # ------------------------------------------------------------------

    def add_signal(self, packet: SignalPacket) -> None:
        """Queue a signal for the next tick."""
        self.pending_signals.append(packet)
        if len(self.pending_signals) > 20:
            self.pending_signals = self.pending_signals[-20:]

    def tick(self) -> list[TradeDecision]:
        """Run one brain tick — score signals, filter, rank, return decisions.

        Called every 60 seconds by the runner. Zero API cost.
        """
        if not self.pending_signals:
            return []

        total_deployed = sum(
            float(p.get("size_usd", 0)) for p in self.open_positions
        )

        # Also check for choppy positions that should be closed
        close_decisions = self._check_chop_exits()

        # Score every pending signal
        scored: list[ScoredSignal] = []
        for packet in self.pending_signals:
            result = _score_signal(
                signal=packet,
                funding_rates=self.funding_rates,
                fgi=self.fgi,
                positions=self.open_positions,
                recently_closed=self._recently_closed,
                balance=self.balance,
                total_deployed=total_deployed,
            )
            if result is not None:
                scored.append(result)

        # Clear processed signals
        self.pending_signals = []

        # Filter by minimum score
        qualified = [s for s in scored if s.score >= MIN_SCORE_TO_TRADE]

        if not qualified:
            if scored:
                top = max(scored, key=lambda s: s.score)
                log("info", f"[RuleBrain] No signals above threshold. Best: {top.signal.symbol} score={top.score}")
            return close_decisions

        # Sort by score descending, take top N
        qualified.sort(key=lambda s: s.score, reverse=True)
        top_signals = qualified[:MAX_DECISIONS_PER_TICK]

        # Ensure we don't exceed balance limits across the batch
        decisions: list[TradeDecision] = list(close_decisions)
        remaining_budget = (self.balance * MAX_BALANCE_DEPLOYED_PCT) - total_deployed

        for s in top_signals:
            size_usd = min(20.0, self.balance * 0.3)
            size_usd = min(size_usd, remaining_budget)

            if size_usd < 5:
                log("info", f"[RuleBrain] SKIP {s.signal.symbol}: remaining budget too low (${remaining_budget:.2f})")
                break

            reasoning = f"Score {s.score} [{s.strategy_type}]: " + " | ".join(s.factors)
            log("info", f"[RuleBrain] BUY {s.signal.symbol} {s.side} ${size_usd:.0f} — {reasoning}")

            decisions.append(TradeDecision(
                action="BUY",
                symbol=s.signal.symbol,
                side=s.side,
                size_usd=size_usd,
                entry_price=0,  # filled by executor from live price
                stop_pct=s.stop_pct,
                target_pct=s.target_pct,
                confidence=_score_to_confidence(s.score),
                reasoning=reasoning,
                signal_id=s.signal.signal_id or f"rule-{int(time.time())}",
                timestamp=time.time() * 1000,
            ))

            remaining_budget -= size_usd

        return decisions

    def review_trade(self, trade_data: dict) -> None:
        """Learn from a closed trade. Updates internal state for future decisions."""
        symbol = trade_data.get("symbol", "")
        pnl_pct = float(trade_data.get("pnl_pct", 0))
        exit_price = float(trade_data.get("exit", trade_data.get("exit_price", 0)))
        closed_at = trade_data.get("closed_at", time.time() * 1000)

        # Track for re-entry prevention
        self._recently_closed[symbol] = {
            "pnl_pct": pnl_pct,
            "exit_price": exit_price,
            "closed_at": closed_at,
        }

        # Track win/loss stats
        if pnl_pct > 0:
            self._win_count += 1
        else:
            self._loss_count += 1

        total = self._win_count + self._loss_count
        wr = (self._win_count / total * 100) if total > 0 else 0

        # Generate lesson
        signal_type = trade_data.get("signal_type", "unknown")
        duration_hours = float(trade_data.get("duration_hours", 0))

        if pnl_pct < -5:
            lesson = f"Large loss on {symbol} ({signal_type}): {pnl_pct:+.1f}% in {duration_hours:.1f}h — tighten stops"
        elif pnl_pct < 0 and duration_hours < 1:
            lesson = f"Quick stop on {symbol} ({signal_type}): stopped out in {duration_hours:.1f}h — possible false breakout"
        elif pnl_pct > 10:
            lesson = f"Strong win on {symbol} ({signal_type}): {pnl_pct:+.1f}% — this setup works"
        else:
            lesson = f"Trade on {symbol} ({signal_type}): {pnl_pct:+.1f}% in {duration_hours:.1f}h"

        self._lessons.append(lesson)
        self._lessons = self._lessons[-50:]

        log("info", f"[RuleBrain] Review: {lesson} | WR: {wr:.0f}% ({self._win_count}W/{self._loss_count}L)")

        # Expire old recently-closed entries (older than 2 hours)
        now_ms = time.time() * 1000
        expired = [
            sym for sym, info in self._recently_closed.items()
            if now_ms - info.get("closed_at", 0) > 2 * 60 * 60 * 1000
        ]
        for sym in expired:
            del self._recently_closed[sym]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_chop_exits(self) -> list[TradeDecision]:
        """Check if any open position has been chopping sideways and should be closed."""
        decisions: list[TradeDecision] = []
        now_ms = time.time() * 1000

        for pos in self.open_positions:
            opened_at = float(pos.get("opened_at", now_ms))
            hold_ms = now_ms - opened_at
            pnl_pct = float(pos.get("pnl_pct", 0))
            symbol = pos.get("symbol", "")

            # If held for >60 min with no meaningful progress, close it
            if hold_ms > CHOP_TIMEOUT_MS and -2.0 < pnl_pct < 2.0:
                reasoning = (
                    f"Chop exit: {symbol} held {hold_ms / 60_000:.0f}min "
                    f"with only {pnl_pct:+.1f}% progress — cutting"
                )
                log("info", f"[RuleBrain] CLOSE {symbol} — {reasoning}")
                decisions.append(TradeDecision(
                    action="CLOSE",
                    symbol=symbol,
                    side="",
                    size_usd=0,
                    entry_price=0,
                    stop_pct=0,
                    target_pct=0,
                    confidence="high",
                    reasoning=reasoning,
                    signal_id=f"chop-{int(time.time())}",
                    timestamp=now_ms,
                ))

        return decisions

    def get_daily_cost_estimate(self) -> float:
        """Rule brain has zero API cost."""
        return 0.0


def _score_to_confidence(score: int) -> str:
    """Map numeric score to confidence level."""
    if score >= 80:
        return "high"
    elif score >= 55:
        return "medium"
    return "low"
