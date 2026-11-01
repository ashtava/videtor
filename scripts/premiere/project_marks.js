// Every clip in the open project: bin, media path, colour label, markers, and
// Premiere's project-metadata columns (source in/out marks, Good, usage, label).
const TPS = 254016000000;
const secs = (t) => { try { return t ? t.seconds : null; } catch (e) { return null; } };
const columns = (xml) => {
  const fields = {};
  const re = /<premierePrivateProjectMetaData:(Column\.[\w.]+)(?:\s[^>]*)?>([\s\S]*?)<\/premierePrivateProjectMetaData:\1>/g;
  let m;
  while ((m = re.exec(xml))) {
    const body = m[2];
    const value = /<rdf:value>([^<]*)<\/rdf:value>/.exec(body);
    if (value) {
      const num = (tag) => { const r = new RegExp("<premierePrivateProjectMetaData:" + tag + ">(-?\\d+)<").exec(body); return r ? Number(r[1]) / TPS : null; };
      fields[m[1]] = { tc: value[1], min_s: num("min"), max_s: num("max"), offset_s: num("offset") };
    } else {
      fields[m[1]] = body.trim().slice(0, 300);
    }
  }
  return fields;
};
const project = await ppro.Project.getActiveProject();
const root = await project.getRootItem();
const out = [];
const stack = [{ folder: root, path: "" }];
while (stack.length) {
  const { folder, path } = stack.pop();
  for (const item of await folder.getItems()) {
    if (item.type === ppro.ProjectItem.TYPE_BIN) {
      stack.push({ folder: ppro.FolderItem.cast(item), path: path + item.name + "/" });
      continue;
    }
    const rec = { bin: path, name: item.name };
    try { rec.color_label = await item.getColorLabelIndex(); } catch (e) {}
    let clip = null;
    try { clip = ppro.ClipProjectItem.cast(item); } catch (e) {}
    if (clip) {
      try { rec.path = await clip.getMediaFilePath(); } catch (e) {}
      try {
        const markers = await ppro.Markers.getMarkers(clip);
        let list = [];
        project.lockedAccess(() => { list = markers.getMarkers() || []; });
        rec.markers = list.map((mk) => {
          const e = {};
          const g = (k, fn) => { try { e[k] = fn(); } catch (err) { e[k] = null; } };
          g("name", () => mk.getName()); g("comments", () => mk.getComments()); g("type", () => mk.getType());
          g("start_s", () => secs(mk.getStart())); g("duration_s", () => secs(mk.getDuration())); g("color", () => mk.getColorIndex());
          return e;
        });
      } catch (e) { rec.markers_error = String(e); }
    }
    try { rec.columns = columns(await ppro.Metadata.getProjectMetadata(item)); } catch (e) { rec.meta_error = String(e); }
    out.push(rec);
  }
}
return out;
