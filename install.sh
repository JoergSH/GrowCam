#!/usr/bin/env bash
# GrowCam installer – Zeitraffer-Kamera fuer Pflanzen mit Weboberflaeche
#
#   curl -fsSL https://raw.githubusercontent.com/@REPO@/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/@REPO@/main/install.sh | bash -s -- --port 8081
#
# oder im geklonten Repo:  ./install.sh [Optionen]
#
# Erneut ausfuehren = Update (Einstellungen und Bilder bleiben erhalten).
set -euo pipefail

REPO="${GROWCAM_REPO:-@REPO@}"
BRANCH="${GROWCAM_BRANCH:-main}"
APPDIR="/opt/growcam"
ENVFILE="/etc/default/growcam"
UNITDIR="/etc/systemd/system"

PORT=""
DATA=""
RUN_USER=""
START=1

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
[ -t 1 ] || { c_ok=""; c_warn=""; c_err=""; c_b=""; c_0=""; }
say()  { echo "${c_ok}==>${c_0} $*"; }
warn() { echo "${c_warn}WARNUNG:${c_0} $*" >&2; }
die()  { echo "${c_err}FEHLER:${c_0} $*" >&2; exit 1; }

usage() {
  cat <<EOF
GrowCam installieren / aktualisieren

Optionen:
  --port N       Port der Weboberflaeche (Standard: 8080)
  --data DIR     Datenordner fuer Bilder/Videos/Einstellungen (Standard: ~/growcam)
                 Tipp: USB-Stick/SSD statt SD-Karte, z.B. --data /mnt/usb/growcam
  --user NAME    Benutzer, unter dem GrowCam laeuft (Standard: aufrufender Benutzer)
  --no-start     Dienste installieren, aber nicht starten
  -h, --help     Diese Hilfe
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?}"; shift 2 ;;
    --data) DATA="${2:?}"; shift 2 ;;
    --user) RUN_USER="${2:?}"; shift 2 ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unbekannte Option: $1 (siehe --help)" ;;
  esac
done

# ------------------------------------------------------------ Voraussetzungen
[ "$(uname -s)" = "Linux" ] || die "GrowCam laeuft nur unter Linux."
command -v systemctl >/dev/null && [ -d /run/systemd/system ] || die "systemd wird benoetigt."

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  RUN_USER="${RUN_USER:-${SUDO_USER:-}}"
  [ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ] || die "Bitte als normaler Benutzer starten (oder --user NAME angeben)."
else
  command -v sudo >/dev/null || die "sudo wird benoetigt."
  SUDO="sudo"
  RUN_USER="${RUN_USER:-$(id -un)}"
fi
id "$RUN_USER" >/dev/null 2>&1 || die "Benutzer '$RUN_USER' existiert nicht."
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

# vorhandene Einstellungen uebernehmen (Update)
if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  . "$ENVFILE"
  PORT="${PORT:-${GROWCAM_PORT:-}}"
  DATA="${DATA:-${GROWCAM_DATA:-}}"
fi
PORT="${PORT:-8080}"
DATA="${DATA:-$RUN_HOME/growcam}"
case "$DATA" in "~"*) DATA="$RUN_HOME${DATA#\~}" ;; esac
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "Ungueltiger Port: $PORT"

say "${c_b}GrowCam-Installation${c_0} fuer Benutzer ${c_b}$RUN_USER${c_0}, Port ${c_b}$PORT${c_0}, Daten in ${c_b}$DATA${c_0}"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
say "System: ${MODEL:-$(uname -m)} ($(uname -m), $(nproc) Kern(e))"

# ------------------------------------------------------------ Quellen finden
SRC=""
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/growcam/core.py" ]; then
  SRC="$SELF_DIR"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  URL="https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz"
  say "Lade GrowCam von $URL"
  if command -v curl >/dev/null; then
    curl -fsSL "$URL" | tar xz -C "$TMP"
  elif command -v wget >/dev/null; then
    wget -qO- "$URL" | tar xz -C "$TMP"
  else
    die "curl oder wget wird zum Herunterladen benoetigt."
  fi
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  [ -f "$SRC/growcam/core.py" ] || die "Download unvollstaendig."
fi

