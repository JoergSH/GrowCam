"""GrowCam: gemeinsame ffmpeg-Helfer fuer Videos (Weboberflaeche und Nachtlauf)."""
import datetime
import functools
import os
import subprocess
import threading

import core

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
ENC_BITRATE = {640: "2M", 854: "3M", 1280: "5M", 1920: "8M", 2048: "10M"}
OVERLAY_STYLE = ("FontName=DejaVu Sans,FontSize=11,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,"
                 "Alignment=3,MarginR=8,MarginV=6")


def img_time(name):
    return datetime.datetime.strptime(os.path.basename(name)[:15], "%Y%m%d_%H%M%S")


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


def overlay_filter(files, fps, tmpdir):
    """Datum/Uhrzeit als Untertitel einblenden (ein Eintrag je Bild)."""
    if not has_filter("subtitles"):
        return []
    srt = os.path.join(tmpdir, "t.srt")
    with open(srt, "w") as fh:
        for i, f in enumerate(files):
            t = img_time(f)
            txt = f"{WEEKDAYS[t.weekday()]} {t:%d.%m.%Y  %H:%M}"
            # Zeitfenster um die Bildmitte legen: Bild i liegt bei i/fps, Rundung auf ms
            # darf nicht dazu fuehren, dass ein Bild noch den vorigen Text zeigt
            a, b = max(0.0, (i - 0.5) / fps), (i + 0.5) / fps
            fh.write(f"{i+1}\n{srt_time(a)} --> {srt_time(b)}\n{txt}\n\n")
    return [f"subtitles={srt}:force_style='{OVERLAY_STYLE}'"]


def _enc(encoder, width):
    if encoder == "h264_v4l2m2m":
        return ["-c:v", "h264_v4l2m2m", "-b:v", ENC_BITRATE.get(width, "6M")]
    enc = ["-c:v", "libx264", "-preset", "ultrafast" if core.hw_profile()["weak"] else "veryfast",
           "-crf", "21"]
    if core.hw_profile()["weak"]:
        enc += ["-threads", "1"]
    return enc


def _head(fps):
    return ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-nostats", "-progress", "pipe:1", "-y", "-framerate", str(fps)]


def jpeg_cmd(tmpdir, fps, width, src_w, vf_extra, encoder, part):
    """Video aus f%06d.jpg im tmpdir."""
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
    else:
        vf = [f"scale={width}:-2"]
    vf.append("format=yuv420p")
    vf += vf_extra
    return (_head(fps) + inp + ["-i", os.path.join(tmpdir, "f%06d.jpg"), "-vf", ",".join(vf)] +
            _enc(encoder, width) + ["-movflags", "+faststart", part])


def raw_cmd(w, h, fps, vf_extra, encoder, part):
    """Video aus rohen BGR-Bildern, die ueber stdin kommen (stabilisierte Bilder)."""
    vf = ["format=yuv420p"] + vf_extra
    return (_head(fps) + ["-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-i", "-",
             "-vf", ",".join(vf)] + _enc(encoder, w) + ["-movflags", "+faststart", part])


def out_size(src_w, src_h, width, encoder):
    """Zielgroesse (gerade Werte); der Pi-Hardware-Encoder schafft max. 1080 Zeilen."""
    w = min(width, src_w) if src_w else width
    h = round(w * src_h / src_w / 2) * 2 if src_w else round(w * 3 / 4 / 2) * 2
    if encoder == "h264_v4l2m2m" and h > 1080:
        w, h = round(1080 * src_w / src_h / 2) * 2, 1080
    return w - w % 2, h


def run(cmd, on_start=None, on_progress=None, feed=None):
    """ffmpeg starten, Fortschritt (Frame-Nummer) melden. feed(stdin) liefert ggf. Rohbilder."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
    if on_start:
        on_start(p.pid)
    feed_err = []
    t = None
    if feed:
        def _feed():
            try:
                feed(p.stdin)
            except (BrokenPipeError, OSError):
                pass
            except Exception as e:  # noqa
                feed_err.append(str(e))
                p.terminate()
            finally:
                try:
                    p.stdin.close()
                except OSError:
                    pass
        t = threading.Thread(target=_feed, daemon=True)
        t.start()
    err = []
    te = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    te.start()
    for line in p.stdout:
        if line.startswith(b"frame=") and on_progress:
            try:
                on_progress(int(line.split(b"=", 1)[1]))
            except ValueError:
                pass
    p.wait()
    te.join(5)
    if t:
        t.join(5)
    msg = (err[0].decode("utf-8", "replace") if err else "") + " ".join(feed_err)
    return (p.returncode if not feed_err else 1), msg


def concat(files, out):
    """Fertige Videos gleicher Art ohne Neukodieren aneinanderhaengen."""
    lst = out + ".txt"
    with open(lst, "w") as fh:
        for f in files:
            fh.write("file '%s'\n" % f.replace("'", "'\\''"))
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                            "-movflags", "+faststart", out], capture_output=True, text=True)
        return r.returncode, r.stderr
    finally:
        os.remove(lst)
