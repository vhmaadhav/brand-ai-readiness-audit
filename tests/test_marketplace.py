#!/usr/bin/env python3
"""Test suite for the brand-ai-readiness-audit marketplace.

Run:  python3 tests/test_marketplace.py            (from the marketplace root)
      python3 -m unittest discover -s tests -v

Standard library only, like everything else here. The end-to-end tests serve a
deliberately flawed fixture site over real HTTP on an ephemeral port, so the
whole stack is exercised rather than mocked.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_site import EXPECTED_FINDINGS, FixtureSite  # noqa: E402
from geo_audit import charts, sitemapaudit  # noqa: E402
from geo_audit.config import AuditConfig  # noqa: E402
from geo_audit.htmldoc import jsonld_types, iter_jsonld_nodes, parse_html  # noqa: E402
from geo_audit.influence import score_absorption  # noqa: E402
from geo_audit.netguard import (  # noqa: E402
    UrlRejected,
    classify_ip,
    registrable_domain,
    validate_url,
)
from geo_audit.pdfwrite import PdfDocument, verify_xref  # noqa: E402
from geo_audit.renderfaults import diagnose  # noqa: E402
from geo_audit.report import (  # noqa: E402
    Finding,
    SuggestedAction,
    build_report,
    cap_severity,
    validate_report,
)
from geo_audit.robots import (  # noqa: E402
    OWN_USER_AGENT_TOKEN,
    parse_robots,
)
from geo_audit.textstats import profile_genres  # noqa: E402


# ===========================================================================
# Security: the SSRF guard
# ===========================================================================

class TestNetguard(unittest.TestCase):
    """The URL validator is the marketplace's security boundary."""

    ATTACKS = [
        ("http://localhost/", "loopback by name"),
        ("http://127.0.0.1/", "loopback by address"),
        ("http://[::1]/", "IPv6 loopback"),
        ("http://169.254.169.254/latest/meta-data/", "AWS metadata"),
        ("http://metadata.google.internal/", "GCP metadata"),
        ("http://100.100.100.200/", "Alibaba metadata"),
        ("http://192.168.1.1/", "RFC1918 /16"),
        ("http://10.0.0.5/", "RFC1918 /8"),
        ("http://172.16.0.1/", "RFC1918 /12"),
        ("http://100.64.1.1/", "CGNAT"),
        ("http://[fd00::1]/", "IPv6 ULA"),
        ("http://[::ffff:127.0.0.1]/", "IPv4-mapped loopback"),
        ("http://[64:ff9b::7f00:1]/", "NAT64-wrapped loopback"),
        ("http://[64:ff9b::a9fe:a9fe]/", "NAT64-wrapped cloud metadata"),
        ("http://[64:ff9b::c0a8:101]/", "NAT64-wrapped RFC1918"),
        ("http://2130706433/", "decimal IP"),
        ("http://0x7f000001/", "hex IP"),
        ("http://0177.0.0.1/", "octal IP"),
        ("file:///etc/passwd", "file scheme"),
        ("gopher://x.com/", "gopher scheme"),
        ("javascript:alert(1)", "javascript scheme"),
        ("http://user:pw@example.com/", "embedded credentials"),
        ("http://foo.internal/", "reserved suffix .internal"),
        ("http://box.local/", "reserved suffix .local"),
        ("http://example.com:22/", "non-allowlisted port"),
        ("http://0.0.0.0/", "unspecified address"),
        ("http://singlelabel/", "not an FQDN"),
        ("", "empty input"),
    ]

    def test_every_attack_is_rejected(self):
        for url, why in self.ATTACKS:
            with self.subTest(vector=why):
                with self.assertRaises(UrlRejected, msg=f"{why} was NOT blocked: {url}"):
                    validate_url(url, resolve_dns=False)

    def test_legitimate_urls_are_accepted(self):
        for url in ["example.com", "https://www.adobe.com/products",
                    "http://neverssl.com/", "https://sub.domain.co.uk/a?q=1",
                    "  https://example.com  "]:
            with self.subTest(url=url):
                validate_url(url, resolve_dns=False)

    def test_nat64_wrapped_public_address_stays_public(self):
        """A DNS64 answer must not make a public host look internal.

        On an IPv6-only network the resolver synthesises AAAA records under
        64:ff9b::/96 with the real IPv4 in the last 32 bits. Treating that as
        "reserved" refuses every site on the network; unwrapping classifies the
        address that is actually going to be contacted.
        """
        is_public, reason = classify_ip("64:ff9b::23be:6b3")  # 35.190.6.179
        self.assertTrue(is_public, f"NAT64-wrapped public host refused: {reason}")
        self.assertEqual(reason, "public unicast address")

    def test_nat64_unwrap_does_not_smuggle_internal_targets(self):
        """Unwrapping is a classification fix, not a bypass."""
        for wrapped, why in [
            ("64:ff9b::7f00:1", "loopback"),
            ("64:ff9b::a9fe:a9fe", "link-local cloud metadata"),
            ("64:ff9b::c0a8:101", "RFC1918"),
            ("64:ff9b::6440:101", "CGNAT"),
        ]:
            with self.subTest(why=why):
                is_public, reason = classify_ip(wrapped)
                self.assertFalse(
                    is_public, f"{why} smuggled past the NAT64 unwrap: {reason}"
                )

    def test_fragment_is_stripped(self):
        self.assertNotIn("#", validate_url("https://x.com/a#frag", resolve_dns=False).url)

    def test_scope_confinement(self):
        with self.assertRaises(UrlRejected):
            validate_url("https://evil.com/", scope_domain="good.com", resolve_dns=False)
        validate_url("https://sub.good.com/", scope_domain="good.com", resolve_dns=False)

    def test_registrable_domain_handles_compound_suffixes(self):
        cases = {"www.bbc.co.uk": "bbc.co.uk", "shop.example.com": "example.com",
                 "example.com": "example.com", "a.b.c.com.au": "c.com.au"}
        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(registrable_domain(host), expected)


# ===========================================================================
# robots.txt
# ===========================================================================

class TestRobots(unittest.TestCase):
    SAMPLE = """
User-agent: GPTBot
Disallow: /

User-agent: PerplexityBot
User-agent: ClaudeBot
Disallow: /

User-agent: Googlebot
Allow: /
Crawl-delay: 1

User-agent: *
Disallow: /admin
Disallow: /private/*.json$
Allow: /admin/public
Sitemap: https://example.com/sitemap.xml
"""

    def setUp(self):
        self.policy = parse_robots(self.SAMPLE, url="https://example.com/robots.txt")

    def test_grouped_agents_share_rules(self):
        self.assertFalse(self.policy.can_fetch("PerplexityBot", "/"))
        self.assertFalse(self.policy.can_fetch("ClaudeBot", "/"))

    def test_named_group_beats_wildcard(self):
        self.assertFalse(self.policy.can_fetch("GPTBot", "/"))
        self.assertTrue(self.policy.can_fetch("Bingbot", "/"))

    def test_longest_match_and_allow_precedence(self):
        self.assertFalse(self.policy.can_fetch("SomeBot", "/admin"))
        self.assertTrue(self.policy.can_fetch("SomeBot", "/admin/public"))

    def test_dollar_anchor_and_wildcard(self):
        self.assertFalse(self.policy.can_fetch("SomeBot", "/private/a.json"))
        self.assertTrue(self.policy.can_fetch("SomeBot", "/private/a.html"))

    def test_sitemap_directive(self):
        self.assertEqual(self.policy.sitemaps, ["https://example.com/sitemap.xml"])

    def test_ai_report_separates_retrieval_from_training(self):
        report = self.policy.ai_crawler_report("/")
        blocked = {k for k, v in report.items() if not v["allowed"]}
        self.assertIn("PerplexityBot", blocked)
        self.assertEqual(report["PerplexityBot"]["purpose"], "retrieval")
        self.assertEqual(report["Perplexity-User"]["purpose"], "user-fetch")
        self.assertEqual(report["GPTBot"]["purpose"], "training")

    def test_malformed_lines_are_reported_not_raised(self):
        policy = parse_robots("this is not a directive\nUser-agent: *\nDisallow: /x")
        self.assertTrue(policy.parse_errors)
        self.assertFalse(policy.can_fetch("Any", "/x"))


# ===========================================================================
# HTML parsing
# ===========================================================================

class TestHtmlDoc(unittest.TestCase):
    def test_extracts_structure_and_jsonld(self):
        doc = parse_html(
            '<html lang="en"><head><title>T</title>'
            '<script type="application/ld+json">{"@type":"Organization","name":"A"}</script>'
            '</head><body><main><h1>H</h1><p>Body text here.</p>'
            '<ul><li>one</li></ul></main></body></html>',
            url="https://x.com/", scope_host="x.com")
        self.assertEqual(doc.title, "T")
        self.assertEqual(doc.lang, "en")
        self.assertEqual(doc.heading_count, 1)
        self.assertEqual(doc.paragraph_count, 1)
        self.assertEqual([jsonld_types(n) for n in iter_jsonld_nodes(doc.json_ld)],
                         [["Organization"]])

    def test_main_content_excludes_nav_and_footer(self):
        doc = parse_html(
            '<html><body><nav><a href="/a">NAVLINK</a></nav>'
            '<main><h1>Real</h1><p>Real content.</p></main>'
            '<footer>FOOTERTEXT</footer></body></html>',
            url="https://x.com/", scope_host="x.com")
        self.assertNotIn("NAVLINK", doc.main_text)
        self.assertNotIn("FOOTERTEXT", doc.main_text)
        self.assertIn("Real content", doc.main_text)

    def test_script_content_is_never_visible_text(self):
        doc = parse_html(
            '<html><body><p>visible</p>'
            '<script>var hidden="SECRETSTRING";</script></body></html>',
            url="https://x.com/")
        self.assertNotIn("SECRETSTRING", doc.text)
        self.assertGreater(doc.inline_script_chars, 0)

    def test_invalid_jsonld_is_recorded_not_raised(self):
        doc = parse_html(
            '<html><head><script type="application/ld+json">{bad</script></head><body></body></html>',
            url="https://x.com/")
        self.assertTrue(doc.json_ld_errors)
        self.assertEqual(doc.json_ld, [])

    def test_malformed_html_does_not_crash(self):
        for bad in ["<html><body><p>unclosed", "<<>><div class=", "",
                    "<html>" * 400, "<script>var a='</p>'</script><p>after</p>",
                    "<p>" + "x" * 50000 + "</p>"]:
            with self.subTest(sample=bad[:24]):
                parse_html(bad, url="https://x.com/")


# ===========================================================================
# Evidence genres and absorption
# ===========================================================================

class TestTextstats(unittest.TestCase):
    def test_detects_positive_genres(self):
        text = ("A widget is defined as a fastener. It refers to a joint. "
                "It holds 450 kg at 20 mm and costs $49 across 1,200 installs. "
                "Compared to welding it is reversible; in contrast welds are permanent. "
                "The difference between them is reversibility.")
        genres = profile_genres(text)
        self.assertIn("definition", genres.positive_genres)
        self.assertIn("numeric", genres.positive_genres)
        self.assertIn("comparison", genres.positive_genres)

    def test_qa_is_detected_but_never_counted_as_positive(self):
        text = ("Q: What is it? A: It is great. Q: Why? A: Because. "
                "Q: How? A: Somehow. What should you do?")
        genres = profile_genres(text)
        self.assertTrue(genres.qa_format)
        self.assertNotIn("qa_format", genres.positive_genres)
        self.assertEqual(genres.genre_count, 0)

    def test_incidental_marker_does_not_credit_a_genre(self):
        genres = profile_genres("We are better than the rest. Nothing else here at all.")
        self.assertNotIn("comparison", genres.positive_genres)


