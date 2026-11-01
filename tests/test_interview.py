"""Interview pieces: A-roll layout, B-roll regions and timing, the music dip, and the timeline they build."""

import tempfile
import unittest
from pathlib import Path

from tests.test_fill import fake_embed, fake_take
from videtor.export import xmeml_writer as w
from videtor.kb import xmeml
from videtor.plan import fill, interview

AROLL = {
    "version": 1, "name": "talk", "key": "T::footage", "fps": 25, "width": 1920, "height": 1080, "length_s": 20.0, "hook_s": 2.0,
    "segments": [
        {"id": "a1", "fid": "I1", "name": "I1.MP4", "in_s": 1.0, "out_s": 5.0, "visible": True, "text": "intro"},
        {"id": "a2", "fid": "I1", "name": "I1.MP4", "in_s": 6.0, "out_s": 9.0, "visible": False, "text": "process"},
        {"id": "a3", "fid": "I2", "name": "I2.MP4", "in_s": 2.0, "out_s": 6.0, "visible": False, "text": "jewellery"},
    ],
}
MAP = {
    "version": 1, "name": "talk", "key": "T::footage", "length_s": 20, "fps": 25, "width": 1920, "height": 1080,
    "sections": [
        {"name": "hook", "region": "hook", "slots": [{"id": "h1", "beats": 1, "show": "axis 0"}, {"id": "h2", "beats": 1, "show": "axis 1"}]},
        {"name": "making", "over": ["a2"], "slots": [{"id": "m1", "beats": 2, "show": "axis 2"}, {"id": "m2", "beats": 2, "show": "axis 3"}]},
        {"name": "jewels", "over": ["a3"], "slots": [{"id": "j1", "beats": 1, "show": "axis 4"}]},
        {"name": "end", "region": "end", "slots": [{"id": "e1", "beats": 2, "show": "axis 5"}, {"id": "e2", "beats": 2, "show": "axis 6"}]},
    ],
}


class LayoutTests(unittest.TestCase):
    def test_segments_follow_the_hook_and_regions_cover_them(self):
        placed = interview.layout(AROLL)
        self.assertEqual([(s["start_s"], s["length_s"]) for s in placed], [(2.0, 4.0), (6.0, 3.0), (9.0, 4.0)])
        spans = interview.regions(AROLL, placed)
        self.assertEqual(spans["hook"], (0.0, 2.0))
        self.assertEqual(spans["end"], (13.0, 20.0))

    def test_slot_times_fill_each_region_exactly_and_cut_on_beats_inside(self):
        placed = interview.layout(AROLL)
        beats = [i * 0.5 for i in range(41)]
        times = interview.slot_times(MAP, AROLL, placed, beats)
        starts = [round(t, 2) for t, _ in times]
        self.assertEqual(starts[0], 0.0)
        self.assertEqual(round(times[1][0] + times[1][1], 2), 2.0)  # the hook ends where the voice starts
        self.assertEqual(starts[2], 6.0)  # B-roll over a2 starts exactly on its join
        self.assertEqual(round(times[3][0] + times[3][1], 2), 9.0)  # and ends on the next join
        self.assertEqual(starts[5], 13.0)
        self.assertAlmostEqual(sum(l for _, l in times[5:]), 7.0, places=6)
        self.assertLess(min(abs(times[3][0] + 0.12 - b) for b in beats), 0.03)  # an inside cut leads a beat (to the frame)

    def test_over_must_name_consecutive_segments(self):
        placed = interview.layout(AROLL)
        bad = {**MAP, "sections": [{"name": "x", "over": ["a1", "a3"], "slots": [{"id": "x1", "beats": 1, "show": "axis 0"}]}]}
        with self.assertRaises(ValueError):
            interview.slot_times(bad, AROLL, placed, None)

    def test_music_dips_in_steps_under_the_voice_and_comes_back_for_the_end(self):
        pieces = interview.music_levels(20.0, 2.0, 13.0, up=-3.0, under=-15.0)
        self.assertEqual(pieces[0], (0.0, 2.0, -3.0))
        self.assertEqual(pieces[-1], (13.0, 20.0, -3.0))
        self.assertIn(-15.0, [g for *_, g in pieces])
        gains = [g for *_, g in pieces]
        self.assertEqual(gains, sorted(gains[:4], reverse=True) + sorted(gains[4:]))  # down, then up
        self.assertTrue(all(abs(a[1] - b[0]) < 1e-9 for a, b in zip(pieces, pieces[1:])))  # contiguous


