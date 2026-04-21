"""Claude-powered trading brain — elite autonomous crypto trader.

Architecture:
    Haiku (fast, cheap) scans market every 60s for signals     ~$0.00025/call
    Sonnet (smart) validates and sizes actual trade entries     ~$0.003/call
    Haiku reviews closed trades for lessons                    ~$0.00025/call

Total cost: ~$1-2/day at 60s ticks with active trading.

The brain sees EVERYTHING every minute:
    - All open positions with live P&L and hold duration
    - 1h price acceleration for top movers (THE key signal)
    - Funding rates across all pairs (squeeze detection)
    - Fear & Greed Index, trending tokens, new listings
    - Reddit sentiment, crypto news headlines
    - Correlation scanner signals (BTC-alt divergence)

Live trading results encoded in prompts:
    - Funding squeeze: ENJ +26%, MBOX +25%, WAL +7%
    - Correlation break: DOT +10% over 2 days
    - Listing pump: 77% WR on Coinbase listings
    - Chop exit: cuts dead trades after 1h with <2% movement
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from src.engine.signal_detector import SignalPacket
from src.engine.log import log


@dataclass
class TradeDecision:
    """Claude's decision on whether and how to trade."""
    action: str             # "BUY", "SELL", "CLOSE", "SKIP"
    symbol: str
    side: str               # "long" or "short"
    size_usd: float
    entry_price: float
    stop_pct: float
    target_pct: float
    confidence: str         # "high", "medium", "low"
    reasoning: str
    signal_id: str
    timestamp: float
    thesis_conditions: dict = field(default_factory=dict)


@dataclass
class TradeReview:
    """Claude's post-trade analysis."""
    trade_id: str
    symbol: str
    pnl_pct: float
    reasoning: str
    lesson: str
    adjustment: str


# ---------------------------------------------------------------------------
# System prompt: persistent identity and strategy knowledge
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an elite autonomous crypto futures trader running 24/7 on Binance/OKX.
You see real-time market data every 60 seconds and make trading decisions as JSON.

PROVEN STRATEGIES (from live trading, ranked by profit):

1. FUNDING SQUEEZE (highest conviction)
   - Signal: funding rate < -0.10% (shorts paying longs heavily)
   - Confirmation: price accelerating >5% in current 1h candle
   - Why it works: crowded shorts get liquidated in cascade, explosive move
   - Live results: ENJ +26.5%, MBOX +25.4%, WAL +6.6%
   - Risk: stop -10%, target +25%
   - You GET PAID to hold via funding rate (~0.1-0.5% every 8h)

2. CORRELATION BREAK (slow but reliable)
   - Signal: alt underperforming BTC by >1.5% on 4h timeframe
   - Why: blue-chip alts mean-revert to BTC correlation
   - Best on: ETH, SOL, DOT, LINK, AVAX, AAVE, SUI, NEAR
   - Live results: DOT +10% over 2 days
   - Risk: stop -8%, target +15%

3. LISTING PUMP (time-sensitive, high WR)
   - Signal: new Coinbase listing (77% WR) or Binance Futures listing
   - Entry: within first 2 hours of listing announcement
   - Risk: stop -10%, target +20%

4. FGI CONTRARIAN (rare but powerful)
   - Signal: Fear & Greed Index < 15 (extreme fear)
   - Entry: BUY BTC or ETH only
   - Risk: stop -8%, target +15%

CRITICAL RULES FROM LIVE EXPERIENCE:

- 1H ACCELERATION is THE key signal. A token pumping +10% THIS hour matters
  more than one that's +200% on 24h. Fresh breakouts > stale pumps.
- NEVER chase tokens already up +100% on 24h WITHOUT >5% 1h acceleration.
  Late entries are exit liquidity. We lost money on every late pump chase.
- CHOP EXIT: if a position has been open >60 min with <2% absolute movement
  in either direction, CLOSE it. Dead capital = missed opportunity.
- MAX 4 POSITIONS at once. Each position = min($20, 30% of balance).
- 1x LEVERAGE ALWAYS. No exceptions.
- PATIENCE IS EDGE. Zero trades on a quiet market is the correct answer.
  Don't force entries. Wait for high-conviction setups.
- TRAILING STOPS work. After a position reaches 1.5x the stop distance in
  profit, the stop trails upward. Let winners run.
- CUT LOSERS FAST. If the thesis is broken (e.g., funding flipped positive
  on a squeeze play), close immediately regardless of P&L.

