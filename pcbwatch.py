#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""pcbwatch: a live, read-only view of KiCad projects while agents edit them.

    uv run pcbwatch [--root DIR] [--port N] [--no-open]

Open the page, pick projects to watch, and every save re-renders the
schematic sheets and the board through kicad-cli. Standard library only.
"""

import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__version__ = "0.1.0"
MIN_KICAD = 9  # --mode-multi layer export and the output naming relied on here
CONFIG = Path(os.environ.get("PCBWATCH_CONFIG") or Path.home() / ".pcbwatch.json")
# Used only if the board's own (layers ...) block cannot be read.
FALLBACK_LAYERS = ["F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
DRAWN = re.compile(rb"<(path|circle|rect|polyline|polygon|line|ellipse|text)\b")
SKIP_DIRS = {"node_modules", "__pycache__"}
POLL_SECONDS = 1.0


def find_kicad_cli():
    """KICAD_CLI, then PATH, then the default install location on macOS, Windows and Linux."""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates = [
        os.environ.get("KICAD_CLI"),
        shutil.which("kicad-cli"),
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        # Windows installs per version (C:\Program Files\KiCad\9.0\bin); newest first.
        *sorted(glob.glob(os.path.join(program_files, "KiCad", "*", "bin", "kicad-cli.exe")), reverse=True),
        "/usr/bin/kicad-cli",
        "/usr/local/bin/kicad-cli",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise SystemExit("kicad-cli not found. Install KiCad 9+, or set KICAD_CLI=/path/to/kicad-cli")


def check_kicad_version(cli):
    """Refuse KiCad < 9 up front, rather than failing obscurely on the first export."""
    try:
        out = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemExit(f"could not run {cli}: {e}")
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        raise SystemExit(f"could not read the KiCad version from {cli!r}: {out.strip()!r}")
    if int(m.group(1)) < MIN_KICAD:
        raise SystemExit(f"pcbwatch needs KiCad {MIN_KICAD} or newer; {cli} is {m.group(0)}")
    return m.group(0)


def default_root():
    """~/Documents/GitHub if it exists, else the current directory."""
    p = Path.home() / "Documents" / "GitHub"
    return str(p) if p.is_dir() else os.getcwd()


def skip_dir(name):
    return name.startswith(".") or name in SKIP_DIRS or name.endswith("-backups")


def discover(root):
    """Every .kicad_pro under root, newest first."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for f in filenames:
            if f.endswith(".kicad_pro"):
                p = os.path.join(dirpath, f)
                try:
                    mtime = max(
                        os.path.getmtime(os.path.join(dirpath, x))
                        for x in filenames
                        if x.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro"))
                    )
                except OSError:
                    continue
                found.append({"path": p, "name": f[:-10], "rel": os.path.relpath(dirpath, root), "mtime": mtime})
    found.sort(key=lambda x: -x["mtime"])
    return found


def resolve(path):
    """A user-given path -> (directory, stem) of the project to watch."""
    p = Path(os.path.expanduser(path.strip())).resolve()
    if p.is_file() and p.suffix in (".kicad_pro", ".kicad_sch", ".kicad_pcb"):
        return str(p.parent), p.stem
    if p.is_dir():
        for suffix in (".kicad_pro", ".kicad_pcb", ".kicad_sch"):
            hits = sorted(p.glob("*" + suffix))
            if hits:
                return str(p), hits[0].stem
        raise ValueError(f"no KiCad project in {p}")
    raise ValueError(f"not a KiCad file or folder: {p}")


def content_hash(svg):
    # The <title> line embeds the export time; ignore it so unchanged sheets stay unchanged.
    return hashlib.sha1(re.sub(rb"<title>.*?</title>", b"", svg, count=1)).hexdigest()


class View:
    """One rendered picture: a schematic sheet or the board."""

    def __init__(self, key, label):
        self.key, self.label = key, label
        self.svg, self.hash = b"", None
        self.version, self.changed_at = 0, 0.0

    def update(self, svg):
        h = content_hash(svg)
        if h != self.hash:
            self.svg, self.hash = svg, h
            self.version += 1
            self.changed_at = time.time()


