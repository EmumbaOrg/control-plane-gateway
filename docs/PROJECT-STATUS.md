# Enterprise Control Plane for Claude Code — Technical Status Report

**Repository:** `control-plane-gateway` · **Branch:** `skills/emumba-react`
**Prepared:** 31 Aug 2026 (updated — action item 03 delivered) · **Audience:** Project Manager / engineering leadership
**Stage:** Proof of Concept, running on a single developer laptop (`localhost`)

---

## 1. Objective

- Put a **gateway we operate** between Emumba developers' Claude Code and the model provider.
- Four outcomes the PoC set out to prove:
  1. **Capture** — every request and response is recorded, and cannot be bypassed.
  2. **Attribution** — spend is tied to a named developer, not a shared pool.
  3. **Budget control** — a per-developer ceiling that the gateway itself enforces.
  4. **Credential containment** — the real provider API key never reaches a laptop.
- Against that, **five specific action items** were assigned for exploration. Their individual how / why / status is **section 3** — **all five are now complete and verified.** Item 03 (in-flight prompt modification) was delivered on 31 Aug 2026.

---

## 2. Architecture as built

- **Component:** LiteLLM Proxy Server (`ghcr.io/berriai/litellm:main-latest`) + PostgreSQL, via Docker Compose.
- **Services running:** `litellm` (port 4000), `postgres` (5432), `picker-shim` (nginx, port 4001).
- **Path:** Claude Code → `localhost:4000/v1/messages` → key/model/budget checks → real provider key swapped in → Anthropic (or OpenRouter/Groq) → response streamed back → capture callback writes to disk + PostgreSQL.
- **Client configuration is two environment variables** — `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`. Nothing is patched, reverse-engineered, or MITM'd.
- **Custom code we wrote:** `custom_capture.py` (capture callback), `verify.py` (capture scorecard), `run_checks.py` (16-point fidelity harness), `stub/stub_upstream.py` (fake provider), `capture-tap/tap.py` (alternative byte-level tap), `claude-gw.sh` (launcher), `picker-shim/nginx.conf`.
- **Deliberate configuration choice:** `drop_params: false` globally, so LiteLLM never silently discards request fields it does not recognise — the worst failure mode for a capture gateway.

### Credential model (the core security idea)

| Credential | Held by | If it leaks |
|---|---|---|
| `ANTHROPIC_API_KEY` (real) | Gateway host only, never distributed | Worst case — rotate at Anthropic |
| `LITELLM_MASTER_KEY` | Administrator | Serious — can mint keys and read captures |
| **Virtual key** (one per developer) | Each developer | Low — worthless off our network; revoke one DB row |
| PostgreSQL credentials | Proxy container | Internal only |

- **Revoking a developer is one database row.** No key rotation, no redeployment across laptops.

---

## 3. Action items — how, why, status

The five capabilities the PoC was asked to explore. **All five are complete and verified.**

### 3.1 Per-user budget enforcement in LiteLLM — ✅ **Done, verified 24–25 Aug 2026**

