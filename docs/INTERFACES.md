# Interfaces

## Language Model Client

The language model contract accepts a list of messages, an optional list of tool schemas, and an optional callback for streamed content fragments. It returns a normalized response that contains either a final answer or a collection of tool invocations, each with an identifier, name, and arguments, plus usage metadata including prompt and completion token counts and cached token counts. A response with no tool calls counts as final.

The callback, when supplied, receives content fragments as they arrive. The contract is the only point where the core interacts with the language model service, allowing different services and offline replay to be substituted without changing orchestration.

## Sandbox

The sandbox contract defines lifecycle and file operations. Provisioning validates inputs, fetches the repository, and runs setup. Execution runs a shell command with a timeout and returns exit status, standard output, standard error, and a timeout flag, with support for external abort that is checked before and during execution.

File operations read and write paths relative to the workspace and reject escapes via resolved path checks, with read operations capped and truncated and write operations validated for size and for parent-chain confinement including symbolic-link checks and re-resolution; writes stage through a uniquely named temporary file before the atomic replace. Cleanup is idempotent and safe to call multiple times, distinguishing between sandboxes that own their directory and those that were given an existing workspace. A command-screening hook lets sandboxes reject commands before execution so every execution path shares one implementation; the default accepts everything.

## Tool

The tool contract defines a single capability. It exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. The schema is passed to the language model as part of the function-calling specification, deep-copied per call so callers cannot corrupt a tool's declared parameters through mutation. A tool with no declared parameters materializes a fresh empty schema per call rather than sharing one mutable default.

Tools share a ledger for read-before-edit enforcement and partial-view tracking and are instantiated via the registry from short names with alias normalization and deduplication.

## Evaluator

The evaluator contract examines the sandbox after the orchestrator finishes. It returns a verdict indicating pass or fail, a descriptive detail string, and optional metrics. The contract requires that it never raise.

One implementation runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support. The other always reports a neutral result for interactive sessions without automated grading. The orchestrator ensures a verdict is always produced, even when evaluation itself raises.

## Logger and Event Bus

The logger contract receives structured events with a name and payload and never propagates input or output errors to the caller. Implementations append one record per line with a timestamp and use both in-process and inter-process locks with verified stale handling. Closing the logger is optional and idempotent.

The event bus is a concrete class rather than an abstract contract. It allows subscription and unsubscription of handlers and fans out events synchronously. Handler exceptions are suppressed so one broken observer cannot fail the run or the other observers, and the failure is reported to standard error so it is never silent.
