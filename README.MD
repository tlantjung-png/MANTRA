# MANTRA

MANTRA is a modular harness that delegates coding and analysis tasks to an external language model. You describe what you want in plain language; the system explores a workspace, edits files through approved tools, runs verification steps, and reports back. It offers two entry points: an interactive console for day-to-day work, and a headless runner that executes one task end to end and exits with a pass or fail status.

## Prerequisites

- An interpreter of version 3.10 or newer, as declared in the project manifest.
- Version-control tooling on the host for workspace setup and change inspection.
- Network access to a compatible language-model endpoint plus a credential for it. Any endpoint that speaks the common chat-completions protocol works, and an alternate endpoint shape is attempted as a fallback.
- A container runtime is optional. It is only needed for the container sandbox, which confines untrusted work. Trusted daily work uses the host sandbox and needs nothing extra.

One runtime dependency is declared: an XML-parsing library used by the document-extraction tool. All other core operation relies on the standard library. An optional alternative parsing library is required only when configuration documents are written in the alternative markup format rather than JSON.

## Installation

Install the project with the standard packaging mechanism, which exposes the console and headless commands everywhere. On Windows the console can also be launched straight from the repository folder with the provided launcher script without installing anything. The launcher probes whether the package resolves from this repository and prepends the source tree to the module search path when it does not, so both options behave identically. The probe also rejects a foreign package of the same name from the installed site packages, so a different package cannot hijack the console.

## First Run

On launch the console shows the active model, endpoint, workspace, version-control status, approval mode, and tool count, and it creates the workspace directory if needed. The default workspace for a run started inside the project directory is a dedicated subfolder; when started elsewhere, the current directory becomes the workspace, unless that directory is the user home, its parent, or a drive root, which are refused.

If no endpoint is configured yet, the console walks you through adding one: supply a base address and a credential when prompted. You can do the same at any time with the model command, which also lists saved endpoints and replaces stored credentials. Local inference servers on the loopback interface need no credential at all; the setup flow tells you when none is required.

Credentials are never written to the run configuration. They resolve from the process environment first and then from a restricted credential store in your home directory, created with owner-only permissions where the platform supports them. Only masked forms are ever displayed. If a credential is missing, the console warns you up front instead of failing mid-task with an authentication error.

## Daily Use

Type a natural-language request at the prompt and press Enter. The system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace, so you can iterate.

Reference workspace files inline with an at-sign before a path or glob. A reference resolves to a file's contents, a directory listing, or every file a pattern matches, and paths are relative to the workspace and cannot escape it. Approval behaviour follows the selected mode, and the shipped default is yolo, which allows everything without a prompt and is only appropriate in a trusted or disposable workspace; set default to be prompted before destructive actions, auto to allow ordinary changes silently, or plan to refuse every change while exploring and reviewing.

Prompt-box tips. Pressing Enter submits; the shifted Enter inserts a newline. Pasted text never auto-submits; it lands in the box as editable text. You can click to place the caret and drag with the mouse to select text, then type to replace the selection or use the editing keys to remove it. Pressing Escape while a turn is streaming interrupts it, exactly like a single interrupt keypress.

The console maintains a compact command surface, listed in full by the help command:

- model: add or switch endpoints and models, manage stored credentials
- sessions: browse, save, and resume conversations
- workspace: show the workspace location and its files
- diff and undo: show uncommitted changes or open the full-screen review, and discard changes after confirmation
- approve: set the approval mode: yolo (the default), default, auto, or plan; the choice persists across sessions
- cost: show token usage and cache metrics
- compact: summarize a long conversation to free context
- skills: discover and attach skill procedures, and launch bundles
- mcp: inspect the configured external tool servers
- suggestions: toggle the follow-up next-step line shown after each reply
- clear: clear the conversation; files are kept
- exit: leave the console; autosaves first

After every agent reply the console shows a dim line of numbered next steps at the tail of the conversation, derived from what the turn actually did: the files it changed, the tests it ran, and the failure it hit. The line remembers the subject of recent turns, so switching to a new subject still offers a step back to the previous thread, and the suggestions command turns the line off and on.

## Headless Mode

