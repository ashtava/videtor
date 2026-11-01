import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from videtor.analyze import frames


def _checker(size=(480, 270), block=12) -> Image.Image:
    y, x = np.indices((size[1], size[0]))
    img = (((x // block + y // block) % 2) * 255).astype(np.uint8)
    return Image.fromarray(np.stack([img, img, img], axis=-1))


class FrameFeatureTests(unittest.TestCase):
    def test_blur_lowers_sharpness_and_keeps_brightness(self):
        with tempfile.TemporaryDirectory() as tmp:
            sharp, blurred = Path(tmp, "a_0.jpg"), Path(tmp, "a_1.jpg")
            _checker().save(sharp, quality=95)
            _checker().filter(ImageFilter.GaussianBlur(6)).save(blurred, quality=95)
            s = frames.frame_features([sharp])
            b = frames.frame_features([blurred])
            self.assertGreater(s["sharpness"], 5 * b["sharpness"])
            self.assertAlmostEqual(s["brightness"], b["brightness"], delta=8)
            self.assertLess(s["colorfulness"], 1.0)  # grey frames carry no color

    def test_exposure_shares_and_colorfulness(self):
        dark = np.zeros((270, 480, 3), dtype=np.uint8)
        red = np.zeros((270, 480, 3), dtype=np.uint8)
        red[..., 0] = 220
        with tempfile.TemporaryDirectory() as tmp:
            Image.fromarray(dark).save(Path(tmp, "d.jpg"))
            Image.fromarray(red).save(Path(tmp, "r.jpg"))
            d = frames.frame_features([Path(tmp, "d.jpg")])
            r = frames.frame_features([Path(tmp, "r.jpg")])
            self.assertGreater(d["dark_share"], 0.95)
            self.assertGreater(r["colorfulness"], 50)
            self.assertEqual(d["frames"], 1)

    def test_missing_or_broken_frames_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "x_0.jpg").write_bytes(b"not a jpeg")
            self.assertIsNone(frames.frame_features([Path(tmp, "x_0.jpg"), Path(tmp, "x_1.jpg")]))
            self.assertEqual(frames.clip_frames(Path(tmp), "x"), [Path(tmp, "x_0.jpg")])


if __name__ == "__main__":
    unittest.main()
