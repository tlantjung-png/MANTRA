# Architecture Guide

## High-Level Design

The system is structured into four layers that isolate concerns and allow extension without changing core logic.

The core layer holds orchestration and state management. It depends only on abstractions, never on concrete components. Its main pieces are the orchestrator loop, the context manager, the knowledge assembler, the approval policy, and the event bus.

The interface layer declares those abstractions: language model clients, sandboxes, tools, evaluators, loggers, and the event bus. Each contract is a small abstract class with a normalized data shape.

The implementation layer provides interchangeable concrete components for each abstraction. There are two language model clients, two sandboxes, a set of file, command, search, and web tools, two evaluators, and one structured logger.

The outer layer provides assembly, configuration loading, and the user-facing entry points for interactive and headless operation. The registry maps configuration names to implementation classes.

The central orchestrator is the only stateful component in the core. It is built via dependency injection with fully constructed collaborators and does not import concrete implementations. New clients, sandboxes, tools, evaluators, or loggers can be added by implementing an interface and registering the implementation, without modifying the core.

## Module Interactions

A task enters through either the interactive console or the headless runner.

The console maintains a long-lived session that reuses the same sandbox directory, context manager, approval policy, and conversation across turns. The headless runner builds a fresh sandbox for each invocation.

The orchestrator seeds history, requests a response, and dispatches approved tool calls. Tool execution is delegated to the sandbox, and the resulting observation is appended to history. Usage metadata is accumulated for accounting. The loop ends on a final answer, step limit, abort, or error, after which the evaluator inspects the sandbox and the logger records the outcome. An event bus distributes lifecycle events to optional observers.

Conversation history is owned by a dedicated manager that enforces limits on message count and total characters. The initial system prompt and task are pinned, and the oldest complete turn is removed first, ensuring that tool results are never left orphaned. When the character budget is exceeded, the manager repeatedly truncates the largest message or removes additional turns until within budget. Oversized single messages are truncated before insertion, reserving space for the system prompt.

System prompt assembly merges the base instruction, environment facts, known-failure knowledge, durable per-workspace memory, and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location, and version-control branch and dirty state. Each source is capped, durable memory retains the tail of the file to preserve recent entries, and the total assembled prompt is capped. The cap is re-applied after per-turn additions such as standing goals, todo checklists, and attached skills.

Component assembly is performed by a registry that maps short names from configuration sections to concrete classes. The registry validates unknown names and unknown keys at startup, validates that required constructor parameters are present, shares a single edit ledger across file tools, deduplicates tools that resolve to the same implementation, and normalizes tool name aliases.

## Key Decisions and Trade-Offs

Zero third-party dependencies are preferred for core operation. Networking, process execution, and terminal handling rely on the standard library. An optional parsing library is required only for the alternative configuration format. This reduces installation drift at the cost of reimplementing some utilities.

Isolation is offered at two levels. A host directory sandbox executes directly in a workspace folder for speed during trusted development. A container-based sandbox manages a disposable container via the container runtime interface for stronger isolation. Neither is presented as a complete security boundary. Both enforce path confinement via resolved path checks at every file and directory operation and check each component of newly created parent chains for symbolic links. The host execution path also blocks traversal, encoded traversal, shell expansions, and home-directory expansion in commands.

Streaming is optional and callback driven. When a delta handler is supplied, content fragments are delivered incrementally, and tool-call deltas are accumulated by index before being assembled into complete invocations. The parser tolerates occasional malformed fragments but limits both consecutive and total malformed fragments, caps total streamed content and individual tool-argument size, and bounds single-line buffers without line breaks.

Approval is modeled as a separate policy with four modes that are evaluated per tool call. Read-only operations are always allowed. The confirmation prompt is supplied by the front end, keeping the policy independent of terminal handling. Positive answers can be remembered for the remainder of the session on a per-tool key basis. Wrapper invocations are classified by their inner command to avoid double counting.

Persistence is file-based and atomic. Settings, credentials, workflows, and session transcripts are written via temporary files with restricted permissions and then moved into place, with quarantine handling for corrupted files and verified stale-lock handling for concurrent access. Background task logs are written to workspace-private or per-process private locations with restricted permissions and are bounded and pruned.

Tool-call arguments are validated and repaired before execution. Invalid arguments that can be repaired are corrected and logged; those that cannot are returned to the model as an error observation with the expected schema. Event and log payloads never carry raw secrets: arguments are redacted and truncated before emission.
