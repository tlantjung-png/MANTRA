# Setup Guide

## Prerequisites

A supported interpreter version is required as declared in the project manifest. Network access to a compatible language model service is required for live operation. For isolated execution, a container runtime must be available on the host and its command-line interface must be executable. Version-control tooling is expected on the host for workspace initialization and change inspection.

## Installation

The project is installed via the standard packaging mechanism using the manifest in the repository root. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only when using the alternative configuration format.

After installation, two entry points are available: the interactive console and the headless runner. The interactive console can also be invoked directly from the source tree without installation through the provided launcher, which retries with the source directory on the module search path if the first attempt fails.

The console can be launched with overrides for workspace location, model, endpoint address, reasoning effort, and approval mode, and with flags to disable styling or to handle a single message non-interactively.

## Configuration

Configuration is supplied as a structured document. The primary format is supported natively; the alternative format requires the optional parsing library. The loader merges the supplied document deeply into a set of defaults, so that partial documents inherit sensible values for omitted sections.

Required sections are validated, at least one tool must be listed, and every tool entry must be a non-empty name. Enumerated values for approval mode and reasoning effort are checked against allowed sets, and the context section, when present, is validated to be an object with integer limits that meet documented minima. Unknown configuration keys at any level are rejected to surface typos early. The loader also caps the raw file size to prevent unbounded reads.

Secrets are not stored in configuration files. The language model section names a lookup key that is resolved at runtime first from the process environment and then from a restricted credentials store. The store is created with owner-only permissions where the platform supports it and is never written to the main settings file. On platforms where permission bits are not enforced, a one-time warning is emitted. Only masked forms of stored values are displayed.

User-wide endpoint selections live in a hand-editable file in the home directory. An override via environment variable is available for tests. The same mechanism allows redirecting the credentials store, the session transcript directory, and the workflow definitions file.

## Local Development

A workspace directory holds the repository under test. If the directory lacks version-control initialization, an empty repository is initialized automatically. The workspace is reused across turns in interactive mode, and its location can be overridden at launch.

Repository-specific instructions are discovered by searching the workspace root for well-known filenames in a defined preference order, and the first match is used. Per-workspace memory is stored in a hidden directory and capped; oversized single lines are truncated.

The system prompt is assembled from the base instruction, environment facts, known-failure knowledge, the durable memory tail, and any repository instructions, with a total cap that is re-applied after per-turn additions.

The interactive console requires a terminal for full functionality. When standard input or output is not a terminal, the console falls back to plain line input and omits decorative framing. A non-interactive single-message mode is available for scripting and for probes. On Windows the console runs on the record-based input path, which delivers every key as a structured record; on other platforms the byte-stream decoder applies the same rules for escape, delete, modifiers, and pastes, so key behavior does not depend on the platform.

## Verification

The test suite is discovered in a dedicated test directory with the module search path adjusted to include the source directory. Running the suite exercises offline paths without network access or credentials, covering the orchestration loop, history truncation and budgeting, configuration validation including unknown-key rejection, the read-before-edit contract, memory capping, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification including wrapper handling, session persistence with legacy fallback, file-tool safety checks including the resume arithmetic after a byte-budget cut, and the byte-exact input decoder of the terminal layer.

Interactive-layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding, with explicit background opt-in. Live probes are available for end-to-end verification with valid credentials and are not required for the offline suite.
