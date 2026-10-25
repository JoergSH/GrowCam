#!/usr/bin/env python3
"""GrowCam-Kern: Konfiguration, Kamera-Zugriff, Aufnahme.

Nur Python-Standardbibliothek; benoetigt ffmpeg und v4l2-ctl (v4l-utils).

CLI:
  core.py capture          -> sofort ein Bild aufnehmen
  core.py capture --cron   -> nur aufnehmen, wenn der Zeitplan passt (jede Minute aufrufen)
  core.py info             -> erkannte Hardware und Kamera anzeigen

Umgebungsvariablen:
  GROWCAM_DATA   Datenordner (Bilder, Videos, config.json)   Standard: ~/growcam
  GROWCAM_LOCK   Lock-Datei fuer exklusiven Kamerazugriff    Standard: /tmp/growcam.lock
"""
import datetime
import fcntl
import functools
import glob
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time

VERSION = "1.0.0"

BASE = os.path.abspath(os.path.expanduser(os.environ.get("GROWCAM_DATA", "~/growcam")))
CFG_FILE = os.path.join(BASE, "config.json")
IMG_DIR = os.path.join(BASE, "images")
THUMB_DIR = os.path.join(BASE, "thumbs")
VID_DIR = os.path.join(BASE, "videos")
ERR_LOG = os.path.join(BASE, "error.log")
LOCK_FILE = os.environ.get("GROWCAM_LOCK", os.path.join(tempfile.gettempdir(), "growcam.lock"))

# Fallback, falls die Kamera ihre Aufloesungen nicht meldet
SIZES = ["2048x1536", "1920x1080", "1280x1024", "1280x720", "1024x768", "800x600", "640x480"]
THUMB_W = 320

DEFAULTS = {
    "device": "auto",          # "auto" = erste USB-Kamera (uvcvideo)
    "input_format": "auto",    # auto | mjpeg | yuyv422
    "capture_mode": "auto",    # auto | encode | copy  (copy = MJPEG-Frame direkt speichern, kaum CPU)
    "video_encoder": "auto",   # auto | libx264 | h264_v4l2m2m
    "interval_min": 10,
    "light_start": 6,
    "light_end": 24,
    "size": "auto",            # "auto" = groesste Aufloesung der Kamera
    "warmup": 60,
    "quality": 2,
    "wb_auto": True,
    "wb_temp": 4600,
    "exp_auto": True,
    "exposure": 300,
    "brightness": 0,
    "contrast": 32,
    "saturation": 64,
    "sharpness": 3,
}

# Kamera-Regler: Config-Schluessel -> (aktueller V4L2-Name, alter Name vor Kernel 5.x)
CTRL_NAMES = {
    "brightness": ("brightness",),
    "contrast": ("contrast",),
    "saturation": ("saturation",),
    "sharpness": ("sharpness",),
    "wb_temp": ("white_balance_temperature",),
    "exposure": ("exposure_time_absolute", "exposure_absolute"),
}
CTRL_FALLBACK = {  # Bereiche, falls die Kamera nichts meldet
    "brightness": (-64, 64), "contrast": (0, 64), "saturation": (0, 128),
    "sharpness": (0, 6), "wb_temp": (2800, 6500), "exposure": (1, 5000),
}


# ---------------------------------------------------------------- Hardware
@functools.lru_cache(maxsize=1)
def hw_profile():
    machine = platform.machine().lower()
    cpus = os.cpu_count() or 1
    model = ""
    for p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(p, "rb") as f:
                model = f.read().rstrip(b"\x00").decode(errors="replace").strip()
            break
        except OSError:
            pass
    if not model:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        model = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    weak = machine.startswith("armv6") or cpus <= 1
    arm = machine.startswith(("arm", "aarch64"))
    return {"machine": machine, "cpus": cpus, "model": model, "weak": weak, "arm": arm}


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


# ---------------------------------------------------------------- Kamera
def find_device():
    """Erste UVC-Kamera finden; stabile /dev/v4l/by-id-Pfade bevorzugen."""
    cands = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    cands += sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    seen = set()
    for dev in cands:
        real = os.path.realpath(dev)
        if real in seen:
            continue
        seen.add(real)
        rc, out, _ = _run(["v4l2-ctl", "-d", dev, "--info"], timeout=5)
        if rc == 0 and "uvcvideo" in out:
            return dev
    return "/dev/video0"


def resolve_device(c):
    d = c.get("device") or "auto"
    return find_device() if d == "auto" else d


