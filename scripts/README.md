# scripts/

Helpers that sit beside the `videtor` CLI. They are not part of the pipeline and have no tests; the CLI never
imports them. Run them from the repo root with the project's Python:

```sh
cd ~/workspace/videtor && export PATH=/opt/homebrew/bin:$PATH
.venv/bin/python scripts/<script>.py ...
```

They were saved from the Claude session scratchpad on 2026-11-01, because the scratchpad (`/private/tmp/...`)
is wiped on reboot.

## Helpers

| Script | What it does | Used for |
|---|---|---|
| `stills_sheets.py <name> <out-dir>` | Contact sheets of every take's middle still, 48 per sheet, by folder. Works as soon as `prep` has sampled stills | Reading a big shoot while the footage map still builds (skill step 3) |
| `search_sheet.py <queries.json> <out.jpg> --key K [--landscape] [--skip ...]` | Runs several CLIP searches at once and sheets the top hits of each, labelled take@second | Finding key moments to pin in a slot's `takes` (rings, the proposal, a sign) |
| `cinematic_rank.py <music-folder> <features.json> [--top N]` | Features per track (harmonic/percussive ratio, onsets/s, tempo, loudness per 5 s), plus a cinematic score | Choosing a track when the brief says "use your own music" and wants a cinematic feel. The score makes a shortlist; then read the per-5 s loudness arcs. Engagement: Featherlight, 2nd on the score, chosen for its arc |
| `music_mood.py <music-folder> <out.json>` | Tempo, harmonic share, brightness and onset rate over each track's strongest minute | The colourstone interview ("royal, upbeat") |
| `transcribe_bytes.py <name> "<folder text>" [--prompt ...]` | Local Whisper over the interview takes of a pool | People bytes: Alpha Summit's "Bytes Camera 1" |
| `lips.py <name> <cache/sync/song.json> <out-prefix> TAKE@SONG_S ...` | Eight face crops 0.12 s apart: is the performer mouthing the words? | Music videos: who sings when (Check) |
| `real_shots.py <export.xml> <out-dir>` | An edit as the viewer sees it: the top-most clip per frame, merged into shots, a still per shot | Reading the user's own edit once they hand it over. Never use it on a blind test's edit before the verdict |
| `kbdiff.py <kb-a> <kb-b>` | Compares two KB folders: used ranges, and every changed value in projects/ and profile/ | Checking that a reader fix changed what it should |
| `extract_transcript.py <session.jsonl> <out-dir>` | Pulls the user's messages and Claude's long replies out of a Claude Code session log | Writing handoffs and reports (`~/.claude/projects/-Users-paritoshsrivastava-workspace-videtor/*.jsonl`) |
| `premiere/project_marks.js` | Read-only. Lists every clip in the open Premiere project: bin, path, colour label, markers, Source Monitor in/out marks | `prpr exec scripts/premiere/project_marks.js`. Produced `kb/marks/guru.json` |
| `premiere/motion_probe.js` | Read-only. Reads Motion Position/Scale/Rotation of named clips on the active sequence (edit `want`) | Checking Basic Motion conventions against Premiere's stored values |

`setup-claude-code.sh` sets this Mac up for Claude Code (terminal, desktop, VS Code); it is safe to rerun,
and `--check` only reports.
- It links `~/.claude/skills/cut-from-brief` to the repo's skill, so `/cut-from-brief` works in every session.
- It adds the auto-push hook to `.claude/settings.local.json` when missing.

`hooks/autopush.sh` is the Claude Code Stop hook. It is set up in `.claude/settings.local.json`, which is
gitignored.
- At the end of every Claude turn it commits whatever changed and pushes it to `origin`.
- In a Claude Code desktop worktree session, it also commits and pushes the worktree's branch.
- It skips merges and rebases in progress, refuses leftover conflict markers and anything that looks like
  a secret, and leaves out files over 50 MB.
- It prints one line when it acts.
- Test it by hand with `echo '{}' | scripts/hooks/autopush.sh`. Turn it off in `/hooks`.

The Premiere scripts go over the prpr bridge. Run them only when the user has said Premiere is free: a read
once picked up whichever project they had open. Never bulk-write effects over the bridge, because it crashed
Premiere twice on 2026-10-30.

## archive/

These are one-off experiments from 2026-10-28 to 10-31, kept as they ran. Many hard-code a drive, a project or the
old scratchpad path, so read one before you run it. Their findings are recorded in `docs/status.md` and the
project memory.

| Files | What they were for |
|---|---|
| `mp_experiment*.py`, `sanity.py`, `tags_sanity.py`, `tags_contrast.py`, `tags_threshold.py`, `tags_vocab.py` | Round 3 (10-28/29): MediaPipe face/hand models and CLIP tag vocabulary on real cached frames |
| `gap.py`, `within_take.py` | Round 3 diagnosis: how much footage the planner looked at, and whether the frame from the used moment beats a take's other frames (it doesn't: sharpness 48%, faces 50%) |
| `calib.py`, `smoke.py`, `try_decode.py`, `verify.py` | Footage-map calibration and checks on the Guru pool (10-29) |
| `boxes.py`, `kp.py`, `kpstats.py`, `clipver.py`, `evalpipe*.py`, `evalrule.py`, `groups.py`, `look*.py`, `probe*.py`, `score.py`, `orient_check.py`, `runall.py`, `sheet*.py` | Orientation detection: face boxes and keypoints per turn, rule sweeps against known rotations, sheets of turned takes (10-29) |
| `survey.py` | xmeml reader survey: clips with −1 starts/ends next to transitions (10-29) |
| `stdio_check.py`, `stdio_music.py` | Smoke tests of `videtor.mcp_server` over stdio (10-29) |
| `mcp_server.py`, `test_mcp_server.py` | Videtor's MCP server and its test, retired 2026-11-11 (a Round 4 copy of the cut; the app replaced it) |
| `rounds-1-3/` | The rule-based planner before the shot map (`rules.py`, `structure.py`, `rerank.py`) and its tests; see its README |
| `pull_meta.py`, `seq_names.js` | Early Premiere reads: per-item project metadata (slow, about 5-9 s each) and sequence names |
| `motion_probe.js` | First version of `premiere/motion_probe.js` |
| `finish_check.js`, `finish_probe*.js` | Scratch versions of the `videtor finish` bridge script (the kept one is `src/videtor/kb/js/finish_interview.js`) |
| `jogi*.py` | Diwali song analysis: beat grid, energy per beat, chroma repeats for medley joins, Whisper on the MC intro (10-31) |
