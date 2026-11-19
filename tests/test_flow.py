"""The rhythm and the order of a cut: a plan's pace in numbers and the advice on it, a section that follows the day
when the plan asks, a draft that holds after a run, and the measures the evaluation shows."""

import unittest

import numpy as np

from test_fill import fake_embed, fake_take
from videtor.plan import draft, evaluate, fill, shotmap


def a_map(beats, **extra) -> dict:
    return {"version": 1, "name": "p", "key": "k", "length_s": sum(beats) * 0.5, "fps": 25, "width": 1080, "height": 1920,
            "sections": [{"name": "s", "story": "they arrive and sit down", "slots": [{"id": f"s{i}", "beats": b, "show": "axis 0"} for i, b in enumerate(beats)]}], **extra}


class PaceTests(unittest.TestCase):
    def test_a_cut_is_measured_in_holds_and_runs(self):
        p = shotmap.pace([0.5, 0.5, 0.5, 2.0, 0.5, 0.5, 3.0])
        self.assertEqual((p["shots"], p["holds"], p["runs"], p["longest_run_s"], p["median_s"]), (7, 2, [3, 2], 1.5, 0.5))
        self.assertAlmostEqual(p["hold_share"], 5.0 / 7.5, places=3)
        self.assertEqual(shotmap.pace([])["hold_share"], 0.0)

    def test_advice_names_the_run_with_no_hold_and_a_section_with_no_story(self):
        quick = a_map([1] * 30)
        quick["sections"][0].pop("story")
        said = shotmap.advice(quick)
        self.assertTrue(any(a.startswith("pace: 0% of the cut is held") for a in said))
        self.assertTrue(any("15 s of quick shots with no hold, from s0 to s29" in a for a in said))
        self.assertTrue(any(a.startswith("story: no `story` line for s") for a in said))
        phrased = a_map(([1] * 6 + [4]) * 4)
        self.assertEqual(shotmap.advice(phrased), [])  # runs of six quick shots, each followed by a two-second hold
        self.assertEqual(shotmap.advice(quick, ls=[2.0] * 30)[0][:6], "story:")  # with the cut's own lengths: nothing to say about the pace
        self.assertEqual(shotmap.advice({**quick, "reference": {"file": "x"}}), [])  # a reference's rhythm is copied on purpose

    def test_two_bursts_close_together_are_said(self):
        m = a_map([1, 1, 4, 1, 4])
        m["sections"][0]["slots"][0]["flicker"] = 3
        m["sections"][0]["slots"][1]["stack"] = True
        self.assertTrue(any(a.startswith("sound: s0 and s1") for a in shotmap.advice(m)))

    def test_the_order_a_plan_may_ask_for_is_checked(self):
        self.assertEqual(shotmap.validate(a_map([1, 1], order="sections")), [])
        self.assertIn("order must be shoot, sections or none", shotmap.validate(a_map([1, 1], order="backwards")))
        m = a_map([1, 1])
        m["sections"][0]["order"] = "sideways"
        self.assertIn("section 's': order must be shoot or none", shotmap.validate(m))
        self.assertEqual(shotmap.warnings(a_map([1, 1])), [])  # `story` and a section's `order` are known keys


class OrderTests(unittest.TestCase):
    def setUp(self):
        # six takes that all show the same thing equally well, filmed in the order of their names
        self.takes = [fake_take(f"C000{k}", 0) for k in range(6)]
        self.times = [(float(i), 1.0) for i in range(6)]

    def names(self, m):
        return [s["pick"]["name"] for s in fill.fill(m, self.takes, self.times, fake_embed)["slots"]]

    def test_a_section_that_asks_follows_the_day_and_one_that_does_not_is_as_before(self):
        m = {"version": 1, "name": "p", "key": "k", "length_s": 6.0, "fps": 25, "width": 1080, "height": 1920,
             "sections": [{"name": "s", "slots": [{"id": f"s{i}", "length_s": 1.0, "show": "axis 0", "takes": None} for i in range(6)]}]}
        for s in m["sections"][0]["slots"]:
            s.pop("takes")
        for k, t in enumerate(self.takes):  # a small preference that runs against the day: slot i likes take 5 - i a little better
            t.a["sharp"] = np.full(len(t.a["t"]), 200.0 + 2 * k, np.float32)
        free = self.names(m)
        asked = self.names({**m, "order": "sections"})
        self.assertEqual(asked, sorted(asked))
        self.assertEqual(sorted(free), sorted(asked))  # the same takes, dealt in the order of the day
        one = {**m, "sections": [{**m["sections"][0], "order": "shoot"}]}
        self.assertEqual(self.names(one), asked)
        self.assertEqual(self.names({**m, "order": "sections", "sections": [{**m["sections"][0], "order": "none"}]}), free)

    def test_two_cameras_each_count_through_the_day(self):
        takes = [fake_take(n, 0) for n in ("C0001", "C0002", "C0003", "DJI_0001", "DJI_0002")]
        self.assertEqual(fill.camera_of("C0012.MP4"), "c")
        self.assertEqual(fill.camera_of("DJI_0034.MP4"), "dji_")
        self.assertEqual([round(float(x), 2) for x in fill.day_position(takes)], [0.0, 0.5, 1.0, 0.0, 1.0])


class DraftTests(unittest.TestCase):
    def test_a_draft_holds_a_shot_after_a_long_run_of_quick_ones(self):
        m = draft.draft("p", "no-such-bucket", 40.0, "p::footage", seed=1)
        self.assertEqual(shotmap.validate(m), [])
        ls = shotmap.lengths(m, 60 / 128)
        self.assertLessEqual(shotmap.pace(ls)["longest_run_s"], shotmap.RUN_MAX_S)
        self.assertAlmostEqual(sum(ls), 40.0, delta=1.5)


class MeasureTests(unittest.TestCase):
    def test_the_evaluation_shows_the_pace_and_the_order_of_a_cut(self):
        def slot(i, name, length, section="a"):
            return {"id": f"s{i}", "section": section, "start_s": float(i), "length_s": length, "pick": {"fid": name, "name": f"{name}.MP4", "in_s": 0.0, "out_s": length}}

        picks = {"slots": [slot(0, "C0001", 0.5), slot(1, "C0002", 0.5), slot(2, "C0004", 2.0), slot(3, "C0003", 0.5), slot(4, "C0005", 0.5), slot(5, "C0006", 0.5, "b")]}
        got = evaluate.sequence(picks)
        self.assertEqual(got["in_order"], 0.75)  # four cuts inside section a, three of them forward
        self.assertAlmostEqual(got["hold_share"], 2.0 / 4.5, places=3)
        self.assertEqual(got["longest_run"], 1.5)
        self.assertNotIn("in_order", evaluate.GUARDS)


if __name__ == "__main__":
    unittest.main()
