"""Reconstruct historical top-movers events for backtest replay.

Prod's `_poll_top_movers` polls Binance's 24hr ticker every 60s and fires
`large_move` (>10% 24h change with $5M+ vol) and `major_pump` (>50% with
$10M+ vol) events. The live brain scores those events via the
`acceleration_1h` data field — fresh momentum is the primary lever.

This loader walks hourly through cached 1h klines for a symbol universe,
computes rolling-24h % change and 1h acceleration, and emits an event
list compatible with `SignalDetector._process_large_move`.

The point: most prod trades come from this poller, NOT funding events.
Without it, the backtest measures a strategy variant that rarely runs.

Outputs:
  events: list of {ts_ms, symbol, change_pct, volume_24h_usd,
                   accel_1h_pct, price, event_type}
sorted by ts_ms.
"""

from __future__ import annotations

from typing import Optional

from src.backtesting.data_loader import load_klines


_LARGE_MOVE_PCT = 10.0   # match prod _on_ws_tick threshold
_LARGE_MOVE_VOL = 5_000_000  # match prod _process_large_move filter
_MAJOR_PUMP_PCT = 50.0
_MAJOR_PUMP_VOL = 10_000_000

# RuleBrain scores accel_1h ≥ 5% with +30 (or ≥10% with +50). Emitting
# events on this directly mirrors the brain's primary scoring lever and
# captures fresh momentum that the 24h-change-only path misses.
_ACCEL_BREAKOUT_PCT = 5.0
_ACCEL_BREAKOUT_VOL = 5_000_000

_KLINE_INTERVAL = "1h"
_LOOKBACK_24H = 24


def _change_pct_24h(klines: list[dict], idx: int) -> float:
    if idx < _LOOKBACK_24H:
        return 0.0
    past = float(klines[idx - _LOOKBACK_24H]["close"])
    cur = float(klines[idx]["close"])
    if past <= 0:
        return 0.0
    return (cur - past) / past * 100.0


def _accel_1h_pct(klines: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    prev = float(klines[idx - 1]["close"])
    cur = float(klines[idx]["close"])
    if prev <= 0:
        return 0.0
    return (cur - prev) / prev * 100.0


def _volume_24h_usd(klines: list[dict], idx: int) -> float:
    if idx <= 0:
        return 0.0
    look = min(_LOOKBACK_24H, idx)
    return sum(float(k["close"]) * float(k["volume"]) for k in klines[idx - look:idx])


def reconstruct(
    symbols: list[str],
    start_ms: int,
    end_ms: int,
    move_threshold_pct: float = _LARGE_MOVE_PCT,
    min_volume_usd: float = _LARGE_MOVE_VOL,
) -> list[dict]:
    """Return time-ordered list of synthetic large_move/major_pump events.

    For each symbol, walks its 1h klines and emits one event per hour
    where |24h change| >= move_threshold_pct AND 24h volume >= min_volume_usd.
    """
    events: list[dict] = []
    for sym in symbols:
        try:
            klines = load_klines(sym, _KLINE_INTERVAL, start_ms, end_ms)
        except Exception:
            continue
        if len(klines) < _LOOKBACK_24H + 1:
            continue
        for idx in range(_LOOKBACK_24H, len(klines)):
            change = _change_pct_24h(klines, idx)
            vol = _volume_24h_usd(klines, idx)
            accel = _accel_1h_pct(klines, idx)
            ts_ms = int(klines[idx]["open_time"])
            price = float(klines[idx]["close"])

            qualifies_24h = (
                abs(change) >= move_threshold_pct and vol >= min_volume_usd
            )
            qualifies_accel = (
                abs(accel) >= _ACCEL_BREAKOUT_PCT and vol >= _ACCEL_BREAKOUT_VOL
            )
            if not (qualifies_24h or qualifies_accel):
                continue

            event_type = "major_pump" if (
                change > _MAJOR_PUMP_PCT and vol >= _MAJOR_PUMP_VOL
            ) else "large_move"
            events.append({
                "ts_ms": ts_ms,
                "symbol": sym,
                "change_pct": change,
                "volume_24h_usd": vol,
                "accel_1h_pct": accel,
                "price": price,
                "event_type": event_type,
                "trigger": "24h_change" if qualifies_24h else "accel_1h",
            })
    events.sort(key=lambda e: e["ts_ms"])
    return events


def to_signal_packet_data(event: dict) -> dict:
    """Convert reconstructed event to the `data` dict consumed by
    `SignalDetector._process_large_move` / `_process_major_pump`."""
    return {
        "symbol": event["symbol"],
        "event_type": event["event_type"],
        "ts_ms": event["ts_ms"],
        "price": event["price"],
        "volume_24h": event["volume_24h_usd"],
        "change_pct": event["change_pct"],
        "acceleration_1h": event["accel_1h_pct"],
    }
