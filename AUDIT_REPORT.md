# Audit Report

## Scope and Method

This report covers the entire repository as it exists in the working tree: the project manifest and launcher, all source packages under `core/`, the test suite under `tests/`, the example documents, rules, knowledge file, skill manifests, and the existing documentation. Every finding below is tied to a specific file and line number in the current, post-comment-refinement tree. Where a suspicion could not be confirmed from source, it is labelled "Unverified – requires further investigation" and the missing information is named.

The offline suite was executed as part of this audit: 1326 tests passed, 5 were skipped, and 34 subtests passed. A small number of defects below are therefore latent rather than currently failing. Several high-severity items are explicitly documented design trade-offs; they are reported because they materially affect risk, not because they are accidental regressions.

## Executive Summary

### Health assessment

The codebase is coherent and unusually well instrumented for its size. It is organized into contracts, concrete components, assembly, and two entry points; safety-critical behaviour such as process spawning, stale-lock handling, path confinement, and repository-address checking is centralized in single modules. Input validation at the configuration boundary is thorough, file writes are staged through uniquely named temporary files, and the test suite is broad. The main weaknesses are concentrated in three places: startup ordering in the console entry point, approval defaults and the point at which tool arguments are classified, and resource lifecycle in the external-tool bridge and the terminal input path.

### Top three risks at audit time

1. The documented module invocation of the console crashed on nearly every slash command because the entry point ran before the command handlers were imported. Reproduced at runtime. Fixed under High-1.
2. The shipped approval default auto-approved every risk level, so destructive commands were classified but never blocked. Fixed under High-2.
3. A long escape sequence in streamed output could hang the interface indefinitely. Fixed under High-3.

All three are now closed; see Remediation Status.

### Architecture coherence

The layering is consistent with its own documentation: the orchestrator depends on abstract contracts, the registry is the only assembly point, and both entry points construct the same collaborators. Cross-layer coupling is limited but real: the evaluator imports a private redaction helper from the approval module, and the console facade relies on late imports from its command modules to preserve test patch seams. That late-import pattern was the direct cause of High-1.

## Remediation Status

All findings listed below have been addressed in code or in the test suite. The full offline suite passes: 1413 tests passed, 6 skipped, 34 subtests passed. Regression tests for the fixes live in `tests/test_closing_summary.py`, `tests/test_fix_agent_regressions.py`, `tests/test_fix_tui_regressions.py`, `tests/test_fix_tools_regressions.py`, `tests/test_fix_core_regressions.py`, and in the updated existing test modules.

Fixed in full: High-1 through High-5; Medium-1, Medium-3 through Medium-8, Medium-9 through Medium-18, Medium-19 through Medium-21; Low-1, Low-3 through Low-8, Low-10 through Low-21, Low-24 through Low-31. Low-5 and Low-32 required no code change.

Mitigated with residual risk, not eliminated:

- High-6: the host sandbox still executes through the host shell with the operator's authority. A one-time startup warning now makes the risk explicit; strong isolation still requires the container sandbox.
- Low-2: the POSIX direct-write path now uses a no-follow open and an inode check. Windows has no equivalent flag, so a narrow check-then-open window remains there.
- Low-3: only newly created lock files carry an owner token; legacy token-less locks still rely on the mtime check.
- Low-25: event subscription is locked and failures are counted, but a hard per-handler timeout would require an asynchronous dispatcher redesign.
- Medium-6: audit health is a conservative process-wide flag, so one failed write can refuse unrelated non-safe calls in confirming modes.
- Medium-12: the edit tool re-reads and compares bytes immediately before writing, shrinking but not eliminating the window because the sandbox has no atomic compare-and-swap write.
- Medium-14: external-tool tree kill remains best effort in the shared helper.
- Medium-18 and Low-8: the scripted credential command form still passes a key as a process argument; interactive entry is now masked.
- Low-31: a few terminal pacing sleeps remain because repaints have no deterministic completion event.

Two new behaviours were added beyond the findings: every interactive turn, the headless verdict, and saved-session replay now end with a plain-language closing summary, and the system prompt requires the model's own final answer to end with the same structure. This directly addresses the reported confusion where a conversation ended on a raw tool-output box.

A further defect was reported after the audit and fixed: prompts submitted while a turn was running were held in a single slot, so a second prompt overwrote the first, and an unexpected turn error discarded the slot before handoff. `core/tui/app.py` now uses an ordered queue drained inside the turn worker's teardown, and `core/console.py` no longer clears the application busy flag before that teardown. A follow-up report added that such prompts should reach the model without stopping the turn: `ConsoleSession.inject_message` and `AgentLoop.user_message_source` now append them to the context at the next step, with the UI queue as the fallback when the turn ends first. A rendering regression from an earlier cursor fix was also corrected: `core/tui/buffer.py` now emits a show-cursor sequence on cursor frames, because a modal frame hides it. Regression coverage is in `tests/test_queued_prompt_delivery.py`. Note that input typed while a blocking approval card or completion popup is open is still consumed by that overlay rather than the prompt box; that is separate from the queued-prompt path.

---

## Bug Registry

### High severity

#### High-1: Module invocation of the console crashes on slash commands

- **Location:** `core/console.py:1553-1554` and `core/console.py:1560-1600`; dispatch reads `_no_arg` at `core/console.py:1336` and `_model_command` at `core/console.py:1344`.
- **Description:** The `if __name__ == "__main__": sys.exit(main())` guard executes before the trailing block that imports `core.console_skills`, `core.console_commands`, and `core.console_model` into the module namespace. Under `python -m core.console`, `sys.exit` raises `SystemExit` and the remaining imports never execute, so the globals that dispatch reads are undefined. The facade re-exports cannot help because they are skipped on this path. Reproduced by piping `/workspace` into `python -m core.console --workspace <dir>`: `NameError: name '_no_arg' is not defined` at `core/console.py:1336`, called from `_repl_plain` and `main`. Only help, the bare slash, and exit avoid undefined names. The TUI path works because `core/tui/app.py:1745-1746` imports `core.console` as a separate, fully loaded module.
- **Impact:** Every slash command other than three crashes the plain or piped REPL when the documented module form is used. The launcher script and several tests use that form.
- **Severity:** High.
- **Remediation:** 1. Move the `__main__` guard to the end of `core/console.py`, after all re-export imports. 2. Keep the imports where they are but import them at module top under a type-checking guard so the names always exist. 3. Add a regression test that runs the module with piped stdin and a slash command, asserting a zero exit status.

