"""Claude-powered trading brain — continuous real-time loop.

Runs every 60 seconds:
    Haiku scans ALL current market state in ONE call ($0.00025)
    → If action needed, Sonnet executes the decision ($0.003)

Cost: ~$0.50/day = $15/month

The brain sees EVERYTHING every minute:
    - All open positions with live P&L
    - New signals since last tick
    - Market regime (FGI, funding landscape, trending)
    - Price action on watched tokens
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
# Prompt: single efficient scan every 60s
# ---------------------------------------------------------------------------

_TICK_PROMPT = """You are an autonomous crypto trading AI running in real-time. This is your 60-second market scan.

CURRENT TIME: {timestamp}
FEAR & GREED INDEX: {fgi} ({fgi_class})

PORTFOLIO:
Balance: ${balance:,.2f}
Daily P&L: ${daily_pnl:,.2f}
Open positions ({n_positions}):
{positions_text}

NEW SIGNALS (since last tick):
{signals_text}

TOP FUNDING RATES:
{funding_text}

TRENDING TOKENS:
{trending_text}

RECENT LISTINGS:
{listings_text}

YOUR PROVEN STRATEGIES (from backtesting):
1. Correlation break: BTC-alt divergence mean reversion (57% WR, 197% CAGR)
2. Coinbase listing pump: buy within 24h of listing (77% WR, +474%)
3. Binance Futures listing pump: buy at listing (28% WR but +417% cumulative)
4. Cross-exchange divergence: spot vs futures spread (73.5% WR)
5. FGI contrarian: long at FGI<20, short at FGI>80 (61% WR on BTC/ETH)
6. Funding squeeze: extreme negative funding = long, extreme positive = short

RULES:
- Max $500 per position, max 10 positions
- New Coinbase listing = ALWAYS BUY (77% WR proven)
- New Binance Futures listing = BUY if token is trending or has social buzz
- FGI < 15 = BUY BTC and ETH (proven contrarian edge)
- Funding rate > 0.1% = SHORT opportunity, < -0.1% = LONG opportunity
- NEVER chase a pump that already happened (>100% move = too late)
- NEVER trade Binance SPOT listings (proven unprofitable)
- Close positions that hit stop or target, or if thesis is invalidated

Respond with a JSON array of actions (can be empty if no action needed):
[
  {{"action": "BUY", "symbol": "XXX", "side": "long", "size_usd": 200, "stop_pct": 0.08, "target_pct": 0.15, "confidence": "high", "reasoning": "..."}},
  {{"action": "CLOSE", "symbol": "YYY", "reasoning": "thesis invalidated"}},
  ...
]

If nothing to do, respond: []"""

_REVIEW_PROMPT = """Trade closed. Quick analysis.

Symbol: {symbol} | Side: {side} | Entry: ${entry:.4f} | Exit: ${exit:.4f}
P&L: {pnl_pct:+.2f}% | Duration: {duration_hours:.1f}h | Signal: {signal_type}
Reasoning: {reasoning}

