#!/usr/bin/env bash
#
# Launch Claude Code against this LiteLLM gateway.
#
#   ./claude-gw.sh                    # default model, nemotron-free
#   ./claude-gw.sh gemini-3.7-flash   # any alias from config.yaml
#   ./claude-gw.sh gpt-5.2 -p "hi"    # extra args pass through to claude
#
# The virtual key is read from .vkey, and only from .vkey. To run against a
# different key, put that key in .vkey — one active key at a time, one place to
# look. Mint it once; it is not per-run. The master key and provider keys stay
# in .env, which is the server's boot config and nothing to do with how you
# authenticate as a caller.
#
# Why this exists rather than an inline command: every failure so far has been
# a credential problem that surfaced as something else — a placeholder pasted
# verbatim, a key that had expired, a variable name that did not exist so the
# token was empty. Claude Code reports all of those as an opaque 401 and then
# retries ten times. This validates the key against the gateway FIRST and says
# what is actually wrong.
#
# The key is never printed. Only a prefix and a length.

set -euo pipefail

cd "$(dirname "$0")"
ENV_FILE=".env"
VKEY_FILE=".vkey"
GATEWAY="http://localhost:4000"
MODEL="${1:-gemini-3.7-flash}"
shift || true

die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }

[[ -f "$ENV_FILE" ]] || die "No $ENV_FILE in $(pwd)"

