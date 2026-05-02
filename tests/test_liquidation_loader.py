"""Tests for liquidation_loader — pure-function logic, no network."""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from src.backtesting import liquidation_loader as ll


# ── Bitfinex row parsing ────────────────────────────────────────────────

def _bfx_row(ts: int, sym: str, amount: float, price: float, is_match: int = 1) -> list:
    # [TYPE, POS_ID, MTS, _, SYMBOL, AMOUNT, BASE_PRICE, _, IS_MATCH,
    #  IS_MARKET_SOLD, _, PRICE_ACQUIRED]
    return ["pos", 1, ts, None, sym, amount, price, None, is_match, 1, None, price]


def test_normalise_symbol_perp():
    assert ll._normalise_symbol("tBTCF0:USTF0") == "BTC"
    assert ll._normalise_symbol("tETHF0:USTF0") == "ETH"


def test_normalise_symbol_spot():
    assert ll._normalise_symbol("tBTCUSD") == "BTC"
    assert ll._normalise_symbol("tETHUST") == "ETH"


def test_normalise_symbol_garbage():
    assert ll._normalise_symbol("") is None
    assert ll._normalise_symbol("BTCUSD") is None  # missing 't' prefix


def test_parse_record_long_liquidated():
    row = _bfx_row(1_700_000_000_000, "tBTCF0:USTF0", -0.5, 50_000)
    rec = ll._parse_record(row)
    assert rec is not None
    assert rec["symbol"] == "BTC"
    assert rec["side"] == "long"  # negative amount => long position liq'd
    assert rec["size_usd"] == pytest.approx(25_000.0)


def test_parse_record_short_liquidated():
    row = _bfx_row(1_700_000_000_000, "tETHF0:USTF0", 2.0, 3000)
    rec = ll._parse_record(row)
    assert rec is not None
    assert rec["side"] == "short"
    assert rec["size_usd"] == pytest.approx(6000.0)


def test_parse_record_skips_unmatched():
    """IS_MATCH=0 rows are the 'open' half — should be filtered to avoid
    double-counting against the matched fill."""
    row = _bfx_row(1_700_000_000_000, "tBTCF0:USTF0", -0.5, 50_000, is_match=0)
    assert ll._parse_record(row) is None


def test_parse_record_skips_zero_amount():
    row = _bfx_row(1_700_000_000_000, "tBTCF0:USTF0", 0, 50_000)
    assert ll._parse_record(row) is None


# ── load_liquidations cache + lag behaviour ─────────────────────────────

def test_load_liquidations_uses_cache_and_applies_lag(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "_DATA_DIR", tmp_path)
    start = 1_700_000_000_000
    day = 86_400_000
    end = start + 3 * day
    # Cache rows: one within the lag window (should be filtered) and one outside.
    cached = [
        {"timestamp": start + day, "symbol": "BTC", "side": "long",
         "size_usd": 100_000.0, "price": 50_000.0, "price_acquired": 50_000.0},
        {"timestamp": end - 60_000, "symbol": "BTC", "side": "short",
         "size_usd": 200_000.0, "price": 51_000.0, "price_acquired": 51_000.0},
    ]
    cache_file = ll._cache_path(start, end)
    ll._write_cache(cache_file, cached)

    # Default lag = 1 day → only the first row survives.
    out = ll.load_liquidations(start, end)
    assert len(out) == 1
    assert out[0]["symbol"] == "BTC"
    assert out[0]["timestamp"] == start + day


def test_load_liquidations_symbol_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(ll, "_DATA_DIR", tmp_path)
    start, end = 1_700_000_000_000, 1_700_000_000_000 + 10 * 86_400_000
    cached = [
        {"timestamp": start + 86_400_000, "symbol": "BTC", "side": "long",
         "size_usd": 1.0, "price": 1.0, "price_acquired": 1.0},
        {"timestamp": start + 86_400_000, "symbol": "ETH", "side": "long",
         "size_usd": 1.0, "price": 1.0, "price_acquired": 1.0},
    ]
    ll._write_cache(ll._cache_path(start, end), cached)

    out = ll.load_liquidations(start, end, symbols=["BTC"])
    assert {r["symbol"] for r in out} == {"BTC"}


# ── aggregate_5m_window ─────────────────────────────────────────────────

def test_aggregate_5m_window_dominant_long():
    t0 = 1_700_000_000_000
    events = [
        {"timestamp": t0 - 60_000, "symbol": "BTC", "side": "long",
         "size_usd": 1_000_000.0},
        {"timestamp": t0 - 30_000, "symbol": "BTC", "side": "long",
         "size_usd": 500_000.0},
        {"timestamp": t0 - 30_000, "symbol": "BTC", "side": "short",
         "size_usd": 200_000.0},
        # Outside the window — must not be counted
        {"timestamp": t0 - 10 * 60_000, "symbol": "BTC", "side": "long",
         "size_usd": 999_000_000.0},
        # Wrong symbol
        {"timestamp": t0 - 30_000, "symbol": "ETH", "side": "long",
         "size_usd": 9_000_000.0},
    ]
    agg = ll.aggregate_5m_window(events, "BTC", t0)
    assert agg["long_liq_usd_5m"] == pytest.approx(1_500_000.0)
    assert agg["short_liq_usd_5m"] == pytest.approx(200_000.0)
    assert agg["dominant_side"] == "long"
    assert agg["count"] == 3
    assert agg["imbalance_ratio"] > 1


def test_aggregate_5m_window_empty():
    agg = ll.aggregate_5m_window([], "BTC", 1_700_000_000_000)
    assert agg["long_liq_usd_5m"] == 0.0
    assert agg["short_liq_usd_5m"] == 0.0
    assert agg["dominant_side"] is None
    assert agg["count"] == 0


# ── load_forward_collected ──────────────────────────────────────────────

def test_load_forward_collected_reads_csv(tmp_path):
    fp = tmp_path / "2025-04-01.csv"
    with open(fp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "symbol", "side", "size_usd", "price"])
        w.writerow([1_700_000_000_000, "BTC", "long", 50000.0, 50000.0])
        w.writerow([1_700_000_500_000, "ETH", "short", 10000.0, 3000.0])
    # No lag (we want both rows back)
    out = ll.load_forward_collected(
        start_ms=1_699_000_000_000,
        end_ms=1_701_000_000_000,
        lag_ms=0,
        data_dir=tmp_path,
    )
    assert len(out) == 2
    assert {r["symbol"] for r in out} == {"BTC", "ETH"}


def test_load_forward_collected_missing_dir_returns_empty(tmp_path):
    out = ll.load_forward_collected(
        0, 1_000_000_000_000, data_dir=tmp_path / "does-not-exist",
    )
    assert out == []
