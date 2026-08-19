#!/usr/bin/env python3
"""
Phase 1 checks — no API key, no cost, no Claude Code.

Sends Claude-Code-shaped requests through the gateway to the stub upstream, then
compares three things: what we sent, what the stub received, and what the
gateway's capture callback recorded. Any difference between the first two is
something the gateway changed.

    # terminal 1
    cd stub && uv run --with fastapi --with "uvicorn[standard]" \
        uvicorn stub_upstream:app --port 8080

    # terminal 2 — LiteLLM pointed at the stub (see STUB.md)
    docker compose -f docker-compose.yml -f docker-compose.stub.yml up

    # terminal 3
    uv run --with httpx run_checks.py --gateway http://localhost:4000 \
        --key sk-your-virtual-key

Add --slow to include the 300-second silence test.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

STUB = "http://localhost:8080"
CAPTURE = Path("./capture")

# A request shaped the way Claude Code shapes them: a system array whose first
# block is an attribution block, tool definitions, and a tool_result carrying
# file contents. This is what a gateway has to survive intact.
def cc_request(marker: str, stream: bool = True) -> dict:
    return {
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "stream": stream,
        "system": [
            {"type": "text", "text": "Claude Code v2.1.234 (attribution block)"},
            {"type": "text", "text": "You are an interactive CLI tool.",
             "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [
            {"name": "read", "description": "Read a file",
             "input_schema": {"type": "object",
                              "properties": {"file_path": {"type": "string"}},
                              "required": ["file_path"]}},
            {"name": "bash", "description": "Run a command",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
        ],
        "messages": [
            {"role": "user", "content": f"{marker} read the config file"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_01", "name": "read",
                 "input": {"file_path": "/app/config.yaml"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "content": "SECRET_LOOKING_FILE_BODY\nline2\nline3"}]},
        ],
    }


CC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "some-future-capability-2027-01-01",
    "x-claude-code-session-id": "sess-phase1-0001",
    "x-claude-code-agent-id": "agent-phase1-aaa",
    "x-claude-code-parent-agent-id": "agent-phase1-root",
    "content-type": "application/json",
}

results: list[tuple[str, str, str]] = []


def add(name: str, ok, detail: str = "") -> None:
    status = "PASS" if ok is True else ("FAIL" if ok is False else "SKIP")
    results.append((name, status, detail))


def stub_reset() -> None:
    try:
        httpx.post(f"{STUB}/_stub/reset", timeout=10)
    except Exception as e:
        print(f"Cannot reach the stub at {STUB}: {e}")
        sys.exit(1)


def stub_received() -> list[dict]:
    return httpx.get(f"{STUB}/_stub/received", timeout=30).json()


def send(gateway: str, key: str, body: dict, extra_headers=None, timeout=120):
    headers = dict(CC_HEADERS)
    headers["authorization"] = f"Bearer {key}"
    if extra_headers:
        headers.update(extra_headers)
    url = gateway.rstrip("/") + "/v1/messages"
    chunks: list[str] = []
    with httpx.Client(timeout=timeout) as c:
        if body.get("stream"):
            with c.stream("POST", url, json=body, headers=headers) as r:
                status = r.status_code
                for line in r.iter_lines():
                    chunks.append(line)
        else:
            r = c.post(url, json=body, headers=headers)
            status = r.status_code
            chunks.append(r.text)
    return status, "\n".join(chunks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:4000")
    ap.add_argument("--key", required=True, help="LiteLLM virtual key")
    ap.add_argument("--slow", action="store_true", help="include the 300s silence test")
    args = ap.parse_args()

    print(f"Gateway: {args.gateway}\nStub:    {STUB}\n")
    stub_reset()

    # ---- check 1: a normal streamed exchange gets through ----------------
    try:
        status, text = send(args.gateway, args.key, cc_request("STUB_MODE=thinking"))
        ok = status == 200 and "message_stop" in text
        add("1. Streamed exchange completes", ok, f"HTTP {status}")
    except Exception as e:
        add("1. Streamed exchange completes", False, f"{type(e).__name__}: {e}")
        text = ""

    # ---- check 2: keep-alive ping relayed to the client ------------------
    add("2. Ping relayed to client", "ping" in text,
        "the stub always sends one; if it is missing here, the gateway "
        "reconstructs the stream and long thinking pauses will abort")

    time.sleep(1.0)
    recv = stub_received()
    msgs = [r for r in recv if r["kind"] == "messages"]

    if not msgs:
        add("3. Request reached upstream", False, "the stub received nothing")
        return finish()
    add("3. Request reached upstream", True, f"{len(msgs)} request(s)")
    obs = msgs[0]["observed"]

    # ---- check 4: unknown beta header survives ---------------------------
    add("4. Unknown anthropic-beta forwarded",
        obs.get("anthropic_beta") == CC_HEADERS["anthropic-beta"],
        f"upstream saw {obs.get('anthropic_beta')!r}")

    # ---- check 5: system array preserved, attribution block first --------
    first = obs.get("system_first_block") or {}
    add("5. system array preserved as a list",
        obs.get("system_is_list") is True,
        "collapsing it to a string defeats prompt caching (~10x input cost)")
    add("6. Attribution block still first",
        isinstance(first, dict) and "attribution" in str(first.get("text", "")).lower(),
        f"first block: {str(first)[:60]}")

    # ---- check 7: tools and tool_result survive --------------------------
    add("7. Tool definitions forwarded", (obs.get("tools_count") or 0) >= 2,
        f"upstream saw {obs.get('tools_count')} tool(s)")
    body_text = json.dumps(msgs[0].get("body"))
    add("8. tool_result content forwarded",
        "SECRET_LOOKING_FILE_BODY" in body_text,
        "this is the file content a capture gateway exists to record")

    # ---- check 9: credential swapped, developer token not leaked ---------
    add("9. Gateway swapped the credential",
        obs.get("has_x_api_key") is True or obs.get("has_authorization") is True,
        f"x-api-key={obs.get('has_x_api_key')} authorization={obs.get('has_authorization')}")

    # ---- check 10: do the claude-code headers reach the CAPTURE layer? ----
    # Deliberately not asserted upstream: Anthropic does not need them, and a
    # gateway dropping them on the outbound leg is fine. What matters is that
    # the capture layer sees them, because they are the only attribution keys.
    idx0 = CAPTURE / "index.jsonl"
    seen_in_capture = []
    if idx0.exists():
        for line in idx0.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    seen_in_capture += json.loads(line).get("cc_headers_present") or []
                except json.JSONDecodeError:
                    pass
    add("10. x-claude-code-* reach the capture layer",
        "x-claude-code-session-id" in seen_in_capture,
        f"capture saw {sorted(set(seen_in_capture))}"
        + (f"; upstream saw {obs.get('cc_session')!r} (not required)" if True else ""))

    # ---- check 11: upstream error body passes through unmodified ---------
    try:
        status, text = send(args.gateway, args.key,
                            cc_request("STUB_MODE=error", stream=False))
        add("11. Upstream 400 body unmodified",
            status == 400 and "stub deliberate failure" in text,
            f"HTTP {status}; client retry logic string-matches on this text")
    except Exception as e:
        add("11. Upstream 400 body unmodified", False, f"{type(e).__name__}: {e}")

    # ---- check 12: cache token fields propagate --------------------------
    try:
        status, text = send(args.gateway, args.key,
                            cc_request("STUB_MODE=cached", stream=False))
        add("12. cache_read_input_tokens propagated",
            "cache_read_input_tokens" in text and "45000" in text,
            "the stub reports 45000; if the client never sees it, cost "
            "reporting is broken")
    except Exception as e:
        add("12. cache_read_input_tokens propagated", False, f"{type(e).__name__}: {e}")

    # ---- check 13: count_tokens served -----------------------------------
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(args.gateway.rstrip("/") + "/v1/messages/count_tokens",
                       json={"model": "claude-haiku-4-5",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers={**CC_HEADERS, "authorization": f"Bearer {args.key}"})
        add("13. count_tokens served", r.status_code == 200,
            f"HTTP {r.status_code} — a 404 means paid inference calls are used to count context")
    except Exception as e:
        add("13. count_tokens served", False, f"{type(e).__name__}: {e}")

    # ---- check 14: capture callback fired --------------------------------
    idx = CAPTURE / "index.jsonl"
    if idx.exists() and idx.read_text(encoding="utf-8").strip():
        rows = [json.loads(l) for l in idx.read_text(encoding="utf-8").splitlines() if l.strip()]
        with_body = [r for r in rows if r.get("has_raw_body")]
        add("14. Capture callback fired", True, f"{len(rows)} exchange(s) recorded")
        add("15. Capture has the raw request body", bool(with_body),
            f"{len(with_body)}/{len(rows)} — without this, capture is LiteLLM's "
            "normalised view rather than what Claude Code actually sent")
    else:
        add("14. Capture callback fired", False,
            "capture/index.jsonl empty — this route is not logged")
        add("15. Capture has the raw request body", None, "no captures to inspect")

    # ---- check 16 (slow): 300s silence -----------------------------------
    if args.slow:
        print("\nRunning the 300-second silence test — this takes ~6 minutes...")
        try:
            t0 = time.time()
            status, text = send(args.gateway, args.key,
                                cc_request("STUB_MODE=long_silence"), timeout=420)
            add("16. Survives a 310s silent gap",
                status == 200 and "message_stop" in text,
                f"completed in {int(time.time() - t0)}s")
        except Exception as e:
            add("16. Survives a 310s silent gap", False,
                f"{type(e).__name__}: {e} — this is the go/no-go failure")
    else:
        add("16. Survives a 310s silent gap", None, "re-run with --slow")

    return finish()


def finish() -> int:
    width = max(len(n) for n, _, _ in results) + 2
    print("\n" + "-" * 96)
    for name, status, detail in results:
        print(f"{status:<5} {name.ljust(width)} {detail}")
    print("-" * 96)
    fails = [n for n, s, _ in results if s == "FAIL"]
    if fails:
        print(f"\n{len(fails)} failure(s):")
        for f in fails:
            print(f"  - {f}")
        print("\nThese are the evidence for the adopt / hybrid / build decision.")
        return 2
    print("\nNo failures. Complete the SKIPped checks before concluding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
