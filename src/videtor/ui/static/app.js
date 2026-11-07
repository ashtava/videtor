/* Videtor UI: plain JavaScript over the local API (ui/server.py). No build step, no framework. */
const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
};
const fileUrl = (p) => "/file?path=" + encodeURIComponent(p);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let state = { projects: [], libraries: [], jobs: [], out_root: "", window: false };
let currentProject = null;
let currentLibrary = null;
const pollers = {};

/* ---- navigation ---- */
document.querySelectorAll("nav button[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
function showView(name) {
  document.querySelectorAll("nav button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("active", s.id === "view-" + name));
  if (name === "project") loadProject();
  if (name === "jobs") renderJobs();
  if (name === "library") loadLibrary();
}
document.querySelectorAll("#project-tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll("#project-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
}

/* ---- state ---- */
async function refreshState() {
  state = await api("/api/state");
  $("repo-line").textContent = state.repo;
  if (!$("prep-out").value) $("prep-out").value = state.out_root;
  if (!$("lib-out").value) $("lib-out").value = state.out_root + "/library";
  const sel = $("project-select");
  const keep = sel.value || (currentProject && currentProject.dir);
  sel.innerHTML = state.projects.slice().reverse().map((p) => `<option value="${esc(p.dir)}">${esc(p.name)} — ${esc(p.dir)}</option>`).join("");
  if (keep && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  const ls = $("lib-select");
  const keepL = ls.value || (currentLibrary && currentLibrary.dir);
  ls.innerHTML = (state.libraries || []).slice().reverse().map((l) => `<option value="${esc(l.dir)}">${esc(l.name)} — ${esc(l.dir)}</option>`).join("");
  if (keepL && [...ls.options].some((o) => o.value === keepL)) ls.value = keepL;
  const busy = state.jobs.some((j) => j.status === "running" || j.status === "paused" || j.status === "waiting");
  $("jobs-dot").classList.toggle("busy", busy);
  if (document.querySelector("#view-jobs.active")) renderJobs();
}

/* ---- folder browser ---- */
let browseTarget = null;
let browsePath = "/Volumes";
document.querySelectorAll("button[data-browse]").forEach((b) => b.addEventListener("click", () => browse(b.dataset.browse)));
async function browse(targetId) {
  browseTarget = targetId;
  const start = $(targetId).value || (targetId.endsWith("out") ? state.out_root : "/Volumes");
  if (state.window) {
    const r = await api("/api/pick-folder", { start });
    if (r.path) { $(targetId).value = r.path; afterPick(targetId); return; }
    if (!r.error || !r.error.startsWith("no native")) { if (r.error) alert(r.error); return; }
  }
  await openBrowser(start);
}
async function openBrowser(path) {
  const r = await api("/api/fs?path=" + encodeURIComponent(path));
  if (r.error && path !== "/Volumes") return openBrowser("/Volumes");
  browsePath = r.path;
  $("browser-path").textContent = r.path;
  $("browser-info").textContent = `${r.dirs.length} folders, ${r.videos} video files here`;
  $("browser-list").innerHTML = r.dirs.map((d) => `<li data-dir="${esc(d)}">${esc(d)}/</li>`).join("") || `<li class="muted">no subfolders</li>`;
  $("browser-list").querySelectorAll("li[data-dir]").forEach((li) => li.addEventListener("click", () => openBrowser(browsePath.replace(/\/$/, "") + "/" + li.dataset.dir)));
  $("browser-up").onclick = () => r.parent && openBrowser(r.parent);
  $("browser").classList.add("open");
}
$("browser-cancel").onclick = () => $("browser").classList.remove("open");
$("browser-choose").onclick = () => { $("browser").classList.remove("open"); if (browseTarget) { $(browseTarget).value = browsePath; afterPick(browseTarget); } };
function afterPick(targetId) {
  if (targetId === "prep-folder" && !$("prep-name").value) $("prep-name").value = $("prep-folder").value.split("/").filter(Boolean).pop() || "";
  if (targetId === "lib-folder") { addLibFolder(); }
}

/* ---- job logs ---- */
function watch(jobId, preId, onDone) {
  if (pollers[preId]) clearInterval(pollers[preId]);
  const pre = $(preId);
  pre.textContent = "";
  pre.style.display = "";
  let since = 0;
  const tick = async () => {
    const r = await api(`/api/jobs/${jobId}?since=${since}`);
    if (r.error) { clearInterval(pollers[preId]); return; }
    if (r.lines.length) { pre.textContent += (pre.textContent ? "\n" : "") + r.lines.join("\n"); pre.scrollTop = pre.scrollHeight; }
    since = r.next;
    const s = r.job.status;
    if (!["running", "paused", "waiting", "queued"].includes(s)) {
      clearInterval(pollers[preId]);
      delete pollers[preId];
      refreshState();
      if (onDone) onDone(r.job);
    }
  };
  pollers[preId] = setInterval(tick, 700);
  tick();
}

/* ---- prep ---- */
$("prep-folder").addEventListener("change", () => afterPick("prep-folder"));
$("prep-run").onclick = async () => {
  const body = { folder: $("prep-folder").value.trim(), out: $("prep-out").value.trim(), name: $("prep-name").value.trim(), dense: $("prep-dense").checked, workers: $("prep-workers").value || null };
  if (!body.folder) { $("prep-msg").textContent = "pick the input folder"; return; }
  const r = await api("/api/prep", body);
  if (r.error) { $("prep-msg").textContent = r.error; $("prep-msg").className = "bad"; return; }
  $("prep-msg").className = "muted";
  $("prep-msg").textContent = `${r.job.id} ${r.job.status}: ${r.project.dir}`;
  await refreshState();
  $("project-select").value = r.project.dir;
  watch(r.job.id, "prep-log", (job) => { $("prep-msg").textContent = `${job.status}: open the Project tab`; });
};

/* ---- project ---- */
$("project-select").onchange = loadProject;
$("project-refresh").onclick = loadProject;
$("project-add").onclick = async () => {
  const r = await api("/api/project/add", { dir: $("project-add-dir").value.trim() });
  if (r.error) { $("project-msg").textContent = r.error; return; }
  $("project-add-dir").value = "";
  await refreshState();
  $("project-select").value = r.project.dir;
  loadProject();
};
$("lib-add-existing").onclick = async () => {
  const r = await api("/api/library/add", { dir: $("lib-add-dir").value.trim() });
  if (r.error) { $("lib-status").textContent = r.error; return; }
  $("lib-add-dir").value = "";
  await refreshState();
  $("lib-select").value = r.library.dir;
  loadLibrary();
};
async function loadProject() {
  const dir = $("project-select").value;
  if (!dir) { $("project-msg").textContent = "no project yet: prep one"; return; }
  const p = await api("/api/project?dir=" + encodeURIComponent(dir));
  if (p.error) { $("project-msg").textContent = p.error; return; }
  currentProject = p;
  $("project-msg").textContent = p.exists ? `${p.key} · ${p.folder}` : "the output folder does not exist yet (prep running?)";
  $("project-classes").innerHTML = p.classes ? Object.entries(p.classes).map(([c, n]) => `<span><b>${n}</b> ${esc(c)}</span>`).join("") : `<span class="muted">no shot types yet</span>`;
  $("project-overview").textContent = p.overview || "No overview yet.";
  $("project-sheets").innerHTML = p.sheets.map((s) => `<img class="sheet" src="${fileUrl(s)}" loading="lazy">`).join("");
  if (document.activeElement !== $("brief")) $("brief").value = p.brief || "";
  if (document.activeElement !== $("shotmap")) $("shotmap").value = p.shotmap || "";
  showErrors(p.errors, !!p.shotmap);
  $("xml-line").textContent = p.xml ? p.xml : "No cut yet.";
  $("xml-reveal").style.display = p.xml ? "" : "none";
  $("xml-reveal").onclick = () => api("/api/reveal", { path: p.xml });
  $("cut-sheet").innerHTML = p.cut_sheet ? `<img class="sheet" src="${fileUrl(p.cut_sheet)}&t=${Date.now()}">` : "";
  renderSlots(p);
}
function showErrors(errors, haveMap) {
  const el = $("shotmap-errors");
  if (!haveMap) { el.textContent = ""; return; }
  el.innerHTML = errors.length ? `<span class="bad">${errors.map(esc).join(" · ")}</span>` : `<span class="ok">shot map ok</span>`;
}
$("save-project").onclick = async () => {
  if (!currentProject) return;
  const r = await api("/api/project/save", { project: currentProject.dir, brief: $("brief").value, shotmap: $("shotmap").value });
  if (r.error) { $("shotmap-errors").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  showErrors(r.errors, !!$("shotmap").value.trim());
  $("project-msg").textContent = "saved";
};
$("validate-shotmap").onclick = async () => { const r = await api("/api/validate", { shotmap: $("shotmap").value }); showErrors(r.errors, true); };
$("music-run").onclick = async () => {
  const r = await api("/api/music", { folder: $("music-folder").value.trim(), length: $("music-length").value, reveal_at: $("music-reveal").value || null, project: currentProject && currentProject.dir });
  if (r.error) { $("music-log").textContent = r.error; return; }
  watch(r.job.id, "music-log");
};
$("search-run").onclick = async () => {
  if (!currentProject) return;
  const r = await api("/api/search", { project: currentProject.dir, query: $("search-query").value.trim(), top: $("search-top").value });
  if (r.error) { $("search-log").textContent = r.error; return; }
  watch(r.job.id, "search-log");
};
$("cut-run").onclick = async () => {
  if (!currentProject) return;
  const r = await api("/api/cut", { project: currentProject.dir, sfx: $("cut-sfx").checked });
  if (r.error) { $("cut-msg").textContent = r.error; $("cut-msg").className = "bad"; return; }
  $("cut-msg").className = "muted";
  $("cut-msg").textContent = `${r.job.id} running: preps are paused meanwhile`;
  watch(r.job.id, "cut-log", async (job) => { $("cut-msg").textContent = job.status; await loadProject(); if (job.status === "done") showTab("look"); });
};
function renderSlots(p) {
  const el = $("slots");
  if (!p.picks) { el.innerHTML = ""; return; }
  el.innerHTML = p.picks.slots.map((s) => {
    const cand = (c, k) => !c ? `<div class="cand muted">—</div>` :
      `<div class="cand ${k === 0 ? "pick" : ""}" data-slot="${esc(s.id)}" data-k="${k}">` +
      (c.still ? `<img src="${fileUrl(c.still)}" loading="lazy">` : `<div class="muted">no still</div>`) +
      `<div class="name">${esc(c.name)} @${Number(c.in_s).toFixed(1)}s ${k === 0 ? "· pick" : "· " + k}</div><div class="why">${esc((c.reasons || []).slice(0, 2).join("; "))}</div></div>`;
    return `<div class="slot"><div class="head"><b>${esc(s.id)}</b><span class="muted">${esc(s.section || "")} · ${Number(s.start_s || 0).toFixed(2)}s · ${Number(s.length_s || 0).toFixed(2)}s</span><span class="muted">${esc(Array.isArray(s.show) ? s.show[0] : s.show)}</span>${s.second_look ? `<span class="chip">swapped: ${esc(s.second_look.why || s.second_look.chose || "")}</span>` : ""}</div>` +
      cand(s.pick, 0) + (s.alternates || []).slice(0, 3).map((c, i) => cand(c, i + 1)).join("") + `</div>`;
  }).join("");
  el.querySelectorAll(".cand[data-k]").forEach((c) => {
    if (c.dataset.k === "0") return;
    c.addEventListener("click", async () => {
      const choices = {}; choices[c.dataset.slot] = Number(c.dataset.k);
      const notes = {}; if ($("look-note").value.trim()) notes[c.dataset.slot] = $("look-note").value.trim();
      const r = await api("/api/look", { project: p.dir, choices, notes });
      if (r.error) { alert(r.error); return; }
      $("look-note").value = "";
      watch(r.job.id, "look-log", () => loadProject());
    });
  });
}

/* ---- library ---- */
const libFolders = [];
$("lib-add").onclick = addLibFolder;
function addLibFolder() {
  const v = $("lib-folder").value.trim();
  if (v && !libFolders.includes(v)) libFolders.push(v);
  $("lib-folder").value = "";
  if (!$("lib-name").value && libFolders.length) $("lib-name").value = libFolders[0].split("/").filter(Boolean).pop();
  $("lib-folders").innerHTML = libFolders.map((f, i) => `<li>${esc(f)} <a href="#" data-i="${i}">remove</a></li>`).join("");
  $("lib-folders").querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); libFolders.splice(Number(a.dataset.i), 1); addLibFolder(); }));
}
$("lib-run").onclick = async () => {
  const r = await api("/api/library/build", { folders: libFolders, out: $("lib-out").value.trim(), name: $("lib-name").value.trim(), extract: $("lib-extract").checked, transcribe: $("lib-transcribe").checked });
  if (r.error) { $("lib-msg").textContent = r.error; $("lib-msg").className = "bad"; return; }
  $("lib-msg").className = "muted"; $("lib-msg").textContent = `${r.job.id} running → ${r.library.dir}`;
  await refreshState();
  $("lib-select").value = r.library.dir;
  watch(r.job.id, "lib-log", () => loadLibrary());
};
$("lib-open").onclick = loadLibrary;
$("lib-select").onchange = loadLibrary;
$("lib-learn").onclick = async () => {
  const dir = $("lib-select").value; if (!dir) return;
  const r = await api("/api/library/learn", { out: dir });
  if (r.error) { $("lib-status").textContent = r.error; return; }
  watch(r.job.id, "lib-log", () => loadLibrary());
};
let libFilter = "all";
async function loadLibrary() {
  const dir = $("lib-select").value;
  if (!dir) { $("lib-view").innerHTML = ""; return; }
  const lib = await api("/api/library?dir=" + encodeURIComponent(dir));
  if (lib.error) { $("lib-status").textContent = lib.error; $("lib-view").innerHTML = ""; return; }
  currentLibrary = lib;
  $("lib-status").textContent = `built ${(lib.built_at || "").replace("T", " ").slice(0, 16)} · ${lib.bites} bites · ${lib.moved} moved since the build` + (lib.model ? ` · model from ${lib.model.n} labels (cv ${lib.model.cv_accuracy})` : " · rules only");
  const classes = ["wide", "close-up", "talking-head", "interview-bite", "b-roll"];
  const counts = classes.map((c) => `<span><b>${lib.counts[c] || 0}</b> ${c}</span>`).join("");
  const rows = lib.clips.filter((x) => libFilter === "all" || x.now === libFilter || (libFilter === "check" && x.conf < 0.6)).slice(0, 400);
  $("lib-view").innerHTML = `<div class="counts">${counts}</div>` +
    `<div class="row"><a href="#" id="lib-reveal">Reveal the folders in Finder</a>${lib.bites_xml ? ` · <a href="#" id="bites-reveal">bites.xml</a>` : ""}</div>` +
    lib.sheets.map((s) => `<img class="sheet" src="${fileUrl(s)}" loading="lazy">`).join("") +
    `<div class="filter"><span class="muted">Show</span>${["all", "check", ...classes].map((c) => `<button class="btn ${libFilter === c ? "primary" : ""}" data-f="${c}">${c === "check" ? "worth a look" : c}</button>`).join("")}<span class="muted">${rows.length} of ${lib.clips.length}</span></div>` +
    `<table><thead><tr><th>clip</th><th>s</th><th>class</th><th>conf</th><th>why</th></tr></thead><tbody>` +
    rows.map((x) => `<tr class="lib-row"><td style="font-family:var(--mono)">${esc(x.name)}${x.copy_of ? ` <span class="muted">(copy of ${esc(x.copy_of)})</span>` : ""}</td><td>${Number(x.duration_s).toFixed(0)}</td>` +
      `<td><select data-name="${esc(x.name)}">${classes.map((c) => `<option ${c === x.now ? "selected" : ""}>${c}</option>`).join("")}</select></td>` +
      `<td class="${x.conf < 0.6 ? "warn" : ""}">${Number(x.conf).toFixed(2)} <span class="muted">${esc(x.by)}</span></td><td class="muted">${esc((x.reasons || []).join("; "))}</td></tr>`).join("") + `</tbody></table>`;
  $("lib-reveal").onclick = (e) => { e.preventDefault(); api("/api/reveal", { path: dir + "/library.md" }); };
  const b = $("bites-reveal"); if (b) b.onclick = (e) => { e.preventDefault(); api("/api/reveal", { path: lib.bites_xml }); };
  $("lib-view").querySelectorAll("button[data-f]").forEach((btn) => btn.addEventListener("click", () => { libFilter = btn.dataset.f; loadLibrary(); }));
  $("lib-view").querySelectorAll("select[data-name]").forEach((sel) => sel.addEventListener("change", async () => {
    const r = await api("/api/library/move", { out: dir, name: sel.dataset.name, to: sel.value });
    $("lib-status").textContent = r.error ? r.error : `${sel.dataset.name}: ${r.from} → ${r.to} (Learn when you are done)`;
  }));
}

