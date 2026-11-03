"""Optional LLM access for the intake and notes lanes.

Two providers: the Anthropic API (paid, needs a key and the `llm` extra) and a local Ollama server (free,
keeps text on the machine). Nothing in the pipeline needs either one; callers fall back to deterministic
code when a provider is unavailable. Every failure reaches callers as `Unavailable`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter
from urllib.parse import urlsplit

from ..paths import ROOT

ANTHROPIC_MODEL = os.environ.get("TAXDIFF_ANTHROPIC_MODEL", "claude-haiku-5-5")
# Effort for every Anthropic call: these are short extraction and rewording tasks. Empty sends no effort.
ANTHROPIC_EFFORT = os.environ.get("TAXDIFF_ANTHROPIC_EFFORT", "low")
OLLAMA_MODEL = os.environ.get("TAXDIFF_OLLAMA_MODEL", "qwen3.5:2b")
LLM_TIMEOUT = float(os.environ.get("TAXDIFF_LLM_TIMEOUT", "60"))  # seconds per Anthropic call; Ollama allows 180
KEY_FILE = ROOT / "api key.env"

# When a caller sets a list here, each call made in that context appends what it sent and received
# (provider, model, tokens, time, outcome), never the prompt, the reply or the key.
METER: ContextVar[list[dict] | None] = ContextVar("taxdiff_llm_meter", default=None)

# Paid calls this server process makes per hour, across every request (0 turns the limit off). The spending
# ceiling for the whole deployment is the monthly limit set on the key in the Anthropic console.
HOURLY_LIMIT = int(os.environ.get("TAXDIFF_LLM_PER_HOUR", "300"))
MIN_CALL_S = 3.0  # a call is not started with less time than this left in its run


@dataclass(frozen=True)
class Guard:
    """Limits a run puts on the calls made for it: stop when it is cancelled, and finish by its deadline
    (a perf_counter time)."""

    deadline: float
    stopped: Callable[[], bool] = lambda: False


GUARD: ContextVar[Guard | None] = ContextVar("taxdiff_llm_guard", default=None)


class _Hourly:
    def __init__(self, limit: int) -> None:
        self.limit, self.calls, self.lock = limit, deque[float](), threading.Lock()

    def take(self) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            while self.calls and now - self.calls[0] > 3600:
                self.calls.popleft()
            if len(self.calls) >= self.limit:
                return False
            self.calls.append(now)
            return True


HOURLY = _Hourly(HOURLY_LIMIT)

# Replies already received, by provider, model and prompt: the same note or answer asked for again (a repeated
# approval, a replayed question) costs no second call on this server.
_REPLIES: OrderedDict[str, str] = OrderedDict()
_REPLIES_LOCK = threading.Lock()
_REPLIES_MAX = 512


def _reply_key(provider: str, prompt: str, max_tokens: int, json_output: bool) -> str:
    model = ANTHROPIC_MODEL if provider == "anthropic" else OLLAMA_MODEL
    return hashlib.sha256(f"{provider}|{model}|{max_tokens}|{json_output}|{prompt}".encode()).hexdigest()


def _remember(key: str, text: str) -> None:
    with _REPLIES_LOCK:
        _REPLIES[key] = text
        _REPLIES.move_to_end(key)
        while len(_REPLIES) > _REPLIES_MAX:
            _REPLIES.popitem(last=False)


def _record(**entry: object) -> None:
    meter = METER.get()
    if meter is not None:
        meter.append(entry)


def ollama_host(raw: str | None) -> str:
    """The Ollama server URL from an OLLAMA_HOST value, with Ollama's own defaults filled in.

    "0.0.0.0" or "0.0.0.0:11434" (a server bind address) becomes http://localhost:11434; a missing scheme
    is http and a missing port is 11434.
    """
    value = (raw or "").strip() or "localhost"
    parts = urlsplit(value if "://" in value else f"http://{value}")
    host = parts.hostname or "localhost"
    host = "localhost" if host in ("0.0.0.0", "::") else f"[{host}]" if ":" in host else host
    try:
        port = parts.port or 11434
    except ValueError:  # a port that is not a number
        port = 11434
    return f"{parts.scheme or 'http'}://{host}:{port}{parts.path.rstrip('/')}"


OLLAMA_HOST = ollama_host(os.environ.get("OLLAMA_HOST"))

PROVIDERS = {"anthropic": f"Anthropic API ({ANTHROPIC_MODEL})", "ollama": f"Ollama, local ({OLLAMA_MODEL})"}


class Unavailable(RuntimeError):
    """The provider cannot be used here (no key, no package, no server), or its reply cannot be read."""


def anthropic_key() -> str | None:
    """The key from ANTHROPIC_API_KEY, else from the key file `api key.env` at the repository root, which
    git ignores. Never logged.

    A key file line is either the bare key or "<label> = <key>" (":" also works); the value must have the
    shape of an Anthropic key (sk-ant-...). The file must hold exactly one such key; a file with several is
    refused, so a key meant for something else is never picked up by accident.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    if not KEY_FILE.is_file():
        return None
    found = []
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        value = re.split(r"[=:]", line, maxsplit=1)[-1].strip().strip("\"'")
        if re.fullmatch(r"sk-ant-[A-Za-z0-9_\-]+", value):
            found.append(value)
    return found[0] if len(found) == 1 else None


