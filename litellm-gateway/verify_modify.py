#!/usr/bin/env python3
"""
Scorecard for request modification — action item 03.

Offline. No API key, no Docker, no spend: it imports `custom_capture` directly
and drives `_modify()` with hand-built Claude-Code-shaped bodies. Run it after
any change to the clamp or the redaction patterns.

    python3 verify_modify.py

Exit code 0 if everything passes, 2 if anything fails, so it can gate a commit.

What it cannot judge: whether the proxy actually *calls* the hook. That needs the
live gateway — see ../docs/GATEWAY-MODIFY.md for the two curl commands.
"""

import json
import os
import sys
import tempfile

# Force a known configuration, so the result does not depend on whatever is
# currently exported in the shell or set in docker-compose.yml.
os.environ["GATEWAY_MAX_OUTPUT_TOKENS"] = "8000"
os.environ["GATEWAY_REDACT"] = "on"
os.environ["GATEWAY_INJECT_SKILLS"] = "always"
os.environ["CAPTURE_DIR"] = tempfile.mkdtemp(prefix="verify-modify-")

HERE = os.path.dirname(os.path.abspath(__file__))

# Point the injector at the real manifest, but with the container paths rewritten
# to their repo equivalents so this runs without Docker. The trigger regexes and
# the skill bodies under test are therefore the genuine ones, not fixtures.
_manifest = json.load(open(os.path.join(HERE, "skills-inject.json")))
for _s in _manifest["skills"]:
    _s["path"] = _s["path"].replace(
        "/app/skills", os.path.join(HERE, "..", "plugins", "emumba-react", "skills"))
_manifest_path = os.path.join(os.environ["CAPTURE_DIR"], "skills-inject.json")
with open(_manifest_path, "w") as _fh:
    json.dump(_manifest, _fh)
os.environ["GATEWAY_SKILL_MANIFEST"] = _manifest_path

sys.path.insert(0, HERE)
import custom_capture as cc  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def report(check: str, ok: bool, detail: str = "") -> None:
    results.append((check, PASS if ok else FAIL, detail))


def modify(body: dict) -> dict:
    """Run _modify on a deep copy, so a test cannot contaminate the next one."""
    return cc._modify(json.loads(json.dumps(body)), "anthropic_messages", "verify")


def body(model: str, max_tokens: int, text: str = "hello") -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": "system prompt sk-ant-api03-" + "s" * 30}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }


# --- 1. the clamp fires on a translated route -------------------------------
out = modify(body("gemini-3.7-flash", 32000))
report("Clamp applies to a translated route", out["max_tokens"] == 8000,
       f'32000 -> {out["max_tokens"]}')

# --- 2. real Anthropic models are exempt -----------------------------------
exempt_ok = all(
    modify(body(m, 32000))["max_tokens"] == 32000
    for m in ("claude-opus-5", "claude-sonnet-5",
              "claude-haiku-4-5", "claude-haiku-4-5-20251001")
)
report("Anthropic models exempt from the clamp", exempt_ok,
       "32000 preserved on all four aliases")

# --- 3. THE TRAP: a picker alias must NOT be exempted by prefix -----------
# `claude-haiku-4-5-gmn-37-flash` is a picker alias for Gemini and starts with
# `claude-haiku-4-5`. Prefix matching here would silently exempt every
# translated route and defeat the clamp on exactly the models that need it.
traps = ("claude-haiku-4-5-gmn-37-flash", "claude-sonnet-4-5-dsk-v4-pro",
         "claude-sonnet-4-5-zai-47f", "claude-haiku-4-5-nvda-free")
trap_ok = all(modify(body(m, 32000))["max_tokens"] == 8000 for m in traps)
report("Picker aliases still clamped (exact match, not prefix)", trap_ok,
       f"{len(traps)} Claude-shaped aliases for other vendors")

# --- 4. a request already under the ceiling is left alone -----------------
out = modify(body("gemini-3.7-flash", 2000))
report("Requests below the ceiling untouched", out["max_tokens"] == 2000,
       "2000 preserved")

# --- 5. redaction reaches tool_result content ----------------------------
# This is the case that matters: tool_result carries the file contents and
# command output fed back to the model, which is where a credential committed
# to a repo actually surfaces.
secrets = {
    "aws-access-key-id": "AKIAIOSFODNN7EXAMPLE",
    "github-token": "ghp_" + "x" * 36,
    "anthropic-api-key": "sk-ant-api03-" + "a" * 30,
    "google-api-key": "AIza" + "b" * 35,
    "slack-token": "xoxb-1234567890-abcdefghij",
}
nested = {
    "model": "claude-sonnet-5",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "t1",
        "content": [{"type": "text",
                     "text": "\n".join(f"{k}={v}" for k, v in secrets.items())}],
    }]}],
}
out = modify(nested)
leaked = [k for k, v in secrets.items() if v in json.dumps(out)]
report("Secrets redacted inside tool_result", not leaked,
       "all patterns replaced" if not leaked else f"LEAKED: {leaked}")