class TestAbsorption(unittest.TestCase):
    def test_rich_page_outscores_thin_page(self):
        rich_html = ("<html><body><main><h1>A</h1>"
                     + "".join(f"<h2>S{i}</h2><p>A widget is defined as a fastener rated "
                               f"to {i * 50} kg costing ${i * 9} across 1,200 units. "
                               f"Compared to welding, in contrast, the difference between "
                               f"them is reversibility.</p>" for i in range(1, 12))
                     + "</main></body></html>")
        thin_html = "<html><body><main><h1>A</h1><p>We are great.</p></main></body></html>"

        rich_doc = parse_html(rich_html, url="https://x.com/a")
        thin_doc = parse_html(thin_html, url="https://x.com/b")
        rich = score_absorption(rich_doc, profile_genres(rich_doc.main_text))
        thin = score_absorption(thin_doc, profile_genres(thin_doc.main_text))

        self.assertGreater(rich.score, thin.score)
        self.assertGreater(rich.score, 0.4)
        self.assertLess(thin.score, 0.2)

    def test_qa_without_evidence_is_flagged_in_notes(self):
        html = ("<html><body><main><h1>FAQ</h1>"
                "<p>Q: What? A: Great. Q: Why? A: Because. Q: How? A: Somehow.</p>"
                "</main></body></html>")
        doc = parse_html(html, url="https://x.com/faq")
        score = score_absorption(doc, profile_genres(doc.main_text))
        self.assertTrue(any("Q&A" in n or "-5.74" in n for n in score.notes))

    def test_score_stays_in_unit_range(self):
        for html in ["<html><body></body></html>",
                     "<html><body><main><p>" + ("word " * 40000) + "</p></main></body></html>"]:
            doc = parse_html(html, url="https://x.com/")
            score = score_absorption(doc, profile_genres(doc.main_text))
            self.assertGreaterEqual(score.score, 0.0)
            self.assertLessEqual(score.score, 1.0)


# ===========================================================================
# Render faults
# ===========================================================================

class TestRenderFaults(unittest.TestCase):
    def _diagnose(self, html):
        doc = parse_html(html, url="https://x.com/p")
        return diagnose(html, doc, url="https://x.com/p")

    def test_healthy_page_has_no_faults(self):
        html = ("<html><body><main><h1>Guide</h1><p>"
                + "Real substantial content in the markup. " * 40 + "</p></main></body></html>")
        self.assertEqual(self._diagnose(html).faults, [])

    def test_state_blob_is_preferred_over_empty_shell_when_recoverable(self):
        payload = ", ".join(
            f'"k{i}":"A substantial sentence of real product copy number {i} that a reader would want."'
            for i in range(40))
        html = ('<html><head><title>P</title></head><body><div id="root"></div>'
                f'<script>window.__NEXT_DATA__={{{payload}}}</script></body></html>')
        report = self._diagnose(html)
        codes = {f.code for f in report.faults}
        self.assertIn("state_blob", codes)
        self.assertNotIn("empty_shell", codes)
        self.assertTrue(next(f for f in report.faults if f.code == "state_blob").recoverable)

    def test_empty_shell_when_nothing_is_recoverable(self):
        html = ('<html><head><title>P</title></head><body><div id="root"></div>'
                '<script src="/app.js"></script></body></html>')
        report = self._diagnose(html)
        self.assertIn("empty_shell", {f.code for f in report.faults})

    def test_hash_routing_is_detected(self):
        html = ('<html><body><script>react-router</script>'
                '<a href="#!/a">A</a><div id="root"></div></body></html>')
        self.assertIn("client_routing", {f.code for f in self._diagnose(html).faults})

    def test_meta_only_is_suppressed_when_a_shell_fault_fired(self):
        html = ('<html><head><title>P</title><meta name="description" content="d">'
                '</head><body><div id="root"></div><script src="/a.js"></script></body></html>')
        codes = {f.code for f in self._diagnose(html).faults}
        self.assertNotIn("meta_only", codes)


# ===========================================================================
# Sitemap audit
# ===========================================================================

class TestSitemapAudit(unittest.TestCase):
    XML = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://x.com/a</loc><lastmod>2019-01-01</lastmod></url>
