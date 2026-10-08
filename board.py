#!/usr/bin/env python3
"""Destination board runner: plays `.dest` bitmap programs on the LED matrix.

A program is a `.dest` JSON file (see destfile.py) mapping service
numbers -> destinations -> ordered bitmap pages. Bitmaps live under:

    bitmaps/<program>/<route>/<route>-<destination>-<page>.png

Run from the project root - bitmap paths in the .dest are
project-root-relative.

    python3 board.py programs/example.dest --service 43 --mock --once
    python3 board.py programs/example.dest --service 43 --preview
    python3 board.py programs/example.dest --list
    sudo python3 board.py programs/example.dest --service 43 --portal
    python3 board.py programs/example.dest --serve --port 4040

Live control: the portal (same process via --portal, or another one
via board_control.json) picks program/service/destination and the
matrix follows within ~0.5s. The CLI selection is the startup pick;
the control file takes over when it changes afterwards.
"""

import argparse
import json
import os
import sys
import time

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, THIS_DIR)

import destfile
from destfile import colour_str
from images import dim_frame, load_frame, tint_frame

CONTROL_FILE = os.path.join(THIS_DIR, "board_control.json")
PROGRAMS_DIR = os.path.join(THIS_DIR, "programs")


def _resolve_root(path):
    """Project-root-relative bitmap path -> absolute path."""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(THIS_DIR, path))


def read_control(path=CONTROL_FILE):
    """Selection written by the portal: (program, service, destination).

    None when absent/unreadable - the CLI selection stands."""
    try:
        with open(path) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict):
        return None
    p = c.get("program")
    if p is not None and (not isinstance(p, str) or not p):
        return None
    for k in ("service", "destination"):
        v = c.get(k)
        if v is not None and not isinstance(v, str):
            return None
    if not isinstance(p, str) or not p:
        return None
    return (p, c.get("service"), c.get("destination"))


def write_control(program, service=None, destination=None,
                  path=CONTROL_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"program": program, "service": service,
                   "destination": destination}, f)
        f.write("\n")
    os.replace(tmp, path)


def find_dest_file(program):
    """Resolve a program name (or path) to a .dest file on disk."""
    if program is None:
        return None
    p = str(program)
    cands = [p]
    if not p.lower().endswith(".dest"):
        cands.append(os.path.join(PROGRAMS_DIR, p + ".dest"))
        cands.append(os.path.join(PROGRAMS_DIR, p))
    else:
        cands.append(os.path.join(PROGRAMS_DIR, os.path.basename(p)))
    for c in cands:
        if os.path.isfile(c):
            return c
        if os.path.isfile(_resolve_root(c)):
            return _resolve_root(c)
    return None


def load_program(program, service=None, destination=None):
    """Load a .dest program and resolve a pick to screens."""
    path = find_dest_file(program)
    if path is None:
        raise SystemExit(f"program '{program}' not found "
                         f"(looked in {PROGRAMS_DIR})")
    data = destfile.load_dest(path)
    prog = destfile.resolve(data, service, destination)
    prog["program"] = destfile.program_name_for(path)
    prog["path"] = path
    return prog


def run_program(args, prog):
    try:
        from rgbmatrix import RGBMatrix, RGBMatrixOptions
    except ImportError:
        raise SystemExit(
            "rgbmatrix is not installed in this venv (LED driver missing).\n"
            "Build it with ./install.sh, or test without hardware:\n"
            "  python3 board.py --list\n"
            "  python3 board.py programs/example.dest --mock --once\n"
            "  python3 board.py programs/example.dest --preview")

    options = RGBMatrixOptions()
    options.rows = args.led_rows
    options.cols = args.led_cols
    options.chain_length = args.led_chain
    options.parallel = args.led_parallel
    options.hardware_mapping = args.led_gpio_mapping
    options.brightness = args.led_brightness
    options.pwm_bits = args.led_pwm_bits
    options.limit_refresh_rate_hz = args.led_limit_refresh
    options.gpio_slowdown = args.led_slowdown_gpio
    options.led_rgb_sequence = args.led_rgb_sequence
    options.pixel_mapper_config = args.led_pixel_mapper
    options.show_refresh_rate = 1 if args.led_show_refresh else 0
    if args.led_no_hardware_pulse:
        options.disable_hardware_pulsing = True

    matrix = RGBMatrix(options=options)
    offscreen = matrix.CreateFrameCanvas()
    W, H = offscreen.width, offscreen.height

    frames = []
    for s in prog["screens"]:
        full = _resolve_root(s["image"])
        sw, sh, frame = load_frame(full, W, H, args.image_fit)
        if s.get("colour") not in ("full", None):
            frame = tint_frame(frame, s["colour"])
        frame = dim_frame(frame, args.image_dim)
        frames.append((s, frame))
        tag = f" [{s['service']} {s['destination']}]" \
            if s.get("destination") else ""
        ctag = f" {colour_str(s.get('colour'))}" if s.get("colour") else ""
        print(f"image {s['image']}: {sw}x{sh} -> {args.image_fit} {W}x{H} "
              f"({s['seconds']:g}s){tag}{ctag}",
              file=sys.stderr, flush=True)
    print(f"program '{prog['program']}' {W}x{H} screens={len(frames)}",
          file=sys.stderr, flush=True)

    def blit(frame):
        for y in range(H):
            o = y * W * 3
            for x in range(W):
                offscreen.SetPixel(x, y, frame[o], frame[o + 1],
                                   frame[o + 2])
                o += 3

    idx = 0
    idx_since = time.time()
    shown = None
    while True:
        now = time.time()
        if len(frames) > 1 and \
                now - idx_since >= frames[idx % len(frames)][0]["seconds"]:
            idx = (idx + 1) % len(frames)
            idx_since = now
        s, frame = frames[idx % len(frames)]
        # static screens draw once: rewriting an identical buffer every
        # cycle just burns CPU and can judder the refresh.
        if shown != idx % len(frames):
            offscreen.Fill(0, 0, 0)
            blit(frame)
            offscreen = matrix.SwapOnVSync(offscreen)
            shown = idx % len(frames)
        if args.once:
            break
        if getattr(args, "_watch", None) is not None and args._watch():
            break
        time.sleep(0.5)


