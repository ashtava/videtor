# Videtor status

Checkpoint 2026-11-07 (first written 2026-11-01). Update this file after every delivery, verdict or build.
The user is switching Claude accounts. A new session starts with `docs/handoff.md`; the history up to now is
in `private/` (local only).

**Where things stand.** Videtor cuts a Premiere Pro timeline from a written brief and a folder of raw footage,
in the user's style. The Guru 25 s cut (Round 4) was approved as the way to work, and five blind tests on
footage the tool had never seen followed:
- blind test 1 (interview): "good", with finishing notes;
- blind test 3 (Diwali): "not good", which led to the event moves and buckets;
- blind tests 2, 4 and 5: waiting for the user's verdict.

The user also has an open question: how much of the work Claude does, and whether to take it offline. The
proposal is below, and the user hasn't answered it yet. All 295 tests pass.

On 2026-11-07 the user asked for two things, both built the same day (see Builds): a footage library that sorts
a shoot into wide / close-up / talking-head / interview-bite / b-roll folders offline and in parallel, and the
slow single-threaded passes behind `/cut-from-brief` made parallel. The TPL library is built and waits for the
user's sorting pass.

The repo has been on GitHub since 2026-11-01 (`ashtava/videtor`, `main`, public). Every Claude turn's
changes are committed and pushed by a Stop hook; `out/` and `cache/` stay local.

## Deliveries

All times IST. Each folder's `report.md` has the report as sent, the commands that rebuild the cut and the
verdict.

| # | Date | Project (`out/…`) | Kind / bucket | Output | Verdict |
|---|---|---|---|---|---|
| R4 | 10-29 | `guru-25s-brief/` | brand-social, not blind | `guru-25s-brief.xml` (25 s, 9:16); `-sfx.xml` with SFX | **"This is good. This is what I was looking for. We can standardize this."** |
| B1 | 10-30 | `colourstone-mia/` | interview (no bucket) | `colourstone-mia-60s.xml` (60 s, 4K 16:9) | **"This is good"**, plus finishing notes. v2 has ramps and zooms; grade and stabilising are done by hand (the bridge crashed Premiere) |
| B2 | 10-30 | `check-hai/` | music video (no bucket) | `check-hai.xml` (176.7 s, 4K 16:9, 272 lip-synced cuts) | **Awaiting** |
| B3 | 10-31 | `diwali-party/` | event | `diwali-party.xml` (59.4 s, 9:16) | **"The shot selection was not good. The flow was also not good."** The user gave their own edit |
| — | 10-31 | `diwali-party-v2/` | event, re-cut with their edit in view | `diwali-party-v2.xml` (47.4 s, 9:16) | None given |
| B4 | 10-31 | `alpha-summit/` | event, `keeper_w` 0.3 | `alpha-summit.xml` (30.5 s, 9:16, bytes, steadied) | **Awaiting** |
| B5 | 11-01 | `engagement/` | wedding (new) | `engagement.xml` (60 s, 16:9, steadied, music only) | **Awaiting** |

Rounds 1-3 (10-28/29) used the rule-based planner (`videtor plan`) on past projects. Their outputs:
- `out/guru-v0`, `guru-v1`, `guru-v2`: benchmarks against the real Guru edit;
- `guru-20s`: a fast cut. Verdict: it plays and the beat timing feels right, but the opener is not a hook;
- `guru-20s-glimpse`: a teaser. The user agreed the hook is a per-piece choice, and said better shots existed;
- `roundtrip-*.xml` and `writer-test-tpl.xml`: reader/writer checks in Premiere;
- `smoke-jw-cc`: the `prep` smoke test.

## Builds (between projects)

