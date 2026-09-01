# Documentation Bundle

---

## File: README.md

# Overview

The system is a modular harness that delegates work to an external language model. It provides a sandboxed workspace, a set of file and shell tools, and automated grading through a configurable test command. Two modes are supported. The interactive console keeps a persistent workspace and conversation across turns. The headless runner executes a single task from provisioning through evaluation and then exits.

The console retains bounded conversation history, supports inline file references, and maintains durable memory per workspace. The headless runner provisions the workspace, runs the orchestration loop, evaluates the result, and writes a structured event log.

Refer to the documentation directory for detailed guides on architecture, setup, interfaces, configuration, console use, core domain, implementations, and deployment.

# Quick Start

The console is the recommended entry point for interactive use. It can be started from the project root without installation or through the installed entry point. On launch it displays the selected model, endpoint, workspace location, and version control status, and creates the workspace directory if needed. If no endpoint is configured it guides the operator to add one by providing a base address and a credential. After configuration, the operator enters a natural language request at the prompt. The system explores the workspace, makes changes through approved tools, and runs verification steps. Follow-up messages continue the same conversation and workspace. Durable memory for the workspace is appended automatically after each turn.

For non-interactive use, the headless runner is invoked with a configuration document and a task document. It provisions the workspace, executes the orchestrator, grades the result with the configured evaluator, writes an event log, and exits with a status that reflects whether grading passed. Relative log paths are anchored to the project root so the same configuration works from any working directory.

# Documentation Map

- Architecture Guide — high-level design, layering, and trade-offs.
- Setup Guide — prerequisites, installation, and local development.
- API Reference — entry points, commands, configuration sections, and abstract contracts.
- Deployment Guide — build, test, and operational guidance.
- Configuration — loader behavior, validation, and user-wide settings.
- Console — terminal interface, session handling, and interactive commands.
- Core Domain — orchestration, context, knowledge, approvals, and events.
- Implementations — concrete clients, sandboxes, tools, and loggers.
- Interfaces — abstract contracts that the core depends on.
- Adoption Notes — what is carried forward and what is intentionally left out.

# Architecture Summary

The system is organized into four layers. The core layer holds orchestration and state management and depends only on abstract contracts. The interface layer declares those contracts. The implementation layer supplies interchangeable concrete components for each contract. The outer layer provides assembly, configuration loading, and user-facing entry points. The central orchestrator is the only stateful component in the core and is built via dependency injection. Component assembly is performed by a registry that maps short names to concrete classes, validates unknown names and unknown keys at startup, and shares a single edit ledger across file tools. Conversation history is bounded and turn-aware, and the system prompt is assembled from base instructions, environment facts, known-failure knowledge, durable memory, and repository instructions, each subject to size caps and a total cap that is re-applied after per-turn additions.

# Safety Notes

The host sandbox executes directly in the workspace folder. It validates timeouts, blocks traversal and encoded traversal, checks for shell expansions, enforces path confinement via resolved paths and symbolic link checks, and caps output size. The container sandbox manages a disposable container with resource limits and owner-only staging. Neither is presented as a complete security boundary. Credentials are resolved from the environment first and then from a restricted store, and only masked forms are displayed. Background task logs are written to workspace-private or per-process private locations with restricted permissions, and file-based persistence uses atomic temporary files and verified stale-lock handling.

# Tests

The offline suite exercises the orchestration loop, context truncation and budgeting, configuration validation including unknown-key rejection, the read-before-edit contract, memory capping and truncation handling, instruction discovery, the streaming parser with consecutive and total malformed-chunk limits, model discovery filtering, approval classification including wrapper expansion, session persistence with legacy fallback, and file-tool safety checks. Running the suite requires no network access or credentials. Live probes are available for end-to-end verification with valid credentials and network access when desired.


---

## File: docs\ADOPTION.md

# Adoption Report — What Was Carried Over and What Was Left

The system adopts selected ideas from a larger workflow-layer harness that provides many scripts, skills and state management. The filter applied was to adopt only what a solo daily-driver coding agent needs, with every addition required to earn its place in terms of complexity and maintenance cost.

## Adopted

### Read-Before-Edit Ledger

The source harness requires a recorded fresh read before any file edit and rejects edits whose anchor no longer matches the on-disk content. This is carried over as an in-memory per-session ledger that records a content hash on each read and write, enforces the invariant in the edit tool, rejects edits to truncated content and when only a partial view was seen, and reports a clear message when the file changed since the last read. The ledger uses normalized path keys and is now platform-aware for case sensitivity.

### Known-Failure Registry

Every recurring incident class is recorded as an entry with symptom, rule and date. The registry is appended to the system prompt on every session so fixed classes stay fixed. New entries are added when a class is fixed and are accompanied by regression tests that lock the fix.

