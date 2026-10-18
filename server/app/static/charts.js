/* Minimal SVG charts: time-series lines with crosshair tooltip, and per-run bars. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const SLOTS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];
  const tip = () => document.getElementById("tip");

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
  function lineChart(container, opts) {
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const series = opts.series.filter(s => s.values && s.values.some(v => v != null));
    const title = document.createElement("div");
    title.className = "title"; title.textContent = opts.title + (opts.unit ? " (" + opts.unit + ")" : "");
    wrap.appendChild(title);
    if (series.length > 1) {
      const lg = document.createElement("div"); lg.className = "legend";
      series.forEach((s, i) => { const sp = document.createElement("span"); sp.innerHTML = `<i style="background:${SLOTS[i]}"></i>`; sp.appendChild(document.createTextNode(s.name)); lg.appendChild(sp); });
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
    (opts.markers || []).forEach(mk => {
      if (mk.t < x0 || mk.t > x1) return;
      const x = X(mk.t);
      el("line", { x1: x, x2: x, y1: m.t, y2: H - m.b, stroke: "var(--ink-2)", "stroke-dasharray": "3 3", "stroke-width": 1, opacity: 0.7 }, svg);
      const lab = el("text", { x: x + (mk.end ? -3 : 3), y: m.t + 9, "text-anchor": mk.end ? "end" : "start", "font-size": 10, fill: "var(--ink-2)" }, svg);
      lab.textContent = mk.label;
    });
    if (opts.window && opts.window[0]) {
      const a = X(Math.max(opts.window[0], x0)), b = X(Math.min(opts.window[1] || x1, x1));
      if (b > a) el("rect", { x: a, y: m.t, width: b - a, height: H - m.t - m.b, fill: "var(--window)" }, svg);
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
      el("path", { d, fill: "none", stroke: SLOTS[i], "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    });
    const cross = el("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
    const dots = series.map((s, i) => el("circle", { r: 4, fill: SLOTS[i], stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg));
    const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * W / r.width;
      const t = x0 + (px - m.l) / (W - m.l - m.r) * (x1 - x0);
      let k = 0, best = Infinity;
      ts.forEach((tt, j) => { const dd = Math.abs(tt - t); if (dd < best) { best = dd; k = j; } });
      cross.setAttribute("x1", X(ts[k])); cross.setAttribute("x2", X(ts[k])); cross.setAttribute("visibility", "visible");
      let html = `<div class="t">${hhmm(ts[k], true)}</div>`;
      series.forEach((s, i) => {
        const v = s.values[k];
        if (v != null) { dots[i].setAttribute("cx", X(ts[k])); dots[i].setAttribute("cy", Y(v)); dots[i].setAttribute("visibility", "visible"); }
        else dots[i].setAttribute("visibility", "hidden");
        html += `<div><i style="background:${SLOTS[i]}"></i>${s.name}<b>${v != null && Math.abs(v) >= 99 && Math.abs(v) <= 100 ? v.toFixed(3) : fmtNum(v)}${opts.unit && opts.unit.length <= 3 ? " " + opts.unit : ""}</b></div>`;
      });
      showTip(html, ev.clientX, ev.clientY);
    });
    hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); hideTip(); });
  }

  /* opts: {title, bars:[{label, value, sub, highlight, href}], unit, lowerBetter, ref} */
  function barChart(container, opts) {
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const title = document.createElement("div"); title.className = "title"; title.textContent = opts.title;
    wrap.appendChild(title);
    container.appendChild(wrap);
    const bars = opts.bars.filter(b => b.value != null);
    if (!bars.length) { const p = document.createElement("div"); p.className = "muted"; p.textContent = "No measured runs yet."; wrap.appendChild(p); return; }
    const W = Math.max(wrap.clientWidth || 300, 240), H = opts.height || 220;
    const m = { l: 48, r: 8, t: 16, b: 34 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.title });
    wrap.appendChild(svg);
    const ticks = niceTicks(0, Math.max(...bars.map(b => b.value), opts.ref != null ? opts.ref : 0), 4);
    const hi = ticks[ticks.length - 1];
    const Y = v => m.t + (H - m.t - m.b) * (1 - v / hi);
    ticks.forEach(v => {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), stroke: v === 0 ? "var(--axis)" : "var(--grid)" }, svg);
      const t = el("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, svg); t.textContent = fmtNum(v);
    });
    const slot = (W - m.l - m.r) / bars.length;
    const bw = Math.min(56, slot - 2 - Math.min(slot * 0.3, 20));
    bars.forEach((b, i) => {
      const cx = m.l + slot * (i + 0.5), y = Y(b.value), h = Y(0) - y;
      const r = Math.min(4, bw / 2, h);
      const x = cx - bw / 2;
      const d = `M${x},${Y(0)} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${Y(0)} Z`;
      const p = el("path", { d, fill: b.highlight ? "var(--s1)" : "var(--axis)" }, svg);
      if (b.highlight === "best") p.setAttribute("fill", "var(--s3)");
      if (b.highlight === "trade") p.setAttribute("fill", "var(--s2)");
      const lab = el("text", { x: cx, y: H - 18, "text-anchor": "middle", "font-size": 11, fill: "var(--ink-2)" }, svg);
      const maxc = Math.max(4, Math.floor((slot - 6) / 6.3));
      lab.textContent = b.label.length > maxc ? b.label.slice(0, maxc - 1) + "…" : b.label;
      if (b.sub) { const s2 = el("text", { x: cx, y: H - 5, "text-anchor": "middle", "font-size": 10.5, fill: "var(--muted)" }, svg); s2.textContent = b.sub; }
      if (b.highlight && slot > 40) { const v = el("text", { x: cx, y: y - 4, "text-anchor": "middle", "font-size": 11, fill: "var(--ink)" }, svg); v.textContent = fmtNum(b.value); }
      const hit = el("rect", { x: cx - slot / 2, y: m.t, width: slot, height: H - m.t, fill: "transparent", style: b.href ? "cursor:pointer" : "" }, svg);
      hit.addEventListener("mousemove", ev => showTip(`<div class="t">${b.label}</div><div>${opts.metric || "value"}<b>${fmtNum(b.value)} ${opts.unit || ""}</b></div>${b.tip || ""}`, ev.clientX, ev.clientY));
      hit.addEventListener("mouseleave", hideTip);
      if (b.href) hit.addEventListener("click", () => { hideTip(); location.hash = b.href; });
    });
    if (opts.ref != null) {
      el("line", { x1: m.l, x2: W - m.r, y1: Y(opts.ref), y2: Y(opts.ref), stroke: "var(--ink-2)", "stroke-dasharray": "4 3", "stroke-width": 1 }, svg);
      const t = el("text", { x: W - m.r, y: Y(opts.ref) - 4, "text-anchor": "end", "font-size": 11, fill: "var(--ink-2)" }, svg); t.textContent = opts.refLabel || "first run";
    }
  }

  window.Charts = { lineChart, barChart, fmtNum, hhmm };
})();
