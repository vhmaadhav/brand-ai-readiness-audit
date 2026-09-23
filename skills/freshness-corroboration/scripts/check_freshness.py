#!/usr/bin/env python3
"""Trust-layer detector: datestamps, staleness and corroboration.

Reads the page inventory written by the reachability layer and re-parses the
saved HTML from disk. It never fetches anything, so it can be re-run offline
against a stored workspace as many times as needed.

Two grounded ideas drive the checks:

* Handout appendix D - agreement across the web is what makes a claim usable.
  A statement that exists only on the brand's own site has nothing to agree
  with it.
* arXiv:2604.25707 SS8.4 domain-type influence: encyclopedia 0.2144,
  academic_publishing 0.1118, commercial 0.1028, nonprofit 0.0971,
  academic 0.0815, government 0.0769, news_media 0.0726. News media is
  *selected* constantly and *absorbed* weakly, so press coverage is not a
  substitute for owning the explanatory page.
"""

import sys
import pathlib

_here = pathlib.Path(__file__).resolve()
for _p in _here.parents:
    if (_p / "lib" / "geo_audit" / "__init__.py").is_file():
        sys.path.insert(0, str(_p / "lib"))
        break

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.htmldoc import HtmlDoc, iter_jsonld_nodes, parse_html  # noqa: E402
from geo_audit.netguard import registrable_domain  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.textstats import CITATION_MARKERS, DOMAIN_TYPE_INFLUENCE  # noqa: E402
from geo_audit.workspace import Workspace  # noqa: E402

LAYER = "trust"

# How long before a datestamp on a page of this type is worth reporting.
# Types absent from this table are never judged stale: a pricing page or an
# about page has no natural decay, and flagging one is a false positive.
STALE_DAYS = {"article": 730, "docs": 548, "legal": 1095}

# Severity when a page type that is expected to carry a date carries none.
MISSING_DATE_SEVERITY = {"article": "high", "docs": "medium", "legal": "low"}

JSONLD_PUBLISHED_KEYS = ("datePublished", "dateCreated", "uploadDate", "startDate")
JSONLD_MODIFIED_KEYS = ("dateModified", "lastReviewed")

META_PUBLISHED_KEYS = (
    "article:published_time", "og:published_time", "publish_date", "pubdate",
    "date", "dc.date", "dcterms.date", "dcterms.created", "citation_publication_date",
    "sailthru.date", "parsely-pub-date", "article:published",
)
META_MODIFIED_KEYS = (
    "article:modified_time", "og:updated_time", "last-modified", "dcterms.modified",
    "revised", "parsely-update-date",
)

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
TEXT_DATE_RE = re.compile(
    rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}\b"
    rf"|\b\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+(?:19|20)\d{{2}}\b"
    rf"|\b(?:19|20)\d{{2}}-\d{{2}}-\d{{2}}\b"
    rf"|\b\d{{1,2}}/\d{{1,2}}/(?:19|20)\d{{2}}\b",
    re.I,
)

UPDATE_LANGUAGE_RE = re.compile(
    r"\b(last updated|updated on|updated:|revised|last reviewed|reviewed on|"
    r"changelog|revision history|what'?s new|edited on|version \d)\b", re.I,
)

# Claims a third party could in principle confirm or contradict.
STATISTIC_RE = re.compile(
    r"(\b\d{1,3}(?:\.\d+)?\s?%|\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s?"
    r"(?:million|billion|thousand|k\+|m\+)\b|\b\d+(?:\.\d+)?x\b|"
    r"\b\d{2,}\s?\+?\s?(?:customers|clients|users|companies|countries|employees|"
    r"installs|downloads|projects|partners)\b)", re.I,
)
SUPERLATIVE_RE = re.compile(
    r"\b(#\s?1\b|number one|the (?:leading|largest|fastest|best|most trusted|"
    r"most popular|top)\b|world'?s (?:best|leading|largest|first)|"
    r"industry[- ]leading|award[- ]winning|market leader|best[- ]in[- ]class|"
    r"the only \w+ that)\b", re.I,
)

