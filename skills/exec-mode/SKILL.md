---
name: exec-mode
description: Choose between an interactive MANTRA turn, a named skill bundle, and a bounded headless task using the interfaces this repository ships.
version: 1.1.0
user-invocable: true
---

# Execution Mode

## Use When

Use when the operator asks how to run a task, particularly when it spans
several steps or must run without an interactive console.

## Procedure

1. Use a normal interactive turn for one task that benefits from dialogue,
   review, or approval prompts.
2. Use `/skills launch <name>` only for an installed bundle whose ordered
   skills match the task. Check `/skills bundles` first.
3. Use `mantra-headless --config <cfg> --task <task.json>` for a bounded,
   non-interactive task with a valid config and task file.
4. State the required inputs, expected outputs, and stop conditions. Do not
   start a multi-step or unattended run merely because it is available.

## Verification

Confirm the chosen command exists in this checkout and that the required
bundle or task file exists before recommending execution.