- **Why:** without a per-person ceiling, a single developer can spend the whole team's allocation, and there is no way to cap exposure before an invoice arrives.
- **How:** LiteLLM stores `max_budget` and `budget_duration` in the credential's own PostgreSQL row. On every request the auth layer reads that row and compares accrued `spend` against the ceiling — a local database read, no network call. Budgets can sit at four levels (**Organization → Team → Internal User → Virtual Key**) and a request is checked against all of them; the most restrictive wins.
- **How it was proven:** a key capped at `max_budget: 0.002`, then real traffic pushed through it. Six calls succeeded, the seventh onward were refused. Documented both ways — dashboard route and a scripted six-step `curl` walkthrough — in `limited_budget_per_dev.md`. Total cost of one full run: **$0.000225**.
- **Status / findings:**
  - **Recommendation: put the cap on the Internal User, not the key**, so it follows the person across every credential they hold. Mint keys with `max_budget` blank.
  - **Refused calls cost nothing** — 13 refused requests logged `0` tokens and `$0`. Stopped at the gateway, never sent to Anthropic. This is the property worth demonstrating.
  - **It is a brake, not a hard cap.** The crossing request always completes, because price is only known after the call. Measured overruns: 12% on a realistic budget, 29× on a deliberately tiny one.
  - **Sizing matters more than expected.** One message reading *"reply with just hi"* cost **$0.18** — Claude Code ships its system prompt and every tool definition on the first message, so ~47k input tokens is the floor for any turn. Start developers at **$20–50/month**.
  - **`Reset Budget: Not set` is a lifetime cap** — without `budget_duration` the budget never refills and the key dies permanently. Always set `monthly`.
  - **Open risk:** over-budget returns `429` from the auth layer, which blocks every route including `/v1/models`. Claude Code cannot distinguish it from a rate limit and burns all ten retries. **Developers will report this as "the gateway is down"** — needs a support runbook.
  - Not yet tested: throttling instead of hard-blocking (`budget_exceeded_throttle_percentage`), and Budget Fallbacks (Opus → Sonnet → Haiku on a per-model budget breach).

### 3.2 Per-user model list changes through LiteLLM — ✅ **Done, verified 21–25 Aug 2026**

- **Why:** the ability to say "this developer may use Haiku but not Opus" is how model cost and data exposure get governed centrally, rather than by asking people nicely.
- **How:** a `models` allowlist on the virtual key, and/or **Personal Models** on the Internal User. Enforcement is in the same auth layer as the budget, so the request is refused before the provider is ever called. Changes are applied in place with `POST /key/update` or `POST /user/new` — the key string, `max_budget` and accrued spend are untouched, so nothing needs re-issuing to the developer.
- **How it was proven:** a key scoped to `claude-haiku-4-5-20251001` was asked for `claude-opus-5`. The refusal came back in **LiteLLM's own wording**, not Anthropic's — proof the gateway inspected and refused the request itself, before any token was billed:
  ```
  key not allowed to access model.
  This key can only access models=['claude-haiku-4-5-20251001'].
  Tried to access claude-opus-5
          code: 403   type: key_model_access_denied
  ```
- **Status / findings:**
  - **Restricting a key to a single model breaks Claude Code mid-session.** It needs a Haiku-class model for background traffic (titles, summaries) *as well as* the model you picked. Scope to a set, not to one.
  - **`no-default-models` blocks at the user level, not the key.** A user created through the UI can carry that sentinel, meaning "no models except through a team" — every call is then refused with a `403` no matter what the key allows. Fix: Personal Models = `all-proxy-models`, or put them in a team.
  - **`GET /v1/models` returns the calling key's allowlist, not the gateway's config.** Add a model to `config.yaml` without updating the key and clients keep seeing the old list — which looks exactly like the config change having failed. Re-run `/key/update` after every model change.

### 3.3 Use LiteLLM as a MITM proxy to modify the prompt in flight — ✅ **Done, verified 31 Aug 2026**

- **Why:** the highest-value control-plane capability of the five. It moves enforcement from credentials and cost onto **content** — injecting standards, redacting secrets before they leave the network, and applying policy to what is actually sent.
- **How:** LiteLLM's `CustomLogger` exposes `async_pre_call_hook(user_api_key_dict, cache, data, call_type)`; returning a `dict` replaces the body that gets forwarded. Our `custom_capture.py` already subclassed `CustomLogger` and was already registered, so this was one method on an object the proxy was already loading — no new wiring.
- **The risk that had to be cleared first:** whether the hook fires on `/v1/messages` at all. That route behaves differently from the others — it is the reason the `anthropic-beta` gap exists — so it was plausible the hook was skipped there. **It is not.** Read from the installed source: `anthropic_endpoints/endpoints.py:101` dispatches through `base_process_llm_request(route_type="anthropic_messages")`, and `common_request_processing.py:1518` awaits `pre_call_hook` and assigns the result back. Unaffected by the `anthropic-beta` gap, which concerns outbound *headers*, not the body.
- **Two behaviours shipped**, both individually switchable, both fail-open:

  **(a) `max_tokens` clamp — on by default, ceiling 8000.** Claude Code reserves 32000 output tokens on every request and providers gate on the *reservation*, so a 60-token reply gets refused. The CLI can be fixed with an environment variable; the desktop app cannot, so server-side is the only place that fixes every client at once.

  **(b) Secret redaction — off by default.** High-specificity credential patterns in outbound message content are replaced before the request leaves our network. Off by default deliberately: it rewrites developer content, so enabling it should be a decision with a named owner.

