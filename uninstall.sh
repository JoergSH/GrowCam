#!/usr/bin/env bash
# GrowCam entfernen. Bilder/Videos bleiben erhalten, ausser mit --purge.
set -euo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"

DATA=""
if [ -f /etc/default/growcam ]; then
  # shellcheck disable=SC1091
  . /etc/default/growcam
  DATA="${GROWCAM_DATA:-}"
fi

echo "==> Stoppe und entferne Dienste"
$SUDO systemctl disable --now growcam-capture.timer growcam-web.service 2>/dev/null || true
$SUDO rm -f /etc/systemd/system/growcam-web.service \
            /etc/systemd/system/growcam-capture.service \
            /etc/systemd/system/growcam-capture.timer
$SUDO systemctl daemon-reload
echo "==> Entferne Programm"
$SUDO rm -rf /opt/growcam /etc/default/growcam

if [ "$PURGE" -eq 1 ] && [ -n "$DATA" ] && [ -d "$DATA" ]; then
  echo "==> Loesche Datenordner $DATA"
  $SUDO rm -rf "$DATA"
elif [ -n "$DATA" ]; then
  echo "    Bilder und Videos bleiben erhalten in: $DATA"
  echo "    (komplett loeschen: uninstall.sh --purge)"
fi
echo "==> GrowCam entfernt."
