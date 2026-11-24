# MANTRA

## Overview

MANTRA is a modular harness that delegates coding and analysis tasks to an external language model. An operator states a goal in ordinary language. The harness explores a workspace, changes files through a set of approved tools, runs verification steps, and reports the outcome. The project manifest describes it as a modular harness for coding agents.

The project exposes three ways in. An interactive console supports day-to-day work across many turns in one session. A headless runner executes one task from a configuration document and a task document, then exits with a pass or fail status for scripting and continuous integration. A third entry point serves the harness's own tools over an external tool protocol for a separate client to consume.

## Requirements

The package manifest requires an interpreter of version 3.10 or newer. Version-control tooling is expected on the host because workspace setup and change inspection use it. Live operation needs network access to a language-model endpoint that speaks the common chat-completions protocol, together with a credential for that endpoint. A container runtime is optional and is only required when the container sandbox is selected; trusted daily work uses the host sandbox.

One runtime dependency is declared: a document-parsing library used by the extraction tool. Everything else in core operation relies on the standard library. An optional parsing library is needed only when configuration is written in the alternative markup format instead of the primary structured format.

## Installation

Install the project with the standard packaging mechanism declared in the manifest. Installation registers the console command and the headless command. On Windows the console can also be launched directly from the repository folder using the supplied launcher script without installing anything. The launcher checks whether the package resolves from this repository and prepends the source tree to the module search path when it does not, so both routes behave the same way. The check also rejects a foreign package of the same name so another package cannot take over the console.

## First Launch

When the console opens it shows the active model, endpoint, workspace, version-control status, approval mode, and tool count, and it creates the workspace directory if needed. A run started inside the project directory uses a dedicated subfolder as its workspace. A run started elsewhere uses the current directory, except when that directory is the user home, its parent, or a drive root; those locations are refused and the dedicated subfolder is used instead.

If no endpoint has been configured, the console guides the operator through adding one by asking for a base address and a credential. The same flow is available at any time through the model command, which also lists saved endpoints and replaces stored credentials. A local inference server on the loopback interface needs no credential, and the setup flow says when that applies.

Credentials are not written to the run configuration. They resolve first from the process environment and then from a restricted credential store in the home directory, created with owner-only permissions where the platform enforces them. Only masked forms are ever displayed. A missing credential produces a warning at startup rather than a failure partway through a task.

## Everyday Use

An operator types a natural-language request at the prompt and submits it. The harness explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace so work can be iterated. Prompts submitted while a turn is still running are injected into that turn at its next step, so the operator does not need to stop the process. If the turn finishes first, they are queued in order and each is processed next.

Workspace files can be referenced inline by prefixing a path or pattern with an at-sign. A reference resolves to file contents, a directory listing, or every file a pattern matches. Paths are relative to the workspace and cannot escape it.

Approval behaviour follows the selected mode. The shipped default prompts before destructive actions. A permissive mode is available for trusted or disposable workspaces, and another mode refuses every change while exploring and reviewing.

Every turn ends with a short plain-language summary as the last visible element. It states what happened, how many steps ran, how many files changed, the verification status, and what to do next. Raw tool output and code are never the final thing shown. The headless runner and resumed sessions also close with a summary.

The prompt box supports ordinary text editing. Submitting sends the text, while a modified submit inserts a newline. Pasted text is never submitted automatically; it arrives as editable text. The caret can be placed by clicking, text can be selected by dragging, and typing or editing keys replace the selection. Interrupting a streaming turn is available through a key action.

The console exposes a compact command surface covering model and endpoint management, session browsing and resumption, workspace inspection, change display and undo, approval-mode selection, usage and cost reporting, conversation compaction, skill discovery and attachment, inspection of configured external tool servers, clearing the conversation while keeping files, and exiting with an autosave.

## Headless Operation

For scripting or continuous integration, one task runs non-interactively from a configuration document and a task document, with starting examples in the examples folder. The runner provisions the workspace, executes the agent loop, grades the result with the configured test command, writes a structured event log, and returns zero on pass, one on failure, or two for configuration or task errors. Input paths resolve against the working directory and then the project root, while relative log paths inside the configuration anchor to the project root so the same configuration works from any directory.

