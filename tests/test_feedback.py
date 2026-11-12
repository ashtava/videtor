"""The feedback loop on synthetic projects: choices and verdicts kept in one store, a finished render read back
against the picks, taste and keeper candidates, and the evaluation gate that keeps or refuses them."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from test_fill import fake_embed, fake_take
from test_pipeline import KEY, the_map, write_project
from videtor import events, pipeline
from videtor.kb import feedback, renderback
from videtor.plan import evaluate, taste

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class Project(unittest.TestCase):
    """A synthetic project on disk, cut once."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k, hands=0) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
        self.paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse",
                                    beats=self.root / "cache" / "beats")
        self.sm = write_project(self.root, self.takes, the_map(bucket="event"))
        self.out = self.sm.parent
        self.cut()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cut(self) -> dict:
        return pipeline.cut(self.sm, sheets=False, sfx=False, paths=self.paths, embed_texts=fake_embed, fresh=True)["picks"]

    def picks(self) -> dict:
        return json.loads((self.out / "picks.json").read_text())


class StoreTests(Project):
    def test_a_choice_a_person_makes_teaches_and_a_machines_does_not(self):
        before = {s["id"]: s for s in self.picks()["slots"]}
        pipeline.look(self.out / "picks.json", {"o2": 1}, {"o2": "sharper"}, paths=self.paths, by="second look")
        self.assertEqual(feedback.all_docs(self.paths.kb), [])  # Claude's second look is not the user's taste
        self.cut()
        pipeline.look(self.out / "picks.json", {"o2": 1}, {"o2": "sharper"}, paths=self.paths, by="you")
        doc = feedback.load("proj", self.paths.kb)
        self.assertEqual((doc["key"], doc["bucket"]), (KEY, "event"))
        (row,) = feedback.teaching_rows(doc)
        self.assertEqual((row["id"], row["show"], row["by"], row["why"]), ("o2", "axis 1", "you", "sharper"))
        self.assertEqual(row["picked"]["name"], before["o2"]["pick"]["name"])
        self.assertEqual(row["chosen"]["name"], before["o2"]["alternates"][0]["name"])
        pipeline.look(self.out / "picks.json", {"o2": 1}, paths=self.paths, by="you")  # changed their mind: one row a slot
        self.assertEqual(len(feedback.teaching_rows(feedback.load("proj", self.paths.kb))), 1)

    def test_a_verdict_and_a_read_back_live_in_the_same_file(self):
        pipeline.verdict(self.out / "picks.json", False, "too slow", self.paths)
        self.assertEqual(json.loads((self.out / "verdict.json").read_text())["good"], False)
        picks = self.picks()
        s = picks["slots"][1]
        seen = {"fid": s["alternates"][0]["fid"], "in_s": 1.0, "out_s": 2.0}
        result = {"name": "proj", "sequence": "proj", "source": "render:/x/final.mp4", "picks": str(self.out / "picks.json"),
                  "summary": {"file_match_share": 1.0}, "added": [{"fid": "C0", "in_s": 0.0, "out_s": 1.0}],
                  "slots": [{"id": s["id"], "status": "swapped_to_alternate", "visible": seen, "pick": s["pick"], "also_visible": []}]}
        feedback.record_readback(result, picks, self.paths.kb)
        doc = feedback.load("proj", self.paths.kb)
        self.assertEqual(doc["verdict"]["note"], "too slow")  # the read-back did not wipe the verdict
        self.assertTrue(feedback.is_final(doc))
        self.assertEqual(doc["slots"][0]["show"], "axis 1")  # the slot's description travels with the row
        self.assertEqual(feedback.used_ranges(doc), {seen["fid"]: [(1.0, 2.0)], "C0": [(0.0, 1.0)]})
        self.assertEqual(feedback.state(self.paths.kb), {"projects": 1, "finals": 1, "by_hand": 0, "rows": 1, "verdicts": {"good": 0, "not": 1}})
        wrong = {**result, "sequence": "another cut", "summary": {"file_match_share": 0.1}}
        with self.assertRaises(Exception):
            feedback.record_readback(wrong, picks, self.paths.kb)  # not this cut: refused, not learnt from


