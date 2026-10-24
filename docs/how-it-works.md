# How pcbwatch works

Everything is in `pcbwatch.py`: a Python standard-library HTTP server, a
polling thread, and one inline HTML page.

```
 agents save files ──▶ .kicad_sch / .kicad_pcb
                              │  (polled every 1 s: mtime + size)
                              ▼
                     Watcher thread ── kicad-cli ──▶ SVGs in a temp dir
                              │                        │
                              │   content-hashed, kept in memory, versioned
                              ▼
             ThreadingHTTPServer on 127.0.0.1:8765
                              │  /api/state every 1 s, /svg on new versions
                              ▼
                       browser page (pan/zoom, layers)
```

## Watching

A watch is a *(directory, stem)* pair: `stem.kicad_sch` and
`stem.kicad_pcb` in that directory, whichever exist at each poll. So a board
that an agent creates later just appears.

- **Schematic signature:** name, mtime and size of every `*.kicad_sch` under
  the directory, so a change to any sub-sheet counts.
- **Board signature:** mtime and size of the `.kicad_pcb`.
- **Debounce:** a file is exported only once its signature differs from the
  last export *and* is the same as on the previous poll. A save still in
  progress is not rendered.

## Rendering

**Schematic**
- Command: `kicad-cli sch export svg -o <tmp> <root>.kicad_sch`, which
  writes one SVG per sheet.
- Sheet order comes from kicad-cli's `Plotted to '…'` lines, which follow
  the hierarchy.

**Board**
- One call exports every layer:

  ```
  kicad-cli pcb export svg --mode-multi --page-size-mode 2 --exclude-drawing-sheet -l <layers> -o <tmp> board.kicad_pcb
  ```

- The layer list is read from the board's own `(layers …)` block.

**Change detection**
- Each sheet or layer is hashed with the SVG `<title>` line removed (it
  holds a timestamp).
- Its version goes up only when the picture actually changed, so you can
  see which layer moved.

### kicad-cli facts this relies on

Measured against kicad-cli 9.0.2. Re-check them when bumping KiCad.

| Fact | Consequence |
|---|---|
| Every layer from one `--mode-multi` export shares one `viewBox`. | Layers stack exactly as plain `<img>`s. |
| `--page-size-mode 2` frames the bounding box of *everything* on the board, not just Edge.Cuts. | An item placed off-board reframes every layer, so all of them show as changed. |
| `-l` accepts the file's legacy names (`F.SilkS`, `Dwgs.User`), and output files use canonical names (`F_Silkscreen`, `User_Drawings`). | The UI shows the canonical names, read from the output filenames. |
| A corrupt `.kicad_sch` or `.kicad_pcb` exits **3** and writes nothing. | Success means a zero exit *and* at least one SVG produced. On failure the last good render is kept. |
| An unused layer is a ~700-byte SVG with no drawing elements. | Shown greyed as "empty". |
| Layer SVGs have a transparent background and use the theme colour. | The page supplies the dark board background, and the swatch colour is the layer's most-used fill/stroke. |
| About 0.5 s per schematic and 0.8 s for 24 layers on an M-series Mac. | Polling once a second is fine. |

## HTTP API

All JSON. `POST`s are refused (403) if they carry a foreign `Origin`.

| Method | Path | |
|---|---|---|
| GET | `/` | the page |
| GET | `/api/state` | watched projects, their views, layer lists, versions, errors |
| GET | `/api/projects` | discovered projects under `--root` |
| GET | `/svg?w=<id>&v=<key>[&layer=<name>]` | one sheet, or one board layer (`v=board`) |
| POST | `/api/watch` `{"path": …}` | start watching a folder or KiCad file |
| POST | `/api/unwatch` `{"id": …}` | stop watching |
| POST | `/api/rescan` | search `--root` again |

`/api/state` is also a convenient hook for other tools, such as a script
that pings you when a board has not changed for ten minutes.

## Page

- Polls `/api/state` every second.
- A sheet is one `<img>`; a board is one `<img>` per visible layer, inside a
  single transformed container, so pan and zoom move them together.
- New versions load off-screen and then swap in, so there's no flicker and
  zoom is kept.
- Hidden layers stay loaded, so toggling them is instant.
- Per-browser preferences (layer choices, opacity, follow, last view) live
  in `localStorage`.
