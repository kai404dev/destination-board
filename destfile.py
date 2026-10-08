#!/usr/bin/env python3
"""Shared `.dest` program file model (stdlib only).

`.dest` format (JSON):

    {
        "defaults": {
            "colour": "#DB9600",
            "rotation_speed": 3,
            "px_width": 240,
            "px_height": 40,
            "text": {
                "route_font": "10x20.bdf",
                "route_scale": 2,
                "dest_font": "10x20.bdf",
                "dest_scale": 1,
                "via_font": "6x13B.bdf",
                "via_scale": 1,
                "style": "top",
                "colour": "#DB9600"
            }
        },
        "services": {
            "<service number>": {
                "<destination>": {
                    "service_code": "001",
                    "service_name": "Service Name",
                    "override": {
                        "colour": "full",
                        "rotation_speed": 3,
                        "px_width": 240,
                        "px_height": 40
                    },
                    "bitmaps": [
                        "bitmaps/<program>/<route>/<route>-<destination>-1.png",
                        "bitmaps/<program>/<route>/<route>-<destination>-2.png"
                    ]
                }
            }
        }
    }

Rules (kept deliberately close to the reference bus/program.py model):

- `defaults.colour` is `#rrggbb` (also accepts `#rgb`). Every
  destination inherits it unless its `override.colour` says otherwise.
- `override.colour = "full"` keeps the bitmap's own colours (no tint).
  Any other `#rrggbb` flattens the page to exactly that shade.
- `rotation_speed` is seconds per page. Cascade:
  `override.rotation_speed` -> `defaults.rotation_speed` -> 3.
- `px_width` / `px_height` describe the bitmap canvas (default 240x40).
  Numbers or numeric strings both accepted (the spec shows strings).
- `services` is `{route: {destination: entry}}`. `service_code` /
  `service_name` are free-form labels (shown in the portal, not on
  the LEDs). `bitmaps` is the ordered page list for that destination.
- Bitmap paths are project-root-relative (`bitmaps/...`).
- `defaults.text` is the text-creator preset (fonts/scales per role,
  layout style, render colour) so every new page starts from the
  house style. All keys optional; missing ones fall back as shown.

This module only reads/validates/resolves - the portal writes files
through `portal.py` so there is one writer with atomic replace.
"""

import json
import os
import re

DEFAULT_COLOUR = "#DB9600"
DEFAULT_ROTATION = 3.0
DEFAULT_PX_W = 240
DEFAULT_PX_H = 40
TEXT_STYLES = ("top", "bottom", "left", "right")
TEXT_DEFAULTS = {
    "route_font": "10x20.bdf",
    "route_scale": 2,
    "dest_font": "10x20.bdf",
    "dest_scale": 1,
    "via_font": "6x13B.bdf",
    "via_scale": 1,
    "style": "top",
    "colour": "#DB9600",
}


def _num(v, fallback):
    """Accept numbers or numeric strings, else fallback."""
    if v is None or v == "":
        return fallback
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _int(v, fallback):
    return int(_num(v, fallback))


def parse_colour(v, where="colour"):
    """Validate a colour value.

    Returns `(r, g, b)` tuple, `"full"`, or raises ValueError.
    `None`/empty means unset (caller falls back to defaults).
    """
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    s = str(v).strip()
    if s.lower() == "full":
        return "full"
    h = s.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        raise ValueError(f"{where}: colour must be #rrggbb or \"full\", "
                         f"got {v!r}")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def colour_str(c):
    if c == "full":
        return "full"
    if c is None:
        return "-"
    return "#%02x%02x%02x" % c


def slug(s, fallback="untitled"):
    return (re.sub(r"[^a-z0-9]+", "-", str(s or "").strip().lower())
            .strip("-") or fallback)


