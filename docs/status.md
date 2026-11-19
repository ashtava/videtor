# Videtor status

Checkpoint 2026-11-11 (first written 2026-11-01). Update this file after every delivery, verdict or build.
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

On 2026-11-10 the user gave eight of their rendered sports reels (an IPL team's hype reels, practice-day wraps
and a match-day reel; `~/Downloads/Exports/Videos`, no raw footage, no projects) and asked for a sports library
learnt "in a reinforcement-based environment", so that raw media of a sports event can be cut like them, with
some of the special effects added in the output. Built on 2026-11-11 (see Builds).

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
| A1 | 11-07 | `~/Desktop/diwali_test/diwali-test/` (an app project, not under `out/`) | event, the first brief through the app; the Diwali footage again, so not blind | Shot map only (49.7 s, 9:16, 91 slots); the user cuts it from the app | **Awaiting the cut** |
| S1 | 11-11 | `sports-practice-a/` | sports-promo, the first raw sports footage (Paritosh_2 `videos`, prep `sports-raw`; `untitled folder` left out unopened); no written brief | `sports-practice-a.xml` (28.4 s, 9:16, a practice-day wrap, effects at the learnt rates) | **Awaiting** (the user reviews it alongside `fx-test`) |
| S2 | 11-11 | `sports-matchday-b/` | sports-promo, the same footage's second shoot (another team, log footage) | `sports-matchday-b.xml` (36.5 s, 9:16, a match-day reel) | **Awaiting** |
| A2 | 11-16 | `/Volumes/Paritosh_4/4/xml/4/` (an app project, not under `out/`) | event (inferred: an arrival at a hotel), 18 takes, all 4K 100 fps | Shot map only (17.9 s, 9:16, 23 slots, Flint - Brass Attack from 77.9 s, drop on the reveal at 5.1 s); the user cuts it from the app | **Awaiting the cut** |

Rounds 1-3 (10-28/29) used the rule-based planner (`videtor plan`) on past projects. Their outputs:
- `out/guru-v0`, `guru-v1`, `guru-v2`: benchmarks against the real Guru edit;
- `guru-20s`: a fast cut. Verdict: it plays and the beat timing feels right, but the opener is not a hook;
- `guru-20s-glimpse`: a teaser. The user agreed the hook is a per-piece choice, and said better shots existed;
- `roundtrip-*.xml` and `writer-test-tpl.xml`: reader/writer checks in Premiere;
- `smoke-jw-cc`: the `prep` smoke test.

## Builds (between projects)

