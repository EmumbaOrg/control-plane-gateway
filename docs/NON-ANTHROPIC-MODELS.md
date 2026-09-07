# Running non-Anthropic models in Claude Code through this gateway

No code was written for this. LiteLLM already exposes an Anthropic-format
`/v1/messages` endpoint that translates to any provider it supports, and Claude
Code already talks to whatever `ANTHROPIC_BASE_URL` points at. The whole change
is five entries in `config.yaml` and one environment variable.

Provider used here: **OpenRouter**, because one key reaches OpenAI, Google,
DeepSeek, xAI and others — the fewest credentials to manage for a PoC. **OpenAI,
z.ai and a local Ollama model are reached first-party instead**, each with its own
configuration; see their sections below.

---

## Headline finding: cap `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, or nothing works

Claude Code asks for **32000 output tokens** on every request, and providers
gate on that *reservation*, not on what the model actually produces. That single
number is what makes free and low-balance accounts reject the traffic:

- OpenRouter refuses outright — "you requested up to 32000 tokens, but can only
  afford 10417" (HTTP 402), even though a reply uses ~60 tokens.
- Every provider counts the reservation toward per-minute token limits, so it
  inflates a ~22k request to ~54k.

Setting `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000` fixes it. Measured on
`gemini-3.7-flash` against a free-tier OpenRouter balance:

| `max_tokens` | Result |
| --- | --- |
| 32000 (default) | **402** — cannot afford |
| 8000 | works |
| 4000 | works |
| 2000 | works |

With the cap in place, a real `claude` session on `gemini-3.7-flash` answered
"hi" with "Hello! How can I help you today?" and separately used its Read tool
to read a file and return its contents — on the existing free balance, with no
top-up. `claude-gw.sh` sets the cap by default for this reason.

Raise the cap once the account has credit and you want longer single responses.

### Groq: unusable on the free tier, for a tested reason

Worth recording so it is not re-litigated. Groq's free (`on_demand`) tier makes
throughput and tool calling mutually exclusive — measured from the
`x-ratelimit-limit-tokens` response header on this account:

| Model class | TPM | Tool calling | Verdict |
| --- | --- | --- | --- |
| `openai/gpt-oss-120b`, `gpt-oss-20b`, `qwen/*` | 8,000 | yes | Claude Code's prompt alone is ~22k. No cap helps; even `max_tokens: 1` exceeds the limit. |
| `groq/compound`, `groq/compound-mini` | 70,000 | **no** — `tool calling is not supported with this model` | Claude Code cannot run without tools. |

The two `groq-gpt-oss-*` entries are left in `config.yaml` because they are
correct, cheap and well-priced in LiteLLM's cost map — they become usable on
Groq's paid Dev Tier, which lifts the 8k TPM limit.

---

## The OpenRouter account is on the free tier

The account has a near-zero balance, which is workable but has consequences.

With `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000` the paid models **do** run on the
free balance — that is how `gemini-3.7-flash` was verified. But the balance is
tiny (usage was $0.000264 against no stated limit), so expect to hit 402 again
after a modest amount of real work, and expect it to arrive mid-session.

Add credit at <https://openrouter.ai/settings/credits> before demoing.
`gemini-3.7-flash` is the one to spend on: $0.375 / $1.875 per M, roughly a
cent per Claude Code turn, so $5–10 covers a long session. That also lets you
raise the output cap back toward 32000.

`nemotron-free` needs no balance at all, but it is too small for the harness —
see the warning under **Models configured**.

---

## OpenAI, first-party — working end to end

Everything OpenAI-branded above is **indirect**: `gemini-3.7-flash` and the rest
go through OpenRouter, and `groq-gpt-oss-120b` / `-20b` are OpenAI's *open-weight*
models served by Groq — no OpenAI account is involved in either. One entry now
talks to **OpenAI itself**, with an Emumba OpenAI key: one account, one hop.

| Alias | Picker alias | Model | Input | Output | Context | Max output |
| --- | --- | --- | --- | --- | --- | --- |
| `openai-gpt-41-nano` | `claude-haiku-4-5-oai-41n` | `openai/gpt-4.1-nano` | $0.10 / 1M | $0.40 / 1M | 1,047,576 | 32,768 |

**`gpt-5-nano` is cheaper — $0.05 / 1M input — and was deliberately not chosen.**
The saving is roughly $0.0015 per *thousand* smoke-test calls, so the tie-breaker
is failure modes, not price. `gpt-5-nano` is a reasoning model that rejects
`max_tokens` (wants `max_completion_tokens`), rejects a non-default `temperature`,
and bills invisible reasoning tokens as output. Each is a hard `400` that looks
exactly like a bad key or a wrong provider prefix — the ambiguity a connectivity
test exists to remove. Register `gpt-5-nano` as a *second* entry once volume
justifies it, with the params it actually wants.

