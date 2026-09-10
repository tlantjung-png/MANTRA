---
name: search-workflow
description: How agents should use this repo's search, extraction, and web tools like a unified grep/markitdown-style workflow: literal-first search, bounded document extraction, tree navigation, and web fetch as reading primitives.
---

# Search / extraction workflow skill

This repo gives agents a small set of bounded reading primitives. The habit that works best is:

1. Find the file or files first.
2. Read the right slice of them second.
3. Extract structure only when the raw text would waste context.

## Tools and when to reach for them

- `search_code` — literal substring search across workspace text files. Use it for presence checks, symbol lookups, and finding where something is mentioned. It is not a regex engine; keep queries literal and narrow.
- `find_file` — filename substring search. Use it when you know the file name or a fragment of it. It does not accept glob metacharacters.
- `extract_document` — read a single file and return a bounded plain-text representation for common structured formats (HTML, JSON, XML, CSV, markdown, ini/toml-ish, and similar). Use it when the file is structured and the model only needs the readable shape, not the raw bytes. It reports a note for formats it cannot render, and it falls back to a short raw snippet when a structured file is malformed.
- `query_tree` — small tree-pattern query for workspace structure. Use it to find files by path/glob or to inspect a directory's immediate children. The brace shape syntax is deliberately tiny; it is a navigation aid, not a full code-query engine. It supports `{dir}`, `{dir/*}`, `{dir/*.ext}`, `{dir/**/pattern}`, and one-level descent into nested directories.
- `web_fetch` — fetch an HTTP(S) URL and return readable text. Use it to read docs, release notes, issue threads, and API references when the information lives outside the workspace.

## A grep/markitdown-style loop

A good agent flow looks like this:

- **Locate first.** Start with `search_code` or `find_file`. Prefer the narrowest query that should uniquely identify the target. If the result is noisy, narrow by filename, directory, or a longer literal fragment before expanding.
- **Read second.** Once you know the file, read only what you need. Use `read_file` with an offset/limit window for large files, or `extract_document` when the file is structured and the readable shape is enough.
- **Extract only when it saves context.** `extract_document` is useful when the raw file would waste turns on tags, braces, or markup the model does not need. It is not a replacement for `read_file`; it is a bounded normalization for specific formats.
- **Fetch external docs when needed.** If the answer is likely in upstream docs, release notes, or an issue thread, use `web_fetch` before guessing. Prefer the authoritative source over inferring from memory.

## What each tool is good for

- `search_code` is best for quick, literal presence questions:
  - "Where is this string used?"
  - "Is this symbol referenced anywhere?"
  - "Which files mention this error text?"
- `find_file` is best for locating files by name:
  - "Where is the config file?"
  - "Which test file talks about this feature?"
- `extract_document` is best for structured reading:
  - HTML pages or fragments
  - JSON blobs
  - XML/SVG
  - CSV exports
  - Markdown files where fences and links are just noise for the question at hand
  - ini/toml-ish config files
- `query_tree` is best for structural navigation:
  - "What is in this directory?"
  - "Which files match this glob under this folder?"
  - "Does this directory exist and what does it contain?"
  - "Which files match a recursive glob under this folder?"
- `web_fetch` is best for external reading:
  - Documentation pages
  - Release notes
  - Issue or discussion threads
  - API references

## What each tool is not

- `search_code` is not full-text search over everything. It skips binary/archive extensions and large files, and it is bounded by result and scan ceilings.
- `find_file` is not a glob or regex tool. It is substring match on filenames only.
- `extract_document` is not a universal document reader. It reports a note for formats it does not handle instead of pretending to parse them. When parsing fails, it may return a short raw snippet rather than a pretty-printed form.
- `query_tree` is not a code-query engine. It cannot express deep structural relationships; it is a file-tree navigator. Deep nesting is reached one brace-level at a time.
- `web_fetch` is not a browser. Tables and page layout are not preserved; it is for readable prose and structured text, not for visual rendering.

## Good habits

- Prefer literal queries. Wildcards and regex-style guesses are usually a sign the query should be narrowed or split.
- Start with the smallest scope that could answer the question. A directory-scoped `query_tree` or a filename-based `find_file` is often better than a full workspace scan.
- Use `extract_document` when the raw file would be mostly markup/braces/rows and you only need the visible content.
- Use `read_file` windows when the file is large or when you need exact line context.
- Use `web_fetch` when the information is external and likely authoritative.
- When results are noisy, do not read everything. Narrow the query, change the tool, or scope the directory before expanding.

## Failure modes to avoid

- Do not treat `search_code` as if it were a structural code analyzer. It finds text, not semantics.
- Do not assume `extract_document` parsed a file correctly just because it returned text. If the format matters, verify with the right tool or with `read_file`.
- Do not use `query_tree` as if it were a dependency or call-graph analyzer. It answers file-tree questions.
- Do not fetch a page and assume layout is preserved. If the layout matters, that is a different problem from readable text extraction.
- Do not retry the same broad search multiple times with only cosmetic query changes. Narrow scope first.

## Typical combined patterns

- Find a file, then extract its readable shape:
  - `find_file` -> `extract_document` for a structured file
  - `search_code` -> `read_file` window for exact line context
- Explore a directory before reading:
  - `query_tree` to see what is there, then `read_file` or `extract_document` on the relevant entry
- Read external docs alongside workspace inspection:
  - `web_fetch` the doc page, then `search_code` the workspace for the relevant symbol or config key
- Narrow a noisy codebase search:
  - `search_code` broad literal -> `find_file` for the right file name pattern -> `query_tree` to confirm location -> `read_file` or `extract_document` for the relevant slice

## Bounds you should expect

All of these tools are bounded by design. Expect ceilings on results, scanned files, file size, and output length. When a tool says it hit a ceiling, narrow the query, shrink the scope, or split the task instead of asking the same tool to do more.

## extract_document and query_tree in the loop

- Use `extract_document` when a structured file is the target and you only need its readable shape. Typical flow: `find_file` or `search_code` first, then `extract_document` for the specific file.
- Use `query_tree` when the question is about directory structure or file locations rather than file contents. Typical flow: `query_tree` to discover layout, then `read_file` or `extract_document` on the relevant entry.
- Combine `extract_document` and `query_tree` when a structured document lives inside a nested layout you do not know yet: `query_tree` to locate it, `extract_document` to read it.
- Do not expect either tool to do the other's job. `extract_document` is for file contents; `query_tree` is for file-tree structure.

## Shape syntax cheat sheet

`query_tree` accepts a small brace-shaped query:

- `{dir}` — immediate children of a directory
- `{dir/*}` — same, as a glob shorthand
- `{dir/*.ext}` — files under `dir` matching a glob
- `{dir/**/pattern}` — recursive glob under `dir`
- `{dir/sub}` — descend one level into a nested directory and list its children

If a brace body contains a nested directory, the tool descends once and keeps parsing. If it contains a glob, it falls back to glob matching rooted at that directory.
