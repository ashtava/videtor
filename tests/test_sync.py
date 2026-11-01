"""Lining takes up with a song from their camera audio, on synthetic onset envelopes (no audio decoded)."""

import unittest

import numpy as np

from videtor.analyze import sync

R = sync.RATE


def song_env(seconds=120, seed=1):
    """A rap-like envelope: a steady beat plus irregular syllable onsets, z-normalised like sync.envelope."""
    rng = np.random.default_rng(seed)
    n = int(seconds * R)
    env = np.zeros(n)
    env[:: int(0.325 * R)] += 2.0  # the beat repeats, which is why true peaks are only a few sigma high
    env[rng.choice(n, n // 12, replace=False)] += rng.gamma(2.0, 1.0, n // 12)
    env = np.convolve(env, np.hanning(5), mode="same")
    return (env - env.mean()) / env.std()


def heard(segment, noise=0.6, seed=2):
    """The song as a camera mic hears it: quieter, with room noise on top."""
    rng = np.random.default_rng(seed)
    x = 0.5 * segment + noise * rng.standard_normal(len(segment))
    return (x - x.mean()) / x.std()


def find(take, song):
    ws = sync.windows(take, song, 1) + sync.windows(take, song, 2)
    best = {}
    for w in ws:
        if w["t"] not in best or w["z"] > best[w["t"]]["z"]:
            best[w["t"]] = w
    return sync.stretches(take, song, list(best.values()))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.song = song_env()

    def test_a_take_filmed_from_song_30s_syncs_with_offset_30(self):
        take = heard(self.song[int(30 * R):int(60 * R)])
        st = find(take, self.song)
        self.assertEqual(len(st), 1)
        self.assertAlmostEqual(st[0]["c"], 30.0, delta=0.02)
        self.assertEqual(st[0]["rate"], 1)
        self.assertLess(st[0]["t0"], 1.0)
        self.assertGreater(st[0]["t1"], 29.0)

    def test_a_restarted_playback_gives_two_stretches(self):
        take = heard(np.concatenate([self.song[int(30 * R):int(45 * R)], self.song[int(10 * R):int(30 * R)]]))
        st = find(take, self.song)
        offsets = sorted(round(s["c"], 1) for s in st)
        self.assertEqual(offsets, [-5.0, 30.0])  # take 15 s is song 10 s after the restart

    def test_room_tone_has_no_stretch(self):
        rng = np.random.default_rng(5)
        self.assertEqual(find(rng.standard_normal(int(30 * R)), self.song), [])

    def test_double_speed_playback_plays_at_half_speed(self):
        take = heard(self.song[int(20 * R):int(80 * R)][::2])  # 30 s of take hold 60 s of song
        st = find(take, self.song)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["rate"], 2)
        self.assertAlmostEqual(st[0]["c"], 20.0, delta=0.05)

    def test_pieces_on_one_offset_are_one_playback(self):
        pieces = [{"rate": 1, "c": 3.3, "t0": 20.0, "t1": 30.0, "r": 0.5, "n": 3, "z": 6.0},
                  {"rate": 1, "c": 3.31, "t0": 45.0, "t1": 60.0, "r": 0.6, "n": 4, "z": 6.0},
                  {"rate": 1, "c": -70.0, "t0": 80.0, "t1": 82.5, "r": 0.42, "n": 1, "z": 9.5}]
        out = sync._bridge(pieces)
        self.assertEqual(len(out), 1)  # the faint lone piece on its own offset is a chance match
        self.assertEqual((out[0]["t0"], out[0]["t1"]), (20.0, 60.0))
        self.assertEqual(out[0]["bridged"], 15.0)

    def test_take_and_song_time_round_trip(self):
        s = {"rate": 2, "c": 20.0, "t0": 0.0, "t1": 30.0}
        self.assertAlmostEqual(sync.song_time(s, 5.0), 30.0)
        self.assertAlmostEqual(sync.take_time(s, 30.0), 5.0)
        hits = sync.covering({"x": {"stretches": [s]}}, 30.0, 40.0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(sync.covering({"x": {"stretches": [s]}}, 70.0, 81.0), [])


if __name__ == "__main__":
    unittest.main()
