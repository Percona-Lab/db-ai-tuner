"""Load the web UI of a just-started version in a headless browser: any JavaScript error (e.g. a syntax error that
leaves the page on 'Loading...') fails the deploy before traffic is switched. Usage: ui_check.py http://127.0.0.1:PORT/"""
import sys
from playwright.sync_api import sync_playwright

url = sys.argv[1]
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url)
    pg.wait_for_timeout(1500)
    body = pg.inner_text("body")
    if not errs and ("Loading..." in body or "Log in" not in body):  # the app must get past its loading placeholder
        errs.append("the page did not render past 'Loading...'")
    b.close()
print("\n".join(errs) or "ui ok")
sys.exit(1 if errs else 0)
