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


class TextCacheTests(unittest.TestCase):
    """The text embeddings kept in memory and on disk, with a fake model that counts what it is asked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name, "text-emb")
        self.asked: list[list[str]] = []

    def tearDown(self):
        self.tmp.cleanup()

    def encode(self, texts):
        self.asked.append(list(texts))
        rng = np.random.default_rng(len(self.asked))  # another call, another vector: a stale one would show
        return tags._normalize(rng.normal(size=(len(texts), 8)))

    def test_a_list_asked_twice_is_encoded_once_and_comes_back_the_same(self):
        cache = tags.TextCache(self.encode, self.folder, "model-a")
        first = cache(["people dancing", "a crowd"])
        first[:] = 0  # what a caller does to its copy stays with it
        again = cache(["people dancing", "a crowd"])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(again.dtype, np.float32)
        self.assertTrue(np.allclose(np.linalg.norm(again, axis=1), 1.0))
        cache(["a crowd", "people dancing"])  # another list (a vector moves with its batch): encoded as asked
        self.assertEqual(self.asked, [["people dancing", "a crowd"], ["a crowd", "people dancing"]])

    def test_a_new_process_reads_the_vectors_back_from_disk_without_the_model(self):
        want = tags.TextCache(self.encode, self.folder, "model-a")(["rings close-up"])
        later = tags.TextCache(lambda texts: self.fail("the model was asked again"), self.folder, "model-a")
        self.assertTrue(np.array_equal(later(["rings close-up"]), want))  # bit for bit
        self.assertEqual(len(list(self.folder.glob("*.npy"))), 1)
        self.assertFalse(list(self.folder.glob(".*")))  # no half-written file left behind

    def test_another_model_or_prompt_form_never_gets_these_vectors(self):
        a = tags.TextCache(self.encode, self.folder, "model-a")(["rings close-up"])
        b = tags.TextCache(self.encode, self.folder, "model-b")(["rings close-up"])
        self.assertEqual(len(self.asked), 2)
        self.assertFalse(np.array_equal(a, b))
        self.assertEqual(len(list(self.folder.glob("*.npy"))), 2)

    def test_without_a_folder_or_with_a_broken_file_it_still_answers(self):
        mem = tags.TextCache(self.encode, None, "model-a")
        mem(["x"]), mem(["x"])
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(self.folder.exists())
        cache = tags.TextCache(self.encode, self.folder, "model-a")
        self.folder.mkdir()
        (self.folder / f"{cache.key(['y'])}.npy").write_bytes(b"not an array")
        self.assertEqual(cache(["y"]).shape, (1, 8))
        self.assertEqual(len(self.asked), 2)

    def test_the_real_cache_is_keyed_by_the_model_its_weights_and_the_prompt(self):
        self.assertIn(tags.MODEL, tags._texts.model)
        self.assertIn(tags.PRETRAINED, tags._texts.model)
        self.assertIn(tags.PROMPT, tags._texts.model)
        self.assertEqual(tags._texts.folder, Path("cache/text-emb"))


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