class LearnTests(Project):
    def choose_hands(self, n: int) -> None:
        """n choices of a take with hands (A*) over the same shot without (B*), for the same description."""
        picks = self.picks()
        by = {t.name: t for t in self.takes}
        rows = []
        for k in range(n):
            a, b = by[f"A{k % 5}.MP4"], by[f"B{k % 5}.MP4"]
            w = lambda t: {"fid": t.fid, "name": t.name, "in_s": 1.0, "out_s": 2.0}  # noqa: E731
            rows.append({"id": f"x{k}", "show": f"axis {k % 5}", "status": "swapped", "visible": w(a), "picked": w(b), "by": "you"})
        doc = {"name": "proj", "key": KEY, "bucket": "event", "width": 1080, "height": 1920, "avoid": [], "choices": rows}
        feedback._write(doc, self.paths.kb)
        self.assertEqual(picks["name"], "proj")

    def pool_for_key(self, key):
        return self.takes if key == KEY else []

    def test_taste_waits_for_enough_choices_then_learns_them_and_a_bucket_gets_its_own_later(self):
        self.choose_hands(taste.MIN_PAIRS - 1)
        self.assertEqual(taste.candidates(feedback.all_docs(self.paths.kb), self.pool_for_key, fake_embed), {})
        self.choose_hands(taste.MIN_PAIRS)
        c = taste.candidates(feedback.all_docs(self.paths.kb), self.pool_for_key, fake_embed)
        self.assertEqual(list(c), [None])
        self.assertGreater(c[None]["weights"]["hands"], 0.05)
        self.choose_hands(taste.BUCKET_MIN)
        c = taste.candidates(feedback.all_docs(self.paths.kb), self.pool_for_key, fake_embed)
        self.assertEqual(set(c), {None, "event"})
        taste.save(c, self.paths.kb)
        self.assertEqual(taste.load(self.paths.kb, "event")["scope"], "your event cuts")
        self.assertEqual(taste.load(self.paths.kb, "wedding")["scope"], "all your cuts")  # no model of its own: the general one

    def test_learn_keeps_what_passes_the_evaluation_and_refuses_what_does_not(self):
        (self.paths.kb / "eval").mkdir()
        (self.paths.kb / "eval" / "set.json").write_text(json.dumps({"projects": [{"name": "proj", "shotmap": str(self.sm)}]}))
        said: list[dict] = []
        out = pipeline.learn(self.paths, events.Events(said.append), embed_texts=fake_embed)
        self.assertEqual((out["kept"], out["taste"]), ([], {}))  # no feedback: nothing learnt, nothing broken
        self.choose_hands(taste.MIN_PAIRS)
        worse = ([{"project": "proj", "measure": "right_take", "was": 0.5, "now": 0.2, "verdict": "worse"}], False)
        with mock.patch.object(evaluate, "compare", return_value=worse):
            out = pipeline.learn(self.paths, events.Events(said.append), embed_texts=fake_embed)
        self.assertEqual(out["kept"], [])
        self.assertEqual(out["refused"], ["taste (all your cuts)"])
        self.assertFalse((self.paths.kb / "taste.json").exists())  # the cuts got worse: the model in use stays
        out = pipeline.learn(self.paths, events.Events(said.append), embed_texts=fake_embed)
        self.assertTrue(out["evaluation"]["passed"])
        self.assertEqual(out["kept"], [str(self.paths.kb / "taste.json")])
        self.assertGreater(taste.load(self.paths.kb, "event")["weights"]["hands"], 0.05)
        self.assertTrue(any("applying your taste" in e["text"] for e in self.cut_events()))  # the next cut uses it

    def cut_events(self) -> list[dict]:
        seen: list[dict] = []
        pipeline.cut(self.sm, sheets=False, sfx=False, paths=self.paths, ev=events.Events(seen.append), embed_texts=fake_embed, fresh=True)
        return seen

    def test_a_finished_job_read_back_is_an_edit_the_keeper_learns_from(self):
        from videtor.plan import keeper

        (self.paths.kb / "manifest.json").write_text(json.dumps({"types": {}}))
        rng = np.random.default_rng(0)
        for t in self.takes:  # every take a little different, so the folds have something to tell apart
            t.a["sharp"] = t.a["sharp"] + rng.normal(0, 5, len(t.a["sharp"])).astype(np.float32)
            np.savez(self.paths.footmap / f"{t.fid}.npz", **t.a)
        kept = [t for t in self.takes if t.name.startswith("A")]  # the final job plays the takes with hands
        slots = [{"id": f"s{k}", "status": "kept", "visible": {"fid": t.fid, "in_s": 0.0, "out_s": 6.0}, "pick": {"fid": t.fid}} for k, t in enumerate(kept)]
        feedback._write({"name": "proj", "key": KEY, "bucket": "event", "source": "render:/x/final.mp4", "slots": slots, "guard": {"suspect": False}}, self.paths.kb)
        self.assertEqual([d["name"] for d in keeper.feedback_edits("event", self.paths.kb)], ["proj"])
        self.assertEqual(keeper.feedback_edits("wedding", self.paths.kb), [])
        model = keeper.train("event", fake_embed_concepts, self.paths.kb, exports=self.root / "none", footmap_dir=self.paths.footmap,
                             log=lambda s: None, model_dir=self.paths.kb / "keeper" / "candidate")
        self.assertEqual(model["edits"][0]["edit"], "feedback:proj")
        self.assertEqual(model["edits"][0]["kept_takes"], 5)
        self.assertGreater(dict(model["keeps"]).get("hands", 0), 0)  # what the finished job kept had hands in frame
        self.assertTrue((self.paths.kb / "keeper" / "candidate" / "event.json").exists())
        self.assertFalse((self.paths.kb / "keeper" / "event.json").exists())  # a candidate until it passes


