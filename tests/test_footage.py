import os
import tempfile
import unittest
from pathlib import Path

from videtor.kb import footage, metrics, xmeml

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.xml"


class HelperTests(unittest.TestCase):
    def test_kind_of(self):
        self.assertEqual(footage.kind_of("C0011.MP4"), "video")
        self.assertEqual(footage.kind_of("track.WAV"), "audio")
        self.assertEqual(footage.kind_of("still.jpg"), "image")
        self.assertIsNone(footage.kind_of("project.prproj"))

    def test_merge_ranges(self):
        self.assertEqual(footage.merge_ranges([[5, 7], [1, 3], [2, 4], [7, 8]]), [[1, 4], [5, 8]])

    def test_outermost(self):
        dirs = ["/v/Vivo/Rohit", "/v/Vivo", "/v/Guru ", "/v/Vivo/Rohit/new"]
        self.assertEqual(footage.outermost(dirs), ["/v/Vivo", "/v/Guru "])

    def test_premiere_caches_are_skipped(self):
        self.assertTrue(footage.skipped("/v/JW 2/pf/Adobe Premiere Pro Video Previews/Untitled.PRV/Rendered - 02d7.mov"))
        self.assertTrue(footage.skipped("/v/Shoot/Media Cache Files/clip.cfa"))
        self.assertTrue(footage.skipped("/v/Shoot/cam a/card 1/M4ROOT/THMBNL/C0001T01.JPG"))
        self.assertFalse(footage.skipped("/v/Shoot/cam a/card 1/M4ROOT/CLIP/C0001.MP4"))
        self.assertFalse(footage.skipped("/v/Shoot/Cam A/1.mp4"))

    def test_render_folders_are_not_footage(self):
        self.assertTrue(footage.is_render("/v/WPL Gujarat Giants/render/final_v3.mp4"))
        self.assertTrue(footage.is_render("/v/Shoot/Exports/teaser.mov"))
        self.assertFalse(footage.is_render("/v/Shoot/cam a/final take.mp4"))  # only folder names count

    def test_file_id_is_stable(self):
        self.assertEqual(footage.file_id("/v/a.mp4"), footage.file_id("/v/a.mp4"))
        self.assertNotEqual(footage.file_id("/v/a.mp4"), footage.file_id("/v/b.mp4"))


