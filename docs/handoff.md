# Handoff: a new session starts here

Checkpoint 2026-11-04. The user is switching Claude accounts. A new session may have no memory of the earlier
chats, and the claude.ai artifacts made under the old account (the architecture page) may be invisible to it.
Everything needed is in this repo and in a few local files on this Mac.

When the user asks a new session to "read into this repo", do the four steps below, in order, then reply as
in step 5.

## 1. Know the project

- **The tool:** Videtor cuts Premiere Pro timelines (FCP7 xmeml) from a folder of raw footage and a written
  brief, in the user's own style, learned from their past Premiere edits.
  - Claude turns the brief into a shot map; everything after that is local code on the Mac.
  - The code: footage maps, CLIP, faces, Whisper, the filler, the XML writer.
  - The user imports the XML into Premiere.
- **The user:** Paritosh, a video editor in India (IST). The work spans brand/social reels, events,
  interviews, music videos and sports promos. They give briefs in plain words and judge cuts in Premiere.
- **The repo:**
  - The main checkout is `/Users/paritoshsrivastava/workspace/videtor`, on GitHub as `ashtava/videtor`
    (`main`, **public**).
  - Every Claude Code turn's changes are committed and pushed by a Stop hook.
  - `out/`, `cache/`, `.venv/` and `private/` are gitignored and exist only on this Mac.

## 2. Set up the skill on the platform you're running on

Work out which one you are in. Can you run shell commands? Which folder is the session in?

**Claude Code** (the `claude` terminal, Claude Code desktop's Code tab, or VS Code), on this Mac:
1. Run `cd /Users/paritoshsrivastava/workspace/videtor && scripts/setup-claude-code.sh --check`.
2. If it reports anything "not linked" or "not set", run `scripts/setup-claude-code.sh` (safe to rerun).
   - It links `~/.claude/skills/cut-from-brief` (or `$CLAUDE_CONFIG_DIR/skills/...`) to the repo's skill, so
     `/cut-from-brief` works in any folder.
   - It adds the auto-push Stop hook to `.claude/settings.local.json`.
   - New links and hooks load in the next session, or open `/hooks` once.
   - A real folder already at that path is left alone; tell the user.
3. Check the toolchain:
   - `export PATH=/opt/homebrew/bin:$PATH && .venv/bin/videtor --help` and `ffmpeg -version`;
   - `.venv/bin/python -m unittest discover -s tests` (295 tests, about 20 s, no drive needed);
   - `git status -sb` and `git ls-remote origin`: SSH push access. The Claude account doesn't matter to git.
   - If the user hasn't approved these commands yet, ask them to: shell commands need approval in a new
     session.
   - The project `.mcp.json` server `prpr` (the Premiere bridge) connects only while Premiere runs with its
     plugin, so a failed `prpr` connection is normal. The skill doesn't need it.
4. If `.venv/` is missing (a fresh clone or another Mac): `brew install ffmpeg uv && uv sync --extra analyze`.
   Then:
   - `cache/` (about 5 GB of footage maps and models) has to be rebuilt with the drives connected, so say so;
   - ask before any install or model download.

**Claude Desktop chat** (no shell):
- The skill needs a shell, so it can't run here. Tell the user to use the Code tab.
- Videtor's MCP server was retired on 2026-11-11 (the app replaced it; `scripts/archive/mcp_server.py`). If the
  user's Desktop config still lists a `videtor` server, tell them to remove that entry; don't edit the file yourself.


**A cloud session** (claude.ai/code on the web) or another machine: the footage drives, `cache/` and `out/`
aren't there. You can read and change code and docs only. Say so before planning a cut.

In Claude Code desktop, sessions should open on the main checkout with Worktree off (the default). In a
Worktree session, the skill still runs Videtor from the main checkout, and the hook pushes the worktree's
branch.

## 3. Read the context, in this order

1. `CLAUDE.md`: the user's rules (footage privacy, blind tests, Premiere bridge, no code changes during a
   delivery, report.md after each delivery), the environment, the layout.
2. `docs/status.md`:
   - every delivery and its verdict;
   - what the user owes us and what we owe them;
   - the offline proposal;
   - working agreements, backlog, drives.
3. `docs/architecture.md`: the modules, files and formats, and the verified Premiere and footage facts.
4. The skill, `.claude/skills/cut-from-brief/SKILL.md`. The commands are in `README.md`, and the helpers in
   `scripts/README.md`.
5. Local only, on this Mac:
   - `out/<name>/report.md` for any project the user names: what was delivered, how to rebuild it, the verdict;
   - `private/README.md`, then `private/session-2026-10-28-to-11-04/users.txt`: every message the user wrote in
     the first session. Read it when you need their exact words;
   - `assistant_long.txt` in the same folder holds the reports and proposals as sent.
6. Claude Code's project memory (`~/.claude/projects/-Users-paritoshsrivastava-workspace-videtor/memory/`)
   loads by itself on this Mac. If it didn't, read `private/memory-snapshot-2026-11-04/`.
7. Before a brief: `kb/buckets/<bucket>.md` and `kb/notes.md`.

## 4. Where things stand (2026-11-04)

The details are in `docs/status.md`.

- **Deliveries:**
  - Guru 25 s: approved as the way to work.
  - Blind test 1 (interview): good, with finishing notes.
  - Blind test 3 (Diwali v1): not good. That led to the event moves, buckets and the v2 re-cut.
  - Blind tests 2 (Check Hai music video), 4 (ET Alpha Summit) and 5 (engagement): awaiting verdicts. So is
    Diwali v2.
- **Decisions, all closed on 2026-11-07** (the user: "all the open decisions are checked and can be closed. move
  ahead"): offline step 1 is approved; the MCP server calling the CLI's code (plus `prep` and library tools) is
  approved; the repo stays public; Desktop chat and the "workflow" need nothing. Don't raise them again.
- **Waiting on the user:**
  - XML exports of their own colourstone and Check edits, to seed the interview and music-video buckets;
  - 3-5 edits per bucket.
- **Platform:** set up on 2026-11-04. `/cut-from-brief` is a personal skill, the auto-push hook is live,
  and Claude Code desktop is installed.
- **Built 2026-11-07:** `videtor library` (a shoot sorted by shot type into folders, offline, on every core,
  learning from the user's moves), the parallel footage map, the sparse-first `prep`/`cut` (prep reads
  keyframes in minutes; the cut decodes only the takes the map's classes allow), and the UI (`videtor ui`: a
  local page in Videtor's own Python, prep/cut as separate processes with the cut first, `~/Applications/Videtor.app`
  from `scripts/make-app.sh` to open it without a terminal, the Mac's own folder dialogs); see `docs/status.md`.
  The TPL library at `out/library/tpl` is accepted and learnt (786 labels). The UI replaces the MCP-parity item.
- **Open (2026-11-07): the UI's next shape.** The user asked for the user journey to give UX feedback; it is at
  https://claude.ai/artifact/Sfuj5WCoBeKHtEoidt4wxX with five decisions (D1-D5, also in `docs/status.md`).
  Build the proposed journey only after the user answers them. Offline step 1 depends on D1.

## 5. Your first reply

In a few lines:
- what you set up or checked on this platform, with anything that failed;
- the state above, in three or four bullets;
- a question: what's next? A new brief, a verdict on a delivered cut, or one of the open decisions.

Don't start building or cutting until the user says what they want. Never open the user's own edit of a
blind test. Say when you look at footage stills.