# ------------------------------------------------------------ Pakete
PKGS=(python3 ffmpeg v4l-utils fonts-dejavu-core)
if command -v apt-get >/dev/null; then
  MISSING=()
  for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
  done
  if [ ${#MISSING[@]} -gt 0 ]; then
    say "Installiere Pakete: ${MISSING[*]} (kann auf einem Pi 1 eine Weile dauern)"
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${MISSING[@]}"
  else
    say "Pakete bereits vorhanden"
  fi
else
  warn "Kein apt gefunden – bitte python3, ffmpeg und v4l-utils selbst installieren."
fi
for b in python3 ffmpeg v4l2-ctl; do
  command -v "$b" >/dev/null || die "'$b' fehlt."
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)' || die "Python 3.7 oder neuer wird benoetigt."

# ------------------------------------------------------------ Benutzer & Daten
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx video; then
  say "Fuege $RUN_USER zur Gruppe 'video' hinzu"
  $SUDO usermod -aG video "$RUN_USER"
fi
$SUDO mkdir -p "$DATA"
$SUDO chown "$RUN_USER": "$DATA"

# ------------------------------------------------------------ Programm
say "Installiere Programm nach $APPDIR"
$SUDO rm -rf "$APPDIR.new"
$SUDO mkdir -p "$APPDIR.new"
$SUDO cp -r "$SRC/growcam/." "$APPDIR.new/"
$SUDO find "$APPDIR.new" -name '__pycache__' -prune -exec rm -rf {} +
$SUDO rm -rf "$APPDIR"
$SUDO mv "$APPDIR.new" "$APPDIR"
$SUDO install -m 0755 "$SRC/uninstall.sh" "$APPDIR/uninstall.sh"

$SUDO tee "$ENVFILE" >/dev/null <<EOF
# GrowCam – nach Aenderung: sudo systemctl restart growcam-web
GROWCAM_DATA=$DATA
GROWCAM_PORT=$PORT
EOF

# ------------------------------------------------------------ Dienste
say "Richte systemd-Dienste ein"
for u in growcam-web.service growcam-capture.service growcam-capture.timer; do
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@APPDIR@|$APPDIR|g" "$SRC/systemd/$u" \
    | $SUDO tee "$UNITDIR/$u" >/dev/null
done
$SUDO systemctl daemon-reload

if command -v ss >/dev/null && ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q . \
   && ! systemctl is-active --quiet growcam-web; then
  warn "Port $PORT ist schon belegt. Anderen Port waehlen: ./install.sh --port 8081"
fi

if [ "$START" -eq 1 ]; then
  $SUDO systemctl enable --now growcam-capture.timer >/dev/null
  $SUDO systemctl enable growcam-web.service >/dev/null
  $SUDO systemctl restart growcam-web.service
else
  $SUDO systemctl enable growcam-capture.timer growcam-web.service >/dev/null
fi

# ------------------------------------------------------------ Kamera pruefen
echo
if [ -z "$SUDO" ]; then
  runuser -u "$RUN_USER" -- env GROWCAM_DATA="$DATA" python3 "$APPDIR/core.py" info || true
else
  sudo -u "$RUN_USER" env GROWCAM_DATA="$DATA" python3 "$APPDIR/core.py" info || true
fi
if ! ls /dev/video* >/dev/null 2>&1; then
  warn "Keine Kamera gefunden – USB-Webcam anschliessen; GrowCam erkennt sie automatisch."
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
say "${c_b}Fertig!${c_0}  Weboberflaeche: ${c_b}http://${IP:-$(hostname)}:$PORT${c_0}"
echo "    Zeitplan, Lichtzeiten und Bildeinstellungen dort unter 'Einstellungen'."
echo "    Status:      systemctl status growcam-web growcam-capture.timer"
echo "    Entfernen:   sudo $APPDIR/uninstall.sh   (Bilder bleiben erhalten)"
