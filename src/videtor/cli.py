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

from videtor.kb import footage, metrics, profile, selects, xmeml

app = typer.Typer(
    help="Timelines for Premiere Pro and DaVinci Resolve from raw footage, cut in your own style.",
    no_args_is_help=True,
)
kb_app = typer.Typer(help="Knowledge base built from your past edits.", no_args_is_help=True)
app.add_typer(kb_app, name="kb")
console = Console()

KB_DIR_DEFAULT = Path("kb")
KB_OPTION = typer.Option(KB_DIR_DEFAULT, "--kb", help="Knowledge base folder.")


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
    kb_dir: Path = KB_OPTION,
    footmap_dir: Path = typer.Option(Path("cache/footmap"), "--footmap"),
) -> None:
    """Learn your taste from every read-back in kb/feedback/: the windows you chose over the ones Videtor
    picked become corrections to the filler's weights (kb/taste.json), applied by the next `videtor cut`."""
    from videtor.analyze import tags
    from videtor.plan import taste

    files = sorted((kb_dir / "feedback").glob("*.json"))
    if not files:
        console.print("No read-backs yet. Fix a cut in Premiere, then: python -m videtor.kb.readback <picks.json> --live")
        raise typer.Exit(1)
    pools: dict[str, list] = {}

    def pool_for_key(key: str | None) -> list:
        if key and key not in pools:
            pools[key] = _takes_for(_index_for_key(key, kb_dir), key, footmap_dir)[0]
        return pools.get(key) or []

    out = taste.learn(files, pool_for_key, tags.embed_texts, kb_dir)
    console.print(f"{out['pairs']} fixes from {len(out['from'])} read-back(s) → {kb_dir / 'taste.json'}")
    for term, w in sorted(out["weights"].items(), key=lambda kv: -abs(kv[1])):
        if abs(w) >= 0.01:
            console.print(f"  {term:13} {w:+.3f}")


@app.command()
def ingest(folder: Path) -> None:
    """Index a folder of raw media. Phase 1."""
    console.print("[yellow]ingest arrives in Phase 1 (needs ffprobe).[/]")
    raise typer.Exit(2)


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
        pool = _pool_for(index, key)
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


def _pool_for(index: dict, key: str) -> list[dict]:
    """Every file under the sequence's footage roots, used flags ignored: the planner must not peek. Files the
    index marks `excluded` (another shoot on the same card) stay out."""
    roots = index["roots"].get(key) or []
    roots = [roots] if isinstance(roots, str) else roots
    return [f for f in index["files"] if any(f["path"].startswith(r.rstrip("/") + "/") for r in roots) and not f.get("excluded")]


