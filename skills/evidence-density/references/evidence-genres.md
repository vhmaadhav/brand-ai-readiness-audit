# Evidence genres

An "evidence genre" is a kind of content that produces reusable, extractable
support units — material an answer engine can lift and build on. This is the
paper's proposed mechanism for why some pages shape answers and others merely
get linked.

## Measured effect (§8.2)

| Genre | Mean influence with | Without | Relative difference |
|---|---|---|---|
| Code / worked examples | 0.1747 | 0.0988 | **+76.88%** |
| Numbers / statistics | 0.1171 | 0.0725 | **+61.55%** |
| Definition markers | 0.1252 | 0.0795 | **+57.33%** |
| Comparison content | 0.1389 | 0.0894 | **+55.28%** |
| How-to content | 0.1296 | 0.0918 | **+41.20%** |
| **Q&A / FAQ format** | **0.0947** | **0.1005** | **−5.74%** |

## The Q&A result

This is the finding most likely to contradict advice a site has already
received. Q&A formatting is the **only** genre measured in the negative
direction, while every other studied genre shows uplift between +41% and +77%.

The paper's interpretation:

> The likely mechanism is that evidence genres create reusable support units,
> while Q&A formatting is only a surface wrapper.

And, in the counter-intuitive-findings table at the front of the paper:

> Question-answer packaging is insufficient without evidence density and
> semantic fit.

### What this marketplace does with it

1. **Never recommends adding an FAQ.** Not as a fix, not as a proactive
   suggestion. There is a regression test asserting no actionable field in any
   report advises it.
2. **Flags Q&A pages carrying no evidence** (`qa_wrapper_without_evidence`) —
   the exact pattern the finding describes.
3. **Surfaces the warning proactively** even when no defect fired, because it
   is advice the site is likely to receive from elsewhere.
4. **Never counts Q&A toward `positive_genres`**, so a page cannot earn an
   absorption score by being interrogative.

The fix for a hollow FAQ is to put real evidence *inside the existing answers*,
not to remove the FAQ and not to add more questions.

## Detection

Each genre requires more than a single incidental match, so one stray "compared
to" in a footer does not credit a page with comparison content.

| Genre | Threshold | Markers |
|---|---|---|
| Definition | ≥2 | "is defined as", "refers to", "means that", "known as", "what is" |
| Comparison | ≥2 | "compared to", "versus", "in contrast", "unlike", "the difference between", "trade-offs" |
| How-to | ≥3 | "step N", "first,", "next,", "finally,", "how to", "follow these" |
| Numeric | ≥3 | percentages, currency, thousands separators, decimals, measured quantities |
| Code | ≥2 | fences, function/class declarations, arrows, shell commands, JSON |
| Q&A | ≥3 | "Q:"/"A:" labels, interrogative sentences |

Numeric detection deliberately excludes bare small integers, which appear
throughout navigation and boilerplate and would credit every page.

## Semantic role (§8.3)

What a citation is *used for* matters as much as what it contains:

| Role | N | Mean influence |
|---|---|---|
| definition | 1,663 | 0.1531 |
| comparison | 1,719 | 0.1524 |
| evidence | 6,216 | 0.1235 |
| statistical_data | 504 | 0.1120 |
| example | 1,468 | 0.1047 |
| opinion | 846 | 0.0938 |
| background | 5,582 | 0.0801 |
| procedure | 497 | 0.0717 |
| reference | 1,298 | 0.0529 |

Definition and comparison lead; reference-only citations are worth roughly a
third as much. A page that merely *exists to be pointed at* contributes far
less than one that supplies the shape of information the answer needs.

## Usage style (§8.3)

| Style | N | Mean influence |
|---|---|---|
| fact_source | 5,411 | 0.1241 |
| synthesized | 3,967 | 0.0964 |
| paraphrased | 5,305 | 0.0955 |
| background_only | 5,100 | 0.0775 |

This underwrites the `marketing_over_evidence` check. An unfalsifiable
superlative — "industry-leading", "world-class" — cannot function as a
`fact_source` because there is nothing in it to lift as fact. Replacing each
superlative with the specific figure that justifies it converts
`background_only` material into `fact_source` material.

## What a page should carry

Matched to purpose, not applied uniformly:

- **Product / pricing** — numeric evidence: price, limits, dimensions, capacity.
- **Explanatory / about** — a definition in the first paragraph.
- **Category / solution** — a comparison against the obvious alternative,
  including where the alternative is the better choice.
- **Documentation** — a numbered procedure or a code example.
- **Legal** — nothing. These pages are exempt; optimising terms of service for
  citation is not a desirable outcome.
