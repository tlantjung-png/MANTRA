---
name: write-docs
description: Write precise technical documentation for current code, APIs, workflows, or features by tracing behavior, applying placement and detail hierarchy, and cross-checking every claim.
version: 1.1.0
user-invocable: true
---

# Write Documentation

## Use When

Use for README work, technical references, API explanations, setup guidance, or a request to explain how a system works.

## Procedure

1. Read the implementation, tests, configuration, and existing documentation structure.
2. Locate the document in the repository tree and set the permitted level of detail: keep full detail about the document's own subject, summarize direct children by purpose and responsibility, and move deeper explanations to their owning descendants with links.
3. Classify the document from its intended use, not its path or title: a tutorial leads through ordered work to an observable outcome; a reference supports lookup within an explicit scope without sequential reading.
4. Start with purpose and when to use the component.
5. Describe parameters, outputs, errors, side effects, state, and limitations in precise prose.
6. Preserve the complete proposition when editing: keep the actor and action, condition, timing and ordering, modality (must, may, never), negative guarantees, and ownership, side effects, failure modes, and consequences. Never drop a factual clause for brevity.
7. Give every explanation one home: keep a complete contract at the point of use, and link to the owning document for architecture, rationale, algorithms, history, or extended examples instead of repeating them.
8. Keep current behavior distinct from planned or historical behavior. Docs match the code in the same change: config, defaults, errors, wire fields, events, and public behavior update the owning README and JSDoc alongside the diff.
9. If a bilingual counterpart exists for the document, update both sides of the pair in the same change.
10. Cross-check paths, counts, defaults, and verification claims before writing.

## Verification

Every claim must be supported by a successful source read, check, or runtime observation. Report the documentation path and what was verified.

## Boundaries

Do not document unchecked behavior as current. Avoid executable examples or implementation snippets when prose and file references are sufficient. Do not narrate change history where a present-tense statement of current behavior is accurate.
