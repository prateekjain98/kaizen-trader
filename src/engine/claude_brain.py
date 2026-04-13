"""Claude-powered trading brain.

Three-tier LLM usage for cost efficiency:
    1. Haiku pre-filter  — $0.00025/call — "Is this signal worth analyzing?"
    2. Sonnet analysis   — $0.003/call  — "Full analysis: trade or skip?"
    3. Sonnet review     — $0.003/call  — "Why did this trade win/lose?"

Expected usage: ~$0.50-2.00/day at 50-200 signals/day.
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
    action: str             # "BUY", "SELL", "SKIP", "WATCH"
    symbol: str
    side: str               # "long" or "short"
    size_usd: float         # how much to trade
    entry_price: float
    stop_pct: float         # stop loss percentage
    target_pct: float       # take profit percentage
    confidence: str         # "high", "medium", "low"
    reasoning: str          # Claude's explanation
    signal_id: str
    timestamp: float


@dataclass
class TradeReview:
    """Claude's post-trade analysis."""
    trade_id: str
    symbol: str
    pnl_pct: float
    reasoning: str
    lesson: str             # what to learn from this trade
    adjustment: str         # suggested parameter adjustment


# Prompt templates — optimized for minimal tokens
_HAIKU_PREFILTER = """You are a crypto trading signal filter. Respond with ONLY "YES" or "NO".

Should this signal be analyzed for a potential trade?

{context}

Rules:
- YES if the signal has clear edge (listing, extreme funding, trending + volume)
- NO if it's noise (weak signal, already priced in, low liquidity)
- YES for any new exchange listing (proven 77% WR)
- YES for funding rate > 0.1% (short squeeze/long squeeze)
- YES for FGI < 20 or > 80 (contrarian proven profitable)

Answer YES or NO:"""

_SONNET_ANALYSIS = """You are an expert crypto trader making real-money decisions. Be decisive.

SIGNAL:
{context}

PORTFOLIO STATE:
Balance: ${balance:,.2f}
Open positions: {open_positions}
Today's P&L: ${daily_pnl:,.2f}

RULES:
1. Max position size: $500 (risk management)
2. Max 10 concurrent positions
3. Listing pumps: enter immediately, 8% stop, 30% target, 24h hold
4. Funding squeeze: enter if rate > 0.1%, 6% stop, 8% target
5. FGI contrarian: only BTC/ETH, 12% stop, 20% target
6. Trending: only if volume confirms, 8% stop, 15% target
7. NEVER trade Binance Spot listings (proven unprofitable)
8. Coinbase listings are 77% WR — high confidence

Respond in this EXACT JSON format (no markdown, no explanation outside JSON):
{{
    "action": "BUY" or "SKIP",
    "side": "long" or "short",
    "size_usd": <number>,
    "stop_pct": <number like 0.08>,
    "target_pct": <number like 0.15>,
    "confidence": "high" or "medium" or "low",
    "reasoning": "<one sentence>"
}}"""

_SONNET_REVIEW = """You are reviewing a closed trade. Be analytical and concise.

TRADE:
Symbol: {symbol}
Side: {side}
Entry: ${entry:.4f}
Exit: ${exit:.4f}
P&L: {pnl_pct:+.2f}%
Duration: {duration_hours:.1f}h
Signal type: {signal_type}
Original reasoning: {reasoning}

What happened? Respond in JSON:
{{
    "lesson": "<what to learn>",
    "adjustment": "<specific parameter change or NONE>"
}}"""


