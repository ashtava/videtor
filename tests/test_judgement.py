"""The judgement that used to need a second look by eye: what the shoot holds (inventory), a draft that asks only
for that, neighbours settled at the cuts, flickers that stay in their light, the order of the day, and the
automatic second look. Synthetic footage maps throughout."""

import unittest
from unittest import mock

import numpy as np

from test_fill import MAP, fake_embed, fake_take, unit
from videtor.plan import autolook, draft, evaluate, fill, inventory, shotmap

VOCAB = {"zero": (["axis 0"], {}), "one": (["axis 1"], {"scale": ["wide"]}), "two": (["axis 2"], {}), "three": (["axis 3"], {"face": "full"})}


def cut(m: dict, takes: list) -> dict:
    return fill.fill(m, takes, shotmap.timing(m, None, 25.0), fake_embed)


def one_section(*shows, **extra) -> dict:
    slots = [{"id": f"s{k + 1}", "length_s": 1.0, "show": s if isinstance(s, str) else s["show"], **({} if isinstance(s, str) else {k: v for k, v in s.items() if k != "show"})}
             for k, s in enumerate(shows)]
    return {**MAP, "length_s": float(len(slots)), "sections": [{"name": "one", "slots": slots}], **extra}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.takes = [fake_take("T1", 0), fake_take("T2", 0), fake_take("T3", 2, n=40), fake_take("T4", 2)]

    def test_the_inventory_says_what_the_shoot_holds_and_one_long_take_is_not_half_of_it(self):
        with mock.patch.object(inventory, "vocabulary", return_value=VOCAB):
            inv = inventory.build(self.takes, fake_embed)
        self.assertEqual(set(inv["labels"]), {"zero", "two"})  # nothing shows axis 1 or 3
        self.assertEqual(inv["labels"]["zero"]["seconds"], 20.0)
        self.assertEqual(inv["labels"]["two"]["seconds"], 30.0)
        self.assertAlmostEqual(inv["labels"]["two"]["share"], 22 / 42, places=3)  # 30 of the 50 s, but the 20 s take counts as TAKE_CAP_S
        self.assertEqual(inv["takes"]["T3.MP4"]["label"], "two")
        self.assertEqual(set(inventory.available(inv)), {"zero", "two"})
        self.assertEqual(inventory.available({"labels": {"x": {"seconds": 0.5, "share": 1.0}}}), {})  # a moment is not footage to plan on

    def test_a_draft_asks_only_for_what_the_shoot_holds(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(inventory, "vocabulary", return_value=VOCAB):
            inv = inventory.build(self.takes, fake_embed)
            m = draft.draft("d", "event", 20.0, "d::footage", kb_dir=Path(tmp), inventory=inv)
            shown = {s["shows"] for s in shotmap.slots(m)}
            self.assertTrue(shown <= {"zero", "two"}, shown)
            self.assertEqual(shown, {"zero", "two"})  # what was shot gets slots even though no reel taught it
            self.assertEqual(shotmap.validate(m), [])
            self.assertEqual(shotmap.warnings(m), [])
            self.assertNotIn("effects", m)  # a bucket with no reels behind it gets no effects drawn
            blind = {s["shows"] for s in shotmap.slots(draft.draft("d", "event", 60.0, "d::footage", kb_dir=Path(tmp)))}
            self.assertFalse(blind <= {"zero", "two"})  # without the inventory it asks for things the shoot lacks
            self.assertEqual(draft.draft("d", "event", 20.0, "d::footage", kb_dir=Path(tmp), inventory=inv, order="shoot")["order"], "shoot")

    def test_the_style_and_the_shoot_are_mixed_half_and_half(self):
        mix = draft._mix({"zero": 0.9, "nine": 0.1}, {"zero": 0.25, "two": 0.75})
        self.assertEqual(set(mix), {"zero", "two"})  # only what the shoot holds
        self.assertAlmostEqual(mix["zero"], 0.5 * 1.0 + 0.5 * 0.25)
        self.assertAlmostEqual(mix["two"], 0.5 * 0.75)
        self.assertEqual(draft._mix({"zero": 1.0}, {}), {"zero": 1.0})  # no inventory: the style as it is


class NeighbourTests(unittest.TestCase):
    def test_day_is_not_cut_to_night_inside_a_section_when_a_day_shot_serves(self):
        day0, day2 = fake_take("D0", 0), fake_take("D2", 2)
        night, day = fake_take("N1", 1), fake_take("D1", 1)
        night.a["bright"][:] = 40.0
        night.a["sharp"][:] = 230.0  # a little sharper: alone, the night take wins the slot
        m = one_section("axis 0", "axis 1", "axis 2")
        with mock.patch.object(fill, "LIGHT_W", 0.0):
            alone = cut(m, [day0, night, day, day2])
        self.assertEqual(alone["slots"][1]["pick"]["name"], "N1.MP4")
        settled = cut(m, [day0, night, day, day2])
        self.assertEqual(settled["slots"][1]["pick"]["name"], "D1.MP4")
        self.assertEqual(evaluate.neighbours(settled, {t.fid: t for t in (day0, night, day, day2)})["light_jumps"], 0)
        two = {**m, "sections": [{"name": "day", "slots": m["sections"][0]["slots"][:1]}, {"name": "night", "slots": m["sections"][0]["slots"][1:2]},
                                 {"name": "day again", "slots": m["sections"][0]["slots"][2:]}]}
        self.assertEqual(cut(two, [day0, night, day, day2])["slots"][1]["pick"]["name"], "N1.MP4")  # a new section may change the light

    def test_the_same_take_is_not_played_on_both_sides_of_a_cut_when_another_serves(self):
        long = fake_take("L0", 0, n=40)
        near = fake_take("M0", 0)
        near.a["emb"] = np.tile(unit(np.eye(8)[0] + 0.45 * np.eye(8)[5]), (20, 1)).astype(np.float16)  # shows it less well, and is another picture
        other = fake_take("X1", 1)
        m = one_section("axis 0", "axis 0", "axis 1")
        with mock.patch.object(fill, "REPEAT_W", 0.0):
            alone = cut(m, [long, near, other])
        settled = cut(m, [long, near, other])
        names = lambda p: [s["pick"]["name"] for s in p["slots"]]  # noqa: E731
        if names(alone)[:2] == ["L0.MP4", "L0.MP4"]:  # the pool is smaller than the map: the best take served both
            self.assertEqual(sorted(names(settled)[:2]), ["L0.MP4", "M0.MP4"])
        self.assertEqual(evaluate.neighbours(settled, {t.fid: t for t in (long, near, other)})["repeats"], 0)

    def test_a_flicker_keeps_to_takes_that_show_its_pick_in_its_light(self):
        takes = [fake_take(f"C{k}", 2) for k in range(4)] + [fake_take("N2", 2), fake_take("Z5", 5)]
        takes[4].a["bright"][:] = 40.0  # the same thing at night
        m = one_section({"show": "axis 2", "flicker": 5})
        pieces = [p["name"] for p in cut(m, takes)["slots"][0]["pick"]["pieces"]]
        self.assertEqual(sorted(pieces), ["C0.MP4", "C1.MP4", "C2.MP4", "C3.MP4"])  # four good sub-shots, not five with a stray
        named = one_section({"show": "axis 2", "flicker": 5, "takes": ["C0*", "N2*", "Z5*"]})
        self.assertEqual(sorted(p["name"] for p in cut(named, takes)["slots"][0]["pick"]["pieces"]), ["C0.MP4", "N2.MP4", "Z5.MP4"])  # the map's own list stands

    def test_the_order_of_the_day_leans_early_slots_to_early_takes(self):
        takes = [fake_take(n, 0) for n in ("T100", "T200", "T300", "T400")]
        m = one_section("axis 0", "axis 0", "axis 0", "axis 0", order="shoot", repeat_take=False)
        self.assertEqual([s["pick"]["name"] for s in cut(m, takes)["slots"]], ["T100.MP4", "T200.MP4", "T300.MP4", "T400.MP4"])


class AutoLookTests(unittest.TestCase):
    def setUp(self):
        self.soft, self.sharp, self.rest = fake_take("S0", 0), fake_take("H0", 0), [fake_take(f"R{k}", k) for k in (1, 2, 3)]
        self.soft.a["sharp"][:] = 8.0
        self.takes = [self.soft, self.sharp, *self.rest]
        self.m = one_section("axis 0", "axis 1")

    def picks_with(self, pick, alt, score=1.0, alt_score=0.8) -> dict:
        w = lambda t, s: {"fid": t.fid, "name": t.name, "path": t.path, "in_s": 1.0, "out_s": 2.0, "score": s}  # noqa: E731
        return {"slots": [{"id": "s1", "show": "axis 0", "length_s": 1.0, "pick": w(pick, score), "alternates": [w(alt, alt_score)]},
                          {"id": "s2", "show": "axis 1", "length_s": 1.0, "pick": w(self.rest[0], 1.0), "alternates": []}]}

    def test_a_soft_pick_gives_way_to_a_sharp_runner_up_that_shows_the_same(self):
        picks = self.picks_with(self.soft, self.sharp)
        lines = autolook.apply(picks, self.takes)
        self.assertEqual(picks["slots"][0]["pick"]["name"], "H0.MP4")
        self.assertEqual(picks["slots"][0]["second_look"]["by"], "auto")
        self.assertIn("soft", lines[0])
        self.assertEqual(picks["slots"][0]["alternates"][0]["name"], "S0.MP4")  # the old pick stays within reach

    def test_it_leaves_alone_what_is_fine_what_scores_much_lower_and_what_would_repeat_a_neighbour(self):
        fine = self.picks_with(self.sharp, self.soft)
        self.assertEqual(autolook.apply(fine, self.takes), [])
        far = self.picks_with(self.soft, self.sharp, alt_score=1.0 - autolook.MARGIN - 0.1)
        self.assertEqual(autolook.apply(far, self.takes), [])  # sharper, but it no longer shows what the slot asks
        repeat = self.picks_with(self.soft, self.rest[0])
        self.rest[0].a["sharp"][:] = 400.0
        self.assertEqual(autolook.apply(repeat, self.takes), [])  # the runner-up is the next slot's shot
        flicker = self.picks_with(self.soft, self.sharp)
        flicker["slots"][0]["pick"]["pieces"] = [dict(flicker["slots"][0]["pick"])]
        self.assertEqual(autolook.apply(flicker, self.takes), [])

    def test_the_plain_tests(self):
        ok = {"sharp": 0.0, "light": 0.0, "shake": 0.3}
        self.assertIsNone(autolook.better(ok, ok))
        self.assertIn("soft", autolook.better({**ok, "sharp": -2.0}, ok))
        self.assertIsNone(autolook.better({**ok, "sharp": -2.0}, {**ok, "sharp": -1.5}))  # not sharper by enough
        self.assertIn("dark or blown", autolook.better({**ok, "light": 0.8}, ok))
        self.assertIn("shakiest", autolook.better({**ok, "shake": 0.95}, ok))
        self.assertIsNone(autolook.better({**ok, "shake": None}, ok))  # a keyframe map measured no shake


if __name__ == "__main__":
    unittest.main()
