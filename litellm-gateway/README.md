# LiteLLM capture spike

Goal: find out whether **LiteLLM can serve as the gateway between Claude Code and
Anthropic while capturing the messages that pass through it** — and if so, on
which route and with what fidelity.

This is an evaluation, not a build. The output is a scorecard plus evidence, which
then settles the adopt / hybrid / build decision.

---

## Files

| File | What it is |
|---|---|
| `gateway-up.sh` | **Start everything with one command** — compose stack *and* the host-side Ollama the local routes need. `--status` to report, `--no-local` to skip Ollama |
| `claude-gw.sh` | **Launch Claude Code against the gateway.** Validates the virtual key first, so a credential problem reports itself instead of arriving as an opaque 401 |
| `.env` | Server boot config: provider keys and the master key. Gitignored |
| `.vkey` | The virtual key you call as. Read from here and nowhere else. Gitignored |
| `docker-compose.yml` | LiteLLM proxy + Postgres. Also the authoritative list of every environment variable, each documented next to the code that reads it |
| `picker-shim/` | nginx on port 4001, rewriting `GET /v1/models` so the desktop app's model picker shows non-Anthropic routes |
| `config.yaml` | Models, master key, DB, and the capture callback wiring |
| `custom_capture.py` | The capture itself, plus a raw-payload dump for the first few calls |
| `verify.py` | Scorecard over the capture directory |
| `capture/` | Output. `<session-id>/*.json.gz`, `index.jsonl`, `_kwargs/` |
| `skills-inject.json` | Which skills the gateway injects into matching requests, and their triggers. `path` values point into `/app/skills/<plugin>/<skill>/SKILL.md`, which is where `docker-compose.yml` mounts each plugin — **one mount per plugin**, because a second flat `:/app/skills` would shadow the first and its skills would silently stop injecting |
| `check_skill_usage.py` | Counts whether the model actually *called* an installed skill. **Measures the marketplace path only** — it is blind to gateway injection by construction, so it correctly reads zero while an injected standard is in fact applied. Not sufficient evidence on its own; see `../docs/GATEWAY-MODIFY.md` |
| `verify_modify.py` | Offline scorecard for the request-modification hook |
| `verify_routing.py` | Offline scorecard for content-based route override, and for whether the captured model — in the capture *and* in the dashboard's spend logs — names the model that actually answered |

Documentation lives in [`../docs/`](../docs/) — start with
[`GATEWAY-OVERVIEW.md`](../docs/GATEWAY-OVERVIEW.md), and see
[`NON-ANTHROPIC-MODELS.md`](../docs/NON-ANTHROPIC-MODELS.md) for non-Anthropic
routing and [`GATEWAY-MODIFY.md`](../docs/GATEWAY-MODIFY.md) for request modification.

**Every config key in `config.yaml` should be verified against the LiteLLM docs
for the image tag you pull.** Key names move between releases:
<https://docs.litellm.ai/docs/proxy/configs> ·
<https://docs.litellm.ai/docs/proxy/logging> ·
<https://docs.litellm.ai/docs/proxy/virtual_keys>

---

## Prerequisites

- Docker (installed), with Docker Desktop **running**.
- An Anthropic **API key** from <https://platform.claude.com> with credit on it.
  This is a developer-platform account — **not** a claude.ai subscription, and
  upgrading claude.ai to Pro does not produce one.
- The Claude Code **CLI** (`npm i -g @anthropic-ai/claude-code`, installed).
  The desktop app cannot be pointed at a gateway with these variables — it goes
  through the picker shim on port 4001 instead.
- **For the local routes only:** `ollama` installed, and the model pulled once
  (~5 GB). `gateway-up.sh` starts and configures Ollama, but cannot install it
  or pull for you:
  ```bash
  brew install ollama && ollama pull qwen3:8b
  ```
  Use `./gateway-up.sh --no-local` to skip this entirely.
- A `.env` in this directory. `ANTHROPIC_API_KEY` and `LITELLM_MASTER_KEY` are
  required; every other variable is optional and defaults sensibly. See the
  environment block in `docker-compose.yml` for the full list.
- A `.vkey` in this directory, holding one virtual key. Mint it once with
  `/key/generate` (below) — it is not per-run.

---

## Run

Once `.env` holds the provider keys, this is the whole thing:

