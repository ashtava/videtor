import io
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from videtor.export import xmeml_writer as w
from videtor.kb import readback as rb
from videtor.plan.build import build_from_picks

FIXTURES = Path(__file__).parent / "fixtures" / "readback"
PICKS_PATH = FIXTURES / "picks.json"
PICKS = json.loads(PICKS_PATH.read_text(encoding="utf-8"))
NO_IDS = rb.FootageIds()  # tests never read the real kb/footage indexes


def statuses(result: dict) -> list[str]:
    return [s["status"] for s in result["slots"]]


class ProposedCut:
    """picks.json written by Videtor's own writer (V1 picks, V2/V3 alternates switched off), then changed
    the way an editor would in Premiere before exporting."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        path = w.write_xmeml(build_from_picks(PICKS), tmp / "proposed.xml", fps=PICKS["fps"], width=1080, height=1920)
        self.tree = ET.parse(path)

    def track(self, n: int) -> ET.Element:
        return self.tree.getroot().find("sequence/media/video").findall("track")[n - 1]

    def clip(self, slot: int, track: int = 1) -> ET.Element:
        return self.track(track).findall("clipitem")[slot - 1]

    @staticmethod
    def _shift(el: ET.Element, tag: str, frames: int) -> None:
        el.find(tag).text = str(int(el.findtext(tag)) + frames)

    @staticmethod
    def _file(parent: ET.Element, path: str, fid: str) -> None:
        f = ET.SubElement(parent, "file", id=fid)
        ET.SubElement(f, "name").text = Path(path).name
        ET.SubElement(f, "pathurl").text = "file://localhost" + quote(path)

    def slip(self, slot: int, frames: int) -> None:
        for tag in ("in", "out"):
            self._shift(self.clip(slot), tag, frames)

    def trim_tail(self, slot: int, frames: int) -> None:
        for tag in ("end", "out"):
            self._shift(self.clip(slot), tag, -frames)

    def enable(self, slot: int, track: int) -> None:
        self.clip(slot, track).find("enabled").text = "TRUE"

    def relink(self, slot: int, path: str, in_frame: int | None = None, track: int = 1) -> None:
        """Overwrite the slot with another file (or another part of one), as dragging from the bin does."""
        c = self.clip(slot, track)
        c.remove(c.find("file"))
        self._file(c, path, f"file-edit-{track}-{slot}")
        c.find("name").text = Path(path).name
        if in_frame is not None:
            length = int(c.findtext("out")) - int(c.findtext("in"))
            c.find("in").text, c.find("out").text = str(in_frame), str(in_frame + length)

    def remove(self, slot: int, track: int = 1) -> None:
        self.track(track).remove(self.clip(slot, track))

    def append(self, path: str, start: int, end: int, in_frame: int = 0, track: int = 1) -> None:
        t = self.track(track)
        c = ET.Element("clipitem", id=f"clipitem-added-{start}")
        for tag, value in (("name", Path(path).name), ("enabled", "TRUE"), ("start", start), ("end", end), ("in", in_frame), ("out", in_frame + end - start)):
            ET.SubElement(c, tag).text = str(value)
        self._file(c, path, f"file-added-{start}")
        later = [x for x in t.findall("clipitem") if int(x.findtext("start")) >= start]
        t.insert(list(t).index(later[0]) if later else list(t).index(t.findall("clipitem")[-1]) + 1, c)

    # ripple edits move everything after the edit point on every (sync-locked) track

    def _shift_after(self, frame: int, frames: int) -> None:
        for n in (1, 2, 3):
            for c in self.track(n).findall("clipitem"):
                if int(c.findtext("start")) >= frame:
                    self._shift(c, "start", frames)
                    self._shift(c, "end", frames)

    def ripple_delete(self, slot: int) -> None:
        start = int(self.clip(slot).findtext("start"))
        length = int(self.clip(slot).findtext("end")) - start
        for n in (1, 2, 3):
            self.remove(slot, track=n)
        self._shift_after(start, -length)

    def ripple_trim(self, slot: int, frames: int) -> None:
        end = int(self.clip(slot).findtext("end"))
        for n in (1, 2, 3):
            for tag in ("end", "out"):
                self._shift(self.clip(slot, n), tag, -frames)
        self._shift_after(end, -frames)

    def ripple_insert(self, path: str, at: int, frames: int, in_frame: int = 0) -> None:
        self._shift_after(at, frames)
        self.append(path, at, at + frames, in_frame)

    def reorder(self, a: int, b: int) -> None:
        """Swap two slots' V1 clips on the timeline (the alternates stay put, switched off)."""
        ca, cb = self.clip(a), self.clip(b)
        for tag in ("start", "end"):
            ca.find(tag).text, cb.find(tag).text = cb.findtext(tag), ca.findtext(tag)
        t = self.track(1)
        ia, ib = list(t).index(ca), list(t).index(cb)
        t.remove(ca)
        t.insert(ib, ca)
        t.remove(cb)
        t.insert(ia, cb)

    def write(self) -> Path:
        path = self.tmp / "edited.xml"
        self.tree.write(path, encoding="utf-8", xml_declaration=True)
        return path

    def diff(self, ids: rb.FootageIds = NO_IDS) -> dict:
        return rb.diff(PICKS, rb.read_xml(self.write()), ids=ids)


class EditedCutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cut = ProposedCut(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_untouched_proposal_reads_back_as_kept(self):
        rows = rb.read_xml(self.cut.write())
        self.assertEqual(len(rows), 18)
        self.assertEqual({(r["track"], r["enabled"]) for r in rows}, {(1, True), (2, False), (3, False)})
        slow = next(r for r in rows if r["name"] == "A006.MP4")
        self.assertEqual((slow["in_s"], slow["out_s"]), (3.0, 3.5))  # 50%: stretched in/out back to source seconds
        result = rb.diff(PICKS, rows, ids=NO_IDS)
        self.assertEqual(statuses(result), ["kept"] * 6)
        self.assertEqual(result["added"], [])
        self.assertEqual(result["summary"]["kept"], 6)

    def test_slip_records_the_in_point_change(self):
        self.cut.slip(2, 10)
        slot = self.cut.diff()["slots"][1]
        self.assertEqual(slot["status"], "slipped")
        self.assertAlmostEqual(slot["delta_in_s"], 0.4)
        self.assertAlmostEqual(slot["delta_length_s"], 0.0)

    def test_changes_within_two_frames_are_kept(self):
        self.cut.slip(1, 2)
        slot = self.cut.diff()["slots"][0]
        self.assertEqual(slot["status"], "kept")
        self.assertAlmostEqual(slot["delta_in_s"], 0.08)

    def test_tail_trim_keeps_the_in_point(self):
        self.cut.trim_tail(3, 5)
        slot = self.cut.diff()["slots"][2]
        self.assertEqual(slot["status"], "trimmed")
        self.assertAlmostEqual(slot["delta_length_s"], -0.2)
        self.assertAlmostEqual(slot["delta_in_s"], 0.0)

    def test_enabling_v2_swaps_to_alternate_0(self):
        self.cut.enable(4, track=2)
        slot = self.cut.diff()["slots"][3]
        self.assertEqual((slot["status"], slot["alternate"]), ("swapped_to_alternate", 0))
        self.assertEqual((slot["visible"]["track"], slot["visible"]["name"]), (2, "B004.MP4"))

    def test_alternate_from_the_picks_own_take_is_not_a_slip(self):
        self.cut.enable(4, track=3)  # alternate 1 is A004.MP4 at 9 s, the pick is A004.MP4 at 5 s
        slot = self.cut.diff()["slots"][3]
        self.assertEqual((slot["status"], slot["alternate"]), ("swapped_to_alternate", 1))
        self.assertAlmostEqual(slot["delta_in_s"], 0.0)

    def test_alternate_dragged_onto_v1_and_slipped_still_counts(self):
        self.cut.relink(3, "/Volumes/RAW/Day 1/B003.MP4", in_frame=55)  # alternate 0 plays 2.0-3.0 s; this 2.2-3.2 s
        slot = self.cut.diff()["slots"][2]
        self.assertEqual((slot["status"], slot["alternate"]), ("swapped_to_alternate", 0))
        self.assertAlmostEqual(slot["delta_in_s"], 0.2)

    def test_another_part_of_an_alternates_file_is_a_swap(self):
        self.cut.relink(3, "/Volumes/RAW/Day 1/B003.MP4", in_frame=150)
        slot = self.cut.diff()["slots"][2]
        self.assertEqual(slot["status"], "swapped")
        self.assertNotIn("alternate", slot)

    def test_swap_to_another_file_records_the_new_clip(self):
        self.cut.relink(5, "/Volumes/RAW/Day 3/X009.MP4", in_frame=100)
        slot = self.cut.diff()["slots"][4]
        self.assertEqual(slot["status"], "swapped")
        v = slot["visible"]
        self.assertEqual((v["path"], v["name"], v["in_s"], v["out_s"], v["track"]), ("/Volumes/RAW/Day 3/X009.MP4", "X009.MP4", 4.0, 5.0, 1))

    def test_removing_the_pick_leaves_the_alternates_off(self):
        self.cut.remove(6)
        slot = self.cut.diff()["slots"][5]
        self.assertEqual((slot["status"], slot["visible"]), ("removed", None))

    def test_clip_after_the_last_slot_is_added(self):
        self.cut.append("/Volumes/RAW/Day 2/Y001.MP4", 150, 165, in_frame=25)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept"] * 6)
        [added] = result["added"]
        self.assertEqual((added["name"], added["start_s"], added["end_s"], added["in_s"], added["visible_s"]), ("Y001.MP4", 6.0, 6.6, 1.0, 0.6))

    def test_alternate_used_for_part_of_a_slot_is_also_visible(self):
        self.cut.enable(2, track=2)
        alt = self.cut.clip(2, track=2)
        for tag in ("end", "out"):
            ProposedCut._shift(alt, tag, -15)  # B002 now covers 1.0-1.4 s of the 1.0-2.0 s slot
        slot = self.cut.diff()["slots"][1]
        self.assertEqual(slot["status"], "kept")  # the midpoint still shows the pick
        [also] = slot["also_visible"]
        self.assertEqual((also["name"], also["track"], also["visible_s"]), ("B002.MP4", 2, 0.4))

    def test_ripple_delete_of_an_early_slot(self):
        self.cut.ripple_delete(2)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept", "removed", "kept", "kept", "kept", "kept"])
        self.assertEqual([s.get("delta_start_s") for s in result["slots"]], [0.0, None, -1.0, -1.0, -1.0, -1.0])
        self.assertEqual((result["added"], result["summary"]["moved"]), ([], 4))

    def test_ripple_trim_shifts_every_later_slot(self):
        self.cut.ripple_trim(3, 10)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept", "kept", "trimmed", "kept", "kept", "kept"])
        self.assertAlmostEqual(result["slots"][2]["delta_length_s"], -0.4)
        self.assertEqual([s["delta_start_s"] for s in result["slots"][3:]], [-0.4, -0.4, -0.4])

    def test_inserted_clip_is_added_and_the_rest_move_on(self):
        self.cut.ripple_insert("/Volumes/RAW/Day 2/Y001.MP4", at=50, frames=15)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept"] * 6)
        self.assertEqual([s["delta_start_s"] for s in result["slots"]], [0.0, 0.0, 0.6, 0.6, 0.6, 0.6])
        [added] = result["added"]
        self.assertEqual((added["name"], added["start_s"], added["end_s"]), ("Y001.MP4", 2.0, 2.6))

    def test_two_slots_swapped_in_order(self):
        self.cut.reorder(3, 4)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept"] * 6)
        self.assertEqual([s["delta_start_s"] for s in result["slots"]], [0.0, 0.0, 1.0, -1.0, 0.0, 0.0])
        self.assertEqual(result["slots"][2]["visible"]["name"], "A003.MP4")
        self.assertEqual(result["added"], [])

    def test_every_kind_of_fix_at_once(self):
        self.cut.slip(2, 10)
        self.cut.trim_tail(3, 5)
        self.cut.enable(4, track=2)
        self.cut.relink(5, "/Volumes/RAW/Day 3/X009.MP4")
        self.cut.remove(6)
        self.cut.append("/Volumes/RAW/Day 2/Y001.MP4", 150, 165)
        result = self.cut.diff()
        self.assertEqual(statuses(result), ["kept", "slipped", "trimmed", "swapped_to_alternate", "swapped", "removed"])
        self.assertEqual(
            result["summary"],
            {"slots": 6, "kept": 1, "trimmed": 1, "slipped": 1, "swapped_to_alternate": 1, "swapped": 1, "removed": 1, "added": 1,
             "moved": 0, "file_match_share": 0.667},  # 4 of 6 slots still show one of their own files
        )


class PremiereExportTests(unittest.TestCase):
    """The hand-written fixture carries the quirks of Premiere's own exports."""

    def setUp(self):
        self.rows = rb.read_xml(FIXTURES / "premiere_export.xml")
        self.by_name = {}
        for r in self.rows:
            self.by_name.setdefault(r["name"], []).append(r)

    def span(self, name: str, track: int = 1) -> tuple:
        r = next(r for r in self.by_name[name] if r["track"] == track)
        return r["start_s"], r["end_s"], r["in_s"], r["out_s"]

    def test_dissolve_moves_both_clips_to_the_cut_point(self):
        self.assertEqual(self.span("A001.MP4"), (0.0, 1.0, 2.0, 3.0))  # end -1; 100 fps media counted at 25 fps
        self.assertEqual(self.span("A002.MP4"), (1.0, 2.0, 1.0, 2.0))  # start -1; in moved past the dissolve's first half

    def test_fade_to_black_ends_at_its_cut_point(self):
        self.assertEqual(self.span("Z001.MP4"), (6.0, 6.4, 0.0, 0.4))  # the fade runs to 6.44 s, the cut is at 6.4 s

    def test_time_remap_in_out_are_source_seconds(self):
        self.assertEqual(self.span("A006.MP4"), (5.0, 6.0, 3.0, 3.5))

    def test_graphics_are_skipped_nests_kept_and_track_output_read(self):
        self.assertFalse({"Adjustment Layer", "Text", "track.wav"} & set(self.by_name))
        [nest] = self.by_name["Nested Sequence 01"]
        self.assertEqual((nest["path"], nest["nested"], nest["enabled"], nest["track"]), (None, True, True, 2))
        hidden = next(r for r in self.by_name["A004.MP4"] if r["track"] == 3)
        self.assertEqual((hidden["enabled"], hidden["track_enabled"], hidden["in_s"]), (True, False, 9.0))
        self.assertEqual(self.by_name["A003.MP4"][0]["path"], "/Volumes/RAW/Day 1/CAFÉ/A003.MP4")

    def test_diff_against_the_picks(self):
        result = rb.diff(PICKS, self.rows, ids=NO_IDS, source="xml:fixture")
        # A003 matches through NFC + lower case; the enabled A004 on the hidden V3 does not show; the nest swaps slot 5
        self.assertEqual(statuses(result), ["kept", "kept", "kept", "kept", "swapped", "kept"])
        self.assertTrue(result["slots"][4]["visible"]["nested"])
        [added] = result["added"]
        self.assertEqual((added["name"], added["visible_s"]), ("Z001.MP4", 0.4))

    def test_sequence_by_name(self):
        self.assertEqual(len(rb.read_xml(FIXTURES / "premiere_export.xml", "readback-fixture")), len(self.rows))
        with self.assertRaises(rb.ReadbackError):
            rb.read_xml(FIXTURES / "premiere_export.xml", "Sequence 99")


class FootageIdsTests(unittest.TestCase):
    def test_relinked_copy_matches_through_its_fid(self):
        with tempfile.TemporaryDirectory() as tmp:
            cut = ProposedCut(Path(tmp))
            cut.relink(1, "/Volumes/BACKUP/Guru copy/a001.mp4")
            ids = rb.FootageIds.from_entries(
                [
                    {"fid": "fid-A001", "path": "/Volumes/RAW/Day 1/A001.MP4", "duplicate_of": None},
                    {"fid": "fid-copy", "path": "/Volumes/BACKUP/Guru copy/A001.MP4", "duplicate_of": "fid-A001"},
                ]
            )
            slot = cut.diff(ids)["slots"][0]
            self.assertEqual((slot["status"], slot["visible"]["fid"]), ("kept", "fid-A001"))
            self.assertEqual(cut.diff(NO_IDS)["slots"][0]["status"], "swapped")  # without the index it is another file

    def test_loads_only_the_footage_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            footage = Path(tmp, "footage")
            footage.mkdir()
            entry = {"fid": "abc", "path": "/Volumes/X/Café.MP4", "duplicate_of": None}
            (footage / "x.json").write_text(json.dumps({"mount": "/Volumes/X", "files": [entry]}))
            (footage / "x.summary.json").write_text(json.dumps({"files": [{"fid": "zzz", "path": "/nope"}]}))
            (footage / "x.selects.json").write_text("[]")
            ids = rb.load_footage_ids(Path(tmp))
            self.assertEqual(ids.by_path, {"/volumes/x/café.mp4": "abc"})
            self.assertEqual(ids.fid("/VOLUMES/X/Café.mp4"), "abc")
            self.assertEqual(rb.load_footage_ids(Path(tmp, "missing")).by_path, {})


LIVE_OUTPUT = {
    "sequence": "Sequence 01",
    "fps": 25,
    "video_tracks": 3,
    "clips": [
        {"track": 1, "track_enabled": True, "name": "C3972.MP4", "start_s": 12.24, "end_s": 13.36, "in_s": 74.88, "out_s": 76,
         "disabled": True, "adjustment": False, "speed": 0.25, "path": "/Volumes/Paritosh_1/Guru /data/C3972.mp4", "nested": False},
        {"track": 2, "track_enabled": False, "name": "Nested Sequence 17", "start_s": 0, "end_s": 1.76, "in_s": 0, "out_s": 1.76,
         "disabled": False, "adjustment": False, "speed": 1, "path": None, "nested": True},
        {"track": 3, "track_enabled": True, "name": "Adjustment Layer", "start_s": 18.04, "end_s": 18.24, "in_s": 3600, "out_s": 3600.2,
         "disabled": False, "adjustment": True, "speed": 1, "path": "1112293707", "nested": False},
        {"track": 3, "track_enabled": True, "name": "Color Matte", "start_s": 1, "end_s": 2, "in_s": 0, "out_s": 1,
         "disabled": False, "adjustment": False, "speed": 1, "path": "", "nested": False},
    ],
}


class LiveTests(unittest.TestCase):
    def test_rows_undo_stretched_in_out_and_skip_graphics(self):
        rows = rb.live_rows(LIVE_OUTPUT["clips"])
        self.assertEqual([r["name"] for r in rows], ["C3972.MP4", "Nested Sequence 17"])
        slow, nest = rows
        self.assertEqual((slow["in_s"], slow["out_s"], slow["enabled"]), (18.72, 19.0, False))  # 74.88 s at 25% is 18.72 s in
        self.assertEqual((nest["path"], nest["nested"], nest["track_enabled"]), (None, True, False))

    def test_runs_the_read_only_script_through_prpr_exec(self):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"], seen["script"] = cmd, Path(cmd[-1]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(LIVE_OUTPUT), stderr="")

        with mock.patch.object(rb.subprocess, "run", side_effect=fake_run):
            read = rb.live_sequence("Sequence 01")
        self.assertEqual(seen["cmd"][1:-1], ["--no-launch", "--timeout", "15", "-f", "json", "exec"])
        self.assertTrue(seen["script"].startswith('const READBACK_ARGS = {"sequence": "Sequence 01", "prefer": null};\n'))
        for mutation in ("executeTransaction", "Action("):  # getters only: no transactions, no edit actions
            self.assertNotIn(mutation, seen["script"])
        self.assertEqual((read["sequence"], len(read["clips"])), ("Sequence 01", 2))

    def test_live_readback_prefers_the_picks_sequence_and_records_its_length(self):
        seen = {}
        output = dict(LIVE_OUTPUT, sequence="readback-fixture", duration_s=6.0, clips=[])

        def fake_run(cmd, **kwargs):
            seen["script"] = Path(cmd[-1]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(output), stderr="")

        with mock.patch.object(rb.subprocess, "run", side_effect=fake_run):
            result = rb.readback(PICKS_PATH, live=True, kb_dir=Path("/nonexistent"))
        self.assertIn('{"sequence": null, "prefer": "readback-fixture"}', seen["script"])
        self.assertEqual((result["source"], result["sequence"], result["sequence_duration_s"]), ("live:readback-fixture", "readback-fixture", 6.0))
        self.assertEqual(statuses(result), ["removed"] * 6)
        self.assertFalse(result["guard"]["suspect"])  # nothing matches, but it is the picks' own sequence

    def test_prpr_errors_surface_as_readback_errors(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=json.dumps({"type": "PrprError", "message": "Sequence not found: X"}))
        with mock.patch.object(rb.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(rb.ReadbackError, "Sequence not found: X"):
                rb.read_live("X")


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kb = Path(self.tmp.name, "kb")
        cut = ProposedCut(Path(self.tmp.name))
        cut.slip(2, 10)
        cut.remove(6)
        self.edited = cut.write()

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_feedback(self):
        result = rb.diff(PICKS, rb.read_xml(self.edited), ids=NO_IDS, source=f"xml:{self.edited}", picks_path=str(PICKS_PATH))
        path = rb.write_feedback(result, self.kb)
        self.assertEqual(path, self.kb / "feedback" / "readback-fixture.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertLessEqual({"picks", "source", "read_at", "slots", "added", "summary"}, set(doc))
        self.assertEqual((doc["picks"], doc["source"]), (str(PICKS_PATH), f"xml:{self.edited}"))
        self.assertEqual(doc["slots"][1]["pick"]["name"], "A002.MP4")  # the slot's own pick travels with the label

    def test_main_prints_the_summary_and_saves_feedback(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = rb.main([str(PICKS_PATH), "--xml", str(self.edited), "--kb", str(self.kb)])
        text = out.getvalue()
        self.assertEqual(code, 0)
        [line] = [l for l in text.splitlines() if l.strip().startswith("open-2")]
        self.assertEqual(line.split()[1:], ["slipped", "in", "+0.40", "s"])
        self.assertIn("summary: 6 slots · kept 4 · trimmed 0 · slipped 1 · swapped_to_alternate 0 · swapped 0 · removed 1 · added 0", text)
        doc = json.loads((self.kb / "feedback" / "readback-fixture.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["summary"]["removed"], 1)


class GuardTests(unittest.TestCase):
    """An XML project holding the proposed cut ("readback-fixture") and a busier, unrelated "Sequence 01"."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.kb = tmp / "kb"
        proposal = ProposedCut(tmp)
        other = ProposedCut(tmp)
        for slot in range(1, 7):
            other.relink(slot, f"/Volumes/RAW/Other/Z00{slot}.MP4")
        other.append("/Volumes/RAW/Other/Z007.MP4", 150, 175)
        other.append("/Volumes/RAW/Other/Z008.MP4", 175, 200)
        seq_other = other.tree.getroot().find("sequence")
        seq_other.set("id", "sequence-2")
        seq_other.find("name").text = "Sequence 01"
        seq_other.find("duration").text = "200"  # 8 s, with the two appended clips
        root = ET.Element("xmeml", version="4")
        children = ET.SubElement(ET.SubElement(root, "project"), "children")
        children.append(proposal.tree.getroot().find("sequence"))
        children.append(seq_other)
        self.xml = tmp / "project.xml"
        ET.ElementTree(root).write(self.xml, encoding="utf-8", xml_declaration=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_the_sequence_named_after_the_picks_by_default(self):
        result = rb.readback(PICKS_PATH, xml=self.xml, kb_dir=self.kb)
        self.assertEqual((result["sequence"], result["sequence_duration_s"]), ("readback-fixture", 6.0))
        self.assertEqual(statuses(result), ["kept"] * 6)
        self.assertEqual(result["guard"], {"file_match_share": 1.0, "same_name": True, "suspect": False})

    def test_another_sequence_is_not_saved_as_feedback_without_force(self):
        result = rb.readback(PICKS_PATH, xml=self.xml, sequence="Sequence 01", kb_dir=self.kb)
        self.assertEqual((result["sequence"], result["sequence_duration_s"]), ("Sequence 01", 8.0))
        self.assertEqual((result["guard"]["file_match_share"], result["guard"]["suspect"]), (0.0, True))
        with self.assertRaisesRegex(rb.ReadbackError, "does not look like the cut"):
            rb.write_feedback(result, self.kb)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch.object(rb.sys, "stderr", err):
            self.assertEqual(rb.main([str(PICKS_PATH), "--xml", str(self.xml), "--sequence", "Sequence 01", "--kb", str(self.kb)]), 1)
        self.assertIn("--force", err.getvalue())
        self.assertFalse((self.kb / "feedback").exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rb.main([str(PICKS_PATH), "--xml", str(self.xml), "--sequence", "Sequence 01", "--kb", str(self.kb), "--force"]), 0)
        doc = json.loads((self.kb / "feedback" / "readback-fixture.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["sequence"], doc["sequence_duration_s"], doc["guard"]["suspect"]), ("Sequence 01", 8.0, True))

    def test_the_picks_own_sequence_is_saved_however_much_it_changed(self):
        cut = ProposedCut(Path(self.tmp.name))
        for slot in range(1, 7):
            cut.relink(slot, f"/Volumes/RAW/Other/Z00{slot}.MP4")
        result = rb.readback(PICKS_PATH, xml=cut.write(), kb_dir=self.kb)
        self.assertEqual((statuses(result), result["guard"]["suspect"]), (["swapped"] * 6, False))
        self.assertTrue(rb.write_feedback(result, self.kb).exists())


if __name__ == "__main__":
    unittest.main()
