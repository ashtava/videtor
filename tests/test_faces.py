import importlib.util
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from videtor.analyze import faces

HAVE_MEDIAPIPE = importlib.util.find_spec("mediapipe") is not None


class ScaleTests(unittest.TestCase):
    def test_classify_scale_boundaries(self):
        self.assertEqual(faces.classify_scale(0.0, 0, 0), "detail")
        self.assertEqual(faces.classify_scale(0.0, 0, 2), "detail")  # hands alone stay a detail shot
        self.assertEqual(faces.classify_scale(0.035, 1, 0), "close")
        self.assertEqual(faces.classify_scale(0.6, 2, 2), "close")
        self.assertEqual(faces.classify_scale(0.034, 1, 0), "medium")
        self.assertEqual(faces.classify_scale(0.008, 1, 0), "medium")
        self.assertEqual(faces.classify_scale(0.0079, 1, 0), "wide")
        self.assertEqual(faces.classify_scale(0.001, 3, 1), "wide")

    def test_is_partial_on_each_edge_and_in_the_middle(self):
        self.assertFalse(faces.is_partial([0.3, 0.3, 0.6, 0.6]))
        self.assertTrue(faces.is_partial([0.0, 0.3, 0.3, 0.6]))  # touches the left edge
        self.assertTrue(faces.is_partial([0.3, 0.015, 0.6, 0.4]))  # within the margin of the top
        self.assertTrue(faces.is_partial([0.6, 0.3, 1.05, 0.6]))  # crosses the right edge
        self.assertTrue(faces.is_partial([0.3, 0.5, 0.6, 0.98]))  # on the bottom margin
        self.assertFalse(faces.is_partial([0.03, 0.3, 0.6, 0.6]))
        self.assertTrue(faces.is_partial([0.03, 0.3, 0.6, 0.6], margin=0.05))

    def test_visible_area_clips_to_the_frame(self):
        self.assertAlmostEqual(faces.visible_area([0.25, 0.25, 0.75, 0.75]), 0.25)
        self.assertAlmostEqual(faces.visible_area([-0.5, 0.0, 0.5, 1.0]), 0.5)
        self.assertEqual(faces.visible_area([1.1, 0.2, 1.4, 0.5]), 0.0)


class ClipFeatureTests(unittest.TestCase):
    def test_no_frames_or_unreadable_frames_give_none(self):
        self.assertIsNone(faces.clip_features([]))
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "x_0.jpg").write_bytes(b"not a jpeg")
            self.assertIsNone(faces.clip_features([Path(tmp, "x_0.jpg"), Path(tmp, "x_1.jpg")]))

    @unittest.skipUnless(HAVE_MEDIAPIPE, "needs mediapipe (uv sync)")
    def test_blank_and_noise_frames_hold_nobody(self):
        grey = np.full((854, 480, 3), 128, dtype=np.uint8)
        noise = np.random.default_rng(0).integers(0, 256, (270, 480, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp, "n_0.jpg"), Path(tmp, "n_1.jpg")]
            Image.fromarray(grey).save(paths[0], quality=95)
            Image.fromarray(noise).save(paths[1], quality=95)
            for p in paths:
                self.assertEqual(
                    faces.frame_faces(p),
                    {"faces": 0, "largest_area": 0.0, "largest_box": None, "partial": False, "hands": 0},
                )
            self.assertEqual(
                faces.clip_features(paths + [Path(tmp, "n_2.jpg")]),
                {
                    "frames": 2,
                    "faces_max": 0,
                    "face_area": 0.0,
                    "face_frames": 0.0,
                    "partial_share": 0.0,
                    "hands_share": 0.0,
                    "scale": "detail",
                },
            )


if __name__ == "__main__":
    unittest.main()
