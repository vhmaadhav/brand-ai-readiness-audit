"""Adaptive configuration.

A fixed threshold is a hidden assumption about the site being audited. "Fewer
than 300 words is thin" is right for a documentation page and wrong for a
pricing table; "no JSON-LD is critical" is right for a storefront and noise for
a personal blog. Since the marketplace is graded on *unseen* sites, baking in
one site's shape would be the single fastest way to fail.

So thresholds here are **derived at runtime** from three inputs:

1. **Paper anchors** — the empirical quartiles from arXiv:2604.25707, which are
   corpus-wide and do not depend on the site.
2. **Site self-calibration** — the site's own observed distribution. A page is
   thin *relative to the rest of this site* as well as relative to the corpus.
3. **Page-type expectations** — a legal page is not expected to carry
   comparison content; a product page is expected to carry an offer.

Every derived threshold records *why* it has the value it does, and that
provenance travels into the finding's evidence string. A reviewer can always
ask "where did 340 come from" and get an answer.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .textstats import STRUCTURE_QUARTILES, THIN_CONTENT_WORDS

# What each page type is legitimately expected to carry. Absence of an expected
# capability is a finding; absence of an unexpected one is not. This is the
# main false-positive suppressor in the whole marketplace.
PAGE_TYPE_EXPECTATIONS: dict[str, dict] = {
    "homepage": {
        "schema_types": ["Organization", "WebSite", "LocalBusiness", "Corporation"],
        "min_words": 150, "expect_evidence_genres": 1, "expect_date": False,
        "expect_author": False, "engagement_critical": True,
    },
    "product": {
        "schema_types": ["Product", "Offer", "AggregateOffer", "ItemList"],
        "min_words": 120, "expect_evidence_genres": 2, "expect_date": False,
        "expect_author": False, "engagement_critical": True,
    },
    "pricing": {
        "schema_types": ["Product", "Offer", "PriceSpecification", "Service"],
        "min_words": 100, "expect_evidence_genres": 2, "expect_date": False,
        "expect_author": False, "engagement_critical": True,
    },
    "article": {
        "schema_types": ["Article", "BlogPosting", "NewsArticle", "TechArticle"],
        "min_words": 300, "expect_evidence_genres": 2, "expect_date": True,
        "expect_author": True, "engagement_critical": False,
    },
    "docs": {
        "schema_types": ["TechArticle", "HowTo", "Article", "APIReference"],
        "min_words": 200, "expect_evidence_genres": 2, "expect_date": True,
        "expect_author": False, "engagement_critical": False,
    },
    "about": {
        "schema_types": ["Organization", "AboutPage", "Corporation"],
        "min_words": 120, "expect_evidence_genres": 1, "expect_date": False,
        "expect_author": False, "engagement_critical": False,
    },
    "service": {
        "schema_types": ["Service", "Product", "Offer"],
        "min_words": 150, "expect_evidence_genres": 2, "expect_date": False,
        "expect_author": False, "engagement_critical": True,
    },
    "category": {
        # A hub page's job is to route, not to explain. Judging it on prose
        # depth produces a false positive on every well-built storefront.
        "schema_types": ["CollectionPage", "ItemList", "BreadcrumbList"],
        "min_words": 60, "expect_evidence_genres": 0, "expect_date": False,
        "expect_author": False, "engagement_critical": False, "is_hub": True,
    },
    "legal": {
        # Legal text should not be optimised for citation. Exempt by design.
        "schema_types": [], "min_words": 100, "expect_evidence_genres": 0,
        "expect_date": True, "expect_author": False,
        "engagement_critical": False, "exempt_from_absorption": True,
    },
    "other": {
        "schema_types": ["WebPage"], "min_words": 120, "expect_evidence_genres": 1,
        "expect_date": False, "expect_author": False, "engagement_critical": False,
    },
}

DEFAULT_EXPECTATION = PAGE_TYPE_EXPECTATIONS["other"]


@dataclass
class Threshold:
    """A number the audit will act on, plus the reason it holds that value."""

    name: str
    value: float
    basis: str
    source: str  # "paper" | "site-calibrated" | "page-type" | "operator"

    def explain(self) -> str:
        return f"{self.name}={self.value:g} ({self.source}: {self.basis})"


@dataclass
class AuditConfig:
    """Runtime configuration for one audit."""

    max_pages: int = 20
    max_seconds: float = 240.0
    per_host_delay: float = 0.4
    timeout: float = 15.0
    min_sample_for_population_claim: int = 3
    strictness: str = "balanced"  # lenient | balanced | strict

    thresholds: dict[str, Threshold] = field(default_factory=dict)
    calibration: dict = field(default_factory=dict)

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, **overrides) -> "AuditConfig":
        """Build a config, layering an optional JSON file then keyword overrides."""
        data: dict = {}
        candidate = Path(path) if path else None
        if candidate and candidate.is_file():
            data = json.loads(candidate.read_text(encoding="utf-8"))
        data.update({k: v for k, v in overrides.items() if v is not None})

        config = cls(**{k: v for k, v in data.items() if k in cls.__annotations__ and k not in ("thresholds", "calibration")})
        config.seed_paper_thresholds()
        for key, value in (data.get("thresholds") or {}).items():
            config.thresholds[key] = Threshold(key, float(value), "set by operator", "operator")
        return config

    def seed_paper_thresholds(self) -> None:
        """Corpus-wide anchors that hold before we have seen the site."""
        self.set(
            "thin_words", THIN_CONTENT_WORDS,
            "bottom-quartile word count of pages by influence score (169.82)", "paper",
        )
        self.set(
            "min_headings", 1,
            "bottom-quartile heading total is 0.85; below one heading is bottom-quartile", "paper",
        )
        self.set(
            "low_paragraphs", STRUCTURE_QUARTILES["paragraph_count"]["bottom"],
            "bottom-quartile paragraph count (8.34)", "paper",
        )
        self.set(
            "absorption_floor", 0.25,
            "readiness below 0.25 places a page with the least-absorbed quartile", "paper",
        )
        self.set(
            "render_gap_words", 120,
            "pages under ~120 visible words in raw HTML cannot supply an answer", "paper",
        )

    def set(self, name: str, value: float, basis: str, source: str) -> None:
        # An operator's explicit value always wins over a derived one.
        if name in self.thresholds and self.thresholds[name].source == "operator":
            return
        self.thresholds[name] = Threshold(name, float(value), basis, source)

    def get(self, name: str, default: float = 0.0) -> float:
        threshold = self.thresholds.get(name)
        return threshold.value if threshold else default

    def why(self, name: str) -> str:
        threshold = self.thresholds.get(name)
        return threshold.explain() if threshold else f"{name} (undefined)"

    # ------------------------------------------------------------------

    def calibrate_to_site(self, page_stats: list[dict]) -> None:
        """Re-derive thresholds from the site's own observed distribution.

        Called once the pages are fetched. The corpus anchor stays as a floor —
        a site cannot talk its way out of being thin by being uniformly thin —
        but a site whose pages are all substantial gets a *higher* bar, so
        "this page is unusually thin for this site" remains detectable.
        """
        words = sorted(s.get("word_count", 0) for s in page_stats if s.get("word_count") is not None)
        if len(words) < 3:
            self.calibration = {"status": "insufficient sample", "pages": len(words)}
            return

        median = statistics.median(words)
        quartile = words[max(0, len(words) // 4 - 1)]
        self.calibration = {
            "pages": len(words),
            "median_words": median,
            "p25_words": quartile,
            "min_words": words[0],
            "max_words": words[-1],
            "spread_ratio": round(words[-1] / max(1, words[0]), 2),
        }

        # Site-relative thin threshold: half the site median, floored at the
        # corpus bottom quartile and capped so it can never become absurd.
        site_relative = max(THIN_CONTENT_WORDS, min(median * 0.5, 600))
        if self.strictness == "lenient":
            site_relative = min(site_relative, THIN_CONTENT_WORDS)
        elif self.strictness == "strict":
            site_relative = max(site_relative, median * 0.6)

        self.set(
            "thin_words", round(site_relative),
            f"max(corpus bottom quartile 170, half this site's median of {median:g} "
            f"across {len(words)} pages), strictness={self.strictness}",
            "site-calibrated",
        )

        headings = [s.get("heading_count", 0) for s in page_stats]
        if headings and statistics.median(headings) >= 4:
            # A site that headings everything makes an unheaded page anomalous.
            self.set(
                "min_headings", 2,
                f"site median heading count is {statistics.median(headings):g}; "
                "a page with fewer than 2 is anomalous here",
                "site-calibrated",
            )

    # ------------------------------------------------------------------

    def expectations_for(self, page_type: str) -> dict:
        return PAGE_TYPE_EXPECTATIONS.get(page_type, DEFAULT_EXPECTATION)

    def min_words_for(self, page_type: str) -> tuple[float, str]:
        """Thin-content threshold for a page type, and its justification."""
        expectation = self.expectations_for(page_type)
        type_floor = expectation.get("min_words", 120)
        derived = self.thresholds.get("thin_words")
        # The page-type floor wins when it is *lower*: a hub page is allowed to
        # be short. It never raises the bar above the site-calibrated value.
        value = min(derived.value if derived else THIN_CONTENT_WORDS, type_floor) \
            if expectation.get("is_hub") else (derived.value if derived else THIN_CONTENT_WORDS)
        basis = (
            f"{page_type} pages floor at {type_floor} words; "
            f"{derived.basis if derived else 'corpus anchor'}"
        )
        return value, basis

    def sample_is_sufficient(self, n: int) -> bool:
        """Whether *n* pages support a claim about the population."""
        return n >= self.min_sample_for_population_claim

    def to_dict(self) -> dict:
        return {
            "max_pages": self.max_pages,
            "max_seconds": self.max_seconds,
            "strictness": self.strictness,
            "min_sample_for_population_claim": self.min_sample_for_population_claim,
            "thresholds": {
                name: {"value": t.value, "source": t.source, "basis": t.basis}
                for name, t in sorted(self.thresholds.items())
            },
            "site_calibration": self.calibration,
        }
