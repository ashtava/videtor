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
/* a job's state is the server's word: `job.live` (jobs.py); today's list only for a server that does not send it */
const LIVE = ["running", "paused", "waiting", "queued"];
const jobLive = (j) => !!j && (typeof j.live === "boolean" ? j.live : LIVE.includes(j.status));
const jobOk = (j) => !!j && j.status === "done";
const cap = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);
const jobWhy = (j) => !j ? "Videtor no longer knows this job (was the app restarted?). Start it again."
  : j.status === "cancelled" ? "It was stopped before it finished."
  : cap((j.result && j.result.error) || (j.progress && j.progress.last) || "it stopped with an error; Details or Activity say more.");
/* answers that can come late: each request gets a number, and an answer older than one already shown is dropped */
const seqs = {};
function ticket(name) {
  const s = seqs[name] || (seqs[name] = { asked: 0, shown: 0 });
  const n = ++s.asked;
  return () => { if (n < s.shown) return false; s.shown = n; return true; };
}
let state = { projects: [], libraries: [], jobs: [], out_root: "" };
let current = null;                          // the open project, as /api/project returns it
let currentDir = store.get("videtor.project");
let shownDir = null;                         // the project whose panels are on screen
let currentLibrary = null;
const pollers = {};                          // watch key -> interval
const watching = {};                         // watch key -> {job, project, els}
const owner = {};                            // a status card or log -> the watch drawing in it (the newest)

