#!/usr/bin/env python3
"""
Builds data.js (the list of albums and photos the site shows) from the folders inside images/.

Folder layout:   images/<medium>/<group>/<album>/[<film stock>/]photo.jpg
    e.g.         images/film/1-places/2-norway/portra-400/fjord-road.jpg

- Folder names become the names shown on the site: "new-zealand" -> "New Zealand".
- A leading number sets the order and is not shown: "1-new-zealand" -> "New Zealand".
- A folder of only years (2026, 2025, ...) is sorted newest first.
- A photo inside a film stock folder goes into that stock's sub-album.
- Camera details live in settings.csv (one row per photo, created and extended
  automatically). Fill it in with Numbers or Excel, then run this script again.

Run it any time you add, move or rename photos:   python3 build.py
Optional, to read camera details out of digital photos automatically:   pip3 install pillow
"""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote

ROOT = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(ROOT, "images")
CSV_PATH = os.path.join(ROOT, "settings.csv")
OUT = os.path.join(ROOT, "data.js")
LINKS_PATH = os.path.join(ROOT, "links.csv")  # external albums: medium, album, title, url, note
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
FIELDS = ["file", "title", "film", "camera", "lens", "focal", "aperture", "shutter", "iso", "pinned", "featured"]
MEDIUM_ORDER = ["film", "digital"]
# --lite : skip the "open original on double-click" link (use when only web/ is being published,
# e.g. a GitHub-hosted copy that does not include the images/ folder).
LITE = "--lite" in sys.argv
# Film photos show only the camera (and the stock, if the photo sits in a stock folder).
# Change this, or fill the "camera" column in settings.csv for a single photo.
DEFAULT_FILM_CAMERA = "Fuji G617"

# The site shows web-sized copies (made into web/) and opens the original on double-click.
# Your files in images/ are never changed.
WEB = os.path.join(ROOT, "web")
MAX_EDGE = 2400


def yes(v):
    return str(v).strip().lower() in ("y", "yes", "1", "true", "x")


def strip_order(name):
    return re.sub(r"^\d{1,2}[-_ ](?=\D)", "", name)


def display(name):
    n = re.sub(r"[-_]+", " ", strip_order(name)).replace(":", "/").strip()
    if n == n.lower():
        n = " ".join(w[:1].upper() + w[1:] for w in n.split())
    return n


def title_from_file(fname):
    n = os.path.splitext(fname)[0].strip()
    if " " not in n:
        n = re.sub(r"[-_]+", " ", n)
    return n[:1].upper() + n[1:]


def subdirs(path):
    names = [d for d in os.listdir(path) if not d.startswith(".") and os.path.isdir(os.path.join(path, d))]
    if names and all(re.fullmatch(r"\d{4}", n) for n in names):
        return sorted(names, reverse=True)
    return sorted(names, key=lambda s: s.lower())


def image_files(path):
    return sorted(f for f in os.listdir(path)
                  if not f.startswith(".") and os.path.splitext(f)[1].lower() in EXTS
                  and os.path.isfile(os.path.join(path, f)))


def read_exif(path):
    """Camera details from a photo's EXIF data, if Pillow is installed."""
    try:
        from PIL import Image
    except ImportError:
        return None
    out = {}
    try:
        ex = Image.open(path).getexif()
        sub = ex.get_ifd(0x8769)
        get = lambda k: sub.get(k, ex.get(k))
        if ex.get(0x0110):
            out["camera"] = str(ex.get(0x0110)).strip()
        if get(0xA434):
            out["lens"] = str(get(0xA434)).strip()
        if get(0x920A):
            out["focal"] = "%d mm" % round(float(get(0x920A)))
        if get(0x829D):
            out["aperture"] = "f/%g" % float(get(0x829D))
        if get(0x829A):
            t = float(get(0x829A))
            out["shutter"] = ("1/%d s" % round(1 / t)) if 0 < t < 1 else ("%g s" % t)
        iso = get(0x8827) or get(0x8832)
        if iso:
            out["iso"] = str(iso)
    except Exception:
        pass
    return out


