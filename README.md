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
  cli.py            videtor <command>: thin callers of the pipeline
  pipeline.py       the steps of a job in one place: prepare, cut, look, read back, learn (every front door calls these)
  events.py         what a running step reports: sentences in the terminal, JSON lines for the app
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

## The UI

```sh
scripts/make-app.sh               # once: makes ~/Applications/Videtor.app, a double-clickable app (drag it to the Dock)
uv run videtor ui                 # the same from a terminal; --browser for a browser tab, --port 8800 if 8765 is taken
uv sync --extra analyze --extra ui   # the native window (pywebview over the system WebKit); the app does this on its first run
```

Videtor.app is a launcher made on this Mac, so Gatekeeper lets it run: it opens the local page in its own window
(the browser if the window package is missing), installs that package on first run, and logs to
`~/Library/Logs/Videtor.log`. Opening it twice shows the one already running. Nothing but the standard library
serves the page (`videtor.ui.server`), and every action is `videtor <command>` run as its own process
(`videtor.ui.jobs`), so the page and the terminal share one code path. Prep and cut are two processes: a cut gets
the machine (running preps and library builds are paused and resumed after it; a prep started during a cut
waits), and a prep paused into failures is run again by itself, since every pass is resumable. Every Choose
button opens the Mac's own folder dialog, in the window and in the browser alike (AppleScript's `choose folder`
when there is no window). The Prep screen takes the footage folder and where the project goes (it lands in
`<output>/<name>/`); the Project screen shows the overview and shot types, holds the
brief and the shot map (validated on save), ranks music, searches the footage, cuts, and does the second look by
clicking a runner-up; the Library screen builds a library, shows its sheets and clips, moves a clip to another
class and learns from the folders; Jobs shows every process with pause, resume and cancel. Every page and tab
opens with a "How this works" guide (the steps in order, what to expect, how long it takes) that stays open until
it is closed, and the Prep page carries the five-step flow: prep, brief and shot map, cut, second look, Premiere.
State lives in
`cache/ui-state.json`; stills and sheets are served only from the repo's cache and the folders you chose.

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

## Analysis

```sh
uv run videtor analyze /Volumes/Paritosh_1 --features frames,audio,motion   # per clip, cached in cache/analysis/
uv run videtor analyze /Volumes/Paritosh_1 --features faces,tags            # from the sampled frames; no drive needed
uv run videtor kb selects /Volumes/Paritosh_1     # used vs unused clips, feature medians
```

A Premiere Pro MCP (`prpr`, bridge plugin inside Premiere) is registered in `.mcp.json` for inspecting sequences
from Claude Code. The rule-based planner of rounds 1-3 (`videtor plan`, `export`, `kb benchmark`, `kb rerank-eval`)
is archived in `scripts/archive/rounds-1-3/`.

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

### After the job: feedback, learning and the evaluation

When the job is finished, read it back. The finished video is enough: save the export in the project's folder
(or name it) and every slot is labelled kept, slipped, trimmed, swapped to a runner-up, swapped or removed. An XML
export or Premiere live work too. It all lands in `kb/feedback/<name>.json`, with the runners-up you chose in a
second look (`videtor look ... --by you`, or a click in the app) and your verdict on the cut:

```sh
uv run videtor readback out/<name>/picks.json                    # the newest video in out/<name>/ (not preview.mp4)
uv run videtor readback out/<name>/picks.json --render ~/Movies/final.mp4   # or --xml <export>, or --live
uv run videtor verdict out/<name>/picks.json not --note "too slow in the middle"
uv run videtor learn                                             # taste and keeper, tried on the evaluation set first
uv run videtor eval                                              # the past projects cut again from the cache and measured
uv run videtor preview out/<name>/picks.json                     # a half-size video of the cut to watch without Premiere
```

- `learn` fits your taste (the windows you chose over the picks; from 12 choices on, per bucket from 20) and, for
  every bucket with a finished job read back, what you keep. A candidate is kept only when `eval` says no guarded
  measure got worse on the past projects (`--force` keeps it anyway).
- `eval` cuts every project of `kb/eval/set.json` exactly as `videtor cut` does (no drive, nothing written) and
  measures it against your own edit where there is one (right take, right second, in the top 3) and on its own
  (repeats, look-alike neighbours, jumps in light, how well the picks match). `--save` keeps the run as the
  baseline in `kb/eval/baseline.json`. Run it after any change to how shots are chosen.
- A second look is remembered: `videtor look` writes `decisions.json` next to the picks and the next
  `videtor cut` puts the swaps back (`--fresh` forgets them).

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

### Learning a style from rendered reels (the sports library)

When only the finished renders exist (no Premiere project, no raw footage), Videtor learns a bucket's style from
the picture and sound alone, offline:

```sh
uv run videtor kb renders ~/Downloads/Exports/Videos --bucket sports-promo   # about a minute for eight reels, every core
#   kb/renders/sports-promo.json        the style: pace, transitions, black and white, moves, grade, beats, what is shown where (no file names)
#   out/references/sports-promo/        each reel read in full (shots, transitions, beats) and a contact sheet of its shots (local)
uv run videtor kb train sports-promo    # the cutting policy, tested on each reel held out in turn: kb/policy/sports-promo.json
uv run videtor draft hype-30 --length 30 --music "<track>"   # a first shot map from that policy and from what the shoot holds
                                                             # (inventory.json, written by prep): edit it, then cut
uv run videtor fx-test "<any video>"    # out/fx-test/fx-test.xml: one clip per effect, a marker naming each, to check them in Premiere
```