<url><loc>https://x.com/b</loc><lastmod>not-a-date</lastmod></url>
<url><loc>http://x.com/c</loc></url>
<url><loc>https://other.invalid/d</loc></url>
</urlset>"""

    def _audit(self, crawled=None):
        audit = sitemapaudit.SitemapAudit()
        audit.files.append(sitemapaudit.parse_sitemap_file(
            self.XML, "https://x.com/sitemap.xml", status=200, ok=True))
        return sitemapaudit.analyse(
            audit, scope_domain="x.com", origin_scheme="https",
            crawled_urls=crawled or [])

    def test_detects_the_planted_defects(self):
        codes = {p["code"] for p in self._audit().problems}
        for expected in ("sitemap_off_scope", "sitemap_scheme_mismatch",
                         "sitemap_bad_lastmod", "sitemap_not_declared"):
            self.assertIn(expected, codes)

    def test_orphan_detection(self):
        codes = {p["code"] for p in self._audit(
            crawled=["https://x.com/a", "https://x.com/orphan1", "https://x.com/orphan2"]
        ).problems}
        self.assertIn("sitemap_orphans", codes)

    def test_lastmod_parsing(self):
        parsed, valid = sitemapaudit.parse_lastmod("2026-03-01T10:00:00Z")
        self.assertTrue(valid)
        self.assertIsNotNone(parsed)
        self.assertEqual(sitemapaudit.parse_lastmod("garbage"), (None, False))

    def test_missing_sitemap_is_a_problem(self):
        audit = sitemapaudit.SitemapAudit()
        audit.files.append(sitemapaudit.SitemapFile(url="https://x.com/sitemap.xml", ok=False))
        codes = {p["code"] for p in sitemapaudit.analyse(
            audit, scope_domain="x.com", origin_scheme="https").problems}
        self.assertIn("sitemap_missing", codes)


# ===========================================================================
# Claim-level discipline
# ===========================================================================

class TestClaimLevels(unittest.TestCase):
    def _finding(self, severity, level):
        return Finding(title="t", severity=severity,
                       evidence="8 of 11 pages at https://x.com/a lack it",
                       suggested_action=SuggestedAction("do the thing", "high"),
                       layer="absorption", claim_level=level)

    def test_severity_is_capped_by_claim_level(self):
        self.assertEqual(self._finding("critical", 1).severity, "critical")
        self.assertEqual(self._finding("critical", 2).severity, "high")
        self.assertEqual(self._finding("critical", 3).severity, "medium")
        self.assertEqual(cap_severity("critical", 4), "info")

    def test_report_validates_and_counts_correctly(self):
        report = build_report(site="x.com", findings=[
            self._finding("critical", 1), self._finding("critical", 3)])
        self.assertEqual(validate_report(report), [])
        self.assertEqual(report["summary"]["total_findings"], 2)
        self.assertEqual(report["summary"]["critical"], 1)
        self.assertEqual(report["summary"]["medium"], 1)

    def test_validator_rejects_level_4_findings(self):
        report = build_report(site="x.com", findings=[self._finding("info", 1)])
        report["findings"][0]["claim_level"] = 4
        self.assertTrue(any("Level 4" in e for e in validate_report(report)))

    def test_validator_catches_tampered_counts(self):
        report = build_report(site="x.com", findings=[self._finding("high", 1)])
        report["summary"]["total_findings"] = 99
        self.assertTrue(any("total_findings" in e for e in validate_report(report)))

    def test_validator_requires_evidence_and_action(self):
        report = {
            "site": "x", "audited_at": "2026-01-01T00:00:00Z",
            "summary": {"total_findings": 1, "critical": 1, "high": 0, "medium": 0},
            "findings": [{"id": "F-001", "title": "t", "severity": "critical",
                          "evidence": "", "suggested_action": {"summary": "", "priority": "nope"}}],
        }
        errors = " ".join(validate_report(report))
        self.assertIn("evidence", errors)
        self.assertIn("priority", errors)


# ===========================================================================
# Agent-review regressions (agent verdict 2026-09-09: REGENERATE)
# ===========================================================================

class TestAgentReviewRegressions(unittest.TestCase):
    """Two defects the report-level review agent caught on cap.kpriet.ac.in.

    1. Advisory-table renderers sliced titles/reasons mid-word, garbling the
       critic trail in published artefacts.
    2. A sample-sufficiency downgrade moved severity critical->high but left
       suggested_action.priority at "critical" with no explanation.
    """

    def _report_with_dropped(self, title, reason):
        return {
            "site": "x.com", "audited_at": "2026-09-09T09:57:33Z",
            "summary": {"total_findings": 0, "critical": 0, "high": 0,
                        "medium": 0, "low": 0, "info": 0},
            "findings": [],
            "scope": {"pages_analysed": 1, "pages_discovered": 1,
                      "runtime_seconds": 1.0},
            "advisory": {"findings_in": 1, "findings_out": 0,
                         "dropped": [{"title": title, "check_id": "SD005",
                                      "gate": "sample_sufficiency",
                                      "reason": reason}],
                         "downgraded": [], "merged": []},
            "generator": {"marketplace": "brand-ai-readiness-audit",
                          "version": "1.0.0", "grounding": "test"},
        }

    def test_advisory_titles_survive_all_renderers_whole(self):
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import render_report
        title = ("The brand declares no organisation entity anywhere in the sample")
        reason = ("claims a site-wide pattern from a sample of 1, below the "
                  "minimum of 3 required for a population claim")
        report = self._report_with_dropped(title, reason)
        self.assertIn(title, render_report.render_markdown(report),
                      "markdown advisory table must not truncate titles")
        html = render_report.render_html(
            report, {"severity": None, "layers": None, "mix": None,
                     "absorption": None, "genres": None, "lengths": None})
        self.assertIn(title, html,
                      "html advisory table must not truncate titles")
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "trail.pdf")
            render_report.render_pdf(report, {}, out)
            data = Path(out).read_bytes()
            self.assertTrue(data.startswith(b"%PDF-"))
            self.assertEqual(verify_xref(data), [])

    def test_priority_is_aligned_to_downgraded_severity(self):
        """Sample-sufficiency demotion must cap action priority at severity."""
        result = self._advise_via_script(
            severity="critical", priority="critical",
            evidence="1 of 1 sampled page (https://x.com/p1) lacks the thing.",
            observed="1 of 1 pages", sample_size=1)
        self.assertEqual(result["output_findings"], 1)
        survivor = result["reviewed_findings"][0]
        self.assertEqual(survivor["severity"], "high")
        self.assertEqual(survivor["suggested_action"]["priority"], "high")
        gates = [d["gate"] for d in result["downgraded"]]
        self.assertIn("priority_alignment", gates)

    def test_priority_below_severity_is_left_alone(self):
        """Alignment caps, never raises: a low-effort medium may jump the queue."""
        result = self._advise_via_script(
            severity="high", priority="medium",
            evidence="8 of 11 sampled pages such as https://x.com/p1 lack the thing.",
            observed="8 of 11 pages", sample_size=11)
        survivor = result["reviewed_findings"][0]
        self.assertEqual(survivor["severity"], "high")
        self.assertEqual(survivor["suggested_action"]["priority"], "medium")

    # -- helpers ---------------------------------------------------------
    def _advise_via_script(self, *, severity, priority, evidence,
                           observed, sample_size):
        run_dir = Path(tempfile.mkdtemp()) / "run-1"
        run_dir.mkdir(parents=True)
        (run_dir / "01-url-validation.json").write_text(json.dumps({
            "cleared": True,
            "target": {"host": "x.com", "registrable_domain": "x.com",
                       "origin": "https://x.com", "scheme": "https",
                       "url": "https://x.com/"},
            "config": {"min_sample_for_population_claim": 3},
            "findings": [],
        }))
        (run_dir / "02-crawl-reachability.json").write_text(json.dumps({
            "pages": [{"url": "https://x.com/p1", "page_type": "article"}],
            "config": {"min_sample_for_population_claim": 3},
            "findings": [{
                "id": "F-001", "title": "A finding", "severity": severity,
                "evidence": evidence,
                "suggested_action": {"summary": "Do the specific thing described.",
                                     "priority": priority},
                "layer": "absorption", "claim_level": 1, "confidence": "high",
                "check_id": "generic_check", "sample_size": sample_size,
                "observed": observed,
                "affected_urls": ["https://x.com/p1"],
            }],
        }))
        cmd = [sys.executable, str(ROOT / "skills/advisory-review/scripts/advise.py"),
               "--workspace", str(run_dir), "--json"]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    @staticmethod
    def tmp_dir():
        return tempfile.gettempdir()


# ===========================================================================
# Review loop (orchestrator step 9: ACCEPT / REGENERATE with repairs)
# ===========================================================================

class TestReviewLoop(unittest.TestCase):
    """The deterministic half of the spec's review agent.

    The spec charges the review loop with re-checking the draft against the
    schema and the stage artefacts, returning VERDICT: ACCEPT or VERDICT:
    REGENERATE with numbered issues and concrete repairs — removal, demotion,
    rewording or re-rendering only, and never reinstating a dropped finding.
    """

    REVIEW = ROOT / "skills" / "audit-orchestrator" / "scripts" / "review_report.py"

    # -- fixtures --------------------------------------------------------
    def _write_run(self, findings, dropped=None, tmp=None):
        tmp = Path(tmp or tempfile.mkdtemp())
        run = tmp / "run-1"
        run.mkdir(parents=True, exist_ok=True)
        (run / "01-url-validation.json").write_text(json.dumps({
            "cleared": True,
            "target": {"host": "x.com", "registrable_domain": "x.com",
                       "origin": "https://x.com", "scheme": "https",
                       "url": "https://x.com/"},
            "config": {"min_sample_for_population_claim": 3},
            "findings": [],
        }))
        (run / "02-crawl-reachability.json").write_text(json.dumps({
            "pages": [{"url": "https://x.com/p1", "page_type": "article"}],
            "config": {"min_sample_for_population_claim": 3},
            "findings": [],
        }))
        (run / "07-advisory-review.json").write_text(json.dumps({
            "input_findings": len(findings), "output_findings": len(findings),
            "dropped": dropped or [], "downgraded": [], "merged": [],
            "reviewed_findings": findings,
            "proactive_recommendations": [], "layers_reviewed": ["reachability"],
            "verdict": {"severity_counts": {}, "rejection_rate": 0.0, "notes": "ok"},
        }))
        return run

    def _finding(self, **over):
        base = {
            "id": "F-001",
            "title": "8 of 11 sampled pages (e.g. https://x.com/p1) carry no JSON-LD",
            "severity": "high",
            "evidence": "8 of 11 sampled pages (https://x.com/p1) carry no JSON-LD block.",
            "suggested_action": {"summary": "Add Organization JSON-LD to the page template.",
                                 "priority": "high"},
            "layer": "selection", "claim_level": 1, "confidence": "high",
            "check_id": "no_jsonld", "observed": "8 of 11 pages",
            "expected": "pages carry JSON-LD",
            "affected_urls": ["https://x.com/p1"], "sample_size": 11,
        }
        base.update(over)
        return base

    def _draft(self, run, findings, summary=None):
        report = {
            "site": "x.com", "audited_at": "2026-09-09T10:00:00Z",
            "summary": summary or {"total_findings": len(findings), "critical": 0,
                                   "high": len(findings), "medium": 0,
                                   "low": 0, "info": 0},
            "findings": findings,
            "advisory": json.loads((run / "07-advisory-review.json").read_text()),
            "generator": {"marketplace": "brand-ai-readiness-audit",
                          "version": "1.1.0", "grounding": "test"},
        }
        path = run.parent / "draft.json"
        path.write_text(json.dumps(report, indent=2))
        return path

    def _review(self, draft, run, *extra):
        cmd = [sys.executable, str(self.REVIEW), "--report", str(draft),
               "--workspace", str(run), "--json", *extra]
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        payload = json.loads(completed.stdout)
        payload["_rc"] = completed.returncode
        return payload

    # -- verdicts --------------------------------------------------------
    def test_clean_draft_accepts(self):
        run = self._write_run([self._finding()])
        draft = self._draft(run, [self._finding()])
        payload = self._review(draft, run)
        self.assertEqual(payload["verdict"], "ACCEPT", payload["issues"])
        self.assertEqual(payload["_rc"], 0)

    def test_stale_summary_is_repaired(self):
        run = self._write_run([self._finding()])
        bad_summary = {"total_findings": 9, "critical": 9, "high": 0,
                       "medium": 0, "low": 0, "info": 0}
        draft = self._draft(run, [self._finding()], summary=bad_summary)
        payload = self._review(draft, run)
        self.assertEqual(payload["verdict"], "REGENERATE")
        self.assertTrue(any(i["kind"] == "summary_mismatch" for i in payload["issues"]))
        payload = self._review(draft, run, "--apply")
        self.assertEqual(payload["verdict"], "REPAIRED")
        self.assertEqual(payload["_rc"], 0)
        repaired = json.loads(draft.read_text())
        self.assertEqual(repaired["summary"]["total_findings"], 1)
        self.assertEqual(repaired["summary"]["high"], 1)
        # And the repaired draft now accepts.
        again = self._review(draft, run)
        self.assertEqual(again["verdict"], "ACCEPT", again["issues"])

    def test_smuggled_finding_is_removed_not_reinstated(self):
        legit = self._finding()
        smuggled = self._finding(id="F-002", title="A finding nobody cleared",
                                 check_id="never_seen_check", severity="critical")
        run = self._write_run([legit])
        draft = self._draft(run, [legit, smuggled],
                            summary={"total_findings": 2, "critical": 1, "high": 1,
                                     "medium": 0, "low": 0, "info": 0})
        payload = self._review(draft, run, "--apply")
        kinds = [i["kind"] for i in payload["issues"]]
        self.assertIn("finding_outside_advisory", kinds)
        repaired = json.loads(draft.read_text())
        self.assertEqual([f["check_id"] for f in repaired["findings"]], ["no_jsonld"])
        self.assertEqual(repaired["summary"]["total_findings"], 1)

    def test_critic_dropped_finding_cannot_be_reinstated(self):
        legit = self._finding()
        dropped = {"title": "0 of 1 sampled pages parse", "check_id": "smoke_check",
                   "gate": "sample_sufficiency", "reason": "too small"}
        smuggled = self._finding(id="F-002", title="0 of 1 sampled pages parse",
                                 check_id="smoke_check", severity="medium")
        run = self._write_run([legit], dropped=[dropped])
        draft = self._draft(run, [legit, smuggled],
                            summary={"total_findings": 2, "critical": 0, "high": 1,
                                     "medium": 1, "low": 0, "info": 0})
        payload = self._review(draft, run, "--apply")
        kinds = [i["kind"] for i in payload["issues"]]
        self.assertIn("reinstates_dropped_finding", kinds)
        repaired = json.loads(draft.read_text())
        self.assertEqual(len(repaired["findings"]), 1)

    def test_reviewer_never_invents_a_finding(self):
        run = self._write_run([self._finding()])
        draft = self._draft(run, [self._finding()])
        self._review(draft, run, "--apply")
        repaired = json.loads(draft.read_text())
        self.assertEqual(len(repaired["findings"]), len(json.loads(
            (run / "07-advisory-review.json").read_text())["reviewed_findings"]))

    def test_priority_above_severity_is_capped(self):
        finding = self._finding(severity="high",
                                suggested_action={"summary": "Do the specific thing described.",
                                                  "priority": "critical"})
        run = self._write_run([finding])
        draft = self._draft(run, [finding])
        payload = self._review(draft, run, "--apply")
        self.assertTrue(any(i["kind"] == "priority_above_severity"
                            for i in payload["issues"]))
        repaired = json.loads(draft.read_text())
        self.assertEqual(repaired["findings"][0]["suggested_action"]["priority"], "high")

    def test_level_4_finding_is_removed_not_rewritten(self):
        finding = self._finding(id="F-001", severity="critical", claim_level=4)
        run = self._write_run([finding])
        draft = self._draft(run, [finding],
                            summary={"total_findings": 1, "critical": 1, "high": 0,
                                     "medium": 0, "low": 0, "info": 0})
        payload = self._review(draft, run, "--apply")
        self.assertTrue(any(i["kind"] == "causal_claim_as_finding"
                            for i in payload["issues"]))
        repaired = json.loads(draft.read_text())
        self.assertEqual(repaired["findings"], [])
        self.assertEqual(repaired["summary"]["total_findings"], 0)

    def test_reviewer_writes_an_audit_trail(self):
        run = self._write_run([self._finding()])
        draft = self._draft(run, [self._finding()])
        self._review(draft, run)
        trail = run / "08-review.json"
        self.assertTrue(trail.is_file(), "reviewer should leave 08-review.json")
        self.assertEqual(json.loads(trail.read_text())["verdict"], "ACCEPT")


# ===========================================================================
# Adaptive configuration
# ===========================================================================

class TestConfig(unittest.TestCase):
    def test_thresholds_adapt_to_the_site(self):
        config = AuditConfig.load()
        baseline = config.get("thin_words")
        config.calibrate_to_site([{"word_count": w, "heading_count": 5}
                                  for w in [1200, 900, 1500, 80, 1100, 1300]])
        self.assertGreater(config.get("thin_words"), baseline)
        self.assertEqual(config.thresholds["thin_words"].source, "site-calibrated")

    def test_every_threshold_carries_provenance(self):
        config = AuditConfig.load()
        for name in config.thresholds:
            self.assertTrue(config.why(name))
            self.assertIn(config.thresholds[name].source,
                          {"paper", "site-calibrated", "page-type", "operator"})

    def test_small_sample_does_not_calibrate(self):
        config = AuditConfig.load()
        config.calibrate_to_site([{"word_count": 100}])
        self.assertEqual(config.thresholds["thin_words"].source, "paper")

    def test_hub_pages_get_a_lower_floor(self):
        config = AuditConfig.load()
        config.calibrate_to_site([{"word_count": w} for w in [1200, 900, 1500, 1100, 1300]])
        hub, _ = config.min_words_for("category")
        normal, _ = config.min_words_for("article")
        self.assertLess(hub, normal)

    def test_page_type_exemptions_exist(self):
        self.assertTrue(config_exempt("legal", "exempt_from_absorption"))
        self.assertTrue(config_exempt("category", "is_hub"))

    def test_sample_sufficiency_gate(self):
        config = AuditConfig.load()
        self.assertFalse(config.sample_is_sufficient(1))
        self.assertTrue(config.sample_is_sufficient(5))


def config_exempt(page_type: str, flag: str) -> bool:
    return bool(AuditConfig.load().expectations_for(page_type).get(flag))


# ===========================================================================
# PDF and charts
# ===========================================================================

class TestPdfAndCharts(unittest.TestCase):
    def test_pdf_structure_is_valid(self):
        doc = PdfDocument()
        doc.heading(1, "Title")
        doc.paragraph("Body text.")
        doc.table(["A", "B"], [[f"row {i}", "x" * 60] for i in range(40)])
        data = doc.to_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertEqual(verify_xref(data), [], "xref offsets must point at their objects")

    def test_pdf_survives_unicode(self):
        doc = PdfDocument()
        doc.paragraph("em—dash “curly” arrow → café naïve 中文 \U0001F600 ≥≤±")
        self.assertTrue(doc.to_bytes().startswith(b"%PDF-"))

    def test_pdf_escapes_delimiters(self):
        doc = PdfDocument()
        doc.paragraph(r"parens ( ) and a backslash \ should not break the stream")
        self.assertEqual(verify_xref(doc.to_bytes()), [])

    def test_charts_render_to_both_backends(self):
        every = [
            charts.severity_bar({"critical": 1, "high": 2, "medium": 3, "low": 1, "info": 0}),
            charts.layer_bar({"reachability": 2, "absorption": 3}),
            charts.severity_donut({"high": 2, "medium": 1}),
            charts.evidence_genre_chart({"numeric": True, "definition": False}),
            charts.absorption_gauge(0.42),
            charts.word_count_distribution([50, 400, 1200, 5000]),
        ]
        for chart in every:
            with self.subTest(chart=chart.title):
                self.assertIn("<svg", chart.to_svg())
                doc = PdfDocument()
                chart.flow(doc)
                self.assertEqual(verify_xref(doc.to_bytes()), [])

    def test_charts_degrade_on_empty_data(self):
        for chart in [charts.severity_bar({}), charts.severity_donut({}),
                      charts.word_count_distribution([]), charts.absorption_gauge(None),
                      charts.layer_bar({})]:
            with self.subTest(chart=chart.title):
                self.assertIn("<svg", chart.to_svg())
                doc = PdfDocument()
                chart.flow(doc)
                self.assertTrue(doc.to_bytes().startswith(b"%PDF-"))

    def test_qa_genre_renders_negative(self):
        svg = charts.evidence_genre_chart({"qa_format": True}).to_svg()
        self.assertIn("-5.74", svg)


# ===========================================================================
# End to end against the fixture site
# ===========================================================================

class TestEndToEnd(unittest.TestCase):
    """Runs the real entrypoint against a real HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.site = FixtureSite().__enter__()
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)

        completed = subprocess.run(
            [sys.executable, str(ROOT / "skills/audit-orchestrator/scripts/run_audit.py"),
             cls.site.origin, "--workspace", str(base / "ws"),
             "--max-pages", "12", "--max-seconds", "180",
             "--formats", "json,md,html,pdf", "--out", str(base / "report"), "--quiet"],
            capture_output=True, text=True, timeout=300,
            # The fixture is served on 127.0.0.1, which the SSRF guard is
            # correct to refuse. This opt-in exists for exactly this case and
            # is never set in normal operation; TestNetguard verifies the guard
            # still blocks everything when it is absent.
            env={**os.environ, "GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"},
        )
        cls.completed = completed
        cls.report_path = base / "report.json"
        cls.report = (json.loads(cls.report_path.read_text())
                      if cls.report_path.is_file() else None)
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.site.__exit__(None, None, None)
        cls.tmp.cleanup()

    def test_pipeline_succeeded(self):
        self.assertEqual(self.completed.returncode, 0,
                         f"stdout:\n{self.completed.stdout}\nstderr:\n{self.completed.stderr}")
        self.assertIsNotNone(self.report)

    def test_report_validates_against_the_schema(self):
        self.assertEqual(validate_report(self.report), [])

    def test_required_fields_are_present(self):
        for key in ("site", "audited_at", "summary", "findings"):
            self.assertIn(key, self.report)
        for key in ("total_findings", "critical", "high", "medium"):
            self.assertIn(key, self.report["summary"])
        for finding in self.report["findings"]:
            for key in ("id", "title", "severity", "evidence", "suggested_action"):
                self.assertIn(key, finding)
            self.assertIn("summary", finding["suggested_action"])
            self.assertIn("priority", finding["suggested_action"])

    def test_detects_the_planted_defects(self):
        found = {f.get("check_id") for f in self.report["findings"]}
        # Consequences merged into a root cause still count as detected.
        for finding in self.report["findings"]:
            found.update(finding.get("consequences", []))
        missing = EXPECTED_FINDINGS["must_detect"] - found
        self.assertEqual(
            missing, set(),
            f"failed to detect: {sorted(missing)}\ndetected: {sorted(c for c in found if c)}")

    def test_no_false_positives_on_exempt_pages(self):
        for check_id, url_part in EXPECTED_FINDINGS["must_not_flag_page"]:
            for finding in self.report["findings"]:
                if finding.get("check_id") != check_id:
                    continue
                urls = finding.get("affected_urls", [])
                # A finding may legitimately span several pages; it is a false
                # positive only when the exempt page is the *only* one cited.
                if urls and all(url_part in u for u in urls):
                    self.fail(f"{check_id} falsely flagged {url_part}: {finding['title']}")

    def test_every_finding_carries_verifiable_evidence(self):
        for finding in self.report["findings"]:
            with self.subTest(finding=finding["id"]):
                self.assertGreater(len(finding["evidence"]), 40)
                has_url = "http" in finding["evidence"] or finding.get("affected_urls")
                self.assertTrue(has_url, f"{finding['id']} has no URL to verify against")

    def test_no_finding_exceeds_its_claim_level_ceiling(self):
        ceiling = {1: ["critical", "high", "medium", "low", "info"],
                   2: ["high", "medium", "low", "info"],
                   3: ["medium", "low", "info"]}
        for finding in self.report["findings"]:
            level = finding.get("claim_level", 1)
            self.assertIn(finding["severity"], ceiling[level],
                          f"{finding['id']} claims level {level} at {finding['severity']}")

    def test_advisory_gate_actually_ran(self):
        advisory = self.report.get("advisory")
        self.assertIsNotNone(advisory)
        self.assertGreaterEqual(advisory["findings_in"], advisory["findings_out"])

    def test_proactive_recommendations_are_present(self):
        self.assertTrue(self.report.get("proactive_recommendations"))

    def test_never_recommends_adding_an_faq(self):
        """The paper's one negative genre must never be advised as a fix.

        Checked against the *actionable* fields only, and a mention is allowed
        when it is negated - the report is expected to warn against the tactic,
        which naturally contains the phrase it warns about.
        """
        import re as _re
        mentions_faq = _re.compile(r"\b(?:add|adding|create|creating|introduce)\s+"
                                   r"(?:an?\s+|more\s+)?(?:faq|q&a)", _re.I)
        negated = _re.compile(r"\b(?:do not|don't|never|avoid|resist|rather than|"
                              r"instead of|not a substitute|is not)\b", _re.I)

        actionable: list[str] = []
        for finding in self.report["findings"]:
            action = finding.get("suggested_action", {})
            actionable.append(action.get("summary", ""))
            actionable.extend(action.get("steps", []))
        for rec in self.report.get("proactive_recommendations", []):
            actionable.append(rec.get("action", ""))
            actionable.append(rec.get("title", ""))

        for text in actionable:
            match = mentions_faq.search(text or "")
            if match and not negated.search(text):
                self.fail(f"report advises adding an FAQ: {text!r}")

        # And the standing warning must actually be present.
        blob = json.dumps(self.report).lower()
        self.assertIn("q&a", blob,
                      "report should carry the paper's Q&A warning somewhere")

    def test_all_output_formats_were_written(self):
        for suffix in (".json", ".md", ".html", ".pdf"):
            path = self.base / f"report{suffix}"
            with self.subTest(fmt=suffix):
                self.assertTrue(path.is_file(), f"{suffix} was not written")
                self.assertGreater(path.stat().st_size, 500)

    def test_pdf_output_is_structurally_valid(self):
        data = (self.base / "report.pdf").read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(verify_xref(data), [])

    def test_runs_within_the_time_limit(self):
        self.assertLess(self.report["scope"]["runtime_seconds"], 300,
                        "audit must finish inside the 5-minute limit")

    def test_scope_declares_read_only_behaviour(self):
        scope = self.report["scope"]
        self.assertTrue(scope["robots_respected"])
        self.assertFalse(scope["javascript_executed"])


