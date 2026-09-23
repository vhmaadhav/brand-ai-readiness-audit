# Claim levels — the identification map

Reproduced from arXiv:2604.25707 §5.5, which states the identification status
of every claim its dataset supports.

| Level | Kind | Paper's example | Supported? |
|---|---|---|---|
| 1 | Direct count | "Perplexity has 602 triggered prompts in the cleaned report." | Yes |
| 2 | Descriptive contrast | "ChatGPT has lower citation breadth and higher fetched-page mean influence." | Yes |
| 3 | Mechanism | "High-influence pages function as evidence containers." | Plausible interpretation |
| 4 | Causal optimisation | "Adding comparison sections will increase future absorption." | **Not established** |

The paper's own words on the danger:

> It is tempting to translate every correlation into a content tactic. That
> translation can be useful as a hypothesis-generation tool, yet it should not
> be marketed as a scientific law unless the feature has been manipulated under
> controlled conditions.

An audit report is exactly where that temptation causes damage, because the
reader will act on it.

## How each level must be written

**Level 1** — assert. "8 of 11 sampled pages contain no JSON-LD block."

**Level 2** — compare, and name both groups. "Product pages average 140 words
against 890 on article pages." Not "product pages are too thin", which smuggles
in a threshold claim.

**Level 3** — hedge, and name the mechanism as an interpretation. "Pages with
no extractable figures give an answer engine little to quote as fact — the
paper's proposed mechanism for the measured association between numeric content
and influence." Not "this is why the brand is not cited."

**Level 4** — do not write it as a finding. It belongs in
`proactive_recommendations` with its basis named and its status as a hypothesis
explicit.

## Detecting a mislabelled claim

The advisory gate re-levels a finding whose *language* is causal even when its
declared level is 1 or 2. Trigger phrases: "will increase", "will improve",
"guarantees", "ensures that", "causes", "leads directly to", "proven to
increase". Hedging language nearby ("may", "tends to", "associated with",
"correlates", "suggests", "consistent with", "proxy") clears the flag, because
a hedged mechanism statement is a legitimate level-3 claim.

The re-level is not cosmetic: it drops the severity ceiling to `medium`, which
is the point. A statement we cannot support should not also be the most urgent
thing in the report.

## Why this is enforced mechanically

Detectors are written one at a time, often by different people, and each one is
confident about its own signal. Politeness about certainty does not survive
that process. Encoding the ceiling in `cap_severity`, re-checking it in the
advisory gate, and re-validating it in `validate_report` means the discipline
holds even when an individual check author forgets it.
