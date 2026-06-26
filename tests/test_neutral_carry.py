"""Delta-neutral funding-carry manager. The ONE invariant under test: never
hold a single naked leg. If one leg fills and the other fails, the filled leg is
immediately unwound. Everything mocks providers — no network, no real orders."""

from unittest.mock import MagicMock

import pytest

from src.execution.neutral_carry import NeutralCarryManager
from src.strategies.funding_neutral import NeutralOpportunity


def _opp(symbol="SKHYNIX", f8h=0.0052):
    return NeutralOpportunity(
        symbol=symbol, funding_8h=f8h, perp_side="short",
        gross_daily_pct=abs(f8h) * 3, gross_apr=abs(f8h) * 3 * 365,
        breakeven_days=0.3, recommended=True,
    )


def _filled(side, qty=10.0, price=4.5):
    t = MagicMock()
    t.status = "filled"; t.side = side.lower(); t.quantity = qty
    t.price = price; t.order_id = "1"; t.size_usd = qty * price
    return t


def _failed(side):
    t = MagicMock()
    t.status = "failed"; t.side = side.lower(); t.quantity = 0
    t.price = 0; t.order_id = None; t.size_usd = 0; t.error = "boom"
    return t


@pytest.fixture
def perp():
    p = MagicMock()
    p.name = "binance"
    return p


@pytest.fixture
def spot():
    s = MagicMock()
    s.name = "binance_spot"
    return s


def test_paper_open_records_both_legs():
    mgr = NeutralCarryManager(paper=True)
    pos = mgr.open(_opp(), notional_usd=45.0, mark_price=4.5)
    assert pos is not None
    assert pos.symbol == "SKHYNIX"
    assert pos.perp_side == "short"
    assert pos.spot_qty == pytest.approx(pos.perp_qty)  # delta-neutral
    assert pos.notional_usd == pytest.approx(45.0)


def test_live_open_shorts_perp_and_buys_equal_spot(perp, spot):
    perp.open_short.return_value = _filled("sell")
    spot.place_spot_market.return_value = _filled("buy")
    mgr = NeutralCarryManager(paper=False, perp_provider=perp, spot_provider=spot)

    pos = mgr.open(_opp(), notional_usd=45.0, mark_price=4.5)

    assert pos is not None
    perp.open_short.assert_called_once()
    spot.place_spot_market.assert_called_once()
    # spot leg is a BUY of equal notional
    _, kwargs = spot.place_spot_market.call_args
    args = spot.place_spot_market.call_args.args
    assert "BUY" in (list(args) + list(kwargs.values()))


def test_spot_leg_failure_unwinds_the_filled_perp(perp, spot):
    # perp short fills, spot buy fails -> we MUST close the perp, hold nothing.
    perp.open_short.return_value = _filled("sell")
    spot.place_spot_market.return_value = _failed("buy")
    perp.close_short.return_value = _filled("buy")
    mgr = NeutralCarryManager(paper=False, perp_provider=perp, spot_provider=spot)

    pos = mgr.open(_opp(), notional_usd=45.0, mark_price=4.5)

    assert pos is None                      # no naked position returned
    perp.close_short.assert_called_once()   # filled perp leg was flattened
    assert mgr.positions == []


def test_perp_leg_failure_never_touches_spot(perp, spot):
    # perp short fails first -> spot buy must NOT be attempted at all.
    perp.open_short.return_value = _failed("sell")
    mgr = NeutralCarryManager(paper=False, perp_provider=perp, spot_provider=spot)

    pos = mgr.open(_opp(), notional_usd=45.0, mark_price=4.5)

    assert pos is None
    spot.place_spot_market.assert_not_called()
    perp.close_short.assert_not_called()
    assert mgr.positions == []


def test_unwind_closes_both_legs(perp, spot):
    perp.open_short.return_value = _filled("sell")
    spot.place_spot_market.return_value = _filled("buy")
    perp.close_short.return_value = _filled("buy")
    mgr = NeutralCarryManager(paper=False, perp_provider=perp, spot_provider=spot)
    pos = mgr.open(_opp(), notional_usd=45.0, mark_price=4.5)
    assert pos is not None

    spot.place_spot_market.reset_mock()
    spot.place_spot_market.return_value = _filled("sell")
    ok = mgr.unwind(pos, mark_price=4.4)

    assert ok is True
    perp.close_short.assert_called_once()        # perp bought back
    spot.place_spot_market.assert_called_once()  # spot sold
    sell_args = list(spot.place_spot_market.call_args.args) + list(spot.place_spot_market.call_args.kwargs.values())
    assert "SELL" in sell_args
    assert mgr.positions == []


def test_notional_cap_rejects_oversize(perp, spot):
    mgr = NeutralCarryManager(paper=False, perp_provider=perp, spot_provider=spot,
                              max_notional_usd=45.0)
    pos = mgr.open(_opp(), notional_usd=100.0, mark_price=4.5)
    assert pos is None
    perp.open_short.assert_not_called()


def test_binance_provider_short_adapters_wrap_place_order(monkeypatch):
    """open_short -> SELL reduce_only=False ; close_short -> BUY reduce_only=True."""
    from unittest.mock import patch
    from src.execution.providers import BinanceProvider
    with patch.object(BinanceProvider, "_load_exchange_info", lambda self: None):
        p = BinanceProvider()
    calls = []
    def fake_place(symbol, position_id, side, quantity, market_price, reduce_only=False):
        calls.append((side, reduce_only))
        return _filled(side)
    monkeypatch.setattr(p, "_place_order", fake_place)
    p.open_short("SKHYNIX", "pid", 10.0, 4.5)
    p.close_short("SKHYNIX", "pid", 10.0, 4.5)
    assert calls == [("SELL", False), ("BUY", True)]
