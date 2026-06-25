"""Engine adopts externally-opened (out-of-band) positions so they get
trailing-stop + chop-exit management — the gap that let the manual ARX long
round-trip a +14% gain to its -10% stop."""

from unittest.mock import MagicMock, patch

import pytest

from src.engine.executor import Executor


@pytest.fixture
def live_executor(tmp_path, monkeypatch):
    monkeypatch.setattr("src.engine.executor._PORTFOLIO_FILE", tmp_path / "p.json")
    monkeypatch.setattr("src.engine.executor.env.binance_api_key", "k", raising=False)
    monkeypatch.setattr("src.engine.executor.env.binance_api_secret", "s", raising=False)
    e = Executor(paper=True, initial_balance=1000.0)
    e._save_state = MagicMock()
    e._sync_watchdog_stop = MagicMock()
    e.positions.clear()
    binance = MagicMock()
    binance.FAPI_BASE = "https://fapi.binance.com"
    binance.name = "binance"
    e._binance = binance
    return e


def _resp(rows):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json = MagicMock(return_value=rows)
    return r


def test_adopts_untracked_position_and_skips_dust(live_executor):
    e = live_executor
    rows = [
        {"symbol": "ARXUSDT", "positionAmt": "52", "entryPrice": "0.3544",
         "markPrice": "0.40", "updateTime": "1700000000000"},
        {"symbol": "LAYERUSDT", "positionAmt": "0.1", "entryPrice": "0.0808",
         "markPrice": "0.078", "updateTime": "1700000000000"},  # dust < $1
    ]
    with patch("src.engine.executor.requests.get", return_value=_resp(rows)):
        e._reconcile_positions("test")

    symbols = {p.symbol for p in e.positions}
    assert "ARX" in symbols           # real position adopted
    assert "LAYER" not in symbols     # dust skipped
    arx = next(p for p in e.positions if p.symbol == "ARX")
    assert arx.side == "long"
    assert arx.quantity == 52
    assert arx.signal_type == "adopted"
    # high-water seeded at the better of entry/mark so the trailing stop can
    # immediately start protecting the in-profit position.
    assert arx.high_watermark == 0.40
    e._sync_watchdog_stop.assert_called()  # watchdog protection wired on adopt


def test_does_not_readopt_already_tracked(live_executor):
    e = live_executor
    rows = [{"symbol": "ARXUSDT", "positionAmt": "52", "entryPrice": "0.3544",
             "markPrice": "0.40", "updateTime": "1700000000000"}]
    with patch("src.engine.executor.requests.get", return_value=_resp(rows)):
        e._reconcile_positions("first")
        e._reconcile_positions("second")
    assert sum(1 for p in e.positions if p.symbol == "ARX") == 1  # adopted once
