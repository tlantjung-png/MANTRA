# Second E2E Test — Work-in-Progress Status Report

## Objective
Add `EndToEndConsoleTest.test_tool_call_and_approval_card` to `tests/test_tui.py`: one compact agent turn driven through the real console harness, where the scripted LLM produces a mutating tool call, the interactive approval policy surfaces the `allow?` QuestionCard, the test confirms it with `y`, and the terminal shows the tool result.

## What is done

- Mapped the full approval path in the codebase:
  - `ApprovalPolicy.check()` gates mutations under `default` mode; `ApprovalPolicy._confirm()` calls `ApprovalPolicy._ask()`, which the TUI overrides to `TuiApp.ask_approval()`.
  - `TuiApp.ask_approval()` renders `QuestionCard("allow?", prompt, choices="yna")` into `self.overlay`, parks a `queue.Queue` in `self._overlay_reply`, and blocks the tool thread on `reply.get()`.
  - `QuestionCard.render()` draws the title, the prompt, and the hint line `[y]es   [n]o   [a]lways for this session`.
  - Typing `y` in `_handle_key()` calls `QuestionCard.consume_key("y", ...)` which sets `answer="y"`, then `_finish_overlay("y")` enqueues the answer back to the blocked tool thread.
  - The agent loop (`_dispatch_tool`) calls `approver.check()`; on confirmation it executes the tool, then emits `tool_call` + `tool_result` events that the session renders into the transcript.

- Verified the scripted-LLM turn shape in-process:
  - A `ScriptedLLMClient` seeded with a `tool_call_response("write_file", ...)` followed by a final `LLMResponse(content=...)` is consumed exactly once per `session.handle()` call: the first LLM call yields the tool call, the session executes it, feeds the observation back, and the second LLM call yields the final reply. The script is empty afterward.
  - With `approvals="default"`, `session.approvals.check("write_file", {...})` `False`s until the interactive `_ask` callback answers `"y"`; in `auto` mode mutations are allowed without a prompt.

- Identified the test harness boot recipe’s actual approvals mode:
  - The harness writes `settings.json` with `active`, `endpoints`, and `approvals` keys and sets `MANTRA_SETTINGS` so `ConsoleSession`/the live app reads the saved active pick.
  - The live app (`python -m core.console`) resolves `--config` to `examples/config.json` by default; that example file sets `approvals: "auto"` and `console.py main()` only merges the *active pick* into the LLM sub-config, not the top-level `approvals` key from `settings.json`.
  - So a harness boot that does not pass `--approve default` sees `approvals=auto` from the example config, which lets mutations through without surfacing the card.

- Wrote the test scaffolding in `tests/test_tui.py` inside `EndToEndConsoleTest`:
  - Boot recipe now writes a `settings.json` that includes `"approvals": "default"` (intended).
  - Prompt submit is paced with the same backspace-clear + text + Enter pattern as the existing help round-trip test.
  - After the prompt it waits for the card hint line `[y]es   [n]o   [a]lways for this session`, then types `y` + Enter, then waits for the turn tail (`ENCHANTER`) and asserts on the `!!` network-error banner (the only rendered output for the dead-endpoint scripted turn).

## Where it is stuck

The new test still fails at:
```python
seen_card = pty.wait_for("[y]es   [n]o   [a]lways for this session", timeout=40)
```
i.e. the approval card never appears in the captured terminal output.

The recipe writes `"approvals": "default"` into `settings.json`, but the live console does not necessarily honor that key from `settings.json` for the session’s approval mode — the boot path reads `examples/config.json` for `approvals` and only merges the active *endpoint/model* pick from the saved settings. In the probes, the child banner repeatedly showed `approvals  auto`, and the turn always went straight to the network-error path instead of the approval card.

So the mismatch is one of config propagation, not of the approval wiring itself: either
- the harness boot recipe must force the approval mode via `--approve default` (or the equivalent arg), or
- the test recipe/assertions need to match whatever approval mode the bootstrapped console actually uses.

The test body also still has a long fixed sleep (`time.sleep(40.0)`) before the card wait, and a second large sleep before the final `ENCHANTER` wait — both need replacing with proper `pty.wait_for(...)` stages once the config path is settled.

## Recommended next steps for the other LLM

1. Decide the intended approvals mode for this test and make the boot recipe authoritative for it:
   - Prefer passing `--approve default` (or the harness equivalent) so the recipe is self-documenting and does not depend on `settings.json` top-level key merging behavior.
   - If the harness does not expose that arg, set `approvals="default"` in the `settings.json` recipe *and* confirm the live console actually reads that key into `ConsoleSession.approvals.mode` in this repo’s boot path.

2. Replace the fixed sleeps with staged `pty.wait_for(...)` calls:
   - `MANTRA` banner at boot,
   - the approval card hint line after the prompt,
   - the `ENCHANTER`/final reply banner after confirmation.

3. Pick assertion anchors that are stable for whatever path the turn takes:
   - If `approvals=default` and the scripted turn reaches the card: assert on `allow?` and the `[y]es ...` hint, then on `ENCHANTER`/final reply after confirm.
   - If the test intentionally runs with `approvals=auto` (no card): assert on the scripted final reply only.

4. Run only `EndToEndConsoleTest::test_tool_call_and_approval_card` repeatedly until it is green before re-running the full suite.

## Test file location
- `tests/test_tui.py` — class `EndToEndConsoleTest`, method `test_tool_call_and_approval_card`.
- Harness: `tests/conpty_harness.py`.

## Notes for the other LLM
- This test is Windows-only and depends on the ConPTY/harness transport selection already in place.
- Headless CI hosts in this environment do not deliver output through a real `CreatePseudoConsole` session reliably; the harness already has the `conhost --headless` fallback transport selected at import time by `_probe_pseudoconsole_works()`.
- Keep the test hermetic: the scripted LLM should be the only LLM client for the turn, and the endpoint in the recipe should remain a dead localhost address so no network is touched.