### ⚠ `drop_params: true` is mandatory on this route

It was missing at first, and the picker alias failed hard because of it. The
desktop app sends an Anthropic `thinking` block on every `/v1/messages` call.
LiteLLM translates it for an OpenAI-shaped upstream, and `gpt-4.1-nano` — a plain
chat model, not a reasoning one — answers:

```
400  Unsupported parameter: 'reasoning.effort' is not supported with this model.
     ... Received Model Group=claude-haiku-4-5-oai-41n
```

Under this config's global `drop_params: false` that reaches the user verbatim and
reads like a broken gateway or a bad key, when nothing is wrong except a param the
client cannot be told to stop sending. Both entries therefore carry:

```yaml
drop_params: true
additional_drop_params: ["thinking", "reasoning", "reasoning_effort"]
```

`drop_params` alone covers the translated `reasoning_effort`; the explicit list
also covers the newer nested `reasoning` block and the raw `thinking`
passthrough, so the same `400` cannot come back under a different spelling.

### Verified

`verify_openai.py`, 7 Sep 2026, both request shapes, `max_tokens: 16`, prompt
*"Reply with exactly: OK"* — **4/4 pass**:

| Check | Result |
| --- | --- |
| `GET /v1/models` lists the alias | ✅ among 38 routes |
| Cost map populated (not a silent `$0`) | ✅ $0.10 / $0.40 per 1M — agrees with OpenAI's published page to the digit |
| `POST /v1/chat/completions` | ✅ `200` — `text='OK'`, in 12 / out 1 |
| `POST /v1/messages` | ✅ `200` — `text='OK'`, in 12 / out 2 |

The picker alias was probed separately **with a `thinking` block attached**, which
is what the desktop app really sends: `200` both with and without it.

```bash
docker compose exec -T litellm python - < verify_openai.py
```

### If it ever 429s again, read it as billing

This route first ran against a credit-less account and returned `429`
`insufficient_quota` — *"You have no credits remaining."* — on every call. That
was never a gateway fault: OpenAI returns `401` for a bad key and `404` for an
unknown model id, and `credit_balance_exhausted` is account-specific, so the
provider had identified the account before refusing it. `verify_openai.py` names
*which* of the three it is — `401` your key, `404` your config, `429` their
balance — so nobody re-debugs the gateway over an empty wallet. Top up at
<https://platform.openai.com/settings/organization/billing>.

⚠ **And confirm the container actually picked up the new key.** `docker compose
restart litellm` reuses the container's existing environment, so an edited
`OPENAI_API_KEY` in `.env` does **not** take effect and every call keeps `429`ing
exactly as before — which looks precisely like the replacement key being bad too.
Use `docker compose up -d litellm`, which recreates the container. To tell the two
cases apart without printing a secret, `sha256` the value from `.env` and the one
from `docker compose exec -T litellm printenv OPENAI_API_KEY` and compare the
first eight characters.

**The rule the z.ai section states still stands**: *do not register a route before
funding it, because one that answers every call with a billing error reads as a
broken gateway.* This route was briefly the exception to it, and the resolution
was to fund the account rather than to keep arguing for the exception.

---

## Read this before you start

**Anthropic does not support this configuration.** From their own gateway docs:
Anthropic "doesn't endorse, maintain, or audit third-party gateway products, and
doesn't support routing Claude Code to non-Claude models through any gateway."

That is not a blocker for a PoC, but it is the honest framing for anyone
evaluating this as a control-plane feature: Claude Code adds capabilities every
release, and when a release breaks translation, absorbing that is ours. Record
it as a risk, not as a solved problem.

---

## What changed in this repo

Three files, all additively. Nothing that worked before behaves differently.

