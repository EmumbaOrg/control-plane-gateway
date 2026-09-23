# Setting up a LiteLLM capture gateway from scratch

A self-contained guide. No repository to clone, nothing to download except Docker
images — you create four small files yourself and paste them in. Written for
someone who has never used LiteLLM. About 20 minutes, most of it Docker pulling
images.

---

## What you are building, in plain words

Claude Code normally talks straight to Anthropic, using a key stored on the
laptop. Nobody at the company can see what was sent, there is no spending limit,
and if that key leaks it has to be replaced on every machine.

**LiteLLM** is an open-source program that speaks the *exact same API language* as
Anthropic. That single fact is what makes this work: you can put it in the middle
and Claude Code cannot tell the difference — you only tell Claude Code a different
address. Nothing is patched, no certificates, no reverse engineering.

Because every request now passes through a program you run, you can record it,
price it, attribute it to a person, and refuse it.

You will end up with two containers running:

| Container | Port | Job |
|---|---|---|
| LiteLLM proxy | 4000 | Receives Claude Code's requests, checks them, forwards them to Anthropic |
| PostgreSQL | 5432 | Stores developer keys, their budgets, and what each has spent |

### The three keys — this is most of the learning

| Key | Who holds it | Where it comes from |
|---|---|---|
| `ANTHROPIC_API_KEY` | The gateway machine only | Anthropic issues it. This is the one that spends real money |
| `LITELLM_MASTER_KEY` | You, the administrator | **You invent it.** It is the root password of your own gateway |
| Virtual key (`sk-…`) | Each developer | Your gateway mints one per person, in step 8 |

The whole security idea in one sentence: **developers only ever hold a virtual
key, Anthropic only ever sees the real key, and the two never meet on a laptop.**
Removing someone's access is deleting one database row — no key rotation, no
redeployment.

---

## Prerequisites

- **Docker Desktop** — https://www.docker.com/products/docker-desktop. Install it,
  launch it, wait until its icon reports running.
- **An Anthropic API key** — https://platform.claude.com → *API keys* → create.
  It starts with `sk-ant-`. One key serves the whole team.
- **Claude Code** installed on whichever machine will use the gateway.

Confirm Docker is ready:

```bash
docker compose version
```

A version number means you are good. "command not found" means Docker Desktop
isn't running or hasn't finished installing.

---

## Step 1 — Make a working folder

```bash
mkdir -p ~/litellm-gateway/capture && cd ~/litellm-gateway
```

Every command from here runs in this folder. You will create four files in it:
`.env`, `config.yaml`, `docker-compose.yml`, and `capture.py`.

## Step 2 — Invent the master key

Nobody issues this one. It is the administrator password for your own gateway, so
you generate it:

```bash
openssl rand -hex 32 | sed 's/^/sk-/'
```

Copy the output somewhere safe. LiteLLM only requires that it start with `sk-`.
Never use an example value from documentation — this key mints developer keys,
revokes them, logs into the dashboard, and can read every captured prompt.

Generate a second random value the same way, for `LITELLM_SALT_KEY`. It encrypts
whatever the proxy stores in the database. Set it before the first boot and never
change it afterwards.

## Step 3 — Create `.env`

Three lines, with your own values:

```text
ANTHROPIC_API_KEY=sk-ant-…your real Anthropic key…
LITELLM_MASTER_KEY=sk-…the value from step 2…
LITELLM_SALT_KEY=sk-…the second random value…
```

`docker compose` picks this file up automatically because of its name and
location. If you ever put this folder in git, add `.env` to `.gitignore` first.

## Step 4 — Create `config.yaml`

This is the proxy's own configuration. It answers three questions: which models
exist, where the admin password and database are, and what to do after each call.

```yaml
model_list:
  - model_name: claude-haiku-4-5-20251001
    litellm_params:
      model: anthropic/claude-haiku-4-5-20251001
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: claude-sonnet-5
    litellm_params:
      model: anthropic/claude-sonnet-5
      api_key: os.environ/ANTHROPIC_API_KEY

  - model_name: claude-opus-5
    litellm_params:
      model: anthropic/claude-opus-5
      api_key: os.environ/ANTHROPIC_API_KEY

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: os.environ/DATABASE_URL
  store_prompts_in_spend_logs: true

litellm_settings:
  drop_params: false
  callbacks: capture.handler
```

Four things in there are worth understanding rather than just pasting:

- **`model_list` is an allow-list.** A client can only ask for a model named here.
  Anything else is refused before it reaches Anthropic. Claude Code sometimes
  sends a bare alias (`claude-haiku-4-5`) and sometimes a dated ID
  (`claude-haiku-4-5-20251001`) depending on its version, so list every ID you
  expect it to ask for. Only add IDs you know exist — a wrong dated ID fails at
  request time and reads to the user as "that model is broken".
- **`drop_params: false`** tells LiteLLM never to silently discard request fields
  it doesn't recognise. For a gateway whose job is faithful recording, silent
  dropping is the worst possible failure mode.
- **`store_prompts_in_spend_logs: true`** keeps prompts in the proxy's own
  database tables, alongside the file capture in step 5.
