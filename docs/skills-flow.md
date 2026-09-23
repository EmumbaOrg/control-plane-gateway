# Emumba control plane — how a skill reaches the model

Two delivery paths, one source of truth (`plugins/*/skills/*/SKILL.md`).

```mermaid
flowchart TD
    SRC["SKILL.md<br/><i>plugins/emumba-*/skills/</i><br/>single source of truth"]

    SRC --> P1
    SRC --> P2

    subgraph P1 ["PATH A — plugin marketplace (client side)"]
        A1["LiteLLM /skills marketplace"] --> A2["claude plugin install<br/>→ ~/.claude/plugins/cache/litellm/"]
        A2 --> A3["Claude Code advertises catalogue<br/>in &lt;system-reminder&gt;"]
        A3 --> A4{"Model chooses<br/>Skill tool?"}
        A4 -->|yes| A5["skillUsage counter++<br/><i>check_skill_usage.py sees this</i>"]
        A4 -->|no| A6["skill never loaded<br/><i>weak models rarely choose it</i>"]
    end

    subgraph P2 ["PATH B — gateway injection (server side)"]
        B1["Claude Code → POST :4000/v1/messages"] --> B2["custom_capture.py<br/>Section 6"]
        B2 --> B3{"trigger regex matches<br/>conversation text,<br/><b>minus &lt;system-reminder&gt; blocks</b>?<br/><i>skills-inject.json</i>"}
        B3 -->|no| B9["no injection"]
        B3 -->|yes| B4{"GATEWAY_INJECT_SKILLS mode"}
        B4 -->|"off"| B9
        B4 -->|"installed (default)<br/>catalogue line present?"| B5
        B4 -->|"always"| B5
        B5["append SKILL.md body to <b>system</b><br/>after cache breakpoint"]
        B5 --> B6["log → capture/modifications.jsonl<br/><i>skills_injected: {name: bytes}</i>"]
        B6 --> B7["route alias → real provider<br/>claude-haiku-4-5-oai-41n → openai/gpt-4.1-nano"]
        B7 --> B8["model sees standard as<br/>non-optional system content"]
    end

    A5 --> OUT["Model output follows Emumba standard"]
    B8 --> OUT

    style SRC fill:#1e3a5f,stroke:#4a90d9,color:#fff
    style OUT fill:#1e4620,stroke:#4caf50,color:#fff
    style A6 fill:#5a1e1e,stroke:#d94a4a,color:#fff
    style B9 fill:#5a1e1e,stroke:#d94a4a,color:#fff
    style B5 fill:#4a3a1e,stroke:#d9a84a,color:#fff
```

## Where each claim is provable

| Claim | Evidence | Status |
|---|---|---|
| Skill distributed via gateway | `installed_plugins.json` → `@litellm`, not `@inline` | ✅ |
| Trigger fired for this request | `capture/modifications.jsonl` → `skills_injected` | ✅ |
| Body reached the model | `capture/<session>/NNN.*.request.json` → `emumba-gateway-skill:` marker | ✅ |
| Model *applied* the rules | A/B: same question with and without trigger text | ✅ |
| Model chose the Skill tool | `~/.claude.json` → `skillUsage` counter | ❌ never, on weak models |

`SKILL_USED=` in a demo prompt is **self-report** — it is not evidence of any of the above.
Injected blocks now also instruct the model to name the skill when asked, which makes that
self-report **self-fulfilling for path B**. The A/B row is the only one that survives scrutiny.

**Why the trigger excludes `<system-reminder>` blocks (fixed 15 Sep 2026):** one of those blocks
is Claude Code's own skill catalogue, which lists every skill *with its description* — and a
description trips its own trigger. With the catalogue in scope, every installed skill injected on
every request; measured at 17,683 chars on an unrelated file. The bug was invisible at one skill
and appeared only at four. The installed-check below deliberately keeps the full text, because the
catalogue is exactly what *it* reads. See `GATEWAY-MODIFY.md`.
