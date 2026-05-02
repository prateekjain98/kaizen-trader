"""Fear & Greed Index loader — fetches from Alternative.me free API with CSV caching."""

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "fgi"
_CACHE_FILE = _DATA_DIR / "fgi_history.csv"
_API_URL = "https://api.alternative.me/fng/?limit=0"


def _read_cache() -> Optional[list[dict]]:
    if not _CACHE_FILE.exists():
        return None
    rows = []
    with open(_CACHE_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp_ms": int(row["timestamp_ms"]),
                "value": int(row["value"]),
                "classification": row["classification"],
            })
    return rows if rows else None


def _write_cache(records: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp_ms", "value", "classification"])
        writer.writeheader()
        writer.writerows(records)


def load_fear_greed_index(force_refresh: bool = False) -> list[dict]:
    """Load historical Fear & Greed Index data.

    Returns list of dicts sorted by timestamp_ms (ascending):
        timestamp_ms: int (milliseconds)
        value: int (0-100)
        classification: str ('Extreme Fear', 'Fear', 'Neutral', 'Greed', 'Extreme Greed')

    Data from Alternative.me, free, no auth required. Coverage: Feb 2018 — present.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached

    req = Request(_API_URL, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  WARNING: FGI API failed: {e}")
        return []

    raw = data.get("data", [])
    records = []
    for entry in raw:
        records.append({
            "timestamp_ms": int(entry["timestamp"]) * 1000,
            "value": int(entry["value"]),
            "classification": entry.get("value_classification", ""),
        })

    # Sort ascending by time
    records.sort(key=lambda x: x["timestamp_ms"])

    if records:
        _write_cache(records)
        print(f"  FGI data loaded: {len(records)} days ({records[0]['classification']} to {records[-1]['classification']})")

    return records


def get_fgi_at_timestamp(fgi_data: list[dict], ts_ms: float) -> Optional[int]:
    """Get the FGI value at or before the given timestamp.

    Uses binary search for efficiency since fgi_data is sorted.
    Returns the FGI value (0-100), or **None** when no real data is available
    (empty dataset OR timestamp predates the FGI dataset start in Feb 2018).

    Previously returned 50 (neutral) on missing data — that silently fabricated
    a "Neutral" reading for any caller that gated on `fgi <= 20` or `fgi >= 80`,
    causing the rule to never fire in pre-2018 windows when the answer should
    be "we don't know." Per the no-fabricated-data discipline, callers must now
    handle None explicitly.
    """
    if not fgi_data:
        return None

    # Binary search for nearest timestamp <= ts_ms
    lo, hi = 0, len(fgi_data) - 1
    if ts_ms < fgi_data[0]["timestamp_ms"]:
        return None  # before data starts — we genuinely don't know
    if ts_ms >= fgi_data[-1]["timestamp_ms"]:
        return fgi_data[-1]["value"]

    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fgi_data[mid]["timestamp_ms"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1

    return fgi_data[lo]["value"]
