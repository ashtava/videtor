import inspect
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from videtor.ui import jobs
from videtor.ui.jobs import Status as S

# a stand-in for `videtor <command> ...`: python -c SCRIPT <seconds> [FAILED]
SCRIPT = "import sys,time; print('start', flush=True); time.sleep(float(sys.argv[1])); print(sys.argv[2] if len(sys.argv) > 2 else 'end', flush=True)"
FAKE = [sys.executable, "-u", "-c", SCRIPT]


def wait_for(cond, timeout: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def proc_state(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


class StatusTests(unittest.TestCase):
    def test_the_statuses_are_the_strings_they_were(self):
        self.assertEqual([s.value for s in S], ["queued", "waiting", "running", "paused", "done", "failed", "cancelled"])
        self.assertTrue(S.RUNNING == "running")
        self.assertEqual(json.dumps({"status": S.PAUSED}), '{"status": "paused"}')
        self.assertEqual(jobs.LIVE, {S.WAITING, S.RUNNING, S.PAUSED})
        self.assertEqual(jobs.ALIVE, {S.RUNNING, S.PAUSED})
        self.assertEqual(jobs.ENDED, {S.DONE, S.FAILED, S.CANCELLED})
        self.assertEqual(set(jobs.TRANSITIONS), set(S))

    def test_every_allowed_move_is_made_and_every_other_refused(self):
        m = jobs.Manager(FAKE, cwd=".")
        allowed = {(a, b) for a, tos in jobs.TRANSITIONS.items() for b in tos}
        self.assertEqual(allowed, {(S.QUEUED, S.WAITING), (S.QUEUED, S.RUNNING), (S.QUEUED, S.FAILED),
                                   (S.WAITING, S.RUNNING), (S.WAITING, S.FAILED), (S.WAITING, S.CANCELLED),
                                   (S.RUNNING, S.PAUSED), (S.RUNNING, S.DONE), (S.RUNNING, S.FAILED), (S.RUNNING, S.CANCELLED),
                                   (S.PAUSED, S.RUNNING), (S.PAUSED, S.DONE), (S.PAUSED, S.FAILED), (S.PAUSED, S.CANCELLED)})
        for a in S:
            for b in S:
                job = jobs.Job(id="x", kind="prep", title="t", args=[], status=a)
                self.assertEqual(m._move(job, b, "moved"), (a, b) in allowed, (a, b))
                self.assertEqual(job.status, b if (a, b) in allowed else a)
                self.assertEqual(list(job.log), ["moved"] if (a, b) in allowed else [])
                self.assertEqual(job.ended is not None, (a, b) in allowed and b in jobs.ENDED)
        # the one place a status is written
        self.assertEqual(len(re.findall(r"\.status = ", inspect.getsource(jobs))), 1)

    def test_a_lane_per_kind(self):
        self.assertIs(jobs.lane("cut"), jobs.Lane.FIRST)
        self.assertIs(jobs.lane("prep"), jobs.Lane.YIELDS)
        self.assertIs(jobs.lane("look"), jobs.Lane.FREE)
        self.assertEqual(jobs.BACKGROUND, {"prep", "library", "teach"})
        self.assertEqual(jobs.FOREGROUND, {"cut"})


class JobTests(unittest.TestCase):
    def setUp(self):
        self.m = jobs.Manager(FAKE, cwd=".")

    def tearDown(self):
        self.m.cancel_all()
        for j in self.m.jobs.values():
            if j.proc:
                j.proc.wait(10)

    def test_a_job_runs_logs_and_finishes(self):
        job = self.m.submit("music", "quick", ["0.2"])
        self.assertEqual(job.status, "running")
        self.assertTrue(job.to_dict()["live"])
        self.assertTrue(wait_for(lambda: job.status == "done"))
        lines, nxt = self.m.tail(job)
        self.assertIn("start", lines)
        self.assertIn("end", lines)
        self.assertEqual(nxt, len(lines))
        self.assertEqual(job.rc, 0)
        later, _ = self.m.tail(job, since=nxt)
        self.assertEqual(later, [])
        d = job.to_dict()
        self.assertEqual((d["live"], d["ended"], d["writes"], d["status"]), (False, True, False, "done"))
        self.assertEqual(d["ended_at"], job.ended)

    def test_a_cut_pauses_a_running_prep_and_resumes_it_after(self):
        prep = self.m.submit("prep", "slow prep", ["3"])
        self.assertTrue(wait_for(lambda: "start" in list(prep.log)))
        cut = self.m.submit("cut", "a cut", ["0.6"])
        self.assertEqual(prep.status, "paused")
        self.assertEqual(prep.paused_by, cut.id)
        self.assertEqual((prep.to_dict()["live"], prep.to_dict()["ended"]), (True, False))
        self.assertTrue(wait_for(lambda: proc_state(prep.proc.pid).startswith("T"), 2.0), proc_state(prep.proc.pid))
        self.assertTrue(wait_for(lambda: cut.status == "done"))
        self.assertEqual(prep.status, "running")
        self.assertFalse(proc_state(prep.proc.pid).startswith("T"))
        self.assertTrue(wait_for(lambda: prep.status == "done", 8.0))
        self.assertIn("paused: a cut gets the machine first", list(prep.log))
        self.assertIn("resumed", list(prep.log))

    def test_a_prep_submitted_during_a_cut_waits_for_it(self):
        cut = self.m.submit("cut", "a cut", ["0.6"])
        prep = self.m.submit("prep", "a prep", ["0.2"])
        self.assertEqual(prep.status, "waiting")
        self.assertIsNone(prep.proc)
        self.assertEqual((prep.to_dict()["live"], prep.to_dict()["ended"]), (True, False))
        self.assertTrue(wait_for(lambda: cut.status == "done"))
        self.assertTrue(wait_for(lambda: prep.status == "done"))
        self.assertIn("the cut has finished: starting", list(prep.log))

    def test_a_paused_prep_that_ends_with_failures_runs_again_once(self):
        prep = self.m.submit("prep", "spoilt prep", ["1.0", "decode FAILED 1/3"])
        self.assertTrue(wait_for(lambda: "start" in list(prep.log)))
        self.m.submit("cut", "a cut", ["0.3"])
        self.assertTrue(wait_for(lambda: prep.status == "done", 8.0))
        self.assertTrue(wait_for(lambda: prep.reran_as is not None))
        again = self.m.get(prep.reran_as)
        self.assertEqual(again.kind, "prep")
        self.assertEqual(again.args, prep.args)
        self.assertTrue(wait_for(lambda: again.status == "done", 8.0))
        self.assertIsNone(again.reran_as)  # once only

    def test_an_unpaused_prep_with_failures_is_not_rerun(self):
        prep = self.m.submit("prep", "prep", ["0.1", "FAILED"])
        self.assertTrue(wait_for(lambda: prep.status == "done"))
        time.sleep(0.2)
        self.assertIsNone(prep.reran_as)

    def test_a_plain_line_is_only_the_last_thing_said(self):
        prep = jobs.Job(id="x", kind="prep", title="t", args=[])
        jobs.note(prep, "        25/68 clips (1m35s): C1380.MP4 8s, 8 stills, faces 0%")
        jobs.note(prep, "Premiere XML: /x/out/my shoot/my shoot.xml")
        jobs.note(prep, "   ")
        self.assertEqual(prep.progress, {"last": "Premiere XML: /x/out/my shoot/my shoot.xml"})
        self.assertEqual(prep.result, {})
        self.assertIsNone(jobs.eta({"done": 5, "total": 5, "t0": 1000.0}, now=1060.0))
        self.assertEqual(jobs.eta({"done": 25, "total": 68, "t0": 1000.0}, now=1010.0), round(10 / 25 * 43))

    def test_events_are_read_as_data_however_the_sentence_is_worded(self):
        t = 1000.0
        cut = jobs.Job(id="z", kind="cut", title="t", args=[])
        jobs.note_event(cut, {"event": "stage", "stage": "decode", "label": "Reading those takes in full", "text": "any wording at all"}, now=t)
        jobs.note_event(cut, {"event": "progress", "done": 12, "total": 52, "text": ""}, now=t + 5)
        self.assertEqual((cut.progress["stage"], cut.progress["done"], cut.progress["total"], cut.progress["t0"]), ("Reading those takes in full", 12, 52, t))
        jobs.note_event(cut, {"event": "artifact", "kind": "xml", "path": "/x/out/my shoot/my shoot.xml", "text": "worded another way"}, now=t + 9)
        jobs.note_event(cut, {"event": "warning", "text": "shot map: the map has an unknown key 'efects'"})
        self.assertEqual(cut.result["xml"], "/x/out/my shoot/my shoot.xml")
        self.assertEqual(cut.result["warnings"], ["shot map: the map has an unknown key 'efects'"])
        # a job's output: event lines are read as data and logged as their sentence, other lines pass as they are
        script = ("import json; print(json.dumps({'event': 'stage', 'stage': 'fill', 'label': 'Choosing the shots', 'text': 'choosing'}), flush=True); "
                  "print('a library warning', flush=True); "
                  "print(json.dumps({'event': 'artifact', 'kind': 'xml', 'path': '/x/a.xml', 'text': 'Premiere XML: /x/a.xml'}), flush=True)")
        m = jobs.Manager([sys.executable, "-u", "-c", script], cwd=".")
        self.assertEqual(m.env["VIDETOR_EVENTS"], "json")
        job = m.submit("cut", "events", [])
        self.assertTrue(wait_for(lambda: job.status == "done"))
        self.assertEqual(m.tail(job)[0][1:4], ["choosing", "a library warning", "Premiere XML: /x/a.xml"])
        self.assertEqual((job.progress["stage"], job.result["xml"]), ("Choosing the shots", "/x/a.xml"))

    def test_the_job_list_carries_progress_and_the_finish_hook_runs(self):
        ended = []
        self.m.on_finish = ended.append
        job = self.m.submit("prep", "p", ["0.1", "overview written"])
        self.assertTrue(wait_for(lambda: job.status == "done"))
        self.assertTrue(wait_for(lambda: ended == [job]))
        d = job.to_dict()
        self.assertEqual((d["progress"]["stage"], d["progress"]["last"]), (None, "overview written"))
        self.assertIsNone(d["progress"]["eta_s"])

    def test_cancel_stops_a_job_and_a_waiting_one(self):
        cut = self.m.submit("cut", "a cut", ["5"])
        prep = self.m.submit("prep", "a prep", ["0.1"])
        self.assertTrue(self.m.cancel(prep))
        self.assertEqual(prep.status, "cancelled")
        self.assertIn("[cancelled before it started]", list(prep.log))
        self.assertIsNone(prep.proc)
        self.assertTrue(self.m.cancel(cut))
        self.assertTrue(wait_for(lambda: cut.proc.poll() is not None))
        self.assertEqual(cut.status, "cancelled")
        self.assertEqual(cut.to_dict()["ended"], True)
        self.assertFalse(self.m.cancel(cut))  # nothing left to cancel
        self.assertEqual([j["id"] for j in self.m.list()], [cut.id, prep.id])

    def test_cancel_of_a_paused_job_stops_it_and_it_is_not_rerun_or_resumed(self):
        prep = self.m.submit("prep", "slow prep", ["5"])
        self.assertTrue(wait_for(lambda: "start" in list(prep.log)))
        cut = self.m.submit("cut", "a cut", ["0.5"])
        self.assertEqual(prep.status, "paused")
        self.assertTrue(self.m.cancel(prep))
        self.assertTrue(wait_for(lambda: prep.proc.poll() is not None, 8.0))
        self.assertTrue(wait_for(lambda: cut.status == "done"))
        time.sleep(0.2)
        self.assertEqual(prep.status, "cancelled")
        self.assertIsNone(prep.reran_as)
        self.assertFalse(self.m.resume(prep))

    def test_pause_and_resume_by_hand_and_the_refused_ones(self):
        job = self.m.submit("library", "sort", ["3"])
        self.assertTrue(wait_for(lambda: "start" in list(job.log)))
        self.assertFalse(self.m.resume(job))  # not paused
        self.assertTrue(self.m.pause(job))
        self.assertFalse(self.m.pause(job))  # already paused
        self.assertIsNone(job.paused_by)
        self.assertTrue(self.m.resume(job))
        self.assertEqual(job.status, "running")

    def test_two_writers_of_one_project_cannot_run(self):
        first = self.m.submit("cut", "Cut · shoot", ["3"], project="/p/shoot", writes=True)
        self.assertIs(self.m.busy("/p/shoot"), first)
        with self.assertRaises(jobs.Busy) as caught:
            self.m.submit("look", "Write the Premiere XML · shoot", ["0.1"], project="/p/shoot", writes=True)
        self.assertIs(caught.exception.job, first)
        self.assertIn("Cut · shoot", str(caught.exception))
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual(len(self.m.jobs), 1)  # the refused one is not kept
        other = self.m.submit("look", "Write · other", ["0.1"], project="/p/other", writes=True)
        reader = self.m.submit("search", "More takes", ["0.1"], project="/p/shoot")
        self.assertTrue(other.writes and other.to_dict()["writes"])
        self.assertFalse(reader.writes)
        self.assertIsNone(self.m.busy("/p/nothing"))
        self.assertIsNone(self.m.busy(None))
        self.assertTrue(self.m.cancel(first))
        self.assertIsNone(self.m.busy("/p/shoot"))
        self.assertTrue(self.m.submit("look", "Write · shoot", ["0.1"], project="/p/shoot", writes=True).writes)

    def test_a_waiting_writer_still_holds_its_project(self):
        self.m.submit("cut", "a cut", ["3"], project="/p/a")
        waiting = self.m.submit("teach", "a teach that writes", ["0.1"], project="/p/b", writes=True)
        self.assertEqual(waiting.status, "waiting")
        self.assertIs(self.m.busy("/p/b"), waiting)

    def test_ids_never_repeat_across_launches(self):
        a = self.m.submit("music", "a", ["0.05"])
        b = self.m.submit("music", "b", ["0.05"])
        self.assertRegex(a.id, r"^j\d+$")
        self.assertNotEqual(a.id, b.id)
        launch = [sys.executable, "-c", "from videtor.ui import jobs; m = jobs.Manager(['true'], '.'); print(m.submit('x', 't', []).id)"]
        first = subprocess.run(launch, capture_output=True, text=True, check=True).stdout.strip()
        second = subprocess.run(launch, capture_output=True, text=True, check=True).stdout.strip()
        self.assertRegex(first, r"^j\d+$")
        self.assertLess(int(b.id[1:]), int(first[1:]))
        self.assertLess(int(first[1:]), int(second[1:]))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = Path(self.tmp.name) / "jobs.json"
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(5)
        self.tmp.cleanup()

    def spawn(self, cmd):
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        self.assertTrue(wait_for(lambda: jobs._command(p.pid) == " ".join(cmd), 5.0))
        return p

    def write(self, *rows):
        self.record.write_text(json.dumps({"jobs": [{"id": "j1", "kind": "prep", "title": "old prep", "project": "/p/a", "started": time.time(), **r} for r in rows]}))

    def test_without_a_record_nothing_is_written(self):
        m = jobs.Manager(FAKE, cwd=".")
        job = m.submit("music", "m", ["0.05"])
        self.assertTrue(wait_for(lambda: job.status == "done"))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_the_running_jobs_are_kept_in_the_record(self):
        m = jobs.Manager(FAKE, cwd=".", record=self.record)
        self.assertEqual(json.loads(self.record.read_text()), {"jobs": []})
        job = m.submit("prep", "a prep", ["0.5"], project="/p/a")
        row = json.loads(self.record.read_text())["jobs"][0]
        self.assertEqual({k: row[k] for k in ("id", "kind", "title", "project", "pid", "started")},
                         {"id": job.id, "kind": "prep", "title": "a prep", "project": "/p/a", "pid": job.proc.pid, "started": job.started})
        self.assertTrue(wait_for(lambda: job.status == "done"))
        self.assertTrue(wait_for(lambda: json.loads(self.record.read_text()) == {"jobs": []}))

    def test_a_launch_stops_what_the_last_one_left_running(self):
        left = self.spawn(FAKE + ["30"])
        self.write({"pid": left.pid, "cmd": FAKE + ["30"]})
        said = []
        m = jobs.Manager(FAKE, cwd=".", log=said.append, record=self.record)
        self.assertIsNotNone(left.wait(5))
        self.assertTrue(any("earlier launch" in s and "old prep" in s for s in said), said)
        self.assertEqual(json.loads(self.record.read_text()), {"jobs": []})
        self.assertEqual(m.jobs, {})

    def test_a_paused_left_over_is_stopped_too(self):
        left = self.spawn(FAKE + ["30"])
        subprocess.run(["kill", "-STOP", str(left.pid)], check=True)
        self.write({"pid": left.pid, "cmd": FAKE + ["30"]})
        jobs.Manager(FAKE, cwd=".", record=self.record)
        self.assertIsNotNone(left.wait(5))

    def test_a_pid_that_is_not_ours_is_left_alone(self):
        stranger = self.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
        dead = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        dead.wait()
        not_videtor = self.spawn([sys.executable, "-c", "import time; time.sleep(29)"])
        self.write({"pid": stranger.pid, "cmd": FAKE + ["30"]},  # its pid, another command
                   {"pid": dead.pid, "cmd": FAKE + ["30"]},  # gone
                   {"pid": not_videtor.pid, "cmd": [sys.executable, "-c", "import time; time.sleep(29)"]})  # not our command prefix
        said = []
        jobs.Manager(FAKE, cwd=".", log=said.append, record=self.record)
        time.sleep(0.3)
        self.assertIsNone(stranger.poll())
        self.assertIsNone(not_videtor.poll())
        self.assertEqual(said, [])
        self.assertEqual(json.loads(self.record.read_text()), {"jobs": []})

    def test_an_unreadable_record_starts_clean(self):
        self.record.write_text("{not json")
        said = []
        jobs.Manager(FAKE, cwd=".", log=said.append, record=self.record)
        self.assertTrue(any("could not be read" in s for s in said))
        self.assertEqual(json.loads(self.record.read_text()), {"jobs": []})


if __name__ == "__main__":
    unittest.main()