class TestRefusesUnsafeTargets(unittest.TestCase):
    def test_entrypoint_refuses_private_addresses(self):
        for target in ["http://169.254.169.254/", "http://localhost/", "file:///etc/passwd"]:
            with self.subTest(target=target):
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "skills/url-validation/scripts/validate_url.py"),
                     target],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(completed.returncode, 1)
                self.assertIn("REFUSED", completed.stdout + completed.stderr)


# ===========================================================================
# The advisory critic - each gate must actually bite
# ===========================================================================

class TestAdvisoryGates(unittest.TestCase):
    """Feeds deliberately defective findings through the critic.

    A gate that never rejects anything is decoration. These tests construct the
    exact defect each gate exists to catch and assert it is caught.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / "ws" / "run-1"
        (self.run_dir).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _advise(self, findings, *, pages=None, strict=False):
        """Write a synthetic workspace and run the real advisory script."""
        (self.run_dir / "01-url-validation.json").write_text(json.dumps({
            "cleared": True,
            "target": {"host": "x.com", "registrable_domain": "x.com",
                       "origin": "https://x.com", "scheme": "https",
                       "url": "https://x.com/"},
            "config": {"min_sample_for_population_claim": 3},
            "findings": [],
        }))
        (self.run_dir / "02-crawl-reachability.json").write_text(json.dumps({
            "pages": pages or [
                {"url": f"https://x.com/p{i}", "page_type": "article"} for i in range(6)
            ],
            "config": {"min_sample_for_population_claim": 3},
            "findings": findings,
        }))
        cmd = [sys.executable, str(ROOT / "skills/advisory-review/scripts/advise.py"),
               "--workspace", str(self.run_dir), "--json"]
        if strict:
            cmd.append("--strict")
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    @staticmethod
    def _finding(**overrides):
        base = {
            "id": "F-001", "title": "A finding", "severity": "high",
            "evidence": "8 of 11 sampled pages such as https://x.com/p1 lack the thing.",
            "suggested_action": {"summary": "Do the specific thing described.",
                                 "priority": "high"},
            "layer": "absorption", "claim_level": 1, "confidence": "high",
            "check_id": "generic_check", "sample_size": 11,
            "affected_urls": ["https://x.com/p1"],
        }
        base.update(overrides)
        return base

    def _dropped_gates(self, result):
        return {d["gate"] for d in result["dropped"]}

    def test_baseline_finding_survives(self):
        result = self._advise([self._finding()])
        self.assertEqual(result["output_findings"], 1, result["dropped"])

    def test_gate_rejects_finding_with_no_url(self):
        result = self._advise([self._finding(
            evidence="The site generally lacks structured data across many pages.",
            affected_urls=[])])
        self.assertEqual(result["output_findings"], 0)
        self.assertIn("evidence_sufficiency", self._dropped_gates(result))

    def test_gate_rejects_finding_with_no_observed_value(self):
        result = self._advise([self._finding(
            evidence="Structured data is missing.", affected_urls=[])])
        self.assertEqual(result["output_findings"], 0)

    def test_gate_rejects_vague_suggested_action(self):
        result = self._advise([self._finding(
            suggested_action={"summary": "Fix it", "priority": "high"})])
        self.assertEqual(result["output_findings"], 0)
        self.assertIn("evidence_sufficiency", self._dropped_gates(result))

    def test_gate_rejects_threshold_that_did_not_breach(self):
        result = self._advise([self._finding(
            check_id="slow_response",
            observed="median response 1400 ms across 6 pages",
            expected="under 3000 ms",
            evidence="Median response was 1400 ms across 6 pages at https://x.com/p1.",
            sample_size=6)])
        self.assertEqual(result["output_findings"], 0)
        self.assertTrue(
            {"threshold_coherence", "false_positive_trap"} & self._dropped_gates(result))

    def test_gate_demotes_a_population_claim_from_a_tiny_sample(self):
        result = self._advise(
            [self._finding(
                evidence="1 of 1 sampled page (https://x.com/p1) lacks the thing.",
                observed="1 of 1 pages", sample_size=1)],
            pages=[{"url": "https://x.com/p1", "page_type": "article"}])
        self.assertTrue(result["downgraded"] or result["dropped"])
        if result["output_findings"]:
            self.assertEqual(result["reviewed_findings"][0]["confidence"], "low")

    def test_strict_mode_drops_what_it_would_otherwise_demote(self):
        args = dict(
            findings=[self._finding(
                evidence="1 of 1 sampled page (https://x.com/p1) lacks the thing.",
                observed="1 of 1 pages", sample_size=1)],
            pages=[{"url": "https://x.com/p1", "page_type": "article"}])
        self.assertEqual(self._advise(**args, strict=True)["output_findings"], 0)

    def test_gate_refuses_level_4_causal_claims(self):
        result = self._advise([self._finding(claim_level=4)])
        self.assertEqual(result["output_findings"], 0)
        self.assertIn("claim_level", self._dropped_gates(result))

    def test_gate_relevels_unhedged_causal_language(self):
        result = self._advise([self._finding(
            claim_level=1, severity="critical",
            evidence=("8 of 11 pages such as https://x.com/p1 lack definitions. "
                      "Adding definitions will increase citations and guarantees "
                      "the brand appears in answers."))])
        self.assertEqual(result["output_findings"], 1)
        emitted = result["reviewed_findings"][0]
        self.assertEqual(emitted["claim_level"], 3)
        self.assertEqual(emitted["severity"], "medium")

    def test_gate_caps_severity_by_claim_level(self):
        result = self._advise([self._finding(claim_level=3, severity="critical")])
        self.assertEqual(result["reviewed_findings"][0]["severity"], "medium")

    def test_trap_suppresses_noindex_on_a_search_page(self):
        result = self._advise([self._finding(
            check_id="noindex_on_public_page",
            evidence="2 of 6 pages carry noindex, e.g. https://x.com/search?q=a",
            affected_urls=["https://x.com/search?q=a", "https://x.com/page/2"])])
        self.assertEqual(result["output_findings"], 0)
        self.assertIn("false_positive_trap", self._dropped_gates(result))

    def test_trap_suppresses_thin_content_on_a_legal_page(self):
        result = self._advise(
            [self._finding(
                check_id="thin_content",
                evidence="1 of 6 pages is thin, namely https://x.com/privacy",
                affected_urls=["https://x.com/privacy"])],
            pages=[{"url": "https://x.com/privacy", "page_type": "legal"}] +
                  [{"url": f"https://x.com/p{i}", "page_type": "article"} for i in range(5)])
        self.assertEqual(result["output_findings"], 0)
        self.assertIn("false_positive_trap", self._dropped_gates(result))

    def test_trap_suppresses_thin_content_on_a_hub_page(self):
        result = self._advise(
            [self._finding(
                check_id="no_evidence_genre",
                evidence="1 of 6 pages has no evidence, namely https://x.com/products",
                affected_urls=["https://x.com/products"])],
            pages=[{"url": "https://x.com/products", "page_type": "category"}] +
                  [{"url": f"https://x.com/p{i}", "page_type": "article"} for i in range(5)])
        self.assertEqual(result["output_findings"], 0)

    def test_real_defect_on_a_normal_page_still_survives_the_traps(self):
        """The traps must not be a blanket off-switch."""
        result = self._advise(
            [self._finding(
                check_id="thin_content",
                evidence="4 of 6 pages are thin, e.g. https://x.com/p1 at 40 words",
                affected_urls=["https://x.com/p1", "https://x.com/p2"])],
            pages=[{"url": f"https://x.com/p{i}", "page_type": "article"} for i in range(6)])
        self.assertEqual(result["output_findings"], 1)

    def test_root_cause_absorbs_downstream_symptoms(self):
        urls = ["https://x.com/p1", "https://x.com/p2"]
        result = self._advise([
            self._finding(id="F-001", check_id="empty_shell", layer="reachability",
                          title="Render fault: empty shell", severity="critical",
                          evidence="2 of 6 pages are empty shells, e.g. https://x.com/p1",
                          affected_urls=urls),
            self._finding(id="F-002", check_id="thin_content",
                          title="Pages carry too little text",
                          evidence="2 of 6 pages are thin, e.g. https://x.com/p1",
                          affected_urls=urls),
            self._finding(id="F-003", check_id="no_evidence_genre",
                          title="Pages contain no evidence",
                          evidence="2 of 6 pages lack evidence, e.g. https://x.com/p1",
                          affected_urls=urls),
        ])
        self.assertEqual(result["output_findings"], 1)
        self.assertTrue(result["merged"])
        kept = result["reviewed_findings"][0]
        self.assertEqual(kept["check_id"], "empty_shell")
        self.assertEqual(len(kept.get("consequences", [])), 2)

    def test_unrelated_findings_are_not_merged(self):
        result = self._advise([
            self._finding(id="F-001", check_id="empty_shell", layer="reachability",
                          evidence="1 of 6 pages is an empty shell: https://x.com/p1",
                          affected_urls=["https://x.com/p1"]),
            self._finding(id="F-002", check_id="thin_content",
                          evidence="2 of 6 pages are thin, e.g. https://x.com/p4",
                          affected_urls=["https://x.com/p4", "https://x.com/p5"]),
        ])
        self.assertEqual(result["output_findings"], 2)
        self.assertFalse(result["merged"])

    def test_proactive_layer_is_produced_and_warns_about_faqs(self):
        result = self._advise([self._finding()])
        titles = " ".join(r["title"] for r in result["proactive_recommendations"])
        self.assertIn("FAQ", titles)


# ===========================================================================
# Marketplace manifest
# ===========================================================================

class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "marketplace.json").read_text())

    def test_exactly_one_entrypoint(self):
        entrypoints = [s for s in self.manifest["skills"] if s.get("entrypoint")]
        self.assertEqual(len(entrypoints), 1)
        self.assertEqual(entrypoints[0]["id"], self.manifest["entrypoint"])

    def test_every_declared_skill_exists_and_is_valid(self):
        for skill in self.manifest["skills"]:
            with self.subTest(skill=skill["id"]):
                folder = ROOT / skill["path"]
                self.assertTrue(folder.is_dir(), f"{skill['path']} missing")
                skill_md = folder / "SKILL.md"
                self.assertTrue(skill_md.is_file(), f"{skill['path']}/SKILL.md missing")

                text = skill_md.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("---\n"), "SKILL.md needs YAML frontmatter")
                frontmatter = text.split("---", 2)[1]
                self.assertIn("name:", frontmatter)
                self.assertIn("description:", frontmatter)
                self.assertIn("license:", frontmatter)
                # name must match the folder, per the agentskills.io spec.
                declared = next(line.split(":", 1)[1].strip()
                                for line in frontmatter.splitlines()
                                if line.startswith("name:"))
                self.assertEqual(declared, folder.name)

    def test_no_skill_folder_is_undeclared(self):
        declared = {s["path"].split("/")[-1] for s in self.manifest["skills"]}
        on_disk = {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()}
        self.assertEqual(on_disk - declared, set(), "undeclared skill folder present")

    def test_declares_no_third_party_dependencies(self):
        self.assertEqual(self.manifest["runtime"]["dependencies"], [])

    def test_declares_recommend_only(self):
        safety = self.manifest["safety"]
        self.assertEqual(safety["mode"], "recommend-only")
        self.assertFalse(safety["mutates_target_site"])
        self.assertFalse(safety["destructive_actions"])

    def test_library_imports_only_the_standard_library(self):
        import ast as ast_mod
        stdlib = set(sys.stdlib_module_names)
        for path in (ROOT / "lib" / "geo_audit").glob("*.py"):
            tree = ast_mod.parse(path.read_text(encoding="utf-8"))
            for node in ast_mod.walk(tree):
                names = []
                if isinstance(node, ast_mod.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast_mod.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in ("geo_audit", "__future__"):
                        continue
                    self.assertIn(name, stdlib,
                                  f"{path.name} imports non-stdlib module {name!r}")


class TestAdobeSubmissionContract(unittest.TestCase):
    """Release metadata and examples must match the Adobe Round 3 package."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / "marketplace.json").read_text())
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")

    def test_runtime_floor_matches_python_syntax(self):
        self.assertEqual(self.manifest["runtime"]["minimum_version"], "3.10")
        self.assertNotIn("Python 3.9", self.readme)
        self.assertNotIn("Python 3.9", self.install)

    def test_documented_total_budget_matches_implementation(self):
        import importlib.util
        path = ROOT / "skills" / "audit-orchestrator" / "scripts" / "run_audit.py"
        spec = importlib.util.spec_from_file_location("run_audit_contract_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected = str(int(module.DEFAULT_TOTAL_SECONDS))
        for path in [ROOT / "README.md", ROOT / "INSTALL.md",
                     ROOT / "skills" / "audit-orchestrator" / "SKILL.md"]:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertIn(f"| {expected} |", text)
                self.assertNotIn("| 270 |", text)

    def test_installation_requires_the_complete_marketplace_tree(self):
        self.assertIn("copy the complete marketplace tree", self.install.lower())
        self.assertNotIn("One agent skill dir", self.install)
        self.assertNotIn("Single stage", self.install)

    def test_skill_descriptions_are_concise(self):
        for path in (ROOT / "skills").glob("*/SKILL.md"):
            frontmatter = path.read_text(encoding="utf-8").split("---", 2)[1]
            description = next(
                line.split(":", 1)[1].strip()
                for line in frontmatter.splitlines()
                if line.startswith("description:")
            )
            with self.subTest(skill=path.parent.name):
                self.assertLessEqual(len(description), 350)

    def test_readme_test_inventory_is_current(self):
        self.assertNotIn("98 tests", self.readme)
        self.assertIn("146 tests", self.readme)

    def test_organization_id_uses_the_audited_site(self):
        import importlib.util
        path = (ROOT / "skills" / "structured-data-identity" / "scripts" /
                "check_structured_data.py")
        spec = importlib.util.spec_from_file_location("structured_contract_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        pages = [module.Page(
            url=f"https://huggingface.co/page-{i}", page_type="homepage" if i == 0 else "docs",
            doc=parse_html("<html><body><h1>Page</h1></body></html>",
                           url=f"https://huggingface.co/page-{i}",
                           scope_host="huggingface.co"),
            html="<html><body><h1>Page</h1></body></html>", status=200,
        ) for i in range(6)]
        findings, _, _ = module.check_structured_data(
            pages, AuditConfig(), "huggingface.co")
        finding = next(f for f in findings if f.check_id == "SD005")
        steps = " ".join(finding.suggested_action.steps)
        self.assertIn("https://huggingface.co/#organization", steps)
        self.assertNotIn("https://example.com/#organization", steps)

        for suffix in ("json", "md", "html"):
            sample = ROOT / "examples" / f"sample-report-huggingface.{suffix}"
            self.assertNotIn("https://example.com/#organization",
                             sample.read_text(encoding="utf-8"))


