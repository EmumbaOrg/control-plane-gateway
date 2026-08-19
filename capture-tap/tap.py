"""
Minimal byte-faithful capture tap.

Forwards Claude Code's requests to Anthropic *unchanged* — including the
Authorization bearer and the anthropic-beta OAuth capability — and copies the
request body and the reassembled response to disk on the way past. No
credential of its own: it rides whatever login Claude Code already has, so it
works on a claude.ai subscription with no API key and no per-token cost.

    uv run --with fastapi --with "uvicorn[standard]" --with httpx \
        uvicorn tap:app --port 8788

Client side (base URL only — do NOT set a credential variable, or the
subscription login is bypassed):

    export ANTHROPIC_BASE_URL="http://localhost:8788"
    claude -p "say hello in three words"
"""

import asyncio
import json
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

UPSTREAM = "https://api.anthropic.com"
STORE = Path("./capture")

# Hop-by-hop / encoding headers we must NOT copy upstream; httpx sets its own.
# Everything else — Authorization, anthropic-beta, anthropic-version, the
# x-claude-code-* headers — is forwarded verbatim.
DROP = {"host", "content-length", "accept-encoding", "connection"}

app = FastAPI()
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))


@app.on_event("startup")
async def startup():
    STORE.mkdir(parents=True, exist_ok=True)


@app.head("/api/hello")
async def hello():
    return Response(status_code=200)


@app.post("/v1/messages")
@app.post("/v1/messages/count_tokens")
async def proxy(request: Request):
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP}
    # Ask upstream for an uncompressed stream. aiter_raw() yields wire bytes as-is,
    # so if upstream gzips them we would relay compressed bytes without the
    # Content-Encoding header the client needs to decode them (the "Failed to
    # parse JSON" symptom) and our own capture would be unreadable. Requesting
    # identity keeps the SSE plain for both the client and the capture.
    headers["accept-encoding"] = "identity"

    url = f"{UPSTREAM}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    started = time.time()
    upstream = await client.send(
        client.build_request("POST", url, content=body, headers=headers),
        stream=True,
    )

    chunks: list[bytes] = []

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():  # raw bytes, pings included
                chunks.append(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            _persist(request, body, b"".join(chunks), upstream.status_code,
                     int((time.time() - started) * 1000))

    # Forward the response headers the client needs, minus hop-by-hop ones that
    # no longer describe our re-chunked stream.
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}
    }
    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
    )


def _persist(request, body, response, status, latency_ms):
    try:
        h = {k.lower(): v for k, v in request.headers.items()}
        session = h.get("x-claude-code-session-id", "no-session")
        d = STORE / session
        d.mkdir(parents=True, exist_ok=True)
        stamp = str(time.time_ns())

        # Plain files: request as .json, response as the raw .sse text stream.
        (d / f"{stamp}.request.json").write_bytes(body)
        (d / f"{stamp}.response.sse.txt").write_bytes(response)

        try:
            model = json.loads(body).get("model")
        except Exception:
            model = None

        with (STORE / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": started_iso(),
                "path": request.url.path,
                "session": session,
                "agent": h.get("x-claude-code-agent-id"),
                "model": model,
                "status": status,
                "latency_ms": latency_ms,
                "request_bytes": len(body),
                "response_bytes": len(response),
                "anthropic_beta": h.get("anthropic-beta"),
            }) + "\n")
        print(f"[tap] captured {request.url.path} status={status} "
              f"model={model} req={len(body)}B resp={len(response)}B -> {d}")
    except Exception as e:
        print(f"[tap] persist failed, forwarding was unaffected: {e}")


def started_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")