# --- 6. the cacheable prefix is never rewritten --------------------------
# `system` and `tools` are the cached prefix and are our own content, not
# developer input. Rewriting them risks the caching win for no benefit.
out = modify(body("claude-sonnet-5", 1024))
report("system block left untouched", "sk-ant-api03-" in out["system"][0]["text"],
       "redaction is scoped to messages only")

# --- 7. determinism, which is what protects prompt caching ---------------
# Claude Code resends the whole conversation every turn. A placeholder carrying
# a counter or a timestamp would change the cached prefix each turn — a silent
# ~10x input-cost increase.
a, b = modify(nested), modify(nested)
report("Redaction is deterministic", json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True),
       "identical bytes across runs, so the cached prefix is stable")

# --- 8. no false positive on ordinary prose ------------------------------
prose = ("Refactor the sk parser and check the AKIA constant naming, then "
         "review src/ghp_helpers.py — see docs at https://example.com/api?key=abc")
out = modify(body("claude-sonnet-5", 1024, prose))
report("No false positive on ordinary text",
       out["messages"][0]["content"][0]["text"] == prose,
       "prefix-lookalikes without a real token body are left alone")

# --- 9. fail-open on a malformed body -----------------------------------
# This runs in front of every request. An exception here would take the gateway
# down, so a body that makes no sense must pass through, not raise.
try:
    cc._modify({"model": "x", "max_tokens": "not-an-int", "messages": "not-a-list"},
               "anthropic_messages", None)
    report("Fails open on a malformed body", True, "forwarded without raising")
except Exception as e:
    report("Fails open on a malformed body", False, f"RAISED {type(e).__name__}: {e}")

# --- 10. the audit trail records kind and count, never the value --------
lines = []
path = os.path.join(os.environ["CAPTURE_DIR"], "modifications.jsonl")
if os.path.exists(path):
    lines = [json.loads(x) for x in open(path) if x.strip()]
audit_blob = json.dumps(lines)
value_leaked = [k for k, v in secrets.items() if v in audit_blob]
report("Audit log records kind + count, never the value",
       bool(lines) and not value_leaked,
       f"{len(lines)} entries" + ("" if not value_leaked else f"  LEAKED: {value_leaked}"))


# --- 11. skill injection fires on an in-scope conversation ---------------
# The scenario that motivated this: a React component in a tool_result, on a
# translated route, where the model demonstrably would not call the Skill tool.
react_body = {
    "model": "claude-sonnet-4-5-nvda-super-120b-free",
    "max_tokens": 8000,
    "system": [{"type": "text", "text": "You are Claude Code.",
                "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {"role": "user", "content": [{"type": "text",
         "text": "could you please analyse the code of OrdersDashboard?"}]},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "t1",
            "content": [{"type": "text", "text":
                         "export function OrdersDashboard() {\n"
                         "  useEffect(() => { fetch('/api/orders') }, [region])\n}"}],
        }]},
    ],
}
out = modify(react_body)
injected = json.dumps(out.get("system"))
report("Skill injected on an in-scope conversation",
       cc.INJECT_MARKER in injected and "Eliminating Waterfalls" in injected,
       "react-best-practices body present in `system`")

# --- 12. and NOT on an unrelated one -------------------------------------
# A standard that attaches to every request is not a standard, it is overhead.
out = modify(body("gemini-3.7-flash", 4000,
                  "write me a bash script that tails a log file"))
report("Not injected on an out-of-scope conversation",
       cc.INJECT_MARKER not in json.dumps(out.get("system")),
       "trigger did not match, request left alone")

# --- 13. THE TRAP: injection must not compound over turns ----------------
# Claude Code resends the whole conversation every turn, and a retry replays the
# same body. Without the marker check the skill would be appended again on every
# pass, growing the prompt without bound.
once = modify(react_body)
twice = cc._modify(json.loads(json.dumps(once)), "anthropic_messages", "verify")
report("Injection is idempotent across turns/retries",
       json.dumps(twice).count(cc.INJECT_MARKER) == 1,
       f'{json.dumps(twice).count(cc.INJECT_MARKER)} marker(s) after a second pass')

