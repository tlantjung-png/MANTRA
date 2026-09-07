# MANTRA

## Overview

MANTRA is a modular harness that delegates coding and analysis tasks to an external language model. It provides a confined workspace, a set of file, command, search, and web tools, and automated grading through a configurable test command. Two modes are available: an interactive console for turn-based work in a persistent workspace, and a headless runner that executes a single task from provisioning through evaluation and then exits.

The interactive console keeps a persistent workspace across turns, retains conversation history within configurable bounds, and supports inline references to workspace files using an at-sign prefix. It draws its own terminal surface: every frame is composed into a cell grid and diff-flushed, one input thread feeds a single event loop, and text selection, scrolling, and the prompt box are all drawn by the application itself, so it behaves identically on every terminal. It also keeps durable per-workspace memory that is appended automatically after each turn.

The headless runner executes one task end to end and records a structured event log, returning an exit status that reflects whether the automated grading passed.

## Quick Start

The console is the recommended entry point for interactive use. It can be launched from the project root without installation, or through the installed entry point. On launch it shows the selected model, endpoint, workspace, and version-control status, and it initializes the workspace directory if needed. If no endpoint is configured, it guides the operator through adding one by supplying a base address and a credential. Once configured, a natural-language request is entered at the prompt; the system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace.

For non-interactive use, the headless runner is invoked with a configuration document and a task document. The runner provisions the workspace, executes the orchestration loop, grades the result with the configured evaluator, writes an event log, and exits with a status indicating whether the automated grading passed. Relative log paths are anchored to the project root, so the same configuration works from any working directory.

## Documentation Map

- Architecture Guide - high-level design, module interactions, and trade-offs.
- Setup Guide - prerequisites, installation, and local development.
- API Reference - entry points, interactive commands, configuration sections, and environment variables.
- Deployment Guide - build, testing, and operational runbooks.
- Configuration - loader behavior, validation, and user-wide settings.
- Console - terminal interface, session management, and interactive commands.
- Core Domain - orchestration, context, knowledge, approvals, and events.
- Implementations - concrete clients, sandboxes, tools, and loggers.
- Interfaces - abstract contracts that the core depends upon.
- Adoption Notes - what was carried forward and what was intentionally left out.

## Architecture Summary

The system is organized into four conceptual layers. The core layer holds orchestration and state management and depends only on abstract contracts. The interface layer declares those contracts. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and the user-facing entry points. The layers are conceptual rather than separate directory trees: the contracts live in a dedicated types module, and the implementations live in the concrete core modules beside it.

The central orchestrator is the only stateful component in the core. It is constructed via dependency injection with fully built collaborators and does not import concrete implementations. Component assembly is performed by a registry that maps short names to concrete classes, validates unknown names and unknown keys at startup, and shares a single edit ledger across the file tools.

Conversation history is bounded and turn-aware: the initial system prompt and task are pinned, and the oldest complete turn is removed first so tool results are never orphaned. The system prompt is assembled from base instructions, environment facts, known-failure knowledge, durable memory, and repository instructions, each subject to size caps, with a total cap that is re-applied after per-turn additions such as goals and attached skills.

## Safety Notes

- Sandbox screening. The host sandbox executes directly in the workspace folder. It validates timeouts, blocks traversal and encoded traversal, checks for shell expansions, enforces path confinement via resolved paths and symbolic-link checks, and caps output size on byte boundaries so multi-byte characters are never split. The screen is defense in depth only and is explicitly not a containment boundary: quoted payloads passed to interpreters run with the full authority of the host account. For untrusted inputs, the container sandbox confines commands to a disposable container with memory and processor limits and reduced privileges, and its timeout handling signals only children of the container keeper so the container itself survives.
- Path confinement. Every file read, write, and directory creation resolves paths against the workspace, rejects escapes, checks each component of newly created parent chains for symbolic links, and re-resolves the full path after directory creation. The fallback write path repeats the full parent-chain check immediately before writing.
- Atomic writes. File-based persistence stages through a uniquely named temporary file before the atomic replace, so a predictable staging path can never be hijacked by a concurrent process. Locks use verified stale handling. Callers receive a persistence status so lock contention is surfaced rather than silently dropped.
- Process-group isolation. Foreground and background command execution share the same screening and the same process-group isolation: a timeout or abort kills the whole process tree through one helper, so orphaned descendants cannot outlive the task.
- Credential handling. Credentials resolve from the environment first and then from a restricted store, and only masked forms are shown. Short values are fully masked. Background task logs and large command-output spill files are written to workspace-private or per-process private locations with restricted permissions.
- Web fetching. Page fetching validates every redirect hop and pins connections to resolved addresses. When the secure opener cannot be constructed, fetching is disabled rather than falling back to unvalidated fetching.
- Log and session integrity. Structured log records and session documents place caller payloads first so timestamps, event names, versions, and names cannot be overwritten by caller-supplied keys.
- Approval fail-closed. Risk classification requires explicit confirmation for unclassified mutating tools and for classifier failures, and interactive prompts are serialized so concurrent requests cannot overwrite each other.
- Session-path allowlist. Session save and load validate resolved paths case-insensitively against an allowlist of directories, matching the platforms where the case of a path segment is not significant.
- Byte-explicit input decoder. The interactive input decoder treats every byte explicitly: a lone escape opens either a control sequence or the escape action, the delete byte edits the prompt like any other terminal, control-one-byte ranges are never inserted as text, modifier parameters on arrow keys decode into key modifiers, and multi-byte characters split across reads are reassembled before decoding.
- Bounded pastes. Bracketed pastes are bounded in memory and delivered whole, so a large paste can neither fill the input queue unboundedly nor leak its marker text into the prompt.

## Tests

The offline suite is run with pytest and exercises the orchestration loop, context truncation, configuration validation, the read-before-edit contract, memory capping and truncation handling, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification, session persistence, file-tool safety checks, and the byte-exact input decoder of the terminal layer. It requires no network access or credentials. The read tool's resume-offset behavior after a byte-budget cut, including the line recount when a cut lands mid-line, is covered by the suite. Live probes are available for end-to-end verification with valid credentials and network access when desired.
