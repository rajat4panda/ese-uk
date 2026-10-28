"""WCAG 2.1 AA sweep across every screen in the system (NFR-11).

Runs axe-core against each route as the role that uses it, and fails on any serious or critical
issue. This is the standing accessibility check: it is meant to be run before each release, not
once. It needs data in the system — run the Sprint 2 and Sprint 3 walkthroughs first, or point it
at an environment that has been used.

    RATE_LIMIT_AUTH_PER_MINUTE=300 uvicorn app.main:app --port 8000 &
    python3 tests/e2e/accessibility_sweep.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.sprint2_chain import API, BASE, STAFF_PASSWORD, sign_in, sign_out  # noqa: E402
from tests.e2e.sprint3_post_cas import (
    ACCESSIBILITY,  # noqa: E402
    audit,  # noqa: E402
)

# Each screen, with the role that uses it. Applicant screens are covered by the Sprint 1 journey
# test; here we sweep them again because the sweep is about markup, not behaviour.
SCREENS: list[tuple[str, str, str]] = [
    ("anonymous", "/", "Sign in"),
    ("anonymous", "/register", "Register"),
    ("anonymous", "/forgot-password", "Forgot password"),
    ("applicant@demo.ese.example", "/my", "My application"),
    ("applicant@demo.ese.example", "/apply", "Application form — section 1"),
    ("applicant@demo.ese.example", "/apply?step=9", "Application form — declaration"),
    ("applicant@demo.ese.example", "/apply/documents", "Supporting documents"),
    ("senior@demo.ese.example", "/staff/dashboard", "Dashboard"),
    ("admissions@demo.ese.example", "/staff", "Applications list"),
    ("admissions@demo.ese.example", "/staff?stage=offers", "Applications list — offers tab"),
    ("admissions@demo.ese.example", "/staff/agents", "Agent register"),
    ("admissions@demo.ese.example", "/staff/cas", "CAS register"),
    ("compliance@demo.ese.example", "/staff/students", "Students"),
    ("compliance@demo.ese.example", "/staff/events", "Reporting queue"),
    ("compliance@demo.ese.example", "/staff/events?view=overdue", "Reporting queue — overdue"),
    ("compliance@demo.ese.example", "/staff/compliance", "Compliance overview"),
    ("compliance@demo.ese.example", "/staff/reports", "Basic Compliance Assessment"),
    ("finance@demo.ese.example", "/staff", "Applications list as finance"),
    ("admin@demo.ese.example", "/admin/users", "User administration"),
]


def first_ids() -> dict[str, int]:
    """Find an application and a student to sweep the record screens against."""
    c = httpx.Client(base_url=API, timeout=30)
    token = c.post(
        "/auth/login", json={"email": "compliance@demo.ese.example", "password": STAFF_PASSWORD}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    apps = c.get("/applications?page=1", headers=headers).json()["items"]
    students = c.get("/students", headers=headers).json()["items"]
    out: dict[str, int] = {}
    if apps:
        out["application"] = apps[0]["id"]
    if students:
        out["student"] = students[0]["id"]
    return out


def sweep(page: Page) -> None:
    ids = first_ids()
    screens = list(SCREENS)
    if "application" in ids:
        screens += [
            ("admissions@demo.ese.example", f"/staff/applications/{ids['application']}", "Application record"),
            ("admissions@demo.ese.example", f"/staff/applications/{ids['application']}/fees", "Fees"),
            ("compliance@demo.ese.example", f"/staff/applications/{ids['application']}/precas", "Pre-CAS summary"),
        ]
    if "application" in ids:
        screens.append(("admissions@demo.ese.example",
                        f"/staff/applications/{ids['application']}?tab=audit", "Application audit trail"))
    if "student" in ids:
        screens += [
            ("compliance@demo.ese.example", f"/staff/students/{ids['student']}", "Student record"),
            ("compliance@demo.ese.example", f"/staff/students/{ids['student']}?tab=attendance", "Student record — attendance"),
            ("compliance@demo.ese.example", f"/staff/students/{ids['student']}?tab=engagement", "Student record — engagement"),
            ("compliance@demo.ese.example", f"/staff/students/{ids['student']}?tab=reporting", "Student record — reporting"),
            ("senior@demo.ese.example", f"/staff/students/{ids['student']}?tab=evidence", "Student record — evidence"),
        ]

    signed_in_as: str | None = None
    for account, path, label in screens:
        if account == "anonymous":
            if signed_in_as:
                sign_out(page)
                signed_in_as = None
        elif account != signed_in_as:
            if signed_in_as:
                sign_out(page)
            sign_in(page, account, STAFF_PASSWORD)
            signed_in_as = account
        page.goto(f"{BASE}{path}")
        page.wait_for_load_state("networkidle")
        audit(page, label)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="en-GB")
        try:
            sweep(page)
        finally:
            browser.close()

    Path("/tmp/claude-0/axe-full-sweep.json").write_text(json.dumps(ACCESSIBILITY, indent=2), encoding="utf-8")
    if ACCESSIBILITY:
        print(f"\n{len(ACCESSIBILITY)} serious or critical issue(s):", file=sys.stderr)
        for issue in ACCESSIBILITY:
            print(f"  [{issue['impact']}] {issue['screen']}: {issue['id']} — {issue['help']} {issue['nodes']}",
                  file=sys.stderr)  # fmt: skip
        return 1
    print("\nNo serious or critical WCAG 2.1 AA issues across the swept screens.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
