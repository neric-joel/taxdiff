"""The review screen previews, approves and applies changes end to end (scenarios 1, 2 and 4)."""

from __future__ import annotations

import pytest

from taxdiff.lanes import llm
from taxdiff.paths import ROOT

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest


def _app(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: {"anthropic": False, "ollama": False})
    app = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60)
    app.run()
    assert not app.exception
    return app


def _button(app, label):
    return next(button for button in app.button if button.label == label)


def _text(app) -> str:
    return " ".join(str(element.value) for element in app.markdown)


def _sign(app, name="A. Reviewer"):
    next(box for box in app.text_input if box.label == "Approver").input(name).run()


def test_app_previews_then_applies_the_overtime_fix(monkeypatch):
    app = _app(monkeypatch)
    _sign(app)
    assert _button(app, "Approve and apply").disabled  # nothing previewed yet
    _button(app, "Preview").click().run()
    _button(app, "Approve and apply").click().run()
    assert not app.exception
    text = _text(app)
    assert "CS-001 (applied): 15 of 40 returns reached" in text
    assert "full recompute found 0 unexpected changes" in text
    assert "Rule pack <b>v2</b>" in text
    assert "FLSA excerpts copied from eCFR as of 2026-10-29" in text
    assert "Authorities for this change" in text

    # Then the PDF statement for C-017 (scenario 2), read through the cached Docling conversion.
    picker = next(box for box in app.selectbox if box.label == "Change set")
    picker.select(next(option for option in picker.options if "004" in str(option))).run()
    _button(app, "Preview").click().run()
    _button(app, "Approve and apply").click().run()
    assert not app.exception
    text = _text(app)
    assert "1 of 40 returns reached" in text
    assert "Docling (cached conversion)" in text
    assert "amendment review 1" in text


def test_preview_shows_amounts_without_applying(monkeypatch):
    app = _app(monkeypatch)
    _button(app, "Preview").click().run()
    assert not app.exception
    text = _text(app)
    assert "CS-001 (preview, not applied): 15 of 40 returns reached" in text
    assert "Rule pack <b>v1</b>" in text  # nothing applied
    assert "Filed returns: all <b>14</b> recompute to their filed amounts" in text
    next(box for box in app.selectbox if box.label == "Client").select("C-031").run()
    text = _text(app)
    assert "As filed" in text and "$7,955" in text  # the filed tax, next to before and after


def test_approval_needs_a_name_and_a_matching_preview(monkeypatch):
    app = _app(monkeypatch)
    _button(app, "Preview").click().run()
    assert _button(app, "Approve and apply").disabled  # no approver yet
    _sign(app)
    assert not _button(app, "Approve and apply").disabled
    next(box for box in app.number_input if box.label.startswith("Materiality")).set_value(100).run()
    assert _button(app, "Approve and apply").disabled  # the preview no longer matches the settings


def test_reset_keeps_the_audit_log(monkeypatch):
    app = _app(monkeypatch)
    _button(app, "Preview").click().run()
    _button(app, "Reset book").click().run()
    assert not app.exception
    assert any(str(expander.label).startswith("Audit log (1 entry)") for expander in app.expander)


def test_written_change_needs_an_explicit_read_before_approval(monkeypatch):
    app = _app(monkeypatch)
    _sign(app)
    app.radio[0].set_value("Write a change").run()
    app.text_area[0].input("set overtime.cap.other to 2500 per P.L. 120-1, a test change").run()
    assert _button(app, "Preview").disabled  # nothing to preview until the instruction is read
    _button(app, "Read instruction").click().run()
    _button(app, "Preview").click().run()
    _button(app, "Approve and apply").click().run()
    assert not app.exception
    text = _text(app)
    assert "CS-100 (applied)" in text and "returns reached" in text