POSITION MANAGEMENT (for CLOSE decisions):
- Check each open position every tick
- CLOSE if: held >60min with <2% move (chop exit)
- CLOSE if: original thesis broken (funding flipped, trend reversed)
- CLOSE if: held >24h with no progress toward target
- Let winning positions run toward target — don't take early profit unless
  thesis is weakening

OUTPUT FORMAT:
Respond with a JSON array. Max 3 actions per tick.
BUY/CLOSE only — no SELL (we go long only on perpetual futures).
Empty array [] means no action (this is often correct).

[{"action":"BUY","symbol":"ENJ","side":"long","size_usd":12,"stop_pct":0.10,"target_pct":0.25,"confidence":"high","reasoning":"funding -0.22% + 1h accel +5.8% = squeeze setup","thesis_conditions":{"funding_negative":true,"strategy":"funding_squeeze"}}]
[{"action":"CLOSE","symbol":"BTC","reasoning":"held 2h, only +0.3%, chop exit"}]
[]"""

# ---------------------------------------------------------------------------
# Tick prompt: compact market state for each 60s scan
# ---------------------------------------------------------------------------

_TICK_PROMPT = """MARKET STATE ({timestamp}):

Portfolio: ${balance:,.0f} | Day P&L: ${daily_pnl:+,.2f} | FGI: {fgi} ({fgi_class})

POSITIONS ({n_positions}):
{positions_text}

SIGNALS (new since last tick):
{signals_text}

1H ACCELERATION (top movers THIS hour):
{accel_text}

FUNDING EXTREMES (negative = longs get paid):
{funding_text}

TRENDING: {trending_text}
LISTINGS: {listings_text}
NEWS: {news_text}
REDDIT: {reddit_sentiment:+.1f} sentiment ({reddit_post_count} posts)

Decide: JSON array of actions, or [] for no action."""

# ---------------------------------------------------------------------------
# Sonnet validation prompt: smarter model confirms trade before execution
# ---------------------------------------------------------------------------

_VALIDATE_PROMPT = """You are validating a trade decision from the fast scanner.

PROPOSED TRADE:
{trade_json}

MARKET CONTEXT:
- Balance: ${balance:,.0f} | Open positions: {n_positions}
- FGI: {fgi} | Funding for {symbol}: {symbol_funding}
- 1h acceleration: {accel_1h}
- 24h change: {change_24h}

VALIDATION CHECKLIST:
1. Is the thesis clear and backed by data?
2. Is the stop/target ratio at least 2:1?
3. Are we chasing a late pump (>100% 24h without fresh acceleration)?
4. Do we already have a correlated position open?
5. Is the position size appropriate for our balance?

Respond with ONE JSON object:
{{"approved": true/false, "size_usd": N, "stop_pct": 0.10, "target_pct": 0.25, "reasoning": "..."}}

If approved, you may adjust size/stop/target. If rejected, explain why."""

# ---------------------------------------------------------------------------
# Review prompt: learn from closed trades
# ---------------------------------------------------------------------------

_REVIEW_PROMPT = """Trade closed — extract lesson.

Symbol: {symbol} | Side: {side} | Entry: ${entry:.4f} | Exit: ${exit:.4f}
P&L: {pnl_pct:+.2f}% (${pnl_usd:+.2f}) | Duration: {duration_hours:.1f}h
Signal type: {signal_type} | Exit reason: {exit_reason}
Original reasoning: {reasoning}

