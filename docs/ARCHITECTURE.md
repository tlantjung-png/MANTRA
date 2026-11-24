# Architecture Guide

## Overall Shape

The system is arranged as four conceptual layers. The layers are ideas rather than separate folders; related code is grouped under the source package.

The core layer owns orchestration and state. It is written against abstract contracts and does not import concrete components. Its parts are the orchestration loop, the conversation-history manager, the system-prompt assembler, the approval policy, and the event bus.

The contract layer declares those abstractions in a single types module. The contracts cover language-model clients, sandboxes, tools, evaluators, and loggers. Each is a small abstract class with a normalized data shape. The event bus is a concrete class because it carries no provider-specific behaviour.

The implementation layer supplies interchangeable concrete components: a network language-model client and an offline replay client, a host-directory sandbox and a container sandbox, a family of file, command, search, extraction, and web tools, two evaluators, and a structured logger.

The outer layer performs assembly and exposes the user-facing entry points. A registry maps names in the configuration to concrete classes and validates them at startup. The configuration loader validates and merges operator input. The interactive console and the headless runner are the two entry points.

## How the Pieces Connect

A task enters through either the console or the headless runner. The console keeps a long-lived session that reuses one sandbox directory, one history manager, one approval policy, and one conversation across turns. The headless runner builds a fresh sandbox for each invocation and maps the configured approval mode to a non-interactive equivalent, because nobody is present to answer a prompt.

The orchestration loop seeds the history, requests a response, and dispatches approved tool calls. Tool execution is delegated to the sandbox, and the resulting observation is appended to the history. Arguments are validated and repaired before execution. Successful repairs are logged; unrepairable arguments are returned to the model as an error observation together with the expected shape. Usage metadata is accumulated for accounting. The loop ends on a final answer, a step limit, an abort, or an error, after which the evaluator inspects the sandbox and the logger records the outcome. An evaluation is always produced, even after errors, and cleanup is attempted in all cases. The event bus distributes lifecycle events to optional observers and isolates observer failures from the run.

The loop also breaks repetitive behaviour by intent. Repeated identical tool calls are blocked after a bounded number of attempts. A single identical retry after a transient failure remains allowed. A successful write or edit invalidates earlier read counters so the natural verification read after a change still runs.

The history manager owns the message list. It pins the initial prompt and task and removes the oldest complete turn first so tool results are never orphaned. Seeding caps both the prompt and the task before insertion, and oversized single messages are truncated with space reserved for the pinned prefix. If the budget is still exceeded, the manager repeatedly truncates the largest message or removes further turns.

System-prompt assembly merges the base instruction, environment facts, known-failure knowledge, durable workspace memory, and repository-specific instructions. Environment facts include the date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state. Each source is capped. Durable memory keeps the tail of the file so recent entries survive. The total prompt is capped again after per-turn additions such as the checklist and attached skills.

The registry maps short names in configuration sections to concrete classes. It rejects unknown names and unknown keys at startup, checks that required constructor parameters are present, shares one edit ledger across file tools, removes duplicate tools that resolve to the same implementation, and normalizes tool-name aliases.

## Design Decisions and Trade-offs

Core operation deliberately uses few third-party libraries. One document-parsing library serves the extraction tool, and the rest of the networking, process, and terminal work uses the standard library. An optional parser is required only for the alternative configuration format. This limits installation drift at the cost of maintaining some utilities in-project.

Isolation is offered at two levels. The host sandbox executes directly in a workspace directory for speed during trusted development. The container sandbox drives a disposable container through the host's container runtime interface for stronger isolation. Neither is presented as a complete security boundary. Both confine paths with resolved-path checks at every file and directory operation, check each component of newly created parent chains for symbolic links, and re-check on the fallback write path. The host execution path also screens traversal, encoded traversal, shell expansions, and home-directory expansion in commands, caps output on byte boundaries, and removes credential-shaped variables from the child environment. Both foreground and background host execution spawn children in a dedicated process group, and every termination path uses one shared tree-kill helper so a timeout or abort also removes descendants. The container timeout path signals only children of its keeper process so the container survives for later calls.

Shared safety-critical behaviour lives in single modules: process spawning, capped pipe reading, and tree killing in one process-utility module; verified stale-lock removal in one lock module; and repository-address and commit validation in one address-checking module. File writes stage through a uniquely named temporary file before an atomic replace so a concurrent process cannot win a race on a predictable staging path. Large command output spilled to files uses owner-only permissions and is pruned by age and count.

The terminal input layer is byte-explicit. A lone escape becomes the escape action, the delete byte maps to backspace, control ranges are dropped rather than inserted as text, and modifier parameters on arrow sequences decode into key modifiers. Bracketed pastes are bounded in memory and delivered as one event with newlines intact; an unterminated paste is cut off at the cap. Submission during a running turn is serialized through the application lock so the busy check and the queue write cannot interleave with the turn-ending path.

Frames are composed as a full cell grid, compared with the previously shown grid, and only changed cells are written. Because no background thread paints at a stale absolute position, a resize is a full reflow and interleaved output cannot tear a frame.

Streaming is optional and callback driven. Content fragments are delivered incrementally when a handler is supplied, and tool-call deltas are accumulated by index before assembly. The parser tolerates occasional malformed fragments but limits both consecutive and total malformed fragments, caps total streamed content and individual argument size, and bounds single-line buffers.

Approval is a separate policy evaluated per tool call with four modes. Read-only operations are always allowed. The front end supplies the confirmation prompt, keeping the policy independent of terminal handling. Positive answers can be remembered for the rest of the session on a per-tool basis. Wrapper invocations are classified by their inner command to avoid double counting, and interpreter one-liners require explicit confirmation because the pattern screen cannot see their payload. Classification fails closed for unknown mutating tools and internal errors. Interactive prompts are serialized through a dedicated lock, and the loop restores the sandbox abort slot after each run so reused sandboxes do not leak cancellation state.

Persistence is file-based and atomic. Settings, credentials, and session transcripts are written through uniquely named temporary files with restricted permissions and then moved into place. Corrupt files and shape mismatches are quarantined under collision-free backup names. Stale locks are handled only after the holder's liveness is checked. Log records and session documents protect timestamps, event names, and names from caller-payload overwrite, and listing caches return copies so callers cannot mutate cached state.

Event and log payloads carry no raw secrets. Tool arguments are redacted and truncated before emission. Redaction covers known prefixes, assignment-like syntax including short values after sensitive key names, high-entropy tokens, and exact matches against stored credentials. The structured run log receives payloads that already exclude raw command output and file contents; the console passes display-worthy observations straight to the renderer. Full command output and background-task logs receive the same redaction before being written to files. Redaction matches known shapes only, so those files remain sensitive.

Web retrieval validates each redirect hop, refuses private and metadata addresses including alternative numeric encodings, resolves the host once and pins the connection to the validated address to close the rebinding window, and caps response size before and after decompression.
