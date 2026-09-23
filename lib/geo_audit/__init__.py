"""geo_audit - shared library for the brand-ai-readiness-audit marketplace.

Standard library only. No third-party dependencies, no external services.

The package implements the observable half of the two-stage GEO measurement
framework described in arXiv:2604.25707 (Zhang, He & Yao, 2026): citation
selection (is a page eligible to be chosen as a source) and citation absorption
(does a cited page actually shape the generated answer).
"""

__version__ = "1.1.0"

PAPER_CITATION = (
    "Zhang K., He X., Yao J. (2026). From Citation Selection to Citation "
    "Absorption: A Measurement Framework for Generative Engine Optimization "
    "Across AI Search Platforms. arXiv:2604.25707v2."
)
