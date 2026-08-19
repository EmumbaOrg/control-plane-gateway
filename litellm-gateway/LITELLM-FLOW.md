# LiteLLM capture flow — status

Roadmap step 3: connect Claude Code to a gateway we control and capture the
messages it exchanges with the model provider. This document tracks the LiteLLM
approach specifically — what it is, what works, and what is left.

_Last updated 18 Aug 2026._

---

## The flow, end to end

```
Claude Code CLI                LiteLLM proxy (:4000)              Anthropic API
  ANTHROPIC_BASE_URL   ───────▶  1. validate virtual key   ─────▶  api.anthropic.com
  ANTHROPIC_AUTH_TOKEN           2. swap in provider key
  (a LiteLLM virtual key)        3. forward + stream back
                                 4. custom_capture callback
                                        │
                                        ▼
                                 capture/<session-id>/*.json   (plain files)
                                 capture/index.jsonl
                                 Postgres  (spend, virtual keys, admin UI)
```

1. **Claude Code** is pointed at the proxy with two environment variables:
   `ANTHROPIC_BASE_URL=http://localhost:4000` and
   `ANTHROPIC_AUTH_TOKEN=<a LiteLLM virtual key>`.
2. **LiteLLM** validates the virtual key against Postgres, resolves it to a
   developer identity and budget, then replaces it with the real provider key
   held server-side and forwards the request to Anthropic.
3. The response streams back through the proxy to Claude Code.
4. On each success or failure, LiteLLM invokes our **`custom_capture.py`**
   callback, which writes the request and response to plain JSON files and
   appends a summary line to `index.jsonl`.

What LiteLLM gives us for free, that a hand-built gateway would not:
**per-developer virtual keys, spend budgets, and an admin UI** at
`http://localhost:4000/ui` (log in with the master key).

---

## What we have done

- **Stood the proxy up** via Docker Compose (`docker-compose.yml`) with Postgres.
  Running it directly with `uv` failed on a FastAPI version conflict; the
  container image has pinned dependencies and works first time.
- **Wrote and loaded a capture callback** (`custom_capture.py`). Confirmed in the
  proxy log: `Initialized Callbacks - [<custom_capture.Capture object>]`.
- **Built a no-cost test harness** — a stub Anthropic upstream (`stub/`) plus a
  16-point check script (`run_checks.py`) — so fidelity can be measured without
  an API key or any spend. See `STUB.md`.
- **Ran the checks through LiteLLM. 15 of 16 pass**, including the two that could
  have ruled it out (see below). Full results in `FINDINGS.md`.
- **Found where the capture payload lives.** On the `/v1/messages` route the
  original request is at `kwargs["litellm_params"]["proxy_server_request"]`, not
  the top-level `proxy_server_request` that examples suggest. The callback now
  checks several locations in order of fidelity.
- **Diagnosed the one failure** to its source in LiteLLM's code (see below).
- **Switched storage to plain files.** Captures were gzipped; they are now plain
  `.json` — double-clickable, greppable, no decode step. Fine for a PoC; revisit
  at real volume, since Claude Code resends the whole conversation each turn and
  the data is large and highly redundant.

### Checks that passed, and why they mattered

| Check | Why it was a risk |
|---|---|
| Survives a 310s silent gap | Claude Code aborts a stream after 300s of silence. LiteLLM relays keep-alive pings, so long thinking pauses survive. **No config could have fixed a failure here.** |
| `system` array preserved, attribution block first | Reshaping it defeats prompt caching — a silent ~10x input-cost increase. |
| `tool_result` content forwarded | Tool results carry file contents and command output — most of the value of capturing. |
| Upstream 400 body unmodified | Claude Code's retry logic string-matches on the provider's error text. |
| Capture callback fired, with the raw request body | Without the raw body, capture would be LiteLLM's normalised view, not what Claude Code actually sent. |

---

## What is remaining

### 1. The one open failure — dropped `anthropic-beta` (blocking for correctness)

