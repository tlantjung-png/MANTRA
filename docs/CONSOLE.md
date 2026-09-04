# Console

## Terminal Interface

The console module provides the interactive terminal interface using only standard-library facilities. It implements styling via terminal escape sequences, with a flag to disable styling for non-terminal output, and ensures that escape sequences on the host console are enabled where needed. The console forces UTF-8 output so box-drawing and wide characters render on narrow codepages.

The screen is a compact full-screen layout with an alternate screen. It reserves a top information bar showing workspace, model, approval mode, and cache rate, a scrollable content viewport, a border row, and a fixed bottom prompt box. The prompt box is closed with corners and a right wall so long input never escapes it. A scroll marker rides the border row while the viewport is scrolled up. The layout wraps long lines at word boundaries, counting wide characters double, and re-emits open style codes when a wrap lands inside a coloured word. Resize events re-wrap stored content and keep the same logical line at the top of the viewport.

The startup card is a centered adaptive block displaying the product name, tagline, and version. It disappears after the first turn.

## Session Model

The console session owns the workspace, sandbox, context manager, system prompt, tools, client, approvals, and event bus. It maintains totals for tokens, turns, and errors, recent tool history, and per-turn cache metrics including hit-rate trends. The sandbox is the host directory sandbox over the persistent workspace, and an empty repository is initialized if the workspace lacks one.

The session manages a streaming renderer that buffers fragments until line boundaries to apply formatting while tracking code-fence state, with sanitization of terminal escape sequences from model output. A spinner runs on a background thread while the model is working and yields to real output. The viewport throttles rapid fragments to avoid flicker and forces a flush at stream completion so the final fragment is never delayed.

While a task streams, a turn-scoped reader keeps native mouse selection working on the host and buffers non-scroll keys for the next prompt. Tool observations render as budgeted boxes whose overflow is queued for an empty-Enter pager that pages through a screenful at a time.

## Line Editor

The line editor reads single keys and provides completion for commands and workspace paths, with handling for navigation keys, deletion, and history, and a popup that can be dismissed or re-invoked via a dedicated key. The popup supports filtering as the operator types, selection via arrow keys, a hint area, and a count hint when the terminal is too small to render. Multi-line input renders as an upward-growing paste box with a size chip. Bracketed paste is assembled with an idle timeout, and mouse drag or click on a transcript line copies the selected text to the clipboard with a status flash. When standard input or output is not a terminal, the editor falls back to plain line input.

## File References

At-sign mentions are expanded by resolving tokens relative to the workspace, rejecting escapes via resolved path checks, and expanding globs with caps on hits and entries. Each file is capped, the total attached content is capped, and unknown references are reported. Directory listings for globs are limited, and content is truncated with a marker when caps are exceeded.

## Commands

Commands are invoked with a leading slash. The full set is listed by the help command: endpoint connection and key replacement, model selection with an effort choice, workspace and memory inspection, difference display and change discard, tool listing, approval-mode selection, cost and cache display, conversation summarization, clearing, resetting, session resume, goal and todo management, workflow creation and launching, skill discovery and attachment, multi-line pasting, step-limit and verbosity toggles, and exit. The console banner displays model, endpoint, workspace, version-control status, approval mode, tool count, and instruction-file information.

## Goals, Todos, Skills, and Workflows

The session supports goal injection where a standing objective and optional notes are rebuilt into the effective system prompt on every turn, with a cap that is re-applied after per-turn additions and with a check that notices when the agent reports the goal as complete. A session todo checklist is injected the same way, and agent-emitted completion lines are applied live during streaming and de-duplicated at turn end.

Skill attachment appends procedure text to the prompt for the current turn, with automatic routing that can attach a matching skill without being asked, subject to confidence and margin thresholds and to flags that disable automatic attachment or bundle launching. Bundles launch as ordered steps, attaching each skill in turn and restoring the previous attachment afterward. Workflows store named sequences of prompts and launch as ordered steps through the same session handler.

Auto-compaction summarizes the conversation via the language model client when the token threshold is exceeded, replacing the body with a summary while preserving the system prompt.

## Persistence

The session autosaves after each turn once the conversation is substantial, so a closing window can be resumed from the transcript list. Session save and load enforce size caps on file and payload, and path validation checks the resolved absolute form against allowed directories for both absolute and relative inputs.
