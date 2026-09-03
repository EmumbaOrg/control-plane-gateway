#!/usr/bin/env bash
#
# Bring the whole gateway up — compose stack AND the host-side local model
# runtime — with one command.
#
#   ./gateway-up.sh              # everything, then smoke-test the local route
#   ./gateway-up.sh --no-local   # compose only, skip Ollama and its checks
#   ./gateway-up.sh --status     # report what is up; change nothing
#
# WHY THIS EXISTS. `docker compose up -d` is NOT enough to make the local
# private-model routes work, and every failure it leaves behind reads as
# "the model is broken" rather than "a prerequisite is missing":
#
#   - Ollama runs on the HOST, not in compose. It has to: a Linux container on
#     macOS has no Metal, so a containerised Ollama falls back to CPU and an 8B
#     model becomes unusably slow. That is why it is not simply another service
#     in docker-compose.yml, and it is the whole reason this script is needed.
#   - Ollama binds 127.0.0.1 by DEFAULT, which Docker Desktop's host gateway
#     cannot reach. Without OLLAMA_HOST=0.0.0.0 the container gets connection
#     refused and LiteLLM reports an upstream error naming the model.
#   - The 32768 num_ctx in config.yaml only fits in 16 GB if the KV cache is
#     quantised (OLLAMA_KV_CACHE_TYPE=q8_0). Without it the cache is ~4.7 GB on
#     top of ~5.2 GB of weights, it spills to CPU, and the symptom is a model
#     that appears to HANG rather than fail.
#   - `ollama serve` started by hand in a terminal does not survive a reboot.
#     On the machine this was written for it was running with PPID 1 and
#     `brew services` reported it unmanaged — so it worked until the next
#     restart and then silently did not.
#
# So the checks below are not defensive padding. Each one maps to a failure
# already observed, and each prints the fix rather than a stack trace.
#
# This script does NOT download anything and does NOT kill a running Ollama.
# Both are deliberate — see the notes at those steps.

set -euo pipefail
cd "$(dirname "$0")"

# --- what we manage --------------------------------------------------------
# KEEP IN SYNC WITH config.yaml. The model tag is the one the `ollama_chat/`
# entries point at; the two aliases are the honest route (CLI, via
# claude-gw.sh) and the Claude-shaped picker route (desktop app, via the shim).
OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"
LOCAL_ALIAS="${LOCAL_ALIAS:-local-qwen3-8b}"
PICKER_ALIAS="${PICKER_ALIAS:-claude-sonnet-4-5-local-q3-8b}"

GATEWAY="http://localhost:4000"
SHIM="http://localhost:4001"
OLLAMA="http://127.0.0.1:11434"
OLLAMA_LOG="ollama-serve.log"   # *.log is gitignored

# Environment `ollama serve` MUST carry. Read the block comment above before
# changing any of these — each one has a measured failure behind it.
#   OLLAMA_HOST           0.0.0.0 or the container cannot reach it at all.
#   OLLAMA_KV_CACHE_TYPE  q8_0 brings the 32k KV cache from ~4.7 GB to ~2.4 GB.
#   OLLAMA_FLASH_ATTENTION 1, because KV-cache quantisation requires it. Recent
#                         Ollama enables it automatically when the cache type is
#                         set (observed as `--flash-attn auto` on llama-server);
#                         pinning it makes that independent of the version.
#   OLLAMA_KEEP_ALIVE     30m so the model stays resident between turns. The
#                         default 5m means an idle chat pays full prefill again,
#                         which on this hardware reads as a hang. The cost is
#                         ~7.6 GB of RAM held while idle — lower it if the
#                         machine is tight.
declare -a OLLAMA_ENV=(
  "OLLAMA_HOST=0.0.0.0"
  "OLLAMA_KV_CACHE_TYPE=q8_0"
  "OLLAMA_FLASH_ATTENTION=1"
  "OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-30m}"
)

