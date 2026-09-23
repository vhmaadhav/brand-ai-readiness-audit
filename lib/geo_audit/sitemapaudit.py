"""Deep sitemap.xml analysis.

A sitemap is the cheapest, highest-leverage signal a site can send about what
it wants indexed, and it is where the selection layer quietly fails: entries
that 404, entries that redirect, `lastmod` values that lie, protocol
mismatches, and — most consequential — pages that exist and matter but are
absent from the sitemap entirely.

Checks implemented against the sitemaps.org 0.9 protocol and the RFC 9309
`Sitemap:` directive:

* presence, reachability, well-formedness, correct namespace
* sitemap-index expansion and per-file entry limits (50,000 URLs / 50 MB)
* `lastmod` presence, W3C-datetime validity, and staleness distribution
* entries that are out of scope, non-canonical, or protocol-mismatched
* live status sampling: entries that 404, redirect, or are robots-blocked
* orphan detection: crawlable pages the sitemap never mentions
* declaration in robots.txt
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .netguard import same_scope

# sitemaps.org caps a single sitemap file at 50,000 URLs and 50 MB uncompressed.
MAX_URLS_PER_SITEMAP = 50_000
MAX_SITEMAP_BYTES = 50 * 1024 * 1024

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

_URL_ENTRY_RE = re.compile(r"<url>(.*?)</url>", re.I | re.S)
_TAG_RE = {
    "loc": re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.I),
    "lastmod": re.compile(r"<lastmod>\s*([^<]+?)\s*</lastmod>", re.I),
    "changefreq": re.compile(r"<changefreq>\s*([^<]+?)\s*</changefreq>", re.I),
    "priority": re.compile(r"<priority>\s*([^<]+?)\s*</priority>", re.I),
}
_NS_RE = re.compile(r'xmlns\s*=\s*["\']([^"\']+)["\']', re.I)

# W3C Datetime, the only format sitemaps.org permits for lastmod.
_W3C_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M%z", "%Y-%m", "%Y",
)


@dataclass
class SitemapEntry:
    loc: str
    lastmod: str = ""
    changefreq: str = ""
    priority: str = ""
    lastmod_parsed: datetime | None = None
    lastmod_valid: bool = True


@dataclass
class SitemapFile:
    url: str
    ok: bool
    status: int | None = None
    is_index: bool = False
    bytes: int = 0
    namespace: str = ""
    entries: list[SitemapEntry] = field(default_factory=list)
    child_sitemaps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SitemapAudit:
    """Everything the audit learned about a site's sitemap posture."""

    declared_in_robots: bool = False
    robots_sitemap_urls: list[str] = field(default_factory=list)
    files: list[SitemapFile] = field(default_factory=list)
    total_entries: int = 0
    unique_locs: list[str] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    lastmod_stats: dict = field(default_factory=dict)
    liveness: dict = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)
    discovered_paths: str = ""

    @property
    def exists(self) -> bool:
        return any(f.ok for f in self.files)

    def problem(self, code: str, detail: str, severity: str = "medium", urls: list[str] | None = None) -> None:
        self.problems.append(
            {"code": code, "detail": detail, "severity": severity, "examples": (urls or [])[:5]}
        )

    def to_dict(self) -> dict:
        return {
            "exists": self.exists,
            "declared_in_robots": self.declared_in_robots,
            "robots_sitemap_urls": self.robots_sitemap_urls,
            "files": [
                {
                    "url": f.url, "ok": f.ok, "status": f.status, "is_index": f.is_index,
                    "bytes": f.bytes, "namespace": f.namespace,
                    "entry_count": len(f.entries), "errors": f.errors,
                }
                for f in self.files
            ],
            "total_entries": self.total_entries,
            "unique_url_count": len(self.unique_locs),
            "problems": self.problems,
            "lastmod": self.lastmod_stats,
            "liveness": self.liveness,
            "orphan_count": len(self.orphans),
            "orphans": self.orphans[:15],
            "discovery_note": self.discovered_paths,
        }


def parse_lastmod(value: str) -> tuple[datetime | None, bool]:
    """Parse a W3C-datetime ``lastmod``. Returns ``(parsed, is_valid_format)``."""
    raw = (value or "").strip()
    if not raw:
        return None, True
    normalised = raw.replace("Z", "+0000")
    if re.search(r"[+-]\d{2}:\d{2}$", normalised):
        normalised = normalised[:-3] + normalised[-2:]
    for fmt in _W3C_FORMATS:
        try:
            parsed = datetime.strptime(normalised, fmt)
            return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed), True
        except ValueError:
            continue
    return None, False


