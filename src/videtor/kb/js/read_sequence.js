// Every clip on a sequence's video tracks, read live from Premiere for videtor.kb.readback.
//
// Runs as the body of `async (ppro, uxp, H, args) => { ... }` through `prpr exec`.
// READ-ONLY: getters only, no transactions, no actions; lockedAccess is used only
// for the synchronous getTrackItems read, as prpr's own snippets do.
// readback.py prepends `const READBACK_ARGS = {...};` because `prpr exec` passes no args.
//
// Times come back in seconds: start/end on the sequence timeline, in/out in the
// source media (or the nested sequence) the clip plays, speed-stretched under a
// speed change exactly as Premiere reports them (readback.live_rows multiplies by speed).
const opts = (typeof READBACK_ARGS !== "undefined" && READBACK_ARGS) || args || {};
const startedAt = Date.now();
const TICKS_PER_SECOND = 254016000000;
const secs = (t) => { try { return t ? t.seconds : null; } catch (e) { return null; } };

const project = await ppro.Project.getActiveProject();
if (!project || !project.name) throw new Error("No active project is open in Premiere");
// opts.sequence must exist; opts.prefer (the picks' own name) is used when the project has it; else the
// active sequence.
let seq = null;
const want = opts.sequence || opts.prefer;
if (want) {
  for (const s of await project.getSequences()) {
    if (s.name === want) { seq = s; break; }
  }
  if (!seq && opts.sequence) throw new Error(`Sequence not found: ${opts.sequence}`);
}
if (!seq) {
  seq = await project.getActiveSequence();
  if (!seq) throw new Error("No active sequence");
}

let fps = null;
try { const tb = Number(await seq.getTimebase()); fps = tb > 0 ? TICKS_PER_SECOND / tb : null; } catch (e) {}
let duration_s = null;
try { duration_s = secs(await seq.getEndTime()); } catch (e) {}

const tracks = [];
const count = await seq.getVideoTrackCount();
for (let i = 0; i < count; i++) tracks.push(await seq.getVideoTrack(i));
const perTrack = [];
project.lockedAccess(() => {
  for (const track of tracks) {
    let items = [];
    try { items = track.getTrackItems(ppro.Constants.TrackItemType.CLIP, false) || []; } catch (e) { items = []; }
    perTrack.push(items);
  }
});

async function readItem(item, trackNumber, trackEnabled) {
  const rec = { track: trackNumber, track_enabled: trackEnabled };
  const grab = async (key, fn) => { try { rec[key] = await fn(); } catch (e) { rec[key] = null; } };
  await grab("name", () => item.getName());
  await grab("start_s", async () => secs(await item.getStartTime()));
  await grab("end_s", async () => secs(await item.getEndTime()));
  await grab("in_s", async () => secs(await item.getInPoint()));
  await grab("out_s", async () => secs(await item.getOutPoint()));
  await grab("disabled", () => item.isDisabled());
  await grab("adjustment", () => item.isAdjustmentLayer());
  await grab("speed", () => item.getSpeed());
  rec.path = null;
  rec.nested = false;
  let projectItem = null;
  try { projectItem = await item.getProjectItem(); } catch (e) {}
  if (projectItem) {
    let clip = null;
    try { clip = ppro.ClipProjectItem.cast(projectItem); } catch (e) {}
    if (clip) {
      try { rec.nested = !!(await clip.isSequence()); } catch (e) {}
      if (!rec.nested) {
        try { rec.path = (await clip.getMediaFilePath()) || null; } catch (e) {}
      }
    }
  }
  return rec;
}

const clips = [];
for (let i = 0; i < tracks.length; i++) {
  // A muted video track is taken as one with its output (eye) switched off; not yet seen live on a hidden track.
  let trackEnabled = true;
  try { trackEnabled = !(await tracks[i].isMuted()); } catch (e) {}
  const items = perTrack[i] || [];
  const recs = await Promise.all(items.map((item) => readItem(item, i + 1, trackEnabled)));
  for (const rec of recs) clips.push(rec);
}
return { sequence: seq.name, fps, duration_s, video_tracks: tracks.length, read_ms: Date.now() - startedAt, clips };