WANT_LOCAL=1
STATUS_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-local) WANT_LOCAL=0 ;;
    --status)   STATUS_ONLY=1 ;;
    -h|--help)  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

# --- output helpers --------------------------------------------------------
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# Wait for a URL to answer, up to N seconds. Returns 1 on timeout rather than
# dying, so callers can print their own diagnosis.
wait_for() {
  local url="$1" secs="${2:-60}" i
  for ((i = 1; i <= secs; i++)); do
    curl -sf --max-time 3 "$url" >/dev/null 2>&1 && { printf '%s' "$i"; return 0; }
    sleep 1
  done
  return 1
}

# ---------------------------------------------------------------------------
# 1. Docker daemon
# ---------------------------------------------------------------------------
step "Docker"
if docker info >/dev/null 2>&1; then
  ok "daemon running"
else
  die "Docker is not running. Start Docker Desktop, then re-run this script.
Nothing else below can work without it."
fi

# ---------------------------------------------------------------------------
# 2. Ollama on the host
# ---------------------------------------------------------------------------
if (( WANT_LOCAL )); then
  step "Ollama (host)"

  command -v ollama >/dev/null 2>&1 \
    || die "ollama is not on PATH. Install it (\`brew install ollama\`) or re-run
with --no-local to bring up the gateway without the private-model routes."

  if curl -sf --max-time 3 "$OLLAMA/api/tags" >/dev/null 2>&1; then
    ok "already serving on 11434"

    # ALREADY RUNNING WITH THE WRONG ENVIRONMENT is the interesting case, and
    # this script deliberately does NOT kill it. `ollama serve` may be backing
    # something else on this machine, and silently restarting a service the
    # user did not ask us to touch is worse than a loud warning. So: diagnose
    # precisely, print the one-line fix, and carry on. The gateway still comes
    # up; only the local routes degrade, and the message says how.
    # Only two variables are CORRECTNESS here, and the check is limited to
    # them on purpose — a warning that fires on a healthy setup trains people
    # to ignore the one that matters.
    #   OLLAMA_KEEP_ALIVE      comfort only (re-prefill after idle), never a
    #                          wrong answer. Not checked.
    #   OLLAMA_FLASH_ATTENTION auto-enabled by Ollama whenever a KV cache type
    #                          is set — observed as `--flash-attn auto` on the
    #                          llama-server command line with the var UNSET.
    #                          We still pass it when starting Ollama ourselves,
    #                          to be version-independent, but its absence from
    #                          a running server proves nothing. Not checked.
    pid="$(pgrep -f 'ollama serve' | head -1 || true)"
    if [[ -n "$pid" ]]; then
      running_env="$(ps eww -p "$pid" 2>/dev/null | tr ' ' '\n' || true)"
      missing=()
      for kv in "OLLAMA_HOST=0.0.0.0" "OLLAMA_KV_CACHE_TYPE=q8_0"; do
        grep -qxF "$kv" <<<"$running_env" || missing+=("$kv")
      done
      if (( ${#missing[@]} )); then
        warn "running WITHOUT: ${missing[*]}"
        # Name the consequence of what is ACTUALLY missing, not of the whole set.
        for kv in "${missing[@]}"; do
          case "$kv" in
            OLLAMA_HOST=*)
              warn "  → binds 127.0.0.1, which Docker's host gateway cannot reach: every local route fails" ;;
            OLLAMA_KV_CACHE_TYPE=*)
              warn "  → 32k KV cache stays fp16 (~4.7 GB); on 16 GB it spills to CPU and looks like a hang" ;;
          esac
        done
        warn "fix (restarts Ollama, drops in-flight requests):"
        printf '      kill %s && ./gateway-up.sh\n' "$pid"
      else
        ok "environment correct (host binding + quantised KV cache)"
      fi
    fi
  elif (( STATUS_ONLY )); then
    bad "not running"
  else
    # NOT running: nothing to lose, so start it ourselves with the right
    # environment. This is the path that makes "just turn on the gateway" true
    # after a reboot.
    printf '  starting ollama serve with: %s\n' "$(printf '%s ' "${OLLAMA_ENV[@]}")"
    env "${OLLAMA_ENV[@]}" nohup ollama serve >>"$OLLAMA_LOG" 2>&1 &
    disown || true
    if t="$(wait_for "$OLLAMA/api/tags" 30)"; then
      ok "up after ${t}s (log: $OLLAMA_LOG)"
    else
      die "Ollama did not answer on $OLLAMA within 30s.
Last lines of $OLLAMA_LOG:
$(tail -5 "$OLLAMA_LOG" 2>/dev/null || echo '  (no log)')"
    fi
  fi

  # Model present? DELIBERATELY NOT AUTO-PULLING. This is a ~5 GB download; a
  # script called "bring the gateway up" should not silently spend that on
  # someone's connection. Print the command and stop.
  if curl -sf --max-time 5 "$OLLAMA/api/tags" 2>/dev/null \
       | grep -q "\"$OLLAMA_MODEL\""; then
    ok "model $OLLAMA_MODEL present"
  else
    bad "model $OLLAMA_MODEL is NOT pulled"
    die "Pull it first (~5 GB), then re-run:
  ollama pull $OLLAMA_MODEL"
  fi
fi

# ---------------------------------------------------------------------------
# 3. Compose stack
# ---------------------------------------------------------------------------
step "Compose stack"
if (( STATUS_ONLY )); then
  docker compose ps --format '  {{.Service}}: {{.Status}}' 2>/dev/null || bad "not up"
else
  # `up -d`, NOT `restart`. A restart reuses the existing container's baked-in
  # environment, so an edited .env would be silently ignored — the exact drift
  # that makes a config change appear not to apply. `up -d` recreates only what
  # actually changed, so this is cheap when nothing has.
  docker compose up -d >/dev/null 2>&1 \
    || die "docker compose up failed. Run it directly to see why:
  docker compose up -d"
  ok "up (litellm, postgres, picker-shim)"
fi

step "Gateway health"
if t="$(wait_for "$GATEWAY/health/readiness" 90)"; then
  ok "ready after ${t}s  ($GATEWAY)"
else
  die "Gateway did not become ready within 90s. Check the logs:
  docker compose logs --tail 50 litellm"
fi

if curl -sf --max-time 5 "$SHIM/health/readiness" >/dev/null 2>&1; then
  ok "picker shim answering ($SHIM — point the desktop app here)"
else
  warn "picker shim not answering on $SHIM; the CLI is unaffected"
fi

# ---------------------------------------------------------------------------
# 4. The link that actually breaks: container -> host Ollama
# ---------------------------------------------------------------------------
if (( WANT_LOCAL )); then
  step "Container → host Ollama"
  if docker compose exec -T litellm python -c "
import urllib.request, sys
try:
    urllib.request.urlopen('http://host.docker.internal:11434/api/tags', timeout=5).read()
except Exception as e:
    sys.exit(str(e))
" >/dev/null 2>&1; then
    ok "host.docker.internal:11434 reachable from the container"
  else
    bad "container CANNOT reach Ollama"
    die "This is almost always OLLAMA_HOST. Ollama binds 127.0.0.1 by default and
Docker Desktop's host gateway cannot reach that. Restart it bound to 0.0.0.0:
  kill \$(pgrep -f 'ollama serve') && ./gateway-up.sh"
  fi
fi

# ---------------------------------------------------------------------------
# 5. Virtual key
# ---------------------------------------------------------------------------
step "Virtual key"
VK=""
[[ -f .vkey ]] && VK="$(tr -d '[:space:]' < .vkey)"
if [[ -z "$VK" ]]; then
  bad "no key in .vkey"
  warn "claude-gw.sh prints the mint command; run it once and it persists."
elif [[ "$VK" != sk-* ]]; then
  bad ".vkey does not start with sk- — placeholder or wrong value"
else
  # A 401 here after a `docker compose down -v` is the classic one: that wipes
  # the pgdata volume, and with it every virtual key, while .vkey still holds
  # the now-unknown token.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "$GATEWAY/v1/models" -H "x-api-key: $VK" || true)"
  if [[ "$code" == "200" ]]; then
    ok "accepted by the gateway (${VK:0:8}…)"
  else
    bad "gateway rejected it (HTTP $code)"
    warn "if you ran 'docker compose down -v', the key store was destroyed with"
    warn "the volume — mint a new key (claude-gw.sh prints the command)."
    VK=""
  fi
fi

# ---------------------------------------------------------------------------
# 6. Smoke-test AND warm the local route
# ---------------------------------------------------------------------------
# Doubles as the warm-up: the first call pays full model load plus prefill, and
# paying it here means the first real Claude Code turn does not look hung.
if (( WANT_LOCAL )) && (( STATUS_ONLY == 0 )) && [[ -n "$VK" ]]; then
  step "Local route smoke test (also warms the model)"
  for alias in "$LOCAL_ALIAS" "$PICKER_ALIAS"; do
    body="$(curl -s --max-time 300 "$GATEWAY/v1/messages" \
      -H "x-api-key: $VK" -H 'anthropic-version: 2023-06-01' \
      -H 'content-type: application/json' \
      -d "{\"model\":\"$alias\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}]}" || true)"
    verdict="$(PAYLOAD="$body" python3 - <<'PY'
import json, os
try:
    d = json.loads(os.environ["PAYLOAD"])
except Exception:
    print("bad|no JSON back"); raise SystemExit
if "error" in d:
    print("bad|" + str(d["error"])[:160]); raise SystemExit
text = "".join(c.get("text", "") for c in d.get("content", []) if c.get("type") == "text").strip()
# An EMPTY text with stop_reason max_tokens is the `think: false` regression:
# the whole output budget went to a thinking block. Name it, because the HTTP
# status is 200 and nothing else in the stack calls it a failure.
if not text and d.get("stop_reason") == "max_tokens":
    print("bad|empty text, stop_reason=max_tokens — 'think: false' missing from this route in config.yaml")
elif not text:
    print("bad|empty text, stop_reason=" + str(d.get("stop_reason")))
else:
    print("ok|%s (%s output tokens)" % (text, d.get("usage", {}).get("output_tokens")))
PY
)"
    if [[ "$verdict" == ok\|* ]]; then ok "$alias → ${verdict#ok|}"
    else bad "$alias → ${verdict#bad|}"; fi
  done
fi

# ---------------------------------------------------------------------------
# What to do next
# ---------------------------------------------------------------------------
if (( STATUS_ONLY == 0 )); then
  printf '\n\033[1mReady.\033[0m\n\n'
  if (( WANT_LOCAL )); then
    cat <<EOF
  CLI       ./claude-gw.sh $LOCAL_ALIAS
  Desktop   point the app at $SHIM, pick the "$PICKER_ALIAS" row
  Status    ./gateway-up.sh --status

Nothing in the local routes leaves this machine: Claude Code → LiteLLM
(container) → Ollama (host) → $OLLAMA_MODEL.
EOF
  else
    # --no-local: do NOT advertise the local aliases or the privacy property.
    # Ollama was neither started nor checked, so those routes may well be down,
    # and a footer claiming otherwise is the kind of confident-but-wrong status
    # line this script exists to replace.
    cat <<EOF
  CLI       ./claude-gw.sh <alias>      # any hosted alias from config.yaml
  Desktop   point the app at $SHIM
  Status    ./gateway-up.sh --status

Started with --no-local: Ollama was NOT started or checked, so the local-*
and *-local-* routes are not known to work. Re-run without the flag for those.
EOF
  fi
fi