# Host families, used to tell corroboration apart from self-promotion.
HOST_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("encyclopedia", ("wikipedia.org", "wikidata.org", "britannica.com", "wikimedia.org")),
    ("academic_publishing", ("doi.org", "arxiv.org", "springer.com", "nature.com",
                             "sciencedirect.com", "jstor.org", "acm.org", "ieee.org",
                             "ssrn.com", "biorxiv.org", "tandfonline.com", "wiley.com")),
    ("academic", (".edu", ".ac.uk", ".edu.au", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov")),
    ("government", (".gov", ".gov.uk", ".mil", "europa.eu", "who.int", "un.org", "oecd.org")),
    ("standards", ("w3.org", "ietf.org", "iso.org", "nist.gov", "unicode.org", "rfc-editor.org")),
    ("news_media", ("nytimes.com", "bbc.co.uk", "bbc.com", "reuters.com", "forbes.com",
                    "bloomberg.com", "wsj.com", "theguardian.com", "cnbc.com", "wired.com",
                    "techcrunch.com", "theverge.com", "venturebeat.com", "ft.com",
                    "businessinsider.com", "axios.com", "cnn.com")),
    ("press_wire", ("prnewswire.com", "businesswire.com", "globenewswire.com",
                    "einpresswire.com", "prweb.com", "newswire.com")),
    ("social", ("twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
                "youtube.com", "tiktok.com", "pinterest.com", "reddit.com", "threads.net")),
    ("review_platform", ("g2.com", "capterra.com", "trustpilot.com", "gartner.com",
                         "getapp.com", "producthunt.com", "yelp.com")),
]

# Categories that actually corroborate a factual claim, as opposed to
# amplifying it. Derived from the SS8.4 absorption ordering.
CORROBORATING_CATEGORIES = {
    "encyclopedia", "academic_publishing", "academic", "government", "standards",
}


@dataclass
class Page:
    url: str
    page_type: str
    doc: HtmlDoc
    html: str
    entry: dict = field(default_factory=dict)
    published: datetime | None = None
    modified: datetime | None = None
    date_sources: list[str] = field(default_factory=list)
    text_dates: list[str] = field(default_factory=list)
    external_hosts: list[str] = field(default_factory=list)
    claims: list[tuple[str, str]] = field(default_factory=list)  # (kind, sentence)


# ---------------------------------------------------------------------------
# Workspace loading
# ---------------------------------------------------------------------------

def _resolve_html_path(ws: Workspace, raw: str) -> pathlib.Path | None:
    if not raw:
        return None
    candidate = pathlib.Path(raw)
    options = [candidate] if candidate.is_absolute() else [
        ws.root / raw, ws.root / "pages" / raw, ws.root / "html" / raw,
    ]
    for option in options:
        try:
            if option.is_file():
                return option
        except OSError:
            continue
    return None


def load_inventory(ws: Workspace) -> tuple[list[dict], str]:
    payload = ws.require("reachability")
    entries = [e for e in (payload.get("pages") or []) if isinstance(e, dict)]
    site = payload.get("site") or payload.get("origin") or payload.get("scope_domain") or ""
    if not site:
        preflight = ws.read("preflight") or {}
        site = preflight.get("site") or preflight.get("url") or preflight.get("origin") or ""
    if not site and entries:
        site = entries[0].get("final_url") or entries[0].get("url") or ""
    return entries, (urlsplit(site).hostname or site or "").lower().strip("/")


def load_pages(ws: Workspace, scope_domain: str) -> tuple[list[Page], list[str]]:
    entries, _ = load_inventory(ws)
    pages: list[Page] = []
    notes: list[str] = []
    missing: list[str] = []
    non_ok: list[str] = []

    for entry in entries:
        url = entry.get("final_url") or entry.get("url") or ""
        if not url:
            continue
        status = entry.get("status")
        if isinstance(status, int) and not (200 <= status < 300):
            non_ok.append(f"{url} (HTTP {status})")
            continue
        path = _resolve_html_path(ws, entry.get("html_path") or entry.get("html") or "")
        if path is None:
            missing.append(url)
            continue
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            missing.append(f"{url} ({exc})")
            continue
        pages.append(Page(
            url=url,
            page_type=entry.get("page_type") or "other",
            doc=parse_html(html, url=url, scope_host=scope_domain),
            html=html,
            entry=entry,
        ))

    if missing:
        notes.append(
            f"{len(missing)} inventory entr{'y' if len(missing) == 1 else 'ies'} had no "
            "readable saved HTML in the workspace and were skipped: "
            + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        )
    if non_ok:
        notes.append(
            f"{len(non_ok)} page(s) were not retrieved successfully upstream and are "
            "excluded from trust-layer analysis: " + ", ".join(non_ok[:5])
            + (" ..." if len(non_ok) > 5 else "")
        )
    return pages, notes


# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------

def parse_date(raw: str) -> datetime | None:
    """Parse the datestamp formats that actually appear in the wild."""
    text = (raw or "").strip()
    if not text or len(text) < 4:
        return None
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y",
                "%b %d, %Y", "%B %d %Y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m", "%Y",
                "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def collect_dates(page: Page) -> None:
    """Fill in published/modified/date_sources from every machine-readable source."""
    doc = page.doc
    published: list[tuple[datetime, str]] = []
    modified: list[tuple[datetime, str]] = []

    for node in iter_jsonld_nodes(doc.json_ld):
        for key in JSONLD_PUBLISHED_KEYS:
            value = node.get(key)
            parsed = parse_date(value) if isinstance(value, str) else None
            if parsed:
                published.append((parsed, f"json-ld {key}"))
        for key in JSONLD_MODIFIED_KEYS:
            value = node.get(key)
            parsed = parse_date(value) if isinstance(value, str) else None
            if parsed:
                modified.append((parsed, f"json-ld {key}"))

    for key in META_PUBLISHED_KEYS:
        parsed = parse_date(doc.meta.get(key, ""))
        if parsed:
            published.append((parsed, f"meta {key}"))
    for key in META_MODIFIED_KEYS:
        parsed = parse_date(doc.meta.get(key, ""))
        if parsed:
            modified.append((parsed, f"meta {key}"))

    for stamp in doc.time_elements:
        parsed = parse_date(stamp)
        if parsed:
            published.append((parsed, "<time datetime>"))

    page.published = min((d for d, _ in published), default=None)
    page.modified = max((d for d, _ in modified), default=None)
    page.date_sources = sorted({src for _, src in published + modified})
    page.text_dates = [m.group(0) for m in TEXT_DATE_RE.finditer(doc.main_text or doc.text)][:5]


def latest_date(page: Page) -> datetime | None:
    stamps = [d for d in (page.published, page.modified) if d]
    return max(stamps) if stamps else None


def header_last_modified(page: Page) -> str:
    headers = page.entry.get("headers") or {}
    if isinstance(headers, dict):
        for key, value in headers.items():
            if key.lower() == "last-modified" and value:
                return str(value)
    return ""


# ---------------------------------------------------------------------------
# Corroboration handling
# ---------------------------------------------------------------------------

def categorise_host(host: str) -> str:
    host = host.lower().removeprefix("www.")
    for name, patterns in HOST_CATEGORIES:
        for pattern in patterns:
            if host == pattern.lstrip(".") or host.endswith(pattern):
                return name
    return "other"


def collect_links_and_claims(page: Page, scope_domain: str) -> None:
    hosts: list[str] = []
    for link in page.doc.external_links:
        host = (urlsplit(link.href).hostname or "").lower().removeprefix("www.")
        if not host or (scope_domain and (host == scope_domain or host.endswith("." + scope_domain))):
            continue
        hosts.append(host)
    page.external_hosts = hosts

    text = page.doc.main_text or page.doc.text
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        sentence = sentence.strip()
        if not (25 <= len(sentence) <= 400):
            continue
        if STATISTIC_RE.search(sentence):
            page.claims.append(("statistic", sentence))
        elif SUPERLATIVE_RE.search(sentence):
            page.claims.append(("superlative", sentence))
    page.claims = page.claims[:12]


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------

def with_urls(text: str, urls: list[str], limit: int = 3) -> str:
    shown = [u for u in urls if u][:limit]
    if not shown:
        return text
    more = f" and {len(urls) - len(shown)} more" if len(urls) > len(shown) else ""
    return f"{text} (e.g. {', '.join(shown)}{more})"


def sample_caveat(config: AuditConfig, n: int, label: str) -> str:
    return (
        f" Sample of {n} {label} is below the population-claim threshold of "
        f"{config.min_sample_for_population_claim}, so this is stated as a "
        "page-level observation rather than a site-wide claim."
    )


def snippet(text: str, limit: int = 150) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_freshness(pages: list[Page], config: AuditConfig,
                    scope_domain: str) -> tuple[list[Finding], dict, list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    now = datetime.now(timezone.utc)

    for page in pages:
        collect_dates(page)
        collect_links_and_claims(page, scope_domain)

    by_type: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_type[page.page_type].append(page)

    dated = [p for p in pages if latest_date(p)]
    ages = sorted(int((now - latest_date(p)).days) for p in dated)
    host_categories = Counter(
        categorise_host(h) for p in pages for h in set(p.external_hosts)
    )

    metrics = {
        "pages_analysed": len(pages),
        "pages_with_machine_readable_date": len(dated),
        "date_coverage": round(len(dated) / max(1, len(pages)), 3),
        "date_sources_seen": dict(Counter(s for p in pages for s in p.date_sources).most_common()),
        "median_content_age_days": ages[len(ages) // 2] if ages else None,
        "oldest_content_age_days": ages[-1] if ages else None,
        "newest_content_age_days": ages[0] if ages else None,
        "pages_with_external_links": sum(1 for p in pages if p.external_hosts),
        "distinct_external_hosts": len({h for p in pages for h in p.external_hosts}),
        "external_host_categories": dict(host_categories),
        "pages_with_citation_language": sum(
            1 for p in pages if CITATION_MARKERS.search(p.doc.main_text or p.doc.text)
        ),
        "pages_with_verifiable_claims": sum(1 for p in pages if p.claims),
        "domain_type_influence_reference": DOMAIN_TYPE_INFLUENCE,
    }

    # -- FR001 / FR002: datestamp presence and machine-readability --------
    for page_type, group in sorted(by_type.items()):
        expectation = config.expectations_for(page_type)
        if not expectation.get("expect_date"):
            continue  # a pricing or about page has no natural datestamp obligation
        undated = [p for p in group if not latest_date(p)]
        if not undated:
            continue
        human_only = [p for p in undated if p.text_dates]
        machine_absent = [p for p in undated if not p.text_dates]
        sufficient = config.sample_is_sufficient(len(group))
        base_severity = MISSING_DATE_SEVERITY.get(page_type, "low")

        if machine_absent:
            header_backed = [p for p in machine_absent if header_last_modified(p)]
            evidence = with_urls(
                f"{len(machine_absent)} of {len(group)} sampled {page_type} pages carry no "
                f"date at all - no <time datetime>, no JSON-LD datePublished/dateModified "
                f"and no article:published_time meta tag"
                + (f"; {len(header_backed)} of them expose only an HTTP Last-Modified header, "
                   "which a reader never sees" if header_backed else ""),
                [p.url for p in machine_absent],
            )
            if not sufficient:
                evidence += sample_caveat(config, len(group), f"{page_type} pages")
            findings.append(Finding(
                title=f"{page_type.capitalize()} pages carry no datestamp",
                severity=base_severity if sufficient else "low",
                evidence=evidence,
                layer=LAYER,
                claim_level=1,
                confidence="high" if sufficient else "medium",
                check_id="FR001",
                observed=f"{len(machine_absent)}/{len(group)} {page_type} pages with zero date signal",
                expected="a visible date plus a machine-readable equivalent",
                affected_urls=[p.url for p in machine_absent],
                sample_size=len(group),
                suggested_action=SuggestedAction(
                    summary=f"Add published and modified dates to the {page_type} template",
                    priority="high" if base_severity == "high" else "medium",
                    effort="low",
                    steps=[
                        "Render the date visibly as <time datetime=\"YYYY-MM-DD\">.",
                        "Mirror it in JSON-LD as datePublished, and dateModified when the page is edited.",
                        "Drive both from the CMS field so they cannot disagree.",
                    ],
                    mechanism=(
                        "An undated claim cannot be placed in time, so a reader (human or "
                        "machine) cannot tell whether it has been superseded. Handout "
                        "appendix D: corroboration requires knowing what is being compared "
                        "against what, and when."
                    ),
                    verification="date_coverage for this page type reaches 1.0 on a re-run.",
                ),
            ))

        if human_only:
            findings.append(Finding(
                title=f"{page_type.capitalize()} pages show a date only as prose",
                severity="medium" if sufficient else "low",
                evidence=with_urls(
                    f"{len(human_only)} of {len(group)} sampled {page_type} pages print a date in "
                    f"the body text (e.g. \"{human_only[0].text_dates[0]}\") but expose no "
                    f"machine-readable equivalent in <time datetime>, JSON-LD or meta tags",
                    [p.url for p in human_only],
                ),
                layer=LAYER,
                claim_level=1,
                confidence="high",
                check_id="FR002",
                observed=f"visible date '{human_only[0].text_dates[0]}' with no machine-readable stamp",
                expected="the same date in <time datetime> or JSON-LD datePublished",
                affected_urls=[p.url for p in human_only],
                sample_size=len(group),
                suggested_action=SuggestedAction(
                    summary="Wrap the printed date in <time datetime> and mirror it in JSON-LD",
                    priority="medium",
                    effort="low",
                    steps=[
                        "Change the rendered date to <time datetime=\"YYYY-MM-DD\">the same text</time>.",
                        "Add datePublished/dateModified to the page's JSON-LD node.",
                    ],
                    mechanism=(
                        "A prose date is ambiguous across locales (03/04/2025) and is not "
                        "reliably extracted. The information already exists on the page; only "
                        "its format is blocking it."
                    ),
                    verification="Re-run this check; FR002 should not report this page type.",
                ),
            ))

    # -- FR003: staleness relative to page type ---------------------------
    for page_type, limit_days in STALE_DAYS.items():
        group = by_type.get(page_type) or []
        stale = [
            p for p in group
            if latest_date(p) and (now - latest_date(p)).days > limit_days
        ]
        if not stale:
            continue
        oldest = max(stale, key=lambda p: (now - latest_date(p)).days)
        oldest_days = (now - latest_date(oldest)).days
        sufficient = config.sample_is_sufficient(len(group))
        evidence = with_urls(
            f"{len(stale)} of {len(group)} sampled {page_type} pages carry a most-recent "
            f"datestamp older than {limit_days} days; the oldest is "
            f"{latest_date(oldest).date().isoformat()} ({oldest_days} days ago)",
            [p.url for p in stale],
        )
        if not sufficient:
            evidence += sample_caveat(config, len(group), f"{page_type} pages")
        findings.append(Finding(
            title=f"{page_type.capitalize()} content has not been dated as reviewed in over {limit_days // 365 or 1} year(s)",
            severity="medium",
            evidence=evidence,
            layer=LAYER,
            claim_level=1,
            confidence="high" if sufficient else "medium",
            check_id="FR003",
            observed=f"oldest datestamp {latest_date(oldest).date().isoformat()} ({oldest_days} days)",
            expected=f"{page_type} content reviewed or re-dated inside {limit_days} days",
            affected_urls=[p.url for p in stale],
            sample_size=len(group),
            suggested_action=SuggestedAction(
                summary=f"Review the stale {page_type} pages and re-date the ones that are still correct",
                priority="medium",
                effort="medium",
                steps=[
                    "Review each listed page for statements that are no longer true.",
                    "Where the content still holds, record a dateModified and say what was reviewed.",
                    "Where it does not, update it or redirect it to the page that supersedes it.",
                    "Do not bulk-touch dateModified without changing anything - see FR004.",
                ],
                mechanism=(
                    "Age is not itself an error; an undated old page is the problem, because "
                    "nothing distinguishes 'still true' from 'never revisited'."
                ),
                verification="Each listed URL carries a dateModified inside the window.",
            ),
        ))

    # -- FR004: fake freshness --------------------------------------------
    suspicious_gap = []
    for page in pages:
        if not (page.published and page.modified):
            continue
        gap_days = (page.modified - page.published).days
        text = page.doc.main_text or page.doc.text
        if gap_days > 365 and not UPDATE_LANGUAGE_RE.search(text):
            suspicious_gap.append((page, gap_days))

    if suspicious_gap:
        page, gap_days = suspicious_gap[0]
        findings.append(Finding(
            title="dateModified is far newer than datePublished with no visible revision",
            severity="medium",
            evidence=with_urls(
                f"{len(suspicious_gap)} page(s) declare dateModified "
                f"{gap_days} days after datePublished (e.g. published "
                f"{page.published.date().isoformat()}, modified "
                f"{page.modified.date().isoformat()}) while the body text contains no "
                f"update, revision or changelog language a reader could check",
                [p.url for p, _ in suspicious_gap],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="FR004a",
            observed=f"published {page.published.date().isoformat()}, modified {page.modified.date().isoformat()}",
            expected="a modification date accompanied by a visible statement of what changed",
            affected_urls=[p.url for p, _ in suspicious_gap],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Say what changed when you bump dateModified",
                priority="medium",
                effort="low",
                steps=[
                    "Add a short 'Updated <date>: <what changed>' line where the page is edited.",
                    "Only advance dateModified when the content actually changed.",
                ],
                mechanism=(
                    "This is an interpretation, not a proven defect - the edit may have been "
                    "real and invisible. It is worth checking because a modification date "
                    "with no corresponding change is a freshness signal that cannot be "
                    "corroborated from the page itself."
                ),
                verification="Each updated page states what was revised.",
            ),
        ))

    modified_days = Counter(
        p.modified.date().isoformat() for p in pages if p.modified
    )
    for day, count in modified_days.items():
        if count < 3:
            continue
        cohort = [p for p in pages if p.modified and p.modified.date().isoformat() == day]
        published_years = {p.published.year for p in cohort if p.published}
        if len(published_years) < 2:
            continue
        findings.append(Finding(
            title="Many pages share one dateModified despite unrelated publication dates",
            severity="medium",
            evidence=with_urls(
                f"{count} sampled pages all declare dateModified {day} while their "
                f"datePublished values span {min(published_years)}-{max(published_years)}; "
                f"a single shared modification date across unrelated content is the shape of "
                f"a template or migration touch rather than {count} separate edits",
                [p.url for p in cohort],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="FR004b",
            observed=f"{count} pages with dateModified {day}, datePublished spanning {min(published_years)}-{max(published_years)}",
            expected="modification dates that track actual per-page edits",
            affected_urls=[p.url for p in cohort],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Stop emitting a site-wide dateModified; derive it per page",
                priority="medium",
                effort="medium",
                steps=[
                    "Bind dateModified to the CMS record's own last-edited timestamp.",
                    "If a migration touched every record, reset dateModified to the real content date.",
                ],
                mechanism=(
                    "Interpretation, hedged accordingly: a uniform modification date carries "
                    "no per-page information, so it cannot support the freshness claim it "
                    "appears to make."
                ),
                verification="dateModified values differ per page on a re-run.",
            ),
        ))

    # -- FR005: load-bearing claims with nothing behind them ---------------
    # Prices are excluded on purpose: a brand's own price is a first-party fact
    # and needs no external source. Only third-party-checkable statistics and
    # superlatives count here.
    uncorroborated = []
    for page in pages:
        expectation = config.expectations_for(page.page_type)
        if expectation.get("exempt_from_absorption") or expectation.get("is_hub"):
            continue
        if len(page.claims) < 3:
            continue
        text = page.doc.main_text or page.doc.text
        if CITATION_MARKERS.search(text):
            continue
        if any(categorise_host(h) != "social" for h in page.external_hosts):
            continue
        uncorroborated.append(page)

    if uncorroborated:
        sample_page = uncorroborated[0]
        kind, sentence = sample_page.claims[0]
        sufficient = config.sample_is_sufficient(len(pages))
        evidence = with_urls(
            f"{len(uncorroborated)} of {len(pages)} sampled pages assert three or more "
            f"third-party-checkable claims with no citation language and no outbound link to "
            f"any source - for example the {kind} claim \"{snippet(sentence)}\"",
            [p.url for p in uncorroborated],
        )
        if not sufficient:
            evidence += sample_caveat(config, len(pages), "sampled pages")
        findings.append(Finding(
            title="Statistics and superlatives are asserted with no source",
            severity="medium",
            evidence=evidence,
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="FR005",
            observed=f"{sum(len(p.claims) for p in uncorroborated)} unsourced claims across {len(uncorroborated)} pages",
            expected="each load-bearing statistic names its basis, date and method",
            affected_urls=[p.url for p in uncorroborated],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Attach a source, a date and a method to every number you want quoted",
                priority="medium",
                effort="medium",
                steps=[
                    "For each statistic, state where it came from and as of when ('4,812 active customers as of 2026-06-30, internal billing data').",
                    "Link to the external study when the number is not yours.",
                    "Replace unverifiable superlatives with the specific fact that supports them.",
                ],
                mechanism=(
                    "Mechanism interpretation, hedged: an unsourced number is quotable but "
                    "not checkable, and handout appendix D treats agreement across sources as "
                    "what makes a claim usable. A sourced number can be corroborated; a bare "
                    "one can only be repeated."
                ),
                verification="Each listed page names a source and an as-of date for its headline numbers.",
            ),
        ))

    # -- FR006: outbound citation density on citation-conventional types ---
    citing_types = [t for t in ("article", "docs") if by_type.get(t)]
    citation_group = [p for t in citing_types for p in by_type[t]]
    if citation_group:
        no_outbound = [p for p in citation_group if not p.external_hosts]
        if no_outbound and len(no_outbound) == len(citation_group):
            sufficient = config.sample_is_sufficient(len(citation_group))
            evidence = with_urls(
                f"all {len(citation_group)} sampled {'/'.join(citing_types)} pages carry zero "
                f"outbound links to any external domain; the site's explanatory content "
                f"references nothing outside itself",
                [p.url for p in no_outbound],
            )
            if not sufficient:
                evidence += sample_caveat(config, len(citation_group), "explanatory pages")
            findings.append(Finding(
                title="Explanatory pages contain no outbound references",
                severity="medium" if sufficient else "low",
                evidence=evidence,
                layer=LAYER,
                claim_level=1,
                confidence="high" if sufficient else "medium",
                check_id="FR006",
                observed=f"0 external links across {len(citation_group)} explanatory pages",
                expected="references to the standards, studies or documentation the content relies on",
                affected_urls=[p.url for p in no_outbound],
                sample_size=len(citation_group),
                suggested_action=SuggestedAction(
                    summary="Link the sources your explanatory content already relies on",
                    priority="medium",
                    effort="low",
                    steps=[
                        "Where a page states an external fact, link the standard, spec, study or dataset behind it.",
                        "Prefer primary sources over aggregators.",
                    ],
                    mechanism=(
                        "Outbound references are the observable half of corroboration: they "
                        "place the page inside a web of claims that agree, rather than in "
                        "isolation."
                    ),
                    verification="Explanatory pages link at least one external source each.",
                ),
            ))

    # -- FR007 / FR008: what kind of corroboration exists ------------------
    claiming_pages = [p for p in pages if p.claims]
    all_categories = {categorise_host(h) for p in pages for h in p.external_hosts}
    corroborating = all_categories & CORROBORATING_CATEGORIES
    amplifying = all_categories & {"news_media", "press_wire", "social", "review_platform"}

    if claiming_pages and config.sample_is_sufficient(len(pages)) and not all_categories:
        findings.append(Finding(
            title="Every factual claim on the site is single-sourced to the site itself",
            severity="medium",
            evidence=with_urls(
                f"{len(claiming_pages)} of {len(pages)} sampled pages make third-party-checkable "
                f"claims, and the whole sample contains 0 outbound links to any external host. "
                f"Nothing on the site can be cross-checked against anything else",
                [p.url for p in claiming_pages],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="FR007",
            observed=f"0 external hosts referenced across {len(pages)} pages",
            expected="the brand's key facts appear on, and link to, at least one independent source",
            affected_urls=[p.url for p in claiming_pages],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Create at least one independently checkable anchor for the brand's core facts",
                priority="medium",
                effort="high",
                steps=[
                    "Own the explanatory page for the concept the brand is associated with, and cite primary sources from it.",
                    "Ensure the brand's basic facts (founding, location, category, leadership) exist on an independent reference source, not only on the site.",
                    "Link out to those sources so the agreement is observable.",
                ],
                mechanism=(
                    "Interpretation, hedged: a self-referential site offers a reader no way to "
                    "corroborate a claim. Handout appendix D treats cross-web agreement as the "
                    "trust signal; a closed loop produces none."
                ),
                verification="Key facts are reachable from at least one independent host.",
            ),
        ))

    if claiming_pages and amplifying and not corroborating:
        findings.append(Finding(
            title="Corroboration rests on press and social coverage only",
            severity="medium",
            evidence=with_urls(
                f"the sample's outbound links reach {', '.join(sorted(amplifying))} hosts "
                f"({metrics['distinct_external_hosts']} distinct external hosts overall) and no "
                f"encyclopedia, standards, government or academic source, while "
                f"{len(claiming_pages)} pages make third-party-checkable claims",
                [p.url for p in claiming_pages],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="FR008",
            observed=f"external host categories: {', '.join(sorted(all_categories))}",
            expected="at least one reference-class source alongside the coverage",
            affected_urls=[p.url for p in claiming_pages],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Own the explanatory page; treat press coverage as amplification, not corroboration",
                priority="medium",
                effort="high",
                steps=[
                    "Publish the definitive explanation of the concept on your own domain, with sources.",
                    "Get the brand's factual record onto reference-class sources (encyclopedia, registries, standards bodies, published research).",
                    "Keep pitching press - just do not count it as the corroboration layer.",
                ],
                mechanism=(
                    "arXiv:2604.25707 SS8.4 measures mean influence by publisher domain type: "
                    "news_media 0.0726 is the LOWEST of the seven categories, below commercial "
                    "0.1028, while encyclopedia is 0.2144. News media is selected often and "
                    "absorbed weakly, so a wall of coverage can raise citation counts while "
                    "contributing little to what an answer actually says. This is a mechanism "
                    "interpretation, not a prediction about this site."
                ),
                verification="At least one reference-class host carries and corroborates the brand's core facts.",
            ),
        ))

    return findings, metrics, notes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def drop_unevidenced(findings: list[Finding], notes: list[str]) -> list[Finding]:
    kept, dropped = [], []
    for finding in findings:
        if "http" in finding.evidence:
            kept.append(finding)
        else:
            dropped.append(finding.check_id or finding.title)
    if dropped:
        notes.append(
            "dropped before emit for carrying no URL in the evidence string: " + ", ".join(dropped)
        )
    return kept


def human_summary(payload: dict) -> str:
    metrics = payload["metrics"]
    lines = [
        f"freshness-corroboration  layer={LAYER}",
        f"  pages analysed   : {payload['pages_analysed']}",
        f"  dated pages      : {metrics.get('pages_with_machine_readable_date', 0)}"
        f"/{payload['pages_analysed']} (coverage {metrics.get('date_coverage', 0)})",
        f"  median age (days): {metrics.get('median_content_age_days')}",
        f"  external hosts   : {metrics.get('distinct_external_hosts', 0)} "
        f"{sorted(metrics.get('external_host_categories', {}))}",
        f"  findings         : {len(payload['findings'])}",
    ]
    for finding in payload["findings"]:
        lines.append(
            f"    [{finding['severity']:<8}] {finding.get('check_id', '-'):<7} "
            f"L{finding['claim_level']} {finding['title']}"
        )
        lines.append(f"             {finding['evidence'][:220]}")
    for note in payload["notes"]:
        lines.append(f"  note: {note}")
    if not payload["findings"]:
        lines.append("    no trust-layer defects detected in the sampled pages")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_freshness.py",
        description="Trust-layer audit: datestamps, staleness and corroboration. "
                    "Reads saved HTML from the audit workspace; never fetches.",
    )
    parser.add_argument("--workspace", default=None,
                        help="path to an audit workspace directory (default: most recent run under ./.audit)")
    parser.add_argument("--json", action="store_true",
                        help="print the layer payload as JSON instead of a human summary")
    parser.add_argument("--config", default=None, help="optional AuditConfig JSON file")
    parser.add_argument("--strictness", choices=("lenient", "balanced", "strict"), default=None)
    args = parser.parse_args(argv)

    try:
        ws = Workspace.open(args.workspace) if args.workspace else Workspace.latest()
        config = AuditConfig.load(args.config, strictness=args.strictness)

        _, site_host = load_inventory(ws)
        scope_domain = registrable_domain(site_host) if site_host else ""
        pages, notes = load_pages(ws, scope_domain or site_host)

        if pages:
            config.calibrate_to_site([p.doc.structure_summary() for p in pages])
            findings, metrics, check_notes = check_freshness(pages, config, scope_domain)
            notes.extend(check_notes)
        else:
            findings, metrics = [], {"pages_analysed": 0}
            notes.append(
                "no page in the reachability inventory had readable saved HTML; "
                "the trust layer made no observations"
            )

        findings = drop_unevidenced(findings, notes)
        payload = {
            "layer": LAYER,
            "skill": "freshness-corroboration",
            "site": site_host,
            "scope_domain": scope_domain,
            "findings": [f.to_dict() for f in findings],
            "metrics": metrics,
            "pages_analysed": len(pages),
            "notes": notes,
            "thresholds": {
                "stale_days_by_page_type": STALE_DAYS,
                "min_sample_for_population_claim": config.min_sample_for_population_claim,
            },
        }
        ws.write(LAYER, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"check_freshness: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else human_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
