/* AI DB Tuner single-page UI */
const $view = document.getElementById("view");
const C = window.Charts;
let ME = null, SERVER = location.origin, timers = [];

const WORKLOAD = { "oltp-write": "OLTP, write-heavy", "oltp-read": "OLTP, read-mostly", olap: "Analytical", mixed: "Mixed", idle: "Idle" };
const PHASE = { safe: "production-safe experiments", "trade-offs": "trade-off experiments", done: "no further experiments" };
function kvText(o) { return Object.entries(o || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "); }
function info(text) { return ` <span class="itip" tabindex="0" role="note" aria-label="${esc(text)}" data-tip="${esc(text)}">i</span>`; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function shareToken() { try { return sessionStorage.getItem("shareToken"); } catch (e) { return null; } }
async function api(path, opts = {}) {
  const headers = opts.body ? { "Content-Type": "application/json" } : {};
  const share = shareToken();
  if (share) headers["X-Share-Token"] = share;
  const r = await fetch(path, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined, credentials: "same-origin" });
  let data = null; try { data = await r.json(); } catch (e) { }
  if (r.status === 401 && !opts.allow401) { location.hash = "#/login"; throw new Error("login required"); }
  if (!r.ok) throw new Error((data && data.detail) || ("HTTP " + r.status));
  return data;
}
function fmtTime(t) { if (!t) return "–"; const d = new Date(t * 1000); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + C.hhmm(t); }
function ago(t) { if (!t) return "never"; const s = Date.now() / 1000 - t; if (s < 90) return Math.round(s) + "s ago"; if (s < 5400) return Math.round(s / 60) + " min ago"; if (s < 129600) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " d ago"; }
function dur(s) { if (s == null) return "–"; s = Math.round(s); if (s < 120) return s + " s"; if (s < 7200) return Math.round(s / 60) + " min"; return (s / 3600).toFixed(1) + " h"; }
function bytes(n) { if (n == null) return "–"; const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0; n = +n; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + " " + u[i]; }
const SIZE_VAR = /(_size|_capacity|_buffer|max_heap_table|_limit_bytes)$/;
function val(name, v) {
  if (v == null) return "";
  if (SIZE_VAR.test(name) && /^\d+$/.test(String(v)) && +v >= 1048576) {
    const n = +v, g = n / 1073741824;
    return String(v) + (g >= 1 ? ` (${+g.toFixed(2)} GiB)` : ` (${+(n / 1048576).toFixed(1)} MiB)`);
  }
  return String(v);
}
function shortVal(name, v) {
  if (v == null) return "";
  if (SIZE_VAR.test(name) && /^\d+$/.test(String(v)) && +v >= 1048576) { const n = +v; return n >= 1073741824 ? +(n / 1073741824).toFixed(2) + "G" : +(n / 1048576).toFixed(0) + "M"; }
  const t = String(v); return t.length > 24 ? t.slice(0, 22) + "…" : t;
}
// flag-set variables (optimizer_switch, sql_mode-like "k=v,k=v"): only the flags that differ matter
function parseFlags(v) { if (v == null) return null; const t = String(v); if (!/^[a-z_]+=[a-z0-9_]+(,[a-z_]+=[a-z0-9_]+)+$/i.test(t)) return null; return Object.fromEntries(t.split(",").map(x => x.split("="))); }
function flagDiff(before, after) {
  const a = parseFlags(before), b = parseFlags(after);
  if (!a || !b) return null;
  return Object.keys({ ...a, ...b }).filter(k => a[k] !== b[k]).map(k => ({ flag: k, from: a[k], to: b[k] }));
}
function num(v, d) { return v == null ? "–" : C.fmtNum(+v); }
function pct(v, sign = true) { if (v == null) return "–"; return (sign && v > 0 ? "+" : "") + (+v).toFixed(1) + "%"; }
function deltaClsN(v, noise) { return v == null || Math.abs(v) <= (noise || 0) ? "" : v > 0 ? "up" : "down"; }
function deltaCls(v) { return v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""; }
function goalName(g) { return g === "latency" ? "Query response time" : "Throughput (TPS)"; }
function goalUnit(g, s) { if (s && s.goal_metric) return s.goal_metric.length <= 5 ? s.goal_metric.toUpperCase() : s.goal_metric.replace(/_/g, " "); return g === "latency" ? "ms" : "tps"; }
const EXCLUDED = ["unreliable", "superseded"];
function verdictBadge(v) {
  const m = { improved: "good", "efficiency-gain": "good", regressed: "bad", "efficiency-loss": "warn", neutral: "plain", reference: "info", repeat: "plain", unreliable: "warn", superseded: "plain" };
  const tip = v === "unreliable" ? ' data-tip="Hypervisor CPU steal during the run could have moved the result by half the session noise threshold or more (at least 0.5%): excluded from all comparisons and re-measured by autotune." tabindex="0"'
    : v === "superseded" ? ' data-tip="Measured with a shorter run length. The noise plan lengthened the runs and calibrated the starting configuration again; this run is kept for reference and excluded from comparisons." tabindex="0"' : "";
  return v ? `<span class="badge ${m[v] || "plain"}"${tip}>${esc(v)}</span>` : "";
}
function statusBadge(s, why) { const m = { active: "good", measuring: "info", done: "plain", closed: "plain", abandoned: "warn", aborted: "warn" }; return `<span class="badge ${m[s] || "plain"}"${why ? ` data-tip="${esc(why)}" tabindex="0"` : ""}>${esc(s)}</span>`; }
function sevBadge(s) { const m = { high: "bad", medium: "warn", low: "plain" }; return `<span class="badge ${m[s] || "plain"}">${esc(s)}</span>`; }
function riskBadge(r) { const m = { none: "good", low: "good", compat: "warn", observability: "warn", durability: "bad" }; return `<span class="badge ${m[r] || "plain"}">risk: ${esc(r)}</span>`; }
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function setNav(active) {
  const nav = document.getElementById("nav");
  if (!ME) { nav.innerHTML = `<a class="right" href="#/login">Log in</a><a href="#/register">Create account</a>`; return; }
  if (ME.share) {
    document.body.classList.add("ro");
    const home = "#/" + (ME.share.kind === "study" ? "study/" : "env/") + ME.share.id;
    nav.innerHTML = `<a href="${home}" class="on">${ME.share.kind === "study" ? "Study" : "Environment"}: ${esc(ME.share.name || "")}</a><span class="right"><span class="badge plain" data-tip="You are looking at a read-only shared view. Nothing can be changed from here.">read-only shared view</span></span><a href="#" id="leaveShare">Leave</a>`;
    document.getElementById("leaveShare").onclick = e => { e.preventDefault(); try { sessionStorage.removeItem("shareToken"); } catch (x) { } ME = null; location.hash = "#/"; loadMe().then(route); };
    return;
  }
  document.body.classList.remove("ro");
  nav.innerHTML = `<a href="#/" class="${active === "envs" ? "on" : ""}">Environments</a><a href="#/studies" class="${active === "studies" ? "on" : ""}">Studies</a><a href="#/connect" class="${active === "connect" ? "on" : ""}">Connect agent</a>` +
    `<a href="#/parameters" class="${active === "parameters" ? "on" : ""}">Parameters</a><a href="#/policies" class="${active === "policies" ? "on" : ""}">Policies</a>${ME.is_admin ? `<a href="#/learning" class="${active === "learning" ? "on" : ""}">Learning</a><a href="/status">Status</a>` : ""}<a href="#/account" class="${active === "account" ? "on" : ""}">Account</a><span class="right">${esc(ME.email)}</span><a href="#" id="logout">Log out</a>`;
  document.getElementById("logout").onclick = async e => { e.preventDefault(); await api("/api/auth/logout", { method: "POST", body: {} }); ME = null; location.hash = "#/"; };
}

// ------------------------------------------------------------------ theme
(function () {
  const saved = (() => { try { return localStorage.getItem("theme"); } catch (e) { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  document.getElementById("theme").onclick = () => {
    const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { }
  };
})();

// ------------------------------------------------------------------ pages
function landing() {
  setNav();
  $view.innerHTML = `<section class="hero"><img class="hero-logo" src="/static/logo.svg" alt="AI DB Tuner logo"><h1>Tune MySQL with an agent you can drive from Claude Code</h1>
  <p class="sub" style="max-width:720px">Register, install the <code>tuning-agent</code> next to your database with your API key, and run tuning sessions:
  a hardware-and-version baseline, measured runs under your benchmark, ranked recommendations with the evidence behind them, and one-command apply / rollback.
  Every run's configuration diff, database and OS metrics and decision inputs land here.</p>
  <div class="row"><a class="btn primary" href="#/register">Create account</a><a class="btn" href="#/login">Log in</a></div></section>
  <div class="grid g3" style="margin-top:28px">
   <div class="card"><h3>Baseline from hardware</h3><p class="muted">Buffer pool, redo, I/O capacity, threads and caches sized from CPU, RAM, disk type and the server version (MySQL, Percona Server, MariaDB).</p></div>
   <div class="card"><h3>Goal-driven sessions</h3><p class="muted">Optimise throughput (TPS) or average query response time. Each run is compared with the previous and the first run, with a noise threshold.</p></div>
   <div class="card"><h3>Built for agents</h3><p class="muted">Every command is non-interactive with <code>--json</code> output, so Claude Code can start measurements, ask for recommendations and apply them.</p></div>
  </div>`;
}

function authForm(kind) {
  setNav();
  const reg = kind === "register";
  $view.innerHTML = `<div class="form card"><h1>${reg ? "Create account" : "Log in"}</h1>
   <form id="f">${reg ? `<label>Name</label><input name="name" autocomplete="name">` : ""}
   <label>Email</label><input name="email" type="email" required autocomplete="email">
   <label>Password${reg ? " (8+ characters)" : ""}</label><input name="password" type="password" required autocomplete="${reg ? "new-password" : "current-password"}">
   <div class="err" id="err"></div>
   <div class="row" style="margin-top:8px"><button class="primary" type="submit">${reg ? "Create account" : "Log in"}</button>
   <span class="spacer"></span><a href="#/${reg ? "login" : "register"}">${reg ? "I have an account" : "Create an account"}</a></div></form></div>`;
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    const fd = Object.fromEntries(new FormData(e.target));
    try {
      const r = await api("/api/auth/" + kind, { method: "POST", body: fd, allow401: true });
      await loadMe();
      if (reg && r.api_key) { sessionStorage.setItem("newkey", r.api_key); location.hash = "#/connect"; }
      else location.hash = "#/";
    } catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

async function sharePanel(kind, id, btn) {
  // owner: create / list / revoke read-only links
  let box = document.getElementById("sharePanel");
  if (box) { box.remove(); return; }
  box = document.createElement("div"); box.id = "sharePanel"; box.className = "card"; box.style.marginBottom = "12px";
  btn.closest(".row").after(box);
  const draw = async () => {
    const d = await api(`/api/shares?kind=${kind}&id=${id}`);
    box.innerHTML = `<div class="row"><b>Read-only links</b>${info(kind === "study" ? "Anyone with the link can view this study, all its environments, their sessions, runs, charts and configurations, without an account. Nothing can be changed." : "Anyone with the link can view this environment, its sessions, runs, charts and configurations, without an account. Nothing can be changed.")}<span class="spacer"></span><button id="newShare" class="primary">Create link</button></div>
      ${d.shares.length ? `<table class="kvt" style="margin-top:8px"><tbody>${d.shares.map(x => `<tr><td><input class="mono" readonly value="${esc(x.url)}" style="width:100%;font-size:12px" onclick="this.select()"></td><td class="muted" style="white-space:nowrap;font-size:12px">created ${ago(x.created)}${x.uses ? ` · opened ${x.uses}× · last ${ago(x.last_used)}` : " · not opened yet"}</td><td><button class="ghost" data-copy="${esc(x.url)}">Copy</button><button class="ghost danger" data-revoke="${x.id}">Revoke</button></td></tr>`).join("")}</tbody></table>` : '<p class="muted" style="margin:8px 0 0">No links yet.</p>'}`;
    box.querySelector("#newShare").onclick = async () => { await api("/api/shares", { method: "POST", body: { kind, id } }); draw(); };
    box.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; });
    box.querySelectorAll("[data-revoke]").forEach(b => b.onclick = async () => { if (confirm("Revoke this link? People using it lose access immediately.")) { await api("/api/shares/" + b.dataset.revoke, { method: "DELETE" }); draw(); } });
  };
  draw();
}
function activityLine(a) {
  // what an active session is doing right now: the agent's report (phase + time left) or derived from its latest run
  if (!a) return "";
  const nowS = Date.now() / 1000;
  const left = a.until ? Math.round((a.until - nowS) / 60) : null;
  const setAt = a.at || a.since;  // when this status started
  // derived "between runs" for over an hour also looks stuck (reset + warm-up never take that long)
  const stale = a.stale || (a.source === "derived" && a.phase === "between runs" && setAt && nowS - setAt > 3600);
  const t = (setAt ? ` · since ${ago(setAt)}` : "") + (left != null && left > 0 ? ` · ~${left} min left` : left != null && left <= 0 ? ` · overdue by ${-left} min` : "");
  const cls = stale ? "warn" : a.phase === "measuring" ? "good" : "info";
  const tip = stale ? "This phase should have ended more than 15 minutes ago and the agent has not reported anything since: check the host."
    : a.source === "derived" ? "Derived from the session's latest run (the agent running this session predates live activity reports)." : "Reported by the tuning-agent.";
  return `<div style="font-size:12px;margin-top:3px"><span class="badge ${cls}" data-tip="${esc(tip)}" tabindex="0">${esc(a.phase)}</span> <span class="muted">${esc(a.detail || "")}${t}</span></div>`;
}
function envStatus(e) {
  if (e.destroyed_at) return `<span class="badge plain" data-tip="The machine was deleted ${ago(e.destroyed_at)} (e.g. at the end of a study). Its sessions and results are kept.">decommissioned</span>`;
  const stopped = e.agent_state === "stopped" && (e.agent_state_at || 0) >= (e.last_seen || 0);
  if (e.online && !stopped) return '<span class="badge good">online</span>';
  if (stopped) return `<span class="badge plain" data-tip="The tuning-agent service was stopped on the host ${ago(e.agent_state_at)} (systemctl stop), not a loss of contact.">agent stopped</span>`;
  return `<span class="badge warn" data-tip="Last report from the agent ${ago(e.last_seen)}: host down, agent crashed or network problem.">offline · ${ago(e.last_seen)}</span>`;
}
async function envList() {
  setNav("envs");
  const d = await api("/api/environments");
  if (!d.environments.length) {
    $view.innerHTML = `<h1>Environments</h1><p class="sub">An environment is one database server with a connected tuning-agent.</p>
    <div class="card"><h3>No environment connected yet</h3><p>Install the agent on the database host to see it here.</p><a class="btn primary" href="#/connect">Connect an agent</a></div>`;
    return;
  }
  const allEnvs = d.environments.slice().sort((a, b) => (!!a.destroyed_at - !!b.destroyed_at) || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
  let mode = "list", scope = "active";
  try { mode = localStorage.getItem("envView") || "list"; scope = localStorage.getItem("envScope") || "active"; } catch (e) { }
  const nActive = allEnvs.filter(e => !e.destroyed_at).length;
  let envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.destroyed_at);
  $view.innerHTML = `<div class="row"><div><h1>Environments</h1><p class="sub">Database servers with a connected tuning-agent.</p></div><span class="spacer"></span>
    ${allEnvs.length > nActive ? `<div class="seg" role="group" aria-label="Show" id="scopeSeg"><button data-s="active">Active (${nActive})</button><button data-s="all">All (${allEnvs.length})</button></div>` : ""}
    <div class="seg" role="group" aria-label="View" id="viewSeg"><button data-v="list">List</button><button data-v="cards">Cards</button></div><a class="btn" href="#/connect">Connect another</a></div><div id="envs"></div>`;
  const box = document.getElementById("envs");
  const metrics = e => { const l = (e.latest && e.latest.db) || {}, o = (e.latest && e.latest.os) || {};
    const dmb = o.disk_r_mbs != null ? (o.disk_r_mbs || 0) + (o.disk_w_mbs || 0) : null;
    return { tps: e.online ? num(l.tps) : "–", lat: e.online && l.avg_latency_ms != null ? num(l.avg_latency_ms) + " ms" : "–", cpu: e.online && o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–",
      disk: e.online && dmb != null ? num(dmb) + " MB/s" : "–", diskTip: e.online && dmb != null ? `read ${num(o.disk_r_mbs)} MB/s · write ${num(o.disk_w_mbs)} MB/s · ${num((o.disk_r_iops || 0) + (o.disk_w_iops || 0))} IOPS` : "" }; };
  const clients = e => (e.clients || []).length ? `<div class="muted" style="font-size:12px" data-tip="Client hosts linked to this database server: ${esc(e.clients.map(c => c.name + (c.online ? " (online)" : " (offline)")).join(", "))}">+ ${e.clients.length} client${e.clients.length > 1 ? "s" : ""}: ${esc(e.clients.map(c => c.name).join(", "))}</div>` : "";
  const status = e => envStatus(e);
  const draw = () => {
    box.parentElement.querySelectorAll("#viewSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === mode));
    box.parentElement.querySelectorAll("#scopeSeg button").forEach(b => b.classList.toggle("on", b.dataset.s === scope));
    if (mode === "list") {
      box.innerHTML = `<div class="card tablewrap"><table data-opt="Database|Hardware|Data|TPS|Avg latency|Disk|Sessions"><thead><tr><th>Name</th><th>Status</th><th>Database</th><th>Hardware</th><th class="num">Data</th><th class="num">TPS</th><th class="num">Avg latency</th><th class="num">CPU</th><th class="num">Disk</th><th>Active session</th><th class="num">Sessions</th></tr></thead><tbody>${envs.map(e => {
        const m = metrics(e);
        return `<tr class="click" data-href="#/env/${e.id}"><td><a href="#/env/${e.id}"><b>${esc(e.name)}</b></a>${clients(e)}</td><td>${status(e)}</td><td>${esc(e.db_short || e.db)}</td><td>${e.cpus || "?"} CPU · ${bytes(e.ram_bytes)} · ${e.disk}</td><td class="num">${bytes(e.data_bytes)}</td>
          <td class="num">${m.tps}</td><td class="num">${m.lat}</td><td class="num">${m.cpu}</td><td class="num"${m.diskTip ? ` data-tip="${m.diskTip}"` : ""}>${m.disk}</td><td>${e.active_session ? `<a href="#/session/${e.active_session.id}">#${e.active_session.id} ${esc(e.active_session.name)}</a>${activityLine(e.active_session.activity)}` : '<span class="muted">–</span>'}</td><td class="num">${e.sessions}</td></tr>`; }).join("")}</tbody></table></div>`;
      box.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = ev => { if (!ev.target.closest("a")) location.hash = tr.dataset.href; });
    } else {
      box.innerHTML = `<div class="grid g3">${envs.map(e => { const m = metrics(e);
        return `<a class="card" href="#/env/${e.id}" style="color:inherit;text-decoration:none"><div class="row"><b style="font-size:15px">${esc(e.name)}</b><span class="spacer"></span>${status(e)}</div>
          <div class="muted" style="margin:4px 0 10px">${esc(e.db_short || e.db)}${(e.clients || []).length ? ` · + ${e.clients.length} client${e.clients.length > 1 ? "s" : ""}` : ""}<br>${e.cpus || "?"} CPUs · ${bytes(e.ram_bytes)} RAM · ${e.disk} · data ${bytes(e.data_bytes)}</div>
          <div class="row" style="gap:18px"><div><div class="muted" style="font-size:12px">TPS</div><b>${m.tps}</b></div><div><div class="muted" style="font-size:12px">Avg latency</div><b>${m.lat}</b></div>
          <div><div class="muted" style="font-size:12px">CPU busy</div><b>${m.cpu}</b></div><div data-tip="${m.diskTip}"><div class="muted" style="font-size:12px">Disk</div><b>${m.disk}</b></div><div><div class="muted" style="font-size:12px">Sessions</div><b>${e.sessions}</b></div></div>
          ${e.active_session ? `<div style="margin-top:10px;font-size:13px;overflow-wrap:anywhere"><span class="muted">Session</span> #${e.active_session.id} ${esc(e.active_session.name)}${activityLine(e.active_session.activity)}</div>` : ""}</a>`; }).join("")}</div>`;
    }
  };
  box.parentElement.querySelectorAll("#viewSeg button").forEach(b => b.onclick = () => { mode = b.dataset.v; try { localStorage.setItem("envView", mode); } catch (e) { } draw(); });
  box.parentElement.querySelectorAll("#scopeSeg button").forEach(b => b.onclick = () => {
    scope = b.dataset.s; try { localStorage.setItem("envScope", scope); } catch (e) { }
    envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.destroyed_at); draw();
  });
  draw();
  timers.push(setInterval(async () => {
    if (!document.getElementById("envs")) return;
    const d2 = await api("/api/environments"); const byId = Object.fromEntries(d2.environments.map(e => [e.id, e]));
    allEnvs.forEach((e, i) => { if (byId[e.id]) allEnvs[i] = byId[e.id]; });
    envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.destroyed_at); draw();
  }, 15000));
}

const STUDY_KINDS = { compare: "Compare", scaling: "Hardware scaling", "min-hardware": "Minimum hardware" };
const STUDY_TIPS = {
  compare: "Sessions on several environments with the same workload, side by side.",
  scaling: "How the tuned result grows with hardware: throughput relative to the smallest environment versus its CPU count.",
  "min-hardware": "The cheapest (or smallest) environment whose best production-safe configuration reaches the target.",
};
async function studiesPage() {
  setNav("studies");
  const d = await api("/api/studies");
  $view.innerHTML = `<div class="row"><div><h1>Studies</h1><p class="sub">Experiments across environments: compare hardware, measure scaling, find the minimum hardware for a target.</p></div><span class="spacer"></span><button class="primary" id="newStudy">New study</button></div>
  <div class="card" id="newForm" hidden><div class="grid g2" style="gap:10px">
    <label>Name<input id="nsName" placeholder="HammerDB 200W hardware scaling"></label>
    <label>Kind<select id="nsKind">${Object.entries(STUDY_KINDS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
    <label>Goal<select id="nsGoal"><option value="tps">Throughput</option><option value="latency">Avg response time</option></select></label>
    <label>Target${info("Minimum hardware studies: the goal value to reach (e.g. 100000 for NOPM, or 5 for 5 ms average latency).")}<input id="nsTarget" type="number" step="any" placeholder="optional"></label></div>
    <div class="row" style="margin-top:10px"><button class="primary" id="nsCreate">Create</button><span class="muted">Add sessions from each session page, or start them with <code>tuning-agent session start --study "name"</code>.</span></div></div>
  <div class="card tablewrap" style="margin-top:10px"><table data-opt="Goal|Target|Environments|Created"><thead><tr><th>#</th><th>Study</th><th>Kind</th><th>Goal</th><th class="num">Target</th><th class="num">Environments</th><th class="num">Sessions</th><th>Created</th></tr></thead><tbody>
  ${d.studies.length ? d.studies.map(st => `<tr class="click" data-href="#/study/${st.id}"><td class="muted">${st.id}</td><td><a href="#/study/${st.id}"><b>${esc(st.name)}</b></a></td><td>${STUDY_KINDS[st.kind] || esc(st.kind)}</td><td>${goalName(st.goal || "tps")}</td><td class="num">${st.target != null ? num(st.target) : "–"}</td><td class="num">${st.environments}</td><td class="num">${st.sessions}${st.active ? ` <span class="badge info">${st.active} active</span>` : ""}</td><td>${fmtTime(st.created)}</td></tr>`).join("")
    : '<tr><td colspan="8" class="muted">No studies yet.</td></tr>'}</tbody></table></div>`;
  $view.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = ev => { if (!ev.target.closest("a")) location.hash = tr.dataset.href; });
  document.getElementById("newStudy").onclick = () => { const f = document.getElementById("newForm"); f.hidden = !f.hidden; };
  document.getElementById("nsCreate").onclick = async () => {
    const name = document.getElementById("nsName").value.trim(); if (!name) return;
    const r = await api("/api/studies", { method: "POST", body: { name, kind: document.getElementById("nsKind").value, goal: document.getElementById("nsGoal").value, target: document.getElementById("nsTarget").value } });
    location.hash = "#/study/" + r.id;
  };
}

async function studyPage(id) {
  setNav("studies");
  const d = await api("/api/studies/" + id);
  const st = d.study, lat = st.goal === "latency";
  const rows = d.sessions.map(s => {
    const e = s.environment, r = s.results || {}, bs = (r.best_safe || {}).mean, bt = r.best && !r.best.safe ? r.best.mean : null;
    return { s, e, r, bs, bt, first: (r.first || {}).value, cpus: e.cpus || 0, cost: e.cost_month,
      meets: st.target == null || bs == null ? null : (lat ? bs <= st.target : bs >= st.target) };
  });
  rows.sort((a, b) => (a.cost || 0) - (b.cost || 0) || a.cpus - b.cpus || (a.e.ram_bytes || 0) - (b.e.ram_bytes || 0));
  const better = (a, b) => lat ? a < b : a > b;
  const mixedWl = new Set(rows.map(x => x.s.workload || x.s.goal_metric || "")).size > 1;
  let verdict = "";
  const measured = rows.filter(x => x.bs != null);
  if (st.kind === "min-hardware") {
    if (st.target == null) verdict = `<span class="muted">Set a target to find the minimum hardware.</span>`;
    else {
      const ok = measured.filter(x => x.meets);
      verdict = ok.length ? `<b>${esc(ok[0].e.name)}</b> is the smallest environment reaching ${num(st.target)}: best production-safe ${num(ok[0].bs)} (${ok[0].e.cpus} CPU · ${bytes(ok[0].e.ram_bytes)}${ok[0].cost ? ` · $${num(ok[0].cost)}/month` : ""}). <a href="#/session/${ok[0].s.id}">Its configuration</a>.`
        : measured.length ? `No environment reaches ${num(st.target)} yet; closest: <b>${esc(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).e.name)}</b> with ${num(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).bs)}.` : "No results yet.";
    }
  } else if (mixedWl && measured.length) {
    const g = measured.filter(x => (x.r.best_safe || {}).gain_pct != null).sort((a, b) => b.r.best_safe.gain_pct - a.r.best_safe.gain_pct);
    verdict = g.length ? `The sessions run different workloads, so results are compared as gains over each session's own starting configuration. Largest: <b>${esc(g[0].s.name.replace(/^Battery \d+: /, ""))}</b> ${pct(g[0].r.best_safe.gain_pct)}; smallest: ${esc(g[g.length - 1].s.name.replace(/^Battery \d+: /, ""))} ${pct(g[g.length - 1].r.best_safe.gain_pct)}.` : "No results yet.";
  } else if (measured.length >= 2) {
    const small = measured[0], big = measured[measured.length - 1];
    const hw = big.cpus && small.cpus ? big.cpus / small.cpus : null, perf = lat ? small.bs / big.bs : big.bs / small.bs;
    verdict = st.kind === "scaling" && hw ? `From <b>${esc(small.e.name)}</b> (${small.cpus} CPU) to <b>${esc(big.e.name)}</b> (${big.cpus} CPU): ${num(hw)}× the CPUs gave ${num(perf)}× the tuned result — scaling efficiency ${Math.round(100 * perf / hw)}%.${info("Scaling efficiency = result ratio / CPU ratio. 100% = linear scaling. Tuned = best production-safe configuration of each environment.")}`
      : `Best production-safe result: <b>${esc(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).e.name)}</b>.`;
  } else verdict = measured.length ? "Add sessions from more environments to compare." : "No results yet.";
  $view.innerHTML = `<div class="crumbs"><a href="#/studies">Studies</a> /</div>
  <div class="row"><h1>${esc(st.name)}</h1><span class="badge plain">${STUDY_KINDS[st.kind] || esc(st.kind)}</span>${info(STUDY_TIPS[st.kind] || "")}<span class="spacer"></span><button class="ghost owner" id="share">Share</button><button class="ghost" id="edit">Edit</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">Goal ${goalName(st.goal || "tps")}${st.target != null ? ` · target <b>${num(st.target)}</b>` : ""}${st.workload ? " · workload: " + esc(st.workload) : ""} · ${new Set(rows.map(x => x.e.id)).size} environment(s), ${rows.length} session(s)</p>
  <div class="note" style="margin-bottom:12px">${verdict}</div>
  <div class="card" id="sbars"></div>
  <h2>Results by environment${info("Start = first run of each session (usually server defaults). Tuned = best production-safe configuration; with trade-offs = best configuration that gives up durability, replication or observability guarantees. Values are means of all runs of a configuration. Click a cost to edit it.")}</h2>
  <div class="card tablewrap"><table data-opt="Hardware|Cost / month|Start|With trade-offs|Per CPU|Per $|Study cost"><thead><tr><th>Environment</th><th>Hardware</th><th class="num">Cost / month</th><th>Session</th><th class="num">Start</th><th class="num">Tuned</th><th class="num">Gain</th><th class="num">With trade-offs</th><th class="num">Per CPU</th>${rows.some(x => x.cost) ? '<th class="num">Per $</th>' : ""}${rows.some(x => x.s.cost || x.s.env_cost) ? `<th class="num">Study cost${info("Machine cost: hourly price × time the machine existed (setup and data load included) when its lifetime is recorded, otherwise the session's wall time. Per-run costs: session pages and the CSV.")}</th>` : ""}${st.target != null ? "<th>Target</th>" : ""}<th></th></tr></thead><tbody>
  ${rows.length ? rows.map(x => `<tr><td><a href="#/env/${x.e.id}"><b>${esc(x.e.name)}</b></a></td><td>${x.e.cpus || "?"} CPU · ${bytes(x.e.ram_bytes)} · ${x.e.disk}</td>
    <td class="num"><a href="#" class="cost" data-env="${x.e.id}" data-cost="${x.cost != null ? x.cost : ""}">${x.cost ? "$" + Math.round(x.cost) : "set"}</a></td>
    <td><a href="#/session/${x.s.id}">#${x.s.id} ${esc(x.s.name)}</a> ${x.s.status === "active" ? "" : statusBadge(x.s.status)}${activityLine(x.s.activity)}</td><td class="num">${num(x.first)}</td><td class="num"><b>${num(x.bs)}</b></td>
    <td class="num ${deltaCls((x.r.best_safe || {}).gain_pct)}">${pct((x.r.best_safe || {}).gain_pct)}</td><td class="num">${x.bt != null ? num(x.bt) : "–"}</td>
    <td class="num">${x.bs != null && x.cpus ? num(lat ? x.bs * x.cpus : x.bs / x.cpus) : "–"}</td>${rows.some(y => y.cost) ? `<td class="num">${x.bs != null && x.cost ? num(lat ? x.bs * x.cost : x.bs / x.cost) : "–"}</td>` : ""}
${rows.some(y => y.s.cost || y.s.env_cost) ? `<td class="num">${(() => { const c = x.s.env_cost || x.s.cost; return c ? `<span data-tip="${c.hours} h × $${c.rate_hourly.toFixed(4)}/h${x.s.env_cost && x.s.env_cost.destroyed ? " · machine destroyed" : ""}${x.s.cost ? ` · session ${x.s.cost.hours} h = $${x.s.cost.total.toFixed(2)}` : ""}">$${c.total.toFixed(2)}</span>` : "–"; })()}</td>` : ""}
        ${st.target != null ? `<td>${x.meets == null ? "–" : x.meets ? '<span class="badge good">meets</span>' : '<span class="badge plain">below</span>'}</td>` : ""}
    <td><a href="#" class="detach" data-sid="${x.s.id}" title="Remove from study">×</a></td></tr>`).join("") : `<tr><td colspan="12" class="muted">No sessions yet.</td></tr>`}</tbody></table></div>
  ${rows.some(x => x.s.cost || x.s.env_cost) ? `<p class="muted" style="margin:6px 0 0">Total machine cost $${rows.reduce((a, x) => a + ((x.s.env_cost || x.s.cost || {}).total || 0), 0).toFixed(2)} · <a href="/api/studies/${id}/costs?format=csv" download="study-${id}-costs.csv">cost per run (CSV)</a></p>` : ""}
  <div class="row" style="margin-top:8px"><select id="addSess" style="width:auto"><option value="">Add a session…</option>${d.other_sessions.map(o => `<option value="${o.id}">#${o.id} ${esc(o.env_name)} · ${esc(o.name)}</option>`).join("")}</select>
   <span class="muted">or <code>tuning-agent session start --study "${esc(st.name)}" ...</code> on each environment</span></div>
  <h2>Notes</h2><div class="card"><textarea id="snotes" rows="3" placeholder="Hypothesis, method, conclusions">${esc(st.notes || "")}</textarea><div class="row" style="margin-top:6px"><button id="savesn">Save notes</button><span id="snsaved" class="muted"></span></div></div>`;
  if (mixedWl) C.barChart(document.getElementById("sbars"), {
    title: "Gain of the best production-safe configuration over each session's starting configuration (%)", unit: "%", metric: "gain",
    bars: rows.filter(x => (x.r.best_safe || {}).gain_pct != null).map(x => ({ label: x.s.name.replace(/^Battery \d+: /, ""), short: (x.s.name.match(/: ([A-Z]\d+) /) || [])[1] || "#" + x.s.id,
      value: Math.max(0, x.r.best_safe.gain_pct), sub: pct(x.r.best_safe.gain_pct), href: "#/session/" + x.s.id, highlight: "best",
      tip: `<div>start<b>${num(x.first)}</b></div><div>tuned<b>${num(x.bs)}</b></div>` })),
  });
  else C.barChart(document.getElementById("sbars"), {
    title: `Tuned result per environment (${goalName(st.goal || "tps")}) — green: meets the target`, unit: "", metric: "tuned",
    ref: st.target, refLabel: "target", bars: rows.map(x => ({ label: x.e.name + (x.cpus ? ` (${x.cpus} CPU)` : ""), value: x.bs, sub: x.first ? "start " + num(x.first) : "", href: "#/session/" + x.s.id, highlight: x.meets ? "best" : false })),
  });
  $view.querySelectorAll("a.cost").forEach(a => a.onclick = async ev => { ev.preventDefault(); const v = prompt("Cost per month in USD (empty to clear)", a.dataset.cost); if (v === null) return; await api("/api/environments/" + a.dataset.env, { method: "PATCH", body: { cost_month: v } }); studyPage(id); });
  $view.querySelectorAll("a.detach").forEach(a => a.onclick = async ev => { ev.preventDefault(); await api("/api/sessions/" + a.dataset.sid, { method: "PATCH", body: { study_id: null } }); studyPage(id); });
  document.getElementById("addSess").onchange = async e => { if (!e.target.value) return; await api("/api/sessions/" + e.target.value, { method: "PATCH", body: { study_id: id } }); studyPage(id); };
  document.getElementById("share").onclick = ev => sharePanel("study", id, ev.target);
  document.getElementById("savesn").onclick = async () => { await api("/api/studies/" + id, { method: "PATCH", body: { notes: document.getElementById("snotes").value } }); document.getElementById("snsaved").textContent = "saved"; };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this study? Its sessions stay in their environments.")) { await api("/api/studies/" + id, { method: "DELETE" }); location.hash = "#/studies"; } };
  document.getElementById("edit").onclick = async () => {
    const name = prompt("Name", st.name); if (name === null) return;
    const kind = prompt("Kind: compare, scaling or min-hardware", st.kind); if (kind === null) return;
    const target = prompt("Target goal value (empty for none)", st.target != null ? st.target : ""); if (target === null) return;
    await api("/api/studies/" + id, { method: "PATCH", body: { name, kind, target } }); studyPage(id);
  };
}

async function connectPage() {
  setNav("connect");
  const keys = await api("/api/keys");
  const fresh = sessionStorage.getItem("newkey");
  const keyShown = fresh || "YOUR_API_KEY";
  const cmd = `curl -fsSL ${SERVER}/install.sh | sudo bash -s -- --api-key ${keyShown} --name my-db`;
  $view.innerHTML = `<h1>Connect a tuning-agent</h1><p class="sub">Run this on the database host (Linux, Python 3.8+, systemd). It installs the agent, registers the environment and starts the metrics collector.</p>
  ${fresh ? `<div class="card" style="margin-bottom:14px"><b>Your new API key</b> — shown once, store it somewhere safe:<div class="key" style="margin-top:6px">${esc(fresh)}</div></div>` : ""}
  <h2>1. Install</h2><pre><code>${esc(cmd)}</code></pre>
  <p class="muted">The agent connects to MySQL as <code>root</code> over the local socket by default. Add <code>--mysql-user U --mysql-password P</code> (or <code>--mysql-host</code>/<code>--mysql-port</code>) for other accounts; it needs <code>PROCESS</code>, <code>SELECT</code> on <code>performance_schema</code> and <code>SYSTEM_VARIABLES_ADMIN</code> to apply changes. Add <code>--no-restart</code> to forbid server restarts.</p>
  <h2>2. Tune</h2><pre><code>tuning-agent baseline --apply          # hardware + version based starting point
tuning-agent session start --goal tps --policy benchmark --name "sysbench 64 threads"
# start your benchmark, let it warm up, then:
tuning-agent measure run --duration 300 --label baseline
tuning-agent recommend
tuning-agent apply --next
tuning-agent measure run --duration 300
tuning-agent rollback --best            # keep the winner
tuning-agent session stop</code></pre>
  <h2>3. Drive it from Claude Code</h2>
  <p>Tell Claude Code: <i>“Tune this MySQL server for throughput using tuning-agent. Read ${esc(SERVER)}/agent-guide.md first.”</i> The guide explains the workflow, the JSON output and the benchmark hygiene rules. Every command takes <code>--json</code>.</p>
  <h2>API keys</h2><div class="card"><div class="tablewrap"><table><thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody id="keys"></tbody></table></div>
  <div class="row" style="margin-top:10px"><input id="kname" placeholder="key name, e.g. lab-db-01" style="max-width:260px"><button id="newkey" class="primary">Create key</button></div></div>`;
  const tb = document.getElementById("keys");
  keys.keys.forEach(k => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${esc(k.name)}</td><td class="mono">${esc(k.prefix)}…</td><td>${fmtTime(k.created)}</td><td>${k.revoked ? "revoked" : ago(k.last_used)}</td><td>${k.revoked ? "" : '<button class="ghost danger">Revoke</button>'}</td>`;
    const b = tr.querySelector("button");
    if (b) b.onclick = async () => { if (confirm("Revoke key " + k.prefix + "…? Agents using it will stop reporting.")) { await api("/api/keys/" + k.id, { method: "DELETE" }); connectPage(); } };
    tb.appendChild(tr);
  });
  document.getElementById("newkey").onclick = async () => {
    const r = await api("/api/keys", { method: "POST", body: { name: document.getElementById("kname").value } });
    sessionStorage.setItem("newkey", r.api_key); connectPage();
  };
  window.addEventListener("hashchange", () => sessionStorage.removeItem("newkey"), { once: true });
}

