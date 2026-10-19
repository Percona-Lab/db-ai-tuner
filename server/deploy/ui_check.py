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
    if pg.locator("input[name=email]").count() == 0 and not errs:  # the login form must render for a logged-out visitor
        errs.append("login form did not render")
    b.close()
print("\n".join(errs) or "ui ok")
sys.exit(1 if errs else 0)
