#!/usr/bin/env python3
"""
Did a gateway-distributed skill actually get USED?

Distribution is deterministic; activation is not. Claude Code keeps a local
usage counter per skill, so this is the objective check — it increments only
when a session really loads the skill, rather than when it merely appears in
the catalogue.

    python3 check_skill_usage.py            # before the test, to get a baseline
    python3 check_skill_usage.py            # after, to see whether it moved

Reads only local Claude Code state. No API call, no cost, no gateway needed.
"""

import datetime
import json
import os
import sys

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "emumba-react"
HOME = os.path.expanduser("~")


def when(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


state = load(os.path.join(HOME, ".claude.json"))
installed = load(os.path.join(HOME, ".claude/plugins/installed_plugins.json"))
settings = load(os.path.join(HOME, ".claude/settings.json"))

print("-" * 72)
print(f"Skill usage for '{PREFIX}'")
print("-" * 72)

skills = {k: v for k, v in state.get("skillUsage", {}).items() if PREFIX in k}
if not skills:
    print("  no usage recorded — the skill has never been loaded in a session")
for name, v in sorted(skills.items()):
    print(f"  {name}")
    print(f"    used {v.get('usageCount', 0)} time(s), last {when(v['lastUsedAt'])}")

# Which copy is installed, and from where. This is the part that makes a
# gateway test honest: an `@inline` registration is a LOCAL folder, so a skill
# loading from it proves nothing about distribution through the gateway.
print()
print("  Installed registrations:")
for key, entries in sorted(installed.get("plugins", {}).items()):
    if PREFIX not in key:
        continue
    for e in entries:
        src = key.split("@")[-1]
        via = "the GATEWAY marketplace" if src == "litellm" else f"a LOCAL source ({src})"
        print(f"    {key}  v{e.get('version')}  scope={e.get('scope')}  via {via}")
        print(f"      {e.get('installPath')}")

enabled = {k: v for k, v in (settings.get("enabledPlugins") or {}).items() if PREFIX in k}
print(f"  Enabled: {enabled or 'none'}")

others = [k for k in state.get("pluginUsage", {}) if PREFIX in k and "@litellm" not in k]
if others:
    print()
    print("  WARNING — a non-gateway registration of this plugin has been seen:")
    for k in others:
        print(f"    {k}   (last seen {when(state['pluginUsage'][k]['lastUsedAt'])})")
    print("  If it is still enabled, a session may load the skill from the local")
    print("  copy and the gateway test becomes a false positive. Check 'Enabled'")
    print("  above lists only the @litellm registration.")

print("-" * 72)
print("Interpretation: a usageCount that rises after your test, with only the")
print("@litellm registration enabled, is proof the gateway-distributed skill was")
print("actually used — not merely offered.")
