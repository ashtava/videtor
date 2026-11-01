"""Steadying with Motion keyframes (plan/stabilize.py) and bytes: a person's own sound, the music dipped under it."""

import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from videtor.export import xmeml_writer as w
from videtor.plan import fill, shotmap, stabilize
from videtor.plan.build import build_from_picks, music_levels
from tests.test_fill import MAP, fake_embed, fake_take


def texture(h=360, w=640, seed=3):
    """A frame with corners to track: smoothed noise plus a few bright blocks."""
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.random((h * 2, w * 2)) * 255, 2.0)
    for _ in range(40):
        y, x = rng.integers(0, h * 2 - 20), rng.integers(0, w * 2 - 20)
        img[y : y + 14, x : x + 14] = rng.choice([20, 235])
    return img


def shaken(n=40, h=360, w=640, roll_deg=0.0):
    """n frames of one picture shaken by known shifts (and a steady roll), with the shifts that made them."""
    import cv2

    big = texture(h, w)
    rng = np.random.default_rng(7)
    shifts = np.cumsum(rng.normal(0, 2.0, (n, 2)), axis=0)
    shifts -= shifts[0]
    frames = []
    for i, (sx, sy) in enumerate(shifts):
        a = math.radians(roll_deg * i)
        m = cv2.getRotationMatrix2D((w, h), -math.degrees(a), 1.0)  # cv2 turns counter-clockwise for a positive angle
        m[:, 2] += (sx - w / 2, sy - h / 2)
        frames.append(cv2.warpAffine(big.astype(np.float32), m, (w, h)).clip(0, 255).astype(np.uint8))
    return np.stack(frames), shifts


class PathTests(unittest.TestCase):
    def test_the_measured_path_follows_the_shake(self):
        frames, shifts = shaken()
        p = stabilize.path_of(frames)
        self.assertLess(float(np.abs(p[:, :2] - shifts).max()), 0.6)  # within a fraction of a pixel
        self.assertLess(float(np.abs(p[:, 2]).max()), 0.002)

    def test_a_clockwise_roll_measures_positive_as_premiere_turns(self):
        frames, _ = shaken(n=12, roll_deg=0.3)
        p = stabilize.path_of(frames)
        self.assertAlmostEqual(math.degrees(p[-1, 2]), 0.3 * 11, delta=0.25)

    def test_the_correction_keeps_the_move_and_cancels_the_shake(self):
        t = np.arange(100) / 50.0
        move = np.stack([t * 120, np.zeros_like(t), np.zeros_like(t)], axis=1)  # a pan, 120 px a second
        shake = np.stack([np.sin(t * 2 * math.pi * 6) * 4, np.cos(t * 2 * math.pi * 5) * 3, np.zeros_like(t)], axis=1)
        c = stabilize.correction(move + shake, 50.0)
        mid = slice(20, 80)
        self.assertLess(float(np.abs(c[mid, :2] + shake[mid, :2]).max()), 1.5)  # what is left of the shake is small, the pan kept

    def test_a_turned_take_is_measured_upright(self):
        self.assertEqual(stabilize._upright_size(2160, 3840), (360, 640))  # the map's display size is already upright
        self.assertEqual(stabilize._upright_size(3840, 2160), (640, 360))

    def test_the_zoom_hides_the_edges_a_shift_and_a_turn_open(self):
        self.assertAlmostEqual(stabilize.zoom_needed(np.zeros(3), np.zeros(3), np.zeros(3), 1080, 1920, 1080, 1920), 1.0)
        z = stabilize.zoom_needed(np.array([20.0]), np.array([0.0]), np.array([0.0]), 1080, 1920, 1080, 1920)
        self.assertAlmostEqual(z, (540 + 20) / 540, places=4)
        self.assertGreater(stabilize.zoom_needed(np.zeros(1), np.zeros(1), np.array([1.0]), 1080, 1920, 1080, 1920), 1.01)


class KeysTests(unittest.TestCase):
    def test_each_timeline_frame_takes_the_source_frame_premiere_shows(self):
        stab = {"from_frame": 90, "file_fps": 50.0, "dx": list(range(60)), "dy": [0.0] * 60, "da": [0.0] * 60, "zoom": 1.05}
        k = stabilize.keys(stab, {"in_s": 2.0, "speed": 1.0}, 4, 25.0)
        self.assertEqual([round(x[1]) for x in k], [10, 12, 14, 16])  # 50 fps at 100% on a 25 fps cut: every other frame
        k = stabilize.keys(stab, {"in_s": 2.0, "speed": 0.25}, 4, 25.0)
        self.assertEqual([round(x[1]) for x in k], [10, 10, 11, 11])  # at 25% each source frame shows twice

    def test_the_xml_moves_the_centre_and_the_rotation_on_every_frame(self):
        take = fake_take("F", 0)
        m = {**MAP, "length_s": 0.4, "sections": [{"name": "s", "slots": [{"id": "a", "beats": 1, "show": "axis 0"}]}]}
        picks = fill.fill(m, [take], shotmap.timing(m, None, 25.0), fake_embed)
        p = picks["slots"][0]["pick"]
        n = 80
        p["stab"] = {"from_frame": int(round(p["in_s"] * 100)) - 10, "file_fps": 100.0, "dx": [38.4] * n, "dy": [-21.6] * n, "da": [0.5] * n, "zoom": 1.06}
        with tempfile.TemporaryDirectory() as d:
            path = w.write_xmeml(build_from_picks(picks), Path(d) / "s.xml", fps=25, width=1080, height=1920)
            root = ET.parse(path).getroot()
        clip = root.find(".//video/track/clipitem")
        params = {q.findtext("parameterid"): q for q in clip.iter("parameter")}
        self.assertAlmostEqual(float(params["scale"].findtext("value")), round(p["motion"]["scale"] * 1.06, 3), places=2)
        kfs = params["center"].findall("keyframe")
        self.assertEqual(len(kfs), 10)  # 0.4 s at 25 fps
        self.assertEqual(int(kfs[0].findtext("when")), int(clip.findtext("in")))
        self.assertAlmostEqual(float(kfs[0].find("value").findtext("horiz")), 0.01, places=5)  # 38.4 px / 3840
        self.assertAlmostEqual(float(kfs[0].find("value").findtext("vert")), -0.01, places=5)  # -21.6 px / 2160
        self.assertAlmostEqual(float(params["rotation"].findall("keyframe")[3].findtext("value")), 0.5, places=3)


