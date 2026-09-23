# emumba-react

A Claude Code plugin holding React/Next.js skills, distributed to engineers
through the LiteLLM gateway's marketplace.

A skill is a folder with a `SKILL.md`: YAML frontmatter (`name`, `description`)
plus markdown instructions. Claude reads only the `description` up front and
pulls the body into context when a task matches it, so an unused skill costs
nothing.

## Layout

```
.claude-plugin/plugin.json          plugin manifest
skills/<skill-name>/SKILL.md        one folder per skill
skills/<skill-name>/references/…    optional files the skill loads on demand
```

The layout is not negotiable. Claude Code discovers skills at
`skills/<name>/SKILL.md` only — nesting them a level deeper (by category, say)
installs cleanly and loads nothing.

The `plugin.json` matters too: without it, skills get namespaced by the version
directory (`0.1.0:react-best-practices`) instead of the plugin name.

## Skills

| Skill | Status | Origin |
| --- | --- | --- |
| `react-best-practices` | Mirrored, unmodified | [claude-code-templates](https://github.com/davila7/claude-code-templates) — MIT, credited to Vercel Engineering. See [ATTRIBUTION.md](ATTRIBUTION.md). |

`skills/react-app/` is an empty placeholder for an Emumba-authored skill.

## Registering it in LiteLLM

Not yet registered — this is repo content only. When ready, push to `main` and
register the subdirectory so engineers receive just this folder:

```bash
curl -X POST http://localhost:4000/claude-code/plugins \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "emumba-react",
    "source": {
      "source": "git-subdir",
      "url": "https://github.com/asif-emumba/control-plane-gateway.git",
      "path": "plugins/emumba-react"
    },
    "version": "0.1.0",
    "description": "React and Next.js skills vetted for Emumba engineering"
  }'
```

Use `git-subdir` or `url`, never `{"source": "github", "repo": "..."}` — that
form makes Claude Code clone over SSH, which fails on any machine without a
github.com host key.

## Installing (engineers)

```bash
claude plugin marketplace add http://localhost:4000/claude-code/marketplace.json
claude plugin install emumba-react@litellm
```

Verify it loaded:

```bash
claude --print "List the skill names available to you that start with 'emumba-react:'."
```

This repository is private, so the clone runs with **your** git credentials —
LiteLLM only hands out the URL, it does not proxy the clone. You need `gh auth`
or a git credential helper configured.

## Testing the distribution end to end

**Verified 31 Aug 2026** against the running gateway.

### 1. The gateway serves the catalog — no auth needed

```bash
curl -s http://localhost:4000/claude-code/marketplace.json | python3 -m json.tool
```

You want a `plugins` array containing `emumba-react`. **This route is
deliberately unauthenticated** — that is how `claude plugin marketplace add`
reaches it, and it is a governance limit worth stating: anyone who can reach the
gateway can read the whole catalog.

### 2. The management routes ARE protected

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:4000/claude-code/plugins -H "Content-Type: application/json" -d '{"name":"x"}'
```

Expect `401`. Registering, updating and deleting need the master key; only
reading the catalog is open.

### 3. Add the marketplace and install (what a developer does)

```bash
claude plugin marketplace add http://localhost:4000/claude-code/marketplace.json
```

```bash
claude plugin install emumba-react@litellm
```

```bash
claude plugin list
```

Expect `emumba-react@litellm` with status `enabled`.

> The clone runs with **your** git credentials — LiteLLM hands out the URL, it
> does not proxy the clone. On a private repo you need `gh auth` or a git
> credential helper configured, or this step fails with an auth error that looks
> like a gateway problem.

### 4. The skill actually reaches a session

```bash
claude --print "List the exact names of any skills available to you that start with 'emumba-react:'. Reply with just the names, nothing else."
```

Expect `emumba-react:react-best-practices`. Run it through the gateway
(`./claude-gw.sh` in `litellm-gateway/`, or export `ANTHROPIC_BASE_URL` and
`ANTHROPIC_AUTH_TOKEN` first) — a bare `claude` with no credential in the shell
fails with `Not logged in`, which is not a skills problem.

### 5. Activation, which is the part that is NOT guaranteed

Distribution is deterministic; whether a session *uses* the skill is the model's
choice. Test it honestly — hand it a React file with a real performance problem
and ask for a review **without naming the skill**:

```bash
claude --print "Review example/OrdersDashboard.tsx for performance problems." 2>&1 | tail -20
```

Then check whether the skill was loaded. Measured 28 Aug 2026: `claude-sonnet-5`
auto-loaded it; `claude-haiku-4-5` did not and answered from general knowledge
(it loaded fine when told explicitly). **So publishing a skill does not guarantee
any developer's session uses it.** Skills are advisory guidance, not a control.

---

## The version field in the LiteLLM catalog is decorative

**Found 31 Aug 2026, and it will bite whoever ships the first update.**

The gateway catalog and the installed plugin disagreed:

| | Version |
|---|---|
| `marketplace.json` from the gateway | `0.2.0` |
| `claude plugin list` locally | `0.1.0` |
| `claude plugin update emumba-react@litellm` | *"already at the latest version (0.1.0)"* |

Not a bug. **The version the CLI honours comes from the cloned repo's
`.claude-plugin/plugin.json`, not from the catalog entry.** The catalog's
`version` is metadata an admin typed into LiteLLM and it drives nothing.

The consequence: **bumping the version in the LiteLLM dashboard does not ship an
update.** An admin would see `0.2.0` in the UI, believe they had released, and
every developer would stay on `0.1.0` indefinitely — with no error anywhere.

To actually ship a change:

1. Edit the skill **and** bump `version` in `.claude-plugin/plugin.json`.
2. Push to the branch the registered source points at — **the default branch**,
   because the source schema has no branch or ref field.
3. Developers run `claude plugin marketplace update litellm` *and*
   `claude plugin update emumba-react@litellm`. Refreshing the marketplace alone
   does **not** upgrade an installed plugin — verified.

Keep the catalog version in step with the manifest anyway, or the dashboard shows
a version nobody is running.

## Where the registered source actually points

As of 23 Sep 2026 the gateway registration points at
**`https://github.com/asif-emumba/emumba-skills-react.git`** (`url` source form,
plugin at the repo root) — **not** at the control-plane repository.

It previously pointed at `testing-skills.git`, path `plugins/emumba-react`, a
shared repo that also held `emumba-backend`. That was replaced because a shared
repo duplicated the backend plugin with no source of truth and could not express
per-guild ownership. Each plugin now has its own repo, matching
`emumba-skills-backend`.

**So this folder is not what developers receive.** It is the copy the *gateway
injects from* — `docker-compose.yml` mounts `./skills` into the container — while
the marketplace path clones from GitHub. Three copies exist in total: this one,
the external repo, and each developer's `~/.claude/plugins/cache/`. Nothing
synchronises them, and drift here is silent: it already happened once, when this
manifest sat at `0.1.0` while the published plugin was `0.2.0`.

Keeping them in step is manual today. Reconcile before this is offered to anyone.


## Notes and limits

- Skills are advisory. They steer model output; they are not a control. Treat
  them accordingly in any compliance context.
- Review skill changes like code. A skill silently changes what Claude produces,
  so a bad one is worse than none.
- `git-subdir` limits what engineers end up with on disk, but the full
  repository is still transferred during the clone. It is convenience, not
  isolation.
- Removing a skill from the LiteLLM catalog stops discovery. Anyone who already
  installed it keeps their local copy.
