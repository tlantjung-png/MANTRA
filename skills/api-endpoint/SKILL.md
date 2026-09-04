---
name: api-endpoint
description: Add or review an API endpoint or webhook using the existing contract, validation, authentication, error, and test patterns.
version: 1.0.0
user-invocable: true
---

# API Endpoint

## Use When

Use for a new route, endpoint, webhook, handler, or boundary contract.

## Procedure

1. Confirm the method, path, request shape, response shape, side effects, authentication, authorization, and error contract.
2. Read a nearby endpoint and follow its ownership, validation, logging, and test patterns.
3. Keep the handler thin. Put validation and domain behavior in the layer that already owns the concept.
4. Cover valid input, malformed input, missing authentication, forbidden access, dependency failure, and idempotency where relevant.
5. Run the project diagnostics and contract suites.

## Verification

Report the endpoint contract, exact files changed, boundary cases tested, and any external integration that could not be exercised.

## Boundaries

Do not invent an API contract when the request is ambiguous. Validate all external input and protect every side-effecting or private operation.