#### High-2: Permissive approval mode is the shipped default

- **Location:** `core/config.py:47-50`; `core/agent/settings.py:37`; `core/agent/approvals.py:743-744`; `core/main.py:57-70` and `core/main.py:133`.
- **Description:** Both the run-configuration default (`"approvals": "yolo"`) and the user-settings default set the permissive mode. In `ApprovalPolicy.check`, after the planning and safe branches, `if self.mode == "yolo": return True` approves mutating, confirm, and destructive risks alike. The headless approver returns `True` unconditionally in that mode. The source comments themselves warn that this default belongs only in a disposable or trusted workspace.
- **Impact:** An operator who accepts defaults runs mutating and destructive tool calls without any confirmation; the risk classifier has no enforcement effect in the default configuration.
- **Severity:** High (documented design choice with material security consequence).
- **Remediation:** 1. Change both defaults to a confirming mode such as `default`. 2. Require an explicit operator action to select the permissive mode and persist it only through the settings API. 3. Emit a startup warning whenever the permissive mode is active.

#### High-3: Infinite loop when an escape sequence straddles the transcript partial-line cap

- **Location:** `core/tui/transcript.py:229-238`.
- **Description:** The chunking loop sets `cut = _PARTIAL_CAP`, then if an escape match begins before and ends after that cut it sets `cut = match.start()`. A match starting at offset zero therefore sets `cut = 0`, appends an empty string, leaves `tail` unchanged, and the `while len(tail) > _PARTIAL_CAP` condition never changes. The escape pattern at `core/tui/transcript.py:20` permits an unbounded parameter run, so such input is representable.
- **Impact:** The thread feeding streamed output never returns; the turn never completes and the interface keeps animating.
- **Severity:** High.
- **Remediation:** 1. Force progress with `cut = max(1, match.start())` when the match begins at or before zero. 2. Re-check the cut after assignment. 3. Add a regression test with an escape start followed by more than the cap's worth of parameter characters and no newline.

#### High-4: Instruction-file fence is evaluated before argument repair

- **Location:** `core/agent/approvals.py:562` and `core/agent/approvals.py:570`; `core/agent/loop.py:726` and `core/agent/loop.py:775`.
- **Description:** `classify` reads only `arguments.get("path", "?")` and compares the basename against the two protected filenames. The approver receives raw tool arguments at `core/agent/loop.py:726`, while alias repair that can convert `filePath` into `path` happens later in `_execute_tool` at `core/agent/loop.py:775`. With an argument object containing `filePath` instead of `path`, the classified path is the placeholder, so the write is ordinary mutating rather than confirm, and automatic mode allows it.
- **Impact:** The mid-session fence protecting instruction and memory files can be bypassed by using an alias key that the repair layer normalizes afterward.
- **Severity:** High.
- **Remediation:** 1. Run `repair_arguments` before the approval check. 2. Alternatively, make `classify` resolve every alias of the path key from the repair module's alias table. 3. Add a test that approves a write to a protected file using an alias key and asserts a confirm verdict.

#### High-5: Failed digest round trip permanently drops the evicted turns

- **Location:** `core/agent/loop.py:700-715`; `core/agent/context.py:199-203` and `core/agent/context.py:217`.
- **Description:** `_fold_evicted` renders `context.pending_evicted`, then calls `context.take_pending_evicted()` at line 700 before invoking the model. If the request raises (line 710) or returns empty (line 714), the function increments a failure counter and returns. The detached turns are then absent from the message list, from `pending_evicted`, and from the installed digest, and the loss counter is not incremented on this path.
- **Impact:** A transient summarizer failure silently loses exactly the turns that eviction was supposed to preserve, contradicting the lossless-eviction behaviour documented in the architecture guide.
- **Severity:** High.
- **Remediation:** 1. Detach the pending queue only after a successful, non-empty summary. 2. On both failure branches, re-insert the turns and increment the loss counter. 3. Add a test with a summarizer that raises once, asserting the turns remain in the pending queue.

#### High-6: Host sandbox is a heuristic screen, not a containment boundary

- **Location:** `core/sandbox.py:88-179` (traversal screening) and `core/sandbox.py:258-273` (command execution with `shell=True`).
- **Description:** The command screen is a set of regex and expansion checks; execution uses the host shell with the workspace as its working directory. The module's own comment at `core/sandbox.py:21-24` states containment cannot be guaranteed and recommends the container sandbox for strong isolation.
- **Impact:** A model-issued command that evades the screen can act outside the workspace with the full authority of the host account.
- **Severity:** High (explicitly documented trade-off).
- **Remediation:** 1. Use the container sandbox for untrusted work. 2. Replace string screening with an argument-vector API where possible. 3. Surface the residual risk in operator-facing setup documentation.

### Medium severity

#### Medium-1: Container path confinement fails open when the workdir is the filesystem root

- **Location:** `core/container.py:206-220` and `core/container.py:466-476`; `core/config.py:216`; `core/registry.py:195-208`.
- **Description:** `_validate_workdir` accepts `/` because normalization leaves it unchanged. `_is_safe_path` then computes `wd = self.workdir.rstrip("/")`, which becomes the empty string, so the containment test `normalized.startswith(wd + "/")` becomes `normalized.startswith("/")`, true for every absolute path. Verified by executing the static methods with `workdir="/"`: arbitrary absolute paths and parent-traversal paths were accepted, while the default workdir rejected them. The workdir is an operator configuration value forwarded by the registry.
- **Impact:** One configuration value disables workspace confinement for read and path checks in the container sandbox.
- **Severity:** Medium.
- **Remediation:** 1. Reject `/` and any workdir whose stripped form is empty. 2. Build the base as `normpath(workdir) + "/"` and test against that. 3. Add a regression test asserting arbitrary absolute paths are rejected.

