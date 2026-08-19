# control-plane-gateway

Proof of concept for an enterprise control plane over Claude Code: route developer
traffic through a gateway we operate, so that requests and responses are captured,
spend is attributable per developer, and the provider API key never leaves the
server.

## What is here

| Path | Purpose |
|---|---|
| `litellm-gateway/` | LiteLLM proxy + PostgreSQL, with a custom capture callback |
| `capture-tap/` | A thin byte-level tap in the data path — the alternative approach |
| `Emumba_PoC_Enterprise_Control_Plane.html` | PoC overview |

Start with [`litellm-gateway/DEMO.md`](litellm-gateway/DEMO.md) — configuration,
flow diagrams, usage, and a walkthrough of a real captured session.

Supporting documents:

- [`litellm-gateway/FINDINGS.md`](litellm-gateway/FINDINGS.md) — fidelity test results (15 of 16 checks pass) and the `anthropic-beta` root-cause analysis
- [`litellm-gateway/LITELLM-FLOW.md`](litellm-gateway/LITELLM-FLOW.md) — status and what remains
- [`litellm-gateway/STUB.md`](litellm-gateway/STUB.md) — how to run the fidelity checks with no API key and no spend

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

Full instructions, including the desktop app, are in `DEMO.md`.

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
questions — see the closing section of `litellm-gateway/DEMO.md`.

## Status

This is a proof of concept running on `localhost`. It is not hardened for shared
use: PostgreSQL is published to the host, there is no TLS, and secrets are read
from a local file. See `DEMO.md` for the rollout checklist and the open
adopt / hybrid / build decision.
