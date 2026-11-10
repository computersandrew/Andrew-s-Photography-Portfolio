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
import base64
import csv
import datetime
import hashlib
import html as html_lib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote

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


# ------------------------------------------------------------------ blog
# Posts live in blog/ as Markdown (.md) or HTML (.html) files, e.g. exported from Claude Docs.
BLOG = os.path.join(ROOT, "blog")
POST_EXTS = (".md", ".markdown", ".html", ".htm")
BLOG_EDGE = 1800
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
IMG_ONLY_RE = re.compile(r'^!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"([^"]*)")?\s*\)$')
TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def h_esc(s):
    return html_lib.escape(str(s), quote=True)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "post"


def md_url(u):
    u = u.strip()
    return u[1:-1] if u.startswith("<") and u.endswith(">") else u


def md_indent(s):
    return len(s.expandtabs(4))


def blog_image(slug, name, data=None, path=None):
    """Saves a web-sized copy of a post image in web/blog/<slug>/ and returns its path on the site."""
    if data is None:
        with open(path, "rb") as fh:
            data = fh.read()
    out_dir = os.path.join(WEB, "blog", slug)
    os.makedirs(out_dir, exist_ok=True)
    stem = slugify(os.path.splitext(name)[0])[:60]
    base = "%s-%s" % (stem, hashlib.sha1(data).hexdigest()[:8])  # same picture -> same file, no re-work
    for ext in (".jpg", ".png", ".gif", ".webp", ".svg"):
        if os.path.exists(os.path.join(out_dir, base + ext)):
            return "web/blog/%s/%s%s" % (slug, base, ext)
    raw_ext = os.path.splitext(name)[1].lower()
    if raw_ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
        raw_ext = ".jpg"
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False) or im.format == "GIF":
            raise ValueError("keep as is")
        alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
        im.thumbnail((BLOG_EDGE, BLOG_EDGE), Image.LANCZOS)
        if alpha:
            out = base + ".png"
            im.convert("RGBA").save(os.path.join(out_dir, out), "PNG", optimize=True)
        else:
            out = base + ".jpg"
            im.convert("RGB").save(os.path.join(out_dir, out), "JPEG", quality=85, optimize=True,
                                   progressive=True, icc_profile=icc)
        return "web/blog/%s/%s" % (slug, out)
    except Exception:
        pass  # no Pillow, or a format it cannot read: keep the file as it is
    out = base + raw_ext
    with open(os.path.join(out_dir, out), "wb") as fh:
        fh.write(data)
    return "web/blog/%s/%s" % (slug, out)


def make_resolver(slug, post_dir):
    """Turns an image reference found in a post into a path on the site."""
    mime_ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}

    def resolve(src):
        s = src.strip()
        m = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", s, re.S)
        if m:
            try:
                data = base64.b64decode(m.group(2))
            except Exception:
                return s
            return blog_image(slug, "image" + mime_ext.get(m.group(1), ".jpg"), data=data)
        if re.match(r"^(https?:)?//", s) or s.startswith("#"):
            return s
        rel = unquote(re.split(r"[?#]", s)[0])
        for base_dir in (post_dir, BLOG, IMG):
            cand = os.path.normpath(os.path.join(base_dir, rel))
            if os.path.isfile(cand):
                return blog_image(slug, os.path.basename(cand), path=cand)
        print("WARNING: image not found for post '%s': %s (put the file next to the post)" % (slug, s[:80]))
        return s

    return resolve


