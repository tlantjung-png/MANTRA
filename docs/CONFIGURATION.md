# Configuration

## Loader Behaviour

Configuration is a structured document. The primary format is supported natively and the alternative format requires an optional parsing library. The raw file is capped in size before parsing, and the parsed document is merged deeply into defaults so partial documents inherit values for omitted sections.

Required sections must be objects, at least one tool must be listed, and every tool entry must be a non-empty name. Unknown keys at any level are rejected so typos surface early. Approval mode and reasoning effort are checked against allowed sets. The context section, when present, must be an object with integer limits that meet minima for message count and character count. Value types are validated for the language-model, sandbox, evaluator, and logging sections so a wrong type is reported at load time rather than inside a constructor. Errors are reported as configuration errors with descriptive messages.

When a component section changes its discriminator, for example the evaluator type or the sandbox provider, the merged section starts from the operator's values rather than the defaults so the previous component's keys cannot leak into the new constructor.

## Defaults

Defaults provide a base system prompt, a maximum step count, language-model settings, a sandbox choice, a tool list, an empty set of external servers, an evaluator command, a logging sink, an approval mode, context limits with a rolling digest switch and ceiling, an observation-reshaping section, an automatic summarization threshold, verbosity, and skill-routing preferences.

The observation section carries a switch and a per-observation character ceiling. When reshaping is on, tool output is condensed before it enters the model's context, while the operator's screen and failure capture keep the raw output. A ceiling of zero turns reshaping off.

The context digest switch makes eviction lossless. When the budget drops the oldest turn, the turn is detached rather than discarded, summarized between steps, and carried forward in a rolling digest that rides between the pinned prefix and the live history. Turning it off restores the older lossy behaviour.

The language-model defaults include a provider, model, credential lookup name, no reasoning effort, and an output-token ceiling applied in the client unless the launch configuration raises it. The client timeout defaults to the value set in the client and can be tuned through the configuration. When a silent reasoning stretch outlasts the read timeout after partial output, the request is retried with a larger budget and a nudge instead of ending the turn, while a hard mid-stream drop still fails the turn.

The default tool list covers file reading, writing, and editing, directory listing, command execution, shell output reading, background-task termination, text search, filename search, document extraction, version-control diff, and web retrieval. Web search is not in the default list: its section carries an empty endpoint address, a credential lookup name, a result count of five, and a timeout. Supplying an endpoint and naming the tool in the tool list enables it; unknown keys and wrong value types in that section are rejected at load time.

The default approval mode prompts before destructive actions. A permissive mode is available for trusted or disposable workspaces and requires an explicit choice. The console's starting mode comes from the configuration document chosen at launch. The approval command records the operator's choice in the user-wide settings document, and a later console start applies that stored choice over the configuration document. An explicit launch flag outranks both.

The external-server section maps a name to a server definition containing the launch command, an optional working directory, optional environment additions, an enabled flag, and an optional timeout. A missing, empty, or wrongly typed command is rejected, as is any unknown key inside a definition, so a malformed server is reported at load time rather than as a bare failure to launch a child process. The console can flip a server's enabled flag at runtime; the change is written back to the configuration file in place and takes effect for the running session.

## Component Routing

The language-model section is forwarded to the registry, which maps the provider name to a concrete client and validates unknown names and keys. Sandbox, evaluator, and logging sections are handled similarly, with only relevant keys forwarded and unknown names reported.

Tool construction validates names at startup, normalizes aliases, shares one edit ledger across file tools to enforce read-before-edit and partial-view tracking, and removes tools that resolve to the same implementation.

## User-Wide Settings

Endpoint and model selections live in a separate hand-editable document in the home directory. It enumerates endpoints with a base address, credential lookup name, known models, and an optional note, plus the active endpoint, model, reasoning effort, and output-token choice, skill-routing preferences, and console preferences. Skill auto-attach lives here rather than in the run configuration because it is a one-time operator preference and the run configuration is never written back to by the system. The file is written atomically through a uniquely named temporary file with restricted permissions and verified stale-lock handling. When it cannot be parsed it is quarantined under a collision-free backup name before anything replaces it, so repeated failures never overwrite the previous backup or the evidence. Endpoint names are normalized to lower case on load and on lookup, so a hand-edited mixed-case name remains reachable.

A parallel credential store holds secret values with restricted permissions and is never written to the main settings file. It is keyed by lookup name, supports masked display, and records a schema version. Secrets resolve first from the process environment and then from the store. A one-time warning is emitted on platforms where permission bits are not enforced and when a credential would cross an unencrypted connection. An environment variable relocates the store for testing. Reads are cached against the file's modification time, size, and a process-local generation counter so redaction and resolution do not re-parse the file on every call.

Stored credentials are plaintext protected only by file permissions, so any process running as the same user, or any backup of the home directory, can read them. Supplying secrets through the process environment is preferable where possible. Command text and output are redacted for known secret formats before being written to persistent logs, including full-output and background-task logs. Redaction recognises known shapes rather than every secret, so those logs remain sensitive.

Workflow definitions live in another document that stores named prompt sequences with a version, creation timestamp, and steps, subject to limits on step count and step length. Writes are atomic with verified locking, and corrupt content and shape mismatches are both quarantined. Only workflows with at least one step are listed or retrievable.

Session transcripts are stored as one file per session under a dedicated directory with an environment-variable override. Each transcript records a version, name, timestamp, workspace, model, summary, totals, checklist, and full message list, with per-message caps, atomic writes, restricted permissions, and legacy-name fallback.
