"""Stablecoin net-flow loader — fetches DefiLlama stablecoin daily totals with CSV caching.

Edge thesis: net stablecoin issuance (today_circulating - 24h_ago_circulating)
> +$300M leads risk-on flows by 12-48h. Net redemption days precede deleveraging.

Endpoint: https://stablecoins.llama.fi/stablecoincharts/all
Returns daily snapshots back to ~Jan 2021. No auth.

Mirrors fgi_loader.py structurally: stdlib urllib only, CSV-cached at
data/stablecoins/stablecoin_history.csv. Cache refreshes if older than 12h.
"""

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "stablecoins"
_CACHE_FILE = _DATA_DIR / "stablecoin_history.csv"
_API_URL = "https://stablecoins.llama.fi/stablecoincharts/all"
_CACHE_MAX_AGE_S = 12 * 3600  # 12 hours


def _read_cache() -> Optional[list[dict]]:
    if not _CACHE_FILE.exists():
        return None
    rows = []
    with open(_CACHE_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "date_ms": int(row["date_ms"]),
                "total_circulating_usd": float(row["total_circulating_usd"]),
                "net_24h_change_usd": float(row["net_24h_change_usd"]),
                "net_7d_change_usd": float(row["net_7d_change_usd"]),
            })
    return rows if rows else None


def _write_cache(records: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["date_ms", "total_circulating_usd",
                        "net_24h_change_usd", "net_7d_change_usd"],
        )
        writer.writeheader()
        writer.writerows(records)


def _cache_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age = time.time() - _CACHE_FILE.stat().st_mtime
    return age < _CACHE_MAX_AGE_S


def load_stablecoin_history(force_refresh: bool = False) -> list[dict]:
    """Load historical stablecoin total-circulating daily snapshots.

    Returns list of dicts sorted by date_ms (ascending):
        date_ms: int (milliseconds)
        total_circulating_usd: float
        net_24h_change_usd: float (computed locally — circulating[i] - circulating[i-1])
        net_7d_change_usd: float (computed locally — circulating[i] - circulating[i-7])

    Data from DefiLlama, free, no auth required. Coverage: ~Jan 2021 — present.
    """
    if not force_refresh and _cache_fresh():
        cached = _read_cache()
        if cached:
            return cached

    req = Request(_API_URL, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  WARNING: stablecoin API failed: {e}")
        # Fall back to stale cache if we have one
        cached = _read_cache()
        return cached or []

    # Build sorted (date_ms, circulating) pairs
    raw: list[tuple[int, float]] = []
    for entry in data:
        try:
            ts_s = int(entry.get("date", 0))
            pegged = entry.get("totalCirculating", {}) or {}
            usd = float(pegged.get("peggedUSD", 0.0))
        except (TypeError, ValueError):
            continue
        if ts_s <= 0 or usd <= 0:
            continue
        raw.append((ts_s * 1000, usd))

    raw.sort(key=lambda x: x[0])

    records: list[dict] = []
    for i, (ts_ms, usd) in enumerate(raw):
        net_24h = usd - raw[i - 1][1] if i >= 1 else 0.0
        net_7d = usd - raw[i - 7][1] if i >= 7 else 0.0
        records.append({
            "date_ms": ts_ms,
            "total_circulating_usd": usd,
            "net_24h_change_usd": net_24h,
            "net_7d_change_usd": net_7d,
        })

    if records:
        _write_cache(records)
        print(f"  Stablecoin data loaded: {len(records)} days "
              f"(${records[0]['total_circulating_usd']/1e9:.1f}B → "
              f"${records[-1]['total_circulating_usd']/1e9:.1f}B)")

    return records


def get_stablecoin_flow_at_timestamp(
    history: list[dict], ts_ms: float
) -> Optional[dict]:
    """Get the stablecoin row at or before the given timestamp.

    Uses binary search since history is sorted. Returns None if before data starts.
    """
    if not history:
        return None
    if ts_ms < history[0]["date_ms"]:
        return None
    if ts_ms >= history[-1]["date_ms"]:
        return history[-1]

    lo, hi = 0, len(history) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if history[mid]["date_ms"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return history[lo]