def md_inline(text, resolve):
    toks = []

    def stash(h):
        toks.append(h)
        return "\x00%d\x00" % (len(toks) - 1)

    def img(m):
        return stash('<img src="%s" alt="%s" loading="lazy">' % (h_esc(resolve(md_url(m.group(2)))), h_esc(m.group(1))))

    def link(m):
        u = md_url(m.group(2))
        if re.match(r"^\s*(javascript|vbscript|data):", u, re.I):
            u = "#"
        ext = ' target="_blank" rel="noopener noreferrer"' if re.match(r"^https?:", u, re.I) else ""
        return stash('<a href="%s"%s>' % (h_esc(u), ext)) + m.group(1) + stash("</a>")

    t = re.sub(r"`([^`\n]+)`", lambda m: stash("<code>%s</code>" % h_esc(m.group(1))), text)
    t = re.sub(r'!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)', img, t)
    t = re.sub(r'\[([^\]]+)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)', link, t)
    t = re.sub(r"<(https?://[^>\s]+)>", lambda m: stash('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (h_esc(m.group(1)), h_esc(m.group(1)))), t)
    t = h_esc(t)
    t = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", t)
    t = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", t)
    t = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", t)
    t = re.sub(r" {2,}\n", "<br>\n", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: toks[int(m.group(1))], t)


def md_starts_block(line):
    return bool(re.match(r"^\s*(```|~~~)", line) or re.match(r"^#{1,6}\s", line) or HR_RE.match(line)
                or re.match(r"^\s{0,3}>", line) or LIST_RE.match(line))


def md_split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def md_list(lines, i, resolve):
    n = len(lines)
    m0 = LIST_RE.match(lines[i])
    base = md_indent(m0.group(1))
    ordered = m0.group(2)[0].isdigit()
    start = int(re.match(r"\d+", m0.group(2)).group(0)) if ordered else 1
    items = []
    while i < n:
        line = lines[i]
        if not line.strip():
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n or not items:
                break
            ind = md_indent(lines[j][:len(lines[j]) - len(lines[j].lstrip())])
            if ind > base + 1:  # the current item goes on after the blank line
                items[-1].append("")
                i = j
                continue
            if LIST_RE.match(lines[j]) and ind >= base:
                i = j
                continue
            break
        m = LIST_RE.match(line)
        ind = md_indent(line[:len(line) - len(line.lstrip())])
        if m and base <= ind <= base + 1:
            if m.group(2)[0].isdigit() != ordered:
                break
            items.append([m.group(3)])
            i += 1
            continue
        if ind > base and items:  # nested list or continued text
            items[-1].append(line)
            i += 1
            continue
        if m and ind < base:
            break
        if items and not m and not md_starts_block(line):
            items[-1].append(line.strip())
            i += 1
            continue
        break
    parts = []
    for it in items:
        first, rest = it[0], it[1:]
        box = ""
        mt = re.match(r"^\[([ xX])\]\s+(.*)$", first)
        if mt:
            box = '<input type="checkbox" disabled%s> ' % (" checked" if mt.group(1) in "xX" else "")
            first = mt.group(2)
        body = md_blocks([first] + rest, resolve)
        if "" not in rest and body.startswith("<p>"):  # tight list: no paragraph wrapper
            body = re.sub(r"^<p>(.*?)</p>", r"\1", body, count=1, flags=re.S)
        parts.append("<li>%s%s</li>" % (box, body))
    tag = "ol" if ordered else "ul"
    attr = ' start="%d"' % start if ordered and start != 1 else ""
    cls = ' class="tasks"' if any('type="checkbox"' in p for p in parts) else ""
    return "<%s%s%s>%s</%s>" % (tag, attr, cls, "".join(parts), tag), i


def md_blocks(lines, resolve):
    out, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^\s*(```|~~~)", line)
        if m:
            fence = m.group(1)
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % h_esc("\n".join(buf)))
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$", line)
        if m:
            lv = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lv, md_inline(m.group(2), resolve), lv))
            i += 1
            continue
        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue
        if re.match(r"^\s{0,3}>", line):
            buf = []
            while i < n and re.match(r"^\s{0,3}>", lines[i]):
                buf.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>%s</blockquote>" % md_blocks(buf, resolve))
            continue
        if "|" in line and i + 1 < n and "|" in lines[i + 1] and TABLE_RULE_RE.match(lines[i + 1]):
            head = md_split_row(line)
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(md_split_row(lines[i]))
                i += 1
            th = "".join("<th>%s</th>" % md_inline(c, resolve) for c in head)
            tb = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % md_inline(c, resolve) for c in r) for r in rows)
            out.append('<div class="tablewrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (th, tb))
            continue
        if LIST_RE.match(line):
            h, i = md_list(lines, i, resolve)
            out.append(h)
            continue
        buf = []
        while i < n and lines[i].strip() and (not buf or not md_starts_block(lines[i])):
            buf.append(lines[i].lstrip())
            i += 1
        text = "\n".join(buf).rstrip()
        mi = IMG_ONLY_RE.match(text.strip())
        if mi:
            cap = "<figcaption>%s</figcaption>" % h_esc(mi.group(3)) if mi.group(3) else ""
            out.append('<figure><img src="%s" alt="%s" loading="lazy">%s</figure>' % (h_esc(resolve(md_url(mi.group(2)))), h_esc(mi.group(1)), cap))
        else:
            out.append("<p>%s</p>" % md_inline(text, resolve))
    return "".join(out)


def md_front_matter(text):
    m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for ln in m.group(1).split("\n"):
        k, sep, v = ln.partition(":")
        if sep:
            meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, text[m.end():]


def html_post(raw, resolve):
    """An .html post (for example a Docs HTML export): returns (title, body html)."""
    mb = re.search(r"<body[^>]*>(.*)</body>", raw, re.S | re.I)
    body = mb.group(1) if mb else raw
    for tag in ("script", "style", "iframe", "object", "embed", "form", "noscript"):
        body = re.sub(r"<%s\b.*?</%s\s*>" % (tag, tag), "", body, flags=re.S | re.I)
    body = re.sub(r"<(?:script|style|iframe|object|embed|link|meta|base)\b[^>]*>", "", body, flags=re.I)

    def clean(m):
        t = re.sub(r"""\s(?:on\w+|style|srcdoc)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", "", m.group(0), flags=re.I)
        return re.sub(r"""(href|src)\s*=\s*(["'])\s*(?:javascript|vbscript):[^"']*\2""", r"\1=\2#\2", t, flags=re.I)

    body = re.sub(r"<[^>]+>", clean, body)
    title = None
    mt = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.S | re.I)
    if mt:
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", mt.group(1))).strip()
        body = body.replace(mt.group(0), "", 1)
    else:
        mt = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        if mt:
            title = html_lib.unescape(mt.group(1)).strip()
    body = re.sub(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""",
                  lambda m: m.group(1) + m.group(2) + h_esc(resolve(html_lib.unescape(m.group(3)))) + m.group(2),
                  body, flags=re.S | re.I)
    return title, body.strip()


def load_posts():
    if not os.path.isdir(BLOG):
        return []
    files = []
    for dp, dn, fn in os.walk(BLOG):
        dn[:] = sorted(d for d in dn if not d.startswith((".", "_")))
        for f in sorted(fn):
            if f.lower().endswith(POST_EXTS) and not f.startswith((".", "_")):
                files.append(os.path.join(dp, f))
    posts, seen = [], set()
    for path in files:
        stem = os.path.splitext(os.path.basename(path))[0]
        parent = os.path.dirname(path)
        name = os.path.basename(parent) if parent != BLOG and stem.lower() in ("index", "post", "article", "readme") else stem
        m = re.match(r"^(\d{4}-\d{2}-\d{2})[-_ ]+(.+)$", name)
        file_date, name = (m.group(1), m.group(2)) if m else (None, name)
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                raw = fh.read()
        except OSError as e:
            print("Could not read %s: %s" % (path, e))
            continue
        is_html = path.lower().endswith((".html", ".htm"))
        meta, raw = ({}, raw) if is_html else md_front_matter(raw)
        if meta.get("draft", "").lower() in ("yes", "true", "1", "y"):
            continue
        slug = slugify(meta.get("slug") or name)
        base_slug, k = slug, 2
        while slug in seen:
            slug = "%s-%d" % (base_slug, k)
            k += 1
        seen.add(slug)
        resolve = make_resolver(slug, parent)
        if is_html:
            title, body = html_post(raw, resolve)
        else:
            lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            k = 0
            while k < len(lines) and not lines[k].strip():
                k += 1
            title = None
            mh = re.match(r"^#\s+(.*?)(?:\s+#+)?\s*$", lines[k]) if k < len(lines) else None
            if mh:
                title = re.sub(r"[*_`]", "", mh.group(1)).strip()
                lines = lines[k + 1:]
            body = md_blocks(lines, resolve)
        title = meta.get("title") or title or display(name)
        date = (meta.get("date") or "")[:10]
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = file_date or datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        excerpt = ""
        mp = re.search(r"<p>(.*?)</p>", body, re.S)
        if mp:
            t = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", mp.group(1)))).strip()
            excerpt = t if len(t) <= 200 else t[:200].rsplit(" ", 1)[0] + "…"
        mc = re.search(r'<img[^>]*\bsrc="([^"]+)"', body)
        posts.append({"slug": slug, "title": title, "date": date, "excerpt": excerpt,
                      "cover": html_lib.unescape(mc.group(1)) if mc else "", "html": body})
    posts.sort(key=lambda p: (p["date"], p["title"]), reverse=True)
    return posts


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

    posts = load_posts()

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("/* Generated by build.py from the images folder. Do not edit; run build.py again. */\n")
        fh.write("/* lite build: double-click opens the web copy, not the original */\n" if LITE else "")
        fh.write("var TREE = " + json.dumps(tree, indent=1, ensure_ascii=False) + ";\n")
        fh.write("var PHOTOS = " + json.dumps(photos, indent=1, ensure_ascii=False) + ";\n")
        fh.write("var LINKS = " + json.dumps(load_links(), indent=1, ensure_ascii=False) + ";\n")
        fh.write("var POSTS = " + json.dumps(posts, indent=1, ensure_ascii=False) + ";\n")

    stale = [r["file"] for r in rows if r.get("file") and r["file"] not in used]
    albums = sum(len(g["albums"]) for m in tree for g in m["groups"])
    print("Built data.js: %d photos in %d albums." % (len(photos), albums))
    if posts:
        print("%d blog post(s)." % len(posts))
    if stale:
        print("%d row(s) in settings.csv have no matching photo (moved or deleted); delete them if you like." % len(stale))


def found_keys(found):
    return [(m, a, s, os.path.relpath(p, IMG).replace(os.sep, "/")) for m, a, s, p in found]


if __name__ == "__main__":
    main()
