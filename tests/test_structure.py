import json
import tempfile
import unittest
from pathlib import Path

from videtor.plan import structure

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.xml"
FPS = 24000 / 1001

# the tagger's vocabulary, as written to cache/analysis by analyze/tags.py
PROMPTS = [
    "hands in close-up", "makeup or hair being done", "clothing and apparel detail", "footwear or sneakers", "face in close-up",
    "someone looking at the camera", "someone partly hidden or turned away", "someone in a full-body shot",
    "buildings or a location in wide shot", "products on a table", "drinks in bottles, cans or glasses", "phones or gadgets",
    "unrecognisable motion blur", "spectators or a crowd", "people in a group", "near-black darkness", "text or screen graphics",
]


def record(fid, scale, faces_max=0, face_area=0.0, partial=0.0, hands=0.0, sharpness=90.0, tags=None):
    """A synthetic analysis record in the shape faces.py and tags.py write; no models involved."""
    frames = 6
    scores = {t: 0.02 for t in PROMPTS}
    scores.update(tags or {})
    top = dict(sorted(scores.items(), key=lambda kv: -kv[1])[:5])
    return {
        "fid": fid,
        "frames": {"sharpness": sharpness, "frames": frames},
        "faces": {"frames": frames, "faces_max": faces_max, "face_area": face_area, "partial_share": partial, "hands_share": hands, "scale": scale, "face_frames": 1.0 if faces_max else 0.0},
        "tags": {"frames": frames, "tags": top, "scores": scores, "embedding": [0.0] * 8},
    }


HANDS = record("hands", "detail", hands=0.8, tags={"hands in close-up": 0.7, "makeup or hair being done": 0.15})
APPAREL = record("apparel", "close", hands=0.1, tags={"clothing and apparel detail": 0.6, "footwear or sneakers": 0.2})
PARTIAL = record("partial", "close", faces_max=1, face_area=0.12, partial=0.7, sharpness=15.0, tags={"someone partly hidden or turned away": 0.5, "unrecognisable motion blur": 0.3})
PORTRAIT = record("portrait", "medium", faces_max=1, face_area=0.06, partial=0.05, sharpness=95.0, tags={"someone looking at the camera": 0.6, "someone in a full-body shot": 0.2})
CROWD = record("crowd", "wide", faces_max=6, face_area=0.005, partial=0.3, tags={"buildings or a location in wide shot": 0.4, "people in a group": 0.4})
PRODUCT = record("product", "detail", tags={"products on a table": 0.7, "drinks in bottles, cans or glasses": 0.2})
ALL = [HANDS, APPAREL, PARTIAL, PORTRAIT, CROWD, PRODUCT]
# modelled on the real Guru opener: a sharp face close-up looking at the camera, in near darkness
DARK = record("dark", "close", faces_max=1, face_area=0.1, sharpness=250.0, tags={"someone looking at the camera": 0.5, "face in close-up": 0.4, "near-black darkness": 0.3})


def candidate(rec, duration=8.0):
    fid = rec["fid"]
    entry = {
        "fid": fid, "path": f"/v/Shoot/{fid}.mp4", "basename": f"{fid}.mp4", "kind": "video",
        "media": {"duration_s": duration, "video": {"fps": 25.0, "width": 1080, "height": 1920}}, "used_by": {}, "duplicate_of": None,
    }
    return {"fid": fid, "entry": entry, "analysis": rec}


