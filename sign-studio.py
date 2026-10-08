#!/usr/bin/env python3
"""Sign Studio -- Qt LED destination editor with pixel touch-up.

A PySide6 rewrite of the sign-designer workflow: message list (route +
destination + via), bitmap or system fonts, the four via layouts,
colour dots, and a zoomable canvas with an LED-dot simulation plus
pixel-grid / field-box overlays. Output is the same crisp 240x40 PNG
(bitmap pixels only, no antialiasing) for programs/*.json.

The pixel tool (paint / erase, 1-3px brush) lets you hand-fix glyphs:
touch-ups sit on top of the rendered text, travel with the message,
and are baked into the saved PNG.

The BDF editor edits bitmap fonts directly: pick a fonts/*.bdf file,
paint glyph pixels, tweak advance/BBX metrics, add/delete glyphs,
then Save font (Save As… for a copy). The sign canvas re-renders
with the edited font straight away.

The Program editor manages programs/*.json natively: programs,
destinations, screens (reorder, per-screen seconds/fit/colour),
an image browser over bitmap/, file defaults, and Show on screen.

  python3 sign-studio.py
  QT_QPA_PLATFORM=offscreen python3 sign-studio.py --smoke  # self-test

Needs: pip install PySide6   (Pillow too, for system fonts)
Messages live in sign-messages.json, shared with sign-designer.py.
"""

import argparse
import json
import os
import re
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, os.path.join(THIS_DIR, "bus"))  # program.py moved in
MSG_FILE = os.path.join(THIS_DIR, "sign-messages.json")

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import (QBrush, QColor, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QCompleter, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QRadioButton, QScrollArea,
                               QSpinBox, QSplitter, QStackedWidget,
                               QStatusBar, QTabWidget, QToolBar,
                               QVBoxLayout, QWidget)

import sysfonts
import program as programs_model
import engine as ENG
W, H = ENG.W, ENG.H
STYLES = ("top", "bottom", "left", "right")
STYLE_TAG = {
    "top": "top via - via over dest",
    "bottom": "bottom via - dest over via",
    "left": "left via - via | dest side by side",
    "right": "right via - dest | via side by side",
}
COLOURS = [
    ("white", "#ffffff"),
    ("amber", "#ff8c00"),
    ("red", "#ff2828"),
    ("green", "#28ff5a"),
    ("blue", "#3c8cff"),
]
CELL_COLOURS = {"route": "#ffe14d", "dest": "#4dd2ff",
                "via": "#ff4dd2"}
