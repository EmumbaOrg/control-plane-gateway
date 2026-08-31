# Modifying requests in flight — action item 03

**Status: done. Verified 31 Aug 2026** against the running stack
(`ghcr.io/berriai/litellm:main-latest`, litellm `1.98.0`).

The gateway no longer only *records* traffic — it can change it on the way past.
Three behaviours ship, each individually switchable, all fail-open.

---

## The mechanism, and why it was uncertain

LiteLLM's `CustomLogger` exposes a pre-call hook:

```python
async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type)
    -> Exception | str | dict | None
```

Return a `dict` and it **replaces the body that gets forwarded**. Our
`custom_capture.py` already subclasses `CustomLogger` and is already registered
via `litellm_settings.callbacks`, so no new wiring was needed — item 03 is one
method on an object the proxy was already loading.

**The real question was whether the hook fires on the route Claude Code uses.**
That route (`/v1/messages`) behaves differently from the others — it is the reason
the `anthropic-beta` gap in `FINDINGS.md` exists — so it was plausible the hook
was skipped there entirely.

It is not. Read from the installed source in the container:

- `proxy/anthropic_endpoints/endpoints.py:101` dispatches through
  `ProxyBaseLLMRequestProcessing.base_process_llm_request(route_type="anthropic_messages")`
- `proxy/common_request_processing.py:1518` awaits
  `proxy_logging_obj.pre_call_hook(..., call_type=route_type)` and **assigns the
  result back to `self.data`**

So the hook fires, with `call_type="anthropic_messages"`, and its return value is
what gets forwarded.

> **This is unaffected by the `anthropic-beta` gap.** That gap is about outbound
> *headers* not being populated on this route. The request *body* is passed in and
> returnable, which is all body modification needs. Two different code paths.

---

## Behaviour 1 — `max_tokens` clamp

**On by default**, ceiling 8000.

### Why

Claude Code reserves **32000 output tokens** on every request, and providers gate
on the *reservation*, not on what is produced. A reply using 60 tokens is refused
because 32000 could not be afforded. The CLI can be fixed with
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`; **the desktop app exposes no equivalent**, so the
only place to fix it for every client at once is server-side.

### Verified, both directions

Same request twice — `gemini-3.7-flash`, `max_tokens: 32000`:

| Clamp | Result |
|---|---|
| **Disabled** (`GATEWAY_MAX_OUTPUT_TOKENS=0`) | `402 — "You requested up to 32000 tokens, but can only afford 8199"` |
| **Enabled** (ceiling 8000) | Succeeded. Reply: `OK` |

Audit line from the passing run:

```json
{"call_type":"anthropic_messages","model":"gemini-3.7-flash",
 "changes":{"max_tokens_clamped":{"from":32000,"to":8000}}}
