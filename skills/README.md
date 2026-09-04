# MANTRA skill library

Bundled skills that the console's `/skills` command discovers and attaches. The
loader (`src/mantra/core/skills.py`) indexes this directory **first**, then your
personal `~/.mantra/skills` tree as a supplement - so a personal skill of the
same name never shadows the bundled one, and your own additions are still found.

## Format

Each skill is a directory containing a `SKILL.md` with YAML-ish frontmatter:

```markdown
---
name: <slug>
description: <one-line summary>
version: 1.0.0
user-invocable: true
---

# Title

## Use When
...

## Procedure
1. ...
```

`/skills` lists every skill, `/skills <name>` attaches one for the session, and
the attached procedure is injected into the agent's system prompt so it is
followed rather than improvised.

## Origin

This library was adopted from a pre-existing skill collection and adapted to
this repo's actual surfaces. Skills that referenced a separate workflow-layer
installation (`.MANTRA` state, PowerShell helper scripts, Rhai workflows, a
monitor stack) were rewritten to use the primitives this repo really ships:

- workspace memory: `<workspace>/.mantra/memory.md`
- known-failure registry: `knowledge/known-failures.md`
- command rules: `rules/commands.rules` + `~/.mantra/rules/commands.rules`
- workflows: `/workflow` (JSON prompt sequences in `~/.mantra/workflows.json`)
- integrity chain: `.mantra/vault` + `scripts/vault-verify.py`
- sessions & logs: `~/.mantra/sessions/`, `logs/*.jsonl`
- session state: `/goal`, `/todo`, `/compact`, `/resume`
- tools: the console's bounded `read_file` / `list_dir` / `search_code` / diff
  renderer, with read-before-edit enforcement built into `edit_file`

If a skill still names a file or command that does not exist in this repo, that
is a leftover from the adaptation - report it and it will be rewired.