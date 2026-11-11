"""videtor <command>: see README.md for the workflow."""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from videtor import events, pipeline
from videtor.kb import footage, metrics, profile, selects, xmeml

app = typer.Typer(
    help="Timelines for Premiere Pro and DaVinci Resolve from raw footage, cut in your own style.",
    no_args_is_help=True,
)
kb_app = typer.Typer(help="Knowledge base built from your past edits.", no_args_is_help=True)
app.add_typer(kb_app, name="kb")
library_app = typer.Typer(
    help="Your footage sorted by shot type into folders (wide, close-up, talking head, interview bite, B-roll), offline, on every core; it learns from how you move things.",
    no_args_is_help=True,
)
app.add_typer(library_app, name="library")
console = Console()

KB_DIR_DEFAULT = Path("kb")
KB_OPTION = typer.Option(KB_DIR_DEFAULT, "--kb", help="Knowledge base folder.")


def _events() -> events.Events:
    """The terminal's sentences, or JSON lines when the app started this command (VIDETOR_EVENTS=json)."""
    return events.from_env(console.print)


def _step(fn, *args, **kwargs):
    """Run a pipeline step; what it cannot do is said in words and ends the command."""
    try:
        return fn(*args, **kwargs)
    except pipeline.PipelineError as exc:
        _events().emit("error", str(exc))
        raise typer.Exit(1) from exc


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def _xml_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("*.xml")))
        elif p.suffix.lower() == ".xml":
            files.append(p)
        else:
            console.print(f"[yellow]skipping {p}: not an .xml file[/]")
    return files


def ingest_files(xml_files: list[Path], kb_dir: Path) -> int:
    """Parse exports into kb/projects/ and rebuild kb/profile/. Returns the number of sequences ingested."""
    projects_dir = kb_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    ingested = 0
    for xml in xml_files:
        try:
            mains, nested = xmeml.load_all(xml)
        except ET.ParseError as exc:
            console.print(f"[red]{xml}: not valid XML ({exc})[/]")
            continue
        if not mains:
            console.print(f"[yellow]{xml}: no main sequence found[/]")
            continue
        for seq in mains:
            m = metrics.sequence_metrics(seq, nested)
            out = projects_dir / f"{_slug(xml.stem)}__{_slug(seq.name)}.json"
            out.write_text(json.dumps(m, indent=2) + "\n")
            shots = m["shot_length_s"]["count"] if m["shot_length_s"] else 0
            note = f", {m['nested']['expanded']}/{m['nested']['clips']} nests expanded" if m["nested"]["clips"] else ""
            console.print(f"{xml.name} :: {seq.name}  →  {out.name}  ({shots} shots, {m['duration_s']:.0f}s{note})")
            ingested += 1

    unique, dupes = profile.dedupe(profile.load_projects(kb_dir))
    for dupe, kept in dupes:
        console.print(f"[yellow]{dupe} is the same sequence as {kept}; counted once[/]")
    profiles = profile.roll_up(unique, profile.load_manifest(kb_dir))
    written = profile.write_profiles(kb_dir, profiles)
    console.print(f"\n{ingested} sequence(s) ingested; {len(written)} profile(s) written to {kb_dir / 'profile'}")
    if list(profiles) == [profile.DEFAULT_TYPE]:
        console.print("Name edit types in kb/manifest.json to split the profile by type (see README).")
    return ingested


