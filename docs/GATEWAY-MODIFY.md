# Modifying requests in flight — action item 03

**Status: done. Verified 31 Aug 2026** against the running stack
(`ghcr.io/berriai/litellm:main-latest`, litellm `1.98.0`).

The gateway no longer only *records* traffic — it can change it on the way past.
Four behaviours ship, each individually switchable, all fail-open.

**Behaviour 0 was added 4 Sep 2026** and is different in kind from the other
three: it changes *which model answers*, not just what the model is sent. That
forced a second change, to the capture side — see
[Attribution](#attribution-the-captured-model-must-be-the-model-that-answered).

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

## Behaviour 0 — content-based route override

**On by default**, one rule: a conversation that mentions **Extreme Networks** is
served by the private local model (`local-qwen3-8b` → `ollama_chat/qwen3:8b`),
whatever the developer selected in the picker.

### Why

Client material that must not leave the building is a policy, and "remember to
switch your model first" is a control that fails the first time somebody is in a
hurry. Moving the decision into the gateway makes it the default path instead of
a habit: the developer keeps working with the model they picked, and the request
for that client goes to on-prem inference without them doing anything.

### Verified end to end, 4 Sep 2026

Against the running stack, with `local-qwen3-8b` served by Ollama on the host.
Same key, same route (`POST /v1/messages`), same selected model — the only
difference is one phrase in the message.

```bash
# routed — the phrase is present
curl -s http://localhost:4000/v1/messages \
  -H "x-api-key: $(cat .vkey)" -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -H "x-claude-code-session-id: live-routing" \
  -d '{"model":"claude-haiku-4-5","max_tokens":64,
       "messages":[{"role":"user",
         "content":"Hi I am Asif from Extreme Networks. Reply with exactly: ack"}]}'
```

`capture/modifications.jsonl`:

```json
{"ts": "2026-09-04T10:56:53", "call_type": "anthropic_messages",
 "model": "local-qwen3-8b", "client_model": "claude-haiku-4-5",
 "key_alias": "plugin-skills-testing",
 "changes": {"model_overridden": {"from": "claude-haiku-4-5",
                                  "to": "local-qwen3-8b",
                                  "rule": "extreme-networks-on-prem",
                                  "matched": "extreme network"}}}
```

`capture/index.jsonl`, the two runs side by side:

| | selected | served by | captured `model` | `model_overridden` |
|---|---|---|---|---|
| phrase present | `claude-haiku-4-5` | `local-qwen3-8b` | `ollama_chat/qwen3:8b` | `true` |
| phrase absent | `claude-haiku-4-5` | `claude-haiku-4-5` | `claude-haiku-4-5-20251001` | `false` |

So LiteLLM's router **does** honour a `model` rewritten inside the pre-call hook —
the open question from the sections below, now closed for this route.

> The captured Anthropic model is the *dated* id (`…-20251001`) because that is
> what Anthropic reports back. The alias is what was asked for; the dated id is
> what answered. Recording the second is the point.

### What is matched — widely, on purpose

**Widened 7 Sep 2026.** The rule carries one keyword, `extreme network`, and it
is matched as a **case-insensitive substring with an optional separator** — the
words are joined by `[\s\-_]*` and there are **no word boundaries**. One keyword
therefore covers every spelling a developer might actually type:

| Text | Matches |
|---|---|
| `Extreme Networks`, `extreme networks`, `EXTREME NETWORKS` | ✅ plural, any case |
| `extreme network` | ✅ singular |
| `extreme-network`, `extreme_networks` | ✅ any separator |
| `extremenetworks`, `extremenetwork` | ✅ no separator |
| `extreme\nnetworks` | ✅ wrapped across a line |
| `extremenetworksmigration`, `extremenetworkstaging` | ✅ inside a longer word — branch names |

**This over-matches, and that is the accepted trade.** `we are seeing extreme
network latency in staging` now routes to the local 8B model. The earlier version
of this rule was anchored on word boundaries and the plural specifically to avoid
that, and the decision was reversed: a rule about *where a client's data may be
processed* should fail toward keeping data in, not toward letting it out. The
false positive costs a developer one degraded answer, which they can see in the
audit trail; the false negative sends client material to a hosted provider, which
nobody sees. `verify_routing.py` **asserts** the over-match rather than tolerating
it, so narrowing the pattern fails the scorecard and forces the policy decision to
be revisited deliberately.

⚠ **The consequence for anyone editing a rule.** Without word boundaries, a short
or common keyword is dangerous in a way it was not before — `keywords: ["net"]`
would reroute most of the working day. Keep every keyword a distinctive
multi-word name.

Two words are still required, adjacent: `extreme-throughput`, `the network switch
in rack 4`, and `extreme caution on the network migration` are all left alone.

Both the conversation **and the `system` block** are scanned. A repo `CLAUDE.md`
saying "this project is for <client>" arrives in `system`, and that is the
likeliest place for the fact to be written down once rather than retyped.

### It runs first, and that ordering is load-bearing

The clamp (behaviour 1) exempts models by name and the tool slimmer (behaviour 4)
selects routes by name, so both must see the *post-override* model. Run the
override last instead and an overridden request keeps its Anthropic exemption
(32000 output tokens against a route that holds 8192) and skips slimming — on a
32,768-token local model that means Ollama **truncates the prompt in silence**,
returns `200`, and the model answers as if the end of its own instructions did
not exist. `verify_routing.py` check 7 exists to catch exactly that regression.

Measured on the overridden path: 32000 → 8000 output tokens, tool definitions
36 → 4.

### What this costs, stated plainly

1. **Capability.** `qwen3:8b` is not Haiku. The developer asked for one model and
   got another, which is why the swap must be visible in the record rather than
   quietly absorbed.
2. **Context.** The client believes it is talking to a 200k-window model and
   builds prompts to match; the local route holds 32,768. Slimming buys back
   ~14.5k tokens, which is enough for ordinary turns and **not** enough for a
   long session. There is no fix for this inside the gateway — the client sets
   its context ceiling from the model it selected, and it selected Haiku.
3. **No memory.** The decision is taken per request from the request's own text.
   The only state is the history Claude Code resends each turn, so if the trigger
   phrase ever falls outside `GATEWAY_ROUTE_SCAN_CHARS`, the route flips back to
   the hosted model mid-conversation. The budget defaults to 2,000,000 characters
   — an order of magnitude above the injector's — precisely because a silent flip
   is worse than the scan cost. **It is a mitigation, not a guarantee.**
4. **The client is not told.** See the note under Attribution below.

---

## Attribution: the captured model must be the model that answered

Once the gateway can rewrite a route, **the model in the client's request is a
request, not a record.** Logging it would make every override invisible in the
capture, in usage tracking and in cost attribution — the metadata would say Haiku
answered when `qwen3:8b` did, and the spend would be attributed to the wrong
model.

So `custom_capture.py` resolves the model from **what came back**, never from what
was asked for (`_actual_model`), in this order:

1. `response_obj["model"]` — the provider's own statement
2. `standard_logging_object["model"]` — LiteLLM's view (`ollama_chat/qwen3:8b`)
3. `kwargs["model"]`, then `litellm_params["model"]`
4. `standard_logging_object["model_group"]` — the route, as a last resort

If all of those are empty the field is left **null**. That is deliberate: a gap is
honest, and the one fallback never used is the client's requested model, because
that is the exact misattribution this change exists to prevent. It comes up on
failed exchanges, where nothing came back — check 15 covers it.

The client's selection is not thrown away; it moves to its own field. Every
record now carries both:

| Field | Meaning | Written to |
|---|---|---|
| `model` | the model that **answered** | `index.jsonl`, `*.response.json`, `modifications.jsonl` |
| `client_model` | what the developer **picked** | `index.jsonl`, `modifications.jsonl` |
| `model_group` | the LiteLLM route that served it | `index.jsonl` |
| `model_overridden` | `client_model` and `model_group` disagree | `index.jsonl` |
| `routing` | all four, next to the untouched original body | `*.request.json` |

The per-exchange `*.request.json` needs that `routing` block because its `body` is
the client's **original** request — `proxy_server_request` is recorded by the
proxy at ingress, before the pre-call hook runs. That is a useful property (it is
also how the two hooks reconcile without sharing any state), but it means someone
reading a capture would otherwise see `claude-haiku-4-5` and no hint that qwen
answered.

### In the LiteLLM dashboard

This is where anyone will actually look, so it is worth being precise about which
half was free and which half needed code.

**Free.** `LiteLLM_SpendLogs.model` and `.model_group` are written from the
router, and the router ran *after* the override — so a rerouted request is
logged, attributed and **priced** as the local model with no help from us.
Verified 4 Sep 2026 by reading the table and the `/spend/logs` endpoint the UI's
Logs page calls:

| `model` | `model_group` | `custom_llm_provider` | `spend` |
|---|---|---|---|
| `ollama_chat/qwen3:8b` | `local-qwen3-8b` | `ollama_chat` | `2.8e-06` |
| `anthropic/claude-haiku-4-5` | `claude-haiku-4-5` | `anthropic` | `3.8e-05` |

Both rows came from the *same* selected model, `claude-haiku-4-5`; the only
difference was one phrase in the message. Note the cost too — the routed request
is billed at the local route's rate, not Anthropic's, so spend reporting is right
for the same reason the model name is.

**Not free.** The row above names what answered but not what was *asked for*, so
a forced route and a developer's own deliberate local selection look identical in
the UI. `_annotate_spend_log()` adds that half, through two fields LiteLLM
already supports on the request's metadata dict:

- `spend_logs_metadata` → `SpendLogs.metadata`, shown in the log's detail view:
  ```json
  {"client_model": "claude-haiku-4-5", "served_by": "local-qwen3-8b",
   "route_rule": "extreme-networks-on-prem", "matched_keyword": "extreme network"}
  ```
- `tags` → `SpendLogs.request_tags`, which the Logs page can **filter** on:
  `gateway-routed:extreme-networks-on-prem`, `client-model:claude-haiku-4-5`.
  So "show me every request that was rerouted, and what each developer had
  selected" is a UI query rather than a grep over the capture directory.

Two details that would fail silently if got wrong, and are covered by checks
20–23: the metadata dict is reached under the key the route uses (`metadata` on
most, `litellm_metadata` on LiteLLM's `LITELLM_METADATA_ROUTES`) — it is already
on `data` because `add_litellm_data_to_request` is awaited at
`common_request_processing.py:1370`, before the pre-call hook at `:1518` — and
tags are **appended**, because the proxy has already put its own User-Agent tags
in the list.

> **⚠ Known gap: the HTTP response to the client still says `claude-haiku-4-5`.**
> Verified 4 Sep 2026 — LiteLLM's `/v1/messages` translation echoes the requested
> model back in the response body, even though the internal response object
> honestly carries `ollama_chat/qwen3:8b` (which is what the capture records). So
> the *gateway's* records are correct and the *developer's* client is not told
> their request was rerouted. Fixing it means rewriting the response in a
> post-call hook, including the streaming path, which is a larger change with its
> own risk of breaking clients that validate the field. Not attempted. Whether
> developers should be told is the same open question as behaviour 2's silent
> redaction — see the list at the end of this document.

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
what, and nothing to keep in sync. Detection anchors on the leading `- `, so a
developer *mentioning* the skill by name — "do we have
`emumba-react:react-best-practices`?" — does not read as having it installed.
Covered by a test case.

**The trailing `:` is optional, and that is load-bearing — fixed 15 Sep 2026.**
A plugin installed in the *current* session is advertised by **name only** until
its `SKILL.md` metadata is indexed — the "Restart to apply changes" window:

```
- emumba-react:react-best-practices: Comprehensive React and Next.js performance…
- emumba-backend:node-express-service
```

Requiring the colon made those installs invisible, so injection was skipped for
**exactly the developer who had just opted in** — and it failed silently, looking
identical to a trigger that did not match. End-of-line is now accepted too; a
bare name alone on a list line is still specific enough not to fire on prose.

#### The catalogue must not be matched against triggers

This is the other half of the same surface, and getting it wrong shipped a bug —
see [Triggers, and the catalogue trap](#triggers-and-the-catalogue-trap) below.
The installed-check reads the **full** conversation text, catalogue included,
because the catalogue is exactly what it needs. Trigger matching reads the same
text with every `<system-reminder>` block **stripped out**. The two checks
deliberately disagree about what they can see.

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

**Note the shape of that evidence, because it is the only shape that works.** It
is an **A/B on the output** — the same request with injection off and on, judged
on whether the answer cites the skill's specific rules. It is deliberately *not*
the model's own account of what it used.

Asking cannot establish this:

- **`check_skill_usage.py` is blind to injection by construction.** It reads
  Claude Code's per-skill counter, which increments when the model calls the
  `Skill` tool. An injected skill never passes through that tool, so the counter
  correctly stays at **zero** while the standard is in fact applied. It measures
  the marketplace path, and only that.
- **A `SKILL_USED=` line is unreliable in both directions.** Weaker models
  receive injected guidance as ordinary system text and then deny using any
  skill; they will equally claim one they never loaded.
- **Injected blocks now instruct the model to name the skill when asked**, which
  was added to fix the denial half of that problem. It does fix it — and it makes
  the self-report **self-fulfilling for the injection path.** Useful in a demo;
  not independent evidence. The runs above predate that instruction.

### The manifest — one source of truth, two delivery paths

`skills-inject.json` lists what to inject and what makes each entry relevant:

```json
{"skills": [
  {"name": "emumba-react:react-best-practices",
   "path": "/app/skills/emumba-react/react-best-practices/SKILL.md",
   "trigger": "\\buse(?:Effect|State|Memo|…)\\s*\\(|\\.[jt]sx\\b|\\breact\\b|…"},
  {"name": "emumba-backend:node-express-service",
   "path": "/app/skills/emumba-backend/node-express-service/SKILL.md",
   "trigger": "from\\s+['\"]express|\\breq\\.(?:body|params|query)\\b|…"}
]}
```

**Four skills across two plugins** are wired today: `emumba-react`
(`react-best-practices`) and `emumba-backend` (`rest-api-conventions`,
`spring-boot-service`, `node-express-service`).

`path` points at **the same `SKILL.md` the marketplace distributes** — not a
copy, because a copy would drift and the two delivery paths would then teach
different things. Each plugin is mounted read-only under its own directory:

```
../plugins/emumba-react/skills:/app/skills/emumba-react:ro
../plugins/emumba-backend/skills:/app/skills/emumba-backend:ro
```

**One mount per plugin, not one shared mount.** A single flat `:/app/skills` can
only ever serve one plugin: the second such line shadows the first, and the
skills it replaced stop injecting **silently**. Nesting also makes the manifest
path mirror the skill's namespaced name, so a wrong path is visible on sight.

A manifest rather than hardcoded paths, so adding a skill is a data change, not a
code change — adding the fourth required no edit to `custom_capture.py`.
Frontmatter is stripped before injection: it is Claude Code's *registration*
metadata, and injected as context it reads as an instruction to load a skill that
is already present.

<a id="triggers-and-the-catalogue-trap"></a>

#### Triggers, and the catalogue trap

An earlier version of this document said the trigger is *"deliberately generous —
a false positive costs a few thousand tokens of irrelevant guidance, a false
negative costs the entire point."* **That reasoning is what let a real bug hide
for two weeks, and it is retracted.** A false positive is not a few thousand
tokens of rounding error; at four skills it was measured at **17,683
characters**, and the cost scales with the catalogue while the benefit does not.

The mechanism: Claude Code advertises installed skills in a `<system-reminder>`,
one line each **including the description** — and a description contains the very
words its own trigger matches.

```
- emumba-backend:spring-boot-service: Spring Boot service structure for Emumba…
                                      ^^^^^^^^^^^ matches \bspring\s*boot\b
```

With the catalogue in scope, **every installed skill injected into every request
regardless of topic.** Measured 15 Sep 2026 on a pure Express file: it pulled in
React *and* Spring Boot alongside the two that belonged.

**The bug was undetectable while one skill was configured.** A lone skill
re-triggering off its own catalogue entry is indistinguishable from working, and
`verify_modify.py` was green throughout. It surfaced only at four.

Fixed by stripping `<system-reminder>` blocks before trigger matching, while the
installed-check keeps the full text. Covered by a regression check that was
confirmed to fail against the original code.

**So: keep each trigger scoped to its own topic, and never widen one to
compensate for a missed activation.** A trigger decides whose prompts get
rewritten, which makes it a policy statement rather than a tuning knob — the same
standing the `GATEWAY_ROUTE_RULES` keywords already have.

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

- **~1,800 tokens of input on every in-scope request**, uncached, **per skill.**
  With four skills configured, the ceiling matters more than the typical case:
  `GATEWAY_INJECT_MAX_CHARS` caps each skill at 20,000 characters, so a request
  matching all four can carry ~80,000 characters of standard. That is the
  number to sanity-check when adding a skill, not the ~1,800 average. On a
  translated route prompt caching is gone anyway (see
  `NON-ANTHROPIC-MODELS.md`), so it is paid in full there.
- **Cost scales with the catalogue; relevance does not.** Each skill added
  widens the set of conversations that carry *some* injected standard. This is
  the economic half of the catalogue trap above — the reason trigger scope is
  worth guarding even now that the self-trigger bug is fixed.
- **Developers are not told their prompt was modified.** Someone gets guidance
  they did not ask for and cannot see why. This is the same open concern
  redaction has, and injection makes it more visible rather than less — which is
  arguably an improvement, since the injected block says who attached it.
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

21 checks over `_modify()` with Claude-Code-shaped bodies. Exit code 0 or 2, so it
can gate a commit. It forces its own configuration, so the result does not depend
on what is currently exported in your shell. The injection checks load the **real**
manifest and the **real** `SKILL.md`, with the container paths rewritten, so they
test the shipped content rather than a fixture.

It covers the four things most likely to be got wrong: that a **picker alias is
still clamped** (the prefix trap), that redaction is **deterministic**, that
injection is **idempotent and appended after the cache breakpoint** — the two
ways it could silently destroy prompt caching or grow a prompt without bound —
and that **the skill catalogue does not trigger unrelated skills**, which is the
regression for the bug described above. It does *not* prove the proxy calls the
hook — that is level 2.

**Two caveats about this suite, both learned the hard way.** It was green for two
weeks while the catalogue bug was live, because it exercised one skill and a
single skill re-triggering off its own catalogue entry looks identical to
success; the regression check therefore needs **at least two** skills in the
manifest to mean anything. And on 22 Sep 2026 the harness itself was found broken
by the per-plugin mount change — its container-to-repo path rewrite was a prefix
swap, which stopped working once the `skills` segment moved, so every skill body
resolved to a missing file and six checks reported `FAIL` for that rather than
for anything they tested. It now exits loudly when a manifest entry does not
resolve. **A suite that fails for the wrong reason is worse than one that does
not run**, because the output still looks like a verdict.

Behaviour 0 and the attribution it forces have their own suite, because the
property under test is different — it spans both hooks:

```bash
python3 verify_routing.py
```

24 checks. Part 1 drives `_modify()` for the override itself: every spelling of
the client name fires (check 3, nine variants), the deliberate over-match on
`extreme network latency` is **asserted** rather than tolerated (check 4), prose
sharing only one word of the keyword is left alone (check 5), the `system` block
is scanned too, and the override
runs **before** the clamp and the slimmer (check 7). Part 2 drives the real
`_capture()` with callback payloads shaped like the ones in `capture/_kwargs`,
and asserts the requirement directly:

| | selected | actual | captured |
|---|---|---|---|
| routing occurs | `claude-haiku-4-5` | `qwen3:8b` | `qwen3:8b` |
| no routing | `claude-haiku-4-5` | `claude-haiku-4-5-20251001` | `claude-haiku-4-5-20251001` |

plus the trap that matters most: with **no** response to read, the captured model
falls back to the route (`local-qwen3-8b`) and **never** to the client's request.
Part 3 covers the dashboard annotation — the right metadata key per route, and
tags appended rather than replaced.

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
| `GATEWAY_ROUTE_RULES` | the Extreme Networks rule | JSON list of `{name, model, keywords}`; `off` disables route override |
| `GATEWAY_ROUTE_SCAN_CHARS` | `2000000` | How much conversation text is scanned for a trigger |
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
- **Who owns the routing rules, and what is the review path?** One rule ships,
  written by whoever needed it. A rule is a policy statement about where a
  client's data may be processed: adding one to `GATEWAY_ROUTE_RULES` is a
  configuration change any operator can make, and a wrong keyword downgrades
  unrelated work to an 8B model in silence. That list needs a named owner.
- **Should the developer be told their request was rerouted?** Today the gateway
  knows and the client does not (see the gap under Attribution). This is the same
  question as silent redaction, with higher stakes: the answer came from a
  materially weaker model.
- **Keyword matching is the weakest part of this.** It catches the client's name,
  not the client's data — a request full of that client's code that never names
  them is not routed. Treat behaviour 0 as a default-path improvement, not as a
  data-loss control.
- **Prompt caching under redaction is untested on a long real session.** The
  replacement is deterministic so it *should* hold; confirm with a real
  `cache_read_input_tokens` reading before trusting it.
