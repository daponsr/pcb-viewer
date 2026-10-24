# Contributing

## Run it from source

```bash
python3 pcbwatch.py --no-open --port 8765
```

## Test

```bash
python3 test_pcbwatch.py      # ~20 s
```

The test starts the real server against a copy of KiCad's bundled
`Arduino_Uno` template and asserts on what it serves. It checks discovery,
watching, per-layer export, an edit to one layer, corrupted and repaired
files, cross-origin refusal, and unwatching.

It needs KiCad 9 installed. On a non-macOS machine, point `PCBWATCH_TEMPLATE`
at a KiCad project directory and `KICAD_CLI` at the binary.

## Pull requests

`main` only changes through pull requests, and every pull request needs an
approval from the code owner ([`.github/CODEOWNERS`](.github/CODEOWNERS)).
Force-pushes to `main` and deleting it are blocked for everyone.

## Rules of the house

- **One file, standard library only.** People should be able to copy
  `pcbwatch.py` somewhere and run it. If a feature needs a dependency, it
  probably belongs in a different tool.
- **Verify against the running `kicad-cli`, not its docs.** Several
  assumptions made while building this turned out to be wrong:
  - the exit code on a corrupt schematic (a `| grep` had hidden it);
  - what "board area" frames.

  Each was caught by running something. The table in
  [docs/how-it-works.md](docs/how-it-works.md) lists what has been
  measured; re-measure it when bumping KiCad.
- **A test that cannot fail is not a test.** When adding an assertion,
  break the code it guards once and watch it fail.
- **Read-only, local-only.** pcbwatch never writes to a project and binds
  to `127.0.0.1`. Keep it that way.
- Design changes go in `docs/superpowers/specs/` first, including
  corrections when a claim turns out wrong.
