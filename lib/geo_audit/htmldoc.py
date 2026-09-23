"""A DOM-lite built on the standard library's ``html.parser``.

No lxml, no BeautifulSoup — the marketplace must be self-contained. What we
need is narrower than a real DOM anyway: visible text, structural counts,
metadata, links, and embedded JSON-LD.

The crucial behaviour is that ``script``, ``style``, ``template`` and
``noscript`` content never counts as visible text. That separation is what lets
:mod:`textstats` measure the render gap: a page whose substance lives inside a
JSON blob in a ``script`` tag has almost no extractable text, which is exactly
the failure mode described in the handout's appendix C.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

NON_TEXT_TAGS = {"script", "style", "template", "noscript", "svg", "canvas", "iframe"}
BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "footer", "nav", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "tr", "blockquote",
    "pre", "figcaption", "dd", "dt", "br", "hr", "form", "ul", "ol", "table",
}
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Regions that are chrome rather than substance. Used for the main-content
# heuristic, never to discard evidence outright.
BOILERPLATE_HINTS = re.compile(
    r"\b(nav|menu|header|footer|sidebar|breadcrumb|cookie|consent|banner|"
    r"newsletter|subscribe|social|share|advert|promo|modal|popup|skip-link)\b",
    re.I,
)
MAIN_HINTS = re.compile(r"\b(main|content|article|post|entry|body|prose|documentation)\b", re.I)


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Link:
    href: str
    text: str
    rel: str = ""
    internal: bool = False
    nofollow: bool = False


@dataclass
class HtmlDoc:
    """Parsed view of one HTML page."""

    url: str = ""
    title: str = ""
    lang: str = ""
    text: str = ""
    main_text: str = ""
    headings: list[Heading] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    json_ld: list[dict] = field(default_factory=list)
    json_ld_errors: list[str] = field(default_factory=list)
    microdata_types: list[str] = field(default_factory=list)
    rdfa_types: list[str] = field(default_factory=list)
    images: int = 0
    images_missing_alt: int = 0
    tables: int = 0
    code_blocks: int = 0
    script_tags: int = 0
    inline_script_chars: int = 0
    noscript_chars: int = 0
    forms: int = 0
    canonical: str = ""
    time_elements: list[str] = field(default_factory=list)
    raw_html_chars: int = 0
    parse_error: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.main_text.split()) if self.main_text else 0

    @property
    def total_word_count(self) -> int:
        return len(self.text.split()) if self.text else 0

    @property
    def heading_count(self) -> int:
        return len(self.headings)

    @property
    def paragraph_count(self) -> int:
        return len(self.paragraphs)

    @property
    def internal_links(self) -> list[Link]:
        return [l for l in self.links if l.internal]

    @property
    def external_links(self) -> list[Link]:
        return [l for l in self.links if not l.internal and l.href.startswith("http")]

    def meta_get(self, *names: str) -> str:
        for name in names:
            value = self.meta.get(name.lower())
            if value:
                return value
        return ""

    def structure_summary(self) -> dict:
        return {
            "word_count": self.word_count,
            "total_word_count": self.total_word_count,
            "heading_count": self.heading_count,
            "h1_count": sum(1 for h in self.headings if h.level == 1),
            "paragraph_count": self.paragraph_count,
            "list_item_count": len(self.list_items),
            "list_density": round(len(self.list_items) / max(1, self.paragraph_count + len(self.list_items)), 4),
            "table_count": self.tables,
            "code_block_count": self.code_blocks,
            "internal_link_count": len(self.internal_links),
            "external_link_count": len(self.external_links),
            "image_count": self.images,
            "images_missing_alt": self.images_missing_alt,
            "json_ld_block_count": len(self.json_ld),
        }


class _Collector(HTMLParser):
    """Single-pass collector. Tracks a tag stack so nesting stays correct."""

    def __init__(self, base_url: str, scope_host: str) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = HtmlDoc(url=base_url)
        self.base_url = base_url
        self.scope_host = scope_host

        self._stack: list[str] = []
        self._suppress_depth = 0
        self._chunks: list[str] = []
        self._buffer: list[str] = []
        self._current_heading: Heading | None = None
        self._current_link: Link | None = None
        self._in_title = False
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []
        self._in_noscript = False

        # Main-content tracking: text collected while inside a container whose
        # id/class looks like content, minus anything that looks like chrome.
        self._main_depth = 0
        self._main_chunks: list[str] = []
        self._saw_main_region = False

    # -- helpers ---------------------------------------------------------

    def _flush(self) -> None:
        text = " ".join(" ".join(self._buffer).split())
        self._buffer.clear()
        if not text:
            return
        self._chunks.append(text)
        if self._main_depth > 0 or not self._saw_main_region:
            self._main_chunks.append(text)

        parent = self._stack[-1] if self._stack else ""
        if parent == "p" and len(text) > 1:
            self.doc.paragraphs.append(text)
        elif parent == "li":
            self.doc.list_items.append(text)

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], name: str) -> str:
        for key, value in attrs:
            if key.lower() == name:
                return (value or "").strip()
        return ""

    # -- parser callbacks -------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        self._stack.append(tag)

        if tag in NON_TEXT_TAGS:
            self._flush()
            self._suppress_depth += 1
            if tag == "script":
                self.doc.script_tags += 1
                if self._attr(attrs, "type").lower() == "application/ld+json":
                    self._in_jsonld = True
                    self._jsonld_buf = []
            elif tag == "noscript":
                self._in_noscript = True
            return

        if self._suppress_depth:
            return

        identity = f"{self._attr(attrs, 'id')} {self._attr(attrs, 'class')}"
        role = self._attr(attrs, "role").lower()
        if tag == "main" or role == "main" or (identity.strip() and MAIN_HINTS.search(identity) and not BOILERPLATE_HINTS.search(identity)):
            self._flush()
            if not self._saw_main_region:
                # Everything gathered before the first real content region was
                # chrome (nav, banners, skip links). Drop it now that we know
                # where the substance starts.
                self._main_chunks.clear()
                self._saw_main_region = True
            self._main_depth += 1
        elif self._main_depth > 0 and BOILERPLATE_HINTS.search(identity):
            # Chrome nested inside the content region: stop counting it.
            self._flush()
            self._main_depth -= 1
            self._stack[-1] = tag + "\x00boilerplate"

        if tag in HEADING_TAGS:
            self._flush()
            self._current_heading = Heading(level=int(tag[1]), text="")
        elif tag == "title":
            self._in_title = True
        elif tag == "a":
            href = self._attr(attrs, "href")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                rel = self._attr(attrs, "rel").lower()
                absolute = urljoin(self.base_url, href)
                host = (urlsplit(absolute).hostname or "").lower()
                self._current_link = Link(
                    href=absolute, text="", rel=rel,
                    internal=bool(host) and (host == self.scope_host or host.endswith("." + self.scope_host)),
                    nofollow="nofollow" in rel,
                )
        elif tag == "meta":
            key = (self._attr(attrs, "name") or self._attr(attrs, "property")
                   or self._attr(attrs, "http-equiv") or self._attr(attrs, "itemprop")).lower()
            content = self._attr(attrs, "content")
            if key and content:
                self.doc.meta.setdefault(key, content)
        elif tag == "link":
            if "canonical" in self._attr(attrs, "rel").lower():
                self.doc.canonical = urljoin(self.base_url, self._attr(attrs, "href"))
        elif tag == "html":
            self.doc.lang = self._attr(attrs, "lang")
        elif tag == "img":
            self.doc.images += 1
            if not self._attr(attrs, "alt"):
                self.doc.images_missing_alt += 1
        elif tag == "table":
            self.doc.tables += 1
        elif tag in ("pre", "code"):
            self.doc.code_blocks += 1
        elif tag == "form":
            self.doc.forms += 1
        elif tag == "time":
            stamp = self._attr(attrs, "datetime")
            if stamp:
                self.doc.time_elements.append(stamp)

        itemtype = self._attr(attrs, "itemtype")
        if itemtype:
            self.doc.microdata_types.append(itemtype)
        typeof = self._attr(attrs, "typeof")
        if typeof:
            self.doc.rdfa_types.append(typeof)

        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in NON_TEXT_TAGS:
            if self._in_jsonld and tag == "script":
                self._absorb_jsonld("".join(self._jsonld_buf))
                self._in_jsonld = False
            if tag == "noscript":
                self._in_noscript = False
            self._suppress_depth = max(0, self._suppress_depth - 1)
        elif not self._suppress_depth:
            if tag in HEADING_TAGS and self._current_heading is not None:
                text = " ".join(" ".join(self._buffer).split())
                self._current_heading.text = text
                if text:
                    self.doc.headings.append(self._current_heading)
                self._current_heading = None
            elif tag == "title":
                self._in_title = False
            elif tag == "a" and self._current_link is not None:
                self._current_link.text = " ".join(" ".join(self._buffer).split())[:200]
                self.doc.links.append(self._current_link)
                self._current_link = None
            if tag in BLOCK_TAGS:
                self._flush()

        # Unwind the stack to the matching open tag, tolerating bad markup.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index].split("\x00")[0] == tag:
                if self._stack[index].endswith("\x00boilerplate"):
                    self._main_depth += 1
                elif tag == "main" or index == 0:
                    self._main_depth = max(0, self._main_depth - 1)
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_noscript:
            self.doc.noscript_chars += len(data.strip())
            return
        if self._suppress_depth:
            self.doc.inline_script_chars += len(data.strip())
            return
        if self._in_title:
            self.doc.title += data.strip() + " "
            return
        self._buffer.append(data)

    # -- JSON-LD ----------------------------------------------------------

    def _absorb_jsonld(self, payload: str) -> None:
        payload = payload.strip()
        if not payload:
            return
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            self.doc.json_ld_errors.append(f"invalid JSON-LD at line {exc.lineno}: {exc.msg}")
            return
        for block in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(block, dict):
                self.doc.json_ld.append(block)
            else:
                self.doc.json_ld_errors.append(
                    f"JSON-LD block is a {type(block).__name__}, expected an object"
                )

    def close(self) -> HtmlDoc:  # type: ignore[override]
        super().close()
        self._flush()
        self.doc.text = "\n".join(self._chunks)
        main = "\n".join(self._main_chunks).strip()
        # A main region that captured almost nothing is a bad heuristic result,
        # not a thin page. Fall back to full text rather than inventing a defect.
        self.doc.main_text = main if len(main.split()) >= 0.25 * len(self.doc.text.split()) else self.doc.text
        self.doc.title = self.doc.title.strip()
        return self.doc


def parse_html(html: str, url: str = "", scope_host: str = "") -> HtmlDoc:
    """Parse *html* into an :class:`HtmlDoc`. Never raises on malformed markup."""
    if not scope_host:
        scope_host = (urlsplit(url).hostname or "").lower()
    collector = _Collector(url, scope_host)
    try:
        collector.feed(html)
        doc = collector.close()
    except Exception as exc:  # noqa: BLE001 - the web is full of broken HTML
        doc = collector.doc
        doc.parse_error = f"{type(exc).__name__}: {exc}"
        doc.text = doc.text or "\n".join(collector._chunks)
        doc.main_text = doc.main_text or doc.text
    doc.raw_html_chars = len(html)
    doc.url = url
    return doc


def iter_jsonld_nodes(blocks: list[dict]):
    """Yield every node in the JSON-LD graph, flattening ``@graph`` containers."""
    stack = list(blocks)
    seen = 0
    while stack and seen < 500:
        node = stack.pop()
        seen += 1
        if not isinstance(node, dict):
            continue
        graph = node.get("@graph")
        if isinstance(graph, list):
            stack.extend(g for g in graph if isinstance(g, dict))
        yield node
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, dict))


def jsonld_types(node: dict) -> list[str]:
    """Normalise a node's ``@type`` to a list of bare type names."""
    raw = node.get("@type") or node.get("type") or []
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).split("/")[-1].split(":")[-1] for v in values if v]