#### Medium-2: Commit references beginning with a dash are accepted

- **Location:** `core/urlcheck.py:37-48`; `core/container.py:104-107`; `core/sandbox.py:207-213`.
- **Description:** `is_safe_commit` rejects shell metacharacters but not a leading dash. Verified: option-like strings such as `--upload-pack=x`, `--orphan`, and `-B` all return true. The value is passed as the final element of an argv list to the version-control checkout command.
- **Impact:** A task-supplied base commit can be interpreted as an option rather than a revision. The exact reachable effect depends on the installed version-control tool and is **Unverified – requires further investigation**; the guard gap itself is confirmed.
- **Severity:** Medium.
- **Remediation:** 1. Reject values starting with `-` in `is_safe_commit`. 2. Pass a position separator before the revision in both checkout calls. 3. Add tests for dash-prefixed values.

#### Medium-3: External-tool clients leak on startup failure and on one-shot console runs

- **Location:** `core/main.py:114-129` and `core/main.py:154-162`; `core/console.py:1443-1473`, `core/console.py:250-256`, and `core/console.py:1513-1524`.
- **Description:** In the headless runner, MCP clients are spawned at line 120, while the evaluator and logger are built afterward inside the same `try`. If either later builder raises, control returns before the `finally` that closes clients. In the console, the one-shot branch closes only the logger; the client-shutdown helper is called from the plain REPL and terminal paths but not there.
- **Impact:** Child server processes can survive a failed startup or a one-shot run.
- **Severity:** Medium.
- **Remediation:** 1. Initialize the client list before the builder `try` and close it in a covering `finally`. 2. Call the client-shutdown helper in the one-shot branch's `finally`. 3. Add tests asserting all clients are closed on a builder failure and after a one-shot run.

#### Medium-4: Secret redaction is not enforced at the log sink

- **Location:** `core/logs.py:1-6` and `core/logs.py:140-146`; `core/evaluators.py:42` and `core/evaluators.py:47`.
- **Description:** The logger's module documentation states it never redacts and that callers own redaction; `log()` serializes the payload as given. Redaction is applied only inside the command evaluator. Any other caller that places a secret-bearing value in a payload persists it verbatim.
- **Impact:** Secrets in tool or event payloads can reach the structured log in plaintext when a caller omits redaction.
- **Severity:** Medium.
- **Remediation:** 1. Apply the shared redaction helper centrally in `JsonlLogger.log` before serialization, keeping caller-side redaction as defence in depth. 2. Add a test that logs a credential-shaped value and asserts it is absent from the line.

#### Medium-5: Stored-credential redaction is silently skipped when the store is unreadable

- **Location:** `core/agent/approvals.py:199-205`.
- **Description:** The final redaction pass imports stored keys and replaces matching values, but the whole block is wrapped in `except Exception: pass`. If the credential store cannot be read, no stored values are redacted and no diagnostic is emitted.
- **Impact:** A secret present in a tool argument or audit detail can reach logs and event payloads unredacted when the store is temporarily unavailable.
- **Severity:** Medium.
- **Remediation:** 1. Narrow the exception to expected store errors. 2. Emit a one-time warning through the existing audit-failure warning path. 3. Consider refusing the audit write when redaction could not be completed.

#### Medium-6: Audit-log write failure is fail-open

- **Location:** `core/agent/approvals.py:601-610` and `core/agent/approvals.py:727-734`.
- **Description:** The audit write is inside a broad `try`. Any failure prints one stderr line through the warning helper, after which the decision logic continues; tool execution is not conditioned on the write succeeding.
- **Impact:** Tools can run with no audit trail, with only a transient stderr message as notice.
- **Severity:** Medium.
- **Remediation:** 1. Expose a persistent health flag or metric. 2. For modes above the permissive one, refuse mutating tools when the audit log cannot be written.

#### Medium-7: Plan mode refuses only a fixed set of tool names

- **Location:** `core/agent/approvals.py:16` and `core/agent/approvals.py:736-741`.
- **Description:** The planning guard tests membership in a hard-coded set of five names. A registered external tool or any future custom tool outside that set is not refused; if it classifies as safe it can execute.
- **Impact:** Plan mode is not a reliable read-only boundary for tools added after the set was written.
- **Severity:** Medium.
- **Remediation:** 1. In plan mode, allow only an explicit read-only allowlist and refuse everything else, including unknown and external tools.

#### Medium-8: Sandbox cleanup failures are discarded

- **Location:** `core/agent/loop.py:508-510`.
- **Description:** The `finally` block calls sandbox cleanup and discards any exception.
- **Impact:** A failed cleanup can leave processes, files, or containers behind while the result still reports success, with no event or metric.
- **Severity:** Medium.
- **Remediation:** 1. Emit a cleanup-error event and increment a metric. 2. Include a cleanup status flag in the run result.

#### Medium-9: Partial bracketed paste is replayed as typed keys

- **Location:** `core/tui/backend.py:700-706` and `core/tui/backend.py:797-806`.
- **Description:** While a bracketed paste has no end marker, the escape parser consumes nothing and leaves the whole sequence pending. When no data arrives for a short idle interval, every pending character is fed through the ordinary character path and becomes a key event. The console reader uses a longer grace window than the POSIX reader, so the two paths disagree.
- **Impact:** A paste split across a short gap is inserted as literal characters, including marker text and embedded newlines that can submit the prompt mid-paste.
- **Severity:** Medium.
- **Remediation:** 1. Detect a pending paste before idle replay and emit a single paste event or extend the timeout. 2. Replay only incomplete ordinary escape sequences as keys. 3. Add a split-paste regression test.

#### Medium-10: Transcript sanitizer only partially removes operating-system command sequences

- **Location:** `core/tui/transcript.py:18-20` and `core/tui/transcript.py:45-65`.
- **Description:** The sanitizer's pattern matches only control-sequence introductions. For an operating-system command sequence, only the escape byte is dropped; the following bracket and payload bytes are printable and are appended to the transcript. The corresponding patterns in the cell buffer and selection module do handle these sequences.
- **Impact:** Payload text from such sequences survives in transcripts and saved sessions.
- **Severity:** Medium.
- **Remediation:** 1. Reuse the broader pattern from the cell buffer. 2. Verify that only styling sequences remain.