| Date | What | Code | Result |
|---|---|---|---|
| 11-07 | **The footage library**, `videtor library build \| learn \| status`: a shoot sorted into wide / close-up / talking-head / interview-bite / b-roll folders (links to the clips), a contact sheet per class, every interview bite as a Premiere sequence (`interview-bite/bites.xml` + `bites.md`), `library.md` with the reasons and the clips worth a look. Rules first; `learn` reads the user's folder moves back as labels and fits a model the next build uses. Keyframes only, every core, no Claude at run time | `plan/library.py`, `analyze/sparse.py` (the keyframe-only map, `cache/sparse/`), `analyze/parallel.py`, `kb/library/` | TPL, both folders the user named (786 clips, 5.9 h): the scan took 11 min 15 s on 8 workers, none failed; Whisper on the 159 speaker clips (136 min of audio) 12 min 45 s the first time; a rebuild with the caches 5 min 49 s. Sorted: 77 wide, 158 close-up, 83 talking-head, 68 interview-bite, 400 b-roll; 903 bites (132 min, 751 with clean edges); 115 clips flagged for a look; 58 of the 61 Master Cam takes read as talking heads (the other 3 have no speech or a walk-in). 15 clips are copies (Sam's Card 3 holds Master Cam files). `out/library/tpl/` |
| 11-07 | **Parallel passes**: workers sized to the Mac (performance cores, memory, `VIDETOR_WORKERS`), ffmpeg thread caps, the footage map's face and hand detections in up to 4 worker processes with CLIP in the main process, steadying measured side by side, `prep` and `orient --workers` default to the machine instead of 3 | `analyze/parallel.py`, `analyze/footmap.py`, `plan/stabilize.py`, `cli.py` | Measured (`docs/architecture.md`): a 4K XAVC H.264 software decode is CPU-bound (one ffmpeg fills 10 cores), so the footage map can't run faster than about 2.5-3× real time whatever the worker count; thread caps gain 15%, HEVC goes to the hardware decoder many at a time, and the content stage (49 ms per sample, the hand landmarker most of it) now runs 4-wide. The keyframe-only pass is 8-10× cheaper, which is why the library reads 5.9 h in 11 min |

## Waiting on the user

1. **Verdicts** on Check Hai, Alpha Summit and Engagement (and Diwali v2). When they fix a cut in Premiere, read
   it back: `.venv/bin/python -m videtor.kb.readback out/<name>/picks.json --live` (say so before touching
   Premiere) or `--xml <export>`, then `videtor kb learn`. No real feedback is recorded yet: `kb/feedback/` is
   empty and there's no `kb/taste.json`.
2. **The offline plan** (below): whether to build step 1.
3. **Bucket material:**
   - Their own XML exports of the colourstone (interview) and Check (music video) edits; the footage is
     already mapped.
   - Then 3-5 edits per bucket: XML, raw footage folder, one line of brief.
   - Confirm the starter bucket list: event, brand-social, sports-promo, interview, music-video, travel-vlog,
     ad, maybe a celebrity/fashion BTS bucket split off for Guru.
4. **A Premiere check** of `out/guru-25s-brief/guru-25s-brief-sfx.xml`: do the SFX levels and `#` in file
   paths come through?
5. **Claude Desktop** was installed by the user on 2026-11-01, who said they set up the MCP and made a workflow.
   - At Desktop's 13:43 start its config had no `mcpServers`, and its log said "no stdio servers
     connected", so Videtor isn't loaded in Desktop yet.
   - The user has the snippet. Claude doesn't edit that config.
   - Where the workflow lives and what it does: asked, not yet known.
   - The server itself starts fine the way Desktop launches it (stdio, cwd `/`, Homebrew on PATH; 10 tools).
   - **Claude Code desktop** was set up by the user on 2026-11-04.
     - `scripts/setup-claude-code.sh` linked `/cut-from-brief` as a personal skill. A session opened outside
       the repo lists it.
     - The skill runs every command from the main checkout.
     - `.claude/worktrees/` is gitignored, and the hook pushes a worktree session's branch.
6. **GitHub visibility:** `github.com/ashtava/videtor` is public (checked 2026-11-01).
   - It holds the user's Premiere XMLs (`.xml-exports/`) and footage indexes (`kb/footage/`) with client file
     names and paths, and the auto-push publishes every change.
   - Raised with the user, who decides whether to make it private.
7. **The TPL library** (`out/library/tpl`, built 11-07): open the five folders or `sheets/<class>-N.jpg`, move
   the wrong links into the right folders, then run `videtor library learn out/library/tpl` and build again
   (`videtor library build "/Volumes/Paritosh_3/TPL/Videos" "/Volumes/Paritosh_2/TPL Promo/Master Cam" --name tpl`).
   The 115 clips under "worth a look" in `library.md` come first. Also whether the five classes are the right
   ones: a tight detail of an object sits in close-up, a two-person interview in talking-head, stage mics and
   podium speeches in interview-bite, and everything with people mid-frame not speaking to camera in b-roll.

## The offline proposal (2026-11-01, not yet approved)

The user asked how much is pushed to Claude, whether frames are sent, and why the code keeps changing. The
answer given:

