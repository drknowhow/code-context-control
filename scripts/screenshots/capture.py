"""Capture README screenshots with Playwright.

Two failure modes this script is built to prevent:

1. "edited != served" — cli/server.py:234 builds `_ui_html_cache` on the first
   request and never invalidates it, and the app runs with debug=False. A
   long-running server therefore serves the code it started with, forever.
   Capturing against it silently publishes a UI missing recent features.
   Mitigation: FRESHNESS_MARKERS below are asserted against the *served*
   bundle before any capture happens. Fail-closed.

2. Port drift — cli/server.py:4211 find_free_port() silently walks
   3333 -> 3334 -> 3335 when a port is taken, and cli/hub_server.py:3048
   returns without starting if a hub already owns the port. Never assume a
   port; the caller passes the one it parsed from the server banner.

Usage:
    python -m scripts.screenshots.capture --ui-url http://127.0.0.1:3333 \
                                          --hub-url http://127.0.0.1:3330 \
                                          --out docs/screenshots/2026-07
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# Viewport chosen so the sidebar + main grid both breathe; 2x scale keeps the
# PNG crisp when the README renders it at width="900".
VIEWPORT = {"width": 1440, "height": 900}
HUB_VIEWPORT = {"width": 1440, "height": 680}
SCALE = 2

# A symbol that exists ONLY in the newest component of each bundle. If the
# served HTML lacks it, the server predates the current tree — abort.
FRESHNESS_MARKERS = {
    "ui": "MASK_EMPTY_FORM",       # cli/ui/components/access.js (v2.63.0)
    "hub": "CREDS_EMPTY_FORM",     # cli/hub_ui/components/hub_credentials.js
}

# (tab label, output slug). Labels come from cli/ui/app.js:5-18.
UI_TABS = [
    ("Dashboard", "ui_dashboard"),
    ("Tasks", "ui_tasks"),
    ("Edits", "ui_edits"),
    ("Memory", "ui_memory"),
    ("Access Guard", "ui_access_guard"),
    ("Credentials", "ui_credentials"),
    ("Instructions", "ui_instructions"),
    ("Sessions", "ui_sessions"),
    ("Settings", "ui_settings"),
]


class StaleServer(RuntimeError):
    """The server is serving a bundle older than the working tree."""


def _assert_fresh(page: Page, which: str) -> None:
    marker = FRESHNESS_MARKERS[which]
    served = page.evaluate("() => document.documentElement.outerHTML")
    if marker not in served:
        raise StaleServer(
            f"{which}: served bundle has no {marker!r}. The server predates the "
            f"working tree — restart it and retry. (cli/server.py:234 caches the "
            f"bundle for the process lifetime.)")
    print(f"    freshness OK ({marker} served)")


def _wait_rendered(page: Page) -> None:
    """Wait for React + babel-standalone + webfonts. Never a bare timeout.

    ui.html loads React and babel-standalone from CDN and compiles JSX in the
    browser, so #root is empty for a variable stretch after load.
    """
    page.wait_for_function(
        """() => {
             const r = document.getElementById('root');
             return !!(r && r.children.length) && document.fonts.status === 'loaded';
           }""",
        timeout=60_000)


def _select_ui_tab(page: Page, label: str) -> None:
    """Click a sidebar tab and confirm it became active before returning.

    There is no routing anywhere in cli/ui/ — no hash, no pushState. Tabs are
    React state + display:none, so selection has to go through a real click.
    """
    page.evaluate(
        """(label) => {
             const btn = [...document.querySelectorAll('nav button')]
               .find(b => b.textContent.trim() === label);
             if (!btn) throw new Error('no tab button: ' + label);
             btn.click();
           }""", label)
    # The active button renders at fontWeight 600 (cli/ui/components/sidebar.js).
    page.wait_for_function(
        """(label) => {
             const active = [...document.querySelectorAll('nav button')]
               .find(b => getComputedStyle(b).fontWeight === '600');
             return active && active.textContent.trim() === label;
           }""", arg=label, timeout=15_000)
    page.wait_for_timeout(500)  # let the panel's own fetch settle


def capture_ui(page: Page, url: str, out: Path) -> list[Path]:
    written: list[Path] = []
    page.goto(url, wait_until="domcontentloaded")

    # Unset => collapsed 54px icon-only sidebar with no labels (cli/ui/app.js:31).
    page.evaluate("() => localStorage.setItem('c3-sidebar-pinned','true')")
    page.reload(wait_until="domcontentloaded")
    _wait_rendered(page)
    _assert_fresh(page, "ui")

    for label, slug in UI_TABS:
        _select_ui_tab(page, label)
        target = out / f"{slug}.png"
        page.screenshot(path=str(target))
        written.append(target)
        print(f"    {slug}")
    return written


def capture_hub(page: Page, url: str, out: Path) -> list[Path]:
    written: list[Path] = []
    # The Hub's views are short (a handful of project rows). At the full 900px
    # height most of the frame is empty background, which reads as a broken
    # page once the README scales it to 900px wide.
    page.set_viewport_size(HUB_VIEWPORT)
    page.goto(url, wait_until="domcontentloaded")
    _wait_rendered(page)
    _assert_fresh(page, "hub")

    # Hub view state is server-side config (cli/hub_server.py:419-457), so it
    # can be driven deterministically instead of by clicking.
    for main_view, slug in [("projects", "hub_projects"),
                            ("board", "hub_tasks"),
                            ("creds", "hub_credentials")]:
        page.request.post(f"{url}/api/hub/config",
                          data={"main_view": main_view},
                          headers={"Origin": url})
        page.reload(wait_until="domcontentloaded")
        _wait_rendered(page)
        page.wait_for_timeout(700)
        target = out / f"{slug}.png"
        page.screenshot(path=str(target))
        written.append(target)
        print(f"    {slug}")
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui-url", required=True)
    ap.add_argument("--hub-url", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=SCALE,
                                  color_scheme="dark")
        page = ctx.new_page()
        try:
            print("  per-project UI")
            written = capture_ui(page, args.ui_url.rstrip("/"), out)
            print("  hub")
            written += capture_hub(page, args.hub_url.rstrip("/"), out)
        except StaleServer as exc:
            print(f"\nABORTED: {exc}", file=sys.stderr)
            return 2
        finally:
            browser.close()

    print(f"\n  {len(written)} screenshots -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
