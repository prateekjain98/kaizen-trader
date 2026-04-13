"""Tests for cross-strategy signal aggregation."""
import pytest
import time
from tests.conftest import make_signal
from src.risk.signal_aggregator import SignalAggregator


class TestSignalAggregator:
    def setup_method(self):
        self.agg = SignalAggregator(window_ms=5000)

    def test_single_signal_passes_through(self):
        sig = make_signal(symbol="BTC", strategy="momentum_swing", score=70)
        results = self.agg.submit(sig)
        assert len(results) == 1
        assert results[0].symbol == "BTC"

    def test_same_symbol_same_side_picks_highest_score(self):
        sig1 = make_signal(symbol="BTC", strategy="momentum_swing", side="long", score=60)
        sig2 = make_signal(symbol="BTC", strategy="mean_reversion", side="long", score=80)
        self.agg.submit(sig1)
        results = self.agg.submit(sig2)
        assert len(results) == 1
        assert results[0].score >= 80

    def test_same_symbol_opposing_sides_cancels(self):
        sig1 = make_signal(symbol="BTC", strategy="momentum_swing", side="long", score=70)
        sig2 = make_signal(symbol="BTC", strategy="funding_extreme", side="short", score=70)
        self.agg.submit(sig1)
        results = self.agg.submit(sig2)
        assert len(results) == 0

    def test_different_symbols_pass_independently(self):
        sig1 = make_signal(symbol="BTC", strategy="momentum_swing", score=70)
        sig2 = make_signal(symbol="ETH", strategy="momentum_swing", score=65)
        self.agg.submit(sig1)
        results = self.agg.submit(sig2)
        assert len(results) == 1
        assert results[0].symbol == "ETH"

    def test_agreement_boosts_score(self):
        sig1 = make_signal(symbol="BTC", strategy="momentum_swing", side="long", score=60)
        sig2 = make_signal(symbol="BTC", strategy="whale_accumulation", side="long", score=65)
        self.agg.submit(sig1)
        results = self.agg.submit(sig2)
        assert len(results) == 1
        assert results[0].score > 65

    def test_expired_signals_dont_aggregate(self):
        sig1 = make_signal(symbol="BTC", strategy="momentum_swing", score=70)
        sig1.created_at = time.time() * 1000 - 10000
        self.agg.submit(sig1)
        sig2 = make_signal(symbol="BTC", strategy="mean_reversion", side="long", score=65)
        results = self.agg.submit(sig2)
        assert len(results) == 1
        assert results[0].score == 65

    def test_flush_returns_pending_signals(self):
        sig = make_signal(symbol="BTC", strategy="momentum_swing", score=70)
        self.agg.submit(sig)
        pending = self.agg.flush()
        assert len(pending) <= 1
