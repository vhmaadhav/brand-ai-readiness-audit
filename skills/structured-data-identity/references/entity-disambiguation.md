# Entity disambiguation

Handout appendix D states the problem plainly:

> A related problem is mistaken identity: when several different things share a
> name, a system can mix them up unless there's something that clearly
> distinguishes one from the others.

A brand that cannot be told apart from something else does not merely lose
citations — it may be described using facts belonging to a different entity.

## Why this sits in the selection layer

arXiv:2604.25707 §7 finds source selection highly concentrated: official, news
and vertical sources make up **79.12%–87.52%** of all citations, with official
sources alone at 34.22% (ChatGPT), 46.35% (Google AIO) and 44.07% (Perplexity).
`Final_DR` medians of 526–592 indicate authority-like signals still gate entry
to the candidate pool.

Being resolvable as the *official* source for an entity is therefore an entry
condition. But note the boundary: §8.4 shows identity does **not** determine
absorption — news media is selected constantly and absorbed weakly (0.0726
against 0.2144 for encyclopedia pages). Fixing identity gets a brand into the
pool. It does not make its pages matter once they are there.

Conflating those two is how audits end up recommending schema markup as a
cure-all.

## The `sameAs` graph

`sameAs` is the strongest disambiguation signal available in structured data:
it links the entity to identities elsewhere, letting a consumer cross-reference.

Ranked by disambiguating power:

1. **Wikidata / Wikipedia** — the canonical entity resolution target, and the
   domain type with the highest measured absorption.
2. **Official registries** — company registration, stock ticker, LEI.
3. **Professional platforms** — LinkedIn, Crunchbase, industry directories.
4. **Social accounts** — verified profiles under the brand's own name.
5. **Same-org properties** — other domains the organisation controls.

Breadth matters more than count: five links to five *different* kinds of source
disambiguate better than five social accounts.

## Common-noun brand names

`SD009` raises the bar for brands whose name is a common noun, a generic word,
or a short ambiguous string. Such a brand needs *more* corroborating identity
signal than a distinctive one, because the ambiguity is inherent to the name
rather than incidental — every mention competes with the ordinary use of the
word.

This is a **claim-level-3 mechanism interpretation** and is capped at `medium`
severity. The paper measures neither brand-name ambiguity nor its effect; the
reasoning comes from the handout's description of mistaken identity plus the
observed concentration of selection on identifiable official sources. Stating
it as a measured effect would be exactly the overreach the claim-level model
exists to prevent.

## NAP consistency

Name, address and phone are checked for consistency across sampled pages.
Inconsistency is a direct observation (level 1) with a mechanical cause:
a footer template updated on some pages and not others, or a rebrand applied
unevenly.

Handout appendix D again: a claim repeated consistently across many sources is
far likelier to be believed and repeated back. A brand that contradicts *itself*
across its own pages undermines the consistency that makes its own pages
trustworthy in the first place — before any third-party corroboration is
considered.

## What good looks like

- One `Organization` node with a stable `@id`, referenced site-wide rather than
  redeclared per page with drifting values.
- `sameAs` spanning at least three different *kinds* of source.
- Name, address and phone identical everywhere they appear.
- Page-appropriate types that do not contradict each other on one page.
- Entity-bearing pages (homepage, about, contact) all resolving to the same
  entity rather than declaring subtly different organisations.
