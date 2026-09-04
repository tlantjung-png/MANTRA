# Core Domain

## Orchestrator

The orchestrator is the sole stateful core component. It runs the provision, conversation, and tool-dispatch loop. It is built via dependency injection and interacts with all collaborators through abstract interfaces.

The loop supports optional context reuse for multi-turn conversations, external abort signalling that is propagated to the sandbox and checked between steps and inside the streaming callback, and per-tool approval through a pluggable policy. It accumulates token usage and cache metrics including prompt and completion counts and cached token counts, with estimation fallbacks when a provider reports no usage.

The orchestrator validates that responses have the expected shape, deduplicates tool-call identifiers within a turn with guaranteed uniqueness, and serializes tool arguments safely. Empty final replies and responses cut off mid-tool-call are treated as transient and retried with a nudged context a bounded number of times before failing with actionable advice.

It enforces intent-based loop breaking using the tool name plus a canonical form of its arguments, with a bounded registry that is pruned to prevent unbounded growth, and a successful write or edit invalidates earlier reads of that path so a verification re-read is allowed.

An evaluation is always produced even after errors, timeouts, or aborts, with cleanup attempted in all cases. Events are emitted for run start, tool calls, tool results, denied tools, repaired arguments, run errors, and run end, and the final result is logged best-effort so a logger failure never turns a completed run into an exception.

## Context Management

The context manager owns the message list sent to the language model. It retains the system prompt and initial task, enforces limits on message count and total characters with validation that limits are integers meeting minima, and removes the oldest complete turn first to avoid orphaned tool results, with fallbacks that remove the oldest non-tool message and then the oldest tool message.

It provides operations to seed the conversation, append messages with truncation of oversized single messages that reserve space for the system prompt, replace the body with a summary while preserving the system prompt, and recompute size after external edits. When still over budget after removing turns, it repeatedly truncates the largest message or removes additional turns until within budget or only the system prompt and a minimal exchange remain. Token estimation is based on character count divided by a constant.

## Knowledge Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions.

Environment facts include date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state, with handling for non-repository directories and for status-probe failures. The interactive session additionally injects a bounded listing of workspace files and a snippet of any readme.

Repository instructions are discovered by searching the workspace root for well-known filenames in a defined preference order. Each source is capped to bound prompt size, with durable memory retaining the tail of the file to preserve recent entries and truncating single-line oversize content. The final assembled prompt is capped.

Appending durable memory uses a per-process lock and an inter-process lock file with verified stale handling, re-reads the file after acquiring the lock to avoid lost updates, prunes oldest lines while over the cap, and writes atomically via a temporary file with restricted permissions.

## Model Discovery

A separate module fetches the model catalogue advertised at the endpoint, filters out entries that chat completion cannot use, ranks live names before dated snapshots, and offers a thinking-effort choice for models whose names suggest reasoning. Failures are reported with plain-English advice distinguishing a refused key from a missing catalogue.

## Approvals

The approval module classifies each tool invocation into safe, mutating, or destructive. File writes and edits are mutating, while certain shell commands are classified as destructive via pattern matching that covers removal, formatting, process control, privilege escalation, and destructive version-control operations. Safe commands are recognized via a separate pattern set that covers listing, inspection, and test commands.

Four modes differ in which categories are auto-allowed and which require prompting, with a plan mode that refuses all mutations. Wrapper invocations are classified by their inner command to avoid double counting. A rules file can forbid or prompt on command patterns, and writes to instruction or memory files are guarded by dedicated patterns.

The policy remembers positive answers that were marked to persist for the remainder of the session on a per-tool key basis, with distinct key generation for commands, file paths, and background-task targets, and it suppresses exceptions from the prompting callback. Pre-tool-use decisions are logged with redaction that covers known prefixes, assignment-like syntax, high-entropy tokens, and exact matches against stored credentials, with rotation to bound the audit log.

## Events and Exceptions

The event bus provides synchronous fan-out to subscribed handlers and suppresses handler exceptions to isolate observers. The exceptions module defines a hierarchy with distinct types for harness, configuration, tool, sandbox, language model, evaluation, and abort conditions, allowing the orchestrator to distinguish operator interruption from other failures.

## Tool Argument Validation

The tool-repair module validates and repairs tool-call arguments before execution. It resolves aliases against the tool's expected vocabulary, strips nulls, unwraps markdown links, parses schema-typed JSON strings, coerces numeric fields, and repairs single-backslash Windows paths in JSON string literals. Repairs that succeed are logged; arguments that cannot be repaired are returned to the model as an error observation with the expected schema.
