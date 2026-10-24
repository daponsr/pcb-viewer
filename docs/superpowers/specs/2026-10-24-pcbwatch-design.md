# pcbwatch: live view of KiCad projects agents are editing

## Purpose

Several agents edit KiCad boards at once. `pcbwatch` is a local, read-only
viewer to keep open beside them: it re-renders a project's schematic sheets and
board every time a file changes, so their progress is visible without opening
KiCad.

Local tool only: runs on your own machine, never deployed or exposed to a network.

## Shape

One file, `pcbwatch.py`, Python standard library only (3.9+). Uses the
KiCad install's `kicad-cli` (9+, checked at startup) for rendering.
Packaged with a `pyproject.toml` (no dependencies) so it runs through uv.

```bash
uv run pcbwatch                     # http://127.0.0.1:8765, opens the browser
uvx --from git+https://github.com/daponsr/pcb-viewer pcbwatch --root ~/work --port 9000 --no-open
```

Binds `127.0.0.1` only.

## Choosing what to watch (in the browser)

- On start, and on the ↻ button, the server walks `--root` (default
  `~/Documents/GitHub`) for `*.kicad_pro`, skipping hidden dirs,
  `node_modules` and `*-backups`. The picker lists them newest first with a
  filter box; click to watch.
- A path box accepts any folder, `.kicad_pro`, `.kicad_sch` or `.kicad_pcb`.
- A watch is a (directory, stem) pair: `stem.kicad_sch` and `stem.kicad_pcb`
  in that directory, whichever exist *at each poll* (an agent may create the
  board later). A folder resolves to its `.kicad_pro`, else its first
  `.kicad_pcb` / `.kicad_sch`.
- The watched list persists in `~/.pcbwatch.json`.

## Watching and rendering

- Every second: schematic signature = (mtime, size) of every `*.kicad_sch`
  under the directory (sub-sheets); board signature = (mtime, size) of the
  `.kicad_pcb`.
- Debounce: export only once a signature differs from the last export *and*
  has been the same for two consecutive polls, so a half-written save is not
  rendered.
- Schematic: `kicad-cli sch export svg` into a fresh temp dir. Sheet order is
  taken from its `Plotted to '…'` lines (hierarchy order).
- Board: every layer the board defines (read from the `(layers …)` block of
  the `.kicad_pcb`) in one `kicad-cli pcb export svg --mode-multi
  --page-size-mode 2 --exclude-drawing-sheet` call, one SVG per layer.
  Verified on kicad-cli 9.0.2: 24 layers in ~0.8 s; all share one `viewBox`
  so they stack exactly; `-l` accepts the file's legacy names (`F.SilkS`)
  and the files carry canonical ones (`F_Silkscreen`), which are what the UI
  shows; layers are drawn in KiCad's theme colour, on a transparent
  background. A layer with no drawing elements is marked empty.
  The frame (`--page-size-mode 2`) is the bounding box of *everything* on
  the board, not just Edge.Cuts: an item placed off-board reframes, and so
  bumps, every layer. They stay aligned because one export frames them all.
- SVGs are held in memory. Each view has a version and a `changed_at` time;
  a sheet's `changed_at` moves only if its content changed (the `<title>`
  line, which embeds a timestamp, is ignored).

### Failure detection (verified against kicad-cli 9.0.2)

- A corrupt board or schematic exits **3** and writes nothing.
- Success is a zero exit *and* at least one SVG on disk, so neither a
  silent non-write nor a stray file passes alone.
- *Correction:* a first probe recorded "corrupt schematic exits 0". That was
  the exit code of a `| grep` in the probe, not of kicad-cli; a mutation test
  (accepting exit 0 with no files) passing when it should not have exposed it.
- On failure the last good render is kept and the error is shown on the
  item. The next file change retries.

## Page

- Sidebar: watched projects, each with its sheets and board, "12s ago"
  ages, a highlight on anything changed in the last 30s, a red note on
  export errors, ✕ to unwatch.
- Main: the selected view as an `<img>`; wheel to zoom, drag to pan,
  double-click to fit. New versions swap in without resetting zoom.
- Board layers: a panel over the board lists every layer with its colour,
  a checkbox, and a dot if it changed in the last 30 s. Ticked layers are
  stacked back-to-front (B.* → inner copper → F.* → user layers →
  Edge.Cuts), all at one opacity set by a slider so lower layers show
  through. Presets: Copper, Front, Back, All, None. Empty layers are greyed.
  Default: all copper, both silkscreens, Edge.Cuts. Choices persist in
  `localStorage` across boards; layer images stay loaded so toggling is
  instant.
- "Follow" toggle: jump to whichever view changed most recently.
- A view's *first* render is not a change: highlights, layer dots and
  follow only react to version 2 onwards, so opening the page does not
  light everything up.
- The selected view is kept in the URL hash (`#w=<id>&k=<view>`), so a view
  can be bookmarked or reopened directly.
- Polls `/api/state` every second.

## Out of scope

Diffs, history, 3D, editing, DRC, filesystem events, multiple users.

## Testing

`test_pcbwatch.py` runs the server on a copy of a KiCad template project and
asserts against the running system: views appear, an edit bumps the version,
a corrupted file reports an error while the last render is still served, and
repairing it clears the error.
