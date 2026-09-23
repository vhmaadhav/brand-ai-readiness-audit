#!/usr/bin/env python3
"""Reachability layer: discovery, fetching, sitemap audit, render faults.

Writes the page inventory that every downstream skill consumes.
Exit codes: 0 success, 1 preflight not cleared, 2 internal error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_here = pathlib.Path(__file__).resolve()
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        sys.path.insert(0, str(_parent / "lib"))
        break

from geo_audit import robots as robots_mod  # noqa: E402
from geo_audit import sitemapaudit, sitemapper  # noqa: E402
from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.fetcher import Budget, Fetcher  # noqa: E402
from geo_audit.htmldoc import parse_html  # noqa: E402
from geo_audit.renderfaults import diagnose, summarise_site  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.workspace import Workspace  # noqa: E402

SOFT_404_RE = re.compile(
    r"\b(page not found|404 error|does not exist|no longer available|"
    r"couldn'?t find|nothing here|sorry,? we can'?t find)\b", re.I)

RENDER_FAULT_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# How much of the detection budget sitemap discovery may spend before it stops
# expanding and works with what it has. Expressed as a share so a run given a
# small budget degrades proportionally, and capped in absolute terms so a large
# budget does not license a long walk over a huge sitemap. A page needs a
# fraction of a second to fetch, so the pool only has to reach max_pages, not
# enumerate the site.
DISCOVERY_BUDGET_SHARE = 0.35
DISCOVERY_SECONDS_CEILING = 45.0


def affordable_sample_size(
    max_pages: int, per_host_delay: float, max_seconds: float, elapsed: float
) -> int:
    """How many pages can be fetched at this politeness rate in what is left.

    Politeness and a time budget pull against each other: a site asking for a 30
    second crawl delay cannot be read at the rate ``max_pages`` assumes. Waiting
    out the delay for every page is not more respectful, it just means the stage
    is killed partway and the report contains nothing -- the site is hit by a
    crawler that then discards all of it. Asking for fewer pages and finishing
    respects the site *and* produces a report.

    The floor of one page is deliberate: a site whose delay exceeds the entire
    budget still gets one page fetched, deliberately, at the requested rate.
    """
    if per_host_delay <= 0:
        return max_pages
    remaining = max(0.0, max_seconds - elapsed)
    return max(1, min(max_pages, int(remaining / per_host_delay)))

# Human titles per sitemap problem code. Derived titles read badly because the
# details contain "sitemap.xml", and splitting on a full stop truncates there.
SITEMAP_TITLES = {
    "sitemap_missing": "No sitemap.xml is reachable",
    "sitemap_not_declared": "Sitemap exists but is not declared in robots.txt",
    "sitemap_malformed": "Sitemap is malformed",
    "sitemap_empty": "Sitemap contains no URL entries",
    "sitemap_duplicates": "Sitemap contains duplicate URL entries",
    "sitemap_off_scope": "Sitemap lists URLs outside the serving host",
    "sitemap_scheme_mismatch": "Sitemap entries use a different scheme than the live site",
    "sitemap_fragments": "Sitemap entries contain URL fragments",
    "sitemap_bad_lastmod": "Sitemap lastmod values are not valid W3C datetimes",
    "sitemap_future_lastmod": "Sitemap lastmod values are dated in the future",
    "sitemap_stale": "Sitemap lastmod values indicate a site unchanged for over a year",
    "sitemap_uniform_lastmod": "Every sitemap entry shares one lastmod value",
    "sitemap_no_lastmod": "Sitemap entries carry no lastmod",
    "sitemap_orphans": "Crawlable pages are absent from the sitemap",
    "sitemap_broken_entries": "Sitemap lists URLs that return errors",
    "sitemap_redirect_heavy": "Most sampled sitemap entries redirect",
}


def _finding(**kwargs) -> Finding:
    action = kwargs.pop("action")
    return Finding(layer="reachability", suggested_action=action, **kwargs)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_pages(fetcher, candidates, policy, pages_dir, workers=4):
    """Fetch the sampled pages concurrently, honouring robots.txt per URL."""
    pages_dir.mkdir(parents=True, exist_ok=True)

    def one(index_candidate):
        index, candidate = index_candidate
        path = "/" + candidate.url.split("/", 3)[-1] if candidate.url.count("/") > 2 else "/"
        # Read the policy as the agent we actually present ourselves as, not as
        # Googlebot: a site that disallows * and allows only Googlebot has not
        # given this crawler permission, and borrowing Googlebot's would make
        # the report's "robots respected" claim false.
        if policy.exists and not policy.can_fetch(robots_mod.OWN_USER_AGENT_TOKEN, path):
            return {
                "url": candidate.url, "page_type": candidate.page_type,
                "source": candidate.source, "skipped": "disallowed by robots.txt",
                "status": None, "ok": False,
            }
        result = fetcher.fetch(candidate.url)
        record = {
            "url": candidate.url,
            "final_url": result.final_url,
            "page_type": candidate.page_type,
            "source": candidate.source,
            "status": result.status,
            "ok": result.ok,
            "elapsed_ms": result.elapsed_ms,
            "content_type": result.content_type,
            "raw_bytes": result.raw_bytes,
            "redirect_chain": result.redirect_chain,
            "error": result.error,
            "headers": {
                k: v for k, v in result.headers.items()
                if k in ("x-robots-tag", "cache-control", "last-modified", "etag", "content-type")
            },
        }
        if result.ok and result.is_html and result.body:
            html_path = pages_dir / f"{index:03d}.html"
            html_path.write_text(result.body, encoding="utf-8", errors="replace")
            record["html_path"] = f"pages/{html_path.name}"
        return record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, enumerate(candidates)))


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def sitemap_findings(audit: sitemapaudit.SitemapAudit, origin: str) -> list[Finding]:
    """Turn sitemap problems into findings with concrete fixes."""
    fixes = {
        "sitemap_missing": (
            "Publish a sitemap.xml listing every canonical public URL and declare it in robots.txt.",
            ["Generate a sitemap covering all indexable pages.",
             "Serve it at /sitemap.xml.",
             "Add `Sitemap: https://<domain>/sitemap.xml` to robots.txt.",
             "Include an accurate lastmod per URL."],
            "A sitemap is the cheapest complete statement of what exists and what changed. "
            "Without one a crawler must infer the site's shape from links alone.",
        ),
        "sitemap_not_declared": (
            "Add a Sitemap: directive to robots.txt.",
            ["Append `Sitemap: <absolute sitemap URL>` to robots.txt."],
            "robots.txt is the first file a crawler requests; declaring the sitemap there "
            "removes a guess from discovery.",
        ),
        "sitemap_orphans": (
            "Add the missing pages to the sitemap, or confirm their exclusion is deliberate.",
            ["Regenerate the sitemap from the live route table rather than by hand.",
             "Confirm each omission is intentional.",
             "Automate regeneration on deploy so it cannot drift again."],
            "Pages absent from the sitemap rely entirely on internal linking to be found. "
            "Deep pages with few inbound links are the ones most likely to be missed.",
        ),
        "sitemap_broken_entries": (
            "Remove or fix sitemap entries that return errors.",
            ["Fix the underlying 404s, or drop the entries.",
             "Regenerate the sitemap from live routes.",
             "Add a CI check that every sitemap URL returns 200."],
            "A sitemap listing dead URLs wastes crawl budget and reduces the weight given "
            "to the whole file.",
        ),
        "sitemap_stale": (
            "Emit accurate lastmod values from real content-change timestamps.",
            ["Derive lastmod from the content store's updated_at, not build time.",
             "Confirm changed pages show a recent lastmod."],
            "An accurate lastmod tells a crawler where to spend its budget; a stale one "
            "says nothing has changed.",
        ),
        "sitemap_future_lastmod": (
            "Correct lastmod values dated in the future.",
            ["Find the source of the future timestamps (usually a timezone or build bug).",
             "Clamp lastmod to now."],
            "Future-dated timestamps are treated as untrustworthy and commonly cause the "
            "whole field to be ignored.",
        ),
    }
    default_fix = (
        "Correct the sitemap so it validates and lists only canonical, live, in-scope URLs.",
        ["Regenerate the sitemap from the live route table.", "Validate it against the sitemaps.org 0.9 schema."],
        "A sitemap a crawler distrusts is a sitemap a crawler ignores.",
    )

    findings: list[Finding] = []
    for problem in audit.problems:
        summary, steps, mechanism = fixes.get(problem["code"], default_fix)
        examples = problem.get("examples") or []
        evidence = problem["detail"]
        if examples:
            evidence += " Examples: " + "; ".join(examples[:3]) + "."
        else:
            evidence += f" Checked at {origin}/sitemap.xml and the robots.txt declarations."

        findings.append(_finding(
            title=SITEMAP_TITLES.get(problem["code"], problem["code"].replace("_", " ").capitalize()),
            severity=problem["severity"],
            claim_level=1,
            confidence="high",
            check_id=problem["code"],
            evidence=evidence,
            observed=problem["detail"],
            affected_urls=[e.split(" ")[0] for e in examples[:6]] or [origin + "/sitemap.xml"],
            action=SuggestedAction(
                summary=summary, priority=problem["severity"] if problem["severity"] in
                ("critical", "high", "medium", "low") else "medium",
                steps=steps, effort="low", mechanism=mechanism,
                verification="Re-run this skill; the sitemap problem must be absent.",
            ),
        ))
    return findings


def render_findings(reports, summary, config) -> list[Finding]:
    """One finding per distinct render fault, aggregated across pages."""
    if not reports:
        return []

    by_code: dict[str, list] = {}
    for report in reports:
        for fault in report.faults:
            by_code.setdefault(fault.code, []).append((report, fault))

    findings: list[Finding] = []
    total = len(reports)

    for code, occurrences in sorted(
        by_code.items(), key=lambda kv: RENDER_FAULT_SEVERITY_ORDER.get(kv[1][0][1].severity, 9)
    ):
        exemplar = occurrences[0][1]
        urls = [r.url for r, _ in occurrences]
        count = len(occurrences)

        # A single affected page is a page-level observation (level 1). A claim
        # about the site as a whole needs a sufficient sample (level 2).
        population_claim = count > 1 and config.sample_is_sufficient(total)
        claim_level = 2 if population_claim else 1
        severity = exemplar.severity
        if count == 1 and severity == "critical" and total > 3:
            severity = "high"  # one page out of many is not a site-wide critical

        recoverable_note = ""
        if exemplar.code == "state_blob":
            trapped = max(
                (f.evidence.get("text_trapped_in_json_chars", 0) for _, f in occurrences),
                default=0,
            )
            recoverable_note = (
                f" The content is recoverable: roughly {trapped:,} characters of "
                "readable text already ship inside the page's client state payload, "
                "so this is a markup problem rather than a missing-content problem."
            )

        findings.append(_finding(
            title=f"JavaScript render fault: {code.replace('_', ' ')}",
            severity=severity,
            claim_level=claim_level,
            confidence="high" if count > 1 else "medium",
            check_id=code,
            observed=f"{count} of {total} sampled pages",
            expected="primary content present in the server response",
            sample_size=total,
            evidence=(
                f"{count} of {total} sampled pages exhibit '{code}'. {exemplar.detail} "
                f"Observed on {urls[0]}"
                + (f" and {count - 1} other page(s)" if count > 1 else "")
                + "." + recoverable_note
            ),
            affected_urls=urls,
            action=SuggestedAction(
                summary=exemplar.fix,
                priority="critical" if severity == "critical" else
                         "high" if severity == "high" else "medium",
                steps=[
                    "Confirm the fault with `curl -s <url> | wc -w` — that word count is "
                    "what a non-executing reader receives.",
                    exemplar.fix,
                    "Re-check that the primary claim of the page appears in the raw HTML.",
                ],
                effort="high" if code in ("empty_shell", "client_routing") else "medium",
                mechanism=(
                    "Handout appendix C: a fact that is plainly visible on screen can be "
                    "invisible to a program that is not looking at it the same way. "
                    "arXiv:2604.25707 §4.1 measures a 76.44% fetch success rate across "
                    "already-cited pages, so retrieval-side failure is common enough to "
                    "treat as a first-order risk."
                ),
                verification="curl -s <url> | grep -c '<p' returns a non-zero count of real paragraphs.",
            ),
        ))
    return findings


def page_findings(pages, docs, config, target) -> list[Finding]:
    """Status, indexability and canonical checks over the fetched sample."""
    findings: list[Finding] = []
    fetched = [p for p in pages if p.get("status") is not None]
    if not fetched:
        return findings

    failed = [p for p in fetched if not p.get("ok")]
    if failed and config.sample_is_sufficient(len(fetched)):
        share = len(failed) / len(fetched)
        findings.append(_finding(
            title="Sampled pages are not retrievable",
            severity="critical" if share > 0.4 else "high",
            claim_level=1, confidence="high", check_id="page_unreachable",
            observed=f"{len(failed)} of {len(fetched)} sampled pages failed",
            expected="200 OK for every linked public page",
            sample_size=len(fetched),
            evidence=(
                f"{len(failed)} of {len(fetched)} sampled pages ({share:.0%}) did not "
                "return a successful response: "
                + "; ".join(f"{p['url']} -> {p.get('status') or p.get('error')}" for p in failed[:4])
                + "."
            ),
            affected_urls=[p["url"] for p in failed],
            action=SuggestedAction(
                summary="Fix or delist the URLs that do not resolve.",
                priority="critical" if share > 0.4 else "high",
                steps=["Fix the failing routes, or remove them from links and the sitemap.",
                       "Add monitoring for the status of linked public URLs."],
                effort="medium",
                mechanism=(
                    "arXiv:2604.25707 §4.1: only 76.44% of pages engines chose to cite "
                    "could be fetched. Every unreachable URL is a citation the brand "
                    "was selected for and then lost."
                ),
                verification="Every sampled URL returns 200.",
            ),
        ))

    # Indexability blocks.
    blocked: list[tuple[str, str]] = []
    for page in fetched:
        header = (page.get("headers") or {}).get("x-robots-tag", "")
        if "noindex" in header.lower() or "none" in header.lower():
            blocked.append((page["url"], f"X-Robots-Tag: {header}"))
    for url, doc in docs.items():
        directive = doc.meta_get("robots", "googlebot")
        if directive and ("noindex" in directive.lower() or "none" in directive.lower()):
            blocked.append((url, f'<meta name="robots" content="{directive}">'))

    if blocked:
        findings.append(_finding(
            title="Public pages are marked noindex",
            severity="high", claim_level=1, confidence="high",
            check_id="noindex_on_public_page",
            observed=f"{len(blocked)} of {len(fetched)} sampled pages carry a noindex directive",
            expected="no indexing block on pages intended to be found",
            sample_size=len(fetched),
            evidence=(
                f"{len(blocked)} sampled page(s) carry an explicit indexing block: "
                + "; ".join(f"{url} ({how})" for url, how in blocked[:4])
                + ". These pages are linked and publicly reachable but instruct "
                "crawlers not to index them."
            ),
            affected_urls=[url for url, _ in blocked],
            action=SuggestedAction(
                summary="Remove the noindex directive from pages that should be discoverable.",
                priority="high",
                steps=["Confirm each block is unintentional — staging leaks are the usual cause.",
                       "Remove the meta robots tag or X-Robots-Tag header.",
                       "Request re-crawling once removed."],
                effort="low",
                mechanism="A noindex page cannot enter the candidate pool at all.",
                verification="curl -I <url> shows no X-Robots-Tag, and no meta robots noindex.",
            ),
        ))

    # Soft 404s: 200 status with not-found content.
    soft = [
        url for url, doc in docs.items()
        if SOFT_404_RE.search(doc.title or "") or
        (doc.word_count < 120 and SOFT_404_RE.search(doc.text[:1200]))
    ]
    if soft:
        findings.append(_finding(
            title="Pages return 200 while displaying not-found content",
            severity="medium", claim_level=2, confidence="medium",
            check_id="soft_404",
            observed=f"{len(soft)} of {len(docs)} parsed pages",
            expected="404 status for missing content",
            sample_size=len(docs),
            evidence=(
                f"{len(soft)} page(s) returned HTTP 200 but their content reads as a "
                "not-found message: " + "; ".join(soft[:3]) + ". Crawlers treat these as "
                "real pages and index the error text."
            ),
            affected_urls=soft,
            action=SuggestedAction(
                summary="Return a real 404 status for missing content.",
                priority="medium",
                steps=["Return 404 (or 410) rather than 200 for missing resources.",
                       "Keep the friendly error page, but with the correct status code."],
                effort="low",
                mechanism="A 200 tells a crawler the content is real and worth indexing.",
                verification="curl -o /dev/null -w '%{http_code}' <missing-url> returns 404.",
            ),
        ))

    # Canonical correctness.
    mismatched = [
        (url, doc.canonical) for url, doc in docs.items()
        if doc.canonical and doc.canonical.rstrip("/") != url.rstrip("/")
        and doc.canonical.rstrip("/") not in {p.get("final_url", "").rstrip("/") for p in fetched}
    ]
    if len(mismatched) > 1 and config.sample_is_sufficient(len(docs)):
        findings.append(_finding(
            title="rel=canonical points away from the page's own URL",
            severity="medium", claim_level=1, confidence="medium",
            check_id="canonical_mismatch",
            observed=f"{len(mismatched)} of {len(docs)} pages",
            expected="self-referential canonical, or a deliberate consolidation target",
            sample_size=len(docs),
            evidence=(
                f"{len(mismatched)} sampled page(s) declare a canonical URL that is not "
                "their own and was not among the pages crawled: "
                + "; ".join(f"{url} -> {canonical}" for url, canonical in mismatched[:3])
                + ". If unintentional, the content's signals are being handed to another URL."
            ),
            affected_urls=[url for url, _ in mismatched],
            action=SuggestedAction(
                summary="Make rel=canonical self-referential unless consolidation is intended.",
                priority="medium",
                steps=["Audit the canonical template for the affected page types.",
                       "Set a self-referential canonical where no consolidation is intended."],
                effort="low",
                mechanism=(
                    "A canonical pointing elsewhere asks crawlers to credit a different "
                    "URL, so the page itself accumulates nothing."
                ),
                verification="Each page's rel=canonical matches its own final URL.",
            ),
        ))

    timings = sorted(p.get("elapsed_ms", 0) for p in fetched if p.get("ok"))
    if timings and timings[len(timings) // 2] > 3000:
        median = timings[len(timings) // 2]
        findings.append(_finding(
            title="Server responses are slow enough to risk crawl abandonment",
            severity="medium", claim_level=2, confidence="medium",
            check_id="slow_response",
            observed=f"median response {median} ms across {len(timings)} pages",
            expected="under 3000 ms",
            sample_size=len(timings),
            evidence=(
                f"Median server response across {len(timings)} successfully fetched pages "
                f"was {median} ms (slowest {timings[-1]} ms). Retrieval during answer "
                "generation is latency-bound; slow responses are dropped rather than waited on."
            ),
            affected_urls=[p["url"] for p in fetched if p.get("elapsed_ms", 0) > 3000][:6],
            action=SuggestedAction(
                summary="Reduce server response time for public pages.",
                priority="medium",
                steps=["Profile the slowest sampled routes.",
                       "Add edge caching for anonymous requests.",
                       "Ensure HTML is not blocked on slow upstream calls."],
                effort="medium",
                mechanism=(
                    "A live-retrieval crawler works inside a user-facing latency budget. "
                    "A page that answers too late is functionally unreachable."
                ),
                verification="Median time-to-first-byte under 1s for public pages.",
            ),
        ))
    return findings


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl and reachability audit layer.")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace = Workspace.open(args.workspace) if args.workspace else Workspace.latest()
    preflight = workspace.read("preflight")
    if not preflight or not preflight.get("cleared"):
        print("preflight has not cleared; run the url-validation skill first", file=sys.stderr)
        return 1

    target = preflight["target"]
    origin, scope = target["origin"], target["registrable_domain"]

    config = AuditConfig.load(None, **{
        k: v for k, v in preflight.get("config", {}).items()
        if k in ("max_pages", "max_seconds", "strictness", "min_sample_for_population_claim")
    })
    if args.max_pages:
        config.max_pages = args.max_pages

    robots_info = preflight.get("robots", {})
    policy = robots_mod.RobotsPolicy(exists=False)
    if robots_info.get("exists"):
        probe = Fetcher(scope, Budget(max_seconds=20)).fetch(
            robots_info.get("url") or robots_mod.robots_url_for(origin),
            count_against_budget=False,
        )
        if probe.ok:
            policy = robots_mod.parse_robots(probe.body, url=probe.final_url, status=probe.status)

    budget = Budget(
        max_pages=config.max_pages,
        max_seconds=config.max_seconds,
        per_host_delay=max(
            config.per_host_delay,
            policy.crawl_delay_for(robots_mod.OWN_USER_AGENT_TOKEN) or 0,
        ),
    )
    fetcher = Fetcher(scope, budget, timeout=config.timeout)

    # -- discover ------------------------------------------------------
    # Discovery is the one phase whose cost follows the site's size rather than
    # max_pages. Bound it to a share of the detection budget so a sitemap index
    # with dozens of wide children cannot starve the page fetches that the
    # report is actually made of. On a normal site discovery finishes in a few
    # seconds and the ceiling is never reached.
    discovery_deadline = budget.started_at + min(
        DISCOVERY_SECONDS_CEILING, config.max_seconds * DISCOVERY_BUDGET_SHARE
    )
    discovery = sitemapper.discover(
        fetcher, origin, scope, policy, deadline=discovery_deadline
    )

    # Politeness has to fit inside the budget, or it produces the opposite of
    # the intended result: a site asking for a 30-second crawl delay (Hacker
    # News does) spends the whole detection budget waiting between requests and
    # is killed having read nothing at all. The delay is still honoured exactly
    # -- the sample is made smaller instead, so the report describes fewer pages
    # rather than no pages, and says which of the two happened.
    sample_size = affordable_sample_size(
        config.max_pages,
        budget.per_host_delay,
        config.max_seconds,
        time.monotonic() - budget.started_at,
    )
    sample = sitemapper.select_sample(discovery.candidates, sample_size)
    if sample_size < config.max_pages:
        discovery.notes.append(
            f"The site requests a {budget.per_host_delay:.0f}s crawl delay, which the "
            f"audit honours; the sample was reduced to {sample_size} page(s) so the "
            f"crawl finishes inside the time budget."
        )

    # -- fetch ---------------------------------------------------------
    pages = fetch_pages(fetcher, sample, policy, workspace.root / "pages")

    docs = {}
    render_reports = []
    for page in pages:
        if not page.get("html_path"):
            continue
        html = (workspace.root / page["html_path"]).read_text(encoding="utf-8", errors="replace")
        doc = parse_html(html, url=page.get("final_url") or page["url"], scope_host=scope)
        docs[page["url"]] = doc
        render_reports.append(diagnose(html, doc, url=page["url"]))
        page["structure"] = doc.structure_summary()
        page["title"] = doc.title[:200]

    # -- sitemap audit -------------------------------------------------
    audit = sitemapaudit.SitemapAudit()
    audit.declared_in_robots = bool(policy.sitemaps)
    audit.robots_sitemap_urls = list(policy.sitemaps)
    audit.discovered_paths = "; ".join(discovery.sitemap_urls) or "none"

    for sitemap_url in discovery.sitemap_urls[:6]:
        response = fetcher.fetch(sitemap_url, count_against_budget=False)
        audit.files.append(sitemapaudit.parse_sitemap_file(
            response.body, sitemap_url, status=response.status, ok=response.ok
        ))
    if not discovery.sitemap_urls:
        audit.files.append(sitemapaudit.SitemapFile(
            url=f"{origin}/sitemap.xml", ok=False, status=None,
        ))

    crawled_urls = [p["url"] for p in pages if p.get("ok")]
    sitemapaudit.analyse(
        audit, scope_domain=scope, origin_scheme=target["scheme"],
        crawled_urls=crawled_urls, now=datetime.now(timezone.utc),
    )

    # Sample sitemap entries for liveness, but only if budget allows.
    liveness_samples = []
    if audit.unique_locs and not budget.exhausted()[0]:
        step = max(1, len(audit.unique_locs) // 5)
        for loc in audit.unique_locs[::step][:5]:
            if loc in crawled_urls:
                continue
            probe = fetcher.fetch(loc, method="HEAD", count_against_budget=False)
            liveness_samples.append({
                "url": loc, "status": probe.status, "redirect_chain": probe.redirect_chain,
            })
        sitemapaudit.record_liveness(audit, liveness_samples)

    # -- calibrate thresholds to this site -----------------------------
    config.calibrate_to_site([p.get("structure", {}) for p in pages if p.get("structure")])

    # -- findings ------------------------------------------------------
    render_summary = summarise_site(render_reports)
    findings = (
        sitemap_findings(audit, origin)
        + render_findings(render_reports, render_summary, config)
        + page_findings(pages, docs, config, target)
    )

    if not discovery.llms_txt:
        pass  # absence of llms.txt is a proactive suggestion, not a defect

    payload = {
        "target": target,
        "pages": pages,
        "discovery": {
            "candidates": len(discovery.candidates),
            "sampled": len(sample),
            "sitemap_found": discovery.sitemap_found,
            "sitemap_urls": discovery.sitemap_urls,
            "sitemap_entry_count": discovery.sitemap_entry_count,
            "llms_txt": discovery.llms_txt,
            "notes": discovery.notes,
            "page_type_spread": {
                t: sum(1 for p in pages if p.get("page_type") == t)
                for t in sorted({p.get("page_type", "other") for p in pages})
            },
        },
        "sitemap": audit.to_dict(),
        "render": {**render_summary, "pages": [r.to_dict() for r in render_reports]},
        "config": config.to_dict(),
        "budget_used": {
            "pages_fetched": budget.pages_fetched,
            "bytes_fetched": budget.bytes_fetched,
            "elapsed_seconds": round(budget.elapsed(), 1),
        },
        "pages_analysed": len(docs),
        "findings": [f.to_dict() for f in findings],
        "notes": discovery.notes,
    }
    workspace.write("reachability", payload)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"REACHABILITY  {origin}")
        print(f"  discovered {len(discovery.candidates)} candidates, sampled {len(sample)}, parsed {len(docs)}")
        print(f"  page types {payload['discovery']['page_type_spread']}")
        print(f"  sitemap    {'found' if audit.exists else 'MISSING'}"
              f" ({audit.total_entries} entries, {len(audit.problems)} problems)")
        print(f"  render     {render_summary.get('pages_with_blocking_faults', 0)}"
              f"/{render_summary.get('pages_analysed', 0)} pages with blocking faults")
        print(f"  elapsed    {budget.elapsed():.1f}s, {budget.pages_fetched} pages fetched")
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
