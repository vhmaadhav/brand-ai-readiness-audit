#!/usr/bin/env python3
"""Absorption layer: can these pages shape an answer once cited?

Reads the page inventory written by crawl-reachability. Issues no requests.
Exit codes: 0 success, 1 missing prerequisite, 2 internal error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

_here = pathlib.Path(__file__).resolve()
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        sys.path.insert(0, str(_parent / "lib"))
        break

from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.htmldoc import parse_html  # noqa: E402
from geo_audit.influence import aggregate_site_absorption, score_absorption  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.textstats import (  # noqa: E402
    EVIDENCE_ATTRIBUTION_NOTE,
    EVIDENCE_GENRE_UPLIFT,
    LEVER_SUPPORT,
    RETRIEVAL_BACKFIRE,
    SEMANTIC_ROLE_INFLUENCE,
    boilerplate_ratio,
    profile_genres,
)
from geo_audit.workspace import Workspace  # noqa: E402


def _finding(**kwargs) -> Finding:
    action = kwargs.pop("action")
    return Finding(layer="absorption", suggested_action=action, **kwargs)


def _examples(urls: list[str], limit: int = 3) -> str:
    shown = urls[:limit]
    tail = f" and {len(urls) - len(shown)} other page(s)" if len(urls) > len(shown) else ""
    return ", ".join(shown) + tail


def analyse_pages(workspace: Workspace, inventory: list[dict], scope: str, config: AuditConfig):
    """Score every fetched page. Returns (records, exempt_records)."""
    records, exempt = [], []

    for page in inventory:
        if not page.get("html_path"):
            continue
        path = workspace.root / page["html_path"]
        if not path.is_file():
            continue

        html = path.read_text(encoding="utf-8", errors="replace")
        doc = parse_html(html, url=page.get("final_url") or page["url"], scope_host=scope)
        genres = profile_genres(doc.main_text or doc.text)
        score = score_absorption(doc, genres)
        page_type = page.get("page_type", "other")
        expectations = config.expectations_for(page_type)

        record = {
            "url": page["url"],
            "page_type": page_type,
            "title": doc.title[:160],
            "structure": doc.structure_summary(),
            "genres": genres.to_dict(),
            "absorption": score.to_dict(),
            "boilerplate_ratio": boilerplate_ratio(doc.word_count, doc.total_word_count),
            "_score": score,
            "_genres": genres,
            "_doc": doc,
        }

        # Absorption is not a goal for every page type. Recording the exemption
        # explicitly is what keeps this from silently hiding a real problem.
        if expectations.get("exempt_from_absorption"):
            record["exempt_reason"] = (
                f"{page_type} pages are exempt: optimising legal text for citation "
                "is not a desirable outcome"
            )
            exempt.append(record)
        else:
            records.append(record)

    return records, exempt


def build_findings(records, exempt, config, rollup) -> list[Finding]:
    findings: list[Finding] = []
    total = len(records)
    if not total:
        return findings

    sufficient = config.sample_is_sufficient(total)

    # -- thin content ---------------------------------------------------
    thin = []
    for record in records:
        floor, basis = config.min_words_for(record["page_type"])
        if record["structure"]["word_count"] < floor:
            thin.append((record, floor, basis))

    if thin:
        worst = min(thin, key=lambda t: t[0]["structure"]["word_count"])
        floor_basis = worst[2]
        findings.append(_finding(
            title="Pages carry too little text to supply an answer",
            severity="high" if len(thin) / total > 0.4 else "medium",
            claim_level=1, confidence="high", check_id="thin_content",
            observed=f"{len(thin)} of {total} non-exempt pages below their word floor",
            expected=f"at least the derived floor ({floor_basis})",
            sample_size=total,
            evidence=(
                f"{len(thin)} of {total} sampled non-exempt pages fall below the "
                f"word-count floor. Thinnest: {worst[0]['url']} at "
                f"{worst[0]['structure']['word_count']} words against a floor of "
                f"{worst[1]:g}. Threshold provenance: {floor_basis}. "
                f"Affected: {_examples([t[0]['url'] for t in thin])}."
            ),
            affected_urls=[t[0]["url"] for t in thin],
            action=SuggestedAction(
                summary="Expand thin pages with extractable evidence rather than filler prose.",
                priority="high" if len(thin) / total > 0.4 else "medium",
                steps=[
                    "For each thin page, state what the thing is in one plain sentence (a definition).",
                    "Add the specific figures a reader would ask for: price, capacity, timing, scale.",
                    "Add one comparison against the obvious alternative.",
                    "Do not pad with restated marketing copy — length without evidence does not help.",
                ],
                effort="medium",
                mechanism=(
                    "arXiv:2604.25707 Fig. 6: mean influence rises from 0.055 for pages "
                    "under 100 words to 0.113 in the 301-600 band and 0.146 above 3,000. "
                    "The paper is explicit that length helps when it is coupled with "
                    "structure and evidence density, not on its own."
                ),
                verification="Each affected page clears its word floor with genuine evidence content.",
            ),
        ))

    # -- no evidence genre at all ---------------------------------------
    barren = [r for r in records if r["_genres"].genre_count == 0]
    if barren and sufficient:
        findings.append(_finding(
            title="Pages contain no extractable evidence of any kind",
            severity="high" if len(barren) / total > 0.5 else "medium",
            claim_level=2, confidence="high", check_id="no_evidence_genre",
            observed=f"{len(barren)} of {total} pages carry zero evidence genres",
            expected="at least one of: definition, numeric, comparison, how-to, code",
            sample_size=total,
            evidence=(
                f"{len(barren)} of {total} sampled pages carry none of the five evidence "
                "genres the paper associates with higher absorption. These pages offer "
                "an answer engine nothing quotable — no definition of what the thing is, "
                "no figures, no comparison, no procedure. "
                f"Affected: {_examples([r['url'] for r in barren])}."
            ),
            affected_urls=[r["url"] for r in barren],
            action=SuggestedAction(
                summary="Add at least one evidence genre to each page, matched to its purpose.",
                priority="high" if len(barren) / total > 0.5 else "medium",
                steps=[
                    "Product and pricing pages: add concrete numbers (price, limits, dimensions).",
                    "Explanatory pages: add a plain-language definition in the first paragraph.",
                    "Category and solution pages: add a comparison table against alternatives.",
                    "Documentation: add a numbered procedure or a code example.",
                ],
                effort="medium",
                mechanism=(
                    "arXiv:2604.25707 §8.2 measures mean influence uplift by genre: code "
                    "+76.9%, numbers +61.6%, definitions +57.3%, comparisons +55.3%, "
                    "how-to +41.2%. The proposed mechanism is that these genres create "
                    "reusable support units an answer can build on."
                ),
                verification="Every non-exempt page carries at least one evidence genre.",
            ),
        ))

    # -- the Q&A trap ---------------------------------------------------
    qa_hollow = [r for r in records if r["_genres"].qa_format and r["_genres"].genre_count == 0]
    if qa_hollow:
        findings.append(_finding(
            title="Q&A formatting is used without underlying evidence",
            severity="medium", claim_level=2, confidence="high",
            check_id="qa_wrapper_without_evidence",
            observed=f"{len(qa_hollow)} of {total} pages are Q&A-formatted with zero evidence genres",
            expected="Q&A structure carrying definitions, figures or comparisons in the answers",
            sample_size=total,
            evidence=(
                f"{len(qa_hollow)} of {total} sampled pages use question-and-answer or FAQ "
                "packaging while carrying none of the five evidence genres. This is the "
                "specific pattern arXiv:2604.25707 §8.2 measures as counter-productive: "
                "Q&A-formatted pages average 0.0947 mean influence against 0.1005 for "
                "pages without it, a 5.74% relative decrease — the only negative genre in "
                f"the study. Affected: {_examples([r['url'] for r in qa_hollow])}."
            ),
            affected_urls=[r["url"] for r in qa_hollow],
            action=SuggestedAction(
                summary=(
                    "Fill the existing answers with real evidence instead of adding more "
                    "questions. Do not add an FAQ section as a fix."
                ),
                priority="medium",
                steps=[
                    "For each question, replace the vague answer with a specific one: a figure, a definition, a named comparison.",
                    "Remove questions whose answers restate marketing copy — they dilute the page.",
                    "Keep the Q&A structure only where a genuine question is answered concretely.",
                    "Resist adding new FAQ blocks; the packaging is not what earns absorption.",
                ],
                effort="medium",
                mechanism=(
                    "The paper's interpretation is that evidence genres create reusable "
                    "support units while Q&A formatting is a surface wrapper. Rephrasing "
                    "thin content as a question adds no extractable material, which is why "
                    "it is the one genre measured in the negative direction."
                ),
                verification="Every FAQ answer contains a specific fact, figure or definition.",
            ),
        ))

    # -- structure ------------------------------------------------------
    min_headings = config.get("min_headings", 1)
    unstructured = [
        r for r in records
        if r["structure"]["heading_count"] < min_headings
        or r["structure"]["paragraph_count"] < config.get("low_paragraphs", 8)
    ]
    if unstructured and sufficient and len(unstructured) / total > 0.3:
        findings.append(_finding(
            title="Pages lack the structure needed to segment content",
            severity="medium", claim_level=1, confidence="high",
            check_id="unstructured_page",
            observed=f"{len(unstructured)} of {total} pages below the heading or paragraph floor",
            expected=f"at least {min_headings:g} heading(s) and {config.get('low_paragraphs', 8):g} paragraphs",
            sample_size=total,
            evidence=(
                f"{len(unstructured)} of {total} sampled pages fall below the structural "
                f"floor ({config.why('min_headings')}). Between the top and bottom "
                "influence quartiles the paper reports a 12.46x difference in heading "
                "count and 5.69x in paragraph count — the widest structural separations "
                f"it measures. Affected: {_examples([r['url'] for r in unstructured])}."
            ),
            affected_urls=[r["url"] for r in unstructured],
            action=SuggestedAction(
                summary="Break long passages into headed sections with self-contained paragraphs.",
                priority="medium",
                steps=[
                    "Add descriptive H2s that name the question each section answers.",
                    "Split walls of text into paragraphs that each make one point.",
                    "Use lists for enumerable facts — list density separates the quartiles 8.92x.",
                ],
                effort="low",
                mechanism=(
                    "A modular page lets an answer engine lift one section without "
                    "carrying the rest. Top-quartile pages average 10.59 headings and "
                    "47.49 paragraphs; bottom-quartile pages average 0.85 and 8.34."
                ),
                verification="Each page has a heading roughly every 150-200 words.",
            ),
        ))

    # -- genre gaps across the whole site --------------------------------
    genre_checks = (
        ("numeric", "no_numeric_evidence", "medium", 2,
         "figures, prices, measurements or statistics",
         "Add the specific numbers a reader would ask for: price, capacity, timing, scale, results.",
         "numbers / statistics", "+61.6%"),
        ("definition", "no_definition_content", "medium", 3,
         "a plain statement of what the product or company is",
         "Add one plain-language sentence defining what the thing is, near the top of the key pages.",
         "definition markers", "+57.3%"),
        ("comparison", "no_comparison_content", "medium", 3,
         "any positioning against alternatives",
         "Add a comparison against the obvious alternative, including where the alternative is the better choice.",
         "comparison content", "+55.3%"),
    )

    for key, check_id, severity, level, description, fix, genre_label, uplift in genre_checks:
        present = [r for r in records if key in r["_genres"].positive_genres]
        if present or not sufficient:
            continue
        stats = EVIDENCE_GENRE_UPLIFT[key]
        role = SEMANTIC_ROLE_INFLUENCE.get(
            {"numeric": "statistical_data", "definition": "definition", "comparison": "comparison"}[key]
        )
        findings.append(_finding(
            title=f"No {genre_label} anywhere in the sampled pages",
            severity=severity, claim_level=level, confidence="medium" if level == 3 else "high",
            check_id=check_id,
            observed=f"0 of {total} sampled pages carry {genre_label}",
            expected=f"{description} on the pages that describe the offering",
            sample_size=total,
            evidence=(
                f"None of the {total} sampled pages contain {description}. "
                f"arXiv:2604.25707 §8.2 measures pages carrying {genre_label} at "
                f"{stats['present']:.4f} mean influence against {stats['absent']:.4f} "
                f"without ({uplift}); §8.3 places this semantic role at {role:.4f} mean "
                f"influence. Sampled: {_examples([r['url'] for r in records])}."
            ),
            affected_urls=[r["url"] for r in records[:8]],
            action=SuggestedAction(
                summary=fix, priority="medium",
                steps=[
                    fix,
                    "Put it in body text, not only in an image or a chart.",
                    "State it plainly enough to be quoted in one sentence.",
                ],
                effort="medium",
                mechanism=(
                    f"Pages carrying {genre_label} average {stats['present']:.4f} influence "
                    f"versus {stats['absent']:.4f} without. This is an association measured "
                    "across a 23,745-row citation feature table, not a demonstrated cause; "
                    "the paper reserves causal claims for intervention experiments."
                ),
                verification=f"The key pages contain {description}.",
            ),
        ))

    # -- survey-graded genres: prices and dates ---------------------------
    #
    # Kept separate from the loop above because the grounding is different in
    # kind. The paper measures a mean-influence uplift for its five genres, so
    # those findings can quote a number. The survey grades prices and dates
    # "moderate" in its lever table (Table 4) without a comparable effect size,
    # so these findings cite the grade and never borrow the paper's figures.
    # Claim level is capped accordingly, and both are advisory rather than
    # urgent: a page with no price is not broken, it is just harder to quote.
    survey_checks = (
        ("price", "no_price_evidence", "medium",
         "a specific price, rate or cost",
         "Publish the actual figures: the price, the tiers, and what changes them.",
         "prices or rates",
         "a price is the most quotable, least ambiguous fact a commercial page "
         "carries, and a reader asking what something costs has no other way to "
         "get an answer from the page itself"),
        ("dated", "no_date_evidence", "low",
         "a published or updated date",
         "Show when each key page was last updated, and date the figures on it.",
         "visible dates",
         "an undated figure cannot be corroborated, so an engine has to either "
         "omit it or hedge it; dating the page is what makes its numbers reusable"),
    )
    for key, check_id, severity, description, fix, genre_label, why in survey_checks:
        present = [r for r in records if key in r["_genres"].survey_genres]
        if present or not sufficient:
            continue
        graded = LEVER_SUPPORT["recency_prices_dates"]
        findings.append(_finding(
            title=f"No {genre_label} anywhere in the sampled pages",
            severity=severity, claim_level=2, confidence="medium",
            check_id=check_id,
            observed=f"0 of {total} sampled pages carry {genre_label}",
            expected=f"{description} on the pages that describe the offering",
            sample_size=total,
            evidence=(
                f"None of the {total} sampled pages contain {description}. "
                f"arXiv:2607.14035 §7.2 lists prices, dates and references among the "
                f"genres an engine can lift verbatim, and grades this lever "
                f"'{graded['grade']}' in Table 4 ({graded['note']}); §7.5 separately "
                f"finds that formatting alone generalises poorly. Reason it matters: "
                f"{why}. Sampled: {_examples([r['url'] for r in records])}."
            ),
            affected_urls=[r["url"] for r in records[:8]],
            action=SuggestedAction(
                summary=fix, priority=severity,
                steps=[
                    fix,
                    "Date the figures themselves, not just the page footer.",
                    "Attribute each figure to a source a reader can open.",
                ],
                effort="low",
                mechanism=(
                    "Pages that cannot be checked cannot be reused. An assistant "
                    "citing an undated or unsourced number takes on the risk of that "
                    "number being wrong, and typically avoids the claim instead. "
                    + EVIDENCE_ATTRIBUTION_NOTE
                ),
                verification=f"The key pages contain {description}, with its source named.",
            ),
        ))

    # -- marketing over evidence -----------------------------------------
    hedgy = [
        r for r in records
        if r["_genres"].marketing_hedges >= 5 and r["_genres"].genre_count <= 1
    ]
    if hedgy and sufficient and len(hedgy) / total > 0.3:
        worst = max(hedgy, key=lambda r: r["_genres"].marketing_hedges)
        findings.append(_finding(
            title="Promotional language substantially outweighs extractable evidence",
            severity="medium", claim_level=3, confidence="medium",
            check_id="marketing_over_evidence",
            observed=f"{len(hedgy)} of {total} pages: 5+ promotional hedges, at most 1 evidence genre",
            expected="claims backed by figures, definitions or comparisons",
            sample_size=total,
            evidence=(
                f"{len(hedgy)} of {total} sampled pages use five or more promotional "
                "hedges ('industry-leading', 'seamless', 'world-class') while carrying at "
                f"most one evidence genre. Worst case: {worst['url']} with "
                f"{worst['_genres'].marketing_hedges} hedges and "
                f"{worst['_genres'].genre_count} evidence genre(s). Claims are asserted "
                "rather than substantiated, leaving nothing an answer can quote as fact."
            ),
            affected_urls=[r["url"] for r in hedgy],
            action=SuggestedAction(
                summary="Replace each superlative with the specific fact that justifies it.",
                priority="medium",
                steps=[
                    "'industry-leading' becomes the actual figure and its source.",
                    "'trusted by thousands' becomes the actual count and date.",
                    "'seamless integration' becomes the named systems and the setup time.",
                ],
                effort="medium",
                mechanism=(
                    "§8.3 places 'fact_source' usage at 0.1241 mean influence against "
                    "0.0775 for 'background_only'. Unfalsifiable superlatives cannot "
                    "function as a fact source because there is nothing to lift. "
                    + RETRIEVAL_BACKFIRE["caution"]
                ),
                verification="Every superlative on a key page is followed by a specific figure.",
            ),
        ))

    # -- boilerplate ------------------------------------------------------
    heavy = [r for r in records if r["boilerplate_ratio"] > 0.6 and r["structure"]["word_count"] > 60]
    if heavy and sufficient and len(heavy) / total > 0.4:
        findings.append(_finding(
            title="Page chrome outweighs the actual content",
            severity="medium", claim_level=2, confidence="medium",
            check_id="boilerplate_dominant",
            observed=f"{len(heavy)} of {total} pages are over 60% navigation, footer and boilerplate",
            expected="main content dominating the page's text",
            sample_size=total,
            evidence=(
                f"On {len(heavy)} of {total} sampled pages more than 60% of the text sits "
                "outside the main content region — navigation, footers, cookie notices and "
                "repeated promotional blocks. "
                f"Highest: {max(heavy, key=lambda r: r['boilerplate_ratio'])['url']} at "
                f"{max(r['boilerplate_ratio'] for r in heavy):.0%}."
            ),
            affected_urls=[r["url"] for r in heavy],
            action=SuggestedAction(
                summary="Increase the share of each page given to its unique content.",
                priority="medium",
                steps=[
                    "Wrap the unique content in a <main> landmark.",
                    "Trim repeated promotional blocks that appear on every page.",
                    "Move long link lists into a collapsed or footer region.",
                ],
                effort="medium",
                mechanism=(
                    "Handout appendix F: when substance is surrounded by low-value filler, "
                    "a summariser has little to work with and the important part disappears. "
                    "The same dilution applies to any extractive reader."
                ),
                verification="Main-content text exceeds boilerplate text on key pages.",
            ),
        ))

    # -- site-level rollup ------------------------------------------------
    if rollup.get("pages_scored", 0) >= 3 and rollup.get("median", 1.0) < config.get("absorption_floor", 0.25):
        findings.append(_finding(
            title="Site-wide absorption readiness sits in the weakest band",
            severity="medium", claim_level=3, confidence="medium",
            check_id="low_absorption_site",
            observed=f"median readiness {rollup['median']:.3f} across {rollup['pages_scored']} pages",
            expected=f"median above {config.get('absorption_floor', 0.25):.2f}",
            sample_size=rollup["pages_scored"],
            evidence=(
                f"Median absorption readiness across {rollup['pages_scored']} scored pages "
                f"is {rollup['median']:.3f} (mean {rollup['mean']:.3f}, range "
                f"{rollup['min']:.3f}-{rollup['max']:.3f}); "
                f"{rollup['weak_or_worse']} pages score below 0.45. The score is a "
                "page-side proxy built from the independent feature correlations in "
                "arXiv:2604.25707 Fig. 7, not the paper's influence_score, which requires "
                "a generated answer to compute."
            ),
            affected_urls=[r["url"] for r in sorted(records, key=lambda r: r["_score"].score)[:6]],
            action=SuggestedAction(
                summary="Treat this as a content-shape problem across the site, not a per-page fix.",
                priority="medium",
                steps=[
                    "Pick the five pages that should be cited when someone asks about this brand.",
                    "Rebuild each as an evidence container: definition, figures, comparison, structure.",
                    "Re-run this audit and confirm those five move into the adequate band or above.",
                ],
                effort="high",
                mechanism=(
                    "A mechanism-level interpretation, capped at medium severity for that "
                    "reason. The paper supports the association between these page "
                    "properties and absorption; it explicitly does not establish that "
                    "changing them causes citation outcomes to change. "
                    + RETRIEVAL_BACKFIRE["caution"]
                ),
                verification="Median readiness across key pages rises above 0.45.",
            ),
        ))

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Absorption-layer evidence density audit.")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = Workspace.open(args.workspace) if args.workspace else Workspace.latest()
    reachability = workspace.read("reachability")
    if not reachability:
        print("reachability layer has not run; run the crawl-reachability skill first", file=sys.stderr)
        return 1

    preflight = workspace.read("preflight") or {}
    scope = (preflight.get("target") or {}).get("registrable_domain", "")

    config = AuditConfig.load(None, **{
        k: v for k, v in (reachability.get("config") or {}).items()
        if k in ("max_pages", "max_seconds", "strictness", "min_sample_for_population_claim")
    })
    config.calibrate_to_site([
        p.get("structure", {}) for p in reachability.get("pages", []) if p.get("structure")
    ])

    records, exempt = analyse_pages(workspace, reachability.get("pages", []), scope, config)
    rollup = aggregate_site_absorption([r["_score"] for r in records])
    findings = build_findings(records, exempt, config, rollup)

    genre_matrix = {
        key: sum(1 for r in records if key in r["_genres"].positive_genres)
        for key in ("definition", "numeric", "comparison", "howto", "code")
    }
    genre_matrix["qa_format"] = sum(1 for r in records if r["_genres"].qa_format)

    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    clean_exempt = [{k: v for k, v in r.items() if not k.startswith("_")} for r in exempt]

    payload = {
        "pages_analysed": len(records),
        "pages_exempt": len(exempt),
        "site_absorption": rollup,
        "genre_matrix": genre_matrix,
        "paper_genre_reference": EVIDENCE_GENRE_UPLIFT,
        "survey_genre_matrix": {
            key: sum(1 for r in records if key in r["_genres"].survey_genres)
            for key in ("price", "dated")
        },
        "lever_support": LEVER_SUPPORT,
        "retrieval_backfire": RETRIEVAL_BACKFIRE,
        "config": config.to_dict(),
        "pages": clean,
        "exempt_pages": clean_exempt,
        "findings": [f.to_dict() for f in findings],
        "notes": [
            "Absorption readiness is a page-side proxy derived from the independent "
            "feature correlations in arXiv:2604.25707 Fig. 7. It is not the paper's "
            "influence_score, which is defined against a generated answer.",
            "A page-side score is a quality filter, not a citation predictor: "
            "arXiv:2609.07559 reports within-query rank correlation of about 0.11 "
            "between such aggregate scores and citation outcomes, so these findings "
            "identify pages that are hard to quote, not pages guaranteed to be cited.",
        ],
    }
    workspace.write("absorption", payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"EVIDENCE DENSITY  {len(records)} pages scored, {len(exempt)} exempt")
        if rollup.get("pages_scored"):
            print(f"  readiness  median {rollup['median']:.3f}  mean {rollup['mean']:.3f}  "
                  f"range {rollup['min']:.3f}-{rollup['max']:.3f}")
            print(f"  bands      {rollup['distribution']}")
        print(f"  genres     {genre_matrix}")
        print(f"  findings   {len(findings)}")
        for finding in findings:
            print(f"    [{finding.severity:8s}] {finding.title}")
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
