---
name: help
description: Explain current MANTRA setup, configuration, authentication, skills, commands, and troubleshooting from the actual repository and ~/.mantra state.
version: 1.0.0
user-invocable: true
---

# Help

## Use When

Use for setup questions, configuration questions, feature discovery, command
behavior, or troubleshooting.

## Procedure

1. Identify the exact feature or failure and whether the answer concerns the repo
   or the user's live configuration (`~/.mantra/config.json`, `credentials.json`,
   `sessions/`, `skills/`, `rules/commands.rules`).
2. Read the current configuration and the relevant source (the console's help
   text, `docs/`, `README.md`) before answering.
3. Explain the present behavior, required inputs, side effects, and limitations.
4. If the answer depends on version or live state, say so and perform an
   appropriate read (`/workspace`, `/model list` - `/model` is the single command
   that manages providers and models, `/tools`, `/cost`, `/skills`).
5. Change configuration or create a skill only when the user explicitly asks for
   that action.

## Verification

Every current-state claim must come from a successful source read or runtime
check. Distinguish a documented default from a personalized live setting.

## Boundaries

Do not guess unknown commands, paths, permissions, provider compatibility, or host
behavior. Do not expose secrets while diagnosing configuration.