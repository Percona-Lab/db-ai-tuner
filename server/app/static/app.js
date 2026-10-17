/* AI DB Tuner single-page UI */
const $view = document.getElementById("view");
const C = window.Charts;
let ME = null, SERVER = location.origin, timers = [];

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
async function api(path, opts = {}) {
  const r = await fetch(path, { method: opts.method || "GET", headers: opts.body ? { "Content-Type": "application/json" } : {}, body: opts.body ? JSON.stringify(opts.body) : undefined, credentials: "same-origin" });
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
function num(v, d) { return v == null ? "–" : C.fmtNum(+v); }
function pct(v, sign = true) { if (v == null) return "–"; return (sign && v > 0 ? "+" : "") + (+v).toFixed(1) + "%"; }
function deltaCls(v) { return v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""; }
function goalName(g) { return g === "latency" ? "Avg query response time" : "Throughput (TPS)"; }
function goalUnit(g) { return g === "latency" ? "ms" : "tps"; }
function verdictBadge(v) {
  const m = { improved: "good", "efficiency-gain": "good", regressed: "bad", "efficiency-loss": "warn", neutral: "plain", reference: "info", repeat: "plain" };
  return v ? `<span class="badge ${m[v] || "plain"}">${esc(v)}</span>` : "";
}
function statusBadge(s) { const m = { active: "good", measuring: "info", done: "plain", closed: "plain", abandoned: "warn", aborted: "warn" }; return `<span class="badge ${m[s] || "plain"}">${esc(s)}</span>`; }
function sevBadge(s) { const m = { high: "bad", medium: "warn", low: "plain" }; return `<span class="badge ${m[s] || "plain"}">${esc(s)}</span>`; }
function riskBadge(r) { const m = { none: "good", low: "good", compat: "warn", observability: "warn", durability: "bad" }; return `<span class="badge ${m[r] || "plain"}">risk: ${esc(r)}</span>`; }
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function setNav(active) {
  const nav = document.getElementById("nav");
  if (!ME) { nav.innerHTML = `<a class="right" href="#/login">Log in</a><a href="#/register">Create account</a>`; return; }
  nav.innerHTML = `<a href="#/" class="${active === "envs" ? "on" : ""}">Environments</a><a href="#/connect" class="${active === "connect" ? "on" : ""}">Connect agent</a>` +
    `<a href="#/account" class="${active === "account" ? "on" : ""}">Account</a><span class="right">${esc(ME.email)}</span><a href="#" id="logout">Log out</a>`;
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
  $view.innerHTML = `<section class="hero"><h1>Tune MySQL with an agent you can drive from Claude Code</h1>
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

async function envList() {
  setNav("envs");
  const d = await api("/api/environments");
  if (!d.environments.length) {
    $view.innerHTML = `<h1>Environments</h1><p class="sub">An environment is one database server with a connected tuning-agent.</p>
    <div class="card"><h3>No environment connected yet</h3><p>Install the agent on the database host to see it here.</p><a class="btn primary" href="#/connect">Connect an agent</a></div>`;
    return;
  }
  $view.innerHTML = `<div class="row"><div><h1>Environments</h1><p class="sub">Database servers with a connected tuning-agent.</p></div><span class="spacer"></span><a class="btn" href="#/connect">Connect another</a></div>
  <div class="grid g3" id="cards"></div>`;
  const cards = document.getElementById("cards");
  d.environments.forEach(e => {
    const l = (e.latest && e.latest.db) || {}, o = (e.latest && e.latest.os) || {};
    const a = document.createElement("a");
    a.className = "card"; a.href = "#/env/" + e.id; a.style.color = "inherit";
    a.innerHTML = `<div class="row"><b style="font-size:15px">${esc(e.name)}</b><span class="spacer"></span>${e.online ? '<span class="badge good">online</span>' : `<span class="badge plain">offline · ${ago(e.last_seen)}</span>`}</div>
     <div class="muted" style="margin:4px 0 10px">${esc(e.db)}<br>${e.cpus || "?"} CPUs · ${bytes(e.ram_bytes)} RAM · ${e.disk} · data ${bytes(e.data_bytes)}</div>
     <div class="row" style="gap:18px"><div><div class="muted" style="font-size:12px">TPS</div><b>${e.online ? num(l.tps) : "–"}</b></div>
     <div><div class="muted" style="font-size:12px">Avg latency</div><b>${e.online && l.avg_latency_ms != null ? num(l.avg_latency_ms) + " ms" : "–"}</b></div>
     <div><div class="muted" style="font-size:12px">CPU busy</div><b>${e.online && o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–"}</b></div>
     <div><div class="muted" style="font-size:12px">Sessions</div><b>${e.sessions}</b></div></div>
     ${e.active_session ? `<div style="margin-top:10px"><span class="badge info">active: ${esc(e.active_session.name)}</span></div>` : ""}`;
    cards.appendChild(a);
  });
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

async function accountPage() {
  setNav("account");
  $view.innerHTML = `<h1>Account</h1><p class="sub">${esc(ME.email)}</p><div class="card form" style="margin:0;max-width:420px"><h3>Change password</h3>
  <form id="f"><label>Current password</label><input type="password" name="old" required><label>New password</label><input type="password" name="new" required minlength="8">
  <div class="err" id="err"></div><button class="primary">Change password</button></form></div>`;
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    try { await api("/api/auth/password", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); document.getElementById("err").textContent = "Password changed."; }
    catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

function chartsGrid(container, m, defs, window_) {
  container.innerHTML = "";
  defs.forEach(def => {
    const cell = document.createElement("div"); cell.className = "card";
    container.appendChild(cell);
    C.lineChart(cell, {
      title: def.title, unit: def.unit, ts: m.ts, window: window_, max100: def.max100, min0: def.min0,
      series: def.series.map(([g, k, name]) => ({ name, values: (m[g] || {})[k] })),
    });
  });
}
const LIVE_CHARTS = [
  { title: "Throughput", unit: "per s", series: [["db", "tps", "transactions"], ["db", "qps", "queries"]] },
  { title: "Avg statement latency", unit: "ms", series: [["db", "avg_latency_ms", "avg latency"]] },
  { title: "CPU", unit: "%", max100: true, series: [["os", "cpu_user", "user"], ["os", "cpu_sys", "system"], ["os", "cpu_iowait", "iowait"], ["os", "mysqld_cpu_pct", "mysqld process"]] },
  { title: "Disk IOPS", unit: "per s", series: [["os", "disk_r_iops", "reads"], ["os", "disk_w_iops", "writes"]] },
];
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
  { title: "Disk throughput", unit: "MiB/s", series: [["os", "disk_r_mbs", "read"], ["os", "disk_w_mbs", "write"]] },
  { title: "Disk utilisation / latency", unit: "", series: [["os", "disk_util", "util %"], ["os", "disk_await_ms", "await ms"]] },
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
  { title: "Context switches", unit: "per s", series: [["os", "ctx_switches", "ctx switches"]] },
];

async function envPage(id) {
  setNav("envs");
  const d = await api("/api/environments/" + id);
  const e = d.environment, f = d.facts || {}, hw = f.hardware || {}, db = f.db || {};
  const kv = db.key_variables || {};
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> /</div>
  <div class="row"><h1>${esc(e.name)}</h1>${e.online ? '<span class="badge good">agent online</span>' : `<span class="badge plain">agent offline · last seen ${ago(e.last_seen)}</span>`}<span class="spacer"></span>
  <button class="ghost" id="rename">Rename</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">${esc(e.db)} · ${esc(hw.hostname || "")} · agent ${esc(e.agent_version || "")}</p>
  <div class="grid g2"><div class="card"><h3 style="margin-top:0">Hardware &amp; OS</h3><div class="kv">
   <div>CPU</div><div>${hw.cpus} × ${esc(hw.cpu_model)}</div><div>Memory</div><div>${bytes(hw.ram_bytes)} (swap ${bytes(hw.swap_bytes)})</div>
   <div>Data disk</div><div>${esc(hw.data_device)} · ${hw.disk_rotational ? "rotational" : "SSD/NVMe"} · ${esc(hw.filesystem)} · scheduler ${esc(hw.disk_scheduler)}</div>
   <div>Disk space</div><div>${bytes(hw.disk_free_bytes)} free of ${bytes(hw.disk_total_bytes)}</div><div>OS</div><div>${esc(hw.os)} · kernel ${esc(hw.kernel)}</div>
   <div>Platform</div><div>${esc(hw.vendor || "")} · THP ${esc(hw.thp)} · swappiness ${esc(hw.swappiness)}</div></div></div>
   <div class="card"><h3 style="margin-top:0">Database</h3><div class="kv"><div>Server</div><div>${esc(db.version_comment)} ${esc(db.version)}</div>
   <div>Data</div><div>${bytes(db.data_bytes)} in ${db.tables} InnoDB tables</div><div>Datadir</div><div class="mono">${esc(db.datadir)}</div>
   ${Object.entries(kv).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(val(k, v))}</div>`).join("")}</div></div></div>
  <div class="card" style="margin-top:14px"><h3 style="margin-top:0">Settings managed by the agent</h3>${Object.keys((f.agent || {}).managed || {}).length ? `<p class="muted" style="margin:0 0 6px">${esc((f.agent || {}).cnf_path || "")} · restarts ${(f.agent || {}).allow_restart === false ? "disabled" : "allowed"}</p><pre><code>[mysqld]\n${Object.entries(f.agent.managed).map(([k, v]) => esc(k) + " = " + esc(v)).join("\n")}</code></pre>` : '<span class="muted">None yet: <code>tuning-agent baseline --apply</code> writes the first set.</span>'}</div>
  <div class="row"><h2>Live metrics</h2><span class="spacer"></span><select id="range" style="width:auto"><option value="900">15 min</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">24 hours</option></select></div>
  <div class="grid g3" id="live"></div>
  <h2>Tuning sessions</h2><div class="card tablewrap"><table><thead><tr><th>Session</th><th>Goal</th><th>Policy</th><th>Status</th><th class="num">Runs</th><th class="num">First run</th><th class="num">Best</th><th class="num">Best gain</th><th>Started</th></tr></thead><tbody id="sess"></tbody></table></div>
  <h2>Recent activity</h2><div class="card" id="events"></div>`;
  const tb = document.getElementById("sess");
  if (!d.sessions.length) tb.innerHTML = `<tr><td colspan="9" class="muted">No sessions yet. Start one with <code>tuning-agent session start --goal tps</code>.</td></tr>`;
  d.sessions.forEach(s => {
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/session/" + s.id;
    tr.innerHTML = `<td><a href="#/session/${s.id}">${esc(s.name)}</a></td><td>${goalName(s.goal)}</td><td>${esc(s.policy)}</td><td>${statusBadge(s.status)}</td><td class="num">${s.runs}</td>
     <td class="num">${num(s.first_value)}</td><td class="num">${num(s.best_value)}</td><td class="num ${deltaCls(s.best_gain_pct)}">${pct(s.best_gain_pct)}</td><td>${fmtTime(s.created)}</td>`;
    tb.appendChild(tr);
  });
  renderEvents(document.getElementById("events"), d.events);
  document.getElementById("rename").onclick = async () => { const n = prompt("Environment name", e.name); if (n) { await api("/api/environments/" + id, { method: "PATCH", body: { name: n } }); envPage(id); } };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this environment with all sessions, runs and metrics? The agent will re-register it on its next report.")) { await api("/api/environments/" + id, { method: "DELETE" }); location.hash = "#/"; } };
  const live = document.getElementById("live"), range = document.getElementById("range");
  const load = async () => { const span = +range.value; const now = Date.now() / 1000; const m = await api(`/api/environments/${id}/metrics?start=${now - span}&end=${now}&points=240`); chartsGrid(live, m, [...LIVE_CHARTS, LIVE_PSI, LIVE_THREADS]); };
  range.onchange = load;
  await load();
  timers.push(setInterval(load, 15000));
}

function renderEvents(el, events) {
  if (!events.length) { el.innerHTML = '<span class="muted">No activity yet.</span>'; return; }
  el.innerHTML = events.map(ev => `<div class="ev"><span class="muted">${fmtTime(ev.ts)}</span><span><span class="badge plain">${esc(ev.type)}</span></span><span>${esc(ev.message)}</span></div>`).join("");
}

async function sessionPage(id) {
  setNav("envs");
  const d = await api("/api/sessions/" + id);
  const s = d.session, runs = d.runs.filter(r => r.status !== "aborted");
  const done = runs.filter(r => r.goal_value != null);
  const lower = s.goal === "latency";
  const first = done[0];
  // configurations are scored by the mean of all runs measured with them (repeats reduce noise)
  const groups = {};
  done.forEach(r => { const k = JSON.stringify(Object.entries(r.managed || {}).sort()); (groups[k] = groups[k] || []).push(r); });
  const cfgs = Object.values(groups).map(rs => ({ runs: rs, mean: rs.reduce((a, r) => a + r.goal_value, 0) / rs.length }));
  cfgs.sort((a, b) => lower ? a.mean - b.mean : b.mean - a.mean);
  const bestCfg = cfgs[0], best = bestCfg ? bestCfg.runs[bestCfg.runs.length - 1] : null;
  const firstCfg = first ? cfgs.find(c => c.runs.includes(first)) : null;
  const gain = firstCfg && bestCfg && firstCfg.mean ? (lower ? (firstCfg.mean - bestCfg.mean) : (bestCfg.mean - firstCfg.mean)) / firstCfg.mean * 100 : null;
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${d.environment.id}">${esc(d.environment.name)}</a> /</div>
  <div class="row"><h1>${esc(s.name)}</h1>${statusBadge(s.status)}<span class="spacer"></span><button class="ghost" id="rename">Rename</button><button class="ghost danger" id="del">Delete</button></div>
  <p class="sub">Goal: <b>${goalName(s.goal)}</b> · policy <b>${esc(s.policy)}</b>${s.policy === "benchmark" ? " (durability / observability trade-offs allowed)" : " (production-safe changes only)"} · noise threshold ±${s.noise_pct}% · started ${fmtTime(s.created)}${s.workload ? " · workload: " + esc(s.workload) : ""}</p>
  ${calibrationNote(s)}
  <div class="tiles"><div class="tile"><div class="k">Runs measured</div><div class="v">${done.length}</div></div>
   <div class="tile"><div class="k">First run (${goalUnit(s.goal)})</div><div class="v">${first ? num(first.goal_value) : "–"}</div><div class="d">${first ? esc(first.label) : ""}</div></div>
   <div class="tile"><div class="k">Best configuration (${goalUnit(s.goal)})</div><div class="v">${bestCfg ? num(bestCfg.mean) : "–"}</div><div class="d">${bestCfg ? (bestCfg.runs.length > 1 ? "mean of runs " : "run ") + bestCfg.runs.map(r => "#" + r.seq).join(", ") : ""}</div></div>
   <div class="tile"><div class="k">Best vs first configuration</div><div class="v ${deltaCls(gain)}">${pct(gain)}</div><div class="d">${lower ? "lower latency is better" : "higher throughput is better"}</div></div></div>
  <div class="card" style="margin-top:14px" id="bars"></div>
  <h2>Runs</h2><div class="card tablewrap"><table><thead><tr><th>#</th><th>Label</th><th>Status</th><th>Changes vs previous run</th><th class="num">${goalUnit(s.goal) === "ms" ? "Avg ms" : "Goal"}</th><th class="num">vs prev</th><th class="num">vs first</th><th>Verdict</th>
   <th class="num">vs parent cfg</th><th class="num">TPS</th><th class="num">Avg lat ms</th><th class="num">mysqld CPU ms/trx</th><th class="num">Disk IOs/trx</th><th class="num">CPU %</th><th>Duration</th></tr></thead><tbody id="runs"></tbody></table></div>
  <h2>Latest recommendations</h2><div class="card" id="recs"></div>
  <h2>Notes</h2><div class="card"><textarea id="notes" rows="3" placeholder="Notes about this session (workload, hypotheses, conclusions)">${esc(s.notes || "")}</textarea><div class="row" style="margin-top:6px"><button id="savenotes">Save notes</button><span id="saved" class="muted"></span></div></div>
  <h2>Activity</h2><div class="card" id="events"></div>`;
  C.barChart(document.getElementById("bars"), {
    title: goalName(s.goal) + " per run (" + goalUnit(s.goal) + ") — green: runs of the best configuration", unit: goalUnit(s.goal), metric: s.goal === "latency" ? "avg latency" : "goal value",
    ref: first ? first.goal_value : null,
    bars: done.map(r => ({ label: "#" + r.seq + " " + r.label, value: r.goal_value, sub: r.delta_vs_first_pct != null ? pct(r.delta_vs_first_pct) : "", href: "#/run/" + r.id,
      highlight: bestCfg && bestCfg.runs.includes(r) ? "best" : false,
      tip: `<div>vs previous<b>${pct(r.delta_pct)}</b></div><div>verdict<b>${esc(r.verdict || "")}</b></div>` })),
  });
  const tb = document.getElementById("runs");
  if (!d.runs.length) tb.innerHTML = `<tr><td colspan="15" class="muted">No runs yet: <code>tuning-agent measure start</code> … <code>measure stop</code>.</td></tr>`;
  d.runs.forEach(r => {
    const sm = r.summary || {};
    const ch = (r.changes || []).map(c => `<span class="mono">${esc(c.variable)}</span>=${esc(val(c.variable, c.new))}`).join(", ");
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/run/" + r.id;
    tr.innerHTML = `<td>${r.seq}</td><td><a href="#/run/${r.id}">${esc(r.label)}</a></td><td>${statusBadge(r.status)}</td><td style="max-width:320px">${ch || '<span class="muted">none</span>'}</td>
     <td class="num"><b>${num(r.goal_value)}</b></td><td class="num ${deltaCls(r.delta_pct)}">${pct(r.delta_pct)}</td><td class="num ${deltaCls(r.delta_vs_first_pct)}">${pct(r.delta_vs_first_pct)}</td><td>${verdictBadge(r.verdict)}</td>
     <td class="num ${deltaCls((r.evaluation || {}).goal_delta_pct)}" title="vs mean of runs ${esc(((r.evaluation || {}).parent_runs || []).join(", "))}">${pct((r.evaluation || {}).goal_delta_pct)}</td>
     <td class="num">${num(sm.tps)}</td><td class="num">${num(sm.avg_latency_ms)}</td><td class="num">${num(sm.mysqld_cpu_ms_per_trx)}</td><td class="num">${num(sm.disk_ios_per_trx)}</td><td class="num">${num(sm.cpu_busy)}</td><td>${dur(sm.duration_s || (r.status === "measuring" ? Date.now() / 1000 - r.started : null))}</td>`;
    tb.appendChild(tr);
  });
  renderRecs(document.getElementById("recs"), d.recommendations[0], runs);
  renderEvents(document.getElementById("events"), d.events);
  document.getElementById("savenotes").onclick = async () => { await api("/api/sessions/" + id, { method: "PATCH", body: { notes: document.getElementById("notes").value } }); document.getElementById("saved").textContent = "Saved."; };
  document.getElementById("rename").onclick = async () => { const n = prompt("Session name", s.name); if (n) { await api("/api/sessions/" + id, { method: "PATCH", body: { name: n } }); sessionPage(id); } };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this session and its runs?")) { await api("/api/sessions/" + id, { method: "DELETE" }); location.hash = "#/env/" + d.environment.id; } };
  if (runs.some(r => r.status === "measuring") || s.status === "active") timers.push(setInterval(async () => {
    const d2 = await api("/api/sessions/" + id);
    if (JSON.stringify(d2.runs.map(r => [r.id, r.status])) !== JSON.stringify(d.runs.map(r => [r.id, r.status])) || d2.recommendations.length !== d.recommendations.length) { clearTimers(); sessionPage(id); }
  }, 10000));
}