@app.command()
def plan(
    mount: Path = typer.Argument(..., help="A drive indexed by `kb footage` that holds the project's footage."),
    key: str = typer.Option(..., "--key", help='Sequence key whose footage pool to cut, e.g. "Guru.xml::Sequence 01".'),
    edit_type: str = typer.Option(..., "--type", help="Edit type whose profile sets the pace, e.g. brand-social."),
    duration: float = typer.Option(..., "--duration", help="Target length in seconds."),
    name: str = typer.Option("Videtor cut", "--name", help="Sequence name in the NLE."),
    out_dir: Path = typer.Option(Path("out/plan"), "--out"),
    kb_dir: Path = KB_OPTION,
    analysis_dir: Path = typer.Option(Path("cache/analysis"), "--analysis"),
    seed: int = typer.Option(0, "--seed"),
    music: Path | None = typer.Option(None, "--music", help="Music track: cuts land on its beats and it goes on its own audio track."),
    opening: str = typer.Option(
        "glimpse", "--opening",
        help="glimpse (subject in part → details → zoom-out reveal) | tease (details → reveal) | subject (open on the subject) | top (strongest shot) | none (shoot order).",
    ),
    pace: str = typer.Option("normal", "--pace", help="normal | fast (quicker half of your shot lengths) | slow"),
    beat_lead: float = typer.Option(0.12, "--beat-lead", help="Seconds each cut lands ahead of the beat (your habit: ~0.14 s)."),
    speech: str = typer.Option("favor", "--speech", help="favor | avoid | neutral: whether talking clips belong in this piece."),
) -> None:
    """Rule-based planner: pick, cut and order shots from the footage pool in the edit type's pace; writes plan.json and Premiere XML."""
    from videtor.analyze import beats as beats_mod
    from videtor.export import xmeml_writer
    from videtor.plan import build, rules

    index_path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    profile_path = kb_dir / "profile" / f"{edit_type}.json"
    for p in (index_path, profile_path):
        if not p.exists():
            console.print(f"Missing {p}")
            raise typer.Exit(1)
    index = json.loads(index_path.read_text())
    prof = json.loads(profile_path.read_text())
    pool = _pool_for(index, key)
    if not pool:
        console.print(f"No footage roots for {key!r} on {mount}; known keys: {list(index['roots'])}")
        raise typer.Exit(1)
    samples = rules.shot_length_samples(kb_dir, edit_type)
    in_samples = rules.in_point_samples(kb_dir, edit_type) or rules.in_point_samples(kb_dir)
    beats = music_info = None
    if music:
        cached = Path("cache/beats") / (music.stem + ".beats.json")
        if not music.exists() and not cached.exists():
            console.print(f"Music track not found and no cached beats for it: {music}")
            raise typer.Exit(1)
        if not music.exists():
            console.print(f"[yellow]{music} is offline; using its cached beats. Premiere will show it offline until the drive is back.[/]")
        tracked = beats_mod.load_or_track(music, Path("cache/beats"))
        beats = tracked["beats_s"]
        music_info = {"path": str(music), "duration_s": tracked["duration_s"], "tempo_bpm": tracked["tempo_bpm"]}
        console.print(f"music: {music.name} · {tracked['tempo_bpm']:.0f} BPM · {len(beats)} beats")
    fps = float(prof.get("fps") or 25.0)
    width, height = prof.get("frame_size") or [1080, 1920]
    if opening not in ("glimpse", "tease", "subject", "top", "none"):
        console.print("--opening must be glimpse, tease, subject, top or none")
        raise typer.Exit(1)
    shots = rules.plan(
        pool, analysis_dir, prof, samples, duration, seed=seed, in_samples=in_samples, beats=beats, hook_first=opening == "top",
        fps=fps, pace=pace, beat_lead_s=beat_lead, speech_weight={"favor": 0.4, "avoid": -0.4, "neutral": 0.0}.get(speech, 0.4),
        opening=opening if opening in ("glimpse", "tease", "subject") else None,
    )
    if not shots:
        console.print("Nothing to cut: no usable video clips in the pool.")
        raise typer.Exit(1)
    settings = {
        "key": key, "edit_type": edit_type, "target_s": duration, "fps": fps, "frame_size": [width, height], "name": name,
        "seed": seed, "mount": index["mount"], "music": music_info, "opening": opening, "pace": pace, "beat_lead_s": beat_lead, "speech": speech,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "plan.json").write_text(json.dumps(rules.plan_to_json(shots, settings), indent=1) + "\n")
    xml_path = xmeml_writer.write_xmeml(build.build_timeline(shots, name, fps, music_info), out_dir / f"{_slug(name)}.xml", fps, width, height)

    table = Table(title=f"{name}: {len(shots)} shots, {sum(s.length_s for s in shots):.1f} s of {duration:.0f} s")
    for col in ("#", "clip", "in", "slot", "score", "why"):
        table.add_column(col, justify="right" if col in ("#", "in", "slot", "score") else "left")
    for i, s in enumerate(shots, start=1):
        table.add_row(str(i), s.name[:28], f"{s.in_s:.1f}s", f"{s.length_s:.2f}s", f"{s.score:+.2f}", "; ".join(s.reasons[:2])[:70])
    console.print(table)
    console.print(f"plan: {out_dir / 'plan.json'}  ·  Premiere XML: {xml_path}")


@app.command()
def export(
    plan_path: Path = typer.Argument(Path("out/plan/plan.json"), help="A plan.json written by `videtor plan`."),
    to: str = typer.Option("premiere", "--to", help="premiere | otio"),
    out: Path | None = typer.Option(None, "--out", help="Output file; defaults next to the plan."),
) -> None:
    """Write a plan as Premiere XML (our writer) or .otio (for Resolve)."""
    import opentimelineio as otio

    from videtor.export import xmeml_writer
    from videtor.plan import build, rules

    data = json.loads(plan_path.read_text())
    shots, st = rules.shots_from_json(data), data["settings"]
    timeline = build.build_timeline(shots, st["name"], st["fps"], st.get("music"))
    if to == "premiere":
        path = out or plan_path.with_name(f"{_slug(st['name'])}.xml")
        xmeml_writer.write_xmeml(timeline, path, st["fps"], *st["frame_size"])
    elif to == "otio":
        path = out or plan_path.with_name(f"{_slug(st['name'])}.otio")
        otio.adapters.write_to_file(timeline, str(path))
    else:
        console.print("--to must be premiere or otio")
        raise typer.Exit(1)
    console.print(f"wrote {path}")


