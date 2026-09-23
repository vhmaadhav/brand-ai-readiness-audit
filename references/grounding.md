# Grounding: every encoded fact and where it lands

Primary source:

> Zhang K., He X., Yao J. (2026). *From Citation Selection to Citation
> Absorption: A Measurement Framework for Generative Engine Optimization Across
> AI Search Platforms.* arXiv:2604.25707v2.

Dataset: 602 controlled prompts across ChatGPT, Google AI Overview/Gemini and
Perplexity; 21,181 cleaned search-layer rows; 21,143 valid search-layer
citations; 23,745 citation-level feature records; 18,151 successfully fetched
pages; 72 feature dimensions.

Secondary source: the Round 2 appendix in the contest handout (referred to
below as "appendix A–F").

Independent corroborating source:

> Martinez O. (2026). *Optimizing Visibility in Generative Engines: A Critical
> Survey of GEO (2023–2026).* arXiv:2607.14035.

A critical survey of 45 GEO studies, deliberately a different kind of evidence
from the primary paper. The primary paper measures what correlates with
absorption in one corpus; the survey grades how well each proposed *lever*
survives replication across 45 studies and ten engine families. Both are
encoded, and they are kept distinguishable in the output: where the primary
paper reports a mean-influence uplift, a finding may quote a number; where the
survey reports a grade, the finding cites the grade and quotes no effect size.

Two further papers shape what the marketplace refuses to claim:

> *Scoring Without the Engine* (2026). arXiv:2609.07559. Adversarial
> falsification gates for page-side GEO scores.
>
> *What Gets Cited: Competitive GEO in AI Answer Engines* (2026).
> arXiv:2605.25517. 252,000 trials across 18 content factors.

### The Fig. 7 weights, and exactly where they come from

Figure 7 of the paper is sourced from the `geo-citation-lab` public report
(§6.4, "which features best predict high influence"), which the paper names as
the figure's source. That report's table is:

| Feature | r | used as a weight? |
|---|---:|---|
| LLM relevance score | 0.4322 | **no** — needs a generated answer |
| answer–citation embedding similarity | 0.3561 | **no** — needs a generated answer |
| LLM content quality score | 0.2917 | **no** — needs a generated answer |
| question–citation embedding similarity | 0.2548 | **no** — needs a generated answer |
| page word count | 0.1995 | yes → 0.200 |
| contains definition markers | 0.1934 | yes → 0.193 |
| contains numbers/statistics | 0.1842 | yes → 0.184 |
| heading total | 0.1751 | yes → 0.175 |
| contains comparison content | 0.1741 | yes → 0.174 |

The four strongest correlates are deliberately excluded: every one of them is
measured against a generated answer, so an offline audit cannot observe them
(§5.3). The five retained values are the strongest correlates that are
page-side observable, and they are rounded to three decimal places for
readability. `influence.py` renormalises them to sum to 1.0.

This distinction matters for how the score may be described: it is built from
the paper's *reported correlates*, not from the paper's `influence_score`
itself, and every emitted score carries that limitation in its
`interpretation` field.

---

## Traceability table

