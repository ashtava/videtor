# Videtor: notes for agents

Videtor is a local tool that cuts Premiere Pro timelines (FCP7 xmeml) from a folder of raw footage and a written
brief, in the user's own style, learned from their past Premiere edits. DaVinci Resolve comes later. The user,
Paritosh, is a video editor in India (IST): brand/social reels, events, interviews, music videos, sports
promos.

**New session, or a new Claude account** (the user switched on 2026-11-04)? Follow `docs/handoff.md` first.
It sets up the skill on whichever platform you're running on (Claude Code terminal or desktop, Desktop chat,
the cloud), and says what to read and how to reply.

## Read before working

0. `docs/handoff.md` when the session is new.
1. `docs/status.md`: where every project stands, what the user owes us and what we owe them, the working
   agreements, the backlog. **Update it after every delivery, verdict or build.**
2. `docs/architecture.md`: the modules, files and formats, and the verified Premiere and footage facts. Don't
   re-derive those.
3. `README.md`: commands. `scripts/README.md`: helpers beside the CLI.
4. For a brief: the `/cut-from-brief` skill (`.claude/skills/cut-from-brief/SKILL.md`). Then the bucket's
   card `kb/buckets/<bucket>.md` and `kb/notes.md`.
5. For a past project: `out/<name>/report.md` (what was delivered, how to rebuild it, the verdict).

## The user's rules

