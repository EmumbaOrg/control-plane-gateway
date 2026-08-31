# Phase 1 — no-key fidelity checks

Tests whether LiteLLM can carry Claude Code's traffic faithfully, **without an
API key, without spending anything, and without running Claude Code.**

The method is a two-run comparison:

| Run | Path | Purpose |
|---|---|---|
| **Baseline** | checker → stub | Proves the harness itself is correct. Everything should pass except the capture check, since there is no gateway to capture anything. |
| **Through gateway** | checker → LiteLLM → stub | The real test. Any check that passed in baseline and fails here was broken **by LiteLLM**. |

That comparison is the whole point: it removes "maybe our test is wrong" from the
argument. A failure in run 2 that passed in run 1 is attributable.

## Baseline result (recorded 18 Aug 2026)

Run against the stub directly, `STUB_SILENCE_SECONDS=8` for speed:

```
PASS   1. Streamed exchange completes
PASS   2. Ping relayed to client
PASS   3. Request reached upstream
PASS   4. Unknown anthropic-beta forwarded
PASS   5. system array preserved as a list
PASS   6. Attribution block still first
PASS   7. Tool definitions forwarded
PASS   8. tool_result content forwarded
PASS   9. Gateway swapped the credential
PASS  10. x-claude-code-session-id forwarded
PASS  11. Upstream 400 body unmodified
PASS  12. cache_read_input_tokens propagated
PASS  13. count_tokens served
FAIL  14. Capture callback fired          <- expected: no gateway in the path
SKIP  15. Capture has the raw request body
PASS  16. Survives a 310s silent gap
```

15 of 16 as designed. The harness is trustworthy.

## Run it

### 1. Start the stub

```bash
cd stub
STUB_SILENCE_SECONDS=310 uv run --with fastapi --with "uvicorn[standard]" \
    uvicorn stub_upstream:app --port 8080
```

Use `310` for the real silence test. Lower it to `8` while iterating.

### 2. Baseline

```bash
uv run --with httpx run_checks.py \
    --gateway http://localhost:8080 --key not-used-by-stub --slow
```

Expect the result above. If anything else fails, fix the harness before
blaming the gateway.

### 3. Through LiteLLM

```bash
export LITELLM_MASTER_KEY="sk-master-local-only"
docker compose -f docker-compose.yml -f docker-compose.stub.yml up
```

Mint a virtual key (see README), then:

```bash
curl -s -X POST http://localhost:8080/_stub/reset    # clear the record
uv run --with httpx run_checks.py \
    --gateway http://localhost:4000 --key sk-…virtual-key… --slow
```

Repeat with `--gateway http://localhost:4000/anthropic` to test the passthrough
route. **Record both.** Which route satisfies more checks is the central finding.

## What each check tells you

| # | If it fails |
|---|---|
| 2 | LiteLLM reconstructs the stream instead of relaying it. Combined with 16, this means long thinking pauses abort. **No config fixes this.** |
| 4 | Beta headers are stripped — the setup breaks on a future Claude Code release. |
| 5, 6 | The `system` array is reshaped. **Prompt caching is defeated and input cost is roughly 10× higher, silently.** |
| 7, 8 | Tool definitions or tool results are dropped. Tool results carry the file contents and command output — most of the value of capturing at all. |
| 11 | Errors are re-wrapped, so the client's retry-and-degrade logic stops working. |
| 13 | `count_tokens` is unserved, so Claude Code counts context with **paid inference calls**. |
| 14 | This route isn't logged. If the faithful route is the unlogged one, the answer is a hybrid. |
| 15 | Capture holds LiteLLM's normalised view, not what Claude Code actually sent. |
| **16** | **Go/no-go.** Streams die after five minutes of thinking. |

## Stub modes

Triggered by an `x-stub-mode` header **or** a `STUB_MODE=<mode>` marker anywhere
in the request body. The body marker is the reliable one — a gateway may drop
unknown headers, which is itself something worth detecting.

| Mode | Behaviour |
|---|---|
| `normal` | Text response, streamed, with a ping |
| `thinking` | `thinking` + `tool_use` + `text` blocks |
| `long_silence` | Ping, then `STUB_SILENCE_SECONDS` of nothing, then finish |
| `error` | `400` with an Anthropic-shaped error body |
| `cached` | Usage reports `cache_read_input_tokens: 45000` |

## Inspecting what the stub received

```bash
curl -s http://localhost:8080/_stub/received | python3 -m json.tool | less
ls stub/received/
```

Each record has an `observed` block summarising the things we care about:
which beta header arrived, whether `system` is still a list, what its first
block is, how many tools came through, and which credential header the gateway
used upstream.

## What Phase 1 cannot tell you

- **Real prompt-cache behaviour.** The stub can report cache fields, but only
  Anthropic decides whether a prefix actually cached. Needs a real key.
- **Real Claude Code payloads.** `run_checks.py` sends a hand-built request
  shaped like Claude Code's; the genuine article is far larger and changes with
  each release. Once you have one real captured request, replay it here.
- **The demo.** The deliverable is Claude Code connected to a gateway that
  captures its messages — that has to be shown with Claude Code running.
