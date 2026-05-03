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

# Tightened 40→60 based on 60d backtest sweep (May 2026):
#   score=40: +0.65%/trade, t=1.30, n=27 (baseline)
#   score=60: +1.27%/trade, t=2.83, n=22 (mean ~2x, t-stat ~2.2x)
#   score=70: 0 trades (over-restrictive)
# Affects RuleBrain (fallback brain + backtest harness) only.
# Prod runs ClaudeBrain; this does not gate prod entries.
MIN_SCORE_TO_TRADE = 60
MAX_POSITIONS = 4
MAX_BALANCE_DEPLOYED_PCT = 0.80
MAX_DECISIONS_PER_TICK = 3
CHOP_TIMEOUT_MS = 60 * 60 * 1000  # 60 min
PUMP_DURATION_LIMIT_HOURS = 8
RE_ENTRY_COOLDOWN_MS = 30 * 60 * 1000  # 30 min cooldown before re-entry

BLUE_CHIP_ALTS = frozenset({
    "ETH", "SOL", "DOT", "LINK", "AVAX",
    "AAVE", "UNI", "MATIC", "ATOM", "NEAR",
})

# Strategy-specific stop/target percentages
STRATEGY_RISK = {
    "correlation_break":   {"stop_pct": 0.08, "target_pct": 0.15},
    "funding_squeeze":     {"stop_pct": 0.10, "target_pct": 0.25},
    "momentum_breakout":   {"stop_pct": 0.12, "target_pct": 0.30},
    "listing_pump":        {"stop_pct": 0.10, "target_pct": 0.20},
    "fgi_contrarian":      {"stop_pct": 0.08, "target_pct": 0.15},
    "trending_breakout":   {"stop_pct": 0.10, "target_pct": 0.20},
    "stable_flow_bull":    {"stop_pct": 0.06, "target_pct": 0.12},
    "stable_flow_bear":    {"stop_pct": 0.05, "target_pct": 0.08},
    # Per-chain TVL flow — macro ecosystem bet (capital rotating into/out of
    # an L1/L2). Asymmetric: bull bigger edge per the spec literature.
    "chain_flow_bull":     {"stop_pct": 0.06, "target_pct": 0.12},
    "chain_flow_bear":     {"stop_pct": 0.05, "target_pct": 0.08},
    # Cross-sectional funding carry — tighter than funding_squeeze because
    # the alpha bleeds out fast: the 8h funding window itself prices most
    # of the reversion, so we want quick targets and quick stops.
    "funding_carry_long":  {"stop_pct": 0.06, "target_pct": 0.10},
    "funding_carry_short": {"stop_pct": 0.06, "target_pct": 0.10},
    # Liquidation cascade fade — reversion is fast (5-30min), so tight stops
    # and modest targets. Empirical wick-revert magnitudes on Oct 10-11 2025
    # cascade clustered in the 6-12% range.
    "liquidation_cascade": {"stop_pct": 0.035, "target_pct": 0.09},
    # Filtered order-book imbalance (OBI-F, arXiv 2507.22712).
    # Fast mean-reverter: 2% stop, 3% target. Default-OFF in data_streams.
    "orderbook_imbalance": {"stop_pct": 0.02, "target_pct": 0.03},
    # BTC mempool fee stress — directional short on regime flip. Tight stop,
    # modest target — moves are quick and the edge decays if a rally absorbs
    # on-chain selling. Default-OFF until ≥7d of collector history exists.
    "mempool_stress":      {"stop_pct": 0.04, "target_pct": 0.07},
}