// ------------------------------------------------------------------ parameters
async function parametersPage() {
  setNav("parameters");
  const d = await api("/api/parameters");
  const st = (() => { try { return JSON.parse(sessionStorage.getItem("paramFilter") || "{}"); } catch (x) { return {}; } })();
  const q = (st.q || "").toLowerCase(), fi = st.impact || "", fs = st.scope || "";
  const rank = { high: 0, medium: 1, low: 2, unknown: 3, container: 4 };
  const rows = d.parameters.filter(p => (!q || (p.name + " " + p.description + " " + (p.when || "")).toLowerCase().includes(q))
    && (!fi || p.effective_impact === fi) && (!fs || p.scope === fs))
    .sort((a, b) => ((rank[a.effective_impact] ?? 3) - (rank[b.effective_impact] ?? 3)) || a.name.localeCompare(b.name));
  const impBadge = p => { const l = p.learned && p.learned.impact !== "unknown" ? p.learned : null;
    const tip = `Shipped estimate: ${p.impact}` + (l ? `. Measured: ${l.impact} - ${l.runs} single-setting experiments in ${l.sessions} session(s): ${l.improved} improved, ${l.regressed} regressed, ${l.neutral} within noise; median change of the decisive ones ${l.median_abs_delta_pct}% (max ${l.max_abs_delta_pct}%). A summary of experiment data, not a law.` : ". Not measured often enough yet to replace the estimate.")
      + (p.learned_by_workload ? " By workload: " + Object.entries(p.learned_by_workload).map(([w, v]) => `${w} ${v.impact} (${v.runs} runs)`).join(", ") + "." : "");
    return `<span class="badge ${{ high: "good", medium: "info", low: "plain", unknown: "plain" }[p.effective_impact] || "plain"}" data-tip="${esc(tip)}" tabindex="0">${esc(p.effective_impact)}${l ? " · measured" : ""}</span>`; };
  const polSel = (p, admin) => { const cur = admin ? p.default_mode : (p.user_mode || "default");
    const opts = admin ? ["tune", "block"] : ["default", "tune", "block"];
    return `<select data-${admin ? "def" : "pol"}="${esc(p.name)}" style="font-size:12px;width:auto">${opts.map(o => `<option value="${o}" ${o === cur ? "selected" : ""}>${o === "default" ? `default (${p.default_mode})` : o}</option>`).join("")}</select>`; };
  $view.innerHTML = `<h1>Parameters${info("The MySQL and operating-system settings the tuner works with. Impact: how much a setting can move results when it counts (shipped estimate; replaced by the measured impact once enough single-setting experiments exist - a summary of experiment data, not a law). When it counts: the conditions or measured bottleneck in which it matters. Precondition: what must happen in the workload for it to act at all (otherwise the tuner does not try it). Safety: safe, or what a value gives up (durability, observability, replication, semantics). Your policy: the tuner never changes a blocked setting in your sessions; 'default' follows the service default.")}</h1>
  <div class="row" style="margin-bottom:8px;gap:8px;flex-wrap:wrap"><input id="pq" placeholder="search" value="${esc(st.q || "")}" style="max-width:260px">
   <select id="pimp" style="width:auto"><option value="">any impact</option>${["high", "medium", "low", "unknown", "container"].map(x => `<option ${x === fi ? "selected" : ""}>${x}</option>`).join("")}</select>
   <select id="pscope" style="width:auto"><option value="">MySQL and OS</option>${["mysql", "os", "pseudo"].map(x => `<option ${x === fs ? "selected" : ""}>${x}</option>`).join("")}</select>
   <span class="muted" style="font-size:12.5px">${rows.length} of ${d.parameters.length} · blocked for you: ${d.parameters.filter(p => p.mode === "block").length}</span></div>
  <div class="card tablewrap"><table><thead><tr><th>Parameter</th><th>Impact</th><th>When it counts</th><th>Safety</th><th>Restart</th><th>Your policy</th>${d.admin ? "<th>Service default</th>" : ""}</tr></thead><tbody>
  ${rows.map(p => `<tr><td class="mono" style="white-space:nowrap">${esc(p.name)}${info((p.description || "") + (p.precondition ? ` Precondition: ${p.precondition}.` : "") + (p.rules && p.rules.length ? ` Tuner rules: ${p.rules.join(", ")}.` : "") + (p.mechanism_tags && p.mechanism_tags.length ? ` Acts on: ${p.mechanism_tags.join(", ")}.` : ""))}</td>
    <td>${impBadge(p)}</td><td style="font-size:12.5px;max-width:360px">${esc(p.when || "")}</td>
    <td>${p.safety === "safe" ? '<span class="badge good">safe</span>' : `<span class="badge warn" ${p.safety_note ? `data-tip="${esc(p.safety_note)}"` : ""} tabindex="0">${esc(p.safety || "?")}</span>`}</td>
    <td>${p.restart ? "yes" : p.restart === false ? "no" : ""}</td><td>${polSel(p, false)}</td>${d.admin ? `<td>${polSel(p, true)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
  const save = () => { try { sessionStorage.setItem("paramFilter", JSON.stringify({ q: document.getElementById("pq").value, impact: document.getElementById("pimp").value, scope: document.getElementById("pscope").value })); } catch (x) { } parametersPage(); };
  document.getElementById("pq").onchange = save; document.getElementById("pimp").onchange = save; document.getElementById("pscope").onchange = save;
  $view.querySelectorAll("select[data-pol]").forEach(el => el.onchange = async () => { await api(`/api/parameters/${encodeURIComponent(el.dataset.pol)}/policy`, { method: "PUT", body: { mode: el.value } }); parametersPage(); });
  $view.querySelectorAll("select[data-def]").forEach(el => el.onchange = async () => { await api(`/api/admin/parameters/${encodeURIComponent(el.dataset.def)}/policy`, { method: "PUT", body: { mode: el.value } }); parametersPage(); });
}

// ------------------------------------------------------------------ learning (admin)
async function learningPage() {
  setNav("learning");
  if (!ME.is_admin) { $view.innerHTML = '<p class="muted">Administrators only.</p>'; return; }
  const [d, L] = await Promise.all([api("/api/admin/learning"), api("/api/admin/learner")]);
  const st = (sessions) => sessions.map(s => `<a href="#/session/${s}">#${s}</a>`).join(" ");
  const pct = v => v == null ? "" : (v > 0 ? "+" : "") + v + "%";
  const filt = (() => { try { return sessionStorage.getItem("learnFilter") || ""; } catch (x) { return ""; } })();
  const ex = d.experience.filter(e => !filt || (e.idea + " " + e.workload).toLowerCase().includes(filt.toLowerCase()));
  let h = `<h1>Learning${info("What the tuner has learned across all sessions and accounts. Agents receive the part that matches their own account and conditions (workload type, CPU class, concurrency class) to rank experiments; ideas that keep losing in comparable sessions are tried last. Hypothesis outcomes per targeted component set how credible an idea is for a given bottleneck. Bottleneck analyses are reused for similar conditions. Snapshots freeze this view as a downloadable file.")}</h1>
  <p class="muted">${d.runs} measured runs behind it.</p>
  <h3>Learner${info("A background process that reads every closed session (observations, tuner.db), asks the service's AI model what it teaches, and keeps the result in the separate learnings database (learning.db): insights with the conditions they hold in, and proposals for the tuner's code. Insights are summaries of experiment data, not truths: each is counted against the observations - runs that changed the same settings under the same conditions - and its status says how well it repeated (observed, repeated, contested, contradicted). Agents receive matching insights as leads to test; only repeated ones nudge the ranking. The evidence gate still decides what runs.")}</h3>
  <div class="card"><div class="row"><span>Every ${L.interval_hours} h · ${L.processed} sessions read · ${L.insights.filter(x => x.status !== "archived").length} live insights (${L.insights.filter(x => x.status === "archived").length} archived) · ${L.proposals.filter(p => p.status === "open").length} open proposals</span><span class="spacer"></span><button class="btn" id="pruneNow" data-tip="Recount every insight against the observations and archive the stale ones: observations that never repeated within 14 days, contradicted results after 7 days, and results resting only on flawed runs (not steady, unreliable, saturated client) or on pre-fix sessions (counted at half weight).">Prune now</button><button class="btn" id="learnNow">Run the learner now</button></div>
  ${L.runs.length ? `<table style="margin-top:8px"><thead><tr><th>Pass</th><th>Started (UTC)</th><th>Trigger</th><th class="num">Sessions</th><th class="num">AI calls</th><th class="num">Cost $</th><th class="num">New / updated insights</th><th class="num">Proposals</th><th>Result</th></tr></thead><tbody>${L.runs.map(x => `<tr><td>#${x.id}</td><td>${new Date(x.started * 1000).toISOString().slice(0, 16).replace("T", " ")}</td><td>${esc(x.trigger || "")}</td><td class="num">${x.sessions}</td><td class="num">${x.calls}</td><td class="num">${(x.cost || 0).toFixed(3)}</td><td class="num">${x.insights_new} / ${x.insights_updated}</td><td class="num">${x.proposals_new}</td><td>${x.ended ? (x.error ? `<span class="badge warn" data-tip="${esc(x.error)}">error</span>` : '<span class="badge good">done</span>') : '<span class="badge plain">running</span>'}</td></tr>`).join("")}</tbody></table>` : '<p class="muted" style="margin:6px 0 0">No pass yet.</p>'}</div>
  <h3>Insights${info("Summaries of experiment data, not truths: each says what was measured, in which conditions, on how many sessions and hosts, with its caveats. A result may not repeat, may hold only in narrow conditions, or may come from a single host. Status says how well it repeated - observed (one session), repeated (supported in 2+ sessions by 2:1 of the runs), contested (runs disagree), contradicted (more against than for) - counted from the observations, not taken from the AI. Agents see them as earlier results to test; only repeated ones nudge the ranking. Mark one 'implemented' once the tuner's code covers it.")}</h3>
  <div class="card tablewrap"><table><thead><tr><th>Status</th><th>Statement</th><th>Settings</th><th>Conditions</th><th class="num">runs for / against</th><th class="num">sessions · hosts</th><th>Sessions</th><th></th></tr></thead><tbody>
  ${L.insights.map(x => `<tr><td><span class="badge ${x.status === "repeated" ? "good" : ["contradicted", "contested", "retired-by-admin"].includes(x.status) ? "warn" : "plain"}" ${x.notes ? `data-tip="${esc(x.notes)}"` : ""}>${esc(x.status)}</span></td><td style="max-width:420px">${esc(x.statement)} <span class="muted">(${esc(x.direction)})</span>${x.caveats ? info("Caveats: " + x.caveats) : ""}</td><td class="mono" style="font-size:12px">${esc((x.settings || []).map(t => t.variable + "=" + t.value).join(", "))}</td><td style="font-size:12px">${esc([x.conditions.workload, x.conditions.cpu_class && x.conditions.cpu_class + " CPUs", x.conditions.concurrency_class && x.conditions.concurrency_class + " conns"].filter(Boolean).join(", "))}</td><td class="num">${x.support} / ${x.contradict}</td><td class="num">${(x.sessions || []).length} · ${x.hosts ?? "?"}</td><td style="font-size:12px">${(x.sessions || []).slice(-6).map(t => `<a href="#/session/${t}">#${t}</a>`).join(" ")}</td><td><select data-ins="${x.id}" style="font-size:12px;width:auto">${["observed", "repeated", "contested", "contradicted", "implemented", "retired-by-admin", "archived"].map(o => `<option ${o === x.status ? "selected" : ""}>${o}</option>`).join("")}</select></td></tr>`).join("") || '<tr><td colspan="8" class="muted">none yet</td></tr>'}</tbody></table></div>
  <h3>Proposals for the tuner's code${info("What the learner thinks the tuner itself should change: a missing measurement, a wrong decision, a rule firing without evidence, a harness problem. Reviewed by people; accepted ones go into the code releases.")}</h3>
  ${L.proposals.map(p => `<div class="card" style="margin-bottom:8px"><div class="row"><b>${esc(p.title)}</b><span class="badge plain">${esc(p.kind || "")}</span><span class="spacer"></span><select data-prop="${p.id}" style="font-size:12px;width:auto;flex:none">${["open", "accepted", "rejected", "done"].map(o => `<option ${o === p.status ? "selected" : ""}>${o}</option>`).join("")}</select></div>
    ${p.proposal ? `<div style="margin:8px 0 0;padding:8px 10px;border-left:3px solid var(--accent);background:var(--soft, rgba(127,127,127,.08));border-radius:4px"><b>Proposed change:</b> ${esc(p.proposal)}${p.expected_effect ? `<div class="muted" style="margin-top:4px;font-size:12.5px">Expected effect: ${esc(p.expected_effect)}</div>` : ""}</div>` : ""}
    <p style="margin:8px 0 0"><span class="muted">${p.proposal ? "Problem: " : ""}</span>${esc(p.body || "")}</p><p class="muted" style="margin:4px 0 0;font-size:12px">${(p.sessions || []).map(t => `<a href="#/session/${t.session}">#${t.session}</a>${t.runs && t.runs.length ? " runs " + t.runs.join(", ") : ""}`).join("; ")}</p></div>`).join("") || '<p class="muted">none yet</p>'}
  <h3>Snapshots${info("A frozen copy of this page's data, kept in the database and downloadable as JSON - for example before the database is reset.")}</h3>
  <div class="card"><div class="row"><input id="snapLabel" placeholder="label (e.g. before reset 2026-10-20)" style="flex:1"><button class="btn" id="snapNow">Save snapshot</button></div>
  ${d.snapshots.length ? `<table style="margin-top:8px"><tbody>${d.snapshots.map(x => `<tr><td>#${x.id}</td><td>${esc(x.label || "")}</td><td class="muted">${new Date(x.created * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC</td><td class="num">${x.runs} runs</td><td><a href="/api/admin/learning/snapshots/${x.id}">download</a></td></tr>`).join("")}</tbody></table>` : ""}</div>
  <h3>Experience by idea and conditions${info("Each experiment is counted for the rule (idea) that proposed it, in the conditions of its run. wins = improved or efficiency gain; losses = regressed or efficiency loss; mean delta = average goal change against the parent configuration.")}</h3>
  <div class="row" style="margin-bottom:6px"><input id="learnFilter" placeholder="filter by idea or workload" value="${esc(filt)}" style="max-width:320px"></div>
  <div class="card tablewrap"><table><thead><tr><th>Idea</th><th>Workload</th><th>CPUs</th><th>Connections</th><th class="num">n</th><th class="num">wins</th><th class="num">losses</th><th class="num">neutral</th><th class="num">mean Δ</th><th>Sessions</th></tr></thead><tbody>
  ${ex.map(e => `<tr><td class="mono">${esc(e.idea)}</td><td>${esc(e.workload)}</td><td>${esc(e.cpu_class)}</td><td>${esc(e.concurrency_class)}</td><td class="num">${e.n}</td><td class="num">${e.wins}</td><td class="num">${e.losses}</td><td class="num">${e.neutral}</td><td class="num">${pct(e.mean_delta_pct)}</td><td style="font-size:12px">${st(e.sessions.slice(-8))}${e.sessions.length > 8 ? " …" : ""}</td></tr>`).join("")}</tbody></table></div>
  <h3>Hypothesis track record${info("For each idea and the component of database time it targeted: supported = goal improved and the targeted time shrank; moved = the targeted time shrank but the goal did not move (that component was not the limit); refuted = neither; improved by another mechanism = goal improved but not through the targeted time.")}</h3>
  <div class="card tablewrap"><table><thead><tr><th>Idea</th><th>Targeted component</th><th class="num">n</th><th class="num">supported</th><th class="num">moved</th><th class="num">refuted</th><th class="num">other mechanism</th></tr></thead><tbody>
  ${d.hypotheses.map(x => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.target)}</td><td class="num">${x.n}</td><td class="num">${x.supported}</td><td class="num">${x.moved}</td><td class="num">${x.refuted}</td><td class="num">${x["improved-other-mechanism"] || 0}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">none yet</td></tr>'}</tbody></table></div>
  <h3>Bottleneck analyses (LLM)${info("Analyses of measured components no rule acted on, cached by the conditions they were made in (server version, component, top stack frames, workload, goal, hardware class, commit / flush settings) and reused for similar conditions.")}</h3>
  ${d.llm_analyses.map(a => `<div class="card" style="margin-bottom:8px"><b>${esc(a.component || "?")}</b> <span class="muted">first in <a href="#/session/${a.first_session}">#${a.first_session}</a>, used ${a.uses}x (${a.reuses} reused), ${esc(a.model || "")}</span>${info("Conditions: " + esc(JSON.stringify(a.conditions)) + (a.frames && a.frames.length ? ". Frames: " + esc(a.frames.join(" < ")) : ""))}
    <p style="margin:6px 0">${esc(a.hypothesis || "")}</p>${a.changes.length ? `<ul style="margin:4px 0 0 18px">${a.changes.map(c => `<li>${esc(c.title || "")}: <span class="mono">${esc((c.changes || []).map(x => x.variable + "=" + x.value).join(", "))}</span> - ${esc(c.mechanism || "")}</li>`).join("")}</ul>` : '<p class="muted" style="margin:0">no configuration change proposed</p>'}</div>`).join("") || '<p class="muted">none yet</p>'}`;
  $view.innerHTML = h;
  document.getElementById("learnFilter").onchange = e => { try { sessionStorage.setItem("learnFilter", e.target.value); } catch (x) { } learningPage(); };
  document.getElementById("pruneNow").onclick = async () => { const r = await api("/api/admin/learner/prune", { method: "POST", body: {} }); alert(`${r.archived} insight(s) archived.`); learningPage(); };
  document.getElementById("learnNow").onclick = async () => { await api("/api/admin/learner/run", { method: "POST", body: {} }); setTimeout(learningPage, 1500); };
  $view.querySelectorAll("select[data-ins]").forEach(el => el.onchange = async () => { await api(`/api/admin/insights/${el.dataset.ins}`, { method: "PATCH", body: { status: el.value } }); });
  $view.querySelectorAll("select[data-prop]").forEach(el => el.onchange = async () => { await api(`/api/admin/proposals/${el.dataset.prop}`, { method: "PATCH", body: { status: el.value } }); });
  document.getElementById("snapNow").onclick = async () => { await api("/api/admin/learning/snapshots", { method: "POST", body: { label: document.getElementById("snapLabel").value } }); learningPage(); };
}

async function policiesPage() {
  setNav("policies");
  const p = await api("/api/policies");
  const vars = Object.entries(p.variables).sort();
  $view.innerHTML = `<h1>Tuning policies</h1><p class="sub">How the agent decides what it may change. Generated from the agent's own rule table.</p>
  <div class="grid g2">${Object.entries(p.policies).map(([k, v]) => `<div class="card"><h3 style="margin-top:0">${esc(k)}</h3><p style="margin:0">${esc(v)}</p></div>`).join("")}</div>
  <h2>What “production-safe” means</h2><p>${esc(p.safe_definition)}</p>
  <h2>Trade-off settings${info("A change is a trade-off when the variable gets one of these values. Under safe they are advice only; under benchmark they are tried after the safe experiments, and runs that use them are flagged and reported separately.")}</h2>
  <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Trade-off when</th><th>Category</th><th>What is given up</th></tr></thead><tbody>
  ${p.tradeoffs.map(t => `<tr><td class="mono">${esc(t.variable)}</td><td>${esc(t.when)}</td><td><span class="badge bad">${esc(t.category)}</span></td><td>${esc(t.lost)}</td></tr>`).join("")}</tbody></table></div>
  <h2>Guards that apply under every policy</h2><ul>${p.guards.map(g => `<li>${esc(g)}</li>`).join("")}</ul>
  <h2>Variables the agent knows</h2><div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Area</th><th>Changed online</th><th>Description</th></tr></thead><tbody>
  ${vars.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td>${esc(v.category)}</td><td>${v.dynamic ? "yes" : "restart"}</td><td>${esc(v.desc)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function accountPage() {
  setNav("account");
  const L = await api("/api/settings/llm");
  $view.innerHTML = `<h1>Account</h1><p class="sub">${esc(ME.email)}</p>
  ${L.admin ? `<div class="card" style="max-width:640px;margin-bottom:14px"><h3 style="margin-top:0">Service AI (OpenRouter)${info("The service's own key: every user's AI assistance (suggestions, bottleneck analyses) and the background learner use it. Users need no key. The tuner asks the model only where it adds value; suggestions are validated and measured like any experiment. The key is stored encrypted on the server and never sent to agents.")}</h3>
   <form id="lf"><label>OpenRouter API key ${L.configured ? '<span class="badge good">configured</span>' : '<span class="badge plain">not set</span>'}</label>
   <input name="key" type="password" placeholder="${L.configured ? "•••••••• (leave empty to keep)" : "sk-or-…"}" autocomplete="off">
   <div class="grid g2"><div><label>Model</label><select name="model" id="lmodel"><option>${esc(L.model)}</option></select></div>
   <div><label>Use the model</label><select name="mode">${L.modes.map(m => `<option value="${m}" ${m === L.mode ? "selected" : ""}>${{ off: "never", "when-needed": "only when it adds value", always: "for every recommendation" }[m]}</option>`).join("")}</select></div></div>
   <div class="err" id="lerr"></div><div class="row"><button class="primary">Save</button>${L.configured ? '<button type="button" class="ghost danger" id="lrm">Remove key</button>' : ""}
   <span class="spacer"></span><span class="muted" style="font-size:12.5px">your sessions: ${L.usage.calls} calls (${L.usage.cached} cached) · $${L.usage.cost_usd}</span></div></form></div>`
  : `<div class="card" style="max-width:640px;margin-bottom:14px"><h3 style="margin-top:0">AI assistance</h3><p style="margin:0">${L.configured && L.mode !== "off" ? 'Provided by the service <span class="badge good">on</span>' : 'Not enabled on this service <span class="badge plain">off</span>'}${info("The service supplies the AI model; you do not need a key. It is used only where it adds value, and every suggestion is validated and measured like any experiment.")}</p>
   <p class="muted" style="margin:6px 0 0;font-size:12.5px">your sessions: ${L.usage.calls} calls (${L.usage.cached} cached) · LLM experiments: ${L.usage.improvements} of ${L.usage.experiments} improved</p></div>`}<div class="card form" style="margin:0;max-width:420px"><h3>Change password</h3>
  <form id="f"><label>Current password</label><input type="password" name="old" required><label>New password</label><input type="password" name="new" required minlength="8">
  <div class="err" id="err"></div><button class="primary">Change password</button></form></div>`;
  if (L.admin) api("/api/llm/models").then(d => { const sel = document.getElementById("lmodel"); if (!sel) return;
    sel.innerHTML = d.models.map(m => `<option value="${esc(m.id)}" ${m.id === L.model ? "selected" : ""}>${esc(m.id)} · $${m.in_per_mtok}/$${m.out_per_mtok} per Mtok</option>`).join(""); }).catch(() => {});
  if (L.admin) document.getElementById("lf").onsubmit = async e => {
    e.preventDefault(); const fd = Object.fromEntries(new FormData(e.target));
    try { await api("/api/settings/llm", { method: "PUT", body: fd }); accountPage(); } catch (err) { document.getElementById("lerr").textContent = err.message; }
  };
  const rm = document.getElementById("lrm"); if (rm) rm.onclick = async () => { if (confirm("Remove the OpenRouter key?")) { await api("/api/settings/llm", { method: "PUT", body: { remove_key: true } }); accountPage(); } };
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    try { await api("/api/auth/password", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); document.getElementById("err").textContent = "Password changed."; }
    catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

function chartsGrid(container, m, defs, window_, markers, opts) {
  // series: [group, key, name] or {name, fn: m => values} for values derived from sampled series
  container.innerHTML = "";
  defs.forEach(def => {
    let series = def.series.map(x => Array.isArray(x) ? { name: x[2], values: (m[x[0]] || {})[x[1]] } : { name: x.name, values: x.fn(m) });
    if (opts && opts.skipEmpty && !series.some(s => s.values && s.values.some(v => v != null))) return;
    // large values in MiB read badly ("30.0k MiB"): switch the whole chart to GiB
    let unit = def.unit;
    const peak = Math.max(0, ...series.flatMap(s => (s.values || []).filter(v => v != null)));
    if ((unit === "MiB" && peak > 4096) || (unit === "MiB/s" && peak > 2048)) {
      unit = unit === "MiB" ? "GiB" : "GiB/s";
      series = series.map(s => ({ name: s.name, values: (s.values || []).map(v => v == null ? v : v / 1024) }));
    }
    const cell = document.createElement("div"); cell.className = "card";
    container.appendChild(cell);
    C.lineChart(cell, { title: def.title, unit, ts: m.ts, window: window_, max100: def.max100, min0: def.min0, markers, series });
    const t = cell.querySelector(".title");
    if (def.tip && t) t.insertAdjacentHTML("beforeend", info(def.tip));
  });
}
// derived series: combine sampled series point by point (no extra sampling)
const ser = (m, g, k) => (m[g] || {})[k] || [];
const combine = (m, f, ...refs) => (m.ts || []).map((_, i) => { const v = refs.map(([g, k]) => ser(m, g, k)[i]); return v.some(x => x == null) ? null : f(...v); });
const perTrx = (m, f, ...refs) => combine(m, (tps, ...v) => tps >= 5 ? f(...v) / tps : null, ["db", "tps"], ...refs);
const LIVE_CHARTS = [
  { title: "Throughput", unit: "per s", series: [["db", "tps", "transactions"], ["db", "qps", "queries"]] },
  { title: "Avg statement latency", unit: "ms", series: [["db", "avg_latency_ms", "avg latency"]] },
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_iowait", "iowait"], ["os", "mysqld_cpu_pct", "mysqld process"]] },
  { title: "Disk IOPS", unit: "per s", series: [["os", "disk_r_iops", "reads"], ["os", "disk_w_iops", "writes"]] },
];
const LIVE_MEM = { title: "Database process memory", unit: "MiB", series: [["os", "dbproc_rss_mb", "RSS"], ["os", "dbproc_pss_mb", "PSS"], ["os", "dbproc_swap_pss_mb", "swap PSS"]] };
const LIVE_SCHED = { title: "Scheduling delay", unit: "µs per CPU slice", series: [["os", "dbproc_sched_delay_us", "database threads"], ["os", "host_sched_delay_us", "all tasks"]] };
const LIVE_THREADS = { title: "Threads", unit: "", series: [["db", "threads_running", "running"], ["db", "threads_connected", "connected"]] };
const LIVE_PSI = { title: "Pressure stall (host, some)", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "CPU"], ["os", "psi_io_some", "I/O"], ["os", "psi_memory_some", "memory"]] };
const RUN_CHARTS = [
  ...LIVE_CHARTS,
  LIVE_THREADS,
  { title: "Buffer pool reads", unit: "pages/s", series: [["db", "bp_disk_reads", "from disk"]] },
  { title: "Buffer pool hit ratio", unit: "%", min0: false, series: [["db", "bp_hit_pct", "hit ratio"]] },
  { title: "Redo checkpoint age", unit: "% of capacity", max100: true, series: [["db", "checkpoint_age_pct", "checkpoint age"], ["db", "bp_dirty_pct", "dirty pages %"]] },
  { title: "Redo written", unit: "MiB/s", series: [["db", "redo_mbs", "redo"]] },
  { title: "fsyncs", unit: "per s", series: [["db", "data_fsyncs", "data fsyncs"], ["db", "log_fsyncs", "log fsyncs"]] },
  LIVE_SCHED,
  { title: "Run-queue wait", unit: "% of runnable time", series: [["os", "dbproc_runq_wait_pct", "database threads"], ["os", "host_runq_wait_pct", "all tasks"]] },
  { title: "Swap traffic", unit: "MiB/s", series: [["os", "swap_in_mbs", "swap-in"], ["os", "swap_out_mbs", "swap-out"]] },
  { title: "Swap used", unit: "MiB", series: [["os", "swap_used_mb", "host"], ["os", "dbproc_swap_mb", "database process(es)"]] },
  { title: "Database process memory", unit: "MiB", series: [["os", "dbproc_rss_mb", "RSS"], ["os", "dbproc_pss_mb", "PSS"], ["os", "dbproc_pss_shmem_mb", "PSS shared memory"], ["os", "dbproc_swap_pss_mb", "swap PSS"], ["os", "dbproc_cg_mem_mb", "cgroup charge"]] },
  { title: "Disk throughput", unit: "MiB/s", series: [["os", "disk_r_mbs", "read"], ["os", "disk_w_mbs", "write"]] },
  { title: "Disk utilisation / latency", unit: "", series: [["os", "disk_util", "util %"], ["os", "disk_r_await_ms", "read ms"], ["os", "disk_w_await_ms", "write ms"]] },
  { title: "Row lock waits", unit: "per s", series: [["db", "row_lock_waits", "waits"]] },
  { title: "Pressure stall: CPU", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "host some"], ["os", "mysqld_psi_cpu_some", "mysqld some"]] },
  { title: "Pressure stall: I/O", unit: "% time", max100: true, series: [["os", "psi_io_some", "host some"], ["os", "psi_io_full", "host full"], ["os", "mysqld_psi_io_some", "mysqld some"], ["os", "mysqld_psi_io_full", "mysqld full"]] },
  { title: "Pressure stall: memory", unit: "% time", max100: true, series: [["os", "psi_memory_some", "host some"], ["os", "psi_memory_full", "host full"], ["os", "mysqld_psi_memory_some", "mysqld some"], ["os", "mysqld_psi_memory_full", "mysqld full"]] },
  { title: "Memory", unit: "%", max100: true, series: [["os", "mem_used_pct", "used"]] },
  { title: "Swap", unit: "pages/s", series: [["os", "swap_in", "in"], ["os", "swap_out", "out"]] },
  { title: "Statements by type", unit: "per s", series: [["db", "selects", "select"], ["db", "inserts", "insert"], ["db", "updates", "update"], ["db", "deletes", "delete"]] },
  { title: "History list length", unit: "", series: [["db", "history_list_length", "undo history"]] },
  { title: "Temp tables on disk", unit: "per s", series: [["db", "tmp_disk_tables", "on disk"], ["db", "tmp_tables", "all"]] },
  { title: "Network", unit: "MiB/s", series: [["os", "net_rx_mbs", "rx"], ["os", "net_tx_mbs", "tx"]] },
  { title: "TCP connections", unit: "per s", series: [["os", "net_conn_per_s", "new connections"], ["os", "net_listen_overflows", "listen overflows"], ["os", "net_resets_per_s", "resets"]] },
  { title: "Client RTT (MySQL connections)", unit: "ms", series: [["os", "rtt_avg_ms", "avg"], ["os", "rtt_p95_ms", "p95"]] },
  { title: "Packet processing", unit: "%", series: [["os", "softirq_max_cpu_pct", "busiest CPU softirq"], ["os", "softirq_avg_cpu_pct", "avg CPU softirq"], ["os", "net_retrans_pct", "TCP retransmits"]] },
  { title: "Context switches", unit: "per s", series: [["os", "ctx_switches", "ctx switches"]] },
];

async function envPage(id) {
  setNav("envs");
  const d = await api("/api/environments/" + id);
  const e = d.environment, f = d.facts || {}, hw = f.hardware || {}, db = f.db || {};
  const kv = db.key_variables || {};
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> /</div>
  <div class="row"><h1>${esc(e.name)}</h1>${envStatus(e)}<span class="spacer"></span>
  <button class="ghost owner" id="share">Share</button><button class="ghost" id="rename">Rename</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">${esc(e.db_short || e.db)} · ${esc(hw.hostname || "")} · agent ${esc(e.agent_version || "")}</p>
  ${d.activity ? `<div class="card" style="margin-bottom:10px;padding:10px 14px"><span class="muted" style="font-size:12px">Now${d.activity.session_id ? `: session <a href="#/session/${d.activity.session_id}">#${d.activity.session_id} ${esc(d.activity.session_name || "")}</a>` : ""}</span>${activityLine(d.activity)}</div>` : ""}
  <div class="card"><div class="row" style="gap:6px 18px">
    <span><b>${hw.cpus} vCPU</b> <span class="muted">${esc((hw.cpu_model || "").replace(/\s+\d+-Core Processor/, "").replace(/\(R\)|\(TM\)/g, ""))}</span></span>
    <span><b>${bytes(hw.ram_bytes)}</b> RAM</span>
    <span>${hw.disk_rotational ? "HDD" : "SSD"} ${esc(hw.filesystem || "")} · ${bytes(hw.disk_free_bytes)} free</span>
    <span>${esc((hw.os || "").replace(/ LTS$/, ""))}</span>
    <span><b>${esc(e.db_short || e.db)}</b></span>
    <span>data ${bytes(db.data_bytes)}</span>
    <span>buffer pool ${bytes(kv.innodb_buffer_pool_size)}</span>
    ${d.steal ? `<span data-tip="CPU time the hypervisor gave to other virtual machines, from the agent's 10 s samples of the last 7 days (${d.steal.samples} samples): mean ${d.steal.mean}%, median ${d.steal.p50}%, p99 ${d.steal.p99}%, max ${d.steal.max}%; ${d.steal.pct_over_1}% of samples above 1%. Runs are flagged when steal could move the result by a quarter of the session's noise threshold (at least 0.25%) or a sample reaches 5%, and excluded from half the threshold." tabindex="0">steal ${d.steal.mean < 0.05 ? "≈ 0" : d.steal.mean + "%"} <span class="muted">(p99 ${d.steal.p99}%)</span></span>` : ""}
    ${(d.hosts || []).filter(h => !h.destroyed_at).length ? `<a href="#" id="clientsJump">+ ${d.hosts.filter(h => !h.destroyed_at).length} client host${d.hosts.filter(h => !h.destroyed_at).length > 1 ? "s" : ""}</a>` : ""}
    <span class="spacer"></span><a href="#" id="detailsToggle">Details ▸</a></div>
  <div id="envDetails" hidden style="margin-top:12px">
  <div class="grid g2"><div><h3 style="margin-top:0">Hardware &amp; OS</h3><div class="kv">
   <div>CPU</div><div>${hw.cpus} × ${esc(hw.cpu_model)}</div><div>Memory</div><div>${bytes(hw.ram_bytes)} (swap ${bytes(hw.swap_bytes)})</div>
   <div>Data disk</div><div>${esc(hw.data_device)} · ${hw.disk_rotational ? "rotational" : "SSD/NVMe"} · ${esc(hw.filesystem)} · scheduler ${esc(hw.disk_scheduler)}</div>
   <div>Disk space</div><div>${bytes(hw.disk_free_bytes)} free of ${bytes(hw.disk_total_bytes)}</div><div>OS</div><div>${esc(hw.os)} · kernel ${esc(hw.kernel)}</div>
   <div>Platform</div><div>${esc(hw.vendor || "")}</div>
   ${Object.entries(f.os || {}).filter(([k]) => KEY_VARS.includes(k)).map(([k, v]) => `<div class="mono">${esc(k.slice(3))}</div><div class="mono">${esc(v)}</div>`).join("")}
   <div>OS tuning</div><div>${(f.agent || {}).os_tuning === false ? "disabled" : "enabled"}</div></div></div>
   <div><h3 style="margin-top:0">Database</h3><div class="kv"><div>Server</div><div>${esc(db.version_comment)} ${esc(db.version)}</div>
   <div>Data</div><div>${bytes(db.data_bytes)} in ${db.tables} InnoDB tables</div><div>Datadir</div><div class="mono">${esc(db.datadir)}</div>
   ${Object.entries(kv).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(val(k, v))}</div>`).join("")}</div></div></div>
  </div></div>
  ${(d.hosts || []).length ? `<h2 id="clientHosts">Client hosts${info("Application or benchmark hosts running the tuning-agent in client role. They report OS and network metrics and accept OS/network setting changes from the session driver.")}</h2>
   <div class="card tablewrap"><table><thead><tr><th>Host</th><th>Status</th><th>Hardware</th><th class="num">RTT to DB</th><th class="num">Connections/s</th><th class="num">TIME_WAIT</th><th class="num">CPU</th></tr></thead><tbody>
   ${d.hosts.map(h => { const o = (h.latest && h.latest.os) || {}, hw = (h.facts || {}).hardware || {};
     return `<tr><td><b>${esc(h.name)}</b></td><td>${h.destroyed_at ? `<span class="badge plain" data-tip="Machine deleted ${ago(h.destroyed_at)}; its metrics stay with the runs.">decommissioned</span>` : h.online ? '<span class="badge good">online</span>' : `<span class="badge warn">offline · ${ago(h.last_seen)}</span>`}</td>
       <td>${hw.cpus || "?"} CPU · ${bytes(hw.ram_bytes)}</td><td class="num">${o.rtt_avg_ms != null ? num(o.rtt_avg_ms) + " ms" : "–"}</td>
       <td class="num">${num(o.net_conn_per_s)}</td><td class="num">${num(o.net_time_wait)}</td><td class="num">${o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–"}</td></tr>`; }).join("")}</tbody></table></div>` : ""}
  <div class="card" style="margin-top:10px"><details><summary style="margin:0">Current agent-applied configuration (${Object.keys((f.agent || {}).managed || {}).length} settings)${info("The settings the tuning-agent currently enforces on this server: MySQL variables written to its own option file (shown below) and operating-system settings. Everything not listed is the server default or comes from your own option files. As of the agent's last report.")}</summary>${Object.keys((f.agent || {}).managed || {}).length ? `<p class="muted" style="margin:0 0 6px">${esc((f.agent || {}).cnf_path || "")} · restarts ${(f.agent || {}).allow_restart === false ? "disabled" : "allowed"}</p>${configBlocks(f.agent.managed)}` : '<span class="muted">None yet: <code>tuning-agent baseline --apply</code> writes the first set.</span>'}</details></div>
  <div class="row"><h2>Live metrics</h2><span class="spacer"></span><select id="range" style="width:auto"><option value="900">15 min</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">24 hours</option></select></div>
  <div id="live"></div>
  <h2>Tuning sessions</h2><div class="card tablewrap"><table data-opt="Goal|Policy|Runs|First run|Best|Started"><thead><tr><th>#</th><th>Session</th><th>Goal</th><th>Policy</th><th>Status</th><th class="num">Runs</th><th class="num">First run</th><th class="num">Best</th><th class="num">Best gain</th><th>Started</th></tr></thead><tbody id="sess"></tbody></table></div>
  <h2>Recent activity</h2><div class="card" id="events"></div>`;
  const tb = document.getElementById("sess");
  if (!d.sessions.length) tb.innerHTML = `<tr><td colspan="10" class="muted">No sessions yet. Start one with <code>tuning-agent session start --goal tps</code>.</td></tr>`;
  d.sessions.forEach(s => {
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/session/" + s.id;
    tr.innerHTML = `<td class="muted">${s.id}</td><td><a href="#/session/${s.id}">${esc(s.name)}</a></td><td>${goalName(s.goal)}</td><td>${esc(s.policy)}</td><td>${statusBadge(s.status)}</td><td class="num">${s.runs}</td>
     <td class="num">${num(s.first_value)}</td><td class="num">${num(s.best_value)}</td><td class="num ${deltaCls(s.best_gain_pct)}">${pct(s.best_gain_pct)}</td><td>${fmtTime(s.created)}</td>`;
    tb.appendChild(tr);
  });
  renderEvents(document.getElementById("events"), null, false, { env: id });
  document.getElementById("share").onclick = ev => sharePanel("environment", id, ev.target);
  document.getElementById("rename").onclick = async () => { const n = prompt("Environment name", e.name); if (n) { await api("/api/environments/" + id, { method: "PATCH", body: { name: n } }); envPage(id); } };
  const cj = document.getElementById("clientsJump"); if (cj) cj.onclick = ev => { ev.preventDefault(); document.getElementById("clientHosts").scrollIntoView({ behavior: "smooth" }); };
  document.getElementById("detailsToggle").onclick = ev => { ev.preventDefault(); const d = document.getElementById("envDetails"); d.hidden = !d.hidden; ev.target.textContent = d.hidden ? "Details ▸" : "Details ▾"; };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this environment with all sessions, runs and metrics? The agent will re-register it on its next report.")) { await api("/api/environments/" + id, { method: "DELETE" }); location.hash = "#/"; } };
  const sessionMarkers = [];
  d.sessions.forEach(x => { sessionMarkers.push({ t: x.created, label: `#${x.id} start` }); if (x.ended) sessionMarkers.push({ t: x.ended, label: `#${x.id} end`, end: true }); });
  const live = document.getElementById("live"), range = document.getElementById("range");
  const cpus = hw.cpus || 1;
  const load = async () => {
    const span = +range.value; const now = Date.now() / 1000;
    const [m, w] = await Promise.all([api(`/api/environments/${id}/metrics?start=${now - span}&end=${now}&points=240`), api(`/api/environments/${id}/waits?start=${now - span}&end=${now}`)]);
    m._waits = w;
    renderLive(live, m, cpus, e.name, sessionMarkers);
  };
  range.onchange = load;
  await load();
  timers.push(setInterval(load, 15000));
}

const LIVE_DB = [
  { title: "Throughput", unit: "per s", series: [["db", "tps", "transactions"], ["db", "qps", "queries"]] },
  { title: "Avg statement latency", unit: "ms", series: [["db", "avg_latency_ms", "avg latency"]] },
  { title: "Statement mix", unit: "per s", series: [["db", "selects", "SELECT"], ["db", "inserts", "INSERT"], ["db", "updates", "UPDATE"], ["db", "deletes", "DELETE"]] },
  { title: "Commits and rollbacks", unit: "per s", series: [["db", "commits", "commits"], ["db", "rollbacks", "rollbacks"]] },
  { title: "Threads", unit: "", series: [["db", "threads_running", "running"], ["db", "threads_connected", "connected"]], tip: "Running threads above the CPU count usually mean contention or waiting; connected minus running = idle connections." },
  { title: "Wait time by class", unit: "threads waiting", series: [["db", "wait_table_io_s", "table I/O"], ["db", "wait_data_io_s", "data file I/O"], ["db", "wait_redo_io_s", "redo I/O"], ["db", "wait_binlog_io_s", "binlog I/O"], ["db", "wait_sync_s", "mutex / rwlock"], ["db", "wait_table_lock_s", "table locks"], ["db", "wait_metadata_lock_s", "metadata locks"], ["db", "wait_socket_s", "socket"], ["db", "wait_other_file_io_s", "other files"]],
    tip: "performance_schema wait time per second = average number of threads waiting on that class. Table I/O is time in storage-engine row calls (including buffer pool hits). Mutex / rwlock waits are only timed when enabled (tuning-agent diag waits on)." },
  { title: "mysqld CPU per transaction", unit: "ms", series: [{ name: "CPU ms / trx", fn: m => perTrx(m, c => c * m._cpus * 10, ["os", "mysqld_cpu_pct"]) }],
    tip: "Derived from sampled mysqld CPU and transactions/s (periods under 5 trx/s left out). Lower = more efficient; the tuner's efficiency verdicts use it." },
  { title: "I/O per transaction", unit: "", series: [
      { name: "disk IOs", fn: m => perTrx(m, (r, w) => r + w, ["os", "disk_r_iops"], ["os", "disk_w_iops"]) },
      { name: "redo KiB", fn: m => perTrx(m, r => r * 1024, ["db", "redo_mbs"]) }],
    tip: "Disk I/O requests and redo written per transaction (periods under 5 trx/s left out)." },
  { title: "Buffer pool hit ratio", unit: "%", min0: false, series: [["db", "bp_hit_pct", "hit ratio"]] },
  { title: "Buffer pool I/O", unit: "pages/s", series: [["db", "bp_disk_reads", "read from disk"], ["db", "bp_pages_flushed", "flushed"], ["db", "bp_wait_free", "waits for a free page"]],
    tip: "Waits for a free page mean user threads had to flush pages themselves: page cleaners cannot keep up." },
  { title: "Buffer pool content", unit: "% of pages", max100: true, series: [["db", "bp_dirty_pct", "dirty"], ["db", "bp_free_pct", "free"]] },
  { title: "Redo log", unit: "", series: [["db", "redo_mbs", "written MiB/s"], ["db", "checkpoint_age_pct", "checkpoint age %"], ["db", "log_waits", "log buffer waits/s"]],
    tip: "Checkpoint age near 75-80% of capacity forces synchronous flushing (stalls). Log buffer waits mean innodb_log_buffer_size is too small." },
  { title: "InnoDB file I/O", unit: "per s", series: [["db", "data_reads", "reads"], ["db", "data_writes", "writes"], ["db", "data_fsyncs", "fsyncs"]] },
  { title: "Locks and purge", unit: "", series: [["db", "row_lock_waits", "row lock waits/s"], ["db", "row_lock_current_waits", "waiting now"]],
    tip: "Row lock waits are a workload property (hot rows) more than a configuration one." },
  { title: "Purge lag (history list length)", unit: "undo records", series: [["db", "history_list_length", "history list"]],
    tip: "Undo records not yet purged. Growth means long transactions or purge falling behind: reads get slower as versions pile up." },
  { title: "Query plan warnings", unit: "per s", series: [["db", "select_scan", "full table scans"], ["db", "select_full_join", "joins without index"], ["db", "sort_merge_passes", "sort merge passes"]],
    tip: "Full scans and joins without an index are query/schema issues; sort merge passes mean sorts spill to disk (sort_buffer_size)." },
  { title: "Rows read sequentially", unit: "rows/s", series: [["db", "handler_read_rnd_next", "rows scanned"]] },
  { title: "Temporary tables", unit: "per s", series: [["db", "tmp_tables", "created"], ["db", "tmp_disk_tables", "on disk"]],
    tip: "On-disk temporary tables are slow: raise tmp_table_size / temptable_max_ram when this is a large share of all temporary tables." },
  { title: "Cache misses", unit: "per s", series: [["db", "opened_tables", "tables opened"], ["db", "opened_table_defs", "table definitions opened"], ["db", "threads_created", "threads created"]],
    tip: "Should be near zero in steady state; otherwise table_open_cache, table_definition_cache or thread_cache_size are too small." },
  { title: "Client traffic (MySQL protocol)", unit: "MiB/s", series: [["db", "net_received_mbs", "received"], ["db", "net_sent_mbs", "sent"]] },
  { title: "Errors and spills", unit: "per s", series: [["db", "aborted_connects", "aborted connects"], ["db", "slow_queries", "slow queries"], ["db", "binlog_cache_disk_use", "binlog cache spills"]] },
];
const LIVE_DBPROC = [
  { title: "mysqld CPU", unit: "% of host", series: [["os", "mysqld_cpu_pct", "mysqld"]] },
  { title: "Database process memory", unit: "MiB", series: [["os", "dbproc_rss_mb", "RSS"], ["os", "dbproc_pss_mb", "PSS"], ["os", "dbproc_swap_pss_mb", "swap PSS"], ["os", "dbproc_cg_mem_mb", "cgroup charge"]],
    tip: "PSS splits shared pages between the processes that map them: the real share of RAM. The cgroup charge also includes page cache charged to the service." },
  { title: "Scheduling delay", unit: "µs per CPU slice", series: [["os", "dbproc_sched_delay_us", "database threads"], ["os", "host_sched_delay_us", "all tasks"]],
    tip: "Time a runnable database thread waited for a CPU each time it was scheduled; adds directly to query latency." },
  { title: "Run-queue wait", unit: "% of runnable time", series: [["os", "dbproc_runq_wait_pct", "database threads"]] },
  { title: "Pressure stall (mysqld cgroup)", unit: "% time", max100: true, series: [["os", "mysqld_psi_cpu_some", "CPU"], ["os", "mysqld_psi_io_some", "I/O"], ["os", "mysqld_psi_memory_some", "memory"]] },
  { title: "Preemptions and faults", unit: "per s", series: [["os", "dbproc_nvcsw_per_s", "involuntary switches"], ["os", "dbproc_pgmajfault_per_s", "major faults"]],
    tip: "Involuntary switches: threads preempted while running. Major faults on a database with O_DIRECT data files are mostly swap-ins." },
];
const LIVE_OS = [
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_iowait", "iowait"], ["os", "cpu_steal", "steal"]] },
  { title: "Load", unit: "", series: [["os", "load1", "load 1m"], ["os", "procs_running", "running"], ["os", "procs_blocked", "blocked on I/O"]] },
  { title: "Pressure stall (host)", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "CPU"], ["os", "psi_io_some", "I/O"], ["os", "psi_memory_some", "memory"]] },
  { title: "Disk IOPS", unit: "per s", series: [["os", "disk_r_iops", "reads"], ["os", "disk_w_iops", "writes"]] },
  { title: "Disk throughput", unit: "MiB/s", series: [["os", "disk_r_mbs", "read"], ["os", "disk_w_mbs", "write"]] },
  { title: "Disk latency and queue", unit: "", series: [["os", "disk_r_await_ms", "read ms"], ["os", "disk_w_await_ms", "write ms"], ["os", "disk_flush_await_ms", "flush ms"], ["os", "disk_queue", "queue depth"], ["os", "disk_util", "util %"]] },
  { title: "Memory", unit: "MiB", series: [["os", "mem_available_mb", "available"], ["os", "page_cache_mb", "page cache"], ["os", "inactive_anon_mb", "inactive anon"]] },
  { title: "Swap", unit: "", series: [["os", "swap_used_mb", "used MiB"], ["os", "swap_in_mbs", "in MiB/s"], ["os", "swap_out_mbs", "out MiB/s"]] },
  { title: "Network", unit: "MiB/s", series: [["os", "net_rx_mbs", "received"], ["os", "net_tx_mbs", "sent"]] },
  { title: "TCP", unit: "", series: [["os", "net_conn_per_s", "connections/s"], ["os", "net_retrans_pct", "retransmits %"], ["os", "net_time_wait", "TIME_WAIT"]] },
  { title: "Packet processing", unit: "", series: [["os", "softirq_max_cpu_pct", "busiest CPU softirq %"], ["os", "softirq_avg_cpu_pct", "avg softirq %"], ["os", "net_softnet_drops", "drops/s"]] },
  { title: "Context switches", unit: "per s", series: [["os", "ctx_switches", "switches"]] },
];
const LIVE_CLIENT = [
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_steal", "steal"]] },
  { title: "Round trip to the database", unit: "ms", series: [["os", "rtt_avg_ms", "avg"], ["os", "rtt_p95_ms", "p95"]] },
  { title: "Network", unit: "MiB/s", series: [["os", "net_rx_mbs", "received"], ["os", "net_tx_mbs", "sent"]] },
  { title: "TCP", unit: "", series: [["os", "net_conn_per_s", "connections/s"], ["os", "net_retrans_pct", "retransmits %"], ["os", "net_time_wait", "TIME_WAIT"]] },
  { title: "Pressure stall", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "CPU"], ["os", "psi_memory_some", "memory"]] },
  { title: "Scheduling delay (all tasks)", unit: "µs per CPU slice", series: [["os", "host_sched_delay_us", "all tasks"]] },
];
function renderLive(el, m, cpus, hostName, markers) {
  m._cpus = cpus;
  let open = {};
  try { open = JSON.parse(localStorage.getItem("liveOpen") || "{}"); } catch (e) { }
  const sections = [["db", `Database: ${hostName}`, LIVE_DB, m], ["proc", `Database process: ${hostName}`, LIVE_DBPROC, m], ["os", `Operating system: ${hostName}`, LIVE_OS, m]];
  (m.clients || []).forEach(c => sections.push(["cli" + c.id, `Client host: ${c.name}`, LIVE_CLIENT, { ts: c.ts, os: c.os, db: {} }]));
  // keep open/closed state across refreshes
  const prev = {}; el.querySelectorAll("details[data-k]").forEach(d => { prev[d.dataset.k] = d.open; });
  el.innerHTML = sections.map(([k, title]) => `<details class="livesec" data-k="${k}" ${(k in prev ? prev[k] : open[k] !== false) ? "open" : ""}><summary><h3 style="display:inline">${esc(title)}</h3></summary><div class="grid g3" data-grid="${k}"></div></details>`).join("");
  sections.forEach(([k, , defs, mm]) => chartsGrid(el.querySelector(`[data-grid="${k}"]`), mm, defs, null, markers, { skipEmpty: true }));
  const dbGrid = el.querySelector('[data-grid="db"]');
  if (dbGrid && m._waits && m._waits.events && m._waits.events.length) {
    const card = document.createElement("div"); card.className = "card";
    card.innerHTML = `<div class="chart"><div class="title">Top wait events (selected range)${info("Average number of threads waiting on each performance_schema wait event over the selected range, and its share of all wait time.")}</div>
      <table class="kvt"><tbody>${m._waits.events.slice(0, 10).map(w => `<tr><td class="mono" style="word-break:break-all">${esc(w.event.replace(/^wait\//, ""))}</td><td class="num">${num(w.avg_threads)}</td><td class="num muted">${w.pct}%</td></tr>`).join("")}</tbody></table></div>`;
    dbGrid.insertBefore(card, dbGrid.children[3] || null);
  }
  el.querySelectorAll("details[data-k]").forEach(d => d.addEventListener("toggle", () => {
    open[d.dataset.k] = d.open; try { localStorage.setItem("liveOpen", JSON.stringify(open)); } catch (e) { }
  }));
}

async function renderEvents(el, _events, _all, scope) {
  // scope: {env} or {session}; shows the latest 10, "Show 20 more" fetches older events on demand
  const q = scope && scope.session ? `session=${scope.session}` : `env=${scope && scope.env}`;
  let shown = [], total = 0;
  const head = el.previousElementSibling && el.previousElementSibling.tagName === "H2" ? el.previousElementSibling : null;
  const draw = () => {
    if (head) head.textContent = total > 10 ? `Recent activity (${shown.length} of ${total})` : "Recent activity";
    if (!shown.length) { el.innerHTML = '<span class="muted">No activity yet.</span>'; return; }
    el.innerHTML = shown.map(ev => `<div class="ev"><span class="muted">${fmtTime(ev.ts)}</span><span><span class="badge plain">${esc(ev.type)}</span></span><span>${esc(ev.message)}</span></div>`).join("") +
      (shown.length < total ? `<div style="margin-top:10px"><button class="more">Show ${Math.min(20, total - shown.length)} more</button></div>` : "");
    const b = el.querySelector(".more");
    if (b) b.onclick = async () => { b.disabled = true; const d = await api(`/api/events?${q}&offset=${shown.length}&limit=20`); shown = shown.concat(d.events); total = d.total; draw(); };
  };
  const d = await api(`/api/events?${q}&offset=0&limit=10`);
  shown = d.events; total = d.total; draw();
}

function meanNoise(noise, n1, n2) {
  // noise threshold for two means of n1 and n2 runs (the calibrated threshold is for one run vs one run)
  return noise * Math.sqrt((1 / Math.max(n1, 1) + 1 / Math.max(n2, 1)) / 2);
}
async function sessionPage(id) {
  setNav("envs");
  const d = await api("/api/sessions/" + id);
  const s = d.session, runs = d.runs.filter(r => r.status !== "aborted");
  const done = runs.filter(r => r.goal_value != null && !EXCLUDED.includes(r.verdict));
  const lower = s.goal === "latency";
  const first = done[0];
  // configurations are scored by the mean of all runs measured with them (repeats reduce noise)
  const groups = {};
  done.forEach(r => { const k = JSON.stringify(Object.entries(r.managed || {}).sort()); (groups[k] = groups[k] || []).push(r); });
  const cfgs = Object.values(groups).map(rs => ({ runs: rs, mean: rs.reduce((a, r) => a + r.goal_value, 0) / rs.length }));
  cfgs.sort((a, b) => lower ? a.mean - b.mean : b.mean - a.mean);
  // same rules as the agent (engine.best_config): means are compared with the noise threshold scaled for the number of
  // runs in each mean; within it, a configuration confirmed better than another tied one wins over it, then kept
  // configurations, then the more often measured
  const noiseP = (s.calibration && s.calibration.noise_pct) || s.noise_pct || 2;
  const KEEP = ["improved", "efficiency-gain", "reference"];
  const keyOf = r => JSON.stringify(Object.entries(r.managed || {}).sort());
  if (cfgs.length) {
    const top = cfgs[0];
    const tied = cfgs.filter(c => Math.abs(c.mean - top.mean) / Math.abs(top.mean) * 100 <= meanNoise(noiseP, top.runs.length, c.runs.length));
    const bySeq = {}; done.forEach(r => bySeq[r.seq] = r);
    const over = {};  // configuration -> configurations it was kept over (improved / efficiency gain / accepted practice)
    done.forEach(r => { if (KEEP.slice(0, 2).includes(r.verdict) || (String(r.label || "").startsWith("best-practices") && r.verdict === "neutral"))
      ((r.evaluation || {}).parent_runs || []).forEach(ps => { if (bySeq[ps] && keyOf(bySeq[ps]) !== keyOf(r)) (over[keyOf(r)] = over[keyOf(r)] || new Set()).add(keyOf(bySeq[ps])); }); });
    const beaten = new Set();
    tied.forEach(c => { c.kept = c.runs.some(r => KEEP.includes(r.verdict)); const me = keyOf(c.runs[0]);
      const todo = [...(over[me] || [])]; while (todo.length) { const k = todo.pop(); if (k !== me && !beaten.has(k)) { beaten.add(k); todo.push(...(over[k] || [])); } } });
    // tie-breaker on resources (same rule as the agent): less CPU / I/O per transaction or memory, if > 3% apart
    const eff = {};
    ["mysqld_cpu_ms_per_trx", "disk_ios_per_trx", "disk_kb_per_trx", "db_mem_mb"].forEach(k => {
      const vals = tied.map(c => { const xs = c.runs.map(r => (r.summary || {})[k]).filter(x => x); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null; });
      if (tied.length < 2 || vals.some(x => x == null)) return;
      const lo = Math.min(...vals);
      if (lo > 0 && Math.max(...vals) / lo - 1 > 0.03) tied.forEach((c, i) => eff[keyOf(c.runs[0])] = (eff[keyOf(c.runs[0])] || 0) + vals[i] / lo - 1);
    });
    tied.sort((a, b) => (beaten.has(keyOf(a.runs[0])) - beaten.has(keyOf(b.runs[0]))) || (b.kept - a.kept) || ((eff[keyOf(a.runs[0])] || 0) - (eff[keyOf(b.runs[0])] || 0)) || (b.runs.length - a.runs.length) || (lower ? a.mean - b.mean : b.mean - a.mean));
    cfgs.splice(cfgs.indexOf(tied[0]), 1); cfgs.unshift(tied[0]);
  }
  const safeCfgs = cfgs.filter(c => !(c.runs[0].tradeoffs || []).length);
  const bestSafe = safeCfgs[0], bestTrade = cfgs.find(c => (c.runs[0].tradeoffs || []).length);
  const bestCfg = cfgs[0], best = bestCfg ? bestCfg.runs[bestCfg.runs.length - 1] : null;
  const firstCfg = first ? cfgs.find(c => c.runs.includes(first)) : null;
  const gain = firstCfg && bestCfg && firstCfg.mean ? (lower ? (firstCfg.mean - bestCfg.mean) : (bestCfg.mean - firstCfg.mean)) / firstCfg.mean * 100 : null;
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${d.environment.id}">${esc(d.environment.name)}</a> /</div>
  <div class="row"><h1><span class="muted" style="font-weight:500">#${s.id}</span> ${esc(s.name)}</h1>${statusBadge(s.status)}<span class="spacer"></span><button class="ghost" id="rename">Rename</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">Goal: <b>${goalName(s.goal)}</b> · policy <a href="#/policies"><b>${esc(s.policy)}</b></a>${s.policy === "benchmark" ? " (trade-offs allowed after safe experiments)" : " (production-safe changes only)"} · OS tuning ${s.os_tuning === 0 ? "off" : "on"}${info("When on, the agent may also change operating-system settings (swappiness, transparent huge pages, I/O scheduler, mount options, dirty writeback, memory allocator, huge pages). OS changes are recorded per run and rolled back like MySQL settings. Disable with --no-os-tuning.")} · noise threshold ±${s.noise_pct}% · started ${fmtTime(s.created)}${s.workload ? " · workload: " + esc(s.workload) : ""}</p>
  ${s.activity ? `<div style="margin:-4px 0 10px">${activityLine(s.activity)}</div>` : ""}
  ${(d.requirements || []).length ? `<p class="sub" style="margin-top:-6px">Benchmark prerequisites${info(REQ_TIP)}: ${d.requirements.map(q => `<span class="mono">${esc(q.variable)} ${/^\d+$/.test(q.required) ? "≥" : "="} ${esc(q.required)}</span>`).join(" · ")}</p>` : ""}
  ${(() => { const sv = (d.events || []).find(e => e.type === "season"); const x = sv && sv.data; return x ? `<p class="sub" style="margin-top:-6px">Data set seasoned for ${x.hours} h before calibration (${x.iterations} benchmark iterations, ${x.steady ? "throughput steady" : "still trending at the time limit"}${x.trend_pct_per_hour != null ? `, trend ${x.trend_pct_per_hour > 0 ? "+" : ""}${x.trend_pct_per_hour}%/h` : ""})${info("data_state = seasoned: the benchmark ran unmeasured until its throughput stopped trending (steady state of the data set), so the tuning runs that follow change the data little.")}</p>` : ""; })()}
  ${calibrationNote(s)}
  <p class="sub study-line" style="margin-top:-6px">Study${info("A study groups sessions from several environments running the same workload, e.g. to compare hardware or find the smallest hardware that reaches a target.")}: <select id="studySel" style="width:auto"><option value="">none</option></select>${d.study ? ` <a href="#/study/${d.study.id}">open study</a>` : ""}</p>
  <div class="tiles"><div class="tile"><div class="k">Runs measured</div><div class="v">${done.length}</div></div>
   <div class="tile"><div class="k">Start${info(`Starting configuration: first run of the session (${goalUnit(s.goal, s)}).`)}</div><div class="v">${first ? num(first.goal_value) : "–"}</div><div class="d">${first ? esc(first.label) : ""}</div></div>
   ${d.cost ? `<div class="tile"><div class="k">Machine cost${info(`Session wall time ${d.cost.hours} h × $${d.cost.rate_hourly.toFixed(4)}/hour (the environment's price). Per-run costs are shown under each run's time.`)}</div><div class="v">$${d.cost.total.toFixed(2)}</div><div class="d">${d.cost.hours} h</div></div>` : ""}
   ${cfgTile("Best safe", bestSafe, firstCfg, lower, s.goal, false, s)}
   ${bestTrade ? cfgTile("Best with trade-offs", bestTrade, firstCfg, lower, s.goal, true, s) : ""}</div>
  <div class="card" style="margin-top:14px" id="bars"></div>
  <h2>Runs</h2><div class="card tablewrap"><table class="runs" data-opt="TPS|Avg ms|CPU ms/trx|IOs/trx|Time"><thead><tr><th>#</th><th>Run</th><th>Changes</th><th class="num">${goalUnit(s.goal, s) === "ms" ? "Avg ms" : "Goal"}</th><th class="num">Δ parent${info("Change of the goal vs the mean of the parent configuration (the configuration this run was changed from). Coloured only beyond the noise threshold.")}</th><th class="num">vs start${info("Change vs the calibrated starting configuration. Bold: the best configuration known at that point of the session (it took the lead by beating the previous best beyond the noise threshold). Changes shown struck through were rolled back afterwards; ↩ marks settings undone before the run.")}</th><th>Verdict</th>
   <th class="num">TPS</th><th class="num">Avg ms</th><th class="num">CPU ms/trx</th><th class="num">IOs/trx</th><th class="num">Time</th></tr></thead><tbody id="runs"></tbody></table></div>
  <h2>Experiments${info("Your own experiments: try a set of settings, or sweep one variable over several values (e.g. buffer pool 1G, 2G, 3G). They run before the tuner's own ideas, each value measured from the same parent configuration with the session's benchmark. The best value is kept only if it beats the parent beyond the noise threshold.")}</h2><div class="card" id="exps"></div>
  <h2>Latest recommendations</h2><div class="card" id="recs"></div>
  <h2>Notes</h2><div class="card"><textarea id="notes" rows="3" placeholder="Notes about this session (workload, hypotheses, conclusions)">${esc(s.notes || "")}</textarea><div class="row" style="margin-top:6px"><button id="savenotes">Save notes</button><span id="saved" class="muted"></span></div></div>
  <h2>Recent activity</h2><div class="card" id="events"></div>`;
  api("/api/studies").then(st => {
    const sel = document.getElementById("studySel"); if (!sel) return;
    sel.innerHTML = '<option value="">none</option>' + st.studies.map(x => `<option value="${x.id}" ${x.id === s.study_id ? "selected" : ""}>${esc(x.name)}</option>`).join("") + '<option value="new">New study…</option>';
    sel.onchange = async () => {
      let v = sel.value;
      if (v === "new") { const n = prompt("Study name (e.g. HammerDB 200W hardware scaling)"); if (!n) { sel.value = s.study_id || ""; return; } v = (await api("/api/studies", { method: "POST", body: { name: n, goal: s.goal, workload: s.workload } })).id; }
      await api("/api/sessions/" + id, { method: "PATCH", body: { study_id: v ? +v : null } }); sessionPage(id);
    };
  });
  // reference = the calibrated starting configuration (mean of its runs); server defaults are usually far below and
  // percentages against them say little
  const refVal = (s.calibration && s.calibration.goal_mean) || (first ? first.goal_value : null);
  const refName = s.calibration && s.calibration.goal_mean ? "starting config (calibration mean)" : "first run";
  const vsRef = v => refVal ? (lower ? (refVal - v) / refVal : (v - refVal) / refVal) * 100 : null;
  // best configuration known at each point of the session: a configuration takes the lead only when its mean beats the
  // current leader beyond the noise threshold (repeats of the leader keep it)
  const bestAt = {}, leadBefore = {};
  { const acc = {}; let lead = null, leadMean = null, leadManaged = null, leadN = 1;
    d.runs.filter(r => r.status !== "aborted").forEach(r => { leadBefore[r.id] = leadManaged;
      if (r.goal_value == null || EXCLUDED.includes(r.verdict)) return;
      const k = JSON.stringify(Object.entries(r.managed || {}).sort());
      const a = acc[k] = acc[k] || { sum: 0, n: 0 }; a.sum += r.goal_value; a.n += 1;
      const mean = a.sum / a.n;
      const better = leadMean == null || (lower ? (leadMean - mean) / leadMean : (mean - leadMean) / leadMean) * 100 > meanNoise(noiseP, leadN, a.n);
      if (k === lead) { leadMean = mean; leadN = a.n; } else if (better) { lead = k; leadMean = mean; leadN = a.n; leadManaged = r.managed || {}; }
      if (k === lead) leadManaged = r.managed || {};
      bestAt[r.id] = k === lead;
    }); }
  // what happened to each run's own changes afterwards: kept, or rolled back by a later run
  const later = d.runs.filter(x => x.status !== "aborted");
  const fate = (r, c) => {  // kept while every later run still has the value; else rolled back at the first one that changed it
    const mine = String((r.managed || {})[c.variable]);
    const nxt = later.filter(x => x.seq > r.seq);
    const ch = nxt.find(x => String((x.managed || {})[c.variable]) !== mine);
    return ch ? { kept: false, by: ch.seq } : { kept: true, pending: !nxt.length };
  };
  C.barChart(document.getElementById("bars"), {
    title: goalName(s.goal) + " per run" + (goalUnit(s.goal, s) && !goalName(s.goal).toLowerCase().includes(goalUnit(s.goal, s).toLowerCase()) ? " (" + goalUnit(s.goal, s) + ")" : "") + " — green: best production-safe configuration · orange: runs with trade-offs · ▲ best configuration known at that point", unit: goalUnit(s.goal, s), metric: s.goal === "latency" ? "avg latency" : "goal value",
    ref: refVal, refLabel: refName, subLabel: "vs " + refName,
    bars: done.map(r => ({ label: "#" + r.seq + " " + r.label, short: "#" + r.seq, value: r.goal_value, sub: vsRef(r.goal_value) != null ? pct(vsRef(r.goal_value)) : "", href: "#/run/" + r.id, mark: bestAt[r.id],
      highlight: (r.tradeoffs || []).length ? "trade" : bestSafe && bestSafe.runs.includes(r) ? "best" : false,
      tip: `<div>vs previous<b>${pct(r.delta_pct)}</b></div><div>verdict<b>${esc(r.verdict || "")}</b></div>` })),
  });
  function changeLines(r, chs, fate) {
    // undo = a change that only returns a setting to its value before the previous run changed it
    // undo = a change that returns a setting to the best configuration known before this run (reverts, sweep endings)
    const lead = leadBefore[r.id];
    const isUndo = c => c.source === "rollback" || (lead != null && String((r.managed || {})[c.variable]) === String(lead[c.variable]));
    const own = chs.filter(c => !isUndo(c)), undo = chs.filter(isUndo);
    const line = c => {
      // from -> to, so the direction of a numeric change is visible at a glance
      const f = fate(r, c), name = esc(c.variable.replace(/^innodb_/, ""));
      const from = c.old != null && c.old !== "" ? esc(shortVal(c.variable, c.old)) : null, to = esc(shortVal(c.variable, c.new));
      const fd = flagDiff(c.old, c.new);
      const txt = fd ? `${name}: ${fd.map(x => `${esc(x.flag)} <span class="muted">${esc(x.from || "–")}</span> → <b>${esc(x.to || "–")}</b>`).join(", ")}`
        : `${name} ${from != null && from !== to ? `<span class="muted">${from}</span> → ` : "= "}<b>${to}</b>`;
      const tip = `${c.variable}: ${c.old != null ? val(c.variable, c.old) : "?"} → ${val(c.variable, c.new)}`;
      return f.kept ? `<span class="mono chg-line" data-tip="${esc(tip)} · ${f.pending ? "in effect now" : "kept: still in effect in the later runs"}">${txt}</span>`
        : `<span class="mono chg-line rolled" data-tip="${esc(tip)} · rolled back before run #${f.by}">${txt} <span class="undo-tag">undone #${f.by}</span></span>`;
    };
    const parts = [];
    if (own.length > 3) {
      const back = own.filter(c => !fate(r, c).kept).length;
      parts.push(`<span data-tip="${esc(own.map(c => c.variable + "=" + val(c.variable, c.new) + (fate(r, c).kept ? "" : " (rolled back)")).join("\n"))}" tabindex="0">${own.length} settings${back ? ` · <span class="rolled">${back === own.length ? "all" : back} rolled back</span>` : ""}</span>`);
    } else parts.push(...own.map(line));
    if (undo.length) parts.push(`<span class="muted" data-tip="${esc("Undone before this run (returning to a measured configuration):\n" + undo.map(c => c.variable + "=" + val(c.variable, c.new)).join("\n"))}" tabindex="0">↩ undo ${undo.length === 1 ? esc(undo[0].variable.replace(/^innodb_/, "")) : undo.length + " settings"}</span>`);
    return parts.join("<br>") || '<span class="muted">none</span>';
  }
  const tb = document.getElementById("runs");
  if (!d.runs.length) tb.innerHTML = `<tr><td colspan="12" class="muted">No runs yet: <code>tuning-agent measure start</code> … <code>measure stop</code>.</td></tr>`;
  d.runs.forEach(r => {
    const sm = r.summary || {};
    const chs = r.changes || [];
    const chTip = chs.map(c => `${c.variable}=${val(c.variable, c.new)}`).join("\n");
    const ch = r.status === "aborted" ? `<span class="muted">${esc((r.abort_reason || "aborted").split(":")[0])}</span>${r.abort_reason ? info(r.abort_reason) : ""}`
      : r.seq === 1 ? `<a href="#/run/${r.id}">starting configuration</a>`
      : !chs.length ? '<span class="muted">none</span>'
      : changeLines(r, chs, fate);
    const costOf = x => d.cost && d.cost.per_run ? d.cost.per_run[x.id] : null;
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/run/" + r.id;
    const ev = r.evaluation || {};
    tr.innerHTML = `<td>${r.seq}</td><td><a href="#/run/${r.id}">${esc(r.label)}</a>${r.status !== "done" ? " " + statusBadge(r.status, r.abort_reason) : ""}${tradeBadge(r.tradeoffs)}</td><td class="chg">${ch}</td>
     <td class="num"><b>${num(r.goal_value)}</b></td><td class="num ${deltaClsN(ev.goal_delta_pct, noiseP)}">${pct(ev.goal_delta_pct)}</td><td class="num ${deltaClsN(r.goal_value != null ? vsRef(r.goal_value) : null, noiseP)}">${r.goal_value != null && vsRef(r.goal_value) != null ? (bestAt[r.id] ? `<b data-tip="Best configuration known at this point">${pct(vsRef(r.goal_value))}</b>` : pct(vsRef(r.goal_value))) : "–"}</td>
     <td>${verdictBadge(r.verdict)}${ev.not_steady ? ` <span class="badge warn" data-tip="Not in steady state: deferred work was still accumulating at the end of the measured window (${esc(ev.not_steady.join("; "))}). Flushing, purge or change-buffer merges postponed past the window are never paid for, so the result can be optimistic; such improvements do not count as the best configuration." tabindex="0">not steady</span>` : ""}${ev.drift ? ' <span class="badge warn">drift</span>' : ""}${ev.unstable_within_run ? ` <span class="badge warn" data-tip="Throughput fell below half its median in ${ev.unstable_within_run.tput_dip_samples} of ${ev.unstable_within_run.tput_samples} samples inside the window (longest stall ${ev.unstable_within_run.tput_dip_longest_samples} samples, worst sample ${ev.unstable_within_run.tput_min_vs_median_pct}% of the median)${ev.unstable_within_run.blk_write_max_ms != null ? `; slowest write I/O ${ev.unstable_within_run.blk_write_max_ms} ms` : ""}. The average hides these stalls." tabindex="0">stalls</span>` : ""}${ev.swap_hazard ? ` <span class="badge warn" data-tip="mysqld memory (${ev.swap_hazard.dbproc_swap_mb} MB in swap) was read back from swap during the window. A thread waiting for a swap-in while holding a latch or lock stalls everything queued behind it, so configurations without swap-ins are preferred as the best." tabindex="0">swap-in</span>` : ""}${ev.environment_shift && !["reference", "repeat", "unreliable", "superseded"].includes(r.verdict) ? ` <span class="badge warn" data-tip="The quick CPU / storage benchmark run before this run differed from the calibration runs beyond its normal variation (${esc(kvText(ev.environment_shift))} %). The machine itself may have changed (noisy neighbour, throttling), so this run's comparison is less trustworthy; autotune measures such runs again.">env shift</span>` : ""}${ev.steal && ev.steal.level === "flag" ? ` <span class="badge warn" data-tip="Hypervisor CPU steal ${ev.steal.avg_pct}% average, ${ev.steal.peak_pct}% peak${ev.steal.client_avg_pct ? `, client ${ev.steal.client_avg_pct}%` : ""} , estimated effect on the result ${ev.steal.est_impact_pct != null ? ev.steal.est_impact_pct : ev.steal.avg_pct}% (flagged from a quarter of the noise threshold, ${ev.steal.flag_at_pct || 0.25}%, or a 5% peak; normally close to 0 on these hosts): differences near the noise threshold may come from neighbours.">steal</span>` : ""}</td>
     <td class="num">${num(sm.tps)}</td><td class="num">${num(sm.avg_latency_ms)}</td><td class="num">${num(sm.mysqld_cpu_ms_per_trx)}</td><td class="num">${num(sm.disk_ios_per_trx)}</td><td class="num" style="white-space:nowrap">${dur(sm.duration_s || (r.status === "measuring" ? Date.now() / 1000 - r.started : null))}${costOf(r) != null ? `<div class="muted" style="font-size:11px" data-tip="Machine cost of this run: wall time since the previous run ended (data reset, warm-up, measurement) × $${(d.cost.rate_hourly).toFixed(4)}/hour">$${costOf(r).toFixed(costOf(r) < 0.1 ? 3 : 2)}</div>` : ""}</td>`;
    tb.appendChild(tr);
  });
  renderRecs(document.getElementById("recs"), d.recommendations[0], runs);
  renderExperiments(document.getElementById("exps"), s);
  renderEvents(document.getElementById("events"), null, false, { session: id });
  document.getElementById("savenotes").onclick = async () => { await api("/api/sessions/" + id, { method: "PATCH", body: { notes: document.getElementById("notes").value } }); document.getElementById("saved").textContent = "Saved."; };
  document.getElementById("rename").onclick = async () => { const n = prompt("Session name", s.name); if (n) { await api("/api/sessions/" + id, { method: "PATCH", body: { name: n } }); sessionPage(id); } };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this session and its runs?")) { await api("/api/sessions/" + id, { method: "DELETE" }); location.hash = "#/env/" + d.environment.id; } };
  if (runs.some(r => r.status === "measuring") || s.status === "active") timers.push(setInterval(async () => {
    const d2 = await api("/api/sessions/" + id);
    if (JSON.stringify(d2.runs.map(r => [r.id, r.status])) !== JSON.stringify(d.runs.map(r => [r.id, r.status])) || d2.recommendations.length !== d.recommendations.length) { clearTimers(); sessionPage(id); }
  }, 10000));
}

function cfgTile(title, cfg, firstCfg, lower, goal, trade, sess) {
  if (!cfg) return "";
  const g = firstCfg && firstCfg.mean ? (lower ? (firstCfg.mean - cfg.mean) : (cfg.mean - firstCfg.mean)) / firstCfg.mean * 100 : null;
  const runs = cfg.runs.map(r => "#" + r.seq).join(", ");
  const tip = `${cfg.runs.length > 1 ? `Mean of runs ${runs}` : `Run ${runs}`} (${goalUnit(goal, sess)}). Gain is vs the starting configuration.` +
    (trade ? ` Trade-offs: ${(cfg.runs[0].tradeoffs || []).map(t => t.variable + "=" + t.value).join(", ")}.` : "");
  return `<div class="tile"><div class="k">${title}${info(tip)}</div><div class="v">${num(cfg.mean)} <span class="${deltaCls(g)}" style="font-size:14px">${pct(g)}</span></div>
   <div class="d">${cfg.runs.length > 1 ? "runs" : "run"} ${runs}</div></div>`;
}

function tradeBadge(t) {
  if (!t || !t.length) return "";
  return ` <span class="badge bad" title="${esc(t.map(x => x.variable + "=" + x.value + ": " + x.lost).join("\n"))}">trade-off</span>`;
}

function calibrationNote(s) {
  const c = s.calibration;
  if (!c) return `<div class="note" style="margin-bottom:12px">Noise not calibrated: fixed ±${s.noise_pct || 2}% threshold${info("Standard protocol: measure the starting configuration 3 times with `tuning-agent calibrate --runs 3`; the run-to-run variation then sets the noise threshold.")}</div>`;
  const eff = Object.entries(c.efficiency || {}).filter(([k]) => EFF_LABELS[k]).map(([k, e]) => `${EFF_LABELS[k]} ±${e.noise_pct}%`).join(" · ");
  const how = `Calibration runs ${c.runs.map(x => "#" + x).join(", ")}: mean ${num(c.goal_mean)}, CV ${c.goal_cv_pct}%, range ${c.goal_range_pct}%. CV (coefficient of variation) = standard deviation ÷ mean. Threshold = ${c.k || 2.5} × CV: a run of an unchanged configuration lands outside it only about 1 time in 100. Natural variance depends on the environment and the workload, so it is re-estimated during the session from every configuration measured more than once; repeats separated by other runs also capture drift. Until such repeats exist, earlier sessions of the same benchmark on this environment are used, else at least 2%.${c.estimate ? ` Current basis: ${c.estimate.basis}; pooled CV ${c.estimate.pooled_cv_pct}%.` : ""} Experiments are compared with the mean of the most recent runs of the configuration they were changed from. Efficiency thresholds: ${eff}.`;
  const p = c.plan;
  let plan = "";
  if (p && p.within_cv_pct != null) {
    const split = `Run-to-run variation: within-run ${p.within_cv_pct}% · between-run ${p.between_cv_pct}% (CV)`;
    const splitTip = "Within-run: what each run's own 10 s samples predict for the spread of its mean at this run length (batch means of 60 s blocks); it shrinks with longer runs. Between-run: the rest of the observed run-to-run variation — shifts from run to run such as data-set state, restarts, caches or neighbours; longer runs do not reduce it, more runs per configuration or removing the cause do. Target: ±" + p.target_pct + "% (typical OLTP tuning gains are 2-8%).";
    const verdict = p.action === "ok" ? "" : p.action === "longer-runs"
      ? (p.applied ? ` · <b>runs lengthened</b>${p.previous ? ` from ${Math.round(p.previous.run_seconds / 60)} to ${Math.round(p.run_seconds / 60)} min (±${p.previous.noise_pct}% → ±${c.noise_pct}%)` : ""}`
        : ` · <span class="badge warn">noisy</span> mostly within-run: ${Math.round(p.seconds / 60)} min runs would bring it to about ±${p.expected_noise_pct || p.target_pct}%`)
      : p.action === "between-runs" ? ` · <span class="badge warn">noisy</span> mostly between runs: longer runs will not help; changes under ±${c.noise_pct}% cannot be seen (${p.repeats >= 9 ? "9+" : "~" + p.repeats} runs per configuration needed, or remove the run-to-run shifts, e.g. reset the data set before each run)` : "";
    plan = `<div style="margin-top:4px">${split}${info(splitTip + (p.text ? " " + p.text : ""))}${verdict}</div>`;
  }
  const wu = c.warmup;
  if (wu && wu.adequate === false) plan += `<div style="margin-top:4px"><span class="badge warn">warm-up too short</span> ${esc(wu.text)}${wu.applied_warmup_s ? ` Warm-up raised to ${wu.applied_warmup_s} s and calibration repeated.` : ""}</div>`;
  else if (wu && wu.previous) plan += `<div style="margin-top:4px">Warm-up raised from ${wu.previous.warmup_s} s to ${wu.warmup_s} s: ${esc(wu.text)}</div>`;
  const ds = c.data_state;
  if (ds && ds.status === "drifting") plan += `<div style="margin-top:4px"><span class="badge warn">data not restored</span> The benchmark changes data${ds.rows_changed_per_s ? ` (${num(ds.rows_changed_per_s)} rows/s)` : ""} but runs neither restore it nor start from a seasoned steady state${ds.trend_pct_per_run != null ? `; calibration trend ${ds.trend_pct_per_run > 0 ? "+" : ""}${ds.trend_pct_per_run}% per run` : ""}${info("Each run starts from the data the previous runs left behind (row versions, page splits, fragmentation, undo), so differences between runs can come from the data rather than the configuration. Fix in the benchmark profile: a reset command that restores the data set before every run (data_state = reset), or data_state = seasoned: one long pre-run until throughput stops trending, so a series of tuning runs no longer moves the state much. Read-only benchmarks: data_state = read-only.")}</div>`;
  return `<div class="note" style="margin-bottom:12px">Noise: changes within <b>±${c.noise_pct}%</b> are treated as noise (${c.estimate ? `pooled CV ${c.estimate.pooled_cv_pct}% over ${c.estimate.dof} repeat${c.estimate.dof === 1 ? "" : "s"}${c.estimate.long_term_dof ? ", including re-checks over time" : ""}` : `CV ${c.goal_cv_pct}% over ${c.runs.length} runs`})${info(how)}${plan}</div>`;
}
const KEY_VARS = ["innodb_buffer_pool_size", "innodb_buffer_pool_instances", "innodb_redo_log_capacity", "innodb_log_file_size", "innodb_log_buffer_size",
  "innodb_flush_log_at_trx_commit", "sync_binlog", "log_bin", "innodb_doublewrite", "innodb_flush_method", "innodb_io_capacity", "innodb_io_capacity_max",
  "innodb_flush_neighbors", "innodb_use_fdatasync", "innodb_read_io_threads", "innodb_write_io_threads", "innodb_page_cleaners", "innodb_lru_scan_depth",
  "innodb_log_writer_threads", "innodb_adaptive_hash_index", "innodb_change_buffering", "innodb_sync_spin_loops", "innodb_spin_wait_delay",
  "innodb_thread_concurrency", "thread_handling", "thread_pool_size", "max_connections", "thread_cache_size", "table_open_cache", "table_open_cache_instances",
  "table_definition_cache", "tmp_table_size", "max_heap_table_size", "temptable_max_ram", "sort_buffer_size", "join_buffer_size", "read_rnd_buffer_size",
  "binlog_format", "binlog_row_image", "binlog_group_commit_sync_delay", "transaction_isolation", "performance_schema", "innodb_dedicated_server",
  "innodb_read_ahead_threshold", "innodb_stats_persistent_sample_pages", "innodb_page_size", "large_pages",
  "os:vm.swappiness", "os:thp", "os:scheduler", "os:read_ahead_kb", "os:vm.dirty_background_bytes", "os:vm.dirty_bytes", "os:mount_noatime",
  "os:mount_barrier", "os:malloc", "os:hugepages", "os:cpu_governor"];
function sourceText(m) {
  if (!m || !m.source) return "";
  const src = { COMPILED: "compiled default", EXPLICIT: "option file", GLOBAL: "option file", DYNAMIC: "SET GLOBAL at runtime", PERSISTED: "SET PERSIST",
    COMMAND_LINE: "command line", "OS-AGENT": "set by tuning-agent (OS)", OS: "operating system", LOGIN: "login", CLIENT: "client", SERVER: "server-adjusted", "AUTO-CONFIGURED": "auto-sized", CONFIG: "option file", "SQL": "SET GLOBAL" }[m.source] || m.source.toLowerCase();
  return src + (m.path ? " · " + m.path : "");
}
function isDefault(m) { return m && (m.source === "COMPILED" || m.source === "COMPILE-TIME" || m.source === "OS"); }
const WAIT_CLASS_NAMES = { table_io: "table I/O", data_io: "data file I/O", redo_io: "redo I/O", binlog_io: "binlog I/O", sync: "mutex / rwlock", table_lock: "table lock", metadata_lock: "metadata lock", socket: "socket", other_file_io: "other files", other: "other" };
const WAIT_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "#8a6fd1", "#4aa3a3", "#c45c8a", "#7a8a99", "#a0a0a0"];
function waitsTable(inp, r) {
  const dur = (r && r.summary && r.summary.duration_s) || null;
  const cov = inp.wait_instruments;
  const classes = Object.entries(inp.wait_classes || {});
  const tot = classes.reduce((a, [, v]) => a + v, 0) || 1;
  const bar = classes.length ? `<div class="waitbar">${classes.map(([k, v], i) => `<span style="width:${(100 * v / tot).toFixed(2)}%;background:${WAIT_COLORS[i % WAIT_COLORS.length]}" data-tip="${esc(WAIT_CLASS_NAMES[k] || k)}: ${num(v)} s (${(100 * v / tot).toFixed(1)}%)"></span>`).join("")}</div>
    <div class="row" style="gap:4px 14px;font-size:12px;margin:4px 0 8px">${classes.map(([k, v], i) => `<span><i class="dot" style="background:${WAIT_COLORS[i % WAIT_COLORS.length]}"></i>${esc(WAIT_CLASS_NAMES[k] || k)} ${(100 * v / tot).toFixed(0)}%</span>`).join("")}</div>` : "";
  return `<h3>Where threads waited${info("performance_schema wait events during the run window. Threads waiting = total wait time / run duration. Table I/O is time spent in storage-engine row calls (including buffer pool hits), not only disk." +
    (cov ? ` Instrumentation: ${cov.enabled} of ${cov.instruments} wait instruments timed${cov.sync_enabled ? "" : "; mutex/rwlock waits are not timed (enable with tuning-agent diag waits on)"}.` : ""))}</h3>
    <div class="card">${bar}<div class="tablewrap"><table><thead><tr><th>Event</th><th>Class</th><th class="num">% of wait time</th><th class="num">threads waiting</th><th class="num">count</th><th class="num">avg µs</th></tr></thead><tbody>` +
    inp.top_waits.slice(0, 15).map(w => `<tr><td class="mono" style="word-break:break-all">${esc(w.event.replace(/^wait\//, ""))}</td><td>${esc(WAIT_CLASS_NAMES[w.class] || w.class || "")}</td><td class="num">${w.pct != null ? w.pct : "–"}</td><td class="num">${dur ? num(w.time_s / dur) : "–"}</td><td class="num">${num(w.count)}</td><td class="num">${w.avg_us != null ? num(w.avg_us) : num(w.time_s * 1e6 / Math.max(w.count, 1))}</td></tr>`).join("") + "</tbody></table></div></div>";
}
function cnfBlock(managed) {
  // MySQL option file only: can be copied into my.cnf as is
  const my = Object.entries(managed || {}).filter(([k]) => !/^(os|rg|client):/.test(k));
  return my.length ? `[mysqld]\n${my.map(([k, v]) => esc(k) + (v === "" ? "" : " = " + esc(v))).join("\n")}` : "";
}
function otherChanges(managed) {
  const e = Object.entries(managed || {});
  return [
    ["Operating system (database host)", e.filter(([k]) => k.startsWith("os:")), k => k.slice(3)],
    ["MySQL resource groups (runtime)", e.filter(([k]) => k.startsWith("rg:")), k => k.slice(3)],
    ["Operating system (client hosts)", e.filter(([k]) => k.startsWith("client:")), k => k.replace(/^client:os:/, "")],
  ].filter(g => g[1].length);
}
function reqTable(reqs) {
  // benchmark prerequisites: settings the benchmark needs to run at all (not tuning)
  return `<table class="kvt"><tbody>${reqs.map(q => `<tr><td class="mono">${esc(q.variable)}</td><td class="mono">${q.raised ? `${esc(q.server)} → ` : ""}${esc(q.required)}${/^\d+$/.test(q.required) ? '<span class="muted"> min</span>' : ""}</td><td>${q.error ? `<span class="badge warn" data-tip="${esc(q.error)}">not set</span>` : q.raised ? '<span class="muted">raised before the run</span>' : '<span class="muted">already met</span>'}</td></tr>`).join("")}</tbody></table>`;
}
const REQ_TIP = "Settings the benchmark needs just to run (for example max_connections for 1024 client threads, or max_prepared_stmt_count for 64 threads × 32 tables of prepared statements). Declared in the benchmark profile, applied before every run including the server-defaults run (numbers are minimums), and never changed by the tuner, so they are not part of the tuning result.";
function configBlocks(managed, reqs) {
  // side by side: the copyable option file, and everything that is not MySQL configuration
  const my = cnfBlock(managed), other = otherChanges(managed);
  reqs = reqs || [];
  const right = other.map(([h, rows, name]) => `<h4 style="margin:0 0 4px">${h}</h4><table class="kvt"><tbody>${rows.map(([k, v]) => `<tr><td class="mono">${esc(name(k))}</td><td class="mono">${esc(v)}</td></tr>`).join("")}</tbody></table>`).join('<div style="height:10px"></div>');
  const reqBox = reqs.length ? `<div class="card" style="padding:10px 12px;margin-top:10px"><h4 style="margin:0 0 4px">Benchmark prerequisites${info(REQ_TIP)}</h4>${reqTable(reqs)}</div>` : "";
  if (!other.length && !reqs.length) return `<pre><code>${my}</code></pre>`;
  if (!other.length) return `<div class="cfgsplit"><div><div class="muted" style="margin-bottom:4px">MySQL option file${info("Can be copied into a my.cnf [mysqld] section as is.")}</div><pre style="margin:0"><code>${my || "# no MySQL settings"}</code></pre></div><div>${reqBox.replace('margin-top:10px', 'margin-top:22px')}</div></div>`;
  return `<div class="cfgsplit"><div><div class="muted" style="margin-bottom:4px">MySQL option file${info("Can be copied into a my.cnf [mysqld] section as is.")}</div><pre style="margin:0"><code>${my || "# no MySQL settings"}</code></pre></div>
    <div><div class="muted" style="margin-bottom:4px">Other changes${info("Settings outside the MySQL option file: kernel and filesystem settings of the database host, MySQL runtime objects, and client-host settings. The agent applies and persists them itself.")}</div><div class="card" style="padding:10px 12px">${right}</div>${reqBox}</div></div>`;
}
async function renderKeySettings(el, runId) {
  const c = await api(`/api/runs/${runId}/config`);
  const meta = c.meta || {};
  if (!Object.keys(meta).length) { el.innerHTML = ""; return; }
  const rows = KEY_VARS.filter(k => k in c.config && !/^(os|rg):/.test(k)).map(k => {
    const m = meta[k] || {}, def = isDefault(m);
    return `<tr><td class="mono">${esc(k)}</td><td class="mono ${def ? "" : "diff-new"}">${esc(val(k, c.config[k]))}</td><td class="mono muted">${m.default != null ? esc(val(k, m.default)) : ""}</td><td>${def ? '<span class="muted">compiled default</span>' : esc(sourceText(m))}</td></tr>`;
  }).join("");
  const nonDef = Object.entries(meta).filter(([k, m]) => !isDefault(m) && !KEY_VARS.includes(k)).length;
  const osKeys = Object.keys(c.config).filter(k => /^(os|rg):/.test(k)).sort();
  const managedNow = c.managed || {};
  const osTable = osKeys.length ? `<h3>Operating system &amp; runtime settings of this run${info("Values in effect on the database host while this run was measured. Highlighted = set by the tuning-agent; the rest are the host's own values.")}</h3>
    <div class="tablewrap"><table><tbody>${osKeys.map(k => `<tr><td class="mono">${esc(k.replace(/^os:/, ""))}</td><td class="mono ${k in managedNow ? "diff-new" : ""}">${esc(c.config[k])}</td><td>${k in managedNow ? "tuning-agent" : '<span class="muted">host</span>'}</td></tr>`).join("")}</tbody></table></div>` : "";
  el.innerHTML = `<h3>Key settings of this run${info(`Value in effect, the server's compiled default and where the value comes from (option files, SET PERSIST, runtime changes). Bold = not the compiled default. ${nonDef} more non-default variables are in the Full configuration tab.`)}</h3>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Value</th><th>Compiled default</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>
   ${osTable ? `<details class="card" style="margin-top:10px"><summary style="margin:0">${osTable.replace(/<\/h3>[\s\S]*$/, "").replace(/^<h3>/, "")} (${osKeys.length})</summary>${osTable.replace(/^[\s\S]*?<\/h3>/, "")}</details>` : ""}`;
}

const EFF_LABELS = { mysqld_cpu_ms_per_trx: "mysqld CPU ms/trx", host_cpu_ms_per_trx: "host CPU ms/trx", disk_ios_per_trx: "disk IOs/trx", disk_kb_per_trx: "disk KiB/trx", fsyncs_per_trx: "fsyncs/trx", redo_kb_per_trx: "redo KiB/trx", rows_read_per_trx: "rows read/trx", db_mem_mb: "database memory MiB" };

async function renderExperiments(el, sess) {
  const d = await api(`/api/sessions/${sess.id}/experiments`);
  const active = sess.status === "active";
  const row = e => {
    const spec = e.kind === "sweep" ? `sweep <span class="mono">${esc(e.spec.variable)}</span> = ${e.spec.values.map(esc).join(", ")}` : `try ${e.spec.changes.map(c => `<span class="mono">${esc(c.variable)}=${esc(c.value)}</span>`).join(", ")}`;
    const res = e.results && e.results.arms ? `<div class="tablewrap" style="margin-top:6px"><table><thead><tr><th>Value</th><th class="num">Runs</th><th class="num">Goal</th><th class="num">vs parent</th><th>Result</th></tr></thead><tbody>${e.results.arms.map(a =>
      `<tr><td class="mono">${esc(a.arm)}${e.results.kept === a.arm ? ' <span class="badge good">kept</span>' : ""}</td><td class="num">${a.runs || ""}</td><td class="num">${num(a.goal_mean)}</td>
       <td class="num ${a.verdict === "better" ? "up" : a.verdict === "worse" ? "down" : ""}">${a.delta_pct != null ? pct(a.delta_pct) : ""}</td><td>${esc(a.verdict || a.status)}${a.reason ? info(a.reason) : ""}</td></tr>`).join("")}</tbody></table></div>` : "";
    return `<div class="rec"><div class="h"><b>#${e.id}</b> ${spec}${e.repeats > 1 ? ` · ${e.repeats} runs each` : ""} ${statusBadge(e.status === "done" ? "done" : e.status === "queued" ? "measuring" : e.status, e.message)}
      <span class="muted" style="font-size:12px">${esc((e.created_by || "").replace("human:", ""))}</span>${e.status === "queued" ? ` <button class="ghost danger" data-cancel="${e.id}">Cancel</button>` : ""}</div>
      ${e.reason ? `<div class="reason">${esc(e.reason)}</div>` : ""}${e.message ? `<div class="reason">${esc(e.message)}</div>` : ""}${res}</div>`;
  };
  el.innerHTML = (active ? `<form id="expf" class="row" style="align-items:flex-end;gap:8px">
      <div><label>Kind</label><select name="kind" id="ekind" style="width:auto"><option value="sweep">Sweep a variable</option><option value="try">Try settings</option></select></div>
      <div id="esweep" class="row" style="gap:8px"><div><label>Variable</label><input name="variable" placeholder="innodb_buffer_pool_size" style="width:220px"></div>
        <div><label>Values</label><input name="values" placeholder="1G, 2G, 3G" style="width:160px"></div></div>
      <div id="etry" hidden><label>Settings</label><input name="settings" placeholder="var=value, var2=value2" style="width:380px"></div>
      <div><label>Runs per value</label><select name="repeats" style="width:auto"><option>1</option><option>2</option><option>3</option></select></div>
      <div style="flex:1;min-width:160px"><label>Reason</label><input name="reason" placeholder="why (optional)"></div>
      <button class="primary">Queue</button></form><div class="err" id="eerr"></div>` : "") +
    (d.experiments.length ? d.experiments.map(row).join("") : `<p class="muted" style="margin:6px 0 0">No experiments${active ? " queued" : ""}.</p>`);
  const f = el.querySelector("#expf");
  if (f) {
    const k = el.querySelector("#ekind");
    k.onchange = () => { el.querySelector("#esweep").hidden = k.value !== "sweep"; el.querySelector("#etry").hidden = k.value !== "try"; };
    f.onsubmit = async ev => { ev.preventDefault();
      try { await api(`/api/sessions/${sess.id}/experiments`, { method: "POST", body: Object.fromEntries(new FormData(f)) }); renderExperiments(el, sess); }
      catch (err) { el.querySelector("#eerr").textContent = err.message; } };
  }
  el.querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => { await api(`/api/experiments/${b.dataset.cancel}`, { method: "DELETE" }); renderExperiments(el, sess); });
}

function renderRecs(el, rec, runs) {
  if (!rec) { el.innerHTML = '<span class="muted">No recommendations yet: run <code>tuning-agent recommend</code> after a measured run.</span>'; return; }
  const run = runs && runs.find(r => r.id === rec.run_id);
  let h = `<div class="muted" style="margin-bottom:6px">Generated ${fmtTime(rec.created)}${run ? ` from run #${run.seq} <a href="#/run/${run.id}">${esc(run.label)}</a>` : " without a measured run"} · goal ${esc(rec.goal)} · policy ${esc(rec.policy)}${rec.next ? ` · suggested next: <code>${esc(rec.next)}</code>` : ""}</div>`;
  (rec.diagnosis || []).forEach(dg => { h += `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}</div>`; });
  if (rec.workload && rec.workload.type) h += `<p style="margin:4px 0 8px">Workload: <b>${esc(WORKLOAD[rec.workload.type] || rec.workload.type)}</b>${info(kvText(rec.workload.evidence))}${rec.phase ? ` · Phase: <b>${esc(PHASE[rec.phase] || rec.phase)}</b>` : ""}</p>`;
  if (rec.llm && (rec.llm.analyses || []).length) h += rec.llm.analyses.map(a => a.hypothesis
    ? `<p style="margin:2px 0 8px"><b>Bottleneck analysis</b> of ${esc(a.component)} (${a.share_pct ?? "?"}% of database time, no rule acts on it)${a.cached ? ` <span class="badge plain">reused from similar conditions${a.cached_from ? " (session " + a.cached_from.session + ")" : ""}</span>` : ""}: ${esc(a.hypothesis)}${info((a.confirm_by ? "Confirmed if: " + a.confirm_by + ". " : "") + (a.notes ? "Notes: " + a.notes + ". " : "") + a.changes + " change(s) proposed; model " + (a.model || "?"))}</p>`
    : `<p class="muted" style="margin:2px 0 8px;font-size:12.5px">${esc(a.component)} (${a.share_pct ?? "?"}%): no rule acts on it; not analysed: ${esc(a.reason || a.error || "")}</p>`).join("");
  if (rec.llm && (rec.llm.model || rec.llm.skipped)) h += rec.llm.skipped ? `<p class="muted" style="margin:2px 0 8px;font-size:12.5px">LLM not asked: ${esc(rec.llm.skipped)}</p>` :
    `<p style="margin:2px 0 8px">LLM <b>${esc(rec.llm.model)}</b>${rec.llm.cached ? " (cached answer)" : ""}: ${rec.llm.accepted} suggestion(s) accepted${(rec.llm.rejected || []).length ? `, ${rec.llm.rejected.length} rejected` + info(rec.llm.rejected.map(x => `${x.title}: ${x.reason}`).join(" · ")) : ""}${info("Asked because: " + ({ "rules-exhausted": "the rule-based experiments were exhausted", "unclear-bottleneck": "no clear bottleneck was found", explicit: "it was requested", routine: "the account is set to use it for every recommendation" }[rec.llm.trigger] || rec.llm.trigger) + (rec.llm.notes ? ". Model notes: " + rec.llm.notes : ""))}</p>`;
  const items = rec.items || [];
  const safe = items.filter(i => (i.safety || "safe") !== "trade-off"), trade = items.filter(i => i.safety === "trade-off");
  const section = (title, list, note) => list.length ? `<h3>${title}${note ? info(note) : ""}</h3>` + list.map(recItem).join("") : "";
  h += section("Production-safe", safe) + section("Trade-offs (benchmark only)", trade, "These give up durability, replication, observability or semantics. Results with them are not production-equivalent; they are reported separately.");
  el.innerHTML = h;
  return;
  function recItem(it) {
    return `<div class="rec"><div class="h"><b>${it.rank}. ${esc(it.title)}</b><span class="id">${esc(it.id)}</span>${it.source === "llm" ? `<span class="badge info">LLM</span>` : ""}${it.kind === "advice" ? '<span class="badge plain">advice only</span>' : it.kind === "revert" ? '<span class="badge warn">revert</span>' : ""}
     ${riskBadge(it.risk)}${it.restart ? '<span class="badge plain">restart</span>' : ""}<span class="badge plain">impact ${it.impact}/3</span><span class="badge plain">confidence ${it.confidence}/3</span>${it.id === rec.next ? '<span class="badge info">next</span>' : ""}</div>
     ${it.changes.length ? `<div class="mono" style="margin:4px 0">${it.changes.map(c => `${esc(c.variable)}: <span class="diff-old">${esc(val(c.variable, c.current))}</span> → <span class="diff-new">${c.unset ? "(server default)" : esc(val(c.variable, c.proposed))}</span>`).join("<br>")}</div>` : ""}
     ${it.hypothesis ? `<div class="reason"><b>Why:</b> ${esc(it.hypothesis.text)}${it.hypothesis.lead ? ` <span class="muted">Leads (outside reports and learned insights - they motivate the test, the measurements decide): ${esc(it.hypothesis.lead)}</span>` : ""}${(it.rescore || {}).why ? ` <span class="badge ${it.rescore.delta > 0 ? "good" : "warn"}" data-tip="${esc(it.rescore.why.join("; "))}">${it.rescore.delta > 0 ? "+" : ""}${it.rescore.delta} from earlier results</span>` : ""}</div>` : ""}
     <div class="reason">${esc(it.rationale)}${it.note ? " <b>" + esc(it.note) + "</b>" : ""}</div>
     ${it.relevance != null || it.experience ? `<div class="reason" style="margin-top:3px">${it.relevance != null ? (it.no_mechanism ? `<span class="badge plain">validation only</span>` : it.relevance ? `<span class="badge good">acts on ${esc((it.addresses || []).join(", "))}</span>` : `<span class="badge plain">not aimed at the current bottleneck</span>`) + info("Mechanism: " + (it.mechanism || "") + (it.no_mechanism ? " Nothing to act on in this workload: " + it.no_mechanism + "." : " Ranked by how much of the database's time (see Where the time went) this change can act on.")) : ""}
       ${it.experience ? ` <span class="badge ${it.experience.wins > it.experience.losses ? "good" : it.experience.losses > it.experience.wins ? "bad" : "plain"}">earlier sessions: ${it.experience.wins} better · ${it.experience.losses} worse · ${it.experience.neutral} no change</span>` + info(`Outcomes of this recommendation in ${it.experience.n} earlier run(s) with a similar workload and CPU count${it.experience.mean_delta_pct != null ? `; average change ${pct(it.experience.mean_delta_pct)}` : ""}. Used to rank experiments.`) : ""}</div>` : ""}
     ${(it.tradeoffs || []).map(t => `<div class="reason"><span class="badge bad">${esc(t.category)}</span> ${esc(t.lost)}</div>`).join("")}
     ${Object.keys(it.evidence || {}).length ? `<details><summary>Evidence</summary><div class="kv">${Object.entries(it.evidence).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div></details>` : ""}</div>`;
  }
}

async function runPage(id, tab) {
  setNav("envs");
  const vs = new URLSearchParams(location.hash.split("?")[1] || "").get("vs");
  const r = await api("/api/runs/" + id + (vs ? "?vs=" + vs : ""));
  const s = r.session, sm = r.summary || {}, prev = r.compared_with;
  const ps = (prev && prev.summary) || {};
  // colour a change only when it is beyond the noise threshold; dir: 1 = higher is better, -1 = lower is better, 0 = no direction
  const cal = s.calibration || {}, goalNoise = (cal.noise_pct || s.noise_pct || 2);
  const effNoise = k => ((cal.efficiency || {})[k] || {}).noise_pct || Math.max(goalNoise, 3);
  const tile = (k, v, unit, pv, dir, noiseKey) => {
    if (v == null) return "";
    let d = "";
    if (pv != null && pv !== 0) {
      const ch = (v - pv) / Math.abs(pv) * 100, thr = noiseKey ? effNoise(noiseKey) : goalNoise;
      const cls = !dir || Math.abs(ch) <= thr ? "" : (ch * dir > 0 ? "up" : "down");
      d = `<div class="d ${cls}" ${dir ? `title="noise threshold ±${thr}%"` : ""}>${ch > 0 ? "+" : ""}${ch.toFixed(1)}% vs prev</div>`;
    }
    return `<div class="tile"><div class="k">${k}</div><div class="v">${num(v)}<span style="font-size:13px;color:var(--ink-2)"> ${unit}</span></div>${d}</div>`;
  };
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${s.env_id}">${esc(r.environment.name)}</a> / <a href="#/session/${s.id}">#${s.id} ${esc(s.name)}</a> /</div>
  <div class="row"><h1>Run #${r.seq} · ${esc(r.label)}</h1>${statusBadge(r.status)}${verdictBadge(r.verdict)}<span class="spacer"></span>
   <label style="margin:0">Compare with</label><select id="vs" style="width:auto"><option value="">previous run</option>${r.runs.filter(x => x.id !== r.id).map(x => `<option value="${x.id}" ${String(x.id) === vs ? "selected" : ""}>#${x.seq} ${esc(x.label)}</option>`).join("")}</select></div>
  <p class="sub">${fmtTime(r.started)} – ${r.ended ? fmtTime(r.ended) : "measuring…"} (${dur(sm.duration_s)}) · goal ${goalName(s.goal)} = <b>${num(r.goal_value)} ${goalUnit(s.goal, s)}</b>${r.goal_source ? ` <span class="muted">(${esc(r.goal_source)})</span>` : ""}
   ${r.delta_pct != null ? ` · <span class="${deltaCls(r.delta_pct)}">${pct(r.delta_pct)}</span> vs previous, <span class="${deltaCls(r.delta_vs_first_pct)}">${pct(r.delta_vs_first_pct)}</span> vs first` : ""}${prev ? ` · compared with run #${prev.seq} ${esc(prev.label)}` : ""}</p>
  ${r.status === "aborted" ? `<div class="note" style="margin-bottom:12px"><span class="badge warn">aborted</span> ${esc(r.abort_reason || "no reason recorded")}. Its changes were handed to the next run.</div>` : ""}
  ${r.notes ? `<div class="note" style="margin-bottom:12px">${esc(r.notes)}</div>` : ""}
  <div class="tiles">${tile("TPS", sm.tps, "/s", ps.tps, 1)}${tile("QPS", sm.qps, "/s", ps.qps, 1)}${tile("Avg statement latency", sm.avg_latency_ms, "ms", ps.avg_latency_ms, -1)}${tile("p95 statement latency", sm.p95_latency_ms, "ms", ps.p95_latency_ms, -1)}
   ${tile("CPU busy (host)", sm.cpu_busy, "%", ps.cpu_busy, 0)}${(sm.cpu_steal || 0) >= 0.5 || (sm.cpu_steal_max || 0) >= 5 ? tile("CPU steal" + info(`CPU time the hypervisor gave to other virtual machines: average over the run, peak ${num(sm.cpu_steal_max)}% in a 10 s sample${sm.client_cpu_steal != null ? `, client ${num(sm.client_cpu_steal)}%` : ""}. Its possible effect on the result (about the average steal, less when the host is not CPU-bound) is flagged from a quarter of the session's noise threshold (at least 0.25%) or a 5% peak, and the run is excluded from half the threshold (at least 0.5%). Normal on these hosts: below 0.05% average.`), sm.cpu_steal, "%", ps.cpu_steal, 0) : ""}${tile("mysqld CPU", sm.mysqld_cpu_pct, "% of host", ps.mysqld_cpu_pct, 0)}${tile("IO wait", sm.cpu_iowait, "%", ps.cpu_iowait, -1, "x")}${tile("BP hit ratio", sm.bp_hit_pct, "%", ps.bp_hit_pct, 1)}${tile("Disk IOPS", (sm.disk_r_iops || 0) + (sm.disk_w_iops || 0), "/s", ps.disk_r_iops != null ? (ps.disk_r_iops || 0) + (ps.disk_w_iops || 0) : null, 0)}
   ${tile("CPU pressure", sm.mysqld_psi_cpu_some != null ? sm.mysqld_psi_cpu_some : sm.psi_cpu_some, "% stalled", ps.mysqld_psi_cpu_some != null ? ps.mysqld_psi_cpu_some : ps.psi_cpu_some, -1, "x")}${tile("I/O pressure", sm.mysqld_psi_io_some != null ? sm.mysqld_psi_io_some : sm.psi_io_some, "% stalled", ps.mysqld_psi_io_some != null ? ps.mysqld_psi_io_some : ps.psi_io_some, -1, "x")}${tile("Memory pressure", sm.psi_memory_some, "% stalled", ps.psi_memory_some, -1, "x")}${tile("Sched delay" + info(`Average time database threads waited in the CPU run queue each time they were scheduled (from /proc/<tid>/schedstat). It adds directly to query latency, mostly at the tail. Database threads waited ${num(sm.dbproc_runq_wait_pct)}% of their runnable time (${num(sm.dbproc_runq_wait_threads)} threads waiting on average); worst 10 s interval ${num(sm.dbproc_sched_delay_us_max)} µs; ${num(sm.dbproc_nvcsw_per_s)} preemptions/s. All tasks on the host: ${num(sm.host_sched_delay_us)} µs.`), sm.dbproc_sched_delay_us, "µs", ps.dbproc_sched_delay_us, -1, "x")}${tile("DB memory (PSS)" + info(`Proportional set size of the database process(es): shared pages are split between the processes that map them, so this is their real share of RAM (RSS counts shared pages once per process). At the end of the run: RSS ${num(sm.dbproc_rss_mb)} MiB · VSZ ${num(sm.dbproc_vsz_mb)} MiB · swap PSS ${num(sm.dbproc_swap_pss_mb)} MiB · ${sm.dbproc_procs || 1} process(es). Peak PSS ${num(sm.dbproc_pss_mb_max)} MiB.`), sm.dbproc_pss_mb, "MiB", ps.dbproc_pss_mb, 0)}
   ${tile("mysqld CPU / trx", sm.mysqld_cpu_ms_per_trx, "ms", ps.mysqld_cpu_ms_per_trx, -1, "mysqld_cpu_ms_per_trx")}${tile("Disk IOs / trx", sm.disk_ios_per_trx, "", ps.disk_ios_per_trx, -1, "disk_ios_per_trx")}
   ${tile("Redo written", sm.redo_mbs, "MiB/s", ps.redo_mbs, 0)}${tile("Ckpt age max", sm.checkpoint_age_pct_max, "%", ps.checkpoint_age_pct_max, -1, "x")}${tile("Threads running", sm.threads_running_avg, "avg", ps.threads_running_avg, 0)}${tile("TPS variation", sm.tps_cv_pct, "% CV", ps.tps_cv_pct, -1, "x")}</div>
  ${Object.keys(r.external || {}).length ? `<h3>Benchmark-reported values</h3><div class="row">${Object.entries(r.external).map(([k, v]) => `<span class="badge plain">${esc(k)} = ${esc(v)}</span>`).join("")}</div>` : ""}
  <div class="tabs" id="tabs"></div><div id="tab"></div>`;
  document.getElementById("vs").onchange = e => { location.hash = "#/run/" + id + (e.target.value ? "?vs=" + e.target.value : ""); };
  const tabs = [["changes", "Configuration"], ["graphs", "Graphs"], ["inputs", "Diagnosis & inputs"], ...((r.inputs || {}).cpu_profile ? [["cpu", "CPU profile"]] : []), ["recs", "Recommendations"], ["config", "Full configuration"]];
  const tabsEl = document.getElementById("tabs"), tabEl = document.getElementById("tab");
  const show = async name => {
    tabsEl.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.t === name));
    clearTimers();
    if (name === "changes") { renderDiff(tabEl, r); const ks = document.createElement("div"); tabEl.appendChild(ks); renderKeySettings(ks, id); }
    else if (name === "graphs") {
      tabEl.innerHTML = `<p class="muted">Shaded = measurement window${info("Charts show the measurement window plus 60 s before and after it.")}</p><div class="grid g3" id="rc"></div>`;
      const m = await api(`/api/runs/${id}/metrics?points=300`);
      chartsGrid(document.getElementById("rc"), m, RUN_CHARTS, m.window);
      (m.clients || []).forEach(cl => {
        const box = document.createElement("div"); box.className = "grid g3"; box.style.marginTop = "14px";
        document.getElementById("rc").after(box);
        const cm = { ts: cl.ts, os: cl.os, db: {} };
        chartsGrid(box, cm, [
          { title: `Client ${cl.name}: CPU`, unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "softirq_max_cpu_pct", "busiest CPU softirq"]] },
          { title: `Client ${cl.name}: RTT to DB`, unit: "ms", series: [["os", "rtt_avg_ms", "avg"], ["os", "rtt_p95_ms", "p95"]] },
          { title: `Client ${cl.name}: connections`, unit: "", series: [["os", "net_conn_per_s", "new/s"], ["os", "net_time_wait", "TIME_WAIT"], ["os", "net_retrans_pct", "retrans %"]] },
        ], m.window);
      });
      if (r.status === "measuring") timers.push(setInterval(async () => { const m2 = await api(`/api/runs/${id}/metrics?points=300`); chartsGrid(document.getElementById("rc"), m2, RUN_CHARTS, m2.window); }, 15000));
    }
    else if (name === "inputs") renderInputs(tabEl, r);
    else if (name === "cpu") renderCpuProfile(tabEl, r.inputs.cpu_profile);
    else if (name === "recs") { tabEl.innerHTML = ""; if (!r.recommendations.length) tabEl.innerHTML = '<p class="muted">No recommendation was generated from this run.</p>'; r.recommendations.forEach(rec => { const c = document.createElement("div"); c.className = "card"; c.style.marginBottom = "12px"; tabEl.appendChild(c); renderRecs(c, rec); }); }
    else if (name === "config") {
      const c = await api(`/api/runs/${id}/config`);
      const managed = c.managed || {}, meta = c.meta || {};
      const hasMeta = Object.keys(meta).length > 0;
      tabEl.innerHTML = `<div class="row"><input id="flt" placeholder="filter variables" style="max-width:300px">${hasMeta ? '<label style="margin:0;display:flex;gap:6px;align-items:center;width:auto"><input type="checkbox" id="nondef" style="width:auto"> only non-default</label>' : ""}<span class="muted">${Object.keys(c.config).length} variables at measurement start; <b>bold</b> = managed by the agent</span></div>
        <div class="card tablewrap" style="margin-top:10px"><table><thead><tr><th>Variable</th><th>Value</th>${hasMeta ? "<th>Compiled default</th><th>Source</th>" : ""}</tr></thead><tbody id="cfg"></tbody></table></div>`;
      const draw = () => {
        const q = document.getElementById("flt").value.toLowerCase(), nd = hasMeta && document.getElementById("nondef").checked;
        document.getElementById("cfg").innerHTML = Object.entries(c.config).filter(([k]) => k.includes(q) && (!nd || !isDefault(meta[k]))).map(([k, v]) => {
          const m = meta[k] || {};
          return `<tr><td class="mono">${managed[k] != null ? "<b>" + esc(k) + "</b>" : esc(k)}</td><td class="mono" style="word-break:break-all">${esc(val(k, v))}</td>${hasMeta ? `<td class="mono muted" style="word-break:break-all">${m.default != null ? esc(val(k, m.default)) : ""}</td><td>${isDefault(m) ? '<span class="muted">compiled default</span>' : esc(sourceText(m))}</td>` : ""}</tr>`;
        }).join("");
      };
      document.getElementById("flt").oninput = draw; if (hasMeta) document.getElementById("nondef").onchange = draw; draw();
    }
    history.replaceState(null, "", "#/run/" + id + "/" + name + (vs ? "?vs=" + vs : ""));
  };
  tabs.forEach(([k, label]) => { const b = document.createElement("button"); b.textContent = label; b.dataset.t = k; b.onclick = () => show(k); tabsEl.appendChild(b); });
  show(tabs.some(t => t[0] === tab) ? tab : "changes");
}

function queryImpact(qi) {
  if (!qi || !qi.rows || !qi.rows.length) return "";
  const row = q => `<tr><td class="mono" style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" data-tip="${esc(q.query)}">${esc(q.query)}</td>
    <td class="num">${q.pct_time != null ? q.pct_time + "%" : ""}</td><td class="num">${num(q.avg_ms_parent)}</td><td class="num">${num(q.avg_ms)}</td>
    <td class="num ${Math.abs(q.faster_pct) > qi.noise_pct ? (q.faster_pct > 0 ? "up" : "down") : ""}">${q.faster_pct > 0 ? "−" : "+"}${Math.abs(q.faster_pct).toFixed(0)}%</td></tr>`;
  return `<details ${qi.mixed ? "open" : ""} style="margin:4px 0 10px"><summary>Per-query impact: ${qi.winners} faster, ${qi.losers} slower${qi.mixed ? " — mixed" : ""}${info(`Average latency of the top statements under the parent configuration vs this run. Changes within ±${qi.noise_pct}% are treated as noise. When a global change helps some statements and hurts others, the tuner suggests applying it per statement with a SET_VAR optimizer hint.`)}</summary>
   <div class="tablewrap"><table><thead><tr><th>Statement</th><th class="num">% time</th><th class="num">Parent ms</th><th class="num">This run ms</th><th class="num">Latency</th></tr></thead><tbody>${qi.rows.map(row).join("")}</tbody></table></div></details>`;
}

function renderEvaluation(r) {
  const ev = r.evaluation || {};
  if (ev.same_config_runs) return `<div class="note" style="margin-bottom:12px">${ev.drift ? '<span class="badge warn">drift</span> ' : ""}Repeat of runs ${ev.same_config_runs.map(x => "#" + x).join(", ")}: <b>${pct(ev.deviation_pct)}</b> vs their mean${info((ev.drift ? ev.note + " " : "") + `Same configuration measured again (mean of earlier runs ${num(ev.same_config_mean)}, noise ±${ev.goal_noise_pct}%). Repeats are averaged into that configuration's mean.`)}</div>`;
  if (!ev.parent_runs) return "";
  const rows = Object.entries(ev.efficiency || {}).map(([k, e]) => `<tr><td>${esc(EFF_LABELS[k] || e.label)}</td><td class="num">${num(e.parent)}</td><td class="num">${num(e.now)}</td><td class="num ${Math.abs(e.improvement_pct) > e.noise_pct ? (e.improvement_pct > 0 ? "up" : "down") : ""}">${e.improvement_pct > 0 ? "−" : "+"}${Math.abs(e.improvement_pct).toFixed(1)}%</td><td class="num muted">±${e.noise_pct}%</td></tr>`).join("");
  const hy = ev.hypothesis;
  const HO = { supported: ["good", "hypothesis supported"], moved: ["warn", "mechanism worked, goal did not move"], refuted: ["bad", "hypothesis refuted"],
    "improved-other-mechanism": ["info", "improved, but not via the targeted time"], done: ["plain", ""] };
  const hyHtml = hy ? `<div class="note" style="margin-bottom:10px"><b>Hypothesis tested:</b> ${esc(hy.text)} ${hy.outcome && HO[hy.outcome] && HO[hy.outcome][1] ? `<span class="badge ${HO[hy.outcome][0]}">${HO[hy.outcome][1]}</span>` : ""}${(hy.evidence || {}).targeted_time_per_unit_change_pct != null ? ` <span class="muted">targeted time per transaction ${hy.evidence.targeted_time_per_unit_change_pct > 0 ? "+" : ""}${hy.evidence.targeted_time_per_unit_change_pct}%</span>` : ""}${info("supported: goal improved and the targeted part of the database's time shrank per transaction. mechanism worked, goal did not move: the targeted time shrank but the goal stayed within noise - that part of the time is not what limits the goal now, so ideas aimed at it rank lower. refuted: neither moved. Outcomes re-score the remaining ideas in this session and teach later sessions in similar conditions.")}</div>` : "";
  return `${hyHtml}<div class="card" style="margin-bottom:14px"><h3 style="margin-top:0">Evaluation ${verdictBadge(r.verdict)}</h3>
   <p style="margin:0 0 8px">Goal <b class="${deltaCls(ev.goal_delta_pct)}">${pct(ev.goal_delta_pct)}</b> vs parent configuration (±${ev.goal_noise_pct}% noise)${info(`Parent configuration = the one this run was changed from; compared with the mean of its most recent runs (${ev.parent_runs.map(x => "#" + x).join(", ")} = ${num(ev.parent_goal_mean)}). Noise threshold ${ev.calibrated ? "calibrated" : "not calibrated"}. improved/regressed: goal beyond noise. efficiency-gain/-loss: goal within noise but CPU or I/O per transaction or database memory measurably lower/higher (beyond its own noise) - using fewer resources for the same result is the tie-breaker.`)}</p>
   ${queryImpact(ev.query_impact)}
   ${rows ? `<div class="tablewrap"><table><thead><tr><th>Work per transaction</th><th class="num">Parent mean</th><th class="num">This run</th><th class="num">Change</th><th class="num">Noise</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}</div>`;
}

function diffRow(d) {
  const fd = flagDiff(d.before, d.after);
  const cells = fd
    ? `<td class="mono">${esc(d.variable)}<div class="muted" style="font-size:11.5px;font-family:inherit">${fd.length} of ${Object.keys(parseFlags(d.after)).length} flags changed</div></td>
       <td class="mono diff-old flags">${fd.map(x => `${esc(x.flag)}=${esc(x.from || "–")}`).join("<br>")}<details class="fullval"><summary>full value</summary>${esc(d.before)}</details></td>
       <td class="mono diff-new flags">${fd.map(x => `${esc(x.flag)}=<b>${esc(x.to || "–")}</b>`).join("<br>")}<details class="fullval"><summary>full value</summary>${esc(d.after)}</details></td>`
    : `<td class="mono">${esc(d.variable)}</td><td class="mono diff-old" style="word-break:break-all">${esc(val(d.variable, d.before))}</td><td class="mono diff-new" style="word-break:break-all">${esc(val(d.variable, d.after))}</td>`;
  return `<tr>${cells}<td>${esc(d.source)}</td><td class="reason">${esc(d.reason || "")}</td></tr>`;
}
function renderDiff(el, r) {
  const prev = r.compared_with;
  let h = renderEvaluation(r);
  if (r.seq === 1) h += `<p class="muted">Starting configuration of the session${info("The first run measures the configuration the session started with. Key settings below show compiled defaults and the source of each value." + (prev ? ` Differences from the last run before this session (run #${prev.seq}) are listed for reference.` : ""))}</p>`;
  if (!prev) {}
  else if (!r.diff.length && r.seq !== 1) h += `<p>No configuration differences with run #${prev.seq} <b>${esc(prev.label)}</b>.</p>`;
  else if (r.diff.length) h += `<h3>Changed since run #${prev.seq} ${esc(prev.label)}${info("Source and reasoning come from the agent. “outside the agent” means the value changed some other way: manual edit, server default after restart, automatic sizing.")}</h3>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Run #${prev.seq}</th><th>This run</th><th>Source</th><th>Reason</th></tr></thead><tbody>${r.diff.map(diffRow).join("")}</tbody></table></div>`;
  const managed = r.managed || {};
  const reqs = ((r.inputs || {}).bench || {}).requirements || [];
  if (Object.keys(managed).length) h += `<h3>Agent-applied configuration during this run${info("The settings the tuning-agent enforced while this run was measured: its MySQL option file plus operating-system settings. Everything else was the server default or came from other option files (see Key settings).")}</h3>${configBlocks(managed, reqs)}`;
  else if (reqs.length) h += `<h3>Benchmark prerequisites${info(REQ_TIP)}</h3><div class="card">${reqTable(reqs)}</div>`;
  el.innerHTML = h;
}

function renderCpuProfile(el, p) {
  if (p.error) { el.innerHTML = `<p class="muted">The CPU profile could not be taken: ${esc(p.error)}</p>`; return; }
  const cats = Object.entries(p.categories || {});
  el.innerHTML = `<p class="sub">${p.seconds} s at ${p.hz} samples/s per CPU ${p.in_measured_window === false ? "at the end of the warm-up (outside the measured window)" : "during the run"} · ${num(p.db_samples)} database samples${p.call_stacks ? "" : " · leaf functions only (binary without frame pointers)"}${info("perf-event sampling (the kernel mechanism eBPF profilers use) on every CPU at a low frequency for a slice of the run; symbols are resolved after the measurement at low priority. Taken at the end of the warm-up, so the measured results never include the profiler.")}</p>
  <h3>Where the database spent CPU${info("Each sample is assigned to the first matching category from the innermost frame outwards. Categories map to tuning mechanisms, e.g. spin / mutex wait → spin loops or a thread pool, memory allocator → jemalloc, performance_schema → instrumentation cost.")}</h3>
  <div class="card"><div class="waitbar">${cats.map(([k, v], i) => `<span style="width:${v}%;background:${WAIT_COLORS[i % WAIT_COLORS.length]}" data-tip="${esc(k)}: ${v}%"></span>`).join("")}</div>
   <div class="row" style="gap:4px 14px;font-size:12px;margin-top:6px">${cats.map(([k, v], i) => `<span><i class="dot" style="background:${WAIT_COLORS[i % WAIT_COLORS.length]}"></i>${esc(k)} ${v}%</span>`).join("")}</div>
   <p class="muted" style="margin:8px 0 0;font-size:12px">Whole host during the profile: ${Object.entries(p.by_process || {}).map(([k, v]) => `${esc(k)} ${v}%`).join(" · ")}</p></div>
  <h3>Flame graph${info("Width = share of database CPU samples. Bottom = thread entry points, top = the function on CPU. Click a frame to zoom in, click it again to zoom out. Grey frames are unsymbolised library code.")}</h3><div class="card" id="flame"></div>
  <div class="grid g2" style="margin-top:12px"><div><h3 style="margin-top:0">Top functions (self)</h3><div class="card tablewrap"><table class="kvt"><tbody>${(p.top_self || []).slice(0, 20).map(x => `<tr><td class="mono" style="word-break:break-all">${esc(x.fn)}</td><td class="num">${x.pct}%</td></tr>`).join("")}</tbody></table></div></div>
   <div><h3 style="margin-top:0">Top functions (including callees)</h3><div class="card tablewrap"><table class="kvt"><tbody>${(p.top_inclusive || []).filter(x => !/^\[/.test(x.fn)).slice(0, 20).map(x => `<tr><td class="mono" style="word-break:break-all">${esc(x.fn)}</td><td class="num">${x.pct}%</td></tr>`).join("")}</tbody></table></div></div></div>`;
  if ((p.folded || []).length) C.flameGraph(document.getElementById("flame"), p.folded);
  else document.getElementById("flame").innerHTML = '<p class="muted">No call stacks.</p>';
}
function renderInputs(el, r) {
  const inp = r.inputs || {};
  let h = "";
  if (inp.workload && inp.workload.type) h += `<p>Workload: <b>${esc(WORKLOAD[inp.workload.type] || inp.workload.type)}</b>${info(kvText(inp.workload.evidence))}</p>`;
  if (inp.probe && Object.keys(inp.probe).length) h += `<p>Environment probe${info("Measured with the database idle right before the benchmark: single-thread CPU speed (sysbench events/s), 4k random read/write IOPS and fsync rate on the data filesystem (fio). Compared with the calibration runs to tell a slower VM from a worse configuration.")}: ${esc(kvText(inp.probe))}${(r.evaluation || {}).environment_shift ? ` <span class="badge warn">shift vs calibration: ${esc(kvText(r.evaluation.environment_shift))} %</span>` : ""}</p>`;
  if (inp.bench) {
    const b = inp.bench;
    h += `<h3>Benchmark: ${esc(b.name)}</h3><div class="kv"><div>Run command</div><div class="mono">${esc(b.run)}</div>${b.reset ? `<div>Reset</div><div class="mono">${esc(b.reset)}</div>` : ""}<div>Warm-up / measured</div><div>${esc(b.warmup)} s / ${b.duration ? esc(b.duration) + " s" : "until finished"}</div><div>Exit code</div><div>${esc(b.exit_code)}</div>${(b.requirements || []).length ? `<div>Prerequisites${info(REQ_TIP)}</div><div class="mono">${b.requirements.map(q => esc(q.variable) + " ≥ " + esc(q.required)).join(" · ")}</div>` : ""}
      ${Object.entries(b.metrics || {}).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono"><b>${esc(v)}</b></div>`).join("")}</div><details><summary>Benchmark output (tail)</summary><pre>${esc(b.log_tail || "")}</pre></details>`;
  }
  const tbk = inp.time_breakdown || [];
  if (tbk.length) {
    const q = tbk[0].cpu_queue_share_pct;
    h += `<h3>Where the time went${info("Average number of database threads in each state during the measured window, largest first: what keeps the workload from going faster. On-CPU time is split by the CPU profile when one was taken (otherwise one CPU component); time spent runnable but waiting for a CPU is added to the CPU components in proportion, because doing less CPU work shortens that queue too. Waits come from the eBPF off-CPU profile of the connection threads when one was taken (every blocked moment, classified by where the thread blocked, including waits performance_schema does not instrument); otherwise from performance_schema waits and file I/O by operation (global, so background threads such as page cleaners are included) and InnoDB row lock counters. Name resolution comes from eBPF probes over the whole window. Hover a component for its source and the stacks behind it. 'Client / network' = the server waiting for the next statement. Experiments are ranked by how much of this time they can act on.")}</h3>
      <div class="card"><div class="waitbar">${tbk.map((p, i) => `<span style="width:${p.share_pct}%;background:${WAIT_COLORS[i % WAIT_COLORS.length]}" data-tip="${esc(p.name)}: ${p.share_pct}% (${num(p.threads)} threads)"></span>`).join("")}</div>
      <div class="tablewrap"><table class="kvt" style="margin-top:6px"><tbody>${tbk.slice(0, 10).map((p, i) => {
        const st = ((inp.offcpu || {}).top_stacks || []).filter(x => x.category === p.name).slice(0, 3);
        const tip = "Source: " + (p.source || "?") + (st.length ? ". Where threads blocked: " + st.map(x => `${x.seconds}s in ${(x.user || []).slice(0, 4).join(" < ")}`).join(" | ") : "");
        return `<tr><td><i class="dot" style="background:${WAIT_COLORS[i % WAIT_COLORS.length]}"></i>${esc(p.name)}${info(tip)}</td><td class="num">${p.share_pct}%</td><td class="num muted">${num(p.threads)} thr</td></tr>`; }).join("")}</tbody></table></div>
      ${q ? `<p class="muted" style="margin:6px 0 0;font-size:12px">Of this, ${q}% was runnable threads waiting for a CPU (included in the CPU components).</p>` : ""}</div>`;
  }
  const blk = ((inp.block_latency || {}).by_device_op) || {};
  const bk = Object.keys(blk).sort((a, b) => blk[b].count - blk[a].count).slice(0, 6);
  if (bk.length) {
    const sm = r.summary || {};
    h += `<h3>Storage latency${info("Every block I/O's device service time (eBPF, issue to completion) over the measured window, by device and operation. p99 / p99.9 are upper bounds of log2 buckets (within 2x). Averages from /proc/diskstats are shown per 10 s sample in the charts; the spread of those samples shows latency moving inside the run. Slow = 8 ms or more.")}</h3>
      <div class="card tablewrap"><table><thead><tr><th>Device: operation</th><th class="num">I/Os</th><th class="num">p50 ms</th><th class="num">p99 ms</th><th class="num">p99.9 ms</th><th class="num">max ms</th><th class="num">slow</th></tr></thead><tbody>${bk.map(k => { const v = blk[k];
        return `<tr><td>${esc(k)}</td><td class="num">${num(v.count)}</td><td class="num">${num(v.p50_us / 1000)}</td><td class="num">${num(v.p99_us / 1000)}</td><td class="num">${num(v.p999_us / 1000)}</td><td class="num">${num((v.max_us || 0) / 1000)}</td><td class="num">${num(v.slow_10ms)}</td></tr>`; }).join("")}</tbody></table>
      ${sm.disk_r_await_ms_p50 != null || sm.disk_w_await_ms_p50 != null ? `<p class="muted" style="margin:6px 0 0;font-size:12px">10 s samples: reads ${sm.disk_r_await_ms_p50 ?? "-"} ms median / ${sm.disk_r_await_ms_max ?? "-"} ms worst, writes ${sm.disk_w_await_ms_p50 ?? "-"} ms / ${sm.disk_w_await_ms_max ?? "-"} ms${sm.disk_flush_await_ms_p50 != null ? `, flushes ${sm.disk_flush_await_ms_p50} ms / ${sm.disk_flush_await_ms_max} ms` : ""}${sm.tput_samples ? `; throughput below half its median in ${sm.tput_dip_samples} of ${sm.tput_samples} samples` : ""}.</p>` : ""}</div>`;
  }
  h += "<h3>Diagnosis</h3>" + ((r.diagnosis || []).map(dg => `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}<div class="mono muted" style="font-size:11.5px;margin-top:3px">${esc(JSON.stringify(dg.evidence))}</div></div>`).join("") || '<p class="muted">none</p>');
  if (inp.top_statements && inp.top_statements.length) {
    h += `<h3>Top statements in the window${info("From performance_schema digests and server-side prepared statements (marked prepared). The agent's own monitoring queries are excluded.")}</h3><div class="card tablewrap"><table><thead><tr><th>Statement</th><th class="num">% time</th><th class="num">per s</th><th class="num">avg ms</th><th class="num">rows exam.</th><th class="num">rows sent</th><th class="num">no index</th><th class="num">tmp disk</th></tr></thead><tbody>` +
      inp.top_statements.map(q => `<tr><td class="mono" style="max-width:520px;word-break:break-all">${q.prepared ? '<span class="badge plain">prepared</span> ' : ""}${esc(q.query)}</td><td class="num">${q.pct_time}</td><td class="num">${num(q.per_s)}</td><td class="num">${num(q.avg_ms)}</td><td class="num">${num(q.rows_examined_avg)}</td><td class="num">${num(q.rows_sent_avg)}</td><td class="num">${q.no_index_used}</td><td class="num">${q.tmp_disk_tables}</td></tr>`).join("") + "</tbody></table></div>";
  }
  if (inp.top_waits && inp.top_waits.length) h += waitsTable(inp, r);
  const st = inp.status_rates || {};
  if (Object.keys(st).length) {
    h += `<h3>Status counter rates in the window (per second)</h3><input id="sflt" placeholder="filter" style="max-width:260px"><div class="card tablewrap" style="margin-top:8px;max-height:420px;overflow:auto"><table><tbody id="st"></tbody></table></div>`;
  }
  const iso = inp.innodb_status || {};
  if (Object.values(iso).some(Boolean)) h += "<h3>InnoDB status at the end of the window</h3>" + Object.entries(iso).filter(([, v]) => v).map(([k, v]) => `<details><summary>${esc(k.replace(/_/g, " "))}</summary><pre>${esc(v)}</pre></details>`).join("");
  const im = inp.innodb_metrics_rates || {};
  if (Object.keys(im).length) h += `<details><summary>InnoDB metrics rates (${Object.keys(im).length})</summary><div class="kv">${Object.entries(im).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(v)}</div>`).join("")}</div></details>`;
  h += `<details><summary>OS rates over the window</summary><div class="kv">${Object.entries(inp.os_rates || {}).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(v)}</div>`).join("")}</div></details>`;
  el.innerHTML = h || '<p class="muted">No inputs recorded.</p>';
  const tb = document.getElementById("st");
  if (tb) {
    const draw = () => { const q = document.getElementById("sflt").value.toLowerCase(); tb.innerHTML = Object.entries(st).filter(([k]) => k.toLowerCase().includes(q)).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="num mono">${num(v)}</td></tr>`).join(""); };
    document.getElementById("sflt").oninput = draw; draw();
  }
}

