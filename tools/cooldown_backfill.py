#!/usr/bin/env python3
"""Estimate avoided-loss savings if per-symbol cooldown had been wired
retroactively across the prod close history.

Replays the OPEN/CLOSE timeline from journalctl, applies the same rule
as src/risk/loss_cooldown.py:
  - 2 consecutive losses on a symbol arms a 4h cooldown
  - any OPEN of that symbol within the cooldown is COUNTERFACTUALLY
    blocked, and that trade's actual realized PnL is summed as 'savings'
A win on a symbol resets the consecutive-loss count.

Real journal data only. Conservative — only counts trades that would
have been blocked under the EXACT rule (no parameter tuning).

Usage:
    python tools/cooldown_backfill.py [--days 14]
"""
import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta

SSH_CMD = ["gcloud", "compute", "ssh", "kaizen-prod",
           "--zone=asia-east2-a", "--tunnel-through-iap", "--command"]
SYMBOL_LOSS_THRESHOLD = 2
SYMBOL_COOLDOWN = timedelta(hours=4)

OPEN_RE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{2}) (\d\d:\d\d:\d\d) .*"
    r"OPEN (?:LONG|SHORT) ([A-Z0-9]+) "
)
CLOSE_RE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{2}) (\d\d:\d\d:\d\d) .*"
    r"CLOSE ([A-Z0-9]+) [a-z_]+ \$([+-][0-9.]+)"
)
MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def fetch_journal(days: int) -> str:
    cmd = (f'sudo journalctl -u kaizen --since="{days} days ago" '
           f'--no-pager | grep -E "💰 (OPEN|CLOSE)"')
    try:
        return subprocess.run(SSH_CMD + [cmd], capture_output=True,
                              text=True, timeout=180).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def parse_event(raw: str):
    """Returns ('open'|'close', symbol, ts, pnl_usd) or None."""
    year = datetime.now(timezone.utc).year
    m = OPEN_RE.search(raw)
    if m:
        mon, day, hms, sym = m.groups()
        try:
            ts = datetime(year, MONTHS[mon], int(day),
                          *map(int, hms.split(":")), tzinfo=timezone.utc)
            return ("open", sym, ts, 0.0)
        except (KeyError, ValueError):
            return None
    m = CLOSE_RE.search(raw)
    if m:
        mon, day, hms, sym, pnl = m.groups()
        try:
            ts = datetime(year, MONTHS[mon], int(day),
                          *map(int, hms.split(":")), tzinfo=timezone.utc)
            return ("close", sym, ts, float(pnl))
        except (KeyError, ValueError):
            return None
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=14)
    args = p.parse_args()

    text = fetch_journal(args.days)
    if not text:
        print("UNAVAILABLE: ssh/journalctl returned no data")
        return 1

    events = []
    for line in text.splitlines():
        e = parse_event(line)
        if e:
            events.append(e)
    events.sort(key=lambda e: e[2])

    consec_losses: dict[str, int] = {}  # symbol -> consec losses
    cooldown_until: dict[str, datetime] = {}  # symbol -> ts cooldown ends
    blocked_count = 0
    blocked_pnl = 0.0  # sum of actual realized pnl on trades that would have been blocked
    blocked_examples = []

    # Track ts of each open so we can later attribute the close to it
    # (single-position-per-symbol assumption — matches prod where positions
    # are unique per symbol).
    for kind, sym, ts, pnl in events:
        if kind == "open":
            until = cooldown_until.get(sym)
            if until and ts < until:
                blocked_count += 1
                # Find the matching close to estimate savings
                # Look ahead for next close on this symbol
                future_close_pnl = 0.0
                for k2, s2, t2, p2 in events:
                    if k2 == "close" and s2 == sym and t2 > ts:
                        future_close_pnl = p2
                        break
                blocked_pnl += future_close_pnl
                blocked_examples.append((sym, ts.isoformat(), future_close_pnl))
        elif kind == "close":
            if pnl >= 0:
                consec_losses[sym] = 0
            else:
                cnt = consec_losses.get(sym, 0) + 1
                consec_losses[sym] = cnt
                if cnt >= SYMBOL_LOSS_THRESHOLD:
                    cooldown_until[sym] = ts + SYMBOL_COOLDOWN

    print(f"\n=== Per-symbol cooldown backfill ({args.days}d window) ===")
    print(f"Events parsed: {len(events)}")
    print(f"Trades that WOULD have been blocked: {blocked_count}")
    print(f"Sum of actual realized pnl on those trades: ${blocked_pnl:+.2f}")
    print(f"  (negative sum = avoided losses, positive = missed wins)")
    if blocked_examples:
        print(f"\nFirst 5 blocked-trade examples (symbol, open_ts, that_trade_pnl):")
        for ex in blocked_examples[:5]:
            print(f"  {ex[0]:<8} {ex[1]}  ${ex[2]:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
