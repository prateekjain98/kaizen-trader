#!/usr/bin/env bash
# Install the Kaizen systemd units on the GCP VM. Run as root:
#   sudo bash deploy/setup_service.sh
set -euo pipefail

DIR="$(dirname "$0")"
for unit in kaizen.service kaizen-watchdog.service; do
    cp "$DIR/$unit" "/etc/systemd/system/$unit"
    chmod 644 "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable kaizen.service kaizen-watchdog.service
systemctl restart kaizen.service kaizen-watchdog.service

echo "Installed. Status:"
systemctl status kaizen.service --no-pager -l | head -20 || true
echo
systemctl status kaizen-watchdog.service --no-pager -l | head -20 || true
echo
echo "Useful commands:"
echo "  sudo systemctl status kaizen kaizen-watchdog"
echo "  sudo journalctl -u kaizen -f"
echo "  sudo journalctl -u kaizen-watchdog -f"
