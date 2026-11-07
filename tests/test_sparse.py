import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from videtor.analyze import sparse
from videtor.kb import footage

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def make_clip(path: Path, seconds: float, fps: int = 25, gop: int = 12) -> None:
    """A test pattern with a keyframe every `gop` frames and a steady tone (the VAD must not call it speech)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={fps}", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
         "-t", f"{seconds}", "-c:v", "mpeg4", "-q:v", "4", "-g", str(gop), "-c:a", "pcm_s16le", "-shortest", str(path)],
        check=True,
    )


def entry_for(path: Path) -> dict:
    return {"fid": footage.file_id(str(path)), "path": str(path), "basename": path.name, "kind": "video", "media": footage.probe(str(path))}


def one_face(rgb: np.ndarray) -> tuple[list[list[float]], int]:
    return [[0.4, 0.3, 0.6, 0.6]], 1


def fake_embed(images: list) -> np.ndarray:
    out = np.zeros((len(images), 512), np.float32)
    out[:, 0] = 1.0
    return out


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class ScanClipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "sparse"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_short_clip_is_sampled_from_its_keyframes_in_one_pass(self):
        clip = self.tmp / "short.mov"
        make_clip(clip, 4.0)
        res = sparse.scan_clip(entry_for(clip), self.cache, detect=one_face)
        self.assertTrue(res["ok"], res["error"])
        rec, a = res["record"], res["arrays"]
        self.assertFalse(rec["seek"])
        self.assertGreaterEqual(rec["n"], 3)
        self.assertLessEqual(rec["n"], sparse.MAX_STILLS)
        self.assertEqual(rec["t"], sorted(rec["t"]))
        self.assertTrue(all(b - a_ >= sparse.MIN_STEP_S - 0.05 for a_, b in zip(rec["t"], rec["t"][1:])))
        self.assertEqual(rec["faces"]["share"], 1.0)
        self.assertAlmostEqual(rec["faces"]["area_med"], 0.06, places=3)
        self.assertEqual(rec["motion"]["steps"], rec["n"] - 1)
        self.assertTrue(rec["audio"]["audio"])
        self.assertEqual(rec["audio"]["speech_share"], 0.0)
        self.assertEqual(a["emb"].shape if "emb" in a else (0,), (0,))  # CLIP is the caller's part
        self.assertEqual(len(a["t"]), rec["n"])
        for i in range(rec["n"]):
            self.assertTrue(sparse.still_path(self.cache, rec["fid"], i).exists())

    def test_a_long_clip_is_sampled_by_seeking(self):
        clip = self.tmp / "long.mov"
        make_clip(clip, sparse.SEEK_OVER_S + 10, fps=10, gop=10)
        res = sparse.scan_clip(entry_for(clip), self.cache, detect=one_face)
        self.assertTrue(res["ok"], res["error"])
        rec = res["record"]
        self.assertTrue(rec["seek"])
        self.assertEqual(rec["n"], sparse.MAX_STILLS)
        self.assertEqual(rec["t"], sorted(rec["t"]))
        self.assertGreater(rec["t"][-1], rec["duration_s"] * 0.8)

    def test_a_missing_file_is_an_error_not_a_raise(self):
        res = sparse.scan_clip({"fid": "nope", "path": str(self.tmp / "missing.mov"), "basename": "missing.mov", "media": {"duration_s": 3}}, self.cache)
        self.assertFalse(res["ok"])
        self.assertIn("FileNotFoundError", res["error"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "sparse"
        self.clip = self.tmp / "a.mov"
        make_clip(self.clip, 3.0)
        self.entry = entry_for(self.clip)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_embeds_saves_and_resumes(self):
        logs = []
        s = sparse.scan([self.entry], self.cache, log=logs.append, embed=fake_embed, detect=one_face)
        self.assertEqual((s["built"], s["already_done"], s["failed"]), (1, 0, {}))
        self.assertTrue(sparse.is_done(self.entry, self.cache))
        rec = sparse.load(self.entry["fid"], self.cache)
        self.assertEqual(rec["a"]["emb"].shape, (rec["n"], 512))
        self.assertEqual(rec["a"]["emb"].dtype, np.float16)
        self.assertEqual(len(rec["a"]["shift"]), rec["n"] - 1)
        again = sparse.scan([self.entry], self.cache, log=logs.append, embed=fake_embed, detect=one_face)
        self.assertEqual((again["built"], again["already_done"]), (0, 1))

    def test_a_bad_clip_is_reported_and_the_rest_built(self):
        bad = {"fid": "bad", "path": str(self.tmp / "none.mov"), "basename": "none.mov", "media": {"duration_s": 2}}
        s = sparse.scan([bad, self.entry], self.cache, log=lambda _: None, embed=fake_embed, detect=one_face)
        self.assertEqual(s["built"], 1)
        self.assertIn("bad", s["failed"])


if __name__ == "__main__":
    unittest.main()
