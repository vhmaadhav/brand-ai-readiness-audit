"""Bounded page discovery.

Strategy, in order of preference:

1. ``robots.txt`` ``Sitemap:`` directives
2. ``/sitemap.xml`` and the usual variants
3. A shallow, same-scope crawl of the homepage's links

Discovery is deliberately *representative* rather than exhaustive. The audit
makes population claims ("product pages lack structured data"), so the sample
must span page types rather than 20 near-identical blog posts. :func:`select_sample`
buckets candidates by URL shape and round-robins across the buckets.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urljoin

from .netguard import UrlRejected, same_scope, validate_url

SITEMAP_CANDIDATES = (
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap.xml.gz", "/sitemap/sitemap.xml", "/wp-sitemap.xml",
)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAPINDEX_RE = re.compile(r"<sitemapindex", re.I)

# URL shapes that stand for distinct page types. Sampling across these is what
# makes a population claim defensible.
PAGE_TYPE_PATTERNS = [
    ("product",  re.compile(r"/(product|products|item|shop|store|p)/", re.I)),
    ("pricing",  re.compile(r"/(pricing|plans|price)", re.I)),
    ("article",  re.compile(r"/(blog|news|article|post|insights|resources)/", re.I)),
    ("docs",     re.compile(r"/(docs|documentation|guide|help|support|kb)/", re.I)),
    ("about",    re.compile(r"/(about|company|team|contact|who-we-are)", re.I)),
    ("service",  re.compile(r"/(service|services|solutions|platform|features)", re.I)),
    ("legal",    re.compile(r"/(privacy|terms|legal|cookie|gdpr)", re.I)),
    ("category", re.compile(r"/(category|collection|topics|tag)/", re.I)),
]

# Never worth a page of the budget.
SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|webp|svg|ico|css|js|mp4|mp3|zip|gz|woff2?|ttf|eot|xml|json|rss|atom)$",
    re.I,
)
SKIP_PATHS = re.compile(
    r"/(wp-admin|wp-content|wp-includes|cdn-cgi|_next/static|assets|static|"
    r"login|signin|signup|register|logout|cart|checkout|account|admin|api)(/|$)",
    re.I,
)


@dataclass
class Candidate:
    url: str
    source: str            # sitemap | robots-sitemap | homepage-link | seed
    page_type: str = "other"
    lastmod: str = ""


@dataclass
class Discovery:
    candidates: list[Candidate] = field(default_factory=list)
    sitemap_urls: list[str] = field(default_factory=list)
    sitemap_found: bool = False
    sitemap_entry_count: int = 0
    llms_txt: bool = False
    notes: list[str] = field(default_factory=list)


def classify_page_type(url: str) -> str:
    path = urlsplit(url).path or "/"
    if path in ("/", ""):
        return "homepage"
    for name, pattern in PAGE_TYPE_PATTERNS:
        if pattern.search(path):
            return name
    return "other"


def is_crawlable(url: str, scope_domain: str) -> bool:
    """Cheap pre-filter before the URL costs a validation or a fetch."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or not same_scope(host, scope_domain):
        return False
    path = parts.path or "/"
    return not (SKIP_EXTENSIONS.search(path) or SKIP_PATHS.search(path))


def parse_sitemap(xml: str, base_url: str) -> tuple[list[str], bool]:
    """Return ``(urls, is_index)`` from sitemap XML."""
    locations = [urljoin(base_url, loc.strip()) for loc in _LOC_RE.findall(xml)]
    return locations, bool(_SITEMAPINDEX_RE.search(xml))