#### Medium-11: Prompt history is mutated outside the submission lock

- **Location:** `core/tui/app.py:502-510` versus `core/tui/app.py:541-570`.
- **Description:** The submit path appends to history and updates the recall index before entering the lock that serializes busy checks and turn state. Submit is reachable both from the presenter thread and from the turn worker.
- **Impact:** Interleaved submissions can produce duplicate or out-of-order history entries and a stale recall index.
- **Severity:** Medium.
- **Remediation:** 1. Move history mutation and index update inside the same lock block as the busy check.

#### Medium-12: Edit tool has a lost-update window

- **Location:** `core/tools/files.py:596`, `core/tools/files.py:622`, and `core/tools/files.py:651`.
- **Description:** The tool reads the file, verifies the ledger hash, then writes the whole file. A concurrent change between the hash check and the full-file replacement is not detected, and the ledger is updated to the clobbering content afterward.
- **Impact:** A concurrent edit can be silently overwritten and then laundered in the ledger.
- **Severity:** Medium.
- **Remediation:** 1. Re-read and re-hash immediately before the write. 2. Make the replacement conditional on the expected bytes. 3. Return an error asking for a fresh read on mismatch.

#### Medium-13: Document-extraction shell fallback lacks a traversal screen

- **Location:** `core/tools/extract.py:147-165`.
- **Description:** When the sandbox has no direct file view, the fallback quotes the supplied path and runs shell commands through the sandbox. Unlike the file-tool glob branch and directory listing, it applies no parent-component or absolute-path refusal. The direct-view branch does enforce confinement.
- **Impact:** In a shell-based sandbox, a caller-supplied parent path can read files outside the intended workspace but inside that environment.
- **Severity:** Medium.
- **Remediation:** 1. Apply the same path checks as the read tool before building shell commands. 2. Or refuse the fallback for paths containing separators.

#### Medium-14: External-tool client process lifecycle gaps

- **Location:** `core/mcp/client.py:80-91` and `core/mcp/client.py:240-245`.
- **Description:** The child server is spawned without the shared process-group options used by the command tool, and shutdown signals only the direct child.
- **Impact:** Grandchildren spawned by a server can survive shutdown and hold inherited pipes.
- **Severity:** Medium.
- **Remediation:** 1. Use the shared process-spawn options. 2. Route shutdown through the shared tree-kill helper. 3. Join reader threads after reaping.

#### Medium-15: Concurrent external-tool requests can discard each other's responses

- **Location:** `core/mcp/client.py:139-142`, `core/mcp/client.py:148-187`, and `core/mcp/client.py:178`.
- **Description:** A request waits on one shared queue and discards any message whose identifier does not match, without re-queuing it. No lock serializes request and await pairs.
- **Impact:** Overlapping calls can cause one waiter to consume and drop another's response, producing a timeout.
- **Severity:** Medium.
- **Remediation:** 1. Serialize request and await with a mutex, or route responses by identifier to per-request queues.

#### Medium-16: Unbounded line buffers in the external-tool transport

- **Location:** `core/mcp/server.py:172-177`; `core/mcp/client.py:68`, `core/mcp/client.py:102-103`; `core/mcp/protocol.py:52-60`.
- **Description:** The server reads lines with no length check; the client places every line into an unbounded queue. The stderr ring, by contrast, is capped.
- **Impact:** A hostile peer can exhaust memory with one large line or a sustained flood.
- **Severity:** Medium.
- **Remediation:** 1. Enforce a maximum line length on both sides. 2. Bound the client queue with an explicit overflow error.

#### Medium-17: One malformed tool advertisement aborts startup

- **Location:** `core/mcp/client.py:319-329` and `core/mcp/client.py:271`.
- **Description:** Server startup and tool listing are inside a try that skips broken servers, but adapter construction is outside it and indexes a required key. A spec missing that key raises.
- **Impact:** One malformed item from an otherwise healthy server prevents harness construction, contradicting the stated skip-broken-server behaviour.
- **Severity:** Medium.
- **Remediation:** 1. Wrap each adapter construction individually and report failures through the error callback.

#### Medium-18: Interactive credential entry uses an echoing reader

- **Location:** `core/console_model.py:454-465` and `core/console_model.py:403-413`; `core/console_common.py:234-283` and `core/console_common.py:164-183`.
- **Description:** The add flow explicitly uses the echoing reader for the credential prompt. The replacement flow tries the masked reader but falls back to the echoing one on any exception.
- **Impact:** Typed credentials can appear in terminal scrollback or captured output.
- **Severity:** Medium.
- **Remediation:** 1. Route every credential prompt through the masked reader. 2. If none is available, refuse to read rather than falling back to echo.

#### Medium-19: Headless log-path test writes into the project tree

- **Location:** `tests/test_main_headless.py:133-138`; `core/main.py:105-108`.
- **Description:** The test configures a relative log path that the runner anchors to the project root, so a successful run writes into the real checkout. Its assertion accepts an unrelated filename in the temporary directory or the existence of the project-root file, so a stale file from an earlier run satisfies it.
- **Impact:** Residue in the source tree and a false pass condition.
- **Severity:** Medium.
- **Remediation:** 1. Monkeypatch the project-root constant to the temporary directory. 2. Assert the exact expected log file and remove the alternative branch.

#### Medium-20: Live probe invokes a non-existent file and consumes credentials without a gate

- **Location:** `tests/probe-console.ps1:14`, `tests/probe-console.ps1:2`, `tests/probe-console.ps1:5`, `tests/probe-console.ps1:8`.
- **Description:** The script passes `console.py` to the interpreter, but no such file exists at the repository root; the module is `core/console.py`. It also reads a real credential from user scope and performs a live call with no opt-in flag, using a single shared temporary directory.
- **Impact:** The probe fails to start, and an accidental run spends real credentials and network access; concurrent runs can delete each other's workspace.
- **Severity:** Medium.
- **Remediation:** 1. Invoke the module form. 2. Require an explicit live switch. 3. Use a unique per-run temporary directory.

