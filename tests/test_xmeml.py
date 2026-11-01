import json
import tempfile
import unittest
from pathlib import Path

from videtor.kb import metrics, profile, xmeml

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "minimal.xml"
FPS = 24000 / 1001


class LoadTests(unittest.TestCase):
    def test_bin_references_and_nested_sequences_are_left_out_by_default(self):
        seqs = xmeml.load(FIXTURE)
        self.assertEqual([(s.name, s.role) for s in seqs], [("Fixture Seq", "main")])

    def test_nested_sequences_can_be_included(self):
        seqs = xmeml.load(FIXTURE, include_nested=True)
        self.assertEqual([(s.id, s.role) for s in seqs], [("sequence-1", "main"), ("sequence-2", "nested")])


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.seq = xmeml.load(FIXTURE)[0]
        self.tracks = {t.name: t for t in self.seq.tracks}

    def test_sequence_header(self):
        self.assertAlmostEqual(self.seq.fps, 23.976, places=3)
        self.assertEqual((self.seq.width, self.seq.height), (1920, 1080))
        self.assertAlmostEqual(self.seq.duration_s, 240 / FPS, places=3)
        self.assertEqual(list(self.tracks), ["V1", "V2", "V3", "V4", "A1", "A2", "A3"])

    def test_file_registry_resolves_by_id_decodes_url_and_keeps_luts(self):
        v1 = self.tracks["V1"].clips[0]
        a1 = self.tracks["A1"].clips[0]
        self.assertEqual(v1.file_path, "/Volumes/RAW/Day 1/A001.mov")
        self.assertEqual(a1.file_path, "/Volumes/RAW/Day 1/A001.mov")  # <file id="file-1"/> only
        self.assertEqual(v1.luts, ["SLog3_to_Rec709.cube"])
        self.assertAlmostEqual(a1.file_fps, 23.976, places=3)  # carried through the id reference
        self.assertIsNone(self.tracks["V2"].clips[0].file_fps)  # B001 has no <rate> in its <file>
        self.assertAlmostEqual(v1.clip_fps, 23.976, places=3)  # the clipitem's own rate: the unit of in/out
        self.assertIsNone(self.tracks["V2"].clips[0].clip_fps)  # no clipitem <rate>: in/out count at the sequence rate

    def test_clip_kinds(self):
        kinds = {c.name: c.kind for t in self.seq.tracks for c in t.clips}
        self.assertEqual(kinds["A001.mov"], "clip")
        self.assertEqual(kinds["Nested Sequence 1"], "nested")
        self.assertEqual(kinds["Lower Third"], "generator")
        self.assertEqual(kinds["Adjustment Layer"], "adjustment")

    def test_speed_links_and_labels(self):
        c1, c2 = self.tracks["V1"].clips
        self.assertEqual(c2.speed, 2.0)
        self.assertEqual(metrics.clip_speed(c2), (2.0, False))  # Time Remap speed=200, not a ramp
        self.assertEqual(c1.linked, ["clipitem-3"])  # own id is dropped
        self.assertEqual(c1.label, "Iris")

    def test_transitions_on_both_media_types(self):
        v = self.tracks["V1"].transitions[0]
        a = self.tracks["A1"].transitions[0]
        self.assertEqual((v.name, v.frames, v.alignment), ("Cross Dissolve", 24, "center"))
        self.assertEqual((a.name, a.frames), ("Cross Fade (+3dB)", 12))

    def test_gain_and_levels_combine_into_db(self):
        camera = self.tracks["A1"].clips[0]
        music = self.tracks["A2"].clips[0]
        self.assertEqual(metrics.clip_gain_db(camera), -3.0)  # Gain(dB) -3 + level 1.0
        self.assertAlmostEqual(metrics.clip_gain_db(music), -12.0, places=1)
        levels = next(e for e in music.effects if e.name == "Audio Levels")
        self.assertEqual([k.when for k in levels.keyframes["level"]], [0, 96])


