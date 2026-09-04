---
name: security-audit
description: Perform an evidence-backed vulnerability review covering secrets, injection, authorization, unsafe defaults, deserialization, sensitive output, and visible dependency risk.
version: 1.0.0
user-invocable: true
---

# Security Audit

## Use When

Use for vulnerability reviews, injection checks, secret scans, permission reviews, and security posture questions.

## Procedure

1. Scope the repository, component, or change and read the known-failure registry first.
2. Trace secrets, command and path inputs, authentication or authorization boundaries, parsing, defaults, logs, and external dependencies.
3. Decide exploitability from the actual runtime path rather than reporting a pattern alone.
4. Rank only confirmed findings and include exact path, line, impact, evidence, and prose-only remediation.
5. State explicitly what was not checked, including unavailable live or dependency evidence.

## Verification

Every finding must have a reachable input or operational path and a source or runtime proof. An empty category is a valid result.

## Boundaries

Do not recommend vague hardening without a concrete defect. Defer live-money decisions to the trading-safe skill, and do not write credentials into reports.
