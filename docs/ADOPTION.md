# Adoption Report

## Scope Filter

This system adopts selected ideas from a larger workflow-layer harness that offers many scripts, skills, and state facilities. The filter was to adopt only what a solo daily-driver coding agent needs, with each addition required to justify its complexity and maintenance cost.

## Adopted

### Read-Before-Edit Ledger

The source harness requires a recorded fresh read before any file edit and rejects edits whose anchor no longer matches the on-disk content. This is carried over as an in-memory per-session ledger that records a content hash on each read and write, enforces the rule in the edit tool, rejects edits to truncated content, partial views, invalid text encoding, and anchors that match more than once, and reports clearly when a file changed since the last read. Path keys are normalized and case is folded only where the platform's filesystem does.

### Known-Failure Registry

Every recurring incident class is recorded with a symptom, a rule, and a date. The registry is appended to the system prompt on every session so fixed classes stay fixed. The injected file is the known-failure document at the project root. New entries accompany a fix and are locked by regression tests.

### Durable Workspace Memory with a Cap

Project state is kept per workspace in a hidden directory and appended after each turn. The memory file is capped, oldest lines are pruned first, and oversized single lines are truncated. The tail is loaded into the system prompt, and locking with verified stale handling protects concurrent appends. Environment facts, instruction files, and repository head are injected alongside it. Entries carry lifecycle metadata so a duplicate is skipped and a replacement marks the older entry superseded, and prompt reads exclude superseded and stale entries.

### Session Persistence and Resumption

Automatically saved transcripts allow a session to be resumed after closing the window. Each transcript records a version, name, timestamp, workspace, model, summary, totals, checklist, and full message list with per-message caps. Transcripts are written atomically with restricted permissions, listed newest first, with corrupt files skipped and unsafe names given a hash suffix with legacy fallback.

### Approval Policy

The policy classifies each tool invocation as safe, mutating, confirm, or destructive through pattern matching, handling wrapper invocations by their inner command and gating interpreter one-liners behind explicit confirmation, with corrected pre-tool-use redaction. Four modes differ in which categories are auto-allowed. Positive answers can be remembered for the rest of the session on a per-tool basis.

### Turn-Aware History Management

Conversation history is bounded and turn-aware, preserving the initial prompt and task while removing the oldest complete turn first to avoid orphaned tool results, with repeated truncation and turn removal until within budget and single-message reservation applied at seeding as well as appending.

### Tool Argument Repair

Validate-then-repair handling of model tool inputs was adopted in a compact form: alias resolution, null stripping, typed string parsing, bare-string wrapping, numeric coercion, path aliasing, and link unwrapping, always validating first and re-validating afterward.

### Bounded Workspace Reading

Literal text search, filename search, single-file structured extraction, and tree-pattern querying were adopted as bounded reading primitives. Every tool has result, scan, size, and output ceilings, honours ignore files and link confinement, and reports when a ceiling is reached instead of silently truncating.

## Adopted Later

Enforced self-verification before finishing and a lightweight session digest were added in minimal form. The orchestrator ensures an evaluation is always produced even after errors, and the structured log provides pass rates, step counts, tool error rates, and token usage without extra services. Automatic summarization runs when the token threshold is exceeded, and background task handling is bounded and explicit.

## Deliberately Left Behind

Host-level orchestration and permission plumbing, multi-agent fan-out, integrity chains for a live installation, research-oriented evaluation stacks, session-mining families that depend on host log formats, document-generation skill libraries, and other host lifecycle features were left behind. They address needs that do not apply to a folder-based harness that is moved rather than deployed, and their cost exceeds their benefit here. Host capabilities such as task tracking, subagents, and planning are used as habits rather than copied as files.

## Current State After Remediation

The adopted features now include hardened path confinement at every file and command boundary including encoded forms, owner-only permissions for staged container files and background logs, bounded streaming and memory handling with consecutive and total limits, validated configuration limits with unknown-key rejection, and validated container inputs. A later hardening pass added process-group isolation with shared tree-kill semantics, uniquely named staging files for atomic writes, a byte-explicit input decoder with bounded pastes and modifier-aware arrows, a locked handoff between prompt submission and turn teardown, transcript sanitization without forgeable internal markers, display-column caret arithmetic, and credential-shaped variable stripping in host child environments. The adopted set remains small, test-locked, and free of host-specific dependencies.

## Known Gaps

Two deliberate trade-offs remain visible and are recorded here rather than presented as solved. First, the host sandbox's traversal screen is a heuristic and is not a containment boundary, so unrecognized command shapes run with the full authority of the host account; the container sandbox exists for work that needs real isolation. Second, full command output and background-task logs pass through the same pattern-based redaction as command headers, while structured run-log payloads exclude raw output and file contents entirely. Because redaction recognises known secret shapes rather than every secret, those log files are still treated as sensitive.
