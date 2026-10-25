#!/usr/bin/env python3
"""GrowCam-Weboberflaeche (nur Python-Standardbibliothek).
Start: python3 server.py [port]   -> http://<ip>:8080
Umgebung: GROWCAM_PORT (Standard 8080), GROWCAM_BIND (Standard 0.0.0.0), GROWCAM_DATA
"""
import datetime
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IMG_RE = re.compile(r"^(\d{8})_(\d{6})\.jpg$")
VID_RE = re.compile(r"^[\w.-]+\.mp4$")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

job_lock = threading.Lock()
job = {"state": "idle"}
preview_lock = threading.Lock()


# ---------------------------------------------------------------- helpers
def img_time(name):
    m = IMG_RE.match(name)
    if not m:
        return None
    return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")


def list_days():
    if not os.path.isdir(core.IMG_DIR):
        return []
    return sorted(d for d in os.listdir(core.IMG_DIR)
                  if DAY_RE.match(d) and os.path.isdir(os.path.join(core.IMG_DIR, d)))


def list_images(day):
    p = os.path.join(core.IMG_DIR, day)
    if not os.path.isdir(p):
        return []
    return sorted(f for f in os.listdir(p) if IMG_RE.match(f))


def day_info(day, interval):
    imgs = list_images(day)
    times = [img_time(i) for i in imgs]
    gaps = []
    lim = datetime.timedelta(minutes=interval * 1.5 + 1)
    for a, b in zip(times, times[1:]):
        if b - a > lim:
            gaps.append([a.strftime("%H:%M"), b.strftime("%H:%M")])
    d = datetime.date.fromisoformat(day)
    return {
        "day": day,
        "label": f"{WEEKDAYS[d.weekday()]} {d:%d.%m.%Y}",
        "count": len(imgs),
        "first": times[0].strftime("%H:%M") if times else None,
        "last": times[-1].strftime("%H:%M") if times else None,
        "gaps": gaps,
    }


def all_images(day_from=None, day_to=None):
    out = []
    for d in list_days():
        if day_from and d < day_from:
            continue
        if day_to and d > day_to:
            continue
        out += [os.path.join(core.IMG_DIR, d, f) for f in list_images(d)]
    return out


def last_image():
    for d in reversed(list_days()):
        imgs = list_images(d)
        if imgs:
            return d, imgs[-1]
    return None, None


def status():
    c = core.load_cfg()
    du = shutil.disk_usage(core.BASE)
    d, f = last_image()
    now = datetime.datetime.now()
    last = img_time(f) if f else None
    age = (now - last).total_seconds() if last else None
    total = sum(len(list_images(x)) for x in list_days())
    warn = None
    if core.light_on(c, now) and (age is None or age > (c["interval_min"] * 2 + 2) * 60):
        warn = "Seit mehr als zwei Intervallen kein neues Bild!"
    errors = []
    try:
        with open(core.ERR_LOG) as fh:
            errors = fh.read().splitlines()[-10:]
    except OSError:
        pass
    per_img = 0
    if f:
        try:
            per_img = os.path.getsize(os.path.join(core.IMG_DIR, d, f))
        except OSError:
            pass
    per_day = 0
    if c["interval_min"]:
        hours = (c["light_end"] - c["light_start"]) % 24 or 24
        per_day = hours * 60 // c["interval_min"]
    return {
        "now": now.strftime("%d.%m.%Y %H:%M:%S"),
        "light_on": core.light_on(c, now),
        "disk_free": du.free, "disk_total": du.total,
        "images_total": total,
        "per_day": per_day,
        "days_left": int(du.free / (per_img * per_day)) if per_img and per_day else None,
        "last_image": f"{d}/{f}" if f else None,
        "last_label": last.strftime("%d.%m. %H:%M") if last else None,
        "last_age_s": age,
        "warning": warn,
        "errors": errors,
        "job": job_snapshot(),
        "config": c,
        "version": core.VERSION,
        "hw": core.hw_profile(),
        "data_dir": core.BASE,
    }


def job_snapshot():
    with job_lock:
        return dict(job)


thumb_lock = threading.Semaphore(1 if core.hw_profile()["weak"] else 2)


def make_thumb(day, name):
    src = os.path.join(core.IMG_DIR, day, name)
    dst = os.path.join(core.THUMB_DIR, day, name)
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    with thumb_lock:  # schwache CPUs nicht mit parallelen ffmpeg-Prozessen ueberlasten
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        return core.make_thumb(src, dst) or src


