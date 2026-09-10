# API Reference

## Entry Points

Two entry points are exposed.

The interactive console accepts optional flags to select the configuration document, override the workspace location, handle a single message non-interactively before exiting, override the configured model, set the reasoning effort, override the endpoint address, and select the approval mode, plus a flag to disable styling. It restores the operator's saved active endpoint and model selections at startup when no explicit overrides are given, and it guides a first-time operator through endpoint configuration before opening the prompt.

The headless runner requires paths to a configuration document and a task document. It returns a process exit status: zero when the evaluator passed, one when it failed, and two for configuration or task-file errors. Input paths are resolved against the working directory and then the project root, while relative log paths inside the configuration are anchored to the project root so the same configuration works from any working directory.

## Interactive Commands

Commands are invoked with a leading slash. The full set is: /exit and /quit to leave the console (with an autosave first), /help or a bare / to show the command help, /workspace to show the workspace path and files, /memory to show the memory file, /diff to show uncommitted changes, /fix to send the most recent failure back to the agent for a diagnosis and a suggested fix, /undo to discard changes after confirmation, /model to manage providers and models (add an endpoint, pick a model, replace a stored key), /reasoning and /effort to set the thinking effort, /approve to select the approval mode, /cost to show token usage and cache metrics, /compact to summarize the conversation, /clear and /reset to clear the conversation while keeping files, /sessions to browse, list, or resume saved conversations, /goal to set, show, or clear a standing objective, /todo to manage a session checklist, /workflow to create, show, launch, or remove workflow sequences, /skills and /skill to discover and attach skills, and /verbose to toggle per-tool detail. Any other line starting with a slash that looks like a path is handed to the agent rather than rejected.

## Tools Exposed to the Model

The default tool list covers file reading, writing, editing, and directory listing; command execution with shell output reading and background-task termination; code search and file finding; document extraction and tree querying; version-control diff and reset; and web fetching. Tool names are normalized (case and dashes) before lookup, and an alias maps the legacy extraction name to its canonical form.

- **read_file / write_file / edit_file / list_dir** — file operations confined to the workspace, with byte and line caps, binary detection, and atomic writes.
- **run_command / shell_output / kill_shell** — shell execution with timeout and abort, cursor-based output reads, and process-tree termination for background tasks.
- **search_code / find_file** — literal text search and filename search across the workspace, bounded by result, scan, size, and line-length ceilings.
- **extract_document** — bounded plain-text extraction from a single structured file (markup, JSON, XML, CSV, markdown, ini/toml-ish). Unrenderable formats are reported as a note; malformed structured files fall back to a short raw snippet.
- **query_tree** — file-tree pattern query: literal paths, globs, and a small brace-shaped syntax for structural presence checks. Targets are re-validated against the workspace on every descent step.
- **git_diff / git_reset** — version-control change display and discard with confirmation.
- **web_fetch** — bounded retrieval of a URL with text extraction and private-network blocking.

## File References

File references use a leading at-sign plus a path or pattern. They are resolved relative to the workspace, rejected if outside, and expanded to content, directory listing, or glob matches. Each file is capped, the total attached content is capped, the number of glob matches is capped, and unknown references are reported.

## Configuration Sections

The configuration document is divided into sections.

The language model section identifies the provider name, model name, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort, and streaming preference. The sandbox section selects the isolation provider and its resource limits, including validated image name, memory limit, and workdir for the container option. The tools section lists the names of tools exposed to the language model, with alias normalization and deduplication. The evaluator section selects the grading strategy, test command, and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects the default policy among four modes. The context section limits retained message count and character count, with validation that limits are integers meeting minima.

Top-level keys control the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity, and skill routing preferences including automatic attachment and bundle launching. Unknown top-level or section keys are rejected.

## Task Document

The task document requires a problem statement. Optional keys provide a repository address, a base commit, a setup command, a setup timeout, a clone timeout, and a task-specific test command that overrides the evaluator configuration.

## Environment Variables

The following environment variables are honored.

- MANTRA_SETTINGS relocates the user-wide settings document.
- MANTRA_CREDENTIALS relocates the restricted credentials store.
- MANTRA_SESSIONS relocates the session transcript directory.
- MANTRA_SKILLS relocates or adds an operator-owned skills root used alongside the bundled library.
- MANTRA_WORKFLOWS relocates the workflow definitions file.
- MANTRA_RULES_FILE adds a command-rules file that the approval policy consults.
- MANTRA_PRE_TOOL_USE_LOG redirects the redacted pre-tool-use approval audit log.
- MANTRA_SCRIPT points at a scripted-conversation document consumed by the scripted language model client for hermetic testing.
- MANTRA_COLOR selects the color mode (auto, always, or never).
- MANTRA_FORCE_COLOR, when set to a truthy value, forces ANSI styling even when standard output is not a terminal.
- NO_COLOR, when present, disables styling and takes precedence over the other color settings.
- MANTRA_ALLOW_FILE_URL, when set to a truthy value, permits file-scheme URLs in web fetching, which are blocked by default.

## Abstract Interfaces

The language model interface accepts a list of messages, optional tool schemas, and an optional streaming callback. It returns a normalized response that is either a final answer or a collection of tool invocations, each with an identifier, name, and arguments, plus usage metadata including prompt and completion token counts and cached token counts. The streaming callback, when supplied, receives content fragments as they arrive.

The sandbox interface provisions the environment for a task, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Execution results include exit status, standard output, standard error, and a timeout flag. A command-screening hook lets a sandbox reject commands before execution so foreground and background paths share one implementation. Cleanup is idempotent.

The tool interface exposes a name, description, and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. Schemas are deep-copied per call so callers cannot mutate a tool's declared parameters. File tools share a single edit ledger that enforces read-before-edit within a session and tracks whether the last view was partial.

The evaluator interface examines the sandbox after the orchestrator finishes and never raises. It returns a verdict indicating pass or fail, a descriptive detail string, and optional metrics. The logger interface receives structured events and never propagates input or output errors. The event bus interface allows subscription of handlers and fans out events synchronously while suppressing handler exceptions to isolate observers.

## Registry

The registry maps short names to concrete classes for language model clients, sandboxes, tools, evaluators, and loggers. Construction forwards only those configuration keys that match constructor parameters and validates that required parameters are present, reporting unknown names, unknown keys, or missing parameters as configuration errors. Tool construction shares a single ledger, deduplicates tools that resolve to the same implementation, and normalizes aliases.
