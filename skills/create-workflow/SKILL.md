---
name: create-workflow
description: Define an ordered skill bundle in a skills root using installed skill names, then verify it with the supported /skills bundle commands.
version: 1.1.0
user-invocable: true
---

# Create Skill Bundle

## Use When

Use when a repeatable task needs an ordered sequence of existing MANTRA skills.
The console reads bundle definitions from `BUNDLES.md` in an installed skills
root; it does not provide a separate workflow command or workflow store.

## Procedure

1. List installed skills with `/skills` and choose only steps with distinct
   purposes. Inspect each procedure with `/skills show <name>`.
2. Add one Markdown table row to `BUNDLES.md` in the chosen skills root. Put
   the bundle name in the first cell and backticked skill names, in execution
   order, in the second cell. Preserve the root's existing table format.
3. Verify every named skill is installed, then check `/skills bundles` and
   inspect the parsed order before launching.
4. Launch only when requested with `/skills launch <name>`. Each step runs as
   a separate turn in the current session and stops on a failed step.

## Boundaries

Do not invent commands or a separate workflow database. Treat editing a
personal skills root as a user-directed file change and preserve its existing
entries. A bundle cannot express arbitrary prompt steps; create a skill for a
new reusable procedure first.
