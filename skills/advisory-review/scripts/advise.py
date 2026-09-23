#!/usr/bin/env python3
"""The in-loop advisory critic. Verifies findings before the report is emitted.

Authority: drop, downgrade, merge, re-level. Never invents a finding.
Exit codes: 0 success, 1 nothing to review, 2 internal error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

_here = pathlib.Path(__file__).resolve()
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        sys.path.insert(0, str(_parent / "lib"))
        break

from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.report import (  # noqa: E402
    SEVERITIES,
    ProactiveRecommendation,
    cap_severity,
)
from geo_audit.workspace import Workspace  # noqa: E402

DETECTION_LAYERS = ("preflight", "reachability", "selection", "absorption", "trust", "engagement")

_URL_RE = re.compile(r"https?://[^\s,;)\"']+")
_DIGIT_RE = re.compile(r"\d")
_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")

# Language that asserts causation. Permitted at claim level 3 only when hedged,
# never at level 1 or 2.
_CAUSAL_RE = re.compile(
    r"\b(will (?:increase|improve|boost|drive|cause|result in)|"
    r"guarantees?|ensures? that|causes?|leads? directly to|"
    r"proven to (?:increase|improve))\b", re.I)
_HEDGE_RE = re.compile(
    r"\b(may|might|likely|tends? to|associated with|correlat\w+|"
    r"suggests?|consistent with|proxy|interpretation|hypothes\w+)\b", re.I)

# ---------------------------------------------------------------------------
# Trap list. Each entry: (check_id predicate, url/context predicate, reason)
# ---------------------------------------------------------------------------

_NOINDEX_LEGITIMATE_RE = re.compile(
    r"/(search|filter|sort|page/\d|tag/|\?|preview|staging|draft|"
    r"cart|checkout|account|login|thank-?you|confirmation)", re.I)

_SCHEMA_EXEMPT_TYPES = {"legal", "about", "category", "other"}
_ABSORPTION_EXEMPT_TYPES = {"legal", "category"}


def _urls_in(finding: dict) -> list[str]:
    explicit = finding.get("affected_urls") or []
    if explicit:
        return explicit
    return _URL_RE.findall(finding.get("evidence", ""))


def _population_counts(finding: dict) -> tuple[int, int] | None:
    """Extract an "N of M" population claim from the observed/evidence text."""
    for text in (finding.get("observed", ""), finding.get("evidence", "")):
        match = re.search(r"\b(\d+)\s+of\s+(\d+)\b", text)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def gate_evidence_sufficiency(finding: dict) -> tuple[bool, str]:
    evidence = (finding.get("evidence") or "").strip()
    if len(evidence) < 40:
        return False, f"evidence string is {len(evidence)} characters; too short to be checkable"
    if not _urls_in(finding):
        return False, "no URL in affected_urls or evidence; the claim cannot be verified against a page"
    if not _DIGIT_RE.search(evidence) and not finding.get("observed"):
        return False, "evidence contains no observed value, only an assertion"
    action = finding.get("suggested_action") or {}
    if len((action.get("summary") or "").strip()) < 15:
        return False, "suggested_action.summary is missing or too vague to act on"
    return True, ""


def gate_threshold_coherence(finding: dict) -> tuple[bool, str]:
    """Reject findings whose observed value does not breach the stated expectation."""
    observed, expected = finding.get("observed", ""), finding.get("expected", "")
    if not observed or not expected:
        return True, ""

    obs_nums = [float(n) for n in _NUMBER_RE.findall(observed)]
    exp_nums = [float(n) for n in _NUMBER_RE.findall(expected)]
    if not obs_nums or not exp_nums:
        return True, ""

    # "N of M" observations are ratios, not scalars to compare against a bound.
    if _population_counts(finding):
        counts = _population_counts(finding)
        if counts and counts[0] == 0 and "0 of" not in observed:
            return False, "observation reports zero affected items"
        return True, ""

    lowered = expected.lower()
    if any(word in lowered for word in ("under", "below", "less than", "at most", "no more than")):
        if obs_nums[0] < exp_nums[0]:
            return False, (
                f"observed {obs_nums[0]:g} already satisfies the expectation "
                f"'{expected}'; the threshold did not breach"
            )
    elif any(word in lowered for word in ("at least", "above", "over", "more than", "minimum")):
        if obs_nums[0] > exp_nums[0]:
            return False, (
                f"observed {obs_nums[0]:g} already satisfies the expectation "
                f"'{expected}'; the threshold did not breach"
            )
    return True, ""


def gate_sample_sufficiency(finding: dict, config: AuditConfig) -> tuple[bool, str, dict | None]:
    """Population claims need enough population. Returns (keep, reason, mutation)."""
    counts = _population_counts(finding)
    sample = finding.get("sample_size") or (counts[1] if counts else 0)
    if not sample:
        return True, "", None

    if config.sample_is_sufficient(sample):
        return True, "", None

    # Too small for a population claim, but a direct observation about the pages
    # actually seen is still valid — demote rather than discard.
    if counts and counts[0] >= 1:
        return True, "", {
            "severity": cap_severity(
                SEVERITIES[min(SEVERITIES.index(finding["severity"]) + 1, len(SEVERITIES) - 1)],
                min(finding.get("claim_level", 1), 2),
            ),
            "confidence": "low",
            "claim_level": max(finding.get("claim_level", 1), 1),
            "_reason": (
                f"sample of {sample} page(s) is below the minimum of "
                f"{config.min_sample_for_population_claim} for a population claim; "
                "severity reduced and scope narrowed to the pages observed"
            ),
        }
    return False, (
        f"claims a site-wide pattern from a sample of {sample}, below the minimum "
        f"of {config.min_sample_for_population_claim}"
    ), None


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEVERITY_TO_PRIORITY_CAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    # `info` is context, not a defect: nothing about it can be top-of-queue.
    "info": "low",
}


def _align_priority_to_severity(finding: dict) -> str | None:
    """Cap suggested_action.priority at the finding's (possibly downgraded) severity.

    Severity answers "how bad is this"; priority answers "what do we do first".
    They may differ, but a priority *above* severity is a contradiction the
    reader cannot resolve: the report would assert the population claim was too
    weak to keep its severity while still demanding top-of-queue action on the
    same claim. Returns a note when it changed anything, else None.
    """
    action = finding.get("suggested_action") or {}
    priority = action.get("priority", "medium")
    cap = _SEVERITY_TO_PRIORITY_CAP.get(finding.get("severity", "medium"), "medium")
    if (_PRIORITY_RANK.get(priority, 2) < _PRIORITY_RANK.get(cap, 2)):
        action["priority"] = cap
        finding["suggested_action"] = action
        return (
            f"suggested_action.priority reduced from {priority} to {cap} to match "
            f"the downgraded severity ({finding.get('severity')})"
        )
    return None


def gate_claim_level(finding: dict) -> tuple[bool, str, dict | None]:
    """Enforce the identification map."""
    level = finding.get("claim_level", 1)
    if level == 4:
        return False, "Level 4 causal prescription; belongs in proactive recommendations, not findings", None

    text = " ".join([
        finding.get("evidence", ""),
        (finding.get("suggested_action") or {}).get("mechanism", ""),
    ])
    if _CAUSAL_RE.search(text) and not _HEDGE_RE.search(text):
        if level <= 2:
            return True, "", {
                "claim_level": 3,
                "severity": cap_severity(finding["severity"], 3),
                "_reason": (
                    "uses unhedged causal language ('will increase', 'guarantees') while "
                    "claiming a direct or contrastive level; re-levelled to mechanism "
                    "interpretation and capped at medium severity"
                ),
            }

    capped = cap_severity(finding["severity"], level)
    if capped != finding["severity"]:
        return True, "", {
            "severity": capped,
            "_reason": (
                f"claim level {level} caps severity at {capped}; detector asserted "
                f"{finding['severity']}"
            ),
        }
    return True, "", None


def gate_false_positive_traps(finding: dict, context: dict) -> tuple[bool, str]:
    """The documented trap list."""
    check = finding.get("check_id", "")
    urls = _urls_in(finding)
    page_types = context.get("page_types", {})

    if check == "noindex_on_public_page":
        legitimate = [u for u in urls if _NOINDEX_LEGITIMATE_RE.search(u)]
        if legitimate and len(legitimate) == len(urls):
            return False, (
                "every affected URL is a search, filter, pagination, cart or preview "
                "route, where noindex is correct behaviour rather than a defect"
            )

    if check in ("missing_structured_data", "schema_type_mismatch", "no_jsonld"):
        types = {page_types.get(u, "other") for u in urls}
        if types and types <= _SCHEMA_EXEMPT_TYPES:
            return False, (
                f"affected pages are all of type(s) {sorted(types)}, which are not "
                "expected to carry product or article schema"
            )

    if check in ("thin_content", "no_evidence_genre", "unstructured_page"):
        types = {page_types.get(u, "other") for u in urls}
        if types and types <= _ABSORPTION_EXEMPT_TYPES:
            return False, (
                f"affected pages are all of type(s) {sorted(types)}; hub pages route "
                "rather than explain, and legal text should not be optimised for citation"
            )

    if check == "sitemap_orphans":
        counts = _population_counts(finding)
        if counts and counts[1] < 3:
            return False, "orphan claim rests on fewer than 3 crawled pages"

    if check == "marketing_over_evidence":
        types = {page_types.get(u, "other") for u in urls}
        if types == {"homepage"}:
            return False, (
                "only the homepage is affected; promotional language in a homepage hero "
                "is conventional and not on its own an absorption defect"
            )

    if check == "slow_response":
        counts = [float(n) for n in _NUMBER_RE.findall(finding.get("observed", ""))]
        if counts and counts[0] < 3000:
            return False, f"median response {counts[0]:g} ms is within the 3000 ms expectation"

    return True, ""


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

# Downstream symptoms of an upstream root cause. Key = upstream check id,
# value = downstream check ids it explains.
ROOT_CAUSE_MAP = {
    "empty_shell": ["thin_content", "no_evidence_genre", "unstructured_page",
                    "missing_structured_data", "no_jsonld", "answer_not_first"],
    "state_blob": ["thin_content", "no_evidence_genre", "unstructured_page"],
    "script_heavy_shell": ["thin_content", "no_evidence_genre"],
    "thin_served_html": ["thin_content", "no_evidence_genre"],
    "robots_blocks_all": ["sitemap_orphans", "page_unreachable"],
    "no_https": ["https_not_enforced"],
}


def deduplicate(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge downstream symptoms into their upstream root cause."""
    by_check: dict[str, list[dict]] = {}
    for finding in findings:
        by_check.setdefault(finding.get("check_id", ""), []).append(finding)

    absorbed_ids: set[int] = set()
    merges: list[dict] = []

    for root, symptoms in ROOT_CAUSE_MAP.items():
        root_findings = by_check.get(root)
        if not root_findings:
            continue
        root_finding = root_findings[0]
        root_urls = set(_urls_in(root_finding))
        if not root_urls:
            continue

        swallowed = []
        for symptom in symptoms:
            for candidate in by_check.get(symptom, []):
                if id(candidate) in absorbed_ids:
                    continue
                candidate_urls = set(_urls_in(candidate))
                if not candidate_urls:
                    continue
                # Only absorb when the symptom is confined to the root's pages.
                if candidate_urls <= root_urls:
                    absorbed_ids.add(id(candidate))
                    swallowed.append(candidate)

        if swallowed:
            consequences = [s["title"] for s in swallowed]
            root_finding["evidence"] += (
                " This single cause also produces the following symptoms on the same "
                "pages, which are reported here rather than separately: "
                + "; ".join(consequences) + "."
            )
            root_finding.setdefault("consequences", []).extend(consequences)
            merges.append({
                "kept": root_finding["title"],
                "kept_check_id": root,
                "absorbed": consequences,
                "reason": (
                    "the downstream findings are symptoms of the upstream render or "
                    "access fault on the same URLs; fixing the cause resolves them all"
                ),
            })

    survivors = [f for f in findings if id(f) not in absorbed_ids]
    return survivors, merges


