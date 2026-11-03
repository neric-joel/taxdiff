"""Documents lane: read an employer overtime statement into facts for one client.

The baseline is a pattern map over the statement text. Each field is either read or left empty;
nothing is guessed. Docling can turn a PDF into the same text when it is installed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from ..changes import FactEdit
from ..facts import Book
from ..paths import ROOT, write_text_atomic
from ..rules import FieldError

_NUMBER = r"\$?\s*([\d,]+(?:\.\d+)?)"
FIELDS: dict[str, re.Pattern[str]] = {
    "tax_year": re.compile(r"Tax year:\s*(\d{4})", re.I),
    "employee": re.compile(r"Employee:\s*(.+?)(?=\s+Employee SSN|\n|$)", re.I),
    "ssn_last4": re.compile(r"SSN \(last 4\):\s*(\d{4})", re.I),
    "flsa_hours": re.compile(r"FLSA overtime hours:\s*" + _NUMBER, re.I),
    "regular_rate": re.compile(r"Regular rate of pay:\s*" + _NUMBER, re.I),
    "premium": re.compile(r"Overtime premium paid under the FLSA[^:]*:\s*" + _NUMBER, re.I),
}


@dataclass
class Statement:
    fields: dict[str, str | None]
    method: str
    errors: list[FieldError] = field(default_factory=list)
    edits: list[FactEdit] = field(default_factory=list)

    @property
    def read_count(self) -> int:
        return sum(value is not None for value in self.fields.values())


CONVERSIONS = ROOT / "cache" / "documents"


def read_text(path: str | Path, use_cache: bool = True) -> tuple[str, str]:
    """Return the document text and how it was read.

    A PDF is converted with Docling (text layer, no OCR). Conversions are cached by the file's SHA-256,
    so a machine without Docling, such as the hosted demo, can still read a PDF that was converted once.
    use_cache=False converts again and refreshes the cache.
    """
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8"), "pattern map"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cached = CONVERSIONS / f"{digest}.md"
    if use_cache and cached.is_file():
        return cached.read_text(encoding="utf-8"), "Docling (cached conversion), then pattern map"
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        if cached.is_file():
            raise ValueError("Docling is not installed, so this PDF cannot be converted again; read the cached conversion instead") from exc
        raise ValueError("this PDF has no cached conversion and Docling is not installed (pip install .[docs])") from exc
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=False)
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    text = converter.convert(str(path)).document.export_to_markdown()
    write_text_atomic(cached, text)
    return text, "Docling, then pattern map"


def read_statement(text: str, method: str, client_id: str, book: Book) -> Statement:
    values: dict[str, str | None] = {}
    for name, pattern in FIELDS.items():
        match = pattern.search(text)
        values[name] = match.group(1).strip() if match else None
    statement = Statement(values, method)

    ret = book.get(client_id)
    if ret is None:
        statement.errors.append(FieldError("client_id", f"{client_id} is not in the book"))
        return statement
    if values["tax_year"] is None:
        statement.errors.append(FieldError("tax_year", "not found on the statement"))
    elif int(values["tax_year"]) != book.tax_year:
        statement.errors.append(
            FieldError("tax_year", f"statement is for {values['tax_year']}; the book is {book.tax_year}")
        )
    index = next((i for i, p in enumerate(ret.people) if p.ssn_last4 == values["ssn_last4"]), None)
    if index is None:
        statement.errors.append(
            FieldError("ssn_last4", f"{values['ssn_last4'] or 'missing'} does not match anyone on {client_id}")
        )
    if statement.errors:
        return statement

    def number(name: str) -> str | None:
        raw = values[name]
        return str(Decimal(raw.replace(",", ""))) if raw is not None else None

    for name, path in (("premium", "ot_premium_reported"), ("flsa_hours", "ot_flsa_hours"), ("regular_rate", "ot_regular_rate")):
        value = number(name)
        if value is not None:
            statement.edits.append(FactEdit(client_id=client_id, path=f"people[{index}].{path}", to=value))
    if not statement.edits:
        statement.errors.append(FieldError("statement", "no overtime premium, hours or rate found"))
    return statement
