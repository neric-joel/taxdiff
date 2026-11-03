"""Signed tokens, so a server that keeps no session can trust what the browser sends back.

- A preview token binds a preview's digest (every route and amount it showed, the change and the
  settings) to the session it was computed from. Approval needs the token, so only a preview this server
  showed can be approved.
- An approval receipt binds each applied change to the receipt before it. The list of approved changes a
  browser sends cannot be edited, reordered or extended without the server's key, so a replay applies
  only what was approved here, in the order it was approved.

The key is TAXDIFF_SECRET. Without it the server makes a random key at start, which suits one local
server; on a host with several instances, set it, or a token made by one instance fails on another.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from decimal import Decimal
from typing import Any

from ..workflow import Run, pack_digest, run_json

CONFIGURED = bool(os.environ.get("TAXDIFF_SECRET"))
_KEY = (os.environ.get("TAXDIFF_SECRET") or secrets.token_hex(32)).encode()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def sign(kind: str, **fields: Any) -> str:
    return hmac.new(_KEY, _canonical({"kind": kind, **fields}).encode(), hashlib.sha256).hexdigest()[:40]


def verify(token: str, kind: str, **fields: Any) -> bool:
    return hmac.compare_digest((token or "").encode(), sign(kind, **fields).encode())


def preview_digest(run: Run, materiality: Decimal) -> str:
    """What approval must find again: the change and its settings, every changed line of every reached
    return with its route, the unexpected list, the tax on every return, and the next rule pack and facts."""
    data = run_json(run)
    data.pop("lanes")  # timings differ from run to run
    report, pending = run.report, run.pending
    payload = {
        "run": data,
        "change": run.change.model_dump(mode="json"),
        "materiality": str(materiality),
        "lines": [[item.client_id, item.route, [[d.line, str(d.before), str(d.after)] for d in item.diffs]] for item in report.reached] if report else [],
        "pack": pack_digest(pending.pack) if pending else "",
        "facts": {ret.client_id: ret.facts_version for ret in pending.book.returns} if pending else {},
    }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()
