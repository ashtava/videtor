---
name: cut-from-brief
description: Cut a Premiere Pro timeline from a written brief with Videtor (folder or indexed project → footage map → shot map → picks with alternates → second look → Premiere XML with music and SFX). Use when the user gives a brief and a footage folder (new, never edited) or an indexed past project, or asks to re-cut one.
---

# Cut from a brief

You turn the user's brief into a shot map; Videtor does the rest on their Mac. Work in
`~/workspace/videtor` with `.venv/bin/videtor` (prefix `export PATH=/opt/homebrew/bin:$PATH`).
The repo is on GitHub (`ashtava/videtor`, public as of 2026-11-01). A Stop hook commits and pushes
every change at the end of each turn, except `out/` and `cache/`, which are gitignored. So keep a
project's specifics there: people's names, client details, sheets. The user allowed Claude to see frames
of their footage (2026-10-29) for the overview and the second look; still say when you are looking.
**Don't change Videtor's code during a delivery.** Cut with what exists, and list the gaps in
`docs/status.md`. Build between projects, unless the brief explicitly needs a feature; then say so.

Inputs: the brief (text) and either a footage folder (`/Volumes/<drive>/<folder>`) or the
key of an indexed project (e.g. `"Guru.xml::Sequence 01"`).

1. **Save the brief** verbatim to `out/<name>/brief.md` (`<name>`: short, kebab-case). **Name its bucket**
   (the kind of work: `event`, `brand-social`, `sports-promo`, ...; the user's word, else infer it from the
   brief and say so) and read `kb/buckets/<bucket>.md` before anything else: your edits of that bucket in
   numbers (pace, ramps, music, layers, sound), what they keep and leave, and the user's rules. A bucket
   with no card yet: plan from `kb/notes.md` and the nearest bucket, and say so. Put `"bucket"` in the
   shot map: the cut then adds the bucket's keeper score (what the user's edits of it keep) to every slot.
2. **Prepare the footage.**
   - New folder: `videtor prep "<folder>" --name <name>` (drive connected; about 5 min per
     25 min of footage). It indexes, samples stills, detects sideways cameras, builds the
     per-second footage map, and writes `out/<name>/overview.md` + `overview-*.jpg`. When it
     prints that the drive can be unplugged, tell the user. The shot-map key is
     `<name>::footage`.
   - Indexed project without a map: `.venv/bin/python -m videtor.analyze.footmap <mount> --key KEY`.
   - Another shoot left on the same card (other dates in the file names, another event in the stills):
     mark those files `"excluded": true` in `kb/footage/<name>.json`, and they stay out of every pool. If
     prep is decoding them, stop it and run it again (Alpha Summit: the 22 May clips).
   - Sony XAVC 4:2:2 10-bit has no hardware decode, so a big shoot's map takes 30-60 min. Run prep in the
     background.
3. **Read the overview**: `overview.md` (takes, cameras, frame rates, how much is portrait or
   landscape, faces, hands, camera moves, setups) and every `overview-*.jpg` (one still per take,
   grouped by setup). Note the looks, locations, products and people the brief can draw on.
   On a large shoot, also sheet every take's middle still as soon as prep's stills step ends
   (`.venv/bin/python scripts/stills_sheets.py <name> out/<name>/work/stills`). You can read the material
   and draft the shot map while the map builds.
   Check orientation by eye: detection reads faces, so a sideways camera that shot only bottles,
   products or hands stays as shot (`prep` prints a "check" line for a camera whose few faces said
   "turned"). Lettering, horizons and standing people tell you; fix a camera or a take with
   `videtor orient <name> --camera M2U --rotate -90` (or `--take <file>`; -90 = counter-clockwise,
   as in Premiere), which rebuilds those takes' footage map (drive connected) and the overview.
4. **Music.** For a folder or a feel ("a high-beat track"): `videtor music <folder> --length
   <seconds> --reveal-at <seconds>`. Choose the track and in-point so a lift (drop) lands on the
   reveal; a beat is 60/BPM seconds. Count the beats per section with the track's beat grid
   (`cache/beats/<track>.beats.json`).
   **No music given** ("use your own music"): pick from the user's library (`~/Downloads/Music`). For a
   cinematic or emotional piece, `.venv/bin/python scripts/cinematic_rank.py ~/Downloads/Music
   out/<name>/work/music-features.json` gives a shortlist. Choose on the per-5 s loudness arc: a quiet
   open, a lift, a climax, an outro. You can't hear the tracks, so say the track was chosen by measurement,
   and don't claim more than the numbers show. A medley of one track's intro, climax and outro covers a
   60 s film (`out/engagement/`).