#### Medium-21: Test name promises output normalization but asserts only a type

- **Location:** `tests/test_search_rich.py:74-83`.
- **Description:** The mocked backend returns plain filenames, not grep-style output, and the only assertion checks that the result is a string. The test's own comment says it is a sanity check, not a claim about output shape.
- **Impact:** A regression in the fallback output shape is not detected, and the name misrepresents coverage.
- **Severity:** Medium.
- **Remediation:** 1. Feed a realistic prefixed line and assert the normalized token. 2. Or rename the test to a does-not-raise check.

### Low severity

#### Low-1: Non-object logging configuration causes an unhandled exception

- **Location:** `core/main.py:106`; `core/config.py:291-303` and `core/config.py:318-320`.
- **Description:** The configuration validator checks that the language-model, sandbox, and evaluator sections are objects and skips type checking for a non-object logging section. The headless runner then calls `.get` on `config["logging"]` before its error-handling block. Verified at runtime with a string logging value: `AttributeError: 'str' object has no attribute 'get'` at `core/main.py:106`, instead of exit status two.
- **Impact:** A malformed configuration produces a traceback rather than the documented configuration error status.
- **Severity:** Low.
- **Remediation:** 1. Add `logging` to the required-object check in `merge_defaults`. 2. Optionally guard the runner access with a configuration error.

#### Low-2: Direct-write fallback has a check-then-open window

- **Location:** `core/sandbox.py:457-486`, especially `core/sandbox.py:481`.
- **Description:** The fallback re-runs confinement checks and then opens the target in a separate operation; a final-component symbolic link planted between the check and the open would be followed. Exploitability from the harness's own threat model is **Unverified – requires further investigation**: which callers can trigger the fallback and what local write primitives exist were not established.
- **Severity:** Low.
- **Remediation:** 1. Use a no-follow open and write through the descriptor, or remove the fallback.

#### Low-3: Lock files use default permissions

- **Location:** `core/locking.py:59`, `core/locking.py:40`, and `core/locking.py:81`.
- **Description:** The lock file is created without an explicit mode, so the process umask decides its permissions, and stale-lock breaking does not verify owner identity.
- **Impact:** On a shared host another user may be able to break or remove a live lock.
- **Severity:** Low.
- **Remediation:** 1. Create with owner-only mode. 2. Record and verify the owner before breaking.

#### Low-4: File-scheme repository URLs are gated by any truthy environment value

- **Location:** `core/urlcheck.py:25` and `core/urlcheck.py:32-34`; `core/sandbox.py:197`; `core/container.py:92`.
- **Description:** Any non-empty value, including a string that reads as false, enables local-path cloning.
- **Impact:** An inadvertently set variable widens repository address acceptance.
- **Severity:** Low.
- **Remediation:** 1. Parse strictly for `1` or `true`. 2. Scope the exemption to tests.

#### Low-5: Secret redaction at the sink is absent (evaluator-only coverage)

- Covered by Medium-4; listed here only to note that the evaluator's redaction is correct at `core/evaluators.py:42` and `core/evaluators.py:47`. No separate defect.

#### Low-6: Overlay and pager state is read outside its lock

- **Location:** `core/console.py:1171`; `core/tui/app.py:891`; `core/tui/app.py:426-434`; `core/tui/app.py:576-618`; `core/console.py:372`.
- **Description:** Pager-queue mutations are locked, but two truthiness checks are not. Overlay reply reads and writes in stop and finish helpers also omit the overlay lock.
- **Impact:** Benign under the current interpreter, but the contract is not enforced; a stop racing setup can lose an overlay answer.
- **Severity:** Low.
- **Remediation:** 1. Add locked accessors and route all reads and writes through them.

#### Low-7: Cross-thread tool-text collector has no reader

- **Location:** `core/console.py:462-464`; `core/tui/app.py:322` and `core/tui/app.py:529`.
- **Description:** Tool text is appended from the worker thread and reset from the main thread; no code consumes it.
- **Impact:** Dead state and an unsynchronized write and reset.
- **Severity:** Low.
- **Remediation:** 1. Consume it under the application lock where follow-up chips are built, or remove it.

#### Low-8: Scripted credential entry places secrets in command arguments

- **Location:** `core/console_model.py:517-528` and `core/console_model.py:533-538`.
- **Description:** Two command forms accept the credential as a token; masking affects only printed output.
- **Impact:** Credential disclosure through shell history or process arguments.
- **Severity:** Low.
- **Remediation:** 1. Support reading the value from an environment variable or a masked prompt.

#### Low-9: Partial credential material is displayed

- **Location:** `core/agent/keys.py:348-354`; used at `core/console_model.py:402`, `core/console_model.py:419`, `core/console_model.py:452`, `core/console_model.py:457`, `core/console_model.py:465`, `core/console_model.py:528`, and `core/console_session_endpoints.py:212`.
- **Description:** The mask helper shows the first and last four characters of a credential.
- **Impact:** Screenshots or recordings expose real key characters.
- **Severity:** Low.
- **Remediation:** 1. Use a non-reversible fingerprint or a fixed mask.

#### Low-10: Autosave failure is announced only once

- **Location:** `core/console_session_persist.py:214-220`; `core/agent/sessions.py:159-161`, `core/agent/sessions.py:178-182`, `core/agent/sessions.py:197-202`.
- **Description:** A single warning is printed on the first failed save; later failures are silent.
- **Impact:** A long session can go entirely unsaved after the first notice.
- **Severity:** Low.
- **Remediation:** 1. Re-warn at intervals or on transition from success to failure, and expose save state in the status row.

#### Low-11: Operating-system command sequence resets styling in the cell parser

- **Location:** `core/tui/buffer.py:80-84`.
- **Description:** The OSC alternative of the escape pattern has no capture groups, so the parser treats it as a style reset.
- **Impact:** An embedded sequence clears color and weight for the rest of the line.
- **Severity:** Low.
- **Remediation:** 1. Skip OSC matches explicitly in the style loop.

