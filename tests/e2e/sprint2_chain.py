"""Browser walkthrough of the Sprint 2 chain: submitted → academic → offer → acceptance →
deposit → compliance → pre-CAS → CAS.

Not part of the pytest suite (it needs a running server and a browser). Seed first with
``python -m app.cli seed-reference`` and ``python -m app.cli seed-demo``, then run:

    python3 tests/e2e/sprint2_chain.py

It drives the real screens with Playwright, takes a screenshot at each stage into
docs/screenshots/, and fails loudly if a control does not behave as the LLD requires.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect, sync_playwright

# Where the running application is. Defaults to a server on this machine; the containerised test
# service sets E2E_BASE_URL, because from inside a container "localhost" is the container itself.
BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE}/api/v1"
SHOTS = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
STAFF_PASSWORD = "Demo-Password-2026!"
APPLICANT_PASSWORD = "Correct-Horse-Battery-9"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def confirm(page: Page, label: str) -> None:
    """Complete a confirmation step: the dialog's own button, not the one that opened it."""
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=label, exact=True).click()


def check_all(page: Page) -> None:
    """Tick every checkbox on the page, once the list has actually rendered."""
    page.locator("input[type=checkbox]").first.wait_for(state="visible")
    for box in page.locator("input[type=checkbox]").all():
        box.check()


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


# ---------------------------------------------------------------------------------------------
# Set up: an applicant with a submitted application (the Sprint 1 journey, driven by the API)
# ---------------------------------------------------------------------------------------------
def submitted_application() -> tuple[str, int, str]:
    from sqlalchemy import create_engine, text

    from app.config import get_settings

    stamp = date.today().strftime("%Y%m%d")
    suffix = httpx.get(f"{API}/health").headers.get("x-request-id", "x")[-6:]
    email = f"priya.{stamp}.{suffix}@example.com"
    c = httpx.Client(base_url=API, timeout=30)
    r = c.post("/auth/register", json={"email": email, "password": APPLICANT_PASSWORD, "full_name": "Priya Raman"})
    r.raise_for_status()
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    engine = create_engine(get_settings().MIGRATION_DATABASE_URL or get_settings().DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET email_verified_at = now() WHERE email = :e"), {"e": email})
        programme_id, intake_id, start = conn.execute(
            text("SELECT f.programme_id, f.intake_id, i.start_date FROM fee_schedules f "
                 "JOIN intakes i ON i.id = f.intake_id WHERE f.effective_to IS NULL "
                 "ORDER BY f.id DESC LIMIT 1")
        ).one()

    app = c.post("/applications", headers=headers).json()
    data = {
        "full_name": "Priya Raman", "date_of_birth": "2004-05-17", "sex": "female", "country_of_birth": "IN",
        "email": email, "mobile": "+919812345678", "nationality": "IN", "passport_number": "Z1234567",
        "passport_issue_date": "2022-03-01",
        "passport_expiry_date": (start + timedelta(days=5 * 365)).isoformat(), "passport_issuing_country": "IN",
        "home_address": {"line1": "12 MG Road", "city": "Kochi", "region": "Kerala", "postcode": "682016",
                         "country": "IN"},
        "programme_id": programme_id, "intake_id": intake_id, "study_mode": "full_time",
        "how_heard": "ese_website",
        "qualifications": [{"institution": "St Teresa's College", "title": "Higher Secondary Certificate",
                            "level": "secondary_school", "country": "IN", "start_date": "2020-06-01",
                            "end_date": "2022-03-31", "grade": "91%"}],
        "highest_level": "secondary_school", "english_route": "slt_test",
        "test_provider": "IELTS Academic (UKVI)", "test_reference": "24IN012345RAMP001A",
        "test_date": (date.today() - timedelta(days=90)).isoformat(),
        "test_scores": {"overall": 6.5, "reading": 6.5, "writing": 6.0, "listening": 7.0, "speaking": 6.5},
        "been_to_uk": False, "previous_visa_refusal": False,
        "personal_statement": "I want to study business at ESE because " + "x" * 220,
        "study_purpose": "To build a career in international business management after graduating.",
        "funding_source": "family", "funder_details": "My parents will fund tuition and living costs from savings.",
        "declaration_truthful": True, "declaration_data_consent": True, "declaration_signature_name": "Priya Raman",
    }
    c.put(f"/applications/{app['id']}", json={"data": data}, headers=headers).raise_for_status()
    submitted = c.post(f"/applications/{app['id']}/submit", headers=headers)
    submitted.raise_for_status()
    return email, app["id"], submitted.json()["ref"]


def go(page: Page, url: str) -> None:
    page.goto(url)
    page.wait_for_load_state("networkidle")


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{BASE}/")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")