// ------------------------------------------------------------------ info tooltips (hover, focus, tap)
(function () {
  const show = el => { const r = el.getBoundingClientRect(); const t = document.getElementById("tip");
    t.innerHTML = `<div style="max-width:300px;white-space:normal;line-height:1.45">${esc(el.dataset.tip)}</div>`; t.hidden = false;
    let left = Math.min(r.left, window.innerWidth - t.offsetWidth - 8), top = r.bottom + 6;
    if (top + t.offsetHeight > window.innerHeight - 8) top = r.top - t.offsetHeight - 6;
    t.style.left = Math.max(8, left) + "px"; t.style.top = top + "px"; };
  const hide = () => { document.getElementById("tip").hidden = true; };
  document.addEventListener("mouseover", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) show(el); });
  document.addEventListener("mouseout", e => { if (e.target.closest && e.target.closest("[data-tip]")) hide(); });
  document.addEventListener("focusin", e => { if (e.target.dataset && e.target.dataset.tip) show(e.target); });
  document.addEventListener("focusout", hide);
  document.addEventListener("click", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) show(el); else hide(); });
})();

// ------------------------------------------------------------------ router
async function loadMe() { const d = await api("/api/me", { allow401: true }); ME = d.user; if (d.server) SERVER = d.server; }
function markOptionalColumns(root) {
  root.querySelectorAll("table[data-opt]").forEach(t => {
    const opt = t.dataset.opt.split("|");
    const idx = [...t.querySelectorAll("thead th")].map((th, i) => opt.some(o => th.textContent.trim().startsWith(o)) ? i : -1).filter(i => i >= 0);
    if (!idx.length) return;
    t.querySelectorAll("tr").forEach(tr => idx.forEach(i => { const c = tr.children[i]; if (c && !c.hasAttribute("colspan")) c.classList.add("opt"); }));
  });
}
new MutationObserver(() => markOptionalColumns(document.getElementById("view") || document.body)).observe(document.getElementById("view") || document.body, { childList: true, subtree: true });
async function route() {
  clearTimers();
  document.getElementById("tip").hidden = true;
  const h = location.hash.replace(/^#/, "").split("?")[0] || "/";
  const parts = h.split("/").filter(Boolean);
  try {
    if (parts[0] === "s" && parts[1]) {  // read-only share link
      try { sessionStorage.setItem("shareToken", parts[1]); } catch (e) { }
      await loadMe();
      if (!ME || !ME.share) { $view.innerHTML = '<p class="err">This share link was revoked or does not exist.</p>'; return; }
      location.replace("#/" + (ME.share.kind === "study" ? "study/" : "env/") + ME.share.id); return;
    }
    if (ME && ME.share && (!parts.length || ["connect", "account", "policies-edit"].includes(parts[0]))) {
      location.replace("#/" + (ME.share.kind === "study" ? "study/" : "env/") + ME.share.id); return;
    }
    if (parts[0] === "login") return authForm("login");
    if (parts[0] === "register") return authForm("register");
    if (!ME) return landing();
    if (!parts.length) return await envList();
    if (parts[0] === "connect") return await connectPage();
    if (parts[0] === "account") return await accountPage();
    if (parts[0] === "policies") return await policiesPage();
    if (parts[0] === "learning") return await learningPage();
    if (parts[0] === "parameters") return await parametersPage();
    if (parts[0] === "env") return await envPage(+parts[1]);
    if (parts[0] === "studies") return await studiesPage();
    if (parts[0] === "study") return await studyPage(+parts[1]);
    if (parts[0] === "session") return await sessionPage(+parts[1]);
    if (parts[0] === "run") return await runPage(+parts[1], parts[2]);
    $view.innerHTML = "<p>Not found.</p>";
  } catch (e) {
    if (e.message !== "login required") $view.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}
window.addEventListener("hashchange", route);
let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (/^#\/(env|run)/.test(location.hash)) route(); }, 400); });
loadMe().then(route);
