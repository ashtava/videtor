import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from statistics import median

from videtor.analyze import beats

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class BeatTests(unittest.TestCase):
    def test_click_track_at_120_bpm(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp, "click.wav")
            # a decaying 1 kHz tick every 0.5 s for 12 s
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "aevalsrc=0.9*exp(-mod(t\\,0.5)*40)*sin(2*PI*1000*t):s=22050:d=12", str(wav)],
                check=True,
            )
            b = beats.track_beats(wav)
            self.assertTrue(110 <= b["tempo_bpm"] <= 130 or 55 <= b["tempo_bpm"] <= 65, b["tempo_bpm"])  # half-time is an acceptable reading
            gaps = [y - x for x, y in zip(b["beats_s"], b["beats_s"][1:])]
            self.assertAlmostEqual(median(gaps) % 0.5, 0.0, delta=0.06)
            self.assertGreater(len(b["beats_s"]), 10)

    def test_beat_lengths_snap_to_the_nearest_beat(self):
        grid = [i * 0.5 for i in range(40)]
        self.assertEqual(beats.beat_lengths(grid, 0.0, 1.1), (1.0, 1.0))  # 2 beats
        self.assertEqual(beats.beat_lengths(grid, 3.2, 0.8), (0.8, 4.0))  # 3.2 → 4.0 (next beats 3.5, 4.0; 0.8 wants 4.0)
        self.assertEqual(beats.beat_lengths(grid, 19.6, 3.0), (3.0, 22.6))  # beyond the grid: target length kept


if __name__ == "__main__":
    unittest.main()
