---
name: imagine
description: Plan image generation, image editing, short video workflows, and UI demonstration GIFs with accurate prompts, references, consistency checks, and safe handling of likenesses.
version: 1.1.0
user-invocable: true
---

# Imagine

## Use When

Use for image creation, image editing, image-to-video work, visual prompt development, or recording a browser or Web UI interaction demo as a GIF.

## Procedure

1. Decide whether exact content is better produced deterministically or generated visually.
2. Define subject, composition, style, camera, lighting, constraints, text, and output format.
3. Use references when identity or visual continuity matters and reuse a base image for a sequence.
4. Plan video as short shots with continuity notes rather than one overloaded prompt.
5. Inspect the result for exact text, numbers, labels, clipping, anatomy, structure, and consistency.

## UI Demonstration GIF

When a change affects user-visible GUI behavior, record a short, truthful GIF from the change's real server and real model flow — never substitute fixture queries, mock transports, or test-only hooks unless the user explicitly asks for a fixture recording.

1. Stage per change: require a clean worktree, record the exact commit, build that tree, and boot one server per port from it with fresh state roots. Treat one storyboard as one evidence run; never splice frames from separate runs.
2. Choose three to six states that tell one story (typed, running, settled, detail), keeping one viewport and crop per frame.
3. Wait for a concrete UI condition before each frame — a unique label, enabled control, changed title, or completed response — never a fixed delay. Make completion predicates match an exact-text element, not a substring that the echo of the prompt also satisfies.
4. Include a detail or trajectory frame when the claim involves a tool call, rejection, or recovery, showing the tool identity, status or stable error code, and downstream result.
5. Encode deterministically (fps, width, color count), then visually read the encoded artifact — not only the source frames — and confirm the final state holds and no sensitive content appears.
6. Capture no secrets, personal data, unrelated tabs, or transient notifications.
7. State the provenance: the exact demonstrated commit SHA, whether a real model round ran, and any mode flags or browser-state exceptions.

When the task includes attaching the GIF to a pull request, publish it through a dedicated orphan assets branch containing media only (append-only, never force-pushed), then embed it with the raw blob URL. Report the absolute GIF path and whether the recording used a real API, fixture, or another transport.

## Verification

Report what was generated, what was inspected, and any inaccuracy that remains. Rebuild exact-content assets deterministically when generation cannot preserve the required text or structure.

## Boundaries

Do not generate named real people without an appropriate reference. Do not create non-consensual sexualized likenesses or minor-involving sexual content, and do not evade moderation blocks. Never expose credential values in a recording.
