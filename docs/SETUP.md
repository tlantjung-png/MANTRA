# Setup Guide

## Prerequisites

A supported interpreter version is required as declared in the project manifest (version 3.10 or newer). Network access to a compatible language model service is required for live operation. Version-control tooling is expected on the host for workspace initialization and change inspection. A container runtime is required only when the container sandbox is used; its command-line interface must be executable on the host.

## Installation

The project is installed via the standard packaging mechanism using the manifest in the repository root. Package discovery is limited to the source directory. One runtime dependency is declared: an XML-parsing library used by the document-extraction tool. An optional alternative parsing library is required only when using the alternative configuration format.

After installation, two entry points are available: the interactive console and the headless runner. On Windows the console can also be invoked directly from the source tree without installation through the provided launcher script. The launcher first probes whether the core package resolves under the launcher's own directory, and when the probe fails it retries with the source directory prepended to the module search path; the probe also rejects a foreign core package from site-packages so a different package cannot hijack the console.

The console can be launched with overrides for workspace location, model, endpoint address, reasoning effort, and approval mode, and with flags to disable styling or to handle a single message non-interactively.

## Configuration

Configuration is supplied as a structured document. The primary format is supported natively; the alternative format requires the optional parsing library. The loader merges the supplied document deeply into a set of defaults, so partial documents inherit sensible values for omitted sections.

Required sections are validated, at least one tool must be listed, and every tool entry must be a non-empty name. Enumerated values for approval mode and reasoning effort are checked against allowed sets, and the context section, when present, is validated to be an object with integer limits that meet documented minima. Unknown configuration keys at any level are rejected to surface typos early. The loader also caps the raw file size to prevent unbounded reads.

Secrets are not stored in configuration files. The language model section names a lookup key that is resolved at runtime first from the process environment and then from a restricted credentials store. The store is created with owner-only permissions where the platform supports it and is never written to the main settings file. On platforms where permission bits are not enforced, a one-time warning is emitted. Only masked forms of stored values are displayed.

User-wide endpoint selections live in a hand-editable file in the home directory. An override via environment variable is available for tests. The same mechanism allows redirecting the credentials store, the session transcript directory, the skills roots, the workflow definitions file, and the command-rules file.

## Local Development

A workspace directory holds the repository under test. If the directory lacks version-control initialization, an empty repository is initialized automatically. The workspace is reused across turns in interactive mode, and its location can be overridden at launch. Workspace inference refuses to use the user home, its parent, or a drive root as the working workspace; such a run falls back to the dedicated workspace folder inside the project.

Repository-specific instructions are discovered by searching the workspace root for well-known filenames in a defined preference order, and the first match is used. Per-workspace memory is stored in a hidden directory under the workspace and capped; oversized single lines are truncated.

The system prompt is assembled from the base instruction, environment facts, known-failure knowledge, the durable memory tail, and any repository instructions, with a total cap that is re-applied after per-turn additions.

The interactive console requires a terminal for full functionality. When standard input or output is not a terminal, the console falls back to plain line input and omits decorative framing. A non-interactive single-message mode is available for scripting and for probes. On Windows the console runs on the record-based input path, which delivers every key as a structured record; on other platforms the byte-stream decoder applies the same rules for escape, delete, modifiers, and pastes, so key behaviour does not depend on the platform.

## Verification

The test suite is discovered in a dedicated test directory with the module search path adjusted to include the source directory, and is run with the standard test runner. Running the suite exercises offline paths without network access or credentials, covering the orchestration loop, history truncation and budgeting, configuration validation including unknown-key rejection, the read-before-edit contract, memory capping, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification including wrapper handling, session persistence with legacy fallback, file-tool safety checks, the byte-exact input decoder of the terminal layer, and the search, document-extraction, and tree-query tools including their workspace-escape checks.

The test harness redirects the session store and the approval audit log to temporary directories for the duration of a run, so no test writes into the developer's real stores. Interactive-layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding, with explicit background opt-in. Live probes are available for end-to-end verification with valid credentials and are not required for the offline suite. Container-sandbox tests require a container runtime on the host.
