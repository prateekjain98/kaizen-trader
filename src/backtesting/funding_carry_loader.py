"""Cross-sectional funding-carry event reconstruction for backtest replay.

The live `funding_squeeze` signal fires per-symbol whenever |rate| > 0.1%.
That measures absolute funding pain on ONE name. Cross-sectional carry is
the stronger sibling: at each 8h funding boundary, RANK every liquid perp
by funding rate and only act on the extreme tails of the distribution.

  * Top decile (most-positive funding) → SHORT — longs are paying through
    the nose; reversion historically reclaims the carry premium.
  * Bottom decile (most-negative funding) → LONG — shorts are paying;
    same logic in reverse.

This converts a single-name punt into a diversified factor and matches
the academic "carry" literature (Sharpe ~1+ standalone, additive to
momentum). Documented thesis from research; this loader is the offline
replay analogue, mirroring the patterns of `top_movers_loader.reconstruct`
and `funding_loader.load_funding_rates`.

Outputs events: {ts_ms, symbol, side_hint, funding_rank_pct, rate,
mark_price, event_type} sorted by ts_ms. event_type is
"funding_carry_long" or "funding_carry_short" so the live_replay event
loop can route them to a dedicated SignalPacket builder distinct from
the absolute-level funding_squeeze path.
"""

from __future__ import annotations

from src.backtesting.funding_loader import load_funding_rates


# A funding boundary that has fewer symbols than this is too thin to rank
# meaningfully — skip emitting events rather than fire on a 3-symbol
# universe where "top decile" rounds to 1 name.
_MIN_SYMBOLS_FOR_RANKING = 8

# Minimum |rate| for an extreme to count. Without this floor, on a quiet
# day the "top decile" can be 0.01% — well inside funding noise — and the
# events become indistinguishable from random sampling. 0.05% matches
# half the funding_squeeze threshold, so carry catches the ranked extremes
# that funding_squeeze would otherwise ignore.
_MIN_RATE_FLOOR = 0.0005

# Funding events on Binance settle every 8h. We bucket all symbols' rates
# into the same boundary by rounding to the nearest 8h slot — handles any
# minor clock skew between symbols' returned fundingTime values.
_FUNDING_BOUNDARY_MS = 8 * 3_600_000


def _bucket_ts(ts_ms: int) -> int:
    """Round funding time to nearest 8h slot for cross-symbol grouping."""
    return (ts_ms // _FUNDING_BOUNDARY_MS) * _FUNDING_BOUNDARY_MS


def reconstruct_funding_carry(
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    top_pct: float = 0.1,
) -> list[dict]:
    """Return time-ordered cross-sectional funding-carry events.

    Walks each 8h boundary, ranks every available symbol by funding rate,
    emits SHORT events for the top `top_pct` (most-positive rates) and
    LONG events for the bottom `top_pct` (most-negative rates). Symbols
    whose absolute rate is below `_MIN_RATE_FLOOR` are filtered before
    ranking — a quiet-day "top decile" of 0.01% is meaningless noise.

    Args:
        symbols: Universe of perp symbols to rank cross-sectionally.
        start_ms: Window start (inclusive).
        end_ms: Window end (inclusive).
        top_pct: Fraction of each tail to emit (default 0.1 = top/bot 10%).

    Returns:
        List of dicts: {ts_ms, symbol, side_hint, funding_rank_pct, rate,
        mark_price, event_type}. event_type is "funding_carry_long" or
        "funding_carry_short". Sorted by ts_ms.
    """
    # Per-symbol funding history, bucketed to the canonical 8h slot.
    #   bucket_ts -> {symbol -> (rate, mark_price)}
    by_bucket: dict[int, dict[str, tuple[float, float]]] = {}
    for sym in symbols:
        try:
            rows = load_funding_rates(sym, start_ms, end_ms)
        except Exception:
            continue
        for r in rows:
            bts = _bucket_ts(int(r["funding_time"]))
            if bts < start_ms or bts > end_ms:
                continue
            by_bucket.setdefault(bts, {})[sym] = (
                float(r["funding_rate"]),
                float(r["mark_price"]),
            )

    events: list[dict] = []
    for bts in sorted(by_bucket.keys()):
        snapshot = by_bucket[bts]
        if len(snapshot) < _MIN_SYMBOLS_FOR_RANKING:
            continue

        # Sort all symbols' rates ascending. Bottom slice = most negative
        # (LONG signal), top slice = most positive (SHORT signal).
        ranked = sorted(snapshot.items(), key=lambda kv: kv[1][0])
        n = len(ranked)
        k = max(1, int(round(n * top_pct)))

        bottom = ranked[:k]   # most-negative rates
        top = ranked[-k:]     # most-positive rates

        for i, (sym, (rate, mark)) in enumerate(bottom):
            if abs(rate) < _MIN_RATE_FLOOR:
                continue
            events.append({
                "ts_ms": bts,
                "symbol": sym,
                "side_hint": "long",
                "funding_rank_pct": (i + 1) / n,
                "rate": rate,
                "mark_price": mark,
                "event_type": "funding_carry_long",
            })

        for j, (sym, (rate, mark)) in enumerate(top):
            if abs(rate) < _MIN_RATE_FLOOR:
                continue
            # Rank from the top — symbol with highest rate is rank 1/n.
            rank_from_top = (k - j) / n
            events.append({
                "ts_ms": bts,
                "symbol": sym,
                "side_hint": "short",
                "funding_rank_pct": rank_from_top,
                "rate": rate,
                "mark_price": mark,
                "event_type": "funding_carry_short",
            })

    events.sort(key=lambda e: e["ts_ms"])
    return events