@kb_app.command("rerank-eval")
def kb_rerank_eval(kb_dir: Path = KB_OPTION, analysis_dir: Path = typer.Option(Path("cache/analysis"), "--analysis")) -> None:
    """Leave-one-project-out: does a reranker trained on your other edits pick this edit's clips better than the hand rules?"""
    from videtor.plan import rerank

    projects = rerank.load_projects(kb_dir, analysis_dir)
    if len(projects) < 3:
        console.print("Need at least three indexed edits with used and unused clips.")
        raise typer.Exit(1)
    result = rerank.leave_one_project_out(projects)
    (kb_dir / "rerank-eval.json").write_text(json.dumps(result, indent=2) + "\n")
    table = Table(title="Take selection, leave-one-project-out (precision among the top-k picks, k = clips you used)")
    for col in ("edit", "pool", "used", "chance", "hand rules", "learned", "+content", "hand AP", "learned AP", "+content AP"):
        table.add_column(col, justify="left" if col == "edit" else "right")
    for key, r in result.items():
        if key.startswith("_"):
            continue
        table.add_row(
            key.split(".xml")[0], str(r["pool"]), str(r["used"]), f"{r['chance']:.0%}", f"{r['hand_p@k']:.0%}", f"{r['learned_p@k']:.0%}",
            f"{r['content_p@k']:.0%}", f"{r['hand_ap']:.2f}", f"{r['learned_ap']:.2f}", f"{r['content_ap']:.2f}",
        )
    console.print(table)
    console.print("weights (all projects): " + ", ".join(f"{k} {v:+.2f}" for k, v in result["_weights"].items()))


