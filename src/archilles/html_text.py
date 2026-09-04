"""HTML-to-plain-text stripping.

Shared helper for Zotero notes/abstracts, which arrive as HTML fragments.
Uses the stdlib HTMLParser (handles entities and malformed markup) and
collapses all whitespace to single spaces.
"""

import re
from html.parser import HTMLParser

__all__ = ["strip_html"]


# Tags that end a run of text. Everything else — <i>, <b>, <sup>, <span>, <a> —
# is inline and must not introduce whitespace: Zotero abstracts and notes carry
# italics inside words and around punctuation all the time.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "td", "th", "tr", "ul",
})


class _HTMLStripper(HTMLParser):
    """Minimal HTML-to-text converter.

    Parts are joined with ``""`` rather than ``" "`` (finding 1.12). Joining
    with a space split every word that contained inline markup —
    ``<i>pre</i>fix`` became ``pre fix`` and ``<i>Hamlet</i>'s`` became
    ``Hamlet 's`` — and the result goes into an embedding, so the damage was a
    quietly worse vector that no later scan corrects. Block-level tags insert
    their own whitespace, which the caller's ``\\s+`` collapse then normalises.
    """

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def _break(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_starttag(self, tag, attrs) -> None:
        self._break(tag)

    def handle_startendtag(self, tag, attrs) -> None:
        self._break(tag)

    def handle_endtag(self, tag) -> None:
        self._break(tag)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def strip_html(html: str) -> str:
    """Strip HTML tags and return whitespace-collapsed plain text."""
    if not html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return re.sub(r"\s+", " ", stripper.get_text()).strip()
