import re
import tempfile
import unittest
from pathlib import Path

import opentimelineio as otio

from videtor.export import xmeml_writer as w
from videtor.kb import metrics, xmeml


def build_timeline() -> otio.schema.Timeline:
    """25 fps vertical sequence: a 50 fps clip at normal speed, a 100 fps clip at half speed, a music bed."""
    fps = 25.0
    tl = otio.schema.Timeline(name="Benchmark cut")
    v1 = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    a_music = otio.schema.Track(name="music", kind=otio.schema.TrackKind.Audio)
    tl.tracks.append(v1)
    tl.tracks.append(a_music)

    c1 = otio.schema.Clip(
        name="C0011.MP4",
        media_reference=w.media_reference("/Volumes/RAW/Day 1/C0011.MP4", 50.0, 20.0, 3840, 2160, 2),
        source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(100, 50.0), otio.opentime.RationalTime(100, 50.0)),  # 2 s of file
    )
    c2 = otio.schema.Clip(
        name="C0042.MP4",
        media_reference=w.media_reference("/Volumes/RAW/Day 1/C0042.MP4", 100.0, 8.0, 3840, 2160, 2),
        source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(200, 100.0), otio.opentime.RationalTime(400, 100.0)),  # 4 s slot
    )
    c2.effects.append(otio.schema.LinearTimeWarp(time_scalar=0.5))  # at half speed the 4 s slot plays 2 s (200 frames) of the file
    v1.append(c1)
    v1.append(c2)

    music = otio.schema.Clip(
        name="track.wav",
        media_reference=w.media_reference("/Volumes/RAW/music/track.wav", fps, 120.0, None, None, 2),
        source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, fps), otio.opentime.RationalTime(150, fps)),  # 6 s
    )
    a_music.append(music)
    return tl


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "cut.xml")
        w.write_xmeml(build_timeline(), self.path, fps=25.0, width=1080, height=1920)
        self.seq = xmeml.load(self.path)[0]
        self.tracks = {t.name: t for t in self.seq.tracks}

    def tearDown(self):
        self.tmp.cleanup()

    def test_sequence_header_and_track_layout(self):
        self.assertTrue(self.path.read_text().startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'))
        self.assertEqual(self.seq.name, "Benchmark cut")
        self.assertEqual(self.seq.fps, 25.0)
        self.assertEqual((self.seq.width, self.seq.height), (1080, 1920))
        self.assertEqual(list(self.tracks), ["V1", "A1", "A2"])  # camera audio mirrors V1 on A1, music on A2

    def test_video_clips_positions_and_sequence_rate_in_out(self):
        c1, c2 = self.tracks["V1"].clips
        # c1: 2 s of a 50 fps file, starting 2 s in = 50 timeline frames; in/out counted in 25 fps sequence frames (Premiere's convention)
        self.assertEqual((c1.start, c1.end, c1.source_in, c1.source_out), (0, 50, 50, 100))
        self.assertEqual((c1.clip_fps, c1.file_fps, c1.file_path), (25.0, 50.0, "/Volumes/RAW/Day 1/C0011.MP4"))
        # c2: a 100 fps file at half speed over 4 s = 100 timeline frames covering 2 s of media from 2 s in; under
        # Time Remap Premiere counts in/out in speed-stretched frames: in = 2 s / 0.5 × 25 = 100, out = in + the slot
        self.assertEqual((c2.start, c2.end, c2.source_in, c2.source_out), (50, 150, 100, 200))
        self.assertAlmostEqual(c2.source_seconds(c2.source_in, 25.0), 2.0)
        self.assertAlmostEqual(c2.source_seconds(c2.source_out, 25.0), 4.0)
        self.assertEqual(metrics.clip_speed(c2), (0.5, False))
        self.assertEqual(c2.file_fps, 100.0)  # the <file> block keeps the native rate
        self.assertEqual(self.seq.duration, 150)

    def test_zoom_keyframes_sit_on_the_clips_in_and_out_frames(self):
        tl = build_timeline()
        clip = next(c for c in tl.tracks[0] if isinstance(c, otio.schema.Clip))
        clip.metadata["videtor"] = {"motion": {"scale": 100.0, "zoom": {"from": 100.0, "to": 108.0}}}
        path = Path(self.tmp.name, "zoom.xml")
        w.write_xmeml(tl, path, fps=25.0, width=1920, height=1080)
        text = path.read_text()
        keys = re.findall(r"<keyframe>\s*<when>(\d+)</when>\s*<value>([^<]*)</value>", text)
        seq = xmeml.load(path)[0]
        c1 = [t for t in seq.tracks if t.name == "V1"][0].clips[0]
        self.assertEqual(keys, [(str(c1.source_in), "100.0"), (str(c1.source_out), "108.0")])

    def test_camera_audio_is_linked_and_music_is_its_own_track(self):
        v1, a1, a2 = self.tracks["V1"], self.tracks["A1"], self.tracks["A2"]
        self.assertEqual([c.start for c in a1.clips], [0, 50])
        self.assertEqual(a1.clips[0].file_path, v1.clips[0].file_path)  # same <file>, referenced by id
        self.assertIn(a1.clips[0].id, v1.clips[0].linked)
        self.assertIn(v1.clips[0].id, a1.clips[0].linked)
        self.assertEqual((a2.clips[0].start, a2.clips[0].end, a2.clips[0].name), (0, 150, "track.wav"))

    def test_file_written_once_then_referenced(self):
        text = self.path.read_text()
        self.assertEqual(text.count("<pathurl>file://localhost/Volumes/RAW/Day%201/C0011.MP4</pathurl>"), 1)
        self.assertEqual(text.count('<file id="file-1"'), 2)  # full element on V1, bare reference on A1

    def test_otio_adapter_reads_it_back(self):
        read = otio.adapters.read_from_file(str(self.path), "fcp_xml")
        tl = read if isinstance(read, otio.schema.Timeline) else read[0]
        self.assertEqual(tl.name, "Benchmark cut")
        self.assertEqual(len(tl.video_tracks()), 1)
        self.assertEqual(len(tl.audio_tracks()), 2)
        self.assertEqual([c.name for c in tl.video_tracks()[0].find_clips()], ["C0011.MP4", "C0042.MP4"])


if __name__ == "__main__":
    unittest.main()