| Date | What | Code | Result |
|---|---|---|---|
| 11-19 | **A reference video for a project** (the user: "add an input as reference video - a video provided while creating a new project, the new project needs to take reference from the already edited video and create timeline similar to the provided reference video"). A finished reel (a file or a link) given on New project or on Brief & plan; Videtor watches it (every cut, what each shot shows) and writes the plan itself: a slot per shot, as long as that shot, a burst of 1-3 frame shots as one flicker, each slot asking for footage that looks like the reference's shot (the fill compares CLIP pictures) and saying the kind of shot in words. With no music chosen the reference's sound plays and the cuts fall on its seconds; the user's own track counts the shots on its beat; "no music" keeps the seconds. After the footage is read the plan is written without a click when the project has a reference. The request to Claude names the reference, so a brief and a reference go together | `plan/reference.py` (new), `pipeline.py` (`reference`, `_plan_from_reference`, `cut` passes `like`), `plan/fill.py` (`like`, `LIKE_W`), `plan/shotmap.py` (`cut_on: seconds`, `reference`, `like`), `plan/settings.py` (`reference`; a plan in seconds counted on the user's track), `project.py` (action `reference`), `ui/server.py` (`api_reference`), `ui/static/`, `cli.py` (`videtor reference`), `tests/test_reference.py` | 456 tests. The nine evaluation projects' picks identical, `videtor eval` unmoved. **Measured on the user's own 11-15 BTS edit** (the reel as reference, its own shoot as footage, the read-back as truth): the slot shows the take the user used in 6 of 65 shots from words alone, 45 of 65 (69%) with the pictures compared at the weight kept (0.8); reading the reel takes 20 s. Clicked through in headless Chrome on a scratch app (the card, two refusals in words, the plan written in 21 s, a cut refused meanwhile, the shot list, a cut of 68 slots from the cache). **Not checked:** the XML in Premiere; a link as the reference (the fetch is the promos'); a reference on footage of another kind of shoot, where nothing says what "right" is. **Known limits:** cuts inside one take (the user's reel opens on seven jump cuts of one take: read as one 4.5 s shot, 75 shots found of about 100); words on screen, voice, grade and ramps are not copied; effects only with `--effects` (their XML is still unchecked in Premiere); no bucket is set from the app, so no keeper |
| 11-19 | **Why the cuts feel like 0.5-1 s shots with no flow** (the user's report; a debug, nothing changed on `main`). The refactor of 11-17 changed no pick. The plans ask for it: 71 of the 82 slots of the latest plan are one second or less, as in every cut since the Diwali verdict (the learnt rule "median shot 0.32 s; a hero moment gets a ramp, not a longer hold"). Nothing models a sequence: each slot is filled alone, the day's order is off unless a plan asks, sections are topics. SFX: a click on every cut of a flicker and every layer of a stack, so a flicker next to a stack is eight clicks in two seconds. Five fixes (pace per section with holds, the day's order inside a section, a story line per section, clicks capped and spaced, measures for pace and order) are built on the branch `pace-and-flow`, for an A/B against `main` | (the branch) | See "Waiting on the user" |
| 11-17 | **The implicit state machines made explicit, and the slow spots** (the user: "find all the implicit state machine in my app and fix them. review the architecture to look for any unoptimisations and create a fix. create a plan first and then spawn several subagents ... on opus"). The audit and the plan: `docs/review-2026-11-17.md` (eight state machines, six measured slow spots, six packages with one owner a file and written contracts). Six subagents on Opus 5.5 did the packages in parallel on the branch `refactor/state-machines`; integrated, checked and merged the same day. A job's status, where a project stands and what may be done to it, the cut as a document (edited or written), a slot's kind and a cut's kind, the fill's assignment, a feedback row's status and a document's kind, and a page panel's run are each said in one place now. A cut is 3 to 7 times faster; cutting a plan again loads no model; the Teach page's data comes in 0.04 s | `ui/jobs.py`, `project.py` (new), `ui/server.py`, `ui/static/*`, `pipeline.py`, `plan/slots.py` (new), `plan/edit.py`, `plan/build.py`, `plan/library.py`, `export/{proxy,preview,promo}.py`, `kb/{feedback,readback,renderback}.py`, `plan/fill.py`, `analyze/tags.py`, `teach.py`, `plan/{autolook,stabilize,evaluate}.py`, `cli.py`, `scripts/picks_snapshot.py` (new) | 445 tests (394 before). Held to recordings made before any change: the picks of the nine evaluation projects byte-identical, `videtor eval` unmoved, 12 past cuts rebuilt to identical XML, the app's 29 recorded answers the same field for field. The page driven in headless Chrome on a scratch app (two writers refused by name, the timeline, promos, Teach). Cut times: 32.3 s for the nine before, 11.6 s a first cut, 4.6 s a re-cut. **Not checked:** the app's own window; a real prep, read-back or learn through the new page (they write into `kb/` or need a drive); a crash of the app with a job running (the stop-what-was-left path is tested with a record written by hand) |
| 11-17 | **Promos: a making replaced the one before, and a failed run looked like a wrong result** (the user: "I created a clipping promo first ... then I created another edit promo. The last one disappeared ... the third time when I created a clipping promo, all three promo videos that it created were somehow the edit video"). From the app's job log and the folders: (1) one folder held one set, so the edits overwrote the clips; (2) the third run was given the same video by another form of its link (the watch page, where the first was a `youtu.be` share link), was not recognised, tried to fetch it again, and YouTube refused (a login check after a day of fetches): the run failed in 2 s, and the page went on showing the edits under it with nothing saying they were older; the fourth run, with the Chrome login, fetched the video a second time into a `-2` folder. Now: every making is a set of its own in the video's folder (`clips-vertical-30s/`, `edits-vertical-30s/`); a video is known by its id whatever the link's form, so it is fetched once; while a run is on, what is on the page is dimmed and said to be older; a run that fails says so in a box with the reason; a run that succeeds opens the set it made; the list names each entry (3 clips, 3 edits, the shape, the time). **Also: the Library page listed nothing** although a shoot had been sorted in the terminal on 11-07 (the user asked whether the sorting by wide shot, talking head and so on was in the app): sorted shoots are now listed by themselves, as projects are | `pipeline.py` (`promos`, `_promo_source`, `promo_sets`, `promo_set_name`), `analyze/link.py` (`key`), `ui/server.py` (`api_promos`, `discover`), `ui/jobs.py` (a job's result names the set it made), `ui/static/` (the Promos page) | 394 tests. The user's sequence replayed in headless Chrome on a scratch folder: clips, then edits (both listed, the new one shown, the first untouched), a link that cannot be fetched (the box says no promos were made and that those below are older), clips in another shape (a third entry). Against the real `out/` (read-only, a scratch app state): their two sets read "3 clips" and "3 edits" and play; the Library page lists the sorted shoot with its counts. Their `-2` folder (a second copy of the video, 68 MB, with the clips) is left as it is |
| 11-16 | **Promos out of a finished video, from a link or a file** (the user: "provided a link to a youtube or instagram video, create 30 second promos out of the main video - the best 30 seconds ... either clip 25-30 second videos from it or edit a 25-30 seconds videos from it. maybe 2-3 videos out of it. the ratio should be selectable"). `videtor promos <link or file>` and a **Promos** page in the app: the shape (vertical, portrait 4:5, square, horizontal), clips or edits, how many, the length; each promo an mp4 to post and a Premiere XML; Try another swaps one for the next best. The video is read once (cuts to the frame, faces, loudness and beats, credits and title cards, words on screen, what is said) and the parts are ranked by plain measures named with each promo; YouTube's most-replayed curve counts most. A wide picture is cropped around the faces shot by shot; a slide of words is shown whole. yt-dlp installed for the links with the user's yes (below) | `analyze/video.py`, `analyze/link.py`, `plan/promo.py`, `export/promo.py`, `pipeline.promos`, `cli.py` (`promos`), `ui/` (the Promos page, `/api/promos*`), `plan/settings.py` (a `portrait` 4:5 frame, also for projects), `pyproject.toml` (extra `links`), `tests/test_promos.py` and the pipeline and server tests | 390 tests. Real videos, all into a scratch folder: four from YouTube (a 3.5 min talk, a 4 min song, a 12 min film with dialogue, a 10.6 min film without) and two Instagram reels fetched without a login (VP9, re-encoded to H.264 on the Mac). The talk was taken as talk and its three clips ran from a sentence's start to a full stop (29, 29 and 26 s); the song and the films as music-led, cut to cut; the film's end credits, picked on the first try, are left out since; the talk's slide is shown whole, the speaker cropped around his face. A fresh 10-minute 1080p video: watched in about 20 s (decoding on the CPU's cores, 8 times faster than VideoToolbox here), Whisper 1-2 min when a voice is heard, 10-15 s a promo. The page was driven in headless Chrome: a set listed and played, Try another, a file, an Instagram link from paste to a 4:5 promo in 27 s, errors in words, no page errors. **Not checked:** the promos by ear or eye beyond contact sheets; the XMLs opened in Premiere; the page in the app's own window; a link that needs a login (`--browser`); Hindi or mixed speech (the English-only reading of openings is skipped, the rest is the same) |
| 11-16 | **A cut crashed with a KeyError** (the user: "a new project called 4 - got killed with a key error"; the first plan with a match-cut section made of three moments of one take, and seven slots naming moments of one long take). Two faults in the fill, both from one take being named by several slots. (1) A match-cut run refused a take twice; a slot left with no other candidate was skipped silently, the run came back shorter than its slots and was laid onto the wrong ones (the take of slot 21 on slot 19: `KeyError: (19, 7)`). Now a slot with no other take gets its own again, and a run that is not one take per slot is never applied. (2) Past the crash, five of the 23 slots came out empty, the reveal among them: the assignment gives a take to two slots at most. Now a slot whose takes the plan names gets one of them however many slots play that take, and when two of its uses sit within a second of each other the slot moves inside the moment the plan named, never to another part of the take | `plan/fill.py` (`match_run`, `fill`), `tests/test_fill.py` (both cases) | The same plan cut into a scratch folder: 23 of 23 slots, every named moment inside its seconds, 17.9 s. 367 tests. `videtor eval`: no guarded measure moved (past projects cut the same). The user's own project folder was not written to: Cut again in the app |
| 11-16 | **The timeline's music was choppy** (the user: "the song is a bit choppy. investigate"). Cause: the page ran the picture on its own clock and chased the music onto it, seeking the track whenever it was more than 0.2 s off. In the app's window (WebKit) the track starts 0.1-0.2 s late and wavers around that, so it was seeked again and again, and every seek of a playing track is heard as a break. Fix: the music is started where the playhead is and never seeked while it plays; the picture's clock leans to the music instead (a fifth of the gap each time the player reports a new time), and the track waits cued at the playhead when stopped. Also: a clip already waiting at its first frame is not seeked again when its turn comes, and a project's late answer no longer shows over the project opened after it | `ui/static/app.js` (`tlTick`, `tlSync`, `tlCue`, `loadProject`) | Measured in WebKit itself (a pywebview window in front, the music at volume 0, the same 16 s cut): before, 16 seeks of the music in 7 s with the track swinging 0 to -0.2 s against the clock; after, 0 seeks in 11 s and the picture within 0.02 s of the music from half a second in. In Chrome: 0 seeks, within 0.02 s (it was a steady 0.1 s late before, so cuts now sit on the music as they will in Premiere). Video seeks fell from 58 in 7 s to 22 in 11 s. A hidden or covered window gets no animation frames, so playback there cannot be measured: the test window has to be in front |
| 11-16 | **A timeline in the app to watch the cut and change it before Premiere** (the user: "an inline video playback setup ... with editing capability for a user to review the final generated timeline similar to how they would do it in premiere ... suggest changes in the app itself and then take the remaining of the project in premiere"). Review is now a timeline: a monitor, transport (space, arrows, cut to cut), the shots as blocks with stills under section labels, the music with its beats, a playhead to scrub. A selected shot can take a runner-up, be slipped in its take, have the cut after it moved, be replaced by a take found in the user's own words, be removed, and carry a note; Undo steps back; notes on the whole cut and a copyable request to have the plan revised from them. Edits apply at once and are made again by the next cut; the XML is written on request and the net change becomes feedback. **Also fixed, the track selection** (the user: "isn't working properly"): a file is now chosen with the window's own dialog (it used an AppleScript dialog that opens in Finder, behind the app's window), a chosen track is used at once without a second button, the card survives opening an interview or music-video project, an error on the Music tab shows there, and ranked tracks carry the file's real path | `plan/edit.py`, `export/proxy.py`, `pipeline.py` (`edit`, `proxies`, `options`, `note`, `write_xml`, `replay`), `ui/` (timeline, `_file` byte ranges, `/media`, `api_pick_file`), `kb/feedback.record_choices`, `cli.py` (`proxies`, `options`, `write-xml`) | 365 tests. Driven in headless Chrome over the DevTools protocol on a copy of a real 16 s cut (26 preview clips made in 37 s from 100 fps 4K): playback ran with the video within 0.01 s of the clock and the music in step; slip, moved cut, swap (its clip fetched by itself), more takes (12 found), replace, a note and undo back to the identical picks all worked, with no page errors; a slip reached the XML's in-point. The track selection was driven the same way: file, start, shape, a ranked row. Not checked: the app's own window (WebKit) playing the clips, and the native file dialog; both need the user's eyes |
| 11-15 | **The shape and the music are the user's to set in the app** (the user: the music step "is selecting a folder, and videtor auto selects the music. update to take user input specifically ... also, add a dropdown/checkbox for vertical or horizontal shoot. currently all the cuts come out as vertical only"). A project's `settings.json`: `frame` (vertical, horizontal, horizontal 4K, square) and `music` (the user's track with where it starts: best for the cut, from the beginning, from a second; no music; or left to the plan). The cut applies it over the plan: another track keeps every shot's beats and the length follows its tempo, `auto` lands the track's lift on the plan's reveal, no music keeps each shot's length. In the app: a Shape dropdown on New project; a **Shape and music** card on Brief & plan (saved as it is changed, with what it does to the cut said under it); the Music tab's ranking is now a table with **Use this track**; the Cut tab says the shape, the music and the length the cut will have; the request for Claude carries both. `videtor draft` follows them too | `plan/settings.py`, `pipeline.cut`, `plan/evaluate.py`, `plan/music.ranked`, `ui/` (`api_settings`, `api_music_ranked`, `_requirements`), `cli.py` (`draft`) | 350 tests. On a copy of a real 16 s vertical arrival cut: set to horizontal with another track on `auto`, the XML came out 1920×1080 (4K takes at 50%, no crop), on that track from the start that puts its lift on the reveal, 16.56 s long instead of 16.0 s (the new tempo). The pages were looked at in headless Chrome; not yet clicked through by the user. `scripts/search_sheet.py` repaired (it imported a helper the pipeline move removed) |
| 11-12 | **The app as the one gateway for teaching, and learning from any finished job** (the user: "create an iterative loop such that in the future any project I do, you can learn from them ... the videtor ui should be the only interface user should provide inputs for iterative training"). A **Teach Videtor** page: (1) finished jobs of Videtor cuts (a finished video in the project's folder is found and read; verdicts; a badge counts what waits), (2) work cut by hand (name, kind of work, the finished video or the Premiere XML, the footage folder), with the edits given before listed with the drive each needs, (3) a style from finished videos alone, (4) rules in the user's words per kind of work, and what Videtor knows per kind of work with how the last learning went. `videtor teach project / edit / style / pending / status`. A job cut by hand needs no picks: its shots are found in the footage (keyframe shortlist, then the takes it plays and a sample of the rest read in full) and it is kept as a finished job the keeper learns from. A finished job of a Videtor cut joins the evaluation set with what the user did as its truth. Also in the app now: every project under `out/` is listed (so cuts made in the terminal can get a verdict), Cut runs the interview or music-video cut when the project is one, a preview of the cut plays in Review, the finishing steps are shown, a sideways camera can be turned from Footage. The three verdicts the user gave in words (Guru good, colourstone good, Diwali not good) are recorded with their words | `teach.py`, `pipeline.py` (`teach_project`, `teach_edit`, `teach_style`, `read_pending`, `ingest`), `kb/renderback.py` (`shortlist`, `read_by_hand`), `kb/feedback.py` (`record_edit`), `kb/render.py` (`read_files`), `plan/evaluate.py` (`add_project`, feedback as truth, bucket override), `plan/keeper.py` (`index_roots`), `ui/` | 341 tests. A job cut by hand, on real footage with no picks given (reel A's preview render against the 87-take sports shoot): the keyframe shortlist kept 54 takes and held all 46 the job used; 39 shots placed, no wrong take, 36 within a second of the true moment (flicker sub-shots are not separated). Using an input already given: a brand-social keeper from the Guru edit alone was fitted and **refused by the evaluation** (Guru's right take 0.613 -> 0.581): one edit is not enough for that kind of work, and the page says so; the other six brand-social edits wait for their drives. Found and fixed on the way: the app's project view imported helpers the pipeline move had removed (it failed for a project with a valid plan), and the keeper crashed on a knowledge base with no manifest. The page was looked at in headless Chrome; not yet clicked through by the user |
| 11-11 | **The pipeline, the evaluation, the feedback loop and the automatic judgement** (the user, after the architecture review: "proceed with 0-4. let other things be as it is. take a call on mcp. leave the plan for app unanswered"). **Phase 0:** a slot's own `effects` reach the cut and flickers draw none; the app's server checks Host and Origin and needs a per-launch token on every POST; a cut never decodes a skipped take, and only the named takes when every slot names its takes. **Phase 1:** `videtor eval` cuts seven past projects from the cache and measures them against the user's own edits and on their own; `kb/eval/baseline.json`. **Phase 2:** every step in `pipeline.py` (prepare, cut, look, cut-interview, cut-mv); steps report events (the app reads JSON lines, not sentences); `decisions.json` keeps second-look swaps across cuts; unknown shot-map keys are reported; the rounds 1-3 planner archived; **the MCP server retired** (my call: a Round 4 copy nobody uses; the app replaced it). **Phase 3:** `videtor readback` reads the finished video against the picks (no export), `videtor verdict`, the user's swaps as feedback, one store in `kb/feedback/`, taste per bucket, the keeper learns from finished jobs, `videtor learn` keeps a candidate only when the evaluation passes; the app's Review step has the verdict, the finished video and Learn. **Phase 4:** `inventory.json` (what the shoot holds), `videtor draft` asks only for that, the filler settles neighbours (repeats, look-alikes, day cut to night) and keeps flickers in their light, an automatic second look swaps soft, badly lit or shaking picks; `videtor preview` renders the cut to watch without Premiere | `pipeline.py`, `events.py`, `plan/evaluate.py`, `kb/feedback.py`, `kb/renderback.py`, `plan/inventory.py`, `plan/autolook.py`, `export/preview.py`, `plan/fill.py`, `plan/draft.py`, `plan/taste.py`, `plan/keeper.py`, `ui/` | 328 tests. The move was checked by cutting seven past projects before and after: identical `picks.json` and XML. The evaluation puts numbers on the verdicts: the first Diwali cut matched 11% of the takes the user used (4% of the seconds), Guru 58%. After phase 4 Guru is at 61% with its day-to-night cut gone and nothing worse anywhere. The render read-back on a real 28 s reel: 41 of 41 shots kept on an untouched preview render; 8 of 8 known changes right (2 runner-up swaps, 2 other takes, 3 slips, 1 removed) through a grade, a 10% push-in and a black-and-white stretch, with no untouched slot mislabelled. `videtor learn` end to end on a scratch knowledge base: candidates fitted from nonsense edits were refused by the evaluation, as they should be. Not yet seen by the user: the app's new Review controls. Not yet happened: a real finished job read back (the loop has only run on test renders) |
| 11-11 | **The sports library** (the user: learn sports content from these reels "in a reinforcement-based environment" and add a few of the Premiere special effects to the output). `videtor kb renders <folder> --bucket sports-promo` reads finished renders alone (cuts, static/flash/black transition frames, shot lengths, black and white, moves, grade, beats and accents, CLIP labels); `videtor kb train sports-promo` learns a cutting policy in an environment made from the reels (each reel's shots as stand-in footage, a reward against the real reel, cross-entropy search, leave-one-out check); `videtor draft` writes a first shot map from it; `"effects": "sports-promo"` on a shot map draws flash and static overlays, whip, spin, blur, push-in, pull-out, punch, shake, echo and black and white at the learnt rates; `"cut_on": "accents"` moves cuts onto the music's strong onsets; `videtor fx-test` writes a sequence with one clip per effect for checking in Premiere | `kb/render.py`, `plan/fx.py`, `plan/gym.py`, `plan/draft.py`, `analyze/beats.accents`, `plan/shotmap.py` (`effects`, `cut_on`), `export/xmeml_writer.py` (rotation/centre keys, Gaussian Blur, Opacity, blend modes, labels, markers), `plan/build.py` (effects, echo track) | Eight reels read in 61 s on 8 workers. 83.7 cuts a minute, held shots 0.5 s, transitions: cut 66%, flash 14%, static 7%, overlay 4%, whip/spin/black 3% each; black and white 10% of the runtime; push-in 28%; cuts sit on the beat at chance (28% v 26%) but on louder onsets (1.23×): they follow accents. Held-out reward (0 = the reel's own style; one real reel scores about -8 against another): old XML profile -13.25, the reels' medians -8.46, trained -8.01 (better than the medians on 5 of 8). Policy: held 0.50 s, open 2.05 s, close 1.04 s, 1.2 flicker bursts a minute, 51% of cuts on accents. End to end: the Diwali v2 map re-cut from the cache with `"effects": "sports-promo"` (scratch folder, not the delivery) drew 23 push-ins, 14 flashes, 5 statics, 4 whips, 3 spins, 4 black-and-white clips, all keyframes inside their clips, XML parses. 330 tests. **Not yet checked in Premiere**: `out/fx-test/fx-test.xml` |
| 11-07 | **The footage library**, `videtor library build \| learn \| status`: a shoot sorted into wide / close-up / talking-head / interview-bite / b-roll folders (links to the clips), a contact sheet per class, every interview bite as a Premiere sequence (`interview-bite/bites.xml` + `bites.md`), `library.md` with the reasons and the clips worth a look. Rules first; `learn` reads the user's folder moves back as labels and fits a model the next build uses. Keyframes only, every core, no Claude at run time | `plan/library.py`, `analyze/sparse.py` (the keyframe-only map, `cache/sparse/`), `analyze/parallel.py`, `kb/library/` | TPL, both folders the user named (786 clips, 5.9 h): the scan took 11 min 15 s on 8 workers, none failed; Whisper on the 159 speaker clips (136 min of audio) 12 min 45 s the first time; a rebuild with the caches 5 min 49 s. Sorted: 77 wide, 158 close-up, 83 talking-head, 68 interview-bite, 400 b-roll; 903 bites (132 min, 751 with clean edges); 115 clips flagged for a look; 58 of the 61 Master Cam takes read as talking heads (the other 3 have no speech or a walk-in). 15 clips are copies (Sam's Card 3 holds Master Cam files). `out/library/tpl/` |
| 11-07 | **The UI reorganised around the user's journey** (the user found the UI confusing and asked for the journey): screens renamed (Projects, New project, Library, Activity; steps Footage → Brief & plan → Music & search → Cut → Review & export), project cards with a status line, a stepper with one "Next" action from the furthest step reached, a new project opens at once (brief while the footage is read), progress as words + bar + time left (raw logs under Details), the plan shown as a shot list (JSON under Edit the plan), a copyable request for Claude Code, drive checks before a cut, "Read the footage again" after a failure, Mac notifications when long work ends, advanced options folded away, an export box in Review | `ui/jobs.py` (`note`, `eta`, `on_finish`), `ui/server.py` (`_status`, `_steps`, `_cut_needs`, `api_scan`, `api_reprep`), `ui/static/` | 318 tests; the page script parses under JavaScriptCore; checked on the real smoke project (a terminal-made project with a cut reads "Cut ready for Premiere"). The journey map with the proposal and decisions D1-D5: https://claude.ai/artifact/Sfuj5WCoBeKHtEoidt4wxX |
| 11-07 | **Sparse-first prep and cut** (after the user closed the decisions): `prep` now reads a shoot from its keyframes (the library's pass), sorts the takes into the library's classes (`out/<name>/classes.json`) and writes the overview in minutes; `cut` narrows the pool to the shot map's `classes` / a slot's `class` (plus named takes and the speech classes for bytes) and decodes only those takes in full, then fills as before. `search` falls back to the keyframe map. `--dense` keeps the old way | `cli.py` (`prep`, `_takes_for_cut`, `search`), `analyze/sparse.py` (`as_takes`, `copy_overview_stills`), `plan/library.py` (`classes_for`, `candidates`), `plan/shotmap.py` (`classes`, `class`) | Smoke test on Jatin's Card 1 (68 takes, 18 min): `prep` 17 s with the scan cached; a 4-slot map with `classes: ["close-up"]` decoded 52 of 68 takes (8.4 min of HEVC) in 107 s on 8 hardware sessions and filled every slot; XML written. The drive now stays connected until the cut |
| 11-07 | **The UI** (the user asked for a user-facing interface, "as lightweight as possible without adding any extra moving parts", inclined to desktop, "this can replace mcp"; recommended and built: a local web page in Videtor's own Python shown in a pywebview window, no Electron, no framework): `videtor ui` / `scripts/Videtor.command`. Prep with input and output folder pickers, the project (overview, shot types, brief, shot map with validation, music ranking, search, cut, second look by clicking a runner-up, Reveal in Finder), the library (build, sheets, move a clip's class, learn), jobs. Prep and cut are separate processes; a cut pauses running preps and gets the machine, they resume after; a prep started during a cut waits; a prep paused into failures reruns once | `ui/server.py`, `ui/jobs.py`, `ui/static/`, `cli.py` (`ui`), `pyproject.toml` (`ui` extra) | 13 tests (real SIGSTOP/SIGCONT on child processes, the API over a live server). Driven on the smoke project from the API against the real repo: search, cut (52 of 68 takes, from the cache) and a second-look swap; the prep call was checked by the server test, since the drives were unplugged by then. Then, for a non-technical end user: every Choose button opens the Mac's own folder dialog in the browser too (AppleScript `choose folder`, compiled-checked), and `scripts/make-app.sh` made `~/Applications/Videtor.app`, a double-clickable launcher with an icon that installs the window package on its first run and logs to `~/Library/Logs/Videtor.log`. Then a "How this works" guide on every page and tab (the steps in order, times, what comes next; open until closed, remembered per page) and a five-step flow strip on the Prep page. The pywebview install was blocked by the session's permission rules, so the window waits for the user to run `uv sync --extra analyze --extra ui`; the page runs in the browser meanwhile |
| 11-07 | **Parallel passes**: workers sized to the Mac (performance cores, memory, `VIDETOR_WORKERS`), ffmpeg thread caps, the footage map's face and hand detections in up to 4 worker processes with CLIP in the main process, steadying measured side by side, `prep` and `orient --workers` default to the machine instead of 3 | `analyze/parallel.py`, `analyze/footmap.py`, `plan/stabilize.py`, `cli.py` | Measured (`docs/architecture.md`): a 4K XAVC H.264 software decode is CPU-bound (one ffmpeg fills 10 cores), so the footage map can't run faster than about 2.5-3× real time whatever the worker count; thread caps gain 15%, HEVC goes to the hardware decoder many at a time, and the content stage (49 ms per sample, the hand landmarker most of it) now runs 4-wide. The keyframe-only pass is 8-10× cheaper, which is why the library reads 5.9 h in 11 min |

## Waiting on the user

00. **Third BTS shoot (11-17)**: plan written, not cut. Its footage was read as `bts-3` (the app's `clip` project had
   reused the 11-15 shoot's index, see the gap below); the plan in the app's project folder names `bts-3::footage`, and
   that folder's overview/classes/inventory were replaced by this shoot's (the old ones were byte-identical copies). The
   user cuts it from the app. Shaped on the user's own finished 11-15 BTS edit, 47 s; the three bytes ("rolling…action",
   "kaat", "chalo guys") and a laugh were read from Whisper and loudness, not heard. Draft and scripts: `out/bts-3/`.

0. **Promos (11-16)**: try the Promos page on a video of their own (a talk and a music-led one), say whether the parts
   picked are the ones they would pick, and import one `promo-N.xml` in Premiere. Their notes decide the weights
   (`plan/promo.py`), which are hand-set so far.
1. **Verdicts** on Check Hai, Alpha Summit and Engagement (and Diwali v2). When they fix a cut in Premiere, read
   it back: `.venv/bin/python -m videtor.kb.readback out/<name>/picks.json --live` (say so before touching
   Premiere) or `--xml <export>`, then `videtor kb learn`. No real feedback is recorded yet: `kb/feedback/` is
   empty and there's no `kb/taste.json`.
1b. **261114ac arrival (11-15)**: the shot map is written and validated (`out/261114ac-*/shotmap.json`). The user cuts
   from the app. Then the second look, `report.md` and the verdict. Bucket `sports-promo` (inferred: a player's hotel
   arrival). 9:16, 16.0 s, Burn4Me from 81.13 s (drop on the handshake at 4.16 s), flash rate 0.35, stabilised;
   entering, meeting, posing, close. Every slot names its takes from a stills sheet.
1c. **logan (11-15)**: the second arrival from the same shoot and brief. Shot map written and validated; the user
   cuts from the app. Daytime, with family; he films on his phone walking in (the brief's "clicking photos").
   9:16, 16.0 s, BLAGE - Go Down from 110.57 s (drop on the greeting at 4.04 s), flash rate 0.35, stabilised; C6207 skipped.
1d. **261115 arrival (11-15)**: the third arrival, same brief plus "only the guy in the grey t-shirt and cap". The project
   folder is on the drive (`Paritosh_4/261114AC /pf/xml/261115-*`), not in `out/`. Shot map written and validated; the
   user cuts from the app. The other man's takes (C6233-C6240, C6249) are in `skip`. Ariel Dahan - On up on Winning
   from 32.37 s (drop on the check-in at 4.16 s), 16.0 s, flash 0.35, stabilised.
1e. **clip (11-15)**: a BTS cut of an ad shoot on the same drive (`Paritosh_4/261114AC /pf/xml/clip`, key `clip::footage`).
   Brief: "45-60 sec BTS cut, fast and flashy, timelapse, bytes like cut/action, start on prep, a byte, then the music;
   slow motion on the beat, stabilise, lots of SFX". Shot map written and validated; the user cuts from the app. Then the
   second look, `report.md` and the verdict. No BTS bucket card: planned as `event` with `keeper_w` 0.3. 9:16, 49.3 s,
   49 slots; Ariel Dahan - On up on Winning from 18.53 s (35-beat prep opening, drop on the cut after "action!").
   Bytes from local Whisper: the AD's "silence, going for take" and "camera rolling, set and action!", "dog is here,
   bro", "let's take a picture". Timelapse = set-build takes ramped to 600%/400% (gap below). Flash 0.35, accents,
   stabilised, classes b-roll/close-up/wide/talking-head. Every slot names 2-10 candidate takes from the overview.
1g. **clip, second shoot (11-16)**: a BTS cut of another ad shoot on Paritosh_4 (`Paritosh_4/<shoot>/xml/clip`, footage
   `M4ROOT/CLIP`, 349 takes, 1080p50). It reuses the name and key `clip::footage`, so `kb/footage/clip.json` now indexes
   this shoot, not 1e's. Brief: "BTS fast cut 45-60 s: 2-3 s of people meeting, a byte, camera/equipment/workers, 2-3 funny
   bytes, split screen and match cuts, a few bytes, everyone on set; fun vibe". Horizontal 16:9 (settings.json). Shot map
   written and validated in the project folder; the user cuts from the app. Bucket `bts` (no card or keeper; planned from
   the event card and notes). 48.4 s, 62 slots, Ariel Dahan - On up on Winning from 30.743 s, drop at 5.8 s right after
   the AD's "Okay silence!". Bytes: "say something nice about me", "top 5 worst creators ever", "Max 4 is him only",
   "Kaju Katli bites are crazy", "We are a PTA meeting", "camera rolling, set and… action!", "let's take a picture" (four
   from C5594). A monitor match-cut run; two `stack` slots stand in for split screen. Then the second look, `report.md`, verdict.
1i. **clip stringout (11-16)**: the same shoot as the second clip (folder `Paritosh_4/<shoot>/xml/2/clip`, same key
   `clip::footage`). Brief: "take all the footage where people are talking to themselves and to camera and make a timeline".
   Horizontal 16:9, no music. All 349 takes transcribed (local Whisper, Hindi, ~40 min), spans kept where the speech band
   shows a voice (level and swing over the take's floor), then sorted by eye from a still per span. Shot map: 146 byte slots,
   12.9 min, in three sections in shoot order: to camera (16, 2.0 min: the two testimonials, the C5594 group, laughs to the
   lens), among themselves (71, 6.7 min: crew and cast chatter, the director briefing), scene takes (59, 4.2 min: the ad
   being shot, action to cut). 12 portrait spans (cropped in 16:9). Work in `out/clip2/work/`. Waiting on the user's cut.
1f. **combined-players (11-16)**: the four arrivals' players in one cut (`Paritosh_4/261114AC /pf/xml/combined-players`,
   key `combined-players::footage`, footage in `Paritosh_4/combined players`). Brief: "combined vertical 15-20 sec cut
   with flashes, slo-mo, time remapping, stabilise; players entering, meeting, clicking or posing for photos". Shot map
   written and validated; the user cuts from the app. Then the second look, `report.md` and the verdict. Bucket
   `sports-promo` (inferred). 9:16, 19.0 s, 28 slots; Burn4Me from 80.18 s (top ranked again; drop at 85.31 s on the
   first handshake), flash 0.35, accents, stabilised. Five players, each in every section; every slot names its takes
   from 8-stills-per-take sheets (`work/takes-*.jpg`). "Clicking photos" = C6278 (a woman photographs a player on her phone).
1g. **data (11-16)**: a second combined arrival, three other players (`Paritosh_4/2/xml/data`, key `data::footage`, footage
   in `Paritosh_4/2/data`). The same brief at 25-30 s; frame vertical set in the app, no track. Shot map written and
   validated; the user cuts from the app. Then the second look, `report.md` and the verdict. Bucket `sports-promo`
   (inferred). 27.8 s, 39 slots; BLAGE - Go Down from 109.69 s (second ranked; drop at 114.66 s on a handshake), flash
   0.35, accents, stabilised. No one takes photos in this footage: "posing" is to the lens. Closes on a hand over the lens (C6400).
   The app then made the project again in `out/data` (same key, overview and settings; no brief there): the same brief and
   shot map copied in and validated. Cut from `out/data`; the drive copy is the first draft.
1h. **3 (11-16)**: one player's hotel arrival, face hidden until the reveal (`Paritosh_4/3/xml/3`, key `3::footage`, 21 takes,
   50/100 fps). Frame vertical; the user's track (IamDayLight - I Think That You Should, instrumental), in-point left to us:
   65.92 s, the drop at 74.63 s on the reveal (C3600, cap lifts to the lens; punch ramp, flash). The 20 hidden beats carry
   `"effects": []`, end on a slow hold and 2 black beats over the track's own silence (72.1-74.2 s). 19.4 s, 44 beats,
   20 slots. No car in the footage, though the brief asks for one: we said so. Shot map validated; the user cuts from the
   app. Then the second look, `report.md` and the verdict.
1a. **diwali-test (A1)**: the shot map is written and validated. The user cuts from the app. Then do the second
   look, write `report.md` in the project folder, and record the verdict. The plan:
   - Brief: "fast paced party edit for diwali, under 60 seconds". Bucket `event` (inferred).
   - Classes: close-up, b-roll, wide and interview-bite. The photo-wall arrivals from the second camera sorted as interview-bite.
   - Music: "Mauja Hi Mauja", the only track in the footage folder, from 154.877 s. Chosen by measurement.
   - The song's own break (164.0-165.4 s) carries the black. The reveal sits on its hit at 165.40 s, half a beat
     before the tracker's beat.
   - The flow follows the event card: decor flicker, sign, card flicker, 3 ramped arrivals, a board run, the
     build, black, then the drop (half-beat cuts, punches, 2 stacks, crowd bed), the casino break, the prism
     finale, and a slowed group at the board.
2. **The offline plan** (below): approved. On 2026-11-07 the user wrote "all the open decisions are checked and
   can be closed. move ahead." Step 1 follows the library's cut integration.
3. **Bucket material:**
   - Their own XML exports of the colourstone (interview) and Check (music video) edits; the footage is
     already mapped.
   - Then 3-5 edits per bucket: XML, raw footage folder, one line of brief.
   - Confirm the starter bucket list: event, brand-social, sports-promo, interview, music-video, travel-vlog,
     ad, maybe a celebrity/fashion BTS bucket split off for Guru.
4. **A Premiere check** of `out/guru-25s-brief/guru-25s-brief-sfx.xml`: do the SFX levels and `#` in file
   paths come through?
5. **Claude Desktop** (closed by the user on 2026-11-07, nothing pending; Claude Code desktop is the platform in
   use) was installed by the user on 2026-11-01, who said they set up the MCP and made a workflow.
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
6. **GitHub visibility** (closed 2026-11-07: the user decided, the repo stays public): `github.com/ashtava/videtor`
   is public (checked 2026-11-01).
   - It holds the user's Premiere XMLs (`.xml-exports/`) and footage indexes (`kb/footage/`) with client file
     names and paths, and the auto-push publishes every change.
   - Raised with the user, who decides whether to make it private.
7. **The TPL library** (`out/library/tpl`, built 11-07): the user looked and said "the current segregation is
   perfect, might need very little changes but can proceed with it". So `learn` ran on the folders as they are:
   786 labels in `kb/library/labels.json`, a model with cross-validated accuracy 0.884 in `kb/library/model.json`.
   When a clip is wrong, move its link and run `videtor library learn out/library/tpl` again; the next build
   keeps it there.
8. **UX decisions for the UI** (asked 2026-11-07, after the user said the UI was confusing "to me as well as a
   dev"). The user journey, its diagnosis, the changes shipped and a proposed five-step journey with wireframes
   are on a private page: https://claude.ai/artifact/Sfuj5WCoBeKHtEoidt4wxX. Waiting on the user's comments and:
   - D1 who writes the plan: Claude from inside the app, offline step 1, or both (recommended: both);
   - D2 Library as its own page or folded into the project (recommended: folded in);
   - D3 where projects live: the repo's `out/`, `~/Movies/Videtor` (recommended) or next to the footage;
   - D4 the day-to-day user: Paritosh, an assistant (recommended to design for) or a client;
   - D5 the hand-off: import by hand (recommended until the bridge is proven) or one click over the bridge.

9. **The effects check in Premiere** (asked 2026-11-11): import `out/fx-test/fx-test.xml` (File ▸ Import; the
   media is one of their reels in Downloads) and say which of the 14 markers look wrong: plain, push-in,
   pull-out, punch, shake, whip, spin, blur, flash, static, echo, and three black-and-white variants (only one
   may survive the import). Until then the effects are built from what Premiere's own exports and translation
   table show, not seen in Premiere. Also: their own static and flash overlay clips, if they have them (the
   reels reuse one 3-frame static), to use instead of the generated ones; and whether speed ramps and the text
   cards should be learnt too (neither can be read from a render reliably).

10. **The architecture review** (2026-11-11): `docs/review-2026-11-11.md`. Phases 0-4 are built. Left open by the
   user: phase 5 (a workspace outside the repo, content ids) and D1 (who writes the plan from the app).
11. **Teaching, from the app** (built 2026-11-12; the row above). What the loop needs, all given on the Teach Videtor
   page (the app must be quit and reopened once to load it):
   - for each finished job, the export saved in its project folder (or chosen on the page), and Good / Not good;
   - the drives of the edits given before, one at a time, to learn what was kept: Adidas (Adidas), Coca cola
     (Paritosh_4), JW (Extreme SSD), Zomato (AJAN SSD4), Vivo 1 and 2 (Paritosh_1), Gujarat Giants (AMAZONSSD), TPL
     (Paritosh_3). Each has a "Read its footage and learn" button once its drive is connected;
   - any job cut by hand, as it is finished: its video (or XML) and footage folder.
   The two sports reels (S1, S2) are still as delivered; a re-cut was offered, not done.

## The offline proposal (2026-11-01, approved 2026-11-07)

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
  - Paritosh_2 `videos` (sports): `untitled folder`;
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
| sports-promo | Gujarat Giants, TPL/Leander; eight rendered reels (2026-11-11) | `kb/profile/sports-promo.json`, `kb/renders/sports-promo.json`, `kb/policy/sports-promo.json` | none | `kb/buckets/sports-promo.md` |
| event | Diwali Party | `kb/profile/event.json` | `kb/keeper/event.json` (cross-validated AUC 0.76) | `kb/buckets/event.md` |
| wedding | none (the engagement cut has no user edit yet) | none | none | none |
| interview, music-video | none (their colourstone and Check exports would seed them) | none | none | none |

Build a card with `videtor kb breakdown <xml> --footage <folder>`, `videtor kb keeper <bucket>` (needs the raw
footage mapped) and `videtor kb bucket <bucket>`.

## Known gaps and backlog

Not built; the source is in brackets.
- **How long each shot is held: who decides** [11-19, the user: "how would you decide what shots to be a burst of 5-6 shots under a
  second or two and what shots to be elongated to 2-3 seconds ... a rule-based engine or let claude define ... how about a skill"].
  Long-term plan, in this order: (1) measure the user's rhythm as structure, not as one median: from their XML exports and read-backs,
  how long runs of quick shots are, how often and where a hold comes (after a run, on the music's downbeat after a lift, at a section's
  start), and what is held (a face to camera, a move that completes, a byte) against what is burst (details, look-alike frames);
  (2) a skill for Claude ("phrase the cut"): it decides run or hold per moment from the music's sections and from strips of frames
  (a take's motion, not one still), with the measured structure as its guide; (3) rules only as a check on the plan (no stretch of
  N seconds without a hold, no hold on a take whose move does not finish) and as measures in `videtor eval`; (4) a reference video
  (built 11-19) borrows the rhythm of a reel outright. Not a rule engine that writes the rhythm: rules give the 0.32 s median again.
- **Reference video, next** [11-19]: jump cuts inside a take are not seen (kb/render's cut detector compares pictures); the app sets
  no bucket for a reference plan; a link as the reference is untested; the reference's effects are only listed, not made.
- **Split screen** [11-16, project 4's brief]: no shot-map option for two or three shots side by side in the frame. Project 4's
  plan stands a `stack` (smaller shots popping on top) in for it. Asked again by the second clip brief (11-16), same stand-in.
- **Portrait takes in a horizontal cut** [11-16, clip second shoot]: 70 of 349 takes are portrait (to-camera bits); in 16:9
  they are cropped hard (fill penalises them), with no pillarbox or blurred-background layout. A split screen of two portrait
  takes would use them well.
- **Whisper's language detection on set audio** [11-16, clip second shoot]: auto-detect read Hindi/English chatter as
  Korean, Romanian, Arabic; `language="hi"` fixed it (English lines came out in English). Nothing passes a language yet
  (library, `teach`); about a minute a take while other work shares the machine. Of 26 likely takes, 8 had usable lines.
  The stringout (1i) ran all 349 takes in ~40 min; Hindi lines come out garbled, so an audio check decided what is speech.
  No stringout mode: a "timeline of all the talking" is written as byte slots by hand-run scripts (`out/clip2/work/`).
- **Two shoots, one project name** [11-17, third BTS shoot]: the app names a project after the footage folder, and a Sony card's
  folder is always `M4ROOT/CLIP`, so a new shoot's project became `clip` again. Its prep reused `kb/footage/clip.json` (the
  11-15 shoot) without reading the new folder: the new project's overview, classes and inventory describe the old shoot. The
  key, `kb/footage/<name>.json` and `kb/feedback/<name>.json` should not be shared by two folders (prep should refuse, or name
  the project after the folder above `M4ROOT`, or ask for a name).
- **BTS** [11-15, clip]: no `bts` bucket card or keeper (planned as `event`). No timelapse move: a slot ramps a take to
  400-600% instead (no frame blending or interval sampling). No SFX density knob for "lots of SFX".
- **After the audit of 11-17** (`docs/review-2026-11-17.md`, part 4): checks that click through the app must use `scripts/scratch_app.py`
  (a test app served from the repo wrote into the real `kb/` twice that day; both files put back from git); the page still spells a job's status for its words and for Pause/Resume; `fx`, `sfx`,
  `second_look` and `moves` read a pick's `pieces`/`layers`/`black` themselves; two speed-ups were held back because they would
  change the order floats are summed in, and so could change a pick.
- **Promos** [11-16]: the choice is by measures, not meaning: no reading of what a sentence says, no speaker
  recognition (two people in a wide shot: the bigger face, or both when they fit the crop), one crop per shot (a
  person walking across a shot is not followed), no captions, no blurred background behind a picture shown whole.
  The weights are hand-set and judged on four videos. Words sung and words spoken are told apart by pauses in the
  sound: a talk over loud music can be taken as music-led (the page says what it took the video for; Advanced
  overrides). A promo is not yet editable on the app's timeline (the XML is). The voice of a `byte` clip has no fade
  in the XML (the mp4 has). Edits of a talk are jump cuts with nothing to cover them. Instagram may ask for a login
  (rate limits); YouTube did on 11-16 after a day of fetches, and the user's run with the Chrome login went through. yt-dlp needs updating when sites change (`videtor promos --update`, a button in
  Advanced).
- **The timeline** [11-16]: it shows what plays when, not the finish: no effects, flashes, sound effects, steadying, stacked
  layers or music fade. Flickers, ramps and bytes keep the plan's timing (swap only). A shot's length changes only by moving a
  cut or removing a shot; there is no insert, no reorder by dragging, no ripple trim, and a moved cut does not snap to the beat
  ticks. More takes come only from footage already read in full. Preview clips of 100 fps 4K take about 1.5 s each. Undo
  reaches back to the last cut. Playback was measured in WebKit (a test window), not yet judged by ear in the app itself.
- **Shape and music** [11-15]: a plan written for vertical and cut horizontal keeps its shots' wording (a "close-up" framed
  for 9:16 may sit loosely in 16:9): for a real change of shape, ask for the plan again. An interview piece and a music
  video keep their own frame and music (their plans set them). Changing the track changes the cut's length; there is no
  option to hold the length and drop beats instead. A track's in-point can't be auditioned in the app.
- **Names in tracked files** [11-15]: a project's name becomes file names under `kb/footage/` and `kb/feedback/`, which are
  tracked and pushed. Projects named after people or clients in the app therefore publish those names. Raised with the
  user; nothing changed yet.
- **Teaching** [11-12]: a past edit's kind of work can't be changed on the page (it is in `kb/manifest.json`). Learning
  from a job cut by hand misses flicker sub-shots and cuts between look-alike shots (9 of 46 takes on the test), finds
  moments to about a second, and takes about two minutes for a 28 s video. A style from one video has no policy (two are
  needed). The plan from the app (D1) is still the copy-and-paste request; `videtor draft` is not on the page, by the
  user's choice to revisit. Versions to choose between, other aspect ratios from one cut and captions are not built.
- **The loop has not turned on a real job yet** [11-11]: read-back, feedback and learning are tested on preview renders with
  known edits and on synthetic footage. The first finished export will show how a real grade, titles, speed ramps and
  Warp Stabilizer read. Limits already known: a slip inside a take that looks the same throughout reads as kept; source
  times are good to about a quarter second (the footage map's spacing), so a few frames of slip is not seen; a flicker
  counts as one choice.
- The inventory's labels are CLIP's: concrete kinds of shot read well, but an action camera's sky read as "sunset" and
  drills as "bowling" on the sports shoot. A draft is a first plan, not a brief read. `"order": "shoot"` uses file
  names as the order of the day (wrong across two cameras) and made neighbours worse on the one draft tried: off by
  default. [11-11]
- The evaluation set has two projects with the user's own edit (Guru, Diwali twice). The guarded measures against a
  real edit rest on those; every finished job read back should be added to `kb/eval/set.json` with its feedback as
  the truth (not built: `evaluate` reads an XML, not a feedback file). [11-11]
- Neighbour settling cannot move a slot whose map names one take, and in a pool about the size of the map there may be
  no free take to move to. [11-11]
- The commands not yet on events: `library build/learn/status`, `music`, `search`, `orient`, `finish` still print
  sentences (the app matches the library's). `orient`, `finish`, `kb ...` are still written in `cli.py`. [11-11]
- Not done from the review, by the user's choice: phase 5 (workspace outside the repo; take ids from content, so a
  renamed drive keeps its cache) and D1. Also open: the per-slot decode shortlist from the keyframe map, hand-set
  weights in one table per bucket, the embedding width read from the map, tests run by the push hook. [11-11]
- Black and white can't be written for sure: no desaturate filter in Premiere's FCP import table; Iris labels and
  a finishing list cover it until `fx-test` shows a variant that survives. [sports]
- From a render, speed ramps, text cards, graphics (silhouettes, title animations) and the grade's LUT can't be
  learnt; the notes carry them. A colour shot turning black and white is measured but not yet a cut effect. [sports]
- The gym's reward measures style, not storytelling; with eight reels, the trained policy beats the plain medians
  by little (-8.01 v -8.46). More reels, or the user's verdicts on sports cuts, should move it. [sports]
- Grade, LUT, Warp Stabilizer and the "Flash 2" Lumetri flashes can't travel in xmeml. The grade is applied by
  hand with an adjustment layer; `videtor finish` over the bridge is unsafe. [B1, B3, B4]
- Keyframe steadying removes about 50-65% of shake, below Warp Stabilizer quality. [B4]
- The riser `ShortRiser5` peaks 0.9 s in. The user starts it on a cut; the placer aligns its peak to the drop.
  [Diwali v2]
- Still photos can't be placed. [Diwali v2]
- `check_shotmap` (MCP) counts half beats as 0 in its "slots ask for N beats" warning and per-section beats.
  The timing it prints is right. [A1]
- A drop off the beat grid is missed. Mauja Hi Mauja hits at 165.40 s, but the beat tracker and the 0.5 s RMS
  put it at 165.74 s or 165.19 s. It was found by hand at 50 ms; `music`/`check_shotmap` could report hits at
  that resolution. [A1]
- The app's copyable request says "the footage is already read" while prep is still running (the user pasted
  it at the start). Word it by the project's state. [A1]
- Guests posing at the photo wall get sorted as interview-bite (they face a locked-off camera). A map has to
  name that class to use them. [A1]
- `scripts/search_sheet.py` no longer runs: it imports `_takes_for` from `videtor.cli`, which the pipeline move removed.
  Searched with `videtor search` and an ffmpeg stills sheet instead. [Arrival 11-15]
- `scripts/search_sheet.py --skip` matches substrings, so `*/drive-download-*/*` skips nothing; pass `drive-download`. [A1]
- Event pace is still slower than the user's (0.56 s against their 0.32 s median); more half-beat slots would
  help. [Diwali v2]
- HD vox-pop bytes scaled 178% into 9:16 look soft. [B4]
- For cinematic films: natural-sound moments, dissolves, titles, letterbox. A per-genre roadmap was proposed.
  [B5]
- Resolve import hasn't been checked (Phase 2). Resolve 19.1.4 Studio has external scripting for later.
- Offline steps 1-3 (above).
- **The library's next steps** [11-07]: transcription runs after the scan, not alongside it (13 min on top of
  the 11 min scan for TPL); the sparse-first `prep`/`cut` is built (above) but narrows by class only, not yet
  by each slot's best candidates, so a map that draws on b-roll still decodes every b-roll take; the
  orientation check runs after the keyframe scan, so a camera it turns keeps as-shot stills and classes until
  the cut's full decode; "speech" is the WebRTC VAD,
  which hears the PA, so a listener in a single medium shot can read as a bite until the user's labels say
  otherwise; CLIP's "stage" prompts tell a podium from an interview setup, a plain wall behind a vox pop can
  still read as a setup.
- `cut` itself: `fill` is single-threaded numpy (90 s on Alpha Summit) and the sheets and SFX placement are
  serial; small next to the footage map. [11-07]
- **The app can't cut an interview piece** [cls-test, 11-07]: its Cut runs `videtor cut <shotmap>` only, so a
  brief with B-roll layered over the voice (`aroll.json` + a region shot map, `cut-interview`) runs from the
  terminal, and Cut on that `shotmap.json` would give B-roll with no voice. The app could run `cut-interview`
  when an `aroll.json` sits beside the shot map (`pipeline.cut_interview` is there to call; not wired). `cut-interview` also
  skips the sparse prep's full decode (`takes_for`, not `takes_for_cut`); cls-test worked only because colourstone-mia had mapped every take.
- Regression check: `videtor eval` (built 11-11) re-cuts the past projects after each change.

## Drives

The memory has more detail. Footage indexes are in `kb/footage/<drive>.json` for drives and
`kb/footage/<name>.json` for `prep` pools.

| Drive | Holds |
|---|---|
| Paritosh_1 | Guru (`/Volumes/Paritosh_1/Guru `, trailing space), Vivo 1, Vivo 2; `JW 2/17 sep/cc` (the prep smoke test) |
| Paritosh_2 | Recent projects, each with the user's `pf/` project: Check, colourstone, colourstone_2, Dikkat, Extra, mia, Mia 2, TPL Promo, Diwali Party; `videos` (sports raw footage, two shoots, prep `sports-raw`) |
| Paritosh_3 | TPL/Leander (`TPL/Videos`), Engagement (blind test 5), `Me/{lego,philips,meta,itc,Photos}`, `DP World 2026` |
| Paritosh_4 | Coca-Cola (`coca cola`; the XML points at `Copied_Khanna Fish`), `Copied_Rigo` |
| AMAZONSSD | Gujarat Giants (`WPL Gujarat Giants`) |
| AJAN SSD4 | Zomato ("PackSwitch"), Bar Maestro, Karl 2, OP, orry, JW copies |
| Extreme SSD | JW ("Jonnie walker X Lolla") |
| Adidas | `ADIDAS x NEXMAG` (the Adidas edit), `ALPHA SUMMIT` (blind test 4), `beauti verse` |

## Machine and installs

- Apple M2 Pro, 16 GB, macOS 26.3.1. Premiere Pro 26.0.2 (UXP), DaVinci Resolve 19.1.4 Studio.
- Homebrew: ffmpeg (no vidstab), uv. Python 3.11 venv in `.venv`.
- In `.venv` since 2026-11-16, with the user's yes, for fetching videos from links (`uv pip install`; extra `links` in
  `pyproject.toml`): yt-dlp 2026.10.26, yt-dlp-ejs 0.8.0, Deno 2.9.8 (`.venv/bin/deno`, YouTube needs a JavaScript
  runtime), curl-cffi 0.17.0 (Instagram without a login), and brotli, mutagen, pycryptodomex, websockets.
- Models:
  - MediaPipe BlazeFace full-range and hand landmarker (`cache/models/`);
  - CLIP ViT-B/32 laion2B (`~/.cache/huggingface`);
  - mlx-whisper large-v3-turbo (`cache/models/whisper-large-v3-turbo`, fetched with curl because the HF client
    stalled).
- prpr 1.0.0 (`~/.local/bin/prpr`) with the bridge plugin 0.3.0 inside Premiere. The bridge can't import XML.
