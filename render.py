#!/usr/bin/env python3
"""BDF-only text renderer: 240x40 blinds from typed text, exact pixels.

Fonts come ONLY from the local `fonts/` folder (BDF bitmap fonts, never
antialiased). There is deliberately no system-font / Pillow path: if a
font is not in `fonts/`, rendering fails with a plain error instead of
silently substituting something else.

Job shape (all keys optional except at least one of route/dest):

    {"route": "43", "dest": "Sheffield", "via": "Dronfield",
     "style": "top",                       # top|bottom|left|right
     "route_font": "10x20.bdf", "dest_font": "10x20.bdf",
     "via_font": "6x13B.bdf",
     "route_scale": 2, "dest_scale": 1, "via_scale": 1,
     "fg": "#DB9600",                      # single #rrggbb colour
     "gap": 4, "pad": 1}

Text is literal: no case changes, no "via " prefix. Type capitals and
prefixes yourself if you want them.

Layouts (route number is the tall block on the right):
  top      via over dest, stacked on the left
  bottom   dest over via, stacked on the left
  left     via | dest side by side, num on the right
  right    dest | via side by side, num on the right

Stdlib only. Used by portal.py (preview + create-from-text).
"""

import os

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
FONT_DIR = os.path.join(THIS_DIR, "fonts")

W, H = 240, 40
STYLES = ("top", "bottom", "left", "right")

DEFAULTS = {
    "route_font": "10x20.bdf",
    "dest_font": "10x20.bdf",
    "via_font": "6x13B.bdf",
    "route_scale": 2,
    "dest_scale": 1,
    "via_scale": 1,
    "fg": "#DB9600",
    "gap": 4,
    "pad": 1,
    "via_fraction": 0.5,
}


# ---------------------------------------------------------------------------
# BDF bitmap fonts (exact pixels, never antialiased). fonts/ only.
# ---------------------------------------------------------------------------

