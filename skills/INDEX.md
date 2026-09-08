# Skill Catalog

| Skill | Function |
|---|---|
| `adopt-workspace` | Adopt a new project into MANTRA - seed its workspace .mantra state directory with a memory file and known-failure registry so every session has somewhere to read and write from day one. |
| `analytics` | Debug console and agent-loop issues by reducing the repo's JSONL event logs and per-session signals into one correlated, prioritized report of bugs, failures, anomalies, per-tool metrics, and token usage. Use to find where a feature is failing or to audit health. |
| `api-endpoint` | Add or review an API endpoint or webhook using the existing contract, validation, authentication, error, and test patterns. |
| `changelog` | Write user-facing release notes from verified repository history, clearly separating current, planned, changed, fixed, and removed behavior. |
| `check-work` | Independently verify completed work by reconstructing the request, inspecting the current state, selecting the narrowest relevant evidence, running the available checks, and reducing all check results into one structured verdict. |
| `code-review` | Perform a strict maintainability and structural-quality review focused on correctness risks, lifecycle, boundaries, duplication, code growth, and the model-visible surface, reporting evidence-backed findings. |
| `commit-message` | Derive a conventional commit message from the actual staged or working-tree change while keeping unrelated concerns separate. |
| `compress-output` | Use the console's bounded tools (read_file with caps, list_dir, search_code, the paged diff renderer) instead of raw file dumps, ls, grep, or git diff when the result would be large. Compression at the source keeps the context window small and the model focused on the relevant signal. |
| `context-handoff` | Manage the context budget across compaction - watch capacity signals, offload volatile state to durable files (workspace memory, the standing goal), write a continuation brief before compacting, and rebuild intent afterwards. |
| `copy` | Write specific, persuasive marketing or branded content in a truthful voice without invented claims, guarantees, or generic filler. |
| `copywriting` | Create persuasive copy that sounds natural, audience-aware, specific, and credible while preserving factual accuracy and avoiding formulaic marketing language. |
| `create-skill` | Create and register a new workflow skill with valid metadata, a focused procedure, catalog and routing entries, and verification coverage. |
| `create-workflow` | Create, register, and launch a MANTRA workflow (an ordered prompt sequence in ~/.mantra/workflows.json) using the /workflow commands. |
| `debug` | Reproduce a bug or failure, identify its root cause from the actual path, make the smallest safe correction, and prove the regression is gone. |
| `dependency-upgrade` | Upgrade one dependency while researching compatibility, handling breaking changes, preserving scope, and proving the result with project checks. |
| `deploy-parity` | Copy changed assets (skills, rules, scripts, knowledge) to a target directory or backup backup-first, with SHA-256 parity proof and a recorded rollback path. |
| `design` | Design accessible responsive web interfaces with distinct visual directions, a coherent system, and checked interaction states rather than generic layouts. |
| `diagnostics` | Detect and run the project's available static analysis, parsers, linters, type checks, and compilers, reduce cross-tool findings into one prioritized report, then fix or report every supported diagnostic. |
| `diagram` | Produce a truthful self-contained architecture or data-flow diagram after reading the actual system and its ownership boundaries. |
| `diff-review` | Inspect a working-tree diff hunk by hunk and selectively accept or reject changes using the console's /diff and file tools without mutating unread content. |
| `eval` | Measure agent-loop quality with the repo's test suite and session logs - validate gates, classify blast radius, and report cost per accepted change. |
| `evolve` | Run a closed task-aware harness-evolution loop that diagnoses a task against pass/fail success criteria, proposes a harness change and model lane, and locks regressions into the known-failure registry. Use when a task repeatedly fails, when a harness needs tuning, or when a failure should become a durable regression. |
| `excel` | Build auditable spreadsheets, financial models, trackers, or data workbooks with explicit inputs, formulas, tie-outs, and usage guidance. |
| `exec-mode` | Recommend whether a task should run in a regular session, as a MANTRA workflow, or as a goal, using a deterministic signal table, and log the decision to the workspace memory. |
| `help` | Explain current MANTRA setup, configuration, authentication, skills, commands, and troubleshooting from the actual repository and ~/.mantra state. |
| `humanize` | Rewrite stiff, robotic, repetitive, or overly polished prose into natural human-sounding writing while preserving meaning, facts, terminology, and the writer's intended voice. |
| `imagine` | Plan image generation, image editing, short video workflows, and UI demonstration GIFs with accurate prompts, references, consistency checks, and safe handling of likenesses. |
| `improve-coverage` | Add meaningful tests for existing behavior by prioritizing high-risk boundaries, regressions, failures, and untested contracts rather than chasing a percentage. |
| `instruction-audit` | Audit always-loaded instruction assets - the base system prompt, workspace memory, AGENTS.md instructions, the known-failure registry, and command rules - for token weight, staleness, contradiction, and attention dilution. |
| `knowledge-gap` | Find documentation gaps between shipped code and docs (missing/stale/hard-to-find/needs-triage) with evidence. |
| `known-failures` | Maintain and re-probe the known-failure registry (knowledge/known-failures.md, injected into the session prompt) so previously fixed incident classes cannot silently return in sibling paths. |
| `maintenance` | Run the periodic health pass - test suite, git state, known-failure probing, session log review - and write one dated report. |
| `mine-sessions` | Mine recent session traces and console logs, reduce cross-session patterns into weighted candidates, and route durable knowledge, repeated workflows, failure trends, and operational candidates without copying secrets. |
| `performance-audit` | Find measurable performance bottlenecks, blocking work, repeated scans, unbounded growth, and memory or I/O waste without micro-optimizing unmeasured paths. |
| `plan` | Produce a self-contained, actionable implementation plan with architecture, exact paths, bite-sized test-first tasks, risks, and final verification without implementing it. |
| `pr` | Manage the full pull-request lifecycle through the repository's approved GitHub integration, including review comments, CI, synchronization, stacked PRs, and explicit merge approval. |
| `recall` | Retrieve targeted knowledge from workspace memory, the known-failure registry, session records, and the skill catalog. Use at task start, before decisions, after long sessions, or when a subagent left durable context. |
| `refine` | Convert completed work into durable memory or skill improvements through an evidence-backed, reversible checkpoint and an honest nothing-to-save outcome. |
| `security-audit` | Perform an evidence-backed vulnerability review covering secrets, injection, authorization, unsafe defaults, deserialization, sensitive output, and visible dependency risk. |
| `simplify` | Reduce complexity and duplication without changing observable behavior by classifying consumers, proving each candidate with evidence, and making a minimal verified change. |
| `slides` | Build a coherent presentation with one message per slide, speaker notes, consistent visual hierarchy, and a final audience-oriented inspection. |
| `spike` | Test an uncertain idea with a disposable, isolated feasibility experiment and record evidence, limitations, comparisons, and a clear verdict. |
| `subagent-dev` | Implement a multi-step plan one task at a time, with review and verification before advancing, keeping each task's changes isolated until accepted. |
| `swarm` | Orchestrate independent subagents for task fan-out, decision panels, or comprehensive category-and-area audits, then reduce and reconcile every result into one verified deliverable. |
| `tdd` | Develop a feature or regression test-first by proving the expected failure, making the smallest implementation, refactoring safely, and running broader relevant checks. |
| `trading-safe` | Review live-money, order, position, execution, and risk paths with a fail-closed standard, proof requirements, numerical checks, and explicit operator decisions. |
| `ui-verify` | Verify web and UI changes end to end in the browser - exercise the changed flow like a real user, hunt cross-page regressions, check edge states, and confirm desktop and mobile viewports. |
| `unattended` | Run bounded unattended work headless or scheduled with provider fallback limits, resumable state, and an explicit escalation contract. |
| `update-memory` | Maintain a concise dated project-state ledger (workspace .mantra/memory.md) with goals, constraints, progress, decisions, next steps, critical context, and relevant files, pruning entries by future decision value. |
| `update-taste` | Maintain concise durable coding and documentation conventions in the repo (knowledge/known-failures.md and AGENTS.md) without inventing one-off preferences. |
| `vault` | Verify the append-only integrity history of project state (the .mantra/vault chain) at checkpoints, after refinements, or before declaring done, where the target installation ships a verifier. |
| `workflow` | Route an ambiguous, cross-category, or skill-selection request to the correct workflow family without executing the task itself. |
| `write-docs` | Write precise technical documentation for current code, APIs, workflows, or features by tracing behavior, applying placement and detail hierarchy, and cross-checking every claim. |
| `write-migration` | Design a safe reversible database migration by following existing conventions and assessing data loss, defaults, indexes, locks, downtime, and rollback. |
