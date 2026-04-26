#!/usr/bin/env python3
"""Stop-loss watchdog — exchange-agnostic safety net.

Runs as a separate process from the main trading engine. Polls open positions
every 30s and closes anything that hits a configured stop-loss or take-profit.
Decoupled from the brain so it survives engine crashes / restarts / deploys.

Selects the exchange from EXCHANGE env var (binance | okx) — same convention as
the main engine. OKX path uses the V5 API with HMAC-SHA256 signing and the
account's posMode (auto-detected) to pick reduceOnly vs posSide on closes.

Custom stops can be supplied via /tmp/watchdog_stops.json:
    {"BTC": {"stop": 0.10, "target": 0.30}, ...}
"""

import base64
import hashlib
import hmac
import json
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

EXCHANGE = os.environ.get("EXCHANGE", "binance").lower()
STOPS_FILE = Path("/tmp/watchdog_stops.json")
DEFAULT_STOP = 0.15   # 15% stop loss
DEFAULT_TARGET = 0.40  # 40% take profit
POLL_INTERVAL = 30.0


def _log(msg: str) -> None:
    """Single-line stdout log; consumed by journalctl when run as systemd."""
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [watchdog] {msg}", flush=True)


# ─── Binance Futures backend ────────────────────────────────────────────────

class BinanceWatchdog:
    """USDM Futures (fapi.binance.com). 1x leverage, market closes."""

    BASE = "https://fapi.binance.com"

    def __init__(self) -> None:
        self.key = os.environ["BINANCE_API_KEY"]
        self.secret = os.environ["BINANCE_API_SECRET"]
        self._step_sizes: dict[str, float] = {}
        self._load_exchange_info()

    def _load_exchange_info(self) -> None:
        try:
            data = requests.get(f"{self.BASE}/fapi/v1/exchangeInfo", timeout=10).json()
            for s in data.get("symbols", []):
                if s["symbol"].endswith("USDT"):
                    sym = s["symbol"].replace("USDT", "")
                    for f in s.get("filters", []):
                        if f["filterType"] == "LOT_SIZE":
                            self._step_sizes[sym] = float(f["stepSize"])
            _log(f"binance: loaded {len(self._step_sizes)} symbol step sizes")
        except Exception as e:
            _log(f"binance exchange info failed: {e}")

    def _round_qty(self, symbol: str, qty: float) -> float:
        step = self._step_sizes.get(symbol, 0.001)
        if step <= 0:
            return round(qty, 3)
        precision = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(math.floor(qty / step) * step, precision)

    def _signed(self, params: str) -> str:
        return hmac.new(self.secret.encode(), params.encode(), "sha256").hexdigest()

    def positions(self) -> list[dict]:
        ts = int(time.time() * 1000)
        params = f"timestamp={ts}"
        sig = self._signed(params)
        # /fapi/v2/positionRisk has markPrice in the response (per-symbol position risk).
        # /fapi/v2/account does NOT include markPrice, only entryPrice + unrealizedProfit.
        # The previous code queried /fapi/v2/account and required mark>0, which always
        # failed silently and made the watchdog blind to every open position.
        r = requests.get(
            f"{self.BASE}/fapi/v2/positionRisk",
            headers={"X-MBX-APIKEY": self.key},
            params=params + "&signature=" + sig,
            timeout=10,
        )
        if r.status_code != 200:
            _log(f"binance positionRisk fetch failed: {r.status_code} {r.text[:200]}")
            return []
        out = []
        for p in r.json():
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            entry = float(p.get("entryPrice", 0))
            mark = float(p.get("markPrice", 0))
            if entry <= 0 or mark <= 0:
                continue
            symbol = p["symbol"].replace("USDT", "")
            side = "long" if amt > 0 else "short"
            pnl_pct = (mark - entry) / entry if side == "long" else (entry - mark) / entry
            out.append({
                "symbol": symbol, "qty": abs(amt), "entry": entry,
                "mark": mark, "side": side, "pnl_pct": pnl_pct,
                "upnl": float(p.get("unRealizedProfit", 0)),  # note: positionRisk uses unRealizedProfit (capital R)
            })
        return out

    def close(self, symbol: str, qty: float, side: str) -> bool:
        # set leverage 1x first (idempotent)
        ts = int(time.time() * 1000)
        params = f"symbol={symbol}USDT&leverage=1&timestamp={ts}"
        sig = self._signed(params)
        try:
            requests.post(
                f"{self.BASE}/fapi/v1/leverage",
                headers={"X-MBX-APIKEY": self.key},
                data=f"{params}&signature={sig}", timeout=5,
            )
        except Exception:
            pass

        rounded = self._round_qty(symbol, qty)
        if rounded <= 0:
            _log(f"binance close {symbol}: rounded qty <= 0, skipping")
            return False
        close_side = "SELL" if side == "long" else "BUY"
        ts = int(time.time() * 1000)
        params = (
            f"symbol={symbol}USDT&side={close_side}&type=MARKET"
            f"&quantity={rounded}&reduceOnly=true&timestamp={ts}"
        )
        sig = self._signed(params)
        try:
            r = requests.post(
                f"{self.BASE}/fapi/v1/order",
                headers={"X-MBX-APIKEY": self.key},
                data=f"{params}&signature={sig}", timeout=10,
            )
            if r.status_code == 200:
                return True
            _log(f"binance close {symbol} failed: {r.status_code} {r.text[:200]}")
            return False
        except Exception as e:
            _log(f"binance close {symbol} exception: {e}")
            return False


