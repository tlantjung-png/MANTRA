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


class _TextExtractor(HTMLParser):
    """Collect the visible text of a document.

    Not a renderer: good enough to read documentation, not good enough to
    reconstruct a table's layout, which is why the callers say so.
    """

    _SKIP = _SKIP_TAGS
    _BREAK = _BREAK_TAGS

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # Stack of open skip-tag names, not a bare depth counter: a
        # mismatched </style> after <script> must not expose the script's
        # content early.
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_stack.append(tag)
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            # An unmatched close tag would otherwise leave the stack
            # stuck and blank out the rest of the page.
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
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


def html_to_text(html: str) -> str:
    """Readable text from an HTML document."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup
        # html.parser is strict about nothing, but a half-downloaded
        # document can still trip it. Partial text beats an exception.
        pass
    return extractor.text()