For scripting or continuous integration, run one task non-interactively with a configuration document and a task document; the examples folder holds starting points. The runner provisions the workspace, executes the agent loop, grades the result with the configured test command, writes a structured event log, and exits zero on pass, one on fail, or two on configuration or task-file errors. Input paths are resolved against the working directory and then the project root, while relative log paths inside the configuration are anchored to the project root so the same configuration works from any directory.

In headless mode nobody can answer an approval prompt, so the configured mode maps to its closest non-interactive form: plan refuses every mutation, default and auto allow ordinary mutations but refuse destructive ones and interpreter one-liners, and yolo allows everything.

## Configuration

- Per-run config, selected with the config flag: model connection, sandbox provider, enabled tools, external tool servers to bridge in, evaluator command, logging, approval mode, and context limits. Unknown keys at any level are rejected to catch typos early.
- Per-user settings in the home directory: saved endpoints, the active endpoint and model, and skill preferences. Hand-editable; corrupt files are quarantined to a backup instead of being overwritten.
- Credentials: stored values, readable only by your user account where the platform enforces permissions. Prefer environment variables on shared machines; stored credentials are plaintext protected only by file permissions.
- Sessions: saved conversations under the user directory, overridable for tests.
- Workflows: named ordered prompt sequences in the user directory, overridable for tests.
- Workspace memory: a durable notes file under a hidden folder in the workspace, appended automatically after each turn.

## External Tools

- Outward: the bundled entry point serves this harness's own tools over the external tool protocol on standard input and output, for a client that speaks it. It exposes exactly the configured tools, applies the configured approval mode to every call, and keeps diagnostics off the protocol stream.
- Inward: name external servers in the configuration and their tools join the tool list, prefixed and marked as external. A remote tool always asks for confirmation first, because its effect happens outside this process and the local command screen cannot see it.
- No extra dependency: the transport and message shapes are implemented in this project.

## Safety Notes

- Two sandboxes. The host sandbox runs commands directly in the workspace folder: fast, but not a containment boundary, because its traversal screen is a heuristic and unrecognized command shapes run with the full authority of your user account. The container sandbox runs everything inside a disposable container with memory and processor limits, reduced privileges, and an unprivileged user. Use the container sandbox for untrusted input or repositories you do not control.
- Approvals. The shipped default is yolo, which allows every tool call without a prompt; set default, auto, or plan to require prompts, restrict destructive commands, or refuse changes. Either way every decision is written to a redacted audit log that rotates at a bounded size.
- Path confinement. All file and command tools are confined to the workspace; escapes through parent-directory components or symbolic links are rejected at every operation, parent chains are checked for symbolic links, and writes are atomic: staged to a uniquely named temporary file, then moved into place.
- Secrets. Command text is redacted for known secret patterns before it is written to logs, and the host sandbox strips credential-shaped variables from the child environment. Command output is redacted with the same patterns before it reaches full-output and background-task log files; redaction recognises known shapes only, so still treat log files as sensitive.
- Web fetching. Retrieval validates every redirect hop, refuses private and internal addresses including alternative numeric encodings, and pins the connection to the address that was validated.

## Tests

Run the offline suite, which needs no network or credentials. It covers the agent loop, context budgeting and truncation, configuration validation and unknown-key rejection, file-tool safety, approval classification, session persistence, the streaming parser, model discovery, the terminal input layer, and the search, document-extraction, and tree-query tools. Live probes for end-to-end verification with real credentials are available under the test directory when needed, and container-sandbox tests require a container runtime on the host.

## Workspace Reading Tools

Beyond the file and command tools, the agent has two bounded helpers for reading the workspace without streaming raw bytes into the conversation:

- search_code and find_file: literal substring search across workspace text files, and filename substring search. One shared bounded walk honors ignore files, never descends into ignored or symlinked-out directories, skips files above five hundred kilobytes and non-text extensions, and notes when result or scan ceilings are reached.
- extract_document: reads a single structured file and returns a bounded plain-text representation for common formats such as markup, JSON, XML, CSV, markdown, and ini-like configuration. Formats it cannot render are reported as a note instead of being silently parsed; malformed structured files fall back to a short raw snippet.

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