| File | Change |
| --- | --- |
| `config.yaml` | Five new `model_list` entries, appended below the Anthropic ones, each with pinned costs |
| `docker-compose.yml` | `OPENROUTER_API_KEY` passed through, optional (`:-`, not `:?`) |
| `config.yaml` | Two first-party **OpenAI** entries — `openai-gpt-41-nano` and the picker alias `claude-haiku-4-5-oai-41n`, both → `openai/gpt-4.1-nano`, both with `drop_params: true`; no hand-pinned costs needed (LiteLLM's cost map already carries the real rates) |
| `docker-compose.yml` | `OPENAI_API_KEY` passed through, optional (`:-`, not `:?`) |
| `verify_openai.py` | Four-check verifier for that route; distinguishes `401` / `404` / `429` |
| `NON-ANTHROPIC-MODELS.md` | This file |
| `picker-shim/nginx.conf` | Desktop model-picker label rewriting, on port 4001 — see the picker section |
| `docker-compose.yml` | `picker-shim` service (nginx), additive; port 4000 unchanged |

Specifically **not** changed:

- The global `litellm_settings.drop_params: false`. It stays false, so Anthropic
  traffic keeps its full-fidelity passthrough. `drop_params: true` is set per
  model on the five new entries only.
- The Anthropic `model_list` entries.
- `custom_capture.py`. It is a normal LiteLLM callback, so it fires for these
  routes on the same code path — though see "What we have not verified" below.
- Existing virtual keys. A key's `models` allowlist does not name the new
  aliases, so an old key gets a clean `model not allowed` rather than
  unexpected access. Nobody's budget or permissions shift underneath them.

---

## Setup

### 1. Add the key

Append to `litellm-gateway/.env` (already gitignored):

```
OPENROUTER_API_KEY=sk-or-v1-...
```

### 2. Restart the proxy

```bash
docker compose up -d --force-recreate litellm
```

### 3. Confirm the models registered

```bash
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

You want the five new aliases alongside the Anthropic ones. If they are missing,
the proxy rejected the config — check `docker compose logs litellm`.

### 4. Mint a key that can reach them

```bash
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models":["claude-haiku-4-5","gemini-3.7-flash","gpt-5.3-codex","deepseek-v4-pro"],
       "max_budget": 10,
       "metadata": {"developer":"asif.hussain","note":"mixed-provider test key"}}'
```

Budgets, spend logs and attribution work the same regardless of which provider
served the request. That is the actual argument for doing this at the gateway
rather than per-developer with a tool like claude-code-router.

### 5. Smoke-test with curl before involving Claude Code

Cheapest model first, and confirm translation works at all:

```bash
curl -s http://localhost:4000/v1/messages \
  -H "Authorization: Bearer sk-…the-virtual-key…" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.7-flash","max_tokens":64,
       "messages":[{"role":"user","content":"Reply with exactly: OK"}]}'
```

A well-formed Anthropic response body (`"type":"message"`, `content` array)
means LiteLLM translated the request out and the response back. That is the
whole mechanism.

### 6. Point Claude Code at it

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="sk-…the-virtual-key…"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000   # see the note below on why
claude --model nemotron-free
```

**The base URL must be `http://localhost:4000`** — "Candidate A" in
`README.md`. The `/anthropic` passthrough route (Candidate B) cannot serve
these: it forwards the body to Anthropic unchanged, so no translation happens.
If your shell still exports the passthrough URL from earlier testing, these
models will fail in a confusing way.

Run `/status` inside the session to confirm the base URL and auth token landed.

---

## Models configured

Updated 27 Aug 2026. The two paid OpenAI routes (`gpt-5.2`, `gpt-5.3-codex`)
were **removed** — this account has no OpenRouter balance, so they only ever
returned 402. Zero-cost routes were added in their place — the current set is
the table below.

**Paid** (need OpenRouter credit; prices per million tokens, input/output):

| Alias | OpenRouter ID | Context | Price |
| --- | --- | --- | --- |
| `gemini-3.7-flash` | `google/gemini-3.7-flash` | 1.05M | $0.375 / $1.875 |
| `deepseek-v4-pro` | `deepseek/deepseek-v4-pro` | 1.05M | $0.579 / $1.158 |

**Zero-cost.** Selected from the live OpenRouter catalogue on 27 Aug 2026 by
three filters: prompt *and* completion price exactly 0, `tools` in
`supported_parameters` (Claude Code cannot run without tool calling), and an
output ceiling >= 16384. 20 free models were listed, 17 had tools, and these are
the largest of those:

| Alias | OpenRouter ID | Context | Max output |
| --- | --- | --- | --- |
| `minimax-m3-free` | `minimax/minimax-m3:free` | 1.05M | 943K |
| `dots3-note-free` | `dots-studio/dots-3-note-preview:free` | 512K | 461K |
| `nemotron-super-120b-free` | `nvidia/nemotron-3-super-120b-a12b:free` | 262K | 236K |
| `minimax-m27-free` | `minimax/minimax-m2.7:free` | 197K | 177K |
| `inkling-small-free` | `thinkingmachines/inkling-small:free` | 1.05M | 262K |
| `nemotron-ultra-550b-free` | `nvidia/nemotron-3-ultra-550b-a55b:free` | 1M | 65K |
| `north-mini-code-free` | `cohere/north-mini-code:free` | 256K | 64K |
| `gemma-4-31b-free` | `google/gemma-4-31b-it:free` | 262K | 32K |
| `nemotron-free` | `nvidia/nemotron-3.5-lightning:free` | 1M | 65K |

