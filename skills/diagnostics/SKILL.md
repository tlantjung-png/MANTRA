---
name: diagnostics
description: Detect and run the project's available static analysis, parsers, linters, type checks, and compilers, reduce cross-tool findings into one prioritized report, then fix or report every supported diagnostic.
version: 2.0.0
user-invocable: true
---

# Diagnostics

## Prerequisites

- Detect the available check tooling first; this is a Python repository, so the guaranteed checks are pytest and Python compile checks (compileall / py_compile). Optional analyzers may be absent and are recorded, not installed. PowerShell parsing applies only to tests/probe-console.ps1 if that probe is kept.

## Use When

Use for linting, type checking, parser errors, compiler warnings, or a request to clean diagnostics.

## Procedure

1. Detect the language, project-defined checks, manifests, and available tools.
2. Prefer the project's own checks and run them in their documented order.
3. Collect every tool's raw output, then run the **Reduce** phase below to produce one cross-tool prioritized finding set.
4. Fix one diagnostic class at a time, keeping changes minimal and in local style.
5. Rerun the affected check, then the repository verification gate or relevant suites.
6. Record unavailable tooling rather than installing it without approval.

## Verification

Report the prioritized finding list from Reduce, fixes made, remaining issues, and the exact reason a check was skipped. Include the cross-tool agreement count per finding — a finding confirmed by multiple tools carries more weight than a single-tool finding. For this repository, the checks are pytest and Python compile checks; the optional analyzer may not be installed.

## Reduce

After all check tools have run, reduce their outputs into one cross-tool prioritized finding set. Raw per-tool output buries high-signal findings under noise, duplicates the same root cause across tools, and hides cross-tool agreement.

### Step 1 — Validate

- Confirm each tool actually ran to completion. A tool that crashed or timed out produced no usable output — log it as unavailable, do not fabricate findings.
- Drop any tool output that is empty, truncated, or returned a non-zero exit with no diagnostics.

### Step 2 — Normalize

- Convert every tool's diagnostic format to one canonical shape: file path, line/column, severity, rule id, message.
- Normalize file paths to a consistent format (relative to project root, forward slashes).
- Normalize severity levels across tools (error/warning/info) to a single scale.

### Step 3 — Group

- Collapse diagnostics that point to the same file, line, and rule into single entries.
- When multiple tools flag the same location for different reasons, group them as co-located findings — the file and line are identical but the underlying issues are distinct.
- When the same root cause surfaces through different rules across tools (e.g., an unused variable flagged by both the linter and the type checker), group them as the same root cause detected through different check layers.

### Step 4 — Surface Structure

- **Cross-tool agreement**: A location flagged by multiple independent tools is higher priority than a single-tool finding. Rank by number of tools agreeing.
- **Cross-tool contradiction**: Where one tool flags a pattern as an error and another tool's configuration explicitly allows it, surface the conflict — this indicates a configuration mismatch between tools, not a code defect.
- **Single-tool isolation**: Findings that appear in only one tool's output are valid but should be noted as single-source. Verify they are not false positives from a tool-specific quirk.
- **Rejection log**: Record any tool that did not run or produced unusable output, with the reason.

### Step 5 — Deliver

The Reduce phase produces:

1. **Prioritized finding list** — ranked by cross-tool agreement count, then by severity. Ready for the fix step.
2. **Cross-tool map** — showing which tools agreed on which findings, revealing configuration gaps or tool blind spots.
3. **Rejection log** — any tool that failed to run and why.

## Boundaries

Do not invent lint rules, silence unresolved warnings, or format unrelated files. Generated or intentional warnings must be identified explicitly.
