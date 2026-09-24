# control-plane-gateway

Proof of concept for an enterprise control plane over Claude Code: route developer
traffic through a gateway we operate, so that requests and responses are captured,
spend is attributable per developer, and the provider API key never leaves the
server.

## What is here

| Path | Purpose |
|---|---|
| `litellm-gateway/` | LiteLLM proxy + PostgreSQL, with a custom capture callback |
| `plugins/emumba-react/` | Gateway-side copy of the React / Next.js skills plugin — source: [emumba-skills-react](https://github.com/asif-emumba/emumba-skills-react) |
| `plugins/emumba-backend/` | Gateway-side copy of the backend skills plugin — source: [emumba-skills-backend](https://github.com/asif-emumba/emumba-skills-backend) |
| `capture-tap/` | A thin byte-level tap in the data path — the alternative approach |
| `docs/` | Everything below |

Each skills plugin reaches the model by **two paths**: developers install the
plugin from the gateway's marketplace, which clones it from its own repository
(below), *and* the gateway injects the matching `SKILL.md` server-side, from the
copy under `plugins/`, when a request matches a trigger. See
[`docs/GATEWAY-MODIFY.md`](docs/GATEWAY-MODIFY.md).

## Skills repositories

Each skills plugin has its own public repository. Those repositories are where
skills are authored, and they are what the gateway's marketplace clones when a
developer installs a plugin:

| Plugin | Repository | Skills |
|---|---|---|
| `emumba-react` | <https://github.com/asif-emumba/emumba-skills-react> | React / Next.js |
| `emumba-backend` | <https://github.com/asif-emumba/emumba-skills-backend> | REST conventions, Spring Boot, Node/Express |

**To add or change a skill**, open a change in the repository for that plugin,
not in this one:

1. Add `skills/<skill-name>/SKILL.md` (or edit an existing one) and bump
   `version` in `.claude-plugin/plugin.json`.
2. Merge to the default branch. The marketplace source has no branch or ref
   field, so that is the only branch developers receive.
3. Developers pick it up with `claude plugin marketplace update litellm`
   followed by `claude plugin update <plugin>@litellm`.

A skill for a new area (a new guild or stack) gets a new plugin in its own
repository, registered in the gateway's marketplace, rather than being added to
an existing one.

> The `plugins/` folder in this repo is **not** the source. It is the copy the
> gateway injects from server-side, and nothing syncs it with the repositories
> above. After a skill changes upstream, copy it here too, or the injected and
> installed versions drift. See
> [`plugins/emumba-react/README.md`](plugins/emumba-react/README.md#where-the-registered-source-actually-points).

Start with [`docs/GATEWAY-OVERVIEW.md`](docs/GATEWAY-OVERVIEW.md) — what was built
and why, in diagrams. Then [`docs/DEMO.md`](docs/DEMO.md) for configuration, usage,
and a walkthrough of a real captured session.

| Document | What it covers |
|---|---|
| [`GATEWAY-OVERVIEW.md`](docs/GATEWAY-OVERVIEW.md) | The whole design in seven sections, with flow diagrams |
| [`DEMO.md`](docs/DEMO.md) | Configuration, usage, and a real captured session analysed |
| [`SETUP.md`](docs/SETUP.md) | Building the same gateway from scratch, no repository needed |
| [`PROJECT-STATUS.md`](docs/PROJECT-STATUS.md) | Status report — action items, what is proven, what is open |
| [`GATEWAY-MODIFY.md`](docs/GATEWAY-MODIFY.md) | Modifying requests in flight: output clamp, secret redaction, skill injection |
| [`NON-ANTHROPIC-MODELS.md`](docs/NON-ANTHROPIC-MODELS.md) | Running Gemini / DeepSeek / GLM in Claude Code through this gateway |
| [`limited_budget_per_dev.md`](docs/limited_budget_per_dev.md) | Setting a per-developer budget, and proving it stops spend |
| [`OLLAMA-TRANSLATION.md`](docs/OLLAMA-TRANSLATION.md) | How an Anthropic-shaped request is translated for a local Ollama model |
| [`FINDINGS.md`](docs/FINDINGS.md) | Fidelity results (15 of 16 checks) and the `anthropic-beta` root-cause analysis |
| [`STUB.md`](docs/STUB.md) | Running the fidelity checks with no API key and no spend |

`litellm-gateway/` has its own [`README.md`](litellm-gateway/README.md) covering
the files in that directory, the boot troubleshooting, and the verification
scripts.

## Running it

Everything below runs from `litellm-gateway/`, which is where the stack, the
scripts and both key files live:

```bash
cd litellm-gateway
```

### 0. Prerequisites

- **Docker Desktop**, running. The scripts check and say so if it is not.
- An **Anthropic API key** from <https://platform.claude.com> with credit on it.
  This is a developer-platform account — **not** a claude.ai subscription, and
  upgrading claude.ai to Pro does not produce one.
- The **Claude Code CLI**: `npm i -g @anthropic-ai/claude-code`.
- **Only if you want the local private-model routes**: `ollama`, plus the model
  pulled once (~5 GB). `gateway-up.sh` starts and configures Ollama itself, but
  it cannot install it or pull for you:
  ```bash
  brew install ollama && ollama pull qwen3:8b
  ```
  Skip this and use `./gateway-up.sh --no-local` for a hosted-models-only run.

### 1. Server configuration — `.env`

Create `litellm-gateway/.env`. Two variables are required; the stack refuses to
start without them.

```
ANTHROPIC_API_KEY=sk-ant-...      # real provider key — stays on the gateway host
LITELLM_MASTER_KEY=sk-...         # admin password for the proxy and its UI
```

`LITELLM_MASTER_KEY` is not issued by anyone — choose a strong string. You need
it to mint virtual keys and to sign in to the dashboard.

Every other variable is **optional**, and an Anthropic-only setup boots fine
with all of them absent. Add one only for the routes you want: `OPENROUTER_API_KEY`
(the free-tier aggregator routes), `GROQ_API_KEY`, `OPENAI_API_KEY` (first-party,
limited credit), `ZAI_API_KEY`, `OLLAMA_API_BASE`, and the `GATEWAY_*` behaviour
knobs. `docker-compose.yml` is the authoritative list and documents each one
next to the code that reads it.

> `.env` is the **server's** boot config — the keys the gateway spends against.
> It is not how you authenticate as a caller; that is `.vkey`, in step 3. Both
> are gitignored, and neither may be committed.

### 2. Start the stack

```bash
./gateway-up.sh
```

**Use this rather than `docker compose up -d`, which is not equivalent.** Ollama
runs on the host, not in compose — a Linux container on macOS has no Metal, so a
containerised Ollama drops to CPU and an 8B model becomes unusable. The script
brings up compose *and* Ollama with the environment it needs, then smoke-tests
both local aliases so a broken prerequisite surfaces here instead of as a hung
Claude Code session.

```bash
./gateway-up.sh --status     # report without changing anything
./gateway-up.sh --no-local   # skip Ollama entirely
```

Nothing installs a background service, so run it again after a reboot.

> ⚠ **Never `docker compose down -v`.** The `-v` destroys the `pgdata` volume and
> with it every virtual key and all spend history. Plain `down` is safe.
>
> ⚠ `docker compose restart` reuses the container's baked-in environment, so an
> edited `.env` needs `up -d` — which is what `gateway-up.sh` runs.

### 3. Mint a developer key, and save it to `.vkey`

A virtual key is scoped to a set of models and a budget, and carries the
developer's name for attribution:

```bash
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models":["claude-haiku-4-5-20251001"],"max_budget":5,
       "metadata":{"developer":"your.name"}}'
```

Put the returned `key` (starting `sk-`) in `litellm-gateway/.vkey` — that file,
and only that file, is where the launcher reads it from. One active key at a
time, one place to look. Mint it once; it is not per-run.

### 4. Launch Claude Code

```bash
./claude-gw.sh                    # default model
./claude-gw.sh local-qwen3-8b     # any alias from config.yaml
./claude-gw.sh gemini-3.7-flash -p "hi"   # extra args pass through to claude
```

**Use the script rather than exporting the variables by hand.** Every failure in
this PoC so far has been a credential problem that surfaced as something else — a
placeholder pasted verbatim, an expired key, a variable name that did not exist
so the token was empty. Claude Code reports all of those as an opaque 401 and
then retries ten times. `claude-gw.sh` validates the key against the gateway
first and says what is actually wrong. The key is never printed, only a prefix
and a length.

For the **desktop app**, point it at `http://localhost:4001` — the picker shim,
which rewrites `GET /v1/models` so non-Anthropic routes appear in the model
picker. The CLI uses port 4000 directly.

Full instructions, the model catalogue and a walkthrough of a real captured
session are in [`docs/DEMO.md`](docs/DEMO.md).

### 5. Check it worked

```bash
ls capture/                       # one directory per session
python3 verify.py                 # fidelity scorecard over the capture store
python3 verify_modify.py          # request-modification scorecard
python3 verify_routing.py         # route-override and model-attribution scorecard
```

The dashboard is at <http://localhost:4000/ui>, signed in with
`LITELLM_MASTER_KEY`.

## Data handling

**Captured traffic is not committed to this repository, and must not be.**

Captures contain the full text of prompts along with file contents and command
output returned to the model in `tool_result` blocks. `.gitignore` excludes
`capture/`, `capture_archive/`, `export/`, and every `.env` / virtual-key file.

Before adding a new output directory, confirm it is ignored:

```bash
git status --porcelain --ignored | grep capture
```

Retention, access, and storage location for the capture store are open policy
questions — see the closing section of [`docs/DEMO.md`](docs/DEMO.md).

## Status

This is a proof of concept running on `localhost`. It is not hardened for shared
use: PostgreSQL is published to the host, there is no TLS, and secrets are read
from a local file. See [`docs/DEMO.md`](docs/DEMO.md) for the rollout checklist and the open
adopt / hybrid / build decision.