```

### The exempt list, and the trap in it

Anthropic honours 32000 happily, so clamping there would truncate long answers for
no reason. The four real Anthropic aliases are exempt.

**The exempt list matches exactly, never by prefix — this is load-bearing.**
`claude-haiku-4-5-gmn-37-flash` is a *picker alias for Gemini* (see
`NON-ANTHROPIC-MODELS.md`) and starts with `claude-haiku-4-5`. Prefix matching
would silently exempt every translated route and defeat the clamp on exactly the
models that need it. Covered by a test case.

---

## Behaviour 2 — outbound secret redaction

**Off by default.** It rewrites developer content, so switching it on should be a
deliberate decision with a named owner, not a default someone inherits.

```
GATEWAY_REDACT=on
```

### Verified

A request was sent through the gateway containing a well-known example AWS key.
The model's own reply is the proof:

> *"I can't repeat back redacted credentials…"*

It never saw the key. Confirmed against the capture file for that exchange:

| Check | Result |
|---|---|
| Real key present in what reached the provider | **False** |
| `[REDACTED:aws-access-key-id]` present instead | **True** |
| `max_tokens` altered on this Anthropic call | No — 32000, correctly exempt |

### What is matched

High-specificity patterns only — a vendor-prefixed token or a PEM header, never
"looks like entropy". A false positive silently corrupts a developer's prompt and
they have no way to see it happened.

| Kind | Shape |
|---|---|
| `anthropic-api-key` | `sk-ant-…` |
| `openai-api-key` | `sk-proj-…` |
| `aws-access-key-id` | `AKIA` + 16 |
| `github-token` | `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` |
| `slack-token` | `xoxb-` / `xoxp-` / … |
| `google-api-key` | `AIza` + 35 |
| `private-key-block` | `-----BEGIN … PRIVATE KEY-----` through its `END` |

### Two design decisions worth knowing

**`messages` only — never `system` or `tools`.** Those are the cacheable prefix
and are our own content, not developer input, so rewriting them buys nothing and
risks the prompt-caching win. `tool_result` blocks *are* walked, and they are the
point: they carry the file contents and command output fed back to the model,
which is where a credential committed to a repo actually surfaces.

**Replacements are deterministic** — no counters, no timestamps in the
placeholder. Claude Code resends the whole conversation every turn, so a
placeholder that varied between turns would change the cached prefix and defeat
prompt caching: a silent ~10x input-cost increase, which is what fidelity checks
5 and 6 exist to catch. Covered by a test case.

---

## Behaviour 3 — skill injection

**Mode `installed` by default.** Added 31 Aug 2026.

### Why: distribution is not activation

The Skills marketplace distributes a plugin to every developer through the
gateway, deterministically. What it cannot do is make the model *use* it.

Measured on `claude-sonnet-4-5-nvda-super-120b-free` (Nemotron 3 Super 120B via
OpenRouter), asked to analyse a React component with a textbook three-`fetch`
waterfall in a `useEffect`:

| Check | Result |
| --- | --- |
| Plugin installed via the gateway marketplace | yes — `emumba-react@litellm`, enabled |
| Skill catalogue present in the request | yes — in all 8 real requests |
| `Skill` tool present in the `tools` array | yes — 60 tools, `Skill` among them |
| Model emitted a `Skill` tool_use | **no — zero, across every response** |
| `skillUsage` counter moved | **no** |

The model answered from its own reasoning, never named the waterfall, and when
asked which skill it had used, named `code-review` — a skill it knew about
generally — rather than the matching one sitting in its own prompt.

**A skill delivered as a tool the model may choose is a capability the gateway
cannot guarantee.** The weaker the routed model, the wider that gap. Injecting
the skill body into `system` removes the model's choice from the path: the
guidance is present as context, so it applies on any model that can read its
prompt.

### Three modes, because consent is a policy question

Injection rewrites developer prompts. Whether it should reach someone who never
asked for it is not a technical decision, so it is a setting rather than an
assumption:

| `GATEWAY_INJECT_SKILLS` | Behaviour |
| --- | --- |
| `off` | No injection. |
| `installed` **(default)** | Inject only for developers who already have the plugin enabled. |
| `always` | Inject regardless of what the developer has installed. |

**`installed` is the default because it is the mode that matches the diagnosis.**
The measured failure was *installed, offered, never called* — that is an
**activation** problem, and activation is the only part injection needs to fix.
Someone who never installed the plugin does not have that problem, and their
prompt is not ours to rewrite; they may be doing client work under entirely
different standards.

`always` is central enforcement in the strong sense: a developer cannot escape a
standard by disabling the plugin. It is a legitimate thing to want, and it is the
right setting for a control-plane mandate — but it needs a named owner and an
announcement, not a default someone inherits. That is the same reasoning that
keeps `GATEWAY_REDACT` off.

#### How installation is detected

Claude Code advertises the developer's available skills to the model as a
catalogue in a `<system-reminder>` — **in the conversation, not the `system`
block**, which is where you would expect it and where an earlier draft of this
document said it was. Each entry is a markdown list item:

```
- emumba-react:react-best-practices: Comprehensive React and Next.js performance…
```

So the request itself carries the answer; no gateway-side registry of who has
what, and nothing to keep in sync. Detection anchors on the leading `- ` and the
trailing `:`, so a developer *mentioning* the skill by name — "do we have
`emumba-react:react-best-practices`?" — does not read as having it installed.
Covered by a test case.

The consequence worth stating: this is a **client-supplied** signal. In `always`
mode that does not matter, but in `installed` mode a client could in principle
suppress injection by not sending the catalogue. For an activation aid that is
fine — someone who does not want the guidance was never the target. Do not
mistake `installed` for an enforcement control.

### Verified, both model families, same request

The component is `plugins/emumba-react/example/OrdersDashboard.tsx`.

| Model | Before injection | After injection |
| --- | --- | --- |
| `claude-sonnet-4-5-nvda-super-120b-free` | Generic advice — "add error handling", "use `Link`", "consider memoization". **Waterfall not mentioned.** | Opens on waterfalls, gives the `Promise.all()` rewrite, then works through barrel imports, SWR dedup and `useMemo` — the skill's categories, in its order |
| `claude-haiku-4-5` (real Anthropic) | n/a | Names the skill explicitly and labels each finding with the rule it applied |

Audit lines from the two passing runs:

```json
{"model":"claude-sonnet-4-5-nvda-super-120b-free","key_alias":"asif-desktop-app",
 "changes":{"skills_injected":{"emumba-react:react-best-practices":6885}}}
{"model":"claude-haiku-4-5","key_alias":"asif-desktop-app",
 "changes":{"skills_injected":{"emumba-react:react-best-practices":6885}}}
