# How a local Ollama request is translated: Anthropic in, Anthropic out

The question this answers: when Claude Code talks to a local model through this
gateway, **which format is on the wire at each hop** — Anthropic, OpenAI, or
something else?

**Short answer: all three, in sequence. Ollama never sees Anthropic format at
any point.** It has no Anthropic-compatible surface at all. Anthropic support on
this route is entirely LiteLLM's translation layer — three formats and two
conversions in each direction.

Everything below was measured on 3 Sep 2026 against `local-qwen3-8b`
(`ollama_chat/qwen3:8b`). Payloads are captured, not illustrative; the
reproduction commands are at the end.

---

## The flow

```
REQUEST — outbound
──────────────────────────────────────────────────────────────────────────────

  ┌────────────────┐          ┌──────────────────────┐          ┌──────────────┐
  │  Claude Code   │          │   LiteLLM  :4000     │          │ Ollama :11434│
  │                │          │                      │          │   (host)     │
  │ ANTHROPIC      │ Anthropic│ auth · budget ·      │  Ollama  │ OLLAMA       │
  │ MESSAGES       ├─────────►│ clamp · capture      ├─────────►│ NATIVE ONLY  │
  │                │          │      then translate  │  native  │              │
  │ POST           │          │ ① Anthropic→OpenAI   │          │ POST         │
  │ /v1/messages   │          │    (internal pivot)  │          │ /api/chat    │
  │                │          │ ② OpenAI→Ollama      │          │              │
  └────────────────┘          └──────────────────────┘          └──────┬───────┘
   CLI and desktop             the OpenAI form is                      │
   app alike                   never on the wire                       ▼
                                                                  llama-server
                                                                    qwen3:8b

RESPONSE — inbound, the same path in reverse
──────────────────────────────────────────────────────────────────────────────

  ┌──────────────┐          ┌──────────────────────┐          ┌────────────────┐
  │ Ollama       │          │   LiteLLM  :4000     │          │  Claude Code   │
  │              │  Ollama  │                      │ Anthropic│                │
  │ OLLAMA       ├─────────►│ ① Ollama→OpenAI      ├─────────►│ ANTHROPIC      │
  │ NATIVE JSON  │  native  │    chat.completion   │          │ MESSAGES       │
  │              │          │ ② OpenAI→Anthropic   │          │                │
  │ message.     │          │    content blocks    │          │ content[]      │
  │ content      │          │                      │          │ stop_reason    │
  │ done_reason  │          │ ① is what the        │          │                │
  │              │          │ capture layer records│          │ unmodified,    │
  └──────────────┘          └──────────────────────┘          │ unaware        │
                                                              └────────────────┘
```

The OpenAI form is a **pivot**, internal to LiteLLM. It is never sent anywhere.
That matters for reading the capture files: what `custom_capture.py` records is
the OpenAI-shaped `ModelResponse`, not what either end actually exchanged.

---

## Hop 1 — Claude Code → LiteLLM: Anthropic Messages

Claude Code is unmodified and unaware. Only `ANTHROPIC_BASE_URL` changes.

```json
POST /v1/messages
{
  "model": "local-qwen3-8b",
  "max_tokens": 24,
  "system": "Be terse.",
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "Reply with exactly: A"}]}
  ]
}
```

Note the Anthropic markers: a top-level `system` field, and `content` as an
array of **typed blocks** rather than a string.

## Hop 2 — inside LiteLLM: Anthropic → OpenAI → Ollama

Captured from LiteLLM's own callback kwargs
(`capture/_kwargs/1788417025957538969.kwargs.json`), i.e. the real internal
state on a real call:

```
custom_llm_provider : ollama_chat
api_base            : http://host.docker.internal:11434/api/chat
messages            : [{"role": "user", "content": "Reply with exactly: OK"}]
optional_params     : {"max_tokens": 32, "num_ctx": 32768, "think": false}
```

Three things to read off that:

- The typed `content[]` array has **collapsed to a plain string**. That is
  conversion ①.
- `num_ctx` and `think` are **Ollama-native parameters**. They do not exist in
  the OpenAI API. Their presence is the tell that conversion ② has happened.
- `api_base` ends in `/api/chat`, not `/v1/chat/completions`.

## Hop 3 — LiteLLM → Ollama: Ollama native

Ollama's own access log, confirming the endpoint:

```
[GIN] 2026/09/03 - 11:19:37 | 200 |  4.365589791s | POST "/api/chat"
[GIN] 2026/09/03 - 11:30:25 | 200 |  3.705603958s | POST "/api/chat"
```

`/api/chat` on every call — never `/v1/chat/completions`, never `/v1/messages`.

The literal native request body, and Ollama's literal native response:

