# MANTRA

MANTRA is a modular harness that delegates coding and analysis tasks to an external language model. You describe what you want in plain language; the system explores your workspace, edits files through approved tools, runs verification steps, and reports back. It works in two modes: an interactive console for day-to-day work, and a headless runner that executes a single task end to end and exits with a pass or fail status.

## Prerequisites

- An interpreter of version 3.10 or newer is required, as declared in the project manifest.
- Version-control tooling is used for workspace setup and for inspecting changes.
- Network access to a language-model endpoint plus an API key for it. Any endpoint that speaks the common chat-completions protocol works.
- A container runtime is optional. It is only needed for the container sandbox, which confines untrusted work. Trusted daily work uses the host sandbox and needs nothing extra.

There is one runtime dependency, an XML-parsing library used when extracting structured documents. All other core operation relies on the standard library; an optional alternative parsing library is needed only when configuration documents are written in the alternative markup format instead of JSON.

## Installation

Get the source first, then pick one of two ways to run it.

Option A, install. Installing exposes the console and headless commands everywhere:

```
pip install .
mantra
```

Option B, run without installing on Windows. Launch the console straight from the repository folder with the provided launcher script. The launcher probes whether the package resolves from this repository and prepends the source tree to the module search path when it does not, so both options behave identically. The probe also rejects a foreign core package from the site-packages directory, so a different package cannot hijack the console.

## First Run

On launch the console shows the active model, endpoint, workspace, and version-control status, and it creates the workspace directory if needed. The default workspace for a run started inside the project directory is a dedicated subfolder; when started elsewhere, the current directory becomes the workspace, unless that directory is the user home, its parent, or a drive root, which are refused.

If no endpoint is configured yet, the console walks you through adding one: supply a base address and an API key when prompted. You can do the same at any time with the model command, which also lists saved providers and replaces stored keys. Local inference servers on the loopback interface need no key at all; the setup flow tells you when none is required.

Keys are never written to the run configuration. They resolve from the environment first and then from a restricted credential store in your home directory, created with owner-only permissions where the platform supports them. Only masked forms are ever displayed. If a key is missing, the console warns you up front instead of failing mid-task with an authentication error.

## Daily Use

Type a natural-language request at the prompt and press Enter. The system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace, so you can iterate.

Useful commands; the help command inside the console lists them all:

- model: add or switch providers and models, manage stored keys
- diff: show uncommitted workspace changes, or open the full-screen review
- undo: discard uncommitted changes after confirmation
- fix: send the most recent failure back for diagnosis and a suggested fix
- sessions: browse, save, and resume conversations
- goal: set, show, or clear a standing objective for the session
- todo: manage a session checklist
- workflow: create and launch ordered prompt sequences
- skills: discover and attach skill procedures
- approve: set the approval mode: default, auto, yolo, or plan
- compact: summarize a long conversation to free context
- memory: show the workspace memory file
- cost: show token usage and cache metrics
- verbose: toggle per-tool detail output
- suggestions: toggle the follow-up chip row shown after each reply
- export: write the conversation to a Markdown or JSON file
- clear: clear the conversation; files are kept
- exit: leave the console; autosaves first

Reference workspace files inline with an at-sign before a path, for example an at-sign followed by a source file path and a question. Approval behaviour follows the selected mode: default prompts for destructive actions, auto allows ordinary changes silently, yolo allows everything and is only for disposable environments, and plan refuses all changes for exploration and review.

Prompt-box tips. Pressing Enter submits; the shifted Enter inserts a newline. Pasted text never auto-submits; it lands in the box as editable text. You can click to place the caret and drag with the mouse to select text, then type to replace the selection or use the editing keys to remove it.

## Headless Mode

For scripting or continuous integration, run one task non-interactively with a configuration document and a task document; the examples folder holds starting points. The runner provisions the workspace, executes the agent loop, grades the result with the configured test command, writes a structured event log, and exits zero on pass, one on fail, or two on configuration or task-file errors. Relative log paths are anchored to the project root so the same configuration works from any directory.

In headless mode nobody can answer an approval prompt, so the configured mode maps to its closest non-interactive form: plan refuses every mutation, default and auto allow ordinary mutations but refuse destructive ones, and yolo allows everything.

## Configuration

- Per-run config, selected with the config flag: model connection, sandbox provider, enabled tools, evaluator command, logging, approval mode, and context limits. Unknown keys are rejected to catch typos early.
- Per-user settings in the home directory: saved endpoints, the active endpoint and model, and skill preferences. Hand-editable; corrupt files are quarantined to a backup instead of being overwritten.
- Credentials: stored keys, readable only by your user account where the platform enforces permissions. Prefer environment variables on shared machines; stored credentials are plaintext protected only by file permissions.
- Sessions: saved conversations under the user directory, overridable for tests.
- Workspace memory: a durable notes file under a hidden folder in the workspace, appended automatically after each turn.

## Safety Notes

- Two sandboxes. The host sandbox runs commands directly in the workspace folder: fast, but not a containment boundary, because its traversal screen is a heuristic and unrecognized command shapes run with the full authority of your user account. The container sandbox runs everything inside a disposable container with memory and processor limits, reduced privileges, and an unprivileged user. Use the container sandbox for untrusted input or repositories you do not control.
- Approvals. The default mode asks before destructive actions. Every decision is written to a redacted audit log.
- Path confinement. All file and command tools are confined to the workspace; escapes through parent-directory components or symbolic links are rejected at every operation, parent chains are checked for symbolic links, and writes are atomic: staged to a uniquely named temporary file, then moved into place.
- Secrets. Command text and tool output are redacted for known secret patterns before being persisted to logs, and the host sandbox strips credential-shaped variables from the child environment. Treat log files as sensitive anyway.

## Tests

Run the offline suite, which needs no network or credentials:

```
python -m pytest tests/ -q
```

It covers the agent loop, context budgeting, configuration validation, file-tool safety, approval classification, session persistence, the streaming parser, model discovery, the terminal input layer, and the search, document-extraction, and tree-query tools. Live probes for end-to-end verification with real credentials are available under the test directory when needed.

## Workspace Reading Tools

Beyond the file and command tools, the agent has three bounded helpers for reading the workspace without streaming raw bytes into the conversation:

- search_code and find_file: literal substring search across workspace text files, and filename substring search. The walk honors ignore files, never descends into ignored or symlinked-out directories, skips files above five hundred kilobytes and non-text extensions, and notes when result or scan ceilings are reached.
- extract_document: reads a single structured file and returns a bounded plain-text representation for common formats such as markup, JSON, XML, CSV, markdown, and ini-like configuration. Formats it cannot render are reported as a note instead of being silently parsed; malformed structured files fall back to a short raw snippet.
- query_tree: a small tree-pattern query for file-tree structure: literal paths, simple globs, and a tiny brace-style shape syntax. A navigation aid, not a code-query engine.

## Documentation Map

- docs/Architecture.md: layers, module interactions, trade-offs.
- docs/Setup.md: prerequisites, installation, local development.
- docs/Api.md: entry points, commands, configuration, environment variables.
- docs/Deployment.md: build, testing, sandbox levels, runbooks.
- docs/Configuration.md: loader behaviour and user-wide settings.
- docs/Console.md: terminal interface and session management.
- docs/Core.md: orchestration, context, knowledge, approvals, events.
- docs/Implementations.md: concrete clients, sandboxes, tools, loggers.
- docs/Interfaces.md: abstract contracts.
- docs/Adoption.md: what was kept and what was left out.

## License

MIT. See the LICENSE file. You are free to use, modify, and distribute this software, including for commercial purposes, as long as the copyright and permission notices are preserved.
