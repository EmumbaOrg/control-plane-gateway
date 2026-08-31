# Setting a limited budget per developer, and proving it works

Two routes to the same control:

- **[Part A — the dashboard](#part-a--setting-the-budget-from-the-dashboard)**, which
  is what you actually use day to day.
- **[Part B — the six `curl` steps](#part-b--the-curl-walkthrough)**, which is the
  scripted demo: mint a key with a small budget, spend through it, and watch the
  gateway refuse the request that crosses the line.

**Verified 24–25 Aug 2026** against the running stack
(`ghcr.io/berriai/litellm:main-latest`, litellm `1.98.0`). Total cost of one
Part B run: **$0.000225** — roughly a fiftieth of a cent.

Prerequisites: the gateway is up (`docker compose ps` shows both containers) and
`.env` holds `LITELLM_MASTER_KEY`.

---

# Part A — Setting the budget from the dashboard

Sign in at http://localhost:4000/ui as `admin` with the master key.

## Where a budget can live

Every level below can carry its own `max_budget`, and a request is checked
against **all** of them. **The most restrictive wins** — widening the key cannot
undo a cap set higher up.

```
Organization  ──>  Team  ──>  Internal User  ──>  Virtual Key
   (company)      (dept)        (the dev)      (the credential)
```

Pick one deliberately:

| Put the budget on | When | Consequence |
|---|---|---|
| **Key** | Quick demo, throwaway credential | Budget dies with the key. Re-minting a lost key resets spend to zero |
| **Internal User** | **Real per-developer caps — prefer this** | Cap follows the person across every key they hold |
| **Team** | A shared departmental pool | Members draw from one allowance; team budgets are not documented yet |

## Route 1 — Budget on the key

**Virtual Keys → Create Key.**

| Field | Set it to | Notes |
|---|---|---|
| Owned By | `Another User` → pick the developer | `You` mints it against your own account |
| Team | Leave blank, unless using a team pool | See the `no-default-models` trap below |
| Key Name | e.g. `asif-claude-code` | Required. Names the developer in the refusal message |
| Models | **Leave empty** (= all models) | Claude Code needs Opus/Sonnet *and* Haiku. Restricting to one model breaks it mid-session |
| Optional Settings → Max Budget | e.g. `20` | USD |
| Optional Settings → **Reset Budget** | **`monthly`** | Do not skip — see below |

Copy the key once on creation; the database stores only a hash.

## Route 2 — Budget on the user (recommended)

**Internal Users → click the user → Edit Settings.**

| Field | Set it to |
|---|---|
| Personal Models | `all-proxy-models` |
| Max Budget | e.g. `20` |
| Budget Reset | `monthly` |

Then mint their key with **Max Budget left blank**, so the key is purely a
credential and the cap belongs to the person.

## UI label → API field

The dashboard and the API use different names for the same columns:

| Dashboard label | API / database field |
|---|---|
| Max Budget (USD) | `max_budget` |
| Reset Budget / Budget Reset | `budget_duration` (`monthly` stores as `30d`) |
| Personal Models | `models` |
| Throttle on budget exceeded | `throttle_on_budget_exceeded` |

## Four traps, all hit in practice

**1. "Reset Budget: Not set" is a lifetime cap.** Without `budget_duration` the
budget never refills. The developer spends it once and the key is dead forever.
Always set `monthly`.

**2. Over-budget looks like an outage, not a budget.** LiteLLM refuses with HTTP
**429**, and enforcement sits in the *auth* layer — so it kills **every** route,
including `/v1/models`. Claude Code's connection test therefore fails with:

```
Gateway /v1/models returned HTTP 429.
```

…and once connected, an over-budget turn shows `Rate limit reached. Retrying in
5s (attempt 4 of 10)` — Claude Code cannot tell `budget_exceeded` from a real
rate limit, so it burns all ten retries against a budget that will not reset for
weeks. **A developer will report this as "the gateway is down".** Check `spend`
before believing an outage.

**3. `no-default-models` blocks the user, not the key.** A user created through
the UI may carry the sentinel `no-default-models` in Personal Models, meaning
*"no models except through a team"*. With no team, every call is refused:

```json
{"error":{"message":"User not allowed to access model. No default model access,
only team models allowed. Tried to access claude-haiku-4-5",
"type":"key_model_access_denied","code":"403"}}
```

Fix: set Personal Models to `all-proxy-models`, or put the user in a team that
grants the models. The key's own model list is irrelevant here — the block is one
level up.

**4. The user detail panel can show a stale value.** After saving a budget the
page may render `Max Budget: $-`. The value is stored correctly; re-open the user
to confirm. Check the database if in doubt:

```bash
docker exec litellm-gateway-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select user_id, max_budget, spend, budget_duration, budget_reset_at from \"LiteLLM_UserTable\";"'
```

## Sizing a budget for Claude Code

**Measured 25 Aug 2026.** A user was capped at `max_budget: 0.002` and sent one
message reading *"reply with just hi"*:

| Model | Calls | Input tokens | Output | Cost |
|---|---|---|---|---|
| `anthropic/claude-sonnet-5` | 1 | **47,572** | 5 | **$0.184892** |
| `anthropic/claude-haiku-4-5` | 2 | 383 | 12 | $0.000443 |
| *(refused after this point)* | 13 | 0 | 0 | **$0** |

**One "hi" cost $0.18** — 92× the whole budget. Claude Code ships its system
prompt plus every tool definition on the first message, so ~47k input tokens is
the *floor* for a turn, before you type anything.

Two conclusions:

- **Budgets meaningful for `curl` demos are useless for Claude Code.** Anything
  under a dollar trips on the first message. Start developers at **$20–50/month**,
  watch real spend for a week, then tune.
- **Refused calls cost nothing.** The 13 refused requests logged `0` tokens and
  `$0` — stopped at the gateway, never sent to Anthropic, never billed. This is
  the property worth demonstrating.

Raising a budget does **not** reset `spend`; the counter keeps running and the new
ceiling applies to it.

## Softening the refusal (optional, untested)

Instead of hard-blocking an over-budget key, LiteLLM can throttle it. Read from
`litellm/proxy/auth/budget_throttle.py` on 1.98.0: this needs **three** things
together, and the UI toggle alone does nothing.

1. `Throttle on budget exceeded: Yes` on the key (proxy admins only)
2. A **TPM or RPM limit** on the key — *"a key with neither limit has nothing to
   throttle, so it stays hard-blocked"*
3. A global percentage in `config.yaml`, which is **not** in the key dialog:

```yaml
litellm_settings:
  budget_exceeded_throttle_percentage: 0.1
```

The over-budget developer then slows to 10% of their rate limit rather than
seeing "gateway is down". **Not yet run here** — worth testing.

Related: **Budget Fallbacks** in the same dialog reroute to cheaper models when a
per-model budget is exceeded (Opus → Sonnet → Haiku), degrading cost instead of
cutting the developer off. Also untested.

Note `Policies`, `Prompts`, and `Allowed Pass Through Routes` are greyed out as
**premium** on this OSS build — a licence conversation, not a config one.

---

# Part B — The `curl` walkthrough

## Step 1 — Load the master key

```bash
cd "/Users/emumba/Desktop/Control_plane(POC)/litellm-gateway" && export LITELLM_MASTER_KEY=$(grep -m1 '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
```

Everything below runs in this shell. If you open a new terminal, run this again —
`export` does not survive a new shell.

## Step 2 — Mint the developer's key with a deliberately tiny budget

```bash
curl -s -X POST http://localhost:4000/key/generate -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" -d '{"key_alias":"budget-demo","models":["claude-haiku-4-5-20251001"],"max_budget":0.0002}'
```

| Field | Why it is set this way |
|---|---|
| `max_budget: 0.0002` | Worth about six Haiku calls, so the demo trips in under a minute instead of after thousands of requests |
| `key_alias` | **Set this.** It is what makes the refusal name the developer. Without it, the error and the dashboard show a token hash |
| `models` | The key is refused (403) for anything not listed — keeps the demo cheap by making Opus unreachable |

Note `metadata.developer` is *not* used here. It is invisible in every spend view
and in the refusal message; `key_alias` is the field that carries the name.

Expected reply (abridged):

```json
{"key":"sk-…","key_name":"sk-...Rw8A","key_alias":"budget-demo","max_budget":0.0002,"spend":0.0}
```

## Step 3 — Capture the key

Copy the `"key"` value from step 2 — the database stores only a hash, so this is
the one time it is visible:

```bash
export DEVKEY="sk-…the key field from step 2…"
```

## Step 4 — Spend through the budget

Run this repeatedly, once per press, so an audience watches it happen. Calls 1–6
return a colour; call 7 is refused.

```bash
curl -s -X POST http://localhost:4000/v1/messages -H "Authorization: Bearer $DEVKEY" -H "Content-Type: application/json" -H "anthropic-version: 2023-06-01" -d '{"model":"claude-haiku-4-5-20251001","max_tokens":64,"messages":[{"role":"user","content":"Name one colour. One word only."}]}'
```

Observed run:

```
call  1: 200 ok  Blue    spend=3.5e-05
call  2: 200 ok  Blue    spend=3.5e-05     <-- counter still stale
call  3: 200 ok  Blue.   spend=0.00011
call  4: 200 ok  Blue.   spend=0.00015
call  5: 200 ok  Blue.   spend=0.00015
call  6: 200 ok  Blue    spend=0.000225    <-- already over the $0.0002 line
call  7: REFUSED
```

The refusal, verbatim:

```json
{
  "error": {
    "message": "Budget has been exceeded! Key=budget-demo (sk-...Rw8A) Current cost: 0.00022499999999999994, Max budget: 0.0002",
    "type": "budget_exceeded",
    "code": "429"
  }
}
```

That request was stopped **at the gateway**. It never reached Anthropic and was
never billed.

**If you script the loop instead of pressing manually, add `sleep 6` between
calls.** LiteLLM batch-writes spend to Postgres, so the counter reads stale —
above it held at `3.5e-05` across calls 1 and 2, then jumped. Without the pause,
extra calls slip past a check that is reading an old number.

## Step 5 — Show the recorded spend

```bash
curl -s "http://localhost:4000/key/info?key=$DEVKEY" -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

Read `spend` against `max_budget`. The observed run ended at `0.000225` against
`0.0002` — **112% of budget**.

The same key, with its spend bar past the limit, is visible at
http://localhost:4000/ui (sign in as `admin` with the master key) if you would
rather point at a screen than a JSON blob.

## Step 6 — Clean up

```bash
curl -s -X POST http://localhost:4000/key/delete -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" -d "{\"keys\":[\"$DEVKEY\"]}"
```

Expected: `{"deleted_keys":["sk-…"]}`. This is also how you revoke a real
developer — one row, no key rotation, no redeployment.

---

## Two things to say out loud

Both are visible in the output above, and someone in the room will ask.

**It overran by 12%.** Call 6 was allowed and finished past the line, taking
spend to 112% of budget. The check runs *before* the request, but the price is
only known *after* it — so the crossing request always completes and is always
billed. `max_budget` stops the **next** request. Describe it as a brake, never as
a guaranteed ceiling.

This is starker here than in production only because the budget is worth six
calls. On a real $5 or $50 allocation the same absolute overrun is a rounding
error. Size budgets so that one Claude Code turn is negligible against them and
the brake behaves like a cap in practice — and note that one measured Claude Code
turn is **$0.18**, not a fraction of a cent. See
[Sizing a budget for Claude Code](#sizing-a-budget-for-claude-code).

**The spend figure is LiteLLM's own arithmetic** — tokens × its internal price
table — not an invoice from Anthropic. Add a model LiteLLM has no pricing for and
cost computes as zero, spend never accrues, and the budget never trips. Check
pricing whenever you add a model.

---

## Variations

**A bigger, more realistic budget.** Raise `max_budget` in step 2. The overrun
becomes proportionally invisible, at the price of needing many more calls to
reach — fine for a real key, impractical for a live demo.

**A budget that follows the person, not the credential.** As written, the budget
sits on the key: the same developer with two keys gets two budgets. To cap the
human instead, create an internal user and mint their keys under that `user_id`:

```bash
curl -s -X POST http://localhost:4000/user/new -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" -d '{"user_id":"firstname.lastname","max_budget":1,"budget_duration":"30d","models":["claude-haiku-4-5-20251001"]}'
```

The refusal then names the user rather than the key, and the ceiling holds across
every key they hold.

**A shared pool carved into per-developer shares** (e.g. $5 total, $1 each) —
that needs a team, which is not documented here. Two gaps to know before
trying it: LiteLLM does not stop you over-allocating
the pool, and an overridden member allocation silently never resets unless you
pass `budget_duration` explicitly.

## Troubleshooting

Dashboard / Claude Code:

| Symptom | Cause and fix |
|---|---|
| Claude Code: `Gateway /v1/models returned HTTP 429` | The key or its user is **over budget**. Enforcement is in the auth layer, so it blocks model discovery too. Check `spend`, raise `max_budget`. Ignore the client's "add entries under Models to skip discovery" hint — that only bypasses discovery; completions still fail |
| Claude Code: `Rate limit reached. Retrying in 5s (attempt 4 of 10)` | Same cause. `budget_exceeded` is returned as 429, which the client retries as a rate limit. The retries cannot succeed — the budget resets on `budget_reset_at`, not in 5s |
| `403 key_model_access_denied` … `No default model access, only team models allowed` | The **user** has `no-default-models`. Set Personal Models to `all-proxy-models`, or add them to a team that grants the models. Editing the key will not help |
| `Max Budget: $-` after saving | Stale panel render. Re-open the user, or verify in Postgres (query above) |
| Budget set but never trips | Value too high for the traffic, or LiteLLM has no pricing for that model — an unpriced model accrues $0 forever |
| Raised the budget, still refused | Raising `max_budget` does not reset `spend`. Confirm the new ceiling actually exceeds current spend |

`curl` walkthrough (Part B):

| Symptom | Cause and fix |
|---|---|
| `401 Authentication Error` on step 2 | `LITELLM_MASTER_KEY` not exported in this shell, or step 1 was run in a different terminal |
| `403 key not allowed to access model` on step 4 | The model in the request body is not in the key's `models` list from step 2 |
| Step 4 never refuses | Budget too high for the number of calls you are willing to make. Lower `max_budget`, or check that LiteLLM has pricing for the model — an unpriced model accrues $0 forever |
| `spend` stays `0.0` in step 5 | Give it a few seconds; spend is batch-written. If it stays zero after a minute, LiteLLM has no price table entry for that model |
| Refusal shows a hash instead of a name | `key_alias` was omitted in step 2 |
