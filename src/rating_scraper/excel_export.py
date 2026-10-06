"""Excel output: the Ratings sheet plus a QA sheet."""
from __future__ import annotations

import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .models import OUTPUT_COLUMNS
from .pipeline import RunResult

log = logging.getLogger(__name__)

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
NABFID_FILL = PatternFill("solid", fgColor="FFF2CC")

COLUMN_WIDTHS = {
    "Borrower Name": 42,
    "CIN": 24,
    "Open Charges": 14,
    "Rating Date": 13,
    "Rating Agency Name": 18,
    "Instrument Category": 30,
    "Instrument Details": 40,
    "Amount": 14,
    "Rating": 22,
    "Development": 26,
    "Outlook": 14,
    "Online url": 60,
    "NaBFID Name": 12,
    "Loan Amount": 14,
    "Unit": 12,
}


def _style_header(ws: Worksheet, columns: list[str]) -> None:
    for idx, name in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=idx, value=name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(idx)].width = COLUMN_WIDTHS.get(name, 18)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"


def write_workbook(result: RunResult, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "Ratings"
    _style_header(ws, OUTPUT_COLUMNS)

    for r_i, record in enumerate(result.records, start=2):
        row = record.to_row()
        for c_i, name in enumerate(OUTPUT_COLUMNS, start=1):
            cell = ws.cell(row=r_i, column=c_i, value=row.get(name))
            if name == "Rating Date" and row.get(name):
                cell.number_format = "dd-mmm-yyyy"
            elif name in ("Amount", "Loan Amount") and row.get(name) is not None:
                cell.number_format = "#,##0.00"
            elif name == "Online url" and row.get(name):
                cell.hyperlink = row[name]
                cell.font = Font(color="0563C1", underline="single", size=9)
            cell.alignment = Alignment(vertical="top", wrap_text=name in ("Instrument Details", "Instrument Category"))

        # Highlight the rows that actually name NaBFID as a lender. Read the
        # record's own field, not the exported row - that column now holds the
        # display string "Yes"/"No", and "No" is truthy as text.
        if record.nabfid_name:
            for c_i in range(1, len(OUTPUT_COLUMNS) + 1):
                ws.cell(row=r_i, column=c_i).fill = NABFID_FILL

    # --- QA sheet ---------------------------------------------------------
    qa = wb.create_sheet("QA")
    qa_cols = ["Borrower Name", "CIN", "Rating Agency Name", "Status", "Detail"]
    _style_header(qa, qa_cols)
    for r_i, q in enumerate(result.qa, start=2):
        for c_i, val in enumerate(
            [q.borrower_name, q.cin, q.agency, q.status, q.detail], start=1
        ):
            qa.cell(row=r_i, column=c_i, value=val).alignment = Alignment(vertical="top")
    qa.column_dimensions["A"].width = 42
    qa.column_dimensions["E"].width = 80

    # --- Summary sheet ----------------------------------------------------
    s = wb.create_sheet("Summary")
    stats = result.summary()
    s["A1"] = "Metric"
    s["B1"] = "Value"
    for c in ("A1", "B1"):
        s[c].fill = HEADER_FILL
        s[c].font = HEADER_FONT
    rows = [
        ("Total rating rows", stats["records"]),
        ("Companies with at least one rating", stats["companies_with_ratings"]),
        ("QA rows (not rated / failed)", stats["qa_rows"]),
        ("Rows naming NaBFID as lender", sum(1 for r in result.records if r.nabfid_name)),
    ]
    rows += [(f"Rows from {a}", n) for a, n in sorted(stats["by_agency"].items())]
    for i, (k, v) in enumerate(rows, start=2):
        s.cell(row=i, column=1, value=k)
        s.cell(row=i, column=2, value=v)
    s.column_dimensions["A"].width = 42
    s.column_dimensions["B"].width = 16

    wb.save(path)
    log.info("wrote %s (%d rating rows, %d QA rows)", path, len(result.records), len(result.qa))
    return path
