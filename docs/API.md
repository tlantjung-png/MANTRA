# API Reference

## Entry Points

Three entry points are exposed.

The interactive console accepts flags to select the configuration document, override the workspace, handle one message non-interactively before exiting, override the model, set reasoning effort, override the endpoint address, and select the approval mode, plus a flag to disable styling. At startup it restores the operator's saved active endpoint and model when no explicit overrides are present, and it guides a first-time operator through endpoint configuration before opening the prompt.

The headless runner requires a path to a configuration document and a path to a task document. It returns zero when the evaluator passes, one when it fails, and two for configuration or task errors. Input paths resolve against the working directory and then the project root, while relative log paths inside the configuration anchor to the project root so the same configuration works from any directory. It prints a one-line summary of each lifecycle event, a note describing the approval policy, and a final verdict line carrying the task identifier, steps used, stop reason, and elapsed time.

The external-tool entry point serves the harness's own tools over a standard input and output protocol. It requires a configuration document and accepts an optional workspace directory. It exposes exactly the tools the configuration names, applies the configured approval mode to every call, writes protocol messages to the standard output stream, and writes every diagnostic to the standard error stream.

## Interactive Commands

Commands are invoked with a leading slash. The full set is reported by the help command. It covers exiting with an autosave, showing help, inspecting the workspace, showing uncommitted changes or opening the full-screen review, discarding changes after confirmation, managing endpoints and models, replacing stored credentials, selecting the approval mode, showing usage and cache metrics, summarizing the conversation, clearing the conversation while keeping files, browsing and resuming saved sessions, discovering and attaching skills and launching bundles, inspecting configured external tool servers, and leaving the console. Each action has one name; duplicated verbs were removed rather than kept as aliases. An unknown command is reported rather than ignored, while a slash-prefixed token that looks like a path is handed to the agent.

## Tools Exposed to the Model

The default tool list covers file reading, writing, editing, and directory listing; command execution with shell output reading and background-task termination; literal text search and filename search; document extraction; version-control diff display; and web retrieval. Tool names are normalized by case folding and dash conversion before lookup, and aliases map legacy extraction and dash-free retrieval names to their canonical forms.

File tools confine paths to the workspace with byte and line caps, binary detection, and atomic writes. Reading supports a window through an offset and a limit, and also accepts a pattern or a comma-separated list of names for bulk reads.

Command tools execute a shell command with a timeout and abort support, read output by cursor, and terminate whole process trees for background tasks. Background execution requires an explicit boolean opt-in and is supported only by the host sandbox.

Search tools perform literal text matching and filename matching under ceilings for results, scanned files, size, and line length.

Document extraction returns bounded plain text from one structured file, covering markup, structured data, tabular data, markdown, and ini-like configuration. Unrenderable formats are reported as a note and malformed structured files fall back to a short raw snippet.

The version-control tool displays changes. A reset tool exists for opt-in configurations and is classified as destructive and confirmed.

The retrieval tool fetches a bounded address, extracts visible text, and blocks private networks.

## File References

A file reference uses a leading at-sign followed by a path or pattern. References resolve relative to the workspace, are rejected if outside it, and expand to file content, a directory listing, or pattern matches. Each file is capped, the total attached content is capped, the number of matches is capped, and unknown references are reported.

## Configuration Sections

The configuration document is divided into sections.

The language-model section identifies the provider, model, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort, streaming preference, timeout, retry count, and the rate-limit wait budget. A rate-limited response is treated as a queue rather than a failure: the client waits out the limit, honouring a server-supplied wait hint when present and otherwise backing off up to thirty seconds, and keeps retrying without spending its ordinary retries until the wait budget is exhausted or the run is aborted. Each wait is announced to the console.

The sandbox section selects the isolation provider and its resource limits, including an image name, memory limit, and working directory for the container option. The tools section lists the tool names exposed to the model, with alias normalization and deduplication. The evaluator section selects the grading strategy, test command, and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects one of four default policies. The context section limits retained message count and character count with validated minima. The external tool server section names servers to launch and bridge into the tool list, each with a launch command, optional working directory, optional environment additions, an enabled flag, and an optional timeout.

Top-level keys set the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity, the external servers to bridge in, and skill-routing preferences. Unknown top-level or section keys are rejected.

## Task Document

The task document requires a problem statement. Optional keys supply a repository address, a base commit, a setup command, a setup timeout, a clone timeout, and a task-specific test command that overrides the evaluator configuration.

## Environment Variables

The following environment variables are honored.

- MANTRA_SETTINGS relocates the user-wide settings document.
- MANTRA_CREDENTIALS relocates the restricted credential store.
- MANTRA_SESSIONS relocates the session transcript directory.
- MANTRA_SKILLS relocates or replaces the skill roots used for discovery, split on the semicolon separator.
- MANTRA_RULES_FILE names a command-rules file consulted by the approval policy.
- MANTRA_PRE_TOOL_USE_LOG redirects the redacted pre-tool-use approval audit log.
- MANTRA_SCRIPT points at a scripted-conversation document consumed by the offline replay client.
- MANTRA_INPUT_DEBUG, when set, appends each input event handed to the application to a trace file in the system temporary directory for diagnosing input-routing reports.
- MANTRA_COLOR selects the colour mode: automatic, always, or never.
- MANTRA_FORCE_COLOR forces styling when set to a truthy value even when output is not a terminal.
- NO_COLOR disables styling when present and takes precedence over the other colour settings.
- MANTRA_ALLOW_FILE_URL permits file-scheme repository addresses, which are blocked by default.
- MANTRA_SHELL names the shell reported in environment facts.

## Abstract Interfaces

The language-model interface accepts a message list, optional tool schemas, and an optional streaming callback. It returns a normalized response that is either a final answer or tool invocations carrying an identifier, name, and arguments, plus usage metadata with prompt, completion, and cached token counts. A response with no tool calls is final. The callback receives content fragments as they arrive.

The sandbox interface provisions the environment, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Execution results carry an exit status, standard output, standard error, and a timeout flag. A command-screening hook lets a sandbox reject commands before execution so all execution paths share one implementation. Cleanup is idempotent.

The tool interface exposes a name, description, and parameter schema. Execution runs against a sandbox and returns an observation string appended to the conversation. Schemas are deep-copied per call so callers cannot mutate a tool's declared parameters. File tools share one ledger that enforces read-before-edit and tracks partial views.

The evaluator interface inspects the sandbox after the loop finishes and never raises. It returns a pass or fail verdict, a descriptive detail string, and optional metrics. The logger interface receives structured events and never propagates input or output errors. The event bus supports subscription and unsubscription and fans out synchronously while suppressing handler exceptions.

## Registry

The registry maps short names to concrete classes for language-model clients, sandboxes, tools, evaluators, and loggers. Construction forwards only configuration keys that match constructor parameters and checks that required parameters are present. Unknown names, unknown keys, and missing parameters are reported as configuration errors. Tool construction shares one ledger, removes duplicates that resolve to the same implementation, and normalizes aliases.