- **Proof — the clamp, tested both directions.** Same request (`gemini-3.7-flash`, `max_tokens: 32000`):

  | Clamp | Result |
  |---|---|
  | Disabled | **`402` — "You requested up to 32000 tokens, but can only afford 8199"** |
  | Enabled | **Succeeded.** Reply: `OK`. Audit line records `32000 → 8000` |

- **Proof — redaction.** A request carrying a well-known example AWS key was sent through the gateway. The model's own reply is the evidence: *"I can't repeat back redacted credentials…"* — it never saw the key. Confirmed against the capture file for that exchange: the real key **absent**, `[REDACTED:aws-access-key-id]` **present**, and `max_tokens` correctly left at 32000 because that call was on an exempt Anthropic model.
- **Status / findings:**
  - **The exempt list matches exactly, never by prefix — this is load-bearing.** `claude-haiku-4-5-gmn-37-flash` is a *picker alias for Gemini* and starts with `claude-haiku-4-5`; prefix matching would silently exempt every translated route and defeat the clamp on exactly the models that need it.
  - **Redaction touches `messages` only — never `system` or `tools`.** Those are the cacheable prefix, so rewriting them risks the prompt-caching win for no benefit. `tool_result` blocks *are* walked, and they are the point: they carry the file contents and command output where a committed credential actually surfaces.
  - **Replacements are deterministic** — no counters or timestamps in the placeholder. Claude Code resends the whole conversation every turn, so a varying placeholder would change the cached prefix and cause a silent ~10x input-cost increase.
  - **Audit trail:** every modification appends one line to `capture/modifications.jsonl` recording the *kind* of secret and the count, attributed to a named key — **never the value.** Logging the value would defeat the purpose.
  - **Fail-open, deliberately.** The hook runs in front of every request, so both behaviours fall through to the unmodified body on any error. A modification we failed to apply is a gap; a gateway that refuses all traffic is an outage. **The honest consequence: redaction is best-effort, not a guarantee** — it must not be described as a control that cannot fail.
  - **This also closed the outstanding desktop-app `402`** recorded as "Still unsolved on desktop" in `NON-ANTHROPIC-MODELS.md`. One hook covered both.
  - **Not built:** injecting Emumba standards into the system prompt — the third option considered. Same hook, but it touches the cacheable prefix, so block ordering needs care.
- **Reference:** `GATEWAY-MODIFY.md`.

### 3.4 LiteLLM translating to Anthropic format, so Claude Code can use non-Anthropic models — ✅ **Done, verified 27 Aug 2026**