### Durable Workspace Memory with Cap

Project state is kept per workspace in a hidden directory and appended after each turn. The memory file is capped to prevent unbounded growth, with oldest lines pruned first and single-line oversize content truncated. The tail is loaded into the system prompt, and locking with verified stale handling protects concurrent appends. Environment facts, instruction files and repository head are injected alongside the memory.

### Session Persistence and Resumption

Automatically saved transcripts allow a session to be resumed after closing the window. Each transcript records version, name, timestamp, workspace, model, summary, totals, goals and the full message list with per-message size caps. Transcripts are written atomically with restricted permissions, listed newest first, corrupted files are skipped, and unsafe names now include a hash suffix with legacy fallback for older transcripts.

### Approval Policy

A daily-driver agent needs a gate between a requested tool and an executed action. The adopted policy classifies each tool invocation as safe, mutating or destructive via pattern matching, now handling wrapper invocations by their inner command and with corrected pre-tool-use redaction. Four modes differ in which categories are auto-allowed. Positive answers can be remembered for the remainder of the session on a per-tool key basis.

### Turn-Aware Context Management

Conversation history is bounded and turn-aware, preserving the initial system prompt and task while removing the oldest complete turn first to avoid orphaned tool results, with repeated truncation and turn removal until within budget and with single-message reservation.

## Adopted Later

Enforced self-verification before finishing and a lightweight session digest were adopted in a minimal form. The orchestrator ensures an evaluation is always produced even after errors, and the structured log provides pass rates, step counts, tool error rates and token usage without requiring additional services. Automatic summarization of the conversation is available when the token threshold is exceeded, and background task handling is now bounded and explicit.

## Deliberately Left Behind

Host-level orchestration and permission plumbing, multi-agent fan-out, integrity chains for deployment to a live installation, research-oriented evaluation stacks, session mining families that depend on host log formats, document generation skill libraries and other host lifecycle features were left behind. They address needs that do not apply to a folder-based harness that is moved rather than deployed, and their cost exceeds their benefit for the current use case. Host capabilities such as task tracking, subagents and plan mode are used via habits rather than copied as files.

## Current State After Remediation

Following the recent remediation, the adopted features now include hardened path confinement at every file and command boundary including encoded forms, owner-only permissions for staged container files and background logs, bounded streaming and memory handling with both consecutive and total limits, validated configuration limits with unknown-key rejection, and validated container inputs. The suite of adopted ideas remains small, test-locked and free of host-specific dependencies, preserving the original goal of a personal daily driver that is easy to move and easy to reason about.


---

## File: docs\API.md

# API Reference

## Entry Points

Two entry points are exposed. The interactive console accepts optional flags to select the workspace location, override the configured model, endpoint address, reasoning effort and approval mode, and to disable styling or to handle a single message non-interactively before exiting. The headless runner requires paths to a configuration document and a task document and returns a process exit status indicating whether the evaluator passed. Both entry points resolve relative paths for configuration and log locations against the project root.

## Interactive Commands

Commands are invoked with a leading slash. The help command lists all commands. Workspace inspection commands display the workspace location and contents, the durable memory file and uncommitted changes, with a confirmation step before discarding changes. Tool, model, endpoint and approval commands list or select the available options, presenting a menu when no argument is supplied and applying a direct assignment when an argument is supplied. Endpoint commands also support listing, removing and replacing stored credentials, with always-prompted replacement to allow correction of a mistyped credential and with handling for both hidden and visible prompts. Cost and status commands display token usage, cache metrics and conversation size in both human-readable and structured forms. History management commands summarize, clear or reset the conversation while preserving the system prompt and files. Session persistence commands save the current conversation to a file, load from a file or resume from automatically saved transcripts, with the most recent transcript listed first and with validation that the target path resides within allowed directories and with legacy name fallback. Goal commands set, show and clear a standing objective that is injected into every turn. Skill and workflow commands discover, display, attach and launch procedural bundles. Pasting, step limit and verbosity commands control input and execution bounds. The exit command terminates the session.

File references use a leading sigil plus path or pattern. Resolved relative to the workspace, rejected if outside, expanded to content, listing or glob matches. Each file is capped, the total attached content is capped, the number of glob matches is capped, and unknown references are reported. Directory listings for globs are limited to a fixed number of entries.

## Configuration Sections