def parse_sitemap_file(xml: str, url: str, *, status: int | None, ok: bool) -> SitemapFile:
    """Parse one sitemap or sitemap index."""
    sitemap = SitemapFile(url=url, ok=ok, status=status, bytes=len(xml.encode("utf-8", "replace")))
    if not ok:
        sitemap.errors.append(f"not retrievable (status {status})")
        return sitemap

    namespace = _NS_RE.search(xml)
    sitemap.namespace = namespace.group(1) if namespace else ""
    if sitemap.namespace and sitemap.namespace != SITEMAP_NS:
        sitemap.errors.append(
            f"declares namespace {sitemap.namespace!r}, expected {SITEMAP_NS!r}"
        )
    elif not sitemap.namespace:
        sitemap.errors.append("no xmlns declared; parsers may reject the file")

    sitemap.is_index = "<sitemapindex" in xml.lower()
    if sitemap.is_index:
        sitemap.child_sitemaps = [m.strip() for m in _TAG_RE["loc"].findall(xml)]
        return sitemap

    blocks = _URL_ENTRY_RE.findall(xml)
    if not blocks and "<loc>" in xml.lower():
        # Well-formed <loc> outside <url> wrappers: salvage them, but say so.
        sitemap.errors.append("<loc> elements are not wrapped in <url> elements")
        blocks = [f"<loc>{m}</loc>" for m in _TAG_RE["loc"].findall(xml)]

    for block in blocks:
        loc_match = _TAG_RE["loc"].search(block)
        if not loc_match:
            continue
        entry = SitemapEntry(loc=loc_match.group(1).strip())
        for name in ("lastmod", "changefreq", "priority"):
            match = _TAG_RE[name].search(block)
            if match:
                setattr(entry, name, match.group(1).strip())
        entry.lastmod_parsed, entry.lastmod_valid = parse_lastmod(entry.lastmod)
        sitemap.entries.append(entry)

    if sitemap.bytes > MAX_SITEMAP_BYTES:
        sitemap.errors.append(f"exceeds the 50 MB per-file limit ({sitemap.bytes} bytes)")
    if len(sitemap.entries) > MAX_URLS_PER_SITEMAP:
        sitemap.errors.append(
            f"contains {len(sitemap.entries)} URLs, above the 50,000 per-file limit"
        )
    return sitemap


