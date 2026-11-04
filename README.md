# Videtor

Point it at a folder of raw footage. It indexes and analyzes every clip, consults a
knowledge base built from your past Premiere Pro edits, plans a rough cut and a sound
bed, and writes a timeline that opens in Premiere Pro or DaVinci Resolve. You finish
the edit in the NLE; when you re-export the finished sequence, the knowledge base
learns from what you changed.

Docs:
- `docs/handoff.md`: where a new Claude session (Claude Code or Desktop) starts. It sets up the skill and
  lists what to read.
- `docs/status.md`: where every project stands, and what's pending.
- `docs/architecture.md`: how it works, as built, with the verified Premiere and footage facts.
- `CLAUDE.md`: notes for agents working in this repo.
- `docs/architecture.html`: the original design outline, GitHub survey and phased plan (2026-10-29).

## Layout

```
src/videtor/
  cli.py            videtor <command>
  mcp_server.py     the brief-to-cut steps as MCP tools (Claude Desktop, Claude Code)
  kb/               past edits: xmeml reader (stdlib only), metrics, profiles per bucket, footage index
                    (used vs unused), selects, SFX habits, edit breakdowns, read-back of a fixed cut;
                    js/ holds the Premiere bridge scripts
  analyze/          per clip and per second: frames, audio, motion, faces, CLIP tags, footage map,
                    orientation, beats, speech (local Whisper), lip-sync audio sync
  plan/             shot map, filler, keeper, taste, editing moves, music, SFX, steadying, interview,
                    music video, second look, sheets, overview, timeline build; planner v0 and benchmark
  export/           Premiere xmeml writer
kb/
  projects/         one JSON per ingested sequence
  profile/          one JSON per bucket (edit type): brand-social, sports-promo, event
  footage/          footage indexes: one per drive (past edits) and one per prepared folder
  keeper/ buckets/  what each bucket's edits keep; the cards Claude reads before planning
  briefs/ marks/ feedback/   briefs for past edits, Source Monitor marks, read-back labels
  manifest.json     bucket per sequence; extra footage folders per sequence
  notes.md          rules in prose, per bucket
  sfx.json          your sound design habits
.xml-exports/       your Premiere exports, the knowledge base's source
cache/              derived data (gitignored): frames, footage maps, analysis, beats, sync audio,
                    transcripts, steadying measures, models
out/<name>/         one folder per cut (gitignored): brief, shot map, picks, sheets, XML, report.md
scripts/            helpers beside the CLI and archived experiments (scripts/README.md)
docs/               status, architecture, the original design page
tests/              unittest suite with synthetic fixtures
```

## Setup

```sh
brew install ffmpeg uv
uv sync                   # core: OpenTimelineIO, CLI
uv sync --extra analyze   # Phase 1: scene detection, Whisper, librosa, loudness
scripts/setup-claude-code.sh   # /cut-from-brief in every Claude Code session, plus the auto-push hook
```

Claude Code works in the terminal, in Claude Code desktop and in VS Code.
- `scripts/setup-claude-code.sh` links the skill into `~/.claude/skills/`, so `/cut-from-brief` works whatever
  folder a session opens in. The skill always runs Videtor from this checkout.
- It also adds the Stop hook that commits and pushes every change (`scripts/hooks/autopush.sh`).
- In desktop, open this folder and leave Worktree off for deliveries. A Worktree session's own edits go to
  its branch on GitHub, while the footage maps and outputs stay in the main checkout.

## Knowledge base

Export each past sequence from Premiere with **File ▸ Export ▸ Final Cut Pro XML**, then:

```sh
uv run videtor kb ingest ~/Exports/            # folders or individual .xml files
uv run videtor kb show
```

Each sequence lands in `kb/projects/` and is rolled up into `kb/profile/<type>.json`.
Sequences are typed `unsorted` until you name their edit type in `kb/manifest.json`:

```json
{ "default": "unsorted",
  "types": { "brand-teaser-v3.xml::Teaser 90s": "brand-film",
             "podcast-ep12.xml::EP12 final": "talking-head" } }
```

Re-run `kb ingest` after editing the manifest; the roll-up is recomputed from everything
in `kb/projects/`.

