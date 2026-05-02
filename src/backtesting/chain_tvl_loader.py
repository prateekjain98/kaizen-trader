"""Chain-level TVL flow loader — DefiLlama historicalChainTvl per chain.

Edge thesis: net TVL flow per ecosystem leads price action by 12-48h.
When Solana TVL rips +5%/24h while uptrending 7d, capital is rotating in
→ SOL/JUP/ORCA bullish. When Ethereum TVL drains in concert with a 7d
downtrend, the ecosystem is risk-off → ETH/UNI/AAVE bearish.

Endpoint: https://api.llama.fi/v2/historicalChainTvl/<Chain>
Returns daily TVL series back to 2019. No auth.

Mirrors stablecoin_loader.py structurally: stdlib urllib only, JSON
cache per chain, 12h TTL. Net 24h / 7d % changes computed locally
(audit lesson — never trust API derived fields).

Audit lesson C2 (also applied in stablecoin_loader): the day-N TVL
snapshot is end-of-day-N, only knowable AFTER day-N. Caller MUST
1-day-lag the lookup (i.e. query t-86400000 not t) — this loader does
NOT lag for you, the consumer is responsible (mirrors stable_flow).
"""

import json
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "chain_tvl"
_API_URL_TMPL = "https://api.llama.fi/v2/historicalChainTvl/{chain}"
_CACHE_MAX_AGE_S = 12 * 3600  # 12 hours

_DAY_MS = 86_400_000


def _cache_file(chain: str) -> Path:
    safe = chain.strip().replace("/", "_")
    return _DATA_DIR / f"{safe}.json"


def _read_cache(chain: str) -> Optional[list[dict]]:
    f = _cache_file(chain)
    if not f.exists():
        return None
    try:
        with open(f, "r") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            return None
        out = []
        for r in data:
            out.append({
                "date_ms": int(r["date_ms"]),
                "tvl_usd": float(r["tvl_usd"]),
                "net_24h_change_pct": float(r["net_24h_change_pct"]),
                "net_7d_change_pct": float(r["net_7d_change_pct"]),
            })
        return out or None
    except Exception:
        return None


def _write_cache(chain: str, records: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_file(chain), "w") as f:
        json.dump(records, f)


def _cache_fresh(chain: str) -> bool:
    f = _cache_file(chain)
    if not f.exists():
        return False
    return (time.time() - f.stat().st_mtime) < _CACHE_MAX_AGE_S


def load_chain_tvl_history(chain: str, force_refresh: bool = False) -> list[dict]:
    """Load daily TVL history for a chain (e.g. 'Ethereum', 'Solana', 'Base', 'Arbitrum').

    Returns list of dicts sorted by date_ms ascending:
        date_ms: int  (epoch ms; DefiLlama ts is UTC midnight, end-of-day snapshot)
        tvl_usd: float
        net_24h_change_pct: float  (locally computed; (tvl[i]-tvl[i-1])/tvl[i-1]*100)
        net_7d_change_pct:  float  ((tvl[i]-tvl[i-7])/tvl[i-7]*100)

    Cached for 12h at data/chain_tvl/<chain>.json. Returns [] on hard failure.
    """
    if not force_refresh and _cache_fresh(chain):
        cached = _read_cache(chain)
        if cached:
            return cached

    url = _API_URL_TMPL.format(chain=chain)
    req = Request(url, headers={"User-Agent": "kaizen-trader-backtest/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  WARNING: chain TVL API failed for {chain}: {e}")
        cached = _read_cache(chain)
        return cached or []

    if not isinstance(data, list):
        return _read_cache(chain) or []

    raw: list[tuple[int, float]] = []
    for entry in data:
        try:
            ts_s = int(entry.get("date", 0))
            tvl = float(entry.get("tvl", 0.0))
        except (TypeError, ValueError):
            continue
        if ts_s <= 0 or tvl <= 0:
            continue
        raw.append((ts_s * 1000, tvl))
    raw.sort(key=lambda x: x[0])

    records: list[dict] = []
    for i, (ts_ms, tvl) in enumerate(raw):
        if i >= 1 and raw[i - 1][1] > 0:
            net24 = (tvl - raw[i - 1][1]) / raw[i - 1][1] * 100.0
        else:
            net24 = 0.0
        if i >= 7 and raw[i - 7][1] > 0:
            net7 = (tvl - raw[i - 7][1]) / raw[i - 7][1] * 100.0
        else:
            net7 = 0.0
        records.append({
            "date_ms": ts_ms,
            "tvl_usd": tvl,
            "net_24h_change_pct": net24,
            "net_7d_change_pct": net7,
        })

    if records:
        _write_cache(chain, records)
        print(f"  Chain TVL ({chain}): {len(records)} days "
              f"(${records[0]['tvl_usd']/1e9:.1f}B → "
              f"${records[-1]['tvl_usd']/1e9:.1f}B)")
    return records


def get_chain_tvl_at_timestamp(
    history: list[dict], ts_ms: float
) -> Optional[dict]:
    """Binary-search lookup of the row at-or-before ts_ms. None if before data."""
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


# Chain → symbol mapping. Base intentionally omitted (no clean BASE-specific
# token in our universe — Aerodrome/cbBTC etc not in tradable set).
CHAIN_SYMBOL_MAP: dict[str, tuple[str, ...]] = {
    "Ethereum": ("ETH", "UNI", "AAVE", "COMP", "LDO", "LINK", "MKR"),
    "Solana":   ("SOL", "JUP", "JTO", "WIF", "BONK", "ORCA", "RAY"),
    "Arbitrum": ("ARB",),
}
