"""Fast rule-based signal detection.

Processes TokenSignals from DataStreams and generates SignalPackets
for Claude to analyze. No LLM calls here — pure Python, <1ms per check.

A SignalPacket contains ALL context Claude needs to make a decision,
compressed to ~500-1000 tokens.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from src.engine.data_streams import TokenSignal, MarketSnapshot, fetch_dexscreener_token


@dataclass
class SignalPacket:
    """Everything Claude needs to make a trading decision."""
    signal_id: str
    symbol: str
    signal_type: str        # "listing_pump", "funding_squeeze", "trending_breakout", "fgi_contrarian"
    priority: int           # 0-3
    timestamp: float

    # Market context
    price_usd: float = 0
    volume_24h: float = 0
    price_change_24h: float = 0
    fear_greed_index: int = 50
    funding_rate: float = 0

    # Signal-specific data
    source: str = ""
    reasoning: str = ""
    data: dict = field(default_factory=dict)

    # Suggested action (from rules, Claude can override)
    suggested_side: str = ""   # "long" or "short"
    suggested_stop_pct: float = 0
    suggested_target_pct: float = 0



class SignalDetector:
    """Converts raw TokenSignals into actionable SignalPackets.

    Rules-based filtering — no LLM calls. Runs in <1ms.
    Only passes high-quality signals to Claude for analysis.
    """

    def __init__(self):
        # Insertion-ordered dedup: an OrderedDict guarantees we evict the OLDEST
        # keys first when we prune. The previous `set(list(...)[-500:])` evicted
        # arbitrary keys (set iteration order is implementation-defined), letting
        # stale signals re-fire and recent ones get dropped.
        from collections import OrderedDict
        self._seen_signals: OrderedDict[str, None] = OrderedDict()
        self._signal_count = 0

    def process(self, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Process a raw signal and return a SignalPacket if it's worth analyzing."""

        # Dedup: don't process same signal within 1 hour
        dedup_key = f"{signal.symbol}:{signal.event_type}:{int(signal.timestamp / 3_600_000)}"
        if dedup_key in self._seen_signals:
            self._seen_signals.move_to_end(dedup_key)  # refresh recency
            return None
        self._seen_signals[dedup_key] = None

        # Prune oldest entries (FIFO) once we exceed the cap.
        while len(self._seen_signals) > 1000:
            self._seen_signals.popitem(last=False)

        self._signal_count += 1
        sid = f"sig-{self._signal_count:06d}"

        if signal.event_type == "new_listing":
            return self._process_listing(sid, signal, snapshot)
        elif signal.event_type == "funding_extreme":
            return self._process_funding(sid, signal, snapshot)
        elif signal.event_type == "fgi_extreme":
            return self._process_fgi(sid, signal, snapshot)
        elif signal.event_type == "trending":
            return self._process_trending(sid, signal, snapshot)
        elif signal.event_type == "major_pump":
            return self._process_major_pump(sid, signal, snapshot)
        elif signal.event_type == "large_move":
            return self._process_large_move(sid, signal, snapshot)

        return None

    def _process_listing(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """New exchange listing — highest priority signal."""
        exchange = signal.data.get("exchange", signal.source)
        age_hours = signal.data.get("age_hours", 0)

        # Skip if listing is too old (>24h)
        if age_hours > 24:
            return None

        # Get price data from DexScreener or Binance
        dex_data = fetch_dexscreener_token(signal.symbol)
        price = dex_data["price_usd"] if dex_data else 0
        volume = dex_data["volume_24h"] if dex_data else 0
        change = dex_data["price_change_24h"] if dex_data else 0

        # Coinbase listings: 77% WR proven
        if "coinbase" in exchange:
            return SignalPacket(
                signal_id=sid, symbol=signal.symbol, signal_type="listing_pump",
                priority=3, timestamp=signal.timestamp, source=exchange,
                price_usd=price, volume_24h=volume, price_change_24h=change,
                fear_greed_index=snapshot.fear_greed_index,
                reasoning=f"NEW Coinbase listing detected. Backtest: 77% WR, +474% cumulative. Age: {age_hours:.1f}h",
                suggested_side="long", suggested_stop_pct=0.08, suggested_target_pct=0.30,
                data=signal.data,
            )

        # Binance Futures listings: also profitable
        if "binance" in exchange:
            return SignalPacket(
                signal_id=sid, symbol=signal.symbol, signal_type="listing_pump",
                priority=3, timestamp=signal.timestamp, source=exchange,
                price_usd=price, volume_24h=volume, price_change_24h=change,
                fear_greed_index=snapshot.fear_greed_index,
                reasoning=f"NEW Binance Futures listing. Backtest: +417% cumulative. Age: {age_hours:.1f}h",
                suggested_side="long", suggested_stop_pct=0.05, suggested_target_pct=0.30,
                data=signal.data,
            )

        return None

    def _process_funding(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Extreme funding rate — short squeeze or long squeeze.

        Cross-sectional carry events (carry_event_type set by the funding_carry
        poller) are routed to the dedicated funding_carry_long / funding_carry_short
        signal types so the brain scores them via its rank-based path. They
        bypass the absolute-level 0.1% gate because the rank IS the signal.
        """
        rate = signal.data.get("funding_rate", 0)
        symbol = signal.symbol

        # Cross-sectional carry route — distinct from absolute-level squeeze.
        carry_event_type = signal.data.get("carry_event_type")
        if carry_event_type in ("funding_carry_long", "funding_carry_short"):
            return self._process_funding_carry(sid, signal, snapshot)

        # Only process very extreme rates
        if abs(rate) < 0.001:
            return None

        side = "long" if rate < 0 else "short"
        price = signal.data.get("mark_price", 0)

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="funding_squeeze",
            priority=2, timestamp=signal.timestamp, source="binance_funding",
            price_usd=price, funding_rate=rate,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"Extreme funding {rate*100:+.3f}% — {'shorts paying longs heavily' if rate < 0 else 'longs paying shorts heavily'}",
            suggested_side=side, suggested_stop_pct=0.06, suggested_target_pct=0.08,
            data=signal.data,
        )

    def _process_funding_carry(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Cross-sectional funding carry — load-bearing alpha (ROBUST OOS).

        Mirrors live_replay.py's carry SignalPacket builder so live and
        backtest produce identical packets, which is what lets the brain's
        rank-based scoring fire on prod the same way it fires in backtest.
        """
        d = signal.data
        carry_event_type = d.get("carry_event_type", "funding_carry_long")
        rate = float(d.get("funding_rate", 0.0))
        rank = float(d.get("funding_rank_pct", 1.0))
        side_hint = d.get("side_hint", "long" if carry_event_type == "funding_carry_long" else "short")
        price = float(d.get("mark_price", 0.0)) or float(snapshot.prices.get(signal.symbol, 0))

        return SignalPacket(
            signal_id=sid, symbol=signal.symbol, signal_type=carry_event_type,
            priority=2, timestamp=signal.timestamp, source="binance_funding_xsec",
            price_usd=price, funding_rate=rate,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"x-sec funding carry rank={rank*100:.0f}% rate={rate*100:+.3f}% — {side_hint}",
            suggested_side=side_hint,
            suggested_stop_pct=0.06, suggested_target_pct=0.10,
            data={
                "funding_rate": rate,
                "funding_rank_pct": rank,
                "mark_price": price,
            },
        )

    def _process_fgi(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Fear & Greed extreme — contrarian trade on BTC/ETH."""
        fgi = signal.data.get("value", 50)

        if fgi <= 20:
            return SignalPacket(
                signal_id=sid, symbol="BTC", signal_type="fgi_contrarian",
                priority=2, timestamp=signal.timestamp, source="alternative_me",
                price_usd=snapshot.prices.get("BTC", 0),
                fear_greed_index=fgi,
                reasoning=f"FGI at {fgi} (Extreme Fear). Backtest: 61.4% WR on BTC/ETH contrarian longs.",
                suggested_side="long", suggested_stop_pct=0.12, suggested_target_pct=0.20,
                data=signal.data,
            )
        elif fgi >= 80:
            return SignalPacket(
                signal_id=sid, symbol="BTC", signal_type="fgi_contrarian",
                priority=1, timestamp=signal.timestamp, source="alternative_me",
                price_usd=snapshot.prices.get("BTC", 0),
                fear_greed_index=fgi,
                reasoning=f"FGI at {fgi} (Extreme Greed). Consider reducing exposure or shorting.",
                suggested_side="short", suggested_stop_pct=0.07, suggested_target_pct=0.12,
                data=signal.data,
            )

        return None

    def _process_trending(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Token just entered CoinGecko trending — potential momentum play."""
        rank = signal.data.get("rank", 99)
        symbol = signal.symbol

        # Only top 3 trending are worth analyzing
        if rank > 3:
            return None

        # Get price/volume from DexScreener
        dex = fetch_dexscreener_token(symbol)
        price = dex["price_usd"] if dex else 0
        volume = dex["volume_24h"] if dex else 0
        change = dex["price_change_24h"] if dex else 0

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="trending_breakout",
            priority=2, timestamp=signal.timestamp, source="coingecko_trending",
            price_usd=price, volume_24h=volume, price_change_24h=change,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"#{rank} trending on CoinGecko. 24h volume ${volume:,.0f}, change {change:+.1f}%.",
            suggested_side="long" if change > 0 else "",
            suggested_stop_pct=0.08, suggested_target_pct=0.15,
            data=signal.data,
        )

    def _process_major_pump(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Major pump detected (>50% in 24h with volume) — inform Claude for analysis."""
        change = signal.data.get("change_pct", 0)
        volume = signal.data.get("volume_24h", 0)
        symbol = signal.symbol
        price = snapshot.prices.get(symbol, 0)

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="major_pump",
            priority=2, timestamp=signal.timestamp, source="binance_movers",
            price_usd=price, volume_24h=volume, price_change_24h=change,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"{symbol} +{change:.0f}% in 24h with ${volume:,.0f} volume. Evaluate: is momentum continuing or exhausted?",
            data=signal.data,
        )

    def _process_large_move(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Large real-time move detected via WS (>10% change)."""
        change = signal.data.get("change_pct", 0)
        price = signal.data.get("price", 0)
        symbol = signal.symbol

        # Only high-volume moves
        volume = signal.data.get("volume_24h", 0)
        if volume < 5_000_000:
            return None

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="large_move",
            priority=2 if abs(change) > 20 else 1,
            timestamp=signal.timestamp, source="binance_ws",
            price_usd=price, volume_24h=volume, price_change_24h=change,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"{symbol} moved {change:+.1f}% with ${volume:,.0f} volume.",
            suggested_side="long" if change > 0 else "short",
            data=signal.data,
        )
