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

REQUEST MODIFICATION (action item 03)
------------------------------------
Besides recording, this callback also *modifies* requests on the way past, via
`async_pre_call_hook`. Four behaviours, each individually switchable and all
fail-open:

  0. Route override      — the model is rewritten when the conversation matches
     a routing rule, so client work scoped to on-prem inference reaches the
     private local model whatever the developer picked. This one changes WHICH
     MODEL ANSWERS, so it also changes what has to be recorded: see
     `_actual_model` and `_routing_of` — the captured `model` is resolved from
     the response, never from the request.
  1. `max_tokens` clamp   — a server-side output ceiling, so every client is
     capped at once. The desktop app has no `CLAUDE_CODE_MAX_OUTPUT_TOKENS`
     equivalent, so this is the only place it can be done for it.
  2. Secret redaction     — high-specificity credential patterns in outbound
     message content are replaced before the request leaves our network.
  3. Skill injection      — org engineering standards are appended to `system`
     when the conversation matches their scope, so they apply on every model
     instead of only on models that choose to call the `Skill` tool.

Confirmed on litellm 1.98.0: the pre-call hook DOES fire on `/v1/messages`
(`call_type="anthropic_messages"`), which is the route Claude Code uses — the
route dispatches through `ProxyBaseLLMRequestProcessing.base_process_llm_request`,
which awaits `proxy_logging_obj.pre_call_hook` and uses its return value. Note
this is unaffected by the `anthropic-beta` header gap in ../docs/FINDINGS.md: that gap is
about outbound *headers* not being populated on this route, whereas the request
*body* is passed in and returnable.

KNOWN FIDELITY CAVEAT
---------------------
For streaming requests, `response_obj` is LiteLLM's *reassembled* response
object, not the raw server-sent-event bytes from the provider. So this callback
captures a normalised view of the response, not a byte-faithful one. That is a
property of callback-based capture, not a bug here — and it is one of the
findings the spike is meant to establish. See README.md, check 2.

HOW THIS FILE IS LAID OUT
-------------------------
Each behaviour owns one section, and each section carries its own configuration
next to the code that reads it — so changing the clamp means editing one block,
not hunting through a shared config header. Sections run in roughly the order a
request meets them.

   1. Imports and shared state
   2. Shared helpers
   3. MODEL SWITCHING — content-based route override      (request, step 1)
   4. OUTPUT CEILING — max_tokens clamp                   (request, step 2)
   5. SECRET REDACTION                                    (request, step 3)
   6. SKILL INJECTION                                     (request, step 4)
   7. TOOL SLIMMING                                       (request, step 5)
   8. AUDIT TRAIL — modifications.jsonl
   9. REQUEST MODIFICATION PIPELINE — `_modify`, which orders 3-7
  10. RESPONSE CAPTURE — locating the exchange
  11. RESPONSE CAPTURE — which model actually answered
  12. RESPONSE CAPTURE — writing it to disk
  13. CALLBACK REGISTRATION — the hooks LiteLLM calls

⚠ ONE ORDERING CONSTRAINT AT IMPORT TIME: section 3 compiles `ROUTE_RULES` when
this module loads. Everything else is either a constant or a function body, so
sections 4-13 can be reordered freely; section 3 cannot move above section 1.
"""

# =============================================================================
# SECTION 1 — IMPORTS AND SHARED STATE
# =============================================================================

import json
import os
import re
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

# Guards every write to a shared file or counter. The proxy is async and a
# single worker still interleaves requests, so `capture/index.jsonl` and the
# per-session sequence number both need one.
_lock = threading.Lock()
_dumped = 0
_skills = None  # lazily loaded injectable skills; see _load_skills()

# Headers Claude Code sends that give us attribution without parsing bodies.
CC_HEADERS = (
    "x-claude-code-session-id",
    "x-claude-code-agent-id",
    "x-claude-code-parent-agent-id",
)


# =============================================================================
# SECTION 2 — SHARED HELPERS
#
# Used by more than one section below. Anything used by exactly one behaviour
# lives in that behaviour's own section instead.
# =============================================================================


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


def _slug(value) -> str:
    """Filesystem-safe fragment for a filename."""
    keep = [c if (c.isalnum() or c == "-") else "-" for c in str(value).lower()]
    return "".join(keep).strip("-")[:24] or "unknown"


def _scan_text(messages, budget: int) -> str:
    """Concatenate message text, newest first, up to `budget` characters.

    Newest first because that is where the signal is: the file the developer just
    opened, the question they just asked. Bounding it keeps the cost of the
    trigger check flat as a session grows.

    Shared by the route override (section 3) and skill injection (section 6);
    they pass different budgets, for reasons each section explains.
    """
    parts, total = [], 0
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if total >= budget:
            break
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            chunks = [content]
        elif isinstance(content, list):
            chunks = []
            for block in content:
                if isinstance(block, dict):
                    inner = block.get("text") or block.get("content")
                    if isinstance(inner, str):
                        chunks.append(inner)
                    elif isinstance(inner, list):
                        chunks += [b.get("text", "") for b in inner
                                   if isinstance(b, dict)]
                elif isinstance(block, str):
                    chunks.append(block)
        else:
            continue
        for chunk in chunks:
            parts.append(chunk[:budget])
            total += len(chunk)
    return "\n".join(parts)


def _system_text(system) -> str:
    """Flatten `system`, which arrives as a string on one route and a list of
    text blocks on the other. Scanned as well as the messages because a repo's
    CLAUDE.md — the most likely place for "this project is for <client>" to be
    written down once — is delivered there, not in the conversation."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            block["text"] for block in system
            if isinstance(block, dict) and isinstance(block.get("text"), str))
    return ""


