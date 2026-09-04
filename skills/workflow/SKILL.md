---
name: workflow
description: Route an ambiguous, cross-category, or skill-selection request to the correct workflow family without executing the task itself.
version: 1.0.0
user-invocable: true
---

# Workflow Router

## Use When

Use for questions about which skill to use, a skill-library overview, ambiguous intent, or a task that spans several workflow families.

## Procedure

1. Classify the request by function rather than provider or implementation language.
2. Ask one targeted question when two routes remain genuinely ambiguous.
3. Route a multi-skill task as a complete chain, using the bundle catalog when a named bundle fits.
4. Load the selected skill's full procedure before executing a real task.
5. End the routed workflow with a verification skill when code or operational changes are involved.

## Verification

Confirm that the selected skill produces the user's requested deliverable and that the route includes required safety, testing, diagnostics, and review steps.

## Boundaries

Routing does not execute work, invent a new workflow, or stretch an unrelated skill. Money and execution paths always include the trading-safe skill; security and performance audits use their focused skills.