def _call_claude(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 200) -> Optional[str]:
    """Call Claude API. Returns response text or None on error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None  # Claude brain disabled — no API key

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text if message.content else None
    except Exception as e:
        log("warn", f"Claude API error: {e}")
        return None


class ClaudeBrain:
    """Claude-powered trading decision engine.

    Uses tiered model selection for cost efficiency:
    - Haiku for quick yes/no filtering (~$0.00025/call)
    - Sonnet for full analysis (~$0.003/call)
    """

    def __init__(self, balance: float = 10_000):
        self.balance = balance
        self.open_positions: list[dict] = []
        self.daily_pnl: float = 0
        self.calls_today: dict[str, int] = {"haiku": 0, "sonnet": 0}
        self._last_reset = time.time()

    def _reset_daily_counters(self):
        now = time.time()
        if now - self._last_reset > 86400:
            self.calls_today = {"haiku": 0, "sonnet": 0}
            self.daily_pnl = 0
            self._last_reset = now

    def pre_filter(self, packet: SignalPacket) -> bool:
        """Quick Haiku filter: is this signal worth a full analysis?

        Cost: ~$0.00025 per call.
        """
        self._reset_daily_counters()

        # Priority 3 (urgent) signals skip the filter — always analyze
        if packet.priority >= 3:
            return True

        prompt = _HAIKU_PREFILTER.format(context=packet.to_prompt_context())
        response = _call_claude(prompt, model="claude-haiku-4-5-20251001", max_tokens=10)
        self.calls_today["haiku"] += 1

        if response is None:
            # If API fails, use rule-based fallback
            return packet.priority >= 2

        return response.strip().upper().startswith("YES")

    def analyze(self, packet: SignalPacket) -> Optional[TradeDecision]:
        """Full Sonnet analysis: should we trade this signal?

        Cost: ~$0.003 per call.
        """
        self._reset_daily_counters()

        positions_str = ", ".join(
            f"{p['symbol']} {p['side']} ${p.get('size_usd', 0):.0f}"
            for p in self.open_positions
        ) if self.open_positions else "None"

        prompt = _SONNET_ANALYSIS.format(
            context=packet.to_prompt_context(),
            balance=self.balance,
            open_positions=positions_str,
            daily_pnl=self.daily_pnl,
        )

        response = _call_claude(prompt, model="claude-sonnet-4-6", max_tokens=300)
        self.calls_today["sonnet"] += 1

        if not response:
            return None

        try:
            # Parse JSON from response
            # Handle potential markdown wrapping
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)

            if data.get("action") == "SKIP":
                log("info", f"Claude SKIP: {packet.symbol} — {data.get('reasoning', '')}")
                return None

            return TradeDecision(
                action=data.get("action", "BUY"),
                symbol=packet.symbol,
                side=data.get("side", packet.suggested_side or "long"),
                size_usd=min(float(data.get("size_usd", 100)), 500),  # cap at $500
                entry_price=packet.price_usd,
                stop_pct=float(data.get("stop_pct", packet.suggested_stop_pct or 0.08)),
                target_pct=float(data.get("target_pct", packet.suggested_target_pct or 0.15)),
                confidence=data.get("confidence", "medium"),
                reasoning=data.get("reasoning", ""),
                signal_id=packet.signal_id,
                timestamp=time.time() * 1000,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log("warn", f"Failed to parse Claude response for {packet.symbol}: {e}")
            return None

    def review_trade(self, trade_data: dict) -> Optional[TradeReview]:
        """Post-trade Sonnet review: what did we learn?

        Cost: ~$0.003 per call. Called after every trade close.
        """
        prompt = _SONNET_REVIEW.format(**trade_data)
        response = _call_claude(prompt, model="claude-sonnet-4-6", max_tokens=200)
        self.calls_today["sonnet"] += 1

        if not response:
            return None

        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(text)
            return TradeReview(
                trade_id=trade_data.get("trade_id", ""),
                symbol=trade_data.get("symbol", ""),
                pnl_pct=trade_data.get("pnl_pct", 0),
                reasoning=f"P&L: {trade_data.get('pnl_pct', 0):+.2f}%",
                lesson=data.get("lesson", ""),
                adjustment=data.get("adjustment", "NONE"),
            )
        except Exception:
            return None

    def get_daily_cost_estimate(self) -> float:
        """Estimated API cost for today's usage."""
        haiku_cost = self.calls_today["haiku"] * 0.00025
        sonnet_cost = self.calls_today["sonnet"] * 0.003
        return haiku_cost + sonnet_cost
