# Console

## Terminal Interface

The console implements the interactive terminal interface using standard-library facilities. It styles output with terminal escape sequences, can disable styling for non-terminal output, and enables escape-sequence processing on the host console where needed. It forces UTF-8 output so drawing characters and wide glyphs render on narrow codepages.

The console owns one terminal surface. Each frame is composed as a full grid of character and style per cell, compared with the previously shown grid, and only changed cells are written. A reader thread turns raw terminal input into key, mouse, paste, and resize events. One presenter loop drains that queue before drawing, batches streamed model output, throttles frames, and arms an animation tick only while something is animating, such as the spinner or drag auto-scrolling. Because no background thread paints at a stale absolute position, resizing is a full reflow and interleaved output cannot tear a frame.

The display uses an alternate screen with a top information bar showing workspace, model, approval mode, and cache rate; a scrollable transcript viewport; a border row carrying the spinner, a themed word for the current activity, and a live token counter; and a prompt box at the bottom. Text selection is drawn by the application: dragging over the transcript selects, releasing copies, the system clipboard is tried first, and an escape-sequence fallback serves remote sessions. Wheel and page keys scroll. While a turn runs, the operator can keep typing. A prompt submitted during a turn is injected into the running agent loop and appended to the model's context at the next step, so the operator does not have to stop the current turn to be heard. If the turn ends before that step happens, the prompt is moved to the turn queue and started as the next turn. Several such prompts keep their submission order. The busy check, queue write, and turn-end handoff share the application lock, so a prompt submitted exactly as a turn ends is either injected, queued, or started, never lost. Menus, pickers, secret entry, and approval prompts are overlay cards rendered into the frame. The prompt box has closed corners and a right wall so long input stays contained, and its caret advances in display columns so a wide character before it is measured correctly.

On byte-stream platforms the input decoder is explicit about every byte. A lone escape becomes the escape action, the delete byte maps to backspace, control ranges are dropped instead of inserted, alternate chords arrive as modifier-carrying keys, arrow-key modifier parameters decode into key modifiers, and a multi-byte character split across reads is reassembled. Bracketed pastes are bounded in memory and delivered whole, and an unterminated paste is cut off at the cap rather than buffered without limit. The startup card is a centered adaptive block showing the product name, tagline, and version. It is removed from the viewport on the first turn rather than painted over, and resizing while it is visible re-renders it centered for the new size.

## Session Model

The console session owns the workspace, sandbox, history manager, system prompt, tools, client, approval policy, and event bus. It tracks totals for tokens, turns, tool errors, and cache hits, per-turn cache metrics, and the most recent failure for the fix flow. The sandbox is the host-directory provider over the persistent workspace, and an empty repository is initialized if the workspace lacks one.

The session manages a streaming renderer that buffers fragments until line boundaries so formatting can be applied while code-fence state is tracked, and it strips terminal escape sequences from model output. A spinner runs while the model works and yields to real output. The viewport throttles rapid fragments to avoid flicker and forces a flush at stream completion so the final fragment is never delayed. A live approximate token counter updates on a throttle.

While a turn streams, a turn-scoped reader keeps native mouse selection working on the host and buffers non-scroll keys for the next prompt. Fragments received during a native selection are deferred and flushed when the drag ends. Tool observations render as boxes measured in viewport rows whose overflow is queued for a pager that advances a screenful at a time; the pager queue can be saved with the session so an interrupted paging session resumes where it left off. Structured unified diffs render as stacked before and after panes rather than a plus and minus stream. File edits show a before and after diff against a snapshot taken when the tool call arrived, and brand-new writes show a syntax preview.

## Closing Summary

Every turn ends with a plain-language summary printed after the usage footer, so the last visible element is an explanation rather than a tool-output box or a code snippet. It reports the stop reason, the number of steps and changed files, the verification status, and a next step. The headless runner prints an equivalent closing line after its verdict, and replaying a saved session also ends with a plain statement instead of a raw tool message. The model is instructed through the system prompt to end its own final answer with the same structure; the deterministic closing line guarantees it even when a turn ends early or the model omits it.

## Line Editor

The line editor reads single keys and provides completion for commands and workspace paths, with navigation, deletion, and history handling. A popup can be dismissed or re-invoked with a dedicated key, supports filtering as the operator types, selection with arrow keys, a hint area, and a count hint when the terminal is too small to render it. Multi-line input renders as an upward-growing paste box with a size chip. Bracketed paste is assembled with an idle timeout, and mouse drag or click on a transcript line copies the selection to the clipboard with a status flash. When input or output is not a terminal, the editor falls back to plain line input.

The completion popup for slash commands and references follows the selection; its overflow row pages by whole windows, by mouse click, or by running the selection past the edge. Mouse capture happens only while the popup is open, so native text selection works the rest of the time. Dismissing with the escape key lasts until the text changes, so a stray escape cannot silence suggestions for the rest of the prompt, and the popup also works inside the multi-line box. While a turn runs, escape interrupts it exactly like a single interrupt keypress.

## File References

References are expanded by resolving tokens relative to the workspace, rejecting escapes with resolved-path checks, and expanding patterns with caps on hits and entries. Each file is capped, the total attached content is capped, and unknown references are reported. Directory listings for patterns are limited, and content is truncated with a marker when caps are exceeded.

## Commands

Commands are invoked with a leading slash, and the help command lists the full set. The surface covers endpoint connection and credential replacement, model selection with an effort choice, workspace inspection, change display and discard, approval-mode selection, usage and cache display, conversation summarization, clearing, session resumption, skill discovery and attachment with bundle launching, external-server inspection, and exit. Each action has one name: the surface keeps only commands that change state the operator cannot otherwise change, answer a question the interface does not already answer, guard safety, or own a unique capability. A command that receives an unused argument says so rather than discarding it. The banner displays model, endpoint, workspace, version-control status, approval mode, tool count, and instruction-file information.

## Todos and Skills

The session keeps a checklist that the agent grows and drains from inside the conversation. The checklist is rebuilt into the effective system prompt every turn. The agent reports new work and finished items in its reply, and those reports are applied live as the reply streams: the raw protocol line never reaches the screen, only a quiet checkbox note does, and reports are de-duplicated at turn end. The checklist travels with the saved conversation so a resumed session keeps its remaining work. It is work state rather than conversation, so clearing the conversation keeps it.

The bundled procedures include three for web work: one produces a short sourced brief from fetched pages, one compares a page against a saved snapshot to report meaningful changes, and one guides integrating a web data endpoint into project code. They rely on the ordinary tools and do not add dependencies.

Skill attachment appends procedure text to the prompt for the current turn. Automatic routing can attach a matching skill without being asked, subject to confidence and margin thresholds and to flags that disable automatic attachment or bundle launching. Automatically attached skills are detached when their turn ends. Bundles launch as ordered steps, attaching each skill in turn and restoring the previous attachment afterward; they are the one sequencing primitive.

Automatic compaction summarizes the conversation through the language-model client when the token threshold is exceeded, replacing the body with a summary while preserving the system prompt.

## Persistence

The session autosaves after each substantial turn so a closed window can be resumed from the transcript list. Save and load enforce size caps on files and payloads, and path validation compares the resolved absolute form against allowed directories for both absolute and relative inputs, case-insensitively where path case does not distinguish files.
