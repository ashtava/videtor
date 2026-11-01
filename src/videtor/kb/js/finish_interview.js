// Finish an imported Videtor cut inside Premiere, one stage per call (run through `prpr exec`; ARGS is prepended
// by `videtor finish`). Stages: "grade" (Lumetri with the grade values on the clips of A.tracks), "stabilize"
// (Warp Stabilizer on the listed shots), "lut" (copy the Input LUT of the source clip named A.lut_from to every
// source clip the cut uses: Premiere keys a LUT by an internal id, given to a .cube only when it is picked once
// in its own panel). Every change is a transaction (one undo each); nothing outside the named sequence changes.
const C = ppro.Constants;
const A = ARGS;
const t0 = Date.now();
const project = await ppro.Project.getActiveProject();
if (!project) throw new Error("No project open in Premiere");

function tx(label, build) {
  let ok = false;
  project.lockedAccess(() => {
    const actions = [];
    build((a) => { if (a) actions.push(a); });
    if (!actions.length) { ok = true; return; }
    ok = project.executeTransaction((compound) => { for (const a of actions) compound.addAction(a); }, label);
  });
  return ok;
}

const sequences = await project.getSequences();
let seq = null;
for (const s of sequences) { if (s.name === A.sequence) { seq = s; break; } }
if (!seq) throw new Error(`Sequence "${A.sequence}" is not in the open project (${project.name}): import the XML first`);
const report = { stage: A.stage, sequence: A.sequence, project: project.name, done: 0, skipped: 0, notes: [], dry_run: !!A.dry_run };

async function clipsOn(trackIndex) {
  const track = await seq.getVideoTrack(trackIndex);
  if (!track) return [];
  let items = [];
  project.lockedAccess(() => { items = track.getTrackItems(C.TrackItemType.CLIP, false) || []; });
  return items;
}

async function findComponent(item, matchName) {
  const chain = await item.getComponentChain();
  let n = 0;
  project.lockedAccess(() => { n = chain.getComponentCount(); });
  for (let i = n - 1; i >= 0; i--) {
    let c = null;
    project.lockedAccess(() => { c = chain.getComponentAtIndex(i); });
    let mn = null;
    try { mn = await c.getMatchName(); } catch (e) {}
    if (mn === matchName) return c;
  }
  return null;
}

async function ensureEffect(item, matchName) {
  const have = await findComponent(item, matchName);
  if (have) return { component: have, added: false };
  if (A.dry_run) return { component: null, added: false };
  const chain = await item.getComponentChain();
  const component = await ppro.VideoFilterFactory.createComponent(matchName);
  tx(`videtor: ${matchName}`, (add) => add(chain.createAppendComponentAction(component)));
  return { component: await findComponent(item, matchName), added: true };
}

function setParams(component, values) {
  const done = {};
  if (!component) return done;
  let n = 0;
  project.lockedAccess(() => { n = component.getParamCount(); });
  const actions = [];
  for (let j = 0; j < n; j++) {
    let p = null;
    project.lockedAccess(() => { p = component.getParam(j); });
    if (!p) continue;
    const name = p.displayName;
    if (name in values && !(name in done)) {
      actions.push(p.createSetValueAction(p.createKeyframe(Number(values[name])), true));
      done[name] = values[name];
    }
  }
  if (!A.dry_run && actions.length) tx("videtor: grade values", (add) => { for (const a of actions) add(a); });
  return done;
}

if (A.stage === "grade") {
  for (const ti of A.tracks) {
    for (const item of await clipsOn(ti)) {
      const { component, added } = await ensureEffect(item, "AE.ADBE Lumetri");
      const set = setParams(component, A.grade);
      if (component) { report.done += 1; report.params = set; } else { report.skipped += 1; }
      if (!added && component) report.notes.push("Lumetri already there: values set again");
    }
  }
} else if (A.stage === "stabilize") {
  for (const want of A.stabilize) {
    let hit = null;
    for (const item of await clipsOn(want.track)) {
      let start = null, name = null;
      try { start = (await item.getStartTime()).seconds; } catch (e) {}
      try { name = await item.getName(); } catch (e) {}
      if (start !== null && Math.abs(start - want.start_s) < 0.03 && name === want.name) { hit = item; break; }
    }
    if (!hit) { report.skipped += 1; report.notes.push(`not found: ${want.slot} ${want.name} @${want.start_s}`); continue; }
    const { component } = await ensureEffect(hit, "AE.ADBE SubspaceStabilizer");
    if (component) report.done += 1; else report.skipped += 1;
    report.notes.push(`${want.slot} ${want.name}`);
  }
} else if (A.stage === "lut") {
  const clips = new Map();
  for (const ti of A.tracks) {
    for (const item of await clipsOn(ti)) {
      let pi = null;
      try { pi = await item.getProjectItem(); } catch (e) {}
      if (!pi) continue;
      const clip = ppro.ClipProjectItem.cast(pi);
      const path = await clip.getMediaFilePath();
      if (path) clips.set(path, clip);
    }
  }
  let id = null;
  for (const [path, clip] of clips) {
    if (path.split("/").pop() === A.lut_from) id = (await clip.getFootageInterpretation()).getInputLUTID();
  }
  report.lut_id = id;
  if (!id || /^0{8}-/.test(id)) throw new Error(`${A.lut_from} has no Input LUT yet: pick the LUT on it once (Modify > Interpret Footage > Color Management)`);
  for (const [path, clip] of clips) {
    const fi = await clip.getFootageInterpretation();
    if (fi.getInputLUTID() === id) { report.skipped += 1; continue; }
    fi.setInputLUTID(id);
    if (!A.dry_run) tx("videtor: input LUT", (add) => add(clip.createSetFootageInterpretationAction(fi)));
    report.done += 1;
  }
}
report.ms = Date.now() - t0;
return report;
