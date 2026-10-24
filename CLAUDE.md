# pcbwatch: notes for agents working on this repo

A local, read-only, live viewer for KiCad projects. It runs on the user's
own machine and is never deployed or exposed to a network.

- Everything is in `pcbwatch.py`. Standard library only, and keep it that way.
- Test: `python3 test_pcbwatch.py`. It drives the real server and real
  `kicad-cli` against a copy of KiCad's `Arduino_Uno` template. Run it
  after any change.
- Behaviour of `kicad-cli` that the code depends on is listed in
  `docs/how-it-works.md`. Re-measure it rather than trusting it. Pipe
  carefully: `cmd | grep; echo $?` reports grep's exit code.
- UI change? Look at it. Headless Chrome works:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --screenshot=out.png --window-size=1400,850 --virtual-time-budget=5000 "http://127.0.0.1:8765/#w=<id>&k=board"`
  (ids come from `/api/state`).
- Never use the user's real boards in screenshots or docs. Use KiCad's
  bundled templates in `KiCad.app/Contents/SharedSupport/template/`.
- Use `PCBWATCH_CONFIG=<tmp file>` for experiments so `~/.pcbwatch.json`
  is left alone.