class TestAddressPinning(unittest.TestCase):
    """The address netguard cleared must be the address we dial.

    netguard resolves the host and refuses every non-public answer, but if the
    *name* is then handed to urllib, urllib resolves it again and connects to
    whatever the second lookup returns. Two lookups, one checked and one used,
    is the DNS-rebinding hole. These tests pin that shut.
    """

    def test_hostname_is_resolved_once_and_the_pin_is_dialled(self):
        import socket as socket_mod
        from geo_audit.fetcher import Fetcher

        looked_up = []
        real = socket_mod.getaddrinfo

        def spy(host, port, *args, **kwargs):
            looked_up.append(host)
            if len(looked_up) == 1:          # netguard's checked lookup
                return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "",
                         ("93.184.216.34", port))]
            return real("127.0.0.1", port or 80, *args, **kwargs)

        socket_mod.getaddrinfo = spy
        try:
            Fetcher("rebind.test-domain.net", respect_scope=False).fetch(
                "http://rebind.test-domain.net/latest/meta-data/")
        finally:
            socket_mod.getaddrinfo = real

        self.assertEqual(looked_up[0], "rebind.test-domain.net")
        # Everything after the first lookup must be the validated IP literal,
        # which is a numeric parse rather than a DNS query -- so a second,
        # different answer can never be substituted.
        for entry in looked_up[1:]:
            self.assertEqual(entry, "93.184.216.34")

    def test_pin_preserves_hostname_for_sni_and_certificate_checks(self):
        import ssl
        from geo_audit.fetcher import _PinnedHTTPSConnection

        conn = _PinnedHTTPSConnection("example.com", pinned_ip="93.184.216.34",
                                      timeout=5)
        self.addCleanup(conn.close)
        self.assertEqual(conn.host, "example.com")
        self.assertEqual(conn.pinned_ip, "93.184.216.34")
        self.assertTrue(conn._context.check_hostname)
        self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)


class TestDecompressionLimit(unittest.TestCase):
    """A response is capped on the wire *and* after inflation."""

    def test_gzip_bomb_is_capped_and_reported(self):
        import gzip
        from geo_audit.fetcher import Fetcher, MAX_DECOMPRESSED_BYTES

        payload = gzip.compress(b"A" * (MAX_DECOMPRESSED_BYTES * 10))
        self.assertLess(len(payload), 1_000_000,
                        "the bomb should be small on the wire")

        body, hit_limit = Fetcher._decode(
            payload, {"content-encoding": "gzip", "content-type": "text/html"})

        self.assertLessEqual(len(body), MAX_DECOMPRESSED_BYTES)
        self.assertTrue(hit_limit, "truncation must be reported, not swallowed")

    def test_ordinary_gzipped_page_survives_intact(self):
        import gzip
        from geo_audit.fetcher import Fetcher

        page = "<html><body>" + "<p>hello world</p>" * 500 + "</body></html>"
        body, hit_limit = Fetcher._decode(
            gzip.compress(page.encode()), {"content-encoding": "gzip"})
        self.assertEqual(body, page)
        self.assertFalse(hit_limit)


