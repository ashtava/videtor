"""What the user sets for a project in the app (the shape of the frame, the music) and what the cut makes of it."""

import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from test_fill import fake_embed, fake_take
from test_pipeline import the_map, write_project
from videtor import events, pipeline
from videtor.plan import settings


def fake_track(root: Path, name: str, period: float, lift_at: float | None = None, seconds: float = 60.0) -> str:
    """A track that exists and is already analysed: a beat every `period`, quiet until `lift_at` then loud."""
    path = root / f"{name}.wav"
    path.write_bytes(b"")
    hop = 0.5
    n = int(seconds / hop)
    rms = [-30.0 if lift_at is not None and k * hop < lift_at else -8.0 for k in range(n)]
    data = {"path": str(path), "duration_s": seconds, "tempo_bpm": 60.0 / period, "beat_period_s": period,
            "beats_s": [round(k * period, 3) for k in range(int(seconds / period))], "beat_strength": [1.0] * int(seconds / period),
            "rms_db": rms, "onset_half_s": [1.0] * n}
    (root / "beats").mkdir(exist_ok=True)
    (root / "beats" / f"{name}.beats.json").write_text(json.dumps(data))
    return str(path)


def beat_map(music: dict | None) -> dict:
    """Eight beats in four slots, the reveal after four."""
    slots = [{"id": "a", "beats": 2, "show": "axis 0"}, {"id": "b", "beats": 2, "show": "axis 1"},
             {"id": "c", "beats": 2, "show": "axis 2", "role": "reveal"}, {"id": "d", "beats": 2, "show": "axis 3"}]
    m = {"version": 1, "name": "proj", "key": "proj::footage", "length_s": 4.0, "fps": 25, "width": 1080, "height": 1920, "cut_on": "accents",
         "sections": [{"name": "s", "slots": slots}]}
    if music:
        m["music"] = music
    return m


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.beats = self.root / "beats"
        self.a = fake_track(self.root, "plan-track", 0.5)
        self.b = fake_track(self.root, "my-track", 0.6, lift_at=6.0)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_choices_are_kept_and_checked(self):
        self.assertEqual(settings.load(self.root), {})
        saved = settings.save(self.root, {"frame": "horizontal", "music": {"path": self.b, "start": "at", "in_s": 3}, "other": 1})
        self.assertEqual(settings.load(self.root), saved)
        self.assertEqual(set(saved), {"frame", "music"})  # only what this file knows
        for bad in ({"frame": "wide"}, {"music": {}}, {"music": {"path": self.b, "start": "middle"}}, {"music": {"path": self.b, "start": "at"}}):
            with self.assertRaises(ValueError):
                settings.save(self.root, bad)
        self.assertEqual(settings.describe(saved), {"frame": "Horizontal 16:9", "size": [1920, 1080], "music": "my-track.wav, from 3 s"})
        self.assertEqual(settings.describe({"music": {"none": True}})["music"], "no music")
        self.assertEqual(settings.frame_of({"width": 1920, "height": 1080}), "horizontal")
        self.assertIsNone(settings.frame_of({"width": 1000, "height": 1000}))

    def test_the_frame_the_user_chose_wins_over_the_plans(self):
        m, notes = settings.apply(beat_map(None), {"frame": "horizontal"}, self.beats)
        self.assertEqual((m["width"], m["height"]), (1920, 1080))
        self.assertIn("1920×1080", notes[0])
        self.assertIn("the plan said 1080×1920", notes[0])
        same, notes = settings.apply(beat_map(None), {"frame": "vertical"}, self.beats)
        self.assertEqual(notes, [])  # the plan already says so

    def test_another_track_keeps_the_plans_beats_and_the_length_follows_its_tempo(self):
        plan = beat_map({"path": self.a, "in_s": 0.0, "fade_out_s": 1.5})
        m, notes = settings.apply(plan, {"music": {"path": self.b, "start": "beginning"}}, self.beats)
        self.assertEqual(m["music"], {"path": self.b, "in_s": 0.0, "fade_out_s": 1.5})
        self.assertEqual(m["length_s"], 4.8)  # eight beats at 0.6 s instead of 0.5 s
        self.assertEqual([s["beats"] for s in m["sections"][0]["slots"]], [2, 2, 2, 2])  # the rhythm of the plan is untouched
        self.assertTrue(any("4.8 s instead of 4.0 s" in n and "plan-track" in n for n in notes))
        self.assertEqual(plan["length_s"], 4.0)  # the plan itself is not changed
        m, _ = settings.apply(plan, {"music": {"path": self.b, "start": "at", "in_s": 12.25}}, self.beats)
        self.assertEqual(m["music"]["in_s"], 12.25)  # the second the user gave, as given

    def test_the_best_start_lands_the_tracks_lift_on_the_plans_reveal(self):
        plan = beat_map({"path": self.a, "in_s": 0.0})
        m, notes = settings.apply(plan, {"music": {"path": self.b, "start": "auto"}}, self.beats)
        self.assertAlmostEqual(m["music"]["in_s"], 6.0 - 4 * 0.6, places=2)  # four beats before the lift at 6 s
        self.assertTrue(any("lands its lift" in n for n in notes))
        kept, notes = settings.apply(plan, {"music": {"path": self.a, "start": "auto"}}, self.beats)
        self.assertEqual((kept["music"]["in_s"], kept["length_s"]), (0.0, 4.0))  # the plan's own track: its in-point stands
        self.assertTrue(any("plan's in-point" in n for n in notes))

    def test_no_music_keeps_every_shot_as_long_as_its_beats_were(self):
        m, notes = settings.apply(beat_map({"path": self.a, "in_s": 0.0}), {"music": {"none": True}}, self.beats)
        self.assertNotIn("music", m)
        self.assertNotIn("cut_on", m)
        self.assertEqual([s["length_s"] for s in m["sections"][0]["slots"]], [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(m["length_s"], 4.0)
        self.assertIn("no music", notes[0])

    def test_a_track_that_is_gone_or_too_short_is_said(self):
        plan = beat_map({"path": self.a, "in_s": 0.0})
        m, notes = settings.apply(plan, {"music": {"path": str(self.root / "gone.wav")}}, self.beats)
        self.assertEqual(m["music"]["path"], self.a)
        self.assertIn("not there", notes[0])
        short = fake_track(self.root, "short", 0.5, seconds=3.0)
        _, notes = settings.apply(plan, {"music": {"path": short, "start": "beginning"}}, self.beats)
        self.assertTrue(any("ends 1.0 s before the cut does" in n for n in notes))


class CutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
        self.paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse", beats=self.root / "beats")
        self.sm = write_project(self.root, takes, the_map())
        self.track = fake_track(self.root, "my-track", 0.5)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_cut_is_made_in_the_users_shape_with_the_users_music(self):
        settings.save(self.sm.parent, {"frame": "horizontal", "music": {"path": self.track, "start": "at", "in_s": 2.0}})
        said: list[dict] = []
        picks = pipeline.cut(self.sm, sheets=False, sfx=False, paths=self.paths, ev=events.Events(said.append), embed_texts=fake_embed)["picks"]
        self.assertEqual((picks["width"], picks["height"]), (1920, 1080))
        self.assertEqual((picks["music"]["path"], picks["music"]["in_s"]), (self.track, 2.0))
        seq = ET.parse(self.sm.parent / "proj.xml").getroot().find(".//sequence")
        self.assertEqual((seq.findtext(".//samplecharacteristics/width"), seq.findtext(".//samplecharacteristics/height")), ("1920", "1080"))
        self.assertTrue(any("my-track" in (e.text or "") for e in seq.iter("pathurl")))
        texts = [e["text"] for e in said]
        self.assertTrue(any("as you chose" in t for t in texts))
        self.assertTrue(any(t.startswith("your music: my-track") for t in texts))
        self.assertEqual(json.loads(self.sm.read_text())["width"], 1080)  # the plan on disk is the plan as written

    def test_without_settings_the_plan_stands(self):
        picks = pipeline.cut(self.sm, sheets=False, sfx=False, paths=self.paths, embed_texts=fake_embed)["picks"]
        self.assertEqual((picks["width"], picks["height"]), (1080, 1920))
        self.assertIsNone(picks.get("music"))


if __name__ == "__main__":
    unittest.main()