# =============================================================================
# SECTION 3 — MODEL SWITCHING: CONTENT-BASED ROUTE OVERRIDE
#
# Request-modification step 1. THIS IS THE SECTION THAT CHANGES WHICH MODEL
# ANSWERS. Read it together with section 11, which is how the swap is made
# visible in the record afterwards.
#
# Config, rule compilation, spend-log annotation and the override itself.
# =============================================================================

# WHY THIS EXISTS. Some client material must not leave the building. Asking every
# developer to remember to switch the picker to the local model whenever they
# touch that client's work is a control that fails the first time someone is in a
# hurry. So the decision moves here: the developer keeps whatever model they
# picked, and when the conversation is about a client scoped to on-prem
# inference, the gateway sends the request to the private local model instead.
#
# THIS RUNS FIRST, before the clamp and the tool slimming, and the order is
# load-bearing: both of those key off `data["model"]`, so they must see the model
# that will actually serve the request. Slimming in particular is what makes an
# overridden request fit a 32,768-token model at all.
#
# ⚠ THE HONEST COSTS, all three:
#
#   1. CAPABILITY. qwen3:8b is not Haiku. The developer asked for one model and
#      got another, so the swap has to be VISIBLE rather than quietly absorbed —
#      which is why the captured `model` is resolved from what answered (see
#      `_actual_model`) and every override lands in modifications.jsonl.
#   2. CONTEXT. The client believes it is talking to a 200k-window model and
#      builds prompts to match; the local route holds 32,768 and Ollama
#      TRUNCATES PAST IT IN SILENCE (see SLIM_TOOLS_MATCH below). Slimming buys
#      ~14.5k tokens back, which is enough for ordinary turns and not enough for
#      a long session. There is no fix for this inside the gateway — the client's
#      context ceiling is set per selected model, and the client selected Haiku.
#   3. NO MEMORY. This is a per-request decision read out of the request text.
#      The only state is the history Claude Code resends every turn, so if the
#      trigger phrase falls outside ROUTE_SCAN_CHARS on a later turn, the route
#      flips back to the hosted model mid-conversation. That is why the budget
#      defaults an order of magnitude above the injector's 400k rather than
#      sharing it.
#
# Rules are ordered and the FIRST match wins; a rule whose target model is
# already the one requested is skipped, so re-entry is a no-op.
_DEFAULT_ROUTE_RULES = [
    {
        "name": "extreme-networks-on-prem",
        "model": "local-qwen3-8b",
        # ONE keyword, matched deliberately WIDELY — see `_compile_route_rules`
        # for the exact pattern. Singular and plural both trigger, so does any
        # separator or none (`extreme-network`, `extremenetwork`), and so does
        # the phrase appearing inside a longer word
        # (`extremenetworksmigration`, a branch name).
        #
        # ⚠ THIS OVER-MATCHES ON PURPOSE, and the cost is real: "extreme network
        # latency" now routes an unrelated request to an 8B model. That is the
        # accepted trade — a rule about where a client's data may be processed
        # should fail toward keeping data in, not toward letting it out. Decided
        # 7 Sep 2026; the narrow word-boundary version is in git history if the
        # trade is ever revisited.
        "keywords": ["extreme network"],
    },
]

# JSON override for the rules above, so the policy is deployment configuration
# rather than a code change. "off" disables content-based routing entirely.
_ROUTE_RULES_RAW = os.environ.get("GATEWAY_ROUTE_RULES", "").strip()

# How much conversation text to scan for a trigger. See cost 3 above for why
# this is large: a mid-conversation route flip is worse than the scan cost.
ROUTE_SCAN_CHARS = int(os.environ.get("GATEWAY_ROUTE_SCAN_CHARS", "2000000"))


def _compile_route_rules(raw: str):
    r"""Parse and compile the rules once, at import.

    MATCHING IS DELIBERATELY WIDE. A keyword's words are joined by `[\s\-_]*` —
    zero or more spaces, hyphens or underscores — and the pattern carries NO
    `\b` anchors, so it is a plain substring search. One keyword therefore
    covers every spelling a developer might actually type:

        "extreme network"  matches  extreme networks, Extreme Network,
                                    extreme-network, extreme_networks,
                                    extremenetwork, extremenetworkstaging,
                                    extremenetworksmigration,
                                    extreme\nnetworks   (wrapped across a line)

    ⚠ IT ALSO MATCHES ORDINARY PROSE: "extreme network latency" trips this rule
    and sends that request to the local model. That is the accepted trade, not
    an oversight — see `_DEFAULT_ROUTE_RULES`. The consequence for whoever edits
    a rule: a SHORT or COMMON keyword is now dangerous in a way it was not when
    boundaries were enforced. `keywords: ["net"]` would reroute most of the
    working day. Keep every keyword a distinctive multi-word name.
    """
    if raw.lower() in ("off", "0", "false", "no", "none"):
        return []
    rules = _DEFAULT_ROUTE_RULES
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError("GATEWAY_ROUTE_RULES must be a JSON list")
            rules = parsed
        except Exception as e:
            # Fail to the default rather than to nothing: an unparseable env var
            # is an operator typo, and dropping the policy silently is the worse
            # of the two failures.
            print(f"[route] bad GATEWAY_ROUTE_RULES, using defaults: "
                  f"{type(e).__name__}: {e}")

    compiled = []
    for rule in rules:
        try:
            target = str(rule["model"]).strip()
            patterns = [
                (kw, re.compile(
                    r"[\s\-_]*".join(re.escape(w) for w in str(kw).split()),
                    re.IGNORECASE))
                for kw in rule.get("keywords") or [] if str(kw).strip()
            ]
            if target and patterns:
                compiled.append({
                    "name": str(rule.get("name") or target),
                    "model": target,
                    "patterns": patterns,
                })
        except Exception as e:
            print(f"[route] skipping malformed rule {rule!r}: "
                  f"{type(e).__name__}: {e}")
    return compiled