```json
POST http://127.0.0.1:11434/api/chat
{
  "model": "qwen3:8b",
  "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
  "stream": false,
  "think": false,
  "options": {"num_ctx": 32768, "num_predict": 32}
}
```

```json
{
  "model": "qwen3:8b",
  "created_at": "2026-09-03T07:40:16.771744Z",
  "message": {"role": "assistant", "content": "OK"},
  "done": true,
  "done_reason": "stop",
  "prompt_eval_count": 21,
  "eval_count": 2
}
```

This is **not** OpenAI format. There is no `choices[]` array, no `usage` object,
no `finish_reason`, and the tuning knobs live under a nested `options` object.

## Hop 4 — back out: Ollama → OpenAI → Anthropic

LiteLLM's normalised `ModelResponse`, captured from the same kwargs dump:

```json
{
  "object": "chat.completion",
  "model": "ollama_chat/qwen3:8b",
  "choices": [
    {"finish_reason": "stop",
     "message": {"content": "OK", "role": "assistant", "tool_calls": null}}
  ],
  "usage": {"completion_tokens": 2, "prompt_tokens": 21, "total_tokens": 23}
}
```

And what Claude Code actually receives:

```json
{
  "type": "message",
  "role": "assistant",
  "model": "local-qwen3-8b",
  "content": [{"type": "text", "text": "A"}],
  "stop_reason": "end_turn",
  "usage": {"input_tokens": 29, "output_tokens": 2}
}
```

---

## Field mappings

### Request · Anthropic → OpenAI → Ollama

| Anthropic (client) | OpenAI (pivot) | Ollama (wire) |
| --- | --- | --- |
| `system` (top-level field) | leading `system` message | leading `system` message |
| `content: [{type:"text"}]` | `content: "…"` string | `content: "…"` string |
| `tools[].input_schema` | `tools[].function.parameters` | `tools[].function.parameters` |
| `tool_result` block | `role:"tool"` message | `role:"tool"` message |
| `max_tokens` | `max_tokens` | `options.num_predict` |
| — | — | `options.num_ctx`, `think` (from `config.yaml`) |

### Response · Ollama → OpenAI → Anthropic

| Ollama (wire) | OpenAI (pivot) | Anthropic (client) |
| --- | --- | --- |
| `message.content` | `choices[].message.content` | `content: [{type:"text"}]` |
| `done_reason: "stop"` | `finish_reason: "stop"` | `stop_reason: "end_turn"` |
| `message.tool_calls[]` | `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |
| `message.thinking` | `reasoning_content` | `content: [{type:"thinking"}]` |
| `prompt_eval_count` | `usage.prompt_tokens` | `usage.input_tokens` |
| `eval_count` | `usage.completion_tokens` | `usage.output_tokens` |

### Two mappings LiteLLM has to infer, not copy

Tool calling is mandatory — Claude Code cannot run without it — and it is the
one place the three formats genuinely disagree rather than merely rename.

Ollama's literal native tool-call response:

```json
{
  "message": {
    "role": "assistant",
    "content": "",
    "tool_calls": [
      {"id": "call_pinr8fjq",
       "function": {"index": 0, "name": "get_weather",
                    "arguments": {"city": "Islamabad"}}}
    ]
  },
  "done_reason": "stop"
}
```

Two incompatibilities with OpenAI:

1. **`done_reason` is `"stop"`, not `"tool_calls"`.** Ollama does not
   distinguish. LiteLLM must *derive* `finish_reason: "tool_calls"` from the
   presence of a non-empty `tool_calls` array.
2. **`arguments` is a JSON object.** OpenAI specifies `arguments` as a JSON
   **string**; Anthropic's `tool_use.input` is an object again. So the value is
   serialised on the way up and parsed back down.

Both work. Verified through the full stack — the same probe via `/v1/messages`
came back as proper Anthropic:

```json
{"content": [{"type": "tool_use", "id": "call_5f0opfm0",
              "name": "get_weather", "input": {"city": "Islamabad"}}],
 "stop_reason": "tool_use"}
