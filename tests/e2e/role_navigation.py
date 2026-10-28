"""Every role's workspace, walked end to end.

The prototype does not have one navigation — it has one per workspace, and a role only ever sees
what its own work needs. Two things can go wrong with that, and this walks both:

  positive — every item in a role's sidebar opens a page that actually loads. A link that 403s on
             its data endpoint is worse than a missing link, because the person cannot tell whether
             they lack permission or the system is broken.

  negative — a page outside a role's matrix is not reachable by typing its address. The server is
             the real guard (FR-122), but the client must not send someone to a dead end either.

Not part of the pytest suite: it needs a running server and a browser.

    RATE_LIMIT_AUTH_PER_MINUTE=400 uvicorn app.main:app --port 8000 &
    python3 tests/e2e/role_navigation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.sprint2_chain import BASE, STAFF_PASSWORD, go, sign_in, sign_out  # noqa: E402

ROLES = ["admissions", "compliance", "finance", "senior", "admin"]

#: What each role's sidebar is expected to hold, in order (LLD 6, reconciled with the shipped
#: route guards). Kept here rather than read from the page so that a nav item silently
#: disappearing is a failure, not an invisible change.
EXPECTED: dict[str, list[str]] = {
    "admissions": ["Dashboard", "Applications", "Assessments", "Offers", "CAS management",
                   "Students", "UKVI reporting", "Agents"],
    "compliance": ["Dashboard", "Applications", "CAS management", "Students", "Compliance",
                   "UKVI reporting", "Agents", "Reports"],
    "finance": ["Applications", "CAS management"],
    "senior": ["Dashboard", "Applications", "Assessments", "Offers", "CAS management", "Students",
               "Compliance", "UKVI reporting", "Agents", "Reports"],
    "admin": ["Dashboard", "Applications", "Assessments", "Offers", "CAS management", "Students",
              "Compliance", "UKVI reporting", "Agents", "Reports", "Administration"],
}

#: Addresses a role must not be able to reach by typing them, and what it is sent to instead.
FORBIDDEN: dict[str, list[str]] = {
    "admissions": ["/staff/compliance", "/staff/reports", "/admin/users"],
    "compliance": ["/admin/users"],
    "finance": ["/staff/dashboard", "/staff/students", "/staff/events", "/staff/compliance",
                "/staff/reports", "/staff/agents", "/admin/users"],
    "senior": ["/admin/users"],
    "admin": [],
}

FAILURES: list[str] = []


def sidebar_items(page: Page) -> list[str]:
    return [a.inner_text().strip() for a in page.locator(".sidebar nav a").all()]


def loaded(page: Page) -> bool:
    """True when the page rendered its own content rather than a failure or an empty frame.

    Screens that fetch before they render show a spinner first, so this waits for a heading
    rather than sampling once — otherwise a slow screen reads as a broken one.
    """
    try:
        page.wait_for_selector("main h1, main h2", timeout=15_000)
    except Exception:
        return False
    if page.locator("text=could not be loaded").count():
        return False
    return not page.locator("text=Page not found").count()


def walk_role(page: Page, role: str) -> None:
    email = f"{role}@demo.ese.example"
    sign_in(page, email, STAFF_PASSWORD)
    items = sidebar_items(page)
    print(f"\n{role}: {len(items)} nav item(s) — {', '.join(items)}")
    if items != EXPECTED[role]:
        FAILURES.append(f"{role}: sidebar is {items}, expected {EXPECTED[role]}")

    for label in items:
        page.locator(".sidebar nav a", has_text=label).first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(250)
        ok = loaded(page)
        print(f"    {'ok ' if ok else 'FAIL'} {label} -> {page.url.replace(BASE, '')}")
        if not ok:
            FAILURES.append(f"{role}: '{label}' ({page.url.replace(BASE, '')}) did not load")

    for path in FORBIDDEN[role]:
        go(page, BASE + path)
        page.wait_for_timeout(400)
        landed = page.url.replace(BASE, "")
        # Landing on "/" would mean the session was lost, which proves nothing about the guard.
        blocked = landed != path and landed != "/"
        print(f"    {'ok ' if blocked else 'FAIL'} {path} is out of scope -> {landed}")
        if not blocked:
            FAILURES.append(f"{role}: reached {path}, which is outside its matrix")

    sign_out(page)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 950}, locale="en-GB")
        for role in ROLES:
            walk_role(page, role)

        # The prototype's own breakpoint is 820px: check the shell folds rather than scrolls.
        narrow = browser.new_page(viewport={"width": 390, "height": 844}, locale="en-GB")
        sign_in(narrow, "senior@demo.ese.example", STAFF_PASSWORD)
        go(narrow, f"{BASE}/staff/dashboard")
        narrow.wait_for_timeout(400)
        overflow = narrow.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        print(f"\nAt 390px the dashboard overflows horizontally by {overflow}px")
        if overflow > 1:
            FAILURES.append(f"dashboard scrolls sideways at 390px (by {overflow}px)")
        narrow.screenshot(path="docs/screenshots/s4-mobile-dashboard.png", full_page=True)
        browser.close()

    if FAILURES:
        print("\nProblems:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nEvery role's sidebar opens, and nothing outside a role's matrix is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
