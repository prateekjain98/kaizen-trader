"""Fee-aware EV gate (#5) + edge-based sizing (#8) in rule_brain."""

import src.engine.rule_brain as rb


def _seed_winrate(strategy, win_rate, n):
    rb._EDGE_CACHE["ts"] = 1e18  # far future so cache is considered fresh
    rb._EDGE_CACHE["by_strategy"] = {strategy: {"win_rate": win_rate, "n": n}}


def test_negative_edge_strategy_has_negative_ev_and_floor_mult():
    # 20% WR, 10% stop / 25% target: ev = .2*.25 - .8*.10 - cost < 0
    # (at 2.5:1 R:R the break-even WR is ~29%, so 20% is genuinely negative-edge)
    _seed_winrate("funding_squeeze", 0.20, 40)
    ev, wr, n, mult = rb._edge_metrics("funding_squeeze", 0.10, 0.25)
    assert ev < 0
    assert mult == rb.EDGE_MULT_MIN  # negative kelly -> floored


def test_positive_edge_strategy_has_positive_ev_and_boosted_mult():
    # 60% WR, 10% stop / 25% target: ev = .6*.25 - .4*.10 - cost > 0
    _seed_winrate("funding_squeeze", 0.60, 40)
    ev, wr, n, mult = rb._edge_metrics("funding_squeeze", 0.10, 0.25)
    assert ev > 0
    assert 1.0 <= mult <= rb.EDGE_MULT_MAX


def test_multiplier_always_bounded():
    for wr in (0.0, 0.5, 0.9, 1.0):
        _seed_winrate("x", wr, 50)
        _, _, _, mult = rb._edge_metrics("x", 0.10, 0.30)
        assert rb.EDGE_MULT_MIN <= mult <= rb.EDGE_MULT_MAX


def test_no_history_is_neutral():
    rb._EDGE_CACHE["ts"] = 1e18
    rb._EDGE_CACHE["by_strategy"] = {}
    ev, wr, n, mult = rb._edge_metrics("unknown_strat", 0.10, 0.25)
    assert n == 0  # caller treats n < MIN_TRADES_FOR_EDGE as neutral (no gate, mult unused)
