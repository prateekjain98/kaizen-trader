"""Historical kline data loader — fetches from Binance public API with local CSV caching."""

import csv
import os
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_BASE_URL = "https://data-api.binance.vision/api/v3/klines"
_MAX_CANDLES_PER_REQUEST = 1000

_VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _cache_path(symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    """Return the local CSV cache file path for a given query."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper().replace("/", "_")
    return _DATA_DIR / f"{safe_symbol}_{interval}_{start_ms}_{end_ms}.csv"


def _parse_kline(raw: list) -> dict:
    """Parse a single Binance kline array into a dict."""
    return {
        "open_time": int(raw[0]),
        "open": float(raw[1]),
        "high": float(raw[2]),
        "low": float(raw[3]),
        "close": float(raw[4]),
        "volume": float(raw[5]),
        "close_time": int(raw[6]),
    }


def _read_cache(path: Path) -> Optional[list[dict]]:
    """Read cached kline data from CSV, returns None if cache miss."""
    if not path.exists():
        return None
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "open_time": int(row["open_time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "close_time": int(row["close_time"]),
            })
    return rows if rows else None


def _write_cache(path: Path, klines: list[dict]) -> None:
    """Write kline data to CSV cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(klines)


def _fetch_klines_chunk(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch a single chunk of klines from Binance (max 1000 per request)."""
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    url = (
        f"{_BASE_URL}?symbol={pair}&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={_MAX_CANDLES_PER_REQUEST}"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return [_parse_kline(row) for row in data]


def load_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Load historical kline/candlestick data for a symbol.

    Fetches from Binance public API and caches results to local CSV.
    No authentication required.

    Args:
        symbol: Trading pair symbol (e.g. "BTC", "ETHUSDT", "BTC-USDT")
        interval: Candle interval - one of "1m", "5m", "15m", "1h", "4h", "1d"
        start_ms: Start time in milliseconds since epoch
        end_ms: End time in milliseconds since epoch

    Returns:
        List of dicts with keys: open_time, open, high, low, close, volume, close_time
    """
    if interval not in _VALID_INTERVALS:
        raise ValueError(f"Invalid interval '{interval}'. Must be one of {_VALID_INTERVALS}")

    cache_file = _cache_path(symbol, interval, start_ms, end_ms)
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    # Fetch in chunks to handle large date ranges
    interval_ms = _INTERVAL_MS[interval]
    all_klines: list[dict] = []
    cursor = start_ms

    # If first chunk returns empty, find when data begins using startTime=0 (single call)
    first_chunk = _fetch_klines_chunk(symbol, interval, cursor, min(cursor + _MAX_CANDLES_PER_REQUEST * interval_ms, end_ms))
    if not first_chunk:
        # Fast probe: fetch with startTime=0 and large endTime to find first available candle
        probe_chunk = _fetch_klines_chunk(symbol, interval, 0, end_ms)
        if probe_chunk:
            actual_start = probe_chunk[0]["open_time"]
            if actual_start <= end_ms:
                cursor = max(actual_start, start_ms)
                # Keep any candles from probe that are in our range
                in_range = [c for c in probe_chunk if c["open_time"] >= start_ms and c["open_time"] <= end_ms]
                if in_range:
                    all_klines.extend(in_range)
                    cursor = in_range[-1]["close_time"] + 1
    else:
        all_klines.extend(first_chunk)
        cursor = first_chunk[-1]["close_time"] + 1

    while cursor < end_ms:
        chunk_end = min(cursor + _MAX_CANDLES_PER_REQUEST * interval_ms, end_ms)
        chunk = _fetch_klines_chunk(symbol, interval, cursor, chunk_end)
        if not chunk:
            break
        all_klines.extend(chunk)
        # Move cursor past the last candle we received
        last_close_time = chunk[-1]["close_time"]
        if last_close_time >= end_ms:
            break
        cursor = last_close_time + 1
        # Rate limiting: be polite to public API
        time.sleep(0.2)

    # Remove duplicates (overlapping boundaries) and sort
    seen: set[int] = set()
    deduped: list[dict] = []
    for k in all_klines:
        if k["open_time"] not in seen:
            seen.add(k["open_time"])
            deduped.append(k)
    deduped.sort(key=lambda x: x["open_time"])

    # Filter to requested range
    deduped = [k for k in deduped if k["open_time"] >= start_ms and k["open_time"] <= end_ms]

    # Cache for next time
    if deduped:
        _write_cache(cache_file, deduped)

    return deduped


# ---------------------------------------------------------------------------
# Futures klines loader (Binance Futures — fapi endpoint)
# ---------------------------------------------------------------------------

_FUTURES_BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
_FUTURES_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "futures"


def _futures_cache_path(symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    """Return the local CSV cache file path for futures kline data."""
    _FUTURES_DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.upper().replace("/", "_")
    return _FUTURES_DATA_DIR / f"{safe_symbol}_{interval}_{start_ms}_{end_ms}.csv"


def _fetch_futures_klines_chunk(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch a single chunk of futures klines from Binance (max 1000 per request)."""
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair = pair + "USDT"

    url = (
        f"{_FUTURES_BASE_URL}?symbol={pair}&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={_MAX_CANDLES_PER_REQUEST}"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        return [_parse_kline(row) for row in data]
    except (URLError, Exception) as e:
        print(f"  WARNING: futures klines fetch failed for {symbol}: {e}")
        return []


def load_futures_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Load historical futures kline/candlestick data for a symbol.

    Fetches from Binance Futures API (fapi) and caches results to local CSV.
    No authentication required.

    Args:
        symbol: Trading pair symbol (e.g. "BTC", "ETHUSDT", "BTC-USDT")
        interval: Candle interval - one of "1m", "5m", "15m", "1h", "4h", "1d"
        start_ms: Start time in milliseconds since epoch
        end_ms: End time in milliseconds since epoch

    Returns:
        List of dicts with keys: open_time, open, high, low, close, volume, close_time
    """
    if interval not in _VALID_INTERVALS:
        raise ValueError(f"Invalid interval '{interval}'. Must be one of {_VALID_INTERVALS}")

    cache_file = _futures_cache_path(symbol, interval, start_ms, end_ms)
    cached = _read_cache(cache_file)
    if cached is not None:
        return cached

    interval_ms = _INTERVAL_MS[interval]
    all_klines: list[dict] = []
    cursor = start_ms

    while cursor < end_ms:
        chunk_end = min(cursor + _MAX_CANDLES_PER_REQUEST * interval_ms, end_ms)
        chunk = _fetch_futures_klines_chunk(symbol, interval, cursor, chunk_end)
        if not chunk:
            break
        all_klines.extend(chunk)
        last_close_time = chunk[-1]["close_time"]
        if last_close_time >= end_ms:
            break
        cursor = last_close_time + 1
        # Rate limiting: be polite to public API
        time.sleep(0.2)

    # Remove duplicates and sort
    seen: set[int] = set()
    deduped: list[dict] = []
    for k in all_klines:
        if k["open_time"] not in seen:
            seen.add(k["open_time"])
            deduped.append(k)
    deduped.sort(key=lambda x: x["open_time"])

    # Filter to requested range
    deduped = [k for k in deduped if k["open_time"] >= start_ms and k["open_time"] <= end_ms]

    # Cache for next time
    if deduped:
        _write_cache(cache_file, deduped)

    return deduped
