#!/usr/bin/env python3
"""GrowCam-Bildstabilisierung (optional, braucht OpenCV: sudo apt install python3-opencv).

Alle Bilder werden auf ein gemeinsames Referenzbild ausgerichtet, damit kleine Verschiebungen
(Topf angestossen, Autofokus, Zelt-Wackeln) im Zeitraffer verschwinden. Die Transformationen
werden je Tag gespeichert (stab/<tag>.json), so muss jedes Bild nur einmal ausgerichtet werden.
Wird die Kamera absichtlich verstellt, legt GrowCam automatisch eine neue Referenz an (Schnitt).

Nachtlauf (Timer, stuendlich): python3 stabilize.py nightly
  - laeuft nur, wenn das Licht aus ist
  - richtet neue Bilder aus und rendert fuer jeden abgeschlossenen Tag videos/tag_<tag>.mp4
Weitere Befehle: align | day JJJJ-MM-TT | reset [--start JJJJ-MM-TT] | info
"""
import datetime
import fcntl
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import render  # noqa: E402

STAB_DIR = os.path.join(core.BASE, "stab")
REF_FILE = os.path.join(STAB_DIR, "reference.json")
STATE_FILE = os.path.join(STAB_DIR, "state.json")
LOCK_FILE = os.path.join(STAB_DIR, ".lock")
CC_MIN = 0.80        # ab hier gilt ein Bild als sicher ausgerichtet
CC_CHAIN = 0.60      # neue Referenz an alte anschliessen (schleichende Veraenderung)
CUT_AFTER = 3        # so viele Fehlschlaege in Folge = Kamera wurde verstellt
CROP = 0.97          # Rand, der beim Ausrichten wegfaellt
DAILY_PREFIX = "tag_"

cv2 = np = None


def available():
    return importlib.util.find_spec("cv2") is not None and importlib.util.find_spec("numpy") is not None


def _cv():
    global cv2, np
    if cv2 is None:
        import cv2 as _c
        import numpy as _n
        cv2, np = _c, _n
        cv2.setNumThreads(1 if core.hw_profile()["weak"] else 2)
    return cv2


# ---------------------------------------------------------------- Ablage
class Lock:
    """Verhindert, dass Nachtlauf und Weboberflaeche gleichzeitig ausrichten."""

    def __init__(self, wait=True):
        self.wait = wait

    def __enter__(self):
        os.makedirs(STAB_DIR, exist_ok=True)
        self.fh = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
        except BlockingIOError:
            self.fh.close()
            raise RuntimeError("Die Stabilisierung läuft gerade schon.")
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def day_file(day):
    return os.path.join(STAB_DIR, day + ".json")


def load_day(day):
    return _load(day_file(day), {"transforms": {}, "cuts": []})


def load_refs():
    return _load(REF_FILE, {"refs": []})


def days():
    if not os.path.isdir(core.IMG_DIR):
        return []
    return sorted(d for d in os.listdir(core.IMG_DIR)
                  if len(d) == 10 and d[4] == "-" and os.path.isdir(os.path.join(core.IMG_DIR, d)))


def images(day):
    p = os.path.join(core.IMG_DIR, day)
    try:
        return sorted(f for f in os.listdir(p) if f.endswith(".jpg") and len(f) == 19)
    except OSError:
        return []


def start_day(c=None):
    c = c or core.load_cfg()
    d = days()
    s = c.get("stab_start") or ""
    return s if s else (d[0] if d else "")


# ---------------------------------------------------------------- Ausrichtung
def _factor(w):
    """Verkleinerung beim Dekodieren, damit rund 400-700 px breit ausgerichtet wird."""
    f = 1
    while f < 8 and w / (f * 2) >= 400:
        f *= 2
    return f


def prep(path, f):
    _cv()
    flag = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2, 4: cv2.IMREAD_REDUCED_COLOR_4,
            8: cv2.IMREAD_REDUCED_COLOR_8}[f]
    img = cv2.imread(path, flag)
    if img is None:
        raise ValueError("Bild nicht lesbar: " + path)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (30, 40, 30), (95, 255, 255))           # Pflanze ausblenden
    k = max(3, int(img.shape[1] / 80) | 1)
    green = cv2.dilate(green, np.ones((k, k), np.uint8))
    mask = cv2.bitwise_not(green)
    g = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
    return g, mask


def ecc(ref, cur, m0):
    """Liefert (cc, M) mit M: Referenz-Koordinaten -> Bild-Koordinaten (verkleinert)."""
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5)
    try:
        cc, m = cv2.findTransformECC(ref[0], cur[0], m0.astype(np.float32).copy(), cv2.MOTION_AFFINE,
                                     crit, cv2.bitwise_and(ref[1], cur[1]), 5)
        return float(cc), m
    except cv2.error:
        return 0.0, m0


