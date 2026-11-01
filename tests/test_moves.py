"""Editing moves (ramps, flickers, stacks, black, flash frames), clicks and beds, the music medley, the keeper."""

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from videtor.export import xmeml_writer as w
from videtor.plan import fill, keeper, moves, shotmap
from videtor.plan import sfx as plan_sfx
from videtor.plan.build import build_from_picks
from tests.test_fill import MAP, fake_embed, fake_take

BEATS = [i * 0.5 for i in range(40)]


def cut(m, takes):
    return fill.fill(m, takes, shotmap.timing(m, BEATS, 25.0), fake_embed)


class RampTests(unittest.TestCase):
    def test_parts_fill_the_slot_in_whole_frames_and_chain_their_source(self):
        parts = moves.ramp_parts(1.56, 25.0, moves.RAMPS["arrival"])
        self.assertEqual(sum(round(s * 25) for s, _ in parts), 39)
        self.assertEqual([sp for _, sp in parts], [1.0, 0.5, 0.25])
        chained = moves.chain({"in_s": 2.0, "speed": 0.4}, parts)
        for (a, _), (b, _) in zip(chained, chained[1:]):
            self.assertAlmostEqual(a["out_s"], b["in_s"], places=3)  # each part's source follows the last
        self.assertAlmostEqual(moves.source_factor(moves.RAMPS["arrival"]), 0.12 + 0.165 + 0.1375, places=4)

    def test_a_ramp_slot_ramps_high_frame_rate_takes_only(self):
        m = {**MAP, "length_s": 2.0, "sections": [{"name": "s", "slots": [{"id": "a", "beats": 4, "show": "axis 0", "ramp": "arrival"}]}]}
        fast = cut(m, [fake_take("F", 0)])["slots"][0]["pick"]  # 100 fps
        self.assertEqual([sp for _, sp in fast["ramp"]], [1.0, 0.5, 0.25])
        self.assertAlmostEqual(fast["out_s"] - fast["in_s"], 2.0 * moves.source_factor(moves.RAMPS["arrival"]), places=2)
        slow = cut(m, [fake_take("S", 0, fps=25.0)])["slots"][0]["pick"]
        self.assertNotIn("ramp", slow)
        self.assertEqual(slow["speed"], 1.0)


class FlickerStackBlackTests(unittest.TestCase):
    def setUp(self):
        self.takes = [fake_take(f"C{n}", 0) for n in range(1, 7)] + [fake_take("W1", 1), fake_take("W2", 1), fake_take("W3", 1), fake_take("W4", 1), fake_take("W5", 1)]
        self.m = {**MAP, "length_s": 3.0, "sections": [{"name": "s", "slots": [
            {"id": "cards", "beats": 1, "show": "axis 0", "flicker": 3, "takes": ["C3*", "C1*", "C2*", "C4*"]},
            {"id": "pause", "beats": 2, "black": True},
            {"id": "wall", "beats": 3, "show": "axis 1", "stack": True}]}]}
        self.picks = cut(self.m, self.takes)
        self.cards, self.pause, self.wall = self.picks["slots"]

    def test_a_flicker_is_sub_shots_of_n_frames_from_different_takes_in_the_named_order(self):
        pieces = self.cards["pick"]["pieces"]
        self.assertEqual([p["name"] for p in pieces], ["C3.MP4", "C1.MP4", "C2.MP4"])  # 0.38 s = 10 frames: 3 + 3 + 4
        self.assertAlmostEqual(sum(p["length_s"] for p in pieces), self.cards["length_s"], places=3)
        self.assertTrue(all(round(p["length_s"] * 25) >= 1 for p in pieces))

    def test_a_black_slot_has_no_picture_and_a_stack_pops_smaller_shots_on_top(self):
        self.assertIsNone(self.pause["pick"])
        self.assertTrue(self.pause["black"])
        layers = self.wall["layers"]
        self.assertEqual([l["offset_s"] for l in layers], [0.2, 0.4, 0.6, 0.8][: len(layers)])
        self.assertTrue(layers)
        names = {self.wall["pick"]["name"]} | {l["name"] for l in layers}
        self.assertEqual(len(names), 1 + len(layers))  # every layer another take
        self.assertLess(layers[0]["motion"]["scale"], self.wall["pick"]["motion"]["scale"])

    def test_the_xml_plays_sub_shots_ramps_a_gap_and_the_layers_above(self):
        with tempfile.TemporaryDirectory() as d:
            path = w.write_xmeml(build_from_picks(self.picks), Path(d) / "x.xml", fps=25, width=1080, height=1920)
            seq = ET.parse(path).getroot().find(".//sequence")
        v = seq.findall("media/video/track")
        v1 = v[0].findall("clipitem")
        self.assertEqual(len(v1), len(self.cards["pick"]["pieces"]) + 1)  # the flicker's sub-shots, nothing in the black, the stack's base
        starts = [int(c.findtext("start")) for c in v1]
        self.assertEqual(starts[-1], round(self.wall["start_s"] * 25))  # the gap of the black slot sits before the base
        self.assertEqual(len(v), 3 + len(self.wall["layers"]))  # V1, two alternate tracks, one track per layer
        layer1 = v[3].find("clipitem")
        self.assertEqual(int(layer1.findtext("start")), round((self.wall["start_s"] + 0.2) * 25))
        self.assertEqual(int(layer1.findtext("end")), round((self.wall["start_s"] + self.wall["length_s"]) * 25))


