"""Cross-exchange funding-rate aggregator -- Binance / Bybit / OKX / Hyperliquid.

All public free endpoints, stdlib urllib only. Rates normalised to 8h-equivalent
so spreads across venues with different funding intervals are comparable.
"""

import csv
import json
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cross_funding"
_TIMEOUT = 15
_UA = {"User-Agent": "kaizen-trader-backtest/1.0"}

# Funding interval per venue, in hours. Used to normalise to 8h-equivalent.
# Binance: 8h. Bybit: usually 8h (some perps 4h). OKX: 8h. Hyperliquid: 1h.
_INTERVAL_H = {"binance": 8, "bybit": 8, "okx": 8, "hyperliquid": 1}


def _http_get(url: str) -> Optional[dict]:
    try:
        with urlopen(Request(url, headers=_UA), timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (URLError, Exception) as e:
        print(f"  WARN GET {url[:60]}: {e}")
        return None


def _http_post(url: str, body: dict) -> Optional[dict]:
    try:
        data = json.dumps(body).encode()
        req = Request(
            url,
            data=data,
            headers={**_UA, "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (URLError, Exception) as e:
        print(f"  WARN POST {url[:60]}: {e}")
        return None


def _to_8h(rate: float, venue: str) -> float:
    """Normalise a per-period funding rate to per-8h."""
    return rate * (8.0 / _INTERVAL_H.get(venue, 8))


# ---------- per-venue spot fetchers ----------

def _binance_rate(symbol: str) -> Optional[float]:
    pair = symbol.upper() + "USDT"
    data = _http_get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={pair}")
    if not data or "lastFundingRate" not in data:
        return None
    return _to_8h(float(data["lastFundingRate"]), "binance")


def _bybit_rate(symbol: str) -> Optional[float]:
    pair = symbol.upper() + "USDT"
    data = _http_get(
        f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={pair}"
    )
    if not data or data.get("retCode") != 0:
        return None
    items = data.get("result", {}).get("list") or []
    if not items:
        return None
    rate = items[0].get("fundingRate")
    if rate in (None, ""):
        return None
    return _to_8h(float(rate), "bybit")


def _okx_rate(symbol: str) -> Optional[float]:
    inst = f"{symbol.upper()}-USDT-SWAP"
    data = _http_get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")
    if not data or data.get("code") != "0":
        return None
    items = data.get("data") or []
    if not items:
        return None
    rate = items[0].get("fundingRate")
    if rate in (None, ""):
        return None
    return _to_8h(float(rate), "okx")


def _hyperliquid_rate(symbol: str) -> Optional[float]:
    data = _http_post(
        "https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"}
    )
    if not isinstance(data, list) or len(data) != 2:
        return None
    universe = data[0].get("universe") or []
    ctxs = data[1] or []
    sym = symbol.upper()
    for i, asset in enumerate(universe):
        if asset.get("name", "").upper() == sym and i < len(ctxs):
            rate = ctxs[i].get("funding")
            if rate in (None, ""):
                return None
            return _to_8h(float(rate), "hyperliquid")
    return None


def fetch_cross_funding(symbol: str) -> dict:
    """Return {exchange: rate_per_8h} for all 4 venues. Missing venues omitted."""
    out: dict = {}
    for name, fn in (
        ("binance", _binance_rate),
        ("bybit", _bybit_rate),
        ("okx", _okx_rate),
        ("hyperliquid", _hyperliquid_rate),
    ):
        r = fn(symbol)
        if r is not None:
            out[name] = r
    return out


def find_spread_events(
    symbol_universe: list[str], min_spread: float = 0.0005
) -> list[dict]:
    """Scan symbols, return events where max(rate)-min(rate) >= min_spread (per 8h)."""
    events: list[dict] = []
    for sym in symbol_universe:
        rates = fetch_cross_funding(sym)
        if len(rates) < 2:
            continue
        hi_v, hi = max(rates.items(), key=lambda kv: kv[1])
        lo_v, lo = min(rates.items(), key=lambda kv: kv[1])
        spread = hi - lo
        if spread >= min_spread:
            events.append({
                "symbol": sym,
                "spread": spread,
                "long_venue": lo_v,   # pay funding low / receive on short side
                "short_venue": hi_v,
                "rates": rates,
            })
    events.sort(key=lambda e: e["spread"], reverse=True)
    return events


# ---------- historical loader ----------

def _cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.upper().replace("/", "_").replace("-", "")
    return _DATA_DIR / f"{safe}_cross_{start_ms}_{end_ms}.csv"


def _binance_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    pair = symbol.upper() + "USDT"
    url = (
        f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={pair}"
        f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
    )
    data = _http_get(url) or []
    return [
        {"venue": "binance", "ts": int(r["fundingTime"]),
         "rate": _to_8h(float(r["fundingRate"]), "binance")}
        for r in data
    ]


def _bybit_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    pair = symbol.upper() + "USDT"
    url = (
        f"https://api.bybit.com/v5/market/funding/history?category=linear"
        f"&symbol={pair}&startTime={start_ms}&endTime={end_ms}&limit=200"
    )
    data = _http_get(url) or {}
    items = data.get("result", {}).get("list") or []
    return [
        {"venue": "bybit", "ts": int(r["fundingRateTimestamp"]),
         "rate": _to_8h(float(r["fundingRate"]), "bybit")}
        for r in items
    ]


def _okx_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    inst = f"{symbol.upper()}-USDT-SWAP"
    url = (
        f"https://www.okx.com/api/v5/public/funding-rate-history?instId={inst}"
        f"&before={start_ms}&after={end_ms}&limit=100"
    )
    data = _http_get(url) or {}
    items = data.get("data") or []
    return [
        {"venue": "okx", "ts": int(r["fundingTime"]),
         "rate": _to_8h(float(r["fundingRate"]), "okx")}
        for r in items
    ]


def load_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Historical funding rows {venue, ts, rate(8h-norm)} from venues that expose it.

    Hyperliquid history isn't exposed via a simple free endpoint, so it's omitted.
    Cached per (symbol, range) to data/cross_funding/.
    """
    cache = _cache_path(symbol, start_ms, end_ms)
    if cache.exists():
        with open(cache, "r", newline="") as f:
            return [
                {"venue": r["venue"], "ts": int(r["ts"]), "rate": float(r["rate"])}
                for r in csv.DictReader(f)
            ]
    rows: list[dict] = []
    for fn in (_binance_history, _bybit_history, _okx_history):
        rows.extend(fn(symbol, start_ms, end_ms))
    rows.sort(key=lambda r: (r["ts"], r["venue"]))
    if rows:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["venue", "ts", "rate"])
            w.writeheader()
            w.writerows(rows)
    return rows
