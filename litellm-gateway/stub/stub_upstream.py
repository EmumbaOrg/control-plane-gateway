"""
Fake Anthropic upstream — the fidelity oracle for the gateway spike.

It speaks enough of the Messages API to satisfy a gateway, and it records
*exactly what it received*. Compare that against what the client sent and you
know precisely what the gateway in the middle changed. No API key, no cost, and
it can misbehave on demand in ways the real API never will on cue.

    uv run --with fastapi --with "uvicorn[standard]" \
        uvicorn stub_upstream:app --port 8080

Endpoints (both with and without the /v1 prefix, since gateways differ in
whether they append it):

    POST /v1/messages               streaming or non-streaming
    POST /v1/messages/count_tokens
    HEAD /api/hello
    GET  /_stub/received            what this stub has been sent, as JSON
    POST /_stub/reset               clear the record

Modes — set `x-stub-mode`, or put `STUB_MODE=<mode>` anywhere in the request
body (the body marker is the reliable one, since a gateway may drop unknown
headers, which is itself something we want to detect):

    normal        text response, streamed as SSE with a ping
    thinking      thinking block + tool_use block + text
    long_silence  ping, then STUB_SILENCE_SECONDS of nothing, then finish
    error         400 with an Anthropic-shaped error body
    cached        usage reports a large cache_read_input_tokens
"""

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

RECEIVED = Path(os.environ.get("STUB_RECEIVED_DIR", "./received"))
SILENCE = float(os.environ.get("STUB_SILENCE_SECONDS", "310"))

app = FastAPI()
RECEIVED.mkdir(parents=True, exist_ok=True)


def record(request: Request, raw: bytes, kind: str) -> dict:
    """Persist what arrived. This file is the evidence."""
    try:
        body = json.loads(raw) if raw else None
    except Exception:
        body = {"_unparseable": raw.decode("utf-8", "replace")[:4000]}

    entry = {
        "ts": time.time(),
        "kind": kind,
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "headers": {k.lower(): v for k, v in request.headers.items()},
        "body": body,
        # The things we specifically care about surviving the gateway:
        "observed": {
            "anthropic_beta": request.headers.get("anthropic-beta"),
            "anthropic_version": request.headers.get("anthropic-version"),
            "has_x_api_key": "x-api-key" in request.headers,
            "has_authorization": "authorization" in request.headers,
            "cc_session": request.headers.get("x-claude-code-session-id"),
            "cc_agent": request.headers.get("x-claude-code-agent-id"),
            "system_present": bool(isinstance(body, dict) and body.get("system")),
            "system_is_list": isinstance((body or {}).get("system"), list)
            if isinstance(body, dict) else None,
            "system_first_block": (
                (body["system"][0] if isinstance(body.get("system"), list) and body["system"] else None)
                if isinstance(body, dict) else None
            ),
            "tools_count": len(body.get("tools") or []) if isinstance(body, dict) else None,
            "messages_count": len(body.get("messages") or []) if isinstance(body, dict) else None,
            "stream": (body or {}).get("stream") if isinstance(body, dict) else None,
        },
    }
    path = RECEIVED / f"{time.time_ns()}.{kind}.json"
    path.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
    print(f"[stub] {kind} recorded -> {path.name} "
          f"beta={entry['observed']['anthropic_beta']!r} "
          f"system={entry['observed']['system_present']} "
          f"tools={entry['observed']['tools_count']}")
    return entry


def mode_of(request: Request, raw: bytes) -> str:
    header = (request.headers.get("x-stub-mode") or "").strip().lower()
    if header:
        return header
    text = raw.decode("utf-8", "replace")
    for m in ("long_silence", "thinking", "error", "cached", "normal"):
        if f"STUB_MODE={m}" in text:
            return m
    return "normal"


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def usage(mode: str) -> dict:
    u = {"input_tokens": 1200, "output_tokens": 24,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    if mode == "cached":
        u["cache_read_input_tokens"] = 45000
        u["input_tokens"] = 5000
    return u


def blocks_for(mode: str) -> list[dict]:
    if mode == "thinking":
        return [
            {"type": "thinking", "thinking": "stub reasoning", "signature": "stub-sig"},
            {"type": "tool_use", "id": "toolu_stub01", "name": "read",
             "input": {"file_path": "/tmp/example.txt"}},
            {"type": "text", "text": "Stub response with thinking and a tool call."},
        ]
    return [{"type": "text", "text": "Stub OK."}]


async def stream_response(mode: str):
    """SSE in the real shape, including the ping that a gateway must relay."""
    msg_id = f"msg_stub_{int(time.time())}"
    yield sse("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant",
        "model": "claude-stub", "content": [], "stop_reason": None,
        "usage": usage(mode)}})

    # A ping straight away. If the gateway strips these, the client aborts a
    # stream after 300s of silence — the failure this stub exists to prove.
    yield sse("ping", {"type": "ping"})

    if mode == "long_silence":
        print(f"[stub] going silent for {SILENCE}s (no pings) — "
              f"a client should abort at ~300s if the gateway relays nothing")
        await asyncio.sleep(SILENCE)

    for idx, block in enumerate(blocks_for(mode)):
        kind = block["type"]
        start = {"type": "content_block_start", "index": idx, "content_block":
                 {k: v for k, v in block.items() if k != "text"} | (
                     {"text": ""} if kind == "text" else {})}
        yield sse("content_block_start", start)
        if kind == "text":
            for piece in block["text"].split(" "):
                yield sse("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": piece + " "}})
                await asyncio.sleep(0.02)
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    yield sse("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": "end_turn"},
                                "usage": usage(mode)})
    yield sse("message_stop", {"type": "message_stop"})


@app.post("/v1/messages")
@app.post("/messages")
async def messages(request: Request):
    raw = await request.body()
    entry = record(request, raw, "messages")
    mode = mode_of(request, raw)

    if mode == "error":
        return JSONResponse(status_code=400, content={
            "type": "error",
            "error": {"type": "invalid_request_error",
                      "message": "stub deliberate failure: unexpected field"}})

    if entry["observed"].get("stream"):
        return StreamingResponse(stream_response(mode), media_type="text/event-stream")

    return JSONResponse({
        "id": f"msg_stub_{int(time.time())}", "type": "message", "role": "assistant",
        "model": "claude-stub", "content": blocks_for(mode),
        "stop_reason": "end_turn", "stop_sequence": None, "usage": usage(mode)})


@app.post("/v1/messages/count_tokens")
@app.post("/messages/count_tokens")
async def count_tokens(request: Request):
    raw = await request.body()
    record(request, raw, "count_tokens")
    return JSONResponse({"input_tokens": 1234})


@app.head("/api/hello")
async def hello():
    return Response(status_code=200)


@app.get("/_stub/received")
async def received():
    out = []
    for p in sorted(RECEIVED.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return JSONResponse(out)


@app.post("/_stub/reset")
async def reset():
    n = 0
    for p in RECEIVED.glob("*.json"):
        p.unlink()
        n += 1
    return JSONResponse({"deleted": n})


@app.get("/")
async def root():
    return JSONResponse({"stub": "anthropic-messages", "silence_seconds": SILENCE,
                         "modes": ["normal", "thinking", "long_silence", "error", "cached"]})
