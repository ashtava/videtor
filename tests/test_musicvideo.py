"""The music-video planner: cuts on the beat grid, lip-synced in-points, singers per section, beat-aligned cutaways."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from videtor.export import xmeml_writer as w
from videtor.plan import musicvideo as mv
from videtor.plan.build import build_from_picks
from tests.test_fill import fake_take

BEATS = [round(0.5 * i, 3) for i in range(80)]  # 120 BPM, 40 s
SONG = {"beats_s": BEATS, "duration_s": 40.0}


def stretch(c, t0, t1):
    return {"rate": 1, "c": c, "t0": t0, "t1": t1, "r": 0.8, "song": [t0 + c, t1 + c], "speed": 1.0}


class CutPointTests(unittest.TestCase):
    SECTIONS = [{"id": "intro", "until": 8.0, "beats": 4}, {"id": "verse", "until": 40.0, "beats": 2, "stutter": True, "open": 4, "singer": "A"}]

    def test_sections_cut_on_their_grid_with_an_opening_hold_and_stutters_at_phrase_ends(self):
        pts = mv.cut_points(BEATS, self.SECTIONS, 40.0, beats_per_bar=4)
        intro = [p["beat"] for p in pts if p["section"] == "intro"]
        verse = [(p["beat"], p["beats"]) for p in pts if p["section"] == "verse"]
        self.assertEqual(intro, [0, 4, 8, 12])
        self.assertEqual(verse[0], (16, 4))  # the hold that introduces the singer
        self.assertEqual(verse[1:9], [(20, 2), (22, 2), (24, 2), (26, 2), (28, 1), (29, 1), (30, 1), (31, 1)])  # last half bar of the phrase
        self.assertEqual(verse[9], (32, 2))

    def test_slots_are_contiguous_frames_led_ahead_of_their_beats(self):
        slots = mv.frame_slots(mv.cut_points(BEATS, self.SECTIONS, 40.0, 4), 25.0, 0.12, 40.0)
        self.assertEqual(slots[0]["start_f"], 0)
        self.assertEqual(slots[1]["start_f"], round((2.0 - 0.12) * 25))
        self.assertTrue(all(a["end_f"] == b["start_f"] for a, b in zip(slots, slots[1:])))
        self.assertEqual(slots[-1]["end_f"], 1000)


class PlanTests(unittest.TestCase):
    def setUp(self):
        # every take rolls a few seconds before the song starts and after it ends
        self.takes = [fake_take(n, axis=i, n=100, faces=1) for i, n in enumerate(("A1", "A2", "B1", "B2"))]
        self.sync = {"A1": {"stretches": [stretch(-3.0, 3.0, 43.0)]}, "A2": {"stretches": [stretch(-5.0, 5.0, 45.0)]},
                     "B1": {"stretches": [stretch(-2.0, 2.0, 42.0)]}, "B2": {"stretches": [stretch(-4.0, 4.0, 44.0)]}}
        self.spec = {
            "name": "mv-test", "music": "/Volumes/T/song.wav", "fps": 25, "width": 3840, "height": 2160, "beat_lead_s": 0.12, "beats_per_bar": 4,
            "takes": {"A1": {"who": "A", "look": "a1"}, "A2": {"who": "A", "look": "a2"}, "B1": {"who": "B", "look": "b1"}, "B2": {"who": "B", "look": "b2"}},
            "sections": [{"id": "a", "until": 20.0, "beats": 2, "singer": "A", "share": {"A": 0.75, "B": 0.25}},
                         {"id": "b", "until": 40.0, "beats": 2, "singer": "B", "share": {"B": 0.75, "A": 0.25}}],
            "cutaways": False,
        }

    def test_every_shot_is_lip_synced_and_no_take_plays_twice_running(self):
        picks = mv.plan(self.spec, self.takes, self.sync, SONG, log=lambda s: None)
        slots = picks["slots"]
        self.assertTrue(all(s["pick"] for s in slots))
        for s in slots:
            p = s["pick"]
            self.assertTrue(p["synced"])
            self.assertAlmostEqual(p["in_s"], s["start_s"] - p["offset_s"], places=3)  # take time = song time - offset
        self.assertFalse(any(a["pick"]["fid"] == b["pick"]["fid"] for a, b in zip(slots, slots[1:])))

    def test_each_section_is_mostly_its_singer_with_some_of_the_other(self):
        picks = mv.plan(self.spec, self.takes, self.sync, SONG, log=lambda s: None)
        for sec, singer, other in (("a", "A", "B"), ("b", "B", "A")):
            whos = [s["pick"]["who"] for s in picks["slots"] if s["section"] == sec]
            self.assertGreater(whos.count(singer) / len(whos), 0.6)
            self.assertGreater(whos.count(other), 0)

    def test_the_xml_keeps_the_sync_and_puts_the_song_on_a1_from_its_start(self):
        picks = mv.plan(self.spec, self.takes, self.sync, SONG, log=lambda s: None)
        with tempfile.TemporaryDirectory() as d:
            path = w.write_xmeml(build_from_picks(picks, picks["music"]), Path(d) / "mv.xml", fps=25, width=3840, height=2160)
            seq = ET.parse(path).getroot().find(".//sequence")
        v1 = seq.findall("media/video/track")[0].findall("clipitem")
        self.assertEqual(len(v1), len(picks["slots"]))
        for clip, s in zip(v1, picks["slots"]):
            self.assertLessEqual(abs((int(clip.findtext("start")) - int(clip.findtext("in"))) / 25 - s["pick"]["offset_s"]), 0.021)
        song = seq.findall("media/audio/track")[0].find("clipitem")
        self.assertEqual((song.findtext("start"), song.findtext("in")), ("0", "0"))


class CutawayTests(unittest.TestCase):
    def test_the_other_performer_comes_from_where_he_is_not_rapping_moved_by_whole_bars(self):
        takes = [fake_take("A1", axis=0, n=90, faces=1), fake_take("B1", axis=1, n=90, faces=1)]
        sync = {"A1": {"stretches": [stretch(0.0, 0.0, 40.0)]}, "B1": {"stretches": [stretch(0.0, 25.0, 40.0)]}}  # B filmed from song 25 s
        spec = {"takes": {"A1": {"who": "A"}, "B1": {"who": "B"}}, "bar_s": 2.0,
                "sections": [{"id": "a", "until": 30.0, "singer": "A"}, {"id": "b", "until": 40.0, "singer": "B"}]}
        scorer = mv.Scorer(takes)
        slot = {"start_s": 10.0, "end_s": 11.0}
        cs = mv.cutaways(slot, spec["sections"][0], {t.fid: t for t in takes}, sync, spec, scorer, {})
        self.assertTrue(cs)
        for c in cs:
            self.assertEqual(c.who, "B")
            self.assertFalse(c.synced)
            shift = c.song_s - slot["start_s"]
            self.assertAlmostEqual(shift / 2.0, round(shift / 2.0), places=6)  # whole bars: the same beat of the bar
            self.assertTrue(25.0 <= c.song_s and c.song_s + 1.0 <= 30.0 - mv.FREE_MARGIN_S)  # before B's own part
            self.assertAlmostEqual(c.in_s, c.song_s, places=3)  # B1's take time is its song time


if __name__ == "__main__":
    unittest.main()