_fmt_cache = {}


def camera_formats(dev):
    """{'mjpeg': ['2048x1536', ...], 'yuyv422': [...]} – groesste zuerst."""
    hit = _fmt_cache.get(dev)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    rc, out, _ = _run(["v4l2-ctl", "-d", dev, "--list-formats-ext"], timeout=5)
    fmts, cur = {}, None
    if rc == 0:
        for line in out.splitlines():
            m = re.search(r"'(\w+)'", line)
            if m and "[" in line:
                cur = {"MJPG": "mjpeg", "YUYV": "yuyv422"}.get(m.group(1))
                if cur:
                    fmts.setdefault(cur, [])
                continue
            m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
            if m and cur:
                s = f"{m.group(1)}x{m.group(2)}"
                if s not in fmts[cur]:
                    fmts[cur].append(s)
    for k in fmts:
        fmts[k].sort(key=lambda s: -int(s.split("x")[0]) * int(s.split("x")[1]))
    _fmt_cache[dev] = (time.time(), fmts)
    return fmts


def resolve_format(c, dev):
    f = c.get("input_format") or "auto"
    if f != "auto":
        return f
    fmts = camera_formats(dev)
    if "mjpeg" in fmts or not fmts:
        return "mjpeg"
    return "yuyv422" if "yuyv422" in fmts else "mjpeg"


def camera_sizes(c, dev=None):
    dev = dev or resolve_device(c)
    return camera_formats(dev).get(resolve_format(c, dev)) or list(SIZES)


def resolve_size(c, dev, size=None):
    size = size or c.get("size") or "auto"
    sizes = camera_sizes(c, dev)
    if size == "auto":
        return sizes[0]
    return size


def nearest_size(sizes, max_w):
    """Groesste Aufloesung mit Breite <= max_w (fuer Vorschau)."""
    ok = [s for s in sizes if int(s.split("x")[0]) <= max_w]
    return ok[0] if ok else sizes[-1]


def resolve_mode(c, fmt):
    m = c.get("capture_mode") or "auto"
    if fmt != "mjpeg":
        return "encode"
    if m == "auto":
        return "copy" if hw_profile()["weak"] else "encode"
    return m


_ctrl_cache = {}


def camera_controls(dev):
    """Regler der Kamera: {v4l2name: {'min','max','step','default'}}"""
    hit = _ctrl_cache.get(dev)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    rc, out, _ = _run(["v4l2-ctl", "-d", dev, "--list-ctrls"], timeout=5)
    ctrls = {}
    if rc == 0:
        for line in out.splitlines():
            m = re.match(r"\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:(.*)", line)
            if not m:
                continue
            vals = dict(re.findall(r"(\w+)=(-?\d+)", m.group(3)))
            ctrls[m.group(1)] = {k: int(v) for k, v in vals.items()
                                 if k in ("min", "max", "step", "default", "value")}
    _ctrl_cache[dev] = (time.time(), ctrls)
    return ctrls


def control_ranges(c, dev=None):
    """Bereiche fuer die Bild-Regler der Oberflaeche (pro Config-Schluessel)."""
    dev = dev or resolve_device(c)
    cam = camera_controls(dev)
    out = {}
    for key, names in CTRL_NAMES.items():
        info = next((cam[n] for n in names if n in cam), None)
        if info and "min" in info and "max" in info:
            out[key] = {"min": info["min"], "max": info["max"], "step": info.get("step", 1),
                        "default": info.get("default", DEFAULTS[key]), "present": True}
        else:
            lo, hi = CTRL_FALLBACK[key]
            out[key] = {"min": lo, "max": hi, "step": 1, "default": DEFAULTS[key],
                        "present": not cam}
    return out


# ---------------------------------------------------------------- Konfiguration
def load_cfg():
    c = dict(DEFAULTS)
    try:
        with open(CFG_FILE) as f:
            c.update(json.load(f))
    except (OSError, ValueError):
        pass
    return c


def _clamp(v, lo, hi, typ=int):
    v = typ(v)
    return max(lo, min(hi, v))


SIZE_RE = re.compile(r"^\d{2,5}x\d{2,5}$")
DEV_RE = re.compile(r"^/dev/[\w./:+-]+$")


