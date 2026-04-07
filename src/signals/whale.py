"""Whale Alert large transaction fetcher."""

import time

import requests

from src.config import env
from src.signals._circuit_breaker import CircuitBreaker
from src.storage.database import log

MIN_USD = 3_000_000
_seen_tx_ids: set[str] = set()
_last_cursor: int = 0
_last_fetch_at: float = 0
_POLL_INTERVAL_MS = 120_000
_breaker = CircuitBreaker("whale")


def _to_wallet_type(owner_type: str) -> str:
    ot = owner_type.lower()
    if ot == "exchange":
        return "exchange"
    if ot in ("fund", "custodian"):
        return "known_fund"
    if ot == "miner":
        return "miner"
    return "unknown_wallet"


def poll_whale_alerts(symbols: list[str]) -> None:
    global _last_cursor, _last_fetch_at
    if not env.whale_alert_api_key:
        return
    now = time.time() * 1000
    if now - _last_fetch_at < _POLL_INTERVAL_MS:
        return

    # Staleness warning
    if _last_fetch_at > 0 and now - _last_fetch_at > 2 * _POLL_INTERVAL_MS:
        log("warn", f"Whale alert data is stale (last fetch {(now - _last_fetch_at) / 60_000:.0f}m ago)")

    if not _breaker.can_call():
        log("warn", "Whale alert circuit breaker OPEN — skipping poll")
        return

    since = _last_cursor or int((now - 7_200_000) / 1000)
    url = (
        f"https://api.whale-alert.io/v1/transactions"
        f"?api_key={env.whale_alert_api_key}&min_value={MIN_USD}&start={since}&limit=100"
    )

    try:
        res = requests.get(url, timeout=8)
        if res.status_code != 200:
            log("warn", f"Whale Alert fetch failed: {res.status_code}")
            _breaker.record_failure()
            return
        data = res.json()
        if data.get("result") != "success":
            _breaker.record_failure()
            return

        _breaker.record_success()

        # Lazy import to avoid circular dependency
        from src.strategies.whale_tracker import on_whale_transfer

        new_cursor = _last_cursor
        for tx in data.get("transactions", []):
            tx_id = tx["id"]
            if tx_id in _seen_tx_ids:
                continue
            _seen_tx_ids.add(tx_id)

            sym = tx["symbol"].upper()
            if sym not in symbols:
                continue

            new_cursor = max(new_cursor, tx["timestamp"])
            on_whale_transfer({
                "symbol": sym,
                "amount_usd": tx["amount_usd"],
                "from_type": _to_wallet_type(tx["from"]["owner_type"]),
                "to_type": _to_wallet_type(tx["to"]["owner_type"]),
                "known_wallet": tx["from"].get("owner") or tx["to"].get("owner"),
                "ts": tx["timestamp"] * 1000,
            })

            log("info",
                f"Whale: ${tx['amount_usd'] / 1e6:.0f}M {sym} {tx['from']['owner_type']} -> {tx['to']['owner_type']}",
                symbol=sym,
                data={"amount_usd": tx["amount_usd"], "from": tx["from"]["owner_type"], "to": tx["to"]["owner_type"]})

        _last_cursor = new_cursor
        _last_fetch_at = now

        if len(_seen_tx_ids) > 5000:
            keep = list(_seen_tx_ids)[-2000:]
            _seen_tx_ids.clear()
            _seen_tx_ids.update(keep)

    except Exception as err:
        log("warn", f"Whale Alert network error: {err}")
        _breaker.record_failure()
