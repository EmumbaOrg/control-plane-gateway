# control-plane-gateway

Proof of concept for an enterprise control plane over Claude Code: route developer
traffic through a gateway we operate, so that requests and responses are captured,
spend is attributable per developer, and the provider API key never leaves the
server.

## What is here

| Path | Purpose |
|---|---|
| `litellm-gateway/` | LiteLLM proxy + PostgreSQL, with a custom capture callback |
| `plugins/emumba-react/` | Skills plugin distributed to developers through the gateway |
| `capture-tap/` | A thin byte-level tap in the data path — the alternative approach |
| `docs/` | Everything below |

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
| [`FINDINGS.md`](docs/FINDINGS.md) | Fidelity results (15 of 16 checks) and the `anthropic-beta` root-cause analysis |
| [`STUB.md`](docs/STUB.md) | Running the fidelity checks with no API key and no spend |

## Running it

Create `litellm-gateway/.env` with two variables:

```
ANTHROPIC_API_KEY=sk-ant-...      # real provider key — stays on the gateway host
LITELLM_MASTER_KEY=...            # admin password for the proxy and its UI
```

Then:

```bash
cd litellm-gateway
docker compose up -d
```

Issue a developer a virtual key, scoped to a model and a budget:

```bash
curl -s -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models":["claude-haiku-4-5-20251001"],"max_budget":5,
       "metadata":{"developer":"your.name"}}'
```

Point Claude Code at the gateway:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="sk-...the virtual key..."
unset ANTHROPIC_API_KEY
claude
```

Full instructions, including the desktop app, are in [`docs/DEMO.md`](docs/DEMO.md).

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