def defaults_of(data):
    """Normalised defaults dict from a loaded .dest document."""
    d = data.get("defaults", {}) if isinstance(data, dict) else {}
    if not isinstance(d, dict):
        d = {}
    try:
        colour = parse_colour(d.get("colour", d.get("color",
                                                    DEFAULT_COLOUR)),
                              "defaults.colour")
    except ValueError:
        colour = parse_colour(DEFAULT_COLOUR)
    if colour is None:
        colour = parse_colour(DEFAULT_COLOUR)
    rot = _num(d.get("rotation_speed", DEFAULT_ROTATION), DEFAULT_ROTATION)
    if rot <= 0:
        rot = DEFAULT_ROTATION
    return {
        "colour": colour,
        "rotation_speed": rot,
        "px_width": _int(d.get("px_width", DEFAULT_PX_W), DEFAULT_PX_W),
        "px_height": _int(d.get("px_height", DEFAULT_PX_H), DEFAULT_PX_H),
    }


def text_defaults_of(data):
    """Normalised text-creator preset from a loaded .dest document.

    Always complete (missing keys fall back to TEXT_DEFAULTS). Font
    names are basenamed (no paths); existence in fonts/ is validated
    by the portal on save, and by the renderer on use.
    """
    d = data.get("defaults", {}) if isinstance(data, dict) else {}
    if not isinstance(d, dict):
        d = {}
    t = d.get("text", {})
    if not isinstance(t, dict):
        t = {}

    def _scale(v, fb):
        try:
            s = int(float(v))
        except (TypeError, ValueError):
            return fb
        return min(4, max(1, s))

    style = str(t.get("style", TEXT_DEFAULTS["style"]) or "").lower()
    if style not in TEXT_STYLES:
        style = TEXT_DEFAULTS["style"]
    try:
        c = parse_colour(t.get("colour", t.get("color",
                                               TEXT_DEFAULTS["colour"])),
                         "defaults.text.colour")
    except ValueError:
        c = None
    if c is None or c == "full":
        c = parse_colour(TEXT_DEFAULTS["colour"])
    out = {"style": style, "colour": "#%02x%02x%02x" % c}
    for role in ("route", "dest", "via"):
        f = str(t.get(f"{role}_font", "") or "").strip()
        out[f"{role}_font"] = os.path.basename(f) or \
            TEXT_DEFAULTS[f"{role}_font"]
        out[f"{role}_scale"] = _scale(t.get(f"{role}_scale"),
                                      TEXT_DEFAULTS[f"{role}_scale"])
    return out


