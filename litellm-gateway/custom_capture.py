"""
Message capture callback for the LiteLLM proxy.

Purpose: record what Claude Code sent and what the provider returned, so we can
answer the only question that matters for this spike — *does LiteLLM hand us the
whole payload, or a lossy summary of it?*

Two things are written per exchange:

  capture/<session-id>/001.title.request.json  incoming body + headers
  capture/<session-id>/001.title.response.json the response object
  capture/index.jsonl                          one flat line per exchange

Exchanges are numbered in arrival order within a session and carry a short label
describing what the call was for — title, tool-call-skill, answer, recap. The
number is authoritative; the label is best effort (see `_label`). `index.jsonl`
carries the sequence, the filename, and the original nanosecond timestamp.

Plus, for the first CAPTURE_KWARGS_DUMPS calls:

  capture/_kwargs/<ts>.kwargs.json             the ENTIRE callback payload

That last one is the point of the exercise. Read it before trusting anything
else here — it shows exactly which fields LiteLLM exposes to a callback.

KNOWN FIDELITY CAVEAT
---------------------
For streaming requests, `response_obj` is LiteLLM's *reassembled* response
object, not the raw server-sent-event bytes from the provider. So this callback
captures a normalised view of the response, not a byte-faithful one. That is a
property of callback-based capture, not a bug here — and it is one of the
findings the spike is meant to establish. See README.md, check 2.
"""

import json
import os
import threading
import time
from pathlib import Path

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # allows importing this file outside the proxy for testing
    class CustomLogger:  # type: ignore[no-redef]
        pass


STORE = Path(os.environ.get("CAPTURE_DIR", "./capture"))
KWARGS_DUMPS = int(os.environ.get("CAPTURE_KWARGS_DUMPS", "5"))

_lock = threading.Lock()
_dumped = 0

# Headers Claude Code sends that give us attribution without parsing bodies.
CC_HEADERS = (
    "x-claude-code-session-id",
    "x-claude-code-agent-id",
    "x-claude-code-parent-agent-id",
)


def _jsonable(obj):
    """Best-effort conversion of pydantic / SDK objects into plain data."""
    for attr in ("model_dump", "dict"):
        if hasattr(obj, attr):
            try:
                return getattr(obj, attr)()
            except Exception:
                pass
    if hasattr(obj, "json"):
        try:
            return json.loads(obj.json())
        except Exception:
            pass
    return obj


def _write_json(path: Path, payload) -> None:
    """Plain, human-readable JSON. Double-clickable and greppable; no decode
    step. For a PoC that is worth more than the disk gzip would save."""
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)


def _dump_raw_kwargs(kwargs, response_obj) -> None:
    """Write the untouched callback payload for the first few calls."""
    global _dumped
    with _lock:
        if _dumped >= KWARGS_DUMPS:
            return
        _dumped += 1
        n = _dumped

    d = STORE / "_kwargs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.time_ns()}.kwargs.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "note": "raw LiteLLM callback payload — inspect this to see what is available",
                "dump_number": n,
                "kwargs_keys": sorted(list(kwargs.keys())) if isinstance(kwargs, dict) else None,
                "kwargs": _jsonable(kwargs),
                "response_obj": _jsonable(response_obj),
            },
            f,
            indent=2,
            default=str,
        )
    print(f"[capture] wrote raw kwargs dump {n}/{KWARGS_DUMPS} -> {path}")


def _next_seq(session_dir: Path) -> str:
    """Number exchanges 001, 002, ... within a session, in arrival order.

    Filenames were nanosecond timestamps, which sort correctly but are unreadable
    when walking someone through a capture. The count is derived from the files
    already on disk rather than an in-memory counter, so it survives a proxy
    restart and stays correct if a session spans one.
    """
    with _lock:
        n = len(list(session_dir.glob("*.request.json"))) + 1
    return f"{n:03d}"


def _last_user_text(body) -> str:
    """The final user instruction, which is where Claude Code states the purpose
    of a call — 'Write the title...', 'Recap in under 40 words...', and so on."""
    if not isinstance(body, dict):
        return ""
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in reversed(content):
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    return block["text"]
        return ""
    return ""


def _label(body, response_obj, ok: bool) -> str:
    """A short, human-readable name for what this exchange was.

    BEST EFFORT, and deliberately so. The sequence number is the authoritative
    identifier; this label exists to make a capture directory readable at a
    glance. It keys off English instruction text that Claude Code sends, which
    can change between releases — a wrong label is cosmetic, never data loss.
    """
    if not ok:
        return "error"

    data = _jsonable(response_obj)
    if isinstance(data, dict):
        try:
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "tool_calls":
                calls = (choice.get("message") or {}).get("tool_calls") or []
                name = ((calls[0] or {}).get("function") or {}).get("name") if calls else None
                return f"tool-call-{_slug(name)}" if name else "tool-call"
        except Exception:
            pass

    text = _last_user_text(body).lower()
    if "write the title" in text:
        return "title"
    if "stepped away and is coming back" in text or "recap in under" in text:
        return "recap"

    if isinstance(body, dict) and not body.get("tools"):
        return "utility"
    return "answer"


