"""Canonical Binance symbol mapping — single source of truth.

All modules that need to map internal symbols (e.g. "BTC") to Binance
tickers (e.g. "BTCUSDT") should import from here.
"""

from typing import Optional

BINANCE_SYMBOL_MAP: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "UNI": "UNIUSDT",
    "AAVE": "AAVEUSDT",
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
    "DOGE": "DOGEUSDT",
    "MATIC": "MATICUSDT",
    "SUI": "SUIUSDT",
    "APT": "APTUSDT",
    "SEI": "SEIUSDT",
    "INJ": "INJUSDT",
    "FET": "FETUSDT",
    "TIA": "TIAUSDT",
}


def to_binance_ticker(symbol: str) -> Optional[str]:
    """Map internal symbol to Binance ticker, or None if unsupported."""
    return BINANCE_SYMBOL_MAP.get(symbol.upper())
