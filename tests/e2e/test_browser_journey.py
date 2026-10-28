"""End-to-end browser journey (Playwright + Chromium) against a running server.

Skipped unless E2E_BASE_URL is set. The server must run with MAIL_BACKEND=console and write its
log to E2E_SERVER_LOG so the test can read the emailed confirmation link:

    uvicorn app.main:app --port 8000 > var/uvicorn.log 2>&1 &
    E2E_BASE_URL=http://localhost:8000 E2E_SERVER_LOG=var/uvicorn.log pytest tests/e2e -p no:cacheprovider

Screenshots go to E2E_SCREENSHOTS (default var/screenshots).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="E2E_BASE_URL not set")

SHOTS = Path(os.environ.get("E2E_SCREENSHOTS", "var/screenshots"))
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PASSWORD = "Tea-Kettle-Orbit-42"


@pytest.fixture(autouse=True)
def clean_state():
    """Overrides the database cleanup fixture: this module talks to a running server."""
    yield


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def email_link(kind: str, recipient: str) -> str:
    log = Path(os.environ.get("E2E_SERVER_LOG", "var/uvicorn.log"))
    for _ in range(40):
        text = log.read_text(encoding="utf-8", errors="replace")
        blocks = [b for b in text.split("EMAIL (console backend)") if f"to={recipient}" in b and kind in b]
        if blocks:
            return re.search(r"(https?://\S+token=[A-Za-z0-9_\-]+\S*)", blocks[-1]).group(1)
        time.sleep(0.25)
    raise AssertionError(f"no {kind} email for {recipient}")


def fill_select(page, label: str, text: str) -> None:
    page.get_by_label(label, exact=True).select_option(label=text)


def test_applicant_journey_and_staff_view(browser):
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    # Welcome and registration
    page.goto(BASE)
    shot(page, "01-welcome")
    page.get_by_role("link", name="Create an account").click()
    page.get_by_label("Full name").fill("Anika Sharma")
    page.get_by_label("Email address").fill(email)
    page.get_by_label("Create a password").fill(PASSWORD)
    page.get_by_role("button", name="Create account").click()
    page.wait_for_url(re.compile(r"/apply\?step=1"))
    page.get_by_role("heading", name="Personal details").wait_for()
    assert page.get_by_text("Confirm your email address").is_visible()

    # Confirm email via the emailed link
    link = email_link("Confirm your email address", email).replace("http://localhost:8000", BASE)
    page.goto(link)
    page.get_by_text("Email address confirmed").wait_for()

    # Step 1 — deliberately malformed mobile first, to see field-level errors
    page.goto(f"{BASE}/apply?step=1")
    page.get_by_role("heading", name="Personal details").wait_for()
    page.get_by_label("Date of birth").fill("2004-05-17")
    page.get_by_label("Female").check()
    fill_select(page, "Country of birth (optional)", "India")
    page.get_by_label("Mobile number").fill("07700 900123")
    page.get_by_role("button", name="Save and continue").click()
    page.get_by_text("Some answers need correcting").wait_for()
    shot(page, "02-step1-validation")
    page.get_by_label("Mobile number").fill("+91 98123 45678")
    page.get_by_role("button", name="Save and continue").click()

    # Step 2
    page.get_by_role("heading", name="Identity and nationality").wait_for()
    fill_select(page, "Nationality", "India")
    page.get_by_label("Passport number").fill("Z1234567")
    page.get_by_label("Passport issue date").fill("2022-03-01")
    page.get_by_label("Passport expiry date").fill("2032-03-01")
    fill_select(page, "Issuing country", "India")
    page.get_by_role("button", name="Save and continue").click()

    # Step 3
    page.get_by_role("heading", name="Address and contact").wait_for()
    home = page.locator("fieldset", has_text="Home address")
    home.get_by_label("Address line 1").fill("12 MG Road")
    home.get_by_label("Town or city").fill("Kochi")
    home.get_by_label("Country").select_option(label="India")
    page.get_by_role("button", name="Save and continue").click()

    # Step 4
    page.get_by_role("heading", name="Programme and intake").wait_for()
    page.get_by_label("Programme", exact=True).select_option(index=1)
    page.get_by_label("Intake", exact=True).select_option(index=1)
    page.get_by_label("Full-time").check()
    page.get_by_role("button", name="Save and continue").click()

    # Step 5
    page.get_by_role("heading", name="Academic background").wait_for()
    page.get_by_role("button", name="Add qualification").click()
    q = page.locator("fieldset", has_text="qualification 1")
    q.get_by_label("Institution").fill("St Teresa's College")
    q.get_by_label("Qualification title").fill("Higher Secondary Certificate")
    q.get_by_label("Level").select_option(label="Secondary school / high school")
    q.get_by_label("Country").select_option(label="India")
    q.get_by_label("Start date").fill("2020-06-01")
    q.get_by_label("End date").fill("2022-03-31")
    q.get_by_label("Grade or result").fill("91%")
    page.get_by_label("Highest level of study completed").select_option(label="Secondary school / high school")
    shot(page, "03-step5-academic")
    page.get_by_role("button", name="Save and continue").click()

    # Step 6
    page.get_by_role("heading", name="English language").wait_for()
    page.get_by_label("A Secure English Language Test (SELT)").check()
    page.get_by_label("Test provider and test name").fill("IELTS Academic for UKVI")
    page.get_by_label("Test reference number").fill("24IN012345RAMP001A")
    page.get_by_label("Test date").fill(time.strftime("%Y-%m-%d", time.localtime(time.time() - 90 * 86400)))
    for part, score in (("Overall", "6.5"), ("Reading", "6.5"), ("Writing", "6"), ("Listening", "7"), ("Speaking", "6.5")):
        page.get_by_label(part, exact=True).fill(score)
    page.get_by_role("button", name="Save and continue").click()

    # Step 7
    page.get_by_role("heading", name="UK immigration history").wait_for()
    page.locator("#f-been_to_uk").get_by_label("No").check()
    page.locator("#f-previous_visa_refusal").get_by_label("No").check()
    page.get_by_role("button", name="Save and continue").click()

    # Step 8
    page.get_by_role("heading", name="Supporting information").wait_for()
    page.get_by_label("Personal statement").fill(
        "I want to study international business at ESE because of its European perspective. " * 4)
    page.get_by_label("Your plans during and after your studies").fill("Build a career in international trade.")
    page.get_by_label("Family").check()
    page.get_by_label("Who is funding your studies?").fill("My parents, from savings.")
    page.get_by_role("button", name="Save and continue").click()

    # Step 9 — review shows outstanding documents; declaration
    page.get_by_role("heading", name="Declaration", exact=True, level=1).wait_for()
    page.get_by_text("Documents still to upload").wait_for()
    page.get_by_label(re.compile("I confirm that the information")).check()
    page.get_by_label(re.compile("I have read the privacy notice")).check()
    page.get_by_label("Type your full name to sign").fill("Anika Sharma")
    shot(page, "04-step9-declaration")

    # Documents
    # Supporting documents is a section of the wizard rail, not a page apart (prototype 3).
    page.get_by_role("link", name="Supporting documents").first.click()
    page.get_by_role("heading", name="Supporting documents").wait_for()
    page.get_by_label("Document type").select_option(label="Passport (photo page)")
    page.get_by_label("File", exact=True).set_input_files({"name": "passport.pdf", "mimeType": "application/pdf", "buffer": PDF})
    page.get_by_role("button", name="Upload").click()
    page.get_by_text("passport.pdf uploaded.").wait_for()
    page.get_by_label("Document type").select_option(label="Passport (photo page)")
    page.get_by_label("File", exact=True).set_input_files({"name": "malware.exe", "mimeType": "application/pdf", "buffer": b"MZ\x90\x00" * 20})
    page.get_by_role("button", name="Upload").click()
    page.get_by_text("Upload a PDF, JPG or PNG file.").wait_for()
    shot(page, "05-documents")

    # Submit
    page.get_by_role("link", name="Back to the declaration").click()
    page.get_by_role("heading", name="Declaration", exact=True, level=1).wait_for()
    page.get_by_label(re.compile("I confirm that the information")).check()
    page.get_by_label(re.compile("I have read the privacy notice")).check()
    page.get_by_label("Type your full name to sign").fill("Anika Sharma")
    page.get_by_role("button", name="Submit application").click()
    page.get_by_role("heading", name="Your application has been submitted").wait_for()
    ref = page.locator(".ref-box").inner_text()
    assert re.fullmatch(r"ESE-\d{4}-\d{6}", ref)
    shot(page, "06-submitted")
    page.get_by_role("link", name="View my application").click()
    page.get_by_text("Submitted — awaiting academic review").wait_for()
    shot(page, "07-my-application")
    ctx.close()

    # Staff: record with documents and audit trail
    staff = browser.new_context(viewport={"width": 1280, "height": 900})
    sp = staff.new_page()
    sp.goto(BASE)
    sp.get_by_label("Email address").fill("senior@demo.ese.example")
    sp.get_by_label("Password").fill("Demo-Password-2026!")
    sp.get_by_role("button", name="Sign in").click()
    # A senior user now lands on their dashboard, so the pipeline is one click away (nav.ts).
    sp.get_by_role("navigation", name="Sections").get_by_role("link", name="Applications").click()
    sp.get_by_role("heading", name="Applications").wait_for()
    sp.get_by_label("Search").fill(ref)
    sp.get_by_role("button", name="Search").click()
    sp.get_by_role("link", name=ref).click()
    # The audit trail has its own tab on the record (prototype screens 6-8).
    sp.get_by_role("navigation", name="Record sections").get_by_role("link", name="Audit trail").click()
    sp.get_by_role("heading", name="Audit trail").wait_for()
    sp.locator(".audit tbody tr").first.wait_for()
    trail = sp.locator(".audit tbody tr").all_inner_texts()
    assert any("application.submit" in row for row in trail)
    assert any("document.upload" in row for row in trail)
    shot(sp, "08-staff-record")
    staff.close()

    # Admin: users
    admin = browser.new_context(viewport={"width": 1280, "height": 900})
    ap = admin.new_page()
    ap.goto(BASE)
    ap.get_by_label("Email address").fill("admin@demo.ese.example")
    ap.get_by_label("Password").fill("Demo-Password-2026!")
    ap.get_by_role("button", name="Sign in").click()
    ap.get_by_role("navigation", name="Sections").get_by_role("link", name="Administration").click()
    ap.get_by_role("heading", name="Users").wait_for()
    shot(ap, "09-admin-users")
    admin.close()

    assert not [e for e in errors if "Content Security Policy" in e], errors