- **Why:** it tests whether the gateway is a genuine control point or just an Anthropic passthrough — and whether cheaper models can be offered under the same budgets, attribution and capture.
- **How:** **no code was written.** LiteLLM already exposes an Anthropic-format `/v1/messages` endpoint that translates to any provider it supports, and Claude Code already talks to whatever `ANTHROPIC_BASE_URL` points at. The whole change was `model_list` entries in `config.yaml` plus one environment variable. Provider: **OpenRouter** (one key reaches Google, DeepSeek, Z.ai, MiniMax, NVIDIA and others), with Groq entries alongside.
- **How it was proven:** a real `claude` CLI session ran against a free NVIDIA Nemotron route, was asked to read a file with its Read tool, and returned the file's contents. Claude Code → gateway → OpenRouter → NVIDIA → back, with a tool round-trip in the middle.
- **Status / findings:**
  - **34 model routes registered** — 4 Anthropic, 15 honest provider aliases, 15 Claude-shaped picker aliases.
  - **Tool calling survives translation** — the most likely thing to break. The streamed response is a correct Anthropic SSE sequence (`content_block_start` with `tool_use`, `input_json_delta`, `stop_reason: "tool_use"`).
  - **Costing is exact to the last digit.** LiteLLM billed $0.000263625; OpenRouter's own API independently reported $0.000263625 — so budget enforcement on these routes is trustworthy.
  - **Capture, attribution and budgets behave identically** regardless of which provider served the request. **This is the argument for doing this at the gateway rather than with per-developer tooling** like claude-code-router, which has no central budget or audit trail.
  - **Headline gotcha:** Claude Code reserves **32000 output tokens** on every request and providers gate on the reservation, not on what is produced — so free and low-balance accounts reject the traffic with a `402` that reads as a broken model. `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000` fixes it; `claude-gw.sh` sets it by default.
  - **Costs must be pinned by hand.** None of these provider IDs are in LiteLLM's built-in cost map, and an unpriced model bills at **$0** — spend never accrues and `max_budget` never trips. It **fails open, and fails quietly**, which for a budget-enforcement PoC is worse than the model not working. Each paid entry now carries explicit `model_info` prices.
  - **Known losses:** prompt caching is gone (`cache_read_input_tokens: 0`), and `drop_params: true` had to be scoped to these routes — so some Claude Code features degrade **silently** on non-Anthropic models. Anthropic traffic is untouched.
  - **Desktop app needed a shim.** The app's picker hard-vetoes any model ID naming a vendor; the rule was read out of the app bundle. Fixed with vendor-free Claude-shaped aliases plus `picker-shim` (nginx on port 4001) rewriting the display labels. Port 4000 and the CLI are unchanged.
  - **Anthropic does not support this configuration** — they state they do not support routing Claude Code to non-Claude models through any gateway. Record as a standing risk, not a solved problem.

### 3.5 Pulling skill files into Claude Code through LiteLLM — ✅ **Done, verified 27–28 Aug 2026**

- **Why:** central distribution of vetted engineering guidance. Every developer gets the same reviewed React/security/standards material without anyone copying files by hand.
- **How:** LiteLLM's UI "Skills" page turns out to be a **Claude Code plugin registry**, not a skill host. It stores git-source metadata, serves it at `GET /claude-code/marketplace.json`, and developers add it with `claude plugin marketplace add`. Claude Code then clones the repository itself. An Emumba plugin was built in-repo at `plugins/emumba-react/` (manifest plus a vetted `react-best-practices` skill) and registered in the gateway.
- **How it was proven:** registered, installed via the LiteLLM marketplace, and a headless `claude --print` session listed the skills under the plugin's namespace. Also verified end to end against a 19-skill external repository.
- **Status / findings:**
  - **Layout is not negotiable.** Claude Code discovers skills only at `<plugin-root>/skills/<name>/SKILL.md`. A repo nesting them by category installs cleanly and loads **zero** skills — so third-party catalogues must be mirrored into a flat layout, not registered directly.
  - Include a `.claude-plugin/plugin.json`, or skills get namespaced by the version directory instead of the plugin name.
  - Use the `url` git-source form. The `github` form makes Claude Code clone over **SSH**, which fails on any machine without a github.com host key.
  - **Governance limits, verified against the schema:** no `team_id` and no key scoping — every enabled plugin is visible to everyone, so **no per-team catalogues**. `marketplace.json` needs **no authentication**. Disabling a plugin removes discovery only; existing installs keep working. **No install telemetry anywhere.**
  - **Distribution is deterministic; activation is not.** Given a React file with a deliberate performance bug and asked for a review without naming the skill, `claude-sonnet-5` auto-loaded the skill and `claude-haiku-4-5` did not. **Publishing a skill does not guarantee any session uses it**, and cheaper models may ignore the curated catalogue entirely.
  - **Therefore skills are advisory guidance, not a control.** This matters for any compliance framing.
  - **The `version` field in the LiteLLM catalog is decorative — found 31 Aug 2026.** The gateway advertised `0.2.0` while every install sat on `0.1.0`, and `claude plugin update` reported *"already at the latest version (0.1.0)"*. The version the CLI honours comes from the cloned repo's `.claude-plugin/plugin.json`, not the catalog entry. **So bumping the version in the LiteLLM dashboard does not ship an update** — an admin would believe they had released while nobody received it, with no error anywhere. Shipping a change means bumping the manifest and pushing; and developers must run *both* `marketplace update` and `plugin update`, because refreshing the catalog alone does not upgrade an installed plugin.
  - **The registered source does not point at this repository.** It points at `github.com/asif-emumba/testing-skills.git`, so `plugins/emumba-react/` here is the review copy, not what developers actually receive. Reconcile before offering this to anyone.
  - **Open item:** the plugin currently sits on branch `skills/emumba-react` and is registered against a throwaway local clone. Re-point it at the real repository URL once the branch is merged — the source schema has no branch field, so the plugin must sit on the default branch.

