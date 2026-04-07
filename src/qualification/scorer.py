"""Multi-signal qualification scorer."""

from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal, MarketContext, ScannerConfig
from src.signals.news import NewsSentiment
from src.signals.social import SocialSentiment
from src.signals.options import OptionsSentiment
from src.signals.stablecoin import StablecoinFlows
from src.signals.derivatives import DerivativesData
from src.indicators.cvd import CVDSnapshot
from src.indicators.regime import RegimeSnapshot
from src.utils.safe_math import safe_score


@dataclass
class QualificationResult:
    score: float
    passed: bool
    breakdown: dict
    reasoning: str


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _news_adjustment(signal: TradeSignal, news: Optional[NewsSentiment]) -> float:
    if not news:
        return 0
    direction_match = news.score if signal.side == "long" else -news.score
    velocity = min(5, (news.velocity_ratio - 2) * 2.5) if news.velocity_ratio > 2 else 0
    return _clamp(direction_match * 12 + velocity, -15, 15)


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

    return _clamp(adj, -12, 12)


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
    return _clamp(adj, -10, 10)


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

    return _clamp(adj, -8, 8)


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

    return _clamp(adj, -10, 10)


def _options_adjustment(signal: TradeSignal, options: Optional[OptionsSentiment]) -> float:
    """Options market sentiment — put/call ratio and skew."""
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

    return _clamp(adj, -6, 6)


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

    return _clamp(adj, -6, 6)


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

    return _clamp(adj, -4, 4)


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
    news_adj = _news_adjustment(signal, news)
    social_adj = _social_adjustment(signal, social)
    ctx_adj = _context_adjustment(signal, ctx)
    fgi_adj = _fear_greed_adjustment(signal, ctx.fear_greed_index)
    cvd_adj = _cvd_adjustment(signal, cvd)
    regime_adj = _regime_adjustment(signal, regime)
    options_adj = _options_adjustment(signal, options)
    deriv_adj = _derivatives_adjustment(signal, derivatives)
    stable_adj = _stablecoin_adjustment(signal, stablecoin)
    unlock_adj = _unlock_risk_adjustment(signal, has_unlock_risk)

    raw_score = (signal.score + news_adj + social_adj + ctx_adj + fgi_adj
                 + cvd_adj + regime_adj + options_adj + deriv_adj + stable_adj + unlock_adj)
    score = safe_score(raw_score, 0, 100)

    min_score = config.min_qual_score_scalp if signal.tier == "scalp" else config.min_qual_score_swing
    passed = score >= min_score

    parts = [f"base={signal.score}"]
    for name, val in [("news", news_adj), ("social", social_adj), ("ctx", ctx_adj),
                      ("fgi", fgi_adj), ("cvd", cvd_adj), ("regime", regime_adj),
                      ("opts", options_adj), ("deriv", deriv_adj),
                      ("stable", stable_adj), ("unlock", unlock_adj)]:
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
            "derivatives_adjustment": deriv_adj, "stablecoin_adjustment": stable_adj,
            "unlock_risk_adjustment": unlock_adj,
        },
        reasoning=" ".join(parts),
    )
