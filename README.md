# 🌱 GrowCam

**Zeitraffer-Kamera für Pflanzen mit Weboberfläche.** Läuft auf jedem Linux-Rechner mit einer USB-Webcam, vom alten Raspberry Pi 1 bis zum PC. Fotografiert nur, solange das Growlicht an ist, und macht aus den Bildern auf Knopfdruck ein Zeitraffer-Video.

*English summary below.*

## Funktionen

- **Live-Vorschau** zum Ausrichten der Kamera, Foto auf Knopfdruck
- **Zeitplan:** Intervall (1–60 min) und Lichtzeiten (auch über Mitternacht, z. B. 18–12 Uhr)
- **Galerie** nach Tagen, mit Lücken-Erkennung (Kamera ausgefallen?), Lightbox und Download
- **Video erstellen** im Browser: Zeitraum, fps, Breite, jedes n-te Bild, Datum/Uhrzeit-Einblendung
- **Bildeinstellungen:** Weißabgleich und Belichtung fest einstellbar (wichtig, damit das Video nicht flackert), dazu Helligkeit, Kontrast, Sättigung und Schärfe. Die Bereiche liest GrowCam direkt aus der Kamera.
- **Statusanzeige:** freier Speicher, Anzahl Tage, für die der Platz noch reicht, Warnung, wenn keine Bilder mehr kommen, Fehlerprotokoll
- **Touch-tauglich:** auch als feste Anzeige am Growzelt nutzbar
- **Ohne Abhängigkeiten:** nur Python-Standardbibliothek, `ffmpeg` und `v4l-utils`, kein pip, keine Cloud

## Installation

Ein Befehl auf dem Pi bzw. Linux-Rechner (Debian, Ubuntu, Raspberry Pi OS, Mint …):

```bash
curl -fsSL https://raw.githubusercontent.com/JoergSH/GrowCam/main/install.sh | bash
```

Danach im Browser `http://<IP-des-Rechners>:8080` öffnen.

Optionen werden mit `bash -s --` angehängt:

```bash
curl -fsSL https://raw.githubusercontent.com/JoergSH/GrowCam/main/install.sh | bash -s -- --port 8081 --data /mnt/usb/growcam
```

| Option | Bedeutung | Standard |
|---|---|---|
| `--port N` | Port der Weboberfläche | `8080` |
| `--data DIR` | Ordner für Bilder, Videos und Einstellungen | `~/growcam` |
| `--user NAME` | Benutzer, unter dem GrowCam läuft | aufrufender Benutzer |
| `--user-service` | ohne Root/sudo installieren, als systemd-Benutzerdienst (Pakete müssen schon vorhanden sein) | – |
| `--no-start` | nur installieren, nicht starten | – |

Alternativ aus dem geklonten Repo:

```bash
git clone https://github.com/JoergSH/GrowCam.git
cd GrowCam && bash install.sh
```

**Update:** einfach den Installer erneut ausführen. Einstellungen und Bilder bleiben erhalten.

**Entfernen:** `sudo bash /opt/growcam/uninstall.sh` (im Benutzer-Modus: `bash ~/.local/share/growcam/uninstall.sh`). Die Bilder bleiben erhalten; mit `--purge` wird auch der Datenordner gelöscht.

## Hardware

| Rechner | Aufnahme | Weboberfläche | Video erstellen |
|---|---|---|---|
| Raspberry Pi 1 / Zero | ✅ sparsamer Modus | ✅ etwas träge | ⚠️ langsam, 1280 px empfohlen |
| Raspberry Pi 2 / 3 / 4 | ✅ | ✅ | ✅ Hardware-Encoder (bis 1080p) |
| Raspberry Pi 5, PC, Laptop | ✅ | ✅ | ✅ |

