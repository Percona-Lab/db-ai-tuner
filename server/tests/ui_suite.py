#!/usr/bin/env python3
"""UI test suite: logs in with a browser and visits every kind of page with real data, read-only (no clicks that change
anything). Fails on what a person would notice first:
- JavaScript errors and console errors;
- API calls answered 4xx / 5xx;
- pages left on "Loading..." or showing an error box;
- horizontal scroll of the whole document (page title included), at desktop width and at phone width;
- tooltips keyboard users cannot open ([data-tip] on an element that cannot take the focus and has no tabindex);
- chart series drawn invisibly (stroke or fill "undefined", e.g. more series than colours);
- missing features that pages must show (checks below).

usage: UI_EMAIL=... UI_PASSWORD=... ui_suite.py https://benchtune.com [--max-sessions 8] [--json report.json] [--static DIR]
       (UI_PASSWORD_FILE=path instead of UI_PASSWORD; PLAYWRIGHT_BROWSERS_PATH as for the deploy check)
       --static DIR: serve index.html, app.js, charts.js and app.css from DIR instead of the server's (test a UI change
       against real data before deploying it)
Exit code 0 = every page passed.

Pages are picked from the account's own data through the API, so the suite follows the data:
- environments, newest first;
- sessions of different kinds (planner, trade-offs, no swap, active, recent);
- one run of each picked session, with every run tab;
- studies, starred notes, policies, parameters, learning, account and the server-rendered /status.
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

DESKTOP = {"width": 1280, "height": 900}
PHONE = {"width": 390, "height": 844}
RUN_TABS = ("changes", "graphs", "inputs", "cpu", "recs", "config")
SETTLE_MS = 2500


def login(ctx, base, email, password):
    r = ctx.request.post(base + "/api/auth/login", data={"email": email, "password": password})
    if r.status != 200:
        sys.exit(f"login failed: HTTP {r.status}")


def api(ctx, base, path):
    r = ctx.request.get(base + path)
    return r.json() if r.status == 200 else None


def pick(ctx, base, max_sessions):
    """Pages to visit: [(name, hash, checks)]. checks: list of (description, js returning true when it holds)."""
    pages = [("environments", "#/", []), ("studies", "#/studies", []), ("starred and notes", "#/notes", []),
             ("policies", "#/policies", []), ("parameters", "#/parameters", []), ("learning", "#/learning", []),
             ("account", "#/account", [("AI mode selector", "!!document.querySelector('select[name=mode]') || !document.querySelector('#lf')")])]
    envs = (api(ctx, base, "/api/environments") or {}).get("environments") or []
    envs = [e for e in envs if e.get("sessions")]
    sessions = []
    for e in envs:
        d = api(ctx, base, f"/api/environments/{e['id']}") or {}
        ss = [s for s in d.get("sessions") or [] if s.get("runs")]
        e["_last"] = max((s.get("id") or 0 for s in ss), default=0)
        sessions += ss
    envs.sort(key=lambda e: -e["_last"])  # the environments in use now first
    for e in envs[:6]:
        pages.append((f"env {e['id']}", f"#/env/{e['id']}", []))
    # newest first, and at least one of each kind: AI planner, active, closed
    sessions.sort(key=lambda s: -(s.get("id") or 0))
    chosen = []
    for want in (lambda s: s.get("planner"), lambda s: s.get("status") == "active", lambda s: s.get("status") == "closed"):
        x = next((s for s in sessions if want(s) and s not in chosen), None)
        if x:
            chosen.append(x)
    chosen += [s for s in sessions if s not in chosen][:max(0, max_sessions - len(chosen))]
    for s in chosen[:max_sessions]:
        sd = api(ctx, base, f"/api/sessions/{s['id']}") or {}
        runs = [r for r in sd.get("runs") or [] if r.get("status") == "done"]
        checks = []
        if any((r.get("summary") or {}).get("swap_total_mb") == 0 for r in runs[-1:]):
            checks.append(("'no swap' shown for a session measured without swap", "document.body.innerText.includes('no swap')"))
        if len(runs) >= 2:
            checks.append(("best configuration tiles", "['Best production-safe', 'Best safe', 'Best with trade-offs'].some(x => document.body.innerText.includes(x))"))
        pages.append((f"session {s['id']}", f"#/session/{s['id']}", checks))
        if runs:
            r = runs[-1]
            rec = [x for x in sd.get("recommendations") or [] if x.get("run_id") == r["id"]]
            rd = api(ctx, base, f"/api/runs/{r['id']}") or {}
            for tab in RUN_TABS:
                ck = []
                if tab == "changes" and (rd.get("vs_baseline") or {}).get("settings"):
                    ck.append(("settings that differ from the baseline", "document.body.innerText.includes('Differs from the baseline')"))
                if tab == "recs" and rec and rec[0].get("decision"):
                    ck.append(("decision shown (rules / AI planner)", "document.body.innerText.includes('decided by')"))
                if tab == "recs" and rec and any(i.get("basis") for i in rec[0].get("items") or []):
                    ck.append(("basis shown per recommendation", "document.body.innerText.includes('Based on')"))
                pages.append((f"run {r['id']} {tab}", f"#/run/{r['id']}/{tab}", ck))
    studies = (api(ctx, base, "/api/studies") or {}).get("studies") or []
    for st in studies[:3]:
        pages.append((f"study {st['id']}", f"#/study/{st['id']}", []))
    return pages


def serve_static(ctx, base, d):
    """Local copies of the single-page app's files instead of the server's (the API and data stay the server's)."""
    types = {"app.js": "application/javascript", "charts.js": "application/javascript", "app.css": "text/css"}
    def serve(path, ctype):
        return lambda rt: rt.fulfill(path=path, content_type=ctype)
    for name, ctype in types.items():
        if os.path.exists(os.path.join(d, name)):
            ctx.route(f"**/static/{name}*", serve(os.path.join(d, name), ctype))
    if os.path.exists(os.path.join(d, "index.html")):
        ctx.route(lambda u: u.split("#")[0].rstrip("/") == base, lambda rt: rt.fulfill(path=os.path.join(d, "index.html"), content_type="text/html")
                  if rt.request.resource_type == "document" else rt.continue_())


def check_page(pg, base, name, hash_, checks, viewport, bad_api):
    errs = []
    n0 = len(bad_api)
    pg.set_viewport_size(viewport)
    # a full load per page: a hash-only navigation can leave the previous page on screen when the checks read it
    pg.goto("about:blank")
    pg.goto(base + "/" + hash_ if not hash_.startswith("/") else base + hash_)
    try:
        pg.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        errs.append("network did not settle in 15 s")
    pg.wait_for_timeout(SETTLE_MS)
    view = pg.evaluate("(document.getElementById('view') || document.body).innerText")
    if "Loading..." in view or "Loading…" in view.split("\n")[0]:
        errs.append("still 'Loading...'")
    err_box = pg.evaluate("Array.from(document.querySelectorAll('#view > p.err, #view > .err')).map(e => e.innerText).join(' | ')")
    if err_box:
        errs.append("error shown: " + err_box[:200])
    over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    if over > 1:
        wide = pg.evaluate("""() => { const w = document.documentElement.clientWidth; return Array.from(document.querySelectorAll('body *'))
            .filter(e => e.getBoundingClientRect().right > w + 1 && !e.closest('.tablewrap, pre, .chartwrap, details:not([open]), #tip'))
            .slice(0, 3).map(e => e.tagName.toLowerCase() + (e.className ? '.' + String(e.className).split(' ')[0] : '')).join(', ') }""")
        errs.append(f"horizontal scroll: {over}px at {viewport['width']}px wide" + (f" ({wide})" if wide else ""))
    no_tab = pg.evaluate("""() => Array.from(document.querySelectorAll('[data-tip]:not([tabindex])'))
        .filter(e => !e.matches('a[href], button, input, select, textarea')).slice(0, 200)
        .map(e => e.tagName.toLowerCase() + ' "' + (e.textContent || '').trim().slice(0, 24) + '"')""")
    if no_tab:
        errs.append(f"{len(no_tab)} tooltip(s) not reachable by keyboard (data-tip without tabindex): " + ", ".join(no_tab[:4]))
    invisible = pg.evaluate("""() => Array.from(document.querySelectorAll('svg path, svg rect, svg circle, svg line, .legend i, .tip i'))
        .filter(e => ['stroke', 'fill'].some(a => e.getAttribute(a) === 'undefined') || /background:\\s*undefined/.test(e.getAttribute('style') || '')).length""")
    if invisible:
        errs.append(f"{invisible} chart series or legend swatches drawn with an undefined colour")
    for desc, js in checks:
        try:
            if not pg.evaluate(js):
                errs.append("missing: " + desc)
        except Exception as e:
            errs.append(f"check failed ({desc}): {e}")
    errs += [f"API {x}" for x in bad_api[n0:]]
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="e.g. https://benchtune.com")
    ap.add_argument("--max-sessions", type=int, default=8)
    ap.add_argument("--phone-pages", type=int, default=12, help="pages also checked at phone width (the first N)")
    ap.add_argument("--json")
    ap.add_argument("--static", help="directory with index.html, app.js, charts.js, app.css to serve instead of the server's")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    email = os.environ.get("UI_EMAIL")
    pw = os.environ.get("UI_PASSWORD") or (open(os.environ["UI_PASSWORD_FILE"]).read().strip() if os.environ.get("UI_PASSWORD_FILE") else None)
    if not email or not pw:
        sys.exit("set UI_EMAIL and UI_PASSWORD (or UI_PASSWORD_FILE)")
    results, t0 = [], time.time()
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport=DESKTOP)
        login(ctx, base, email, pw)
        if a.static:
            serve_static(ctx, base, a.static)
        pg = ctx.new_page()
        js_errs, bad_api = [], []
        pg.on("pageerror", lambda e: js_errs.append(str(e)))
        pg.on("console", lambda m: js_errs.append("console: " + m.text) if m.type == "error" and "favicon" not in m.text else None)
        pg.on("response", lambda r: bad_api.append(f"{r.status} {r.request.method} {r.url.replace(base, '')}")
              if "/api/" in r.url and r.status >= 400 else None)
        pages = pick(ctx, base, a.max_sessions)
        for i, (name, hash_, checks) in enumerate(pages):
            for vp in ([DESKTOP, PHONE] if i < a.phone_pages else [DESKTOP]):
                j0 = len(js_errs)
                errs = check_page(pg, base, name, hash_, checks if vp is DESKTOP else [], vp, bad_api)
                errs += ["JS " + x[:300] for x in js_errs[j0:]]
                results.append({"page": name, "url": hash_, "width": vp["width"], "errors": errs})
                print(("FAIL " if errs else "ok   ") + f"{name} @{vp['width']}" + ("".join("\n       - " + e for e in errs) if errs else ""), flush=True)
        # the server-rendered status page
        pg.set_viewport_size(DESKTOP)
        j0, n0 = len(js_errs), len(bad_api)
        r = pg.goto(base + "/status")
        pg.wait_for_timeout(1500)
        errs = ([f"HTTP {r.status}"] if r and r.status >= 400 else []) + ["JS " + x for x in js_errs[j0:]] + bad_api[n0:]
        results.append({"page": "status", "url": "/status", "width": DESKTOP["width"], "errors": errs})
        print(("FAIL " if errs else "ok   ") + "status" + ("".join("\n       - " + e for e in errs) if errs else ""))
        b.close()
    failed = [x for x in results if x["errors"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} page checks passed in {time.time() - t0:.0f} s")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