```bash
./gateway-up.sh
```

It brings up the compose stack **and** the host-side Ollama that the local
private-model routes depend on, then smoke-tests both local aliases so a broken
prerequisite surfaces here instead of as a hung Claude Code session. Every check
it runs maps to a failure that has actually happened; each prints the fix.

`docker compose up -d` on its own is **not** equivalent. Ollama runs on the
host, not in compose — a Linux container on macOS has no Metal, so a
containerised Ollama drops to CPU and an 8B model becomes unusable. The
consequences of skipping the script:

| Missing prerequisite | Symptom without the script |
|---|---|
| `ollama serve` not running | Local routes fail with an upstream error naming the model |
| `OLLAMA_HOST` not `0.0.0.0` | Ollama binds `127.0.0.1`; Docker's host gateway cannot reach it → connection refused |
| `OLLAMA_KV_CACHE_TYPE` not `q8_0` | 32k KV cache stays fp16 (~4.7 GB on top of ~5.2 GB of weights); on 16 GB it spills to CPU and **looks like a hang** |
| Model not pulled | 404 at request time |

`./gateway-up.sh --status` reports without changing anything.
`./gateway-up.sh --no-local` skips Ollama entirely for a hosted-models-only run.

Then launch Claude Code:

```bash
./claude-gw.sh local-qwen3-8b
```

For the **desktop app**, point it at `http://localhost:4001` (the picker shim)
and choose the `claude-sonnet-4-5-local-q3-8b` row.

### Reboot

Nothing here installs a background service, so after a restart run
`./gateway-up.sh` again — it will start Ollama itself. Docker Desktop must
already be running; the script says so if it is not.

### If it will not boot

If the proxy refuses to start complaining about an unknown setting, comment out
`store_prompts_in_spend_logs` in `config.yaml` — our callback does not depend
on it.

⚠ **Never `docker compose down -v`.** The `-v` destroys the `pgdata` volume and
with it every virtual key and all spend history; `.vkey` then 401s with
"Unable to find token in `LiteLLM_VerificationTokenTable`". Plain `down` is
safe. Also note that `docker compose restart` reuses the container's baked-in
environment, so an edited `.env` needs `up -d` (which is what `gateway-up.sh`
runs) rather than a restart.

### Mint a developer key

```bash
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models":["claude-opus-5","claude-sonnet-5","claude-haiku-4-5"],
       "max_budget": 25,
       "metadata": {"developer":"asif.hussain"}}'
```

The returned `key` (starting `sk-`) is the per-developer credential. This is
LiteLLM giving you attribution and budget enforcement for free — features we had
descoped when planning to write our own gateway.

### Point Claude Code at it

**To just run it, use `./claude-gw.sh` — it does all of the below and validates
the key first.** What follows is the manual form, kept because the choice of
base URL is one of the spike's open questions.

Two candidate base URLs. **Which one works is the main question this spike
answers**, so test both. Candidate A is what everything here runs on today;
B is still untested — see [`../docs/FINDINGS.md`](../docs/FINDINGS.md), "The
passthrough route is untested".

```bash
# Candidate A — LiteLLM's native Messages endpoint
export ANTHROPIC_BASE_URL="http://localhost:4000"

# Candidate B — the Anthropic passthrough route
export ANTHROPIC_BASE_URL="http://localhost:4000/anthropic"
```

Claude Code appends `/v1/messages` itself, so B resolves to
`/anthropic/v1/messages`.

```bash
export ANTHROPIC_AUTH_TOKEN="sk-…the-virtual-key…"
claude
```

Inside Claude Code, run `/status`. You want an `Anthropic base URL` line showing
the proxy and an `Auth token` line naming `ANTHROPIC_AUTH_TOKEN`. A
`Login method` line naming a claude.ai account instead means the variable did not
reach the session.

Set `--model claude-haiku-4-5` for the early runs. Prove the plumbing on the
cheap model before spending anything on Opus.

---

## The nine checks

Run `python3 verify.py` after generating traffic. It judges 1–3 and 5–8 from the
captured files; 4 and 6 need a deliberate test.

### 1. Does capture fire at all?

```bash
ls -R capture/ && cat capture/index.jsonl
```

Nothing there means either the callback is not loaded (check the proxy log for an
import error) or **this route is not logged**. If A logs and B does not, that is
the central finding — see "Likely outcome" below.

