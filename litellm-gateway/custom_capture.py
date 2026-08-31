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
`async_pre_call_hook`. Three behaviours, each individually switchable and all
fail-open:

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
"""

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

# ---------------------------------------------------------------------------
# Request modification (action item 03)
# ---------------------------------------------------------------------------

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

# Skill injection.
#
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

# Guard against pathological nesting in a hand-crafted body.
_MAX_WALK_DEPTH = 12

_lock = threading.Lock()
_dumped = 0
_skills = None  # lazily loaded injectable skills; see _load_skills()

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


# ---------------------------------------------------------------------------
# Request modification helpers (action item 03)
# ---------------------------------------------------------------------------


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


def _scan_text(messages, budget: int) -> str:
    """Concatenate message text, newest first, up to `budget` characters.

    Newest first because that is where the signal is: the file the developer just
    opened, the question they just asked. Bounding it keeps the cost of the
    trigger check flat as a session grows.
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


def _log_modification(record: dict) -> None:
    """Append one audit line. Records WHAT KIND of secret was found and how
    many — never the value itself, which would defeat the point."""
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        with _lock, (STORE / "modifications.jsonl").open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        print(f"[modify] audit write failed, continuing: {type(e).__name__}: {e}")


def _modify(data: dict, call_type: str, key_alias) -> dict:
    """Apply the clamp and the redaction. Returns the same dict, mutated.

    Fails open by design: this runs in front of EVERY request, so an exception
    here would take the whole gateway down. A modification we failed to apply is
    a gap; a gateway that refuses all traffic is an outage.
    """
    changes = {}

    # --- 1. max_tokens clamp -----------------------------------------------
    model = data.get("model")
    if CLAMP_MAX_TOKENS > 0 and model not in CLAMP_EXEMPT:
        requested = data.get("max_tokens")
        if isinstance(requested, int) and requested > CLAMP_MAX_TOKENS:
            data["max_tokens"] = CLAMP_MAX_TOKENS
            changes["max_tokens_clamped"] = {"from": requested, "to": CLAMP_MAX_TOKENS}

    # --- 2. secret redaction -----------------------------------------------
    if REDACT and isinstance(data.get("messages"), list):
        counts = {}
        _redact_in_place(data["messages"], counts)
        if counts:
            changes["secrets_redacted"] = counts

    # --- 3. skill injection -------------------------------------------------
    # After redaction, deliberately: redaction walks `messages` only, and this
    # writes to `system`, so the order cannot matter today — but if redaction is
    # ever widened to `system`, injecting first would put our own content through
    # the credential patterns for no reason.
    if INJECT_MODE != "off":
        _inject_skills(data, changes)

    if changes:
        _log_modification({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "call_type": call_type,
            "model": model,
            "key_alias": key_alias,
            "changes": changes,
        })

    return data


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
