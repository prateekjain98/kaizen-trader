"""Real-time data ingestion from all free APIs.

Streams:
    1. Binance WebSocket — price, volume, order book, funding (real-time)
    2. CoinGecko trending — hottest tokens (every 10 min)
    3. DexScreener — DEX volume spikes, new pairs (every 5 min)
    4. Alternative.me — Fear & Greed Index (every 1 hour)
    5. Binance announcements — new listings (every 1 min)
    6. Binance funding rates — extreme funding (every 1 min)
    7. Coinbase products — new Coinbase listings (every 1 min)

All free, no auth required (except Binance WS for authenticated channels).
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

from src.engine.log import log


# ---------------------------------------------------------------------------
# Cross-sectional funding-carry constants (mirror funding_carry_loader.py)
# ---------------------------------------------------------------------------

# Liquid universe — must match rule_brain._CARRY_LIQUID_UNIVERSE so the
# brain actually scores what we emit. Hard-coded rather than imported to
# avoid a circular dep (rule_brain imports SignalPacket from signal_detector
# which imports from data_streams).
_CARRY_LIQUID_UNIVERSE = frozenset({
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK", "MATIC",
    "DOT", "UNI", "ATOM", "NEAR", "AAVE", "COMP", "LTC", "ADA", "TRX",
    "OP", "ARB", "INJ", "SUI", "APT", "FIL", "TIA", "SEI", "STX",
})

# 8h funding boundaries on Binance: 00:00, 08:00, 16:00 UTC. Fire within
# ±5 min of a boundary so we settle on the freshly-stamped rate.
_CARRY_BOUNDARY_HOURS = (0, 8, 16)
_CARRY_BOUNDARY_WINDOW_S = 5 * 60

# Mirror funding_carry_loader floors exactly so live and backtest agree.
_CARRY_MIN_RATE = 0.0005          # |rate| ≥ 0.05%
_CARRY_TOP_PCT = 0.10             # top/bottom decile
_CARRY_MIN_SYMBOLS = 8            # need a real distribution to rank


# ---------------------------------------------------------------------------
# Liquidation-cascade emitter constants
# ---------------------------------------------------------------------------
# Tier-aware thresholds mirror entry_filters._LIQ_THRESHOLDS so the entry
# filter and the signal emitter agree on what counts as a "cascade". If the
# entry filter would block sub-threshold cascades anyway, no point emitting
# signals that always get filtered out.
_LIQ_CASCADE_THRESHOLDS = {
    "major": 250_000,
    "large": 50_000,
    "small_alt": 10_000,   # slightly above filter floor (5k) to avoid noise
}
# Same tiers as entry_filters._tier_of — duplicated to avoid a circular import
# (entry_filters imports SignalPacket from signal_detector → data_streams).
_LIQ_TIER_MAJORS = frozenset({"BTC", "ETH"})
_LIQ_TIER_LARGE = frozenset({
    "SOL", "BNB", "XRP", "AVAX", "MATIC", "LINK", "DOT", "UNI", "ATOM",
})
# Don't re-emit a cascade signal for the same symbol within this window —
# the brain already dedups by hour bucket, but we don't want to spam the
# pending_signals queue every 30s while a cascade is rolling.
_LIQ_CASCADE_COOLDOWN_S = 300

# OBI-F: don't re-emit per-symbol within this window. The persistent
# imbalance + 1h-opposite gate is rare; if it does fire, give the trade
# room to play out before re-arming.
_OB_IMBALANCE_COOLDOWN_S = 600
_OB_IMBALANCE_TRIGGER = 0.4  # |obi_f_ema| threshold per arXiv 2507.22712


def _liq_tier_of(symbol: str) -> str:
    s = (symbol or "").upper()
    if s in _LIQ_TIER_MAJORS:
        return "major"
    if s in _LIQ_TIER_LARGE:
        return "large"
    return "small_alt"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TokenSignal:
    """A detected event from any data stream."""
    source: str           # "coingecko_trending", "dexscreener", "binance_listing", etc.
    symbol: str
    event_type: str       # "trending", "volume_spike", "new_listing", "funding_extreme", "fgi_extreme"
    data: dict            # raw data payload
    timestamp: float      # unix ms
    priority: int = 0     # 0=low, 1=medium, 2=high, 3=urgent


@dataclass
class MarketSnapshot:
    """Current state of all market data."""
    prices: dict[str, float] = field(default_factory=dict)
    volumes_24h: dict[str, float] = field(default_factory=dict)
    funding_rates: dict[str, float] = field(default_factory=dict)
    fear_greed_index: int = 50
    trending_tokens: list[str] = field(default_factory=list)
    dex_volume_spikes: list[dict] = field(default_factory=list)
    recent_listings: list[dict] = field(default_factory=list)
    news_items: list[dict] = field(default_factory=list)
    last_updated: float = 0


# ---------------------------------------------------------------------------
# API fetchers (all free, no auth)
# ---------------------------------------------------------------------------

_UA = {"User-Agent": "kaizen-trader-engine/2.0"}


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict | list]:
    """Fetch JSON from URL with error handling."""
    try:
        req = Request(url, headers=_UA)
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def fetch_coingecko_trending() -> list[dict]:
    """Get top 7 trending tokens from CoinGecko. Free, no auth, 30 calls/min."""
    data = _fetch_json("https://api.coingecko.com/api/v3/search/trending")
    if not data:
        return []
    results = []
    for coin in data.get("coins", []):
        item = coin.get("item", {})
        results.append({
            "symbol": item.get("symbol", ""),
            "name": item.get("name", ""),
            "rank": item.get("score", 99) + 1,
            "market_cap_rank": item.get("market_cap_rank"),
            "price_btc": item.get("price_btc", 0),
        })
    return results


def fetch_dexscreener_boosted() -> list[dict]:
    """Get top boosted tokens from DexScreener. Free, no auth."""
    data = _fetch_json("https://api.dexscreener.com/token-boosts/top/v1")
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "chain": t.get("chainId", ""),
            "address": t.get("tokenAddress", ""),
            "url": t.get("url", ""),
            "description": t.get("description", ""),
        }
        for t in data[:20]
    ]


def fetch_dexscreener_token(symbol: str) -> Optional[dict]:
    """Search DexScreener for a token. Returns top pair info."""
    data = _fetch_json(f"https://api.dexscreener.com/latest/dex/search?q={symbol}")
    if not data:
        return None
    pairs = data.get("pairs", [])
    if not pairs:
        return None
    p = pairs[0]
    return {
        "symbol": p.get("baseToken", {}).get("symbol", ""),
        "chain": p.get("chainId", ""),
        "dex": p.get("dexId", ""),
        "price_usd": float(p.get("priceUsd", 0) or 0),
        "volume_24h": float(p.get("volume", {}).get("h24", 0) or 0),
        "price_change_24h": float(p.get("priceChange", {}).get("h24", 0) or 0),
        "liquidity_usd": float(p.get("liquidity", {}).get("usd", 0) or 0),
        "pair_created_at": p.get("pairCreatedAt"),
    }


def fetch_fear_greed_index() -> tuple[int, str]:
    """Get current Fear & Greed Index. Free, no auth."""
    data = _fetch_json("https://api.alternative.me/fng/?limit=1")
    if not data or "data" not in data:
        return 50, "Neutral"
    entry = data["data"][0]
    return int(entry.get("value", 50)), entry.get("value_classification", "Neutral")


def fetch_binance_funding_rates() -> list[dict]:
    """Get all Binance Futures funding rates. Free, no auth."""
    data = _fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    if not data or not isinstance(data, list):
        return []
    results = []
    for item in data:
        rate = float(item.get("lastFundingRate", 0))
        if abs(rate) > 0.0003:  # only return notable rates
            results.append({
                "symbol": item.get("symbol", "").replace("USDT", ""),
                "funding_rate": rate,
                "mark_price": float(item.get("markPrice", 0)),
                "index_price": float(item.get("indexPrice", 0)),
            })
    return sorted(results, key=lambda x: abs(x["funding_rate"]), reverse=True)


def fetch_binance_new_listings() -> list[dict]:
    """Check Binance Futures exchangeInfo for recently listed tokens.
    Returns tokens listed in the last 7 days."""
    data = _fetch_json("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
    if not data:
        return []
    now_ms = time.time() * 1000
    seven_days_ms = 7 * 86_400_000
    results = []
    for sym in data.get("symbols", []):
        obd = sym.get("onboardDate", 0)
        if obd and now_ms - obd < seven_days_ms and sym.get("status") == "TRADING":
            base = sym["symbol"].replace("USDT", "")
            if base.startswith("1000"):
                base = base[4:]
            results.append({
                "symbol": base,
                "listed_at_ms": obd,
                "age_hours": (now_ms - obd) / 3_600_000,
                "exchange": "binance_futures",
            })
    return sorted(results, key=lambda x: x["listed_at_ms"], reverse=True)


def fetch_coinbase_new_listings(known_products: set[str]) -> list[dict]:
    """Check Coinbase for new products not in known_products set."""
    data = _fetch_json("https://api.exchange.coinbase.com/products")
    if not data or not isinstance(data, list):
        return []
    new = []
    for p in data:
        pid = p.get("id", "")
        if (pid not in known_products
                and p.get("quote_currency") == "USD"
                and p.get("status") == "online"):
            new.append({
                "symbol": p.get("base_currency", ""),
                "product_id": pid,
                "exchange": "coinbase",
            })
    return new


def fetch_lunarcrush_trending() -> list[dict]:
    """Get trending coins from LunarCrush. Requires API key in env."""
    import os
    key = os.environ.get("LUNARCRUSH_API_KEY", "")
    if not key:
        return []
    try:
        req = Request(
            "https://lunarcrush.com/api4/public/coins/list/v2",
            headers={"User-Agent": "kaizen-trader/2.0", "Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        coins = data.get("data", [])
        # Return top coins by social activity
        return [
            {
                "symbol": c.get("symbol", ""),
                "name": c.get("name", ""),
                "galaxy_score": c.get("galaxy_score", 0),
                "alt_rank": c.get("alt_rank", 0),
                "social_volume": c.get("social_volume", 0),
                "social_score": c.get("social_score", 0),
            }
            for c in sorted(coins, key=lambda x: x.get("galaxy_score", 0), reverse=True)[:20]
        ]
    except Exception:
        return []


def fetch_reddit_crypto_sentiment() -> list[dict]:
    """Get top posts from r/cryptocurrency. Free, no auth."""
    data = _fetch_json("https://www.reddit.com/r/cryptocurrency/hot.json?limit=10")
    if not data:
        return []
    posts = data.get("data", {}).get("children", [])
    return [
        {
            "title": p["data"].get("title", ""),
            "score": p["data"].get("score", 0),
            "num_comments": p["data"].get("num_comments", 0),
            "url": p["data"].get("url", ""),
        }
        for p in posts
        if p.get("data", {}).get("score", 0) > 10
    ]


def fetch_coingecko_global() -> dict:
    """Get global crypto market stats. Free, no auth."""
    data = _fetch_json("https://api.coingecko.com/api/v3/global")
    if not data:
        return {}
    d = data.get("data", {})
    return {
        "total_market_cap_usd": d.get("total_market_cap", {}).get("usd", 0),
        "total_volume_24h": d.get("total_volume", {}).get("usd", 0),
        "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
        "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd", 0),
    }


def fetch_binance_top_movers(limit: int = 10) -> tuple[list[dict], list[dict]]:
    """Get top gainers and losers from Binance Futures 24h. Free, no auth."""
    data = _fetch_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    if not data:
        return [], []
    usdt = [t for t in data if t.get("symbol", "").endswith("USDT")]
    for t in usdt:
        t["_change"] = float(t.get("priceChangePercent", 0))
        t["_volume"] = float(t.get("quoteVolume", 0))
        t["_symbol"] = t["symbol"].replace("USDT", "")
        if t["_symbol"].startswith("1000"):
            t["_symbol"] = t["_symbol"][4:]

    gainers = sorted(usdt, key=lambda x: x["_change"], reverse=True)[:limit]
    losers = sorted(usdt, key=lambda x: x["_change"])[:limit]

    def _fmt(items):
        return [{"symbol": t["_symbol"], "change_pct": t["_change"], "volume_24h": t["_volume"]} for t in items]

    return _fmt(gainers), _fmt(losers)


def fetch_crypto_news() -> list[dict]:
    """Get latest crypto news from CoinTelegraph RSS. Free, no auth."""
    import re
    try:
        req = Request("https://cointelegraph.com/rss", headers=_UA)
        with urlopen(req, timeout=10) as resp:
            xml = resp.read().decode()
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", xml)
        links = re.findall(r"<link>(https://cointelegraph\.com/news/[^<]+)</link>", xml)
        pub_dates = re.findall(r"<pubDate>(.*?)</pubDate>", xml)
        results = []
        for i, title in enumerate(titles[:15]):
            results.append({
                "title": title,
                "url": links[i] if i < len(links) else "",
                "published": pub_dates[i] if i < len(pub_dates) else "",
            })
        return results
    except Exception:
        return []


def fetch_binance_prices(symbols: list[str], snapshot=None) -> dict[str, float]:
    """Fetch current prices for symbols.

    If a MarketSnapshot is provided, prefer its in-memory prices (populated by
    the WS feed every ~1s) — saves a ~80KB REST round-trip per call. Falls back
    to single-symbol REST endpoints only for symbols not in the snapshot.
    The previous implementation pulled ALL ~2000 Binance spot prices on every
    call regardless of how few symbols were needed.
    """
    prices: dict[str, float] = {}
    missing: list[str] = []
    if snapshot is not None:
        for s in symbols:
            su = s.upper()
            p = snapshot.prices.get(su)
            if p and p > 0:
                prices[su] = p
            else:
                missing.append(su)
    else:
        missing = [s.upper() for s in symbols]

    # Per-symbol fallback for what's still missing (cheap: 1 small JSON each).
    for s in missing:
        item = _fetch_json(f"https://api.binance.com/api/v3/ticker/price?symbol={s}USDT")
        if item and "price" in item:
            try:
                prices[s] = float(item["price"])
            except (ValueError, TypeError):
                pass
    return prices


# ---------------------------------------------------------------------------
# DataStreams — manages all data feed polling
# ---------------------------------------------------------------------------

class DataStreams:
    """Manages all real-time data feeds.

    Polls free APIs at appropriate intervals and emits TokenSignals
    to a callback function when events are detected.
    """

    def __init__(self, on_signal: Callable[[TokenSignal], None]):
        self.on_signal = on_signal
        self.snapshot = MarketSnapshot()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._known_coinbase_products: set[str] = set()
        self._known_binance_listings: set[str] = set()
        self._prev_trending: set[str] = set()
        self._lock = threading.Lock()
        self._binance_ws = None
        self._ws_tick_count = 0
        # Last 8h funding boundary we've already emitted carry events for.
        # Avoids duplicate emission across the 60s tick / ±5min window.
        self._last_carry_boundary_ms: int = 0
        # symbol → unix-seconds of last cascade signal emission (for cooldown)
        self._last_liq_cascade_emit: dict[str, float] = {}
        # symbol → unix-seconds of last OBI-F signal emission (for cooldown)
        self._last_ob_imbalance_emit: dict[str, float] = {}
        # Symbols held by the executor — set externally by runner each tick so
        # the OBI-F WS subscription can rotate to active positions + trending.
        self._held_position_symbols: set[str] = set()
        # Last regime emitted for mempool stress (so we only fire on flips, not
        # every poll while regime stays elevated/extreme).
        self._last_mempool_regime: str = "calm"
        self._last_mempool_emit_s: float = 0.0

    def get_prices_snapshot(self) -> dict[str, float]:
        """Return a shallow copy of the current prices dict, taken under lock.

        WS thread mutates self.snapshot.prices under self._lock, and pollers
        sometimes replace whole dicts (e.g. funding_rates). Reading the live
        dict from another thread can yield torn reads or stale references after
        a poller swap. Always copy under lock for cross-thread reads.
        """
        with self._lock:
            return dict(self.snapshot.prices)

    def _on_ws_tick(self, symbol: str, price: float, volume_24h: float, change_pct: float):
        """Called by BinanceWebSocket on every tick (~1s for all symbols)."""
        with self._lock:
            self.snapshot.prices[symbol] = price
            self.snapshot.volumes_24h[symbol] = volume_24h
        self._ws_tick_count += 1

        # Detect large sudden moves (>10% in 24h) as potential signals
        if abs(change_pct) > 10:
            self.on_signal(TokenSignal(
                source="binance_ws",
                symbol=symbol,
                event_type="large_move",
                data={"price": price, "volume_24h": volume_24h, "change_pct": change_pct},
                timestamp=time.time() * 1000,
                priority=2 if abs(change_pct) > 20 else 1,
            ))

    def _on_ws_funding(self, symbol: str, funding_rate: float, mark_price: float):
        """Called by BinanceWebSocket with real-time funding rates."""
        with self._lock:
            self.snapshot.funding_rates[symbol] = funding_rate
            self.snapshot.prices[symbol] = mark_price

    def start(self):
        """Start all data stream polling threads + Binance WebSocket."""
        log("info", "Starting data streams...")

        # Start Binance WebSocket for real-time prices + funding
        try:
            from src.engine.binance_ws import BinanceWebSocket
            self._binance_ws = BinanceWebSocket(
                on_tick=self._on_ws_tick,
                on_funding=self._on_ws_funding,
            )
            self._binance_ws.connect()
            log("info", "Binance Futures WS starting — real-time prices for 700+ symbols")
        except Exception as e:
            log("warn", f"Binance WS failed to start: {e} — using REST polling fallback")

        # Initialize known Coinbase products
        products = _fetch_json("https://api.exchange.coinbase.com/products")
        if products:
            self._known_coinbase_products = {p["id"] for p in products}
            log("info", f"Loaded {len(self._known_coinbase_products)} known Coinbase products")

        # Initialize known Binance listings
        listings = fetch_binance_new_listings()
        self._known_binance_listings = {l["symbol"] for l in listings}

        streams = [
            ("coingecko_trending", self._poll_trending, 600),      # 10 min
            ("dexscreener", self._poll_dexscreener, 300),           # 5 min
            ("fear_greed", self._poll_fgi, 3600),                   # 1 hour
            ("binance_funding", self._poll_funding, 60),            # 1 min
            ("binance_listings", self._poll_binance_listings, 60),  # 1 min
            ("coinbase_listings", self._poll_coinbase_listings, 30),# 30 sec
            ("lunarcrush", self._poll_lunarcrush, 120),             # 2 min (rate limited)
            ("reddit", self._poll_reddit, 300),                     # 5 min
            ("global_market", self._poll_global_market, 300),       # 5 min
            ("top_movers", self._poll_top_movers, 60),              # 1 min — catches pumps fast
            ("crypto_news", self._poll_news, 300),                  # 5 min — CoinTelegraph RSS
            ("funding_carry", self._poll_funding_carry, 60),        # 1 min tick, fires only at 8h boundaries
            ("liquidation_cascade", self._poll_liquidation_cascade, 30),  # 30s tick, env-gated
            ("ob_imbalance", self._poll_orderbook_imbalance, 15),         # 15s tick, env-gated
            ("mempool_stress", self._poll_mempool_stress, 300),  # 5min, env-gated, BTC fee regime
        ]

        for name, fn, interval_s in streams:
            t = threading.Thread(
                target=self._poll_loop, args=(name, fn, interval_s),
                daemon=True, name=f"stream-{name}",
            )
            t.start()
            self._threads.append(t)
            log("info", f"  Started stream: {name} (every {interval_s}s)")

    def stop(self):
        self._stop.set()

    def _poll_loop(self, name: str, fn: Callable, interval_s: float):
        """Generic polling loop with error handling."""
        while not self._stop.is_set():
            try:
                fn()
            except Exception as e:
                log("warn", f"Stream {name} error: {e}")
            self._stop.wait(timeout=interval_s)

    def _poll_trending(self):
        trending = fetch_coingecko_trending()
        if not trending:
            return
        now = time.time() * 1000
        symbols = [t["symbol"] for t in trending]

        with self._lock:
            self.snapshot.trending_tokens = symbols

        # Detect NEW entries to trending
        current = set(symbols)
        new_trending = current - self._prev_trending
        self._prev_trending = current

        for t in trending:
            if t["symbol"] in new_trending:
                self.on_signal(TokenSignal(
                    source="coingecko_trending",
                    symbol=t["symbol"],
                    event_type="trending",
                    data=t,
                    timestamp=now,
                    priority=2 if t["rank"] <= 3 else 1,
                ))

    def _poll_dexscreener(self):
        boosted = fetch_dexscreener_boosted()
        now = time.time() * 1000
        with self._lock:
            self.snapshot.dex_volume_spikes = boosted

        for t in boosted[:5]:
            self.on_signal(TokenSignal(
                source="dexscreener",
                symbol=t.get("address", "")[:10],
                event_type="volume_spike",
                data=t,
                timestamp=now,
                priority=1,
            ))

    def _poll_fgi(self):
        value, classification = fetch_fear_greed_index()
        now = time.time() * 1000

        with self._lock:
            old_fgi = self.snapshot.fear_greed_index
            self.snapshot.fear_greed_index = value

        # Signal on extreme readings
        if value <= 20 or value >= 80:
            self.on_signal(TokenSignal(
                source="alternative_me",
                symbol="BTC",
                event_type="fgi_extreme",
                data={"value": value, "classification": classification},
                timestamp=now,
                priority=2 if (value <= 10 or value >= 90) else 1,
            ))

    def _poll_funding(self):
        rates = fetch_binance_funding_rates()
        now = time.time() * 1000

        with self._lock:
            self.snapshot.funding_rates = {r["symbol"]: r["funding_rate"] for r in rates}

        # Signal on extreme funding
        for r in rates[:10]:
            if abs(r["funding_rate"]) > 0.001:  # > 0.1% per 8h
                self.on_signal(TokenSignal(
                    source="binance_funding",
                    symbol=r["symbol"],
                    event_type="funding_extreme",
                    data=r,
                    timestamp=now,
                    priority=2 if abs(r["funding_rate"]) > 0.003 else 1,
                ))

    def _poll_binance_listings(self):
        listings = fetch_binance_new_listings()
        now = time.time() * 1000

        for l in listings:
            if l["symbol"] not in self._known_binance_listings and l["age_hours"] < 24:
                self._known_binance_listings.add(l["symbol"])
                with self._lock:
                    self.snapshot.recent_listings.append(l)
                    # Keep only last 50
                    self.snapshot.recent_listings = self.snapshot.recent_listings[-50:]

                self.on_signal(TokenSignal(
                    source="binance_listing",
                    symbol=l["symbol"],
                    event_type="new_listing",
                    data=l,
                    timestamp=now,
                    priority=3,  # URGENT — listing pumps are time-sensitive
                ))
                log("info", f"NEW BINANCE LISTING: {l['symbol']} ({l['age_hours']:.1f}h ago)")

    def _poll_coinbase_listings(self):
        new = fetch_coinbase_new_listings(self._known_coinbase_products)
        now = time.time() * 1000

        for l in new:
            self._known_coinbase_products.add(l["product_id"])
            with self._lock:
                self.snapshot.recent_listings.append(l)

            self.on_signal(TokenSignal(
                source="coinbase_listing",
                symbol=l["symbol"],
                event_type="new_listing",
                data=l,
                timestamp=now,
                priority=3,  # URGENT — Coinbase listings are 77% WR
            ))
            log("info", f"NEW COINBASE LISTING: {l['symbol']}")

    def _poll_lunarcrush(self):
        lc = fetch_lunarcrush_trending()
        if not lc:
            return
        now = time.time() * 1000

        for coin in lc[:5]:
            if coin.get("galaxy_score", 0) > 70:
                self.on_signal(TokenSignal(
                    source="lunarcrush",
                    symbol=coin["symbol"],
                    event_type="social_buzz",
                    data=coin,
                    timestamp=now,
                    priority=2 if coin.get("galaxy_score", 0) > 80 else 1,
                ))

    def _poll_reddit(self):
        # Reddit is for market context, not direct signals.
        # Fetch is kept so the brain can reference sentiment in its tick prompt.
        posts = fetch_reddit_crypto_sentiment()
        with self._lock:
            self.snapshot.reddit_posts = posts

    def _poll_global_market(self):
        data = fetch_coingecko_global()
        if not data:
            return
        now = time.time() * 1000

        # Large market-wide moves — stored in snapshot for brain context only.
        # No signal emitted because SignalDetector has no handler for "market_move".

    def _poll_top_movers(self):
        """Detect tokens with massive 24h moves — catches pumps like ALPACA +391%."""
        gainers, losers = fetch_binance_top_movers(limit=10)
        now = time.time() * 1000

        for g in gainers:
            change = g.get("change_pct", 0)
            vol = g.get("volume_24h", 0)
            sym = g.get("symbol", "")

            # Major pump: >50% with >$10M volume = real move, not manipulation
            if change > 50 and vol > 10_000_000:
                self.on_signal(TokenSignal(
                    source="binance_movers",
                    symbol=sym,
                    event_type="major_pump",
                    data=g,
                    timestamp=now,
                    priority=2 if change > 100 else 1,
                ))

        for l in losers:
            change = l.get("change_pct", 0)
            vol = l.get("volume_24h", 0)
            sym = l.get("symbol", "")

            # Major dumps are stored in snapshot for context only.
            # No signal emitted because SignalDetector has no handler for "major_dump".

    def _poll_news(self):
        """Fetch crypto news from CoinTelegraph RSS."""
        articles = fetch_crypto_news()
        now = time.time() * 1000

        # Persist to snapshot so the brain's tick prompt actually sees them.
        # Without this, runner.py reads snapshot.news_items and always gets [].
        with self._lock:
            self.snapshot.news_items = articles[:10]

        # Look for market-moving keywords in headlines
        _URGENT_KEYWORDS = ["hack", "exploit", "SEC", "ban", "crash", "surge", "listing",
                            "binance", "coinbase", "regulation", "arrest", "fraud"]
        for a in articles[:5]:
            title_lower = a.get("title", "").lower()
            is_urgent = any(kw in title_lower for kw in _URGENT_KEYWORDS)
            if is_urgent:
                self.on_signal(TokenSignal(
                    source="cointelegraph",
                    symbol="NEWS",
                    event_type="breaking_news",
                    data=a,
                    timestamp=now,
                    priority=2,
                ))

    def _poll_funding_carry(self):
        """Cross-sectional funding-carry — load-bearing alpha (ROBUST OOS).

        Fires at 8h funding boundaries (00:00, 08:00, 16:00 UTC, ±5min). Pulls
        every USDT-perp funding rate in one /fapi/v1/premiumIndex call, ranks
        them, and emits funding_extreme TokenSignals for the top decile (with
        side_hint='short') and bottom decile (side_hint='long') — but only for
        symbols in the liquid universe. Mirrors funding_carry_loader.py
        ranking exactly so live and backtest agree.

        DISABLED BY DEFAULT (env var FUNDING_CARRY_ENABLED='1' to enable).
        Adversarial audit found the +$5.53 backtest result that justified
        this poller was fabricated by three compounding bugs: re-entry
        cooldown clock comparing wall-clock to sim time (so 42 trades on
        9 names back-to-back was a re-trading artifact, not 42 draws),
        funding_carry totally bypassing the volatility/correlation/oi/basis/
        cvd/top-crowding chain, and exit-attribution being wick-PnL not
        carry-PnL (25/42 fast_cut, only 1/42 target). Until those are
        fixed in backtest AND a clean ROBUST OOS verdict reproduces, the
        poller stays off-by-default — code path is wired so flipping the
        env to '1' is a one-line rollout once the validation is honest.
        """
        if os.environ.get("FUNDING_CARRY_ENABLED", "0") != "1":
            return

        # Are we within ±5min of an 8h boundary?
        now_s = time.time()
        gm = time.gmtime(now_s)
        seconds_into_hour = gm.tm_min * 60 + gm.tm_sec
        on_boundary_hour = gm.tm_hour in _CARRY_BOUNDARY_HOURS
        in_post_window = on_boundary_hour and seconds_into_hour <= _CARRY_BOUNDARY_WINDOW_S
        in_pre_window = (
            (gm.tm_hour + 1) % 24 in _CARRY_BOUNDARY_HOURS
            and seconds_into_hour >= 3600 - _CARRY_BOUNDARY_WINDOW_S
        )
        if not (in_post_window or in_pre_window):
            return

        # Bucket to canonical 8h slot for dedup (matches loader._bucket_ts).
        now_ms = int(now_s * 1000)
        bucket_ms = (now_ms // (8 * 3_600_000)) * (8 * 3_600_000)
        if bucket_ms == self._last_carry_boundary_ms:
            return  # already emitted for this funding window

        # One call returns funding for ALL perps. Use raw fetch (not
        # fetch_binance_funding_rates — that drops names below 0.03%, which
        # is fine for funding_squeeze but we need the FULL distribution to
        # rank cross-sectionally).
        data = _fetch_json("https://fapi.binance.com/fapi/v1/premiumIndex")
        if not data or not isinstance(data, list):
            return

        # Build universe: USDT-quoted perps only, normalize 1000-prefixed
        # symbols (e.g. 1000PEPEUSDT → PEPE) to match brain's expected names.
        snapshot: list[tuple[str, float, float]] = []  # (symbol, rate, mark)
        for item in data:
            raw_sym = item.get("symbol", "")
            if not raw_sym.endswith("USDT"):
                continue
            base = raw_sym[:-4]
            if base.startswith("1000"):
                base = base[4:]
            try:
                rate = float(item.get("lastFundingRate", 0))
                mark = float(item.get("markPrice", 0))
            except (TypeError, ValueError):
                continue
            if abs(rate) < _CARRY_MIN_RATE:
                continue
            snapshot.append((base, rate, mark))

        if len(snapshot) < _CARRY_MIN_SYMBOLS:
            return

        snapshot.sort(key=lambda x: x[1])
        n = len(snapshot)
        k = max(1, int(round(n * _CARRY_TOP_PCT)))
        bottom = snapshot[:k]   # most-negative rates → LONG
        top = snapshot[-k:]     # most-positive rates → SHORT

        ts_ms = float(now_ms)
        emitted = 0

        for i, (sym, rate, mark) in enumerate(bottom):
            if sym not in _CARRY_LIQUID_UNIVERSE:
                continue
            self.on_signal(TokenSignal(
                source="binance_funding_xsec",
                symbol=sym,
                event_type="funding_extreme",
                data={
                    "symbol": sym,
                    "funding_rate": rate,
                    "mark_price": mark,
                    "side_hint": "long",
                    "funding_rank_pct": (i + 1) / n,
                    "carry_event_type": "funding_carry_long",
                },
                timestamp=ts_ms,
                priority=2,
            ))
            emitted += 1

        for j, (sym, rate, mark) in enumerate(top):
            if sym not in _CARRY_LIQUID_UNIVERSE:
                continue
            rank_from_top = (k - j) / n
            self.on_signal(TokenSignal(
                source="binance_funding_xsec",
                symbol=sym,
                event_type="funding_extreme",
                data={
                    "symbol": sym,
                    "funding_rate": rate,
                    "mark_price": mark,
                    "side_hint": "short",
                    "funding_rank_pct": rank_from_top,
                    "carry_event_type": "funding_carry_short",
                },
                timestamp=ts_ms,
                priority=2,
            ))
            emitted += 1

        self._last_carry_boundary_ms = bucket_ms
        if emitted:
            log("info", f"[funding_carry] emitted {emitted} carry events at boundary {bucket_ms}")

    def _poll_liquidation_cascade(self):
        """Emit liquidation_cascade TokenSignals from the live WS tracker.

        Strategy thesis: when a tier-relevant USD volume of liquidations
        cascades on a symbol within 5min and one side dominates, the wick
        often reverts in minutes (academic evidence on Oct 10-11 2025
        cascade). We emit OPPOSITE-side bias:
          - long_liq dominant (price wicked DOWN on long stops) → side_hint=long (fade-up)
          - short_liq dominant (price wicked UP on short stops) → side_hint=short (fade-down)

        DEFAULT-OFF until OOS validation passes (LIQUIDATION_CASCADE_ENABLED='1').
        Mirrors funding_carry's audit-discipline pattern — code is wired,
        flip the env to roll it out once a clean backtest verdict reproduces.
        """
        if os.environ.get("LIQUIDATION_CASCADE_ENABLED", "0") != "1":
            return
        try:
            from src.engine.liquidation_tracker import get_tracker
            tracker = get_tracker()
        except Exception:
            return
        if tracker.status != "connected":
            return

        now_s = time.time()
        now_ms = now_s * 1000
        emitted = 0
        for sym in tracker.all_active_symbols():
            # Cooldown: don't re-emit while a cascade is rolling.
            last_emit = self._last_liq_cascade_emit.get(sym, 0.0)
            if now_s - last_emit < _LIQ_CASCADE_COOLDOWN_S:
                continue
            summary = tracker.cascade_score(sym, window_seconds=300)
            long_usd = summary["long_liq_usd_5m"]
            short_usd = summary["short_liq_usd_5m"]
            total = long_usd + short_usd
            tier = _liq_tier_of(sym)
            threshold = _LIQ_CASCADE_THRESHOLDS[tier]
            if total < threshold:
                continue
            # Need a clear dominant side; otherwise it's two-sided liquidation
            # (chop) and there's no clean revert direction to fade.
            dominant = summary["dominant_side"]
            if dominant is None or summary["imbalance_ratio"] < 1.5:
                continue
            # forced_long_close (longs liquidated, dominant=='long', price
            # wicked down) → fade with LONG entry.
            # forced_short_close (shorts liquidated, dominant=='short',
            # price wicked up) → fade with SHORT entry.
            side_hint = "long" if dominant == "long" else "short"
            cascade_event = (
                "forced_long_close" if dominant == "long" else "forced_short_close"
            )
            largest = tracker.largest_single_in_window(sym, 300)
            count = tracker.liquidation_count(sym, 300)

            self.on_signal(TokenSignal(
                source="binance_liq_ws",
                symbol=sym,
                event_type="liquidation_cascade",
                data={
                    "side_hint": side_hint,
                    "cascade_event": cascade_event,
                    "liq_usd_5m": total,
                    "long_liq_usd_5m": long_usd,
                    "short_liq_usd_5m": short_usd,
                    "imbalance_ratio": summary["imbalance_ratio"],
                    "largest_single_usd": largest,
                    "liq_count_5m": count,
                    "tier": tier,
                },
                timestamp=now_ms,
                priority=2,
            ))
            self._last_liq_cascade_emit[sym] = now_s
            emitted += 1

        if emitted:
            log("info", f"[liq_cascade] emitted {emitted} cascade events")

    def _poll_orderbook_imbalance(self):
        """Emit orderbook_imbalance TokenSignals from the OBI-F tracker.

        Filtered top-5 OBI on the 5-min EMA, gated to active symbols only.
        We rotate the WS subscription to (held positions ∪ top trending),
        because the L2 depth20@100ms stream is HIGH bandwidth — broadcasting
        the universe would be wasteful.

        DEFAULT-OFF: gated by env OB_IMBALANCE_ENABLED='1'. This signal is
        HARD to backtest — exchanges don't republish historical L2 books
        for free. A faithful walk-forward would need an aggTrade-replay
        infrastructure that reconstructs the book from depth-snapshot +
        diff-update events. Until that exists, OOS validation is impossible
        and the safe default is OFF, mirroring the audit discipline applied
        to FUNDING_CARRY_ENABLED and LIQUIDATION_CASCADE_ENABLED.
        """
        if os.environ.get("OB_IMBALANCE_ENABLED", "0") != "1":
            return
        try:
            from src.engine.orderbook_tracker import get_tracker
            tracker = get_tracker()
        except Exception:
            return
        if tracker.status != "connected" and not tracker.active_symbols():
            tracker.start()

        # Refresh active symbol set: held positions + top trending, capped.
        with self._lock:
            held = set(self._held_position_symbols)
            trending = list(self.snapshot.trending_tokens or [])
        active = set(s.upper() for s in held)
        for t in trending:
            active.add((t or "").upper())
            if len(active) >= 12:
                break
        active.discard("")
        tracker.set_active_symbols(active)

        now_s = time.time()
        now_ms = now_s * 1000
        emitted = 0
        for sym in tracker.active_symbols():
            ema = tracker.obi_f_ema(sym)
            if ema is None or abs(ema) <= _OB_IMBALANCE_TRIGGER:
                continue
            last_emit = self._last_ob_imbalance_emit.get(sym, 0.0)
            if now_s - last_emit < _OB_IMBALANCE_COOLDOWN_S:
                continue
            price = self.snapshot.prices.get(sym, 0.0)
            self.on_signal(TokenSignal(
                source="binance_obi_ws",
                symbol=sym,
                event_type="orderbook_imbalance",
                data={
                    "obi_f_ema": ema,
                    "obi_f": tracker.obi_f(sym),
                    "price": price,
                },
                timestamp=now_ms,
                priority=1,
            ))
            self._last_ob_imbalance_emit[sym] = now_s
            emitted += 1
        if emitted:
            log("info", f"[ob_imbalance] emitted {emitted} OBI-F events")

    def _poll_mempool_stress(self):
        """BTC mempool fee-market regime → bearish directional signal on flip.

        Thesis: BTC fee spikes precede major sell-offs (miners netflow stress)
        and post-rally tops (retail FOMO). When the trailing-24h median fee
        crosses into the 75th/95th percentile of trailing-7d, we emit
        `mempool_stress` with side_hint=short for BTC ONLY.

        DEFAULT-OFF (MEMPOOL_STRESS_ENABLED='1' to enable). Default off because
        we have NO history yet — can't backtest until the collector
        (scripts/collect_mempool.py) has run for ≥7 days.
        """
        if os.environ.get("MEMPOOL_STRESS_ENABLED", "0") != "1":
            return
        try:
            from src.backtesting.mempool_loader import (
                append_snapshot, load_history, regime_from_recent,
            )
        except Exception:
            return

        append_snapshot()
        history = load_history()
        if not history:
            return
        regime = regime_from_recent(history)

        prev = self._last_mempool_regime
        self._last_mempool_regime = regime
        if regime == "calm" or prev == regime:
            return

        now_s = time.time()
        if now_s - self._last_mempool_emit_s < 6 * 3600:
            return
        self._last_mempool_emit_s = now_s

        latest = history[-1]
        self.on_signal(TokenSignal(
            source="mempool_space",
            symbol="BTC",
            event_type="mempool_stress",
            data={
                "side_hint": "short",
                "regime": regime,
                "fastest_fee": latest["fastest_fee"],
                "half_hour_fee": latest["half_hour_fee"],
                "mempool_vsize": latest["mempool_vsize"],
                "total_pending_fee_btc": latest["total_pending_fee_btc"],
                "baseline_samples": len(history),
            },
            timestamp=now_s * 1000,
            priority=2 if regime == "extreme" else 1,
        ))
        log("info", f"[mempool_stress] regime flip {prev}->{regime} "
                    f"fastest={latest['fastest_fee']} sat/vB")
