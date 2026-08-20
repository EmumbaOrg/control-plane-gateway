#!/usr/bin/env python3
"""
Scorecard for the LiteLLM capture spike.

Run this after putting real Claude Code traffic through the proxy. It reads the
capture directory and reports which of the fidelity checks pass, so the
build-vs-adopt decision rests on evidence instead of opinion.

    python3 verify.py                 # uses ./capture
    python3 verify.py path/to/capture

Two checks cannot be judged from files alone and are reported as MANUAL:
long-thinking-pause survival, and unknown-beta pass-through. The README says
how to run those.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

STORE = Path(sys.argv[1] if len(sys.argv) > 1 else "./capture")

PASS, FAIL, WARN, MANUAL = "PASS", "FAIL", "WARN", "MANUAL"
SYMBOL = {PASS: "PASS  ", FAIL: "FAIL  ", WARN: "WARN  ", MANUAL: "MANUAL"}

results: list[tuple[str, str, str]] = []


def report(check: str, status: str, detail: str = "") -> None:
    results.append((check, status, detail))


def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ! could not read {path.name}: {e}")
        return None


def walk_for_types(node, found: set) -> None:
    """Collect every content-block `type` value anywhere in the structure."""
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, str):
            found.add(t)
        for v in node.values():
            walk_for_types(v, found)
    elif isinstance(node, list):
        for v in node:
            walk_for_types(v, found)


def main() -> int:
    if not STORE.exists():
        print(f"No capture directory at {STORE.resolve()}")
        return 1

    index_path = STORE / "index.jsonl"
    rows = []
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    print(f"\nCapture store: {STORE.resolve()}")
    print(f"Exchanges indexed: {len(rows)}")

    # --- 0. anything captured at all -------------------------------------
    if not rows:
        report("Capture fires at all", FAIL,
               "index.jsonl empty — callback not wired, or this route is not logged")
        print_results()
        return 1
    report("Capture fires at all", PASS, f"{len(rows)} exchanges")

    # --- 1. raw incoming body available ----------------------------------
    with_body = [r for r in rows if r.get("has_raw_body")]
    if not with_body:
        report("Raw incoming request body", FAIL,
               "proxy_server_request.body absent — only LiteLLM's normalised view is available")
    elif len(with_body) < len(rows):
        report("Raw incoming request body", WARN,
               f"present on {len(with_body)}/{len(rows)} exchanges")
    else:
        report("Raw incoming request body", PASS, "present on every exchange")

    # --- 2. request completeness: system / tools / tool_result -----------
    saw_system = saw_tools = saw_tool_result = False
    req_files = sorted(STORE.glob("*/*.request.json"))
    for p in req_files[:200]:
        data = read_json(p)
        if not data:
            continue
        body = data.get("body")
        if isinstance(body, dict):
            if body.get("system"):
                saw_system = True
            if body.get("tools"):
                saw_tools = True
        types: set = set()
        walk_for_types(body if body is not None else data.get("litellm_messages"), types)
        if "tool_result" in types:
            saw_tool_result = True

    detail = (f"system={'yes' if saw_system else 'no'} "
              f"tools={'yes' if saw_tools else 'no'} "
              f"tool_result={'yes' if saw_tool_result else 'no'}")
    if saw_system and saw_tools and saw_tool_result:
        report("Request completeness", PASS, detail)
    elif saw_system or saw_tools:
        report("Request completeness", WARN, detail + " — exercise tools then re-run")
    else:
        report("Request completeness", FAIL, detail)

    # --- 3. response completeness ---------------------------------------
    #
    # LiteLLM's callback hands us a NORMALISED response, so the captured object
    # does not use Anthropic's content-block vocabulary. The answer text arrives
    # as a plain STRING at `choices[].message.content`, tool calls as
    # `message.tool_calls` entries typed "function", and reasoning as
    # `thinking_blocks` / `reasoning_content`.
    #
    # Measured 21 Aug 2026 over 51 captured responses: 0 contained a
    # `type: "text"` block and 0 contained `type: "tool_use"`; only the 15 with
    # nested `thinking_blocks` matched the Anthropic vocabulary at all. So the old
    # `"text" in interesting` requirement was unsatisfiable — PASS could never be
    # reached, and the WARN it fell back to advised running a tool-using prompt,
    # which could never clear it either.
    #
    # Both vocabularies are judged below. The Anthropic branch stays first, so if
    # real Anthropic blocks ever do reach the callback they still take precedence.
    resp_types: set = set()
    oa_content = oa_tool_calls = oa_reasoning = False
    for p in sorted(STORE.glob("*/*.response.json"))[:200]:
        data = read_json(p)
        if not data:
            continue
        resp = data.get("response")
        walk_for_types(resp, resp_types)
        if isinstance(resp, dict):
            for choice in resp.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                msg = choice.get("message")
                if not isinstance(msg, dict):
                    continue
                if isinstance(msg.get("content"), str) and msg["content"].strip():
                    oa_content = True
                if msg.get("tool_calls"):
                    oa_tool_calls = True
                if msg.get("thinking_blocks") or msg.get("reasoning_content"):
                    oa_reasoning = True

    interesting = {t for t in resp_types if t in
                   {"text", "thinking", "tool_use", "redacted_thinking"}}
    oa_seen = [n for n, present in (("content", oa_content),
                                    ("tool_calls", oa_tool_calls),
                                    ("reasoning", oa_reasoning)) if present]

    if "text" in interesting and ("tool_use" in interesting or "thinking" in interesting):
        report("Response completeness", PASS, "blocks seen: " + ", ".join(sorted(interesting)))
    elif oa_content and (oa_tool_calls or oa_reasoning):
        report("Response completeness", PASS,
               "normalised shape: " + ", ".join(oa_seen))
    elif oa_seen:
        report("Response completeness", WARN,
               "normalised shape: " + ", ".join(oa_seen) +
               " — run a tool-using prompt to confirm tool calls are captured")
    elif interesting:
        report("Response completeness", WARN,
               "blocks seen: " + ", ".join(sorted(interesting)))
    else:
        report("Response completeness", FAIL,
               "no recognisable content in either the Anthropic or the normalised "
               "shape — response is normalised away")

    # --- 4. prompt caching survived the proxy ---------------------------
    per_session = defaultdict(list)
    for r in rows:
        per_session[r.get("session")].append(r)

    multi = {s: rs for s, rs in per_session.items() if len(rs) > 1 and s != "no-session-id"}
    reads = []
    for rs in multi.values():
        for r in rs:
            v = (r.get("usage") or {}).get("cache_read_input_tokens")
            if isinstance(v, (int, float)):
                reads.append(v)

    if not multi:
        report("Prompt caching preserved", MANUAL,
               "need a multi-turn session — send several prompts in one Claude Code session")
    elif not reads:
        report("Prompt caching preserved", WARN,
               "cache_read_input_tokens not reported in usage — check a _kwargs dump for where it lives")
    elif max(reads) > 0:
        report("Prompt caching preserved", PASS, f"max cache_read_input_tokens={int(max(reads))}")
    else:
        report("Prompt caching preserved", FAIL,
               "cache reads are zero across a multi-turn session — input cost is ~10x what it should be")

    # --- 5. attribution headers ------------------------------------------
    sessions = {r.get("session") for r in rows} - {"no-session-id", None}
    agents = {r.get("agent") for r in rows if r.get("agent")}
    if sessions:
        report("Session attribution", PASS,
               f"{len(sessions)} distinct session id(s)" +
               (f", {len(agents)} sub-agent id(s)" if agents else ", no sub-agent traffic yet"))
    else:
        report("Session attribution", FAIL,
               "x-claude-code-session-id never reached the callback")

    # --- 6. which route -------------------------------------------------
    routes = {r.get("route") for r in rows if r.get("route")}
    if routes:
        report("Route observed", PASS, "; ".join(sorted(str(r) for r in routes))[:200])
    else:
        report("Route observed", WARN, "request URL not exposed in the callback payload")

    # --- 7. streaming captured ------------------------------------------
    streamed = [r for r in rows if r.get("stream")]
    if streamed:
        report("Streaming exchanges captured", PASS, f"{len(streamed)}/{len(rows)}")
    else:
        report("Streaming exchanges captured", WARN,
               "no streamed exchange recorded — Claude Code streams, so check the route is logged")

    # --- 8. count_tokens ------------------------------------------------
    if any("count_tokens" in str(r.get("route") or "") for r in rows):
        report("count_tokens served", PASS, "seen in captured routes")
    else:
        report("count_tokens served", MANUAL,
               "check the proxy log for /v1/messages/count_tokens — a 404 means paid inference calls are used to count context")

    # --- manual-only checks ---------------------------------------------
    report("Long thinking pause (>300s)", MANUAL, "see README check 4 — no config fixes this if it fails")
    report("Unknown anthropic-beta pass-through", MANUAL, "see README check 6")

    print_results()
    return 0 if not any(s == FAIL for _, s, _ in results) else 2


def print_results() -> None:
    width = max(len(c) for c, _, _ in results) + 2
    print("\n" + "-" * 78)
    for check, status, detail in results:
        print(f"{SYMBOL[status]}  {check.ljust(width)} {detail}")
    print("-" * 78)
    fails = [c for c, s, _ in results if s == FAIL]
    if fails:
        print(f"\n{len(fails)} blocking failure(s): " + ", ".join(fails))
        print("Record these in the design doc — they are the evidence for the "
              "adopt-vs-hybrid-vs-build decision.")
    else:
        print("\nNo blocking failures in the file-based checks. Complete the "
              "MANUAL items before concluding LiteLLM is viable.")


if __name__ == "__main__":
    sys.exit(main())
