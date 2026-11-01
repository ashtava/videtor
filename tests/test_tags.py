import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from videtor.analyze import tags

try:
    import open_clip  # noqa: F401
    import torch  # noqa: F401

    HAVE_CLIP = True
except ImportError:
    HAVE_CLIP = False


def _solid(rgb, size=(480, 270)) -> Image.Image:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[...] = rgb
    return Image.fromarray(arr)


def _checker(size=(480, 270), block=24) -> Image.Image:
    y, x = np.indices((size[1], size[0]))
    img = (((x // block + y // block) % 2) * 255).astype(np.uint8)
    return Image.fromarray(np.stack([img, img, img], axis=-1))


class GroupingTests(unittest.TestCase):
    def test_two_clusters_and_an_outlier(self):
        vecs = {  # interleaved so the grouping cannot rely on insertion order
            "a1": [1.0, 0.05, 0.0, 0.0],
            "b1": [0.0, 1.0, 0.05, 0.0],
            "a2": [1.0, -0.05, 0.0, 0.0],
            "lone": [0.0, 0.0, 0.0, 1.0],
            "b2": [0.0, 1.0, -0.05, 0.0],
            "a3": [1.0, 0.0, 0.05, 0.0],
            "a4": [1.0, 0.0, 0.0, 0.05],
        }
        groups = tags.group_by_similarity(vecs)
        self.assertEqual(set(groups), set(vecs))
        self.assertEqual({groups["a1"], groups["a2"], groups["a3"], groups["a4"]}, {0})  # largest group is 0
        self.assertEqual(groups["b1"], groups["b2"])
        self.assertEqual(len({groups["a1"], groups["b1"], groups["lone"]}), 3)
        self.assertEqual(sorted(set(groups.values())), [0, 1, 2])

    def test_threshold_edges_and_empty_input(self):
        vecs = {"x": [1.0, 0.0], "y": [0.0, 1.0], "z": [-1.0, 0.0]}
        self.assertEqual(set(tags.group_by_similarity(vecs, threshold=-1.0).values()), {0})
        self.assertEqual(len(set(tags.group_by_similarity(vecs, threshold=0.99).values())), 3)
        self.assertEqual(tags.group_by_similarity({}), {})


class VocabularyTests(unittest.TestCase):
    def test_vocabulary_is_clean(self):
        self.assertTrue(20 <= len(tags.VOCABULARY) <= 30)
        self.assertEqual(len(set(tags.VOCABULARY)), len(tags.VOCABULARY))
        for tag in tags.VOCABULARY:
            self.assertEqual(tag, tag.strip().lower())
            self.assertTrue(tag)
            self.assertIn(tag, tags.PROMPT.format(tag=tag))

    def test_clip_tags_without_readable_frames_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "x_0.jpg").write_bytes(b"not a jpeg")
            self.assertIsNone(tags.clip_tags([Path(tmp, "x_0.jpg"), Path(tmp, "x_1.jpg")]))


@unittest.skipUnless(HAVE_CLIP, "needs torch and open_clip (uv sync)")
class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.paths = [Path(cls.tmp.name, f"c_{i}.jpg") for i in range(3)]
        for img, p in zip([_solid((220, 0, 0)), _solid((0, 0, 220)), _checker()], cls.paths):
            img.save(p, quality=95)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_embeddings_are_unit_vectors_and_scores_sum_to_one(self):
        emb = tags.embed_images(self.paths)
        self.assertEqual(emb.shape, (3, 512))
        self.assertEqual(emb.dtype, np.float32)
        self.assertTrue(np.allclose(np.linalg.norm(emb, axis=1), 1.0, atol=1e-3))
        self.assertLess(float(emb[0] @ emb[1]), 0.999)  # red and blue do not embed identically
        scores = tags.tag_scores(emb)
        self.assertEqual(scores.shape, (3, len(tags.VOCABULARY)))
        self.assertTrue(np.allclose(scores.sum(axis=1), 1.0, atol=1e-4))
        self.assertTrue((scores >= 0).all())
        self.assertEqual(tags.embed_images([]).shape, (0, 512))

    def test_clip_tags_record(self):
        broken = Path(self.tmp.name, "broken.jpg")
        broken.write_bytes(b"not a jpeg")
        out = tags.clip_tags([*self.paths, broken])
        self.assertEqual(set(out), {"frames", "tags", "scores", "embedding"})
        self.assertEqual(out["frames"], 3)
        self.assertEqual(len(out["tags"]), 5)
        self.assertEqual(set(out["scores"]), set(tags.VOCABULARY))
        top = list(out["tags"].values())
        self.assertEqual(top, sorted(top, reverse=True))
        self.assertTrue(all(out["scores"][t] == s for t, s in out["tags"].items()))
        self.assertGreaterEqual(min(top), max(s for t, s in out["scores"].items() if t not in out["tags"]))
        self.assertEqual(len(out["embedding"]), 512)
        self.assertTrue(all(isinstance(x, float) for x in out["embedding"]))
        self.assertAlmostEqual(float(np.linalg.norm(out["embedding"])), 1.0, delta=0.01)


if __name__ == "__main__":
    unittest.main()
