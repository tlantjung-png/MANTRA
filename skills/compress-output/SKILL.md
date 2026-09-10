---
name: compress-output
description: Use the console's bounded tools (read_file with caps, list_dir, search_code, the paged diff renderer) instead of raw file dumps, ls, grep, or git diff when the result would be large. Compression at the source keeps the context window small and the model focused on the relevant signal.
version: 1.0.0
user-invocable: true
---

# Compress Output

## Use When

Use when inspecting a directory, searching for matches, or reviewing a diff, and
the raw command output would be large or noisy. Bare listing, searching, and full
diff output can dump thousands of tokens that drown the relevant signal. The
console's bounded tools return only what matters.

## Tools

- `read_file` reads a file with a line/byte cap and reports when it truncated,
  with a `Note:` recovery offset; it also enforces the read-before-edit ledger,
  so an edit after a capped read is safe.
- `list_dir` returns one line per directory entry, capped and sorted, instead of a
  raw `ls`.
- `search_code` searches text files for a literal substring and returns matching lines
  with file path and line number, capped, instead of raw `grep`.
  Use it for code search, symbol lookups, and quick text presence checks across the
  workspace.
- The console renders `git diff` output and edit diffs as paged old/new panes
  (capped, with "N more diff lines" notes) instead of dumping a full patch.

## Procedure

1. Decide whether the raw output would be large. If the file, directory, search,
   or diff is big or untrusted, use a bounded tool.
2. For a large or minified file, call `read_file` (it caps lines/bytes and reports
   truncation); follow its resume note instead of widening the first read.
3. Run the listing, search, or diff through the bounded tool appropriate to the
   question.
4. If the tool truncates and reports hidden counts, narrow the query or read the
   specific file rather than widening the dump.
5. When you need a detail the tool omitted, read the exact file or hunk directly
   (or page through the console's queued output with an empty Enter).

## Verification

Prefer the bounded tool's capped output over the raw dump. If the raw output
would be under a dozen lines, the raw command is fine; the bounded tools are for
when it would be large.

## Boundaries

Do not pipe bounded output through another broad command that re-expands it. The
tools exist to keep output bounded; chaining them into a full re-dump defeats the
purpose.