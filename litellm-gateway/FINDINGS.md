# Phase 1 findings — LiteLLM as the capture gateway

**Tested 18 Aug 2026** against `ghcr.io/berriai/litellm:main-latest`, Claude-Code-shaped
requests, stub upstream, no API key, no cost.

## Verdict

**LiteLLM is viable for message capture. 15 of 16 checks pass.** One real gap, described
below, needs a decision.

```
PASS   1. Streamed exchange completes
PASS   2. Ping relayed to client
PASS   3. Request reached upstream
FAIL   4. Unknown anthropic-beta forwarded          <-- the one gap
PASS   5. system array preserved as a list
PASS   6. Attribution block still first
PASS   7. Tool definitions forwarded
PASS   8. tool_result content forwarded
PASS   9. Gateway swapped the credential
PASS  10. x-claude-code-* reach the capture layer
PASS  11. Upstream 400 body unmodified
PASS  12. cache_read_input_tokens propagated
PASS  13. count_tokens served
PASS  14. Capture callback fired
PASS  15. Capture has the raw request body
PASS  16. Survives a 310s silent gap                <-- the go/no-go, passed
```

## What passed that could have killed it

**Streams survive a 310-second silent gap (check 16).** This was the go/no-go. LiteLLM
relays keep-alive pings rather than swallowing them, so long thinking pauses do not abort
the session. Nothing in configuration could have fixed a failure here.

**Prompt caching is not defeated (checks 5, 6).** The `system` array arrives upstream still
a list, with the attribution block first and unmerged. Had LiteLLM collapsed it to a string
or reordered it, input cost would have risen roughly tenfold with no error message.

**Tool results survive (check 8).** The file contents inside a `tool_result` block reach both
the provider and the capture store. That content is most of the value of capturing at all.

**Upstream error bodies pass through unmodified (check 11).** Claude Code's retry-and-degrade
logic string-matches on the provider's error wording, and it still works.

## The one gap: client `anthropic-beta` values are dropped

On the `/v1/messages` route, a beta capability value sent by the client **does not reach the
provider**. Verified: the stub saw `anthropic-beta: None` for a request that carried
`anthropic-beta: client-sent-2027-01-01`.

### Why it happens

Read from the installed source in the container:

- `llms/custom_httpx/llm_http_handler.py` builds the outbound headers from three sources —
  `forwarded_headers` (which is `kwargs.get("headers")`), `extra_headers` from kwargs, and
  provider-specific headers.
- `llms/anthropic/experimental_pass_through/messages/transformation.py` →
  `_update_headers_with_anthropic_beta()` **preserves** any existing `anthropic-beta` value
  and merges auto-detected ones into it.

So the plumbing to forward the header exists and would work — **nothing on the proxy layer
populates `kwargs["headers"]` from the incoming client request on this route.**
`forward_client_headers_to_llm_api` is referenced in `litellm/responses/utils.py`, i.e. it is
wired for the Responses API path, not for `anthropic_messages`.

### Workarounds tested, all ineffective

| Attempt | Result |
|---|---|
| `litellm_settings: forward_client_headers_to_llm_api: true` | No effect on this route |
| `general_settings: forward_client_headers_to_llm_api: true` | No effect on this route |
| `extra_headers` in the request body | No effect |
| `extra_headers` in `litellm_params` per model | No effect |

### Why it matters, precisely

LiteLLM **auto-injects** beta headers for capabilities it recognises — context management,
structured outputs, fast mode, advisor tool, tool search, compaction. So known features keep
working. What is lost is any beta value LiteLLM does not know about, which is exactly what a
new Claude Code release ships.

The failure mode is not graceful. Anthropic pairs a capability's beta header with body
fields. With `drop_params: false` (which we must keep, or capabilities vanish silently) the
body field is forwarded while its header is stripped — and a mismatched pair produces a hard
`400`, not a quiet degradation.

## The passthrough route is untested

`/anthropic/*` returned `401` and the stub received nothing: the passthrough route ignores
`api_base`, so it went to the real API with a fake key. **Evaluating the passthrough route
requires a real API key.** It may well forward headers verbatim — that is the obvious next
test once a key exists, and it may make this whole gap moot.

## Two things worth knowing about the setup

**The Docker image is the right way to run this.** Installing `litellm[proxy]` with `uv`
failed on a FastAPI incompatibility (`cannot import name 'get_flat_dependant'`). The
container has pinned dependencies and started first time.

**Capture works, but the payload is nested deeper than documented examples suggest.** On this
route `proxy_server_request` is **not** a top-level callback kwarg — it lives at
`kwargs["litellm_params"]["proxy_server_request"]`, carrying url, method, headers and body.
The client headers are also mirrored at
`standard_logging_object.metadata.requester_custom_headers`, and the outbound body at
`additional_args.complete_input_dict`. `custom_capture.py` checks all of these in order of
fidelity.

**Streaming capture is normalised, not byte-faithful.** For streamed requests the callback
receives LiteLLM's reassembled response object, not the provider's raw SSE bytes. That is
inherent to callback-based capture. If a byte-exact record is required, it needs a tap in the
data path rather than a callback beside it.

## Options

| Option | What it means |
|---|---|
| **Adopt, monitor the gap** | Ship LiteLLM. Accept that unrecognised betas are stripped, and watch for `400`s after each Claude Code release. Cheapest path; carries a known recurring risk. |
| **Test the passthrough route** | Needs an API key. If `/anthropic/*` forwards headers verbatim and still logs, the gap disappears. **Do this before anything else.** |
| **Upstream fix** | The plumbing exists; populating `kwargs["headers"]` from the client request on this route is a small change. File an issue, or contribute the patch. |
| **Hybrid** | LiteLLM for keys, budgets and the admin UI; a thin byte-level tap in the data path for faithful capture. Solves both this gap and the streaming-fidelity caveat. |

## Reproducing

See `STUB.md`. Roughly:

```bash
cd stub && STUB_SILENCE_SECONDS=310 uv run --with fastapi --with "uvicorn[standard]" \
    uvicorn stub_upstream:app --host 0.0.0.0 --port 8080

export ANTHROPIC_API_KEY="stub-not-a-real-key" LITELLM_MASTER_KEY="sk-master-local-only"
docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d

uv run --with httpx run_checks.py --gateway http://localhost:4000 --key "$(cat .vkey)" --slow
```

Drop `--slow` to skip the five-minute silence test; use `STUB_SILENCE_SECONDS=8` while
iterating.
