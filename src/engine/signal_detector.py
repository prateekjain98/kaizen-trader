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
        elif signal.event_type == "liquidation_cascade":
            return self._process_liquidation_cascade(sid, signal, snapshot)
        elif signal.event_type == "orderbook_imbalance":
            return self._process_orderbook_imbalance(sid, signal, snapshot)
        elif signal.event_type == "mempool_stress":
            return self._process_mempool_stress(sid, signal, snapshot)

        return None

    def _process_mempool_stress(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """BTC mempool fee stress — bearish directional signal (BTC only).

        Edge: high on-chain fees correlate with miner sell pressure and
        post-rally retail FOMO tops. Backtest validation pending — the
        collector (scripts/collect_mempool.py) must accumulate ≥7d of history
        before the regime classifier returns anything but 'calm'.
        """
        if signal.symbol != "BTC":
            return None
        d = signal.data or {}
        side = d.get("side_hint", "short")
        regime = d.get("regime", "elevated")
        price = float(snapshot.prices.get("BTC", 0))
        return SignalPacket(
            signal_id=sid, symbol="BTC", signal_type="mempool_stress",
            priority=signal.priority, timestamp=signal.timestamp,
            source=signal.source or "mempool_space",
            price_usd=price,
            volume_24h=snapshot.volumes_24h.get("BTC", 0),
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"BTC mempool fee regime={regime} (fastest={d.get('fastest_fee', 0)} sat/vB) — fade with {side}",
            suggested_side=side,
            suggested_stop_pct=0.04, suggested_target_pct=0.07,
            data=d,
        )

    def _process_orderbook_imbalance(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Filtered OBI mean-reversion (arXiv 2507.22712).

        Trigger: |obi_f_ema| > 0.4 AND prevailing 1h move runs OPPOSITE
        the imbalance (book is loaded for the snap-back). side_hint is
        therefore the imbalance direction itself — long when bids dominate
        a downtrend, short when asks dominate an uptrend.
        """
        d = signal.data or {}
        obi_ema = float(d.get("obi_f_ema", 0.0))
        accel_1h = float(d.get("acceleration_1h", 0.0))
        if abs(obi_ema) <= 0.4:
            return None
        # Mean-reversion gate: imbalance must be opposite the 1h move.
        if obi_ema > 0 and accel_1h >= 0:
            return None
        if obi_ema < 0 and accel_1h <= 0:
            return None
        side_hint = "long" if obi_ema > 0 else "short"
        symbol = signal.symbol
        price = float(d.get("price", 0.0)) or float(snapshot.prices.get(symbol, 0))
        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="orderbook_imbalance",
            priority=1, timestamp=signal.timestamp, source="binance_obi_ws",
            price_usd=price,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=(f"filtered OBI {obi_ema:+.2f} vs 1h move {accel_1h:+.1f}% — "
                       f"mean-revert {side_hint}"),
            suggested_side=side_hint,
            suggested_stop_pct=0.02, suggested_target_pct=0.03,
            data={"obi_f_ema": obi_ema, "acceleration_1h": accel_1h, "price": price},
        )

    def _process_liquidation_cascade(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Liquidation cascade — fade the wick.

        side_hint is set by the emitter to the OPPOSITE of the cascade side:
        forced_long_close (longs liquidated, price wicked down) → suggested_side='long'
        forced_short_close (shorts liquidated, price wicked up)  → suggested_side='short'
        Brain reads suggested_side directly; field-name discipline per audit.
        """
        d = signal.data or {}
        side_hint = d.get("side_hint", "long")
        liq_usd = float(d.get("liq_usd_5m", 0.0))
        tier = d.get("tier", "small_alt")
        cascade_event = d.get("cascade_event", "")
        price = float(snapshot.prices.get(signal.symbol, 0))
        return SignalPacket(
            signal_id=sid, symbol=signal.symbol, signal_type="liquidation_cascade",
            priority=signal.priority, timestamp=signal.timestamp,
            source=signal.source or "binance_liq_ws",
            price_usd=price,
            volume_24h=snapshot.volumes_24h.get(signal.symbol, 0),
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"Liq cascade {cascade_event} ${liq_usd:,.0f}/5m on {signal.symbol} ({tier}) — fade with {side_hint}",
            suggested_side=side_hint,
            suggested_stop_pct=0.04, suggested_target_pct=0.09,
            data=d,
        )

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
            # P0 audit fix: pass full upstream data through. Prior truncated
            # dict killed the brain's accel_1h (+50/+30) and btc_divergence
            # (+20) bonuses for ALL carry signals — only rank-based scoring
            # could fire. Carry-specific keys layered ON TOP for precedence.
            data={**d, "funding_rate": rate, "funding_rank_pct": rank, "mark_price": price},
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
        """Token just entered CoinGecko trending — potential momentum play.

        P0 audit fix below: skip when 24h change <= 0. Prior code emitted with
        suggested_side="" for negative-change tokens; brain's `or _strat_default`
        fallback then forced a LONG entry on a falling token. Trending without
        positive momentum is a fade candidate, not a long-momentum play.
        """
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
        # P0: drop trending entries without positive momentum
        if change <= 0:
            return None

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="trending_breakout",
            priority=2, timestamp=signal.timestamp, source="coingecko_trending",
            price_usd=price, volume_24h=volume, price_change_24h=change,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"#{rank} trending on CoinGecko. 24h volume ${volume:,.0f}, change {change:+.1f}%.",
            suggested_side="long",
            suggested_stop_pct=0.08, suggested_target_pct=0.15,
            data=signal.data,
        )

    def _process_major_pump(self, sid: str, signal: TokenSignal, snapshot: MarketSnapshot) -> Optional[SignalPacket]:
        """Major pump detected (>50% in 24h with volume) — inform Claude for analysis.

        P1 audit fix: emit explicit suggested_side based on continuation vs
        exhaustion. Prior code omitted suggested_side, making brain default
        to long via `_strat_default`. Now: continuing momentum (positive
        change + accel) → long; otherwise fade short. Future tuning may
        gate this with an upstream filter.
        """
        change = signal.data.get("change_pct", 0)
        volume = signal.data.get("volume_24h", 0)
        accel_1h = signal.data.get("acceleration_1h", 0)
        symbol = signal.symbol
        price = snapshot.prices.get(symbol, 0)

        return SignalPacket(
            signal_id=sid, symbol=symbol, signal_type="major_pump",
            priority=2, timestamp=signal.timestamp, source="binance_movers",
            price_usd=price, volume_24h=volume, price_change_24h=change,
            fear_greed_index=snapshot.fear_greed_index,
            reasoning=f"{symbol} +{change:.0f}% in 24h with ${volume:,.0f} volume. Evaluate: is momentum continuing or exhausted?",
            suggested_side="long" if (change > 0 and accel_1h > 5) else "short",
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
