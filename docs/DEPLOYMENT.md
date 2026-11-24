# Deployment Guide

## Build

Build is declared in the standard packaging manifest with a minimum interpreter version of 3.10. Package discovery is limited to the source package. One runtime dependency is declared for document parsing, and an optional parser is required only for the alternative configuration format.

Build metadata declares entry points for the interactive console and the headless runner. The manifest also ships example configuration and task documents, the known-failure knowledge file, command rules, and skill documents as package data, and it declares test discovery and module search path settings.

## Testing

The test-runner configuration points to a dedicated test directory and adds the source directory to the module search path. The offline suite exercises the orchestration loop, including final answers, step limits, unknown tools, retry budgets for empty or truncated replies, and error paths. It covers history truncation with pinned messages and orphan avoidance, configuration validation and unknown-key rejection, the read-before-edit contract, memory capping, instruction discovery, the streaming parser with malformed-chunk limits, model discovery filtering, approval classification with wrapper handling, session persistence, file-tool safety, and the search, extraction, and tree-query tools including escape checks.

Interactive-layer tests cover the prompt box, completion popup, mouse and paste handling, workspace persistence, conversation continuity, turn-aware truncation, approval prompts, abort handling, and prompt forwarding, with explicit opt-in for background behaviour. The suite is expected to pass without network access, apart from optional live probes. Container-sandbox tests require a container runtime on the host.

## Environment Promotion

Configuration documents are kept beside example files that show the minimal required fields. Environment-specific values such as endpoint addresses and credential lookup names are supplied through configuration and resolved at runtime. Relative log paths anchor to the project root so the same configuration works from any directory.

Task documents can override the evaluator test command per task. User-wide settings, credentials, and session transcripts live in the home directory, with override locations available through environment variables for testing. These documents are written atomically through uniquely named temporary files with restricted permissions and verified stale-lock handling.

Background task logs are written to workspace-private or per-process private locations with restricted permissions. The registry of background tasks is bounded and pruned. Tasks run in their own process group and every termination path uses one shared tree-kill helper, so a timed-out or aborted task leaves no descendants holding pipes or ports.

## Isolation Levels

Two sandbox providers are available with different guarantees.

The host sandbox executes directly on the host in the workspace directory. A one-time startup warning states this limitation explicitly. It applies a best-effort screen that blocks obvious path escapes, home-directory expansion, and shell expansions, but that screen is defence in depth only and is not a containment boundary: quoted payloads passed to interpreters and any other construction the screen does not recognize run with the full authority of the host account. Credential-shaped variables are removed from the child environment. Only the host sandbox supports background tasks and termination by port. Direct process-id termination is restricted to processes the harness started as background tasks. File writes stage through uniquely named temporary files, and session save and load validate resolved paths against an allowlist case-insensitively.

The container sandbox runs every command inside an ephemeral container with memory and processor limits. File access is confined to the container workspace; symbolic links are resolved and re-validated before every read and write; and host-side process control is unavailable by design. Containers run with reduced capabilities, no new privileges, and an unprivileged user, and are created detached so later executions have a running target. Timeout and abort handling terminates only children of the container keeper process so the container survives for later calls. Use this provider for untrusted inputs or repositories outside the operator's control, and keep the host provider for trusted interactive work.

## Operational Runbooks

### Monitoring

The system writes one structured record per event to an append-only log, one record per line. Records carry a timestamp, event name, and payload with the task identifier, step, tool name, and result status including elapsed time and a success flag. Caller payloads cannot overwrite timestamps or event names. The file rotates at a bounded size by renaming the current file aside and starting a fresh one. Monitoring consists of tailing this file and aggregating pass rates, step counts, tool error rates, token usage including cache hits, and the dropped-record counter, which is the only visibility into logging pressure.

The console keeps per-turn totals in its status area, and the streaming path maintains a live token counter. A separate approval audit log records each tool decision with redacted arguments and rotates when a sampled size check crosses a threshold.

### Backup

Persistent state to preserve includes the workspace directory, the user-wide settings document, the restricted credential store, and the session transcript directory. Each is an ordinary file or directory in the home directory or workspace. Backup uses standard file copy while the system is idle, and no additional services require backup. Stored credentials are plaintext protected only by file permissions, so backups of the home directory inherit that exposure.

### Recovery

Corrupt persisted documents are quarantined under collision-free backup names rather than deleted. Recovery consists of restoring the workspace from version control, restoring home-directory documents from backup, and restarting. A quarantined settings or credential file is replaced with defaults or an empty store on next use, so no manual repair is needed. Sessions are autosaved after substantial turns, so a closing window loses at most the current turn.

### Incident Handling

An approval audit entry accompanies every tool decision. For a suspected bad action, inspect that log and the structured run log for the tool name, redacted arguments, and result, then use version control to revert unwanted workspace changes. Background tasks are terminated through one shared tree-kill path on timeout or abort. If a task still holds ports or pipes after an abort, the tree-kill path has failed and the process group should be terminated manually. Full command output and background-task logs pass through the same redaction as command headers, but redaction matches known shapes only, so treat them as sensitive when collecting evidence.