def validate_cfg(new):
    c = load_cfg()
    if "device" in new and (new["device"] == "auto" or DEV_RE.match(str(new["device"]))):
        c["device"] = new["device"]
    for k, allowed in (("input_format", ("auto", "mjpeg", "yuyv422")),
                       ("capture_mode", ("auto", "encode", "copy")),
                       ("video_encoder", ("auto", "libx264", "h264_v4l2m2m"))):
        if k in new and new[k] in allowed:
            c[k] = new[k]
    if "interval_min" in new:
        c["interval_min"] = _clamp(new["interval_min"], 1, 1440)
    if "light_start" in new:
        c["light_start"] = _clamp(new["light_start"], 0, 23)
    if "light_end" in new:
        c["light_end"] = _clamp(new["light_end"], 1, 24)
    if "size" in new and (new["size"] == "auto" or SIZE_RE.match(str(new["size"]))):
        c["size"] = new["size"]
    if "warmup" in new:
        c["warmup"] = _clamp(new["warmup"], 1, 300)
    if "quality" in new:
        c["quality"] = _clamp(new["quality"], 1, 10)
    for k in ("wb_auto", "exp_auto"):
        if k in new:
            c[k] = bool(new[k])
    rng = None
    for key in CTRL_NAMES:
        if key in new:
            if rng is None:
                rng = control_ranges(c)
            c[key] = _clamp(new[key], rng[key]["min"], rng[key]["max"])
    return c