# --- 14. the cached prefix survives injection ---------------------------
# Claude Code puts its cache_control breakpoint on the last system block it
# sends. Appending AFTER that breakpoint leaves the cached prefix byte-identical.
# Inserting earlier, or writing into an existing block, would invalidate the
# cache every turn — the same ~10x input-cost failure checks 5 and 6 guard.
out = modify(react_body)
original = react_body["system"][0]
prefix_ok = (out["system"][0] == original
             and cc.INJECT_MARKER in out["system"][-1]["text"]
             and "cache_control" not in out["system"][-1])
report("Injected block is appended after the cache breakpoint", prefix_ok,
       "existing blocks byte-identical; our block last and uncached")

# --- 15. determinism, for the same reason as check 7 --------------------
a, b = modify(react_body), modify(react_body)
report("Injection is deterministic",
       json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True),
       "identical bytes across runs")

# --- 16. the audit trail names what was injected ------------------------
lines = [json.loads(x) for x in open(path)] if os.path.exists(path) else []
named = [l for l in lines if "skills_injected" in l.get("changes", {})]
report("Audit log records which skill was injected", bool(named),
       (list(named[-1]["changes"]["skills_injected"]) if named else "no entry"))

# --- 17. `installed` mode leaves non-adopters alone ----------------------
# The consent case. A developer who never installed the plugin is doing React
# work through the same gateway — possibly for a client with different
# standards. In the default mode their prompt must not be touched.
cc.INJECT_MODE = "installed"
try:
    out = modify(react_body)
    report("`installed` mode: no plugin, no injection",
           cc.INJECT_MARKER not in json.dumps(out.get("system")),
           "developer who never opted in is left alone")

    # --- 18. ...and still helps the developer who DID install it --------
    # The measured failure this whole behaviour exists for: plugin installed,
    # skill offered in the catalogue, model never calls it. Claude Code sends
    # that catalogue as a markdown list in a <system-reminder>, in the
    # CONVERSATION rather than the system block.
    adopter = json.loads(json.dumps(react_body))
    adopter["messages"].insert(0, {"role": "user", "content": [{"type": "text", "text":
        "<system-reminder>\nThe following skills are available:\n"
        "- tdd: Test-driven development.\n"
        "- emumba-react:react-best-practices: React and Next.js performance guide.\n"
        "</system-reminder>"}]})
    out = modify(adopter)
    report("`installed` mode: plugin present, skill injected",
           cc.INJECT_MARKER in json.dumps(out.get("system")),
           "catalogue entry detected in the conversation")

    # --- 19. THE TRAP: mentioning a skill is not installing it ----------
    # If a bare name match counted as installation, asking "should I use
    # emumba-react:react-best-practices?" would inject it — and so would this
    # very test file's contents pasted into a prompt.
    mentioner = json.loads(json.dumps(react_body))
    mentioner["messages"].append({"role": "user", "content": [{"type": "text", "text":
        "Do we have emumba-react:react-best-practices available for this repo?"}]})
    out = modify(mentioner)
    report("Mentioning a skill by name does not count as installed",
           cc.INJECT_MARKER not in json.dumps(out.get("system")),
           "detection is anchored on the `- name:` catalogue line")
finally:
    cc.INJECT_MODE = "always"

# --- 20. the audit records which mode was in force ----------------------
lines = [json.loads(x) for x in open(path)] if os.path.exists(path) else []
modes = {l["changes"].get("inject_mode") for l in lines
         if "skills_injected" in l.get("changes", {})}
report("Audit distinguishes enforced from opt-in injection",
       modes == {"always", "installed"},
       f"modes recorded: {sorted(m for m in modes if m)}")


# --- summary ------------------------------------------------------------
width = max(len(c) for c, _, _ in results)
print("\n" + "-" * 78)
print(f"Request modification (action item 03) — clamp={cc.CLAMP_MAX_TOKENS} "
      f"redact={cc.REDACT} inject={len(cc._load_skills())} skill(s)")
print("-" * 78)
for check, status, detail in results:
    print(f"{status}  {check.ljust(width)}  {detail}")
print("-" * 78)

fails = [c for c, s, _ in results if s == FAIL]
if fails:
    print(f"\n{len(fails)} failure(s): " + ", ".join(fails))
    print("Do not ship a modification layer with a failing check — a wrong")
    print("redaction corrupts prompts silently and a wrong exempt list")
    print("disables the clamp where it is needed.")
else:
    print(f"\nAll {len(results)} checks pass. This proves the LOGIC only —")
    print("that the proxy actually calls the hook needs the live gateway.")
    print("See ../docs/GATEWAY-MODIFY.md for the two curl commands.")

sys.exit(2 if fails else 0)
