/* Minimal SVG charts: time-series lines with crosshair tooltip, and per-run bars. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const SLOTS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
  // more series than colours: the colours repeat with a dashed line
  const slot = i => SLOTS[i % SLOTS.length], dash = i => i >= SLOTS.length ? "5 3" : null;
  const tip = () => document.getElementById("tip");
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function niceTicks(min, max, n) {
    if (!isFinite(min) || !isFinite(max)) return [0, 1];
    if (min === max) { max = min + 1; }
    const span = max - min, step0 = span / Math.max(n, 1);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step, out = [];
    for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function fmtNum(v) {
    if (v == null || !isFinite(v)) return "–";
    if (v === 0) return "0";
    const a = Math.abs(v);
    if (Number.isInteger(v) && a < 1e4) return String(v);
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  }
  function fmtTick(v, ticks) {
    const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
    if (step >= 1 || v === 0) return fmtNum(v);
    const dec = Math.min(6, Math.max(0, -Math.floor(Math.log10(step)) + (step * Math.pow(10, -Math.floor(Math.log10(step))) % 1 ? 1 : 0)));
    return v.toFixed(dec);
  }
  function hhmm(t, withSec) {
    const d = new Date(t * 1000);
    const p = x => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + (withSec ? ":" + p(d.getSeconds()) : "");
  }
  function showTip(html, x, y) {
    const t = tip(); t.innerHTML = html; t.hidden = false;
    const w = t.offsetWidth, h = t.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + w > window.innerWidth - 8) left = x - w - 14;
    if (top + h > window.innerHeight - 8) top = y - h - 14;
    t.style.left = Math.max(8, left) + "px"; t.style.top = Math.max(8, top) + "px";
  }
  function hideTip() { tip().hidden = true; }

  /* opts: {title, ts:[], series:[{name, values, unit}], window:[t0,t1], height, unit, min0} */
  // linked cross-hair: every time-series chart on the page follows the one under the pointer (same moment in time)
  let linked = [];
  function syncAll(src, t) {
    linked = linked.filter(c => c.svg.isConnected);
    linked.forEach(c => { if (c !== src) (t == null ? c.hide() : c.show(t)); });
  }

  function lineChart(container, opts) {
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const series = opts.series.filter(s => s.values && s.values.some(v => v != null));
    const title = document.createElement("div");
    title.className = "title"; title.textContent = opts.title + (opts.unit ? " (" + opts.unit + ")" : "");
    wrap.appendChild(title);
    if (series.length > 1) {
      const lg = document.createElement("div"); lg.className = "legend";
      series.forEach((s, i) => { const sp = document.createElement("span"); sp.innerHTML = dash(i) ? `<i class="dash" style="border-color:${slot(i)}"></i>` : `<i style="background:${slot(i)}"></i>`; sp.appendChild(document.createTextNode(s.name)); lg.appendChild(sp); });
      wrap.appendChild(lg);
    }
    container.appendChild(wrap);
    const ts = opts.ts || [];
    if (!series.length || ts.length < 2) {
      const p = document.createElement("div"); p.className = "muted"; p.style.cssText = "height:" + (opts.height || 150) + "px;display:flex;align-items:center;justify-content:center;font-size:12px";
      p.textContent = "no data"; wrap.appendChild(p); return;
    }
    const W = Math.max(wrap.clientWidth || 300, 240), H = opts.height || 150;
    const m = { l: 44, r: 8, t: 6, b: 20 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.title });
    wrap.appendChild(svg);
    const x0 = ts[0], x1 = ts[ts.length - 1];
    let ymin = Infinity, ymax = -Infinity;
    series.forEach(s => s.values.forEach(v => { if (v != null) { ymin = Math.min(ymin, v); ymax = Math.max(ymax, v); } }));
    if (opts.min0 !== false) ymin = Math.min(0, ymin);
    if (opts.max100) ymax = Math.max(ymax, 100);
    const ticks = niceTicks(ymin, ymax, 4);
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const X = t => m.l + (W - m.l - m.r) * (t - x0) / Math.max(x1 - x0, 1);
    const Y = v => m.t + (H - m.t - m.b) * (1 - (v - lo) / Math.max(hi - lo, 1e-9));
    // session start / end markers: a label only where it does not run into the previous one, never past the plot's edge
    let lastLab = -Infinity;
    (opts.markers || []).filter(mk => mk.t >= x0 && mk.t <= x1).sort((a, b) => a.t - b.t).forEach(mk => {
      const x = X(mk.t);
      el("line", { x1: x, x2: x, y1: m.t, y2: H - m.b, stroke: "var(--ink-2)", "stroke-dasharray": "3 3", "stroke-width": 1, opacity: 0.7 }, svg);
      if (x - lastLab < 48) return;
      const right = mk.end || x > W - m.r - 60;
      const lab = el("text", { x: x + (right ? -3 : 3), y: m.t + 9, "text-anchor": right ? "end" : "start", "font-size": 10, fill: "var(--ink-2)" }, svg);
      lab.textContent = mk.label; lastLab = x;
    });
    if (opts.warmup && opts.warmup[0]) {  // warm-up before the measured window: its own (amber) band
      const a = X(Math.max(opts.warmup[0], x0)), b = X(Math.min(opts.warmup[1] || x1, x1));
      if (b > a) el("rect", { x: a, y: m.t, width: b - a, height: H - m.t - m.b, fill: "var(--warmup)" }, svg);
    }
    if (opts.window && opts.window[0]) {
      const a = X(Math.max(opts.window[0], x0)), b = X(Math.min(opts.window[1] || x1, x1));
      if (b > a) el("rect", { x: a, y: m.t, width: b - a, height: H - m.t - m.b, fill: "var(--window)" }, svg);
    }
    if (opts.startup && opts.startup[0]) {  // the benchmark's start-up inside the window: left out of the analyses (grey band)
      const a = X(Math.max(opts.startup[0], x0)), b = X(Math.min(opts.startup[1], x1));
      if (b > a) el("rect", { x: a, y: m.t, width: Math.max(b - a, 2), height: H - m.t - m.b, fill: "var(--startup)" }, svg);
    }
    ticks.forEach(v => {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === lo ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, svg);
      const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, svg);
      t.textContent = fmtTick(v, ticks);
    });
    const nx = Math.max(2, Math.min(6, Math.floor((W - m.l) / 90)));
    for (let i = 0; i <= nx; i++) {
      const t = x0 + (x1 - x0) * i / nx;
      const tx = el("text", { x: X(t), y: H - 5, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle", "font-size": 11, fill: "var(--muted)" }, svg);
      tx.textContent = hhmm(t, x1 - x0 < 600);
    }
    series.forEach((s, i) => {
      let d = "", pen = false;
      s.values.forEach((v, k) => {
        if (v == null) { pen = false; return; }
        d += (pen ? "L" : "M") + X(ts[k]).toFixed(1) + "," + Y(v).toFixed(1);
        pen = true;
      });
      const p = el("path", { d, fill: "none", stroke: slot(i), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      if (dash(i)) p.setAttribute("stroke-dasharray", dash(i));
    });
    const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
    const dots = series.map((s, i) => el("circle", { r: 4, fill: slot(i), stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg));
    // value label for the linked cross-hair (when another chart is under the pointer): this chart's values at that moment
    const vlab = el("text", { y: m.t + 10, "font-size": 11, "font-weight": 600, visibility: "hidden", "paint-order": "stroke", stroke: "var(--surface)", "stroke-width": 3 }, svg);
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent", style: "touch-action:pan-y" }, svg);
    const fmtV = v => (v != null && Math.abs(v) >= 99 && Math.abs(v) <= 100 ? v.toFixed(3) : fmtNum(v));
    const nearest = t => { let k = 0, best = Infinity; ts.forEach((tt, j) => { const dd = Math.abs(tt - t); if (dd < best) { best = dd; k = j; } }); return k; };
    const place = k => {
      cross.setAttribute("x1", X(ts[k])); cross.setAttribute("x2", X(ts[k])); cross.setAttribute("visibility", "visible");
      series.forEach((s, i) => {
        const v = s.values[k];
        if (v != null) { dots[i].setAttribute("cx", X(ts[k])); dots[i].setAttribute("cy", Y(v)); dots[i].setAttribute("visibility", "visible"); }
        else dots[i].setAttribute("visibility", "hidden");
      });
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); vlab.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); };
    const me = {
      svg,
      show(t) {  // driven by another chart: same moment, values written next to the line
        if (t < x0 - 30 || t > x1 + 30) { hide(); return; }
        const k = nearest(t);
        place(k);
        while (vlab.firstChild) vlab.removeChild(vlab.firstChild);
        series.forEach((s, i) => {
          if (s.values[k] == null) return;
          const sp = el("tspan", { fill: slot(i) }, vlab);
          sp.textContent = (vlab.childNodes.length > 1 ? " · " : "") + fmtV(s.values[k]);
        });
        const x = X(ts[k]), right = x > (m.l + W - m.r) / 2;
        vlab.setAttribute("x", right ? x - 5 : x + 5);
        vlab.setAttribute("text-anchor", right ? "end" : "start");
        vlab.setAttribute("visibility", vlab.childNodes.length ? "visible" : "hidden");
      },
      hide,
    };
    linked.push(me);
    const move = ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * W / r.width;
      const t = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
      const k = nearest(t);
      vlab.setAttribute("visibility", "hidden");
      place(k);
      let html = `<div class="t">${hhmm(ts[k], true)}</div>`;
      series.forEach((s, i) => {
        const v = s.values[k], u = s.unit != null ? s.unit : opts.unit;
        html += `<div><i style="background:${slot(i)}"></i>${esc(s.name)}<b>${fmtV(v)}${u && v != null ? " " + esc(u) : ""}</b></div>`;
      });
      showTip(html, ev.clientX, ev.clientY);
      syncAll(me, ts[k]);
    };
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", () => { hide(); hideTip(); syncAll(me, null); });
  }

  /* opts: {title, bars:[{label, short, value, sub, highlight, href, mark, tip}], unit, metric, subLabel, ref, refLabel,
     legend:[[fill, label] | ["mark", label]], diverging (bars around 0, negatives in red)} */
  function barChart(container, opts) {
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const title = document.createElement("div"); title.className = "title"; title.textContent = opts.title;
    wrap.appendChild(title);
    container.appendChild(wrap);
    const bars = opts.bars.filter(b => b.value != null);
    if (!bars.length) { const p = document.createElement("div"); p.className = "muted"; p.textContent = "No measured runs yet."; wrap.appendChild(p); return; }
    const lg = (opts.legend || []).map(([f, l]) => f === "mark" ? `<span>▲ ${esc(l)}</span>` : `<span><span class="sw" style="background:${f}"></span>${esc(l)}</span>`);
    if (opts.ref != null) lg.push(`<span><span class="ref"></span>${esc(opts.refLabel || "first run")}</span>`);
    if (lg.length) { const k = document.createElement("div"); k.className = "legend"; k.innerHTML = lg.join(""); wrap.appendChild(k); }
    const W = Math.max(wrap.clientWidth || 300, 240), H = opts.height || 220;
    const m = { l: 48, r: 8, t: 26, b: (W - 56) / Math.max(bars.length, 1) < 70 ? 24 : 34 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "group", "aria-label": opts.title });
    wrap.appendChild(svg);
    const vals = bars.map(b => b.value).concat(opts.ref != null ? [opts.ref] : []);
    const ticks = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals), 4);
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const Y = v => m.t + (H - m.t - m.b) * (1 - (v - lo) / Math.max(hi - lo, 1e-9));
    ticks.forEach(v => {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? "var(--axis)" : "var(--grid)" }, svg);
      const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, svg); t.textContent = fmtNum(v);
    });
    const slotW = (W - m.l - m.r) / bars.length;
    const bw = Math.min(56, slotW - 2 - Math.min(slotW * 0.3, 20));
    const tipHtml = b => `<div class="t">${esc(b.label)}</div><div>${esc(opts.metric || "value")}<b>${fmtNum(b.value)} ${esc(opts.unit || "")}</b></div>${b.sub ? `<div>${esc(opts.subLabel || "change")}<b>${esc(b.sub)}</b></div>` : ""}${b.tip || ""}${b.mark ? '<div class="t">▲ best configuration known at this point</div>' : ""}${b.href ? '<div class="t" style="margin:4px 0 0">tap again or press Enter to open</div>' : ""}`;
    let armed = null;  // touch: the first tap shows the values, a second tap on the same bar opens it
    bars.forEach((b, i) => {
      const cx = m.l + slotW * (i + 0.5), y0 = Y(0), y = Y(b.value), neg = b.value < 0;
      const top = Math.min(y, y0), h = Math.abs(y0 - y);
      const r = Math.min(4, bw / 2, h);
      const x = cx - bw / 2;
      const d = neg ? `M${x},${y0} V${y - r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y - r} V${y0} Z`
        : `M${x},${y0} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${y0} Z`;
      const fill = neg && opts.diverging ? "var(--crit)" : b.highlight === "best" ? "var(--s3)" : b.highlight === "trade" ? "var(--s2)" : b.highlight ? "var(--s1)" : "var(--axis)";
      el("path", { d, fill }, svg);
      // dense charts (many runs): short labels only, thinned so they never overlap; details are in the tooltip
      const dense = slotW < 70;
      const every = dense ? Math.max(1, Math.ceil(28 / slotW)) : 1;
      if (i % every === 0 || i === bars.length - 1 && slotW * ((bars.length - 1) % every) >= 28) {
        const lab = el("text", { x: cx, y: H - 18, "text-anchor": "middle", "font-size": 11, fill: "var(--ink-2)" }, svg);
        const text = dense ? (b.short || b.label) : b.label;
        const maxc = Math.max(3, Math.floor((slotW * every - 6) / 6.3));
        lab.textContent = text.length > maxc ? text.slice(0, maxc - 1) + "…" : text;
      }
      if (b.sub && !dense) { const s2 = el("text", { x: cx, y: H - 5, "text-anchor": "middle", "font-size": 10.5, fill: "var(--muted)" }, svg); s2.textContent = b.sub; }
      const showVal = b.highlight && slotW > 40;
      const vy = neg ? y + 13 : y - 4;
      if (b.mark) {  // above the value label when there is one, never on top of it
        const tw = Math.min(9, bw), base = neg ? top - 3 : y - (showVal ? 17 : 3);
        el("path", { d: `M${cx - tw / 2},${base} L${cx + tw / 2},${base} L${cx},${base - tw * 0.8} Z`, fill: "var(--ink)" }, svg);
      }
      if (showVal) { const v = el("text", { x: cx, y: vy, "text-anchor": "middle", "font-size": 11, fill: "var(--ink)" }, svg); v.textContent = fmtNum(b.value); }
      const hit = el("rect", { x: cx - slotW / 2, y: m.t, width: slotW, height: H - m.t, fill: "transparent", style: b.href ? "cursor:pointer" : "", tabindex: 0,
        role: b.href ? "link" : "img", "aria-label": `${b.label}: ${fmtNum(b.value)} ${opts.unit || ""}${b.sub ? ", " + b.sub : ""}` }, svg);
      const open = () => { hideTip(); location.hash = b.href; };
      hit.addEventListener("pointermove", ev => { if (ev.pointerType === "mouse") showTip(tipHtml(b), ev.clientX, ev.clientY); });
      hit.addEventListener("pointerleave", ev => { if (ev.pointerType === "mouse") hideTip(); });
      hit.addEventListener("click", ev => {
        if (ev.pointerType && ev.pointerType !== "mouse" && armed !== i) { ev.stopPropagation(); armed = i; showTip(tipHtml(b), ev.clientX, ev.clientY); return; }
        if (b.href) open();
      });
      hit.addEventListener("focus", () => { const r = hit.getBoundingClientRect(); showTip(tipHtml(b), r.left + r.width / 2, r.top + 20); });
      hit.addEventListener("blur", hideTip);
      hit.addEventListener("keydown", ev => { if (ev.key === "Enter" && b.href) open(); });
    });
    if (opts.ref != null) el("line", { x1: m.l, x2: W - m.r, y1: Y(opts.ref), y2: Y(opts.ref), stroke: "var(--ink-2)", "stroke-dasharray": "4 3", "stroke-width": 1 }, svg);
  }

  // flame graph (root at the bottom) from folded stacks [["a;b;c", count], ...]; click a frame to zoom, click it again to reset
  function flameGraph(container, folded, opts) {
    opts = opts || {};
    const root = { name: "all", value: 0, children: {} };
    folded.forEach(([stack, n]) => {
      root.value += n;
      let node = root;
      stack.split(";").forEach(f => { node = node.children[f] = node.children[f] || { name: f, value: 0, children: {} }; node.value += n; });
    });
    const H = 17, wrap = document.createElement("div"); wrap.className = "chart flame";
    container.appendChild(wrap);
    // depth of the frames wide enough to draw (narrower ones are skipped), so no empty band sits above the stacks
    const depthOf = (n, px) => px < 0.6 ? 0 : 1 + Math.max(0, ...Object.values(n.children).map(c => depthOf(c, px * c.value / n.value)));
    let focus = root;
    const color = name => {
      if (/^\[|\.so/.test(name)) return "var(--grid)";
      let h = 0; for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      return `hsl(${20 + h % 35}, ${70 + h % 20}%, ${58 + h % 14}%)`;
    };
    function draw() {
      wrap.innerHTML = "";
      if (focus !== root) {
        const c = document.createElement("div"); c.className = "flame-crumb";
        c.innerHTML = `<a href="#" role="button">all</a> ▸ ${esc(focus.name)}`;
        c.querySelector("a").onclick = ev => { ev.preventDefault(); focus = root; draw(); };
        wrap.appendChild(c);
      }
      const W = Math.max(wrap.clientWidth || 600, 300), D = Math.max(1, Math.min(depthOf(focus, W), opts.maxDepth || 60)), Ht = D * H;
      const svg = el("svg", { viewBox: `0 0 ${W} ${Ht}`, height: Ht, width: "100%", role: "img", "aria-label": "CPU flame graph" });
      wrap.appendChild(svg);
      const total = focus.value;
      (function walk(node, x, depth) {
        const w = node.value / total * W;
        if (w < 0.6 || depth >= D) return;
        const y = Ht - (depth + 1) * H;
        const g = el("g", { "data-tip": `${node.name} — ${(100 * node.value / root.value).toFixed(1)}% of database CPU samples`, style: "cursor:pointer", tabindex: w > 6 ? 0 : -1, role: "button" }, svg);
        el("rect", { x, y, width: Math.max(w - 0.5, 0.5), height: H - 1, fill: color(node.name), rx: 2 }, g);
        if (w > 40) {
          const t = el("text", { x: x + 4, y: y + 12, "font-size": 11, fill: "var(--on-warm)" }, g);
          const fit = Math.max(1, Math.floor((w - 8) / 6.3));
          t.textContent = node.name.length > fit ? node.name.slice(0, fit - 1) + "…" : node.name;
        }
        g.addEventListener("click", () => { focus = node === focus ? root : node; draw(); });
        g.addEventListener("keydown", ev => { if (ev.key === "Enter") { focus = node === focus ? root : node; draw(); } });
        let cx = x;
        Object.values(node.children).sort((a, b) => b.value - a.value).forEach(ch => { walk(ch, cx, depth + 1); cx += ch.value / total * W; });
      })(focus, 0, 0);
    }
    draw();
  }

  window.Charts = { lineChart, barChart, flameGraph, fmtNum, hhmm };
})();
