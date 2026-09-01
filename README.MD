# Overview

Modular harness that delegates tasks to a language model. Provides a sandboxed workspace, file and command tools, and automated grading via a test command. Available as an interactive console or a headless runner.

The interactive console maintains a persistent workspace across turns, retains conversation history within configurable bounds, and supports inline references to workspace files. The headless runner executes one task from start to evaluation and records a structured event log.

See the documentation directory for architecture, setup, API, deployment, and module guides.

# Quick Start

The console is the recommended entry point for interactive use. It can be launched from the project root without installation or via the installed entry point. Upon launch it shows model, endpoint, workspace, and version-control status, and it initializes the workspace directory if needed. If no endpoint is configured, it guides the operator through adding one by supplying a base address and a credential. Once configured, a natural-language request is entered at the prompt; the system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace, and the durable memory for the workspace is appended automatically after each turn.

For non-interactive use, the headless runner is invoked with a configuration document and a task document. The runner provisions the workspace, executes the orchestrator loop, grades the result with the configured evaluator, writes an event log, and exits with a status indicating whether the automated grading passed. Relative log paths are anchored to the project root, so the same configuration works from any working directory.

# Documentation Map

- Architecture Guide — high-level design, module interactions, and trade-offs.
- Setup Guide — prerequisites, installation, and local development.
- API Reference — public interfaces, configuration sections, and commands.
- Deployment Guide — build, testing, and operational runbooks.
- Configuration — loader behavior, validation, and user-wide settings.
- Console — terminal interface, session management, and interactive commands.
- Core Domain — orchestration, context, knowledge, approvals, and events.
- Implementations — concrete clients, sandboxes, tools, and loggers.
- Interfaces — abstract contracts that the core depends upon.

# Architecture Summary

The system is organized into four layers. The core layer holds orchestration and state management and depends only on abstract contracts. The interface layer declares those contracts. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and user-facing entry points. The central orchestrator is the only stateful component in the core and is constructed via dependency injection. Component assembly is performed by a registry that maps short names to concrete classes, validates unknown names at startup, and shares a single edit ledger across file tools. Conversation history is bounded and turn-aware, and the system prompt is assembled from base instructions, environment facts, known-failure knowledge, durable memory, and repository instructions, each subject to size caps.

# Safety Notes

Host sandbox runs in the workspace folder. Validates inputs, blocks traversal and expansion, enforces path confinement, and caps output size. Container sandbox manages a disposable container with limits and owner-only staging. Neither is a complete security boundary. Credentials resolve from environment then the restricted store, and only masked forms are shown.

# Tests

The offline suite exercises the orchestration loop, context truncation, configuration validation, the read-before-edit contract, memory capping and truncation handling, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification, session persistence, and file-tool safety checks. Running the suite requires no network access or credentials. Live probes are available for end-to-end verification with valid credentials and network access when desired.
