"""Shared markup-to-text extraction.

One implementation of the visible-text reader, used by the web-fetch tool and
the document-extraction tool. It drops the contents of tags that never reach
the screen and turns block-level tags into newlines so paragraphs do not run
together.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

# Tags whose contents never reach the screen, and block-level tags that
# separate paragraphs.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BREAK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header",
    "footer", "nav", "table", "ul", "ol", "dl", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
# In main-only mode, text inside these elements is page furniture rather than
# article content. (noscript/template/svg are already covered by _SKIP_TAGS.)
_MAIN_EXCLUDE_TAGS = {"nav", "footer", "aside", "form", "header"}
# A header inside one of these is the content's own header, so it stays.
_MAIN_CONTAINER_TAGS = {"article", "main"}


class _TextExtractor(HTMLParser):
    """Collect the visible text of a document.

    Not a renderer: good enough to read documentation, not good enough to
    reconstruct a table's layout, which is why the callers say so.
    """

    _SKIP = _SKIP_TAGS
    _BREAK = _BREAK_TAGS

    def __init__(self, main_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_only = main_only
        # Stack of open skip-tag names, not a bare depth counter: a
        # mismatched </style> after <script> must not expose the script's
        # content early.
        self._skip_stack: list[str] = []
        # Stack of open excluded elements for main-only mode. While it is
        # non-empty it acts as the skip-depth counter: nested excluded tags
        # deepen the skip, and only a matching close leaves the region.
        self._main_skip_stack: list[str] = []
        # Open article/main elements, so a content header can be told apart
        # from a page-level header.
        self._main_container_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_stack.append(tag)
            return
        if self.main_only:
            if self._main_skip_stack:
                # Deepen the skip for nested excluded tags; ignore every
                # other start tag, including block separators.
                if tag in _MAIN_EXCLUDE_TAGS:
                    self._main_skip_stack.append(tag)
                return
            if tag in _MAIN_EXCLUDE_TAGS and not (
                tag == "header" and self._main_container_stack
            ):
                self._main_skip_stack.append(tag)
                return
            if tag in _MAIN_CONTAINER_TAGS:
                self._main_container_stack.append(tag)
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if self.main_only and (
            self._main_skip_stack or tag in _MAIN_EXCLUDE_TAGS
        ):
            return
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            # An unmatched close tag would otherwise leave the stack
            # stuck and blank out the rest of the page.
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
        if self.main_only:
            if self._main_skip_stack:
                if tag in _MAIN_EXCLUDE_TAGS and self._main_skip_stack[-1] == tag:
                    self._main_skip_stack.pop()
                return
            if (
                tag in _MAIN_CONTAINER_TAGS
                and self._main_container_stack
                and self._main_container_stack[-1] == tag
            ):
                self._main_container_stack.pop()
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack and not self._main_skip_stack:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = joined.replace("\r\n", "\n").replace("\r", "\n")
        # Collapse runs of blanks and spaces: stripped markup is mostly
        # indentation, and leaving it in wastes most of the budget.
        joined = re.sub(r"[ \t\f\v]+", " ", joined)
        joined = re.sub(r" *\n *", "\n", joined)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()


def html_to_text(html: str, main_only: bool = False) -> str:
    """Readable text from an HTML document.

    When ``main_only`` is set, text from page furniture (navigation, footer,
    aside, forms and page-level headers) is omitted. The default keeps the
    original behavior for every caller.
    """
    extractor = _TextExtractor(main_only=main_only)
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup
        # html.parser is strict about nothing, but a half-downloaded
        # document can still trip it. Partial text beats an exception.
        pass
    return extractor.text()
