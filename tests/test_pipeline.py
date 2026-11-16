"""The pipeline end to end on a synthetic project: a shot map cut through `pipeline.cut` as the terminal and the app
do it, what crosses from the map to the picks, decisions kept across cuts, events, and the evaluation's measures."""

import io
import json
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from test_fill import fake_embed, fake_take
from videtor import events, pipeline
from videtor.plan import evaluate, shotmap

KEY = "proj::footage"


def write_project(root: Path, takes: list, m: dict) -> Path:
    """An index, a cached footage map for every take and the shot map, laid out as prep and a cut leave them."""
    kb, cache, out = root / "kb", root / "cache" / "footmap", root / "out" / m["name"]
    (kb / "footage").mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for t in takes:
        files.append({"fid": t.fid, "path": t.path, "basename": t.name, "dir": "/Volumes/T", "kind": "video", "used_by": {},
                      "media": {"duration_s": t.duration_s, "video": {"fps": t.fps, "width": t.width, "height": t.height}, "audio": {"channels": 2}}})
        np.savez(cache / f"{t.fid}.npz", **t.a)
        (cache / f"{t.fid}.json").write_text(json.dumps({"version": 1, "applied_rotation": 0, "duration_s": t.duration_s,
                                                         "display_w": t.display_w, "display_h": t.display_h, "n": len(t.a["t"])}))
    (kb / "footage" / "proj.json").write_text(json.dumps({"mount": "/Volumes/T", "roots": {KEY: ["/Volumes/T"]}, "files": files}))
    (out / "shotmap.json").write_text(json.dumps(m))
    return out / "shotmap.json"


def the_map(**extra) -> dict:
    return {
        "version": 1, "name": "proj", "key": KEY, "length_s": 6.0, "fps": 25, "width": 1080, "height": 1920,
        "sections": [
            {"name": "open", "slots": [{"id": "o1", "length_s": 1.0, "show": "axis 0", "effects": ["push-in"]},
                                       {"id": "o2", "length_s": 1.0, "show": "axis 1"}]},
            {"name": "run", "slots": [{"id": "r1", "length_s": 1.0, "show": "axis 2", "flicker": 5},
                                      {"id": "r2", "length_s": 1.0, "show": "axis 3", "effects": []},
                                      {"id": "r3", "length_s": 2.0, "show": "axis 4"}]},
        ],
        **extra,
    }


class CutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
        self.paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse",
                                    beats=self.root / "cache" / "beats")
        self.takes = takes

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cut(self, m: dict, **kw):
        seen: list[dict] = []
        path = write_project(self.root, self.takes, m) if not (self.root / "kb").exists() else self.root / "out" / m["name"] / "shotmap.json"
        path.write_text(json.dumps(m))
        result = pipeline.cut(path, sheets=False, sfx=False, paths=self.paths, ev=events.Events(seen.append), embed_texts=fake_embed, **kw)
        return result, seen

    def test_a_cut_writes_the_picks_and_the_xml_and_says_so_in_events(self):
        result, seen = self.cut(the_map(tpyo=1))
        out = self.root / "out" / "proj"
        self.assertEqual(result["filled"], 5)
        picks = pipeline.check_picks(json.loads((out / "picks.json").read_text()))
        self.assertEqual([s["pick"]["name"][1] for s in picks["slots"]], ["0", "1", "2", "3", "4"])  # each slot the take that shows it
        self.assertEqual(len(ET.parse(out / "proj.xml").getroot().findall(".//video/track")[0].findall("clipitem")), 4 + 5)  # r1 flickers 5 ways
        kinds = [e["event"] for e in seen]
        self.assertIn("stage", kinds)
        self.assertEqual([e["path"] for e in seen if e["event"] == "artifact" and e["kind"] == "xml"], [str(out / "proj.xml")])
        self.assertTrue(any(e["event"] == "warning" and "'tpyo'" in e["text"] for e in seen))  # an unknown key is said, not dropped

    def test_a_slots_own_effects_reach_the_cut_and_a_flicker_draws_none(self):
        always = {"style": "none", "rates": {"flash": 1.0, "push-in": 1.0, "bw_runs_per_min": 0}}
        result, _ = self.cut(the_map(effects=always))
        eff = {s["id"]: s["effects"] for s in result["picks"]["slots"]}
        self.assertEqual(eff["o1"], ["push-in"])  # the map's own list, not the draw
        self.assertEqual(eff["r2"], [])  # an empty list leaves the shot plain
        self.assertEqual(eff["r1"], [])  # a flicker takes none
        self.assertEqual(eff["o2"], ["flash", "push-in"])  # the rest draw at the rates given
        self.assertEqual(eff["r3"], ["flash", "push-in"])

    def test_a_second_look_is_remembered_and_the_next_cut_keeps_it(self):
        result, _ = self.cut(the_map())
        out = self.root / "out" / "proj"
        slot = next(s for s in result["picks"]["slots"] if s["id"] == "o2")
        first, runner_up = slot["pick"]["name"], slot["alternates"][0]["name"]
        pipeline.look(out / "picks.json", {"o2": 1}, {"o2": "sharper"}, paths=self.paths, by="you")
        d = json.loads((out / "decisions.json").read_text())
        self.assertEqual((d["swaps"]["o2"]["take"], d["swaps"]["o2"]["why"], d["swaps"]["o2"]["by"]), (runner_up, "sharper", "you"))
        result, seen = self.cut(the_map())  # a re-cut fills afresh, then puts the swap back
        slot = next(s for s in result["picks"]["slots"] if s["id"] == "o2")
        self.assertEqual(slot["pick"]["name"], runner_up)
        self.assertTrue(slot["second_look"]["replayed"])
        self.assertTrue(any("your changes from before, made again: 1 of 1" in e["text"] for e in seen))
        result, _ = self.cut(the_map(), fresh=True)
        self.assertEqual(next(s for s in result["picks"]["slots"] if s["id"] == "o2")["pick"]["name"], first)

    def test_a_swap_whose_take_is_gone_is_reported_not_guessed(self):
        result, _ = self.cut(the_map())
        (self.root / "out" / "proj" / "decisions.json").write_text(json.dumps({"version": 1, "swaps": {"o2": {"take": "ZZ.MP4"}, "nope": {"take": "A0.MP4"}}}))
        done, missed = pipeline.replay(result["picks"], pipeline.load_decisions(self.root / "out" / "proj"))
        self.assertEqual((done, sorted(missed)), ([], ["nope", "o2"]))

    def test_what_cannot_be_done_is_an_error_in_words(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.check_picks({"slots": [{"id": "a", "pick": {"path": "/x"}}, {"id": "a"}], "name": "x", "fps": 25, "width": 1, "height": 1})
        self.assertIn("repeated", str(ctx.exception))
        self.assertIn("in/out", str(ctx.exception))
        path = write_project(self.root, self.takes, {**the_map(), "key": "other::footage"})
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.cut(path, paths=self.paths, embed_texts=fake_embed)
        self.assertIn("No footage index lists", str(ctx.exception))

    def test_carry_takes_the_maps_settings_the_later_steps_read(self):
        picks = pipeline.carry({"sfx_skip": ["shutter"], "stabilize": True, "under_db": -9, "beds": [], "unknown": 1}, {})
        self.assertEqual(picks, {"sfx_skip": ["shutter"], "stabilize": True, "under_db": -9})


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class PromoTests(unittest.TestCase):
    """Promos end to end on a small made-up video: the reading, the choice, the picks, the XML and the rendered files.
    The faces, the cards, the words and the beats are stand-ins; ffmpeg is the real one."""

    @classmethod
    def setUpClass(cls):
        from test_promos import make_video

        cls.root = Path(tempfile.mkdtemp())
        cls.video = make_video(cls.root / "My Talk.mp4", seconds=36)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def make(self, source, **kw):
        from test_promos import said, sound

        seen = []
        opts = dict(count=2, length=8.0, frame="square", out_root=self.root / "promos", ev=events.Events(seen.append), detect=lambda rgb: [[0.6, 0.2, 0.8, 0.6]],
                    cards=lambda frames: np.zeros(len(frames), np.float32), track=lambda wav: sound(36.0, loud=[(12, 24)]),
                    transcribe=lambda path, fid, folder: said((2.0, "Hello."), (30.0, "Goodbye.")))
        return pipeline.promos(str(source), **{**opts, **kw}), seen

    def probe(self, path):
        import subprocess

        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, check=True)
        d = json.loads(out.stdout)
        v = next(x for x in d["streams"] if x["codec_type"] == "video")
        return v["width"], v["height"], float(d["format"]["duration"]), sum(x["codec_type"] == "audio" for x in d["streams"])

    def test_promos_from_a_file_are_finished_videos_and_premiere_xmls_and_another_swaps_one(self):
        result, seen = self.make(self.video)
        folder, doc = result["dir"], result["doc"]
        self.assertEqual((folder.name, doc["lead"], len(doc["promos"]), doc["options"]["frame"]), ("my-talk", "music", 2, "square"))
        first = doc["promos"][0]
        self.assertEqual((first["from_s"], first["length_s"]), ([[12.0, 20.0]], 8.0))  # from the cut where the sound lifts
        self.assertEqual(first["reasons"][:2], ["the loudest, punchiest stretch of the sound", "starts as the sound lifts"])
        for row in doc["promos"]:
            w, h, dur, sounds = self.probe(row["file"])
            self.assertEqual((w, h, sounds), (1080, 1080, 1))
            self.assertAlmostEqual(dur, row["length_s"], delta=0.12)
            x = ET.parse(row["xml"]).getroot()
            self.assertEqual(x.find("sequence/name").text, f"my-talk-promo-{row['n']}")
            self.assertEqual({e.text for e in x.iter("pathurl")}, {"file://localhost" + str(self.video.resolve()).replace(" ", "%20")})
            self.assertGreaterEqual(len(list(x.iter("clipitem"))), 2)  # the picture and its own sound
            picks = json.loads((folder / "work" / f"promo-{row['n']}.picks.json").read_text())
            self.assertTrue(all(s["byte"] and s["pick"]["motion"]["scale"] == 600.0 for s in picks["slots"]))  # 180 high fills 1080
        self.assertEqual([e["stage"] for e in seen if e["event"] == "stage"], ["picture", "sound", "speech", "choose", "render"])
        self.assertEqual([e["kind"] for e in seen if e["event"] == "artifact"], ["promo", "promo", "promos"])
        self.assertEqual(pipeline.promo_sets(self.root / "promos")[0]["name"], "my-talk")
        # the same video again in another shape: read from what was kept, the promos made anew
        again, seen = self.make(self.video, frame="vertical", count=1, detect=lambda rgb: 1 / 0)
        self.assertEqual((again["dir"], len(again["doc"]["promos"]), self.probe(again["doc"]["promos"][0]["file"])[:2]), (folder, 1, (1080, 1920)))
        self.assertEqual(again["doc"]["notes"], [])
        many, _ = self.make(self.video, count=6, length=12.0)
        self.assertTrue(len(many["doc"]["promos"]) < 6 and any("promos made, not 6" in n or "promo made, not 6" in n for n in many["doc"]["notes"]), many["doc"]["notes"])
        again, seen = self.make(self.video, frame="vertical", count=1, detect=lambda rgb: 1 / 0)
        self.assertFalse((folder / "promo-2.mp4").exists())  # the second promo of the earlier run is gone with it
        self.assertNotIn("picture", [e.get("stage") for e in seen])
        # another one: the next best stretch takes that promo's place
        before = again["doc"]["promos"][0]["from_s"]
        other, _ = self.make(folder, another=1)
        row = other["doc"]["promos"][0]
        self.assertNotEqual(row["from_s"], before)
        self.assertEqual((row["n"], other["doc"]["used"], Path(row["file"]).name), (1, [0, 1], "promo-1.mp4"))
        for _ in range(len(other["doc"]["candidates"]) - 2):
            self.make(folder, another=1)
        with self.assertRaises(pipeline.PipelineError) as ctx:
            self.make(folder, another=1)
        self.assertIn("no other stretch", str(ctx.exception))

    def test_promos_from_a_link_fetch_the_video_once_and_an_edit_runs_on_its_own_stretch_of_sound(self):
        fetched = []

        def fetch(url, out_dir, progress=None, log=print, browser=None):
            fetched.append((url, browser))
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.video, out_dir / "source.mp4")
            info = {"title": "A Film: Part 1!", "duration": 36.0, "url": url,
                    "heatmap": [{"start_time": t, "end_time": t + 1.0, "value": 1.0 if 24 <= t < 33 else 0.05} for t in range(36)]}
            (out_dir / "source.json").write_text(json.dumps(info))
            return {"path": out_dir / "source.mp4", "info": info}

        url = "https://www.youtube.com/watch?v=abc"
        result, seen = self.make(url, fetch=fetch, count=1, browser="chrome", out_root=self.root / "linked")
        doc, folder = result["doc"], result["dir"]
        self.assertEqual((folder.name, fetched, doc["source"]["url"], doc["replayed"]), ("a-film-part-1", [(url, "chrome")], url, True))
        self.assertTrue(23.0 <= doc["promos"][0]["from_s"][0][0] <= 26.0, doc["promos"][0]["from_s"])  # what viewers replayed, over the loud stretch
        self.assertIn("one of the most replayed parts on YouTube", doc["promos"][0]["reasons"])
        self.assertEqual([e["stage"] for e in seen if e["event"] == "stage"][0], "fetch")
        self.assertEqual(list((self.root / "linked").glob(".fetching-*")), [])
        edit, _ = self.make(url, fetch=fetch, out_root=self.root / "linked", count=1, kind="edit")
        self.assertEqual(len(fetched), 1)  # the second time the file on the Mac is used
        row = edit["doc"]["promos"][0]
        self.assertEqual((row["kind"], row["lead"], row["shots"] >= 4, len(row["from_s"]) == row["shots"]), ("edit", "music", True, True))
        x = ET.parse(row["xml"]).getroot()
        self.assertEqual({Path(e.text).name for e in x.iter("pathurl")}, {"source.mp4", "promo-1-sound.wav"})  # the shots, and the bed as a file of its own
        self.assertEqual((self.probe(row["file"])[3], (folder / "promo-1-sound.wav").exists()), (1, True))
        self.assertAlmostEqual(self.probe(row["file"])[2], row["bed_s"][1] - row["bed_s"][0], delta=0.12)

    def test_what_cannot_be_made_is_said(self):
        from videtor.analyze import link

        for kw, words in ((dict(frame="round"), "the shape must be one of"), (dict(count=9), "between 1 and 6"), (dict(length=300.0), "between 8 and 90")):
            with self.assertRaises(pipeline.PipelineError) as ctx:
                self.make(self.video, **kw)
            self.assertIn(words, str(ctx.exception))
        with self.assertRaises(pipeline.PipelineError) as ctx:
            self.make(self.root / "nothing.mp4")
        self.assertIn("give a link", str(ctx.exception))

        def refused(url, out_dir, **kw):
            raise link.LinkError("Instagram wants a login for this video")

        with self.assertRaises(pipeline.PipelineError) as ctx:
            self.make("https://www.instagram.com/reel/xyz/", fetch=refused, out_root=self.root / "refused")
        self.assertIn("wants a login", str(ctx.exception))


