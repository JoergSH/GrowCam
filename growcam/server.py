#!/usr/bin/env python3
"""GrowCam-Weboberflaeche (nur Python-Standardbibliothek).
Start: python3 server.py [port]   -> http://<ip>:8080
Umgebung: GROWCAM_PORT (Standard 8080), GROWCAM_BIND (Standard 0.0.0.0), GROWCAM_DATA
"""
import datetime
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import render  # noqa: E402
import stabilize  # noqa: E402

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
        "dark_today": dark_count(now.strftime("%Y-%m-%d")),
        "dark_total": dark_count(),
        "version": core.VERSION,
        "name": os.environ.get("GROWCAM_NAME", ""),
        "hw": core.hw_profile(),
        "data_dir": core.BASE,
    }


def dark_count(day=None):
    """Anzahl aussortierter (zu dunkler) Bilder, fuer einen Tag oder insgesamt."""
    if not os.path.isdir(core.DARK_DIR):
        return 0
    days = [day] if day else os.listdir(core.DARK_DIR)
    n = 0
    for d in days:
        p = os.path.join(core.DARK_DIR, d)
        if DAY_RE.match(d) and os.path.isdir(p):
            n += sum(1 for f in os.listdir(p) if IMG_RE.match(f))
    return n


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


# ---------------------------------------------------------------- video job
def _set_pid(pid):
    with job_lock:
        job["pid"] = pid
        job["done"] = 0


def _set_done(n):
    with job_lock:
        job["done"] = n


def _cancelled():
    with job_lock:
        return job.get("state") == "cancelled"


def _finish(rc, err, out_name, n):
    if rc == 0:
        with job_lock:
            job.update(state="done", done=n, file=out_name, finished=time.strftime("%H:%M:%S"))
    else:
        with job_lock:
            if job.get("state") != "cancelled":
                job.update(state="error", error=(err or "ffmpeg Fehler")[-500:])


def video_worker(files, fps, width, overlay, out_name, stab=False):
    tmpdir = tempfile.mkdtemp(prefix="growcam_vid_")
    try:
        out = os.path.join(core.VID_DIR, out_name)
        os.makedirs(core.VID_DIR, exist_ok=True)
        encoder = core.video_encoder()
        with job_lock:
            job["encoder"] = encoder
        if stab:
            # noch nicht ausgerichtete Bilder (z. B. von heute) zuerst ausrichten
            def prog(a, b):
                with job_lock:
                    job.update(phase="align", done=a, total_align=b)
            with job_lock:
                job.update(phase="align", done=0)
            stabilize.align_all(progress=prog, should_stop=_cancelled)
            if _cancelled():
                return
            with job_lock:
                job.update(phase="encode", done=0)
            rc, err = stabilize.render_video(files, out, fps, width, overlay, encoder,
                                             _set_pid, _set_done)
            return _finish(rc, err, out_name, len(files))
        for i, f in enumerate(files):
            os.symlink(f, os.path.join(tmpdir, f"f{i:06d}.jpg"))
        vf = render.overlay_filter(files, fps, tmpdir) if overlay else []
        part = out + ".part.mp4"
        src_w, _ = core.jpeg_size(files[0])
        rc, err = render.run(render.jpeg_cmd(tmpdir, fps, width, src_w, vf, encoder, part),
                             _set_pid, _set_done)
        if rc != 0 and not _cancelled() and encoder != "libx264":
            # Hardware-Encoder gescheitert -> Software-Fallback
            with job_lock:
                job["encoder"] = "libx264"
            rc, err = render.run(render.jpeg_cmd(tmpdir, fps, width, src_w, vf, "libx264", part),
                                 _set_pid, _set_done)
        if rc == 0 and os.path.exists(part):
            os.replace(part, out)
        elif os.path.exists(part):
            os.remove(part)
        _finish(rc, err, out_name, len(files))
    except Exception as e:  # noqa
        with job_lock:
            job.update(state="error", error=str(e))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def concat_worker(files, out_name):
    out = os.path.join(core.VID_DIR, out_name)
    part = out + ".part.mp4"
    try:
        rc, err = render.concat(files, part)
        if rc == 0:
            os.replace(part, out)
        elif os.path.exists(part):
            os.remove(part)
        _finish(rc, err, out_name, len(files))
    except Exception as e:  # noqa
        with job_lock:
            job.update(state="error", error=str(e))


def _range(p):
    day_from = p.get("from") or None
    day_to = p.get("to") or None
    for d in (day_from, day_to):
        if d and not DAY_RE.match(d):
            raise ValueError("Ungültiges Datum")
    return day_from, day_to