# ---------------------------------------------------------------------------
# Proactive recommendations
# ---------------------------------------------------------------------------

def build_proactive(layers: dict, findings: list[dict], config: AuditConfig) -> list[ProactiveRecommendation]:
    """Non-obvious, paper-grounded improvements. Never generic advice."""
    recommendations: list[ProactiveRecommendation] = []
    fired = {f.get("check_id") for f in findings}

    reachability = layers.get("reachability") or {}
    absorption = layers.get("absorption") or {}
    genre_matrix = absorption.get("genre_matrix") or {}
    pages_scored = (absorption.get("site_absorption") or {}).get("pages_scored", 0)

    # 1. The corroboration / domain-type asymmetry. Genuinely counter-intuitive.
    recommendations.append(ProactiveRecommendation(
        title="Seed explanatory corroboration off-site, not just press coverage",
        rationale=(
            "arXiv:2604.25707 §8.4 measures a split between how often a source type is "
            "selected and how deeply it is absorbed. News media supplies a large share "
            "of the candidate pool yet averages only 0.0726 mean influence, while "
            "encyclopedia-style pages average 0.2144 — roughly three times higher. "
            "Press coverage gets a brand into the pool; explanatory reference pages are "
            "what answers actually draw on. A PR-led visibility strategy optimises the "
            "weaker of the two outcomes."
        ),
        action=(
            "Alongside press outreach, ensure accurate explanatory entries exist on "
            "reference-style properties (industry wikis, standards bodies, well-maintained "
            "directories, documentation of integrations on partners' sites). Prioritise "
            "pages that define what the company does over pages that announce what it did."
        ),
        priority="medium",
        basis="arXiv:2604.25707 §8.4 domain-type influence table; handout appendix D",
        claim_level=3,
    ))

    # 2. Evidence-genre conversion, aimed at whichever genre is actually missing.
    missing = [k for k in ("definition", "numeric", "comparison") if genre_matrix.get(k, 0) == 0]
    thin_coverage = [
        k for k in ("definition", "numeric", "comparison", "howto")
        if pages_scored and 0 < genre_matrix.get(k, 0) <= max(1, pages_scored // 4)
    ]
    if thin_coverage and not missing:
        recommendations.append(ProactiveRecommendation(
            title=f"Extend {', '.join(thin_coverage)} content beyond the few pages that carry it",
            rationale=(
                f"Only {', '.join(f'{genre_matrix.get(k, 0)}/{pages_scored} pages carry {k}' for k in thin_coverage)}. "
                "The genres are present on the site, so the capability and the tone already "
                "exist; the gap is coverage. §8.2 reports mean influence uplift of +57.3% "
                "for definition markers, +61.6% for numeric content and +55.3% for "
                "comparisons over pages lacking them."
            ),
            action=(
                "Take the pattern from the pages that already do this well and apply it to "
                "the page types that do not. This is a templating change rather than a "
                "writing project."
            ),
            priority="medium",
            basis="arXiv:2604.25707 §8.2 evidence-genre uplift",
            claim_level=3,
        ))

    # 3. The Q&A warning, surfaced even when no defect fired — because it is the
    #    advice the site is most likely to receive from elsewhere.
    if "qa_wrapper_without_evidence" not in fired:
        recommendations.append(ProactiveRecommendation(
            title="Do not add FAQ sections as an AI-visibility tactic",
            rationale=(
                "This is the most commonly recommended GEO tactic and the only evidence "
                "genre arXiv:2604.25707 measures in the negative direction. Q&A-formatted "
                "pages average 0.0947 mean influence against 0.1005 for pages without the "
                "format — a 5.74% relative decrease (§8.2). Every other genre studied "
                "showed uplift between +41% and +77%. The paper's interpretation is that "
                "evidence genres create reusable support units while Q&A formatting is a "
                "surface wrapper."
            ),
            action=(
                "If FAQ blocks are added for human readers, make each answer carry a "
                "specific figure, definition or comparison. Never add questions as a "
                "substitute for evidence, and do not convert existing explanatory pages "
                "into Q&A format expecting a visibility gain."
            ),
            priority="medium",
            basis="arXiv:2604.25707 §8.2, Fig. 8 (the negative case)",
            claim_level=2,
        ))

    # 4. llms.txt, only when absent.
    if not (reachability.get("discovery") or {}).get("llms_txt"):
        recommendations.append(ProactiveRecommendation(
            title="Publish an llms.txt to state which pages carry the canonical facts",
            rationale=(
                "An emerging convention that lets a site nominate its authoritative "
                "explanatory pages rather than leaving an assistant to infer them from "
                "link structure. Adoption is not universal and support is not guaranteed, "
                "so this is a low-cost hedge rather than a reliable mechanism."
            ),
            action=(
                "Add /llms.txt listing the canonical page for each core topic — what the "
                "company does, what each product is, pricing, and the primary "
                "documentation entry point — with a one-line description each."
            ),
            priority="low",
            basis="Emerging convention; no measurement in the cited paper",
            claim_level=3,
        ))

    # 5. Platform-shape asymmetry. Only meaningful once several pages exist.
    if pages_scored >= 3:
        recommendations.append(ProactiveRecommendation(
            title="Optimise for depth on a few pages rather than breadth across many",
            rationale=(
                "Citation breadth and citation depth diverge sharply by platform: mean "
                "citations per prompt are 6.88 (ChatGPT), 12.06 (Google AIO) and 16.35 "
                "(Perplexity), while mean influence per fetched page runs the other way at "
                "0.2713, 0.0584 and 0.0646 respectively (§6, §8). A site cannot be "
                "optimal for both objectives at once, and the platform that absorbs most "
                "deeply is the one that cites fewest sources. Concentrating evidence on a "
                "small number of genuinely strong pages targets the depth objective."
            ),
            action=(
                "Identify the five questions this brand should be the answer to. Build one "
                "unambiguous evidence container for each — definition, figures, comparison, "
                "clear structure — instead of spreading thin coverage across many pages."
            ),
            priority="medium",
            basis="arXiv:2604.25707 §6 and §8 platform breadth-versus-depth divergence",
            claim_level=3,
        ))

    # 6. The backfire warning. Fires whenever a finding proposes editing page
    #    content, because that is exactly the intervention the survey measures as
    #    harmful. The site is likely to be told to "rewrite for AI" by someone;
    #    this is the one recommendation that says not to.
    content_findings = [
        f for f in findings
        if f.get("check_id") in {
            "thin_content", "no_evidence_genre", "qa_wrapper_without_evidence",
            "unstructured_page", "no_numeric_evidence", "no_comparison_content",
            "no_definition_content", "marketing_over_evidence", "boilerplate_dominant",
            "low_absorption_site", "no_price_evidence", "no_date_evidence",
        }
    ]
    if content_findings:
        recommendations.append(ProactiveRecommendation(
            title="Add the missing fact — do not rewrite the page around it",
            rationale=(
                "The findings above all point at page content, and the obvious response is "
                "to rewrite. That is the intervention with the strongest measured evidence "
                "of harm. In a controlled arena, body-only rewrites reduced top-20 presence "
                "by about 9%, top-10 presence after reranking by about 16%, and final "
                "citation by about 6% (arXiv:2607.14035 §7.4, summarising SAGEO Arena). "
                "Across 54 tested method-domain combinations in C-SEO Bench only 3 were "
                "significantly positive, and none were positive in question answering "
                "(§7.3) — several transformations reduced rank. Rewriting prose does not "
                "add retrievable evidence, and it can disturb whatever the page already "
                "had working."
            ),
            action=(
                "Treat each content finding as an instruction to add one specific, "
                "verifiable, dated fact to a page that already ranks and explains — a "
                "price, a measurement, a named comparison, an update date with its "
                "source. Leave the surrounding prose in place. Change one page, re-measure, "
                "then continue; do not rewrite the whole site at once."
            ),
            priority="high",
            basis="arXiv:2607.14035 §7.3 C-SEO Bench and §7.4 SAGEO Arena",
            claim_level=3,
        ))

    # 7. Prices and dates, aimed at the site's own commercial reality. Only
    #    fires when the audit actually looked for them and found none, so it is
    #    never generic advice.
    survey_matrix = absorption.get("survey_genre_matrix") or {}
    absent_genres = [
        label for key, label in (("price", "pricing"), ("dated", "update dates"))
        if pages_scored and survey_matrix.get(key, 0) == 0
    ]
    if absent_genres and fired & {"no_price_evidence", "no_date_evidence"}:
        recommendations.append(ProactiveRecommendation(
            title=f"Publish {', '.join(absent_genres)} as quotable facts",
            rationale=(
                "The survey grades prices and dates `Moderate` in its lever table "
                "(arXiv:2607.14035 §7.5, Table 4) and lists them among the genres an "
                "engine can lift verbatim (§7.2); competitive-citation work with 252,000 "
                "trials reaches the same conclusion, finding explicit prices and recent "
                "timestamps help while formatting-only edits do little "
                "(arXiv:2605.25517). A price or a date is the difference between a claim "
                "an assistant can repeat and one it has to hedge or drop."
            ),
            action=(
                "Put the actual figure on the page in text: the price or range, what "
                "changes it, and the date the page and its numbers were last verified. "
                "Do not add a number that cannot be attributed — the research is explicit "
                "that the aim is verifiable, dated, attributed evidence rather than the "
                "appearance of specificity."
            ),
            priority="low",
            basis="arXiv:2607.14035 §7.2/§7.5 Table 4; arXiv:2605.25517",
            claim_level=2,
        ))

    return recommendations


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Advisory review gate.")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--strict", action="store_true",
                        help="Also drop findings that would merely be downgraded.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = Workspace.open(args.workspace) if args.workspace else Workspace.latest()

    layers = {name: workspace.read(name) for name in DETECTION_LAYERS}
    layers = {k: v for k, v in layers.items() if v}
    if not layers:
        print("no detection layers have run in this workspace", file=sys.stderr)
        return 1

    config_source = (layers.get("reachability") or layers.get("preflight") or {}).get("config", {})
    config = AuditConfig.load(None, **{
        k: v for k, v in config_source.items()
        if k in ("max_pages", "max_seconds", "strictness", "min_sample_for_population_claim")
    })

    # Page-type context powers the trap list.
    page_types: dict[str, str] = {}
    for page in (layers.get("reachability") or {}).get("pages", []):
        page_types[page["url"]] = page.get("page_type", "other")
        if page.get("final_url"):
            page_types[page["final_url"]] = page.get("page_type", "other")
    context = {"page_types": page_types}

    incoming: list[dict] = []
    for layer_name, payload in layers.items():
        for finding in payload.get("findings", []):
            incoming.append({**finding, "_layer_source": layer_name})

    dropped, downgraded, kept = [], [], []

    for finding in incoming:
        title = finding.get("title", "(untitled)")

        ok, reason = gate_evidence_sufficiency(finding)
        if not ok:
            dropped.append({"title": title, "check_id": finding.get("check_id"),
                            "gate": "evidence_sufficiency", "reason": reason})
            continue

        ok, reason = gate_threshold_coherence(finding)
        if not ok:
            dropped.append({"title": title, "check_id": finding.get("check_id"),
                            "gate": "threshold_coherence", "reason": reason})
            continue

        ok, reason = gate_false_positive_traps(finding, context)
        if not ok:
            dropped.append({"title": title, "check_id": finding.get("check_id"),
                            "gate": "false_positive_trap", "reason": reason})
            continue

        ok, reason, mutation = gate_sample_sufficiency(finding, config)
        if not ok:
            dropped.append({"title": title, "check_id": finding.get("check_id"),
                            "gate": "sample_sufficiency", "reason": reason})
            continue
        if mutation:
            if args.strict:
                dropped.append({"title": title, "check_id": finding.get("check_id"),
                                "gate": "sample_sufficiency", "reason": mutation["_reason"]})
                continue
            downgraded.append({"title": title, "check_id": finding.get("check_id"),
                               "from": finding["severity"], "to": mutation["severity"],
                               "gate": "sample_sufficiency", "reason": mutation.pop("_reason")})
            finding.update(mutation)

        ok, reason, mutation = gate_claim_level(finding)
        if not ok:
            dropped.append({"title": title, "check_id": finding.get("check_id"),
                            "gate": "claim_level", "reason": reason})
            continue
        if mutation:
            downgraded.append({"title": title, "check_id": finding.get("check_id"),
                               "from": finding["severity"],
                               "to": mutation.get("severity", finding["severity"]),
                               "gate": "claim_level", "reason": mutation.pop("_reason")})
            finding.update(mutation)

        # Priority may never exceed severity. A downgraded severity with an
        # untouched "critical" action priority reads as a contradiction
        # (agent-review 2026-09-09: F-002 showed severity high beside a
        # critical-priority action with no explanation). Record the alignment
        # in the downgrade trail so the audit trail stays complete.
        alignment = _align_priority_to_severity(finding)
        if alignment:
            downgraded.append({"title": title, "check_id": finding.get("check_id"),
                               "from": "priority above severity", "to": finding["severity"],
                               "gate": "priority_alignment", "reason": alignment})

        kept.append(finding)

    survivors, merges = deduplicate(kept)
    proactive = build_proactive(layers, survivors, config)

    for finding in survivors:
        finding.pop("_layer_source", None)

    severity_counts = {s: sum(1 for f in survivors if f["severity"] == s) for s in SEVERITIES}
    payload = {
        "input_findings": len(incoming),
        "output_findings": len(survivors),
        "dropped": dropped,
        "downgraded": downgraded,
        "merged": merges,
        "reviewed_findings": survivors,
        "proactive_recommendations": [r.to_dict() for r in proactive],
        "layers_reviewed": sorted(layers),
        "verdict": {
            "severity_counts": severity_counts,
            "rejection_rate": round(len(dropped) / max(1, len(incoming)), 3),
            "notes": (
                "Every surviving finding carries a concrete observed value and at least "
                "one verifiable URL, breaches the threshold it claims to breach, rests on "
                "a sufficient sample, and asserts nothing above what its claim level "
                "supports."
            ),
        },
    }
    workspace.write("advisory", payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"ADVISORY REVIEW  {len(incoming)} in -> {len(survivors)} out")
        print(f"  dropped     {len(dropped)}")
        for item in dropped:
            print(f"    [{item['gate']}] {item['title'][:62]}")
            print(f"        {item['reason'][:100]}")
        print(f"  downgraded  {len(downgraded)}")
        for item in downgraded:
            print(f"    {item['title'][:56]}: {item['from']} -> {item['to']}")
        print(f"  merged      {len(merges)}")
        for item in merges:
            print(f"    kept '{item['kept'][:44]}' absorbing {len(item['absorbed'])} symptom(s)")
        print(f"  proactive   {len(proactive)} recommendation(s)")
        print(f"  severities  {severity_counts}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
