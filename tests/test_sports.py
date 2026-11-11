import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import opentimelineio as otio

from videtor.export import xmeml_writer as w
from videtor.kb import render
from videtor.plan import draft, fx, gym, shotmap
from videtor.plan.build import build_from_picks

FPS = 25.0


def textured(seed: int, h: int = 64, w: int = 36, colour=(1.0, 1.0, 1.0)) -> np.ndarray:
    import cv2

    rng = np.random.default_rng(seed)
    g = cv2.resize(rng.uniform(30, 220, (8, 5)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
    return np.clip(np.stack([g * c for c in colour], axis=-1), 0, 255).astype(np.uint8)


def reel() -> np.ndarray:
    """Shot A (warm, 20 frames), 3 static frames, shot B (cool, 15), a flash peaking on its last frames, shot C (grey, 20)."""
    a = [textured(1, colour=(1.0, 0.7, 0.4))] * 20
    static = [np.repeat(np.linspace(90, 160, 64)[:, None, None], 36, axis=1).repeat(3, axis=2).astype(np.uint8) + k for k in range(3)]
    b = [textured(2, colour=(0.4, 0.7, 1.0))] * 15
    flash = [np.clip(b[0].astype(int) + d, 0, 255).astype(np.uint8) for d in (40, 110, 40)]
    c = [textured(3)] * 20
    return np.stack(a + static + b + flash + c)


def ref(duration=10.0, seed=0, fmt="hype") -> dict:
    """A read reel (the shape kb/render.analyze writes) with shots every ~0.5 s."""
    rng = np.random.default_rng(seed)
    t, shots, trans = 0.0, [], []
    labels = ["batting", "bowling", "portrait", "walk"]
    while t < duration - 0.3:
        L = min(duration - t, round(float(rng.uniform(0.3, 0.8)) * FPS) / FPS)
        if shots:
            trans.append({"t": round(t, 3), "frames": 0, "via": "flash" if rng.random() < 0.2 else "cut"})
        shots.append({"a": int(t * FPS), "b": int((t + L) * FPS), "start_s": round(t, 3), "length_s": round(L, 3), "bw": bool(rng.random() < 0.1),
                      "turns_bw": False, "move": "push-in" if rng.random() < 0.3 else None, "shows": labels[int(rng.integers(0, 4))],
                      "zoom": 1.0, "spin_deg": 0.0, "jitter": 0.0, "sharp": 100.0, "luma": 100.0, "contrast": 40.0, "sat": 100.0, "warmth": 0.0, "vignette": 0.9})
        t += L
    d = {"file": f"Client secret {seed}.mp4", "format": fmt, "fps": FPS, "width": 1080, "height": 1920, "duration_s": duration, "tempo_bpm": 120.0,
         "beats_s": [round(k * 0.5, 3) for k in range(int(duration * 2))], "shots": shots, "transitions": trans,
         "beat_fit": {"on_beat": 0.3, "near_beat": 0.5, "on_half_beat": 0.5, "chance_on_beat": 0.25, "median_lead_s": 0.0},
         "hits": {"per_min": 60.0, "on_cut": 0.3, "accent": 1.2, "times": [x["t"] for x in trans[::2]]}}
    d["summary"] = render.summary(d)
    return d


class RenderTest(unittest.TestCase):
    def test_segments_shots_static_and_flash(self):
        frames = reel()
        feat = render.frame_features(frames)
        shots, trans = render.segment(feat, render.frame_motion(frames), FPS)
        self.assertEqual(len(shots), 3)
        self.assertEqual([t["via"] for t in trans], ["static", "flash"])
        self.assertAlmostEqual(shots[0]["length_s"], 0.8, places=2)
        self.assertTrue(shots[2]["bw"])
        self.assertFalse(shots[0]["bw"])

    def test_beat_fit_and_format(self):
        bf = render.beat_fit([0.5, 1.0, 1.27], [0.0, 0.5, 1.0, 1.5], FPS)
        self.assertAlmostEqual(bf["on_beat"], 0.67, places=2)
        self.assertEqual(render.format_of("TEAM A VS TEAM B HYPE REEL.mp4"), "hype")
        self.assertEqual(render.format_of("Team wrap 6th May.mp4"), "wrap")
        self.assertEqual(render.format_of("Match Day Reel_2.mp4"), "matchday")

    def test_flicker_bursts(self):
        shots = [{"length_s": 0.08}] * 5 + [{"length_s": 1.0}] + [{"length_s": 0.04}] * 2
        self.assertEqual(render.flicker_bursts(shots, FPS), [(0, 5)])

    def test_style_keeps_file_names_out(self):
        style = {**render.summarize([ref(seed=1), ref(seed=2, fmt="wrap")]), "renders": render.anonymous([ref(seed=1), ref(seed=2, fmt="wrap")])}
        text = json.dumps(style)
        self.assertNotIn("Client secret", text)
        self.assertEqual({r["label"] for r in style["renders"]}, {"hype-1", "wrap-1"})
        self.assertIn("From your renders (2)", render.markdown(style, "sports-promo"))


def slot(sid, start, length, section="a", **kw):
    pick = {"path": "/m/a.mov", "name": "a.mov", "in_s": 1.0, "out_s": 1.0 + length, "speed": 1.0, "file_fps": FPS, "duration_s": 30.0,
            "width": 1080, "height": 1920, "audio_channels": 0, "motion": {"scale": 50.0, "rotation": 0.0, "center": [0.0, 0.0]}}
    return {"id": sid, "section": section, "start_s": start, "length_s": length, "pick": pick, "alternates": [], **kw}


class FxTest(unittest.TestCase):
    def test_assign_is_deterministic_and_respects_slots(self):
        slots = [slot(f"s{k}", k * 0.5, 0.5, section="a" if k < 6 else "b") for k in range(12)]
        slots[3]["effects"] = ["whip"]
        slots[4]["byte"] = True
        rates = {**fx.DEFAULT_RATES, "flash": 0.5, "bw_runs_per_min": 10}
        e1, e2 = fx.assign(slots, rates, 6.0, seed=3), fx.assign(slots, rates, 6.0, seed=3)
        self.assertEqual(e1, e2)
        self.assertEqual(e1["s3"], ["whip"])
        self.assertEqual(e1["s4"], [])
        self.assertFalse(set(e1["s0"]) & set(fx.CUT_FX))  # nothing cuts into the first shot
        self.assertTrue(any("bw" in v for v in e1.values()))

    def test_clip_keys(self):
        k = fx.clip_fx(["whip", "push-in"], ["spin"], 20, {"scale": 50.0, "rotation": 0.0, "center": [0.0, 0.0]})
        self.assertEqual(k["blur"][0], [0, fx.BLUR_MAX])
        self.assertEqual(k["center"][0][0], 0)
        self.assertGreater(k["center"][0][1], 0)  # comes in from the right
        self.assertEqual(k["rotation"][-1][0], 19)  # spins out over the last frames
        self.assertLess(k["rotation"][-1][1], 0)
        self.assertEqual([r for r in k["rotation"] if r[0] == 16][0][1], 0.0)  # held until the exit starts
        self.assertGreater(k["scale"][-1][1], 50.0)
        self.assertEqual(fx.clip_fx(["bw"], [], 10, None), {"bw": True, "label": "Iris"})

    def test_overlay_assets(self):
        self.assertEqual(len(fx.flash_frames(8, 12)), 3)
        self.assertEqual(fx.flash_frames(8, 12)[1][0, 0, 3], 191)
        st = fx.static_frames(40, 72)
        self.assertEqual(st[0].shape, (72, 40, 3))
        self.assertGreater(float(st[0][:10].mean()), float(st[0][-10:].mean()))  # lighter at the top

    def test_build_writes_effects(self):
        slots = [slot("s0", 0.0, 0.8, effects=["push-in"]), slot("s1", 0.8, 0.8, effects=["whip", "echo"]), slot("s2", 1.6, 0.8, effects=["bw"])]
        picks = {"name": "t", "fps": FPS, "width": 1080, "height": 1920, "slots": slots,
                 "flashes": [{"path": "/m/static.mov", "in_s": 0.0, "length_s": 0.12, "start_s": 0.76, "duration_s": 0.12, "fps": FPS,
                              "width": 1080, "height": 1920, "motion": {"scale": 100.0, "rotation": 0.0, "center": [0.0, 0.0]}}]}
        tl = build_from_picks(picks, None, alternates=0)
        tl.metadata["videtor"] = {"markers": [{"name": "whip", "at_s": 0.8}]}
        with tempfile.TemporaryDirectory() as d:
            root = ET.parse(w.write_xmeml(tl, Path(d) / "t.xml", FPS, 1080, 1920)).getroot()
        seq = root.find("sequence")
        self.assertEqual(seq.find("marker/name").text, "whip")
        self.assertEqual(seq.find("marker/in").text, "20")
        tracks = seq.findall("media/video/track")
        self.assertEqual(len(tracks), 3)  # V1, echo, overlays
        v1 = tracks[0].findall("clipitem")
        ids = [[e.findtext("effectid") for e in c.findall("filter/effect")] for c in v1]
        self.assertIn("Gaussian Blur", ids[1])
        self.assertIn("Color Corrector 3-way", ids[2])
        self.assertEqual(v1[2].findtext("labels/label2"), "Iris")
        echo = tracks[1].find("clipitem")
        self.assertEqual(echo.findtext("compositemode"), "lighten")
        opacity = [e for e in echo.findall("filter/effect") if e.findtext("effectid") == "opacity"][0]
        self.assertEqual(opacity.find("parameter/value").text, "45.0")
        blur = [e for e in v1[1].findall("filter/effect") if e.findtext("effectid") == "Gaussian Blur"][0]
        whens = [int(k.findtext("when")) for k in blur.findall("parameter/keyframe")]
        self.assertEqual(whens[0], int(v1[1].findtext("in")))  # keys count from the clip's in-point
        scale = v1[0].find("filter/effect/parameter")
        values = [float(k.findtext("value")) for k in scale.findall("keyframe")]
        self.assertEqual(values[0], 50.0)  # the push-in starts at the framing scale
        self.assertGreater(values[-1], 55.0)  # and the whip into the next shot pushes it further


class AccentTimingTest(unittest.TestCase):
    def test_cuts_move_onto_accents(self):
        m = {"length_s": 4.0, "beat_lead_s": 0.0, "cut_on": "accents",
             "sections": [{"name": "a", "slots": [{"id": f"s{k}", "beats": 2, "show": "x"} for k in range(4)]}]}
        grid = [k * 0.5 for k in range(10)]
        t = shotmap.timing(m, grid, FPS, accents_rel=[1.08, 2.4, 3.3])
        self.assertEqual([round(a, 2) for a, _ in t], [0.0, 1.08, 2.0, 3.0])  # 2.4 and 3.3 are more than a third of a beat away
        m.pop("cut_on")
        self.assertEqual([round(a, 2) for a, _ in shotmap.timing(m, grid, FPS, [1.08])], [0.0, 1.0, 2.0, 3.0])


class GymTest(unittest.TestCase):
    def test_reward_zero_on_itself_and_rollout_covers_the_reel(self):
        refs = [ref(seed=s) for s in range(4)]
        eps = [gym.episode(r) for r in refs]
        unit = gym.scales(eps)
        self.assertEqual(gym.reward(eps[0]["features"], eps[0]["features"], unit), 0.0)
        cut = gym.rollout(gym.medians(refs), eps[0], np.random.default_rng(1))
        self.assertAlmostEqual(sum(s["length_s"] for s in cut["shots"]), refs[0]["duration_s"], places=6)
        self.assertEqual(len(cut["cuts"]), len(cut["shots"]) - 1)

    def test_search_beats_the_generic_policy(self):
        refs = [ref(seed=s) for s in range(4)]
        p, r = gym.cem(refs, iters=4, pop=12, elite=3, rollouts=1)
        eps = [gym.episode(x) for x in refs]
        unit = gym.scales(eps)
        g = gym.generic({"shot_length_s": {"median": 2.0}})
        self.assertGreater(gym.score(p, eps, unit), gym.score(g, eps, unit))
        self.assertEqual(gym.Policy.from_json(p.to_json()).held_s, p.held_s)

    def test_draft_is_a_valid_shot_map(self):
        refs = [ref(seed=s) for s in range(3)]
        with tempfile.TemporaryDirectory() as d:
            kb = Path(d) / "kb"
            (kb / "policy").mkdir(parents=True)
            (kb / "policy" / "sports-promo.json").write_text(json.dumps({"policy": gym.medians(refs).to_json(), "rates": {}}))
            m = draft.draft("t", "sports-promo", 20.0, "t::footage", kb_dir=kb)
            path = draft.write(m, Path(d) / "out")
            self.assertEqual(shotmap.validate(json.loads(path.read_text())), [])
        beats = sum(s["beats"] for sec in m["sections"] for s in sec["slots"])
        self.assertAlmostEqual(beats * 60 / 128, 20.0, delta=60 / 128 * 1.5)
        self.assertEqual(m["sections"][0]["name"], "open")
        self.assertEqual(m["effects"], "sports-promo")


if __name__ == "__main__":
    unittest.main()
