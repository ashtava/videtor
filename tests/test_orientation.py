import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from videtor.analyze import footmap
from videtor.analyze import orientation as o

HAVE_MEDIAPIPE = importlib.util.find_spec("mediapipe") is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
# Guru stills with one large upright face (BlazeFace 0.8-0.87, nothing in the other turns)
FACE_STILLS = ["db5c543956ae_2.jpg", "5020ce062e4b_2.jpg", "a767cb08dcd6_0.jpg", "b6c048fb4b27_0.jpg"]
UPRIGHT_KP = [(0.4, 0.4), (0.6, 0.4), (0.5, 0.5), (0.5, 0.62), (0.3, 0.45), (0.7, 0.45)]  # in box units


def stored_as(upright: np.ndarray, rotation: int) -> np.ndarray:
    """The frame a camera stores when the editor must turn it by `rotation` to see `upright`."""
    return np.ascontiguousarray(np.rot90(upright, -o.TURNS[rotation]))


def votes(**by_rotation) -> dict:
    """frame_votes-shaped dict from votes per turn: votes(m90=2, r0=1)."""
    names = {"r0": 0, "m90": -90, "p90": 90, "r180": 180}
    out = {r: {"frames": 0, "area": 0.0, "score": 0.0, "votes": 0, "repeats": 0, "best": 0.0} for r in o.ROTATIONS}
    for name, n in by_rotation.items():
        out[names[name]].update(votes=n, frames=n, best=0.9 if n else 0.0)
    return out


def fake_face(box, score=0.9, keypoints=True) -> dict:
    x0, y0, x1, y1 = box
    face = {"box": list(box), "score": score}
    if keypoints:
        face["keypoints"] = [(x0 + u * (x1 - x0), y0 + v * (y1 - y0)) for u, v in UPRIGHT_KP]
    return face


