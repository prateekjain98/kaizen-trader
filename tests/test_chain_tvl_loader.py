"""Tests for chain_tvl_loader — pure-function logic, no network."""

import os
from unittest.mock import patch, MagicMock

import pytest

from src.backtesting import chain_tvl_loader as ctl


def _mk_history():
    """8 days of TVL: linear growth then a 10% pop on day 8."""
    base = 100_000_000_000.0  # $100B
    rows = []
    day_ms = 86_400_000
    t0 = 1_700_000_000_000
    vals = [base, base*1.01, base*1.02, base*1.03, base*1.04,
            base*1.05, base*1.06, base*1.06*1.10]
    for i, v in enumerate(vals):
        prev = vals[i-1] if i >= 1 else v
        prev7 = vals[i-7] if i >= 7 else v
        n24 = (v - prev) / prev * 100.0 if prev else 0.0
        n7 = (v - prev7) / prev7 * 100.0 if prev7 else 0.0
        rows.append({
            "date_ms": t0 + i * day_ms,
            "tvl_usd": v,
            "net_24h_change_pct": n24,
            "net_7d_change_pct": n7,
        })
    return rows


def test_local_24h_pct_computed_correctly():
    rows = _mk_history()
    # Day 8: +10% on top of day 7
    assert rows[7]["net_24h_change_pct"] == pytest.approx(10.0, rel=1e-6)
    # 7d change day 7 vs day 0: ~6%
    assert rows[7]["net_7d_change_pct"] > 5.0


def test_get_at_timestamp_binary_search():
    rows = _mk_history()
    # Exact hit
    assert ctl.get_chain_tvl_at_timestamp(rows, rows[3]["date_ms"]) is rows[3]
    # Before-range
    assert ctl.get_chain_tvl_at_timestamp(rows, rows[0]["date_ms"] - 1) is None
    # Mid-day (should return floor)
    mid = rows[3]["date_ms"] + 12 * 3_600_000
    got = ctl.get_chain_tvl_at_timestamp(rows, mid)
    assert got is rows[3]
    # After end
    assert ctl.get_chain_tvl_at_timestamp(rows, rows[-1]["date_ms"] + 99) is rows[-1]
    # Empty
    assert ctl.get_chain_tvl_at_timestamp([], 12345) is None


def test_chain_symbol_map_covers_required_chains():
    assert "Ethereum" in ctl.CHAIN_SYMBOL_MAP
    assert "Solana" in ctl.CHAIN_SYMBOL_MAP
    assert "Arbitrum" in ctl.CHAIN_SYMBOL_MAP
    assert "ETH" in ctl.CHAIN_SYMBOL_MAP["Ethereum"]
    assert "SOL" in ctl.CHAIN_SYMBOL_MAP["Solana"]
    assert "ARB" in ctl.CHAIN_SYMBOL_MAP["Arbitrum"]


def test_prod_gate_default_off():
    # Default env: should be off
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CHAIN_FLOW_ENABLED", None)
        assert ctl.chain_flow_enabled_in_prod() is False
    with patch.dict(os.environ, {"CHAIN_FLOW_ENABLED": "1"}):
        assert ctl.chain_flow_enabled_in_prod() is True
    with patch.dict(os.environ, {"CHAIN_FLOW_ENABLED": "0"}):
        assert ctl.chain_flow_enabled_in_prod() is False


def test_load_handles_api_failure(tmp_path, monkeypatch):
    # Force cache miss + URL failure → empty list, no exception
    monkeypatch.setattr(ctl, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ctl, "_cache_file",
                        lambda chain: tmp_path / f"{chain}.json")

    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(ctl, "urlopen", boom)
    out = ctl.load_chain_tvl_history("FakeChain", force_refresh=True)
    assert out == []


def test_load_parses_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(ctl, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ctl, "_cache_file",
                        lambda chain: tmp_path / f"{chain}.json")

    fake_payload = [
        {"date": 1_700_000_000, "tvl": 100e9},
        {"date": 1_700_086_400, "tvl": 105e9},   # +5%
        {"date": 1_700_172_800, "tvl": 110.25e9},  # +5%
    ]

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return __import__("json").dumps(fake_payload).encode()

    monkeypatch.setattr(ctl, "urlopen", lambda *a, **kw: FakeResp())
    rows = ctl.load_chain_tvl_history("Ethereum", force_refresh=True)
    assert len(rows) == 3
    assert rows[1]["net_24h_change_pct"] == pytest.approx(5.0, abs=0.01)
    assert rows[2]["net_24h_change_pct"] == pytest.approx(5.0, abs=0.01)
    # Sorted ascending
    assert rows[0]["date_ms"] < rows[1]["date_ms"] < rows[2]["date_ms"]
    # Cache written
    assert (tmp_path / "Ethereum.json").exists()


def test_live_replay_chain_flow_event_shape():
    """Smoke-test that live_replay imports cleanly and signal_type wiring works."""
    from src.backtesting.live_replay import replay  # noqa: F401
    from src.engine.rule_brain import STRATEGY_RISK
    assert "chain_flow_bull" in STRATEGY_RISK
    assert "chain_flow_bear" in STRATEGY_RISK
    assert STRATEGY_RISK["chain_flow_bull"]["target_pct"] > \
        STRATEGY_RISK["chain_flow_bear"]["target_pct"]