On the `/v1/messages` route, a client's `anthropic-beta` value **is not forwarded
to the provider**. LiteLLM auto-injects beta headers for capabilities it
recognises, so known features work; what is lost is any beta value it does not
know — exactly what a new Claude Code release ships. Because Anthropic pairs a
beta header with body fields, a stripped header against a forwarded field is a
hard `400`, not a graceful degradation.

Root cause (read from the installed source): the outbound header builder reads
`kwargs.get("headers")`, but nothing on this route populates it from the incoming
request. `forward_client_headers_to_llm_api` is wired for the Responses API path,
not `anthropic_messages`. Four config workarounds tried, none effective — details
in `FINDINGS.md`.

**Next action:** test the `/anthropic/*` passthrough route, which may forward
headers verbatim and make this moot. **That test needs a real API key** — it
can't be checked against the stub, which the passthrough route ignores.

### 2. Test against real Claude Code, not the harness

`run_checks.py` sends Claude-Code-*shaped* requests. The genuine article is far
larger and changes with each release. Point the real CLI at the proxy for one
session and re-run `verify.py`. (Done already for the thin-tap approach — see
`../capture-tap/` — but not yet through LiteLLM.)

### 3. Confirm prompt caching on real traffic

The stub can only *report* cache fields. Whether a prefix actually caches is
Anthropic's decision and needs a real key. The thin-tap run already showed a real
`cache_read_input_tokens: 38863`, so the request shape survives a faithful proxy;
confirm LiteLLM's native route does the same.

### 4. Decide: adopt / hybrid / build

| Outcome | When |
|---|---|
| **Adopt LiteLLM** | If the passthrough route forwards `anthropic-beta` and still logs. |
| **Hybrid** | LiteLLM for virtual keys, budgets, and UI; a thin byte-level tap in the data path for faithful capture (streaming capture through the callback is normalised, not byte-exact). |
| **Build** | If neither LiteLLM route is both faithful and logged. The thin tap in `../capture-tap/` already demonstrates this path on real traffic. |

### 5. Not started (later roadmap, out of scope here)

Routing / cheapest-model selection, provider switching to Bedrock or Vertex,
multi-machine rollout (TLS, DNS, managed settings), and the policy questions
(who funds tokens; where the capture store lives, who reads it, retention).

---

## Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | LiteLLM proxy + Postgres |
| `docker-compose.stub.yml` | Overlay pointing the proxy at the local stub (no key, no cost) |
| `config.yaml` | Real config — models point at Anthropic |
| `config.stub.yaml` | Stub config — models point at the local stub |
| `custom_capture.py` | The capture callback (writes plain JSON) |
| `verify.py` | Scorecard over the capture directory |
| `run_checks.py` | 16-point fidelity harness driven with Claude-Code-shaped requests |
| `stub/stub_upstream.py` | Fake Anthropic upstream that records what it received and can misbehave on cue |
| `STUB.md` | How to run the no-key checks |
| `FINDINGS.md` | Full results and the `anthropic-beta` root-cause analysis |
| `capture/` | Captured exchanges (`<session>/<ts>.request.json`, `.response.json`) + `index.jsonl` |

---

## Run it (no key, no cost)

```bash
# 1. stub upstream
cd stub && STUB_SILENCE_SECONDS=310 uv run --with fastapi --with "uvicorn[standard]" \
    uvicorn stub_upstream:app --host 0.0.0.0 --port 8080

# 2. proxy + postgres, pointed at the stub
export ANTHROPIC_API_KEY="stub-not-a-real-key" LITELLM_MASTER_KEY="sk-master-local-only"
docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d

# 3. mint a virtual key
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-master-local-only" -H "Content-Type: application/json" \
  -d '{"models":["claude-haiku-4-5"],"max_budget":25,"metadata":{"developer":"asif.hussain"}}'

# 4. run the checks (drop --slow to skip the 5-minute silence test)
uv run --with httpx run_checks.py --gateway http://localhost:4000 --key <virtual-key> --slow

# 5. read the scorecard over what was captured
python3 verify.py
```

To run against **real** Claude Code and a real key, use `config.yaml` instead of
the stub overlay, export a real `ANTHROPIC_API_KEY`, and point the CLI at
`http://localhost:4000` with the virtual key as `ANTHROPIC_AUTH_TOKEN`.