# Read one variable out of .env without sourcing the file. Sourcing would also
# export ANTHROPIC_API_KEY, and when that is set Claude Code sends it as
# x-api-key — the gateway then rejects a real Anthropic key for not being a
# virtual key, which looks nothing like the actual cause.
getenv_var() {
  sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

MASTER_KEY="$(getenv_var LITELLM_MASTER_KEY)"
[[ -n "$MASTER_KEY" ]] || die "LITELLM_MASTER_KEY not found in $ENV_FILE"

# The virtual key lives in its own file, not in .env.
#
# .env configures the SERVER: the master key and the provider keys, read by
# docker compose at container boot. Those are singular and long-lived. The
# virtual key is the opposite — it is a CLIENT credential, per developer and
# per scenario, and it gets replaced whenever you mint a new one. Keeping it in
# .env meant the two files drifted: minting wrote one place and left the other
# stale, and there was no single source of truth to point at.
#
# Deliberately no override — no VKEY_FILE variable, no --key-file flag. Keeping
# spare keys in sibling files is how the .env/.vkey drift started: several files
# hold a plausible key and nothing says which one is live. One file, one active
# key. To test another key, paste it into .vkey.
#
# The old behaviour — scanning .env for anything starting sk- — is deliberately
# gone rather than kept as a fallback. It excluded four provider names by hand,
# so a fifth provider whose key also starts sk- would have been handed to Claude
# Code as the virtual key, producing a 401 naming a key you never chose. That is
# the exact failure class this script exists to prevent.
VK=""
if [[ -f "$VKEY_FILE" ]]; then
  # Strip whitespace and any trailing newline; `curl … > .vkey` leaves one.
  VK="$(tr -d '[:space:]' < "$VKEY_FILE")"
fi

[[ -n "$VK" ]] || die "No virtual key in $(pwd)/$VKEY_FILE.
Mint one and write it there:
  export LITELLM_MASTER_KEY=\"\$(sed -n 's/^LITELLM_MASTER_KEY=//p' $ENV_FILE | head -1)\"
  curl -s -X POST $GATEWAY/key/generate \\
    -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\
    -H 'Content-Type: application/json' \\
    -d '{\"models\":[\"$MODEL\"],\"max_budget\":10}' \\
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"key\"])' > $VKEY_FILE

You do NOT need to mint a key every run. Do this once; the key persists until
it expires or you delete it."

note "key      : $VKEY_FILE ${VK:0:8}… (${#VK} chars)"
[[ "$VK" == sk-* ]] || die "That value does not start with 'sk-', so the gateway will reject it.
Looks like a placeholder or the wrong variable was picked up."

# --- validate against the gateway before launching -------------------------
# gateway-up.sh, not `docker compose up -d`: compose alone does not start the
# host-side Ollama the local-* routes depend on, and the failure that leaves
# behind reads as a broken model rather than a missing prerequisite.
curl -sf "$GATEWAY/health/readiness" >/dev/null 2>&1 \
  || die "Gateway not reachable at $GATEWAY. Start everything with:  ./gateway-up.sh"

export INFO
INFO="$(curl -s "$GATEWAY/key/info?key=$VK" -H "Authorization: Bearer $MASTER_KEY")"

python3 - "$MODEL" <<'PY' || exit 1
import json, sys, datetime, os
model = sys.argv[1]
try:
    info = json.loads(os.environ["INFO"]).get("info") or {}
except Exception:
    print("\033[31mCould not read /key/info — is the master key correct?\033[0m"); sys.exit(1)
if not info:
    print("\033[31mThe gateway does not recognise this key. It may have been deleted.\033[0m"); sys.exit(1)

problems = []

exp = info.get("expires")
if exp:
    try:
        when = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        if when < now:
            problems.append(f"EXPIRED on {when:%Y-%m-%d %H:%M UTC}. Mint a new key (omit \"duration\" so it does not expire).")
        else:
            print(f"expires  : {when:%Y-%m-%d %H:%M UTC}")
    except ValueError:
        print(f"expires  : {exp}")
else:
    print("expires  : never")

budget, spend = info.get("max_budget"), info.get("spend") or 0.0
if budget is not None:
    print(f"budget   : ${spend:.6f} of ${budget}")
    if spend >= budget:
        problems.append(f"Budget exhausted: spend ${spend} >= max_budget ${budget}.")
else:
    print(f"budget   : unlimited (spend ${spend:.6f})")

allowed = info.get("models") or []
# `all-proxy-models` / `all-team-models` are LiteLLM wildcard SENTINELS, not
# model names: they mean "every model this key can reach". Comparing the
# requested alias against them literally refuses a key that grants everything,
# which reads as "the model does not exist" — the same class of misleading
# credential error the rest of this script exists to prevent.
#
# This is NOT hypothetical and NOT local-model-specific: the .vkey minted for
# this PoC carries exactly `["all-proxy-models"]`, so without this branch every
# model on every route is refused before launch.
wildcards = {"all-proxy-models", "all-team-models"} & set(allowed)
if wildcards:
    print(f"models   : all ({', '.join(sorted(wildcards))})")
elif not allowed:
    print("models   : all (no allowlist set)")
else:
    print(f"models   : {len(allowed)} allowed")
    if model not in allowed:
        problems.append(f"'{model}' is not in this key's allowlist. Allowed: {', '.join(allowed)}")

if problems:
    print("\n\033[31mThis key will not work:\033[0m")
    for p in problems:
        print("  - " + p)
    sys.exit(1)
print("\033[32mkey OK\033[0m")
PY

note "model    : $MODEL"
note "gateway  : $GATEWAY"

# --- context window default, per route class -------------------------------
# The 1000000 default below is right for the hosted 1M-context routes and WRONG
# for a local one. Ollama serves these at num_ctx 32768 (config.yaml) and
# TRUNCATES silently past it — no error, just a model that has not seen the end
# of its own prompt. Claude Code told it has a 1M window will happily build one.
#
# So local routes default to just under the num_ctx the server allocates:
# 32000, under the num_ctx 32768 set on every `ollama_chat/` entry in
# config.yaml.
#
# KEEP THIS IN SYNC WITH config.yaml — one setting expressed in two files, and
# the failure when they drift is silent truncation, not an error.
#
# DO NOT LOWER THIS TO SAVE PREFILL TIME. 8000 was tried (tracking a num_ctx
# 8192 experiment on the qwen3 route) and Claude Code refused to start: "Prompt
# is too long" before the first turn, because its system prompt plus tool
# definitions plus injected org skills already exceed 8k. Anything that cannot
# seat the preamble is not a smaller window, it is a broken route.
#
# Matches both naming schemes: the honest `local-*` aliases and the
# Claude-shaped picker ones (`claude-sonnet-4-5-local-q3-8b`), which is why the
# pattern looks for `local-` anywhere rather than as a prefix.
#
# Still an override, not a floor: an explicit CLAUDE_CODE_MAX_CONTEXT_TOKENS in
# the environment wins, as before.
case "$MODEL" in
  *local-*) DEFAULT_CTX=32000 ;;
  # z.ai direct. These are NOT 1M-input models, and the generic default below
  # would tell Claude Code it has several times the window it really has.
  # config.yaml declares max_input_tokens for the desktop app's benefit, but
  # the CLI reads this variable instead, so it needs its own case.
  #
  # ORDER MATTERS: the 4.5 pattern must come FIRST. `case` takes the first
  # match, and both aliases contain `zai-`, so a single `*zai-*` arm would
  # silently hand the 128k model the 200k figure. That was the state between
  # adding zai-glm45-flash and this fix.
  #   glm-4.5-flash  128k in /  32k out  -> 120000
  #   glm-4.7-flash  200k in / 128k out  -> 190000
  # Each leaves headroom under the real ceiling.
  *zai-*45f|*zai-glm45-*) DEFAULT_CTX=120000 ;;
  *zai-*)   DEFAULT_CTX=190000 ;;
  *)        DEFAULT_CTX=1000000 ;;
