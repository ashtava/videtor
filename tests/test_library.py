import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from videtor.analyze import sparse
from videtor.kb import footage, xmeml
from videtor.plan import library

RNG = np.random.default_rng(7)
PROMPT_VECS = {}


def fake_embed_texts(texts: list[str]) -> np.ndarray:
    out = []
    for t in texts:
        if t not in PROMPT_VECS:
            v = RNG.normal(size=512).astype(np.float32)
            PROMPT_VECS[t] = v / np.linalg.norm(v)
        out.append(PROMPT_VECS[t])
    return np.array(out)


def class_emb(cls: str) -> np.ndarray:
    """An embedding that CLIP's softmax would read as `cls`: the mean of its prompts' vectors."""
    v = fake_embed_texts(library.PROMPTS[cls]).mean(axis=0)
    return v / np.linalg.norm(v)


def feats(**over) -> dict:
    base = {"log_dur": 2.0, "face_share": 0.0, "faces_med": 0.0, "multi_share": 0.0, "face_area_med": 0.0, "face_area_max": 0.0, "partial_share": 0.0,
            "hands_share": 0.0, "face_drift": 0.0, "speech_share": 0.0, "log_speech_s": 0.0, "rms": -30.0, "shift_med": 0.1, "shift_p90": 0.2,
            "response_med": 0.1, "diff_med": 0.05, "static_share": 0.0, "steps": 10, "log_sharp": 5.0, "bright": 100.0,
            "p_wide": 0.2, "p_close": 0.2, "p_interview": 0.15, "p_stage": 0.1, "p_talk": 0.25, "p_broll": 0.35, "emb": np.zeros(512, np.float32)}
    base.update(over)
    if "p_talk" not in over:
        base["p_talk"] = base["p_interview"] + base["p_stage"]
    return base


def speaker(**over) -> dict:
    """A green-screen interview: one face, locked off, speech, CLIP sure of the setup."""
    base = dict(speech_share=0.3, face_share=1.0, faces_med=1, face_area_med=0.006, shift_med=0.003, shift_p90=0.01, static_share=0.8, face_drift=0.01,
                p_interview=0.9, p_stage=0.05, p_wide=0.0, p_close=0.0, p_broll=0.05)
    base.update(over)
    return feats(**base)


class RuleTests(unittest.TestCase):
    def test_a_locked_off_speaker_is_a_talking_head(self):
        cls, conf, reasons, second = library.classify_rules(speaker())
        self.assertEqual(cls, "talking-head")
        self.assertGreaterEqual(conf, 0.5)
        self.assertEqual(second, "interview-bite")
        self.assertTrue(any("locked off" in r for r in reasons))

    def test_a_plain_backdrop_counts_as_locked_when_the_face_stays_put(self):
        # the shift follows the subject's gestures on a green screen: static 0.3, but the face barely moves
        self.assertEqual(library.classify_rules(speaker(shift_med=0.02, shift_p90=0.06, static_share=0.3, face_drift=0.03))[0], "talking-head")
        self.assertEqual(library.classify_rules(speaker(shift_med=0.02, shift_p90=0.2, static_share=0.3, face_drift=0.15, p_interview=0.6))[0], "interview-bite")

    def test_a_stage_mic_or_a_podium_is_a_bite_even_on_a_tripod(self):
        f = speaker(speech_share=0.9, p_interview=0.02, p_stage=0.95)
        cls, _, reasons, _ = library.classify_rules(f)
        self.assertEqual(cls, "interview-bite")
        self.assertTrue(any("a stage" in r for r in reasons))

    def test_a_speaker_with_the_camera_moving_is_a_bite_when_clip_sees_a_speaker(self):
        f = feats(speech_share=0.6, face_share=0.9, faces_med=1, face_area_med=0.01, shift_med=0.08, shift_p90=0.2, static_share=0.0, face_drift=0.1,
                  p_interview=0.1, p_stage=0.5)
        self.assertEqual(library.classify_rules(f)[0], "interview-bite")
        listener = feats(speech_share=1.0, face_share=1.0, faces_med=1, face_area_med=0.03, shift_med=0.03, shift_p90=0.05, static_share=0.0,
                         face_drift=0.03, p_interview=0.3, p_stage=0.0, p_broll=0.7)  # an audience close-up with the PA on the track
        self.assertNotIn(library.classify_rules(listener)[0], ("interview-bite", "talking-head"))

    def test_two_people_facing_a_locked_camera_can_be_a_talking_head_but_a_group_cannot(self):
        pair = speaker(faces_med=2, multi_share=1.0, speech_share=0.7)
        self.assertEqual(library.classify_rules(pair)[0], "talking-head")
        group = feats(speech_share=0.6, face_share=1.0, faces_med=4, multi_share=1.0, face_area_med=0.01, shift_med=0.003, static_share=0.9, p_interview=0.5)
        self.assertEqual(library.classify_rules(group)[0], "b-roll")

    def test_a_transcript_without_words_demotes_a_speaker(self):
        self.assertEqual(library.classify_rules(speaker())[0], "talking-head")
        self.assertEqual(library.classify_rules(speaker(), speaker=False)[0], "b-roll")

    def test_a_face_filling_the_frame_is_a_close_up(self):
        f = feats(face_share=1.0, faces_med=1, face_area_med=0.06)
        cls, conf, _, _ = library.classify_rules(f)
        self.assertEqual(cls, "close-up")
        self.assertGreaterEqual(conf, 0.6)

    def test_clip_decides_wide_and_detail_when_nobody_is_mid_frame(self):
        self.assertEqual(library.classify_rules(feats(p_wide=0.7, p_close=0.1, p_talk=0.1, p_broll=0.1))[0], "wide")
        self.assertEqual(library.classify_rules(feats(p_wide=0.1, p_close=0.7, p_talk=0.1, p_broll=0.1))[0], "close-up")

    def test_people_mid_frame_not_speaking_are_b_roll(self):
        f = feats(face_share=0.8, faces_med=1, face_area_med=0.012, p_broll=0.5, p_talk=0.2)
        self.assertEqual(library.classify_rules(f)[0], "b-roll")


