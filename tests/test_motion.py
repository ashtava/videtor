import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videtor.analyze import motion

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def render(path: Path, lavfi: str, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", lavfi, "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class MotionTests(unittest.TestCase):
    def test_static_frame_has_no_motion_and_no_cuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp, "static.mp4")
            render(clip, "color=c=gray:s=320x180:r=25", 3)
            m = motion.motion_features(str(clip))
            self.assertLess(m["motion"]["median"], 1.0)
            self.assertGreater(m["static_share"], 0.9)
            self.assertEqual(m["shot_boundaries"], [])

    def test_moving_pattern_registers_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp, "moving.mp4")
            render(clip, "testsrc=s=320x180:r=25", 3)  # scrolling test pattern with a running counter
            m = motion.motion_features(str(clip))
            self.assertGreater(m["motion"]["median"], 2.0)
            self.assertLess(m["static_share"], 0.2)

    def test_hard_cut_between_two_colors_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, joined = Path(tmp, "a.mp4"), Path(tmp, "b.mp4"), Path(tmp, "cut.mp4")
            render(a, "color=c=black:s=320x180:r=25", 2)
            render(b, "color=c=white:s=320x180:r=25", 2)
            Path(tmp, "list.txt").write_text(f"file '{a}'\nfile '{b}'\n")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(Path(tmp, "list.txt")), "-c", "copy", str(joined)], check=True)
            m = motion.motion_features(str(joined))
            self.assertEqual(len(m["shot_boundaries"]), 1)
            self.assertAlmostEqual(m["shot_boundaries"][0], 2.0, delta=0.3)
            self.assertLess(m["motion"]["median"], 1.0)  # the cut itself is left out of the motion level

    def test_audio_only_file_has_no_motion(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp, "tone.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "aevalsrc=0.2*sin(440*2*PI*t):s=16000:d=1", str(wav)], check=True)
            self.assertIsNone(motion.motion_features(str(wav)))


if __name__ == "__main__":
    unittest.main()
