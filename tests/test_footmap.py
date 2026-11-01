import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from videtor.analyze import footmap
from videtor.kb import footage

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
KEY = "Test.xml::Sequence 01"
CONTRACT = {
    "t": (np.float32, ()), "emb": (np.float16, (512,)), "sharp": (np.float32, ()), "bright": (np.float32, ()),
    "faces": (np.int8, ()), "face_area": (np.float32, ()), "face_box": (np.float32, (4,)), "partial": (np.bool_, ()),
    "hands": (np.int8, ()), "cam_dx": (np.float32, ()), "cam_dy": (np.float32, ()), "cam_zoom": (np.float32, ()),
    "cam_rot": (np.float32, ()), "shake": (np.float32, ()), "subject_motion": (np.float32, ()), "jump": (np.bool_, ()),
}
PAN_FW_PER_S = 200 / 640  # the pan clip's content moves right 200 px/s across a 640 px frame


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def entry_for(path: Path) -> dict:
    """An index entry like `kb footage` writes."""
    media = footage.probe(str(path))
    return {"fid": footage.file_id(str(path)), "path": str(path), "basename": path.name, "kind": "video", "media": media, "used_by": {}}


def coarse(rgb: np.ndarray, width: int = 60) -> np.ndarray:
    """Box-filtered to `width` px, so JPEG noise on the random test texture averages out."""
    im = Image.fromarray(np.ascontiguousarray(rgb).astype(np.uint8))
    return np.asarray(im.resize((width, round(width * im.height / im.width)), Image.BOX), dtype=np.float32)


def fake_embed(images: list) -> np.ndarray:
    """Stands in for CLIP: a unit vector per image from its mean colour."""
    out = np.zeros((len(images), 512), np.float32)
    for i, im in enumerate(images):
        out[i, :3] = np.asarray(im, dtype=np.float32).mean(axis=(0, 1)) + 1.0
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def fake_detect(rgb: np.ndarray) -> tuple[list[list[float]], int]:
    """Stands in for MediaPipe: a small face on the edge and a larger one inside, and one hand."""
    return [[0.0, 0.1, 0.1, 0.2], [0.4, 0.3, 0.6, 0.55]], 1


class GeometryTests(unittest.TestCase):
    def test_editor_rotation_turns_only_the_sideways_camcorder_takes(self):
        self.assertEqual(footmap.editor_rotation({"basename": "M2U00401.MPG"}), -90)
        self.assertEqual(footmap.editor_rotation({"basename": "m2u00099.mpg"}), -90)
        self.assertEqual(footmap.editor_rotation({"path": "/Volumes/X/Guru /CC/M2U00402.MPG"}), -90)
        self.assertEqual(footmap.editor_rotation({"basename": "C3953.MP4"}), 0)
        self.assertEqual(footmap.editor_rotation({"basename": "clip_M2U.mp4"}), 0)

    def test_display_matrix_follows_ffmpeg_autorotate(self):
        sony = [0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30]  # ffprobe "rotation": 90 (C3953.MP4)
        iphone = [0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30]  # ffprobe "rotation": -90
        self.assertEqual(footmap._rotation_filters(90, sony), (["transpose=cclock"], True))
        self.assertEqual(footmap._rotation_filters(-90, iphone), (["transpose=clock"], True))
        self.assertEqual(footmap._rotation_filters(180, [-65536, 0, 0, 0, -65536, 0, 0, 0, 1 << 30]), (["hflip", "vflip"], False))
        self.assertEqual(footmap._rotation_filters(0, None), ([], False))
        self.assertEqual(footmap._display_matrix("\n00000000:            0      -65536           0\n00000001:        65536           0           0\n00000002:            0           0  1073741824\n"), sony)

    def test_portrait_4k_and_sideways_sd_both_come_out_480_wide_and_upright(self):
        sony = {"width": 3840, "height": 2160, "sar": 1.0, "rotation": 90.0, "matrix": [0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30]}
        g = footmap.frame_geometry(sony, 0)
        self.assertEqual((g["display_w"], g["display_h"], g["frame_w"], g["frame_h"]), (2160, 3840, 480, 854))
        self.assertEqual((g["scale_w"], g["scale_h"], g["ffmpeg_w"], g["ffmpeg_h"], g["rot90"]), (854, 480, 480, 854, 0))
        # 720x576 16:9 anamorphic, turned -90 by the editor: Premiere's ~188% fill of 1080 wide is 1080 / 576
        sd = {"width": 720, "height": 576, "sar": 64 / 45, "rotation": 0.0, "matrix": None}
        g = footmap.frame_geometry(sd, -90)
        self.assertEqual((g["display_w"], g["display_h"], g["frame_w"], g["frame_h"]), (576, 1024, 480, 854))
        self.assertEqual((g["ffmpeg_w"], g["ffmpeg_h"], g["rot90"]), (854, 480, 1))
        self.assertEqual((g["motion_w"], g["motion_h"]), (320, 569))


