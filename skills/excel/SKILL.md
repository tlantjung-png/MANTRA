---
name: excel
description: Build auditable spreadsheets, financial models, trackers, or data workbooks with explicit inputs, formulas, tie-outs, and usage guidance.
version: 1.0.0
user-invocable: true
---

# Excel

## Use When

Use for spreadsheets, budgets, forecasts, trackers, financial models, or structured CSV-to-workbook work.

## Procedure

1. Clarify purpose, audience, assumptions, source data, and update cadence.
2. Separate inputs, calculations, outputs, checks, and explanatory notes.
3. Keep derived values formula-driven and use one source of truth for each input.
4. Add tie-out, validation, and error visibility.
5. Reopen the workbook and inspect formulas, formatting, ranges, and usability.

## Verification

Confirm that the workbook opens, formulas recalculate, the tie-out is zero where expected, and manual refresh steps are documented.

## Boundaries

Do not hide data in merged cells, replace formulas with stale literals, or silently assume financial inputs.
