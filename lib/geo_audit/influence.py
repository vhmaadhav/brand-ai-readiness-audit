"""Absorption-readiness scoring.

WHAT THIS IS NOT
----------------
This is **not** the paper's ``influence_score``. Equation (2) of
arXiv:2604.25707 is::

    Influence = 0.20 * min(ref_count/3, 1)
              + 0.15 * (1 - first_position_ratio)
              + 0.20 * paragraph_coverage_ratio
              + 0.25 * tfidf_cosine
              + 0.20 * mean(bigram_overlap, trigram_overlap)

Every one of those terms is measured against a *generated answer* — how often
the answer referenced the page, where in the answer it first appeared, how much
of the answer it covered, how similar its text was to the answer. An offline
audit has no generated answer, so none of those terms can be computed. The
paper is also explicit (§5.3) that these components must never be reused as
independent predictors of the score they define.

WHAT THIS IS
------------
A page-side readiness proxy built only from features the paper reports as
*independent* correlates of influence (Fig. 7), using those reported
correlations as relative weights:

===========================  ===========
Feature                      Reported r
===========================  ===========
page word count              0.200
definition markers           0.193
numeric / statistical        0.184
heading count                0.175
comparison markers           0.174
===========================  ===========

The score answers "does this page carry the properties that high-absorption
pages tend to carry", which is a Level 3 mechanism claim in the paper's
identification map — never a prediction that the page will be cited. Findings
derived from it are capped at ``medium`` severity for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .textstats import (
    MIN_ABSORBABLE_WORDS,
    STRUCTURE_QUARTILES,
    GenreProfile,
    word_count_band,
)

# Reported independent correlations with influence_score (paper Fig. 7),
# renormalised to sum to 1.0 so the score lands in [0, 1].
_RAW_WEIGHTS = {
    "length": 0.200,
    "definition": 0.193,
    "numeric": 0.184,
    "headings": 0.175,
    "comparison": 0.174,
}
_TOTAL = sum(_RAW_WEIGHTS.values())
WEIGHTS = {k: round(v / _TOTAL, 4) for k, v in _RAW_WEIGHTS.items()}


def _saturating(value: float, target: float) -> float:
    """Map a raw metric to [0, 1], reaching 1.0 at the top-quartile value.

    Saturating rather than linear: past the top quartile, more of the same
    metric is not evidence of more absorption, and a 20,000-word page should
    not outscore a well-structured 2,000-word one.
    """
    if target <= 0:
        return 0.0
    return min(1.0, max(0.0, value / target))


@dataclass
class AbsorptionScore:
    """A page's readiness to be absorbed, with the arithmetic exposed."""

    score: float
    components: dict[str, float] = field(default_factory=dict)
    band: str = ""
    expected_mean_influence: float | None = None
    missing_genres: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rating(self) -> str:
        if self.score >= 0.70:
            return "strong"
        if self.score >= 0.45:
            return "adequate"
        if self.score >= 0.25:
            return "weak"
        return "very_weak"

    def to_dict(self) -> dict:
        return {
            "absorption_readiness": round(self.score, 4),
            "rating": self.rating,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "weights": WEIGHTS,
            "word_count_band": self.band,
            "band_expected_mean_influence": self.expected_mean_influence,
            "missing_evidence_genres": self.missing_genres,
            "notes": self.notes,
            "interpretation": (
                "Page-side readiness proxy derived from the independent feature "
                "correlations in arXiv:2604.25707 Fig. 7. Not the paper's "
                "influence_score, which requires a generated answer. Supports "
                "Level 3 mechanism claims only."
            ),
        }


def score_absorption(doc, genres: GenreProfile) -> AbsorptionScore:
    """Compute absorption readiness for one parsed page."""
    words = doc.word_count
    headings = doc.heading_count

    components = {
        "length": _saturating(words, STRUCTURE_QUARTILES["word_count"]["top"]),
        "headings": _saturating(headings, STRUCTURE_QUARTILES["heading_count"]["top"]),
        # Genre terms are graded, not binary: the detection threshold earns most
        # of the credit, additional markers earn the rest.
        "definition": _graded(genres.counts.get("definition", 0), floor=2, full=8),
        "numeric": _graded(genres.counts.get("numeric", 0), floor=3, full=15),
        "comparison": _graded(genres.counts.get("comparison", 0), floor=2, full=8),
    }

    score = sum(components[name] * WEIGHTS[name] for name in WEIGHTS)

    band = word_count_band(words)
    notes: list[str] = []

    # The paper's one negative genre. A page wearing Q&A packaging without
    # underlying evidence density is the specific pattern §8.2 warns about.
    if genres.qa_format and genres.genre_count == 0:
        notes.append(
            "Page uses Q&A packaging but carries no definition, numeric, "
            "comparison, how-to or code content. The paper measures Q&A format "
            "alone at -5.74% mean influence (0.0947 vs 0.1005); the packaging "
            "is a wrapper, not evidence."
        )

    if words < MIN_ABSORBABLE_WORDS:
        notes.append(
            f"{words} words sits below the 301-600 band where mean influence "
            "first plateaus (0.113); pages under 100 words average 0.055."
        )

    if genres.marketing_hedges >= 5 and genres.genre_count <= 1:
        notes.append(
            f"{genres.marketing_hedges} promotional hedges against "
            f"{genres.genre_count} evidence genre(s): claims are asserted "
            "rather than substantiated, leaving nothing extractable to quote."
        )

    missing = [g for g in ("definition", "numeric", "comparison", "howto")
               if g not in genres.positive_genres]

    return AbsorptionScore(
        score=round(score, 4),
        components=components,
        band=band["band"],
        expected_mean_influence=band["expected_mean_influence"],
        missing_genres=missing,
        notes=notes,
    )


def _graded(count: int, *, floor: int, full: int) -> float:
    """Award 0 below *floor*, 0.5 at *floor*, and scale to 1.0 at *full*."""
    if count < floor:
        return round(min(0.49, count / max(1, floor) * 0.49), 4)
    if count >= full:
        return 1.0
    span = max(1, full - floor)
    return round(0.5 + 0.5 * (count - floor) / span, 4)


def aggregate_site_absorption(scores: list[AbsorptionScore]) -> dict:
    """Site-level rollup. Reports the distribution, not just a mean.

    A site with a strong homepage and eleven thin pages has a very different
    problem from one that is uniformly mediocre, and a single average hides it.
    """
    if not scores:
        return {"pages_scored": 0}
    values = sorted(s.score for s in scores)
    n = len(values)
    return {
        "pages_scored": n,
        "mean": round(sum(values) / n, 4),
        "median": round(values[n // 2], 4),
        "min": round(values[0], 4),
        "max": round(values[-1], 4),
        "weak_or_worse": sum(1 for s in scores if s.score < 0.45),
        "distribution": {
            "strong": sum(1 for s in scores if s.rating == "strong"),
            "adequate": sum(1 for s in scores if s.rating == "adequate"),
            "weak": sum(1 for s in scores if s.rating == "weak"),
            "very_weak": sum(1 for s in scores if s.rating == "very_weak"),
        },
    }
