import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videtor.analyze import run
from videtor.kb import footage

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class AnalyzeRunTests(unittest.TestCase):
    def test_index_to_cached_records_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            shoot = Path(tmp, "Shoot", "Cam A")
            shoot.mkdir(parents=True)
            clip = shoot / "C0001.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=25", "-f", "lavfi",
                 "-i", "aevalsrc=0.2*sin(440*2*PI*t):s=48000", "-t", "3", "-pix_fmt", "yuv420p", "-shortest", str(clip)],
                check=True,
            )
            (shoot / "music.wav").write_bytes(b"")  # unreadable: must be skipped, not crash
            projects = [{"key": "x.xml::S", "source_xml": "/nowhere/x.xml", "sources": [str(clip)]}]
            index = footage.scan(tmp, projects, log=lambda s: None)
            frames_dir, out_dir = Path(tmp, "frames"), Path(tmp, "analysis")
            footage.extract_frames(index, frames_dir, log=lambda s: None)

            base = ("frames", "audio", "motion")  # the model-backed features (faces, tags) have their own tests
            counts = run.analyze_index(index, frames_dir, out_dir, base, workers=1, log=lambda s: None)
            self.assertEqual(counts, {"frames": 1, "audio": 1, "motion": 1})
            fid = next(e["fid"] for e in index["files"] if e["basename"] == "C0001.mp4")
            record = json.loads((out_dir / f"{fid}.json").read_text())
            self.assertEqual(record["frames"]["frames"], 3)
            self.assertTrue(record["audio"]["audio"])
            self.assertFalse(record["audio"]["speech"])
            self.assertGreater(record["motion"]["motion"]["median"], 1.0)
            self.assertEqual(record["motion"]["shot_boundaries"], [])

            # a second pass finds everything cached
            self.assertEqual(run.analyze_index(index, frames_dir, out_dir, base, workers=1, log=lambda s: None), {"frames": 0, "audio": 0, "motion": 0})
            cov = run.coverage(index, out_dir, base)
            self.assertEqual(cov["clips"], 1)  # the empty wav has no duration and is not analyzable
            self.assertEqual(cov["have"], {"frames": 1, "audio": 1, "motion": 1})


if __name__ == "__main__":
    unittest.main()
