import subprocess
import sys
import time
import unittest

from videtor.ui import jobs

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


class JobTests(unittest.TestCase):
    def setUp(self):
        self.m = jobs.Manager(FAKE, cwd=".")

    def tearDown(self):
        self.m.cancel_all()

    def test_a_job_runs_logs_and_finishes(self):
        job = self.m.submit("music", "quick", ["0.2"])
        self.assertEqual(job.status, "running")
        self.assertTrue(wait_for(lambda: job.status == "done"))
        lines, nxt = self.m.tail(job)
        self.assertIn("start", lines)
        self.assertIn("end", lines)
        self.assertEqual(nxt, len(lines))
        self.assertEqual(job.rc, 0)
        later, _ = self.m.tail(job, since=nxt)
        self.assertEqual(later, [])

    def test_a_cut_pauses_a_running_prep_and_resumes_it_after(self):
        prep = self.m.submit("prep", "slow prep", ["3"])
        self.assertTrue(wait_for(lambda: "start" in list(prep.log)))
        cut = self.m.submit("cut", "a cut", ["0.6"])
        self.assertEqual(prep.status, "paused")
        self.assertEqual(prep.paused_by, cut.id)
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

    def test_cancel_stops_a_job_and_a_waiting_one(self):
        cut = self.m.submit("cut", "a cut", ["5"])
        prep = self.m.submit("prep", "a prep", ["0.1"])
        self.assertTrue(self.m.cancel(prep))
        self.assertEqual(prep.status, "cancelled")
        self.assertTrue(self.m.cancel(cut))
        self.assertTrue(wait_for(lambda: cut.proc.poll() is not None))
        self.assertEqual(cut.status, "cancelled")
        self.assertEqual([j["id"] for j in self.m.list()], [cut.id, prep.id])


if __name__ == "__main__":
    unittest.main()