def _full(m, f):
    m = np.array(m, dtype=np.float64).reshape(2, 3).copy()
    m[:, 2] *= f
    return m


def _small(m, f):
    m = np.array(m, dtype=np.float64).reshape(2, 3).copy()
    m[:, 2] /= f
    return m.astype(np.float32)


def _m33(m):
    return np.vstack([np.array(m, dtype=np.float64).reshape(2, 3), [0, 0, 1]])


class Aligner:
    def __init__(self):
        _cv()
        self.refs = load_refs()
        self.cache = {}

    def ref(self, rid=None):
        rs = self.refs["refs"]
        if not rs:
            return None
        r = rs[-1] if rid is None else next((x for x in rs if x["id"] == rid), None)
        return r

    def ref_prep(self, r):
        if r["id"] not in self.cache:
            self.cache[r["id"]] = prep(os.path.join(STAB_DIR, r["file"]), r["f"])
        return self.cache[r["id"]]

    def new_ref(self, path, old=None):
        w, h = core.jpeg_size(path)
        rid = (self.refs["refs"][-1]["id"] + 1) if self.refs["refs"] else 1
        r = {"id": rid, "file": f"reference_{rid}.jpg", "image": os.path.basename(path),
             "w": w, "h": h, "f": _factor(w), "base": [1, 0, 0, 0, 1, 0], "chained": False,
             "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        shutil.copyfile(path, os.path.join(STAB_DIR, r["file"]))
        if old and old["w"] == w and old["h"] == h:
            # an die alte Referenz anschliessen, wenn sich das Bild nur langsam veraendert hat
            cc, m = ecc(self.ref_prep(old), self.ref_prep(r),
                        np.eye(2, 3, dtype=np.float32))
            if cc >= CC_CHAIN:
                # alt -> neu: m bildet alte Referenz-Koordinaten auf das neue Bild ab
                base = _m33(_full(old["base"], 1)) @ np.linalg.inv(_m33(_full(m, r["f"])))
                r["base"] = [round(float(x), 6) for x in base[:2].flatten()]
                r["chained"] = True
        self.refs["refs"].append(r)
        core.write_json_atomic(REF_FILE, self.refs, indent=1)
        return r

    def align_one(self, path, r, m_prev):
        cur = prep(path, r["f"])
        cc, m = ecc(self.ref_prep(r), cur, _small(m_prev, r["f"]))
        if cc < CC_MIN:
            cc2, m2 = ecc(self.ref_prep(r), cur, np.eye(2, 3, dtype=np.float32))
            if cc2 > cc:
                cc, m = cc2, m2
        return cc, [round(float(x), 5) for x in _full(m, r["f"]).flatten()]


def align_all(progress=None, should_stop=None):
    """Alle noch nicht ausgerichteten Bilder ab dem Starttag verarbeiten. Gibt Anzahl zurueck."""
    if not available():
        return 0
    c = core.load_cfg()
    first = start_day(c)
    todo_days = [d for d in days() if d >= first]
    total = sum(len(images(d)) - len(load_day(d)["transforms"]) for d in todo_days)
    if total <= 0:
        return 0
    with Lock():
        al = Aligner()
        r = al.ref()
        m_prev = [1, 0, 0, 0, 1, 0]
        done = 0
        for d in todo_days:
            data = load_day(d)
            tr = data["transforms"]
            names = images(d)
            missing = [n for n in names if n not in tr]
            if not missing:
                if tr:
                    last = tr[max(tr)]
                    r, m_prev = al.ref(last["ref"]) or r, last["m"]
                continue
            fails = []
            changed = 0
            for n in names:
                if n in tr:
                    r, m_prev = al.ref(tr[n]["ref"]) or r, tr[n]["m"]
                    continue
                if should_stop and should_stop():
                    break
                path = os.path.join(core.IMG_DIR, d, n)
                w, h = core.jpeg_size(path)
                if r is None or (w, h) != (r["w"], r["h"]):
                    r = al.new_ref(path, r)
                    data["cuts"].append({"at": n, "ref": r["id"], "reason": "size" if r["id"] > 1 else "start"})
                    m_prev = [1, 0, 0, 0, 1, 0]
                cc, m = al.align_one(path, r, m_prev)
                if cc >= CC_MIN:
                    for fn in fails:   # einzelne Ausreisser (Hand im Bild ...) -> letzte gute Lage
                        tr[fn] = {"m": m_prev, "ref": r["id"], "cc": 0.0, "bad": True}
                    fails = []
                    tr[n] = {"m": m, "ref": r["id"], "cc": round(cc, 4)}
                    m_prev = m
                else:
                    fails.append(n)
                    if len(fails) >= CUT_AFTER:
                        # Kamera wurde verstellt: neue Referenz ab dem ersten Fehlschlag
                        r = al.new_ref(os.path.join(core.IMG_DIR, d, fails[0]), r)
                        data["cuts"].append({"at": fails[0], "ref": r["id"],
                                             "reason": "drift" if r["chained"] else "moved"})
                        m_prev = [1, 0, 0, 0, 1, 0]
                        for fn in fails:
                            cc, m = al.align_one(os.path.join(core.IMG_DIR, d, fn), r, m_prev)
                            tr[fn] = {"m": m, "ref": r["id"], "cc": round(cc, 4)}
                            if cc >= CC_MIN:
                                m_prev = m
                        fails = []
                done += 1
                changed += 1
                if progress:
                    progress(done, total)
                if changed % 25 == 0:
                    _save_day(d, data)
            for fn in fails:   # Tagesende mit offenen Fehlschlaegen: letzte gute Lage
                tr[fn] = {"m": m_prev, "ref": r["id"], "cc": 0.0, "bad": True}
            _save_day(d, data)
            if should_stop and should_stop():
                break
        return done


def _save_day(d, data):
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    core.write_json_atomic(day_file(d), data)


# ---------------------------------------------------------------- Rendern
def final_matrices(files):
    """Je Bild die Matrix Referenz(Basis)-Koordinaten -> Bild-Koordinaten (volle Aufloesung)."""
    refs = {r["id"]: r for r in load_refs()["refs"]}
    cache = {}
    out = []
    for f in files:
        d = os.path.basename(os.path.dirname(f))
        if d not in cache:
            cache[d] = load_day(d)["transforms"]
        t = cache[d].get(os.path.basename(f))
        if t and t["ref"] in refs:
            r = refs[t["ref"]]
            # Basis -> Referenz r -> Bild
            out.append(_m33(t["m"]) @ np.linalg.inv(_m33(r["base"])))
        else:
            out.append(None)
    return out


def render_video(files, out_path, fps, width, overlay, encoder=None, on_start=None, on_progress=None):
    """Stabilisiertes Video rendern. Gibt (rc, fehlertext) zurueck."""
    _cv()
    encoder = encoder or core.video_encoder()
    mats = final_matrices(files)
    sw, sh = core.jpeg_size(files[0])
    ow, oh = render.out_size(sw, sh, width, encoder)
    tmpdir = tempfile.mkdtemp(prefix="growcam_stab_")
    try:
        vf = render.overlay_filter(files, fps, tmpdir) if overlay else []

        def feed(stdin):
            for f, m in zip(files, mats):
                img = cv2.imread(f, cv2.IMREAD_COLOR)
                if img is None:
                    continue
                h, w = img.shape[:2]
                if m is None:
                    m = np.eye(3)
                # Ausgabepixel -> Basis-Koordinaten (mittiger Ausschnitt CROP, skaliert)
                s = ow / (w * CROP)
                t = np.array([[1 / s, 0, w * (1 - CROP) / 2], [0, 1 / s, h * (1 - CROP) / 2], [0, 0, 1]])
                a = (m @ t)[:2].astype(np.float32)
                frame = cv2.warpAffine(img, a, (ow, oh), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                       borderMode=cv2.BORDER_REPLICATE)
                stdin.write(frame.tobytes())

        part = out_path + ".part.mp4"
        cmd = render.raw_cmd(ow, oh, fps, vf, encoder, part)
        rc, err = render.run(cmd, on_start, on_progress, feed)
        if rc != 0 and encoder != "libx264":
            cmd = render.raw_cmd(ow, oh, fps, vf, "libx264", part)
            rc, err = render.run(cmd, on_start, on_progress, feed)
        if rc == 0 and os.path.exists(part):
            os.replace(part, out_path)
        elif os.path.exists(part):
            os.remove(part)
        return rc, err
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def daily_params(c):
    return {"fps": int(c["stab_fps"]), "width": int(c["stab_width"] or core.recommended_width()),
            "overlay": bool(c["stab_overlay"])}


def daily_path(day):
    return os.path.join(core.VID_DIR, f"{DAILY_PREFIX}{day}.mp4")


def render_day(day, c=None):
    c = c or core.load_cfg()
    files = [os.path.join(core.IMG_DIR, day, n) for n in images(day)]
    if len(files) < 2:
        return False
    prm = daily_params(c)
    os.makedirs(core.VID_DIR, exist_ok=True)
    t0 = time.time()
    rc, err = render_video(files, daily_path(day), prm["fps"], prm["width"], prm["overlay"])
    if rc != 0:
        core.log_error(f"Tagesvideo {day} fehlgeschlagen: {err.strip()[-300:]}")
        return False
    data = load_day(day)
    data["video"] = dict(prm, images=len(files), seconds=round(time.time() - t0))
    _save_day(day, data)
    return True


def needs_render(day, c):
    p = daily_path(day)
    data = load_day(day)
    v = data.get("video")
    if not os.path.exists(p) or not v:
        return True
    if v.get("images") != len(images(day)):
        return True
    return {k: v.get(k) for k in ("fps", "width", "overlay")} != daily_params(c)


def nightly(force=False):
    if not available():
        print("OpenCV fehlt (sudo apt install python3-opencv) – Stabilisierung aus.")
        return 0
    c = core.load_cfg()
    if not force:
        if not c.get("stab_nightly", True):
            return 0
        always_on = c["light_start"] % 24 == c["light_end"] % 24
        now = datetime.datetime.now()
        if core.light_on(c, now) and not (always_on and now.hour == 3):
            return 0
    t0 = time.time()
    st = _load(STATE_FILE, {})
    st.update(running=True, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    core.write_json_atomic(STATE_FILE, st)
    try:
        n = align_all()
        today = time.strftime("%Y-%m-%d")
        rendered = []
        for d in days():
            if d < start_day(c) or d >= today:
                continue
            if needs_render(d, c):
                if not force and core.light_on(core.load_cfg()) and not always_on_now(c):
                    break  # Licht wieder an: Rest in der naechsten Dunkelphase
                if render_day(d, c):
                    rendered.append(d)
        st.update(aligned=n, rendered=rendered, seconds=round(time.time() - t0), error="")
        print(f"ausgerichtet: {n} Bilder, Tagesvideos: {', '.join(rendered) or '-'}")
    except Exception as e:  # noqa
        st["error"] = str(e)
        core.log_error(f"Stabilisierung: {e}")
        raise
    finally:
        st.update(running=False, finished=time.strftime("%Y-%m-%d %H:%M:%S"))
        core.write_json_atomic(STATE_FILE, st)
    return 0


def always_on_now(c):
    return c["light_start"] % 24 == c["light_end"] % 24


def reset(start=None):
    """Alle Ausrichtungen verwerfen; naechster Lauf beginnt mit neuer Referenz."""
    with Lock(wait=False):
        for f in os.listdir(STAB_DIR):
            if f.endswith(".json") or f.startswith("reference_"):
                os.remove(os.path.join(STAB_DIR, f))
    if start is not None:
        c = core.load_cfg()
        c["stab_start"] = start
        core.save_cfg(c)


def info():
    c = core.load_cfg()
    refs = load_refs()["refs"]
    first = start_day(c)
    ds = [d for d in days() if d >= first] if first else []
    aligned = total = 0
    cuts = []
    for d in ds:
        data = load_day(d)
        aligned += len(data["transforms"])
        total += len(images(d))
        cuts += [dict(x, day=d) for x in data.get("cuts", []) if x.get("reason") not in ("start",)]
    daily = sorted(f[len(DAILY_PREFIX):-4] for f in os.listdir(core.VID_DIR)
                   if f.startswith(DAILY_PREFIX) and f.endswith(".mp4")) if os.path.isdir(core.VID_DIR) else []
    return {"available": available(), "nightly": c.get("stab_nightly", True), "start": first,
            "refs": len(refs), "aligned": aligned, "total": total, "cuts": cuts,
            "daily": daily, "state": _load(STATE_FILE, {})}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "info"
    if cmd == "nightly":
        return nightly(force="--force" in argv)
    if not available() and cmd != "info":
        print("OpenCV fehlt: sudo apt install python3-opencv")
        return 1
    if cmd == "align":
        t0 = time.time()
        n = align_all(progress=lambda a, b: print(f"\r{a}/{b}", end="", flush=True))
        print(f"\n{n} Bilder in {time.time()-t0:.0f} s ausgerichtet")
        return 0
    if cmd == "day" and len(argv) > 2:
        align_all()
        t0 = time.time()
        ok = render_day(argv[2])
        print(("fertig: " + daily_path(argv[2])) if ok else "fehlgeschlagen", f"({time.time()-t0:.0f} s)")
        return 0 if ok else 1
    if cmd == "reset":
        start = argv[argv.index("--start") + 1] if "--start" in argv else None
        reset(start)
        print("zurückgesetzt" + (f", Start ab {start}" if start else ""))
        return 0
    print(json.dumps(info(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