class ScoreTests(unittest.TestCase):
    def test_tease_rewards_details_and_orders_them_before_the_partial_face(self):
        scores = {r["fid"]: structure.tease_score(r)[0] for r in ALL}
        for fid in ("hands", "apparel", "partial"):
            self.assertGreaterEqual(scores[fid], structure.TEASE_MIN, fid)
        self.assertLess(scores["portrait"], structure.TEASE_MIN)  # a sharp, whole face is the reveal, not a tease
        self.assertLess(scores["product"], structure.TEASE_MIN)  # a product is not the subject
        self.assertLess(structure.exposure(HANDS), structure.exposure(PARTIAL))
        self.assertLess(structure.exposure(APPAREL), structure.exposure(PARTIAL))
        _, reasons = structure.tease_score(PARTIAL)
        self.assertTrue(any("half-shown" in r for r in reasons))
        self.assertTrue(any("blur" in r for r in reasons))
        self.assertTrue(any("hands" in r for r in structure.tease_score(HANDS)[1]))
        self.assertTrue(any("reveal, not a tease" in r for r in structure.tease_score(PORTRAIT)[1]))

    def test_a_face_glimpsed_in_the_dark_teases_more_and_reveals_less_than_a_clean_portrait(self):
        tease, reasons = structure.tease_score(DARK)
        self.assertGreater(tease, structure.tease_score(PORTRAIT)[0])
        self.assertTrue(any("glimpsed" in r for r in reasons))
        self.assertFalse(any("reveal, not a tease" in r for r in reasons))
        reveal, reasons = structure.reveal_score(DARK, None, None)
        self.assertLess(reveal, structure.reveal_score(PORTRAIT, None, None)[0])
        self.assertGreater(reveal, tease)  # it still shows the face more than it hides it
        self.assertTrue(any("looking at the camera" in r for r in reasons))

    def test_reveal_picks_the_portrait_and_likes_the_main_subjects_group(self):
        scores = {r["fid"]: structure.reveal_score(r, None, None)[0] for r in ALL}
        self.assertEqual(max(scores, key=scores.get), "portrait")
        self.assertGreaterEqual(scores["portrait"], structure.REVEAL_MIN)
        self.assertEqual(scores["hands"], 0.0)
        self.assertTrue(any("no face" in r for r in structure.reveal_score(HANDS, None, None)[1]))
        same, _ = structure.reveal_score(PORTRAIT, 0, 0)
        other, why = structure.reveal_score(PORTRAIT, 0, 1)
        self.assertGreater(same, other)
        self.assertTrue(any("not the main subject" in r for r in why))

    def test_wide_crowd_is_never_a_tease(self):
        score, reasons = structure.tease_score(CROWD)
        self.assertEqual(score, 0.0)
        self.assertTrue(any("wide" in r for r in reasons))
        self.assertTrue(any("crowd" in r for r in reasons))
        # with only one real tease in the pool the crowd still does not fill the second slot
        opening = structure.build_opening([candidate(HANDS), candidate(PORTRAIT), candidate(CROWD)])
        self.assertEqual([(s["fid"], s["role"]) for s in opening], [("hands", "tease"), ("portrait", "reveal")])

    def test_tag_strengths_are_relative_to_the_records_strongest_tag(self):
        strengths = structure.tag_strengths({"tags": {"scores": {"a": 0.21, "b": 0.27, "c": 0.24}}})  # raw similarities
        self.assertEqual(strengths["b"], 1.0)
        self.assertEqual(strengths["a"], 0.0)
        self.assertAlmostEqual(strengths["c"], 0.5)
        self.assertEqual(structure.tag_strengths({}), {})
        self.assertEqual(structure.top_tags(HANDS)[0], "hands in close-up")
        # an unsure tagger (top probability 0.087, as on a real detail shot) has its strengths shrunk, so no tag is cited
        unsure = structure.tag_strengths({"tags": {"scores": {"a": 0.087, "b": 0.038, "c": 0.01}}})
        self.assertAlmostEqual(unsure["a"], 0.087 / structure.TAG_CONFIDENT, places=3)
        self.assertLess(unsure["b"], structure.TAG_MIN)
        self.assertEqual(structure.tag_strengths({"tags": ["hands in close-up"]}), {})  # a report row's list of names is not a tags block