class TestRobotsIdentity(unittest.TestCase):
    """We obey the rules written for the agent we actually claim to be."""

    def test_token_matches_the_user_agent_actually_sent(self):
        from geo_audit.fetcher import USER_AGENT

        self.assertTrue(
            USER_AGENT.startswith(OWN_USER_AGENT_TOKEN),
            "the robots token must be the product token in the UA header")

    def test_does_not_borrow_a_permission_granted_to_googlebot(self):
        # The site allows Googlebot and nobody else. Reading the file as
        # Googlebot would grant this crawler access it was never given.
        policy = parse_robots("""
User-agent: *
Disallow: /

User-agent: Googlebot
Allow: /
""")
        self.assertTrue(policy.can_fetch("Googlebot", "/private"))
        self.assertFalse(
            policy.can_fetch(OWN_USER_AGENT_TOKEN, "/private"))

    def test_crawl_delay_is_read_for_our_own_agent(self):
        policy = parse_robots("""
User-agent: *
Crawl-delay: 7

User-agent: Googlebot
Crawl-delay: 0
""")
        self.assertEqual(policy.crawl_delay_for(OWN_USER_AGENT_TOKEN), 7.0)


class TestRobotsPathMatchingIsLinear(unittest.TestCase):
    """robots.txt is attacker-controlled; matching it must not backtrack."""

    def test_pathological_rule_completes_promptly(self):
        import time as time_mod
        from geo_audit.robots import _path_matches

        # Against the previous regex implementation this ran for minutes.
        rule = "/" + "*a" * 20 + "b$"
        path = "/" + "a" * 2048
        started = time_mod.monotonic()
        _path_matches(rule, path)
        self.assertLess(time_mod.monotonic() - started, 1.0)

    def test_rfc9309_matching_semantics_are_unchanged(self):
        from geo_audit.robots import _path_matches

        cases = [
            ("/fish", "/fish.html", True),
            ("/fish", "/Fish.html", False),
            ("/fish", "/fish", True),
            ("/fish*.php", "/fish/xyz.php", True),
            ("/fish*.php", "/Fish/xyz.php", False),
            ("/*.php$", "/index.php", True),
            ("/*.php$", "/index.php?v=1", False),
            ("/*.php$", "/window.php.bak", False),
            ("/fish$", "/fish", True),
            ("/fish$", "/fish.html", False),
            ("/*", "/anything", True),
            ("/a*b*c", "/axxbyyczz", True),
            ("/a*b*c", "/axxbyy", False),
            ("", "/anything", False),
        ]
        for rule, path, expected in cases:
            with self.subTest(rule=rule, path=path):
                self.assertEqual(_path_matches(rule, path), expected)


