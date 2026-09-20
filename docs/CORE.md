# Core Domain

## Orchestrator

The orchestrator is the sole stateful core component. It runs the provision, conversation, and tool-dispatch loop. It is built via dependency injection and interacts with all collaborators through abstract interfaces.

The loop supports optional context reuse for multi-turn conversations, external abort signalling that is propagated to the sandbox and checked between steps and inside the streaming callback, and per-tool approval through a pluggable policy. It accumulates token usage and cache metrics including prompt and completion counts and cached token counts, with flags marking when usage was estimated or unrecognized rather than inventing numbers.

The orchestrator validates that responses have the expected shape, deduplicates tool-call identifiers within a turn with guaranteed uniqueness, and serializes tool arguments safely. Empty final replies and responses cut off mid-tool-call are treated as transient and retried with a nudged context a bounded number of times before failing with actionable advice.

It enforces intent-based loop breaking using the tool name plus a canonical form of its arguments, with a bounded registry that is pruned to prevent unbounded growth, and a successful write or edit invalidates earlier reads of that path so a verification re-read is allowed. A failed execution keeps one identical retry open so recovering from a transient error is not punished.

An evaluation is always produced even after errors, timeouts, or aborts, with cleanup attempted in all cases. Events are emitted for run start, tool calls, tool results, denied tools, repaired arguments, run errors, and run end, and the final result is logged best-effort so a logger failure never turns a completed run into an exception. Tool arguments carried on event and log payloads are redacted and truncated, and the raw observation is handed to the front end through a display-only callback rather than placed on the payload or the run log. The copy appended to the model's context is reshaped first: command and background-task output is collapsed and capped, while output the model edits against, above all file reads, is only capped so its anchors still match the file. Both copies keep the leading error and exit-code header verbatim, because the loop's failure tracking keys off it, and reshaping runs after redaction so it cannot re-join a credential the redactor split across lines. Eviction from the bounded history is lossless by the same arrangement: the context manager detaches the turns it drops instead of discarding them, the loop summarises the detached batch between steps, and the resulting digest rides in every later request. A summariser that fails leaves the previous digest in force, which is exactly the behaviour of a run without one.

## Context Management

The context manager owns the message list sent to the language model. It retains the system prompt and initial task, enforces limits on message count and total characters with validation that limits are integers meeting minima, and removes the oldest complete turn first to avoid orphaned tool results, with fallbacks that remove the oldest non-tool message and then the oldest tool message. The newest assistant turn is exempt from eviction while it is still the last message, because its tool results are appended in a later call and evicting it early would orphan them.

It provides operations to seed the conversation, append messages with truncation of oversized single messages that reserve space for the system prompt, replace the body with a summary while preserving the system prompt, recompute size after external edits, and re-apply the budget after a saved session is loaded wholesale. Seeding applies the same single-message cap as appending, so an oversized initial prompt cannot bypass bounding by being present before any append. When still over budget after removing turns, it repeatedly truncates the largest message or removes additional turns until within budget or only the system prompt and a minimal exchange remain. Token estimation is based on character count divided by a constant, derived from the same size model used for the character budget so the two cannot drift apart.

## Knowledge Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions.

Environment facts include date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state, with handling for non-repository directories and for status-probe failures. The version-control facts are cached briefly per workspace so a system-prompt rebuild does not spawn a fresh process each time. The interactive session additionally injects a bounded listing of workspace files and a snippet of any readme.

Repository instructions are discovered by searching the workspace root for well-known filenames in a defined preference order. Each source is capped to bound prompt size, with durable memory retaining the tail of the file to preserve recent entries and truncating single-line oversize content. The final assembled prompt is capped.

Appending durable memory uses a per-process lock and an inter-process lock file with verified stale handling, re-reads the file after acquiring the lock to avoid lost updates, prunes oldest lines while over the cap, and writes atomically via a temporary file with restricted permissions. Its lock-wait threshold is slightly longer than the other stores because appends run on the interactive turn path.