## Footage: what you left out

An export only records what you kept. With the drive that holds a project's raw footage
connected, the footage index records everything else in the project folder, so the
planner can later learn what you discard:

```sh
uv run videtor kb footage /Volumes/Paritosh_1     # ffprobe every media file, mark used clips + ranges
uv run videtor kb frames  /Volumes/Paritosh_1     # 3 frames per clip + 1 per used range → cache/frames/
uv run videtor kb status                          # per drive: coverage, and whether it is safe to unplug
```

The project folder is inferred as the deepest folder holding most of the clips an edit
used. When a project spans more folders than its XML points at, name them in the
manifest and the scan includes them:

```json
{ "footage": { "Gujarat Giants.xml::Sequence 01": ["/Volumes/AMAZONSSD/WPL Gujarat Giants"] } }
```

Premiere/After Effects caches, camera thumbnail folders and render/export folders are
kept out; byte-identical copies, files saved twice with a `(1)` or ` copy` suffix and
Premiere's trimmed consolidated copies are flagged as duplicates and never counted twice. `kb footage <drive> --rematch` recomputes
used/unused and duplicates from the saved index without the drive.

## Analysis, planning, benchmark

```sh
uv run videtor analyze /Volumes/Paritosh_1 --features frames,audio,motion   # per clip, cached in cache/analysis/
uv run videtor analyze /Volumes/Paritosh_1 --features faces,tags            # from the sampled frames; no drive needed
uv run videtor plan /Volumes/Paritosh_1 --key "Guru.xml::Sequence 01" --type brand-social \
    --duration 20 --pace fast --music ~/Music/track.wav --speech favor --out out/guru
uv run videtor kb benchmark /Volumes/Paritosh_1 --key "Guru.xml::Sequence 01" --plan out/guru/plan.json
uv run videtor kb selects /Volumes/Paritosh_1     # used vs unused clips, feature medians
uv run videtor kb rerank-eval                      # learned reranker vs hand rules, leave-one-project-out
uv run videtor export out/guru/plan.json --to otio # .otio for Resolve; Premiere XML is written by plan
```

`plan` writes `plan.json` (a reason per shot) and the Premiere XML; cuts land 120 ms
ahead of the music's beats (`--beat-lead`), `--opening` builds the first beats from
face and tag features (`glimpse`: the subject in part, two detail teases, the
zoom-out reveal; `tease`: details then the reveal; `subject`: open on the subject;
`top`: the strongest shot; `none`: shoot order), and `--speech favor|avoid|neutral`
says whether talking clips belong in the piece. `kb benchmark` scores the plan's
opening against your real edit's under the same template. A Premiere Pro MCP (`prpr`, bridge plugin inside Premiere) is registered
in `.mcp.json` for inspecting sequences from Claude Code.

## Cutting from a brief

You write what the piece is for; Claude turns it into a shot map; Videtor fills every
slot from a per-second map of the footage and writes the cut with alternates. In Claude
Code, `/cut-from-brief` (`.claude/skills/cut-from-brief/SKILL.md`) runs the whole loop.

```sh
python -m videtor.analyze.footmap /Volumes/Paritosh_1 --key "Guru.xml::Sequence 01"  # 2 samples/s of every take
uv run videtor music ~/Downloads/music --length 25 --reveal-at 8.5     # tracks ranked; in-point lands a drop on the reveal
uv run videtor search /Volumes/Paritosh_1 "rings on his fingers, close-up" --key "Guru.xml::Sequence 01"
uv run videtor cut out/guru-25s-brief/shotmap.json /Volumes/Paritosh_1   # picks.json, second-look sheets, Premiere XML
uv run videtor look out/guru-25s-brief/picks.json details-3=2 --note "details-3=sharper"  # apply second-look swaps
```