The configuration document is divided into sections. The language model section identifies the provider name, model name, endpoint address, credential lookup name, sampling temperature, token limit, reasoning effort and streaming preference. The sandbox section selects the isolation provider and its resource limits, including validated image name, memory limit and workdir for the container option. The tools section lists the names of tools exposed to the language model, with alias normalization and deduplication. The evaluator section selects the grading strategy, test command and timeout, with per-task override support. The logging section selects the sink and file path, with relative paths anchored to the project root. The approvals section selects the default policy among four modes. The context section limits retained message count and character count, with validation that limits are integers meeting minima. Top-level keys control the maximum steps per task, the base system prompt, the token threshold for automatic summarization, verbosity and skill routing preferences including automatic attachment and bundle launching. Unknown top-level or section keys are now rejected.

The task document requires a problem statement. Optional keys provide a repository address, a base commit, a setup command, a setup timeout, a clone timeout and a task-specific test command that overrides the evaluator configuration.

## Abstract Interfaces

The language model interface accepts a list of messages, optional tool schemas and an optional streaming callback. It returns a normalized response that is either a final answer or a collection of tool invocations, each with an identifier, name and arguments, plus usage metadata including prompt and completion token counts and cached token counts. The streaming callback, when supplied, receives content fragments as they arrive. Implementations cap total streamed content, bound single-line buffers, and enforce limits on both consecutive and total malformed fragments.

The sandbox interface provisions the environment for a task, executes shell commands with timeout and abort support, and reads and writes files relative to the workspace. Provisioning optionally fetches a repository, validates the repository address and commit identifier, and runs a setup command. Execution results include exit status, standard output, standard error and a timeout flag. File operations reject escapes via resolved path checks at read, write and directory creation time, check each component of newly created parent chains for symbolic links, and cap read and execution output. Cleanup is idempotent and distinguishes between sandboxes that own their directory and those that were given an existing workspace.

The tool interface exposes a name, description and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. Tool schemas are passed to the language model as part of the function-calling specification. File tools share a single edit ledger that enforces read-before-edit within a session and tracks whether the last view was partial.

The evaluator interface examines the sandbox after the orchestrator finishes. It returns a verdict indicating pass or fail, a descriptive detail string and optional metrics. One implementation runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support and tail truncation. The other always reports a neutral result for interactive sessions without automated grading.

Logger receives structured events and never propagates input or output errors. Implementations append one record per line with a timestamp and use both in-process and inter-process locks with verified stale handling. The event bus interface allows subscription of handlers and fans out events synchronously while suppressing handler exceptions to isolate observers.

## Registry

The registry maps short names to concrete classes for language model clients, sandboxes, tools, evaluators and loggers. Construction forwards only those configuration keys that match constructor parameters and validates that required parameters are present, reporting unknown names, unknown keys or missing parameters as configuration errors. Tool construction shares a single ledger, deduplicates tools that resolve to the same implementation, and normalizes aliases.


---

## File: docs\Architecture.md

# Architecture Guide

## High-Level Design

The system is structured into four layers that isolate concerns and allow extension without changing core logic. The core layer holds orchestration and state management and depends only on abstractions. The interface layer declares those abstractions for language model clients, sandboxes, tools, evaluators, loggers and the event bus. The implementation layer provides interchangeable concrete components for each abstraction. The outer layer provides assembly, configuration loading and user-facing entry points for interactive and headless operation.

The central orchestrator is the only stateful component in the core. It is built via dependency injection with fully constructed collaborators and does not import concrete implementations. New clients, sandboxes, tools, evaluators or loggers can be added by implementing an interface and registering the implementation, without modifying the core.

## Module Interactions

A task enters through either the interactive console or the headless runner. The console maintains a long-lived session that reuses the same sandbox directory, context manager and approval policy across turns. The headless runner builds a fresh sandbox for each invocation.

The orchestrator seeds history, requests a response, and dispatches approved tool calls. Tool execution is delegated to the sandbox and the resulting observation is appended to history. Usage metadata is accumulated for accounting. The loop ends on a final answer, step limit, abort or error, after which the evaluator inspects the sandbox and the logger records the outcome. An event bus distributes lifecycle events to optional observers.

Conversation history is owned by a dedicated manager that enforces limits on message count and total characters. The initial system prompt and task are pinned, and the oldest complete turn is removed first, ensuring that tool results are never left orphaned. When the character budget is exceeded, the manager repeatedly truncates the largest message or removes additional turns until within budget. Oversized single messages are truncated before insertion, reserving space for the system prompt.

System prompt assembly merges the base instruction, environment facts, known-failure knowledge, durable per-workspace memory and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location and version control branch and dirty state. Each source is capped, durable memory retains the tail of the file to preserve recent entries, and the total assembled prompt is capped. The cap is re-applied after per-turn additions such as active procedures and standing goals.

