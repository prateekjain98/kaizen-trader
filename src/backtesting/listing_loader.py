"""Exchange listing date loader — builds a historical listing dataset from multiple free sources.

Data sources (all free, no auth):
1. Binance Futures exchangeInfo — onboardDate field gives exact listing timestamps for 500+ symbols
2. Binance Spot kline probe — first available candle date = spot listing date
3. Derived from kline gaps — sudden appearance of a symbol in kline data = new listing

The loader builds a CSV dataset: symbol, exchange, listing_date_ms, listing_type
"""

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "listings"
_CACHE_FILE = _DATA_DIR / "exchange_listings.csv"


def _fetch_binance_futures_listings() -> list[dict]:
    """Fetch all Binance Futures perpetual listing dates from exchangeInfo.

    The onboardDate field gives the exact timestamp when each symbol went live.
    This is the most reliable free source of listing dates.
    """
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  WARNING: Binance Futures exchangeInfo failed: {e}")
        return []

    listings = []
    for sym_info in data.get("symbols", []):
        obd = sym_info.get("onboardDate", 0)
        symbol = sym_info.get("symbol", "")
        if not obd or not symbol.endswith("USDT"):
            continue
        base = symbol.replace("USDT", "")
        # Skip non-crypto pairs (stock tokens, commodities)
        if base in ("AAPL", "TSLA", "AMZN", "GOOG", "MSFT", "NVDA", "META",
                     "TSM", "QQQ", "SPY", "CL", "BZ", "NATGAS", "XAU"):
            continue
        listings.append({
            "symbol": base,
            "exchange": "binance_futures",
            "listing_date_ms": int(obd),
            "listing_type": "futures_perpetual",
        })
    return listings


def _probe_binance_spot_listing_date(symbol: str) -> Optional[int]:
    """Find the first available 1d candle for a symbol on Binance Spot.

    The first candle's open_time IS the spot listing date.
    Uses a single API call with startTime=0 to get the earliest available data.
    """
    pair = symbol.upper().replace("-", "").replace("/", "")
    if not pair.endswith("USDT"):
        pair += "USDT"

    url = (
        f"https://data-api.binance.vision/api/v3/klines"
        f"?symbol={pair}&interval=1d&startTime=0&limit=1"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data:
            return int(data[0][0])  # open_time of first ever candle
    except Exception:
        pass
    return None


def _fetch_binance_spot_listings(symbols: list[str]) -> list[dict]:
    """Probe Binance Spot for listing dates of given symbols.

    Each call fetches the first available candle (startTime=0, limit=1).
    Rate limited to be polite.
    """
    listings = []
    for i, sym in enumerate(symbols):
        listing_ms = _probe_binance_spot_listing_date(sym)
        if listing_ms:
            listings.append({
                "symbol": sym,
                "exchange": "binance_spot",
                "listing_date_ms": listing_ms,
                "listing_type": "spot",
            })
        if (i + 1) % 10 == 0:
            print(f"    Probed {i + 1}/{len(symbols)} spot symbols...")
        time.sleep(0.15)  # rate limit
    return listings


def _read_cache() -> Optional[list[dict]]:
    """Read cached listing data."""
    if not _CACHE_FILE.exists():
        return None
    rows = []
    with open(_CACHE_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "symbol": row["symbol"],
                "exchange": row["exchange"],
                "listing_date_ms": int(row["listing_date_ms"]),
                "listing_type": row["listing_type"],
            })
    return rows if rows else None


def _write_cache(records: list[dict]) -> None:
    """Write listing data to CSV cache."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["symbol", "exchange", "listing_date_ms", "listing_type"]
    with open(_CACHE_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def load_exchange_listings(
    symbols: Optional[list[str]] = None,
    force_refresh: bool = False,
) -> list[dict]:
    """Load exchange listing dates from all available sources.

    Returns list of dicts with: symbol, exchange, listing_date_ms, listing_type

    Args:
        symbols: Optional list of symbols to probe for spot listing dates.
                 If None, only returns Binance Futures data (fast).
        force_refresh: If True, ignores cache and re-fetches from APIs.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached

    print("  Fetching Binance Futures listing dates...")
    all_listings = _fetch_binance_futures_listings()
    print(f"    Got {len(all_listings)} futures listings")

    # Also probe spot listing dates if symbols provided
    if symbols:
        print(f"  Probing Binance Spot listing dates for {len(symbols)} symbols...")
        spot = _fetch_binance_spot_listings(symbols)
        print(f"    Got {len(spot)} spot listings")
        all_listings.extend(spot)

    # Deduplicate: keep earliest listing per (symbol, exchange)
    seen = {}
    for rec in all_listings:
        key = (rec["symbol"], rec["exchange"])
        if key not in seen or rec["listing_date_ms"] < seen[key]["listing_date_ms"]:
            seen[key] = rec
    deduped = sorted(seen.values(), key=lambda x: x["listing_date_ms"])

    if deduped:
        _write_cache(deduped)

    return deduped


def get_listing_events_in_range(
    listings: list[dict],
    start_ms: int,
    end_ms: int,
    exchange: Optional[str] = None,
) -> list[dict]:
    """Filter listing events within a date range.

    Returns listings sorted by date, optionally filtered by exchange.
    """
    filtered = [
        r for r in listings
        if start_ms <= r["listing_date_ms"] <= end_ms
        and (exchange is None or r["exchange"] == exchange)
    ]
    return sorted(filtered, key=lambda x: x["listing_date_ms"])


def score_listing_quality(
    symbol: str,
    exchange: str,
    listings: list[dict],
) -> float:
    """Score a listing event's quality based on available signals.

    Higher score = more likely to produce a profitable listing pump.
    Factors:
    - Exchange tier (Binance > others)
    - Is this the FIRST major exchange listing? (higher alpha)
    - Futures listing before spot (signals exchange confidence)

    Returns score 0-100.
    """
    # Get all listings for this symbol
    sym_listings = [r for r in listings if r["symbol"] == symbol]
    sym_listings.sort(key=lambda x: x["listing_date_ms"])

    score = 50.0  # base

    # Exchange tier bonus
    if "binance" in exchange:
        score += 15
    elif "coinbase" in exchange:
        score += 12

    # First major exchange listing = highest alpha
    if sym_listings and sym_listings[0]["symbol"] == symbol:
        is_first = True
        for prev in sym_listings:
            if prev["exchange"] != exchange and prev["listing_date_ms"] < \
               next((r["listing_date_ms"] for r in sym_listings
                     if r["exchange"] == exchange), float("inf")):
                is_first = False
                break
        if is_first:
            score += 20

    # Futures listing exists = exchange did due diligence
    has_futures = any(r["listing_type"] == "futures_perpetual" for r in sym_listings)
    if has_futures:
        score += 10

    return min(95, score)
