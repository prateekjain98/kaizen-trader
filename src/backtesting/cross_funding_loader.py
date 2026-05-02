"""Cross-exchange funding aggregator -- Binance/Bybit/OKX/Hyperliquid.

Public free endpoints, stdlib urllib only. Rates normalised to per-8h
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
# Funding interval per venue (hours). Bybit can be 4h on some perps; we assume 8h.
_INTERVAL_H = {"binance": 8, "bybit": 8, "okx": 8, "hyperliquid": 1}


def _http(url: str, body: Optional[dict] = None) -> Optional[dict]:
    try:
        headers = dict(_UA)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(url, data=data, headers=headers,
                      method="POST" if body is not None else "GET")
        with urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (URLError, Exception) as e:
        print(f"  WARN {url[:60]}: {e}")
        return None


def _to_8h(rate: float, venue: str) -> float:
    return rate * (8.0 / _INTERVAL_H.get(venue, 8))


def _binance_rate(sym: str) -> Optional[float]:
    d = _http(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}USDT")
    return _to_8h(float(d["lastFundingRate"]), "binance") if d and "lastFundingRate" in d else None


def _bybit_rate(sym: str) -> Optional[float]:
    d = _http(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}USDT")
    if not d or d.get("retCode") != 0:
        return None
    items = d.get("result", {}).get("list") or []
    if not items or items[0].get("fundingRate") in (None, ""):
        return None
    return _to_8h(float(items[0]["fundingRate"]), "bybit")


def _okx_rate(sym: str) -> Optional[float]:
    d = _http(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}-USDT-SWAP")
    if not d or d.get("code") != "0":
        return None
    items = d.get("data") or []
    if not items or items[0].get("fundingRate") in (None, ""):
        return None
    return _to_8h(float(items[0]["fundingRate"]), "okx")


_HL_CACHE: dict = {}


def _hyperliquid_rate(sym: str) -> Optional[float]:
    if not _HL_CACHE:
        d = _http("https://api.hyperliquid.xyz/info", {"type": "metaAndAssetCtxs"})
        if not isinstance(d, list) or len(d) != 2:
            return None
        universe, ctxs = d[0].get("universe") or [], d[1] or []
        for i, asset in enumerate(universe):
            if i >= len(ctxs):
                break
            r = ctxs[i].get("funding")
            if r not in (None, ""):
                _HL_CACHE[asset.get("name", "").upper()] = _to_8h(float(r), "hyperliquid")
    return _HL_CACHE.get(sym.upper())


_FETCHERS = (("binance", _binance_rate), ("bybit", _bybit_rate),
             ("okx", _okx_rate), ("hyperliquid", _hyperliquid_rate))


def fetch_cross_funding(symbol: str) -> dict:
    """Return {exchange: rate_per_8h} for the 4 venues. Missing venues omitted."""
    sym = symbol.upper()
    out: dict = {}
    for name, fn in _FETCHERS:
        r = fn(sym)
        if r is not None:
            out[name] = r
    return out


def find_spread_events(symbol_universe: list[str], min_spread: float = 0.0005) -> list[dict]:
    """Symbols where max(rate) - min(rate) >= min_spread (per 8h), sorted desc."""
    events: list[dict] = []
    for sym in symbol_universe:
        rates = fetch_cross_funding(sym)
        if len(rates) < 2:
            continue
        hi_v, hi = max(rates.items(), key=lambda kv: kv[1])
        lo_v, lo = min(rates.items(), key=lambda kv: kv[1])
        if hi - lo >= min_spread:
            events.append({"symbol": sym, "spread": hi - lo,
                           "long_venue": lo_v, "short_venue": hi_v, "rates": rates})
    events.sort(key=lambda e: e["spread"], reverse=True)
    return events


# ---------- historical ----------

def _cache_path(symbol: str, start_ms: int, end_ms: int) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe = symbol.upper().replace("/", "_").replace("-", "")
    return _DATA_DIR / f"{safe}_cross_{start_ms}_{end_ms}.csv"


def _binance_hist(sym: str, s: int, e: int) -> list[dict]:
    d = _http(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}USDT"
              f"&startTime={s}&endTime={e}&limit=1000") or []
    return [{"venue": "binance", "ts": int(r["fundingTime"]),
             "rate": _to_8h(float(r["fundingRate"]), "binance")} for r in d]


def _bybit_hist(sym: str, s: int, e: int) -> list[dict]:
    d = _http(f"https://api.bybit.com/v5/market/funding/history?category=linear"
              f"&symbol={sym}USDT&startTime={s}&endTime={e}&limit=200") or {}
    items = d.get("result", {}).get("list") or []
    return [{"venue": "bybit", "ts": int(r["fundingRateTimestamp"]),
             "rate": _to_8h(float(r["fundingRate"]), "bybit")} for r in items]


def _okx_hist(sym: str, s: int, e: int) -> list[dict]:
    d = _http(f"https://www.okx.com/api/v5/public/funding-rate-history"
              f"?instId={sym}-USDT-SWAP&before={s}&after={e}&limit=100") or {}
    items = d.get("data") or []
    return [{"venue": "okx", "ts": int(r["fundingTime"]),
             "rate": _to_8h(float(r["fundingRate"]), "okx")} for r in items]


def load_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Historical {venue, ts, rate(8h-norm)} from venues that expose it.
    Hyperliquid history isn't on a free endpoint, so it's omitted.
    Cached to data/cross_funding/.
    """
    cache = _cache_path(symbol, start_ms, end_ms)
    if cache.exists():
        with open(cache, "r", newline="") as f:
            return [{"venue": r["venue"], "ts": int(r["ts"]), "rate": float(r["rate"])}
                    for r in csv.DictReader(f)]
    sym = symbol.upper()
    rows: list[dict] = []
    for fn in (_binance_hist, _bybit_hist, _okx_hist):
        rows.extend(fn(sym, start_ms, end_ms))
    rows.sort(key=lambda r: (r["ts"], r["venue"]))
    if rows:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["venue", "ts", "rate"])
            w.writeheader()
            w.writerows(rows)
    return rows