class SeriesTests(unittest.TestCase):
    def test_smoothing_skips_nans_and_handles_short_takes(self):
        x = np.array([1.0, np.nan, 3.0])
        np.testing.assert_allclose(footmap._smooth(x), [2.0, 2.0, 2.0])
        self.assertEqual(footmap._smooth(np.zeros(0)).shape, (0,))
        self.assertTrue(np.isnan(footmap._smooth(np.full(4, np.nan))).all())

    def test_windows_average_four_steps_per_sample(self):
        x = np.array([1, 1, 3, 3, 5, np.nan, np.nan, np.nan, 9.0])
        np.testing.assert_allclose(footmap._windows(x, 3), [2.0, 5.0, 9.0])
        self.assertTrue(np.isnan(footmap._windows(np.zeros(2), 2)[1]))

    def test_steady_pan_on_an_uneven_cadence_is_not_shake(self):
        # a 25 fps source sampled at 8 fps: steps of 3, 3, 3, 4 frames, each moving in proportion
        frame_t = np.concatenate([[0.0], np.cumsum(np.tile([0.12, 0.12, 0.12, 0.16], 10))])
        dt = np.diff(frame_t)
        steps = {"dx": 100 * dt, "dy": np.zeros_like(dt), "log_scale": np.zeros_like(dt), "rot": np.zeros_like(dt),
                 "subject": np.zeros_like(dt), "diff": np.full_like(dt, 5.0)}
        mot = footmap.summarize(frame_t, steps, 9, 320, 180)
        np.testing.assert_allclose(mot["cam_dx"], 100 / 320, rtol=1e-6)
        self.assertLess(float(np.nanmax(mot["shake"])), 1e-9)
        nominal = footmap.summarize(np.arange(41) / 8, steps, 9, 320, 180)  # the same motion read on nominal ticks
        self.assertGreater(float(np.nanmax(nominal["shake"])), 1e-3)

    def test_jump_rule_matches_shot_boundaries(self):
        diffs = np.array([3.0, 2.5, 40.0, 3.0, 2.0, 30.0, 31.0, 2.0])
        self.assertEqual(np.flatnonzero(footmap.jump_steps(diffs)).tolist(), [2, 5, 6])