| Paper figure | Value | Where it is encoded | Effect |
|---|---|---|---|
| §6 search-trigger rates | 98.64% / 99.67% / 100.00% | `url-validation` severity model | A retrieval-crawler block is `critical`, a training block `medium` |
| §6 citations per prompt | 6.88 / 12.06 / 16.35 | `advisory-review` proactive layer | The breadth-versus-depth recommendation |
| §8 mean influence | 0.2713 / 0.0584 / 0.0646 | pipeline decomposition | Selection and absorption are separate skills |
| §4.1 fetch success rate | 76.44% | `crawl-reachability` | Reachability is a first-class gate; cited in every reachability fix's mechanism |
| §7 source-type share | 79.12%–87.52% official+news+vertical | `structured-data-identity` | Identity treated as an entry condition |
| §7 official share | 34.22% / 46.35% / 44.07% | `structured-data-identity` SKILL.md | Rationale for the selection layer |
| §7 `Final_DR` medians | 526–592 | `structured-data-identity` references | Authority still gates pool entry |
| §8.1 word count quartiles | 1943.30 / 169.82 (11.44×) | `textstats.STRUCTURE_QUARTILES` | `thin_words` floor = 170 (bottom quartile) |
| §8.1 heading quartiles | 10.59 / 0.85 (12.50×) | `textstats.STRUCTURE_QUARTILES` | `min_headings` floor |
| §8.1 paragraph quartiles | 47.49 / 8.34 (5.69×) | `textstats.STRUCTURE_QUARTILES` | `low_paragraphs` floor |
| §8.1 list density quartiles | 0.428 / 0.048 (8.94×) | `textstats.STRUCTURE_QUARTILES` | Structure findings |
| §8.2 code uplift | 0.1747 vs 0.0988 (+76.88%) | `textstats.EVIDENCE_GENRE_UPLIFT` | Genre detection + chart |
| §8.2 numeric uplift | 0.1171 vs 0.0725 (+61.55%) | `textstats.EVIDENCE_GENRE_UPLIFT` | `no_numeric_evidence` |
| §8.2 definition uplift | 0.1252 vs 0.0795 (+57.33%) | `textstats.EVIDENCE_GENRE_UPLIFT` | `no_definition_content` |
| §8.2 comparison uplift | 0.1389 vs 0.0894 (+55.28%) | `textstats.EVIDENCE_GENRE_UPLIFT` | `no_comparison_content` |
| §8.2 how-to uplift | 0.1296 vs 0.0918 (+41.20%) | `textstats.EVIDENCE_GENRE_UPLIFT` | Genre detection |
| **§8.2 Q&A effect** | **0.0947 vs 0.1005 (−5.74%)** | `textstats`, `influence`, `evidence-density`, `advisory-review`, `charts` | **Never recommends an FAQ; flags hollow ones; standing proactive warning; regression test** |
| §8.3 semantic role | definition 0.1531 → reference 0.0529 | `textstats.SEMANTIC_ROLE_INFLUENCE` | Genre-gap finding mechanisms |
| §8.3 usage style | fact_source 0.1241 vs background_only 0.0775 | `evidence-density` | `marketing_over_evidence` |
| §8.4 domain type | encyclopedia 0.2144 → news_media 0.0726 | `textstats.DOMAIN_TYPE_INFLUENCE` | `FR008` and the press-coverage proactive recommendation |
| Fig. 6 word-count bins | 0.055 → 0.146 across six bins | `textstats.WORD_COUNT_BINS` | `MIN_ABSORBABLE_WORDS`; the length chart |
| Fig. 7 independent correlations | words 0.200, definition 0.193, numeric 0.184, headings 0.175, comparison 0.174 | `influence.WEIGHTS` | The absorption-readiness score |
| §5.3 component-reuse prohibition | — | `influence.py` module docstring | Why Eq. (2) is *not* reimplemented |
| §5.5 identification map | Levels 1–4 | `report.CLAIM_LEVEL_MAX_SEVERITY` | Severity ceilings; level-4 exclusion |
| Appendix A | crawl → read → extract | Pipeline gate ordering | Why reachability precedes everything |
| Appendix C | content assembled after load | `renderfaults.py` | Eight-way render-fault classification |
| Appendix D | agreement across sources | `freshness-corroboration`, `SD010`, host canonicalisation | Corroboration and self-consistency checks |
| Appendix E | answers tailored to the asker | `EN002` | Deep pages must stand alone |
| Appendix F | summariser legibility | `EN005`, `EN006`, `boilerplate_ratio` | Substance-versus-filler checks |

### From the survey (arXiv:2607.14035)

| Survey figure | Value | Where it is encoded | Effect |
|---|---|---|---|
| §7.2 evidence genres | prices, dates, references | `textstats.PRICE_MARKERS`, `DATE_MARKERS`, `GenreProfile.price`/`dated` | `no_price_evidence`, `no_date_evidence` |
| §7.2 framing | "not to 'add numbers' but relevant, verifiable, dated, properly attributed evidence" | `textstats.EVIDENCE_ATTRIBUTION_NOTE` | Every evidence recommendation requires a named source |
| §7.3 C-SEO Bench | 3 of 54 method-domain combinations significantly positive; none positive in QA | `textstats.RETRIEVAL_BACKFIRE` | Reported alongside the backfire caution |
| §7.4 SAGEO Arena | body-only rewrites: −9% top-20, −16% top-10 after reranking, −6% final citation | `textstats.RETRIEVAL_BACKFIRE` | Attached to every content-rewrite recommendation |
| §7.5 Table 4 lever grades | relevance/prominence Strong; extractable evidence Moderate-strong; recency, structure Moderate; fluency Weak-moderate; authoritative tone Weak; formatting alone Poor; keyword stuffing Null/negative | `textstats.LEVER_SUPPORT` | Emitted as `lever_support`; sets the claim level a genre finding may reach |
| §7.5 authoritative tone | "do not conflate confidence with evidence" | `LEVER_SUPPORT["authoritative_tone"]` | Promotional-hedge findings cite it; tone is never recommended on its own |
| §8.1 engine disagreement | 26% domain overlap, URL Jaccard 0.11–0.18 | scored under "What is *not* claimed" | Single-engine position is never treated as a stable target |