```

### The manifest — one source of truth, two delivery paths

`skills-inject.json` lists what to inject and what makes each entry relevant:

```json
{"skills": [{
  "name": "emumba-react:react-best-practices",
  "path": "/app/skills/react-best-practices/SKILL.md",
  "trigger": "\\buse(?:Effect|State|Memo|…)\\s*\\(|\\.[jt]sx\\b|\\breact\\b|…"
}]}
```

`path` points at **the same `SKILL.md` the marketplace distributes**, mounted
read-only from `../plugins/emumba-react/skills`. Not a copy — a copy would drift,
and the two delivery paths would then teach different things.

A manifest rather than hardcoded paths so adding the second skill is a data
change, not a code change. Frontmatter is stripped before injection: it is Claude
Code's *registration* metadata, and injected as context it reads as an
instruction to load a skill that is already present.

The trigger is **deliberately generous** — a false positive costs a few thousand
tokens of irrelevant guidance, a false negative costs the entire point.

### Injection is appended, never inserted — this is load-bearing

Claude Code puts its `cache_control` breakpoint on the last `system` block it
sends. Appending **after** that breakpoint leaves the cached prefix
byte-identical, so prompt caching is unaffected; the injected block simply sits
in the uncached suffix. Inserting earlier — or rewriting an existing block —
would invalidate the cache every turn, which is the ~10x input-cost failure that
fidelity checks 5 and 6 exist to catch. The block also carries no `cache_control`
of its own, since adding a breakpoint would change how the whole request caches.
Covered by a test case.

Injection is **idempotent**: the block opens with `<!-- emumba-gateway-skill:`
and a request already carrying that marker is left alone. Without it, a retry or
a second gateway in the path would append the skill again on every pass and grow
the prompt without bound. Also covered by a test case.

### What this costs, stated plainly

- **~1,800 tokens of input on every in-scope request**, uncached, per skill. On a
  translated route prompt caching is gone anyway (see `NON-ANTHROPIC-MODELS.md`),
  so it is paid in full there.
- **Developers are not told their prompt was modified.** Someone gets React
  performance guidance they did not ask for and cannot see why. This is the same
  open concern redaction has, and injection makes it more visible rather than
  less — which is arguably an improvement, since the injected block says who
  attached it.
- **It applies to Anthropic models too, where native activation already works.**
  That is deliberate: a guarantee that holds only on some routes is not a
  guarantee. `GATEWAY_INJECT_SKILL_MODELS` narrows it if you want the tokens
  back.

---

## The audit trail

Every modification appends one line to `capture/modifications.jsonl`:

```json
{"ts":"…","call_type":"anthropic_messages","model":"claude-haiku-4-5",
 "key_alias":"asif-desktop-app","changes":{"secrets_redacted":{"aws-access-key-id":1}}}
```

It records **the kind of secret and how many** — attributed to a named key — and
never the value. Logging the value would defeat the purpose: the point is to keep
the credential out of storage, and the audit file is storage.

---

## Fail-open, deliberately

The hook runs in front of **every** request. An exception here would take the
whole gateway down, so both behaviours are wrapped and fall through to the
unmodified body on any error:

```python
except Exception as e:
    print(f"[modify] pre-call hook failed, forwarding unmodified: …")
    return data
