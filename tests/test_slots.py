"""What a slot of the picks is (plan/slots.py): its kind, what the timeline may do to it, the clips it plays on V1 and
whether its own sound is heard; and what kind of cut a picks.json is (pipeline.cut_kind) and how each is rebuilt."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from videtor import pipeline
from videtor.plan import build, edit, slots


def take(name: str, in_s: float = 1.0, out_s: float = 2.0, **kw) -> dict:
    return {"fid": name, "name": name, "path": f"/x/{name}.MP4", "in_s": in_s, "out_s": out_s, "duration_s": 10.0, **kw}


SLOTS = {
    "shot": {"id": "a", "length_s": 1.0, "pick": take("A"), "alternates": [take("B")]},
    "ramp": {"id": "b", "length_s": 1.0, "pick": take("A", ramp=[[0.4, 1.0], [0.6, 0.5]]), "alternates": []},
    "flicker": {"id": "c", "length_s": 0.4, "pick": {**take("A"), "pieces": [{**take("A"), "length_s": 0.2}, {**take("B"), "length_s": 0.2}]}},
    "stack": {"id": "d", "length_s": 1.0, "pick": take("A"), "layers": [{**take("C"), "offset_s": 0.2, "length_s": 0.8}]},
    "byte": {"id": "e", "length_s": 2.0, "pick": take("A"), "byte": True},
    "black": {"id": "f", "length_s": 1.0, "pick": None, "black": True},
    "empty": {"id": "g", "length_s": 1.0, "pick": None},
}


class SlotTests(unittest.TestCase):
    def test_every_kind_of_slot_is_told_from_its_flags_in_one_place(self):
        self.assertEqual({k: slots.kind(s) for k, s in SLOTS.items()}, {k: k for k in SLOTS})
        self.assertEqual(set(slots.KINDS), set(SLOTS))
        self.assertEqual([k for k, s in SLOTS.items() if slots.plain(s)], ["shot"])
        self.assertIs(edit.kind, slots.kind)  # the old names still answer
        self.assertIs(edit.plain, slots.plain)

    def test_what_the_timeline_may_do_to_each_kind(self):
        shot, ramp, flicker = SLOTS["shot"], SLOTS["ramp"], SLOTS["flicker"]
        self.assertEqual(slots.can(shot, shot), {"slip": True, "roll": True, "remove": True, "swap": True, "replace": True})
        self.assertEqual(slots.can(shot)["roll"], False)  # the last shot has no cut after it
        self.assertEqual(slots.can(shot, flicker)["roll"], False)  # nor one into a flicker
        self.assertEqual(slots.can(ramp), {"slip": False, "roll": False, "remove": True, "swap": False, "replace": True})
        self.assertEqual(slots.can(SLOTS["byte"])["replace"], False)
        self.assertEqual(slots.can(SLOTS["black"]), {"slip": False, "roll": False, "remove": False, "swap": False, "replace": False})

    def test_the_clips_each_kind_plays_on_v1(self):
        self.assertIs(build._v1_clips, slots.clips)
        (c, length), = slots.clips(SLOTS["shot"], 25)
        self.assertEqual((c["name"], length), ("A", 1.0))
        parts = slots.clips(SLOTS["ramp"], 25)
        self.assertEqual([(p["in_s"], p["out_s"], p["speed"], t) for p, t in parts], [(1.0, 1.4, 1.0, 0.4), (1.4, 1.7, 0.5, 0.6)])  # chained in-points
        self.assertEqual([(p["name"], t) for p, t in slots.clips(SLOTS["flicker"], 25)], [("A", 0.2), ("B", 0.2)])
        self.assertEqual(len(slots.clips(SLOTS["stack"], 25)), 1)  # the layers are on the tracks above, not V1
        self.assertEqual((slots.clips(SLOTS["black"], 25), slots.clips(SLOTS["empty"], 25)), ([], []))

    def test_what_a_slot_shows_and_whether_its_sound_is_heard(self):
        self.assertEqual([p["name"] for p in slots.pieces(SLOTS["flicker"])], ["A", "B"])
        self.assertEqual(slots.pieces(SLOTS["shot"]), [])
        self.assertEqual([p["name"] for p in slots.shown(SLOTS["flicker"])], ["A", "B"])
        self.assertEqual([p["name"] for p in slots.shown(SLOTS["shot"])], ["A"])
        self.assertEqual((slots.shown(SLOTS["black"]), slots.shown({})), ([], []))
        self.assertEqual([k for k, s in SLOTS.items() if slots.own_sound(s)], ["byte"])


class CutKindTests(unittest.TestCase):
    def test_a_cut_is_a_plain_cut_an_interview_a_music_video_or_a_promo(self):
        self.assertEqual(pipeline.cut_kind({"slots": []}), "cut")
        self.assertEqual(pipeline.cut_kind({"source": {"aroll": "/x/aroll.json", "shotmap": "/x/shotmap.json"}}), "interview")
        self.assertEqual(pipeline.cut_kind({"kind": "music-video", "source": {"spec": "/x/mv.json"}}), "music-video")
        self.assertEqual(pipeline.cut_kind({"kind": "promo"}), "promo")
        self.assertEqual(set(pipeline.CUT_KINDS), {"cut", "interview", "music-video", "promo"})
        self.assertFalse(pipeline.CUT_KINDS["music-video"].borrow)

    def test_what_cannot_be_rebuilt_is_refused_in_words_before_anything_is_written(self):
        root = Path(tempfile.mkdtemp())
        try:
            base = {"version": 1, "name": "p", "fps": 25, "width": 1080, "height": 1920, "slots": [{**SLOTS["shot"], "start_s": 0.0}]}
            for picks, words in (({**base, "kind": "promo"}, "Promos page"), ({**base, "kind": "music-video"}, "out of sync")):
                (root / "picks.json").write_text(json.dumps(picks))
                with self.assertRaises(pipeline.PipelineError) as ctx:
                    pipeline.look(root / "picks.json", {"a": ("b", 1)})
                self.assertIn(words, str(ctx.exception))
                self.assertEqual(json.loads((root / "picks.json").read_text()), picks)
                self.assertFalse((root / "p.xml").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