### From the falsification literature

| Source | Value | Where it is encoded | Effect |
|---|---|---|---|
| 2609.07559 | within-query rank correlation ≈ 0.11 between page-side scores and citation | `score_evidence.py` payload note | The absorption score is positioned as a quality filter, not a citation predictor |
| 2609.07559 | adversarial benchmark: attacker gains ≤ 6 points across 500 sources | design constraint | Guards stay in the report as visible limitations |
| 2605.25517 | topical relevance and list position dominate; formatting-only edits do little | `LEVER_SUPPORT`, finding mechanisms | Findings lead with relevance and evidence, never with markup |
| 2605.25517 | explicit prices and recent timestamps help | `no_price_evidence`, `no_date_evidence` | Price and date promoted to first-class genres |

---

## What is *not* claimed

The paper's identification map (§5.5) draws a line this marketplace holds to.

**Claimed (levels 1–2):** what was observed on the audited site — counts of
pages missing a feature, contrasts between page types, status codes,
robots directives, parse failures.

**Interpreted (level 3, capped at `medium`):** that a page lacking extractable
evidence gives an answer engine little to work with. This is the paper's own
proposed mechanism, and it is stated as interpretation.

**Never claimed (level 4):** that making a specific change *will* cause the
brand to be cited more. The paper reserves that for intervention experiments
nobody has run:

> Level 4: causal optimisation. Example: adding comparison sections will
> increase future absorption. Supported by this manuscript? Not established
> here.

Level-4 material appears only in `proactive_recommendations`, labelled as a
hypothesis with its basis named.

## Deliberate divergences

**The absorption score is not `influence_score`.** Equation (2)'s terms all
require a generated answer, and §5.3 forbids reusing them as independent
predictors. This marketplace computes a different quantity from the reported
independent correlates and says so in every emitted score's `interpretation`
field. See `skills/evidence-density/references/absorption-model.md`.

**Thresholds sit at the bottom quartile, not the top.** Using top-quartile
values (1,943 words, 10.6 headings) would flag most of the legitimate web.
Bottom-quartile values identify pages sitting with the least-absorbed quarter
of the corpus, which is a defensible level-1 observation.

**Site calibration overrides corpus anchors upward, never downward.** The
corpus bottom quartile is a floor a site cannot argue its way below by being
uniformly thin, while a site of long-form guides gets a higher bar so
"unusually thin *for this site*" stays detectable.

**Platform-specific figures are not turned into platform-specific advice.** The
paper warns that these are "platform-specific descriptive profiles, not
permanent platform laws" and that product behaviour may change. The
marketplace uses them to explain *why* breadth and depth are different
objectives, not to recommend optimising for one named product.

**A page-side score is a quality filter, not a citation predictor.** The
falsification literature is direct about this: aggregate page-side scores
correlate with citation at roughly 0.11 in rank terms within a query
(arXiv:2609.07559), and re-measuring classic GEO effect sizes on modern engine
families moves citation on none of them. The absorption score therefore exists
to find pages that are *hard to quote*, which is a statement about the page and
is verifiable offline. It is never presented as a forecast of citations, and
no finding's severity depends on a predicted ranking outcome.

**Content-rewrite advice carries a warning, not an assumption.** The obvious
way to act on a content finding is to rewrite the page body, and that is the
intervention with the strongest measured evidence of *harm*: body-only rewrites
reduced top-20 presence by about 9%, top-10 after reranking by about 16%, and
final citation by about 6% in a controlled arena (arXiv:2607.14035 §7.4). Only
3 of 54 tested method-domain combinations were significantly positive, and none
in question answering (§7.3). Recommendations therefore say *add the missing
fact*, never *rewrite the page around it*.

**Evidence recommendations require attribution, not specificity.** Survey §7.2
is explicit that the goal is "not to 'add numbers' but relevant, verifiable,
dated, properly attributed evidence." A figure a reader cannot check is worse
than no figure, because an assistant that cannot corroborate it will not repeat
it. Every genre recommendation names the source requirement alongside the
genre.