esac
# --- request timeout, per route class --------------------------------------
# A local 8B model on this hardware spends minutes in prefill before it emits a
# single byte, and Claude Code's default wait is far shorter. The observed
# failure is NOT an error anywhere: the gateway logs 200 OK from Ollama, then
# "client disconnected before first chunk, upstream LLM request cancelled", and
# the user sees an empty response. Raising the client's patience is the only
# fix on this side; think:false and a smaller num_ctx in config.yaml attack the
# same problem from the server side.
#
# 600000ms = 10 minutes, for local routes only — a hosted route that has not
# answered in ten minutes is hung, and waiting that long on it hides a real
# fault. Explicit API_TIMEOUT_MS in the environment still wins.
case "$MODEL" in
  *local-*) DEFAULT_TIMEOUT_MS=600000 ;;
  *)        DEFAULT_TIMEOUT_MS=120000 ;;
esac

note "context  : ${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-$DEFAULT_CTX} tokens"
note "timeout  : ${API_TIMEOUT_MS:-$DEFAULT_TIMEOUT_MS} ms"
echo

# CLAUDE_CODE_MAX_CONTEXT_TOKENS: Claude Code does not recognise these aliases
# and would otherwise assume a 200k window, discarding most of a 1M-context
# model. Set it to the real window of the model you are running.
# -u ANTHROPIC_API_KEY is load-bearing. If that variable is set in the calling
# shell — easy to do by accident, since it lives in this same .env — Claude Code
# sends it as x-api-key and it wins over ANTHROPIC_AUTH_TOKEN. The gateway then
# rejects a key it has never seen, and the 401 names a key you did not choose,
# which is genuinely baffling to debug. Observed in exactly that form.
# CLAUDE_CODE_MAX_OUTPUT_TOKENS is the important one. Claude Code asks for
# 32000 output tokens by default, and providers bill or gate on the RESERVATION,
# not on what is actually produced. OpenRouter refuses outright on a low balance
# ("you requested up to 32000 tokens, but can only afford 10417"), and every
# provider counts it toward the per-minute token limit. Capping it to 8000 is
# what makes these models usable on a free balance; measured working at 8000,
# refused at 32000. Raise it if you top up credit and want longer outputs.
exec env -u ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL="$GATEWAY" \
  ANTHROPIC_AUTH_TOKEN="$VK" \
  CLAUDE_CODE_MAX_CONTEXT_TOKENS="${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-$DEFAULT_CTX}" \
  CLAUDE_CODE_MAX_OUTPUT_TOKENS="${CLAUDE_CODE_MAX_OUTPUT_TOKENS:-8000}" \
  API_TIMEOUT_MS="${API_TIMEOUT_MS:-$DEFAULT_TIMEOUT_MS}" \
  claude --model "$MODEL" "$@"
