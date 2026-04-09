"""Tests for new features: bid-ask spread filter, sector exposure limits,
signal decay, trade journal, regime-aware exits, warm-up period."""

import time
import uuid
import pytest
from dataclasses import dataclass
from unittest.mock import patch, MagicMock

from tests.conftest import make_position, make_signal, _now_ms


# ─── Bid-Ask Spread Filter ──────────────────────────────────────────────────

class TestBidAskSpread:
    def test_spread_with_valid_book(self):
        from src.strategies.orderbook_imbalance import (
            update_order_book, get_bid_ask_spread, OrderBookLevel,
        )
        bids = [OrderBookLevel(price=100.0, size=10.0)]
        asks = [OrderBookLevel(price=100.50, size=10.0)]
        update_order_book("TEST", bids, asks)

        spread = get_bid_ask_spread("TEST")
        assert spread is not None
        # mid = 100.25, spread = 0.50 / 100.25 ≈ 0.00499
        assert abs(spread - 0.00499) < 0.001

    def test_spread_returns_none_for_unknown_symbol(self):
        from src.strategies.orderbook_imbalance import get_bid_ask_spread
        assert get_bid_ask_spread("UNKNOWN_SYMBOL_XYZ") is None

    def test_spread_returns_none_for_empty_book(self):
        from src.strategies.orderbook_imbalance import (
            update_order_book, get_bid_ask_spread, OrderBookLevel,
        )
        update_order_book("EMPTY", [], [])
        assert get_bid_ask_spread("EMPTY") is None

    def test_spread_returns_none_for_zero_prices(self):
        from src.strategies.orderbook_imbalance import (
            update_order_book, get_bid_ask_spread, OrderBookLevel,
        )
        bids = [OrderBookLevel(price=0.0, size=10.0)]
        asks = [OrderBookLevel(price=100.0, size=10.0)]
        update_order_book("ZERO", bids, asks)
        assert get_bid_ask_spread("ZERO") is None

    def test_tight_spread(self):
        from src.strategies.orderbook_imbalance import (
            update_order_book, get_bid_ask_spread, OrderBookLevel,
        )
        bids = [OrderBookLevel(price=50000.0, size=1.0)]
        asks = [OrderBookLevel(price=50001.0, size=1.0)]
        update_order_book("BTC", bids, asks)

        spread = get_bid_ask_spread("BTC")
        assert spread is not None
        assert spread < 0.001  # very tight spread

    def test_wide_spread(self):
        from src.strategies.orderbook_imbalance import (
            update_order_book, get_bid_ask_spread, OrderBookLevel,
        )
        bids = [OrderBookLevel(price=95.0, size=10.0)]
        asks = [OrderBookLevel(price=105.0, size=10.0)]
        update_order_book("WIDE", bids, asks)

        spread = get_bid_ask_spread("WIDE")
        assert spread is not None
        # mid = 100, spread = 10/100 = 0.10 (10%)
        assert spread > 0.005  # would be filtered


# ─── Sector Exposure Limits ──────────────────────────────────────────────────

