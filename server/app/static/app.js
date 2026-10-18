/* AI DB Tuner single-page UI */
const $view = document.getElementById("view");
const C = window.Charts;
let ME = null, SERVER = location.origin, timers = [];

const WORKLOAD = { "oltp-write": "OLTP, write-heavy", "oltp-read": "OLTP, read-mostly", olap: "Analytical", mixed: "Mixed", idle: "Idle" };
const PHASE = { safe: "production-safe experiments", "trade-offs": "trade-off experiments", done: "no further experiments" };
function kvText(o) { return Object.entries(o || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "); }
function info(text) { return ` <span class="itip" tabindex="0" role="note" aria-label="${esc(text)}" data-tip="${esc(text)}">i</span>`; }
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
function goalName(g) { return g === "latency" ? "Query response time" : "Throughput (TPS)"; }
function goalUnit(g, s) { if (s && s.goal_metric) return s.goal_metric.length <= 5 ? s.goal_metric.toUpperCase() : s.goal_metric.replace(/_/g, " "); return g === "latency" ? "ms" : "tps"; }
function verdictBadge(v) {
  const m = { improved: "good", "efficiency-gain": "good", regressed: "bad", "efficiency-loss": "warn", neutral: "plain", reference: "info", repeat: "plain" };
  return v ? `<span class="badge ${m[v] || "plain"}">${esc(v)}</span>` : "";
}
function statusBadge(s, why) { const m = { active: "good", measuring: "info", done: "plain", closed: "plain", abandoned: "warn", aborted: "warn" }; return `<span class="badge ${m[s] || "plain"}"${why ? ` data-tip="${esc(why)}" tabindex="0"` : ""}>${esc(s)}</span>`; }
function sevBadge(s) { const m = { high: "bad", medium: "warn", low: "plain" }; return `<span class="badge ${m[s] || "plain"}">${esc(s)}</span>`; }
function riskBadge(r) { const m = { none: "good", low: "good", compat: "warn", observability: "warn", durability: "bad" }; return `<span class="badge ${m[r] || "plain"}">risk: ${esc(r)}</span>`; }
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function setNav(active) {
  const nav = document.getElementById("nav");
  if (!ME) { nav.innerHTML = `<a class="right" href="#/login">Log in</a><a href="#/register">Create account</a>`; return; }
  nav.innerHTML = `<a href="#/" class="${active === "envs" ? "on" : ""}">Environments</a><a href="#/connect" class="${active === "connect" ? "on" : ""}">Connect agent</a>` +
    `<a href="#/policies" class="${active === "policies" ? "on" : ""}">Policies</a><a href="#/account" class="${active === "account" ? "on" : ""}">Account</a><span class="right">${esc(ME.email)}</span><a href="#" id="logout">Log out</a>`;
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
  const envs = d.environments.slice().sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
  let mode = "list";
  try { mode = localStorage.getItem("envView") || "list"; } catch (e) { }
  $view.innerHTML = `<div class="row"><div><h1>Environments</h1><p class="sub">Database servers with a connected tuning-agent.</p></div><span class="spacer"></span>
    <div class="seg" role="group" aria-label="View"><button data-v="list">List</button><button data-v="cards">Cards</button></div><a class="btn" href="#/connect">Connect another</a></div><div id="envs"></div>`;
  const box = document.getElementById("envs");
  const metrics = e => { const l = (e.latest && e.latest.db) || {}, o = (e.latest && e.latest.os) || {};
    return { tps: e.online ? num(l.tps) : "–", lat: e.online && l.avg_latency_ms != null ? num(l.avg_latency_ms) + " ms" : "–", cpu: e.online && o.cpu_busy != null ? Math.round(o.cpu_busy) + "%" : "–" }; };
  const status = e => e.online ? '<span class="badge good">online</span>' : `<span class="badge plain">offline · ${ago(e.last_seen)}</span>`;
  const draw = () => {
    box.parentElement.querySelectorAll(".seg button").forEach(b => b.classList.toggle("on", b.dataset.v === mode));
    if (mode === "list") {
      box.innerHTML = `<div class="card tablewrap"><table><thead><tr><th>Name</th><th>Status</th><th>Database</th><th>Hardware</th><th class="num">Data</th><th class="num">TPS</th><th class="num">Avg latency</th><th class="num">CPU</th><th>Active session</th><th class="num">Sessions</th></tr></thead><tbody>${envs.map(e => {
        const m = metrics(e);
        return `<tr class="click" data-href="#/env/${e.id}"><td><a href="#/env/${e.id}"><b>${esc(e.name)}</b></a></td><td>${status(e)}</td><td>${esc(e.db_short || e.db)}</td><td>${e.cpus || "?"} CPU · ${bytes(e.ram_bytes)} · ${e.disk}</td><td class="num">${bytes(e.data_bytes)}</td>
          <td class="num">${m.tps}</td><td class="num">${m.lat}</td><td class="num">${m.cpu}</td><td>${e.active_session ? `<a href="#/session/${e.active_session.id}">#${e.active_session.id} ${esc(e.active_session.name)}</a>` : '<span class="muted">–</span>'}</td><td class="num">${e.sessions}</td></tr>`; }).join("")}</tbody></table></div>`;
      box.querySelectorAll("tr[data-href]").forEach(tr => tr.onclick = ev => { if (!ev.target.closest("a")) location.hash = tr.dataset.href; });
    } else {
      box.innerHTML = `<div class="grid g3">${envs.map(e => { const m = metrics(e);
        return `<a class="card" href="#/env/${e.id}" style="color:inherit;text-decoration:none"><div class="row"><b style="font-size:15px">${esc(e.name)}</b><span class="spacer"></span>${status(e)}</div>
          <div class="muted" style="margin:4px 0 10px">${esc(e.db_short || e.db)}<br>${e.cpus || "?"} CPUs · ${bytes(e.ram_bytes)} RAM · ${e.disk} · data ${bytes(e.data_bytes)}</div>
          <div class="row" style="gap:18px"><div><div class="muted" style="font-size:12px">TPS</div><b>${m.tps}</b></div><div><div class="muted" style="font-size:12px">Avg latency</div><b>${m.lat}</b></div>
          <div><div class="muted" style="font-size:12px">CPU busy</div><b>${m.cpu}</b></div><div><div class="muted" style="font-size:12px">Sessions</div><b>${e.sessions}</b></div></div>
          ${e.active_session ? `<div style="margin-top:10px"><span class="badge info">active: ${esc(e.active_session.name)}</span></div>` : ""}</a>`; }).join("")}</div>`;
    }
  };
  box.parentElement.querySelectorAll(".seg button").forEach(b => b.onclick = () => { mode = b.dataset.v; try { localStorage.setItem("envView", mode); } catch (e) { } draw(); });
  draw();
  timers.push(setInterval(async () => {
    if (!document.getElementById("envs")) return;
    const d2 = await api("/api/environments"); const byId = Object.fromEntries(d2.environments.map(e => [e.id, e]));
    envs.forEach((e, i) => { if (byId[e.id]) envs[i] = byId[e.id]; }); draw();
  }, 15000));
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
  $view.innerHTML = `<h1>Account</h1><p class="sub">${esc(ME.email)}</p><div class="card form" style="margin:0;max-width:420px"><h3>Change password</h3>
  <form id="f"><label>Current password</label><input type="password" name="old" required><label>New password</label><input type="password" name="new" required minlength="8">
  <div class="err" id="err"></div><button class="primary">Change password</button></form></div>`;
  document.getElementById("f").onsubmit = async e => {
    e.preventDefault();
    try { await api("/api/auth/password", { method: "POST", body: Object.fromEntries(new FormData(e.target)) }); document.getElementById("err").textContent = "Password changed."; }
    catch (err) { document.getElementById("err").textContent = err.message; }
  };
}

function chartsGrid(container, m, defs, window_, markers) {
  container.innerHTML = "";
  defs.forEach(def => {
    const cell = document.createElement("div"); cell.className = "card";
    container.appendChild(cell);
    C.lineChart(cell, {
      title: def.title, unit: def.unit, ts: m.ts, window: window_, max100: def.max100, min0: def.min0, markers,
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
  <p class="sub">${esc(e.db_short || e.db)} · ${esc(hw.hostname || "")} · agent ${esc(e.agent_version || "")}</p>
  <div class="card"><div class="row" style="gap:6px 18px">
    <span><b>${hw.cpus} vCPU</b> <span class="muted">${esc((hw.cpu_model || "").replace(/\s+\d+-Core Processor/, "").replace(/\(R\)|\(TM\)/g, ""))}</span></span>
    <span><b>${bytes(hw.ram_bytes)}</b> RAM</span>
    <span>${hw.disk_rotational ? "HDD" : "SSD"} ${esc(hw.filesystem || "")} · ${bytes(hw.disk_free_bytes)} free</span>
    <span>${esc((hw.os || "").replace(/ LTS$/, ""))}</span>
    <span><b>${esc(e.db_short || e.db)}</b></span>
    <span>data ${bytes(db.data_bytes)}</span>
    <span>buffer pool ${bytes(kv.innodb_buffer_pool_size)}</span>
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
  <div class="card" style="margin-top:10px"><details><summary style="margin:0">Current agent-applied configuration (${Object.keys((f.agent || {}).managed || {}).length} settings)${info("The settings the tuning-agent currently enforces on this server: MySQL variables written to its own option file (shown below) and operating-system settings. Everything not listed is the server default or comes from your own option files. As of the agent's last report.")}</summary>${Object.keys((f.agent || {}).managed || {}).length ? `<p class="muted" style="margin:0 0 6px">${esc((f.agent || {}).cnf_path || "")} · restarts ${(f.agent || {}).allow_restart === false ? "disabled" : "allowed"}</p><pre><code>${cnfBlock(f.agent.managed)}</code></pre>` : '<span class="muted">None yet: <code>tuning-agent baseline --apply</code> writes the first set.</span>'}</details></div>
  <div class="row"><h2>Live metrics</h2><span class="spacer"></span><select id="range" style="width:auto"><option value="900">15 min</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">24 hours</option></select></div>
  <div class="grid g3" id="live"></div>
  <h2>Tuning sessions</h2><div class="card tablewrap"><table><thead><tr><th>#</th><th>Session</th><th>Goal</th><th>Policy</th><th>Status</th><th class="num">Runs</th><th class="num">First run</th><th class="num">Best</th><th class="num">Best gain</th><th>Started</th></tr></thead><tbody id="sess"></tbody></table></div>
  <h2>Recent activity</h2><div class="card" id="events"></div>`;
  const tb = document.getElementById("sess");
  if (!d.sessions.length) tb.innerHTML = `<tr><td colspan="10" class="muted">No sessions yet. Start one with <code>tuning-agent session start --goal tps</code>.</td></tr>`;
  d.sessions.forEach(s => {
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/session/" + s.id;
    tr.innerHTML = `<td class="muted">${s.id}</td><td><a href="#/session/${s.id}">${esc(s.name)}</a></td><td>${goalName(s.goal)}</td><td>${esc(s.policy)}</td><td>${statusBadge(s.status)}</td><td class="num">${s.runs}</td>
     <td class="num">${num(s.first_value)}</td><td class="num">${num(s.best_value)}</td><td class="num ${deltaCls(s.best_gain_pct)}">${pct(s.best_gain_pct)}</td><td>${fmtTime(s.created)}</td>`;
    tb.appendChild(tr);
  });
  renderEvents(document.getElementById("events"), d.events);
  document.getElementById("rename").onclick = async () => { const n = prompt("Environment name", e.name); if (n) { await api("/api/environments/" + id, { method: "PATCH", body: { name: n } }); envPage(id); } };
  document.getElementById("detailsToggle").onclick = ev => { ev.preventDefault(); const d = document.getElementById("envDetails"); d.hidden = !d.hidden; ev.target.textContent = d.hidden ? "Details ▸" : "Details ▾"; };
  document.getElementById("del").onclick = async () => { if (confirm("Delete this environment with all sessions, runs and metrics? The agent will re-register it on its next report.")) { await api("/api/environments/" + id, { method: "DELETE" }); location.hash = "#/"; } };
  const sessionMarkers = [];
  d.sessions.forEach(x => { sessionMarkers.push({ t: x.created, label: `#${x.id} start` }); if (x.ended) sessionMarkers.push({ t: x.ended, label: `#${x.id} end`, end: true }); });
  const live = document.getElementById("live"), range = document.getElementById("range");
  const load = async () => { const span = +range.value; const now = Date.now() / 1000; const m = await api(`/api/environments/${id}/metrics?start=${now - span}&end=${now}&points=240`); chartsGrid(live, m, [...LIVE_CHARTS, LIVE_PSI, LIVE_THREADS], null, sessionMarkers); };
  range.onchange = load;
  await load();
  timers.push(setInterval(load, 15000));
}

function renderEvents(el, events, all) {
  if (!events.length) { el.innerHTML = '<span class="muted">No activity yet.</span>'; return; }
  const shown = all ? events : events.slice(0, 10);
  el.innerHTML = (events.length > 10 ? `<div class="muted" style="font-size:12.5px;margin-bottom:4px">${all ? `All ${events.length} events` : `Latest 10 of ${events.length}`} · <a href="#" class="evtoggle">${all ? "Show latest only" : "Show all"}</a></div>` : "") + shown.map(ev => `<div class="ev"><span class="muted">${fmtTime(ev.ts)}</span><span><span class="badge plain">${esc(ev.type)}</span></span><span>${esc(ev.message)}</span></div>`).join("");
  const t = el.querySelector(".evtoggle"); if (t) t.onclick = e => { e.preventDefault(); renderEvents(el, events, !all); };
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
  // within the noise threshold the leader is not displaced: prefer kept configurations, then more runs
  const noiseP = (s.calibration && s.calibration.noise_pct) || s.noise_pct || 2;
  const KEEP = ["improved", "efficiency-gain", "reference"];
  if (cfgs.length) {
    const top = cfgs[0].mean;
    const tied = cfgs.filter(c => Math.abs(c.mean - top) / Math.abs(top) * 100 <= noiseP);
    tied.forEach(c => c.kept = c.runs.some(r => KEEP.includes(r.verdict)));
    tied.sort((a, b) => (b.kept - a.kept) || (b.runs.length - a.runs.length) || (lower ? a.mean - b.mean : b.mean - a.mean));
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
  ${calibrationNote(s)}
  <div class="tiles"><div class="tile"><div class="k">Runs measured</div><div class="v">${done.length}</div></div>
   <div class="tile"><div class="k">First run (${goalUnit(s.goal, s)})</div><div class="v">${first ? num(first.goal_value) : "–"}</div><div class="d">${first ? esc(first.label) : ""}</div></div>
   ${cfgTile("Best production-safe configuration", bestSafe, firstCfg, lower, s.goal, false, s)}
   ${bestTrade ? cfgTile("Best with trade-offs", bestTrade, firstCfg, lower, s.goal, true, s) : ""}</div>
  <div class="card" style="margin-top:14px" id="bars"></div>
  <h2>Runs</h2><div class="card tablewrap"><table><thead><tr><th>#</th><th>Label</th><th>Status</th><th>Changes vs previous run</th><th class="num">${goalUnit(s.goal, s) === "ms" ? "Avg ms" : "Goal"}</th><th class="num">vs prev</th><th class="num">vs first</th><th>Verdict</th>
   <th class="num">vs parent cfg</th><th class="num">TPS</th><th class="num">Avg lat ms</th><th class="num">mysqld CPU ms/trx</th><th class="num">Disk IOs/trx</th><th class="num">CPU %</th><th>Duration</th></tr></thead><tbody id="runs"></tbody></table></div>
  <h2>Latest recommendations</h2><div class="card" id="recs"></div>
  <h2>Notes</h2><div class="card"><textarea id="notes" rows="3" placeholder="Notes about this session (workload, hypotheses, conclusions)">${esc(s.notes || "")}</textarea><div class="row" style="margin-top:6px"><button id="savenotes">Save notes</button><span id="saved" class="muted"></span></div></div>
  <h2>Activity</h2><div class="card" id="events"></div>`;
  C.barChart(document.getElementById("bars"), {
    title: goalName(s.goal) + " per run (" + goalUnit(s.goal, s) + ") — green: best production-safe configuration · orange: runs with trade-offs", unit: goalUnit(s.goal, s), metric: s.goal === "latency" ? "avg latency" : "goal value",
    ref: first ? first.goal_value : null,
    bars: done.map(r => ({ label: "#" + r.seq + " " + r.label, value: r.goal_value, sub: r.delta_vs_first_pct != null ? pct(r.delta_vs_first_pct) : "", href: "#/run/" + r.id,
      highlight: (r.tradeoffs || []).length ? "trade" : bestSafe && bestSafe.runs.includes(r) ? "best" : false,
      tip: `<div>vs previous<b>${pct(r.delta_pct)}</b></div><div>verdict<b>${esc(r.verdict || "")}</b></div>` })),
  });
  const tb = document.getElementById("runs");
  if (!d.runs.length) tb.innerHTML = `<tr><td colspan="15" class="muted">No runs yet: <code>tuning-agent measure start</code> … <code>measure stop</code>.</td></tr>`;
  d.runs.forEach(r => {
    const sm = r.summary || {};
    const ch = r.status === "aborted" ? `<span class="muted">${esc((r.abort_reason || "aborted").split(":")[0])}</span>${r.abort_reason ? info(r.abort_reason) : ""}` : r.seq === 1 ? `<a href="#/run/${r.id}">starting configuration</a>` : (r.changes || []).map(c => `<span class="mono">${esc(c.variable)}</span>=${esc(val(c.variable, c.new))}`).join(", ");
    const tr = document.createElement("tr"); tr.className = "click"; tr.onclick = () => location.hash = "#/run/" + r.id;
    tr.innerHTML = `<td>${r.seq}</td><td><a href="#/run/${r.id}">${esc(r.label)}</a>${tradeBadge(r.tradeoffs)}</td><td>${statusBadge(r.status, r.abort_reason)}</td><td style="max-width:320px">${ch || '<span class="muted">none</span>'}</td>
     <td class="num"><b>${num(r.goal_value)}</b></td><td class="num ${deltaCls(r.delta_pct)}">${pct(r.delta_pct)}</td><td class="num ${deltaCls(r.delta_vs_first_pct)}">${pct(r.delta_vs_first_pct)}</td><td>${verdictBadge(r.verdict)}${(r.evaluation || {}).drift ? ' <span class="badge warn">drift</span>' : ""}</td>
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

function cfgTile(title, cfg, firstCfg, lower, goal, trade, sess) {
  if (!cfg) return "";
  const g = firstCfg && firstCfg.mean ? (lower ? (firstCfg.mean - cfg.mean) : (cfg.mean - firstCfg.mean)) / firstCfg.mean * 100 : null;
  return `<div class="tile"><div class="k">${title} (${goalUnit(goal, sess)})</div><div class="v">${num(cfg.mean)} <span class="${deltaCls(g)}" style="font-size:14px">${pct(g)}</span></div>
   <div class="d">${cfg.runs.length > 1 ? "mean of runs " : "run "}${cfg.runs.map(r => "#" + r.seq).join(", ")} · vs first configuration${trade ? `<br>${esc((cfg.runs[0].tradeoffs || []).map(t => t.variable + "=" + t.value).join(", "))}` : ""}</div></div>`;
}

function tradeBadge(t) {
  if (!t || !t.length) return "";
  return ` <span class="badge bad" title="${esc(t.map(x => x.variable + "=" + x.value + ": " + x.lost).join("\n"))}">trade-off</span>`;
}

function calibrationNote(s) {
  const c = s.calibration;
  if (!c) return `<div class="note" style="margin-bottom:12px">Noise not calibrated: fixed ±${s.noise_pct || 2}% threshold${info("Standard protocol: measure the starting configuration 3 times with `tuning-agent calibrate --runs 3`; the run-to-run variation then sets the noise threshold.")}</div>`;
  const eff = Object.entries(c.efficiency || {}).filter(([k]) => EFF_LABELS[k]).map(([k, e]) => `${EFF_LABELS[k]} ±${e.noise_pct}%`).join(" · ");
  const how = `Calibration runs ${c.runs.map(x => "#" + x).join(", ")}: mean ${num(c.goal_mean)}, CV ${c.goal_cv_pct}%, range ${c.goal_range_pct}%. CV (coefficient of variation) = standard deviation ÷ mean. Threshold = ${c.k || 2.5} × CV, never below the floor (goal 2%, efficiency 3%): a run of an unchanged configuration lands outside it only about 1 time in 100. Experiments are compared with the mean of the most recent runs of the configuration they were changed from. Efficiency thresholds: ${eff}.`;
  return `<div class="note" style="margin-bottom:12px">Noise: changes within <b>±${c.noise_pct}%</b> are treated as noise (CV ${c.goal_cv_pct}% over ${c.runs.length} runs)${info(how)}</div>`;
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
function cnfBlock(managed) {
  const e = Object.entries(managed || {}), my = e.filter(([k]) => !k.startsWith("os:")), os = e.filter(([k]) => k.startsWith("os:"));
  return (my.length ? `[mysqld]\n${my.map(([k, v]) => esc(k) + (v === "" ? "" : " = " + esc(v))).join("\n")}` : "") +
    (os.length ? `${my.length ? "\n\n" : ""}# operating system\n${os.map(([k, v]) => esc(k.slice(3)) + " = " + esc(v)).join("\n")}` : "");
}
async function renderKeySettings(el, runId) {
  const c = await api(`/api/runs/${runId}/config`);
  const meta = c.meta || {};
  if (!Object.keys(meta).length) { el.innerHTML = ""; return; }
  const rows = KEY_VARS.filter(k => k in c.config).map(k => {
    const m = meta[k] || {}, def = isDefault(m);
    return `<tr><td class="mono">${esc(k)}</td><td class="mono ${def ? "" : "diff-new"}">${esc(val(k, c.config[k]))}</td><td class="mono muted">${m.default != null ? esc(val(k, m.default)) : ""}</td><td>${def ? '<span class="muted">compiled default</span>' : esc(sourceText(m))}</td></tr>`;
  }).join("");
  const nonDef = Object.entries(meta).filter(([k, m]) => !isDefault(m) && !KEY_VARS.includes(k)).length;
  el.innerHTML = `<h3>Key settings of this run${info(`Value in effect, the server's compiled default and where the value comes from (option files, SET PERSIST, runtime changes). Bold = not the compiled default. ${nonDef} more non-default variables are in the Full configuration tab.`)}</h3>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Value</th><th>Compiled default</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

const EFF_LABELS = { mysqld_cpu_ms_per_trx: "mysqld CPU ms/trx", host_cpu_ms_per_trx: "host CPU ms/trx", disk_ios_per_trx: "disk IOs/trx", disk_kb_per_trx: "disk KiB/trx", fsyncs_per_trx: "fsyncs/trx", redo_kb_per_trx: "redo KiB/trx", rows_read_per_trx: "rows read/trx" };

function renderRecs(el, rec, runs) {
  if (!rec) { el.innerHTML = '<span class="muted">No recommendations yet: run <code>tuning-agent recommend</code> after a measured run.</span>'; return; }
  const run = runs && runs.find(r => r.id === rec.run_id);
  let h = `<div class="muted" style="margin-bottom:6px">Generated ${fmtTime(rec.created)}${run ? ` from run #${run.seq} <a href="#/run/${run.id}">${esc(run.label)}</a>` : " without a measured run"} · goal ${esc(rec.goal)} · policy ${esc(rec.policy)}${rec.next ? ` · suggested next: <code>${esc(rec.next)}</code>` : ""}</div>`;
  (rec.diagnosis || []).forEach(dg => { h += `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}</div>`; });
  if (rec.workload && rec.workload.type) h += `<p style="margin:4px 0 8px">Workload: <b>${esc(WORKLOAD[rec.workload.type] || rec.workload.type)}</b>${info(kvText(rec.workload.evidence))}${rec.phase ? ` · Phase: <b>${esc(PHASE[rec.phase] || rec.phase)}</b>` : ""}</p>`;
  const items = rec.items || [];
  const safe = items.filter(i => (i.safety || "safe") !== "trade-off"), trade = items.filter(i => i.safety === "trade-off");
  const section = (title, list, note) => list.length ? `<h3>${title}${note ? info(note) : ""}</h3>` + list.map(recItem).join("") : "";
  h += section("Production-safe", safe) + section("Trade-offs (benchmark only)", trade, "These give up durability, replication, observability or semantics. Results with them are not production-equivalent; they are reported separately.");
  el.innerHTML = h;
  return;
  function recItem(it) {
    return `<div class="rec"><div class="h"><b>${it.rank}. ${esc(it.title)}</b><span class="id">${esc(it.id)}</span>${it.kind === "advice" ? '<span class="badge plain">advice only</span>' : it.kind === "revert" ? '<span class="badge warn">revert</span>' : ""}
     ${riskBadge(it.risk)}${it.restart ? '<span class="badge plain">restart</span>' : ""}<span class="badge plain">impact ${it.impact}/3</span><span class="badge plain">confidence ${it.confidence}/3</span>${it.id === rec.next ? '<span class="badge info">next</span>' : ""}</div>
     ${it.changes.length ? `<div class="mono" style="margin:4px 0">${it.changes.map(c => `${esc(c.variable)}: <span class="diff-old">${esc(val(c.variable, c.current))}</span> → <span class="diff-new">${c.unset ? "(server default)" : esc(val(c.variable, c.proposed))}</span>`).join("<br>")}</div>` : ""}
     <div class="reason">${esc(it.rationale)}${it.note ? " <b>" + esc(it.note) + "</b>" : ""}</div>
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
   ${tile("CPU busy (host)", sm.cpu_busy, "%", ps.cpu_busy, 0)}${tile("mysqld CPU", sm.mysqld_cpu_pct, "% of host", ps.mysqld_cpu_pct, 0)}${tile("IO wait", sm.cpu_iowait, "%", ps.cpu_iowait, -1, "x")}${tile("BP hit ratio", sm.bp_hit_pct, "%", ps.bp_hit_pct, 1)}${tile("Disk IOPS", (sm.disk_r_iops || 0) + (sm.disk_w_iops || 0), "/s", ps.disk_r_iops != null ? (ps.disk_r_iops || 0) + (ps.disk_w_iops || 0) : null, 0)}
   ${tile("CPU pressure", sm.mysqld_psi_cpu_some != null ? sm.mysqld_psi_cpu_some : sm.psi_cpu_some, "% stalled", ps.mysqld_psi_cpu_some != null ? ps.mysqld_psi_cpu_some : ps.psi_cpu_some, -1, "x")}${tile("I/O pressure", sm.mysqld_psi_io_some != null ? sm.mysqld_psi_io_some : sm.psi_io_some, "% stalled", ps.mysqld_psi_io_some != null ? ps.mysqld_psi_io_some : ps.psi_io_some, -1, "x")}${tile("Memory pressure", sm.psi_memory_some, "% stalled", ps.psi_memory_some, -1, "x")}
   ${tile("mysqld CPU / trx", sm.mysqld_cpu_ms_per_trx, "ms", ps.mysqld_cpu_ms_per_trx, -1, "mysqld_cpu_ms_per_trx")}${tile("Disk IOs / trx", sm.disk_ios_per_trx, "", ps.disk_ios_per_trx, -1, "disk_ios_per_trx")}
   ${tile("Redo written", sm.redo_mbs, "MiB/s", ps.redo_mbs, 0)}${tile("Ckpt age max", sm.checkpoint_age_pct_max, "%", ps.checkpoint_age_pct_max, -1, "x")}${tile("Threads running", sm.threads_running_avg, "avg", ps.threads_running_avg, 0)}${tile("TPS variation", sm.tps_cv_pct, "% CV", ps.tps_cv_pct, -1, "x")}</div>
  ${Object.keys(r.external || {}).length ? `<h3>Benchmark-reported values</h3><div class="row">${Object.entries(r.external).map(([k, v]) => `<span class="badge plain">${esc(k)} = ${esc(v)}</span>`).join("")}</div>` : ""}
  <div class="tabs" id="tabs"></div><div id="tab"></div>`;
  document.getElementById("vs").onchange = e => { location.hash = "#/run/" + id + (e.target.value ? "?vs=" + e.target.value : ""); };
  const tabs = [["changes", "Configuration"], ["graphs", "Graphs"], ["inputs", "Diagnosis & inputs"], ["recs", "Recommendations"], ["config", "Full configuration"]];
  const tabsEl = document.getElementById("tabs"), tabEl = document.getElementById("tab");
  const show = async name => {
    tabsEl.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.t === name));
    clearTimers();
    if (name === "changes") { renderDiff(tabEl, r); const ks = document.createElement("div"); tabEl.appendChild(ks); renderKeySettings(ks, id); }
    else if (name === "graphs") {
      tabEl.innerHTML = `<p class="muted">Shaded = measurement window${info("Charts show the measurement window plus 60 s before and after it.")}</p><div class="grid g3" id="rc"></div>`;
      const m = await api(`/api/runs/${id}/metrics?points=300`);
      chartsGrid(document.getElementById("rc"), m, RUN_CHARTS, m.window);
      if (r.status === "measuring") timers.push(setInterval(async () => { const m2 = await api(`/api/runs/${id}/metrics?points=300`); chartsGrid(document.getElementById("rc"), m2, RUN_CHARTS, m2.window); }, 15000));
    }
    else if (name === "inputs") renderInputs(tabEl, r);
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

function renderEvaluation(r) {
  const ev = r.evaluation || {};
  if (ev.same_config_runs) return `<div class="note" style="margin-bottom:12px">${ev.drift ? '<span class="badge warn">drift</span> ' : ""}Repeat of runs ${ev.same_config_runs.map(x => "#" + x).join(", ")}: <b>${pct(ev.deviation_pct)}</b> vs their mean${info((ev.drift ? ev.note + " " : "") + `Same configuration measured again (mean of earlier runs ${num(ev.same_config_mean)}, noise ±${ev.goal_noise_pct}%). Repeats are averaged into that configuration's mean.`)}</div>`;
  if (!ev.parent_runs) return "";
  const rows = Object.entries(ev.efficiency || {}).map(([k, e]) => `<tr><td>${esc(EFF_LABELS[k] || e.label)}</td><td class="num">${num(e.parent)}</td><td class="num">${num(e.now)}</td><td class="num ${Math.abs(e.improvement_pct) > e.noise_pct ? (e.improvement_pct > 0 ? "up" : "down") : ""}">${e.improvement_pct > 0 ? "−" : "+"}${Math.abs(e.improvement_pct).toFixed(1)}%</td><td class="num muted">±${e.noise_pct}%</td></tr>`).join("");
  return `<div class="card" style="margin-bottom:14px"><h3 style="margin-top:0">Evaluation ${verdictBadge(r.verdict)}</h3>
   <p style="margin:0 0 8px">Goal <b class="${deltaCls(ev.goal_delta_pct)}">${pct(ev.goal_delta_pct)}</b> vs parent configuration (±${ev.goal_noise_pct}% noise)${info(`Parent configuration = the one this run was changed from; compared with the mean of its most recent runs (${ev.parent_runs.map(x => "#" + x).join(", ")} = ${num(ev.parent_goal_mean)}). Noise threshold ${ev.calibrated ? "calibrated" : "not calibrated"}. improved/regressed: goal beyond noise. efficiency-gain/-loss: goal within noise but CPU or I/O per transaction measurably lower/higher (only resources under load count).`)}</p>
   ${rows ? `<div class="tablewrap"><table><thead><tr><th>Work per transaction</th><th class="num">Parent mean</th><th class="num">This run</th><th class="num">Change</th><th class="num">Noise</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}</div>`;
}

function renderDiff(el, r) {
  const prev = r.compared_with;
  let h = renderEvaluation(r);
  if (r.seq === 1) h += `<p class="muted">Starting configuration of the session${info("The first run measures the configuration the session started with. Key settings below show compiled defaults and the source of each value." + (prev ? ` Differences from the last run before this session (run #${prev.seq}) are listed for reference.` : ""))}</p>`;
  if (!prev) {}
  else if (!r.diff.length && r.seq !== 1) h += `<p>No configuration differences with run #${prev.seq} <b>${esc(prev.label)}</b>.</p>`;
  else if (r.diff.length) h += `<h3>Changed since run #${prev.seq} ${esc(prev.label)}${info("Source and reasoning come from the agent. “outside the agent” means the value changed some other way: manual edit, server default after restart, automatic sizing.")}</h3>
   <div class="card tablewrap"><table><thead><tr><th>Variable</th><th>Run #${prev.seq}</th><th>This run</th><th>Source</th><th>Reason</th></tr></thead><tbody>${r.diff.map(d => `<tr><td class="mono">${esc(d.variable)}</td><td class="mono diff-old" style="word-break:break-all">${esc(val(d.variable, d.before))}</td><td class="mono diff-new" style="word-break:break-all">${esc(val(d.variable, d.after))}</td><td>${esc(d.source)}</td><td class="reason">${esc(d.reason || "")}</td></tr>`).join("")}</tbody></table></div>`;
  const managed = r.managed || {};
  if (Object.keys(managed).length) h += `<h3>Agent-applied configuration during this run${info("The settings the tuning-agent enforced while this run was measured: its MySQL option file plus operating-system settings. Everything else was the server default or came from other option files (see Key settings).")}</h3><pre><code>${cnfBlock(managed)}</code></pre>`;
  el.innerHTML = h;
}

function renderInputs(el, r) {
  const inp = r.inputs || {};
  let h = "";
  if (inp.workload && inp.workload.type) h += `<p>Workload: <b>${esc(WORKLOAD[inp.workload.type] || inp.workload.type)}</b>${info(kvText(inp.workload.evidence))}</p>`;
  if (inp.bench) {
    const b = inp.bench;
    h += `<h3>Benchmark: ${esc(b.name)}</h3><div class="kv"><div>Run command</div><div class="mono">${esc(b.run)}</div>${b.reset ? `<div>Reset</div><div class="mono">${esc(b.reset)}</div>` : ""}<div>Warm-up / measured</div><div>${esc(b.warmup)} s / ${b.duration ? esc(b.duration) + " s" : "until finished"}</div><div>Exit code</div><div>${esc(b.exit_code)}</div>
      ${Object.entries(b.metrics || {}).map(([k, v]) => `<div class="mono">${esc(k)}</div><div class="mono"><b>${esc(v)}</b></div>`).join("")}</div><details><summary>Benchmark output (tail)</summary><pre>${esc(b.log_tail || "")}</pre></details>`;
  }
  h += "<h3>Diagnosis</h3>" + ((r.diagnosis || []).map(dg => `<div class="diag">${sevBadge(dg.severity)} <b>${esc(dg.type)}</b> — ${esc(dg.text)}<div class="mono muted" style="font-size:11.5px;margin-top:3px">${esc(JSON.stringify(dg.evidence))}</div></div>`).join("") || '<p class="muted">none</p>');
  if (inp.top_statements && inp.top_statements.length) {
    h += `<h3>Top statements in the window${info("From performance_schema digests and server-side prepared statements (marked prepared). The agent's own monitoring queries are excluded.")}</h3><div class="card tablewrap"><table><thead><tr><th>Statement</th><th class="num">% time</th><th class="num">per s</th><th class="num">avg ms</th><th class="num">rows exam.</th><th class="num">rows sent</th><th class="num">no index</th><th class="num">tmp disk</th></tr></thead><tbody>` +
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
    if (parts[0] === "policies") return await policiesPage();
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
