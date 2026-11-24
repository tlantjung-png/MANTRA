# Core Domain

## Orchestrator

The orchestrator is the only stateful core component. It runs the provision, conversation, and tool-dispatch loop, is built through dependency injection, and reaches all collaborators through abstract interfaces.

The loop supports optional history reuse for multi-turn conversations, external abort signalling propagated to the sandbox and checked between steps and inside the streaming callback, and per-tool approval through a pluggable policy. It accumulates token and cache usage with flags marking estimated or unrecognized values rather than inventing numbers.

Responses are checked for the expected shape, tool-call identifiers are deduplicated within a turn with guaranteed uniqueness, and tool arguments are serialized safely. Empty final replies and replies cut off mid-tool-call are treated as transient and retried with a nudged context a bounded number of times before failing with actionable advice.

Intent-based loop breaking uses the tool name plus a canonical form of its arguments, with a bounded registry that is pruned to prevent unbounded growth. A successful write or edit invalidates earlier reads of that path so a verification read is allowed. A failed execution keeps one identical retry open so recovery from a transient error is not punished.

The base system prompt requires every final answer to end with a short plain-language summary of what was done, the outcome, what remains uncertain, and the next step, written as prose rather than a code block or raw tool output. The step-limit wrap-up carries the same requirement.

An evaluation is always produced, even after errors, timeouts, or aborts, and cleanup is attempted in all cases. Events are emitted for run start, tool calls, tool results, denied tools, repaired arguments, run errors, and run end. The final result is logged best-effort so a logger failure never turns a completed run into an exception. Tool arguments on event and log payloads are redacted and truncated, and the raw observation reaches the front end through a display-only callback rather than through the payload or run log.

The copy appended to the model's history is reshaped first. Command and background-task output is collapsed and capped, while output the model edits against, above all file reads, is only capped so its anchors still match the file. Both copies keep the leading error and exit header verbatim because failure tracking keys off it, and reshaping runs after redaction so it cannot rejoin a credential the redactor split across lines.

Eviction from the bounded history is lossless. The history manager detaches the turns it drops instead of discarding them, the loop summarizes the detached batch between steps, and the digest rides in every later request. A summarizer that fails leaves the previous digest in force, which matches the behaviour of a run without one.

## History Management

The history manager owns the message list sent to the model. It retains the system prompt and initial task, enforces limits on message count and total characters with validation that limits are integers meeting minima, and removes the oldest complete turn first to avoid orphaned tool results, with fallbacks that remove the oldest non-tool message and then the oldest tool message. The newest assistant turn is exempt from eviction while it is the last message, because its tool results are appended later and evicting it early would orphan them.

It seeds the conversation, appends messages with truncation of oversized single messages that reserve space for the pinned prefix, replaces the body with a summary while preserving the system prompt, recomputes size after external edits, and re-applies the budget after a saved session is loaded wholesale. Seeding applies the same single-message cap as appending so an oversized initial prompt cannot bypass bounding by being present first. When still over budget after removing turns, it repeatedly truncates the largest message or removes further turns until within budget or only a minimal exchange remains. Token estimation derives from the same character size model as the character budget so the two cannot drift apart.

## System-Prompt Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable workspace memory, and repository-specific instructions.

Environment facts include the date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state, with handling for non-repository directories and failed status probes. Version-control facts are cached briefly per workspace so rebuilding the prompt does not spawn a fresh process each time. The interactive session also injects a bounded workspace listing and a readme snippet.

Repository instructions are discovered by searching the workspace root for well-known filenames in a defined preference order. Each source is capped. Durable memory keeps the tail of the file to preserve recent entries and truncates oversized single lines. The final prompt is capped.

Appending durable memory uses a per-process lock and an inter-process lock file with verified stale handling, re-reads the file after acquiring the lock to avoid lost updates, prunes oldest lines while over the cap, and writes atomically with restricted permissions. Its lock-wait threshold is slightly longer than other stores because appends run on the interactive turn path.

Memory entries carry lifecycle metadata. A planner classifies a new entry as a duplicate to skip, a replacement that marks an older entry superseded, or a fresh append. Filtered reads exclude superseded and stale entries from prompt injection. A relevance read adds older entries that overlap significant words in the current request.

## Model Discovery

A separate module fetches the model catalogue advertised at the endpoint, filters out entries that chat completion cannot use, ranks live names before dated snapshots, and offers a thinking-effort choice for models whose names suggest reasoning. The reasoning match is a hint for offering a choice, not a gate, so a wrong guess costs one extra prompt. Catalogue responses are capped at the same size limit as chat responses so a hostile endpoint cannot exhaust memory through the listing. Failures are reported with plain-English advice distinguishing a refused credential from a missing catalogue.

## Approvals

The approval module classifies each tool invocation as safe, mutating, confirm, or destructive. File writes and edits are mutating. Certain shell commands are destructive through pattern matching that covers removal, formatting, process control, privilege escalation, and destructive version-control operations. Safe commands come from a separate pattern set covering listing, inspection, and tests. Interpreter one-liners and wrapper invocations whose payload the screen cannot see are classified as confirm.

Four modes differ in which categories are auto-allowed, and one mode refuses all mutations. Wrapper invocations are classified by their inner command to avoid double counting. A rules file can forbid or prompt on command patterns: regular-expression rules match the whole command and token-sequence rules match per segment. Prompting forces explicit confirmation, and allowing only relaxes an ordinary mutating verdict, never one the destructive screen caught. An unreadable or corrupt rules file fails closed to explicit confirmation. Writes to instruction or memory files are guarded by patterns covering both redirection and content-setting commands, and tool-level writes to those files classify as confirm.

The policy remembers positive answers marked to persist for the rest of the session on a per-tool key basis, with distinct keys for commands, file paths, and background-task targets, and it suppresses exceptions from the prompting callback. Pre-tool-use decisions are logged with redaction covering known prefixes, assignment-like syntax including short values after sensitive key names, high-entropy tokens, and exact matches against stored credentials. Size-based rotation uses a sampling counter guarded against concurrent updates.

## Events and Exceptions

The event bus fans out synchronously to subscribed handlers and suppresses handler exceptions to isolate observers. A failing handler is reported to standard error with its name and a bounded error snippet so the failure is never silent. The exceptions module defines distinct types for harness, configuration, tool, sandbox, model, evaluation, and abort conditions so the orchestrator can distinguish operator interruption from other failures. The model error type carries a retryable-truncation flag set by clients whose stream dropped mid-tool-call so the loop can treat it as transient.

## Tool Argument Validation

The repair module validates and repairs tool-call arguments before execution. It resolves aliases against the tool's expected vocabulary, strips nulls for optional fields, unwraps markdown links, parses schema-typed strings, wraps a bare string where an array is expected, coerces numeric fields with strict semantics that reject trailing characters and fractional values for integers, and repairs single-backslash paths in string literals. It also supplies relational defaults for windowed reads. The design is to validate first, repair only on issue paths, then re-validate. Successful repairs are logged and unrepairable arguments return to the model as an error observation with the expected schema.

## Workspace Boundary Helpers

Pre-edit snapshots join onto the workspace through the same resolved-path confinement as other workspace reads, so a model-supplied path cannot widen the snapshot read. Workspace inference refuses to turn the user home, its parent, or a drive root into a working workspace on any platform, with the drive-root check applied only where a drive concept exists. Session save and load compare resolved paths against an allowlist case-insensitively because the target platforms treat differing case in a path segment as the same file.
