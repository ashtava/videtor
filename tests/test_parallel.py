import os
import unittest
from unittest import mock

from videtor.analyze import parallel


def _square(x, offset=0):
    return x * x + offset


def _boom(x):
    if x == 3:
        raise ValueError("three")
    return x


class SizingTests(unittest.TestCase):
    def test_defaults_are_at_least_one_and_capped_by_the_items(self):
        self.assertGreaterEqual(parallel.cores(), 1)
        self.assertGreaterEqual(parallel.workers(), 1)
        self.assertEqual(parallel.workers(items=1), 1)
        self.assertLessEqual(parallel.workers(items=3), 3)

    def test_the_environment_overrides_the_default(self):
        with mock.patch.dict(os.environ, {"VIDETOR_WORKERS": "2"}):
            self.assertEqual(parallel.workers(), 2)
            self.assertEqual(parallel.workers(items=1), 1)

    def test_ffmpeg_threads_share_the_cores(self):
        self.assertGreaterEqual(parallel.ffmpeg_threads(1), 2)
        self.assertGreaterEqual(parallel.ffmpeg_threads(8), 2)
        self.assertLessEqual(parallel.ffmpeg_threads(8), parallel.ffmpeg_threads(1))


class RunTests(unittest.TestCase):
    def test_inline_run_returns_every_result_and_calls_on_done_in_order(self):
        seen = []
        rows = parallel.run(_square, [1, 2, 3], args=(1,), workers_n=1, on_done=lambda it, r, e, k, n: seen.append((it, r, e, k, n)))
        self.assertEqual([(it, r) for it, r, _ in rows], [(1, 2), (2, 5), (3, 10)])
        self.assertEqual([s[3] for s in seen], [1, 2, 3])
        self.assertTrue(all(s[4] == 3 for s in seen))

    def test_an_exception_becomes_the_items_error(self):
        logged = []
        rows = parallel.run(_boom, [1, 3, 5], workers_n=1, log=logged.append)
        by_item = {it: (r, e) for it, r, e in rows}
        self.assertEqual(by_item[1], (1, None))
        self.assertEqual(by_item[5], (5, None))
        self.assertIsNone(by_item[3][0])
        self.assertIn("ValueError: three", by_item[3][1])
        self.assertEqual(len(logged), 1)

    def test_threads_run_everything(self):
        rows = parallel.run(_square, list(range(20)), workers_n=4, threads=True)
        self.assertEqual(sorted(r for _, r, _ in rows), [x * x for x in range(20)])
        self.assertTrue(all(e is None for _, _, e in rows))

    def test_processes_run_everything(self):
        rows = parallel.run(_boom, list(range(6)), workers_n=2)
        self.assertEqual(sorted(it for it, _, _ in rows), list(range(6)))
        self.assertEqual(sum(1 for _, _, e in rows if e), 1)

    def test_an_empty_list_is_fine(self):
        self.assertEqual(parallel.run(_square, []), [])


if __name__ == "__main__":
    unittest.main()