@functools.lru_cache(maxsize=8)
def has_filter(name):
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-filters"], capture_output=True,
                           text=True, timeout=10)
        return f" {name} " in r.stdout
    except Exception:  # noqa
        return False


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------- video job
ENC_BITRATE = {640: "2M", 854: "3M", 1280: "5M", 1920: "8M", 2048: "10M"}


def encode_cmd(tmpdir, fps, width, src_w, vf_extra, encoder, part):
    weak = core.hw_profile()["weak"]
    inp = []
    # JPEGs verkleinert dekodieren, wenn das Ziel ohnehin viel kleiner ist (spart viel CPU)
    lowres = 0
    while src_w and lowres < 2 and src_w / (2 ** (lowres + 1)) >= width:
        lowres += 1
    if lowres:
        inp = ["-lowres", str(lowres)]
    if encoder == "h264_v4l2m2m":
        # Pi-Hardware-Encoder: max. 1920x1080
        vf = [f"scale=w={width}:h=1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]
        enc = ["-c:v", "h264_v4l2m2m", "-b:v", ENC_BITRATE.get(width, "6M")]
    else:
        vf = [f"scale={width}:-2"]
        enc = ["-c:v", "libx264", "-preset", "ultrafast" if weak else "veryfast", "-crf", "21"]
        if weak:
            enc += ["-threads", "1"]
    vf.append("format=yuv420p")
    vf += vf_extra
    return (["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-nostats", "-progress", "pipe:1", "-y", "-framerate", str(fps)] + inp +
            ["-i", os.path.join(tmpdir, "f%06d.jpg"), "-vf", ",".join(vf)] + enc +
            ["-movflags", "+faststart", part])


def run_encode(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with job_lock:
        job["pid"] = p.pid
        job["done"] = 0
    for line in p.stdout:
        if line.startswith("frame="):
            try:
                n = int(line.split("=", 1)[1])
            except ValueError:
                continue
            with job_lock:
                job["done"] = n
    err = p.stderr.read()
    p.wait()
    return p.returncode, err


def video_worker(files, fps, width, overlay, out_name):
    tmpdir = tempfile.mkdtemp(prefix="growcam_vid_")
    try:
        for i, f in enumerate(files):
            os.symlink(f, os.path.join(tmpdir, f"f{i:06d}.jpg"))
        vf = []
        if overlay and has_filter("subtitles"):
            srt = os.path.join(tmpdir, "t.srt")
            with open(srt, "w") as fh:
                for i, f in enumerate(files):
                    t = img_time(os.path.basename(f))
                    txt = f"{WEEKDAYS[t.weekday()]} {t:%d.%m.%Y  %H:%M}"
                    fh.write(f"{i+1}\n{srt_time(i/fps)} --> {srt_time((i+1)/fps)}\n{txt}\n\n")
            style = ("FontName=DejaVu Sans,FontSize=11,PrimaryColour=&H00FFFFFF,"
                     "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,"
                     "Alignment=3,MarginR=8,MarginV=6")
            vf.append(f"subtitles={srt}:force_style='{style}'")
        out = os.path.join(core.VID_DIR, out_name)
        part = out + ".part.mp4"
        os.makedirs(core.VID_DIR, exist_ok=True)
        src_w, _ = core.jpeg_size(files[0])
        encoder = core.video_encoder()
        with job_lock:
            job["encoder"] = encoder
        rc, err = run_encode(encode_cmd(tmpdir, fps, width, src_w, vf, encoder, part))
        with job_lock:
            cancelled = job.get("state") == "cancelled"
        if rc != 0 and not cancelled and encoder != "libx264":
            # Hardware-Encoder gescheitert -> Software-Fallback
            with job_lock:
                job["encoder"] = "libx264"
            rc, err = run_encode(encode_cmd(tmpdir, fps, width, src_w, vf, "libx264", part))
            with job_lock:
                cancelled = job.get("state") == "cancelled"
        if rc == 0 and os.path.exists(part):
            os.replace(part, out)
            with job_lock:
                job.update(state="done", done=len(files), file=out_name,
                           finished=time.strftime("%H:%M:%S"))
        else:
            if os.path.exists(part):
                os.remove(part)
            with job_lock:
                if not cancelled:
                    job.update(state="error", error=(err or "ffmpeg Fehler")[-500:])
    except Exception as e:  # noqa
        with job_lock:
            job.update(state="error", error=str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_video(p):
    with job_lock:
        if job.get("state") == "running":
            return {"ok": False, "error": "Es läuft bereits ein Video-Auftrag."}
    day_from = p.get("from") or None
    day_to = p.get("to") or None
    for d in (day_from, day_to):
        if d and not DAY_RE.match(d):
            return {"ok": False, "error": "Ungültiges Datum"}
    fps = max(1, min(60, int(p.get("fps", 24))))
    width = int(p.get("width", core.recommended_width()))
    if width not in (640, 854, 1280, 1920, 2048):
        width = core.recommended_width()
    nth = max(1, min(100, int(p.get("nth", 1))))
    overlay = bool(p.get("overlay", True))
    files = all_images(day_from, day_to)[::nth]
    if len(files) < 2:
        return {"ok": False, "error": "Zu wenige Bilder im gewählten Zeitraum."}
    a = os.path.basename(os.path.dirname(files[0]))
    b = os.path.basename(os.path.dirname(files[-1]))
    out_name = f"growcam_{a}_bis_{b}_{fps}fps_{time.strftime('%H%M%S')}.mp4"
    with job_lock:
        job.clear()
        job.update(state="running", total=len(files), done=0, file=out_name,
                   started=time.strftime("%H:%M:%S"), seconds=round(len(files) / fps, 1))
    threading.Thread(target=video_worker, args=(files, fps, width, overlay, out_name),
                     daemon=True).start()
    return {"ok": True, "job": job_snapshot()}


def cancel_video():
    with job_lock:
        if job.get("state") != "running" or not job.get("pid"):
            return {"ok": False}
        job["state"] = "cancelled"
        pid = job["pid"]
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    return {"ok": True}


def list_videos():
    if not os.path.isdir(core.VID_DIR):
        return []
    out = []
    for f in sorted(os.listdir(core.VID_DIR), reverse=True):
        if VID_RE.match(f) and not f.endswith(".part.mp4"):
            st = os.stat(os.path.join(core.VID_DIR, f))
            out.append({"name": f, "size": st.st_size,
                        "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(st.st_mtime))})
    return out


def config_info():
    c = core.load_cfg()
    dev = core.resolve_device(c)
    fmt = core.resolve_format(c, dev)
    sizes = core.camera_sizes(c, dev)
    if c["size"] not in sizes and c["size"] != "auto":
        sizes = [c["size"]] + sizes
    return {"config": c, "sizes": ["auto"] + sizes, "controls": core.control_ranges(c, dev),
            "camera": {"device": dev, "format": fmt, "size": core.resolve_size(c, dev),
                       "mode": core.resolve_mode(c, fmt),
                       "formats": core.camera_formats(dev)},
            "hw": core.hw_profile(), "recommended_width": core.recommended_width()}


# ---------------------------------------------------------------- HTTP
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
        ".css": "text/css", ".jpg": "image/jpeg", ".mp4": "video/mp4",
        ".svg": "image/svg+xml", ".png": "image/png"}


class H(BaseHTTPRequestHandler):
    server_version = "GrowCam/" + core.VERSION

    def log_message(self, *a):
        pass

    # --- responses
    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def send_err(self, code, msg=""):
        self.send_json({"ok": False, "error": msg}, code)

    def send_file(self, path, cache=False, download=False):
        if not os.path.isfile(path):
            return self.send_err(404, "nicht gefunden")
        size = os.path.getsize(path)
        ctype = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        code = 200
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-", 1)
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:
                    start = size - int(b)
                end = min(end, size - 1)
                code = 206
            except ValueError:
                start, end = 0, size - 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(path)}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    # --- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = urllib.parse.unquote(u.path)
        parts = [p for p in path.split("/") if p]
        try:
            if path in ("/", "/index.html"):
                return self.send_file(os.path.join(STATIC, "index.html"))
            if parts[:1] == ["static"] and len(parts) == 2 and "/" not in parts[1] \
                    and not parts[1].startswith("."):
                return self.send_file(os.path.join(STATIC, parts[1]))
            if path == "/api/status":
                return self.send_json(status())
            if path == "/api/config":
                return self.send_json(config_info())
            if path == "/api/days":
                c = core.load_cfg()
                return self.send_json([day_info(d, c["interval_min"])
                                       for d in reversed(list_days())])
            if path == "/api/images":
                day = (q.get("day") or [""])[0]
                if not DAY_RE.match(day):
                    return self.send_err(400, "day")
                return self.send_json([{"name": f, "time": img_time(f).strftime("%H:%M")}
                                       for f in list_images(day)])
            if path == "/api/videos":
                return self.send_json(list_videos())
            if path == "/api/video/status":
                return self.send_json(job_snapshot())
            if path == "/api/preview.jpg":
                return self.preview()
            if path == "/latest.jpg":
                d, f = last_image()
                if not f:
                    return self.send_err(404, "noch kein Bild")
                return self.send_file(os.path.join(core.IMG_DIR, d, f))
            if len(parts) == 3 and parts[0] in ("img", "thumb") and DAY_RE.match(parts[1]) \
                    and IMG_RE.match(parts[2]):
                if parts[0] == "thumb":
                    return self.send_file(make_thumb(parts[1], parts[2]), cache=True)
                return self.send_file(os.path.join(core.IMG_DIR, parts[1], parts[2]),
                                      cache=True, download="dl" in q)
            if len(parts) == 2 and parts[0] == "videos" and VID_RE.match(parts[1]):
                return self.send_file(os.path.join(core.VID_DIR, parts[1]),
                                      download="dl" in q)
            return self.send_err(404, "nicht gefunden")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        p = self.body()
        try:
            if path == "/api/config":
                c = core.validate_cfg(p)
                core.save_cfg(c)
                return self.send_json({"ok": True, "config": c})
            if path == "/api/capture":
                try:
                    f = core.capture_now(lock_timeout=20)
                except Exception as e:  # noqa
                    return self.send_err(500, str(e))
                return self.send_json({"ok": True, "file": os.path.basename(f),
                                       "day": os.path.basename(os.path.dirname(f))})
            if path == "/api/delete_day":
                day = p.get("day", "")
                if not DAY_RE.match(day):
                    return self.send_err(400, "day")
                shutil.rmtree(os.path.join(core.IMG_DIR, day), ignore_errors=True)
                shutil.rmtree(os.path.join(core.THUMB_DIR, day), ignore_errors=True)
                return self.send_json({"ok": True})
            if path == "/api/delete_image":
                day, name = p.get("day", ""), p.get("name", "")
                if not (DAY_RE.match(day) and IMG_RE.match(name)):
                    return self.send_err(400, "name")
                for d in (core.IMG_DIR, core.THUMB_DIR):
                    try:
                        os.remove(os.path.join(d, day, name))
                    except OSError:
                        pass
                return self.send_json({"ok": True})
            if path == "/api/video":
                return self.send_json(start_video(p))
            if path == "/api/video/cancel":
                return self.send_json(cancel_video())
            if path == "/api/video/delete":
                name = p.get("name", "")
                if not VID_RE.match(name):
                    return self.send_err(400, "name")
                try:
                    os.remove(os.path.join(core.VID_DIR, name))
                except OSError:
                    pass
                return self.send_json({"ok": True})
            return self.send_err(404, "nicht gefunden")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa
            return self.send_err(500, str(e))

    def preview(self):
        if not preview_lock.acquire(timeout=8):
            return self.send_err(503, "Vorschau belegt")
        try:
            c = core.load_cfg()
            out = os.path.join(tempfile.gettempdir(), f"growcam_preview_{os.getuid()}.jpg")
            try:
                with core.CamLock(timeout=5):
                    core.apply_controls(c)
                    size = core.nearest_size(core.camera_sizes(c), 1280)
                    core.grab(c, out, size=size, warmup=12, quality=4)
            except TimeoutError:
                return self.send_err(503, "Kamera gerade belegt (Aufnahme läuft)")
            except Exception as e:  # noqa
                return self.send_err(500, str(e))
            return self.send_file(out)
        finally:
            preview_lock.release()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("GROWCAM_PORT", 8080))
    bind = os.environ.get("GROWCAM_BIND", "0.0.0.0")
    for d in (core.IMG_DIR, core.THUMB_DIR, core.VID_DIR):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(core.CFG_FILE):
        core.save_cfg(core.load_cfg())
    srv = ThreadingHTTPServer((bind, port), H)
    srv.daemon_threads = True
    print(f"GrowCam {core.VERSION} läuft auf {bind}:{port}, Daten: {core.BASE}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
