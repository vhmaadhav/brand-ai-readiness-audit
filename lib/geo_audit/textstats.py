"""Evidence-genre and structural analysis.

Every constant here traces to a reported figure in arXiv:2604.25707. They are
kept in one place, with the source table named, so a reviewer can check the
encoding against the paper rather than taking the thresholds on faith.

The single most important behaviour in this module is the treatment of **Q&A
format**. Adding an FAQ section is the standard GEO recommendation, and it is
the one evidence genre the paper measures as *negative* (mean influence 0.0947
with Q&A formatting versus 0.1005 without, a −5.74% relative difference,
paper §8.2). This module therefore detects Q&A wrappers in order to flag them
as insufficient, never to credit them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Paper constants
# ---------------------------------------------------------------------------

# Paper §8.2, table "Feature / True mean / False mean / Relative difference".
# Mean influence_score for pages with and without each evidence genre.
EVIDENCE_GENRE_UPLIFT = {
    "code":        {"present": 0.1747, "absent": 0.0988, "uplift_pct": +76.88},
    "numeric":     {"present": 0.1171, "absent": 0.0725, "uplift_pct": +61.55},
    "definition":  {"present": 0.1252, "absent": 0.0795, "uplift_pct": +57.33},
    "comparison":  {"present": 0.1389, "absent": 0.0894, "uplift_pct": +55.28},
    "howto":       {"present": 0.1296, "absent": 0.0918, "uplift_pct": +41.20},
    "qa_format":   {"present": 0.0947, "absent": 0.1005, "uplift_pct": -5.74},
}

# Paper §8.1, top-quartile versus bottom-quartile pages by influence_score.
# Paper §8.1, Fig. 5: top-quartile vs bottom-quartile page attributes.
# Only `top` and `bottom` are read by code (see structure_percentile). `ratio` is
# carried for traceability and holds the paper's *stated* ratio, which it derives
# from full-precision quartiles -- so it will not always equal top/bottom at the
# two decimal places printed in the table (heading total is the clear case:
# 10.59/0.85 = 12.46, but the paper reports 12.50x).
STRUCTURE_QUARTILES = {
    "word_count":      {"top": 1943.30, "bottom": 169.82, "ratio": 11.44},
    "heading_count":   {"top": 10.59,   "bottom": 0.85,   "ratio": 12.50},
    "paragraph_count": {"top": 47.49,   "bottom": 8.34,   "ratio": 5.69},
    "list_density":    {"top": 0.428,   "bottom": 0.048,  "ratio": 8.94},
}

# Paper §8.1, Fig. 6: mean influence_score by word-count bin.
WORD_COUNT_BINS = [
    (0, 100, 0.055), (101, 300, 0.085), (301, 600, 0.113),
    (601, 1000, 0.112), (1001, 3000, 0.126), (3001, 10**9, 0.146),
]

# Paper §8.3: mean influence by the semantic role a citation plays.
SEMANTIC_ROLE_INFLUENCE = {
    "definition": 0.1531, "comparison": 0.1524, "evidence": 0.1235,
    "statistical_data": 0.1120, "example": 0.1047, "opinion": 0.0938,
    "background": 0.0801, "procedure": 0.0717, "reference": 0.0529,
}

# Paper §8.4: mean influence by the publisher's domain type. Included because
# it explains the selection/absorption split — news media is selected often and
# absorbed weakly, so "get press coverage" is not a substitute for owning an
# explanatory page.
DOMAIN_TYPE_INFLUENCE = {
    "encyclopedia": 0.2144, "academic_publishing": 0.1118, "commercial": 0.1028,
    "nonprofit": 0.0971, "academic": 0.0815, "government": 0.0769,
    "news_media": 0.0726,
}

# Thresholds are set at the BOTTOM quartile, not the top. A page below the
# bottom-quartile value sits with the least-absorbed quarter of the corpus,
# which is a defensible Level 1 observation. Flagging everything below the top
# quartile would fire on most of the legitimate web and destroy precision.
THIN_CONTENT_WORDS = 170        # bottom-quartile word_count
MIN_USEFUL_HEADINGS = 1         # bottom-quartile heading_count is 0.85
LOW_PARAGRAPH_COUNT = 8         # bottom-quartile paragraph_count
MIN_ABSORBABLE_WORDS = 300      # influence plateaus above this (Fig. 6)

# ---------------------------------------------------------------------------
# Lever support, from arXiv:2607.14035 (survey of 45 GEO studies)
# ---------------------------------------------------------------------------
#
# The survey is the second grounding source, and it is deliberately a different
# kind of source from 2604.25707: that paper measures what correlates with
# absorption, this one grades how well each *lever* survives replication across
# 45 studies and ten engine families. Both are needed. A lever can correlate
# strongly in one corpus and still be unresolvable when 54 method-domain
# combinations are tested, and the marketplace should not recommend the
# difference to a customer without saying so.
#
# Table 4 of the survey grades each lever on a four-point scale. Reproduced
# here verbatim as the grade plus the survey's own qualification, so every
# suggested action in this marketplace can state the evidence behind it.
LEVER_SUPPORT = {
    "query_document_relevance": {
        "grade": "Strong",
        "note": "the clearest and most consistently replicated lever",
    },
    "position_in_context": {
        "grade": "Strong",
        "note": "where a passage sits in the retrieved context",
    },
    "extractable_evidence": {
        "grade": "Moderate-strong",
        "note": "numbers, prices, dates and references an engine can lift verbatim",
    },
    "recency_prices_dates": {
        "grade": "Moderate",
        "note": "helps, but weaker and more engine-dependent than relevance",
    },
    "document_structure": {
        "grade": "Moderate",
        "note": "moderate and heterogeneous across engines and domains",
    },
    "fluency": {
        "grade": "Weak-moderate",
        "note": "weak and inconsistent once relevance is controlled for",
    },
    "authoritative_tone": {
        "grade": "Weak",
        "note": (
            "weak and unstable; the survey's own caution is that confidence of "
            "tone must not be confused with evidence"
        ),
    },
    "formatting_alone": {
        "grade": "Poor",
        "note": "does not generalise; formatting without substance does not move citation",
    },
    "keyword_stuffing": {
        "grade": "Null or negative",
        "note": "no measured benefit, and several transformations reduce rank",
    },
}

# Survey §7.3: of 54 method-domain combinations tested in C-SEO Bench, only 3
# were significantly positive and none were positive in question answering.
# §7.4: SAGEO Arena, body-only rewrites reduced top-20 presence by ~9%, top-10
# after reranking by ~16% and final citation by ~6%.
#
# This is the single most load-bearing caution in the whole marketplace: the
# obvious way to act on a content finding -- rewrite the page body -- is the
# intervention most likely to make things worse. Every content-rewrite
# recommendation therefore carries this warning rather than assuming that
# more prose is more retrievable.
RETRIEVAL_BACKFIRE = {
    "top20_presence_pct": -9,
    "top10_reranked_pct": -16,
    "final_citation_pct": -6,
    "cseo_positive_combos": 3,
    "cseo_total_combos": 54,
    "cseo_qa_positive": 0,
    "caution": (
        "Editing the page body alone is the intervention with the strongest "
        "evidence of harm: in a controlled arena, body-only rewrites reduced "
        "top-20 presence by about 9%, top-10 presence after reranking by about "
        "16%, and final citation by about 6%. Of 54 tested method-domain "
        "combinations only 3 were significantly positive, and none were "
        "positive in question answering. Add the missing fact; do not rewrite "
        "the page around it."
    ),
}

# Survey §7.2: the genres an engine can lift verbatim. The survey's wording is
# explicit that the point is "not to 'add numbers' but relevant, verifiable,
# dated, properly attributed evidence" -- so a recommendation must never be
# satisfied by prose that merely sounds specific. Fabricated figures are worse
# than none, because a citation an assistant cannot verify is a citation it
# will not repeat.
EVIDENCE_ATTRIBUTION_NOTE = (
    "Every figure recommended here must be specific, dated and attributable to "
    "a source a reader can open. The research is explicit that the goal is not "
    "to add numbers but verifiable, attributed evidence; a fabricated or "
    "undated figure is worse than none, because an assistant that cannot "
    "corroborate it will not repeat it."
)

# ---------------------------------------------------------------------------
# Genre detection
# ---------------------------------------------------------------------------

DEFINITION_MARKERS = re.compile(
    r"\b(is defined as|refers to|is a type of|means that|is the process of|"
    r"stands for|is an? [a-z]+ that|known as|terminology|glossary|"
    r"what is\b|definition of|in other words|that is to say)\b", re.I)

COMPARISON_MARKERS = re.compile(
    r"\b(compared (?:to|with)|versus|vs\.?|in contrast|whereas|"
    r"the difference between|better than|worse than|alternative to|"
    r"pros and cons|advantages? and disadvantages?|trade-?offs?|"
    r"unlike|rather than|outperforms?)\b", re.I)

HOWTO_MARKERS = re.compile(
    r"\b(step \d|first,|second,|third,|next,|finally,|how to|"
    r"in order to|follow these|procedure|instructions|"
    r"you (?:can|should|need to|must)|begin by|start by)\b", re.I)

# A number that carries information: percentages, currency, measurements,
# and multi-digit figures. Deliberately excludes bare small integers, which
# appear in nav and boilerplate.
NUMERIC_MARKERS = re.compile(
    r"(\d+(?:\.\d+)?\s?%|[$£€¥]\s?\d|"
    r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\.\d+\b|\b\d{2,}\b\s*"
    r"(?:users|customers|percent|million|billion|thousand|years|hours|days|"
    r"ms|kg|km|mb|gb|x\b))", re.I)

CODE_MARKERS = re.compile(
    r"(```|\bfunction\s*\(|\bclass\s+\w+|=>|\bimport\s+\w+|\bdef\s+\w+|"
    r"</?[a-z]+>|\$\s?(?:npm|pip|yarn|curl|git)\s|\{\s*\"\w+\"\s*:)", re.I)

# Q&A packaging. Detected so it can be flagged, not credited.
#
# Deliberately catches all three shapes real FAQ content takes: explicit "Q:"/
# "A:" labels anywhere (not only at a line start, since markup collapses
# differently across sites), interrogative sentences, and FAQPage structured
# data. Under-detecting here would let the paper's one negative genre slip
# through unflagged, which is the specific mistake this module exists to avoid.
QA_MARKERS = re.compile(
    r"(\bq(?:uestion)?\s*[:.\)]\s|\ba(?:nswer)?\s*[:.\)]\s|"
    r"(?:what|why|how|when|where|who|can|does|do|is|are|should|will)\b[^.\n?]{0,90}\?)",
    re.I)

CITATION_MARKERS = re.compile(
    r"\b(according to|source:|cited (?:by|in)|reported by|"
    r"study (?:by|from)|research (?:by|from)|\[\d+\]|et al\.)\b", re.I)

HEDGE_MARKERS = re.compile(
    r"\b(may|might|could|possibly|perhaps|arguably|it seems|"
    r"industry-leading|world-class|best-in-class|cutting-edge|"
    r"revolutionary|game-chang\w+|seamless|synerg\w+)\b", re.I)

# Survey §7.2 and §7.5 both call out explicit prices as extractable evidence:
# they are the most concrete, most quotable, least ambiguous fact a commercial
# page can carry. Matched loosely on the currency shape rather than on a
# formatted amount, because real pages write "from $12/mo", "€1,299",
# "Rs. 4,999" and "120 GBP" and all four are equally liftable.
PRICE_MARKERS = re.compile(
    r"([$£€¥]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|inr|jpy|cny|aud|cad)\b|"
    r"\brs\.?\s?\d|\b\d[\d,.]*\s?(?:per|a)\s+(?:month|year|user|seat|day|hour)\b|"
    r"\b(?:pricing|price|cost|fee|starting at|from)\b[^.]{0,24}[$£€¥]\s?\d)",
    re.I)

# Survey §7.2, same sentence as the price genre: an undated claim is not
# extractable evidence, it is an assertion. Year-anchored phrases only -- a
# bare four-digit number is as likely to be a part number as a date.
DATE_MARKERS = re.compile(
    r"\b(?:"
    r"(?:updated|published|revised|reviewed|last modified|as of|effective)\s+"
    r"(?:on\s+)?(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}|"
    r"(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
    r"\b(?:19|20)\d{2}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])\b|"
    r"(?:updated|published|revised|reviewed|as of|effective)\s+(?:19|20)\d{2}\b|"
    r"\bq[1-4]\s+(?:19|20)\d{2}\b|"
    r"\b(?:19|20)\d{2}\s+(?:edition|report|survey|study)\b"
    r")", re.I)


@dataclass
class GenreProfile:
    """Which extractable evidence genres a page carries."""

    definition: bool = False
    comparison: bool = False
    howto: bool = False
    numeric: bool = False
    code: bool = False
    qa_format: bool = False
    citations: bool = False

    # Survey §7.2 genres. Kept apart from the five paper-measured genres above
    # because they come from a different measurement: the paper's uplift figures
    # are mean influence scores this module can quote directly, while these two
    # are graded "moderate" in the survey's lever table and must not be reported
    # with the paper's percentage uplift attached.
    price: bool = False
    dated: bool = False

    counts: dict[str, int] = field(default_factory=dict)
    marketing_hedges: int = 0

    @property
    def positive_genres(self) -> list[str]:
        """Genres the paper associates with higher absorption. Q&A is excluded."""
        return [
            name for name, present in (
                ("definition", self.definition), ("comparison", self.comparison),
                ("howto", self.howto), ("numeric", self.numeric), ("code", self.code),
            ) if present
        ]

    @property
    def survey_genres(self) -> list[str]:
        """Genres graded by the survey's lever table rather than the paper.

        Reported separately so a reader can tell which measurement backs each
        claim: the paper's genres carry a measured mean-influence uplift, these
        carry a graded support level.
        """
        return [
            name for name, present in (
                ("price", self.price), ("dated", self.dated),
            ) if present
        ]

    @property
    def genre_count(self) -> int:
        return len(self.positive_genres)

    def to_dict(self) -> dict:
        return {
            "positive_genres": self.positive_genres,
            "survey_genres": self.survey_genres,
            "genre_count": self.genre_count,
            "qa_format": self.qa_format,
            "has_citations": self.citations,
            "has_price": self.price,
            "is_dated": self.dated,
            "marker_counts": self.counts,
            "marketing_hedges": self.marketing_hedges,
        }


def profile_genres(text: str) -> GenreProfile:
    """Detect evidence genres in *text*.

    Thresholds require more than a single incidental match, so one stray
    "compared to" in a footer does not credit a page with comparison content.
    """
    counts = {
        "definition": len(DEFINITION_MARKERS.findall(text)),
        "comparison": len(COMPARISON_MARKERS.findall(text)),
        "howto": len(HOWTO_MARKERS.findall(text)),
        "numeric": len(NUMERIC_MARKERS.findall(text)),
        "code": len(CODE_MARKERS.findall(text)),
        "qa": len(QA_MARKERS.findall(text)),
        "citations": len(CITATION_MARKERS.findall(text)),
        "price": len(PRICE_MARKERS.findall(text)),
        "date": len(DATE_MARKERS.findall(text)),
    }
    return GenreProfile(
        definition=counts["definition"] >= 2,
        comparison=counts["comparison"] >= 2,
        howto=counts["howto"] >= 3,
        numeric=counts["numeric"] >= 3,
        code=counts["code"] >= 2,
        qa_format=counts["qa"] >= 3,
        citations=counts["citations"] >= 1,
        # A page with one price is quoting someone else's; a page with two is
        # publishing its own. Two matches is the same bar the paper-measured
        # genres clear, so the two families are not held to different standards.
        price=counts["price"] >= 2,
        dated=counts["date"] >= 1,
        counts=counts,
        marketing_hedges=len(HEDGE_MARKERS.findall(text)),
    )


def word_count_band(words: int) -> dict:
    """Locate a page in the paper's word-count bins (Fig. 6)."""
    for low, high, influence in WORD_COUNT_BINS:
        if low <= words <= high:
            label = f"{low}-{high}" if high < 10**9 else ">3000"
            return {"band": label, "expected_mean_influence": influence}
    return {"band": "unknown", "expected_mean_influence": None}