| Work | Where it runs |
|---|---|
| Index, stills, orientation, footage map (CLIP, faces, hands, motion, sharpness, 2 samples/s) | The Mac. Alpha Summit: 770 takes, about 21,000 samples, about 36 min |
| Transcription (Whisper), music analysis (beats, lifts, loudness) | The Mac |
| Picking the take and second for every slot; timing, ramps, SFX, steadying, XML | The Mac, Videtor code. Alpha Summit: 57 slots × 736 takes in about 90 s |
| Brief → structure, slot descriptions, music in-points, which bytes | Claude: one shot map per project |
| Looking at footage | Claude: contact sheets of downscaled stills, about 10-35 per project (about 1,500 thumbnails for Alpha Summit). Never video |
| Second-look swaps; calls like aspect ratio and what to leave out | Claude |
| New features and fixes | Claude, in every project so far |

Steps proposed:
1. **Automate what Claude does by hand.** No new models; a few days of work.
   - A bucket template that lays its section skeleton onto the music's lift, drop and outro.
   - Slot descriptions drawn from what the bucket's edits keep, sampled through the shoot in time order.
   - Music chosen by measurement (`scripts/cinematic_rank.py`, `videtor music`).
   - A rule-based byte picker.
   - An automatic second look from the keeper, the taste model and rules (crew at the frame edges, closed
     eyes, blur).
2. **Local models for language and vision.** A 7-8B LLM on MLX turns a brief into a shot map, using past
   maps as examples. A local captioner describes takes in words. That's 5-8 GB of downloads (needs consent);
   on 16 GB of RAM they run one at a time, and they're weaker than Claude on unusual briefs.
3. **Close the loop.** After every delivery, read the user's fixes back into the taste and keeper models.

A middle ground: Claude sees text only (the brief, local captions, numbers), and the second look runs locally.

## Working agreements

- **GitHub, with auto-push.** Until 2026-11-01 the rule was "Ignore Committing. We are going to keep this
  local only." On 2026-11-01 the user put the repo on GitHub and asked: "push to github and create a push
  everytime a change happens".
  - `scripts/hooks/autopush.sh` runs as a Stop hook (`.claude/settings.local.json`).
  - The repo is public, so tracked files must not carry private people's names or client-confidential
    details; those go in `out/`.
  - Never rewrite pushed history.
- **Ask before installing** anything, including model downloads.
- **Footage privacy.** Claude may see stills and contact sheets (allowed 2026-10-29); say when you look.
  Video never leaves the Mac. No client footage goes to any cloud service.
- **Blind tests stay blind.** Never open the user's own edit before the verdict:
  - colourstone_2: `main-16x9.mp4`, `9x16_4K_1.mp4`, `pf/`, `render/`, `A Beam of Light.mp3`;
  - Check: `pf/`, `DCIM`;
  - Diwali Party: `pf/`, `untitled folder`;
  - Paritosh_3: `TPL/pf`, `DP World 2026/pf`, `Me/...` were not opened.
- **Premiere.** Say so before reading it live; the user may be working in another project. Never bulk-insert
  effects over the prpr bridge (it crashed Premiere 26.0.2 twice on 2026-10-30).
- **Claude Desktop config:** never edit it; give the snippet.
- **Drives:** tell the user explicitly when a drive is safe to unplug, and expect swaps.
- **No code changes during a delivery** (agreed 2026-11-01, after the user asked why the code keeps changing).
  - Cut with what exists, list the gaps here, and build between projects.
  - The exception is a brief that explicitly needs a feature (as "stabilise" did); say so.
  - After a build, re-cut past projects to check that earlier cuts still come out the same (proposed).
- **After every delivery,** write `out/<name>/report.md`, update this file, and keep project memory current.

## Buckets

Buckets are the edit types in `kb/manifest.json`. The skill reads `kb/buckets/<bucket>.md` before planning.

| Bucket | The user's edits in it | Profile | Keeper | Card |
|---|---|---|---|---|
| brand-social | Adidas, Coca-Cola, Guru, JW, Vivo 1, Vivo 2, Zomato | `kb/profile/brand-social.json` | none | none |
| sports-promo | Gujarat Giants, TPL/Leander | `kb/profile/sports-promo.json` | none | none |
| event | Diwali Party | `kb/profile/event.json` | `kb/keeper/event.json` (cross-validated AUC 0.76) | `kb/buckets/event.md` |
| wedding | none (the engagement cut has no user edit yet) | none | none | none |
| interview, music-video | none (their colourstone and Check exports would seed them) | none | none | none |

Build a card with `videtor kb breakdown <xml> --footage <folder>`, `videtor kb keeper <bucket>` (needs the raw
footage mapped) and `videtor kb bucket <bucket>`.

## Known gaps and backlog

