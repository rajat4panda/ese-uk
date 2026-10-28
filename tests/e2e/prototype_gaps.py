"""Browser walkthrough of the screens added to close the prototype gaps: Settings, the server-classified
compliance overview and dashboard alerts, the event detail with evidence and "+ New report", the
credibility interview and staff upload, and the student's Notes and Documents tabs. Each screen gets
the same axe-core sweep as the other walkthroughs.

Needs a running server with the demo data:

    RATE_LIMIT_AUTH_PER_MINUTE=200 uvicorn app.main:app --port 8000 &
    python3 tests/e2e/prototype_gaps.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.sprint2_chain import BASE, STAFF_PASSWORD, go, shot, sign_in, sign_out, submitted_application  # noqa: E402
from tests.e2e.sprint3_post_cas import ACCESSIBILITY, audit, cas_assigned_application, tab  # noqa: E402

PDF = Path("/tmp/prototype-gaps-evidence.pdf")


def main() -> int:
    PDF.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    _email, app_id, _ref = submitted_application()
    cas_assigned_application()  # a sponsored student, awaiting arrival, for "+ New report"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # --- Settings (Authorising Officer) ------------------------------------------------------
        sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
        page.get_by_role("link", name="Settings").click()
        expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
        page.get_by_role("button", name="Change Attendance threshold").click()
        page.get_by_label("Attendance threshold").fill("150")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("Enter a value from 1 to 100.")).to_be_visible()
        page.get_by_label("Attendance threshold").fill("80")
        page.get_by_role("button", name="Save", exact=True).click()
        expect(page.get_by_text("That is already the current value.")).to_be_visible()
        page.get_by_role("button", name="Cancel").click()
        audit(page, "settings: compliance")
        shot(page, "gap-01-settings")
        page.get_by_role("link", name="Programmes and intakes").click()
        expect(page.get_by_role("heading", name="Programmes", exact=True)).to_be_visible()
        audit(page, "settings: programmes and intakes")
        page.get_by_role("link", name="Fees", exact=True).click()
        expect(page.get_by_role("heading", name="Fees by programme and intake")).to_be_visible()
        audit(page, "settings: fees")
        page.get_by_role("link", name="CAS allocation").click()
        expect(page.get_by_role("heading", name="Allocation periods")).to_be_visible()
        audit(page, "settings: CAS allocation")

        # --- Dashboard and compliance overview --------------------------------------------------
        page.get_by_role("link", name="Dashboard").click()
        expect(page.get_by_role("heading", name="Key alerts")).to_be_visible()
        audit(page, "dashboard")
        page.get_by_role("link", name="Compliance", exact=True).click()
        expect(page.get_by_role("heading", name="Compliance overview")).to_be_visible()
        shot(page, "gap-04-compliance")
        audit(page, "compliance overview")
        sign_out(page)

        # --- Reporting queue: + New report, then the event detail with evidence -----------------
        sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/events")
        page.get_by_role("button", name="+ New report").click()
        student_select = page.get_by_label("Student")
        expect(student_select.locator("option").nth(1)).to_be_attached(timeout=10000)
        student_select.select_option(index=1)
        page.get_by_label("What has changed").select_option("change_of_circumstances")
        page.get_by_label("Reason", exact=True).fill("Student reported a change of UK address by email.")
        page.get_by_role("button", name="Record change of circumstances").click()
        page.get_by_role("alertdialog").get_by_role("button", name="Record and raise the event").click()
        page.wait_for_load_state("networkidle")
        audit(page, "reporting queue with new report")
        page.get_by_role("link", name="View Change of circumstances event").first.click()
        expect(page.get_by_role("heading", name="Evidence of the report")).to_be_visible()
        page.locator("#event-evidence").set_input_files(str(PDF))
        page.get_by_role("button", name="Upload evidence").click()
        expect(page.get_by_text("Evidence uploaded and scanned.")).to_be_visible()
        expect(page.get_by_text("Evidence of report attached").first).to_be_visible()
        shot(page, "gap-05-event-detail")
        audit(page, "event detail")
        page.locator("dl.kv a").first.click()  # the student, from the event's Student row
        page.wait_for_url("**/staff/students/*")
        page.wait_for_load_state("networkidle")

        # --- Student record: Notes and Documents -----------------------------------------------
        tab(page, "Notes")
        note = f"Called the student to confirm the new address (run {PDF.stat().st_mtime_ns})."
        page.get_by_label("Note", exact=True).fill(note)
        page.get_by_role("button", name="Add note").click()
        expect(page.get_by_text(note)).to_be_visible()
        audit(page, "student notes")
        tab(page, "Documents")
        page.locator("#student-file").set_input_files(str(PDF))
        page.get_by_role("button", name="Upload", exact=True).click()
        expect(page.get_by_role("cell", name=PDF.name).first).to_be_visible()
        audit(page, "student documents")
        sign_out(page)

        # --- Academic assessment: interview and staff upload ------------------------------------
        sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/applications/{app_id}")
        page.get_by_label("Document type").select_option("interview_notes")
        page.locator("#staff-file").set_input_files(str(PDF))
        page.get_by_role("button", name="Upload", exact=True).click()
        expect(page.get_by_text("Interview notes uploaded and scanned.")).to_be_visible()
        audit(page, "application record with staff upload")
        page.get_by_role("link", name="Academic assessment").click()
        page.get_by_label("Interview date").fill((date.today() - timedelta(days=1)).isoformat())
        page.get_by_label("Interviewer").fill("Demo Admissions")
        page.get_by_role("radio", name="Satisfactory", exact=True).check()
        expect(page.get_by_text("(1 on file)")).to_be_visible()
        page.get_by_role("button", name="Save without completing").click()
        page.wait_for_load_state("networkidle")
        audit(page, "academic assessment with interview")
        shot(page, "gap-08-interview")
        browser.close()

    for a in ACCESSIBILITY:
        print(f"  SERIOUS on {a['screen']}: {a['id']} — {a['help']} {a['nodes']}")
    if errors:
        print("Page errors:", errors)
        return 1
    if ACCESSIBILITY:
        return 1
    print("\nPrototype-gap screens walked end to end; no serious accessibility issues found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
