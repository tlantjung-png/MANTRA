# MANTRA

MANTRA is a modular harness that delegates coding and analysis tasks to an
external language model. You describe what you want in plain language; the
system explores your workspace, edits files through approved tools, runs
verification steps, and reports back. It works in two modes: an interactive
console for day-to-day work, and a headless runner that executes a single
task end to end and exits with a pass/fail status.

## Prerequisites

- **Python 3.10 or newer.** Check with `python --version`.
- **Git.** Used for workspace setup and for inspecting changes (`/diff`, `/undo`).
- **Network access to a language-model endpoint** plus an API key for it
  (any OpenAI-compatible endpoint works).
- **A container runtime** is optional. It is only needed for the container
  sandbox, which confines untrusted work. Trusted daily work uses the host
  sandbox and needs nothing extra.

There are no mandatory Python dependencies: core operation relies on the
standard library only. An optional parsing library is required only when
configuration documents are written in YAML instead of JSON.

## Installation

Get the source first:

```sh
git clone https://github.com/tlantjung-png/MANTRA.git
cd MANTRA
```

Then pick one of the two ways to run it.

**Option A — install (recommended for daily use).**
This exposes the `mantra` and `mantra-headless` commands everywhere:

```sh
pip install .
mantra
```

**Option B — run without installing (Windows).**
Launch the console straight from the repository folder:

```bat
mantra.cmd
```

The launcher probes whether the package resolves from this repository and
prepends the source tree to the module search path when it does not, so
both options behave identically. The probe also rejects a foreign `core`
package from site-packages, so a different package cannot hijack the console.

## First Run

On launch the console shows the active model, endpoint, workspace, and
version-control status, and it creates the workspace directory if needed
(default: `workspace/` inside the project).

If no endpoint is configured yet, the console walks you through adding one:
supply a base URL and an API key when prompted. You can do the same at any
time with the `/model` command:

```text
/model                          open the provider and model menu
/model <url> [key] [model]      add a provider, then pick a model
/model list                     show saved providers
/model key [name]               replace a stored key
```

Local inference servers on the loopback interface need no key at all; the
setup flow tells you when none is required.

Keys are never written to the run configuration. They resolve from the
environment first (e.g. `OPENAI_API_KEY`) and then from a restricted
credential store in your home directory (`~/.mantra/credentials.json`,
owner-only permissions). Only masked forms are ever displayed. If a key is
missing, the console warns you up front instead of failing mid-task with an
authentication error.

## Daily Use

Type a natural-language request at the prompt and press **Enter**. The
system explores the workspace, makes changes through approved tools, and
runs verification steps. Follow-up messages continue the same conversation
and workspace, so you can iterate ("now also handle the empty case").

Useful commands (see `/help` inside the console for the full list):

| Command     | What it does                                              |
|-------------|-----------------------------------------------------------|
| `/model`    | Add/switch providers and models, manage stored keys       |
| `/diff`     | Show uncommitted workspace changes                        |
| `/undo`     | Discard uncommitted changes (asks for confirmation)       |
| `/fix`      | Send the most recent failure back for diagnosis and a fix |
| `/sessions` | Browse, save, and resume conversations                    |
| `/goal`     | Set, show, or clear a standing objective for the session  |
| `/todo`     | Manage a session checklist                                |
| `/workflow` | Create and launch ordered prompt sequences                |
| `/skills`   | Discover and attach skill procedures                      |
| `/approve`  | Set approval mode: `default`, `auto`, `yolo`, or `plan`   |
| `/compact`  | Summarise a long conversation to free context             |
| `/memory`   | Show the workspace memory file                            |
| `/cost`     | Show token usage and cache metrics                        |
| `/verbose`  | Toggle per-tool detail output                             |
| `/clear`    | Clear the conversation (files are kept)                   |
| `/exit`     | Leave the console (autosaves first)                       |

Reference workspace files inline with `@`, e.g. `@src/main.py what does
this do`. Approval behaviour follows the selected mode: `default` prompts
for destructive actions, `auto` allows ordinary changes silently, `yolo`
allows everything (only for disposable environments), and `plan` refuses
all changes for exploration and review.

**Prompt-box tips.** Pressing Enter submits; Shift+Enter inserts a newline.
Pasted text never auto-submits — it lands in the box as editable text. You
can click to place the caret and drag with the mouse to select text, then
type to replace the selection or use Backspace/Delete to remove it.

## Headless Mode

For scripting or CI, run one task non-interactively with a configuration
document and a task document (see `examples/` for starting points):

```sh
mantra-headless --config examples/config.json --task examples/task.example.json
```

