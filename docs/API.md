# API Reference

## Entry Points

Two entry points are exposed.

The interactive console accepts optional flags to select the workspace location, override the configured model, endpoint address, reasoning effort, and approval mode, and to disable styling or to handle a single message non-interactively before exiting. It restores the operator's saved active endpoint and model selections at startup when no explicit overrides are given, and it guides a first-time operator through endpoint configuration before opening the prompt.

The headless runner requires paths to a configuration document and a task document. It returns a process exit status: zero when the evaluator passed, one when it failed, and two for configuration or task-file errors. Relative paths for configuration and log locations are resolved against the project root.

## Interactive Commands

Commands are invoked with a leading slash. The full set is listed by the help command.

Workspace inspection commands display the workspace location and contents, the durable memory file, and uncommitted changes, with a confirmation step before discarding changes.

Tool, model, endpoint, and approval commands list or select the available options. They present a menu when no argument is supplied and apply a direct assignment when an argument is supplied. Model commands also offer a thinking-effort choice and complete from the endpoint's own model catalogue. Endpoint commands support adding, switching, listing, removing, and replacing stored credentials, with always-prompted replacement to allow correction of a mistyped credential and with handling for both hidden and visible prompts.

Cost and status commands display token usage, cache metrics, and conversation size in both human-readable and structured forms. History management commands summarize, clear, or reset the conversation while preserving the system prompt and files.

Session persistence commands save the current conversation to a file, load from a file, or resume from automatically saved transcripts, with the most recent transcript listed first and with validation that the target path resides within allowed directories.

Goal and todo commands set, show, and clear a standing objective and a session checklist, both of which are injected into every turn's system prompt. Skill and workflow commands discover, display, attach, and launch procedural bundles. Pasting, step-limit, and verbosity commands control input and execution bounds. The exit command autosaves the session and terminates.

File references use a leading at-sign plus a path or pattern. They are resolved relative to the workspace, rejected if outside, and expanded to content, listing, or glob matches. Each file is capped, the total attached content is capped, the number of glob matches is capped, and unknown references are reported.

## Configuration Sections

The configuration document is divided into sections.

The language model section identifies the provider name, model name, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort, and streaming preference. The sandbox section selects the isolation provider and its resource limits, including validated image name, memory limit, and workdir for the container option. The tools section lists the names of tools exposed to the language model, with alias normalization and deduplication. The evaluator section selects the grading strategy, test command, and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects the default policy among four modes. The context section limits retained message count and character count, with validation that limits are integers meeting minima.

Top-level keys control the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity, and skill routing preferences including automatic attachment and bundle launching. Unknown top-level or section keys are rejected.

The task document requires a problem statement. Optional keys provide a repository address, a base commit, a setup command, a setup timeout, a clone timeout, and a task-specific test command that overrides the evaluator configuration.

## Abstract Interfaces

The language model interface accepts a list of messages, optional tool schemas, and an optional streaming callback. It returns a normalized response that is either a final answer or a collection of tool invocations, each with an identifier, name, and arguments, plus usage metadata including prompt and completion token counts and cached token counts. The streaming callback, when supplied, receives content fragments as they arrive. Implementations cap total streamed content, bound single-line buffers, and enforce limits on both consecutive and total malformed fragments.

The sandbox interface provisions the environment for a task, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Provisioning optionally fetches a repository, validates the repository address and commit identifier, and runs a setup command. Execution results include exit status, standard output, standard error, and a timeout flag. File operations reject escapes via resolved path checks at read, write, and directory-creation time, check each component of newly created parent chains for symbolic links, and cap read and execution output. Cleanup is idempotent and distinguishes between sandboxes that own their directory and those that were given an existing workspace.

The tool interface exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. Tool schemas are passed to the language model as part of the function-calling specification. File tools share a single edit ledger that enforces read-before-edit within a session and tracks whether the last view was partial.

The evaluator interface examines the sandbox after the orchestrator finishes. It returns a verdict indicating pass or fail, a descriptive detail string, and optional metrics. One implementation runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support and tail truncation. The other always reports a neutral result for interactive sessions without automated grading.

The logger interface receives structured events and never propagates input or output errors. Implementations append one record per line with a timestamp and use both in-process and inter-process locks with verified stale handling. The event bus interface allows subscription of handlers and fans out events synchronously while suppressing handler exceptions to isolate observers.

## Registry

The registry maps short names to concrete classes for language model clients, sandboxes, tools, evaluators, and loggers. Construction forwards only those configuration keys that match constructor parameters and validates that required parameters are present, reporting unknown names, unknown keys, or missing parameters as configuration errors. Tool construction shares a single ledger, deduplicates tools that resolve to the same implementation, and normalizes aliases.
