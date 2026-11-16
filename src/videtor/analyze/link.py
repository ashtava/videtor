"""A video fetched from a link (YouTube, Instagram, and the other sites yt-dlp reads) onto this Mac.

The promos (pipeline.promos) start from a file; this gets the file. It asks for the H.264 version up to 1080 on
the short side, which Premiere reads, and re-encodes on the Mac when a site has no such version. With the video
comes what the site knows about it: the title, the chapters and, on YouTube, the most-replayed curve.

yt-dlp is not part of the base install: `uv sync --extra links` (it brings yt-dlp, its YouTube scripts, Deno to
run them, and curl-cffi for Instagram without a login). Sites change often; when a link that used to work stops,
update it (`videtor promos --update`). `browser` lets yt-dlp use that browser's login for the site (a private
account, or Instagram when it asks to log in): only when the user says so.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

MAX_S = 4 * 3600.0
FORMAT = "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*[vcodec^=avc1]+ba/b[vcodec^=avc1]/bv*+ba/b"
SORT = ["res:1080", "fps:60", "vcodec:h264", "acodec:aac"]  # res is the short side: a vertical 1080x1920 counts as 1080
KEEP = ("id", "title", "uploader", "channel", "duration", "webpage_url", "extractor_key", "upload_date", "heatmap", "chapters", "width", "height")
BROWSERS = ("chrome", "safari", "firefox", "brave", "edge")
SETUP = 'uv sync --extra analyze --extra ui --extra links'


class LinkError(ValueError):
    """The link could not be fetched; the message says why in plain words."""


def is_link(text: str) -> bool:
    return str(text).strip().lower().startswith(("http://", "https://"))


def available() -> str | None:
    """yt-dlp's version when it is installed, else None."""
    try:
        import yt_dlp.version

        return yt_dlp.version.__version__
    except ImportError:
        return None


def missing() -> str:
    return ("Fetching a video from a link needs yt-dlp, which is not installed on this Mac yet. "
            f"Install it once in the Videtor folder ({SETUP}), or choose the video file instead.")


def _plain(message: str, url: str) -> str:
    """yt-dlp's error in the user's words."""
    low = message.lower()
    site = "Instagram" if "instagram" in url.lower() else "YouTube" if "youtu" in url.lower() else "the site"
    if any(k in low for k in ("login", "log in", "sign in", "cookies", "registered users", "rate-limit", "empty media response")):
        return (f"{site} wants a login for this video (or has seen too many requests from here for now). Save the video from {site} and choose "
                "the file instead, or let Videtor use your browser's login (Advanced).")
    if any(k in low for k in ("private", "unavailable", "removed", "not available", "does not exist", "404")):
        return f"{site} says this video is private, removed or not available here."
    if "unsupported url" in low:
        return "This is not a link to a video Videtor can fetch. Give the link of the video's own page."
    if any(k in low for k in ("timed out", "name resolution", "connection", "network", "temporary failure")):
        return "The video could not be reached: check the internet connection and try again."
    if "javascript runtime" in low or "signature solving" in low:
        return f"YouTube's player could not be read: the fetcher's helpers are missing or out of date ({SETUP}, then `videtor promos --update`)."
    return f"The video could not be fetched ({message.strip()[-220:]}). Sites change often: updating the fetcher (`videtor promos --update`) usually fixes it."


def _h264(path: Path, log: Callable[[str], None]) -> Path:
    """The file as an H.264/AAC mp4 (what Premiere reads): as it is when it already is one, else re-encoded on the Mac."""
    run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)], capture_output=True, text=True)
    streams = json.loads(run.stdout or "{}").get("streams", [])
    v = next((s["codec_name"] for s in streams if s["codec_type"] == "video"), None)
    a = next((s["codec_name"] for s in streams if s["codec_type"] == "audio"), None)
    out = path.with_name("source.mp4")
    if v == "h264" and a in (None, "aac") and path.suffix.lower() == ".mp4":
        if path != out:
            path.replace(out)
        return out
    log(f"  the site gave {v}/{a}: making an H.264 copy Premiere can read")
    tmp = path.with_name("source.h264.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path), "-c:v", "h264_videotoolbox", "-b:v", "12M", "-pix_fmt", "yuv420p"] if v != "h264" else \
          ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path), "-c:v", "copy"]
    done = subprocess.run([*cmd, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)], capture_output=True, text=True, errors="replace")
    if done.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise LinkError(f"The fetched video could not be made into a file Premiere reads ({done.stderr.strip()[-200:]}).")
    path.unlink(missing_ok=True)
    tmp.replace(out)
    return out


def fetch(url: str, out_dir: Path, progress: Callable[[int, int], None] | None = None, log: Callable[[str], None] = print, browser: str | None = None) -> dict:
    """Fetch the video at `url` into `out_dir` as source.mp4, with source.json beside it. Returns {path, info}."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise LinkError(missing()) from exc
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    class Quiet:
        def debug(self, msg):
            pass

        info = debug

        def warning(self, msg):
            if "javascript runtime" in msg.lower() or "signature solving" in msg.lower():
                log(f"  {msg}")

        def error(self, msg):
            pass

    def hook(d: dict) -> None:
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress(int((d.get("downloaded_bytes") or 0) / 1e5), int(total / 1e5))

    def allowed(info: dict, *, incomplete: bool = False) -> str | None:
        if info.get("is_live"):
            return "a live stream"
        if (info.get("duration") or 0) > MAX_S:
            return "too long"
        return None

    opts = {"format": FORMAT, "format_sort": SORT, "merge_output_format": "mp4", "outtmpl": {"default": str(out_dir / "fetched.%(ext)s")}, "noplaylist": True,
            "playlist_items": "1", "quiet": True, "no_warnings": False, "noprogress": True, "logger": Quiet(), "progress_hooks": [hook], "match_filter": allowed,
            "overwrites": True, "retries": 3, "socket_timeout": 30}
    if browser:
        if browser not in BROWSERS:
            raise LinkError(f"browser must be one of {', '.join(BROWSERS)}")
        opts["cookiesfrombrowser"] = (browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.sanitize_info(ydl.extract_info(url, download=True))
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many kinds; all mean "not fetched"
        raise LinkError(_plain(str(exc), url)) from exc
    if isinstance(info, dict) and info.get("entries"):  # a post with several videos: the first
        info = next((e for e in info["entries"] if e), None)
    if not isinstance(info, dict):
        raise LinkError("Nothing to fetch at this link.")
    got = [d.get("filepath") for d in info.get("requested_downloads") or [] if d.get("filepath")]
    if not got or not Path(got[0]).exists():
        why = allowed(info)
        raise LinkError({"a live stream": "This is a live stream: give the link once it has ended.",
                         "too long": f"This video is longer than {MAX_S / 3600:.0f} hours."}.get(why, "The video's file did not arrive: try again."))
    path = _h264(Path(got[0]), log)
    kept = {k: info.get(k) for k in KEEP if info.get(k) is not None}
    kept.update(url=url, fetched_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    (out_dir / "source.json").write_text(json.dumps(kept, indent=1, ensure_ascii=False) + "\n")
    return {"path": path, "info": kept}


def update(log: Callable[[str], None] = print) -> bool:
    """Bring yt-dlp and its helpers up to date in this environment (sites change; an old fetcher stops working)."""
    import shutil
    import sys

    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    run = subprocess.run([uv, "pip", "install", "--python", sys.executable, "-U", "yt-dlp[default,deno,curl-cffi]"], capture_output=True, text=True, errors="replace")
    for line in (run.stdout + run.stderr).strip().splitlines()[-6:]:
        log(f"  {line}")
    return run.returncode == 0