# ⚠ Compiled at import. This is the one line in the file with a hard ordering
# dependency — it needs the imports and the function above, nothing else.
ROUTE_RULES = _compile_route_rules(_ROUTE_RULES_RAW)


def _annotate_spend_log(data: dict, override: dict) -> None:
    """Put the override on the row the LiteLLM dashboard shows.

    THE PROBLEM THIS SOLVES. The dashboard's Logs page already names the model
    that ANSWERED — `model` and `model_group` in `LiteLLM_SpendLogs` come from
    the router, and the router ran after we rewrote `data["model"]`, so a routed
    request is logged and priced as `ollama_chat/qwen3:8b` with no help from us.
    What the row cannot show is that the developer asked for something else:
    a forced route and a deliberate local selection look identical there.

    Two supported fields carry that, both read from the request's metadata dict
    (litellm 1.98.0, `proxy/spend_tracking/spend_tracking_utils.py`):

      spend_logs_metadata  copied verbatim into SpendLogs.metadata, visible in
                           the log's detail view (line 125, `clean_metadata`)
      tags                 stored in SpendLogs.request_tags, which the Logs page
                           can FILTER on (line 285) — so "show me every request
                           that was rerouted" is a UI query, not a grep

    The metadata dict is already on `data` by the time this runs:
    `add_litellm_data_to_request` is awaited at common_request_processing.py:1370
    and the pre-call hook at :1518. Its KEY DIFFERS BY ROUTE — `metadata` on
    most, `litellm_metadata` on the ones in LITELLM_METADATA_ROUTES — so the
    existing key is reused where there is one, and writing to the wrong name
    would land the annotation somewhere nothing reads.

    Tags are APPENDED. The proxy has already put its own in there (User-Agent),
    and replacing the list would drop them.
    """
    key = "litellm_metadata" if isinstance(data.get("litellm_metadata"), dict) else "metadata"
    md = data.get(key)
    if not isinstance(md, dict):
        md = {}
        data[key] = md

    existing = md.get("spend_logs_metadata")
    md["spend_logs_metadata"] = {
        **(existing if isinstance(existing, dict) else {}),
        # Named `client_model` to match index.jsonl and modifications.jsonl —
        # one vocabulary across all three records.
        "client_model": override["from"],
        "served_by": override["to"],
        "route_rule": override["rule"],
        "matched_keyword": override["matched"],
    }

    tags = md.get("tags")
    tags = list(tags) if isinstance(tags, list) else []
    for tag in (f"gateway-routed:{override['rule']}",
                f"client-model:{override['from']}"):
        if tag not in tags:
            tags.append(tag)
    md["tags"] = tags


def _route_override(data: dict, changes: dict) -> None:
    """Rewrite `data["model"]` when the conversation matches a routing rule.

    Mutates `data` in place, and returns on the FIRST matching rule so the
    manifest order is the precedence order.

    A rule that matches but names the model already requested is a no-op and is
    NOT logged: a developer who picked the local route deliberately should not
    generate override lines in the audit trail.
    """
    if not ROUTE_RULES:
        return

    requested = data.get("model") or ""
    haystack = "\n".join((
        _system_text(data.get("system")),
        _scan_text(data.get("messages"), ROUTE_SCAN_CHARS),
    ))

    for rule in ROUTE_RULES:
        for keyword, pattern in rule["patterns"]:
            if not pattern.search(haystack):
                continue
            # Matched. Return either way — a later rule must not get a second
            # opinion on a conversation the first one has already claimed.
            if rule["model"] != requested:
                data["model"] = rule["model"]
                changes["model_overridden"] = {
                    "from": requested,
                    "to": rule["model"],
                    "rule": rule["name"],
                    "matched": keyword,
                }
                _annotate_spend_log(data, changes["model_overridden"])
            return


# =============================================================================
# SECTION 4 — OUTPUT CEILING: max_tokens CLAMP
#
# Request-modification step 2. Configuration only — the clamp is four lines and
# lives inline in `_modify` (section 9), because splitting it into a function
# would be more indirection than logic.
# =============================================================================

# Server-side output ceiling. Claude Code reserves 32000 output tokens on every
# request and providers gate on the *reservation*, not on what is produced — so
# a low-balance account 402s on traffic that would have used 60 tokens. The CLI
# can be fixed with CLAUDE_CODE_MAX_OUTPUT_TOKENS; the desktop app exposes no
# equivalent, which is why this has to happen here.
#
# 0 disables the clamp entirely.
CLAMP_MAX_TOKENS = int(os.environ.get("GATEWAY_MAX_OUTPUT_TOKENS", "8000"))

# Models the clamp does NOT apply to. The clamp exists to stop non-Anthropic
# providers rejecting the reservation; Anthropic honours 32000 happily, and
# clamping it there would truncate long answers for no reason.
#
# EXACT matches, never prefixes. `claude-haiku-4-5-gmn-37-flash` is a *picker
# alias for Gemini* and starts with `claude-haiku-4-5` — prefix matching would
# silently exempt every translated route and defeat the clamp.
CLAMP_EXEMPT = {
    m.strip() for m in os.environ.get(
        "GATEWAY_CLAMP_EXEMPT_MODELS",
        "claude-opus-5,claude-sonnet-5,claude-haiku-4-5,claude-haiku-4-5-20251001",
    ).split(",") if m.strip()
}


