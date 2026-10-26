#!/usr/bin/env bash
# GrowCam entfernen. Bilder/Videos bleiben erhalten, ausser mit --purge.
# Erkennt automatisch, ob GrowCam systemweit oder als Benutzerdienst installiert ist.
# shellcheck disable=SC2086  # $SUDO/$SYSTEMCTL werden absichtlich aufgeteilt
set -euo pipefail
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ "$(id -u)" -ne 0 ] && [ -f "$HOME/.config/systemd/user/growcam-web.service" ]; then
  SUDO=""; SYSTEMCTL="systemctl --user"
  UNITDIR="$HOME/.config/systemd/user"
  APPDIR="$HOME/.local/share/growcam"
  ENVFILE="$HOME/.config/growcam/growcam.env"
else
  SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
  SYSTEMCTL="systemctl"
  UNITDIR="/etc/systemd/system"
  APPDIR="/opt/growcam"
  ENVFILE="/etc/default/growcam"
fi

DATA=""
if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  . "$ENVFILE"
  DATA="${GROWCAM_DATA:-}"
fi

echo "==> Stoppe und entferne Dienste"
$SUDO $SYSTEMCTL disable --now growcam-capture.timer growcam-web.service 2>/dev/null || true
$SUDO rm -f "$UNITDIR/growcam-web.service" "$UNITDIR/growcam-capture.service" "$UNITDIR/growcam-capture.timer"
$SUDO $SYSTEMCTL daemon-reload
echo "==> Entferne Programm"
$SUDO rm -rf "$APPDIR" "$ENVFILE"

if [ "$PURGE" -eq 1 ] && [ -n "$DATA" ] && [ -d "$DATA" ]; then
  echo "==> Loesche Datenordner $DATA"
  $SUDO rm -rf "$DATA"
elif [ -n "$DATA" ]; then
  echo "    Bilder und Videos bleiben erhalten in: $DATA"
  echo "    (komplett loeschen: uninstall.sh --purge)"
fi
echo "==> GrowCam entfernt."
