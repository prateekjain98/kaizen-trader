"""Options sentiment from Deribit — put/call ratio, implied vol, skew.

Only BTC and ETH have liquid options markets on Deribit.
All endpoints are public (no auth needed).
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

_CACHE_TTL_MS = 300_000  # 5 minutes
_API_BASE = "https://www.deribit.com/api/v2/public"

_lock = threading.Lock()
_cache: dict[str, tuple["OptionsSentiment", float]] = {}
_breaker = CircuitBreaker("deribit_options", failure_threshold=3, reset_timeout_s=300)

# Only BTC and ETH have liquid options
_SUPPORTED_CURRENCIES = {"BTC", "ETH"}


@dataclass
class OptionsSentiment:
    symbol: str
    put_call_ratio: float         # > 1 = bearish sentiment
    total_put_oi: float           # total put open interest
    total_call_oi: float          # total call open interest
    implied_vol_avg: float        # average IV across options
    skew_25d: Optional[float]     # 25-delta skew (call IV - put IV); negative = fear
    max_pain_price: Optional[float] = None  # strike where most options expire worthless
    spot_to_max_pain_pct: Optional[float] = None  # (spot - max_pain) / spot * 100


def _compute_max_pain(instruments: list[dict]) -> Optional[float]:
    """Compute max pain price — the strike where option writers have minimum payout.

    For each candidate strike, sum the intrinsic value of all calls and puts.
    The strike that minimizes total payout to option holders is max pain.
    Price tends to gravitate toward max pain near expiry (option writer hedging).
    """
    # Collect strikes with their OI
    strike_data: dict[float, dict] = {}  # strike -> {"call_oi": float, "put_oi": float}

    for inst in instruments:
        try:
            name = inst.get("instrument_name", "")
            strike = _extract_strike(name)
            if strike is None or strike <= 0:
                continue
            oi = float(inst.get("open_interest", 0))
            if oi <= 0:
                continue

            if strike not in strike_data:
                strike_data[strike] = {"call_oi": 0.0, "put_oi": 0.0}

            if "-C" in name:
                strike_data[strike]["call_oi"] += oi
            elif "-P" in name:
                strike_data[strike]["put_oi"] += oi
        except (ValueError, TypeError):
            continue

    if not strike_data:
        return None

    strikes = sorted(strike_data.keys())
    if len(strikes) < 3:
        return None

    # For each candidate settlement price, compute total option holder payout
    min_payout = float("inf")
    max_pain_strike = strikes[len(strikes) // 2]

    for candidate in strikes:
        total_payout = 0.0
        for strike, data in strike_data.items():
            # Call payout: max(0, candidate - strike) * call_oi
            if candidate > strike:
                total_payout += (candidate - strike) * data["call_oi"]
            # Put payout: max(0, strike - candidate) * put_oi
            if candidate < strike:
                total_payout += (strike - candidate) * data["put_oi"]

        if total_payout < min_payout:
            min_payout = total_payout
            max_pain_strike = candidate

    return max_pain_strike


def _fetch_index_price(currency: str) -> Optional[float]:
    """Fetch the current index price from Deribit for max pain distance calc."""
    try:
        resp = requests.get(
            f"{_API_BASE}/get_index_price",
            params={"index_name": f"{currency.lower()}_usd"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        return float(data.get("result", {}).get("index_price", 0))
    except Exception as err:
        log("warn", f"Deribit index price fetch failed for {currency}: {err}")
        return None


def _extract_strike(instrument_name: str) -> Optional[float]:
    """Extract strike price from Deribit instrument name.

    Format: BTC-28MAR25-85000-C or ETH-28MAR25-3500-P
    """
    parts = instrument_name.split("-")
    if len(parts) < 4:
        return None
    try:
        return float(parts[2])
    except (ValueError, IndexError):
        return None


def fetch_options_sentiment(symbol: str) -> Optional[OptionsSentiment]:
    """Fetch options market data from Deribit for BTC or ETH."""
    currency = symbol.upper()
    if currency not in _SUPPORTED_CURRENCIES:
        return None

    now = time.time() * 1000

    with _lock:
        cached = _cache.get(currency)
        if cached and (now - cached[1]) < _CACHE_TTL_MS:
            return cached[0]

    if not _breaker.can_call():
        with _lock:
            c = _cache.get(currency)
            return c[0] if c else None

    try:
        resp = requests.get(
            f"{_API_BASE}/get_book_summary_by_currency",
            params={"currency": currency, "kind": "option"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _breaker.record_success()
    except Exception as err:
        _breaker.record_failure()
        log("warn", f"Deribit options fetch failed for {currency}: {err}", symbol=symbol)
        with _lock:
            c = _cache.get(currency)
            return c[0] if c else None

    result_list = data.get("result", [])
    if not result_list:
        return None

    total_put_oi = 0.0
    total_call_oi = 0.0
    iv_values = []

    for instrument in result_list:
        try:
            name = instrument.get("instrument_name", "")
            oi = float(instrument.get("open_interest", 0))
            iv = instrument.get("mark_iv")

            if "-P" in name:
                total_put_oi += oi
            elif "-C" in name:
                total_call_oi += oi

            if iv is not None and float(iv) > 0:
                iv_values.append(float(iv))
        except (ValueError, TypeError):
            continue

    put_call_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    avg_iv = sum(iv_values) / len(iv_values) if iv_values else 0

    # 25-delta skew approximation: difference between put and call IV
    # Positive skew = puts more expensive = fear/hedging demand
    # We approximate by comparing average put IV vs call IV
    put_ivs = []
    call_ivs = []
    for instrument in result_list:
        try:
            name = instrument.get("instrument_name", "")
            iv = instrument.get("mark_iv")
            if iv is None:
                continue
            iv = float(iv)
            if iv <= 0:
                continue
            if "-P" in name:
                put_ivs.append(iv)
            elif "-C" in name:
                call_ivs.append(iv)
        except (ValueError, TypeError):
            continue

    skew = None
    if put_ivs and call_ivs:
        avg_put_iv = sum(put_ivs) / len(put_ivs)
        avg_call_iv = sum(call_ivs) / len(call_ivs)
        skew = avg_call_iv - avg_put_iv  # negative = puts more expensive = fear

    # Compute max pain
    max_pain = _compute_max_pain(result_list)
    spot_to_mp_pct = None
    if max_pain is not None:
        index_price = _fetch_index_price(currency)
        if index_price and index_price > 0:
            spot_to_mp_pct = ((index_price - max_pain) / index_price) * 100

    sentiment = OptionsSentiment(
        symbol=currency,
        put_call_ratio=put_call_ratio,
        total_put_oi=total_put_oi,
        total_call_oi=total_call_oi,
        implied_vol_avg=avg_iv,
        skew_25d=skew,
        max_pain_price=max_pain,
        spot_to_max_pain_pct=spot_to_mp_pct,
    )

    with _lock:
        _cache[currency] = (sentiment, now)

    return sentiment


def is_options_bearish(symbol: str) -> bool:
    """Quick check: are options markets signaling bearish sentiment?"""
    sent = fetch_options_sentiment(symbol)
    if sent is None:
        return False
    # High put/call ratio + negative skew = bearish
    return sent.put_call_ratio > 1.2 and (sent.skew_25d is not None and sent.skew_25d < -5)