def scan():
    if not os.path.isdir(IMG):
        sys.exit("No images folder found next to build.py")
    tree, found = [], []
    seen = {}
    mediums = subdirs(IMG)
    mediums.sort(key=lambda m: MEDIUM_ORDER.index(strip_order(m).lower()) if strip_order(m).lower() in MEDIUM_ORDER else 99)
    for med in mediums:
        med_path = os.path.join(IMG, med)
        med_name = display(med)
        groups = []
        for grp in subdirs(med_path):
            grp_path = os.path.join(med_path, grp)
            albums = []
            for alb in subdirs(grp_path):
                alb_path = os.path.join(grp_path, alb)
                alb_name = display(alb)
                if (med_name, alb_name) in seen:
                    print("WARNING: two albums named '%s' in %s; give one a different name." % (alb_name, med_name))
                seen[(med_name, alb_name)] = True
                albums.append(alb_name)
                for f in image_files(alb_path):
                    found.append((med_name, alb_name, "", os.path.join(alb_path, f)))
                for st in subdirs(alb_path):
                    for dp, dn, fn in os.walk(os.path.join(alb_path, st)):
                        dn[:] = sorted(d for d in dn if not d.startswith("."))
                        parts = os.path.relpath(dp, alb_path).split(os.sep)
                        label = " \u00b7 ".join(display(x) for x in parts)
                        for f in image_files(dp):
                            found.append((med_name, alb_name, label, os.path.join(dp, f)))
            groups.append({"title": display(grp), "albums": albums})
        tree.append({"medium": med_name, "groups": groups})
    return tree, found


def load_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(fh)]