#### Low-12: Window-resize signal handler is not restored

- **Location:** `core/tui/backend.py:315-320` and `core/tui/backend.py:330-333`.
- **Description:** The previous handler is not saved, and teardown installs the default unconditionally. Interrupt handling elsewhere is saved and restored correctly.
- **Impact:** An embedding process loses its own resize handler.
- **Severity:** Low.
- **Remediation:** 1. Save and restore the previous handler in the existing guarded teardown.

#### Low-13: Paste marker prefix and trailing-byte handling

- **Location:** `core/tui/backend.py:798` and `core/tui/backend.py:803-806`.
- **Description:** The paste branch tests a shorter prefix than the full marker, and the oversized-paste path consumes trailing bytes from the same read without decoding them.
- **Impact:** A non-paste control sequence can be treated as a paste, and input arriving with an oversized paste can be lost.
- **Severity:** Low.
- **Remediation:** 1. Test the full marker. 2. Report the cap position as the consumed count.

#### Low-14: Hardware cursor position persists across modal frames

- **Location:** `core/tui/buffer.py:194`, `core/tui/buffer.py:201-202`, `core/tui/buffer.py:259-261`; `core/tui/app.py:1192-1219`, `core/tui/app.py:1370`.
- **Description:** The cursor is set only in the normal frame path and is never cleared for minimal, review, or panel frames.
- **Impact:** The hardware cursor can sit over a modal frame.
- **Severity:** Low.
- **Remediation:** 1. Reset the stored cursor at the start of each frame composition.

#### Low-15: Width parameter has two interpretations

- **Location:** `core/tui/buffer.py:145-149` and `core/tui/buffer.py:173-178`.
- **Description:** One method treats the width as relative to the starting column, the other as an absolute bound. Current callers pass the full column count, so the interpretations coincide. Callers outside the audited cluster were not reviewed; this is **Unverified – requires further investigation**.
- **Impact:** None observed; a future caller could truncate at the wrong column.
- **Severity:** Low.
- **Remediation:** 1. Normalize the parameter to an absolute bound.

#### Low-16: Session-panel clamp mixes entry counts with row counts

- **Location:** `core/tui/app.py:1604-1607`; `core/tui/sessionpanel.py:52-62`.
- **Description:** The clamp receives a count of entries as the total and a count of screen rows as the viewport, although each entry renders two rows.
- **Impact:** A stale or externally set offset could scroll to a blank list.
- **Severity:** Low.
- **Remediation:** 1. Divide the available rows by two, matching the key-handler math.

#### Low-17: Clipboard fallback bypasses the terminal write lock

- **Location:** `core/tui/clipboard.py:170-179`; `core/tui/backend.py:192-202`.
- **Description:** The fallback writes directly to standard output rather than through the backend's locked writer. Current callers are on the presenter thread; other threads were not reviewed and this is **Unverified – requires further investigation**.
- **Impact:** A future background copy could interleave escape bytes into a frame.
- **Severity:** Low.
- **Remediation:** 1. Route the fallback through the locked writer.

#### Low-18: Glob validation and response-header limits lack consistency

- **Location:** `core/tools/files.py:222`; `core/tools/web.py:648-653`.
- **Description:** The glob check omits the drive-prefix pattern used by directory listing, though later realpath confinement prevents a content leak. The web fetch caps the body and inflated size but has no explicit header-block limit.
- **Impact:** Extra files from another drive root can appear in results; a hostile server can force header-proportional allocation.
- **Severity:** Low.
- **Remediation:** 1. Reuse the drive-prefix pattern and realpath-check each glob result. 2. Add a header-size ceiling before reading the body.

#### Low-19: External-tool server does not cap tool output

- **Location:** `core/mcp/server.py:145-152`; `core/tools/commands.py:973-975`.
- **Description:** Observations are serialized without a size cap; at least one tool can return an unbounded command output string.
- **Impact:** A single oversized protocol line can defeat the bounded-output convention.
- **Severity:** Low.
- **Remediation:** 1. Cap the observation and append a truncation note before serialization.

#### Low-20: Background reader can outlive a failed tree-kill

- **Location:** `core/tools/commands.py:465-510`, `core/tools/commands.py:528-535`; `core/procutil.py:47-77`.
- **Description:** Tree kill is best effort and falls back to the direct child. The abort paths join the reader thread with a timeout but do not force-close the pipe on timeout.
- **Impact:** A daemon thread can remain blocked on the pipe and hold a handle until process exit.
- **Severity:** Low.
- **Remediation:** 1. Close the pipe after the join timeout and record a cleanup warning.

#### Low-21: Windows command guard splits on quotes

- **Location:** `core/tools/commands.py:229-245`.
- **Description:** The guard splits on separators without respecting quoting before inspecting first tokens.
- **Impact:** An ordinary command can be refused with a misleading message; this is a usability issue, not a confinement bypass.
- **Severity:** Low.
- **Remediation:** 1. Tokenize with a platform-appropriate splitter or match only an unquoted command-position token.

#### Low-22: Memory rewrite does not take the in-process lock used by append

- **Location:** `core/agent/knowledge.py:219`, `core/agent/knowledge.py:477-527`.
- **Description:** Append holds both an in-process lock and a file lock; rewrite holds only the file lock. Whether the file lock serializes threads inside one process is **Unverified – requires further investigation** because the lock implementation in `core/locking.py` was not part of that cluster.
- **Impact:** A rewrite could race an append and clobber lines if the file lock is inter-process only.
- **Severity:** Low.
- **Remediation:** 1. After confirming lock semantics, take the in-process lock in rewrite too.

#### Low-23: Lock release calls are unguarded

- **Location:** `core/agent/knowledge.py:288`, `core/agent/knowledge.py:527`; `core/agent/settings.py:164`, `core/agent/settings.py:188`; `core/agent/sessions.py:205`.
- **Description:** Lock release is called directly in `finally` blocks. Whether it can raise is **Unverified – requires further investigation** pending confirmation of the lock implementation.
- **Impact:** A raising release could mask the original outcome.
- **Severity:** Low.
- **Remediation:** 1. Wrap each release in a narrow exception handler.

