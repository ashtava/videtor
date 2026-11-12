/* Videtor UI: plain JavaScript over the local API (ui/server.py). No build step, no framework. */
const $ = (id) => document.getElementById(id);
const TOKEN = document.querySelector('meta[name="videtor-token"]')?.content || "";  // the server refuses a POST without it
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Videtor-Token": TOKEN }, body: JSON.stringify(body) });
  return r.json();
};
const fileUrl = (p) => "/file?path=" + encodeURIComponent(p);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* a private window: fine */ } },
};
const fmtDur = (s) => {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
};
const LIVE = ["running", "paused", "waiting", "queued"];
let state = { projects: [], libraries: [], jobs: [], out_root: "" };
let current = null;                          // the open project, as /api/project returns it
let currentDir = store.get("videtor.project");
let currentLibrary = null;
const pollers = {};                          // status element id -> interval
const watching = {};                         // status element id -> {job, project}

/* ---- navigation ---- */
function showView(name) {
  document.querySelectorAll("nav button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("active", s.id === "view-" + name));
  store.set("videtor.view", name);
  if (name === "project") loadProject();
  if (name === "jobs") renderJobs();
  if (name === "library") loadLibrary();
  if (name === "teach") loadTeach();
}
function showTab(name) {
  document.querySelectorAll("#project-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
}
function goto(target) {
  const [view, tab] = target.split(":");
  showView(view);
  if (tab) showTab(tab);
}
document.querySelectorAll("nav button[data-view]").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
document.querySelectorAll("#project-tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) { e.preventDefault(); goto(g.dataset.goto); }
});

/* ---- the guides: open until the user closes one, remembered per page ---- */
document.querySelectorAll("details.guide").forEach((d) => {
  const key = "videtor.guide." + d.dataset.guide;
  const saved = store.get(key);
  d.open = saved === null ? true : saved === "1";
  d.addEventListener("toggle", () => store.set(key, d.open ? "1" : "0"));
});

/* ---- progress in words ---- */
function stageText(job) {
  const p = job.progress || {};
  if (job.status === "paused") return job.paused_by ? "Paused while a cut runs" : "Paused";
  if (job.status === "waiting") return "Waits for the cut to finish";
  if (job.status === "done") return "Done";
  if (job.status === "failed") return "Stopped with an error";
  if (job.status === "cancelled") return "Stopped";
  return (p.stage || "Working") + (p.total ? ` · ${p.done || 0} of ${p.total}` : "");
}
function renderStatus(el, job) {
  if (!el) return;
  const p = job.progress || {};
  const pct = p.total ? Math.min(100, Math.round(100 * (p.done || 0) / p.total)) : null;
  const running = job.status === "running";
  const eta = running && p.eta_s ? ` · about ${fmtDur(p.eta_s)} left` : "";
  el.hidden = false;
  el.className = "status st-" + job.status;
  el.innerHTML = `<div class="st-line"><b>${esc(stageText(job))}</b>${eta}<span class="muted"> · ${fmtDur(job.elapsed)} so far</span></div>` +
    `<div class="bar ${running && pct === null ? "indet" : ""}"><div style="width:${job.status === "done" ? 100 : (pct ?? 0)}%"></div></div>` +
    (job.status === "failed" ? `<div class="bad small">${esc(p.last || "See Details below.")}</div>` : "");
}
function stopWatch(key) {
  if (pollers[key]) clearInterval(pollers[key]);
  delete pollers[key];
  delete watching[key];
}
function watch(jobId, opts) {
  const key = opts.status || opts.log;
  if (watching[key] && watching[key].job === jobId && pollers[key]) return;  // already on it
  stopWatch(key);
  watching[key] = { job: jobId, project: opts.project || null };
  const pre = opts.log ? $(opts.log) : null;
  if (pre) { pre.textContent = ""; if (opts.showLog) pre.hidden = false; }
  if (opts.logbox) $(opts.logbox).hidden = false;
  let since = 0;
  const tick = async () => {
    const r = await api(`/api/jobs/${jobId}?since=${since}`);
    if (r.error) { stopWatch(key); return; }
    if (pre && r.lines.length) { pre.textContent += (pre.textContent ? "\n" : "") + r.lines.join("\n"); pre.scrollTop = pre.scrollHeight; }
    since = r.next;
    if (opts.status) renderStatus($(opts.status), r.job);
    if (!LIVE.includes(r.job.status)) {
      stopWatch(key);
      refreshState();
      if (opts.onDone) opts.onDone(r.job);
    }
  };
  pollers[key] = setInterval(tick, 800);
  tick();
}

/* ---- state: projects, libraries, what is running ---- */
async function refreshState() {
  state = await api("/api/state");
  if (!$("prep-out").value) { $("prep-out").value = state.out_root; updateDest(); }
  if (!$("lib-out").value) $("lib-out").value = state.out_root + "/library";
  renderProjectCards();
  const ls = $("lib-select");
  const keepL = ls.value || (currentLibrary && currentLibrary.dir);
  ls.innerHTML = (state.libraries || []).slice().reverse().map((l) => `<option value="${esc(l.dir)}">${esc(l.name)}</option>`).join("") || `<option value="">none yet</option>`;
  if (keepL && [...ls.options].some((o) => o.value === keepL)) ls.value = keepL;
  const live = state.jobs.filter((j) => ["running", "paused", "waiting"].includes(j.status));
  $("jobs-dot").classList.toggle("busy", live.length > 0);
  const mini = $("activity-mini");
  if (live.length) {
    const j = live[live.length - 1];
    mini.hidden = false;
    mini.innerHTML = `<b>${esc(j.title)}</b><span>${esc(stageText(j))}</span>` + (live.length > 1 ? `<span class="muted">and ${live.length - 1} more</span>` : "");
  } else mini.hidden = true;
  if ($("view-jobs").classList.contains("active")) renderJobs();
  const badge = $("teach-badge");
  badge.hidden = !state.pending;
  badge.textContent = state.pending || "";
}

/* ---- folder choosing: the Mac's own dialog ---- */
let browseTarget = null;
let browsePath = "/Volumes";
const PROMPTS = { "tp-footage": "Choose the footage folder of that job", "ts-source": "Choose the folder of finished videos", "prep-folder": "Choose the footage folder", "prep-out": "Choose where the project goes", "music-folder": "Choose the music folder",
  "lib-folder": "Choose a footage folder", "lib-out": "Choose where the sorted footage goes", "project-add-dir": "Choose a project folder", "lib-add-dir": "Choose a library folder" };
document.querySelectorAll("button[data-browse]").forEach((b) => b.addEventListener("click", () => browse(b.dataset.browse)));
async function browse(targetId) {
  browseTarget = targetId;
  const start = $(targetId).value || (/(out|dir)$/.test(targetId) ? state.out_root : "/Volumes");
  const btn = document.querySelector(`button[data-browse="${targetId}"]`);
  if (btn) { btn.disabled = true; btn.textContent = "Choosing…"; }
  try {
    const r = await api("/api/pick-folder", { start, prompt: PROMPTS[targetId] || "Choose a folder" });
    if (r.path) { $(targetId).value = r.path; afterPick(targetId); return; }
    if (r.cancelled) return;
    await openBrowser(start);  // no dialog came up (not a Mac?): the page's own folder list
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Choose…"; }
  }
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
  if (targetId === "prep-folder") {
    if (!$("prep-name").value) $("prep-name").value = slug($("prep-folder").value.split("/").filter(Boolean).pop());
    describeFolder();
  }
  if (targetId === "prep-folder" || targetId === "prep-out") updateDest();
  if (targetId === "lib-folder") addLibFolder();
}

/* ---- new project ---- */
const FOLDER_HINT = "The shoot's folder on its drive; cards and subfolders inside are fine.";
function updateDest() {
  const out = $("prep-out").value.trim().replace(/\/$/, "");
  const name = slug($("prep-name").value);
  $("prep-dest").textContent = out ? `The project will be saved in ${out}/${name || "<name>"}` : "";
}
async function describeFolder() {
  const f = $("prep-folder").value.trim();
  if (!f) { $("prep-folder-info").textContent = FOLDER_HINT; return; }
  $("prep-folder-info").textContent = "Looking at the folder…";
  const r = await api("/api/scan?path=" + encodeURIComponent(f));
  if (r.error) { $("prep-folder-info").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  const where = r.drive && r.drive.name ? ` on ${r.drive.name}` : "";
  $("prep-folder-info").innerHTML = r.videos
    ? `<span class="ok">${r.videos}${r.partial ? "+" : ""} video files, ${r.gb} GB${esc(where)}.</span>`
    : `<span class="warn">No video files in this folder or under it.</span>`;
}
$("prep-name").addEventListener("input", updateDest);
$("prep-out").addEventListener("input", updateDest);
$("prep-folder").addEventListener("change", () => afterPick("prep-folder"));
$("prep-run").onclick = async () => {
  const body = { folder: $("prep-folder").value.trim(), out: $("prep-out").value.trim(), name: $("prep-name").value.trim(), dense: $("prep-dense").checked, workers: $("prep-workers").value || null };
  if (!body.folder) { $("prep-msg").innerHTML = `<span class="bad">Choose the footage folder first.</span>`; return; }
  $("prep-run").disabled = true;
  const r = await api("/api/prep", body);
  $("prep-run").disabled = false;
  if (r.error) { $("prep-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  $("prep-msg").textContent = "";
  $("prep-folder").value = ""; $("prep-name").value = ""; $("prep-folder-info").textContent = FOLDER_HINT; updateDest();
  currentDir = r.project.dir;
  store.set("videtor.project", currentDir);
  await refreshState();
  goto("project:overview");  // the project opens at once: the brief can go in while the footage is read
};

/* ---- projects ---- */
function renderProjectCards() {
  const el = $("project-cards");
  const list = state.projects.slice().reverse();
  if (!list.length) {
    el.innerHTML = `<div class="empty"><b>No projects yet.</b> A project starts from the folder of a shoot. <button class="btn primary" data-goto="prep">Start a new project</button></div>`;
    return;
  }
  el.innerHTML = list.map((p) => `<button class="pcard ${p.dir === currentDir ? "sel" : ""}" data-dir="${esc(p.dir)}"><span class="pname">${esc(p.name)}</span>` +
    `<span class="pstat tone-${esc(p.status.tone)}">${esc(p.status.text)}</span><span class="pfolder">${esc(p.folder || p.dir)}</span></button>`).join("");
  el.querySelectorAll(".pcard").forEach((c) => c.addEventListener("click", () => selectProject(c.dataset.dir)));
}
function selectProject(dir) {
  currentDir = dir;
  store.set("videtor.project", dir);
  renderProjectCards();
  loadProject(true);
}
async function loadProject(pick) {
  if (!state.projects.some((p) => p.dir === currentDir)) currentDir = state.projects.length ? state.projects[state.projects.length - 1].dir : null;
  $("project-body").hidden = !currentDir;
  if (!currentDir) return;
  const p = await api("/api/project?dir=" + encodeURIComponent(currentDir));
  if (p.error) { $("project-msg").textContent = p.error; return; }
  current = p;
  $("project-title").textContent = p.name;
  const d = p.drive || {};
  $("project-msg").innerHTML = esc(p.folder || "") + (d.name ? (d.connected ? ` · <span class="ok">${esc(d.name)} connected</span>` : ` · <span class="warn">${esc(d.name)} not connected</span>`) : "");
  const states = Object.fromEntries((p.steps || []).map((s) => [s.id, s.state]));
  document.querySelectorAll("#project-tabs button").forEach((b) => { b.dataset.state = states[b.dataset.tab] || "todo"; });
  if (pick || !document.querySelector("#project-tabs button.active")) showTab((p.next && p.next.tab) || "overview");
  renderNext(p);
  renderOverview(p);
  renderBrief(p);
  renderCutTab(p);
  renderReview(p);
  showJobs(p);
}
function showJobs(p) {
  // the status cards belong to the open project: drop another project's, show this one's running or failed job
  for (const key of ["overview-status", "cut-status"]) {
    if (watching[key] && watching[key].project !== p.dir) stopWatch(key);
  }
  const live = p.job, last = p.last_job;
  const card = (kind) => (kind === "prep" ? ["overview-status", "overview-log", "overview-logbox"] : kind === "cut" ? ["cut-status", "cut-log", "cut-logbox"] : null);
  for (const kind of ["prep", "cut"]) {
    const [status, log, box] = card(kind);
    if (live && live.kind === kind) {
      watch(live.id, { status, log, logbox: box, project: p.dir, onDone: (job) => { loadProject(); if (kind === "cut" && job.status === "done") showTab("look"); } });
    } else if (!pollers[status]) {
      if (last && last.kind === kind && last.status === "failed") { renderStatus($(status), last); $(box).hidden = false; }
      else { $(status).hidden = true; $(box).hidden = true; }
    }
  }
}
function renderNext(p) {
  const n = p.next || {};
  const el = $("next-step");
  el.hidden = !n.text;
  const btn = n.action === "reprep" ? `<button class="btn primary" id="reprep">Read the footage again</button>`
    : n.tab ? `<button class="btn" data-goto="project:${esc(n.tab)}">Go there</button>` : "";
  el.innerHTML = `<span class="label">Next</span><span class="ntext">${esc(n.text)}</span>${btn}`;
  const rp = $("reprep");
  if (rp) rp.onclick = async () => {
    const r = await api("/api/reprep", { project: p.dir });
    if (r.error) { $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
    showTab("overview");
    loadProject();
  };
}
function renderOverview(p) {
  $("project-classes").innerHTML = p.classes ? Object.entries(p.classes).map(([c, n]) => `<span><b>${n}</b> ${esc(c)}</span>`).join("") : "";
  $("project-overview").textContent = p.overview || "Not written yet.";
  const reading = p.job && p.job.kind === "prep";
  $("project-sheets").innerHTML = p.sheets.length ? p.sheets.map((s) => `<img class="sheet" src="${fileUrl(s)}" loading="lazy">`).join("")
    : reading ? "" : `<div class="empty">No overview yet.</div>`;
}

/* ---- brief and plan ---- */
function claudeRequest(p) {
  return `/cut-from-brief The footage of this project is already read. Project folder: ${p.dir} (shot-map key "${p.key}"). ` +
    `The brief is in ${p.dir}/brief.md. Read the overview there, write ${p.dir}/shotmap.json from the brief (name the shot types it draws on in "classes"), ` +
    `check it with the validator, and stop before cutting: I cut from the Videtor app.`;
}
function renderBrief(p) {
  if (document.activeElement !== $("brief")) $("brief").value = p.brief || "";
  if (document.activeElement !== $("shotmap")) $("shotmap").value = p.shotmap || "";
  showErrors(p.errors || [], !!(p.shotmap || "").trim());
  renderPlan(p);
}
function renderPlan(p) {
  const el = $("plan-view");
  if (!(p.shotmap || "").trim()) {
    const ready = (p.brief || "").trim();
    el.innerHTML = `<div class="empty"><p><b>No plan yet.</b> The plan lists the shots the cut fills: what each one must show and how long it lasts. Claude writes it from the brief and the overview.</p>
      <ol><li>${ready ? "The brief is saved." : "Paste and save the brief above."}</li><li>Open <b>Claude Code</b> on the Videtor folder and paste this request:</li></ol>
      <textarea readonly id="claude-req" class="req">${esc(claudeRequest(p))}</textarea>
      <div class="row"><button class="btn" id="copy-req">Copy the request</button><span class="muted">When Claude is done, the plan appears here.</span></div></div>`;
    $("copy-req").onclick = () => copyText($("claude-req"), $("copy-req"));
    $("plan-edit").open = false;
    return;
  }
  let m = null;
  try { m = JSON.parse(p.shotmap); } catch (e) { m = null; }
  if (!m) { el.innerHTML = `<div class="empty"><span class="bad">The plan is not valid JSON.</span> Open Edit the plan below.</div>`; return; }
  const music = m.music ? (Array.isArray(m.music) ? `music: a medley of ${m.music.filter((x) => x.path).length} pieces` : `music: ${esc((m.music.path || "").split("/").pop())} from ${esc(m.music.in_s ?? 0)} s`) : "no music";
  const sections = (m.sections || []).filter((s) => (s.slots || []).length);
  const shots = sections.reduce((a, s) => a + s.slots.length, 0);
  const len = (s) => (s.beats ? `${s.beats} beat${s.beats === 1 ? "" : "s"}` : s.length_s ? `${s.length_s} s` : "");
  const what = (s) => (s.black ? "black" : Array.isArray(s.show) ? s.show[0] : s.show || "");
  const extra = (s) => [s.byte && "someone speaking", s.ramp && "speed ramp", s.speed && s.speed < 1 && "slow motion", s.flicker && "flicker", s.takes && "named take"].filter(Boolean).join(", ");
  const rows = sections.map((sec) => sec.slots.map((s, i) => `<tr>${i === 0 ? `<td rowspan="${sec.slots.length}" class="sec">${esc(sec.name)}${sec.match ? ` <span class="chip">match cuts</span>` : ""}</td>` : ""}` +
    `<td>${esc(what(s))}${extra(s) ? `<div class="muted small">${esc(extra(s))}</div>` : ""}</td><td>${esc(len(s))}</td><td>${esc([].concat(s.class || m.classes || []).join(", ") || "any")}</td></tr>`).join("")).join("");
  el.innerHTML = `<div class="plan-meta"><span><b>${esc(m.length_s)} s</b>, ${esc(m.width)}×${esc(m.height)}</span><span>${music}</span><span>${sections.length} sections, ${shots} shots</span></div>` +
    `<div class="tablewrap"><table class="plan"><thead><tr><th>Section</th><th>What the shot shows</th><th>Length</th><th>From</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function showErrors(errors, haveMap) {
  const el = $("shotmap-errors");
  if (!haveMap) { el.textContent = ""; return; }
  el.innerHTML = errors.length ? `<span class="bad">${errors.map(esc).join(" · ")}</span>` : `<span class="ok">The plan is usable.</span>`;
  if (errors.length) $("plan-edit").open = true;
}
async function copyText(ta, btn) {
  const done = (msg) => { if (btn) { const t = btn.textContent; btn.textContent = msg; setTimeout(() => { btn.textContent = t; }, 1600); } };
  try { await navigator.clipboard.writeText(ta.value); done("Copied"); }
  catch (e) { ta.focus(); ta.select(); try { document.execCommand("copy"); done("Copied"); } catch (e2) { done("Selected: press ⌘C"); } }
}
$("save-brief").onclick = async () => {
  if (!current) return;
  const r = await api("/api/project/save", { project: current.dir, brief: $("brief").value });
  $("brief-msg").textContent = r.error ? r.error : "Saved.";
  setTimeout(() => { $("brief-msg").textContent = ""; }, 2500);
  loadProject();
};
$("save-plan").onclick = async () => {
  if (!current) return;
  const r = await api("/api/project/save", { project: current.dir, shotmap: $("shotmap").value });
  if (r.error) { $("shotmap-errors").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  showErrors(r.errors, !!$("shotmap").value.trim());
  loadProject();
};
$("validate-shotmap").onclick = async () => { const r = await api("/api/validate", { shotmap: $("shotmap").value }); showErrors(r.errors, true); };

/* ---- music and search ---- */
$("music-run").onclick = async () => {
  const r = await api("/api/music", { folder: $("music-folder").value.trim(), length: $("music-length").value, reveal_at: $("music-reveal").value || null, project: current && current.dir });
  if (r.error) { $("music-log").hidden = false; $("music-log").textContent = r.error; return; }
  watch(r.job.id, { log: "music-log", showLog: true });
};
$("search-run").onclick = async () => {
  if (!current) return;
  const r = await api("/api/search", { project: current.dir, query: $("search-query").value.trim(), top: 12 });
  if (r.error) { $("search-log").hidden = false; $("search-log").textContent = r.error; return; }
  watch(r.job.id, { log: "search-log", showLog: true });
};

/* ---- cut ---- */
function renderCutTab(p) {
  const d = p.drive || {}, n = p.cut_needs;
  const hasPlan = !!(p.shotmap || "").trim(), bad = (p.errors || []).length > 0;
  const blocked = !!(n && n.to_decode && !d.connected);
  let line = "";
  if (!hasPlan) line = `<span class="warn">No plan yet.</span> Write it on <a href="#" data-goto="project:brief">Brief &amp; plan</a> first.`;
  else if (bad) line = `<span class="warn">The plan has ${p.errors.length} problem(s).</span> Fix them on <a href="#" data-goto="project:brief">Brief &amp; plan</a>.`;
  else if (blocked) line = `<span class="bad">Plug in ${esc(d.name || "the footage drive")}.</span> ${n.to_decode} of the ${n.takes} takes the plan can use still need reading from it.`;
  else if (n && n.to_decode) line = `<span class="ok">${esc(d.name || "The footage")} is connected.</span> This cut reads ${n.to_decode} takes in full first (a few minutes); later cuts are quick.`;
  else if (n) line = `<span class="ok">Everything the plan can use is read: ${n.takes} takes to choose from.</span>` + (d.connected ? "" : " The drive can stay unplugged for the cut; Premiere needs it to play the clips.");
  $("cut-drive").innerHTML = line;
  const running = !!(p.job && p.job.kind === "cut");
  $("cut-run").disabled = !hasPlan || bad || blocked || running;
  $("cut-run").textContent = p.picks ? "Cut again" : "Cut";
}
$("cut-run").onclick = async () => {
  if (!current) return;
  $("cut-run").disabled = true;
  const r = await api("/api/cut", { project: current.dir, sfx: $("cut-sfx").checked });
  if (r.error) { $("cut-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; $("cut-run").disabled = false; return; }
  $("cut-msg").textContent = "";
  loadProject();
};

/* ---- review and export ---- */
function renderReview(p) {
  const d = p.drive || {};
  $("export").innerHTML = p.xml
    ? `<div class="exportcard"><div><b>Your timeline:</b> ${esc(p.xml.split("/").pop())}</div>
       <ol><li>Open your project in Premiere.</li><li>File ▸ Import…, and choose this file (or drag it in from Finder).</li><li>Keep ${esc(d.name || "the footage drive")} connected so the clips play.</li></ol>
       <div class="row"><button class="btn primary" id="xml-reveal">Show in Finder</button><span class="muted">Picks on V1; runners-up on V2 and V3, switched off; music on A1.</span></div>
       ${(p.finishing || []).length ? `<div><b>Left to do by hand in Premiere:</b><ul>${p.finishing.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>` : ""}</div>`
    : `<div class="empty">No cut yet. <a href="#" data-goto="project:cut">Cut it</a> first.</div>`;
  const rv = $("xml-reveal");
  if (rv) rv.onclick = () => api("/api/reveal", { path: p.xml });
  $("cut-sheet").innerHTML = p.cut_sheet ? `<img class="sheet" src="${fileUrl(p.cut_sheet)}&t=${Date.now()}">` : "";
  renderPreview(p);
  renderSlots(p);
  renderTeach(p);
}
/* ---- watch the cut without Premiere ---- */
function renderPreview(p) {
  const el = $("preview");
  if (!p.picks) { el.innerHTML = ""; return; }
  el.innerHTML = (p.preview ? `<video class="preview" controls preload="metadata" src="${fileUrl(p.preview)}&t=${Date.now()}"></video>` : "") +
    `<div class="row"><button class="btn" id="preview-make">${p.preview ? "Make the preview again" : "Watch the cut here"}</button>
     <span class="muted">A small video of the cut with its music, made from the footage (the drive must be connected; about a minute). No effects or sound effects in it.</span></div>`;
  $("preview-make").onclick = async () => {
    const r = await api("/api/preview", { project: p.dir });
    if (r.error) { $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
    watch(r.job.id, { status: "preview-status", onDone: () => loadProject() });
  };
}
/* ---- when the job is finished: a verdict, and the finished video read back ---- */
function renderTeach(p) {
  const el = $("teach");
  if (!p.picks) { el.innerHTML = ""; return; }
  const f = p.feedback || {}, v = f.verdict, s = f.summary;
  const said = v ? `<span class="chip">You said: ${v.good ? "good" : "not good"}${v.note ? " · " + esc(v.note) : ""}</span>` : "";
  const read = s ? `<div class="muted">Last read: ${s.kept} kept, ${s.slipped + s.trimmed} moved, ${s.swapped_to_alternate + s.swapped} swapped, ${s.removed} removed of ${s.slots} shots.</div>` : "";
  el.innerHTML = `<div class="exportcard"><div><b>When you have finished it</b></div>
    <div class="row"><span>Was this cut a good start?</span><button class="btn" id="verdict-good">Good</button><button class="btn" id="verdict-not">Not good</button>
      <input type="text" id="verdict-note" placeholder="why, in a line (optional)" style="min-width:280px">${said}</div>
    <div class="row"><span>Your finished video:</span><span class="muted" id="final-path">${esc(f.final ? f.final.split("/").pop() : "none in the project's folder yet")}</span>
      <button class="btn" id="final-choose">Choose…</button><button class="btn primary" id="final-read">Read my finished video</button></div>
    ${read}
    <div class="row"><button class="btn" id="learn-run">Learn from my feedback</button>
      <span class="muted">${f.choices || 0} of your swaps kept so far. Learning is tried on your past cuts first and kept only if they get no worse.</span></div></div>`;
  let video = f.final || null;
  const verdict = async (good) => { await api("/api/verdict", { project: p.dir, good, note: $("verdict-note").value.trim() }); loadProject(); };
  $("verdict-good").onclick = () => verdict(true);
  $("verdict-not").onclick = () => verdict(false);
  $("final-choose").onclick = async () => {
    const r = await api("/api/pick-video", { start: p.dir });
    if (r.path) { video = r.path; $("final-path").textContent = r.path.split("/").pop(); }
  };
  $("final-read").onclick = async () => {
    const r = await api("/api/readback", { project: p.dir, video });
    if (r.error) { $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
    watch(r.job.id, { log: "teach-log", showLog: true, onDone: () => loadProject() });
  };
  $("learn-run").onclick = async () => {
    const r = await api("/api/learn", {});
    if (!r.error) watch(r.job.id, { log: "teach-log", showLog: true });
  };
}
function renderSlots(p) {
  const el = $("slots");
  if (!p.picks) { el.innerHTML = ""; return; }
  el.innerHTML = p.picks.slots.map((s) => {
    const cand = (c, k) => !c ? `<div class="cand muted">—</div>` :
      `<div class="cand ${k === 0 ? "pick" : ""}" data-slot="${esc(s.id)}" data-k="${k}">` +
      (c.still ? `<img src="${fileUrl(c.still)}" loading="lazy">` : `<div class="muted">no still</div>`) +
      `<div class="name">${esc(c.name)} @${Number(c.in_s).toFixed(1)}s ${k === 0 ? "· in the cut" : "· use this"}</div><div class="why">${esc((c.reasons || []).slice(0, 2).join("; "))}</div></div>`;
    return `<div class="slot"><div class="head"><b>${esc(Array.isArray(s.show) ? s.show[0] : s.show)}</b><span class="muted">${esc(s.section || "")} · ${Number(s.start_s || 0).toFixed(1)} s · ${Number(s.length_s || 0).toFixed(1)} s</span>` +
      `${s.second_look ? `<span class="chip">swapped${s.second_look.why ? ": " + esc(s.second_look.why) : ""}</span>` : ""}</div>` +
      cand(s.pick, 0) + (s.alternates || []).slice(0, 3).map((c, i) => cand(c, i + 1)).join("") + `</div>`;
  }).join("");
  el.querySelectorAll(".cand[data-k]").forEach((c) => {
    if (c.dataset.k === "0") return;
    c.addEventListener("click", async () => {
      const choices = {}; choices[c.dataset.slot] = Number(c.dataset.k);
      const notes = {}; if ($("look-note").value.trim()) notes[c.dataset.slot] = $("look-note").value.trim();
      c.classList.add("busy");
      const r = await api("/api/look", { project: p.dir, choices, notes });
      if (r.error) { c.classList.remove("busy"); $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
      $("look-note").value = "";
      watch(r.job.id, { log: "look-log", onDone: () => loadProject() });
    });
  });
}
$("project-add").onclick = async () => {
  const r = await api("/api/project/add", { dir: $("project-add-dir").value.trim() });
  if (r.error) { $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  $("project-add-dir").value = "";
  await refreshState();
  selectProject(r.project.dir);
};

/* ---- library ---- */
const libFolders = [];
$("lib-add").onclick = addLibFolder;
function addLibFolder() {
  const v = $("lib-folder").value.trim();
  if (v && !libFolders.includes(v)) libFolders.push(v);
  $("lib-folder").value = "";
  if (!$("lib-name").value && libFolders.length) $("lib-name").value = slug(libFolders[0].split("/").filter(Boolean).pop());
  $("lib-folders").innerHTML = libFolders.map((f, i) => `<li>${esc(f)} <a href="#" data-i="${i}">remove</a></li>`).join("");
  $("lib-folders").querySelectorAll("a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); libFolders.splice(Number(a.dataset.i), 1); addLibFolder(); }));
}
$("lib-run").onclick = async () => {
  if (!libFolders.length && $("lib-folder").value.trim()) addLibFolder();
  const r = await api("/api/library/build", { folders: libFolders, out: $("lib-out").value.trim(), name: $("lib-name").value.trim(), extract: $("lib-extract").checked, transcribe: $("lib-transcribe").checked });
  if (r.error) { $("lib-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  $("lib-msg").textContent = "";
  await refreshState();
  $("lib-select").value = r.library.dir;
  watch(r.job.id, { status: "lib-status-card", log: "lib-log", logbox: "lib-logbox", onDone: () => loadLibrary() });
};
$("lib-select").onchange = loadLibrary;
$("lib-learn").onclick = async () => {
  const dir = $("lib-select").value; if (!dir) return;
  const r = await api("/api/library/learn", { out: dir });
  if (r.error) { $("lib-status").textContent = r.error; return; }
  watch(r.job.id, { status: "lib-status-card", log: "lib-log", logbox: "lib-logbox", onDone: () => loadLibrary() });
};
$("lib-add-existing").onclick = async () => {
  const r = await api("/api/library/add", { dir: $("lib-add-dir").value.trim() });
  if (r.error) { $("lib-status").textContent = r.error; return; }
  $("lib-add-dir").value = "";
  await refreshState();
  $("lib-select").value = r.library.dir;
  loadLibrary();
};
let libFilter = "all";
async function loadLibrary() {
  const dir = $("lib-select").value;
  if (!dir) { $("lib-view").innerHTML = ""; $("lib-status").textContent = ""; return; }
  const lib = await api("/api/library?dir=" + encodeURIComponent(dir));
  if (lib.error) { $("lib-status").textContent = lib.error; $("lib-view").innerHTML = ""; return; }
  currentLibrary = lib;
  $("lib-status").textContent = `sorted ${(lib.built_at || "").replace("T", " ").slice(0, 16)} · ${lib.bites} bites` + (lib.moved ? ` · ${lib.moved} changed by you since (Learn to keep them)` : "") +
    (lib.model ? ` · sorting learnt from ${lib.model.n} of your choices` : "");
  const classes = ["wide", "close-up", "talking-head", "interview-bite", "b-roll"];
  const counts = classes.map((c) => `<span><b>${lib.counts[c] || 0}</b> ${c}</span>`).join("");
  const rows = lib.clips.filter((x) => libFilter === "all" || x.now === libFilter || (libFilter === "check" && x.conf < 0.6)).slice(0, 400);
  $("lib-view").innerHTML = `<div class="counts">${counts}</div>` +
    `<div class="row"><button class="btn" id="lib-reveal">Show the folders in Finder</button>${lib.bites_xml ? `<button class="btn" id="bites-reveal">Show the bites sequence</button>` : ""}</div>` +
    lib.sheets.map((s) => `<img class="sheet" src="${fileUrl(s)}" loading="lazy">`).join("") +
    `<div class="filter"><span class="muted">Show</span>${["all", "check", ...classes].map((c) => `<button class="btn small ${libFilter === c ? "primary" : ""}" data-f="${c}">${c === "check" ? "worth a look" : c}</button>`).join("")}<span class="muted">${rows.length} of ${lib.clips.length}</span></div>` +
    `<div class="tablewrap"><table><thead><tr><th>Clip</th><th>s</th><th>Type</th><th>Sure</th><th>Why</th></tr></thead><tbody>` +
    rows.map((x) => `<tr class="lib-row"><td class="mono">${esc(x.name)}${x.copy_of ? ` <span class="muted">(copy of ${esc(x.copy_of)})</span>` : ""}</td><td>${Number(x.duration_s).toFixed(0)}</td>` +
      `<td><select data-name="${esc(x.name)}">${classes.map((c) => `<option ${c === x.now ? "selected" : ""}>${c}</option>`).join("")}</select></td>` +
      `<td class="${x.conf < 0.6 ? "warn" : ""}">${Math.round(100 * x.conf)}%</td><td class="muted">${esc((x.reasons || []).join("; "))}</td></tr>`).join("") + `</tbody></table></div>`;
  $("lib-reveal").onclick = () => api("/api/reveal", { path: dir + "/library.md" });
  const b = $("bites-reveal"); if (b) b.onclick = () => api("/api/reveal", { path: lib.bites_xml });
  $("lib-view").querySelectorAll("button[data-f]").forEach((btn) => btn.addEventListener("click", () => { libFilter = btn.dataset.f; loadLibrary(); }));
  $("lib-view").querySelectorAll("select[data-name]").forEach((sel) => sel.addEventListener("change", async () => {
    const r = await api("/api/library/move", { out: dir, name: sel.dataset.name, to: sel.value });
    $("lib-status").textContent = r.error ? r.error : `${sel.dataset.name}: ${r.from} → ${r.to}. Learn from my sorting when you are done.`;
  }));
}

/* ---- activity ---- */
async function renderJobs() {
  const r = await api("/api/jobs");
  const tb = $("jobs-table").querySelector("tbody");
  tb.innerHTML = r.jobs.slice().reverse().map((j) => {
    const p = j.progress || {};
    const pct = j.status === "running" && p.total ? Math.round(100 * (p.done || 0) / p.total) : null;
    const left = j.status === "running" && p.eta_s ? ` · about ${fmtDur(p.eta_s)} left` : "";
    return `<tr><td><b>${esc(j.title)}</b></td><td class="st-${esc(j.status)}">${esc(stageText(j))}${pct !== null ? ` (${pct}%)` : ""}<span class="muted">${left}</span></td><td>${fmtDur(j.elapsed)}</td>` +
      `<td class="acts"><button class="btn small" data-show="${j.id}">Details</button>` +
      (j.status === "running" ? `<button class="btn small" data-act="pause" data-id="${j.id}">Pause</button>` : "") +
      (j.status === "paused" && !j.paused_by ? `<button class="btn small" data-act="resume" data-id="${j.id}">Resume</button>` : "") +
      (["running", "paused", "waiting"].includes(j.status) ? `<button class="btn small danger" data-act="cancel" data-id="${j.id}">Stop</button>` : "") + `</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">Nothing yet.</td></tr>`;
  tb.querySelectorAll("button[data-show]").forEach((b) => b.addEventListener("click", () => { $("jobs-logbox").hidden = false; $("jobs-logbox").open = true; watch(b.dataset.show, { log: "jobs-log" }); }));
  tb.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => { await api(`/api/jobs/${b.dataset.id}/${b.dataset.act}`, {}); renderJobs(); }));
}

/* ---- a camera left sideways ---- */
$("orient-run").onclick = async () => {
  if (!current) return;
  const r = await api("/api/orient", { project: current.dir, which: $("orient-which").value.trim(), rotate: Number($("orient-turn").value) });
  if (r.error) { $("project-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  watch(r.job.id, { status: "overview-status", log: "overview-log", logbox: "overview-logbox", onDone: () => loadProject() });
};

/* ---- one file: a finished video or a Premiere XML ---- */
document.querySelectorAll("button[data-pick]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try {
    const r = await api("/api/pick-file", { kind: b.dataset.kind, start: state.home });
    if (r.path) $(b.dataset.pick).value = r.path;
  } finally { b.disabled = false; }
}));

/* ---- Teach Videtor: everything it learns from is given here ---- */
let teach = null;
const watchTeach = (id) => watch(id, { status: "teach-status", log: "teach-job-log", logbox: "teach-logbox", onDone: () => loadTeach() });
async function teachPost(path, body) {
  const r = await api(path, body);
  $("teach-msg").innerHTML = r.error ? `<span class="bad">${esc(r.error)}</span>` : "";
  if (r.job) watchTeach(r.job.id);
  return r;
}
async function loadTeach() {
  teach = await api("/api/teach");
  $("kinds").innerHTML = teach.kinds.map((k) => `<option value="${esc(k)}">`).join("");
  renderTeachJobs();
  renderTeachEdits();
  renderRules();
  renderKnown();
  if (teach.active) watchTeach(teach.active.id);
}
const day = (iso) => (iso || "").slice(0, 10);
function renderTeachJobs() {
  const el = $("teach-jobs"), jobs = teach.projects || [];
  if (!jobs.length) { el.innerHTML = `<div class="empty">No cut yet. When a project has a cut, it appears here.</div>`; return; }
  const rows = jobs.map((j) => {
    const s = j.summary, v = j.verdict;
    const what = j.unread ? `<b>Finished video found:</b> ${esc(j.unread.split("/").pop())}. Not read yet.`
      : s ? `Read ${esc(day(j.read_at))}: ${s.kept} kept, ${s.slipped + s.trimmed} moved, ${s.swapped_to_alternate + s.swapped} swapped, ${s.removed} removed of ${s.slots} shots.`
      : `<span class="muted">Waiting for your finished video: save the export in this project's folder, or choose it.</span>`;
    return `<div class="teachrow" data-dir="${esc(j.dir)}"><div class="who">${esc(j.name)}</div><div class="what">${what}${v && v.note ? `<div class="muted small">You said: ${esc(v.note)}</div>` : ""}</div>
      <span class="acts">${j.unread ? `<button class="btn primary" data-act="read">Read it</button>` : `<button class="btn" data-act="choose">Choose the video…</button>`}
      <button class="btn" data-act="folder">Show folder</button></span>
      <span class="acts"><span class="muted">Was the cut a good start?</span>
      <button class="btn ${v && v.good ? "primary" : ""}" data-act="good">Good</button><button class="btn ${v && !v.good ? "primary" : ""}" data-act="not">Not good</button></span></div>`;
  }).join("");
  el.innerHTML = `<div class="card">` + (teach.pending ? `<div class="row"><button class="btn primary" id="teach-pending">Read ${teach.pending} finished video${teach.pending > 1 ? "s" : ""} and learn</button></div>` : "") + rows + `</div>`;
  if ($("teach-pending")) $("teach-pending").onclick = () => teachPost("/api/teach/pending", {});
  el.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const dir = b.closest(".teachrow").dataset.dir, act = b.dataset.act;
    if (act === "read") return teachPost("/api/readback", { project: dir });
    if (act === "folder") return api("/api/reveal", { path: dir + "/picks.json" });
    if (act === "choose") {
      const r = await api("/api/pick-file", { kind: "video", start: dir });
      if (r.path) teachPost("/api/readback", { project: dir, video: r.path });
      return;
    }
    const note = act === "not" ? (window.prompt("What was wrong, in a line? (optional)") || "") : "";
    await api("/api/verdict", { project: dir, good: act === "good", note });
    loadTeach();
  }));
}
function renderTeachEdits() {
  const edits = (teach.buckets || []).flatMap((b) => b.edits);
  if (!edits.length) { $("teach-edits").innerHTML = ""; return; }
  const row = (e) => {
    const read = e.mapped_takes > 20;  // enough of its footage is already read to learn from, drive or no drive
    const what = e.learnt ? `Videtor learnt what you kept from this (${e.kept_takes} takes).`
      : read && e.tried ? `Its footage is read. Videtor tried what it learnt from it and did not keep it: on its own this edit made a past cut worse. More edits of this kind will help.`
      : read ? `Its footage is read (${e.mapped_takes} takes). Not learnt from yet.`
      : !e.folders.length ? `<span class="muted">Its footage folder is not known: add the job again above with the folder.</span>`
      : !e.connected ? `<span class="muted">Its footage is on <b>${esc(e.drive || "a drive")}</b>, which is not connected. Connect it to learn which shots you kept.</span>`
      : `Its footage is connected.`;
    return `<div class="teachrow"><div class="who">${esc(e.name)}</div><div class="what"><span class="chip">${esc(e.bucket)}</span> ${what}</div>
      ${!e.learnt && (e.connected || read) ? `<button class="btn ${e.tried ? "" : "primary"}" data-key="${esc(e.key)}">${read ? (e.tried ? "Try again" : "Learn from it") : "Read its footage and learn"}</button>` : ""}</div>`;
  };
  const waiting = edits.filter((e) => !e.learnt).length;
  $("teach-edits").innerHTML = `<details class="advanced" ${waiting && edits.some((e) => !e.learnt && e.connected) ? "open" : ""}><summary>The ${edits.length} edits you gave before (${edits.length - waiting} learnt from, ${waiting} waiting for their footage)</summary><div class="card">${edits.map(row).join("")}</div></details>`;
  $("teach-edits").querySelectorAll("button[data-key]").forEach((b) => b.addEventListener("click", () => teachPost("/api/teach/edit", { key: b.dataset.key })));
}
function renderRules() {
  const sel = $("rules-bucket"), keep = sel.value;
  sel.innerHTML = teach.kinds.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join("");
  if (keep && teach.kinds.includes(keep)) sel.value = keep;
  const b = (teach.buckets || []).find((x) => x.bucket === sel.value) || { rules: [] };
  $("rules-list").innerHTML = b.rules.length ? b.rules.map((r, i) => `<div class="rule"><span>${esc(r)}</span><button class="btn" data-rule="${i}">Remove</button></div>`).join("")
    : `<div class="muted">No rules for this kind of work yet.</div>`;
  $("rules-list").querySelectorAll("button[data-rule]").forEach((x) => x.addEventListener("click", async () => {
    await api("/api/teach/rule", { bucket: sel.value, text: b.rules[Number(x.dataset.rule)], remove: true });
    loadTeach();
  }));
}
$("rules-bucket").onchange = () => renderRules();
$("rule-add").onclick = async () => {
  const r = await api("/api/teach/rule", { bucket: $("rules-bucket").value, text: $("rule-text").value });
  $("teach-msg").innerHTML = r.error ? `<span class="bad">${esc(r.error)}</span>` : "";
  if (!r.error) { $("rule-text").value = ""; loadTeach(); }
};
function renderKnown() {
  const known = (teach.buckets || []).filter((b) => b.known);
  const keeps = (k) => !k ? null : `Knows what you keep: ${k.auc >= 0.75 ? "well" : k.auc >= 0.65 ? "fairly" : "roughly"}, from ${k.edits} job${k.edits > 1 ? "s" : ""}` +
    (k.keeps.length ? ` (leans to ${k.keeps.map((x) => esc(x.replace("concept: ", ""))).join("; ")})` : "");
  $("teach-known").innerHTML = known.length ? `<div class="known">` + known.map((b) => {
    const lines = [
      b.edits.length ? `${b.edits.length} of your edits` : null,
      b.reels ? `${b.reels} finished video${b.reels > 1 ? "s" : ""}${b.style ? ": pace, effects and cuts on the music learnt" : ""}` : null,
      b.finished ? `${b.finished} finished job${b.finished > 1 ? "s" : ""} read back${b.by_hand ? ` (${b.by_hand} cut by hand)` : ""}` : null,
      keeps(b.keeper) || (b.edits.length || b.finished ? `<span class="muted">Does not know what you keep yet: it needs a job with its footage read.</span>` : null),
      b.taste ? `Your taste in shots: from ${b.taste.pairs} of your swaps` : b.choices ? `<span class="muted">${b.choices} of your swaps so far; your taste is learnt from ${teach.taste_from} on</span>` : null,
      b.verdicts.good + b.verdicts.not ? `Your verdicts: ${b.verdicts.good} good, ${b.verdicts.not} not good` : null,
      b.rules.length ? `${b.rules.length} rule${b.rules.length > 1 ? "s" : ""} in your words` : null,
    ].filter(Boolean);
    return `<div class="card"><h3>${esc(b.bucket)}</h3><ul>${lines.map((l) => `<li>${l}</li>`).join("")}</ul></div>`;
  }).join("") + `</div>` : `<div class="empty">Nothing yet. Start with a finished job above.</div>`;
  const last = teach.last, ev = teach.evaluation;
  const said = !last ? "Videtor has not learnt from feedback yet."
    : last.kept.length ? `Last learned ${day(last.at)}: kept (${last.kept.length} model${last.kept.length > 1 ? "s" : ""}); your past cuts got no worse.`
    : last.refused.length ? `Last tried ${day(last.at)}: not kept, because it made a past cut worse (${esc((last.worse || [])[0] || "see Details")}). More examples of this kind of work will help.`
    : `Last checked ${day(last.at)}: nothing new to learn from yet.`;
  const measured = ev && ev.summary && ev.summary.right_take != null ? ` Measured on ${ev.projects} past cuts: where you edited the same footage, ${Math.round(100 * ev.summary.right_take)}% of Videtor's shots are takes you used.` : "";
  $("teach-last").textContent = said + measured;
}
$("teach-learn").onclick = () => teachPost("/api/learn", {});
$("tp-run").onclick = async () => {
  const r = await teachPost("/api/teach/project", { name: $("tp-name").value, bucket: $("tp-bucket").value, video: $("tp-video").value, xml: $("tp-xml").value, footage: $("tp-footage").value });
  if (!r.error) ["tp-name", "tp-video", "tp-xml", "tp-footage"].forEach((id) => { $(id).value = ""; });
};
$("ts-run").onclick = async () => {
  const r = await teachPost("/api/teach/style", { source: $("ts-source").value, bucket: $("ts-bucket").value });
  if (!r.error) $("ts-source").value = "";
};

/* ---- start ---- */
refreshState().then(() => {
  if (location.hash.length > 1) return goto(decodeURIComponent(location.hash.slice(1)));  // a link to one page: #teach, #project:look
  const v = store.get("videtor.view");
  showView(!state.projects.length ? "prep" : v && v !== "prep" ? v : "project");
});
setInterval(refreshState, 4000);
