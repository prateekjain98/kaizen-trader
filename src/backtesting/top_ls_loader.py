"""Top-trader long/short position ratio loader -- fetches from Binance Futures
public API with local CSV caching.

Mirrors `oi_loader.py`. The endpoint returns up to ~30 days of history with
5-minute granularity, which matches the live `top_trader_crowding_filter`
cadence (the live filter caches the latest value with a 5-min TTL).
"""

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "top_ls"
_BASE_URL = "https://fapi.binance.com/futures/data/topLongShortPositionRatio"
_MAX_PER_REQUEST = 500
_FIVE_MIN_MS = 5 * 60 * 1000


def _cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper().replace("/", "_").replace("-", "")
    return _DATA_DIR / f"{safe_symbol}_topls_{start_ms}_{end_ms}.csv"


def _parse_record(raw: dict) -> dict:
    return {
        "timestamp": int(raw["timestamp"]),
        "long_short_ratio": float(raw["longShortRatio"]),
        "long_account": float(raw.get("longAccount", 0) or 0),
        "short_account": float(raw.get("shortAccount", 0) or 0),
    }


def _read_cache(path: Path) -> Optional[list[dict]]:
    if not path.exists():
        return None
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp": int(row["timestamp"]),
                "long_short_ratio": float(row["long_short_ratio"]),
                "long_account": float(row["long_account"]),
                "short_account": float(row["short_account"]),
            })
    return rows if rows else None


def _write_cache(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["timestamp", "long_short_ratio", "long_account", "short_account"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _fetch_chunk(symbol: str, start_ms: int) -> list[dict]:
    """Fetch a single chunk of top L/S history from Binance (max 500 per request)."""
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    url = (
        f"{_BASE_URL}?symbol={pair}&period=5m"
        f"&startTime={start_ms}&limit={_MAX_PER_REQUEST}"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return [_parse_record(r) for r in data]
    except (URLError, Exception) as e:
        print(f"  WARNING: top L/S fetch failed for {symbol}: {e}")
        return []


def load_top_ls_ratio(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Load historical top-trader long/short position ratio for a symbol.

    Fetches from Binance Futures public API and caches results to local CSV.
    No authentication required. Binance retains roughly 30 days of history;
    older windows return an empty list.

    Args:
        symbol: Trading pair symbol (e.g. "BTC", "ETHUSDT", "BTC-USDT")
        start_ms: Start time in milliseconds since epoch
        end_ms: End time in milliseconds since epoch

    Returns:
        List of dicts: timestamp, long_short_ratio, long_account, short_account.
    """
    cache_file = _cache_path(symbol, start_ms, end_ms)
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    all_records: list[dict] = []
    cursor = start_ms

    while cursor < end_ms:
        chunk = _fetch_chunk(symbol, cursor)
        if not chunk:
            break
        all_records.extend(chunk)
        last_time = chunk[-1]["timestamp"]
        if last_time >= end_ms:
            break
        cursor = last_time + _FIVE_MIN_MS
        time.sleep(0.2)

    seen: set[int] = set()
    deduped: list[dict] = []
    for r in all_records:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            deduped.append(r)
    deduped.sort(key=lambda x: x["timestamp"])
    deduped = [r for r in deduped if start_ms <= r["timestamp"] <= end_ms]

    if deduped:
        _write_cache(cache_file, deduped)

    return deduped
