import json
import tempfile
import unittest
from pathlib import Path

from videtor.kb import xmeml
from videtor.plan import benchmark, build, rules
from videtor.export import xmeml_writer


def entry(fid, path, duration, fps=50.0, height=2160, created="2025-09-26T10:00:00Z", used=None):
    return {
        "fid": fid, "path": path, "basename": path.rsplit("/", 1)[-1], "kind": "video", "size": 1, "inode": 1, "mtime": 0,
        "media": {"duration_s": duration, "created": created, "video": {"fps": fps, "width": 3840, "height": height}, "audio": {"channels": 2}},
        "used_by": used or {}, "duplicate_of": None,
    }


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.analysis = Path(self.tmp.name, "analysis")
        self.analysis.mkdir()
        # sharp long takes, a blurry one, a proxy, a slow-mo clip and one with an internal cut
        self.entries = [
            entry("a", "/v/Shoot/A001.mp4", 12.0, created="2025-09-26T10:03:00Z", used={"g.xml::S": [[4.0, 6.0]]}),
            entry("b", "/v/Shoot/A002.mp4", 9.0, created="2025-09-26T10:01:00Z", used={"g.xml::S": [[1.0, 3.0]]}),
            entry("c", "/v/Shoot/A003.mp4", 10.0, created="2025-09-26T10:02:00Z"),
            entry("d", "/v/Shoot/A004.mp4", 8.0, height=960, created="2025-09-26T10:04:00Z"),
            entry("e", "/v/Shoot/A005.mp4", 8.0, fps=100.0, created="2025-09-26T10:05:00Z"),
            entry("f", "/v/Shoot/A006.mp4", 0.5),  # too short to be a candidate
        ]
        sharp = {"a": 90, "b": 80, "c": 20, "d": 85, "e": 85}
        for fid, s in sharp.items():
            rec = {"fid": fid, "frames": {"sharpness": s}}
            if fid == "a":
                rec["motion"] = {"shot_boundaries": [3.0], "motion": {"median": 4.0}}
            (self.analysis / f"{fid}.json").write_text(json.dumps(rec))
        self.profile = {"shot_length_s": {"median": 1.0}, "fps": 25.0, "frame_size": [1080, 1920]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_scoring_prefers_sharp_long_full_res_normal_speed(self):
        cands = rules.candidates(self.entries, self.analysis)
        self.assertEqual([c["entry"]["fid"] for c in cands], ["a", "b", "c", "d", "e"])
        rules.score(cands)
        by = {c["entry"]["fid"]: c for c in cands}
        self.assertGreater(by["a"]["score"], by["c"]["score"])  # sharp beats blurry
        self.assertGreater(by["b"]["score"], by["d"]["score"])  # full-res beats the 960p proxy
        self.assertGreater(by["b"]["score"], by["e"]["score"])  # 100 fps is penalised
        self.assertTrue(any("lower resolution" in r for r in by["d"]["reasons"]))

    def test_plan_fills_the_target_in_shoot_order_with_reasons(self):
        shots = rules.plan(self.entries, self.analysis, self.profile, [1.0, 1.5, 2.0], target_s=4.0, seed=1, hook_first=False)
        self.assertGreaterEqual(sum(s.length_s for s in shots), 4.0)
        self.assertEqual([s.name for s in shots], sorted((s.name for s in shots), key=lambda n: next(e["media"]["created"] for e in self.entries if e["basename"] == n)))
        for s in shots:
            self.assertTrue(s.reasons)
            self.assertLessEqual(s.out_s, s.file_duration_s)
        a = next((s for s in shots if s.fid == "a"), None)
        if a:  # the slot never crosses the detected cut at 3.0 s
            self.assertTrue(a.out_s <= 3.0 or a.in_s >= 3.0, (a.in_s, a.out_s))

    def test_in_point_follows_the_habit_and_avoids_cuts(self):
        cands = rules.candidates(self.entries, self.analysis)
        a = next(c for c in cands if c["entry"]["fid"] == "a")  # 12 s take with a cut at 3.0 s
        in_s, why = rules.choose_in_point(a, 2.0, rel=0.1)  # 10% in → 1.0 s, inside the 0–3 s run
        self.assertEqual(in_s, 1.0)
        self.assertIn("uncut run 0.0–3.0", why)
        in_s, _ = rules.choose_in_point(a, 2.0, rel=0.2)  # 20% in → 2.0 s would cross the cut: moved to the 3–12 s run
        self.assertEqual(in_s, 3.0)
        c = next(c for c in cands if c["entry"]["fid"] == "c")  # no cuts known: plain habit
        in_s, why = rules.choose_in_point(c, 2.0, rel=0.25)
        self.assertEqual(in_s, 2.0)
        self.assertIn("where your cuts start", why)

    def test_speech_clip_cuts_in_before_the_longest_phrase_and_scores_higher(self):
        rec = {"fid": "c", "frames": {"sharpness": 20}, "audio": {"audio": True, "speech": True, "speech_s": 4.0, "speech_spans": [[1.0, 1.5], [4.0, 7.5]]}}
        (self.analysis / "c.json").write_text(json.dumps(rec))
        cands = rules.candidates(self.entries, self.analysis)
        c = next(c for c in cands if c["entry"]["fid"] == "c")
        in_s, why = rules.choose_in_point(c, 2.0, rel=0.5)
        self.assertEqual(in_s, 3.85)  # a breath before the 4.0–7.5 s phrase, not the habit position
        self.assertIn("longest phrase", why)
        rules.score(cands)
        blurry_talking = next(c for c in cands if c["entry"]["fid"] == "c")
        self.assertTrue(any("speaks" in r and "wanted" in r for r in blurry_talking["reasons"]))
        favored = blurry_talking["score"]
        rules.score(cands, speech_weight=-0.4)  # a music montage brief: talking clips lose
        avoided = next(c for c in cands if c["entry"]["fid"] == "c")["score"]
        self.assertAlmostEqual(favored - avoided, 0.8, places=3)

    def test_beats_put_every_cut_on_the_grid_and_the_hook_opens(self):
        grid = [round(i * 0.5, 3) for i in range(60)]  # 120 BPM; at 20 fps a beat is exactly 10 frames
        shots = rules.plan(self.entries, self.analysis, self.profile, [0.7, 1.2, 1.9], target_s=5.0, seed=3, beats=grid, hook_first=True, fps=20.0, beat_lead_s=0.0)
        t = 0.0
        for s in shots:
            t += s.length_s
            self.assertAlmostEqual((t / 0.5) - round(t / 0.5), 0.0, places=3, msg=f"cut at {t} is off the beat")
        # with a lead, every cut sits that many frames ahead of a beat, no slot collapses, and the target is reached
        lead = rules.plan(self.entries, self.analysis, self.profile, [0.3], target_s=2.0, seed=3, beats=grid, hook_first=False, fps=20.0, beat_lead_s=0.1)
        t = 0.0
        for s in lead:
            self.assertGreaterEqual(s.length_s, 0.2)
            t += s.length_s
            self.assertAlmostEqual(((t + 0.1) / 0.5) - round((t + 0.1) / 0.5), 0.0, places=3, msg=f"cut at {t} is not 0.1 s ahead of a beat")
        self.assertGreaterEqual(t, 2.0 - 0.5)  # five clips at one beat each can just cover a 2 s target
        self.assertEqual(shots[0].score, max(s.score for s in shots))
        self.assertIn("opens the piece", shots[0].reasons[0])
        self.assertTrue(all(any("beat" in r for r in s.reasons) for s in shots))

    def test_glimpse_opening_leads_with_part_of_the_subject_then_details_then_the_reveal(self):
        def features(fid, scale, faces_max=0, face_area=0.0, hands=0.0, tags=None):
            path = self.analysis / f"{fid}.json"
            rec = json.loads(path.read_text())
            scores = {"face in close-up": 0.02, "hands in close-up": 0.02, "clothing and apparel detail": 0.02, "someone looking at the camera": 0.02, "products on a table": 0.02}
            scores.update(tags or {})
            rec["faces"] = {"frames": 3, "faces_max": faces_max, "face_area": face_area, "partial_share": 0.0, "hands_share": hands, "scale": scale, "face_frames": 1.0 if faces_max else 0.0}
            rec["tags"] = {"frames": 3, "tags": scores, "scores": scores}
            path.write_text(json.dumps(rec))

        features("d", "close", faces_max=1, face_area=0.1, tags={"face in close-up": 0.6})  # the glimpse
        features("c", "detail", hands=0.8, tags={"hands in close-up": 0.7})  # tease
        features("e", "detail", hands=0.3, tags={"clothing and apparel detail": 0.6})  # tease
        features("b", "medium", faces_max=1, face_area=0.02, tags={"someone looking at the camera": 0.6})  # the zoom-out
        features("a", "wide", faces_max=4, face_area=0.003, tags={"products on a table": 0.5})
        shots = rules.plan(self.entries, self.analysis, self.profile, [2.0], target_s=10.0, seed=0, hook_first=False, opening="glimpse")
        self.assertEqual(shots[0].fid, "d")
        self.assertEqual({s.fid for s in shots[1:3]}, {"c", "e"})
        self.assertEqual(shots[3].fid, "b")
        self.assertTrue(shots[0].reasons[0].startswith("opening beat 1 (glimpse)"))
        self.assertTrue(shots[3].reasons[0].startswith("opening beat 4 (reveal)"))

    def test_music_goes_on_its_own_track(self):
        shots = rules.plan(self.entries, self.analysis, self.profile, [1.0], target_s=3.0, seed=0)
        tl = build.build_timeline(shots, "Cut", 25.0, {"path": "/v/music/track.wav", "duration_s": 120.0})
        audio = tl.audio_tracks()
        self.assertEqual(len(audio), 1)
        music = list(audio[0].find_clips())[0]
        self.assertEqual(music.name, "track.wav")
        self.assertAlmostEqual(music.source_range.duration.to_seconds(), sum(s.length_s for s in shots), delta=0.05)

    def test_timeline_and_xml_and_benchmark(self):
        shots = rules.plan(self.entries, self.analysis, self.profile, [2.0], target_s=6.0, seed=0)
        tl = build.build_timeline(shots, "Test cut", 25.0)
        self.assertEqual(len(list(tl.video_tracks()[0].find_clips())), len(shots))
        xml_path = Path(self.tmp.name, "cut.xml")
        xmeml_writer.write_xmeml(tl, xml_path, 25.0, 1080, 1920)
        seq = xmeml.load(xml_path)[0]
        self.assertEqual(len([t for t in seq.tracks if t.media == "video"][0].clips), len(shots))

        index = {"files": self.entries, "roots": {"g.xml::S": ["/v/Shoot"]}}
        result = benchmark.score_plan(shots, index, "g.xml::S", {"shot_length_s": {"count": 40, "median": 0.9}, "duration_s": 44})
        self.assertEqual(result["clips"]["real"], 2)
        self.assertIn(result["clips"]["precision"], [None, *[round(x / len({s.fid for s in shots}), 3) for x in range(0, 3)]])
        self.assertEqual(result["pace"]["real_shots"], 40)
        data = rules.plan_to_json(shots, {"name": "Test cut"})
        self.assertEqual([s.fid for s in rules.shots_from_json(data)], [s.fid for s in shots])


if __name__ == "__main__":
    unittest.main()