@kb_app.command("ingest")
def kb_ingest(
    paths: list[Path] = typer.Argument(..., help="Premiere 'Final Cut Pro XML' exports, or folders of them."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Parse XML exports into kb/projects/ and rebuild kb/profile/."""
    ingest_files(_xml_files(paths), kb_dir)


@kb_app.command("show")
def kb_show(kb_dir: Path = KB_OPTION) -> None:
    """Print the profiles in kb/profile/."""
    paths = sorted((kb_dir / "profile").glob("*.json"))
    if not paths:
        console.print("No profiles yet. Run: videtor kb ingest <xml files or folder>")
        raise typer.Exit(1)
    table = Table(title="Style profiles")
    for col in ("type", "seqs", "format", "shot median s", "p10–p90 s", "cuts/min", "layers", "speed %", "adj/min", "audio fades %", "bed dB", "camera dB"):
        table.add_column(col, justify="left" if col in ("type", "format") else "right")
    for p in paths:
        pr = json.loads(p.read_text())
        shots = pr["shot_length_s"] or {}
        fmt = f"{pr['frame_size'][0]}×{pr['frame_size'][1]} @ {pr['fps']:g}"
        pct = lambda v: f"{v:.0%}" if v is not None else "–"  # noqa: E731
        num = lambda v, f=".1f": format(v, f) if v is not None else "–"  # noqa: E731
        table.add_row(
            pr["type"],
            str(pr["learned_from"]),
            fmt,
            num(shots.get("median"), ".2f"),
            f"{num(shots.get('p10'))}–{num(shots.get('p90'))}",
            num(pr["cuts_per_min"]),
            num(pr["layers"]["median"], ".0f"),
            pct(pr["speed_changes"]["share"]),
            num(pr["adjustment_layers_per_min"]),
            pct(pr["audio_transitions"]["share_of_audio_clips"]),
            num(pr["audio_db"]["bed"]),
            num(pr["audio_db"]["camera"]),
        )
    console.print(table)


@kb_app.command("footage")
def kb_footage(
    mount: Path = typer.Argument(..., help="A connected drive (or folder) holding the raw footage of ingested edits."),
    kb_dir: Path = KB_OPTION,
    rematch: bool = typer.Option(False, "--rematch", help="Reuse the saved index; only recompute used/unused from the XMLs."),
) -> None:
    """Index used and unused media on a drive into kb/footage/, with ffprobe metadata and used ranges."""
    projects = profile.load_projects(kb_dir)
    if not projects:
        console.print("Ingest some XML exports first: videtor kb ingest <files>")
        raise typer.Exit(1)
    out_dir = kb_dir / "footage"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _slug(mount.name or str(mount))
    extra_roots = profile.load_manifest(kb_dir).get("footage", {})  # "<key>": ["/Volumes/X/folder", ...]
    if rematch:
        index_path = out_dir / f"{name}.json"
        if not index_path.exists():
            console.print(f"No saved index at {index_path}; run without --rematch while the drive is connected.")
            raise typer.Exit(1)
        index = footage.rematch(json.loads(index_path.read_text()), projects, extra_roots)
    else:
        index = footage.scan(str(mount), projects, extra_roots, log=console.print)
    (out_dir / f"{name}.json").write_text(json.dumps(index, indent=1) + "\n")
    summary = footage.summarize(index)
    (out_dir / f"{name}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    table = Table(title=f"Footage on {mount}")
    for col in ("sequence", "root", "used files", "unused files", "used h", "unused h", "selected s", "select ratio", "top format"):
        table.add_column(col, justify="left" if col in ("sequence", "root", "top format") else "right")
    for key, s in summary.items():
        fmt = next(iter(s["formats"]), "–")
        table.add_row(
            key,
            ", ".join(os.path.relpath(r, str(mount)) for r in s["roots"]) if s["roots"] else "(copies)",
            str(s["files"]["used"]),
            str(s["files"]["unused"]),
            f"{s['seconds']['used_files'] / 3600:.1f}",
            f"{s['seconds']['unused_files'] / 3600:.1f}",
            f"{s['seconds']['selected']:.0f}",
            f"{s['select_ratio']:.1%}" if s["select_ratio"] is not None else "–",
            fmt,
        )
    console.print(table)
    unmatched = [t for t in index["top_level"] if not any(d.startswith(os.path.join(index["mount"], t)) for d in index["dirs"])]
    if unmatched:
        console.print(f"Not scanned (no ingested edit points there): {', '.join(unmatched)}")
    console.print(f"Index: {out_dir / (name + '.json')}  ·  next: videtor kb frames {mount}")


@kb_app.command("frames")
def kb_frames(
    mount: Path = typer.Argument(..., help="The drive indexed by `kb footage`."),
    kb_dir: Path = KB_OPTION,
    cache_dir: Path = typer.Option(Path("cache/frames"), "--cache"),
    per_clip: int = typer.Option(3, help="Frames sampled per video clip."),
) -> None:
    """Sample frames from every indexed video clip into cache/frames/ while the drive is connected."""
    index_path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    if not index_path.exists():
        console.print(f"No index at {index_path}. Run: videtor kb footage {mount}")
        raise typer.Exit(1)
    index = json.loads(index_path.read_text())
    done = footage.extract_frames(index, cache_dir, per_clip=per_clip, log=console.print)
    cov = footage.frame_coverage(index, cache_dir, per_clip)
    console.print(f"{done} frames written to {cache_dir}")
    if cov["missing"] or cov["missing_used_range"]:
        console.print(
            f"[yellow]{cov['missing']} of {cov['clips']} clips still lack frames, "
            f"{cov['missing_used_range']} used clips lack their used-range frame: keep the drive connected and run again.[/]"
        )
    else:
        console.print(f"[green]All {cov['clips']} clips sampled; {mount} can be unplugged.[/]")


@kb_app.command("selects")
def kb_selects(
    mount: Path = typer.Argument(..., help="A drive indexed by `kb footage` (need not be connected)."),
    kb_dir: Path = KB_OPTION,
    cache_dir: Path = typer.Option(Path("cache/frames"), "--cache"),
) -> None:
    """Compare the clips each edit used with the ones it left out: duration, format, sharpness, exposure, color."""
    index_path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    if not index_path.exists():
        console.print(f"No index at {index_path}. Run: videtor kb footage {mount}")
        raise typer.Exit(1)
    report = selects.selects_report(json.loads(index_path.read_text()), cache_dir)
    if not report:
        console.print("No sequence on this drive has both used and unused clips with frames.")
        raise typer.Exit(1)
    (kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.selects.json").write_text(json.dumps(report, indent=2) + "\n")
    for key, r in report.items():
        table = Table(title=f"{key}: used {r['n']['used']} vs unused {r['n']['unused']} clips (medians)")
        table.add_column("feature")
        table.add_column("used", justify="right")
        table.add_column("unused", justify="right")
        table.add_column("used / unused", justify="right")
        for k in selects.FEATURES:
            u, n = r["used"][k], r["unused"][k]
            ratio = f"{u / n:.2f}×" if u is not None and n not in (None, 0) else "–"
            table.add_row(k, "–" if u is None else f"{u:g}", "–" if n is None else f"{n:g}", ratio)
        console.print(table)


@kb_app.command("status")
def kb_status(kb_dir: Path = KB_OPTION, cache_dir: Path = typer.Option(Path("cache/frames"), "--cache")) -> None:
    """Per indexed drive: files, used/unused, frame coverage, and whether it is mounted right now."""
    indexes = [p for p in sorted((kb_dir / "footage").glob("*.json")) if not p.name.endswith(".summary.json")]
    if not indexes:
        console.print("No drives indexed yet. Run: videtor kb footage /Volumes/<drive>")
        raise typer.Exit(1)
    table = Table(title="Footage drives")
    for col in ("drive", "scanned", "mounted", "files", "used", "unused", "clips", "frames missing", "used-range missing", "ready"):
        table.add_column(col, justify="left" if col in ("drive", "scanned", "mounted", "ready") else "right")
    for p in indexes:
        index = json.loads(p.read_text())
        summary_path = p.with_name(p.stem + ".summary.json")
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        used = sum(s["files"]["used"] for s in summary.values())
        unused = sum(s["files"]["unused"] for s in summary.values() if s["roots"])
        cov = footage.frame_coverage(index, cache_dir)
        ready = not cov["missing"] and not cov["missing_used_range"]
        table.add_row(
            index["mount"],
            index.get("rematched_at") or index["scanned_at"][:16],
            "yes" if os.path.isdir(index["mount"]) else "no",
            str(len(index["files"])),
            str(used),
            str(unused),
            str(cov["clips"]),
            str(cov["missing"]),
            str(cov["missing_used_range"]),
            "[green]safe to unplug[/]" if ready else "[yellow]needs kb frames[/]",
        )
    console.print(table)


@kb_app.command("keeper")
def kb_keeper(
    bucket: str = typer.Argument(..., help="An edit type from kb/manifest.json, e.g. event."),
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = typer.Option(Path("cache/footmap"), "--footmap", help="Footage map folder."),
) -> None:
    """Learn what your edits of a bucket keep, against the footage they left out (needs the footage mapped)."""
    from videtor.analyze import tags
    from videtor.plan import keeper

    out = keeper.train(bucket, tags.embed_texts, kb_dir, footmap_dir=footmap_dir, log=console.print)
    if not out:
        console.print(f"No edit of {bucket!r} with mapped footage: label edits in kb/manifest.json and map their footage first.")
        raise typer.Exit(1)
    console.print(f"{bucket}: cv AUC {out['cv_auc']} over {out['samples']} samples ({out['kept_share']:.1%} kept) -> {kb_dir / 'keeper' / (bucket + '.json')}")
    console.print("keeps: " + "; ".join(f"{n} {w:+.2f}" for n, w in out["keeps"][:6]))
    console.print("leaves: " + "; ".join(f"{n} {w:+.2f}" for n, w in out["drops"][:6]))


@kb_app.command("breakdown")
def kb_breakdown(
    xml: Path = typer.Argument(..., help="A Premiere export (File > Export > Final Cut Pro XML)."),
    footage: list[str] = typer.Option([], "--footage", help="The edit's raw footage folder(s): tells shots from overlays."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Describe one edit: music (medley, silences), pace per stretch, ramps, stacks, flash frames, black, SFX."""
    from videtor.kb import breakdown

    console.print(breakdown.markdown(breakdown.describe(xml, kb_dir, footage or None)))


@kb_app.command("renders")
def kb_renders(
    folder: Path = typer.Argument(..., help="A folder of your finished, rendered reels (mp4/mov), e.g. ~/Downloads/Exports/Videos."),
    bucket: str = typer.Option(..., "--bucket", help="The edit type they are, e.g. sports-promo."),
    labels: bool = typer.Option(True, "--labels/--no-labels", help="Name what each shot shows with CLIP."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Learn a bucket's style from rendered reels alone: pace, transitions, black and white, moves, grade, beats, content."""
    from videtor.kb import breakdown, render

    style = render.read_folder(folder, bucket, kb_dir=kb_dir, labels=labels, log=console.print)
    console.print(render.markdown(style, bucket))
    console.print(f"style: {kb_dir / 'renders' / f'{bucket}.json'}; per reel: out/references/{bucket}/")
    console.print(f"bucket card: {breakdown.card(bucket, kb_dir)}")


@kb_app.command("train")
def kb_train(
    bucket: str = typer.Argument(..., help="A bucket whose reels have been read (videtor kb renders), e.g. sports-promo."),
    iters: int = typer.Option(30, "--iters", help="Search rounds."),
    check: bool = typer.Option(True, "--check/--no-check", help="First hold each reel out in turn and report how the policy does on it."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Learn a cutting policy from the bucket's reels (plan/gym.py): writes kb/policy/<bucket>.json for effects and drafts."""
    from videtor.plan import gym

    data = gym.train(bucket, kb_dir, iters=iters, check=check, log=console.print)
    ev = data.get("evaluation")
    if ev:
        m = ev["mean"]
        console.print(f"held-out reward (0 = the reel's own style; two real reels differ by about -8): generic {m['generic']}, "
                      f"medians {m['medians']}, trained {m['trained']}; trained beat the medians on {ev['won']} of {ev['of']} reels")
    p = data["policy"]
    console.print(f"policy: held shot {p['held_s']:.2f} s, open {p['open_s']:.2f} s, close {p['close_s']:.2f} s, "
                  f"{p['flicker_per_min']:.1f} flicker bursts a minute, {p['snap']:.0%} of cuts on accents (within {p['window_s']:.2f} s)")
    console.print("effect rates: " + ", ".join(f"{k} {v:.2f}" for k, v in data["rates"].items()))
    console.print(f"written: {kb_dir / 'policy' / f'{bucket}.json'}")


@kb_app.command("bucket")
def kb_bucket(
    bucket: str = typer.Argument(..., help="An edit type from kb/manifest.json, e.g. event."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Write kb/buckets/<bucket>.md: what work of this bucket is like, from your edits, its profile, keeper and rules."""
    from videtor.analyze import tags
    from videtor.kb import breakdown

    console.print(f"bucket card: {breakdown.card(bucket, kb_dir, embed_texts=tags.embed_texts)}")


@kb_app.command("learn")
def kb_learn(
    force: bool = typer.Option(False, "--force", help="Keep what was learnt even when the evaluation set says the cuts got worse."),
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = typer.Option(Path("cache/footmap"), "--footmap"),
) -> None:
    """The same as `videtor learn`."""
    _step(pipeline.learn, pipeline.Paths(kb=kb_dir, footmap=footmap_dir), _events(), force=force)


@app.command()
def analyze(
    mount: Path = typer.Argument(..., help="A drive indexed by `kb footage`; audio and motion need it connected."),
    kb_dir: Path = KB_OPTION,
    features: str = typer.Option("frames,audio,motion", "--features", help="Comma-separated subset of frames,audio,motion,faces,tags (faces and tags need only the sampled frames)."),
    only_used: bool = typer.Option(False, "--only-used", help="Analyze only clips an edit used."),
    key: str | None = typer.Option(None, "--key", help="Restrict to one sequence's footage pool, e.g. \"Guru.xml::Sequence 01\"."),
    workers: int = typer.Option(3, "--workers"),
    frames_dir: Path = typer.Option(Path("cache/frames"), "--frames"),
    out_dir: Path = typer.Option(Path("cache/analysis"), "--out"),
) -> None:
    """Per clip: looks from sampled frames, loudness/silence/speech, motion and in-clip cuts. Cached, resumable."""
    from videtor.analyze import run

    index_path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    if not index_path.exists():
        console.print(f"No index at {index_path}. Run: videtor kb footage {mount}")
        raise typer.Exit(1)
    index = json.loads(index_path.read_text())
    if key:
        pool = pipeline.pool_for(index, key)
        if not pool:
            console.print(f"No footage roots for {key!r} on {mount}; known keys: {list(index['roots'])}")
            raise typer.Exit(1)
        index = {**index, "files": pool}
    wanted = tuple(f.strip() for f in features.split(",") if f.strip() in run.FEATURES)
    if not wanted:
        console.print(f"--features must name some of {', '.join(run.FEATURES)}")
        raise typer.Exit(1)
    if any(f in wanted for f in ("audio", "motion")) and not os.path.isdir(index["mount"]):
        console.print(f"[yellow]{index['mount']} is not mounted; only frame-based features can run.[/]")
        wanted = tuple(f for f in wanted if f in run.FRAME_ONLY)
    counts = run.analyze_index(index, frames_dir, out_dir, wanted, only_used=only_used, workers=workers, log=console.print)
    cov = run.coverage(index, out_dir, run.FEATURES)
    console.print(f"computed: {counts}")
    console.print(f"coverage: {cov['have']} of {cov['clips']} clips  →  {out_dir}")


# --- cutting from a brief ------------------------------------------------------------

FOOTMAP_OPTION = typer.Option(Path("cache/footmap"), "--footmap", help="Footage map folder (videtor footmap).")
SPARSE_OPTION = typer.Option(Path("cache/sparse"), "--cache", help="Where the keyframe scan of every clip is kept (videtor.analyze.sparse).")


@app.command()
def orient(
    name: str = typer.Argument(..., help="The project name given to `prep`, or a sequence key."),
    rotate: int = typer.Option(..., "--rotate", help="Premiere's turn: -90 (counter-clockwise), 90, 180, or 0 to undo."),
    take: list[str] = typer.Option([], "--take", help="A take's file name (repeatable)."),
    camera: str = typer.Option(None, "--camera", help="Every take whose file name starts with this, e.g. M2U."),
    workers: int = typer.Option(None, "--workers", help="Decode processes (default: the machine's performance cores)."),
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = FOOTMAP_OPTION,
    out_root: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """Turn takes the detector could not judge (a sideways camera shooting bottles and hands has no faces to
    read): sets their orientation by hand, rebuilds their footage map (drive connected) and the overview."""
    from videtor.analyze import footmap
    from videtor.plan import fill, overview

    if rotate not in (0, -90, 90, 180):
        console.print("--rotate must be -90, 90, 180 or 0")
        raise typer.Exit(1)
    key = name if "::" in name else f"{name}::footage"
    index_path = _step(pipeline.index_path_for_key, key, kb_dir)
    index = json.loads(index_path.read_text())
    pool = footmap.pool(index, key)
    wanted = {t.lower() for t in take}
    hit = [e for e in pool if e["basename"].lower() in wanted or (camera and e["basename"].upper().startswith(camera.upper()))]
    if not hit:
        console.print("No take matches; give --take <file name> or --camera <prefix>.")
        raise typer.Exit(1)
    for e in hit:
        e["orientation"] = {"rotation": rotate, "source": "manual", "reason": "set by hand with videtor orient"}
    index_path.write_text(json.dumps(index, indent=1) + "\n")
    console.print(f"{len(hit)} take(s) set to {rotate:+d}; rebuilding their footage map")
    result = footmap.build_pool(index, key, workers=workers, cache_dir=footmap_dir, log=lambda s: None)
    console.print(f"  {result.get('built', 0)} rebuilt, {len(result.get('failed') or {})} failed")
    if "::footage" in key:
        takes = fill.load_takes([e for e in pool if (e.get("media") or {}).get("duration_s")], footmap_dir)
        md, sheets = overview.write(takes, fill.Pool(takes), footmap_dir / "frames", out_root / name, name)
        console.print(f"  overview rewritten: {md} · {len(sheets)} sheet(s)")


@app.command()
def prep(
    folder: Path = typer.Argument(..., help="A folder of raw footage on any drive, never edited before."),
    name: str = typer.Option(..., "--name", help="A short project name, e.g. 'nike-teaser'; the brief's shot map uses it."),
    workers: int = typer.Option(None, "--workers", help="Decode processes for the footage map (default: the machine's performance cores)."),
    sparse: bool = typer.Option(True, "--sparse/--dense", help="Read every take from its keyframes (minutes; the cut decodes what the map needs) or map every second now (hours)."),
    kb_dir: Path = KB_OPTION,
    frames_dir: Path = typer.Option(Path("cache/frames"), "--frames"),
    footmap_dir: Path = FOOTMAP_OPTION,
    sparse_dir: Path = SPARSE_OPTION,
    out_root: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """Get unknown footage ready for a brief: index it, read every take from its keyframes (faces, speech, CLIP,
    the library's shot types), turn sideways cameras upright and write an overview of what it holds, in minutes on
    every core. The cut then decodes in full only the takes the shot map can use (drive connected). `--dense` maps
    every second of every take now instead, as before, and says when the drive can be unplugged."""
    _step(pipeline.prepare, folder, name, workers=workers, sparse=sparse, out_root=out_root,
          paths=pipeline.Paths(kb=kb_dir, footmap=footmap_dir, sparse=sparse_dir, frames=frames_dir), ev=_events())


@app.command()
def music(
    folder: Path = typer.Argument(Path("~/Downloads/music"), help="Folder of tracks."),
    length: float = typer.Option(25.0, "--length", help="Seconds of music the cut needs."),
    reveal_at: float = typer.Option(0.0, "--reveal-at", help="Land a lift of the track this many seconds into the cut."),
    top: int = typer.Option(10, "--top"),
    cache_dir: Path = typer.Option(Path("cache/beats"), "--cache"),
) -> None:
    """Rank a music folder for a fast cut: cutting tempo, punch of the best stretch, and where to start."""
    from videtor.plan import music as mus

    scanned = mus.scan(folder.expanduser(), cache_dir, log=lambda s: None)
    by_path = {d["path"]: d for d in scanned}
    table = Table("#", "track", "bpm", "start", "punch", "lift", "score")
    for i, r in enumerate(mus.rank(scanned, length)[:top], start=1):
        w, placed = r["window"], mus.place(by_path[r["path"]], length, reveal_at) if reveal_at else None
        start = f"{placed['in_s']:.1f}s (drop {placed['drop_s']:.1f}s)" if placed else f"{w['start_s']:.1f}s"
        table.add_row(str(i), r["name"][:48], f"{r['cut_bpm']:.0f}", start, f"{w['punch']:.2f}", f"{(placed or w)['lift_db']:+.1f}", f"{r['score']:.2f}")
    console.print(table)


@app.command()
def search(
    mount: Path = typer.Argument(..., help="The drive whose index holds the project."),
    query: str = typer.Argument(..., help='What the shot shows, e.g. "rings on his fingers, close-up".'),
    key: str = typer.Option(..., "--key"),
    top: int = typer.Option(12, "--top"),
    footmap_dir: Path = FOOTMAP_OPTION,
    sparse_dir: Path = SPARSE_OPTION,
    kb_dir: Path = KB_OPTION,
) -> None:
    """Find moments by description: the best-matching half-second of each take, best takes first. Takes without a
    footage map are searched on their keyframe map (a sparse prep)."""
    import numpy as np

    from videtor.analyze import tags
    from videtor.plan import fill

    takes, n = pipeline.takes_for(_step(pipeline.index_for_key, key, kb_dir, mount), key, footmap_dir, sparse_dir)
    if not takes:
        console.print(f"No footage map for {key!r} yet ({n} takes in the pool). Run: videtor prep, or python -m videtor.analyze.footmap {mount} --key {key!r}")
        raise typer.Exit(1)
    pool = fill.Pool(takes)
    z = pool.similarity(tags.embed_texts([query]))
    rows = sorted(((float(zz.max()), t, int(zz.argmax())) for t, zz in zip(takes, z)), key=lambda r: -r[0])
    table = Table("match", "take", "at", "scale", "hands", "camera")
    for score, t, i in rows[:top]:
        _, _, moving, steady = pool.camera(t)
        camera = "moving" if moving[i] else "steady" if steady[i] else "-"
        table.add_row(f"{score:+.2f}", t.name, f"{t.a['t'][i]:.1f}s", str(t.scale[i]), str(int(t.a["hands"][i])), camera)
    k = sum(1 for t in takes if getattr(t, "sparse", False))
    console.print(f"{len(takes)} of {n} takes mapped" + (f" ({k} on the keyframe map only: moments to the nearest keyframe, camera unknown)" if k else ""))
    console.print(table)


@app.command("fx-test")
def fx_test(
    media: Path = typer.Argument(..., help="Any video file on this Mac (e.g. one of your exports)."),
    out: Path = typer.Option(Path("out/fx-test"), "--out"),
) -> None:
    """Write a test sequence with one clip per effect, a marker naming each: import it in Premiere to see which travel."""
    from videtor.plan import fx

    path = fx.write_test(media, out)
    console.print(f"{path}: in Premiere, File > Import, then play the sequence; each marker names the effect on the clip after it")
    console.print("tell Videtor which ones look wrong; three black-and-white variants are at the end (only one may work)")


@app.command("draft")
def draft_cmd(
    name: str = typer.Argument(..., help="The project name (out/<name>/shotmap.json)."),
    bucket: str = typer.Option("sports-promo", "--bucket", help="Whose learnt policy to draft from (kb/policy/<bucket>.json)."),
    length: float = typer.Option(30.0, "--length", help="Seconds."),
    key: str = typer.Option(None, "--key", help="The footage key (default <name>::footage after videtor prep)."),
    music: Path = typer.Option(None, "--music", help="The track: slots are counted on its beats and cut on its accents."),
    music_in: float = typer.Option(0.0, "--music-in", help="Where in the track the cut starts (seconds, on a beat)."),
    seed: int = typer.Option(0, "--seed", help="Another seed, another draft."),
    use_inventory: bool = typer.Option(True, "--inventory/--no-inventory", help="Give slots only to what the shoot holds (read from its footage map or keyframe map)."),
    skip: list[str] = typer.Option([], "--skip", help="Leave out takes whose path matches (repeatable), e.g. another shoot in the same folder; kept in the shot map."),
    order: str = typer.Option("style", "--order", help="style: what each slot asks for decides; shoot: also lean on the order of the day (early slots prefer early takes)."),
    out: Path = typer.Option(Path("out"), "--out"),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Draft a shot map offline from a bucket's learnt style (pace, what is shown where, effects) and from what the
    shoot holds: edit it, then cut."""
    from fnmatch import fnmatch

    from videtor.plan import draft, inventory

    mu = {"path": str(music.expanduser()), "in_s": music_in, "fade_out_s": 1.0} if music else None
    key = key or f"{name}::footage"
    inv = None
    if use_inventory:
        try:  # what the shoot holds: from the footage map, else the keyframe map prep made
            takes, _ = pipeline.takes_for(pipeline.index_for_key(key, kb_dir), key, Path("cache/footmap"), Path("cache/sparse"))
        except pipeline.PipelineError:
            takes = []
        takes = [t for t in takes if not any(fnmatch(t.path, pat) for pat in skip)]
        if takes:
            (out / name).mkdir(parents=True, exist_ok=True)
            inventory.write(takes, out / name, bucket)
            inv = inventory.load(out / name)
            console.print(f"the shoot holds ({len(takes)} takes):", markup=False)
            for line in inventory.text(inv, 10):
                console.print(f"  {line}", markup=False)
        else:
            console.print(f"no footage read for {key!r} yet (videtor prep): drafting from the style alone")
    m = draft.draft(name, bucket, length, key, mu, seed=seed, kb_dir=kb_dir, inventory=inv, order=order)
    if skip:
        m["skip"] = list(skip)
    path = draft.write(m, out)
    for sec in m["sections"]:
        console.print(f"{sec['name']}: " + ", ".join(f"{s['shows']} {s['beats']:g}b" + (" flicker" if s.get("flicker") else "") for s in sec["slots"]))
    console.print(f"draft: {path} (effects from {bucket}" + (", cut on accents" if m.get("cut_on") else "") + "); check it against the brief, then videtor cut")
    for note in m["notes"]:
        console.print(f"  note: {note}")


@app.command()
def cut(
    shotmap_path: Path = typer.Argument(..., help="shotmap.json written from the brief."),
    mount: Path = typer.Argument(None, help="The drive whose index holds the project (optional: found from the shot map's key)."),
    out: Path = typer.Option(None, "--out", help="Output folder (default: next to the shot map)."),
    sheets: bool = typer.Option(True, "--sheets/--no-sheets", help="Write second-look contact sheets."),
    sfx: bool = typer.Option(True, "--sfx/--no-sfx", help="Place SFX in your style (kb/sfx.json) on their own tracks."),
    fresh: bool = typer.Option(False, "--fresh", help="Forget the second-look swaps kept from earlier cuts (decisions.json)."),
    footmap_dir: Path = FOOTMAP_OPTION,
    sparse_dir: Path = SPARSE_OPTION,
    kb_dir: Path = KB_OPTION,
    beats_dir: Path = typer.Option(Path("cache/beats"), "--beats"),
) -> None:
    """Cut from a shot map: fill every slot from the footage map; writes picks.json, the second-look sheets and the
    Premiere XML. After a sparse prep it first decodes in full the takes the map can use (drive connected). Swaps
    made by an earlier second look are applied again."""
    _step(pipeline.cut, shotmap_path, out=out, mount=mount, sheets=sheets, sfx=sfx, fresh=fresh,
          paths=pipeline.Paths(kb=kb_dir, footmap=footmap_dir, sparse=sparse_dir, beats=beats_dir), ev=_events())


@app.command("cut-interview")
def cut_interview(
    aroll_path: Path = typer.Argument(..., help="aroll.json: the paper edit of the interview (plan/interview.py)."),
    shotmap_path: Path = typer.Argument(..., help="The B-roll shot map: sections name the region they fill."),
    sfx: bool = typer.Option(True, "--sfx/--no-sfx"),
    footmap_dir: Path = FOOTMAP_OPTION,
    kb_dir: Path = KB_OPTION,
    beats_dir: Path = typer.Option(Path("cache/beats"), "--beats"),
) -> None:
    """Cut an interview piece: the A-roll from the paper edit, B-roll filled over it and around it, the music
    dipped under the voice, SFX; writes picks.json, the second-look sheets, the cut sheet and the Premiere XML."""
    _step(pipeline.cut_interview, aroll_path, shotmap_path, sfx=sfx, paths=pipeline.Paths(kb=kb_dir, footmap=footmap_dir, beats=beats_dir), ev=_events())


@app.command()
def finish(
    picks_path: Path = typer.Argument(..., help="picks.json of a cut you have imported into Premiere."),
    stage: str = typer.Option("all", "--stage", help="grade | stabilize | lut | all (grade + stabilize)."),
    lut_from: str = typer.Option(None, "--lut-from", help="Source clip (file name) whose Input LUT, picked once in Premiere, goes on every clip."),
    temperature: float = typer.Option(15.0, "--temperature", help="Lumetri temperature (warm is positive)."),
    contrast: float = typer.Option(18.0, "--contrast"),
    saturation: float = typer.Option(105.0, "--saturation", help="Lumetri saturation, 100 = unchanged."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Find everything, change nothing."),
    force_bridge: bool = typer.Option(False, "--force-bridge", help="Apply effects over the bridge despite the crash risk."),
) -> None:
    """Finish an imported cut in the running Premiere over the bridge, one stage per call: a Lumetri grade on every
    video clip (track by track), Warp Stabilizer on the shots picks.json marks as handheld, and the Input LUT copied
    from one source clip to all (Premiere keys LUTs by an internal id a .cube gets once it is picked in its panel)."""
    import subprocess
    import tempfile

    picks = json.loads(picks_path.read_text())
    m = json.loads(Path(picks["source"]["shotmap"]).read_text()) if (picks.get("source") or {}).get("shotmap") else None
    track_of = {s["id"]: (0 if sec.get("region") in ("hook", "end") else 1) for sec in m["sections"] for s in sec["slots"]} if m else {}
    stabilize = [{"slot": s["id"], "name": s["pick"]["name"], "start_s": round(s["start_s"], 3), "track": track_of.get(s["id"], 0)}
                 for s in picks["slots"] if (s.get("fx") or {}).get("stabilize") and s.get("pick")]
    js = (Path(__file__).parent / "kb" / "js" / "finish_interview.js").read_text()
    grade = {"Temperature": temperature, "Contrast": contrast, "Saturation": saturation}

    def run(args: dict) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(f"const ARGS = {json.dumps(args)};\n" + js)
        r = subprocess.run([str(Path.home() / ".local/bin/prpr"), "--no-launch", "--timeout", "30", "-f", "json", "exec", f.name],
                           capture_output=True, text=True, errors="replace", timeout=300)
        if r.returncode != 0:
            console.print(f"[red]Premiere said:[/] {(r.stdout + r.stderr).strip()[-500:]}")
            raise typer.Exit(1)
        return json.loads(r.stdout)

    if not dry_run and stage in ("grade", "stabilize", "all") and not force_bridge:
        console.print("[yellow]Bulk effect changes over the bridge crashed Premiere 26.0.2 twice (2026-10-30): finish by hand.[/]\n"
                      "  Grade: an Adjustment Layer over the whole sequence on a new top track; Lumetri Color > Input LUT > Browse > your LUT;\n"
                      "  Temperature, Contrast, Saturation in Basic Correction.\n"
                      "  Stabilise: select the clips below, then drag Warp Stabilizer onto them:")
        for s in stabilize:
            console.print(f"    V{s['track'] + 1}  {s['start_s']:6.2f}s  {s['name']}  ({s['slot']})")
        console.print("  (--force-bridge runs the old bridge path, one clip at a time; only if you accept the crash risk.)")
        return
    base = {"sequence": picks["name"], "dry_run": dry_run, "tracks": [0, 1, 2, 3], "grade": grade, "stabilize": stabilize, "lut_from": lut_from}
    steps = []
    if stage in ("grade", "all"):
        steps += [{**base, "stage": "grade", "tracks": [t]} for t in (0, 1, 2, 3)]
    if stage in ("stabilize", "all"):
        steps.append({**base, "stage": "stabilize"})
    if stage == "lut":
        steps.append({**base, "stage": "lut"})
    for args in steps:
        rep = run(args)
        where = f"V{args['tracks'][0] + 1}" if args["stage"] == "grade" else args["stage"]
        extra = f" {rep.get('params', '')}" if args["stage"] == "grade" else f" LUT id {rep.get('lut_id')}" if args["stage"] == "lut" else ""
        console.print(f"{'(dry run) ' if dry_run else ''}{where}: {rep['done']} done, {rep['skipped']} skipped in {rep['ms']} ms{extra}")
        for note in rep.get("notes", [])[:8]:
            console.print(f"   {note}")


@app.command("cut-mv")
def cut_mv(
    spec_path: Path = typer.Argument(..., help="The music-video spec: song, who is in each take, sections (plan/musicvideo.py)."),
    footmap_dir: Path = FOOTMAP_OPTION,
    kb_dir: Path = KB_OPTION,
    beats_dir: Path = typer.Option(Path("cache/beats"), "--beats"),
) -> None:
    """Cut a lip-synced music video: every take lined up with the song from its camera audio, a cut on the beat
    grid, each slot the best synced take for the section's singer; writes picks.json, contact sheets of the cut
    and the Premiere XML (the song on A1 from its start, alternates on V2-V3)."""
    _step(pipeline.cut_mv, spec_path, paths=pipeline.Paths(kb=kb_dir, footmap=footmap_dir, beats=beats_dir), ev=_events())


@app.command()
def learn(
    force: bool = typer.Option(False, "--force", help="Keep what was learnt even when the evaluation set says the cuts got worse."),
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = FOOTMAP_OPTION,
) -> None:
    """Learn from all the feedback in kb/feedback: your taste from the shots you chose over the picks, and what
    you keep from every finished job read back. What is learnt is tried on the evaluation set first and kept only
    when the cuts got no worse."""
    _step(pipeline.learn, pipeline.Paths(kb=kb_dir, footmap=footmap_dir), _events(), force=force)


@app.command()
def readback(
    picks_path: Path = typer.Argument(..., help="picks.json of the cut that was finished."),
    render: Path = typer.Option(None, "--render", help="The finished video (any export of the cut). Default: the newest video in the project's folder."),
    xml: Path = typer.Option(None, "--xml", help="An XML exported from Premiere instead."),
    live: bool = typer.Option(False, "--live", help="Read the open Premiere sequence over the bridge instead."),
    sequence: str = typer.Option(None, "--sequence", help="The sequence to read (XML or live)."),
    force: bool = typer.Option(False, "--force", help="Keep it even when it does not look like this cut."),
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = FOOTMAP_OPTION,
) -> None:
    """Read a finished job back against its picks: which shots you kept, slipped, swapped or removed. From the
    finished video alone (nothing to export), or from an XML or Premiere live. Kept in kb/feedback for `videtor learn`."""
    _step(pipeline.read_back, picks_path, render=render, xml=xml, live=live, sequence=sequence, force=force,
          paths=pipeline.Paths(kb=kb_dir, footmap=footmap_dir), ev=_events())


@app.command()
def verdict(
    picks_path: Path = typer.Argument(..., help="picks.json of the cut."),
    word: str = typer.Argument(..., help="good or not."),
    note: str = typer.Option("", "--note", help="Why, in a line."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Keep your verdict on a cut as a whole: good or not, and a line of why (kb/feedback and verdict.json)."""
    if word.lower() not in ("good", "not"):
        console.print("The verdict is good or not.")
        raise typer.Exit(1)
    _step(pipeline.verdict, picks_path, word.lower() == "good", note, pipeline.Paths(kb=kb_dir), _events())


@app.command()
def preview(
    picks_path: Path = typer.Argument(..., help="picks.json from `videtor cut`."),
    scale: float = typer.Option(0.5, "--scale", help="Size against the sequence (0.5 = half)."),
) -> None:
    """A small video of the cut (what V1 plays, framed as the XML frames it, with the music) to watch without
    Premiere: preview.mp4 next to the picks. Needs the footage drive; no effects, SFX or alternates."""
    from videtor.export import preview as preview_mod

    try:
        preview_mod.render(pipeline.check_picks(json.loads(picks_path.read_text())), picks_path.parent / "preview.mp4", scale, log=console.print)
    except (FileNotFoundError, pipeline.PipelineError) as exc:
        console.print(str(exc))
        raise typer.Exit(1) from exc


@app.command("eval")
def eval_cmd(
    only: list[str] = typer.Option([], "--only", help="A project of the set by name (repeatable); default all."),
    save: bool = typer.Option(False, "--save", help="Keep this run as the baseline later runs are held to."),
    note: str = typer.Option("", "--note", help="What changed, kept with a saved baseline."),
    second_look: bool = typer.Option(True, "--second-look/--no-second-look", help="With or without the automatic second look."),
    taste: bool = typer.Option(True, "--taste/--no-taste", help="With or without the corrections learnt from your fixes."),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Cut every past project of kb/eval/set.json again from the cache (no drive, nothing written) and measure the
    cuts: against your own edit where there is one, and on their own (repeats, look-alike neighbours, jumps in
    light, how well the picks match). Fails when a guarded measure is worse than the saved baseline."""
    from videtor.plan import evaluate

    projects = evaluate.load_set(kb_dir)
    if not projects:
        console.print(f"No evaluation set at {evaluate.set_path(kb_dir)}.")
        raise typer.Exit(1)
    results = evaluate.run(projects, pipeline.Paths(kb=kb_dir), only=only or None, log=console.print, second_look=second_look, taste=taste)
    for line in evaluate.table(results):
        console.print(line, markup=False, soft_wrap=True)
    rows, ok = evaluate.compare(results, evaluate.load_baseline(kb_dir))
    for r in rows:
        console.print(f"  {r['project']}: {r['measure']} {r['was']} -> {r['now']} ({r['verdict']})", markup=False)
    if not rows:
        console.print("no guarded measure moved against the baseline" if evaluate.load_baseline(kb_dir) else "no baseline yet: run with --save to keep this one")
    if save:
        console.print(f"baseline saved: {evaluate.save_baseline(results, kb_dir, note)}")
    elif not ok:
        console.print("FAILED: a guarded measure is worse than the baseline (see the rows marked worse)")
        raise typer.Exit(1)


@app.command()
def look(
    picks_path: Path = typer.Argument(..., help="picks.json from `videtor cut`."),
    choices: list[str] = typer.Argument(None, help='Slot choices as id=k (runner-up 1..3), id=<take> (the runner-up from that take) or id=other-id:k (borrow), e.g. "details-3=2" "match-7=C1590" "reveal-2=reveal-1:1"; none rewrites the XML as is.'),
    note: list[str] = typer.Option([], "--note", help='Why, as id=text, e.g. "details-3=face sharper".'),
    by: str = typer.Option("second look", "--by", help="Who chose (kept in decisions.json and the feedback): second look, you, app."),
) -> None:
    """Apply second-look choices to picks.json and rewrite the Premiere XML. The choices are kept in decisions.json
    next to it, so cutting the shot map again keeps them."""
    parse = lambda v: (v.rsplit(":", 1)[0], int(v.rsplit(":", 1)[1])) if ":" in v else int(v) if v.isdigit() else v  # noqa: E731
    chosen = {c.split("=", 1)[0]: parse(c.split("=", 1)[1]) for c in (choices or [])}
    notes = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in note}
    _step(pipeline.look, picks_path, chosen, notes, ev=_events(), by=by)


@app.command()
def ui(
    port: int = typer.Option(8765, "--port", help="The local port the page is served on (this Mac only)."),
    window: bool = typer.Option(True, "--window/--browser", help="A native window (pywebview: uv sync --extra analyze --extra ui) or the browser."),
    out_root: Path = typer.Option(Path("out"), "--out", help="The output folder offered for new projects; every prep and build can pick its own."),
) -> None:
    """The user interface: a local page to prep a shoot into a folder you choose, write the brief and the shot map,
    cut, do the second look and sort the library. Every action runs as its own videtor process; a cut pauses running
    preps and gets the machine, and they carry on after it."""
    from videtor.ui import server

    server.main(port=port, window=window, out_root=out_root, log=console.print)


@library_app.command("build")
def library_build(
    folders: list[Path] = typer.Argument(..., help="Footage folders on the drives, e.g. /Volumes/Paritosh_3/TPL/Videos."),
    out: Path = typer.Option(None, "--out", help="The library folder (default out/library/<name>)."),
    name: str = typer.Option(None, "--name", help="A short name for the library, e.g. tpl (default: the first folder's name)."),
    workers: int = typer.Option(None, "--workers", help="Clips scanned at once (default: the machine's performance cores, memory permitting)."),
    copy: bool = typer.Option(False, "--copy", help="Copy the files into the folders instead of linking them."),
    extract: bool = typer.Option(False, "--extract", help="Also cut every interview bite to its own file (stream copy, no re-encode)."),
    transcribe: bool = typer.Option(True, "--transcribe/--no-transcribe", help="Transcribe the speaker clips (local Whisper) to find the bites."),
    fresh: bool = typer.Option(False, "--fresh", help="Rebuild even if clips were moved since the last build (the moves are lost unless learnt first)."),
    cache_dir: Path = SPARSE_OPTION,
    kb_dir: Path = KB_OPTION,
) -> None:
    """Sort every clip under the folders into wide / close-up / talking-head / interview-bite / b-roll, with contact
    sheets per class, a Premiere sequence of the interview bites and library.md. Offline: keyframes only, every core.
    Move a link to another folder and `videtor library learn` to teach it."""
    from videtor.plan import library

    folders = [f.expanduser().resolve() for f in folders]
    for f in folders:
        if not f.is_dir():
            console.print(f"Not a folder: {f}")
            raise typer.Exit(1)
    name = name or _slug(folders[0].name)
    out = (out or Path("out") / "library" / name).expanduser()
    try:
        lib = library.run_build(folders, out, cache_dir=cache_dir, kb_dir=kb_dir, workers=workers, copy=copy, extract=extract,
                                transcribe=transcribe, fresh=fresh, log=lambda s: console.print(f"  {s}"))
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1)
    console.print(f"Library: {out}  ·  read {out / 'library.md'} and the sheets in {out / 'sheets'}")
    if lib.get("bites"):
        console.print(f"Interview bites: {lib['bites_xml']} (Premiere: File > Import) and {out / 'interview-bite' / 'bites.md'}")


@library_app.command("learn")
def library_learn(
    out: Path = typer.Argument(..., help="The library folder, after you moved links between its class folders."),
    cache_dir: Path = SPARSE_OPTION,
    kb_dir: Path = KB_OPTION,
) -> None:
    """Read the folders back as your labels, keep them, and fit the model the next build sorts with."""
    from videtor.plan import library

    try:
        library.learn(out.expanduser(), cache_dir, kb_dir, log=lambda s: console.print(f"  {s}"))
    except FileNotFoundError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(1)


@library_app.command("status")
def library_status(out: Path = typer.Argument(..., help="The library folder.")) -> None:
    """What sits in each folder now, and what moved since the build."""
    from videtor.plan import library

    st = library.status(out.expanduser())
    if not st.get("built"):
        console.print(f"No library at {out} (no {library.LIBRARY_JSON}); run videtor library build.")
        raise typer.Exit(1)
    console.print(f"built {st['built_at']}: {st['clips']} clips · " + ", ".join(f"{st['counts'].get(c, 0)} {c}" for c in library.CLASSES))
    for p, (was, now) in sorted(st["moved"].items()):
        console.print(f"  moved: {os.path.basename(p)}  {was} -> {now}")
    if st["missing"]:
        console.print(f"  {len(st['missing'])} clips are in no folder any more")
    console.print(f"{len(st['moved'])} moved since the build" + (f"; run videtor library learn {out} to keep them" if st["moved"] else ""))


if __name__ == "__main__":
    app()