def _busy():
    with job_lock:
        return job.get("state") == "running"


def start_video(p):
    if _busy():
        return {"ok": False, "error": "Es läuft bereits ein Video-Auftrag."}
    try:
        day_from, day_to = _range(p)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    fps = max(1, min(60, int(p.get("fps", 24))))
    width = int(p.get("width", core.recommended_width()))
    if width not in (640, 854, 1280, 1920, 2048):
        width = core.recommended_width()
    nth = max(1, min(100, int(p.get("nth", 1))))
    overlay = bool(p.get("overlay", True))
    stab = bool(p.get("stabilize")) and stabilize.available()
    files = all_images(day_from, day_to)[::nth]
    if len(files) < 2:
        return {"ok": False, "error": "Zu wenige Bilder im gewählten Zeitraum."}
    a = os.path.basename(os.path.dirname(files[0]))
    b = os.path.basename(os.path.dirname(files[-1]))
    out_name = f"growcam_{a}_bis_{b}_{fps}fps{'_stab' if stab else ''}_{time.strftime('%H%M%S')}.mp4"
    with job_lock:
        job.clear()
        job.update(state="running", phase="encode", total=len(files), done=0, file=out_name,
                   stab=stab, started=time.strftime("%H:%M:%S"), seconds=round(len(files) / fps, 1))
    threading.Thread(target=video_worker, args=(files, fps, width, overlay, out_name, stab),
                     daemon=True).start()
    return {"ok": True, "job": job_snapshot()}


def start_concat(p):
    """Tagesvideos (videos/tag_*.mp4) ohne Neukodieren zusammenfuegen."""
    if _busy():
        return {"ok": False, "error": "Es läuft bereits ein Video-Auftrag."}
    try:
        day_from, day_to = _range(p)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    sel = [d for d in stabilize.info()["daily"]
           if (not day_from or d >= day_from) and (not day_to or d <= day_to)]
    if not sel:
        return {"ok": False, "error": "Keine Tagesvideos im gewählten Zeitraum."}
    prm = {json.dumps({k: v for k, v in (stabilize.load_day(d).get("video") or {}).items()
                       if k in ("fps", "width", "overlay")}, sort_keys=True) for d in sel}
    if len(prm) > 1:
        return {"ok": False, "error": "Die Tagesvideos wurden mit unterschiedlichen Einstellungen "
                                      "erstellt. Sie werden in der nächsten Nacht angeglichen."}
    out_name = f"growcam_{sel[0]}_bis_{sel[-1]}_tage_{time.strftime('%H%M%S')}.mp4"
    with job_lock:
        job.clear()
        job.update(state="running", phase="concat", total=len(sel), done=0, file=out_name,
                   started=time.strftime("%H:%M:%S"), seconds=None)
    threading.Thread(target=concat_worker, args=([stabilize.daily_path(d) for d in sel], out_name),
                     daemon=True).start()
    return {"ok": True, "job": job_snapshot()}


def cancel_video():
    with job_lock:
        if job.get("state") != "running":
            return {"ok": False}
        job["state"] = "cancelled"
        pid = job.get("pid")
    if pid:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    return {"ok": True}


def list_videos():
    if not os.path.isdir(core.VID_DIR):
        return []
    out = []
    for f in os.listdir(core.VID_DIR):
        if VID_RE.match(f) and not f.endswith(".part.mp4"):
            st = os.stat(os.path.join(core.VID_DIR, f))
            out.append({"name": f, "size": st.st_size, "daily": f.startswith(stabilize.DAILY_PREFIX),
                        "mtime": st.st_mtime,
                        "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(st.st_mtime))})
    # eigene Videos (neueste zuerst), danach die Tagesvideos (neuester Tag zuerst)
    out.sort(key=lambda v: (v["daily"], -v["mtime"] if not v["daily"] else 0, v["name"]), reverse=False)
    daily = sorted((v for v in out if v["daily"]), key=lambda v: v["name"], reverse=True)
    return [v for v in out if not v["daily"]] + daily


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
            if path == "/api/stab":
                return self.send_json(stabilize.info())
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
            if path == "/api/video/concat":
                return self.send_json(start_concat(p))
            if path == "/api/stab/reset":
                start = p.get("start", "")
                if start and not DAY_RE.match(start):
                    return self.send_err(400, "start")
                try:
                    stabilize.reset(start)
                except RuntimeError as e:
                    return self.send_json({"ok": False, "error": str(e)})
                return self.send_json({"ok": True})
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
