"""The app's timeline: the edits a person makes to a cut (swap, slip, roll, remove, replace, undo), the small
preview clips it plays, the picks as the page draws them, and the edits kept for the next cut and as feedback."""

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_fill import fake_embed, fake_take
from test_pipeline import the_map, write_project
from videtor import pipeline
from videtor.export import proxy
from videtor.kb import feedback
from videtor.plan import edit


class Cut(unittest.TestCase):
    """A synthetic project cut once: o1, o2 plain; r1 a flicker; r2, r3 plain (see test_pipeline.the_map)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.takes = [fake_take(f"A{k}", k) for k in range(5)] + [fake_take(f"B{k}", k) for k in range(5)] + [fake_take(f"C{k}", 2) for k in range(4)]
        self.paths = pipeline.Paths(kb=self.root / "kb", footmap=self.root / "cache" / "footmap", sparse=self.root / "cache" / "sparse",
                                    beats=self.root / "cache" / "beats")
        self.sm = write_project(self.root, self.takes, the_map())
        self.out = self.sm.parent
        self.picks_p = self.out / "picks.json"
        self.cut()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def cut(self, **kw) -> dict:
        return pipeline.cut(self.sm, sheets=False, sfx=False, paths=self.paths, embed_texts=fake_embed, **kw)["picks"]

    def picks(self) -> dict:
        return json.loads(self.picks_p.read_text())

    def slot(self, sid: str, picks: dict | None = None) -> dict:
        return next(s for s in (picks or self.picks())["slots"] if s["id"] == sid)


class EditTests(Cut):
    def test_a_slip_moves_the_shot_in_its_take_by_whole_frames_and_stops_at_the_takes_ends(self):
        p = self.picks()
        before = dict(self.slot("o1", p)["pick"])
        self.assertIn("slipped +5 frame(s)", edit.slip(p, "o1", 5))
        after = self.slot("o1", p)["pick"]
        self.assertAlmostEqual(after["in_s"] - before["in_s"], 5 / 25)
        self.assertAlmostEqual(after["out_s"] - before["out_s"], 5 / 25)  # the same length, a later moment
        edit.slip(p, "o1", -100000)
        self.assertLess(self.slot("o1", p)["pick"]["in_s"], 1 / 25)  # as far as the take goes, in whole frames
        with self.assertRaises(edit.EditError):
            edit.slip(p, "o1", -1)  # no footage before its first frame
        with self.assertRaises(edit.EditError) as ctx:
            edit.slip(p, "r1", 2)
        self.assertIn("flicker", str(ctx.exception))  # its timing is the plan's

    def test_a_roll_moves_one_cut_and_nothing_else(self):
        p = self.picks()
        p["flashes"] = [{"start_s": 1.0 - 1 / 25, "kind": "flash"}, {"start_s": 3.0, "kind": "flash"}]  # one on the cut after o1
        a, b = dict(self.slot("o1", p)), dict(self.slot("o2", p))
        a_out, b_in, end = a["pick"]["out_s"], b["pick"]["in_s"], b["start_s"] + b["length_s"]
        self.assertIn("moved +3 frame(s)", edit.roll(p, "o1", 3))
        a2, b2 = self.slot("o1", p), self.slot("o2", p)
        self.assertAlmostEqual(a2["length_s"], a["length_s"] + 3 / 25)
        self.assertAlmostEqual(a2["pick"]["out_s"], a_out + 3 / 25)
        self.assertAlmostEqual(b2["start_s"], b["start_s"] + 3 / 25)
        self.assertAlmostEqual(b2["pick"]["in_s"], b_in + 3 / 25)  # the next shot loses its first frames, it does not slide
        self.assertAlmostEqual(b2["start_s"] + b2["length_s"], end)  # everything after stays where it was
        self.assertAlmostEqual(p["flashes"][0]["start_s"], 1.0 - 1 / 25 + 3 / 25)  # the flash on that cut goes with it
        self.assertEqual(p["flashes"][1]["start_s"], 3.0)
        edit.roll(p, "o1", 1000)
        self.assertAlmostEqual(self.slot("o2", p)["length_s"], 2 / 25)  # a shot keeps two frames
        for sid in ("o2", "r3"):  # into a flicker; after the last shot
            with self.assertRaises(edit.EditError):
                edit.roll(p, sid, 1)

    def test_a_removed_shots_time_goes_to_the_shot_before_it_or_the_one_after(self):
        p = self.picks()
        o1, o2 = dict(self.slot("o1", p)), dict(self.slot("o2", p))
        self.assertIn("o1 runs on over it", edit.remove(p, "o2"))
        self.assertEqual([s["id"] for s in p["slots"]], ["o1", "r1", "r2", "r3"])
        self.assertAlmostEqual(self.slot("o1", p)["length_s"], o1["length_s"] + o2["length_s"])
        p = self.picks()
        first = dict(self.slot("o1", p))
        self.slot("o2", p)["pick"].update(in_s=4.0, out_s=5.0)  # footage to spare before it
        nxt_in = self.slot("o2", p)["pick"]["in_s"]
        self.assertIn("o2 starts earlier", edit.remove(p, "o1"))
        self.assertEqual(self.slot("o2", p)["start_s"], 0.0)
        self.assertAlmostEqual(self.slot("o2", p)["pick"]["in_s"], nxt_in - first["length_s"])
        p = self.picks()
        for sid in ("o1", "o2"):
            self.slot(sid, p)["pick"].update(in_s=0.0, duration_s=1.0, out_s=1.0)  # neither has footage to spare
        with self.assertRaises(edit.EditError):
            edit.remove(p, "o1")

    def test_a_swap_and_a_replace_change_the_take_and_keep_the_old_one_within_reach(self):
        p = self.picks()
        s = self.slot("o2", p)
        old, runner = s["pick"]["name"], s["alternates"][0]["name"]
        self.assertEqual(edit.apply(p, {"op": "swap", "slot": "o2", "take": runner}), f"o2: {runner} instead of {old}")
        self.assertEqual(self.slot("o2", p)["alternates"][0]["name"], old)
        other = {"fid": "Z9", "path": "/Volumes/T/Z9.MP4", "name": "Z9.MP4", "in_s": 2.0, "out_s": 9.0, "speed": 1.0, "duration_s": 10.0, "thumb": "x"}
        edit.apply(p, {"op": "replace", "slot": "o2", "choice": other, "why": "closer"})
        s = self.slot("o2", p)
        self.assertEqual((s["pick"]["name"], s["pick"]["out_s"], s["second_look"]["why"]), ("Z9.MP4", 2.0 + s["length_s"], "closer"))
        self.assertNotIn("thumb", s["pick"])
        self.assertEqual(s["alternates"][0]["name"], runner)  # what it replaced is the first runner-up now
        with self.assertRaises(edit.EditError):
            edit.apply(p, {"op": "replace", "slot": "o2", "choice": {**other, "in_s": 9.9}})  # the take ends before the shot would
        with self.assertRaises(edit.EditError):
            edit.apply(p, {"op": "fly", "slot": "o2"})

    def test_the_picks_as_the_page_draws_them(self):
        pipeline.note(self.out, "o2", "  too   long ")
        pipeline.note(self.out, None, "slower opening")
        tl = edit.timeline(self.picks(), self.out, [self.paths.footmap / "frames"], self.paths.beats)
        self.assertEqual(tl["length_s"], 6.0)
        self.assertEqual(len(tl["slots"]), 5)
        self.assertEqual(len(tl["clips"]), 4 + 5)  # the flicker is five clips of one slot
        self.assertEqual([round(c["start_s"], 2) for c in tl["clips"]][:4], [0.0, 1.0, 2.0, 2.2])
        by = {s["id"]: s for s in tl["slots"]}
        self.assertEqual(by["o1"]["can"], {"slip": True, "roll": True, "remove": True, "swap": True, "replace": True})
        self.assertEqual((by["r1"]["kind"], by["r1"]["can"]["slip"], by["o2"]["can"]["roll"]), ("flicker", False, False))  # o2's cut joins a flicker
        self.assertEqual((by["o2"]["note"], tl["overall"]), ("too long", "slower opening"))
        self.assertEqual([s["name"] for s in tl["sections"]], ["open", "run"])
        self.assertEqual((tl["missing"], tl["dirty"], tl["has_xml"]), (9, False, True))  # no preview clips yet; the XML is the cut's


class ProxyTests(Cut):
    def fake_render(self, choice, width, height, fps, out_dir, audio=False):
        a, b = proxy.span(choice)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"{choice['fid']}-{a:.2f}.mp4"
        (out_dir / name).write_bytes(b"x")
        return {"file": name, "sig": proxy.signature(choice, width, height), "fid": choice["fid"], "src_a": a, "src_b": b, "speed": 1.0, "audio": audio, "duration_s": b - a}

    def test_preview_clips_are_made_once_and_cover_an_edit_within_their_handles(self):
        with mock.patch.object(proxy, "render", side_effect=self.fake_render) as render:
            out = proxy.ensure(self.picks(), self.out, workers=1)
            self.assertEqual((out["made"], out["had"], out["failed"]), (9, 0, []))
            self.assertEqual(proxy.ensure(self.picks(), self.out, workers=1)["made"], 0)  # all there
            self.assertEqual(render.call_count, 9)
            pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 10}, self.paths)  # 0.4 s: inside the 0.8 s handle
            tl = edit.timeline(self.picks(), self.out, [], self.paths.beats)
            clip = next(c for c in tl["clips"] if c["slot"] == "o1")
            self.assertAlmostEqual(clip["proxy"]["offset_s"], self.slot("o1")["pick"]["in_s"] - proxy.span(self.slot("o1", json.loads((self.out / "work" / "picks.cut.json").read_text()))["pick"])[0])
            self.assertEqual(tl["missing"], 0)  # nothing to make again
            pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 40}, self.paths)  # past the handle
            self.assertEqual(edit.timeline(self.picks(), self.out, [], self.paths.beats)["missing"], 1)
            self.assertEqual(proxy.ensure(self.picks(), self.out, slots={"o1"}, workers=1)["made"], 1)  # only that shot's
            self.assertGreaterEqual(proxy.ensure(self.picks(), self.out, slots={"o2"}, alternates=True, workers=1)["made"], 1)  # its runners-up too

    def test_a_take_that_is_not_there_is_said_not_fatal(self):
        said = []
        out = proxy.ensure(self.picks(), self.out, slots={"o1"}, workers=1, log=said.append)
        self.assertEqual((out["made"], out["failed"]), (0, [self.slot("o1")["pick"]["name"]]))
        self.assertIn("no preview clip", said[0])

    def test_a_proxys_signature_is_the_take_its_speed_its_framing_and_the_frame(self):
        c = self.slot("o1")["pick"]
        self.assertNotEqual(proxy.signature(c, 1080, 1920), proxy.signature(c, 1920, 1080))
        self.assertNotEqual(proxy.signature(c, 1080, 1920), proxy.signature({**c, "speed": 0.5}, 1080, 1920))
        a, b = proxy.span({**c, "in_s": 0.3, "out_s": 1.0, "speed": 0.5, "duration_s": 1.2})
        self.assertEqual((a, round(b, 2)), (0.0, 1.16))  # 0.8 s of timeline is 0.4 s of a half-speed take; never past its ends


class PipelineTests(Cut):
    def test_edits_are_made_at_once_undone_in_order_and_made_again_by_the_next_cut(self):
        first = self.picks()
        out = pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        self.assertIn("slipped", out["line"])
        runner = self.slot("o2")["alternates"][0]["name"]
        pipeline.edit(self.picks_p, {"op": "swap", "slot": "o2", "k": 1}, self.paths)
        d = json.loads((self.out / "decisions.json").read_text())
        self.assertEqual([(e["op"], e["slot"], e.get("take"), e["by"]) for e in d["edits"]], [("slip", "o1", None, "you"), ("swap", "o2", runner, "you")])
        self.assertNotIn("k", d["edits"][1])  # remembered by the take's name
        edited = self.picks()
        again = self.cut()  # a fresh fill, then the same edits
        self.assertEqual(self.slot("o1", again)["pick"]["in_s"], self.slot("o1", edited)["pick"]["in_s"])
        self.assertEqual(self.slot("o2", again)["pick"]["name"], runner)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)  # undo reaches back to this cut, not past it
        self.assertEqual(len(json.loads((self.out / "decisions.json").read_text())["edits"]), 2)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.edit(self.picks_p, {"op": "slip", "slot": "nope", "frames": 1}, self.paths)
        fresh = self.cut(fresh=True)
        self.assertEqual(self.slot("o1", fresh)["pick"]["in_s"], self.slot("o1", first)["pick"]["in_s"])
        self.assertFalse((self.out / "decisions.json").exists())

    def test_undo_takes_back_one_edit_at_a_time_to_the_cut_as_made(self):
        first = self.picks()
        pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        pipeline.edit(self.picks_p, {"op": "roll", "slot": "o1", "frames": 2}, self.paths)
        pipeline.edit(self.picks_p, {"op": "remove", "slot": "o2"}, self.paths)
        self.assertEqual(len(self.picks()["slots"]), 4)
        for _ in range(3):
            pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)
        self.assertEqual(self.picks(), first)
        self.assertEqual(json.loads((self.out / "decisions.json").read_text())["edits"], [])
        with self.assertRaises(pipeline.PipelineError):
            pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)

    def test_writing_the_xml_keeps_the_net_change_as_the_users_choices(self):
        runner = self.slot("o2")["alternates"][0]["name"]
        pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        pipeline.edit(self.picks_p, {"op": "swap", "slot": "o2", "k": 1}, self.paths)
        self.assertEqual(feedback.all_docs(self.paths.kb), [])  # nothing is taught by a click
        pipeline.write_xml(self.picks_p, self.paths)
        rows = {r["id"]: r for r in feedback.teaching_rows(feedback.load("proj", self.paths.kb))}
        self.assertEqual({k: r["status"] for k, r in rows.items()}, {"o1": "slipped", "o2": "swapped_to_alternate"})
        self.assertEqual(rows["o2"]["chosen"]["name"], runner)
        pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)  # the swap, taken back
        pipeline.write_xml(self.picks_p, self.paths)
        self.assertEqual([r["id"] for r in feedback.teaching_rows(feedback.load("proj", self.paths.kb))], ["o1"])  # what stands now, not the history
        self.assertFalse(edit.timeline(self.picks(), self.out, [], self.paths.beats)["dirty"])

    def test_the_xml_notes_the_picks_it_was_written_from_and_edited_means_they_moved_on(self):
        dirty = lambda: edit.timeline(self.picks(), self.out, [], self.paths.beats)["dirty"]  # noqa: E731
        rec = json.loads((self.out / "work" / pipeline.XML_RECORD).read_text())
        self.assertEqual((rec["xml"], rec["picks_sha1"]), ("proj.xml", hashlib.sha1(self.picks_p.read_bytes()).hexdigest()))
        self.assertFalse(dirty())
        later = time.time() + 30
        os.utime(self.picks_p, (later, later))  # copied, or a clock off: the same picks, a newer file
        self.assertFalse(dirty())
        pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        self.assertTrue(dirty())
        pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)  # back to the picks the XML holds
        self.assertFalse(dirty())
        pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        pipeline.look(self.picks_p, {}, paths=self.paths)
        self.assertFalse(dirty())
        (self.out / "work" / pipeline.XML_RECORD).unlink()  # a cut written before the note: the files' times decide, as before
        os.utime(self.picks_p, (later, later))
        self.assertTrue(dirty())
        os.utime(self.picks_p, (time.time() - 30,) * 2)
        self.assertFalse(dirty())

    def test_each_file_is_written_whole_and_a_failure_never_leaves_picks_the_decisions_do_not_describe(self):
        first = self.picks()
        real = pipeline._write

        def picks_fail(path, text):
            if path.name == "picks.json":
                raise OSError("the drive went away")
            real(path, text)

        with mock.patch.object(pipeline, "_write", side_effect=picks_fail):
            with self.assertRaises(OSError):
                pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        self.assertEqual(self.picks(), first)  # the picks as they were; the decisions one edit ahead, with the way back
        self.assertEqual(len(json.loads((self.out / "decisions.json").read_text())["edits"]), 1)
        pipeline.edit(self.picks_p, {"op": "undo"}, self.paths)  # puts both back
        self.assertEqual((self.picks(), json.loads((self.out / "decisions.json").read_text())["edits"]), (first, []))
        with mock.patch("os.replace", side_effect=OSError("no room")):  # a write that fails half way leaves the file as it was
            with self.assertRaises(OSError):
                pipeline.note(self.out, "o1", "darker")
            with self.assertRaises(OSError):
                pipeline.edit(self.picks_p, {"op": "slip", "slot": "o1", "frames": 5}, self.paths)
        self.assertEqual(self.picks(), first)
        self.assertFalse((self.out / "review.json").exists())
        self.assertEqual([p.name for p in self.out.rglob(".*.tmp")], [])  # and nothing half written beside it

    def test_more_takes_for_a_shot_come_in_the_users_words_with_the_ones_in_the_cut_last(self):
        doc = pipeline.options(self.picks_p, "o2", show="axis 0", n=6, paths=self.paths, embed_texts=fake_embed)
        names = [r["name"] for r in doc["rows"]]
        self.assertNotIn(self.slot("o2")["pick"]["name"], names)
        self.assertEqual(names[0], "B0.MP4")  # shows axis 0 and no other shot uses it
        flags = [bool(r.get("used_in")) for r in doc["rows"]]
        self.assertEqual(flags, sorted(flags))  # takes already in the cut come last
        self.assertEqual(next(r for r in doc["rows"] if r["name"] == "A0.MP4")["used_in"], "o1")  # and are said to be
        out = pipeline.edit(self.picks_p, {"op": "replace", "slot": "o2", "option": 0}, self.paths)
        self.assertIn("B0.MP4 instead of", out["line"])
        self.assertEqual(self.slot("o2")["pick"]["name"], "B0.MP4")
        self.assertIsInstance(json.loads((self.out / "decisions.json").read_text())["edits"][0]["choice"], dict)  # the next cut can make it again
        with self.assertRaises(pipeline.PipelineError):
            pipeline.edit(self.picks_p, {"op": "replace", "slot": "o2", "option": 99}, self.paths)


if __name__ == "__main__":
    unittest.main()