@kb_app.command("benchmark")
def kb_benchmark(
    mount: Path = typer.Argument(..., help="The drive whose index holds the real edit's used clips."),
    key: str = typer.Option(..., "--key"),
    plan_path: Path = typer.Option(Path("out/plan/plan.json"), "--plan"),
    kb_dir: Path = KB_OPTION,
) -> None:
    """Score a plan against the real edit of the same footage: clips, moments, pace."""
    from videtor.plan import benchmark, rules

    index = json.loads((kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json").read_text())
    data = json.loads(plan_path.read_text())
    shots = rules.shots_from_json(data)
    real = benchmark.load_real_metrics(kb_dir, key)
    result = benchmark.score_plan(shots, index, key, real)
    opening = data["settings"].get("opening")
    if real and opening in ("glimpse", "tease", "subject"):
        result["opening"] = benchmark.opening_comparison(shots, index, real, Path("cache/analysis"), opening)
    (plan_path.with_name("benchmark.json")).write_text(json.dumps(result, indent=2) + "\n")
    c, m, p = result["clips"], result["moments"], result["pace"]
    console.print(f"clips: planned {c['planned']}, you used {c['real']}, shared {c['shared']}  →  precision {c['precision']}, recall {c['recall']}")
    console.print(f"moments: {m['inside_your_used_ranges_s']} of {m['planned_s']} planned seconds fall inside ranges you used ({m['share']})")
    console.print(f"pace: planned {p['planned_shots']} shots, median {p['planned_median_shot_s']} s; real {p.get('real_shots')} shots, median {p.get('real_median_shot_s')} s over {p.get('real_duration_s')} s")
    if o := result.get("opening"):
        console.print(f"opening ({' → '.join(o['template'])}): plan {o['plan']:.2f}, your edit {o['real']:.2f} ({', '.join(o['real_beats'])})")


# --- cutting from a brief ------------------------------------------------------------

FOOTMAP_OPTION = typer.Option(Path("cache/footmap"), "--footmap", help="Footage map folder (videtor footmap).")


def _index_for(mount: Path, kb_dir: Path) -> dict:
    index_path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    if not index_path.exists():
        console.print(f"No index at {index_path}. Run: videtor kb footage {mount}")
        raise typer.Exit(1)
    return json.loads(index_path.read_text())


def _index_for_key(key: str, kb_dir: Path, mount: Path | None = None) -> dict:
    """The footage index whose roots hold `key`: the drive's own index when given, else any index that lists it
    (a folder prepared with `videtor prep` has an index of its own)."""
    candidates = []
    if mount:
        candidates.append(kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json")
    candidates += sorted(p for p in (kb_dir / "footage").glob("*.json") if not p.name.endswith((".summary.json", ".selects.json")))
    for p in candidates:
        if p.exists():
            index = json.loads(p.read_text())
            if index.get("roots", {}).get(key):
                return index
    console.print(f"No footage index lists {key!r}. For a new folder run: videtor prep <folder> --name <name>")
    raise typer.Exit(1)


def _index_path_for_key(key: str, kb_dir: Path) -> Path:
    for p in sorted((kb_dir / "footage").glob("*.json")):
        if not p.name.endswith((".summary.json", ".selects.json")) and json.loads(p.read_text()).get("roots", {}).get(key):
            return p
    console.print(f"No footage index lists {key!r}.")
    raise typer.Exit(1)


@app.command()
def orient(
    name: str = typer.Argument(..., help="The project name given to `prep`, or a sequence key."),
    rotate: int = typer.Option(..., "--rotate", help="Premiere's turn: -90 (counter-clockwise), 90, 180, or 0 to undo."),
    take: list[str] = typer.Option([], "--take", help="A take's file name (repeatable)."),
    camera: str = typer.Option(None, "--camera", help="Every take whose file name starts with this, e.g. M2U."),
    workers: int = typer.Option(3, "--workers"),
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
    index_path = _index_path_for_key(key, kb_dir)
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


def _mount_of(folder: Path) -> Path:
    """The drive a folder sits on (/Volumes/<name>), or its parent folder on the internal disk."""
    parts = folder.parts
    return Path(*parts[:3]) if len(parts) > 2 and parts[1] == "Volumes" else folder.parent


@app.command()
def prep(
    folder: Path = typer.Argument(..., help="A folder of raw footage on any drive, never edited before."),
    name: str = typer.Option(..., "--name", help="A short project name, e.g. 'nike-teaser'; the brief's shot map uses it."),
    workers: int = typer.Option(3, "--workers", help="Decode processes for the footage map."),
    kb_dir: Path = KB_OPTION,
    frames_dir: Path = typer.Option(Path("cache/frames"), "--frames"),
    footmap_dir: Path = FOOTMAP_OPTION,
    out_root: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """Get unknown footage ready for a brief: index it, sample it, turn sideways cameras upright, map every
    second, and write an overview of what it holds. Prints the shot-map key and when the drive can be unplugged."""
    from videtor.analyze import footmap
    from videtor.plan import fill, overview

    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        console.print(f"Not a folder: {folder}")
        raise typer.Exit(1)
    mount, key = _mount_of(folder), f"{name}::footage"
    index_path = kb_dir / "footage" / f"{_slug(name)}.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index = json.loads(index_path.read_text())
        console.print(f"[1/5] index: reusing {index_path} ({len(index['files'])} files)")
    else:
        console.print(f"[1/5] index: scanning {folder}")
        index = footage.scan(str(mount), [], {key: [str(folder)]}, log=lambda s: console.print(f"      {s}"))
        index["project"] = {"name": name, "folder": str(folder), "key": key}
        index_path.write_text(json.dumps(index, indent=1) + "\n")
    pool = footmap.pool(index, key)
    console.print(f"      {len(pool)} takes in the pool, {sum((e['media'] or {}).get('duration_s', 0) for e in pool) / 60:.1f} min")
    console.print("[2/5] stills: 3 per take")
    footage.extract_frames({**index, "files": pool}, frames_dir, log=lambda s: None)
    try:
        from videtor.analyze import orientation
    except ImportError:
        orientation = None
    if orientation:
        console.print("[3/5] orientation: faces found only when a take is turned mark a sideways camera")
        result = orientation.orient_takes(pool, frames_dir)
        console.print(f"      {result.get('counts', result)}")
        for cam in result.get("cameras") or []:
            if cam.get("check"):
                console.print(f"      [yellow]check[/]: camera {cam['camera']} — {cam['decided']} of its {cam['takes']} takes turned by their faces, "
                              f"the rest left as shot. If the overview shows them sideways: videtor orient {name} --camera <prefix> --rotate <turn>")
        by_fid = {e["fid"]: e for e in pool}
        for f in index["files"]:
            if f["fid"] in by_fid and "orientation" in by_fid[f["fid"]]:
                f["orientation"] = by_fid[f["fid"]]["orientation"]
        index_path.write_text(json.dumps(index, indent=1) + "\n")
    else:
        console.print("[3/5] orientation: detector not installed; sideways cameras stay as shot")
    console.print("[4/5] footage map: 2 samples a second of every take")
    result = footmap.build_pool(index, key, workers=workers, cache_dir=footmap_dir, log=lambda s: console.print(f"      {s}"))
    console.print(f"      {result.get('built', 0)} built, {result.get('already_done', 0)} already mapped, {len(result.get('failed') or {})} failed")
    console.print(f"      {mount} can be unplugged now: everything below runs from the cache")
    takes = fill.load_takes([e for e in pool if (e.get("media") or {}).get("duration_s")], footmap_dir)
    md, sheets = overview.write(takes, fill.Pool(takes), footmap_dir / "frames", out_root / name, name)
    console.print(f"[5/5] overview: {md}  ·  {len(sheets)} contact sheet(s)")
    console.print(f"Shot-map key: {key!r}. Next: write {out_root / name / 'shotmap.json'} from the brief, then videtor cut it.")


def _takes_for(index: dict, key: str, footmap_dir: Path) -> tuple[list, int]:
    """The pool's takes that have a footage map, and how many takes the pool has."""
    from videtor.plan import fill

    entries = [
        f for f in _pool_for(index, key)
        if f["kind"] == "video" and not f.get("duplicate_of") and not footage.is_render(f["path"]) and (f.get("media") or {}).get("duration_s")
    ]
    return fill.load_takes(entries, footmap_dir), len(entries)


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
    kb_dir: Path = KB_OPTION,
) -> None:
    """Find moments by description: the best-matching half-second of each take, best takes first."""
    import numpy as np

    from videtor.analyze import tags
    from videtor.plan import fill

    takes, n = _takes_for(_index_for_key(key, kb_dir, mount), key, footmap_dir)
    if not takes:
        console.print(f"No footage map for {key!r} yet ({n} takes in the pool). Run: python -m videtor.analyze.footmap {mount} --key {key!r}")
        raise typer.Exit(1)
    pool = fill.Pool(takes)
    z = pool.similarity(tags.embed_texts([query]))
    rows = sorted(((float(zz.max()), t, int(zz.argmax())) for t, zz in zip(takes, z)), key=lambda r: -r[0])
    table = Table("match", "take", "at", "scale", "hands", "camera")
    for score, t, i in rows[:top]:
        _, _, moving, steady = pool.camera(t)
        camera = "moving" if moving[i] else "steady" if steady[i] else "-"
        table.add_row(f"{score:+.2f}", t.name, f"{t.a['t'][i]:.1f}s", str(t.scale[i]), str(int(t.a["hands"][i])), camera)
    console.print(f"{len(takes)} of {n} takes mapped")
    console.print(table)


def _write_cut(picks: dict, out: Path, music: dict | None, footmap_dir: Path = Path("cache/footmap")) -> Path:
    from videtor.export import xmeml_writer as w
    from videtor.plan import second_look
    from videtor.plan.build import build_from_picks

    if picks.get("stabilize"):  # steady the handheld picks with Motion keyframes (plan/stabilize.py; drive connected)
        from videtor.plan import stabilize

        stabilize.plan(picks, log=console.print)
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    tl = build_from_picks(picks, music, sfx=picks.get("sfx"))
    second_look.cut_sheet(picks, footmap_dir / "frames", out / "cut-sheet.jpg")
    second_look.match_sheet(picks, footmap_dir / "frames", out / "match-cuts.jpg")
    return w.write_xmeml(tl, out / f"{picks['name']}.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])


def _level_bytes(picks: dict, target_lufs: float) -> None:
    """Each byte's voice gain (pick["gain_db"]) so the bytes sit at one loudness; the music dips under them in the build."""
    from videtor.plan import interview

    for s in picks["slots"]:
        p = s.get("pick")
        if not s.get("byte") or not p:
            continue
        lufs = interview.segment_lufs(p["path"], float(p["in_s"]), float(p["out_s"])) if Path(p["path"]).exists() else None
        p["gain_db"] = round(float(min(12.0, max(-12.0, target_lufs - lufs))), 1) if lufs is not None and lufs > -70 else 0.0


def _place_sfx(picks: dict, kb_dir: Path, beats_dir: Path, footmap_dir: Path) -> list[dict] | None:
    """Your SFX habits (kb/sfx.json) placed on the cut; None when the habits have not been mined yet."""
    from videtor.analyze import beats as beats_mod
    from videtor.kb import sfx as sfx_kb
    from videtor.plan import sfx as sfx_plan

    habits = sfx_kb.load(kb_dir)
    if not habits:
        return None
    mu = picks.get("music") or {}
    data = beats_mod.load_or_track(Path(mu["path"]), beats_dir) if mu.get("path") and Path(mu["path"]).exists() else None
    skip = set(picks.get("sfx_skip") or [])  # kinds the shot map leaves out, e.g. shutters when the flashes are party lights
    return _clear_bytes([s for s in sfx_plan.place(picks, habits, music_data=data, footmap_dir=footmap_dir) if s["kind"] not in skip],
                        picks["slots"])


def _clear_bytes(sfx: list[dict], slots: list[dict]) -> list[dict]:
    """The SFX around the bytes: none starts on one; one running into one (a whoosh peaking on the cut) ends on the
    cut, fading over two frames."""
    talk = [(s["start_s"], s["start_s"] + s["length_s"]) for s in slots if s.get("byte")]
    out = [s for s in sfx if not any(a - 0.05 < s["start_s"] < b - 0.05 for a, b in talk)]
    for s in out:
        for a, _ in talk:
            if s["start_s"] < a < s["start_s"] + (s["out_s"] - s["in_s"]):
                s["out_s"], s["fade_out_s"] = round(s["in_s"] + (a - s["start_s"]) + 0.04, 3), 0.08
    return out


def _medley(segments: list[dict], cache_dir: Path) -> tuple[dict, list[float]]:
    """Music as a medley: each segment ({path, in_s, beats | length_s}) plays after the last, from a beat of its own
    grid; {gap_beats | gap_s} is silence, its beats continuing the last tempo. Returns the builder's music
    ({segments: [{path, duration_s, in_s, start_s, length_s}], fade_out_s}) and the beats on the cut's clock."""
    import numpy as np

    from videtor.analyze import beats as beats_mod

    out, beats, t, period = [], [], 0.0, 0.5
    for g in segments:
        if not g.get("path"):
            length = float(g.get("gap_s") or float(g.get("gap_beats") or 0) * period)
            beats += [round(t + k * period, 3) for k in range(int(length / period - 0.05) + 1)]
            t += length
            continue
        data = beats_mod.load_or_track(Path(g["path"]).expanduser(), cache_dir)
        in_s = float(g.get("in_s") or 0.0)
        grid = [round(b - in_s, 3) for b in data["beats_s"] if b >= in_s - 0.05]
        if len(grid) > 1:
            period = float(np.median(np.diff(grid)))
        if g.get("beats") is not None:
            n = int(g["beats"])
            length = grid[n] if n < len(grid) else grid[-1] + (n - len(grid) + 1) * period
        elif g.get("length_s"):
            length = float(g["length_s"])
        else:
            length = data["duration_s"] - in_s
        beats += [round(t + b, 3) for b in grid if b < length - 0.05]
        out.append({"path": str(Path(g["path"]).expanduser()), "duration_s": data["duration_s"], "in_s": in_s,
                    "start_s": round(t, 4), "length_s": round(length, 4)})
        t += length
    return {"segments": out, "fade_out_s": float(segments[-1].get("fade_out_s", 1.0))}, beats


def _music_for(m: dict, cache_dir: Path) -> tuple[dict | None, list[float] | None]:
    from videtor.analyze import beats as beats_mod

    mu = m.get("music") or {}
    if isinstance(mu, list):
        return _medley(mu, cache_dir)
    if not mu.get("path"):
        return None, None
    data = beats_mod.load_or_track(Path(mu["path"]).expanduser(), cache_dir)
    in_s = float(mu.get("in_s") or 0.0)
    rel = [round(b - in_s, 3) for b in data["beats_s"] if b >= in_s - 0.05]
    return {"path": str(Path(mu["path"]).expanduser()), "duration_s": data["duration_s"], "in_s": in_s, "fade_out_s": mu.get("fade_out_s", 1.0)}, rel


@app.command()
def cut(
    shotmap_path: Path = typer.Argument(..., help="shotmap.json written from the brief."),
    mount: Path = typer.Argument(None, help="The drive whose index holds the project (optional: found from the shot map's key)."),
    out: Path = typer.Option(None, "--out", help="Output folder (default: next to the shot map)."),
    sheets: bool = typer.Option(True, "--sheets/--no-sheets", help="Write second-look contact sheets."),
    sfx: bool = typer.Option(True, "--sfx/--no-sfx", help="Place SFX in your style (kb/sfx.json) on their own tracks."),
    footmap_dir: Path = FOOTMAP_OPTION,
    kb_dir: Path = KB_OPTION,
    beats_dir: Path = typer.Option(Path("cache/beats"), "--beats"),
) -> None:
    """Cut from a shot map: fill every slot from the footage map; writes picks.json, the second-look sheets and the Premiere XML."""
    from videtor.analyze import tags
    from videtor.plan import benchmark, fill, second_look, shotmap

    m = shotmap.load(shotmap_path)
    out = out or shotmap_path.parent
    out.mkdir(parents=True, exist_ok=True)
    takes, n = _takes_for(_index_for_key(m["key"], kb_dir, mount), m["key"], footmap_dir)
    if not takes:
        console.print(f"No footage map for {m['key']!r} ({n} takes in the pool). Run: python -m videtor.analyze.footmap {mount} --key {m['key']!r}")
        raise typer.Exit(1)
    music, beats_rel = _music_for(m, beats_dir)
    times = shotmap.timing(m, beats_rel, float(m.get("fps", 25)))
    real = benchmark.load_real_metrics(kb_dir, m["key"])
    habits = fill.motion_habits(real["source_xml"]) if real and os.path.exists(real.get("source_xml", "")) else None
    from videtor.plan import taste as taste_mod

    learnt = taste_mod.load(kb_dir)
    if learnt and learnt.get("pairs"):
        console.print(f"applying your taste from {learnt['pairs']} fixes ({kb_dir / 'taste.json'})")
    keep = None
    if m.get("bucket"):
        from videtor.plan import keeper as keeper_mod

        model = keeper_mod.load(m["bucket"], kb_dir)
        if model:
            keep = keeper_mod.scores(model, takes, fill.Pool(takes), tags.embed_texts, kb_dir)
            console.print(f"keeper: what your {m['bucket']} edits keep (cv AUC {model['cv_auc']}, from {len(model['edits'])} edit(s))")
        else:
            console.print(f"keeper: no model for bucket {m['bucket']!r} yet (videtor kb keeper {m['bucket']})")
    picks = fill.fill(m, takes, times, tags.embed_texts, habits, taste=learnt, frames_dir=footmap_dir / "frames", keeper=keep)
    if m.get("sfx_skip"):
        picks["sfx_skip"] = list(m["sfx_skip"])
    for key in ("stabilize", "under_db"):
        if m.get(key) is not None:
            picks[key] = m[key]
    _level_bytes(picks, float(m.get("voice_lufs", -14.0)))
    if m.get("beds"):
        picks["beds"] = m["beds"]
    if m.get("flash"):
        from videtor.plan import moves

        picks["flashes"] = moves.flashes(picks["slots"], m["flash"], picks["fps"], picks["width"], picks["height"])
        console.print(f"flash frames: {len(picks['flashes'])} over the cuts" if picks["flashes"] else f"flash frames: cannot read {m['flash'].get('path')}")
    if music:
        picks["music"] = {**(m["music"] if isinstance(m.get("music"), dict) else {}), **music}
    if sfx:
        picks["sfx"] = _place_sfx(picks, kb_dir, beats_dir, footmap_dir)
        if picks["sfx"] is not None:
            console.print(f"SFX: {len(picks['sfx'])} placed in your style ({', '.join(sorted({s['kind'] for s in picks['sfx']})) or 'none'})")
    path = _write_cut(picks, out, music, footmap_dir)
    filled = sum(1 for s in picks["slots"] if s["pick"])
    console.print(f"{filled} of {len(picks['slots'])} slots filled from {len(takes)} mapped takes ({n} in the pool), {sum(l for _, l in times):.2f} s")
    if sheets:
        for p in second_look.sheets(picks, footmap_dir / "frames", out):
            console.print(f"second look: {p}")
        if (p := out / "match-cuts.jpg").exists():
            console.print(f"match cuts: {p}")
    console.print(f"Premiere XML: {path}")


def _write_interview(picks: dict, aroll: dict, m: dict, takes: list, out: Path, sfx: bool, kb_dir: Path, footmap_dir: Path) -> Path:
    """picks.json, the cut sheet and the Premiere XML of an interview piece (SFX placed again on every write)."""
    from videtor.export import xmeml_writer as w
    from videtor.kb import sfx as sfx_kb
    from videtor.plan import fill, interview, second_look

    placed = interview.layout(aroll)
    if aroll.get("finish", True):  # stabilise flags, ramps and zooms (plan/interview.finish_fx)
        interview.finish_fx(picks, m, {t.fid: t for t in takes}, fill.Pool(takes))
    habits = sfx_kb.load(kb_dir) if sfx else None
    picks["sfx"] = interview.place_sfx(m, picks, aroll, placed, habits) if habits else []
    gains = interview.voice_gains(placed, {t.fid: t.path for t in takes})
    W, H = int(aroll["width"]), int(aroll["height"])
    motion_for = lambda take, seg: fill.framing(take, W, H, None, (int(seg["in_s"] // 0.5), max(1, int(round((seg["out_s"] - seg["in_s"]) / 0.5)))))  # noqa: E731
    music = picks.get("music") if (picks.get("music") or {}).get("levels") else None
    tl = interview.build(aroll, placed, picks, m, {t.fid: t for t in takes}, music, picks["sfx"], gains, motion_for)
    picks["aroll"] = [{**s, "gain_db": gains.get(s["id"])} for s in placed]
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    second_look.cut_sheet(picks, footmap_dir / "frames", out / "cut-sheet.jpg")
    second_look.match_sheet(picks, footmap_dir / "frames", out / "match-cuts.jpg")
    return w.write_xmeml(tl, out / f"{aroll['name']}.xml", fps=float(aroll["fps"]), width=W, height=H)


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
    from videtor.analyze import tags
    from videtor.export import xmeml_writer as w
    from videtor.kb import sfx as sfx_kb
    from videtor.plan import fill, interview, second_look, shotmap
    from videtor.plan import taste as taste_mod

    aroll = json.loads(aroll_path.read_text())
    m = json.loads(shotmap_path.read_text())
    out = aroll_path.parent
    takes, n = _takes_for(_index_for_key(aroll["key"], kb_dir), aroll["key"], footmap_dir)
    by_fid = {t.fid: t for t in takes}
    interview_takes = set(aroll.get("interview_takes") or []) | {s["name"] for s in aroll["segments"]}
    broll = [t for t in takes if t.name not in interview_takes]
    placed = interview.layout(aroll)
    music, beats_rel = _music_for(m, beats_dir)
    times = interview.slot_times(m, aroll, placed, beats_rel)
    picks = fill.fill(m, broll, times, tags.embed_texts, None, taste=taste_mod.load(kb_dir), frames_dir=footmap_dir / "frames")
    spans = interview.regions(aroll, placed)
    if music:
        mu = m.get("music") or {}
        music["levels"] = interview.music_levels(float(aroll["length_s"]), spans["hook"][1], spans["end"][0], fps=float(aroll["fps"]),
                                                 up=float(mu.get("up_db", interview.MUSIC_UP_DB)), under=float(mu.get("under_db", interview.MUSIC_UNDER_DB)))
        picks["music"] = {**(m.get("music") or {}), **music}
    picks["source"] = {"aroll": str(aroll_path), "shotmap": str(shotmap_path), "sfx": sfx}
    path = _write_interview(picks, aroll, m, takes, out, sfx, kb_dir, footmap_dir)
    for p in second_look.sheets(picks, footmap_dir / "frames", out):
        console.print(f"second look: {p}")
    voice = sum(s["length_s"] for s in interview.layout(aroll))
    console.print(f"A-roll {len(placed)} segments, {voice:.1f} s of voice ({sum(1 for s in placed if s.get('visible'))} on screen) · "
                  f"B-roll {sum(1 for s in picks['slots'] if s['pick'])} of {len(picks['slots'])} slots from {len(broll)} takes · SFX {len(picks['sfx'])}")
    console.print(f"Premiere XML: {path}")


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
    from videtor.analyze import beats as beats_mod
    from videtor.analyze import sync as sync_mod
    from videtor.plan import musicvideo

    spec = json.loads(spec_path.read_text())
    out = spec_path.parent
    takes, n = _takes_for(_index_for_key(spec["key"], kb_dir), spec["key"], footmap_dir)
    data = beats_mod.load_or_track(Path(spec["music"]), beats_dir)
    synced = sync_mod.load_or_sync([{"fid": t.fid, "path": t.path, "basename": t.name} for t in takes if t.audio_channels], spec["music"], log=console.print)
    picks = musicvideo.plan(spec, takes, synced, data, log=console.print)
    picks["source"] = {"spec": str(spec_path)}
    path = _write_mv(picks, out, footmap_dir)
    used = {s["pick"]["fid"] for s in picks["slots"] if s.get("pick")}
    off = [s["pick"] for s in picks["slots"] if s.get("pick") and not s["pick"].get("synced", True)]
    away = sum(1 for p in off if p.get("filmed_at_song_s") is not None)
    console.print(f"{len(picks['slots'])} cuts on {data['tempo_bpm']:.0f} BPM ({len(used)} of {n} takes used; {len(picks['slots']) - len(off)} lip-synced, "
                  f"{away} cutaways of the performer not singing, moved by whole bars, {len(off) - away} inserts without playback)")
    for sec, d in picks["shares"].items():
        console.print(f"  {sec:10} {d['slots']:3} cuts  " + "  ".join(f"{k} {v:.0%}" for k, v in d.items() if k != "slots"))
    console.print(f"Premiere XML: {path}")


def _write_mv(picks: dict, out: Path, footmap_dir: Path = Path("cache/footmap")) -> Path:
    from videtor.export import xmeml_writer as w
    from videtor.plan import musicvideo
    from videtor.plan.build import build_from_picks

    picks["shares"] = musicvideo.shares(picks)
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    for p in musicvideo.pages(picks, footmap_dir / "frames", out):
        console.print(f"cut sheet: {p}")
    return w.write_xmeml(build_from_picks(picks, picks["music"]), out / f"{picks['name']}.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])


@app.command()
def look(
    picks_path: Path = typer.Argument(..., help="picks.json from `videtor cut`."),
    choices: list[str] = typer.Argument(None, help='Slot choices as id=k (runner-up 1..3), id=<take> (the runner-up from that take) or id=other-id:k (borrow), e.g. "details-3=2" "match-7=C1590" "reveal-2=reveal-1:1"; none rewrites the XML as is.'),
    note: list[str] = typer.Option([], "--note", help='Why, as id=text, e.g. "details-3=face sharper".'),
) -> None:
    """Apply second-look choices to picks.json and rewrite the Premiere XML."""
    from videtor.plan import second_look

    picks = json.loads(picks_path.read_text())
    parse = lambda v: (v.rsplit(":", 1)[0], int(v.rsplit(":", 1)[1])) if ":" in v else int(v) if v.isdigit() else v  # noqa: E731
    chosen = {c.split("=", 1)[0]: parse(c.split("=", 1)[1]) for c in (choices or [])}
    notes = {c.split("=", 1)[0]: c.split("=", 1)[1] for c in note}
    if picks.get("kind") == "music-video" and any(isinstance(v, tuple) for v in chosen.values()):
        console.print("A music video only swaps to a slot's own runners-up (id=k): a shot borrowed from another slot would be out of sync.")
        raise typer.Exit(1)
    second_look.apply(picks, chosen, notes)
    if picks.get("kind") == "music-video":
        path = _write_mv(picks, picks_path.parent)
        console.print(f"{len(chosen)} slots swapped · Premiere XML: {path}")
        return
    if (picks.get("source") or {}).get("aroll"):  # an interview piece: rebuild it the interview way
        src = picks["source"]
        aroll, m = json.loads(Path(src["aroll"]).read_text()), json.loads(Path(src["shotmap"]).read_text())
        takes, _ = _takes_for(_index_for_key(aroll["key"], KB_DIR_DEFAULT), aroll["key"], Path("cache/footmap"))
        path = _write_interview(picks, aroll, m, takes, picks_path.parent, src.get("sfx", True), KB_DIR_DEFAULT, Path("cache/footmap"))
        console.print(f"{len(chosen)} slots swapped · Premiere XML: {path}")
        return
    mu = picks.get("music")
    if picks.get("sfx") is not None:  # shutter clicks follow the picked shots' flashes: place again
        picks["sfx"] = _place_sfx(picks, KB_DIR_DEFAULT, Path("cache/beats"), Path("cache/footmap"))
    path = _write_cut(picks, picks_path.parent, mu if mu and (mu.get("duration_s") or mu.get("segments")) else None)
    console.print(f"{len(chosen)} slots swapped · Premiere XML: {path}")


if __name__ == "__main__":
    app()