def sign_out(page: Page) -> None:
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(f"{BASE}/")


def main() -> int:
    email, app_id, ref = submitted_application()
    print(f"Application {ref} (id {app_id}) submitted by {email}")
    record = f"{BASE}/staff/applications/{app_id}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="en-GB", timezone_id="Europe/London")
        try:
            walk(page, email, ref, record, app_id)
        except Exception:
            shot(page, "s2-failure")
            print(f"Failed at {page.url}\n{page.locator('main').inner_text()[:800]}", file=sys.stderr)
            raise
        finally:
            browser.close()
    print("Sprint 2 chain walked end to end. Screenshots in docs/screenshots/.")
    return 0


def walk(page: Page, email: str, ref: str, record: str, app_id: int) -> None:

    # --- Admissions: academic assessment ---------------------------------------------------
    sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
    go(page, record)
    # The case sidebar carries the reference (prototype screens 6-8); the heading names the stage.
    expect(page.get_by_role("complementary").get_by_text(ref, exact=True)).to_be_visible()
    shot(page, "s2-01-record")

    page.get_by_role("link", name="Academic assessment").click()
    page.wait_for_url(f"{record}/academic")
    check_all(page)
    page.get_by_label("Assessment notes").fill("Meets the entry requirements for the programme.")
    page.get_by_role("radio", name="Recommend an offer").check()
    # FR-36: the English tick now has to say what was accepted, and against what.
    page.get_by_label("Evidence accepted").select_option("slt_test")
    page.get_by_label("Standard it was checked against").fill("IELTS UKVI 6.0, no band below 5.5")
    # FR-37: the interview ticked as held is recorded; the notes tick needs the file, so it is left off.
    page.get_by_role("checkbox", name="Interview notes uploaded to the record").uncheck()
    page.get_by_label("Interview date").fill((date.today() - timedelta(days=1)).isoformat())
    page.get_by_label("Interviewer").fill("Demo Admissions")
    page.get_by_role("radio", name="Satisfactory", exact=True).check()
    shot(page, "s2-02-academic")
    page.get_by_role("button", name="Complete assessment").click()
    confirm(page, "Complete assessment")
    page.wait_for_url(f"{record}/offer")

    # --- Admissions: build and issue the offer ---------------------------------------------
    page.get_by_role("checkbox", name="Pay the tuition fee deposit in full").check()
    page.get_by_label("Offer expires").fill((date.today() + timedelta(days=30)).isoformat())
    shot(page, "s2-03-offer-builder")
    page.get_by_role("button", name="Create offer").click()
    expect(page.get_by_role("button", name="Issue offer")).to_be_visible()
    page.get_by_role("button", name="Issue offer").click()
    expect(page.get_by_text("Open the offer letter")).to_be_visible()
    shot(page, "s2-04-offer-issued")
    sign_out(page)

    # --- Applicant: accept the offer -------------------------------------------------------
    sign_in(page, email, APPLICANT_PASSWORD)
    expect(page.get_by_role("heading", name="You have an offer")).to_be_visible()
    page.get_by_role("link", name="View and respond to your offer").click()
    page.wait_for_url(f"{BASE}/my/offer")
    shot(page, "s2-05-applicant-offer")
    page.get_by_role("button", name="Accept this offer").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    shot(page, "s2-06-accept-confirm")
    confirm(page, "Accept offer")
    expect(page.get_by_text("You accepted this offer")).to_be_visible()
    sign_out(page)

    # --- Senior: the CAS is blocked before the deposit and the compliance review ------------
    sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/precas")
    expect(page.get_by_text("block CAS assignment")).to_be_visible()
    expect(page.get_by_role("button", name="Assign CAS")).to_be_disabled()
    shot(page, "s2-07-precas-blocked")
    sign_out(page)

    # --- Finance: record the deposit --------------------------------------------------------
    sign_in(page, "finance@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/fees")
    page.get_by_label("Amount (GBP)").fill("3000")
    page.get_by_label("Bank or receipt reference").fill("HSBC-2026-0918-441")
    page.get_by_role("button", name="Record payment").click()
    expect(page.get_by_text("Satisfied")).to_be_visible()
    shot(page, "s2-08-fees")
    sign_out(page)

    # --- Admissions: the deposit condition is now met ---------------------------------------
    sign_in(page, "admissions@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/offer")
    page.get_by_role("button", name="Mark met").first.click()
    expect(page.get_by_text("Met").first).to_be_visible()
    shot(page, "s2-09-condition-met")
    sign_out(page)

    # --- Compliance: the pre-CAS review -----------------------------------------------------
    sign_in(page, "compliance@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/compliance")
    check_all(page)
    page.get_by_role("radio", name="Medium").check()
    page.get_by_role("radio", name="No").check()
    page.get_by_label("Review notes").fill("Identity, funds and credibility all verified against the documents.")
    shot(page, "s2-10-compliance")
    page.get_by_role("button", name="Complete review").click()
    confirm(page, "Complete review")
    page.wait_for_url(f"{record}/precas")
    expect(page.get_by_text("Ready for CAS assignment")).to_be_visible()
    shot(page, "s2-11-precas-ready")
    sign_out(page)

    # --- Compliance colleague: second-line review (FR-52) -----------------------------------
    sign_in(page, "compliance2@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/compliance")
    page.get_by_role("button", name="Support the review").click()
    expect(page.get_by_text("Outcome: Supported")).to_be_visible()
    shot(page, "s2-12-second-line")
    sign_out(page)

    # --- Senior: assign the CAS --------------------------------------------------------------
    sign_in(page, "senior@demo.ese.example", STAFF_PASSWORD)
    go(page, f"{record}/precas")
    page.get_by_role("link", name="Assign CAS").last.click()
    page.wait_for_url(f"{record}/cas")
    page.get_by_label("CAS number").fill(f"E4G0{date.today():%y%m%d}{app_id:04d}")
    # FR-52: the figures come from the CAS itself; the ledger only pre-fills them.
    page.get_by_label("Tuition fee on the CAS").fill("15000.00")
    page.get_by_label("Amount paid on the CAS").fill("3000.00")
    page.get_by_label("Level 1 user who created the CAS").fill("Helen Whitmore (Level 1 User)")
    page.get_by_label("Course end date").fill((date.today() + timedelta(days=3 * 365)).isoformat())
    shot(page, "s2-13-cas-form")
    page.get_by_role("button", name="Assign CAS").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    shot(page, "s2-14-cas-confirm")
    confirm(page, "Assign CAS")
    page.wait_for_url(f"{record}/precas")
    expect(page.get_by_text("assigned", exact=False).first).to_be_visible()
    shot(page, "s2-15-cas-assigned")

    go(page, f"{BASE}/staff/cas")
    expect(page.get_by_role("heading", name="CAS register")).to_be_visible()
    expect(page.get_by_text("Priya Raman").first).to_be_visible()
    shot(page, "s2-16-cas-register")

    # The audit trail has a tab of its own, so the summary is not 25 rows deep (prototype 6-8).
    go(page, f"{record}?tab=audit")
    expect(page.get_by_text("cas.assign").first).to_be_visible()
    shot(page, "s2-17-audit")


if __name__ == "__main__":
    raise SystemExit(main())