def save_cfg(c):
    os.makedirs(BASE, exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
    os.replace(tmp, CFG_FILE)


def light_on(c, now=None):
    now = now or datetime.datetime.now()
    m = now.hour * 60 + now.minute
    s, e = c["light_start"] * 60, c["light_end"] * 60
    return (s <= m < e) if s < e else (m >= s or m < e)


def due(c, now=None):
    now = now or datetime.datetime.now()
    if not light_on(c, now):
        return False
    m = now.hour * 60 + now.minute
    return (m - c["light_start"] * 60) % c["interval_min"] == 0


def log_error(msg):
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(ERR_LOG, "a") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


class CamLock:
    """Exklusiver Kamera-Zugriff zwischen geplanter Aufnahme und Web-Vorschau."""

    def __init__(self, timeout=60):
        self.timeout = timeout
        self.f = None

    def __enter__(self):
        self.f = open(LOCK_FILE, "a")
        try:
            os.chmod(LOCK_FILE, 0o666)
        except OSError:
            pass
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    self.f.close()
                    raise TimeoutError("Kamera belegt")
                time.sleep(0.2)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


# ---------------------------------------------------------------- Aufnahme
def apply_controls(c, dev=None):
    dev = dev or resolve_device(c)
    cam = camera_controls(dev)

    def name(key):
        names = CTRL_NAMES[key]
        return next((n for n in names if n in cam), names[0])

    ctrls = ["power_line_frequency=1"]
    for key in ("brightness", "contrast", "saturation", "sharpness"):
        ctrls.append(f"{name(key)}={c[key]}")
    wb_auto = "white_balance_temperature_auto" if "white_balance_temperature_auto" in cam \
        else "white_balance_automatic"
    exp_auto = "exposure_auto" if "exposure_auto" in cam else "auto_exposure"
    if c["wb_auto"]:
        ctrls.append(f"{wb_auto}=1")
    else:
        ctrls += [f"{wb_auto}=0", f"{name('wb_temp')}={c['wb_temp']}"]
    if c["exp_auto"]:
        ctrls.append(f"{exp_auto}=3")
    else:
        ctrls += [f"{exp_auto}=1", "exposure_dynamic_framerate=0",
                  f"{name('exposure')}={c['exposure']}"]
    # einzeln setzen, damit ein fehlender/inaktiver Regler die anderen nicht blockiert
    for ctl in ctrls:
        if cam and ctl.split("=", 1)[0] not in cam:
            continue
        subprocess.run(["v4l2-ctl", "-d", dev, "-c", ctl],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_thumb(src, dst, width=THUMB_W):
    """Vorschaubild erzeugen; -lowres dekodiert JPEGs verkleinert (schnell auf schwacher CPU)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part.jpg"
    w, _ = jpeg_size(src)
    lowres = 0
    while w and lowres < 3 and w / (2 ** (lowres + 1)) >= width:
        lowres += 1
    cmd = ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    if lowres:
        cmd += ["-lowres", str(lowres)]
    cmd += ["-i", src, "-vf", f"scale={width}:-2", "-q:v", "5", tmp]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    if os.path.exists(tmp):
        os.replace(tmp, dst)
        return dst
    return None


def jpeg_size(path):
    """(Breite, Hoehe) aus dem JPEG-Header lesen, ohne zu dekodieren."""
    try:
        with open(path, "rb") as f:
            data = f.read(65536)
    except OSError:
        return 0, 0
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return 0, 0


def grab(c, path, size=None, warmup=None, quality=None, mode=None):
    dev = resolve_device(c)
    fmt = resolve_format(c, dev)
    size = resolve_size(c, dev, size)
    warmup = c["warmup"] if warmup is None else warmup
    quality = quality or c["quality"]
    mode = mode or resolve_mode(c, fmt)
    tmp = path + ".part.jpg"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "v4l2", "-input_format", fmt, "-video_size", size, "-i", dev]
    if mode == "copy":
        # MJPEG-Frame unveraendert speichern: kein Dekodieren/Kodieren, ideal fuer Pi 1/Zero.
        # Vorlauf zeitbasiert (~30 fps), damit Belichtung/Weissabgleich einschwingen.
        cmd += ["-ss", f"{max(warmup, 1) / 30:.2f}", "-frames:v", "1",
                "-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-f", "image2", tmp]
    else:
        cmd += ["-vf", f"select=gte(n\\,{warmup})", "-frames:v", "1",
                "-q:v", str(quality), tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        err = r.stderr
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        ok, err = False, "Zeitueberschreitung bei der Aufnahme"
    if not ok or not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError((err or "ffmpeg Fehler").strip()[-300:] + f" [{dev} {fmt} {size} {mode}]")
    os.replace(tmp, path)
    return path


def capture_now(c=None, lock_timeout=90):
    c = c or load_cfg()
    now = datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    day_dir = os.path.join(IMG_DIR, day)
    os.makedirs(day_dir, exist_ok=True)
    name = now.strftime("%Y%m%d_%H%M%S") + ".jpg"
    path = os.path.join(day_dir, name)
    with CamLock(lock_timeout):
        apply_controls(c)
        grab(c, path)
    link = os.path.join(BASE, "latest.jpg")
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(path, link)
    except OSError:
        pass
    make_thumb(path, os.path.join(THUMB_DIR, day, name))
    return path


# ---------------------------------------------------------------- Video-Encoder
@functools.lru_cache(maxsize=4)
def encoder_works(name):
    rc, _, _ = _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", "testsrc=size=640x480:rate=10", "-frames:v", "10",
                     "-pix_fmt", "yuv420p", "-c:v", name, "-b:v", "1M", "-f", "null", "-"],
                    timeout=30)
    return rc == 0


def video_encoder(c=None):
    c = c or load_cfg()
    pref = c.get("video_encoder") or "auto"
    if pref != "auto":
        return pref
    if hw_profile()["arm"] and encoder_works("h264_v4l2m2m"):
        return "h264_v4l2m2m"
    return "libx264"


def recommended_width():
    hw = hw_profile()
    if hw["weak"]:
        return 1280
    return 1920


# ---------------------------------------------------------------- CLI
def info():
    c = load_cfg()
    dev = resolve_device(c)
    fmt = resolve_format(c, dev)
    hw = hw_profile()
    print(f"GrowCam {VERSION}")
    print(f"Hardware : {hw['model'] or '?'} ({hw['machine']}, {hw['cpus']} Kern(e))"
          f"{'  -> schwache CPU, sparsamer Modus' if hw['weak'] else ''}")
    print(f"Daten    : {BASE}")
    print(f"Kamera   : {dev}")
    fmts = camera_formats(dev)
    if not fmts:
        print("           (keine Formate gemeldet - Kamera angeschlossen? Benutzer in Gruppe 'video'?)")
    for k, v in fmts.items():
        print(f"  {k:8s}: {', '.join(v)}")
    print(f"Aufnahme : {fmt} {resolve_size(c, dev)} Modus={resolve_mode(c, fmt)}")
    print(f"Video    : Encoder={video_encoder(c)}  empfohlene Breite={recommended_width()}")


def main(argv):
    if len(argv) >= 2 and argv[1] == "capture":
        c = load_cfg()
        if ("--cron" in argv or "--scheduled" in argv) and not due(c):
            return 0
        try:
            p = capture_now(c)
            print(p)
            return 0
        except Exception as e:  # noqa
            log_error(f"Aufnahme fehlgeschlagen: {e}")
            print(e, file=sys.stderr)
            return 1
    if len(argv) >= 2 and argv[1] == "info":
        info()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
