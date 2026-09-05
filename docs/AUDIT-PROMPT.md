# Full Audit Prompt: MANTRA Agent Harness (Repeat Audit, Delta-Focused)

> Copy everything below the marker into a fresh session to run a follow-up
> audit. Keep the metadata block current after each audit (last-audit commit,
> test count, findings count).

**Last audit:** 2026-09-05, commit `c14a80d` — 48 findings fixed (1 Critical,
4 High, 17 Medium, 26 Low). Full suite at audit time: 771 passed, 1 skipped.

---

## Context You Must Load First

MANTRA is a terminal-based coding-agent harness (~17.5k lines, Python 3.14,
src-layout, zero heavy dependencies — stdlib urllib/http.client, no SDK).

Architecture map:

- `src/mantra/main.py` — headless entry point; wires config into AgentLoop
- `src/mantra/console.py` (~5.5k lines) — REPL session: streaming markdown
  renderer, @-mentions, slash commands, diff panes, pager, endpoint/key
  management, autosave. Largest and most UI-fragile file.
- `src/mantra/line_editor.py` (~1.5k lines) — readline replacement:
  completion popup, bracketed paste, mouse-selection auto-copy,
  platform-split escape parsing (msvcrt vs termios)
- `src/mantra/compact.py` — alternate-screen TUI viewport
- `src/mantra/core/agent_loop.py` — chat/tool loop, repeat-call breaker,
  usage accounting, abort handling
- `src/mantra/core/context.py` — bounded history (char budgets, turn eviction)
- `src/mantra/core/approvals.py` — risk classification (safe/mutating/
  destructive), mode gating, redacted pre-tool-use audit log
- `src/mantra/core/{sessions,settings,workflows,knowledge}.py` — JSON
  persistence under `~/.mantra` with atomic writes + exclusive-create locks
- `src/mantra/core/keys.py` — plaintext credential store (0o600)
- `src/mantra/implementations/llm/openai_client.py` — stdlib chat client:
  buffered + SSE streaming, retries, 400-field downgrades, Responses-API
  fallback
- `src/mantra/implementations/tools/` — command_tool (run_command /
  shell_output / kill_shell, background task registry), file_tools,
  web_tools (SSRF guard), search_tools, edit_ledger
- `src/mantra/implementations/sandbox/` — local (host, cwd-isolated) and
  docker (one container per run)
- Tests: `tests/` (14 files + `test_audit_remediations.py`), run with pytest

## History — Do Not Re-Report

A full audit (2026-09-05, commit c14a80d) found and fixed 48 issues:
1 Critical (background exec escaping the docker sandbox), 4 High (port-kill
PID parsing, wrapper-command classification truncation, mid-stream network
drop silently truncated, oversized seed bypassing truncation), plus medium/
low items. All fixes carry regression tests in `test_audit_remediations.py`.

Known intentional trade-offs — flag only if they have CHANGED for the worse:

- Credentials are plaintext JSON at rest (documented residual risk;
  keychain integration deferred)
- JsonlLogger drops a record after 0.5s lock-contention timeout
- Model-catalog naming heuristics are non-gating
- `_read_tail`/`_truncate` loop caps and the 2-message eviction floor are
  documented decisions

## Audit Scope

Phase 1 — Reload knowledge: read the file map above, then diff-scan
`git log <last-audit-commit>..HEAD` and read every changed hunk fully.
For unchanged files, targeted re-verification only (hot spots listed in
Phase 2). Re-derive the module relationship graph; check that docstrings
still match behavior after recent changes.

Phase 2 — Systematic audit, prioritized by trust boundary:

1. Tool-execution boundary: command_tool, both sandboxes, approvals
   (any NEW path from model request to host process? any way around
   `sandbox.exec`? screening fail-open anywhere?)
2. LLM/network: openai_client (streaming, retry, downgrade, fallback
   interactions — these paths interleave in subtle ways)
3. Persistence: sessions/settings/workflows/knowledge/keys (atomicity,
   lock handling, error-path contracts)
4. web_tools SSRF guard (check-then-connect gaps)
5. UI layer: console, line_editor, compact, menu, term (state machines,
   width math, escape handling — audit only what changed or what the
   prior audit's fixes touched)
6. Cross-cutting: new dead code, doc/code drift, test gaps for new code

Hunt for: injection, auth/access flaws, exposed secrets, logic errors,
off-by-one, race conditions, edge-case crashes (None, empty, encoding,
wide chars), type coercion bugs, resource leaks, performance cliffs,
dead/redundant code introduced since last audit.

## Verification Standard

- Every finding must cite file:line and be verified against actual
  runtime context and real dependency versions. No speculation; mark
  uncertain items explicitly as speculative.
- Before reporting, check the fix history: is this a regression of a
  fixed issue, a variant of one, or genuinely new? Label it as such.
- Run the test suite (`python -m pytest tests/ -q`) and report the result
  as part of the audit; new findings should come with a failing-test
  demonstration where feasible.
- False positives from the last audit (documented above as intentional
  or actually-used code) must not reappear.

## Output

Single Markdown report: (1) Executive summary with issue counts and
delta vs. last audit, (2) findings grouped by severity then category —
each with file:line, plain-English description, severity, actionable
prose recommendation, (3) regression status of previous fixes (verify
each test in `test_audit_remediations.py` still passes and still guards
the original defect), (4) overall health verdict for daily-driver use.

## Constraints

No code output — prose recommendations only. No emojis. Plain Markdown,
one document. Do not modify any files; this is a read-only audit.