def discover(fetcher, origin: str, scope_domain: str, robots_policy, *,
             max_candidates: int = 300, deadline: float | None = None) -> Discovery:
    """Build a candidate pool without spending the page budget on it.

    *deadline* is a ``time.monotonic()`` instant after which no further sitemap
    is fetched. Discovery is the one phase whose cost is set by the *site's*
    size rather than by ``max_pages``: a sitemap index with forty children, each
    megabytes wide, can be walked one at a time for minutes, and a stage killed
    by its parent for exceeding its slice publishes nothing at all. Stopping
    early instead keeps the candidates already found -- enough to fill
    ``max_pages`` many times over on any real site -- and records why it
    stopped, so a partial pool is never mistaken for a complete one.
    """
    found = Discovery()
    seen: set[str] = set()
    truncated = False

    def add(url: str, source: str, lastmod: str = "") -> None:
        clean = url.split("#")[0].rstrip("/") or url
        if clean in seen or not is_crawlable(clean, scope_domain):
            return
        if len(found.candidates) >= max_candidates:
            return
        seen.add(clean)
        found.candidates.append(
            Candidate(url=clean, source=source, page_type=classify_page_type(clean), lastmod=lastmod)
        )

    add(origin, "seed")

    # llms.txt: an emerging convention for telling assistants what matters.
    llms = fetcher.fetch(urljoin(origin, "/llms.txt"), count_against_budget=False)
    found.llms_txt = bool(llms.ok and "#" in llms.body[:2000])

    sitemap_queue = list(robots_policy.sitemaps) if robots_policy.exists else []
    for path in SITEMAP_CANDIDATES:
        sitemap_queue.append(urljoin(origin, path))

    processed: set[str] = set()
    index_expansions = 0
    for sitemap_url in sitemap_queue:
        if sitemap_url in processed or len(found.candidates) >= max_candidates:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            truncated = True
            break
        processed.add(sitemap_url)
        try:
            validate_url(sitemap_url, scope_domain=scope_domain)
        except UrlRejected:
            continue

        result = fetcher.fetch(sitemap_url, count_against_budget=False)
        if not result.ok or "<loc>" not in result.body:
            continue

        found.sitemap_found = True
        found.sitemap_urls.append(sitemap_url)
        locations, is_index = parse_sitemap(result.body, sitemap_url)
        found.sitemap_entry_count += len(locations)

        if is_index and index_expansions < 3:
            index_expansions += 1
            sitemap_queue.extend(locations[:5])
            continue
        for location in locations:
            add(location, "sitemap")

    if not found.sitemap_found:
        found.notes.append("no sitemap.xml discovered via robots.txt or common paths")
    if truncated:
        found.notes.append(
            "sitemap discovery stopped at its time limit; the candidate pool is "
            "a partial sample of a large sitemap, not the whole site"
        )

    # Fall back to homepage links when the sitemap is missing or thin.
    if len(found.candidates) < 12:
        home = fetcher.fetch(origin, count_against_budget=False)
        if home.ok and home.is_html:
            from .htmldoc import parse_html
            doc = parse_html(home.body, url=home.final_url, scope_host=scope_domain)
            for link in doc.internal_links:
                add(link.href, "homepage-link")
            found.notes.append(
                f"supplemented discovery with {len(doc.internal_links)} homepage links"
            )

    return found


def select_sample(candidates: list[Candidate], limit: int) -> list[Candidate]:
    """Pick a representative sample spanning page types.

    Round-robins across type buckets so a 200-post blog cannot crowd out the
    product and pricing pages. The homepage is always first.
    """
    if len(candidates) <= limit:
        return candidates

    buckets: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        buckets.setdefault(candidate.page_type, []).append(candidate)

    chosen: list[Candidate] = []
    for homepage in buckets.pop("homepage", [])[:1]:
        chosen.append(homepage)

    # Distinctive types first; "other" is the residue and goes last.
    order = sorted(buckets, key=lambda name: (name == "other", name))
    while len(chosen) < limit and any(buckets.get(name) for name in order):
        for name in order:
            if len(chosen) >= limit:
                break
            if buckets.get(name):
                chosen.append(buckets[name].pop(0))
    return chosen[:limit]
