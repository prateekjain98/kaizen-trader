"""Historical aggTrade loader for tick-accurate backtest CVD.

Goal: replay Binance Futures aggTrade data offline so backtests measure CVD
with the SAME granularity prod gets via WS. Today the backtest derives CVD
from kline taker-buy columns aggregated across hourly bars — that's two
orders of magnitude coarser than prod's tick-by-tick tape, which is the
single biggest cause of CVD-filter divergence between paper and live.

This file is a SKELETON. Wiring is a follow-up; the goal here is just to
nail down the data shape and the two-tier source strategy so the rest of
the codebase can target stable types.

Sources (two-tier):

1. REST `/fapi/v1/aggTrades` (recent ~3 days only)
   - https://binance-docs.github.io/apidocs/futures/en/#compressed-aggregate-trades-list
   - 1000 trades per request max; paginate via `fromId`.
   - Use for: ad-hoc replay near `now`, integration tests, last-72h windows.

2. data.binance.vision daily archives (full history)
   - https://data.binance.vision/?prefix=data/futures/um/daily/aggTrades/<SYMBOL>/
   - One zipped CSV per UTC day per symbol, ~10-200 MB compressed.
   - Use for: walk-forward, multi-week backtests, calibration runs.

Both sources should produce the SAME `AggTrade` records so callers don't
care which tier supplied the data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AggTrade:
    """One Binance Futures aggregated trade.

    Mirrors the WS aggTrade payload so cvd_tracker semantics carry over 1:1:
      `is_buyer_maker = True`  → seller-initiated (sell hit bid)  → signed USD negative
      `is_buyer_maker = False` → buyer-initiated  (buy lifted ask) → signed USD positive
    """
    symbol: str
    ts_ms: int
    price: float
    qty: float
    is_buyer_maker: bool

    @property
    def signed_usd(self) -> float:
        """Convenience: signed flow in USD with prod's sign convention."""
        usd = self.price * self.qty
        return -usd if self.is_buyer_maker else usd


def fetch_recent(symbol: str, start_ms: int, end_ms: int) -> list[AggTrade]:
    """REST tier: pull aggTrades via `/fapi/v1/aggTrades`.

    TODO:
      - Paginate using `fromId` (the response's last `a` field + 1).
      - Respect Binance weight: each call is weight 20, watch X-MBX-USED-WEIGHT.
      - Cap lookback to ~3 days; the endpoint returns empty beyond that.
      - Cache responses keyed by (symbol, fromId) to disk to survive reruns.
    """
    raise NotImplementedError("aggtrade_loader.fetch_recent — follow-up wiring")


def fetch_daily_archive(symbol: str, date_str: str) -> list[AggTrade]:
    """S3 tier: pull a daily zipped CSV from data.binance.vision.

    URL pattern:
      https://data.binance.vision/data/futures/um/daily/aggTrades/{SYMBOL}USDT/
        {SYMBOL}USDT-aggTrades-{YYYY-MM-DD}.zip

    TODO:
      - Stream the zip (don't fully buffer; some days are >200MB compressed).
      - CSV columns: agg_trade_id, price, quantity, first_trade_id,
        last_trade_id, transact_time, is_buyer_maker.
      - Cache the unzipped CSV under data/aggtrades/{symbol}/{date}.csv so
        re-runs are free.
      - Verify checksum file alongside the zip if available.
    """
    raise NotImplementedError("aggtrade_loader.fetch_daily_archive — follow-up wiring")


def load_window(symbol: str, start_ms: int, end_ms: int) -> list[AggTrade]:
    """High-level entry: choose REST vs archive based on age of `start_ms`.

    Strategy:
      - If end_ms is within the last 3 days → fetch_recent.
      - Else → enumerate UTC days in [start_ms, end_ms] and concat archive
        pulls, then trim to the exact ms window.

    TODO: implement once fetch_recent and fetch_daily_archive are real.
    """
    raise NotImplementedError("aggtrade_loader.load_window — follow-up wiring")
