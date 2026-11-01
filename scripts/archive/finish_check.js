const ppro = {}; const ARGS = {};
(async () => {
// Finish an imported Videtor cut inside Premiere (run through `prpr exec`; ARGS is prepended by videtor finish):
// the LUT as each source clip's Input LUT, Lumetri with the grade on every video clip, Warp Stabilizer on the
// handheld shots. Each step is a transaction (one undo per step); nothing outside the named sequence changes.
const C = ppro.Constants;
const A = ARGS;
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
if (!seq) throw new Error(`Sequence "${A.sequence}" is not in the open project: import the XML first`);

const report = { sequence: A.sequence, lut: { set: 0, failed: [], id_after: null }, lumetri: 0, lumetri_params: {}, stabilized: [], missing: [], dry_run: !!A.dry_run };

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

async function addEffect(item, matchName) {
  if (await findComponent(item, matchName)) return await findComponent(item, matchName);  // never twice
  const chain = await item.getComponentChain();
  const component = await ppro.VideoFilterFactory.createComponent(matchName);
  if (!A.dry_run) tx(`videtor: ${matchName}`, (add) => add(chain.createAppendComponentAction(component)));
  return await findComponent(item, matchName);
}

function setParams(component, values) {
  const done = {};
  if (!component) return done;
  let n = 0;
  project.lockedAccess(() => { n = component.getParamCount(); });
  const wanted = Object.keys(values);
  const actions = [];
  for (let j = 0; j < n; j++) {
    let p = null;
    project.lockedAccess(() => { p = component.getParam(j); });
    if (!p) continue;
    const name = p.displayName;
    if (wanted.includes(name) && !(name in done)) {
      const kf = p.createKeyframe(Number(values[name]));
      actions.push(p.createSetValueAction(kf, true));
      done[name] = values[name];
    }
  }
  if (!A.dry_run && actions.length) tx("videtor: grade values", (add) => { for (const a of actions) add(a); });
  return done;
}

// 1. the LUT on every source clip the cut uses (footage interpretation, so every use of a take converts)
const seen = new Set();
for (let t = 0; t < A.tracks.length; t++) {
  for (const item of await clipsOn(A.tracks[t])) {
    let pi = null;
    try { pi = await item.getProjectItem(); } catch (e) {}
    if (!pi) continue;
    let clip = null;
    try { clip = ppro.ClipProjectItem.cast(pi); } catch (e) {}
    if (!clip) continue;
    let path = null;
    try { path = await clip.getMediaFilePath(); } catch (e) {}
    if (!path || seen.has(path) || !A.lut) continue;
    seen.add(path);
    try {
      const fi = await clip.getFootageInterpretation();
      fi.setInputLUTID(String(A.lut));
      if (!A.dry_run) tx("videtor: input LUT", (add) => add(clip.createSetFootageInterpretationAction(fi)));
      const after = await clip.getFootageInterpretation();
      report.lut.id_after = after.getInputLUTID();
      report.lut.set += 1;
    } catch (e) {
      report.lut.failed.push(`${path.split("/").pop()}: ${e}`);
    }
  }
}

// 2. the grade on every video clip of the cut, alternates included so a swap stays graded
for (const ti of A.tracks) {
  for (const item of await clipsOn(ti)) {
    const lumetri = await addEffect(item, "AE.ADBE Lumetri");
    const done = setParams(lumetri, A.grade);
    if (lumetri) report.lumetri += 1;
    report.lumetri_params = done;
  }
}

// 3. Warp Stabilizer on the handheld shots, found by track and start
for (const want of A.stabilize) {
  let hit = null;
  for (const item of await clipsOn(want.track)) {
    let start = null, name = null;
    try { start = (await item.getStartTime()).seconds; } catch (e) {}
    try { name = await item.getName(); } catch (e) {}
    if (start !== null && Math.abs(start - want.start_s) < 0.03 && name === want.name) { hit = item; break; }
  }
  if (!hit) { report.missing.push(`${want.slot} ${want.name} @${want.start_s}`); continue; }
  const ws = await addEffect(hit, "AE.ADBE SubspaceStabilizer");
  report.stabilized.push(`${want.slot} ${want.name}${ws ? "" : " (not added)"}`);
}
return report;
});
print("syntax ok");
