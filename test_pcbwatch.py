#!/usr/bin/env python3
"""End-to-end test: runs pcbwatch against a copy of a KiCad template project.

    uv run python test_pcbwatch.py

Asserts against the running server, not against the code.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pcbwatch  # noqa: E402


def find_template():
    """KiCad's bundled Arduino_Uno template, wherever this OS's KiCad keeps it."""
    if os.environ.get("PCBWATCH_TEMPLATE"):
        return Path(os.environ["PCBWATCH_TEMPLATE"])
    cli = Path(pcbwatch.find_kicad_cli()).resolve()
    for share in (
        cli.parent.parent / "SharedSupport" / "template",   # macOS: KiCad.app/Contents/MacOS/kicad-cli
        cli.parent.parent / "share" / "kicad" / "template",  # Windows: KiCad/9.0/bin/kicad-cli.exe, Linux /usr/local
        Path("/usr/share/kicad/template"),                   # Linux packages
    ):
        if (share / "Arduino_Uno").is_dir():
            return share / "Arduino_Uno"
    raise unittest.SkipTest("KiCad's Arduino_Uno template not found; set PCBWATCH_TEMPLATE")


TEMPLATE = find_template()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pcbwatch-test-"))
        cls.proj = cls.tmp / "proj"
        shutil.copytree(TEMPLATE, cls.proj)
        cls.stem = next(cls.proj.glob("*.kicad_pro")).stem
        cls.sch = cls.proj / f"{cls.stem}.kicad_sch"
        cls.pcb = cls.proj / f"{cls.stem}.kicad_pcb"
        cls.config = cls.tmp / "config.json"
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = dict(os.environ, PCBWATCH_CONFIG=str(cls.config))
        cls.server = subprocess.Popen(
            [sys.executable, str(HERE / "pcbwatch.py"), "--no-open", "--port", str(cls.port), "--root", str(cls.tmp)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.wait(lambda: cls.get("/api/state"), "server up")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------
    @classmethod
    def get(cls, path, raw=False):
        with urllib.request.urlopen(cls.base + path, timeout=5) as r:
            body = r.read()
            return body if raw else json.loads(body)

    @classmethod
    def post(cls, path, body, headers=None):
        req = urllib.request.Request(cls.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    @staticmethod
    def wait(cond, what, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = cond()
                if v:
                    return v
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        raise AssertionError(f"timed out waiting for {what}")

    def watch(self):
        return next(w for w in self.get("/api/state")["watches"] if w["id"] == self.wid)

    def view(self, kind):
        return next(v for v in self.watch()["views"] if v["kind"] == kind)

    def svg(self, key, layer=None):
        return self.get(f"/svg?w={self.wid}&v={key}" + (f"&layer={layer}" if layer else ""), raw=True)

    def layers(self):
        return {l["name"]: l for l in self.view("pcb")["layers"]}

    # ---- the test, in order ------------------------------------------
    def test_lifecycle(self):
        # Discovery finds the copied project.
        projects = self.wait(lambda: self.get("/api/projects")["projects"], "discovery")
        self.assertEqual([p["name"] for p in projects], [self.stem])

        # Bad paths are refused.
        code, r = self.post("/api/watch", {"path": str(self.tmp / "nope")})
        self.assertEqual(code, 400, r)

        # Cross-origin pages cannot change the watch list.
        code, _ = self.post("/api/watch", {"path": str(self.proj)}, {"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        self.assertEqual(self.get("/api/state")["watches"], [])

        # Watch by folder: a sheet and the board render, as real SVG.
        code, r = self.post("/api/watch", {"path": str(self.proj)})
        self.assertEqual(code, 200, r)
        self.wid = r["id"]
        self.wait(lambda: {v["kind"] for v in self.watch()["views"]} == {"sch", "pcb"}, "both views")
        sch, pcb = self.view("sch"), self.view("pcb")
        self.assertEqual((sch["version"], pcb["version"]), (1, 1))
        self.assertIn(b"<svg", self.svg(sch["key"])[:1000])

        # The board is every layer it defines, under canonical names, each servable on its own.
        layers = self.layers()
        for name in ("F.Cu", "B.Cu", "F.Silkscreen", "B.Silkscreen", "Edge.Cuts", "F.Mask"):
            self.assertIn(name, layers)
        self.assertNotIn("F.SilkS", layers)  # legacy name from the file, not shown
        self.assertFalse(layers["F.Cu"]["empty"])
        self.assertFalse(layers["Edge.Cuts"]["empty"])
        self.assertTrue(layers["F.Adhesive"]["empty"])
        self.assertNotEqual(layers["F.Cu"]["color"], layers["B.Cu"]["color"])
        self.assertLess(layers["B.Cu"]["z"], layers["F.Cu"]["z"])
        self.assertLess(layers["F.Cu"]["z"], layers["Edge.Cuts"]["z"])
        self.assertIn(b"<svg", self.svg("board", "F.Cu")[:1000])
        self.assertNotEqual(self.svg("board", "F.Cu"), self.svg("board", "B.Cu"))
        with self.assertRaises(urllib.error.HTTPError):
            self.svg("board", "No.Such.Layer")

        # A silkscreen-only edit bumps that layer and the board, nothing else. The text goes in
        # the middle of the Arduino_Uno outline: anything that grows the board's bounding box
        # reframes every layer, which would (correctly) bump them all.
        good_pcb = self.pcb.read_text()
        self.pcb.write_text(good_pcb.rstrip()[:-1] +
                            '\n\t(gr_text "AGENT" (at 131.1 73.3 0) (layer "F.SilkS")'
                            ' (effects (font (size 3 3) (thickness 0.5))))\n)\n')
        self.wait(lambda: self.layers()["F.Silkscreen"]["version"] == 2, "silkscreen re-render")
        layers = self.layers()
        self.assertEqual(layers["F.Cu"]["version"], 1)
        self.assertEqual(layers["B.Silkscreen"]["version"], 1)
        self.assertEqual(self.view("pcb")["version"], 2)
        self.pcb.write_text(good_pcb)
        self.wait(lambda: self.layers()["F.Silkscreen"]["version"] == 3, "silkscreen restored")
        self.assertEqual(self.watch()["errors"], {})
        self.assertEqual(json.loads(self.config.read_text())["watched"], [[str(self.proj.resolve()), self.stem]])

        # An edit to the schematic bumps the sheet, not the board.
        good_sch = self.sch.read_text()
        self.sch.write_text(good_sch.rstrip()[:-1] +
                            '\n  (text "AGENT WAS HERE" (at 60 60 0) (effects (font (size 5 5))))\n)\n')
        self.wait(lambda: self.view("sch")["version"] == 2, "schematic re-render")
        self.assertEqual(self.view("pcb")["version"], 3)
        last_good = self.svg(sch["key"])

        # A touch that changes nothing visible does not bump the version.
        os.utime(self.sch)
        time.sleep(4)
        self.assertEqual(self.view("sch")["version"], 2)

        # Corrupt schematic: error shown, last render kept.
        self.sch.write_text("(kicad_sch (version")
        self.wait(lambda: "sch" in self.watch()["errors"], "schematic error")
        self.assertEqual(self.svg(sch["key"]), last_good)

        # Corrupt board: error shown, layers still served.
        self.pcb.write_text("garbage")
        self.wait(lambda: "pcb" in self.watch()["errors"], "board error")
        self.assertIn(b"<svg", self.svg("board", "F.Cu")[:1000])

        # Repair both: errors clear.
        self.sch.write_text(good_sch)
        self.pcb.write_text(good_pcb)
        self.wait(lambda: self.watch()["errors"] == {}, "errors cleared")

        # Unwatch.
        code, _ = self.post("/api/unwatch", {"id": self.wid})
        self.assertEqual(code, 200)
        self.assertEqual(self.get("/api/state")["watches"], [])
        self.assertEqual(json.loads(self.config.read_text())["watched"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