Deliberately excluded, with reasons: `thinkingmachines/inkling:free` (403,
"only available on agentic harnesses" — `inkling-small` is its sibling and is
**untested**, it may do the same), `poolside/laguna-*:free` (rate-limits
immediately), `liquid/lfm-2.5-2.6b:free` (8192 output ceiling), `openrouter/free`
(0 output ceiling), and `google/lyria-3-*` plus
`nvidia/nemotron-3.5-content-safety:free` (no tool calling).

Free capacity is best-effort and can 429 or 502 at any time. Fine for proving
the plumbing; do not demo on it. Note also that LiteLLM logs `Skipping all
budget checks for zero-cost model`, so `max_budget` does not constrain these
routes at all — a real consideration for a control-plane PoC whose point is
budget enforcement.

Re-check the catalogue before blaming the gateway for a `not_found_error`:

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -m json.tool | less
```

To add another, copy an existing block and change `model_name` and the ID.
Re-run the `curl` in step 3 to confirm it registered — OpenRouter retires IDs,
and a wrong one fails at request time, which reads as "the gateway is broken".

**Copy the `model_info` block too, and put real prices in it.** See below.

---

## Costs must be pinned by hand, or budgets silently stop working

This is the trap, and it was found by booting the config rather than by reading
docs. None of these OpenRouter IDs are in LiteLLM's built-in cost map. The proxy
says so on startup, once per model:

```
LiteLLM:WARNING: register_model: model=openrouter/deepseek/deepseek-v4-pro not
in built-in cost map and no prefix/region variant matched
```

With no cost data, every call through that model is priced at **$0**. Spend logs
show zero, the dashboard shows zero, and `max_budget` on a virtual key never
trips — a developer could run an unbounded spend against a key that looks
perfectly well governed. For a PoC whose entire point is central budget
enforcement, that is worse than the models not working at all: it fails open,
and it fails quietly.

So each of the five entries carries an explicit `model_info` block with
`input_cost_per_token`, `output_cost_per_token` and the cache-read cost. Verified
after the change — `GET /model/info` now reports a real price for all nine
models, and the startup warnings for the five named models are gone.

Re-check these prices whenever you re-check the model IDs. A stale price is a
wrong budget, not an error.

---

## Verification actually performed

The config change was tested by starting a **separate** LiteLLM container on
port 4001 against the same config, leaving the running gateway on 4000 alone.
Confirmed there:

- The proxy boots with `OPENROUTER_API_KEY` unset or empty. It does not refuse
  to start, which is what makes the change safe for anyone who has not added
  the key.
- `GET /v1/models` lists all nine models — four Anthropic, five new.
- `GET /model/info` reports correct per-token costs for all nine.
- No config errors or tracebacks in the logs.
- The four Anthropic entries are byte-for-byte unchanged and still resolve to
  their built-in prices.

Not tested, because it needs the key and spends money: an actual completion
through any of the five. Step 5 above is that test.

---

## The one real tradeoff: `drop_params`

`config.yaml` deliberately keeps `drop_params: false` globally, with a comment
explaining why: if LiteLLM silently discards request params it does not
recognise, new Claude Code capabilities disappear without erroring — the worst
failure mode for a capture gateway.

Non-Anthropic providers force the opposite choice. Claude Code sends
Anthropic-only params, and an OpenAI-format endpoint rejects them outright
(`Unknown parameter: 'output_config'` — [LiteLLM #22963](https://github.com/BerriAI/litellm/issues/22963)).
Without dropping them these routes return a hard 400 on every request.

So `drop_params: true` is scoped to the five new entries. The consequence is
real and worth stating plainly: **on non-Anthropic models, some Claude Code
features will degrade silently rather than error.** Anthropic traffic is
untouched and keeps full fidelity.

---

## Confirmed working, end to end

A real `claude` CLI session ran against `nemotron-free`, was asked to read a
file with its Read tool, and returned the file's contents. That is the whole
thing working: Claude Code → gateway → OpenRouter → NVIDIA → back, with a tool
round-trip in the middle.

Verified along with it:

- **Tool calling survives translation.** The streamed response is a correct
  Anthropic SSE sequence: `content_block_start` with a `tool_use` block,
  `input_json_delta` carrying the arguments, `stop_reason: "tool_use"`. This
  was the most likely thing to break and it does not.
- **Costing is exact.** LiteLLM billed the paid-model tests at
  `$0.000263625`; OpenRouter's own `/api/v1/key` endpoint independently
  reported usage of `0.000263625`. The pinned prices agree with the provider
  to the last digit, so `max_budget` enforcement is trustworthy.
- **Capture fires normally.** `custom_capture.py` recorded every exchange with
  the real Claude Code session id, `stream: true`, `has_raw_body: true`, and a
  captured `tool_use` round-trip.
- **Attribution and budgets work** exactly as on the Anthropic path — that is
  the argument for doing this at the gateway rather than per-developer.

### `--model` works, but Claude Code does not recognise the name

The CLI accepts an arbitrary alias and runs, but warns:

```
"nemotron-free" is not a model this version of Claude Code recognizes, so
auto-compact will keep this session within 200k tokens
```

It assumes a **200k context window** regardless of the model's real one. On a
1M-context model that silently throws away three quarters of the context. Fix
it per model with `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, or the `modelOverrides`
setting, or by appending `[1m]` to the model name. The commands above set the
variable for this reason.

