# Deployment Guide

## Build

Build is declared via the standard packaging manifest with a minimum interpreter version of 3.10. Package discovery is limited to the source directory. One runtime dependency is declared: an XML-parsing library used by the document-extraction tool. An optional parsing library is required only for the alternative configuration format.

Build artifacts include distribution metadata and entry-point declarations for the interactive console and the headless runner. The manifest also ships example configuration and task documents, the knowledge file, command rules, and skill markdown as package data, and declares test discovery and module-search-path adjustments for the test suite.

## Testing

The test-runner configuration points to a dedicated test directory and adjusts the module search path to include the source directory. The suite is run with the standard test runner.

The offline suite exercises the orchestration loop including final-answer handling, step-limit termination, unknown-tool resilience, empty-final and truncated-call retry budgets, and error paths. It covers context truncation with pinned messages, orphan avoidance, and looping-budget enforcement; configuration validation including context limits and unknown-key rejection; the read-before-edit contract including unread and stale-content rejection and partial-view handling; memory capping including single-line truncation; instruction-file discovery and preference order; the streaming parser including content accumulation, tool-call reassembly, and consecutive and total malformed-chunk limits; model discovery filtering and ranking; approval classification including wrapper handling; session persistence with size caps and legacy-name fallback; file-tool safety checks including binary sampling and bulk limits; and the search, extraction, and tree-query tools including nested-descent and workspace-escape regression tests.

Interactive-layer tests cover the composer, the completion popup, mouse and paste handling, workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding, with explicit background opt-in. The suite is expected to pass without network access aside from the optional live probes, and the container-sandbox tests require a container runtime on the host.

## Environment Promotion

Configuration documents are kept alongside example files that illustrate the minimal required fields. Environment-specific values such as endpoint addresses and credential lookup names are supplied via configuration and resolved at runtime, with relative log paths anchored to the project root so the same configuration works from any working directory.

Task documents may override the evaluator test command on a per-task basis. User-wide settings, credentials, and session transcripts are kept in the user home directory, with override locations available via environment variables for testing, and all are written atomically via uniquely named temporary files with restricted permissions and verified stale-lock handling.

Background task logs are written to workspace-private or per-process private locations with restricted permissions, and the registry of background tasks is bounded and pruned. Background tasks run in their own process group and every kill path routes through one tree-kill helper, so a timed-out or aborted task leaves no orphaned descendants holding pipes or ports.

## Sandbox Isolation Levels

Two sandbox providers are available, and they offer different isolation guarantees.

The host sandbox executes commands directly on the host in the workspace directory. It applies a best-effort traversal screen that blocks obvious path escapes, home-directory expansion, and shell expansions, but the screen is defense in depth only and is explicitly not a containment boundary: quoted payloads passed to interpreters, and any other construction the heuristic does not recognize, run with the full authority of the host account. The child environment is additionally stripped of credential-shaped variables so a model-issued command cannot trivially print secrets. Only the host sandbox supports background task execution and process termination by port; direct process-id termination is restricted to processes the harness itself started as background tasks. File writes stage through uniquely named temporary files, and session save and load validate paths case-insensitively against their directory allowlist.

The container sandbox runs every command inside an ephemeral container with memory and processor limits, and offers strong isolation: file access is confined to the container workspace, symlinks are resolved and re-validated before every read and write, and host-side process control is unavailable by design. Containers run with reduced capabilities, no new privileges, and an unprivileged user. Timeout and abort handling terminates only children of the container keeper process, preserving the container for later calls. Use the container sandbox for any task whose inputs are untrusted or whose repository is not under operator control; keep the host sandbox for trusted, interactive work where its speed and simplicity are worth the weaker boundary.

## Operational Runbooks

### Monitoring

The system writes one structured record per event to an append-only log file, one record per line. Records include a timestamp, event name, and payload with task identifier, step, tool name, and result status including elapsed time and success flag. Caller payloads cannot overwrite timestamps or event names. The file rotates at a bounded size by renaming the current file and starting a fresh one. Monitoring consists of tailing this file and aggregating pass rates, step counts, tool error rates, token usage including cache-hit metrics, and the dropped-record counter, which is the only visibility into logging pressure.

The console also maintains per-turn totals and displays them in the status area, and the streaming path maintains a live token counter. An approval audit log records each tool decision with redacted arguments and rotates at a fixed size. A vault verification script is not shipped: integrity chains for persisted ledgers were deliberately left behind, as recorded in the adoption report.

### Backup

Persistent state to preserve includes the workspace directory, the user-wide settings document, the restricted credentials store, the session transcript directory, and the workflow definitions file. Each of these is a regular file or directory in the user home or workspace. Backup is performed via standard file copy while the system is idle. No additional services require backup. Stored credentials are plaintext protected only by file permissions, so backups of the home directory inherit that exposure.

### Recovery

Corrupt persisted documents are quarantined under collision-free backup names rather than deleted, so recovery is: restore the workspace from version control, restore the home-directory documents from backup, and restart. A quarantined settings or credentials file is replaced with defaults or an empty store on next use; no manual repair is required. Sessions are autosaved after each substantial turn, so a closing window loses at most the current turn.

### Incident Handling

An approval audit log entry accompanies every tool decision. For a suspected bad action, inspect the audit log and the structured run log for the tool name, redacted arguments, and result, then use version control to revert unwanted workspace changes. Background tasks are killed through a single tree-kill path on timeout or abort; if a task is observed holding ports or pipes after an abort, the tree-kill helper has failed and the process group should be terminated manually.