def analyse(
    audit: SitemapAudit,
    *,
    scope_domain: str,
    origin_scheme: str,
    crawled_urls: list[str] | None = None,
    now: datetime | None = None,
) -> SitemapAudit:
    """Derive problems from the parsed sitemap files."""
    now = now or datetime.now(timezone.utc)
    entries = [e for f in audit.files for e in f.entries]
    audit.total_entries = len(entries)
    audit.unique_locs = sorted({e.loc for e in entries})

    if not audit.exists:
        audit.problem(
            "sitemap_missing",
            "No sitemap.xml was retrievable via robots.txt or the conventional paths.",
            "high",
        )
        return audit

    if not audit.declared_in_robots:
        audit.problem(
            "sitemap_not_declared",
            "A sitemap exists but robots.txt does not declare it with a Sitemap: "
            "directive, so a crawler must guess the path.",
            "medium",
        )

    for sitemap in audit.files:
        for error in sitemap.errors:
            audit.problem("sitemap_malformed", f"{sitemap.url}: {error}", "medium", [sitemap.url])

    if audit.total_entries == 0:
        audit.problem("sitemap_empty", "The sitemap contains no <url> entries.", "high")
        return audit

    duplicates = len(entries) - len(audit.unique_locs)
    if duplicates:
        audit.problem(
            "sitemap_duplicates",
            f"{duplicates} duplicate <loc> entries across the sitemap set.",
            "low",
        )

    off_scope, wrong_scheme, malformed_lastmod, has_fragment = [], [], [], []
    for entry in entries:
        parts = urlsplit(entry.loc)
        host = (parts.hostname or "").lower()
        if not host or not same_scope(host, scope_domain):
            off_scope.append(entry.loc)
        elif parts.scheme != origin_scheme:
            wrong_scheme.append(entry.loc)
        if parts.fragment:
            has_fragment.append(entry.loc)
        if not entry.lastmod_valid:
            malformed_lastmod.append(f"{entry.loc} (lastmod={entry.lastmod!r})")

    if off_scope:
        audit.problem(
            "sitemap_off_scope",
            f"{len(off_scope)} entries point outside {scope_domain}; a sitemap may "
            "only list URLs under the host that serves it.",
            "medium", off_scope,
        )
    if wrong_scheme:
        audit.problem(
            "sitemap_scheme_mismatch",
            f"{len(wrong_scheme)} entries use a different scheme than the live site "
            f"({origin_scheme}), which forces a redirect on every crawl.",
            "medium", wrong_scheme,
        )
    if has_fragment:
        audit.problem(
            "sitemap_fragments",
            f"{len(has_fragment)} entries contain URL fragments, which are never sent "
            "to a server and cannot identify a distinct page.",
            "low", has_fragment,
        )
    if malformed_lastmod:
        audit.problem(
            "sitemap_bad_lastmod",
            f"{len(malformed_lastmod)} entries carry a lastmod that is not a valid "
            "W3C datetime, so crawlers will ignore it.",
            "low", malformed_lastmod,
        )

    # lastmod coverage and staleness.
    dated = [e for e in entries if e.lastmod_parsed]
    coverage = len(dated) / len(entries) if entries else 0.0
    stats: dict = {
        "entries_with_lastmod": len(dated),
        "coverage": round(coverage, 3),
    }
    if dated:
        ages = [max(0, (now - e.lastmod_parsed).days) for e in dated]
        ages.sort()
        stats.update(
            {
                "median_age_days": ages[len(ages) // 2],
                "oldest_age_days": ages[-1],
                "newest_age_days": ages[0],
                "stale_over_365d": sum(1 for a in ages if a > 365),
                "future_dated": sum(1 for e in dated if e.lastmod_parsed > now),
            }
        )
        if stats["future_dated"]:
            audit.problem(
                "sitemap_future_lastmod",
                f"{stats['future_dated']} entries carry a lastmod in the future, which "
                "crawlers treat as untrustworthy and commonly ignore wholesale.",
                "medium",
            )
        if stats["median_age_days"] > 365 and len(dated) >= 5:
            audit.problem(
                "sitemap_stale",
                f"Median lastmod age is {stats['median_age_days']} days; the sitemap "
                "signals a site that has not changed in over a year.",
                "medium",
            )
        # All-identical lastmod means the field is generated, not meaningful.
        if len({e.lastmod for e in dated}) == 1 and len(dated) >= 10:
            audit.problem(
                "sitemap_uniform_lastmod",
                f"All {len(dated)} entries share one lastmod value, so the field "
                "carries no per-page change information.",
                "low",
            )
    elif entries:
        audit.problem(
            "sitemap_no_lastmod",
            "No entry carries a lastmod, so crawlers cannot prioritise what changed.",
            "low",
        )
    audit.lastmod_stats = stats

    # Orphans: pages we reached by crawling that the sitemap never lists.
    if crawled_urls:
        listed = {u.rstrip("/") for u in audit.unique_locs}
        orphans = [u for u in crawled_urls if u.rstrip("/") not in listed]
        audit.orphans = orphans
        if orphans and len(crawled_urls) >= 3:
            share = len(orphans) / len(crawled_urls)
            audit.problem(
                "sitemap_orphans",
                f"{len(orphans)} of {len(crawled_urls)} crawled pages "
                f"({share:.0%}) are absent from the sitemap.",
                "high" if share > 0.5 else "medium",
                orphans,
            )
    return audit


def record_liveness(audit: SitemapAudit, samples: list[dict]) -> SitemapAudit:
    """Fold in live status checks for a sample of sitemap entries."""
    if not samples:
        return audit

    broken = [s for s in samples if s.get("status") and s["status"] >= 400]
    redirects = [s for s in samples if s.get("redirect_chain")]
    audit.liveness = {
        "sampled": len(samples),
        "broken": len(broken),
        "redirecting": len(redirects),
        "broken_rate": round(len(broken) / len(samples), 3),
    }

    if broken:
        audit.problem(
            "sitemap_broken_entries",
            f"{len(broken)} of {len(samples)} sampled sitemap entries return an error "
            "status. A sitemap that lists dead URLs reduces trust in the whole file.",
            "high" if len(broken) / len(samples) > 0.2 else "medium",
            [f"{s['url']} -> {s['status']}" for s in broken],
        )
    if len(redirects) / len(samples) > 0.3:
        audit.problem(
            "sitemap_redirect_heavy",
            f"{len(redirects)} of {len(samples)} sampled entries redirect. A sitemap "
            "should list final canonical URLs, not URLs that bounce.",
            "medium",
            [f"{s['url']} -> {s['redirect_chain'][-1]['to']}" for s in redirects if s.get("redirect_chain")],
        )
    return audit