# =============================================================================
# SECTION 5 — SECRET REDACTION
#
# Request-modification step 3. Config, the patterns, and the content walk.
# =============================================================================

# Secret redaction. Off unless explicitly enabled: it rewrites developer content,
# so it should be a deliberate decision, not a default someone inherits.
REDACT = os.environ.get("GATEWAY_REDACT", "off").lower() in ("1", "true", "on", "yes")

# Only high-specificity patterns — a vendor-prefixed token or a PEM header, never
# "looks like entropy". A false positive here silently corrupts a developer's
# prompt, and they have no way to see that it happened.
#
# Replacements are DETERMINISTIC (no counters, no timestamps). Claude Code
# resends the whole conversation every turn, so a placeholder that varied between
# turns would change the cached prefix and defeat prompt caching — a silent ~10x
# input-cost increase, which is exactly what fidelity checks 5 and 6 guard.
SECRET_PATTERNS = (
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-api-key", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
)

# Guard against pathological nesting in a hand-crafted body.
_MAX_WALK_DEPTH = 12


def _redact_text(text: str, counts: dict) -> str:
    """Replace credential patterns in one string. Deterministic — same input
    always yields the same output, so the cached prefix stays stable."""
    for kind, pattern in SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text


def _redact_in_place(node, counts: dict, depth: int = 0):
    """Walk message content and redact every string found.

    Deliberately applied to `messages` only — never to `system` or `tools`.
    Those two are the cacheable prefix and are our own content, not developer
    input, so rewriting them buys nothing and risks the caching win.

    `tool_result` blocks are the reason this exists: they carry the actual file
    contents and command output fed back to the model, which is where a
    credential in a repo would surface.
    """
    if depth > _MAX_WALK_DEPTH:
        return node
    if isinstance(node, str):
        return _redact_text(node, counts)
    if isinstance(node, list):
        for i, item in enumerate(node):
            node[i] = _redact_in_place(item, counts, depth + 1)
        return node
    if isinstance(node, dict):
        for key, value in node.items():
            # Only text-bearing fields. Skipping ids, types and cache_control
            # keeps the walk cheap and avoids rewriting structural values.
            if key in ("text", "content", "thinking"):
                node[key] = _redact_in_place(value, counts, depth + 1)
        return node
    return node


# =============================================================================
# SECTION 6 — SKILL INJECTION
#
# Request-modification step 4. Config, manifest loading, trigger detection and
# the append into `system`.
# =============================================================================

# Distribution through the plugin marketplace is deterministic; *activation* is
# not. Measured 31 Aug 2026 on `claude-sonnet-4-5-nvda-super-120b-free`: the
# skill was installed, its catalogue entry was in the request and the `Skill`
# tool was in the tools array — and the model never called it, answering a React
# question from its own reasoning instead. A weaker model simply does not reach
# for a skill, so a skill delivered as a *tool the model may choose* is a
# capability the gateway cannot guarantee.
#
# Injecting the skill body into the request removes the model's choice from the
# path. The guidance is present as context, so it applies on any model that can
# read its prompt — which is every model, by definition.
#
# Three modes, because "should this apply to someone who never asked for it?" is
# a policy question, not a technical one:
#
#   off        no injection at all.
#   installed  (default) inject ONLY when the developer already has the plugin
#              enabled — detected from the skill catalogue Claude Code sends in
#              the conversation. This treats injection as an ACTIVATION aid, not
#              a distribution mechanism: it fixes exactly the measured failure
#              (installed, offered, never called) and imposes on nobody else.
#   always     inject regardless. Central enforcement in the strong sense — a
#              developer cannot escape the standard by disabling the plugin. It
#              also modifies the prompts of people who never opted in, including
#              anyone doing client work under different standards, so it is a
#              deliberate decision with a named owner, not a default.
_MODE = os.environ.get("GATEWAY_INJECT_SKILLS", "installed").strip().lower()
INJECT_MODE = (
    "off" if _MODE in ("0", "false", "off", "no", "") else
    "always" if _MODE in ("always", "all", "enforce") else
    "installed"
)

# Which skills, and what makes each one relevant. A manifest rather than
# hardcoded paths so adding the second skill is a data change, not a code change.
SKILL_MANIFEST = Path(os.environ.get(
    "GATEWAY_SKILL_MANIFEST", "/app/skills-inject.json"))

# Optional exact-match allowlist. Empty (the default) means every model, which is
# the honest reading of "the standard applies to everyone". Narrowing it to the
# non-Anthropic routes would save tokens where native activation already works,
# at the cost of the guarantee no longer being uniform.
INJECT_MODELS = {
    m.strip() for m in os.environ.get("GATEWAY_INJECT_SKILL_MODELS", "").split(",")
    if m.strip()
}

# Ceiling per skill body. A skill large enough to crowd out the conversation is a
# worse outcome than a skill that arrives abridged.
INJECT_MAX_CHARS = int(os.environ.get("GATEWAY_INJECT_MAX_CHARS", "20000"))

# How much conversation text to scan for the trigger. Claude Code resends the
# whole history every turn and it grows without bound; the relevant signal is
# near the end, so the scan runs backwards from the newest message.
INJECT_SCAN_CHARS = int(os.environ.get("GATEWAY_INJECT_SCAN_CHARS", "400000"))