# ─── OKX V5 backend ─────────────────────────────────────────────────────────

class OKXWatchdog:
    """OKX SWAP perpetuals (V5 API). Detects posMode on first call."""

    def __init__(self) -> None:
        self.base = os.environ.get("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
        self.key = os.environ["OKX_API_KEY"]
        self.secret = os.environ["OKX_API_SECRET"]
        self.passphrase = os.environ["OKX_PASSPHRASE"]
        self._instruments: dict[str, dict] = {}
        self._pos_mode: str | None = None
        self._load_exchange_info()

    @staticmethod
    def _ts() -> str:
        n = datetime.now(timezone.utc)
        return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        prehash = ts + method + path + body
        mac = hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        return base64.b64encode(mac).decode()

    def _hdrs(self, method: str, path: str, body: str = "") -> dict:
        ts = self._ts()
        return {
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }

    def _load_exchange_info(self) -> None:
        try:
            r = requests.get(
                f"{self.base}/api/v5/public/instruments",
                params={"instType": "SWAP"}, timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            if body.get("code") != "0":
                _log(f"okx instruments error: {body.get('msg')}")
                return
            for inst in body.get("data", []):
                inst_id = inst.get("instId", "")
                if inst_id.endswith("-USDT-SWAP"):
                    self._instruments[inst_id] = {
                        "ctVal": float(inst.get("ctVal", 1)),
                        "lotSz": float(inst.get("lotSz", 1)),
                    }
            _log(f"okx: loaded {len(self._instruments)} USDT-SWAP instruments")
        except Exception as e:
            _log(f"okx exchange info failed: {e}")

    def _ensure_pos_mode(self) -> str:
        if self._pos_mode is not None:
            return self._pos_mode
        path = "/api/v5/account/config"
        try:
            r = requests.get(f"{self.base}{path}", headers=self._hdrs("GET", path), timeout=10)
            r.raise_for_status()
            body = r.json()
            if body.get("code") == "0" and body.get("data"):
                self._pos_mode = body["data"][0].get("posMode") or "net_mode"
                _log(f"okx posMode = {self._pos_mode}")
            else:
                self._pos_mode = "net_mode"
                _log(f"okx posMode fetch error {body.get('code')}: {body.get('msg')} — defaulting net_mode")
        except Exception as e:
            self._pos_mode = "net_mode"
            _log(f"okx posMode fetch failed ({e}) — defaulting net_mode")
        return self._pos_mode

    def positions(self) -> list[dict]:
        path = "/api/v5/account/positions"
        try:
            r = requests.get(f"{self.base}{path}", headers=self._hdrs("GET", path), timeout=10)
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            _log(f"okx positions fetch failed: {e}")
            return []
        if body.get("code") != "0":
            _log(f"okx positions error: {body.get('msg')}")
            return []
        out = []
        for p in body.get("data", []):
            try:
                contracts = float(p.get("pos", 0))
            except (ValueError, TypeError):
                continue
            if contracts == 0:
                continue
            inst_id = p.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            symbol = inst_id.split("-")[0]
            entry = float(p.get("avgPx", 0) or 0)
            mark = float(p.get("markPx", 0) or 0)
            if entry <= 0 or mark <= 0:
                continue
            # OKX 'pos' is signed: positive=long in net mode; in hedge mode, posSide tells us
            pos_side = p.get("posSide", "net")
            if pos_side == "long" or (pos_side == "net" and contracts > 0):
                side = "long"
            else:
                side = "short"
            pnl_pct = (mark - entry) / entry if side == "long" else (entry - mark) / entry
            ct_val = self._instruments.get(inst_id, {}).get("ctVal", 1.0)
            base_qty = abs(contracts) * ct_val
            out.append({
                "symbol": symbol, "qty": base_qty, "entry": entry,
                "mark": mark, "side": side, "pnl_pct": pnl_pct,
                "upnl": float(p.get("upl", 0) or 0),
                "_inst_id": inst_id, "_contracts": abs(contracts), "_pos_side": pos_side,
            })
        return out

    def close(self, symbol: str, qty: float, side: str, _meta: dict | None = None) -> bool:
        """Close position. _meta is passed through from positions() to avoid re-resolving inst_id."""
        if _meta is None:
            _meta = {}
        inst_id = _meta.get("_inst_id") or f"{symbol}-USDT-SWAP"
        contracts = _meta.get("_contracts")
        if contracts is None:
            ct_val = self._instruments.get(inst_id, {}).get("ctVal", 1.0)
            contracts = qty / ct_val
        lot_sz = self._instruments.get(inst_id, {}).get("lotSz", 1.0)
        sz = math.floor(contracts / lot_sz) * lot_sz if lot_sz > 0 else math.floor(contracts)
        if sz <= 0:
            _log(f"okx close {symbol}: rounded sz <= 0 (contracts={contracts}, lot={lot_sz})")
            return False

        pos_mode = self._ensure_pos_mode()
        order_side = "sell" if side == "long" else "buy"
        body_dict = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": order_side,
            "ordType": "market",
            "sz": str(sz),
            # Idempotency: stable per (symbol, minute) — repeated retries within
            # a minute hit OKX dedup (51000) instead of placing a second order.
            "clOrdId": f"wd{int(time.time()/60)%10000000:07d}{symbol[:10]}",
        }
        if pos_mode == "long_short_mode":
            body_dict["posSide"] = "long" if side == "long" else "short"
        else:
            body_dict["reduceOnly"] = True
        body = json.dumps(body_dict)
        path = "/api/v5/trade/order"
        try:
            r = requests.post(
                f"{self.base}{path}", headers=self._hdrs("POST", path, body),
                data=body, timeout=10,
            )
            body_resp = r.json()
            if body_resp.get("code") == "0":
                return True
            first = body_resp.get("data", [{}])[0]
            if first.get("sCode") == "51000":
                _log(f"okx close {symbol}: duplicate clOrdId (already submitted)")
                return True  # treat as success
            _log(f"okx close {symbol} rejected: {first.get('sMsg', body_resp.get('msg'))}")
            return False
        except Exception as e:
            _log(f"okx close {symbol} exception: {e}")
            return False


# ─── Driver ─────────────────────────────────────────────────────────────────

def make_backend():
    if EXCHANGE == "okx":
        for v in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"):
            if not os.environ.get(v):
                _log(f"FATAL: {v} not set"); sys.exit(1)
        return OKXWatchdog()
    elif EXCHANGE == "binance":
        for v in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
            if not os.environ.get(v):
                _log(f"FATAL: {v} not set"); sys.exit(1)
        return BinanceWatchdog()
    _log(f"FATAL: unsupported EXCHANGE='{EXCHANGE}' (expected: binance|okx)")
    sys.exit(1)


_running = True


def _shutdown(signum, _frame):
    global _running
    _log(f"shutting down (signal {signum})")
    _running = False


def main() -> None:
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    _log(f"starting on EXCHANGE={EXCHANGE}, polling every {int(POLL_INTERVAL)}s")
    backend = make_backend()
    _log(f"backend: {type(backend).__name__}")

    last_status_print = 0.0
    while _running:
        try:
            stops_overrides: dict = {}
            if STOPS_FILE.exists():
                try:
                    with open(STOPS_FILE) as f:
                        stops_overrides = json.load(f)
                except Exception as e:
                    _log(f"stops override read failed: {e}")

            positions = backend.positions()
            for pos in positions:
                sym = pos["symbol"]
                stop = stops_overrides.get(sym, {}).get("stop", DEFAULT_STOP)
                target = stops_overrides.get(sym, {}).get("target", DEFAULT_TARGET)
                pnl_pct = pos["pnl_pct"]

                if pnl_pct <= -stop:
                    _log(f"STOP {sym} {pnl_pct*100:+.1f}% (limit -{stop*100:.0f}%) — closing")
                    if backend.close(sym, pos["qty"], pos["side"], pos) if EXCHANGE == "okx" \
                            else backend.close(sym, pos["qty"], pos["side"]):
                        _log(f"  closed {sym} @ ${pos['mark']:.6f} | upnl ${pos['upnl']:+.2f}")
                elif pnl_pct >= target:
                    _log(f"TARGET {sym} {pnl_pct*100:+.1f}% (limit +{target*100:.0f}%) — closing")
                    if backend.close(sym, pos["qty"], pos["side"], pos) if EXCHANGE == "okx" \
                            else backend.close(sym, pos["qty"], pos["side"]):
                        _log(f"  closed {sym} @ ${pos['mark']:.6f} | upnl ${pos['upnl']:+.2f}")

            # Periodic status line every ~5 min for liveness in journalctl
            now = time.time()
            if now - last_status_print >= 300:
                if positions:
                    summary = ", ".join(f"{p['symbol']}={p['pnl_pct']*100:+.1f}%" for p in positions)
                    _log(f"alive: {len(positions)} open ({summary})")
                else:
                    _log("alive: no open positions")
                last_status_print = now
        except Exception as e:
            _log(f"loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
