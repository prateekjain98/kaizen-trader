#!/usr/bin/env python3
"""A/B compare prod close stats: pre-tightening vs post-tightening.

Pulls journal close lines from kaizen-prod via gcloud SSH and prints a
side-by-side table of WR / mean PnL / target-hit-rate / exit-reason mix.

Cutoff: 2026-05-03T06:08:00 UTC — wave of audit fixes started landing
(cooldowns wired, MIN_SCORE 40→60, strategy_type clobbers, FGI/side
fixes, signal_detector emit fixes, trail-attribution fix). Anything
opened-and-closed before that timestamp is "pre"; after is "post".

Usage:
    python tools/post_tightening_report.py [--days N]

Real journal data only. No fabrication. Prints UNAVAILABLE if SSH fails
or if no closes found in either bucket.
"""
import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone

CUTOFF_UTC = datetime(2026, 5, 3, 6, 8, 0, tzinfo=timezone.utc)
SSH_CMD = ["gcloud", "compute", "ssh", "kaizen-prod",
           "--zone=asia-east2-a", "--tunnel-through-iap", "--command"]

CLOSE_RE = re.compile(
    r"(?P<mon>[A-Z][a-z]{2}) (?P<day>\d{2}) (?P<hms>\d\d:\d\d:\d\d) .*"
    r"\xf0\x9f\x92\xb0 CLOSE (?P<sym>[A-Z0-9]+) (?P<reason>[a-z_]+) "
    r"\$(?P<pnl>[+-][0-9.]+) \((?P<pct>[+-][0-9.]+)%\)"
)


def fetch_journal(since_days: int) -> str:
    cmd = (f'sudo journalctl -u kaizen --since="{since_days} days ago" '
           f'--no-pager | grep "💰 CLOSE"')
    try:
        r = subprocess.run(SSH_CMD + [cmd], capture_output=True,
                           text=True, timeout=120)
        return r.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def parse_closes(text: str) -> list[dict]:
    closes = []
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    # The regex above used escaped UTF-8; simpler: split by line and parse.
    line_re = re.compile(
        r"^([A-Z][a-z]{2}) (\d{2}) (\d\d:\d\d:\d\d) .*"
        r"CLOSE ([A-Z0-9]+) ([a-z_]+) \$([+-][0-9.]+) \(([+-][0-9.]+)%\)"
    )
    for raw in text.splitlines():
        # Strip the heart emoji bytes by working char-by-char
        m = line_re.search(raw)
        if not m:
            continue
        mon, day, hms, sym, reason, pnl_usd, pnl_pct = m.groups()
        # Assume current year; journal doesn't include year
        year = datetime.now(timezone.utc).year
        try:
            ts = datetime(year, months[mon], int(day),
                          *map(int, hms.split(":")), tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        closes.append({
            "ts": ts, "symbol": sym, "reason": reason,
            "pnl_usd": float(pnl_usd), "pnl_pct": float(pnl_pct),
        })
    return closes


def summarize(closes: list[dict], label: str) -> None:
    n = len(closes)
    if n == 0:
        print(f"  {label:>5}: UNAVAILABLE (no closes)")
        return
    wins = sum(1 for c in closes if c["pnl_usd"] >= 0)
    targets = sum(1 for c in closes if c["reason"] == "target")
    stops = sum(1 for c in closes if c["reason"] == "stop")
    trails = sum(1 for c in closes if c["reason"] == "trail")
    fast = sum(1 for c in closes if c["reason"] == "fast_cut")
    big_loss = sum(1 for c in closes if c["pnl_pct"] <= -10)
    total_pnl = sum(c["pnl_usd"] for c in closes)
    mean_pct = sum(c["pnl_pct"] for c in closes) / n
    print(f"  {label:>5}: n={n:3d}  WR={100*wins/n:5.1f}%  "
          f"mean_pct={mean_pct:+.2f}%  total_pnl=${total_pnl:+.2f}")
    print(f"         exits: target={targets}  stop={stops}  "
          f"trail={trails}  fast_cut={fast}  big_loss(<=-10%)={big_loss}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    args = p.parse_args()

    text = fetch_journal(args.days)
    if not text:
        print("UNAVAILABLE: ssh/journalctl returned no data")
        return 1

    closes = parse_closes(text)
    pre = [c for c in closes if c["ts"] < CUTOFF_UTC]
    post = [c for c in closes if c["ts"] >= CUTOFF_UTC]

    print(f"\n=== Post-tightening A/B (cutoff {CUTOFF_UTC.isoformat()}) ===")
    print(f"Total closes parsed: {len(closes)}  (pre={len(pre)}, post={len(post)})")
    print()
    summarize(pre, "PRE")
    summarize(post, "POST")
    print()
    if pre and post:
        delta_wr = (sum(1 for c in post if c["pnl_usd"] >= 0)/len(post)) - \
                   (sum(1 for c in pre if c["pnl_usd"] >= 0)/len(pre))
        delta_mean = (sum(c["pnl_pct"] for c in post)/len(post)) - \
                     (sum(c["pnl_pct"] for c in pre)/len(pre))
        print(f"  DELTA: WR {delta_wr*100:+.1f}pp  mean_pct {delta_mean:+.2f}pp")
        if len(post) < 10:
            print(f"  VERDICT: INSUFFICIENT (post n={len(post)} < 10; need more closes)")
        else:
            print(f"  VERDICT: see numbers; t-test gating in scripts/run_live_backtest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
