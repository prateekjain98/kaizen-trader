#!/usr/bin/env python3
"""Stop-loss watchdog — monitors positions and exits on stops/targets.

This does NOT make entry decisions. Claude (in conversation) is the brain.
This only runs as a safety net between conversations to protect capital.
"""

import hmac
import json
import math
import os
import signal
import sys
import time

import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BINANCE_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_SECRET = os.environ.get('BINANCE_API_SECRET', '')

if not BINANCE_KEY or not BINANCE_SECRET:
    print("[watchdog] ERROR: BINANCE_API_KEY and BINANCE_API_SECRET must be set", flush=True)
    sys.exit(1)
FAPI_BASE = "https://fapi.binance.com"

# Step sizes cache
_step_sizes = {}

def load_exchange_info():
    try:
        data = requests.get(f"{FAPI_BASE}/fapi/v1/exchangeInfo", timeout=10).json()
        for s in data.get('symbols', []):
            if s['symbol'].endswith('USDT'):
                sym = s['symbol'].replace('USDT', '')
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        _step_sizes[sym] = float(f['stepSize'])
        print(f"[watchdog] Loaded {len(_step_sizes)} symbols", flush=True)
    except Exception as e:
        print(f"[watchdog] Exchange info failed: {e}", flush=True)

def round_qty(symbol, quantity):
    step = _step_sizes.get(symbol, 0.001)
    if step <= 0: return round(quantity, 3)
    precision = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    qty = math.floor(quantity / step) * step
    return round(qty, precision)

def set_leverage(symbol):
    ts = int(time.time() * 1000)
    params = f"symbol={symbol}USDT&leverage=1&timestamp={ts}"
    sig = hmac.new(BINANCE_SECRET.encode(), params.encode(), 'sha256').hexdigest()
    requests.post(f"{FAPI_BASE}/fapi/v1/leverage", headers={"X-MBX-APIKEY": BINANCE_KEY},
                  data=f"{params}&signature={sig}", timeout=5)

def sell(symbol, quantity):
    set_leverage(symbol)
    ts = int(time.time() * 1000)
    qty = round_qty(symbol, quantity)
    if qty <= 0: return False
    params = f"symbol={symbol}USDT&side=SELL&type=MARKET&quantity={qty}&timestamp={ts}"
    sig = hmac.new(BINANCE_SECRET.encode(), params.encode(), 'sha256').hexdigest()
    r = requests.post(f"{FAPI_BASE}/fapi/v1/order", headers={"X-MBX-APIKEY": BINANCE_KEY},
                     data=f"{params}&signature={sig}", timeout=10)
    return r.status_code == 200

def get_account():
    ts = int(time.time() * 1000)
    params = f'timestamp={ts}'
    sig = hmac.new(BINANCE_SECRET.encode(), params.encode(), 'sha256').hexdigest()
    r = requests.get(f'{FAPI_BASE}/fapi/v2/account', headers={'X-MBX-APIKEY': BINANCE_KEY},
                     params=params + '&signature=' + sig, timeout=5)
    if r.status_code != 200: return None
    return r.json()

# === WATCHDOG LOOP ===
load_exchange_info()

# Default stops (override via /tmp/watchdog_stops.json)
STOPS_FILE = Path("/tmp/watchdog_stops.json")
DEFAULT_STOP = 0.15  # 15% stop loss
DEFAULT_TARGET = 0.40  # 40% take profit

_running = True

def _shutdown(signum, frame):
    global _running
    print(f"\n[watchdog] Shutting down (signal {signum})", flush=True)
    _running = False

signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

print("[watchdog] Running — monitoring positions every 30s", flush=True)

while _running:
    try:
        acct = get_account()
        if not acct:
            time.sleep(30)
            continue

        # Load custom stops
        stops = {}
        if STOPS_FILE.exists():
            with open(STOPS_FILE) as f:
                stops = json.load(f)

        positions = [p for p in acct.get('positions', []) if float(p.get('positionAmt', 0)) != 0]

        for pos in positions:
            sym = pos['symbol'].replace('USDT', '')
            qty = abs(float(pos['positionAmt']))
            entry = float(pos['entryPrice'])
            current = float(pos.get('markPrice', 0))
            upnl = float(pos['unrealizedProfit'])

            if entry <= 0 or current <= 0:
                continue

            pnl_pct = (current - entry) / entry
            stop = stops.get(sym, {}).get('stop', DEFAULT_STOP)
            target = stops.get(sym, {}).get('target', DEFAULT_TARGET)

            ts_str = time.strftime('%H:%M:%S')

            # STOP LOSS
            if pnl_pct <= -stop:
                print(f"[{ts_str}] 🛑 STOP {sym}: {pnl_pct*100:+.1f}% (limit: -{stop*100:.0f}%)", flush=True)
                if sell(sym, qty):
                    print(f"  Closed {sym} at ${current:.6f} | PnL: ${upnl:.2f}", flush=True)

            # TAKE PROFIT
            elif pnl_pct >= target:
                print(f"[{ts_str}] 🎯 TARGET {sym}: {pnl_pct*100:+.1f}% (limit: +{target*100:.0f}%)", flush=True)
                if sell(sym, qty):
                    print(f"  Closed {sym} at ${current:.6f} | PnL: ${upnl:.2f}", flush=True)

            else:
                if int(time.time()) % 300 < 30:  # Log every ~5 min
                    print(f"[{ts_str}] {sym}: {pnl_pct*100:+.1f}% | entry=${entry:.6f} cur=${current:.6f}", flush=True)

    except Exception as e:
        print(f"[watchdog] Error: {e}", flush=True)

    time.sleep(30)
