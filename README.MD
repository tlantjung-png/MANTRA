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
- **A container runtime** (e.g. Docker) is optional. It is only needed for
  the container sandbox, which confines untrusted work. Trusted daily work
  uses the host sandbox and needs nothing extra.

There are no mandatory Python dependencies: core operation relies on the
standard library only.

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

The launcher detects whether the package is installed and falls back to the
source tree automatically, so both options behave identically.

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

Example — add a provider and pick a model in one go:

```text
/model https://api.openai.com/v1 sk-... gpt-4o
```

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
| `/approve`  | Set approval mode: `default`, `auto`, `yolo`, or `plan`   |
| `/compact`  | Summarise a long conversation to free context             |
| `/memory`   | Show the workspace memory file                            |
| `/cost`     | Show token usage                                          |
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

## Configuration

- **Per-run config** (`--config`): model connection, sandbox provider,
  enabled tools, evaluator command, logging, approval mode, and context
  limits. Unknown keys are rejected to catch typos early.
- **Per-user settings** (`~/.mantra/config.json`): saved endpoints, the
  active endpoint/model, and skill preferences. Hand-editable; corrupt
  files are quarantined to a backup instead of being overwritten.
- **Credentials** (`~/.mantra/credentials.json`): stored keys, readable
  only by your user account. Prefer environment variables on shared
  machines.
- **Sessions** (`~/.mantra/sessions/`): saved conversations.
- **Workspace memory** (`<workspace>/.mantra/memory.md`): durable notes
  the system appends automatically after each turn.

## Safety Notes

- **Two sandboxes.** The host sandbox runs commands directly in the
  workspace folder — fast, but not a containment boundary. The container
  sandbox runs everything inside a disposable container with memory/CPU
  limits and reduced privileges. Use the container sandbox for untrusted
  input or repositories you do not control.
- **Approvals.** The default mode asks before destructive actions. Every
  decision is written to a redacted audit log.
- **Path confinement.** All file and command tools are confined to the
  workspace; escapes via `..` or symlinks are rejected, and writes are
  atomic (staged to a temporary file, then moved into place).
- **Secrets.** Command text and tool output are redacted for known secret
  patterns before being persisted to logs, but treat log files as
  sensitive anyway.

See `docs/ARCHITECTURE.md` and `docs/DEPLOYMENT.md` for the full details.

## Tests

Run the offline suite (no network or credentials required):

```sh
python -m pytest tests/ -q
```

It covers the agent loop, context budgeting, configuration validation,
file-tool safety, approval classification, session persistence, the
streaming parser, model discovery, and the terminal input layer. Live
probes for end-to-end verification with real credentials are available
under `tests/` when needed.

## Documentation Map

- `docs/ARCHITECTURE.md` — layers, module interactions, trade-offs.
- `docs/SETUP.md` — prerequisites, installation, local development.
- `docs/API.md` — entry points, commands, configuration, environment variables.
- `docs/DEPLOYMENT.md` — build, testing, sandbox levels, runbooks.
- `docs/CONFIGURATION.md` — loader behaviour and user-wide settings.
- `docs/CONSOLE.md` — terminal interface and session management.
- `docs/CORE.md` — orchestration, context, knowledge, approvals, events.
- `docs/IMPLEMENTATIONS.md` — concrete clients, sandboxes, tools, loggers.
- `docs/INTERFACES.md` — abstract contracts.
- `docs/ADOPTION.md` — what was kept and what was left out.

## License

MIT — see [LICENSE](LICENSE). You are free to use, modify, and distribute
this software, including for commercial purposes, as long as the copyright
and permission notices are preserved.