### Summary

| # | Action item | Status | Effort to complete |
|---|---|---|---|
| 1 | Per-user budget enforcement | ✅ Done, verified | — (runbook for the 429 outstanding) |
| 2 | Per-user model list changes | ✅ Done, verified | — |
| 3 | MITM prompt modification | ✅ Done, verified | Decide whether redaction is switched on, and who owns the pattern list |
| 4 | Non-Anthropic model translation | ✅ Done, verified end to end | — (unsupported by Anthropic; treat as PoC extra) |
| 5 | Skill files into Claude Code | ✅ Done, verified | Re-point source at the real repo after merge |

---

## 4. What has been proven to work

### 4.1 Capture

- Every exchange writes a request JSON, a response JSON, and a summary line in `index.jsonl`, grouped by Claude Code session ID.
- **Capture cannot be skipped** — traffic must traverse the gateway to work at all. Verified by stopping the proxy: Claude Code fails outright.
- **Fidelity: 15 of 16 automated checks pass** (`FINDINGS.md`), tested against a stub upstream at zero cost.
- The two checks that could have killed the approach both passed:
  - **Survives a 310-second silent gap** — Claude Code aborts a stream after 300s of silence; LiteLLM relays keep-alive pings. No configuration could have rescued a failure here.
  - **Prompt caching is not defeated** — the `system` array reaches the provider unreshaped, attribution block first. A failure here would have raised input cost roughly tenfold with no error message.

### 4.2 Enforcement gates (verified live, 21 Aug 2026)

- **Invalid key** → `401`, provider never called.
- **Disallowed model** → `403 key_model_access_denied`, in LiteLLM's own wording — proof the gateway inspected and refused before any token was billed.
- **Over budget** → `429`, provider never called.

### 4.3 Budgets, attribution and model scoping

- Both work, and both are enforced before the provider is called. Covered in detail as action items **3.1** and **3.2** above.

### 4.4 Cost visibility — the headline finding

- **One typed user question produced four billed API calls** (title generation, tool-call decision, the answer, and a closing recap).
- Reference session: 199,111 input tokens, **$0.1153 for one question**. Three of the four calls are invisible without capture.
- **Consequence for planning:** any cost estimate based on "how many questions did we ask" is wrong by roughly **4×**.

### 4.5 Beyond the original scope

- **Non-Anthropic model routing** and **central skill distribution** are both working and verified — see action items **3.4** and **3.5**.

### 4.6 In-flight request modification

- The gateway now **changes** traffic, not only records it — a `max_tokens` clamp (on) and secret redaction (off by default). Both verified in both directions; see action item **3.3**.

---

## 5. Known gaps and risks — all verified, not assumed