class BuildTests(unittest.TestCase):
    def test_three_teases_then_the_reveal_with_roles_and_reasons(self):
        opening = structure.build_opening([candidate(r) for r in ALL])
        self.assertEqual([s["role"] for s in opening], ["tease", "tease", "tease", "reveal"])
        self.assertEqual([s["fid"] for s in opening], ["hands", "apparel", "partial", "portrait"])
        self.assertEqual(len({s["fid"] for s in opening}), 4)
        for s in opening:
            self.assertIsInstance(s["score"], float)
            self.assertTrue(s["reasons"])
            self.assertIn("entry", s)
        self.assertIn("most abstract", opening[0]["reasons"][0])
        self.assertIn("half-shown", opening[2]["reasons"][0])
        self.assertIn("the reveal", opening[-1]["reasons"][0])
        exposures = [s["exposure"] for s in opening[:3]]
        self.assertEqual(exposures, sorted(exposures))

    def test_degrades_to_two_teases_when_only_three_clips_suit(self):
        opening = structure.build_opening([candidate(r) for r in (CROWD, PORTRAIT, PRODUCT, PARTIAL, HANDS)])
        self.assertEqual([(s["fid"], s["role"]) for s in opening], [("hands", "tease"), ("partial", "tease"), ("portrait", "reveal")])

    def test_groups_prefer_the_main_subject_and_a_clip_is_used_once(self):
        other = dict(PORTRAIT, fid="portrait2")
        groups = {"portrait": 1, "portrait2": 0, "partial": 0, "hands": 0}
        opening = structure.build_opening([candidate(r) for r in (HANDS, APPAREL, PARTIAL, PORTRAIT, other)], groups)
        self.assertEqual(opening[-1]["fid"], "portrait2")
        self.assertTrue(any("main subject" in r for r in opening[-1]["reasons"]))
        self.assertEqual(len({s["fid"] for s in opening}), len(opening))
        # a copy of a chosen clip never joins it
        copy = candidate(dict(HANDS, fid="hands-copy"))
        copy["entry"]["duplicate_of"] = "hands"
        opening = structure.build_opening([candidate(HANDS), copy, candidate(PORTRAIT)])
        self.assertEqual([s["fid"] for s in opening], ["hands", "portrait"])

    def test_no_reveal_when_nothing_shows_a_face(self):
        opening = structure.build_opening([candidate(r) for r in (HANDS, APPAREL, PRODUCT)])
        self.assertEqual([(s["fid"], s["role"]) for s in opening], [("hands", "tease"), ("apparel", "tease")])


class GlimpseTests(unittest.TestCase):
    def test_glimpse_prefers_a_face_close_up_over_details_portraits_and_crowds(self):
        scores = {r["fid"]: structure.glimpse_score(r)[0] for r in [*ALL, DARK]}
        self.assertGreaterEqual(scores["dark"], structure.GLIMPSE_MIN)  # the Guru-style opener
        self.assertGreaterEqual(scores["partial"], structure.GLIMPSE_MIN)  # a face cut by the frame, in blur
        for fid in ("hands", "apparel", "product", "crowd"):
            self.assertLess(scores[fid], structure.GLIMPSE_MIN, fid)  # no face: nothing of the subject shown
        self.assertGreater(scores["dark"], scores["portrait"])  # a medium portrait is the zoom-out, not the glimpse

    def test_build_opening_with_a_glimpse_then_details_then_the_zoom_out(self):
        opening = structure.build_opening([candidate(r) for r in [*ALL, DARK]], tease_count=2, glimpse=True)
        self.assertEqual([c["role"] for c in opening], ["glimpse", "tease", "tease", "reveal"])
        self.assertEqual(opening[-1]["fid"], "portrait")
        self.assertIn(opening[0]["fid"], ("dark", "partial"))
        self.assertEqual(len({c["fid"] for c in opening}), 4)
        self.assertTrue(opening[0]["reasons"][0].startswith("glimpse"))

    def test_no_glimpse_when_no_clip_shows_part_of_a_face(self):
        opening = structure.build_opening([candidate(r) for r in (HANDS, APPAREL, PRODUCT, PORTRAIT)], tease_count=2, glimpse=True)
        self.assertNotIn("glimpse", [c["role"] for c in opening])

    def test_glimpse_template_scores_the_real_guru_shape_above_detail_first(self):
        guru_like = [{"analysis": DARK}, {"analysis": HANDS}, {"analysis": APPAREL}, {"analysis": PORTRAIT}]
        detail_first = [{"analysis": HANDS}, {"analysis": APPAREL}, {"analysis": DARK}, {"analysis": PORTRAIT}]
        glimpse = structure.opening_score(guru_like, structure.GLIMPSE_TEMPLATE)
        self.assertGreater(glimpse["score"], structure.opening_score(detail_first, structure.GLIMPSE_TEMPLATE)["score"])
        self.assertEqual(glimpse["early_reveal"], 0.0)  # the glimpse shows a face by design: no early-reveal penalty
        self.assertEqual(glimpse["template"], list(structure.GLIMPSE_TEMPLATE))