---

## Still not verified

1. **Long thinking pauses.** README check 4 is the go/no-go and has not been
   run against a translated route. Related and concerning: **the translated
   stream contains no `ping` event.** README check 2 exists because a gateway
   that does not relay keep-alive pings will abort long thinking pauses. Test
   this before trusting the setup on real work.
2. **`run_checks.py` cannot test these routes yet.** It exercises exactly the
   right request shape but is hardcoded to `claude-haiku-4-5` against the stub
   upstream. It needs a `--model` flag.

### Known losses

- **Prompt caching is gone.** `cache_read_input_tokens` came back `0`. Input
  cost is roughly what it would be with caching disabled — which for Claude
  Code's ~27k-token system prompt on every turn is substantial.
- **Attribution records the underlying ID, not your alias.** Spend logs show
  `openrouter/nvidia/nemotron-3.5-lightning:free` and capture shows
  `nvidia/nemotron-3.5-lightning:free`, not `nemotron-free`. Any reporting
  that groups by alias needs to map these.
- **The streamed `message_start` reports the underlying model ID** while the
  non-streaming response reports the alias. Inconsistent; harmless so far.

---

## Claude Code desktop app: the model picker hides these models

Observed symptom: the LiteLLM dashboard lists all ten models, but the desktop
app's picker shows only the four `claude-*` ones.

Two causes, neither a misconfiguration:

1. **The desktop app ignores `ANTHROPIC_BASE_URL` and `settings.json`.** It
   reads gateway routing only from its own third-party inference
   configuration, so nothing you export for the CLI reaches it.
2. **Auto-discovery filters by name.** Per Anthropic's third-party gateway
   docs, discovery "shows only models whose IDs are recognizably Claude." Our
   gateway advertises all ten on `GET /v1/models` — verified — and the app
   drops `gpt-5.2`, `gemini-3.7-flash`, `deepseek-v4-pro` and `nemotron-free`
   before rendering the picker. LiteLLM's dashboard applies no
   such filter, hence the discrepancy.

<a id="the-filter"></a>
### The actual filter rule — read out of the client, 27 Aug 2026

This was previously inferred from what survived, and the inference was wrong in
the one way that mattered. The rule is **not** server-side: it lives in the
desktop app's `app.asar`, in the `[custom-3p]` model-discovery function. Search
that bundle for `anthropic_family_tier` to find it. Reduced to readable form:

```js
TIERS = ['sonnet','opus','haiku','fable','mythos']
BARE  = /^(sonnet|opus|haiku|fable|mythos)(-[\d.]+)?$/
ALLOW = ['claude', ...TIERS, 'anthropic']
DENY  = /ark-code|astron|command-r|deepseek|doubao|gemini|gemma|glm|gpt|grok|
         hermes|hy3|kimi|lfm|\bling\b|llama|longcat|mimo|minimax|mistral|
         mixtral|moonshot|nemotron|openai|phi-|qianfan|qwen|tc-code|\bunic\b|
         yi-|stepfun|step-3|seed-|bytedance|hunyuan|granite|amazon\.nova|nova-|
         devstral|ministral|ernie|codex|arcee|trinity|abab|phi\d|\bk2\.|\bm2\.|
         jamba|arctic|solar|mercury|zamba|kat-coder|\bds-|dpsk/

isClaudeName(id) =
  DENY.test(id) ? false                                    // hard veto, FIRST
                : BARE.test(id) || ALLOW.some(w => id.includes(w))

keep(m) = isClaudeName(m.id) || TIERS.includes(m.anthropic_family_tier)
```

Two consequences, both decisive:

- **`DENY` is a hard veto evaluated before anything else.** A `claude-` prefix
  cannot rescue an id that also names a vendor.
- **`anthropic_family_tier` is a genuine second branch with no deny list.** A
  model carrying a valid tier passes *under its honest name*.