class FinishTests(unittest.TestCase):
    def test_ramp_parts_fill_the_slot_and_read_the_take_continuously(self):
        choice = {"fid": "B0", "path": "/x/B0.MP4", "name": "B0.MP4", "in_s": 4.0, "out_s": 6.0, "speed": 1.0, "duration_s": 20.0,
                  "file_fps": 50.0, "width": 3840, "height": 2160, "audio_channels": 2, "motion": {"scale": 100.0}}
        parts = interview.ramp_parts(choice, 2.0, 25.0)
        self.assertEqual([p["speed"] for p, _ in parts], [1.0, 0.5, 1.1])
        self.assertAlmostEqual(sum(s for _, s in parts), 2.0, places=6)
        self.assertEqual([round(s * 25) for _, s in parts], [4, 42, 4])
        for (a, _), (b, _) in zip(parts, parts[1:]):
            self.assertAlmostEqual(a["out_s"], b["in_s"], places=4)

    def test_zoom_runs_continuously_across_the_parts_of_a_ramp(self):
        motion = interview._zoomed({"scale": 100.0}, {"zoom": {"dir": "in", "amount": 0.08}}, 0.0, 0.5)
        self.assertEqual(motion["zoom"], {"from": 100.0, "to": 104.0})
        out = interview._zoomed({"scale": 100.0}, {"zoom": {"dir": "out", "amount": 0.06}}, 0.5, 1.0)
        self.assertEqual(out["zoom"], {"from": 103.0, "to": 100.0})

    def test_handheld_shots_are_stabilised_not_ramped_and_about_half_are_ramped(self):
        placed = interview.layout(AROLL)
        takes = [fake_take(f"B{i}", i, n=30) for i in range(7)]
        takes[2].a["shake"][:] = 0.02  # handheld
        times = interview.slot_times(MAP, AROLL, placed, None)
        picks = fill.fill(MAP, takes, times, fake_embed)
        interview.finish_fx(picks, MAP, {t.fid: t for t in takes}, fill.Pool(takes))
        fx = {s["id"]: s["fx"] for s in picks["slots"]}
        shaky = [k for k, f in fx.items() if f["stabilize"]]
        self.assertTrue(shaky)
        self.assertFalse(any(fx[k]["ramp"] for k in shaky))
        self.assertTrue(all("zoom" in f for f in fx.values()))
        self.assertLessEqual(sum(f["ramp"] for f in fx.values()), round(0.5 * len(fx)))


class BuildTests(unittest.TestCase):
    def test_voice_on_v1_and_a1_broll_over_it_on_v2_hook_and_end_on_v1(self):
        placed = interview.layout(AROLL)
        takes = [fake_take(f"B{i}", i, n=30) for i in range(7)]
        times = interview.slot_times(MAP, AROLL, placed, None)
        picks = fill.fill(MAP, takes, times, fake_embed)
        i1, i2 = fake_take("I1", 7, n=40, faces=1), fake_take("I2", 7, n=40, faces=1)
        music = {"path": "/Music/royal.wav", "duration_s": 200.0, "in_s": 30.0, "levels": interview.music_levels(20.0, 2.0, 13.0), "fade_out_s": 1.0}
        tl = interview.build(AROLL, placed, picks, MAP, {"I1": i1, "I2": i2}, music, [], {"a1": 3.0, "a2": 3.0, "a3": -1.0},
                             lambda take, seg: {"scale": 50.0, "rotation": 0.0, "center": [0.0, 0.0]})
        with tempfile.TemporaryDirectory() as tmp:
            path = w.write_xmeml(tl, Path(tmp, "talk.xml"), fps=25, width=1920, height=1080)
            seq = xmeml.load(path)[0]
            text = path.read_text()
        video = [t for t in seq.tracks if t.media == "video"]
        audio = [t for t in seq.tracks if t.media == "audio"]
        v1 = [(c.file_name, c.start, c.end) for c in video[0].clips]
        self.assertEqual([n for n, *_ in v1][2:5], ["I1.MP4", "I1.MP4", "I2.MP4"])  # hook (2 shots), then the voice
        self.assertEqual(v1[2][1], 50)  # the voice starts at 2 s
        self.assertEqual({c.start for c in video[1].clips} & {150, 225}, {150, 225})  # overlays start on the joins
        self.assertTrue(all(c.enabled for c in video[0].clips + video[1].clips))
        self.assertFalse(any(c.enabled for c in video[2].clips))
        self.assertEqual([c.file_name for c in audio[0].clips], ["I1.MP4", "I1.MP4", "I2.MP4"])  # A1: only the voice
        self.assertEqual(len(audio[1].clips), len(music["levels"]))
        self.assertIn("end-black", text)
        self.assertEqual(seq.duration, 500)


if __name__ == "__main__":
    unittest.main()
