// Read-only: Motion Position/Scale of named clips on the active sequence's video tracks.
const C = ppro.Constants;
const want = new Set(["C3997.MP4", "C3970.MP4", "C3902.MP4", "C3953.MP4"]);
const project = await ppro.Project.getActiveProject();
const seq = await project.getActiveSequence();
const n = await seq.getVideoTrackCount();
const out = [];
for (let ti = 0; ti < n; ti++) {
  const track = await seq.getVideoTrack(ti);
  let items = [];
  project.lockedAccess(() => { items = track.getTrackItems(C.TrackItemType.CLIP, false) || []; });
  for (const item of items) {
    let name = null;
    try { name = await item.getName(); } catch (e) {}
    if (!want.has(name)) continue;
    const start = await item.getStartTime();
    const rec = { track: ti + 1, name, start_s: start.seconds, params: {} };
    const chain = await item.getComponentChain();
    let count = 0;
    project.lockedAccess(() => { count = chain.getComponentCount(); });
    for (let i = 0; i < count; i++) {
      let comp = null;
      project.lockedAccess(() => { comp = chain.getComponentAtIndex(i); });
      let mn = null;
      try { mn = await comp.getMatchName(); } catch (e) {}
      if (mn !== "AE.ADBE Motion") continue;
      let pc = 0;
      project.lockedAccess(() => { pc = comp.getParamCount(); });
      for (let j = 0; j < pc; j++) {
        let p = null;
        project.lockedAccess(() => { p = comp.getParam(j); });
        if (!p) continue;
        const dn = p.displayName;
        if (dn !== "Position" && dn !== "Scale" && dn !== "Rotation" && dn !== "Anchor Point") continue;
        try {
          const v = await p.getValueAtTime(start);
          rec.params[dn] = (v && typeof v === "object") ? JSON.parse(JSON.stringify(v)) : v;
        } catch (e) { rec.params[dn] = "error: " + e; }
      }
    }
    out.push(rec);
  }
}
const size = await seq.getFrameSize();
return { frame: JSON.parse(JSON.stringify(size)), clips: out };
