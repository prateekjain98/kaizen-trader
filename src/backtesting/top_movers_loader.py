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
#
# Threshold tuning history (kept here so the rationale survives):
#   5%  → 175 events on 10 alts × 90d, but win rate dropped from 60→25%
#         in W2 (commit 1fdfe6d) — too noisy, low-quality bursts displaced
#         better trades from MAX_DECISIONS=3 budget
#   8%  → still 4 trades W2 25% WR, agg -$0.47 (worse) — accel events
#         at 8% still bad enough to displace winners
#   10% → restored to original 1h-base behavior (mega-accel zone)
_ACCEL_BREAKOUT_PCT = 10.0
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


def accel_events_from_15m(
    symbol: str,
    klines_15m: list[dict],
    accel_threshold_pct: float = _ACCEL_BREAKOUT_PCT,
    min_volume_usd: float = _ACCEL_BREAKOUT_VOL,
    cooldown_minutes: int = 30,
) -> list[dict]:
    """Emit large_move events from a 15m kline series whenever a sliding-1h
    window shows |move| >= accel_threshold_pct with sufficient volume.

    This catches sub-hour bursts that the 1h-kline path misses — the live
    `AccelerationTracker` samples WS ticks continuously, so 15m is the
    closest cached approximation we have offline.

    Cooldown prevents 4 consecutive 15m bars from each emitting their own
    event for the same underlying breakout (default 30 min between emits
    per symbol).
    """
    if len(klines_15m) < 4:
        return []
    bars_per_hour = 4
    bars_per_24h = 96
    events: list[dict] = []
    last_emit_ms = -10**18
    cooldown_ms = cooldown_minutes * 60_000
    for i in range(bars_per_hour, len(klines_15m)):
        cur_close = float(klines_15m[i]["close"])
        prev_close = float(klines_15m[i - bars_per_hour]["close"])
        if prev_close <= 0:
            continue
        move_pct = (cur_close - prev_close) / prev_close * 100.0
        if abs(move_pct) < accel_threshold_pct:
            continue
        # Rolling 24h vol from last 96 bars (or what's available)
        look = min(bars_per_24h, i)
        vol_24h = sum(
            float(k["close"]) * float(k["volume"])
            for k in klines_15m[i - look:i]
        )
        if vol_24h < min_volume_usd:
            continue
        ts_ms = int(klines_15m[i]["open_time"])
        if ts_ms - last_emit_ms < cooldown_ms:
            continue
        last_emit_ms = ts_ms
        events.append({
            "ts_ms": ts_ms,
            "symbol": symbol,
            "change_pct": move_pct,  # this is the 1h sliding move, not 24h
            "volume_24h_usd": vol_24h,
            "accel_1h_pct": move_pct,
            "price": cur_close,
            "event_type": "large_move",
            "trigger": "accel_1h_15m",
        })
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
