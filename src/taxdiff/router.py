"""Routing: what happens to each return a change reaches. The rules are firm policy settings."""

from __future__ import annotations

from decimal import Decimal

ROUTES: dict[str, str] = {
    "update": "Update return",
    "reviewer": "Back to reviewer",
    "amendment": "Amendment review",
    "document": "Document request",
    "below_materiality": "Too small to amend: tell the client",
    "no_change": "No action",
}


def route(
    status: str,
    changed: bool,
    unknown: bool,
    filed_delta: Decimal | None,
    materiality: Decimal,
    engines_differ: bool = False,
    conflict: bool = False,
    conflict_delta: Decimal | None = None,
) -> str:
    """Route one reached return.

    filed_delta is the change in tax against the return as filed; it is used only for filed returns.
    engines_differ is true when Engine A and PolicyEngine-US disagree on the new result. conflict is true when
    a new document or fact contradicts the return as filed, whether or not the active rules move a line;
    conflict_delta is that conflict priced against the return as filed, when it is known, and a filed return follows
    the firm's amend threshold on it as on any other change.
    """
    if conflict:
        if status != "filed":
            return "reviewer"
        return "below_materiality" if conflict_delta is not None and abs(conflict_delta) < materiality else "amendment"
    if not changed:
        return "no_change"  # a return still waiting on an earlier request gets no new step from this change
    if unknown:
        return "amendment" if status == "filed" else "document"  # a filed return gets both: the request is written too
    if engines_differ:
        return "reviewer"
    if status == "prep":
        return "update"
    if status == "review":
        return "reviewer"
    if filed_delta is not None and abs(filed_delta) < materiality:
        return "below_materiality"
    return "amendment"
