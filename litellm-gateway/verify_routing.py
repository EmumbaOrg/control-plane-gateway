#!/usr/bin/env python3
"""
Scorecard for content-based route override, and for the model metadata it forces
us to get right.

Offline. No API key, no Docker, no spend: it imports `custom_capture` directly,
drives `_modify()` with Claude-Code-shaped bodies, and drives `_capture()` with
callback payloads shaped like the real ones in capture/_kwargs.

    python3 verify_routing.py

Exit code 0 if everything passes, 2 if anything fails, so it can gate a commit.

THE PROPERTY UNDER TEST, in one sentence: when the gateway silently swaps the
model, every record of the exchange must name the model that ANSWERED, not the
one the developer picked — otherwise usage tracking, cost attribution and
analytics all quietly attribute local-model traffic to Haiku.

Two scenarios, from the requirement:

  routing occurs      selected claude-haiku-4-5 -> served by local-qwen3-8b
                      (ollama_chat/qwen3:8b), captured model = qwen3:8b
  no routing          selected claude-haiku-4-5 -> served by claude-haiku-4-5,
                      captured model = claude-haiku-4-5

What it cannot judge: whether LiteLLM's router honours a model rewritten inside
the pre-call hook. That needs the live gateway — see ../docs/GATEWAY-MODIFY.md.
"""

import json
import os
import sys
import tempfile

# Force a known configuration, so the result does not depend on the shell or on
# docker-compose.yml. Skills are off here: this file is about routing, and
# ../verify_modify.py already covers injection.
os.environ["GATEWAY_MAX_OUTPUT_TOKENS"] = "8000"
os.environ["GATEWAY_REDACT"] = "off"
os.environ["GATEWAY_INJECT_SKILLS"] = "off"
os.environ["CAPTURE_DIR"] = tempfile.mkdtemp(prefix="verify-routing-")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import custom_capture as cc  # noqa: E402

STORE = cc.STORE
PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []

PICKED = "claude-haiku-4-5"          # what the developer selected
LOCAL = "local-qwen3-8b"             # the route the rule sends them to
LOCAL_UPSTREAM = "ollama_chat/qwen3:8b"
LOCAL_SERVED = "qwen3:8b"            # what Ollama reports back


def report(check: str, ok: bool, detail: str = "") -> None:
    results.append((check, PASS if ok else FAIL, detail))


def modify(body: dict) -> dict:
    """Run _modify on a deep copy, so a test cannot contaminate the next one."""
    return cc._modify(json.loads(json.dumps(body)), "anthropic_messages", "verify")


def body(text: str, model: str = PICKED, max_tokens: int = 32000,
         system: str = "You are Claude Code.", tools: bool = False) -> dict:
    out = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
    }
    if tools:
        # A realistic slice of the 33 definitions Claude Code actually sends;
        # only the names matter to the slimmer.
        out["tools"] = [{"name": n, "input_schema": {"type": "object"}} for n in
                        ("Read", "Edit", "Write", "Bash", "Artifact", "Glob",
                         "Grep", "WebFetch")]
    return out


def kwargs_for(served: str | None, upstream: str | None, group: str | None,
               client_model: str | None = PICKED) -> dict:
    """A callback payload shaped like the real ones.

    FIELD LOCATIONS ARE NOT INVENTED. Taken from capture/_kwargs on litellm
    1.98.0, for a request the client sent as `local-qwen3-8b`:

        kwargs["model"]                            "qwen3:8b"
        standard_logging_object["model"]            "ollama_chat/qwen3:8b"
        standard_logging_object["model_group"]      "local-qwen3-8b"
        litellm_params["proxy_server_request"]      original client body

    `proxy_server_request` is recorded by the proxy at INGRESS, before the
    pre-call hook runs, which is why it still carries the picker's model on an
    overridden exchange — and why no state has to be shared between the hooks.
    """
    return {
        "model": served,
        "litellm_params": {
            "proxy_server_request": {
                "url": "http://localhost:4000/v1/messages",
                "method": "POST",
                "headers": {"x-claude-code-session-id": "verify-routing"},
                "body": {"model": client_model, "max_tokens": 32000,
                         "messages": [{"role": "user", "content": "hi"}]},
            },
        },
        "standard_logging_object": {"model": upstream, "model_group": group},
        "optional_params": {"stream": True},
    }