class FlashTests(unittest.TestCase):
    def test_flash_frames_land_on_section_changes_and_every_two_seconds_between(self):
        slots = [{"id": f"s{i}", "section": "a" if i < 6 else "b", "start_s": i * 0.5} for i in range(10)]
        self.assertEqual(moves.flash_times(slots, every_s=2.0), [0.5, 2.5, 3.0])  # 3.0 opens section b; 4.5 is too soon after it


class ClickBedTests(unittest.TestCase):
    def test_clicks_on_a_flicker_and_a_stack_and_a_bed_from_the_shot_map(self):
        with tempfile.TemporaryDirectory() as d:
            import wave
            for name, secs in (("Karl CLick.wav", 0.08), ("crowd.wav", 30.0)):
                with wave.open(str(Path(d) / name), "wb") as f:
                    f.setnchannels(2), f.setsampwidth(2), f.setframerate(48000)
                    f.writeframes(np.zeros(int(48000 * secs) * 2, np.int16).tobytes())
            kb = {"kinds": {"other": {"snippets": [{"file": "Karl CLick.wav", "path": str(Path(d) / "Karl CLick.wav"), "in_s": 0.0, "out_s": 0.08,
                                                    "level_db": -8.0, "uses": 26, "edits": ["E"]}], "placement": {}}},
                  "rates": {"event": {"edits": ["E"], "per_min": {}}}, "edits": {"E": {"type": "event"}}}
            pieces = [{"length_s": 0.12}] * 4
            picks = {"type": "event", "key": "K", "slots": [
                {"id": "cards", "section": "a", "start_s": 0.0, "length_s": 0.48, "pick": {"name": "C", "pieces": pieces}},
                {"id": "wall", "section": "b", "start_s": 0.48, "length_s": 1.0, "pick": {"name": "W"}, "layers": [{"offset_s": 0.2}, {"offset_s": 0.4}]},
                {"id": "party", "section": "b", "start_s": 1.48, "length_s": 3.0, "pick": {"name": "P"}}],
                "beds": [{"path": str(Path(d) / "crowd.wav"), "in_s": 2.0, "from": "wall", "to": None, "gain_db": -6.0}]}
            out = plan_sfx.place(picks, kb, None, Path(d))
        clicks = [round(c["start_s"], 2) for c in out if c["kind"] == "click"]
        self.assertEqual(clicks, [0.0, 0.12, 0.24, 0.36, 0.68, 0.88])
        bed = next(c for c in out if c["kind"] == "bed")
        self.assertEqual((bed["start_s"], bed["in_s"], bed["gain_db"]), (0.48, 2.0, -6.0))
        self.assertAlmostEqual(bed["end_s"], 4.48, places=2)


