#!/usr/bin/env python3
"""Web portal for the destination board (stdlib only).

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
import json
import os
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

import destfile
import render

PROGRAMS_DIR = os.path.join(THIS_DIR, "programs")
BITMAPS_DIR = os.path.join(THIS_DIR, "bitmaps")


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
    """Load a program, apply fn(data) -> message, save. Returns message."""
    path = _dest_path(program)
    if not os.path.isfile(path):
        raise ValueError(f"no program '{program}'")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read program: {e}")
    msg = fn(data)
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


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Destination board</title>
<style>
*{box-sizing:border-box}
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
button:disabled{opacity:.5;cursor:default}
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
.strip button{font-size:11px;padding:2px 8px;margin-top:2px}
code{background:#000;padding:1px 5px;border-radius:4px;color:#ffb000}
label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:#aaa}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
</style></head><body>
<h1>Destination board</h1>
<div class="wrap">
<div class="card"><h2>Now showing (board follows)</h2>
<div class="row">
<select id="sprog"></select><select id="ssvc"></select>
<select id="sdest"></select>
<button onclick="show()">Show on board</button>
</div>
<div class="showing" id="showing"></div>
<div class="hint" id="msg"></div></div>
<div class="card"><h2>Programs (.dest)</h2>
<div class="row">
<input id="newname" placeholder="new program e.g. citybus" size="18">
<button onclick="createProgram()">Create</button>
<input type="file" id="destfile" accept=".dest,.json,application/json">
<button class="ghost" onclick="uploadProgram()">Upload .dest</button>
<button class="danger" onclick="deleteProgram()">Delete</button>
</div>
<div class="hint">Programs live in <code>programs/*.dest</code>, bitmaps in
<code>bitmaps/&lt;program&gt;/&lt;route&gt;/&lt;route&gt;-&lt;destination&gt;-&lt;page&gt;.png</code>.
<a id="dl" href="#" style="color:#8cf">download this .dest</a></div></div>
<div class="card"><h2>Defaults</h2>
<div class="row">
<label>colour<input id="dcolour" size="8"></label>
<label>rotation secs<input id="drot" size="5"></label>
<label>px width<input id="dpxw" size="5"></label>
<label>px height<input id="dpxh" size="5"></label>
<button onclick="saveDefaults()">Save defaults</button>
</div>
<div class="hint">colour <code>#rrggbb</code> tints every page unless a
destination overrides it with <code>full</code> (keep bitmap colours).</div></div>
<div class="card"><h2>Services &amp; destinations</h2>
<div class="row">
<input id="nsvc" placeholder="service e.g. 43" size="8">
<button class="ghost" onclick="addService()">Add service</button>
<input id="ndest" placeholder="destination e.g. Sheffield" size="14">
<input id="ncode" placeholder="code e.g. 001" size="7">
<input id="nname" placeholder="service name" size="14">
<button class="ghost" onclick="addDestination()">Add destination</button>
</div>
<div id="svcs"></div></div>
<div class="card"><h2>Upload bitmap page</h2>
<div class="row">
<input type="file" id="upfile" accept=".png,image/png">
<button onclick="uploadBitmap()">Upload + append page</button>
</div>
<div class="hint" id="upmsg">PNG only (ideally 240x40). Saved as
<code>&lt;route&gt;-&lt;destination&gt;-&lt;next-page&gt;.png</code> and
appended to the destination in order.</div></div>
<div class="card"><h2>Create page from text (fonts/ only)</h2>
<div class="grid">
<label>service (route no.)<input id="tservice" placeholder="e.g. 43"></label>
<label>destination slot<input id="tslot" placeholder="e.g. Sheffield"></label>
<label>route text<input id="troute" placeholder="e.g. 43"></label>
<label>destination text<input id="ttext" placeholder="e.g. Sheffield"></label>
<label>via (optional)<input id="tvia" placeholder="e.g. Dronfield"></label>
<label>layout<select id="tstyle"><option value="top">top - via over dest</option><option value="bottom">bottom - dest over via</option><option value="left">left - via | dest</option><option value="right">right - dest | via</option></select></label>
<label>colour<input id="tcolour" value="#DB9600"></label>
<label>rotation secs (optional)<input id="trot" placeholder="default"></label>
<label>route font<select id="troute_font"></select></label>
<label>route scale<select id="troute_scale"><option>1</option><option selected>2</option><option>3</option><option>4</option></select></label>
<label>dest font<select id="tdest_font"></select></label>
<label>dest scale<select id="tdest_scale"><option selected>1</option><option>2</option><option>3</option><option>4</option></select></label>
<label>via font<select id="tvia_font"></select></label>
<label>via scale<select id="tvia_scale"><option selected>1</option><option>2</option><option>3</option><option>4</option></select></label>
</div>
<div class="row"><button class="ghost" onclick="previewText()">Preview</button>
<button onclick="createText()">Create + show on board</button></div>
<img id="tpreview" alt="preview" style="width:100%;max-width:480px;height:80px;object-fit:contain;background:#000;border-radius:6px;border:1px solid #3a3a42;display:none;image-rendering:pixelated;margin-top:8px">
<div class="hint" id="tmsg">Rendered with BDF bitmap fonts from
<code>fonts/</code> only - no system fonts. Saved as the next page of the
slot and shown on the board.</div></div>
</div>
<script>
var S=null;
function el(id){return document.getElementById(id);}
async function api(path,body){
 var r=await fetch(path,{method:body===undefined?'GET':'POST',
  headers:{'Content-Type':'application/json'},
  body:body===undefined?undefined:JSON.stringify(body)});
 var j=await r.json();
 if(!j.ok){el('msg').textContent=j.error||'failed';
  el('msg').className='err';return null;}
 return j;
}
function opts(sel,items,cur,allLabel){
 var s=el(sel);s.innerHTML='';
 if(allLabel){var o=document.createElement('option');o.value='';
  o.textContent=allLabel;s.appendChild(o);}
 items.forEach(function(v){var o=document.createElement('option');
  o.value=v;o.textContent=v;if(v===cur)o.selected=true;s.appendChild(o);});
}
function render(){
 if(!S)return;
 opts('sprog',S.programs,S.program);
 var svc=S.services.find(function(x){return x.number===S.service;});
 var svcs=S.services.map(function(x){return x.number;});
 opts('ssvc',svcs,S.service,'all services');
 var dests=svc?svc.destinations.map(function(d){return d.name;}):[];
 opts('sdest',dests,S.destination,'all destinations');
 el('showing').innerHTML='board: <b>'+esc(S.program||'-')+' / '+
  esc(S.service||'all')+' / '+esc(S.destination||'all')+'</b> ('+
  S.screens.length+' pages)';
 el('msg').textContent=S.message||'';el('msg').className='';
 if(S.defaults){el('dcolour').value=S.defaults.colour;
  el('drot').value=S.defaults.rotation_speed;
  el('dpxw').value=S.defaults.px_width;el('dpxh').value=S.defaults.px_height;}
 el('dl').href='/api/destfile?program='+encodeURIComponent(S.program||'');
 var box=el('svcs');box.innerHTML='';
 S.services.forEach(function(sv){
  var h=document.createElement('h2');
  h.textContent='Service '+sv.number+' ';
  var del=document.createElement('button');del.textContent='delete service';
  del.className='danger';
  del.onclick=function(){call('/api/service/delete',
   {program:S.program,service:sv.number});};
  h.appendChild(del);box.appendChild(h);
  sv.destinations.forEach(function(d){
   var t=document.createElement('table');t.innerHTML='';
   var tr=document.createElement('tr');
   tr.innerHTML='<td><b>'+esc(d.name)+'</b><br><span class="hint">code '+
    esc(d.service_code||'-')+' / '+esc(d.service_name||d.name)+'</span></td>'+
    '<td>colour <input data-k="colour" value="'+esc(d.override.colour||'')+
    '" placeholder="default" size="8"><br>'+
    '<span class="hint">use <code>full</code> for bitmap colours</span></td>'+
    '<td>rotation <input data-k="rotation_speed" value="'+
    esc(d.override.rotation_speed??'')+'" placeholder="default" size="5"></td>'+
    '<td><button>save</button> <button class="danger">delete</button></td>';
   var inputs=tr.querySelectorAll('input');
   tr.querySelectorAll('button')[0].onclick=function(){
    var ov={};inputs.forEach(function(i){ov[i.dataset.k]=i.value;});
    call('/api/destination/update',{program:S.program,service:sv.number,
     destination:d.name,override:ov});};
   tr.querySelectorAll('button')[1].onclick=function(){
    if(confirm('Delete '+d.name+'?'))
     call('/api/destination/delete',{program:S.program,service:sv.number,
      destination:d.name});};
   box.appendChild(tr);
   var strip=document.createElement('div');strip.className='strip';
   d.bitmaps.forEach(function(img,i){
    var f=document.createElement('figure');
    var im=document.createElement('img');
    im.src='/api/img?path='+encodeURIComponent(img)+'&t='+Date.now();
    im.title=img;im.loading='lazy';
    var cap=document.createElement('figcaption');cap.textContent='page '+(i+1);
    var b=document.createElement('button');b.textContent='delete';
    b.className='danger';
    b.onclick=function(){if(confirm('Delete page '+(i+1)+'?'))
     call('/api/bitmap/delete',{program:S.program,service:sv.number,
      destination:d.name,image:img});};
    f.appendChild(im);f.appendChild(cap);f.appendChild(b);
    strip.appendChild(f);});
   if(!d.bitmaps.length){var p=document.createElement('div');
    p.className='hint';p.textContent='no pages yet - upload a PNG below.';
    strip.appendChild(p);}
   box.appendChild(strip);});
  });
 fillFonts();
}
function esc(s){return String(s??'').replace(/[&<>"]/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function call(path,body){var j=await api(path,body);
 if(j){S=j.state;render();}}
async function poll(){var j=await api('/api/state');if(j){S=j.state;render();}}
async function show(){await call('/api/show',{program:el('sprog').value,
 service:el('ssvc').value||null,destination:el('sdest').value||null});}
async function createProgram(){await call('/api/program/create',
 {name:el('newname').value});}
async function deleteProgram(){if(confirm('Delete program?'))
 await call('/api/program/delete',{program:el('sprog').value});}
async function uploadProgram(){var f=el('destfile').files[0];
 if(!f){el('msg').textContent='pick a .dest file first';
  el('msg').className='err';return;}
 var text=await f.text();
 var name=f.name.replace(/\\.dest$/i,'');
 await call('/api/program/upload',{name:name,text:text});}
async function saveDefaults(){await call('/api/defaults',
 {program:S.program,defaults:{colour:el('dcolour').value,
  rotation_speed:el('drot').value,px_width:el('dpxw').value,
  px_height:el('dpxh').value}});}
async function addService(){await call('/api/service/add',
 {program:S.program,service:el('nsvc').value});}
async function addDestination(){await call('/api/destination/add',
 {program:S.program,service:S.service||el('nsvc').value,
  destination:el('ndest').value,service_code:el('ncode').value,
  service_name:el('nname').value});}
async function uploadBitmap(){var f=el('upfile').files[0];
 if(!f){el('upmsg').textContent='pick a PNG file first';return;}
 el('upmsg').textContent='uploading...';
 var rd=new FileReader();
 rd.onload=async function(){
  var j=await api('/api/bitmap/upload',{program:S.program,
   service:S.service,destination:S.destination,filename:f.name,data:rd.result});
  if(j){S=j.state;render();el('upmsg').textContent='saved - on screen now';}};
  rd.readAsDataURL(f);}
function textForm(){return {program:S.program,service:el('tservice').value,
 slot:el('tslot').value,route:el('troute').value,text:el('ttext').value,
 via:el('tvia').value,style:el('tstyle').value,
 colour:el('tcolour').value||'#DB9600',rotation:el('trot').value,
 fonts:{route:el('troute_font').value,route_scale:el('troute_scale').value,
  dest:el('tdest_font').value,dest_scale:el('tdest_scale').value,
  via:el('tvia_font').value,via_scale:el('tvia_scale').value}};}
async function previewText(){var m=el('tmsg');m.textContent='rendering...';
 var r=await fetch('/api/render-preview',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(textForm())});
 var j=await r.json();
 if(!j.ok){m.textContent=j.error||'preview failed';return;}
 var im=el('tpreview');im.src=j.data;im.style.display='block';
 m.textContent=j.lit+' lit pixels'+(j.warnings.length?' - '+j.warnings.join('; '):'');}
async function createText(){var m=el('tmsg');m.textContent='creating...';
 var j=await api('/api/create-text',textForm());
 if(j){S=j.state;render();m.textContent='saved - on screen now';}}
function fillFonts(){var fonts=S.fonts||[];
 [['troute_font','10x20.bdf'],['tdest_font','10x20.bdf'],
  ['tvia_font','6x13B.bdf']].forEach(function(p){
  var s=el(p[0]);if(s.options.length)return;
  fonts.forEach(function(f){var o=document.createElement('option');
   o.value=f;o.textContent=f;if(f===p[1])o.selected=true;s.appendChild(o);});});
 if(!el('tservice').value)el('tservice').value=S.service||'';
 if(!el('tslot').value)el('tslot').value=S.destination||'';
 if(!el('troute').value)el('troute').value=S.service||'';
 if(!el('ttext').value)el('ttext').value=S.destination||'';}
el('sprog').addEventListener('change',function(){
 call('/api/show',{program:el('sprog').value,service:null,destination:null});});
el('ssvc').addEventListener('change',async function(){
 S.service=el('ssvc').value||null;S.destination=null;render();});
setInterval(poll,3000);poll();
</script></body></html>
"""


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
                self._send(PAGE.encode(), "text/html")
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
                elif path == "/api/destination/add":
                    self._dest_upsert(self._body() or {}, need_new=True)
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
                        if not dests:
                            raise ValueError(
                                "service needs a destination")
                        return "Deleted destination"
                    outer.message = _mutate(b.get("program"), _del2)
                    outer.refresh()
                    self._ok()
                elif path == "/api/bitmap/upload":
                    self._bmp_upload(self._body(8 * 1024 * 1024) or {})
                elif path == "/api/render-preview":
                    self._render_preview(self._body() or {})
                elif path == "/api/create-text":
                    self._create_text(self._body() or {})
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
                        if not lst:
                            raise ValueError(
                                "destination needs at least one page")
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
                    dd["colour"] = "full" if c == "full" else \
                        "#%02x%02x%02x" % c
                if "rotation_speed" in d and d["rotation_speed"] not in (
                        None, ""):
                    try:
                        r = float(d["rotation_speed"])
                    except (TypeError, ValueError):
                        raise ValueError("rotation_speed must be a number")
                    if r <= 0:
                        raise ValueError("rotation_speed must be positive")
                    dd["rotation_speed"] = r
                for k in ("px_width", "px_height"):
                    if k in d and d[k] not in (None, ""):
                        try:
                            v = int(float(d[k]))
                        except (TypeError, ValueError):
                            raise ValueError(f"{k} must be a whole number")
                        if v <= 0:
                            raise ValueError(f"{k} must be positive")
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
            self._json({"ok": True,
                        "data": "data:image/png;base64," + base64.b64encode(
                            png).decode(),
                        "lit": info.get("lit", 0),
                        "warnings": info.get("warnings", [])})

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
