#!/usr/bin/env python3
"""Web portal for the destination board (stdlib only).

Two pages, never wiping your inputs:

- `/` (Board) - pick program / service / destination and show it on
  the board. Only the status line and filmstrip refresh live; the
  form is never rewritten.
- `/create` (Create) - programs (.dest create/upload/delete/download),
  defaults, services, destinations + overrides, bitmap upload, and
  create-a-page-from-text (BDF fonts from fonts/ only). Every action
  updates just its own region - no page reloads except explicit
  program switches.

- Create new `.dest` programs, or upload an existing `.dest` file.
- Edit defaults (colour, rotation speed, panel size).
- Add/remove services (route numbers) and destinations, edit
  service_code / service_name / per-destination overrides.
- Upload bitmap PNG pages (saved as
  `bitmaps/<program>/<route>/<route>-<destination>-<page>.png` and
  appended to the destination, in order) or delete pages.
- Toggle what shows on the board: pick program / service /
  destination and the matrix follows via `board_control.json`
  (within ~0.5s, even across processes).

    python3 portal.py                      # http://localhost:4040
    python3 portal.py --port 4041
    python3 board.py programs/x.dest --serve   # same portal, other entry

Run from the project root.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

import destfile
import images
import render

PROGRAMS_DIR = os.path.join(THIS_DIR, "programs")
BITMAPS_DIR = os.path.join(THIS_DIR, "bitmaps")
PREVIEW_DIR = os.path.join(BITMAPS_DIR, ".preview")
PREVIEW_REL = "bitmaps/.preview/preview.png"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _programs():
    try:
        return sorted(f[:-5] for f in os.listdir(PROGRAMS_DIR)
                      if f.endswith(".dest"))
    except OSError:
        return []


def _dest_path(program):
    base = re.sub(r"[^a-zA-Z0-9._-]", "-", str(program or "").strip())
    base = base.strip("-") or "untitled"
    if not base.lower().endswith(".dest"):
        base += ".dest"
    full = os.path.normpath(os.path.join(PROGRAMS_DIR, base))
    if not full.startswith(PROGRAMS_DIR + os.sep):
        raise ValueError("bad program name")
    return full


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _write_json_validated(path, data):
    """Atomic write; re-validates as a .dest, restoring on failure."""
    try:
        original = None
        if os.path.isfile(path):
            with open(path) as f:
                original = f.read()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        destfile.load_dest(path)
    except SystemExit as e:
        if original is not None:
            with open(path, "w") as f:
                f.write(original)
        raise ValueError(str(e))


def _mutate(program, fn):
    """Load a program, apply fn(data) -> message, save. Returns message.

    When fn leaves the data unchanged the file is left alone, so
    no-op saves never cause cosmetic rewrites (3 -> 3.0, key order,
    colour case).
    """
    path = _dest_path(program)
    if not os.path.isfile(path):
        raise ValueError(f"no program '{program}'")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read program: {e}")
    before = json.dumps(data, sort_keys=True)
    msg = fn(data)
    if json.dumps(data, sort_keys=True) == before:
        return msg
    _write_json_validated(path, data)
    return msg


def _norm_override(raw):
    ov = {}
    if not isinstance(raw, dict):
        return ov
    if "colour" in raw or "color" in raw:
        c = destfile.parse_colour(raw.get("colour", raw.get("color")),
                                  "override colour")
        if c is not None:
            ov["colour"] = "full" if c == "full" else "#%02x%02x%02x" % c
    if "rotation_speed" in raw and raw["rotation_speed"] not in (None, ""):
        try:
            r = float(raw["rotation_speed"])
        except (TypeError, ValueError):
            raise ValueError("rotation_speed must be a number")
        if r <= 0:
            raise ValueError("rotation_speed must be positive")
        ov["rotation_speed"] = r
    for k in ("px_width", "px_height"):
        if k in raw and raw[k] not in (None, ""):
            try:
                v = int(float(raw[k]))
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a whole number")
            if v <= 0:
                raise ValueError(f"{k} must be positive")
            ov[k] = v
    return ov


def render_text_png(route, dest, via, style, colour, fonts=None):
    """Render one 240x40 blind from typed text, BDF fonts only.

    `fonts` is {"route": "<name>.bdf", "route_scale": 2, ...} for the
    route/dest/via roles; names must live in fonts/ (render.get_font
    rejects anything else, including system font names and paths).
    Returns (png_bytes, info). Raises ValueError with a plain message.
    """
    fonts = fonts or {}
    job = {"route": str(route or ""), "dest": str(dest or ""),
           "via": str(via or ""), "style": str(style or "top").lower(),
           "fg": colour or "#DB9600"}
    for role in ("route", "dest", "via"):
        f = fonts.get(role)
        if f not in (None, ""):
            job[f"{role}_font"] = str(f)
        s = fonts.get(f"{role}_scale")
        if s not in (None, ""):
            try:
                s = int(float(s))
            except (TypeError, ValueError):
                raise ValueError(f"{role} scale must be a whole number")
            if s < 1 or s > 4:
                raise ValueError(f"{role} scale must be 1-4")
            job[f"{role}_scale"] = s
    frame, info = render.render(job)  # ValueError propagates as-is
    return render.encode_png(frame), info


# ---------------------------------------------------------------------------
# controller (shared with board.py's live loop)
# ---------------------------------------------------------------------------

class Controller:
    """Live selection shared between the portal and the matrix loop."""

    def __init__(self, program=None, service=None, dest=None,
                 control=None, programs_dir=PROGRAMS_DIR):
        self.programs_dir = programs_dir
        self.control = control
        self.lock = threading.RLock()
        self.program_name = None
        self.service_name = None
        self.dest_name = None
        self.message = ""
        names = _programs()
        if program is not None:
            # board.py may pass a file path or a bare name
            program = destfile.program_name_for(program)
            if not program.endswith(".dest"):
                pass
        if program is None:
            program = names[0] if names else None
        if program is None:
            self.message = "Create or upload a program to begin"
            return
        self.program_name = program
        self.service_name = service
        self.dest_name = dest
        self._clamp()
        if self.message == "":
            self.message = "Pick a program, service and destination"

    # -- state ---------------------------------------------------------
    def _data(self):
        if not self.program_name:
            return None
        try:
            return destfile.load_dest(_dest_path(self.program_name))
        except (SystemExit, ValueError, OSError) as e:
            self.message = str(e)
            return None

    def _clamp(self):
        data = self._data()
        if data is None:
            return
        svcs = destfile.list_services(data)
        if self.service_name not in (data.get("services") or {}):
            self.service_name = None
        if self.service_name is not None:
            dests = destfile.list_destinations(data, self.service_name)
            if self.dest_name not in dests:
                self.dest_name = None

    def refresh(self):
        with self.lock:
            self._clamp()
            return False

    def key(self):
        with self.lock:
            return (self.program_name, self.service_name, self.dest_name)

    def selection(self):
        with self.lock:
            return (self.program_name, self.service_name, self.dest_name)

    def resolve(self):
        with self.lock:
            if not self.program_name:
                raise SystemExit("no program selected")
            path = _dest_path(self.program_name)
            data = destfile.load_dest(path)
            prog = destfile.resolve(data, self.service_name, self.dest_name)
            prog["program"] = self.program_name
            prog["path"] = path
            return prog

    def adopt(self, program, service=None, destination=None):
        with self.lock:
            if program not in _programs():
                self.message = f"Ignoring control: no program '{program}'"
                return False
            self.program_name = program
            self.service_name = service
            self.dest_name = destination
            self._clamp()
            self.message = ""
            return True

    # -- showing -------------------------------------------------------
    def show(self, program, service=None, destination=None):
        with self.lock:
            if program not in _programs():
                raise ValueError(f"no program '{program}'")
            data = destfile.load_dest(_dest_path(program))
            if service not in (None, ""):
                if service not in (data.get("services") or {}):
                    raise ValueError(f"no service '{service}' in '{program}'")
                dests = destfile.list_destinations(data, service)
                if destination not in (None, "") and destination not in dests:
                    raise ValueError(f"no destination '{destination}'")
                service = service or None
                destination = destination or None
            else:
                service, destination = None, None
            self.program_name = program
            self.service_name = service
            self.dest_name = destination
            self._save_control()
            self.message = ""
            return self.snapshot()

    def _save_control(self):
        if not self.control:
            return
        try:
            tmp = self.control + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"program": self.program_name,
                           "service": self.service_name,
                           "destination": self.dest_name}, f)
                f.write("\n")
            os.replace(tmp, self.control)
        except OSError as e:
            self.message = f"Cannot write control: {e}"

    # -- snapshot for the UI -------------------------------------------
    def snapshot(self):
        with self.lock:
            programs = _programs()
            data = self._data()
            defaults = destfile.defaults_of(data) if data else None
            if defaults is not None:
                defaults = {"colour": destfile.colour_str(defaults["colour"]),
                            "rotation_speed": defaults["rotation_speed"],
                            "px_width": defaults["px_width"],
                            "px_height": defaults["px_height"]}
            services = []
            if data:
                for s in destfile.list_services(data):
                    ds = []
                    for d in destfile.list_destinations(data, s):
                        e = destfile.entry_of(data, s, d) or {}
                        ds.append({"name": d,
                                   "service_code": e.get("service_code", ""),
                                   "service_name": e.get("service_name", d),
                                   "override": e.get("override") or {},
                                   "bitmaps": list(e.get("bitmaps") or [])})
                    services.append({"number": s, "destinations": ds})
            screens = []
            if data:
                try:
                    screens = [{"image": x["image"],
                                "service": x["service"],
                                "destination": x["destination"]}
                               for x in destfile.resolve(
                                   data, self.service_name,
                                   self.dest_name)["screens"]]
                except SystemExit:
                    screens = []
            return {
                "programs": programs,
                "program": self.program_name,
                "service": self.service_name,
                "destination": self.dest_name,
                "defaults": defaults,
                "services": services,
                "screens": screens,
                "fonts": render.available_fonts(),
                "message": self.message,
            }

    def snapshot_for(self, program):
        """Snapshot dict for any program (create page context).

        Never touches the live selection - browsing programs here does
        not retune the board. Returns None for unknown/unreadable
        programs.
        """
        with self.lock:
            if program not in _programs():
                return None
            try:
                data = destfile.load_dest(_dest_path(program))
            except (OSError, ValueError, SystemExit):
                return None
            defaults = destfile.defaults_of(data)
            services = []
            for s in destfile.list_services(data):
                ds = []
                for d in destfile.list_destinations(data, s):
                    e = destfile.entry_of(data, s, d) or {}
                    ds.append({"name": d,
                               "service_code": e.get("service_code", ""),
                               "service_name": e.get("service_name", d),
                               "override": e.get("override") or {},
                               "bitmaps": list(e.get("bitmaps") or [])})
                services.append({"number": s, "destinations": ds})
            same = (program == self.program_name)
            return {
                "programs": _programs(),
                "program": program,
                "service": self.service_name if same else None,
                "destination": self.dest_name if same else None,
                "defaults": {
                    "colour": destfile.colour_str(defaults["colour"]),
                    "rotation_speed": defaults["rotation_speed"],
                    "px_width": defaults["px_width"],
                    "px_height": defaults["px_height"]},
                "services": services,
                "screens": [],
                "fonts": render.available_fonts(),
                "message": "",
            }

    # -- program management --------------------------------------------
    def create_program(self, name):
        name = re.sub(r"[^a-zA-Z0-9._-]", "-",
                      str(name or "").strip()).strip("-")
        if not name:
            raise ValueError("name the program (e.g. citybus)")
        if name.lower().endswith(".dest"):
            name = name[:-5]
        if name in _programs():
            raise ValueError(f"program '{name}' already exists")
        path = _dest_path(name)
        doc = destfile.blank_document()
        # fix template path to this program name
        doc["services"]["43"]["Sheffield"]["bitmaps"] = []
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_json_validated(path, doc)
        with self.lock:
            self.program_name = name
            self.service_name = None
            self.dest_name = None
            self._save_control()
            self.message = f"Created program '{name}'"
            return self.snapshot()

    def upload_program(self, name, text):
        name = re.sub(r"[^a-zA-Z0-9._-]", "-",
                      str(name or "").strip()).strip("-")
        if not name:
            raise ValueError("name the program")
        if name.lower().endswith(".dest"):
            name = name[:-5]
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"not valid JSON: {e}")
        if not isinstance(data, dict) or "services" not in data:
            raise ValueError("not a .dest file (need defaults + services)")
        path = _dest_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        old = None
        if os.path.isfile(path):
            with open(path) as f:
                old = f.read()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        try:
            destfile.load_dest(path)
        except SystemExit as e:
            if old is not None:
                with open(path, "w") as f:
                    f.write(old)
            else:
                os.remove(path)
            raise ValueError(str(e))
        with self.lock:
            self.program_name = name
            self.service_name = None
            self.dest_name = None
            self._save_control()
            self.message = f"Uploaded program '{name}'"
            return self.snapshot()

    def delete_program(self, name):
        if name not in _programs():
            raise ValueError(f"no program '{name}'")
        os.remove(_dest_path(name))
        with self.lock:
            if self.program_name == name:
                rest = _programs()
                self.program_name = rest[0] if rest else None
                self.service_name = self.dest_name = None
                self._save_control()
            self.message = f"Deleted program '{name}'"
            return self.snapshot()

    # -- bitmap upload ---------------------------------------------------
    def add_bitmap(self, program, service, destination, filename, png):
        service = str(service or "").strip()
        destination = str(destination or "").strip()
        if not service or not destination:
            raise ValueError("pick a service and destination first")
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("not a PNG file")

        def _apply(data):
            svcs = data.get("services")
            if not isinstance(svcs, dict) or service not in svcs:
                raise ValueError(f"no service '{service}'")
            dests = svcs[service]
            if destination not in dests:
                raise ValueError(f"no destination '{destination}'")
            entry = dests[destination]
            lst = entry.get("bitmaps")
            if not isinstance(lst, list):
                lst = entry["bitmaps"] = []
            stem = f"{service}-{destfile.slug(destination)}"
            n = len(lst) + 1
            while True:
                base = f"{stem}-{n}.png"
                rel = "/".join(["bitmaps", destfile.slug(program),
                                destfile.slug(service), base])
                if rel not in lst and not os.path.isfile(
                        os.path.join(THIS_DIR, rel)):
                    break
                n += 1
            full = os.path.normpath(os.path.join(THIS_DIR, rel))
            if not full.startswith(BITMAPS_DIR + os.sep):
                raise ValueError("bad image path")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp_png = full + ".tmp"
            with open(tmp_png, "wb") as f:
                f.write(png)
            os.replace(tmp_png, full)
            lst.append(rel)
            return f"Added {base}"

        msg = _mutate(program, _apply)
        with self.lock:
            self.message = msg
            return self.snapshot()

    def create_text(self, program, service, slot, text_job,
                    service_code=None, rotation=None):
        """Create a destination page from typed text (BDF fonts only).

        Renders one 240x40 blind, saves it as the next
        `<route>-<destination>-<page>.png` page and selects it on the
        board. New services/destinations are created as needed. The
        destination gets `override.colour = "full"` (pixels are already
        final) plus `rotation_speed` when given. Returns the snapshot.
        """
        slot = str(slot or "").strip()
        service = str(service or "").strip()
        if not service or not slot:
            raise ValueError("name the service and destination slot")
        png, info = render_text_png(
            text_job.get("route", ""), text_job.get("dest", ""),
            text_job.get("via", ""), text_job.get("style", "top"),
            text_job.get("colour", "#DB9600") or "#DB9600",
            fonts=text_job.get("fonts") or {})
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("renderer produced a bad PNG")
        rot = None
        if rotation not in (None, ""):
            try:
                rot = float(rotation)
            except (TypeError, ValueError):
                raise ValueError("rotation must be a number")
            if rot <= 0:
                raise ValueError("rotation must be positive")

        # next page path (mirrors add_bitmap naming)
        try:
            with open(_dest_path(program)) as f:
                cur = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read program: {e}")
        have = []
        try:
            have = (cur.get("services") or {}).get(service, {}).get(
                slot, {}).get("bitmaps") or []
        except AttributeError:
            have = []
        stem = f"{service}-{destfile.slug(slot)}"
        n = len(have) + 1
        while True:
            base = f"{stem}-{n}.png"
            rel = "/".join(["bitmaps", destfile.slug(program),
                            destfile.slug(service), base])
            if rel not in have and not os.path.isfile(
                    os.path.join(THIS_DIR, rel)):
                break
            n += 1
        full = os.path.normpath(os.path.join(THIS_DIR, rel))
        if not full.startswith(BITMAPS_DIR + os.sep):
            raise ValueError("bad image path")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp_png = full + ".tmp"
        with open(tmp_png, "wb") as f:
            f.write(png)
        os.replace(tmp_png, full)

        def _ensure(data):
            svcs = data.setdefault("services", {})
            dests = svcs.setdefault(service, {})
            e = dests.get(slot)
            if not isinstance(e, dict):
                e = dests[slot] = {"bitmaps": []}
            if service_code not in (None, ""):
                e["service_code"] = str(service_code)
            elif not e.get("service_code"):
                e["service_code"] = "%03d" % len(dests)
            if not e.get("service_name"):
                e["service_name"] = slot
            ov = {"colour": "full"}  # pixels already final, no tint
            if rot is not None:
                ov["rotation_speed"] = rot
            e["override"] = ov
            lst = e.get("bitmaps")
            if not isinstance(lst, list):
                lst = e["bitmaps"] = []
            if rel not in lst:
                lst.append(rel)
            return f"Added {base}"

        _mutate(program, _ensure)
        with self.lock:
            self.program_name = program
            self.service_name = service
            self.dest_name = slot
            self._save_control()
            warns = info.get("warnings", [])
            self.message = f"Showing {service} {slot}" + \
                (f" ({'; '.join(warns)})" if warns else "")
            return self.snapshot()

    # -- flash preview (shows one image on the board, then resumes) ---
    def _colour_for(self, image):
        """Effective colour string for a bitmap, else "full"."""
        for name in _programs():
            try:
                data = destfile.load_dest(_dest_path(name))
            except (OSError, ValueError, SystemExit):
                continue
            defaults = destfile.defaults_of(data)
            for s in destfile.list_services(data):
                for d in destfile.list_destinations(data, s):
                    e = destfile.entry_of(data, s, d) or {}
                    if image in (e.get("bitmaps") or []):
                        ov = e.get("override") or {}
                        c = ov.get("colour", ov.get("color"))
                        if c is None:
                            return destfile.colour_str(
                                defaults["colour"])
                        try:
                            cc = destfile.parse_colour(c, "colour")
                        except ValueError:
                            return "full"
                        if cc == "full":
                            return "full"
                        return "#%02x%02x%02x" % cc
        return "full"

    def flash(self, image, colour=None, seconds=10):
        """Show one bitmap on the board for `seconds`, then resume.

        Writes a preview stanza into the control file; the matrix loop
        picks it up within ~0.5s and returns to the live selection when
        it expires. The .dest file is untouched.
        """
        image = str(image or "").strip()
        full = os.path.normpath(os.path.join(THIS_DIR, image))
        if not full.startswith(BITMAPS_DIR + os.sep):
            raise ValueError("bad image path")
        if not os.path.isfile(full):
            raise ValueError("no such image")
        try:
            secs = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("seconds must be a number")
        if secs <= 0 or secs > 120:
            raise ValueError("seconds must be 1-120")
        if colour is None:
            colour = self._colour_for(image)
        if not self.control:
            raise ValueError("no control file configured")
        base = {"program": self.program_name,
                "service": self.service_name,
                "destination": self.dest_name}
        if os.path.isfile(self.control):
            try:
                with open(self.control) as f:
                    c = json.load(f)
                if isinstance(c, dict) and isinstance(c.get("program"),
                                                     str):
                    base = {"program": c.get("program"),
                            "service": c.get("service"),
                            "destination": c.get("destination")}
            except (OSError, ValueError):
                pass
        try:
            tmp = self.control + ".tmp"
            with open(tmp, "w") as f:
                json.dump({**base, "preview": {
                    "image": image, "colour": colour,
                    "seconds": secs, "at": time.time()}}, f)
                f.write("\n")
            os.replace(tmp, self.control)
        except OSError as e:
            raise ValueError(f"cannot write control: {e}")
        with self.lock:
            self.message = f"Flashing {os.path.basename(image)} on board"
            return self.snapshot()

    def stage_preview(self, png):
        """Save a rendered preview to the staging slot. Returns rel path."""
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("renderer produced a bad PNG")
        full = os.path.join(THIS_DIR, PREVIEW_REL)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, full)
        return PREVIEW_REL


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

STYLE = """*{box-sizing:border-box}
body{background:#0d0d10;color:#ddd;font-family:Arial,Helvetica,sans-serif;
margin:0;padding:20px;display:flex;flex-direction:column;align-items:center;
gap:14px;min-height:100vh}
h1{color:#ffb000;margin:0;font-size:22px}
h2{color:#ffb000;font-size:16px;margin:14px 0 6px}
.wrap{width:100%;max-width:860px;display:flex;flex-direction:column;gap:14px}
.card{background:#16161b;border:1px solid #333;border-radius:10px;
padding:12px 14px;font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
input,select,button{background:#222;color:#fff;border:1px solid #555;
border-radius:6px;padding:6px 8px;font-size:13px}
button{background:#1d5c2e;border-color:#1d5c2e;cursor:pointer}
button.ghost{background:#1d3a4c;border-color:#1d3a4c}
button.danger{background:#6e1b1b;border-color:#6e1b1b}
.hint{color:#888;font-size:12px}
.ok{color:#6f6}.err{color:#f66}
.showing{font-size:15px}
.showing b{color:#37e05a}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
th,td{border-bottom:1px solid #333;padding:6px 8px;text-align:left;
vertical-align:top}
.strip{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px}
.strip figure{margin:0;text-align:center}
.strip img{width:180px;height:30px;object-fit:contain;background:#000;
border-radius:4px;border:1px solid #3a3a42;image-rendering:pixelated}
.strip figcaption{font-size:11px;color:#888;margin-top:2px}
code{background:#000;padding:1px 5px;border-radius:4px;color:#ffb000}
label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:#aaa}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.tabs{display:flex;gap:8px}
.tabs a{background:#1b1b21;color:#9a9aa2;border:1px solid #34343e;
border-radius:8px;padding:8px 22px;font-size:15px;text-decoration:none}
.tabs a.on{background:#1d3a4c;color:#fff;border-color:#1d3a4c}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
"""


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _sel(name, items, cur, all_label=None, extra=""):
    out = [f'<select name="{_esc(name)}" id="{_esc(extra or name)}">']
    if all_label is not None:
        out.append(f'<option value="">{_esc(all_label)}</option>')
    for v in items:
        sel = " selected" if v == cur else ""
        out.append(f'<option value="{_esc(v)}"{sel}>{_esc(v)}</option>')
    out.append("</select>")
    return "".join(out)


def _program_map():
    """{program: {service: [destinations]}} for every loadable .dest."""
    pmap = {}
    for name in _programs():
        try:
            with open(_dest_path(name)) as f:
                data = json.load(f)
            destfile.load_dest(_dest_path(name))
        except (OSError, ValueError, SystemExit):
            continue
        pmap[name] = {s: destfile.list_destinations(data, s)
                      for s in destfile.list_services(data)}
    return pmap


def _shell(title, active, body):
    t1 = ' class="on"' if active == "board" else ""
    t2 = ' class="on"' if active == "create" else ""
    return ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,"
            " initial-scale=1\">"
            f"<title>{_esc(title)}</title>"
            f"<style>{STYLE}</style></head><body><h1>Destination board</h1>"
            f"<div class=\"tabs\"><a href=\"/\"{t1}>Board</a>"
            f"<a href=\"/create\"{t2}>Create</a></div>"
            f"<div class=\"wrap\">{body}</div></body></html>")


def edit_html(rel, w, h, dataurl):
    """Pixel editor for one bitmap (LED-dot canvas, no reloads)."""
    cfg = json.dumps({"image": rel, "w": w, "h": h,
                      "src": dataurl}).replace("<", "\\u003c")
    scales = "".join(
        f"<option{' selected' if s == 4 else ''}>{s}</option>"
        for s in (2, 3, 4, 6, 8, 12, 16))
    body = f"""<div class="card"><h2>Editing <code>{_esc(rel)}</code>
({_esc(w)}x{_esc(h)})</h2>
<div class="row">
<button id="t-paint" class="on">paint</button>
<button id="t-erase">erase</button>
<button id="t-pick" class="ghost">pick</button>
<label>colour<input type="color" id="paint" value="#db9600"></label>
<label>zoom<select id="zoom">{scales}</select></label>
<button id="undo" class="ghost">undo</button>
<button id="clear" class="danger">clear</button>
<button id="reset" class="ghost">reset</button>
<button id="save">Save</button>
<a href="/create" style="color:#8cf">back to create</a>
</div>
<div style="overflow:auto;margin-top:8px"><canvas id="view"
style="image-rendering:pixelated;background:#000;border:1px solid #3a3a42">
</canvas></div>
<div class="hint" id="emsg">drag to paint, one LED per pixel. Save writes the
PNG back over the original.</div>
<div class="hint" id="lit"></div>
</div>
<script>
var CFG={cfg};
var cv=document.getElementById('view');
var cx=cv.getContext('2d');
var off=document.createElement('canvas');
off.width=CFG.w;off.height=CFG.h;
var ox=off.getContext('2d',{{willReadFrequently:true}});
var img=new Image();
var scale=4,tool='paint',drawing=false,undo=[];
function setTool(t){{tool=t;
 ['paint','erase','pick'].forEach(function(k){{
  document.getElementById('t-'+k).className=(k===t)?'on':'ghost';}});}}
document.getElementById('t-paint').onclick=function(){{setTool('paint');}};
document.getElementById('t-erase').onclick=function(){{setTool('erase');}};
document.getElementById('t-pick').onclick=function(){{setTool('pick');}};
document.getElementById('zoom').onchange=function(e){{
 scale=parseInt(e.target.value,10)||4;render();}};
function push(){{undo.push(ox.getImageData(0,0,CFG.w,CFG.h));
 if(undo.length>30)undo.shift();}}
document.getElementById('undo').onclick=function(){{
 var d=undo.pop();if(!d)return;ox.putImageData(d,0,0);render();}};
document.getElementById('clear').onclick=function(){{
 if(!confirm('Clear to black?'))return;push();
 ox.fillStyle='#000';ox.fillRect(0,0,CFG.w,CFG.h);render();}};
document.getElementById('reset').onclick=function(){{
 if(!confirm('Reload original?'))return;push();
 ox.drawImage(img,0,0);render();}};
function render(){{
 cv.width=CFG.w*scale;cv.height=CFG.h*scale;
 var d=ox.getImageData(0,0,CFG.w,CFG.h).data;
 var lit=0;
 cx.fillStyle='#000';cx.fillRect(0,0,cv.width,cv.height);
 for(var y=0;y<CFG.h;y++){{for(var x=0;x<CFG.w;x++){{
  var o=(y*CFG.w+x)*4;
  var r=d[o],g=d[o+1],b=d[o+2];
  var on=(r||g||b)?true:false;
  if(on)lit++;
  var px=x*scale,py=y*scale,r2=scale*0.38;
  cx.beginPath();
  cx.arc(px+scale/2,py+scale/2,r2,0,6.2832);
  cx.fillStyle=on?('rgb('+r+','+g+','+b+')'):'#181818';
  cx.fill();
 }}}}
 document.getElementById('lit').textContent=
  lit+' lit pixels of '+(CFG.w*CFG.h);
}}
function cell(ev){{
 var r=cv.getBoundingClientRect();
 var x=Math.floor((ev.clientX-r.left)/r.width*CFG.w);
 var y=Math.floor((ev.clientY-r.top)/r.height*CFG.h);
 return [Math.max(0,Math.min(CFG.w-1,x)),
         Math.max(0,Math.min(CFG.h-1,y))];
}}
function stroke(ev){{
 var c=cell(ev),x=c[0],y=c[1];
 if(tool==='pick'){{
  var d=ox.getImageData(x,y,1,1).data;
  var h='#'+((1<<24)+(d[0]<<16)+(d[1]<<8)+d[2]).toString(16).slice(1);
  document.getElementById('paint').value=h;setTool('paint');return;
 }}
 ox.fillStyle=(tool==='erase')?'#000':
  document.getElementById('paint').value;
 ox.fillRect(x,y,1,1);render();
}}
cv.addEventListener('pointerdown',function(ev){{
 ev.preventDefault();drawing=true;push();stroke(ev);
 try{{cv.setPointerCapture(ev.pointerId);}}catch(e){{}}}});
cv.addEventListener('pointermove',function(ev){{
 if(!drawing)return;stroke(ev);}});
window.addEventListener('pointerup',function(){{drawing=false;}});
document.getElementById('save').onclick=async function(){{
 var m=document.getElementById('emsg');m.textContent='saving...';
 var r=await fetch('/api/bitmap/edit',{{method:'POST',
  headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{image:CFG.image,
   data:off.toDataURL('image/png')}})}});
 var j=await r.json();
 m.textContent=j.ok?('saved '+CFG.image):(j.error||'save failed');
 m.className=j.ok?'hint ok':'hint err';
}};
img.onload=function(){{ox.drawImage(img,0,0);render();}};
img.src=CFG.src;
</script>"""
    return _shell("Edit image", "create", body)


def board_html(snap, pmap):
    prog = snap["program"]
    svc = snap["service"]
    dst = snap["destination"]
    svcs = list((pmap.get(prog) or {}).keys())
    dests = (pmap.get(prog) or {}).get(svc, []) if svc else []
    now = (f"{prog or '-'} / {svc or 'all'} / {dst or 'all'} "
           f"({len(snap['screens'])} pages)")
    strip = "".join(
        f"<figure><img src=\"/api/img?path={urllib.parse.quote(s['image'])}\""
        f" title=\"{_esc(s['image'])}\" loading=\"lazy\">"
        f"<figcaption>{_esc(s['service'])} / {_esc(s['destination'])}"
        f"</figcaption></figure>" for s in snap["screens"])
    map_json = json.dumps(pmap).replace("<", "\\u003c")
    sig_json = json.dumps("|".join(
        [prog or "", svc or "", dst or ""] +
        [s["image"] for s in snap["screens"]])).replace("<", "\\u003c")
    body = f"""<div class="card"><h2>Now showing</h2>
<div class="showing">board: <b id="now">{_esc(now)}</b></div>
<form method="post" action="/show"><div class="row">
{_sel("program", snap["programs"], prog)}
{_sel("service", svcs, svc, "all services")}
{_sel("destination", dests, dst, "all destinations")}
<button type="submit">Show on board</button>
</div></form>
<div class="hint">The line above follows the board live. This form is never
rewritten - pick and press Show.</div></div>
<div class="card"><h2>Screens</h2><div class="strip" id="strip">{strip}</div>
</div>
<script>
var MAP={map_json};
var lastSig={sig_json};
var prog=document.getElementsByName('program')[0];
var svc=document.getElementsByName('service')[0];
var dst=document.getElementsByName('destination')[0];
function fill(sel, items, cur, allLabel){{
 var keep=false;
 sel.innerHTML='';
 if(allLabel){{var o=document.createElement('option');o.value='';
  o.textContent=allLabel;sel.appendChild(o);}}
 items.forEach(function(v){{var o=document.createElement('option');
  o.value=v;o.textContent=v;if(v===cur){{o.selected=true;keep=true;}}
  sel.appendChild(o);}});
 if(!keep&&allLabel)sel.value='';
}}
prog.addEventListener('change',function(){{
 var m=MAP[prog.value]||{{}};
 fill(svc,Object.keys(m),'','all services');
 fill(dst,[],'','all destinations');
}});
svc.addEventListener('change',function(){{
 var m=MAP[prog.value]||{{}};
 fill(dst,m[svc.value]||[],'','all destinations');
}});
async function tick(){{
 try{{
  var r=await fetch('/api/state');var j=await r.json();
  if(!j.ok)return;
  var s=j.state;
  document.getElementById('now').textContent=
   (s.program||'-')+' / '+(s.service||'all')+' / '+
   (s.destination||'all')+' ('+s.screens.length+' pages)';
  var sig=[s.program||'',s.service||'',s.destination||'']
   .concat(s.screens.map(function(x){{return x.image;}})).join('|');
  if(sig===lastSig)return;
  lastSig=sig;
  var st=document.getElementById('strip');st.innerHTML='';
  s.screens.forEach(function(x){{
   var f=document.createElement('figure');
   var im=document.createElement('img');
   im.src='/api/img?path='+encodeURIComponent(x.image);
   im.title=x.image;im.loading='lazy';
   var c=document.createElement('figcaption');
   c.textContent=x.service+' / '+x.destination;
   f.appendChild(im);f.appendChild(c);st.appendChild(f);
  }});
 }}catch(e){{}}
}}
setInterval(tick,3000);
</script>"""
    return _shell("Board", "board", body)


def create_html(snap, program, fonts):
    if program not in snap["programs"]:
        program = snap["program"]
    data_json = json.dumps(snap).replace("<", "\\u003c")
    font_opts = "".join(f"<option>{_esc(f)}</option>" for f in fonts)

    def _fontsel(fid, default):
        opts = []
        for f in fonts:
            sel = " selected" if f == default else ""
            opts.append(f"<option{sel}>{_esc(f)}</option>")
        return f"<select id=\"{fid}\">{''.join(opts)}</select>"

    body = f"""<div class="card"><h2>Program</h2>
<div class="row">
{_sel("cprog", snap["programs"], program)}
<button class="ghost" onclick="switchProgram()">Edit</button>
<input id="newname" placeholder="new program e.g. citybus" size="18">
<button onclick="createProgram()">Create</button>
<button class="danger" onclick="deleteProgram()">Delete</button>
</div>
<div class="row">
<input type="file" id="destfile" accept=".dest,.json,application/json">
<button class="ghost" onclick="uploadProgram()">Upload .dest</button>
<a id="dl" href="/api/destfile?program={urllib.parse.quote(program or '')}"
style="color:#8cf">download .dest</a>
</div>
<div class="hint" id="pmsg"></div></div>
<div class="card"><h2>Defaults</h2>
<div class="row">
<label>colour<input id="dcolour" size="8"></label>
<label>rotation secs<input id="drot" size="5"></label>
<label>px width<input id="dpxw" size="5"></label>
<label>px height<input id="dpxh" size="5"></label>
<button onclick="saveDefaults()">Save defaults</button>
</div>
<div class="hint" id="dmsg"><code>full</code> is not allowed here - it is a
per-destination override meaning "keep bitmap colours".</div></div>
<div class="card"><h2>Services &amp; destinations</h2>
<div class="row">
<input id="nsvc" placeholder="service e.g. 43" size="8">
<button class="ghost" onclick="addService()">Add service</button>
<input id="ndest" placeholder="destination e.g. Sheffield" size="14">
<input id="ncode" placeholder="code e.g. 001" size="7">
<button class="ghost" onclick="addDestination()">Add destination</button>
</div>
<div id="svcs"></div>
<div class="hint" id="smsg"></div></div>
<div class="card"><h2>Create page from text (fonts/ only)</h2>
<div class="grid">
<label>service (route no.)<input id="tservice"></label>
<label>destination slot<input id="tslot"></label>
<label>route text<input id="troute"></label>
<label>destination text<input id="ttext"></label>
<label>via (optional)<input id="tvia"></label>
<label>layout<select id="tstyle"><option value="top">top - via over dest</option><option value="bottom">bottom - dest over via</option><option value="left">left - via | dest</option><option value="right">right - dest | via</option></select></label>
<label>colour<input id="tcolour" value="#DB9600"></label>
<label>rotation secs (optional)<input id="trot" placeholder="default"></label>
<label>route font{_fontsel("troute_font", "10x20.bdf")}</label>
<label>route scale<select id="troute_scale"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
<label>dest font{_fontsel("tdest_font", "10x20.bdf")}</label>
<label>dest scale<select id="tdest_scale"><option selected>1</option><option>2</option><option>3</option><option>4</option></select></label>
<label>via font{_fontsel("tvia_font", "6x13B.bdf")}</label>
<label>via scale<select id="tvia_scale"><option selected>1</option><option>2</option><option>3</option><option>4</option></select></label>
</div>
<div class="row"><button class="ghost" onclick="previewText()">Preview</button>
<button onclick="createText()">Create + show on board</button></div>
<img id="tpreview" alt="preview" style="width:100%;max-width:480px;height:80px;object-fit:contain;background:#000;border-radius:6px;border:1px solid #3a3a42;display:none;image-rendering:pixelated;margin-top:8px">
<div class="hint" id="tmsg">Rendered with BDF bitmap fonts from
<code>fonts/</code> only - no system fonts. This card never reloads.</div>
</div>
<div class="card"><h2>Upload bitmap page</h2>
<div class="row">
<input id="usvc" placeholder="service" size="8">
<input id="uslot" placeholder="destination" size="14">
<input type="file" id="upfile" accept=".png,image/png">
<button onclick="uploadBitmap()">Upload + append page</button>
</div>
<div class="hint" id="upmsg">PNG only (ideally 240x40). Saved as
<code>&lt;route&gt;-&lt;destination&gt;-&lt;next-page&gt;.png</code>.</div>
</div>
<script>
var S={data_json};
function el(id){{return document.getElementById(id);}}
function esc(s){{return String(s===null||s===undefined?'':s).replace(
 /[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;',
 '"':'&quot;'}}[c];}});}}
function toast(id,msg,err){{var m=el(id);m.textContent=msg;
 m.className=err?'err':'ok';}}
async function api(path,body){{
 var r=await fetch(path,{{method:'POST',
  headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify(body)}});
 var j=await r.json();
 return j;
}}
function renderSvcs(){{
 var box=el('svcs');box.innerHTML='';
 (S.services||[]).forEach(function(sv){{
  var h=document.createElement('h2');
  h.setAttribute('data-svc',sv.number);
  h.innerHTML='Service '+esc(sv.number)+' '+
   '<input size="8" data-rensvc placeholder="new number...">'+
   '<button data-act="ren-svc">rename</button> '+
   '<button class="danger" data-act="del-svc">delete service</button>';
  box.appendChild(h);
  sv.destinations.forEach(function(d){{
   var t=document.createElement('div');t.className='row';
   t.innerHTML='<b>'+esc(d.name)+'</b><span class="hint">code '+
    esc(d.service_code||'-')+' / '+esc(d.service_name||d.name)+'</span>'+
    '<button data-act="show-dest">show</button>'+
    '<button class="danger" data-act="del-dest">delete</button>'+
    '<details><summary>edit</summary><div class="row">'+
    '<label>slot<input data-e="slot" value="'+esc(d.name)+'"></label>'+
    '<label>code<input data-e="service_code" size="7" value="'+
    esc(d.service_code||'')+'"></label>'+
    '<label>name<input data-e="service_name" value="'+
    esc(d.service_name||'')+'"></label>'+
    '<label>colour<input data-e="colour" size="8" value="'+
    esc(d.override.colour||'')+'" placeholder="default"></label>'+
    '<label>rotation<input data-e="rotation_speed" size="5" value="'+
    esc(d.override.rotation_speed===undefined?'':d.override.rotation_speed)+
    '" placeholder="default"></label>'+
    '<label>px w<input data-e="px_width" size="5" value="'+
    esc(d.override.px_width===undefined?'':d.override.px_width)+
    '" placeholder="default"></label>'+
    '<label>px h<input data-e="px_height" size="5" value="'+
    esc(d.override.px_height===undefined?'':d.override.px_height)+
    '" placeholder="default"></label>'+
    '<button data-act="save-dest">save</button>'+
    '</div><div class="hint">Renaming the slot renames its PNG files too. '+
    'Empty override fields fall back to defaults; saving with all four '+
    'empty removes the override.</div></details>';
   t.setAttribute('data-svc',sv.number);t.setAttribute('data-dest',d.name);
   box.appendChild(t);
   var strip=document.createElement('div');strip.className='strip';
   (d.bitmaps||[]).forEach(function(img,i){{
    var f=document.createElement('figure');
    var im=document.createElement('img');
    im.src='/api/img?path='+encodeURIComponent(img);
    im.title=img;im.loading='lazy';
    var cap=document.createElement('figcaption');
    cap.textContent='page '+(i+1);
    var b=document.createElement('button');b.textContent='delete';
    b.className='danger';b.setAttribute('data-act','del-page');
    b.setAttribute('data-svc',sv.number);b.setAttribute('data-dest',d.name);
    b.setAttribute('data-img',img);
    var fl=document.createElement('button');fl.textContent='flash';
    fl.setAttribute('data-act','flash-page');
    fl.setAttribute('data-svc',sv.number);fl.setAttribute('data-dest',
     d.name);fl.setAttribute('data-img',img);
    var ed=document.createElement('button');ed.textContent='edit';
    ed.className='ghost';ed.setAttribute('data-act','edit-page');
    ed.setAttribute('data-svc',sv.number);ed.setAttribute('data-dest',
     d.name);ed.setAttribute('data-img',img);
    f.appendChild(im);f.appendChild(cap);
    f.appendChild(fl);f.appendChild(ed);f.appendChild(b);
    strip.appendChild(f);}});
   if(!(d.bitmaps||[]).length){{var p=document.createElement('div');
    p.className='hint';p.textContent='no pages yet.';strip.appendChild(p);}}
   box.appendChild(strip);
  }});
 }});
}}
el('svcs').addEventListener('click',async function(e){{
 var b=e.target.closest('button');if(!b)return;
 var row=e.target.closest('[data-svc]');if(!row)return;
 var svc=row.getAttribute('data-svc');
 var dest=row.getAttribute('data-dest');
 var act=b.getAttribute('data-act');
 var j=null;
 if(act==='del-svc'){{
  if(!confirm('Delete service '+svc+'?'))return;
  j=await api('/api/service/delete',{{program:S.program,service:svc}});
 }}else if(act==='del-dest'){{
  if(!confirm('Delete '+dest+'?'))return;
  j=await api('/api/destination/delete',{{program:S.program,
   service:svc,destination:dest}});
 }}else if(act==='ren-svc'){{
  var rninp=row.querySelector('input[data-rensvc]');
  var nn=rninp?rninp.value:'';
  if(!nn){{toast('smsg','type the new service number first',true);return;}}
  j=await api('/api/service/rename',{{program:S.program,
   service:svc,new_name:nn}});
 }}else if(act==='save-dest'){{
  var f={{}};
  row.querySelectorAll('input[data-e]').forEach(function(i){{
   f[i.getAttribute('data-e')]=i.value;}});
  j=await api('/api/destination/edit',{{program:S.program,
   service:svc,destination:dest,new_name:f.slot,
   service_code:f.service_code,service_name:f.service_name,
   override:{{colour:f.colour,rotation_speed:f.rotation_speed,
    px_width:f.px_width,px_height:f.px_height}}}});
 }}else if(act==='show-dest'){{
  j=await api('/api/show',{{program:S.program,service:svc,
   destination:dest}});
 }}else if(act==='flash-page'){{
  j=await api('/api/flash',{{image:b.getAttribute('data-img')}});
  if(!j)return;
  if(!j.ok){{toast('smsg',j.error||'failed',true);return;}}
  S=j.state;renderSvcs();
  toast('smsg',S.message||'flashing on board',false);
  return;
 }}else if(act==='edit-page'){{
  location='/edit?image='+encodeURIComponent(b.getAttribute('data-img'));
  return;
 }}else if(act==='del-page'){{
  if(!confirm('Delete this page?'))return;
  j=await api('/api/bitmap/delete',{{program:S.program,service:svc,
   destination:dest,image:b.getAttribute('data-img')}});
 }}else return;
 if(!j)return;
 if(!j.ok){{toast('smsg',j.error||'failed',true);return;}}
 S=j.state;renderSvcs();toast('smsg','saved',false);
}});
function ctxSvc(){{var s=S.services||[];
 for(var i=0;i<s.length;i++)if(s[i].number===S.service)return s[i];
 return s[0];}}
function fillCtx(){{
 if(S.defaults){{el('dcolour').value=S.defaults.colour;
  el('drot').value=S.defaults.rotation_speed;
  el('dpxw').value=S.defaults.px_width;
  el('dpxh').value=S.defaults.px_height;}}
 var c=ctxSvc();var d=c&&c.destinations[0];
 if(!el('tservice').value)el('tservice').value=S.service||(c&&c.number)||'';
 if(!el('tslot').value)el('tslot').value=S.destination||(d&&d.name)||'';
 if(!el('troute').value)el('troute').value=S.service||(c&&c.number)||'';
 if(!el('ttext').value)el('ttext').value=S.destination||(d&&d.name)||'';
 if(!el('usvc').value)el('usvc').value=el('tservice').value;
 if(!el('uslot').value)el('uslot').value=el('tslot').value;
 var dl=el('dl');if(dl)dl.textContent='download '+S.program+'.dest';
}}
async function switchProgram(){{location='/create?program='+
 encodeURIComponent(el('cprog').value);}}
async function createProgram(){{
 var j=await api('/api/program/create',{{name:el('newname').value}});
 if(!j.ok){{toast('pmsg',j.error||'failed',true);return;}}
 location='/create?program='+encodeURIComponent(
  (j.state&&j.state.program)||el('newname').value);
}}
async function deleteProgram(){{
 if(!confirm('Delete program '+S.program+'?'))return;
 var j=await api('/api/program/delete',{{program:S.program}});
 if(!j.ok){{toast('pmsg',j.error||'failed',true);return;}}
 location='/create';
}}
async function uploadProgram(){{
 var f=el('destfile').files[0];
 if(!f){{toast('pmsg','pick a .dest file first',true);return;}}
 var text=await f.text();
 var j=await api('/api/program/upload',{{name:f.name.replace(/\\.dest$/i,''),
  text:text}});
 if(!j.ok){{toast('pmsg',j.error||'failed',true);return;}}
 location='/create?program='+encodeURIComponent(j.state.program);
}}
async function saveDefaults(){{
 var j=await api('/api/defaults',{{program:S.program,
  defaults:{{colour:el('dcolour').value,rotation_speed:el('drot').value,
   px_width:el('dpxw').value,px_height:el('dpxh').value}}}});
 if(!j.ok){{toast('dmsg',j.error||'failed',true);return;}}
 S=j.state;toast('dmsg','saved defaults',false);
}}
async function addService(){{
 var j=await api('/api/service/add',{{program:S.program,
  service:el('nsvc').value}});
 if(!j.ok){{toast('smsg',j.error||'failed',true);return;}}
 S=j.state;renderSvcs();el('nsvc').value='';toast('smsg','saved',false);
}}
async function addDestination(){{
 var svc=S.service||el('nsvc').value;
 var j=await api('/api/destination/add',{{program:S.program,service:svc,
  destination:el('ndest').value,service_code:el('ncode').value}});
 if(!j.ok){{toast('smsg',j.error||'failed',true);return;}}
 S=j.state;renderSvcs();el('ndest').value='';toast('smsg','saved',false);
}}
function textForm(){{return {{program:S.program,
 service:el('tservice').value,slot:el('tslot').value,
 route:el('troute').value,text:el('ttext').value,via:el('tvia').value,
 style:el('tstyle').value,colour:el('tcolour').value||'#DB9600',
 rotation:el('trot').value,
 fonts:{{route:el('troute_font').value,
  route_scale:el('troute_scale').value,dest:el('tdest_font').value,
  dest_scale:el('tdest_scale').value,via:el('tvia_font').value,
  via_scale:el('tvia_scale').value}}}};}}
async function previewText(){{
 toast('tmsg','rendering...',false);
 var form=textForm();form.board=true;
 var r=await fetch('/api/render-preview',{{method:'POST',
  headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify(form)}});
 var j=await r.json();
 if(!j.ok){{toast('tmsg',j.error||'preview failed',true);return;}}
 var im=el('tpreview');im.src=j.data;im.style.display='block';
 toast('tmsg',j.lit+' lit pixels'+(j.warnings.length?' - '+
  j.warnings.join('; '):'')+(j.flashed?' - on the board for 10s':''),false);
}}
async function createText(){{
 toast('tmsg','creating...',false);
 var j=await api('/api/create-text',textForm());
 if(!j.ok){{toast('tmsg',j.error||'create failed',true);return;}}
 location='/';
}}
async function uploadBitmap(){{
 var f=el('upfile').files[0];
 if(!f){{toast('upmsg','pick a PNG file first',true);return;}}
 toast('upmsg','uploading...',false);
 var rd=new FileReader();
 rd.onload=async function(){{
  var j=await api('/api/bitmap/upload',{{program:S.program,
   service:el('usvc').value,destination:el('uslot').value,
   filename:f.name,data:rd.result}});
  if(!j.ok){{toast('upmsg',j.error||'upload failed',true);return;}}
  S=j.state;renderSvcs();toast('upmsg','saved page',false);
 }};
 rd.readAsDataURL(f);
}}
var cprog=el('cprog');
if(cprog)cprog.addEventListener('change',switchProgram);
renderSvcs();fillCtx();
</script>"""
    return _shell("Create", "create", body)


def serve(ctl, port):
    outer = ctl

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _body(self, max_bytes=4 * 1024 * 1024):
            try:
                ln = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                ln = 0
            if ln <= 0 or ln > max_bytes:
                return None
            try:
                return json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return None

        def _ok(self, extra=None):
            out = {"ok": True, "state": outer.snapshot()}
            if extra:
                out.update(extra)
            self._json(out)

        def _fail(self, e, code=400):
            self._json({"ok": False,
                        "error": str(e) or "failed"}, code)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            if parts.path in ("/", "/index.html"):
                self._send(board_html(outer.snapshot(),
                                      _program_map()).encode(),
                           "text/html; charset=utf-8")
            elif parts.path == "/create":
                q = urllib.parse.parse_qs(parts.query)
                prog = (q.get("program") or [""])[0]
                snap = (outer.snapshot_for(prog)
                        if prog else None) or outer.snapshot()
                self._send(create_html(
                    snap, snap["program"],
                    snap.get("fonts") or []).encode(),
                    "text/html; charset=utf-8")
            elif parts.path == "/edit":
                q = urllib.parse.parse_qs(parts.query)
                page = self._edit_page((q.get("image") or [""])[0])
                if page is None:
                    self._send(b"no such image", "text/plain", 404)
                else:
                    self._send(page, "text/html; charset=utf-8")
            elif parts.path == "/api/state":
                self._json({"ok": True, "state": outer.snapshot()})
            elif parts.path == "/api/destfile":
                q = urllib.parse.parse_qs(parts.query)
                name = (q.get("program") or [""])[0] or outer.program_name
                if not name or name not in _programs():
                    self._send(b"no such program", "text/plain", 404)
                    return
                try:
                    with open(_dest_path(name), "rb") as f:
                        raw = f.read()
                except OSError:
                    self._send(b"not found", "text/plain", 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}.dest"')
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif parts.path == "/api/img":
                q = urllib.parse.parse_qs(parts.query)
                p = (q.get("path") or [""])[0]
                full = os.path.normpath(os.path.join(THIS_DIR, p))
                if not full.startswith(BITMAPS_DIR + os.sep):
                    self._send(b"not found", "text/plain", 404)
                    return
                try:
                    with open(full, "rb") as f:
                        self._send(f.read(), "image/png")
                except OSError:
                    self._send(b"not found", "text/plain", 404)
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/show":
                    # board page form (urlencoded): retune + redirect.
                    # Never JSON: the form must survive intact.
                    try:
                        ln = int(self.headers.get("Content-Length", 0))
                    except (TypeError, ValueError):
                        ln = 0
                    form = urllib.parse.parse_qs(
                        self.rfile.read(ln).decode(),
                        keep_blank_values=True)
                    g = lambda k: (form.get(k) or [""])[0] or None
                    try:
                        outer.show(g("program"), g("service"),
                                   g("destination"))
                    except (ValueError, SystemExit, OSError) as e:
                        self._send(
                            f"<html><body><p>show failed: "
                            f"{html.escape(str(e))}</p>"
                            f"<p><a href=\"/\">back</a></p></body></html>"
                            .encode(), "text/html; charset=utf-8", 400)
                        return
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                if path == "/api/show":
                    b = self._body() or {}
                    outer.show(b.get("program"),
                               b.get("service"), b.get("destination"))
                    self._ok()
                elif path == "/api/program/create":
                    b = self._body() or {}
                    outer.create_program(b.get("name", ""))
                    self._ok()
                elif path == "/api/program/upload":
                    b = self._body() or {}
                    outer.upload_program(b.get("name", ""),
                                         b.get("text", ""))
                    self._ok()
                elif path == "/api/program/delete":
                    b = self._body() or {}
                    outer.delete_program(b.get("program"))
                    self._ok()
                elif path == "/api/defaults":
                    self._defaults(self._body() or {})
                elif path == "/api/service/add":
                    b = self._body() or {}
                    svc = str(b.get("service", "")).strip()
                    if not svc:
                        raise ValueError("name the service (e.g. 43)")

                    def _add(data):
                        svcs = data.setdefault("services", {})
                        if svc in svcs:
                            raise ValueError(f"service '{svc}' exists")
                        svcs[svc] = {}
                        return f"Added service '{svc}'"
                    outer.message = _mutate(b.get("program"), _add)
                    outer.refresh()
                    self._ok()
                elif path == "/api/service/delete":
                    b = self._body() or {}

                    def _del(data):
                        svcs = data.get("services") or {}
                        if b.get("service") not in svcs:
                            raise ValueError("no such service")
                        del svcs[b.get("service")]
                        if not svcs:
                            raise ValueError("program needs a service")
                        return "Deleted service"
                    outer.message = _mutate(b.get("program"), _del)
                    if outer.service_name == b.get("service"):
                        outer.service_name = outer.dest_name = None
                    outer.refresh()
                    self._ok()
                elif path == "/api/service/rename":
                    self._rename_service(self._body() or {})
                elif path == "/api/destination/add":
                    self._dest_upsert(self._body() or {}, need_new=True)
                elif path == "/api/destination/edit":
                    self._edit_destination(self._body() or {})
                elif path == "/api/destination/update":
                    self._dest_upsert(self._body() or {}, need_new=False)
                elif path == "/api/destination/delete":
                    b = self._body() or {}

                    def _del2(data):
                        svcs = data.get("services") or {}
                        dests = svcs.get(b.get("service") or "", {})
                        if b.get("destination") not in dests:
                            raise ValueError("no such destination")
                        del dests[b.get("destination")]
                        # may leave the service empty (valid: under
                        # construction, yields no screens)
                        return "Deleted destination"
                    outer.message = _mutate(b.get("program"), _del2)
                    outer.refresh()
                    self._ok()
                elif path == "/api/bitmap/upload":
                    self._bmp_upload(self._body(8 * 1024 * 1024) or {})
                elif path == "/api/flash":
                    b = self._body() or {}
                    try:
                        outer.flash(b.get("image"),
                                    seconds=b.get("seconds", 10))
                    except ValueError as e:
                        self._fail(e)
                        return
                    self._ok()
                elif path == "/api/render-preview":
                    self._render_preview(self._body() or {})
                elif path == "/api/create-text":
                    self._create_text(self._body() or {})
                elif path == "/api/bitmap/edit":
                    self._bmp_edit(self._body(8 * 1024 * 1024) or {})
                elif path == "/api/bitmap/delete":
                    b = self._body() or {}

                    def _rm(data):
                        svcs = data.get("services") or {}
                        e = (svcs.get(b.get("service") or "") or {}).get(
                            b.get("destination") or "")
                        if not isinstance(e, dict):
                            raise ValueError("no such destination")
                        lst = e.get("bitmaps") or []
                        if b.get("image") not in lst:
                            raise ValueError("no such page")
                        lst.remove(b.get("image"))
                        return "Deleted page"
                    outer.message = _mutate(b.get("program"), _rm)
                    outer.refresh()
                    self._ok()
                else:
                    self._send(b"not found", "text/plain", 404)
            except ValueError as e:
                self._fail(e)
            except OSError as e:
                # e.g. portal running dropped-to-daemon under a locked-down
                # home dir: report it in the UI instead of dropping the
                # connection with a traceback.
                self._fail(f"filesystem error: {e}")

        def _defaults(self, b):
            d = b.get("defaults") or {}

            def _apply(data):
                dd = data.setdefault("defaults", {})
                if "colour" in d or "color" in d:
                    c = destfile.parse_colour(
                        d.get("colour", d.get("color")), "colour")
                    if c is None:
                        raise ValueError("colour must be #rrggbb")
                    new_c = "full" if c == "full" else "#%02x%02x%02x" % c
                    if str(dd.get("colour", "")).lower() != new_c.lower():
                        dd["colour"] = new_c
                if "rotation_speed" in d and d["rotation_speed"] not in (
                        None, ""):
                    try:
                        r = float(d["rotation_speed"])
                    except (TypeError, ValueError):
                        raise ValueError("rotation_speed must be a number")
                    if r <= 0:
                        raise ValueError("rotation_speed must be positive")
                    try:
                        same = float(dd.get("rotation_speed")) == r
                    except (TypeError, ValueError):
                        same = False
                    if not same:
                        dd["rotation_speed"] = r
                for k in ("px_width", "px_height"):
                    if k in d and d[k] not in (None, ""):
                        try:
                            v = int(float(d[k]))
                        except (TypeError, ValueError):
                            raise ValueError(f"{k} must be a whole number")
                        if v <= 0:
                            raise ValueError(f"{k} must be positive")
                        try:
                            same = int(float(dd.get(k))) == v
                        except (TypeError, ValueError):
                            same = False
                        if not same:
                            dd[k] = v
                return "Saved defaults"
            try:
                outer.message = _mutate(b.get("program"), _apply)
            except ValueError as e:
                self._fail(e)
                return
            outer.refresh()
            self._ok()

        def _dest_upsert(self, b, need_new):
            svc = str(b.get("service", "")).strip()
            dest = str(b.get("destination", "")).strip()
            if not svc or not dest:
                self._fail("name the service and destination")
                return
            try:
                ov = _norm_override(b.get("override") or {})
            except ValueError as e:
                self._fail(e)
                return

            def _apply(data):
                svcs = data.setdefault("services", {})
                dests = svcs.setdefault(svc, {})
                if need_new and dest in dests:
                    raise ValueError(f"destination '{dest}' exists")
                e = dests.get(dest)
                if not isinstance(e, dict):
                    e = dests[dest] = {"bitmaps": []}
                if b.get("service_code") not in (None, ""):
                    e["service_code"] = str(b.get("service_code"))
                if b.get("service_name") not in (None, ""):
                    e["service_name"] = str(b.get("service_name"))
                if not e.get("service_code"):
                    # auto-number: position within the service
                    others = [d for d in dests if d != dest]
                    e["service_code"] = "%03d" % (len(others) + 1)
                if not e.get("service_name"):
                    e["service_name"] = dest
                if isinstance(b.get("override"), dict):
                    if ov:
                        e["override"] = ov
                    elif "override" in b and not ov:
                        e.pop("override", None)
                if not isinstance(e.get("bitmaps"), list):
                    e["bitmaps"] = []
                return "Saved destination"
            try:
                outer.message = _mutate(b.get("program"), _apply)
            except ValueError as e:
                self._fail(e)
                return
            outer.refresh()
            self._ok()

        def _move_pages(self, program, service, old_slot, new_slot,
                        new_service=None):
            """Rename a destination's PNG files to a new stem.

            Returns the rewritten bitmaps list. Files not matching the
            expected `<route>-<destination>-<page>.png` pattern are left
            untouched. Raises ValueError on collision.
            """
            pslug = destfile.slug(program)
            svc = new_service or service
            old_dir = f"bitmaps/{pslug}/{destfile.slug(service)}"
            new_dir = f"bitmaps/{pslug}/{destfile.slug(svc)}"
            old_stem = f"{service}-{destfile.slug(old_slot)}"
            new_stem = f"{svc}-{destfile.slug(new_slot)}"
            plan = []
            try:
                with open(_dest_path(program)) as f:
                    cur = json.load(f)
                lst = ((cur.get("services") or {}).get(service, {}).get(
                    old_slot, {}).get("bitmaps") or [])
            except (OSError, ValueError) as e:
                raise ValueError(f"cannot read program: {e}")
            for rel in lst:
                base = os.path.basename(rel)
                if os.path.dirname(rel) == old_dir and \
                        base.startswith(old_stem + "-"):
                    nrel = f"{new_dir}/{new_stem}{base[len(old_stem):]}"
                    sfull = os.path.join(THIS_DIR, rel)
                    nfull = os.path.normpath(os.path.join(THIS_DIR, nrel))
                    if not nfull.startswith(BITMAPS_DIR + os.sep):
                        raise ValueError("bad image path")
                    if os.path.isfile(sfull):
                        if os.path.isfile(nfull):
                            raise ValueError(
                                f"refusing to overwrite {nrel}")
                        plan.append((sfull, nfull, rel, nrel))
                        continue
                plan.append((None, None, rel, rel))
            for sfull, nfull, _old, _new in plan:
                if sfull is None:
                    continue
                os.makedirs(os.path.dirname(nfull), exist_ok=True)
                os.replace(sfull, nfull)
            try:
                os.rmdir(os.path.join(THIS_DIR, old_dir))
            except OSError:
                pass
            return [n for _, _, _, n in plan]

        def _rename_service(self, b):
            prog = b.get("program")
            svc = str(b.get("service", "")).strip()
            new = str(b.get("new_name", "")).strip()
            if not svc or not new:
                self._fail("name the service and its new number")
                return
            if new == svc:
                self._ok()
                return

            def _apply(data):
                svcs = data.get("services") or {}
                if svc not in svcs:
                    raise ValueError(f"no service '{svc}'")
                if new in svcs:
                    raise ValueError(f"service '{new}' exists")
                for dname in list(svcs[svc].keys()):
                    moved = self._move_pages(prog, svc, dname, dname,
                                             new_service=new)
                    svcs[svc][dname]["bitmaps"] = moved
                items = [(new if k == svc else k, v)
                         for k, v in svcs.items()]
                svcs.clear()
                svcs.update(items)
                return f"Renamed service {svc} to {new}"
            try:
                outer.message = _mutate(prog, _apply)
            except ValueError as e:
                self._fail(e)
                return
            if outer.service_name == svc:
                outer.service_name = new
                outer._save_control()
            outer.refresh()
            self._ok()

        def _edit_destination(self, b):
            prog = b.get("program")
            svc = str(b.get("service", "")).strip()
            dest = str(b.get("destination", "")).strip()
            new = str(b.get("new_name", "")).strip() or dest
            if not svc or not dest:
                self._fail("pick a service and destination first")
                return
            try:
                ov = _norm_override(b.get("override") or {})
            except ValueError as e:
                self._fail(e)
                return
            code = b.get("service_code", None)
            name = b.get("service_name", None)

            def _apply(data):
                svcs = data.get("services") or {}
                if svc not in svcs:
                    raise ValueError(f"no service '{svc}'")
                dests = svcs[svc]
                if dest not in dests:
                    raise ValueError(f"no destination '{dest}'")
                e = dests[dest]
                if new != dest:
                    if new in dests:
                        raise ValueError(f"destination '{new}' exists")
                    e["bitmaps"] = self._move_pages(prog, svc, dest, new)
                    items = [(new if k == dest else k, v)
                             for k, v in dests.items()]
                    dests.clear()
                    dests.update(items)
                    e = dests[new]
                    if e.get("service_name", dest) == dest:
                        e["service_name"] = new
                if code not in (None, ""):
                    e["service_code"] = str(code)
                if name not in (None, ""):
                    e["service_name"] = str(name)
                if isinstance(b.get("override"), dict):
                    if ov:
                        e["override"] = ov
                    else:
                        e.pop("override", None)
                return "Saved destination"
            try:
                outer.message = _mutate(prog, _apply)
            except ValueError as e:
                self._fail(e)
                return
            if outer.service_name == svc and outer.dest_name == dest:
                outer.dest_name = new
                outer._save_control()
            outer.refresh()
            self._ok()

        def _bmp_upload(self, b):
            data = str(b.get("data", "") or "")
            if "," in data and data.startswith("data:"):
                data = data.split(",", 1)[1]
            try:
                png = base64.b64decode(data, validate=True)
            except Exception:
                self._fail("bad image data (need PNG dataURL)")
                return
            if len(png) > 6 * 1024 * 1024:
                self._fail("PNG too large (max 6MB)")
                return
            try:
                outer.add_bitmap(b.get("program"), b.get("service"),
                                 b.get("destination"),
                                 b.get("filename", "upload.png"), png)
            except ValueError as e:
                self._fail(e)
                return
            self._ok()

        def _render_preview(self, b):
            try:
                png, info = render_text_png(
                    b.get("route", ""), b.get("destination", ""),
                    b.get("via", ""), b.get("style", "top"),
                    b.get("colour", "#DB9600") or "#DB9600",
                    fonts=b.get("fonts") or {})
            except ValueError as e:
                self._fail(e)
                return
            warnings = list(info.get("warnings", []))
            flashed = False
            if b.get("board"):
                try:
                    rel = outer.stage_preview(png)
                    outer.flash(rel, colour="full", seconds=10)
                    flashed = True
                except ValueError as e:
                    warnings.append(f"board flash failed: {e}")
            self._json({"ok": True,
                        "data": "data:image/png;base64," + base64.b64encode(
                            png).decode(),
                        "lit": info.get("lit", 0),
                        "warnings": warnings,
                        "flashed": flashed})

        def _edit_page(self, rel):
            """Editor HTML for one bitmap, or None."""
            rel = str(rel or "")
            full = os.path.normpath(os.path.join(THIS_DIR, rel))
            if not full.startswith(BITMAPS_DIR + os.sep):
                return None
            if not os.path.isfile(full):
                return None
            try:
                w, h, _ = images.decode_png(full)
            except SystemExit:
                return None
            try:
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError:
                return None
            if len(raw) > 6 * 1024 * 1024:
                return None
            return edit_html(
                rel, w, h,
                "data:image/png;base64," + base64.b64encode(raw).decode()
            ).encode()

        def _bmp_edit(self, b):
            """Save pixels from the /edit page back over a bitmap."""
            rel = str(b.get("image", "") or "")
            full = os.path.normpath(os.path.join(THIS_DIR, rel))
            if not full.startswith(BITMAPS_DIR + os.sep):
                self._fail("bad image path")
                return
            if not os.path.isfile(full):
                self._fail("no such image")
                return
            data = str(b.get("data", "") or "")
            if "," in data and data.startswith("data:"):
                data = data.split(",", 1)[1]
            try:
                png = base64.b64decode(data, validate=True)
            except Exception:
                self._fail("bad image data (need PNG dataURL)")
                return
            if len(png) > 6 * 1024 * 1024:
                self._fail("PNG too large (max 6MB)")
                return
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                self._fail("not a PNG file")
                return
            tmp = full + ".tmp"
            try:
                with open(tmp, "wb") as f:
                    f.write(png)
                try:
                    images.decode_png(tmp)
                except SystemExit as e:
                    raise ValueError(f"unreadable PNG: {e}")
                os.replace(tmp, full)
            except (OSError, ValueError) as e:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                self._fail(str(e) or "save failed")
                return
            outer.message = f"Saved {os.path.basename(full)}"
            self._ok()

        def _create_text(self, b):
            try:
                outer.create_text(
                    b.get("program"), b.get("service"),
                    b.get("slot", ""),
                    {"route": b.get("route", ""),
                     "dest": b.get("text", ""),
                     "via": b.get("via", ""),
                     "style": b.get("style", "top"),
                     "colour": b.get("colour", "#DB9600") or "#DB9600",
                     "fonts": b.get("fonts") or {}},
                    service_code=b.get("service_code", ""),
                    rotation=b.get("rotation", ""))
            except ValueError as e:
                self._fail(e)
                return
            self._ok()

    print(f"portal on :{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Destination board portal")
    ap.add_argument("--port", type=int, default=4040)
    ap.add_argument("--program", default=None)
    args = ap.parse_args()
    os.makedirs(PROGRAMS_DIR, exist_ok=True)
    os.makedirs(BITMAPS_DIR, exist_ok=True)
    ctl = Controller(args.program,
                     control=os.path.join(THIS_DIR, "board_control.json"))
    serve(ctl, args.port)


if __name__ == "__main__":
    main()