SEED = [
    {"name": "43 Sheffield", "route": "43", "destination": "Sheffield",
     "via": "Dronfield, Chesterfield", "style": "top",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
    {"name": "X12 Burton", "route": "X12", "destination": "Burton",
     "via": "Lichfield", "style": "bottom",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
    {"name": "Not In Service", "route": "", "destination": "Not In Service",
     "via": "", "style": "top",
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf", "route_scale": 2, "dest_scale": 1,
     "via_scale": 1},
]


def load_messages():
    try:
        with open(MSG_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [m for m in data if isinstance(m, dict)]
    except (OSError, ValueError):
        pass
    return [dict(m) for m in SEED]


def save_messages(msgs):
    tmp = MSG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(msgs, f, indent=2)
        f.write("\n")
    os.replace(tmp, MSG_FILE)


def touch_of(msg):
    """(add_set, del_set) of (x, y) touch-ups stored on a message."""
    t = msg.get("touch") if isinstance(msg.get("touch"), dict) else {}
    try:
        add = {(int(x), int(y)) for x, y in t.get("add", [])}
        dele = {(int(x), int(y)) for x, y in t.get("del", [])}
    except (TypeError, ValueError):
        add, dele = set(), set()
    return ({p for p in add if 0 <= p[0] < W and 0 <= p[1] < H},
            {p for p in dele if 0 <= p[0] < W and 0 <= p[1] < H})


# ---------------------------------------------------------------------------
# BDF font editing (bitmap glyphs, round-trip .bdf files)
# ---------------------------------------------------------------------------

class BdfGlyph:
    """One editable glyph: metrics + w-bit rows (row 0 is the top)."""

    def __init__(self, name, enc, swidth, dwidth, bbx, rows):
        self.name = str(name or "")
        self.enc = int(enc)
        self.swidth = tuple(swidth) if len(swidth) == 2 else (480, 0)
        self.dwidth = tuple(dwidth) if len(dwidth) == 2 else (8, 0)
        w, h, xo, yo = bbx
        self.w, self.h, self.xoff, self.yoff = w, h, xo, yo
        mask = (1 << w) - 1 if w > 0 else 0
        self.rows = [(r & mask) for r in list(rows[:h])]
        while len(self.rows) < max(0, h):
            self.rows.append(0)

    def pixel(self, x, y):
        if 0 <= x < self.w and 0 <= y < self.h:
            return bool((self.rows[y] >> (self.w - 1 - x)) & 1)
        return False

    def set_pixel(self, x, y, on):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return False
        bit = 1 << (self.w - 1 - x)
        before = self.rows[y]
        self.rows[y] = (before | bit) if on else (before & ~bit)
        return self.rows[y] != before

    def clear(self):
        self.rows = [0] * self.h

    def fill(self):
        mask = (1 << self.w) - 1 if self.w else 0
        self.rows = [mask] * self.h

    def invert(self):
        mask = (1 << self.w) - 1 if self.w else 0
        self.rows = [(r ^ mask) for r in self.rows]

    def lit_count(self):
        return sum(bin(r).count("1") for r in self.rows)

    def resize(self, w, h):
        """Resize bitmap, anchoring existing pixels top-left."""
        w = max(1, min(64, int(w)))
        h = max(1, min(64, int(h)))
        new_rows = []
        for y in range(h):
            if y < len(self.rows) and w == self.w:
                new_rows.append(self.rows[y])
            elif y < len(self.rows):
                # re-align left: old MSB-first w bits -> new w bits
                old = self.rows[y]
                if w > self.w:
                    new_rows.append(old << (w - self.w))
                else:
                    new_rows.append((old >> (self.w - w)) & (
                        (1 << w) - 1))
            else:
                new_rows.append(0)
        self.w, self.h, self.rows = w, h, new_rows

    def row_hex(self):
        nbytes = (self.w + 7) // 8 or 1
        shift = nbytes * 8 - self.w
        return [f"{(r << shift) & ((1 << nbytes * 8) - 1):0{nbytes * 2}X}"
                for r in self.rows]


class BdfDoc:
    """Parsed .bdf file preserving header order + glyph order."""

    def __init__(self, path, header, glyphs, footer="ENDFONT"):
        self.path = path
        self.header = list(header)  # lines before first STARTCHAR,
        # with the CHARS line last (count rewritten on save)
        self.glyphs = list(glyphs)
        self.footer = footer
        self.dirty = set()  # encodings touched since load/save

    @property
    def filename(self):
        return os.path.basename(self.path)

    def find(self, enc):
        for g in self.glyphs:
            if g.enc == enc:
                return g
        return None

    @staticmethod
    def _ints(words, n, fallback=0):
        out = []
        for i in range(n):
            try:
                out.append(int(words[i]))
            except (IndexError, TypeError, ValueError):
                out.append(fallback)
        return out

    @classmethod
    def load(cls, path):
        with open(path, errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f]
        try:
            first = next(i for i, l in enumerate(lines)
                         if l.startswith("STARTCHAR"))
        except StopIteration:
            raise ValueError(f"{os.path.basename(path)}: no glyphs found")
        header = lines[:first]
        glyphs = []
        i = first
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("STARTCHAR"):
                name = line[len("STARTCHAR"):].strip() or f"char{len(glyphs)}"
                enc, sw, dw, bbx = -1, (480, 0), (8, 0), (8, 8, 0, 0)
                extra = []
                i += 1
                while i < len(lines):
                    parts = lines[i].strip().split()
                    if not parts:
                        i += 1
                        continue
                    tag = parts[0]
                    if tag == "ENCODING":
                        try:
                            enc = int(parts[1])
                        except (IndexError, ValueError):
                            enc = -1
                    elif tag == "SWIDTH":
                        sw = tuple(cls._ints(parts[1:], 2))
                    elif tag == "DWIDTH":
                        dw = tuple(cls._ints(parts[1:], 2))
                    elif tag == "BBX":
                        bbx = tuple(cls._ints(parts[1:], 4))
                    elif tag == "BITMAP":
                        i += 1
                        break
                    elif tag in ("ENDCHAR", "ENDFONT"):
                        break
                    else:
                        extra.append(lines[i])
                    i += 1
                rows = []
                w, h = max(0, bbx[0]), max(0, bbx[1])
                while i < len(lines):
                    s = lines[i].strip()
                    if s in ("ENDCHAR", "ENDFONT") or \
                            s.startswith("STARTCHAR"):
                        break
                    if s:
                        try:
                            val = int(s, 16)
                        except ValueError:
                            val = 0
                        total = len(s) * 4
                        if total >= w and w > 0:
                            val >>= total - w
                        elif w > 0:
                            val <<= w - total
                        rows.append(val & ((1 << w) - 1) if w else 0)
                    i += 1
                while len(rows) < h:
                    rows.append(0)
                if enc < 0:
                    enc = -(len(glyphs) + 1)  # unencoded: stable slot
                glyphs.append(BdfGlyph(name, enc, sw, dw, bbx,
                                       rows[:h] if h else []))
                # keep unrecognised pre-bitmap lines out of the way:
                # they are dropped on save (only the standard tags
                # the engine reads are kept)
                if i < len(lines) and lines[i].strip() == "ENDCHAR":
                    i += 1
            elif line == "ENDFONT":
                i += 1
                break
            else:
                i += 1
        # normalise header: drop any stale CHARS line, re-added on save
        header = [l for l in header
                  if not l.startswith("CHARS")]
        return cls(path, header, glyphs)

    def _grow_bbox(self):
        """Grow FONTBOUNDINGBOX in the header so edited glyphs fit."""
        mw = max([g.w for g in self.glyphs] + [0])
        mh = max([g.h for g in self.glyphs] + [0])
        for i, line in enumerate(self.header):
            if line.startswith("FONTBOUNDINGBOX"):
                try:
                    _w, _h, xo, yo = self._ints(line.split()[1:], 4)
                except ValueError:
                    _w, _h, xo, yo = mw, mh, 0, 0
                self.header[i] = (f"FONTBOUNDINGBOX {max(_w, mw)} "
                                  f"{max(_h, mh)} {xo} {yo}")
                return

    def save(self, path=None):
        dest = path or self.path
        self._grow_bbox()
        lines = list(self.header) + [f"CHARS {len(self.glyphs)}"]
        for g in self.glyphs:
            lines.append(f"STARTCHAR {g.name or f'char{g.enc}'}")
            lines.append(f"ENCODING {g.enc}")
            lines.append(f"SWIDTH {g.swidth[0]} {g.swidth[1]}")
            lines.append(f"DWIDTH {g.dwidth[0]} {g.dwidth[1]}")
            lines.append(f"BBX {g.w} {g.h} {g.xoff} {g.yoff}")
            lines.append("BITMAP")
            lines.extend(g.row_hex())
            lines.append("ENDCHAR")
        lines.append(self.footer or "ENDFONT")
        tmp = dest + ".tmp"
        with open(tmp, "w", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, dest)
        self.path = dest
        self.dirty.clear()
        # drop the engine cache so the sign canvas picks up the edit
        try:
            ENG._FONTS.pop(os.path.basename(dest), None)
        except AttributeError:
            pass


def slug(s, fallback="untitled"):
    return (re.sub(r"[^a-z0-9]+", "-",
                   str(s or "").strip().lower()).strip("-")
            or fallback)


def bitmap_base_for_program_file(path):
    """Bitmap base dir (repo-root-relative) for a program file.

    Program files under bus/programs/ keep their PNGs under
    bus/bitmap/; everything else uses the root bitmap/.
    """
    full = path if os.path.isabs(path) else os.path.normpath(
        os.path.join(THIS_DIR, path))
    if full.startswith(os.path.join(THIS_DIR, "bus", "programs")
                       + os.sep):
        return os.path.join("bus", "bitmap")
    return "bitmap"


def page_paths(program, name_route, name_dest, n, bitmap_base="bitmap"):
    """Repo-relative PNG paths for n pages, 1-based and stable:

      <bitmap_base>/destinations/<program>/<route>/<route>-<dest>-<page>.png

    e.g. bitmap/destinations/401/401/401-burton-1.png
    """
    p = slug(program, "custom")
    r = slug(name_route, "noroute")
    d = slug(name_dest, "untitled")
    base = f"{r}-{d}" if r != d else r
    return [os.path.join(bitmap_base, "destinations", p, r,
                          f"{base}-{i}.png").replace(os.sep, "/")
            for i in range(1, n + 1)]


def page_job(page, fg_hex):
    """Engine job dict from a page snapshot (fields + rules)."""
    dest = page.get("destination", page.get("dest", ""))
    return {
        "route": page.get("route", ""), "dest": dest,
        "via": page.get("via", ""), "style": page.get("style", "top"),
        "route_font": page.get("route_font", "10x20.bdf"),
        "dest_font": page.get("dest_font", "10x20.bdf"),
        "via_font": page.get("via_font", "6x13B.bdf"),
        "route_scale": page.get("route_scale", 2),
        "dest_scale": page.get("dest_scale", 1),
        "via_scale": page.get("via_scale", 1),
        "fg": fg_hex,
        "upper_dest": page.get("upper_dest", False),
        "via_prefix": page.get("via_prefix", False),
        "offsets": _norm_offsets(page),
    }


def page_spec(page):
    """(path, index, px) per field for a page snapshot (system fonts)."""
    sysm = page.get("sys") if isinstance(page.get("sys"), dict) else {}
    out = {}
    for name, default_px in (("route", 34), ("dest", 20), ("via", 12)):
        s = sysm.get(name, {}) if isinstance(sysm.get(name), dict) \
            else {}
        fam = str(s.get("family", "") or "")
        try:
            px = max(6, min(120, int(s.get("px", default_px))))
        except (TypeError, ValueError):
            px = default_px
        bold = bool(s.get("bold", False))
        out[name] = (*sysfonts.resolve_face(fam, bold), px)
    return out


def page_is_empty(page):
    for k in ("route", "destination", "dest", "via"):
        if str(page.get(k, "") or "").strip():
            return False
    t = page.get("touch")
    return not (isinstance(t, dict) and (t.get("add") or t.get("del")))


def _clamp_nudge(v):
    try:
        return max(-80, min(80, int(v or 0)))
    except (TypeError, ValueError):
        return 0


def _norm_offsets(p):
    """Per-field move offsets, always complete.

    Understands the old global dx/dy keys (applied to every field)
    and the current per-field form.
    """
    out = {}
    legacy = (_clamp_nudge(p.get("dx", 0)), _clamp_nudge(p.get("dy", 0)))
    has_legacy = "dx" in p or "dy" in p
    raw = p.get("offsets")
    raw = raw if isinstance(raw, dict) else {}
    for name in ("route", "dest", "via"):
        v = raw.get(name, None)
        try:
            if v is None:
                raise ValueError
            x, y = int(v[0]), int(v[1])
        except (TypeError, ValueError, IndexError):
            x, y = legacy if has_legacy else (0, 0)
        out[name] = [_clamp_nudge(x), _clamp_nudge(y)]
    return out


def _touch_lists(t):
    """Touch-ups as sorted [[x, y]] lists (JSON-safe)."""
    add, dele = touch_of({"touch": t})
    return {"add": sorted(add), "del": sorted(dele)}


def normalize_page(p):
    """Fill defaults so every page has the full editor state."""
    sysm = p.get("sys") if isinstance(p.get("sys"), dict) else {}
    sysn = {}
    for name, default_px in (("route", 34), ("dest", 20), ("via", 12)):
        s = sysm.get(name, {}) if isinstance(sysm.get(name), dict) \
            else {}
        try:
            px = max(6, min(120, int(s.get("px", default_px))))
        except (TypeError, ValueError):
            px = default_px
        sysn[name] = {"family": str(s.get("family", "") or ""),
                      "px": px, "bold": bool(s.get("bold", False))}
    try:
        secs = float(p.get("seconds", 0) or 0)
    except (TypeError, ValueError):
        secs = 0
    return {
        "route": str(p.get("route", "") or ""),
        "destination": str(p.get("destination", p.get("dest", ""))
                           or ""),
        "via": str(p.get("via", "") or ""),
        "style": p.get("style", "top") if p.get("style") in STYLES
        else "top",
        "fsrc": p.get("fsrc", "bdf"),
        "route_font": str(p.get("route_font", "10x20.bdf") or ""),
        "dest_font": str(p.get("dest_font", "10x20.bdf") or ""),
        "via_font": str(p.get("via_font", "6x13B.bdf") or ""),
        "route_scale": p.get("route_scale", 2),
        "dest_scale": p.get("dest_scale", 1),
        "via_scale": p.get("via_scale", 1),
        "upper_dest": p.get("upper_dest", False),
        "via_prefix": p.get("via_prefix", False),
        "invert": bool(p.get("invert", False)),
        "offsets": _norm_offsets(p),
        "sys": sysn,
        "touch": _touch_lists(p.get("touch")),
        "seconds": secs if secs > 0 else 0,
    }


def page_from_flat(m):
    """Legacy flat message (tk format) -> single-page snapshot."""
    p = {"route": m.get("route", ""),
         "destination": m.get("destination", m.get("dest", "")),
         "via": m.get("via", ""), "style": m.get("style", "top"),
         "fsrc": m.get("fsrc", "bdf"),
         "route_font": m.get("route_font", "10x20.bdf"),
         "dest_font": m.get("dest_font", "10x20.bdf"),
         "via_font": m.get("via_font", "6x13B.bdf"),
         "route_scale": m.get("route_scale", 2),
         "dest_scale": m.get("dest_scale", 1),
         "via_scale": m.get("via_scale", 1),
         "sys": m.get("sys") if isinstance(m.get("sys"), dict) else {},
         "touch": m.get("touch") if isinstance(m.get("touch"), dict)
         else {"add": [], "del": []}}
    return normalize_page(p)


def pages_from_message(m):
    pages = m.get("pages")
    if isinstance(pages, list) and pages and all(
            isinstance(p, dict) for p in pages):
        return [normalize_page(dict(p)) for p in pages]
    return [page_from_flat(m)]


def program_files():
    """Repo-relative programs/*.json paths, sorted (root + bus/)."""
    out = []
    for sub in ("programs", os.path.join("bus", "programs")):
        base = os.path.join(THIS_DIR, sub)
        try:
            names = sorted(f for f in os.listdir(base)
                           if f.lower().endswith(".json"))
        except OSError:
            continue
        out.extend([os.path.join(sub, f) for f in names])
    return sorted(out)


def program_index(path):
    """{program: {'route': r, 'destinations': [names]}} for a file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    progs = data.get("programs") if isinstance(data, dict) else None
    if not isinstance(progs, dict):
        return {}
    for name, raw in progs.items():
        if not isinstance(raw, dict):
            continue
        dests = raw.get("destinations", [])
        if isinstance(dests, dict):
            names = list(dests)
        elif isinstance(dests, str):
            names = [dests]
        elif isinstance(dests, list):
            names = [str(d) for d in dests if str(d).strip()]
        else:
            names = []
        out[str(name)] = {"route": str(raw.get("route", "") or ""),
                          "destinations": names}
    return out


def _screen_image(entry):
    return entry.get("image") if isinstance(entry, dict) else entry


def send_pages_to_program(pages, fg_hex, invert, name_route, name_dest,
                          routing):
    """Render pages and append them to a programs file.

    pages: page snapshots (fields + touch + seconds + per-page
    invert). routing: {"file": programs/*.json (repo-relative or
    absolute), "program": name, "destination": name, "route": route
    for a new program}. The `invert` fallback applies only to pages
    without their own invert flag. Blank pages are skipped;
    already-listed images are not duplicated.
    Returns a summary dict. Raises ValueError with a plain message;
    the JSON file is only replaced once the result validates (a
    backup is restored otherwise).
    """
    if not pages:
        raise ValueError("no pages to send -- add a page first")
    rel = str(routing.get("file", "") or "")
    full = rel if os.path.isabs(rel) else os.path.normpath(
        os.path.join(THIS_DIR, rel))
    if not full.lower().endswith(".json"):
        raise ValueError("program file must be a *.json file")
    if os.path.isfile(full):
        try:
            with open(full) as f:
                original = f.read()
            data = json.loads(original)
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read {rel or full}: {e}")
        if not isinstance(data, dict):
            raise ValueError(f"{rel or full}: top level must be an "
                             f"object")
        try:
            programs_model.load_programs_file(full)
        except SystemExit as e:
            raise ValueError(f"{rel or full} is already broken: {e}")
    else:
        # brand-new program file in house style (midlandclassic
        # defaults: amber blinds). New files must live under
        # programs/ or bus/programs/ so a typo can't spray files
        # around the repo.
        roots = (os.path.join(THIS_DIR, "programs") + os.sep,
                 os.path.join(THIS_DIR, "bus", "programs") + os.sep)
        if not full.startswith(roots) or os.sep in os.path.basename(
                full):
            raise ValueError("new program files must live under "
                              "programs/ or bus/programs/, e.g. "
                              "bus/programs/mine.json")
        if not os.path.isdir(os.path.dirname(full)):
            raise ValueError(f"folder does not exist: "
                             f"{os.path.dirname(full)}")
        original = None
        data = {"rotate_seconds": 5, "image_fit": "fit",
                "colour": "#ff8000", "programs": {}}
    pname = str(routing.get("program", "") or "").strip()
    dname = str(routing.get("destination", "") or "").strip()
    if not pname:
        raise ValueError("name a program (route)")
    if not dname:
        raise ValueError("name a destination")

    relpaths = page_paths(pname, name_route, name_dest, len(pages),
                          bitmap_base_for_program_file(full))
    fg = ENG.parse_colour(fg_hex)
    # phase 1: prepare every page (spec resolution fails here, before
    # any PNG is written)
    planned = []  # (page, job, spec-or-None, png_full_path, secs)
    skipped = 0
    for i, page in enumerate(pages):
        if page_is_empty(page):
            skipped += 1
            continue
        job = page_job(page, fg_hex)
        spec = None
        if str(page.get("fsrc", "bdf")) == "sys" and sysfonts.PIL_OK:
            spec = page_spec(page)  # raises ValueError without fonts
        try:
            secs = float(page.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            secs = 0
        planned.append((page, job, spec,
                        os.path.join(THIS_DIR, relpaths[i]),
                        secs if secs > 0 else None))
    if not planned:
        raise ValueError("every page is blank -- nothing sent")
    # phase 2: render + write PNGs
    rendered = []  # (repo-rel-posix-path, seconds-or-None)
    for full_png in {p[3] for p in planned}:
        os.makedirs(os.path.dirname(full_png), exist_ok=True)
    for page, job, spec, full_png, secs in planned:
        if spec is not None:
            frame, _info = sysfonts.render_system(ENG, job, spec)
        else:
            frame, _info = ENG.render(job)
        pinv = page.get("invert", None)
        if pinv is None:
            pinv = invert
        if pinv:
            out = bytearray(len(frame))
            for j in range(0, len(frame), 3):
                if not (frame[j] or frame[j + 1] or frame[j + 2]):
                    out[j], out[j + 1], out[j + 2] = fg
            frame = out
        # touch-ups bake in
        t = page.get("touch") if isinstance(page.get("touch"), dict) \
            else {}
        for x, y in t.get("add", []) or []:
            try:
                o = (int(y) * W + int(x)) * 3
                frame[o], frame[o + 1], frame[o + 2] = fg
            except (TypeError, ValueError, IndexError):
                continue
        for x, y in t.get("del", []) or []:
            try:
                o = (int(y) * W + int(x)) * 3
                frame[o], frame[o + 1], frame[o + 2] = 0, 0, 0
            except (TypeError, ValueError, IndexError):
                continue
        tmp = full_png + ".tmp"
        with open(tmp, "wb") as f:
            f.write(ENG.encode_png(frame))
        os.replace(tmp, full_png)
        try:
            secs = float(page.get("seconds", 0) or 0)
        except (TypeError, ValueError):
            secs = 0
        rendered.append((os.path.relpath(full_png, THIS_DIR).replace(
            os.sep, "/"), secs if secs > 0 else None))
    if not rendered:
        raise ValueError("every page is blank -- nothing sent")

    progs = data.get("programs")
    if not isinstance(progs, dict):
        progs = data["programs"] = {}
    prog = progs.get(pname)
    if not isinstance(prog, dict):
        prog = {"route": str(routing.get("route", "") or "").strip()
                or name_route.strip() or pname,
                "destinations": {}}
        progs[pname] = prog
    dests = prog.get("destinations")
    if isinstance(dests, str):
        dests = prog["destinations"] = [dests]
    if isinstance(dests, dict):
        entry = dests.get(dname)
        if isinstance(entry, dict):
            key = "images" if "images" in entry else (
                "screens" if "screens" in entry else "images")
            lst = entry.get(key)
            if not isinstance(lst, list):
                lst = entry[key] = []
        elif isinstance(entry, list):
            lst = entry
        else:
            lst = dests[dname] = []
        existing = [_screen_image(s) for s in lst]
        added = 0
        for relp, secs in rendered:
            if relp in existing:
                continue
            lst.append({"image": relp, "seconds": secs} if secs
                       else relp)
            added += 1
    elif isinstance(dests, list):
        if dname not in [str(d) for d in dests]:
            dests.append(dname)
        screens = prog.get("screens")
        if not isinstance(screens, list):
            screens = prog["screens"] = []
        existing = [_screen_image(s) for s in screens]
        added = 0
        for relp, secs in rendered:
            if relp in existing:
                continue
            screens.append({"image": relp, "seconds": secs} if secs
                           else relp)
            added += 1
    else:
        raise ValueError(f"program '{pname}' has an odd 'destinations' "
                         f"shape -- edit it in the program editor first")
    try:
        tmp = full + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, full)
        programs_model.load_programs_file(full)
    except SystemExit as e:
        if original is None:
            try:
                os.remove(full)  # our own half-written new file: remove
            except OSError:
                pass
        else:
            with open(full, "w") as f:
                f.write(original)
        raise ValueError(f"write failed validation ({e}) -- restored")
    except OSError as e:
        raise ValueError(str(e))
    return {"paths": [p for p, _ in rendered], "added": added,
            "skipped": skipped, "file": rel or full,
            "program": pname, "destination": dname,
            "created_file": original is None}


class SendDialog(QDialog):
    """Pick where pages land: program file > program > destination."""

    def __init__(self, parent, routing, message_route, message_dest,
                 pages, fg_hex):
        super().__init__(parent)
        self.setWindowTitle("Send pages to program")
        self._pages = pages
        self._fg_hex = fg_hex
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.c_file = QComboBox()
        self.c_file.setEditable(True)
        self.c_file.setInsertPolicy(QComboBox.NoInsert)
        self.c_file.lineEdit().setPlaceholderText(
            "programs/mine.json — type a new path to create it")
        files = program_files()
        self.c_file.addItems(files)
        if routing.get("file"):
            # editable: an existing file or a new programs/*.json path
            self.c_file.setCurrentText(routing["file"])
        self.c_file.currentIndexChanged.connect(self._file_changed)
        self.c_file.lineEdit().editingFinished.connect(self._file_changed)
        form.addRow("Program file", self.c_file)
        self.c_prog = QComboBox()
        self.c_prog.setEditable(True)
        self.c_prog.setInsertPolicy(QComboBox.NoInsert)
        self.c_prog.currentTextChanged.connect(self._prog_changed)
        form.addRow("Program", self.c_prog)
        self.e_route = QLineEdit()
        self.e_route.textChanged.connect(self._update_summary)
        form.addRow("Route (new program)", self.e_route)
        self.c_dest = QComboBox()
        self.c_dest.setEditable(True)
        self.c_dest.setInsertPolicy(QComboBox.NoInsert)
        self.c_dest.currentTextChanged.connect(self._update_summary)
        form.addRow("Destination", self.c_dest)
        lay.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        lay.addWidget(QLabel(
            "Saves one PNG per page into bitmap/destinations/program/ "
            "route/ and appends them to the destination (new programs "
            "and destinations are created as needed). Global colour "
            "applies to every page; invert is per page; blank pages "
            "are skipped; images already listed are not duplicated."))
        btns = QDialogButtonBox(QDialogButtonBox.Ok
                                | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Send")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._index = {}
        self._file_changed()
        # prefill (after _file_changed so combos exist)
        if routing.get("program"):
            self.c_prog.setCurrentText(routing["program"])
        elif message_route:
            self.c_prog.setCurrentText(message_route)
        if routing.get("destination"):
            self.c_dest.setCurrentText(routing["destination"])
        elif message_dest:
            self.c_dest.setCurrentText(message_dest)
        if routing.get("route"):
            self.e_route.setText(routing["route"])
        elif message_route:
            self.e_route.setText(message_route)
        self._update_summary()

    def _full(self):
        rel = self.c_file.currentText().strip()
        return rel if os.path.isabs(rel) else os.path.normpath(
            os.path.join(THIS_DIR, rel))

    def _file_changed(self):
        self._index = program_index(self._full())
        cur = self.c_prog.currentText()
        self.c_prog.blockSignals(True)
        try:
            self.c_prog.clear()
            self.c_prog.addItems(sorted(self._index))
        finally:
            self.c_prog.blockSignals(False)
        if cur:
            self.c_prog.setCurrentText(cur)
        self._prog_changed()

    def _prog_changed(self):
        prog = self.c_prog.currentText().strip()
        info = self._index.get(prog, {})
        if info.get("route") and not self.e_route.text().strip():
            self.e_route.setText(info["route"])
        cur = self.c_dest.currentText()
        self.c_dest.blockSignals(True)
        try:
            self.c_dest.clear()
            self.c_dest.addItems(info.get("destinations", []))
        finally:
            self.c_dest.blockSignals(False)
        if cur:
            self.c_dest.setCurrentText(cur)
        self._update_summary()

    def _is_new_file(self):
        full = self._full()
        return bool(full) and not os.path.isfile(full)

    def _update_summary(self):
        n = len(self._pages)
        paths = page_paths(self.c_prog.currentText(),
                           self.e_route.text(),
                           self.c_dest.currentText(), n,
                           bitmap_base_for_program_file(
                               self.c_file.currentText()))
        secs = []
        for p in self._pages:
            try:
                s = float(p.get("seconds", 0) or 0)
            except (TypeError, ValueError):
                s = 0
            secs.append(f"{s:g}s" if s > 0 else "file default")
        new = " (new file — will be created)" if self._is_new_file() \
            else ""
        self.summary.setText(
            f"{n} page(s) → {self.c_file.currentText() or '?'} › "
            f"{self.c_prog.currentText() or '?'} › "
            f"{self.c_dest.currentText() or '?'}{new}\n"
            + ", ".join(f"{f} ({s})"
                        for f, s in zip(paths, secs)))

    def routing(self):
        return {"file": self.c_file.currentText().strip(),
                "program": self.c_prog.currentText().strip(),
                "destination": self.c_dest.currentText().strip(),
                "route": self.e_route.text().strip()}


CONTROL_FILE = os.path.join(THIS_DIR, "program_control.json")


class PixelCanvas(QLabel):
    """Zoomed 240x40 canvas: left button paints, right button erases,
    drag strokes interpolate so fast moves leave no gaps. Arrow keys
    nudge the whole blind 1px when the canvas has focus (click it)."""

    def __init__(self, studio):
        super().__init__()
        self.studio = studio
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._last = None

    def pixel_from_pos(self, pos):
        z = self.studio.zoom()
        x, y = int(pos.x() // z), int(pos.y() // z)
        if 0 <= x < W and 0 <= y < H:
            return (x, y)
        return None

    def mousePressEvent(self, event):
        p = self.pixel_from_pos(event.position().toPoint())
        if p is None:
            return
        erase = (event.button() == Qt.RightButton or
                 self.studio.tool() == "erase")
        self._last = p
        self.studio.paint_stroke([p], erase)

    def mouseMoveEvent(self, event):
        p = self.pixel_from_pos(event.position().toPoint())
        self.studio.hover_pixel(p)
        if p is None or self._last is None:
            if p is None:
                self._last = None
            return
        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            erase = (bool(event.buttons() & Qt.RightButton) or
                     self.studio.tool() == "erase")
            self.studio.paint_stroke(self._line(self._last, p), erase)
            self._last = p

    def mouseReleaseEvent(self, _event):
        self._last = None

    def leaveEvent(self, _event):
        self._last = None
        self.studio.hover_pixel(None)

    def keyPressEvent(self, event):
        steps = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0),
                 Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
        if event.key() in steps:
            dx, dy = steps[event.key()]
            self.studio.nudge(dx, dy)
            event.accept()
        else:
            super().keyPressEvent(event)

    @staticmethod
    def _line(a, b):
        """Pixel walk between two grid points (no gaps on fast drags)."""
        x0, y0, x1, y1 = a[0], a[1], b[0], b[1]
        pts, dx, dy = [], abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, x, y = dx - dy, x0, y0
        while True:
            pts.append((x, y))
            if x == x1 and y == y1:
                return pts
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x += sx
            if e2 < dx:
                err += dx
                y += sy


class GlyphCanvas(QLabel):
    """Zoomed glyph editor: left paints, right erases, drag interpolates."""

    def __init__(self, studio):
        super().__init__()
        self.studio = studio
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self._last = None

    def glyph_pixel_from_pos(self, pos):
        g = self.studio.bdf_glyph()
        if g is None:
            return None
        z = self.studio.bdf_zoom()
        pad = z  # 1-cell margin so xoff/yoff guides fit
        x, y = int((pos.x() - pad) // z), int((pos.y() - pad) // z)
        if 0 <= x < g.w and 0 <= y < g.h:
            return (x, y)
        return None

    def mousePressEvent(self, event):
        p = self.glyph_pixel_from_pos(event.position().toPoint())
        if p is None:
            return
        erase = (event.button() == Qt.RightButton or
                 self.studio.tool() == "erase")
        self._last = p
        self.studio.bdf_paint([p], erase)

    def mouseMoveEvent(self, event):
        p = self.glyph_pixel_from_pos(event.position().toPoint())
        self.studio.bdf_hover(p)
        if p is None or self._last is None:
            if p is None:
                self._last = None
            return
        if event.buttons() & (Qt.LeftButton | Qt.RightButton):
            erase = (bool(event.buttons() & Qt.RightButton) or
                     self.studio.tool() == "erase")
            self.studio.bdf_paint(PixelCanvas._line(self._last, p),
                                  erase)
            self._last = p

    def mouseReleaseEvent(self, _event):
        self._last = None

    def leaveEvent(self, _event):
        self._last = None
        self.studio.bdf_hover(None)


class Studio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sign Studio -- 240x40")
        self.fonts = ENG.available_fonts() or ["10x20.bdf"]
        self.messages = load_messages()
        self.fg_hex = "#ffffff"
        self.touch_add = set()
        self.touch_del = set()
        self.base = bytearray(W * H * 3)   # engine render, no touch-ups
        self.frame = bytearray(W * H * 3)  # base + touch-ups (== saved)
        self.info = {"lit": 0, "warnings": [], "fg": "#ffffff",
                     "fields": {}}
        self._last_sig = None
        self._path_touched = False
        self.pages = []
        self.page_idx = 0
        self.program_routing = {}
        self._deb = QTimer(self)
        self._deb.setSingleShot(True)
        self._deb.timeout.connect(self.refresh)
        self._faces_ready = False
        # -- app mode ----------------------------------------------
        self.mode = "sign"  # sign | program | bdf
        # -- BDF editor state ----------------------------------------
        self.bdf_doc = None
        self.bdf_enc = 65
        self.bdf_orig_rows = {}  # enc -> (rows, w, h) for revert
        self._bdf_loading = False
        # -- program editor state ------------------------------------
        self.prog_file = None  # full path of programs/*.json
        self.prog_data = None  # parsed JSON dict being edited
        self.prog_name = None  # selected program
        self.prog_dest = None  # selected destination (None = all)
        self.prog_screen = 0  # selected screen row
        self.prog_dirty = False
        self._prog_loading = False
        self._build()
        self._bdf_init()
        self._prog_init()
        self._reload_list()
        # always have a working page: nudge, touch-ups and flags work
        # straight away, with no message selected
        self.pages = [self._snapshot_page()]
        self.page_idx = 0
        self._rebuild_pages()
        self.refresh()
        self.set_mode("sign")

    # -- layout ------------------------------------------------------
    def _build(self):
        men = self.menuBar()
        filem = men.addMenu("File")
        filem.addAction("Save (this editor)", QKeySequence.StandardKey.Save,
                        self.save_current)
        filem.addAction("Save PNG", self.save_png)
        filem.addAction("Send to program…", self.send_to_program)
        filem.addAction("Show on screen", self.push_to_screen)
        filem.addSeparator()
        filem.addAction("Save font", self.bdf_save)
        filem.addAction("Save program file", self.prog_save)
        filem.addSeparator()
        filem.addAction("Quit", QKeySequence.StandardKey.Quit, self.close)
        viewm = men.addMenu("View")
        self.act_sign = viewm.addAction("Sign editor")
        self.act_sign.setShortcut(QKeySequence("Ctrl+1"))
        self.act_sign.triggered.connect(lambda: self.set_mode("sign"))
        self.act_prog = viewm.addAction("Program editor")
        self.act_prog.setShortcut(QKeySequence("Ctrl+2"))
        self.act_prog.triggered.connect(lambda: self.set_mode("program"))
        self.act_bdf = viewm.addAction("BDF editor")
        self.act_bdf.setShortcut(QKeySequence("Ctrl+3"))
        self.act_bdf.triggered.connect(lambda: self.set_mode("bdf"))
        msgm = men.addMenu("Message")
        msgm.addAction("New from fields", self.msg_new)
        msgm.addAction("Update selected", self.msg_update)
        msgm.addAction("Delete", self.msg_delete)
        helpm = men.addMenu("Help")
        helpm.addAction("About", self.about)

        split = QSplitter()
        self.setCentralWidget(split)
        self.toolbar = QToolBar("main")
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        # -- right side: one panel per mode ------------------------
        self.right_stack = QStackedWidget()
        self.right_stack.setFixedWidth(400)
        self.sign_tabs = QTabWidget()
        self.right_stack.addWidget(self.sign_tabs)      # index 0: sign
        self.prog_side = QWidget()                      # index 1: program
        self.right_stack.addWidget(self.prog_side)
        self.bdf_side = QWidget()                       # index 2: bdf
        self.right_stack.addWidget(self.bdf_side)

        def _scroll_wrap():
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            inner = QWidget()
            lay = QVBoxLayout(inner)
            lay.setContentsMargins(8, 8, 8, 8)
            sc.setWidget(inner)
            return sc, lay

        def _tab(parent, name):
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(8, 8, 8, 8)
            parent.addTab(w, name)
            return lay

        # sign sidebar: 3 tabs instead of 5
        design_tab = _tab(self.sign_tabs, "Design")
        library_tab = _tab(self.sign_tabs, "Library")
        output_tab = _tab(self.sign_tabs, "Output")
        # program + BDF sidebars scroll (they are tall)
        prog_sc, prog_side_lay = _scroll_wrap()
        _ql = QVBoxLayout(self.prog_side)
        _ql.setContentsMargins(0, 0, 0, 0)
        _ql.addWidget(prog_sc)
        bdf_sc, bdf_side_lay = _scroll_wrap()
        _ql2 = QVBoxLayout(self.bdf_side)
        _ql2.setContentsMargins(0, 0, 0, 0)
        _ql2.addWidget(bdf_sc)

        text_box = self._group("Content", design_tab)
        tl = QVBoxLayout(text_box)
        self.e_route = self._field(tl, "Route no.", "43")
        self.e_dest = self._area(tl, "Destination", "Sheffield")
        self.e_via = self._area(tl, "Via", "Dronfield, Chesterfield")
        brow = QHBoxLayout()
        bprev, bdrop = QPushButton("Preview"), QPushButton("Drop")
        bprev.clicked.connect(self.refresh)
        bdrop.clicked.connect(self.drop)
        brow.addWidget(bprev)
        brow.addWidget(bdrop)
        brow.addStretch(1)
        brow.addWidget(QLabel("Sign 240x40"))
        tl.addLayout(brow)

        lay_box = self._group("Layout  (keys 1-4)", design_tab)
        ll = QVBoxLayout(lay_box)
        self.style_btns = QButtonGroup(self)
        self.style_radios = {}
        for i, s in enumerate(STYLES):
            rb = QRadioButton(STYLE_TAG[s])
            rb.setChecked(i == 0)
            rb.clicked.connect(self.refresh)
            self.style_btns.addButton(rb)
            self.style_radios[s] = rb
            ll.addWidget(rb)

        font_box = self._group("Fonts", design_tab)
        fl = QVBoxLayout(font_box)
        srcrow = QHBoxLayout()
        self.rb_bdf = QRadioButton("bitmap BDF")
        self.rb_sys = QRadioButton("system")
        self.rb_bdf.setChecked(True)
        if not sysfonts.PIL_OK:
            self.rb_sys.setEnabled(False)
            self.rb_sys.setToolTip("needs Pillow: pip install pillow")
        self.rb_bdf.clicked.connect(self.refresh)
        self.rb_sys.clicked.connect(self.refresh)
        srcrow.addWidget(self.rb_bdf)
        srcrow.addWidget(self.rb_sys)
        srcrow.addStretch(1)
        self.face_count = QLabel("")
        srcrow.addWidget(self.face_count)
        fl.addLayout(srcrow)
        self.bdf_rows = QWidget()
        bl = QVBoxLayout(self.bdf_rows)
        bl.setContentsMargins(0, 0, 0, 0)
        self.c_rf, self.s_rs = self._bdf_row(bl, "route",
                                              "johnston100-45.bdf", 1)
        self.c_df, self.s_ds = self._bdf_row(bl, "dest",
                                             "johnston100-33.bdf", 1)
        self.c_vf, self.s_vs = self._bdf_row(bl, "via",
                                             "johnston100-20.bdf", 1)
        fl.addWidget(self.bdf_rows)
        self.sys_rows = QWidget()
        yl = QVBoxLayout(self.sys_rows)
        yl.setContentsMargins(0, 0, 0, 0)
        self.c_srf, self.s_srp, self.b_srb = self._sys_row(
            yl, "route", "Arial", 34, True)
        self.c_sfd, self.s_sdp, self.b_sdb = self._sys_row(
            yl, "dest", "Arial", 20, True)
        self.c_sfv, self.s_svp, self.b_svb = self._sys_row(
            yl, "via", "Arial", 12, False)
        self.sys_rows.setVisible(False)
        fl.addWidget(self.sys_rows)

        col_box = self._group("Colour", design_tab)
        cl = QHBoxLayout(col_box)
        self.col_btns = QButtonGroup(self)
        for i, (name, hexv) in enumerate(COLOURS):
            b = QPushButton()
            b.setCheckable(True)
            b.setFixedSize(30, 22)
            b.setToolTip(name)
            b.setStyleSheet(f"background-color: {hexv}; border: 1px "
                            f"solid #888; border-radius: 4px;")
            b.clicked.connect(
                lambda _c=False, h=hexv: self.set_fg(h))
            self.col_btns.addButton(b, i)
            cl.addWidget(b)
        self.col_btns.button(0).setChecked(True)
        self.e_custom = QLineEdit()
        self.e_custom.setPlaceholderText("#rrggbb")
        self.e_custom.setMaximumWidth(80)
        self.e_custom.textChanged.connect(self._custom_live)
        cl.addWidget(self.e_custom)

        opt_box = self._group("Display", design_tab)
        ol = QHBoxLayout(opt_box)
        self.ck_invert = QCheckBox("Invert")
        self.ck_invert.stateChanged.connect(self._invert_toggled)
        ol.addWidget(self.ck_invert)
        ol.addStretch(1)

        nudge_box = self._group("Nudge (1px)", design_tab)
        nl = QHBoxLayout(nudge_box)
        self.c_move = QComboBox()
        self.c_move.addItems(["All", "Route", "Dest", "Via"])
        self.c_move.setToolTip("which text the pad + arrow keys move")
        self.c_move.currentIndexChanged.connect(
            lambda _i: self._update_nudge_label())
        nl.addWidget(self.c_move)
        for arrow, step in (("◀", (-1, 0)), ("▲", (0, -1)),
                           ("▼", (0, 1)), ("▶", (1, 0))):
            b = QPushButton(arrow)
            b.setMaximumWidth(36)
            b.setAutoRepeat(True)
            b.clicked.connect(
                lambda _c=False, d=step: self.nudge(*d))
            nl.addWidget(b)
        self.b_nudge0 = QPushButton("reset (0, 0)")
        self.b_nudge0.clicked.connect(lambda: self.nudge(reset=True))
        nl.addWidget(self.b_nudge0)
        nl.addStretch(1)

        msg_box = self._group("Messages", library_tab)
        ml = QVBoxLayout(msg_box)
        self.listbox = QListWidget()
        self.listbox.setMaximumHeight(90)
        self.listbox.currentRowChanged.connect(self._on_select)
        ml.addWidget(self.listbox)
        mbr = QHBoxLayout()
        for label, fn in (("New", self.msg_new), ("Update", self.msg_update),
                          ("Delete", self.msg_delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            mbr.addWidget(b)
        ml.addLayout(mbr)

        self.pages_box = self._group("Pages (0)", library_tab)
        pl = QVBoxLayout(self.pages_box)
        self.pagelist = QListWidget()
        self.pagelist.setMaximumHeight(70)
        self.pagelist.currentRowChanged.connect(self._select_page)
        pl.addWidget(self.pagelist)
        pbr = QHBoxLayout()
        for label, fn in (("Add", self.page_add),
                          ("Dupe", self.page_dupe),
                          ("Del", self.page_delete)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            pbr.addWidget(b)
        for label, fn in (("▲", lambda: self.page_move(-1)),
                          ("▼", lambda: self.page_move(1))):
            b = QPushButton(label)
            b.setMaximumWidth(34)
            b.clicked.connect(fn)
            pbr.addWidget(b)
        pl.addLayout(pbr)
        secrow = QHBoxLayout()
        secrow.addWidget(QLabel("secs/page"))
        self.s_secs = QSpinBox()
        self.s_secs.setRange(0, 120)
        self.s_secs.setToolTip("dwell per page on the panel "
                               "(0 = file default)")
        self.s_secs.valueChanged.connect(self._secs_changed)
        secrow.addWidget(self.s_secs)
        secrow.addStretch(1)
        pl.addLayout(secrow)

        prog_group = self._group("Send to program", output_tab)
        pgl = QVBoxLayout(prog_group)
        self.routing_label = QLabel("not sent anywhere yet")
        self.routing_label.setWordWrap(True)
        pgl.addWidget(self.routing_label)
        pgl.addWidget(QLabel(
            "1. design pages   2. Send to program   3. Show on screen\n"
            "The matrix follows program_control.json while program.py "
            "is running."))
        prow = QHBoxLayout()
        self.b_send = QPushButton("Send to program…")
        self.b_send.clicked.connect(self.send_to_program)
        prow.addWidget(self.b_send)
        self.b_show = QPushButton("Show on screen")
        self.b_show.clicked.connect(self.push_to_screen)
        prow.addWidget(self.b_show)
        pgl.addLayout(prow)

        save_box = self._group("PNG file", output_tab)
        vl = QVBoxLayout(save_box)
        self.e_path = QLineEdit()
        self.e_path.textEdited.connect(self._mark_path_touched)
        vl.addWidget(self.e_path)
        sr = QHBoxLayout()
        self.b_save = QPushButton("Save PNG")
        self.b_save.clicked.connect(self.save_png)
        sr.addWidget(self.b_save)
        sr.addStretch(1)
        vl.addLayout(sr)

        # -- BDF glyph editor (sidebar lives in BDF mode) --------
        font_group = self._group("Font", bdf_side_lay)
        fgl = QVBoxLayout(font_group)
        frow = QHBoxLayout()
        self.c_bdffont = QComboBox()
        self.c_bdffont.addItems(self.fonts)
        self.c_bdffont.currentTextChanged.connect(self._bdf_font_changed)
        frow.addWidget(self.c_bdffont, 1)
        self.b_bdf_reload = QPushButton("Reload")
        self.b_bdf_reload.setMaximumWidth(60)
        self.b_bdf_reload.clicked.connect(self._bdf_load_selected)
        frow.addWidget(self.b_bdf_reload)
        fgl.addLayout(frow)
        brow2 = QHBoxLayout()
        self.b_bdf_save = QPushButton("Save font")
        self.b_bdf_save.clicked.connect(self.bdf_save)
        brow2.addWidget(self.b_bdf_save)
        self.b_bdf_saveas = QPushButton("Save As…")
        self.b_bdf_saveas.clicked.connect(self.bdf_save_as)
        brow2.addWidget(self.b_bdf_saveas)
        fgl.addLayout(brow2)
        self.l_bdf_file = QLabel("")
        self.l_bdf_file.setWordWrap(True)
        fgl.addWidget(self.l_bdf_file)

        gly_group = self._group("Glyph", bdf_side_lay)
        gl = QVBoxLayout(gly_group)
        self.e_bdf_filter = QLineEdit()
        self.e_bdf_filter.setPlaceholderText("filter e.g. A or 65")
        self.e_bdf_filter.textChanged.connect(self._bdf_rebuild_list)
        gl.addWidget(self.e_bdf_filter)
        self.bdf_list = QListWidget()
        self.bdf_list.setMaximumHeight(110)
        self.bdf_list.currentRowChanged.connect(self._bdf_row_changed)
        gl.addWidget(self.bdf_list)
        nav = QHBoxLayout()
        self.b_bdf_prev = QPushButton("◀ Prev")
        self.b_bdf_prev.clicked.connect(lambda: self._bdf_step(-1))
        nav.addWidget(self.b_bdf_prev)
        self.b_bdf_next = QPushButton("Next ▶")
        self.b_bdf_next.clicked.connect(lambda: self._bdf_step(1))
        nav.addWidget(self.b_bdf_next)
        gl.addLayout(nav)
        self.bdf_canvas = GlyphCanvas(self)
        self.bdf_canvas.setAlignment(Qt.AlignCenter)
        # (hosted on the BDF center stage built below, not here)
        zrow = QHBoxLayout()
        zrow.addWidget(QLabel("zoom"))
        self.s_bdfzoom = QSpinBox()
        self.s_bdfzoom.setRange(4, 24)
        self.s_bdfzoom.setValue(12)
        self.s_bdfzoom.valueChanged.connect(
            lambda _v: self._bdf_draw())
        zrow.addWidget(self.s_bdfzoom)
        self.ck_bdfgrid = QCheckBox("grid")
        self.ck_bdfgrid.setChecked(True)
        self.ck_bdfgrid.stateChanged.connect(
            lambda _s: self._bdf_draw())
        zrow.addWidget(self.ck_bdfgrid)
        zrow.addStretch(1)
        self.l_bdf_coord = QLabel("x -, y -")
        zrow.addWidget(self.l_bdf_coord)
        gl.addLayout(zrow)
        self.l_bdf_status = QLabel("")
        self.l_bdf_status.setWordWrap(True)
        gl.addWidget(self.l_bdf_status)

        met_group = self._group("Metrics", bdf_side_lay)
        mfl = QFormLayout(met_group)
        self.s_bdf_dw = QSpinBox()
        self.s_bdf_dw.setRange(0, 64)
        self.s_bdf_dw.setToolTip("advance width (DWIDTH)")
        mfl.addRow("advance", self.s_bdf_dw)
        self.s_bdf_w = QSpinBox()
        self.s_bdf_w.setRange(1, 64)
        self.s_bdf_h = QSpinBox()
        self.s_bdf_h.setRange(1, 64)
        wh = QHBoxLayout()
        wh.addWidget(self.s_bdf_w)
        wh.addWidget(QLabel("×"))
        wh.addWidget(self.s_bdf_h)
        wh.addStretch(1)
        ww = QWidget()
        ww.setLayout(wh)
        mfl.addRow("BBX w×h", ww)
        self.s_bdf_xo = QSpinBox()
        self.s_bdf_xo.setRange(-32, 32)
        self.s_bdf_yo = QSpinBox()
        self.s_bdf_yo.setRange(-32, 32)
        xy = QHBoxLayout()
        xy.addWidget(self.s_bdf_xo)
        xy.addWidget(QLabel(","))
        xy.addWidget(self.s_bdf_yo)
        xy.addStretch(1)
        xw = QWidget()
        xw.setLayout(xy)
        mfl.addRow("xoff,yoff", xw)
        mrow = QHBoxLayout()
        self.b_bdf_apply = QPushButton("Apply size")
        self.b_bdf_apply.setToolTip(
            "resize bitmap top-left anchored; sets BBX + advance")
        self.b_bdf_apply.clicked.connect(self._bdf_apply_metrics)
        mrow.addWidget(self.b_bdf_apply)
        self.b_bdf_revert = QPushButton("Revert")
        self.b_bdf_revert.clicked.connect(self.bdf_revert_glyph)
        mrow.addWidget(self.b_bdf_revert)
        mfl.addRow(mrow)
        trow = QHBoxLayout()
        self.b_bdf_clear = QPushButton("Clear")
        self.b_bdf_clear.clicked.connect(self._bdf_clear_fill(False))
        trow.addWidget(self.b_bdf_clear)
        self.b_bdf_fill = QPushButton("Fill")
        self.b_bdf_fill.clicked.connect(self._bdf_clear_fill(True))
        trow.addWidget(self.b_bdf_fill)
        self.b_bdf_inv = QPushButton("Invert")
        self.b_bdf_inv.clicked.connect(self.bdf_invert_glyph)
        trow.addWidget(self.b_bdf_inv)
        mfl.addRow(trow)
        arow = QHBoxLayout()
        self.b_bdf_add = QPushButton("+ Add")
        self.b_bdf_add.clicked.connect(self.bdf_add_glyph)
        arow.addWidget(self.b_bdf_add)
        self.b_bdf_del = QPushButton("Delete")
        self.b_bdf_del.clicked.connect(self.bdf_delete_glyph)
        arow.addWidget(self.b_bdf_del)
        mfl.addRow(arow)
        bdf_hint = QLabel("left paints · right erases · shares Paint/Erase "
                          "tool · Save font writes fonts/*.bdf and refreshes "
                          "the sign")
        bdf_hint.setWordWrap(True)
        bdf_side_lay.addWidget(bdf_hint)
        bdf_side_lay.addStretch(1)

        for tab in (design_tab, library_tab, output_tab):
            tab.addStretch(1)

        mid = QWidget()
        mml = QVBoxLayout(mid)
        mml.setContentsMargins(8, 8, 8, 8)
        self.c_zoom = QComboBox()
        self.c_zoom.addItems(["fit", "2", "3", "4", "6", "8"])
        self.c_zoom.setCurrentText("fit")
        self.c_zoom.currentIndexChanged.connect(self.refresh_display)
        self.ck_dots = QCheckBox("LED dots")
        self.ck_dots.setChecked(True)
        self.ck_dots.stateChanged.connect(lambda _s: self.refresh_display())
        self.ck_grid = QCheckBox("overlay")
        self.ck_grid.stateChanged.connect(lambda _s: self.refresh_display())
        self.tool_btns = QButtonGroup(self)
        self.b_paint = QPushButton("Paint")
        self.b_erase = QPushButton("Erase")
        for i, b in enumerate((self.b_paint, self.b_erase)):
            b.setCheckable(True)
            self.tool_btns.addButton(b, i)
        self.b_paint.setChecked(True)
        self.s_brush = QSpinBox()
        self.s_brush.setRange(1, 3)
        self.s_brush.setToolTip("touch-up brush size (sign + BDF)")
        self.b_clear = QPushButton("Clear touch-ups")
        self.b_clear.clicked.connect(self.clear_touch)
        self.b_preview = QPushButton("Preview 1:1")
        self.b_preview.clicked.connect(self.open_preview)
        self.b_tbsave = QPushButton("Save PNG")
        self.b_tbsave.clicked.connect(self.save_png)
        self.b_tbsend = QPushButton("Send to program…")
        self.b_tbsend.clicked.connect(self.send_to_program)
        self.b_tbshow = QPushButton("Show on screen")
        self.b_tbshow.clicked.connect(self.push_to_screen)
        # program-mode toolbar actions (built here, shown in program mode)
        self.b_prog_save_tb = QPushButton("Save program")
        self.b_prog_save_tb.clicked.connect(self.prog_save)
        self.b_prog_reload_tb = QPushButton("Reload")
        self.b_prog_reload_tb.clicked.connect(self._prog_reload)
        self.b_prog_show_tb = QPushButton("Show on screen")
        self.b_prog_show_tb.clicked.connect(self.prog_push_to_screen)
        # bdf-mode toolbar actions
        self.b_bdf_save_tb = QPushButton("Save font")
        self.b_bdf_save_tb.clicked.connect(self.bdf_save)
        self.b_bdf_reload_tb = QPushButton("Reload")
        self.b_bdf_reload_tb.clicked.connect(self._bdf_load_selected)
        # mode switch first, then per-mode widgets (toggled by set_mode)
        self.mode_btns = QButtonGroup(self)
        self.b_mode_sign = QPushButton("Sign editor")
        self.b_mode_prog = QPushButton("Program")
        self.b_mode_bdf = QPushButton("BDF")
        for i, b in enumerate(
                (self.b_mode_sign, self.b_mode_prog, self.b_mode_bdf)):
            b.setCheckable(True)
            b.setToolTip(f"Ctrl+{i + 1}")
            self.mode_btns.addButton(b, i)
        self.b_mode_sign.setChecked(True)
        self.b_mode_sign.clicked.connect(lambda: self.set_mode("sign"))
        self.b_mode_prog.clicked.connect(lambda: self.set_mode("program"))
        self.b_mode_bdf.clicked.connect(lambda: self.set_mode("bdf"))
        for w in (self.b_mode_sign, self.b_mode_prog, self.b_mode_bdf):
            self.toolbar.addWidget(w)
        self._tb_sep0 = self.toolbar.addSeparator()
        for w in (self.b_tbsave, self.b_tbsend, self.b_tbshow,
                  self.b_preview):
            self.toolbar.addWidget(w)
        for w in (self.b_prog_save_tb, self.b_prog_reload_tb,
                  self.b_prog_show_tb):
            self.toolbar.addWidget(w)
        for w in (self.b_bdf_save_tb, self.b_bdf_reload_tb):
            self.toolbar.addWidget(w)
        self._tb_sep1 = self.toolbar.addSeparator()
        self._tb_zoom_label = QLabel("zoom")
        self.toolbar.addWidget(self._tb_zoom_label)
        self.toolbar.addWidget(self.c_zoom)
        self.toolbar.addWidget(self.ck_dots)
        self.toolbar.addWidget(self.ck_grid)
        self._tb_sep2 = self.toolbar.addSeparator()
        self.toolbar.addWidget(self.b_paint)
        self.toolbar.addWidget(self.b_erase)
        self._tb_brush_label = QLabel("brush")
        self.toolbar.addWidget(self._tb_brush_label)
        self.toolbar.addWidget(self.s_brush)
        self.toolbar.addWidget(self.b_clear)
        # QToolBar owns widget visibility: toggle via actions, not widgets
        self._tb_action_of = {}
        for _a in self.toolbar.actions():
            _wd = self.toolbar.widgetForAction(_a)
            if _wd is not None:
                self._tb_action_of[_wd] = _a

        STAGE_DARK = "QScrollArea { background: #141414; border: none; }"
        self.scroll = QScrollArea()
        self.scroll.setStyleSheet(STAGE_DARK)
        self.canvas = PixelCanvas(self)
        self.scroll.setWidget(self.canvas)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.viewport().installEventFilter(self)
        mml.addWidget(self.scroll, 1)
        self.sign_info = QLabel("")
        mml.addWidget(self.sign_info)
        mml.addWidget(QLabel(
            "left paints · right erases · B/E tools · touch-ups stick to "
            "coordinates and bake into the PNG"))

        # -- program center page: programs | screens | images ---------
        prog_center = QWidget()
        pcl = QHBoxLayout(prog_center)
        pcl.setContentsMargins(8, 8, 8, 8)
        prog_split = QSplitter()
        pcl.addWidget(prog_split)
        self.prog_split = prog_split
        left_w, mid_w, right_w = QWidget(), QWidget(), QWidget()
        for w, title in ((left_w, "Programs"), (mid_w, "Screens"),
                         (right_w, "Images")):
            _l = QVBoxLayout(w)
            _l.setContentsMargins(0, 0, 0, 0)
            _l.addWidget(QLabel(f"<b>{title}</b>"))
        prog_split.addWidget(left_w)
        prog_split.addWidget(mid_w)
        prog_split.addWidget(right_w)
        prog_split.setStretchFactor(0, 0)
        prog_split.setStretchFactor(1, 1)
        prog_split.setStretchFactor(2, 1)
        ll = left_w.layout()
        self.prog_list = QListWidget()
        self.prog_list.currentRowChanged.connect(self._prog_row_changed)
        ll.addWidget(self.prog_list, 1)
        prow = QHBoxLayout()
        self.b_prog_add = QPushButton("+ Add")
        self.b_prog_add.clicked.connect(self.prog_add_program)
        prow.addWidget(self.b_prog_add)
        self.b_prog_del = QPushButton("Delete")
        self.b_prog_del.clicked.connect(self.prog_delete_program)
        prow.addWidget(self.b_prog_del)
        ll.addLayout(prow)
        ll.addWidget(QLabel("Route"))
        self.e_prog_route = QLineEdit()
        self.e_prog_route.setPlaceholderText("e.g. 401")
        self.e_prog_route.editingFinished.connect(
            self._prog_route_edited)
        ll.addWidget(self.e_prog_route)
        ll.addWidget(QLabel("Destination"))
        self.c_prog_dest = QComboBox()
        self.c_prog_dest.currentIndexChanged.connect(
            self._prog_dest_changed)
        ll.addWidget(self.c_prog_dest)
        drow = QHBoxLayout()
        self.b_prog_dest_add = QPushButton("+ Dest")
        self.b_prog_dest_add.clicked.connect(self.prog_add_dest)
        drow.addWidget(self.b_prog_dest_add)
        self.b_prog_dest_del = QPushButton("Del")
        self.b_prog_dest_del.clicked.connect(self.prog_delete_dest)
        drow.addWidget(self.b_prog_dest_del)
        ll.addLayout(drow)
        ml_ = mid_w.layout()
        self.prog_screens = QListWidget()
        self.prog_screens.setIconSize(QSize(120, 20))
        self.prog_screens.currentRowChanged.connect(
            self._prog_screen_changed)
        self.prog_screens.itemDoubleClicked.connect(
            lambda _i: self.prog_preview_screen())
        ml_.addWidget(self.prog_screens, 1)
        srow = QHBoxLayout()
        self.b_screen_up = QPushButton("▲")
        self.b_screen_up.setMaximumWidth(36)
        self.b_screen_up.clicked.connect(lambda: self.prog_move(-1))
        srow.addWidget(self.b_screen_up)
        self.b_screen_down = QPushButton("▼")
        self.b_screen_down.setMaximumWidth(36)
        self.b_screen_down.clicked.connect(lambda: self.prog_move(1))
        srow.addWidget(self.b_screen_down)
        self.b_screen_del = QPushButton("Remove")
        self.b_screen_del.clicked.connect(self.prog_delete_screen)
        srow.addWidget(self.b_screen_del)
        srow.addStretch(1)
        ml_.addLayout(srow)
        rl = right_w.layout()
        self.e_prog_imgfilter = QLineEdit()
        self.e_prog_imgfilter.setPlaceholderText("filter images…")
        self.e_prog_imgfilter.textChanged.connect(
            self._prog_images_rebuild)
        rl.addWidget(self.e_prog_imgfilter)
        self.prog_images = QListWidget()
        self.prog_images.setIconSize(QSize(120, 20))
        self.prog_images.itemDoubleClicked.connect(
            lambda _i: self.prog_add_image())
        rl.addWidget(self.prog_images, 1)
        self.b_img_add = QPushButton("Add selected image → screens")
        self.b_img_add.clicked.connect(self.prog_add_image)
        rl.addWidget(self.b_img_add)
        self.b_img_new = QPushButton("New sign PNG → screens…")
        self.b_img_new.setToolTip(
            "jump to the sign editor to design a page, then Send to program")
        self.b_img_new.clicked.connect(lambda: self.set_mode("sign"))
        rl.addWidget(self.b_img_new)

        # -- BDF center stage -----------------------------------------
        bdf_center = QWidget()
        bcl = QVBoxLayout(bdf_center)
        bcl.setContentsMargins(8, 8, 8, 8)
        self.bdf_stage = QScrollArea()
        self.bdf_stage.setStyleSheet(STAGE_DARK)
        self.bdf_stage.setWidget(self.bdf_canvas)
        self.bdf_stage.setAlignment(Qt.AlignCenter)
        bcl.addWidget(self.bdf_stage, 1)
        self.bdf_stage_info = QLabel("")
        bcl.addWidget(self.bdf_stage_info)
        bcl.addWidget(QLabel(
            "left paints · right erases · B/E tools · Ctrl+S saves the font"))

        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(mid)           # 0 sign
        self.center_stack.addWidget(prog_center)   # 1 program
        self.center_stack.addWidget(bdf_center)    # 2 bdf
        split.addWidget(self.center_stack)
        split.addWidget(self.right_stack)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)

        self._build_prog_side(prog_side_lay)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.coord_label = QLabel("x -, y -")
        self.status.addPermanentWidget(self.coord_label)
        self.status.showMessage("keys: 1-4 layout · B/E tools · arrows nudge "
                                "(canvas focused) · Ctrl+S save")
        for key, style in (("1", "top"), ("2", "bottom"), ("3", "left"),
                           ("4", "right")):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(
                lambda s=style: self._quick_style(s))
        for key, tool in (("B", "paint"), ("E", "erase")):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(
                lambda t=tool: self._set_tool(t))

    def _invert_toggled(self, _state=None):
        if 0 <= self.page_idx < len(self.pages):
            self.pages[self.page_idx]["invert"] = \
                bool(self.ck_invert.isChecked())
        self.refresh()

    def _typing(self):
        return isinstance(QApplication.focusWidget(),
                          (QLineEdit, QPlainTextEdit, QComboBox, QSpinBox))

    def _mark_path_touched(self, _text=""):
        self._path_touched = True

    def _set_tool(self, tool):
        if self._typing():
            return
        (self.b_paint if tool == "paint" else self.b_erase).setChecked(
            True)

    # -- modes -----------------------------------------------------------
    MODES = ("sign", "program", "bdf")
    MODE_TITLE = {"sign": "Sign editor", "program": "Program editor",
                  "bdf": "BDF editor"}

    def set_mode(self, mode):
        if mode not in self.MODES:
            return
        self.mode = mode
        idx = {"sign": 0, "program": 1, "bdf": 2}[mode]
        self.center_stack.setCurrentIndex(idx)
        self.right_stack.setCurrentIndex(idx)
        for b, m in ((self.b_mode_sign, "sign"),
                     (self.b_mode_prog, "program"),
                     (self.b_mode_bdf, "bdf")):
            b.blockSignals(True)
            try:
                b.setChecked(m == mode)
            finally:
                b.blockSignals(False)
        for act, m in ((self.act_sign, "sign"), (self.act_prog, "program"),
                       (self.act_bdf, "bdf")):
            act.setChecked(m == mode)
        # toolbar: contextual per mode (paint tools shared sign + BDF).
        # NOTE: QToolBar drives widget visibility from its actions, so
        # toggle the actions (widget.hide() alone is ignored here).
        def _show(widgets, cond):
            for _w in widgets:
                _a = self._tb_action_of.get(_w)
                if _a is not None:
                    _a.setVisible(bool(cond))
                else:
                    _w.setVisible(bool(cond))

        sign_w = (self.b_tbsave, self.b_tbsend, self.b_tbshow,
                  self.b_preview, self._tb_zoom_label, self.c_zoom,
                  self.ck_dots, self.ck_grid, self.b_clear)
        prog_w = (self.b_prog_save_tb, self.b_prog_reload_tb,
                  self.b_prog_show_tb)
        bdf_w = (self.b_bdf_save_tb, self.b_bdf_reload_tb)
        paint_w = (self.b_paint, self.b_erase, self._tb_brush_label,
                   self.s_brush)
        _show(sign_w, mode == "sign")
        _show(prog_w, mode == "program")
        _show(bdf_w, mode == "bdf")
        _show(paint_w, mode in ("sign", "bdf"))
        self._tb_sep0.setVisible(True)
        self._tb_sep1.setVisible(mode == "sign")
        self._tb_sep2.setVisible(mode in ("sign", "bdf"))
        self.setWindowTitle(
            f"Sign Studio -- {self.MODE_TITLE[mode]} (240x40)")
        hints = {
            "sign": "sign: type text, pick fonts, paint touch-ups, "
                    "Ctrl+S saves PNG · Ctrl+2 program · Ctrl+3 BDF",
            "program": "program: pick a file + program + destination, "
                       "manage screens, Ctrl+S saves · Ctrl+1 sign",
            "bdf": "bdf: paint glyph pixels, apply metrics, Ctrl+S saves "
                   "the font · Ctrl+1 sign",
        }
        self.status.showMessage(hints[mode])
        if mode == "bdf":
            self._bdf_draw()
        if mode == "program":
            self._prog_refresh_all()

    # -- widget helpers ----------------------------------------------
    @staticmethod
    def _group(title, parent):
        g = QGroupBox(title)
        parent.addWidget(g)
        return g

    def _field(self, parent, label, default):
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        e = QLineEdit(default)
        e.textChanged.connect(lambda _t: self._deb.start(120))
        e.returnPressed.connect(self.refresh)
        row.addWidget(e, 1)
        parent.addLayout(row)
        return e

    def _area(self, parent, label, default):
        """Two-line text box: Enter starts a new line on the blind."""
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        e = QPlainTextEdit(default)
        e.setFixedHeight(52)
        e.setTabChangesFocus(True)
        e.setLineWrapMode(QPlainTextEdit.NoWrap)
        e.textChanged.connect(lambda: self._deb.start(120))
        row.addWidget(e, 1)
        parent.addLayout(row)
        return e

    def _bdf_row(self, parent, which, default_font, default_scale):
        row = QHBoxLayout()
        row.addWidget(QLabel(which))
        c = QComboBox()
        c.addItems(self.fonts)
        c.setCurrentText(default_font if default_font in self.fonts
                         else self.fonts[0])
        c.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(c, 1)
        s = QSpinBox()
        s.setRange(1, 8)
        s.setValue(default_scale)
        s.valueChanged.connect(lambda _v: self.refresh())
        row.addWidget(s)
        parent.addLayout(row)
        return c, s

    def _sys_row(self, parent, which, default_fam, default_px,
                 default_bold):
        row = QHBoxLayout()
        row.addWidget(QLabel(which))
        c = QComboBox()
        c.setEditable(True)
        c.setInsertPolicy(QComboBox.NoInsert)
        comp = c.completer()
        comp.setCompletionMode(QCompleter.PopupCompletion)
        comp.setFilterMode(Qt.MatchContains)
        c.setCompleter(comp)
        c.setCurrentText(default_fam)
        c.activated.connect(lambda _i: self.refresh())
        c.lineEdit().returnPressed.connect(self.refresh)
        row.addWidget(c, 1)
        s = QSpinBox()
        s.setRange(6, 120)
        s.setValue(default_px)
        s.valueChanged.connect(lambda _v: self.refresh())
        row.addWidget(s)
        b = QCheckBox("B")
        b.setChecked(default_bold)
        b.setToolTip("bold")
        b.stateChanged.connect(lambda _s: self.refresh())
        row.addWidget(b)
        parent.addLayout(row)
        return c, s, b

    # -- state ---------------------------------------------------------
    def zoom(self):
        text = self.c_zoom.currentText()
        if text == "fit":
            try:
                vw = self.scroll.viewport().width()
                vh = self.scroll.viewport().height()
            except Exception:
                vw = vh = 0
            if vw >= W and vh >= H:
                # fit the whole 240x40 blind: height is the binding
                # constraint in a wide window, width in a narrow one
                return max(1, min(8, vw // W, vh // H))
            return 3
        try:
            return max(1, min(8, int(text)))
        except (TypeError, ValueError):
            return 3

    def eventFilter(self, obj, event):
        if obj is self.scroll.viewport() and \
                event.type() == QEvent.Type.Resize and \
                self.c_zoom.currentText() == "fit":
            self.refresh_display()
        return super().eventFilter(obj, event)

    def tool(self):
        return "erase" if self.b_erase.isChecked() else "paint"

    def style(self):
        for s, rb in self.style_radios.items():
            if rb.isChecked():
                return s
        return "top"

    def set_fg(self, hexv):
        self.fg_hex = hexv
        self.refresh()

    def _custom_live(self, text):
        v = text.strip()
        s = v[1:] if v.startswith("#") else v
        if len(s) in (3, 6) and all(
                c in "0123456789abcdefABCDEF" for c in s):
            self.fg_hex = "#" + s
            for b in self.col_btns.buttons():
                b.setChecked(False)
            self.refresh()

    def _quick_style(self, style):
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QPlainTextEdit, QComboBox,
                           QSpinBox)):
            return  # typing "43" must not flip layouts
        self.style_radios[style].setChecked(True)
        self.refresh()

    def drop(self):
        for e in (self.e_route, self.e_dest, self.e_via):
            e.clear()
        self.refresh()

    def job(self):
        cur = self.pages[self.page_idx] \
            if 0 <= self.page_idx < len(self.pages) else {}
        return {
            "route": self.e_route.text(), "dest": self.e_dest.toPlainText(),
            "via": self.e_via.toPlainText(), "style": self.style(),
            "route_font": self.c_rf.currentText(),
            "dest_font": self.c_df.currentText(),
            "via_font": self.c_vf.currentText(),
            "route_scale": self.s_rs.value(),
            "dest_scale": self.s_ds.value(),
            "via_scale": self.s_vs.value(),
            "fg": self.fg_hex,
            "upper_dest": cur.get("upper_dest", False),
            "via_prefix": cur.get("via_prefix", False),
            "offsets": _norm_offsets(cur),
        }

    def ensure_faces(self):
        if sysfonts.FACES:
            return True
        for _ in range(2):
            try:
                found = sysfonts.scan_system_fonts()
            except Exception:
                found = {}
            if found:
                sysfonts.FACES.update(found)
                break
        fams = sorted(sysfonts.FACES)
        for combo in (self.c_srf, self.c_sfd, self.c_sfv):
            cur = combo.currentText()
            combo.clear()
            combo.addItems(fams)
            combo.setCurrentText(cur if cur in fams else
                                 sysfonts.preferred_default(fams))
        self.face_count.setText(
            f"{len(fams)} families" if fams else "scan found nothing")
        return bool(fams)

    def _sys_spec(self):
        out = {}
        for name, combo, spin, bold, default in (
                ("route", self.c_srf, self.s_srp, self.b_srb, 34),
                ("dest", self.c_sfd, self.s_sdp, self.b_sdb, 20),
                ("via", self.c_sfv, self.s_svp, self.b_svb, 12)):
            fam = combo.currentText().strip()
            px = spin.value()
            out[name] = (*sysfonts.resolve_face(fam, bold.isChecked()),
                         px)
        return out

    def fsrc(self):
        return "sys" if (self.rb_sys.isChecked() and sysfonts.PIL_OK) \
            else "bdf"

    # -- render --------------------------------------------------------
    def refresh(self):
        t0 = time.perf_counter()
        sys_mode = self.fsrc() == "sys" and self.ensure_faces()
        self.bdf_rows.setVisible(not sys_mode)
        self.sys_rows.setVisible(sys_mode)
        job = self.job()
        cur = self.pages[self.page_idx] \
            if 0 <= self.page_idx < len(self.pages) else {}
        inv = bool(cur.get("invert", False))
        try:
            if sys_mode:
                spec = self._sys_spec()
                sig = ("sys", repr(sorted(job.items())), repr(spec),
                       inv)
                if sig == self._last_sig:
                    return
                frame, info = sysfonts.render_system(ENG, job, spec)
            else:
                sig = ("bdf", repr(sorted(job.items())), inv)
                if sig == self._last_sig:
                    return
                frame, info = ENG.render(job)
        except (ValueError, OSError) as e:
            self.status.showMessage(str(e))
            return
        self._last_sig = sig
        self._inv = inv
        if inv:
            fg = ENG.parse_colour(self.fg_hex)
            out = bytearray(len(frame))
            for i in range(0, len(frame), 3):
                if not (frame[i] or frame[i + 1] or frame[i + 2]):
                    out[i], out[i + 1], out[i + 2] = fg
            frame = out
            info = dict(info, lit=sum(
                1 for i in range(0, len(frame), 3)
                if frame[i] or frame[i + 1] or frame[i + 2]))
        self.base = frame
        self.info = info
        if not self._path_touched:
            self.e_path.setText(ENG.default_filename(
                info.get("route", ""), self.e_dest.toPlainText()))
        self._apply_touch()
        ms = (time.perf_counter() - t0) * 1000
        self._show_status(ms)

    def _apply_touch(self):
        fg = ENG.parse_colour(self.fg_hex)
        frame = bytearray(self.base)
        for x, y in self.touch_add:
            o = (y * W + x) * 3
            frame[o], frame[o + 1], frame[o + 2] = fg
        for x, y in self.touch_del:
            o = (y * W + x) * 3
            frame[o], frame[o + 1], frame[o + 2] = 0, 0, 0
        self.frame = frame
        self.info = dict(
            self.info, lit=sum(
                1 for i in range(0, len(frame), 3)
                if frame[i] or frame[i + 1] or frame[i + 2]))
        self.refresh_display()

    def _show_status(self, ms=None):
        i = self.info
        touch = ""
        if self.touch_add or self.touch_del:
            touch = (f"  touch +{len(self.touch_add)} "
                     f"-{len(self.touch_del)}")
        warn = ("  ! " + " / ".join(i.get("warnings", ""))
                if i.get("warnings") else "")
        timing = f"  {ms:.0f}ms" if ms is not None else ""
        self.status.showMessage(
            f"{i.get('w', W)}x{i.get('h', H)}  {i.get('lit', 0)} lit "
            f"pixels  {i.get('fg', '')}"
            f"{'  system' if i.get('mode') == 'system' else ''}"
            f"{'  inverted' if getattr(self, '_inv', False) else ''}"
            f"{touch}{warn}{timing}")

    # -- display ---------------------------------------------------------
    def _frame_image(self):
        data = bytes(self.frame)
        img = QImage(data, W, H, W * 3, QImage.Format.Format_RGB888)
        img._keep = data  # QImage borrows; keep the bytes alive
        return img

    def refresh_display(self):
        z = self.zoom()
        img = self._frame_image()
        if self.ck_dots.isChecked() and z >= 3:
            big = QImage(W * z, H * z, QImage.Format.Format_RGB888)
            big.fill(Qt.black)
            fg = QColor(self.info.get("fg", "#ffffff"))
            p = QPainter(big)
            try:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(fg))
                fr = self.frame
                tiny = (z < 5)  # sub-3px ellipses rasterise to nothing
                for y in range(H):
                    o = y * W * 3
                    for x in range(W):
                        if fr[o] or fr[o + 1] or fr[o + 2]:
                            if tiny:
                                p.drawRect(x * z + 1, y * z + 1,
                                           z - 2, z - 2)
                            else:
                                p.drawEllipse(x * z + 1, y * z + 1,
                                              z - 2, z - 2)
                        o += 3
            finally:
                p.end()
            shown = big
        else:
            shown = img.scaled(W * z, H * z, Qt.IgnoreAspectRatio,
                               Qt.FastTransformation)
        if self.ck_grid.isChecked():
            shown = QImage(shown)
            p = QPainter(shown)
            try:
                for x in range(W + 1):
                    p.setPen(QColor("#3d3d3d" if x % 10 == 0
                                    else "#242424"))
                    p.drawLine(x * z, 0, x * z, H * z)
                for y in range(H + 1):
                    p.setPen(QColor("#3d3d3d" if y % 10 == 0
                                    else "#242424"))
                    p.drawLine(0, y * z, W * z, y * z)
                for name, f in self.info.get("fields", {}).items():
                    c = f.get("cell")
                    if not c:
                        continue
                    p.setPen(QColor(CELL_COLOURS.get(name, "#ffffff")))
                    p.drawRect(c[0] * z, c[1] * z,
                               (c[2] - c[0] + 1) * z - 1,
                               (c[3] - c[1] + 1) * z - 1)
            finally:
                p.end()
        pm = QPixmap.fromImage(shown)
        self.canvas.setPixmap(pm)
        self.canvas.setFixedSize(pm.size())
        i = self.info
        warn = (" · " + " / ".join(i.get("warnings", "")[:2])
                if i.get("warnings") else "")
        self.sign_info.setText(
            f"{i.get('w', W)}x{i.get('h', H)} · {i.get('lit', 0)} lit · "
            f"{i.get('fg', '')} · zoom {self.zoom()}x{warn}")
        self._show_status()

    # -- pixel tool --------------------------------------------------------
    def paint_stroke(self, pts, erase):
        s = self.s_brush.value()
        changed = False
        for x, y in pts:
            for dy in range(s):
                for dx in range(s):
                    px, py = x + dx, y + dy
                    if not (0 <= px < W and 0 <= py < H):
                        continue
                    if erase:
                        if (px, py) in self.touch_add:
                            self.touch_add.discard((px, py))
                            changed = True
                        if (px, py) not in self.touch_del:
                            self.touch_del.add((px, py))
                            changed = True
                    else:
                        if (px, py) in self.touch_del:
                            self.touch_del.discard((px, py))
                            changed = True
                        if (px, py) not in self.touch_add:
                            self.touch_add.add((px, py))
                            changed = True
        if changed:
            self._apply_touch()
            self._show_status()

    def clear_touch(self):
        self.touch_add.clear()
        self.touch_del.clear()
        self._apply_touch()
        self._show_status()

    # -- BDF editor ------------------------------------------------------
    def _bdf_init(self):
        self._bdf_load_selected()

    def _bdf_font_path(self, name):
        return os.path.join(THIS_DIR, "fonts", os.path.basename(name))

    def _bdf_font_changed(self, _text=""):
        if not self._bdf_loading:
            self._bdf_load_selected()

    def _bdf_load_selected(self):
        name = self.c_bdffont.currentText().strip()
        if not name:
            return
        try:
            self.bdf_doc = BdfDoc.load(self._bdf_font_path(name))
        except (OSError, ValueError) as e:
            self.bdf_doc = None
            self.l_bdf_file.setText(f"cannot load {name}: {e}")
            self.bdf_list.clear()
            self._bdf_draw()
            return
        self.bdf_orig_rows = {g.enc: (list(g.rows), g.w, g.h)
                              for g in self.bdf_doc.glyphs}
        # prefer a useful default glyph: 'A', else first glyph
        fav = self.bdf_doc.find(65) or (
            self.bdf_doc.glyphs[0] if self.bdf_doc.glyphs else None)
        self.bdf_enc = fav.enc if fav else 65
        self._bdf_loading = True
        try:
            self._bdf_rebuild_list()
        finally:
            self._bdf_loading = False
        self._bdf_select_enc(self.bdf_enc)
        self._bdf_update_file_label()

    def _bdf_update_file_label(self):
        if self.bdf_doc is None:
            return
        n = len(self.bdf_doc.glyphs)
        dirty = len(self.bdf_doc.dirty)
        tag = f" *{dirty} unsaved*" if dirty else " saved"
        self.l_bdf_file.setText(
            f"{self.bdf_doc.filename}: {n} glyphs{tag}")

    @staticmethod
    def _bdf_label(g):
        if 32 <= g.enc < 127:
            ch = chr(g.enc)
        elif g.enc >= 0:
            try:
                ch = chr(g.enc)
                if not ch.isprintable():
                    ch = "·"
            except ValueError:
                ch = "·"
        else:
            ch = "?"
        return f"U+{g.enc:04X} {g.enc:4d} '{ch}' {g.name} [{g.w}x{g.h}]"

    def _bdf_filtered(self):
        if self.bdf_doc is None:
            return []
        q = self.e_bdf_filter.text().strip().lower()
        glyphs = self.bdf_doc.glyphs
        if not q:
            return list(glyphs)
        out = []
        for g in glyphs:
            if q in g.name.lower() or q == str(g.enc) or \
                    (len(q) == 1 and ord(q) == g.enc):
                out.append(g)
                continue
            try:
                if int(q, 0) == g.enc:
                    out.append(g)
            except ValueError:
                pass
        return out

    def _bdf_rebuild_list(self):
        if self.bdf_doc is None:
            return
        keep = self.bdf_enc
        self.bdf_list.blockSignals(True)
        try:
            self.bdf_list.clear()
            self._bdf_rows = self._bdf_filtered()
            for g in self._bdf_rows:
                mark = "*" if g.enc in self.bdf_doc.dirty else " "
                self.bdf_list.addItem(mark + self._bdf_label(g))
        finally:
            self.bdf_list.blockSignals(False)
        self._bdf_select_enc(keep)

    def _bdf_select_enc(self, enc):
        rows = getattr(self, "_bdf_rows", [])
        for i, g in enumerate(rows):
            if g.enc == enc:
                self.bdf_list.setCurrentRow(i)
                return
        if rows:
            self.bdf_list.setCurrentRow(0)

    def _bdf_row_changed(self, row):
        rows = getattr(self, "_bdf_rows", [])
        if 0 <= row < len(rows):
            self.bdf_enc = rows[row].enc
            self._bdf_sync_spins()
            self._bdf_draw()

    def _bdf_step(self, direction):
        n = self.bdf_list.count()
        if n <= 0:
            return
        self.bdf_list.setCurrentRow(
            (self.bdf_list.currentRow() + direction) % n)

    def bdf_glyph(self):
        if self.bdf_doc is None:
            return None
        return self.bdf_doc.find(self.bdf_enc)

    def bdf_zoom(self):
        try:
            return max(4, min(24, int(self.s_bdfzoom.value())))
        except (TypeError, ValueError):
            return 12

    def bdf_hover(self, p):
        self.l_bdf_coord.setText(
            f"x {p[0]}, y {p[1]}" if p else "x -, y -")

    def _bdf_draw(self):
        g = self.bdf_glyph()
        z = self.bdf_zoom()
        if g is None:
            img = QImage(64, 64, QImage.Format.Format_RGB888)
            img.fill(Qt.black)
            self.bdf_canvas.setPixmap(QPixmap.fromImage(img))
            self.l_bdf_status.setText("no glyph")
            return
        pad = 1  # 1-cell margin for the origin/baseline guides
        img = QImage((g.w + pad * 2) * z, (g.h + pad * 2) * z,
                     QImage.Format.Format_RGB888)
        img.fill(Qt.black)
        p = QPainter(img)
        try:
            # lit pixels as amber blocks
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor("#ffb000")))
            for y in range(g.h):
                bits = g.rows[y] if y < len(g.rows) else 0
                for x in range(g.w):
                    if (bits >> (g.w - 1 - x)) & 1:
                        p.drawRect((x + pad) * z, (y + pad) * z,
                                   z, z)
            if self.ck_bdfgrid.isChecked():
                for gx in range(g.w + 1):
                    p.setPen(QColor("#3d3d3d" if gx % 5 == 0
                                    else "#222222"))
                    p.drawLine((gx + pad) * z, pad * z,
                               (gx + pad) * z, (g.h + pad) * z)
                for gy in range(g.h + 1):
                    p.setPen(QColor("#3d3d3d" if gy % 5 == 0
                                    else "#222222"))
                    p.drawLine(pad * z, (gy + pad) * z,
                               (g.w + pad) * z, (gy + pad) * z)
            # origin (baseline start) + advance marker, like the engine:
            # bitmap left edge sits at origin_x + xoff, top row at
            # baseline - (yoff + h)
            # baseline guide: device row of the text baseline
            base_row = pad + g.h + g.yoff
            p.setPen(QColor("#28ff5a"))
            p.drawLine(pad * z, base_row * z,
                       (g.w + pad) * z, base_row * z)
            # origin dot + advance line
            ox = pad - g.xoff
            p.setPen(QColor("#4dd2ff"))
            p.drawLine(ox * z, pad * z, ox * z, (g.h + pad) * z)
            adv = pad - g.xoff + self._bdf_advance()
            p.setPen(QColor("#ffe14d"))
            p.drawLine(adv * z, pad * z, adv * z, (g.h + pad) * z)
        finally:
            p.end()
        pm = QPixmap.fromImage(img)
        self.bdf_canvas.setPixmap(pm)
        self.bdf_canvas.setFixedSize(pm.size())
        dirty = "*" if g.enc in self.bdf_doc.dirty else ""
        ch = chr(g.enc) if 32 <= g.enc < 127 else ""
        txt = (f"{dirty}U+{g.enc:04X} '{ch}' {g.name}  {g.w}x{g.h} "
               f"xo {g.xoff} yo {g.yoff} adv {self._bdf_advance()}  "
               f"{g.lit_count()} lit")
        self.l_bdf_status.setText(txt)
        self.bdf_stage_info.setText(txt)

    def _bdf_advance(self):
        g = self.bdf_glyph()
        return g.dwidth[0] if g else 0

    def _bdf_sync_spins(self):
        g = self.bdf_glyph()
        if g is None:
            return
        for w in (self.s_bdf_dw, self.s_bdf_w, self.s_bdf_h,
                  self.s_bdf_xo, self.s_bdf_yo):
            w.blockSignals(True)
        try:
            self.s_bdf_dw.setValue(max(0, min(64, g.dwidth[0])))
            self.s_bdf_w.setValue(g.w)
            self.s_bdf_h.setValue(g.h)
            self.s_bdf_xo.setValue(max(-32, min(32, g.xoff)))
            self.s_bdf_yo.setValue(max(-32, min(32, g.yoff)))
        finally:
            for w in (self.s_bdf_dw, self.s_bdf_w, self.s_bdf_h,
                      self.s_bdf_xo, self.s_bdf_yo):
                w.blockSignals(False)

    def _bdf_mark_dirty(self, g):
        self.bdf_doc.dirty.add(g.enc)
        self._bdf_update_file_label()
        self._bdf_draw()
        # refresh the * in the list without rebuilding
        row = self.bdf_list.currentRow()
        if row >= 0:
            self.bdf_list.blockSignals(True)
            try:
                item = self.bdf_list.item(row)
                if item is not None and not item.text().startswith("*"):
                    item.setText("*" + item.text()[1:])
            finally:
                self.bdf_list.blockSignals(False)

    def bdf_paint(self, pts, erase):
        g = self.bdf_glyph()
        if g is None:
            return
        try:
            s = max(1, min(3, int(self.s_brush.value())))
        except (TypeError, ValueError):
            s = 1
        changed = False
        for x, y in pts:
            for dy in range(s):
                for dx in range(s):
                    if g.set_pixel(x + dx, y + dy, not erase):
                        changed = True
        if changed:
            self._bdf_mark_dirty(g)

    def _bdf_clear_fill(self, fill):
        def _go():
            g = self.bdf_glyph()
            if g is None:
                return
            (g.fill() if fill else g.clear())
            self._bdf_mark_dirty(g)
        return _go

    def bdf_invert_glyph(self):
        g = self.bdf_glyph()
        if g is None:
            return
        g.invert()
        self._bdf_mark_dirty(g)

    def _bdf_apply_metrics(self):
        g = self.bdf_glyph()
        if g is None:
            return
        g.resize(self.s_bdf_w.value(), self.s_bdf_h.value())
        g.xoff = int(self.s_bdf_xo.value())
        g.yoff = int(self.s_bdf_yo.value())
        g.dwidth = (int(self.s_bdf_dw.value()), g.dwidth[1])
        self._bdf_mark_dirty(g)
        self._bdf_sync_spins()
        # list label shows the new size
        self._bdf_rebuild_list()
        self._bdf_select_enc(g.enc)

    def bdf_revert_glyph(self):
        g = self.bdf_glyph()
        if g is None:
            return
        orig = self.bdf_orig_rows.get(g.enc)
        if orig is None:
            self.status.showMessage("nothing to revert for this glyph")
            return
        rows, w, h = orig
        g.rows, g.w, g.h = list(rows), w, h
        self.bdf_doc.dirty.discard(g.enc)
        self._bdf_sync_spins()
        self._bdf_rebuild_list()
        self._bdf_select_enc(g.enc)
        self._bdf_update_file_label()
        self._bdf_draw()

    def bdf_add_glyph(self):
        if self.bdf_doc is None:
            return
        text, ok = QInputDialog.getText(
            self, "Add glyph",
            "character or code (e.g. A, 65, U+0041):")
        if not ok or not text.strip():
            return
        t = text.strip()
        if len(t) == 1 and not t.startswith("U"):
            enc = ord(t)
        else:
            try:
                enc = int(t.replace("U+", "0x").replace("u+", "0x"), 0)
            except ValueError:
                QMessageBox.warning(self, "Add glyph",
                                    f"cannot parse '{t}'")
                return
        if self.bdf_doc.find(enc) is not None:
            QMessageBox.warning(self, "Add glyph",
                                f"U+{enc:04X} already exists")
            return
        tpl = self.bdf_glyph() or (self.bdf_doc.glyphs[0]
                                   if self.bdf_doc.glyphs else None)
        if tpl is not None:
            w, h = tpl.w, tpl.h
            dw = tpl.dwidth
            sw = tpl.swidth
        else:
            w, h, dw, sw = 8, 8, (8, 0), (480, 0)
        name = chr(enc) if 32 <= enc < 127 else f"uni{enc:04X}"
        g = BdfGlyph(name, enc, sw, dw, (w, h, 0, 0),
                     [0] * h)
        # keep codepoint order (BDF readers like that)
        self.bdf_doc.glyphs.append(g)
        self.bdf_doc.glyphs.sort(key=lambda gg: gg.enc)
        self.bdf_orig_rows[enc] = ([0] * h, w, h)
        self.bdf_doc.dirty.add(enc)
        self.bdf_enc = enc
        self._bdf_rebuild_list()
        self._bdf_select_enc(enc)
        self._bdf_sync_spins()
        self._bdf_update_file_label()

    def bdf_delete_glyph(self):
        g = self.bdf_glyph()
        if g is None or self.bdf_doc is None:
            return
        if QMessageBox.question(
                self, "Delete glyph",
                f"Delete U+{g.enc:04X} '{g.name}' from "
                f"{self.bdf_doc.filename}?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        self.bdf_doc.glyphs = [gg for gg in self.bdf_doc.glyphs
                               if gg.enc != g.enc]
        self.bdf_doc.dirty.discard(g.enc)
        self.bdf_orig_rows.pop(g.enc, None)
        self.bdf_enc = (self.bdf_doc.glyphs[0].enc
                        if self.bdf_doc.glyphs else 65)
        self._bdf_rebuild_list()
        self._bdf_select_enc(self.bdf_enc)
        self._bdf_update_file_label()
        self._bdf_sync_spins()
        self._bdf_draw()

    def _bdf_refresh_font_lists(self, name):
        if name not in self.fonts:
            self.fonts = ENG.available_fonts() or self.fonts
        for combo in (self.c_rf, self.c_df, self.c_vf,
                      self.c_bdffont):
            cur = combo.currentText()
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItems(self.fonts)
                combo.setCurrentText(
                    name if combo is self.c_bdffont
                    else (cur if cur in self.fonts else self.fonts[0]))
            finally:
                combo.blockSignals(False)

    def bdf_save(self):
        if self.bdf_doc is None:
            return False
        try:
            self.bdf_doc.save()
        except OSError as e:
            self.status.showMessage(f"BDF save failed: {e}")
            return False
        self.bdf_orig_rows = {g.enc: (list(g.rows), g.w, g.h)
                              for g in self.bdf_doc.glyphs}
        self._bdf_rebuild_list()
        self._bdf_select_enc(self.bdf_enc)
        self._bdf_update_file_label()
        self._bdf_refresh_font_lists(self.bdf_doc.filename)
        self._last_sig = None  # sign canvas re-renders with new glyphs
        self.refresh()
        self.status.showMessage(f"saved {self.bdf_doc.filename}")
        return True

    def bdf_save_as(self):
        if self.bdf_doc is None:
            return False
        start = os.path.join(THIS_DIR, "fonts",
                             self.bdf_doc.filename)
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save BDF As", start, "BDF fonts (*.bdf)")
        if not dest:
            return False
        if not dest.lower().endswith(".bdf"):
            dest += ".bdf"
        if os.path.normpath(dest) != os.path.normpath(
                os.path.join(THIS_DIR, "fonts",
                             os.path.basename(dest))):
            QMessageBox.warning(self, "Save BDF As",
                                "BDF fonts must live under fonts/")
            return False
        try:
            self.bdf_doc.save(dest)
        except OSError as e:
            self.status.showMessage(f"BDF save failed: {e}")
            return False
        self.bdf_orig_rows = {g.enc: (list(g.rows), g.w, g.h)
                              for g in self.bdf_doc.glyphs}
        self._bdf_rebuild_list()
        self._bdf_select_enc(self.bdf_enc)
        self._bdf_update_file_label()
        self._bdf_refresh_font_lists(self.bdf_doc.filename)
        self.c_bdffont.setCurrentText(self.bdf_doc.filename)
        self._last_sig = None
        self.refresh()
        self.status.showMessage(f"saved {self.bdf_doc.filename}")
        return True

    # -- program editor --------------------------------------------------
    def _build_prog_side(self, lay):
        file_box = self._group("Program file", lay)
        fl = QVBoxLayout(file_box)
        self.c_prog_file = QComboBox()
        self.c_prog_file.currentIndexChanged.connect(
            self._prog_file_changed)
        fl.addWidget(self.c_prog_file)
        frow = QHBoxLayout()
        self.b_prog_new = QPushButton("New…")
        self.b_prog_new.clicked.connect(self.prog_new_file)
        frow.addWidget(self.b_prog_new)
        self.b_prog_save = QPushButton("Save")
        self.b_prog_save.clicked.connect(self.prog_save)
        frow.addWidget(self.b_prog_save)
        self.b_prog_reload = QPushButton("Reload")
        self.b_prog_reload.clicked.connect(self._prog_reload)
        frow.addWidget(self.b_prog_reload)
        fl.addLayout(frow)
        self.l_prog_msg = QLabel("")
        self.l_prog_msg.setWordWrap(True)
        fl.addWidget(self.l_prog_msg)

        def_box = self._group("Defaults", lay)
        df = QFormLayout(def_box)
        self.s_prog_rot = QSpinBox()
        self.s_prog_rot.setRange(1, 3600)
        self.s_prog_rot.setValue(5)
        self.s_prog_rot.valueChanged.connect(self._prog_defaults_edited)
        df.addRow("rotate (s)", self.s_prog_rot)
        self.c_prog_fit = QComboBox()
        self.c_prog_fit.addItems(["fit", "fill", "stretch"])
        self.c_prog_fit.currentIndexChanged.connect(
            self._prog_defaults_edited)
        df.addRow("image fit", self.c_prog_fit)
        self.e_prog_colour = QLineEdit()
        self.e_prog_colour.setPlaceholderText("(inherit)")
        self.e_prog_colour.editingFinished.connect(
            self._prog_defaults_edited)
        df.addRow("colour", self.e_prog_colour)

        scr_box = self._group("Screen", lay)
        sf = QFormLayout(scr_box)
        self.l_prog_screen = QLabel("—")
        self.l_prog_screen.setWordWrap(True)
        sf.addRow("image", self.l_prog_screen)
        self.s_prog_secs = QSpinBox()
        self.s_prog_secs.setRange(0, 3600)
        self.s_prog_secs.setSpecialValueText("inherit")
        self.s_prog_secs.valueChanged.connect(self._prog_screen_edited)
        sf.addRow("seconds", self.s_prog_secs)
        self.c_prog_screen_fit = QComboBox()
        self.c_prog_screen_fit.addItems(
            ["inherit", "fit", "fill", "stretch"])
        self.c_prog_screen_fit.currentIndexChanged.connect(
            self._prog_screen_edited)
        sf.addRow("fit", self.c_prog_screen_fit)
        self.e_prog_screen_colour = QLineEdit()
        self.e_prog_screen_colour.setPlaceholderText("(inherit)")
        self.e_prog_screen_colour.editingFinished.connect(
            self._prog_screen_edited)
        sf.addRow("colour", self.e_prog_screen_colour)
        self.b_prog_preview = QPushButton("Preview screen")
        self.b_prog_preview.clicked.connect(self.prog_preview_screen)
        sf.addRow(self.b_prog_preview)

        show_box = self._group("Show on screen", lay)
        sl = QVBoxLayout(show_box)
        self.l_prog_routing = QLabel("")
        self.l_prog_routing.setWordWrap(True)
        sl.addWidget(self.l_prog_routing)
        self.b_prog_push = QPushButton("Show on screen")
        self.b_prog_push.clicked.connect(self.prog_push_to_screen)
        sl.addWidget(self.b_prog_push)
        self.l_prog_hint = QLabel("The matrix follows program_control.json "
                                  "while program.py is running.")
        self.l_prog_hint.setWordWrap(True)
        sl.addWidget(self.l_prog_hint)

    def save_current(self):
        if self.mode == "bdf":
            return self.bdf_save()
        if self.mode == "program":
            return self.prog_save()
        return self.save_png()

    def _prog_full(self, rel):
        rel = str(rel or "").strip()
        if not rel:
            return None
        return rel if os.path.isabs(rel) else os.path.normpath(
            os.path.join(THIS_DIR, rel))

    def _prog_init(self):
        files = program_files()
        want = None
        if isinstance(self.program_routing, dict):
            want = self.program_routing.get("file")
        self._prog_loading = True
        try:
            self.c_prog_file.clear()
            self.c_prog_file.addItems(files)
            if want and want in files:
                self.c_prog_file.setCurrentText(want)
            elif files:
                self.c_prog_file.setCurrentIndex(0)
        finally:
            self._prog_loading = False
        self._prog_load_file(self.c_prog_file.currentText())

    def _prog_file_changed(self, _i=0):
        if self._prog_loading:
            return
        if self.prog_dirty and not self._prog_confirm_discard():
            self._prog_loading = True
            try:
                if self.prog_file:
                    rel = os.path.relpath(self.prog_file, THIS_DIR)
                    self.c_prog_file.setCurrentText(rel)
            finally:
                self._prog_loading = False
            return
        self._prog_load_file(self.c_prog_file.currentText())

    def _prog_confirm_discard(self):
        return QMessageBox.question(
            self, "Program",
            "Discard unsaved program changes?",
            QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _prog_load_file(self, rel):
        full = self._prog_full(rel)
        if not full or not os.path.isfile(full):
            self.prog_file, self.prog_data = None, None
            self.prog_name, self.prog_dest = None, None
            self.l_prog_msg.setText("pick a programs/*.json file")
            self._prog_refresh_all()
            return False
        try:
            with open(full) as f:
                data = json.load(f)
            programs_model.load_programs_file(full)
        except (OSError, ValueError) as e:
            self.l_prog_msg.setText(f"cannot read: {e}")
            return False
        except SystemExit as e:
            self.l_prog_msg.setText(f"invalid program file: {e}")
            return False
        self.prog_file, self.prog_data = full, data
        self.prog_dirty = False
        names = sorted((data.get("programs") or {}).keys())
        keep = self.prog_name if self.prog_name in names else None
        if keep is None and isinstance(self.program_routing, dict):
            r = self.program_routing.get("program")
            keep = r if r in names else None
        self.prog_name = keep or (names[0] if names else None)
        self.prog_dest = None
        self.prog_screen = 0
        self.l_prog_msg.setText(
            f"{os.path.relpath(full, THIS_DIR)} · {len(names)} programs")
        self._prog_refresh_all()
        return True

    def _prog_reload(self):
        if self.prog_dirty and not self._prog_confirm_discard():
            return
        if self.prog_file:
            self._prog_load_file(
                os.path.relpath(self.prog_file, THIS_DIR))

    def _prog_mark_dirty(self, msg="unsaved changes"):
        self.prog_dirty = True
        base = os.path.relpath(self.prog_file, THIS_DIR) \
            if self.prog_file else "no file"
        self.l_prog_msg.setText(f"{base} · *{msg}*")

    def _prog_progs(self):
        if isinstance(self.prog_data, dict):
            p = self.prog_data.get("programs")
            if isinstance(p, dict):
                return p
        return {}

    def _prog_current(self):
        return self._prog_progs().get(self.prog_name)

    def _prog_dest_names(self, prog):
        if not isinstance(prog, dict):
            return []
        d = prog.get("destinations", [])
        if isinstance(d, dict):
            return list(d)
        if isinstance(d, str):
            return [d]
        if isinstance(d, list):
            return [str(x) for x in d if str(x).strip()]
        return []

    def _prog_entry_list(self, prog, dest):
        """Live screen list for dest (both file shapes)."""
        if not isinstance(prog, dict):
            return []
        d = prog.get("destinations", [])
        if isinstance(d, dict):
            entry = d.get(dest)
            if isinstance(entry, dict):
                key = "images" if "images" in entry else (
                    "screens" if "screens" in entry else "images")
                lst = entry.get(key)
                if not isinstance(lst, list):
                    lst = entry[key] = []
                return lst
            if isinstance(entry, list):
                return entry
            lst = d[dest] = []
            return lst
        if isinstance(d, list):
            lst = prog.get("screens")
            if not isinstance(lst, list):
                lst = prog["screens"] = []
            return lst
        return []

    def _prog_refresh_all(self):
        if self.prog_file is None or self.prog_data is None:
            if not self.prog_dirty:
                self._prog_load_file(self.c_prog_file.currentText())
                return
        self._prog_programs_rebuild()
        self._prog_dests_rebuild()
        self._prog_screens_rebuild()
        self._prog_inspector_sync()
        self._prog_images_rebuild()
        self._prog_routing_sync()

    def _prog_programs_rebuild(self):
        self.prog_list.blockSignals(True)
        try:
            self.prog_list.clear()
            for name in sorted(self._prog_progs()):
                raw = self._prog_progs()[name] or {}
                route = str(raw.get("route", "") or "")
                tag = name if route in ("", name) else f"{route} ({name})"
                n = len(self._prog_dest_names(raw))
                self.prog_list.addItem(f"{tag} · {n} dest")
            names = sorted(self._prog_progs())
            if self.prog_name in names:
                self.prog_list.setCurrentRow(names.index(self.prog_name))
            elif names:
                self.prog_list.setCurrentRow(0)
        finally:
            self.prog_list.blockSignals(False)

    def _prog_row_changed(self, row):
        names = sorted(self._prog_progs())
        if 0 <= row < len(names):
            self.prog_name = names[row]
            self.prog_dest = None
            self.prog_screen = 0
            prog = self._prog_current() or {}
            self.e_prog_route.setText(str(prog.get("route", "") or ""))
            self._prog_dests_rebuild()
            self._prog_screens_rebuild()
            self._prog_inspector_sync()
            self._prog_routing_sync()

    def _prog_dests_rebuild(self):
        prog = self._prog_current() or {}
        names = self._prog_dest_names(prog)
        self.c_prog_dest.blockSignals(True)
        try:
            self.c_prog_dest.clear()
            self.c_prog_dest.addItems(names)
            if self.prog_dest in names:
                self.c_prog_dest.setCurrentText(self.prog_dest)
            elif names:
                self.c_prog_dest.setCurrentIndex(0)
                self.prog_dest = names[0]
            else:
                self.prog_dest = None
        finally:
            self.c_prog_dest.blockSignals(False)

    def _prog_dest_changed(self, _i=0):
        t = self.c_prog_dest.currentText().strip()
        self.prog_dest = t or None
        self.prog_screen = 0
        self._prog_screens_rebuild()
        self._prog_inspector_sync()
        self._prog_routing_sync()

    def _prog_route_edited(self):
        prog = self._prog_current()
        if prog is None:
            return
        prog["route"] = self.e_prog_route.text().strip()
        self._prog_mark_dirty()
        self._prog_programs_rebuild()

    def _prog_thumb(self, rel):
        full = rel if os.path.isabs(str(rel or "")) else \
            os.path.normpath(os.path.join(THIS_DIR, str(rel or "")))
        pm = QPixmap(full)
        if pm.isNull():
            return None
        return QIcon(pm.scaled(QSize(120, 20), Qt.KeepAspectRatio,
                               Qt.FastTransformation))

    def _prog_screen_text(self, i, entry):
        img = _screen_image(entry)
        base = os.path.basename(str(img or "")) or "(no image)"
        bits = []
        if isinstance(entry, dict):
            if entry.get("seconds"):
                bits.append(f"{entry['seconds']}s")
            if entry.get("fit"):
                bits.append(str(entry["fit"]))
            if entry.get("colour", entry.get("color")):
                bits.append(str(entry.get("colour",
                                          entry.get("color"))))
        dest = ""
        if isinstance(entry, dict) and entry.get("destination"):
            dest = f" [{entry['destination']}]"
        extra = f" ({', '.join(bits)})" if bits else ""
        missing = "" if os.path.isfile(
            os.path.join(THIS_DIR, str(img or ""))) else "missing! "
        return f"{i + 1}. {missing}{base}{extra}{dest}"

    def _prog_screens_rebuild(self):
        prog = self._prog_current()
        lst = self._prog_entry_list(prog, self.prog_dest) \
            if prog is not None else []
        self.prog_screens.blockSignals(True)
        try:
            self.prog_screens.clear()
            for i, entry in enumerate(lst):
                item = QListWidgetItem(
                    self._prog_screen_text(i, entry))
                icon = self._prog_thumb(_screen_image(entry))
                if icon is not None:
                    item.setIcon(icon)
                self.prog_screens.addItem(item)
            if lst:
                self.prog_screen = max(
                    0, min(self.prog_screen, len(lst) - 1))
                self.prog_screens.setCurrentRow(self.prog_screen)
            else:
                self.prog_screen = 0
        finally:
            self.prog_screens.blockSignals(False)
        self._prog_inspector_sync()

    def _prog_screen_changed(self, row):
        if row >= 0:
            self.prog_screen = row
        self._prog_inspector_sync()

    def _prog_selected_entry(self):
        prog = self._prog_current()
        if prog is None:
            return None, None
        lst = self._prog_entry_list(prog, self.prog_dest)
        if 0 <= self.prog_screen < len(lst):
            return lst, lst[self.prog_screen]
        return lst, None

    def _prog_inspector_sync(self):
        self._prog_loading = True
        try:
            if isinstance(self.prog_data, dict):
                self.s_prog_rot.setValue(max(
                    1, int(self.prog_data.get("rotate_seconds", 5) or 5)))
                fit = str(self.prog_data.get("image_fit", "fit") or "fit")
                if fit in ("fit", "fill", "stretch"):
                    self.c_prog_fit.setCurrentText(fit)
                col = self.prog_data.get("colour",
                                         self.prog_data.get("color", ""))
                self.e_prog_colour.setText(str(col or ""))
            _lst, entry = self._prog_selected_entry()
            img = _screen_image(entry) if entry is not None else ""
            self.l_prog_screen.setText(str(img or "—"))
            secs = entry.get("seconds", 0) if isinstance(entry, dict) \
                else 0
            try:
                secs = int(float(secs or 0))
            except (TypeError, ValueError):
                secs = 0
            self.s_prog_secs.setValue(max(0, min(3600, secs)))
            fit = entry.get("fit", "") if isinstance(entry, dict) \
                else ""
            self.c_prog_screen_fit.setCurrentText(
                fit if fit in ("fit", "fill", "stretch") else "inherit")
            col = ""
            if isinstance(entry, dict):
                col = entry.get("colour", entry.get("color", ""))
            self.e_prog_screen_colour.setText(str(col or ""))
        finally:
            self._prog_loading = False
        self._prog_routing_sync()

    def _prog_routing_sync(self):
        if self.prog_file and self.prog_name:
            rel = os.path.relpath(self.prog_file, THIS_DIR)
            self.l_prog_routing.setText(
                f"{rel} › {self.prog_name} › "
                f"{self.prog_dest or 'all'}")
        else:
            self.l_prog_routing.setText("nothing selected")

    def _prog_defaults_edited(self, *_a):
        if self._prog_loading or not isinstance(self.prog_data, dict):
            return
        try:
            self.prog_data["rotate_seconds"] = int(self.s_prog_rot.value())
        except (TypeError, ValueError):
            pass
        self.prog_data["image_fit"] = self.c_prog_fit.currentText()
        col = self.e_prog_colour.text().strip()
        self.prog_data.pop("colour", None)
        self.prog_data.pop("color", None)
        if col:
            self.prog_data["colour"] = col
        self._prog_mark_dirty()

    def _prog_screen_edited(self, *_a):
        if self._prog_loading:
            return
        lst, entry = self._prog_selected_entry()
        if lst is None or entry is None:
            return
        if not isinstance(entry, dict):
            if self.s_prog_secs.value() == 0 and \
                    self.c_prog_screen_fit.currentText() == "inherit" \
                    and not self.e_prog_screen_colour.text().strip():
                return
            entry = {"image": _screen_image(entry)}
            lst[self.prog_screen] = entry
        secs = int(self.s_prog_secs.value())
        if secs > 0:
            entry["seconds"] = secs
        else:
            entry.pop("seconds", None)
        fit = self.c_prog_screen_fit.currentText()
        if fit in ("fit", "fill", "stretch"):
            entry["fit"] = fit
        else:
            entry.pop("fit", None)
        col = self.e_prog_screen_colour.text().strip()
        entry.pop("colour", None)
        entry.pop("color", None)
        if col:
            entry["colour"] = col
        self._prog_mark_dirty()
        self._prog_screens_rebuild()

    def prog_add_program(self):
        if not isinstance(self.prog_data, dict):
            self.status.showMessage("open a program file first")
            return
        name, ok = QInputDialog.getText(
            self, "Add program", "program name (e.g. 401):")
        name = (name or "").strip()
        if not ok or not name:
            return
        progs = self._prog_progs()
        if name in progs:
            QMessageBox.warning(self, "Add program", "name taken")
            return
        progs[name] = {"route": name, "destinations": {}}
        self.prog_name, self.prog_dest, self.prog_screen = name, None, 0
        self._prog_mark_dirty()
        self._prog_refresh_all()

    def prog_delete_program(self):
        progs = self._prog_progs()
        if self.prog_name not in progs:
            return
        if len(progs) <= 1:
            QMessageBox.warning(self, "Delete program",
                                "a file needs at least one program")
            return
        if QMessageBox.question(
                self, "Delete program",
                f"Delete program '{self.prog_name}'?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        del progs[self.prog_name]
        self.prog_name = sorted(progs)[0]
        self.prog_dest, self.prog_screen = None, 0
        self._prog_mark_dirty()
        self._prog_refresh_all()

    def prog_add_dest(self):
        prog = self._prog_current()
        if prog is None:
            self.status.showMessage("pick a program first")
            return
        name, ok = QInputDialog.getText(
            self, "Add destination", "destination name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        d = prog.get("destinations")
        if isinstance(d, dict):
            if name in d:
                QMessageBox.warning(self, "Add destination",
                                    "name taken")
                return
            d[name] = []
        elif isinstance(d, list):
            if name not in [str(x) for x in d]:
                d.append(name)
        else:
            prog["destinations"] = {name: []}
        self.prog_dest, self.prog_screen = name, 0
        self._prog_mark_dirty()
        self._prog_refresh_all()

    def prog_delete_dest(self):
        prog = self._prog_current()
        if prog is None or not self.prog_dest:
            return
        if QMessageBox.question(
                self, "Delete destination",
                f"Delete destination '{self.prog_dest}'?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        d = prog.get("destinations")
        if isinstance(d, dict):
            d.pop(self.prog_dest, None)
        elif isinstance(d, list):
            prog["destinations"] = [x for x in d
                                    if str(x) != self.prog_dest]
        self.prog_dest, self.prog_screen = None, 0
        self._prog_mark_dirty()
        self._prog_refresh_all()

    def prog_move(self, direction):
        prog = self._prog_current()
        if prog is None:
            return
        lst = self._prog_entry_list(prog, self.prog_dest)
        j = self.prog_screen + direction
        if not (0 <= self.prog_screen < len(lst) and
                0 <= j < len(lst)):
            return
        lst[self.prog_screen], lst[j] = lst[j], lst[self.prog_screen]
        self.prog_screen = j
        self._prog_mark_dirty()
        self._prog_screens_rebuild()

    def prog_delete_screen(self):
        prog = self._prog_current()
        if prog is None:
            return
        lst = self._prog_entry_list(prog, self.prog_dest)
        if not 0 <= self.prog_screen < len(lst):
            return
        del lst[self.prog_screen]
        self.prog_screen = max(0, self.prog_screen - 1)
        self._prog_mark_dirty()
        self._prog_screens_rebuild()

    def _prog_images_rebuild(self, *_a):
        self.prog_images.blockSignals(True)
        try:
            self.prog_images.clear()
            q = self.e_prog_imgfilter.text().strip().lower()
            out = []
            base = os.path.join(THIS_DIR, "bitmap")
            for root, _ds, files in os.walk(base):
                for f in sorted(files):
                    if not f.lower().endswith(".png"):
                        continue
                    rel = os.path.relpath(os.path.join(root, f),
                                          THIS_DIR)
                    if q and q not in rel.lower():
                        continue
                    out.append(rel)
            for rel in sorted(out)[:150]:
                item = QListWidgetItem(rel)
                icon = self._prog_thumb(rel)
                if icon is not None:
                    item.setIcon(icon)
                self.prog_images.addItem(item)
            if len(out) > 150:
                self.prog_images.addItem(
                    f"… {len(out) - 150} more (refine the filter)")
        finally:
            self.prog_images.blockSignals(False)

    def prog_add_image(self):
        prog = self._prog_current()
        if prog is None:
            self.status.showMessage("pick a program first")
            return
        item = self.prog_images.currentItem()
        if item is None or item.text().startswith("… "):
            self.status.showMessage("pick an image first")
            return
        rel = item.text()
        if self.prog_dest is None:
            names = self._prog_dest_names(prog)
            if not names:
                self.status.showMessage("add a destination first")
                return
            self.prog_dest = names[0]
        lst = self._prog_entry_list(prog, self.prog_dest)
        if rel in [_screen_image(s) for s in lst]:
            self.status.showMessage("image already listed")
            return
        lst.append(rel)
        self.prog_screen = len(lst) - 1
        self._prog_mark_dirty()
        self._prog_screens_rebuild()
        self.status.showMessage(f"added to {self.prog_dest}")

    def prog_new_file(self):
        name, ok = QInputDialog.getText(
            self, "New program file", "programs/name.json:")
        name = (name or "").strip()
        if not ok or not name:
            return
        if not name.lower().endswith(".json"):
            name += ".json"
        full = os.path.normpath(os.path.join(THIS_DIR, "programs",
                                             os.path.basename(name)))
        if os.path.exists(full):
            QMessageBox.warning(self, "New program file",
                                "that file already exists")
            return
        data = {"rotate_seconds": 5, "image_fit": "fit",
                "colour": "#ff8000", "programs": {}}
        try:
            with open(full, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            programs_model.load_programs_file(full)
        except (OSError, SystemExit) as e:
            QMessageBox.warning(self, "New program file", str(e))
            return
        self._prog_loading = True
        try:
            self.c_prog_file.clear()
            self.c_prog_file.addItems(program_files())
            self.c_prog_file.setCurrentText(
                os.path.relpath(full, THIS_DIR))
        finally:
            self._prog_loading = False
        self._prog_load_file(os.path.relpath(full, THIS_DIR))

    def prog_save(self):
        if self.prog_file is None or not isinstance(self.prog_data,
                                                     dict):
            self.status.showMessage("nothing to save -- open a file first")
            return False
        if not self.prog_dirty:
            self.status.showMessage("program file already saved")
            return True
        tmp = self.prog_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.prog_data, f, indent=2)
                f.write("\n")
            programs_model.load_programs_file(tmp)
        except SystemExit as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.l_prog_msg.setText(f"save rejected: {e}")
            return False
        except OSError as e:
            self.l_prog_msg.setText(f"save failed: {e}")
            return False
        os.replace(tmp, self.prog_file)
        self.prog_dirty = False
        rel = os.path.relpath(self.prog_file, THIS_DIR)
        names = len(self._prog_progs())
        self.l_prog_msg.setText(f"{rel} · saved · {names} programs")
        self.status.showMessage(f"saved {rel}")
        return True

    def prog_preview_screen(self):
        _lst, entry = self._prog_selected_entry()
        if entry is None:
            return
        rel = _screen_image(entry)
        full = os.path.normpath(os.path.join(THIS_DIR, str(rel or "")))
        dlg = QDialog(self)
        dlg.setWindowTitle(str(rel or "screen"))
        lab = QLabel()
        pm = QPixmap(full)
        if pm.isNull():
            lab.setText(f"missing file:\n{rel}")
        else:
            lab.setPixmap(pm.scaled(
                QSize(480, 80), Qt.IgnoreAspectRatio,
                Qt.FastTransformation))
        box = QVBoxLayout(dlg)
        box.addWidget(lab)
        box.addWidget(QLabel(self._prog_screen_text(
            self.prog_screen, entry)))
        dlg.exec()

    def prog_push_to_screen(self):
        if not self.prog_name:
            self.status.showMessage("pick a program first")
            return False
        prog = self._prog_current() or {}
        dest = self.prog_dest
        if dest not in self._prog_dest_names(prog):
            dest = None
        try:
            tmp = CONTROL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"program": self.prog_name,
                           "destination": dest}, f)
            os.replace(tmp, CONTROL_FILE)
        except OSError as e:
            self.status.showMessage(f"cannot write control file: {e}")
            return False
        self.status.showMessage(
            f"screen following {self.prog_name}/"
            f"{dest or 'all'} -- matrix must be running program.py")
        return True

    def _nudge_targets(self):
        t = self.c_move.currentText()
        if t == "Route":
            return ["route"]
        if t == "Dest":
            return ["dest"]
        if t == "Via":
            return ["via"]
        return ["route", "dest", "via"]

    def nudge(self, dx=0, dy=0, reset=False):
        """Shift text 1px (pad buttons auto-repeat while held; arrow
        keys work when the canvas is focused). Acts on the move target
        (all fields, or just route/dest/via). Works with no message
        selected by starting a working page from the editor."""
        if not self.pages:
            self.pages = [self._snapshot_page()]
            self.page_idx = 0
            self._rebuild_pages()
        if not 0 <= self.page_idx < len(self.pages):
            return
        page = self.pages[self.page_idx]
        offs = _norm_offsets(page)
        for name in self._nudge_targets():
            if reset:
                offs[name] = [0, 0]
            else:
                offs[name] = [_clamp_nudge(offs[name][0] + dx),
                              _clamp_nudge(offs[name][1] + dy)]
        page["offsets"] = offs
        self._update_nudge_label()
        self.refresh()

    def _update_nudge_label(self):
        if 0 <= self.page_idx < len(self.pages):
            offs = _norm_offsets(self.pages[self.page_idx])
            vals = [tuple(offs[n]) for n in self._nudge_targets()]
            tag = self.c_move.currentText()
        else:
            vals, tag = [(0, 0)], "All"
        if len(set(vals)) == 1:
            self.b_nudge0.setText(f"reset {tag} {vals[0]}")
        else:
            self.b_nudge0.setText(f"reset {tag} (mixed)")

    def _show_routing(self):
        r = self.program_routing
        if r.get("program"):
            self.routing_label.setText(
                f"{r.get('file', '?')} › {r['program']} › "
                f"{r.get('destination') or 'all'}")
        else:
            self.routing_label.setText("not sent anywhere yet")

    def push_to_screen(self):
        """Tell a running matrix to show this message's program pick.

        Writes program_control.json like the portal does; any
        `program.py` matrix run following the control file switches to
        it within about a second.
        """
        r = self.program_routing
        if not r.get("program"):
            self.status.showMessage(
                "nothing to show yet -- Send to program first so this "
                "message has a program + destination")
            return False
        dest = r.get("destination") or None
        payload = {"program": r["program"], "destination": dest}
        wrote = []
        targets = [CONTROL_FILE,
                   os.path.join(THIS_DIR, "bus", "program_control.json")]
        try:
            for target in dict.fromkeys(targets):
                tmp = target + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f)
                os.replace(tmp, target)
                wrote.append(target)
        except OSError as e:
            self.status.showMessage(f"cannot write control file: {e}")
            return False
        self.status.showMessage(
            f"screen following {r['program']}/"
            f"{dest or 'all'} -- matrix must be running program.py")
        return True

    def hover_pixel(self, p):
        self.coord_label.setText(
            f"x {p[0]}, y {p[1]}" if p else "x -, y -")

    # -- messages ----------------------------------------------------------
    def _reload_list(self):
        self.listbox.clear()
        for m in self.messages:
            self.listbox.addItem(
                m.get("name") or
                f"{m.get('route', '')} {m.get('destination', '')}".strip())

    def _on_select(self, row):
        if not 0 <= row < len(self.messages):
            return
        m = self.messages[row]
        self.pages = pages_from_message(m)
        self.page_idx = 0
        prog = m.get("program")
        self.program_routing = dict(prog) if isinstance(prog, dict) \
            else {}
        self._show_routing()
        self._rebuild_pages()
        self._load_page(self.pages[0])

    def _current_message(self, single=False):
        if single or not self.pages:
            pages = [self._snapshot_page()]
        else:
            self._flush_page()
            pages = [dict(p) for p in self.pages]
        cur = pages[self.page_idx] \
            if 0 <= self.page_idx < len(pages) else pages[0]
        flat = lambda s: str(s or "").replace("\n", " / ").strip()
        route = flat(cur.get("route", ""))
        dest = flat(cur.get("destination", ""))
        msg = {
            "name": f"{route} {dest}".strip() or "(empty)",
            "route": route, "destination": dest,
            "via": cur.get("via", ""), "style": cur.get("style", "top"),
            "fsrc": cur.get("fsrc", "bdf"),
            "route_font": cur.get("route_font", "10x20.bdf"),
            "dest_font": cur.get("dest_font", "10x20.bdf"),
            "via_font": cur.get("via_font", "6x13B.bdf"),
            "route_scale": cur.get("route_scale", 2),
            "dest_scale": cur.get("dest_scale", 1),
            "via_scale": cur.get("via_scale", 1),
            "sys": cur.get("sys", {}),
            "touch": cur.get("touch", {"add": [], "del": []}),
            "program": dict(self.program_routing),
            "pages": pages,
        }
        return msg

    def msg_new(self):
        self.messages.append(self._current_message(single=True))
        save_messages(self.messages)
        self._reload_list()
        self.listbox.setCurrentRow(len(self.messages) - 1)
        self.status.showMessage(f"message {len(self.messages)} added")

    def msg_update(self):
        row = self.listbox.currentRow()
        if row < 0:
            self.status.showMessage("select a message first")
            return
        self.messages[row] = self._current_message()
        save_messages(self.messages)
        self._reload_list()
        self.listbox.setCurrentRow(row)

    def msg_delete(self):
        row = self.listbox.currentRow()
        if row < 0:
            return
        del self.messages[row]
        if not self.messages:
            self.messages = [self._current_message()]
        save_messages(self.messages)
        self._reload_list()

    # -- pages -----------------------------------------------------------
    def _snapshot_page(self):
        sys_spec = {}
        for name, combo, spin, bold, default in (
                ("route", self.c_srf, self.s_srp, self.b_srb, 34),
                ("dest", self.c_sfd, self.s_sdp, self.b_sdb, 20),
                ("via", self.c_sfv, self.s_svp, self.b_svb, 12)):
            sys_spec[name] = {"family": combo.currentText().strip(),
                              "px": spin.value(),
                              "bold": bold.isChecked()}
        # case/prefix rules live on the page (no editor UI): keep the
        # current page's values so editing never flips them; fresh
        # pages default to off (type casing manually)
        cur = self.pages[self.page_idx] \
            if 0 <= self.page_idx < len(self.pages) else {}
        return normalize_page({
            "route": self.e_route.text(), "destination": self.e_dest.toPlainText(),
            "via": self.e_via.toPlainText(), "style": self.style(),
            "fsrc": self.fsrc(),
            "route_font": self.c_rf.currentText(),
            "dest_font": self.c_df.currentText(),
            "via_font": self.c_vf.currentText(),
            "route_scale": self.s_rs.value(),
            "dest_scale": self.s_ds.value(),
            "via_scale": self.s_vs.value(),
            "upper_dest": cur.get("upper_dest", False),
            "via_prefix": cur.get("via_prefix", False),
            "invert": cur.get("invert", False),
            "offsets": _norm_offsets(cur),
            "sys": sys_spec,
            "touch": {"add": sorted(self.touch_add),
                      "del": sorted(self.touch_del)},
            "seconds": self.s_secs.value(),
        })

    def _load_page(self, page):
        page = normalize_page(page)
        for w in (self.e_route, self.e_dest, self.e_via, self.c_rf,
                  self.c_df, self.c_vf, self.s_rs, self.s_ds, self.s_vs,
                  self.c_srf, self.c_sfd, self.c_sfv, self.s_srp,
                  self.s_sdp, self.s_svp, self.s_secs, self.ck_invert):
            w.blockSignals(True)
        try:
            self.e_route.setText(page["route"])
            self.e_dest.setPlainText(page["destination"])
            self.e_via.setPlainText(page["via"])
            self.style_radios[page["style"]].setChecked(True)
            for combo, k in ((self.c_rf, "route_font"),
                             (self.c_df, "dest_font"),
                             (self.c_vf, "via_font")):
                if page[k] in self.fonts:
                    combo.setCurrentText(page[k])
            self.s_rs.setValue(max(1, min(8, int(page["route_scale"]))))
            self.s_ds.setValue(max(1, min(8, int(page["dest_scale"]))))
            self.s_vs.setValue(max(1, min(8, int(page["via_scale"]))))
            if sysfonts.PIL_OK and page.get("fsrc") == "sys":
                self.rb_sys.setChecked(True)
            else:
                self.rb_bdf.setChecked(True)
            for combo, spin, bold, k in (
                    (self.c_srf, self.s_srp, self.b_srb, "route"),
                    (self.c_sfd, self.s_sdp, self.b_sdb, "dest"),
                    (self.c_sfv, self.s_svp, self.b_svb, "via")):
                s = page["sys"][k]
                if s["family"] in sysfonts.FACES:
                    combo.setCurrentText(s["family"])
                spin.setValue(s["px"])
                bold.setChecked(s["bold"])
            self.s_secs.setValue(int(page["seconds"] or 0))
            self.ck_invert.setChecked(bool(page["invert"]))
            self.touch_add, self.touch_del = touch_of(
                {"touch": page["touch"]})
        finally:
            for w in (self.e_route, self.e_dest, self.e_via, self.c_rf,
                      self.c_df, self.c_vf, self.s_rs, self.s_ds,
                      self.s_vs, self.c_srf, self.c_sfd, self.c_sfv,
                      self.s_srp, self.s_sdp, self.s_svp, self.s_secs,
                      self.ck_invert):
                w.blockSignals(False)
        self._last_sig = None
        self._update_nudge_label()
        self.refresh()

    def _flush_page(self):
        if 0 <= self.page_idx < len(self.pages):
            self.pages[self.page_idx] = self._snapshot_page()

    def _rebuild_pages(self):
        self.pagelist.blockSignals(True)
        try:
            self.pagelist.clear()
            for i, p in enumerate(self.pages):
                label = (f"{p.get('route', '')} "
                         f"{p.get('destination', '')}").replace(
                             "\n", " / ").strip() or "(blank)"
                self.pagelist.addItem(f"Page {i + 1} -- {label}")
            self.pages_box.setTitle(f"Pages ({len(self.pages)})")
            if 0 <= self.page_idx < len(self.pages):
                self.pagelist.setCurrentRow(self.page_idx)
        finally:
            self.pagelist.blockSignals(False)

    def _select_page(self, row):
        if not 0 <= row < len(self.pages) or row == self.page_idx:
            if 0 <= row < len(self.pages):
                self.page_idx = row
            return
        self._flush_page()
        self.page_idx = row
        self._load_page(self.pages[row])

    def _blank_page(self):
        cur = self._snapshot_page()
        cur.update({"destination": "", "via": "",
                    "touch": {"add": [], "del": []}, "seconds": 0})
        return normalize_page(cur)

    def page_add(self):
        self._flush_page()
        self.pages.append(self._blank_page())
        self.page_idx = len(self.pages) - 1
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_dupe(self):
        self._flush_page()
        self.pages.append(self._snapshot_page())
        self.page_idx = len(self.pages) - 1
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_delete(self):
        if not self.pages:
            return
        del self.pages[self.page_idx]
        if not self.pages:
            self.pages = [self._blank_page()]
        self.page_idx = min(self.page_idx, len(self.pages) - 1)
        self._rebuild_pages()
        self._load_page(self.pages[self.page_idx])

    def page_move(self, direction):
        j = self.page_idx + direction
        if not (0 <= self.page_idx < len(self.pages) and
                0 <= j < len(self.pages)):
            return
        self._flush_page()
        self.pages[self.page_idx], self.pages[j] = \
            self.pages[j], self.pages[self.page_idx]
        self.page_idx = j
        self._rebuild_pages()

    def _secs_changed(self, value):
        if 0 <= self.page_idx < len(self.pages):
            self.pages[self.page_idx]["seconds"] = value

    # -- file / windows ------------------------------------------------------
    def _resolve_path(self):
        rel = self.e_path.text().strip()
        if not rel:
            rel = ENG.default_filename(self.info.get("route", ""),
                                       self.e_dest.toPlainText())
            self.e_path.setText(rel)
        full = ENG.resolve_save_path(rel)
        if full is None:
            self.status.showMessage("path must be a bitmap/*.png file")
        return full

    def save_png(self):
        full = self._resolve_path()
        if full is None:
            return False
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".tmp"
            with open(tmp, "wb") as f:
                f.write(ENG.encode_png(self.frame))
            os.replace(tmp, full)
        except OSError as e:
            self.status.showMessage(str(e))
            return False
        rel = os.path.relpath(full, THIS_DIR)
        self.status.showMessage(
            f"saved {rel} ({self.info.get('lit', 0)} lit pixels)")
        return True

    def save_and_exit(self):
        if self.save_png():
            self.close()

    def _send_names(self):
        """(route, dest) text for PNG filenames: editor first, then
        the first non-blank page."""
        route = self.e_route.text().strip()
        dest = self.e_dest.toPlainText().strip()
        if not route or not dest:
            for p in self.pages:
                if not route and str(p.get("route", "")).strip():
                    route = str(p["route"]).strip()
                if not dest and str(p.get("destination", "")).strip():
                    dest = str(p["destination"]).strip()
                if route and dest:
                    break
        return route, dest

    def send_to_program(self):
        self._flush_page()
        if not self.pages:
            self.pages = [self._snapshot_page()]
            self.page_idx = 0
            self._rebuild_pages()
        if sysfonts.PIL_OK and any(
                str(p.get("fsrc", "bdf")) == "sys" for p in self.pages):
            if not self.ensure_faces():
                self.status.showMessage(
                    "system font scan found nothing -- cannot render "
                    "system-font pages")
                return False
        name_route, name_dest = self._send_names()
        dlg = SendDialog(self, self.program_routing, name_route,
                         name_dest, self.pages, self.fg_hex)
        if dlg.exec() != QDialog.Accepted:
            return False
        routing = dlg.routing()
        try:
            summary = send_pages_to_program(
                self.pages, self.fg_hex, False,
                name_route, name_dest, routing)
        except ValueError as e:
            self.status.showMessage(f"send failed: {e}")
            return False
        self.program_routing = {k: routing[k] for k in
                                ("file", "program", "destination",
                                 "route")}
        self._show_routing()
        row = self.listbox.currentRow()
        if 0 <= row < len(self.messages):
            self.messages[row]["program"] = dict(self.program_routing)
            save_messages(self.messages)
        skip = (f" (+{summary['skipped']} blank skipped)"
                if summary["skipped"] else "")
        dup = summary["added"] < len(summary["paths"])
        note = " (already-listed images skipped)" if dup else ""
        new = " (new file created)" if summary.get("created_file") else ""
        self.status.showMessage(
            f"sent {len(summary['paths'])} page(s) to "
            f"{summary['program']}/{summary['destination']} in "
            f"{summary['file']}{skip}{note}{new}")
        return True

    def open_preview(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Preview 1:1")
        lab = QLabel()
        lab.setPixmap(QPixmap.fromImage(self._frame_image()))
        box = QVBoxLayout(dlg)
        box.addWidget(lab)
        box.addWidget(QLabel("exact panel pixels"))
        dlg.exec()

    def about(self):
        QMessageBox.about(
            self, "About Sign Studio",
            "Sign Studio -- 240x40 LED toolchain.\n\n"
            "Sign editor: route + destination + via text, bitmap or "
            "system fonts, hand touch-ups, Save PNG.\n\n"
            "Program editor: programs/*.json with destinations, screens "
            "and Show on screen.\n\n"
            "BDF editor: paint bitmap font glyphs and metrics.\n\n"
            "Bitmap pixels only -- no antialiasing.")

    def closeEvent(self, event):
        event.accept()


def smoke():
    """Offscreen self-test: prints results, returns exit code."""
    print("faces...", flush=True)
    t0 = time.time()
    found = sysfonts.scan_system_fonts()
    sysfonts.FACES.update(found)
    print(f"  {len(found)} families in {time.time() - t0:.1f}s",
          flush=True)
    w = Studio()
    fails = []

    def check(name, cond, extra=""):
        print(f"  {'ok' if cond else 'FAIL'} {name} {extra}", flush=True)
        if not cond:
            fails.append(name)

    check("bdf render", w.info.get("lit", 0) > 0,
          f"lit={w.info.get('lit')}")
    for s in STYLES:
        w.style_radios[s].setChecked(True)
        w.refresh()
        check(f"style {s}", w.info.get("lit", 0) > 0)
    w.style_radios["top"].setChecked(True)
    w.rb_sys.setChecked(True)
    w.refresh()
    check("sys render", w.info.get("mode") == "system"
          and w.info.get("lit", 0) > 0, f"lit={w.info.get('lit')}")
    colours = {(w.frame[i], w.frame[i + 1], w.frame[i + 2])
               for i in range(0, len(w.frame), 3)}
    check("two colours only", len(colours) == 2, f"{colours}")
    # pixel tool: paint then erase the same pixel, plus a brush-2 dab
    w.rb_bdf.setChecked(True)
    w.refresh()
    lit0 = w.info["lit"]
    w.paint_stroke([(5, 5)], erase=False)
    check("paint adds", (5, 5) in w.touch_add
          and w.info["lit"] >= lit0)
    w.paint_stroke([(5, 5)], erase=True)
    check("erase removes", (5, 5) not in w.touch_add
          and (5, 5) in w.touch_del)
    w.s_brush.setValue(2)
    w.paint_stroke([(10, 10)], erase=False)
    check("brush 2x2", len(w.touch_add) == 4, f"{sorted(w.touch_add)}")
    # coordinate mapping incl. clipping (at explicit 3x: fit varies)
    w.c_zoom.setCurrentText("3")
    check("map inside", w.canvas.pixel_from_pos(QPoint(3 * 10 + 1,
                                                        3 * 20 + 2))
          == (10, 20))
    check("map clipped", w.canvas.pixel_from_pos(QPoint(-5, 999)) is None)
    # save + decode (clean up our own artifact afterwards)
    w.e_path.setText("bitmap/destinations/custom/qt-smoke.png")
    check("save", w.save_png())
    try:
        from images import decode_png
        sw, sh, _ = decode_png(
            "bitmap/destinations/custom/qt-smoke.png")
        check("png 240x40", (sw, sh) == (240, 40))
    except SystemExit as e:
        check("png 240x40", False, str(e))
    finally:
        try:
            os.remove("bitmap/destinations/custom/qt-smoke.png")
        except OSError:
            pass
    # messages round-trip (backup + restore the real file)
    bak = None
    if os.path.exists(MSG_FILE):
        with open(MSG_FILE) as f:
            bak = f.read()
    try:
        n0 = len(w.messages)
        w.msg_new()
        check("msg new", len(w.messages) == n0 + 1)
        w.listbox.setCurrentRow(n0)
        w.msg_update()
        w.listbox.setCurrentRow(0)
        w.msg_delete()
        check("msg update/delete", len(w.messages) == n0)
        disk = json.load(open(MSG_FILE))
        check("msg persisted", len(disk) == n0)
    finally:
        if bak is None:
            if os.path.exists(MSG_FILE):
                os.remove(MSG_FILE)
        else:
            with open(MSG_FILE, "w") as f:
                f.write(bak)
    # BDF editor: load, paint, metrics, round-trip via a temp copy
    try:
        import tempfile
        check("bdf loaded", w.bdf_doc is not None
              and len(w.bdf_doc.glyphs) > 0,
              f"{w.c_bdffont.currentText()}")
        g = w.bdf_glyph()
        check("bdf glyph selected", g is not None)
        if g is not None:
            lit_before = g.lit_count()
            w.bdf_paint([(0, 0)], erase=False)
            check("bdf paint", g.pixel(0, 0),
                  f"lit={lit_before}->{g.lit_count()}")
            w.bdf_paint([(0, 0)], erase=True)
            check("bdf erase", not g.pixel(0, 0))
            w0, h0 = g.w, g.h
            g.resize(min(64, w0 + 1), h0)
            check("bdf resize", g.w == min(64, w0 + 1))
            g.resize(w0, h0)  # restore size for the save test
            with tempfile.NamedTemporaryFile(
                    suffix=".bdf", delete=False) as tf:
                tmp_bdf = tf.name
            try:
                w.bdf_doc.save(tmp_bdf)
                reloaded = BdfDoc.load(tmp_bdf)
                check("bdf round-trip",
                      len(reloaded.glyphs) == len(w.bdf_doc.glyphs))
                again = reloaded.find(g.enc)
                check("bdf glyph survives",
                      again is not None and again.w == g.w
                      and again.h == g.h)
            finally:
                try:
                    os.remove(tmp_bdf)
                except OSError:
                    pass
        # glyph canvas mapping (pad margin of one zoomed cell)
        w.s_bdfzoom.setValue(12)
        gg = w.bdf_glyph()
        if gg is not None:
            z = 12
            check("bdf map inside",
                  w.bdf_canvas.glyph_pixel_from_pos(
                      QPoint(z * 1 + z + 1, z * 0 + z + 1)) == (1, 0))
            check("bdf map clipped",
                  w.bdf_canvas.glyph_pixel_from_pos(QPoint(0, 0))
                  is None)
    except Exception as e:
        check("bdf editor", False, f"{e!r}")
    # modes: switch through all three, toolbar follows
    try:
        for m in ("program", "bdf", "sign"):
            w.set_mode(m)
            check(f"mode {m}",
                  w.center_stack.currentIndex()
                  == {"sign": 0, "program": 1, "bdf": 2}[m]
                  and w.right_stack.currentIndex()
                  == {"sign": 0, "program": 1, "bdf": 2}[m])
        check("program loaded", w.prog_data is not None
              and w.prog_name is not None,
              f"{w.prog_file}")
        check("program screens", w.prog_screens.count() >= 0,
              f"n={w.prog_screens.count()}")
        w.set_mode("sign")
    except Exception as e:
        check("modes", False, f"{e!r}")
    print("SMOKE " + ("OK" if not fails else f"FAILED: {fails}"),
          flush=True)
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Sign Studio (Qt)")
    ap.add_argument("--smoke", action="store_true",
                    help="Offscreen self-test, prints results, exits")
    args = ap.parse_args()
    app = QApplication(sys.argv)
    if args.smoke:
        sys.exit(smoke())
    w = Studio()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