def response_for(model: str | None) -> dict:
    resp = {
        "id": "chatcmpl-1",
        "choices": [{"finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 19501, "completion_tokens": 12},
    }
    if model is not None:
        resp["model"] = model
    return resp


def captured(kwargs: dict, response_obj, ok: bool = True) -> dict:
    """Drive the real logging callback and read back the line it wrote."""
    index = STORE / "index.jsonl"
    before = index.read_text().count("\n") if index.exists() else 0
    cc._capture(kwargs, response_obj, ok=ok)
    lines = index.read_text().splitlines()
    assert len(lines) > before, "capture wrote nothing"
    return json.loads(lines[-1])


# ===========================================================================
# Part 1 — the override itself
# ===========================================================================

# --- 1. the trigger phrase reroutes a hosted selection ---------------------
out = modify(body("Hi I'm Asif from Extreme Networks, review this switch config"))
report("Trigger phrase overrides the selected model",
       out["model"] == LOCAL, f'{PICKED} -> {out["model"]}')

# --- 2. an ordinary request is left alone ----------------------------------
out = modify(body("Refactor this React component to memoise the row list"))
report("Unrelated request keeps the selected model",
       out["model"] == PICKED, f'model stayed {out["model"]}')

# --- 3. case and line breaks must not defeat it ----------------------------
variants = {
    "lowercase": "we are onboarding extreme networks next week",
    "uppercase": "EXTREME NETWORKS ticket 4471",
    "wrapped": "the customer is extreme\nnetworks and the box is a switch",
    "single word": "see the extremenetworks repo",
}
mismatched = [k for k, t in variants.items() if modify(body(t))["model"] != LOCAL]
report("Matching survives case, wrapping and the one-word spelling",
       not mismatched, "all 4 forms trigger" if not mismatched
       else f"missed: {mismatched}")

# --- 4. THE TRAP: no false positive on ordinary prose ----------------------
# "extreme network latency" is the reason the rule matches the plural company
# name and not the singular word pair. A false positive here does not fail
# loudly — it silently answers with an 8B model.
safe = {
    "adjective": "we are seeing extreme network latency in staging",
    "substring": "the extremenetworksmigration branch is stale",
    "hyphenated": "run the extreme-throughput benchmark",
}
tripped = [k for k, t in safe.items() if modify(body(t))["model"] != PICKED]
report("No false positive on adjectival or substring uses",
       not tripped, "all 3 left alone" if not tripped else f"tripped: {tripped}")

# --- 5. the trigger is honoured in the system block too --------------------
# A repo CLAUDE.md saying "this project is for <client>" arrives in `system`,
# not in the conversation, and is the likeliest place for the fact to be
# written down once rather than retyped every turn.
out = modify(body("summarise the diff",
                  system="Project context: this repo is Extreme Networks' "
                         "provisioning service."))
report("Trigger in the system prompt also overrides",
       out["model"] == LOCAL, f'system-only match -> {out["model"]}')

# --- 6. re-entry is a no-op, not a logged override -------------------------
out = modify(body("Extreme Networks switch config", model=LOCAL))
report("Already-local selection is not re-overridden",
       out["model"] == LOCAL, "no change, and nothing to audit")

# --- 7. ORDER: an overridden request must be clamped and slimmed -----------
# This is the check that would catch the override being moved after the other
# steps. Post-override the model is no longer clamp-exempt and now matches the
# `local-` slimming pattern; if the order were wrong the request would keep
# 32000 output tokens and all 8 tool definitions, and Ollama would truncate it
# in silence rather than erroring.
out = modify(body("Extreme Networks config review", max_tokens=32000, tools=True))
report("Override runs before the clamp and the tool slimmer",
       out["model"] == LOCAL and out["max_tokens"] == 8000
       and [t["name"] for t in out["tools"]] == ["Read", "Edit", "Write", "Bash"],
       f'model={out["model"]} max_tokens={out["max_tokens"]} '
       f'tools={len(out["tools"])}')

# --- 8. the same selection is untouched without the trigger ----------------
out = modify(body("plain question", max_tokens=32000, tools=True))
report("Without the trigger, clamp exemption and tools are preserved",
       out["model"] == PICKED and out["max_tokens"] == 32000
       and len(out["tools"]) == 8,
       f'max_tokens={out["max_tokens"]} tools={len(out["tools"])}')

# --- 9. fail-open on a malformed body --------------------------------------
try:
    out = cc._modify({"model": PICKED, "messages": "not-a-list",
                      "system": 42}, "anthropic_messages", "verify")
    report("Malformed body fails open", out.get("model") == PICKED,
           "returned the body unmodified")
except Exception as e:
    report("Malformed body fails open", False, f"raised {type(e).__name__}: {e}")

# --- 10. the audit trail records the swap ----------------------------------
path = STORE / "modifications.jsonl"
lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
overrides = [l for l in lines if "model_overridden" in l.get("changes", {})]
one = overrides[0] if overrides else {}
report("Audit line names both models and the rule that fired",
       bool(overrides)
       and one["changes"]["model_overridden"]["from"] == PICKED
       and one["changes"]["model_overridden"]["to"] == LOCAL
       and one["changes"]["model_overridden"]["rule"] == "extreme-networks-on-prem"
       and one["model"] == LOCAL and one["client_model"] == PICKED,
       f"{len(overrides)} override(s) logged; audit model={one.get('model')} "
       f"client_model={one.get('client_model')}")

# --- 11. the policy is configuration, not code -----------------------------
custom = cc._compile_route_rules(json.dumps(
    [{"name": "acme", "model": "local-qwen3-8b", "keywords": ["acme corp"]}]))
report("Rules are overridable by env, and disablable",
       len(custom) == 1 and custom[0]["name"] == "acme"
       and cc._compile_route_rules("off") == [],
       "GATEWAY_ROUTE_RULES parsed; 'off' yields no rules")

# --- 12. a bad env var falls back to the policy, not to nothing ------------
report("Unparseable rules fall back to the default policy",
       [r["name"] for r in cc._compile_route_rules("{not json")]
       == [r["name"] for r in cc._compile_route_rules("")],
       "default rule still in force")


# ===========================================================================
# Part 2 — the captured metadata, which is the requirement proper
# ===========================================================================

# --- 13. ROUTING OCCURRED: the capture must name qwen, not haiku -----------
line = captured(kwargs_for(LOCAL_SERVED, LOCAL_UPSTREAM, LOCAL),
                response_for(LOCAL_SERVED))
report("Routed: captured model is the model that answered",
       line["model"] == LOCAL_SERVED
       and line["client_model"] == PICKED
       and line["model_group"] == LOCAL
       and line["model_overridden"] is True,
       f'selected={line["client_model"]} captured={line["model"]} '
       f'group={line["model_group"]}')

# --- 14. NO ROUTING: the capture must still name what answered ------------
haiku_full = "claude-haiku-4-5-20251001"
line = captured(kwargs_for(haiku_full, haiku_full, PICKED),
                response_for(haiku_full))
report("Unrouted: captured model is the selected model",
       line["model"] == haiku_full
       and line["client_model"] == PICKED
       and line["model_overridden"] is False,
       f'selected={line["client_model"]} captured={line["model"]}')

# --- 15. THE TRAP: never fall back to the requested model -----------------
# A failed exchange returns no response body. The tempting fallback is the
# client's request — and that is exactly the misattribution this whole change
# exists to prevent. The right answer is the route, or nothing.
line = captured(kwargs_for(None, None, LOCAL), None, ok=False)
report("With no response, capture reports the route, never the request",
       line["model"] == LOCAL and line["client_model"] == PICKED
       and line["model_overridden"] is True,
       f'captured={line["model"]} (not {PICKED})')

# --- 16. and nothing at all is recorded as a gap, not as a guess ----------
line = captured(kwargs_for(None, None, None, client_model=PICKED), None, ok=False)
report("Unknowable model is left null rather than guessed",
       line["model"] is None and line["client_model"] == PICKED,
       "model=null, selection preserved")

# --- 17. the per-exchange files carry the reconciliation ------------------
# `body` in the request file is the client's original, so someone reading a
# capture would otherwise see `claude-haiku-4-5` and no hint that qwen answered.
captured(kwargs_for(LOCAL_SERVED, LOCAL_UPSTREAM, LOCAL), response_for(LOCAL_SERVED))
d = STORE / "verify-routing"
req = json.loads(sorted(d.glob("*.request.json"))[-1].read_text())
resp = json.loads(sorted(d.glob("*.response.json"))[-1].read_text())
report("Request and response files state who really answered",
       req["routing"]["model"] == LOCAL_SERVED
       and req["routing"]["client_model"] == PICKED
       and req["body"]["model"] == PICKED
       and resp["model"] == LOCAL_SERVED,
       "routing block present alongside the untouched original body")

# --- 18. the response's own model wins over LiteLLM's view ---------------
# If a provider answers with a different model than the one requested (a
# silent substitution upstream), the response is the authority.
line = captured(kwargs_for(LOCAL_SERVED, LOCAL_UPSTREAM, LOCAL),
                response_for("qwen3:8b-instruct-q4"))
report("Provider's reported model takes precedence",
       line["model"] == "qwen3:8b-instruct-q4",
       f'captured={line["model"]}')

# --- 19. capture never raises --------------------------------------------
try:
    cc._capture("not-a-dict", None, ok=True)
    cc._capture({}, object(), ok=True)
    report("Capture fails open on junk input", True, "no exception")
except Exception as e:
    report("Capture fails open on junk input", False,
           f"raised {type(e).__name__}: {e}")


# ===========================================================================
# Part 3 — the LiteLLM dashboard row
# ===========================================================================
# `model` and `model_group` in LiteLLM_SpendLogs need nothing from us: the
# router ran after the override, so the dashboard already names and prices the
# local model. What it cannot show unaided is that the developer asked for
# something else — these checks cover the annotation that adds it.

# --- 20. the override is annotated onto the spend log ---------------------
out = modify(dict(body("Extreme Networks review"),
                  metadata={"tags": ["User-Agent: curl"], "user_api_key": "x"}))
md = out["metadata"]
report("Spend log annotated with the developer's selection",
       md["spend_logs_metadata"] == {"client_model": PICKED, "served_by": LOCAL,
                                     "route_rule": "extreme-networks-on-prem",
                                     "matched_keyword": "extreme networks"}
       and md["user_api_key"] == "x",
       "spend_logs_metadata written, existing metadata untouched")

# --- 21. tags are appended, and are what the UI can filter on ------------
report("Routing tags appended without dropping the proxy's own",
       md["tags"] == ["User-Agent: curl",
                      "gateway-routed:extreme-networks-on-prem",
                      f"client-model:{PICKED}"],
       " | ".join(md["tags"][1:]))

# --- 22. the right metadata key per route --------------------------------
# `metadata` on most routes, `litellm_metadata` on the ones in LiteLLM's
# LITELLM_METADATA_ROUTES. Writing the wrong name lands the annotation where
# nothing reads it, and nothing would fail loudly.
out = modify(dict(body("Extreme Networks review"), litellm_metadata={"tags": []}))
report("Existing metadata key is reused, not shadowed",
       "spend_logs_metadata" in out["litellm_metadata"]
       and "metadata" not in out,
       "wrote into litellm_metadata")

# --- 23. and nothing is annotated when nothing was overridden ------------
out = modify(dict(body("ordinary question"), metadata={"tags": ["User-Agent: curl"]}))
report("Unrouted request's spend log is left alone",
       out["metadata"] == {"tags": ["User-Agent: curl"]},
       "no annotation, no tags added")


# --- summary ------------------------------------------------------------
width = max(len(c) for c, _, _ in results)
print("\n" + "-" * 82)
print(f"Route override + model attribution — {len(cc.ROUTE_RULES)} rule(s), "
      f"scan={cc.ROUTE_SCAN_CHARS} chars")
print("-" * 82)
for check, status, detail in results:
    print(f"{status}  {check.ljust(width)}  {detail}")
print("-" * 82)

fails = [c for c, s, _ in results if s == FAIL]
if fails:
    print(f"\n{len(fails)} failure(s): " + ", ".join(fails))
    print("Do not ship this with a failing check. A wrong keyword downgrades")
    print("unrelated work to an 8B model in silence, and wrong attribution")
    print("makes every routed request look like the model it was not.")
else:
    print(f"\nAll {len(results)} checks pass. This proves the LOGIC only — that")
    print("LiteLLM's router honours a model rewritten in the pre-call hook")
    print("needs the live gateway. See ../docs/GATEWAY-MODIFY.md.")

sys.exit(2 if fails else 0)