Respond JSON: {{"lesson": "...", "adjustment": "..."}}"""


def _call_claude(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 500) -> Optional[str]:
    """Call Claude API."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

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
    """Continuous Claude-powered trading brain.

    Runs a full market scan every tick (60s) using Haiku ($0.00025/call).
    Escalates to Sonnet only for trade execution decisions ($0.003/call).
    """

    def __init__(self, balance: float = 10_000):
        self.balance = balance
        self.open_positions: list[dict] = []
        self.daily_pnl: float = 0
        self.pending_signals: list[SignalPacket] = []
        self.calls_today: dict[str, int] = {"haiku": 0, "sonnet": 0}
        self._last_reset = time.time()
        self._lessons: list[str] = []  # accumulated lessons from trade reviews

        # Market state (updated by runner)
        self.fgi: int = 50
        self.fgi_class: str = "Neutral"
        self.funding_rates: dict[str, float] = {}
        self.trending_tokens: list[str] = []
        self.recent_listings: list[dict] = []

    def _reset_daily(self):
        now = time.time()
        if now - self._last_reset > 86400:
            self.calls_today = {"haiku": 0, "sonnet": 0}
            self.daily_pnl = 0
            self._last_reset = now

    def add_signal(self, packet: SignalPacket):
        """Queue a signal for the next tick."""
        self.pending_signals.append(packet)
        # Keep last 20
        if len(self.pending_signals) > 20:
            self.pending_signals = self.pending_signals[-20:]

    def tick(self) -> list[TradeDecision]:
        """Run one brain tick — full market scan → list of trade decisions.

        Called every 60 seconds by the runner. Returns list of actions to execute.
        Cost: ~$0.00025 per tick (Haiku).
        """
        self._reset_daily()

        # Build the prompt with ALL current state
        positions_text = "\n".join(
            f"  {p['symbol']} {p['side']} ${p.get('size_usd', 0):.0f} entry=${p.get('entry', 0):.4f} "
            f"current=${p.get('current_price', 0):.4f} P&L={p.get('pnl_pct', 0):+.1f}%"
            for p in self.open_positions
        ) if self.open_positions else "  (none)"

        signals_text = "\n".join(
            f"  [{p.signal_type}] {p.symbol} — {p.reasoning}"
            for p in self.pending_signals
        ) if self.pending_signals else "  (none)"

        funding_text = "\n".join(
            f"  {sym:12s} {rate*100:+.4f}% {'← SHORT squeeze' if rate < -0.001 else '← LONG squeeze' if rate > 0.001 else ''}"
            for sym, rate in sorted(self.funding_rates.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
        ) if self.funding_rates else "  (none)"

        trending_text = ", ".join(self.trending_tokens[:7]) if self.trending_tokens else "(none)"

        listings_text = "\n".join(
            f"  {l.get('symbol', '?')} on {l.get('exchange', '?')} ({l.get('age_hours', 0):.0f}h ago)"
            for l in self.recent_listings[:5]
        ) if self.recent_listings else "  (none)"

        import datetime
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt = _TICK_PROMPT.format(
            timestamp=ts,
            fgi=self.fgi, fgi_class=self.fgi_class,
            balance=self.balance, daily_pnl=self.daily_pnl,
            n_positions=len(self.open_positions),
            positions_text=positions_text,
            signals_text=signals_text,
            funding_text=funding_text,
            trending_text=trending_text,
            listings_text=listings_text,
        )

        # Call Haiku for the scan
        response = _call_claude(prompt, model="claude-haiku-4-5-20251001", max_tokens=800)
        self.calls_today["haiku"] += 1

        # Clear processed signals
        self.pending_signals = []

        if not response:
            return []

        # Parse response
        decisions = []
        try:
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            actions = json.loads(text)
            if not isinstance(actions, list):
                actions = [actions]

            for action in actions:
                if action.get("action") in ("BUY", "SELL"):
                    decisions.append(TradeDecision(
                        action=action["action"],
                        symbol=action.get("symbol", ""),
                        side=action.get("side", "long"),
                        size_usd=min(float(action.get("size_usd", 100)), 500),
                        entry_price=0,  # filled by executor from live price
                        stop_pct=float(action.get("stop_pct", 0.08)),
                        target_pct=float(action.get("target_pct", 0.15)),
                        confidence=action.get("confidence", "medium"),
                        reasoning=action.get("reasoning", ""),
                        signal_id=f"tick-{int(time.time())}",
                        timestamp=time.time() * 1000,
                    ))
                elif action.get("action") == "CLOSE":
                    decisions.append(TradeDecision(
                        action="CLOSE",
                        symbol=action.get("symbol", ""),
                        side="", size_usd=0, entry_price=0,
                        stop_pct=0, target_pct=0,
                        confidence="high",
                        reasoning=action.get("reasoning", ""),
                        signal_id=f"tick-{int(time.time())}",
                        timestamp=time.time() * 1000,
                    ))

            if decisions:
                for d in decisions:
                    log("info", f"Claude decision: {d.action} {d.symbol} {d.side} ${d.size_usd:.0f} [{d.confidence}] — {d.reasoning}")

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log("warn", f"Failed to parse Claude tick response: {e}")

        return decisions

    def review_trade(self, trade_data: dict) -> Optional[TradeReview]:
        """Post-trade review using Haiku (cheap). Called after every close."""
        prompt = _REVIEW_PROMPT.format(**trade_data)
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
                self._lessons.append(lesson)
                # Keep last 50 lessons
                self._lessons = self._lessons[-50:]
                log("info", f"Trade lesson: {lesson}")
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

    def get_daily_cost_estimate(self) -> float:
        haiku_cost = self.calls_today["haiku"] * 0.00025
        sonnet_cost = self.calls_today["sonnet"] * 0.003
        return haiku_cost + sonnet_cost
