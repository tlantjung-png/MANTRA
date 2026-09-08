---
name: humanize
description: Rewrite stiff, robotic, repetitive, or overly polished prose into natural human-sounding writing while preserving meaning, facts, terminology, and the writer's intended voice.
version: 1.0.0
user-invocable: true
---

# Humanize

## Purpose

Turn existing writing into prose that feels naturally written by a real person.

The goal is not to make text "look human" through artificial randomness. The goal is to remove the patterns that commonly make prose feel machine-generated: formulaic structure, excessive symmetry, generic transitions, inflated wording, repetitive sentence rhythm, unnecessary explanation, and a voice that sounds detached from the subject.

Natural writing can still be clear, polished, grammatical, and professional. Do not make writing worse merely to make it appear human.

## Use When

Use for rewriting prose that feels robotic, stiff, generic, over-formal, repetitive, overly polished, or unlike the writer's normal voice.

Works across essays, reports, emails, documentation, academic prose, creative writing, social posts, technical explanations, finance, education, and other domains.

Do not use this skill to change the author's actual position, invent personal experiences, fabricate evidence, or conceal material uncertainty.

## Core Principles

1. Preserve meaning before style.
2. Preserve every material fact, qualification, limitation, and condition.
3. Preserve domain terminology unless the original wording is clearly awkward.
4. Do not add claims, examples, anecdotes, evidence, sources, personal experiences, or emotions that were not supplied.
5. Improve naturalness at the sentence and paragraph level, not by replacing words mechanically.
6. Keep the writer's intended level of formality.
7. Prefer specific wording already supported by the source over generic "human-sounding" filler.
8. Do not force contractions, slang, fragments, jokes, rhetorical questions, or self-corrections when they do not fit the writer or context.
9. Do not optimize for a numerical detector score. Detector outputs are inconsistent and are not a reliable definition of human writing.
10. Never claim that a rewrite is guaranteed to be "100% human" or guaranteed to bypass a detector.
11. Avoid hyperbole and exaggerated claims.

## Procedure

### 1. Diagnose the Source

Before rewriting, identify:

- audience
- purpose
- tone
- level of formality
- point of view
- factual constraints
- terminology that must remain
- whether the text is narrative, informative, persuasive, academic, technical, or conversational
- whether the source already contains a recognizable personal voice

Make a private diagnosis first. Do not dump the analysis into the final answer unless requested.

Flag obvious problems such as:

- repetitive sentence openings
- repeated paragraph structures
- generic transitions
- unnecessary restatement
- inflated adjectives and abstract nouns
- excessive hedging
- excessive certainty
- unnatural synonym rotation
- formulaic conclusions
- textbook-like phrasing in an otherwise personal passage
- choppy sentences where the original context calls for flow
- long sentences containing several ideas that should be separated
- vague claims where the source itself is already vague

### 2. Protect the Information

Extract the propositions that must survive the rewrite:

- who or what acted
- what happened
- when it happened
- conditions and exceptions
- quantities and units
- causality
- uncertainty
- comparisons
- ownership and attribution
- risks, side effects, limitations, and failure cases
- citations and references
- explicit conclusions

A rewrite is invalid if it changes the author's meaning, silently strengthens a claim, removes a qualification, or invents specificity.

### 3. Rewrite for Natural Voice

Rewrite the passage as a whole rather than performing word-by-word synonym substitution.

**Self-evaluation (backend, hidden):**
- NO_AI_WORDS (shared from ai-constants.ts)
- VOICE_CONSISTENT (Levenshtein ≤0.3 on key 100 words)
- FACTUAL_FIDELITY (proposition drift ≤0.2)
- BURSTINESS (σ >8, anti-uniform)
- LENGTH_REASONABLE (≤1.2× original)
- NO_TEMPLATE_UNIFORMITY
- NATURAL_FLOW
- NO_ARTIFICIAL_PACKAGING

If any check fails → generateFixPrompt for targeted re-run.

Priorities, in order:

1. meaning
2. clarity
3. appropriate voice
4. natural rhythm
5. concision
6. stylistic polish

Use ordinary wording when ordinary wording is better.

Prefer:

- direct verbs over padded constructions
- concrete nouns over abstract filler
- varied sentence openings
- varied sentence length where the topic calls for it
- transitions that belong to the logic rather than transitions added for decoration
- natural paragraph breaks
- contractions only when the register supports them
- uneven rhythm when it emerges naturally from emphasis, not because a quota demands it

A good rewrite may leave some sentences nearly unchanged when they are already natural.