function calibrationNote(s) {
  const c = s.calibration;
  if (!c) return `<div class="note" style="margin-bottom:12px">Noise not calibrated: verdicts use a fixed ±${s.noise_pct || 2}% threshold. Standard protocol: <code>tuning-agent calibrate --runs 3</code> on the starting configuration.</div>`;
  const eff = Object.entries(c.efficiency || {}).filter(([k]) => EFF_LABELS[k]).map(([k, e]) => `${EFF_LABELS[k]} ±${e.noise_pct}%`).join(" · ");
  return `<div class="note" style="margin-bottom:12px"><b>Noise calibrated</b> from runs ${c.runs.map(x => "#" + x).join(", ")}: goal mean ${num(c.goal_mean)}, run-to-run CV ${c.goal_cv_pct}% (range ${c.goal_range_pct}%) → changes within <b>±${c.noise_pct}%</b> are treated as noise. Efficiency thresholds: ${eff}.</div>`;
}
const EFF_LABELS = { mysqld_cpu_ms_per_trx: "mysqld CPU ms/trx", host_cpu_ms_per_trx: "host CPU ms/trx", disk_ios_per_trx: "disk IOs/trx", disk_kb_per_trx: "disk KiB/trx", fsyncs_per_trx: "fsyncs/trx", redo_kb_per_trx: "redo KiB/trx", rows_read_per_trx: "rows read/trx" };

