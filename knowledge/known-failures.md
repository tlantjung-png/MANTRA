# MANTRA known-failure registry

Recurring failure classes injected into the prompt. Add an entry when a class is fixed.

Format:

## KF-N | short title
- symptom: what went wrong, observable
- rule: the concrete behavior the agent must follow
- date: YYYY-MM-DD

## KF-1 | editing a file that was never read
- symptom: edit without a prior read corrupted the file.
- rule: never call edit_file on a path you have not read in this session; the tool enforces this and will reject the edit.
- date: 2026-08-26

## KF-2 | entrypoint lost during bulk deletion
- symptom: removing demo code also removed the `if __name__ == "__main__"` guard, so the CLI exited silently with code 0.
- rule: after any deletion-based refactor, verify the module still has its intended entrypoint and run it once.
- date: 2026-08-26

## KF-3 | nested quotes in inline interpreter one-liners
- symptom: python -c "..." containing embedded single/double quotes or trailing-backslash raw strings fails to parse on Windows shells (unterminated string literal), burning a turn.
- rule: for any command needing more than trivial quoting, write a temporary script file and execute that instead of fighting shell escaping.
- date: 2026-08-26

## KF-4 | search_replace anchor not unique after batch edits
- symptom: nine failed search_replace calls in one session: eight rejected as "found multiple times" and one "not found" when a prior edit to the same file shifted a neighbouring anchor. All followed batch comment/docstring edits where the same short line (a repeated assignment, an identical comment block, a duplicated fixture line) appears more than once in a file.
- rule: before editing with search_replace, treat short or duplicated anchor text as non-unique. Either include enough surrounding context to make the anchor unique, or deliberately pass replace_all when every occurrence should change. After any edit to a file, re-read the region before the next edit in that file when anchors are similar; a stale anchor fails with "not found".
- date: 2026-09-05
