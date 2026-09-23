"""JavaScript render-fault detection, without running JavaScript.

Handout appendix C: "A page that looks complete to a person isn't always
complete to a machine. Some content is only assembled after a page loads."
This module identifies *which* fault a page has, because the fixes are
completely different:

===================== ===========================================
Fault                 Fix
===================== ===========================================
``empty_shell``       Server-side render or prerender the route
``state_blob``        The content exists in HTML but only inside a
                      JSON payload; hydrate it into real markup
``lazy_content``      Move above-the-fold content out of the
                      intersection-observer path
``meta_only``         Metadata is server-rendered but the body is
                      not — the page previews well and reads empty
``noscript_gap``      No fallback at all for a non-executing client
``client_routing``    Routes resolve only in the browser
===================== ===========================================

The distinction matters for severity too. ``state_blob`` is recoverable — a
sufficiently determined parser can find the text. ``empty_shell`` is not: there
is nothing on the wire to extract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Framework state containers. When a page's copy lives in one of these, the
# text is on the wire but not in the markup.
STATE_BLOB_PATTERNS = {
    "Next.js":      re.compile(r"__NEXT_DATA__|_next/static|self\.__next_f", re.I),
    "Nuxt":         re.compile(r"__NUXT__|window\.__NUXT|/_nuxt/", re.I),
    "Gatsby":       re.compile(r"___gatsby|window\.page-data|/page-data/", re.I),
    "Redux/Vuex":   re.compile(r"__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__", re.I),
    "SvelteKit":    re.compile(r"__sveltekit_|/_app/immutable/", re.I),
    "Remix":        re.compile(r"__remixContext|__remixManifest", re.I),
    "Angular":      re.compile(r"ng-version=|<app-root|ngcontent", re.I),
    "Astro":        re.compile(r"astro-island|astro-slot", re.I),
}

CLIENT_FRAMEWORK_PATTERNS = {
    "React":  re.compile(r"react(?:-dom)?[.@\-/]|data-reactroot|__reactContainer", re.I),
    "Vue":    re.compile(r"\bvue(?:\.runtime)?[.@\-/]|data-v-[0-9a-f]{8}|v-cloak", re.I),
    "Angular": re.compile(r"@angular|ng-version", re.I),
    "Svelte": re.compile(r"\bsvelte[.@\-/]", re.I),
    "Ember":  re.compile(r"ember(?:-cli)?[.@\-/]", re.I),
}

# Mount points that are empty in the served HTML.
EMPTY_MOUNT_RE = re.compile(
    r"<(?:div|main|section|app-root)[^>]*\b(?:id|class)\s*=\s*[\"']"
    r"(root|app|__next|___gatsby|__nuxt|main-content|application)[\"'][^>]*>\s*</(?:div|main|section|app-root)>",
    re.I,
)
SELF_CLOSING_MOUNT_RE = re.compile(
    r"<(?:div|app-root)[^>]*\b(?:id|class)\s*=\s*[\"'](root|app|__next|___gatsby)[\"'][^>]*/?>\s*<script",
    re.I,
)

LAZY_ATTR_RE = re.compile(
    r'\b(?:loading\s*=\s*["\']lazy["\']|data-src\s*=|data-lazy|'
    r'data-defer|IntersectionObserver)', re.I)

SKELETON_RE = re.compile(
    r'\b(?:skeleton|placeholder|shimmer|is-loading|spinner|'
    r'loading-state|content-loader)\b', re.I)

CLIENT_ROUTER_RE = re.compile(
    r"\b(?:react-router|vue-router|@angular/router|history\.pushState|"
    r"createBrowserRouter|useNavigate)\b", re.I)

HASH_ROUTE_RE = re.compile(r'href\s*=\s*["\']#!?/', re.I)


@dataclass
class RenderFault:
    """One diagnosed render problem."""

    code: str
    severity: str
    detail: str
    recoverable: bool
    fix: str
    evidence: dict = field(default_factory=dict)


@dataclass
class RenderReport:
    """Full render-legibility picture for one page."""

    url: str = ""
    frameworks: list[str] = field(default_factory=list)
    state_blobs: list[str] = field(default_factory=list)
    faults: list[RenderFault] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    recoverable_text_chars: int = 0

    @property
    def has_blocking_fault(self) -> bool:
        return any(f.severity in ("critical", "high") for f in self.faults)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "frameworks_detected": self.frameworks,
            "state_containers": self.state_blobs,
            "metrics": self.metrics,
            "recoverable_text_chars": self.recoverable_text_chars,
            "faults": [
                {
                    "code": f.code, "severity": f.severity, "detail": f.detail,
                    "recoverable": f.recoverable, "fix": f.fix, "evidence": f.evidence,
                }
                for f in self.faults
            ],
        }


def _extract_json_text(html: str, limit: int = 400_000) -> int:
    """Approximate how much human-readable text is trapped inside JSON blobs.

    If a page renders empty but ships 40 KB of readable strings in
    ``__NEXT_DATA__``, the copy exists — it is just not in the markup. That is
    a materially different (and easier) fix than content that never left the
    server, so it is worth measuring rather than guessing.
    """
    total = 0
    for match in re.finditer(
        r"<script[^>]*>\s*(?:window\.\w+\s*=\s*)?(\{.*?\})\s*;?\s*</script>",
        html[:limit], re.S,
    ):
        payload = match.group(1)
        if len(payload) < 40:
            continue
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, RecursionError):
            # Not valid JSON on its own; approximate with quoted-string length.
            total += sum(len(s) for s in re.findall(r'"([^"\\]{25,})"', payload))
            continue
        stack, seen = [parsed], 0
        while stack and seen < 8000:
            node = stack.pop()
            seen += 1
            if isinstance(node, str):
                if len(node) >= 25 and " " in node:
                    total += len(node)
            elif isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return total


def diagnose(html: str, doc, *, url: str = "") -> RenderReport:
    """Diagnose render faults for one page from its raw HTML and parsed doc."""
    report = RenderReport(url=url or doc.url)

    for name, pattern in CLIENT_FRAMEWORK_PATTERNS.items():
        if pattern.search(html):
            report.frameworks.append(name)
    for name, pattern in STATE_BLOB_PATTERNS.items():
        if pattern.search(html):
            report.state_blobs.append(name)

    words = doc.total_word_count
    html_chars = max(1, len(html))
    text_chars = len(doc.text)
    text_ratio = text_chars / html_chars
    trapped = _extract_json_text(html)
    report.recoverable_text_chars = trapped

    empty_mount = bool(EMPTY_MOUNT_RE.search(html) or SELF_CLOSING_MOUNT_RE.search(html))
    skeletons = len(SKELETON_RE.findall(html))
    lazy_markers = len(LAZY_ATTR_RE.findall(html))

    report.metrics = {
        "visible_words": words,
        "visible_text_chars": text_chars,
        "html_chars": html_chars,
        "text_to_html_ratio": round(text_ratio, 4),
        "inline_script_chars": doc.inline_script_chars,
        "script_tags": doc.script_tags,
        "noscript_chars": doc.noscript_chars,
        "empty_mount_point": empty_mount,
        "skeleton_markers": skeletons,
        "lazy_markers": lazy_markers,
        "text_trapped_in_json_chars": trapped,
        "headings": doc.heading_count,
        "paragraphs": doc.paragraph_count,
    }

    # -- fault classification -------------------------------------------
    # Ordered from most to least severe; a page can carry several.

    # Order matters: when the copy is recoverable from a state payload the fix is
    # "hydrate it into markup", which is far cheaper than "server-render this
    # route". Diagnose that case first even though the mount point is also empty.
    trapped_is_meaningful = trapped >= 500

    if trapped_is_meaningful and words < 120:
        report.faults.append(RenderFault(
            code="state_blob",
            severity="high",
            detail=(
                f"Only {words} visible words in the markup, with roughly "
                f"{trapped:,} characters of readable text inside client state "
                f"payloads ({', '.join(report.state_blobs) or 'inline JSON'}). "
                "The content reaches the browser but is not addressable as markup."
            ),
            recoverable=True,
            fix=(
                "Emit the content that currently lives in the client state payload "
                "as real HTML elements. The text already ships with the page; it "
                "simply is not in a form a non-executing reader can extract."
            ),
            evidence={
                "visible_words": words,
                "text_trapped_in_json_chars": trapped,
                "state_containers": report.state_blobs,
                "empty_mount_point": empty_mount,
            },
        ))

    elif empty_mount and words < 60:
        report.faults.append(RenderFault(
            code="empty_shell",
            severity="critical" if trapped < 500 else "high",
            detail=(
                f"The served HTML contains an empty mount point and only {words} "
                f"visible words. A crawler that does not execute JavaScript "
                f"receives essentially no content."
            ),
            recoverable=trapped >= 500,
            fix=(
                "Server-side render or prerender this route so the primary content "
                "is present in the initial HTML response."
            ),
            evidence={"visible_words": words, "empty_mount_point": True},
        ))

    elif words < 120 and doc.inline_script_chars > 4 * max(1, text_chars):
        report.faults.append(RenderFault(
            code="script_heavy_shell",
            severity="high",
            detail=(
                f"Only {words} visible words in the markup against "
                f"{doc.inline_script_chars:,} characters of inline script, with no "
                "recoverable text in the payload."
            ),
            recoverable=False,
            fix="Server-side render the primary content for this route.",
            evidence={
                "visible_words": words,
                "inline_script_chars": doc.inline_script_chars,
                "state_containers": report.state_blobs,
            },
        ))

    elif words < 120 and text_ratio < 0.05:
        report.faults.append(RenderFault(
            code="thin_served_html",
            severity="high",
            detail=(
                f"Visible text is {text_ratio:.1%} of the served HTML "
                f"({words} words in {html_chars:,} characters)."
            ),
            recoverable=False,
            fix="Ensure the primary content is present in the server response.",
            evidence={"text_to_html_ratio": round(text_ratio, 4), "visible_words": words},
        ))

    # Metadata renders but body does not: previews well, reads empty.
    meta_desc = doc.meta_get("description", "og:description")
    already_diagnosed = {f.code for f in report.faults} & {
        "empty_shell", "state_blob", "script_heavy_shell", "thin_served_html"
    }
    if not already_diagnosed and words < 100 and (doc.title or meta_desc) and doc.paragraph_count == 0:
        report.faults.append(RenderFault(
            code="meta_only",
            severity="medium",
            detail=(
                "Title and meta description are server-rendered but the page body "
                f"has no paragraph content ({words} visible words). The page will "
                "preview correctly in a link unfurl while offering an answer engine "
                "nothing to quote."
            ),
            recoverable=False,
            fix="Render the body content server-side alongside the metadata.",
            evidence={"title": doc.title[:120], "visible_words": words},
        ))

    if skeletons >= 3 and words < 200:
        report.faults.append(RenderFault(
            code="lazy_content",
            severity="medium",
            detail=(
                f"{skeletons} loading-placeholder markers with only {words} visible "
                "words: the primary content is deferred behind a client-side load."
            ),
            recoverable=False,
            fix=(
                "Render above-the-fold content directly and reserve lazy loading for "
                "below-the-fold media."
            ),
            evidence={"skeleton_markers": skeletons, "visible_words": words},
        ))

    if lazy_markers >= 10 and doc.images and doc.images_missing_alt / max(1, doc.images) > 0.5:
        report.faults.append(RenderFault(
            code="lazy_media_unlabelled",
            severity="low",
            detail=(
                f"{lazy_markers} lazy-loading markers and {doc.images_missing_alt} of "
                f"{doc.images} images without alt text. Deferred, unlabelled media is "
                "invisible to any non-executing reader."
            ),
            recoverable=True,
            fix="Add alt text so deferred images still carry meaning as text.",
            evidence={"lazy_markers": lazy_markers, "images_missing_alt": doc.images_missing_alt},
        ))

    if report.frameworks and doc.noscript_chars == 0 and words < 150:
        report.faults.append(RenderFault(
            code="noscript_gap",
            severity="medium",
            detail=(
                f"A client-side framework ({', '.join(report.frameworks)}) is present "
                "with no <noscript> fallback and little server-rendered text."
            ),
            recoverable=False,
            fix=(
                "Provide a <noscript> fallback carrying the page's core claim, or "
                "server-render the route."
            ),
            evidence={"frameworks": report.frameworks, "noscript_chars": 0},
        ))

    if HASH_ROUTE_RE.search(html) and CLIENT_ROUTER_RE.search(html):
        report.faults.append(RenderFault(
            code="client_routing",
            severity="high",
            detail=(
                "Navigation uses hash-fragment routes with a client-side router. "
                "Fragments are never sent to the server, so every route resolves to "
                "the same document and no sub-page can be cited independently."
            ),
            recoverable=False,
            fix="Move to real path-based URLs that each return their own HTML document.",
            evidence={"hash_routes": True},
        ))

    return report


def summarise_site(reports: list[RenderReport]) -> dict:
    """Roll page-level render reports into a site-level picture."""
    if not reports:
        return {"pages_analysed": 0}

    by_code: dict[str, int] = {}
    for report in reports:
        for fault in report.faults:
            by_code[fault.code] = by_code.get(fault.code, 0) + 1

    affected = [r for r in reports if r.faults]
    blocking = [r for r in reports if r.has_blocking_fault]
    frameworks: dict[str, int] = {}
    for report in reports:
        for name in report.frameworks:
            frameworks[name] = frameworks.get(name, 0) + 1

    return {
        "pages_analysed": len(reports),
        "pages_with_faults": len(affected),
        "pages_with_blocking_faults": len(blocking),
        "blocking_rate": round(len(blocking) / len(reports), 3),
        "fault_counts": dict(sorted(by_code.items(), key=lambda kv: -kv[1])),
        "frameworks": frameworks,
        "median_visible_words": sorted(r.metrics.get("visible_words", 0) for r in reports)[len(reports) // 2],
    }