The shot map (`src/videtor/plan/shotmap.py` documents the schema) lists sections and
slots; each slot says what it must show (CLIP text search), how many beats it lasts,
and soft filters for scale, face, hands and camera movement, with `speed: 0.5` for slow
motion on 50/100 fps takes. A section marked `"match": true` is a run of match cuts: its
takes are chosen together so the frame each shot ends on lines up with the next one's
first (grey levels and edges on a 48×27 grid, and the camera moving the same way), and
`match-cuts.jpg` shows every cut as a pair. A slot's `takes` limits it to named takes or
moments (`"C1777*@0.5-3.5"`), `skip` leaves takes out by path pattern, `sfx_skip` leaves
out SFX kinds, and `"role": "reveal"` names the slot the riser and impact build to.
`videtor look picks.json match-7=C1590` swaps to a runner-up by its take. The cut puts each pick on V1 and two alternates on V2–V3,
switched off; scale and rotation follow your own Basic Motion for each camera type in
the project's past edit (rotated 4K at 50%, sideways SD camcorder at −90° and 188%),
otherwise the scale that fills the frame. Camera audio stays out; the music sits on A1
from its in-point with a fade-out. `kb/briefs/` holds briefs written for past edits
(the benchmark), `kb/marks/` your Source Monitor in/out marks read from the project.

After you fix a cut in Premiere, read it back (live over the bridge, or from an XML
export) and every slot is labelled kept, slipped, trimmed, swapped to an alternate,
swapped or removed in `kb/feedback/`; `kb learn` turns those fixes into corrections
to the filler's weights (`kb/taste.json`), which the next `videtor cut` applies:

```sh
.venv/bin/python -m videtor.kb.readback out/guru-25s-brief/picks.json --live
uv run videtor kb learn
```

### Buckets: what each kind of work is like

Every edit in `kb/manifest.json` has a bucket (its edit type: `event`, `brand-social`, `sports-promo`, ...).
Per bucket, `kb/profile/<bucket>.json` holds the numbers, `kb keeper` learns what your edits keep against
the footage they left out (generic concepts such as "a person smiling at the camera" plus faces, scale,
focus and camera movement; needs the raw footage mapped), and `kb bucket` writes the card Claude reads
before planning a brief of that bucket:

```sh
uv run videtor kb breakdown ".xml-exports/Diwali Party.xml" --footage "/Volumes/Paritosh_2/Diwali Party/Data"
uv run videtor kb keeper event        # kb/keeper/event.json: weights, cross-validated AUC
uv run videtor kb bucket event        # kb/buckets/event.md: numbers, what gets kept, rules, each edit's breakdown
```

A shot map that names its `bucket` gets the keeper score on every slot. Feed a bucket with 3-5 of your
edits (the XML export, the raw footage folder, a line of brief); each one sharpens its numbers and,
with the footage, what it keeps.

### Editing moves

Slots can `ramp` ("arrival" 100% → 50% → 25%, "punch" 140% → 50% → 130%, or your own parts), `flicker`
(sub-shots of a few frames from different takes, a click on each cut), `stack` (smaller shots popping on
top every 5 frames, with clicks) or be `black`; `beats` may be half beats. The shot map's `music` can be a
medley of segments with silent gaps, `flash` lays a flash clip over cuts, and `beds` run ambience under
part of the cut (`src/videtor/plan/moves.py`).

A `"byte": true` slot is a person speaking (a vox pop): it plays its moment from the transcript
(`"takes": ["A011C045*@59.82-62.26"]`) with its own sound on A1, levelled to the map's `voice_lufs`, the
music dipped to `under_db` in two steps around it and no SFX on it. `"stabilize": true` on the shot map
steadies the handheld V1 shots of half a second or more inside the XML (`src/videtor/plan/stabilize.py`): the shake is measured from
the frames (drive connected) and cancelled with a Position and Rotation keyframe on every frame, plus the
small zoom (at most 10%) that keeps the edges out; delete the keyframes to undo. A file the index marks
`"excluded"` (another shoot left on the same card) stays out of the pool.

### Interview pieces

```sh
.venv/bin/python -m videtor.analyze.speech kb/footage/<name>.json --key "<name>::footage" --prompt "Brand, Product names"
# Claude reads the transcripts, writes out/<name>/aroll.json (the paper edit) and a B-roll shot map whose
# sections name the region they fill: "region": "hook" | "end", or "over": ["a2", "a3"]
uv run videtor cut-interview out/<name>/aroll.json out/<name>/shotmap.json
uv run videtor finish out/<name>/picks.json --lut "~/Downloads/LUTs/S-Log3 SV Vision3.cube"   # after importing the XML
```

