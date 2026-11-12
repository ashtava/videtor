# How Videtor works (as built; last rewritten 2026-11-11 for the pipeline and the feedback loop)

This describes the code as it stands. The original design page from 2026-10-28/29 (GitHub survey, phased plan,
Round 4 design) is `docs/architecture.html`; it's also published as an artifact
(https://claude.ai/artifact/EeQ111gwMv8iA6TKtnQHzS) and hasn't been updated since 10-29. For where each project
stands, see `docs/status.md`.

## The loop

```
 the user's past edits (.xml-exports/*.xml)
   └─ kb ingest → kb/projects, kb/profile/<bucket>.json        (style numbers per bucket)
   └─ kb footage / frames → kb/footage/<drive>.json            (what each edit used vs left out)
   └─ kb sfx, kb breakdown, kb keeper, kb bucket                (sound habits, edit breakdowns, the keeper, bucket cards)

 a new shoot (/Volumes/<drive>/<folder>)
   └─ videtor prep → index, sparse map (keyframes only, cache/sparse), orientation, the library's classes
   │                 (out/<name>/classes.json), out/<name>/overview.md + sheets: minutes. --dense maps every second now
   └─ videtor library build → sparse map → out/library/<name>/<class>/ links, sheets, bites.xml
        └─ the user moves links → videtor library learn → kb/library/{labels,model}.json → the next build and prep sort with it

 a brief ──► Claude (/cut-from-brief) reads the bucket card, kb/notes.md, the overview, search sheets
              └─ writes out/<name>/shotmap.json                 (sections → slots: what each shot must show)
                   └─ videtor cut → the footage map (2 samples/s, camera motion) for the takes the map's classes allow
                        → picks.json, second-look sheets, cut-sheet.jpg, <name>.xml
                        └─ Claude's second look → videtor look <id>=<take> → the XML again
                             └─ the user imports into Premiere and fixes the cut
                                  └─ videtor readback (the finished video, or --xml / --live) + verdict + the user's swaps
                                       → kb/feedback/<name>.json → videtor learn (taste, keeper) → videtor eval gate → next cut
```

Everything after the shot map is deterministic code on the Mac. Claude's part is the brief → shot map
translation, the music choice when none is given, reading contact sheets, and the second look for what needs
eyes. `videtor draft` writes a first shot map with no Claude (the bucket's style, the shoot's inventory), and the
cut itself settles neighbours and runs an automatic second look.

**One home for the steps** (2026-11-11): `pipeline.py` holds `prepare`, `cut`, `look`, `cut_interview`, `cut_mv`,
`read_back`, `verdict` and `learn`. The terminal (`cli.py`) and the app (`ui/`) call them and hold no step of their
own. A step reports through `events.Events` (sentences in the terminal; JSON lines when `VIDETOR_EVENTS=json`,
which the app sets) and raises `PipelineError` for what it cannot do. `videtor eval` cuts the past projects through
the same `pipeline.cut_picks`, so a measure is always a measure of the real cut.

## Modules (`src/videtor/`)

| Area | Module | What it does |
|---|---|---|
| Steps | `pipeline.py` | The steps of a job (above). `cut_picks` is the cut without files (the evaluation's entry); `carry` is the one place a shot map's settings cross to the picks; `decisions.json` keeps second-look swaps and `replay` puts them back on the next cut; `read_back` reads the finished job; `learn` fits candidates and keeps them only when the evaluation passes |
| | `events.py` | `Events` (stage, progress, artifact, info, warning, error), a text sink and a JSON-lines sink, `parse` for the app |
| Teaching | `teach.py` | What the user gives Videtor to learn from and what it made of it (the app's Teach Videtor page): kinds of work, rules in `kb/notes.md`, an XML taken in as an edit (`add_edit`), the edits given before and which drive each needs (`past_edits`), `state` per kind of work, `kb/learning.json` (how each learning went). The steps are in `pipeline.py`: `teach_project` (a job cut by hand: the video for the style; with footage, a keyframe shortlist of the takes it plays, those and a sample of the rest read in full, each shot found, kept as a finished job in `kb/feedback`), `teach_edit` (an edit already in the manifest), `teach_style` (finished videos alone), `read_pending` (every finished video waiting in a project folder), each followed by the gated `learn` |
| CLI | `cli.py` | `videtor <command>` (typer), thin: `prep orient music search draft cut look cut-interview cut-mv readback verdict learn eval preview finish analyze fx-test ui`, `kb ...` and `library ...`. `prep` is sparse by default; `cut` narrows the pool to what the map can use (`library.candidates`: its classes, the takes its slots name, never a skipped take) and decodes those in full before filling |
| UI | `ui/static/` | The page. Teach Videtor is the one place for training inputs; Review has the verdict, the finished video, an in-app preview and the finishing steps; Footage can turn a sideways camera |
| | `ui/server.py` | `videtor ui`: a standard-library HTTP server on 127.0.0.1 with JSON endpoints (prep, cut, look, music, search, library build/learn/move, projects, jobs, files from allowed roots) and the static page in `ui/static/`; a pywebview window when installed |
| | `ui/jobs.py` | Background jobs, each `videtor <command>` in its own process group with a polled log ring; a cut pauses (SIGSTOP) running preps and library builds and resumes them after, a prep submitted during a cut waits, a paused prep that ends with failures is rerun once |
| Knowledge base | `kb/xmeml.py` | Reads Premiere FCP7 xmeml exports into dataclasses (stdlib only). Nests, Time Remap, transitions' −1 ends |
| | `kb/metrics.py`, `kb/profile.py` | Style numbers per sequence, rolled up per bucket |
| | `kb/footage.py`, `kb/selects.py` | Indexes the raw footage behind an edit (used ranges, duplicates, consolidated copies); used vs unused |
| | `kb/sfx.py` | The user's SFX habits mined from their edits (snippets, offsets from the cut, levels) → `kb/sfx.json` |
| | `kb/breakdown.py` | An edit in numbers and words (medley, pauses, pace per stretch, ramps, stacks, flashes, SFX): the material for bucket cards |
| | `kb/render.py` | A finished reel read from its render alone: cuts, the frames between shots (recurring static overlay, white flash, black), each shot's length, look and move, the grade, beat fit and accents, CLIP labels (`SHOTS`); `summarize` → `kb/renders/<bucket>.json` (no file names), each reel in `out/references/<bucket>/` |
| | `kb/readback.py` | Reads a fixed cut back (xmeml or live over prpr), aligns it to the picks, labels each slot kept/slipped/trimmed/swapped/removed |
| | `kb/renderback.py` | The same from the finished video alone: shots split (and split again at the cuts the picks proposed), each found in the footage map (CLIP proposes takes, the frame is laid over cached stills at a few sizes with normalised correlation), turned into the clip rows `readback.diff` labels. Source times are known to about a quarter second; a shot found near its slot's pick is that pick |
| | `kb/feedback.py` | The one store, `kb/feedback/<name>.json`: the latest read-back, the choices a person made in a second look, the verdict. `teaching_rows` feeds the taste, `used_ranges` the keeper |
| | `kb/js/` | Premiere UXP scripts: `read_sequence.js` (read-only) and `finish_interview.js` (writes effects; only with `--force-bridge`) |
| Analysis | `analyze/frames.py`, `audio.py`, `motion.py` | Per clip: sharpness, exposure, colour; loudness, silence, speech; motion and in-clip cuts |
| | `analyze/faces.py`, `tags.py` | MediaPipe faces, hands and shot scale; CLIP ViT-B/32 tags and embeddings |
| | `analyze/footmap.py` | Every take sampled twice a second (CLIP embedding, faces, hands, focus, exposure) with camera motion at 8 fps (LK flow + partial affine): the per-second map everything searches |
| | `analyze/orientation.py` | Sideways or upside-down cameras found from faces turned four ways (camera majority ≥ 3 at 70%) |
| | `analyze/parallel.py` | How many processes and ffmpeg threads this machine gets (performance cores, memory, `VIDETOR_WORKERS`), and `run(fn, items)` over them with each item's failure captured |
| | `analyze/sparse.py` | The keyframe-only map of a clip: at most 24 stills (`-skip_frame nokey`, seeks over 60 s), faces, hands, VAD speech, the shift between stills, CLIP; `cache/sparse/` |
| | `analyze/beats.py` | Beat grid, loudness and onset curves per track (`cache/beats/`) |
| | `analyze/speech.py` | Local Whisper (mlx-whisper large-v3-turbo) with word timings; a fumble finder |
| | `analyze/sync.py` | Lip-sync: each take's camera audio matched to the master by onset envelopes, stretch by stretch, 2× playback included |
| Planning | `plan/shotmap.py` | The shot-map schema (docstring) and `validate` |
| | `plan/fill.py` | Scores every half-second of every take against every slot (CLIP z-score, avoid, soft filters, keeper, taste, crop penalties), then one assignment over all slots with 3 runners-up each; `_settle_neighbours` then gives a slot another of its good takes where a cut shows the same take twice, two takes of one setup, or day cut to night inside a section; a flicker's sub-shots stay within 1.5 sd of its pick and in its light; `"order": "shoot"` leans on the order of the day |
| | `plan/keeper.py` | Per bucket: balanced logistic regression on generic CLIP concepts plus footmap measures, kept vs left-out samples; out-of-fold scores for seen takes |
| | `plan/taste.py` | Corrections to the filler's term weights, learnt from the feedback store: `kb/taste.json` across all cuts (from 12 choices), `kb/taste/<bucket>.json` per bucket (from 20) |
| | `plan/moves.py` | Ramps (arrival, punch), flicker, stack, black, flash frames, beds, medley music |
| | `plan/music.py` | Library scan, best window, ranking for fast cuts (`videtor music`) |
| | `plan/sfx.py` | Places risers, impacts, whooshes, shutters and clicks the way the user does |
| | `plan/stabilize.py` | Keyframe steadying (below); the shots are measured side by side |
| | `plan/library.py` | A shoot sorted into wide / close-up / talking-head / interview-bite / b-roll folders from the sparse map: rules first, then a model learnt from the user's moves (`kb/library/`); the bites as a Premiere sequence |
| | `plan/fx.py` | Effects drawn at a bucket's rates (`kb/policy/`, else `kb/renders/`): flash and static overlays (generated in `cache/assets`, or the user's), whip, spin, blur, push-in, pull-out, punch, shake, echo, black and white (Iris label); `fx-test` sequence |
| | `plan/gym.py` | Learning a cutting style from reels alone: each reel's shots as stand-in footage, a policy that cuts them, a reward against the real reel, cross-entropy search, leave-one-out evaluation → `kb/policy/<bucket>.json` |
| | `plan/draft.py` | A first shot map for new footage (`videtor draft`): the bucket's policy for pace, effects and accents; the shoot's inventory for what to ask for (half the style's share of each kind of shot, half the shoot's own). A bucket with no reels drafts on its edits' pace with the general vocabulary and no effects |
| | `plan/interview.py` | A-roll paper edit (`aroll.json`), B-roll by region, voice levelling, music dips, finishing pass (ramps, zooms, Warp flags) |
| | `plan/musicvideo.py` | Beat-grid lip-sync cut with shares per singer, cutaways moved by whole bars, beam search (`mv.json`) |
| | `plan/second_look.py`, `sheets.py`, `overview.py` | Contact sheets for Claude or the user; `look` applies choices |
| | `plan/build.py` | Picks → OpenTimelineIO timeline (tracks, levels, music pieces, steadied clips) |
| | `plan/evaluate.py` | `videtor eval`: the projects of `kb/eval/set.json` cut again from the cache and measured against the user's own edit (right take, right second, in top 3) and on their own (filled, match, sharp, repeats, look-alikes, light jumps, keeper); `compare` against `kb/eval/baseline.json` with a tolerance per guarded measure |
| | `plan/inventory.py` | What a shoot holds by kind of shot (a vocabulary of labels with phrasings; seconds, takes and a capped share per label): `inventory.json`, written by `prep` and read by `draft` |
| | `plan/autolook.py` | The automatic second look: a soft, badly lit or shaking pick swapped for a runner-up that passes and scores within 0.6 sd |
| | `plan/benchmark.py` | `score_picks` against an index's used ranges, `load_real_metrics`. The rounds 1-3 planner (`rules.py`, `structure.py`, `rerank.py`) is in `scripts/archive/rounds-1-3/` |
| Export | `export/preview.py` | `videtor preview`: a half-size video of what V1 plays, framed as the XML frames it, with the music (ffmpeg; drive connected) |
| | `export/xmeml_writer.py` | Our own Premiere xmeml writer (Basic Motion with scale, rotation and centre keyframes, Time Remap, Gaussian Blur, Opacity, blend modes, labels, sequence markers, levels, links, disabled alternates) |

## Files on disk

| Path | What | Notes |
|---|---|---|
| `.xml-exports/` | The user's Premiere exports (11 edits) | The KB's source. `TPL.xml` duplicates `leander.xml` |
| `kb/manifest.json` | Bucket per edit, extra footage folders per edit | Edit by hand |
| `kb/notes.md` | The user's rules in prose, `- [bucket] ...` | Read verbatim into bucket cards and by Claude |
| `kb/projects/`, `kb/profile/` | Per-sequence metrics; profiles per bucket | `videtor kb ingest` |
| `kb/footage/` | Indexes: `<drive>.json` (+ `.summary`, `.selects`) for past edits; `<name>.json` per `prep` pool | A file can carry `"excluded": true` (another shoot on the same card) |
| `kb/sfx.json` | SFX habits | From the exports |
| `kb/keeper/`, `kb/buckets/` | Keeper models; bucket cards | event only so far |
| `kb/briefs/`, `kb/marks/` | Briefs written for past edits; Source Monitor marks | |
| `kb/feedback/<name>.json` | Per project: the latest read-back (render, XML or live), the user's choices, the verdict | Empty until the first finished job is read back |
| `kb/taste.json`, `kb/taste/<bucket>.json` | Corrections to the filler's weights | Written by `videtor learn` when the evaluation passes |
| `kb/eval/set.json`, `baseline.json` | The evaluation set (shot maps in `out/`, the user's edit where there is one) and the numbers runs are held to | `videtor eval --save` |
| `cache/footmap/` (2.0 GB) | Per take: `<fid>.json`, `.npz`, `.decode.npz`, `frames/<fid>_<i>.jpg` | Hours of decoding with the drives connected. Don't delete |
| `cache/frames/` (1.0 GB) | Sampled stills per clip (`<fid>_N.jpg`) | |
| `cache/analysis/`, `beats/`, `transcripts/`, `sync/`, `stab/`, `probe/` | Per-clip analyses, beat grids, Whisper transcripts, synced camera audio, stabiliser measurements, probe frames | All rebuildable, but slowly |
| `cache/models/` (1.5 GB) | BlazeFace, hand landmarker, Whisper large-v3-turbo | CLIP lives in `~/.cache/huggingface` (1.4 GB) |
| `cache/sparse/` | Per clip: `<fid>.json`, `<fid>.npz` (stills' arrays and CLIP), `frames/<fid>_<i>.jpg` (keyframe stills, 480 px) | `videtor library build`; minutes per shoot, resumable |
| `kb/library/` | `labels.json` (path → class, read from the user's folder moves), `model.json` (the logistic regression) | `videtor library learn` |
| `out/library/<name>/` | One folder per class holding links to the clips, `sheets/<class>-N.jpg`, `interview-bite/bites.xml` + `bites.md`, `library.json`, `library.md`, `index.json` | The user sorts by moving links |
| `out/<name>/` | Per cut: `brief.md`, `inventory.json`, `shotmap.json` (or `aroll.json`/`mv.json`), `picks.json`, `decisions.json` (second-look swaps, replayed by the next cut), `<name>.xml`, `overview*`, `second-look-*.jpg`, `cut-sheet.jpg`, `preview.mp4`, `verdict.json`, `report.md`, `work/`, and the finished video when the user saves it there | `report.md` is the record of what was delivered |

`cache/` and `out/` are gitignored, so that media and derived data never get committed.

## Formats

- **Shot map:** the schema is the docstring of `plan/shotmap.py`; the skill explains how to write one.
  - Map level: `name key bucket length_s fps width height beat_lead_s music avoid skip sfx_skip flash beds
    stabilize voice_lufs under_db keeper_w repeat_take classes effects cut_on order second_look notes`. `shotmap.warnings`
    names any key it doesn't know (a misspelt option is said, not dropped).
  - `sections[]`: `name match slots[]`.
  - Each slot: `id beats show scale face hands camera speed role takes ramp flicker stack black byte stabilize`.
  - Examples: `out/guru-25s-brief/` (plain), `out/diwali-party-v2/` (moves, medley), `out/alpha-summit/`
    (bytes, stabilise, keeper_w), `out/engagement/` (cinematic 16:9, pinned moments).
- **picks.json:** the map's settings (`pipeline.carry`) plus `slots[]`, each with `id section role show start_s length_s`,
  the slot's own `effects` and filters (`scale face hands camera`) and `pick`/`alternates[]`. `pipeline.check_picks`
  reads it. A pick has `fid path name in_s out_s speed display_w display_h score reasons motion
  stab`. A swapped slot has `second_look: {chose | borrowed_from, why}`.
- **Footage index:** `files[]` entries have `fid path basename dir kind media{duration_s video audio ...}
  orientation used_by duplicate_of` and optionally `excluded`.

## Verified facts: Premiere xmeml

These were checked against the user's own exports and projects; don't re-derive them.
- **Clip rate:** clipitem in/out count at the clipitem's `<rate>`, which Premiere sets to the sequence rate even
  for 50/100 fps media (the `<file>` keeps the native rate). Writing at the media rate ran 100 fps clips off
  their end.
- **Time Remap:** in/out are in speed-stretched frames. Source seconds = in / rate × speed; clipitem duration =
  media frames / speed. The `speed` parameter (percent) is authoritative.
- **Basic Motion centre:** offsets are Δx ÷ (2 × the sequence's long side) and Δy ÷ (2 × its short side),
  right/down positive. On 1080×1920 that's Δx/3840 and Δy/2160, matched against the Guru `.prproj`.
  Rotation is in degrees, clockwise positive.
- **Keyframes:** `<when>` is in the clipitem's in/out frame space (when = in + k), verified on the Coca-Cola
  and Adidas exports. Centre keyframes are `<keyframe><when>N</when><value><horiz/><vert/></value></keyframe>`.
- **The user's framing per camera:** rotated Sony 4K at scale 50; the sideways SD camcorder (`M2U*.MPG`,
  720×576) at rotation −90 and scale 188-190. The writer copies the project's habit; otherwise it uses the
  scale that fills the frame.
- **Can't travel in xmeml:** Lumetri (its LUT lives in a blob), Warp Stabilizer. Adjustment layers re-import as
  a black slug.
- **What the FCP XML import translates** (read from the strings of Premiere 26's own binary, 2026-11-11; its
  behaviour on our files is not yet checked, which is what `videtor fx-test` is for): Basic Motion (with
  `motionblur`, `dropshadow`), Distort (`deformation`), Crop, Opacity with blend modes (`<compositemode>`:
  screen, lighten, darken, softlight, hardlight, ...), Time Remap, Gaussian Blur (`radius` → Blurriness), Luma Key,
  Four/Eight-Point Garbage Matte, Color Corrector 3-way, Cross/Additive/Film Dissolve, Dip to Color/White/Black,
  Edge Wipe, colour mattes (`fillcolor`), slugs, bars and tone, the text generators. Anything else is "flushed -
  unsupported". The user's exports write Gaussian Blur, Luma Key, Distort and Opacity in exactly the FCP form, and
  Guru's has `<compositemode>lighten</compositemode>` right after `<out>`; labels sit at the clipitem's end
  (`<labels><label2>Iris</label2></labels>`). No desaturate filter is in the table.
- **Paths:** pathurls keep `/,[]()#` raw, like Premiere. Readers must not use `urlparse` (`#` would cut file
  names).
- **Transitions:** a −1 start/end next to a transition resolves via `cutPointTicks`.
- **OTIO's `fcp_xml` adapter** fails on 100/119.88 fps media, hence our own writer. Premiere 26 imports
  OTIO-written xmeml, but speed-remapped clips drift a frame.
- **Source Monitor marks** live in project metadata: `Column.Intrinsic.VideoInPoint/OutPoint`, with in =
  OutPoint.min − 1 frame, out = InPoint.max + 1 frame, ticks of 254016000000/s.
- **Linked Selection:** the user's timeline had it off, which made linked clips look unlinked at first.

## Verified facts: footage

- **Sony XAVC 4:2:2 10-bit H.264** has no hardware decode: the footage map runs at about real time per
  worker. HEVC 50p (Diwali) is fine. BRAW and R3D need proxies.
- **Sony S-Log3:** the gamma is in the `C####M01.XML` sidecar (`CaptureGammaEquation`). File numbers wrap
  C9999 → C0001.
- **Rotation:** Sony 4K held vertical records a 90° display matrix, except when the shot starts pointing at
  the floor or sky (then none, or the opposite one). In the footage map, `display_w`/`display_h` are the
  upright size after the editor's turn, and `applied_rotation` is the Basic Motion turn. Rotation sign as in
  Premiere: −90 = counter-clockwise = `np.rot90(k=1)`.
- **Audio:** the Zoom recorder writes four mono tracks per take (`ZOOM000N_Tr1..Tr4.WAV`). The Canon bytes
  camera (MXF) has 4 mono tracks with the mic on channel 1.
- **Habits measured from the user's edits:**
  - Cuts land a median 0.14 s before the beat, so `beat_lead_s` is 0.12.
  - In-points sit a median 38% into the take.
  - Used clips are 1.2-2.4× sharper than unused, and usually from longer takes.
  - 50/100 fps slow motion is mostly left unused in brand-social.

## Built-in cutting rules

- **Tracks:** V1 the picks; two alternates per slot on V2-V3, switched off (interview: V3-V4); music on A1, or
  on A2 when bytes or voice take A1; SFX on their own tracks.
- **Voice:** interview voice at −16 LUFS. Bytes at the map's `voice_lufs` (default −14), with the music stepped
  down to `under_db` (default −12) in two steps over 0.24 s. No SFX on a byte; SFX that run into one end on its
  cut with a 2-frame fade.
- **SFX timing:** risers crest +0.04 s after their cut, whooshes peak at +0.06 s, shutters land at +0.02 s;
  impacts start early so the hit lands on the cut.
- **Keeper:** `fill.KEEP_W` = 0.6 per sd; a map's `keeper_w` overrides it (0.3 for a shoot unlike the bucket's
  edits).
- **Avoid:** words like crew, person, people, photographer or stylist in `avoid` add a penalty per extra face.
  Leave them out of a party's avoid list.
- **Crops:** a take whose orientation doesn't match the sequence (landscape in 9:16, portrait in 16:9) is
  cropped to fill the frame and penalised in the filler. Landscape takes in a vertical piece are cropped
  around the face in the chosen window.
- **Steadying** (`plan/stabilize.py`):
  - OpenCV features + LK flow + `estimateAffinePartial2D` give the camera path;
  - a Gaussian smooths it (σ 0.35 s, with straight-line padding);
  - the correction (smooth − path) is written as a Position + Rotation keyframe on every frame, with the
    smallest zoom (≤ 1.10) that hides the edges;
  - it covers V1 shots of 0.5 s or more, skipping bytes, black, flickers and shakes over 40 px;
  - on real takes it removes about 50-65% of the shake.

## Parallelism and decode costs (measured 2026-11-07, M2 Pro 8+4 cores, 16 GB, USB SSDs)

- **A 4K 4:2:2 10-bit H.264 (XAVC) full decode is CPU-bound.** One ffmpeg takes about 10 cores for 138 fps
  (2.8× real time at 50p): 64 CPU-seconds per 18 s of footage. More decode workers can't make the footage map
  faster than that; capping each ffmpeg at `-threads 2` makes it 15% cheaper per frame, which is what the pool
  does now (`parallel.ffmpeg_threads`).
- **HEVC 4:2:2 10-bit decodes in hardware** (VideoToolbox): 1.4 CPU-seconds per 8 s clip, and four sessions at
  once ran 2.4× faster than one, so hardware takes are worth decoding many at a time.
- **Keyframes only** (`-skip_frame nokey`) cost 0.13-0.15 s per 4K keyframe, single-threaded, 8-10× less than a
  full decode. A seek to one keyframe costs about 0.7 s on USB (the file's index is read each time), so clips
  over 60 s are sampled by 24 seeks and shorter ones in one pass. Sony cards keep a keyframe every 12 frames
  (25p) or every 24-40 (50p).
- **The content stage, per 480 px still:** PIL load 1 ms, BlazeFace 4.5 ms, the hand landmarker 14 ms, CLIP
  ViT-B/32 on MPS 6 ms (4 ms forward, batched). The hands are the cost, so the footage map now runs its
  detections in up to 4 processes and keeps CLIP in the main process (one model on the GPU).
- `analyze/parallel.py` sizes every pass: workers = the performance cores (8), capped by memory (0.9 GB per 4K
  software decode, 4 GB kept free) and by the items; `VIDETOR_WORKERS` overrides. `prep`, the footage map,
  the sparse map, the stills and the steadying measurements all use it; `videtor prep --workers` is no longer
  3 by default.

## Where Claude is in the loop

- **Local on the Mac:** all per-frame work (footage map, faces, CLIP, motion), Whisper, music analysis, the
  filler, and XML writing.
- **Claude:**
  - one shot map per project;
  - the music choice when none is given (by measurement, not by ear);
  - about 10-35 contact sheets per project: overview stills, search hits, the pick plus three runners-up per
    slot;
  - the second-look swaps;
  - judgment calls (aspect ratio, what to leave out);
  - and so far, new features.
- **Never sent:** video. A proposed path to running offline is in `docs/status.md`.

## Tests

`.venv/bin/python -m unittest discover -s tests`: 295 tests in about 20 s, with synthetic fixtures (no drive
needed). The parser and metrics use only the standard library.
