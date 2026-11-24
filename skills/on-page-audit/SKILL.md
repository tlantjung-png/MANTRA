---
name: on-page-audit
description: Audit web pages for on-page search-visibility signals (title, meta description, canonical, viewport, robots, heading structure, image alt, links, content length) and compare any two pages side by side, with evidence.
version: 1.0.0
user-invocable: true
---

# On-Page Audit

## Use When

Use when the operator asks why a page does not perform in search, wants a
page audit, or wants to compare a page against a competitor's page. Works
for any public web page; the audit is generic, deterministic, and free of
brands or paid data.

## Procedure

1. Run the `page_audit` tool on the page's URL. It returns:
   - metrics: title, meta description, canonical, viewport, noindex
     flag, h1/h2 counts, heading sequence, image alt coverage, link
     counts, and word count;
   - findings: stable rule ids (O01..O14) with fix hints, advisory only;
   - optionally, a comparison block when a second URL is given.
2. When comparing, pass `compare_url` (a competitor or an earlier version
   of the same page). Read the comparison rows: `!=` marks where the two
   pages differ.
3. Treat every finding as a signal to verify, not a verdict. Confirm the
   page really has the problem (for example, re-check the live page
   before claiming a missing h1). Label each finding CONFIRMED,
   SUSPECTED, or UNVERIFIED.
4. For CONFIRMED findings, write one concise recommendation per finding:
   what to change and why, with the evidence. Use the `claim_audit` tool
   on any recommendation text so recommendations do not add unsupported
   claims themselves.
5. Write the report to `<workspace>/.mantra/on-page-audit.md` with a
   table: Page | Finding | Evidence | Status | Recommended action.

## Verification

Every finding in the report has a status; every CONFIRMED finding has a
checked evidence line; recommendations contain no unsupported claims
(re-check with `claim_audit`); when comparing, both pages are named in
the report.

## Boundaries

Read-only: audit external pages, never edit them. Do not invent metrics,
titles, word counts, or fixes. The tool needs network access; if the
fetch fails, report the error rather than guessing. No scores, no
guarantees about ranking.
