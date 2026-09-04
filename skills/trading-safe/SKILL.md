---
name: trading-safe
description: Review live-money, order, position, execution, and risk paths with a fail-closed standard, proof requirements, numerical checks, and explicit operator decisions.
version: 1.0.0
user-invocable: true
---

# Trading Safe

## Use When

Use for trading, orders, positions, execution, capital, stops, risk controls, or live-money changes.

## Procedure

1. Reject malformed, stale, ambiguous, or incomplete inputs before any action.
2. Inspect direction agreement, gates, threading, numerical precision, stop rules, and failure outcomes.
3. Require evidence from source, tests, and runtime probes for high-risk claims.
4. Mark money-path changes as risky and require an operator decision.
5. Add a regression test for each incident class and verify fail-closed behavior.

## Verification

A crash, invalid input, stale state, or uncertain dependency must block execution. Report exact evidence, operator decisions, and remaining unverified live conditions.

## Boundaries

Read-only by default. Never log credentials. Do not normalize a risky execution change as routine cleanup.