def _slug(value) -> str:
    """Filesystem-safe fragment for a filename."""
    keep = [c if (c.isalnum() or c == "-") else "-" for c in str(value).lower()]
    return "".join(keep).strip("-")[:24] or "unknown"


def _usage_of(response_obj) -> dict:
    """Pull token usage out, including the cache fields that tell us whether
    prompt caching survived the proxy. All-zero cache reads across turns of one
    session means caching is broken and the bill is roughly 10x what it should be."""
    data = _jsonable(response_obj)
    usage = {}
    if isinstance(data, dict):
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = _jsonable(usage) or {}
    keys = (
        "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
    )
    return {k: usage.get(k) for k in keys if isinstance(usage, dict)}


def _incoming(kwargs: dict) -> dict:
    """Locate the original client request inside the callback payload.

    VERIFIED 18 Aug 2026 against ghcr.io/berriai/litellm:main-latest on the
    /v1/messages route: `proxy_server_request` is NOT a top-level kwarg — it
    sits under `litellm_params`. The top-level location is checked first anyway
    because other routes and releases do put it there.

    Fallbacks, in order of fidelity:
      1. litellm_params.proxy_server_request   — url, method, headers, body
      2. proxy_server_request                  — same shape, top level
      3. litellm_params.metadata.headers       — headers only
      4. standard_logging_object.metadata.requester_custom_headers
      5. additional_args.complete_input_dict   — the outbound body
    """
    lp = kwargs.get("litellm_params") or {}
    for candidate in (lp.get("proxy_server_request"), kwargs.get("proxy_server_request")):
        if isinstance(candidate, dict) and (candidate.get("headers") or candidate.get("body")):
            return candidate

    headers = {}
    md = lp.get("metadata") or {}
    if isinstance(md, dict) and isinstance(md.get("headers"), dict):
        headers = md["headers"]
    if not headers:
        slo_md = ((kwargs.get("standard_logging_object") or {}).get("metadata") or {})
        if isinstance(slo_md.get("requester_custom_headers"), dict):
            headers = slo_md["requester_custom_headers"]

    body = None
    aa = kwargs.get("additional_args") or {}
    if isinstance(aa, dict) and isinstance(aa.get("complete_input_dict"), dict):
        body = aa["complete_input_dict"]

    return {"headers": headers, "body": body, "_source": "reconstructed"}


def _capture(kwargs, response_obj, ok: bool) -> None:
    """Never raise. A capture failure must not break a developer's session."""
    try:
        if not isinstance(kwargs, dict):
            kwargs = {}

        _dump_raw_kwargs(kwargs, response_obj)

        incoming = _incoming(kwargs)
        headers = incoming.get("headers") or {}
        headers = {str(k).lower(): v for k, v in headers.items()} if isinstance(headers, dict) else {}
        body = incoming.get("body")

        session = headers.get("x-claude-code-session-id") or "no-session-id"
        stamp = str(time.time_ns())

        d = STORE / session
        d.mkdir(parents=True, exist_ok=True)
        seq = _next_seq(d)
        name = f"{seq}.{_label(body, response_obj, ok)}"

        _write_json(d / f"{name}.request.json", {
            "body": body,
            "headers": headers,
            "incoming_source": incoming.get("_source", "proxy_server_request"),
            # LiteLLM's own normalised view, for comparison against `body`.
            # If `body` is missing but this is present, LiteLLM is only giving
            # us its interpretation of the request — a finding worth recording.
            "litellm_messages": _jsonable(kwargs.get("messages")),
            "litellm_optional_params": _jsonable(kwargs.get("optional_params")),
        })

        _write_json(d / f"{name}.response.json", {
            "ok": ok,
            "response": _jsonable(response_obj),
        })

        STORE.mkdir(parents=True, exist_ok=True)
        with (STORE / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "seq": seq,
                "file": name,
                "stamp": stamp,
                "ok": ok,
                "session": session,
                "agent": headers.get("x-claude-code-agent-id"),
                "parent_agent": headers.get("x-claude-code-parent-agent-id"),
                "model": kwargs.get("model"),
                "route": (incoming.get("url") if isinstance(incoming, dict) else None),
                "stream": bool((kwargs.get("optional_params") or {}).get("stream"))
                          if isinstance(kwargs.get("optional_params"), dict) else None,
                "usage": _usage_of(response_obj),
                "has_raw_body": body is not None,
                "cc_headers_present": [h for h in CC_HEADERS if h in headers],
            }, default=str) + "\n")

    except Exception as e:  # fail open, always
        print(f"[capture] write failed, continuing: {type(e).__name__}: {e}")


class Capture(CustomLogger):
    # Async variants are what the proxy uses in normal operation.
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, ok=True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, ok=False)

    # Sync variants, in case this LiteLLM release routes through them instead.
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, ok=True)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, ok=False)


handler = Capture()