- **Kamera:** jede USB-Webcam (UVC), am besten eine mit MJPEG. Das Pi-Kameramodul (CSI/libcamera) wird nicht unterstützt.
- **Betriebssystem auf dem Pi 1 / Zero:** *Raspberry Pi OS Lite (32-bit)*.
- **Speicher:** Pro Bild fallen je nach Kamera etwa 0,3–1 MB an, bei 10-min-Intervall und 18 h Licht also rund 50–110 MB pro Tag. Für längere Grows lieber einen USB-Stick oder eine SSD nehmen (`--data`) statt der SD-Karte.
- **Strom:** Am Pi 1/2 kann die Webcam die USB-Versorgung überlasten. Dann ein ordentliches Netzteil oder einen aktiven USB-Hub verwenden.

### Was GrowCam auf schwacher Hardware automatisch anders macht

Auf einem Pi 1 / Zero (ARMv6 oder 1 Kern) erkennt GrowCam die schwache CPU selbst und schaltet um:

- **Direkt speichern:** Das MJPEG-Bild der Kamera wird unverändert gespeichert, ohne Dekodieren und neu Kodieren. Das braucht kaum CPU.
- **Vorschaubilder** entstehen gleich bei der Aufnahme, verkleinert dekodiert (`-lowres`), damit die Galerie flott lädt.
- **Videos** werden standardmäßig mit 1280 px Breite erstellt. GrowCam nutzt den Hardware-Encoder des Pi (`h264_v4l2m2m`), wenn er funktioniert, sonst `libx264 ultrafast`.

Alles lässt sich unter *Einstellungen → Kamera & Technik* auch manuell festlegen.

## Fehlerbehebung

**Paketinstallation extrem langsam oder bricht ab** (`Unable to connect to raspbian.raspberrypi.com`): Der Standard-Paketserver leitet manchmal auf einen überlasteten Spiegelserver weiter. Dann einen deutschen Spiegel eintragen und den Installer erneut starten:

```bash
sudo sed -i 's#http://raspbian.raspberrypi.com/raspbian#http://mirror.netcologne.de/raspbian/raspbian#g' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null
sudo apt-get update
```

**Pi 1 / Zero:** Die Installation von ffmpeg dauert dort 30–60 Minuten. Das ist normal, bitte nicht abbrechen.

## Bedienung & Tipps

- **Flackern vermeiden:** Unter *Einstellungen* Weißabgleich und Belichtung auf manuell stellen und bei eingeschaltetem Growlicht feste Werte wählen.
- **Vorlauf:** Die Anzahl Frames vor dem Foto gibt Autofokus und Belichtung Zeit zum Einschwingen. Wenn Bilder unscharf oder zu dunkel sind, erhöhen.
- **Mehrere Kameras:** Unter *Kamera-Gerät* einen festen Pfad eintragen, z. B. `/dev/v4l/by-id/usb-…-video-index0`.
- Prüfen, was erkannt wurde: `python3 /opt/growcam/core.py info`

## Technik

```
/opt/growcam/              Programm (core.py, server.py, static/)
/etc/default/growcam       GROWCAM_DATA, GROWCAM_PORT
~/growcam/                 config.json, images/JJJJ-MM-TT/, thumbs/, videos/, error.log
growcam-web.service        Weboberfläche
growcam-capture.timer      prüft jede Minute, ob ein Bild fällig ist
```

```bash
systemctl status growcam-web growcam-capture.timer
journalctl -u growcam-web -f
```

Die Weboberfläche hat **keine Anmeldung**. Sie ist fürs Heimnetz gedacht, also nicht ins Internet freigeben.

---

## English summary

GrowCam is a plant time-lapse camera with a web UI for any Linux box with a USB (UVC) webcam, from a Raspberry Pi 1 up to a PC. It captures only while the grow light is on, shows a gallery per day with gap detection, and renders time-lapse videos in the browser (date overlay, fps, width, every n-th frame). It has no dependencies beyond the Python standard library, `ffmpeg` and `v4l-utils`.

```bash
curl -fsSL https://raw.githubusercontent.com/JoergSH/GrowCam/main/install.sh | bash
```

Then open `http://<ip>:8080`. On weak hardware (Pi 1/Zero) GrowCam automatically stores the camera's MJPEG frames as-is (almost no CPU), builds thumbnails at capture time, and renders videos at 1280 px using the Pi's hardware encoder if available. The UI is in German. It has no authentication, so keep it on your LAN.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
