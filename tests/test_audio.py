import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from videtor.analyze import audio

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_SAY = shutil.which("say") is not None  # macOS speech synthesis, for a real voice sample


def make(path: Path, lavfi: str, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", lavfi, "-t", str(seconds), "-ar", "48000", "-ac", "2", str(path)],
        check=True,
    )


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class AudioTests(unittest.TestCase):
    def test_tone_then_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp, "tone_silence.wav")
            # 2 s of a 440 Hz tone at 0.3 full scale (about -10 dBFS) followed by 2 s of digital silence
            make(wav, "aevalsrc=0.3*sin(440*2*PI*t):s=48000:d=2,apad=pad_dur=2", 4)
            f = audio.audio_features(str(wav))
            self.assertTrue(f["audio"])
            self.assertAlmostEqual(f["silence_share"], 0.5, delta=0.1)
            # a 0.3 full-scale sine lands around -14 LUFS / -13 dBTP after ffmpeg's -3 dB mono-to-stereo upmix
            self.assertTrue(-20 < f["loudness"]["integrated_lufs"] < -8, f["loudness"])
            self.assertTrue(-16 < f["loudness"]["true_peak_dbtp"] < -9, f["loudness"])
            self.assertFalse(f["speech"])  # a sine is not a voice

    def test_metadata_that_is_not_utf8_does_not_break_the_run(self):
        import struct

        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp, "copyright.wav")
            sr, seconds = 16000, 1
            pcm = b"\x00\x00" * sr * seconds
            info_text = b"\xa9 2026 Someone\x00"  # Latin-1 copyright sign: not valid UTF-8
            info = b"INFO" + b"ICOP" + struct.pack("<I", len(info_text)) + info_text + (b"\x00" if len(info_text) % 2 else b"")
            fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            data = b"data" + struct.pack("<I", len(pcm)) + pcm
            body = b"WAVE" + fmt + b"LIST" + struct.pack("<I", len(info)) + info + data
            wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            f = audio.audio_features(str(wav))  # ffmpeg prints the ICOP tag to stderr
            self.assertTrue(f["audio"])
            self.assertEqual(f["silence_share"], 1.0)

    def test_video_without_audio_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4 = Path(tmp, "mute.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=10", "-t", "1", str(mp4)], check=True)
            self.assertEqual(audio.audio_features(str(mp4)), {"audio": False})

    @unittest.skipUnless(HAVE_SAY, "needs macOS `say` for a voice sample")
    def test_synthesized_speech_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            aiff = Path(tmp, "voice.aiff")
            subprocess.run(["say", "-o", str(aiff), "The quick brown fox jumps over the lazy dog, again and again."], check=True)
            f = audio.audio_features(str(aiff))
            self.assertTrue(f["speech"])
            self.assertGreater(f["speech_share"], 0.5)


if __name__ == "__main__":
    unittest.main()
