---
name: create-workflow
description: Create, register, and launch a MANTRA workflow (an ordered prompt sequence in ~/.mantra/workflows.json) using the /workflow commands.
version: 1.0.0
user-invocable: true
---

# Create Workflow

## Use When

Use when a user asks to create, edit, save, or launch a MANTRA workflow. MANTRA
workflows are ordered prompt sequences stored in
`~/.mantra/workflows.json` (a single JSON document), created and launched through
the `/workflow` command - there is no separate `/create-workflow` command.

## Procedure

1. Confirm the workflow name, description, and each step's prompt with the user.
2. Create it with `/workflow create <name>`, then type one step per line and end
   with a `.` on its own line. The console stores the sequence and prints a slug,
   e.g. `my-flow`.
3. Show it with `/workflow show <name>` (or bare `/workflow` to list); inspect each
   step before launching.
4. Launch with `/workflow launch <name>` - each step runs as its own turn against
   the same workspace.
5. Remove with `/workflow remove <name>` when the workflow is no longer needed.
6. Confirm the run completed before reporting success.

## Contract

- Storage: `~/.mantra/workflows.json` (JSON), not per-file `.rhai` scripts.
- Slugs: spaces become `-` (`/workflow create my flow` -> `my-flow`).
- Steps: plain prompts run through the same agent loop as a typed message.
- Limits: up to 50 steps, 4000 chars per step (enforced by the console).

## Verification

The workflow must appear in `/workflow` (or `/workflow show <name>`) and launch by
name. A launch failure must be reported instead of treated as success.

## Boundaries

File-writing scaffolding is side-effecting and should be manually invoked; require
approval before creating the workflow. Do not invent host commands such as
`/create-workflow`. Keep `create-skill` for Markdown workflow skills; this skill
covers only `/workflow` prompt sequences.