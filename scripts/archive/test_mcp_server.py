"""The MCP server's tools on a synthetic knowledge base: no drive, no CLIP, no music analysis."""

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from videtor import mcp_server as ms

KEY = "Test.xml::Sequence 01"
ROOT = "/Volumes/videtor-test-not-mounted/proj"
EXPECTED_TOOLS = {
    "list_projects", "rank_music", "search_footage", "shotmap_schema", "check_shotmap",
    "cut", "second_look_sheets", "apply_look", "score_cut", "read_back",
}


def video(fid, path, duration=10.0, **extra):
    return {"fid": fid, "path": path, "basename": Path(path).name, "kind": "video", "media": {"duration_s": duration}, "used_by": {}, **extra}


def slot(sid, **kw):
    return {"id": sid, "show": [f"shot {sid}"], **kw}


def call(name, args):
    """A tool call as an MCP client makes it: (is_error, text)."""
    try:
        r = asyncio.run(ms.server.call_tool(name, args))
    except Exception as exc:  # the SDK raises ToolError; the protocol layer turns it into an error result
        return True, str(exc)
    if isinstance(r, tuple):  # mcp 1.x: (content, structured)
        r = r[0]
    content = getattr(r, "content", r)
    return bool(getattr(r, "is_error", getattr(r, "isError", False))), "".join(getattr(c, "text", "") for c in content)


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        kb, footmap, beats = self.tmp / "kb", self.tmp / "footmap", self.tmp / "beats"
        for d in (kb / "footage", kb / "projects", kb / "marks", footmap, beats):
            d.mkdir(parents=True)
        files = [
            video("aaa", f"{ROOT}/A001.MP4"),
            video("bbb", f"{ROOT}/day 2/B002.MP4", 5.0),
            video("ccc", f"{ROOT}/A001 copy.MP4", duplicate_of="aaa"),
            video("ddd", f"{ROOT}/Exports/final.mp4"),
            video("eee", f"{ROOT}/C003.MP4", 0.0),
            video("fff", "/Volumes/videtor-test-not-mounted/other/D004.MP4"),
            {"fid": "ggg", "path": f"{ROOT}/still.png", "basename": "still.png", "kind": "image", "media": {"duration_s": 0.0}, "used_by": {}},
        ]
        index = {"mount": "/Volumes/videtor-test-not-mounted", "roots": {KEY: [ROOT]}, "files": files}
        (kb / "footage" / "test-drive.json").write_text(json.dumps(index))
        (kb / "footage" / "test-drive.summary.json").write_text("{}")  # not an index: must be skipped
        (kb / "footage" / "test-drive.selects.json").write_text("[]")
        (kb / "projects" / "test__sequence-01.json").write_text(json.dumps({"key": KEY}))
        (kb / "marks" / "test.json").write_text(json.dumps({"key": KEY, "marks": {}}))
        (footmap / "aaa.npz").write_bytes(b"")
        (footmap / "aaa.json").write_text("{}")
        (footmap / "bbb.decode.npz").write_bytes(b"")  # decoded only: not mapped yet
        self.example = self.tmp / "example.json"
        patcher = mock.patch.multiple(ms, KB_DIR=kb, FOOTMAP_DIR=footmap, BEATS_DIR=beats, EXAMPLE_SHOTMAP=self.example)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, doc):
        p = self.tmp / name
        p.write_text(json.dumps(doc))
        return p

    def test_tools(self):
        tools = asyncio.run(ms.server.list_tools())
        self.assertEqual({t.name for t in tools}, EXPECTED_TOOLS)
        self.assertTrue(all(t.description for t in tools))
        schema = {t.name: getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) for t in tools}
        self.assertEqual(set(schema["apply_look"]["properties"]), {"picks_path", "choices", "notes"})
        self.assertEqual(schema["cut"]["required"], ["shotmap_path"])

    def test_list_projects(self):
        out = ms.list_projects()
        self.assertEqual(len(out["projects"]), 1)
        p = out["projects"][0]
        self.assertEqual((p["key"], p["drive"], p["mounted"]), (KEY, "test-drive", False))
        self.assertEqual(p["takes"], 2)  # no copy, no render, no zero-length take, nothing outside the roots
        self.assertEqual(p["mapped"], 1)
        self.assertTrue(p["real_edit"])
        self.assertEqual(p["marks"], "test.json")
        json.dumps(out)

    def test_shotmap_schema(self):
        out = ms.shotmap_schema()
        self.assertIn("beats", out["doc"])
        self.assertEqual(out["allowed"]["camera"], ["any", "moving", "steady"])
        self.assertIsNone(out["example"])
        self.example.write_text(json.dumps({"version": 1, "name": "ex"}))
        self.assertEqual(ms.shotmap_schema()["example"]["name"], "ex")
        is_error, text = call("shotmap_schema", {})
        self.assertFalse(is_error)
        self.assertIn('"allowed"', text)

    def test_check_shotmap_without_music(self):
        m = {"version": 1, "name": "t", "key": KEY, "length_s": 3, "fps": 25,
             "sections": [{"name": "a", "slots": [slot("a-1", length_s=1), slot("a-2", length_s=0.6)]}, {"name": "b", "slots": [slot("b-1", length_s=1.4)]}]}
        out = ms.check_shotmap(str(self.write("shotmap.json", m)))
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual([s["id"] for s in out["slots"]], ["a-1", "a-2", "b-1"])
        self.assertEqual([s["length_s"] for s in out["slots"]], [1.0, 0.6, 1.4])  # frame-exact at 25 fps
        self.assertEqual(out["total_s"], 3.0)
        self.assertEqual([(s["name"], s["slots"], s["end_s"]) for s in out["sections"]], [("a", 2, 1.6), ("b", 1, 3.0)])
        self.assertEqual(out["footage"], {"drive": "test-drive", "takes": 2, "mapped": 1})
        self.assertTrue(any("no music" in w for w in out["warnings"]))
        json.dumps(out)

    def test_check_shotmap_on_the_beat(self):
        # a 120 BPM track, cached (the file itself is missing): beats every 0.5 s, a 10 dB lift at 4 s
        rms = [-30.0] * 8 + [-20.0] * 52
        data = {"path": str(self.tmp / "song.wav"), "duration_s": 30.0, "tempo_bpm": 120.0, "beats_s": [i * 0.5 for i in range(60)],
                "rms_db": rms, "onset_half_s": [1.0] * 60}
        (ms.BEATS_DIR / "song.beats.json").write_text(json.dumps(data))
        m = {"version": 1, "name": "t", "key": KEY, "length_s": 4, "fps": 25, "beat_lead_s": 0.12,
             "music": {"path": str(self.tmp / "song.wav"), "in_s": 2.0},
             "sections": [{"name": "s", "slots": [slot("s-1", beats=2), slot("s-2", beats=2), slot("s-3", beats=4, speed=0.5)]}]}
        out = ms.check_shotmap(str(self.write("shotmap.json", m)))
        self.assertTrue(out["ok"], out["problems"])
        # cuts lead the closing beat by 0.12 s, on frames: 0.88, 1.88, end at 4
        self.assertEqual([(s["start_s"], s["length_s"]) for s in out["slots"]], [(0.0, 0.88), (0.88, 1.0), (1.88, 2.12)])
        self.assertEqual(out["slots"][2]["source_s"], 1.06)
        mu = out["music"]
        self.assertEqual((mu["beats_in_cut"], mu["beats_asked"], mu["bpm"]), (8, 8, 120.0))
        self.assertEqual(len(mu["lifts_in_cut"]), 1)
        self.assertAlmostEqual(mu["lifts_in_cut"][0]["at_s"], 2.0)
        self.assertTrue(any("music file not found" in w for w in out["warnings"]))
        self.assertFalse(any("ask for" in w or "not on a beat" in w for w in out["warnings"]))

    def test_check_shotmap_problems(self):
        m = {"version": 2, "length_s": 5, "sections": [{"name": "a", "slots": [{"id": "a-1", "show": [], "beats": 1, "scale": ["huge"]}]}],
             "music": {"path": str(self.tmp / "missing.wav")}}
        out = ms.check_shotmap(str(self.write("bad.json", m)))
        self.assertFalse(out["ok"])
        text = " | ".join(out["problems"])
        for want in ("version must be 1", "a-1: show is empty", "a-1: scale", "music file not found", "key is missing"):
            self.assertIn(want, text)
        self.assertNotIn("slots", out)

    def test_errors_reach_the_model(self):
        is_error, text = call("check_shotmap", {"path": str(self.tmp / "nope.json")})
        self.assertTrue(is_error)
        self.assertIn("shot map not found", text)
        is_error, text = call("search_footage", {"key": "Nope.xml::Seq", "query": "rings"})
        self.assertTrue(is_error)
        self.assertIn(KEY, text)  # names the keys it knows

    def test_apply_look_checks_slot_ids(self):
        picks = {"name": "t", "fps": 25, "width": 1080, "height": 1920, "slots": [{"id": "a", "pick": None, "alternates": []}]}
        p = self.write("picks.json", picks)
        before = p.read_text()
        with self.assertRaises(ms.ToolError) as ctx:
            ms.apply_look(str(p), {"zz": 1})
        self.assertIn("'zz'", str(ctx.exception))
        with self.assertRaises(ms.ToolError):
            ms.apply_look(str(p), {"a": 1})  # no runner-up 1
        self.assertEqual(p.read_text(), before)

    def test_read_back_without_the_module(self):
        import videtor.kb as kb_pkg

        saved = kb_pkg.__dict__.pop("readback", None)  # else `from videtor.kb import readback` finds the attribute
        try:
            with mock.patch.dict(sys.modules, {"videtor.kb.readback": None}):
                with self.assertRaises(ms.ToolError) as ctx:
                    ms.read_back(str(self.tmp / "picks.json"))
        finally:
            if saved is not None:
                kb_pkg.readback = saved
        self.assertIn("not available", str(ctx.exception))

    def test_plain(self):
        out = ms._plain({"a": np.float32(1.5), "b": (np.int64(2), Path("/x")), "c": float("nan"), "d": np.array([True])})
        self.assertEqual(out, {"a": 1.5, "b": [2, "/x"], "c": None, "d": [True]})


if __name__ == "__main__":
    unittest.main()