class MetricsTests(unittest.TestCase):
    def setUp(self):
        mains, nested = xmeml.load_all(FIXTURE)
        self.m = metrics.sequence_metrics(mains[0], nested)

    def test_adjustment_layer_does_not_win_primary_track(self):
        self.assertEqual(self.m["primary_video_track"], "V1")
        self.assertEqual(self.m["orientation"], "horizontal")

    def test_nested_sequence_is_opened_into_its_shots(self):
        # V1: 96 + 144 frames; V2: 48 (B001) + the nest's two 12-frame shots; disabled clip, generator and adjustment excluded
        self.assertEqual(self.m["nested"], {"clips": 1, "expanded": 1})
        self.assertEqual(self.m["shot_length_s"]["count"], 5)
        self.assertAlmostEqual(self.m["shot_length_s"]["median"], 48 / FPS, places=2)
        self.assertEqual(self.m["broll"]["count"], 3)
        self.assertEqual(self.m["cuts_s"], [round(f / FPS, 3) for f in (96, 120, 168, 180, 192)])
        self.assertAlmostEqual(self.m["cuts_per_min"], 5 / (240 / FPS / 60), places=1)
        self.assertEqual(self.m["layers"], {"median": 1, "p90": 2, "max": 2})

    def test_unknown_nest_stays_one_shot(self):
        m = metrics.sequence_metrics(xmeml.load(FIXTURE)[0])  # no registry passed
        self.assertEqual(m["nested"], {"clips": 1, "expanded": 0})
        self.assertEqual(m["shot_length_s"]["count"], 4)

    def test_transitions_effects_speed(self):
        self.assertEqual(self.m["transitions"]["by_name"], {"Cross Dissolve": 1})
        self.assertEqual(self.m["transitions"]["share_of_edits"], 0.2)
        self.assertEqual(self.m["audio_transitions"]["by_name"], {"Cross Fade (+3dB)": 1})
        self.assertEqual(self.m["audio_transitions"]["frames"]["median"], 12)
        self.assertEqual(self.m["effects_seen"], {"Lumetri Color": 1})  # fixed effects excluded
        self.assertEqual(self.m["speed_changes"], {"share": 0.2, "ramp_share": 0.0, "values": [2.0]})

    def test_graphics_and_adjustment_effects(self):
        g = self.m["graphics"]
        self.assertEqual((g["adjustment"], g["graphic"], g["matte"]), (1, 1, 0))
        self.assertEqual(g["adjustment_effects"], {"Gaussian Blur": 1})  # Vector Motion is fixed
        self.assertAlmostEqual(g["first_title_s"], 48 / FPS, places=2)

    def test_audio_roles_levels_and_effects(self):
        a1, a2, a3 = self.m["audio_tracks"]
        self.assertEqual([a["role"] for a in (a1, a2, a3)], ["camera", "bed", "sfx"])
        self.assertEqual(a1["level_db"]["median"], -3.0)
        self.assertAlmostEqual(a2["level_db"]["median"], -12.0, places=1)
        self.assertEqual(a2["keyframed_share"], 1.0)
        self.assertEqual(a3["level_db"]["median"], 0.0)  # untouched clips sit at unity
        self.assertIsNone(a3["level_db_adjusted"])
        self.assertEqual(a3["effects"], {"Low Pass Filter": 1})
        self.assertEqual(self.m["audio_effects_seen"], {"Low Pass Filter": 1})

    def test_labels_luts_sources_markers(self):
        self.assertEqual(self.m["labels"]["video"], {"Iris": 1})
        self.assertEqual(self.m["luts_seen"], {"SLog3_to_Rec709.cube": 1})
        self.assertIn("/Volumes/SFX/hit.wav", self.m["sources"])
        self.assertEqual(self.m["markers"], 1)


