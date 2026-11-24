# Interfaces

## Language-Model Client

The language-model contract accepts a message list, an optional list of tool schemas, and an optional callback for streamed content fragments. It returns a normalized response containing either a final answer or tool invocations with an identifier, name, and arguments, plus usage metadata with prompt, completion, and cached token counts. A response with no tool calls is final.

The callback receives content fragments as they arrive. This contract is the only point where the core contacts the language-model service, so different services and offline replay can be substituted without changing orchestration.

## Sandbox

The sandbox contract defines lifecycle and file operations. Provisioning validates inputs, fetches the repository, and runs setup. Execution runs a shell command with a timeout and returns an exit status, standard output, standard error, and a timeout flag, with external abort checked before and during execution.

File operations read and write paths relative to the workspace and reject escapes through resolved-path checks. Reads are capped and truncated. Writes are validated for size and for parent-chain confinement including symbolic-link checks and re-resolution, and stage through a uniquely named temporary file before the atomic replace. Cleanup is idempotent and distinguishes sandboxes that own their directory from those given an existing workspace. A command-screening hook lets a sandbox reject commands before execution so all execution paths share one implementation; the default accepts everything.

## Tool

The tool contract defines one capability with a name, description, and parameter schema. Execution runs against a sandbox and returns an observation string that is appended to the conversation. The schema is passed to the model as part of the function specification and is deep-copied per call so callers cannot corrupt a tool's declared parameters through mutation. A tool with no declared parameters materializes a fresh empty schema per call rather than sharing a mutable default.

Tools share a ledger for read-before-edit enforcement and partial-view tracking, and are instantiated through the registry from short names with alias normalization and deduplication.

## Evaluator

The evaluator contract inspects the sandbox after the loop finishes. It returns a pass or fail verdict, a descriptive detail string, and optional metrics, and it must never raise.

One implementation runs a shell command and treats a zero exit status without timeout as a pass, with per-task override support. The other always reports a neutral result for interactive sessions without automated grading. The orchestrator ensures a verdict is produced even when evaluation itself raises.

## Logger and Event Bus

The logger contract receives structured events with a name and payload and never propagates input or output errors to the caller. An implementation appends one record per line with a timestamp and uses in-process and inter-process locks with verified stale handling. Closing is optional and idempotent.

The event bus is a concrete class rather than an abstract contract. It allows subscription and unsubscription and fans out synchronously. Handler exceptions are suppressed so one broken observer cannot fail the run or the other observers, and the failure is reported to standard error so it is never silent.