Component assembly is performed by a registry that maps short names from configuration sections to concrete classes. The registry validates unknown names and unknown keys at startup, validates that required constructor parameters are present, shares a single edit ledger across file tools, deduplicates tools that resolve to the same implementation, and normalizes tool name aliases.

## Key Decisions and Trade-Offs

Zero third-party dependencies are preferred for core operation. Networking, process execution and terminal handling rely on the standard library, with an optional parsing library required only for the alternative configuration format. This reduces installation drift at the cost of reimplementing some utilities.

Isolation is offered at two levels. A host directory sandbox executes directly in a workspace folder for speed during trusted development. A container-based sandbox manages a disposable container via the container runtime interface for stronger isolation. Neither is presented as a complete security boundary. Both enforce path confinement via resolved path checks at every file and directory operation, and check each component of newly created parent chains for symbolic links. The host execution path also blocks traversal, encoded traversal and shell expansions in commands, and checks for home directory expansion.

Streaming is optional and callback driven. When a delta handler is supplied, content fragments are delivered incrementally and tool call deltas are accumulated by index before being assembled into complete invocations. The parser tolerates occasional malformed fragments but limits both consecutive and total malformed fragments, caps total streamed content and individual tool argument size, and bounds single-line buffers without line breaks.

Approval is modeled as a separate policy with four modes that are evaluated per tool call. Read-only operations are always allowed. The confirmation prompt is supplied by the front end, keeping the policy independent of terminal handling. Positive answers are remembered for the remainder of the session on a per-tool key basis. Wrapper invocations are classified by their inner command to avoid double counting.

Persistence is file-based and atomic. Settings, credentials, workflows and session transcripts are written via temporary files with restricted permissions and then moved into place, with quarantine handling for corrupted files and verified stale-lock handling for concurrent access. Background task logs are written to workspace-private or per-process private locations with restricted permissions and are bounded and pruned.


---

## File: docs\Deployment.md

# Deployment Guide

## Build

Build is declared via the standard packaging manifest with a setuptools backend and a minimum interpreter version. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only for the alternative configuration format. Build artifacts include distribution metadata and entry point declarations for the interactive console and the headless runner. The manifest also declares test discovery and module search path adjustments for the test suite.

## Testing

The test runner configuration points to a dedicated test directory and adjusts the module search path to include the source directory. The offline suite exercises the orchestration loop including final-answer handling, step-limit termination, unknown-tool resilience and error paths, context truncation with pinned messages, orphan avoidance and looping budget enforcement, configuration validation including context limits and unknown-key rejection, the read-before-edit contract including unread and stale-content rejection and partial-view handling, memory capping including single-line truncation, instruction file discovery and preference order, the streaming parser including content accumulation, tool call reassembly and consecutive and total malformed-chunk limits, model discovery filtering and ranking, approval classification including wrapper handling, session persistence with size caps and legacy name fallback, and file-tool safety checks including binary sampling and bulk limits. Interactive layer tests cover workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling and prompt forwarding, with explicit background opt-in. Live probes exercise end-to-end paths with valid credentials and network access and are not required for the offline suite. The full suite is expected to pass without network access aside from the live probes, and the container-based sandbox tests require a container runtime on the host.

## Environment Promotion

Configuration documents are kept alongside example files that illustrate the minimal required fields. Environment-specific values such as endpoint addresses and credential lookup names are supplied via configuration and resolved at runtime, with relative log paths anchored to the project root so the same configuration works from any working directory. Task documents may override the evaluator test command on a per-task basis. User-wide settings, credentials and session transcripts are kept in the user home directory with override locations available via environment variables for testing, and all are written atomically via temporary files with restricted permissions and verified stale-lock handling. Background task logs are written to workspace-private or per-process private locations.

## Operational Runbooks

### Monitoring

The system writes one structured record per event to an append-only file. Records include a timestamp, event name and payload with task identifier, step, tool name and result status including elapsed time and success flag. Monitoring consists of tailing this file and aggregating pass rates, step counts, tool error rates and token usage including cache hit metrics. The console also maintains per-turn totals and displays them in the status area, and the streaming path maintains a live token counter.

### Backup

Persistent state to preserve includes the workspace directory, the user-wide settings document, the restricted credentials store, the session transcript directory and the workflow definitions file. Each of these is a regular file or directory in the user home or workspace. Backup is performed via standard file copy while the system is idle. No additional services require backup.

### Recovery

Recovery from corrupted user settings or credentials is automatic, treating the file as empty and continuing with defaults, with a quarantined copy of the corrupted file retained alongside the original. Recovery from a corrupted session transcript skips the unreadable file and continues to list the remaining transcripts, with fallback to legacy name forms. Recovery from a corrupted workflow file similarly returns an empty collection. Recovery from a sandbox failure is performed by the orchestrator, which ensures that the evaluator still produces a verdict and that cleanup is attempted even after errors, and that abort signals are propagated to running commands with termination and forced kill semantics.


