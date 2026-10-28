#!/usr/bin/env python3
"""Capture the screenshots the user guide uses, by walking the real flow.

Reusing the test evidence would be quicker and wrong: those images are whatever state a test
happened to leave behind, at whatever width the test ran. A guide needs the same screen every
time, cropped to what the reader is being shown. This walks one application from registration to
CAS and captures each step as it passes, so re-running it after a change refreshes the guide.

    RATE_LIMIT_AUTH_PER_MINUTE=400 uvicorn app.main:app --port 8000 &
    python3 scripts/capture_guide_screenshots.py
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
from PIL import Image
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE}/api/v1"
OUT = ROOT / "docs" / "guide"
STAFF_PASSWORD = "Demo-Password-2026!"  # noqa: S105 — demo environment only
APPLICANT_PASSWORD = "Correct-Horse-Battery-9"  # noqa: S105 — demo environment only


def shot(page: Page, name: str, *, top: int = 0, height: int | None = None, full: bool = False,
         sel: str | None = None) -> None:  # fmt: skip
    """Save a screenshot cropped to the band the guide is talking about, so the reader sees the
    thing being described rather than a screen of whitespace around it. `sel` captures one element
    instead, which survives the reflow that a pixel offset does not."""
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / f"{name}.png"
    page.wait_for_timeout(450)
    if sel:
        page.locator(sel).screenshot(path=str(raw))
    else:
        page.screenshot(path=str(raw), full_page=full)
    img = Image.open(raw).convert("RGB")
    if top or height:
        bottom = min(top + height, img.height) if height else img.height
        img = img.crop((0, top, img.width, bottom))
    out = OUT / f"{name}.jpg"
    img.save(out, quality=80, optimize=True)
    raw.unlink()
    if not sel:
        trim_bottom(out)
    print(f"  {out.name}  {Image.open(out).size[0]}x{Image.open(out).size[1]}")


def trim_bottom(path: Path, *, left: int = 300, pad: int = 24) -> None:
    """Drop empty page background from the foot of a capture.

    A fixed crop height cannot follow content that reflows, so every shot is cut generously and
    then trimmed here. Only the area to the right of `left` is examined: the role sidebar is a
    solid block of colour that runs to the bottom of every staff screen and would otherwise look
    like content.
    """
    img = Image.open(path).convert("RGB")
    px = img.load()
    base = px[img.width - 4, img.height - 4]
    step = 6

    def blank(y: int) -> bool:
        return all(
            abs(px[x, y][0] - base[0]) + abs(px[x, y][1] - base[1]) + abs(px[x, y][2] - base[2]) < 24
            for x in range(left, img.width - 2, step)
        )

    y = img.height - 1
    while y > 0 and blank(y):
        y -= 1
    bottom = min(img.height, y + pad)
    if bottom < img.height - pad:
        img.crop((0, 0, img.width, bottom)).save(path, quality=80, optimize=True)


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{BASE}/")
    page.wait_for_load_state("networkidle")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)


def sign_out(page: Page) -> None:
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(f"{BASE}/")


def go(page: Page, url: str) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle")


def token(c: httpx.Client, email: str, password: str = STAFF_PASSWORD) -> dict[str, str]:
    r = c.post("/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main() -> int:  # noqa: PLR0915 — a linear walkthrough; splitting it would only hide the order
    from tests.e2e.sprint2_chain import submitted_application

    email, app_id, ref = submitted_application()
    print(f"Application {ref} (id {app_id}) submitted by {email}")
    record = f"{BASE}/staff/applications/{app_id}"
    c = httpx.Client(base_url=API, timeout=30)
    admissions = token(c, "admissions@demo.ese.example")
    compliance = token(c, "compliance@demo.ese.example")
    finance = token(c, "finance@demo.ese.example")
    applicant = token(c, email, APPLICANT_PASSWORD)

    with sync_playwright() as p:
        # --lang as well as locale: Chromium renders a date input in its *UI* language, so without
        # it the guide would show UK course dates as mm/dd/yyyy.
        browser = p.chromium.launch(args=["--lang=en-GB"])
        page = browser.new_page(viewport={"width": 1024, "height": 1300}, locale="en-GB",
                                timezone_id="Europe/London")  # fmt: skip

        # --- Signing in, and the applicant's own screens -----------------------------------------
        go(page, f"{BASE}/")
        shot(page, "01-sign-in", height=1000)

        fresh = f"guide.{date.today():%Y%m%d}.{app_id}@example.com"
        c.post("/auth/register", json={"email": fresh, "password": APPLICANT_PASSWORD,
                                       "full_name": "Maria Fernanda Silva"}).raise_for_status()  # fmt: skip
        sign_in(page, fresh, APPLICANT_PASSWORD)
        go(page, f"{BASE}/apply?step=1")
        shot(page, "02-application-form", height=1150)
        go(page, f"{BASE}/apply/documents")
        shot(page, "03-documents", height=970)
        sign_out(page)

        sign_in(page, email, APPLICANT_PASSWORD)
        go(page, f"{BASE}/my")
        shot(page, "04-my-application", height=730)
        sign_out(page)

        # --- Admissions ---------------------------------------------------------------------------
        sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff")
        shot(page, "05-pipeline", height=840)
        go(page, f"{record}/academic")
        shot(page, "06-academic-assessment", full=True, height=1510)
        sign_out(page)

        checks = c.get("/reference/checklists/academic", headers=admissions).json()
        c.post(f"/applications/{app_id}/academic", headers=admissions, json={
            "checklist": {i["item_key"]: True for i in checks},
            "outcome": "recommend_offer", "complete": True,
            "comments": "Strong academic background and a clear, credible study rationale.",
            "english_evidence": "slt_test", "english_standard": "IELTS UKVI 6.5, no band below 6.0",
        }).raise_for_status()  # fmt: skip

        sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{record}/offer")
        shot(page, "07-offer-builder", height=1050)
        sign_out(page)

        offer = c.post(f"/applications/{app_id}/offer", headers=admissions, json={
            "offer_type": "conditional",
            "conditions": ["Pay the tuition fee deposit in full", "Provide the original of your passport"],
        }).json()  # fmt: skip
        c.post(f"/offers/{offer['id']}/issue", headers=admissions,
               json={"issue_method": "email"}).raise_for_status()  # fmt: skip

        sign_in(page, email, APPLICANT_PASSWORD)
        go(page, f"{BASE}/my/offer")
        shot(page, "08-offer-response", height=790)
        sign_out(page)

        # --- The controls, before anything has been satisfied ---------------------------------------
        sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{record}/precas")
        shot(page, "09-precas-blocked", height=1130)
        sign_out(page)

        c.post(f"/offers/{offer['id']}/response", headers=applicant,
               json={"response": "accepted"}).raise_for_status()  # fmt: skip

        # --- Finance ----------------------------------------------------------------------------------
        sign_in(page, "finance@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{record}/fees")
        shot(page, "10-fees", height=1130)
        sign_out(page)

        c.post(f"/applications/{app_id}/payments", headers=finance, json={
            "type": "deposit", "amount": "3000.00", "payment_date": date.today().isoformat(),
            "reference": "HSBC-0927-0114", "method": "bank_transfer",
        }).raise_for_status()  # fmt: skip
        for cond in c.get(f"/applications/{app_id}", headers=admissions).json()["offer"]["conditions"]:
            c.patch(f"/offers/{offer['id']}/conditions/{cond['id']}", headers=admissions,
                    json={"status": "met"}).raise_for_status()  # fmt: skip

        # --- Compliance --------------------------------------------------------------------------------
        sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{record}/compliance")
        shot(page, "11-compliance-review", full=True, height=1430)
        sign_out(page)

        cchecks = c.get("/reference/checklists/compliance", headers=compliance).json()
        c.post(f"/applications/{app_id}/compliance", headers=compliance, json={
            "checklist": {i["item_key"]: True for i in cchecks}, "risk_rating": "low",
            "atas_required": False, "complete": True,
            "comments": "Documents verified, no credibility concerns. Suitable to proceed to CAS.",
        }).raise_for_status()  # fmt: skip

        # --- Authorising Officer: assign the CAS -----------------------------------------------------
        sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{record}/precas")
        shot(page, "12-precas-ready", height=1020)

        go(page, f"{record}/cas")
        page.get_by_label("CAS number").fill(f"E4G{date.today():%y%m%d}{app_id:05d}")
        page.get_by_label("Course end date").fill((date.today() + timedelta(days=730)).isoformat())
        page.get_by_label("Level 1 user who created the CAS").fill("Lavinia Lucchesi")
        shot(page, "13-cas-form", full=True, height=1370)

        page.get_by_role("button", name="Assign CAS").click()
        page.wait_for_selector("[role=alertdialog]")
        page.wait_for_timeout(300)
        page.locator("[role=alertdialog]").scroll_into_view_if_needed()
        # Only the confirmation panel: the form above it is already shown as 13.
        shot(page, "14-cas-confirm", sel="[role=alertdialog]")
        page.get_by_role("alertdialog").get_by_role("button", name="Assign CAS").click()
        page.wait_for_timeout(2000)

        go(page, f"{record}/cas")
        shot(page, "15-cas-assigned", height=1020)
        go(page, f"{BASE}/staff/dashboard")
        shot(page, "16-dashboard", height=700)
        go(page, f"{BASE}/staff/cas")
        shot(page, "17-cas-register", height=920)
        sign_out(page)

        # --- The student record, once they are enrolled --------------------------------------------------
        student = c.get(f"/applications/{app_id}/student", headers=compliance).json()
        sid = student["id"]
        c.post(f"/applications/{app_id}/visa-outcome", headers=compliance,
               json={"outcome": "granted", "decision_date": date.today().isoformat()}).raise_for_status()  # fmt: skip
        c.post(f"/students/{sid}/enrol", headers=compliance, json={
            "enrolment_date": date.today().isoformat(),
            "permission_expiry": (date.today() + timedelta(days=500)).isoformat(),
        }).raise_for_status()  # fmt: skip
        sessions = [(0, "BUS701 Strategic Management", "present"),
                    (2, "BUS702 Financial Strategy", "present"),
                    (4, "BUS703 Global Markets", "absent"),
                    (7, "BUS701 Strategic Management", "authorised_absence")]  # fmt: skip
        for offset, module, status in sessions:
            body = {"session_date": (date.today() - timedelta(days=offset)).isoformat(),
                    "module": module, "status": status}  # fmt: skip
            if status == "authorised_absence":
                body["absence_reason"] = "Medical appointment; letter on file."
            c.post(f"/students/{sid}/attendance", headers=compliance, json=body).raise_for_status()

        sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/students/{sid}")
        shot(page, "18-student-record", height=1050)
        go(page, f"{BASE}/staff/students/{sid}?tab=attendance")
        shot(page, "19-attendance", height=1050)

        c.post(f"/students/{sid}/status-change", headers=compliance, json={
            "type": "deferral", "effective_date": date.today().isoformat(),
            "reason": "Student has asked to defer to the January intake for family reasons.",
        }).raise_for_status()  # fmt: skip
        go(page, f"{BASE}/staff/students/{sid}?tab=reporting")
        shot(page, "20-reporting-tab", height=1000)
        go(page, f"{BASE}/staff/events")
        shot(page, "21-reporting-queue", height=510)
        go(page, f"{BASE}/staff/compliance")
        shot(page, "22-compliance-monitoring", height=550)
        go(page, f"{BASE}/staff/reports")
        shot(page, "23-reports", height=900)
        sign_out(page)

        # --- Administrator ---------------------------------------------------------------------------------
        sign_in(page, "admin@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/admin/users")
        shot(page, "24-administration", height=950)
        sign_out(page)

        # --- Agents -------------------------------------------------------------------------------------------
        sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/agents")
        shot(page, "25-agents", height=900)

        browser.close()

    print(f"\nGuide screenshots in {OUT} (application {ref}, student {sid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
