"""Funding rate data loader -- fetches from Binance Futures public API with local CSV caching."""

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "funding"
_BASE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_MAX_PER_REQUEST = 1000


def _cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    """Return the local CSV cache file path for a given query."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper().replace("/", "_").replace("-", "")
    return _DATA_DIR / f"{safe_symbol}_funding_{start_ms}_{end_ms}.csv"


def _parse_record(raw: dict) -> dict:
    """Parse a single Binance funding rate record into a normalised dict."""
    return {
        "funding_time": int(raw["fundingTime"]),
        "funding_rate": float(raw["fundingRate"]),
        "mark_price": float(raw["markPrice"]) if raw.get("markPrice") else 0.0,
    }


def _read_cache(path: Path) -> Optional[list[dict]]:
    """Read cached funding data from CSV, returns None if cache miss."""
    if not path.exists():
        return None
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "funding_time": int(row["funding_time"]),
                "funding_rate": float(row["funding_rate"]),
                "mark_price": float(row["mark_price"]),
            })
    return rows if rows else None


def _write_cache(path: Path, records: list[dict]) -> None:
    """Write funding data to CSV cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["funding_time", "funding_rate", "mark_price"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def _fetch_chunk(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch a single chunk of funding rates from Binance (max 1000 per request)."""
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    url = (
        f"{_BASE_URL}?symbol={pair}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={_MAX_PER_REQUEST}"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return [_parse_record(r) for r in data]
    except (URLError, Exception) as e:
        print(f"  WARNING: funding rate fetch failed for {symbol}: {e}")
        return []


def load_funding_rates(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Load historical funding rate data for a symbol.

    Fetches from Binance Futures public API and caches results to local CSV.
    No authentication required.

    Args:
        symbol: Trading pair symbol (e.g. "BTC", "ETHUSDT", "BTC-USDT")
        start_ms: Start time in milliseconds since epoch
        end_ms: End time in milliseconds since epoch

    Returns:
        List of dicts with keys: funding_time, funding_rate (float), mark_price (float)
    """
    cache_file = _cache_path(symbol, start_ms, end_ms)
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    # Funding events happen every 8 hours, so 1000 records covers ~333 days.
    # Fetch in chunks for longer ranges.
    _EIGHT_HOURS_MS = 8 * 3_600_000
    all_records: list[dict] = []
    cursor = start_ms

    while cursor < end_ms:
        chunk_end = min(cursor + _MAX_PER_REQUEST * _EIGHT_HOURS_MS, end_ms)
        chunk = _fetch_chunk(symbol, cursor, chunk_end)
        if not chunk:
            break
        all_records.extend(chunk)
        # Move cursor past the last record we received
        last_time = chunk[-1]["funding_time"]
        if last_time >= end_ms:
            break
        cursor = last_time + 1
        # Rate limiting: be polite to public API
        time.sleep(0.2)

    # Remove duplicates and sort
    seen: set[int] = set()
    deduped: list[dict] = []
    for r in all_records:
        if r["funding_time"] not in seen:
            seen.add(r["funding_time"])
            deduped.append(r)
    deduped.sort(key=lambda x: x["funding_time"])

    # Filter to requested range
    deduped = [r for r in deduped if start_ms <= r["funding_time"] <= end_ms]

    # Cache for next time
    if deduped:
        _write_cache(cache_file, deduped)

    return deduped
