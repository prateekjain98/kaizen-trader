"""Historical liquidation data loader — fetches from Bitfinex public API
with local CSV caching, and a CSV reader for forward-collected Binance
WS data (see scripts/collect_liquidations.py).

WHY BITFINEX:
- Coinglass /public/v2/liquidation_ex requires an API key (free tier
  exists but rate-limited to 30/min and email signup needed) — not
  drop-in usable.
- OKX /api/v5/public/liquidation-orders is free but timed out repeatedly
  during research from this environment; not reliable as primary source.
- Bybit recent-trade does NOT expose liquidation flag in v5 public data.
- Bitfinex /v2/liquidations/hist is FREE, NO AUTH, supports start/end/limit
  query params, returns ≥1 year of historical data verified against live
  queries at 7d / 30d / 90d / 365d windows.

CAVEAT: Bitfinex liquidations cover BTC/ETH/LTC/SUI/PEPE/LDO/etc on the
*Bitfinex* venue only — single-exchange, not cross-exchange aggregate.
This is the best free historical proxy available; for true cross-exchange
aggregate liquidations we'd need a paid Coinglass/Glassnode subscription,
or to forward-collect Binance WS live (see scripts/collect_liquidations.py).

AUDIT-LESSON LAG:
- Per audit pattern (FGI, stable_flow): callers should LAG historical
  lookups by ≥1 day to avoid same-bar lookahead. This loader returns
  raw timestamped events; the replay layer is responsible for applying
  the lag.

Bitfinex schema for /v2/liquidations/hist
  [POS_ID, MTS, _, SYMBOL, AMOUNT, BASE_PRICE, _, IS_MATCH, IS_MARKET_SOLD,
   _, PRICE_ACQUIRED]
  AMOUNT < 0  → long position liquidated (forced sell)
  AMOUNT > 0  → short position liquidated (forced buy)
  Two records emit per liquidation; IS_MATCH=1 is the filled match — we
  keep only those for accurate $-notional and to avoid double counting.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data" / "liquidations"
_FORWARD_DIR = _DATA_DIR / "forward"  # populated by scripts/collect_liquidations.py
_BITFINEX_URL = "https://api-pub.bitfinex.com/v2/liquidations/hist"
_MAX_PER_REQUEST = 500
_ONE_MS = 1
_DAY_MS = 86_400_000
_DEFAULT_LAG_MS = _DAY_MS  # audit lesson — never read same-day data


def _cache_path(start_ms: int, end_ms: int) -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR / f"bitfinex_liq_{start_ms}_{end_ms}.csv"


def _normalise_symbol(bfx_sym: str) -> Optional[str]:
    """Map Bitfinex perp symbols (tBTCF0:USTF0) → kaizen short ticker (BTC).

    Spot-style symbols (tBTCUSD) we also accept and strip the leading 't'
    + trailing 'USD' / 'UST'. Returns None if the symbol cannot be mapped.
    """
    if not bfx_sym or not bfx_sym.startswith("t"):
        return None
    s = bfx_sym[1:]
    # Perp: BASE F0:QUOTE F0
    if "F0:" in s:
        base = s.split("F0:")[0]
        if base:
            return base.upper()
    # Spot: BASEUSD or BASEUST
    for suf in ("USDT", "UST", "USD"):
        if s.endswith(suf):
            base = s[: -len(suf)]
            if base:
                return base.upper()
    return None


def _parse_record(raw: list) -> Optional[dict]:
    """Parse a single Bitfinex liquidation row to a normalised event dict."""
    if not isinstance(raw, list) or len(raw) < 12:
        return None
    try:
        # raw is [TYPE, POS_ID, MTS, _, SYMBOL, AMOUNT, BASE_PRICE, _,
        #        IS_MATCH, IS_MARKET_SOLD, _, PRICE_ACQUIRED]
        # The outer payload sometimes wraps each record in another list.
        ts = int(raw[2])
        sym_raw = raw[4]
        amount = float(raw[5])
        base_price = float(raw[6] or 0.0)
        is_match = int(raw[8] or 0)
        price_acq = raw[11]
        if amount == 0 or base_price <= 0:
            return None
        # Keep only the matched fill record (IS_MATCH=1) to avoid double counting.
        if is_match != 1:
            return None
        sym = _normalise_symbol(sym_raw)
        if sym is None:
            return None
        # AMOUNT < 0 = long liquidated (forced sell). AMOUNT > 0 = short liq'd.
        side = "long" if amount < 0 else "short"
        usd = abs(amount) * base_price
        return {
            "timestamp": ts,
            "symbol": sym,
            "side": side,
            "size_usd": usd,
            "price": base_price,
            "price_acquired": float(price_acq) if price_acq is not None else 0.0,
        }
    except (TypeError, ValueError, IndexError):
        return None


def _read_cache(path: Path) -> Optional[list[dict]]:
    if not path.exists():
        return None
    rows: list[dict] = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "timestamp": int(row["timestamp"]),
                "symbol": row["symbol"],
                "side": row["side"],
                "size_usd": float(row["size_usd"]),
                "price": float(row["price"]),
                "price_acquired": float(row.get("price_acquired") or 0.0),
            })
    return rows


def _write_cache(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["timestamp", "symbol", "side", "size_usd", "price", "price_acquired"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(records)


def _fetch_chunk(start_ms: int, end_ms: int) -> list[dict]:
    """One Bitfinex page (≤500 rows, ascending by MTS)."""
    url = (
        f"{_BITFINEX_URL}?start={start_ms}&end={end_ms}"
        f"&limit={_MAX_PER_REQUEST}&sort=1"
    )
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (URLError, Exception) as e:
        print(f"  WARNING: Bitfinex liquidation fetch failed: {e}")
        return []

    out: list[dict] = []
    # Bitfinex sometimes wraps each row in an extra list — flatten one level.
    for row in data or []:
        if isinstance(row, list) and row and isinstance(row[0], list):
            for inner in row:
                rec = _parse_record(inner)
                if rec:
                    out.append(rec)
        else:
            rec = _parse_record(row)
            if rec:
                out.append(rec)
    return out


def load_liquidations(
    start_ms: int,
    end_ms: int,
    symbols: Optional[list[str]] = None,
    lag_ms: int = _DEFAULT_LAG_MS,
) -> list[dict]:
    """Load historical liquidations from Bitfinex (single-exchange).

    Args:
        start_ms: window start (ms since epoch). Lag is applied AFTER fetch.
        end_ms: window end (ms since epoch). Records with ts > (end_ms - lag_ms)
                are dropped to avoid same-day lookahead.
        symbols: optional filter to short tickers ('BTC', 'ETH', ...).
        lag_ms: audit-mandated minimum lag. Default 1 day.

    Returns:
        Time-ordered list of dicts:
          {timestamp, symbol, side ('long'|'short'), size_usd, price,
           price_acquired}
    """
    cache_file = _cache_path(start_ms, end_ms)
    cached = _read_cache(cache_file)
    if cached is None:
        all_records: list[dict] = []
        cursor = start_ms
        while cursor < end_ms:
            chunk = _fetch_chunk(cursor, end_ms)
            if not chunk:
                break
            all_records.extend(chunk)
            last_ts = chunk[-1]["timestamp"]
            if last_ts >= end_ms or len(chunk) < 1:
                break
            cursor = last_ts + _ONE_MS
            time.sleep(0.25)  # be polite to public API
        # dedupe by (ts, symbol, size_usd) — IS_MATCH filter usually suffices
        seen: set[tuple] = set()
        deduped: list[dict] = []
        for r in all_records:
            key = (r["timestamp"], r["symbol"], round(r["size_usd"], 2))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        deduped.sort(key=lambda r: r["timestamp"])
        if deduped:
            _write_cache(cache_file, deduped)
        cached = deduped

    cutoff = end_ms - max(0, lag_ms)
    out = [r for r in cached if start_ms <= r["timestamp"] <= cutoff]
    if symbols:
        sset = {s.upper() for s in symbols}
        out = [r for r in out if r["symbol"] in sset]
    return out


def load_forward_collected(
    start_ms: int,
    end_ms: int,
    symbols: Optional[list[str]] = None,
    lag_ms: int = _DEFAULT_LAG_MS,
    data_dir: Optional[Path] = None,
) -> list[dict]:
    """Read liquidations forward-collected from Binance WS (cross-exchange
    is closer if we ever extend the collector to multiple venues; today
    this is Binance-only, but it's the live signal source so it's exact).

    File layout: data/liquidations/forward/<YYYY-MM-DD>.csv with header
    `timestamp,symbol,side,size_usd,price`.
    """
    base = data_dir or _FORWARD_DIR
    if not base.exists():
        return []
    out: list[dict] = []
    sset = {s.upper() for s in symbols} if symbols else None
    for csv_path in sorted(base.glob("*.csv")):
        try:
            with open(csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        ts = int(row["timestamp"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if ts < start_ms or ts > end_ms:
                        continue
                    sym = (row.get("symbol") or "").upper()
                    if sset is not None and sym not in sset:
                        continue
                    out.append({
                        "timestamp": ts,
                        "symbol": sym,
                        "side": row.get("side", ""),
                        "size_usd": float(row.get("size_usd") or 0.0),
                        "price": float(row.get("price") or 0.0),
                    })
        except OSError:
            continue
    cutoff = end_ms - max(0, lag_ms)
    out = [r for r in out if r["timestamp"] <= cutoff]
    out.sort(key=lambda r: r["timestamp"])
    return out


def aggregate_5m_window(
    events: list[dict],
    symbol: str,
    end_ms: int,
    window_ms: int = 5 * 60_000,
) -> dict:
    """Sum liquidation $-notional by side over [end_ms - window_ms, end_ms].
    Mirrors LiquidationTracker.cascade_score shape so the replay layer can
    feed it straight into the same downstream filters.
    """
    sym = symbol.upper()
    cutoff = end_ms - window_ms
    long_usd = 0.0
    short_usd = 0.0
    count = 0
    for ev in events:
        if ev["symbol"] != sym:
            continue
        ts = ev["timestamp"]
        if ts < cutoff or ts > end_ms:
            continue
        count += 1
        if ev["side"] == "long":
            long_usd += ev["size_usd"]
        else:
            short_usd += ev["size_usd"]
    dominant = None
    ratio = 1.0
    if long_usd > 0 or short_usd > 0:
        if long_usd > short_usd:
            dominant, ratio = "long", long_usd / max(short_usd, 1.0)
        else:
            dominant, ratio = "short", short_usd / max(long_usd, 1.0)
    return {
        "long_liq_usd_5m": long_usd,
        "short_liq_usd_5m": short_usd,
        "dominant_side": dominant,
        "imbalance_ratio": ratio,
        "count": count,
    }
