#!/usr/bin/env python3
"""
Does the first-party OpenAI route actually work?

Run it INSIDE the litellm container — that is where LITELLM_MASTER_KEY and
OPENAI_API_KEY already live, so no secret has to be pasted on a command line:

    docker compose exec -T litellm python - < verify_openai.py

Four checks, cheapest first, so a failure stops before spending anything:

  1. route registered   — GET /v1/models lists the alias
  2. cost map           — real prices, not a silent $0 (a zero-cost route is
                          exempt from max_budget entirely; see config.yaml)
  3. OpenAI-shape call  — POST /v1/chat/completions
  4. Anthropic-shape    — POST /v1/messages, what Claude Code actually sends

READ THE FAILURE CODE, they mean different things:
    401  the key is wrong or revoked           -> fix OPENAI_API_KEY
    404  the model id does not exist           -> fix litellm_params.model
    429  insufficient_quota / no credits       -> the account needs money;
         the GATEWAY IS FINE, OpenAI authenticated us and then refused

STATUS: 4/4 pass. Re-measured 7 Sep 2026 against the live gateway — both
request shapes return HTTP 200 with real content and real token usage.

    PASS  route registered      openai-gpt-41-nano among 38 routes
    PASS  cost map populated    in $0.1000/1M  out $0.4000/1M
    PASS  OpenAI-shape          text='OK'  in=12 out=1
    PASS  Anthropic-shape       text='OK'  in=12 out=2

That is a CHANGE from earlier the same day, and the history is the useful part.
This route first ran against a credit-less account and checks 3 and 4 returned
429 "You have no credits remaining." Nothing was wrong with the gateway then
either — which is the whole reason this script separates 401 from 404 from 429.
A funded key fixed it. If a 429 comes back here, reach for the billing page
before you touch config.yaml.

⚠ AND CHECK THE CONTAINER IS ACTUALLY USING YOUR NEW KEY. `docker compose
restart litellm` reuses the existing container environment, so an edited
OPENAI_API_KEY in .env does NOT take effect and every call keeps 429ing exactly
as it did before — which looks precisely like the new key being bad too. Use
`docker compose up -d litellm`, which recreates the container. See config.yaml's
OpenAI section for the fingerprint comparison that tells the two cases apart
without printing a secret.
"""

# stdlib only, deliberately. This runs inside the LiteLLM container, and the
# point of the exercise is to test the gateway — not to prove that a `requests`
# install worked. Nothing here may need a dependency the image might not have.
import json
import os
import urllib.error
import urllib.request

# All four are overridable so the same script can check a second OpenAI entry
# (gpt-5-nano, when someone adds it) without being copied and edited.
BASE = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MODEL = os.environ.get("OPENAI_ROUTE", "openai-gpt-41-nano")
UPSTREAM = os.environ.get("OPENAI_UPSTREAM", "openai/gpt-4.1-nano")
# No default and no fallback: this is read from the container's own environment
# so the master key is never typed on a command line or left in shell history.
# A KeyError here means you are running it on the host, not in the container.
MK = os.environ["LITELLM_MASTER_KEY"]

PASS, FAIL, WARN = "PASS  ", "FAIL  ", "WARN  "
results: list[tuple[str, str, str]] = []


def report(check: str, status: str, detail: str = "") -> None:
    """Print a check as it happens AND keep it for the closing tally.

    Printed immediately rather than buffered: a call to a paid provider can hang
    on the provider's side, and a half-finished run should still show which
    checks got through before it stalled.
    """
    results.append((check, status, detail))
    print(f"{status}{check}" + (f"\n        {detail}" if detail else ""))


def call(path, payload=None, hdrs=None):
    """POST/GET the gateway and return (status, parsed-body-or-error-text).

    NEVER RAISES. An HTTP error is a RESULT here, not an exception — a 429 is
    the single most informative thing this script can learn (see `explain`), so
    it has to reach the caller intact rather than unwinding the run. The body is
    truncated to 600 chars because provider error payloads can carry the entire
    offending request back, and the useful part is always at the front.
    """
    h = {"Authorization": f"Bearer {MK}", "Content-Type": "application/json"}
    h.update(hdrs or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:600]
    except Exception as e:
        # Connection refused, DNS, timeout: the gateway itself is unreachable.
        # `None` for the status distinguishes that from any HTTP answer.
        return None, f"{type(e).__name__}: {e}"


def explain(status, body) -> str:
    """Turn a failure into the one sentence that says whose fault it is.

    THIS IS THE REASON THE SCRIPT EXISTS. All three failures look identical from
    a developer's seat — "the OpenAI model doesn't work" — and they have nothing
    to do with each other: 401 is our key, 404 is our config, 429 is their
    balance. Naming which one it is stops the next person spending an afternoon
    rewriting config.yaml over an unpaid invoice. It happened; see the docstring.

    Order matters: the two 429 branches are checked most-specific first, because
    a plain rate limit and an exhausted balance share a status code and demand
    opposite responses (wait vs. pay).
    """
    text = body if isinstance(body, str) else json.dumps(body)
    if status == 401:
        return "401 — key rejected. OPENAI_API_KEY is wrong or revoked."
    if status == 404:
        return f"404 — OpenAI does not know '{UPSTREAM}'. Check the model id."
    # Substring-sniffed rather than parsed: OpenAI has used more than one code
    # for this (`insufficient_quota`, `credit_balance_exhausted`) and the shape
    # of the error envelope is not contractual. Both spellings contain one of
    # these two words.
    if status == 429 and ("quota" in text or "credit" in text):
        return ("429 — OpenAI authenticated this account and refused on BILLING. "
                "The gateway path works; the account needs credit.")
    if status == 429:
        return "429 — rate limited (not necessarily billing). Retry later."
    return f"HTTP {status} — {text[:300]}"