def ollama_running(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def available() -> dict[str, bool]:
    """Which providers can run here: Anthropic needs a key and the anthropic package; Ollama a running server."""
    anthropic_ready = anthropic_key() is not None and importlib.util.find_spec("anthropic") is not None
    return {"anthropic": anthropic_ready, "ollama": ollama_running()}


def ask(provider: str, prompt: str, *, json_output: bool = False, max_tokens: int = 600, timeout: float | None = None) -> str:
    """Send one prompt and return the reply text. Raises Unavailable when the provider cannot run or answer,
    when the run it serves was stopped or has too little time left, or when this server's hourly limit is
    used up. A reply this server already received for the same prompt is returned without a call."""
    guard = GUARD.get()
    if guard is not None:
        if guard.stopped():
            raise Unavailable("the run was stopped")
        left = guard.deadline - perf_counter()
        if left < MIN_CALL_S:
            raise Unavailable("too little time left in this run")
        timeout = min(timeout or LLM_TIMEOUT, left - 1.0)
    key = _reply_key(provider, prompt, max_tokens, json_output)
    with _REPLIES_LOCK:
        cached = _REPLIES.get(key)
    if cached is not None:
        _record(provider=provider, model=ANTHROPIC_MODEL if provider == "anthropic" else OLLAMA_MODEL, ms=0, tokens_in=0, tokens_out=0,
                max_tokens=max_tokens, outcome="reused reply")
        return cached
    if provider == "anthropic" and not HOURLY.take():
        raise Unavailable(f"this server's limit of {HOURLY_LIMIT} model calls an hour is used up")
    text = _ask(provider, prompt, json_output=json_output, max_tokens=max_tokens, timeout=timeout)
    _remember(key, text)
    return text


def _ask(provider: str, prompt: str, *, json_output: bool, max_tokens: int, timeout: float | None) -> str:
    if provider == "anthropic":
        key = anthropic_key()
        if not key:
            raise Unavailable("no Anthropic API key configured")
        try:
            import anthropic
        except ImportError as exc:
            raise Unavailable("the anthropic package is not installed (pip install .[llm])") from exc
        started = perf_counter()
        extra = {"output_config": {"effort": ANTHROPIC_EFFORT}} if ANTHROPIC_EFFORT else {}
        try:
            guard = GUARD.get()
            room = guard.deadline - perf_counter() if guard is not None else float("inf")
            retries = 1 if (timeout or LLM_TIMEOUT) * 2 < room else 0  # retry only when a second try still fits the run
            reply = anthropic.Anthropic(api_key=key, timeout=timeout or LLM_TIMEOUT, max_retries=retries).messages.create(
                model=ANTHROPIC_MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}], **extra
            )
        except anthropic.AnthropicError as exc:  # authentication, rate limit, timeout, connection and server errors
            _record(provider="anthropic", model=ANTHROPIC_MODEL, ms=round((perf_counter() - started) * 1000), outcome=type(exc).__name__)
            raise Unavailable(f"Anthropic API error ({type(exc).__name__})") from exc
        usage = getattr(reply, "usage", None)
        _record(
            provider="anthropic",
            model=getattr(reply, "model", ANTHROPIC_MODEL),
            ms=round((perf_counter() - started) * 1000),
            tokens_in=getattr(usage, "input_tokens", None),
            tokens_out=getattr(usage, "output_tokens", None),
            max_tokens=max_tokens,
            outcome=getattr(reply, "stop_reason", "") or "ok",
        )
        if getattr(reply, "stop_reason", "") == "refusal":
            raise Unavailable("the model declined the request")
        text = "".join(block.text for block in reply.content if getattr(block, "type", "") == "text")
        if not text.strip() and getattr(reply, "stop_reason", "") == "max_tokens":
            raise Unavailable("the reply was cut off before any text")
        return text
    if provider == "ollama":
        body = {"model": OLLAMA_MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False, "think": False}
        if json_output:
            body["format"] = "json"
        request = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        started = perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout or 180) as response:
                data = json.loads(response.read())
                content = data["message"]["content"]
        except OSError as exc:
            raise Unavailable(f"Ollama is not answering at {OLLAMA_HOST}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise Unavailable("Ollama returned a reply this tool cannot read") from exc
        if not isinstance(content, str):
            raise Unavailable("Ollama returned a reply this tool cannot read")
        _record(provider="ollama", model=OLLAMA_MODEL, ms=round((perf_counter() - started) * 1000),
                tokens_in=data.get("prompt_eval_count"), tokens_out=data.get("eval_count"), max_tokens=None, outcome="ok")
        return content
    raise Unavailable(f"unknown provider {provider}")


def check_anthropic(timeout: float = 5.0) -> tuple[str, str]:
    """Whether the configured key and model answer, without spending tokens (a model lookup).

    Returns (state, detail): "ok", "no_key" (no key or no package), or "error" with the error class. Never
    returns or logs the key.
    """
    key = anthropic_key()
    if not key or importlib.util.find_spec("anthropic") is None:
        return "no_key", "no Anthropic API key configured" if not key else "the anthropic package is not installed"
    import anthropic

    try:
        model = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0).models.retrieve(ANTHROPIC_MODEL)
    except anthropic.AnthropicError as exc:
        return "error", f"Anthropic API error ({type(exc).__name__})"
    return "ok", str(getattr(model, "id", ANTHROPIC_MODEL))
