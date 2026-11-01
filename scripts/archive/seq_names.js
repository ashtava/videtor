const project = await ppro.Project.getActiveProject();
const seqs = await project.getSequences();
const out = [];
for (const s of seqs) { let n = null; try { n = s.name; } catch (e) {} let g = null; try { g = s.guid ? String(s.guid) : null; } catch (e) {} out.push({ name: n, guid: g }); }
return out;