def fake_embed_concepts(texts):
    """The keeper's concept phrases as fixed random directions (they carry nothing here; the scalars do)."""
    rng = np.random.default_rng(1)
    v = rng.normal(size=(len(texts), 8)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# --- the render read back ------------------------------------------------------------------------

COLOURS = [(200, 60, 50), (50, 170, 70), (60, 80, 200), (210, 190, 60), (150, 60, 170), (70, 190, 190)]


def texture(seed: int, sample: int, colour) -> np.ndarray:
    """A 480x270 landscape still: the take's colour under a pattern that is half the take's own and half this
    sample's, so two samples of a take differ as two moments of a shot do."""
    def pattern(s: int) -> np.ndarray:
        low = np.random.default_rng(s).uniform(-1, 1, (18, 32)).astype(np.float32)
        return np.asarray(Image.fromarray(((low + 1) * 127).astype(np.uint8)).resize((480, 270), Image.BICUBIC), np.float32) / 127 - 1

    pat = 0.5 * pattern(seed) + 0.5 * pattern(1000 * seed + sample + 1)
    return np.clip(np.asarray(colour, np.float32)[None, None, :] + 90 * pat[:, :, None], 0, 255).astype(np.uint8)


def colour_embed(images) -> np.ndarray:
    """A stand-in for CLIP that survives a crop: the picture's mean colour."""
    v = np.stack([np.asarray(im.convert("RGB").resize((2, 2), Image.BOX), np.float32).reshape(-1) - 127 for im in images])
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


class RenderbackUnitTests(unittest.TestCase):
    def test_a_frame_is_found_in_its_still_through_a_crop_a_grade_and_a_push_in(self):
        img = texture(1, 0, COLOURS[0])
        still = renderback._grey(img, renderback.GREY_H)
        crop = img[:, 164:316]  # the 9:16 middle of a 16:9 still
        frame = renderback._grey(crop, renderback.GREY_H)
        self.assertGreater(renderback.overlay(frame, still)[0], 0.95)
        graded = np.clip(crop.astype(np.float32) * 1.3 + 20, 0, 255).astype(np.uint8).mean(axis=2).astype(np.uint8)  # brighter, black and white
        self.assertGreater(renderback.overlay(renderback._grey(graded, renderback.GREY_H), still)[0], 0.9)
        pushed = img[34:236, 183:297]  # the same framing 25% closer
        r, size = renderback.overlay(renderback._grey(pushed, renderback.GREY_H), still)
        self.assertGreater(r, 0.85)
        self.assertGreater(size, 1.1)
        other = renderback._grey(texture(2, 0, COLOURS[1]), renderback.GREY_H)
        self.assertLess(renderback.overlay(frame, other)[0], 0.5)
        self.assertEqual(renderback.overlay(np.zeros_like(frame), still)[0], -1.0)  # a black frame matches nothing

    def test_shots_split_where_the_picks_cut_and_slivers_of_fast_action_join_their_shot(self):
        picks = {"slots": [{"id": "a", "start_s": 0.0, "length_s": 1.0, "pick": {}}, {"id": "b", "start_s": 1.0, "length_s": 1.0, "pick": {}},
                           {"id": "f", "start_s": 2.0, "length_s": 0.4, "pick": {"pieces": [{"length_s": 0.2}, {"length_s": 0.2}]}}]}
        shots = [{"a": 0, "b": 50}, {"a": 50, "b": 52}, {"a": 52, "b": 55}, {"a": 55, "b": 60}]  # one shot across a|b; f in slivers
        ranges = renderback.split_at_proposed_cuts(shots, picks, 25.0)
        self.assertEqual(ranges[:2], [(0, 25), (25, 50)])  # two takes of one setup: no cut seen, but one was proposed
        self.assertEqual(renderback.merge_slivers([(0, 20), (20, 22), (22, 25), (25, 50)], picks, 25.0), [(0, 25), (25, 50)])
        self.assertEqual(renderback.merge_slivers([(50, 52), (52, 55), (55, 60)], picks, 25.0), [(50, 52), (52, 55), (55, 60)])  # a flicker's are real

    def test_rows_of_one_shot_join_and_a_flicker_slot_is_one_choice(self):
        row = lambda t0, t1, path, i, fid: {"start_s": t0, "end_s": t1, "path": path, "in_s": i, "out_s": i + (t1 - t0), "match": 0.9, "fid": fid}  # noqa: E731
        joined = renderback._joined([row(0.0, 0.4, "/a", 2.0, "A"), row(0.4, 1.0, "/a", 2.4, "A"), row(1.0, 1.5, "/a", 9.0, "A")], 25.0)
        self.assertEqual([(r["start_s"], r["end_s"]) for r in joined], [(0.0, 1.0), (1.0, 1.5)])  # the third is another moment
        picks = {"slots": [{"id": "f", "start_s": 0.0, "length_s": 0.4, "pick": {"pieces": [
            {"fid": "A", "path": "/a", "name": "a", "in_s": 5.0, "length_s": 0.2}, {"fid": "B", "path": "/b", "name": "b", "in_s": 1.0, "length_s": 0.2}]}}]}
        rows = renderback._whole_slots([row(0.0, 0.2, "/a", 5.0, "A"), row(0.2, 0.4, "/b", 1.0, "B")], picks)
        self.assertEqual([(r["start_s"], r["end_s"], r["path"], r["in_s"]) for r in rows], [(0.0, 0.4, "/a", 5.0)])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class RenderbackTests(unittest.TestCase):
    """A render made from the stills of a synthetic footage map, edited in known ways, read back."""

    N = 8  # samples a take

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        frames = self.root / "cache" / "footmap" / "frames"
        frames.mkdir(parents=True)
        self.takes = []
        for k in range(6):
            t = fake_take(f"T{k}", k, n=self.N, fps=25.0)
            stills = [texture(10 + k, i, COLOURS[k]) for i in range(self.N)]
            for i, img in enumerate(stills):
                Image.fromarray(img).save(frames / f"{t.fid}_{i:04d}.jpg", quality=92)
            t.a["emb"] = colour_embed([Image.fromarray(s) for s in stills]).astype(np.float16)
            t.display_w, t.display_h = 3840, 2160
            self.takes.append(t)
        self.frames_dir = frames

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def window(self, k: int, i: int, length: float = 0.4) -> dict:
        t = self.takes[k]
        return {"fid": t.fid, "path": t.path, "name": t.name, "in_s": i * 0.5, "out_s": i * 0.5 + length, "speed": 1.0}

    def render(self, shots: list[tuple[int, int] | None], gain: float = 1.0) -> Path:
        """A 270x480 video at 25 fps: ten frames of each shot (take, sample) cropped to 9:16; None is a title card."""
        out = self.root / "final.mp4"
        raw = b""
        for shot in shots:
            if shot is None:
                frame = np.full((480, 270, 3), 235, np.uint8)
                frame[200:280, 40:230] = 20
            else:
                k, i = shot
                img = np.asarray(Image.open(self.frames_dir / f"{self.takes[k].fid}_{i:04d}.jpg").convert("RGB"))
                frame = np.asarray(Image.fromarray(img[:, 164:316]).resize((270, 480), Image.BICUBIC))
                frame = np.clip(frame.astype(np.float32) * gain, 0, 255).astype(np.uint8)
            raw += frame.tobytes() * 10
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "270x480", "-r", "25", "-i", "-",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", str(out)], input=raw, check=True)
        return out

    def test_kept_swapped_slipped_and_removed_are_read_from_the_finished_video(self):
        slots = [
            {"id": "s0", "start_s": 0.0, "length_s": 0.4, "show": "x", "pick": self.window(0, 2), "alternates": [self.window(4, 1)]},
            {"id": "s1", "start_s": 0.4, "length_s": 0.4, "show": "x", "pick": self.window(1, 3), "alternates": [self.window(5, 2)]},
            {"id": "s2", "start_s": 0.8, "length_s": 0.4, "show": "x", "pick": self.window(2, 1), "alternates": []},
            {"id": "s3", "start_s": 1.2, "length_s": 0.4, "show": "x", "pick": self.window(3, 2), "alternates": []},
            {"id": "s4", "start_s": 1.6, "length_s": 0.4, "show": "x", "pick": self.window(4, 5), "alternates": []},
        ]
        picks = {"name": "proj", "key": KEY, "fps": 25.0, "width": 1080, "height": 1920, "slots": slots}
        path = self.root / "picks.json"
        path.write_text(json.dumps(picks))
        # as cut; the runner-up instead; a take from nowhere in the picks; the same take five samples later; a title card
        video = self.render([(0, 2), (5, 2), (5, 6), (3, 7), None], gain=1.25)
        result = renderback.read(path, video, self.takes, self.frames_dir, self.root / "kb", colour_embed, log=lambda s: None)
        got = {s["id"]: s["status"] for s in result["slots"]}
        self.assertEqual(got, {"s0": "kept", "s1": "swapped_to_alternate", "s2": "swapped", "s3": "slipped", "s4": "removed"})
        self.assertEqual(result["slots"][2]["visible"]["name"], "T5.MP4")
        self.assertAlmostEqual(result["slots"][3]["delta_in_s"], 2.5, delta=0.5)
        self.assertEqual(len(result["render"]["unmatched"]), 1)  # the title card is nobody's footage
        self.assertEqual(result["source"], f"render:{video}")
        self.assertFalse(result["guard"]["suspect"])


if __name__ == "__main__":
    unittest.main()