---

## File: docs\Setup.md

# Setup Guide

## Prerequisites

A supported interpreter version is required as declared in the project manifest. Network access to a compatible language model service is required for live operation. For isolated execution, a container runtime must be available on the host and its command-line interface must be executable. Version control tooling is expected on the host for workspace initialization and change inspection.

## Installation

The project is installed via the standard packaging mechanism using the manifest in the repository root. Package discovery is limited to the source directory. No runtime dependencies are declared for core operation. An optional parsing library is required only when using the alternative configuration format.

After installation, two entry points are available: the interactive console and the headless runner. The interactive console can also be invoked directly from the source tree without installation. The console can be launched with overrides for workspace location, model, endpoint address, reasoning effort and approval mode, and with flags to disable styling or to handle a single message non-interactively.

## Configuration

Configuration is supplied as a structured document. The primary format is supported natively; the alternative format requires the optional parsing library. The loader merges the supplied document deeply into a set of defaults, so that partial documents inherit sensible values for omitted sections. Required sections are validated, at least one tool must be listed, enumerated values for approval mode and reasoning effort are checked, and unknown configuration keys are now rejected to surface typos early. The context section, when present, is validated to be an object with integer limits that meet documented minima for message count and character count. The loader also caps the raw file size to prevent unbounded reads.

Secrets are not stored in configuration files. The language model section names a lookup key that is resolved at runtime first from the process environment and then from a restricted credentials store. The store is created with owner-only permissions where the platform supports it and is never written to the main settings file. On platforms where permission bits are not enforced, a one-time warning is emitted. Only masked forms of stored values are displayed.

User-wide endpoint selections live in a hand-editable file in the home directory. An override via environment variable is available for tests. The same mechanism allows redirecting the credentials store, the session transcript directory and the workflow definitions file.

## Local Development

A workspace directory holds the repository under test. If the directory lacks version control initialization, an empty repository is initialized automatically. The workspace is reused across turns in interactive mode, and its location can be overridden at launch. Repository-specific instructions are discovered by searching the workspace root for well-known filenames in a defined preference order, and the first match is used.

Per-workspace memory is stored in a hidden directory and capped. Oversized single lines are truncated. The system prompt is assembled from the base instruction, environment facts, known-failure knowledge, durable memory tail and any repository instructions, with a total cap that is re-applied after per-turn additions.

The interactive console requires a terminal for full functionality. When standard input or output is not a terminal, the console falls back to plain line input and omits decorative framing. A non-interactive single-message mode is available for scripting and for probes.

## Verification

The test suite is discovered in a dedicated test directory with the module search path adjusted to include the source directory. Running the suite exercises offline paths without network access or credentials, covering the orchestration loop, history truncation and budgeting including the new looping behavior, configuration validation including unknown-key rejection, the read-before-edit contract, memory capping, instruction discovery, the streaming parser with consecutive and total malformed-chunk limits, model discovery filtering, approval classification including wrapper handling, session persistence with legacy fallback, and file-tool safety checks. Live probes are available for end-to-end verification with valid credentials and are not required for the offline suite.


---

## File: docs\configuration.md

# Configuration

Configuration is a structured document. The primary format is supported natively; the alternative format requires an optional library. The raw file size is capped before parsing, and the parsed document is merged deeply into a set of defaults so that partial documents inherit sensible values for omitted sections. Required sections are validated to be objects, at least one tool must be listed, and every tool entry must be a non-empty name. Unknown configuration keys at any level are now rejected to surface typos. Enumerated values for approval mode and reasoning effort are checked against allowed sets, and the context section, when present, is validated to be an object with integer limits that meet minima for message count and character count. Errors are reported as configuration errors with descriptive messages.

Defaults provide a base system prompt, maximum steps, language model settings, sandbox selection, tool list, evaluator command, logging sink, approval mode, context limits, automatic summarization threshold, verbosity and skill routing preferences including automatic attachment and bundle launching. The language model defaults include provider, model, credential lookup name and no reasoning effort.

The language model section is forwarded to the registry, which maps the provider name to a concrete client class and validates unknown names and unknown keys, including handling for reasoning effort. The sandbox, evaluator and logger sections are handled similarly, with only the relevant keys forwarded and unknown provider names reported. Tool construction validates unknown names at startup, normalizes aliases, shares a single edit ledger across file tools to enforce read-before-edit and partial-view tracking, deduplicates tools that resolve to the same implementation, and applies alias normalization.

