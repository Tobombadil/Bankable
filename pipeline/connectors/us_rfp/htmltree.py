"""A small, lenient HTML tree on the standard library's tokenizer.

The repo carries no HTML parsing dependency (no lxml, no BeautifulSoup), and the issuer pages this
connector reads are plain server-rendered markup. `parse` builds a tree of `Node`s, closing the
elements browsers close implicitly (an open `<p>` before a block, a sibling `<li>`, `<tr>`, `<td>`),
ignoring stray end tags and treating void elements as leaves. Queries are by tag and class only,
which is all the extractors need.

`fix_mojibake` repairs the one encoding defect seen on the AEP pages: UTF-8 punctuation that was
decoded as Windows-1252 and re-encoded ("2025 ... RFPs - Proposals Due July 15, 2025 â€" CLOSED").
"""

from __future__ import annotations

import html.parser
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
#: Elements whose start closes an open `<p>` (the HTML content model, abridged).
_CLOSES_P = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "fieldset",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    }
)
#: An element of the key's kind closes an open sibling of any of the value's kinds.
_CLOSES_SIBLING = {
    "li": frozenset({"li"}),
    "tr": frozenset({"tr", "td", "th"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "option": frozenset({"option"}),
}
_SKIP_TEXT = frozenset({"script", "style", "noscript", "template", "svg"})
#: Elements that start a new line in `Node.lines`.
_BLOCK = _CLOSES_P | frozenset({"li", "tr", "td", "th", "br", "dd", "dt"})

#: Windows-1252 readings of UTF-8 punctuation, longest first.
_MOJIBAKE = (
    ("\u00e2\u20ac\u201c", "\u2013"),  # en dash
    ("\u00e2\u20ac\u201d", "\u2014"),  # em dash
    ("\u00e2\u20ac\u2122", "\u2019"),  # right single quote
    ("\u00e2\u20ac\u02dc", "\u2018"),  # left single quote
    ("\u00e2\u20ac\u0153", "\u201c"),  # left double quote
    ("\u00e2\u20ac\u009d", "\u201d"),  # right double quote
    ("\u00e2\u20ac\u00a6", "\u2026"),  # ellipsis
    ("\u00c2\u00a0", "\u00a0"),
    ("\u00c2\u00a7", "\u00a7"),
)
_WS = re.compile(r"\s+")


def fix_mojibake(text: str) -> str:
    for bad, good in _MOJIBAKE:
        if bad in text:
            text = text.replace(bad, good)
    return text


def clean_text(text: str) -> str:
    """Whitespace collapsed (non-breaking spaces included), mojibake repaired, ends stripped."""
    return _WS.sub(" ", fix_mojibake(text).replace("\u00a0", " ")).strip()


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def iter(self) -> Iterator[Node]:
        """This node and every element below it, in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str | None = None, cls: str | None = None) -> list[Node]:
        return [
            n
            for n in self.iter()
            if n is not self and (tag is None or n.tag == tag) and (cls is None or cls in n.classes)
        ]

    def find(self, tag: str | None = None, cls: str | None = None) -> Node | None:
        found = self.find_all(tag, cls)
        return found[0] if found else None

    def text(self) -> str:
        """Visible text as a reader sees it: inline elements run together, blocks separated by a
        space, cleaned (`clean_text`)."""
        parts: list[str] = []
        self._collect(parts, " ")
        return clean_text("".join(parts))

    def lines(self) -> list[str]:
        """Visible text split at block boundaries: one cleaned string per non-empty line."""
        parts: list[str] = []
        self._collect(parts, "\n")
        return [ln for ln in (clean_text(p) for p in "".join(parts).split("\n")) if ln]

    def _collect(self, parts: list[str], separator: str) -> None:
        if self.tag in _SKIP_TEXT:
            return
        if self.tag in _BLOCK:
            parts.append(separator)
        for child in self.children:
            if isinstance(child, Node):
                child._collect(parts, separator)
            else:
                parts.append(child)
        if self.tag in _BLOCK:
            parts.append(separator)


class _Builder(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack: list[Node] = [self.root]

    def _close(
        self, tags: frozenset[str], stop_at: frozenset[str] = frozenset({"table", "ul", "ol"})
    ) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[i].tag
            if tag in tags:
                del self.stack[i:]
                return
            if tag in stop_at or tag in ("div", "body", "html"):
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _CLOSES_P:
            self._close(frozenset({"p"}))
        if tag in _CLOSES_SIBLING:
            self._close(_CLOSES_SIBLING[tag])
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(markup: str) -> Node:
    builder = _Builder()
    builder.feed(markup)
    builder.close()
    return builder.root


def region(markup: str, start: str, ends: tuple[str, ...], max_chars: int = 400_000) -> str | None:
    """The markup from the tag that contains `start` up to the first of `ends` after it, or None
    when `start` is absent. Cutting at a marker keeps the stored snapshot to the part of the page
    the extractor reads, without the navigation, scripts and per-request tokens around it (so two
    fetches of an unchanged listing store the same bytes)."""
    at = markup.find(start)
    if at < 0:
        return None
    tag_open = markup.rfind("<", 0, at)
    begin = tag_open if tag_open >= 0 else at
    stop = len(markup)
    for end in ends:
        pos = markup.find(end, at + len(start))
        if 0 <= pos < stop:
            # Cut before the tag the marker sits in, so no half tag is left at the end.
            tag_start = markup.rfind("<", at, pos)
            stop = tag_start if tag_start > at else pos
    return markup[begin : min(stop, begin + max_chars)]
