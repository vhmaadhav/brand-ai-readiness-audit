# Severity and claim-level model

Two independent axes. Confusing them is how audits end up asserting more than
they know.

## Severity — how much this costs the brand

| Severity | Meaning | Test |
|---|---|---|
| `critical` | The brand cannot enter the candidate pool at all. | Would a compliant crawler fail to retrieve or index this content? |
| `high` | A whole page type is materially impaired. | Does this remove a page's ability to be selected or absorbed? |
| `medium` | A real weakness with a clear fix, but not disqualifying. | Would fixing this measurably improve the page's standing? |
| `low` | Hygiene. Worth doing, unlikely to change outcomes alone. | Is this a best practice with a weak individual effect? |
| `info` | Context, not a defect. | Is this something the reader should know rather than fix? |

## Claim level — how much we actually know

From the identification map in arXiv:2604.25707 §5.5. The paper insists that a
GEO result state what kind of claim it is making, because the same dataset
supports confident counting and almost no causal prescription.

| Level | Kind | Example | Ceiling |
|---|---|---|---|
| 1 | Direct observation | "0 of 12 product pages carry JSON-LD" | `critical` |
| 2 | Descriptive contrast | "Product pages are thinner than blog pages" | `high` |
| 3 | Mechanism interpretation | "Thin pages give an answer engine little to extract" | `medium` |
| 4 | Causal prescription | "Adding definitions will increase citations" | not a finding |

## Why the ceiling exists

Severity expresses urgency; claim level expresses certainty. An urgent-sounding
statement we cannot support is the failure mode that destroys an audit's
credibility — and on unseen sites it is also the most likely one, because the
detector has no way to know what it is looking at.

The cap is enforced in three places rather than one, so no single mistake can
let it through:

1. `report.Finding.__post_init__` caps severity at construction time.
2. `advisory-review` re-levels findings whose *language* is causal even when
   their declared level is not, then re-caps.
3. `report.validate_report` refuses to emit a report containing any level-4
   finding or any severity above its level's ceiling.

## Level 4 material

Level 4 is not discarded — it is relocated. Causal prescriptions appear only in
`proactive_recommendations`, where each one must name its evidentiary basis and
is framed as a hypothesis. The paper makes exactly this move: it treats such
translation as "a hypothesis-generation tool" that "should not be marketed as a
scientific law".

## Priority is not severity

`suggested_action.priority` is the order in which to *do the work* and may
differ from severity. A `medium` finding with a one-line fix can carry a higher
priority than a `high` finding requiring a re-platform. Severity answers "how
bad is this"; priority answers "what should we do first".
