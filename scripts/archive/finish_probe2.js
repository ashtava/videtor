const C = ppro.Constants;
const project = await ppro.Project.getActiveProject();
const t0 = Date.now();
const out = { project: project ? project.name : null, steps: {} };
const seqs = await project.getSequences();
let seq = null;
for (const s of seqs) { if (s.name === "colourstone-mia-60s") seq = s; }
out.sequences = seqs.length; out.found = !!seq;
out.steps.find_ms = Date.now() - t0;
if (!seq) return out;
const counts = [];
let first = null;
for (let ti = 0; ti < 4; ti++) {
  const track = await seq.getVideoTrack(ti);
  let items = [];
  project.lockedAccess(() => { items = track.getTrackItems(C.TrackItemType.CLIP, false) || []; });
  counts.push(items.length);
  if (!first && items.length) first = items[0];
}
out.clips_per_track = counts;
out.steps.list_ms = Date.now() - t0;
let t = Date.now();
const pi = await first.getProjectItem();
const clip = ppro.ClipProjectItem.cast(pi);
out.first = await clip.getMediaFilePath();
out.steps.projectitem_ms = Date.now() - t;
t = Date.now();
try { const fi = await clip.getFootageInterpretation(); out.lut_id_now = fi.getInputLUTID(); } catch (e) { out.fi_error = String(e); }
out.steps.interpretation_ms = Date.now() - t;
t = Date.now();
const chain = await first.getComponentChain();
let n = 0; project.lockedAccess(() => { n = chain.getComponentCount(); });
const names = [];
for (let i = 0; i < n; i++) { let c = null; project.lockedAccess(() => { c = chain.getComponentAtIndex(i); }); names.push(await c.getMatchName()); }
out.first_components = names;
out.steps.chain_ms = Date.now() - t;
t = Date.now();
try {
  const fi2 = await clip.getFootageInterpretation();
  out.fi_methods = Object.getOwnPropertyNames(Object.getPrototypeOf(fi2)).filter((k) => /LUT|Color|lut|color/.test(k));
  fi2.setInputLUTID("/Users/paritoshsrivastava/Downloads/LUTs/S-Log3 SV Vision3.cube");
  out.lut_id_local_after_set = fi2.getInputLUTID();
} catch (e) { out.set_error = String(e); }
out.steps.set_local_ms = Date.now() - t;
out.steps.total_ms = Date.now() - t0;
return out;
