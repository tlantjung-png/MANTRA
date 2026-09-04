---
name: ui-verify
description: Verify web and UI changes end to end in the browser - exercise the changed flow like a real user, hunt cross-page regressions, check edge states, and confirm desktop and mobile viewports.
version: 1.0.0
user-invocable: true
---

# UI Verify

## Use When

Use whenever a change touches what a user sees or interacts with - components, layout,
styling, routing, client state, or the data pages render - before declaring it done.

## Procedure

1. Open the running app with browser tools and exercise the changed feature end to
   end the way a user would: click, type, submit, navigate. A single render screenshot
   of the changed screen is not verification - confirm behavior, not just appearance.
2. Visit every page and route that shares the state, data, or components you touched;
   application state must stay consistent everywhere a surface reads it.
3. Hunt for regressions: the dominant failure mode is a change that works in isolation
   and breaks existing behavior elsewhere. Navigate the surrounding flows looking for
   what broke.
4. Verify the paths your change touches beyond the happy path: empty states, error
   states, route and flag variants.
5. When layout or styling changed, check desktop and mobile viewport sizes.
6. If verification finds a problem, fix it and re-verify; do not finish with
   unverified UI work.

## Verification

Report the flows exercised (not screenshots taken), every shared-state page visited,
the edge states checked, both viewports when visual, and any problem found plus its
fix. When no browser tools exist, verify through the closest substitute (tests,
curl against the dev server, rendering scripts) and state explicitly what could not
be verified.

## Boundaries

Never declare UI work complete on unverified rendering. Do not mock the transport for
a verification run unless the operator asked for a fixture demonstration (see imagine
for truthful recording rules).