function renderRecs(el, rec, runs) {
  if (!rec) { el.innerHTML = '<span class="muted">No recommendations yet: run <code>tuning-agent recommend</code> after a measured run.</span>'; return; }
  const run = runs && runs.find(r => r.id === rec.run_id);
  let h = `<div class="muted" style="margin-bottom:6px">Generated ${fmtTime(rec.created)}${run ? ` from run #${run.seq} <a href="#/run/${run.id}">${esc(run.label)}</a>` : " without a measured run"} · goal ${esc(rec.goal)} · policy ${esc(rec.policy)}${rec.next ? ` · suggested next: <code>${esc(rec.next)}</code>` : ""}</div>`;
  (rec.diagnosis || []).forEach(dg => { h += `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}</div>`; });
  (rec.items || []).forEach(it => {
    h += `<div class="rec"><div class="h"><b>${it.rank}. ${esc(it.title)}</b><span class="id">${esc(it.id)}</span>${it.kind === "advice" ? '<span class="badge plain">advice only</span>' : it.kind === "revert" ? '<span class="badge warn">revert</span>' : ""}
     ${riskBadge(it.risk)}${it.restart ? '<span class="badge plain">restart</span>' : ""}<span class="badge plain">impact ${it.impact}/3</span><span class="badge plain">confidence ${it.confidence}/3</span>${it.id === rec.next ? '<span class="badge info">next</span>' : ""}</div>
     ${it.changes.length ? `<div class="mono" style="margin:4px 0">${it.changes.map(c => `${esc(c.variable)}: <span class="diff-old">${esc(val(c.variable, c.current))}</span> → <span class="diff-new">${esc(c.proposed)}</span>`).join("<br>")}</div>` : ""}
     <div class="reason">${esc(it.rationale)}${it.note ? " <b>" + esc(it.note) + "</b>" : ""}</div>
     ${Object.keys(it.evidence || {}).length ? `<details><summary>Evidence</summary><div class="kv">${Object.entries(it.evidence).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div></details>` : ""}</div>`;
  });
  el.innerHTML = h;
}

async function runPage(id, tab) {
  setNav("envs");
  const vs = new URLSearchParams(location.hash.split("?")[1] || "").get("vs");
  const r = await api("/api/runs/" + id + (vs ? "?vs=" + vs : ""));
  const s = r.session, sm = r.summary || {}, prev = r.compared_with;
  const ps = (prev && prev.summary) || {};
  const tile = (k, v, unit, pv, lowerBetter) => {
    let d = "";
    if (pv != null && v != null && pv !== 0) { const ch = (v - pv) / Math.abs(pv) * 100; const good = lowerBetter ? ch < 0 : ch > 0; d = `<div class="d ${Math.abs(ch) < 0.05 ? "" : good ? "up" : "down"}">${ch > 0 ? "+" : ""}${ch.toFixed(1)}% vs prev</div>`; }
    return `<div class="tile"><div class="k">${k}</div><div class="v">${num(v)}<span style="font-size:13px;color:var(--ink-2)"> ${unit}</span></div>${d}</div>`;
  };
  $view.innerHTML = `<div class="crumbs"><a href="#/">Environments</a> / <a href="#/env/${s.env_id}">${esc(r.environment.name)}</a> / <a href="#/session/${s.id}">${esc(s.name)}</a> /</div>
  <div class="row"><h1>Run #${r.seq} · ${esc(r.label)}</h1>${statusBadge(r.status)}${verdictBadge(r.verdict)}<span class="spacer"></span>
   <label style="margin:0">Compare with</label><select id="vs" style="width:auto"><option value="">previous run</option>${r.runs.filter(x => x.id !== r.id).map(x => `<option value="${x.id}" ${String(x.id) === vs ? "selected" : ""}>#${x.seq} ${esc(x.label)}</option>`).join("")}</select></div>
  <p class="sub">${fmtTime(r.started)} – ${r.ended ? fmtTime(r.ended) : "measuring…"} (${dur(sm.duration_s)}) · goal ${goalName(s.goal)} = <b>${num(r.goal_value)} ${goalUnit(s.goal)}</b>${r.goal_source ? ` <span class="muted">(${esc(r.goal_source)})</span>` : ""}
   ${r.delta_pct != null ? ` · <span class="${deltaCls(r.delta_pct)}">${pct(r.delta_pct)}</span> vs previous, <span class="${deltaCls(r.delta_vs_first_pct)}">${pct(r.delta_vs_first_pct)}</span> vs first` : ""}${prev ? ` · compared with run #${prev.seq} ${esc(prev.label)}` : ""}</p>
  ${r.notes ? `<div class="note" style="margin-bottom:12px">${esc(r.notes)}</div>` : ""}
  <div class="tiles">${tile("TPS", sm.tps, "/s", ps.tps)}${tile("QPS", sm.qps, "/s", ps.qps)}${tile("Avg statement latency", sm.avg_latency_ms, "ms", ps.avg_latency_ms, true)}${tile("p95 statement latency", sm.p95_latency_ms, "ms", ps.p95_latency_ms, true)}
   ${tile("CPU busy (host)", sm.cpu_busy, "%", ps.cpu_busy, true)}${tile("mysqld CPU", sm.mysqld_cpu_pct, "% of host", ps.mysqld_cpu_pct, true)}${tile("IO wait", sm.cpu_iowait, "%", ps.cpu_iowait, true)}${tile("BP hit ratio", sm.bp_hit_pct, "%", ps.bp_hit_pct)}${tile("Disk IOPS", (sm.disk_r_iops || 0) + (sm.disk_w_iops || 0), "/s", ps.disk_r_iops != null ? (ps.disk_r_iops || 0) + (ps.disk_w_iops || 0) : null, true)}
   ${tile("CPU pressure", sm.mysqld_psi_cpu_some != null ? sm.mysqld_psi_cpu_some : sm.psi_cpu_some, "% stalled", ps.mysqld_psi_cpu_some != null ? ps.mysqld_psi_cpu_some : ps.psi_cpu_some, true)}${tile("I/O pressure", sm.mysqld_psi_io_some != null ? sm.mysqld_psi_io_some : sm.psi_io_some, "% stalled", ps.mysqld_psi_io_some != null ? ps.mysqld_psi_io_some : ps.psi_io_some, true)}${tile("Memory pressure", sm.psi_memory_some, "% stalled", ps.psi_memory_some, true)}
   ${tile("mysqld CPU / trx", sm.mysqld_cpu_ms_per_trx, "ms", ps.mysqld_cpu_ms_per_trx, true)}${tile("Disk IOs / trx", sm.disk_ios_per_trx, "", ps.disk_ios_per_trx, true)}
   ${tile("Redo written", sm.redo_mbs, "MiB/s", ps.redo_mbs)}${tile("Ckpt age max", sm.checkpoint_age_pct_max, "%", ps.checkpoint_age_pct_max, true)}${tile("Threads running", sm.threads_running_avg, "avg", ps.threads_running_avg, true)}${tile("TPS variation", sm.tps_cv_pct, "% CV", ps.tps_cv_pct, true)}</div>
  ${Object.keys(r.external || {}).length ? `<h3>Benchmark-reported values</h3><div class="row">${Object.entries(r.external).map(([k, v]) => `<span class="badge plain">${esc(k)} = ${esc(v)}</span>`).join("")}</div>` : ""}
  <div class="tabs" id="tabs"></div><div id="tab"></div>`;
  document.getElementById("vs").onchange = e => { location.hash = "#/run/" + id + (e.target.value ? "?vs=" + e.target.value : ""); };
  const tabs = [["changes", "Configuration changes"], ["graphs", "Graphs"], ["inputs", "Diagnosis & inputs"], ["recs", "Recommendations"], ["config", "Full configuration"]];
  const tabsEl = document.getElementById("tabs"), tabEl = document.getElementById("tab");
  const show = async name => {
    tabsEl.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.t === name));
    clearTimers();
    if (name === "changes") renderDiff(tabEl, r);
    else if (name === "graphs") {
      tabEl.innerHTML = `<p class="muted">Shaded area = measurement window (±60 s shown around it).</p><div class="grid g3" id="rc"></div>`;
      const m = await api(`/api/runs/${id}/metrics?points=300`);
      chartsGrid(document.getElementById("rc"), m, RUN_CHARTS, m.window);
      if (r.status === "measuring") timers.push(setInterval(async () => { const m2 = await api(`/api/runs/${id}/metrics?points=300`); chartsGrid(document.getElementById("rc"), m2, RUN_CHARTS, m2.window); }, 15000));
    }
    else if (name === "inputs") renderInputs(tabEl, r);
    else if (name === "recs") { tabEl.innerHTML = ""; if (!r.recommendations.length) tabEl.innerHTML = '<p class="muted">No recommendation was generated from this run.</p>'; r.recommendations.forEach(rec => { const c = document.createElement("div"); c.className = "card"; c.style.marginBottom = "12px"; tabEl.appendChild(c); renderRecs(c, rec); }); }
    else if (name === "config") {
      const c = await api(`/api/runs/${id}/config`);
      const managed = c.managed || {};
      tabEl.innerHTML = `<div class="row"><input id="flt" placeholder="filter variables" style="max-width:300px"><span class="muted">${Object.keys(c.config).length} variables at measurement start; <b>bold</b> = managed by the agent</span></div><div class="card tablewrap" style="margin-top:10px"><table><tbody id="cfg"></tbody></table></div>`;
      const draw = () => {
        const q = document.getElementById("flt").value.toLowerCase();
        document.getElementById("cfg").innerHTML = Object.entries(c.config).filter(([k]) => k.includes(q)).map(([k, v]) => `<tr><td class="mono">${managed[k] != null ? "<b>" + esc(k) + "</b>" : esc(k)}</td><td class="mono" style="word-break:break-all">${esc(v)}</td></tr>`).join("");
      };
      document.getElementById("flt").oninput = draw; draw();
    }
    history.replaceState(null, "", "#/run/" + id + "/" + name + (vs ? "?vs=" + vs : ""));
  };
  tabs.forEach(([k, label]) => { const b = document.createElement("button"); b.textContent = label; b.dataset.t = k; b.onclick = () => show(k); tabsEl.appendChild(b); });
  show(tabs.some(t => t[0] === tab) ? tab : "changes");
}

function renderEvaluation(r) {
  const ev = r.evaluation || {};
  if (!ev.parent_runs) return r.verdict === "repeat" ? `<div class="note" style="margin-bottom:12px">Repeat of the previous configuration: averaged into its mean.</div>` : "";
  const rows = Object.entries(ev.efficiency || {}).map(([k, e]) => `<tr><td>${esc(EFF_LABELS[k] || e.label)}</td><td class="num">${num(e.parent)}</td><td class="num">${num(e.now)}</td><td class="num ${Math.abs(e.improvement_pct) > e.noise_pct ? (e.improvement_pct > 0 ? "up" : "down") : ""}">${e.improvement_pct > 0 ? "−" : "+"}${Math.abs(e.improvement_pct).toFixed(1)}%</td><td class="num muted">±${e.noise_pct}%</td></tr>`).join("");
  return `<div class="card" style="margin-bottom:14px"><h3 style="margin-top:0">Evaluation ${verdictBadge(r.verdict)}</h3>
   <p style="margin:0 0 8px">Goal vs the parent configuration (mean of runs ${ev.parent_runs.map(x => "#" + x).join(", ")} = ${num(ev.parent_goal_mean)}): <b class="${deltaCls(ev.goal_delta_pct)}">${pct(ev.goal_delta_pct)}</b>, noise threshold ±${ev.goal_noise_pct}%${ev.calibrated ? " (calibrated)" : " (not calibrated)"}.
   ${r.verdict === "efficiency-gain" ? " Within noise on the goal, but the server does measurably less work per transaction: keep." : r.verdict === "efficiency-loss" ? " Within noise on the goal, but measurably more work per transaction: revert." : ""}</p>
   ${rows ? `<div class="tablewrap"><table><thead><tr><th>Work per transaction</th><th class="num">Parent mean</th><th class="num">This run</th><th class="num">Change</th><th class="num">Noise</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}</div>`;
}

function renderDiff(el, r) {
  const prev = r.compared_with;
  let h = renderEvaluation(r);
  if (!prev) h += '<p class="muted">This is the first run of the environment: nothing to compare with. The full configuration is in the last tab.</p>';
  else if (!r.diff.length) h += `<p>No configuration differences with run #${prev.seq} <b>${esc(prev.label)}</b>.</p>`;
  else h += `<p class="muted">${r.diff.length} variable(s) differ from run #${prev.seq} ${esc(prev.label)}. Source and reasoning come from the agent; “outside the agent” means the value changed some other way (manual edit, server default after restart, automatic sizing).</p>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Run #${prev.seq}</th><th>This run</th><th>Source</th><th>Reason</th></tr></thead><tbody>${r.diff.map(d => `<tr><td class="mono">${esc(d.variable)}</td><td class="mono diff-old" style="word-break:break-all">${esc(val(d.variable, d.before))}</td><td class="mono diff-new" style="word-break:break-all">${esc(val(d.variable, d.after))}</td><td>${esc(d.source)}</td><td class="reason">${esc(d.reason || "")}</td></tr>`).join("")}</tbody></table></div>`;
  const managed = r.managed || {};
  if (Object.keys(managed).length) h += `<h3>Settings managed by the agent for this run</h3><pre><code>[mysqld]\n${Object.entries(managed).map(([k, v]) => esc(k) + " = " + esc(v)).join("\n")}</code></pre>`;
  el.innerHTML = h;
}

function renderInputs(el, r) {
  const inp = r.inputs || {};
  let h = "";
  h += "<h3>Diagnosis</h3>" + ((r.diagnosis || []).map(dg => `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}<div class="mono muted" style="font-size:11.5px;margin-top:3px">${esc(JSON.stringify(dg.evidence))}</div></div>`).join("") || '<p class="muted">none</p>');
  if (inp.top_statements && inp.top_statements.length) {
    h += `<h3>Top statements in the window</h3><p class="muted">From performance_schema digests and server-side prepared statements (marked <span class="badge plain">prepared</span>); the agent's own monitoring queries are excluded.</p><div class="card tablewrap"><table><thead><tr><th>Statement</th><th class="num">% time</th><th class="num">per s</th><th class="num">avg ms</th><th class="num">rows exam.</th><th class="num">rows sent</th><th class="num">no index</th><th class="num">tmp disk</th></tr></thead><tbody>` +
      inp.top_statements.map(q => `<tr><td class="mono" style="max-width:520px;word-break:break-all">${q.prepared ? '<span class="badge plain">prepared</span> ' : ""}${esc(q.query)}</td><td class="num">${q.pct_time}</td><td class="num">${num(q.per_s)}</td><td class="num">${num(q.avg_ms)}</td><td class="num">${num(q.rows_examined_avg)}</td><td class="num">${num(q.rows_sent_avg)}</td><td class="num">${q.no_index_used}</td><td class="num">${q.tmp_disk_tables}</td></tr>`).join("") + "</tbody></table></div>";
  }
  if (inp.top_waits && inp.top_waits.length) {
    h += `<h3>Top wait events in the window</h3><div class="card tablewrap"><table><thead><tr><th>Event</th><th class="num">count</th><th class="num">total s</th></tr></thead><tbody>` +
      inp.top_waits.map(w => `<tr><td class="mono">${esc(w.event)}</td><td class="num">${num(w.count)}</td><td class="num">${num(w.time_s)}</td></tr>`).join("") + "</tbody></table></div>";
  }
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

// ------------------------------------------------------------------ router
async function loadMe() { const d = await api("/api/me", { allow401: true }); ME = d.user; if (d.server) SERVER = d.server; }
async function route() {
  clearTimers();
  document.getElementById("tip").hidden = true;
  const h = location.hash.replace(/^#/, "").split("?")[0] || "/";
  const parts = h.split("/").filter(Boolean);
  try {
    if (parts[0] === "login") return authForm("login");
    if (parts[0] === "register") return authForm("register");
    if (!ME) return landing();
    if (!parts.length) return await envList();
    if (parts[0] === "connect") return await connectPage();
    if (parts[0] === "account") return await accountPage();
    if (parts[0] === "env") return await envPage(+parts[1]);
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