- **GitHub, with auto-push** (the user's instruction, 2026-11-01).
  - The repo is `git@github.com:ashtava/videtor.git`, branch `main`. It is **public**.
  - A Stop hook (`scripts/hooks/autopush.sh`, set up in `.claude/settings.local.json`) commits and pushes
    every change at the end of each Claude turn.
  - `out/` and `cache/` are gitignored and stay local. So keep people's names, client details, sheets
    and footage stills there, never in tracked files.
  - Never rewrite pushed history. The hook skips a merge or rebase in progress and refuses leftover
    conflict markers.
- **Ask before any install,** including model downloads.
- **Footage stays on the Mac.** Claude may look at stills and contact sheets (allowed 2026-10-29); say when
  you do. Never send footage to a cloud service.
- **Blind tests stay blind:** in a project folder, don't open the user's own edit (`pf/`, `render/`, exported
  `.mp4`s, their music, `untitled folder`) until they give it. The per-project lists are in `docs/status.md`.
- **Premiere** (prpr bridge, `.mcp.json`):
  - Say so before reading it live; the user may be working in another project.
  - Use read-only scripts.
  - Never bulk-insert effects: it crashed Premiere 26.0.2 twice. `videtor finish` prints manual steps unless
    `--force-bridge`.
- **Never edit the Claude Desktop config;** give the snippet.
- **Say plainly when a drive is safe to unplug.**
- **No code changes during a delivery.** Cut with what exists, and list the gaps in `docs/status.md` (the user
  asked on 2026-11-01 why the code kept changing with every project). Build between projects. The exception
  is a brief that explicitly needs a feature; then say so.
- **After a delivery,** write `out/<name>/report.md`: the report as sent, the rebuild commands with every
  `look` swap and note, and the verdict when it comes. Update `docs/status.md` too.

## Environment

```sh
cd ~/workspace/videtor && export PATH=/opt/homebrew/bin:$PATH   # ffmpeg, uv
.venv/bin/videtor --help                                         # Python 3.11 venv, installed editable
.venv/bin/python -m unittest discover -s tests                   # 328 tests, about 30 s, no drive needed
.venv/bin/videtor eval                                           # the past projects re-cut from the cache and measured (about 1 min)
```

- **Machine:** M2 Pro, 16 GB, macOS 26.3.1. Premiere Pro 26.0.2 (UXP), Resolve 19.1.4 Studio.
- **Footage** is on external drives under `/Volumes` (the map is in `docs/status.md`):
  - `prep` (keyframes, minutes) and the first `cut` of a shot map (it decodes the takes the map's classes
    allow in full, plus the steadying pass) need the drive connected;
  - the XML needs it to play;
  - a re-cut and `look` otherwise run from `cache/`.
- **Slow decode:** Sony XAVC 4:2:2 10-bit decodes in software (about real time per worker, and CPU-bound: more
  workers don't help), so a big shoot's `prep` takes 30-60 min. Run it in the background and read stills meanwhile
  (`scripts/stills_sheets.py`). `videtor library build "<folder>" --name <name>` reads a shoot from its keyframes
  alone in minutes (all cores, no Claude) and sorts it by shot type; the footage map is still needed to cut.
- **Parallel passes** are sized by `analyze/parallel.py` (performance cores, memory); `VIDETOR_WORKERS=4` lowers
  it when the Mac is busy with Premiere.
- **The UI** (2026-11-07): `videtor ui`, or `~/Applications/Videtor.app` (made by `scripts/make-app.sh`; remake
  it if the repo moves), serves a local page on this Mac that runs every step as its own `videtor` process (a cut
  pauses preps and gets the machine). Folder pickers are the Mac's own dialog. The window needs pywebview
  (`uv sync --extra analyze --extra ui`), which the app installs on its first run; without it the page opens in
  the browser. The user chose this over the MCP server, and it is for a non-technical end user: keep its words
  plain and its steps few.
- **Styles from renders** (2026-11-11): `videtor kb renders <folder> --bucket <b>` learns a bucket from finished
  reels alone and `videtor kb train <b>` a cutting policy from them (`plan/gym.py`); `videtor draft` drafts a shot
  map from it; a shot map's `"effects": "<b>"` adds the learnt effects (`plan/fx.py`) and `"cut_on": "accents"`
  cuts on the music's hits. The sports reels' file names (a client's) stay in `out/references/`, never in kb/.
- **The pipeline and the loop** (2026-11-11, after `docs/review-2026-11-11.md`):
  - Every step lives in `src/videtor/pipeline.py`; the terminal and the app only call it. A new step goes there,
    with a test in `tests/test_pipeline.py` that runs it end to end. Steps report through `events.py`, never `print`.
  - **After any change to how shots are chosen, run `videtor eval`.** It fails when a guarded measure is worse
    than `kb/eval/baseline.json`; `--save` only when the change is meant and the numbers are better or equal.
  - Feedback: `videtor readback <picks.json>` reads the finished video (or `--xml`, `--live`) against the picks,
    `videtor verdict`, and a swap made by a person (`look --by you`, the app) all go to `kb/feedback/<name>.json`.
    `videtor learn` fits taste and keeper from it and keeps them only if `eval` passes.
  - `prep` writes `inventory.json` (what the shoot holds); `videtor draft` asks only for that. The cut settles
    neighbours (repeats, look-alikes, day cut to night) and runs an automatic second look (`plan/autolook.py`);
    Claude's second look is for what needs eyes.
- **Claude Code desktop** (set up 2026-11-04) reads the same `~/.claude` and project settings as the CLI.
  - `scripts/setup-claude-code.sh` links the skill into `~/.claude/skills/`, so `/cut-from-brief` works from
    any folder (checked in a session opened outside the repo), and makes sure the auto-push hook is set up.
  - Prefer sessions opened on this folder with Worktree off (the desktop default).
  - A Worktree session (`.claude/worktrees/<name>`, gitignored) still runs every Videtor command from the
    main checkout, and the hook pushes its own edits to its branch.
  - Hooks wait for the folder-trust prompt.
  - The project's `.mcp.json` server (prpr) needs approving per session; the skill doesn't use it. Videtor's own MCP
    server was retired on 2026-11-11 (`scripts/archive/`).

## Layout

```
src/videtor/  cli.py (thin) · pipeline.py (the steps: prepare, cut, look, read back, learn) · events.py · ui/
              kb/ (past edits, read-backs, feedback) · analyze/ (per clip, per second)
              plan/ (shot map → picks; moves, music, SFX, steadying, interview, music video) · export/ (xmeml writer)
kb/           manifest.json (bucket per edit) · notes.md (the user's rules) · profile/ keeper/ buckets/ footage/ library/ renders/ policy/
              eval/ (the evaluation set and baseline) · feedback/ (read-backs, choices, verdicts) · taste.json, taste/<bucket>.json
.xml-exports/ the user's Premiere exports: the knowledge base's source
cache/        derived data, about 5 GB (footage maps took hours with the drives connected: don't delete); sparse/ holds the keyframe maps
out/<name>/   one folder per cut: brief.md, shotmap.json, inventory.json, picks.json, decisions.json, <name>.xml, sheets, report.md,
              verdict.json, the finished video when the user saves it there, work/
out/library/<name>/  a shoot sorted by shot type (videtor library): wide/ close-up/ talking-head/ interview-bite/ b-roll/ (links), sheets/, bites.xml, library.md
scripts/      helpers and archived experiments (not imported by the CLI)
docs/         handoff.md · status.md · architecture.md · architecture.html (the original 10-29 design page)
private/      local-only context: the first session's messages and reports, a memory snapshot
```

`cache/`, `out/` and `private/` are gitignored.

## Conventions

- **Plain JSON files, no database.** xmeml is read with the stdlib (`kb/xmeml.py`) and written by our own
  writer (`export/xmeml_writer.py`). Not OTIO's `fcp_xml` adapter: it fails on 100 fps media.
- **Determinism:** Claude writes the shot map (or `aroll.json`/`mv.json`); everything after it is
  deterministic code. A re-cut keeps `look` swaps (`decisions.json` next to the picks; `cut --fresh` forgets them).
- **Tests:** `unittest` with synthetic fixtures. Every module opens with a docstring saying what it is for, in
  plain words. Match the surrounding code: compact Python, few comments, the same naming.
- **Writing to the user:** short and plain. Lead with the XML path and how to import it (File ▸ Import, drive
  connected). Then the cut section by section, the second-look changes, and the judgment calls. Report test
  results and anything unverified honestly.
