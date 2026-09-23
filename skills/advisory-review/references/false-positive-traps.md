# False-positive traps

Patterns that look like defects and are not. Each entry states the trap, why a
detector fires on it, and the condition under which the finding is legitimately
suppressed.

The audit is graded on unseen sites. On an unseen site a confident wrong
finding costs more than a missed one, because it sends someone to change
something that was already correct.

---

## 1. `noindex` on pages that should be noindexed

**Trap.** Search-results pages, filter and sort permutations, deep pagination,
cart, checkout, account, thank-you and preview URLs are *supposed* to carry
`noindex`. Flagging them inverts good practice into a defect.

**Suppressed when** every affected URL matches a search, filter, sort,
pagination, cart, checkout, account, login, thank-you or preview pattern. If
even one affected URL is a normal content page the finding stands, scoped to
that page.

---

## 2. Schema types that a page type should not have

**Trap.** A privacy policy has no `Product`. A contact page has no `Article`. A
detector looking for "does this page have commerce schema" fires on every page
that legitimately does not.

**Suppressed when** every affected page's type is in `{legal, about, category,
other}` and the missing schema is a commerce or article type. Expectations come
from `AuditConfig.expectations_for(page_type)`.

---

## 3. Hub and category pages judged as thin

**Trap.** A storefront category page's job is to route, not to explain. It is
*supposed* to be mostly links. Word-count thresholds fire on every well-built
one.

**Suppressed when** every affected page is a category or hub type. These carry
`is_hub: true` and a much lower word floor.

---

## 4. Legal pages judged on absorption

**Trap.** Optimising terms of service for citation is not a desirable outcome.

**Suppressed** categorically: legal pages carry `exempt_from_absorption: true`
and are excluded from absorption scoring entirely, with the exemption recorded
in the output so it is visible rather than silent.

---

## 5. Marketing language in a homepage hero

**Trap.** "Industry-leading" in a hero headline is a genre convention, not an
evidence failure. Flagging it makes the audit read as a style critique.

**Suppressed when** the homepage is the only affected page. The finding
requires promotional hedges to dominate *and* evidence to be absent, across
more than one page.

---

## 6. Missing dates on page types that do not imply recency

**Trap.** A contact page with no date is fine. An undated news article is not.
A blanket "no datestamp" check cannot tell them apart.

**Suppressed when** the page type's `expect_date` is false.

---

## 7. Population claims from a tiny sample

**Trap.** "Product pages lack structured data" from a single product page is
not a claim about product pages.

**Suppressed or demoted when** the sample is below
`min_sample_for_population_claim` (default 3). A single-page observation
survives as a narrower, lower-confidence finding rather than being discarded —
the observation was real, only the generalisation was not.

---

## 8. One page out of many treated as site-wide critical

**Trap.** One JavaScript-rendered route on a 20-page site is a page-level
problem. Reporting it as `critical` implies the site is invisible.

**Handled by** severity demotion: a single-occurrence critical on a sample of
more than three pages becomes `high`, and the finding text says "1 of N".

---

## 9. Threshold inversions

**Trap.** A detector with a flipped comparison reports "median response 1,400
ms" against an expectation of "under 3,000 ms". The finding reads plausibly and
is wrong.

**Suppressed by** the threshold-coherence gate, which parses the numbers out of
`observed` and `expected` and drops the finding when the observation already
satisfies the expectation. This catches off-by-one and inverted-comparison bugs
at the report boundary, where they would otherwise be invisible.

---

## 10. The same root cause reported five times

**Trap.** A client-rendered page produces a render fault, missing structured
data, thin content, no evidence genres and no answer-first orientation. Five
findings, one cause. The report reads as a catastrophe and buries the one fix
that resolves all of it.

**Handled by** deduplication: downstream symptoms confined to the root cause's
URLs are merged into it and listed as consequences.

---

## Adding a trap

A new entry needs: the pattern, the detector that fires on it, the suppression
condition, and the condition under which the finding should *still* stand. A
trap that suppresses unconditionally is a disabled check, not a trap — and if a
check needs unconditional suppression, the check itself is wrong and should be
removed instead.