def load_dest(path):
    """Load + validate a .dest file. Returns the parsed document.

    Raises SystemExit with a plain message (so CLI + portal share it).
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        raise SystemExit(f"program file {path}: {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"program file {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"program file {path}: top level must be an object")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise SystemExit(f"program file {path}: need a 'services' object "
                         f"with at least one service number")
    # validate defaults early so typos fail fast
    defaults_of(data)
    for svc_no, dests in services.items():
        # a service may be empty while under construction in the portal
        if not isinstance(dests, dict):
            raise SystemExit(f"program file {path}: service "
                             f"'{svc_no}' must hold an object of "
                             f"destinations")
        for dest_name, entry in dests.items():
            _validate_entry(path, svc_no, dest_name, entry)
    return data


def _validate_entry(path, svc_no, dest_name, entry):
    where = f"service '{svc_no}' destination '{dest_name}'"
    if not isinstance(entry, dict):
        raise SystemExit(f"program file {path}: {where} must be an object")
    maps = entry.get("bitmaps")
    # may be empty while under construction in the portal; resolve()
    # simply yields no screens for such destinations
    if not isinstance(maps, list):
        raise SystemExit(f"program file {path}: {where} needs a "
                         f"'bitmaps' list (may be empty)")
    for b in maps:
        if not isinstance(b, str) or not b.strip():
            raise SystemExit(f"program file {path}: {where} has an empty "
                             f"bitmap path")
    ov = entry.get("override", {})
    if ov in (None, ""):
        return
    if not isinstance(ov, dict):
        raise SystemExit(f"program file {path}: {where} 'override' must be "
                         f"an object")
    if "colour" in ov or "color" in ov:
        try:
            parse_colour(ov.get("colour", ov.get("color")), f"{where} colour")
        except ValueError as e:
            raise SystemExit(f"program file {path}: {e}")
    if "rotation_speed" in ov:
        r = _num(ov.get("rotation_speed"), -1)
        if r <= 0:
            raise SystemExit(f"program file {path}: {where} "
                             f"'rotation_speed' must be positive")
    for k in ("px_width", "px_height"):
        if k in ov and _int(ov.get(k), -1) <= 0:
            raise SystemExit(f"program file {path}: {where} '{k}' must be "
                             f"positive")


def list_services(data):
    """Sorted service numbers in a loaded document."""
    return sorted((data.get("services") or {}).keys(),
                  key=lambda s: (str(s).isdigit() is False, str(s)))


def list_destinations(data, service):
    """Destination names of one service, in file order."""
    dests = (data.get("services") or {}).get(service, {})
    if not isinstance(dests, dict):
        return []
    return list(dests.keys())


def entry_of(data, service, destination):
    dests = (data.get("services") or {}).get(service, {})
    if not isinstance(dests, dict):
        return None
    e = dests.get(destination)
    return e if isinstance(e, dict) else None


def resolve(data, service=None, destination=None):
    """Resolve a live pick to an ordered screen list.

    Returns `{"service", "destination", "screens", "defaults"}` where
    each screen is `{"image", "colour", "seconds", "service",
    "destination"}`. Colour/seconds already cascaded
    (override -> defaults).
    """
    defaults = defaults_of(data)
    services = data.get("services") or {}
    if service is None:
        # whole program: every service/destination in file order
        pairs = [(s, d) for s in services for d in list_destinations(data, s)]
    elif service not in services:
        raise SystemExit(f"service '{service}' not in program "
                         f"(have: {', '.join(list_services(data))})")
    elif destination is None:
        pairs = [(service, d) for d in list_destinations(data, service)]
    else:
        if destination not in (services[service] or {}):
            raise SystemExit(f"service '{service}' has no destination "
                             f"'{destination}'")
        pairs = [(service, destination)]
    if not pairs:
        raise SystemExit("nothing to show: program has no bitmaps")
    screens = []
    for s, d in pairs:
        entry = entry_of(data, s, d) or {}
        ov = entry.get("override") or {}
        if not isinstance(ov, dict):
            ov = {}
        colour = None
        if "colour" in ov or "color" in ov:
            colour = parse_colour(ov.get("colour", ov.get("color")),
                                  f"{s}/{d} colour")
        if colour is None:
            colour = defaults["colour"]
        secs = _num(ov.get("rotation_speed"), defaults["rotation_speed"])
        if secs <= 0:
            secs = defaults["rotation_speed"]
        for img in (entry.get("bitmaps") or []):
            screens.append({"image": img, "colour": colour,
                            "seconds": secs, "service": s,
                            "destination": d,
                            "service_code": entry.get("service_code", ""),
                            "service_name": entry.get("service_name", d)})
    if not screens:
        raise SystemExit("nothing to show: program has no bitmaps")
    return {"service": service, "destination": destination,
            "screens": screens, "defaults": defaults}


def blank_document():
    """Template for a brand-new .dest program."""
    return {
        "defaults": {
            "colour": DEFAULT_COLOUR,
            "rotation_speed": DEFAULT_ROTATION,
            "px_width": DEFAULT_PX_W,
            "px_height": DEFAULT_PX_H,
        },
        "services": {
            "43": {
                "Sheffield": {
                    "service_code": "001",
                    "service_name": "Sheffield",
                    "bitmaps": [
                        "bitmaps/PROGRAM/43/43-Sheffield-1.png"
                    ],
                }
            }
        },
    }


def dump(data):
    return json.dumps(data, indent=2) + "\n"


def program_name_for(path):
    base = os.path.basename(str(path))
    return base[:-5] if base.lower().endswith(".dest") else base