### 2. Read a raw payload dump — do this first

```bash
ls capture/_kwargs/
python3 -m json.tool capture/_kwargs/*.kwargs.json | less
```

This is the untouched callback payload. Look for:

- **`kwargs.proxy_server_request.body`** — the original Claude Code request. If
  present, we have the real payload. If absent, LiteLLM is only exposing its own
  normalised view, and capture is lossy by construction.
- **`system`**, **`tools`**, and any **`tool_result`** blocks inside that body.
  Tool results carry the file contents and command output — most of the value of
  capturing at all. Missing tool results is close to disqualifying.
- **`response_obj`** — does it contain `thinking` and `tool_use` blocks, or only
  flattened text?

> **Known caveat:** for streamed requests, `response_obj` is LiteLLM's
> *reassembled* response, not the provider's raw SSE bytes. Callback-based
> capture is therefore normalised, not byte-faithful. That is a property of this
> approach and one of the findings to record — a byte-faithful record needs a tap
> in the data path rather than a callback beside it.

### 3. Response completeness

Run a prompt that makes Claude use a tool (ask it to read a file), then re-run
`verify.py`. You want `tool_use` among the captured block types.

### 4. Long thinking pause — go / no-go, and no config fixes it

Claude Code aborts a stream after **300 seconds of silence**. The provider's
keep-alive pings are the only traffic during a long thinking pause, so if LiteLLM
reconstructs the stream rather than relaying it and drops those pings, sessions
die mid-thought.

Test it: a hard prompt at high effort on Opus, something that will think for
several minutes. If the session dies around the five-minute mark, that is a
blocking failure.

### 5. Prompt caching — the cost check

Send several prompts inside one Claude Code session, then:

```bash
python3 verify.py
```

You need `cache_read_input_tokens` above zero. **Zero cache reads across a
multi-turn session means LiteLLM is reshaping the request and input cost is
roughly ten times what it should be** — silently, with no error.

### 6. Unknown `anthropic-beta` pass-through

Send a request through the proxy by hand with a beta value LiteLLM cannot know
about, and confirm it reaches the provider rather than being stripped:

```bash
curl -sS http://localhost:4000/v1/messages \
  -H "Authorization: Bearer sk-…virtual-key…" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: some-future-capability-2027-01-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":16,
       "messages":[{"role":"user","content":"say OK"}]}'
```

A stripped header means this setup breaks on a future Claude Code release.

### 7. Session and sub-agent attribution

`verify.py` reports distinct session ids and sub-agent ids. Sub-agent traffic
appears once you run something that spawns parallel agents.

### 8. `count_tokens`

```bash
docker compose logs litellm | grep -i count_tokens
```

A `404` means Claude Code falls back to counting context with **real inference
calls** — paying model rates for something that has a free endpoint.

### 9. Which route wins

Repeat 1–8 under both base URLs and record which satisfies the most checks.

---

## Likely outcome, and what to do about it

The pattern to expect is that the **native route logs but normalises**, and the
**passthrough route is faithful but may not log**. If that is what you find, the
answer is a **hybrid**: LiteLLM keeps virtual keys, budgets, and the spend
dashboard; a thin byte-level tap sits in the data path purely to record the
exchange. That keeps the product's governance without accepting lossy capture.

Three possible conclusions:

| Finding | Decision |
|---|---|
| A route passes every check | **Adopt LiteLLM.** Deliverable becomes its configuration plus the capture pipeline. Two weeks saved, budgets and per-developer keys gained. |
| Faithful route is unlogged, or capture is lossy | **Hybrid.** LiteLLM for governance, thin tap for capture. |
| Check 4 or 5 fails on every route | **Evidence to bring back.** Streams aborting after 300 s, or a measurable ten-fold cost increase, is a concrete argument — not an opinion. |

---

## Cost while running this

Nothing here costs money except tokens. Run checks on `claude-haiku-4-5` where
possible. Load a small amount of credit (~$25) and let `capture/index.jsonl` tell
you the real burn rate after the first day rather than estimating it.

⚠️ Never commit the provider key, and do not put it in a `.env` file — this
machine's managed permission rules deny reading `.env`, which will only confuse
you. Export it in the shell that starts the proxy.