def board_layers(pcb_file):
    """Layer names the board defines, from the top-level (layers ...) block."""
    try:
        text = Path(pcb_file).read_text(errors="replace")
        start = text.index("(layers")
        end = text.find("(setup", start)
        block = text[start:end if end > 0 else start + 20000]
        names = re.findall(r'\(\s*-?\d+\s+"([^"]+)"\s+(?:signal|power|mixed|jumper|user)', block)
        return names or FALLBACK_LAYERS
    except (OSError, ValueError):
        return FALLBACK_LAYERS


SIDE_ORDER = ["Cu", "Mask", "Paste", "Adhesive", "Silkscreen", "Courtyard", "Fab"]  # nearest the board first


def layer_z(name):
    """Back-to-front stacking order, as KiCad draws it viewed from the front."""
    side, _, kind = name.partition(".")
    rank = SIDE_ORDER.index(kind) if kind in SIDE_ORDER else len(SIDE_ORDER)
    if side == "B":
        return 100 - rank  # B.Fab furthest back, B.Cu nearest the core
    m = re.fullmatch(r"In(\d+)\.Cu", name)
    if m:
        return 300 - int(m.group(1))  # In1, nearest the front, drawn last
    if side == "F":
        return 400 + rank
    if name in ("Margin", "Edge.Cuts"):
        return 700 + (name == "Edge.Cuts")
    return 600  # User.*


def layer_color(svg):
    """The layer's theme colour: its most used fill/stroke, ignoring black and white."""
    counts = collections.Counter(
        c.upper() for c in re.findall(rb"(?:fill|stroke):#([0-9A-Fa-f]{6})", svg)
        if c.upper() not in (b"000000", b"FFFFFF")
    )
    return "#" + counts.most_common(1)[0][0].decode() if counts else "#888888"


class Board:
    """The board as a stack of per-layer views."""

    key = label = "board"

    def __init__(self):
        self.layers = {}  # name -> View, in export order
        self.version, self.changed_at = 0, 0.0

    def update(self, layers):
        fresh, changed = {}, False
        for name, svg in layers:
            v = self.layers.get(name) or View(name, name)
            before = v.version
            v.update(svg)
            v.color, v.empty, v.z = layer_color(svg), not DRAWN.search(svg), layer_z(name)
            fresh[name] = v
            if v.version != before:
                changed = True
                self.changed_at = max(self.changed_at, v.changed_at)
        if changed or fresh.keys() != self.layers.keys():
            self.version += 1
        self.layers = fresh


class Watch:
    def __init__(self, directory, stem):
        self.dir, self.stem = directory, stem
        self.id = hashlib.sha1(f"{directory}/{stem}".encode()).hexdigest()[:10]
        self.sch_views, self.pcb_view = [], None
        self.errors = {}  # "sch" / "pcb" -> message
        self.exported = {"sch": None, "pcb": None}  # signature last exported
        self.seen = {"sch": None, "pcb": None}  # signature at previous poll
        self.busy = False

    @property
    def sch_file(self):
        return os.path.join(self.dir, self.stem + ".kicad_sch")

    @property
    def pcb_file(self):
        return os.path.join(self.dir, self.stem + ".kicad_pcb")

    def signature(self, kind):
        try:
            if kind == "pcb":
                if not os.path.exists(self.pcb_file):
                    return None
                st = os.stat(self.pcb_file)
                return (st.st_mtime_ns, st.st_size)
            if not os.path.exists(self.sch_file):
                return None
            sig = []
            for dirpath, dirnames, filenames in os.walk(self.dir):
                dirnames[:] = [d for d in dirnames if not skip_dir(d)]
                for f in filenames:
                    if f.endswith(".kicad_sch"):
                        st = os.stat(os.path.join(dirpath, f))
                        sig.append((f, st.st_mtime_ns, st.st_size))
            return tuple(sorted(sig))
        except OSError:
            return None  # mid-rename; try next poll


