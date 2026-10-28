"""PDF rendering for the documents the system issues and the evidence it produces.

Two documents come out of here: the offer letter an applicant receives (FR-37) and the index of an
evidence pack (FR-127). Both are laid out with ReportLab's platypus flowables — a pure-Python
dependency, so the runtime image needs no browser or system libraries to produce a document that
someone outside ESE will read.

House rules, so both documents read as the same organisation's paperwork: the ESE maroon for
headings, one serif face for body text, and a footer on every page saying what produced it. An
evidence pack goes to the Home Office; it should look like a record, not a printout.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

MAROON = colors.HexColor("#641619")
RED = colors.HexColor("#a12e2c")
INK = colors.HexColor("#252525")
MUTED = colors.HexColor("#595959")
LINE = colors.HexColor("#ddd4d2")
TINT = colors.HexColor("#fbf6f5")

PAGE_MARGINS = {"leftMargin": 20 * mm, "rightMargin": 20 * mm, "topMargin": 18 * mm, "bottomMargin": 20 * mm}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ESETitle", parent=base["Title"], fontName="Times-Bold", fontSize=20, leading=24,
            textColor=MAROON, alignment=TA_LEFT, spaceAfter=2,
        ),  # fmt: skip
        "subtitle": ParagraphStyle(
            "ESESubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13,
            textColor=MUTED, spaceAfter=12,
        ),  # fmt: skip
        "h2": ParagraphStyle(
            "ESEH2", parent=base["Heading2"], fontName="Times-Bold", fontSize=13, leading=16,
            textColor=MAROON, spaceBefore=14, spaceAfter=5,
        ),  # fmt: skip
        "h3": ParagraphStyle(
            "ESEH3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10, leading=13,
            textColor=INK, spaceBefore=8, spaceAfter=3,
        ),  # fmt: skip
        "body": ParagraphStyle(
            "ESEBody", parent=base["Normal"], fontName="Times-Roman", fontSize=10.5, leading=15,
            textColor=INK, spaceAfter=7,
        ),  # fmt: skip
        "small": ParagraphStyle(
            "ESESmall", parent=base["Normal"], fontName="Helvetica", fontSize=8, leading=11, textColor=MUTED
        ),
        "cell": ParagraphStyle(
            "ESECell", parent=base["Normal"], fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=INK
        ),
        "cellhead": ParagraphStyle(
            "ESECellHead", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8.5, leading=11.5,
            textColor=colors.white,
        ),  # fmt: skip
    }


def _footer(canvas, doc, note: str) -> None:  # noqa: ANN001 — ReportLab canvas
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(20 * mm, 12 * mm, note)
    canvas.drawRightString(A4[0] - 20 * mm, 12 * mm, f"Page {canvas.getPageNumber()}")
    canvas.setStrokeColor(LINE)
    canvas.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canvas.restoreState()


def _build(flowables: list, note: str, title: str) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, title=title, author="European School of Economics", **PAGE_MARGINS
    )
    doc.build(
        flowables,
        onFirstPage=lambda c, d: _footer(c, d, note),
        onLaterPages=lambda c, d: _footer(c, d, note),
    )
    return buffer.getvalue()


def kv_table(rows: Sequence[tuple[str, str]], styles: dict, widths=(52 * mm, 108 * mm)) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", styles["cell"]), Paragraph(v or "—", styles["cell"])] for k, v in rows]
    table = Table(data, colWidths=list(widths), hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def grid_table(header: Sequence[str], rows: Iterable[Sequence[str]], styles: dict, widths: Sequence[float]) -> Table:
    data = [[Paragraph(h, styles["cellhead"]) for h in header]]
    data += [[Paragraph(str(c) if c not in (None, "") else "—", styles["cell"]) for c in row] for row in rows]
    table = Table(data, colWidths=list(widths), repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), MAROON),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, TINT]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


# --- Offer letter (FR-37, FR-38) ----------------------------------------------------------------
def offer_letter(
    *,
    ref: str,
    applicant_name: str,
    programme: str,
    intake: str,
    offer_type: str,
    conditions: Sequence[str],
    tuition_fee: str,
    minimum_deposit: str,
    expiry_date: date | None,
    issued_on: date | None = None,
) -> bytes:
    s = _styles()
    issued = issued_on or datetime.now(UTC).date()
    flow: list = [
        Paragraph("European School of Economics", s["title"]),
        Paragraph("International admissions · Student route sponsor", s["subtitle"]),
        Paragraph(issued.strftime("%d %B %Y"), s["body"]),
        Paragraph(f"Dear {applicant_name},", s["body"]),
        Paragraph(
            f"Following assessment of your application <b>{ref}</b>, I am pleased to make you a "
            f"<b>{offer_type}</b> offer of a place on <b>{programme}</b>, starting with the "
            f"{intake} intake.",
            s["body"],
        ),
    ]
    if conditions:
        flow.append(Paragraph("This offer is subject to the following conditions:", s["body"]))
        for condition in conditions:
            flow.append(Paragraph(f"•  {condition}", s["body"]))
        flow.append(
            Paragraph(
                "A Confirmation of Acceptance for Studies cannot be assigned until every condition "
                "is met or formally waived.",
                s["body"],
            )
        )
    flow += [
        Spacer(1, 4),
        kv_table(
            [
                ("Application reference", ref),
                ("Programme", programme),
                ("Intake", intake),
                ("Tuition fee", tuition_fee),
                ("Deposit required before CAS", minimum_deposit),
                ("Respond by", expiry_date.strftime("%d %B %Y") if expiry_date else "—"),
            ],
            s,
        ),
        Spacer(1, 10),
        Paragraph("Yours sincerely,", s["body"]),
        Paragraph("Admissions<br/>European School of Economics", s["body"]),
        Spacer(1, 8),
        Paragraph(
            "This letter is an offer of a place. It is not a visa and not a Confirmation of "
            "Acceptance for Studies.",
            s["small"],
        ),
    ]
    return _build(
        flow,
        f"Offer letter · {ref} · issued {issued.isoformat()}",
        f"Offer — {ref}",
    )


# --- Evidence pack index (FR-127, T-18) -----------------------------------------------------------
def evidence_pack_index(pack: dict) -> bytes:
    """The readable index of an evidence pack: what the file contains and what it shows.

    `pack` is assembled by services.evidence, which owns what goes in. This function owns how it
    reads, and nothing else — so a change to the contents cannot quietly change the layout rules.
    """
    s = _styles()
    generated = pack["generated_at"]
    flow: list = [
        Paragraph("Evidence pack", s["title"]),
        Paragraph(
            f"European School of Economics · Student route sponsor licence<br/>"
            f"Application {pack['application']['ref']} · generated "
            f"{generated.strftime('%d %B %Y at %H:%M')} by {pack['generated_by']}",
            s["subtitle"],
        ),
        Paragraph(
            "This pack was produced from the sponsor compliance system's own records. The audit "
            "trail at the end lists every action taken on this application, in order, with the "
            "person who took it. Audit entries cannot be edited or deleted in this system.",
            s["body"],
        ),
    ]

    a = pack["application"]
    flow += [Paragraph("1. Applicant and application", s["h2"]), kv_table(a["summary"], s)]

    flow += [Paragraph("2. Documents held", s["h2"])]
    if a["documents"]:
        flow.append(
            grid_table(
                ["Category", "File", "Uploaded", "Scan", "Verified"],
                a["documents"],
                s,
                [30 * mm, 52 * mm, 28 * mm, 20 * mm, 30 * mm],
            )
        )
    else:
        flow.append(Paragraph("No documents on file.", s["body"]))

    flow += [Paragraph("3. Assessment and sign-off", s["h2"])]
    for heading, rows in pack["assessments"]:
        flow.append(KeepTogether([Paragraph(heading, s["h3"]), kv_table(rows, s)]))

    flow += [Paragraph("4. Offer and conditions", s["h2"])]
    if pack["offer"]["summary"]:
        flow.append(kv_table(pack["offer"]["summary"], s))
        if pack["offer"]["conditions"]:
            flow.append(Spacer(1, 4))
            flow.append(
                grid_table(
                    ["Condition", "Status", "Verified"],
                    pack["offer"]["conditions"],
                    s,
                    [100 * mm, 25 * mm, 35 * mm],
                )
            )
    else:
        flow.append(Paragraph("No offer was made.", s["body"]))

    flow += [Paragraph("5. Fees", s["h2"]), kv_table(pack["fees"]["summary"], s)]
    if pack["fees"]["payments"]:
        flow.append(Spacer(1, 4))
        flow.append(
            grid_table(
                ["Date", "Type", "Amount", "Method", "Reference"],
                pack["fees"]["payments"],
                s,
                [24 * mm, 30 * mm, 26 * mm, 30 * mm, 50 * mm],
            )
        )

    flow += [Paragraph("6. CAS and visa outcome", s["h2"])]
    flow.append(kv_table(pack["cas"], s) if pack["cas"] else Paragraph("No CAS was assigned.", s["body"]))

    flow += [Paragraph("7. Student record", s["h2"])]
    if pack["student"]["summary"]:
        flow.append(kv_table(pack["student"]["summary"], s))
        if pack["student"]["attendance"]:
            flow.append(Spacer(1, 4))
            flow.append(Paragraph("Attendance summary", s["h3"]))
            flow.append(kv_table(pack["student"]["attendance"], s))
        flow.append(Paragraph("Right-to-study checks", s["h3"]))
        if pack["student"].get("checks"):
            flow.append(grid_table(["Date", "Method", "Reference", "Outcome", "Permission", "Notes"],
                                   pack["student"]["checks"], s,
                                   [20 * mm, 32 * mm, 24 * mm, 26 * mm, 28 * mm, 30 * mm]))  # fmt: skip
        else:
            flow.append(Paragraph("No right-to-study check has been recorded.", s["body"]))
        if pack["student"].get("contacts"):
            flow.append(Paragraph("Address and contact history", s["h3"]))
            flow.append(grid_table(["From", "Address", "Phone / email", "Reason"], pack["student"]["contacts"], s,
                                   [20 * mm, 70 * mm, 40 * mm, 30 * mm]))  # fmt: skip
    else:
        flow.append(Paragraph("No student record exists for this application.", s["body"]))

    flow += [Paragraph("8. Reportable events", s["h2"])]
    if pack["events"]:
        flow.append(
            grid_table(
                ["Event", "Occurred", "Deadline", "Status", "Reported", "SMS reference"],
                pack["events"],
                s,
                [32 * mm, 22 * mm, 22 * mm, 24 * mm, 22 * mm, 38 * mm],
            )
        )
    else:
        flow.append(Paragraph("No reportable events have arisen.", s["body"]))

    flow += [PageBreak(), Paragraph("9. Audit trail", s["h2"])]
    flow.append(
        Paragraph(
            f"{len(pack['audit'])} entries, oldest first. The application database role may add "
            "and read audit rows and nothing else, so these entries cannot be amended or removed "
            "from within the system.",
            s["body"],
        )
    )
    flow.append(
        grid_table(
            ["When", "Who", "Role", "Action", "Detail"],
            pack["audit"],
            s,
            # The action column is wide enough for the longest name in the vocabulary: an audit
            # action broken across two lines is unreadable as evidence.
            [21 * mm, 24 * mm, 18 * mm, 32 * mm, 65 * mm],
        )
    )
    return _build(
        flow,
        f"Evidence pack · {a['ref']} · generated {generated.strftime('%Y-%m-%d %H:%M')} UTC",
        f"Evidence pack — {a['ref']}",
    )