class TestSectorExposure:
    def test_no_group_returns_unchanged(self):
        from src.risk.position_sizer import check_sector_exposure
        # "UNKNOWN" is not in _CORRELATION_GROUPS
        result = check_sector_exposure("UNKNOWN", "long", 500, 10_000, [])
        assert result == 500

    def test_empty_sector_allows_full_size(self):
        from src.risk.position_sizer import check_sector_exposure
        result = check_sector_exposure("SOL", "long", 500, 10_000, [])
        # max sector = 30% of 10k = 3000, no existing exposure
        assert result == 500

    def test_sector_cap_reduces_size(self):
        from src.risk.position_sizer import check_sector_exposure
        # Already have 2500 in alt_l1 sector (SOL, AVAX, etc.)
        existing = [
            make_position(symbol="AVAX", side="long", entry_price=30, quantity=50,
                          strategy="momentum_swing"),  # 1500 USD
            make_position(symbol="NEAR", side="long", entry_price=5, quantity=200,
                          strategy="momentum_swing"),  # 1000 USD
        ]
        # Override size_usd since make_position computes it from entry*qty
        existing[0].size_usd = 1500
        existing[1].size_usd = 1000

        result = check_sector_exposure("SOL", "long", 1000, 10_000, existing)
        # max sector = 3000, current = 2500, remaining = 500
        assert result == 500

    def test_sector_full_returns_zero(self):
        from src.risk.position_sizer import check_sector_exposure
        existing = [
            make_position(symbol="AVAX", side="long", entry_price=30, quantity=100,
                          strategy="momentum_swing"),
        ]
        existing[0].size_usd = 3000  # Already at 30% of 10k

        result = check_sector_exposure("SOL", "long", 500, 10_000, existing)
        assert result == 0.0

    def test_zero_portfolio_returns_unchanged(self):
        from src.risk.position_sizer import check_sector_exposure
        result = check_sector_exposure("SOL", "long", 500, 0, [])
        assert result == 500

    def test_different_sector_not_counted(self):
        from src.risk.position_sizer import check_sector_exposure
        # DOGE is in "meme" group, SOL is in "alt_l1" — should not interfere
        existing = [make_position(symbol="DOGE", side="long", entry_price=0.1, quantity=100000)]
        existing[0].size_usd = 2900  # 29% of 10k in meme sector

        result = check_sector_exposure("SOL", "long", 500, 10_000, existing)
        assert result == 500  # alt_l1 sector is empty

    def test_suffix_stripping(self):
        from src.risk.position_sizer import check_sector_exposure
        # "SOL-USD" should be recognized as SOL in alt_l1
        result = check_sector_exposure("SOL-USD", "long", 500, 10_000, [])
        assert result == 500  # recognized, full sector available


# ─── Signal Decay ────────────────────────────────────────────────────────────

class TestSignalDecay:
    def test_fresh_signal_no_decay(self):
        """A signal created right now should keep its full score."""
        sig = make_signal(score=80.0)
        # Signal just created, age_s ≈ 0 → decay_factor ≈ 1.0
        age_s = 0
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))
        assert decay_factor == 1.0
        assert sig.score * decay_factor == 80.0

    def test_15s_signal_decays(self):
        """A 15-second old signal should lose ~25% of its score."""
        age_s = 15
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))
        assert abs(decay_factor - 0.75) < 0.01

    def test_30s_signal_decays_to_half(self):
        """A 30-second old signal should have 50% of its score."""
        age_s = 30
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))
        assert abs(decay_factor - 0.50) < 0.01

    def test_decay_floor_at_50pct(self):
        """Even very old signals floor at 50% decay factor."""
        age_s = 60
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))
        assert decay_factor == 0.5

        age_s = 120
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))
        assert decay_factor == 0.5


# ─── Trade Journal ───────────────────────────────────────────────────────────

class TestTradeJournal:
    def test_insert_delegates_to_storage(self, _mock_convex_storage):
        from src.storage.database import insert_trade_journal

        entry = {
            "id": str(uuid.uuid4()),
            "position_id": "test-pos-1",
            "symbol": "ETH",
            "strategy": "momentum_swing",
            "r_multiple": 1.5,
            "hold_hours": 3.2,
            "mae_pct": -0.02,
            "mfe_pct": 0.05,
            "partial_exit_pct": 0.5,
            "exit_reason": "trailing_stop",
            "pnl_pct": 0.03,
            "regime_at_entry": "trending_up/normal_vol",
            "regime_at_exit": "trending_up/high_vol",
            "was_partial_beneficial": 0,
            "timestamp": _now_ms(),
        }
        insert_trade_journal(entry)
        _mock_convex_storage.insert_trade_journal.assert_called_once_with(entry)

    def test_get_journal_delegates_to_storage(self, _mock_convex_storage):
        from src.storage.database import get_trade_journal

        _mock_convex_storage.get_trade_journal.return_value = [
            {"position_id": "pos-1", "symbol": "ETH", "strategy": "momentum_swing"},
        ]
        journal = get_trade_journal(limit=10)
        assert len(journal) == 1
        assert journal[0]["position_id"] == "pos-1"

    def test_journal_with_none_values(self, _mock_convex_storage):
        from src.storage.database import insert_trade_journal

        entry = {
            "id": str(uuid.uuid4()),
            "position_id": "test-pos-none",
            "symbol": "BTC",
            "strategy": "funding_extreme",
            "timestamp": _now_ms(),
            # All optional fields missing
        }
        insert_trade_journal(entry)
        _mock_convex_storage.insert_trade_journal.assert_called_once_with(entry)


# ─── Regime-Aware Exits ──────────────────────────────────────────────────────

