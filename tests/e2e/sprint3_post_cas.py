"""Browser walkthrough of the Sprint 3 chain: CAS assigned → visa granted → enrolled → attendance
→ deferral raising its reportable event → reported to UKVI → compliance overview → evidence pack.

It also runs an axe-core accessibility sweep over every screen it visits (NFR-11, WCAG 2.1 AA) and
fails if any serious or critical issue is found.

Not part of the pytest suite: it needs a running server and a browser. Seed first with
``python -m app.cli seed-reference`` and ``python -m app.cli seed-demo``, then run:

    RATE_LIMIT_AUTH_PER_MINUTE=200 uvicorn app.main:app --port 8000 &
    python3 tests/e2e/sprint3_post_cas.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.sprint2_chain import (  # noqa: E402
    API,
    BASE,
    STAFF_PASSWORD,
    confirm,
    go,
    shot,
    sign_in,
    sign_out,
    submitted_application,
)

# axe-core is a dev dependency of the web client, so the sweep needs no network access.
AXE_PATH = Path(__file__).resolve().parents[2] / "web" / "node_modules" / "axe-core" / "axe.min.js"
ACCESSIBILITY: list[dict] = []


def axe_source() -> str:
    if not AXE_PATH.exists():
        raise SystemExit("axe-core is missing. Run npm install in web/ before the accessibility sweep.")
    return AXE_PATH.read_text(encoding="utf-8")


def audit(page: Page, screen: str) -> None:
    """Run axe-core against the current screen and collect anything serious (NFR-11)."""
    # Evaluated rather than injected as a script tag: the application's CSP is script-src 'self',
    # and relaxing it for a test would be testing a different application.
    page.evaluate(axe_source())
    result = page.evaluate(
        "async () => await axe.run(document, {runOnly: {type: 'tag', "
        "values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}})"
    )
    for violation in result["violations"]:
        if violation["impact"] in ("serious", "critical"):
            ACCESSIBILITY.append(
                {
                    "screen": screen,
                    "id": violation["id"],
                    "impact": violation["impact"],
                    "help": violation["help"],
                    "nodes": [n["target"] for n in violation["nodes"][:3]],
                }
            )
    print(f"  axe {screen}: {len(result['violations'])} violation(s), "
          f"{sum(1 for v in result['violations'] if v['impact'] in ('serious', 'critical'))} serious")  # fmt: skip


def cas_assigned_application() -> tuple[str, int, str, int]:
    """Drive an application to an assigned CAS through the API, then hand the browser the record."""
    email, app_id, ref = submitted_application()
    c = httpx.Client(base_url=API, timeout=60)

    def token(user: str) -> dict[str, str]:
        r = c.post("/auth/login", json={"email": user, "password": STAFF_PASSWORD})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['token']}"}

    admissions = token("admissions@demo.ese.example")
    compliance = token("compliance@demo.ese.example")
    finance = token("finance@demo.ese.example")
    senior = token("senior@demo.ese.example")
    applicant = c.post("/auth/login", json={"email": email, "password": "Correct-Horse-Battery-9"}).json()
    applicant_headers = {"Authorization": f"Bearer {applicant['token']}"}

    checks = c.get("/reference/checklists/academic", headers=admissions).json()
    c.post(f"/applications/{app_id}/academic", headers=admissions, json={
        # Every check except "interview notes uploaded": that tick needs the notes on file (FR-37).
        "checklist": {i["item_key"]: True for i in checks if i["item_key"] != "interview_notes_uploaded"},
        "outcome": "recommend_offer",
        "comments": "Meets the entry requirements.", "complete": True,
        # FR-36: the English tick carries what was accepted.
        "english_evidence": "slt_test", "english_standard": "IELTS UKVI 6.0",
        # FR-37: an interview ticked as held records who, when and what it found.
        "interview_date": (date.today() - timedelta(days=1)).isoformat(), "interviewer": "Demo Admissions",
        "interview_outcome": "satisfactory",
    }).raise_for_status()  # fmt: skip
    offer = c.post(f"/applications/{app_id}/offer", headers=admissions, json={
        "offer_type": "conditional", "conditions": ["Pay the tuition fee deposit in full"],
    }).json()  # fmt: skip
    c.post(f"/offers/{offer['id']}/issue", headers=admissions, json={"issue_method": "email"}).raise_for_status()
    c.post(f"/offers/{offer['id']}/response", headers=applicant_headers,
           json={"response": "accepted"}).raise_for_status()  # fmt: skip
    c.post(f"/applications/{app_id}/payments", headers=finance, json={
        "type": "deposit", "amount": "3000.00", "payment_date": date.today().isoformat(),
        "method": "bank_transfer", "reference": "HSBC-2026-0926-77",
    }).raise_for_status()  # fmt: skip
    for condition in offer["conditions"]:
        c.patch(f"/offers/{offer['id']}/conditions/{condition['id']}", headers=admissions,
                json={"status": "met"}).raise_for_status()  # fmt: skip
    compliance_checks = c.get("/reference/checklists/compliance", headers=compliance).json()
    c.post(f"/applications/{app_id}/compliance", headers=compliance, json={
        "checklist": {i["item_key"]: True for i in compliance_checks}, "risk_rating": "low",
        "atas_required": False, "comments": "All checks satisfied.", "complete": True,
    }).raise_for_status()  # fmt: skip
    # Keep the demonstration allocation ahead of repeated runs: an exhausted allocation is a
    # warning the walkthrough is not trying to exercise (Sprint 2 covers it).
    position = c.get("/cas", headers=senior).json()["allocation"]
    if position["remaining"] <= 0:
        c.post("/admin/allocations", headers=senior, json={
            "period_label": position["period_label"], "period_start": position["period_start"],
            "period_end": position["period_end"], "allocated_count": position["assigned"] + 10,
        }).raise_for_status()  # fmt: skip

    start = date.today() - timedelta(days=7)  # already started, so enrolment is due
    cas = c.post(f"/applications/{app_id}/cas", headers=senior, json={
        "cas_number": f"E4G{date.today():%y%m%d}{app_id:05d}",
        "course_title": "MSc International Management",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=365)).isoformat(),
        "tuition_fee": "15000.00", "amount_paid": "3000.00",
        "level1_user": "Helen Whitmore (Level 1 User)", "notes": "",
    })  # fmt: skip
    cas.raise_for_status()
    student = c.get(f"/applications/{app_id}/student", headers=compliance).json()
    return email, app_id, ref, student["id"]


def tab(page: Page, name: str) -> None:
    """The student record is tabbed (prototype screen 12); open a tab and wait for it to render."""
    page.get_by_role("navigation", name="Student record sections").get_by_role("link", name=name, exact=True).click()
    page.wait_for_timeout(350)


def walk(page: Page, app_id: int, ref: str, student_id: int) -> None:
    record = f"{BASE}/staff/students/{student_id}"

    # --- Compliance: the record as it stands after CAS assignment ---------------------------
    sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{BASE}/staff/students")
    expect(page.get_by_role("heading", name="Students")).to_be_visible()
    shot(page, "s3-01-students")
    audit(page, "students list")

    go(page, record)
    expect(page.get_by_role("navigation", name="Student record sections")).to_be_visible()
    shot(page, "s3-02-student-record")
    audit(page, "student record")

    # --- Visa decision (FR-85) ----------------------------------------------------------------
    page.get_by_label("Outcome").select_option("granted")
    page.get_by_role("button", name="Record decision").click()
    expect(page.get_by_text("Granted on")).to_be_visible()
    shot(page, "s3-03-visa-granted")

    # --- Enrolment (FR-72) ---------------------------------------------------------------------
    page.get_by_label("Permission to stay expires").fill((date.today() + timedelta(days=500)).isoformat())
    page.get_by_role("button", name="Record enrolment").click()
    expect(page.get_by_text("Enrolled", exact=False).first).to_be_visible()
    shot(page, "s3-04-enrolled")

    # --- Attendance, including an authorised absence (FR-74 to FR-76) ---------------------------
    tab(page, "Attendance")
    for offset, module, status in [(0, "BUS701 Strategy", "present"), (1, "BUS702 Finance", "absent"),
                                   (2, "BUS703 Markets", "authorised_absence")]:  # fmt: skip
        page.get_by_label("Session date").fill((date.today() - timedelta(days=offset)).isoformat())
        page.get_by_label("Module or session").fill(module)
        page.get_by_label("Attendance", exact=True).select_option(status)
        if status == "authorised_absence":
            page.get_by_label("Reason for the authorised absence").fill("Medical appointment, letter on file.")
        page.get_by_role("button", name="Record attendance").click()
        page.wait_for_timeout(400)
    expect(page.get_by_text("Sessions scheduled")).to_be_visible()
    shot(page, "s3-05-attendance")
    audit(page, "student record with attendance")

    # --- A deferral raises its reportable event atomically (T-20) --------------------------------
    tab(page, "Overview")
    page.get_by_label("What has changed").select_option("deferral")
    page.get_by_label("Date it took effect").fill(date.today().isoformat())
    page.get_by_label("Reason", exact=True).fill(
        "Student has asked to defer to the January intake for family reasons."
    )
    page.get_by_role("button", name="Record deferral").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    shot(page, "s3-06-deferral-confirm")
    audit(page, "status change confirmation")
    confirm(page, "Record and raise the event")
    tab(page, "Reporting")
    expect(page.get_by_role("heading", name="Reportable events")).to_be_visible()
    expect(page.get_by_text("Deferral").first).to_be_visible()
    shot(page, "s3-07-event-raised")
    sign_out(page)

    # --- The reporting queue and the clock (FR-93 to FR-96) ---------------------------------------
    sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{BASE}/staff/events")
    expect(page.get_by_role("heading", name="Reporting queue")).to_be_visible()
    shot(page, "s3-08-reporting-queue")
    audit(page, "reporting queue")

    page.get_by_role("button", name="Record report").first.click()
    page.get_by_label("SMS reference").fill(f"SMS-{date.today():%Y%m%d}-4471")
    page.get_by_role("button", name="Record report").last.click()
    expect(page.get_by_text("Reported").first).to_be_visible()
    shot(page, "s3-09-reported")

    # --- The compliance overview (FR-87 to FR-90) --------------------------------------------------
    go(page, f"{BASE}/staff/compliance")
    expect(page.get_by_role("heading", name="Compliance overview")).to_be_visible()
    shot(page, "s3-10-compliance-overview")
    audit(page, "compliance overview")

    # --- The evidence pack (FR-127, T-18) -----------------------------------------------------------
    # A senior action: the pack carries the payment history that LLD 6 withholds from compliance.
    sign_out(page)
    sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
    go(page, record)
    tab(page, "Evidence pack")
    page.get_by_role("button", name="Build evidence pack").click()
    expect(page.get_by_role("button", name="Download evidence-pack", exact=False)).to_be_visible(timeout=30_000)
    with page.expect_download() as download_info:
        page.get_by_role("button", name="Download evidence-pack", exact=False).click()
    download = download_info.value
    target = Path("/tmp/claude-0") / download.suggested_filename
    download.save_as(target)
    print(f"  evidence pack downloaded: {target} ({target.stat().st_size:,} bytes)")
    shot(page, "s3-11-evidence-pack")
    sign_out(page)

    # --- The applicant sees the CAS, not the compliance machinery ------------------------------------
    sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{BASE}/staff/applications/{app_id}?tab=audit")
    expect(page.get_by_text("cas.assign").first).to_be_visible()
    shot(page, "s3-12-audit-trail")
    audit(page, "application record audit trail")


def main() -> int:
    email, app_id, ref, student_id = cas_assigned_application()
    print(f"Application {ref} (id {app_id}) has CAS and student record {student_id}; applicant {email}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="en-GB", timezone_id="Europe/London",
            accept_downloads=True,
        )  # fmt: skip
        page = context.new_page()
        try:
            walk(page, app_id, ref, student_id)
        except Exception:
            shot(page, "s3-failure")
            print(f"Failed at {page.url}\n{page.locator('main').inner_text()[:800]}", file=sys.stderr)
            raise
        finally:
            browser.close()

    Path("/tmp/claude-0/axe-sprint3.json").write_text(json.dumps(ACCESSIBILITY, indent=2), encoding="utf-8")
    if ACCESSIBILITY:
        print(f"\n{len(ACCESSIBILITY)} serious or critical accessibility issue(s):", file=sys.stderr)
        for issue in ACCESSIBILITY:
            print(f"  [{issue['impact']}] {issue['screen']}: {issue['id']} — {issue['help']} {issue['nodes']}",
                  file=sys.stderr)  # fmt: skip
        return 1
    print("\nSprint 3 chain walked end to end; no serious accessibility issues found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