```

---

## Why the `ollama_chat/` prefix is load-bearing

Ollama exposes three surfaces. The prefix in `config.yaml` picks which one, and
the choice is not cosmetic:

| Ollama endpoint | Format | LiteLLM prefix | Verdict |
| --- | --- | --- | --- |
| `/api/chat` | Ollama native | `ollama_chat/` | **what we use** |
| `/v1/chat/completions` | OpenAI-compatible | `openai/` + custom base | loses `think`/`num_ctx` |
| `/api/generate` | legacy completion | `ollama/` | **no tool calling** — Claude Code cannot run |

Because the config uses `ollama_chat/`, LiteLLM speaks Ollama's native protocol,
and that is the **only** reason `think: false` and `num_ctx: 32768` reach the
engine at all. On the OpenAI-compatible surface they would be unrecognised
fields and silently dropped, which resurrects two already-diagnosed failures:
the empty-response bug below, and silent truncation at Ollama's 4096-token
default.

The `ollama/` prefix would break Claude Code outright.

## What the translation costs

Anthropic-specific features with no OpenAI equivalent have nowhere to go and are
dropped:

- **`cache_control` / prompt caching.** No upstream concept, so
  `cache_read_input_tokens` is always 0. Prompt caching does not exist on this
  route.
- **Extended thinking budgets** and `anthropic-beta` headers.
- **`output_config`** and other Anthropic-only parameters.

These are fine on the native `anthropic/` routes, which skip translation
entirely. Fidelity on this path is only as good as LiteLLM's Anthropic↔OpenAI
mapping — worth stating plainly in any writeup, because it is a real limit of
the approach rather than a bug in the setup.

## The translation working correctly looked like a bug

Worth recording, because it cost time. The first probe of `local-qwen3-8b`
returned HTTP 200 and **empty text**:

```json
{"content": [{"type": "thinking", "thinking": "Okay, the user wants me to reply…"},
             {"type": "text", "text": ""}],
 "stop_reason": "max_tokens",
 "usage": {"input_tokens": 15, "output_tokens": 64}}
```

The chain was working perfectly. qwen3 is a hybrid reasoning model, so it
emitted `message.thinking`; LiteLLM faithfully carried that to
`reasoning_content` and then to an Anthropic `{"type":"thinking"}` block — which
consumed the entire 64-token budget before the answer started.

The fix is `think: false` in `config.yaml`, not anything in the translation
layer. Measured, same prompt and budget:

| Config | Time | Output |
| --- | --- | --- |
| no `think: false` | 9.3s | 64 tokens, all reasoning, `text: ""`, `max_tokens` |
| `think: false` | 0.4s | 2 tokens, `"OK"`, `end_turn` |

Note that nothing in the stack reports this as a failure: the HTTP status is
200 all the way up. `gateway-up.sh` now smoke-tests for exactly this signature
(empty text plus `stop_reason: max_tokens`) and names the missing config key.

---

## Both client formats work on the same alias

One `config.yaml` entry serves both endpoints; the response shape follows
whichever you call.

```bash
# Anthropic shape — what Claude Code uses
curl -s http://localhost:4000/v1/messages \
  -H "x-api-key: $(cat .vkey)" -H 'anthropic-version: 2023-06-01' \
  -H 'content-type: application/json' \
  -d '{"model":"local-qwen3-8b","max_tokens":24,
       "messages":[{"role":"user","content":[{"type":"text","text":"Reply with exactly: A"}]}]}'
```

```json
{"type":"message","content":[{"type":"text","text":"A"}],"stop_reason":"end_turn"}
```

```bash
# OpenAI shape — same alias, same model
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $(cat .vkey)" -H 'content-type: application/json' \
  -d '{"model":"local-qwen3-8b","max_tokens":24,
       "messages":[{"role":"user","content":"Reply with exactly: B"}]}'
```

```json
{"object":"chat.completion","choices":[{"finish_reason":"stop",
 "message":{"content":"B","role":"assistant"}}]}
```

---

## Reproducing any of this

```bash
# Which endpoint Ollama actually receives
grep -oE '\[GIN\].*' litellm-gateway/ollama-serve.log | tail -20

# LiteLLM's internal view: provider, api_base, translated messages
python3 -c "
import json, glob
d = json.load(open(sorted(glob.glob('litellm-gateway/capture/_kwargs/*.json'))[-1]))['kwargs']
print('provider :', d.get('custom_llm_provider'))
print('api_base :', d.get('litellm_params', {}).get('api_base'))
print('messages :', json.dumps(d.get('messages'))[:300])
print('params   :', json.dumps(d.get('optional_params'), default=str))"
```

The kwargs dumps are capped by `CAPTURE_KWARGS_DUMPS` in
`docker-compose.yml` (default 5). Raise it and `docker compose up -d` to capture
fresh ones.

## Why this matters for the PoC

The control-plane properties survive the translation. Spend attribution, budget
enforcement, the request-modification hooks and full prompt capture all work on
a route where **no prompt leaves the laptop** — spend moved
`$0.316431 → $0.320582` across the test calls above, logged against the virtual
key like any hosted model. A fully offline model still shows up in the audit
trail, which is arguably a more interesting result than the model's own quality.

See also [`NON-ANTHROPIC-MODELS.md`](NON-ANTHROPIC-MODELS.md) for the route
inventory and [`GATEWAY-MODIFY.md`](GATEWAY-MODIFY.md) for the hooks that run
before conversion ①.