def run_dynamic(args, ctl):
    """Matrix loop following the live portal selection."""
    last = None
    last_file = read_control()
    while True:
        ctl.refresh()
        cur_file = read_control()
        if cur_file != last_file:
            last_file = cur_file
            if cur_file is not None and cur_file != ctl.selection():
                if ctl.adopt(*cur_file):
                    print(f"control: showing {cur_file[0]} / "
                          f"{cur_file[1] or 'all'} / "
                          f"{cur_file[2] or 'all'}",
                          file=sys.stderr, flush=True)
                else:
                    print(f"control: ignoring {cur_file}",
                          file=sys.stderr, flush=True)
        key = ctl.key()
        if key != last:
            try:
                prog = ctl.resolve()
            except SystemExit as e:
                print(f"selection failed ({e}); keeping screens",
                      file=sys.stderr, flush=True)
                time.sleep(2)
                continue
            last = key
            args._watch = lambda k=key: bool(
                ctl.refresh() or ctl.key() != k or
                read_control() != last_file)
            run_program(args, prog)
            args._watch = None
            if args.once:
                break
        else:
            time.sleep(0.5)


def main():
    p = argparse.ArgumentParser(description="Destination board runner")
    p.add_argument("program_file", nargs="?",
                   help=".dest file, or a program name from programs/")
    p.add_argument("--program", default=None,
                   help="Program name (unneeded when program_file is given)")
    p.add_argument("--service", default=None,
                   help="Show only this service number "
                        "(default: every service in file order)")
    p.add_argument("--destination", default=None,
                   help="Show only this destination (default: every "
                        "destination in file order)")
    p.add_argument("--list", action="store_true",
                   help="List programs / services and exit")
    p.add_argument("--image-fit", default="fit",
                   choices=["fit", "fill", "stretch"],
                   help="How bitmaps map to the panel")
    p.add_argument("--image-dim", type=int, default=100,
                   help="Dim bitmaps to PCT%% brightness (default 100)")
    p.add_argument("--mock", action="store_true",
                   help="Print the program instead of driving the matrix")
    p.add_argument("--once", action="store_true",
                   help="Show the first screen, then exit")
    p.add_argument("--preview", action="store_true",
                   help="ASCII preview of every screen (no hardware needed)")
    p.add_argument("--portal", action="store_true",
                   help="Host the web portal alongside the matrix")
    p.add_argument("--port", type=int, default=4040,
                   help="Web portal port (default 4040)")
    p.add_argument("--serve", action="store_true",
                   help="Host the web portal only, without the matrix")
    p.add_argument("--led-rows", type=int, default=40)
    p.add_argument("--led-cols", type=int, default=80)
    p.add_argument("--led-chain", type=int, default=3)
    p.add_argument("--led-parallel", type=int, default=1)
    p.add_argument("--led-gpio-mapping", default="regular")
    p.add_argument("--led-brightness", type=int, default=60)
    p.add_argument("--led-pwm-bits", type=int, default=8)
    p.add_argument("--led-limit-refresh", type=int, default=0)
    p.add_argument("--led-slowdown-gpio", type=int, default=2)
    p.add_argument("--led-rgb-sequence", default="RGB")
    p.add_argument("--led-pixel-mapper", default="")
    p.add_argument("--led-show-refresh", action="store_true")
    p.add_argument("--led-no-hardware-pulse", action="store_true",
                   default=True)
    args = p.parse_args()

    if not 1 <= args.image_dim <= 100:
        raise SystemExit("--image-dim must be 1-100")

    # --list with no program: list available .dest files
    if args.list and not args.program_file and not args.program:
        try:
            names = sorted(f[:-5] for f in os.listdir(PROGRAMS_DIR)
                           if f.endswith(".dest"))
        except OSError:
            names = []
        if not names:
            print(f"no .dest programs in {PROGRAMS_DIR}")
        for n in names:
            try:
                data = destfile.load_dest(
                    os.path.join(PROGRAMS_DIR, n + ".dest"))
                svcs = destfile.list_services(data)
                print(f"{n}: services {', '.join(svcs)}")
                for s in svcs:
                    for d in destfile.list_destinations(data, s):
                        e = destfile.entry_of(data, s, d) or {}
                        nimg = len(e.get("bitmaps") or [])
                        print(f"    {s} / {d} "
                              f"(code {e.get('service_code', '-')}) "
                              f"x{nimg}")
            except SystemExit as e:
                print(f"{n}: {e}")
        return

    program = args.program_file or args.program
    if program is None:
        # single program in programs/: pick it automatically
        try:
            names = [f for f in os.listdir(PROGRAMS_DIR)
                     if f.endswith(".dest")]
        except OSError:
            names = []
        if len(names) == 1:
            program = os.path.join(PROGRAMS_DIR, names[0])
        else:
            p.error("give a .dest program file (or --program NAME)")

    if args.serve or args.portal or \
            (not args.mock and not args.preview and not args.list):
        from portal import Controller, serve
        ctl = Controller(program, service=args.service,
                         dest=args.destination, control=CONTROL_FILE)
        if args.serve:
            serve(ctl, args.port)  # blocking
            return
        if args.portal:
            import threading
            threading.Thread(target=serve, args=(ctl, args.port),
                             daemon=True).start()
            print(f"portal on :{args.port}", file=sys.stderr, flush=True)
        run_dynamic(args, ctl)
        return

    if args.list:
        path = find_dest_file(program)
        data = destfile.load_dest(path)
        name = destfile.program_name_for(path)
        print(f"{name}: services {', '.join(destfile.list_services(data))}")
        for s in destfile.list_services(data):
            for d in destfile.list_destinations(data, s):
                e = destfile.entry_of(data, s, d) or {}
                print(f"  {s} / {d} (code {e.get('service_code', '-')}, "
                      f"name {e.get('service_name', d)}):")
                for b in (e.get("bitmaps") or []):
                    print(f"    {b}")
        return

    prog = load_program(program, args.service, args.destination)

    if args.mock:
        d = prog["defaults"]
        print(f"program '{prog['program']}' "
              f"(colour {colour_str(d['colour'])}, "
              f"rotation {d['rotation_speed']:g}s, "
              f"{d['px_width']}x{d['px_height']})")
        last = None
        for s in prog["screens"]:
            if (s["service"], s["destination"]) != last:
                last = (s["service"], s["destination"])
                print(f"  {s['service']} / {s['destination']} "
                      f"(code {s.get('service_code') or '-'}, "
                      f"{colour_str(s.get('colour'))}, "
                      f"{s['seconds']:g}s):")
            print(f"    {s['image']}")
        return

    if args.preview:
        W = args.led_cols * args.led_chain
        H = args.led_rows
        for i, s in enumerate(prog["screens"], 1):
            full = _resolve_root(s["image"])
            try:
                sw, sh, frame = load_frame(full, W, H, args.image_fit)
            except SystemExit as e:
                print(f"screen {i}: {e}")
                continue
            if s.get("colour") not in ("full", None):
                frame = tint_frame(frame, s["colour"])
            lit = sum(1 for j in range(0, len(frame), 3)
                      if frame[j] or frame[j + 1] or frame[j + 2])
            print(f"--- screen {i}/{len(prog['screens'])} ({W}x{H}) "
                  f"{s['image']} [{s['seconds']:g}s] "
                  f"{s['service']} / {s['destination']} "
                  f"{colour_str(s.get('colour'))} ---")
            print(f"  {sw}x{sh} -> {args.image_fit}, "
                  f"{lit} lit pixels of {W * H}")
            # crude ASCII map, 4x downsample
            for y in range(0, H, 4):
                row = ""
                for x in range(0, W, 2):
                    o = (y * W + x) * 3
                    row += "#" if (frame[o] or frame[o + 1]
                                   or frame[o + 2]) else "."
                print(f"  {row}")
        return

    run_program(args, prog)


if __name__ == "__main__":
    main()
