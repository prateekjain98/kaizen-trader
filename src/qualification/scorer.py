"""Multi-signal qualification scorer."""

from dataclasses import dataclass
from typing import Optional

from src.types import TradeSignal, MarketContext, ScannerConfig
from src.signals.news import NewsSentiment
from src.signals.social import SocialSentiment


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
    if not social:
        return 0
    galaxy = (social.galaxy_score - 50) / 50 * 8
    velocity_bonus = min(4, (social.velocity_multiple - 3) * 2) if social.velocity_multiple > 3 else 0
    raw = galaxy + velocity_bonus if signal.side == "long" else -(galaxy + velocity_bonus)
    return _clamp(raw, -12, 12)


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


def qualify(
    signal: TradeSignal, ctx: MarketContext, config: ScannerConfig,
    news: Optional[NewsSentiment] = None,
    social: Optional[SocialSentiment] = None,
) -> QualificationResult:
    news_adj = _news_adjustment(signal, news)
    social_adj = _social_adjustment(signal, social)
    ctx_adj = _context_adjustment(signal, ctx)
    fgi_adj = _fear_greed_adjustment(signal, ctx.fear_greed_index)

    raw_score = signal.score + news_adj + social_adj + ctx_adj + fgi_adj
    score = _clamp(raw_score, 0, 100)

    min_score = config.min_qual_score_scalp if signal.tier == "scalp" else config.min_qual_score_swing
    passed = score >= min_score

    parts = [f"base={signal.score}"]
    if news_adj != 0:
        parts.append(f"news{'+' if news_adj > 0 else ''}{news_adj:.0f}")
    if social_adj != 0:
        parts.append(f"social{'+' if social_adj > 0 else ''}{social_adj:.0f}")
    if ctx_adj != 0:
        parts.append(f"ctx{'+' if ctx_adj > 0 else ''}{ctx_adj:.0f}")
    if fgi_adj != 0:
        parts.append(f"fgi{'+' if fgi_adj > 0 else ''}{fgi_adj:.0f}")
    parts.append(f"= {score:.0f} (min {min_score})")

    return QualificationResult(
        score=score, passed=passed,
        breakdown={
            "base": signal.score, "news_adjustment": news_adj,
            "social_adjustment": social_adj, "context_adjustment": ctx_adj,
            "fear_greed_adjustment": fgi_adj,
        },
        reasoning=" ".join(parts),
    )
