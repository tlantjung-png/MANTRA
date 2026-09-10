# Configuration

## Loader Behavior

Configuration is a structured document. The primary format is supported natively; the alternative format requires an optional parsing library. The raw file size is capped before parsing, and the parsed document is merged deeply into a set of defaults so that partial documents inherit sensible values for omitted sections.

Required sections are validated to be objects, at least one tool must be listed, and every tool entry must be a non-empty name. Unknown configuration keys at any level are rejected to surface typos. Enumerated values for approval mode and reasoning effort are checked against allowed sets, and the context section, when present, is validated to be an object with integer limits that meet minima for message count and character count. Errors are reported as configuration errors with descriptive messages.

When a component section changes its discriminating key (for example the evaluator type or the sandbox provider), the merged section starts from the operator's own values rather than the defaults, so a previous component's default keys cannot leak into the new component's constructor.

## Defaults

Defaults provide a base system prompt, a maximum step count, language model settings, sandbox selection, a tool list, an evaluator command, a logging sink, an approval mode, context limits, an automatic-summarization threshold, verbosity, and skill-routing preferences including automatic attachment and bundle launching. The language model defaults include provider, model, credential lookup name, and no reasoning effort. The default tool list covers file reading, writing, and editing, directory listing, command execution, shell output reading, background-task termination, code search, file finding, document extraction, tree querying, version-control diff and reset, and web fetching.

The default approval mode in the loader is the strictest interactive mode, which prompts for mutations. Note, however, that the console's default configuration document is the shipped example, and that example selects the automatic approval mode; a fresh operator therefore starts in the automatic mode unless they pass an explicit override. Approval mode can only be set through the configuration document or the console's approval flag; a hand-edited top-level approval key in the user-wide settings document is not read.

## Component Routing

The language model section is forwarded to the registry, which maps the provider name to a concrete client class and validates unknown names and unknown keys, including handling for reasoning effort. The sandbox, evaluator, and logger sections are handled similarly, with only the relevant keys forwarded and unknown provider names reported.

Tool construction validates unknown names at startup, normalizes aliases, shares a single edit ledger across file tools to enforce read-before-edit and partial-view tracking, and deduplicates tools that resolve to the same implementation.

## User-Wide Settings

User-wide endpoint and model selections are kept in a separate hand-editable document. It enumerates endpoints with base address, credential lookup name, known models, and an optional note, plus the active endpoint, model, and reasoning effort, and skill-routing preferences. Skill auto-attach lives here rather than in the run config because it is a one-time operator preference and the run config is never written back to by the system. The file is written atomically via a uniquely named temporary file with restricted permissions, with verified stale-lock handling. When the document cannot be parsed, it is quarantined under a collision-free backup name before anything replaces it, so repeated failures never overwrite the previous backup nor the evidence.

A parallel credentials store holds secret values with restricted permissions and is never written to the main settings file. The store is keyed by lookup name, supports masking for display, and records a schema version. Secrets are resolved first from the process environment and then from the store, with a one-time warning on platforms where permission bits are not enforced. An environment variable override relocates the store for testing.

Residual risk of stored secrets: credentials are held in plaintext (protected only by file permissions), so any process running as the same user, or any backup of the home directory, can read them. Prefer supplying secrets through the process environment where possible. Command text and output are redacted for known secret formats before being written to persistent task logs, but unrecognized secret shapes may still land in those logs; treat task and full-output logs as sensitive.

Workflow definitions are kept in another document that stores named sequences of prompts with version, creation timestamp, and steps, subject to limits on step count and step length, with atomic writes and verified locking. Corrupt content and shape mismatches are both quarantined under collision-free backup names. Only workflows with at least one step are listed or retrievable, so listed items are always launchable.

Session transcripts are kept as one file per session under a dedicated directory, with an override location available via an environment variable. Each transcript records version, name, timestamp, workspace, model, summary, totals, goals, notes, and the full message list, with per-message size caps, atomic writes, restricted permissions, and legacy-name fallback.
