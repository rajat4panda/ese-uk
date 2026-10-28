"""Browser walkthrough of the student-record sprint: enrolment with its evidence (FR-61), a
right-to-study check (FR-68), a change of address (FR-67) and the CAS withdrawal screen (FR-58).
Each screen gets the same axe-core sweep as the other walkthroughs.

Needs a running server with the demo data:

    RATE_LIMIT_AUTH_PER_MINUTE=200 uvicorn app.main:app --port 8000 &
    python3 tests/e2e/student_record.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.sprint2_chain import BASE, STAFF_PASSWORD, go, shot, sign_in, sign_out  # noqa: E402
from tests.e2e.sprint3_post_cas import ACCESSIBILITY, audit, cas_assigned_application, tab  # noqa: E402

PDF = Path("/tmp/student-record-evidence.pdf")


def main() -> int:
    PDF.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    _e1, _app1, _ref1, student_id = cas_assigned_application()
    _e2, _app2, _ref2, withdraw_id = cas_assigned_application()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # --- Enrolment with its evidence --------------------------------------------------------
        sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/students/{student_id}")
        page.get_by_label("Outcome").select_option("granted")
        page.get_by_role("button", name="Record decision").click()
        expect(page.get_by_text("Granted on")).to_be_visible()
        page.locator("#enrolment-evidence").set_input_files(str(PDF))
        page.get_by_role("button", name="Record enrolment").click()
        expect(page.get_by_role("button", name=PDF.name)).to_be_visible(timeout=10000)
        expect(page.get_by_text("Never")).to_be_visible()  # no check claimed until one is made
        shot(page, "sr-01-enrolled-with-evidence")
        audit(page, "student overview after enrolment")

        # --- Right-to-study check ---------------------------------------------------------------
        tab(page, "Right to study")
        expect(page.get_by_text("No right-to-study check has been recorded.")).to_be_visible()
        page.get_by_label("Share code").fill("W4K 9P2")
        page.get_by_label("Permission expires").fill((date.today() + timedelta(days=480)).isoformat())
        page.get_by_role("button", name="Record check").click()
        expect(page.get_by_text("Enter the 9-character share code").first).to_be_visible()
        page.get_by_label("Share code").fill("W4K 9P2 7XA")
        page.get_by_role("button", name="Record check").click()
        expect(page.get_by_role("cell", name="Right to study confirmed")).to_be_visible()
        expect(page.get_by_text("W4K9P27XA")).to_be_visible()
        shot(page, "sr-02-right-to-study")
        audit(page, "right to study")

        # --- Change of address -----------------------------------------------------------------
        tab(page, "Contact details")
        expect(page.get_by_text("No UK address is on record.")).to_be_visible()
        audit(page, "contact details before the change")
        page.get_by_label("Address line 1").fill("Flat 3, 20 Bishopsgate")
        page.get_by_label("Town or city").fill("London")
        page.get_by_label("Postcode").fill("EC2N 4AJ")
        page.get_by_label("Reason or source").fill("Student emailed the new address.")
        page.get_by_role("button", name="Record change").click()
        expect(page.get_by_role("cell", name="Student emailed the new address.", exact=False)).to_be_visible()
        expect(page.get_by_text("No UK address is on record.")).to_have_count(0)
        shot(page, "sr-03-contact-history")
        audit(page, "contact details with history")

        tab(page, "Overview")
        expect(page.get_by_text("Never")).to_have_count(0)
        sign_out(page)

        # --- CAS withdrawal (Authorising Officer) ------------------------------------------------
        sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
        go(page, f"{BASE}/staff/students/{withdraw_id}")
        expect(page.get_by_role("heading", name="Withdraw or cancel the CAS")).to_be_visible()
        audit(page, "CAS withdrawal panel")
        page.get_by_role("radio", name="Cancelled").check()
        page.get_by_label("Reason for the withdrawal").fill("Assigned with the wrong course dates; to be reissued.")
        page.get_by_role("button", name="Cancel CAS").click()
        dialog = page.get_by_role("alertdialog")
        expect(dialog).to_be_visible()
        shot(page, "sr-04-cas-cancel-confirm")
        audit(page, "CAS withdrawal confirmation")
        dialog.get_by_role("button", name="Cancel the CAS").click()
        expect(page.get_by_text("Cancelled with effect from")).to_be_visible(timeout=10000)
        tab(page, "Reporting")
        expect(page.get_by_text("CAS withdrawal").first).to_be_visible()
        shot(page, "sr-05-cas-cancelled")

        page.get_by_role("link", name="Dashboard").click()
        expect(page.get_by_role("heading", name="Key alerts")).to_be_visible()
        audit(page, "dashboard with record-keeping alerts")
        browser.close()

    for a in ACCESSIBILITY:
        print(f"  SERIOUS on {a['screen']}: {a['id']} — {a['help']} {a['nodes']}")
    if errors:
        print("Page errors:", errors)
        return 1
    if ACCESSIBILITY:
        return 1
    print("\nStudent-record screens walked end to end; no serious accessibility issues found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