class MedleyTests(unittest.TestCase):
    def test_segments_play_one_after_the_other_on_their_own_beats_with_a_silent_gap(self):
        from videtor.cli import _medley

        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            (cache / "a.beats.json").write_text(json.dumps({"beats_s": [i * 0.6 for i in range(100)], "duration_s": 60.0}))
            (cache / "b.beats.json").write_text(json.dumps({"beats_s": [10.0 + i * 0.5 for i in range(100)], "duration_s": 70.0}))
            music, beats = _medley([{"path": "/x/a.mp3", "in_s": 3.0, "beats": 4}, {"gap_beats": 2}, {"path": "/x/b.mp3", "in_s": 12.0}], cache)
        segs = music["segments"]
        self.assertEqual([(s["start_s"], s["length_s"]) for s in segs], [(0.0, 2.4), (3.6, 58.0)])  # 4 beats of 0.6 s, 2 beats of silence, then b to its end
        self.assertEqual(beats[:8], [0.0, 0.6, 1.2, 1.8, 2.4, 3.0, 3.6, 4.1])
        tl = build_from_picks({"name": "m", "fps": 25, "slots": [{"id": "x", "length_s": 6.0, "pick": None, "alternates": []}]}, music)
        a = [t for t in tl.tracks if t.kind == "Audio"][0]
        self.assertEqual([round(c.range_in_parent().start_time.to_seconds(), 2) for c in a if hasattr(c, "media_reference")], [0.0, 3.6])


class BreakdownTests(unittest.TestCase):
    def test_a_written_cut_is_read_back_as_a_medley_with_a_silence_ramps_and_black(self):
        from videtor.kb import breakdown

        takes = [fake_take(f"T{n}", n % 2) for n in range(6)]
        m = {**MAP, "length_s": 6.0, "sections": [{"name": "s", "slots": [
            {"id": "a", "beats": 4, "show": "axis 0", "ramp": "arrival"}, {"id": "b", "beats": 2, "black": True},
            {"id": "c", "beats": 6, "show": "axis 1"}]}]}
        picks = cut(m, takes)
        music = {"segments": [{"path": "/Music/one.wav", "duration_s": 60.0, "in_s": 10.0, "start_s": 0.0, "length_s": 2.0},
                              {"path": "/Music/two.wav", "duration_s": 60.0, "in_s": 30.0, "start_s": 3.0, "length_s": 3.0}], "fade_out_s": 0.5}
        with tempfile.TemporaryDirectory() as d:
            path = w.write_xmeml(build_from_picks(picks, music), Path(d) / "edit.xml", fps=25, width=1080, height=1920)
            got = breakdown.describe(path, Path(d), ["/Volumes/T"])
        self.assertEqual([seg["file"] for seg in got["music"]], ["one.wav", "two.wav"])
        self.assertEqual(got["silences"], [(2.0, 3.0)])
        self.assertIn("100% -> 50% -> 25%", got["ramps"])
        self.assertEqual(len(got["black"]), 1)
        self.assertAlmostEqual(got["black"][0][0], picks["slots"][1]["start_s"], places=1)


class KeeperTests(unittest.TestCase):
    def test_kept_samples_copies_and_scores_over_the_pool(self):
        t = fake_take("K", 0, n=10)
        self.assertEqual(list(np.where(keeper.labels(t, [(1.0, 2.0)]))[0]), [1, 2, 3, 4])  # samples at 0.75-2.25 s
        self.assertEqual(keeper._base("c1879(1).mp4"), "c1879.mp4")
        pool = fill.Pool([t, fake_take("L", 1, n=10)])
        f = keeper.features(t, fake_embed(["axis 0", "axis 1"]), pool)
        self.assertEqual(f.shape, (10, 2 + len(keeper.SCALARS)))
        model = {"bucket": "x", "concepts": ["axis 0", "axis 1"], "mean": [0.0] * f.shape[1], "std": [1.0] * f.shape[1],
                 "coef": [1.0, -1.0] + [0.0] * len(keeper.SCALARS), "intercept": 0.0}
        z = keeper.scores(model, pool.takes, pool, fake_embed, Path(tempfile.gettempdir()) / "no-kb")
        self.assertGreater(float(z["K"].mean()), float(z["L"].mean()))  # the kept concept scores higher


if __name__ == "__main__":
    unittest.main()
