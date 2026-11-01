import subprocess, tempfile, json
from pathlib import Path
from videtor.analyze import speech
p = "/Volumes/Paritosh_2/Diwali Party/music/Panjabi MC Jogi Official Video.mp3"
wav = "/private/tmp/claude-501/-Users-paritoshsrivastava-workspace-videtor/fae8fc32-db3d-4845-add9-a4e19cc2e12a/scratchpad/jogi_0_100.wav"
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", p, "-t", "100", "-ac", "1", "-ar", "16000", wav], check=True)
import inspect; print(inspect.signature(speech.transcribe))
r = speech.transcribe(wav, "song-jogi-0-100", cache_dir=Path("/private/tmp/claude-501/-Users-paritoshsrivastava-workspace-videtor/fae8fc32-db3d-4845-add9-a4e19cc2e12a/scratchpad"))
segs = r.get("segments") if isinstance(r, dict) else r
for s in segs:
    print(f"{s['start']:6.2f}-{s['end']:6.2f} {s['text']}")