class EventTests(unittest.TestCase):
    def test_sentences_for_a_terminal_and_json_lines_for_the_app(self):
        said: list[str] = []
        ev = events.Events(events.text_sink(said.append))
        ev.stage("decode", "footage map: decoding 3 takes", "Reading those takes in full")
        ev.progress(2, 3)  # no sentence: a terminal prints nothing for it
        ev.wrote("xml", "/x/a.xml", "Premiere XML: /x/a.xml")
        self.assertEqual(said, ["footage map: decoding 3 takes", "Premiere XML: /x/a.xml"])
        buf = io.StringIO()
        ev = events.Events(events.json_sink(buf))
        ev.stage("decode", "footage map: decoding 3 takes", "Reading those takes in full")
        ev.progress(2, 3)
        lines = buf.getvalue().splitlines()
        self.assertEqual(events.parse(lines[0])["label"], "Reading those takes in full")
        self.assertEqual((events.parse(lines[1])["done"], events.parse(lines[1])["total"]), (2, 3))
        self.assertIsNone(events.parse("WARNING: something a library printed"))
        self.assertIsNone(events.parse('{"event" broken'))

    def test_unknown_shot_map_keys_come_with_the_key_they_resemble(self):
        m = the_map(efects="sports-promo")
        m["sections"][0]["slots"][0]["flickr"] = 3
        w = shotmap.warnings(m)
        self.assertTrue(any("'efects'" in x and "'effects'" in x for x in w))
        self.assertTrue(any(x.startswith("o1") and "'flicker'" in x for x in w))
        self.assertEqual(shotmap.warnings(the_map()), [])


