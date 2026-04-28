#!/usr/bin/env python3
"""Send a critical-incident alert via email (preferred) or ntfy.sh push.

Reads config from /home/prateekjain/kaizen-trader/.env (or env vars):
  ALERT_EMAIL          — recipient address (required for email path)
  SMTP_HOST            — e.g. smtp.gmail.com (default if empty)
  SMTP_PORT            — default 587
  SMTP_USER            — sender address
  SMTP_PASS            — Gmail App Password (NOT account password)
  NTFY_TOPIC           — used as fallback if SMTP not configured.
                         Subscribe via the ntfy app — see https://ntfy.sh.
                         Default: kaizen-trader-alerts-<random>; document the
                         actual value in deploy notes.

Usage:
  send-alert.py "Subject" "Body text" [severity]
    severity: 'info' | 'warn' | 'critical'  (default: 'critical')

Exit 0 on success, 1 on all-channels-failed (still prints to stderr so
the caller can see it in journalctl).
"""

import os
import smtplib
import sys
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path


def _load_env() -> dict:
    """Merge process env with .env file (process env wins)."""
    env = dict(os.environ)
    env_file = Path("/home/prateekjain/kaizen-trader/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k not in env:
                env[k] = v
    return env


def _send_email(env: dict, subject: str, body: str) -> bool:
    host = env.get("SMTP_HOST", "smtp.gmail.com")
    port = int(env.get("SMTP_PORT", "587"))
    user = env.get("SMTP_USER", "").strip()
    pwd = env.get("SMTP_PASS", "").strip()
    to = env.get("ALERT_EMAIL", "").strip()
    if not (user and pwd and to):
        return False
    msg = MIMEText(body)
    msg["Subject"] = f"[kaizen-trader] {subject}"
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        print(f"alert email sent to {to}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"smtp send failed: {e}", file=sys.stderr)
        return False


def _send_ntfy(env: dict, subject: str, body: str, severity: str) -> bool:
    topic = env.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    priority = {"info": "3", "warn": "4", "critical": "5"}.get(severity, "5")
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": f"kaizen-trader: {subject}",
                "Priority": priority,
                "Tags": "warning,robot",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
        print(f"alert pushed to ntfy.sh/{topic}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"ntfy push failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: send-alert.py SUBJECT BODY [severity]", file=sys.stderr)
        return 2
    subject = sys.argv[1]
    body = sys.argv[2]
    severity = sys.argv[3] if len(sys.argv) > 3 else "critical"

    env = _load_env()
    sent_any = False
    if _send_email(env, subject, body):
        sent_any = True
    if _send_ntfy(env, subject, body, severity):
        sent_any = True

    if not sent_any:
        print(f"NO ALERT CHANNELS CONFIGURED — incident: {subject}: {body}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
