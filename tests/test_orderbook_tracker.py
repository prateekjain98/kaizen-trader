"""Tests for filtered order-book imbalance (OBI-F)."""

import time

from src.engine.orderbook_tracker import OrderBookTracker
from src.engine.signal_detector import SignalDetector
from src.engine.data_streams import TokenSignal, MarketSnapshot
from src.engine.rule_brain import _score_signal, STRATEGY_RISK


def _ingest(tracker: OrderBookTracker, sym: str, bids, asks, ts: float):
    tracker.ingest(sym, bids, asks, ts=ts)


def test_strategy_risk_has_orderbook_imbalance():
    risk = STRATEGY_RISK["orderbook_imbalance"]
    assert risk["stop_pct"] == 0.02
    assert risk["target_pct"] == 0.03


def test_obi_filters_spoof_levels():
    """A bid level that appears for one snap and disappears must not
    contribute to the filtered OBI — that's the whole point of the filter.
    """
    tr = OrderBookTracker()
    persistent_bids = [(100.0, 5.0), (99.0, 5.0), (98.0, 5.0), (97.0, 5.0), (96.0, 5.0)]
    persistent_asks = [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0), (104.0, 1.0), (105.0, 1.0)]

    base = 1_000_000.0
    # 3 snaps with persistent book — should yield strong positive OBI
    for i in range(3):
        _ingest(tr, "BTC", persistent_bids, persistent_asks, ts=base + i * 1.5)
    obi = tr.obi_f("BTC")
    assert obi is not None
    assert obi > 0.5  # bids dominate

    # Now insert a spoofed ask wall in only ONE snap then yank it.
    spoof_asks = persistent_asks + [(106.0, 1000.0)]
    _ingest(tr, "BTC", persistent_bids, spoof_asks, ts=base + 5 * 1.5)
    # Two more snaps without the spoof level — the spoof must be filtered out
    _ingest(tr, "BTC", persistent_bids, persistent_asks, ts=base + 6 * 1.5)
    _ingest(tr, "BTC", persistent_bids, persistent_asks, ts=base + 7 * 1.5)
    obi_after_spoof = tr.obi_f("BTC")
    assert obi_after_spoof is not None
    # Should still strongly favor bids — the 1000-qty spoof was discarded.
    assert obi_after_spoof > 0.5


def test_obi_normalized_range():
    tr = OrderBookTracker()
    bids = [(100.0, 1.0)] * 5
    bids = [(100.0 - i, 1.0) for i in range(5)]
    asks = [(101.0 + i, 1.0) for i in range(5)]
    base = 2_000_000.0
    for i in range(4):
        _ingest(tr, "ETH", bids, asks, ts=base + i * 1.5)
    obi = tr.obi_f("ETH")
    assert obi is not None
    assert -1.0 <= obi <= 1.0


def test_signal_detector_emits_obi_packet_with_opposing_trend():
    det = SignalDetector()
    snap = MarketSnapshot()
    snap.prices["SOL"] = 100.0
    sig = TokenSignal(
        source="binance_obi_ws", symbol="SOL",
        event_type="orderbook_imbalance",
        data={"obi_f_ema": 0.55, "acceleration_1h": -3.0, "price": 100.0},
        timestamp=time.time() * 1000, priority=1,
    )
    packet = det.process(sig, snap)
    assert packet is not None
    assert packet.signal_type == "orderbook_imbalance"
    assert packet.suggested_side == "long"  # bids dominate, price down → fade-up
    assert packet.suggested_stop_pct == 0.02
    assert packet.suggested_target_pct == 0.03


def test_signal_detector_filters_obi_when_trend_aligned():
    """Trend aligned with imbalance is NOT a mean-revert setup — drop it."""
    det = SignalDetector()
    snap = MarketSnapshot()
    sig = TokenSignal(
        source="binance_obi_ws", symbol="SOL",
        event_type="orderbook_imbalance",
        # bids dominant AND price already pumping → not a snap setup
        data={"obi_f_ema": 0.55, "acceleration_1h": +5.0, "price": 100.0},
        timestamp=time.time() * 1000, priority=1,
    )
    assert det.process(sig, snap) is None


def test_signal_detector_filters_subthreshold_obi():
    det = SignalDetector()
    snap = MarketSnapshot()
    sig = TokenSignal(
        source="binance_obi_ws", symbol="SOL",
        event_type="orderbook_imbalance",
        data={"obi_f_ema": 0.30, "acceleration_1h": -3.0, "price": 100.0},
        timestamp=time.time() * 1000, priority=1,
    )
    assert det.process(sig, snap) is None


def test_rule_brain_scores_obi_with_opposite_trend_bonus():
    det = SignalDetector()
    snap = MarketSnapshot()
    snap.prices["SOL"] = 100.0
    sig = TokenSignal(
        source="binance_obi_ws", symbol="SOL",
        event_type="orderbook_imbalance",
        data={"obi_f_ema": 0.55, "acceleration_1h": -3.0, "price": 100.0},
        timestamp=time.time() * 1000, priority=1,
    )
    packet = det.process(sig, snap)
    assert packet is not None
    scored = _score_signal(
        signal=packet, funding_rates={}, fgi=50, positions=[],
        recently_closed={}, balance=1000, total_deployed=0,
    )
    assert scored is not None
    assert scored.strategy_type == "orderbook_imbalance"
    assert scored.stop_pct == 0.02
    assert scored.target_pct == 0.03
    # +35 base + +15 opposite-trend confirmation = 50 (no other factors fire here)
    assert scored.score >= 50