| # | Issue | Impact | Status |
|---|---|---|---|
| 1 | **Unrecognised `anthropic-beta` headers are dropped** on `/v1/messages` | A new Claude Code release shipping an unknown beta can cause a hard `400`, not graceful degradation | Root-caused in LiteLLM source; 4 config workarounds tried, none work. **Next action: test the `/anthropic/*` passthrough route** — now unblocked, we have a real key |
| 2 | **Streaming capture is normalised, not byte-exact** | Content is complete and faithful; the raw SSE byte stream is not preserved. Matters only if byte-exact audit evidence is required | Inherent to callback capture. `capture-tap/` already demonstrates the byte-faithful alternative |
| 3 | **`max_budget` is retrospective, not pre-authorising** | The request that breaches the ceiling still completes. Measured overshoot: **29× on a tiny cap**. Concurrent requests can each pass the check before any writes its spend | By design in LiteLLM. Must always be described as "stops the *next* request", never a hard cap |
| 4 | **Models with no price data cost $0** | Spend never accrues and budgets never trip — **fails open, and fails quietly**. Applies to all zero-cost free routes | Mitigated for paid routes by hand-pinned `model_info` prices. Must be re-checked whenever a model is added |
| 5 | **Over-budget looks like an outage** | LiteLLM returns `429` from the auth layer, killing every route including `/v1/models`. Claude Code cannot distinguish it from a rate limit and burns 10 retries. **A developer will report this as "the gateway is down"** | Documented; needs a support runbook before rollout |
| 6 | **Prompt caching is lost on non-Anthropic routes** | `cache_read_input_tokens` returns 0. With Claude Code's ~27k-token system prompt resent every turn, this is a substantial cost increase | Accepted limitation of format translation |
| 7 | **`drop_params: true` on non-Anthropic routes** | Some Claude Code features degrade **silently** rather than erroring on those models. Anthropic traffic is untouched | Deliberate trade-off — those routes return a hard 400 otherwise |
| 8 | **Anthropic does not support routing Claude Code to non-Claude models** through any gateway | Every breaking Claude Code release is ours to absorb | Record as an ongoing risk, not a solved problem |
| 9 | **Desktop app still sends background traffic to Anthropic** | Selecting a free model does not make a session free, and does not keep prompts off Anthropic | Measured from spend logs. A knob exists to capture that slot; left off deliberately |
| 10 | **Skill activation is not deterministic** | Distribution is reliable; whether a session *uses* a published skill is the model's choice. Sonnet auto-loaded it, Haiku did not | Skills are advisory guidance, **not a control** — important for any compliance framing |

---

## 6. Security and data-handling items requiring a decision

- **Capture files are confidential client data, not logs.** They contain, verbatim:
  - the file paths opened and shell commands run (`tool_use` arguments),
  - **the actual file contents and command output** fed back to the model (`tool_result`),
  - the model's internal reasoning,
  - the full conversation history, resent every turn (one measured turn was 609 KB),
  - a **machine fingerprint** of the developer (OS, CPU architecture, Node version, CLI version).
- They are git-ignored today. **Retention period, storage location, and who may read them are open policy questions** — these need the security owner before this becomes a shared service.
- **The master key is currently a placeholder value** published as an example in the repo's own README, and the dashboard signs in with it. Tolerable while bound to `localhost`; must be replaced with a generated secret before any shared deployment. (Changing it invalidates existing virtual keys — do it before handing keys out.)
- **PostgreSQL port 5432 is mapped to the host** for PoC convenience. Remove before deployment.
- **Two files hold a live virtual key in plaintext** — `litellm-gateway/.vkey` and `litellm-gateway/vkey.json`. Both are correctly git-ignored by the root `.gitignore` and neither has ever been tracked (confirmed with `git check-ignore`), so there is no exposure today. Worth knowing they exist, and that the credential is on disk unencrypted.
- **The LiteLLM image is pinned to a moving `main-latest` tag.** LiteLLM shipped credential-stealing malware in PyPI releases 1.82.7 and 1.82.8. **Pin a digest before this gateway handles anything real.**