class BiteTests(unittest.TestCase):
    def transcript(self):
        def seg(start, text):
            ws, t = [], start
            for w in text.split():
                ws.append({"w": w, "start": round(t, 2), "end": round(t + 0.3, 2), "p": 0.9})
                t += 0.4
            return {"start": start, "end": ws[-1]["end"], "text": text, "words": ws}

        # the first two sentences sit 0.6 s apart (one bite); the third comes after a 3.6 s pause
        return {"segments": [seg(1.0, "We loved the season."), seg(3.1, "The crowd was huge this year."), seg(9.0, "Next year we go bigger.")]}

    def test_sentences_close_together_form_one_bite_and_a_pause_splits(self):
        rec = {"fid": "f1", "duration_s": 20.0}
        bites = library.bites_for(rec, self.transcript())
        self.assertEqual(len(bites), 2)
        first, second = bites
        self.assertEqual(first["text"], "We loved the season. The crowd was huge this year.")
        self.assertEqual(second["text"], "Next year we go bigger.")
        self.assertLess(first["in"], 1.0)
        self.assertGreater(first["out"], first["end"])
        self.assertTrue(second["clean"])
        self.assertEqual(first["fumbles"], 0)

    def test_bites_from_a_fresh_transcript_with_numpy_timings_are_plain_json(self):
        data = self.transcript()
        for s in data["segments"]:
            s["start"], s["end"] = np.float64(s["start"]), np.float64(s["end"])
            for w in s["words"]:
                w["start"], w["end"] = np.float64(w["start"]), np.float64(w["end"])
        bites = library.bites_for({"fid": "f1", "duration_s": 20.0}, data)
        self.assertEqual(len(bites), 2)
        json.dumps(bites)  # numpy bools would raise here
        self.assertIs(type(bites[0]["clean"]), bool)

    def test_the_bites_xml_has_one_linked_clip_per_bite(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            rec = {"fid": "f1", "path": str(tmp / "A.MP4"), "basename": "A.MP4", "duration_s": 20.0, "fps": 25.0, "stored_w": 1920, "stored_h": 1080,
                   "display_w": 1920, "display_h": 1080, "audio_channels": 2}
            bites = library.bites_for(rec, self.transcript())
            out = library.write_bites_xml(bites, {"f1": rec}, tmp / "bites.xml")
            seqs = xmeml.load(out)
            video = [t for t in seqs[0].tracks if t.media == "video"][0]
            audio = [t for t in seqs[0].tracks if t.media == "audio"]
            self.assertEqual(len(video.clips), 2)
            self.assertTrue(audio and len(audio[0].clips) == 2)
            self.assertIn("We loved the season", video.clips[0].name)
            self.assertEqual(video.clips[0].source_in, round(bites[0]["in"] * 25))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CandidateTests(unittest.TestCase):
    """Which takes a shot map can use, by the library's classes."""

    def setUp(self):
        self.entries = [{"fid": f, "path": f"/V/{n}", "basename": n} for f, n in (("a", "C0001.MP4"), ("b", "C0002.MP4"), ("c", "C0003.MP4"), ("d", "C0004.MP4"))]
        self.classes = {"a": {"class": "wide"}, "b": {"class": "close-up"}, "c": {"class": "talking-head"}}  # d was never scanned

    def test_a_map_without_classes_keeps_every_take(self):
        m = {"sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x"}]}]}
        self.assertEqual(library.wanted_classes(m), set())
        self.assertEqual(library.candidates(self.entries, self.classes, m), self.entries)

    def test_map_and_slot_classes_narrow_the_pool_and_unscanned_takes_stay(self):
        m = {"classes": ["wide"], "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "class": "close-up"}]}]}
        self.assertEqual(library.wanted_classes(m), {"wide", "close-up"})
        self.assertEqual([e["fid"] for e in library.candidates(self.entries, self.classes, m)], ["a", "b", "d"])

    def test_a_byte_slot_admits_the_speech_classes_and_named_takes_always_pass(self):
        m = {"classes": ["wide"], "sections": [{"name": "s", "slots": [{"id": "b-1", "show": "x", "byte": True, "takes": ["C0003*@1-3"]}]}]}
        self.assertEqual(library.wanted_classes(m), {"wide", "talking-head", "interview-bite"})
        m2 = {"classes": ["wide"], "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "takes": ["C0002*"]}, {"id": "s-2", "show": "y"}]}]}
        self.assertEqual([e["fid"] for e in library.candidates(self.entries, self.classes, m2)], ["a", "b", "d"])

    def test_a_cut_decodes_no_take_the_map_skips_and_only_the_named_ones_when_every_slot_names_its_takes(self):
        slots = [{"id": "s-1", "show": "x", "takes": ["C0002*", "C0003*@1-2"]}, {"id": "s-2", "show": "y", "takes": ["C0004*"]}, {"id": "s-3", "black": True}]
        m = {"classes": ["wide"], "sections": [{"name": "s", "slots": slots}]}
        self.assertEqual([e["fid"] for e in library.candidates(self.entries, self.classes, m)], ["b", "c", "d"])
        m = {"skip": ["/V/C0001*", "*/C0004.MP4"], "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x"}]}]}
        self.assertEqual([e["fid"] for e in library.candidates(self.entries, self.classes, m)], ["b", "c"])

    def test_the_shot_map_validator_knows_the_classes(self):
        from videtor.plan import shotmap

        base = {"version": 1, "length_s": 10, "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "beats": 1, "class": "b-roll"}]}]}
        self.assertEqual(shotmap.validate({**base, "classes": ["wide", "close-up"]}), [])
        self.assertTrue(any("classes must be" in e for e in shotmap.validate({**base, "classes": ["wides"]})))
        bad = {**base, "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "beats": 1, "class": ["b-roll", "x"]}]}]}
        self.assertTrue(any("class must be" in e for e in shotmap.validate(bad)))


class FolderTests(unittest.TestCase):
    def test_link_names_keep_the_folder_when_two_cards_share_a_file_number(self):
        entries = [{"fid": "a", "path": "/V/Sam/Card 1/C0101.MP4", "basename": "C0101.MP4"}, {"fid": "b", "path": "/V/Sam/Card 2/C0101.MP4", "basename": "C0101.MP4"},
                   {"fid": "c", "path": "/V/Jatin/Card 1/C1380.MP4", "basename": "C1380.MP4"}]
        names = library.link_names(entries)
        self.assertEqual(names["a"], "C0101.MP4")
        self.assertEqual(names["b"], "Card-2__C0101.MP4")
        self.assertEqual(names["c"], "C1380.MP4")


class BuildTests(unittest.TestCase):
    """A library built from synthetic sparse records: folders, json, md, sheets, then a move, learn and a rebuild."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache, self.kb, self.out = self.tmp / "sparse", self.tmp / "kb", self.tmp / "lib"
        self.entries = []
        plan = [("wide", 4), ("close-up", 4), ("talking-head", 2)]
        k = 0
        for cls, n in plan:
            for _ in range(n):
                k += 1
                path = self.tmp / "drive" / f"C{k:04d}.MP4"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"\0")
                self.entries.append(self.make(path, cls))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, path: Path, cls: str) -> dict:
        fid = footage.file_id(str(path))
        n = 6
        emb = np.tile(class_emb(cls), (n, 1))
        talk = cls == "talking-head"
        faces = np.full(n, 1 if talk else 0, np.int8)
        area = np.full(n, 0.006 if talk else 0.0, np.float32)
        box = np.tile([0.45, 0.2, 0.55, 0.35] if talk else [np.nan] * 4, (n, 1)).astype(np.float32)
        arrays = {"t": np.arange(n, dtype=np.float32), "faces": faces, "face_area": area, "face_box": box, "partial": np.zeros(n, bool),
                  "hands": np.zeros(n, np.int8), "sharp": np.full(n, 200.0, np.float32), "bright": np.full(n, 100.0, np.float32),
                  "shift": np.full(n - 1, 0.002 if talk else 0.1, np.float32), "response": np.full(n - 1, 0.8 if talk else 0.1, np.float32),
                  "diff": np.full(n - 1, 0.05, np.float32), "dt": np.ones(n - 1, np.float32)}
        rec = {"fid": fid, "path": str(path), "basename": path.name, "duration_s": 12.0, "fps": 25.0, "codec": "h264", "stored_w": 1920, "stored_h": 1080,
               "display_w": 1920, "display_h": 1080, "rotation": 0, "audio_channels": 2, "n": n, "t": list(range(n)), "hw": False, "seek": False,
               "audio": {"audio": True, "speech_share": 0.5 if talk else 0.0, "speech_s": 6.0 if talk else 0.0, "spans": [], "rms_dbfs": -30.0},
               "motion": {"steps": n - 1, "shift_med": 0.002 if talk else 0.1, "shift_p90": 0.003 if talk else 0.2, "response_med": 0.8 if talk else 0.1,
                          "diff_med": 0.05, "static_share": 1.0 if talk else 0.0},
               "faces": {"share": 1.0 if talk else 0.0, "count_med": 1 if talk else 0, "area_med": 0.006 if talk else 0.0, "area_max": 0.006 if talk else 0.0,
                         "partial_share": 0.0, "hands_share": 0.0}, "sharp_med": 200.0, "bright_med": 100.0, "version": sparse.VERSION}
        sparse.save(rec, arrays, emb, self.cache)
        for i in range(n):
            p = sparse.still_path(self.cache, fid, i)
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 36), (40, 40, 40)).save(p)
        return {"fid": fid, "path": str(path), "basename": path.name, "kind": "video", "media": {"duration_s": 12.0, "video": {"fps": 25.0}, "audio": {"channels": 2}}}

    def test_build_sorts_into_folders_and_writes_the_records(self):
        lib = library.build(self.entries, self.out, self.cache, self.kb, log=lambda _: None, transcribe=False, embed_texts=fake_embed_texts, folders=["/V"])
        self.assertEqual(lib["counts"], {"wide": 4, "close-up": 4, "talking-head": 2})
        for c in library.CLASSES:
            self.assertTrue((self.out / c).is_dir())
        links = sorted(p.name for p in (self.out / "wide").iterdir())
        self.assertEqual(links, ["C0001.MP4", "C0002.MP4", "C0003.MP4", "C0004.MP4"])
        self.assertTrue((self.out / "wide" / "C0001.MP4").is_symlink())
        self.assertEqual(os.readlink(self.out / "wide" / "C0001.MP4"), self.entries[0]["path"])
        self.assertTrue((self.out / "library.md").exists())
        self.assertTrue(all(Path(p).exists() for p in lib["sheets"]))
        saved = json.loads((self.out / library.LIBRARY_JSON).read_text())
        self.assertEqual(len(saved["clips"]), 10)
        self.assertTrue(all(x["by"] == "rules" for x in saved["clips"]))

    def test_a_move_is_seen_learnt_and_kept_by_the_next_build(self):
        library.build(self.entries, self.out, self.cache, self.kb, log=lambda _: None, transcribe=False, embed_texts=fake_embed_texts)
        moved = self.out / "wide" / "C0001.MP4"
        os.rename(moved, self.out / "b-roll" / "C0001.MP4")
        st = library.status(self.out)
        self.assertEqual(list(st["moved"].values()), [("wide", "b-roll")])
        with self.assertRaises(RuntimeError):
            library.run_build([self.tmp / "drive"], self.out, cache_dir=self.cache, kb_dir=self.kb, log=lambda _: None)
        res = library.learn(self.out, self.cache, self.kb, log=lambda _: None, embed_texts=fake_embed_texts)
        self.assertEqual(res["labels"], 10)
        labels = json.loads(library.labels_path(self.kb).read_text())
        self.assertEqual(labels[self.entries[0]["path"]], "b-roll")
        self.assertIsNotNone(res["model"])  # 4 wide and 4 close-up: enough for two classes
        self.assertEqual(sorted(res["model"]["classes"]), ["close-up", "wide"])
        model = library.load_model(self.kb)
        text = library.prompt_embeddings(fake_embed_texts)
        p = library.predict(model, library.features(sparse.load(self.entries[5]["fid"], self.cache), text))
        self.assertAlmostEqual(sum(p.values()), 1.0, places=5)
        self.assertEqual(max(p, key=p.get), "close-up")
        lib = library.build(self.entries, self.out, self.cache, self.kb, log=lambda _: None, transcribe=False, embed_texts=fake_embed_texts)
        first = next(x for x in lib["clips"] if x["path"] == self.entries[0]["path"])
        self.assertEqual((first["class"], first["by"]), ("b-roll", "label"))
        self.assertTrue((self.out / "b-roll" / "C0001.MP4").is_symlink())
        self.assertFalse((self.out / "wide" / "C0001.MP4").exists())


if __name__ == "__main__":
    unittest.main()