User-wide endpoint and model selections are kept in a separate hand-editable document. It enumerates endpoints with base address, credential lookup name, known models and an optional note, plus the active endpoint, model and reasoning effort, and skill routing preferences. The file is written atomically via a temporary file with restricted permissions, with quarantine handling for corrupted files and verified stale-lock handling. A parallel credentials store holds secret values with restricted permissions and is never written to the main settings file; the store is keyed by lookup name and supports masking for display. Secrets are resolved first from the process environment and then from the store, with a one-time warning on platforms where permission bits are not enforced.

Workflow definitions are kept in another document that stores named sequences of prompts with version, creation timestamp and steps, subject to limits on step count and step length, with atomic writes and verified locking. Session transcripts are kept as one file per session under a dedicated directory, with an override location available via an environment variable. Each transcript records version, name, timestamp, workspace, model, summary, totals, goals and notes, and the full message list, with per-message size caps, atomic writes, restricted permissions and legacy name fallback.


---

## File: docs\console.md

# Console

The console module provides the interactive terminal interface using only standard library facilities. It implements styling via terminal escape sequences, with a flag to disable styling for non-terminal output, and ensures that escape sequences on the host console are enabled where needed.

Console session owns workspace, sandbox, context, prompt, tools, client, approvals and event bus. The session maintains totals for tokens, turns and errors, recent tool history and per-turn cache metrics including hit rate trends. It also manages a streaming renderer that buffers fragments until line boundaries to apply formatting while tracking code fence state, with sanitisation of terminal escape sequences from model output that covers both bracketed and operating system command forms, and with bounding of single-line growth without line breaks. A spinner runs on a background thread while the model is working and is paused or retired when real output arrives, and is killed on the first streamed token to avoid overwriting.

The line editor reads single keys and provides completion for commands and workspace paths, with handling for navigation keys, deletion and history, and a popup that can be dismissed or re-invoked via a dedicated key. The popup supports filtering as the operator types, selection via arrow keys and a hint area, and now shows a count hint when the terminal is too small to render. When standard input or output is not a terminal, the editor falls back to plain line input, and a helper adapts the editor to a prompt-to-line signature.

Layout is managed by a compact viewport that reserves rows for a top information bar, a separator, a content area with an internal buffer and scroll support, a border row and a fixed bottom prompt. The layout supports entering and leaving an alternate screen, clearing and redrawing chrome and handling resize events with synchronized output to prevent flicker. The startup card is rendered as a centered adaptive block whose width follows the terminal width with small margins, displaying the product name, tagline and version. The card disappears after the first turn. The bottom prompt is fixed at the window bottom with a separator and a single-line status showing the model, reasoning effort and workspace, and includes a live token estimate during streaming that is throttled to avoid flicker. The viewport buffers short fragments and forces a flush at stream completion so the final fragment is never delayed.

File mentions are expanded by resolving sigil-prefixed tokens relative to the workspace, rejecting escapes via resolved path checks and expanding globs with caps on hits and entries. Each file is capped, the total attached content is capped and unknown references are reported. Directory listings for globs are limited, and content is truncated with a marker when caps are exceeded.

The session supports goal injection where a standing objective and optional notes are rebuilt into the effective system prompt on every turn, with a cap that is re-applied after per-turn additions and with a check that notices when the agent reports the goal as complete. Skill attachment appends procedure text to the prompt for the current turn, with automatic routing that can attach a matching skill without being asked, subject to confidence and margin thresholds and to flags that disable automatic attachment or bundle launching. Bundles launch as ordered steps, attaching each skill in turn and restoring the previous attachment afterward. Workflows store named sequences of prompts and launch as ordered steps through the same session handler. Auto-compaction summarizes the conversation via the language model client when the token threshold is exceeded, replacing the body with a summary while preserving the system prompt.

Help text enumerates all slash commands and describes file reference syntax. Commands include workspace, memory, difference, undo, tools, approval mode, cost and compact and status variants, history management, session persistence with path validation that checks the resolved absolute form against allowed directories for both absolute and relative inputs and with legacy name fallback, goal, skills, workflows, pasting, step limit and verbosity. The console banner displays model, endpoint, workspace, version control status, approval mode, tool count and instruction file information. Session save and load enforce size caps on file and payload, and autosave keeps the session resumable after each turn once the conversation is substantial.


---

## File: docs\core.md

# Core Domain

## Orchestrator

The orchestrator is the sole stateful core component. It runs the provision, conversation and tool dispatch loop. It is built via dependency injection and interacts with all collaborators through abstract interfaces. The loop supports optional context reuse for multi-turn conversations, external abort signalling that is propagated to the sandbox and checked between steps and inside the streaming callback, and per-tool approval through a pluggable policy. It accumulates token usage and cache metrics including prompt and completion counts and cached token counts, handles deduplication of tool call identifiers within a turn with guaranteed uniqueness, validates that responses have the expected shape, and ensures that an evaluation is always produced even after errors, timeouts or aborts, with cleanup attempted in all cases. It also enforces intent-based loop breaking using the full primary argument and a bounded registry to prevent unbounded growth.