class TransitionTests(unittest.TestCase):
    """Premiere writes start/end -1 for a clip under a transition; the reader gives it its real span."""

    def setUp(self):
        self.tracks = {t.name: t for t in xmeml.load(FIXTURES / "transitions.xml")[0].tracks}

    @staticmethod
    def spans(track: xmeml.Track) -> list[tuple]:
        return [(c.name, c.start, c.end, c.source_in, c.source_out) for c in track.clips]

    def test_cut_points_from_cut_point_ticks_or_alignment(self):
        v1 = self.tracks["V1"].transitions
        self.assertEqual([(t.cut_point, t.is_fade) for t in v1], [(0, True), (50, False), (110, True)])
        self.assertEqual(self.tracks["A1"].transitions[0].cut_point, 100)  # centred, no cutPointTicks

    def test_dissolves_move_in_out_to_the_cut_and_fades_do_not(self):
        # A: fades in at 0 and dissolves out at 50, its out moves back from 55; B: in moves on from 45 to the
        # cut, out stays where the fade to black (105-111, cut at 110) ends its media
        self.assertEqual(self.spans(self.tracks["V1"]), [("A.mov", 0, 50, 100, 150), ("B.mov", 50, 110, 205, 265)])
        b = self.tracks["V1"].clips[1]
        self.assertEqual((b.source_seconds(b.source_in, 25.0), b.source_seconds(b.source_out, 25.0)), (4.1, 5.3))  # 50%

    def test_audio_crossfades_resolve_the_same_way(self):
        self.assertEqual(self.spans(self.tracks["A1"]), [("m.wav", 0, 100, 0, 100), ("m.wav", 100, 200, 300, 400)])

    def test_track_output_switch(self):
        self.assertEqual([(n, t.enabled) for n, t in self.tracks.items()], [("V1", True), ("V2", False), ("A1", True)])
        self.assertTrue(self.tracks["V2"].clips[0].enabled)  # the clip keeps its own flag


class FlattenTests(unittest.TestCase):
    def test_partial_nest_at_double_speed_maps_onto_the_parent_timeline(self):
        rate = xmeml.Rate(25, False)
        inner_clips = [
            xmeml.Clip("n1", "N1.mov", "V1", 0, 50, 0, 50, True, "clip", file_path="/x/N1.mov"),
            xmeml.Clip("n2", "N2.mov", "V1", 50, 100, 500, 550, True, "clip", file_path="/x/N2.mov"),
        ]
        inner = xmeml.Sequence("seq-2", "Nest", "f.xml", rate, 100, None, None, "nested", [xmeml.Track("V1", "video", inner_clips)])
        # parent plays inner frames 40–80 (40 frames) in 20 timeline frames at 1000–1020: double speed
        nested_clip = xmeml.Clip("p", "Nest", "V3", 1000, 1020, 40, 80, True, "nested", nested_id="seq-2")
        parent = xmeml.Sequence("seq-1", "Main", "f.xml", rate, 1020, None, None, "main", [xmeml.Track("V3", "video", [nested_clip])])
        flat = metrics.flatten_footage(parent, {"seq-2": inner})
        self.assertEqual([(c.name, c.track, c.start, c.end, c.source_in, c.source_out) for c in flat],
                         [("N1.mov", "V3", 1000, 1005, 40, 50), ("N2.mov", "V3", 1005, 1020, 500, 530)])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.m = metrics.sequence_metrics(xmeml.load(FIXTURE)[0])

    def test_roll_up_groups_by_manifest_type(self):
        manifest = {"default": "unsorted", "types": {self.m["key"]: "brand-film"}}
        profiles = profile.roll_up([self.m], manifest)
        self.assertEqual(list(profiles), ["brand-film"])
        p = profiles["brand-film"]
        self.assertEqual(p["learned_from"], 1)
        self.assertEqual(p["orientation"], "horizontal")
        self.assertEqual(p["transitions"]["by_name"], {"Cross Dissolve": 1.0})
        self.assertEqual(p["audio_db"], {"camera": -3.0, "bed": -12.0, "sfx": 0.0})
        self.assertEqual(p["audio_db_adjusted"], {"camera": -3.0, "bed": -12.0, "sfx": None})
        self.assertEqual(p["layers"]["median"], 1)
        self.assertEqual(p["adjustment_effects"], {"Gaussian Blur": 1})

    def test_dedupe_drops_the_same_sequence_exported_twice(self):
        twin = dict(self.m, key="copy.xml::Fixture Seq")
        unique, dupes = profile.dedupe([self.m, twin])
        self.assertEqual(len(unique), 1)
        self.assertEqual(dupes, [("copy.xml::Fixture Seq", self.m["key"])])

    def test_write_profiles_round_trips_and_removes_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            (kb / "profile").mkdir()
            (kb / "profile" / "old.json").write_text("{}")
            written = profile.write_profiles(kb, profile.roll_up([self.m], profile.load_manifest(kb)))
            self.assertEqual([p.name for p in written], ["unsorted.json"])
            self.assertEqual(sorted(p.name for p in (kb / "profile").glob("*.json")), ["unsorted.json"])
            self.assertEqual(json.loads(written[0].read_text())["learned_from"], 1)


if __name__ == "__main__":
    unittest.main()