---

## 7. Work completed, by artefact

| Area | Artefact | State |
|---|---|---|
| Gateway stack | `litellm-gateway/docker-compose.yml`, `config.yaml` | Running; 34 model routes |
| Capture | `custom_capture.py` | Working; writes plain JSON + `index.jsonl` |
| Verification | `run_checks.py` (16 checks), `verify.py`, `stub/stub_upstream.py` | 15/16 pass, zero-cost |
| Byte-faithful alternative | `capture-tap/tap.py`, `export_captures.py` | Demonstrated on real traffic |
| Desktop model picker | `picker-shim/nginx.conf` | Working on port 4001 |
| Developer launcher | `claude-gw.sh` | Validates the key before launching, so failures are legible |
| Skill distribution | `plugins/emumba-react/` | Registered and verified installing |
| Request modification | `custom_capture.py` (`async_pre_call_hook`), `GATEWAY-MODIFY.md` | Clamp on, redaction off by default; both verified both directions |
| Web report | `Emumba_PoC_Status_Report.html` | This document as a navigable HTML report with seven diagrams |
| Documentation | all of `docs/` — `GATEWAY-OVERVIEW.md`, `DEMO.md`, `SETUP.md`, `FINDINGS.md`, `NON-ANTHROPIC-MODELS.md`, `GATEWAY-MODIFY.md`, `limited_budget_per_dev.md`, `STUB.md` | ~3,000 lines, all findings dated and reproducible |

---

## 8. Outstanding work before this could be a shared service

**Technical — must do**

1. Test the `/anthropic/*` passthrough route (resolves gap #1, now unblocked).
2. Pin the LiteLLM container to a digest.
3. Generate a real master key; remove the PostgreSQL host port mapping.
4. Host on a shared VM with an internal DNS name and TLS in front — auth tokens must not cross the network in plaintext.
5. Distribute client configuration via managed settings rather than by hand.
6. Add a `--model` flag to `run_checks.py` so translated routes can be tested.
7. Verify long thinking pauses on translated routes (the streamed translation contains **no `ping` event** — untested and concerning).
8. Confirm **prompt caching still holds under redaction** on a long real session — the replacement is deterministic so it should, but no `cache_read_input_tokens` reading has been taken with it on.

**Policy — needs an owner**

9. Capture retention, access control, and storage location.
10. Who funds tokens, and at what per-developer budget.
11. A support runbook for the "over-budget looks like an outage" failure — this will otherwise be reported as a gateway outage.
12. **Whether secret redaction is switched on, and who owns the pattern list.** Adding a pattern is a code change today, and a wrong pattern corrupts prompts silently. Related: developers are **not told** when their prompt was modified — the audit line goes to the gateway, not the session.

---

## 9. Recommendation

- **The core control-plane premise is proven.** Capture, attribution, budget enforcement and credential containment all work on real Claude Code traffic, with the failure modes measured rather than assumed.
- **All five assigned action items are now complete and verified.** Item 03 (in-flight prompt modification) was delivered on 31 Aug 2026 and closed the outstanding desktop-app `402` along with it.
- **Three paths remain open**, and the passthrough-route test decides between them:
  - **Adopt LiteLLM** — if the passthrough route forwards `anthropic-beta` and still logs.
  - **Hybrid** — LiteLLM for keys, budgets and the admin UI; a thin byte-level tap in the data path for faithful capture.
  - **Build** — only if neither LiteLLM route is both faithful and logged.
- **Suggested next step:** run the passthrough test, then take the security items in section 6 to the control owner. Non-Anthropic routing and skill distribution are working but should be treated as PoC extras, not commitments — Anthropic does not support the former, and the latter is advisory guidance rather than an enforceable control.

---

*All findings in this document are dated and reproducible from the repository. Figures marked "verified" were measured on the running stack; nothing here is projected.*
