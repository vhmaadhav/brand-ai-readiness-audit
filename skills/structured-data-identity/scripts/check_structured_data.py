#!/usr/bin/env python3
"""Selection-layer detector: structured data and entity identity.

Reads the page inventory written by the reachability layer, re-parses the saved
HTML from disk (never re-crawls), and reports whether an answer engine could
resolve *who this brand is* and *what each page is about*.

Grounding: arXiv:2604.25707 Fig. 3 / SS7 reports official sources at
34.22-46.35% of all AI citations across platforms. Identity resolution is an
entry condition to the candidate pool, not a ranking tweak: a page whose
publisher cannot be resolved to a known entity is competing for the remaining
share.
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
from urllib.parse import urlsplit  # noqa: E402

from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.htmldoc import HtmlDoc, iter_jsonld_nodes, jsonld_types, parse_html  # noqa: E402
from geo_audit.netguard import registrable_domain  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.workspace import Workspace  # noqa: E402

LAYER = "selection"

# Types that every templated page carries. Their presence does not tell an
# engine what *this* page is about, so they never satisfy a page-type
# expectation on their own.
GENERIC_TYPES = {
    "WebPage", "WebSite", "BreadcrumbList", "SearchAction", "SiteNavigationElement",
    "ImageObject", "Person", "Thing", "Graph", "ListItem", "EntryPoint",
    "ReadAction", "PostalAddress", "ContactPoint", "Offer", "AggregateRating",
    "OpeningHoursSpecification", "GeoCoordinates", "Rating", "Review",
}

# Page types whose schema expectation is specific enough to enforce. "other"
# and "legal" are deliberately absent: "other" is a catch-all bucket from URL
# inference and "legal" is exempt by design in PAGE_TYPE_EXPECTATIONS.
ENFORCED_PAGE_TYPES = {
    "homepage", "product", "pricing", "article", "docs", "about", "service", "category",
}

ORGANIZATION_TYPES = {
    "Organization", "Corporation", "LocalBusiness", "OnlineBusiness", "NGO",
    "EducationalOrganization", "GovernmentOrganization", "Store", "Restaurant",
    "MedicalBusiness", "ProfessionalService", "SportsOrganization", "NewsMediaOrganization",
}

# One page should describe one primary thing. Two different primary categories
# on the same document is a genuine conflict; Organization + WebSite +
# BreadcrumbList alongside a Product is normal templating and is NOT a conflict.
PRIMARY_TYPE_GROUPS = {
    "product": {"Product", "ProductGroup", "IndividualProduct", "SoftwareApplication", "Vehicle", "Book"},
    "article": {"Article", "BlogPosting", "NewsArticle", "TechArticle", "ScholarlyArticle", "Report"},
    "howto": {"HowTo", "Recipe"},
    "event": {"Event", "BusinessEvent", "EducationEvent"},
    "job": {"JobPosting"},
    "course": {"Course"},
    "faq": {"FAQPage", "QAPage"},
}

# Required / strongly-expected properties per type. Kept deliberately short:
# only the properties a consumer needs to make the node usable at all. The
# long-form rationale lives in references/schema-requirements.md.
REQUIRED_PROPERTIES: dict[str, list[tuple[str, list[str]]]] = {
    "Product": [("name", ["name"]), ("offers or price", ["offers", "price", "aggregateOffer"])],
    "ProductGroup": [("name", ["name"]), ("offers or price", ["offers", "price", "hasVariant"])],
    "SoftwareApplication": [("name", ["name"]), ("offers or applicationCategory", ["offers", "applicationCategory"])],
    "Article": [("headline", ["headline", "name"]), ("datePublished", ["datePublished", "dateCreated"]), ("author", ["author", "creator"])],
    "BlogPosting": [("headline", ["headline", "name"]), ("datePublished", ["datePublished", "dateCreated"]), ("author", ["author", "creator"])],
    "NewsArticle": [("headline", ["headline", "name"]), ("datePublished", ["datePublished", "dateCreated"]), ("author", ["author", "creator"])],
    "TechArticle": [("headline", ["headline", "name"]), ("datePublished", ["datePublished", "dateCreated"])],
    "LocalBusiness": [("name", ["name"]), ("address", ["address"]), ("telephone or contactPoint", ["telephone", "contactPoint"])],
    "Event": [("name", ["name"]), ("startDate", ["startDate"]), ("location", ["location"])],
    "JobPosting": [("title", ["title", "name"]), ("datePosted", ["datePosted"]), ("hiringOrganization", ["hiringOrganization"])],
    "Recipe": [("name", ["name"]), ("recipeIngredient", ["recipeIngredient"]), ("recipeInstructions", ["recipeInstructions"])],
    "HowTo": [("name", ["name"]), ("step", ["step"])],
    "FAQPage": [("mainEntity", ["mainEntity"])],
    "BreadcrumbList": [("itemListElement", ["itemListElement"])],
    "Course": [("name", ["name"]), ("description", ["description"]), ("provider", ["provider"])],
}

ORGANIZATION_FIELDS = [
    ("name", ["name", "legalName"]),
    ("url", ["url"]),
    ("logo", ["logo", "image"]),
    ("description", ["description"]),
    ("contact point", ["contactPoint", "telephone", "email", "address"]),
]

# Brand tokens that are also ordinary English words or heavily reused across
# unrelated companies. A brand whose name is a common noun needs more
# disambiguation signal, not less. This list is a *trigger* for a hedged
# Level 3 finding, never a claim that the name is bad.
AMBIGUOUS_BRAND_TOKENS = {
    "acme", "align", "alpha", "amber", "anchor", "apex", "arc", "arcade", "arch",
    "arrow", "ascend", "aspect", "aspire", "atlas", "aurora", "axis", "beacon",
    "bell", "bloom", "blue", "bolt", "boost", "branch", "bridge", "bright",
    "canvas", "cardinal", "cascade", "catalyst", "cedar", "circle", "clarity",
    "cloud", "coast", "compass", "core", "crest", "crown", "current", "cypress",
    "delta", "drift", "eagle", "echo", "edge", "element", "elevate", "ember",
    "epic", "essence", "everest", "evergreen", "flow", "flux", "focus", "forge",
    "fountain", "fox", "frame", "fusion", "gateway", "glow", "grove", "harbor",
    "harbour", "haven", "helix", "horizon", "hub", "impact", "impulse", "insight",
    "iris", "iron", "ivory", "jade", "keystone", "kite", "lantern", "leaf",
    "legacy", "lever", "liberty", "lift", "light", "lighthouse", "link", "lumen",
    "luna", "magnet", "maple", "meridian", "mesa", "monarch", "moss", "motion",
    "nexus", "nimbus", "north", "nova", "oak", "oasis", "onyx", "orbit", "origin",
    "outpost", "oxide", "pace", "paragon", "peak", "pearl", "pilot", "pine",
    "pinnacle", "pivot", "plane", "prime", "prism", "pulse", "quarry", "quartz",
    "quest", "radiant", "rail", "range", "rapid", "reach", "relay", "ridge",
    "rise", "river", "rocket", "root", "sage", "sail", "scale", "scope", "seed",
    "shift", "shore", "signal", "silver", "slate", "solid", "source", "spark",
    "sphere", "spire", "spring", "sprout", "stack", "stone", "storm", "stream",
    "summit", "sun", "surge", "swift", "table", "tempo", "terra", "thread",
    "tide", "torch", "trace", "track", "trail", "trust", "unity", "vault",
    "vector", "velocity", "venture", "vertex", "vista", "vital", "wave", "willow",
    "wing", "zenith", "zephyr",
}

GENERIC_BRAND_SUFFIXES = re.compile(
    r"(group|solutions|services|systems|digital|media|labs?|tech|technologies|"
    r"consulting|partners|holdings|ventures|studio|agency|works|global|online)$",
    re.I,
)

_PHONE_DIGITS = re.compile(r"\D+")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Workspace loading (read-only; never re-crawls)
# ---------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    page_type: str
    doc: HtmlDoc
    html: str
    status: int | None = None
    nodes: list[dict] = field(default_factory=list)
    types: set[str] = field(default_factory=set)
    top_types: set[str] = field(default_factory=set)


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


def load_inventory(ws: Workspace) -> tuple[list[dict], str, list[str]]:
    """Return (page entries, site host, notes) from the reachability layer."""
    payload = ws.require("reachability")
    entries = [e for e in (payload.get("pages") or []) if isinstance(e, dict)]
    site = (
        payload.get("site") or payload.get("origin") or payload.get("scope_domain") or ""
    )
    if not site:
        preflight = ws.read("preflight") or {}
        site = preflight.get("site") or preflight.get("url") or preflight.get("origin") or ""
    if not site and entries:
        site = entries[0].get("final_url") or entries[0].get("url") or ""
    host = (urlsplit(site).hostname or site or "").lower().strip("/")
    return entries, host, []


def load_pages(ws: Workspace, scope_domain: str) -> tuple[list[Page], list[str]]:
    """Re-parse every saved page. Missing HTML is skipped, never fatal."""
    entries, _, notes = load_inventory(ws)
    pages: list[Page] = []
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
        doc = parse_html(html, url=url, scope_host=scope_domain)
        pages.append(Page(
            url=url,
            page_type=entry.get("page_type") or "other",
            doc=doc,
            html=html,
            status=status if isinstance(status, int) else None,
        ))

    if missing:
        notes.append(
            f"{len(missing)} inventory entr{'y' if len(missing) == 1 else 'ies'} had no "
            f"readable saved HTML in the workspace and were skipped: "
            + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        )
    if non_ok:
        notes.append(
            f"{len(non_ok)} page(s) were not retrieved successfully upstream and are "
            "excluded from selection-layer analysis: " + ", ".join(non_ok[:5])
            + (" ..." if len(non_ok) > 5 else "")
        )
    return pages, notes


# ---------------------------------------------------------------------------
# JSON-LD helpers
# ---------------------------------------------------------------------------

def top_level_nodes(blocks: list[dict]) -> list[dict]:
    """Nodes that describe the page itself, flattening ``@graph`` one level."""
    out: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        graph = block.get("@graph")
        if isinstance(graph, list):
            out.extend(node for node in graph if isinstance(node, dict))
            if jsonld_types(block):
                out.append(block)
        else:
            out.append(block)
    return out


def annotate(page: Page) -> None:
    page.nodes = list(iter_jsonld_nodes(page.doc.json_ld))
    for node in page.nodes:
        page.types.update(jsonld_types(node))
    for node in top_level_nodes(page.doc.json_ld):
        page.top_types.update(jsonld_types(node))


def bare_type(value: str) -> str:
    return str(value).split("/")[-1].split("#")[-1].split(":")[-1]


def has_any(node: dict, keys: list[str]) -> bool:
    for key in keys:
        value = node.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def flatten_text(value) -> str:
    if isinstance(value, str):
        return _WS.sub(" ", value).strip()
    if isinstance(value, dict):
        parts = [
            flatten_text(value.get(k))
            for k in ("streetAddress", "addressLocality", "addressRegion",
                      "postalCode", "addressCountry", "name")
            if value.get(k)
        ]
        return ", ".join(p for p in parts if p)
    if isinstance(value, list):
        return ", ".join(p for p in (flatten_text(v) for v in value) if p)
    return ""


def normalise_phone(raw: str) -> str:
    digits = _PHONE_DIGITS.sub("", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def normalise_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").lower())
    cleaned = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|bv|plc|co|sa|ag)\b", " ", cleaned)
    return _WS.sub(" ", cleaned).strip()


def sameas_values(node: dict) -> list[str]:
    raw = node.get("sameAs")
    values = raw if isinstance(raw, list) else ([raw] if raw else [])
    return [str(v).strip() for v in values if isinstance(v, (str, int)) and str(v).strip()]


# ---------------------------------------------------------------------------
# Evidence helpers
# ---------------------------------------------------------------------------

def with_urls(text: str, urls: list[str], limit: int = 3) -> str:
    """Every evidence string must name at least one concrete URL."""
    shown = [u for u in urls if u][:limit]
    if not shown:
        return text
    joined = ", ".join(shown)
    more = f" and {len(urls) - len(shown)} more" if len(urls) > len(shown) else ""
    return f"{text} (e.g. {joined}{more})"


def sample_caveat(config: AuditConfig, n: int, label: str) -> str:
    return (
        f" Sample of {n} {label} is below the population-claim threshold of "
        f"{config.min_sample_for_population_claim}, so this is stated as a "
        "page-level observation rather than a site-wide claim."
    )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_structured_data(pages: list[Page], config: AuditConfig,
                          scope_domain: str) -> tuple[list[Finding], dict, list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    metrics: dict = {}

    first_url = urlsplit(pages[0].url) if pages else None
    if first_url and first_url.scheme in ("http", "https") and first_url.netloc:
        site_origin = f"{first_url.scheme}://{first_url.netloc}"
    else:
        site_origin = f"https://{scope_domain}"
    organization_id = f"{site_origin.rstrip('/')}/#organization"

    for page in pages:
        annotate(page)

    by_type: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        by_type[page.page_type].append(page)

    type_histogram: Counter[str] = Counter()
    for page in pages:
        type_histogram.update(sorted(page.types))

    metrics["pages_by_page_type"] = {k: len(v) for k, v in sorted(by_type.items())}
    metrics["schema_types_observed"] = dict(type_histogram.most_common(30))
    metrics["pages_with_json_ld"] = sum(1 for p in pages if p.doc.json_ld)
    metrics["pages_with_microdata_or_rdfa"] = sum(
        1 for p in pages if p.doc.microdata_types or p.doc.rdfa_types
    )
    metrics["pages_with_any_structured_data"] = sum(
        1 for p in pages if p.doc.json_ld or p.doc.microdata_types or p.doc.rdfa_types
    )
    metrics["json_ld_coverage"] = round(
        metrics["pages_with_json_ld"] / max(1, len(pages)), 3
    )

    # -- SD001 / SD008: presence of machine-readable identity -------------
    # Only pages whose type is *expected* to declare something are counted.
    # A legal page has an empty expectation list and is exempt by design.
    expecting = [
        p for p in pages
        if config.expectations_for(p.page_type).get("schema_types")
    ]
    without_any = [p for p in expecting if not (p.doc.json_ld or p.doc.microdata_types or p.doc.rdfa_types)]
    microdata_only = [p for p in expecting if not p.doc.json_ld and (p.doc.microdata_types or p.doc.rdfa_types)]

    if without_any:
        share = len(without_any) / max(1, len(expecting))
        sufficient = config.sample_is_sufficient(len(expecting))
        if share >= 0.999 and sufficient:
            severity, confidence = "high", "high"
        elif sufficient:
            severity, confidence = "medium", "high"
        else:
            severity, confidence = "low", "medium"
        evidence = with_urls(
            f"{len(without_any)} of {len(expecting)} sampled pages whose page type is "
            f"expected to declare a schema.org entity carry no JSON-LD, microdata or "
            f"RDFa at all; affected page types: "
            f"{', '.join(sorted({p.page_type for p in without_any}))}",
            [p.url for p in without_any],
        )
        if not sufficient:
            evidence += sample_caveat(config, len(expecting), "eligible pages")
        findings.append(Finding(
            title="Pages carry no machine-readable entity declaration",
            severity=severity,
            evidence=evidence,
            layer=LAYER,
            claim_level=1,
            confidence=confidence,
            check_id="SD001",
            observed=f"{len(without_any)}/{len(expecting)} eligible pages with zero structured data",
            expected="each eligible page declares at least one schema.org type appropriate to its page type",
            affected_urls=[p.url for p in without_any],
            sample_size=len(expecting),
            suggested_action=SuggestedAction(
                summary="Add JSON-LD to the page template declaring the entity each page is about",
                priority="high" if severity == "high" else "medium",
                effort="medium",
                steps=[
                    "Add one <script type=\"application/ld+json\"> block per page template.",
                    "Homepage and about pages: an Organization node with name, url, logo, description and contactPoint.",
                    "Product and pricing pages: a Product node with name and offers.",
                    "Article and docs pages: an Article/TechArticle node with headline, datePublished and author.",
                    "Validate each template once with a schema validator before shipping.",
                ],
                mechanism=(
                    "Official sources supply 34.22-46.35% of AI-assistant citations "
                    "(arXiv:2604.25707 Fig. 3). Resolving the publisher to a known entity "
                    "is an entry condition for that pool; an undeclared page competes only "
                    "for the residual share."
                ),
                verification="Re-run this check; json_ld_coverage should reach 1.0 for eligible page types.",
            ),
        ))

    if microdata_only:
        findings.append(Finding(
            title="Structured data present only as microdata/RDFa, not JSON-LD",
            severity="low",
            evidence=with_urls(
                f"{len(microdata_only)} of {len(expecting)} eligible pages expose entity data "
                f"only through microdata/RDFa attributes "
                f"({', '.join(sorted({bare_type(t) for p in microdata_only for t in (p.doc.microdata_types + p.doc.rdfa_types)})[:6]) or 'unnamed types'}) "
                f"with no JSON-LD block",
                [p.url for p in microdata_only],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="SD008",
            observed=f"{len(microdata_only)} pages with microdata/RDFa and 0 JSON-LD blocks",
            expected="JSON-LD alongside or instead of inline attribute markup",
            affected_urls=[p.url for p in microdata_only],
            sample_size=len(expecting),
            suggested_action=SuggestedAction(
                summary="Mirror the existing microdata into a JSON-LD block",
                priority="low",
                effort="low",
                steps=[
                    "Keep the microdata; add an equivalent JSON-LD block to the same template.",
                    "Reuse the values already rendered so the two never disagree.",
                ],
                mechanism=(
                    "Microdata is valid and is a real fallback, so this is not a defect. "
                    "JSON-LD is simply a single block that survives template refactors, "
                    "where attribute markup is lost whenever the surrounding HTML changes."
                ),
                verification="Both microdata and JSON-LD parse to the same entity.",
            ),
        ))

    # -- SD002: parse validity -------------------------------------------
    broken = [p for p in pages if p.doc.json_ld_errors]
    if broken:
        detail = "; ".join(
            f"{p.url}: {p.doc.json_ld_errors[0]}" for p in broken[:3]
        )
        findings.append(Finding(
            title="JSON-LD blocks fail to parse and are silently discarded",
            severity="high",
            evidence=(
                f"{len(broken)} of {len(pages)} sampled pages ship a JSON-LD block that "
                f"does not parse as JSON - {detail}"
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="SD002",
            observed=f"{len(broken)} pages with unparseable JSON-LD",
            expected="every ld+json block parses",
            affected_urls=[p.url for p in broken],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Fix the malformed JSON-LD blocks",
                priority="high",
                effort="low",
                steps=[
                    "Open the reported URL and copy the ld+json block into a JSON validator.",
                    "The usual causes are unescaped quotes in a description, a trailing comma, or an unescaped newline injected by a CMS field.",
                    "Escape values at render time instead of concatenating strings into the template.",
                ],
                mechanism=(
                    "A block that fails to parse is discarded whole. The page reads as if "
                    "it had no structured data at all, which is strictly worse than the "
                    "effort already spent on it."
                ),
                verification="Re-run this check; the SD002 finding should disappear.",
            ),
        ))

    # -- SD003: type appropriateness per page type ------------------------
    mismatches: list[tuple[Page, list[str]]] = []
    for page in pages:
        if page.page_type not in ENFORCED_PAGE_TYPES:
            continue
        expected_types = set(config.expectations_for(page.page_type).get("schema_types") or [])
        if not expected_types or not page.doc.json_ld:
            continue  # absence is SD001's job, not this check's
        if page.types & expected_types:
            continue
        specific = sorted(page.types - GENERIC_TYPES)
        mismatches.append((page, specific))

    if mismatches:
        by_page_type = defaultdict(list)
        for page, specific in mismatches:
            by_page_type[page.page_type].append((page, specific))
        for page_type, rows in sorted(by_page_type.items()):
            expected_types = config.expectations_for(page_type)["schema_types"]
            total_of_type = len(by_type[page_type])
            sufficient = config.sample_is_sufficient(total_of_type)
            observed_types = sorted({t for _, specific in rows for t in specific}) or ["only generic wrapper types"]
            evidence = with_urls(
                f"{len(rows)} of {total_of_type} sampled {page_type} pages carry JSON-LD that "
                f"declares {', '.join(observed_types[:5])} but none of "
                f"{', '.join(expected_types[:4])}",
                [p.url for p, _ in rows],
            )
            if not sufficient:
                evidence += sample_caveat(config, total_of_type, f"{page_type} pages")
            findings.append(Finding(
                title=f"{page_type.capitalize()} pages declare a schema type that does not describe them",
                severity="medium" if sufficient else "low",
                evidence=evidence,
                layer=LAYER,
                claim_level=1,
                # page_type is inferred from URL shape upstream, so the mapping
                # itself carries uncertainty even though the count does not.
                confidence="medium",
                check_id="SD003",
                observed=f"types present: {', '.join(observed_types[:5])}",
                expected=f"one of {', '.join(expected_types)}",
                affected_urls=[p.url for p, _ in rows],
                sample_size=total_of_type,
                suggested_action=SuggestedAction(
                    summary=f"Declare a {expected_types[0]} node on {page_type} pages",
                    priority="medium",
                    effort="low",
                    steps=[
                        f"Add a {expected_types[0]} node to the {page_type} template, populated from the same fields the page already renders.",
                        "Keep the existing generic nodes (WebPage, BreadcrumbList) - they are complementary, not replacements.",
                    ],
                    mechanism=(
                        "Generic wrapper types identify a document; they do not identify the "
                        "thing the document is about. An engine matching a product or "
                        "how-to intent has nothing typed to match against."
                    ),
                    verification=f"Each {page_type} URL exposes a {expected_types[0]} node.",
                ),
            ))

    # -- SD007: required-property completeness for types actually present --
    incomplete: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for page in pages:
        for node in top_level_nodes(page.doc.json_ld):
            for type_name in jsonld_types(node):
                requirements = REQUIRED_PROPERTIES.get(type_name)
                if not requirements:
                    continue
                missing = [label for label, keys in requirements if not has_any(node, keys)]
                if missing:
                    incomplete[type_name].append((page.url, ", ".join(missing)))

    for type_name, node_rows in sorted(incomplete.items()):
        urls = [url for url, _ in node_rows]
        missing_counter = Counter(m for _, m in node_rows)
        findings.append(Finding(
            title=f"{type_name} nodes are missing required properties",
            severity="medium",
            evidence=with_urls(
                f"{len(node_rows)} {type_name} node(s) across the sample omit "
                f"{'; '.join(f'{fields} ({count}x)' for fields, count in missing_counter.most_common(3))}",
                urls,
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="SD007",
            observed=f"{len(node_rows)} incomplete {type_name} node(s)",
            expected=f"{type_name} carries {', '.join(label for label, _ in REQUIRED_PROPERTIES[type_name])}",
            affected_urls=urls,
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary=f"Populate the missing {type_name} properties from data the page already renders",
                priority="medium",
                effort="low",
                steps=[
                    f"For each {type_name} node, fill {', '.join(sorted(missing_counter))}.",
                    "Bind the values to the same template variables the visible page uses so they cannot drift apart.",
                ],
                mechanism=(
                    "An incomplete node is not a partially useful node. Consumers that "
                    "validate required properties drop it entirely, so the page reads as "
                    "untyped despite carrying markup."
                ),
                verification="Re-run this check; SD007 should not report this type.",
            ),
        ))

    # -- SD011: conflicting primary types on one page ---------------------
    conflicted: list[tuple[str, list[str]]] = []
    duplicate_orgs: list[tuple[str, list[str]]] = []
    for page in pages:
        groups = {
            name for name, members in PRIMARY_TYPE_GROUPS.items()
            if page.top_types & members
        }
        if len(groups) >= 2:
            conflicted.append((page.url, sorted(groups)))
        org_names = []
        for node in top_level_nodes(page.doc.json_ld):
            if set(jsonld_types(node)) & ORGANIZATION_TYPES:
                name = flatten_text(node.get("name") or node.get("legalName"))
                if name:
                    org_names.append(name)
        distinct = {normalise_name(n) for n in org_names}
        if len(distinct) >= 2:
            duplicate_orgs.append((page.url, sorted(set(org_names))))

    if conflicted:
        findings.append(Finding(
            title="A single page declares two conflicting primary entity types",
            severity="medium",
            evidence=with_urls(
                f"{len(conflicted)} page(s) declare more than one primary entity category in "
                f"top-level JSON-LD: "
                + "; ".join(f"{url} -> {' + '.join(groups)}" for url, groups in conflicted[:3]),
                [url for url, _ in conflicted],
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="SD011",
            observed="; ".join(f"{'+'.join(g)}" for _, g in conflicted[:3]),
            expected="one primary entity per document (supporting Organization/WebSite/BreadcrumbList nodes are fine)",
            affected_urls=[url for url, _ in conflicted],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Keep one primary entity per URL and move the other onto its own page",
                priority="medium",
                effort="medium",
                steps=[
                    "Decide what the URL is primarily about and keep only that node as the page entity.",
                    "Express the secondary entity as a reference (@id) to its own canonical URL rather than a second full node.",
                ],
                mechanism=(
                    "Two primary types on one document force a consumer to guess which one "
                    "the URL answers for. Guessing is resolved by dropping the ambiguous "
                    "node, not by picking one."
                ),
                verification="Each URL exposes exactly one primary entity type.",
            ),
        ))

    if duplicate_orgs:
        findings.append(Finding(
            title="Conflicting organisation names declared on the same page",
            severity="medium",
            evidence=with_urls(
                f"{len(duplicate_orgs)} page(s) declare two or more differently named "
                f"Organization nodes: "
                + "; ".join(f"{url} -> {' vs '.join(names[:3])}" for url, names in duplicate_orgs[:3]),
                [url for url, _ in duplicate_orgs],
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="SD012",
            observed="; ".join(" vs ".join(n[:3]) for _, n in duplicate_orgs[:2]),
            expected="one organisation identity per site, referenced by @id where repeated",
            affected_urls=[url for url, _ in duplicate_orgs],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Collapse the organisation nodes to one identity with a stable @id",
                priority="medium",
                effort="low",
                steps=[
                    f"Define the Organization once, with an @id such as {organization_id}.",
                    "Reference that @id from every other node (publisher, provider, author) instead of repeating the object.",
                ],
                mechanism=(
                    "Two names on one page is the signal that splits an entity in two. "
                    "Resolution is the entry condition to the official-source pool; a split "
                    "identity halves whatever authority the brand has accumulated."
                ),
                verification="Only one distinct organisation name appears in the sample.",
            ),
        ))

    # -- SD004 / SD005 / SD006: organisation identity ---------------------
    org_nodes: list[tuple[str, dict]] = []
    for page in pages:
        for node in top_level_nodes(page.doc.json_ld):
            if set(jsonld_types(node)) & ORGANIZATION_TYPES:
                org_nodes.append((page.url, node))

    metrics["organization_nodes_found"] = len(org_nodes)

    if not org_nodes:
        sufficient = config.sample_is_sufficient(len(pages))
        candidate_urls = [p.url for p in pages if p.page_type in ("homepage", "about")] or [p.url for p in pages]
        evidence = with_urls(
            f"No Organization, Corporation or LocalBusiness node appears in the JSON-LD of "
            f"any of the {len(pages)} sampled pages, including the homepage and about pages "
            f"in the sample",
            candidate_urls,
        )
        if not sufficient:
            evidence += sample_caveat(config, len(pages), "sampled pages")
        findings.append(Finding(
            title="The brand declares no organisation entity anywhere in the sample",
            severity="high" if sufficient else "low",
            evidence=evidence,
            layer=LAYER,
            claim_level=1,
            confidence="high" if sufficient else "medium",
            check_id="SD005",
            observed="0 Organization-class nodes across the sample",
            expected="one Organization node, ideally site-wide via the base template",
            affected_urls=candidate_urls,
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Declare an Organization node site-wide with a stable @id",
                priority="high",
                effort="low",
                steps=[
                    "Add an Organization node to the base template: name, url, logo, description, contactPoint.",
                    f"Give it a stable @id ({organization_id}) and reference it as publisher from article and product nodes.",
                    "List every profile the brand controls in sameAs.",
                ],
                mechanism=(
                    "Official sources are 34.22-46.35% of AI citations (arXiv:2604.25707 "
                    "Fig. 3). An engine reaches that pool by resolving a page to a known "
                    "publisher; with no declared entity there is nothing to resolve to."
                ),
                verification="An Organization node is present on every page of a re-run sample.",
            ),
        ))
    else:
        # Completeness is judged on the richest node the site publishes.
        best_url, best_node, best_score = org_nodes[0][0], org_nodes[0][1], -1
        for url, node in org_nodes:
            score = sum(1 for _, keys in ORGANIZATION_FIELDS if has_any(node, keys)) + len(sameas_values(node))
            if score > best_score:
                best_url, best_node, best_score = url, node, score
        missing_fields = [label for label, keys in ORGANIZATION_FIELDS if not has_any(best_node, keys)]
        present_fields = [label for label, keys in ORGANIZATION_FIELDS if has_any(best_node, keys)]
        metrics["organization_fields_present"] = present_fields
        metrics["organization_fields_missing"] = missing_fields

        if missing_fields:
            findings.append(Finding(
                title="Organization node is incomplete",
                severity="medium" if len(missing_fields) >= 2 else "low",
                evidence=with_urls(
                    f"The most complete Organization node on the site declares "
                    f"{', '.join(present_fields) or 'no recognised fields'} but omits "
                    f"{', '.join(missing_fields)}",
                    [best_url],
                ),
                layer=LAYER,
                claim_level=1,
                confidence="high",
                check_id="SD004",
                observed=f"missing: {', '.join(missing_fields)}",
                expected="name, url, logo, description and a contact point",
                affected_urls=[url for url, _ in org_nodes],
                sample_size=len(pages),
                suggested_action=SuggestedAction(
                    summary="Complete the Organization node",
                    priority="medium",
                    effort="low",
                    steps=[f"Add {field} to the Organization node in the base template." for field in missing_fields],
                    mechanism=(
                        "Each field is an independent handle for resolving the brand to a "
                        "single real-world entity. A node with a name and nothing else "
                        "matches every other company with a similar name."
                    ),
                    verification="Re-run this check; organization_fields_missing should be empty.",
                ),
            ))

        all_sameas = sorted({v for _, node in org_nodes for v in sameas_values(node)})
        sameas_hosts = sorted({(urlsplit(v).hostname or "").lower().removeprefix("www.") for v in all_sameas} - {""})
        metrics["sameas_count"] = len(all_sameas)
        metrics["sameas_hosts"] = sameas_hosts

        if len(sameas_hosts) < 2:
            findings.append(Finding(
                title="Entity has too few sameAs references to disambiguate",
                severity="medium",
                evidence=with_urls(
                    f"The Organization node lists {len(all_sameas)} sameAs reference(s) "
                    f"across {len(sameas_hosts)} distinct host(s)"
                    + (f" ({', '.join(sameas_hosts)})" if sameas_hosts else "")
                    + "; an engine has no second source to cross-check the entity against",
                    [best_url],
                ),
                layer=LAYER,
                claim_level=1,
                confidence="high",
                check_id="SD006",
                observed=f"{len(all_sameas)} sameAs values on {len(sameas_hosts)} hosts",
                expected="references to several independent profiles the brand controls or appears in",
                affected_urls=[best_url],
                sample_size=len(pages),
                suggested_action=SuggestedAction(
                    summary="List every profile the brand controls in sameAs",
                    priority="medium",
                    effort="low",
                    steps=[
                        "Add the brand's LinkedIn, Crunchbase, GitHub, X and any industry-registry profile URLs to sameAs.",
                        "Add the Wikidata or Wikipedia entity URL if one exists.",
                        "Only list profiles that actually link back or are verifiably the same entity.",
                    ],
                    mechanism=(
                        "sameAs is the only place a page states which of several similarly "
                        "named entities it is. Each additional independent host is one more "
                        "way a resolver can confirm the match."
                    ),
                    verification="sameAs_hosts in the metrics block lists three or more distinct hosts.",
                ),
            ))

        # -- SD009: generic brand name needs more disambiguation ----------
        brand_token = (scope_domain.split(".")[0] if scope_domain else "").lower()
        ambiguous_reason = ""
        if brand_token:
            if brand_token in AMBIGUOUS_BRAND_TOKENS:
                ambiguous_reason = "is an ordinary English word reused by many unrelated companies"
            elif len(brand_token) <= 4 and brand_token.isalpha():
                ambiguous_reason = f"is a {len(brand_token)}-letter token that collides easily with acronyms and other brands"
            elif GENERIC_BRAND_SUFFIXES.search(brand_token):
                ambiguous_reason = "ends in a generic industry suffix shared by many companies"
        if ambiguous_reason and len(sameas_hosts) < 3:
            findings.append(Finding(
                title="Common-noun brand name is under-disambiguated",
                severity="medium",
                evidence=with_urls(
                    f"The brand token '{brand_token}' (from {scope_domain}) {ambiguous_reason}, "
                    f"and the entity carries only {len(sameas_hosts)} distinct sameAs host(s)"
                    + (f" ({', '.join(sameas_hosts)})" if sameas_hosts else "")
                    + ". A name that is also a common word needs more external anchoring, not less",
                    [best_url],
                ),
                layer=LAYER,
                claim_level=3,
                confidence="medium",
                check_id="SD009",
                observed=f"brand token '{brand_token}', {len(sameas_hosts)} sameAs host(s)",
                expected="three or more independent sameAs anchors, plus a description that names the category",
                affected_urls=[best_url],
                sample_size=len(pages),
                suggested_action=SuggestedAction(
                    summary="Anchor the ambiguous brand name with more external references and an explicit category",
                    priority="medium",
                    effort="low",
                    steps=[
                        "Expand sameAs to three or more independent hosts.",
                        "Write the Organization description so it names the category and the audience in the first clause, not a slogan.",
                        "Use the full legal name in legalName alongside the trading name.",
                    ],
                    mechanism=(
                        "This is a mechanism interpretation, not a measured outcome: a token "
                        "that is also a common noun gives a resolver more candidate entities "
                        "to choose between, and external anchors are what narrow the set."
                    ),
                    verification="sameAs lists three or more hosts and the description names the category.",
                ),
            ))

    # -- SD010: NAP consistency across pages ------------------------------
    names: dict[str, list[str]] = defaultdict(list)
    phones: dict[str, list[str]] = defaultdict(list)
    addresses: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        for node in top_level_nodes(page.doc.json_ld):
            if not set(jsonld_types(node)) & ORGANIZATION_TYPES:
                continue
            name = flatten_text(node.get("name") or node.get("legalName"))
            if name:
                names[normalise_name(name)].append(page.url)
            phone = flatten_text(node.get("telephone")) or flatten_text(
                (node.get("contactPoint") or {}) if isinstance(node.get("contactPoint"), dict)
                else next((c for c in (node.get("contactPoint") or []) if isinstance(c, dict)), {})
            )
            phone_digits = normalise_phone(phone)
            if len(phone_digits) >= 7:
                phones[phone_digits].append(page.url)
            address = flatten_text(node.get("address"))
            if address:
                addresses[normalise_name(address)].append(page.url)

    metrics["nap_variants"] = {
        "names": {k: len(v) for k, v in names.items()},
        "phones": {k: len(v) for k, v in phones.items()},
        "addresses": {k: len(v) for k, v in addresses.items()},
    }

    for label, bucket, check_id in (
        ("organisation name", names, "SD010a"),
        ("phone number", phones, "SD010b"),
        ("postal address", addresses, "SD010c"),
    ):
        if len(bucket) < 2:
            continue
        pages_covered = len({u for urls in bucket.values() for u in urls})
        if pages_covered < 2:
            continue
        variants = sorted(bucket.items(), key=lambda kv: -len(kv[1]))
        findings.append(Finding(
            title=f"Inconsistent {label} across pages",
            severity="medium",
            evidence=with_urls(
                f"{len(bucket)} different {label} values appear in the structured data of "
                f"{pages_covered} sampled pages: "
                + "; ".join(f"'{value}' on {len(urls)} page(s)" for value, urls in variants[:3]),
                [urls[0] for _, urls in variants],
            ),
            layer=LAYER,
            claim_level=2,
            confidence="medium",
            check_id=check_id,
            observed=f"{len(bucket)} distinct {label} values",
            expected="one value, rendered from a single source in the template",
            affected_urls=[urls[0] for _, urls in variants],
            sample_size=pages_covered,
            suggested_action=SuggestedAction(
                summary=f"Render the {label} from one source of truth",
                priority="medium",
                effort="low",
                steps=[
                    f"Pick the canonical {label} and store it once in the site configuration.",
                    "Render both the visible page and the JSON-LD from that single value.",
                    "If the variants are legitimately different locations, model them as separate LocalBusiness nodes with distinct @ids.",
                ],
                mechanism=(
                    "Name, address and phone are the classic cross-source matching keys. "
                    "Disagreeing values on the brand's own site give a resolver a reason to "
                    "treat the pages as separate entities."
                ),
                verification="Only one normalised value per field appears in the sample.",
            ),
        ))

    metrics["pages_analysed"] = len(pages)
    return findings, metrics, notes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def drop_unevidenced(findings: list[Finding], notes: list[str]) -> list[Finding]:
    """Structural guarantee: no finding leaves without a concrete URL in evidence."""
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
    lines = [
        f"structured-data-identity  layer={LAYER}",
        f"  pages analysed : {payload['pages_analysed']}",
        f"  JSON-LD covered: {payload['metrics'].get('pages_with_json_ld', 0)}"
        f"/{payload['pages_analysed']}"
        f"  (coverage {payload['metrics'].get('json_ld_coverage', 0)})",
        f"  org nodes      : {payload['metrics'].get('organization_nodes_found', 0)}"
        f"   sameAs hosts: {len(payload['metrics'].get('sameas_hosts', []))}",
        f"  findings       : {len(payload['findings'])}",
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
        lines.append("    no selection-layer defects detected in the sampled pages")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_structured_data.py",
        description="Selection-layer audit: structured data and entity identity. "
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

        _, site_host, notes = load_inventory(ws)
        scope_domain = registrable_domain(site_host) if site_host else ""
        pages, load_notes = load_pages(ws, scope_domain or site_host)
        notes.extend(load_notes)

        if pages:
            config.calibrate_to_site([p.doc.structure_summary() for p in pages])
            findings, metrics, check_notes = check_structured_data(pages, config, scope_domain)
            notes.extend(check_notes)
        else:
            findings, metrics = [], {"pages_analysed": 0}
            notes.append(
                "no page in the reachability inventory had readable saved HTML; "
                "the selection layer made no observations"
            )

        findings = drop_unevidenced(findings, notes)
        payload = {
            "layer": LAYER,
            "skill": "structured-data-identity",
            "site": site_host,
            "scope_domain": scope_domain,
            "findings": [f.to_dict() for f in findings],
            "metrics": metrics,
            "pages_analysed": len(pages),
            "notes": notes,
            "thresholds": {
                "min_sample_for_population_claim": config.min_sample_for_population_claim,
            },
        }
        ws.write(LAYER, payload)
    except Exception as exc:  # noqa: BLE001 - a hard error must exit 2, not traceback
        print(f"check_structured_data: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else human_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