Durable memory entries carry lifecycle metadata. A planner classifies a new entry as a duplicate to skip, a replacement that marks an older entry superseded, or a fresh append, and filtered reads exclude superseded and stale entries from prompt injection. A separate relevance read adds older entries that overlap the current request's significant words.

## Model Discovery

A separate module fetches the model catalogue advertised at the endpoint, filters out entries that chat completion cannot use, ranks live names before dated snapshots, and offers a thinking-effort choice for models whose names suggest reasoning. The reasoning match is a hint for offering an effort choice, not a gate: a wrong guess costs one extra prompt. Catalogue responses are capped at the same size limit as chat responses so a hostile endpoint cannot exhaust memory through the listing. Failures are reported with plain-English advice distinguishing a refused credential from a missing catalogue.

## Approvals

The approval module classifies each tool invocation into safe, mutating, confirm, or destructive. File writes and edits are mutating, while certain shell commands are classified as destructive via pattern matching that covers removal, formatting, process control, privilege escalation, and destructive version-control operations. Safe commands are recognized via a separate pattern set that covers listing, inspection, and test commands. Interpreter one-liners and wrapper invocations whose payload the pattern screen cannot see are classified as confirm.

Four modes differ in which categories are auto-allowed and which require prompting, with a plan mode that refuses all mutations. Wrapper invocations are classified by their inner command to avoid double counting. A rules file can forbid or prompt on command patterns: regular-expression rules match the whole command, while token-sequence rules are matched per command segment, with prompt forcing an explicit confirmation and allow relaxing only an ordinary mutating verdict, never approving something the destructive screen caught. An unreadable or corrupt rules file fails closed to explicit confirmation. Writes to instruction or memory files are guarded by dedicated patterns that cover both redirection and the content-setting commands, and tool-level writes to those files classify as confirm.

The policy remembers positive answers that were marked to persist for the remainder of the session on a per-tool key basis, with distinct key generation for commands, file paths, and background-task targets, and it suppresses exceptions from the prompting callback. Pre-tool-use decisions are logged with redaction that covers known prefixes, assignment-like syntax including short values after sensitive key names, high-entropy tokens, and exact matches against stored credentials, with size-based rotation whose sampling counter is guarded against concurrent updates.

## Events and Exceptions

The event bus provides synchronous fan-out to subscribed handlers and suppresses handler exceptions to isolate observers; a failing handler is reported to standard error with its name and a bounded error snippet so the failure is never silent. The exceptions module defines a hierarchy with distinct types for harness, configuration, tool, sandbox, language model, evaluation, and abort conditions, allowing the orchestrator to distinguish operator interruption from other failures. The language model error type carries a retryable-truncation flag set by clients whose stream dropped mid-tool-call, so the loop can treat it as transient.

## Tool Argument Validation

The tool-repair module validates and repairs tool-call arguments before execution. It resolves aliases against the tool's expected vocabulary, strips nulls for optional fields, unwraps markdown links, parses schema-typed JSON strings, wraps a bare string where an array is expected, coerces numeric fields with strict semantics that reject trailing characters and fractional values for integer fields, and repairs single-backslash Windows paths in JSON string literals. It also supplies relational defaults for windowed reads. Design: validate first, repair only on issue paths, re-validate. Repairs that succeed are logged; arguments that cannot be repaired are returned to the model as an error observation with the expected schema.

## Workspace Boundary Helpers

Pre-edit file snapshots are joined onto the workspace through the same resolved-path confinement as every other workspace read, so a model-supplied tool path can never widen the snapshot read beyond the workspace. Workspace inference refuses to turn the user home, its parent, or a drive root into a working workspace on any platform, with the drive-root check applied only where a drive concept exists. Session save and load compare resolved paths against their allowlist case-insensitively, because the target platforms treat differing case in a path segment as the same file.