The app also reads `display_name` (used as the picker label, falling back to the
id), `is_family_default`, and infers `supports1m` from `supports_1m === true`
**or** `max_input_tokens >= 1_000_000`.

The discovery request itself is `GET {baseUrl}/v1/models?limit=1000` with
`anthropic-version: 2023-06-01`. Note that LiteLLM returns the *Anthropic*-shaped
list for that header (`{"type":"model","id":...,"display_name":...}`), not the
OpenAI-shaped one you get from a bare `curl /v1/models`.

### Setting `inferenceModels` explicitly does NOT work — retracted 27 Aug 2026

This section previously recommended an explicit `inferenceModels` list, on the
documented grounds that it "overrides discovery entirely… the picker will show
exactly the entries you provide." **It was tried and it does not work for
non-Anthropic names.** The same name filter is applied to the explicit list,
and the app reports each rejection in its configuration warnings:

```
inferenceModels: "gpt-5.2" is not an Anthropic model and was removed from the
list — expected a gateway model route referencing an Anthropic model (e.g.
claude-sonnet-4-5, anthropic/claude-*). Name routes to match the underlying model.
```

All eight non-Anthropic entries were stripped; the four `claude-*` ones
survived, which is exactly the symptom this section opens with. Note that this
contradicts the vendor documentation, so re-test it after an app update before
assuming it still holds. The filter rule itself is server-side — the strings
above are **not** present in the client binary — so the precise test it applies
cannot be read locally, only inferred from what survives.

`modelDiscoveryEnabled: false` is not the workaround it looks like. Its only
interaction with `inferenceModels` is to warn that bare tier aliases such as
`sonnet` need discovery to resolve; it has no bearing on the Anthropic-name
filter.

### The filter runs TWICE — `anthropic_family_tier` is not a way in

Retracted from the first draft of this section: injecting `anthropic_family_tier`
was expected to be sufficient. It is not. The name check is applied a second
time when the picker list is assembled, in `jkt()`:

```js
if (adminList?.length) a = adminList.map(...)   // explicit inferenceModels
else                   a = discovered ?? []     // discovery results
return provider ? a.filter(t => uo(provider, t.id).ok) : a

uo('gateway', id) -> vge(id) -> lo(id)          // same deny-list-first check
```

The tier field gets a model past **discovery** only. Its actual purpose is
`shortnameIdentityOverrides()` — pinning what the bare `sonnet` / `opus` aliases
resolve to. It never puts a row in the picker, and injecting it on the gateway
side would silently steal those aliases from the real Anthropic models. The shim
no longer sets it; don't add it back.

**Consequence: on a gateway provider only Anthropic-looking IDs can ever appear
in the picker.** Renaming is not one option among several, it is the only one.

### Fix as implemented: vendor-free aliases + `picker-shim`

Two parts, because the app checks two different things.

**Part 1 — the ID has to pass `lo()`.** The earlier `claude-<tier>-via-<real
model>` aliases named their vendor in every entry, which is exactly what `DENY`
vetoes. They were renamed to carry a tier plus a **vendor-free** abbreviation.
Every name was checked against `DENY` individually before being committed:
`dsk` avoids both `\bds-` and `dpsk`; `gmn` avoids `gemini`; `zai` avoids
`glm`; `nvda` avoids `nemotron`; `mmx` avoids `minimax`; `mmx-m27` avoids
`\bm2\.`; `goog-g4` avoids `gemma`; a bare `groq-oss` avoids `gpt`.

**Part 2 — the label.** Those IDs are unreadable, and the picker labels each row
`display_name || id`. LiteLLM sets `display_name` to the id, so the picker would
read `claude-sonnet-4-5-mmx-m3-free`. `picker-shim/` fixes that.

| honest alias (CLI) | picker alias | picker label |
| --- | --- | --- |
| `gemini-3.7-flash` | `claude-haiku-4-5-gmn-37-flash` | Gemini 3.7 Flash (gateway) |
| `deepseek-v4-pro` | `claude-sonnet-4-5-dsk-v4-pro` | DeepSeek V4 Pro (gateway) |
| `nemotron-free` | `claude-haiku-4-5-nvda-free` | Nemotron 3.5 Lightning free (gateway) |
| `groq-gpt-oss-120b` | `claude-sonnet-4-5-groq-oss-120b` | Groq gpt-oss-120b (gateway) |
| `groq-gpt-oss-20b` | `claude-haiku-4-5-groq-oss-20b` | Groq gpt-oss-20b (gateway) |
| `openai-gpt-41-nano` | `claude-haiku-4-5-oai-41n` | GPT-4.1 nano (gateway) |
| `minimax-m3-free` | `claude-sonnet-4-5-mmx-m3-free` | MiniMax M3 free (gateway) |
| `nemotron-super-120b-free` | `claude-sonnet-4-5-nvda-super-120b-free` | Nemotron 3 Super 120B free (gateway) |
| `north-mini-code-free` | `claude-haiku-4-5-north-mini-code-free` | North Mini Code free (gateway) |
| `dots3-note-free` | `claude-sonnet-4-5-dots3-note-free` | DOTS-3 Note free (gateway) |
| `nemotron-ultra-550b-free` | `claude-sonnet-4-5-nvda-ultra-550b-free` | Nemotron 3 Ultra 550B free (gateway) |
| `minimax-m27-free` | `claude-sonnet-4-5-mmx-m27-free` | MiniMax M2.7 free (gateway) |
| `inkling-small-free` | `claude-sonnet-4-5-tm-inkling-small-free` | Inkling Small free (gateway) |
| `gemma-4-31b-free` | `claude-sonnet-4-5-goog-g4-31b-free` | Gemma 4 31B free (gateway) |

