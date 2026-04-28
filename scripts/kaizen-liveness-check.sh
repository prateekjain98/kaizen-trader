#!/usr/bin/env bash
# Root-cron liveness check for kaizen.service.
#
# The bot writes a heartbeat file every ~60s in its stats loop. If the file
# is older than KAIZEN_HB_MAX_AGE seconds, the service is hung (yes, even if
# `systemctl is-active` returns "active" — we lost ~17h of trading on
# 2026-04-27 to exactly this scenario). Restart with logging so the gap is
# auditable.
#
# Install:
#   sudo cp scripts/kaizen-liveness-check.sh /usr/local/bin/kaizen-liveness-check
#   sudo chmod +x /usr/local/bin/kaizen-liveness-check
#   sudo crontab -e
#   * * * * * /usr/local/bin/kaizen-liveness-check 2>&1 | logger -t kaizen-liveness

set -euo pipefail

HB_FILE="/home/prateekjain/kaizen-trader/data/.heartbeat"
MAX_AGE_SEC="${KAIZEN_HB_MAX_AGE:-180}"   # 3 min default
SERVICE="kaizen.service"
ALERT_SCRIPT="/home/prateekjain/kaizen-trader/scripts/send-alert.py"

# Send alert via send-alert.py (email + ntfy fallback). Always run as
# the prateekjain user so the .env file is readable.
alert() {
  local subject="$1"; shift
  local body="$1"; shift
  if [[ -x "$ALERT_SCRIPT" ]]; then
    sudo -u prateekjain "$ALERT_SCRIPT" "$subject" "$body" critical || true
  fi
}

# If heartbeat doesn't exist yet, the bot may be in startup — give it 5 min
# of grace from systemd's own start time before firing.
if [[ ! -f "$HB_FILE" ]]; then
  start_ts=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$SERVICE" 2>/dev/null || echo 0)
  uptime_us=$(awk '{print $1*1000000}' /proc/uptime 2>/dev/null || echo 0)
  if (( start_ts > 0 )) && (( $(echo "($uptime_us - $start_ts) / 1000000" | bc) < 300 )); then
    exit 0  # in 5min startup grace
  fi
  echo "kaizen-liveness: $HB_FILE missing — restarting $SERVICE"
  systemctl restart "$SERVICE"
  alert "Bot restarted: heartbeat file missing" \
        "kaizen-trader had no heartbeat file at $HB_FILE. Restarted $SERVICE. Verify positions on Binance and check journalctl -u kaizen for the cause."
  exit 0
fi

now=$(date +%s)
mtime=$(stat -c %Y "$HB_FILE")
age=$(( now - mtime ))

if (( age > MAX_AGE_SEC )); then
  echo "kaizen-liveness: heartbeat ${age}s stale (> ${MAX_AGE_SEC}s) — restarting $SERVICE"
  systemctl restart "$SERVICE"
  alert "Bot hung — auto-restarted" \
        "kaizen-trader heartbeat was ${age}s stale (threshold ${MAX_AGE_SEC}s). Auto-restarted $SERVICE. Check journalctl -u kaizen --since=10min for the root cause."
fi
