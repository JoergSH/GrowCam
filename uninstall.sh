#!/usr/bin/env bash
# GrowCam entfernen. Bilder/Videos bleiben erhalten, ausser mit --purge.
#   uninstall.sh                 Hauptinstanz entfernen
#   uninstall.sh --name wurzeln  nur die Instanz "wurzeln" entfernen
# Erkennt automatisch, ob GrowCam systemweit oder als Benutzerdienst installiert ist.
# shellcheck disable=SC2086  # $SUDO/$SYSTEMCTL werden absichtlich aufgeteilt
set -euo pipefail
PURGE=0
NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --name) NAME="${2:?}"; shift 2 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 1 ;;
  esac
done
SFX="${NAME:+-$NAME}"

if [ "$(id -u)" -ne 0 ] && [ -f "$HOME/.config/systemd/user/growcam$SFX-web.service" ]; then
  SUDO=""; SYSTEMCTL="systemctl --user"
  UNITDIR="$HOME/.config/systemd/user"
  APPDIR="$HOME/.local/share/growcam"
  ENVFILE="$HOME/.config/growcam/growcam$SFX.env"
else
  SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
  SYSTEMCTL="systemctl"
  UNITDIR="/etc/systemd/system"
  APPDIR="/opt/growcam"
  ENVFILE="/etc/default/growcam$SFX"
fi

DATA=""
if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  . "$ENVFILE"
  DATA="${GROWCAM_DATA:-}"
fi

echo "==> Stoppe und entferne Dienste${NAME:+ der Instanz $NAME}"
$SUDO $SYSTEMCTL disable --now "growcam$SFX-capture.timer" "growcam$SFX-stab.timer" "growcam$SFX-web.service" 2>/dev/null || true
$SUDO rm -f "$UNITDIR/growcam$SFX-web.service" "$UNITDIR/growcam$SFX-capture.service" "$UNITDIR/growcam$SFX-capture.timer" \
            "$UNITDIR/growcam$SFX-stab.service" "$UNITDIR/growcam$SFX-stab.timer"
$SUDO $SYSTEMCTL daemon-reload
$SUDO rm -f "$ENVFILE"

# Programm nur entfernen, wenn keine andere Instanz es mehr nutzt
if ls "$UNITDIR"/growcam*-web.service >/dev/null 2>&1; then
  echo "    Andere Instanzen laufen weiter, Programm bleibt in $APPDIR"
else
  echo "==> Entferne Programm"
  $SUDO rm -rf "$APPDIR"
fi

if [ "$PURGE" -eq 1 ] && [ -n "$DATA" ] && [ -d "$DATA" ]; then
  echo "==> Loesche Datenordner $DATA"
  $SUDO rm -rf "$DATA"
elif [ -n "$DATA" ]; then
  echo "    Bilder und Videos bleiben erhalten in: $DATA"
  echo "    (komplett loeschen: uninstall.sh${NAME:+ --name $NAME} --purge)"
fi
echo "==> GrowCam${NAME:+ ($NAME)} entfernt."