`kb renders` finds every cut and the frames between shots that belong to none (a recurring static overlay, a
white flash, black), each shot's length, look (black and white) and move (push-in, pull-out, spin, whip,
shake), the grade, the beat grid and how the cuts sit on it (on accents, not the grid), and names what each
shot shows with CLIP (`src/videtor/kb/render.py`). `kb train` (`src/videtor/plan/gym.py`) uses each reel's own
shots as stand-in footage: a policy (shot lengths through the runtime, holds, flicker bursts, snapping cuts to
the music's accents, effect rates, black-and-white runs, what is shown where) cuts a reel from them; a reward
scores how close the cut's style is to the real reel; the cross-entropy method tunes the policy, and each reel
held out in turn shows how well it does on a reel it has not seen. A shot map then asks for effects with
`"effects": "sports-promo"` and cuts on accents with `"cut_on": "accents"`.

The effects (`src/videtor/plan/fx.py`) use only what Premiere's FCP XML import translates: Basic Motion keyframes
(push-in, pull-out, punch, shake, the slide of a whip, the turn of a spin), Gaussian Blur keyframes (whip, spin,
blur-in), Opacity and blend modes (an echo three frames behind on a track above, lighten), and overlay clips over
the cut (a 3-frame white flash with its own alpha, 3 frames of film-scan static: generated in `cache/assets`, or
your own clips via `"effects": {"style": "sports-promo", "assets": {"static": "...", "flash": "..."}}`). Black and
white has no filter known to survive the import: the clips carry the Iris label and the cut prints the runs to
set by hand; `fx-test` tries three variants.

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
uv run videtor prep "/Volumes/Drive/Shoot" --name nike-teaser   # index, keyframe map, orientation, shot types, overview: minutes, all cores
# read out/nike-teaser/overview.md and overview-*.jpg, write out/nike-teaser/shotmap.json (key "nike-teaser::footage"),
# naming the classes it draws on ("classes": ["close-up", "b-roll"], or a slot's "class")
uv run videtor cut out/nike-teaser/shotmap.json                  # decodes those takes in full (drive connected), then picks, sheets, Premiere XML
uv run videtor prep "/Volumes/Drive/Shoot" --name nike-teaser --dense   # the old way: map every second of every take now (hours)
```

`prep` reads every take from its keyframes (`videtor.analyze.sparse`, the same pass
the library uses: faces, hands, speech, CLIP, a motion proxy), sorts the takes into
the library's shot types (`out/<name>/classes.json`, with your labels and model from
`kb/library/` when they exist) and writes the overview, in minutes. The full footage
map (2 samples a second with camera motion) is built by `cut`, only for the takes the
shot map's classes allow and the ones its slots name, so the drive stays connected
until the cut; a map that names no class decodes everything, as before. `search`
works on the keyframe map until then (moments to the nearest keyframe, camera
unknown). `prep` gives the folder its own index (`kb/footage/<name>.json`), so a
drive's existing index is never touched. Cameras held sideways are found from the stills (faces only
appear once the frame is turned) and turned upright before analysis and in the cut; a
sideways camera that shot nothing with a face stays as shot, so check the overview and
fix it with `videtor orient <name> --camera <prefix> --rotate -90` (or `--take <file>`);
landscape takes in a vertical piece are cropped around the face in the chosen window
(Basic Motion centre: offset ÷ 3840 and ÷ 2160 on a 1080×1920 sequence, verified
against Premiere's own stored positions).

### A shoot sorted by shot type (the library)

```sh
uv run videtor library build "/Volumes/Drive/Shoot" "/Volumes/Drive/Interviews" --name shoot   # minutes, every core, no Claude
#   out/library/shoot/{wide,close-up,talking-head,interview-bite,b-roll}/   links to the clips (--copy for copies)
#   out/library/shoot/sheets/<class>-N.jpg                                   every clip of a class on one page
#   out/library/shoot/interview-bite/bites.xml + bites.md                    every bite as a Premiere sequence (--extract: files too)
#   out/library/shoot/library.md                                             counts, how each class was decided, the ones to check
# move the wrong links into the right folders (Finder or mv), then
uv run videtor library learn out/library/shoot      # keeps your labels (kb/library/labels.json), fits a model, reports its accuracy
uv run videtor library build ... --name shoot        # sorts with the model; your labelled clips stay where you put them
uv run videtor library status out/library/shoot     # what moved since the build
```

The library reads each clip from its keyframes alone (`videtor.analyze.sparse`: at most 24 stills, faces
and hands, speech, how much the picture shifts between stills, CLIP), which is 8-10× cheaper than the
footage map's full decode and runs on every core (`VIDETOR_WORKERS` caps it). The rules: someone speaking
to a locked-off camera is a **talking head**; speaking with the camera moving (a vox pop, a stage mic) an
**interview bite**; a face filling the frame or a tight detail a **close-up**; a scene with faces small or
none a **wide**; the rest **B-roll**. The speaker clips are transcribed (local Whisper, cached) and their
sentences become the bites; a speaker clip whose transcript has no words is demoted. The folders are the
training data: `learn` fits a logistic regression on the sparse features from wherever you put the links.

Helpers for reading a big shoot sit in `scripts/`:
- `stills_sheets.py` sheets every take's middle still while the footage map builds;
- `search_sheet.py` runs several searches at once and sheets the hits;
- `cinematic_rank.py` shortlists a music library for a cinematic piece when the brief leaves the music to
  Claude;
- `transcribe_bytes.py` transcribes an interview camera for people bytes.

The MCP server (`videtor.mcp_server`) was retired on 2026-11-11: it was a Round 4 copy of the cut that the app
replaced. It is in `scripts/archive/` with its test.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

The parser and metrics use only the standard library, so the tests also run on the
system Python: `PYTHONPATH=src python3 -m unittest discover -s tests`.
