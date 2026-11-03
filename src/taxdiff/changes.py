"""Change sets: rule edits, fact edits or a new document, validated before anything is recomputed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .facts import Book, apply_fact_edit, normal_path, validation_messages
from .rules import FieldError, RulePack, edit_pack


class RuleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    to: Any
    cite: str = ""
    url: str | None = None


class FactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    path: str
    to: Any


class ChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^CS-\d{3}$")
    kind: Literal["rule", "fact", "document"]
    title: str
    author: str = "reviewer"
    rationale: str = ""
    rule_edits: list[RuleEdit] = []
    fact_edits: list[FactEdit] = []
    document: str | None = None
    client_id: str | None = None


def load_change(path: str | Path) -> ChangeSet:
    return ChangeSet.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


Touched = tuple[str | None, str]  # (client_id or None for every client, provenance path)


@dataclass
class Validation:
    errors: list[FieldError] = field(default_factory=list)
    pack: RulePack | None = None
    book: Book | None = None
    touched: set[Touched] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_change(change: ChangeSet, pack: RulePack, book: Book) -> Validation:
    """Build the next pack and book from the change, or collect every reason it cannot apply."""
    result = Validation(pack=pack, book=book)
    if change.kind == "rule":
        if not change.rule_edits:
            result.errors.append(FieldError("rule_edits", "a rule change needs at least one edit"))
            return result
        new_pack, errors = edit_pack(pack, [edit.model_dump() for edit in change.rule_edits])
        result.errors += errors
        result.pack = new_pack
        result.touched = {(None, f"rule:{edit.path}") for edit in change.rule_edits}
        return result

    if not change.fact_edits:
        result.errors.append(FieldError("fact_edits", "no facts to change"))
        return result
    new_book = book
    for edit in change.fact_edits:
        current = new_book.get(edit.client_id)
        if current is None:
            result.errors.append(FieldError(edit.client_id, "no such client in the book"))
            continue
        try:
            updated = apply_fact_edit(current, edit.path, edit.to)
        except ValidationError as exc:
            result.errors += [FieldError(f"{edit.client_id}.{loc}", msg) for loc, msg in validation_messages(exc)]
            continue
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            result.errors.append(FieldError(f"{edit.client_id}.{edit.path}", str(exc) or "cannot be set"))
            continue
        new_book = new_book.replace(updated)
        result.touched.add((edit.client_id, f"fact:{normal_path(edit.path)}"))
    for client_id in {client for client, _ in result.touched}:
        # One change set is one new version of a client's facts, however many fields it edits.
        edited = new_book.get(client_id)
        new_book = new_book.replace(edited.model_copy(update={"facts_version": book.get(client_id).facts_version + 1}))  # type: ignore[union-attr]
    result.book = new_book
    return result
