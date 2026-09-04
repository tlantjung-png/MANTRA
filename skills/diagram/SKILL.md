---
name: diagram
description: Produce a truthful self-contained architecture or data-flow diagram after reading the actual system and its ownership boundaries.
version: 1.0.0
user-invocable: true
---

# Diagram

## Use When

Use for architecture, component, dependency, lifecycle, or data-flow visualization.

## Procedure

1. Read the actual modules, inputs, outputs, state, and ownership boundaries.
2. Run the source scan as a precursor when the goal is to understand a whole repository; use its entry-point and importance digest to choose what to read first.
3. Decide the audience and one visual story.
4. Map only verified components and relationships.
5. Produce an offline self-contained artifact with clear direction and labels.
6. Render or inspect the result and correct overlap, clipping, and misleading arrows.

## Verification

Confirm that every node and edge exists in the source, labels are legible, ownership is clear, and the artifact works without external dependencies.

## Boundaries

Do not invent services, data stores, or ownership. A diagram is documentation of the system, not a proposal disguised as current architecture.