class EvaluateTests(unittest.TestCase):
    def test_neighbours_count_repeats_lookalikes_and_jumps_in_light(self):
        a, b, c, d = fake_take("A", 0), fake_take("B", 0), fake_take("C", 1), fake_take("D", 2)
        d.a["bright"][:] = 20.0  # a night shot
        pick = lambda t: {"fid": t.fid, "name": t.name, "in_s": 1.0, "out_s": 2.0}  # noqa: E731
        slots = [{"id": f"s{k}", "section": "one", "pick": pick(t)} for k, t in enumerate([a, a, b, c, d])]
        nb = evaluate.neighbours({"slots": slots}, {t.fid: t for t in (a, b, c, d)})
        self.assertEqual((nb["repeats"], nb["lookalikes"], nb["light_jumps"]), (1, 1, 1))  # a|a, a|b alike, c|d day to night
        slots[4]["section"] = "two"  # a new section may change the light
        self.assertEqual(evaluate.neighbours({"slots": slots}, {t.fid: t for t in (a, b, c, d)})["light_jumps"], 0)

    def test_a_cut_against_your_edit_and_a_run_against_the_baseline(self):
        slots = [{"id": "s1", "pick": {"name": "C0001.MP4", "in_s": 2.0, "out_s": 3.0}, "alternates": []},
                 {"id": "s2", "pick": {"name": "C0002(1).MP4", "in_s": 0.0, "out_s": 1.0}, "alternates": [{"name": "C0009.MP4"}]},
                 {"id": "s3", "pick": {"name": "C0003.MP4", "in_s": 0.0, "out_s": 1.0}, "alternates": [{"name": "c0001.mp4"}]}]
        used = {"c0001.mp4": [(2.5, 4.0)], "c0002.mp4": [(5.0, 6.0)]}
        self.assertEqual(evaluate.against_edit({"slots": slots}, used), {"right_take": 0.667, "right_second": 0.333, "in_top3": 1.0})
        base = {"projects": {"p": {"right_take": 0.5, "match": 2.0, "repeats": 0, "lookalikes": 1}}}
        rows, ok = evaluate.compare({"p": {"right_take": 0.48, "match": 2.2, "repeats": 0, "lookalikes": 2}}, base)
        self.assertTrue(ok)  # inside the tolerances
        self.assertEqual({r["measure"]: r["verdict"] for r in rows}, {"right_take": "within tolerance", "match": "better", "lookalikes": "within tolerance"})
        rows, ok = evaluate.compare({"p": {"right_take": 0.4, "match": 2.0, "repeats": 1, "lookalikes": 1}}, base)
        self.assertFalse(ok)
        self.assertEqual({r["measure"] for r in rows if r["verdict"] == "worse"}, {"right_take", "repeats"})

    def test_a_run_measures_each_project_of_the_set_from_the_cache(self):
        root = Path(tempfile.mkdtemp())
        try:
            takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
            path = write_project(root, takes, the_map())
            paths = pipeline.Paths(kb=root / "kb", footmap=root / "cache" / "footmap", beats=root / "cache" / "beats")
            res = evaluate.run([{"name": "proj", "shotmap": str(path)}, {"name": "gone", "shotmap": str(root / "nope.json")}], paths, log=lambda s: None,
                               embed_texts=fake_embed)
            self.assertEqual(list(res), ["proj"])
            self.assertEqual((res["proj"]["filled"], res["proj"]["repeats"]), (1.0, 0))
            self.assertGreater(res["proj"]["match"], 1.0)
            evaluate.save_baseline(res, root / "kb", "first")
            self.assertEqual(evaluate.compare(res, evaluate.load_baseline(root / "kb")), ([], True))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
