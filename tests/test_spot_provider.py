"""Binance SPOT execution provider (api/v3) — the long leg of the delta-neutral
funding carry. Futures-only engine had no spot path; this adds it.

All tests mock `requests` so nothing hits the network or places a real order."""

from unittest.mock import MagicMock, patch

import pytest

from src.execution.spot_providers import BinanceSpotProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr("src.execution.spot_providers.env.binance_api_key", "k", raising=False)
    monkeypatch.setattr("src.execution.spot_providers.env.binance_api_secret", "s", raising=False)
    # Skip the network exchangeInfo load; seed filters directly.
    with patch.object(BinanceSpotProvider, "_load_exchange_info", lambda self: None):
        p = BinanceSpotProvider()
    p._step_sizes = {"SKHYNIXUSDT": 0.01, "BTCUSDT": 0.00001}
    p._min_qty = {"SKHYNIXUSDT": 0.01, "BTCUSDT": 0.00001}
    p._min_notional = {"SKHYNIXUSDT": 5.0, "BTCUSDT": 5.0}
    p._exchange_info_loaded = True
    return p


def _ok(payload):
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.status_code = 200
    r.json = MagicMock(return_value=payload)
    return r


def test_base_is_spot_not_futures(provider):
    # Must hit spot api, never the futures fapi host.
    assert provider.SPOT_BASE == "https://api.binance.com"
    assert "fapi" not in provider.SPOT_BASE
    assert provider.name == "binance_spot"


def test_buy_places_signed_market_order(provider):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["apikey"] = headers.get("X-MBX-APIKEY")
        return _ok({"orderId": 99, "status": "FILLED",
                    "executedQty": "10.0", "cummulativeQuoteQty": "45.0",
                    "fills": [{"price": "4.5", "qty": "10.0", "commission": "0.01"}]})

    with patch("src.execution.spot_providers.requests.post", side_effect=fake_post):
        trade = provider.place_spot_market("SKHYNIX", "pos1", "BUY", quantity=10.0,
                                           market_price=4.5)

    assert trade.status == "filled"
    assert trade.side == "buy"
    assert trade.order_id == "99"
    assert trade.quantity == pytest.approx(10.0)
    assert trade.price == pytest.approx(4.5)
    # endpoint + signing
    assert captured["url"] == "https://api.binance.com/api/v3/order"
    assert "SKHYNIXUSDT" in captured["data"]
    assert "type=MARKET" in captured["data"]
    assert "signature=" in captured["data"]
    assert "reduceOnly" not in captured["data"]  # spot has no reduceOnly
    assert captured["apikey"] == "k"


def test_sell_uses_held_quantity(provider):
    def fake_post(url, headers=None, data=None, timeout=None):
        assert "side=SELL" in data
        return _ok({"orderId": 100, "status": "FILLED", "executedQty": "10.0",
                    "cummulativeQuoteQty": "46.0",
                    "fills": [{"price": "4.6", "qty": "10.0", "commission": "0.01"}]})

    with patch("src.execution.spot_providers.requests.post", side_effect=fake_post):
        trade = provider.place_spot_market("SKHYNIX", "pos1", "SELL", quantity=10.0,
                                           market_price=4.6)
    assert trade.status == "filled"
    assert trade.side == "sell"


def test_quantity_rounded_to_lot_step(provider):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["data"] = data
        return _ok({"orderId": 1, "status": "FILLED", "executedQty": "10.12",
                    "cummulativeQuoteQty": "45.5", "fills": []})

    with patch("src.execution.spot_providers.requests.post", side_effect=fake_post):
        provider.place_spot_market("SKHYNIX", "p", "BUY", quantity=10.129, market_price=4.5)
    # step 0.01 -> 10.12, not 10.129
    assert "quantity=10.12" in captured["data"]


def test_rejects_below_min_notional_without_calling_api(provider):
    with patch("src.execution.spot_providers.requests.post") as post:
        trade = provider.place_spot_market("SKHYNIX", "p", "BUY", quantity=0.01,
                                           market_price=4.5)  # $0.045 << $5 min
    post.assert_not_called()
    assert trade.status in ("failed", "error")
    assert "notional" in (trade.error or "").lower()


def test_missing_keys_fails_safe(monkeypatch, provider):
    monkeypatch.setattr("src.execution.spot_providers.env.binance_api_key", "", raising=False)
    monkeypatch.setattr("src.execution.spot_providers.env.binance_api_secret", "", raising=False)
    with patch("src.execution.spot_providers.requests.post") as post:
        trade = provider.place_spot_market("SKHYNIX", "p", "BUY", quantity=10.0,
                                           market_price=4.5)
    post.assert_not_called()
    assert trade.status in ("failed", "error")


def test_get_spot_balances_parses_free(provider):
    payload = {"balances": [
        {"asset": "USDT", "free": "144.14", "locked": "0.0"},
        {"asset": "SKHYNIX", "free": "10.0", "locked": "0.0"},
        {"asset": "ZERO", "free": "0.0", "locked": "0.0"},
    ]}
    with patch("src.execution.spot_providers.requests.get", return_value=_ok(payload)):
        bals = provider.get_spot_balances()
    assert bals["USDT"] == pytest.approx(144.14)
    assert bals["SKHYNIX"] == pytest.approx(10.0)
    assert "ZERO" not in bals  # zero balances dropped