Nobody is present to answer an approval prompt in headless operation, so the configured mode maps to its closest non-interactive form. The planning mode refuses every mutation. The default and automatic modes allow ordinary mutations but refuse destructive ones and interpreter one-liners. The permissive mode allows everything.

## Configuration

A per-run configuration document selects the model connection, sandbox provider, enabled tools, external tool servers to bridge in, evaluator command, logging, approval mode, and context limits. Unknown keys at any level are rejected so typos surface early.

Per-user settings in the home directory hold saved endpoints, the active endpoint and model, and skill preferences. The file is meant to be hand-editable, and corrupt files are quarantined to a backup rather than overwritten.

Credentials are stored separately, readable only by the owning account where the platform enforces permissions. Environment variables are preferable on shared machines because stored credentials are plaintext protected only by file permissions. Sessions and workflow definitions live in the user directory with overridable locations. Workspace memory is a durable notes file under a hidden folder in the workspace and is appended after each turn.

## External Tools

The bundled external-tool entry point serves the harness's own tools over a standard input and output protocol. It exposes exactly the configured tools, applies the configured approval mode to every call, and keeps diagnostics off the protocol stream.

External servers named in the configuration contribute their tools to the tool list with a prefix that marks them as external. A remote tool always asks for confirmation first because its effect happens outside the local process and the local command screen cannot see it. The transport and message shapes are implemented in the project itself.

## Safety Model

Two sandbox providers are available. The host sandbox runs commands directly in the workspace folder; it is fast but is not a containment boundary, because its traversal screen is a heuristic and unrecognized command shapes run with the full authority of the operator account. The container sandbox runs work inside a disposable container with memory and processor limits, reduced privileges, and an unprivileged user. Untrusted input or repositories outside the operator's control should use the container sandbox.

Every approval decision is written to a redacted audit log that rotates at a bounded size. All file and command tools confine paths to the workspace, reject escapes through parent-directory components and symbolic links, check parent chains for symbolic links, and stage writes through a uniquely named temporary file before moving them into place.

Command text is redacted for known secret patterns before it is logged, and the host sandbox strips credential-shaped variables from the child environment. Command output is redacted with the same patterns before it reaches full-output and background-task log files. Redaction recognises known shapes only, so those files should still be treated as sensitive.

Web retrieval validates every redirect hop, refuses private and internal addresses including alternative numeric encodings, and pins the connection to the address that was validated.

## Workspace Reading Tools

Beyond file and command tools, the harness has two bounded helpers. Literal text search and filename search share one bounded walk that honours ignore files, avoids ignored or symlinked-out directories, skips oversized and non-text files, and reports when result or scan ceilings are reached. Document extraction reads one structured file and returns a bounded plain-text form for markup, structured data, tabular data, markdown, and ini-like configuration; formats it cannot render are reported as a note, and malformed structured files fall back to a short raw snippet.

## Verification

The offline test suite needs no network access or credentials. It covers the agent loop, context budgeting and truncation, configuration validation and unknown-key rejection, file-tool safety, approval classification, session persistence, the streaming parser, model discovery, the terminal input layer, and the search, extraction, and tree-query tools. Live probes for end-to-end verification with real credentials are available under the test directory. Container-sandbox tests require a container runtime on the host.

## Repository Documentation

These documents live in the source repository and are not installed with the package.

- docs/ARCHITECTURE.md: layers, module interactions, and trade-offs.
- docs/SETUP.md: prerequisites, installation, and local development.
- docs/API.md: entry points, commands, configuration, and environment variables.
- docs/DEPLOYMENT.md: build, testing, isolation levels, and runbooks.
- docs/CONFIGURATION.md: loader behaviour and user-wide settings.
- docs/CONSOLE.md: terminal interface and session management.
- docs/CORE.md: orchestration, context, knowledge, approvals, and events.
- docs/IMPLEMENTATIONS.md: concrete clients, sandboxes, tools, and loggers.
- docs/INTERFACES.md: abstract contracts.
- docs/ADOPTION.md: scope decisions and deliberate omissions.

## License

The project is licensed under the terms in the LICENSE file. Use, modification, and distribution are permitted, including for commercial purposes, provided the copyright and permission notices are preserved.
