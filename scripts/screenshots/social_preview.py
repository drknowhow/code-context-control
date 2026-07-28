"""Render the GitHub social preview card to PNG.

GitHub's social preview (Settings -> General -> Social preview) is what every
share on X / Slack / LinkedIn / Discord renders. There is no API for setting
it, so this only produces the file — upload it by hand once.

Spec: 1280x640, under 1MB. Rendered from HTML rather than generated, because
the card is almost entirely typography and image models cannot render
legible text reliably.

Usage:
    python -m scripts.screenshots.social_preview [--out docs/social-preview.png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "social_preview.html"

WIDTH, HEIGHT = 1280, 640
MAX_BYTES = 1_000_000


def render(out: Path, scale: int = 2) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=scale,
        ).new_page()
        page.goto(TEMPLATE.as_uri(), wait_until="load")
        page.wait_for_function("() => document.fonts.status === 'loaded'", timeout=30_000)
        page.screenshot(path=str(out), clip={"x": 0, "y": 0,
                                             "width": WIDTH, "height": HEIGHT})
        browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/social-preview.png")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()

    out = Path(args.out)
    render(out, args.scale)
    size = out.stat().st_size

    # Fall back to 1x rather than shipping something GitHub will reject.
    if size > MAX_BYTES and args.scale > 1:
        print(f"  {size/1000:.0f} KB at {args.scale}x exceeds 1 MB — re-rendering at 1x")
        render(out, 1)
        size = out.stat().st_size

    print(f"  {out}  {size/1000:.0f} KB")
    if size > MAX_BYTES:
        print("  WARNING: still over GitHub's 1 MB limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
