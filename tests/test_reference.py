"""A plan shaped on a reference video: the reference's shots become slots of the same lengths, a burst becomes a
flicker, the fill matches pictures, the user's own track counts the shots in beats, and the step runs end to end."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from test_fill import DIM, fake_take, unit
from videtor import events, pipeline
from videtor.plan import fill, reference, settings, shotmap


def words(texts):
    """Any text as a unit vector: "axis k" as basis vector k (test_fill), other words by their letters."""
    out = []
    for t in texts:
        if t.startswith("axis "):
            out.append(unit(np.eye(DIM)[int(t.split()[-1])] + 0.05))
        else:
            rng = np.random.default_rng(int(hashlib.sha1(t.encode()).hexdigest()[:8], 16))
            out.append(unit(rng.normal(size=DIM)))
    return np.stack(out)


def a_ref(lengths, fps=25.0, over=None) -> dict:
    """A reading with one shot per length, shot k showing basis picture k % DIM."""
    shots, t = [], 0.0
    for k, ln in enumerate(lengths):
        shots.append({"start_s": round(t, 3), "length_s": ln, "frames": round(ln * fps), "bw": False, "move": None, "via": "cut", "shows": "hug",
                      "scores": {}, "faces": 0, "face_area": 0.0, "hands": 0, "scale": None, "card": False, "rows": [k, k + 1], **(over or {}).get(k, {})})
        t += ln
    return {"version": reference.VERSION, "sig": "x", "file": "ref.mp4", "path": "/ref.mp4", "fps": fps, "width": 1080, "height": 1920,
            "duration_s": round(t, 3), "tempo_bpm": 120.0, "beats_s": [], "beat_fit": {}, "shots": shots}


class ShapeTests(unittest.TestCase):
    def test_every_shot_is_a_slot_of_its_length_and_a_burst_is_one_flicker(self):
        ref = a_ref([2.0, 0.4, 0.08, 0.08, 0.08, 0.08, 0.08, 1.0, 0.04, 0.6])
        us = reference.units(ref, 25.0)
        self.assertEqual([u["length_s"] for u in us], [2.0, 0.4, 0.4, 1.04, 0.6])  # the burst is one; the one-frame shot joins the shot before
        self.assertEqual([u["flicker"] for u in us], [0, 0, 2, 0, 0])
        self.assertEqual(us[2]["shots"], [2, 3, 4, 5, 6])
        self.assertAlmostEqual(sum(u["length_s"] for u in us), ref["duration_s"], places=3)
        m = reference.plan(ref, "p", "p::footage", sound="/tmp/s.wav")
        self.assertEqual(shotmap.validate(m), [])
        self.assertEqual(shotmap.warnings(m), [])
        slots = shotmap.slots(m)
        self.assertEqual([s["length_s"] for s in slots], [2.0, 0.4, 0.4, 1.04, 0.6])
        self.assertEqual(slots[2]["flicker"], 2)
        self.assertEqual(slots[2]["like"], [2, 3, 4, 5, 6])
        self.assertEqual((m["cut_on"], m["music"]["path"], m["length_s"]), ("seconds", "/tmp/s.wav", 4.44))
        # the cuts fall where the reference has them, music or not
        times = shotmap.timing(m, [0.5 * k for k in range(20)], 25.0)
        self.assertEqual([round(a, 2) for a, _ in times], [0.0, 2.0, 2.4, 2.8, 3.84])

    def test_runs_and_holds_are_sections(self):
        us = reference.units(a_ref([2.0, 2.5, 3.0] + [0.4] * 6 + [1.5, 2.0, 2.2]), 25.0)
        secs = reference.sections(us)
        self.assertEqual([(n, len(i)) for n, i in secs], [("opening", 3), ("run-2", 6), ("closing", 3)])
        self.assertEqual(sorted(i for _, idx in secs for i in idx), list(range(12)))
        self.assertEqual(reference.sections(reference.units(a_ref([0.4, 2.0]), 25.0)), [("opening", [0, 1])])

    def test_on_another_track_the_shots_are_counted_to_the_half_beat_without_drifting(self):
        beats = reference.on_beats([0.3] * 10 + [2.0], 0.5)
        self.assertTrue(all(b >= 0.5 and (2 * b) % 1 == 0 for b in beats))
        self.assertEqual(sum(beats[:10]) * 0.5, 3.0)  # ten shots of 0.3 s end at 3.0 s, not at 5.0
        self.assertEqual(sum(beats) * 0.5, 5.0)

    def test_what_the_reference_shows_is_said_in_notes_and_in_words(self):
        ref = a_ref([1.0, 1.0, 1.0], over={1: {"via": "flash", "bw": True, "card": True, "scale": "close", "faces": 1}})
        m = reference.plan(ref, "p", "p::footage")
        self.assertNotIn("music", m)
        self.assertNotIn("effects", m)
        text = " ".join(m["notes"])
        self.assertIn("1 flashes", text)
        self.assertIn("words on screen", text)
        self.assertEqual(shotmap.slots(m)[1]["scale"], ["close"])
        with_fx = reference.plan(ref, "p", "p::footage", effects=True)
        self.assertEqual(shotmap.slots(with_fx)[1]["effects"], ["flash", "bw"])
        self.assertEqual(shotmap.validate(with_fx), [])
        self.assertIn("| 0:01.0 | 1.00 s | hug |", reference.markdown(ref, m))


class LikeTests(unittest.TestCase):
    def test_a_slot_takes_the_footage_that_looks_like_its_reference_shot(self):
        takes = [fake_take(f"A{k}", k) for k in range(5)]
        m = {"version": 1, "name": "p", "key": "k", "length_s": 2.0, "fps": 25, "width": 1080, "height": 1920,
             "sections": [{"name": "s", "slots": [{"id": "a", "length_s": 1.0, "show": "axis 0"}, {"id": "b", "length_s": 1.0, "show": "axis 1"}]}]}
        times = [(0.0, 1.0), (1.0, 1.0)]
        plain = fill.fill(m, takes, times, words)
        self.assertEqual([s["pick"]["name"] for s in plain["slots"]], ["A0.MP4", "A1.MP4"])
        seen = fill.fill(m, takes, times, words, like={"a": unit(np.eye(DIM)[3] + 0.05)[None, :]})
        self.assertEqual([s["pick"]["name"] for s in seen["slots"]], ["A3.MP4", "A1.MP4"])  # the picture outweighs the words; a slot with none is as before

    def test_the_pictures_are_found_by_the_slots_like_and_missing_ones_are_no_error(self):
        d = Path(tempfile.mkdtemp())
        try:
            ref = a_ref([1.0, 0.08, 0.08, 0.08, 0.08, 1.0])
            work = d / reference.WORK
            work.mkdir(parents=True)
            (work / "read.json").write_text(json.dumps(ref))
            np.save(work / "emb.npy", np.eye(6, DIM, dtype=np.float16))
            m = reference.plan(ref, "p", "p::footage")
            got = reference.likes(m, d)
            ids = [s["id"] for s in shotmap.slots(m)]
            self.assertEqual({k: v.shape[0] for k, v in got.items()}, {ids[0]: 1, ids[1]: 4, ids[2]: 1})
            self.assertEqual(int(got[ids[2]].argmax()), 5)
            self.assertEqual(reference.likes(m, d / "elsewhere"), {})
            self.assertEqual(reference.likes({"sections": []}, d), {})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class SettingsTests(unittest.TestCase):
    def test_the_users_track_counts_a_reference_plan_in_its_beats_and_no_music_keeps_the_seconds(self):
        d = Path(tempfile.mkdtemp())
        try:
            track = d / "mine.wav"
            track.write_bytes(b"x")
            (d / "mine.beats.json").write_text(json.dumps({"path": str(track), "duration_s": 60.0, "tempo_bpm": 120.0, "beats_s": [0.5 * k for k in range(120)],
                                                             "rms_db": [-20.0] * 120, "energy": [1.0] * 120, "energy_hop_s": 0.5}))
            m = reference.plan(a_ref([0.28] * 10 + [2.0]), "p", "p::footage", sound=str(d / "ref.wav"))
            mine, notes = settings.apply(m, {"music": {"path": str(track), "start": "beginning"}}, d)
            self.assertNotIn("cut_on", mine)
            self.assertEqual(Path(mine["music"]["path"]), track)
            self.assertEqual(mine["length_s"], 4.75)  # 4.8 s of shots, to the half beat of a 120 BPM track
            self.assertTrue(all(s["beats"] >= 0.5 for s in shotmap.slots(mine)))
            self.assertIn("counted in its beats", " ".join(notes))
            self.assertEqual(m["cut_on"], "seconds")  # the plan itself is left as written
            silent, _ = settings.apply(m, {"music": {"none": True}}, d)
            self.assertNotIn("music", silent)
            self.assertEqual([s["length_s"] for s in shotmap.slots(silent)], [0.28] * 10 + [2.0])
            self.assertEqual(settings.save(d, {"frame": "square", "reference": "/v.mp4", "music": None}), {"frame": "square", "reference": "/v.mp4"})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class StepTests(unittest.TestCase):
    """The step end to end on a small made-up video (three shots of two seconds): ffmpeg and the cut reader are the
    real ones; CLIP and the face detector are stand-ins (shot k looks like basis picture k)."""

    @classmethod
    def setUpClass(cls):
        from test_promos import make_video

        cls.root = Path(tempfile.mkdtemp())
        cls.video = make_video(cls.root / "Their Reel.mp4", seconds=6)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    @staticmethod
    def read(path, work, ev=None):
        def pictures(images):  # three frames a shot, in order: shot k is basis picture k
            return np.stack([unit(np.eye(DIM)[min(i // 3, DIM - 1)] + 0.05) for i in range(len(images))])

        return reference.read(path, work, ev=ev, embed=pictures, embed_texts=words, detect=lambda rgb: ([], 0))

    def test_the_plan_is_written_from_the_video_and_the_cut_follows_its_pictures(self):
        from test_pipeline import write_project

        root = self.root / "a"
        takes = [fake_take(f"A{k}", k) for k in range(5)]
        paths = pipeline.Paths(kb=root / "kb", footmap=root / "cache" / "footmap", sparse=root / "cache" / "sparse", beats=root / "cache" / "beats")
        old = {"version": 1, "name": "proj", "key": "proj::footage", "length_s": 1.0, "fps": 25, "sections": [{"name": "s", "slots": [{"id": "x", "length_s": 1.0, "show": "axis 4"}]}]}
        plan_p = write_project(root, takes, old)
        d = plan_p.parent
        seen: list[dict] = []
        got = pipeline.reference(d, str(self.video), paths=paths, ev=events.Events(seen.append), read=self.read)
        m = shotmap.load(plan_p)
        self.assertEqual((got["slots"], got["shots"], m["reference"]["file"], m["key"]), (3, 3, "Their Reel.mp4", "proj::footage"))
        self.assertEqual([round(s["length_s"], 1) for s in shotmap.slots(m)], [2.0, 2.0, 2.0])
        self.assertEqual((m["width"], m["height"]), (1920, 1080))  # no frame chosen: the reference's own shape
        self.assertEqual(json.loads((d / "work" / "shotmap.before-reference.json").read_text())["sections"][0]["slots"][0]["id"], "x")
        self.assertEqual(settings.load(d)["reference"], str(self.video))
        self.assertTrue(Path(m["music"]["path"]).is_file())
        self.assertIn("Their Reel.mp4", (d / "reference.md").read_text())
        self.assertIn("plan", [e.get("kind") for e in seen if e.get("event") == "artifact"])
        result = pipeline.cut(plan_p, sheets=False, sfx=False, paths=paths, ev=events.Events(seen.append), embed_texts=words)
        self.assertEqual([s["pick"]["name"] for s in result["picks"]["slots"]], ["A0.MP4", "A1.MP4", "A2.MP4"])
        self.assertEqual([round(s["start_s"], 1) for s in result["picks"]["slots"]], [0.0, 2.0, 4.0])
        self.assertTrue(any("looks like its shots" in str(e.get("text")) for e in seen))
        # again, with the frame the user chose and no video named: the kept reference, read from its cache
        settings.save(d, {**settings.load(d), "frame": "vertical"})
        pipeline.reference(d, paths=paths, ev=events.Events(seen.append), read=self.read)
        self.assertEqual((shotmap.load(plan_p)["width"], shotmap.load(plan_p)["height"]), (1080, 1920))
        self.assertTrue(any("read before" in str(e.get("text")) for e in seen))
        self.assertEqual(json.loads((d / "work" / "shotmap.before-reference.json").read_text())["sections"][0]["slots"][0]["id"], "x")

    def test_reading_the_footage_ends_with_the_plan_when_the_project_has_a_reference(self):
        d = self.root / "b" / "out" / "proj"
        d.mkdir(parents=True)
        settings.save(d, {"reference": str(self.video)})
        seen: list[dict] = []
        real = reference.read
        with mock.patch.object(reference, "read", lambda path, work, ev=None: real(path, work, ev=ev, embed=lambda im: np.tile(unit(np.ones(DIM)), (len(im), 1)),
                                                                                  embed_texts=words, detect=lambda rgb: ([], 0))):
            pipeline._plan_from_reference(d, "proj::footage", pipeline.Paths(), events.Events(seen.append))
        self.assertEqual(len(shotmap.slots(shotmap.load(d / "shotmap.json"))), 3)
        (d / "shotmap.json").write_text("{}")
        pipeline._plan_from_reference(d, "proj::footage", pipeline.Paths(), events.Events(seen.append))  # a plan is there: left alone
        self.assertEqual((d / "shotmap.json").read_text(), "{}")

    def test_what_cannot_be_done_is_said(self):
        d = self.root / "c"
        with self.assertRaisesRegex(pipeline.PipelineError, "No reference video"):
            pipeline.reference(d)
        with self.assertRaisesRegex(pipeline.PipelineError, "not there"):
            pipeline.reference(d, str(self.root / "nothing.mp4"))
        seen: list[dict] = []
        settings.save(d, {"reference": str(self.root / "nothing.mp4")})
        pipeline._plan_from_reference(d, "c::footage", pipeline.Paths(), events.Events(seen.append))
        self.assertTrue(any(e.get("event") == "warning" and "not written" in str(e.get("text")) for e in seen))


if __name__ == "__main__":
    unittest.main()
