---
name: performance-audit
description: Find measurable performance bottlenecks, blocking work, repeated scans, unbounded growth, and memory or I/O waste without micro-optimizing unmeasured paths.
version: 1.0.0
user-invocable: true
---

# Performance Audit

## Use When

Use for slow paths, latency, profiling, repeated work, memory growth, or resource leaks.

## Procedure

1. Define the affected scenario, workload, and acceptable target.
2. Read the hot path and identify repeated work, blocking operations, large payloads, and unbounded state.
3. Profile or time candidates with the project's available tools.
4. Rank findings by measured impact and separate quick changes from architectural work.
5. Measure again after a change and preserve safety invariants.

## Verification

Report the workload, evidence, timing or resource observation, and before-and-after measurement when a change is made.

## Boundaries

Do not flag a code-reading suspicion as a measured bottleneck. Do not trade correctness, safety, or clarity for an unimportant micro-optimization.
