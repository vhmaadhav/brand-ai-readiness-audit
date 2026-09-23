#!/usr/bin/env python3
"""Engagement-layer detector: why the visitor who arrives does not stay.

This is the on-site half of the audit. It reads the page inventory written by
the reachability layer, re-parses the saved HTML from disk, and never fetches
anything.

The framing that makes this layer different from the off-site layers: a visitor
arriving from an AI answer has already been told what the page says. They land
mid-funnel, on a deep page, with a specific question, and they leave the moment
the page fails to continue that intent. So the checks here are about
orientation and continuity, not about ranking.

Core Web Vitals are NOT measured. They cannot be measured from raw HTML, and
pretending otherwise would be a fabricated number. What is measured instead is
a set of structural proxies - render-blocking resources in <head>, images with
no intrinsic dimensions - and every finding derived from them is emitted at
claim level 3 (mechanism interpretation) and says so in its own evidence.
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
import math  # noqa: E402
import re  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from urllib.parse import urlsplit, urlunsplit  # noqa: E402

from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.htmldoc import HtmlDoc, iter_jsonld_nodes, jsonld_types, parse_html  # noqa: E402
from geo_audit.netguard import registrable_domain  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.textstats import (  # noqa: E402
    COMPARISON_MARKERS,
    DEFINITION_MARKERS,
    NUMERIC_MARKERS,
    boilerplate_ratio,
)
from geo_audit.workspace import Workspace  # noqa: E402

LAYER = "engagement"

LEAD_WORDS = 150          # proxy for "above the fold"; the fold itself is unmeasurable
MIN_LEAD_WORDS = 40       # below this there is no lead to judge
BOILERPLATE_LIMIT = 0.60  # share of page text outside the main content region
MIN_WORDS_FOR_RATIO = 200
FORM_FIELD_FRICTION = 6
RENDER_BLOCKING_LIMIT = 4
STYLESHEET_LIMIT = 6

# Promotional vocabulary only. The generic hedges in textstats.HEDGE_MARKERS
# ("may", "could") appear in perfectly good technical prose, so using them here
# would manufacture false positives.
MARKETING_RE = re.compile(
    r"\b(industry[- ]leading|world[- ]class|best[- ]in[- ]class|cutting[- ]edge|"
    r"state[- ]of[- ]the[- ]art|revolutionary|game[- ]chang\w+|seamless\w*|"
    r"synerg\w+|next[- ]generation|innovat(?:ive|ion)|empower\w*|unlock\w*|"
    r"transform(?:ing|ative|ation)?|elevat\w+|unparalleled|unrivalled|unrivaled|"
    r"bespoke|holistic|turnkey|frictionless|effortless\w*|reimagin\w+|"
    r"passionate|delight\w*|journey|mission[- ]driven|thought leader\w*)\b", re.I,
)

# Interstitial markup. Split into signals that genuinely block the first
# interaction and signals that merely suggest an overlay exists.
DIALOG_OPEN_RE = re.compile(r"<dialog\b[^>]*\bopen\b", re.I)
ARIA_MODAL_RE = re.compile(r"aria-modal\s*=\s*[\"']?true", re.I)
SCROLL_LOCK_RE = re.compile(
    r"<body\b[^>]*class\s*=\s*[\"'][^\"']*\b(modal-open|no-scroll|noscroll|"
    r"overflow-hidden|scroll-locked|menu-open-body)\b", re.I,
)
PAYWALL_RE = re.compile(
    r"\b(paywall|regwall|age[-_ ]?gate|age[-_ ]verification|subscribe[-_ ]wall|"
    r"registration[-_ ]wall|content[-_ ]lock)\b", re.I,
)
CONSENT_MARKUP_RE = re.compile(
    r"(id|class)\s*=\s*[\"'][^\"']*\b(cookie[-_ ]?(banner|consent|notice|wall)|"
    r"consent[-_ ]?(banner|modal|manager|dialog)|gdpr[-_ ]?(banner|modal)|"
    r"cmp[-_ ]?(container|banner))\b", re.I,
)
POPUP_MARKUP_RE = re.compile(
    r"(id|class)\s*=\s*[\"'][^\"']*\b(newsletter[-_ ]?(modal|popup|overlay)|"
    r"exit[-_ ]?intent|signup[-_ ]?(modal|overlay)|promo[-_ ]?(modal|overlay)|"
    r"lightbox[-_ ]?overlay|interstitial)\b", re.I,
)
FULLSCREEN_OVERLAY_RE = re.compile(
    r"style\s*=\s*[\"'][^\"']*position\s*:\s*fixed[^\"']*z-index\s*:\s*(\d{3,})", re.I,
)

BREADCRUMB_MARKUP_RE = re.compile(
    r"(class|id)\s*=\s*[\"'][^\"']*\bbreadcrumbs?\b|aria-label\s*=\s*[\"']breadcrumb", re.I,
)

HEAD_END_RE = re.compile(r"</head\s*>", re.I)
SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>", re.I)
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
FORM_BLOCK_RE = re.compile(r"<form\b[^>]*>(.*?)</form\s*>", re.I | re.S)
FORM_OPEN_RE = re.compile(r"<form\b[^>]*>", re.I)
INPUT_TAG_RE = re.compile(r"<(input|select|textarea)\b[^>]*>", re.I)
LABEL_FOR_RE = re.compile(r"<label\b[^>]*\bfor\s*=\s*[\"']([^\"']+)[\"']", re.I)
ATTR_RE = re.compile(r"([a-zA-Z_:][-\w:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)")

SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset", "search"}


def attrs_of(tag: str) -> dict[str, str]:
    return {
        key.lower(): value.strip("\"'")
        for key, value in ATTR_RE.findall(tag)
    }


@dataclass
class Page:
    url: str
    page_type: str
    doc: HtmlDoc
    html: str
    head: str = ""
    signals: dict = field(default_factory=dict)


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
        head_match = HEAD_END_RE.search(html)
        pages.append(Page(
            url=url,
            page_type=entry.get("page_type") or "other",
            doc=parse_html(html, url=url, scope_host=scope_domain),
            html=html,
            head=html[: head_match.start()] if head_match else html[:8000],
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
            "excluded from engagement analysis: " + ", ".join(non_ok[:5])
            + (" ..." if len(non_ok) > 5 else "")
        )
    return pages, notes


# ---------------------------------------------------------------------------
# Per-page signal extraction
# ---------------------------------------------------------------------------

def analyse_forms(html: str) -> dict:
    """Count real user-facing fields per form, and how many carry a label."""
    blocks = [m.group(1) for m in FORM_BLOCK_RE.finditer(html)]
    if not blocks and FORM_OPEN_RE.search(html):
        blocks = [html[FORM_OPEN_RE.search(html).end():]]
    forms = []
    for block in blocks[:6]:
        labelled_ids = set(LABEL_FOR_RE.findall(block))
        fields, unlabelled = 0, 0
        required = 0
        for match in INPUT_TAG_RE.finditer(block):
            tag = match.group(0)
            attrs = attrs_of(tag)
            if match.group(1).lower() == "input" and attrs.get("type", "text").lower() in SKIP_INPUT_TYPES:
                continue
            if "hidden" in attrs:
                continue
            fields += 1
            if "required" in attrs or attrs.get("aria-required") == "true":
                required += 1
            has_label = (
                attrs.get("id", "\x00") in labelled_ids
                or attrs.get("aria-label")
                or attrs.get("aria-labelledby")
                or attrs.get("placeholder")
                or attrs.get("title")
            )
            if not has_label:
                unlabelled += 1
        if fields:
            forms.append({"fields": fields, "unlabelled": unlabelled, "required": required})
    return {"forms": forms, "max_fields": max((f["fields"] for f in forms), default=0)}


def analyse_head(page: Page) -> dict:
    blocking_scripts = 0
    for match in SCRIPT_TAG_RE.finditer(page.head):
        attrs = attrs_of(match.group(0))
        if "src" not in attrs:
            continue
        if "async" in attrs or "defer" in attrs or attrs.get("type", "").lower() == "module":
            continue
        blocking_scripts += 1
    stylesheets = sum(
        1 for match in LINK_TAG_RE.finditer(page.head)
        if "stylesheet" in attrs_of(match.group(0)).get("rel", "").lower()
    )
    return {"render_blocking_scripts": blocking_scripts, "head_stylesheets": stylesheets}


def analyse_images(html: str) -> dict:
    total = 0
    no_dimensions = 0
    for match in IMG_TAG_RE.finditer(html):
        attrs = attrs_of(match.group(0))
        total += 1
        style = attrs.get("style", "").lower()
        if not (("width" in attrs and "height" in attrs) or "aspect-ratio" in style):
            no_dimensions += 1
    return {"img_tags": total, "img_without_dimensions": no_dimensions}


def interstitial_signals(page: Page) -> tuple[list[str], list[str]]:
    strong, weak = [], []
    if DIALOG_OPEN_RE.search(page.html):
        strong.append("<dialog open> in the served HTML")
    if ARIA_MODAL_RE.search(page.html):
        strong.append('aria-modal="true" element present on load')
    if SCROLL_LOCK_RE.search(page.html):
        strong.append("<body> ships with a scroll-lock class")
    paywall = PAYWALL_RE.search(page.html)
    if paywall:
        strong.append(f"'{paywall.group(0)}' markup present")
    if CONSENT_MARKUP_RE.search(page.html):
        weak.append("consent/cookie banner markup")
    if POPUP_MARKUP_RE.search(page.html):
        weak.append("newsletter/promo overlay markup")
    overlay = FULLSCREEN_OVERLAY_RE.search(page.html)
    if overlay and int(overlay.group(1)) >= 100:
        weak.append(f"fixed-position overlay with z-index {overlay.group(1)}")
    return strong, weak


def heading_skips(doc: HtmlDoc) -> list[tuple[int, int]]:
    skips = []
    previous = None
    for heading in doc.headings:
        if previous is not None and heading.level > previous + 1:
            skips.append((previous, heading.level))
        previous = heading.level
    return skips


def has_breadcrumb(page: Page) -> bool:
    if BREADCRUMB_MARKUP_RE.search(page.html):
        return True
    for node in iter_jsonld_nodes(page.doc.json_ld):
        if "BreadcrumbList" in jsonld_types(node):
            return True
    return False


def normalise_href(href: str) -> str:
    parts = urlsplit(href)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def annotate(page: Page, scope_domain: str, brand_token: str) -> None:
    doc = page.doc
    lead = " ".join((doc.main_text or doc.text).split()[:LEAD_WORDS])
    text = doc.main_text or doc.text
    page.signals = {
        "word_count": doc.word_count,
        "total_word_count": doc.total_word_count,
        "boilerplate_ratio": boilerplate_ratio(doc.word_count, doc.total_word_count),
        "lead": lead,
        "lead_words": len(lead.split()),
        "lead_marketing_terms": len(MARKETING_RE.findall(lead)),
        "lead_evidence_markers": (
            len(DEFINITION_MARKERS.findall(lead))
            + len(NUMERIC_MARKERS.findall(lead))
            + len(COMPARISON_MARKERS.findall(lead))
        ),
        "h1_count": sum(1 for h in doc.headings if h.level == 1),
        "heading_skips": heading_skips(doc),
        "images": doc.images,
        "images_missing_alt": doc.images_missing_alt,
        "internal_links": [normalise_href(l.href) for l in doc.internal_links],
        "has_breadcrumb": has_breadcrumb(page),
        "brand_named": bool(brand_token) and (
            brand_token in text.lower() or brand_token in doc.title.lower()
        ),
        "path_depth": len([s for s in urlsplit(page.url).path.split("/") if s]),
    }
    page.signals.update(analyse_head(page))
    page.signals.update(analyse_images(page.html))
    page.signals.update(analyse_forms(page.html))
    strong, weak = interstitial_signals(page)
    page.signals["interstitial_strong"] = strong
    page.signals["interstitial_weak"] = weak


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


def snippet(text: str, limit: int = 140) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_engagement(pages: list[Page], config: AuditConfig,
                     scope_domain: str) -> tuple[list[Finding], dict, list[str]]:
    findings: list[Finding] = []
    notes: list[str] = []
    sufficient_sample = config.sample_is_sufficient(len(pages))

    def expectation(page: Page) -> dict:
        return config.expectations_for(page.page_type)

    def exempt(page: Page) -> bool:
        exp = expectation(page)
        return bool(exp.get("exempt_from_absorption") or exp.get("is_hub"))

    # -- EN001: answer-first orientation ----------------------------------
    lead_candidates = [
        p for p in pages
        if not exempt(p)
        and p.page_type in ("homepage", "product", "pricing", "service", "article", "docs", "about")
        and p.signals["lead_words"] >= MIN_LEAD_WORDS
        and p.signals["word_count"] >= 120
    ]
    buried = [
        p for p in lead_candidates
        if p.signals["lead_marketing_terms"] >= 2 and p.signals["lead_evidence_markers"] == 0
    ]
    if buried:
        example = buried[0]
        findings.append(Finding(
            title="The opening of the page sells rather than answers",
            severity="medium",
            evidence=with_urls(
                f"{len(buried)} of {len(lead_candidates)} sampled pages open with "
                f"{example.signals['lead_marketing_terms']}+ promotional phrases and zero "
                f"definition, numeric or comparison content in the first {LEAD_WORDS} words - "
                f"e.g. \"{snippet(example.signals['lead'])}\"",
                [p.url for p in buried],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN001",
            observed=f"first {LEAD_WORDS} words: {example.signals['lead_marketing_terms']} promotional phrases, 0 evidence markers",
            expected="the page's core claim stated plainly in the opening lines",
            affected_urls=[p.url for p in buried],
            sample_size=len(lead_candidates),
            suggested_action=SuggestedAction(
                summary="Lead with the answer: what this is, who it is for, and the one number that proves it",
                priority="medium",
                effort="low",
                steps=[
                    "Rewrite the first paragraph as a direct statement of what the page is about.",
                    "Move the aspirational copy below that statement rather than in front of it.",
                    "Include one concrete, checkable fact in the opening - a price, a measurement, a scope limit.",
                ],
                mechanism=(
                    "Mechanism interpretation, hedged accordingly: a visitor arriving from an "
                    "AI answer has already been told roughly what the page says and is "
                    "checking whether it is true. An opening that restates ambition instead "
                    "of substance gives them nothing to confirm. The first "
                    f"{LEAD_WORDS} words are a proxy for the fold, which cannot be measured "
                    "from HTML."
                ),
                verification="The first paragraph of each listed page states a checkable fact.",
            ),
        ))

    # -- EN002: intent continuity on deep pages ---------------------------
    deep = [
        p for p in pages
        if p.signals["path_depth"] >= 2 and not exempt(p) and p.signals["word_count"] >= 80
    ]
    orphaned = []
    for page in deep:
        reasons = []
        if not page.signals["has_breadcrumb"]:
            reasons.append("no breadcrumb trail")
        if not page.signals["brand_named"]:
            reasons.append("the brand is never named in the page's own text or title")
        if page.signals["h1_count"] == 0:
            reasons.append("no h1 to state what the page is")
        if len(reasons) >= 2:
            orphaned.append((page, reasons))

    if orphaned:
        page, reasons = orphaned[0]
        findings.append(Finding(
            title="Deep pages do not stand on their own for a mid-funnel arrival",
            severity="medium",
            evidence=with_urls(
                f"{len(orphaned)} of {len(deep)} sampled deep pages (path depth 2+) lack two or "
                f"more self-orientation signals - e.g. {page.url} has: {', '.join(reasons)}. "
                f"A visitor sent straight here by an AI answer has no way to place the page",
                [p.url for p, _ in orphaned],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN002",
            observed="; ".join(reasons),
            expected="every deep page names the brand, states its subject in an h1, and shows where it sits",
            affected_urls=[p.url for p, _ in orphaned],
            sample_size=len(deep),
            suggested_action=SuggestedAction(
                summary="Make every deep page a valid entry point, not a continuation of the nav",
                priority="medium",
                effort="medium",
                steps=[
                    "Add a breadcrumb trail (and a BreadcrumbList node) so the page states its own context.",
                    "Name the brand and the product in the page's own copy, not only in the header logo.",
                    "Give every page exactly one h1 that answers 'what is this page'.",
                ],
                mechanism=(
                    "Interpretation, hedged: AI referrals land mid-funnel on deep URLs rather "
                    "than the homepage, so a page written as step three of a guided path is "
                    "read cold. Self-orientation signals are what let a cold arrival continue "
                    "rather than bounce."
                ),
                verification="Each listed page carries a breadcrumb, an h1 and its own brand context.",
            ),
        ))

    # -- EN003: interstitials that block the first interaction -------------
    blocked = [
        p for p in pages
        if p.signals["interstitial_strong"]
        and len(p.signals["interstitial_strong"]) + len(p.signals["interstitial_weak"]) >= 2
    ]
    if blocked:
        page = blocked[0]
        detail = "; ".join(page.signals["interstitial_strong"] + page.signals["interstitial_weak"])
        findings.append(Finding(
            title="An interstitial is present in the served HTML before any interaction",
            severity="medium",
            evidence=with_urls(
                f"{len(blocked)} of {len(pages)} sampled pages ship blocking-interstitial markup "
                f"in the initial HTML response - {detail}. Two or more independent signals are "
                f"required before this is reported, so an ordinary dismissible cookie bar alone "
                f"does not trigger it",
                [p.url for p in blocked],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN003",
            observed=detail,
            expected="the content is readable and interactive on arrival; consent handled without a scroll lock",
            affected_urls=[p.url for p in blocked],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Let the page be read before asking for anything",
                priority="medium",
                effort="medium",
                steps=[
                    "Render the content behind the overlay in the initial HTML, not after dismissal.",
                    "Replace scroll-locking modals with an inline banner that does not trap focus or block reading.",
                    "Delay newsletter and promo overlays until after a meaningful interaction, and never on a first deep-link arrival.",
                ],
                mechanism=(
                    "Interpretation from static markup, hedged: presence in the HTML is "
                    "observed directly, but whether it blocks depends on runtime CSS and "
                    "consent state, which this audit does not execute. A visitor arriving "
                    "with a specific question spends their patience on the overlay instead of "
                    "the answer."
                ),
                verification="The page's primary content is readable without dismissing anything.",
            ),
        ))

    # -- EN004: heading hierarchy as a scannability proxy -------------------
    no_h1 = [p for p in pages if p.signals["h1_count"] == 0 and p.signals["word_count"] >= 80]
    if no_h1:
        evidence = with_urls(
            f"{len(no_h1)} of {len(pages)} sampled pages with 80+ words carry no h1 element; "
            f"the page never states in a heading what it is about",
            [p.url for p in no_h1],
        )
        if not sufficient_sample:
            evidence += sample_caveat(config, len(pages), "sampled pages")
        findings.append(Finding(
            title="Pages have no h1",
            severity="medium" if sufficient_sample else "low",
            evidence=evidence,
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="EN004a",
            observed=f"{len(no_h1)} pages with 0 h1 elements",
            expected="one h1 per page naming its subject",
            affected_urls=[p.url for p in no_h1],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Give every page exactly one h1 that names its subject",
                priority="medium",
                effort="low",
                steps=[
                    "Promote the page's visible title to an h1 rather than a styled div.",
                    "Keep the h1 text identical to what the page is actually about, not the brand name.",
                ],
                mechanism=(
                    "The h1 is the one element every reader - human scanning, screen reader, "
                    "or extractor - uses to decide what the page is. Its absence forces "
                    "everyone to reconstruct the subject from body copy."
                ),
                verification="Every sampled page reports h1_count of 1.",
            ),
        ))

    skipping = [p for p in pages if len(p.signals["heading_skips"]) >= 2]
    if skipping:
        page = skipping[0]
        first_skip = page.signals["heading_skips"][0]
        findings.append(Finding(
            title="Heading levels skip, so the page outline does not describe its structure",
            severity="low",
            evidence=with_urls(
                f"{len(skipping)} of {len(pages)} sampled pages skip heading levels two or more "
                f"times (e.g. h{first_skip[0]} followed directly by h{first_skip[1]}, "
                f"{len(page.signals['heading_skips'])} skips on that page)",
                [p.url for p in skipping],
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="EN004b",
            observed=f"h{first_skip[0]} -> h{first_skip[1]} and {len(page.signals['heading_skips']) - 1} further skip(s)",
            expected="heading levels descend one step at a time",
            affected_urls=[p.url for p in skipping],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Choose heading levels by document structure, not by font size",
                priority="low",
                effort="low",
                steps=[
                    "Pick the level from the nesting depth of the section.",
                    "Style headings with CSS classes when a smaller size is wanted at a higher level.",
                ],
                mechanism=(
                    "The heading tree is the page's table of contents. A tree with missing "
                    "levels reads as a flat list, which removes the grouping a scanner relies on."
                ),
                verification="No sampled page reports a heading-level skip.",
            ),
        ))

    # -- EN005: text-to-chrome ratio (handout appendix F) ------------------
    ratio_candidates = [
        p for p in pages
        if not exempt(p) and p.signals["total_word_count"] >= MIN_WORDS_FOR_RATIO
    ]
    filler_heavy = [p for p in ratio_candidates if p.signals["boilerplate_ratio"] >= BOILERPLATE_LIMIT]
    if filler_heavy:
        page = max(filler_heavy, key=lambda p: p.signals["boilerplate_ratio"])
        findings.append(Finding(
            title="Substance is outweighed by surrounding chrome",
            severity="medium",
            evidence=with_urls(
                f"{len(filler_heavy)} of {len(ratio_candidates)} sampled pages carry "
                f"{int(BOILERPLATE_LIMIT * 100)}%+ of their text outside the main content region - "
                f"worst is {page.url} at {page.signals['boilerplate_ratio']:.0%} "
                f"({page.signals['word_count']} words of content inside "
                f"{page.signals['total_word_count']} total)",
                [p.url for p in filler_heavy],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN005",
            observed=f"boilerplate ratio {page.signals['boilerplate_ratio']:.2f}",
            expected="the main content region carries the majority of the page's words",
            affected_urls=[p.url for p in filler_heavy],
            sample_size=len(ratio_candidates),
            suggested_action=SuggestedAction(
                summary="Cut repeated chrome and let the main content dominate the page",
                priority="medium",
                effort="medium",
                steps=[
                    "Reduce mega-menus, repeated footers and promotional rails that appear on every page.",
                    "Wrap the actual content in <main> so it is unambiguous which region matters.",
                    "Move secondary link farms behind a disclosure rather than rendering them inline.",
                ],
                mechanism=(
                    "Interpretation, hedged: handout appendix F describes substance surrounded "
                    "by low-value filler. A reader arriving with one question has to find the "
                    "answer inside a page that is mostly navigation, and a summariser weighs "
                    "the filler alongside the content."
                ),
                verification="boilerplate_ratio falls below 0.6 on the listed pages.",
            ),
        ))

    # -- EN006: substance carried only in unlabelled images ----------------
    image_heavy = [
        p for p in pages
        if not exempt(p)
        and p.signals["images"] >= 6
        and p.signals["images_missing_alt"] >= 4
        and p.signals["images_missing_alt"] / max(1, p.signals["images"]) >= 0.6
        and p.signals["word_count"] < 300
    ]
    if image_heavy:
        page = image_heavy[0]
        findings.append(Finding(
            title="Pages carry their substance in images with no text alternative",
            severity="medium",
            evidence=with_urls(
                f"{len(image_heavy)} of {len(pages)} sampled pages combine "
                f"{page.signals['images']} images ({page.signals['images_missing_alt']} with no "
                f"alt text) with only {page.signals['word_count']} words of body content; on "
                f"these pages the message is carried by pictures that no text reader receives",
                [p.url for p in image_heavy],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN006",
            observed=f"{page.signals['images_missing_alt']}/{page.signals['images']} images without alt, {page.signals['word_count']} words",
            expected="the page's claims exist as text, with alt attributes on informative images",
            affected_urls=[p.url for p in image_heavy],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Put the claim in text and describe informative images",
                priority="medium",
                effort="medium",
                steps=[
                    "Write out, as body copy, any specification, price or comparison that currently only appears inside an image.",
                    "Add alt text to informative images; keep alt=\"\" for genuinely decorative ones.",
                    "Prefer real text over text baked into a graphic.",
                ],
                mechanism=(
                    "Interpretation, hedged, and honestly limited: raw HTML cannot distinguish "
                    "a decorative image correctly marked alt=\"\" from an informative one left "
                    "unlabelled. The finding fires only when a low word count coincides with "
                    "many unlabelled images, which is the pattern where the message is "
                    "genuinely image-borne."
                ),
                verification="Body copy states the claims; informative images carry alt text.",
            ),
        ))

    # -- EN007: form friction on conversion paths --------------------------
    conversion_pages = [
        p for p in pages
        if expectation(p).get("engagement_critical") and p.signals["max_fields"] >= FORM_FIELD_FRICTION
    ]
    if conversion_pages:
        page = max(conversion_pages, key=lambda p: p.signals["max_fields"])
        worst = max(page.signals["forms"], key=lambda f: f["fields"])
        findings.append(Finding(
            title="The primary conversion form asks for a lot before giving anything",
            severity="medium" if worst["fields"] >= 8 else "low",
            evidence=with_urls(
                f"{len(conversion_pages)} conversion-critical page(s) carry a form with "
                f"{FORM_FIELD_FRICTION}+ user-facing fields - the largest is "
                f"{worst['fields']} fields ({worst['required']} marked required) on {page.url}",
                [p.url for p in conversion_pages],
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="EN007a",
            observed=f"{worst['fields']} fields, {worst['required']} required",
            expected="the shortest form that lets the next step happen",
            affected_urls=[p.url for p in conversion_pages],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Cut the first form to the fields you genuinely act on",
                priority="medium",
                effort="low",
                steps=[
                    "Remove any field whose value is not used before the first human contact.",
                    "Move qualification questions to a second step, after the visitor has received something.",
                    "Mark optional fields optional rather than making everything required.",
                ],
                mechanism=(
                    "Every field is a decision the visitor makes before receiving value. A "
                    "visitor who arrived from an AI answer is mid-evaluation, not mid-purchase, "
                    "and has less committed attention than one who navigated in deliberately."
                ),
                verification="The primary form is under six fields.",
            ),
        ))

    unlabelled_forms = [
        p for p in pages
        if any(f["unlabelled"] >= 2 for f in p.signals["forms"])
    ]
    if unlabelled_forms:
        page = unlabelled_forms[0]
        worst = max(page.signals["forms"], key=lambda f: f["unlabelled"])
        findings.append(Finding(
            title="Form fields have no label, aria-label or placeholder",
            severity="low",
            evidence=with_urls(
                f"{len(unlabelled_forms)} sampled page(s) contain a form where "
                f"{worst['unlabelled']} of {worst['fields']} fields carry no <label for>, "
                f"aria-label, aria-labelledby, placeholder or title attribute",
                [p.url for p in unlabelled_forms],
            ),
            layer=LAYER,
            claim_level=1,
            confidence="high",
            check_id="EN007b",
            observed=f"{worst['unlabelled']}/{worst['fields']} fields unlabelled",
            expected="every field is named in the markup",
            affected_urls=[p.url for p in unlabelled_forms],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Label every form field in markup",
                priority="low",
                effort="low",
                steps=[
                    "Add <label for=\"field-id\"> to each input, or an aria-label where a visible label is not wanted.",
                    "Do not rely on adjacent text or CSS positioning to convey what a field is.",
                ],
                mechanism=(
                    "An unlabelled field is unusable by assistive technology and ambiguous to "
                    "everyone else once the placeholder disappears on focus."
                ),
                verification="No form reports unlabelled fields.",
            ),
        ))

    # -- EN008: dead ends (no page-specific next step) ---------------------
    if sufficient_sample:
        link_frequency = Counter()
        for page in pages:
            link_frequency.update(set(page.signals["internal_links"]))
        chrome_threshold = max(2, math.ceil(0.8 * len(pages)))
        chrome_links = {href for href, count in link_frequency.items() if count >= chrome_threshold}

        dead_ends = []
        for page in pages:
            if expectation(page).get("exempt_from_absorption"):
                continue  # a privacy policy is allowed to be a leaf
            own = normalise_href(page.url)
            contextual = {h for h in page.signals["internal_links"] if h not in chrome_links and h != own}
            if not contextual:
                dead_ends.append(page)

        if dead_ends:
            findings.append(Finding(
                title="Pages offer no next step beyond the site-wide navigation",
                severity="medium",
                evidence=with_urls(
                    f"{len(dead_ends)} of {len(pages)} sampled pages contain zero internal links "
                    f"that are not already present on {chrome_threshold}+ of the {len(pages)} "
                    f"sampled pages; every link on them is site-wide chrome, so the page itself "
                    f"proposes nothing to read next",
                    [p.url for p in dead_ends],
                ),
                layer=LAYER,
                claim_level=2,
                confidence="medium",
                check_id="EN008",
                observed=f"{len(dead_ends)} pages with 0 contextual internal links",
                expected="each page links onward to the specific pages that continue its subject",
                affected_urls=[p.url for p in dead_ends],
                sample_size=len(pages),
                suggested_action=SuggestedAction(
                    summary="Add contextual links that continue the visitor's specific question",
                    priority="medium",
                    effort="low",
                    steps=[
                        "End each page with two or three links to the pages a reader of THIS page would want next.",
                        "Link from within the body copy where a concept is first named.",
                        "Do not count the header and footer as next steps - they are the same on every page.",
                    ],
                    mechanism=(
                        "Comparative observation across the sampled pages: links common to "
                        "nearly every page are navigation, not continuation. A page whose only "
                        "outbound paths are chrome forces the visitor back to browsing, which "
                        "is where a mid-funnel arrival leaves."
                    ),
                    verification="Every content page carries at least two page-specific internal links.",
                ),
            ))
    else:
        notes.append(
            f"EN008 (contextual next-step analysis) needs at least "
            f"{config.min_sample_for_population_claim} pages to separate site chrome from "
            f"page-specific links; the sample of {len(pages)} was too small, so the check "
            "was skipped rather than guessed at"
        )

    # -- EN009: structural proxies for perceived speed and stability -------
    blocking_pages = [p for p in pages if p.signals["render_blocking_scripts"] >= RENDER_BLOCKING_LIMIT
                      or p.signals["head_stylesheets"] >= STYLESHEET_LIMIT]
    if blocking_pages:
        page = max(blocking_pages, key=lambda p: p.signals["render_blocking_scripts"])
        findings.append(Finding(
            title="Render-blocking resources in <head> (structural proxy, not a measurement)",
            severity="medium",
            evidence=with_urls(
                f"{len(blocking_pages)} of {len(pages)} sampled pages load "
                f"{page.signals['render_blocking_scripts']} synchronous scripts and "
                f"{page.signals['head_stylesheets']} stylesheets in <head> before any content can "
                f"paint. This is a count of markup, not a Core Web Vitals measurement - no timing "
                f"was observed and none is claimed",
                [p.url for p in blocking_pages],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN009a",
            observed=f"{page.signals['render_blocking_scripts']} sync scripts, {page.signals['head_stylesheets']} stylesheets in head",
            expected="scripts deferred or async; critical CSS inlined, the rest loaded without blocking",
            affected_urls=[p.url for p in blocking_pages],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Defer head scripts and cut the blocking stylesheet chain",
                priority="medium",
                effort="medium",
                steps=[
                    "Add defer (or type=\"module\") to scripts in <head> that are not needed before first paint.",
                    "Consolidate stylesheets and inline the critical rules.",
                    "Measure the actual effect with a real performance tool - this check cannot.",
                ],
                mechanism=(
                    "Mechanism interpretation and an explicit proxy: each blocking resource in "
                    "<head> is a round trip before anything renders. The real metric requires "
                    "a browser, so treat this as a pointer to where to measure, not as the "
                    "measurement."
                ),
                verification="A real performance measurement on these URLs, after deferring the scripts.",
            ),
        ))

    shifty = [
        p for p in pages
        if p.signals["img_tags"] >= 5
        and p.signals["img_without_dimensions"] / max(1, p.signals["img_tags"]) >= 0.8
    ]
    if shifty:
        page = max(shifty, key=lambda p: p.signals["img_without_dimensions"])
        findings.append(Finding(
            title="Images declare no intrinsic dimensions (layout-shift proxy, not a measurement)",
            severity="low",
            evidence=with_urls(
                f"{len(shifty)} of {len(pages)} sampled pages serve images without width/height "
                f"attributes or a CSS aspect-ratio - worst is {page.url} with "
                f"{page.signals['img_without_dimensions']} of {page.signals['img_tags']} images. "
                f"Layout shift itself was not measured and is not being claimed",
                [p.url for p in shifty],
            ),
            layer=LAYER,
            claim_level=3,
            confidence="medium",
            check_id="EN009b",
            observed=f"{page.signals['img_without_dimensions']}/{page.signals['img_tags']} images with no declared dimensions",
            expected="width and height attributes, or an aspect-ratio, on every image",
            affected_urls=[p.url for p in shifty],
            sample_size=len(pages),
            suggested_action=SuggestedAction(
                summary="Declare width and height on images so space is reserved before they load",
                priority="low",
                effort="low",
                steps=[
                    "Add the intrinsic width and height attributes to <img> tags; CSS can still resize them.",
                    "For responsive art direction, set an aspect-ratio in CSS instead.",
                ],
                mechanism=(
                    "Structural proxy, hedged: without dimensions the browser cannot reserve "
                    "space, so content moves as images arrive. Whether that produces a bad CLS "
                    "score depends on the runtime, which this audit does not execute."
                ),
                verification="A real Core Web Vitals measurement, after the attributes are added.",
            ),
        ))

    metrics = {
        "pages_analysed": len(pages),
        "median_boilerplate_ratio": sorted(p.signals["boilerplate_ratio"] for p in pages)[len(pages) // 2] if pages else None,
        "pages_without_h1": len(no_h1),
        "pages_with_heading_skips": len(skipping),
        "pages_with_interstitial_markup": sum(1 for p in pages if p.signals["interstitial_strong"] or p.signals["interstitial_weak"]),
        "pages_with_blocking_interstitial": len(blocked),
        "pages_with_forms": sum(1 for p in pages if p.signals["forms"]),
        "max_form_fields": max((p.signals["max_fields"] for p in pages), default=0),
        "median_render_blocking_scripts": sorted(p.signals["render_blocking_scripts"] for p in pages)[len(pages) // 2] if pages else None,
        "images_without_dimensions": sum(p.signals["img_without_dimensions"] for p in pages),
        "image_tags_total": sum(p.signals["img_tags"] for p in pages),
        "page_types_seen": dict(Counter(p.page_type for p in pages)),
        "core_web_vitals": "not measured - raw HTML cannot supply timing; EN009a/EN009b are structural proxies only",
    }
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
        f"engagement-audit  layer={LAYER}",
        f"  pages analysed      : {payload['pages_analysed']}",
        f"  pages without h1    : {metrics.get('pages_without_h1', 0)}",
        f"  interstitial markup : {metrics.get('pages_with_interstitial_markup', 0)}"
        f" (blocking: {metrics.get('pages_with_blocking_interstitial', 0)})",
        f"  median boilerplate  : {metrics.get('median_boilerplate_ratio')}",
        f"  findings            : {len(payload['findings'])}",
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
        lines.append("    no engagement-layer defects detected in the sampled pages")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_engagement.py",
        description="Engagement-layer audit: orientation, continuity and friction for the "
                    "visitor who arrives. Reads saved HTML from the audit workspace; never fetches.",
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
        brand_token = (scope_domain.split(".")[0] if scope_domain else "").lower()
        pages, notes = load_pages(ws, scope_domain or site_host)

        if pages:
            config.calibrate_to_site([p.doc.structure_summary() for p in pages])
            for page in pages:
                annotate(page, scope_domain, brand_token)
            findings, metrics, check_notes = check_engagement(pages, config, scope_domain)
            notes.extend(check_notes)
        else:
            findings, metrics = [], {"pages_analysed": 0}
            notes.append(
                "no page in the reachability inventory had readable saved HTML; "
                "the engagement layer made no observations"
            )

        findings = drop_unevidenced(findings, notes)
        payload = {
            "layer": LAYER,
            "skill": "engagement-audit",
            "site": site_host,
            "scope_domain": scope_domain,
            "findings": [f.to_dict() for f in findings],
            "metrics": metrics,
            "pages_analysed": len(pages),
            "notes": notes,
            "thresholds": {
                "lead_words": LEAD_WORDS,
                "boilerplate_ratio_limit": BOILERPLATE_LIMIT,
                "form_field_friction": FORM_FIELD_FRICTION,
                "render_blocking_limit": RENDER_BLOCKING_LIMIT,
                "min_sample_for_population_claim": config.min_sample_for_population_claim,
            },
        }
        ws.write(LAYER, payload)
    except Exception as exc:  # noqa: BLE001
        print(f"check_engagement: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else human_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
