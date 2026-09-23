# Corroboration

## The mechanism

Handout appendix D:

> Machines tend to treat a fact as more trustworthy when many independent
> places say the same thing. A claim that lives in only one spot is fragile; a
> claim repeated consistently across lots of unrelated sources is far more
> likely to be believed and repeated back.

This has a direct consequence that most brands get backwards: **your own site
cannot corroborate itself.** A fact asserted on ten of your pages is still one
source.

## The press-coverage trap

The instinct when a brand is invisible is to pursue press coverage. Press
coverage does help — at the **selection** layer. arXiv:2604.25707 §7 shows news
sources make up 16.07%–31.17% of citations, so press genuinely gets a brand
into the candidate pool.

But §8.4 measures what happens next:

| Domain type | N | Mean influence |
|---|---|---|
| encyclopedia | 527 | **0.2144** |
| academic_publishing | 86 | 0.1118 |
| commercial | 11,779 | 0.1028 |
| nonprofit | 2,009 | 0.0971 |
| academic | 1,024 | 0.0815 |
| government | 892 | 0.0769 |
| **news_media** | 1,546 | **0.0726** |

News media is the most frequently selected non-official source type and the
**least absorbed** in the table. Encyclopedia-style pages absorb roughly three
times as deeply. The paper's reading:

> News appears frequently in the candidate pool, yet `news_media` has lower
> mean influence than encyclopedia and several other domain types in the
> absorption table. This suggests that answer engines often need explanatory
> pages after they have identified timely sources.

So a corroboration profile made entirely of press hits optimises the weaker of
the two outcomes. That is what `FR008` flags, and it is one of the genuinely
non-obvious recommendations this marketplace makes.

## What to seed instead

Alongside press, not instead of it:

1. **Reference-style properties** — industry wikis, standards bodies,
   well-maintained directories. The domain type with the highest measured
   absorption.
2. **Documentation on partners' sites** — an integration page on a partner's
   documentation is independent corroboration written by someone else.
3. **Standards and specification mentions** — where the brand implements or
   contributes to a published standard.
4. **Academic and research citations** — where the domain supports it.

The distinction that matters: prioritise sources that **define what the company
does** over sources that **announce what it did**. Announcements age;
definitions are what an answer reaches for.

## Freshness

### Machine-readable dates

A date rendered as "Updated last spring" is not a datestamp. The audit looks
for `<time datetime="...">`, JSON-LD `datePublished` / `dateModified`, and
`meta` `article:published_time` — in that order of reliability.

### Staleness by page type

Thresholds come from what the page type implies about recency. An undated
contact page is fine; an undated news article is not. Page types carrying
`expect_date: false` are exempt from `FR001` — a blanket "no datestamp" check
fires on most of a normal site and trains the reader to skip the section.

### Fake freshness

`FR004` catches `dateModified` bumped on every deploy so content looks
maintained. It compares the modification gap against actual signals of revision.

A site where every page claims to have been updated yesterday and none of them
changed is making a claim a consumer can check and find false. That damages
precisely the trust the timestamp was meant to build — worse than having no
timestamp, because a missing date is merely absent while a false one is
misleading.

## Unsourced claims

`FR005` finds statistics, percentages, superlatives and market-position claims
asserted with no source. Level 3, capped at `medium`: the audit can observe
that a figure has no nearby citation, but whether that figure is *load-bearing*
is an interpretation.

The connection to absorption (§8.3): `fact_source` usage averages 0.1241 mean
influence against 0.0775 for `background_only`. An unsourced superlative cannot
function as a fact source, because there is nothing in it to lift.

## What the audit does not do

It does **not** crawl third-party sites to verify corroboration. Everything is
assessed from signals observable on the audited site: outbound references,
`sameAs` targets, cited sources, and the shape of what is linked. This
limitation is stated in the report rather than implied away, because a
corroboration finding that sounds like it checked the wider web when it did not
would be exactly the kind of overreach the advisory gate exists to catch.