#### Low-24: ACL helper ignores the command result

- **Location:** `core/agent/permissions.py:18-22`; `core/agent/keys.py:159-163`, `core/agent/keys.py:173-179`.
- **Description:** A subprocess is run without checking its return code, and callers only catch exceptions.
- **Impact:** Owner-only permission tightening can silently fail.
- **Severity:** Low.
- **Remediation:** 1. Inspect the return code and warn when tightening did not take effect.

#### Low-25: Event bus subscription is unsynchronized and observers are unbounded

- **Location:** `core/agent/events.py:18-36`; `core/agent/loop.py:966-974`.
- **Description:** Handler lists are mutated without a lock while emission copies them; a slow handler blocks emission synchronously and failures are only printed to standard error.
- **Impact:** Concurrent subscription changes are undefined and a slow observer delays the loop.
- **Severity:** Low.
- **Remediation:** 1. Guard the handler list with a lock or immutable snapshot. 2. Bound handler execution and record failures as metrics.

#### Low-26: Approved, recorded, and executed arguments can differ

- **Location:** `core/agent/loop.py:398`, `core/agent/loop.py:726`, `core/agent/loop.py:775`.
- **Description:** The context serializes raw arguments and the approver sees raw arguments, but execution replaces them with repaired values.
- **Impact:** Approval records can disagree with what actually ran.
- **Severity:** Low.
- **Remediation:** 1. Repair once before both approval and serialization.

#### Low-27: Inter-process write locks are best effort

- **Location:** `core/agent/keys.py:261-275`; `core/agent/settings.py:150-154`; `core/agent/sessions.py:154-156`.
- **Description:** The credential store proceeds without a lock when acquisition fails; settings and sessions skip the write on timeout.
- **Impact:** Lost updates or silently skipped saves. Atomic replacement still prevents file corruption.
- **Severity:** Low.
- **Remediation:** 1. Retry with backoff and report skipped saves through the existing error surfaces.

#### Low-28: Multiple temp directories are never cleaned up

- **Location:** `tests/test_attention.py:28`; `tests/test_audit_round3.py:49-50`, `tests/test_audit_round3.py:63`, `tests/test_audit_round3.py:102`; `tests/test_fixes_plumbing.py:335`; `tests/test_fixes_tools.py:137`, `tests/test_fixes_tools.py:185`, `tests/test_fixes_tools.py:201`, `tests/test_fixes_tools.py:276`, `tests/test_fixes_tools.py:290`, `tests/test_fixes_tools.py:335`, `tests/test_fixes_tools.py:359`; `tests/test_console_session.py:47`, `tests/test_console_session.py:81`, `tests/test_console_session.py:348`, `tests/test_console_session.py:382`, `tests/test_console_session.py:422`, `tests/test_console_session.py:510`, `tests/test_console_session.py:674`; `tests/test_smoke.py:52-58`; `tests/test_remediations_round2.py:45-46`, `tests/test_remediations_round2.py:49-50`; `tests/tui_harness.py:105-117`; `tests/test_tui_surfaces.py:644`.
- **Description:** These directories are created without paired cleanup, unlike nearby tests that register removal.
- **Impact:** Temporary directories accumulate across runs and stale files can affect later tests.
- **Severity:** Low.
- **Remediation:** 1. Register removal immediately after creation or convert helpers to yielding fixtures.

#### Low-29: Several tests make weak or no assertions

- **Location:** `tests/test_logging_evaluator.py:63-69`; `tests/test_fixes_plumbing.py:67-77`; `tests/test_smoke.py:995-1001`; `tests/test_page_audit.py:108-115`; `tests/test_console_split.py:134-141`.
- **Description:** These tests rely only on "no exception raised", accept both branches of an either-or assertion for a fixture with a known value, or check a result caused by unrelated setup.
- **Impact:** A no-op or regressed implementation can pass.
- **Severity:** Low.
- **Remediation:** 1. Assert observable postconditions. 2. Pin exact expected values with a separate fixture for the opposite case. 3. Assert that patched seams were called.

#### Low-30: Test isolation defects

- **Location:** `tests/test_skills.py:106-112`; `tests/test_console_mcp.py:69-74`; `tests/test_smoke.py:7-9`; `tests/test_attention.py:16-17` and `tests/test_memory.py:9-10` (original comments).
- **Description:** One fixture removes a pre-existing skills-root override instead of restoring it; one teardown swallows client-close exceptions; two module docstrings and two comments pointed store isolation at an empty package initializer or a non-existent test conftest rather than the root conftest. Two of the comments were corrected during comment refinement; the docstring remains.
- **Impact:** Environment leakage, invisible teardown failures, and misleading maintenance guidance.
- **Severity:** Low.
- **Remediation:** 1. Restore prior environment values in LIFO order. 2. Narrow or report teardown exceptions. 3. Correct the remaining docstring at `tests/test_smoke.py:7-9`.

#### Low-31: Environment-dependent and timing-sensitive tests

- **Location:** `tests/test_audit_round3.py:315-317` and `tests/test_audit_round3.py:301-313`; `tests/test_tui.py:105`, `tests/test_tui.py:221-223`, `tests/test_tui.py:312`, `tests/test_tui.py:450`, `tests/test_tui.py:504-516`; `tests/test_tui_app.py:633`, `tests/test_tui_app.py:677-682`, `tests/test_tui_app.py:688`.
- **Description:** One test calls the real resolver without a gate; another asserts a wall-clock threshold tighter than its own stub delay. Several terminal tests use fixed sleeps alongside bounded wait helpers.
- **Impact:** Environment-dependent and flaky failures.
- **Severity:** Low.
- **Remediation:** 1. Patch the resolver with a fixed answer. 2. Widen the timing margin or assert an event instead of elapsed time. 3. Prefer bounded wait helpers over fixed sleeps.

#### Low-32: Comment drift found and corrected