## Context Management

The context manager owns the message list sent to the language model. It retains the system prompt and initial task, enforces limits on message count and total characters with validation that limits are integers meeting minima, and removes the oldest complete turn first to avoid orphaned tool results, with fallbacks that remove the oldest non-tool message and then the oldest tool message. It provides operations to seed the conversation, append messages with truncation of oversized single messages that reserve space for the system prompt, replace the body with a summary while preserving the system prompt, and recompute size after external edits. When still over budget after removing turns, it repeatedly truncates the largest message or removes additional turns until within budget or only the system prompt and a minimal exchange remain. Token estimation is based on character count divided by a constant.

## Knowledge Assembly

The knowledge module assembles the system prompt from environment facts, known-failure knowledge, durable per-workspace memory and repository-specific instructions. Environment facts include date, operating system, shell, interpreter version, workspace location and version control branch and dirty state with handling for non-repository directories and for status probe failures. Repository instructions are discovered by searching the workspace root for well-known filenames in a defined preference order. Each source is capped to bound prompt size, with durable memory retaining the tail of the file to preserve recent entries and truncating single-line oversize content. The final assembled prompt is capped. Appending durable memory uses a per-process lock and an inter-process lock file with verified stale handling, re-reads the file after acquiring the lock to avoid lost updates, prunes oldest lines while over the cap, and writes atomically via a temporary file with restricted permissions.

## Approvals

The approval module classifies each tool invocation into safe, mutating or destructive. File writes and edits are mutating, while certain shell commands are classified as destructive via pattern matching that covers removal, formatting, process control, privilege escalation and destructive version control operations, and safe commands are recognized via a separate pattern set that covers listing, inspection and test commands. Four modes differ in which categories are auto-allowed and which require prompting, with a plan mode that refuses all mutations. Wrapper invocations are classified by their inner command to avoid double counting. The policy remembers positive answers that were marked to persist for the remainder of the session on a per-tool key basis, with distinct key generation for commands, file paths and other tools, and it suppresses exceptions from the prompting callback. Pre-tool-use decisions are logged with redaction that covers known prefixes, assignment-like syntax, high-entropy tokens and exact matches against stored credentials.

## Events and Exceptions

The event bus provides synchronous fan-out to subscribed handlers and suppresses handler exceptions to isolate observers. The exceptions module defines a hierarchy with distinct types for harness, configuration, tool, sandbox, language model, evaluation and abort conditions, allowing the orchestrator to distinguish operator interruption from other failures.


---

## File: docs\implementations.md

# Implementations

## Language Model Clients

One implementation speaks the standard chat completions protocol over standard library networking facilities. It supports both buffered and streaming modes, accumulates tool call deltas by index with handling for name and argument fragments that may arrive separately or as already parsed structures, handles server-sent event framing including the terminating sentinel and usage objects, and retries with backoff. It performs per-field downgrades when a server rejects optional parameters such as usage inclusion, reasoning effort or token limit naming, switching the token field name where needed and remembering the choice for subsequent turns, with word-boundary checks to avoid false positives. It also provides a fallback that translates a chat payload to a responses shape when the chat endpoint returns a not-found or server error, but it does not use that fallback for parameter downgrade cases and it now chains the original error when the fallback also fails. Credentials are resolved from the configured lookup and included as a bearer token when present, and headers include an identifier. Response size and total streamed content are capped, and the parser now limits both consecutive and total malformed streaming fragments.

A second implementation replays a fixed script of responses for offline testing and raises when the script is exhausted.

## Sandboxes

The host directory sandbox executes directly in a workspace directory on the host, creating the directory if needed and using a temporary directory when none is supplied. It validates repository addresses and commit identifiers, blocks traversal including encoded traversal and home-directory and environment-variable expansion in commands including shell expansions, and runs commands via the host process facility with polling to support abort and timeout and with caps on output size. Path resolution rejects escapes via symlink resolution at read, write and directory creation time, checks each component of newly created parent chains for symbolic links, re-resolves the full path after directory creation, rejects symbolic link targets, and uses atomic temporary file replacement for writes. Reads are capped and truncated with a marker, and writes are rejected when content would exceed the allowed size.