def structure_percentile(metric: str, value: float) -> str:
    """Place a metric against the paper's quartile boundaries."""
    ref = STRUCTURE_QUARTILES.get(metric)
    if not ref:
        return "unknown"
    if value <= ref["bottom"]:
        return "bottom_quartile"
    if value >= ref["top"]:
        return "top_quartile"
    return "mid_range"


def boilerplate_ratio(main_words: int, total_words: int) -> float:
    """Share of page text that sits outside the main content region.

    Handout appendix F: when substance is surrounded by low-value filler, a
    summariser has little to work with and the important part disappears.
    """
    if total_words <= 0:
        return 0.0
    return round(max(0.0, (total_words - main_words) / total_words), 4)


def render_gap_signals(doc) -> dict:
    """Estimate how much of the page exists only after JavaScript runs.

    We never execute JS. Instead we compare the visible text actually present in
    the raw HTML against the weight of inline script payload and the size of the
    document. A page that ships 200 KB of HTML and 40 visible words is almost
    certainly assembling its content client-side — the failure mode in the
    handout's appendix C.
    """
    words = doc.total_word_count
    script_chars = doc.inline_script_chars
    html_chars = max(1, doc.raw_html_chars)
    text_chars = len(doc.text)

    text_to_html = round(text_chars / html_chars, 4)
    script_to_text = round(script_chars / max(1, text_chars), 2)

    # Both conditions must hold: little text *and* a script-heavy document.
    # Either alone is a normal page (a short landing page; a rich app shell
    # that still ships its copy in HTML).
    likely_js_rendered = words < 120 and (text_to_html < 0.05 or script_to_text > 3.0)

    return {
        "visible_words_in_raw_html": words,
        "text_to_html_ratio": text_to_html,
        "inline_script_chars": script_chars,
        "script_to_text_ratio": script_to_text,
        "noscript_chars": doc.noscript_chars,
        "likely_js_rendered": likely_js_rendered,
    }
