"""FastAPI app: the review dashboard's API.

  GET  /api/health    service status: the rule pack, the book, and whether Anthropic and eCFR answer
  GET  /api/examples  the example questions shown as chips, one per demo step
  GET  /api/book      the book at the session start (rule pack v1, original facts)
  POST /api/run       a question, streamed as NDJSON node events, then the result
  POST /api/approve   the reviewer's decision on a preview, streamed the same way

No tax logic lives here: requests are validated and handed to the pipeline. The server keeps no session;
each request carries the approved change sets with their signed receipts (see session.py and signing.py).
The front end is served from the same origin, so no cross-origin access is allowed: a POST must be JSON and
must not come from another site. Limits: a request body of 64 KB, a question of 2,000 characters, 25 approved
changes, 8 runs at once per instance, and per address 40 runs and 15 runs with an LLM every 10 minutes
(best effort: each server instance counts on its own). Over the LLM limit a run still completes, with the
pattern reader and templates, and its nodes say why.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .. import __version__
from ..lanes import engine_b, law, llm
from ..lanes.engine_b import load_cache
from ..paths import ROOT
from . import payloads, pipeline, signing
from .session import PARITY_MODE, base

HOSTED = bool(os.environ.get("VERCEL"))
MAX_BODY = 64 * 1024
STARTED = time.time()
_first_request = threading.Event()


class Throttle:
    """At most `limit` hits per key in a sliding window of `window` seconds. Per process, best effort."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self.lock:
            hits = self.hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, (self.window - (now - hits[0])) if hits else self.window
            hits.append(now)
            if len(self.hits) > 10_000:  # bounded memory: forget idle addresses, then the oldest
                stale = [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]
                for key in (stale or list(self.hits))[:5_000]:
                    self.hits.pop(key, None)
            return True, 0.0


RUNS = Throttle(int(os.environ.get("TAXDIFF_RUNS_PER_10_MIN", "40")), 600)
LLM_RUNS = Throttle(int(os.environ.get("TAXDIFF_LLM_RUNS_PER_10_MIN", "15")), 600)
SLOTS = threading.BoundedSemaphore(int(os.environ.get("TAXDIFF_CONCURRENT_RUNS", "8")))


def _address(request: Request) -> str:
    """The client's address. On the host, its proxy sets these headers and a caller cannot reach the
    function past it; anywhere else a forwarding header is the caller's word, so the socket address counts."""
    if HOSTED:
        for header in ("x-real-ip", "x-vercel-forwarded-for", "x-forwarded-for"):
            value = request.headers.get(header, "").split(",")[0].strip()
            if value:
                return value
    return request.client.host if request.client else "unknown"


def _cross_site(request: Request) -> bool:
    """True for a browser request sent by another site: it may not start a run here.

    A browser says where a request comes from in Sec-Fetch-Site; without it, the Origin must name this
    host (or, behind a proxy, the host the proxy forwarded). A request with no Origin is not from a page."""
    site = request.headers.get("sec-fetch-site")
    if site == "cross-site":
        return True
    if site in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin == "null":
        return True
    hosts = {request.headers.get("host", ""), *request.headers.get("x-forwarded-host", "").split(",")}
    return urlsplit(origin).netloc.lower() not in {h.strip().lower() for h in hosts if h.strip()}


def _provider() -> tuple[str | None, str]:
    """The LLM provider this server offers, or why there is none. Ollama is offered only off the host."""
    choice = os.environ.get("TAXDIFF_LLM", "anthropic").lower()
    if choice == "off":
        return None, "the LLM is turned off on this server"
    if choice == "ollama" and not HOSTED:
        return ("ollama", "") if llm.ollama_running() else (None, "Ollama is not running")
    if llm.anthropic_key() is None:
        return None, "no Anthropic API key on this server"
    if importlib.util.find_spec("anthropic") is None:
        return None, "the anthropic package is not installed"
    return "anthropic", ""


