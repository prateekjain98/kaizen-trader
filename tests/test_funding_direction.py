"""Regression: the negative-funding score bonus is a LONG (short-squeeze) edge
and must never be applied to SHORT signals.

Live bug (2026-06-22): a negative-24h-change top-mover SHORT on a coin with
extreme negative funding inherited the +40 "extreme neg funding" bonus, got
relabeled "funding_squeeze", cleared the trade threshold, and was then vetoed
100% of the time by the basis entry-filter (perp already below spot). Net
effect: the bot took ZERO trades for days despite hourly qualifying signals.
"""

import time

from src.engine.signal_detector import SignalPacket
from src.engine.rule_brain import _score_signal, MIN_SCORE_TO_TRADE


def _score(side: str):
    pkt = SignalPacket(
        signal_id="t", symbol="SYN", signal_type="top_mover",
        priority=3, timestamp=time.time() * 1000, source="test",
        price_usd=1.0, funding_rate=-0.003,  # extreme negative funding (< -0.2%)
        suggested_side=side, volume_24h=200_000_000,
    )
    return _score_signal(
        signal=pkt, funding_rates={"SYN": -0.003}, fgi=50,
        positions=[], recently_closed={}, balance=1000, total_deployed=0,
    )


def test_short_does_not_inherit_negative_funding_bonus():
    scored = _score("short")
    # No +40 bonus, no funding_squeeze relabel -> stays a low-score short.
    assert "funding" not in scored.strategy_type
    assert scored.score < MIN_SCORE_TO_TRADE
    assert scored.side == "short"


def test_long_still_gets_negative_funding_bonus():
    scored = _score("long")
    assert scored.strategy_type == "funding_squeeze"
    assert scored.score >= MIN_SCORE_TO_TRADE
    assert scored.side == "long"