The honest aliases in the left column are unchanged and still the ones to use
when scripting. The picker aliases exist only to satisfy `lo()`.

#### What `picker-shim` is

An `nginx:1.27-alpine` container (`picker-shim/nginx.conf`, wired into
`docker-compose.yml`) listening on **4001**:

```
desktop app ──► localhost:4001 (picker-shim) ──► litellm:4000 ──► providers
CLI / curl  ──► localhost:4000 ─────────────────────┘
```

It has exactly two locations. `location = /v1/models` rewrites `display_name`
via `sub_filter`, one rule per alias. `location /` proxies everything else
straight through. That is the whole component — no logic, no state.

Why a separate process rather than LiteLLM config: LiteLLM's `/v1/models`
serialiser emits a fixed field set and offers no hook for overriding
`display_name` (verified on `main-latest`, 27 Aug 2026).

Details that are load-bearing, each learned by breaking it:

- **`proxy_set_header Accept-Encoding ""`** — `sub_filter` cannot rewrite a
  gzipped body. Without this the rules silently do nothing.
- **`proxy_buffering off`** on the catch-all location, or `/v1/messages` SSE is
  held and arrives as one late blob.
- **`sub_filter_types application/json`** — `sub_filter` only touches
  `text/html` by default.
- **Rules anchor on the quote after `"display_name":`**, so a short id cannot
  also match a longer id that contains it.
- **It does NOT inject `anthropic_family_tier`.** An earlier revision did, on
  the theory that it was the supported way past the filter. It is not — see the
  section above — and it has a side effect: the app uses that field in
  `shortnameIdentityOverrides()` to decide what the bare `sonnet`/`opus`/`haiku`
  aliases resolve to. Injecting it silently redirects those. Left out
  deliberately; see the background-traffic note below for when you might want
  it.

#### The desktop app still sends traffic to Anthropic

Measured, not assumed. One turn on `claude-haiku-4-5-nvda-free`, from
`LiteLLM_SpendLogs`:

```
10:09:27 | openrouter/nvidia/nemotron-3.5-lightning:free | openrouter | claude-haiku-4-5-nvda-free | $0
10:09:26 | anthropic/claude-haiku-4-5                    | anthropic  | claude-haiku-4-5           | $0.00043
10:09:20 | anthropic/claude-haiku-4-5                    | anthropic  | claude-haiku-4-5           | $0.000013
```

The selected model handled the turn. The two Anthropic rows are Claude Code's
**background traffic** — titles, summaries, small internal calls — which always
goes to a haiku-class model regardless of the picker selection. It resolves to
the literal `claude-haiku-4-5` because no gateway model claims a tier.

So **selecting a free model does not make a session free, and does not keep
prompts off Anthropic.** Pennies per session, but for a control-plane PoC the
data-flow half matters more than the cost. If you need it gone, inject
`anthropic_family_tier: haiku` on one free route in the shim — that is the knob
that captures the background slot. It is a deliberate decision, not a fix, which
is why it is off by default.

#### Setup in the desktop app

1. **Help → Troubleshooting → Enable Developer Mode** (restarts the app, adds a
   Developer menu)
2. **Developer → Configure Third-Party Inference…**
3. **Connection** → Inference provider: `Gateway`
4. **Gateway credentials**: base URL `http://localhost:4001` — **the shim, not
   4000**; pointing at 4000 gives unreadable picker labels — the LiteLLM virtual
   key as the API key, credential kind **Static API key**, auth scheme
   **Bearer**
5. **Models** → **clear `inferenceModels` entirely.** A set list keeps the app
   on the explicit path and skips discovery, so new aliases are never seen. An
   empty setting is what turns discovery back on. (It would not help anyway —
   the same `lo()` check is applied to an explicit list.)
