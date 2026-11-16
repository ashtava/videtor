"""Promos out of one finished video: the reading of the video, the choice of its best stretches (clips) and
moments (edits), the framing in the user's shape, the picks and the render command."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from videtor.analyze import link, video
from videtor.export import promo as promo_out
from videtor.plan import promo


def said(*sentences, wps=3.0, language="en"):
    """A transcript: (start second, "words ...") per sentence, three words a second."""
    segs = []
    for start, text in sentences:
        ws, t = [], float(start)
        for w in text.split():
            ws.append({"w": w, "start": round(t, 3), "end": round(t + 0.8 / wps, 3), "p": 0.9})
            t += 1.0 / wps
        segs.append({"start": start, "end": ws[-1]["end"], "text": text, "words": ws})
    return {"language": language, "segments": segs}


def sound(duration, loud=(), base=-32.0, peak=-10.0, bpm=120.0):
    """Beats and loudness per half second: quiet, but for the (from, to) stretches in `loud`."""
    n = int(duration / 0.5)
    rms = np.full(n, base)
    for a, b in loud:
        rms[int(a / 0.5): int(b / 0.5)] = peak
    onset = (rms - base) / 10.0
    return {"duration_s": duration, "tempo_bpm": bpm, "pauses": 0.4, "voice": 0.8, "beats_s": [round(k * 60.0 / bpm, 3) for k in range(int(duration * bpm / 60.0))],
            "rms_db": rms.tolist(), "onset_half_s": onset.tolist()}


def fake_video(duration=120.0, step=0.5, cuts=(), fps=25.0, size=(1920, 1080), speech=None, beats=None, content=None, extra=None, **arrays):
    n = int(duration / step) + 1
    a = {"t": np.arange(n) * step, "sharp": np.full(n, 200.0), "bright": np.full(n, 120.0), "motion": np.zeros(n), "faces": np.zeros(n),
         "face_area": np.zeros(n), "face_x": np.full(n, np.nan), "face_y": np.full(n, np.nan), "faces_x0": np.full(n, np.nan), "faces_x1": np.full(n, np.nan),
         "card": np.zeros(n), "text": np.zeros(n)}
    for k, v in arrays.items():
        a[k] = np.asarray(v(a["t"]) if callable(v) else np.full(n, v), np.float32)
    a["spoken"] = video.spoken(speech, n, step)
    info = {"width": size[0], "height": size[1], "display_w": size[0], "display_h": size[1], "fps": fps, "duration_s": duration, "audio_channels": 2,
            "content": content or [0.0, 0.0, 1.0, 1.0]}
    return video.Video(path="/Volumes/T/long.mp4", fid="vlong", info=info, step_s=step, a=a, cuts_s=list(cuts), beats=beats, speech=speech, extra=extra or {})


def a_talk():
    """Two minutes of talk in three-second sentences that run on, but for one thought: it starts after a pause at
    0:40 and ends on a full stop and a pause at 1:07."""
    rows, t = [], 1.0
    while t < 118:
        if 39 < t < 41:
            t = 41.5  # the pause before the thought
            rows.append((t, "The biggest mistake people make is waiting too long."))
        elif 66.5 < t < 69:
            rows.append((t, "that is the whole idea of it."))
            t += 1.4  # and the pause after it
        else:
            rows.append((t, "and then we went on and talked some more about it"))  # no full stop: it runs on
        t += 3.2
    return said(*rows)


class ChoiceTests(unittest.TestCase):
    def test_a_talking_videos_clip_runs_from_a_new_thought_to_a_full_stop(self):
        v = fake_video(speech=a_talk(), beats=sound(120.0), faces=1.0, face_x=0.5, face_y=0.4, faces_x0=0.45, faces_x1=0.55)
        cands, led, notes = promo.candidates(v, "clip", "auto", 25.0, 30.0, n=3)
        self.assertEqual((led, notes), ("speech", []))
        best = cands[0]
        a, b = best["parts"][0]["a"], best["parts"][0]["b"]
        self.assertAlmostEqual(a, 41.5 - promo.PRE_S, places=2)  # a breath before the first word
        self.assertTrue(25.0 <= b - a <= 30.0 and 68.0 < b < 70.5, (a, b))
        self.assertTrue(best["text"].startswith("The biggest mistake") and best["text"].endswith("idea of it."))
        self.assertIn("ends on a full stop", best["reasons"])
        self.assertTrue(any(r.startswith("starts on a new thought, after a") for r in best["reasons"]))
        self.assertIn("a face on screen throughout", best["reasons"])
        spans = sorted((c["parts"][0]["a"], c["parts"][0]["b"]) for c in cands)
        self.assertTrue(all(y[0] - x[1] >= promo.APART_S for x, y in zip(spans, spans[1:])), spans)  # three promos, three parts of the video

    def test_a_thought_that_leans_on_the_one_before_counts_for_less_in_english(self):
        us = promo.units(said((10.0, "And that is why it never works out."), (20.0, "The secret is to start small today.")))
        m = promo.Measures(fake_video(duration=40.0))
        lean, alone = promo._said(us, 0, 0, m, True), promo._said(us, 1, 1, m, True)
        self.assertLess(lean[2], alone[2] - 0.5)
        self.assertTrue(any("lean on what came before" in r for r in lean[3]))
        self.assertAlmostEqual(promo._said(us, 0, 0, m, False)[2], promo._said(us, 1, 1, m, False)[2], delta=0.35)  # another language: no such reading

    def test_words_that_never_stop_are_split_at_their_widest_gap(self):
        run = said((0.0, " ".join(["word"] * 30)), (10.4, " ".join(["more"] * 30)))  # twenty seconds, one 0.67 s gap, no full stop
        us = promo.units(run)
        self.assertEqual([round(u["start"], 1) for u in us], [0.0, 10.4])
        self.assertTrue(all(u["end"] - u["start"] <= promo.MAX_UNIT_S for u in us))

    def test_a_music_led_videos_clip_is_its_loud_stretch_from_cut_to_cut(self):
        cuts = [float(t) for t in range(3, 120, 3)]
        v = fake_video(cuts=cuts, beats=sound(120.0, loud=[(60, 90)]))
        cands, led, _ = promo.candidates(v, "clip", "auto", 25.0, 30.0, n=2)
        self.assertEqual(led, "music")
        a, b = cands[0]["parts"][0]["a"], cands[0]["parts"][0]["b"]
        self.assertTrue(a in cuts and b in cuts and 57 <= a <= 63 and 25 <= b - a <= 30, (a, b))
        self.assertIn("starts and ends on a cut", cands[0]["reasons"])
        self.assertIn("the loudest, punchiest stretch of the sound", cands[0]["reasons"])
        # the same stretch as the end credits: not a promo
        credits = fake_video(cuts=cuts, beats=sound(120.0, loud=[(60, 90)]), card=lambda t: ((t >= 58) & (t < 92)).astype(float))
        a2, b2 = promo.candidates(credits, "clip", "auto", 25.0, 30.0, n=1)[0][0]["parts"][0].values()
        self.assertTrue(b2 <= 62 or a2 >= 88, (a2, b2))

    def test_what_youtubes_viewers_replayed_counts_for_more_than_the_sound(self):
        heat = [{"start_time": t, "end_time": t + 1.2, "value": 1.0 if 20 <= t < 48 else 0.1} for t in np.arange(0, 120, 1.2)]
        v = fake_video(cuts=[float(t) for t in range(4, 120, 4)], beats=sound(120.0, loud=[(80, 110)]), extra={"heatmap": heat})
        best = promo.candidates(v, "clip", "music", 25.0, 30.0, n=1)[0][0]
        self.assertTrue(18 <= best["parts"][0]["a"] <= 24, best["parts"])
        self.assertIn("one of the most replayed parts on YouTube", best["reasons"])

    def test_an_edit_of_a_talk_is_its_strongest_lines_in_the_order_they_were_said(self):
        lines = [(4.0 + 6.0 * k, "This is one whole thought said in one go.") for k in range(18)]
        lines[7] = (lines[7][0], "Why does nobody ever tell you this one thing?")
        v = fake_video(speech=said(*lines), beats=sound(120.0))
        cands, led, notes = promo.candidates(v, "edit", "auto", 25.0, 30.0, n=2)
        first = cands[0]
        self.assertEqual((first["kind"], first["lead"], led), ("edit", "speech", "speech"))
        starts = [p["a"] for p in first["parts"]]
        self.assertEqual(starts, sorted(starts))
        total = sum(p["b"] - p["a"] for p in first["parts"])
        self.assertTrue(20.0 <= total <= 30.0 and len(first["parts"]) >= 5, (total, len(first["parts"])))
        self.assertTrue(any("Why does nobody" in p["text"] for p in first["parts"]))  # the question is among the strongest
        self.assertFalse(any(a < p["b"] and b > p["a"] for a, b in promo.spans(cands[1]) for p in first["parts"]))  # the second promo: other footage
        short = fake_video(duration=40.0, speech=said(*lines[:6]), beats=sound(40.0))
        few, _, notes = promo.candidates(short, "edit", "speech", 25.0, 30.0, n=2)
        self.assertEqual([c["kind"] for c in few][:1], ["edit"])
        self.assertTrue(any("too few separate lines" in n for n in notes), notes)  # six lines make one edit, not two
        self.assertEqual(promo.candidates(short, "edit", "speech", 25.0, 30.0, n=2, want=1)[2], [])  # nothing to say when the one asked for is an edit

    def test_an_edit_of_a_music_led_video_lays_its_best_shots_on_the_strongest_sound_on_the_beat(self):
        cuts = [float(t) for t in np.arange(2.5, 180, 2.5)]
        sharp = lambda t: np.where((t % 10) < 5, 400.0, 60.0)  # noqa: E731 - every other pair of shots is the sharp one
        v = fake_video(duration=180.0, cuts=cuts, beats=sound(180.0, loud=[(100, 140)]), sharp=sharp)
        cands, _, notes = promo.candidates(v, "edit", "auto", 25.0, 30.0, n=2)
        e = cands[0]
        self.assertEqual((e["kind"], e["lead"], notes), ("edit", "music", []))
        self.assertTrue(98 <= e["bed"]["a"] <= 112 and 25 <= e["bed"]["b"] - e["bed"]["a"] <= 30, e["bed"])
        lens = [round(p["b"] - p["a"], 3) for p in e["parts"]]
        self.assertTrue(all(abs(x - 2.0) < 1e-6 for x in lens[:-1]), lens)  # four beats a shot at 120 bpm: nearest to three quarters of a 2.5 s shot
        self.assertAlmostEqual(sum(lens), e["bed"]["b"] - e["bed"]["a"], places=3)
        starts = [p["a"] for p in e["parts"]]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all((p["a"] % 10) < 5 for p in e["parts"]), starts)  # the sharp shots
        for p in e["parts"]:  # each inside one shot, clear of its cuts
            inside = [c for c in cuts if p["a"] < c < p["b"]]
            self.assertEqual(inside, [], p)
        second = cands[1]
        self.assertEqual(second["kind"], "edit")
        self.assertFalse(any(p["a"] < q["b"] and p["b"] > q["a"] for p in second["parts"] for q in e["parts"]))  # no shot twice across promos
        self.assertTrue(second["bed"]["a"] >= e["bed"]["b"] or second["bed"]["b"] <= e["bed"]["a"])

    def test_words_sung_over_music_do_not_make_a_talking_video_but_a_voice_over_does(self):
        half = said(*[(4.0 + 8.0 * k, "these are the words of a song that goes on") for k in range(14)])  # words during half of it
        full = said(*[(1.0 + 3.4 * k, "this voice goes on and on about the product") for k in range(34)])  # words nearly throughout
        song = {**sound(120.0), "pauses": 0.06}
        self.assertEqual(promo.lead_of(fake_video(speech=half, beats=song)), "music")
        self.assertEqual(promo.lead_of(fake_video(speech=half, beats=sound(120.0))), "speech")  # the same words with the pauses of talk
        self.assertEqual(promo.lead_of(fake_video(speech=full, beats=song)), "speech")

    def test_a_video_too_short_gives_what_it_has(self):
        v = fake_video(duration=12.0, beats=sound(12.0))
        cands, _, _ = promo.candidates(v, "clip", "auto", 25.0, 30.0, n=3)
        self.assertEqual(len(cands), 1)
        self.assertLessEqual(cands[0]["parts"][0]["b"], 12.0)
        self.assertEqual(promo.lead_of(v), "music")


class FramingTests(unittest.TestCase):
    def test_a_wide_picture_in_a_vertical_frame_is_cropped_around_the_face(self):
        v = fake_video(faces=1.0, face_x=0.8, face_y=0.4, faces_x0=0.74, faces_x1=0.86)
        f = promo.framing(v, 10.0, 20.0, 1080, 1920)
        self.assertEqual(f["scale"], 177.8)  # 1080 high fills 1920
        self.assertAlmostEqual(f["center"][0], -0.3 * 1920 * 1.778 / (2 * 1920), places=4)  # the face from 80% across to the middle
        self.assertEqual(f["center"][1], 0.0)
        self.assertEqual(promo.framing(fake_video(), 10.0, 20.0, 1080, 1920)["center"], [0.0, 0.0])  # no face: the middle
        far = promo.framing(fake_video(faces=1.0, face_x=0.99, face_y=0.4, faces_x0=0.97, faces_x1=1.0), 10.0, 20.0, 1080, 1920)
        self.assertAlmostEqual(far["center"][0], -(1920 * 1.778 - 1080) / 2 / (2 * 1920), places=4)  # never past the picture's edge
        whole = promo.framing(v, 10.0, 20.0, 1080, 1920, fit="whole")
        self.assertEqual((whole["scale"], whole["center"]), (56.2, [0.0, 0.0]))
        two = fake_video(faces=2.0, face_x=0.45, face_y=0.4, faces_x0=0.42, faces_x1=0.62)  # two people close together: both kept in
        self.assertAlmostEqual(promo.framing(two, 0.0, 10.0, 1080, 1920)["reframed"]["subject_x"], 0.52, places=3)
        apart = fake_video(faces=2.0, face_x=0.2, face_y=0.4, faces_x0=0.15, faces_x1=0.85)  # too far apart for the crop: the bigger face
        self.assertAlmostEqual(promo.framing(apart, 0.0, 10.0, 1080, 1920)["reframed"]["subject_x"], 0.2, places=3)

    def test_a_slide_of_words_is_shown_whole_and_the_speaker_beside_it_is_still_cropped(self):
        v = fake_video(cuts=[10.0], faces=lambda t: (t < 10).astype(float), face_x=0.7, face_y=0.4, faces_x0=0.65, faces_x1=0.75, text=lambda t: (t >= 10).astype(float))
        segs = promo.segments(v, 4.0, 20.0, 1080, 1920, "fill", 25.0)
        self.assertEqual([(a, b, f["scale"], f.get("whole")) for a, b, f in segs], [(4.0, 10.0, 177.8, None), (10.0, 20.0, 56.2, "words on screen")])
        p = promo.picks_for(v, {"kind": "clip", "lead": "speech", "parts": [{"a": 4.0, "b": 20.0}], "bed": None, "score": 1.0, "reasons": [], "text": ""}, "x", 1080, 1920)
        self.assertEqual([("shown whole: words on screen" in s["pick"]["reasons"]) for s in p["slots"]], [False, True])
        talking = fake_video(faces=1.0, face_x=0.7, face_y=0.4, faces_x0=0.65, faces_x1=0.75, text=1.0)  # a caption under a speaker is not a slide
        self.assertEqual(promo.framing(talking, 0.0, 10.0, 1080, 1920)["scale"], 177.8)
        self.assertNotIn("whole", promo.framing(v, 10.0, 20.0, 1920, 1080))  # nothing to crop in the video's own shape

    def test_black_bars_in_the_source_are_left_outside_the_frame(self):
        boxed = fake_video(content=[0.342, 0.0, 0.658, 1.0])  # a vertical video inside a 16:9 upload
        f = promo.framing(boxed, 0.0, 10.0, 1080, 1920)
        self.assertEqual((f["scale"], f["center"]), (178.1, [0.0, 0.0]))  # the picture inside the bars fills the frame
        off = promo.framing(fake_video(content=[0.0, 0.0, 0.5, 1.0]), 0.0, 10.0, 1920, 1080, fit="whole")
        self.assertAlmostEqual(off["center"][0], 0.25 * 1920 * off["scale"] / 100 / (2 * 1920), places=4)  # the left half, moved to the middle

    def test_a_stretch_is_split_where_its_shots_need_different_crops_and_only_there(self):
        v = fake_video(cuts=[10.0, 20.0], faces=1.0, face_y=0.4, face_x=lambda t: np.where(t < 10, 0.2, 0.8), faces_x0=lambda t: np.where(t < 10, 0.15, 0.75),
                       faces_x1=lambda t: np.where(t < 10, 0.25, 0.85))
        segs = promo.segments(v, 4.0, 28.0, 1080, 1920, "fill", 25.0)
        self.assertEqual([(a, b) for a, b, _ in segs], [(4.0, 10.0), (10.0, 28.0)])  # the cut at 20 changes nothing: one piece
        self.assertGreater(segs[0][2]["center"][0], 0)
        self.assertLess(segs[1][2]["center"][0], 0)
        self.assertEqual(len(promo.segments(v, 4.0, 28.0, 1920, 1080, "fill", 25.0)), 1)  # the video's own shape: nothing to crop

    def test_a_promo_is_picks_on_the_videos_own_frames_with_its_own_sound(self):
        v = fake_video(cuts=[10.0], fps=29.97, faces=1.0, face_y=0.4, face_x=lambda t: np.where(t < 10, 0.2, 0.8), faces_x0=lambda t: np.where(t < 10, 0.15, 0.75),
                       faces_x1=lambda t: np.where(t < 10, 0.25, 0.85))
        cand = {"kind": "clip", "lead": "speech", "parts": [{"a": 4.013, "b": 30.02}], "bed": None, "score": 1.5, "reasons": ["ends on a full stop"], "text": "Hello there."}
        p = promo.picks_for(v, cand, "talk-promo-1", 1080, 1920)
        self.assertEqual((p["fps"], p["width"], p["height"], p["kind"], len(p["slots"])), (29.97, 1080, 1920, "promo", 2))
        t = 0.0
        for s in p["slots"]:
            self.assertTrue(s["byte"])
            self.assertAlmostEqual(s["start_s"], t, places=3)
            self.assertAlmostEqual(s["length_s"] * 29.97, round(s["length_s"] * 29.97), places=2)  # whole frames
            self.assertAlmostEqual(s["pick"]["out_s"] - s["pick"]["in_s"], s["length_s"], places=3)
            t += s["length_s"]
        self.assertAlmostEqual(p["slots"][0]["pick"]["out_s"], p["slots"][1]["pick"]["in_s"], places=4)  # the sound runs on across the change of crop
        self.assertAlmostEqual(p["length_s"], 30.02 - 4.013, delta=0.04)
        self.assertEqual(p["promo"]["reasons"], ["ends on a full stop"])
        bed = {**cand, "kind": "edit", "lead": "music", "parts": [{"a": 2.0, "b": 3.0}, {"a": 40.0, "b": 41.0}], "bed": {"a": 60.0, "b": 62.0}}
        silent = promo.picks_for(v, bed, "x", 1080, 1920)
        self.assertEqual([s.get("byte", False) for s in silent["slots"]], [False, False])  # the shots are silent: the bed plays under them
        self.assertEqual(promo.seq_fps(29.97003), 29.97)
        self.assertEqual(promo.seq_fps(27.3), 27.3)


class RenderTests(unittest.TestCase):
    def test_the_render_is_one_ffmpeg_call_with_one_piece_of_sound_per_unbroken_stretch(self):
        v = fake_video(cuts=[10.0], faces=1.0, face_y=0.4, face_x=lambda t: np.where(t < 10, 0.2, 0.8), faces_x0=lambda t: np.where(t < 10, 0.15, 0.75),
                       faces_x1=lambda t: np.where(t < 10, 0.25, 0.85))
        edit = {"kind": "edit", "lead": "speech", "parts": [{"a": 4.0, "b": 14.0}, {"a": 50.0, "b": 55.0}], "bed": None, "score": 1.0, "reasons": [], "text": ""}
        p = promo.picks_for(v, edit, "x", 1080, 1920)
        self.assertEqual(len(p["slots"]), 3)  # the first line crosses a cut: two crops
        self.assertEqual(promo_out.stretches(p), [("/Volumes/T/long.mp4", 4.0, 10.0), ("/Volumes/T/long.mp4", 50.0, 5.0)])
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            for s in p["slots"]:
                s["pick"]["path"] = f.name
            cmd = promo_out.command(p, Path("/tmp/out.mp4"), None, gain_db=3.5)
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertEqual(cmd.count("-i"), 5)  # three pictures, two sounds
            self.assertIn("concat=n=3:v=1:a=0[v]", graph)
            self.assertIn("concat=n=2:v=0:a=1[a0]", graph)
            self.assertIn("volume=3.5dB", graph)
            self.assertIn("crop=1080:1920", graph)
            music = promo_out.command({**p, "slots": [{**s, "byte": False} for s in p["slots"]]}, Path("/tmp/out.mp4"), {"path": "/tmp/bed.wav", "in_s": 1.0})
            self.assertEqual((music.count("-i"), music[music.index("/tmp/bed.wav") - 4]), (4, "1.0000"))
        with self.assertRaises(FileNotFoundError):
            promo_out.command(promo.picks_for(v, edit, "x", 1080, 1920), Path("/tmp/out.mp4"))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class ReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.video = make_video(cls.tmp / "long.mp4")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_video_is_read_once_cuts_to_the_frame_and_kept(self):
        calls = {"faces": 0, "cards": 0, "words": 0}

        def detect(rgb):
            calls["faces"] += 1
            return [[0.6, 0.2, 0.8, 0.5]]

        def cards(frames):
            calls["cards"] += len(frames)
            return np.tile(np.array([0.9, 0.2], np.float32), (len(frames), 1))

        def words(path, fid, folder):
            calls["words"] += 1
            return said((1.0, "Hello there, this is a test."))

        v = video.read(self.video, self.tmp / "work", detect=detect, cards=cards, transcribe=words, track=lambda wav: sound(12.0))
        self.assertEqual((v.info["display_w"], v.info["display_h"], v.info["fps"], v.info["audio_channels"]), (320, 180, 25.0, 1))
        self.assertEqual(v.cuts_s, [4.0, 8.0])
        self.assertEqual(v.shots(), [(0.0, 4.0), (4.0, 8.0), (8.0, v.duration_s)])
        n = len(v.a["t"])
        self.assertTrue(23 <= n <= 25 and all(len(v.a[k]) == n for k in (*video.KEYS, "spoken")), n)
        self.assertAlmostEqual(float(v.a["face_x"][3]), 0.7, places=3)
        self.assertTrue((v.a["card"] > 0.5).all() and (v.a["text"] < 0.5).all() and v.a["spoken"][2:6].all() and not v.a["spoken"][10:].any())
        self.assertEqual(calls["faces"], n)
        again = video.read(self.video, self.tmp / "work", detect=detect, cards=cards, transcribe=words, track=lambda wav: 1 / 0)
        self.assertEqual((calls["faces"], again.cuts_s, again.beats["tempo_bpm"]), (n, [4.0, 8.0], 120.0))  # the picture and the sound are not read twice
        self.assertEqual(calls["words"], 2)  # the transcript's own cache is the transcriber's
        music = video.read(self.video, self.tmp / "song", detect=detect, cards=cards, transcribe=words, track=lambda wav: {**sound(12.0), "voice": 0.05})
        self.assertEqual((calls["words"], music.speech, float(music.a["spoken"].sum())), (2, None, 0.0))  # hardly a voice in it: Whisper is not run
        self.assertIsNotNone(video.read(self.video, self.tmp / "song", detect=detect, cards=cards, transcribe=words, listen=True).speech)  # unless told to
        self.assertLess(video.pauses(self.tmp / "work" / "audio.wav"), 0.05)  # a steady tone has no pauses
        self.assertLess(video.voiced(self.tmp / "work" / "audio.wav", 12.0), 0.1)  # and no voice
        with self.assertRaises(ValueError):
            video.probe(self.tmp / "nothing.mp4")

    def test_what_a_frame_is_never_crosses_a_cut(self):
        info = video.probe(self.video)
        arr, cuts, _ = video.scan_picture(self.video, info, 0.5, detect=lambda rgb: [], cards=lambda fr: np.array([[float(k == 0), 0.0] for k in range(len(fr))], np.float32))
        self.assertEqual(cuts, [4.0, 8.0])
        # only the very first frame said "card": every sample of its shot follows the asked frame nearest to it there, none past the cut at 4 s
        self.assertEqual(arr["card"][:8].round(2).tolist(), [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        late = lambda fr: np.array([[0.0, 0.0]] * len(fr), np.float32)  # noqa: E731
        arr2, _, _ = video.scan_picture(self.video, info, 0.5, detect=lambda rgb: [], cards=late)
        self.assertEqual(float(arr2["card"].sum() + arr2["text"].sum()), 0.0)
        self.assertEqual(len(arr["card"]), len(arr["t"]))

    def test_black_bars_are_measured(self):
        boxed = self.tmp / "boxed.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=100x180:r=25:d=3", "-vf", "pad=320:180:110:0:black", "-pix_fmt", "yuv420p",
                        str(boxed)], check=True)
        arr, cuts, content = video.scan_picture(boxed, video.probe(boxed), 0.5, detect=lambda rgb: [], cards=lambda fr: np.zeros(len(fr), np.float32))
        self.assertAlmostEqual(content[0], 110 / 320, delta=0.02)
        self.assertAlmostEqual(content[2], 210 / 320, delta=0.02)
        self.assertEqual((content[1], content[3], cuts), (0.0, 1.0, []))


def make_video(path: Path, seconds: int = 12) -> Path:
    """A small video with a cut every third of its length and a tone: testsrc2, colour bars, the RGB test picture."""
    third = seconds / 3
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=25:d={third}", "-f", "lavfi", "-i", f"smptebars=s=320x180:r=25:d={third}",
                    "-f", "lavfi", "-i", f"rgbtestsrc=s=320x180:r=25:d={third}", "-f", "lavfi", "-i", f"sine=f=330:d={seconds}",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[v]", "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return path


class LinkTests(unittest.TestCase):
    def test_a_link_is_told_from_a_file_and_errors_come_in_plain_words(self):
        self.assertTrue(link.is_link(" https://youtu.be/abc "))
        self.assertFalse(link.is_link("/Users/me/Movies/talk.mp4"))
        self.assertIn("Instagram wants a login", link._plain("ERROR: [Instagram] xyz: You need to log in to access this content", "https://www.instagram.com/reel/xyz/"))
        self.assertIn("private, removed or not available", link._plain("ERROR: [youtube] abc: Video unavailable", "https://youtu.be/abc"))
        self.assertIn("not a link to a video", link._plain("ERROR: Unsupported URL: https://example.com", "https://example.com"))
        self.assertIn("updating the fetcher", link._plain("ERROR: something new", "https://youtu.be/abc"))
        self.assertIn("uv sync", link.missing())

    def test_a_video_is_the_same_video_however_its_link_was_copied(self):
        same = ["https://youtu.be/izY-H7x_67?si=AbCd", "https://www.youtube.com/watch?v=izY-H7x_67", "https://m.youtube.com/watch?v=izY-H7x_67&t=42s&list=PL1",
                "https://www.youtube.com/shorts/izY-H7x_67?feature=share", "https://music.youtube.com/watch?v=izY-H7x_67&si=zz"]
        self.assertEqual({link.key(u) for u in same}, {"youtube:izY-H7x_67"})
        self.assertEqual({link.key(u) for u in ("https://www.instagram.com/reel/DeIX_9-Q/?igsh=abc", "https://instagram.com/someone/reel/DeIX_9-Q", "https://www.instagram.com/p/DeIX_9-Q/")},
                         {"instagram:DeIX_9-Q"})
        self.assertNotEqual(link.key("https://youtu.be/aaa"), link.key("https://youtu.be/bbb"))
        self.assertEqual(link.key("https://vimeo.com/123/?utm_source=x&h=9f"), "vimeo.com/123?h=9f")  # other sites: the address without its tracking parts


if __name__ == "__main__":
    unittest.main()