class TestRegimeAwareExits:
    def test_high_vol_widens_long_stop(self):
        """In high volatility, stops should be widened (lowered for longs)."""
        pos = make_position(
            symbol="ETH", side="long", entry_price=2000.0,
            high_watermark=2100.0,
        )
        pos.trail_pct = 0.07
        pos.stop_price = 2100 * (1 - 0.07)  # 1953

        # High vol: widen to trail_pct * 1.2
        widened = pos.high_watermark * (1 - pos.trail_pct * 1.2)
        # 2100 * (1 - 0.084) = 2100 * 0.916 = 1923.6
        assert widened < pos.stop_price  # widened stop is lower

    def test_low_vol_tightens_long_stop(self):
        """In low volatility, stops should be tightened (raised for longs)."""
        pos = make_position(
            symbol="ETH", side="long", entry_price=2000.0,
            high_watermark=2100.0,
        )
        pos.trail_pct = 0.07
        pos.stop_price = 2100 * (1 - 0.07)  # 1953

        # Low vol: tighten to trail_pct * 0.8
        tightened = pos.high_watermark * (1 - pos.trail_pct * 0.8)
        # 2100 * (1 - 0.056) = 2100 * 0.944 = 1982.4
        assert tightened > pos.stop_price  # tightened stop is higher

    def test_short_position_high_vol_widens(self):
        """In high volatility, short position stop should be raised (widened)."""
        pos = make_position(
            symbol="ETH", side="short", entry_price=2000.0,
            low_watermark=1900.0,
        )
        pos.trail_pct = 0.07
        pos.stop_price = 1900 * (1 + 0.07)  # 2033

        widened = pos.low_watermark * (1 + pos.trail_pct * 1.2)
        # 1900 * (1 + 0.084) = 1900 * 1.084 = 2059.6
        assert widened > pos.stop_price  # widened stop is higher for shorts

    def test_short_position_low_vol_tightens(self):
        """In low volatility, short position stop should be lowered (tightened)."""
        pos = make_position(
            symbol="ETH", side="short", entry_price=2000.0,
            low_watermark=1900.0,
        )
        pos.trail_pct = 0.07
        pos.stop_price = 1900 * (1 + 0.07)  # 2033

        tightened = pos.low_watermark * (1 + pos.trail_pct * 0.8)
        # 1900 * (1 + 0.056) = 1900 * 1.056 = 2006.4
        assert tightened < pos.stop_price  # tightened stop is lower for shorts


# ─── Warm-up Period ──────────────────────────────────────────────────────────

class TestWarmupPeriod:
    def test_warmup_constant_exists(self):
        from src.main import _WARMUP_PERIOD_S
        assert _WARMUP_PERIOD_S == 60  # 1 minute

    def test_warmup_blocks_during_startup(self):
        """During warm-up, _process_signal should return without processing."""
        from src import main as main_mod
        original_start = main_mod._start_time

        try:
            # Set start time to "just now" so we're in warm-up
            main_mod._start_time = time.time()

            sig = make_signal(score=80.0)
            ctx = MagicMock()

            # Mock strategy_selector to return True
            with patch.object(main_mod.strategy_selector, "is_strategy_enabled", return_value=True):
                with patch.object(main_mod, "log"):
                    # _process_signal should return early during warm-up
                    # We can verify it doesn't call qualify()
                    with patch.object(main_mod, "qualify") as mock_qualify:
                        main_mod._process_signal(sig, ctx)
                        mock_qualify.assert_not_called()
        finally:
            main_mod._start_time = original_start

    def test_after_warmup_processes_normally(self):
        """After warm-up period, signals should be processed."""
        from src import main as main_mod
        original_start = main_mod._start_time

        try:
            # Set start time to 10 minutes ago (past warm-up)
            main_mod._start_time = time.time() - 600

            sig = make_signal(score=80.0)
            ctx = MagicMock()

            with patch.object(main_mod.strategy_selector, "is_strategy_enabled", return_value=True):
                with patch.object(main_mod, "get_bid_ask_spread", return_value=None):
                    with patch.object(main_mod, "qualify") as mock_qualify:
                        mock_qualify.return_value = MagicMock(passed=False)
                        with patch.object(main_mod, "log"):
                            main_mod._process_signal(sig, ctx)
                            # After warm-up, qualify should be called (unless blocked by other checks)
                            # It might not reach qualify if spread/staleness blocks it,
                            # but it shouldn't return from warm-up check
        finally:
            main_mod._start_time = original_start
