#!/usr/bin/env python3
"""Fetch the Cursor usage CSV from the dashboard via Playwright (last-resort path).

Reuses a persistent browser profile you log into ONCE by hand -- we never automate
your IdP/SSO. Normal runs are headless and:
  1. open cursor.com/dashboard/usage for the current month-to-date,
  2. click the "Export CSV" button,
  3. save the download into ~/.tokometer/cursor-exports/,
  4. record success/failure to ~/.tokometer/state/cursor_fetch.json.

The companion cursor_reconcile.py then parses that CSV into the ledger.

First-time setup (visible window):
    python3 cursor_fetch.py --login
log in via SSO/MFA, then press Enter to persist the session.

Exit code is non-zero on failure so harvest.sh and the morning report can advise you.
"""
import os
import sys
import json
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

PROFILE_DIR = os.path.join(ledger.TOKOMETER_HOME, "cursor-browser-profile")
EXPORT_DIR = os.path.join(ledger.TOKOMETER_HOME, "cursor-exports")
STATE_PATH = os.path.join(ledger.STATE_DIR, "cursor_fetch.json")
USAGE_URL = "https://cursor.com/dashboard/usage"
AUTH_MARKERS = ("/login", "/sign-in", "authenticator", "auth0", "okta", "accounts.")
EXPORT_SELECTORS = (
    'button:has-text("Export CSV")',
    'button:has-text("Export")',
    'text=Export CSV',
    '[aria-label*="Export" i]',
)


def _write_status(ok, **extra):
    os.makedirs(ledger.STATE_DIR, exist_ok=True)
    payload = {"ok": ok, "ts": ledger.iso_utc(), **extra}
    with open(STATE_PATH, "w") as f:
        json.dump(payload, f, indent=1)
    return payload


def _mtd_range():
    today = dt.date.today()
    return today.replace(day=1).isoformat(), today.isoformat()


def login():
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(USAGE_URL, wait_until="domcontentloaded")
        print("\nA browser window opened. Log in to Cursor (SSO/MFA) and load the\n"
              "usage page, then return here and press Enter to save the session.",
              file=sys.stderr)
        try:
            input()
        except EOFError:
            pass
        ctx.close()
    print(f"session saved to {PROFILE_DIR}", file=sys.stderr)


def fetch(headless=True):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    if not os.path.isdir(PROFILE_DIR):
        return _write_status(False, error="no browser profile; run: cursor_fetch.py --login")

    os.makedirs(EXPORT_DIR, exist_ok=True)
    frm, to = _mtd_range()
    url = f"{USAGE_URL}?from={frm}&to={to}"
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=headless, accept_downloads=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # NOT networkidle: the dashboard polls/streams, so it rarely goes idle
            # and would time out daily. domcontentloaded + waiting for the export
            # button below is both faster and reliable.
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)  # let client-side render settle

            if any(m in page.url.lower() for m in AUTH_MARKERS):
                ctx.close()
                return _write_status(False, error=f"not logged in (redirected to {page.url}); "
                                                   f"run: cursor_fetch.py --login")

            btn = None
            for sel in EXPORT_SELECTORS:
                try:
                    el = page.locator(sel).first
                    el.wait_for(state="visible", timeout=8000)
                    btn = el
                    break
                except PWTimeout:
                    continue
            if btn is None:
                ctx.close()
                return _write_status(False, error="Export CSV button not found (layout changed "
                                                   "or session expired)")

            with page.expect_download(timeout=30000) as dl_info:
                btn.click()
            dest = os.path.join(EXPORT_DIR, f"team-usage-events-{to}.csv")
            dl_info.value.save_as(dest)
            ctx.close()
            return _write_status(True, csv=dest, range=[frm, to])

    except PWTimeout as e:
        return _write_status(False, error=f"timeout: {e}")
    except Exception as e:  # playwright launch / browser-missing / nav errors
        return _write_status(False, error=f"{type(e).__name__}: {e}")


def main():
    if "--login" in sys.argv:
        login()
        return
    status = fetch(headless="--headful" not in sys.argv)
    if status["ok"]:
        print(f"[cursor_fetch] ok -> {status['csv']}", file=sys.stderr)
        sys.exit(0)
    print(f"[cursor_fetch] FAILED: {status['error']}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