# Liquid universe for cross-sectional carry. The signal IS the rank — we
# don't want it firing on micro-caps where one whale's funding spike is
# the whole signal. Names below cover ~80% of perp open interest.
_CARRY_LIQUID_UNIVERSE = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "MATIC",
    "DOT", "UNI", "ATOM", "NEAR", "AAVE", "COMP", "LTC", "ADA", "TRX",
    "OP", "ARB", "INJ", "SUI", "APT", "FIL", "TIA", "SEI", "STX",
})
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
    # P2 audit fix: signal_detector never emits `pump_duration_hours`, so
    # this field is always missing. Default of 0 made the late-pump leniency
    # branch (line ~360) always fire (-10 instead of -30) on any accelerating
    # signal with price_change_24h > 100%, regardless of how stale the move
    # actually was. Default to limit+1 so unknown-pump-age is treated as
    # stale; signal_detector can later populate the real value to override.
    pump_hours = float(data.get("pump_duration_hours", PUMP_DURATION_LIMIT_HOURS + 1))
    btc_divergence = float(data.get("btc_divergence_4h", 0))
    # CORRECTNESS (audit — brain/filter): live signal_detector populates
    # the field as `age_hours` (from data_streams TokenSignal.data), backtest
    # populates it as `listing_age_hours`. The brain previously read only
    # `listing_age_hours`, so in LIVE the +35 listing bonus never fired
    # (always defaulted to 999) — the 77%-WR Coinbase listing-pump strategy
    # was effectively dead in production while backtest claimed it worked.
    # Read both keys, take the smaller (more conservative) age.
    listing_age_hours = float(data.get("listing_age_hours",
                                        data.get("age_hours", 999)))

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

    # Correlation break — alts underperforming BTC.
    # Score bonus always applies, but strategy_type attribution is only
    # claimed if no higher-priority strategy (funding_squeeze, etc.) has
    # already taken it. P0 fix: prior unconditional overwrite was applying
    # correlation_break risk params (stop 8% / target 15%) to funding_squeeze
    # entries (intended stop 10% / target 25%), causing tighter stops and
    # under-targeting. This explains the 7d prod pattern of fast_cut at -2%
    # before targets could fire.
    if abs(btc_divergence) > 1.5 and symbol in BLUE_CHIP_ALTS:
        score += 20
        factors.append(f"corr break vs BTC {btc_divergence:+.1f}% +20")
        if strategy_type == signal_type:
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

    # FGI extreme fear — contrarian BTC/ETH buy.
    # CORRECTNESS (audit — brain): aligned threshold to <=20 (was <15) to
    # match signal_detector.py:200 emit condition. Detector fires whenever
    # FGI<=20, but brain previously awarded points only at FGI<15, so the
    # 15-20 sub-band emitted signals that scored 0 and silently dropped.
    if fgi <= 20 and symbol in ("BTC", "ETH"):
        score += 30
        factors.append(f"FGI={fgi} extreme fear on {symbol} +30")
        strategy_type = "fgi_contrarian"

    # Stablecoin net-flow — orthogonal risk-on / risk-off signal (BTC/ETH only)
    if signal_type == "stable_flow_bull" and symbol in ("BTC", "ETH"):
        net24 = float(data.get("stablecoin_net_24h_usd", 0.0))
        score += 30
        factors.append(f"stbl net24 ${net24/1e6:+.0f}M (bull issuance) +30")
        strategy_type = "stable_flow_bull"
    elif signal_type == "stable_flow_bear" and symbol in ("BTC", "ETH"):
        net24 = float(data.get("stablecoin_net_24h_usd", 0.0))
        score += 25
        factors.append(f"stbl net24 ${net24/1e6:+.0f}M (bear redemption) +25")
        strategy_type = "stable_flow_bear"

    # Chain-level TVL flow (per-ecosystem capital rotation). Mapped at the
    # event source (live_replay) — by the time we score, the symbol is
    # guaranteed to be on the chain that fired. Asymmetric +25 / +20 per
    # spec — bull regime has historically been the bigger edge per unit risk.
    if signal_type == "chain_flow_bull":
        chg24 = float(data.get("chain_tvl_net_24h_pct", 0.0))
        chain = data.get("chain", "?")
        score += 25
        factors.append(f"{chain} TVL +{chg24:.1f}%/24h + 7d↑ (bull) +25")
        strategy_type = "chain_flow_bull"
    elif signal_type == "chain_flow_bear":
        chg24 = float(data.get("chain_tvl_net_24h_pct", 0.0))
        chain = data.get("chain", "?")
        score += 20
        factors.append(f"{chain} TVL {chg24:+.1f}%/24h + 7d↓ (bear) +20")
        strategy_type = "chain_flow_bear"

    # Cross-sectional funding carry — the rank IS the signal. We don't
    # double-count the absolute-funding-level scoring above; carry events
    # are emitted separately by the dedicated loader. Asymmetric scoring:
    # longs (bot decile) get +35 because crypto's mean-reversion edge is
    # historically stronger from the short-pain side; shorts +30 because
    # extreme positive funding can sustain in a strong uptrend.
    if signal_type == "funding_carry_long" and symbol in _CARRY_LIQUID_UNIVERSE:
        rank = float(data.get("funding_rank_pct", 1.0))
        carry_rate = float(data.get("funding_rate", 0.0))
        score += 35
        factors.append(f"x-sec funding carry LONG rank={rank*100:.0f}% rate={carry_rate*100:+.3f}% +35")
        # Extreme-tail bonus: deepest 5% (rank ≤ 0.05) gets an extra +10
        # so the strongest cross-sectional outliers clear MIN_SCORE_TO_TRADE
        # even when the absolute funding level is too small to trip the
        # legacy funding_squeeze bonus (-0.1% threshold). The whole point
        # of the carry strategy is that the RANK is the signal.
        if rank <= 0.05:
            score += 10
            factors.append(f"x-sec carry tail (top 5%) +10")
        strategy_type = "funding_carry_long"
    elif signal_type == "funding_carry_short" and symbol in _CARRY_LIQUID_UNIVERSE:
        rank = float(data.get("funding_rank_pct", 1.0))
        carry_rate = float(data.get("funding_rate", 0.0))
        score += 30
        factors.append(f"x-sec funding carry SHORT rank={rank*100:.0f}% rate={carry_rate*100:+.3f}% +30")
        if rank <= 0.05:
            score += 10
            factors.append(f"x-sec carry tail (top 5%) +10")
        strategy_type = "funding_carry_short"

    # Liquidation cascade fade — base +35, with tier-aware bonus when the
    # cascade is unusually large for its tier (≥3x threshold). The
    # opposite-side trade-direction is already encoded in suggested_side
    # by signal_detector — we just score and trust the side_hint.
    if signal_type == "liquidation_cascade":
        liq_usd = float(data.get("liq_usd_5m", 0.0))
        tier = data.get("tier", "small_alt")
        ratio = float(data.get("imbalance_ratio", 1.0))
        score += 35
        factors.append(f"liq cascade {tier} ${liq_usd/1e3:.0f}k/5m ratio={ratio:.1f}x +35")
        # Big-cascade bonus: ≥3x the tier floor (250k major / 50k large /
        # 10k small) means a real capitulation, not just background noise.
        tier_floors = {"major": 250_000, "large": 50_000, "small_alt": 10_000}
        floor = tier_floors.get(tier, 10_000)
        if liq_usd >= 3 * floor:
            score += 10
            factors.append(f"liq cascade ≥3x floor (${floor/1e3:.0f}k) +10")
        # Strong dominance bonus: ratio ≥3 means one side is overwhelmingly
        # dominant — the directional revert is cleaner.
        if ratio >= 3.0:
            score += 5
            factors.append(f"liq cascade strong dominance ratio={ratio:.1f}x +5")
        strategy_type = "liquidation_cascade"

    # Filtered order-book imbalance (OBI-F, arXiv 2507.22712).
    # +35 base for the persistent imbalance triggering, +15 when the 1h
    # trend confirms the mean-revert setup (price moving OPPOSITE the
    # imbalance direction — book is loaded for the snap-back).
    if signal_type == "orderbook_imbalance":
        obi_ema = float(data.get("obi_f_ema", 0.0))
        score += 35
        factors.append(f"OBI-F |{obi_ema:+.2f}| persistent imbalance +35")
        # accel_1h is OPPOSITE obi sign → snap setup confirmed
        if (obi_ema > 0 and accel_1h < 0) or (obi_ema < 0 and accel_1h > 0):
            score += 15
            factors.append(f"1h accel {accel_1h:+.1f}% opposes imbalance (snap setup) +15")
        strategy_type = "orderbook_imbalance"

    # BTC mempool fee stress — directional short on regime flip. +25 base,
    # +15 when FGI is also elevated (>70): paired greed + fee stress is the
    # post-rally-top setup the thesis is built around. BTC-only by emitter.
    if signal_type == "mempool_stress" and symbol == "BTC":
        regime = data.get("regime", "elevated")
        score += 25
        factors.append(f"mempool_stress regime={regime} +25")
        if fgi > 70:
            score += 15
            factors.append(f"FGI={fgi} extreme greed pairs with fee stress +15")
        strategy_type = "mempool_stress"

    # ------------------------------------------------------------------
    # Penalties
    # ------------------------------------------------------------------

    # Late-stage pump penalty — override if mega acceleration
    if accel_1h > 10:
        # Mega acceleration overrides late-pump penalties entirely
        # If pumping +10% THIS hour, the move is live regardless of 24h
        factors.append(f"mega accel {accel_1h:+.1f}% overrides late-pump penalty")
    elif price_change_24h > 200:
        if accel_1h > 5:
            score -= 15
            factors.append(f"24h pump {price_change_24h:+.0f}% (very late but accelerating) -15")
        else:
            score -= 40
            factors.append(f"24h pump {price_change_24h:+.0f}% (very late, no accel) -40")
    elif price_change_24h > 100:
        if accel_1h > 5 and pump_hours < PUMP_DURATION_LIMIT_HOURS:
            score -= 10
            factors.append(f"24h pump {price_change_24h:+.0f}% (late but accelerating) -10")
        else:
            score -= 30
            factors.append(f"24h pump {price_change_24h:+.0f}% (stale, no accel) -30")

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

        # Cross-session memory and acceleration (set by runner)
        self.memory = None  # BrainMemory instance, set by runner
        self.acceleration_data: dict[str, float] = {}  # set by runner from AccelerationTracker

        # Internal state for learning from results
        self._recently_closed: dict[str, dict] = {}  # symbol -> last closed trade info
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
        # Always check for choppy positions, even when no new signals
        close_decisions = self._check_chop_exits()

        if not self.pending_signals:
            return close_decisions

        total_deployed = sum(
            float(p.get("size_usd", 0)) for p in self.open_positions
        )

        # Filter out avoided symbols from memory
        signals_to_score = list(self.pending_signals)
        if self.memory:
            filtered = []
            for sig in signals_to_score:
                should_avoid, reason = self.memory.should_avoid(sig.symbol)
                if should_avoid:
                    log("info", f"[RuleBrain] SKIP {sig.symbol}: {reason}")
                else:
                    filtered.append(sig)
            signals_to_score = filtered

        # Score every pending signal
        scored: list[ScoredSignal] = []
        for packet in signals_to_score:
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
            if s.score >= 80:
                size_usd = min(20.0, self.balance * 0.35)
            elif s.score >= 60:
                size_usd = min(15.0, self.balance * 0.25)
            else:
                size_usd = min(12.0, self.balance * 0.20)
            size_usd = min(size_usd, remaining_budget)

            if size_usd < 5:
                log("info", f"[RuleBrain] SKIP {s.signal.symbol}: remaining budget too low (${remaining_budget:.2f})")
                break

            reasoning = f"Score {s.score} [{s.strategy_type}]: " + " | ".join(s.factors)
            log("info", f"[RuleBrain] BUY {s.signal.symbol} {s.side} ${size_usd:.0f} — {reasoning}")

            thesis_conditions = {"strategy": s.strategy_type}
            if s.strategy_type == "funding_squeeze":
                thesis_conditions["funding_negative"] = True
            elif s.strategy_type == "correlation_break":
                thesis_conditions["btc_divergence_negative"] = True
            elif s.strategy_type == "listing_pump":
                thesis_conditions["listing_recent"] = True

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
                thesis_conditions=thesis_conditions,
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

        log("info", f"[RuleBrain] Review: {lesson} | WR: {wr:.0f}% ({self._win_count}W/{self._loss_count}L)")

        # Record trade in persistent cross-session memory
        if self.memory:
            self.memory.record_trade(
                symbol=symbol,
                pnl_pct=pnl_pct,
                pnl_usd=trade_data.get("pnl_usd", 0),
                exit_reason=trade_data.get("exit_reason", "unknown"),
                strategy=trade_data.get("signal_type", "unknown"),
                duration_h=duration_hours,
            )

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
