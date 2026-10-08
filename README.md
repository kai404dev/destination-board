# Destination board

Bitmap programme board for 240x40 LED panels, modelled on the
`bus/program.py` + ICU 602 portal in `depature-board` (same stdlib-only
style, same tint/fit/dim pipeline, same live `*_control.json` follow).

## Layout

- `board.py` — matrix runner (mock / preview / live + portal hosting)
- `portal.py` — web portal (create + upload programs, toggle the board)
- `destfile.py` — `.dest` format model (load / validate / resolve)
- `images.py` — stdlib PNG decode / scale / tint
- `render.py` — text-to-bitmap renderer (BDF fonts from `fonts/` only)
- `fonts/*.bdf` — bitmap fonts (the only font source; no system fonts)
- `programs/*.dest` — programs (JSON, see below)
- `bitmaps/<program>/<route>/<route>-<destination>-<page>.png` — pages
- `board_control.json` — live pick the matrix follows (`program`,
  `service`, `destination`; `null` = all)

## `.dest` format

```json
{
    "defaults": {
        "colour": "#DB9600",
        "rotation_speed": 3,
        "px_width": 240,
        "px_height": 40
    },
    "services": {
        "43": {
            "Sheffield": {
                "service_code": "001",
                "service_name": "Sheffield",
                "override": {
                    "colour": "full",
                    "rotation_speed": 3,
                    "px_width": 240,
                    "px_height": 40
                },
                "bitmaps": [
                    "bitmaps/example/43/43-sheffield-1.png",
                    "bitmaps/example/43/43-sheffield-2.png"
                ]
            }
        }
    }
}
```

- `services` is `{service number: {destination: entry}}`.
- `colour` tints the page to one shade; `"full"` keeps the bitmap's own
  colours. Cascade: `override.colour` → `defaults.colour`.
- `rotation_speed` is seconds per page (override → defaults → 3).
- Numbers or numeric strings accepted (`"3"` works like `3`).
- `override` is optional and may hold any subset of the four keys.
- Bitmap paths are project-root-relative, in play order.

## Run it

```bash
python3 board.py --list                                  # what's installed
python3 board.py programs/example.dest --list            # this program
python3 board.py programs/example.dest --service 43 --mock --once
python3 board.py programs/example.dest --service 43 \
    --destination Sheffield --preview
python3 portal.py                                       # http://localhost:4040
sudo python3 board.py programs/example.dest --service 43 --portal --led-no-drop-privs
```

`--serve` hosts the portal without the matrix. The control file steers
a live matrix within ~0.5s, same process or another one.

Combined matrix+portal runs need `--led-no-drop-privs`: the rgbmatrix
driver otherwise drops root→`daemon` after hardware init, and the
portal thread then cannot write programs, bitmaps or
`board_control.json` (especially under a locked-down home dir such as
`drwx------ /home/kai`). Alternative split topology: matrix as root
without `--portal`, plus the portal as your normal user
(`python portal.py --port 4040` or `board.py --serve`) — both sides
coordinate through `board_control.json`, so keep the repo owned by
that user (`sudo chown -R kai:kai /home/kai/destination-board`).

## Portal

Two pages (tabs at the top). Nothing you type is ever wiped by a
background refresh:

**Board (`/`)** — program / service / destination pick; `Show on
board` retunes the matrix immediately. Only the status line and the
screens strip update live; the form is never rewritten.

**Create (`/create`)** — everything else, each action updating just
its own region:

- **Program** — switch the edited program, create a blank `.dest`,
  upload a `.dest` file, delete, or download the current one.
- **Defaults** — colour / rotation / panel size for the program.
- **Services & destinations** — add services (route numbers) and
  destinations (auto `service_code` numbering), rename either (PNG files
  move with the new names), per-destination editor for slot name,
  service code/name and the full override (colour, rotation, panel
  size), per-destination `show` (puts it on the board), page filmstrip
  with per-page delete. Half-built services/destinations (no pages
  yet) are allowed.
- **Create page from text** — type route / destination / via, pick layout,
  colour and BDF fonts (from `fonts/` only, never system fonts);
  `Preview` renders the 240x40 blind, `Create + show` saves the PNG,
  appends it and puts it on the board.
- **Upload bitmap page** — PNG saved as
  `<route>-<destination>-<next-page>.png` under
  `bitmaps/<program>/<route>/` and appended to the destination.

## Pi auto-start

```ini
[Unit]
Description=Destination board
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/kai/destination-board
ExecStart=/home/kai/destination-board/.venv/bin/python board.py programs/example.dest --service 43 --portal --port 4040
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
