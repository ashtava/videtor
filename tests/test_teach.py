"""Teaching Videtor: rules, edits taken in, a job cut by hand learnt from its finished video and its footage, the
state the app's Teach page shows, and a finished job joining the evaluation set. Synthetic footage throughout."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from test_feedback import HAVE_FFMPEG, RenderbackTests, colour_embed
from test_fill import fake_embed, fake_take
from test_pipeline import KEY, the_map, write_project
from videtor import events, pipeline, teach
from videtor.kb import feedback
from videtor.plan import evaluate, keeper


class RulesAndStateTests(unittest.TestCase):
    def setUp(self):
        self.kb = Path(tempfile.mkdtemp())
        (self.kb / "notes.md").write_text("# Editing notes\n\nExamples (delete these and write your own):\n\n- [event] An example, not a rule.\n\n"
                                         "## Learned from an edit\n\n- [event] People first.\n- [sports-promo] Cut on the hits.\n")

    def tearDown(self):
        shutil.rmtree(self.kb, ignore_errors=True)

    def test_rules_are_read_added_and_removed_per_kind_of_work(self):
        self.assertEqual(teach.rules("event", self.kb), ["People first."])  # the template's example is not the user's rule
        self.assertEqual(teach.add_rule("event", "  Never open   on a wide shot. ", self.kb), ["People first.", "Never open on a wide shot."])
        self.assertEqual(teach.add_rule("event", "Never open on a wide shot.", self.kb), ["People first.", "Never open on a wide shot."])  # once
        self.assertIn("## Your rules (added in the app)\n- [event] Never open on a wide shot.", (self.kb / "notes.md").read_text())
        self.assertEqual(teach.remove_rule("event", "People first.", self.kb), ["Never open on a wide shot."])
        self.assertEqual(teach.rules("sports-promo", self.kb), ["Cut on the hits."])
        with self.assertRaises(ValueError):
            teach.add_rule("event", "   ", self.kb)

    def test_the_state_says_what_was_given_and_what_is_known_per_kind_of_work(self):
        (self.kb / "manifest.json").write_text(json.dumps({"types": {"A.xml::Seq": "event"}, "footage": {"A.xml::Seq": ["/Volumes/Gone/shoot"]}}))
        feedback._write({"name": "p", "key": "p::footage", "bucket": "event", "source": "render:/x.mp4", "slots": [{"id": "s", "status": "kept"}],
                         "verdict": {"good": True}}, self.kb)
        st = teach.state(self.kb, exports=self.kb / "none", refs_dir=self.kb / "none", footmap_dir=self.kb / "none")
        ev = next(b for b in st["buckets"] if b["bucket"] == "event")
        self.assertEqual((ev["finished"], ev["verdicts"], ev["rules"], ev["known"]), (1, {"good": 1, "not": 0}, ["People first."], True))
        self.assertEqual((ev["edits"][0]["drive"], ev["edits"][0]["connected"], ev["edits"][0]["learnt"]), ("Gone", False, False))
        self.assertEqual(st["buckets"][0]["bucket"], "event")  # kinds with something learnt come first, the starter list after
        self.assertFalse(next(b for b in st["buckets"] if b["bucket"] == "wedding")["known"])
        self.assertIsNone(st["last"])
        teach.log_learning({"kept": ["kb/keeper/event.json"], "refused": [], "evaluation": {"passed": True, "rows": []}}, self.kb)
        teach.log_learning({"kept": [], "refused": ["keeper (event)"], "evaluation": {"passed": False, "rows": [
            {"project": "p", "measure": "right_take", "was": 0.5, "now": 0.4, "verdict": "worse"}]}}, self.kb)
        last = teach.last_learning(self.kb)
        self.assertEqual((last["kept"], last["refused"], last["worse"]), ([], ["keeper (event)"], ["p: right_take 0.5 -> 0.4"]))  # the newest first


class EditTests(unittest.TestCase):
    """An XML export of a job cut by hand, taken in as one of the user's edits."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k, hands=0) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
        self.paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse", beats=self.root / "cache" / "beats")
        sm = write_project(self.root, self.takes, the_map())
        pipeline.cut(sm, sheets=False, sfx=False, paths=self.paths, embed_texts=fake_embed)  # its XML stands in for the user's export
        self.xml = sm.parent / "proj.xml"
        self.exports = self.root / "exports"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_an_xml_export_becomes_one_of_the_users_edits_with_its_kind_and_footage(self):
        key = teach.add_edit(self.xml, "event", "/Volumes/T/", self.paths.kb, self.exports)
        self.assertEqual(key, "proj.xml::proj")
        self.assertTrue((self.exports / "proj.xml").exists())
        m = teach.manifest(self.paths.kb)
        self.assertEqual((m["types"][key], m["footage"][key]), ("event", ["/Volumes/T"]))
        self.assertEqual(keeper.edits_of("event", self.paths.kb, self.exports), [(key, self.exports / "proj.xml", ["/Volumes/T"])])
        self.assertEqual(pipeline.ingest([self.exports / "proj.xml"], self.paths.kb), 1)
        self.assertTrue((self.paths.kb / "profile" / "event.json").exists())  # the pace of this kind of work
        with self.assertRaises(ValueError):
            (self.root / "empty.xml").write_text("<xmeml version='4'></xmeml>")
            teach.add_edit(self.root / "empty.xml", "event", None, self.paths.kb, self.exports)

    def test_an_edit_without_folders_in_the_manifest_is_found_through_its_index(self):
        teach.add_edit(self.xml, "event", None, self.paths.kb, self.exports)
        index = self.paths.kb / "footage" / "proj.json"
        d = json.loads(index.read_text())
        d["roots"]["proj.xml::proj"] = ["/Volumes/T"]
        index.write_text(json.dumps(d))
        self.assertEqual(keeper.edits_of("event", self.paths.kb, self.exports)[0][2], ["/Volumes/T"])

    def test_learning_from_an_edit_already_given_reads_what_it_used_and_a_sample_of_the_rest(self):
        key = teach.add_edit(self.xml, "event", "/Volumes/T", self.paths.kb, self.exports)
        said: list[dict] = []
        with mock.patch.object(pipeline, "learn", return_value={"kept": []}) as learn:
            out = pipeline.teach_edit(key, sample=3, paths=self.paths, ev=events.Events(said.append), exports=self.exports)
        self.assertEqual(out["bucket"], "event")
        self.assertEqual(out["takes"]["pool"], 14)
        self.assertGreaterEqual(out["takes"]["used"], 5)  # the takes the export plays; the footage was read before, so no drive is needed
        self.assertEqual(learn.call_args.kwargs["buckets"], ["event"])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.teach_edit("nope.xml::x", paths=self.paths, exports=self.exports)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class ByHandTests(RenderbackTests):
    """A job cut by hand: only its finished video and its footage folder are given."""

    def test_a_finished_video_and_its_footage_teach_what_the_job_kept(self):
        write_project(self.root, self.takes, the_map())  # the footage's index and map, as prep and a full read leave them
        (self.root / "footage").mkdir()
        video = self.render([(0, 2), (1, 3), (5, 6)])
        paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse", beats=self.root / "cache" / "beats")
        said: list[dict] = []
        with mock.patch.object(pipeline, "prepare"), mock.patch("videtor.analyze.footmap.build_pool"), mock.patch.object(pipeline, "teach_style", return_value={}), \
                mock.patch("videtor.kb.breakdown.card", return_value=self.root / "card.md"):
            out = pipeline.teach_project("proj", "event", footage=self.root / "footage", video=video, paths=paths, ev=events.Events(said.append),
                                         embed_images=colour_embed, embed_texts=concepts_12, exports=self.root / "exports")
        self.assertEqual(out["matched"], {"shots": 3, "found": 3})
        doc = feedback.load("proj", paths.kb)
        self.assertTrue(doc["by_hand"])
        self.assertTrue(feedback.is_final(doc))  # no picks behind it, yet a finished job the keeper learns from
        used = feedback.used_ranges(doc)
        self.assertEqual(set(used), {"T0", "T1", "T5"})
        self.assertAlmostEqual(used["T5"][0][0], 3.0, delta=0.5)  # sample 6 of the take: about three seconds in
        self.assertEqual([d["name"] for d in keeper.feedback_edits("event", paths.kb)], ["proj"])
        self.assertEqual(out["learning"]["keeper"]["event"]["edits"][0]["edit"], "feedback:proj")
        self.assertTrue((paths.kb / "keeper" / "event.json").exists())  # no evaluation set here: kept unchecked, and said so
        self.assertTrue(any(e["event"] == "warning" and "no evaluation set" in e["text"] for e in said))
        self.assertEqual(teach.last_learning(paths.kb)["note"], "after proj")

    def test_a_job_needs_its_finished_video_or_its_xml(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.teach_project("x", "event", footage=self.root)
        self.assertIn("finished video or the Premiere XML", str(ctx.exception))
        with self.assertRaises(pipeline.PipelineError):
            pipeline.teach_project("x", "event", video=self.root / "missing.mp4")


def concepts_12(texts):
    """The keeper's concept phrases as fixed directions in the 12 dimensions `colour_embed` gives pictures."""
    v = np.random.default_rng(1).normal(size=(len(texts), 12)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


class LoopTests(unittest.TestCase):
    def test_a_finished_job_read_back_joins_the_evaluation_set_with_what_the_user_did_as_the_truth(self):
        kb = Path(tempfile.mkdtemp())
        try:
            self.assertTrue(evaluate.add_project("p", Path("out/p/shotmap.json"), kb, feedback="p"))
            self.assertFalse(evaluate.add_project("p", Path("out/p/shotmap.json"), kb, feedback="p"))  # once
            self.assertEqual(evaluate.load_set(kb), [{"name": "p", "shotmap": "out/p/shotmap.json", "feedback": "p"}])
        finally:
            shutil.rmtree(kb, ignore_errors=True)

    def test_a_finished_video_waits_until_it_is_read_and_again_when_it_is_replaced(self):
        root = Path(tempfile.mkdtemp())
        try:
            d = root / "out" / "p"
            d.mkdir(parents=True)
            self.assertIsNone(pipeline.unread_final(d, root / "kb"))  # no cut: nothing to read a video against
            (d / "picks.json").write_text(json.dumps({"name": "p", "slots": []}))
            self.assertIsNone(pipeline.unread_final(d, root / "kb"))
            (d / "preview.mp4").write_bytes(b"x")
            self.assertIsNone(pipeline.unread_final(d, root / "kb"))  # Videtor's own preview is not the finished job
            final = d / "final.mp4"
            final.write_bytes(b"x")
            self.assertEqual(pipeline.unread_final(d, root / "kb"), final)
            feedback._write({"name": "p", "source": f"render:{final}", "slots": [{"id": "s"}], "read_at": "2999-01-01T00:00:00"}, root / "kb")
            self.assertIsNone(pipeline.unread_final(d, root / "kb"))  # read
            feedback._write({"name": "p", "source": f"render:{final}", "slots": [{"id": "s"}], "read_at": "2000-01-01T00:00:00"}, root / "kb")
            self.assertEqual(pipeline.unread_final(d, root / "kb"), final)  # exported again since
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
