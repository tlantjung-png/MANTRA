---
name: workflow
description: Choose the relevant installed skill or ordered skill bundle for an ambiguous request, using the catalog and the user's actual goal.
version: 1.1.0
user-invocable: true
---

# Workflow Router

## Use When

Use when the user asks which MANTRA skill fits a task, or when several skills
could reasonably apply. A direct task request should normally be worked on,
not routed into a separate discussion.

## Procedure

1. Read the installed skill names and descriptions with `/skills` or the
   catalog. Inspect a candidate's full procedure with `/skills show <name>`.
2. Choose the narrowest skill that covers the requested result. Explain a
   multi-skill sequence only when each step adds a distinct deliverable.
3. If a skills root provides `BUNDLES.md`, check `/skills bundles` before
   suggesting `/skills launch <name>`. Do not invent a named bundle.
4. State the route and its reason briefly. If the user asked for the actual
   work, continue with it using the selected procedure.

## Boundaries

Do not claim that an unlisted slash command exists. Do not create or launch a
bundle without the user's task calling for that action.