- **Location:** `tests/test_attention.py:16-17`, `tests/test_memory.py:9-10`, `tests/test_keys_knowledge.py:29`, `tests/tui_harness.py:120-124`, `tests/test_attention.py:80-81` in `tests/test_audit_remediations.py` equivalent, and `rules/commands.rules:19` (originals).
- **Description:** Several comments cited a non-existent test module, an empty initializer, or described coverage the test did not provide. They were refined in place during Phase 4; no code changed.
- **Impact:** Resolved. Retained here as a record of documentation drift found by the audit.
- **Severity:** Low.
- **Remediation:** None required. Add a documentation-lint check if drift recurs.

---

## Quality & Maintainability Enhancements

1. **Late-import facade pattern.** `core/console.py:1560-1600` imports command handlers after the module body and after the `__main__` guard. This preserves test patch seams but creates an invalid state when the module is executed directly. Prefer explicit dependency injection or a single entry-point module that imports the facade, so the console module is always fully initialized before use.

2. **Duplicate default-approval declarations.** The permissive mode appears in `core/config.py:50` and `core/agent/settings.py:37`, with precedence logic spread across `core/console.py:1450-1465` and `core/agent/approvals.py:743-744`. Consolidate the precedence into one function and derive both defaults from one constant.

3. **Private cross-layer import.** `core/evaluators.py:5` imports a private redaction helper from `core/agent/approvals.py`. Move redaction to a neutral utility module and have both callers import from it. This also enables the centralized redaction recommended in Medium-4.

4. **Argument lifecycle split.** Approval sees raw arguments at `core/agent/loop.py:726`; repair happens at `core/agent/loop.py:775`; serialization happens at `core/agent/loop.py:398`. Repair once at response validation and pass a single immutable argument object to every consumer.

5. **Broad exception swallowing.** Confirmed at `core/agent/loop.py:508-510`, `core/agent/loop.py:966-974`, `core/agent/events.py:29-36`, `core/agent/approvals.py:199-205`, `core/agent/approvals.py:727-734`, `tests/test_console_mcp.py:69-74`, and `tests/conpty_harness.py:319-321`, `tests/conpty_harness.py:493-495`, `tests/conpty_harness.py:542-548`. Keep best-effort semantics where required, but emit a metric or diagnostic in each catch so failures are observable.

6. **Validation-table duplication.** Adding a component requires edits to both `core/config.py:181-229` and `core/registry.py:42-82`. Generate the validation tables from the registry's constructor signatures where practical, or add a single test that asserts the two sets stay in sync.

7. **Unbounded collections.** The MCP client queue at `core/mcp/client.py:68`, the transcript partial buffer in `core/tui/transcript.py`, and the pager queue in `core/console.py:372` all need explicit ceilings; the surrounding code already follows this convention elsewhere.

8. **Test helper isolation.** `tests/_helpers.py`, `tests/test_smoke.py:52-58`, `tests/test_remediations_round2.py:45-50`, and `tests/tui_harness.py:105-117` create workspaces without cleanup. Convert them to fixtures with `yield` teardown.

9. **Test documentation accuracy.** The docstring at `tests/test_smoke.py:7-9` still describes an empty initializer as the source of store isolation. Correct it to name the root conftest.

10. **Comment discipline.** The audit refined 170 comments across 57 files, mostly shortening multi-line historical rationale while preserving intent. A few remaining long comment blocks, such as `rules/commands.rules:2` and `core/tools/commands.py` section headers, are accurate and were left unchanged.

---

## Prioritised Action Plan

Ordered by risk, then by effort.

1. **Fix the console module-invocation crash** (High-1). Move the `__main__` guard below all imports and add a piped-command regression test. Low effort, immediate functional impact.
2. **Change the shipped approval defaults to a confirming mode** (High-2). Two constants plus a startup warning. Low effort, highest security impact.
3. **Fix the transcript chunking infinite loop** (High-3). Guarantee forward progress and add a cap-straddling test. Low effort, availability impact.
4. **Repair arguments before approval and serialization** (High-4, Low-26). One ordering change in the loop plus a fenced-file regression test. Medium effort, closes a safety-fence gap.
5. **Preserve evicted turns on summary failure** (High-5). Detach only after success and re-insert on failure. Medium effort, protects conversation integrity.
6. **Close external-tool clients on every exit path** (Medium-3, Medium-14, Medium-16, Medium-17). Add covering `finally` blocks, process-group spawning, line caps, and per-item error handling. Medium effort.
7. **Centralize redaction in the log sink and narrow redaction exception handling** (Medium-4, Medium-5, Medium-6). Medium effort, reduces secret-exposure paths.
8. **Close the container workdir and commit-validation gaps** (Medium-1, Medium-2, Low-4). Input validation changes with regression tests. Low effort.
9. **Serialize MCP requests and bound transport buffers** (Medium-15, Medium-16, Low-19). Medium effort, prevents cross-request loss and memory exhaustion.
10. **Make the edit tool write conditional** (Medium-12) and **screen the extraction fallback path** (Medium-13). Medium effort, closes lost-update and read-scope gaps.
11. **Route all credential prompts through the masked reader** (Medium-18, Low-8, Low-9). Low effort, reduces disclosure.
12. **Repair test hygiene** (Medium-19, Medium-20, Medium-21, Low-28 through Low-31). Add cleanup registration, gate live probes, pin assertions, and remove environment dependence. Medium effort, improves signal.
13. **Apply the remaining low-severity hardening** in the order listed, batching the terminal-state and lock-handling changes into single passes.
14. **Adopt the maintainability changes** in the Quality section as follow-up refactors, starting with the redaction utility and the console entry-point structure.

### Unverified items requiring further investigation

- Exact reachability of dash-prefixed commit options under the installed version-control tool (Medium-2).
- Whether the host-sandbox direct-write fallback can be reached by a caller with a local write primitive (Low-2).
- Whether the process file lock serializes threads inside one process, which determines the impact of the memory-rewrite and lock-release findings (Low-22, Low-23).
- Callers outside the TUI cluster of the width helper and the clipboard fallback (Low-15, Low-17).
- Exact effect of the installed version-control tool on option-like commit references and of the container runtime on the workdir root configuration.
