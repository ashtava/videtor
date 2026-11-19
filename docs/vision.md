# Videtor, long term: a composer for serious editors

Written 2026-11-20 from the user's words, to be revisited. Nothing here is built unless a section says so.

> "The entire flow of a project converts into a composer style with LLM tooling: we use the engine we've already
> built to create a timeline, create the necessary skills to train Claude with the art of editing, and do basic
> edits on the timeline: fixing things, prompting to update a specific video, or the user editing things manually.
> [...] our tool is going to be for serious editors, who do high-demand edits, work with over 100 GB of data, and we
> are able to provide them with a similar kind of editing to help them jumpstart their editing projects."

## The product in one paragraph

An editor opens a shoot of any size, says what the piece is (a brief, a reference, or both), and gets a first
timeline in their own style within minutes. They then shape it in one place: by talking ("hold longer on the
greeting", "swap this for a closer shot", "make the middle faster"), by hand on the timeline, or both in turn. When
it is close, it goes to Premiere or Resolve as a real project, with their footage, and they finish there. Videtor is
the jump start and the assistant, not the finishing room.

## Where it differs from Cardboard

Cardboard (cardboard.ai, read 2026-11-20: home, desktop, pricing and plugin pages) is the nearest thing to this.
What it says of itself:

- A browser editor and a Mac app. "Describe it, direct it, share it": a prompt, footage or both become an editable
  first cut; refined by chat or on a timeline; exported as MP4/MOV up to 4K, or sent to Premiere, Final Cut or
  Resolve from the browser version (not from the Mac app).
- An agent called the Director that "plans the edit, builds the cut, then checks its work", with "see the plan
  before it edits", undo, and "take over on the timeline anytime". It can be driven by the user's own Claude or
  ChatGPT plan, and through an MCP server or CLI.
- Reference matching: "match the pacing and color of this video", "match your reference cut for cut".
- Generation and sourcing: AI video, images, music, SFX, voice clone, motion graphics, stock and web B-roll.
- Sold to people who do not edit ("No editing experience needed"): social clips, podcasts, UGC ads, explainers.
  Credits by usage; uploads of 2.5 GB a file (10 GB on its top plan) in the browser; the Mac app reads local files
  and sends frames and transcripts to the cloud.

| | Cardboard | Videtor |
|---|---|---|
| Who | people who do not edit; marketing teams | working editors with paid, deadline jobs |
| Footage | clips and recordings; GB-sized uploads, or local on the Mac app | whole shoots: hundreds of takes, 100 GB and more, on external drives, 10-bit camera files |
| Where the footage goes | cloud in the browser; frames and transcripts to the cloud on the Mac | stays on the Mac; nothing is uploaded |
| Whose style | a reference video, or the Director's own taste | the editor's own: learnt from their past projects, their finished videos and every fix they make |
| What comes out | a finished video, mostly | a timeline in the editor's NLE, alternates on tracks above, ready to be finished by hand |
| Making content | generates video, music, voice, graphics | none: selection and assembly of what was shot |
| The agent | plans, cuts, checks its frames | the same loop, plus skills that carry the craft and a record of what this editor accepts |

The claim to earn: **on a real shoot, for a real editor, the first timeline is one they would rather fix than
restart.** Cardboard does not have to meet that; a Videtor that does not is of no use to this user.

## What exists today (2026-11-20)

- The engine: every take of a shoot read once (faces, speech, what each second shows), a plan turned into picks with
  runners-up, music, SFX, steadying, and a Premiere XML. Runs from a cache with the drive unplugged.
- Three ways to a plan: Claude from a brief (the skill), a draft from a learnt style, and, since 11-19, a reference
  video (the cuts and the kind of shot in each place copied from a finished reel; footage chosen by comparing
  pictures). The user's verdict of 11-20 on three cuts of one shoot: the reference one was the best.
- A timeline in the app that plays the cut and edits it (swap, slip, roll, remove, replace, undo), and notes per shot.
- A loop: finished videos read back against the cut, verdicts, swaps; taste and keeper models fitted from them and
  kept only when the evaluation does not get worse.
- What it lacks for this vision: any conversation inside the app (Claude works in a separate window, by a pasted
  request); a skill that knows editing as a craft; an edit asked for in words; Resolve.

## The shape to build

```
        the editor                        Claude, with skills                    the engine (deterministic)
  brief · reference · notes   ──▶   plans, phrases, judges, revises   ──▶   reads footage · fills slots · builds
  talks or edits by hand      ◀──   says what it did and why          ◀──   timeline · proxies · XML · measures
                   └────────────────── one composer window: chat beside the timeline ──────────────────┘
```

Three rules hold it together:

1. **Claude decides, the engine executes.** Bursts and holds, what a section is about, which moment is the beat:
   judgment, taught by skills (the user, 11-20: "no rule can teach bursts and holds"). Finding the footage, timing
   to the frame, writing the project: code. A rule engine is only ever a check on a plan, never its author.
2. **Every edit is one operation**, whether a hand made it or a sentence did. The timeline's operations
   (`plan/edit.py`) are the vocabulary; a prompt becomes a short list of them, shown before it is applied, undone
   as one step. This is what makes "fix this shot" safe on a 60-shot cut.
3. **Everything the editor does teaches.** A swap, a trim, a rejected suggestion and a finished export all land in
   the same record (`kb/feedback`), and the skills read it.

## The skills (the art of editing, written down)

Skills are how Claude is "trained" here: documents with the craft, the user's measured habits, and worked examples
from their own projects. Each needs material Claude can see: strips of frames through a take (motion, not one
still), the transcript, the music's sections.

| Skill | What it decides | Fed by |
|---|---|---|
| Read the brief | the piece's job, length, must-haves, what the client means by "fast" or "cinematic" | briefs and verdicts of past jobs |
| Find the story | what happened on the shoot, what the piece says, a line a section | the footage inventory, transcripts, the overview |
| Phrase the cut | runs and holds: where a burst, where 2-3 s, what earns a hold | the music's sections; the user's rhythm measured as structure (11-15 BTS: half the shots under 0.36 s, holds 45% of the time); references |
| Choose the moment | within a take: the move that completes, the look, the laugh | frame strips, the keeper model, the user's swaps |
| Cut on sound | bytes, breaths, the drop, where SFX help and where they pile up | transcripts, loudness, the user's SFX habits |
| Review like an editor | watch the result as frames and fix what reads wrong before the editor sees it | the second look, the evaluation's measures |
| Revise from a note | turn "the middle drags" into operations on the timeline | the note, the timeline, the same skills above |

Order of writing: Phrase the cut and Choose the moment first (they are what the 11-19 debug found missing), each
tested the way 11-19/11-20 was: several cuts of one shoot, the user picks.

## The composer (the app)

One window, three parts:

- **The timeline**, as now, with the plan's sections and story lines above it and the runners-up one key away.
- **The conversation.** The editor types or talks; Claude answers with a proposed change as a list ("hold meet-3
  for 2 s; drop meet-4 and meet-5") and a preview; Accept applies it as one undoable step. A note can be pinned to a
  shot, a section or a time range, so "this one" means something.
- **The source.** The shoot by shot type and by what was said, searchable in words and by picture ("more like
  this one"), draggable onto the timeline.

Claude runs as the editor's own Claude (Claude Code, or the API with their key) driving the engine through a small
set of tools: read the project, propose operations, apply, undo, search, render a strip. The engine never needs the
cloud; frames go to Claude only as the user has allowed (stills and contact sheets, since 2026-10-29), and the
footage itself never leaves the Mac.

## Serious footage (100 GB and more)

What already holds: keyframe-only reading (a day's shoot in about ten minutes), full decode only of the takes a plan
can use, everything cached so the drive can go. What a composer adds:

- Proxies made in the background at prep, so the timeline plays any take at once, with the drive unplugged.
- Multi-day and multi-card shoots as one project: several folders and drives, takes told apart beyond their file
  names (the 11-17 gap: two shoots, one project name).
- Sync: multicam and dual-system sound by waveform, so an interview is one moment, not three files.
- Long-form: interviews and documentaries cut from the transcript first (the paper edit exists; it needs the
  composer's conversation), then covered with B-roll.
- A true project out: Premiere XML today; Resolve; bins by shot type, markers for notes, alternates as disabled
  tracks. Round trip: read the editor's Premiere changes back without an export (the bridge exists, read-only).
- Speed as a feature: a first timeline in minutes after prep, an edit by prompt in seconds.

## Phases

Each ends with something the user watches and judges; a change to how cuts come out goes through an A/B first
(the 11-19 rule).

1. **The reference route as the main road** (started 11-19/20). A plan from the editor's own finished work or a
   reel they like; the brief fitted onto it by Claude. Next on it: bytes and voice in a reference, the reference's
   effects made rather than listed, jump cuts inside a take (not seen from the picture: needs another signal), a
   library of the editor's finished pieces by kind of work.
2. **Skills, first two.** Phrase the cut; Choose the moment. Frame strips as the material. Judged by A/B on past
   shoots with the user's own edits as the measure.
3. **Edits by prompt.** A note on a shot or a range becomes proposed operations; shown, accepted, undone as one.
   Starts in Claude Code against the existing timeline operations, before any chat is built into the app.
4. **The composer window.** The conversation beside the timeline; the source panel; proposals with previews.
5. **Serious footage.** Background proxies, multi-drive projects, sync, Resolve, the Premiere round trip.
6. **Other editors.** Today everything is learnt from one editor. A second editor needs their own knowledge base
   from their own projects, an install that is not this Mac, and a way to start with no history (references first).

## Open questions

- Which Claude runs the conversation in the app: the editor's Claude Code, or an API key in the app? It decides
  what the composer window is.
- How much of a take may Claude see? Strips of frames are needed to judge motion; the user's rule today allows
  stills and contact sheets.
- Where the line to the NLE sits: how far to take finishing (grade, titles, effects) before handing over.
- What the measure of "better" is besides the user's eye: the evaluation counts takes the user used; it does not
  know whether a cut reads as a sequence (the lesson of 11-19).
- Is this a tool for this editor, or a product? Phase 6 is a different amount of work.

Sources for the Cardboard section: cardboard.ai (home, /desktop, /pricing, /mcp), read 2026-11-20; claims are the
vendor's own.