- **`callbacks: capture.handler`** points at the file you write next. This is the
  line that turns a plain proxy into a *capture* gateway.

## Step 5 — Create `capture.py`

LiteLLM calls this after every exchange, successful or failed. It writes one
request file and one response file per call, plus an index line.

```python
"""Minimal capture callback for a LiteLLM proxy."""
import json, os, time
from pathlib import Path

from litellm.integrations.custom_logger import CustomLogger

STORE = Path(os.getenv("CAPTURE_DIR", "/app/capture"))


def _jsonable(obj):
    """Best-effort conversion of LiteLLM objects into plain JSON types."""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    for attr in ("model_dump", "dict"):
        if hasattr(obj, attr):
            try:
                return _jsonable(getattr(obj, attr)())
            except Exception:
                pass
    return str(obj)


def _capture(kwargs, response_obj, ok):
    # The raw body and headers Claude Code sent, exactly as the proxy received them.
    proxy_request = (kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}
    body = proxy_request.get("body") or {}
    headers = proxy_request.get("headers") or {}

    session = headers.get("x-claude-code-session-id") or "no-session-id"
    folder = STORE / session
    folder.mkdir(parents=True, exist_ok=True)

    seq = f"{len(list(folder.glob('*.request.json'))) + 1:03d}"

    (folder / f"{seq}.request.json").write_text(
        json.dumps({"body": _jsonable(body), "headers": _jsonable(headers)}, indent=2)
    )
    (folder / f"{seq}.response.json").write_text(
        json.dumps({"ok": ok, "response": _jsonable(response_obj)}, indent=2)
    )

    with (STORE / "index.jsonl").open("a") as fh:
        fh.write(json.dumps({
            "ts": time.time(), "ok": ok,
            "session": session, "model": body.get("model"),
        }) + "\n")


class Capture(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, False)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, True)

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _capture(kwargs, response_obj, False)


handler = Capture()
```

The important line is `proxy_server_request` — that is where LiteLLM keeps the
body and headers **as Claude Code sent them**, before any normalisation. Anything
else you might log has already been reshaped.

If you only care about spend tracking and not about capture files, you can skip
this file entirely: delete the `callbacks:` line from `config.yaml` and rely on
`store_prompts_in_spend_logs` plus the dashboard.

## Step 6 — Create `docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: litellm
      POSTGRES_PASSWORD: litellm
      POSTGRES_DB: litellm
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U litellm"]
      interval: 5s
      timeout: 3s
      retries: 12

  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "4000:4000"
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY in .env}
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY in .env}
      LITELLM_SALT_KEY: ${LITELLM_SALT_KEY:?set LITELLM_SALT_KEY in .env}
      DATABASE_URL: postgresql://litellm:litellm@postgres:5432/litellm
      CAPTURE_DIR: /app/capture
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./capture.py:/app/capture.py:ro
      - ./capture:/app/capture
    command: ["--config", "/app/config.yaml"]

volumes:
  pgdata:
```

Notes: the `:?` syntax makes the container refuse to start with a clear message
if a secret is missing, rather than failing mysteriously later. `config.yaml` and
`capture.py` are mounted read-only; `capture/` is writable so the callback can
write into it. PostgreSQL is deliberately **not** published to the host — nothing
outside Docker needs to reach it.

## Step 7 — Start it

```bash
docker compose up -d
```

> **This is the from-scratch path, and `docker compose up -d` is correct for it.**
> What you have built here is Anthropic-only: no host-side Ollama, so there is
> nothing for compose to miss. If you are instead working from the
> `control-plane-gateway` repository, use `./gateway-up.sh` — that stack has local
> model routes backed by an Ollama running on the **host**, and plain `docker
> compose up -d` leaves them broken in a way that looks like a hang. See
> [`DEMO.md`](DEMO.md) §5.1.

The first run downloads both images; allow a few minutes. Then:

```bash
docker compose ps
```

You want both `litellm` and `postgres` showing `Up`.

## Step 8 — Confirm it is healthy

```bash
curl http://localhost:4000/health/liveliness
```

Expected: `"I'm alive!"` — the proxy is running.

```bash
curl http://localhost:4000/health/readiness
```

Expected: `{"status":"healthy","db":"connected"}` — this also proves the proxy
reached PostgreSQL, which is the usual thing to get wrong.

If either fails:

```bash
docker compose logs litellm --tail 50
```

## Step 9 — Mint a virtual key for a developer

The developer never receives the Anthropic key. They receive a virtual key that
works only through this gateway, only for the models you allow, and only up to the
budget you set.

Load the master key into your shell:

```bash
export LITELLM_MASTER_KEY="sk-…the value from step 2…"
```

Mint the key:

```bash
curl -s -X POST http://localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" -d '{"models":["claude-haiku-4-5-20251001"],"max_budget":5,"metadata":{"developer":"firstname.lastname"}}'
```

The reply contains a `"key"` field starting with `sk-`. **That string is what you
hand to the developer.** Store it safely — the database keeps only a SHA-256 hash
of it, so the gateway can never show it to you again.

| Field | Effect |
|---|---|
| `models` | The key is refused (403) for any model not in this list |
| `max_budget` | Once recorded spend passes $5, the next request is refused (429) |
| `metadata.developer` | Spend becomes attributable to a named person, not a pool |

To revoke someone later, delete their key — one row, no rotation, no
redeployment:

```bash
curl -s -X POST http://localhost:4000/key/delete -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" -d '{"keys":["sk-…the virtual key…"]}'
```

## Step 10 — Point Claude Code at the gateway

Two environment variables, in the developer's terminal:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
```

