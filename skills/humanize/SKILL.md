---
name: humanize
description: Rewrite stiff, generic, or AI-sounding prose into natural, specific, truthful writing while preserving every factual proposition and removing reasoning-transcript leakage.
version: 1.1.0
user-invocable: true
---

# Humanize

## Use When

Use when text feels robotic, over-formal, repetitive, or generic and needs a natural rewrite, or when prose reads like a leaked reasoning transcript.

## Procedure

1. Identify the intended audience, voice, degree of formality, and factual constraints.
2. Enumerate the passage's propositions before editing — actor and action, condition, timing and ordering, modality, negative guarantees, and ownership, side effects, failure modes, and consequences. Every factual clause must survive the rewrite; a smaller word count alone is not an improvement.
3. Rewrite the passage rather than applying only cosmetic substitutions.
4. Vary rhythm and sentence length, replace vague claims with specific truthful language, and remove filler.
5. Preserve important terminology, meaning, and natural bilingual language when present.
6. Apply the leakage test to any passage whose vantage looks like the authoring session: could a reader at HEAD, with no session transcript, PR thread, or uncommitted draft, resolve every reference and verify every claim? If not, restate the surviving facts from the repository's vantage and delete the rest.
7. Recognize leakage classes: dead design-session citations (decision numbers, audit codes, uncommitted draft sections), change narration ("used to", "no longer", "the old X"), review choreography ("rejected in review", draft ordinals), reviewer-addressed justifications, control-flow narration, and hedges or deferrals with no marker — promote deferred work to a TODO or state the actual bound.
8. Read the result aloud and revise anything that still sounds artificial.

## Verification

Confirm that the rewrite retains every original factual proposition and does not introduce unsupported claims. Confirm every remaining citation resolves at HEAD.

## Boundaries

Do not flatten a distinct voice, hide uncertainty, or replace specificity with persuasive but unverified language. Do not delete a factual clause to shorten prose; restate it or keep it.
