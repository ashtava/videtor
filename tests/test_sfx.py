"""SFX habits mined from an export, SFX placed on a cut, and the SFX track in the written XML (no drive needed)."""

import json
import shutil
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from videtor.export import xmeml_writer as w
from videtor.kb import metrics, xmeml
from videtor.kb import sfx as kb_sfx
from videtor.plan import build
from videtor.plan import sfx as plan_sfx

FIXTURES = Path(__file__).parent / "fixtures" / "sfx"
KEY = "sfx-mini.xml::SFX Mini"
HAS_FFMPEG = bool(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").exists())


def write_wav(path: Path, seconds: float, shape, channels: int = 2, sr: int = 8000) -> Path:
    """A test tone whose loudness over time is `shape(t)` (0..1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(seconds * sr)) / sr
    y = (np.sin(2 * np.pi * 440 * t) * np.clip(shape(t), 0, 1) * 0.8 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(np.repeat(y[:, None], channels, axis=1).tobytes())
    return path


class KindTests(unittest.TestCase):
    def test_kinds_from_names_and_folders(self):
        cases = {
            ("#Riser ALL-IN-ONE.wav", None): "riser",
            ("ShortRiser2.wav", None): "riser",
            ("Build Mix 3.wav", None): "riser",
            ("#Impacts ALL-IN-ONE.wav", None): "impact",
            ("BOOM Library - Transitions - Impact Whoosh Drum.wav", None): "impact",
            ("DSGNImpt_Low Riser, Textured, Transition, Whoosh,_Ocular Sounds_Epic Hits Vol 1.wav", None): "impact",
            ("DSGNWhsh_Quick Whoosh, Cymbal Riser, Crash_Ocular Sounds_Short Whooshes.wav", None): "whoosh",
            ("FlyBy 13.wav", None): "whoosh",
            ("On Your Left.wav", "/x/LOBO SFX/Whooshes/On Your Left.wav"): "whoosh",
            ("Sub Drop Whoosh1.wav", "/x/LOBO SFX/Whooshes/Sub Drop Whoosh1.wav"): "sub_drop",
            ("COMCam_Kissy Short Shutter_Ocular Sounds_Camera Shutters.wav", None): "shutter",
            ("ReelAudio-12930.mp3", "/x/Camera click and Elements  SFX/ReelAudio-12930.mp3"): "shutter",
            ("Camera_Click_9.wav", None): "shutter",
            ("BOOM Library - WATER Refreshing burst.wav", None): "other",
            ("Karl CLick.mp3", "/x/Regular Basis SFX/Karl CLick.mp3"): "other",
            ("AMBForst_Birdsong, Wind High in Trees_Ocular Sounds.wav", None): "other",
        }
        for (name, path), kind in cases.items():
            self.assertEqual(kb_sfx.kind_of(name, path), kind, name)

    def test_what_is_not_sound_design(self):
        self.assertEqual(kb_sfx.excluded_reason("SnapInsta.to_AQx.mp4", "/a/SnapInsta.to_AQx.mp4"), "reference")
        self.assertEqual(kb_sfx.excluded_reason("9x16.mp4", "/a/9x16.mp4"), "video_audio")
        self.assertEqual(kb_sfx.excluded_reason("260925-T017.WAV", "/a/260925-T017.WAV"), "recorder")
        self.assertEqual(kb_sfx.excluded_reason("WhatsApp Audio 2026-10-02 at 17.45.13.mp3", None), "voice")
        self.assertIsNone(kb_sfx.excluded_reason("Whoosh 01.mp3", "/a/Whoosh 01.mp3"))


class MiningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "kb").mkdir()
        (root / "kb" / "manifest.json").write_text(json.dumps({"default": "unsorted", "types": {KEY: "brand-social"}}))
        cls.library = root / "Regular Basis SFX"  # the library moved from ~/Documents/Assetss to here
        # the riser pack: silence, then a tone swelling from 12.0 s to its crest at 13.5 s, then silence
        write_wav(cls.library / "Risers" / "#Riser Pack.wav", 14.5, lambda t: np.where((t >= 12.0) & (t < 13.5), (t - 12.0) / 1.5, 0.0))
        (cls.library / "whoosh").mkdir(parents=True)
        (cls.library / "whoosh" / "Whoosh 01.mp3").write_bytes(b"")
        cls.data = kb_sfx.build(FIXTURES, root / "kb", (cls.library,), root / "beats", log=lambda *a: None)
        cls.uses = {u["file"]: u for u in cls.data["uses"]}
        cls.kb_dir = root / "kb"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_sound_design_counts(self):
        self.assertEqual(sorted(u["kind"] for u in self.data["uses"]), ["impact", "riser", "shutter", "whoosh", "whoosh"])
        # camera audio, the Instagram reference, the disabled clip, the recorder take and the music bed (a stereo
        # pair on two tracks, counted once) are left out
        self.assertEqual(self.data["edits"][KEY]["excluded"], {"camera": 1, "reference": 1, "disabled": 1, "recorder": 1, "music": 1})
        self.assertEqual(self.data["edits"][KEY]["music"]["files"], ["Artist - Song.wav"])
        self.assertEqual(self.data["kinds"]["riser"]["uses"], 1)  # the riser's stereo pair once

    def test_positions_next_to_transitions(self):
        riser = self.uses["#Riser Pack.wav"]
        self.assertEqual((riser["start_s"], riser["end_s"]), (2.0, 4.0))  # start -1: where the fade-in starts
        self.assertEqual((riser["in_s"], riser["out_s"]), (12.0, 14.0))
        self.assertEqual(riser["fade_in_s"], 0.4)
        flyby = self.uses["FlyBy 13.wav"]
        self.assertEqual((flyby["start_s"], flyby["end_s"]), (8.48, 9.48))  # both edges -1, between two fades
        self.assertEqual((flyby["fade_in_s"], flyby["fade_out_s"]), (0.2, 0.2))

    def test_paths_with_a_hash_and_a_moved_library(self):
        riser = self.uses["#Riser Pack.wav"]
        self.assertTrue(riser["path"].endswith("/Risers/#Riser Pack.wav"), riser["path"])
        self.assertFalse(riser["exists"])
        self.assertEqual(riser["found"], str(self.library / "Risers" / "#Riser Pack.wav"))
        self.assertEqual(self.uses["Whoosh 01.mp3"]["found"], str(self.library / "whoosh" / "Whoosh 01.mp3"))
        self.assertIsNone(self.uses["Impact Hit.wav"]["found"])

    def test_levels(self):
        self.assertEqual(self.uses["#Riser Pack.wav"]["level_db"], 4.5)  # Gain
        self.assertEqual(self.uses["Impact Hit.wav"]["level_db"], -6.0)  # Audio Levels 0.501187
        self.assertEqual(self.uses["Whoosh 01.mp3"]["level_db"], -3.0)
        self.assertEqual(self.uses["COMCam_Test Shutter_Ocular Sounds_Camera Shutters.wav"]["level_db"], 0.0)

    def test_offsets_from_the_nearest_cut(self):
        self.assertEqual(self.uses["#Riser Pack.wav"]["cut"]["end"], 0.0)  # ends on the 4 s cut
        self.assertEqual(self.uses["Impact Hit.wav"]["cut"]["start"], 0.0)  # starts on it
        self.assertEqual(self.uses["COMCam_Test Shutter_Ocular Sounds_Camera Shutters.wav"]["cut"]["start"], 0.04)  # a frame after 7 s
        self.assertEqual(self.data["edits"][KEY]["cuts"], 3)

    @unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
    def test_peak_measured_on_disk(self):
        riser = self.uses["#Riser Pack.wav"]
        self.assertAlmostEqual(riser["peak_s"], 1.5, delta=0.06)  # the crest, 1.5 s into the 12-14 s snippet
        self.assertAlmostEqual(riser["cut"]["peak"], -0.5, delta=0.06)
        self.assertAlmostEqual(kb_sfx.audible_s(riser["found"], 12.0, 2.0), 1.5, delta=0.06)

    def test_snippets_rates_and_file(self):
        snip = self.data["kinds"]["riser"]["snippets"][0]
        self.assertEqual((snip["file"], snip["in_s"], snip["out_s"], snip["uses"], snip["edits"]), ("#Riser Pack.wav", 12.0, 14.0, 1, [KEY]))
        self.assertTrue(snip["exists"])
        per = self.data["rates"]["brand-social"]["per_min"]
        self.assertEqual(per["riser"]["median"], 6.0)  # one riser in a 10 s edit
        self.assertEqual(per["whoosh"]["when_used"], 12.0)
        self.assertEqual(per["all"]["median"], 30.0)
        kb_sfx.write(self.data, self.kb_dir)
        self.assertEqual(kb_sfx.load(self.kb_dir)["kinds"]["riser"]["uses"], 1)
        self.assertIsNone(kb_sfx.load(self.kb_dir / "nowhere"))
        self.assertTrue(any("riser" in line for line in kb_sfx.summary(self.data)))


def _footmap(path: Path, n: int = 20, bright=None, cam_dx=0.0, shake=0.01) -> None:
    t = (np.arange(n) + 0.5) / 2
    np.savez(
        path, t=t.astype(np.float32), bright=np.asarray(bright if bright is not None else np.full(n, 100.0), np.float32),
        cam_dx=np.full(n, cam_dx, np.float32), cam_dy=np.zeros(n, np.float32), cam_zoom=np.zeros(n, np.float32),
        cam_rot=np.zeros(n, np.float32), shake=np.full(n, shake, np.float32),
    )


def _picks(fps: float = 25.0) -> dict:
    """10 s: details 0-3 s, reveal 3-5 s, body 5-8 s, end 8-10 s; music from 10 s of its track."""
    layout = [("details-1", "details", 0, 1), ("details-2", "details", 1, 1), ("details-3", "details", 2, 1),
              ("reveal-1", "reveal", 3, 2), ("body-1", "body", 5, 1), ("body-2", "body", 6, 1), ("body-3", "body", 7, 1),
              ("end-1", "end", 8, 2)]
    slots = []
    for k, (sid, section, start, length) in enumerate(layout, start=1):
        pick = {"fid": f"f{k}", "path": f"/Volumes/RAW/T{k}.MP4", "name": f"T{k}.MP4", "in_s": 2.0, "out_s": 2.0 + length,
                "speed": 1.0, "file_fps": fps, "duration_s": 10.0, "width": 1080, "height": 1920, "audio_channels": 0,
                "reasons": ["camera moving smoothly"] if sid == "body-3" else []}
        slots.append({"id": sid, "section": section, "role": "", "show": "", "start_s": float(start), "length_s": float(length), "pick": pick, "alternates": []})
    return {"version": 1, "name": "test-cut", "key": KEY, "fps": fps, "width": 1080, "height": 1920,
            "music": {"path": "/Users/nobody/music/song.wav", "in_s": 10.0}, "slots": slots}


def _music_data() -> dict:
    """Beats every 0.5 s from 10.1 s; the track lifts 10 dB at 13.1 s (3.1 s into the cut, 0.1 s after the reveal cut)."""
    rms = [-30.0] * 26 + [-20.0] * 34
    return {"duration_s": 30.0, "beats_s": [round(10.1 + 0.5 * k, 3) for k in range(-20, 30) if 10.1 + 0.5 * k > 0], "rms_db": rms}


def _kb(sounds: Path, **rates) -> dict:
    snip = lambda f, a, b, pk, lvl, uses=2, edits=(KEY,): {  # noqa: E731
        "file": f.name, "path": str(f), "exists": True, "in_s": a, "out_s": b, "length_s": b - a, "uses": uses, "edits": list(edits),
        "level_db": lvl, "peak_s": pk}
    place = lambda med: {"cut": {"peak": {"count": 6, "median": med, "two_frames": 0.6}}}  # noqa: E731
    per = {"riser": 2.0, "impact": 2.0, "whoosh": 18.0, "sub_drop": 0.0, "shutter": 12.0, "other": 0.0, "all": 60.0} | rates
    return {
        "kinds": {
            "riser": {"snippets": [snip(sounds / "riser.wav", 0.0, 2.0, 1.5, 3.5)], "placement": place(0.04), "cut_chance_two_frames": 0.2},
            "impact": {"snippets": [snip(sounds / "build-hit.wav", 0.0, 3.0, 2.5, -4.0, uses=5),
                                    snip(sounds / "impact.wav", 0.0, 1.0, 0.1, 2.5, uses=1)], "placement": {}, "cut_chance_two_frames": 0.2},
            "whoosh": {"snippets": [snip(sounds / "whoosh.wav", 0.0, 1.0, 0.5, 3.0)], "placement": place(0.06), "cut_chance_two_frames": 0.2},
            "shutter": {"snippets": [snip(sounds / "shutter.wav", 0.0, 0.2, 0.01, -6.5)], "placement": place(0.02), "cut_chance_two_frames": 0.2},
        },
        "rates": {"brand-social": {"edits": [KEY], "per_min": {k: {"when_used": v} for k, v in per.items()}}},
        "edits": {KEY: {"type": "brand-social"}},
    }


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sounds = root / "Risers and things"
        write_wav(self.sounds / "riser.wav", 2.0, lambda t: np.where(t < 1.5, t / 1.5, 0.0))
        write_wav(self.sounds / "build-hit.wav", 3.0, lambda t: np.where(t < 2.5, t / 2.5, 0.2))
        write_wav(self.sounds / "impact.wav", 1.0, lambda t: np.exp(-np.abs(t - 0.1) * 6))
        write_wav(self.sounds / "whoosh.wav", 1.0, lambda t: 1 - np.abs(t - 0.5) * 2)
        write_wav(self.sounds / "shutter.wav", 0.2, lambda t: np.exp(-t * 40), channels=1)
        self.footmap = root / "footmap"
        self.footmap.mkdir()
        for k in range(1, 9):
            _footmap(self.footmap / f"f{k}.npz")
        flash = np.full(20, 100.0)
        flash[5] = 150.0  # a flash 2.75 s into take 2, inside details-2's window (2-3 s)
        _footmap(self.footmap / "f2.npz", bright=flash)
        bursts = np.full(20, 80.0)
        bursts[[1, 9, 14, 18]] = 130.0  # take 6 flashes throughout, though not inside its own window
        _footmap(self.footmap / "f6.npz", bright=bursts)
        _footmap(self.footmap / "f7.npz", cam_dx=0.6)  # body-3 opens on a fast, smooth camera move
        self.picks = _picks()
        self.kb = _kb(self.sounds)

    def tearDown(self):
        self.tmp.cleanup()

    def place(self, kb=None, music=True):
        return plan_sfx.place(self.picks, kb or self.kb, _music_data() if music else None, self.footmap)

    def by_kind(self, clips):
        out = {}
        for c in clips:
            out.setdefault(c["kind"], []).append(c)
        return out

    def test_riser_crests_and_impact_hits_on_the_drop(self):
        k = self.by_kind(self.place())
        riser, impact = k["riser"][0], k["impact"][0]
        self.assertAlmostEqual(riser["anchor_s"], 3.1, places=3)  # the drop, which the reveal cut at 3.0 s leads
        self.assertAlmostEqual(riser["start_s"], 1.6, places=3)
        if HAS_FFMPEG:
            self.assertLessEqual(riser["end_s"], 3.1 + 0.2)  # ends with its crest: the silence after it is trimmed
        self.assertEqual(impact["file"], "impact.wav")  # a hit, not the build that ends in one (though used more)
        self.assertAlmostEqual(impact["anchor_s"], 3.1, places=3)
        self.assertAlmostEqual(impact["start_s"], 3.0, places=3)
        self.assertIn("drop", riser["why"])
        self.assertEqual((riser["gain_db"], impact["gain_db"]), (3.5, 2.5))  # your level for each snippet

    def test_a_slot_named_the_reveal_takes_the_riser_whatever_its_section(self):
        slots = [{"id": "a", "section": "open"}, {"id": "b", "section": "match"}, {"id": "c", "section": "drop", "role": "reveal"}]
        self.assertEqual(plan_sfx.reveal_index(slots), 2)
        self.assertEqual(plan_sfx.reveal_index([{**s, "role": ""} for s in slots]), 1)  # unnamed: the second section's first slot

    def test_without_music_on_the_cut(self):
        k = self.by_kind(self.place(music=False))
        self.assertAlmostEqual(k["riser"][0]["anchor_s"], 3.04, places=3)  # your risers crest 0.04 s after the cut
        self.assertAlmostEqual(k["impact"][0]["anchor_s"], 3.04, places=3)

    def test_whooshes_on_big_transitions(self):
        whooshes = self.by_kind(self.place())["whoosh"]
        self.assertEqual([round(c["target_s"], 2) for c in whooshes], [5.0, 7.0, 8.0])  # two section changes + the camera move
        for c in whooshes:
            self.assertAlmostEqual(c["anchor_s"] - c["target_s"], 0.06, places=3)
            self.assertGreaterEqual(abs(c["target_s"] - 3.0), plan_sfx.REVEAL_CLEAR_S)
        self.assertIn("camera move", whooshes[1]["why"])
        self.assertIn("section change", whooshes[0]["why"])

    def test_shutters_on_photo_moments(self):
        shutters = self.by_kind(self.place())["shutter"]
        self.assertEqual([c["target_s"] for c in shutters], [1.0, 6.0])  # the flash in details-2, the flashing take of body-2
        self.assertAlmostEqual(shutters[0]["anchor_s"], 1.02, places=3)
        self.assertEqual(shutters[0]["channels"], 1)
        self.assertIn("flash", shutters[0]["why"])

    def test_density_caps(self):
        k = self.by_kind(self.place(kb=_kb(self.sounds, whoosh=7.0, shutter=6.0)))  # 10 s: 1.2 whooshes, 1 shutter
        self.assertEqual(len(k["whoosh"]), 1)
        self.assertEqual(len(k["shutter"]), 1)
        k = self.by_kind(self.place(kb=_kb(self.sounds, all=30.0)))  # 5 in all: riser, impact, 3 whooshes leave no room
        self.assertNotIn("shutter", k)
        self.assertNotIn("riser", self.by_kind(self.place(kb=_kb(self.sounds, riser=0.0))))

    def test_same_kind_never_overlaps_and_files_exist(self):
        clips = self.place()
        for kind, cs in self.by_kind(clips).items():
            for a, b in zip(cs, cs[1:]):
                self.assertLessEqual(a["end_s"], b["start_s"] + 1e-6, kind)
        for c in clips:
            self.assertTrue(Path(c["path"]).exists())
            self.assertTrue(c["why"])
            self.assertLessEqual(c["end_s"], 10.0 + 1e-6)
            self.assertAlmostEqual(c["end_s"] - c["start_s"], c["out_s"] - c["in_s"], places=3)

    def test_library_fallback_when_your_files_are_gone(self):
        kb = _kb(self.sounds)
        gone = self.sounds / "Risers" / "Gone Riser.wav"
        kb["kinds"]["riser"]["snippets"] = [dict(kb["kinds"]["riser"]["snippets"][0], file=gone.name, path=str(gone))]
        write_wav(self.sounds / "Risers" / "Other Riser.wav", 1.5, lambda t: t / 1.5)
        write_wav(self.sounds / "Risers" / "Some Whoosh.wav", 1.0, lambda t: t)  # another kind: never chosen as a riser
        riser = self.by_kind(plan_sfx.place(self.picks, kb, _music_data(), self.footmap))["riser"][0]
        self.assertEqual(riser["file"], "Other Riser.wav")
        self.assertEqual(riser["in_s"], 0.0)  # at full length
        self.assertIn("library", riser["snippet"])
        kb["kinds"]["riser"]["snippets"] = []
        self.assertNotIn("riser", self.by_kind(plan_sfx.place(self.picks, kb, _music_data(), self.footmap)))

    def test_no_kb_no_sfx(self):
        self.assertEqual(plan_sfx.place(self.picks, None), [])
        self.assertEqual(plan_sfx.place(self.picks, {}), [])


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.picks = _picks()
        self.music = {"path": "/Users/nobody/music/song.wav", "duration_s": 30.0, "in_s": 10.0, "fade_out_s": 1.0}
        self.sfx = [
            {"kind": "riser", "path": "/SFX/Risers/#Riser Pack.wav", "in_s": 5.12, "out_s": 7.97, "start_s": 0.15, "gain_db": 3.5, "channels": 2, "duration_s": 23.4},
            {"kind": "shutter", "path": "/SFX/Shutter.wav", "in_s": 0.0, "out_s": 0.32, "start_s": 1.0, "gain_db": -7.5, "channels": 1, "duration_s": 0.33},
            {"kind": "whoosh", "path": "/SFX/On Your Left.wav", "in_s": 0.0, "out_s": 2.68, "start_s": 4.0, "gain_db": 0.0, "channels": 2, "duration_s": 2.71},
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, sfx):
        path = self.root / "cut.xml"
        w.write_xmeml(build.build_from_picks(self.picks, self.music, sfx=sfx), path, fps=25, width=1080, height=1920)
        return path

    def test_sfx_tracks_after_the_music_with_audio_levels(self):
        path = self.write(self.sfx)
        seq = xmeml.load(path)[0]
        audio = [t for t in seq.tracks if t.media == "audio"]
        self.assertEqual([c.file_name for c in audio[0].clips], ["song.wav"])  # the music first
        self.assertEqual(len(audio), 3)  # the shutter plays under the riser: a second SFX track for it
        self.assertEqual([c.file_name for c in audio[1].clips], ["#Riser Pack.wav", "On Your Left.wav"])
        self.assertEqual([c.file_name for c in audio[2].clips], ["Shutter.wav"])
        riser = audio[1].clips[0]
        self.assertEqual((riser.start, riser.end, riser.source_in, riser.source_out), (4, 75, 128, 199))
        self.assertEqual(riser.file_path, "/SFX/Risers/#Riser Pack.wav")  # '#' written as %23, so it reads back whole
        gains = {c.file_name: metrics.clip_gain_db(c) for t in audio[1:] for c in t.clips}
        self.assertEqual(gains, {"#Riser Pack.wav": 3.5, "On Your Left.wav": 0.0, "Shutter.wav": -7.5})
        self.assertNotIn("audiolevels", [e.effect_id for e in audio[0].clips[0].effects])  # the music keeps its level

    def test_audio_levels_written_like_premiere(self):
        root = ET.parse(self.write(self.sfx)).getroot()
        clips = {c.findtext("name"): c for c in root.iter("clipitem")}
        effect = clips["Shutter.wav"].find("filter/effect")
        self.assertEqual([ch.tag for ch in effect], ["name", "effectid", "effectcategory", "effecttype", "mediatype", "pproBypass", "parameter"])
        self.assertEqual([effect.findtext(t) for t in ("name", "effectid", "effectcategory", "effecttype", "mediatype", "pproBypass")],
                         ["Audio Levels", "audiolevels", "audiolevels", "audiolevels", "audio", "false"])
        p = effect.find("parameter")
        self.assertEqual(p.get("authoringApp"), "PremierePro")
        self.assertEqual([ch.tag for ch in p], ["parameterid", "name", "valuemin", "valuemax", "value"])
        self.assertEqual([p.findtext(t) for t in ("parameterid", "name", "valuemin", "valuemax")], ["level", "Level", "0", "3.98109"])
        self.assertAlmostEqual(float(p.findtext("value")), 10 ** (-7.5 / 20), places=5)
        self.assertEqual(clips["Shutter.wav"].get("premiereChannelType"), "mono")
        self.assertEqual(clips["#Riser Pack.wav"].get("premiereChannelType"), "stereo")
        # '#' stays raw, as Premiere's own exports write it
        self.assertTrue(clips["#Riser Pack.wav"].findtext("file/pathurl").endswith("/SFX/Risers/#Riser%20Pack.wav"))
        self.assertEqual(float(clips["On Your Left.wav"].find("filter/effect/parameter").findtext("value")), 1.0)

    def test_without_sfx_nothing_changes(self):
        without = self.write(None).read_text()
        self.assertEqual([t.name for t in xmeml.load(self.root / "cut.xml")[0].tracks], ["V1", "V2", "V3", "A1"])
        self.assertEqual(self.write([]).read_text(), without)
        self.assertNotIn("audiolevels", without)


if __name__ == "__main__":
    unittest.main()