class Watcher:
    def __init__(self, cli, root):
        self.cli, self.root = cli, root
        self.lock = threading.Lock()
        self.watches = {}
        self.projects = []
        self.scanned_at = 0.0
        for d, s in self.load():
            self.add(d, s, save=False)

    # ---- persistence -------------------------------------------------
    def load(self):
        try:
            return [tuple(x) for x in json.loads(CONFIG.read_text()).get("watched", [])]
        except (OSError, ValueError):
            return []

    def save(self):
        data = {"watched": [[w.dir, w.stem] for w in self.watches.values()]}
        try:
            CONFIG.write_text(json.dumps(data, indent=2))
        except OSError as e:
            print(f"could not save {CONFIG}: {e}")

    # ---- watch list --------------------------------------------------
    def add(self, directory, stem, save=True):
        w = Watch(directory, stem)
        with self.lock:
            self.watches.setdefault(w.id, w)
        if save:
            self.save()
        return w.id

    def remove(self, wid):
        with self.lock:
            self.watches.pop(wid, None)
        self.save()

    def rescan(self):
        self.projects = discover(self.root)
        self.scanned_at = time.time()

    # ---- rendering ---------------------------------------------------
    def run(self, args):
        r = subprocess.run([self.cli, *args], capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def export_sch(self, w):
        with tempfile.TemporaryDirectory(prefix="pcbwatch-") as out:
            code, log = self.run(["sch", "export", "svg", "-o", out, w.sch_file])
            # Require both: a clean exit and at least one sheet actually written.
            plotted = re.findall(r"Plotted to '(.+?)'", log)
            files = [p for p in plotted if os.path.isfile(p)]
            if code != 0 or not files:
                return None, last_line(log) or f"exit {code}, no sheets"
            sheets = []
            for p in files:
                name = Path(p).stem
                label = name[len(w.stem) + 1:] if name.startswith(w.stem + "-") else "root"
                sheets.append((name, label, Path(p).read_bytes()))
            return sheets, None

    def export_pcb(self, w):
        # One file per layer. -l takes the file's own (possibly legacy) names; the output
        # files carry canonical ones (F.SilkS -> <stem>-F_Silkscreen.svg), which we display.
        with tempfile.TemporaryDirectory(prefix="pcbwatch-") as out:
            code, log = self.run([
                "pcb", "export", "svg", "--mode-multi", "--page-size-mode", "2", "--exclude-drawing-sheet",
                "-l", ",".join(board_layers(w.pcb_file)), "-o", out, w.pcb_file,
            ])
            files = sorted(Path(out).glob("*.svg"))
            if code != 0 or not files:
                return None, last_line(log) or f"exit {code}, no layers"
            prefix = w.stem + "-"
            layers = []
            for f in files:
                name = f.stem[len(prefix):] if f.stem.startswith(prefix) else f.stem
                layers.append((name.replace("_", "."), f.read_bytes()))
            return layers, None

    def step(self, w):
        for kind in ("sch", "pcb"):
            sig = w.signature(kind)
            stable = sig == w.seen[kind]
            w.seen[kind] = sig
            if sig is None:
                if kind == "pcb":
                    w.pcb_view = None
                else:
                    w.sch_views = []
                w.errors.pop(kind, None)
                w.exported[kind] = None
                continue
            if sig == w.exported[kind] or not stable:
                continue
            w.exported[kind] = sig
            if kind == "sch":
                sheets, err = self.export_sch(w)
                if sheets:
                    old = {v.key: v for v in w.sch_views}
                    views = []
                    for key, label, svg in sheets:
                        v = old.get(key) or View(key, label)
                        v.update(svg)
                        views.append(v)
                    w.sch_views = views
            else:
                layers, err = self.export_pcb(w)
                if layers:
                    w.pcb_view = w.pcb_view or Board()
                    w.pcb_view.update(layers)
            if err:
                w.errors[kind] = err
            else:
                w.errors.pop(kind, None)

    def loop(self):
        while True:
            with self.lock:
                watches = list(self.watches.values())
            for w in watches:
                try:
                    w.busy = True
                    self.step(w)
                except Exception as e:  # keep the loop alive whatever one project does
                    w.errors["sch"] = f"{type(e).__name__}: {e}"
                finally:
                    w.busy = False
            time.sleep(POLL_SECONDS)

    # ---- API shapes --------------------------------------------------
    def state(self):
        with self.lock:
            watches = list(self.watches.values())
        return {
            "now": time.time(),
            "watches": [
                {
                    "id": w.id,
                    "name": w.stem,
                    "dir": w.dir,
                    "busy": w.busy,
                    "errors": w.errors,
                    "views": [
                        {"key": v.key, "label": v.label, "kind": "sch",
                         "version": v.version, "changed_at": v.changed_at}
                        for v in w.sch_views
                    ] + ([{
                        "key": "board", "label": "board", "kind": "pcb",
                        "version": w.pcb_view.version, "changed_at": w.pcb_view.changed_at,
                        "layers": [
                            {"name": l.key, "version": l.version, "changed_at": l.changed_at,
                             "color": l.color, "empty": l.empty, "z": l.z}
                            for l in w.pcb_view.layers.values()
                        ],
                    }] if w.pcb_view else []),
                }
                for w in watches
            ],
        }

    def svg(self, wid, key, layer=None):
        w = self.watches.get(wid)
        if not w:
            return None
        if key == "board":
            v = w.pcb_view.layers.get(layer) if w.pcb_view else None
            return v.svg if v else None
        for v in w.sch_views:
            if v.key == key:
                return v.svg
        return None


def last_line(log):
    lines = [l for l in log.splitlines() if l.strip() and not l.startswith("Fontconfig")]
    return lines[-1].strip() if lines else ""


def make_handler(watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self.send(200, watcher.state())
            if u.path == "/api/projects":
                watched = {(w.dir, w.stem) for w in watcher.watches.values()}
                projects = [dict(p, watched=(str(Path(p["path"]).parent), p["name"]) in watched)
                            for p in watcher.projects]
                return self.send(200, {"root": watcher.root, "scanned_at": watcher.scanned_at, "projects": projects})
            if u.path == "/svg":
                svg = watcher.svg(q.get("w", [""])[0], q.get("v", [""])[0], q.get("layer", [None])[0])
                if svg is None:
                    return self.send(404, {"error": "no such view"})
                return self.send(200, svg, "image/svg+xml")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            # Only same-origin pages may change the watch list.
            if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
                return self.send(403, {"error": "cross-origin"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.send(400, {"error": "bad json"})
            if self.path == "/api/watch":
                try:
                    d, s = resolve(body.get("path", ""))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                return self.send(200, {"id": watcher.add(d, s)})
            if self.path == "/api/unwatch":
                watcher.remove(body.get("id", ""))
                return self.send(200, {"ok": True})
            if self.path == "/api/rescan":
                watcher.rescan()
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>pcbwatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#f4f4f2;--panel:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e2de;--accent:#1f6feb;--hot:#f2a900;--bad:#d1242f;--stage:#e9e9e6}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--panel:#1c1c1c;--ink:#e8e8e4;--mute:#8d8d87;--line:#2c2c2c;--accent:#58a6ff;--hot:#e3b341;--bad:#f85149;--stage:#0d0d0d}}
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;background:var(--bg);color:var(--ink);font:13px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;overflow:hidden}
aside{width:270px;min-width:200px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column}
header{padding:10px 12px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
header b{flex:1;font-size:14px}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:5px;padding:3px 8px;cursor:pointer}
button:hover{border-color:var(--accent)}
label.follow{display:flex;gap:4px;align-items:center;color:var(--mute);cursor:pointer}
#list{flex:1;overflow:auto;padding:6px 0}
.proj{padding:6px 12px 8px}
.proj h3{margin:0 0 3px;font-size:13px;display:flex;gap:6px;align-items:center}
.proj h3 span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.proj h3 .x{border:0;background:none;color:var(--mute);padding:0 2px}
.proj .dir{color:var(--mute);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left;margin-bottom:4px}
.err{color:var(--bad);font-size:11px;margin:2px 0 4px;word-break:break-word}
.view{display:flex;gap:6px;align-items:center;padding:3px 6px;border-radius:5px;cursor:pointer;border-left:3px solid transparent}
.view:hover{background:var(--bg)}
.view.sel{background:var(--bg);outline:1px solid var(--accent)}
.view.hot{border-left-color:var(--hot)}
.view .k{font-size:10px;color:var(--mute);width:24px}
.view .l{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.view .age{color:var(--mute);font-size:11px;font-variant-numeric:tabular-nums}
.empty{color:var(--mute);padding:14px 12px}
main{flex:1;position:relative;background:var(--stage);overflow:hidden;cursor:grab}
main.drag{cursor:grabbing}
#canvas{position:absolute;left:0;top:0;transform-origin:0 0}
#canvas img{position:absolute;left:0;top:0;user-select:none;-webkit-user-drag:none;max-width:none}
#canvas.pcb{background:#001023}
#canvas.sch{background:#fff}
#layers{position:absolute;right:10px;top:10px;width:220px;max-height:calc(100% - 40px);display:none;flex-direction:column;background:var(--panel);border:1px solid var(--line);border-radius:8px;cursor:default;font-size:12px;z-index:2}
#layers.show{display:flex}
#layers .hd{display:flex;align-items:center;padding:6px 8px;gap:6px;cursor:pointer;font-weight:600}
#layers .hd span{flex:1}
#layers .body{padding:0 8px 8px;overflow:auto}
#layers.collapsed .body{display:none}
.lp{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px}
.lp button{padding:1px 6px;font-size:11px}
.op{display:flex;align-items:center;gap:6px;color:var(--mute);margin-bottom:6px}
.op input{flex:1}
.ly{display:flex;align-items:center;gap:6px;padding:2px;border-radius:4px;cursor:pointer}
.ly:hover{background:var(--bg)}
.ly .sw{width:11px;height:11px;border-radius:2px;flex:none}
.ly.empty{opacity:.4}
.ly .dot{margin-left:auto;width:7px;height:7px;border-radius:50%;background:var(--hot)}
#hud{position:absolute;left:10px;bottom:8px;color:var(--mute);font-size:11px;pointer-events:none}
#pick{position:absolute;inset:0;background:rgba(0,0,0,.35);display:none;align-items:flex-start;justify-content:center;padding-top:8vh;z-index:5}
#pick.open{display:flex}
.box{background:var(--panel);border:1px solid var(--line);border-radius:8px;width:min(640px,92vw);max-height:80vh;display:flex;flex-direction:column}
.box .row{display:flex;gap:6px;padding:10px;border-bottom:1px solid var(--line)}
.box input{flex:1;font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:5px;background:var(--bg);color:var(--ink)}
#plist{overflow:auto}
.p{padding:6px 12px;cursor:pointer;display:flex;gap:8px}
.p:hover{background:var(--bg)}
.p .n{font-weight:600}
.p .r{color:var(--mute);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.p.on .n::after{content:" ✓";color:var(--accent)}
#perr{color:var(--bad);padding:0 10px 8px;font-size:12px}
</style></head><body>
<aside>
  <header><b>pcbwatch</b><label class="follow" title="Jump to whatever changed last"><input type="checkbox" id="follow">follow</label><button id="add">+ watch</button></header>
  <div id="list"></div>
</aside>
<main id="stage"><div id="canvas"></div>
  <div id="layers"><div class="hd" id="lyhd"><span>Layers</span><small id="lycount"></small><small id="lycaret">▾</small></div>
    <div class="body">
      <div class="lp"><button data-preset="copper">Copper</button><button data-preset="front">Front</button><button data-preset="back">Back</button><button data-preset="all">All</button><button data-preset="none">None</button></div>
      <label class="op">opacity <input id="op" type="range" min="0.2" max="1" step="0.05"></label>
      <div id="lylist"></div>
    </div></div>
  <div id="hud"></div></main>
<div id="pick"><div class="box">
  <div class="row"><input id="filter" placeholder="Filter projects…"><button id="rescan" title="Search again">↻</button><button id="close">close</button></div>
  <div class="row"><input id="path" placeholder="…or paste a folder / .kicad_pro / .kicad_sch / .kicad_pcb path"><button id="addpath">watch</button></div>
  <div id="perr"></div>
  <div id="plist"></div>
</div></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = {watches: [], now: 0}, sel = null, projects = [];
let canvasFor = null, imgs = {}, nat = null, lySig = '';
let layerOn = {}, opacity = 0.85;
try { layerOn = JSON.parse(localStorage.getItem('pcbwatch.layers')) || {}; opacity = +(localStorage.getItem('pcbwatch.opacity') || 0.85); } catch (e) {}
const DEFAULT_ON = n => /\.Cu$/.test(n) || /Silkscreen$/.test(n) || n === 'Edge.Cuts';
const isOn = n => n in layerOn ? layerOn[n] : DEFAULT_ON(n);
const PRESETS = {copper: n => /\.Cu$/.test(n) || n === 'Edge.Cuts', front: n => n.startsWith('F.') || n === 'Edge.Cuts',
  back: n => n.startsWith('B.') || n === 'Edge.Cuts', all: () => true, none: () => false};
function saveLayers() { try { localStorage.setItem('pcbwatch.layers', JSON.stringify(layerOn)); localStorage.setItem('pcbwatch.opacity', opacity); } catch (e) {} }
let z = {s: 1, x: 0, y: 0}, fitPending = true;
try { sel = JSON.parse(localStorage.getItem('pcbwatch.sel')); $('#follow').checked = localStorage.getItem('pcbwatch.follow') === '1'; } catch (e) {}
{ const h = new URLSearchParams(location.hash.slice(1)); if (h.get('w')) sel = {w: h.get('w'), k: h.get('k')}; }  // #w=<id>&k=<view> opens a view

const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}).then(r => r.json());
// The first render is not a change: only later versions mark something as recently changed.
const hot = x => x.version > 1 && state.now - x.changed_at < 30;
const ago = t => { if (!t) return '–'; const s = Math.max(0, Math.round(state.now - t)); return s < 60 ? s + 's' : s < 3600 ? Math.round(s / 60) + 'm' : Math.round(s / 3600) + 'h'; };

function select(w, k) {
  sel = {w, k}; fitPending = true;
  try { localStorage.setItem('pcbwatch.sel', JSON.stringify(sel)); } catch (e) {}
  history.replaceState(null, '', '#' + new URLSearchParams({w, k}));
  render();
}

function render() {
  const list = $('#list');
  if (!state.watches.length) { list.innerHTML = '<div class="empty">Nothing watched yet. Click <b>+ watch</b>.</div>'; }
  else list.innerHTML = state.watches.map(w => `
    <div class="proj">
      <h3><span title="${esc(w.dir)}">${esc(w.name)}</span>${w.busy ? '<small>⟳</small>' : ''}<button class="x" data-un="${w.id}" title="Stop watching">✕</button></h3>
      <div class="dir" title="${esc(w.dir)}"><bdi>${esc(w.dir)}</bdi></div>
      ${Object.entries(w.errors).map(([k, e]) => `<div class="err">${k}: ${esc(e)}</div>`).join('')}
      ${w.views.length ? '' : '<div class="empty" style="padding:2px 0">no schematic or board yet</div>'}
      ${w.views.map(v => `<div class="view ${sel && sel.w === w.id && sel.k === v.key ? 'sel' : ''} ${hot(v) ? 'hot' : ''}" data-w="${w.id}" data-k="${esc(v.key)}">
        <span class="k">${v.kind}</span><span class="l">${esc(v.label)}</span><span class="age">${ago(v.changed_at)}</span></div>`).join('')}
    </div>`).join('');
  showImage();
}

function current() {
  for (const w of state.watches) for (const v of w.views) if (sel && w.id === sel.w && v.key === sel.k) return [w, v];
  return [null, null];
}

function showImage() {
  const [w, v] = current(), canvas = $('#canvas');
  if (!v) { canvas.innerHTML = ''; imgs = {}; canvasFor = null; $('#hud').textContent = ''; $('#layers').classList.remove('show'); return; }
  const id = w.id + '/' + v.key;
  if (id !== canvasFor) { canvas.innerHTML = ''; imgs = {}; nat = null; canvasFor = id; canvas.className = v.kind; lySig = ''; }
  $('#hud').textContent = `${w.name} · ${v.label} · v${v.version} · changed ${ago(v.changed_at)} ago`;

  // A schematic sheet is one image; a board is one image per visible layer, stacked by z.
  const parts = v.kind === 'pcb'
    ? v.layers.filter(l => isOn(l.name) && !l.empty).map(l => ({name: l.name, ver: l.version, z: l.z}))
    : [{name: '', ver: v.version, z: 0}];
  drawLayers(v);
  const wanted = new Set(parts.map(p => p.name));
  for (const [n, o] of Object.entries(imgs)) o.el.style.display = wanted.has(n) ? '' : 'none';
  for (const p of parts) {
    let o = imgs[p.name];
    if (!o) { const el = document.createElement('img'); el.alt = ''; canvas.appendChild(el); o = imgs[p.name] = {el, want: -1}; }
    o.el.style.zIndex = p.z;
    o.el.style.opacity = v.kind === 'pcb' ? opacity : 1;
    if (o.want === p.ver) continue;
    o.want = p.ver;
    const next = new Image();  // load off-screen, then swap: no flicker, zoom kept
    next.onload = () => {
      if (o.want !== p.ver || canvasFor !== id) return;
      if (!nat) { nat = [next.naturalWidth, next.naturalHeight]; canvas.style.width = nat[0] + 'px'; canvas.style.height = nat[1] + 'px'; }
      o.el.style.width = nat[0] + 'px'; o.el.src = next.src;
      if (fitPending) { fit(); fitPending = false; } else apply();
    };
    next.src = `/svg?w=${encodeURIComponent(w.id)}&v=${encodeURIComponent(v.key)}` +
      (p.name ? `&layer=${encodeURIComponent(p.name)}` : '') + `&n=${p.ver}`;
  }
}

function drawLayers(v) {
  const panel = $('#layers');
  panel.classList.toggle('show', v.kind === 'pcb');
  if (v.kind !== 'pcb') return;
  const rows = [...v.layers].sort((a, b) => b.z - a.z);  // front at the top, like KiCad
  const sig = JSON.stringify(rows.map(l => [l.name, isOn(l.name), l.empty, l.color, hot(l)]));
  if (sig === lySig) return;  // only redraw on change, so clicks are never lost
  lySig = sig;
  $('#lycount').textContent = `${rows.filter(l => isOn(l.name) && !l.empty).length}/${rows.filter(l => !l.empty).length}`;
  $('#lylist').innerHTML = rows.map(l => `<label class="ly ${l.empty ? 'empty' : ''}" title="${l.empty ? 'nothing on this layer' : esc(l.name)}">
    <input type="checkbox" data-layer="${esc(l.name)}" ${isOn(l.name) ? 'checked' : ''}>
    <span class="sw" style="background:${esc(l.color)}"></span>${esc(l.name)}
    ${hot(l) ? '<span class="dot" title="changed in the last 30s"></span>' : ''}</label>`).join('');
}

function apply() { $('#canvas').style.transform = `translate(${z.x}px,${z.y}px) scale(${z.s})`; }
function fit() {
  if (!nat) return;
  const st = $('#stage').getBoundingClientRect(), [w, h] = nat, ly = $('#layers');
  // Leave room for the layer panel when it is open, so it does not cover the board.
  const room = ly.classList.contains('show') && !ly.classList.contains('collapsed') ? ly.offsetWidth + 20 : 0;
  const width = st.width - room;
  z.s = Math.min(width / w, st.height / h) * 0.95;
  z.x = (width - w * z.s) / 2; z.y = (st.height - h * z.s) / 2; apply();
}

async function tick() {
  try {
    state = await fetch('/api/state').then(r => r.json());
    if ($('#follow').checked) {
      let best = null;
      for (const w of state.watches) for (const v of w.views) if (v.version > 1 && (!best || v.changed_at > best[2])) best = [w.id, v.key, v.changed_at];
      if (best && (!sel || sel.w !== best[0] || sel.k !== best[1])) { sel = {w: best[0], k: best[1]}; fitPending = true; }
    }
    if (!current()[1]) {  // nothing (valid) selected: first view of the selected project, else of any
      const w = state.watches.find(w => sel && w.id === sel.w && w.views.length) || state.watches.find(w => w.views.length);
      if (w) { sel = {w: w.id, k: w.views[0].key}; fitPending = true; }
    }
    render();
  } catch (e) { $('#hud').textContent = 'server not reachable'; }
}

$('#list').addEventListener('click', async e => {
  const un = e.target.closest('[data-un]');
  if (un) { await post('/api/unwatch', {id: un.dataset.un}); return tick(); }
  const v = e.target.closest('.view');
  if (v) select(v.dataset.w, v.dataset.k);
});

// ---- layer panel ----
$('#lylist').addEventListener('change', e => {
  const n = e.target.dataset.layer;
  if (n) { layerOn[n] = e.target.checked; saveLayers(); showImage(); }
});
$('.lp').addEventListener('click', e => {
  const f = PRESETS[e.target.dataset.preset], v = current()[1];
  if (!f || !v || !v.layers) return;
  for (const l of v.layers) layerOn[l.name] = f(l.name);
  saveLayers(); showImage();
});
$('#op').value = opacity;
$('#op').addEventListener('input', e => { opacity = +e.target.value; saveLayers(); showImage(); });
$('#lyhd').addEventListener('click', () => {
  const c = $('#layers').classList.toggle('collapsed'); $('#lycaret').textContent = c ? '▸' : '▾';
});
for (const ev of ['mousedown', 'wheel', 'dblclick']) $('#layers').addEventListener(ev, e => e.stopPropagation());

const stage = $('#stage');
stage.addEventListener('wheel', e => {
  e.preventDefault();
  const r = stage.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  const f = Math.exp(-e.deltaY * 0.0015), s = Math.min(80, Math.max(0.02, z.s * f));
  z.x = mx - (mx - z.x) * s / z.s; z.y = my - (my - z.y) * s / z.s; z.s = s; apply();
}, {passive: false});
let drag = null;
stage.addEventListener('mousedown', e => { drag = [e.clientX - z.x, e.clientY - z.y]; stage.classList.add('drag'); });
addEventListener('mousemove', e => { if (drag) { z.x = e.clientX - drag[0]; z.y = e.clientY - drag[1]; apply(); } });
addEventListener('mouseup', () => { drag = null; stage.classList.remove('drag'); });
stage.addEventListener('dblclick', fit);
addEventListener('resize', () => fitPending && fit());

$('#follow').addEventListener('change', e => { try { localStorage.setItem('pcbwatch.follow', e.target.checked ? '1' : '0'); } catch (_) {} });

// ---- picker ----
async function loadProjects() {
  const r = await fetch('/api/projects').then(r => r.json());
  projects = r.projects; $('#filter').placeholder = `Filter ${projects.length} projects under ${r.root}…`; drawProjects();
}
function drawProjects() {
  const f = $('#filter').value.toLowerCase();
  const hits = projects.filter(p => (p.rel + '/' + p.name).toLowerCase().includes(f)).slice(0, 300);
  $('#plist').innerHTML = hits.map(p => `<div class="p ${p.watched ? 'on' : ''}" data-path="${esc(p.path)}"><span class="n">${esc(p.name)}</span><span class="r">${esc(p.rel)}</span><span class="age">${new Date(p.mtime * 1000).toLocaleDateString()}</span></div>`).join('')
    || '<div class="empty">No projects match.</div>';
}
async function watchPath(path) {
  $('#perr').textContent = '';
  const r = await post('/api/watch', {path});
  if (r.error) { $('#perr').textContent = r.error; return; }
  sel = {w: r.id, k: null}; fitPending = true;
  $('#pick').classList.remove('open'); tick();
}
$('#add').onclick = () => { $('#pick').classList.add('open'); $('#perr').textContent = ''; loadProjects(); $('#filter').focus(); };
$('#close').onclick = () => $('#pick').classList.remove('open');
$('#pick').addEventListener('click', e => { if (e.target.id === 'pick') $('#pick').classList.remove('open'); });
addEventListener('keydown', e => { if (e.key === 'Escape') $('#pick').classList.remove('open'); });
$('#filter').oninput = drawProjects;
$('#rescan').onclick = async () => { $('#plist').innerHTML = '<div class="empty">Searching…</div>'; await post('/api/rescan'); loadProjects(); };
$('#plist').addEventListener('click', e => { const p = e.target.closest('.p'); if (p) watchPath(p.dataset.path); });
$('#addpath').onclick = () => watchPath($('#path').value);
$('#path').addEventListener('keydown', e => { if (e.key === 'Enter') watchPath($('#path').value); });

tick(); setInterval(tick, 1000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=default_root(),
                    help="where to look for projects (default: ~/Documents/GitHub if it exists, else the current dir)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--version", action="version", version=f"pcbwatch {__version__}")
    args = ap.parse_args()

    cli = find_kicad_cli()
    kicad = check_kicad_version(cli)
    watcher = Watcher(cli, os.path.expanduser(args.root))
    threading.Thread(target=watcher.rescan, daemon=True).start()
    threading.Thread(target=watcher.loop, daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(watcher))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"pcbwatch {__version__} on {url}  (KiCad {kicad}, root {watcher.root}, watching {len(watcher.watches)}, config {CONFIG})", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
