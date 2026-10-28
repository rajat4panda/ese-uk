#!/usr/bin/env python3
"""Render a docs/*.md report to a PDF using docs/report.css.

Markdown → HTML with pandoc, then HTML → PDF with the Chromium that Playwright already provides,
so the PDF uses the same print stylesheet as the other reports and needs no extra toolchain.

    python3 scripts/render_report.py docs/UI_PROTOTYPE_COMPARISON.md docs/ESE_UI_Comparison.pdf
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "docs" / "report.css"


def render(source: Path, target: Path) -> None:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "report.html"
        # Fixed argument vector, no shell: the only variable parts are paths this script built.
        subprocess.run(  # noqa: S603
            ["pandoc", str(source), "-o", str(html), "--standalone",  # noqa: S607
             "--css", str(CSS), "--metadata", f"pagetitle={source.stem}",
             "--embed-resources"],
            check=True,
        )  # fmt: skip
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(html.as_uri(), wait_until="load")
            page.pdf(path=str(target), format="A4", print_background=True,
                     margin={"top": "18mm", "bottom": "20mm", "left": "16mm", "right": "16mm"})  # fmt: skip
            browser.close()
    print(f"{target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    render(Path(sys.argv[1]), Path(sys.argv[2]))