def gf670_format(path):
    """6x6 for square frames, 6x7 for the rest (needs Pillow)."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        w, h = Image.open(path).size
    except Exception:
        return None
    return "6x6" if max(w, h) / min(w, h) < 1.12 else "6x7"


_warned_resize = False


def make_web(path, key):
    """Makes a web-sized copy in web/ and returns its path relative to the site, or None to use the original."""
    global _warned_resize
    out_rel = "web/" + os.path.splitext(key)[0] + ".jpg"
    out = os.path.join(ROOT, *out_rel.split("/"))
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path):
        return out_rel
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(path)
        if max(im.size) <= MAX_EDGE and path.lower().endswith((".jpg", ".jpeg")):
            return None  # already small: use the original as is
        print("Resizing %s ..." % key)
        im.draft("RGB", (MAX_EDGE, MAX_EDGE))
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        im.save(out, "JPEG", quality=85, optimize=True, progressive=True, icc_profile=icc)
        return out_rel
    except ImportError:
        pass
    except Exception as e:
        print("Could not resize %s: %s" % (key, e))
        return None
    if shutil.which("sips"):  # macOS built-in fallback
        print("Resizing %s ..." % key)
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", "-Z", str(MAX_EDGE), path, "--out", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.exists(out):
            return out_rel
    if not _warned_resize:
        print("Could not make web-sized copies (install Pillow: pip3 install pillow). Using the originals.")
        _warned_resize = True
    return None


def load_links():
    """External links shown inside an album (e.g. a Google Photos album). Edit links.csv."""
    if not os.path.exists(LINKS_PATH):
        return []
    with open(LINKS_PATH, newline="", encoding="utf-8-sig") as fh:
        rows = [{k: (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(fh)]
    return [{"medium": r.get("medium", ""), "album": r.get("album", ""), "title": r.get("title", ""),
             "url": r.get("url", ""), "note": r.get("note", "")} for r in rows if r.get("url") and r.get("title")]


def main():
    tree, found = scan()
    rows = load_rows()
    by_key = {r.get("file", ""): r for r in rows}
    used = set()
    warned = False
    photos = []
    for med, alb, stock, path in found:
        key = os.path.relpath(path, IMG).replace(os.sep, "/")
        row = by_key.get(key)
        if row is None:  # photo moved to another album: match by file name if unambiguous
            base = os.path.basename(key)
            cand = [r for r in rows if os.path.basename(r.get("file", "")) == base and r.get("file") not in {p[3] for p in found_keys(found)}]
            if len(cand) == 1:
                row = cand[0]
                row["file"] = key
        if row is None:
            row = {k: "" for k in FIELDS}
            row["file"] = key
            ex = read_exif(path)
            if ex is None and not warned:
                print("Note: install Pillow (pip3 install pillow) to fill camera details from digital photos automatically.")
                warned = True
            row.update(ex or {})
            rows.append(row)
            by_key[key] = row
        used.add(row["file"])
        web_rel = make_web(path, key)
        film_name = display(os.path.basename(os.path.dirname(path))) if stock else ""
        if med.lower() == "film":  # show the film defaults in settings.csv so they can be edited per photo
            if not row.get("camera"):
                row["camera"] = DEFAULT_FILM_CAMERA
            if not row.get("film") and film_name:
                row["film"] = film_name
            if row.get("camera", "").strip().lower() in ("fujifilm gf670", "fuji gf670", "gf670"):
                fmt = gf670_format(path)
                if fmt:
                    row["camera"] = "Fujifilm GF670 (%s)" % fmt
        vals = lambda k: row.get(k, "")
        if med.lower() == "film":
            spec = [("Camera", vals("camera") or DEFAULT_FILM_CAMERA), ("Film", vals("film") or film_name)]
        else:
            spec = [("Camera", vals("camera")), ("Lens", vals("lens")), ("Focal", vals("focal")),
                    ("Aperture", vals("aperture")), ("Shutter", vals("shutter")), ("ISO", vals("iso"))]
        p = {
            "title": vals("title") or title_from_file(os.path.basename(path)),
            "medium": med,
            "album": alb,
            "src": "/".join(quote(s) for s in (web_rel or "images/" + key).split("/")),
            "settings": [[k, v] for k, v in spec if v],
        }
        sub_path = stock.split(" \u00b7 ") if stock else ([vals("film")] if med.lower() == "film" and vals("film") else [])
        if sub_path:
            p["sub"] = sub_path
        if yes(vals("pinned")):
            p["pinned"] = True
        if yes(vals("featured")):
            p["featured"] = True
        if web_rel and not LITE:
            p["full"] = "images/" + "/".join(quote(s) for s in key.split("/"))
        photos.append(p)

    # write settings.csv (keeps every row, including ones whose photo has moved or been deleted)
    order = {k: i for i, k in enumerate(p[3] for p in found_keys(found))}
    rows.sort(key=lambda r: (order.get(r.get("file", ""), 10 ** 9), r.get("file", "")))
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("/* Generated by build.py from the images folder. Do not edit; run build.py again. */\n")
        fh.write("/* lite build: double-click opens the web copy, not the original */\n" if LITE else "")
        fh.write("var TREE = " + json.dumps(tree, indent=1, ensure_ascii=False) + ";\n")
        fh.write("var PHOTOS = " + json.dumps(photos, indent=1, ensure_ascii=False) + ";\n")
        fh.write("var LINKS = " + json.dumps(load_links(), indent=1, ensure_ascii=False) + ";\n")

    stale = [r["file"] for r in rows if r.get("file") and r["file"] not in used]
    albums = sum(len(g["albums"]) for m in tree for g in m["groups"])
    print("Built data.js: %d photos in %d albums." % (len(photos), albums))
    if stale:
        print("%d row(s) in settings.csv have no matching photo (moved or deleted); delete them if you like." % len(stale))


def found_keys(found):
    return [(m, a, s, os.path.relpath(p, IMG).replace(os.sep, "/")) for m, a, s, p in found]


if __name__ == "__main__":
    main()