class PoolTests(unittest.TestCase):
    def test_pool_keeps_video_takes_under_the_roots(self):
        def f(fid, path, kind="video", dup=None, used=None):
            media = {"duration_s": 3.0, "video": {"width": 640, "height": 360, "fps": 25.0}} if kind == "video" else {"duration_s": 3.0}
            return {"fid": fid, "path": path, "basename": Path(path).name, "kind": kind, "media": media, "used_by": used or {}, "duplicate_of": dup}

        index = {"mount": "/Volumes/T", "roots": {KEY: ["/Volumes/T/Shoot"]}, "files": [
            f("a", "/Volumes/T/Shoot/A001.MP4"),
            f("b", "/Volumes/T/Shoot/A002.MP4", dup="a"),
            f("c", "/Volumes/T/Shoot/exports/cut.mp4"),
            f("d", "/Volumes/T/Shoot/voice.wav", kind="audio"),
            f("e", "/Volumes/T/Other/B001.MP4"),
            f("g", "/Volumes/T/Other/B002.MP4", used={KEY: [[0, 1]]}),
            {**f("h", "/Volumes/T/Shoot/A003.MP4"), "excluded": "another shoot on the same card"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([e["fid"] for e in footmap.pool(index, KEY, Path(tmp))], ["a", "g"])
        from videtor.cli import _pool_for

        self.assertNotIn("h", [e["fid"] for e in _pool_for(index, KEY)])


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class DecodeTests(unittest.TestCase):
    """Synthetic clips with known camera motion, decoded once for the class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = cls.tmp = Path(cls._tmp.name)
        cls.cache = tmp / "footmap"
        rng = np.random.default_rng(7)
        blocks = rng.integers(0, 256, (50, 200, 3), dtype=np.uint8)
        Image.fromarray(np.kron(blocks, np.ones((8, 8, 1), np.uint8))).save(tmp / "tex.png")  # 1600x400, corners everywhere
        src = ["-loop", "1", "-framerate", "25", "-i", str(tmp / "tex.png"), "-t", "3"]
        enc = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        ffmpeg(*src, "-vf", "crop=640:360:400:20", *enc, str(tmp / "static.mp4"))
        ffmpeg(*src, "-vf", "crop=640:360:'800-200*t':20:exact=1", *enc, str(tmp / "pan.mp4"))
        ffmpeg(*src, "-vf", "crop=640:360:'if(lt(t,1.5),100,900)':20", *enc, str(tmp / "cut.mp4"))
        ffmpeg(*src, "-vf", "crop=640:360:480:20,zoompan=z='1+0.15*it':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s=640x360:fps=25", *enc, str(tmp / "zoom.mp4"))
        ffmpeg("-display_rotation", "90", "-i", str(tmp / "pan.mp4"), "-c", "copy", str(tmp / "rotated.mp4"))
        shutil.copy(tmp / "pan.mp4", tmp / "M2U00001.mp4")
        cls.entries, cls.decoded = {}, {}
        for name in ("static", "pan", "cut", "zoom", "rotated", "M2U00001"):
            e = cls.entries[name] = entry_for(tmp / f"{name}.mp4")
            res = footmap.decode_take(e, cls.cache)
            assert res["ok"], res
            with np.load(cls.cache / f"{e['fid']}.decode.npz") as z:
                d = {k: z[k] for k in z.files}
            d["meta"] = json.loads(str(d["meta"]))
            cls.decoded[name] = d

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def interior(self, name: str, key: str) -> np.ndarray:
        return self.decoded[name][key][1:-1]  # the first and last half-second see the moving average shrink

    def test_samples_follow_the_contract(self):
        d = self.decoded["static"]
        n = math.floor(self.entries["static"]["media"]["duration_s"] * 2)
        self.assertEqual(n, 6)
        np.testing.assert_allclose(d["t"], (np.arange(n) + 0.5) / 2)
        for key in ("t", "sharp", "bright", "cam_dx", "cam_dy", "cam_zoom", "cam_rot", "shake", "subject_motion"):
            self.assertEqual((d[key].dtype, d[key].shape), (np.dtype(np.float32), (n,)), key)
        self.assertEqual((d["jump"].dtype, d["jump"].shape), (np.dtype(bool), (n,)))
        meta = d["meta"]
        self.assertEqual({k: meta[k] for k in ("n", "sample_fps", "motion_fps", "version", "display_w", "display_h", "rotation", "applied_rotation")},
                         {"n": n, "sample_fps": 2, "motion_fps": 8, "version": 1, "display_w": 640, "display_h": 360, "rotation": 0, "applied_rotation": 0})
        frames = sorted((self.cache / "frames").glob(f"{self.entries['static']['fid']}_*.jpg"))
        self.assertEqual(len(frames), n)
        with Image.open(frames[0]) as im:
            self.assertEqual(im.size, (480, 270))
        self.assertTrue((d["sharp"] > 50).all() and (d["bright"] > 50).all())

    def test_static_shot_has_no_camera_motion(self):
        d = self.decoded["static"]
        for key in ("cam_dx", "cam_dy", "cam_zoom", "cam_rot", "shake", "subject_motion"):
            self.assertLess(float(np.nanmax(np.abs(d[key]))), 0.01, key)
        self.assertFalse(np.isnan(d["cam_dx"]).any())
        self.assertFalse(d["jump"].any())

    def test_pan_reads_as_steady_rightward_camera_motion(self):
        cam_dx = self.interior("pan", "cam_dx")
        np.testing.assert_allclose(cam_dx, PAN_FW_PER_S, rtol=0.1)
        self.assertLess(float(np.abs(self.interior("pan", "cam_dy")).max()), 0.02)
        self.assertLess(float(self.interior("pan", "shake").max()), 0.003)  # despite the 3, 3, 3, 4 frame cadence
        self.assertLess(float(self.interior("pan", "subject_motion").max()), 0.02)
        self.assertEqual(sorted(set(np.round(np.diff(self.decoded["pan"]["frame_t"]), 2).tolist())), [0.12, 0.16])

    def test_push_in_reads_as_positive_zoom(self):
        self.assertGreater(float(self.interior("zoom", "cam_zoom").min()), 0.05)
        self.assertLess(float(np.abs(self.interior("zoom", "cam_dx")).max()), 0.05)

    def test_hard_cut_marks_one_window_and_is_left_out_of_the_motion(self):
        d = self.decoded["cut"]
        self.assertEqual(d["jump"].sum(), 1)
        self.assertIn(int(np.flatnonzero(d["jump"])[0]), (2, 3))  # the cut is at 1.5 s
        self.assertLess(float(np.nanmax(np.abs(d["cam_dx"]))), 0.01)
        self.assertEqual(len(d["meta"]["cuts_s"]), 1)

    def test_display_matrix_is_applied_after_scaling(self):
        d, e = self.decoded["rotated"], self.entries["rotated"]
        self.assertEqual((d["meta"]["rotation"], d["meta"]["display_w"], d["meta"]["display_h"]), (90, 360, 640))
        with Image.open(footmap.frame_path(self.cache, e["fid"], 2)) as im:
            ours = np.asarray(im.convert("RGB"))
        self.assertEqual(ours.shape, (854, 480, 3))
        run = subprocess.run(  # ffmpeg's own autorotation, on the same 8 fps frame
            ["ffmpeg", "-v", "error", "-i", e["path"], "-vf", "fps=8:round=up:start_time=0,scale=480:854", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        )
        theirs = np.frombuffer(run.stdout, np.uint8).reshape(-1, 854, 480, 3)[4 * 2 + 2]
        self.assertLess(float(np.abs(coarse(ours) - coarse(theirs)).mean()), 5.0)
        self.assertGreater(float(np.abs(coarse(ours) - coarse(theirs[::-1, ::-1])).mean()), 20.0)
        # content moving right in the stored frame moves up once turned 90 degrees counter-clockwise
        self.assertLess(float(self.interior("rotated", "cam_dy").max()), -0.8 * PAN_FW_PER_S)

    def test_m2u_takes_are_turned_like_the_editor_turns_them(self):
        d, e = self.decoded["M2U00001"], self.entries["M2U00001"]
        self.assertEqual((d["meta"]["applied_rotation"], d["meta"]["display_w"], d["meta"]["display_h"]), (-90, 360, 640))
        with Image.open(footmap.frame_path(self.cache, e["fid"], 1)) as im:
            turned = np.asarray(im.convert("RGB"))
        with Image.open(footmap.frame_path(self.cache, self.entries["pan"]["fid"], 1)) as im:
            plain = coarse(np.asarray(im.convert("RGB")))
        self.assertEqual(turned.shape, (854, 480, 3))
        self.assertLess(float(np.abs(coarse(np.rot90(turned, -1)) - plain).mean()), 5.0)  # np.rot90(frame, 1) undone
        self.assertGreater(float(np.abs(coarse(np.rot90(turned, 1)) - plain).mean()), 20.0)
        np.testing.assert_allclose(self.interior("M2U00001", "cam_dy"), -PAN_FW_PER_S, rtol=0.12)
        self.assertLess(float(np.abs(self.interior("M2U00001", "cam_dx")).max()), 0.02)

    def test_content_merge_writes_the_planner_contract(self):
        e = self.entries["pan"]
        footmap.content_take(e, self.cache, embed=fake_embed, detect=fake_detect)
        self.assertTrue(footmap.is_done(e, self.cache))
        m = footmap.load(e["fid"], self.cache)
        n = m["meta"]["n"]
        self.assertEqual(set(m) - {"meta"}, set(CONTRACT))
        for key, (dtype, tail) in CONTRACT.items():
            self.assertEqual((m[key].dtype, m[key].shape), (np.dtype(dtype), (n, *tail)), key)
        np.testing.assert_allclose(np.linalg.norm(m["emb"].astype(np.float32), axis=1), 1.0, atol=2e-3)
        self.assertTrue((m["faces"] == 2).all() and (m["hands"] == 1).all())
        np.testing.assert_allclose(m["face_area"], 0.05, atol=1e-6)  # the larger of the two faces
        np.testing.assert_allclose(m["face_box"][0], [0.4, 0.3, 0.6, 0.55], atol=1e-6)
        self.assertFalse(m["partial"].any())
        for key in ("fid", "path", "duration_s", "display_w", "display_h", "rotation", "fps", "n", "sample_fps", "motion_fps", "version", "seconds", "applied_rotation"):
            self.assertIn(key, m["meta"])
        np.testing.assert_array_equal(m["cam_dx"], self.decoded["pan"]["cam_dx"])

    def test_pool_run_resumes_and_needs_no_drive_for_content(self):
        e = self.entries["static"]
        index = {"mount": str(self.tmp), "roots": {KEY: [str(self.tmp)]}, "files": [e]}
        logs = []
        summary = footmap.build_pool(index, KEY, workers=1, stage="decode", cache_dir=self.cache, log=logs.append)
        self.assertEqual((summary["decoded"], summary["failed"]), (0, {}))  # decoded in setUpClass: skipped
        self.assertTrue(any("can be unplugged" in line for line in logs))
        summary = footmap.build_pool(index, KEY, workers=1, stage="content", cache_dir=self.cache, log=logs.append, embed=fake_embed, detect=lambda rgb: ([], 0))
        self.assertEqual((summary["built"], summary["complete"], summary["failed"]), (1, 1, {}))
        m = footmap.load(e["fid"], self.cache)
        self.assertTrue((m["faces"] == 0).all() and np.isnan(m["face_box"]).all() and (m["face_area"] == 0).all())
        again = footmap.build_pool(index, KEY, workers=1, stage="all", cache_dir=self.cache, log=logs.append, embed=fake_embed, detect=fake_detect)
        self.assertEqual((again["already_done"], again["decoded"], again["built"]), (1, 0, 0))

    def test_a_missing_file_fails_alone(self):
        e = {**self.entries["static"], "fid": "missing00000", "path": str(self.tmp / "gone.mp4")}
        res = footmap.decode_take(e, self.cache)
        self.assertFalse(res["ok"])
        self.assertIn("FileNotFoundError", res["error"])


if __name__ == "__main__":
    unittest.main()
