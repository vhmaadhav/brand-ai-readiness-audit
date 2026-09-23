# The absorption model, and how it departs from the paper

## What the paper computes

arXiv:2604.25707 Equation (2):

```
Influence_i = 0.20 · min(ref_count_i / 3, 1)
            + 0.15 · (1 − first_position_ratio_i)
            + 0.20 · paragraph_coverage_ratio_i
            + 0.25 · tfidf_cosine_i
            + 0.20 · (bigram_overlap_i + trigram_overlap_i) / 2
```

Read the terms carefully. Every one is measured **against a generated answer**:

| Term | What it measures |
|---|---|
| `ref_count` | How many times the answer referenced this page |
| `first_position_ratio` | How early in the answer it first appeared |
| `paragraph_coverage_ratio` | How much of the answer it covered |
| `tfidf_cosine` | Text similarity between page and answer |
| `bigram/trigram_overlap` | N-gram overlap with the answer |

## Why this audit cannot compute it

An offline audit has no generated answer. There is no ChatGPT response to
measure `paragraph_coverage_ratio` against. Computing these terms would require
querying live assistants — which would be non-deterministic, rate-limited,
unavailable offline, and would make the same site produce different findings on
consecutive runs.

The paper adds a second, stronger prohibition in §5.3:

> Variables used inside Equation (2), such as `ref_count`,
> `first_position_ratio`, `paragraph_coverage_ratio`, TF-IDF similarity, and
> n-gram overlap, can be described as parts of influence. They should not be
> claimed as independent drivers of influence in a regression that uses
> `influence_score` as the dependent variable.

So we may not repurpose the components either. Doing so would be circular.

## What this audit computes instead

A different, clearly-labelled quantity built **only** from features the paper
reports as *independent* correlates of influence (Fig. 7), using the reported
correlations as relative weights:

| Feature | Reported r | Normalised weight |
|---|---|---|
| Page word count | 0.200 | 0.2160 |
| Definition markers | 0.193 | 0.2084 |
| Numeric / statistical content | 0.184 | 0.1987 |
| Heading count | 0.175 | 0.1890 |
| Comparison markers | 0.174 | 0.1879 |

Weights are the reported correlations renormalised to sum to 1.0, so the score
lands in [0, 1].

Fig. 7's three stronger correlates — LLM relevance (0.4322), answer-citation
embedding similarity (0.3561) and LLM content quality (0.2917) — are excluded.
The first and third require an LLM judgment this deterministic script does not
make; the second requires an answer. The agent reading the SKILL.md can form a
qualitative view on relevance and quality, but that judgment does not enter the
numeric score.

### Saturating, not linear

Each component maps to [0, 1] reaching 1.0 at the paper's **top-quartile**
value. Past that, more of the same metric is not evidence of more absorption,
and a 20,000-word page should not outscore a well-structured 2,000-word one.

### Graded genre terms

Genre components award 0 below the detection threshold, 0.5 at it, and scale to
1.0 at a saturation count. A page with two definition markers and a page with
twenty are different, and a binary flag would erase that.

## What the score means

> Does this page carry the properties that high-absorption pages tend to carry?

That is a **level-3 mechanism claim** in the paper's identification map, and
findings derived from it are capped at `medium` severity for exactly that
reason. It is explicitly **not**:

- a prediction that the page will be cited;
- a measurement of the page's actual influence on any answer;
- the paper's `influence_score`;
- comparable to the numeric values in the paper's tables.

The `interpretation` field in every emitted score states this, so the
distinction survives into the report rather than living only here.

## Bands

| Score | Rating | Reading |
|---|---|---|
| ≥0.70 | strong | Carries most high-absorption properties |
| ≥0.45 | adequate | Carries several |
| ≥0.25 | weak | Carries few |
| <0.25 | very weak | Sits with the least-absorbed quartile |

## Site rollup reports a distribution, not a mean

A site with one strong page and eleven thin ones has a very different problem
from one that is uniformly mediocre, and a single average hides it. The rollup
therefore reports median, mean, min, max, a count below the adequate band, and
the full band distribution.
