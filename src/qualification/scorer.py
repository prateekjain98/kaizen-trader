"""Multi-signal qualification scorer."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.types import TradeSignal, MarketContext, ScannerConfig
from src.config import env
from src.signals.news import NewsSentiment
from src.signals.social import SocialSentiment
from src.signals.options import OptionsSentiment
from src.signals.stablecoin import StablecoinFlows
from src.signals.derivatives import DerivativesData
from src.indicators.cvd import CVDSnapshot
from src.indicators.regime import RegimeSnapshot
from src.storage.database import log
from src.utils.safe_math import safe_score


@dataclass
class QualificationResult:
    score: float
    passed: bool
    breakdown: dict
    reasoning: str




def _news_adjustment(signal: TradeSignal, news: Optional[NewsSentiment]) -> float:
    if not news:
        return 0
    direction_match = news.score if signal.side == "long" else -news.score
    velocity = min(5, (news.velocity_ratio - 2) * 2.5) if news.velocity_ratio > 2 else 0
    return safe_score(direction_match * 12 + velocity, -15, 15)


def _social_adjustment(signal: TradeSignal, social: Optional[SocialSentiment]) -> float:
    """Enhanced social scoring with sentiment breakdown and rank momentum."""
    if not social:
        return 0

    adj = 0.0

    # Galaxy score component
    if social.galaxy_score >= 70:
        adj += 5
    elif social.galaxy_score <= 30:
        adj -= 5

    # Velocity component
    if social.velocity_multiple >= 3:
        adj += 7
    elif social.velocity_multiple >= 2:
        adj += 3

    # Sentiment breakdown
    # If >70% negative sentiment, penalize long entries by 5
    if social.negative_pct > 70 and signal.side == "long":
        adj -= 5
    # If >70% positive with social volume, boost
    if social.positive_pct > 70 and social.social_volume > 0:
        adj += 3

    # AltRank momentum (negative change = improving)
    if social.alt_rank_change_24h < -20:  # Significant rank improvement
        adj += 4
    elif social.alt_rank_change_24h > 20:  # Rank declining
        adj -= 3

    # Social volume trend
    if social.social_volume_24h_change > 100:  # Volume doubled
        adj += 3
    elif social.social_volume_24h_change < -50:  # Volume halved
        adj -= 2

    return safe_score(adj, -12, 12)


def _context_adjustment(signal: TradeSignal, ctx: MarketContext) -> float:
    adj = 0.0
    phase = ctx.phase
    if phase == "bull":
        adj = 8 if signal.side == "long" else -5
    elif phase == "bear":
        adj = -8 if signal.side == "long" else 8
    elif phase == "extreme_greed":
        adj = -5 if signal.side == "long" else 5
    elif phase == "extreme_fear":
        adj = 3 if signal.side == "long" else -3
    if ctx.btc_dominance > 55 and signal.side == "long" and signal.symbol != "BTC":
        adj -= 3
    return safe_score(adj, -10, 10)


def _fear_greed_adjustment(signal: TradeSignal, fgi: float) -> float:
    if signal.side == "long":
        if fgi < 30:
            return 6
        if fgi > 75:
            return -5
    else:
        if fgi > 70:
            return 6
        if fgi < 25:
            return -5
    return 0


def _cvd_adjustment(signal: TradeSignal, cvd: Optional[CVDSnapshot]) -> float:
    """CVD divergence signals — the most reliable short-term reversal indicator."""
    if not cvd:
        return 0
    adj = 0.0
    # Positive divergence score = price up but CVD down (bearish)
    # Negative divergence score = price down but CVD up (bullish)
    if cvd.divergence_score > 0.3 and signal.side == "long":
        adj -= 6  # bearish divergence, penalize longs
    elif cvd.divergence_score < -0.3 and signal.side == "long":
        adj += 5  # bullish divergence, boost longs
    elif cvd.divergence_score > 0.3 and signal.side == "short":
        adj += 5  # bearish divergence, boost shorts
    elif cvd.divergence_score < -0.3 and signal.side == "short":
        adj -= 6  # bullish divergence, penalize shorts

    # Strong buy/sell pressure confirmation
    if cvd.buy_volume_1m > 0 and cvd.sell_volume_1m > 0:
        ratio = cvd.buy_volume_1m / cvd.sell_volume_1m
        if ratio > 2.0 and signal.side == "long":
            adj += 3  # strong buy pressure confirms long
        elif ratio < 0.5 and signal.side == "short":
            adj += 3  # strong sell pressure confirms short

    return safe_score(adj, -8, 8)


def _regime_adjustment(signal: TradeSignal, regime: Optional[RegimeSnapshot]) -> float:
    """Adjust score based on market regime classification."""
    if not regime or regime.trend == "unknown":
        return 0
    adj = 0.0

    # Mean reversion strategies should be penalized in strong trends
    if signal.strategy in ("mean_reversion", "fear_greed_contrarian"):
        if regime.trend in ("trending_up", "trending_down") and regime.trend_strength > 30:
            adj -= 8  # avoid mean reversion in strong trends

    # Momentum strategies boosted in trending markets
    if signal.strategy in ("momentum_swing", "momentum_scalp"):
        if regime.trend == "trending_up" and signal.side == "long":
            adj += 5
        elif regime.trend == "trending_down" and signal.side == "short":
            adj += 5
        elif regime.trend == "ranging":
            adj -= 4  # momentum struggles in ranging markets

    # Bollinger squeeze = breakout imminent, boost momentum
    if regime.bb_squeeze and signal.strategy in ("momentum_swing", "momentum_scalp"):
        adj += 4

    # Volatility filter: penalize entries in extreme low vol (nothing moves)
    if regime.volatility == "low_vol" and not regime.bb_squeeze:
        adj -= 3  # low vol without squeeze = dead market
    # High vol: penalize scalps (whipsaws) but boost mean reversion
    if regime.volatility == "high_vol":
        if signal.strategy in ("momentum_scalp",):
            adj -= 4  # scalps get whipsawed in high vol
        if signal.strategy in ("mean_reversion", "fear_greed_contrarian"):
            adj += 3  # mean reversion thrives in high vol ranging

    return safe_score(adj, -10, 10)


def _options_adjustment(signal: TradeSignal, options: Optional[OptionsSentiment]) -> float:
    """Options market sentiment — put/call ratio, skew, and max pain gravity."""
    if not options:
        return 0
    adj = 0.0

    # High put/call ratio = hedging demand = bearish
    if options.put_call_ratio > 1.3:
        adj += -4 if signal.side == "long" else 4
    elif options.put_call_ratio < 0.7:
        adj += 4 if signal.side == "long" else -4

    # Negative skew (puts expensive) = fear
    if options.skew_25d is not None:
        if options.skew_25d < -10:
            adj += -3 if signal.side == "long" else 3
        elif options.skew_25d > 10:
            adj += 3 if signal.side == "long" else -3

    # Max pain gravity: price tends to gravitate toward max pain near expiry
    # If spot is far above max pain, longs face headwind (price pulled down)
    # If spot is far below max pain, shorts face headwind (price pulled up)
    if options.spot_to_max_pain_pct is not None:
        distance = options.spot_to_max_pain_pct
        if distance > 5:  # spot >5% above max pain
            adj += -3 if signal.side == "long" else 3
        elif distance < -5:  # spot >5% below max pain
            adj += 3 if signal.side == "long" else -3

    return safe_score(adj, -8, 8)


def _derivatives_adjustment(signal: TradeSignal, deriv: Optional[DerivativesData]) -> float:
    """Futures basis and funding — detect overheated markets."""
    if not deriv:
        return 0
    adj = 0.0

    # High positive basis + positive funding = crowded longs
    if deriv.futures_basis_pct > 0.5 and deriv.funding_rate > 0.0005:
        adj += -5 if signal.side == "long" else 5  # fade the crowd
    # Negative basis + negative funding = crowded shorts
    elif deriv.futures_basis_pct < -0.3 and deriv.funding_rate < -0.0005:
        adj += 5 if signal.side == "long" else -5  # fade the crowd

    return safe_score(adj, -6, 6)


def _leverage_profile_adjustment(signal: TradeSignal, deriv: Optional[DerivativesData]) -> float:
    """Leverage bracket analysis — detect retail crowding and liquidation cascade risk.

    When retail traders (global ratio) are heavily skewed vs top traders,
    it signals overleveraged retail positions that are liquidation fuel.
    """
    if not deriv or not deriv.leverage_profile:
        return 0
    lp = deriv.leverage_profile
    adj = 0.0

    # Top traders diverging from global = smart money positioning differently
    # If top traders are more short than global → retail is overleveraged long
    if lp.top_trader_long_ratio < 0.45 and lp.high_leverage_long_pct > 5:
        # Smart money short, retail long → bearish for longs
        adj += -5 if signal.side == "long" else 5
    # If top traders are more long than global → retail is overleveraged short
    elif lp.top_trader_short_ratio < 0.45 and lp.high_leverage_short_pct > 5:
        # Smart money long, retail short → bullish for longs
        adj += 5 if signal.side == "long" else -5

    # Extreme one-sided positioning (>65% on one side globally)
    global_bracket = next((b for b in lp.brackets if b.bracket == "global"), None)
    if global_bracket:
        if global_bracket.long_ratio > 0.65:
            adj += -3 if signal.side == "long" else 3  # crowded longs = liq risk
        elif global_bracket.short_ratio > 0.65:
            adj += 3 if signal.side == "long" else -3  # crowded shorts = squeeze risk

    return safe_score(adj, -6, 6)


def _oi_funding_composite(signal: TradeSignal, deriv: Optional[DerivativesData]) -> float:
    """OI + Funding composite — detect liquidation risk and crowded positioning.

    Rising OI + extreme positive funding = overleveraged longs, high liquidation risk.
    Rising OI + extreme negative funding = overleveraged shorts.
    This is the #1 derivatives signal top traders use.
    """
    if not deriv:
        return 0
    adj = 0.0

    # High OI + extreme funding = overleveraged, liquidation risk
    # Use relative OI threshold — $500M absolute only works for BTC/ETH
    # For altcoins, even $50M OI is significant
    high_oi = (deriv.open_interest_usd > 500_000_000
               or (deriv.open_interest_usd > 50_000_000
                   and signal.symbol not in ("BTC", "ETH")))
    high_positive_funding = deriv.funding_rate > 0.0008  # very positive
    high_negative_funding = deriv.funding_rate < -0.0008  # very negative

    if high_oi and high_positive_funding:
        # Crowded longs with high leverage — penalize longs, boost shorts
        adj += -7 if signal.side == "long" else 7
    elif high_oi and high_negative_funding:
        # Crowded shorts with high leverage — boost longs, penalize shorts
        adj += 7 if signal.side == "long" else -7

    # Extreme funding without high OI = less conviction
    if not high_oi and (high_positive_funding or high_negative_funding):
        adj += -3 if signal.side == "long" and high_positive_funding else 0
        adj += 3 if signal.side == "long" and high_negative_funding else 0

    return safe_score(adj, -8, 8)


def _exchange_flow_adjustment(signal: TradeSignal) -> float:
    """Macro exchange flow signal — net whale movements to/from exchanges.

    Net outflows = accumulation = bullish for longs.
    Net inflows = distribution = bearish for longs.
    """
    try:
        from src.strategies.whale_tracker import get_net_exchange_flow
        flows = get_net_exchange_flow()
    except ImportError:
        return 0
    except Exception as err:
        log("warn", f"Exchange flow adjustment failed: {err}")
        return 0

    net = flows.get("net_flow_usd", 0)
    if flows.get("symbols_tracked", 0) < 2:
        return 0  # insufficient data

    adj = 0.0
    # Significant outflows (>$20M) = bullish
    if net > 20_000_000:
        adj += 4 if signal.side == "long" else -3
    # Significant inflows (>$20M) = bearish
    elif net < -20_000_000:
        adj += -4 if signal.side == "long" else 3

    return safe_score(adj, -5, 5)


def _stablecoin_adjustment(signal: TradeSignal, flows: Optional[StablecoinFlows]) -> float:
    """Stablecoin capital flows — macro-level liquidity signal."""
    if not flows:
        return 0
    adj = 0.0

    # Capital flowing in (stablecoin supply growing) = bullish
    if flows.mcap_change_24h_pct > 0.1:
        adj += 3 if signal.side == "long" else -2
    # Capital flowing out = bearish
    elif flows.mcap_change_24h_pct < -0.1:
        adj += -3 if signal.side == "long" else 2

    return safe_score(adj, -4, 4)


def _time_of_day_adjustment(signal: TradeSignal) -> float:
    """Adjust score based on time-of-day liquidity and volatility patterns.

    High-volume windows (US/EU overlap 14-21 UTC):
      - Momentum strategies get a boost (breakouts more reliable)
    Low-volume windows (02-06 UTC):
      - Momentum penalized (more false breakouts)
      - Mean reversion boosted (markets range more)
    Funding settlement windows (±30min of 00:00, 08:00, 16:00 UTC):
      - Funding extreme strategy gets a boost (settlement squeeze)
    """
    now_utc = datetime.now(timezone.utc)
    utc_hour = now_utc.hour
    utc_minute = now_utc.minute
    adj = 0.0

    momentum_strategies = ("momentum_swing", "momentum_scalp")
    mean_rev_strategies = ("mean_reversion", "fear_greed_contrarian")

    # US/EU high-volume overlap (14:00-21:00 UTC)
    if 14 <= utc_hour <= 20:
        if signal.strategy in momentum_strategies:
            adj += 3  # breakouts more reliable in high volume
    # Asian low-volume window (02:00-06:00 UTC)
    elif 2 <= utc_hour <= 5:
        if signal.strategy in momentum_strategies:
            adj -= 3  # false breakouts in thin markets
        if signal.strategy in mean_rev_strategies:
            adj += 2  # ranging markets favor mean reversion

    # Funding settlement timing (00:00, 08:00, 16:00 UTC ±30 min)
    if signal.strategy == "funding_extreme":
        settlement_hours = (0, 8, 16)
        for sh in settlement_hours:
            # Check if within 30 min before settlement
            minutes_to_settlement = (sh * 60 - (utc_hour * 60 + utc_minute)) % (24 * 60)
            if minutes_to_settlement <= 30 or minutes_to_settlement >= (24 * 60 - 5):
                adj += 4  # entering just before settlement captures the squeeze
                break

    return safe_score(adj, -5, 5)


def _unlock_risk_adjustment(signal: TradeSignal, has_unlock_risk: bool) -> float:
    """Penalize longs on symbols with large upcoming token unlocks."""
    if not has_unlock_risk:
        return 0
    if signal.side == "long":
        return -8  # significant supply pressure incoming
    return 3  # unlocks can be a short catalyst


def qualify(
    signal: TradeSignal, ctx: MarketContext, config: ScannerConfig,
    news: Optional[NewsSentiment] = None,
    social: Optional[SocialSentiment] = None,
    cvd: Optional[CVDSnapshot] = None,
    regime: Optional[RegimeSnapshot] = None,
    options: Optional[OptionsSentiment] = None,
    derivatives: Optional[DerivativesData] = None,
    stablecoin: Optional[StablecoinFlows] = None,
    has_unlock_risk: bool = False,
) -> QualificationResult:
    # Fee-aware filter: reject signals where target-to-stop ratio is structurally poor
    round_trip_fee_pct = env.commission_per_side * 2
    if signal.entry_price > 0 and signal.stop_price and signal.stop_price > 0:
        stop_distance_pct = abs(signal.entry_price - signal.stop_price) / signal.entry_price
        # If stop is less than 3x the round-trip fee, the risk/reward is structurally poor
        if stop_distance_pct < round_trip_fee_pct * 3:
            # But allow if target gives sufficient reward (R:R > 2)
            target_distance = 0.0
            if signal.target_price and signal.target_price > 0:
                target_distance = abs(signal.target_price - signal.entry_price) / signal.entry_price
            if target_distance < stop_distance_pct * 2:
                return QualificationResult(
                    score=0, passed=False,
                    breakdown={"fee_filter": f"stop {stop_distance_pct:.2%} < 3x fees {round_trip_fee_pct*3:.2%}"},
                    reasoning=f"REJECTED: stop distance {stop_distance_pct:.2%} too close to fees {round_trip_fee_pct:.2%}",
                )

    news_adj = _news_adjustment(signal, news)
    social_adj = _social_adjustment(signal, social)
    ctx_adj = _context_adjustment(signal, ctx)
    fgi_adj = _fear_greed_adjustment(signal, ctx.fear_greed_index)
    cvd_adj = _cvd_adjustment(signal, cvd)
    regime_adj = _regime_adjustment(signal, regime)
    options_adj = _options_adjustment(signal, options)
    deriv_adj = _derivatives_adjustment(signal, derivatives)
    oi_funding_adj = _oi_funding_composite(signal, derivatives)

    # Cap combined regime + derivatives/OI penalty to avoid double-penalizing the same condition
    _regime_deriv_combined = regime_adj + deriv_adj + oi_funding_adj
    if _regime_deriv_combined < -12:
        # Only scale negative components to avoid flipping positive adjustments negative
        neg_sum = sum(v for v in (regime_adj, deriv_adj, oi_funding_adj) if v < 0)
        if neg_sum < 0:
            _scale = (-12 - _regime_deriv_combined + neg_sum) / neg_sum
            _scale = max(0, min(1, _scale))
            if regime_adj < 0:
                regime_adj *= _scale
            if deriv_adj < 0:
                deriv_adj *= _scale
            if oi_funding_adj < 0:
                oi_funding_adj *= _scale
    leverage_adj = _leverage_profile_adjustment(signal, derivatives)
    flow_adj = _exchange_flow_adjustment(signal)
    stable_adj = _stablecoin_adjustment(signal, stablecoin)
    unlock_adj = _unlock_risk_adjustment(signal, has_unlock_risk)
    tod_adj = _time_of_day_adjustment(signal)

    # Hourly strategy performance adjustment
    from src.evaluation.hourly_stats import get_hour_adjustment
    hour_adj = get_hour_adjustment(signal.strategy)

    raw_score = (signal.score + news_adj + social_adj + ctx_adj + fgi_adj
                 + cvd_adj + regime_adj + options_adj + deriv_adj + oi_funding_adj
                 + leverage_adj + stable_adj + unlock_adj + tod_adj + hour_adj + flow_adj)
    score = safe_score(raw_score, 0, 100)

    min_score = config.min_qual_score_scalp if signal.tier == "scalp" else config.min_qual_score_swing
    passed = score >= min_score

    parts = [f"base={signal.score}"]
    for name, val in [("news", news_adj), ("social", social_adj), ("ctx", ctx_adj),
                      ("fgi", fgi_adj), ("cvd", cvd_adj), ("regime", regime_adj),
                      ("opts", options_adj), ("deriv", deriv_adj),
                      ("oi_fund", oi_funding_adj), ("leverage", leverage_adj),
                      ("flow", flow_adj),
                      ("stable", stable_adj), ("unlock", unlock_adj),
                      ("tod", tod_adj), ("hourly", hour_adj)]:
        if val != 0:
            parts.append(f"{name}{'+' if val > 0 else ''}{val:.0f}")
    parts.append(f"= {score:.0f} (min {min_score})")

    return QualificationResult(
        score=score, passed=passed,
        breakdown={
            "base": signal.score, "news_adjustment": news_adj,
            "social_adjustment": social_adj, "context_adjustment": ctx_adj,
            "fear_greed_adjustment": fgi_adj, "cvd_adjustment": cvd_adj,
            "regime_adjustment": regime_adj, "options_adjustment": options_adj,
            "derivatives_adjustment": deriv_adj,
            "oi_funding_composite": oi_funding_adj,
            "leverage_profile_adjustment": leverage_adj,
            "exchange_flow_adjustment": flow_adj,
            "stablecoin_adjustment": stable_adj,
            "unlock_risk_adjustment": unlock_adj,
            "time_of_day_adjustment": tod_adj,
            "hourly_perf_adjustment": hour_adj,
        },
        reasoning=" ".join(parts),
    )