```bash
export ANTHROPIC_AUTH_TOKEN="sk-…the virtual key from step 9…"
```

```bash
unset ANTHROPIC_API_KEY
```

That third line is not optional. If a real Anthropic key is still in the
environment, Claude Code may use it and bypass the gateway entirely — you would
see no captures and wonder why.

Start Claude Code and check:

```bash
claude
```

Inside it, run `/status`. You want a base URL line naming
`http://localhost:4000`. If it still names Anthropic, the variables did not reach
the process.

**If the gateway runs on a different machine**, replace `localhost` with that
machine's hostname or IP, and publish the port there. Before doing that, read the
security note at the end — a gateway on a shared network needs more than this
guide sets up.

**For the Claude desktop app:** a GUI application never sees shell exports. Put
the same two values in `~/.claude/settings.json` under an `env` block, then fully
quit and reopen the app:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_AUTH_TOKEN": "sk-…the virtual key…"
  }
}
```

## Step 11 — Prove that capture is working

Ask Claude Code any question, let it finish, then look:

```bash
ls -lt capture/
```

A folder named after the session appears. Inside:

```bash
ls capture/*/
```

Paired files: `001.request.json` holds what Claude Code sent, `001.response.json`
what came back. And:

```bash
cat capture/index.jsonl
```

Two things surprise everybody at this point:

- **One typed question is not one API call.** A single question typically produces
  several billed calls — a title generation, a tool-use decision, the answer, a
  closing recap. Estimating cost from "how many questions did we ask" understates
  it by roughly 4×. The index file makes this visible immediately.
- **These files hold far more than the conversation.** They contain the paths of
  every file Claude Code opened, the shell commands it ran, **the contents and
  command output fed back to the model**, its internal reasoning, and headers that
  fingerprint the machine (OS, CPU, runtime version). Treat the capture directory
  as confidential data, not as logs. Decide retention and who may read it *before*
  this serves more than one person.

## Step 12 — The dashboard

Open http://localhost:4000/ui and sign in as user `admin` with the master key from
step 2. It shows each virtual key with spend against budget, per-request usage
logs, and the registered models.

---

## Everyday commands

```bash
docker compose logs litellm -f
```

```bash
docker compose restart litellm
```

```bash
docker compose down
```

`down` stops both containers but keeps the database, so keys and spend survive a
restart. Adding `-v` deletes the database volume too — including every virtual key
you ever issued. Don't reach for it casually.

---

## If something goes wrong

| Symptom | Cause and fix |
|---|---|
| `set ANTHROPIC_API_KEY in .env` at startup | `.env` missing, misnamed, or not in the same folder as `docker-compose.yml` |
| Container exits immediately | `docker compose logs litellm` — nearly always a YAML typo or an unknown config key. If it complains about an unrecognised setting, comment that line out and restart |
| `401 Authentication Error` on every call | Wrong virtual key, or `ANTHROPIC_AUTH_TOKEN` not actually exported in that shell |
| `403 key not allowed to access model` | Working as designed — the model isn't in that key's `models` list. Mint a key with a wider list |
| `429 Budget has been exceeded` | Working as designed — raise `max_budget` or issue a new key |
| Calls succeed but `capture/` stays empty | The callback didn't load. Grep the proxy log for `capture` — if the release wants a list, change `config.yaml` to `callbacks: ["capture.handler"]` |
| `not_found_error` for a model | That model ID doesn't exist at Anthropic. Fix the ID in `model_list` |
| `/status` still shows Anthropic's URL | Variables not exported in that shell, or the desktop app wasn't fully quit and reopened |

---

## Two honest caveats

**`max_budget` is a brake, not a hard cap.** The proxy cannot know what a request
costs until Anthropic has answered it, so the request that crosses the ceiling
still completes and is still billed — only the *next* one is refused. Concurrent
requests can each pass the check before any of them records its spend. Describe it
as "stops the next request", never as a guaranteed limit.

Also, the spend figure is LiteLLM's own arithmetic — tokens × its internal price
table — not an invoice from Anthropic. If you add a model LiteLLM has no pricing
for, cost computes as zero, spend never accrues, and the budget never trips. Check
pricing whenever you add a model.

**This setup is a single-machine proof of concept.** As written it is plain HTTP,
bound to localhost, with a PostgreSQL password of `litellm`. Before it serves more
than one person: put it behind TLS, change the database password, keep the DB
unpublished, restrict who can reach port 4000, and settle retention and access
rules for the capture directory — it holds source code and command output.
