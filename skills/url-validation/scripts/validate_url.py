#!/usr/bin/env python3
"""Preflight gate: normalise, validate and scope a target URL.

Exit codes: 0 cleared, 1 refused, 2 internal error.
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

from geo_audit import robots as robots_mod  # noqa: E402
from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.fetcher import Budget, Fetcher  # noqa: E402
from geo_audit.netguard import UrlRejected, validate_url  # noqa: E402
from geo_audit.report import Finding, SuggestedAction  # noqa: E402
from geo_audit.workspace import Workspace  # noqa: E402


def _finding(**kwargs) -> Finding:
    action = kwargs.pop("action")
    return Finding(layer="preflight", suggested_action=action, **kwargs)


def _plaintext_variant(url: str) -> str:
    """The same URL with only the scheme swapped to ``http``.

    Host, port, path and query are preserved so the plaintext probe reaches the
    exact endpoint the HTTPS request targeted. Rebuilding the URL from ``host``
    and ``path`` alone silently drops a non-default port, which would point the
    probe at port 80 instead of the server actually being audited.
    """
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return url


def check_https(fetcher: Fetcher, target, findings: list[Finding], *, https_available: bool = True) -> dict:
    """Is the site HTTPS, and is HTTPS actually enforced?

    *https_available* is False when the HTTPS landing request failed at the
    transport layer and only the plaintext variant answered. That is a strictly
    worse condition than "HTTPS works but is not enforced", so it is graded
    ``critical`` rather than ``high``: the domain is not merely duplicated
    across schemes, it is unreachable over the scheme that engines prefer.
    """
    evidence: dict = {"scheme": target.scheme, "https_available": https_available}

    if target.scheme != "https":
        total = not https_available
        findings.append(_finding(
            title=(
                "Site serves no HTTPS at all" if total
                else "Site is served over plaintext HTTP"
            ),
            severity="critical" if total else "high",
            claim_level=1, confidence="high", check_id="no_https",
            observed=(
                f"https://{target.host}{target.path} did not answer; "
                f"final URL is {target.url}"
                if total else f"final URL is {target.url}"
            ),
            expected="https:// with a valid certificate",
            evidence=(
                (
                    f"The HTTPS variant of {target.host} failed at the transport "
                    f"layer, and the audit fell back to {target.url}, which served "
                    "content over plaintext HTTP. A host that serves no HTTPS at all "
                    "is the strongest form of this defect: there is no secure variant "
                    "for a crawler to prefer. Browsers mark the page insecure, and "
                    "several crawlers deprioritise or skip non-HTTPS documents "
                    "outright."
                ) if total else (
                    f"The audited target resolved to {target.url}, which uses plaintext "
                    "HTTP. Browsers mark it insecure and several crawlers deprioritise "
                    "or skip non-HTTPS documents outright."
                )
            ),
            affected_urls=[target.url],
            action=SuggestedAction(
                summary="Serve the site over HTTPS and redirect all HTTP traffic to it.",
                priority="critical",
                steps=[
                    "Obtain a certificate for the apex and www hosts.",
                    "Serve every route over HTTPS.",
                    "Return 301 from http:// to the https:// equivalent, preserving the path.",
                    "Add Strict-Transport-Security once redirects are confirmed.",
                ],
                effort="medium",
                mechanism=(
                    "HTTPS is an entry condition for the candidate pool. A document a "
                    "crawler will not retrieve cannot be selected as a source, so this "
                    "gates every downstream signal."
                ),
                verification="curl -I http://<domain>/ returns 301 to the https:// URL.",
            ),
        ))
        return evidence

    # HTTPS is live; confirm the HTTP variant redirects to it.
    http_url = _plaintext_variant(target.url)
    http_probe = fetcher.fetch(
        http_url, count_against_budget=False
    )
    evidence["http_probe_status"] = http_probe.status
    evidence["http_redirects_to_https"] = False

    if http_probe.redirect_chain:
        final = http_probe.redirect_chain[-1]["to"]
        evidence["http_redirects_to_https"] = final.startswith("https://")
        evidence["http_redirect_target"] = final

    if http_probe.ok and not http_probe.redirect_chain:
        findings.append(_finding(
            title="HTTPS is available but not enforced",
            severity="high", claim_level=1, confidence="high",
            check_id="https_not_enforced",
            observed=f"http://{target.host}{target.path} returned {http_probe.status} without redirecting",
            expected="301 redirect from http:// to https://",
            evidence=(
                f"Both http://{target.host} and https://{target.host} serve content "
                f"(HTTP returned {http_probe.status} with no redirect). The same page "
                "is reachable at two URLs, splitting signals between them and leaving "
                "a plaintext version citable."
            ),
            affected_urls=[f"http://{target.host}{target.path}", target.url],
            action=SuggestedAction(
                summary="Return a 301 from every HTTP URL to its HTTPS equivalent.",
                priority="high",
                steps=[
                    "Add a server-level 301 from http to https, preserving path and query.",
                    "Confirm no route answers 200 over plaintext HTTP.",
                    "Add Strict-Transport-Security with a conservative max-age first.",
                ],
                effort="low",
                mechanism=(
                    "Two live variants of one page divide corroboration between two "
                    "URLs. Consolidating them concentrates the signal on one address."
                ),
                verification="curl -I http://<domain>/<path> returns 301.",
            ),
        ))
    return evidence


def check_host_canonicalisation(fetcher: Fetcher, target, findings: list[Finding]) -> dict:
    """Do the apex and www hosts agree on which one is canonical?"""
    host = target.host
    other = host[4:] if host.startswith("www.") else f"www.{host}"
    probe = fetcher.fetch(
        f"{target.scheme}://{other}/", allow_offsite=True, count_against_budget=False
    )

    evidence = {
        "alternate_host": other,
        "status": probe.status,
        "redirected": bool(probe.redirect_chain),
    }
    if probe.ok and not probe.redirect_chain:
        findings.append(_finding(
            title="Apex and www hosts both serve content without redirecting",
            severity="medium", claim_level=1, confidence="high",
            check_id="host_canonicalisation",
            observed=f"both {host} and {other} return {probe.status}",
            expected="one host canonical, the other 301-redirecting to it",
            evidence=(
                f"{target.scheme}://{other}/ returned {probe.status} with no redirect, "
                f"while {target.origin}/ also serves content. The same site answers on "
                "two hostnames, so references, links and any corroboration a machine "
                "gathers are split across both."
            ),
            affected_urls=[f"{target.scheme}://{other}/", target.origin + "/"],
            action=SuggestedAction(
                summary="Pick one canonical host and 301 the other to it.",
                priority="medium",
                steps=[
                    f"Decide whether {host} or {other} is canonical.",
                    "301 every request on the non-canonical host to the canonical one.",
                    "Ensure rel=canonical on every page points at the canonical host.",
                    "Update the sitemap to list only canonical URLs.",
                ],
                effort="low",
                mechanism=(
                    "Handout appendix D: a claim repeated consistently across sources "
                    "is likelier to be believed. Two hostnames fragment the very "
                    "consistency that makes a brand's own pages trustworthy."
                ),
                verification=f"curl -I {target.scheme}://{other}/ returns 301.",
            ),
        ))
    return evidence


def check_robots(policy, target, findings: list[Finding]) -> dict:
    """Report on robots.txt, separating retrieval blocks from training blocks."""
    if not policy.exists:
        findings.append(_finding(
            title="No robots.txt found",
            severity="low", claim_level=1, confidence="high", check_id="robots_missing",
            observed=f"{policy.url} returned {policy.status or 'no response'}",
            expected="a robots.txt declaring crawl policy and the sitemap location",
            evidence=(
                f"{policy.url} returned {policy.status or 'no response'}. Crawling is "
                "unrestricted by default, so this is not blocking, but the site "
                "forgoes the cheapest place to declare its sitemap and state an "
                "explicit policy toward AI crawlers."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary="Add a robots.txt that declares the sitemap and an explicit AI-crawler policy.",
                priority="low",
                steps=[
                    "Create /robots.txt.",
                    "Add a Sitemap: line pointing at the full sitemap URL.",
                    "State an explicit policy for AI crawlers rather than relying on defaults.",
                ],
                effort="low",
                mechanism="A declared sitemap removes the guesswork from discovery.",
                verification="curl https://<domain>/robots.txt returns 200.",
            ),
        ))
        return {"exists": False, "status": policy.status, "error": policy.fetch_error}

    report = policy.ai_crawler_report("/")
    blocked_retrieval = [
        name for name, info in report.items()
        if not info["allowed"] and info["purpose"] in ("retrieval", "user-fetch")
    ]
    blocked_training = [
        name for name, info in report.items()
        if not info["allowed"] and info["purpose"] == "training"
    ]

    wildcard = policy.group_for("*")
    if wildcard and wildcard.blocks_everything():
        findings.append(_finding(
            title="robots.txt blocks all crawlers from the entire site",
            severity="critical", claim_level=1, confidence="high",
            check_id="robots_blocks_all",
            observed="User-agent: * with Disallow: / and no Allow rules",
            expected="crawlable public content",
            evidence=(
                f"{policy.url} contains a wildcard group disallowing the entire site. "
                "No compliant crawler will retrieve any page, so the brand cannot "
                "enter any answer engine's candidate pool at all."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary="Remove the site-wide Disallow and restrict only genuinely private paths.",
                priority="critical",
                steps=[
                    "Replace `Disallow: /` with specific paths that must stay private.",
                    "Verify public routes are crawlable after the change.",
                    "Add the sitemap directive.",
                ],
                effort="low",
                mechanism=(
                    "Retrieval is attempted on essentially every prompt (98.6-100% "
                    "search-trigger rates, arXiv:2604.25707 §6). A site-wide block "
                    "makes the brand unreachable at the one moment it could be cited."
                ),
                verification="Confirm a public URL is allowed for Googlebot and PerplexityBot.",
            ),
        ))

    elif blocked_retrieval:
        names = ", ".join(sorted(blocked_retrieval))
        operators = sorted({report[n]["operator"] for n in blocked_retrieval})
        findings.append(_finding(
            title="robots.txt blocks AI crawlers that fetch pages during answer generation",
            severity="critical", claim_level=1, confidence="high",
            check_id="robots_blocks_ai_retrieval",
            observed=f"blocked live-retrieval agents: {names}",
            expected="live-retrieval agents allowed on public content",
            evidence=(
                f"{policy.url} disallows {names} at '/'. These are live-retrieval and "
                f"user-fetch agents operated by {', '.join(operators)} — they fetch "
                "pages while an answer is being written, not on a training schedule. "
                "Blocking them prevents the brand from being cited even when an "
                "assistant is actively looking for it."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary=f"Allow the live-retrieval agents ({names}) on public content.",
                priority="critical",
                steps=[
                    f"Remove or narrow the Disallow rules covering {names}.",
                    "Keep training-crawler policy separate if the intent was to opt out of training.",
                    "Re-test with a crawl simulator for each named agent.",
                ],
                effort="low",
                mechanism=(
                    "Blocking retrieval and blocking training are different decisions "
                    "with different consequences. A brand can decline training-corpus "
                    "inclusion while still being citable in live answers; these rules "
                    "currently forfeit both."
                ),
                verification="Re-run this skill; blocked_retrieval must be empty.",
            ),
        ))

    if blocked_training and not blocked_retrieval:
        names = ", ".join(sorted(blocked_training))
        findings.append(_finding(
            title="robots.txt blocks AI training crawlers",
            severity="medium", claim_level=1, confidence="high",
            check_id="robots_blocks_ai_training",
            observed=f"blocked training agents: {names}",
            expected="a deliberate, documented decision either way",
            evidence=(
                f"{policy.url} disallows {names}, which are training-corpus crawlers. "
                "Live retrieval is unaffected, so the brand remains citable in answers "
                "that search the web. This is reported as a deliberate trade-off rather "
                "than a defect: it reduces the chance of the brand being known without "
                "a search, while preserving citation when a search happens."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary="Confirm the training opt-out is intentional; if not, allow these agents.",
                priority="low",
                steps=[
                    "Confirm with whoever owns the policy that the opt-out is deliberate.",
                    "If unintentional, remove the Disallow for the training agents.",
                    "If deliberate, keep it and rely on retrieval-layer visibility.",
                ],
                effort="low",
                mechanism=(
                    "Training inclusion affects unprompted brand recall; retrieval "
                    "affects citation during a live answer. Only the second is "
                    "measured by this audit."
                ),
                verification="Policy decision recorded; no technical verification needed.",
            ),
        ))

    if policy.parse_errors:
        findings.append(_finding(
            title="robots.txt contains unparseable directives",
            severity="medium", claim_level=1, confidence="high",
            check_id="robots_unparseable",
            observed=f"{len(policy.parse_errors)} malformed line(s)",
            expected="valid RFC 9309 syntax throughout",
            evidence=(
                f"{policy.url} has {len(policy.parse_errors)} line(s) that do not parse: "
                + "; ".join(policy.parse_errors[:3])
                + ". Crawlers differ in how they recover from syntax errors, so the "
                "effective policy is not the intended one."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary="Correct the malformed robots.txt lines.",
                priority="medium",
                steps=[
                    "Fix each reported line to `Field: value` form.",
                    "Ensure every rule sits under a User-agent group.",
                    "Re-validate with a robots.txt tester.",
                ],
                effort="low",
                mechanism="An ambiguous policy is enforced differently by each crawler.",
                verification="Re-run this skill; parse_errors must be empty.",
            ),
        ))

    delay = policy.crawl_delay_for("Googlebot") or 0
    if delay >= 10:
        findings.append(_finding(
            title="Crawl-delay is high enough to starve crawlers",
            severity="medium", claim_level=2, confidence="medium",
            check_id="crawl_delay_excessive",
            observed=f"Crawl-delay: {delay}",
            expected="under 5 seconds, or unset",
            evidence=(
                f"{policy.url} sets Crawl-delay: {delay}. At that rate a crawler "
                f"retrieves roughly {int(86400 / delay)} pages per day, so large "
                "sections of a site of any size go stale or unvisited."
            ),
            affected_urls=[policy.url],
            action=SuggestedAction(
                summary="Lower or remove Crawl-delay and rate-limit at the server instead.",
                priority="medium",
                steps=[
                    "Reduce Crawl-delay below 5, or remove it.",
                    "Apply rate limiting at the edge, where it can be selective.",
                ],
                effort="low",
                mechanism="Crawl budget consumed by waiting is crawl budget not spent on content.",
                verification="robots.txt shows no Crawl-delay above 5.",
            ),
        ))

    return {
        "exists": True,
        "status": policy.status,
        "url": policy.url,
        "sitemaps": policy.sitemaps,
        "group_count": len(policy.groups),
        "parse_errors": policy.parse_errors,
        "ai_crawlers": report,
        "blocked_retrieval": blocked_retrieval,
        "blocked_training": blocked_training,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight URL validation gate.")
    parser.add_argument("url", help="Target site: a bare domain or a full URL.")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=240.0)
    parser.add_argument("--strictness", choices=["lenient", "balanced", "strict"], default="balanced")
    parser.add_argument("--workspace", default=None, help="Base directory for run artefacts.")
    parser.add_argument("--config", default=None, help="Optional JSON config file.")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout.")
    args = parser.parse_args(argv)

    findings: list[Finding] = []

    # -- gate ----------------------------------------------------------
    try:
        target = validate_url(args.url)
    except UrlRejected as exc:
        payload = {
            "cleared": False,
            "input": args.url,
            "refusal": {"code": exc.code, "reason": exc.reason},
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"REFUSED: {exc.reason}  [{exc.code}]", file=sys.stderr)
            print("Nothing was fetched. No downstream skill may run.", file=sys.stderr)
        return 1

    config = AuditConfig.load(
        args.config,
        max_pages=args.max_pages,
        max_seconds=args.max_seconds,
        strictness=args.strictness,
    )
    budget = Budget(
        max_pages=config.max_pages,
        max_seconds=config.max_seconds,
        per_host_delay=config.per_host_delay,
    )
    fetcher = Fetcher(target.registrable_domain, budget, timeout=config.timeout)

    # -- reach the target, following and re-validating each hop --------
    # `allow_rescope` lets the *landing* request follow a redirect onto a
    # different registrable domain (cern.ch -> home.cern, example.com ->
    # example.org). Scope is then re-anchored below so the rest of the audit
    # still crawls exactly one domain. Every SSRF guard still applies to the
    # out-of-scope hop; see Fetcher.fetch.
    landing = fetcher.fetch(target.url, count_against_budget=False, allow_rescope=True)

    # A site with no HTTPS at all is a finding, not a reason to give up. When
    # the HTTPS landing request fails at the transport layer, ask the HTTP
    # variant before refusing: "this domain serves no HTTPS" is one of the most
    # consequential discoverability results the audit can return, and it is
    # precisely the case that would otherwise produce no report at all.
    https_available = True
    if target.scheme == "https" and not landing.ok and landing.error_code in (
        "transport_error", "timeout", "network_error", "no_safe_address"
    ):
        http_landing = fetcher.fetch(
            _plaintext_variant(target.url),
            count_against_budget=False,
            allow_rescope=True,
        )
        if http_landing.ok or http_landing.status is not None:
            https_available = False
            landing = http_landing

    if not landing.ok and landing.error_code in (
        "dns_failure", "network_error", "timeout", "resolves_private", "out_of_scope"
    ):
        payload = {
            "cleared": False,
            "input": args.url,
            "target": target.to_dict(),
            "refusal": {"code": landing.error_code, "reason": landing.error or "unreachable"},
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"REFUSED: {landing.error}  [{landing.error_code}]", file=sys.stderr)
        return 1

    # Re-anchor the audit on whatever the landing request actually reached.
    # Two things can move it: a plaintext-only site (handled by the HTTP
    # fallback above) and a redirect onto a different registrable domain.
    final_target = target
    if not https_available or landing.redirect_chain:
        try:
            final_target = validate_url(landing.final_url)
        except UrlRejected:
            final_target = target
    rescoped_from: str = ""
    if https_available and final_target.registrable_domain != target.registrable_domain:
        rescoped_from = target.registrable_domain
        # The audit now follows a different domain, so every later probe
        # must be scoped to it rather than to the domain that was asked
        # for. Rebuild the fetcher instead of mutating the old one.
        fetcher = Fetcher(
            final_target.registrable_domain, budget, timeout=config.timeout
        )
        # The status code of the hop decides how bad this is, so read it rather
        # than assuming. A 301/308 is a deliberate, signal-consolidating move; a
        # 302/307 across a host change is a temporary redirect where a permanent
        # one is wanted, and every future retrieval keeps paying for it.
        hop_status = next(
            (
                int(hop.get("status") or 0)
                for hop in landing.redirect_chain
                if hop.get("from") == target.url
            ),
            int(landing.redirect_chain[0].get("status") or 0)
            if landing.redirect_chain else 0,
        )
        permanent = hop_status in (301, 308)
        status_text = f"a {hop_status} redirect" if hop_status else "a redirect"

        findings.append(_finding(
            title="Landing URL redirects to a different domain",
            severity="low" if permanent else "medium",
            claim_level=2, confidence="high",
            check_id="cross_domain_redirect",
            observed=(
                f"{target.registrable_domain} redirects to "
                f"{final_target.registrable_domain} via HTTP {hop_status}"
                if hop_status else
                f"{target.registrable_domain} redirects to {final_target.registrable_domain}"
            ),
            expected="the audited domain serves the requested URL directly",
            evidence=(
                f"{target.url} answered with {status_text} to {landing.final_url}, "
                f"moving the canonical host from {target.registrable_domain} to "
                f"{final_target.registrable_domain}. The audit re-anchored its scope "
                "to the final domain and reports on that entity, because that is "
                "what an engine retrieves and cites. Links and citations that still "
                "point at the old domain pay an extra hop, and trust signals "
                "(citations, knowledge-panel facts) are not transferred "
                "automatically."
                + (
                    ""
                    if permanent
                    else f" This hop is not permanent (HTTP {hop_status}), so "
                    "engines are told to keep treating the old host as canonical "
                    "while the new one is the address actually served."
                )
            ),
            affected_urls=[target.url, landing.final_url],
            action=SuggestedAction(
                summary=(
                    f"Make {target.registrable_domain} a permanent 301 redirect to "
                    f"{final_target.registrable_domain} and update every reference "
                    "to it."
                    if not permanent else
                    f"Serve {target.registrable_domain} directly, or make it a "
                    f"permanent redirect with all references updated to "
                    f"{final_target.registrable_domain}."
                ),
                priority="low" if permanent else "medium",
                steps=[
                    (
                        "Keep the redirect permanent (301/308) so ranking signals consolidate."
                        if permanent else
                        f"Change the {hop_status} redirect to a 301 (or 308) so ranking "
                        "signals consolidate on the domain actually served."
                    ),
                    f"Update canonical tags, sitemap <loc> entries and internal links to {final_target.registrable_domain}.",
                    "Update off-site references (profiles, citations, social) to the final domain.",
                ],
                effort="low",
                mechanism=(
                    "Redirect hops are a documented retrieval risk: "
                    "arXiv:2604.25707 §4.1 reports a 76.44% fetch success rate "
                    "across pages engines chose to cite, so every hop is another "
                    "chance for a fetcher to give up."
                    + (
                        ""
                        if permanent else
                        " A temporary status also withholds the consolidation a "
                        "permanent redirect performs, so the two hosts keep "
                        "competing for the same claims."
                    )
                ),
                verification=f"curl -IL {target.url} resolves to {final_target.registrable_domain} in one permanent hop.",
            ),
        ))
    if len(landing.redirect_chain) >= 3:
        findings.append(_finding(
            title="Landing URL passes through a long redirect chain",
            severity="medium", claim_level=1, confidence="high",
            check_id="redirect_chain_long",
            observed=f"{len(landing.redirect_chain)} hops",
            expected="at most one redirect to the canonical URL",
            evidence=(
                f"{target.url} reached {landing.final_url} through "
                f"{len(landing.redirect_chain)} redirects: "
                + " -> ".join(
                    [target.url] + [hop["to"] for hop in landing.redirect_chain]
                )
                + ". Each hop costs latency and crawl budget, and some fetchers "
                "abandon long chains entirely."
            ),
            affected_urls=[target.url, landing.final_url],
            action=SuggestedAction(
                summary="Collapse the redirect chain to a single hop to the final URL.",
                priority="medium",
                steps=[
                    "Identify the final canonical URL.",
                    "Rewrite the first redirect to point straight at it.",
                    "Remove now-unreachable intermediate rules.",
                ],
                effort="low",
                mechanism=(
                    "arXiv:2604.25707 §4.1 reports a 76.44% fetch success rate "
                    "across pages engines chose to cite. Retrieval reliability is "
                    "not a given, and every extra hop is another chance to fail."
                ),
                verification="curl -IL <url> shows a single 301 to the final URL.",
            ),
        ))

    https_evidence = check_https(
        fetcher, final_target, findings, https_available=https_available
    )
    host_evidence = check_host_canonicalisation(fetcher, final_target, findings)

    # -- robots --------------------------------------------------------
    robots_url = robots_mod.robots_url_for(final_target.origin)
    robots_response = fetcher.fetch(robots_url, count_against_budget=False)
    if robots_response.ok and robots_response.body.strip():
        policy = robots_mod.parse_robots(
            robots_response.body, url=robots_url, status=robots_response.status
        )
    else:
        policy = robots_mod.missing_policy(
            robots_url, robots_response.status, robots_response.error
        )
    robots_evidence = check_robots(policy, final_target, findings)

    # -- persist -------------------------------------------------------
    workspace = Workspace.create(final_target.host, base=args.workspace)
    payload = {
        "cleared": True,
        "input": args.url,
        "target": final_target.to_dict(),
        "landing": {
            "status": landing.status,
            "final_url": landing.final_url,
            "elapsed_ms": landing.elapsed_ms,
            "content_type": landing.content_type,
        },
        "redirect_chain": landing.redirect_chain,
        "rescoped_from": rescoped_from,
        "https": https_evidence,
        "host_canonicalisation": host_evidence,
        "robots": robots_evidence,
        "config": config.to_dict(),
        "budget": {
            "max_pages": budget.max_pages,
            "max_seconds": budget.max_seconds,
            "per_host_delay": budget.per_host_delay,
        },
        "findings": [f.to_dict() for f in findings],
        "notes": final_target.notes,
    }
    workspace.write("preflight", payload)

    if args.json:
        print(json.dumps({**payload, "workspace": str(workspace.root)}, indent=2))
    else:
        print(f"CLEARED  {final_target.url}")
        print(f"  scope      {final_target.registrable_domain}")
        if rescoped_from:
            print(f"  RESCOPED   {rescoped_from} -> {final_target.registrable_domain}")
        print(f"  resolved   {', '.join(final_target.resolved_ips) or 'n/a'}")
        print(f"  robots     {'present' if policy.exists else 'absent'}"
              + (f", {len(policy.sitemaps)} sitemap(s) declared" if policy.sitemaps else ""))
        if robots_evidence.get("blocked_retrieval"):
            print(f"  BLOCKED    {', '.join(robots_evidence['blocked_retrieval'])}")
        print(f"  findings   {len(findings)}")
        for finding in findings:
            print(f"    [{finding.severity:8s}] {finding.title}")
        print(f"  workspace  {workspace.root}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - a crash here must be legible
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
