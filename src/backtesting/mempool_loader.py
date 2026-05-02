"""BTC mempool fee-market loader (mempool.space free public API).

Documented edge: BTC fee spikes precede major sell-offs (miners netflow stress)
and post-rally tops (retail FOMO). Free, public, no auth.

mempool.space exposes NO history endpoint, so this module must be paired with a
long-running collector (scripts/collect_mempool.py) that snapshots every 5 min
into data/mempool/snapshots.csv. Once 7+ days of history accumulates, the
regime classifier returns meaningful "calm" / "elevated" / "extreme" labels.
Until then, `regime_from_recent` returns "calm" gracefully (no fabrication).
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request


_DEFAULT_CSV = Path(__file__).resolve().parent.parent.parent / "data" / "mempool" / "snapshots.csv"
_FEES_URL = "https://mempool.space/api/v1/fees/recommended"
_MEMPOOL_URL = "https://mempool.space/api/mempool"
_UA = {"User-Agent": "kaizen-trader-mempool/1.0"}

_FIELDS = [
    "ts_ms", "fastest_fee", "half_hour_fee",
    "mempool_vsize", "total_pending_fee_btc",
]

# Need at least this many snapshots in the trailing 7d window to compute a
# real percentile. Below threshold we return "calm" — honest "we don't know".
_MIN_BASELINE_SAMPLES = 200


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        with urlopen(Request(url, headers=_UA), timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def fetch_mempool_state() -> Optional[dict]:
    """Snapshot the current BTC fee market.

    Returns None on network failure (caller decides whether to retry / skip).
    """
    fees = _fetch_json(_FEES_URL)
    pool = _fetch_json(_MEMPOOL_URL)
    if not fees or not pool:
        return None
    total_fee_sats = float(pool.get("total_fee", 0))
    return {
        "ts_ms": int(time.time() * 1000),
        "fastest_fee": int(fees.get("fastestFee", 0)),
        "half_hour_fee": int(fees.get("halfHourFee", 0)),
        "mempool_vsize": int(pool.get("vsize", 0)),
        "total_pending_fee_btc": total_fee_sats / 1e8,
    }


def append_snapshot(out_csv: Path = _DEFAULT_CSV) -> Optional[dict]:
    """Fetch one snapshot and append to CSV. Creates header on first write.

    Returns the written row, or None on fetch failure.
    """
    row = fetch_mempool_state()
    if row is None:
        return None
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_csv.exists()
    with open(out_csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)
    return row


def load_history(out_csv: Path = _DEFAULT_CSV) -> list[dict]:
    """Read all snapshots from CSV, sorted ascending by ts_ms."""
    if not Path(out_csv).exists():
        return []
    rows: list[dict] = []
    with open(out_csv, "r", newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "ts_ms": int(r["ts_ms"]),
                    "fastest_fee": float(r["fastest_fee"]),
                    "half_hour_fee": float(r["half_hour_fee"]),
                    "mempool_vsize": float(r["mempool_vsize"]),
                    "total_pending_fee_btc": float(r["total_pending_fee_btc"]),
                })
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda x: x["ts_ms"])
    return rows


def regime_from_recent(history: list[dict], lookback_hours: int = 24) -> str:
    """Classify current fee regime as 'calm' | 'elevated' | 'extreme'.

    Uses the median fastest_fee over the last `lookback_hours` vs the
    distribution of medians over the trailing 7d. If we don't have enough
    baseline data yet, returns 'calm' (no fabrication — backtest path is
    HONEST: this signal can't be validated until enough live data exists).

    - extreme:   recent median ≥ p95 of trailing-7d
    - elevated:  recent median ≥ p75 of trailing-7d
    - calm:      otherwise (or insufficient baseline)
    """
    if not history:
        return "calm"
    now_ms = history[-1]["ts_ms"]
    week_ms = 7 * 24 * 3600 * 1000
    look_ms = lookback_hours * 3600 * 1000

    baseline = [h["fastest_fee"] for h in history if h["ts_ms"] >= now_ms - week_ms]
    recent = [h["fastest_fee"] for h in history if h["ts_ms"] >= now_ms - look_ms]
    if len(baseline) < _MIN_BASELINE_SAMPLES or not recent:
        return "calm"

    recent_med = sorted(recent)[len(recent) // 2]
    sb = sorted(baseline)
    p75 = sb[int(len(sb) * 0.75)]
    p95 = sb[int(len(sb) * 0.95)]

    if recent_med >= p95:
        return "extreme"
    if recent_med >= p75:
        return "elevated"
    return "calm"