class MarkerDetector:
    """A face wherever a frame's top-left block is red: stills are drawn so one turn brings it there.

    frame_votes calls it once per turn of each still; the box moves from one still to the next, so votes
    from different stills never look like the same box seen again (unless `same_box`)."""

    def __init__(self, score: float = 0.9, same_box: bool = False):
        self.score, self.same_box, self.calls = score, same_box, 0

    def __call__(self, rgb: np.ndarray) -> list[dict]:
        still = self.calls // len(o.ROTATIONS)
        self.calls += 1
        h, w = rgb.shape[:2]
        block = rgb[: max(1, h // 4), : max(1, w // 4)].reshape(-1, 3).mean(axis=0)
        if not (block[0] > 180 and block[1] < 90 and block[2] < 90):
            return []
        k = 0 if self.same_box else still % 4
        return [fake_face([0.05 + 0.22 * k, 0.4, 0.25 + 0.22 * k, 0.6], self.score)]


def marker_still(rotation: int | None, size=(48, 64)) -> np.ndarray:
    """A grey still as stored (always `size`, like one camera's frames) whose red marker reaches the
    top-left corner once turned by `rotation`; None: no marker, nobody in it."""
    stored = np.full((*size, 3), 128, np.uint8)
    if rotation is None:
        return stored
    up = o.turn(stored, rotation)
    up[: up.shape[0] // 4, : up.shape[1] // 4] = (230, 20, 20)
    return stored_as(up, rotation)


class TurnTests(unittest.TestCase):
    def test_premiere_minus_90_is_numpy_counter_clockwise(self):
        frame = np.arange(6).reshape(2, 3)
        np.testing.assert_array_equal(o.turn(frame, -90), np.rot90(frame, 1))
        np.testing.assert_array_equal(o.turn(frame, 90), np.rot90(frame, -1))
        np.testing.assert_array_equal(o.turn(frame, 180), frame[::-1, ::-1])
        for r in o.ROTATIONS:
            np.testing.assert_array_equal(o.turn(stored_as(frame, r), r), frame)
        # footmap's decoder turns the same way: np.rot90 turns for a Premiere rotation
        for r in o.ROTATIONS:
            self.assertEqual(footmap.frame_geometry({"rotation": 0.0, "matrix": None, "width": 640, "height": 360, "sar": 1.0}, r)["rot90"] % 4,
                             o.TURNS[r] % 4)


class DecideTests(unittest.TestCase):
    def test_no_votes_and_ties_leave_it_to_the_camera(self):
        self.assertEqual(o.decide(votes()), (0, o.UNDECIDED))
        self.assertEqual(o.decide(votes(r0=2, m90=1)), (0, o.UNDECIDED))
        self.assertEqual(o.decide(votes(m90=2, p90=2)), (0, o.UNDECIDED))
        self.assertEqual(o.decide(votes(m90=3, r180=2)), (0, o.UNDECIDED))

    def test_one_turn_wins_alone_or_by_two(self):
        self.assertEqual(o.decide(votes(m90=2))[0], -90)
        self.assertEqual(o.decide(votes(r180=1))[0], 180)
        self.assertEqual(o.decide(votes(r0=1, m90=3))[0], -90)
        self.assertEqual(o.decide(votes(p90=4, r0=2))[0], 90)
        rotation, reason = o.decide(votes(r0=3))
        self.assertEqual(rotation, 0)
        self.assertIn("upright", reason)
        self.assertEqual(o.decide(votes(r0=3, m90=1))[0], 0)

    def test_min_frames(self):
        self.assertEqual(o.decide(votes(m90=1), min_frames=2), (0, o.UNDECIDED))
        self.assertEqual(o.decide(votes(m90=2), min_frames=2)[0], -90)
        self.assertEqual(o.decide(votes(r0=1), min_frames=2), (0, o.UNDECIDED))


class FaceCheckTests(unittest.TestCase):
    def test_keypoints_must_say_upright(self):
        face = fake_face([0.4, 0.4, 0.6, 0.6])
        self.assertTrue(o.plausible(face, 640, 360))
        roll, eyes = o.face_geometry(face, 100, 100)
        self.assertAlmostEqual(roll, 0.0, places=6)
        self.assertAlmostEqual(eyes, 0.2, places=6)
        sideways = dict(face, keypoints=[(y, x) for x, y in face["keypoints"]])  # mirrored about the diagonal: a face lying down
        self.assertGreater(abs(o.face_geometry(sideways, 100, 100)[0]), 80)
        self.assertFalse(o.plausible(sideways, 100, 100))
        squeezed = dict(face, keypoints=[(0.5, 0.48), (0.505, 0.48)] + face["keypoints"][2:])  # eyes on top of each other
        self.assertFalse(o.plausible(squeezed, 100, 100))

    def test_tiny_boxes_do_not_count_but_missing_keypoints_do(self):
        self.assertFalse(o.plausible(fake_face([0.5, 0.5, 0.53, 0.55]), 854, 480))
        self.assertTrue(o.plausible(fake_face([0.5, 0.5, 0.6, 0.7], keypoints=False), 854, 480))

    def test_detector_results_in_any_shape(self):
        self.assertEqual(o._as_faces(([[0, 0, 0.5, 0.5]], 2)), [{"box": [0, 0, 0.5, 0.5], "score": 1.0}])  # faces.detect
        self.assertEqual(o._as_faces([([0, 0, 0.5, 0.5], 0.7)]), [{"box": [0, 0, 0.5, 0.5], "score": 0.7}])
        self.assertEqual(o._as_faces([]), [])


class FrameVoteTests(unittest.TestCase):
    def test_each_still_votes_for_the_turn_that_finds_its_face(self):
        for r in o.ROTATIONS:
            v = o.frame_votes([marker_still(r), marker_still(r)], detect=MarkerDetector(), verify=False)
            self.assertEqual(v[r]["votes"], 2, r)
            self.assertEqual(sum(v[q]["votes"] for q in o.ROTATIONS), 2)
            self.assertEqual(o.decide(v)[0], r)
        v = o.frame_votes([marker_still(None)], detect=MarkerDetector(), verify=False)
        self.assertEqual(o.decide(v), (0, o.UNDECIDED))

    def test_the_same_box_again_is_one_vote(self):
        v = o.frame_votes([marker_still(-90)] * 3, detect=MarkerDetector(same_box=True), verify=False)
        self.assertEqual((v[-90]["votes"], v[-90]["repeats"]), (1, 2))

    def test_weak_faces_do_not_vote(self):
        v = o.frame_votes([marker_still(-90)] * 2, detect=MarkerDetector(score=0.55), verify=False)
        self.assertEqual(v[-90]["frames"], 2)
        self.assertEqual(v[-90]["votes"], 0)

    def test_verification_rejects_non_upright_faces_only(self):
        reject = lambda crops: [0.0] * len(crops)  # noqa: E731
        v = o.frame_votes([marker_still(-90), marker_still(0)], detect=MarkerDetector(), verify=reject)
        self.assertEqual((v[-90]["votes"], v[0]["votes"]), (0, 1))
        boom = lambda crops: 1 / 0  # noqa: E731 - a verifier that fails counts the face unverified
        self.assertEqual(o.frame_votes([marker_still(90)], detect=MarkerDetector(), verify=boom)[90]["votes"], 1)

    def test_unreadable_images_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp, "x.jpg")
            bad.write_bytes(b"not a jpeg")
            v = o.frame_votes([bad, Path(tmp, "missing.jpg"), np.zeros((1, 1, 3), np.uint8)], detect=MarkerDetector(), verify=False)
        self.assertEqual(sum(x["frames"] for x in v.values()), 0)

    @unittest.skipUnless(HAVE_MEDIAPIPE, "needs mediapipe")
    def test_a_real_face_is_found_only_once_turned_upright(self):
        path = next((Path("cache/frames") / s for s in FACE_STILLS if (Path("cache/frames") / s).exists()), None)
        if path is None:
            self.skipTest("no cached face still in cache/frames (run videtor kb frames)")
        upright = o._as_rgb(path)
        for r in o.ROTATIONS:
            v = o.frame_votes([stored_as(upright, r)], verify=False)
            self.assertEqual(o.decide(v)[0], r, f"{path.name} stored for a {r} turn: {v}")
            self.assertEqual(v[r]["votes"], 1)


def entry(tmp: Path, name: str, folder: str = "/Volumes/X/cam", codec: str = "h264", size=(64, 48)) -> dict:
    return {
        "fid": f"{folder.strip('/').replace('/', '_')}_{name}".replace(".", "_"), "basename": name, "dir": folder,
        "path": str(tmp / "absent" / name), "kind": "video",
        "media": {"duration_s": 5.0, "video": {"codec": codec, "width": size[0], "height": size[1]}},
    }


class OrientTakesTests(unittest.TestCase):
    """The camera vote, on synthetic stills and the marker detector."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.frames = self.tmp / "frames"
        self.frames.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def take(self, name: str, turns: list, folder: str = "/Volumes/X/cam", codec: str = "mpeg2video") -> dict:
        e = entry(self.tmp, name, folder, codec)
        for i, r in enumerate(turns):
            Image.fromarray(marker_still(r)).save(self.frames / f"{e['fid']}_{i}.jpg", quality=95)
        return e

    def run_pool(self, entries):
        return o.orient_takes(entries, self.frames, detect=MarkerDetector(), verify=False, probe=False,
                              footmap_dir=self.tmp / "no-footmap", sample=False)

    def test_takes_without_faces_follow_their_camera(self):
        cam = [self.take(f"M2U{n:05d}.MPG", [-90, -90, None]) for n in range(3)]
        weak = self.take("M2U00010.MPG", [90, None, None])  # one face the other way: the camera outvotes it
        blank = self.take("M2U00011.MPG", [None, None, None])
        other = [self.take(f"C{n:04d}.MP4", [0, 0, 0], codec="h264") for n in range(2)]
        lone = self.take("C0009.MP4", [None, None, None], codec="h264")  # its camera has only 2 decided takes
        summary = self.run_pool(cam + [weak, blank] + other + [lone])
        got = {e["basename"]: (e["orientation"]["rotation"], e["orientation"]["source"]) for e in cam + [weak, blank] + other + [lone]}
        self.assertEqual(got, {
            "M2U00000.MPG": (-90, "faces"), "M2U00001.MPG": (-90, "faces"), "M2U00002.MPG": (-90, "faces"),
            "M2U00010.MPG": (-90, "camera"), "M2U00011.MPG": (-90, "camera"),
            "C0000.MP4": (0, "faces"), "C0001.MP4": (0, "faces"), "C0009.MP4": (0, "default"),
        })
        self.assertEqual(summary["counts"], {"rotation": {"0": 3, "-90": 5}, "source": {"faces": 5, "camera": 2, "default": 1}})
        self.assertEqual(blank["orientation"]["votes"], {"0": 0, "-90": 0, "90": 0, "180": 0})
        self.assertEqual(cam[0]["orientation"]["votes"]["-90"], 2)
        m2u = next(c for c in summary["cameras"] if "M2U*" in c["camera"])
        self.assertEqual((m2u["takes"], m2u["decided"], m2u["rotation"]), (5, {"-90": 3}, -90))
        self.assertEqual(sorted(t["name"] for t in summary["turned"]), [f"M2U{n:05d}.MPG" for n in (0, 1, 2, 10, 11)])

    def test_a_split_camera_keeps_its_takes_upright(self):
        takes = [self.take(f"GX01{n:04d}.MP4", [r, r]) for n, r in enumerate((-90, -90, 0, 0, 180))]
        blank = self.take("GX010099.MP4", [None])
        self.run_pool(takes + [blank])
        self.assertEqual([e["orientation"]["rotation"] for e in takes], [-90, -90, 0, 0, 180])  # their own faces
        self.assertEqual((blank["orientation"]["rotation"], blank["orientation"]["source"]), (0, "default"))  # 40% is no majority

    def test_a_single_strong_face_decides_alone_but_a_weak_one_does_not(self):
        sure = self.take("DJI_0001.MP4", [180, None, None], folder="/Volumes/X/drone")
        self.run_pool([sure])
        self.assertEqual((sure["orientation"]["rotation"], sure["orientation"]["source"]), (180, "faces"))
        unsure = self.take("DJI_0002.MP4", [180, None, None], folder="/Volumes/X/drone2")
        o.orient_takes([unsure], self.frames, detect=MarkerDetector(score=0.7), verify=False, probe=False,
                       footmap_dir=self.tmp / "no-footmap", sample=False)
        self.assertEqual((unsure["orientation"]["rotation"], unsure["orientation"]["source"]), (0, "default"))

    def test_takes_without_stills_and_non_video_entries(self):
        e = entry(self.tmp, "M2U00099.MPG")
        audio = {"fid": "a", "basename": "x.wav", "kind": "audio", "media": {"duration_s": 3.0}}
        summary = self.run_pool([e, audio])
        self.assertEqual(summary["takes"], 1)
        self.assertEqual((e["orientation"]["rotation"], e["orientation"]["stills"]), (0, 0))
        self.assertTrue(e["orientation"]["reason"].startswith("no stills"))
        self.assertNotIn("orientation", audio)


class CameraKeyTests(unittest.TestCase):
    def test_prefix_is_the_name_before_the_take_number(self):
        for name, prefix in [("M2U00401.MPG", "M2U"), ("C3880.MP4", "C"), ("DJI_0001.MP4", "DJI_"), ("GX010123.MP4", "GX"),
                             ("akh1020260124_1467.MP4", "AKH"), ("CAM A0469.MP4", "CAM A"), ("B_0073_Proxy.mp4", "B_"),
                             ("IMG_7253.MOV", "IMG_"), ("00001.MTS", "")]:
            self.assertEqual(o.name_prefix(name), prefix, name)

    def test_key_holds_folder_prefix_codec_size_and_optional_sensor_tags(self):
        e = {"basename": "C3923.MP4", "dir": "/V/Guru /data", "media": {"video": {"codec": "h264", "width": 3840, "height": 2160}}}
        self.assertEqual(o.camera_key(e), "/V/Guru /data/C* h264 3840x2160")
        self.assertEqual(o.camera_key(e, 0, "landscape"), "/V/Guru /data/C* h264 3840x2160 landscape dm0")
        self.assertNotEqual(o.camera_key(e, 90, "portrait"), o.camera_key(e, -90, "portrait"))
        self.assertEqual(o.camera_key({**e, "basename": "C3999.MP4"}), o.camera_key(e))
        self.assertNotEqual(o.camera_key({**e, "dir": "/V/other"}), o.camera_key(e))

    def test_display_rotation_from_the_index_then_the_footage_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = {"fid": "abc", "path": str(Path(tmp, "absent.mp4")), "media": {"video": {}}}
            self.assertIsNone(o.display_rotation(e, Path(tmp)))
            Path(tmp, "abc.json").write_text(json.dumps({"rotation": -90}))
            self.assertEqual(o.display_rotation(e, Path(tmp)), -90)
            self.assertEqual(o.display_rotation({**e, "media": {"video": {"rotation": 90.0}}}, Path(tmp)), 90)


class EditorRotationTests(unittest.TestCase):
    def test_detected_rotation_wins_and_the_m2u_rule_stays_the_fallback(self):
        self.assertEqual(footmap.editor_rotation({"basename": "C3920.MP4", "orientation": {"rotation": 180}}), 180)
        self.assertEqual(footmap.editor_rotation({"basename": "M2U00401.MPG", "orientation": {"rotation": 0}}), 0)
        self.assertEqual(footmap.editor_rotation({"basename": "M2U00401.MPG"}), -90)
        self.assertEqual(footmap.editor_rotation({"basename": "C3953.MP4", "orientation": {}}), 0)


class WriteIndexTests(unittest.TestCase):
    def test_atomic_write_leaves_only_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "x.json")
            path.write_text("{}")
            o.write_index({"files": [{"fid": "a", "orientation": {"rotation": -90}}]}, path)
            self.assertEqual(json.loads(path.read_text())["files"][0]["orientation"]["rotation"], -90)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["x.json"])


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class SampleFramesTests(unittest.TestCase):
    def test_frames_come_out_upright_as_ffmpeg_reads_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, rot = Path(tmp, "a.mp4"), Path(tmp, "r.mp4")
            run = lambda *a: subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True, capture_output=True)  # noqa: E731
            run("-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=1", "-pix_fmt", "yuv420p", str(src))
            run("-display_rotation", "90", "-i", str(src), "-c", "copy", str(rot))
            frames = o.sample_frames(src, n=3, width=80)
            self.assertEqual([f.shape for f in frames], [(46, 80, 3)] * 3)  # 80 x 45 rounded to an even height
            turned = o.sample_frames(rot, n=2, width=80, duration=1.0)
            self.assertEqual([f.shape[:2] for f in turned], [(142, 80)] * 2)  # portrait once the display matrix is applied
            self.assertEqual(o.sample_frames(Path(tmp, "missing.mp4")), [])


if __name__ == "__main__":
    unittest.main()