5. **Search** for every kind of shot the brief asks for: `videtor search - "<description>" --key
   KEY` (the drive argument may be `-` for a prepared folder). Rephrase until the top hits are
   what you mean; CLIP reads concrete nouns (rings, chains, buttons, sunglasses, a man in a white
   jacket) far better than qualities (premium, confident). To run many searches at once with a sheet
   of the hits: `.venv/bin/python scripts/search_sheet.py <queries.json> out/<name>/work/search-1.jpg
   --key KEY [--landscape]`.
   For an event or party reel, read the `[event]` rules in `kb/notes.md` and `kb/profile/event.json`
   before planning: the user's own Diwali edit (`.xml-exports/Diwali Party.xml`) is people first
   (faces to camera, poses, hugs; no crowd wides), uses the photo wall with the event's sign as the
   recurring "board", flickers decor and cards at 2-3 frames, cuts at a median 0.32 s with speed
   ramps on hero moments, and builds a medley with a black pause before the drop. A 1-2 beat grid of
   venue coverage (the first Diwali cut, `out/diwali-party/`) was judged poor on selection and flow.
   The moves the user's edits use are shot-map options (`src/videtor/plan/shotmap.py`, example
   `out/diwali-party-v2/shotmap.json`): half-beat slots (`beats: 0.5`), `ramp` ("arrival" 100/50/25%,
   "punch" 140/50/130%), `flicker: 3` (sub-shots of 3 frames from different takes, clicks on the cuts),
   `stack: true` (smaller shots popping on top every 5 frames), `black: true`, a music medley (`music` as
   a list of segments with `gap_beats`), `flash` frames over cuts and `beds` of ambience. Use them where
   the bucket's card shows the user using them, not by default.
   **People bytes** ("use people bytes", vox pops, testimonials): transcribe the interview takes
   (`videtor.analyze.speech.transcribe`, local Whisper), pick 1.5-2.5 s lines that say something about the
   event, check their edges on the audio (40 ms loudness: start and end in silence, not on a neighbour's
   word), and give each a `"byte": true` slot with `takes: ["<take>*@<in>-<out>"]` and the beats that cover it
   (half beats allowed). The voice plays on A1 at `voice_lufs` (default -14), the music dips to `under_db`
   (default -12), no SFX land on it; a medley jump hidden under a byte goes unheard. **Shaky shots** ("stabilise"):
   `"stabilize": true` on the map steadies the handheld V1 picks of 0.5 s or more with Motion keyframes (bytes and whips excluded) (drive connected; a
   slot's `"stabilize": false` opts out); the cut prints how many and the reasons say by how much.
6. **Write `out/<name>/shotmap.json`** (schema: `src/videtor/plan/shotmap.py`; example:
   `out/guru-25s-brief/shotmap.json`). Sections in the brief's order; per slot 2-3 `show`
   phrasings, `beats` (fast cut: 1-2, holds: 4), soft filters (`scale`, `face`, `hands`,
   `camera`: steady|moving), `speed: 0.5` where slow motion suits (honoured on 50/100 fps
   takes). Put what the brief rules out in `avoid` (crew, phones, light stands; words like
   "people" or "photographer" there also cost every frame with two faces, so leave them out of a
   party's avoid list). Give the opening section exactly the beats up to the drop, and mark the
   drop's first slot `"role": "reveal"` when the opening has more than one section (the riser and
   impact build to it). Name the hero look when there are several. More control when you know the
   footage (example: `out/diwali-party/shotmap.json`):
   - `"match": true` on a section makes it a run of match cuts (boards, signs, objects framed alike):
     its takes are chosen together so each cut lines up; `match-cuts.jpg` shows every cut as a pair.
   - a slot's `takes` limits it to named takes (`"C1588*"`) or moments (`"C1777*@0.5-3.5"`, seconds),
     for a shot you have seen and want (a sign, an arrival before a photographer walks in).
   - `skip` leaves out takes by path pattern (a second camera's folder, `"*/drive-download-*/*"`);
     `sfx_skip` leaves out SFX kinds (`["shutter"]` when the flashes are party lights, not cameras).
7. **Cut.** `videtor cut out/<name>/shotmap.json` writes `picks.json`, the second-look sheets and
   `<name>.xml`: picks on V1, two alternates per slot on V2-V3 switched off, music on A1 with a
   fade-out, SFX in the user's style on their own track (`--no-sfx` to leave them out), Basic
   Motion per camera type (the user's own habit, else fill the frame; landscape takes in a
   vertical piece are cropped around the face).
8. **Second look.** Read every `out/<name>/second-look-*.jpg`. Swap a slot when a runner-up fits
   the brief better: the right content, the subject sharp, nothing the brief avoids (look for
   crew at the frame edges and in the foreground), the move resolving inside the window, no empty
   frame. Apply: `videtor look out/<name>/picks.json details-3=2 match-7=C1590 reveal-2=reveal-1:1
   --note "details-3=why"` (`id=k` takes runner-up k, `id=<take>` the runner-up from that take, which
   survives a re-cut that reorders them, `id=other:k` borrows a neighbour's). A re-cut drops earlier
   swaps: apply them again. Look across slots too: two takes of one group at one table (consecutive
   file numbers) read as a repeat. When a slot's runners-up are all wrong, improve its `show`
   wording, filters or `takes` and cut again instead.
9. **Check** with one sheet of the final cut in order (build it like `cut-sheet.jpg` in
   `out/guru-25s-brief/`), and `videtor.plan.benchmark.score_picks` when the project has a real
   edit to compare with.
10. **Report** the XML path (Premiere: File ▸ Import; the drive must be connected to play), the
    track and in-point, the cut section by section, what the second look changed and why, and the
    judgment calls you made on the brief. Alternates sit on V2-V3. Save the same report as
    `out/<name>/report.md` (examples in `out/engagement/`, `out/alpha-summit/`). Include the commands
    that rebuild the cut: prep, cut, and `look` with every swap and its note. Then update
    `docs/status.md` (the delivery row, what you're waiting on, any gaps you noted).
11. **After the user fixes the cut**, read it back: `.venv/bin/python -m videtor.kb.readback
    out/<name>/picks.json --live` (or `--xml <export>`); the labels land in `kb/feedback/`.

## When the brief is a music video (performers lip-syncing to a song)

Steps 1-3 as above; then, instead of a shot map:

1. **Sync** every take to the song (drive connected; seconds per take):
   `.venv/bin/python -m videtor.analyze.sync kb/footage/<name>.json --key "<name>::footage" --music "<song>"`.
   Each take prints its synced stretches (song span, take span, local r 0.6-0.9 when the playback is
   clear). Takes without a stretch are B-roll. A stretch at `50%` was filmed to double-speed playback.
2. **Song structure**: `videtor.analyze.beats.load_or_track` for the grid (check whether the tracker
   counted eighth notes: a rap at ~92 BPM tracks as ~185; then a bar is 8 tracked beats), the RMS and
   onset curves for sections (drops, breakdowns, outro), and local Whisper on the song
   (`videtor.analyze.speech.transcribe`) for where vocals start and a new voice enters ("Uh, yeah, okay").
3. **Who sings when**: build sheets of every take's stills with its synced song span (`out/<name>/takes-*.jpg`);
   coverage tells (a performer's takes start rolling just before his part). Confirm by eye with lip strips:
   8 face-cropped frames 0.12 s apart at chosen song times (`scripts/lips.py <name> "cache/sync/<song>.json"
   out/<name>/lips C1518@95.2 ...`); open and changing mouth shapes = rapping. Roaming group takes: a frame every few seconds shows whom the camera follows.
4. **Write `out/<name>/mv.json`** (schema in `src/videtor/plan/musicvideo.py`; example `out/check-hai/mv.json`):
   per take `who` (A, B, both, `singer` for a take that follows whoever sings, none) and `look` (location +
   outfit), `skip` for unusable takes, `avoid` ranges (crew hands), sections on the bars with `beats`,
   `stutter`, `open` (a hold that introduces the singer), `singer`, `share` (the brief's "some of the other
   rapper": ~15%), `inserts` only where nobody sings, `open_on`/`close_on`.
5. **Cut**: `videtor cut-mv out/<name>/mv.json`. It prints shares per section; read every `cut-*.jpg`
   (outlined = the other performer; `away` = a cutaway moved by whole bars; `ins` = no playback).
   Swap weak shots with `videtor look out/<name>/picks.json <id>=k` (a slot's own runner-ups only: they stay
   in sync); a re-cut drops earlier swaps, so apply them again. Check the XML: start − in = the take's offset
   (within a frame) for every lip-synced clip.
6. **Report** as in step 10, plus where each singer's part is and how you found it.