class ScanTests(unittest.TestCase):
    def test_scan_indexes_project_folders_and_loose_files_at_the_top(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Shoot", "Cam A"))
            os.makedirs(os.path.join(tmp, "Shoot", "Adobe Premiere Pro Video Previews"))
            os.makedirs(os.path.join(tmp, "Unrelated"))
            for rel in ("Shoot/Cam A/1.mp4", "Shoot/Cam A/2.mp4", "Shoot/Adobe Premiere Pro Video Previews/r.mov", "song.wav", "Unrelated/x.mp4", "notes.txt"):
                Path(tmp, rel).write_bytes(b"x" * 10)
            projects = [{"key": "x.xml::S", "source_xml": "/nowhere/x.xml", "sources": [f"{tmp}/Shoot/Cam A/1.mp4"]}]
            index = footage.scan(tmp, projects, log=lambda s: None)
            names = sorted(e["basename"] for e in index["files"])
            self.assertEqual(names, ["1.mp4", "2.mp4", "song.wav"])  # cache folder and unrelated folder left out
            self.assertEqual(index["roots"], {"x.xml::S": [f"{tmp}/Shoot/Cam A"]})


class CoverageTests(unittest.TestCase):
    def test_frame_coverage_counts_missing_clip_and_used_range_frames(self):
        media = {"video": {"codec": "h264"}, "duration_s": 10.0}
        index = {"files": [
            {"fid": "a", "kind": "video", "media": media, "used_by": {"x": [[1, 2]]}},
            {"fid": "b", "kind": "video", "media": media, "used_by": {}},
            {"fid": "c", "kind": "video", "media": media, "used_by": {}, "duplicate_of": "b"},  # copies are not owed frames
            {"fid": "d", "kind": "audio", "media": {"duration_s": 5.0}, "used_by": {}},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            for name in ("a_0", "a_1", "a_2", "b_0"):
                (cache / f"{name}.jpg").write_bytes(b"")
            self.assertEqual(
                footage.frame_coverage(index, cache),
                {"clips": 2, "complete": 1, "missing": 1, "used_clips": 1, "missing_used_range": 1},
            )


class RootsAndRangesTests(unittest.TestCase):
    def test_project_roots_use_common_folder_on_the_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Shoot", "Cam A"))
            os.makedirs(os.path.join(tmp, "Shoot", "Cam B"))
            projects = [
                {"key": "x.xml::S", "sources": [f"{tmp}/Shoot/Cam A/1.mp4", f"{tmp}/Shoot/Cam B/2.mp4", "/Volumes/Other/3.mp4"]},
                {"key": "y.xml::T", "sources": ["/Volumes/Other/4.mp4"]},
            ]
            self.assertEqual(footage.project_roots(projects, tmp), {"x.xml::S": f"{tmp}/Shoot"})

    def test_manifest_can_add_footage_folders_to_a_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Shoot", "Cam A"))
            projects = [{"key": "x.xml::S", "sources": [f"{tmp}/Shoot/Cam A/1.mp4", f"{tmp}/Shoot/Cam A/2.mp4"]}]
            manifest = {"x.xml::S": [f"{tmp}/DRONE/", "/Volumes/Elsewhere/extra"], "y.xml::T": [f"{tmp}/Other"]}
            roots = footage.all_roots(projects, tmp, manifest)
            self.assertEqual(roots, {"x.xml::S": [f"{tmp}/Shoot/Cam A", f"{tmp}/DRONE"], "y.xml::T": [f"{tmp}/Other"]})

    def test_project_roots_ignore_a_stray_file_above_the_footage(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Shoot", "Cam A"))
            cam_a = [f"{tmp}/Shoot/Cam A/{i}.mp4" for i in range(5)]
            projects = [{"key": "x.xml::S", "sources": cam_a + [f"{tmp}/Shoot/old-export.mp4", f"{tmp}/music.wav"]}]
            # 5 of 6 video files sit in Cam A (83%), the audio file is not counted
            self.assertEqual(footage.project_roots(projects, tmp), {"x.xml::S": f"{tmp}/Shoot/Cam A"})

    def test_used_ranges_come_from_the_xml_in_file_seconds(self):
        m = metrics.sequence_metrics(xmeml.load(FIXTURE)[0])
        ranges = footage.used_ranges([m])
        fps = 24000 / 1001
        a001 = ranges["/Volumes/RAW/Day 1/A001.mov"][m["key"]]
        # video clip-1 and its linked audio clip-3 both use frames 10–106 of the file at the file's own 23.976 fps
        self.assertEqual(footage.merge_ranges(a001), [[round(10 / fps, 3), round(106 / fps, 3)]])
        # B001 has no file rate, so the sequence rate stands in; disabled and synthetic clips are absent
        self.assertIn("/Volumes/RAW/B001.mov", ranges)
        self.assertNotIn("/Volumes/RAW/Disabled.mov", ranges)
        # the nested clip on V2 plays all 24 frames of Nested Sequence 1: N001 then N002, 12 frames each
        self.assertEqual(ranges["/Volumes/RAW/N001.mov"][m["key"]], [[0.0, round(12 / fps, 3)]])
        self.assertEqual(ranges["/Volumes/RAW/N002.mov"][m["key"]], [[0.0, round(12 / fps, 3)]])

    def test_used_ranges_stop_at_transition_cut_points(self):
        seq = xmeml.load(FIXTURE.with_name("transitions.xml"))[0]
        ranges = [r for r in footage.footage_ranges(seq, {}) if r[0] != "/x/C.mov"]
        # A and B meet under a dissolve cut at frame 50; B plays at 50%; the music is cut with a crossfade at 100
        self.assertEqual(ranges, [("/x/A.mov", 4.0, 6.0), ("/x/B.mov", 4.1, 5.3), ("/x/m.wav", 0.0, 4.0), ("/x/m.wav", 12.0, 16.0)])

    def test_in_out_count_at_the_clipitem_rate_not_the_media_rate(self):
        # Premiere: a 100 fps file on a 25 fps sequence, clipitem rate 25, in/out 102–113 → 4.08–4.52 s of the file
        rate = xmeml.Rate(25, False)
        clip = xmeml.Clip("c", "C3950.MP4", "V1", 343, 354, 102, 113, True, "clip", file_path="/x/C3950.MP4", file_fps=100.0, clip_fps=25.0)
        seq = xmeml.Sequence("s", "Seq", "g.xml", rate, 354, None, None, "main", [xmeml.Track("V1", "video", [clip])])
        self.assertEqual(footage.footage_ranges(seq, {}), [("/x/C3950.MP4", 4.08, 4.52)])
        # a clipitem without its own rate falls back to the sequence rate
        clip.clip_fps = None
        self.assertEqual(footage.footage_ranges(seq, {}), [("/x/C3950.MP4", 4.08, 4.52)])

    def test_nested_ranges_follow_parent_in_out_and_speed(self):
        rate = xmeml.Rate(25, False)
        inner_clip = xmeml.Clip("n", "N.mov", "V1", 0, 100, 500, 600, True, "clip", file_path="/x/N.mov", file_fps=25.0)
        inner = xmeml.Sequence("seq-2", "Nest", "f.xml", rate, 100, None, None, "nested", [xmeml.Track("V1", "video", [inner_clip])])
        # parent shows inner frames 20–60 (40 frames) over 20 timeline frames: double speed
        nested_clip = xmeml.Clip("p", "Nest", "V1", 1000, 1020, 20, 60, True, "nested", nested_id="seq-2")
        parent = xmeml.Sequence("seq-1", "Main", "f.xml", rate, 1020, None, None, "main", [xmeml.Track("V1", "video", [nested_clip])])
        self.assertEqual(footage.footage_ranges(parent, {"seq-2": inner}), [("/x/N.mov", 520 / 25, 560 / 25)])
        # only the part of the parent clip inside the asked window counts
        self.assertEqual(footage.footage_ranges(parent, {"seq-2": inner}, start=1010), [("/x/N.mov", 540 / 25, 560 / 25)])

    def test_copies_are_flagged_and_the_used_one_is_kept(self):
        entries = [
            {"fid": "b", "path": "/v/consolidated/C0011.MP4", "basename": "C0011.MP4", "size": 10, "used_by": {}},
            {"fid": "a", "path": "/v/raw/C0011.MP4", "basename": "C0011.MP4", "size": 10, "used_by": {"x.xml::S": [[0, 1]]}},
            {"fid": "c", "path": "/v/raw/C0012.MP4", "basename": "C0012.MP4", "size": 10, "used_by": {}},
            {"fid": "d", "path": "/v/raw/other/C0011.MP4", "basename": "C0011.MP4", "size": 99, "used_by": {}},  # same name, different size
        ]
        self.assertEqual(footage.mark_duplicates(entries), 1)
        self.assertEqual([e["duplicate_of"] for e in entries], ["a", None, None, None])

    def test_a_file_saved_twice_with_a_suffix_is_a_copy(self):
        entries = [
            {"fid": "a", "path": "/v/3/C1869.MP4", "basename": "C1869.MP4", "size": 10, "used_by": {}},
            {"fid": "b", "path": "/v/3/C1869(1).MP4", "basename": "C1869(1).MP4", "size": 10, "used_by": {}},
            {"fid": "c", "path": "/v/5/C1891 copy.MP4", "basename": "C1891 copy.MP4", "size": 30, "used_by": {}},
            {"fid": "d", "path": "/v/5/C1891.MP4", "basename": "C1891.MP4", "size": 30, "used_by": {}},
            {"fid": "e", "path": "/v/3/C1870(1).MP4", "basename": "C1870(1).MP4", "size": 11, "used_by": {}},  # another size: a different file
            {"fid": "f", "path": "/v/3/C1870.MP4", "basename": "C1870.MP4", "size": 12, "used_by": {}},
        ]
        self.assertEqual(footage.mark_duplicates(entries), 2)
        self.assertEqual([e["duplicate_of"] for e in entries], [None, "a", "d", None, None, None])

    def test_canonical_name_folds_a_doubled_extension(self):
        self.assertEqual(footage.canonical_name("A_0008.MP4.mp4"), "a_0008.mp4")
        self.assertEqual(footage.canonical_name("C0011.MP4"), "c0011.mp4")
        self.assertEqual(footage.canonical_name("Take 2.final.mov"), "take 2.final.mov")  # only media extensions fold

    def test_rewrapped_copies_match_by_name_and_duration_but_cameras_do_not(self):
        m = lambda d: {"duration_s": d, "video": {}}  # noqa: E731
        entries = [
            {"fid": "raw", "path": "/v/coca cola/Day 1/CAM A/C0011.MP4", "basename": "C0011.MP4", "size": 900, "used_by": {}, "media": m(12.34)},
            {"fid": "cp", "path": "/v/Copied_Khanna Fish/C0011.MP4", "basename": "C0011.MP4", "size": 880, "used_by": {"c.xml::S": [[1, 2]]}, "media": m(12.31)},
            {"fid": "camb", "path": "/v/coca cola/Day 1/CAM B/C0011.MP4", "basename": "C0011.MP4", "size": 700, "used_by": {}, "media": m(12.34)},
            {"fid": "camc", "path": "/v/coca cola/Day 1/CAM C/C0011.MP4", "basename": "C0011.MP4", "size": 650, "used_by": {}, "media": m(99.0)},
        ]
        footage.mark_duplicates(entries)
        # the raw original and CAM B's same-length file both resolve to the used copy; CAM C differs in duration
        self.assertEqual([e["duplicate_of"] for e in entries], ["cp", None, "cp", None])
        two_cams = [dict(entries[0], duplicate_of=None), dict(entries[2], duplicate_of=None)]
        self.assertEqual(footage.mark_duplicates(two_cams), 0)  # no copy folder involved: left alone

    def test_trimmed_consolidated_copies_point_back_to_the_original(self):
        entries = [
            {"fid": "orig", "path": "/v/RAW/B018C003.mp4", "basename": "B018C003.mp4", "size": 900, "used_by": {"z.xml::F": [[1, 2]]}},
            {"fid": "trim", "path": "/v/Packswitch_consolidated/B018C003_001.mp4", "basename": "B018C003_001.mp4", "size": 40, "used_by": {}},
            {"fid": "cam", "path": "/v/RAW/C0011_001.MP4", "basename": "C0011_001.MP4", "size": 40, "used_by": {}},  # camera name, not a copy
        ]
        self.assertEqual(footage.mark_duplicates(entries), 1)
        self.assertEqual([e["duplicate_of"] for e in entries], [None, "orig", None])

    def test_match_used_is_case_insensitive_and_falls_back_to_basename(self):
        entries = [
            {"path": "/v/Shoot/C0011.MP4", "basename": "C0011.MP4"},
            {"path": "/v/Shoot/copy/track.wav", "basename": "track.wav"},
            {"path": "/v/Shoot/C0099.MP4", "basename": "C0099.MP4"},
        ]
        ranges = {"/v/Shoot/c0011.mp4": {"a.xml::S": [[1, 2]]}, "/Volumes/Other/Track.WAV": {"b.xml::T": [[0, 5]]}}
        footage.match_used(entries, ranges, "/v")
        self.assertEqual((entries[0]["used_by"], entries[0]["matched_by"]), ({"a.xml::S": [[1, 2]]}, "path"))
        self.assertEqual((entries[1]["used_by"], entries[1]["matched_by"]), ({"b.xml::T": [[0, 5]]}, "basename"))
        self.assertEqual((entries[2]["used_by"], entries[2]["matched_by"]), ({}, None))


if __name__ == "__main__":
    unittest.main()