class ByteTests(unittest.TestCase):
    def setUp(self):
        self.takes = [fake_take("I", 0, n=40, fps=25.0, faces=1), fake_take("B", 1), fake_take("C", 1)]
        self.m = {**MAP, "length_s": 4.0, "sections": [{"name": "s", "slots": [
            {"id": "b1", "beats": 2, "show": "axis 1"},
            {"id": "talk", "beats": 4, "show": "axis 0", "byte": True, "takes": ["I*@6.3-8.4"]},
            {"id": "b2", "beats": 2, "show": "axis 1"}]}]}
        self.picks = fill.fill(self.m, self.takes, shotmap.timing(self.m, [i * 0.5 for i in range(12)], 25.0), fake_embed)

    def test_a_byte_starts_on_its_first_word_at_full_speed(self):
        talk = self.picks["slots"][1]
        self.assertTrue(talk["byte"])
        self.assertEqual((talk["pick"]["name"], talk["pick"]["in_s"], talk["pick"]["speed"]), ("I.MP4", 6.3, 1.0))
        self.assertIn("names its moment", " ".join(shotmap.validate({**self.m, "sections": [{"name": "s", "slots": [
            {"id": "x", "beats": 2, "show": "a", "byte": True, "takes": ["I*"]}]}]})))

    def test_its_voice_plays_and_the_music_steps_down_under_it(self):
        self.picks["slots"][1]["pick"]["gain_db"] = 4.5
        music = {"path": "/Volumes/T/song.mp3", "duration_s": 60.0, "in_s": 10.0, "under_db": -12.0}
        with tempfile.TemporaryDirectory() as d:
            root = ET.parse(w.write_xmeml(build_from_picks(self.picks, music), Path(d) / "b.xml", fps=25, width=1080, height=1920)).getroot()
        tracks = root.findall(".//audio/track")
        voice = tracks[0].findall("clipitem")
        self.assertEqual([c.findtext("name") for c in voice], ["I.MP4"])  # only the byte brings its sound
        self.assertAlmostEqual(float(voice[0].find(".//parameter/value").text), 10 ** (4.5 / 20), places=4)
        song = tracks[1].findall("clipitem")
        gains = [round(20 * math.log10(float(c.find(".//parameter/value").text)), 1) if c.find(".//parameter/value") is not None else 0.0 for c in song]
        self.assertEqual(gains, [0.0, -6.0, -12.0, -6.0, 0.0])
        talk = self.picks["slots"][1]
        starts = [int(c.findtext("start")) for c in song]
        self.assertEqual(starts[2], round((talk["start_s"] - 0.12) * 25))  # fully down before the first word
        ins = [int(c.findtext("in")) - int(c.findtext("start")) for c in song]
        self.assertEqual(len(set(ins)), 1)  # the song runs on underneath: every piece follows from the last

    def test_sfx_keep_off_the_voice(self):
        from videtor.cli import _clear_bytes

        slots = [{"start_s": 0.0, "length_s": 2.0}, {"start_s": 2.0, "length_s": 1.5, "byte": True}]
        sfx = [{"kind": "whoosh", "start_s": 1.0, "in_s": 0.0, "out_s": 2.7}, {"kind": "click", "start_s": 2.5, "in_s": 0.0, "out_s": 0.1},
               {"kind": "click", "start_s": 0.2, "in_s": 0.0, "out_s": 0.1}]
        out = _clear_bytes(sfx, slots)
        self.assertEqual([s["kind"] for s in out], ["whoosh", "click"])  # the click on the byte is gone
        self.assertEqual((out[0]["out_s"], out[0]["fade_out_s"]), (1.04, 0.08))  # the whoosh ends on the cut, fading

    def test_levels_share_one_dip_between_close_bytes(self):
        lv = music_levels([(1.0, 2.0), (2.2, 3.0)], -10.0, 5.0, 25.0)
        self.assertEqual([g for _, _, g in lv], [0.0, -5.0, -10.0, -5.0, 0.0])
        self.assertAlmostEqual(sum(b - a for a, b, _ in lv), 5.0, places=6)


if __name__ == "__main__":
    unittest.main()