What's the ONE key lesson? Be specific and actionable.
Respond JSON: {{"lesson": "...", "adjustment": "..."}}"""

# ---------------------------------------------------------------------------
# API caller with model routing
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _call_claude(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 500,
    system: str = "",
) -> Optional[str]:
    """Call Claude API with optional system prompt."""
    client = _get_client()
    if not client:
        return None
    try:
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        message = client.messages.create(**kwargs)
        return message.content[0].text if message.content else None
    except Exception as e:
        log("warn", f"Claude API error ({model}): {e}")
        return None


# ---------------------------------------------------------------------------
# ClaudeBrain
# ---------------------------------------------------------------------------

class ClaudeBrain:
    """Elite autonomous trading brain powered by Claude.

    Architecture:
        - Haiku scans market every 60s (fast, cheap)  → signal detection
        - Sonnet validates trade entries (smart)       → quality gate
        - Haiku reviews closed trades (cheap)          → learning loop

    The system prompt encodes all live trading experience so Claude
    makes decisions as good as a human trader watching 24/7.
    """

    def __init__(self, balance: float = 10_000):
        self.balance = balance
        self.open_positions: list[dict] = []
        self.daily_pnl: float = 0
        self.pending_signals: list[SignalPacket] = []
        self.calls_today: dict[str, int] = {"haiku": 0, "sonnet": 0}
        self._last_reset = time.time()

        # Market state (updated by runner before each tick)
        self.fgi: int = 50
        self.fgi_class: str = "Neutral"
        self.funding_rates: dict[str, float] = {}
        self.trending_tokens: list[str] = []
        self.recent_listings: list[dict] = []

        self.memory = None  # BrainMemory instance, set by runner
        self.acceleration_data: dict[str, float] = {}  # set by runner from AccelerationTracker

        # Social & news
        self.reddit_sentiment: float = 0.0
        self.reddit_post_count: int = 0
        self.latest_news: list[dict] = []

    def _reset_daily(self):
        now = time.time()
        if now - self._last_reset > 86400:
            self.calls_today = {"haiku": 0, "sonnet": 0}
            self.daily_pnl = 0
            self._last_reset = now

    def add_signal(self, packet: SignalPacket):
        """Queue a signal for the next tick."""
        self.pending_signals.append(packet)
        if len(self.pending_signals) > 20:
            self.pending_signals = self.pending_signals[-20:]

    def _format_positions(self) -> str:
        if not self.open_positions:
            return "(none — all capital available)"
        lines = []
        now_ms = time.time() * 1000
        for p in self.open_positions:
            hold_min = (now_ms - p.get("opened_at", now_ms)) / 60_000
            pnl = p.get("pnl_pct", 0)
            lines.append(
                f"  {p['symbol']:12s} {p['side']:5s} ${p.get('size_usd', 0):6.0f} "
                f"{pnl:+6.1f}% held {hold_min:.0f}min"
            )
        return "\n".join(lines)

    def _format_signals(self) -> str:
        if not self.pending_signals:
            return "(none)"
        lines = []
        for s in self.pending_signals[:10]:
            accel = s.data.get("acceleration_1h", 0)
            accel_str = f" 1h:{accel:+.1f}%" if accel else ""
            lines.append(
                f"  {s.symbol:12s} [{s.signal_type}] "
                f"24h:{s.price_change_24h:+.0f}% vol:${s.volume_24h/1e6:.0f}M"
                f"{accel_str} fr:{s.funding_rate*100:+.3f}%"
            )
        return "\n".join(lines)

    def _format_accel(self) -> str:
        if not self.acceleration_data:
            return "(no significant 1h moves)"
        sorted_accel = sorted(self.acceleration_data.items(), key=lambda x: x[1], reverse=True)
        top = sorted_accel[:8]  # top accelerators
        bottom = sorted_accel[-3:]  # biggest decliners
        parts = []
        if top:
            parts.append("Rising: " + " | ".join(f"{s} {a:+.1f}%" for s, a in top if a > 2))
        if bottom:
            parts.append("Falling: " + " | ".join(f"{s} {a:+.1f}%" for s, a in bottom if a < -5))
        return "\n  ".join(parts) if parts else "(flat market)"

    def _format_funding(self) -> str:
        if not self.funding_rates:
            return "(none)"
        sorted_rates = sorted(
            self.funding_rates.items(), key=lambda x: x[1]
        )
        # Show most negative (squeeze candidates) and most positive
        neg = [f"{s} {r*100:+.3f}%" for s, r in sorted_rates[:5] if r < -0.0005]
        pos = [f"{s} {r*100:+.3f}%" for s, r in sorted_rates[-3:] if r > 0.0005]
        parts = []
        if neg:
            parts.append("Neg (squeeze): " + " | ".join(neg))
        if pos:
            parts.append("Pos: " + " | ".join(pos))
        return "\n  ".join(parts) if parts else "(all near zero)"

    def tick(self) -> list[TradeDecision]:
        """Run one brain tick — Haiku scan → optional Sonnet validation.

        Called every 60s. Returns list of trade actions to execute.
        """
        self._reset_daily()
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt = _TICK_PROMPT.format(
            timestamp=ts,
            balance=self.balance,
            daily_pnl=self.daily_pnl,
            fgi=self.fgi,
            fgi_class=self.fgi_class,
            n_positions=len(self.open_positions),
            positions_text=self._format_positions(),
            signals_text=self._format_signals(),
            accel_text=self._format_accel(),
            funding_text=self._format_funding(),
            trending_text=", ".join(self.trending_tokens[:7]) or "(none)",
            listings_text=" | ".join(
                f"{l.get('symbol', '?')}({l.get('age_hours', 0):.0f}h)"
                for l in self.recent_listings[:3]
            ) or "(none)",
            news_text="; ".join(
                n.get("title", "")[:50] for n in self.latest_news[:3]
            ) or "(none)",
            reddit_sentiment=self.reddit_sentiment,
            reddit_post_count=self.reddit_post_count,
        )

        if self.memory:
            memory_text = self.memory.get_context_for_prompt()
            prompt += f"\n\nMEMORY (cross-session):\n{memory_text}"

        # Step 1: Haiku fast scan — cheap signal detection
        response = _call_claude(
            prompt,
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=_SYSTEM_PROMPT,
        )
        self.calls_today["haiku"] += 1
        signals_snapshot = list(self.pending_signals)
        self.pending_signals = []

        if not response:
            return []

        # Parse Haiku's response
        raw_decisions = self._parse_response(response)
        if not raw_decisions:
            return []

        # Step 2: Sonnet validates BUY decisions (quality gate)
        validated = []
        for d in raw_decisions:
            if d.action == "CLOSE":
                # CLOSE decisions don't need validation
                validated.append(d)
                log("info", f"[ClaudeBrain] CLOSE {d.symbol} — {d.reasoning}")
                continue

            if d.action in ("BUY", "SELL"):
                # Validate with Sonnet for actual entries
                confirmed = self._validate_with_sonnet(d, signals_snapshot)
                if confirmed:
                    validated.append(confirmed)
                else:
                    log("info", f"[ClaudeBrain] Sonnet REJECTED {d.action} {d.symbol} — quality gate")

        return validated

    def _validate_with_sonnet(self, decision: TradeDecision, signals_snapshot: list[SignalPacket] | None = None) -> Optional[TradeDecision]:
        """Use Sonnet to validate and refine a trade decision.

        This is the quality gate — Sonnet is smarter and catches
        mistakes Haiku might make (late pumps, bad sizing, etc.).
        Cost: ~$0.003 per validation.
        """
        trade_json = json.dumps({
            "action": decision.action,
            "symbol": decision.symbol,
            "side": decision.side,
            "size_usd": decision.size_usd,
            "stop_pct": decision.stop_pct,
            "target_pct": decision.target_pct,
            "reasoning": decision.reasoning,
        })

        symbol_funding = self.funding_rates.get(decision.symbol, 0)
        accel_1h = "unknown"
        change_24h = "unknown"
        signals_to_search = signals_snapshot if signals_snapshot is not None else self.pending_signals
        for s in signals_to_search:
            if s.symbol == decision.symbol:
                accel_1h = f"{s.data.get('acceleration_1h', 0):+.1f}%"
                change_24h = f"{s.price_change_24h:+.1f}%"
                break

        prompt = _VALIDATE_PROMPT.format(
            trade_json=trade_json,
            balance=self.balance,
            n_positions=len(self.open_positions),
            fgi=self.fgi,
            symbol=decision.symbol,
            symbol_funding=f"{symbol_funding*100:+.3f}%",
            accel_1h=accel_1h,
            change_24h=change_24h,
        )

        response = _call_claude(
            prompt,
            model="claude-sonnet-4-6",
            max_tokens=200,
        )
        self.calls_today["sonnet"] += 1

        if not response:
            # If Sonnet is unavailable, fall back to Haiku's decision
            log("warn", "[ClaudeBrain] Sonnet unavailable, using Haiku decision as-is")
            return decision

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            if not result.get("approved", False):
                log("info", f"[ClaudeBrain] Sonnet rejected {decision.symbol}: {result.get('reasoning', '')}")
                return None

            # Apply Sonnet's adjustments with confidence-based sizing
            confidence = result.get("confidence", decision.confidence)
            if confidence == "high":
                max_size = min(20.0, self.balance * 0.35)
            elif confidence == "medium":
                max_size = min(15.0, self.balance * 0.25)
            else:
                max_size = min(12.0, self.balance * 0.20)
            decision.size_usd = min(float(result.get("size_usd", decision.size_usd)), max_size)
            decision.stop_pct = float(result.get("stop_pct", decision.stop_pct))
            decision.target_pct = float(result.get("target_pct", decision.target_pct))
            decision.reasoning += f" [Sonnet: {result.get('reasoning', 'approved')}]"
            decision.confidence = "high"  # Sonnet-validated = high confidence

            log("info",
                f"[ClaudeBrain] Sonnet APPROVED {decision.action} {decision.symbol} "
                f"${decision.size_usd:.0f} stop={decision.stop_pct:.0%} "
                f"target={decision.target_pct:.0%}")
            return decision

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log("warn", f"[ClaudeBrain] Failed to parse Sonnet response: {e}")
            return decision  # Fall back to Haiku's decision

    def _parse_response(self, response: str) -> list[TradeDecision]:
        """Parse Claude's JSON response into TradeDecision objects."""
        decisions = []
        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            actions = json.loads(text)
            if not isinstance(actions, list):
                actions = [actions]

            for action in actions:
                act = action.get("action", "").upper()
                if act in ("BUY", "SELL"):
                    decisions.append(TradeDecision(
                        action=act,
                        symbol=action.get("symbol", ""),
                        side=action.get("side", "long"),
                        size_usd=min(float(action.get("size_usd", 12)), 20),
                        entry_price=0,
                        stop_pct=float(action.get("stop_pct", 0.10)),
                        target_pct=float(action.get("target_pct", 0.25)),
                        confidence=action.get("confidence", "medium"),
                        reasoning=action.get("reasoning", ""),
                        signal_id=f"claude-{int(time.time())}",
                        timestamp=time.time() * 1000,
                        thesis_conditions=action.get("thesis_conditions", {}),
                    ))
                elif act == "CLOSE":
                    decisions.append(TradeDecision(
                        action="CLOSE",
                        symbol=action.get("symbol", ""),
                        side="", size_usd=0, entry_price=0,
                        stop_pct=0, target_pct=0,
                        confidence="high",
                        reasoning=action.get("reasoning", ""),
                        signal_id=f"claude-{int(time.time())}",
                        timestamp=time.time() * 1000,
                    ))

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log("warn", f"Failed to parse Claude tick response: {e}")

        return decisions

    def review_trade(self, trade_data: dict) -> Optional[TradeReview]:
        """Post-trade review using Haiku. Called after every close."""
        prompt = _REVIEW_PROMPT.format(
            pnl_usd=trade_data.get("pnl_usd", 0),
            exit_reason=trade_data.get("exit_reason", "unknown"),
            **{k: trade_data.get(k, "") for k in
               ["symbol", "side", "entry", "exit", "pnl_pct",
                "duration_hours", "signal_type", "reasoning"]},
        )
        response = _call_claude(prompt, model="claude-haiku-4-5-20251001", max_tokens=150)
        self.calls_today["haiku"] += 1

        if not response:
            return None

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            data = json.loads(text)
            lesson = data.get("lesson", "")
            if lesson:
                log("info", f"[ClaudeBrain] Trade lesson: {lesson}")
            return TradeReview(
                trade_id=trade_data.get("trade_id", ""),
                symbol=trade_data.get("symbol", ""),
                pnl_pct=trade_data.get("pnl_pct", 0),
                reasoning=f"P&L: {trade_data.get('pnl_pct', 0):+.2f}%",
                lesson=lesson,
                adjustment=data.get("adjustment", "NONE"),
            )
        except Exception:
            return None

    def deep_analysis(self) -> Optional[str]:
        """Hourly deep market analysis using Opus. Costs ~$0.015/call."""
        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build comprehensive context
        positions_text = self._format_positions()
        accel_text = self._format_accel()
        funding_text = self._format_funding()

        memory_text = ""
        if self.memory:
            memory_text = self.memory.get_context_for_prompt()

        prompt = f"""HOURLY DEEP ANALYSIS ({ts})

Portfolio: ${self.balance:,.0f} | Day P&L: ${self.daily_pnl:+,.2f} | FGI: {self.fgi} ({self.fgi_class})

POSITIONS:
{positions_text}

1H ACCELERATION (all symbols):
{accel_text}

FUNDING LANDSCAPE:
{funding_text}

TRENDING: {', '.join(self.trending_tokens[:10]) or '(none)'}

{f'MEMORY:{chr(10)}{memory_text}' if memory_text else ''}

Questions:
1. What is the current market regime? (trending/ranging/volatile/dead)
2. What high-conviction setups should we hunt in the next 1-2 hours?
3. Should we adjust stops/targets on any open positions?
4. What risks do you see that the 60s scanner might miss?

Be specific and actionable. Max 200 words."""

        response = _call_claude(
            prompt,
            model="claude-opus-4-6",
            max_tokens=400,
            system="You are an elite crypto trader doing your hourly market assessment. Be concise and actionable."
        )
        self.calls_today["opus"] = self.calls_today.get("opus", 0) + 1

        if response:
            log("info", f"[ClaudeBrain] Opus analysis: {response[:200]}")
            if self.memory:
                self.memory.add_lesson(response[:500], "opus_hourly")

        return response

    def get_daily_cost_estimate(self) -> float:
        haiku_cost = self.calls_today["haiku"] * 0.00025
        sonnet_cost = self.calls_today["sonnet"] * 0.003
        opus_cost = self.calls_today.get("opus", 0) * 0.015
        return haiku_cost + sonnet_cost + opus_cost