/* ---- jobs ---- */
let shownJob = null;
async function renderJobs() {
  const r = await api("/api/jobs");
  const tb = $("jobs-table").querySelector("tbody");
  tb.innerHTML = r.jobs.slice().reverse().map((j) => `<tr><td>${j.id}</td><td><span class="chip ${esc(j.kind)}">${esc(j.kind)}</span>${esc(j.title)}</td>` +
    `<td class="status-${esc(j.status)}">${esc(j.status)}${j.paused_by ? ` by ${j.paused_by}` : ""}</td><td>${j.elapsed}s</td>` +
    `<td><button class="btn" data-show="${j.id}">log</button> ${j.status === "running" ? `<button class="btn" data-act="pause" data-id="${j.id}">pause</button>` : ""}${j.status === "paused" ? `<button class="btn" data-act="resume" data-id="${j.id}">resume</button>` : ""}${["running", "paused", "waiting"].includes(j.status) ? `<button class="btn danger" data-act="cancel" data-id="${j.id}">cancel</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">No jobs yet.</td></tr>`;
  tb.querySelectorAll("button[data-show]").forEach((b) => b.addEventListener("click", () => { shownJob = b.dataset.show; watch(shownJob, "jobs-log"); }));
  tb.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => { await api(`/api/jobs/${b.dataset.id}/${b.dataset.act}`, {}); renderJobs(); }));
}

/* ---- boot ---- */
refreshState().then(() => { if (state.projects.length) { $("project-select").value = state.projects[state.projects.length - 1].dir; } });
setInterval(refreshState, 4000);
