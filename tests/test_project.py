import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from videtor import pipeline, project
from videtor.project import Kind, Stage

PLAN = {"version": 1, "name": "demo", "key": "demo::footage", "length_s": 10, "sections": [{"name": "s", "slots": [{"id": "s-1", "show": "x", "beats": 4}]}]}
CUTTING = {"kind": "cut", "status": "running", "title": "Cut · demo", "writes": True, "progress": {"done": 3, "total": 12}}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.d = self.tmp / "out" / "demo"
        self.d.mkdir(parents=True)
        self.footage = self.tmp / "shoot"
        self.footage.mkdir()
        project.forget()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, name: str, text) -> None:
        (self.d / name).write_text(text if isinstance(text, str) else json.dumps(text))

    def read(self, folder: str | None = None) -> project.Project:
        return project.read(self.d, str(self.footage) if folder is None else folder, "demo")

    def test_every_stage_from_a_folder_with_its_card_and_what_comes_next(self):
        seen = []
        for step in ("nothing", "overview.md", "brief.md", "bad plan", "shotmap.json", "picks.json"):
            if step == "bad plan":
                self.put("shotmap.json", {"version": 1, "sections": []})
            elif step == "shotmap.json":
                self.put("shotmap.json", PLAN)
            elif step != "nothing":
                self.put(step, "x" if step != "picks.json" else {"name": "demo", "slots": []})
            p = self.read()
            seen.append((p.stage, p.stage.step, project.status(p)["text"], project.steps(p)[1]["text"]))
        self.assertEqual(seen, [
            (Stage.FOOTAGE, "footage", "Footage not read", "The footage has not been read yet."),
            (Stage.BRIEF, "brief", "Needs the brief", "Paste the client's brief."),
            (Stage.PLAN, "plan", "Needs the plan", "Get the plan written from the brief: copy the request on Brief & plan into Claude Code."),
            (Stage.PROBLEMS, "plan", "The plan has problems", "Fix the plan: 2 problem(s), listed under Edit the plan."),
            (Stage.READY, "cut", "Ready to cut", "Cut it."),
            (Stage.CUT, "review", "Cut ready for Premiere", "Review the shots, then import the XML into Premiere."),
        ])
        steps, _ = project.steps(self.read())
        self.assertEqual([s["state"] for s in steps], ["done", "done", "optional", "done", "todo"])

    def test_the_furthest_step_decides_and_an_empty_brief_is_no_brief(self):
        self.put("overview.md", "x")
        self.put("brief.md", "  \n")
        self.assertEqual(self.read().stage, Stage.BRIEF)
        self.put("picks.json", {"name": "demo"})  # a cut made in the terminal, with no plan
        self.assertEqual((self.read().stage, project.steps(self.read())[0][3]["state"]), (Stage.CUT, "done"))

    def test_a_job_running_is_said_on_the_card_and_in_the_steps(self):
        self.put("overview.md", "x")
        self.put("shotmap.json", PLAN)
        p = self.read()
        self.assertEqual(project.status(p, CUTTING), {"text": "Cutting · 25%", "tone": "busy"})
        self.assertEqual(project.status(p, {**CUTTING, "kind": "prep", "status": "paused"})["text"], "Reading the footage · paused for a cut")
        self.assertEqual(project.status(p, {**CUTTING, "kind": "odd", "status": "waiting"})["text"], "Working · waits for a cut")
        steps, nxt = project.steps(p, CUTTING)
        self.assertEqual((steps[3]["state"], nxt["text"]), ("running", "Cutting. The review opens when it is done."))
        steps, nxt = project.steps(self.read(), {**CUTTING, "kind": "prep"})
        self.assertEqual((steps[0]["state"], nxt["tab"]), ("running", "brief"))

    def test_the_three_kinds_and_what_each_needs_to_be_cut(self):
        self.assertEqual(self.read().kind, Kind.CUT)
        self.assertEqual(project.can(self.read(), "cut"), (False, "No plan yet: write it on Brief & plan first."))
        self.put("aroll.json", {})
        p = self.read()
        self.assertEqual(p.kind, Kind.INTERVIEW)
        self.assertFalse(project.can(p, "cut")[0])
        self.put("shotmap.json", {"version": 1})  # an interview's plan is not held to the shot map's rules (as before)
        self.assertEqual(project.can(self.read(), "cut"), (True, ""))
        self.put("mv.json", {})
        p = self.read()
        self.assertEqual((p.kind, p.kind.value, p.stage, project.can(p, "cut")), (Kind.MUSIC_VIDEO, "music video", Stage.READY, (True, "")))

    def test_a_plan_with_problems_or_takes_still_on_the_drive_is_not_cut(self):
        self.put("shotmap.json", {"version": 1, "sections": []})
        ok, why = project.can(self.read(), "cut")
        self.assertFalse(ok)
        self.assertTrue(why.startswith("The plan has problems: ") and "no slots" in why)
        self.put("shotmap.json", " ")
        self.assertIn("not valid JSON", project.can(self.read(), "cut")[1])
        self.put("shotmap.json", PLAN)
        needs = {"takes": 40, "pool": 50, "to_decode": 7}
        self.assertEqual(project.can(self.read(), "cut", needs=needs), (True, ""))  # the drive is there: the cut reads them
        gone = self.read("/Volumes/NoSuchDrive/shoot")
        self.assertEqual(project.can(gone, "cut", needs=needs),
                         (False, "Plug in NoSuchDrive first: 7 of the 40 takes the plan can use still need reading from it."))
        self.assertEqual(project.steps(gone, needs=needs)[1]["text"], "Plug in NoSuchDrive: 7 takes still need reading from it. Then cut.")
        self.assertEqual(project.can(gone, "cut", needs={**needs, "to_decode": 0}), (True, ""))

    def test_an_unplugged_drive(self):
        p = self.read("/Volumes/NoSuchDrive/shoot")
        self.assertEqual(p.drive, {"folder": "/Volumes/NoSuchDrive/shoot", "name": "NoSuchDrive", "connected": False})
        self.assertEqual(project.status(p)["text"], "Footage not read · plug in NoSuchDrive")
        _, nxt = project.steps(p)
        self.assertEqual((nxt["text"], nxt["action"]), ("Plug in NoSuchDrive, then read the footage again.", None))
        self.assertEqual(project.can(p, "prep"), (False, "Plug in NoSuchDrive, then read the footage again."))
        self.assertEqual(project.steps(self.read())[1]["action"], "reprep")  # plugged in: read it again from the page
        self.assertEqual(project.status(self.read("/elsewhere/shoot"))["text"], "Footage not read · plug in the drive")

    def test_every_action_says_why_not(self):
        p = self.read()
        live = [CUTTING]
        busy = "“Cut · demo” is still working on this project: wait for it to finish, or stop it, first."
        for action in ("edit", "look", "write_xml", "readback"):
            self.assertEqual(project.can(p, action), (False, "There is no cut yet: cut it first."), action)
        self.assertEqual(project.can(p, "prep"), (True, ""))
        self.assertEqual(project.can(p, "prep", [{**CUTTING, "kind": "prep", "title": "Read the footage · demo", "writes": False}])[1],
                         "“Read the footage · demo” is still working on this project: wait for it to finish, or stop it, first.")
        self.assertIn("does not know where", project.can(self.read(""), "prep")[1])
        self.put("shotmap.json", PLAN)
        self.put("picks.json", {"name": "demo", "slots": []})
        p = self.read()
        for action in ("cut", "edit", "look", "write_xml"):
            self.assertEqual(project.can(p, action), (True, ""), action)
            self.assertEqual(project.can(p, action, live), (False, busy), action)
            self.assertEqual(project.can(p, action, [{**CUTTING, "writes": False}]), (True, ""), action)  # a job that does not write the cut
        self.assertEqual(project.can(p, "readback"), (False, "No finished video yet: save your export in the project's folder, or choose it."))
        (self.d / "final.mp4").write_bytes(b"x")
        self.assertEqual(project.can(p, "readback", live, video=self.d / "final.mp4"), (True, ""))  # reading the video writes no cut
        self.assertEqual(project.can_all(p, live)["cut"], {"ok": False, "why": busy})
        self.assertEqual(set(project.can_all(p)), set(project.ACTIONS))
        gone = project.read(self.tmp / "nowhere", str(self.footage))
        self.assertIn("folder is not there", project.can(gone, "edit")[1])
        with self.assertRaises(ValueError):
            project.can(p, "dance")

    def test_what_was_read_is_kept_until_the_file_changes(self):
        self.put("overview.md", "x")
        self.put("brief.md", "A reel.")
        with mock.patch.object(project, "_facts", wraps=project._facts) as facts:
            self.assertEqual(self.read().stage, Stage.PLAN)
            self.assertEqual(self.read().stage, Stage.PLAN)
            self.assertEqual(facts.call_count, 1)  # nothing changed: nothing read again
            self.put("shotmap.json", PLAN)
            self.assertEqual(self.read().stage, Stage.READY)
            self.put("shotmap.json", {**PLAN, "version": 2})  # the same file changed
            self.assertEqual(self.read().stage, Stage.PROBLEMS)
            self.assertEqual(facts.call_count, 3)
        self.assertTrue(self.read("/Volumes/NoSuchDrive/x").drive["name"])  # the drive is looked at every time

    def test_what_a_cut_still_needs_from_the_drive(self):
        kb, root = self.tmp / "kb", str(self.footage)
        (kb / "footage").mkdir(parents=True)
        files = [{"fid": f, "path": f"{root}/{f}.mp4", "kind": "video", "media": {"duration_s": 5.0}} for f in "abc"]
        (kb / "footage" / "demo.json").write_text(json.dumps({"roots": {"demo::footage": [root]}, "files": files}))
        self.put("shotmap.json", {**PLAN, "classes": ["wide"]})
        self.put("classes.json", {"a": {"class": "wide"}, "b": {"class": "close-up"}})
        p = self.read()
        with mock.patch("videtor.analyze.footmap.is_done", side_effect=lambda e, d: e["fid"] == "a"):
            self.assertEqual(project.needs(p, "demo::footage", kb, self.tmp / "footmap"), {"takes": 2, "pool": 3, "to_decode": 1})  # c has no class yet
        with mock.patch("videtor.analyze.footmap.is_done", return_value=True):
            self.assertEqual(project.needs(p, "demo::footage", kb, self.tmp / "footmap")["to_decode"], 0)
        self.assertIsNone(project.needs(project.read(self.tmp / "nowhere"), "x", kb, self.tmp / "footmap"))

    def test_the_feedback_and_the_unread_video_agree_with_the_pipeline(self):
        kb = self.tmp / "kb"
        self.put("picks.json", {"name": "the-cut", "slots": []})
        self.assertEqual(project.feedback(self.d, "demo", kb)["final"], None)
        self.assertIsNone(project.unread(self.d, kb))
        final = self.d / "final.mp4"
        final.write_bytes(b"x")
        fb = project.feedback(self.d, "demo", kb)
        self.assertEqual((fb["final"], fb["unread"], fb["summary"], fb["choices"]), (str(final), str(final), None, 0))
        self.assertEqual(project.unread(self.d, kb), pipeline.unread_final(self.d, kb))
        (kb / "feedback").mkdir(parents=True)
        later = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 60))
        doc = {"name": "the-cut", "source": f"render:{final}", "read_at": later, "slots": [{"status": "kept"}], "summary": {"kept": 1}, "choices": [{}]}
        (kb / "feedback" / "the-cut.json").write_text(json.dumps(doc))
        fb = project.feedback(self.d, "demo", kb)  # filed under the picks' name, not the project's
        self.assertEqual((fb["unread"], fb["summary"], fb["choices"], fb["read_at"]), (None, {"kept": 1}, 1, later))
        self.assertEqual((project.unread(self.d, kb), pipeline.unread_final(self.d, kb)), (None, None))
        os.utime(final, (time.time() + 120, time.time() + 120))  # exported again after the read
        self.assertEqual((project.unread(self.d, kb), pipeline.unread_final(self.d, kb)), (final, final))
        with mock.patch.object(Path, "read_text", wraps=Path.read_text, autospec=True) as reads:
            project.feedback(self.d, "demo", kb)
        self.assertFalse(any(c.args[0].name == "picks.json" for c in reads.call_args_list))  # its name was kept
        self.put("verdict.json", {"good": True, "note": "ok"})
        self.assertEqual(project.feedback(self.d, "demo", kb)["verdict"]["note"], "ok")


if __name__ == "__main__":
    unittest.main()