class TestRuntimeBudget(unittest.TestCase):
    """The 5-minute ceiling the docs promise has to be structural, not hoped for.

    Before this, --max-seconds governed only the detection stages; the advisory
    critic, the renders and the whole review loop ran unbudgeted, so the real
    worst case was roughly 16 minutes against a documented 5.
    """

    @staticmethod
    def _orchestrator():
        import importlib.util
        path = ROOT / "skills" / "audit-orchestrator" / "scripts" / "run_audit.py"
        spec = importlib.util.spec_from_file_location("run_audit_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_worst_case_fits_inside_five_minutes(self):
        m = self._orchestrator()
        total = m.DEFAULT_TOTAL_SECONDS
        deadline = m.Deadline(total)

        # Detection and the advisory critic must hand over with the finalise
        # reserve intact; then walk the finalisation steps at full cost.
        elapsed = total - deadline.finalise_reserve
        for floor, cap in ((m.RENDER_FLOOR, m.RENDER_CAP),      # pass 1 render
                           (5.0, m.REVIEW_CAP),                 # pass 1 review
                           (m.RENDER_FLOOR, m.RENDER_CAP)):     # repair re-render
            elapsed += max(floor, min(cap, max(0.0, total - elapsed)))

        self.assertLess(
            elapsed, 300.0,
            f"worst-case runtime {elapsed:.0f}s exceeds the documented 300s limit")

    def test_slice_never_exceeds_what_remains(self):
        m = self._orchestrator()
        deadline = m.Deadline(m.DEFAULT_TOTAL_SECONDS)
        for spent in (0, 60, 150, 239, 260):
            deadline.started = time.monotonic() - spent
            allowed = deadline.slice(m.REVIEW_CAP)
            self.assertLessEqual(
                allowed, max(5.0, deadline.remaining()) + 0.5,
                "slice() handed out time the run does not have")

    def test_reserves_scale_with_the_ceiling(self):
        m = self._orchestrator()
        # A flat reserve starves detection on a small ceiling: at 60s a fixed
        # 50s reserve leaves 10s, and every stage gets skipped.
        small = m.Deadline(60.0)
        self.assertLess(small.finalise_reserve, 60.0 * 0.5)
        self.assertGreater(60.0 - small.finalise_reserve, 30.0,
                           "a 60s ceiling must still leave a usable crawl window")
        big = m.Deadline(m.DEFAULT_TOTAL_SECONDS)
        self.assertEqual(big.finalise_reserve, m.FINALISE_RESERVE)

    def test_detection_budget_is_smaller_than_the_total_ceiling(self):
        m = self._orchestrator()
        self.assertLess(m.DEFAULT_DETECTION_SECONDS, m.DEFAULT_TOTAL_SECONDS,
                        "detection must leave room for compose, render and review")
        self.assertLessEqual(m.DEFAULT_TOTAL_SECONDS, 300.0)


class TestSubprocessTimeoutResilience(unittest.TestCase):
    """A slow subprocess must not abort the run.

    ``run_stage`` and ``review_once`` always caught ``TimeoutExpired``, but the
    preflight and the render call did not. A preflight that overran -- nothing
    worse than a slow DNS lookup or TLS handshake -- let the exception escape
    ``main()`` and end the process with exit 2 and no report at all. It was
    intermittent, so it read as a flaky network rather than a bug: the same URL
    would audit cleanly on the next attempt.

    The documented contract is that a failing stage is recorded and the audit
    continues, so these two paths have to fail gracefully too.
    """

    @staticmethod
    def _orchestrator():
        import importlib.util
        path = ROOT / "skills" / "audit-orchestrator" / "scripts" / "run_audit.py"
        spec = importlib.util.spec_from_file_location("run_audit_timeout_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    class _Shim:
        """Stands in for the ``subprocess`` module inside run_audit.

        Only ``run`` is intercepted; everything else is proxied through so the
        rest of the orchestrator behaves normally.
        """

        def __init__(self, real, timeout_marker):
            self._real = real
            self._marker = timeout_marker
            self.calls = 0

        def run(self, cmd, **kwargs):
            self.calls += 1
            if any(self._marker in str(part) for part in cmd):
                raise self._real.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            return self._real.run(cmd, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    def test_preflight_timeout_fails_closed_without_crashing(self):
        m = self._orchestrator()
        shim = self._Shim(subprocess, "validate_url.py")
        m.subprocess = shim

        with tempfile.TemporaryDirectory() as tmp:
            code = m.main(["example.com", "--out", str(Path(tmp) / "r"),
                           "--formats", "json", "--quiet"])

        self.assertEqual(shim.calls, 1, "expected the preflight to be the first call")
        # 1 == refused. The important part is that it is *not* an escaping
        # TimeoutExpired, and not 2 (which would mean an internal error).
        self.assertEqual(code, 1, "a preflight timeout must refuse, not crash")

    def test_render_timeout_still_leaves_the_json_artefact(self):
        m = self._orchestrator()
        site = FixtureSite().__enter__()
        try:
            shim = self._Shim(subprocess, "render_report.py")
            m.subprocess = shim
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                with mock.patch.dict(os.environ, {
                        "GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
                    code = m.main([
                        site.origin, "--workspace", str(base / "ws"),
                        "--max-pages", "6", "--max-seconds", "90",
                        "--formats", "json,md", "--out", str(base / "report"),
                        "--quiet",
                    ])
                # The JSON is dumped before the render subprocess is spawned, so
                # a render failure must degrade to a clean error, not lose work
                # and not raise.
                self.assertTrue((base / "report.json").is_file(),
                                "the draft JSON must survive a render timeout")
                self.assertIn(code, (1, 2),
                              "a render timeout must not escape as an exception")
        finally:
            site.__exit__(None, None, None)


class TestCrossDomainRescope(unittest.TestCase):
    """A landing URL that moves to another domain is followed, but never widened.

    Pointing the audit at ``cern.ch`` used to refuse outright: the site 302s to
    ``home.cern``, every redirect hop was re-validated against the *requested*
    registrable domain, and so the first hop was rejected as ``out_of_scope``.
    No report was produced for a site any search engine reaches happily -- and
    the entity engines retrieve and cite is the destination, not the URL the
    operator typed.

    The fix drops the scope guard for redirect hops on the landing request only.
    Both halves are pinned here: the hop is followed, and every address guard
    that protects the auditor still fires on it. The hop runs between two
    loopback names (``127.0.0.1`` and ``localhost``) whose registrable domains
    differ, so it is genuinely cross-domain while needing no external DNS.
    """

    class _Server:
        """Serves a fixed body, or one fixed redirect, on an ephemeral port.

        Bound to every interface so the same helper can stand in for the origin
        (reached at ``127.0.0.1``) and the redirect destination (reached at
        ``127.0.0.2``).
        """

        def __init__(self, location: str | None = None, status: int = 302) -> None:
            self.location = location
            self.status = status
            self.hits: list[str] = []

        def __enter__(self) -> "TestCrossDomainRescope._Server":
            import threading
            from http.server import BaseHTTPRequestHandler, HTTPServer

            outer = self

            class Handler(BaseHTTPRequestHandler):
                def _respond(self) -> None:
                    outer.hits.append(self.path)
                    if outer.location:
                        self.send_response(outer.status)
                        self.send_header("Location", outer.location)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    payload = b"<html><body><h1>Destination</h1></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)

                do_GET = _respond
                do_HEAD = _respond

                def log_message(self, *args) -> None:  # noqa: D102
                    pass

            self.httpd = HTTPServer(("0.0.0.0", 0), Handler)
            self.port = self.httpd.server_address[1]
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *exc) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()

        @property
        def url(self) -> str:
            return f"http://127.0.0.1:{self.port}/"

        @property
        def cross_domain_url(self) -> str:
            """The same server reached under a *different* registrable domain.

            ``a.localhost`` and ``127.0.0.1`` are distinct eTLD+1 values that
            both resolve to the loopback interface, so a redirect between them is
            a genuine cross-domain hop with no external DNS involved. A bare
            ``localhost`` will not do: it has a single label and netguard refuses
            it as not fully-qualified.
            """
            return f"http://a.localhost:{self.port}/"

    @staticmethod
    def _permissive():
        """Loopback access is required to stand up a redirect locally at all."""
        return mock.patch.dict(os.environ, {"GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"})

    def test_cross_domain_hop_is_refused_without_opt_in(self):
        from geo_audit.fetcher import Fetcher

        with self._permissive():
            with self._Server() as destination:
                with self._Server(location=destination.cross_domain_url) as origin:
                    result = Fetcher("127.0.0.1", respect_scope=True).fetch(
                        origin.url, allow_rescope=False
                    )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "out_of_scope",
                         "a cross-domain hop must still be refused by default")
        self.assertEqual(result.rehomed_from, "")

    def test_opted_in_hop_is_followed_and_recorded(self):
        from geo_audit.fetcher import Fetcher

        with self._permissive():
            with self._Server() as destination:
                with self._Server(location=destination.cross_domain_url) as origin:
                    result = Fetcher("127.0.0.1", respect_scope=True).fetch(
                        origin.url, allow_rescope=True
                    )

        self.assertTrue(result.ok, result.error)
        self.assertTrue(destination.hits, "the destination must have been fetched")
        self.assertTrue(result.final_url.startswith(f"http://a.localhost:{destination.port}"),
                        f"audited URL should be the destination, got {result.final_url}")
        self.assertEqual(result.rehomed_from, "127.0.0.1")
        self.assertTrue(result.rescoped)
        # The rehome has to survive serialisation: downstream stages read the
        # preflight artefact, not the FetchResult.
        self.assertTrue(result.to_dict()["rescoped"])
        self.assertEqual(result.to_dict()["rehomed_from"], "127.0.0.1")

    def test_rescope_flag_does_not_widen_the_requested_url(self):
        """Only redirect hops are relaxed. The URL the user typed is not.

        A public domain is used here deliberately: the scope guard is evaluated
        before any DNS lookup, so this asserts the policy without touching the
        network.
        """
        from geo_audit.fetcher import Fetcher

        result = Fetcher("127.0.0.1", respect_scope=True).fetch(
            "http://example.org/", allow_rescope=True
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.error_code, "out_of_scope",
            "allow_rescope must relax redirect hops only, never the requested URL",
        )
        self.assertEqual(result.rehomed_from, "")

    def test_metadata_target_stays_blocked_even_when_rescoping(self):
        """The scope guard is dropped; the metadata guard is not.

        Run with private space permitted, so the *only* thing that can refuse
        this hop is the unconditional metadata block. If `allow_rescope` ever
        widened the address checks, this is the test that would catch it.
        """
        from geo_audit.fetcher import Fetcher

        with self._permissive():
            with self._Server(
                location="http://169.254.169.254/latest/meta-data/"
            ) as origin:
                result = Fetcher("127.0.0.1", respect_scope=True).fetch(
                    origin.url, allow_rescope=True
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "blocked_host")
        self.assertIn("metadata", (result.error or "").lower())
        self.assertEqual(result.rehomed_from, "",
                         "a refused hop must not record a rehome")

    def test_address_guard_is_independent_of_the_scope_guard(self):
        """Scope and address classification are two separate gates.

        `allow_rescope` turns off exactly one of them, and the callees are not
        interchangeable: an out-of-scope public address is refused for scope,
        while a private address is refused for its address even with no scope
        set at all.
        """
        with self.assertRaises(UrlRejected) as caught:
            validate_url("http://10.11.12.13/", scope_domain=None,
                         resolve_dns=False, allow_private=False)
        self.assertEqual(caught.exception.code, "private_ip")

        with self.assertRaises(UrlRejected) as caught:
            validate_url("http://93.184.216.34/", scope_domain="127.0.0.1",
                         resolve_dns=False, allow_private=False)
        self.assertEqual(caught.exception.code, "out_of_scope")


class TestCrossDomainRedirectPermanence(unittest.TestCase):
    """A cross-domain landing redirect is graded by the status of the hop.

    The first version graded every cross-domain redirect ``low`` and told the
    operator to "keep the redirect permanent". That reads as reassurance, and
    it is wrong for a temporary redirect: ``cern.ch`` answers 302 while moving
    its canonical host to ``home.cern``, which tells every engine to keep
    treating the old host as canonical while serving the new one. A 301 makes
    the same move a non-defect, so the two must not share a grade or a fix.
    """

    @staticmethod
    def _preflight(url: str):
        import importlib.util
        path = ROOT / "skills" / "url-validation" / "scripts" / "validate_url.py"
        spec = importlib.util.spec_from_file_location("validate_url_perm", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = [url, "--json", "--max-seconds", "30"]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
            with contextlib.redirect_stdout(buf):
                code = module.main(argv)
        return code, json.loads(buf.getvalue())

    @staticmethod
    def _cross_domain_finding(payload):
        hits = [f for f in payload.get("findings", [])
                if f.get("check_id") == "cross_domain_redirect"]
        return hits[0] if hits else None

    def test_temporary_hop_is_a_medium_with_a_change_the_status_fix(self):
        server = TestCrossDomainRescope._Server
        with server() as destination:
            with server(location=destination.cross_domain_url, status=302) as origin:
                code, payload = self._preflight(origin.url)

        self.assertEqual(code, 0, payload)
        finding = self._cross_domain_finding(payload)
        self.assertIsNotNone(finding, "the cross-domain hop must be reported")
        self.assertEqual(finding["severity"], "medium",
                         "a temporary cross-domain redirect is a real defect")
        self.assertEqual(finding["suggested_action"]["priority"], "medium")
        self.assertIn("302", finding["evidence"],
                      "the evidence must name the status code it observed")
        self.assertIn("302", finding["observed"])
        self.assertIn("301", finding["suggested_action"]["summary"],
                      "the fix must be to make the redirect permanent")
        self.assertIn("not permanent", finding["evidence"])

    def test_permanent_hop_is_a_low_that_keeps_the_redirect(self):
        server = TestCrossDomainRescope._Server
        with server() as destination:
            with server(location=destination.cross_domain_url, status=301) as origin:
                code, payload = self._preflight(origin.url)

        self.assertEqual(code, 0, payload)
        finding = self._cross_domain_finding(payload)
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "low",
                         "a permanent cross-domain move is not the same defect")
        self.assertEqual(finding["suggested_action"]["priority"], "low")
        self.assertIn("301", finding["observed"])
        self.assertIn("Keep the redirect permanent",
                      " ".join(finding["suggested_action"]["steps"]))
        self.assertNotIn("not permanent", finding["evidence"])
        self.assertIn("301", finding["evidence"])


class TestHttpOnlyFallback(unittest.TestCase):
    """A site with no HTTPS must be audited, not refused.

    The HTTPS landing request against a plaintext-only host fails at the
    transport layer (the server answers a TLS ClientHello with plaintext). The
    preflight read that as "unreachable" and refused, so the single most
    consequential finding such a host can have -- that it serves no HTTPS at
    all -- was exactly the case that produced no report. The fallback asks the
    HTTP variant before giving up, and grades a genuinely HTTPS-less host
    ``critical`` rather than ``high``.
    """

    @staticmethod
    def _preflight():
        import importlib.util
        path = ROOT / "skills" / "url-validation" / "scripts" / "validate_url.py"
        spec = importlib.util.spec_from_file_location("validate_url_http_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_https_failure_falls_back_to_http_and_grades_it_critical(self):
        import io
        from contextlib import redirect_stdout

        m = self._preflight()
        site = FixtureSite().__enter__()
        try:
            with mock.patch.dict(os.environ, {
                    "GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
                with tempfile.TemporaryDirectory() as tmp:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        code = m.main([
                            f"https://127.0.0.1:{site.port}/",
                            "--workspace", tmp, "--json",
                        ])
            payload = json.loads(buf.getvalue())
        finally:
            site.__exit__(None, None, None)

        self.assertEqual(code, 0, "a plaintext-only site must be audited, not refused")
        self.assertTrue(payload["cleared"])
        self.assertTrue(
            payload["target"]["url"].startswith("http://"),
            "the audited target must be the plaintext URL that actually answered",
        )
        self.assertFalse(
            payload["https"]["https_available"],
            "the run must record that the HTTPS variant did not answer",
        )

        https_findings = [f for f in payload["findings"] if f.get("check_id") == "no_https"]
        self.assertEqual(len(https_findings), 1, "exactly one HTTPS finding is expected")
        finding = https_findings[0]
        self.assertEqual(finding["severity"], "critical")
        self.assertIn("no HTTPS at all", finding["title"])
        self.assertEqual(finding["suggested_action"]["priority"], "critical")

    def test_plaintext_http_with_live_https_is_still_only_high(self):
        """The critical grade is reserved for hosts with no HTTPS at all."""
        import types

        m = self._preflight()
        target = types.SimpleNamespace(
            scheme="http", host="example.com", path="/",
            url="http://example.com/", registrable_domain="example.com",
        )
        findings: list = []
        evidence = m.check_https(None, target, findings, https_available=True)

        self.assertTrue(evidence["https_available"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertEqual(findings[0].title, "Site is served over plaintext HTTP")


class TestRendererOutputIntegrity(unittest.TestCase):
    """The renderers must emit a whole document, not a degenerate slice.

    Both renderers accumulate their output in a list that they join at the end.
    That makes a local variable shadowing the accumulator silently destructive
    rather than fatal: the join still returns, the file is still written, the
    exit code is still 0 — and the published artefact is a few hundred bytes of
    nonsense. It happened once while adding the survey-genre note (a local named
    `parts` overwrote the HTML accumulator) and produced a 163-byte "report"
    from a 63 KB document with no error anywhere. These assertions pin the
    shape of the output rather than its contents, so any repeat is loud.
    """

    @staticmethod
    def _report(**scores):
        return {
            "site": "x.com", "audited_at": "2026-09-11T00:00:00Z",
            "summary": {"total_findings": 1, "critical": 0, "high": 1,
                        "medium": 0, "low": 0, "info": 0},
            "findings": [{
                "id": "F-001", "title": "HTTPS is available but not enforced",
                "severity": "high", "layer": "preflight", "claim_level": 2,
                "confidence": "high", "evidence": "http://x.com/ answered 200.",
                "affected_urls": ["http://x.com/"],
                "suggested_action": {"summary": "Redirect HTTP to HTTPS.",
                                     "priority": "high", "effort": "low",
                                     "steps": ["Add a 301."],
                                     "mechanism": "Half the fleet is plaintext.",
                                     "verification": "http://x.com/ 301s."},
            }],
            "scope": {"pages_analysed": 1, "pages_discovered": 1,
                      "runtime_seconds": 1.0},
            "scores": {"survey_genres": scores} if scores else {},
            "advisory": {"findings_in": 1, "findings_out": 1, "dropped": [],
                         "downgraded": [], "merged": []},
            "proactive_recommendations": [],
            "limitations": ["Single page sampled."],
            "generator": {"marketplace": "brand-ai-readiness-audit",
                          "version": "1.2.0", "grounding": "test"},
        }

    @staticmethod
    def _render_html(report):
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import render_report
        return render_report.render_html(report, render_report.build_charts(report))

    def test_html_is_a_complete_document(self):
        # Both variants are checked because the defect this guards against lived
        # inside the survey-genre branch: a report without genres never entered
        # it, so completeness alone was not enough to catch it.
        for label, report in (
            ("without survey genres", self._report()),
            ("with survey genres", self._report(price=0, dated=2)),
        ):
            with self.subTest(render="html", case=label):
                html = self._render_html(report)
                self.assertTrue(html.startswith("<!doctype html>"), "missing doctype")
                self.assertTrue(html.rstrip().endswith("</html>"), "document is truncated")
                self.assertIn("Overview", html)
                self.assertIn("F-001", html, "findings section must be present")
                self.assertGreater(
                    len(html), 4000,
                    f"a report with one finding rendered {len(html)} bytes; the "
                    "output accumulator was almost certainly clobbered",
                )

    def test_markdown_survives_the_same_report(self):
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import render_report
        for report in (self._report(), self._report(price=0, dated=2)):
            md = render_report.render_markdown(report)
            self.assertIn("F-001", md)
            self.assertIn("HTTPS is available but not enforced", md)
            self.assertGreater(len(md), 400)

    def test_survey_genres_render_only_when_present(self):
        with_genres = self._report(price=0, dated=2)
        html = self._render_html(with_genres)
        self.assertIn("Extractable facts", html)
        self.assertIn("visible dates", html)
        # A site that carries neither genre still gets the note, saying so.
        self.assertIn("absent", self._render_html(self._report(price=0, dated=0)))

        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import render_report
        self.assertIn("Extractable facts", render_report.render_markdown(with_genres))
        without = self._report()
        self.assertNotIn("Extractable facts", render_report.render_markdown(without))
        self.assertNotIn("Extractable facts", self._render_html(without))

    def test_survey_genres_do_not_join_the_paper_uplift_chart(self):
        """The chart plots measured effect sizes; the survey genres have none.

        Mixing them would imply the same evidence backs both, which is the one
        thing the two-source grounding exists to prevent.
        """
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import render_report
        charts = render_report.build_charts(self._report(price=1, dated=1))
        self.assertIsNotNone(charts.get("genres"))
        svg = charts["genres"].to_svg()
        self.assertNotIn("visible dates", svg)
        self.assertNotIn("prices or rates", svg)


class TestAddressWalkBudget(unittest.TestCase):
    """A dead address must not consume the whole request timeout.

    ``getaddrinfo`` returns a host's addresses in preference order, and the
    fetcher dials them in turn. Each attempt used to receive the full request
    timeout, so a dual-stack host whose IPv6 route blackholes (neverssl.com is
    one: its AAAA record accepts a SYN and never answers) spent the entire
    timeout on the unreachable family and then the entire timeout again on the
    family that works -- on every request, for every page. The audit was
    doubling its own worst case and blaming the site.

    macOS routes ``127.0.0.1`` but blackholes ``127.0.0.2``, so a two-address
    walk from the dead one to the live one reproduces the shape exactly, with
    no external network.
    """

    def test_dead_first_address_costs_a_bounded_slice_not_the_whole_timeout(self):
        from geo_audit import fetcher as fetcher_mod
        from geo_audit.fetcher import Fetcher

        with TestCrossDomainRescope._Server() as server:
            with mock.patch.dict(os.environ,
                                 {"GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
                with mock.patch.object(
                    fetcher_mod, "_connect_addresses",
                    staticmethod(lambda validated: ["127.0.0.2", "127.0.0.1"]),
                ):
                    with mock.patch.object(fetcher_mod,
                                           "ADDRESS_CONNECT_SECONDS", 0.4):
                        started = time.monotonic()
                        result = Fetcher(
                            "127.0.0.1", respect_scope=False, timeout=5.0
                        ).fetch(f"http://127.0.0.1:{server.port}/")
                        elapsed = time.monotonic() - started

        self.assertTrue(result.ok, result.error)
        self.assertTrue(server.hits, "the live address must still be reached")
        self.assertLess(
            elapsed, 2.5,
            f"the walk took {elapsed:.1f}s: the dead address was given the whole "
            "5s timeout instead of a bounded slice",
        )

    def test_single_address_host_keeps_the_full_timeout(self):
        """The cap must not shorten the budget for a normal, one-address host.

        The final address in the walk always receives everything remaining, so
        a host that publishes one address behaves exactly as it did before the
        cap existed.
        """
        from geo_audit import fetcher as fetcher_mod
        from geo_audit.fetcher import Fetcher

        seen: list[float] = []
        real_open = fetcher_mod.Fetcher._open_pinned

        def spy(self, req, timeout, addresses):
            seen.append(len(addresses))
            return real_open(self, req, timeout, addresses)

        with TestCrossDomainRescope._Server() as server:
            with mock.patch.dict(os.environ,
                                 {"GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
                with mock.patch.object(
                    fetcher_mod, "_connect_addresses",
                    staticmethod(lambda validated: ["127.0.0.1"]),
                ):
                    with mock.patch.object(fetcher_mod.Fetcher,
                                           "_open_pinned", spy):
                        result = Fetcher(
                            "127.0.0.1", respect_scope=False, timeout=5.0
                        ).fetch(f"http://127.0.0.1:{server.port}/")

        self.assertTrue(result.ok, result.error)
        self.assertEqual(seen, [1], "expected exactly one address in the walk")


class TestDiscoveryDeadline(unittest.TestCase):
    """Sitemap discovery must stop at its deadline and keep what it found.

    Discovery's cost follows the *site's* size, not ``max_pages``: a sitemap
    index whose forty children are each megabytes wide is walked one fetch at a
    time. On developers.google.com that walk spent 83s totalling five children
    that each timed out, and the crawl stage was then killed by its parent for
    exceeding its slice -- publishing no pages and no findings at all, for a
    public site that answers every request. The deadline turns that into a
    partial pool and a partial report.
    """

    class _SitemapServer:
        """An index of child sitemaps; each child is served with a delay."""

        def __init__(self, children: int = 8, child_delay: float = 0.0) -> None:
            self.children = children
            self.child_delay = child_delay
            self.fetched: list[str] = []

        def __enter__(self):
            import threading
            from http.server import BaseHTTPRequestHandler, HTTPServer

            outer = self

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    outer.fetched.append(self.path)
                    if self.path.endswith("/sitemap.xml"):
                        locs = "".join(
                            f"<sitemap><loc>http://127.0.0.1:{outer.port}"
                            f"/child{i}.xml</loc></sitemap>"
                            for i in range(outer.children)
                        )
                        body = (
                            '<?xml version="1.0"?><sitemapindex '
                            'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                            + locs + "</sitemapindex>"
                        ).encode()
                    else:
                        if outer.child_delay:
                            time.sleep(outer.child_delay)
                        child = self.path.rsplit("/", 1)[-1].split(".")[0]
                        body = (
                            '<?xml version="1.0"?><urlset '
                            'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                            f"<url><loc>http://127.0.0.1:{outer.port}/page-{child}</loc></url>"
                            "</urlset>"
                        ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                do_HEAD = do_GET

                def log_message(self, *args) -> None:  # noqa: D102
                    pass

            self.httpd = HTTPServer(("0.0.0.0", 0), Handler)
            self.port = self.httpd.server_address[1]
            self.thread = threading.Thread(target=self.httpd.serve_forever,
                                           daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *exc) -> None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def _discover(self, deadline, **kwargs):
        from geo_audit import sitemapper
        from geo_audit.fetcher import Budget, Fetcher
        from geo_audit.robots import RobotsPolicy

        with mock.patch.dict(os.environ, {"GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES": "1"}):
            with self._SitemapServer(**kwargs) as server:
                origin = f"http://127.0.0.1:{server.port}/"
                policy = RobotsPolicy(exists=True)
                fetcher = Fetcher("127.0.0.1", Budget(max_seconds=60),
                                  timeout=5.0)
                discovery = sitemapper.discover(
                    fetcher, origin, "127.0.0.1", policy,
                    deadline=deadline(),
                )
                return discovery, list(server.fetched)

    def test_expired_deadline_keeps_candidates_and_says_it_stopped(self):
        # A deadline already in the past: the seed is added, the first sitemap
        # (declared in robots.txt) is skipped, and discovery returns at once.
        discovery, fetched = self._discover(
            lambda: time.monotonic() - 1.0, children=8,
        )
        self.assertEqual(
            [c.url for c in discovery.candidates],
            [f"http://127.0.0.1:{discovery.candidates[0].url.rsplit(':', 1)[-1]}"]
            if discovery.candidates else [],
            "only the seed survives an expired deadline",
        )
        self.assertEqual(
            len(discovery.candidates), 1,
            f"an expired deadline must yield the seed alone; got "
            f"{[c.url for c in discovery.candidates]}",
        )
        self.assertEqual(
            [f for f in fetched if "sitemap" in f], [],
            f"no sitemap may be walked once the deadline has passed; got {fetched}",
        )
        self.assertTrue(
            any("stopped at its time limit" in n for n in discovery.notes),
            f"a truncated walk must say so; notes were {discovery.notes}",
        )

    def test_deadline_does_not_stop_a_site_that_finishes_in_time(self):
        discovery, fetched = self._discover(
            lambda: time.monotonic() + 60.0, children=3,
        )
        self.assertTrue(discovery.sitemap_found)
        urls = [c.url for c in discovery.candidates]
        for i in range(3):
            self.assertIn(f"http://127.0.0.1:{discovery.candidates[0].url.rsplit(':', 1)[-1]}"
                          f"/page-child{i}", urls,
                          f"child{i}'s page must be in the pool")
        self.assertFalse(
            any("stopped at its time limit" in n for n in discovery.notes),
            "a walk that completed must not claim it was cut short",
        )

    def test_deadline_stops_a_slow_walk_partway_and_keeps_what_it_got(self):
        # Each child costs 0.35s; an 0.7s deadline cannot reach all eight, and
        # the walk must still return the candidates it did collect.
        discovery, _ = self._discover(
            lambda: time.monotonic() + 0.7, children=8, child_delay=0.35,
        )
        self.assertTrue(
            any("stopped at its time limit" in n for n in discovery.notes),
            "a slow walk must be cut short and must say so",
        )
        self.assertTrue(discovery.sitemap_found,
                        "the index it already fetched must still count")
        pages = [c for c in discovery.candidates if "/page-" in c.url]
        self.assertTrue(pages, "candidates found before the cut-off must survive")
        self.assertLess(
            len(pages), 8,
            f"the deadline did not stop the walk early; got {len(pages)} pages",
        )
class TestPolitenessBudget(unittest.TestCase):
    """Crawl-delay politeness must be bounded by the time budget.

    news.ycombinator.com publishes ``Crawl-delay: 30``. Honouring it for every
    page in a 20-page sample needs ten minutes, so the crawl stage was killed by
    its parent and the report contained zero pages -- while still having fetched
    some of them. Sampling fewer pages is both more respectful and more useful
    than being killed, and the report says which of the two happened.
    """

    @staticmethod
    def _fn():
        import importlib.util
        path = (ROOT / "skills" / "crawl-reachability" / "scripts"
                / "check_reachability.py")
        spec = importlib.util.spec_from_file_location("check_reachability_pol", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.affordable_sample_size

    def test_no_crawl_delay_leaves_max_pages_untouched(self):
        f = self._fn()
        self.assertEqual(f(20, 0.0, 150.0, 5.0), 20)
        self.assertEqual(f(20, -1.0, 150.0, 5.0), 20)

    def test_large_crawl_delay_shrinks_the_sample_to_fit(self):
        f = self._fn()
        # 30s delay, 60s left -> two pages fit.
        self.assertEqual(f(20, 30.0, 150.0, 90.0), 2)
        # A delay that exceeds the whole budget still yields exactly one page,
        # fetched deliberately at the requested rate.
        self.assertEqual(f(20, 300.0, 150.0, 0.0), 1)

    def test_shrunk_sample_never_exceeds_what_was_asked_for(self):
        f = self._fn()
        self.assertEqual(f(5, 1.0, 150.0, 0.0), 5,
                         "a fast site must not be given extra pages")

    def test_small_delay_does_not_shrink_a_large_budget(self):
        f = self._fn()
        self.assertEqual(f(20, 0.4, 150.0, 0.0), 20)

    def test_reducing_the_sample_is_reported_as_a_limitation(self):
        """The report must say the sample was narrowed, and why.

        A reader who sees "1 page analysed" needs to know it was the site's
        crawl delay and not a failure or a clean result.
        """
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import run_audit

        note = ("The site requests a 30s crawl delay, which the audit honours; "
                "the sample was reduced to 1 page(s) so the crawl finishes "
                "inside the time budget.")
        reachability = {
            "pages_analysed": 1,
            "discovery": {"candidates": 166, "notes": [note]},
        }
        limitations = run_audit.discovery_limitations(reachability)
        self.assertEqual(limitations, [note],
                         "the crawl-delay note must reach the limitations list")

    def test_truncated_discovery_is_restated_not_copied(self):
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import run_audit

        reachability = {
            "pages_analysed": 16,
            "discovery": {"candidates": 16, "notes": [
                "sitemap discovery stopped at its time limit; the candidate pool "
                "is a partial sample of a large sitemap, not the whole site",
            ]},
        }
        limitations = run_audit.discovery_limitations(reachability)
        self.assertEqual(len(limitations), 1)
        self.assertIn("partial sample", limitations[0])
        self.assertIn("never considered", limitations[0])

    def test_ordinary_discovery_notes_are_not_promoted_to_limitations(self):
        """A missing sitemap is already a finding; it is not also a limitation."""
        sys.path.insert(0, str(ROOT / "skills" / "audit-orchestrator" / "scripts"))
        import run_audit

        reachability = {
            "pages_analysed": 3,
            "discovery": {"candidates": 3, "notes": [
                "no sitemap.xml discovered via robots.txt or common paths",
            ]},
        }
        self.assertEqual(run_audit.discovery_limitations(reachability), [])
        self.assertEqual(run_audit.discovery_limitations({}), [])
        self.assertEqual(run_audit.discovery_limitations({"discovery": {}}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