The container-based sandbox manages a disposable container via the container runtime interface, creating the container with validated image name, memory limit and workdir, with resource limits and optional networking control, and executing commands via the runtime with polling for abort and timeout. Input validation for the container sandbox now rejects malformed image names, out-of-range memory limits and non-normalized workdirs at construction time. File transfer is performed by staging locally with owner-only permissions and copying into the container to avoid shell quoting issues, with validation that paths do not escape the container workdir and that repository addresses and commits are safe. Cleanup removes the container and execution respects abort signals.

## Tools

File tools provide reading with windowed display, byte and per-line caps, header notes for truncated views, deduplication via an in-memory and persistent ledger that is now platform-aware, partial-view tracking, and did-you-mean suggestions. Writing creates parent directories with post-creation validation and ledger recording. Editing enforces read-before-edit and stale-content checks and rejects edits when the file was truncated due to size or when only a partial view was seen. Directory listing provides separate handling for sandboxes without a direct file view that validates against shell metacharacters and uses safe quoting with a fallback sequence that reports empty directories correctly. The ledger enforces the read-before-edit invariant via content hashing with normalized path keys, and file tools reject paths containing invalid characters, device names, trailing dots or alternate data streams, and sample multiple windows for binary detection. Bulk reads now report how many files matched on disk versus how many were read and note when ceilings are reached.

Command tools execute a shell command and format the result as exit status, timeout flag and truncated output with middle-out handling, untrusted fencing, honest exit codes and full log capture. Background execution now requires explicit opt-in and writes logs to workspace-private or per-process private locations with restricted permissions, and the registry of background tasks is bounded and pruned. Difference and reset tools display version control differences and discard changes with timeout handling.

Search tools walk the workspace without descending into ignored directories, filter symlinked directories that point outside the workspace, skip files above a size limit or with non-text extensions or hard link counts indicating potential outside exposure, skip symlinked and hard-linked files that escape, and return matching lines or file names with limits on result count, scanned file count and line length, now noting when ceilings are reached, with an alternate shell-based path for sandboxes without a direct file view.

Web fetching retrieves a URL, enforces scheme and size and decompression limits during and after inflation, decodes according to declared character set with fallbacks, extracts visible text from markup while dropping non-visible elements and collapsing whitespace, blocks private, loopback, link-local, reserved and metadata hosts including via alternative numeric encodings and via live name resolution with timeout, validates each redirect target, and returns an error string rather than raising.

## Evaluators and Loggers

One evaluator runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support and tail truncation. The other evaluator always reports a neutral result for interactive sessions without automated grading.

The structured logger appends one serialized record per line with a timestamp, event name and payload, using both in-process and inter-process locks with verified stale handling, and suppresses input and output errors.


---

## File: docs\interfaces.md

# Interfaces

## Language Model Client

The language model contract accepts a list of messages, an optional list of tool schemas and an optional callback for streamed content fragments. It returns a normalized response that contains either a final answer or a collection of tool invocations, each with an identifier, name and arguments, plus usage metadata including prompt and completion token counts and cached token counts. The callback, when supplied, receives content fragments as they arrive. The contract is the only point where the core interacts with the language model service, allowing different services and offline replay to be substituted without changing orchestration.

## Sandbox

Sandbox contract defines lifecycle and file operations. Provisioning validates inputs, fetches the repository and runs setup. Execution runs a shell command with a timeout and returns exit status, standard output, standard error and a timeout flag, with support for external abort that is checked before and during execution. File operations read and write paths relative to the workspace and reject escapes via resolved path checks, with read operations capped and truncated and write operations validated for size and for parent-chain confinement including symbolic link checks and re-resolution. Cleanup is idempotent and safe to call multiple times, distinguishing between sandboxes that own their directory and those that were given an existing workspace. Validation for container sandboxes also covers image, memory and workdir at construction.

## Tool

The tool contract defines a single capability. It exposes a name, description and parameter schema. Execution is performed against a sandbox and returns an observation string that is appended to the conversation. The schema is passed to the language model as part of the function-calling specification. Tools share a ledger for read-before-edit enforcement and partial-view tracking and are instantiated via the registry from short names with alias normalization and deduplication.

## Evaluator

The evaluator contract examines the sandbox after the orchestrator finishes. It returns a verdict indicating pass or fail, a descriptive detail string and optional metrics. One implementation runs a shell command and interprets a zero exit status without timeout as pass, with per-task override support. The other always passes for interactive sessions without automated grading. The orchestrator ensures a verdict is always produced, even when evaluation itself raises.

## Logger and Event Bus

The logger contract receives structured events with a name and payload and never propagates input or output errors to the caller. Implementations append one record per line with a timestamp and use both in-process and inter-process locks with verified stale handling. The event bus contract allows subscription of handlers and fans out events synchronously while suppressing handler exceptions to isolate observers from the run and from each other.