/* ---- navigation ---- */
function showView(name) {
  document.querySelectorAll("nav button[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll("section.view").forEach((s) => s.classList.toggle("active", s.id === "view-" + name));
  store.set("videtor.view", name);
  if (name !== "project" && TL.playing) tlStop();
  if (name === "project") loadProject();
  if (name === "jobs") renderJobs();
  if (name === "library") loadLibrary();
  if (name === "teach") loadTeach();
  if (name === "promos") loadPromos();
  else document.querySelectorAll("#pr-view video").forEach((v) => v.pause());
}
function showTab(name) {
  if (name !== "look" && typeof tlStop === "function" && TL.playing) tlStop();
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
    (!jobLive(job) && !jobOk(job) ? `<div class="bad small">${esc(jobWhy(job))}</div>` : "");
}
function stopWatch(key) {
  if (pollers[key]) clearInterval(pollers[key]);
  const w = watching[key];
  delete pollers[key];
  delete watching[key];
  for (const id of (w && w.els) || []) {  // a card another live watch also draws in goes back to it
    if (owner[id] !== key) continue;
    const next = Object.keys(watching).reverse().find((k) => watching[k].els.includes(id));
    if (next) owner[id] = next; else delete owner[id];
  }
}
/* follow a job: its log, its status card, onDone(job) when it has ended (job null: the server no longer knows it).
   `key` names the watch (else the card's or the log's id); the newest watch on a card or a log draws in it. */
function watch(jobId, opts) {
  const key = opts.key || opts.status || opts.log;
  if (watching[key] && watching[key].job === jobId && pollers[key]) return;  // already on it
  stopWatch(key);
  const w = watching[key] = { job: jobId, project: opts.project || null, els: [opts.status, opts.log, opts.logbox].filter(Boolean), since: 0, busy: false };
  w.els.forEach((id) => { owner[id] = key; });
  const mine = (id) => id && owner[id] === key;
  const pre = opts.log ? $(opts.log) : null;
  if (pre) { pre.textContent = ""; if (opts.showLog) pre.hidden = false; }
  if (opts.logbox) $(opts.logbox).hidden = false;
  const tick = async () => {
    if (w.busy) return;  // the last answer is still on its way: asking again would show its lines twice
    w.busy = true;
    let r;
    try { r = await api(`/api/jobs/${jobId}?since=${w.since}`); } catch (e) { r = null; }
    w.busy = false;
    if (watching[key] !== w || !r) return;  // stopped, or another job took this place, while the answer was on its way
    if (r.error) { stopWatch(key); if (opts.onDone) opts.onDone(null); return; }
    if (pre && mine(opts.log) && r.lines.length) { pre.textContent += (pre.textContent ? "\n" : "") + r.lines.join("\n"); pre.scrollTop = pre.scrollHeight; }
    w.since = r.next;
    if (mine(opts.status)) renderStatus($(opts.status), r.job);
    if (!jobLive(r.job)) {
      if (opts.hideDone && jobOk(r.job) && mine(opts.status)) $(opts.status).hidden = true;
      stopWatch(key);
      refreshState();
      if (opts.onDone) opts.onDone(r.job);
    }
  };
  pollers[key] = setInterval(tick, 800);
  tick();
}

/* ---- a panel's run: idle, running, done or failed, and said so beside the control that started it ----
   runPanel(name, jobId, o) follows the job and keeps the panel's state in `runs`; paintRuns() shows it on the page:
     [data-run=name]        the line beside the control: what is being done, then the reason it failed (or o.done)
     [data-run-stale=name]  what was shown before, dimmed while it runs
     [data-run-btn=name]    a control held while it runs
   o: status, log, logbox, showLog, hideDone (as watch); project (the run is that project's; its lines show only there);
      what (the running words), failed (the head of a failure), older (said of what is shown, while it runs and after a
      failure; olderFailed for the failure only; text or a function), done (said after a run that worked);
      onDone(job) after a run that worked, onFail(job) after one that did not, onEnd(job) after either. */
const runs = {};
const runKey = (name, project) => (project ? name + "@" + project : name);
const runOf = (name) => runs[runKey(name, currentDir)] || runs[name];
const txt = (v) => (typeof v === "function" ? v() : v) || "";
function runPanel(name, jobId, o = {}) {
  if (!jobId) return;
  const rk = runKey(name, o.project);
  const had = runs[rk], same = had && [had, had.prev].find((x) => x && x.job === jobId);
  if (same) { if (same.state === "running") attachRun(same); return; }  // already this panel's run
  runs[rk] = { rk, name, project: o.project || null, job: jobId, state: "running", why: "", last: null, o };
  paintRuns();
  attachRun(runs[rk]);
}
function attachRun(r) {
  const o = r.o;
  watch(r.job, { key: "run:" + r.rk, status: o.status, log: o.log, logbox: o.logbox, showLog: o.showLog, hideDone: o.hideDone, project: r.project, onDone: async (job) => {
    const cur = runs[r.rk];
    if (cur !== r && !(cur && cur.prev === r)) return;  // a newer run of this panel took its place
    runs[r.rk] = r;  // a refusal said meanwhile gives way to the run's own end
    const ok = jobOk(job);
    r.state = ok ? "done" : "failed";
    r.why = ok ? "" : jobWhy(job);
    r.last = job;
    paintRuns();
    try {
      if (ok && o.onDone) await o.onDone(job);
      if (!ok && o.onFail) await o.onFail(job);
      if (o.onEnd) await o.onEnd(job);
    } finally { paintRuns(); }
  } });
}
/* a refusal from the server, said where the run's failure would be */
function refuseRun(name, why, project) {
  const rk = runKey(name, project), had = runs[rk];
  runs[rk] = { rk, name, project: project || null, job: null, state: "failed", why: cap(why), last: null, o: { failed: "Not started." }, prev: had && had.state === "running" ? had : null };
  paintRuns();
}
const runRunning = (name) => { const r = runOf(name); return !!r && (r.state === "running" || (!!r.prev && r.prev.state === "running")); };
function paintRuns() {
  document.querySelectorAll("[data-run]").forEach((el) => {
    const r = runOf(el.dataset.run), o = r ? r.o : {}, st = r ? r.state : "idle";
    const html = st === "running" ? `${esc(o.what || "Working")}…` + (txt(o.older) ? ` <span class="muted">${esc(txt(o.older))}</span>` : "")
      : st === "failed" ? `<b>${esc(o.failed || "It did not work.")}</b> ${esc(r.why)}` + (txt(o.olderFailed || o.older) ? `<br>${esc(txt(o.olderFailed || o.older))}` : "")
      : st === "done" ? esc(txt(o.done)) : "";
    if (el.innerHTML !== html) el.innerHTML = html;
    el.hidden = !html;
    ["running", "failed", "done"].forEach((c) => el.classList.toggle(c, c === st));
  });
  document.querySelectorAll("[data-run-stale]").forEach((el) => el.classList.toggle("stale", runRunning(el.dataset.runStale)));
  document.querySelectorAll("[data-run-btn]").forEach((el) => {
    if (runRunning(el.dataset.runBtn)) { if (!el.disabled) { el.disabled = true; el.dataset.held = "1"; } }
    else if (el.dataset.held) { el.disabled = false; delete el.dataset.held; }
  });
}
/* the open project changed: another project's runs stop drawing, its cards go; this project's running runs come back
   (their cards and logs drawn again from the start) */
const PROJECT_CARDS = ["overview-status", "overview-logbox", "cut-status", "cut-logbox", "preview-status", "tl-status", "look-log", "teach-log", "search-log"];
function enterProject(dir) {
  for (const k of Object.keys(watching)) if (watching[k].project) stopWatch(k);
  PROJECT_CARDS.forEach((id) => { $(id).hidden = true; });
  Object.values(runs).forEach((r) => { const go = r.state === "running" ? r : r.prev; if (r.project === dir && go && go.state === "running") attachRun(go); });
}

/* ---- state: projects, libraries, what is running ---- */
async function refreshState() {
  const fresh = ticket("state");
  const s = await api("/api/state");
  if (!fresh()) return;  // a newer answer is already shown
  state = s;
  if (!$("prep-out").value) { $("prep-out").value = state.out_root; updateDest(); }
  if (!$("lib-out").value) $("lib-out").value = state.out_root + "/library";
  renderProjectCards();
  const ls = $("lib-select");
  const keepL = ls.value || (currentLibrary && currentLibrary.dir);
  ls.innerHTML = (state.libraries || []).slice().reverse().map((l) => `<option value="${esc(l.dir)}">${esc(l.name)}</option>`).join("") || `<option value="">none yet</option>`;
  if (keepL && [...ls.options].some((o) => o.value === keepL)) ls.value = keepL;
  const live = state.jobs.filter(jobLive);
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
  if ($("prep-folder").value.trim() !== f) return;  // another folder was chosen meanwhile: its own look answers
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
  const body = { folder: $("prep-folder").value.trim(), out: $("prep-out").value.trim(), name: $("prep-name").value.trim(), dense: $("prep-dense").checked, workers: $("prep-workers").value || null,
    frame: $("prep-frame").value, reference: $("prep-ref").value.trim() };
  if (!body.folder) { $("prep-msg").innerHTML = `<span class="bad">Choose the footage folder first.</span>`; return; }
  $("prep-run").disabled = true;
  const r = await api("/api/prep", body);
  $("prep-run").disabled = false;
  if (r.error) { $("prep-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  $("prep-msg").textContent = "";
  $("prep-folder").value = ""; $("prep-name").value = ""; $("prep-ref").value = ""; $("prep-folder-info").textContent = FOLDER_HINT; updateDest();
  currentDir = r.project.dir;
  store.set("videtor.project", currentDir);
  if (r.job) runPanel("prep", r.job.id, PANELS.prep(r.project.dir));
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
  const asked = currentDir;
  const p = await api("/api/project?dir=" + encodeURIComponent(asked));
  if (asked !== currentDir) return;  // another project was opened while this one was on its way: its answer is not shown over it
  if (p.error) { $("project-msg").textContent = p.error; return; }
  if (shownDir !== p.dir) enterProject(p.dir);
  shownDir = p.dir;
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
  paintRuns();
}
/* the two cards of a project: reading the footage (and turning a camera) on Footage, the cut on Cut */
const PANELS = {
  prep: (dir) => ({ project: dir, status: "overview-status", log: "overview-log", logbox: "overview-logbox", what: "Reading the footage", failed: "The footage was not read.", onEnd: () => loadProject() }),
  cut: (dir) => ({ project: dir, status: "cut-status", log: "cut-log", logbox: "cut-logbox", what: "Cutting", failed: "The cut was not made.",
    older: () => (current && current.dir === dir && current.picks ? "Review still shows the cut before." : ""), onDone: () => showTab("look"), onEnd: () => loadProject() }),
};
function showJobs(p) {
  // the cards show this project's running job, else its run that failed (or the server's last failed job), else nothing
  const live = p.job, last = p.last_job;
  for (const kind of ["prep", "cut"]) {
    const o = PANELS[kind](p.dir), r = runs[runKey(kind, p.dir)];
    if (live && live.kind === kind) { runPanel(kind, live.id, o); continue; }
    if (r && r.state === "running") continue;
    const failed = r && r.state === "failed" && r.last ? r.last : last && last.kind === kind && !jobLive(last) && !jobOk(last) ? last : null;
    if (failed) { renderStatus($(o.status), failed); $(o.logbox).hidden = false; }
    else { $(o.status).hidden = true; $(o.logbox).hidden = true; }
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
    if (r.job) runPanel("prep", r.job.id, PANELS.prep(p.dir));
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
  const r = p.requirements || {}, mine = r.mine || {}, says = r.says || {}, mu = mine.music;
  const frame = says.frame ? ` The cut is ${says.frame}: write the plan for ${says.size[0]}x${says.size[1]}.` : "";
  const music = !mu ? "" : mu.none ? " No music: give every shot a length in seconds." :
    ` Use my music: ${mu.path} (${mu.start === "at" ? `starting at ${mu.in_s} s` : mu.start === "beginning" ? "from its beginning" : "choose the in-point so its lift lands on the reveal"}); count the plan on its beats.`;
  const ref = mine.reference ? ` I gave a reference video to cut like: run .venv/bin/videtor reference "${p.dir}" (it writes a first plan and reference.md from that video), then revise that plan against the brief instead of starting from nothing.` : "";
  return `/cut-from-brief The footage of this project is already read. Project folder: ${p.dir} (shot-map key "${p.key}"). ` +
    `The brief is in ${p.dir}/brief.md.${frame}${music}${ref} Read the overview there, write ${p.dir}/shotmap.json from the brief (name the shot types it draws on in "classes"), ` +
    `check it with the validator, and stop before cutting: I cut from the Videtor app.`;
}
/* ---- shape and music: what the user asks for; the cut follows it whatever the plan says ---- */
function renderRequirements(p) {
  const r = p.requirements || { mine: {}, frames: [], notes: [] }, mine = r.mine || {}, plan = r.plan;
  $("req-card").hidden = p.kind !== "cut";
  $("req-other").hidden = p.kind === "cut";
  $("req-other").textContent = p.kind === "cut" ? "" : `A ${p.kind} sets its frame and its music in its own plan.`;
  $("ref-block").hidden = p.kind !== "cut";
  if (p.kind !== "cut") { $("cut-as").innerHTML = ""; $("music-chosen").innerHTML = ""; return; }
  if (document.activeElement !== $("ref-video")) $("ref-video").value = mine.reference || "";
  const fromRef = plan && plan.reference, hasPlan = !!(p.shotmap || "").trim();
  $("ref-note").textContent = fromRef ? `The plan below was written from ${plan.reference}: as many shots, each as long as the video's. With your own track above, the shots are counted on its beat.`
    : hasPlan ? "Writing the plan from a video replaces the plan below (the one there now is kept aside in the project's work folder)."
    : "Videtor copies the video's rhythm from your footage: as many shots, each as long, the same kind of shot in each place. What it cannot copy: words on screen, the voice, the grade.";
  $("ref-run").textContent = fromRef ? "Write the plan again" : "Write the plan from this video";
  const sel = $("req-frame");
  sel.innerHTML = (r.frames || []).map(([k, label]) => `<option value="${esc(k)}">${esc(label)}</option>`).join("");
  sel.value = mine.frame || (plan && plan.frame) || "vertical";
  $("req-frame-note").textContent = mine.frame && plan && plan.frame !== mine.frame ? `The plan was written for ${plan.width}×${plan.height}; the cut is made in this shape.` : "";
  const mu = mine.music, mode = mu && mu.none ? "none" : mu && mu.path ? "mine" : "plan";
  document.querySelectorAll('input[name="req-music"]').forEach((x) => { x.checked = x.value === mode; });
  $("req-mine").hidden = mode !== "mine";
  if (document.activeElement !== $("req-track")) $("req-track").value = mu && mu.path ? mu.path : "";
  $("req-start").value = (mu && mu.start) || "auto";
  $("req-in").hidden = $("req-start").value !== "at";
  if (document.activeElement !== $("req-in")) $("req-in").value = mu && mu.in_s != null ? mu.in_s : "";
  const planMusic = plan && plan.music ? `the plan's music: ${plan.music}${plan.in_s != null ? ` from ${plan.in_s} s` : ""}` : plan ? "the plan has no music" : "the plan will say";
  const lines = (r.notes || []).map((n) => `<div>${esc(n.charAt(0).toUpperCase() + n.slice(1))}.</div>`).join("");
  $("req-msg").innerHTML = lines || (mode === "plan" ? `<div>Music is left to the plan (${esc(planMusic)}).</div>` : mode === "none" ? `<div>The cut will have no music.</div>` : "");
  const c = r.cut, says = r.says || {};
  $("cut-as").innerHTML = !(p.shotmap || "").trim() ? "" :
    `This cut: <b>${esc(says.frame || (plan ? `${plan.width}×${plan.height}` : ""))}</b> · music: <b>${esc(c ? (c.music ? `${c.music} from ${Number(c.in_s).toFixed(1)} s` : "none") : mu && mu.path ? says.music : plan && plan.music ? plan.music : "none")}</b>` +
    `${c && c.length_s ? ` · ${Number(c.length_s).toFixed(1)} s long` : ""} <a href="#" data-goto="project:brief">change</a>`;
  $("music-chosen").innerHTML = mode === "mine" ? `Music for this project: <b>${esc(says.music)}</b>.` : mode === "none" ? "This project is set to no music." : `Music for this project is left to the plan (${esc(planMusic)}).`;
}
async function saveRequirements(body, btn, errEl) {
  if (!current) return false;
  if (btn) btn.disabled = true;
  $("req-msg").innerHTML = body.music && body.music.path ? "Reading the track…" : "";
  const dir = current.dir;
  const r = await api("/api/settings", { project: dir, ...body });
  if (btn) btn.disabled = false;
  if (!current || current.dir !== dir) return !r.error;  // saved for a project no longer open: nothing of it is shown over this one
  if (r.error) { (errEl || $("req-msg")).innerHTML = `<span class="bad">${esc(r.error)}</span>`; if (errEl) $("req-msg").innerHTML = ""; return false; }
  if (errEl) errEl.innerHTML = "";
  current.requirements = r.requirements;
  renderRequirements(current);
  renderPlan(current);
  return true;
}
$("req-frame").onchange = () => saveRequirements({ frame: $("req-frame").value });
$("ref-run").onclick = async () => {
  if (!current) return;
  const dir = current.dir, video = $("ref-video").value.trim();
  if (!video) { refuseRun("reference", "Choose the video to cut like first: a finished video, or a link to one.", dir); return; }
  const r = await api("/api/reference", { project: dir, video });
  if (r.error) { refuseRun("reference", r.error, dir); return; }
  runPanel("reference", r.job.id, { project: dir, what: "Watching the video and writing the plan (about a minute)", failed: "The plan was not written.",
    done: "The plan is written from the video: check the shot list below, then go to Cut.", onEnd: () => loadProject() });
};
document.querySelectorAll('input[name="req-music"]').forEach((x) => x.addEventListener("change", () => {
  $("req-mine").hidden = x.value !== "mine";
  if (x.value === "none") saveRequirements({ music: { none: true } });
  if (x.value === "plan") saveRequirements({ music: null });
  if (x.value === "mine") $("req-msg").innerHTML = "Choose your track, then <b>Use this music</b>.";
}));
const useTrack = (quiet) => {
  const path = $("req-track").value.trim(), start = $("req-start").value;
  if (!path) { if (!quiet) $("req-msg").innerHTML = `<span class="bad">Choose your track first.</span>`; return; }
  if (start === "at" && $("req-in").value === "") { $("req-msg").innerHTML = "Say the second of the track the cut starts on, then <b>Use this music</b>."; return; }
  saveRequirements({ music: { path, start, in_s: $("req-in").value === "" ? null : Number($("req-in").value) } }, $("req-save"));
};
$("req-save").onclick = () => useTrack(false);
$("req-track").addEventListener("change", () => useTrack(true));  // a track chosen with the file dialog is used at once
$("req-start").onchange = () => { $("req-in").hidden = $("req-start").value !== "at"; useTrack(true); };
$("req-in").addEventListener("change", () => useTrack(true));
function renderBrief(p) {
  renderRequirements(p);
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
  if (r.error) { refuseRun("music", r.error); return; }
  runPanel("music", r.job.id, { log: "music-log", showLog: true, what: "Ranking the tracks", failed: "No tracks were ranked.",
    older: () => ($("music-results").innerHTML.trim() ? "The tracks listed are from before." : ""), onDone: () => showTracks() });
};
/* the ranked tracks as rows to choose from: Use this track sets the project's music */
async function showTracks() {
  const fresh = ticket("tracks");
  const r = await api("/api/music/ranked", { folder: $("music-folder").value.trim(), length: $("music-length").value, reveal_at: $("music-reveal").value || null });
  if (!fresh()) return;
  const el = $("music-results");
  if (r.error || !(r.tracks || []).length) { el.innerHTML = r.error ? `<span class="bad">${esc(r.error)}</span>` : `<div class="muted">No track in that folder suits a cut of this length.</div>`; return; }
  $("music-log").hidden = true;
  el.innerHTML = `<div id="music-msg"></div><div class="tablewrap"><table class="plan"><thead><tr><th>#</th><th>Track</th><th>Tempo</th><th>Start</th><th></th></tr></thead><tbody>` +
    r.tracks.map((t, i) => `<tr><td>${i + 1}</td><td>${esc(t.name)}</td><td>${t.bpm} BPM</td><td>${Number(t.in_s).toFixed(1)} s${t.drop_s != null ? ` <span class="muted">(lift at ${Number(t.drop_s).toFixed(1)} s)</span>` : ""}</td>` +
      `<td><button class="btn" data-track="${i}">Use this track</button></td></tr>`).join("") + `</tbody></table></div>`;
  el.querySelectorAll("button[data-track]").forEach((b) => b.addEventListener("click", async () => {
    const t = r.tracks[Number(b.dataset.track)];
    if (await saveRequirements({ music: { path: t.path, start: "at", in_s: t.in_s } }, b, $("music-msg"))) { el.querySelectorAll("button[data-track]").forEach((x) => { x.textContent = "Use this track"; }); b.textContent = "In use ✓"; }
  }));
}
$("search-run").onclick = async () => {
  if (!current) return;
  const r = await api("/api/search", { project: current.dir, query: $("search-query").value.trim(), top: 12 });
  if (r.error) { refuseRun("search", r.error, current.dir); return; }
  runPanel("search", r.job.id, { project: current.dir, log: "search-log", showLog: true, what: "Searching the footage", failed: "The search did not run." });
};

/* ---- cut ---- */
/* whether a cut can start is the server's word (`can.cut`, project.py): live or not, and why not in words. A server that
   does not send it gets today's reading of the plan, its problems and the drive. */
function renderCutTab(p) {
  const d = p.drive || {}, n = p.cut_needs, can = p.can && p.can.cut;
  const hasPlan = !!(p.shotmap || "").trim(), bad = (p.errors || []).length > 0;
  const blocked = !!(n && n.to_decode && !d.connected);
  const toBrief = ` <a href="#" data-goto="project:brief">Brief &amp; plan</a>`;
  let line = "";
  if (can && !can.ok) line = `<span class="warn">${esc(can.why || "Not ready to cut.")}</span>` + (["brief", "plan"].includes(p.stage) ? toBrief : "");
  else if (!can && !hasPlan) line = `<span class="warn">No plan yet.</span> Write it on <a href="#" data-goto="project:brief">Brief &amp; plan</a> first.`;
  else if (!can && bad) line = `<span class="warn">The plan has ${p.errors.length} problem(s).</span> Fix them on <a href="#" data-goto="project:brief">Brief &amp; plan</a>.`;
  else if (!can && blocked) line = `<span class="bad">Plug in ${esc(d.name || "the footage drive")}.</span> ${n.to_decode} of the ${n.takes} takes the plan can use still need reading from it.`;
  else if (n && n.to_decode) line = `<span class="ok">${esc(d.name || "The footage")} is connected.</span> This cut reads ${n.to_decode} takes in full first (a few minutes); later cuts are quick.`;
  else if (n) line = `<span class="ok">Everything the plan can use is read: ${n.takes} takes to choose from.</span>` + (d.connected ? "" : " The drive can stay unplugged for the cut; Premiere needs it to play the clips.");
  $("cut-drive").innerHTML = line;
  const running = !!(p.job && p.job.kind === "cut") || runRunning("cut");
  $("cut-run").disabled = (can ? !can.ok : !hasPlan || bad || blocked) || running;
  $("cut-run").textContent = p.picks ? "Cut again" : "Cut";
}
$("cut-run").onclick = async () => {
  if (!current) return;
  const dir = current.dir;
  $("cut-run").disabled = true;
  $("cut-msg").textContent = "";
  const r = await api("/api/cut", { project: dir, sfx: $("cut-sfx").checked });
  if (r.error) { refuseRun("cut", r.error, dir); $("cut-run").disabled = false; return; }
  runPanel("cut", r.job.id, PANELS.cut(dir));
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
  loadTimeline(true);
}
/* ---- watch the cut without Premiere ---- */
function renderPreview(p) {
  const el = $("preview");
  if (!p.picks) { el.innerHTML = ""; return; }
  el.innerHTML = (p.preview ? `<video class="preview" data-run-stale="preview" controls preload="metadata" src="${fileUrl(p.preview)}&t=${Date.now()}"></video>` : "") +
    `<div class="row"><button class="btn" id="preview-make" data-run-btn="preview">${p.preview ? "Make the preview again" : "Watch the cut here"}</button>
     <span class="muted">A small video of the cut with its music, made from the footage (the drive must be connected; about a minute). No effects or sound effects in it.</span></div>
     <div class="runmsg" data-run="preview"></div>`;
  $("preview-make").onclick = async () => {
    const r = await api("/api/preview", { project: p.dir });
    if (r.error) { refuseRun("preview", r.error, p.dir); return; }
    runPanel("preview", r.job.id, { project: p.dir, status: "preview-status", what: "Making the preview", failed: "No preview was made.",
      older: p.preview ? "The video shown is the one made before." : "", onEnd: () => loadProject() });
  };
}
/* ---- when the job is finished: a verdict, and the finished video read back ---- */
function renderTeach(p) {
  const el = $("teach");
  if (!p.picks) { el.innerHTML = ""; return; }
  const f = p.feedback || {}, v = f.verdict, s = f.summary;
  const said = v ? `<span class="chip">You said: ${v.good ? "good" : "not good"}${v.note ? " · " + esc(v.note) : ""}</span>` : "";
  const read = s ? `<div class="muted" data-run-stale="readback">Last read: ${s.kept} kept, ${s.slipped + s.trimmed} moved, ${s.swapped_to_alternate + s.swapped} swapped, ${s.removed} removed of ${s.slots} shots.</div>` : "";
  el.innerHTML = `<div class="exportcard"><div><b>When you have finished it</b></div>
    <div class="row"><span>Was this cut a good start?</span><button class="btn" id="verdict-good">Good</button><button class="btn" id="verdict-not">Not good</button>
      <input type="text" id="verdict-note" placeholder="why, in a line (optional)" style="min-width:280px">${said}</div>
    <div class="row"><span>Your finished video:</span><span class="muted" id="final-path">${esc(f.final ? f.final.split("/").pop() : "none in the project's folder yet")}</span>
      <button class="btn" id="final-choose">Choose…</button><button class="btn primary" id="final-read" data-run-btn="readback">Read my finished video</button></div>
    <div class="runmsg" data-run="readback"></div>
    ${read}
    <div class="row"><button class="btn" id="learn-run" data-run-btn="learn">Learn from my feedback</button>
      <span class="muted">${f.choices || 0} of your swaps kept so far. Learning is tried on your past cuts first and kept only if they get no worse.</span></div>
    <div class="runmsg" data-run="learn"></div></div>`;
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
    if (r.error) { refuseRun("readback", r.error, p.dir); return; }
    runPanel("readback", r.job.id, { project: p.dir, log: "teach-log", showLog: true, what: "Reading your finished video", failed: "Your finished video was not read.",
      older: s ? "The numbers below are from the last read." : "", done: "Read: the numbers below are from this read.", onEnd: () => loadProject() });
  };
  $("learn-run").onclick = async () => {
    const r = await api("/api/learn", {});
    if (r.error) { refuseRun("learn", r.error); return; }
    runPanel("learn", r.job.id, { log: "teach-log", showLog: true, what: "Learning from your feedback", failed: "Nothing was learnt.",
      done: "Done. What was learnt, and whether it was kept, is on Teach Videtor.", onEnd: () => loadProject() });
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
      if (r.error) { c.classList.remove("busy"); refuseRun("swap", r.error, p.dir); return; }
      $("look-note").value = "";
      runPanel("swap", r.job.id, { project: p.dir, log: "look-log", what: "Making the swap", failed: "The swap was not made.",
        older: "The list shows the cut as it was.", olderFailed: "The cut is as it was.", onEnd: () => loadProject() });
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
  runPanel("lib-build", r.job.id, LIB_RUN["lib-build"]);
  await refreshState();
  $("lib-select").value = r.library.dir;
  loadLibrary();
};
/* sorting a shoot and learning from the user's sorting: one card, a line beside each button, the list dimmed meanwhile */
const LIB_RUN = {
  "lib-build": { status: "lib-status-card", log: "lib-log", logbox: "lib-logbox", what: "Sorting the footage", failed: "The footage was not sorted.", onEnd: () => loadLibrary() },
  "lib-learn": { status: "lib-status-card", log: "lib-log", logbox: "lib-logbox", what: "Learning from your sorting", failed: "Nothing was learnt.",
    older: "The list below is as it was before.", done: "Learnt: new sorting follows yours.", onEnd: () => loadLibrary() },
};
$("lib-select").onchange = loadLibrary;
$("lib-learn").onclick = async () => {
  const dir = $("lib-select").value; if (!dir) return;
  const r = await api("/api/library/learn", { out: dir });
  if (r.error) { refuseRun("lib-learn:" + dir, r.error); return; }
  runPanel("lib-learn:" + dir, r.job.id, LIB_RUN["lib-learn"]);
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
  const fresh = ticket("library");
  const live = (state.jobs || []).filter((j) => j.project === dir && (j.kind === "library" || j.kind === "learn") && jobLive(j)).pop();
  // learning is the chosen library's: its line, its button and its list follow the choice
  $("lib-learn").dataset.runBtn = $("lib-learn-run").dataset.run = $("lib-view-wrap").dataset.runStale = "lib-learn:" + dir;
  if (live && live.kind === "learn") runPanel("lib-learn:" + dir, live.id, LIB_RUN["lib-learn"]);  // started before the page was opened
  else if (live) runPanel("lib-build", live.id, LIB_RUN["lib-build"]);
  const lib = await api("/api/library?dir=" + encodeURIComponent(dir));
  if (!fresh() || $("lib-select").value !== dir) return;  // another library was chosen, or asked for again, meanwhile
  paintRuns();
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
    if ($("lib-select").value !== dir) return;
    $("lib-status").textContent = r.error ? r.error : `${sel.dataset.name}: ${r.from} → ${r.to}. Learn from my sorting when you are done.`;
  }));
}

/* ---- promos: the best half minute of a finished video ---- */
let promoSets = null;
const PR_KEEP = ["pr-frame", "pr-kind", "pr-count", "pr-length", "pr-lead", "pr-fit"];
const prShape = (frame) => ((promoSets.frames.find(([k]) => k === frame) || [0, frame])[1] || "").split(" (")[0];
const prKind = (kind, n) => (kind === "edit" ? "Edit" : kind === "clip" ? "Clip" : "Promo") + (n === 1 ? "" : "s");
const prMade = (x) => { const kinds = [...new Set(x.promos.map((p) => p.kind))]; return kinds.length === 1 ? kinds[0] : "mixed"; };  // what the set holds, whatever was asked for
/* a run of the job (runPanel): what was there before is dimmed while it runs; when it ends the page shows the set it made,
   or says plainly that nothing was made (the promos still listed are older ones, not this run's) */
function prWatch(id, what) {
  $("pr-msg").textContent = "";
  runPanel("promos", id, { status: "pr-status", log: "pr-log", logbox: "pr-logbox", what: what || "Making the promos",
    older: () => (promoSets && promoSets.sets.length ? "What is shown below was made before." : ""),
    failed: "No promos were made this time.", olderFailed: () => (promoSets && promoSets.sets.length ? "The promos below are from an earlier run." : ""),
    onDone: (job) => loadPromos((job.result && job.result.promos ? job.result.promos.replace(/\/promos\.json$/, "") : null) || true),
    onFail: () => loadPromos() });
}
/* `show`: a set's folder to open, true for the newest, nothing to stay on the one chosen */
async function loadPromos(show) {
  const fresh = ticket("promos");
  const r = await api("/api/promos");
  if (!fresh()) return;  // a newer answer is already shown
  if (r.error) { $("pr-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  const first = promoSets === null;
  promoSets = r;
  if (first) {
    $("pr-frame").innerHTML = r.frames.map(([k, label]) => `<option value="${esc(k)}">${esc(label)}</option>`).join("");
    PR_KEEP.forEach((id) => { const v = store.get("videtor." + id); if (v && [...$(id).options].some((o) => o.value === v)) $(id).value = v; });
    const live = (state.jobs || []).filter((j) => j.kind === "promos" && jobLive(j)).pop();
    if (live) prWatch(live.id);
  }
  $("pr-fetcher").textContent = r.fetcher ? `yt-dlp ${r.fetcher} is installed. When a link that used to work stops, update it.` : "The link fetcher (yt-dlp) is not installed: links will not work, files will.";
  const sel = $("pr-select"), keep = show ? null : sel.value;
  const when = (x) => (x.made_at || "").replace("T", " ").slice(5, 16);
  sel.innerHTML = r.sets.map((x) => `<option value="${esc(x.dir)}">${esc(x.title)} · ${x.promos.length} ${prKind(prMade(x), x.promos.length).toLowerCase()} · ${esc(prShape(x.options.frame))} · ${esc(when(x))}</option>`).join("") || `<option value="">none yet</option>`;
  const want = typeof show === "string" ? show : keep;
  if (want && r.sets.some((x) => x.dir === want)) sel.value = want;
  renderPromos();
}
function renderPromos() {
  const x = (promoSets.sets || []).find((q) => q.dir === $("pr-select").value);
  $("pr-reveal").hidden = $("pr-again").hidden = !x;
  if (!x) { $("pr-view").innerHTML = `<p class="muted">Nothing yet: give a video above.</p>`; return; }
  const o = x.options, shape = (promoSets.frames.find(([k]) => k === o.frame) || [0, o.frame])[1];
  const wide = ["horizontal", "horizontal-4k", "square"].includes(o.frame);
  const led = x.lead === "speech" ? "the talking" : "the music and the picture";
  const others = promoSets.sets.filter((q) => q.video === x.video && q.dir !== x.dir).length;
  $("pr-view").innerHTML =
    `<p class="promo-head"><b>${esc(x.title)}</b> · ${fmtDur(x.source.duration_s)}${x.source.url ? ` · <a href="${esc(x.source.url)}" target="_blank" rel="noopener">the link</a>` : ""}<br>` +
    `<b>${prKind(prMade(x), 2)}</b>${prMade(x) !== o.kind ? ` (${prKind(o.kind, 2).toLowerCase()} were asked for: see the note below)` : ""} in ${esc(shape)}, made ${esc((x.made_at || "").replace("T", " ").slice(0, 16))}` +
    (others ? `. This video has ${others} other set${others > 1 ? "s" : ""} in the list above.` : ".") + `<br>` +
    `Videtor took this as a video carried by ${led} (words during ${Math.round(100 * (x.said || 0))}% of it)` +
    `${x.replayed ? ", and had YouTube's most-replayed parts to go by" : ""}. <span class="muted">Not so? Set “What carries the video” under Advanced and make them again.</span>` + (x.notes || []).map((n) => `<br><span class="warn">${esc(n)}</span>`).join("") + `</p>` +
    `<div class="promos">` + x.promos.map((p) =>
      `<div class="promo ${wide ? "wide" : ""}"><video controls preload="metadata" playsinline src="${fileUrl(p.file)}&v=${encodeURIComponent(p.made_at || "")}"></video>` +
      `<h3>${prKind(p.kind, 1)} ${p.n} · ${Number(p.length_s).toFixed(1)} s</h3><div class="muted">${p.kind === "edit" ? `${p.shots} pieces from across the video; the sound from` : "one stretch, from"} ${esc(p.at)} of the video</div>` +
      `<ul>${(p.reasons || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` + (p.text ? `<blockquote>${esc(p.text)}</blockquote>` : "") +
      `<div class="row"><button class="btn small" data-another="${p.n}" ${x.more > 0 ? "" : "disabled"} title="${x.more > 0 ? `${x.more} more to try` : "no other part as good: change the kind or the length"}">Try another</button>` +
      `<button class="btn small" data-show="${esc(p.file)}">Show the file</button><button class="btn small" data-show="${esc(p.xml)}">Premiere XML</button></div></div>`).join("") + `</div>`;
  $("pr-view").querySelectorAll("button[data-show]").forEach((b) => b.addEventListener("click", () => api("/api/reveal", { path: b.dataset.show })));
  $("pr-view").querySelectorAll("button[data-another]").forEach((b) => b.addEventListener("click", async () => {
    const r = await api("/api/promos/another", { dir: x.dir, n: Number(b.dataset.another) });
    if (r.error) { $("pr-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
    prWatch(r.job.id, `Finding another for ${prKind(x.options.kind, 1).toLowerCase()} ${b.dataset.another}`);
  }));
}
$("pr-select").onchange = renderPromos;
$("pr-reveal").onclick = () => { const d = $("pr-select").value; if (d) api("/api/reveal", { path: d + "/promos.json" }); };
$("pr-again").onclick = () => {
  const x = (promoSets.sets || []).find((q) => q.dir === $("pr-select").value);
  if (x) { $("pr-source").value = x.source.url || x.source.path; $("pr-source").focus(); window.scrollTo(0, 0); }
};
$("pr-run").onclick = async () => {
  PR_KEEP.forEach((id) => store.set("videtor." + id, $(id).value));
  $("pr-msg").textContent = "";
  const r = await api("/api/promos/make", { source: $("pr-source").value.trim(), frame: $("pr-frame").value, kind: $("pr-kind").value, count: Number($("pr-count").value),
    length: Number($("pr-length").value), lead: $("pr-lead").value, fit: $("pr-fit").value, browser: $("pr-browser").value });
  if (r.error) { $("pr-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  prWatch(r.job.id, `Making ${$("pr-count").value} ${prKind($("pr-kind").value, Number($("pr-count").value)).toLowerCase()}`);
};
$("pr-update").onclick = async () => {
  const r = await api("/api/promos/update", {});
  if (r.error) { $("pr-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  prWatch(r.job.id, "Updating the link fetcher");
};

/* ---- activity ---- */
async function renderJobs() {
  const fresh = ticket("jobs");
  const r = await api("/api/jobs");
  if (!fresh()) return;
  const tb = $("jobs-table").querySelector("tbody");
  tb.innerHTML = r.jobs.slice().reverse().map((j) => {
    const p = j.progress || {};
    const pct = j.status === "running" && p.total ? Math.round(100 * (p.done || 0) / p.total) : null;
    const left = j.status === "running" && p.eta_s ? ` · about ${fmtDur(p.eta_s)} left` : "";
    const why = !jobLive(j) && !jobOk(j) && j.status !== "cancelled" ? `<div class="small">${esc(jobWhy(j))}</div>` : "";
    return `<tr><td><b>${esc(j.title)}</b></td><td class="st-${esc(j.status)}">${esc(stageText(j))}${pct !== null ? ` (${pct}%)` : ""}<span class="muted">${left}</span>${why}</td><td>${fmtDur(j.elapsed)}</td>` +
      `<td class="acts"><button class="btn small" data-show="${j.id}">Details</button>` +
      (j.status === "running" ? `<button class="btn small" data-act="pause" data-id="${j.id}">Pause</button>` : "") +
      (j.status === "paused" && !j.paused_by ? `<button class="btn small" data-act="resume" data-id="${j.id}">Resume</button>` : "") +
      (jobLive(j) ? `<button class="btn small danger" data-act="cancel" data-id="${j.id}">Stop</button>` : "") + `</td></tr>`;
  }).join("") || `<tr><td colspan="4" class="muted">Nothing yet.</td></tr>`;
  tb.querySelectorAll("button[data-show]").forEach((b) => b.addEventListener("click", () => { $("jobs-logbox").hidden = false; $("jobs-logbox").open = true; watch(b.dataset.show, { log: "jobs-log" }); }));
  tb.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => { await api(`/api/jobs/${b.dataset.id}/${b.dataset.act}`, {}); renderJobs(); }));
}

/* ---- a camera left sideways ---- */
$("orient-run").onclick = async () => {
  if (!current) return;
  const r = await api("/api/orient", { project: current.dir, which: $("orient-which").value.trim(), rotate: Number($("orient-turn").value) });
  if (r.error) { refuseRun("orient", r.error, current.dir); return; }
  runPanel("orient", r.job.id, { ...PANELS.prep(current.dir), what: "Turning the takes", failed: "The takes were not turned.", done: "Turned." });
};

/* ---- one file: a finished video or a Premiere XML ---- */
document.querySelectorAll("button[data-pick]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try {
    const r = await api("/api/pick-file", { kind: b.dataset.kind, start: state.home });
    if (r.path) { $(b.dataset.pick).value = r.path; $(b.dataset.pick).dispatchEvent(new Event("change")); }
  } finally { b.disabled = false; }
}));

/* ---- Teach Videtor: everything it learns from is given here ---- */
let teach = null;
/* every job of this page shares its card and its log; each control has its own line (data-run=name) beside it */
const watchTeach = (name, id, what) => runPanel(name, id, { status: "teach-status", log: "teach-job-log", logbox: "teach-logbox", what: what || "Learning",
  failed: "Nothing was learnt this time.", olderFailed: "What Videtor knows is as it was.", onEnd: () => loadTeach() });
async function teachPost(path, body, name, what) {
  $("teach-msg").innerHTML = "";
  const r = await api(path, body);
  if (r.error) refuseRun(name, r.error);
  if (r.job) watchTeach(name, r.job.id, what);
  return r;
}
async function loadTeach() {
  const fresh = ticket("teach");
  const t = await api("/api/teach");
  if (!fresh()) return;  // a newer answer is already shown
  if (t.error) { $("teach-msg").innerHTML = `<span class="bad">${esc(t.error)}</span>`; return; }
  teach = t;
  $("kinds").innerHTML = teach.kinds.map((k) => `<option value="${esc(k)}">`).join("");
  renderTeachJobs();
  renderTeachEdits();
  renderRules();
  renderKnown();
  // a job started elsewhere (or before the page was opened) shows on the card, unless one of this page's lines already follows it
  if (teach.active && !Object.values(runs).some((r) => r.job === teach.active.id && r.o.status === "teach-status")) watchTeach("teach", teach.active.id, teach.active.title);
  paintRuns();
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
    return `<div class="teachrow" data-dir="${esc(j.dir)}"><div class="who">${esc(j.name)}</div><div class="what">${what}${v && v.note ? `<div class="muted small">You said: ${esc(v.note)}</div>` : ""}<div class="runmsg" data-run="teach-row:${esc(j.dir)}"></div></div>
      <span class="acts">${j.unread ? `<button class="btn primary" data-act="read" data-run-btn="teach-row:${esc(j.dir)}">Read it</button>` : `<button class="btn" data-act="choose" data-run-btn="teach-row:${esc(j.dir)}">Choose the video…</button>`}
      <button class="btn" data-act="folder">Show folder</button></span>
      <span class="acts"><span class="muted">Was the cut a good start?</span>
      <button class="btn ${v && v.good ? "primary" : ""}" data-act="good">Good</button><button class="btn ${v && !v.good ? "primary" : ""}" data-act="not">Not good</button></span></div>`;
  }).join("");
  el.innerHTML = `<div class="card">` + (teach.pending ? `<div class="row"><button class="btn primary" id="teach-pending" data-run-btn="teach-pending">Read ${teach.pending} finished video${teach.pending > 1 ? "s" : ""} and learn</button>` +
    `<span class="runmsg" data-run="teach-pending"></span></div>` : `<div class="runmsg" data-run="teach-pending"></div>`) + rows + `</div>`;
  if ($("teach-pending")) $("teach-pending").onclick = () => teachPost("/api/teach/pending", {}, "teach-pending", "Reading the finished videos");
  el.querySelectorAll("button[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const dir = b.closest(".teachrow").dataset.dir, act = b.dataset.act, name = "teach-row:" + dir;
    if (act === "read") return teachPost("/api/readback", { project: dir }, name, "Reading the finished video");
    if (act === "folder") return api("/api/reveal", { path: dir + "/picks.json" });
    if (act === "choose") {
      const r = await api("/api/pick-file", { kind: "video", start: dir });
      if (r.path) teachPost("/api/readback", { project: dir, video: r.path }, name, "Reading the finished video");
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
    return `<div class="teachrow"><div class="who">${esc(e.name)}</div><div class="what"><span class="chip">${esc(e.bucket)}</span> ${what}<div class="runmsg" data-run="teach-edit:${esc(e.key)}"></div></div>
      ${!e.learnt && (e.connected || read) ? `<button class="btn ${e.tried ? "" : "primary"}" data-key="${esc(e.key)}" data-run-btn="teach-edit:${esc(e.key)}">${read ? (e.tried ? "Try again" : "Learn from it") : "Read its footage and learn"}</button>` : ""}</div>`;
  };
  const waiting = edits.filter((e) => !e.learnt).length;
  $("teach-edits").innerHTML = `<details class="advanced" ${waiting && edits.some((e) => !e.learnt && e.connected) ? "open" : ""}><summary>The ${edits.length} edits you gave before (${edits.length - waiting} learnt from, ${waiting} waiting for their footage)</summary><div class="card">${edits.map(row).join("")}</div></details>`;
  $("teach-edits").querySelectorAll("button[data-key]").forEach((b) => b.addEventListener("click", () => teachPost("/api/teach/edit", { key: b.dataset.key }, "teach-edit:" + b.dataset.key, "Learning from this edit")));
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
$("teach-learn").onclick = () => teachPost("/api/learn", {}, "teach-learn", "Learning again");
$("tp-run").onclick = async () => {
  const r = await teachPost("/api/teach/project", { name: $("tp-name").value, bucket: $("tp-bucket").value, video: $("tp-video").value, xml: $("tp-xml").value, footage: $("tp-footage").value },
    "teach-tp", "Learning from this job");
  if (!r.error) ["tp-name", "tp-video", "tp-xml", "tp-footage"].forEach((id) => { $(id).value = ""; });
};
$("ts-run").onclick = async () => {
  const r = await teachPost("/api/teach/style", { source: $("ts-source").value, bucket: $("ts-bucket").value }, "teach-ts", "Learning the style");
  if (!r.error) $("ts-source").value = "";
};

/* ---- the timeline: the cut played and changed in the app before it goes to Premiere ---- */
const TL = { data: null, dir: null, t: 0, playing: false, t0: 0, sel: null, zoom: 1, els: new Map(), audio: [], cur: null, options: null, auto: {} };
const tlFrames = (s) => Math.round(s * TL.data.fps);
const tlClock = (t) => { const f = Math.max(0, Math.round(t * TL.data.fps)), fps = Math.round(TL.data.fps); return `${Math.floor(f / fps / 60)}:${String(Math.floor(f / fps) % 60).padStart(2, "0")}:${String(f % fps).padStart(2, "0")}`; };
const tlClipAt = (t) => { const cs = TL.data.clips; for (let i = cs.length - 1; i >= 0; i--) if (t >= cs[i].start_s - 1e-6) return cs[i]; return cs[0]; };
const tlSlot = (id) => (TL.data.slots || []).find((s) => s.id === id);

async function loadTimeline(keep) {
  if (!current || !current.picks) { $("tl").hidden = true; tlStop(); return; }
  const asked = current.dir, fresh = ticket("timeline");
  const d = await api("/api/timeline?dir=" + encodeURIComponent(asked));
  if (!fresh() || !current || current.dir !== asked) return;  // another project was opened, or the timeline asked for again, meanwhile
  if (d.error) { $("tl").hidden = true; return; }
  const same = TL.dir === d.dir;
  TL.data = d; TL.dir = d.dir;
  if (!same) { tlStop(); TL.t = 0; TL.sel = null; TL.options = null; TL.auto = {}; }
  if (!keep && !same) TL.t = 0;
  TL.t = Math.min(TL.t, Math.max(0, d.length_s - 1 / d.fps));
  $("tl").hidden = false;
  tlBuild();
  if (d.job && ["preview", "search", "look"].includes(d.job.kind) && !Object.values(runs).some((r) => r.job === d.job.id)) tlWatch(d.job);
  paintRuns();
}
function tlBuild() {
  const d = TL.data, stage = $("tl-stage");
  const mon = stage.parentElement, h = 440;
  mon.style.width = Math.round(h * d.width / d.height) + "px";
  // the picture: one element a clip (its preview clip when there is one, else its still), reused across rebuilds
  const keepEls = new Map();
  d.clips.forEach((c, i) => {
    const key = `${c.slot}:${c.part}:${c.proxy ? c.proxy.path : c.thumb || c.kind}`;
    let el = TL.els.get(key);
    if (!el) {
      if (c.proxy) { el = document.createElement("video"); el.muted = !c.sound; el.playsInline = true; el.preload = "auto"; el.src = fileUrl(c.proxy.path); }
      else if (c.thumb) { el = document.createElement("img"); el.src = fileUrl(c.thumb); }
      else { el = document.createElement("div"); el.className = "tl-black"; }
      stage.appendChild(el);
    }
    keepEls.set(key, el);
    c.el = el; c.i = i;
  });
  TL.els.forEach((el, key) => { if (!keepEls.has(key)) el.remove(); });
  TL.els = keepEls;
  TL.cur = null;
  // the music: one player a piece
  const want = d.music.map((g, i) => (g.there ? `/media?dir=${encodeURIComponent(d.dir)}&i=${i}` : ""));
  if (TL.audio.length !== want.length || TL.audio.some((a, i) => a.dataset.src !== want[i])) {
    TL.audio.forEach((a) => { a.pause(); a.remove(); });
    TL.audio = want.map((src) => { const a = document.createElement("audio"); a.preload = "auto"; a.dataset.src = src; if (src) a.src = src; document.body.appendChild(a); return a; });
  }
  tlDraw();
  tlInspector();
  tlSync(true);
  const stills = d.clips.filter((c) => c.kind !== "black" && c.kind !== "empty" && !c.proxy).length;
  $("tl-load").hidden = !stills;
  $("tl-load").textContent = stills === d.clips.length || stills > 3 ? "Load the moving picture" : `Load ${stills} more clip${stills > 1 ? "s" : ""}`;
  $("tl-flag").textContent = stills ? (d.drive && d.drive.name && !d.drive.connected ? `Stills: plug in ${d.drive.name} for the moving picture` : "Stills") : "";
  $("tl-undo").disabled = !d.can_undo;
  $("tl-write").classList.toggle("primary", !!d.dirty || !d.has_xml);
  $("tl-write").textContent = d.dirty ? "Write the Premiere XML (changes not in it yet)" : "Write the Premiere XML";
  $("tl-total").textContent = ` / ${tlClock(d.length_s)} · ${d.slots.length} shots`;
  if (document.activeElement !== $("tl-overall")) $("tl-overall").value = d.overall || "";
}
function tlDraw() {
  const d = TL.data, W = Math.max(600, $("tl-wrap").clientWidth - 2) * TL.zoom, x = (t) => (t / d.length_s) * W;
  $("tl-inner").style.width = W + "px";
  $("tl-sections").innerHTML = d.sections.map((s) => `<span style="left:${x(s.start_s)}px;width:${x(s.end_s - s.start_s)}px">${esc(s.name)}</span>`).join("");
  const slots = Object.fromEntries(d.slots.map((s) => [s.id, s]));
  $("tl-clips").innerHTML = d.clips.map((c) => {
    const s = slots[c.slot] || {}, w = Math.max(2, x(c.length_s));
    const cls = ["tl-clip", c.kind === "black" || c.kind === "empty" ? "black" : "", c.slot === TL.sel ? "sel" : "", s.second_look ? "changed" : "", s.note ? "noted" : ""].join(" ");
    const bg = c.thumb ? `background-image:url('${fileUrl(c.thumb)}')` : "";
    return `<div class="${cls}" data-slot="${esc(c.slot)}" data-start="${c.start_s}" title="${esc(s.show || "")} · ${esc(c.name || c.kind)} · ${tlFrames(c.length_s)} frames" style="left:${x(c.start_s)}px;width:${w}px;${bg}">${w > 46 ? `<label>${esc((c.name || c.kind).replace(/\.[A-Za-z0-9]+$/, ""))}</label>` : ""}</div>`;
  }).join("");
  $("tl-music").innerHTML = d.music.map((g) => `<span style="left:${x(g.start_s) + 6}px">${esc(g.name)}${g.there ? "" : " (not found: no sound here)"}</span>`).join("") +
    d.beats.map((b) => `<i style="left:${x(b)}px"></i>`).join("");
  tlPlayhead();
}
function tlPlayhead() {
  const d = TL.data, W = parseFloat($("tl-inner").style.width) || 1;
  const px = (TL.t / d.length_s) * W;
  $("tl-playhead").style.left = px + "px";
  $("tl-time").textContent = tlClock(TL.t);
  if (TL.playing) { const wrap = $("tl-wrap"); if (px < wrap.scrollLeft || px > wrap.scrollLeft + wrap.clientWidth - 40) wrap.scrollLeft = Math.max(0, px - 80); }
}
/* show the frame for TL.t: the active clip's element, its video at the right moment, the music with it */
function tlSync(force) {
  const d = TL.data;
  if (!d) return;
  const c = tlClipAt(TL.t);
  if (c !== TL.cur || force) {
    d.clips.forEach((o) => { if (o.el && o !== c) { o.el.classList.remove("on"); if (o.el.tagName === "VIDEO" && !o.el.paused) o.el.pause(); } });
    if (c.el) c.el.classList.add("on");
    TL.cur = c;
  }
  if (c.el && c.el.tagName === "VIDEO") {
    const v = c.el, want = c.proxy.offset_s + (TL.t - c.start_s);
    if (TL.playing) {
      if (v.paused) { if (Math.abs(v.currentTime - want) > 0.04) v.currentTime = want; v.play().catch(() => {}); }
      else if (Math.abs(v.currentTime - want) > 0.2) v.currentTime = want;
    } else {
      if (!v.paused) v.pause();
      if (Math.abs(v.currentTime - want) > 0.015) v.currentTime = want;
    }
  }
  const nx = d.clips[c.i + 1];  // have the next clip ready at its first frame before the cut
  if (nx && nx.el && nx.el.tagName === "VIDEO" && nx.start_s - TL.t < 0.5 && nx.el.paused && Math.abs(nx.el.currentTime - nx.proxy.offset_s) > 0.03) nx.el.currentTime = nx.proxy.offset_s;
  d.music.forEach((g, i) => {
    const a = TL.audio[i];
    if (!a || !a.dataset.src) return;
    const on = TL.playing && TL.t >= g.start_s && TL.t < g.start_s + g.length_s, want = g.in_s + (TL.t - g.start_s);
    // The music is started where the playhead is and then left alone: every seek of a playing track is heard as a
    // break. It is the picture's clock that leans to the music (tlTick), not the other way round.
    if (on) {
      if (a.paused) { if (Math.abs(a.currentTime - want) > 0.03) a.currentTime = want; a._seen = null; a.play().catch(() => {}); }
      else if (TL.reseek) { a.currentTime = want; a._seen = null; }  // the playhead was moved while it plays
    } else if (!a.paused) a.pause();
  });
  TL.reseek = false;
  tlPlayhead();
}
/* with the playhead at rest, have the music waiting at that moment, so play starts at once */
function tlCue() {
  clearTimeout(TL.cue);
  TL.cue = setTimeout(() => {
    if (!TL.data || TL.playing) return;
    TL.data.music.forEach((g, i) => {
      const a = TL.audio[i];
      if (a && a.dataset.src && a.readyState > 0 && TL.t >= g.start_s && TL.t < g.start_s + g.length_s && Math.abs(a.currentTime - (g.in_s + TL.t - g.start_s)) > 0.03) a.currentTime = g.in_s + (TL.t - g.start_s);
    });
  }, 250);
}
function tlTick() {
  if (!TL.playing) return;
  const now = performance.now() / 1000;
  let t = now - TL.t0;
  // the music is the clock: whenever its player reports a new time, the picture's clock leans to it
  const k = TL.data.music.findIndex((g) => t >= g.start_s && t < g.start_s + g.length_s), a = k >= 0 ? TL.audio[k] : null;
  if (a && a.dataset.src && !a.paused && a.readyState > 2 && a.currentTime !== a._seen) {
    const g = TL.data.music[k], first = a._seen === null || a._seen === undefined;
    a._seen = a.currentTime;
    const err = g.start_s + (a.currentTime - g.in_s) - t;  // + : the music is ahead of the picture
    if (!first && Math.abs(err) < 0.6) { TL.t0 -= err * 0.2; t = now - TL.t0; }
    else if (Math.abs(err) >= 0.6 && now - (TL.aSeek || 0) > 1.5) { TL.aSeek = now; a.currentTime = g.in_s + (t - g.start_s); a._seen = null; }  // it did not start where asked: once
  }
  TL.t = t;
  if (TL.t >= TL.data.length_s - 0.5 / TL.data.fps) { TL.t = TL.data.length_s - 1 / TL.data.fps; tlStop(); return; }
  tlSync();
  requestAnimationFrame(tlTick);
}
function tlPlay() {
  if (!TL.data) return;
  if (TL.t >= TL.data.length_s - 2 / TL.data.fps) TL.t = 0;
  TL.playing = true; TL.t0 = performance.now() / 1000 - TL.t;
  $("tl-play").textContent = "Stop";
  tlSync(true);
  requestAnimationFrame(tlTick);
}
function tlStop() {
  TL.playing = false;
  if ($("tl-play")) $("tl-play").textContent = "Play";
  TL.audio.forEach((a) => a.pause());
  if (TL.data) { tlSync(true); tlCue(); }
}
function tlSeek(t) {
  const d = TL.data;
  TL.t = Math.max(0, Math.min(d.length_s - 1 / d.fps, Math.round(t * d.fps) / d.fps));
  if (TL.playing) { TL.t0 = performance.now() / 1000 - TL.t; TL.reseek = true; }
  tlSync();
  if (!TL.playing) tlCue();
}
function tlJump(dir) {
  const starts = [...new Set(TL.data.clips.map((c) => c.start_s))].sort((a, b) => a - b);
  const e = 0.5 / TL.data.fps;
  const to = dir > 0 ? starts.find((s) => s > TL.t + e) : [...starts].reverse().find((s) => s < TL.t - e);
  if (to !== undefined) tlSeek(to);
}
function tlSelect(id, seek) {
  TL.sel = id; TL.options = null;
  const s = tlSlot(id);
  if (seek && s) tlSeek(s.start_s);
  document.querySelectorAll("#tl-clips .tl-clip").forEach((el) => el.classList.toggle("sel", el.dataset.slot === id));
  tlInspector();
}
/* the selected shot: what it is, and everything that can be done to it */
function tlInspector() {
  const el = $("tl-inspector"), s = TL.sel && tlSlot(TL.sel);
  if (!s) { el.innerHTML = `<h3>No shot selected</h3><p class="muted">Click a shot on the timeline to see its runners-up, slip it, move its cut, look for more takes or remove it.</p>`; return; }
  const what = { flicker: "a flicker of several takes", ramp: "a speed ramp", byte: "someone speaking", stack: "a stack of shots", black: "black", empty: "empty" }[s.kind];
  const step = (op, n, label, title) => `<button class="btn" data-op="${op}" data-n="${n}" title="${title}">${label}</button>`;
  const alts = s.alternates.map((a) => `<div class="tl-alt" data-swap="${a.k}" title="${esc(a.name)} from ${Number(a.in_s).toFixed(1)} s${a.reasons.length ? " · " + esc(a.reasons.join("; ")) : ""}">${a.thumb ? `<img src="${fileUrl(a.thumb)}" loading="lazy">` : ""}<span>${esc(a.name)}</span></div>`).join("");
  const opts = TL.options && TL.options.slot === s.id ? (TL.options.rows.length ? `<div class="tl-alts">` + TL.options.rows.map((a) => `<div class="tl-alt" data-option="${a.i}" title="${esc(a.name)} from ${Number(a.in_s).toFixed(1)} s${a.used_in ? " · already in the cut (" + esc(a.used_in) + ")" : ""}${a.reasons.length ? " · " + esc(a.reasons.join("; ")) : ""}">${a.thumb ? `<img src="${fileUrl(a.thumb)}" loading="lazy">` : ""}<span>${a.used_in ? "↺ " : ""}${esc(a.name)}</span></div>`).join("") + `</div>` : `<span class="muted">Nothing else in the footage that has been read fits.</span>`) : "";
  el.innerHTML = `<h3>${esc(s.show || s.id)}</h3>
    <div class="muted small">${esc(s.section)} · ${s.name ? `${esc(s.name)} from ${Number(s.in_s).toFixed(2)} s` : esc(what || "")} · ${tlFrames(s.length_s)} frames${s.speed < 1 ? ` · slow motion ${Math.round(s.speed * 100)}%` : ""}${what && s.name ? ` · ${what}` : ""}${s.second_look ? " · changed" : ""}</div>
    ${s.can.slip ? `<div class="grp"><b>Slip: the same take, earlier or later (frames)</b>${step("slip", -5, "◀ 5", "five frames earlier in the take")}${step("slip", -1, "◀ 1", "one frame earlier")}${step("slip", 1, "1 ▶", "one frame later")}${step("slip", 5, "5 ▶", "five frames later")} <span class="muted small">${s.room.before_f} before, ${s.room.after_f} after</span></div>` : ""}
    ${s.can.roll ? `<div class="grp"><b>The cut after this shot (frames)</b>${step("roll", -2, "◀ 2", "cut two frames sooner")}${step("roll", -1, "◀ 1", "cut one frame sooner")}${step("roll", 1, "1 ▶", "cut one frame later")}${step("roll", 2, "2 ▶", "cut two frames later")}</div>` : ""}
    ${!s.can.slip && s.name ? `<div class="grp muted small">This shot is ${esc(what || "set by the plan")}: its timing is the plan's. It can still be swapped${s.can.replace ? " or replaced" : ""}.</div>` : ""}
    ${s.can.swap ? `<div class="grp"><b>Runners-up: click one to use it</b><div class="tl-alts">${alts}</div></div>` : ""}
    ${s.can.replace ? `<div class="grp"><b>More takes</b><div class="row" style="margin:0"><input type="text" id="tl-find" placeholder="${esc(s.show || "what the shot should show")}" style="flex:1;min-width:160px"><button class="btn" id="tl-find-run" data-run-btn="options:${esc(s.id)}">Find</button></div><div class="runmsg" data-run="options:${esc(s.id)}"></div><div data-run-stale="options:${esc(s.id)}">${opts}</div></div>` : ""}
    <div class="grp"><b>A note on this shot</b><input type="text" id="tl-note" value="${esc(s.note || "")}" placeholder="e.g. too long, wants a closer shot" style="width:100%"></div>
    <div class="grp">${s.can.remove ? `<button class="btn" data-op="remove">Remove this shot</button>` : ""}</div>
    ${s.reasons.length ? `<div class="grp muted small">Why it was picked: ${esc(s.reasons.join("; "))}</div>` : ""}`;
  el.querySelectorAll("button[data-op]").forEach((b) => b.addEventListener("click", () => tlEdit(b.dataset.op === "remove" ? { op: "remove", slot: s.id } : { op: b.dataset.op, slot: s.id, frames: Number(b.dataset.n) })));
  el.querySelectorAll("[data-swap]").forEach((a) => a.addEventListener("click", () => tlEdit({ op: "swap", slot: s.id, k: Number(a.dataset.swap) })));
  el.querySelectorAll("[data-option]").forEach((a) => a.addEventListener("click", () => tlEdit({ op: "replace", slot: s.id, option: Number(a.dataset.option) })));
  const note = $("tl-note");
  note.addEventListener("change", async () => { await api("/api/note", { project: TL.dir, slot: s.id, text: note.value }); s.note = note.value.trim(); tlDraw(); });
  if ($("tl-find-run")) {
    const find = async () => {
      const dir = TL.dir, name = "options:" + s.id;
      const r = await api("/api/options", { project: dir, slot: s.id, show: $("tl-find").value });
      if (r.error) { refuseRun(name, r.error, dir); return; }
      runPanel(name, r.job.id, { project: dir, status: "tl-status", hideDone: true, what: "Looking for more takes", failed: "No takes were found.",
        older: TL.options && TL.options.slot === s.id ? "The takes listed are from the last look." : "", onDone: async () => {
          const o = await api(`/api/options?dir=${encodeURIComponent(dir)}&slot=${encodeURIComponent(s.id)}`);
          if (TL.dir !== dir) return;  // another project is open now: its own shot is shown
          TL.options = o;
          tlInspector();
        } });
    };
    $("tl-find-run").onclick = find;
    $("tl-find").addEventListener("keydown", (e) => { if (e.key === "Enter" && !runRunning("options:" + s.id)) find(); });
  }
  paintRuns();
}
/* one edit: made at once on the server, the timeline redrawn from what comes back; missing preview clips are fetched */
async function tlEdit(op) {
  if (!TL.dir) return;
  const dir = TL.dir;
  const r = await api("/api/edit", { project: dir, ...op });
  if (TL.dir !== dir) return;  // another project's timeline is open now: this answer is not drawn over it
  if (r.error) { $("tl-msg").innerHTML = `<span class="bad">${esc(r.error)}</span>`; return; }
  $("tl-msg").textContent = r.line;
  TL.data = r.timeline;
  if (TL.sel && !tlSlot(TL.sel)) TL.sel = null;
  if (op.op === "replace" || op.op === "swap") TL.options = null;
  tlBuild();
  if (current) current.picks = current.picks || {};
  const missing = TL.data.clips.filter((c) => c.kind !== "black" && c.kind !== "empty" && !c.proxy).map((c) => c.slot);
  const had = TL.data.clips.some((c) => c.proxy);
  if (missing.length && had && TL.data.drive && TL.data.drive.connected !== false && !TL.auto[missing.join()]) {  // only what the edit left without a clip
    TL.auto[missing.join()] = true;
    const j = await api("/api/proxies", { project: TL.dir, slots: [...new Set(missing)] });
    if (j.job) tlWatch(j.job);
  }
}
/* a job for the timeline: its preview clips (tl-load), the XML (tl-xml), or any other job of the project found running (tl-job) */
function tlWatch(job, name) {
  const nm = name || (job.kind === "preview" ? "tl-load" : "tl-job");
  const say = { "tl-load": ["Loading the moving picture", "The moving picture was not loaded."], "tl-xml": ["Writing the Premiere XML", "The XML was not written."] }[nm] || [job.title || "Working", "It did not finish."];
  runPanel(nm, job.id, { project: TL.dir, status: "tl-status", hideDone: true, what: say[0], failed: say[1], done: nm === "tl-xml" ? "The Premiere XML is written." : "",
    onEnd: () => (nm === "tl-load" ? loadTimeline(true) : loadProject()) });
}
$("tl-play").onclick = () => (TL.playing ? tlStop() : tlPlay());
$("tl-back").onclick = () => { tlStop(); tlSeek(TL.t - 1 / TL.data.fps); };
$("tl-fwd").onclick = () => { tlStop(); tlSeek(TL.t + 1 / TL.data.fps); };
$("tl-prev").onclick = () => tlJump(-1);
$("tl-next").onclick = () => tlJump(1);
$("tl-undo").onclick = () => tlEdit({ op: "undo" });
$("tl-zoom").oninput = () => { TL.zoom = Number($("tl-zoom").value); if (TL.data) tlDraw(); };
$("tl-load").onclick = async () => {
  const r = await api("/api/proxies", { project: TL.dir });
  if (r.error) { refuseRun("tl-load", r.error, TL.dir); return; }
  $("tl-msg").textContent = "";
  tlWatch(r.job, "tl-load");
};
$("tl-write").onclick = async () => {
  const r = await api("/api/write-xml", { project: TL.dir });
  if (r.error) { refuseRun("tl-xml", r.error, TL.dir); return; }
  $("tl-msg").textContent = "";
  tlWatch(r.job, "tl-xml");
};
$("tl-overall").addEventListener("change", () => api("/api/note", { project: TL.dir, text: $("tl-overall").value }));
$("tl-ask").onclick = async () => {
  await api("/api/note", { project: TL.dir, text: $("tl-overall").value });
  const text = `/cut-from-brief Revise the plan of this project from my review notes. Project folder: ${TL.dir}. My notes are in ${TL.dir}/review.json ("overall" for the whole cut, "notes" per shot id). ` +
    `Keep what works, change ${TL.dir}/shotmap.json for what the notes ask, check it with the validator, and stop before cutting: I cut from the Videtor app (my changes on the timeline are kept in decisions.json and made again).`;
  const ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta);
  await copyText(ta, $("tl-ask")); ta.remove();
};
/* scrubbing and choosing on the timeline */
(() => {
  const inner = $("tl-inner");
  let down = false, moved = false, x0 = 0;
  const at = (e) => { const r = inner.getBoundingClientRect(); return ((e.clientX - r.left) / r.width) * TL.data.length_s; };
  inner.addEventListener("mousedown", (e) => { if (!TL.data) return; down = true; moved = false; x0 = e.clientX; });
  window.addEventListener("mousemove", (e) => { if (!down) return; if (Math.abs(e.clientX - x0) > 3) moved = true; if (moved) tlSeek(at(e)); });
  window.addEventListener("mouseup", (e) => {
    if (!down) return;
    down = false;
    if (moved) return;
    const clip = e.target.closest ? e.target.closest(".tl-clip") : null;
    if (clip) tlSelect(clip.dataset.slot, false);
    tlSeek(at(e));
  });
})();
document.addEventListener("keydown", (e) => {
  if (!TL.data || $("tl").hidden || !$("tab-look").classList.contains("active") || !$("view-project").classList.contains("active")) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target.tagName || "").toUpperCase())) return;
  const k = e.key;
  if (k === " ") { e.preventDefault(); TL.playing ? tlStop() : tlPlay(); }
  else if (k === "ArrowLeft") { e.preventDefault(); tlStop(); tlSeek(TL.t - 1 / TL.data.fps); }
  else if (k === "ArrowRight") { e.preventDefault(); tlStop(); tlSeek(TL.t + 1 / TL.data.fps); }
  else if (k === "ArrowUp") { e.preventDefault(); tlJump(-1); }
  else if (k === "ArrowDown") { e.preventDefault(); tlJump(1); }
  else if ((e.metaKey || e.ctrlKey) && k.toLowerCase() === "z") { e.preventDefault(); tlEdit({ op: "undo" }); }
});
window.addEventListener("resize", () => { if (TL.data && !$("tl").hidden) tlDraw(); });

/* ---- start ---- */
refreshState().then(() => {
  if (location.hash.length > 1) return goto(decodeURIComponent(location.hash.slice(1)));  // a link to one page: #teach, #project:look
  const v = store.get("videtor.view");
  showView(!state.projects.length ? "prep" : v && v !== "prep" ? v : "project");
});
setInterval(refreshState, 4000);
