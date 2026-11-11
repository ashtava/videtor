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
    (kb / "footage").mkdir(parents=True)
    cache.mkdir(parents=True)
    out.mkdir(parents=True)
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
        self.assertTrue(any("second look kept from before: 1 of 1" in e["text"] for e in seen))
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
