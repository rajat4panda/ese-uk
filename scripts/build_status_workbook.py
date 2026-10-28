#!/usr/bin/env python3
"""Build the management project-status workbook from the assessed requirement matrix.

The counts on the summary sheet are COUNTIFS over the Requirements sheet rather than numbers
typed in, so if a status is edited the summary follows it. Everything else is evidence: the
requirement rows come from the 26 September assessment, with the statuses that have since
changed marked and attributed to the sprint that changed them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

MAROON = "641619"
RED = "A12E2C"
GOLD = "CABB87"
INK = "152238"
MUTED = "5B6B85"
LINE = "DDE4EF"
GREEN_FILL = "E7F6EC"
AMBER_FILL = "FDF3E3"
RED_FILL = "FDEAEA"
GREY_FILL = "EEF0F4"
BAND = "F7F9FD"

FONT = "Arial"
H1 = Font(name=FONT, size=16, bold=True, color=MAROON)
H2 = Font(name=FONT, size=11, bold=True, color=MAROON)
HEAD = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY = Font(name=FONT, size=10, color=INK)
BODY_B = Font(name=FONT, size=10, bold=True, color=INK)
SMALL = Font(name=FONT, size=9, color=MUTED)
BIG = Font(name=FONT, size=22, bold=True, color=MAROON)

HEAD_FILL = PatternFill("solid", fgColor=MAROON)
BAND_FILL = PatternFill("solid", fgColor=BAND)
THIN = Side(style="thin", color=LINE)
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
CENTRE = Alignment(horizontal="center", vertical="center")

STATUS_WORDS = {
    "D": "Delivered",
    "P": "Partial",
    "N": "Not built",
    "O": "Operational — depends on hosting",
    "E": "Not evidenced",
}


def header_row(ws, row: int, headers: list[tuple[str, int]]) -> None:
    for col, (text, width) in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = HEAD
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        cell.border = BOX
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[row].height = 28


def write_row(ws, row: int, values: list, *, band: bool, wrap_from: int = 1) -> None:
    for col, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font = BODY
        cell.border = BOX
        cell.alignment = WRAP if col >= wrap_from else TOP
        if band:
            cell.fill = BAND_FILL


def title_block(ws, title: str, subtitle: str, span: int) -> int:
    ws["A1"] = title
    ws["A1"].font = H1
    ws["A2"] = subtitle
    ws["A2"].font = SMALL
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=span)
    ws.row_dimensions[1].height = 22
    return 4


# --------------------------------------------------------------------------------------------
def page_setup(ws, *, header_row_index: int | None = None) -> None:
    """Landscape, one page wide. These sheets get printed and circulated, and a table that
    spills a column onto a second sheet of paper is how a status pack gets misread."""
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    if header_row_index is not None:
        ws.print_title_rows = f"{header_row_index}:{header_row_index}"


def build(reqs: list[dict], out: Path) -> None:
    wb = Workbook()

    # ---- 1. Status summary -------------------------------------------------------------------
    ws = wb.active
    ws.title = "Status summary"
    ws.sheet_view.showGridLines = False
    r = title_block(
        ws,
        "ESE Insight Limited — International Student Recruitment & Compliance System",
        "Project status for management · Student-route module of the eMigrant® platform · "
        "Finteam Ltd with OMFYS Technologies UK Ltd · as at 27 September 2026",
        6,
    )
    for col, width in zip("ABCDEF", (30, 16, 16, 16, 16, 44), strict=False):
        ws.column_dimensions[col].width = width

    ws.cell(row=r, column=1, value="Overall position").font = H2
    r += 1
    headline = (
        "The application-to-CAS chain the Student route submission depends on is built and tested. "
        "Of 96 assessed requirements, 60 are delivered, against 47 at the formal assessment two days ago. "
        "Every defect found by that assessment is fixed. Three functional Must requirements remain, all "
        "of them Phase 2 items due before the January 2027 intake; the rest of the outstanding work is "
        "non-functional — backups, user guides and hosting — and two inputs are needed from ESE."
    )
    cell = ws.cell(row=r, column=1, value=headline)
    cell.font = BODY
    cell.alignment = WRAP
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 2, end_column=6)
    r += 4

    # KPI tiles, computed from the Requirements sheet
    ws.cell(row=r, column=1, value="Requirements delivered").font = H2
    r += 1
    tiles = [
        ("Delivered", '=COUNTIF(Requirements!$F$5:$F$100,"D")', "of 96 assessed requirements"),
        ("Partial", '=COUNTIF(Requirements!$F$5:$F$100,"P")', "started, not finished"),
        ("Not built", '=COUNTIF(Requirements!$F$5:$F$100,"N")', "not started"),
        ("Must delivered",
         '=COUNTIFS(Requirements!$D$5:$D$100,"Must",Requirements!$F$5:$F$100,"D")',
         "of 60 Must requirements"),
    ]
    for i, (label, formula, note) in enumerate(tiles):
        col = 1 + i
        ws.cell(row=r, column=col, value=label).font = BODY_B
        v = ws.cell(row=r + 1, column=col, value=formula)
        v.font = BIG
        v.alignment = CENTRE
        n = ws.cell(row=r + 2, column=col, value=note)
        n.font = SMALL
        n.alignment = WRAP
        for rr in (r, r + 1, r + 2):
            ws.cell(row=rr, column=col).border = BOX
    ws.row_dimensions[r + 1].height = 32
    ws.row_dimensions[r + 2].height = 26
    r += 4

    # Movement by phase
    ws.cell(row=r, column=1, value="By delivery phase").font = H2
    r += 1
    header_row(ws, r, [("Phase", 46), ("Requirements", 14), ("Delivered", 12),
                       ("Partial", 12), ("Not built", 12), ("Delivered at the 26 Sep assessment", 34)])  # fmt: skip
    r += 1
    phases = [
        "Phase 1 — before the Student route submission",
        "Phase 2 — before the January 2027 intake",
        "Non-functional",
    ]
    first_phase_row = r
    for i, phase in enumerate(phases):
        ws.cell(row=r, column=1, value=phase)
        ws.cell(row=r, column=2, value=f'=COUNTIF(Requirements!$C$5:$C$100,$A{r})')
        ws.cell(row=r, column=3, value=f'=COUNTIFS(Requirements!$C$5:$C$100,$A{r},Requirements!$F$5:$F$100,"D")')
        ws.cell(row=r, column=4, value=f'=COUNTIFS(Requirements!$C$5:$C$100,$A{r},Requirements!$F$5:$F$100,"P")')
        ws.cell(row=r, column=5, value=f'=COUNTIFS(Requirements!$C$5:$C$100,$A{r},Requirements!$F$5:$F$100,"N")')
        ws.cell(row=r, column=6, value=f'=COUNTIFS(Requirements!$C$5:$C$100,$A{r},Requirements!$E$5:$E$100,"D")')
        for col in range(1, 7):
            c = ws.cell(row=r, column=col)
            c.font = BODY
            c.border = BOX
            c.alignment = WRAP if col == 1 else CENTRE
            if i % 2:
                c.fill = BAND_FILL
        r += 1
    total = r
    ws.cell(row=total, column=1, value="Total").font = BODY_B
    for col in range(2, 7):
        letter = get_column_letter(col)
        c = ws.cell(row=total, column=col,
                    value=f"=SUM({letter}{first_phase_row}:{letter}{total - 1})")  # fmt: skip
        c.font = BODY_B
        c.alignment = CENTRE
        c.border = BOX
    ws.cell(row=total, column=1).border = BOX
    r = total + 2

    ws.cell(row=r, column=1, value="What changed in the last two days").font = H2
    r += 1
    changes = [
        ("Interface rebuilt to the ESE-approved prototype",
         "All 15 prototype screens now have a counterpart, using ESE's own logo and imagery. "
         "Three screens that did not exist were built: the Authorising Officer dashboard, "
         "compliance monitoring and the agent register. Navigation is scoped by role."),
        ("Every defect from the assessment fixed",
         "Five defects, including two that would have shown first at a compliance visit: the "
         "evidence pack reported every document as unverified, and one attendance breach emailed "
         "the compliance inbox daily and indefinitely."),
        ("Nine Must requirements closed",
         "FR-08, FR-24, FR-35, FR-36, FR-52, FR-54, FR-55, FR-80 and FR-100."),
        ("A user guide for every role",
         "An end-to-end guide covering all six roles, with screenshots taken from the running "
         "system, so ESE staff can be trained and the flow walked through without our help. This "
         "moves NFR-14 from not started to partial; an administrator manual for operations is "
         "still to write."),
    ]
    for i, (what, detail) in enumerate(changes):
        ws.cell(row=r, column=1, value=what).font = BODY_B
        ws.cell(row=r, column=1).alignment = WRAP
        ws.cell(row=r, column=1).border = BOX
        d = ws.cell(row=r, column=2, value=detail)
        d.font = BODY
        d.alignment = WRAP
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        for col in range(1, 7):
            ws.cell(row=r, column=col).border = BOX
            if i % 2:
                ws.cell(row=r, column=col).fill = BAND_FILL
        ws.row_dimensions[r].height = 42
        r += 1
    r += 1

    ws.cell(row=r, column=1, value="Status key").font = H2
    r += 1
    for code, word in STATUS_WORDS.items():
        ws.cell(row=r, column=1, value=code).font = BODY_B
        ws.cell(row=r, column=1).alignment = Alignment(horizontal="left", vertical="center")
        ws.cell(row=r, column=2, value=word).font = BODY
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=6)
        r += 1
    ws.cell(row=r + 1, column=1,
            value="Requirement numbering follows BRD v1.0, which is the baseline in the ESE project "
                  "folder. The build is written to BRD v1.1, whose numbers differ; reconciling the two "
                  "is an open item.").font = SMALL  # fmt: skip
    ws.cell(row=r + 1, column=1).alignment = WRAP
    ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 2, end_column=6)
    ws.freeze_panes = "A5"
    page_setup(ws)

    # ---- 2. Requirements ----------------------------------------------------------------------
    ws = wb.create_sheet("Requirements")
    ws.sheet_view.showGridLines = False
    title_block(
        ws,
        "Requirement traceability",
        "Every requirement in BRD v1.0, its status at the 26 September assessment and its status now. "
        "Filter column F for what is outstanding, or column G for what has moved.",
        10,
    )
    headers = [
        ("Ref", 9), ("Area", 30), ("Phase", 34), ("Priority", 10),
        ("Status 26 Sep", 13), ("Status now", 11), ("Movement", 12), ("Closed in", 12),
        ("What is in the build", 62), ("What is still outstanding", 62),
    ]
    header_row(ws, 4, headers)
    row = 5
    for i, q in enumerate(reqs):
        moved = "Improved" if q["was"] != q["now"] else ""
        evidence = q["evidence"]
        gap = q["gap"] if q["gap"] != "—" else ""
        if q["note"]:
            # A requirement that moved describes what is in the build *now*. Left as it was, the
            # evidence column would still describe the state the assessment found, which is the
            # single most misleading thing a status report can do.
            if q["now"] == "D":
                evidence, gap = q["note"], ""
            else:
                gap = q["note"]
        write_row(ws, row, [
            q["ref"], q["section"], q["phase"], q["priority"],
            q["was"], q["now"], moved, q["changed_in"],
            evidence, gap,
        ], band=bool(i % 2), wrap_from=2)  # fmt: skip
        for col in (1, 4, 5, 6, 7):
            ws.cell(row=row, column=col).alignment = CENTRE
        row += 1
    last = row - 1
    ws.auto_filter.ref = f"A4:J{last}"
    ws.freeze_panes = "A5"
    page_setup(ws, header_row_index=4)

    # Status colours, applied to both status columns
    for col in ("E", "F"):
        rng = f"{col}5:{col}{last}"
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=['"D"'], fill=PatternFill("solid", fgColor=GREEN_FILL)))
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=['"P"'], fill=PatternFill("solid", fgColor=AMBER_FILL)))
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal", formula=['"N"'], fill=PatternFill("solid", fgColor=RED_FILL)))
        for code in ("O", "E"):
            ws.conditional_formatting.add(rng, CellIsRule(
                operator="equal", formula=[f'"{code}"'], fill=PatternFill("solid", fgColor=GREY_FILL)))
    dv = DataValidation(type="list", formula1='"D,P,N,O,E"', allow_blank=False)
    ws.add_data_validation(dv)
    dv.add(f"F5:F{last}")

    # ---- 3. Delivery timeline -------------------------------------------------------------------
    ws = wb.create_sheet("Delivery timeline")
    ws.sheet_view.showGridLines = False
    title_block(ws, "Delivery timeline",
                "What each sprint delivered, and how it was assured before being called done.", 5)
    header_row(ws, 4, [("Sprint", 22), ("Dates", 16), ("Scope delivered", 66),
                       ("Assurance", 50), ("Outcome", 14)])  # fmt: skip
    sprints = [
        ("Foundation", "21 Sep 2026",
         "Database schema, authentication, the nine-step application, document upload with malware "
         "scanning, and the immutable audit trail.",
         "Independent security review; findings acted on before sign-off.", "Complete"),
        ("Sprint 2 — admissions to CAS", "25 Sep 2026",
         "Academic assessment, offers and conditions, compliance review, fees and the deposit gate, "
         "and the CAS guard chain with segregation of duties.",
         "Independent review; browser walkthrough of the whole chain; concurrency test on CAS "
         "assignment against a real database.", "Complete"),
        ("Sprint 3 — post-CAS compliance", "26 Sep 2026",
         "Visa outcomes, enrolment and attendance, escalations, reportable events on a working-day "
         "clock, seven scheduled jobs, Basic Compliance Assessment metrics and evidence packs.",
         "Independent review, which found a transaction fault that discarded escalations; fixed and "
         "regression-tested. Accessibility sweep introduced.", "Complete"),
        ("Requirements assessment", "26 Sep 2026",
         "Formal re-assessment against BRD v1.0: 96 requirements, full traceability matrix.",
         "Counted programmatically rather than by eye; five defects found in scope already counted "
         "as delivered.", "Complete"),
        ("Interface rebuild", "26 Sep 2026",
         "Rebuilt against the ESE-approved 15-screen prototype using its own assets. Role-scoped "
         "navigation. Three new screens: Authorising Officer dashboard, compliance monitoring, "
         "agent register.",
         "Role navigation walk across all five staff roles; accessibility sweep extended to 28 "
         "screens; narrow-viewport check.", "Complete"),
        ("Sprint 4 — Must gaps and defects", "27 Sep 2026",
         "Nine Must requirements closed and all five defects fixed. A new application status, "
         "Offer Pending, was added to close FR-35.",
         "Independent review, which found six items including two introduced by the sprint; all "
         "fixed. Regression tests run against the old code to prove they fail without the fix.",
         "Complete"),
        ("User guide", "27 Sep 2026",
         "Step-by-step guide to the whole flow, by role, with 25 screenshots captured from the "
         "running application by a script that can be re-run whenever the interface changes.",
         "Every screenshot taken while walking the real application-to-CAS chain, so the guide "
         "cannot describe a screen that no longer exists.", "Complete"),
    ]
    row = 5
    for i, s in enumerate(sprints):
        write_row(ws, row, list(s), band=bool(i % 2), wrap_from=3)
        ws.cell(row=row, column=5).alignment = CENTRE
        ws.row_dimensions[row].height = 58
        row += 1
    ws.freeze_panes = "A5"
    page_setup(ws, header_row_index=4)

    # ---- 4. Open items ---------------------------------------------------------------------------
    ws = wb.create_sheet("Open items")
    ws.sheet_view.showGridLines = False
    title_block(ws, "Open items, decisions and risks",
                "Everything outstanding, with who has to move it. Sorted by what blocks the most.", 6)
    header_row(ws, 4, [("Item", 36), ("Type", 14), ("Owner", 18), ("Impact if unresolved", 56),
                       ("What is needed", 56), ("Priority", 11)])  # fmt: skip
    items = [
        ("Programme catalogue (decision D-05)", "Input", "ESE",
         "Every screen reads 'Postgraduate programme A (placeholder — awaiting ESE catalogue)'. "
         "The system cannot be shown to a stakeholder or used for real applications until this is real.",
         "Programme titles, qualification levels, durations, intakes, tuition fees and minimum deposits.",
         "High"),
        ("Backups and a tested restore (NFR-09)", "Build", "Finteam / OMFYS",
         "A Must requirement with nothing behind it. A sponsor that cannot restore its records cannot "
         "evidence them.",
         "Backup schedule, retention, and a restore rehearsed and written up.", "High"),
        ("User guides and administrator manual (NFR-14)", "Build", "Finteam / OMFYS",
         "A Must requirement. Staff cannot be trained on the compliance duties the system enforces.",
         "One short guide per role, and an administrator manual.", "High"),
        ("Hosting decision (D-02)", "Decision", "ESE with Finteam",
         "Availability, TLS, the database's own security posture and UK data residency all depend on it. "
         "Two Must NFRs sit in an operational state until it is settled.",
         "Where this runs, who operates it, and the support arrangement.", "High"),
        ("New 'Offer Pending' status — LLD amendment", "Decision", "ESE",
         "Closing FR-35 required a status the reviewed LLD does not have. The build and the document "
         "disagree until this is accepted.",
         "Acceptance of the amendment to LLD 7.1, or a different instruction.", "Medium"),
        ("BRD v1.1 is not in the project folder", "Input", "Finteam",
         "The project holds BRD v1.0; the build is written to v1.1 and the requirement numbers differ. "
         "Every status report has to say which numbering it uses.",
         "Upload BRD v1.1, or confirm v1.0 as the reporting baseline.", "Medium"),
        ("Agent management is read-only (FR-90, FR-91)", "Build", "Finteam / OMFYS",
         "Agents and their due-diligence records are maintained directly in the database, which is "
         "neither auditable nor safe for day-to-day use.",
         "Create, edit and suspend an agent, and attach agreement and due-diligence documents.",
         "Medium"),
        ("No configuration screen (FR-126 / FR-105)", "Build", "Finteam / OMFYS",
         "Thresholds, fee schedules and templates are changed in the database rather than by an "
         "administrator.",
         "An administration screen for the settings that already exist.", "Medium"),
        ("Phase 2 Must requirements (FR-61, FR-68, FR-76)", "Build", "Finteam / OMFYS",
         "Due before the January 2027 intake rather than before the submission, so not blocking today.",
         "Scheduling into a Phase 2 sprint.", "Medium"),
        ("Multi-factor authentication (NFR-07)", "Build", "Finteam / OMFYS",
         "A Should requirement. Sign-in is password-only for accounts that can assign a CAS.",
         "TOTP enrolment and challenge, at least for senior and admin roles.", "Medium"),
        ("Manual accessibility audit (NFR-11)", "Build", "Finteam / OMFYS",
         "The automated sweep is clean across 28 screens, but keyboard-only, screen-reader and 400% "
         "zoom testing has not been done, and the sweep is not yet in CI so it can regress unnoticed.",
         "One manual pass, and the sweep added to the build pipeline.", "Medium"),
        ("Mobile and tablet widths (NFR-01)", "Build", "Finteam / OMFYS",
         "Only the dashboard has been checked at phone width. Applicants apply on phones.",
         "A pass across the applicant journey at phone and tablet widths.", "Medium"),
        ("Training environment (NFR-13)", "Build", "ESE with Finteam",
         "Demo data seeds into the same configuration. There is no scripted refresh and no anonymised "
         "data procedure.",
         "A second environment and a refresh procedure.", "Low"),
        ("Load and volume testing (NFR-03, NFR-10)", "Build", "Finteam / OMFYS",
         "The three-second response target at 100 concurrent users is unevidenced. Two known "
         "compromises exist in live metric computation.",
         "A load test at the target concurrency.", "Low"),
    ]
    row = 5
    for i, item in enumerate(items):
        write_row(ws, row, list(item), band=bool(i % 2), wrap_from=4)
        ws.cell(row=row, column=6).alignment = CENTRE
        ws.cell(row=row, column=2).alignment = CENTRE
        ws.row_dimensions[row].height = 46
        row += 1
    ws.auto_filter.ref = f"A4:F{row - 1}"
    ws.freeze_panes = "A5"
    ws.conditional_formatting.add(f"F5:F{row - 1}", CellIsRule(
        operator="equal", formula=['"High"'], fill=PatternFill("solid", fgColor=RED_FILL)))
    ws.conditional_formatting.add(f"F5:F{row - 1}", CellIsRule(
        operator="equal", formula=['"Medium"'], fill=PatternFill("solid", fgColor=AMBER_FILL)))
    ws.conditional_formatting.add(f"F5:F{row - 1}", CellIsRule(
        operator="equal", formula=['"Low"'], fill=PatternFill("solid", fgColor=GREY_FILL)))
    page_setup(ws, header_row_index=4)

    # ---- 5. Quality and assurance ------------------------------------------------------------------
    ws = wb.create_sheet("Quality and assurance")
    ws.sheet_view.showGridLines = False
    title_block(ws, "Quality and assurance",
                "How the build is checked, and what those checks currently report. "
                "Measured on 27 September 2026.", 4)
    header_row(ws, 4, [("Measure", 40), ("Result", 22), ("What it covers", 62), ("Frequency", 22)])
    measures = [
        ("Automated test suite", "334 pass, 1 skipped",
         "Unit, integration and database-level tests against a real PostgreSQL instance, including "
         "a threaded concurrency test on CAS assignment.", "Every change"),
        ("Tests added in Sprint 4", "32",
         "One per requirement closed and per defect fixed. Four were run against the old code with "
         "the fix removed, to prove they fail without it.", "Sprint 4"),
        ("Browser walkthrough — admissions to CAS", "Passes",
         "The whole chain driven through a real browser as each role in turn.", "Every sprint"),
        ("Browser walkthrough — post-CAS compliance", "Passes",
         "Visa decision, enrolment, attendance, a deferral raising its reportable event, reporting "
         "to UKVI, and an evidence pack downloaded.", "Every sprint"),
        ("Applicant journey", "Passes",
         "Register, nine sections, document upload including a rejected file, submission.",
         "Every sprint"),
        ("Accessibility sweep (axe-core, WCAG 2.1 AA)", "28 screens, 0 serious or critical",
         "Every screen, including the applicant portal, swept as the role that uses it.",
         "Every sprint"),
        ("Role navigation walk", "5 roles, 0 failures",
         "Every item in every staff sidebar opens a page that loads, and 21 out-of-scope addresses "
         "redirect. Introduced after a link was found that returned an error to the role it was "
         "shown to.", "Every sprint"),
        ("Narrow viewport", "0 px horizontal overflow",
         "The dashboard at 390 px. Other screens are not yet covered — see Open items.", "Every sprint"),
        ("Independent review", "4 sprints reviewed",
         "Each sprint is reviewed by an agent that did not write it. Every review so far has found "
         "at least one real defect, including two introduced by Sprint 4 itself.", "Every sprint"),
        ("Defects found and fixed", "5 from the assessment, 6 from the Sprint 4 review",
         "All closed. Three of the five assessment defects were in scope already counted as "
         "delivered, which is why the assessment was run.", "Ongoing"),
        ("Code size", "9,966 lines backend · 5,767 front end · 5,379 tests",
         "Tests are roughly one line for every two of application code.", "27 Sep 2026"),
        ("Database migrations", "7, all forward-only",
         "No migration can be reversed, by design: a compliance record is not rolled back.",
         "Ongoing"),
    ]
    row = 5
    for i, m in enumerate(measures):
        write_row(ws, row, list(m), band=bool(i % 2), wrap_from=3)
        ws.cell(row=row, column=2).font = BODY_B
        ws.row_dimensions[row].height = 42
        row += 1
    ws.freeze_panes = "A5"
    page_setup(ws, header_row_index=4)

    wb.save(out)
    print(f"{out} written")


#: The assessed matrix, kept in the repository so the workbook can be rebuilt from source rather
#: than re-derived by hand. Extracted from docs/REQUIREMENTS_ASSESSMENT.md and updated as
#: statuses change, with the sprint that changed each one recorded against it.
MATRIX = Path(__file__).resolve().parents[1] / "docs" / "requirements_status.json"

if __name__ == "__main__":
    data = json.loads(MATRIX.read_text(encoding="utf-8"))
    build(data, Path(sys.argv[1] if len(sys.argv) > 1 else "ESE_Project_Status.xlsx"))
