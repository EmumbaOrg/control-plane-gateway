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
