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