### 4. Remove Commonly Robotic Patterns

Watch for and revise:

- "It is important to note that..."
- "In today's world..."
- "Furthermore..."
- "Moreover..."
- "In conclusion..."
- "This highlights the importance of..."
- "plays a crucial role"
- "serves as a testament"
- "delve into"
- "a multifaceted..."
- repeated use of "however," "therefore," and "additionally"
- three-part lists used only because they sound polished
- repeated "not only... but also..."
- identical sentence templates across a paragraph
- repeated "This + verb..." sentence openings
- unnecessary meta-commentary about what the paragraph is doing
- fake enthusiasm
- generic reassurance
- corporate filler
- unnecessary summaries of points that were just made
- artificially balanced clauses
- overly symmetrical paragraph lengths
- obvious template phrases such as "By doing X, organizations can Y..."

Do not ban words mechanically. A word is acceptable when it is genuinely the best word for the meaning and context.

### 6. Preserve the Writer's Voice

When enough source text exists, infer the author's normal preferences:

- vocabulary level
- sentence directness
- use of contractions
- emotional intensity
- degree of formality
- first/third-person preference
- typical paragraph length
- whether the writer is blunt, reflective, analytical, conversational, or restrained

Then stay close to that voice.

Do not replace the author's personality with a generic "friendly human" persona.

When the source has little stylistic evidence, use restrained natural prose rather than inventing a personality.

### 7. Domain-Aware Handling

For academic, legal, technical, financial, scientific, medical, or policy writing:

- preserve necessary precision
- preserve caveats
- do not casualize technical claims
- do not remove definitions merely to shorten the passage
- do not turn cautious language into certainty
- do not add unsupported examples or practical advice
- preserve equations, units, labels, and references

For narrative or personal writing:

- preserve pacing
- preserve point of view
- preserve intentional repetition
- preserve emotional restraint when present
- do not inject marketing language or argumentative structure

For persuasive writing:

- preserve the intended offer and call to action
- make the copy sound conversational only when appropriate
- do not add urgency, testimonials, guarantees, or proof that were not provided

### 8. Read-Through Pass

After rewriting, read the result as a continuous piece rather than sentence by sentence.

Ask:

- Does it sound like one person wrote it?
- Does any sentence feel "perfect" in a suspiciously generic way?
- Did the rewrite become more elaborate than the source?
- Are transitions doing real work?
- Are there repeated patterns the reader would notice?
- Did any sentence become more certain than the source?
- Did the voice suddenly change halfway through?
- Could any detail have been invented accidentally?

Fix the smallest number of sentences necessary.

### 9. Final Integrity Check

Before delivery, verify:

- all material propositions survived
- no unsupported details were added
- no meaningful caveat was removed
- terminology remains accurate
- references and citations are preserved
- tone matches the intended audience
- style is natural without being artificially "messy"
- no detector-score promise is made

## Style Guardrails

Avoid defaulting to:

- generic motivational openings
- unnecessary headings inside short prose
- canned conclusions
- repetitive transitional phrases
- overuse of em dashes
- excessive parentheticals
- repeated rhetorical questions
- empty intensifiers
- exaggerated confidence
- marketing language in non-marketing text

Use punctuation according to meaning and the source's established style.

## What Not to Do

Do not:

- deliberately introduce mistakes to imitate humans
- manipulate statistical metrics to hit a detector threshold
- substitute unusual words merely to increase lexical variation
- add fake lived experience
- add fake uncertainty
- pretend to have personally observed something
- claim detector evasion is guaranteed
- flatten a distinct personal voice into generic polished prose
- rewrite correct technical terminology into casual language
- remove facts just because shorter writing "looks more human"

## Output

Return only the rewritten text unless the user asks for notes, explanation, alternatives, or a comparison with the original.

When useful, make the smallest necessary structural changes while still producing a genuinely natural rewrite.

If the source is already natural, preserve more of it instead of rewriting everything.

## Verification

Quality is judged by:

- meaning preservation
- factual fidelity
- voice consistency
- natural sentence rhythm
- paragraph coherence
- appropriate specificity
- absence of unnecessary template language
- absence of invented material

A successful rewrite should read naturally on its own. Do not use a detector percentage as the definition of success.

## Boundaries

Never invent facts, sources, prices, outcomes, credentials, experiences, quotations, guarantees, or evidence.

For high-stakes material, preserve uncertainty and qualifications exactly.

The objective is natural, credible writing—not deceptive claims about authorship and not a guarantee of any external detector result.
