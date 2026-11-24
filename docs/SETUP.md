# Setup Guide

## Prerequisites

An interpreter of version 3.10 or newer is required, as declared in the project manifest. Live operation needs network access to a language-model endpoint that speaks the common chat-completions protocol, plus a credential for that endpoint. Version-control tooling is expected on the host for workspace initialization and change inspection. A container runtime is required only when the container sandbox is selected, and its command-line interface must be executable on the host.

One runtime dependency is declared: a document-parsing library used by the extraction tool. An optional parsing library is required only when configuration is written in the alternative markup format.

## Installation

Install the project with the standard packaging mechanism using the manifest at the repository root. Package discovery is limited to the source package. Installation registers two commands: the interactive console and the headless runner.

On Windows the console can also be started directly from the source tree without installation through the supplied launcher script. The launcher checks whether the source package resolves from the launcher's own directory and, when it does not, retries with the source directory prepended to the module search path. The check also rejects a foreign package of the same name so another installation cannot take over the console.

The console accepts launch overrides for the workspace location, model, endpoint address, reasoning effort, and approval mode, plus flags to disable styling or to handle a single message non-interactively before exiting.

## Configuration Basics

Configuration is supplied as a structured document. The primary format is supported natively; the alternative format requires the optional parsing library. The loader merges the supplied document deeply into defaults, so partial documents inherit values for omitted sections.

Required sections must be objects, at least one tool must be listed, and every tool entry must be a non-empty name. Approval mode and reasoning effort are checked against allowed sets. The context section, when present, must be an object with integer limits that meet documented minima. Unknown keys at any level are rejected. The loader caps the raw file size before parsing, and a component section that changes its discriminator starts from the operator's values so the previous component's keys cannot leak into the new constructor.

Web search is optional. It remains disabled until an endpoint is supplied in the web search section and the tool name is added to the tool list. The credential for that endpoint is named by an environment variable, so it is never written into the configuration document. With no endpoint, the tool is absent from the model's tool list and no network request is made.

Secrets are not stored in the run configuration. The language-model section names a lookup key that is resolved at runtime first from the process environment and then from a restricted credential store. The store is created with owner-only permissions where the platform enforces them and is never written to the main settings file. Platforms that do not enforce permission bits produce a one-time warning. Only masked forms are displayed.

User-wide endpoint selections live in a hand-editable document in the home directory. An environment variable overrides its location for tests. The same mechanism can redirect the credential store, the session directory, the skill roots, the workflow definitions file, and the command-rules file.

## Local Development

A workspace directory holds the repository under test. If it lacks version-control initialization, an empty repository is initialized automatically. The workspace is reused across turns in interactive mode and can be overridden at launch. Workspace inference refuses to use the user home, its parent, or a drive root as the working workspace; such a run falls back to the dedicated workspace folder inside the project.

Repository-specific instructions are discovered by searching the workspace root for well-known filenames in a defined preference order, and the first match is used. Per-workspace memory is stored in a hidden directory under the workspace, is capped, and has oversized single lines truncated.

The system prompt is assembled from the base instruction, environment facts, known-failure knowledge, the durable memory tail, and repository instructions, with a total cap that is re-applied after per-turn additions.

The console requires a terminal for full functionality. When standard input or output is not a terminal, it falls back to plain line input and omits decorative framing. A non-interactive single-message mode supports scripting and probes. On the host platform the console requests virtual-terminal input so mouse-wheel reports are delivered instead of alternate-screen arrow keys; the record reader reassembles encoded keys and mouse reports while accepting native records. It checks the input mode during the session and restores mouse reporting if another console client resets it. On other platforms the byte-stream decoder applies the same rules for escape, delete, modifiers, and pastes.

A headless rendering harness can be run as a module against a fake terminal backend. It paints full frames and writes a browser-readable preview and a text grid, which supports visual verification without a graphical environment.

## Verification

The test runner is configured to discover a dedicated test directory and to include the source directory on the module search path. The offline suite needs no network access or credentials. It covers the orchestration loop, history truncation and budgeting, configuration validation and unknown-key rejection, the read-before-edit contract, memory capping, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification including wrapper handling, session persistence with legacy fallback, file-tool safety checks, the byte-exact input decoder, and the search, extraction, and tree-query tools including workspace-escape checks.

The test harness redirects the session store and the approval audit log to temporary directories for the duration of a run, so no test writes into the developer's real stores. Interactive-layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding, with explicit opt-in for background behaviour. Live probes for end-to-end verification with real credentials are available under the test directory and are not required for the offline suite. Container-sandbox tests require a container runtime on the host.