print(f"gateway {BASE}   route {MODEL}   upstream {UPSTREAM}\n")

# --- 1. route registered ----------------------------------------------------
# Free, and it comes first because everything below is meaningless if the config
# never loaded. THE ONLY HARD STOP IN THE SCRIPT: a missing route means the YAML
# did not parse or the container is running an older config, and the remaining
# checks would just produce confusing downstream errors about that one fact.
st, body = call("/v1/models")
ids = [m["id"] for m in body["data"]] if st == 200 and isinstance(body, dict) else []
if MODEL in ids:
    report("route registered", PASS, f"{MODEL} advertised among {len(ids)} routes")
else:
    report("route registered", FAIL, f"HTTP {st}; {MODEL} not in GET /v1/models")
    raise SystemExit(1)

# --- 2. cost map --------------------------------------------------------------
# Still free — a local lookup, no request. This guards a failure that is silent
# and expensive rather than loud: a route LiteLLM prices at $0 is exempt from
# `max_budget` altogether, so it would carry unbounded real spend while the
# budget dashboard showed nothing. Asserted here, not assumed, because this
# entry deliberately pins no `model_info` of its own and leans entirely on
# LiteLLM's built-in map (see config.yaml).
import litellm  # noqa: E402  (imported late: only needed once check 1 passes)

# NOTE: looked up by UPSTREAM (`openai/gpt-4.1-nano`), not by MODEL. The cost map
# is keyed on the real provider model, not on our alias.
info = litellm.get_model_info(UPSTREAM)
cin, cout = info["input_cost_per_token"], info["output_cost_per_token"]
detail = (f"in ${cin * 1_000_000:.4f}/1M  out ${cout * 1_000_000:.4f}/1M  "
          f"ctx {info.get('max_input_tokens')}  out_max {info.get('max_output_tokens')}")
# `PASS if cin and cout` — 0 is the failure being tested for, so a falsy check is
# the right one here, not `is not None`.
report("cost map populated", PASS if cin and cout else FAIL, detail)

# --- 3 & 4. the two request shapes -------------------------------------------
# The first two checks cost nothing; these spend money, which is why they run
# last. BOTH shapes are tested because they are not the same code path: Claude
# Code sends Anthropic-format /v1/messages, which LiteLLM must translate to an
# OpenAI-shaped upstream, and that translation is where params get mangled — it
# is exactly how the `thinking` -> `reasoning.effort` 400 was found. A route can
# pass /v1/chat/completions and still be useless to the actual client.
#
# `max_tokens: 16` keeps a failed run cheap; the probe asks for a two-token reply
# so the assertion is on content coming back at all, not on model quality.
PROBE = "Reply with exactly: OK"
for name, path, payload, hdrs in (
    ("OpenAI-shape /v1/chat/completions", "/v1/chat/completions",
     {"model": MODEL, "max_tokens": 16, "temperature": 0,
      "messages": [{"role": "user", "content": PROBE}]}, None),
    ("Anthropic-shape /v1/messages", "/v1/messages",
     {"model": MODEL, "max_tokens": 16,
      "messages": [{"role": "user", "content": PROBE}]},
     {"anthropic-version": "2023-06-01"}),
):
    st, body = call(path, payload, hdrs)
    if st != 200:
        # `continue`, not exit: if the OpenAI shape fails, whether the Anthropic
        # shape fails the SAME way is diagnostic. Both failing on 429 is a
        # billing problem; only one failing is a translation problem.
        report(name, FAIL, explain(st, body))
        continue
    # The two shapes report content and usage under entirely different keys, so
    # each is unpacked on its own terms rather than through a shared normaliser.
    if path.endswith("messages"):
        # Anthropic: content is a LIST of blocks. Joined rather than indexed at
        # [0] because a response may carry more than one text block.
        text = "".join(b.get("text", "") for b in body.get("content", []))
        u = body.get("usage", {})
        pt, ct = u.get("input_tokens", 0), u.get("output_tokens", 0)
    else:
        # OpenAI: a single string. Indexed directly — a 200 with no choices[0]
        # would be a broken gateway, and crashing on it is the honest outcome.
        text = body["choices"][0]["message"]["content"]
        u = body.get("usage", {})
        pt, ct = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
    # Priced from the same cost map check 2 just validated, so the figure printed
    # here is the one `max_budget` would actually charge against — not an estimate.
    cost = pt * cin + ct * cout
    # WARN, not FAIL, on empty text: the path demonstrably works (a 200 with real
    # token usage), and an empty completion is a model behaviour question, not a
    # gateway fault. Only a non-200 is a failure of what this script tests.
    report(name, PASS if text.strip() else WARN,
           f"text={text.strip()!r}  in={pt} out={ct}  cost=${cost:.10f}")

print()
# Exit code carries the verdict so this can be dropped into CI unchanged. WARN
# deliberately does not count as failure — see the empty-text note above.
failed = [c for c, s, _ in results if s == FAIL]
print(f"{len(results) - len(failed)}/{len(results)} passed"
      + (f" — failing: {', '.join(failed)}" if failed else ""))
raise SystemExit(1 if failed else 0)