```

**A modification we failed to apply is a gap. A gateway that refuses all traffic
is an outage.** That trade is the right way round for a PoC — but note the
consequence honestly: **redaction is best-effort, not a guarantee.** It must not
be presented as a control that cannot fail, and it is not a substitute for
developers not pasting credentials in the first place.

---

## How to check this yourself

Three levels, cheapest first.

### 1. The logic — offline, no key, no spend

```bash
python3 verify_modify.py
```

16 checks over `_modify()` with Claude-Code-shaped bodies. Exit code 0 or 2, so it
can gate a commit. It forces its own configuration, so the result does not depend
on what is currently exported in your shell. The injection checks load the **real**
manifest and the **real** `SKILL.md`, with the container paths rewritten, so they
test the shipped content rather than a fixture.

It covers the three things most likely to be got wrong: that a **picker alias is
still clamped** (the prefix trap), that redaction is **deterministic**, and that
injection is **idempotent and appended after the cache breakpoint** — the two
ways it could silently destroy prompt caching or grow a prompt without bound. It
does *not* prove the proxy calls the hook — that is level 2.

### 2. The hook is loaded in the running container

```bash
docker compose exec -T litellm python -c "import sys; sys.path.insert(0,'/app'); import custom_capture as cc; print('clamp =', cc.CLAMP_MAX_TOKENS, '| redact =', cc.REDACT, '| hook =', hasattr(cc.handler,'async_pre_call_hook'))"
```

Expect `clamp = 8000 | redact = False | hook = True` on defaults. The injector
logs its own state at first use — `[inject] loaded skill '…' (N chars)` in
`docker compose logs litellm`, or the explicit check under level 3.

### 3. End to end, through the gateway

**The clamp.** Ask for 32000 on a translated route:

```bash
curl -s http://localhost:4000/v1/messages -H "Authorization: Bearer $(cat .vkey)" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" -d '{"model":"gemini-3.7-flash","max_tokens":32000,"messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

Then read what the gateway recorded doing to it:

```bash
tail -1 capture/modifications.jsonl
```

You want `{"max_tokens_clamped":{"from":32000,"to":8000}}`.

**The counterfactual — proves the clamp is what made it work.** Turn it off:

```bash
set -a && . ./.env && set +a && GATEWAY_MAX_OUTPUT_TOKENS=0 docker compose up -d --force-recreate litellm
```

Re-run the same curl: it now returns `402 — "You requested up to 32000 tokens,
but can only afford …"`. Then restore the default:

```bash
set -a && . ./.env && set +a && docker compose up -d --force-recreate litellm
```

**Redaction.** It is off by default, so switch it on for the test:

```bash
set -a && . ./.env && set +a && GATEWAY_REDACT=on docker compose up -d --force-recreate litellm
```

Send a credential-shaped string and ask the model to echo it:

```bash
curl -s http://localhost:4000/v1/messages -H "Authorization: Bearer $(cat .vkey)" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" -d '{"model":"claude-haiku-4-5","max_tokens":256,"messages":[{"role":"user","content":"Repeat back exactly the credential you see here, then stop: AKIAIOSFODNN7EXAMPLE"}]}'
```

**The model's own reply is the proof** — it says it cannot repeat back a *redacted*
credential, because `[REDACTED:aws-access-key-id]` is all it received. Confirm
against what was actually captured:

```bash
grep -rl "AKIAIOSFODNN7EXAMPLE" capture/ ; echo "exit $? — non-zero means the key is nowhere in the capture store"
```

Then put it back to the default:

```bash
set -a && . ./.env && set +a && docker compose up -d --force-recreate litellm
```

**Skill injection.** First confirm the mode and the manifest:

```bash
docker compose exec -T litellm python -c "import sys; sys.path.insert(0,'/app'); import custom_capture as cc; print('mode =', cc.INJECT_MODE, '| skills =', [s['name'] for s in cc._load_skills()])"
```

Expect `mode = installed` on defaults. **A bare `curl` carries no skill
catalogue, so in the default mode it will correctly inject nothing** — that is
the check passing, not failing. To exercise injection from `curl`, switch to
`always` for the test:

