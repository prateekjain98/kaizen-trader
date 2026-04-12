"""Open Interest historical data loader -- fetches from Binance Futures public API with local CSV caching."""

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "oi"
_BASE_URL = "https://fapi.binance.com/futures/data/openInterestHist"
_MAX_PER_REQUEST = 500
_ONE_HOUR_MS = 3_600_000


def _cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    """Return the local CSV cache file path for a given query."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper().replace("/", "_").replace("-", "")
    return _DATA_DIR / f"{safe_symbol}_oi_{start_ms}_{end_ms}.csv"


def _parse_record(raw: dict) -> dict:
    """Parse a single Binance OI record into a normalised dict."""
    return {
        "timestamp": int(raw["timestamp"]),
        "sum_open_interest": float(raw["sumOpenInterest"]),
        "sum_open_interest_value": float(raw["sumOpenInterestValue"]),
    }


def _read_cache(path: Path) -> Optional[list[dict]]:
    """Read cached OI data from CSV, returns None if cache miss."""
    if not path.exists():
        return None
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp": int(row["timestamp"]),
                "sum_open_interest": float(row["sum_open_interest"]),
                "sum_open_interest_value": float(row["sum_open_interest_value"]),
            })
    return rows if rows else None


def _write_cache(path: Path, records: list[dict]) -> None:
    """Write OI data to CSV cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["timestamp", "sum_open_interest", "sum_open_interest_value"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _fetch_chunk(symbol: str, start_ms: int) -> list[dict]:
    """Fetch a single chunk of OI history from Binance (max 500 per request).

    Note: the Binance endpoint does not support endTime, only startTime + limit.
    """
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    url = (
        f"{_BASE_URL}?symbol={pair}&period=1h"
        f"&startTime={start_ms}&limit={_MAX_PER_REQUEST}"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return [_parse_record(r) for r in data]
    except (URLError, Exception) as e:
        print(f"  WARNING: OI fetch failed for {symbol}: {e}")
        return []


def load_open_interest(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Load historical open interest data for a symbol.

    Fetches from Binance Futures public API and caches results to local CSV.
    No authentication required.

    Args:
        symbol: Trading pair symbol (e.g. "BTC", "ETHUSDT", "BTC-USDT")
        start_ms: Start time in milliseconds since epoch
        end_ms: End time in milliseconds since epoch

    Returns:
        List of dicts with keys: timestamp, sum_open_interest (float), sum_open_interest_value (float)
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
        # Move cursor past the last record we received
        last_time = chunk[-1]["timestamp"]
        if last_time >= end_ms:
            break
        cursor = last_time + _ONE_HOUR_MS  # advance by 1h to avoid overlap
        # Rate limiting: be polite to public API
        time.sleep(0.2)

    # Remove duplicates and sort
    seen: set[int] = set()
    deduped: list[dict] = []
    for r in all_records:
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            deduped.append(r)
    deduped.sort(key=lambda x: x["timestamp"])

    # Filter to requested range
    deduped = [r for r in deduped if start_ms <= r["timestamp"] <= end_ms]

    # Cache for next time
    if deduped:
        _write_cache(cache_file, deduped)

    return deduped