Transcripts come from local Whisper (mlx-whisper large-v3-turbo, `cache/models/`), with word timings, so
the paper edit can cut fillers, restarts and pauses; a gaze score tells talking-to-camera stretches from
reading. The cut lays the voice on V1/A1 (levelled to −16 LUFS), B-roll over it on V2, the hook and the
ending on V1, the music dipped under the voice in short steps, SFX below. The finishing pass stabilises
handheld shots (flagged for Warp Stabilizer, played at 100% so it runs without nesting), ramps about half
the others (4 frames at 100%, 50%, 4 frames at 110%) and keyframes a 5–10% zoom on every clip; `finish`
applies the LUT, a Lumetri grade and Warp Stabilizer inside the running Premiere.

### Music videos (lip sync)

```sh
.venv/bin/python -m videtor.analyze.sync kb/footage/<name>.json --key "<name>::footage" --music "/path/song.wav"
# Claude labels who is in each take and writes out/<name>/mv.json (sections on the song's bars, who sings each)
uv run videtor cut-mv out/<name>/mv.json                       # picks.json, cut-*.jpg sheets, Premiere XML
uv run videtor look out/<name>/picks.json verse-b-12=1         # swap to a slot's own runner-up (stays in sync)
```

Every take's camera audio (the playback the mic heard) is lined up with the master: 8 s windows of an
onset envelope are matched against the whole song, windows that agree on one offset make a synced stretch,
and the local correlation at that offset (0.6–0.9 with the playback audible) says where it starts and stops,
so a take where the song was restarted syncs piece by piece; a take filmed to double-speed playback syncs at
50%. The audio is copied to `cache/sync/audio/`, so a rerun needs no drive. `cut-mv` cuts on the beat grid
(a section every 1, 2 or 4 beats, single-beat stutters on the last half bar of each phrase, a hold that
introduces each singer) and gives every slot the best synced take that holds it: the in-point comes from the
sync, so lips land on the words wherever the cut falls. A beam search keeps the singer's share per section,
never repeats a take back to back and spreads the looks. The other performer also comes from his own takes
where he is not rapping, moved by whole bars so he moves on the same beat; takes without playback only go
where nobody sings, slowed to 50% from 50/100 fps.

### Footage you have never edited

```sh
uv run videtor prep "/Volumes/Drive/Shoot" --name nike-teaser   # index, stills, orientation, footage map, overview
# read out/nike-teaser/overview.md and overview-*.jpg, write out/nike-teaser/shotmap.json (key "nike-teaser::footage")
uv run videtor cut out/nike-teaser/shotmap.json                  # picks, second-look sheets, cut sheet, Premiere XML
```

`prep` gives the folder its own index (`kb/footage/<name>.json`), so a drive's
existing index is never touched; it says when the drive can be unplugged (after the
footage map's decode). Cameras held sideways are found from the stills (faces only
appear once the frame is turned) and turned upright before analysis and in the cut; a
sideways camera that shot nothing with a face stays as shot, so check the overview and
fix it with `videtor orient <name> --camera <prefix> --rotate -90` (or `--take <file>`);
landscape takes in a vertical piece are cropped around the face in the chosen window
(Basic Motion centre: offset ÷ 3840 and ÷ 2160 on a 1080×1920 sequence, verified
against Premiere's own stored positions).

Helpers for reading a big shoot sit in `scripts/`:
- `stills_sheets.py` sheets every take's middle still while the footage map builds;
- `search_sheet.py` runs several searches at once and sheets the hits;
- `cinematic_rank.py` shortlists a music library for a cinematic piece when the brief leaves the music to
  Claude;
- `transcribe_bytes.py` transcribes an interview camera for people bytes.

`videtor.mcp_server` exposes the same steps as MCP tools (projects, music, search, shot
map check, cut, second look, score, read-back) for Claude Desktop and Claude Code; the
repo's `.mcp.json` registers it next to `prpr`.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

The parser and metrics use only the standard library, so the tests also run on the
system Python: `PYTHONPATH=src python3 -m unittest discover -s tests`.