```bash
set -a && . ./.env && set +a && GATEWAY_INJECT_SKILLS=always docker compose up -d --force-recreate litellm
```

Then send an in-scope question to the cheapest free route and read the answer:

```bash
curl -s http://localhost:4000/v1/messages -H "Authorization: Bearer $(cat .vkey)" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" -d '{"model":"claude-sonnet-4-5-nvda-super-120b-free","max_tokens":800,"messages":[{"role":"user","content":"Review this: useEffect(() => { const a = await fetch(1); const b = await fetch(2) }, [])"}]}'
```

The signal is not that the answer is good — it is that the answer **leads with
the waterfall and reaches for `Promise.all()`**, which is the skill's first
critical rule. Then confirm the gateway says it did it:

```bash
tail -1 capture/modifications.jsonl
```

You want `{"skills_injected":{"emumba-react:react-best-practices":6885},
"inject_mode":"always"}`.

**The counterfactual.** Turn it off and ask the identical question:

```bash
set -a && . ./.env && set +a && GATEWAY_INJECT_SKILLS=off docker compose up -d --force-recreate litellm
```

The same model gives generic advice and does not name the waterfall. That
difference is the whole feature. **Restore the default afterwards** — leaving the
gateway on `always` silently enforces the standard on everyone:

```bash
set -a && . ./.env && set +a && docker compose up -d --force-recreate litellm
```

To test the default mode honestly you need a real Claude Code session rather than
`curl`, since the catalogue only exists there. `python3 check_skill_usage.py` is
the companion check: in `installed` mode the skill's guidance now arrives as
context, so expect the answer to improve **without** the `usageCount` moving —
the counter tracks `Skill` tool calls, and injection deliberately bypasses those.

> Each restart takes ~15 seconds before the proxy answers. `curl
> localhost:4000/health/liveliness` returning `200` is the signal it is ready.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `GATEWAY_MAX_OUTPUT_TOKENS` | `8000` | Output ceiling. `0` disables the clamp |
| `GATEWAY_CLAMP_EXEMPT_MODELS` | the 4 Anthropic aliases | Exact matches, comma-separated |
| `GATEWAY_REDACT` | `off` | `on` enables secret redaction |
| `GATEWAY_INJECT_SKILLS` | `installed` | `off`, `installed` (opt-in only) or `always` (enforced) |
| `GATEWAY_SKILL_MANIFEST` | `/app/skills-inject.json` | What to inject, and its triggers |
| `GATEWAY_INJECT_SKILL_MODELS` | empty (= all models) | Exact matches, comma-separated |
| `GATEWAY_INJECT_MAX_CHARS` | `20000` | Per-skill ceiling; longer bodies are truncated |

Wired in `docker-compose.yml`; change and `docker compose up -d --force-recreate litellm`.

---

## What this closes, and what it opens

**Closes:** action item 03, and the outstanding desktop-app `402` from
`NON-ANTHROPIC-MODELS.md` ("Still unsolved on desktop") — one hook covered both.

**Opens, for a decision:**

- **Is redaction switched on, and who owns the pattern list?** Adding a pattern is
  a code change today. A wrong pattern corrupts prompts silently.
- **Developers are not told when their prompt was modified.** The audit line goes
  to the gateway, not to the session. Someone whose credential was redacted sees
  only that the model behaved oddly. Consider whether that is acceptable.
- **Which skills are mandatory, and who owns the trigger regexes?** Adding a
  skill is now a manifest change rather than a code change, but a trigger that is
  too broad taxes every request and one that is too narrow silently does nothing.
  Nobody owns that list yet.
- **Skill injection applies to Anthropic routes as well as translated ones.**
  Uniform by design, duplicated effort in practice on models that would have
  activated the skill natively. Revisit if the token cost shows up in spend.
- **Does anyone want `always`?** The default (`installed`) helps developers who
  opted in and leaves everyone else alone. Turning a skill into a genuine
  organisational mandate means `always`, which rewrites the prompts of people who
  did not opt in — a decision for whoever owns engineering standards, not for
  whoever runs the gateway.
- **Prompt caching under redaction is untested on a long real session.** The
  replacement is deterministic so it *should* hold; confirm with a real
  `cache_read_input_tokens` reading before trusting it.