# Marks our own block so a retry, or a second gateway in the path, cannot inject
# the same skill twice.
INJECT_MARKER = "<!-- emumba-gateway-skill:"


def _strip_frontmatter(text: str) -> str:
    """Drop a leading `---` YAML block. The frontmatter is Claude Code's skill
    *registration* metadata — name, description, tags. Injected as context it is
    noise at best, and at worst it reads as an instruction to go and load a skill
    that is already present."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def _load_skills():
    """Read the manifest once and keep it. Each entry becomes
    {name, trigger (compiled), body}.

    Lazy rather than import-time so a missing mount at boot does not have to be
    diagnosed from a stack trace, and so tests can point at their own manifest.
    Anything unreadable is skipped with a log line — a skill we could not load is
    a gap, not a reason to refuse traffic.
    """
    global _skills
    if _skills is not None:
        return _skills
    loaded = []
    try:
        manifest = json.loads(SKILL_MANIFEST.read_text())
        for entry in manifest.get("skills", []):
            name = entry.get("name") or "unnamed"
            try:
                path = Path(entry["path"])
                body = _strip_frontmatter(path.read_text()).strip()
                if len(body) > INJECT_MAX_CHARS:
                    body = body[:INJECT_MAX_CHARS] + "\n\n[truncated by gateway]"
                loaded.append({
                    "name": name,
                    "trigger": re.compile(entry["trigger"], re.IGNORECASE),
                    "body": body,
                })
                print(f"[inject] loaded skill '{name}' ({len(body)} chars) "
                      f"from {path}")
            except Exception as e:
                print(f"[inject] skipping skill '{name}': {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[inject] no injectable skills ({SKILL_MANIFEST}): "
              f"{type(e).__name__}: {e}")
    _skills = loaded
    return _skills


# Claude Code's injected context blocks. The closing tag is optional on the
# LAST one on purpose: the scan budget can truncate mid-block, and a trailing
# unterminated `<system-reminder>` would otherwise keep its catalogue in scope
# and reintroduce the self-triggering it exists to remove.


def _skill_is_installed(name: str, haystack: str) -> bool:
    """Has this client actually got the skill enabled?

    Claude Code advertises its available skills to the model as a catalogue in a
    `<system-reminder>` inside the CONVERSATION — not in the `system` block,
    which is where you would expect it. Each entry is a markdown list item:

        - emumba-react:react-best-practices: Comprehensive React and Next.js …

    So the request itself tells us whether the developer has the plugin, and we
    can make injection an activation aid for people who opted in rather than a
    standard imposed on people who did not.

    Anchored on the leading `- ` and the trailing `:` so a developer merely
    *mentioning* the skill by name in a question does not read as installation.
    """
    return re.search(r"^[ \t]*-[ \t]+" + re.escape(name) + r"\s*:",
                     haystack, re.MULTILINE) is not None


def _already_injected(system) -> bool:
    if isinstance(system, str):
        return INJECT_MARKER in system
    if isinstance(system, list):
        return any(INJECT_MARKER in block.get("text", "")
                   for block in system if isinstance(block, dict))
    return False


def _skill_block(skill) -> str:
    return (
        f"{INJECT_MARKER}{skill['name']} -->\n"
        f"# Mandatory engineering standard: {skill['name']}\n\n"
        "This guidance was attached by the Emumba gateway because the current "
        "conversation matches its scope. It is not optional context and it is "
        "not a suggestion to load a skill — the content is already below.\n\n"
        "Apply it directly when reviewing, writing or refactoring code in this "
        "conversation, and say which of its rules you applied. If the fuller "
        "rule set is available locally under the skill's `references/` folder, "
        "read that before giving detailed advice.\n\n"
        "---\n\n"
        f"{skill['body']}"
    )


def _inject_skills(data: dict, changes: dict) -> None:
    """Append matching skill bodies to `system`, as trailing text blocks.

    APPENDED, never inserted. Claude Code puts its `cache_control` breakpoint on
    the last system block it sends; anything added after that breakpoint sits
    outside the cached prefix, so the prefix stays byte-identical and prompt
    caching is unaffected. Inserting earlier — or writing into an existing block
    — would invalidate the cache on every turn, which is the ~10x input-cost
    failure that fidelity checks 5 and 6 exist to catch.

    For the same reason the block carries no `cache_control` of its own: adding a
    breakpoint here would change how the whole request is cached.
    """
    skills = _load_skills()
    if not skills:
        return
    if INJECT_MODELS and data.get("model") not in INJECT_MODELS:
        return
    system = data.get("system")
    if _already_injected(system):
        return

    haystack = _scan_text(data.get("messages"), INJECT_SCAN_CHARS)
    if not haystack:
        return
    matched = [s for s in skills if s["trigger"].search(haystack)]
    if INJECT_MODE == "installed":
        matched = [s for s in matched if _skill_is_installed(s["name"], haystack)]
    if not matched:
        return

    blocks = [{"type": "text", "text": _skill_block(s)} for s in matched]
    if system is None:
        data["system"] = blocks
    elif isinstance(system, str):
        data["system"] = [{"type": "text", "text": system}] + blocks
    elif isinstance(system, list):
        system.extend(blocks)
    else:
        return  # unrecognised shape; leave the request alone

    # The mode is recorded because it is the difference between "helped someone
    # who opted in" and "modified the prompt of someone who did not". An audit
    # trail that cannot distinguish those two is not much of an audit trail.
    changes["skills_injected"] = {s["name"]: len(s["body"]) for s in matched}
    changes["inject_mode"] = INJECT_MODE


# =============================================================================
# SECTION 7 — TOOL SLIMMING
#
# Request-modification step 5, and the one that makes the local route in
# section 3 usable at all.
# =============================================================================

# THE PROBLEM THIS SOLVES, measured from a real captured request:
#
#   tools (33 built-in)   36,212 tok   <- 74% of the whole request
#   system prompt          9,237 tok
#   messages               3,188 tok
#   TOTAL                 48,638 tok   vs a 32,768-token context ceiling
#
# `Artifact` alone is ~9,500 tokens. So a local model does not fail because it
# is small or slow — it fails before inference starts, on a request that cannot
# fit.
#
# WHAT THIS LOOKS LIKE ON OLLAMA, and it is worse than an error. Ollama does
# NOT reject an oversized request: it silently truncates anything past num_ctx
# (config.yaml sets 32768 on the ollama_chat/ entries). There is no 400, no
# warning, and a 200 all the way back to the client — just a model that never
# saw the end of its own prompt and answers as if the tools and instructions it
# was given do not exist. Nothing in the stack reports it.
#
# MEASURED ON THIS HOST, 3 Sep 2026, ollama_chat/qwen3:8b through the picker
# alias — from capture/modifications.jsonl, not estimated:
#
#   tools_slimmed   36 defs -> 3      ~14,525 tok saved
#   forwarded request                 19,501 tok   (fits 32,768)
#   unslimmed equivalent             ~34,000 tok   (would have been truncated)
#
# So on this route the hook is not an optimisation, it is the difference
# between a working model and a silently-lobotomised one.
#
# WHY THIS HAS TO BE SERVER-SIDE, and is not just a duplicate of the CLI's
# `--tools` flag: THE DESKTOP APP HAS NO `--tools` EQUIVALENT. Same gap as the
# max_tokens clamp above — the CLI has an env var, the app has nothing — so the
# gateway is the only place that can fix it for both clients at once. Doing it
# here is what makes a local route usable from the picker at all.
#
# Trimming to Read/Edit/Write/Bash takes the request to ~13.5k tokens, leaving
# ~19k of headroom inside 32,768 for actual conversation.
#
# ⚠ THE HONEST COST. The app still OFFERS the features whose tools we removed,
# and the model simply cannot do them — it will not say "I lack that tool", it
# will improvise. That is a real usability tax, which is why this is scoped by
# model name and defaults to local routes only. Do NOT widen the pattern to
# hosted models to save tokens; there it would silently downgrade a model that
# had no problem in the first place.
#
# Matching is SUBSTRING, unlike CLAMP_EXEMPT above which is deliberately exact.
# The reason differs: the clamp exempts a few named models and a prefix bug
# there would silently exempt every translated alias, whereas this must catch
# both naming schemes for the same route (`local-qwen3-8b` and
# `claude-sonnet-4-5-local-q3-8b`), which share no prefix. Empty disables.
SLIM_TOOLS_MATCH = [
    m.strip() for m in os.environ.get("GATEWAY_SLIM_TOOLS_MODELS", "local-").split(",")
    if m.strip()
]

# The tools a coding agent genuinely cannot work without. This exact set was
# confirmed working end to end against ollama_chat/qwen3:8b before being made
# the default. Keep it SMALL: every name added here is subtracted from the
# context left for the conversation, and `Bash` alone is ~2,942 tokens.
#
# NOTE WHAT ELSE GETS DROPPED. `Skill` is not in this set, so a local route
# cannot invoke a skill as a tool at all — which is precisely why the skill
# INJECTION hook above exists and why it runs before this one. Glob and Grep
# also go, so file discovery falls back to Bash.
SLIM_TOOLS_KEEP = {
    t.strip() for t in os.environ.get(
        "GATEWAY_SLIM_TOOLS_KEEP", "Read,Edit,Write,Bash",
    ).split(",") if t.strip()
}


def _slim_tools(data: dict, changes: dict) -> None:
    """Drop tool definitions a small-context local model cannot afford.

    Mutates `data` in place. See SLIM_TOOLS_MATCH for why this is server-side
    and not left to the CLI's `--tools` flag.
    """
    if not SLIM_TOOLS_MATCH or not SLIM_TOOLS_KEEP:
        return

    model = data.get("model") or ""
    if not any(m in model for m in SLIM_TOOLS_MATCH):
        return

    tools = data.get("tools")
    if not isinstance(tools, list) or not tools:
        return

    # Both request shapes reach this hook. /v1/messages carries Anthropic-format
    # tools with a top-level "name"; the OpenAI-format path nests it under
    # "function". Read both, or the filter silently drops EVERY tool on one of
    # them — which looks identical to "the model ignored its tools".
    def _name(t):
        if not isinstance(t, dict):
            return ""
        return t.get("name") or (t.get("function") or {}).get("name") or ""

    kept = [t for t in tools if _name(t) in SLIM_TOOLS_KEEP]

    # Never hand back an empty tools list. An allowlist that matches nothing is
    # a misconfiguration (a renamed tool, a typo in the env var), and stripping
    # every tool would break the agent far more thoroughly than the oversized
    # prompt we are fixing. Leave the request alone and say so in the log.
    if not kept:
        changes["tools_slimmed_skipped"] = {
            "reason": "allowlist matched no tools",
            "saw": sorted({_name(t) for t in tools if _name(t)})[:40],
        }
        return

    if len(kept) == len(tools):
        return

    dropped = sorted({_name(t) for t in tools if _name(t) not in SLIM_TOOLS_KEEP})
    before = len(json.dumps(tools))
    data["tools"] = kept

    # `tool_choice` may name a tool that no longer exists, which is a hard 400
    # on most providers. Fall back to "auto" rather than leaving a dangling ref.
    tc = data.get("tool_choice")
    if isinstance(tc, dict):
        chosen = tc.get("name") or (tc.get("function") or {}).get("name")
        if chosen and chosen not in SLIM_TOOLS_KEEP:
            data["tool_choice"] = {"type": "auto"}
            changes["tool_choice_reset"] = chosen

    changes["tools_slimmed"] = {
        "from": len(tools), "to": len(kept),
        "approx_tokens_saved": (before - len(json.dumps(kept))) // 4,
        "dropped": dropped,
    }


# =============================================================================
# SECTION 8 — AUDIT TRAIL
#
# One append-only file, `capture/modifications.jsonl`, written by section 9
# whenever any of sections 3-7 changed something.
# =============================================================================


def _log_modification(record: dict) -> None:
    """Append one audit line. Records WHAT KIND of secret was found and how
    many — never the value itself, which would defeat the point."""
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        with _lock, (STORE / "modifications.jsonl").open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[modify] audit write failed, continuing: {type(e).__name__}: {e}")


# =============================================================================
# SECTION 9 — REQUEST MODIFICATION PIPELINE
#
# The one place that decides WHICH of sections 3-7 run and IN WHAT ORDER. The
# ordering is load-bearing and each step says why it sits where it does.
# =============================================================================


def _modify(data: dict, call_type: str, key_alias) -> dict:
    """Apply the clamp and the redaction. Returns the same dict, mutated.

    Fails open by design: this runs in front of EVERY request, so an exception
    here would take the whole gateway down. A modification we failed to apply is
    a gap; a gateway that refuses all traffic is an outage.
    """
    changes = {}
    requested_model = data.get("model")

    # --- 1. content-based route override (section 3) -------------------------
    # FIRST, and everything below depends on it: steps 2 and 5 both branch on
    # `data["model"]`, so they have to see the model that will actually serve
    # the request, not the one the picker asked for. Getting this order wrong
    # would exempt an overridden request from the clamp (it would still carry an
    # Anthropic model name) and skip tool slimming on the one route that cannot
    # survive without it.
    _route_override(data, changes)

    # --- 2. max_tokens clamp (section 4) -------------------------------------
    model = data.get("model")
    if CLAMP_MAX_TOKENS > 0 and model not in CLAMP_EXEMPT:
        requested = data.get("max_tokens")
        if isinstance(requested, int) and requested > CLAMP_MAX_TOKENS:
            data["max_tokens"] = CLAMP_MAX_TOKENS
            changes["max_tokens_clamped"] = {"from": requested, "to": CLAMP_MAX_TOKENS}

    # --- 3. secret redaction (section 5) -------------------------------------
    if REDACT and isinstance(data.get("messages"), list):
        counts = {}
        _redact_in_place(data["messages"], counts)
        if counts:
            changes["secrets_redacted"] = counts

    # --- 4. skill injection (section 6) --------------------------------------
    # After redaction, deliberately: redaction walks `messages` only, and this
    # writes to `system`, so the order cannot matter today — but if redaction is
    # ever widened to `system`, injecting first would put our own content through
    # the credential patterns for no reason.
    if INJECT_MODE != "off":
        _inject_skills(data, changes)

    # --- 5. tool slimming (section 7) ----------------------------------------
    # LAST, deliberately. Skill injection above grows `system`, and this shrinks
    # `tools`; running the trim last means the saving it logs is measured
    # against the request we actually forward, not an intermediate one. It also
    # keeps the ordering honest if injection ever learns to mention tools.
    _slim_tools(data, changes)

    if changes:
        _log_modification({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "call_type": call_type,
            # `model` is the route that will serve this request — post-override,
            # so a reader of the audit trail sees the model that answered.
            # `client_model` is what the picker sent, kept alongside it because
            # "which developer asked for what" is the other half of the story.
            "model": model,
            "client_model": requested_model,
            "key_alias": key_alias,
            "changes": changes,
        })

    return data


# =============================================================================
# SECTION 10 — RESPONSE CAPTURE: LOCATING THE EXCHANGE
#
# Everything below runs AFTER the provider answered. This section finds the
# original request inside LiteLLM's callback payload and works out what to call
# the files on disk.
# =============================================================================


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


# =============================================================================
# SECTION 11 — RESPONSE CAPTURE: WHICH MODEL ACTUALLY ANSWERED
#
# The other half of section 3. Once the gateway can rewrite a route, the model
# in the request is a *request*, not a record — so every field below is resolved
# from the RESPONSE, and none of them falls back to what the client asked for.
# =============================================================================


def _model_group(kwargs: dict) -> str | None:
    """The deployment alias LiteLLM routed to — `local-qwen3-8b`, not
    `ollama_chat/qwen3:8b`. This is the value `data["model"]` was rewritten to
    by `_route_override`, which makes it the field that tells us WHETHER an
    override happened, by comparing it against what the client asked for.

    VERIFIED 4 Sep 2026 on litellm 1.98.0 from capture/_kwargs: for a request
    the client sent as `local-qwen3-8b`, `standard_logging_object` carried
    model_group=`local-qwen3-8b` and model=`ollama_chat/qwen3:8b`.
    """
    slo = kwargs.get("standard_logging_object")
    if isinstance(slo, dict) and isinstance(slo.get("model_group"), str):
        return slo["model_group"] or None
    md = (kwargs.get("litellm_params") or {}).get("metadata")
    if isinstance(md, dict) and isinstance(md.get("model_group"), str):
        return md["model_group"] or None
    return None


def _actual_model(kwargs, response_obj, group=None) -> str | None:
    """The model that ACTUALLY produced this response.

    THE WHOLE POINT OF THIS FUNCTION: once the gateway can rewrite the route
    (see `_route_override`), the model in the client's request is a *request*,
    not a record. Logging it would mean every override is invisible in the
    capture, in usage tracking and in cost attribution — the metadata would say
    Haiku answered when qwen3:8b did.

    So the chain below reads the response first and NEVER falls back to the
    client-requested model. If every source is empty we return None and the
    reader sees a gap, which is honest; a plausible-looking wrong model name is
    not. `model_group` is the last resort because it is at least the route that
    was chosen, even on a failed exchange where nothing came back.
    """
    data = _jsonable(response_obj)
    if isinstance(data, dict):
        for key in ("model", "model_name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
    slo = kwargs.get("standard_logging_object")
    candidates = [
        slo.get("model") if isinstance(slo, dict) else None,
        kwargs.get("model"),
        (kwargs.get("litellm_params") or {}).get("model"),
        group,
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _routing_of(kwargs: dict, body, response_obj) -> dict:
    """Reconcile what the client asked for against what served the request.

    `body` is the ORIGINAL client body — `proxy_server_request` is recorded by
    the proxy at ingress, before the pre-call hook runs, so it still carries the
    picker's model even when we rewrote the route. Verified from the captures in
    this repo, where the stored body shows max_tokens 32000 and 36 tools while
    the forwarded request had been clamped and slimmed.

    That is what makes this reconciliation possible without any state shared
    between the two hooks.
    """
    client_model = body.get("model") if isinstance(body, dict) else None
    client_model = client_model if isinstance(client_model, str) else None
    group = _model_group(kwargs)
    actual = _actual_model(kwargs, response_obj, group)
    return {
        # What the developer picked.
        "client_model": client_model,
        # The route that served it, and the model behind that route.
        "model_group": group,
        "model": actual,
        # True only when we can see both sides and they disagree.
        "overridden": bool(client_model and group and group != client_model),
    }


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


# =============================================================================
# SECTION 12 — RESPONSE CAPTURE: WRITING IT TO DISK
#
# The raw-kwargs dump (the point of the whole spike) and the per-exchange
# request/response/index write. Both fail open — a capture failure must never
# break a developer's session.
# =============================================================================


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

        # Who actually answered. Resolved from the response, never from the
        # request — see `_actual_model` for why that distinction is the whole
        # point once the gateway can rewrite a route.
        routing = _routing_of(kwargs, body, response_obj)

        d = STORE / session
        d.mkdir(parents=True, exist_ok=True)
        seq = _next_seq(d)
        name = f"{seq}.{_label(body, response_obj, ok)}"

        _write_json(d / f"{name}.request.json", {
            "body": body,
            "headers": headers,
            # `body` above is the client's original request, so on an overridden
            # exchange its `model` is NOT the model that served it. This block
            # is where the reconciliation lives; read it before believing
            # `body["model"]`.
            "routing": routing,
            "incoming_source": incoming.get("_source", "proxy_server_request"),
            # LiteLLM's own normalised view, for comparison against `body`.
            # If `body` is missing but this is present, LiteLLM is only giving
            # us its interpretation of the request — a finding worth recording.
            "litellm_messages": _jsonable(kwargs.get("messages")),
            "litellm_optional_params": _jsonable(kwargs.get("optional_params")),
        })

        _write_json(d / f"{name}.response.json", {
            "ok": ok,
            "model": routing["model"],
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
                # THE MODEL THAT ANSWERED, not the one that was asked for.
                # Everything downstream — usage tracking, cost attribution,
                # analytics — reads this field, so an override that left it
                # saying `claude-haiku-4-5` would misreport every routed
                # request. `client_model` keeps the developer's selection.
                "model": routing["model"],
                "client_model": routing["client_model"],
                "model_group": routing["model_group"],
                "model_overridden": routing["overridden"],
                "route": (incoming.get("url") if isinstance(incoming, dict) else None),
                "stream": bool((kwargs.get("optional_params") or {}).get("stream"))
                          if isinstance(kwargs.get("optional_params"), dict) else None,
                "usage": _usage_of(response_obj),
                "has_raw_body": body is not None,
                "cc_headers_present": [h for h in CC_HEADERS if h in headers],
            }, default=str) + "\n")

    except Exception as e:  # fail open, always
        print(f"[capture] write failed, continuing: {type(e).__name__}: {e}")


# =============================================================================
# SECTION 13 — CALLBACK REGISTRATION
#
# The only part LiteLLM knows about. `handler` at the bottom is what
# config.yaml's `callbacks: custom_capture.handler` resolves to.
# =============================================================================


class Capture(CustomLogger):
    # ---- BEFORE the provider is called: modify the request ----------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Fires on /v1/messages as call_type="anthropic_messages" — verified on
        litellm 1.98.0. Returning a dict replaces the body that gets forwarded;
        returning None leaves it untouched."""
        try:
            return _modify(
                data,
                call_type=call_type,
                key_alias=getattr(user_api_key_dict, "key_alias", None),
            )
        except Exception as e:
            # Fail open — never block traffic because a modification failed.
            print(f"[modify] pre-call hook failed, forwarding unmodified: "
                  f"{type(e).__name__}: {e}")
            return data

    # ---- AFTER the exchange: record it -----------------------------------
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
