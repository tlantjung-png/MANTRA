# API Reference

## Entry Points

Two entry points are exposed.

The interactive console accepts optional flags to select the configuration document, override the workspace location, handle a single message non-interactively before exiting, override the configured model, set the reasoning effort, override the endpoint address, and select the approval mode, plus a flag to disable styling. It restores the operator's saved active endpoint and model selections at startup when no explicit overrides are given, and it guides a first-time operator through endpoint configuration before opening the prompt.

A third entry point serves this harness's own tools over the external tool protocol on the standard input and output streams, for a client that speaks that protocol. It requires a path to a configuration document and takes an optional workspace directory; it exposes exactly the tools the configuration names, gated by the configured approval mode, and it writes protocol messages to the standard output stream and every diagnostic to the standard error stream.

The headless runner requires paths to a configuration document and a task document. It returns a process exit status: zero when the evaluator passed, one when it failed, and two for configuration or task-file errors. Input paths are resolved against the working directory and then the project root, while relative log paths inside the configuration are anchored to the project root so the same configuration works from any working directory. The runner prints a one-line summary of each lifecycle event, an approvals-policy note, and a final verdict line carrying the task identifier, steps used, stop reason, and elapsed time.

## Interactive Commands

Commands are invoked with a leading slash. The full set is: exit and quit to leave the console with an autosave first; help, or a bare slash, to show the command help; workspace to show the workspace path and files; diff to show uncommitted changes or open the full-screen review; undo to discard changes after confirmation; model to manage endpoints and models, add an endpoint, pick a model, or replace a stored credential; approve to select the approval mode; cost to show token usage and cache metrics; compact to summarize the conversation; clear to clear the conversation while keeping files; sessions to browse, list, or resume saved conversations; skills to discover, attach, and launch skill bundles; mcp to inspect the configured external tool servers; and suggestions to turn the follow-up next-step line off and on. One name per action: commands that duplicated something the harness already does (a separate failure-fix prompt, a conversation exporter beside the autosave, a second verbosity toggle, prompt-sequence macros beside skill bundles, a memory-file viewer, a standing-objective setter beside the conversation's own direction, and a checklist the agent already maintains from inside its replies) were removed rather than kept as aliases, and an unknown command is reported instead of silently ignored. Any other line starting with a slash that looks like a path is handed to the agent rather than rejected.

## Tools Exposed to the Model

The default tool list covers file reading, writing, editing, and directory listing; command execution with shell output reading and background-task termination; code search and file finding; document extraction; version-control diff; and web fetching. Tool names are normalized, meaning case folding and dash conversion, before lookup, and aliases map the legacy extraction name and a dash-free web-fetch name to their canonical forms.

- read_file, write_file, edit_file, list_dir: file operations confined to the workspace, with byte and line caps, binary detection, and atomic writes. Reading supports a window via offset and limit, and also accepts a glob or a comma-separated list of plain names for bulk reads.
- run_command, shell_output, kill_shell: shell execution with timeout and abort, cursor-based output reads, and process-tree termination for background tasks. Background execution requires an explicit boolean opt-in and is only supported by the host sandbox.
- search_code, find_file: literal text search and filename search across the workspace, bounded by result, scan, size, and line-length ceilings.
- extract_document: bounded plain-text extraction from a single structured file, covering markup, JSON, XML, CSV, markdown, and ini-like configuration. Unrenderable formats are reported as a note; malformed structured files fall back to a short raw snippet.
- git_diff: version-control change display. A git_reset tool also exists for opt-in configurations and is classified destructive and confirmed.
- web_fetch: bounded retrieval of a URL with text extraction and private-network blocking.

## File References

File references use a leading at-sign plus a path or pattern. They are resolved relative to the workspace, rejected if outside, and expanded to content, directory listing, or glob matches. Each file is capped, the total attached content is capped, the number of glob matches is capped, and unknown references are reported rather than silently dropped.

## Configuration Sections

The configuration document is divided into sections.

The language model section identifies the provider name, model name, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort, streaming preference, timeout, retry count, and the rate-limit wait budget. A rate-limited request (HTTP 429) is treated as a queue rather than a failure: the client waits out the limit - honouring the server's Retry-After header when present, otherwise backing off exponentially up to thirty seconds - and keeps retrying without spending its ordinary retries, until the wait budget is exhausted or the run is aborted. Each wait is announced to the console so a paused turn explains itself. The sandbox section selects the isolation provider and its resource limits, including a validated image name, memory limit, and workdir for the container option. The tools section lists the names of tools exposed to the language model, with alias normalization and deduplication. The evaluator section selects the grading strategy, test command, and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects the default policy among four modes. The context section limits retained message count and character count, with validation that limits are integers meeting minima. The external tool server section names servers to launch and bridge into the tool list, each with its command, optional working directory, optional environment additions, an enabled flag, and an optional timeout.

Top-level keys control the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity, the next-step suggestion toggle, the external tool servers bridged into the tool list, and skill routing preferences including automatic attachment and bundle launching. Unknown top-level or section keys are rejected.

## Task Document

The task document requires a problem statement. Optional keys provide a repository address, a base commit, a setup command, a setup timeout, a clone timeout, and a task-specific test command that overrides the evaluator configuration.

## Environment Variables

The following environment variables are honored.

- MANTRA_SETTINGS relocates the user-wide settings document.
- MANTRA_CREDENTIALS relocates the restricted credentials store.
- MANTRA_SESSIONS relocates the session transcript directory.
- MANTRA_SKILLS relocates or replaces the skills roots used for discovery, split on the semicolon separator.
- MANTRA_RULES_FILE adds a command-rules file that the approval policy consults.
- MANTRA_PRE_TOOL_USE_LOG redirects the redacted pre-tool-use approval audit log.
- MANTRA_SCRIPT points at a scripted-conversation document consumed by the scripted language model client for hermetic testing.
- MANTRA_INPUT_DEBUG, when set, appends every input event the terminal backend hands the application to a trace file in the system temporary directory (mantra-input-debug.log), for diagnosing input-routing reports that cannot be reproduced locally.
- MANTRA_COLOR selects the color mode: auto, always, or never.
- MANTRA_FORCE_COLOR, when set to a truthy value, forces styling even when standard output is not a terminal.
- NO_COLOR, when present, disables styling and takes precedence over the other color settings.
- MANTRA_ALLOW_FILE_URL, when set, permits file-scheme repository addresses, which are blocked by default.
- MANTRA_SHELL names the shell reported in environment facts.

## Abstract Interfaces

The language model interface accepts a list of messages, optional tool schemas, and an optional streaming callback. It returns a normalized response that is either a final answer or a collection of tool invocations, each with an identifier, name, and arguments, plus usage metadata including prompt and completion token counts and cached token counts. The streaming callback, when supplied, receives content fragments as they arrive.

The sandbox interface provisions the environment for a task, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Execution results include exit status, standard output, standard error, and a timeout flag. A command-screening hook lets a sandbox reject commands before execution so foreground and background paths share one implementation. Cleanup is idempotent.

The tool interface exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. Schemas are deep-copied per call so callers cannot mutate a tool's declared parameters. File tools share a single edit ledger that enforces read-before-edit within a session and tracks whether the last view was partial.

The evaluator interface examines the sandbox after the orchestrator finishes and never raises. It returns a verdict indicating pass or fail, a descriptive detail string, and optional metrics. The logger interface receives structured events and never propagates input or output errors. The event bus allows subscription and unsubscription of handlers and fans out events synchronously while suppressing handler exceptions to isolate observers.

## Registry

The registry maps short names to concrete classes for language model clients, sandboxes, tools, evaluators, and loggers. Construction forwards only those configuration keys that match constructor parameters and validates that required parameters are present, reporting unknown names, unknown keys, or missing parameters as configuration errors. Tool construction shares a single ledger, deduplicates tools that resolve to the same implementation, and normalizes aliases.
