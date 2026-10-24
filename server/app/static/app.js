/* AI DB Tuner single-page UI */
const $view = document.getElementById("view");
const C = window.Charts;
let ME = null, SERVER = location.origin, timers = [];
let navSeq = 0, widthFns = [];  // navigation counter (a page's late answers are dropped once another page started); chart redraws on width changes

const WORKLOAD = { "oltp-write": "OLTP, write-heavy", "oltp-read": "OLTP, read-mostly", olap: "Analytical", mixed: "Mixed", idle: "Idle" };
const PHASE = { safe: "production-safe experiments", "trade-offs": "trade-off experiments", done: "no further experiments" };
function kvText(o) { return Object.entries(o || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v != null && typeof v === "object" ? JSON.stringify(v) : typeof v === "number" ? num(v) : v}`).join(" · "); }
// numbers in agent-written texts: 6.036e+05 -> 604k, "->" -> "→"
function sci(t) { return String(t == null ? "" : t).replace(/-?\d+(?:\.\d+)?e[+-]\d+/gi, x => C.fmtNum(+x)).replace(/ -> /g, " → "); }
function setTitle(t) { document.title = (t ? t + " · " : "") + "AI DB Tuner"; }
function info(text) { return ` <span class="itip" tabindex="0" role="note" aria-label="${esc(text)}" data-tip="${esc(text)}">i</span>`; }
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function shareToken() { try { return sessionStorage.getItem("shareToken"); } catch (e) { return null; } }
async function api(path, opts = {}) {
  const nav = navSeq;
  const headers = opts.body ? { "Content-Type": "application/json" } : {};
  const share = shareToken();
  if (share) headers["X-Share-Token"] = share;
  const r = await fetch(path, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined, credentials: "same-origin" });
  let data = null; try { data = await r.json(); } catch (e) { }
  if (r.status === 401 && !opts.allow401) { location.hash = "#/login"; throw new Error("login required"); }
  const d = data && data.detail;  // FastAPI validation errors: a list of {msg}
  if (!r.ok) throw new Error(Array.isArray(d) ? d.map(x => x.msg || JSON.stringify(x)).join("; ") : typeof d === "string" ? d : d ? JSON.stringify(d) : "HTTP " + r.status);
  // answered after the user went to another page: the old page's code stops here (never resolves) instead of drawing over the new one
  if (nav !== navSeq && !opts.anyPage) return new Promise(() => { });
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
function pct(v, sign = true) { if (v == null) return "–"; if (Math.abs(v) < 0.05) return "0.0%"; return (sign && v > 0 ? "+" : "") + (+v).toFixed(1) + "%"; }
// a gain (positive = better): for lower-is-better goals said as faster / slower, so the sign never reads backwards
function gainTxt(g, lower) { if (g == null) return "–"; if (!lower) return pct(g); return Math.abs(g) < 0.05 ? "0.0%" : `${Math.abs(g).toFixed(1)}% ${g > 0 ? "faster" : "slower"}`; }
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
function statusBadge(s, why) { const m = { active: "good", measuring: "info", queued: "plain", done: "plain", closed: "plain", abandoned: "warn", aborted: "warn", stalled: "bad" }; return `<span class="badge ${m[s] || "plain"}"${why ? ` data-tip="${esc(why)}" tabindex="0"` : ""}>${esc(s)}</span>`; }
function sevBadge(s) { const m = { high: "bad", medium: "warn", low: "plain" }; return `<span class="badge ${m[s] || "plain"}">${esc(s)}</span>`; }
function riskBadge(r) { const m = { none: "good", low: "good", compat: "warn", observability: "warn", durability: "bad" }; return `<span class="badge ${m[r] || "plain"}">risk: ${esc(r)}</span>`; }
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function setNav(active) {
  const nav = document.getElementById("nav");
  if (!ME) { nav.innerHTML = `<a class="right" href="#/login">Log in</a><a href="#/register">Create account</a>`; return; }
  if (ME.share) {
    document.body.classList.add("ro");
    const home = "#/" + (ME.share.kind === "study" ? "study/" : "env/") + ME.share.id;
    nav.innerHTML = `<a href="${home}" class="on">${ME.share.kind === "study" ? "Study" : "Environment"}: ${esc(ME.share.name || "")}</a><span class="right"><span class="badge plain" data-tip="You are looking at a read-only shared view. Nothing can be changed from here." tabindex="0">read-only shared view</span></span><a href="#" id="leaveShare">Leave</a>`;
    document.getElementById("leaveShare").onclick = e => { e.preventDefault(); try { sessionStorage.removeItem("shareToken"); } catch (x) { } ME = null; location.hash = "#/"; loadMe().then(route); };
    return;
  }
  document.body.classList.remove("ro");
  nav.innerHTML = `<a href="#/" class="${active === "envs" ? "on" : ""}">Environments</a><a href="#/studies" class="${active === "studies" ? "on" : ""}">Studies</a><a href="#/connect" class="${active === "connect" ? "on" : ""}">Connect agent</a>` +
    `<a href="#/parameters" class="${active === "parameters" ? "on" : ""}">Parameters</a><a href="#/policies" class="${active === "policies" ? "on" : ""}">Policies</a><a href="#/notes" class="${active === "notes" ? "on" : ""}" data-tip="Runs you starred or gave feedback on">★ Starred</a>${ME.is_admin ? `<a href="#/learning" class="${active === "learning" ? "on" : ""}">Learning</a><a href="/status">Status</a>` : ""}<a href="#/account" class="${active === "account" ? "on" : ""}">Account</a><span class="right">${esc(ME.email)}</span><a href="#" id="logout">Log out</a>`;
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
  const t = (setAt ? ` · since ${ago(setAt)}` : "") + (left != null && left > 0 ? ` · ~${left} min left` : left === 0 ? " · due now" : left != null && left < 0 ? ` · overdue by ${-left} min` : "");
  const cls = stale ? "warn" : a.phase === "measuring" ? "good" : "info";
  const tip = stale ? "This phase should have ended more than 15 minutes ago and the agent has not reported anything since: check the host."
    : a.source === "derived" ? "Derived from the session's latest run (the agent running this session predates live activity reports)." : "Reported by the tuning-agent.";
  return `<div style="font-size:12px;margin-top:3px"><span class="badge ${cls}" data-tip="${esc(tip)}" tabindex="0">${esc(a.phase)}</span> <span class="muted">${esc(a.detail || "")}${t}</span></div>`;
}
function envStatus(e) {
  if (e.archived) return `<span class="badge plain" data-tip="${e.archived_how === "auto" ? `No contact from the agent since ${ago(e.last_seen)} and no active session: archived automatically; back in the active list on the agent's next report.` : `Archived ${ago(e.archived_at)}.`} Listed under All; its sessions and results are kept." tabindex="0">archived</span>`;
  const stopped = ["stopped", "shutdown"].includes(e.agent_state) && (e.agent_state_at || 0) >= (e.last_seen || 0);
  if (stopped && e.agent_state === "shutdown") return `<span class="badge plain" data-tip="The host shut down ${ago(e.agent_state_at)} (powered off or deleted); the agent said goodbye on the way down. Archived automatically after 3 days without contact." tabindex="0">host shut down</span>`;
  if (e.online && !stopped) return '<span class="badge good">online</span>';
  if (stopped) return `<span class="badge plain" data-tip="The tuning-agent service was stopped on the host ${ago(e.agent_state_at)} (systemctl stop), not a loss of contact." tabindex="0">agent stopped</span>`;
  return `<span class="badge warn" data-tip="Last report from the agent ${ago(e.last_seen)}: host down, agent crashed or network problem." tabindex="0">offline · ${ago(e.last_seen)}</span>`;
}
async function envList() {
  setNav("envs");
  const d = await api("/api/environments");
  if (!d.environments.length) {
    $view.innerHTML = `<h1>Environments</h1><p class="sub">An environment is one database server with a connected tuning-agent.</p>
    <div class="card"><h3>No environment connected yet</h3><p>Install the agent on the database host to see it here.</p><a class="btn primary" href="#/connect">Connect an agent</a></div>`;
    return;
  }
  setTitle("Environments");
  // in use first (online or with an active session), then by last contact
  const live = e => e.online || e.active_session ? 1 : 0;
  const allEnvs = d.environments.slice().sort((a, b) => (!!a.archived - !!b.archived) || live(b) - live(a) || (b.last_seen || 0) - (a.last_seen || 0) || a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
  let mode = "list", scope = "active";
  try { mode = localStorage.getItem("envView") || "list"; scope = localStorage.getItem("envScope") || "active"; } catch (e) { }
  const nActive = allEnvs.filter(e => !e.archived).length;
  let envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.archived);
  $view.innerHTML = `<div class="row"><div><h1>Environments</h1><p class="sub">Database servers with a connected tuning-agent.</p></div><span class="spacer"></span>
    <button class="ghost mut" id="archGone" hidden></button>
    ${allEnvs.length > nActive ? `<div class="seg" role="group" aria-label="Show" id="scopeSeg"><button data-s="active">Recent (${nActive})</button><button data-s="all">All (${allEnvs.length})</button></div>` : ""}
    <div class="seg" role="group" aria-label="View" id="viewSeg"><button data-v="list">List</button><button data-v="cards">Cards</button></div><a class="btn" href="#/connect">Connect another</a></div><div id="archNote" class="note" hidden style="margin-bottom:10px"></div><div id="envs"></div>`;
  const box = document.getElementById("envs");
  // environments that are gone: host shut down, agent stopped, or silent for a day, with no active session - one click
  // (or one for all) archives them; archiving is undone from All -> the environment's Unarchive, or the Undo link here
  const gone = e => !e.archived && !e.active_session && (["stopped", "shutdown"].includes(e.agent_state) && (e.agent_state_at || 0) >= (e.last_seen || 0)
    || (!e.online && Date.now() / 1000 - (e.last_seen || 0) > 86400));
  const archive = async (list, on = true) => {
    await Promise.all(list.map(e => api("/api/environments/" + e.id, { method: "PATCH", body: { archived: on } })));
    list.forEach(e => { e.archived = on; if (on) e.archived_how = "manual"; });
    envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.archived);
    const note = document.getElementById("archNote");
    if (on) {
      note.hidden = false;
      note.innerHTML = `Archived ${list.length === 1 ? `<b>${esc(list[0].name)}</b>` : `${list.length} environments`}: listed under All, sessions and results kept. <a href="#" id="archUndo">Undo</a>`;
      document.getElementById("archUndo").onclick = ev => { ev.preventDefault(); archive(list, false); };
    } else note.hidden = true;
    draw();
  };
  const archBtn = e => gone(e) ? ` <button class="ghost mut small-btn" data-arch="${e.id}" data-tip="Archive: hide it from Recent. Sessions, runs and metrics are kept; Undo or Unarchive brings it back." tabindex="0">Archive</button>` : "";
  const metrics = e => { const l = (e.latest && e.latest.db) || {}, o = (e.latest && e.latest.os) || {};
    const dmb = o.disk_r_mbs != null ? (o.disk_r_mbs || 0) + (o.disk_w_mbs || 0) : null;
    return { tps: e.online ? num(l.tps) : "–", lat: e.online && l.avg_latency_ms != null ? num(l.avg_latency_ms) + " ms" : "–", cpu: e.online && o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–",
      disk: e.online && dmb != null ? num(dmb) + " MB/s" : "–", diskTip: e.online && dmb != null ? `read ${num(o.disk_r_mbs)} MB/s · write ${num(o.disk_w_mbs)} MB/s · ${num((o.disk_r_iops || 0) + (o.disk_w_iops || 0))} IOPS` : "" }; };
  const clients = e => (e.clients || []).length ? `<div class="muted" style="font-size:12px" tabindex="0" data-tip="Client hosts linked to this database server: ${esc(e.clients.map(c => c.name + (c.online ? " (online)" : " (offline)")).join(", "))}">+ ${e.clients.length} client${e.clients.length > 1 ? "s" : ""}: ${esc(e.clients.map(c => c.name).join(", "))}</div>` : "";
  const status = e => envStatus(e);
  let draw = () => {
    box.parentElement.querySelectorAll("#viewSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === mode));
    box.parentElement.querySelectorAll("#scopeSeg button").forEach(b => b.classList.toggle("on", b.dataset.s === scope));
    if (mode === "list") {
      box.innerHTML = `<div class="card tablewrap"><table data-opt="Database|Hardware|Data|TPS|Avg latency|Disk|Sessions"><thead><tr><th>Name</th><th>Status</th><th>Database</th><th>Hardware</th><th class="num">Data</th><th class="num">TPS</th><th class="num">Avg latency</th><th class="num">CPU</th><th class="num">Disk</th><th>Active session</th><th class="num">Sessions</th></tr></thead><tbody>${envs.map(e => {
        const m = metrics(e);
        return `<tr class="click" data-href="#/env/${e.id}"><td><a href="#/env/${e.id}" class="envname"><b>${esc(e.name)}</b></a>${clients(e)}</td><td>${status(e)}${archBtn(e)}</td><td>${esc(e.db_short || e.db)}</td><td>${e.cpus || "?"} CPU · ${bytes(e.ram_bytes)} · ${e.disk}</td><td class="num">${bytes(e.data_bytes)}</td>
          <td class="num">${m.tps}</td><td class="num">${m.lat}</td><td class="num">${m.cpu}</td><td class="num"${m.diskTip ? ` data-tip="${m.diskTip}" tabindex="0"` : ""}>${m.disk}</td><td>${e.active_session ? `<a href="#/session/${e.active_session.id}" class="sname" data-tip="${esc(e.active_session.name)}">#${e.active_session.id} ${esc(e.active_session.name)}</a>${activityLine(e.active_session.activity)}` : '<span class="muted">–</span>'}</td><td class="num">${e.sessions}</td></tr>`; }).join("")}</tbody></table></div>`;
      box.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = ev => { if (!ev.target.closest("a, button")) location.hash = tr.dataset.href; });
    } else {
      box.innerHTML = `<div class="grid g3">${envs.map(e => { const m = metrics(e);
        return `<a class="card" href="#/env/${e.id}" style="color:inherit;text-decoration:none"><div class="row"><b style="font-size:15px">${esc(e.name)}</b><span class="spacer"></span>${status(e)}${archBtn(e)}</div>
          <div class="muted" style="margin:4px 0 10px">${esc(e.db_short || e.db)}${(e.clients || []).length ? ` · + ${e.clients.length} client${e.clients.length > 1 ? "s" : ""}` : ""}<br>${e.cpus || "?"} CPUs · ${bytes(e.ram_bytes)} RAM · ${e.disk} · data ${bytes(e.data_bytes)}</div>
          <div class="row" style="gap:18px"><div><div class="muted" style="font-size:12px">TPS</div><b>${m.tps}</b></div><div><div class="muted" style="font-size:12px">Avg latency</div><b>${m.lat}</b></div>
          <div><div class="muted" style="font-size:12px">CPU busy</div><b>${m.cpu}</b></div><div${m.diskTip ? ` data-tip="${m.diskTip}" tabindex="0"` : ""}><div class="muted" style="font-size:12px">Disk</div><b>${m.disk}</b></div><div><div class="muted" style="font-size:12px">Sessions</div><b>${e.sessions}</b></div></div>
          ${e.active_session ? `<div style="margin-top:10px;font-size:13px;overflow-wrap:anywhere"><span class="muted">Session</span> #${e.active_session.id} ${esc(e.active_session.name)}${activityLine(e.active_session.activity)}</div>` : ""}</a>`; }).join("")}</div>`;
    }
  };
  const wireArch = () => {
    box.querySelectorAll("[data-arch]").forEach(b => b.onclick = ev => { ev.preventDefault(); ev.stopPropagation(); archive(allEnvs.filter(e => e.id === +b.dataset.arch)); });
    const g = allEnvs.filter(gone), bb = document.getElementById("archGone");
    bb.hidden = !g.length;
    bb.textContent = `Archive all stopped (${g.length})`;
    bb.dataset.tip = "Archive every environment whose host shut down, whose agent was stopped, or that has been silent for a day - none with an active session: " + g.map(e => e.name).join(", ");
    bb.tabIndex = 0;
    bb.onclick = () => { if (g.length && confirm(`Archive ${g.length} environment(s)? ${g.map(e => e.name).join(", ")}\nSessions and results are kept; Undo brings them back.`)) archive(g); };
  };
  const draw0 = draw;
  draw = () => { draw0(); wireArch(); };
  box.parentElement.querySelectorAll("#viewSeg button").forEach(b => b.onclick = () => { mode = b.dataset.v; try { localStorage.setItem("envView", mode); } catch (e) { } draw(); });
  box.parentElement.querySelectorAll("#scopeSeg button").forEach(b => b.onclick = () => {
    scope = b.dataset.s; try { localStorage.setItem("envScope", scope); } catch (e) { }
    envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.archived); draw();
  });
  draw();
  timers.push(setInterval(async () => {
    if (!document.getElementById("envs")) return;
    const d2 = await api("/api/environments"); const byId = Object.fromEntries(d2.environments.map(e => [e.id, e]));
    allEnvs.forEach((e, i) => { if (byId[e.id]) allEnvs[i] = byId[e.id]; });
    envs = scope === "all" ? allEnvs : allEnvs.filter(e => !e.archived); draw();
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
  setTitle("Studies");
  const oneKind = new Set(d.studies.map(st => st.kind)).size <= 1;  // the same kind on every row says nothing
  $view.innerHTML = `<div class="row"><div><h1>Studies</h1><p class="sub">Experiments across environments: compare hardware, measure scaling, find the minimum hardware for a target.</p></div><span class="spacer"></span><button class="primary" id="newStudy">New study</button></div>
  <div class="card" id="newForm" hidden><div class="grid g2" style="gap:10px">
    <label>Name<input id="nsName" placeholder="HammerDB 200W hardware scaling"></label>
    <label>Kind<select id="nsKind">${Object.entries(STUDY_KINDS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
    <label>Goal<select id="nsGoal"><option value="tps">Throughput</option><option value="latency">Avg response time</option></select></label>
    <label>Target${info("Minimum hardware studies: the goal value to reach (e.g. 100000 for NOPM, or 5 for 5 ms average latency).")}<input id="nsTarget" type="number" step="any" placeholder="optional"></label></div>
    <div class="row" style="margin-top:10px"><button class="primary" id="nsCreate">Create</button><span class="muted">Add sessions from each session page, or start them with <code>tuning-agent session start --study "name"</code>.</span></div></div>
  <div class="card tablewrap" style="margin-top:10px"><table data-opt="Goal|Target|Environments|Created"><thead><tr><th>#</th><th>Study</th>${oneKind ? "" : "<th>Kind</th>"}<th>Goal</th><th class="num">Target</th><th class="num">Environments</th><th class="num">Sessions</th><th>Created</th></tr></thead><tbody>
  ${d.studies.length ? d.studies.map(st => `<tr class="click" data-href="#/study/${st.id}"><td class="muted">${st.id}</td><td><a href="#/study/${st.id}"><b>${esc(st.name)}</b></a><div class="muted m-only" style="font-size:12px">${fmtTime(st.created)}${oneKind ? "" : " · " + (STUDY_KINDS[st.kind] || esc(st.kind))}</div></td>${oneKind ? "" : `<td>${STUDY_KINDS[st.kind] || esc(st.kind)}</td>`}<td>${goalName(st.goal || "tps")}</td><td class="num">${st.target != null ? num(st.target) : "–"}</td><td class="num">${st.environments}</td><td class="num">${st.sessions}${st.active ? ` <span class="badge info">${st.active} active</span>` : ""}</td><td>${fmtTime(st.created)}</td></tr>`).join("")
    : `<tr><td colspan="${oneKind ? 7 : 8}" class="muted">No studies yet.</td></tr>`}</tbody></table></div>`;
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
  setTitle("Study: " + st.name);
  const rows = d.sessions.map(s => {
    // every number of a row in its session's own goal direction (a study can mix throughput and latency sessions)
    const e = s.environment, r = s.results || {}, bs = (r.best_safe || {}).mean, bt = r.best && !r.best.safe ? r.best.mean : null;
    const sl = s.goal === "latency";
    return { s, e, r, bs, bt, sl, start: (r.start || {}).value, first: (r.first || {}).value, cpus: e.cpus || 0, cost: e.cost_month,
      gain: (r.best_safe || {}).gain_pct, tgain: r.best && !r.best.safe ? r.best.gain_pct : null,
      // an "active" session on a host that is gone will never finish
      stalled: s.status === "active" && (e.archived || e.agent_state === "shutdown"),
      meets: st.target == null || bs == null ? null : (sl ? bs <= st.target : bs >= st.target) };
  });
  rows.sort((a, b) => (a.cost || 0) - (b.cost || 0) || a.cpus - b.cpus || (a.e.ram_bytes || 0) - (b.e.ram_bytes || 0));
  const better = (a, b) => lat ? a < b : a > b;
  const mixedWl = new Set(rows.map(x => x.s.workload || x.s.goal_metric || "")).size > 1;
  const mixedGoal = new Set(rows.map(x => x.s.goal || "tps")).size > 1;
  const gainsOnly = mixedWl || mixedGoal;  // absolute results of different workloads or goals do not compare: gains only
  const short = x => (x.s.name.match(/: ([A-Z]{1,3}-?\d+(?:\.\w+)?) /) || x.s.name.match(/^([A-Z]{1,3}-\d+(?:\.\w+)?)\b/) || [])[1] || `${x.e.name} #${x.s.id}`;
  const sname = x => x.s.name.replace(/^Battery \d+: /, "");
  let verdict = "";
  const measured = rows.filter(x => x.bs != null);
  if (st.kind === "min-hardware") {
    if (st.target == null) verdict = `<span class="muted">Set a target to find the minimum hardware.</span>`;
    else {
      const ok = measured.filter(x => x.meets);
      verdict = ok.length ? `<b>${esc(ok[0].e.name)}</b> is the smallest environment reaching ${num(st.target)}: best production-safe ${num(ok[0].bs)} (${ok[0].e.cpus} CPU · ${bytes(ok[0].e.ram_bytes)}${ok[0].cost ? ` · $${num(ok[0].cost)}/month` : ""}). <a href="#/session/${ok[0].s.id}">Its configuration</a>.`
        : measured.length ? `No environment reaches ${num(st.target)} yet; closest: <b>${esc(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).e.name)}</b> with ${num(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).bs)}.` : "No results yet.";
    }
  } else if (gainsOnly && measured.length) {
    const g = measured.filter(x => x.gain != null).sort((a, b) => b.gain - a.gain);
    verdict = g.length ? `The sessions run different workloads${mixedGoal ? " and goals" : ""}, so results are compared as gains of the best production-safe configuration over each session's calibrated starting configuration. Largest: <b>${esc(sname(g[0]))}</b> ${gainTxt(g[0].gain, g[0].sl)}; smallest: ${esc(sname(g[g.length - 1]))} ${gainTxt(g[g.length - 1].gain, g[g.length - 1].sl)}.` : "No results yet.";
  } else if (measured.length >= 2) {
    const small = measured[0], big = measured[measured.length - 1];
    const hw = big.cpus && small.cpus ? big.cpus / small.cpus : null, perf = lat ? small.bs / big.bs : big.bs / small.bs;
    verdict = st.kind === "scaling" && hw ? `From <b>${esc(small.e.name)}</b> (${small.cpus} CPU) to <b>${esc(big.e.name)}</b> (${big.cpus} CPU): ${num(hw)}× the CPUs gave ${num(perf)}× the tuned result — scaling efficiency ${Math.round(100 * perf / hw)}%.${info("Scaling efficiency = result ratio / CPU ratio. 100% = linear scaling. Tuned = best production-safe configuration of each environment.")}`
      : `Best production-safe result: <b>${esc(measured.reduce((a, b) => better(a.bs, b.bs) ? a : b).e.name)}</b>.`;
  } else verdict = measured.length ? "Add sessions from more environments to compare." : "No results yet.";
  // machine cost: a machine created for the study counts its whole lifetime once; a shared (lab) host the time of its sessions
  const envCost = new Map();
  rows.forEach(x => { const c = envCost.get(x.e.id) || { sessions: 0, machine: x.s.env_dedicated && x.s.env_cost ? x.s.env_cost.total : null }; c.sessions += (x.s.cost || {}).total || 0; envCost.set(x.e.id, c); });
  const totalCost = [...envCost.values()].reduce((a, c) => a + (c.machine != null ? c.machine : c.sessions), 0);
  const anyCost = rows.some(x => x.s.cost || x.s.env_cost);
  const costTip = "Per row: the session's machine time × the hourly price. Total: machines created for this study count their whole lifetime once (setup and data load included); hosts that also ran other sessions count only this study's sessions. Per-run costs: session pages and the CSV.";
  const wlCount = new Set(rows.map(x => x.s.workload || x.s.goal_metric || "")).size;
  const resTip = gainsOnly ? "Gain = best production-safe configuration vs the session's calibrated starting configuration (its own goal: faster for response-time sessions). With trade-offs = the best configuration that gives up durability, replication or observability guarantees. Hover a gain for the absolute values."
    : "Start = the calibrated starting configuration of each session (calibration mean; server defaults in the tooltip). Tuned = best production-safe configuration; with trade-offs = best configuration that gives up durability, replication or observability guarantees. Values are means of all runs of a configuration. Click a cost to edit it.";
  const absTip = x => esc(`start ${num(x.start)} · tuned ${num(x.bs)}${x.first != null ? ` · server defaults (run #1) ${num(x.first)}${(x.r.best_safe || {}).gain_vs_first_pct != null ? `, ${pct(x.r.best_safe.gain_vs_first_pct)}` : ""}` : ""}`);
  $view.innerHTML = `<div class="crumbs"><a href="#/studies">Studies</a> /</div>
  <div class="row"><h1>${esc(st.name)}</h1><span class="badge plain" data-tip="${esc(STUDY_TIPS[st.kind] || "")}" tabindex="0">${STUDY_KINDS[st.kind] || esc(st.kind)}</span><span class="spacer"></span><button class="ghost owner" id="share">Share</button><button class="ghost" id="edit">Edit</button><button class="ghost danger" id="del">Delete</button></div>
  <div class="card mut" id="editForm" hidden style="margin-bottom:12px"><div class="grid g3" style="gap:10px">
    <label>Name<input id="esName" value="${esc(st.name)}"></label>
    <label>Kind<select id="esKind">${Object.entries(STUDY_KINDS).map(([k, v]) => `<option value="${k}" ${k === st.kind ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>Target${info("Minimum hardware studies: the goal value to reach. Empty for none.")}<input id="esTarget" type="number" step="any" value="${st.target != null ? st.target : ""}"></label></div>
    <div class="row" style="margin-top:10px"><button class="primary" id="esSave">Save</button><button class="ghost" id="esCancel">Cancel</button><span class="err" id="esErr"></span></div></div>
  <p class="sub">Goal ${goalName(st.goal || "tps")}${st.target != null ? ` · target <b>${num(st.target)}</b>` : ""}${mixedWl ? ` · mixed workloads (${wlCount})` : st.workload ? " · workload: " + esc(st.workload) : ""} · ${new Set(rows.map(x => x.e.id)).size} environment(s), ${rows.length} session(s)</p>
  <div class="note" style="margin-bottom:12px">${verdict}</div>
  <div class="card" id="sbars"></div>
  <h2>Results by environment${info(resTip)}</h2>
  <div class="card tablewrap"><table data-opt="Hardware|Cost / month|Start|With trade-offs|Per CPU|Per $|Study cost"><thead><tr><th>Environment</th><th>Hardware</th><th class="num">Cost / month</th><th>Session</th>${gainsOnly ? "" : '<th class="num">Start</th><th class="num">Tuned</th>'}<th class="num">Gain</th><th class="num">With trade-offs</th>${gainsOnly ? "" : `<th class="num">Per CPU</th>${rows.some(x => x.cost) ? '<th class="num">Per $</th>' : ""}`}${anyCost ? `<th class="num">Study cost</th>` : ""}${st.target != null ? "<th>Target</th>" : ""}<th></th></tr></thead><tbody>
  ${rows.length ? rows.map(x => `<tr><td><a href="#/env/${x.e.id}" class="envname"><b>${esc(x.e.name)}</b></a></td><td>${x.e.cpus || "?"} CPU · ${bytes(x.e.ram_bytes)} · ${x.e.disk}</td>
    <td class="num"><a href="#" class="cost" data-env="${x.e.id}" data-cost="${x.cost != null ? x.cost : ""}" data-tip="Machine price per month: click to edit" aria-label="Edit the price per month">${x.cost ? "$" + Math.round(x.cost) : '<span class="muted">–</span>'} <span class="muted mut">✎</span></a></td>
    <td><a href="#/session/${x.s.id}">#${x.s.id} ${esc(x.s.name)}</a> ${x.stalled ? statusBadge("stalled", `Still marked active, but its host ${x.e.archived ? "is archived" : "shut down"}: it will not finish. Close it with tuning-agent session stop, or delete it.`) : x.s.status === "active" ? activityLine(x.s.activity) : statusBadge(x.s.status)}</td>
    ${gainsOnly ? "" : `<td class="num"${x.first != null ? ` data-tip="${esc(`Server defaults (run #1): ${num(x.first)}`)}" tabindex="0"` : ""}>${num(x.start)}</td><td class="num"><b>${num(x.bs)}</b></td>`}
    <td class="num ${deltaCls(x.gain)}" data-tip="${absTip(x)}" tabindex="0">${gainTxt(x.gain, x.sl)}</td><td class="num">${gainsOnly ? (x.tgain != null ? gainTxt(x.tgain, x.sl) : "–") : x.bt != null ? num(x.bt) : "–"}</td>
    ${gainsOnly ? "" : `<td class="num">${x.bs != null && x.cpus ? num(x.sl ? x.bs * x.cpus : x.bs / x.cpus) : "–"}</td>${rows.some(y => y.cost) ? `<td class="num">${x.bs != null && x.cost ? num(x.sl ? x.bs * x.cost : x.bs / x.cost) : "–"}</td>` : ""}`}
    ${anyCost ? `<td class="num">${x.s.cost ? `<span data-tip="${esc(`${x.s.cost.hours} h × $${x.s.cost.rate_hourly.toFixed(4)}/h${x.s.env_cost ? ` · machine lifetime ${x.s.env_cost.hours} h = $${x.s.env_cost.total.toFixed(2)}${x.s.env_cost.destroyed ? " (destroyed)" : ""}` : ""}`)}" tabindex="0">$${x.s.cost.total.toFixed(2)}</span>` : "–"}</td>` : ""}
    ${st.target != null ? `<td>${x.meets == null ? "–" : x.meets ? '<span class="badge good">meets</span>' : '<span class="badge plain">below</span>'}</td>` : ""}
    <td><a href="#" class="detach" data-sid="${x.s.id}" data-tip="Remove from the study (the session stays in its environment)" aria-label="Remove #${x.s.id} from the study">×</a></td></tr>`).join("") : `<tr><td colspan="12" class="muted">No sessions yet.</td></tr>`}</tbody></table></div>
  ${anyCost ? `<p class="muted" style="margin:6px 0 0">Total machine cost $${totalCost.toFixed(2)}${info(costTip)} · <a href="/api/studies/${id}/costs?format=csv" download="study-${id}-costs.csv">cost per run (CSV)</a></p>` : ""}
  <div class="row" style="margin-top:8px"><select id="addSess" style="width:auto"><option value="">Add a session…</option>${d.other_sessions.map(o => `<option value="${o.id}">#${o.id} ${esc(o.env_name)} · ${esc(o.name)}</option>`).join("")}</select>
   <span class="muted">or <code>tuning-agent session start --study "${esc(st.name)}" ...</code> on each environment</span></div>
  <h2>Notes</h2><div class="card"><textarea id="snotes" rows="3" placeholder="Hypothesis, method, conclusions">${esc(st.notes || "")}</textarea><div class="row" style="margin-top:6px"><button id="savesn">Save notes</button><span id="snsaved" class="muted"></span></div></div>`;
  const drawBars = () => {
    const box = document.getElementById("sbars"); if (!box) return; box.innerHTML = "";
    if (gainsOnly) C.barChart(box, {
      title: "Gain of the best production-safe configuration over each session's calibrated start (%)", unit: "%", metric: "gain", diverging: true,
      legend: [["var(--s3)", "gain"], ["var(--crit)", "loss"]],
      bars: rows.filter(x => x.gain != null).map(x => ({ label: sname(x), short: short(x),
        value: x.gain, sub: gainTxt(x.gain, x.sl), href: "#/session/" + x.s.id, highlight: "best",
        tip: `<div>start<b>${num(x.start)}</b></div><div>tuned<b>${num(x.bs)}</b></div>` })),
    });
    else C.barChart(box, {
      title: `Tuned result per environment (${goalName(st.goal || "tps")}${lat ? ", lower is better" : ""})`, unit: "", metric: "tuned",
      legend: st.target != null ? [["var(--s3)", "meets the target"], ["var(--axis)", "below the target"]] : [],
      ref: st.target, refLabel: "target", bars: rows.map(x => ({ label: x.e.name + (x.cpus ? ` (${x.cpus} CPU)` : ""), value: x.bs, sub: x.start ? "start " + num(x.start) : "", href: "#/session/" + x.s.id, highlight: x.meets ? "best" : false })),
    });
  };
  drawBars(); widthFns.push(drawBars);
  $view.querySelectorAll("a.cost").forEach(a => a.onclick = async ev => { ev.preventDefault(); if (ME && ME.share) return; const v = prompt("Cost per month in USD (empty to clear)", a.dataset.cost); if (v === null) return; await api("/api/environments/" + a.dataset.env, { method: "PATCH", body: { cost_month: v } }); studyPage(id); });
  $view.querySelectorAll("a.detach").forEach(a => a.onclick = async ev => { ev.preventDefault(); if (!confirm(`Remove session #${a.dataset.sid} from this study? The session and its runs stay in their environment; you can add it back from the list below.`)) return; await api("/api/sessions/" + a.dataset.sid, { method: "PATCH", body: { study_id: null } }); studyPage(id); });
  document.getElementById("addSess").onchange = async e => { if (!e.target.value) return; await api("/api/sessions/" + e.target.value, { method: "PATCH", body: { study_id: id } }); studyPage(id); };
  document.getElementById("share").onclick = ev => sharePanel("study", id, ev.target);
  document.getElementById("savesn").onclick = async () => { await api("/api/studies/" + id, { method: "PATCH", body: { notes: document.getElementById("snotes").value } }); document.getElementById("snsaved").textContent = "saved"; };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this study? Its sessions stay in their environments.")) { await api("/api/studies/" + id, { method: "DELETE" }); location.hash = "#/studies"; } };
  const ef = document.getElementById("editForm");
  document.getElementById("edit").onclick = () => { ef.hidden = !ef.hidden; };
  document.getElementById("esCancel").onclick = () => { ef.hidden = true; };
  document.getElementById("esSave").onclick = async () => {
    const name = document.getElementById("esName").value.trim(); if (!name) return;
    try { await api("/api/studies/" + id, { method: "PATCH", body: { name, kind: document.getElementById("esKind").value, target: document.getElementById("esTarget").value } }); studyPage(id); }
    catch (err) { document.getElementById("esErr").textContent = err.message; }
  };
}

async function connectPage() {
  setNav("connect");
  const keys = await api("/api/keys");
  const fresh = sessionStorage.getItem("newkey");
  const keyShown = fresh || "YOUR_API_KEY";
  const cmd = `curl -fsSL ${SERVER}/install.sh | sudo bash -s -- --api-key ${keyShown} --name my-db`;
  setTitle("Connect agent");
  const copyBtn = '<button class="ghost" data-copypre style="float:right;margin:-2px 0 0 8px;padding:2px 8px;font-size:12px">Copy</button>';
  $view.innerHTML = `<h1>Connect a tuning-agent</h1><p class="sub">Run this on the database host (Linux, Python 3.8+, systemd). It installs the agent, registers the environment and starts the metrics collector.</p>
  ${fresh ? `<div class="card" style="margin-bottom:14px"><b>Your new API key</b> — shown once, store it somewhere safe:<div class="key" style="margin-top:6px">${esc(fresh)}</div></div>` : ""}
  <h2>1. Install</h2><pre>${copyBtn}<code>${esc(cmd)}</code></pre>
  <p class="muted">The agent connects to MySQL as <code>root</code> over the local socket by default. Add <code>--mysql-user U --mysql-password P</code> (or <code>--mysql-host</code>/<code>--mysql-port</code>) for other accounts; it needs <code>PROCESS</code>, <code>SELECT</code> on <code>performance_schema</code> and <code>SYSTEM_VARIABLES_ADMIN</code> to apply changes. Add <code>--no-restart</code> to forbid server restarts.</p>
  <h2>2. Tune</h2><pre>${copyBtn}<code>tuning-agent baseline --apply          # hardware + version based starting point
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
  $view.querySelectorAll("[data-copypre]").forEach(b => b.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(b.parentElement.querySelector("code").textContent); b.textContent = "Copied"; });
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
const FAMILY = { mysql: "MySQL", percona: "Percona", mariadb: "MariaDB", pxc: "PXC" };
function releasesCell(p) {
  const v = p.versions;
  if (!v) return "";
  const all = (v.series === "all" ? [] : v.series).concat(v.missing || []);
  const fams = {};
  all.forEach(s => { const [f, n] = s.split(" "); (fams[f] = fams[f] || { all: [], have: [] }).all.push(n); });
  if (v.series !== "all") v.series.forEach(s => { const [f, n] = s.split(" "); fams[f].have.push(n); });
  const label = v.series === "all" ? "all" : Object.entries(fams).filter(([, x]) => x.have.length)
    .map(([f, x]) => FAMILY[f] + (x.have.length < x.all.length ? " " + x.have.join("/") : "")).join(", ") || "none";
  const per = (k, fmt) => { if (typeof v[k] !== "object" || v[k] === null || Array.isArray(v[k])) return null;
    const g = {}; Object.entries(v[k]).forEach(([s, x]) => (g[fmt(x)] = g[fmt(x)] || []).push(s));
    return Object.entries(g).map(([x, ss]) => `${x} in ${ss.join(", ")}`).join("; "); };
  const defs = per("default", x => x), dyn = per("dynamic", x => x ? "online" : "restart");
  const tip = [v.series === "all" ? "In every supported LTS series." : `In: ${v.series.join(", ")}. Not in: ${(v.missing || []).join(", ")}.`,
    defs ? `Defaults differ: ${defs}.` : v.default != null ? `Default: ${v.default}.` : "",
    dyn ? `Change needs: ${dyn}.` : ""].filter(Boolean).join(" ");
  const n = v.series === "all" ? null : v.series.length, of = n == null ? null : n + (v.missing || []).length;
  return `<span data-tip="${esc((v.series === "all" ? "" : "Releases: " + label + ". ") + tip + " Measured on the official images of every supported LTS series.")}" tabindex="0">${v.series === "all" ? "all releases" : `${n} of ${of} release series`}</span>${defs ? ' <span class="badge plain" data-tip="' + esc("Default differs between releases: " + defs) + '" tabindex="0">defaults</span>' : ""}`;
}
let paramAll = false;  // the full list instead of the first 100 rows
async function parametersPage() {
  setNav("parameters");
  setTitle("Parameters");
  const d = await api("/api/parameters");
  const st = (() => { try { return JSON.parse(sessionStorage.getItem("paramFilter") || "{}"); } catch (x) { return {}; } })();
  const q = (st.q || "").toLowerCase(), fi = st.impact || "", fs = st.scope || "", fr = st.release || "";
  const rank = { high: 0, medium: 1, low: 2, none: 3, unknown: 4, container: 5 };
  const allSeries = d.series || [];
  const inSeries = (p, s) => !p.versions || p.versions.series === "all" || (p.versions.series || []).includes(s);
  const rows = d.parameters.filter(p => (!q || (p.name + " " + p.description + " " + (p.when || "")).toLowerCase().includes(q))
    && (!fi || p.effective_impact === fi) && (!fs || p.scope === fs) && (!fr || (p.scope === "mysql" && inSeries(p, fr))))
    .sort((a, b) => {
      const k = st.sort || "impact", dir = st.desc ? -1 : 1;
      const key = p => ({ name: p.name, impact: rank[p.effective_impact] ?? 3, when: p.when || "~", safety: (p.safety === "safe" ? "0" : "1") + (p.safety || ""),
        restart: p.restart ? 1 : 0, policy: p.mode + (p.user_mode ? "0" : "1"), def: p.default_mode })[k];
      const x = key(a), y = key(b);
      return (x < y ? -1 : x > y ? 1 : 0) * dir || ((rank[a.effective_impact] ?? 3) - (rank[b.effective_impact] ?? 3)) || a.name.localeCompare(b.name);
    });
  const th = (k, label) => `<th class="sortable" data-sort="${k}" style="cursor:pointer;white-space:nowrap">${label}${(st.sort || "impact") === k ? (st.desc ? " ▾" : " ▴") : ""}</th>`;
  const impBadge = p => { const l = p.learned && p.learned.impact !== "unknown" ? p.learned : null;
    const tip = `Shipped estimate: ${p.impact}${p.prior_source === "documentation" ? " (from the server documentation, not measured yet)" : ""}` + (l ? `. Measured: ${l.impact} - ${l.runs} single-setting experiments in ${l.sessions} session(s): ${l.improved} improved, ${l.regressed} regressed, ${l.neutral} within noise; median change of the decisive ones ${l.median_abs_delta_pct}% (max ${l.max_abs_delta_pct}%). A summary of experiment data, not a law.` : ". Not measured often enough yet to replace the estimate.")
      + (p.learned_by_workload ? " By workload: " + Object.entries(p.learned_by_workload).map(([w, v]) => `${w} ${v.impact} (${v.runs} runs)`).join(", ") + "." : "");
    // a neutral scale: a high impact is neither good nor bad
    const dots = { high: "●●●", medium: "●●○", low: "●○○", none: "○○○" }[p.effective_impact];
    return `<span class="badge plain" data-tip="${esc(tip)}" tabindex="0">${dots ? `<span class="dots" aria-hidden="true">${dots}</span>` : ""}${esc(p.effective_impact)}${l ? " · measured" : ""}</span>`; };
  const polSel = (p, admin) => { const cur = admin ? p.default_mode : (p.user_mode || "default");
    const opts = admin ? ["tune", "block"] : ["default", "tune", "block"];
    return `<select data-${admin ? "def" : "pol"}="${esc(p.name)}" aria-label="${admin ? "Service default" : "Your policy"} for ${esc(p.name)}" style="font-size:12px;width:auto">${opts.map(o => `<option value="${o}" ${o === cur ? "selected" : ""}>${o === "default" ? `default (${p.default_mode})` : o}</option>`).join("")}</select>`; };
  $view.innerHTML = `<h1>Parameters${info("The MySQL and operating-system settings the tuner works with. Impact: how much a setting can move results when it counts (shipped estimate; replaced by the measured impact once enough single-setting experiments exist - a summary of experiment data, not a law). When it counts: the conditions or measured bottleneck in which it matters. Precondition: what must happen in the workload for it to act at all (otherwise the tuner does not try it). Safety: safe, or what a value gives up (durability, observability, replication, semantics). Your policy: the tuner never changes a blocked setting in your sessions; 'default' follows the service default.")}</h1>
  <div class="row" style="margin-bottom:8px;gap:8px;flex-wrap:wrap"><input id="pq" placeholder="search" value="${esc(st.q || "")}" style="max-width:260px" aria-label="Search parameters">
   <select id="pimp" style="width:auto"><option value="">any impact</option>${["high", "medium", "low", "none", "unknown", "container"].map(x => `<option ${x === fi ? "selected" : ""}>${x}</option>`).join("")}</select>
   <select id="prel" style="width:auto"><option value="">any release</option>${allSeries.map(x => `<option ${x === fr ? "selected" : ""}>${x}</option>`).join("")}</select>
   <select id="pscope" style="width:auto"><option value="">MySQL and OS</option>${["mysql", "os", "pseudo"].map(x => `<option ${x === fs ? "selected" : ""}>${x}</option>`).join("")}</select>
   <span class="muted" style="font-size:12.5px">${rows.length} of ${d.parameters.length} · blocked for you: ${d.parameters.filter(p => p.mode === "block").length}</span></div>
  <div class="card tablewrap"><table data-opt="When it counts"><thead><tr>${th("name", "Parameter")}${th("impact", "Impact")}${th("when", "When it counts")}${th("safety", "Safety")}${th("policy", d.admin ? "Policy: yours / service default" : "Your policy")}</tr></thead><tbody>
  ${(paramAll ? rows : rows.slice(0, 100)).map(p => `<tr><td class="mono">${esc(p.name)}${info((p.description || "") + (p.precondition ? ` Precondition: ${p.precondition}.` : "") + (p.rules && p.rules.length ? ` Tuner rules: ${p.rules.join(", ")}.` : "") + (p.mechanism_tags && p.mechanism_tags.length ? ` Acts on: ${p.mechanism_tags.join(", ")}.` : "") + (p.memory_cost ? ` Memory: ${p.memory_cost}.` : "") + (p.response ? ` Response: ${p.response.shape}${p.response.note ? " - " + p.response.note : ""} (expected; validated per workload by sweeps).` : ""))}${p.response && ["saturating", "optimum"].includes(p.response.shape) ? ` <span class="badge plain" data-tip="${esc(p.response.shape === "saturating" ? "More is better up to a point: the tuner steps upward and stops once a step no longer helps beyond noise (or at the memory budget / data size)." : "Too little and too much both hurt: the tuner tries both sides of the current value, then one more step towards the better side.")}" tabindex="0">${p.response.shape}</span>` : ""}${p.memory_cost ? ' <span class="badge plain" data-tip="' + esc("Memory cost: " + p.memory_cost + ". The tuner keeps every configuration within the host's memory budget; one that would exceed it is a memory trade-off, never production-safe.") + '" tabindex="0">memory</span>' : ""}${p.versions ? `<div class="muted" style="font-size:12px;font-family:system-ui, -apple-system, &quot;Segoe UI&quot;, sans-serif;white-space:normal">${releasesCell(p)}</div>` : ""}</td>
    <td>${impBadge(p)}</td><td style="font-size:12.5px;max-width:260px;min-width:160px">${esc(p.when || "")}</td>
    <td>${p.safety === "safe" ? '<span class="badge good">safe</span>' : `<span class="badge bad" ${p.safety_note ? `data-tip="${esc(p.safety_note)}"` : ""} tabindex="0">${esc(p.safety || "?")}</span>`}${p.restart ? '<div style="margin-top:3px"><span class="badge plain" data-tip="Changing it needs a server restart." tabindex="0">restart</span></div>' : ""}</td>
    <td style="white-space:nowrap">${polSel(p, false)}${d.admin ? `<div style="margin-top:3px">${polSel(p, true)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>
  ${!paramAll && rows.length > 100 ? `<div style="margin-top:8px"><button id="pall">Show all ${rows.length}</button></div>` : ""}`;
  const save = (extra) => { try { sessionStorage.setItem("paramFilter", JSON.stringify(Object.assign({ q: document.getElementById("pq").value, impact: document.getElementById("pimp").value, scope: document.getElementById("pscope").value, release: document.getElementById("prel").value, sort: st.sort, desc: st.desc }, extra || {}))); } catch (x) { } parametersPage(); };
  $view.querySelectorAll("th[data-sort]").forEach(el => el.onclick = () => { const k = el.dataset.sort; save({ sort: k, desc: (st.sort || "impact") === k ? !st.desc : false }); });
  // search as you type (debounced); the box keeps the focus and the caret across the redraw
  const pq = document.getElementById("pq"); let pqT;
  pq.oninput = () => { clearTimeout(pqT); pqT = setTimeout(() => { try { sessionStorage.setItem("paramFocus", "1"); } catch (x) { } save(); }, 300); };
  try { if (sessionStorage.getItem("paramFocus")) { sessionStorage.removeItem("paramFocus"); pq.focus(); pq.setSelectionRange(pq.value.length, pq.value.length); } } catch (x) { }
  const pall = document.getElementById("pall"); if (pall) pall.onclick = () => { paramAll = true; parametersPage(); };
  document.getElementById("pimp").onchange = () => save(); document.getElementById("pscope").onchange = () => save(); document.getElementById("prel").onchange = () => save();
  $view.querySelectorAll("select[data-pol]").forEach(el => el.onchange = async () => { await api(`/api/parameters/${encodeURIComponent(el.dataset.pol)}/policy`, { method: "PUT", body: { mode: el.value } }); parametersPage(); });
  $view.querySelectorAll("select[data-def]").forEach(el => el.onchange = async () => { await api(`/api/admin/parameters/${encodeURIComponent(el.dataset.def)}/policy`, { method: "PUT", body: { mode: el.value } }); parametersPage(); });
}

// ------------------------------------------------------------------ learning (admin)
async function learningPage() {
  setNav("learning");
  setTitle("Learning");
  if (!ME.is_admin) { $view.innerHTML = '<p class="muted">Administrators only.</p>'; return; }
  const [d, L] = await Promise.all([api("/api/admin/learning"), api("/api/admin/learner")]);
  const st = (sessions) => sessions.map(s => `<a href="#/session/${s}">#${s}</a>`).join(" ");
  const pct = v => v == null ? "" : (v > 0 ? "+" : "") + v + "%";
  // long lists are drawn a page at a time ("Show more"); the page is redrawn from the data already loaded
  const lim = { passes: 5, insights: 50, proposals: 30, experience: 50, analyses: 20 };
  let filt = (() => { try { return sessionStorage.getItem("learnFilter") || ""; } catch (x) { return ""; } })();
  let insStatus = "live";
  const more = (key, shown, total) => total > shown ? `<div style="margin-top:8px"><button data-more="${key}">Show ${Math.min(total - shown, lim[key])} more</button> <span class="muted" style="font-size:12.5px">${shown} of ${total}</span></div>` : "";
  const draw = () => {
    const ex = d.experience.filter(e => !filt || (e.idea + " " + e.workload).toLowerCase().includes(filt.toLowerCase()));
    // passes that read nothing, called nothing and failed nothing are counted, not listed
    const busy = L.runs.filter(x => x.calls || x.sessions || x.insights_new || x.insights_updated || x.proposals_new || x.error || !x.ended);
    const idle = L.runs.length - busy.length;
    const ins = L.insights.filter(x => insStatus === "all" || (insStatus === "live" ? x.status !== "archived" : x.status === insStatus));
    const props = L.proposals.slice().sort((a, b) => (a.status === "open" ? 0 : 1) - (b.status === "open" ? 0 : 1));
    $view.innerHTML = `<h1>Learning${info("What the tuner has learned across all sessions and accounts. Agents receive the part that matches their own account and conditions (workload type, CPU class, concurrency class) to rank experiments; ideas that keep losing in comparable sessions are tried last. Hypothesis outcomes per targeted component set how credible an idea is for a given bottleneck. Bottleneck analyses are reused for similar conditions. Snapshots freeze this view as a downloadable file.")}</h1>
  <p class="muted">${d.runs} measured runs behind it.</p>
  <h3>Learner${info("A background process that reads every closed session (observations, tuner.db), asks the service's AI model what it teaches, and keeps the result in the separate learnings database (learning.db): insights with the conditions they hold in, and proposals for the tuner's code. Insights are summaries of experiment data, not truths: each is counted against the observations - runs that changed the same settings under the same conditions - and its status says how well it repeated (observed, repeated, contested, contradicted). Agents receive matching insights as leads to test; only repeated ones nudge the ranking. The evidence gate still decides what runs.")}</h3>
  <div class="card"><div class="row"><span>Every ${L.interval_hours} h · ${L.processed} sessions read · ${L.insights.filter(x => x.status !== "archived").length} live insights (${L.insights.filter(x => x.status === "archived").length} archived) · ${L.proposals.filter(p => p.status === "open").length} open proposals</span><span class="spacer"></span><button class="btn" id="pruneNow" data-tip="Recount every insight against the observations and archive the stale ones: observations that never repeated within 14 days, contradicted results after 7 days, and results resting only on flawed runs (not steady, unreliable, saturated client) or on pre-fix sessions (counted at half weight).">Prune now</button><button class="btn" id="learnNow">Run the learner now</button></div>
  ${busy.length ? `<div class="tablewrap"><table style="margin-top:8px"><thead><tr><th>Pass</th><th>Started (UTC)</th><th>Trigger</th><th class="num">Sessions</th><th class="num">AI calls</th><th class="num">Cost $</th><th class="num">New / updated insights</th><th class="num">Proposals</th><th>Result</th></tr></thead><tbody>${busy.slice(0, lim.passes).map(x => `<tr><td>#${x.id}</td><td>${new Date(x.started * 1000).toISOString().slice(0, 16).replace("T", " ")}</td><td>${esc(x.trigger || "")}</td><td class="num">${x.sessions}</td><td class="num">${x.calls}</td><td class="num">${(x.cost || 0).toFixed(3)}</td><td class="num">${x.insights_new} / ${x.insights_updated}</td><td class="num">${x.proposals_new}</td><td>${x.ended ? (x.error ? `<span class="badge warn" data-tip="${esc(x.error)}" tabindex="0">error</span>` : '<span class="badge good">done</span>') : '<span class="badge plain">running</span>'}</td></tr>`).join("")}</tbody></table></div>${more("passes", Math.min(lim.passes, busy.length), busy.length)}` : '<p class="muted" style="margin:6px 0 0">No pass with work yet.</p>'}
  ${idle ? `<p class="muted" style="margin:6px 0 0;font-size:12.5px">${idle} idle pass${idle > 1 ? "es" : ""} (nothing new to read)</p>` : ""}</div>
  <h3>Insights${info("Summaries of experiment data, not truths: each says what was measured, in which conditions, on how many sessions and hosts, with its caveats. A result may not repeat, may hold only in narrow conditions, or may come from a single host. Status says how well it repeated - observed (one session), repeated (supported in 2+ sessions by 2:1 of the runs), contested (runs disagree), contradicted (more against than for) - counted from the observations, not taken from the AI. Agents see them as earlier results to test; only repeated ones nudge the ranking. Mark one 'implemented' once the tuner's code covers it.")}</h3>
  <div class="row" style="margin-bottom:6px"><select id="insStatus" style="width:auto" aria-label="Insight status">${[["live", "live (not archived)"], ["all", "all"], ...["observed", "repeated", "contested", "contradicted", "implemented", "retired-by-admin", "archived"].map(x => [x, x])].map(([k, t]) => `<option value="${k}" ${k === insStatus ? "selected" : ""}>${t} (${k === "all" ? L.insights.length : k === "live" ? L.insights.filter(x => x.status !== "archived").length : L.insights.filter(x => x.status === k).length})</option>`).join("")}</select></div>
  <div class="card tablewrap"><table><thead><tr><th>Status</th><th>Statement</th><th>Settings</th><th>Conditions</th><th class="num">runs for / against</th><th class="num">sessions · hosts</th><th>Sessions</th><th></th></tr></thead><tbody>
  ${ins.slice(0, lim.insights).map(x => `<tr><td><span class="badge ${x.status === "repeated" ? "good" : ["contradicted", "contested", "retired-by-admin"].includes(x.status) ? "warn" : "plain"}" ${x.notes ? `data-tip="${esc(x.notes)}" tabindex="0"` : ""}>${esc(x.status)}</span></td><td style="max-width:420px">${esc(x.statement)} <span class="muted">(${esc(x.direction)})</span>${x.caveats ? info("Caveats: " + x.caveats) : ""}</td><td class="mono" style="font-size:12px">${esc((x.settings || []).map(t => t.variable + "=" + t.value).join(", "))}</td><td style="font-size:12px">${esc([x.conditions.workload, x.conditions.cpu_class && x.conditions.cpu_class + " CPUs", x.conditions.concurrency_class && x.conditions.concurrency_class + " conns"].filter(Boolean).join(", "))}</td><td class="num">${x.support} / ${x.contradict}</td><td class="num">${(x.sessions || []).length} · ${x.hosts ?? "?"}</td><td style="font-size:12px">${(x.sessions || []).slice(-6).map(t => `<a href="#/session/${t}">#${t}</a>`).join(" ")}</td><td style="white-space:nowrap"><select data-ins="${x.id}" data-was="${esc(x.status)}" style="font-size:12px;width:auto" aria-label="Status of insight ${x.id}">${["observed", "repeated", "contested", "contradicted", "implemented", "retired-by-admin", "archived"].map(o => `<option ${o === x.status ? "selected" : ""}>${o}</option>`).join("")}</select> <span class="muted" data-saved style="font-size:12px"></span></td></tr>`).join("") || '<tr><td colspan="8" class="muted">none</td></tr>'}</tbody></table></div>${more("insights", Math.min(lim.insights, ins.length), ins.length)}
  <h3>Proposals for the tuner's code${info("What the learner thinks the tuner itself should change: a missing measurement, a wrong decision, a rule firing without evidence, a harness problem. Reviewed by people; accepted ones go into the code releases. Open ones first; click a title for the details.")}</h3>
  <div class="card">${props.slice(0, lim.proposals).map(p => `<details class="rec"><summary style="margin:0"><span class="row" style="display:inline-flex;gap:8px;vertical-align:middle"><b style="color:var(--ink)">${esc(p.title)}</b><span class="badge plain">${esc(p.kind || "")}</span><span class="badge ${p.status === "open" ? "info" : "plain"}">${esc(p.status)}</span></span></summary>
    <div class="row" style="margin:4px 0"><label style="margin:0;display:flex;gap:6px;align-items:center;width:auto">Status <select data-prop="${p.id}" style="font-size:12px;width:auto">${["open", "accepted", "rejected", "done"].map(o => `<option ${o === p.status ? "selected" : ""}>${o}</option>`).join("")}</select></label><span class="muted" data-saved style="font-size:12px"></span></div>
    ${p.proposal ? `<div style="margin:8px 0 0;padding:8px 10px;border-left:3px solid var(--accent);background:var(--soft);border-radius:4px"><b>Proposed change:</b> ${esc(p.proposal)}${p.expected_effect ? `<div class="muted" style="margin-top:4px;font-size:12.5px">Expected effect: ${esc(p.expected_effect)}</div>` : ""}</div>` : ""}
    <p style="margin:8px 0 0"><span class="muted">${p.proposal ? "Problem: " : ""}</span>${esc(p.body || "")}</p><p class="muted" style="margin:4px 0 0;font-size:12px">${(p.sessions || []).map(t => `<a href="#/session/${t.session}">#${t.session}</a>${t.runs && t.runs.length ? " runs " + t.runs.join(", ") : ""}`).join("; ")}</p></details>`).join("") || '<p class="muted" style="margin:0">none yet</p>'}${more("proposals", Math.min(lim.proposals, props.length), props.length)}</div>
  <h3>Snapshots${info("A frozen copy of this page's data, kept in the database and downloadable as JSON - for example before the database is reset.")}</h3>
  <div class="card"><div class="row"><input id="snapLabel" placeholder="label (e.g. before reset 2026-10-20)" style="flex:1"><button class="btn" id="snapNow">Save snapshot</button></div>
  ${d.snapshots.length ? `<div class="tablewrap"><table style="margin-top:8px"><tbody>${d.snapshots.map(x => `<tr><td>#${x.id}</td><td>${esc(x.label || "")}</td><td class="muted">${new Date(x.created * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC</td><td class="num">${x.runs} runs</td><td><a href="/api/admin/learning/snapshots/${x.id}">download</a></td></tr>`).join("")}</tbody></table></div>` : ""}</div>
  <h3>Experience by idea and conditions${info("Each experiment is counted for the rule (idea) that proposed it, in the conditions of its run. wins = improved or efficiency gain; losses = regressed or efficiency loss; mean delta = average goal change against the parent configuration.")}</h3>
  <div class="row" style="margin-bottom:6px"><input id="learnFilter" placeholder="filter by idea or workload" value="${esc(filt)}" style="max-width:320px" aria-label="Filter by idea or workload"></div>
  <div class="card tablewrap"><table><thead><tr><th>Idea</th><th>Workload</th><th>CPUs</th><th>Connections</th><th class="num">n</th><th class="num">wins</th><th class="num">losses</th><th class="num">neutral</th><th class="num">mean Δ</th><th>Sessions</th></tr></thead><tbody>
  ${ex.slice(0, lim.experience).map(e => `<tr><td class="mono">${esc(e.idea)}</td><td>${esc(e.workload)}</td><td>${esc(e.cpu_class)}</td><td>${esc(e.concurrency_class)}</td><td class="num">${e.n}</td><td class="num">${e.wins}</td><td class="num">${e.losses}</td><td class="num">${e.neutral}</td><td class="num">${pct(e.mean_delta_pct)}</td><td style="font-size:12px">${st(e.sessions.slice(-8))}${e.sessions.length > 8 ? " …" : ""}</td></tr>`).join("")}</tbody></table></div>${more("experience", Math.min(lim.experience, ex.length), ex.length)}
  <h3>Hypothesis track record${info("For each idea and the component of database time it targeted: supported = goal improved and the targeted time shrank; moved = the targeted time shrank but the goal did not move (that component was not the limit); refuted = neither; improved by another mechanism = goal improved but not through the targeted time.")}</h3>
  <div class="card tablewrap"><table><thead><tr><th>Idea</th><th>Targeted component</th><th class="num">n</th><th class="num">supported</th><th class="num">moved</th><th class="num">refuted</th><th class="num">other mechanism</th></tr></thead><tbody>
  ${d.hypotheses.map(x => `<tr><td class="mono">${esc(x.id)}</td><td>${esc(x.target)}</td><td class="num">${x.n}</td><td class="num">${x.supported}</td><td class="num">${x.moved}</td><td class="num">${x.refuted}</td><td class="num">${x["improved-other-mechanism"] || 0}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">none yet</td></tr>'}</tbody></table></div>
  <h3>Bottleneck analyses (LLM)${info("Analyses of measured components no rule acted on, cached by the conditions they were made in (server version, component, top stack frames, workload, goal, hardware class, commit / flush settings) and reused for similar conditions.")}</h3>
  ${d.llm_analyses.slice(0, lim.analyses).map(a => `<div class="card" style="margin-bottom:8px"><b>${esc(a.component || "?")}</b> <span class="muted">first in <a href="#/session/${a.first_session}">#${a.first_session}</a>, used ${a.uses}x (${a.reuses} reused), ${esc(a.model || "")}</span>${info("Conditions: " + JSON.stringify(a.conditions) + (a.frames && a.frames.length ? ". Frames: " + a.frames.join(" < ") : ""))}
    <p style="margin:6px 0">${esc(a.hypothesis || "")}</p>${a.changes.length ? `<ul style="margin:4px 0 0 18px">${a.changes.map(c => `<li>${esc(c.title || "")}: <span class="mono">${esc((c.changes || []).map(x => x.variable + "=" + x.value).join(", "))}</span> - ${esc(c.mechanism || "")}</li>`).join("")}</ul>` : '<p class="muted" style="margin:0">no configuration change proposed</p>'}</div>`).join("") || '<p class="muted">none yet</p>'}${more("analyses", Math.min(lim.analyses, d.llm_analyses.length), d.llm_analyses.length)}`;
    wire();
  };
  const saved = el => { const m = el.parentElement.querySelector("[data-saved]") || el.closest("details, td").querySelector("[data-saved]"); if (m) { m.textContent = "saved"; setTimeout(() => { m.textContent = ""; }, 2500); } };
  const wire = () => {
    $view.querySelectorAll("[data-more]").forEach(b => b.onclick = () => { lim[b.dataset.more] *= 2; draw(); });
    document.getElementById("learnFilter").onchange = e => { filt = e.target.value; try { sessionStorage.setItem("learnFilter", filt); } catch (x) { } lim.experience = 50; draw(); };
    document.getElementById("insStatus").onchange = e => { insStatus = e.target.value; lim.insights = 50; draw(); };
    document.getElementById("pruneNow").onclick = async () => { const r = await api("/api/admin/learner/prune", { method: "POST", body: {} }); alert(`${r.archived} insight(s) archived.`); learningPage(); };
    document.getElementById("learnNow").onclick = async () => { await api("/api/admin/learner/run", { method: "POST", body: {} }); setTimeout(learningPage, 1500); };
    $view.querySelectorAll("select[data-ins]").forEach(el => el.onchange = async () => {
      if (["retired-by-admin", "archived"].includes(el.value) && !confirm(`Set this insight to "${el.value}"? Agents stop receiving it.`)) { el.value = el.dataset.was; return; }
      await api(`/api/admin/insights/${el.dataset.ins}`, { method: "PATCH", body: { status: el.value } });
      const x = L.insights.find(i => String(i.id) === el.dataset.ins); if (x) x.status = el.value; el.dataset.was = el.value; saved(el);
    });
    $view.querySelectorAll("select[data-prop]").forEach(el => el.onchange = async () => {
      await api(`/api/admin/proposals/${el.dataset.prop}`, { method: "PATCH", body: { status: el.value } });
      const x = L.proposals.find(i => String(i.id) === el.dataset.prop); if (x) x.status = el.value; saved(el);
    });
    document.getElementById("snapNow").onclick = async () => { await api("/api/admin/learning/snapshots", { method: "POST", body: { label: document.getElementById("snapLabel").value } }); learningPage(); };
  };
  draw();
}

async function policiesPage() {
  setNav("policies");
  const p = await api("/api/policies");
  const vars = Object.entries(p.variables).sort();
  setTitle("Policies");
  $view.innerHTML = `<h1>Tuning policies</h1><p class="sub">How the agent decides what it may change. Generated from the agent's own rule table.</p>
  <div class="grid g2">${Object.entries(p.policies).map(([k, v]) => `<div class="card"><h3 style="margin-top:0">${esc(k)}</h3><p style="margin:0">${esc(v)}</p></div>`).join("")}</div>
  <h2>What “production-safe” means</h2><p>${esc(p.safe_definition)}</p>
  <h2>Trade-off settings${info("A change is a trade-off when the variable gets one of these values. Under safe they are advice only; under benchmark they are tried after the safe experiments, and runs that use them are flagged and reported separately.")}</h2>
  <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Trade-off when</th><th>Category</th><th>What is given up</th></tr></thead><tbody>
  ${p.tradeoffs.map(t => `<tr><td class="mono">${esc(t.variable)}</td><td>${esc(t.when)}</td><td><span class="badge bad">${esc(t.category)}</span></td><td>${esc(t.lost)}</td></tr>`).join("")}</tbody></table></div>
  <h2>Guards that apply under every policy</h2><ul>${p.guards.map(g => `<li>${esc(g)}</li>`).join("")}</ul>
  <h2>Variables the agent knows</h2><p class="muted" style="margin:-4px 0 8px"><a href="#/parameters">Parameters</a> lists them with impact, safety and your policy.</p>
  <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Area</th><th>Restart</th><th>Description</th></tr></thead><tbody>
  ${vars.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td>${esc(v.category)}</td><td>${v.dynamic ? "no" : "yes"}</td><td>${esc(v.desc)}</td></tr>`).join("")}</tbody></table></div>`;
}

async function accountPage() {
  setNav("account");
  const L = await api("/api/settings/llm");
  setTitle("Account");
  $view.innerHTML = `<h1>Account</h1><p class="sub">${esc(ME.email)}</p>
  ${L.admin ? `<div class="card" style="max-width:640px;margin-bottom:14px"><h3 style="margin-top:0">Service AI (OpenRouter)${info("The service's own key: every user's AI assistance (suggestions, bottleneck analyses) and the background learner use it. Users need no key. The tuner asks the model only where it adds value; suggestions are validated and measured like any experiment. The key is stored encrypted on the server and never sent to agents.")}</h3>
   <form id="lf"><label>OpenRouter API key ${L.configured ? '<span class="badge good">configured</span>' : '<span class="badge plain">not set</span>'}</label>
   <input name="key" type="password" placeholder="${L.configured ? "•••••••• (leave empty to keep)" : "sk-or-…"}" autocomplete="off">
   <div class="grid g2"><div style="grid-column:1 / -1"><label>Model</label><select name="model" id="lmodel"><option>${esc(L.model)}</option></select></div>
   <div><label>Use the model</label><select name="mode">${L.modes.map(m => `<option value="${m}" ${m === L.mode ? "selected" : ""}>${{ off: "never (built-in rules + learnings)", "when-needed": "for new ideas when the rules run out", always: "for new ideas at every step", planner: "to plan every step (rules + learnings + AI) - default" }[m] || m}</option>`).join("")}</select></div></div>
   <div class="err" id="lerr"></div><div class="row"><button class="primary">Save</button>${L.configured ? '<button type="button" class="ghost danger" id="lrm">Remove key</button>' : ""}
   <span class="spacer"></span><span class="muted" style="font-size:12.5px">your sessions: ${L.usage.calls} calls (${L.usage.cached} cached) · $${(+L.usage.cost_usd || 0).toFixed(2)}</span></div></form></div>`
  : `<div class="card" style="max-width:640px;margin-bottom:14px"><h3 style="margin-top:0">AI assistance</h3><p style="margin:0">${L.configured && L.mode !== "off" ? 'Provided by the service <span class="badge good">on</span>' : 'Not enabled on this service <span class="badge plain">off</span>'}${info("The service supplies the AI model; you do not need a key. It is used only where it adds value, and every suggestion is validated and measured like any experiment.")}</p>
   <p class="muted" style="margin:6px 0 0;font-size:12.5px">your sessions: ${L.usage.calls} calls (${L.usage.cached} cached) · LLM experiments: ${L.usage.improvements} of ${L.usage.experiments} improved</p></div>`}<div class="card form" style="margin:0;max-width:420px"><h3>Change password</h3>
  <form id="f"><label>Current password</label><input type="password" name="old" required><label>New password</label><input type="password" name="new" required minlength="8">
  <div class="err" id="err"></div><button class="primary">Change password</button></form></div>`;
  if (L.admin) api("/api/llm/models").then(d => { const sel = document.getElementById("lmodel"); if (!sel) return;
    sel.innerHTML = d.models.map(m => `<option value="${esc(m.id)}" title="${esc(`${m.id}: $${m.in_per_mtok} in / $${m.out_per_mtok} out per million tokens`)}" ${m.id === L.model ? "selected" : ""}>${esc(m.id)} · $${m.in_per_mtok}/$${m.out_per_mtok} per Mtok</option>`).join(""); }).catch(() => {});
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
  // series: [group, key, name, unit] or {name, fn: m => values, unit} for values derived from sampled series (unit: when it is
  // not the chart's, shown in the tooltip)
  container.innerHTML = "";
  defs.forEach(def => {
    let series = def.series.map(x => Array.isArray(x) ? { name: x[2], values: (m[x[0]] || {})[x[1]], unit: x[3] } : { name: x.name, values: x.fn(m), unit: x.unit });
    if (opts && opts.skipEmpty && !series.some(s => s.values && s.values.some(v => v != null))) return;
    // large values in MiB read badly ("30.0k MiB"): switch the whole chart to GiB
    let unit = def.unit;
    const peak = Math.max(0, ...series.flatMap(s => (s.values || []).filter(v => v != null)));
    if ((unit === "MiB" && peak > 4096) || (unit === "MiB/s" && peak > 2048)) {
      unit = unit === "MiB" ? "GiB" : "GiB/s";
      series = series.map(s => ({ name: s.name, unit: s.unit, values: (s.values || []).map(v => v == null ? v : v / 1024) }));
    }
    const cell = document.createElement("div"); cell.className = "card";
    container.appendChild(cell);
    C.lineChart(cell, { title: def.title, unit, ts: m.ts, window: window_, warmup: m.warmup, startup: m.startup, max100: def.max100, min0: def.min0, markers, series });
    const t = cell.querySelector(".title");
    if (def.tip && t) t.insertAdjacentHTML("beforeend", info(def.tip));
  });
}
// derived series: combine sampled series point by point (no extra sampling)
const ser = (m, g, k) => (m[g] || {})[k] || [];
const combine = (m, f, ...refs) => (m.ts || []).map((_, i) => { const v = refs.map(([g, k]) => ser(m, g, k)[i]); return v.some(x => x == null) ? null : f(...v); });
const perTrx = (m, f, ...refs) => combine(m, (tps, ...v) => tps >= 5 ? f(...v) / tps : null, ["db", "tps"], ...refs);

async function envPage(id) {
  setNav("envs");
  const d = await api("/api/environments/" + id);
  const e = d.environment, f = d.facts || {}, hw = f.hardware || {}, db = f.db || {};
  const kv = db.key_variables || {};
  setTitle(e.name);
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> /</div>
  <div class="row"><h1>${esc(e.name)}</h1>${envStatus(e)}<span class="spacer"></span>
  <button class="ghost owner" id="share">Share</button><button class="ghost" id="rename">Rename</button><button class="ghost mut" id="archive" data-tip="${e.archived ? "Back to the active list." : "Hide from the active list (for example when the machine is gone); sessions, runs and metrics are kept. Environments without contact for 3 days and no active session are archived automatically."}">${e.archived ? "Unarchive" : "Archive"}</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">${esc(e.db_short || e.db)} · ${esc(hw.hostname || "")} · agent ${esc(e.agent_version || "")}</p>
  ${d.activity ? `<div class="card" style="margin-bottom:10px;padding:10px 14px"><span class="muted" style="font-size:12px">Now${d.activity.session_id ? `: session <a href="#/session/${d.activity.session_id}">#${d.activity.session_id} ${esc(d.activity.session_name || "")}</a>` : ""}</span>${activityLine(d.activity)}</div>` : ""}
  <div class="card"><div class="row" style="gap:6px 18px">
    <span><b>${hw.cpus} vCPU</b> <span class="muted">${esc((hw.cpu_model || "").replace(/\s+\d+-Core Processor/, "").replace(/\(R\)|\(TM\)/g, ""))}</span></span>
    <span><b>${bytes(hw.ram_bytes)}</b> RAM${hw.swap_bytes === 0 ? ` <span class="badge warn" data-tip="No swap configured. Memory beyond RAM is not slowed down by swapping but ends in the OOM killer, so the tuner keeps every configuration inside the memory budget and cannot look for the practical buffer pool size where active swapping starts. Add swap to explore that range." tabindex="0">no swap</span>` : ""}</span>
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
     return `<tr><td><b>${esc(h.name)}</b></td><td>${h.destroyed_at ? `<span class="badge plain" data-tip="Machine deleted ${ago(h.destroyed_at)}; its metrics stay with the runs." tabindex="0">decommissioned</span>` : h.online ? '<span class="badge good">online</span>' : `<span class="badge warn">offline · ${ago(h.last_seen)}</span>`}</td>
       <td>${hw.cpus || "?"} CPU · ${bytes(hw.ram_bytes)}</td><td class="num">${o.rtt_avg_ms != null ? num(o.rtt_avg_ms) + " ms" : "–"}</td>
       <td class="num">${num(o.net_conn_per_s)}</td><td class="num">${num(o.net_time_wait)}</td><td class="num">${o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–"}</td></tr>`; }).join("")}</tbody></table></div>` : ""}
  <div class="card" style="margin-top:10px"><details><summary style="margin:0">Current agent-applied configuration (${Object.keys((f.agent || {}).managed || {}).length - ((f.agent || {}).at_default || []).length} changed from compiled defaults)${info("The settings the tuning-agent currently enforces on this server: MySQL variables written to its own option file (shown below) and operating-system settings. Everything not listed is the server default or comes from your own option files. As of the agent's last report.")}</summary>${Object.keys((f.agent || {}).managed || {}).length ? `<p class="muted" style="margin:0 0 6px">${esc((f.agent || {}).cnf_path || "")} · restarts ${(f.agent || {}).allow_restart === false ? "disabled" : "allowed"}</p>${configBlocks(Object.fromEntries(Object.entries(f.agent.managed).filter(([k]) => !(f.agent.at_default || []).includes(k))), null, unsafeMap(f.agent.tradeoffs))}${(f.agent.at_default || []).length ? `<p class="muted" style="font-size:12.5px;margin:6px 0 0">Also written at their compiled default (no change on this server): ${f.agent.at_default.map(k => `<code>${esc(k)}</code>`).join(" ")}</p>` : ""}` : '<span class="muted">None yet: <code>tuning-agent baseline --apply</code> writes the first set.</span>'}</details></div>
  <div class="row"><h2>Live metrics</h2><span class="spacer"></span><select id="range" style="width:auto"><option value="900">15 min</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">24 hours</option></select></div>
  <div id="live"><p class="muted">Loading metrics…</p></div>
  <h2>Tuning sessions</h2><div class="card tablewrap"><table data-opt="Goal|Policy|Runs|Start|Best safe|Started"><thead><tr><th>#</th><th>Session</th><th>Goal</th><th>Policy</th><th>Status</th><th class="num">Runs</th><th class="num">Start${info("The calibrated starting configuration of each session (mean of its calibration runs; else its baseline run). Server defaults (run #1) in the tooltip.")}</th><th class="num">Best safe${info("Best production-safe configuration: mean of the runs measured with it. The best configuration with trade-offs is in the tooltip.")}</th><th class="num">Best gain${info("Best production-safe configuration vs the calibrated start (faster, for response-time goals).")}</th><th>Started</th></tr></thead><tbody id="sess"></tbody></table></div>
  <div id="sessMore"></div>
  <h2>Recent activity</h2><div class="card" id="events"></div>`;
  const tb = document.getElementById("sess");
  if (!d.sessions.length) tb.innerHTML = `<tr><td colspan="10" class="muted">No sessions yet. Start one with <code>tuning-agent session start --goal tps</code>.</td></tr>`;
  const sessRow = s => {
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = ev => { if (!ev.target.closest("a, [data-tip]")) location.hash = "#/session/" + s.id; };
    const lower = s.goal === "latency";
    const startTip = s.first_value != null && s.start_value != null && s.first_value !== s.start_value ? ` data-tip="${esc(`Server defaults (run #1): ${num(s.first_value)}`)}" tabindex="0"` : "";
    const bestTip = s.best_tradeoff_value != null ? ` data-tip="${esc(`With trade-offs: ${num(s.best_tradeoff_value)} (${gainTxt(s.best_tradeoff_gain_pct, lower)})`)}" tabindex="0"` : "";
    const gainTip = s.best_gain_vs_first_pct != null ? ` data-tip="${esc(`vs server defaults (run #1): ${gainTxt(s.best_gain_vs_first_pct, lower)}`)}" tabindex="0"` : "";
    tr.innerHTML = `<td class="muted">${s.id}</td><td><a href="#/session/${s.id}" class="sname" data-tip="${esc(s.name)}">${esc(s.name)}</a></td><td>${goalName(s.goal)}</td><td>${esc(s.policy)}</td><td>${statusBadge(s.status)}</td><td class="num">${s.runs}</td>
     <td class="num"${startTip}>${num(s.start_value)}</td><td class="num"${bestTip}>${num(s.best_value)}</td><td class="num ${deltaCls(s.best_gain_pct)}"${gainTip}>${gainTxt(s.best_gain_pct, lower)}</td><td>${fmtTime(s.created)}</td>`;
    tb.appendChild(tr);
  };
  // the latest 10; older sessions on request
  d.sessions.slice(0, 10).forEach(sessRow);
  if (d.sessions.length > 10) {
    document.getElementById("sessMore").innerHTML = `<div style="margin-top:8px"><button id="sessAll">Show all ${d.sessions.length} sessions</button></div>`;
    document.getElementById("sessAll").onclick = ev => { d.sessions.slice(10).forEach(sessRow); ev.target.remove(); };
  }
  renderEvents(document.getElementById("events"), null, false, { env: id });
  document.getElementById("share").onclick = ev => sharePanel("environment", id, ev.target);
  document.getElementById("rename").onclick = async () => { const n = prompt("Environment name", e.name); if (n) { await api("/api/environments/" + id, { method: "PATCH", body: { name: n } }); envPage(id); } };
  const cj = document.getElementById("clientsJump"); if (cj) cj.onclick = ev => { ev.preventDefault(); document.getElementById("clientHosts").scrollIntoView({ behavior: "smooth" }); };
  document.getElementById("detailsToggle").onclick = ev => { ev.preventDefault(); const d = document.getElementById("envDetails"); d.hidden = !d.hidden; ev.target.textContent = d.hidden ? "Details ▸" : "Details ▾"; };
  document.getElementById("archive").onclick = async () => { await api("/api/environments/" + id, { method: "PATCH", body: { archived: !e.archived } }); envPage(id); };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this environment with all sessions, runs and metrics? The agent will re-register it on its next report.")) { await api("/api/environments/" + id, { method: "DELETE" }); location.hash = "#/"; } };
  const sessionMarkers = [];
  d.sessions.forEach(x => { sessionMarkers.push({ t: x.created, label: `#${x.id} start` }); if (x.ended) sessionMarkers.push({ t: x.ended, label: `#${x.id} end`, end: true }); });
  const live = document.getElementById("live"), range = document.getElementById("range");
  const cpus = hw.cpus || 1;
  let last = null;
  const load = async (force) => {
    const span = +range.value; const now = Date.now() / 1000;
    const [m, w] = await Promise.all([api(`/api/environments/${id}/metrics?start=${now - span}&end=${now}&points=240`), api(`/api/environments/${id}/waits?start=${now - span}&end=${now}`)]);
    m._waits = w; last = m;
    // the 15 s refresh waits while the pointer is on a chart (the cross-hair and tooltip would vanish under it)
    if (force !== true && live.querySelector(".chart:hover")) return;
    renderLive(live, m, cpus, e.name, sessionMarkers);
  };
  range.onchange = () => load(true);
  widthFns.push(() => { if (last) renderLive(live, last, cpus, e.name, sessionMarkers); });
  await load(true);
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
  { title: "Disk I/O per transaction", unit: "IOs", series: [{ name: "disk IOs", fn: m => perTrx(m, (r, w) => r + w, ["os", "disk_r_iops"], ["os", "disk_w_iops"]) }],
    tip: "Disk I/O requests per transaction (periods under 5 trx/s left out)." },
  { title: "Redo per transaction", unit: "KiB", series: [{ name: "redo written", fn: m => perTrx(m, r => r * 1024, ["db", "redo_mbs"]) }],
    tip: "Redo log written per transaction (periods under 5 trx/s left out)." },
  { title: "Buffer pool hit ratio", unit: "%", min0: false, series: [["db", "bp_hit_pct", "hit ratio"]] },
  { title: "Buffer pool I/O", unit: "pages/s", series: [["db", "bp_disk_reads", "read from disk"], ["db", "bp_pages_flushed", "flushed"], ["db", "bp_wait_free", "waits for a free page"]],
    tip: "Waits for a free page mean user threads had to flush pages themselves: page cleaners cannot keep up." },
  { title: "Buffer pool content", unit: "% of pages", max100: true, series: [["db", "bp_dirty_pct", "dirty"], ["db", "bp_free_pct", "free"]] },
  { title: "Redo written", unit: "MiB/s", series: [["db", "redo_mbs", "written"]] },
  { title: "Checkpoint age", unit: "% of redo capacity", max100: true, series: [["db", "checkpoint_age_pct", "checkpoint age"]],
    tip: "Checkpoint age near 75-80% of capacity forces synchronous flushing (stalls)." },
  { title: "InnoDB file I/O", unit: "per s", series: [["db", "data_reads", "reads"], ["db", "data_writes", "writes (all)"], ["db", "log_writes", "redo log writes"], ["db", "bp_pages_flushed", "data page writes"], ["db", "dblwr_writes", "doublewrite writes"], ["db", "log_fsyncs", "redo log fsyncs"], ["db", "datafile_fsyncs", "data-file fsyncs"], ["db", "data_fsyncs", "fsyncs (all, incl. redo)"]],
    tip: "Write operations through InnoDB's file layer. 'writes (all)' (Innodb_data_writes) is the sum of redo log writes, data pages flushed from the buffer pool and doublewrite batches - often mostly small redo writes. Redo log fsyncs (Innodb_os_log_fsyncs) follow commits with innodb_flush_log_at_trx_commit=1 (fewer with group commit). Innodb_data_fsyncs counts all InnoDB fsyncs including the redo log's; data-file fsyncs is the difference." },
  { title: "InnoDB bytes written", unit: "MiB/s", series: [["db", "data_written_mbs", "all"], ["db", "log_written_mbs", "redo log"]] },
  { title: "Row locks", unit: "", series: [["db", "row_lock_waits", "lock waits", "per s"], ["db", "row_lock_current_waits", "waiting now", "threads"]],
    tip: "Row lock waits are a workload property (hot rows) more than a configuration one." },
  { title: "Purge lag: history list length", unit: "undo records", series: [["db", "history_list_length", "history list"]],
    tip: "Undo records not yet purged. Growth means long transactions or purge falling behind: reads get slower as versions pile up." },
  { title: "Query plan warnings", unit: "per s", series: [["db", "select_scan", "full table scans"], ["db", "select_full_join", "joins without index"], ["db", "sort_merge_passes", "sort merge passes"]],
    tip: "Full scans and joins without an index are query/schema issues; sort merge passes mean sorts spill to disk (sort_buffer_size)." },
  { title: "Rows read sequentially", unit: "rows/s", series: [["db", "handler_read_rnd_next", "rows scanned"]] },
  { title: "Temporary tables", unit: "per s", series: [["db", "tmp_tables", "created"], ["db", "tmp_disk_tables", "on disk"]],
    tip: "On-disk temporary tables are slow: raise tmp_table_size / temptable_max_ram when this is a large share of all temporary tables." },
  { title: "Cache misses", unit: "per s", series: [["db", "opened_tables", "tables opened"], ["db", "opened_table_defs", "table definitions opened"], ["db", "threads_created", "threads created"]],
    tip: "Should be near zero in steady state; otherwise table_open_cache, table_definition_cache or thread_cache_size are too small." },
  { title: "Client traffic (MySQL protocol)", unit: "MiB/s", series: [["db", "net_received_mbs", "received"], ["db", "net_sent_mbs", "sent"]] },
  { title: "Errors and spills", unit: "per s", series: [["db", "aborted_connects", "aborted connects"], ["db", "slow_queries", "slow queries"], ["db", "binlog_cache_disk_use", "binlog cache spills"], ["db", "log_waits", "log buffer waits"]],
    tip: "Log buffer waits mean innodb_log_buffer_size is too small." },
];
const LIVE_DBPROC = [
  { title: "mysqld CPU", unit: "% of host", series: [["os", "mysqld_cpu_pct", "mysqld"]] },
  { title: "Database process memory", unit: "MiB", series: [["os", "dbproc_rss_mb", "RSS"], ["os", "dbproc_pss_mb", "PSS"], ["os", "dbproc_pss_shmem_mb", "PSS shared memory"], ["os", "dbproc_swap_pss_mb", "swap PSS"], ["os", "dbproc_swap_mb", "swap"], ["os", "dbproc_cg_mem_mb", "cgroup charge"]],
    tip: "PSS splits shared pages between the processes that map them: the real share of RAM. The cgroup charge also includes page cache charged to the service." },
  { title: "Scheduling delay", unit: "µs per CPU slice", series: [["os", "dbproc_sched_delay_us", "database threads"], ["os", "host_sched_delay_us", "all tasks"]],
    tip: "Time a runnable database thread waited for a CPU each time it was scheduled; adds directly to query latency." },
  { title: "Run-queue wait", unit: "% of runnable time", series: [["os", "dbproc_runq_wait_pct", "database threads"], ["os", "host_runq_wait_pct", "all tasks"]] },
  { title: "Pressure stall (mysqld cgroup)", unit: "% time", max100: true, series: [["os", "mysqld_psi_cpu_some", "CPU"], ["os", "mysqld_psi_io_some", "I/O"], ["os", "mysqld_psi_memory_some", "memory"], ["os", "mysqld_psi_io_full", "I/O (all stalled)"], ["os", "mysqld_psi_memory_full", "memory (all stalled)"]] },
  { title: "Preemptions and faults", unit: "per s", series: [["os", "dbproc_nvcsw_per_s", "involuntary switches"], ["os", "dbproc_pgmajfault_per_s", "major faults"]],
    tip: "Involuntary switches: threads preempted while running. Major faults on a database with O_DIRECT data files are mostly swap-ins." },
];
const LIVE_OS = [
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_iowait", "iowait"], ["os", "cpu_steal", "steal"]] },
  { title: "Load", unit: "threads", series: [["os", "load1", "load 1m"], ["os", "procs_running", "running"], ["os", "procs_blocked", "blocked on I/O"]] },
  { title: "Pressure stall (host)", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "CPU"], ["os", "psi_io_some", "I/O"], ["os", "psi_memory_some", "memory"], ["os", "psi_io_full", "I/O (all stalled)"], ["os", "psi_memory_full", "memory (all stalled)"]] },
  { title: "Disk IOPS", unit: "per s", series: [["os", "disk_r_iops", "reads"], ["os", "disk_w_iops", "writes"]] },
  { title: "Disk throughput", unit: "MiB/s", series: [["os", "disk_r_mbs", "read"], ["os", "disk_w_mbs", "write"]] },
  { title: "Disk latency", unit: "ms", series: [["os", "disk_r_await_ms", "read"], ["os", "disk_w_await_ms", "write"], ["os", "disk_flush_await_ms", "flush"]] },
  { title: "Disk queue depth", unit: "requests", series: [["os", "disk_queue", "queue depth"]] },
  { title: "Disk utilisation", unit: "% busy", max100: true, series: [["os", "disk_util", "util"]] },
  { title: "Memory", unit: "MiB", series: [["os", "mem_available_mb", "available"], ["os", "page_cache_mb", "page cache"], ["os", "inactive_anon_mb", "inactive anon"]] },
  { title: "Memory used", unit: "%", max100: true, series: [["os", "mem_used_pct", "used"]] },
  { title: "Swap used", unit: "MiB", series: [["os", "swap_used_mb", "used"]] },
  { title: "Swap traffic", unit: "MiB/s", series: [["os", "swap_in_mbs", "in"], ["os", "swap_out_mbs", "out"]] },
  { title: "Swap pages", unit: "pages/s", series: [["os", "swap_in", "in"], ["os", "swap_out", "out"]] },
  { title: "Network", unit: "MiB/s", series: [["os", "net_rx_mbs", "received"], ["os", "net_tx_mbs", "sent"]] },
  { title: "TCP connections", unit: "per s", series: [["os", "net_conn_per_s", "new connections"], ["os", "net_resets_per_s", "resets"], ["os", "net_listen_overflows", "listen overflows"]] },
  { title: "TCP sockets and retransmits", unit: "", series: [["os", "net_time_wait", "TIME_WAIT", "sockets"], ["os", "net_retrans_pct", "retransmits", "%"]],
    tip: "TIME_WAIT sockets (count) and retransmitted segments (% of sent): two scales on one axis, values in the tooltip." },
  { title: "Softirq CPU", unit: "%", series: [["os", "softirq_max_cpu_pct", "busiest CPU"], ["os", "softirq_avg_cpu_pct", "average"]] },
  { title: "Packet drops", unit: "per s", series: [["os", "net_softnet_drops", "softnet drops"]] },
  { title: "Context switches", unit: "per s", series: [["os", "ctx_switches", "switches"]] },
];
const LIVE_CLIENT = [
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_steal", "steal"], ["os", "softirq_max_cpu_pct", "busiest CPU softirq"]] },
  { title: "Round trip to the database", unit: "ms", series: [["os", "rtt_avg_ms", "avg"], ["os", "rtt_p95_ms", "p95"]] },
  { title: "Network", unit: "MiB/s", series: [["os", "net_rx_mbs", "received"], ["os", "net_tx_mbs", "sent"]] },
  { title: "TCP connections", unit: "per s", series: [["os", "net_conn_per_s", "new connections"]] },
  { title: "TCP sockets and retransmits", unit: "", series: [["os", "net_time_wait", "TIME_WAIT", "sockets"], ["os", "net_retrans_pct", "retransmits", "%"]] },
  { title: "Pressure stall", unit: "% time", max100: true, series: [["os", "psi_cpu_some", "CPU"], ["os", "psi_memory_some", "memory"]] },
  { title: "Scheduling delay (all tasks)", unit: "µs per CPU slice", series: [["os", "host_sched_delay_us", "all tasks"]] },
];
function renderLive(el, m, cpus, hostName, markers, window_) {
  // one chart set for the environment (last X) and a run (warm-up and measured window shaded)
  m._cpus = cpus;
  let open = {};
  try { open = JSON.parse(localStorage.getItem("liveOpen") || "{}"); } catch (e) { }
  const sections = [["db", `Database: ${hostName}`, LIVE_DB, m], ["proc", `Database process: ${hostName}`, LIVE_DBPROC, m], ["os", `Operating system: ${hostName}`, LIVE_OS, m]];
  (m.clients || []).forEach((c, i) => sections.push(["cli" + (c.id != null ? c.id : i), `Client host: ${c.name}`, LIVE_CLIENT, { ts: c.ts, os: c.os, db: {}, warmup: m.warmup }]));
  // keep open/closed state across refreshes
  const prev = {}; el.querySelectorAll("details[data-k]").forEach(d => { prev[d.dataset.k] = d.open; });
  el.innerHTML = sections.map(([k, title]) => `<details class="livesec" data-k="${k}" ${(k in prev ? prev[k] : open[k] !== false) ? "open" : ""}><summary><h3 style="display:inline">${esc(title)}</h3></summary><div class="grid g3" data-grid="${k}"></div></details>`).join("");
  // a closed section is drawn when it is first opened (charts need their width)
  const fill = ([k, , defs, mm]) => { const g = el.querySelector(`[data-grid="${k}"]`); if (g.dataset.drawn) return; g.dataset.drawn = 1; chartsGrid(g, mm, defs, window_ || null, markers, { skipEmpty: true }); if (k === "db") waitsCard(); };
  const waitsCard = () => {
  const dbGrid = el.querySelector('[data-grid="db"]');
  if (dbGrid && m._waits && m._waits.events && m._waits.events.length) {
    const card = document.createElement("div"); card.className = "card";
    card.innerHTML = `<div class="chart"><div class="title">Top wait events (selected range)${info("Average number of threads waiting on each performance_schema wait event over the selected range, and its share of all wait time.")}</div>
      <table class="kvt"><tbody>${m._waits.events.slice(0, 10).map(w => `<tr><td class="mono" style="word-break:break-all">${esc(w.event.replace(/^wait\//, ""))}</td><td class="num">${num(w.avg_threads)}</td><td class="num muted">${w.pct}%</td></tr>`).join("")}</tbody></table></div>`;
    dbGrid.insertBefore(card, dbGrid.children[3] || null);
  }
  };
  sections.forEach(sec => { if (el.querySelector(`details[data-k="${sec[0]}"]`).open) fill(sec); });
  el.querySelectorAll("details[data-k]").forEach(d => d.addEventListener("toggle", () => {
    open[d.dataset.k] = d.open; try { localStorage.setItem("liveOpen", JSON.stringify(open)); } catch (e) { }
    if (d.open) fill(sections.find(x => x[0] === d.dataset.k));
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
function runRanges(seqs) {
  const out = []; let a = null, p = null;
  seqs.forEach(x => { if (a === null) { a = p = x; } else if (x === p + 1) { p = x; } else { out.push(a === p ? `#${a}` : `#${a}–#${p}`); a = p = x; } });
  if (a !== null) out.push(a === p ? `#${a}` : `#${a}–#${p}`);
  return out.join(", ");
}
// passwords out of benchmark commands (the server masks them too; kept here as a backstop for older records)
function maskPw(t) {
  if (t == null) return t;
  t = String(t).replace(/(--?[\w-]*pass(?:word)?[= ])\S+/gi, "$1***").replace(/(\b\w*(?:PASSWORD|PASSWD|PWD)\w*=)\S+/g, "$1***");
  return /mysql/.test(t) ? t.replace(/(\s-p)(?=[^\s-])[^\s'"]+/g, "$1***") : t;
}
function shq(a) { return /^[\w@%+=:,./-]+$/.test(a) ? a : "'" + a.replace(/'/g, "'\\''") + "'"; }
function benchCard(list) {
  if (!list.length) return "";
  const tool = o => `<div style="margin:6px 0 10px">${o.version ? `<div class="muted" style="font-size:12.5px">${esc(o.version)}</div>` : ""}
      <pre style="margin:2px 0">${esc(o.argv.map(shq).join(" "))}</pre>
      ${Object.keys(o.env || {}).length ? `<div class="kv" style="font-size:13px">${Object.entries(o.env).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(v)}</div>`).join("")}</div>` : ""}
      ${Object.entries(o.scripts || {}).map(([p, t]) => `<details><summary>${esc(p)}</summary><pre>${esc(t)}</pre></details>`).join("")}</div>`;
  const one = b => {
    const ph = b.phases || {}, wu = ph.warmup_s ?? (/^\d+$/.test(String(b.warmup)) ? +b.warmup : null), ms = ph.measured_s ?? b.duration;
    const phases = wu != null ? `warm-up ${wu} s, then measured ${ms ? ms + " s" : "until the benchmark finishes"}` : `measured ${ms ? ms + " s" : "until the benchmark finishes"} (no fixed warm-up)`;
    const obs = b.observed || [];
    const harness = `<div class="kv"><div>Command</div><div><pre style="margin:0">${esc(maskPw(b.command || b.run || ""))}</pre></div>
      ${b.reset ? `<div>Reset before each run</div><div class="mono">${esc(maskPw(b.reset))}</div>` : ""}
      ${b.profile ? `<div>Profile</div><div class="mono">${esc(b.profile)}</div>` : ""}</div>
      ${b.profile_text ? `<details><summary>Profile file</summary><pre>${esc(b.profile_text)}</pre></details>` : ""}
      ${Object.entries(b.scripts || {}).map(([p, t]) => `<details><summary>${esc(p)}</summary><pre>${esc(t)}</pre></details>`).join("")}`;
    return `<div style="margin-top:8px"><b>${esc(b.name || "")}</b>${list.length > 1 ? ` <span class="muted">runs ${runRanges(b.runs)}</span>` : ""}
      <div style="margin:4px 0" class="muted">${esc(phases)}</div>
      ${obs.length ? `<div style="margin-top:6px"><span class="muted" style="font-size:12.5px">Benchmark tool as it ran${info("Seen by the agent while measuring: the processes the benchmark command started, minus shells and helpers - full command line, version, the script files the tool was given and the environment variables those scripts read. Passwords masked.")}</span>${obs.map(tool).join("")}</div>
        <details><summary>Harness: how the agent started it</summary>${harness}</details>` : harness}</div>`;
  };
  const head = list.map(b => (b.observed || [])[0]).filter(Boolean).map(o => o.argv.slice(0, 3).map(shq).join(" "))[0];
  return `<details class="card" style="margin-top:14px"><summary style="cursor:pointer"><b>Benchmark</b>${info("What each run executed, as recorded by the agent: the benchmark tool's own command line and configuration, the warm-up and measured phases, and how the agent started it. Runs with a different specification (for example after the agent lengthened the warm-up) are listed separately.")}
    <span class="muted" style="margin-left:6px">${esc(list[0].name || "")}${head ? " · " + esc(head) : ""}${list.length > 1 ? ` · ${list.length} specifications` : ""}</span></summary>${list.map(one).join("")}</details>`;
}
async function sessionPage(id) {
  setNav("envs");
  const d = await api("/api/sessions/" + id);
  const s = d.session, runs = d.runs.filter(r => r.status !== "aborted");
  const done = runs.filter(r => r.goal_value != null && !EXCLUDED.includes(r.verdict));
  // swap added or removed during the session: each run says which state it measured (the header shows the latest)
  const mixedSwap = new Set(runs.filter(r => r.summary && r.summary.swap_total_mb != null).map(r => r.summary.swap_total_mb === 0)).size > 1;
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
  const added = x => (x.tradeoffs_added || x.tradeoffs || []);  // beyond the starting configuration's trade-offs
  const safeCfgs = cfgs.filter(c => !added(c.runs[0]).length);
  const bestSafe = safeCfgs[0], bestTrade = cfgs.find(c => added(c.runs[0]).length);
  const bestCfg = cfgs[0], best = bestCfg ? bestCfg.runs[bestCfg.runs.length - 1] : null;
  // columns that say something for this session: TPS when it is not the goal itself, per-transaction work when there are transactions
  const tpsRuns = done.filter(r => (r.summary || {}).tps != null);
  const analytic = tpsRuns.length > 0 && tpsRuns.every(r => r.summary.tps < 1);
  const cols = { tps: !analytic && !(tpsRuns.length && tpsRuns.every(r => Math.abs(r.summary.tps - r.goal_value) <= 0.02 * Math.abs(r.goal_value || 1))),
    avg: goalUnit(s.goal, s) !== "ms", perTrx: !analytic };
  // reference for every gain on this page (and on the environment, study and run pages): the calibrated starting
  // configuration (calibration mean), else the first run labelled baseline / calibration, else run #1; server defaults
  // are usually far below it and percentages against them say little
  const isBase = r => /^(baseline|calibration)/i.test(String(r.label || ""));
  const baseRun = done.find(isBase) || first;
  const startCfg = baseRun ? cfgs.find(c => c.runs.includes(baseRun)) : null;
  const calMean = s.calibration && s.calibration.goal_mean;
  const refVal = calMean || (startCfg ? startCfg.mean : null);
  const refN = calMean ? (s.calibration.runs || []).length || 1 : startCfg ? startCfg.runs.length : 1;
  const refName = calMean ? "calibrated start" : baseRun && isBase(baseRun) ? "baseline" : "first run";
  const vsRef = v => refVal ? (lower ? (refVal - v) / refVal : (v - refVal) / refVal) * 100 : null;
  const vsFirst = v => first && first.goal_value ? (lower ? (first.goal_value - v) / first.goal_value : (v - first.goal_value) / first.goal_value) * 100 : null;
  const unit = goalUnit(s.goal, s);
  const empty = !done.length && s.status !== "active";  // closed without measured runs: nothing to chart or recommend
  const T = { ref: refVal, refN, refName, lower, sess: s, noise: noiseP, vsFirst, first };
  setTitle(`#${s.id} ${s.name}`);
  const lastSwap = [...done].reverse().find(r => r.summary && r.summary.swap_total_mb != null);
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${d.environment.id}">${esc(d.environment.name)}</a> /</div>
  <div class="row"><h1><span class="muted" style="font-weight:500">#${s.id}</span> ${esc(s.name)}</h1>${statusBadge(s.status)}<span class="spacer"></span><button class="ghost" id="rename">Rename</button><button class="ghost danger" id="del">Delete</button></div>
  <div class="chips"><span><span class="k">Goal</span><b>${goalName(s.goal)}</b>${lower ? ' <span class="muted">(lower is better)</span>' : ""}</span>
   <span><span class="k">Policy</span><a href="#/policies"><b>${esc(s.policy)}</b></a>${info(s.policy === "benchmark" ? "Trade-offs allowed after the production-safe experiments." : "Production-safe changes only.")}</span>
   <span><span class="k">Planner</span>${s.planner ? `<span class="badge info" data-tip="An AI planner chose each next step from the evidence (the rules still checked every choice). The Recommendations of each run show what the planner and the rules would have done." tabindex="0">AI planner</span>` : s.ai === "off" ? `<span data-tip="This session asked no AI model anything: built-in rules plus learnings from earlier sessions only (session start --ai off)." tabindex="0">rules + learnings, no AI</span>` : "rules"}</span>
   <span><span class="k">OS tuning</span>${s.os_tuning === 0 ? "off" : "on"}${info("When on, the agent may also change operating-system settings (swappiness, transparent huge pages, I/O scheduler, mount options, dirty writeback, memory allocator, huge pages). OS changes are recorded per run and rolled back like MySQL settings. Disable with --no-os-tuning.")}</span>
   <span><span class="k">Noise</span>±${s.noise_pct}%</span>
   <span><span class="k">Started</span>${fmtTime(s.created)}</span>${lastSwap && noSwapBadge(lastSwap.summary) ? `<span>${noSwapBadge(lastSwap.summary)}</span>` : ""}</div>
  ${s.workload ? `<p class="sub wl">workload: ${esc(s.workload)}</p>` : ""}
  ${s.activity ? `<div style="margin:0 0 10px">${activityLine(s.activity)}</div>` : ""}
  ${startTradeoffNote(s)}${qualityLine(s, d)}
  <p class="sub study-line" style="margin-top:-6px">Study${info("A study groups sessions from several environments running the same workload, e.g. to compare hardware or find the smallest hardware that reaches a target.")}: <select id="studySel" style="width:auto"><option value="">none</option></select>${d.study ? ` <a href="#/study/${d.study.id}">open study</a>` : ""}</p>
  ${empty ? `<div class="note">Closed without measured runs.</div>` : `<div class="tiles">${bestSafe ? cfgTile("Best production-safe", bestSafe, Object.assign({ lead: true }, T)) : `<div class="tile lead"><div class="k">Best production-safe</div><div class="v">–</div><div class="d">${done.length ? "every measured configuration has trade-offs" : "no measured run yet"}</div></div>`}
   ${bestTrade ? cfgTile("Best with trade-offs", bestTrade, Object.assign({ trade: true }, T)) : ""}
   <div class="tile"><div class="k">Start (${calMean ? "calibrated" : esc(refName)})${info(calMean ? `Mean of the calibration runs ${(s.calibration.runs || []).map(x => "#" + x).join(", ")} (${unit}): the reference for every gain on this page.` : `${baseRun ? `Run #${baseRun.seq} ${baseRun.label}` : "The first run"} (${unit}): the reference for every gain on this page.`)}</div><div class="v">${num(refVal)}</div>
    ${first && (calMean || baseRun !== first) ? `<div class="d2">server defaults ${num(first.goal_value)} <span data-tip="${esc(`Run #${first.seq} ${first.label}`)}" tabindex="0">(run #${first.seq})</span></div>` : first ? `<div class="d2">${esc(first.label)}</div>` : ""}</div></div>
  <p class="meta">${done.length} run${done.length === 1 ? "" : "s"} measured${d.cost ? ` · machine cost $${d.cost.total.toFixed(2)} (${d.cost.hours} h)${info(`Session wall time ${d.cost.hours} h × $${d.cost.rate_hourly.toFixed(4)}/hour (the environment's price). Per-run costs are shown under each run's time.`)}` : ""}</p>`}
  ${bestSafe || bestTrade ? `<details class="card" id="bestcfg" style="margin-top:10px"><summary style="margin:0;cursor:pointer"><b>Best known configuration</b> <span class="muted">· what differs from the defaults (MySQL and OS)</span></summary><div id="bestcfgBody" class="muted" style="margin-top:8px">Loading…</div></details>` : ""}
  ${empty ? "" : `<div class="card" style="margin-top:14px"><div class="row" id="barScopeRow" hidden><span class="spacer"></span><div class="seg small" role="group" aria-label="Runs in the chart" id="barScope"><button data-b="safe">Production-safe runs</button><button data-b="all">All runs</button></div></div><div id="bars"></div></div>`}
  ${benchCard(d.benchmarks || [])}
  ${d.runs.length ? `<h2>Runs</h2><div id="runFlags"></div><div class="card tablewrap"><table class="runs" data-opt="TPS|Avg ms|CPU ms/trx|IOs/trx|Time"><thead><tr><th>#</th><th>Run</th><th class="chg">Changes</th><th class="num">${esc(unit)}</th><th class="num">Δ parent${info("Change of the goal vs the mean of the parent configuration (the configuration this run was changed from). Coloured only beyond the noise threshold." + (lower ? " Faster = lower response time." : ""))}</th><th class="num">vs start${info(`Change vs the ${refName === "calibrated start" ? "calibrated starting configuration (calibration mean)" : refName} = ${num(refVal)}. Bold: the best configuration known at that point of the session (it took the lead by beating the previous best beyond the noise threshold). Changes shown struck through were rolled back afterwards; ↩ marks settings undone before the run.`)}</th><th>Verdict</th>
   ${cols.tps ? '<th class="num">TPS</th>' : ""}${cols.avg ? '<th class="num">Avg ms</th>' : ""}${cols.perTrx ? '<th class="num">CPU ms/trx</th><th class="num">IOs/trx</th>' : ""}<th class="num">Time</th></tr></thead><tbody id="runs"></tbody></table></div>` : ""}
  ${empty ? "" : `<h2>Experiments${info("Your own experiments: try a set of settings, or sweep one variable over several values (e.g. buffer pool 1G, 2G, 3G). They run before the tuner's own ideas, each value measured from the same parent configuration with the session's benchmark. The best value is kept only if it beats the parent beyond the noise threshold.")}</h2><div class="card" id="exps"></div>
  <h2>Latest recommendations</h2><div class="card" id="recs"></div>`}
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
  // trade-off runs far above the production-safe ones set the scale and flatten the safe progress: shown on request then
  const isTrade = r => (r.tradeoffs_added || r.tradeoffs || []).length > 0;
  const tradeRuns = done.filter(isTrade);
  let barScope = "all";
  if (tradeRuns.length && bestSafe) {
    const ext = lower ? Math.min(...tradeRuns.map(r => r.goal_value)) : Math.max(...tradeRuns.map(r => r.goal_value));
    if (lower ? ext * 1.5 < bestSafe.mean : ext > 1.5 * bestSafe.mean) barScope = "safe";
  }
  const drawBars = () => {
    const box = document.getElementById("bars"); if (!box) return; box.innerHTML = "";
    const row = document.getElementById("barScopeRow");
    if (tradeRuns.length && done.length > tradeRuns.length) { row.hidden = false; row.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.b === barScope)); }
    const shown = barScope === "safe" ? done.filter(r => !isTrade(r)) : done;
    C.barChart(box, {
      title: `${goalName(s.goal)} per run${unit && !goalName(s.goal).toLowerCase().includes(unit.toLowerCase()) ? ` (${unit})` : ""}${lower ? ", lower is better" : ""}`, unit, metric: s.goal === "latency" ? "avg latency" : "goal value",
      legend: [["var(--s3)", "best production-safe configuration"], ...(barScope === "all" && tradeRuns.length ? [["var(--s2)", "with trade-offs"]] : []), ["var(--axis)", "other runs"], ["mark", "best configuration known at that point"]],
      ref: refVal, refLabel: refName, subLabel: "vs " + refName,
      bars: shown.map(r => ({ label: "#" + r.seq + " " + r.label, short: "#" + r.seq, value: r.goal_value, sub: vsRef(r.goal_value) != null ? gainTxt(vsRef(r.goal_value), lower) : "", href: "#/run/" + r.id, mark: bestAt[r.id],
        highlight: isTrade(r) ? "trade" : bestSafe && bestSafe.runs.includes(r) ? "best" : false,
        tip: `<div>vs previous<b>${gainTxt(r.delta_pct, lower)}</b></div><div>verdict<b>${esc(r.verdict || "")}</b></div>` })),
    });
  };
  if (!empty) {
    drawBars(); widthFns.push(drawBars);
    document.querySelectorAll("#barScope button").forEach(b => b.onclick = () => { barScope = b.dataset.b; drawBars(); });
  }
  function changeLines(r, chs, fate) {
    // undo = a change that only returns a setting to its value before the previous run changed it
    // undo = a change that returns a setting to the best configuration known before this run (reverts, sweep endings)
    const lead = leadBefore[r.id];
    const isUndo = c => c.source === "rollback" || (lead != null && String((r.managed || {})[c.variable]) === String(lead[c.variable]));
    const own = chs.filter(c => !isUndo(c)), undo = chs.filter(isUndo);
    const line = c => {
      // from -> to, so the direction of a numeric change is visible at a glance
      const f = fate(r, c), name = esc(c.variable.replace(/^innodb_/, "")).replace(/_/g, "_<wbr>");
      const from = c.old != null && c.old !== "" ? esc(shortVal(c.variable, c.old)) : null, to = esc(shortVal(c.variable, c.new));
      const fd = flagDiff(c.old, c.new);
      const txt = fd ? `${name}: ${fd.map(x => `${esc(x.flag)} <span class="muted">${esc(x.from || "–")}</span> → <b>${esc(x.to || "–")}</b>`).join(", ")}`
        : `${name} ${from != null && from !== to ? `<span class="muted">${from}</span> → ` : "= "}<b>${to}</b>`;
      const tip = `${c.variable}: ${c.old != null ? val(c.variable, c.old) : "?"} → ${val(c.variable, c.new)}`;
      return f.kept ? `<span class="mono chg-line" data-tip="${esc(tip)} · ${f.pending ? "in effect now" : "kept: still in effect in the later runs"}" tabindex="0">${txt}</span>`
        : `<span class="mono chg-line rolled" data-tip="${esc(tip)} · rolled back before run #${f.by}" tabindex="0">${txt} <span class="undo-tag">undone #${f.by}</span></span>`;
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
  if (!tb) { /* no runs */ }
  else if (!d.runs.length) tb.innerHTML = `<tr><td colspan="12" class="muted">No runs yet: <code>tuning-agent measure start</code> … <code>measure stop</code>.</td></tr>`;
  // badges for a run's verdict cell: {key, html, text}; text = what the "+N" chip's tooltip says about it
  const badge = (key, cls, name, tip) => ({ key, text: name + (tip ? ": " + tip : ""), html: ` <span class="badge ${cls}"${tip ? ` data-tip="${esc(tip)}" tabindex="0"` : ""}>${esc(name)}</span>` });
  const runFlags = r => {
    const sm = r.summary || {}, ev = r.evaluation || {}, out = [];
    const notSteady = ev.not_steady || (sm.steady_state && sm.steady_state.steady === false ? sm.steady_state.growing : null), warmShort = ev.warmup_short || sm.warmup_signals;
    if (mixedSwap && sm.swap_total_mb === 0) out.push(badge("noswap", "warn", "no swap", NO_SWAP_TIP));
    if (ev.promising) out.push(badge("promising", "info", "promising", `${pct(ev.promising.goal_delta_pct)} beats the measured variation (±${ev.promising.point_threshold_pct}%) but not the ±${ev.promising.threshold_pct}% floor that holds until a re-check measures drift: measured again (with repeats) once a re-check has lowered the threshold.`));
    if (ev.secondary_gain) out.push(badge("faster", "good", "faster statements", "Statements got faster at no extra CPU or I/O per transaction: " + Object.values(ev.secondary_gain).map(g => `${g.label} ${num(g.parent)} → ${num(g.now)} ms (−${g.faster_pct}%)`).join("; ") + ". Offered in the recommendations as a latency improvement."));
    if (notSteady) out.push(badge("notsteady", "warn", "not steady", `Not in steady state: deferred work was still accumulating at the end of the measured window (${notSteady.join("; ")}). Flushing, purge or change-buffer merges postponed past the window are never paid for, so the result can be optimistic; such improvements do not count as the best configuration.`));
    if (ev.unbounded || sm.unbounded_growth) out.push(badge("unbounded", "bad", "unsustainable", "Unsustainable: " + (ev.unbounded || sm.unbounded_growth).map(u => `${u.label} grows without slowing down (${num(u.start)} → ${num(u.end)} in the window, ~${num(u.per_hour)} per hour)`).join("; ") + ". This configuration cannot keep up with the load: left running, the backlog grows until it fails. A longer warm-up would not settle it."));
    if (ev.low_free_space || (sm.datadir_free_pct != null && sm.datadir_free_pct < 10)) out.push(badge("disk", "warn", "low disk space", `The data disk had ${sm.datadir_free_gb} GiB free (${sm.datadir_free_pct}%)${sm.datadir_full_in_h ? `, filling at ${sm.datadir_growth_gb_per_h} GiB/h (full in ~${sm.datadir_full_in_h} h)` : ""}. Below ~10% free, SSDs and filesystems slow down: the result may drift for reasons that are not the configuration.`));
    if (ev.drift) out.push(badge("drift", "warn", "drift", ev.note || ""));
    if (warmShort) out.push(badge("warmup", "plain", "warm-up?", sci(warmShort.text) + " A warning about the warm-up, not about this run's result: the verdict is unchanged."));
    if (ev.unstable_within_run) { const u = ev.unstable_within_run; out.push(badge("stalls", "warn", "stalls", `Throughput fell below half its median in ${u.tput_dip_samples} of ${u.tput_samples} samples inside the window (longest stall ${u.tput_dip_longest_samples} samples, worst sample ${u.tput_min_vs_median_pct}% of the median)${u.blk_write_max_ms != null ? `; slowest write I/O ${u.blk_write_max_ms} ms` : ""}. The average hides these stalls.`)); }
    if (ev.swap_hazard) out.push(badge("swapin", "warn", "swap-in", `mysqld memory (${ev.swap_hazard.dbproc_swap_mb} MB in swap) was read back from swap during the window. A thread waiting for a swap-in while holding a latch or lock stalls everything queued behind it, so configurations without swap-ins are preferred as the best.`));
    if (ev.environment_shift && !["reference", "repeat", "unreliable", "superseded"].includes(r.verdict)) out.push(badge("envshift", "warn", "env shift", `The quick CPU / storage benchmark run before this run differed from the calibration runs beyond its normal variation (${kvText(ev.environment_shift)} %). The machine itself may have changed (noisy neighbour, throttling), so this run's comparison is less trustworthy; autotune measures such runs again.`));
    if (ev.steal && ev.steal.level === "flag") out.push(badge("steal", "warn", "steal", `Hypervisor CPU steal ${ev.steal.avg_pct}% average, ${ev.steal.peak_pct}% peak${ev.steal.client_avg_pct ? `, client ${ev.steal.client_avg_pct}%` : ""}, estimated effect on the result ${ev.steal.est_impact_pct != null ? ev.steal.est_impact_pct : ev.steal.avg_pct}% (flagged from a quarter of the noise threshold, ${ev.steal.flag_at_pct || 0.25}%, or a 5% peak; normally close to 0 on these hosts): differences near the noise threshold may come from neighbours.`));
    return out;
  };
  // a flag on most runs is a property of the session (benchmark, data set), said once above the table
  const COMMON = { notsteady: "not in steady state", unbounded: "unsustainable backlog growth", warmup: "warm-up warning", stalls: "throughput stalls", steal: "CPU steal" };
  const flagsOf = {}; done.forEach(r => { flagsOf[r.id] = runFlags(r); });
  const common = Object.keys(COMMON).filter(k => { const n = done.filter(r => flagsOf[r.id].some(f => f.key === k)).length; return n >= 3 && n >= 0.6 * done.length; });
  const rf = document.getElementById("runFlags");
  if (rf && common.length) rf.innerHTML = `<div class="note" style="margin-bottom:8px">${common.map(k => { const rs = done.filter(r => flagsOf[r.id].some(f => f.key === k)); const ex = flagsOf[rs[rs.length - 1].id].find(f => f.key === k);
    return `<span class="badge ${k === "unbounded" ? "bad" : "warn"}" data-tip="${esc(`Latest (run #${rs[rs.length - 1].seq}): ${ex.text}`)}" tabindex="0">${COMMON[k]}</span> in ${rs.length} of ${done.length} runs`; }).join(" · ")}${info("Flags that apply to most runs of this session come from the benchmark or the data set rather than from the configurations, so they are shown once here instead of on every run.")}</div>`;
  if (tb) d.runs.forEach(r => {
    const sm = r.summary || {};
    const chs = r.changes || [];
    const ch = r.status === "aborted" ? `<span class="muted">${esc((r.abort_reason || "aborted").split(":")[0])}</span>${r.abort_reason ? info(r.abort_reason) : ""}`
      : r.seq === 1 ? `<a href="#/run/${r.id}">${r === baseRun || !calMean ? "starting configuration" : "first run"}</a>`
      : !chs.length ? '<span class="muted">none</span>'
      : changeLines(r, chs, fate);
    const costOf = x => d.cost && d.cost.per_run ? d.cost.per_run[x.id] : null;
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = ev => { if (!ev.target.closest("a, button, [data-tip]")) location.hash = "#/run/" + r.id; };
    const ev = r.evaluation || {};
    const rfb = (d.feedback || []).filter(f => f.run_id === r.id);
    // verdict, then at most one flag; the rest behind a "+N" chip
    const fl = (flagsOf[r.id] || runFlags(r)).filter(f => !common.includes(f.key));
    const shownFl = fl.slice(0, 1), restFl = fl.slice(1);
    const vr = vsRef(r.goal_value);
    tr.innerHTML = `<td style="white-space:nowrap">${r.seq}${ME && !ME.share ? starBtn(r.id, r.starred) : ""}</td><td><a href="#/run/${r.id}">${esc(r.label)}</a>${r.status !== "done" ? " " + statusBadge(r.status, r.abort_reason) : ""}${tradeBadge(r.tradeoffs_added || r.tradeoffs)}${fbMark(rfb)}<div class="chg-m">${ch}</div></td><td class="chg">${ch}</td>
     <td class="num"><b>${num(r.goal_value)}</b></td><td class="num ${deltaClsN(ev.goal_delta_pct, noiseP)}">${gainTxt(ev.goal_delta_pct, lower)}</td><td class="num ${deltaClsN(r.goal_value != null ? vr : null, noiseP)}">${r.goal_value != null && vr != null ? (bestAt[r.id] ? `<b data-tip="Best configuration known at this point" tabindex="0">${gainTxt(vr, lower)}</b>` : gainTxt(vr, lower)) : "–"}</td>
     <td>${verdictBadge(r.verdict)}${r.verdict === "neutral" && ev.note ? info(ev.note) : ""}${shownFl.map(f => f.html).join("")}${restFl.length ? ` <span class="badge plain" data-tip="${esc(restFl.map(f => f.text).join("\n\n"))}" tabindex="0">+${restFl.length}</span>` : ""}</td>
     ${cols.tps ? `<td class="num">${num(sm.tps)}</td>` : ""}${cols.avg ? `<td class="num">${num(sm.avg_latency_ms)}</td>` : ""}${cols.perTrx ? `<td class="num">${num(sm.mysqld_cpu_ms_per_trx)}</td><td class="num">${num(sm.disk_ios_per_trx)}</td>` : ""}<td class="num" style="white-space:nowrap">${dur(sm.duration_s || (r.status === "measuring" ? Date.now() / 1000 - r.started : null))}${costOf(r) != null ? `<div class="muted" style="font-size:11px" data-tip="Machine cost of this run: wall time since the previous run ended (data reset, warm-up, measurement) × $${(d.cost.rate_hourly).toFixed(4)}/hour" tabindex="0">$${costOf(r).toFixed(costOf(r) < 0.1 ? 3 : 2)}</div>` : ""}</td>`;
    tb.appendChild(tr);
  });
  if (tb) wireStars(tb);
  const bc = document.getElementById("bestcfg");
  if (bc) bc.addEventListener("toggle", () => { if (bc.open && !bc.dataset.loaded) { bc.dataset.loaded = 1; renderBestConfig(document.getElementById("bestcfgBody"), bestSafe, bestTrade, first); } });
  if (!empty) {
    renderRecs(document.getElementById("recs"), d.recommendations[0], runs, d.feedback);
    renderExperiments(document.getElementById("exps"), s);
  }
  renderEvents(document.getElementById("events"), null, false, { session: id });
  document.getElementById("savenotes").onclick = async () => { await api("/api/sessions/" + id, { method: "PATCH", body: { notes: document.getElementById("notes").value } }); document.getElementById("saved").textContent = "Saved."; };
  document.getElementById("rename").onclick = async () => { const n = prompt("Session name", s.name); if (n) { await api("/api/sessions/" + id, { method: "PATCH", body: { name: n } }); sessionPage(id); } };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this session and its runs?")) { await api("/api/sessions/" + id, { method: "DELETE" }); location.hash = "#/env/" + d.environment.id; } };
  if (runs.some(r => r.status === "measuring") || s.status === "active") timers.push(setInterval(async () => {
    const d2 = await api("/api/sessions/" + id);
    if (JSON.stringify(d2.runs.map(r => [r.id, r.status])) !== JSON.stringify(d.runs.map(r => [r.id, r.status])) || d2.recommendations.length !== d.recommendations.length) { clearTimers(); sessionPage(id); }
  }, 10000));
}

// paths, identities and file locations differ from compiled defaults on every server: not configuration choices
const NOT_TUNING = /(^|_)(dir|file|path|socket|pid|port|uuid|hostname|ssl|tls|key|cert|ca|version|plugin_load|server_id|log_error|basedir|datadir|tmpdir|character_sets_dir|lc_messages)/;
async function renderBestConfig(el, bestSafe, bestTrade, first) {
  const lastRun = cfg => cfg && cfg.runs[cfg.runs.length - 1];
  const want = [["Best production-safe", lastRun(bestSafe)], ...(bestTrade && bestTrade !== bestSafe ? [["Best with trade-offs", lastRun(bestTrade)]] : [])].filter(x => x[1]);
  try {
    const [cfgs, start] = await Promise.all([Promise.all(want.map(([, r]) => api(`/api/runs/${r.id}/config`))), first ? api(`/api/runs/${first.id}/config`) : null]);
    el.classList.remove("muted");
    el.innerHTML = want.map(([title, r], i) => bestConfigHtml(title, r, cfgs[i], start)).join('<div style="height:14px"></div>');
  } catch (e) { el.textContent = "Could not load the configuration: " + e.message; }
}
function bestConfigHtml(title, run, c, start) {
  const meta = c.meta || {}, cfg = c.config || {}, managed = c.managed || {}, atDef = new Set(c.at_default || []);
  const isDef = k => isDefault(meta[k]) || atDef.has(k);
  const mine = Object.keys(managed).filter(k => !/^(os|rg|client):/.test(k) && !isDef(k)).sort();
  const others = Object.keys(cfg).filter(k => !/^(os|rg):/.test(k) && !(k in managed) && meta[k] && meta[k].default != null && !isDef(k) && !NOT_TUNING.test(k)).sort();
  const os = Object.keys(managed).filter(k => /^os:/.test(k)).sort(), rt = Object.keys(managed).filter(k => /^rg:/.test(k) && !/^off$/i.test(String(managed[k]))).sort();
  const sc = (start && start.config) || {};
  const unsafe = unsafeMap(run.tradeoffs);
  const row = (k, v, d, key) => `<tr${unsafe[key || k] ? ` class="unsafe" data-tip="${esc(unsafe[key || k])}" tabindex="0"` : ""}><td class="mono">${esc(k)}</td><td class="mono"><b>${esc(val(k, v))}</b></td><td class="mono muted">${d != null ? esc(val(k, d)) : "–"}</td></tr>`;
  const tbl = (head, rows) => `<div class="tablewrap"><table><thead><tr><th>Setting</th><th>Value</th><th>${head}</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const myCnf = cnfBlock(Object.fromEntries(mine.map(k => [k, managed[k]])), unsafe);
  return `<h3 style="margin:0 0 4px">${esc(title)} <span class="muted" style="font-weight:400">· run <a href="#/run/${run.id}">#${run.seq} ${esc(run.label)}</a></span>${tradeBadge(run.tradeoffs_added || [])}</h3>
    <h4 style="margin:8px 0 4px">MySQL: set by the tuner (${mine.length})${Object.keys(unsafe).length ? ` <span class="unsafe" style="font-weight:400;font-size:12.5px">red = not production-safe</span>` : ""}</h4>
    ${mine.length ? tbl("Compiled default", mine.map(k => row(k, cfg[k] != null ? cfg[k] : managed[k], (meta[k] || {}).default)).join("")) : '<p class="muted" style="margin:0">Nothing differs from the compiled defaults.</p>'}
    ${myCnf ? `<details style="margin-top:6px"><summary style="cursor:pointer">Option file (copy into my.cnf)</summary><pre><code>${myCnf}</code></pre></details>` : ""}
    ${os.length ? `<h4 style="margin:10px 0 4px">Operating system (${os.length})</h4>${tbl("Host at session start", os.map(k => row(k.replace(/^os:/, ""), managed[k], sc[k], k)).join(""))}` : ""}
    ${rt.length ? `<h4 style="margin:10px 0 4px">Applied after MySQL starts (${rt.length})${info("MySQL resource groups: thread CPU binding and priority. No option file can carry them - the group definitions survive a restart, the thread assignments do not. The agent re-applies them after every start; without the agent run the SQL below after each start (for example from an init_file). Thread priorities need CAP_SYS_NICE for mysqld (systemd AmbientCapabilities=CAP_SYS_NICE).")}</h4>${tbl("Host at session start", rt.map(k => row(k.replace(/^rg:/, ""), managed[k], sc[k], k)).join(""))}
      <details style="margin-top:6px"><summary style="cursor:pointer">SQL to apply after each start</summary><pre><code>${esc(rgSql(Object.fromEntries(rt.map(k => [k, managed[k]]))))}</code></pre></details>` : ""}
    ${others.length ? `<details style="margin-top:8px"><summary style="cursor:pointer">Other non-default MySQL settings on this server, not set by the tuner (${others.length})${info("From the server's own option files or the benchmark prerequisites (e.g. max_connections for many client threads). Paths, ports, identities and certificates are left out.")}</summary>${tbl("Compiled default", others.map(k => row(k, cfg[k], meta[k].default)).join(""))}</details>` : ""}`;
}

function cfgTile(title, cfg, o) {
  // o: {ref, refN, refName, lower, sess, noise, vsFirst, trade, lead}; the gain is vs the session's calibrated start
  if (!cfg) return "";
  const g = o.ref ? (o.lower ? (o.ref - cfg.mean) : (cfg.mean - o.ref)) / o.ref * 100 : null;
  const thr = meanNoise(o.noise, cfg.runs.length, o.refN || 1), flat = g != null && Math.abs(g) <= thr;
  const runs = cfg.runs.map(r => "#" + r.seq).join(", ");
  const unit = goalUnit(o.sess.goal, o.sess), vf = o.vsFirst(cfg.mean);
  const tip = `${cfg.runs.length > 1 ? `Mean of runs ${runs}` : `Run ${runs}`} (${unit}). Gain vs the ${o.refName} ${num(o.ref)}; changes within ±${thr.toFixed(2)}% (the noise threshold for these means) are noise.` +
    (vf != null ? ` vs server defaults (run #1): ${gainTxt(vf, o.lower)}.` : "") +
    (o.trade ? ` Trade-offs: ${(cfg.runs[0].tradeoffs_added || cfg.runs[0].tradeoffs || []).map(t => t.variable + "=" + t.value).join(", ")}.` : "");
  const gl = g == null ? "" : flat ? `<div class="d">no gain beyond noise <span class="muted">(${gainTxt(g, o.lower)}, noise ±${thr.toFixed(2)}%)</span></div>`
    : `<div class="d ${g > 0 ? "up" : "down"}">${gainTxt(g, o.lower)} vs ${esc(o.refName)}</div>`;
  return `<div class="tile${o.lead ? " lead" : ""}"><div class="k">${esc(title)}${o.lower ? " (lower is better)" : ""}${info(tip)}</div><div class="v">${num(cfg.mean)} <span style="font-size:13px;font-weight:400;color:var(--ink-2);white-space:nowrap">${esc(unit)}</span></div>
   ${gl}<div class="d2">${cfg.runs.length > 1 ? "runs" : "run"} ${runs}</div></div>`;
}

function startTradeoffNote(s) {  // the benchmark setup itself is not production-safe: said once, not on every run
  const t = s.start_tradeoffs || [];
  if (!t.length) return "";
  return `<div class="note" style="margin-bottom:10px"><span class="badge warn">not production-safe setup</span> The starting configuration of this session already gives up ${t.map(x => x.category === "memory" ? "<b>memory</b> (over the memory budget)" : `<b>${esc(x.category)}</b> (<code>${esc(x.variable)}=${esc(x.value)}</code>)`).join(", ")}${info(t.map(x => `${x.variable}=${x.value}: ${x.lost}`).join(" · ") + " Every run inherits this from the benchmark setup, so results compare configurations under it. Runs are marked as trade-offs only for what the tuner added on top (for memory: needing more than the starting configuration), and the best production-safe configuration is the best one without added trade-offs.")}.</div>`;
}

function tradeBadge(t) {
  if (!t || !t.length) return "";
  return ` <span class="badge bad" data-tip="${esc(t.map(x => x.variable + "=" + x.value + ": " + x.lost).join("\n"))}" tabindex="0">trade-off</span>`;
}

function qualityLine(s, d) {
  // noise, calibration plan, warm-up, data state, seasoning and benchmark prerequisites: one line of badges, the details on hover
  const c = s.calibration, out = [];
  const b = (cls, name, tip) => `<span class="badge ${cls}" data-tip="${esc(sci(tip))}" tabindex="0">${name}</span>`;
  if (!c) out.push(b("warn", `noise not calibrated: ±${s.noise_pct || 2}%`, "Fixed threshold. Standard protocol: measure the starting configuration 3 times with `tuning-agent calibrate --runs 3`; the run-to-run variation then sets the noise threshold."));
  else {
    const eff = Object.entries(c.efficiency || {}).filter(([k]) => EFF_LABELS[k]).map(([k, e]) => `${EFF_LABELS[k]} ±${e.noise_pct}%`).join(" · ");
    const basis = c.estimate ? `pooled CV ${c.estimate.pooled_cv_pct}% over ${c.estimate.dof} repeat${c.estimate.dof === 1 ? "" : "s"}${c.estimate.long_term_dof ? ", including re-checks over time" : ""}` : `CV ${c.goal_cv_pct}% over ${c.runs.length} runs`;
    const how = `Changes within ±${c.noise_pct}% are treated as noise (${basis}). Calibration runs ${c.runs.map(x => "#" + x).join(", ")}: mean ${num(c.goal_mean)}, CV ${c.goal_cv_pct}%, range ${c.goal_range_pct}%. CV (coefficient of variation) = standard deviation ÷ mean. Threshold = ${c.k || 2.5} × CV: a run of an unchanged configuration lands outside it only about 1 time in 100. Natural variance depends on the environment and the workload, so it is re-estimated during the session from every configuration measured more than once; repeats separated by other runs also capture drift. Until such repeats exist, earlier sessions of the same benchmark on this environment are used, else at least 2%.${c.estimate ? ` Current basis: ${c.estimate.basis}; pooled CV ${c.estimate.pooled_cv_pct}%.` : ""} Experiments are compared with the mean of the most recent runs of the configuration they were changed from.${eff ? ` Efficiency thresholds: ${eff}.` : ""}`;
    const p = c.plan;
    let split = "";
    if (p && p.within_cv_pct != null) {
      // only the parts that were measured
      const parts = [`within-run ${p.within_cv_pct}%`, p.between_cv_pct != null ? `between-run ${p.between_cv_pct}%` : null].filter(Boolean).join(" · ");
      split = ` Run-to-run variation (CV): ${parts}. Within-run: what each run's own 10 s samples predict for the spread of its mean at this run length (batch means of 60 s blocks); it shrinks with longer runs. Between-run: the rest of the observed run-to-run variation — shifts from run to run such as data-set state, restarts, caches or neighbours; longer runs do not reduce it, more runs per configuration or removing the cause do. Target: ±${p.target_pct}% (typical OLTP tuning gains are 2-8%).${p.text ? " " + p.text : ""}`;
    }
    out.push(b("plain", `noise ±${c.noise_pct}%`, how + split));
    if (p && p.action === "longer-runs" && p.applied) out.push(b("plain", "runs lengthened", p.previous ? `Runs lengthened from ${Math.round(p.previous.run_seconds / 60)} to ${Math.round(p.run_seconds / 60)} min (±${p.previous.noise_pct}% → ±${c.noise_pct}%).` : "Runs lengthened to bring the noise down."));
    else if (p && p.action === "longer-runs") out.push(b("warn", "noisy: within runs", `Mostly within-run variation: ${Math.round(p.seconds / 60)} min runs would bring it to about ±${p.expected_noise_pct || p.target_pct}%.`));
    else if (p && p.action === "between-runs") out.push(b("warn", "noisy: between runs", `Mostly between runs: longer runs will not help; changes under ±${c.noise_pct}% cannot be seen (${p.repeats >= 9 ? "9+" : "~" + p.repeats} runs per configuration needed, or remove the run-to-run shifts, e.g. reset the data set before each run).`));
    const wu = c.warmup;
    if (wu && wu.adequate === false) out.push(b("warn", "warm-up too short", wu.text + (wu.applied_warmup_s ? ` Warm-up raised to ${wu.applied_warmup_s} s and calibration repeated.` : "")));
    else if (wu && wu.settling_runs) out.push(b("warn", "warm-up may be short", "A warning, not a fault: results stay valid. An adequate warm-up ends in the state the measured window sees - its last 10-20% should look like the window. Here state such as checkpoint age or buffer pool contents was still moving when measuring began, so part of each window measured a different regime. " + wu.text));
    else if (wu && wu.previous) out.push(b("plain", "warm-up raised", `Warm-up raised from ${wu.previous.warmup_s} s to ${wu.warmup_s} s: ${wu.text}`));
    const ds = c.data_state;
    if (ds && ds.status === "drifting") out.push(b("warn", "data not restored", `The benchmark changes data${ds.rows_changed_per_s ? ` (${num(ds.rows_changed_per_s)} rows/s)` : ""} but runs neither restore it nor start from a seasoned steady state${ds.trend_pct_per_run != null ? `; calibration trend ${ds.trend_pct_per_run > 0 ? "+" : ""}${ds.trend_pct_per_run}% per run` : ""}. Each run starts from the data the previous runs left behind (row versions, page splits, fragmentation, undo), so differences between runs can come from the data rather than the configuration. Fix in the benchmark profile: a reset command that restores the data set before every run (data_state = reset), or data_state = seasoned: one long pre-run until throughput stops trending, so a series of tuning runs no longer moves the state much. Read-only benchmarks: data_state = read-only.`));
  }
  const sv = (d.events || []).find(e => e.type === "season"), x = sv && sv.data;
  if (x) {
    const ck = x.check || {}, why = { flat: "trend flat", plateau: "last hour equal to the hour before", flattening: "flattening, little change left" };
    out.push(b(x.steady ? "plain" : "warn", `data seasoned ${x.hours} h${x.steady ? "" : ", still trending"}`, `Data set seasoned for ${x.hours} h before calibration: ${x.steady ? `steady (${why[x.stop_reason] || x.stop_reason})` : "still trending at the time limit"}${x.trend_pct_per_hour != null ? `, trend ${x.trend_pct_per_hour > 0 ? "+" : ""}${x.trend_pct_per_hour}%/h` : ""}${ck.allowed_pct_per_hour != null ? ` (allowed ${ck.allowed_pct_per_hour}%/h)` : ""}. data_state = seasoned: the benchmark ran unmeasured until its throughput stopped trending (steady state of the data set), so the tuning runs that follow change the data little. Throughput was sampled every 5 minutes${x.continuous ? " during one continuous run" : " across benchmark iterations"}${ck.noise_pct != null ? `; scatter around the trend ${ck.noise_pct}%. The allowed trend keeps drift between re-checks of the current configuration (${x.span_h} h) within half a noise threshold, never below 0.5%/h; the stop needs the trend's upper bound (+1.65 standard errors) below it, an hour-to-hour plateau, or a flattening curve with little change left.` : "."}`));
  }
  const rq = d.requirements || [];
  if (rq.length) out.push(b("plain", `${rq.length} benchmark prerequisite${rq.length > 1 ? "s" : ""}`, rq.map(q => `${q.variable} ${/^\d+$/.test(q.required) ? "≥" : "="} ${q.required}`).join(", ") + ". " + REQ_TIP));
  return `<div class="note" style="margin-bottom:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center"><span class="muted">Measurement quality</span>${out.join("")}</div>`;
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
function autoSized(k, v, d) {
  if (d == null || v == null || k === "log_bin") return false;
  const n = x => { const t = String(x).trim().toLowerCase(); return { on: "1", true: "1", off: "0", false: "0" }[t] || t; };
  return n(v) !== n(d);
}
function isDefault(m) { return m && (m.source === "COMPILED" || m.source === "COMPILE-TIME" || m.source === "OS"); }
const WAIT_CLASS_NAMES = { table_io: "table I/O", data_io: "data file I/O", redo_io: "redo I/O", binlog_io: "binlog I/O", sync: "mutex / rwlock", table_lock: "table lock", metadata_lock: "metadata lock", socket: "socket", other_file_io: "other files", other: "other" };
// categorical colours for the stacked bars: the first 8 categories distinct, everything after them one grey "other"
const WAIT_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
const catColor = i => i < WAIT_COLORS.length ? WAIT_COLORS[i] : "var(--axis)";
function stackBar(items, name, value, tip) {  // items sorted largest first; [label, value] beyond 8 merged into "other"
  const top = items.slice(0, WAIT_COLORS.length), rest = items.slice(WAIT_COLORS.length);
  const rv = rest.reduce((a, x) => a + value(x), 0);
  const segs = top.map((x, i) => [name(x), value(x), catColor(i), tip(x)]).concat(rest.length ? [["other", rv, catColor(99), `other (${rest.map(name).join(", ")}): ${(+rv).toFixed(1)}`]] : []);
  return segs;
}
function waitsTable(inp, r) {
  const dur = (r && r.summary && r.summary.duration_s) || null;
  const cov = inp.wait_instruments;
  // table I/O (wait/io/table) times whole storage-engine row calls: CPU work plus the file I/O, mutex and lock waits nested
  // inside them (performance_schema NESTING_EVENT_ID) - adding it to those waits counts the same time twice
  const NESTING = new Set(["table_io"]);
  const classes = Object.entries(inp.wait_classes || {}).filter(([k]) => !NESTING.has(k)).sort((a, b) => b[1] - a[1]);
  const rowCalls = (inp.wait_classes || {}).table_io;
  const tot = classes.reduce((a, [, v]) => a + v, 0) || 1;
  const segs = stackBar(classes, ([k]) => WAIT_CLASS_NAMES[k] || k, ([, v]) => v, ([k, v]) => `${WAIT_CLASS_NAMES[k] || k}: ${num(v)} s (${(100 * v / tot).toFixed(1)}%)`);
  const bar = classes.length ? `<div class="waitbar">${segs.map(([n, v, c, t]) => `<span style="width:${(100 * v / tot).toFixed(2)}%;background:${c}" data-tip="${esc(t)}" tabindex="0"></span>`).join("")}</div>
    <div class="row" style="gap:4px 14px;font-size:12px;margin:4px 0 8px">${segs.map(([n, v, c]) => `<span><i class="dot" style="background:${c}"></i>${esc(n)} ${(100 * v / tot).toFixed(0)}%</span>`).join("")}</div>` : "";
  const rowNote = rowCalls ? `<p class="muted" style="font-size:12px;margin:0 0 8px">Row access through the storage engine (table I/O): ${num(rowCalls)} s${dur ? ` (${num(rowCalls / dur)} threads busy)` : ""} - CPU work plus the waits above, so not added to them.</p>` : "";
  return `<h3>Where threads waited${info("performance_schema wait events during the run window. Threads waiting = total wait time / run duration; it includes background threads (redo writes are mostly the log writer, data file writes the page cleaners), so it is not all time connections waited. Table I/O (row calls into the storage engine) contains the file I/O, mutex and lock waits that happen inside it and its own CPU work: it is shown separately and not counted in the percentages." +
    (cov ? ` Instrumentation: ${cov.enabled} of ${cov.instruments} wait instruments timed${cov.sync_enabled ? "" : "; mutex/rwlock waits are not timed (enable with tuning-agent diag waits on)"}.` : ""))}</h3>
    <div class="card">${bar}${rowNote}<div class="tablewrap"><table><thead><tr><th>Event</th><th>Class</th><th class="num">% of wait time</th><th class="num">threads waiting</th><th class="num">count</th><th class="num">avg µs</th></tr></thead><tbody>` +
    inp.top_waits.slice(0, 15).map(w => `<tr><td class="mono" style="word-break:break-all">${esc(w.event.replace(/^wait\//, ""))}</td><td>${esc(NESTING.has(w.class) ? "row access (contains the waits)" : (WAIT_CLASS_NAMES[w.class] || w.class || ""))}</td><td class="num">${NESTING.has(w.class) ? "–" : (w.time_s != null ? (100 * w.time_s / tot).toFixed(1) : (w.pct != null ? w.pct : "–"))}</td><td class="num">${dur ? num(w.time_s / dur) : "–"}</td><td class="num">${num(w.count)}</td><td class="num">${w.avg_us != null ? num(w.avg_us) : num(w.time_s * 1e6 / Math.max(w.count, 1))}</td></tr>`).join("") + "</tbody></table></div></div>";
}
function unsafeMap(tradeoffs) {  // variable -> what it gives up, for the settings a run's trade-offs name
  const m = {};
  (tradeoffs || []).forEach(t => { if (t.variable && t.variable !== "memory") m[t.variable] = `Not production-safe (${t.category}): ${t.lost}`; });
  return m;
}
function cnfBlock(managed, unsafe) {
  // MySQL option file only: can be copied into my.cnf as is; trade-off settings in red with what they give up
  const my = Object.entries(managed || {}).filter(([k]) => !/^(os|rg|client):/.test(k));
  const line = (k, v) => esc(k) + (v === "" ? "" : " = " + esc(v));
  return my.length ? `[mysqld]\n${my.map(([k, v]) => unsafe && unsafe[k] ? `<span class="unsafe" data-tip="${esc(unsafe[k])}" tabindex="0">${line(k, v)}</span>` : line(k, v)).join("\n")}` : "";
}
// resource groups as SQL (thread assignments do not survive a restart: run after every start)
function rgSql(rg) {
  const out = [];
  const val = x => x && typeof x === "object" ? x.value : x;
  const bg = val(rg["rg:innodb_bg_cpus"]), pu = val(rg["rg:purge"]);
  if (bg && !/^off$/i.test(bg)) out.push(`CREATE RESOURCE GROUP ta_bg TYPE = SYSTEM VCPU = ${bg};  -- once`,
    "-- after every start: InnoDB background threads" + (pu && !/^off$/i.test(pu) ? " (purge excluded)" : ""),
    "SELECT GROUP_CONCAT(THREAD_ID) FROM performance_schema.threads WHERE TYPE = 'BACKGROUND' AND NAME LIKE 'thread/innodb/%'"
      + (pu && !/^off$/i.test(pu) ? " AND NAME NOT IN ('thread/innodb/srv_purge_thread', 'thread/innodb/srv_worker_thread')" : "") + ";",
    "SET RESOURCE GROUP ta_bg FOR <the thread ids above>;");
  if (pu && !/^off$/i.test(pu)) {
    const cpus = (pu.match(/cpus=([^,]+)/) || [])[1], prio = (pu.match(/prio=(-?\d+)/) || [])[1];
    out.push(`CREATE RESOURCE GROUP ta_purge TYPE = SYSTEM${cpus ? ` VCPU = ${cpus}` : ""}${prio ? ` THREAD_PRIORITY = ${prio}` : ""};  -- once`,
      "-- after every start: purge coordinator and workers",
      "SELECT GROUP_CONCAT(THREAD_ID) FROM performance_schema.threads WHERE NAME IN ('thread/innodb/srv_purge_thread', 'thread/innodb/srv_worker_thread');",
      "SET RESOURCE GROUP ta_purge FOR <the thread ids above>;");
  }
  return out.join("\n");
}
function otherChanges(managed) {
  const e = Object.entries(managed || {});
  return [
    ["Operating system (database host)", e.filter(([k]) => k.startsWith("os:")), k => k.slice(3)],
    ["Applied after MySQL starts (resource groups)", e.filter(([k]) => k.startsWith("rg:")), k => k.slice(3)],
    ["Operating system (client hosts)", e.filter(([k]) => k.startsWith("client:")), k => k.replace(/^client:os:/, "")],
  ].filter(g => g[1].length);
}
function reqTable(reqs) {
  // benchmark prerequisites: settings the benchmark needs to run at all (not tuning)
  return `<table class="kvt"><tbody>${reqs.map(q => `<tr><td class="mono">${esc(q.variable)}</td><td class="mono">${q.raised ? `${esc(q.server)} → ` : ""}${esc(q.required)}${/^\d+$/.test(q.required) ? '<span class="muted"> min</span>' : ""}</td><td>${q.error ? `<span class="badge warn" data-tip="${esc(q.error)}" tabindex="0">not set</span>` : q.raised ? '<span class="muted">raised before the run</span>' : '<span class="muted">already met</span>'}</td></tr>`).join("")}</tbody></table>`;
}
const REQ_TIP = "Settings the benchmark needs just to run (for example max_connections for 1024 client threads, or max_prepared_stmt_count for 64 threads × 32 tables of prepared statements). Declared in the benchmark profile, applied before every run including the server-defaults run (numbers are minimums), and never changed by the tuner, so they are not part of the tuning result.";
function configBlocks(managed, reqs, unsafe) {
  // side by side: the copyable option file, and everything that is not MySQL configuration
  const my = cnfBlock(managed, unsafe), other = otherChanges(managed);
  reqs = reqs || [];
  const right = other.map(([h, rows, name]) => `<h4 style="margin:0 0 4px">${h}</h4><table class="kvt"><tbody>${rows.map(([k, v]) => `<tr${unsafe && unsafe[k] ? ` class="unsafe" data-tip="${esc(unsafe[k])}" tabindex="0"` : ""}><td class="mono">${esc(name(k))}</td><td class="mono">${esc(v)}</td></tr>`).join("")}</tbody></table>`).join('<div style="height:10px"></div>');
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
  const atDef = new Set(c.at_default || []);
  const rows = KEY_VARS.filter(k => k in c.config && !/^(os|rg):/.test(k)).map(k => {
    const m = meta[k] || {}, def = isDefault(m) || atDef.has(k);
    // a compiled-default source with another value: the server sized it itself (sentinel defaults like 0 or 4294967295 mean "auto")
    const auto = isDefault(m) && autoSized(k, c.config[k], m.default);
    const dv = k === "log_bin" ? "" : m.default != null ? (auto ? `<span data-tip="${esc("Compiled default " + m.default + ": sized by the server at startup")}" tabindex="0">auto</span>` : esc(val(k, m.default))) : "";
    return `<tr><td class="mono">${esc(k)}</td><td class="mono ${def && !auto ? "" : "diff-new"}">${esc(val(k, c.config[k]))}</td><td class="mono muted">${dv}</td><td>${auto ? '<span class="muted">auto-sized by the server</span>' : isDefault(m) ? '<span class="muted">compiled default</span>' : def ? `<span class="muted" data-tip="${esc("Same value as the compiled default; also written in " + sourceText(m))}" tabindex="0">compiled default (pinned)</span>` : esc(sourceText(m))}</td></tr>`;
  }).join("");
  const nonDef = Object.entries(meta).filter(([k, m]) => !isDefault(m) && !atDef.has(k) && !KEY_VARS.includes(k)).length;
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
    const res = e.results && e.results.arms ? `<div class="tablewrap" style="margin-top:6px"><table><thead><tr><th>Value</th><th class="num">Runs</th><th class="num">${esc(goalUnit(sess.goal, sess))}</th><th class="num">vs parent</th><th>Result</th></tr></thead><tbody>${e.results.arms.map(a =>
      `<tr><td class="mono">${esc(a.arm)}${e.results.kept === a.arm ? ' <span class="badge good">kept</span>' : ""}</td><td class="num">${a.runs || ""}</td><td class="num">${num(a.goal_mean)}</td>
       <td class="num ${a.verdict === "better" ? "up" : a.verdict === "worse" ? "down" : ""}">${a.delta_pct != null ? pct(a.delta_pct) : ""}</td><td>${esc(a.verdict || a.status)}${a.reason ? info(a.reason) : ""}</td></tr>`).join("")}</tbody></table></div>` : "";
    return `<div class="rec"><div class="h"><b>#${e.id}</b> ${spec}${e.repeats > 1 ? ` · ${e.repeats} runs each` : ""} ${statusBadge(e.status === "running" ? "measuring" : e.status, e.message)}
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

const NO_SWAP_TIP = "No swap on the database host during this run. Memory pressure ends in an OOM kill of mysqld instead of slower pages, so memory settings are kept inside RAM and the buffer pool range the tuner can explore is limited. Best practice (applied by the baseline when OS tuning is allowed): a swap file of about 1x RAM - unused swap costs nothing.";
function noSwapBadge(sm) { return sm && sm.swap_total_mb === 0 ? ` <span class="badge warn" data-tip="${esc(NO_SWAP_TIP)}" tabindex="0">no swap</span>` : ""; }
// provenance: who chose the next step, and what each recommendation rests on (built-in rules, learnings, AI)
function decisionLine(d) {
  if (!d) return "";
  const L = d.learning || {};
  const learned = (L.experience_runs || 0) + (L.hypothesis_outcomes || 0) + (L.learned_parameters || 0) + (L.insights || 0);
  const tip = `Learned data loaded for this decision: ${L.experience_runs || 0} comparable earlier runs of ${L.experience_ideas || 0} ideas (all accounts), ${L.hypothesis_outcomes || 0} hypothesis outcomes, ${L.learned_parameters || 0} learned parameter impacts, ${L.insights || 0} insights.`
    + (d.engine_choice ? ` The rules would have chosen ${d.engine_choice}.` : "") + (d.planner_rejected ? ` The planner's choice was not used: ${d.planner_rejected}.` : "");
  const by = String(d.by || "rules");
  return ` · decided by <b>${by.startsWith("planner:") ? "AI planner (" + esc(by.slice(8).replace("+reviewer:", ", reviewer ")) + ")" : "rules"}</b>${learned ? " + learnings" : ""}${info(tip)}`;
}
function basisLine(it) {
  const b = it.basis;
  if (!b) return "";
  const part = p => `${p.what}${p.score ? ` (${p.score > 0 ? "+" : ""}${p.score})` : ""}`;
  const origin = b.origin === "llm" ? '<span class="badge info">idea: AI</span>' : b.origin === "user" ? '<span class="badge info">idea: user</span>' : `<span class="badge plain">idea: rule ${esc(b.rule || "")}</span>`;
  const bi = (b.built_in || []).length ? ` <span class="badge plain" data-tip="${esc("Built-in (shipped rules and catalog): " + b.built_in.map(part).join("; "))}" tabindex="0">built-in ${b.built_in.length}</span>` : "";
  const le = (b.learned || []).length ? ` <span class="badge good" data-tip="${esc("Learned (earlier sessions, learning database): " + b.learned.map(part).join("; "))}" tabindex="0">learned ${b.learned.length}</span>` : "";
  const pl = it.planner ? ` <span class="badge info" data-tip="${esc("Chosen by the AI planner " + (it.planner.model || "") + (it.planner.prediction ? ". Predicted: " + it.planner.prediction : ""))}" tabindex="0">chosen by AI</span>` : "";
  return `<div class="reason" style="margin-top:3px">Based on: ${origin}${bi}${le}${pl}${it.known_neutral ? ` <span class="badge plain" data-tip="${esc(`No win in ${it.known_neutral.n} comparable earlier runs: ranked behind untried ideas.`)}" tabindex="0">known neutral here</span>` : ""}</div>`;
}

const dots3 = n => { n = Math.max(0, Math.min(3, Math.round(+n || 0))); return "●".repeat(n) + "○".repeat(3 - n); };
// the rationale often repeats the hypothesis ("Why:") word for word
const dupOf = (a, b) => { const n = x => String(x || "").toLowerCase().replace(/\W+/g, " ").trim(); return !!a && !!b && (n(b).includes(n(a)) || n(a).startsWith(n(b))); };
function renderRecs(el, rec, runs, feedback, thisRun) {
  if (!rec) { el.innerHTML = '<span class="muted">No recommendations yet: run <code>tuning-agent recommend</code> after a measured run.</span>'; return; }
  const run = runs && runs.find(r => r.id === rec.run_id);
  let h = `<div class="muted" style="margin-bottom:6px">Generated ${fmtTime(rec.created)}${thisRun && rec.run_id === thisRun ? " from this run" : run ? ` from run #${run.seq} <a href="#/run/${run.id}">${esc(run.label)}</a>` : rec.run_id ? ` from run <a href="#/run/${rec.run_id}">${rec.run_id}</a>` : " without a measured run"} · goal ${esc(rec.goal)} · policy ${esc(rec.policy)}${rec.next ? ` · suggested next: <code>${esc(rec.next)}</code>` : ""}${decisionLine(rec.decision)}</div>`;
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
  h += adviceHtml(rec);
  h += section("Production-safe", safe) + section("Trade-offs (benchmark only)", trade, "These give up durability, replication, observability or semantics. Results with them are not production-equivalent; they are reported separately.");
  el.innerHTML = h;
  wireAdvice(el, feedback);
  return;
  function recItem(it) {
    return `<div class="rec"><div class="h"><b>${it.rank}. ${esc(it.title)}</b><span class="id">${esc(it.id)}</span>${it.source === "llm" ? `<span class="badge info">LLM</span>` : ""}${it.kind === "advice" ? '<span class="badge plain">advice only</span>' : it.kind === "revert" ? '<span class="badge warn">revert</span>' : ""}
     ${it.restart ? '<span class="badge plain">restart</span>' : ""}${it.id === rec.next ? '<span class="badge info">next</span>' : ""}
     <span class="muted" style="font-size:12px">impact <span class="dots" aria-label="${it.impact} of 3">${dots3(it.impact)}</span> · confidence <span class="dots" aria-label="${it.confidence} of 3">${dots3(it.confidence)}</span> · risk <span class="${["durability", "compat", "observability"].includes(it.risk) ? "unsafe" : ""}">${esc(it.risk || "none")}</span></span></div>
     ${it.changes.length ? `<div class="mono" style="margin:4px 0">${it.changes.map(c => `${esc(c.variable)}: <span class="diff-old">${esc(val(c.variable, c.current))}</span> → <span class="diff-new">${c.unset ? "(server default)" : esc(val(c.variable, c.proposed))}</span>`).join("<br>")}</div>` : ""}
     ${it.hypothesis ? `<div class="reason"><b>Why:</b> ${esc(it.hypothesis.text)}${it.hypothesis.lead ? ` <span class="muted">Leads (outside reports and learned insights - they motivate the test, the measurements decide): ${esc(it.hypothesis.lead)}</span>` : ""}${(it.rescore || {}).why ? ` <span class="badge ${it.rescore.delta > 0 ? "good" : "warn"}" data-tip="${esc(it.rescore.why.join("; "))}" tabindex="0">${it.rescore.delta > 0 ? "+" : ""}${it.rescore.delta} from earlier results</span>` : ""}</div>` : ""}
     ${dupOf(it.rationale, it.hypothesis && it.hypothesis.text) && !it.note ? "" : `<div class="reason">${dupOf(it.rationale, it.hypothesis && it.hypothesis.text) ? "" : esc(it.rationale)}${it.note ? " <b>" + esc(it.note) + "</b>" : ""}</div>`}
     ${basisLine(it)}
     ${it.relevance != null || it.experience ? `<div class="reason" style="margin-top:3px">${it.relevance != null ? (it.no_mechanism ? `<span class="badge plain">validation only</span>` : it.relevance ? ((a) => `<span class="badge good wrap"${a.length > 3 ? ` data-tip="${esc("Acts on: " + a.join(", "))}" tabindex="0"` : ""}>acts on ${esc(a.slice(0, 3).join(", "))}${a.length > 3 ? ` +${a.length - 3} more` : ""}</span>`)(it.addresses || []) : `<span class="badge plain">not aimed at the current bottleneck</span>`) + info("Mechanism: " + (it.mechanism || "") + (it.no_mechanism ? " Nothing to act on in this workload: " + it.no_mechanism + "." : " Ranked by how much of the database's time (see Where the time went) this change can act on.")) : ""}
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
      // a percentage metric changes by percentage points, not by a percentage of itself
      const pp = /^%/.test(unit), dv = pp ? v - pv : ch;
      const txt = Math.abs(dv) < 0.05 ? (pp ? "±0 pp" : "±0%") : `${dv > 0 ? "+" : "−"}${Math.abs(dv).toFixed(1)}${pp ? " pp" : "%"}`;
      d = `<div class="d ${cls}"${dir ? ` data-tip="Coloured beyond the noise threshold (±${thr}% relative change)" tabindex="0"` : ""}>${txt} vs prev</div>`;
    }
    const mib = unit === "MiB", shown = mib ? bytes(v * 1048576) : num(v);
    return `<div class="tile"><div class="k">${k}</div><div class="v">${shown}<span style="font-size:13px;color:var(--ink-2)"> ${mib ? "" : unit}</span></div>${d}</div>`;
  };
  const lowerG = s.goal === "latency", ref = (r.start || {}).value || cal.goal_mean;
  const vsStart = r.goal_value != null && ref ? (lowerG ? (ref - r.goal_value) / ref : (r.goal_value - ref) / ref) * 100 : null;
  setTitle(`Run #${r.seq} · ${r.label} · #${s.id}`);
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${s.env_id}">${esc(r.environment.name)}</a> / <a href="#/session/${s.id}">#${s.id} ${esc(s.name)}</a> /</div>
  <div class="row"><h1>Run #${r.seq} · ${esc(r.label)}</h1>${ME && !ME.share ? starBtn(r.id, r.starred) : ""}${statusBadge(r.status)}${verdictBadge(r.verdict)}${tradeBadge(r.tradeoffs_added || [])}${noSwapBadge(sm)}${fbMark(r.feedback)}</div>
  <p class="sub">${fmtTime(r.started)} – ${r.ended ? fmtTime(r.ended) : "measuring…"} (${dur(sm.duration_s)}) · goal ${goalName(s.goal)} = <b>${num(r.goal_value)} ${goalUnit(s.goal, s)}</b>${r.goal_source ? ` <span class="muted">(${esc(r.goal_source)})</span>` : ""}
   ${r.delta_pct != null ? ` · <span class="${deltaCls(r.delta_pct)}">${gainTxt(r.delta_pct, lowerG)}</span> vs previous` : ""}${vsStart != null && r.seq > 1 ? `, <span class="${deltaCls(vsStart)}">${gainTxt(vsStart, lowerG)}</span> vs start${info(`Calibrated starting configuration: ${num(ref)} ${goalUnit(s.goal, s)} (${(r.start ? r.start.basis === "calibration" : cal.goal_mean) ? "calibration mean" : "its baseline run"}).${r.delta_vs_first_pct != null ? ` vs server defaults (run #1): ${gainTxt(r.delta_vs_first_pct, lowerG)}.` : ""}`)}` : ""}</p>
  <label class="row" style="margin:-6px 0 12px;gap:6px;width:auto;color:var(--ink-2)">Compare with <select id="vs" style="width:auto;max-width:100%"><option value="">previous run</option>${r.runs.filter(x => x.id !== r.id).map(x => `<option value="${x.id}" ${String(x.id) === vs ? "selected" : ""}>#${x.seq} ${esc(x.label)}</option>`).join("")}</select>${prev ? `<span class="muted" style="font-size:12.5px">run #${prev.seq} ${esc(prev.label)}</span>` : ""}</label>
  ${r.status === "aborted" ? `<div class="note" style="margin-bottom:12px"><span class="badge warn">aborted</span> ${esc(r.abort_reason || "no reason recorded")}. Its changes were handed to the next run.</div>` : ""}
  ${r.notes ? `<div class="note" style="margin-bottom:12px">${esc(r.notes)}</div>` : ""}
  <div class="tiles">${tile("TPS", sm.tps, "/s", ps.tps, 1)}${tile("QPS", sm.qps, "/s", ps.qps, 1)}${tile("Avg statement latency", sm.avg_latency_ms, "ms", ps.avg_latency_ms, -1)}${tile("p95 statement latency", sm.p95_latency_ms, "ms", ps.p95_latency_ms, -1)}
   ${tile("CPU busy (host)", sm.cpu_busy, "%", ps.cpu_busy, 0)}${tile("mysqld CPU / trx", sm.mysqld_cpu_ms_per_trx, "ms", ps.mysqld_cpu_ms_per_trx, -1, "mysqld_cpu_ms_per_trx")}${tile("Disk IOs / trx", sm.disk_ios_per_trx, "", ps.disk_ios_per_trx, -1, "disk_ios_per_trx")}${tile("TPS variation", sm.tps_cv_pct, "% CV", ps.tps_cv_pct, -1, "x")}</div>
  <details style="margin-top:6px"><summary>More metrics</summary><div class="tiles">${(sm.cpu_steal || 0) >= 0.5 || (sm.cpu_steal_max || 0) >= 5 ? tile("CPU steal" + info(`CPU time the hypervisor gave to other virtual machines: average over the run, peak ${num(sm.cpu_steal_max)}% in a 10 s sample${sm.client_cpu_steal != null ? `, client ${num(sm.client_cpu_steal)}%` : ""}. Its possible effect on the result (about the average steal, less when the host is not CPU-bound) is flagged from a quarter of the session's noise threshold (at least 0.25%) or a 5% peak, and the run is excluded from half the threshold (at least 0.5%). Normal on these hosts: below 0.05% average.`), sm.cpu_steal, "%", ps.cpu_steal, 0) : ""}${tile("mysqld CPU", sm.mysqld_cpu_pct, "% of host", ps.mysqld_cpu_pct, 0)}${tile("IO wait", sm.cpu_iowait, "%", ps.cpu_iowait, -1, "x")}${tile("BP hit ratio", sm.bp_hit_pct, "%", ps.bp_hit_pct, 1)}${tile("Disk IOPS", (sm.disk_r_iops || 0) + (sm.disk_w_iops || 0), "/s", ps.disk_r_iops != null ? (ps.disk_r_iops || 0) + (ps.disk_w_iops || 0) : null, 0)}
   ${tile("CPU pressure", sm.mysqld_psi_cpu_some != null ? sm.mysqld_psi_cpu_some : sm.psi_cpu_some, "% stalled", ps.mysqld_psi_cpu_some != null ? ps.mysqld_psi_cpu_some : ps.psi_cpu_some, -1, "x")}${tile("I/O pressure", sm.mysqld_psi_io_some != null ? sm.mysqld_psi_io_some : sm.psi_io_some, "% stalled", ps.mysqld_psi_io_some != null ? ps.mysqld_psi_io_some : ps.psi_io_some, -1, "x")}${tile("Memory pressure", sm.psi_memory_some, "% stalled", ps.psi_memory_some, -1, "x")}${tile("Sched delay" + info(`Average time database threads waited in the CPU run queue each time they were scheduled (from /proc/<tid>/schedstat). It adds directly to query latency, mostly at the tail. Database threads waited ${num(sm.dbproc_runq_wait_pct)}% of their runnable time (${num(sm.dbproc_runq_wait_threads)} threads waiting on average); worst 10 s interval ${num(sm.dbproc_sched_delay_us_max)} µs; ${num(sm.dbproc_nvcsw_per_s)} preemptions/s. All tasks on the host: ${num(sm.host_sched_delay_us)} µs.`), sm.dbproc_sched_delay_us, "µs", ps.dbproc_sched_delay_us, -1, "x")}${tile("DB memory (PSS)" + info(`Proportional set size of the database process(es): shared pages are split between the processes that map them, so this is their real share of RAM (RSS counts shared pages once per process). At the end of the run: RSS ${num(sm.dbproc_rss_mb)} MiB · VSZ ${num(sm.dbproc_vsz_mb)} MiB · swap PSS ${num(sm.dbproc_swap_pss_mb)} MiB · ${sm.dbproc_procs || 1} process(es). Peak PSS ${num(sm.dbproc_pss_mb_max)} MiB.`), sm.dbproc_pss_mb, "MiB", ps.dbproc_pss_mb, 0)}
   ${tile("Redo written", sm.redo_mbs, "MiB/s", ps.redo_mbs, 0)}${tile("Ckpt age max", sm.checkpoint_age_pct_max, "%", ps.checkpoint_age_pct_max, -1, "x")}${tile("Threads running", sm.threads_running_avg, "avg", ps.threads_running_avg, 0)}</div></details>
  ${Object.keys(r.external || {}).length ? `<h3>Benchmark-reported values</h3><div class="row">${Object.entries(r.external).map(([k, v]) => `<span class="badge plain"${typeof v === "number" ? ` data-tip="${esc(k + " = " + v)}" tabindex="0"` : ""}>${esc(k)} = ${typeof v === "number" ? num(v) : esc(v)}</span>`).join("")}</div>` : ""}
  <div class="card" id="fbsec" style="margin:12px 0"></div>
  <div class="tabs" id="tabs"></div><div id="tab"></div>`;
  wireStars($view);
  const fbRefresh = async () => { const r2 = await api(`/api/runs/${id}`); r.feedback = r2.feedback; feedbackSection(document.getElementById("fbsec"), r, (r.feedback || []).filter(f => f.about === "run"), fbRefresh); };
  feedbackSection(document.getElementById("fbsec"), r, (r.feedback || []).filter(f => f.about === "run"), fbRefresh);
  document.getElementById("vs").onchange = e => { location.hash = "#/run/" + id + (e.target.value ? "?vs=" + e.target.value : ""); };
  const tabs = [["changes", "Configuration"], ["graphs", "Graphs"], ["inputs", "Diagnosis & inputs"], ...((r.inputs || {}).cpu_profile ? [["cpu", "CPU profile"]] : []), ["recs", "Recommendations"], ["config", "Full configuration"]];
  const tabsEl = document.getElementById("tabs"), tabEl = document.getElementById("tab");
  const show = async name => {
    tabsEl.querySelectorAll("button").forEach(b => { const on = b.dataset.t === name; b.classList.toggle("on", on); b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; });
    clearTimers();
    if (name === "changes") { renderDiff(tabEl, r); const ks = document.createElement("div"); tabEl.appendChild(ks); renderKeySettings(ks, id); }
    else if (name === "graphs") {
      tabEl.innerHTML = `<p class="muted"><span class="swatch warm"></span>warm-up ${(r.summary || {}).startup_excluded_s ? `<span class="swatch su"></span><span data-tip="${esc(`The benchmark's own start-up at the beginning of the measured window (a new benchmark process connecting its threads): ${r.summary.startup_excluded_s} s, left out of the warm-up, steady-state and noise analyses. The benchmark's own result still covers it.`)}" tabindex="0">benchmark start-up</span> ` : ""}<span class="swatch win"></span>measurement window${info("The same charts as the environment page, for this run: the warm-up, the measurement window and 60 s around them. The benchmark runs throughout: throughput should be at its steady level for the whole measurement window - a drop inside it means the window measured something other than the workload.")}</p><div id="rc"></div>`;
      const loadRun = async () => {
        const m = await api(`/api/runs/${id}/metrics?points=300`);
        const w1 = (m.window || [])[1] || Date.now() / 1000;
        try { m._waits = await api(`/api/environments/${r.environment.id}/waits?start=${m.window[0]}&end=${w1}`); } catch (e) { }
        const draw = () => { const rc = document.getElementById("rc"); if (rc) renderLive(rc, m, m.cpus || 1, r.environment.name, null, m.window); };
        draw(); widthFns = [draw];
      };
      await loadRun();
      if (r.status === "measuring") timers.push(setInterval(loadRun, 15000));
    }
    else if (name === "inputs") renderInputs(tabEl, r);
    else if (name === "cpu") renderCpuProfile(tabEl, r.inputs.cpu_profile);
    else if (name === "recs") { tabEl.innerHTML = ""; if (!r.recommendations.length) tabEl.innerHTML = '<p class="muted">No recommendation was generated from this run.</p>'; r.recommendations.forEach(rec => { const c = document.createElement("div"); c.className = "card"; c.style.marginBottom = "12px"; tabEl.appendChild(c); renderRecs(c, rec, r.runs, r.feedback, r.id); }); }
    else if (name === "config") {
      const c = await api(`/api/runs/${id}/config`);
      const managed = c.managed || {}, meta = c.meta || {};
      const hasMeta = Object.keys(meta).length > 0, atDefC = new Set(c.at_default || []);
      tabEl.innerHTML = `<div class="row"><input id="flt" placeholder="filter variables" style="max-width:300px">${hasMeta ? '<label style="margin:0;display:flex;gap:6px;align-items:center;width:auto"><input type="checkbox" id="nondef" style="width:auto"> only non-default</label>' : ""}<span class="muted">${Object.keys(c.config).length} variables at measurement start; <b>bold</b> = managed by the agent</span></div>
        <div class="card tablewrap" style="margin-top:10px"><table><thead><tr><th>Variable</th><th>Value</th>${hasMeta ? "<th>Compiled default</th><th>Source</th>" : ""}</tr></thead><tbody id="cfg"></tbody></table></div>`;
      const draw = () => {
        const q = document.getElementById("flt").value.toLowerCase(), nd = hasMeta && document.getElementById("nondef").checked;
        document.getElementById("cfg").innerHTML = Object.entries(c.config).filter(([k]) => k.includes(q) && (!nd || !(isDefault(meta[k]) || atDefC.has(k)))).map(([k, v]) => {
          const m = meta[k] || {};
          return `<tr><td class="mono">${managed[k] != null ? "<b>" + esc(k) + "</b>" : esc(k)}</td><td class="mono" style="word-break:break-all">${esc(val(k, v))}</td>${hasMeta ? `<td class="mono muted" style="word-break:break-all">${m.default != null ? esc(val(k, m.default)) : ""}</td><td>${isDefault(m) ? '<span class="muted">compiled default</span>' : atDefC.has(k) ? `<span class="muted" data-tip="${esc("Same value as the compiled default; also written in " + sourceText(m))}" tabindex="0">compiled default (pinned)</span>` : esc(sourceText(m))}</td>` : ""}</tr>`;
        }).join("");
      };
      document.getElementById("flt").oninput = draw; if (hasMeta) document.getElementById("nondef").onchange = draw; draw();
    }
    history.replaceState(null, "", "#/run/" + id + "/" + name + (vs ? "?vs=" + vs : ""));
  };
  tabsEl.setAttribute("role", "tablist"); tabEl.setAttribute("role", "tabpanel");
  tabs.forEach(([k, label]) => { const b = document.createElement("button"); b.textContent = label; b.dataset.t = k; b.setAttribute("role", "tab"); b.onclick = () => show(k); tabsEl.appendChild(b); });
  tabsEl.onkeydown = ev => {  // arrow keys move between tabs
    const bs = [...tabsEl.querySelectorAll("button")], i = bs.indexOf(document.activeElement);
    if (i < 0 || !["ArrowRight", "ArrowLeft", "Home", "End"].includes(ev.key)) return;
    ev.preventDefault();
    const j = ev.key === "Home" ? 0 : ev.key === "End" ? bs.length - 1 : (i + (ev.key === "ArrowRight" ? 1 : -1) + bs.length) % bs.length;
    bs[j].focus(); show(bs[j].dataset.t);
  };
  show(tabs.some(t => t[0] === tab) ? tab : "changes");
}

function queryImpact(qi) {
  if (!qi || !qi.rows || !qi.rows.length) return "";
  const row = q => `<tr><td class="mono" style="max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" data-tip="${esc(q.query)}" tabindex="0">${esc(q.query)}</td>
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
    "improved-other-mechanism": ["info", "improved, but not via the targeted time"], "improved-unverified": ["info", "improved; mechanism not measured"],
    efficiency: ["plain", "less work, goal unchanged"], "not-confirmed": ["warn", "did not hold on its repeat"], done: ["plain", ""] };
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
// everything this run runs with that differs from the session's baseline: its own change plus what it inherited
function baselineDiffHtml(r) {
  const vb = r.vs_baseline;
  if (!vb || !(vb.settings || []).length) return vb ? `<p class="muted">Same agent-managed configuration as the baseline (run #${vb.baseline.seq}).</p>` : "";
  const unsafe = unsafeMap(r.tradeoffs);
  const rows = vb.settings.map(x => `<tr${unsafe[x.variable] ? ` class="unsafe" data-tip="${esc(unsafe[x.variable])}" tabindex="0"` : ""}><td class="mono">${x.this_run ? "<b>" + esc(x.variable) + "</b>" : esc(x.variable)}</td>`
    + `<td class="mono muted">${x.baseline == null ? "–" : esc(val(x.variable, x.baseline))}</td><td class="mono"><b>${x.value == null ? "(default)" : esc(val(x.variable, x.value))}</b></td>`
    + `<td>${x.this_run ? '<span class="badge info">this run</span>' : x.set_in ? `<a href="#/run/${x.set_in.id}">#${x.set_in.seq} ${esc(x.set_in.label)}</a>` : '<span class="muted">outside the experiments</span>'}</td></tr>`).join("");
  const own = vb.settings.filter(x => x.this_run).length;
  return `<h3>Differs from the baseline (run #${vb.baseline.seq})${info("Every agent-managed setting (MySQL and OS) this run measured with that differs from the session's baseline: this run's own change, marked, and the changes it inherited from earlier experiments, with the run that made each. Red = not production-safe.")} <span class="muted" style="font-weight:400;font-size:13px">${vb.settings.length} setting${vb.settings.length === 1 ? "" : "s"}${own ? `, ${own} changed in this run` : ""}</span></h3>
   <div class="card tablewrap"><table><thead><tr><th>Setting</th><th>Baseline</th><th>This run</th><th>Changed in</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderDiff(el, r) {
  const prev = r.compared_with;
  let h = renderEvaluation(r);
  h += baselineDiffHtml(r);
  if (r.seq === 1) h += `<p class="muted">Starting configuration of the session${info("The first run measures the configuration the session started with. Key settings below show compiled defaults and the source of each value." + (prev ? ` Differences from the last run before this session (run #${prev.seq}) are listed for reference.` : ""))}</p>`;
  if (!prev) {}
  else if (!r.diff.length && r.seq !== 1) h += `<p>No configuration differences with run #${prev.seq} <b>${esc(prev.label)}</b>.</p>`;
  else if (r.diff.length) h += `<h3>Changed since run #${prev.seq} ${esc(prev.label)}${info("Source and reasoning come from the agent. “outside the agent” means the value changed some other way: manual edit, server default after restart, automatic sizing.")}</h3>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Run #${prev.seq}</th><th>This run</th><th>Source</th><th>Reason</th></tr></thead><tbody>${r.diff.map(diffRow).join("")}</tbody></table></div>`;
  const managed = r.managed || {};
  const reqs = ((r.inputs || {}).bench || {}).requirements || [];
  const pinned = (r.at_default || []).filter(k => k in managed);
  const changedManaged = Object.fromEntries(Object.entries(managed).filter(([k]) => !pinned.includes(k)));
  if (Object.keys(managed).length) h += `<h3>Agent-applied configuration during this run${info("The settings the tuning-agent enforced while this run was measured that differ from the server's compiled defaults: its MySQL option file plus operating-system settings. Everything else was the server default or came from other option files (see Key settings).")}</h3>${configBlocks(changedManaged, reqs, unsafeMap(r.tradeoffs))}`
    + (pinned.length ? `<p class="muted" style="font-size:12.5px;margin:6px 0 0">Also written at their compiled default (no change on this server): ${pinned.map(k => `<code>${esc(k)}</code>`).join(" ")}${info("The agent's option file pins these at the value this server version uses anyway. Pinning keeps the configuration the same when the same file is used on another release series, where some defaults differ (e.g. MySQL 8.0 vs 8.4).")}</p>` : "");
  else if (reqs.length) h += `<h3>Benchmark prerequisites${info(REQ_TIP)}</h3><div class="card">${reqTable(reqs)}</div>`;
  el.innerHTML = h;
}

function renderCpuProfile(el, p) {
  if (p.error) { el.innerHTML = `<p class="muted">The CPU profile could not be taken: ${esc(p.error)}</p>`; return; }
  const cats = Object.entries(p.categories || {}).sort((a, b) => b[1] - a[1]);
  const segs = stackBar(cats, ([k]) => k, ([, v]) => v, ([k, v]) => `${k}: ${v}%`);
  el.innerHTML = `<p class="sub">${p.seconds} s at ${p.hz} samples/s per CPU ${p.in_measured_window === false ? "at the end of the warm-up (outside the measured window)" : "during the run"} · ${num(p.db_samples)} database samples${p.call_stacks ? "" : " · leaf functions only (binary without frame pointers)"}${info("perf-event sampling (the kernel mechanism eBPF profilers use) on every CPU at a low frequency for a slice of the run; symbols are resolved after the measurement at low priority. Taken at the end of the warm-up, so the measured results never include the profiler.")}</p>
  <h3>Where the database spent CPU${info("Each sample is assigned to the first matching category from the innermost frame outwards. Categories map to tuning mechanisms, e.g. spin / mutex wait → spin loops or a thread pool, memory allocator → jemalloc, performance_schema → instrumentation cost.")}</h3>
  <div class="card"><div class="waitbar">${segs.map(([k, v, c, t]) => `<span style="width:${v}%;background:${c}" data-tip="${esc(t)}" tabindex="0"></span>`).join("")}</div>
   <div class="row" style="gap:4px 14px;font-size:12px;margin-top:6px">${segs.map(([k, v, c]) => `<span><i class="dot" style="background:${c}"></i>${esc(k)} ${+(+v).toFixed(1)}%</span>`).join("")}</div>
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
    h += `<h3>Benchmark: ${esc(b.name)}</h3><div class="kv">${(b.observed || []).map(o => `<div>Tool as it ran</div><div class="mono">${esc(o.argv.map(shq).join(" "))}${Object.keys(o.env || {}).length ? " " + esc(Object.entries(o.env).map(([k, v]) => k + "=" + v).join(" ")) : ""}</div>`).join("")}<div>Run command</div><div class="mono">${esc(maskPw(b.command || b.run))}</div>${b.reset ? `<div>Reset</div><div class="mono">${esc(maskPw(b.reset))}</div>` : ""}<div>Warm-up / measured</div><div>${esc(b.warmup)} s / ${b.duration ? esc(b.duration) + " s" : "until finished"}</div><div>Exit code</div><div>${esc(b.exit_code)}</div>${(b.requirements || []).length ? `<div>Prerequisites${info(REQ_TIP)}</div><div class="mono">${b.requirements.map(q => esc(q.variable) + " ≥ " + esc(q.required)).join(" · ")}</div>` : ""}
      ${Object.entries(b.metrics || {}).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono"><b${typeof v === "number" ? ` data-tip="${esc(v)}" tabindex="0"` : ""}>${typeof v === "number" ? num(v) : esc(v)}</b></div>`).join("")}</div><details><summary>Benchmark output (tail)</summary><pre>${esc(b.log_tail || "")}</pre></details>`;
  }
  const tbk = inp.time_breakdown || [];
  if (tbk.length) {
    const q = tbk[0].cpu_queue_share_pct;
    h += `<h3>Where the time went${info("Average number of database threads in each state during the measured window, largest first: what keeps the workload from going faster. On-CPU time is split by the CPU profile when one was taken (otherwise one CPU component); time spent runnable but waiting for a CPU is added to the CPU components in proportion, because doing less CPU work shortens that queue too. Waits come from the eBPF off-CPU profile of the connection threads when one was taken (every blocked moment, classified by where the thread blocked, including waits performance_schema does not instrument); otherwise from performance_schema waits and file I/O by operation (global, so background threads such as page cleaners are included) and InnoDB row lock counters. Name resolution comes from eBPF probes over the whole window. Hover a component for its source and the stacks behind it. 'Client / network' = the server waiting for the next statement. Experiments are ranked by how much of this time they can act on.")}</h3>
      <div class="card"><div class="waitbar">${stackBar(tbk, p => p.name, p => p.share_pct, p => `${p.name}: ${p.share_pct}% (${num(p.threads)} threads)`).map(([k, v, c, t]) => `<span style="width:${v}%;background:${c}" data-tip="${esc(t)}" tabindex="0"></span>`).join("")}</div>
      <div class="tablewrap"><table class="kvt" style="margin-top:6px"><tbody>${tbk.slice(0, 10).map((p, i) => {
        const st = ((inp.offcpu || {}).top_stacks || []).filter(x => x.category === p.name).slice(0, 3);
        const tip = "Source: " + (p.source || "?") + (st.length ? ". Where threads blocked: " + st.map(x => `${x.seconds}s in ${(x.user || []).slice(0, 4).join(" < ")}`).join(" | ") : "");
        return `<tr><td><i class="dot" style="background:${catColor(i)}"></i>${esc(p.name)}${info(tip)}</td><td class="num">${p.share_pct}%</td><td class="num muted">${num(p.threads)} thr</td></tr>`; }).join("")}</tbody></table></div>
      ${q ? `<p class="muted" style="margin:6px 0 0;font-size:12px">Of this, ${q}% was runnable threads waiting for a CPU (included in the CPU components).</p>` : ""}</div>`;
  }
  const blk = ((inp.block_latency || {}).by_device_op) || {};
  const bk = Object.keys(blk).sort((a, b) => blk[b].count - blk[a].count).slice(0, 6);
  if (bk.length) {
    const sm = r.summary || {};
    h += `<h3>Storage latency${info("Every block I/O's device service time (eBPF, issue to completion) over the measured window, by device and operation. p99 / p99.9 are upper bounds of log2 buckets (within 2x). Averages from /proc/diskstats are shown per 10 s sample in the charts; the spread of those samples shows latency moving inside the run. Slow = 8 ms or more.")}</h3>
      <div class="card tablewrap"><table><thead><tr><th>Device: operation</th><th class="num">I/Os</th><th class="num">p50 ms</th><th class="num">p99 ms</th><th class="num">p99.9 ms</th><th class="num">max ms</th><th class="num">slow (≥8 ms)</th></tr></thead><tbody>${bk.map(k => { const v = blk[k];
        return `<tr><td>${esc(k)}</td><td class="num">${num(v.count)}</td><td class="num">${num(v.p50_us / 1000)}</td><td class="num">${num(v.p99_us / 1000)}</td><td class="num">${num(v.p999_us / 1000)}</td><td class="num">${num((v.max_us || 0) / 1000)}</td><td class="num">${num(v.slow_10ms)}</td></tr>`; }).join("")}</tbody></table>
      ${sm.disk_r_await_ms_p50 != null || sm.disk_w_await_ms_p50 != null ? `<p class="muted" style="margin:6px 0 0;font-size:12px">10 s samples: reads ${sm.disk_r_await_ms_p50 ?? "-"} ms median / ${sm.disk_r_await_ms_max ?? "-"} ms worst, writes ${sm.disk_w_await_ms_p50 ?? "-"} ms / ${sm.disk_w_await_ms_max ?? "-"} ms${sm.disk_flush_await_ms_p50 != null ? `, flushes ${sm.disk_flush_await_ms_p50} ms / ${sm.disk_flush_await_ms_max} ms` : ""}${sm.tput_samples ? `; throughput below half its median in ${sm.tput_dip_samples} of ${sm.tput_samples} samples` : ""}.</p>` : ""}</div>`;
  }
  const mb = inp.memory_budget;
  if (mb && mb.items) {
    const gb = x => (x / 1073741824).toFixed(2);
    h += `<h3>Memory budget${info("What this configuration needs from the host's memory: global allocations at their size, count-based caches at their count times an estimated cost per entry, every allowed connection's base cost, and 2 x CPU cores connections active at once with all their per-query buffers (not the worst case of every connection at every buffer, which would make settings absurdly small). Plus the OS reserve and a co-located client's reserve. A configuration over the budget can swap or be OOM-killed under load: it is a memory trade-off, never production-safe. mysqld's measured memory (PSS + swap) is shown to check the estimate.")}</h3>
      <div class="card tablewrap"><p style="margin:0 0 6px">${mb.over_budget ? '<span class="badge warn">over budget</span> ' : '<span class="badge good">within budget</span> '}${gb(mb.needed)} GiB needed of ${gb(mb.ram)} GiB RAM (headroom ${gb(mb.headroom)} GiB) · ${mb.active_connections} active of ${mb.max_connections} connections${mb.measured_mysqld_bytes ? ` · mysqld measured ${gb(mb.measured_mysqld_bytes)} GiB vs ${gb(mb.mysqld_bytes)} GiB modelled` : ""}</p>
      <table class="kvt"><tbody>${mb.items.slice().sort((a, b) => b.bytes - a.bytes).map(i => `<tr><td>${esc(i.name)}${info(i.basis)}</td><td class="num">${gb(i.bytes)} GiB</td></tr>`).join("")}
      <tr><td class="muted">OS reserve${mb.client_reserve ? " + client" : ""}</td><td class="num muted">${gb(mb.os_reserve + mb.client_reserve)} GiB</td></tr></tbody></table></div>`;
  }
  const mp = inp.memory_pfs;
  if (mp && mp.total_mb) {
    const rs = r.summary || {};
    const g = x => x == null ? "–" : (x / 1024).toFixed(2);
    const seen = rs.dbproc_pss_mb != null ? (rs.dbproc_pss_mb || 0) + (rs.dbproc_swap_pss_mb || 0) : null;
    h += `<h3>Memory measured by MySQL${info("MySQL's own accounting (performance_schema memory instruments, on by default) at the end of the window, next to what the OS sees for mysqld (PSS + swap). The difference is memory MySQL does not account for: thread stacks, the binary, allocator overhead and fragmentation, uninstrumented libraries. High-water marks show the peak of each allocation since mysqld started.")}</h3>
      <div class="card tablewrap"><p style="margin:0 0 6px">${g(mp.total_mb)} GiB accounted by MySQL${seen != null ? ` · ${g(seen)} GiB seen by the OS · ${g(seen - mp.total_mb)} GiB not accounted` : ""} · connections ${g(mp.connections_mb)} GiB${rs.pfs_mem_per_connection_kb ? ` (${bytes(rs.pfs_mem_per_connection_kb * 1024)} each)` : ""} · background threads ${g(mp.background_mb)} GiB${mp.growth_mb != null ? ` · ${mp.growth_mb >= 0 ? "+" : "−"}${bytes(Math.abs(mp.growth_mb) * 1048576)} during the window` : ""}</p>
      <table class="kvt"><tbody>${Object.entries(mp.categories_mb || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${g(v)} GiB</td></tr>`).join("")}</tbody></table>
      <table style="margin-top:8px"><thead><tr><th>Largest allocations</th><th class="num">now</th><th class="num">peak</th></tr></thead><tbody>${(mp.top || []).slice(0, 8).map(t => `<tr><td class="mono" style="word-break:break-all">${esc(t.event.replace(/^memory\//, ""))}</td><td class="num">${t.current_mb != null ? bytes(t.current_mb * 1048576) : "–"}</td><td class="num">${t.high_mb != null ? bytes(t.high_mb * 1048576) : "–"}</td></tr>`).join("")}</tbody></table></div>`;
  }
  h += "<h3>Diagnosis</h3>" + ((r.diagnosis || []).map(dg => `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}${Object.keys(dg.evidence || {}).length ? `<div class="muted" style="font-size:12px;margin-top:3px">${esc(kvText(dg.evidence))}</div>` : ""}</div>`).join("") || '<p class="muted">none</p>');
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
  // dismissible (WCAG 1.4.13): Esc, scrolling and page changes hide it
  document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", hide, { capture: true, passive: true });
  window.addEventListener("hashchange", hide);
  document.addEventListener("mouseover", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) show(el); });
  document.addEventListener("mouseout", e => { if (e.target.closest && e.target.closest("[data-tip]")) hide(); });
  document.addEventListener("focusin", e => { if (e.target.dataset && e.target.dataset.tip) show(e.target); });
  document.addEventListener("focusout", hide);
  document.addEventListener("click", e => { const el = e.target.closest && e.target.closest("[data-tip]"); if (el) show(el); else hide(); });
})();

// ------------------------------------------------------------------ router
async function loadMe() { const d = await api("/api/me", { allow401: true, anyPage: true }); ME = d.user; if (d.server) SERVER = d.server; }
function markOptionalColumns(root) {
  root.querySelectorAll("table[data-opt]").forEach(t => {
    // exact header text (without the ⓘ): "Best" must not also hide "Best gain"
    const opt = t.dataset.opt.split("|");
    const txt = th => [...th.childNodes].filter(n => !(n.classList && n.classList.contains("itip"))).map(n => n.textContent).join("").replace(/[▴▾]/g, "").trim();
    const idx = [...t.querySelectorAll("thead th")].map((th, i) => opt.includes(txt(th)) ? i : -1).filter(i => i >= 0);
    if (!idx.length) return;
    t.querySelectorAll("tr").forEach(tr => idx.forEach(i => { const c = tr.children[i]; if (c && !c.hasAttribute("colspan")) c.classList.add("opt"); }));
  });
}
// once per frame at most: charts redraw their cross-hair labels on every pointer move
let optPending = false;
new MutationObserver(() => {
  $view.classList.remove("loading");
  if (optPending) return; optPending = true;
  requestAnimationFrame(() => { optPending = false; markOptionalColumns($view); });
}).observe($view, { childList: true, subtree: true });
function notFound(what) {
  setTitle("Not found");
  $view.innerHTML = `<div class="card notfound" style="margin-top:20px"><h1>${esc(what || "This page does not exist")}</h1><p class="muted">It may have been deleted, or it belongs to another account.</p>
    <div class="row"><a class="btn primary" href="#/">Environments</a><a class="btn" href="#/studies">Studies</a><a href="#" id="goBack">Back</a></div></div>`;
  document.getElementById("goBack").onclick = e => { e.preventDefault(); history.back(); };
}
async function route() {
  clearTimers();
  const nav = ++navSeq;
  widthFns = [];
  document.getElementById("tip").hidden = true;
  setTitle("");
  $view.classList.add("loading");
  const h = location.hash.replace(/^#/, "").split("?")[0] || "/";
  const parts = h.split("/").filter(Boolean);
  const idPages = ["env", "study", "session", "run"];
  try {
    if (idPages.includes(parts[0]) && !/^\d+$/.test(parts[1] || "")) return notFound();
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
    if (parts[0] === "notes") return await notesPage();
    if (parts[0] === "parameters") return await parametersPage();
    if (parts[0] === "env") return await envPage(+parts[1]);
    if (parts[0] === "studies") return await studiesPage();
    if (parts[0] === "study") return await studyPage(+parts[1]);
    if (parts[0] === "session") return await sessionPage(+parts[1]);
    if (parts[0] === "run") return await runPage(+parts[1], parts[2]);
    notFound();
  } catch (e) {
    if (nav !== navSeq || e.message === "login required") return;
    if (/not found/i.test(e.message)) notFound(e.message.replace(/^\w/, c => c.toUpperCase()));
    else $view.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  } finally {
    if (nav === navSeq) $view.classList.remove("loading");
  }
}
window.addEventListener("hashchange", route);
// charts follow width changes only (a phone's URL bar hiding changes the height): redrawn in place, the page is not reloaded
let resizeT, lastW = innerWidth;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (Math.abs(innerWidth - lastW) < 40) return; lastW = innerWidth; widthFns.forEach(f => f()); }, 300); });
loadMe().then(route);

// ------------------------------------------------------------------ stars, people's feedback, model advice
const FB_VERDICT = { reasonable: ["good", "reasonable to try"], questionable: ["warn", "questionable"], "not-reasonable": ["bad", "not reasonable"],
  agree: ["good", "agree"], disagree: ["bad", "disagree"] };
function starBtn(runId, on) {
  return `<button class="star ${on ? "on" : ""}" data-star="${runId}" aria-pressed="${on ? "true" : "false"}" aria-label="Star" data-tip="${on ? "Starred: click to remove" : "Star this run to find it later"}">${on ? "★" : "☆"}</button>`;
}
function wireStars(root) {
  root.querySelectorAll("[data-star]").forEach(b => b.onclick = async e => {
    e.stopPropagation(); e.preventDefault();
    const on = b.classList.contains("on");
    await api(`/api/runs/${b.dataset.star}/star`, { method: on ? "DELETE" : "PUT" });
    b.classList.toggle("on", !on); b.textContent = on ? "☆" : "★"; b.setAttribute("aria-pressed", String(!on));
    b.dataset.tip = on ? "Star this run to find it later" : "Starred: click to remove";
  });
}
function fbMark(list) {  // compact marker for run lists: count, coloured by the most critical view
  if (!list || !list.length) return "";
  const rank = { "not-reasonable": 3, disagree: 3, questionable: 2, reasonable: 1, agree: 1 };
  const worst = list.reduce((a, f) => (rank[f.verdict] || 0) > (rank[a.verdict] || 0) ? f : a, list[0]);
  const tip = list.map(f => `${(FB_VERDICT[f.verdict] || ["", f.verdict])[1]}${f.about && f.about !== "run" ? " (" + f.about.replace(/^advice:/, "advice: ") + ")" : ""}${f.email ? " · " + f.email : ""}${f.comment ? ": " + f.comment : ""}${f.alternative ? " · instead: " + f.alternative : ""}`).join("\n");
  return `<span class="fbmark ${(FB_VERDICT[worst.verdict] || [""])[0]}" data-tip="${esc(tip)}" tabindex="0">💬 ${list.length}</span>`;
}
function fbEntry(f) {
  const [cls, txt] = FB_VERDICT[f.verdict] || ["", f.verdict];
  return `<div class="fb ${cls}"><b>${esc(txt)}</b>${f.about && f.about !== "run" ? ` <span class="muted">on ${esc(f.about.replace(/^advice:/, "advice of "))}</span>` : ""}
    <span class="muted">· ${f.email ? esc(f.email) + " · " : ""}${fmtTime(f.created)}</span>${f.mine !== false && f.id && ME && !ME.share ? ` <button class="ghost" data-fbdel="${f.id}" style="padding:0 6px;font-size:12px">delete</button>` : ""}
    ${f.comment ? `<div>${esc(f.comment)}</div>` : ""}${f.alternative ? `<div><span class="muted">Instead:</span> ${esc(f.alternative)}</div>` : ""}</div>`;
}
function feedbackSection(el, run, list, onChange) {
  const ro = ME && ME.share;
  el.innerHTML = `<h3>Expert feedback${info("Your view of this run: was it a reasonable thing to try at that point? It is shown to the AI planner and the learner as a person's opinion next to the measurements, not as a verdict.")}</h3>
    <div id="fblist">${list.length ? list.map(fbEntry).join("") : '<p class="muted" style="margin:4px 0">No feedback yet.</p>'}</div>
    ${ro ? "" : `<button id="fbopen">Add feedback</button><form class="fbform" id="fbform" hidden>
      <div class="seg" role="radiogroup">${["reasonable", "questionable", "not-reasonable"].map((v, i) => `<button type="button" data-v="${v}" class="${i === 0 ? "on" : ""}">${FB_VERDICT[v][1]}</button>`).join("")}</div>
      <textarea name="comment" rows="2" placeholder="Why (what in the data makes it reasonable or not)"></textarea>
      <input name="alternative" placeholder="What you would have tried instead (optional)">
      <div class="row"><button class="primary" type="submit">Save</button><button type="button" class="ghost" id="fbcancel">Cancel</button><span class="err" id="fberr"></span></div></form>`}`;
  el.querySelectorAll("[data-fbdel]").forEach(b => b.onclick = async () => { if (confirm("Delete this feedback?")) { await api(`/api/feedback/${b.dataset.fbdel}`, { method: "DELETE" }); onChange(); } });
  if (ro) return;
  const form = el.querySelector("#fbform");
  let verdict = "reasonable";
  el.querySelector("#fbopen").onclick = () => { form.hidden = false; el.querySelector("#fbopen").hidden = true; };
  el.querySelector("#fbcancel").onclick = () => { form.hidden = true; el.querySelector("#fbopen").hidden = false; };
  form.querySelectorAll("[data-v]").forEach(b => b.onclick = () => { verdict = b.dataset.v; form.querySelectorAll("[data-v]").forEach(x => x.classList.toggle("on", x === b)); });
  form.onsubmit = async e => {
    e.preventDefault();
    try { await api(`/api/runs/${run.id}/feedback`, { method: "POST", body: { verdict, comment: form.comment.value, alternative: form.alternative.value } }); onChange(); }
    catch (err) { el.querySelector("#fberr").textContent = err.message; }
  };
}
function adviceHtml(rec) {  // what each model advised for this decision, with agree / disagree for people to review it
  const L = rec.llm || {}, out = [];
  const choice = c => !c ? "–" : c.candidate_id ? `<code>${esc(c.candidate_id)}</code>` : (c.changes || []).map(x => `<code>${esc(x.variable)}=${esc(x.value)}</code>`).join(" ") || "–";
  const acts = about => rec.run_id && ME && !ME.share ? `<span class="acts"><button data-adv="${esc(about)}" data-v="agree" data-rec="${rec.id}" data-run="${rec.run_id}">agree</button><button data-adv="${esc(about)}" data-v="disagree" data-rec="${rec.id}" data-run="${rec.run_id}">disagree</button></span>` : "";
  const P = L.planner;
  if (P && P.plan) {
    const p = P.plan, pr = p.prediction || {};
    out.push(`<div class="advice"><div class="h"><b>Planner</b> <span class="muted">${esc(P.model || "")}</span> → ${p.action === "stop" ? "<b>stop</b>" : choice(p.choice)}
      ${P.rejected ? ` <span class="badge warn" data-tip="${esc(P.rejected)}" tabindex="0">not used</span>` : P.applied !== undefined ? (P.applied === P.engine_next ? ' <span class="badge plain">same as the rules</span>' : ` <span class="badge info" data-tip="The rules would have run ${esc(P.engine_next || "nothing")}" tabindex="0">overruled the rules</span>`) : ""}${acts("advice:planner")}</div>
      <div>${esc(p.diagnosis)}</div>${p.action !== "stop" ? `<div class="muted" style="font-size:12.5px">Predicts ${pr.goal_delta_pct_low}% to ${pr.goal_delta_pct_high}% on the goal; ${esc(pr.metric)} ${esc(pr.metric_direction)} ~${pr.metric_change_pct}% · confidence ${esc(p.confidence)}${info("Why above the noise: " + (p.why_above_noise || "") + " · Rationale: " + (p.rationale || "") + ((p.alternatives || []).length ? " · Not now: " + p.alternatives.map(a => a.candidate_id + " (" + a.why_not_now + ")").join("; ") : ""))}</div>` : `<div class="muted">${esc(p.stop_advice)}</div>`}</div>`);
    if (P.critic) out.push(`<div class="advice"><div class="h"><b>Reviewer</b> <span class="muted">${esc(P.critic_model || "")}</span> → <b>${esc(P.critic.verdict)}</b>${P.critic.verdict === "revise" ? " to " + (P.critic.action === "stop" ? "<b>stop</b>" : choice(P.critic)) : ""}${P.final ? ' <span class="badge info">taken</span>' : ""}${acts("advice:reviewer")}</div>
      ${(P.critic.issues || []).length ? `<div style="font-size:12.5px">${P.critic.issues.map(esc).join("<br>")}</div>` : ""}${P.critic.reason ? `<div class="muted" style="font-size:12.5px">${esc(P.critic.reason)}</div>` : ""}</div>`);
  } else if (P && P.error) out.push(`<p class="muted" style="font-size:12.5px">Planner error: ${esc(P.error)}</p>`);
  (L.suggestions || []).forEach((sg, n) => out.push(`<div class="advice"><div class="h"><b>Idea</b> <span class="muted">${esc(L.model || "")}</span> ${esc(sg.title || "")}: ${(sg.changes || []).map(x => `<code>${esc(x.variable)}=${esc(x.value)}</code>`).join(" ")}
    <span class="muted">${esc(sg.expected_effect || "")}${sg.risk && sg.risk !== "none" && sg.risk !== "low" ? " · " + esc(sg.risk) : ""}</span>${acts("advice:idea:" + (sg.title || n))}</div><div style="font-size:12.5px">${esc(sg.rationale || "")}${sg.mechanism ? `<span class="muted"> Mechanism: ${esc(sg.mechanism)}</span>` : ""}</div></div>`));
  return out.length ? `<h3>Model advice${info("What each AI model advised at this decision, stored as given. Agree / disagree records your view for the planner and for comparing models.")}</h3>` + out.join("") : "";
}
function wireAdvice(root, feedback) {
  root.querySelectorAll("[data-adv]").forEach(b => {
    const mine = (feedback || []).find(f => f.rec_id === +b.dataset.rec && f.about === b.dataset.adv);
    if (mine && mine.verdict === b.dataset.v) b.classList.add("primary");
    b.onclick = async () => {
      const comment = b.dataset.v === "disagree" ? prompt("Why, or what would you do instead?") : "";
      if (comment === null) return;
      try {
        await api(`/api/runs/${b.dataset.run}/feedback`, { method: "POST", body: { verdict: b.dataset.v, about: b.dataset.adv, rec_id: +b.dataset.rec, comment } });
        b.parentElement.querySelectorAll("button").forEach(x => x.classList.toggle("primary", x === b));
      } catch (err) { alert(err.message); }
    };
  });
}
async function notesPage() {
  setNav("notes");
  const filter = new URLSearchParams(location.hash.split("?")[1] || "").get("f") || "all";
  const scope = new URLSearchParams(location.hash.split("?")[1] || "").get("scope") || "mine";
  const d = await api("/api/notes?scope=" + scope);
  setTitle("Starred & feedback");
  const runs = d.runs.filter(r => filter === "all" || (filter === "starred" ? r.starred : r.feedback.length));
  const link = (f, sc) => `#/notes?f=${f}&scope=${sc}`;
  $view.innerHTML = `<h1>Starred &amp; feedback</h1>
  <p class="sub">Runs you starred or commented on, newest first.</p>
  <div class="row"><div class="seg">${[["all", "All"], ["starred", "Starred"], ["feedback", "With feedback"]].map(([k, t]) => `<button data-f="${k}" class="${filter === k ? "on" : ""}">${t}</button>`).join("")}</div>
   ${ME.is_admin ? `<div class="seg">${[["mine", "Mine"], ["all", "Everyone's feedback"]].map(([k, t]) => `<button data-sc="${k}" class="${scope === k ? "on" : ""}">${t}</button>`).join("")}</div>` : ""}</div>
  <div class="card tablewrap" style="margin-top:10px"><table data-opt="Environment|Goal|Δ parent"><thead><tr><th></th><th>Run</th><th>Session</th><th>Environment</th><th>Verdict</th><th class="num">Goal</th><th class="num">Δ parent</th><th>Feedback</th><th>Last note</th></tr></thead><tbody>
  ${runs.length ? runs.map(r => `<tr class="click" data-href="#/run/${r.id}"><td>${starBtn(r.id, r.starred)}</td><td><a href="#/run/${r.id}">#${r.seq} ${esc(r.label)}</a></td><td><a href="#/session/${r.session.id}">#${r.session.id} ${esc(r.session.name || "")}</a></td><td>${esc(r.environment.name)}</td>
    <td>${verdictBadge(r.verdict)}</td><td class="num">${num(r.goal_value)}</td><td class="num">${pct((r.evaluation || {}).goal_delta_pct)}</td>
    <td>${fbMark(r.feedback)}${r.feedback.length ? ` <span class="muted" style="font-size:12.5px">${esc(((r.feedback[0].comment || r.feedback[0].alternative || "")).slice(0, 80))}</span>` : ""}</td><td style="white-space:nowrap">${fmtTime(r.last_note)}</td></tr>`).join("")
    : `<tr><td colspan="9" class="muted">Nothing here yet: star a run (☆ in a session's run list or on a run page) or add feedback on a run page.</td></tr>`}</tbody></table></div>`;
  $view.querySelectorAll("[data-f]").forEach(b => b.onclick = () => location.hash = link(b.dataset.f, scope));
  $view.querySelectorAll("[data-sc]").forEach(b => b.onclick = () => location.hash = link(filter, b.dataset.sc));
  $view.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = () => location.hash = tr.dataset.href);
  wireStars($view);
}
