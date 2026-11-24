---
name: design-audit
description: Audit a web page's design signals: structure (linked stylesheets, inline CSS, image dimensions) and, when a headless browser is installed, a rendered screenshot analyzed for blank renders, dark pages, and overall contrast, with evidence.
version: 1.0.0
user-invocable: true
---

# Design Audit

## Use When

Use when the operator asks whether a page looks broken, renders blank,
has no styling, or how its visual quality compares to expectations. The
audit is generic, deterministic, and free of brands or paid services.

## Procedure

1. Run the `design_audit` tool on the page's URL. It returns:
   - structure: number of linked stylesheets, inline `<style>` blocks,
     and images missing `width`/`height` attributes;
   - visual (when a headless browser is installed and the render
     succeeds): viewport size, average luminance, luminance spread,
     average saturation, blank/white ratio, and dark ratio;
   - findings: stable rule ids (F01, F02 structural; D01..D03 visual)
     with fix hints, advisory only.
2. When the report says no headless browser was available, treat the
   visual section as unverified and say so in the report.
3. Verify each finding before acting: re-check the live page, and for a
   visual finding, confirm the rendered behavior. Label findings
   CONFIRMED, SUSPECTED, or UNVERIFIED.
4. For CONFIRMED findings, write one concise recommendation per finding:
   what to change and why, with the evidence. Use the `claim_audit`
   tool on recommendation text so recommendations do not add unsupported
   claims.
5. Write the report to `<workspace>/.mantra/design-audit.md` with a
   table: Page | Finding | Evidence | Status | Recommended action.

## Verification

Every finding in the report has a status; every CONFIRMED finding has a
checked evidence line; recommendations contain no unsupported claims
(re-check with `claim_audit`); when the visual section is missing, the
report says why.

## Boundaries

Read-only: audit external pages, never edit them. Do not invent metrics
or visual judgments beyond what the tool measured. The visual section
needs a headless browser and network access; if either is missing, report
the limitation rather than guessing. No scores, no guarantees about
aesthetics or ranking.