def _models(request: Request) -> pipeline.Models:
    provider, why_not = _provider()
    if provider is None:
        return pipeline.Models(None, why_not)
    allowed, _ = LLM_RUNS.allow(_address(request))
    if not allowed:
        return pipeline.Models(None, f"rate limit for this address ({LLM_RUNS.limit} runs with the model per 10 minutes)")
    return pipeline.Models(provider)


def create_app() -> FastAPI:
    app = FastAPI(title="TaxDiff API", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def limits_and_headers(request: Request, call_next: Any) -> Any:
        _first_request.set()
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY):
            return JSONResponse({"error": f"the request body is over {MAX_BODY // 1024} KB"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def body(request: Request) -> bytes | JSONResponse:
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_BODY:
                return JSONResponse({"error": f"the request body is over {MAX_BODY // 1024} KB"}, status_code=413)
        return data

    def invalid(exc: ValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(p) for p in e["loc"]) or "body", "message": e["msg"]} for e in exc.errors(include_url=False)]
        return JSONResponse({"error": "the request is not valid", "fields": fields[:20]}, status_code=422)

    def throttled(request: Request) -> JSONResponse | None:
        allowed, retry = RUNS.allow(_address(request))
        if allowed:
            return None
        return JSONResponse({"error": f"too many runs from this address; try again in {retry:.0f} s"}, status_code=429,
                            headers={"Retry-After": str(int(retry) + 1)})

    def refused(request: Request) -> JSONResponse | None:
        """The checks every run request passes before any work: same site, JSON, under the rate limit."""
        if _cross_site(request):
            return JSONResponse({"error": "runs start only from this site"}, status_code=403)
        if not request.headers.get("content-type", "").lower().startswith("application/json"):
            return JSONResponse({"error": "send the request as application/json"}, status_code=415)
        return None

    def stream_response(start: Any) -> Any:
        if not SLOTS.acquire(blocking=False):
            return JSONResponse({"error": "the server is busy with other runs; try again in a few seconds"}, status_code=503,
                                headers={"Retry-After": "5"})
        try:
            stream = start(SLOTS.release)
        except Exception:
            SLOTS.release()
            raise
        # "identity" marks the stream as already encoded, so a CDN does not compress it: a compressor holds events
        # back until a block fills, and the browser's decoder ends the body before the connection does.
        return StreamingResponse(stream.lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no", "Content-Encoding": "identity"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        state = base()
        provider, why_not = _provider()
        cache = load_cache()
        return {
            "ok": True,
            "version": __version__,
            "hosted": HOSTED,
            "book": {"label": state.book.label, "tax_year": state.book.tax_year, "returns": len(state.results)},
            "pack": payloads.pack_view(state.pack),
            "facts_version": 1,
            "services": {
                "anthropic": _anthropic_status(provider, why_not),
                "ecfr": _ecfr_status() if pipeline.LAW_MODE == "live" else {"state": "off", "detail": "this server reads the saved eCFR excerpts"},
                "federal_register": _ping("federal_register", law.FEDERAL_REGISTER + "?per_page=1") if pipeline.LAW_MODE == "live"
                else {"state": "off", "detail": "not searched on this server"},
                "irs": _ping("irs", "https://www.irs.gov/pub/irs-drop/n-25-69.pdf", head=True) if pipeline.LAW_MODE == "live"
                else {"state": "off", "detail": "cited sources are not checked on this server"},
                "policyengine": {"state": {"cache": "cached", "api": "live"}.get(PARITY_MODE, PARITY_MODE),
                                 "release": "api.policyengine.org" if PARITY_MODE == "api" else cache.get("policyengine_us", ""),
                                 "label": ("PolicyEngine-US, live API" if PARITY_MODE == "api"
                                           else f"PolicyEngine-US {cache.get('policyengine_us', '')}, cached results".replace("  ", " ")),
                                 "detail": (f"the public API, with cached PolicyEngine-US {cache.get('policyengine_us', '')} results when it does not "
                                            f"answer within {engine_b.API_TIMEOUT:g} s") if PARITY_MODE == "api" else "results computed ahead of time",
                                 "cached_at": None if PARITY_MODE == "api" else (cache["runs"][-1]["at"] if cache.get("runs") else None)},
                "docling": {"state": "live" if pipeline.DOCLING_LIVE else "cached"},
                "ollama": None if HOSTED else {"state": "ok" if llm.ollama_running(0.5) else "off", "model": llm.OLLAMA_MODEL},
            },
            "limits": {"question_chars": pipeline.QUESTION_CHARS, "history": 25, "runs_per_10_min": RUNS.limit, "llm_runs_per_10_min": LLM_RUNS.limit},
            "signing": "configured" if signing.CONFIGURED else "per process",
            "eval": _eval_summary(),
            "instance": {"started_at": datetime.fromtimestamp(STARTED, UTC).isoformat(timespec="seconds"), "uptime_s": round(time.time() - STARTED, 1)},
        }

    @app.get("/api/examples")
    def examples() -> dict[str, Any]:
        return {"examples": EXAMPLES, "can_do": list(pipeline.questions.CAN_DO)}

    @app.get("/api/book")
    def book() -> dict[str, Any]:
        state = base()
        counts = {s: sum(p.state == s for p in state.parity.values()) for s in ("agree", "differs", "not_checked")}
        statuses = {s: sum(r.status == s for r in state.results.values()) for s in ("prep", "review", "filed")}
        return {
            "label": state.book.label,
            "tax_year": state.book.tax_year,
            "pack": payloads.pack_view(state.pack),
            "returns": payloads.book_rows(state, {}, pipeline.CASES),
            "parity": counts,
            "statuses": statuses,
            "drift": {cid: list(lines) for cid, lines in state.drift.items()},
            "filed": len(state.as_filed),
            "parity_note": state.parity_note,
        }

    @app.post("/api/run")
    async def run(request: Request) -> Any:
        guard = refused(request)
        if guard is not None:
            return guard
        raw = await body(request)
        if isinstance(raw, JSONResponse):
            return raw
        try:
            req = pipeline.RunRequest.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            return invalid(exc)
        limited = throttled(request)
        if limited is not None:
            return limited
        models = _models(request)
        return stream_response(lambda done: pipeline.question(req, models, on_done=done))

    @app.post("/api/approve")
    async def approve(request: Request) -> Any:
        guard = refused(request)
        if guard is not None:
            return guard
        raw = await body(request)
        if isinstance(raw, JSONResponse):
            return raw
        try:
            req = pipeline.DecisionRequest.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            return invalid(exc)
        if HOSTED and not signing.CONFIGURED:
            # A per-process key cannot be shared by the host's instances; refuse rather than fail at random.
            return JSONResponse({"detail": "approvals are off: this server has no signing key (TAXDIFF_SECRET)"}, status_code=503)
        limited = throttled(request)
        if limited is not None:
            return limited
        models = _models(request)
        return stream_response(lambda done: pipeline.decision(req, models, on_done=done))

    return app


# Status checks are cached per process, so a page load does not call Anthropic or eCFR every time.
_checks: dict[str, tuple[float, dict[str, Any]]] = {}
_checks_lock = threading.Lock()


def _cached(name: str, ttl: float, check: Any) -> dict[str, Any]:
    with _checks_lock:
        found = _checks.get(name)
        if found and time.monotonic() - found[0] < ttl:
            return {**found[1], "checked_s_ago": round(time.monotonic() - found[0])}
    result = check()
    with _checks_lock:
        _checks[name] = (time.monotonic(), result)
    return {**result, "checked_s_ago": 0}


def _anthropic_status(provider: str | None, why_not: str) -> dict[str, Any]:
    if provider != "anthropic":
        return {"state": "off" if provider is None else "other", "detail": why_not or f"this server uses {provider}", "model": llm.ANTHROPIC_MODEL}

    def check() -> dict[str, Any]:
        started = time.perf_counter()
        state, detail = llm.check_anthropic(timeout=4.0)
        return {"state": state, "detail": detail, "model": llm.ANTHROPIC_MODEL, "ms": round((time.perf_counter() - started) * 1000)}

    return _cached("anthropic", 600, check)


def _eval_summary() -> dict[str, Any] | None:
    """The question reader's score on the recorded eval set (scripts/eval_intake.py), or None without the file."""
    try:
        return json.loads((ROOT / "data" / "eval_intake.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ping(name: str, url: str, head: bool = False) -> dict[str, Any]:
    """Whether a public source answers, checked at most every five minutes per server."""

    def check() -> dict[str, Any]:
        started = time.perf_counter()
        try:
            detail = law._head(url, 3.0) if head else law._get(url, 3.0)[1].split(":", 1)[-1].strip()
        except (OSError, ValueError) as exc:
            return {"state": "error", "detail": f"no answer ({type(exc).__name__})", "ms": round((time.perf_counter() - started) * 1000)}
        return {"state": "ok", "detail": detail, "ms": round((time.perf_counter() - started) * 1000)}

    return _cached(name, 300, check)


def _ecfr_status() -> dict[str, Any]:
    def check() -> dict[str, Any]:
        started = time.perf_counter()
        try:
            _, line = law._get(f"{law.ECFR_API}/titles.json", 3.0)
        except (OSError, ValueError) as exc:
            return {"state": "error", "detail": f"the eCFR API did not answer ({type(exc).__name__})", "ms": round((time.perf_counter() - started) * 1000)}
        return {"state": "ok", "detail": line.split(":", 1)[-1].strip(), "ms": round((time.perf_counter() - started) * 1000)}

    return _cached("ecfr", 300, check)


# "needs" names the change an example only makes sense after; the dashboard offers to run that change first.
EXAMPLES: list[dict[str, str]] = [
    {"id": "evidence", "step": "Trace a line", "label": "Where C-017's overtime comes from",
     "question": "Where does C-017's overtime deduction come from?"},
    {"id": "fix", "step": "IRS Notice 2025-69", "label": "CS-001: count only the FLSA premium",
     "question": "Run CS-001: count only the FLSA-required overtime premium (IRS Notice 2025-69)"},
    {"id": "document", "step": "Employer statement", "label": "After CS-001: C-017's corrected overtime statement", "needs": "CS-001",
     "question": "C-017's employer sent a corrected overtime statement"},
    {"id": "why-up", "step": "Employer statement", "label": "After CS-001: Did C-017's tax go down?", "needs": "CS-001",
     "question": "Did C-017's tax go down?"},
    {"id": "why-down", "step": "IRS Notice 2025-69", "label": "After CS-001: Why did C-031's tax go down?", "needs": "CS-001",
     "question": "Why did C-031's tax go down?"},
    {"id": "mfs", "step": "Married filing separately", "label": "Why C-010's overtime deduction is zero",
     "question": "Why is C-010's overtime deduction zero?"},
    {"id": "amend", "step": "IRS Notice 2025-69", "label": "After CS-001: Which filed returns need amending?", "needs": "CS-001",
     "question": "Which filed returns need amending?"},
    {"id": "w2c", "step": "Corrected W-2", "label": "C-021's corrected W-2: wages $149,000",
     "question": "C-021's corrected W-2 shows wages of $149,000"},
    {"id": "what-if", "step": "What-if", "label": "What if the overtime cap drops to $10,000?",
     "question": "What if the overtime cap drops to $10,000?"},
    {"id": "invalid-900", "step": "Invalid change sets", "label": "CS-900: negative cap, no citation",
     "question": "Run CS-900: a negative overtime cap with no citation"},
    {"id": "invalid-901", "step": "Invalid change sets", "label": "CS-901: statement for 2024",
     "question": "Run CS-901: an employer statement for tax year 2024"},
]

app = create_app()
