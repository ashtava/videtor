"""Shot map timing, slot filling and the cut built from picks, on synthetic footage maps."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from videtor.export import xmeml_writer as w
from videtor.kb import xmeml
from videtor.plan import fill, second_look, shotmap
from videtor.plan.build import build_from_picks

DIM = 8


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def fake_take(name, axis, n=20, fps=100.0, faces=0, hands=1, moving=False, rotation=0):
    """A take whose every sample looks like basis vector `axis` (so text axis k matches it)."""
    emb = np.tile(unit(np.eye(DIM)[axis] + 0.05), (n, 1)).astype(np.float16)
    cam = np.full(n, 0.2 if moving else 0.0, np.float32)
    a = {
        "t": (np.arange(n) + 0.5) / 2, "emb": emb, "sharp": np.full(n, 200.0, np.float32), "bright": np.full(n, 120.0, np.float32),
        "faces": np.full(n, faces, np.int8), "face_area": np.full(n, 0.05 if faces else 0.0, np.float32),
        "face_box": np.full((n, 4), np.nan, np.float32), "partial": np.zeros(n, bool), "hands": np.full(n, hands, np.int8),
        "cam_dx": cam, "cam_dy": np.zeros(n, np.float32), "cam_zoom": np.zeros(n, np.float32), "cam_rot": np.zeros(n, np.float32),
        "shake": np.full(n, 0.001, np.float32), "subject_motion": np.zeros(n, np.float32), "jump": np.zeros(n, bool),
    }
    return fill.Take(fid=name, path=f"/Volumes/T/{name}.MP4", name=f"{name}.MP4", fps=fps, duration_s=n / 2, width=3840, height=2160,
                     audio_channels=2, display_w=2160, display_h=3840, rotation=rotation, a=a)


def fake_embed(texts):
    """Text "axis k" embeds as basis vector k, matching fake_take(axis=k)."""
    return np.stack([unit(np.eye(DIM)[int(t.split()[-1])] + 0.05) for t in texts])


MAP = {
    "version": 1, "name": "test-cut", "key": "T.xml::Seq", "length_s": 4.0, "fps": 25, "width": 1080, "height": 1920, "beat_lead_s": 0.12,
    "sections": [
        {"name": "details", "slots": [
            {"id": "d1", "role": "detail", "beats": 2, "show": "axis 0", "speed": 0.5},
            {"id": "d2", "role": "detail", "beats": 2, "show": ["axis 1", "axis 2"], "camera": "moving"},
        ]},
        {"name": "reveal", "slots": [{"id": "r1", "role": "reveal", "beats": 4, "show": "axis 3", "face": "full"}]},
    ],
}


class CropShapeTests(unittest.TestCase):
    def test_a_take_shaped_against_the_frame_is_flagged_both_ways(self):
        portrait, landscape = fake_take("P", 0), fake_take("L", 0)
        landscape.display_w, landscape.display_h = 3840, 2160
        self.assertTrue(fill.is_landscape(landscape, 1080, 1920))  # landscape in a vertical cut
        self.assertFalse(fill.is_landscape(portrait, 1080, 1920))
        self.assertTrue(fill.is_landscape(portrait, 1920, 1080))  # portrait in a horizontal cut
        self.assertFalse(fill.is_landscape(landscape, 1920, 1080))


class TimingTests(unittest.TestCase):
    def test_slots_end_a_lead_before_their_closing_beat_and_the_last_ends_at_length(self):
        beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
        times = shotmap.timing(MAP, beats, 25.0)
        self.assertEqual([round(s, 2) for s, _ in times], [0.0, 0.88, 1.88])  # beat 2 at 1.0 s - 0.12, beat 4 at 2.0 s - 0.12
        self.assertAlmostEqual(sum(l for _, l in times), 4.0, places=6)

    def test_half_beats_cut_between_two_beats_of_the_grid(self):
        m = {**MAP, "sections": [{"name": "s", "slots": [{"id": "a", "beats": 0.5, "show": "x"}, {"id": "b", "beats": 1.5, "show": "x"},
                                                        {"id": "c", "beats": 1, "show": "x"}]}]}
        times = shotmap.timing(m, [0.0, 0.6, 1.2, 1.8, 2.4], 25.0)
        self.assertEqual([round(s, 2) for s, _ in times], [0.0, 0.16, 1.08])  # 0.3 - 0.12 lead (4.5 frames, rounds to 4), then beat 2 at 1.2 - 0.12

    def test_validate_names_the_problem(self):
        bad = {**MAP, "sections": [{"name": "x", "slots": [{"id": "a", "show": "", "beats": 1, "camera": "fast"}]}]}
        problems = " ".join(shotmap.validate(bad))
        self.assertIn("show is empty", problems)
        self.assertIn("camera must be one of", problems)


class FillTests(unittest.TestCase):
    def setUp(self):
        self.takes = [fake_take("A0", 0), fake_take("A1", 1, moving=True), fake_take("A2", 2), fake_take("A3", 3, faces=1),
                      fake_take("B0", 0, fps=25.0)]
        times = shotmap.timing(MAP, [i * 0.5 for i in range(10)], 25.0)
        self.picks = fill.fill(MAP, self.takes, times, fake_embed, habits={"M2U": (-90.0, 188.0)})

    def test_each_slot_gets_the_take_that_shows_it_and_no_take_twice(self):
        picks = {s["id"]: s["pick"]["name"] for s in self.picks["slots"]}
        self.assertEqual(picks["d1"], "A0.MP4")
        self.assertEqual(picks["d2"], "A1.MP4")  # matches "axis 1" and the camera moves
        self.assertEqual(picks["r1"], "A3.MP4")
        self.assertEqual(len(set(picks.values())), 3)

    def test_slow_motion_only_on_high_frame_rate_takes_and_out_follows_speed(self):
        d1 = self.picks["slots"][0]
        self.assertEqual(d1["pick"]["speed"], 0.5)  # A0 is 100 fps
        self.assertAlmostEqual(d1["pick"]["out_s"] - d1["pick"]["in_s"], d1["length_s"] * 0.5, places=3)
        b0 = next(a for a in d1["alternates"] if a["name"] == "B0.MP4")  # the 25 fps twin plays at full speed
        self.assertEqual(b0["speed"], 1.0)

    def test_runners_up_prefer_takes_no_slot_uses(self):
        d1 = self.picks["slots"][0]
        self.assertEqual(d1["alternates"][0]["name"], "B0.MP4")

    def test_skip_and_a_slots_own_takes_limit_the_choice(self):
        m = {**MAP, "skip": ["*/A1.MP4"], "sections": [{"name": "s", "slots": [
            {"id": "s1", "beats": 2, "show": "axis 1"}, {"id": "s2", "beats": 2, "show": "axis 0", "takes": ["B*"]}]}]}
        times = shotmap.timing(m, [i * 0.5 for i in range(10)], 25.0)
        picks = fill.fill(m, self.takes, times, fake_embed)
        seen = [s["pick"]["name"] for s in picks["slots"]] + [a["name"] for s in picks["slots"] for a in s["alternates"]]
        self.assertNotIn("A1.MP4", seen)
        self.assertEqual(picks["slots"][1]["pick"]["name"], "B0.MP4")  # A0 shows it too, but the slot names B takes only
        self.assertEqual([a["name"] for a in picks["slots"][1]["alternates"]], [])
        moment = {**m, "length_s": 1.5, "skip": [], "sections": [{"name": "s", "slots": [{"id": "s1", "beats": 2, "show": "axis 0", "takes": ["A0*@6.0-8.0"]}]}]}
        pick = fill.fill(moment, self.takes, shotmap.timing(moment, [i * 0.5 for i in range(10)], 25.0), fake_embed)["slots"][0]["pick"]
        self.assertTrue(6.0 - 0.25 <= pick["in_s"] and pick["out_s"] <= 8.0 + 0.25)  # inside the named moment
        self.assertEqual(fill.allowed_span(["X*", "A0*@1-2.5"], "A0.MP4"), (1.0, 2.5))
        self.assertIsNone(fill.allowed_span(["X*"], "A0.MP4"))

    def test_framing_fills_the_frame_or_follows_your_habit(self):
        self.assertEqual(self.picks["slots"][0]["pick"]["motion"]["scale"], 50.0)  # 2160x3840 portrait in 1080x1920
        cam = fake_take("M2U00413", 0, fps=25.0)
        cam.name = "M2U00413.MPG"
        self.assertEqual(fill.framing(cam, 1080, 1920, {"M2U": (-90.0, 188.0)}), {"scale": 188.0, "rotation": -90.0, "center": [0.0, 0.0]})

    def test_a_takes_own_detected_rotation_beats_its_cameras_habit(self):
        flipped = fake_take("C3920", 0, rotation=180)
        flipped.name = "C3920.MP4"
        m = fill.framing(flipped, 1080, 1920, {"C39": (0.0, 50.0)})
        self.assertEqual((m["rotation"], m["scale"]), (180.0, 50.0))  # turned, and still filled
        upright = fake_take("C3953", 0)
        upright.name = "C3953.MP4"
        self.assertEqual(fill.framing(upright, 1080, 1920, {"C39": (0.0, 50.0)})["rotation"], 0.0)

    def test_centre_offset_matches_premieres_own_positions(self):
        # C3997 in the user's Guru edit: centre (-0.00546875, -0.1) is stored by Premiere as position 0.480556 : 0.3875
        dx, dy = (0.480556 - 0.5) * 1080, (0.3875 - 0.5) * 1920
        self.assertEqual(fill.centre_offset(round(dx, 1), round(dy, 1), 1080, 1920), [-0.005469, -0.1])

    def test_landscape_take_slides_to_keep_the_face_in_a_vertical_frame(self):
        wide = fake_take("L1", 0, faces=1)
        wide.display_w, wide.display_h = 3840, 2160
        wide.a["face_box"][:] = [0.65, 0.3, 0.75, 0.5]  # face right of centre
        m = fill.framing(wide, 1080, 1920, window=(0, 4))
        self.assertEqual(m["scale"], 88.9)  # fills the frame's height
        sw = 3840 * 0.889
        self.assertAlmostEqual(m["center"][0], round((0.5 - 0.7) * sw / 3840, 6), places=5)  # slides left
        self.assertEqual(m["center"][1], 0.0)  # height fits exactly: no vertical move
        far = fake_take("L2", 0, faces=1)
        far.display_w, far.display_h = 3840, 2160
        far.a["face_box"][:] = [0.97, 0.3, 0.99, 0.5]  # at the edge: clamped so no black edge shows
        self.assertAlmostEqual(fill.framing(far, 1080, 1920, window=(0, 4))["center"][0], -((sw - 1080) / 2) / 3840, places=5)

    def test_second_look_swaps_in_a_runner_up_and_keeps_the_old_pick(self):
        old = self.picks["slots"][0]["pick"]["name"]
        second_look.apply(self.picks, {"d1": 1}, {"d1": "sharper"})
        slot = self.picks["slots"][0]
        self.assertEqual(slot["pick"]["name"], "B0.MP4")
        self.assertEqual(slot["alternates"][0]["name"], old)
        self.assertEqual(slot["second_look"], {"chose": 1, "why": "sharper"})


    def test_second_look_can_name_the_runner_up_by_its_take(self):
        second_look.apply(self.picks, {"d1": "b0"})
        self.assertEqual(self.picks["slots"][0]["pick"]["name"], "B0.MP4")
        with self.assertRaises(ValueError):
            second_look.apply(self.picks, {"d1": "nothing-like-it"})

    def test_second_look_can_borrow_a_neighbouring_slots_runner_up(self):
        lent = self.picks["slots"][0]["alternates"][0]
        n_before = len(self.picks["slots"][0]["alternates"])
        second_look.apply(self.picks, {"r1": ("d1", 1)})
        r1 = self.picks["slots"][2]
        self.assertEqual(r1["pick"]["name"], lent["name"])
        self.assertAlmostEqual(r1["pick"]["out_s"] - r1["pick"]["in_s"], r1["length_s"] * r1["pick"]["speed"], places=3)
        self.assertEqual(len(self.picks["slots"][0]["alternates"]), n_before - 1)  # no repeat on the alternate tracks
        self.assertEqual(r1["second_look"]["borrowed_from"], "d1")
        with self.assertRaises(ValueError):
            second_look.apply(self.picks, {"nope": 1})


class TasteTests(unittest.TestCase):
    def test_fixes_that_always_choose_hands_teach_a_hands_weight_and_change_the_pick(self):
        from videtor.plan import taste

        # two takes that match a slot equally; the editor always chose the one with hands in frame
        with_hands, without = fake_take("H", 0, hands=1), fake_take("N", 0, hands=0)
        m = {**MAP, "sections": [{"name": "s", "slots": [{"id": "s1", "beats": 2, "show": "axis 0"}]}]}
        times = shotmap.timing(m, [i * 0.5 for i in range(10)], 25.0)
        picks = fill.fill(m, [without, with_hands], times, fake_embed)
        # whichever was picked, the editor's choice is the take with hands
        chosen = {"fid": "H", "in_s": 1.0, "out_s": 2.0}
        picks["slots"][0]["pick"] = {**picks["slots"][0]["pick"], "fid": "N", "in_s": 1.0, "out_s": 2.0}
        fb = {"slots": [{"id": "s1", "status": "swapped", "visible": chosen}]}
        diffs = taste.pairs(fb, picks, {"H": with_hands, "N": without}, fill.Pool([without, with_hands]), fake_embed) * 6
        w = dict(zip(fill.TERMS, taste.fit(diffs)))
        self.assertGreater(w["hands"], 0.1)
        self.assertAlmostEqual(w["match"], 0.0, places=6)  # both matched alike: nothing learnt about matching
        # with the learnt taste, a take with hands now beats an otherwise better-matching one without
        slight = fake_take("N2", 0, hands=0)
        slight.a["sharp"][:] = 260.0
        plain = fill.fill(m, [slight, with_hands], times, fake_embed)
        tasted = fill.fill(m, [slight, with_hands], times, fake_embed, taste={"weights": {"hands": 1.0}})
        self.assertEqual(plain["slots"][0]["pick"]["name"], "N2.MP4")
        self.assertEqual(tasted["slots"][0]["pick"]["name"], "H.MP4")


class MatchCutTests(unittest.TestCase):
    def test_a_match_section_picks_the_take_whose_frame_lines_up_with_the_cut_before(self):
        from PIL import Image, ImageDraw

        # one take for the first slot (a board on the left); two for the second: a sharper one with its card on
        # the right, and one with its card on the left, where the board was
        board, right, left = fake_take("BOARD", 0), fake_take("RIGHT", 1), fake_take("LEFT", 1)
        right.a["sharp"][:] = 260.0
        sec = {"name": "boards", "match": True, "slots": [{"id": "b1", "beats": 2, "show": "axis 0"}, {"id": "b2", "beats": 2, "show": "axis 1"}]}
        m = {**MAP, "sections": [sec]}
        times = shotmap.timing(m, [i * 0.5 for i in range(10)], 25.0)
        with tempfile.TemporaryDirectory() as d:
            for take, x in ((board, 40), (right, 330), (left, 40)):
                for i in range(len(take.a["t"])):
                    im = Image.new("RGB", (480, 270), (30, 30, 30))
                    ImageDraw.Draw(im).rectangle([x, 70, x + 110, 200], fill=(235, 235, 235))
                    im.save(Path(d) / f"{take.fid}_{i:04d}.jpg")
            plain = fill.fill({**m, "sections": [{**sec, "match": False}]}, [board, right, left], times, fake_embed, frames_dir=Path(d))
            matched = fill.fill(m, [board, right, left], times, fake_embed, frames_dir=Path(d))
            self.assertIsNone(second_look.match_sheet(plain, Path(d), Path(d) / "plain.jpg"))
            self.assertTrue(second_look.match_sheet(matched, Path(d), Path(d) / "pairs.jpg").exists())
        self.assertEqual(plain["slots"][1]["pick"]["name"], "RIGHT.MP4")  # the sharper take wins on its own
        self.assertEqual(matched["slots"][1]["pick"]["name"], "LEFT.MP4")  # the card where the board was
        self.assertGreater(matched["slots"][1]["pick"]["match"], 0.7)
        self.assertNotIn("match", matched["slots"][0]["pick"])
        self.assertEqual([s.get("match_cut", False) for s in matched["slots"]], [False, True])


class CutTests(unittest.TestCase):
    def test_xml_has_picks_on_v1_alternates_switched_off_motion_and_a_music_fade(self):
        takes = [fake_take(f"T{i}", i % 4, faces=1 if i % 4 == 3 else 0) for i in range(8)]
        times = shotmap.timing(MAP, [i * 0.5 for i in range(10)], 25.0)
        picks = fill.fill(MAP, takes, times, fake_embed)
        music = {"path": "/Music/track.wav", "duration_s": 120.0, "in_s": 30.0, "fade_out_s": 1.0}
        with tempfile.TemporaryDirectory() as tmp:
            path = w.write_xmeml(build_from_picks(picks, music), Path(tmp, "cut.xml"), fps=25, width=1080, height=1920)
            text = path.read_text()
            seq = xmeml.load(path)[0]
        video = [t for t in seq.tracks if t.media == "video"]
        audio = [t for t in seq.tracks if t.media == "audio"]
        self.assertEqual(len(video), 3)
        self.assertTrue(all(c.enabled for c in video[0].clips))
        self.assertTrue(video[1].clips and not any(c.enabled for c in video[1].clips + video[2].clips))
        self.assertEqual(len(audio), 1)  # no camera audio: the music is A1
        self.assertEqual(audio[0].clips[0].source_in, 30 * 25)
        self.assertIn("<alignment>end-black</alignment>", text)
        self.assertIn("<effectid>basic</effectid>", text)
        self.assertEqual(seq.duration, 100)


if __name__ == "__main__":
    unittest.main()
