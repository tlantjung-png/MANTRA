# MANTRA

## Overview

MANTRA is a modular harness that delegates coding and analysis tasks to an external language model. It provides a sandboxed workspace, a set of file and shell tools, and automated grading through a configurable test command. Two modes are available: an interactive console for turn-based work in a persistent workspace, and a headless runner that executes a single task from provisioning through evaluation and then exits.

The interactive console maintains a persistent workspace across turns, retains conversation history within configurable bounds, and supports inline references to workspace files using an at-sign prefix. The console is an application-owned terminal surface: every frame is composed into a cell grid and diff-flushed, one input thread feeds a single event loop, and text selection, scrolling and the prompt box are all drawn by the application itself - so it behaves identically on every terminal. It also keeps durable per-workspace memory that is appended automatically after each turn.

The headless runner executes one task from start to evaluation and records a structured event log, returning an exit status that reflects whether the automated grading passed.

## Quick Start

The console is the recommended entry point for interactive use. It can be launched from the project root without installation, or through the installed entry point. On launch it shows the selected model, endpoint, workspace, and version-control status, and it initializes the workspace directory if needed. If no endpoint is configured, it guides the operator through adding one by supplying a base address and a credential. Once configured, a natural-language request is entered at the prompt; the system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace.

For non-interactive use, the headless runner is invoked with a configuration document and a task document. The runner provisions the workspace, executes the orchestration loop, grades the result with the configured evaluator, writes an event log, and exits with a status indicating whether the automated grading passed. Relative log paths are anchored to the project root, so the same configuration works from any working directory.

## Documentation Map

- Architecture Guide - high-level design, module interactions, and trade-offs.
- Setup Guide - prerequisites, installation, and local development.
- API Reference - entry points, interactive commands, and configuration sections.
- Deployment Guide - build, testing, and operational runbooks.
- Configuration - loader behavior, validation, and user-wide settings.
- Console - terminal interface, session management, and interactive commands.
- Core Domain - orchestration, context, knowledge, approvals, and events.
- Implementations - concrete clients, sandboxes, tools, and loggers.
- Interfaces - abstract contracts that the core depends upon.
- Adoption Notes - what was carried forward and what was intentionally left out.

## Architecture Summary

The system is organized into four layers. The core layer holds orchestration and state management and depends only on abstract contracts. The interface layer declares those contracts. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and user-facing entry points. The layers are conceptual, not separate directory trees: the contracts live in `core/types.py`, and the implementations live in the concrete core modules beside it.

The central orchestrator is the only stateful component in the core. It is constructed via dependency injection with fully built collaborators and does not import concrete implementations. Component assembly is performed by a registry that maps short names to concrete classes, validates unknown names and unknown keys at startup, and shares a single edit ledger across file tools.

Conversation history is bounded and turn-aware: the initial system prompt and task are pinned, and the oldest complete turn is removed first so tool results are never orphaned. The system prompt is assembled from base instructions, environment facts, known-failure knowledge, durable memory, and repository instructions, each subject to size caps, with a total cap that is re-applied after per-turn additions such as goals and attached skills.

## Safety Notes

The host sandbox executes directly in the workspace folder. It validates timeouts, blocks traversal and encoded traversal, checks for shell expansions, enforces path confinement via resolved paths and symbolic-link checks, and caps output size. The container sandbox manages a disposable container with resource limits and owner-only staging. Neither is presented as a complete security boundary.

Credentials resolve from the environment first and then from a restricted store, and only masked forms are shown. Background task logs are written to workspace-private or per-process private locations with restricted permissions. File-based persistence uses atomic temporary files and verified stale-lock handling.

## Tests

The offline suite exercises the orchestration loop, context truncation, configuration validation, the read-before-edit contract, memory capping and truncation handling, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification, session persistence, and file-tool safety checks. Running the suite requires no network access or credentials. Live probes are available for end-to-end verification with valid credentials and network access when desired.