6. Restart the app. The picker caches discovery, so a reload is not enough.

<a id="key-allowlist"></a>
#### The virtual key gates the picker, and this is the trap

`GET /v1/models` returns **the calling key's `models` allowlist**, not the
gateway's config. Add or rename an alias without updating the key and the app
keeps receiving the old list — the picker simply does not change, which looks
exactly like the rename having failed. This cost an hour.

Re-run after every `config.yaml` model change. It reads the list from the config
so it cannot drift:

```bash
curl -s -X POST http://localhost:4000/key/update \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"key\":\"$LITELLM_VIRTUAL_KEY\",\"models\":$(grep '^  - model_name:' config.yaml \
       | sed 's/^  - model_name: //' \
       | python3 -c 'import sys,json;print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))')}"
```

`/key/update` edits settings in place — the key string is unchanged, and
`max_budget` and accrued spend are untouched, so nothing needs re-entering in
the app.

#### Adding a model later — the whole checklist

Miss any step and the failure is silent or misleading:

1. Add the honest entry to `config.yaml`, with `model_info` costs (see the
   costs section — a missing cost prices at $0 and budgets stop tripping).
2. Add a picker alias, and **check the name against `DENY` first**. The regex is
   quoted in full earlier in this section.
3. Add a `display_name` rule to `picker-shim/nginx.conf`.
4. `docker compose restart litellm picker-shim`.
5. Re-run the `/key/update` command above.
6. Restart the desktop app.

#### Verification performed

- The app's real discovery request, replayed against `:4001` and run through a
  reimplementation of `lo()`: 24 of 34 routes kept, all picker aliases among
  them, labels correct. *(Measured 27 Aug 2026; the route list has changed since
  — re-run before quoting the figures.)*
- `picker-shim` access log shows the app's own
  `GET /v1/models?limit=1000 200` and `POST /v1/messages 200`, so proxying and
  streaming through the shim work in practice, not only in theory.
- Spend logs confirm alias → upstream routing is correct (the table above).

#### Still unsolved on desktop

**The output ceiling.** Claude Code asks for 32000 output tokens. The CLI caps
it with `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000` (`claude-gw.sh`); the desktop app
exposes no equivalent. On a near-zero OpenRouter balance every paid route
therefore returns:

```
402 "You requested up to 32000 tokens, but can only afford 2210"
```

which reads as a broken model rather than an empty wallet. The `:free` routes
have no such gate, which is why the free set exists. A server-side clamp — a
LiteLLM pre-call hook in `custom_capture.py` that lowers `max_tokens` — would
fix it for every client at once and has not been built.

### Also expect on desktop

- **MCP tool search and other beta features are off by default** on
  third-party gateways, because strict gateways reject the experimental
  `anthropic-beta` headers. `toolSearchEnabled` turns them on; the documented
  preflight is to run terminal Claude Code through the same gateway with
  `ENABLE_TOOL_SEARCH=true` first.
- **Remote Control and SSH/cloud environments are unavailable** while a gateway
  configuration is active — sessions run locally only.

---

## Unrelated but worth fixing while you are here

`docker-compose.yml` pins `ghcr.io/berriai/litellm:main-latest`. LiteLLM shipped
credential-stealing malware in PyPI releases 1.82.7 and 1.82.8, and a moving
`main-latest` tag gives no control over what gets pulled. Pin a digest before
this gateway handles anything real.

---

## Alternative considered: claude-code-router

Purpose-built for Claude Code, and better at per-task routing than LiteLLM's
router — a cheap model for background/Haiku-slot traffic, a strong one for the
main loop, with fallback chains.

Rejected for this PoC because it runs per developer on their own machine, with
no central budget, attribution or audit trail. That is the opposite of what a
control plane is for. Worth revisiting only if task-aware routing turns out to
matter more than centralisation.

---

## Sources

- [LiteLLM: Use Claude Code with Non-Anthropic Models](https://docs.litellm.ai/docs/tutorials/claude_non_anthropic_models)
- [LiteLLM: `/v1/messages`](https://docs.litellm.ai/docs/anthropic_unified/)
- [LiteLLM: OpenRouter provider](https://docs.litellm.ai/docs/providers/openrouter)
- [LiteLLM: `drop_params`](https://docs.litellm.ai/docs/completion/drop_params)
- [Claude Code: Other LLM gateways](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code: connect to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect) (per-surface config, incl. desktop app)
- [Claude Desktop on 3P with an LLM gateway](https://claude.com/docs/third-party/claude-desktop/gateway) (`inferenceModels`, discovery filter)
- [Claude Code: environment variables](https://code.claude.com/docs/en/env-vars)
