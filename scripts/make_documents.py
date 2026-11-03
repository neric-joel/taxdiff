"""Render each synthetic employer overtime statement in data/documents/*.txt as a PDF next to it.

Usage: python scripts/make_documents.py
The PDFs give the documents lane a realistic input for its Docling path. Every document is synthetic. The
creation date is fixed, so the same text always gives the same PDF bytes and the same SHA-256, which is the
key of its cached Docling conversion in cache/documents/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

CREATED = datetime(2026, 1, 15, tzinfo=UTC)

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "data" / "documents"


def render(text_path: Path) -> Path:
    lines = text_path.read_text(encoding="utf-8").splitlines()
    pdf = FPDF(format="Letter")
    pdf.set_creation_date(CREATED)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, lines[0], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 6, lines[1], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 11)
    for line in lines[2:]:
        if not line.strip():
            pdf.ln(3)
            continue
        if ":" in line and not line.startswith(("Overtime pay in", "with daily")):
            label, value = line.split(":", 1)
            pdf.set_font("Helvetica", "", 11)
            pdf.cell(118, 7, f"{label}:")
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, value.strip(), new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    out = text_path.with_suffix(".pdf")
    pdf.output(str(out))
    return out


def main() -> None:
    for path in sorted(DOCUMENTS.glob("*.txt")):
        print(f"wrote {render(path).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
