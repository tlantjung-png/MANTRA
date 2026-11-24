---
name: web-api-integration
description: Integrate a web data endpoint into project code by inspecting the project, locating configuration and client layer, defining minimal request and response shapes, adding validation and bounds, writing offline tests with a fake transport, and running the relevant test command.
version: 1.0.0
user-invocable: true
---

# Web API Integration

## Use When

Use when the operator asks to add, wire up, or harden a call to a web data
endpoint in an existing project: a new client function, a new data source,
or validation of a response that is currently trusted without checks.

## Procedure

1. Inspect the project: locate the configuration layer, the existing client
   or outbound-call layer, and the tests. Follow the closest existing
   pattern instead of introducing a parallel abstraction.
2. Decide where endpoint settings belong (base URL, timeout, and any
   credentials supplied through configuration or the environment) and
   where the client function should live. Keep the endpoint address and
   secrets out of source files.
3. Define the smallest request and response shapes that cover the fields
   actually consumed. Add validation that rejects missing fields, wrong
   types, and malformed payloads, and add bounds for response size, item
   counts, string lengths, retries, and timeouts.
4. Implement the call in the existing client layer with explicit handling
   for transport failure, timeout, and an invalid or oversized response.
   Keep error messages redacted so they cannot leak credentials.
5. Write offline tests using a fake transport: cover a valid response,
   malformed data, missing fields, an oversized payload, a timeout or
   transport failure, and credential handling. Make no live calls.
6. Run the relevant test command documented by the project and report the
   exact command and its result, including failures that remain.

## Verification

The tests run offline through a fake transport with no network access, and
they exercise the validation and bounds rather than merely naming them. No
credential or secret-bearing address appears in source, logs, or test
fixtures. The relevant test command was run and its exact result is
reported.

## Boundaries

Keep credentials out of source and logs; read them from configuration or the
environment and redact them in errors. No network in tests and no live calls
used as a shortcut to verify behavior. Do not add a new runtime dependency
unless the operator approves it; prefer the primitives already in the
project. If the endpoint contract is unclear, stop and ask rather than
inventing one.
