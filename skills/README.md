# MANTRA skill library

Bundled skills that the console's `/skills` command discovers and attaches. The
loader (`core/agent/skills.py`) indexes this directory **first**, then your
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

## Humanize and Copywriting

Two writing skills are intentionally kept separate:

- `humanize` rewrites existing prose. Its priority is meaning preservation, factual fidelity, voice consistency, and natural flow. It should not inject a generic persona, artificial mistakes, statistical irregularities, or detector-evasion tactics.
- `copywriting` creates new persuasive copy. It adapts the message to audience, awareness, channel, offer, objection, and desired action while avoiding unsupported claims and formulaic marketing language.

Neither skill guarantees a particular AI-detector score or claims that text is "100% human." Naturalness is treated as a writing-quality goal rather than a numerical detector target.

## Origin

This library was adopted from a pre-existing skill collection and adapted to
this repo's actual surfaces. Skills that referenced a separate workflow-layer
installation (`.MANTRA` state, PowerShell helper scripts, Rhai workflows, a
monitor stack) were rewritten to use the primitives this repo really ships:

- workspace memory: `<workspace>/.mantra/memory.md`
- known-failure registry: `knowledge/known-failures.md`
- command rules: `rules/commands.rules` + `~/.mantra/rules/commands.rules`
- workflows: `/workflow` (JSON prompt sequences in `~/.mantra/workflows.json`)
- integrity chain: not shipped - deliberately left behind (see docs/ADOPTION.md)
- sessions & logs: `~/.mantra/sessions/`, `logs/*.jsonl`
- session state: `/goal`, `/todo`, `/compact`, `/sessions` (resume was folded into `/sessions`)
- tools: the console's bounded `read_file` / `list_dir` / `search_code` / diff
  renderer, with read-before-edit enforcement built into `edit_file`

If a skill still names a file or command that does not exist in this repo, that
is a leftover from the adaptation - report it and it will be rewired.