The runner provisions the workspace, executes the agent loop, grades the
result with the configured test command, writes a structured event log to
`logs/mantra-run.jsonl`, and exits `0` on pass, `1` on fail, or `2` on
configuration/task-file errors. Relative log paths are anchored to the
project root so the same configuration works from any directory.

In headless mode nobody can answer an approval prompt, so the configured
mode maps to its closest non-interactive form: `plan` refuses every
mutation, `default` and `auto` allow ordinary mutations but refuse
destructive ones, and `yolo` allows everything.

## Configuration

- **Per-run config** (`--config`): model connection, sandbox provider,
  enabled tools, evaluator command, logging, approval mode, and context
  limits. Unknown keys are rejected to catch typos early.
- **Per-user settings** (`~/.mantra/config.json`): saved endpoints, the
  active endpoint/model, and skill preferences. Hand-editable; corrupt
  files are quarantined to a backup instead of being overwritten.
- **Credentials** (`~/.mantra/credentials.json`): stored keys, readable
  only by your user account. Prefer environment variables on shared
  machines; stored credentials are plaintext protected only by file
  permissions.
- **Sessions** (`~/.mantra/sessions/`): saved conversations.
- **Workspace memory** (`<workspace>/.mantra/memory.md`): durable notes
  the system appends automatically after each turn.

## Safety Notes

- **Two sandboxes.** The host sandbox runs commands directly in the
  workspace folder — fast, but not a containment boundary: its traversal
  screen is a heuristic, and unrecognized command shapes run with the full
  authority of your user account. The container sandbox runs everything
  inside a disposable container with memory/CPU limits, reduced privileges,
  and an unprivileged user. Use the container sandbox for untrusted input
  or repositories you do not control.
- **Approvals.** The default mode asks before destructive actions. Every
  decision is written to a redacted audit log.
- **Path confinement.** All file and command tools are confined to the
  workspace; escapes via `..` or symlinks are rejected at every operation,
  parent chains are checked for symbolic links, and writes are atomic
  (staged to a uniquely named temporary file, then moved into place).
- **Secrets.** Command text and tool output are redacted for known secret
  patterns before being persisted to logs, and the host sandbox strips
  credential-shaped variables from the child environment, but treat log
  files as sensitive anyway.

See `docs/` for the full documentation set.

## Tests

Run the offline suite (no network or credentials required):

```sh
python -m pytest tests/ -q
```

It covers the agent loop, context budgeting, configuration validation,
file-tool safety, approval classification, session persistence, the
streaming parser, model discovery, the terminal input layer, and the
search, document-extraction, and tree-query tools. Live probes for
end-to-end verification with real credentials are available under
`tests/` when needed.

## Workspace Reading Tools

Beyond the file and command tools, the agent has three bounded helpers for
reading the workspace without streaming raw bytes into the conversation:

- **search_code / find_file** — literal substring search across workspace
  text files, and filename substring search. The walk honors ignore files,
  never descends into ignored or symlinked-out directories, skips files
  above 500 kilobytes and non-text extensions, and notes when result or
  scan ceilings are reached.
- **extract_document** — read a single structured file and return a
  bounded plain-text representation for common formats (HTML, JSON, XML,
  CSV, markdown, ini/toml-ish). Formats it cannot render are reported as
  a note instead of being silently parsed; malformed structured files fall
  back to a short raw snippet.
- **query_tree** — a small tree-pattern query for file-tree structure:
  literal paths, simple globs, and a tiny brace-style shape syntax
  (`{dir}`, `{dir/*}`, `{dir/*.ext}`, `{dir/**/pattern}`, one-level
  descent into nested directories). A navigation aid, not a code-query
  engine.

## Documentation Map

- `docs/Architecture.md` — layers, module interactions, trade-offs.
- `docs/Setup.md` — prerequisites, installation, local development.
- `docs/API.md` — entry points, commands, configuration, environment variables.
- `docs/Deployment.md` — build, testing, sandbox levels, runbooks.
- `docs/configuration.md` — loader behaviour and user-wide settings.
- `docs/console.md` — terminal interface and session management.
- `docs/core.md` — orchestration, context, knowledge, approvals, events.
- `docs/implementations.md` — concrete clients, sandboxes, tools, loggers.
- `docs/interfaces.md` — abstract contracts.
- `docs/ADOPTION.md` — what was kept and what was left out.

## License

MIT — see [LICENSE](LICENSE). You are free to use, modify, and distribute
this software, including for commercial purposes, as long as the copyright
and permission notices are preserved.