class OpeningScoreTests(unittest.TestCase):
    def test_tease_tease_reveal_beats_reveal_first(self):
        good = structure.opening_score([candidate(HANDS), candidate(PARTIAL), candidate(PORTRAIT)])
        bad = structure.opening_score([candidate(PORTRAIT), candidate(HANDS), candidate(PARTIAL)])
        self.assertGreater(good["score"], bad["score"])
        self.assertGreater(good["score"], 0.6)
        self.assertEqual(good["early_reveal"], 0.0)
        self.assertGreater(bad["early_reveal"], 0.5)
        self.assertEqual([s["expected"] for s in good["shots"]], ["tease", "tease", "reveal"])

    def test_roles_alone_are_enough_and_the_template_can_follow_the_plan(self):
        self.assertEqual(structure.opening_score([{"role": "tease"}, {"role": "tease"}, {"role": "reveal"}])["score"], 1.0)
        self.assertLess(structure.opening_score([{"role": "reveal"}, {"role": "tease"}, {"role": "tease"}])["score"], 0.3)
        opening = structure.build_opening([candidate(r) for r in ALL])
        self.assertGreater(structure.opening_score(opening, template=None)["score"], 0.6)
        short = structure.opening_score(opening[:2])  # a missing third shot counts as nothing at the turn
        self.assertEqual(len(short["shots"]), 3)
        self.assertEqual(short["reveal"], 0.0)


class RealOpeningTests(unittest.TestCase):
    INDEX = {
        "files": [
            {"fid": "a1", "path": "/Volumes/RAW/Day 1/A001.mov", "duplicate_of": None},
            {"fid": "b1", "path": "/volumes/raw/B001.MOV", "duplicate_of": "b0"},  # the drive and the export disagree on case
            {"fid": "n1", "path": "/Volumes/RAW/N001.mov", "duplicate_of": None},
        ]
    }

    def test_first_shots_in_timeline_order_with_fids_by_path(self):
        shots = structure.real_openings(FIXTURE, self.INDEX, n=5)
        self.assertEqual([s["name"] for s in shots], ["A001.mov", "A002.mov", "B001.mov", "N001.mov", "N002.mov"])
        self.assertEqual([s["fid"] for s in shots], ["a1", None, "b1", "n1", None])
        self.assertEqual(shots[2]["canonical_fid"], "b0")
        self.assertEqual(shots[1]["path"], "/Volumes/RAW/Day 1/A002.mov")
        self.assertAlmostEqual(shots[1]["start_s"], 96 / FPS, places=3)
        self.assertAlmostEqual(shots[1]["length_s"], 144 / FPS, places=3)
        self.assertEqual([s["track"] for s in shots], ["V1", "V1", "V2", "V2", "V2"])
        self.assertEqual(len(structure.real_openings(FIXTURE, self.INDEX, n=2)), 2)
        self.assertEqual(structure.real_openings(FIXTURE, self.INDEX, sequence="No such sequence"), [])

    def test_nested_stays_one_shot_when_not_opened_and_same_start_counts_once(self):
        shots = structure.real_openings(FIXTURE, self.INDEX, nested_ok=False)
        self.assertEqual([s["name"] for s in shots], ["A001.mov", "A002.mov", "B001.mov", "Nested Sequence 1"])
        self.assertIsNone(shots[3]["path"])
        self.assertIsNone(shots[3]["fid"])
        with tempfile.TemporaryDirectory() as tmp:
            # B001 moved onto V2 at the same frame as A002 on V1: the lower track's shot is the one that counts
            xml = Path(tmp, "stacked.xml")
            xml.write_text(FIXTURE.read_text().replace("<start>120</start><end>168</end>", "<start>96</start><end>168</end>"))
            names = [s["name"] for s in structure.real_openings(xml, self.INDEX)]
        self.assertEqual(names, ["A001.mov", "A002.mov", "N001.mov", "N002.mov"])

    def test_report_joins_each_shot_with_its_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            analysis = Path(tmp)
            (analysis / "a1.json").write_text(json.dumps(HANDS))
            (analysis / "b0.json").write_text(json.dumps(PORTRAIT))  # a copy's analysis lives under the original
            report = structure.opening_report(structure.real_openings(FIXTURE, self.INDEX, n=3), analysis)
        self.assertEqual([r["analysed"] for r in report], [True, False, True])
        self.assertEqual(report[0]["scale"], "detail")
        self.assertEqual(report[0]["tags"][0], "hands in close-up")
        self.assertGreater(report[0]["tease"], report[0]["reveal"])
        self.assertGreater(report[2]["reveal"], report[2]["tease"])
        self.assertEqual(report[2]["sharpness"], 95.0)
        self.assertIsNone(report[1]["tease"])
        self.assertEqual(report[1]["tags"], [])


if __name__ == "__main__":
    unittest.main()