Not built; the source is in brackets.
- Grade, LUT, Warp Stabilizer and the "Flash 2" Lumetri flashes can't travel in xmeml. The grade is applied by
  hand with an adjustment layer; `videtor finish` over the bridge is unsafe. [B1, B3, B4]
- Keyframe steadying removes about 50-65% of shake, below Warp Stabilizer quality. [B4]
- The riser `ShortRiser5` peaks 0.9 s in. The user starts it on a cut; the placer aligns its peak to the drop.
  [Diwali v2]
- Still photos can't be placed. [Diwali v2]
- Event pace is still slower than the user's (0.56 s against their 0.32 s median); more half-beat slots would
  help. [Diwali v2]
- HD vox-pop bytes scaled 178% into 9:16 look soft. [B4]
- For cinematic films: natural-sound moments, dissolves, titles, letterbox. A per-genre roadmap was proposed.
  [B5]
- Resolve import hasn't been checked (Phase 2). Resolve 19.1.4 Studio has external scripting for later.
- Offline steps 1-3 (above).
- **The library's next steps** [11-07]: transcription runs after the scan, not alongside it (13 min on top of
  the 11 min scan for TPL); the sparse map could feed `search` and a sparse-first `cut` (dense-decode only the
  candidate takes), which would take a big shoot's `prep` from an hour to minutes; "speech" is the WebRTC VAD,
  which hears the PA, so a listener in a single medium shot can read as a bite until the user's labels say
  otherwise; CLIP's "stage" prompts tell a podium from an interview setup, a plain wall behind a vox pop can
  still read as a setup.
- `cut` itself: `fill` is single-threaded numpy (90 s on Alpha Summit) and the sheets and SFX placement are
  serial; small next to the footage map. [11-07]
- **The MCP server lags the CLI** (found 2026-11-01). `mcp_server.cut` and `apply_look` are Round 4 copies of
  the CLI code.
  - Missing: SFX placement, steadying, byte levelling, the bucket keeper, taste corrections, `sfx_skip`,
    beds, flash frames, the cut and match sheets.
  - A medley `music` list fails: its `_music_for` only reads a single track.
  - `apply_look` takes runner-up numbers only; no `id=<take>` or `other:k`.
  - No tools for `prep`, `orient`, `cut-interview`, `cut-mv`, transcription, cinematic music ranking, or the
    library.
  - Fix: have the MCP tools call the CLI's functions (one code path), and add `prep` as a background job
    with a status tool.
- Regression check: re-cut past projects after each change, and compare picks.

## Drives

The memory has more detail. Footage indexes are in `kb/footage/<drive>.json` for drives and
`kb/footage/<name>.json` for `prep` pools.

| Drive | Holds |
|---|---|
| Paritosh_1 | Guru (`/Volumes/Paritosh_1/Guru `, trailing space), Vivo 1, Vivo 2; `JW 2/17 sep/cc` (the prep smoke test) |
| Paritosh_2 | Recent projects, each with the user's `pf/` project: Check, colourstone, colourstone_2, Dikkat, Extra, mia, Mia 2, TPL Promo, Diwali Party |
| Paritosh_3 | TPL/Leander (`TPL/Videos`), Engagement (blind test 5), `Me/{lego,philips,meta,itc,Photos}`, `DP World 2026` |
| Paritosh_4 | Coca-Cola (`coca cola`; the XML points at `Copied_Khanna Fish`), `Copied_Rigo` |
| AMAZONSSD | Gujarat Giants (`WPL Gujarat Giants`) |
| AJAN SSD4 | Zomato ("PackSwitch"), Bar Maestro, Karl 2, OP, orry, JW copies |
| Extreme SSD | JW ("Jonnie walker X Lolla") |
| Adidas | `ADIDAS x NEXMAG` (the Adidas edit), `ALPHA SUMMIT` (blind test 4), `beauti verse` |

## Machine and installs

- Apple M2 Pro, 16 GB, macOS 26.3.1. Premiere Pro 26.0.2 (UXP), DaVinci Resolve 19.1.4 Studio.
- Homebrew: ffmpeg (no vidstab), uv. Python 3.11 venv in `.venv`.
- Models:
  - MediaPipe BlazeFace full-range and hand landmarker (`cache/models/`);
  - CLIP ViT-B/32 laion2B (`~/.cache/huggingface`);
  - mlx-whisper large-v3-turbo (`cache/models/whisper-large-v3-turbo`, fetched with curl because the HF client
    stalled).
- prpr 1.0.0 (`~/.local/bin/prpr`) with the bridge plugin 0.3.0 inside Premiere. The bridge can't import XML.