class BDF:
    """Minimal BDF rasterizer: glyph bitmaps + DWIDTH advances."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.glyphs = {}
        self.bbox_w = 0
        self.bbox_h = 0
        self.space_w = 0
        self._parse()

    def _parse(self):
        enc = dwidth = bbx = None
        bitmap = None
        with open(self.path, errors="replace") as f:
            for line in f:
                p = line.split()
                if not p:
                    continue
                tag = p[0]
                if tag == "FONTBOUNDINGBOX" and len(p) >= 3:
                    try:
                        self.bbox_w = int(p[1])
                        self.bbox_h = int(p[2])
                    except ValueError:
                        pass
                elif tag == "ENCODING" and len(p) >= 2:
                    try:
                        enc = int(p[1])
                    except ValueError:
                        enc = None
                    dwidth, bbx, bitmap = None, None, None
                elif tag == "DWIDTH" and len(p) >= 2:
                    try:
                        dwidth = int(p[1])
                    except ValueError:
                        dwidth = None
                elif tag == "BBX" and len(p) >= 5:
                    try:
                        bbx = tuple(int(x) for x in p[1:5])
                    except ValueError:
                        bbx = None
                elif tag == "BITMAP":
                    bitmap = []
                elif tag == "ENDCHAR":
                    if enc is not None and enc >= 0 and dwidth is not None \
                            and bbx is not None and bitmap is not None:
                        w, h, xo, yo = bbx
                        rows = []
                        for hexrow in bitmap[:h]:
                            hexrow = hexrow.strip()
                            try:
                                val = int(hexrow or "0", 16)
                            except ValueError:
                                val = 0
                            total = len(hexrow or "0") * 4
                            if total >= w:
                                val >>= total - w
                            else:
                                val <<= w - total
                            rows.append(val & ((1 << w) - 1) if w else 0)
                        while len(rows) < h:
                            rows.append(0)
                        self.glyphs[enc] = (dwidth, w, h, xo, yo, rows)
                    enc, dwidth, bbx, bitmap = None, None, None, None
                elif bitmap is not None:
                    bitmap.append(line.strip())
        sp = self.glyphs.get(32)
        self.space_w = sp[0] if sp else (self.bbox_w or 4)

    def glyph(self, ch):
        return self.glyphs.get(ord(ch))

    def advance(self, ch):
        g = self.glyphs.get(ord(ch))
        return g[0] if g is not None else self.space_w

    def text_advance(self, text):
        return sum(self.advance(c) for c in text)


_FONTS = {}


def _safe_name(name):
    """Basename only: no paths, no traversal out of fonts/."""
    base = os.path.basename(str(name or ""))
    if not base.lower().endswith(".bdf") or base in (".bdf",):
        raise ValueError(f"not a BDF font: {name!r}")
    return base


def get_font(name):
    """Load a BDF font from fonts/ only. Raises ValueError otherwise."""
    base = _safe_name(name or DEFAULTS["route_font"])
    if base not in _FONTS:
        path = os.path.join(FONT_DIR, base)
        if not os.path.isfile(path):
            raise ValueError(
                f"unknown font '{base}' (fonts/ only: "
                f"{', '.join(available_fonts()) or 'empty'})")
        _FONTS[base] = BDF(path)
    return _FONTS[base]


def available_fonts():
    try:
        return sorted(f for f in os.listdir(FONT_DIR)
                      if f.endswith(".bdf"))
    except OSError:
        return []


def text_ink(font, text, scale):
    scale = max(1, int(scale))
    adv = font.text_advance(text) * scale
    x0 = y0 = None
    x1 = y1 = None
    ox = 0
    for ch in text:
        g = font.glyph(ch)
        dw = font.advance(ch) * scale
        if g is not None:
            _, w, h, xoff, yoff, rows = g
            top = -(yoff + h) * scale
            left = ox + xoff * scale
            for r in range(h):
                bits = rows[r] if r < len(rows) else 0
                for c in range(w):
                    if bits >> (w - 1 - c) & 1:
                        px0, py0 = left + c * scale, top + r * scale
                        px1, py1 = px0 + scale - 1, py0 + scale - 1
                        if x0 is None:
                            x0, y0, x1, y1 = px0, py0, px1, py1
                        else:
                            x0 = min(x0, px0)
                            y0 = min(y0, py0)
                            x1 = max(x1, px1)
                            y1 = max(y1, py1)
        ox += dw
    return adv, x0, y0, x1, y1


def stamp(frame, font, text, ox, baseline, scale, fg):
    scale = max(1, int(scale))
    fr, fg_, fb = fg
    lit = 0
    missing = []
    x = ox
    for ch in text:
        g = font.glyph(ch)
        dw = font.advance(ch) * scale
        if g is None:
            if ch != " " and ch not in missing:
                missing.append(ch)
            x += dw
            continue
        _, w, h, xoff, yoff, rows = g
        top = baseline - (yoff + h) * scale
        left = x + xoff * scale
        for r in range(h):
            bits = rows[r] if r < len(rows) else 0
            for c in range(w):
                if bits >> (w - 1 - c) & 1:
                    for dy in range(scale):
                        yy = top + r * scale + dy
                        if yy < 0 or yy >= H:
                            continue
                        for dx in range(scale):
                            xx = left + c * scale + dx
                            if xx < 0 or xx >= W:
                                continue
                            o = (yy * W + xx) * 3
                            frame[o], frame[o + 1], frame[o + 2] = fr, fg_, fb
                            lit += 1
        x += dw
    return lit, missing


# ---------------------------------------------------------------------------
# Colours (single shade only; "full"/rainbow make no sense for text)
# ---------------------------------------------------------------------------

def parse_colour(v, fallback=(219, 150, 0)):
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return fallback
    s = str(v).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            pass
    raise ValueError(f"colour must be #rrggbb, got {v!r}")


def colour_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


# ---------------------------------------------------------------------------
# Text rules + layout
# ---------------------------------------------------------------------------

def normalize(job):
    """Literal text rules: strip edge whitespace, nothing else.

    No case changes, no "via " prefix - type exactly what should light
    up, including capitals and prefixes.
    """
    route = str(job.get("route", "") or "")
    dest = str(job.get("dest", job.get("destination", "")) or "")
    via = str(job.get("via", "") or "")
    return route.strip(), dest.strip(), via.strip()


def place_centered(cell, adv, ink):
    cx0, cy0, cx1, cy1 = cell
    ccx = (cx0 + cx1) // 2
    ccy = (cy0 + cy1) // 2
    ix0, iy0, ix1, iy1 = ink
    if ix0 is None:
        return cx0 + max(0, (cx1 - cx0 + 1 - adv) // 2), ccy
    ox = ccx - (ix0 + ix1 + 1) // 2
    baseline = ccy - (iy0 + iy1 + 1) // 2
    return ox, baseline


def field_advance(font, text, scale):
    if "\n" not in text:
        return font.text_advance(text) * max(1, int(scale))
    return max([font.text_advance(ln) for ln in text.split("\n")]
               + [0]) * max(1, int(scale))


def stamp_multiline(frame, font, text, cell, scale, fg, warnings, name,
                    dx=0, dy=0):
    scale = max(1, int(scale))
    lines = text.split("\n")
    pitch = (font.bbox_h or 20) * scale
    ccx = (cell[0] + cell[2]) // 2
    ccy = (cell[1] + cell[3]) // 2
    top = ccy - len(lines) * pitch // 2
    lit_total = 0
    missing = []
    first_origin = None
    tall_warned = False
    last_base = None
    for i, ln in enumerate(lines):
        _adv, ix0, iy0, ix1, iy1 = text_ink(font, ln, scale)
        adv = font.text_advance(ln) * scale
        wide = adv - (cell[2] - cell[0] + 1)
        if wide > 0:
            warnings.append(f"{name} too wide by {wide}px in this style")
        if ix0 is None:
            ox = cell[0] + max(0, (cell[2] - cell[0] + 1 - adv) // 2)
            base = last_base + pitch if last_base is not None \
                else top + pitch
        else:
            ox = ccx - (ix0 + ix1 + 1) // 2 + dx
            base = top + i * pitch - iy0 + dy
            if first_origin is None:
                first_origin = (ox, base)
            if not tall_warned and (base + iy0 < 0 or base + iy1 >= H):
                warnings.append(f"{name} taller than the 40px panel")
                tall_warned = True
        lit, miss = stamp(frame, font, ln, ox, base, scale, fg)
        lit_total += lit
        for m in miss:
            if m not in missing:
                missing.append(m)
        last_base = base
    if first_origin is None:
        first_origin = (cell[0], ccy)
    return lit_total, missing, first_origin


def job_geometry(job):
    try:
        gap = int(job.get("gap", DEFAULTS["gap"]))
    except (TypeError, ValueError):
        gap = DEFAULTS["gap"]
    try:
        pad = int(job.get("pad", DEFAULTS["pad"]))
    except (TypeError, ValueError):
        pad = DEFAULTS["pad"]
    try:
        vfract = float(job.get("via_fraction", DEFAULTS["via_fraction"]))
    except (TypeError, ValueError):
        vfract = DEFAULTS["via_fraction"]
    return (max(0, min(20, gap)), max(0, min(8, pad)),
            min(0.75, max(0.25, vfract)))


def layout_cells(route, dest, via, r_adv, d_adv, v_adv, style,
                 gap, pad, vfract):
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    route_cell = None
    route_over = 0
    if route:
        rx1 = W - 1 - pad
        rx0 = rx1 - r_adv + 1
        if rx0 < pad:
            route_over = pad - rx0
            rx0 = pad
        route_cell = (rx0, 0, rx1, H - 1)
        left = (pad, 0, max(pad, rx0 - gap - 1), H - 1)
    else:
        left = (pad, 0, W - 1 - pad, H - 1)

    jobs = []
    if style in ("top", "bottom"):
        mid = H // 2
        top_cell = (left[0], 0, left[2], mid - 1)
        bot_cell = (left[0], mid, left[2], H - 1)
        if style == "top":
            first, second = ("via", top_cell), ("dest", bot_cell)
        else:
            first, second = ("dest", top_cell), ("via", bot_cell)
        if second[0] == "dest" and dest:
            jobs.append((second[0], second[1]))
        if second[0] == "via" and via:
            jobs.append((second[0], second[1]))
        if first[0] == "dest" and dest:
            jobs.append((first[0], first[1]))
        if first[0] == "via" and via:
            jobs.append((first[0], first[1]))
        if dest and not via:
            jobs = [("dest", left)]
        if via and not dest:
            jobs = [("via", left)]
    else:
        avail = left[2] - left[0] + 1
        if style == "left":
            first, second = "via", "dest"
        else:
            first, second = "dest", "via"
        if dest and via:
            cut = left[0] + int(avail * vfract) - 1
            first_cell = (left[0], 0, cut - gap // 2, H - 1)
            second_cell = (cut + gap // 2 + 1, 0, left[2], H - 1)
            jobs.append((first, first_cell))
            jobs.append((second, second_cell))
        elif dest:
            jobs.append(("dest", left))
        elif via:
            jobs.append(("via", left))
    return route_cell, jobs, route_over


def render(job):
    """Render one 240x40 blind from BDF fonts. Returns (frame, info)."""
    style = str(job.get("style", "top")).lower()
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    route, dest, via = normalize(job)
    if not (route or dest):
        raise ValueError("type a route number and/or a destination")
    rf = get_font(job.get("route_font", DEFAULTS["route_font"]))
    df = get_font(job.get("dest_font", DEFAULTS["dest_font"]))
    vf = get_font(job.get("via_font", DEFAULTS["via_font"]))
    rs = max(1, int(job.get("route_scale", DEFAULTS["route_scale"])))
    ds = max(1, int(job.get("dest_scale", DEFAULTS["dest_scale"])))
    vs = max(1, int(job.get("via_scale", DEFAULTS["via_scale"])))
    fg = parse_colour(job.get("fg", job.get("colour", DEFAULTS["fg"])))
    gap, pad, vfract = job_geometry(job)

    frame = bytearray(W * H * 3)
    warnings = []
    fields = {}

    def _adv_ink(font, text, scale):
        if not text:
            return 0, (None, None, None, None)
        if "\n" in text:
            return field_advance(font, text, scale), \
                (None, None, None, None)
        _a, _x0, _y0, _x1, _y1 = text_ink(font, text, scale)
        return _a, (_x0, _y0, _x1, _y1)

    r_adv, (r_x0, r_y0, r_x1, r_y1) = _adv_ink(rf, route, rs)
    d_adv, (d_x0, d_y0, d_x1, d_y1) = _adv_ink(df, dest, ds)
    v_adv, (v_x0, v_y0, v_x1, v_y1) = _adv_ink(vf, via, vs)

    route_cell, job_cells, route_over = layout_cells(
        route, dest, via, r_adv, d_adv, v_adv, style, gap, pad, vfract)
    if route_over:
        warnings.append(f"route number too wide by {route_over}px")
    pick = {"route": (rf, route, rs, (r_x0, r_y0, r_x1, r_y1)),
            "dest": (df, dest, ds, (d_x0, d_y0, d_x1, d_y1)),
            "via": (vf, via, vs, (v_x0, v_y0, v_x1, v_y1))}
    adv_of = {"route": r_adv, "dest": d_adv, "via": v_adv}

    missing = set()
    ordered = job_cells + ([("route", route_cell)] if route else [])
    for name, cell in ordered:
        font, text, scale, ink = pick[name]
        adv = adv_of[name]
        if "\n" in text:
            lit, miss, origin = stamp_multiline(
                frame, font, text, cell, scale, fg, warnings, name)
            fields[name] = {"advance": adv, "lit": lit,
                            "cell": list(cell), "origin": list(origin)}
            missing.update(miss)
            continue
        wide = adv - (cell[2] - cell[0] + 1)
        if wide > 0:
            warnings.append(f"{name} too wide by {wide}px in this style")
        ox, baseline = place_centered(cell, adv, ink)
        lit, miss = stamp(frame, font, text, ox, baseline, scale, fg)
        fields[name] = {"advance": adv, "lit": lit,
                        "cell": list(cell), "origin": [ox, baseline]}
        missing.update(miss)
        if ink[0] is not None and (
                baseline + ink[1] < 0 or baseline + ink[3] >= H):
            warnings.append(f"{name} taller than the 40px panel")
    if missing:
        warnings.append("missing glyphs (blank): "
                        + ", ".join(sorted(missing)))

    lit_total = sum(1 for i in range(0, len(frame), 3)
                    if frame[i] or frame[i + 1] or frame[i + 2])
    info = {"w": W, "h": H, "style": style, "route": route,
            "dest": dest, "via": via, "fg": colour_hex(fg),
            "fields": fields, "lit": lit_total,
            "warnings": warnings}
    return frame, info


def encode_png(frame, w=W, h=H):
    """True-colour PNG bytes from an RGB bytearray (stdlib only)."""
    import struct
    import zlib
    rows = [bytes(frame[y * w * 3:(y + 1) * w * 3]) for y in range(h)]

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))
