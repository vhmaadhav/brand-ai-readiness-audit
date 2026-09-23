# Brand AI-Readiness Audit — huggingface.co

*Audited 2026-09-11T05:36:21Z*
 · 20 of 300 discovered pages analysed in 24.2s


**17 findings** — **1** high · **10** medium · **5** low · **1** info

**Extractable facts** — 2 of 2 survey-graded genre(s) present (prices and visible dates; arXiv:2607.14035 §7.2, graded `Moderate` in its §7.5 lever table).

> The advisory critic reviewed 17 candidate findings and emitted 17: 0 dropped, 3 downgraded, 0 merged into a root cause.


## Findings

### F-001 · The brand declares no organisation entity anywhere in the sample

**Severity:** `high` · **Layer:** `selection` · **Claim level:** 1 · **Confidence:** high

**Evidence.** No Organization, Corporation or LocalBusiness node appears in the JSON-LD of any of the 20 sampled pages, including the homepage and about pages in the sample (e.g. https://huggingface.co/)

- Observed: 0 Organization-class nodes across the sample
- Expected: one Organization node, ideally site-wide via the base template
- Affected: https://huggingface.co/

**Suggested action** (`high` priority, low effort). Declare an Organization node site-wide with a stable @id

1. Add an Organization node to the base template: name, url, logo, description, contactPoint.
1. Give it a stable @id (https://huggingface.co/#organization) and reference it as publisher from article and product nodes.
1. List every profile the brand controls in sameAs.

*Why this works.* Official sources are 34.22-46.35% of AI citations (arXiv:2604.25707 Fig. 3). An engine reaches that pool by resolving a page to a known publisher; with no declared entity there is nothing to resolve to.

*Verify.* An Organization node is present on every page of a re-run sample.

---

### F-002 · Crawlable pages are absent from the sitemap

**Severity:** `medium` · **Layer:** `reachability` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 1 of 20 crawled pages (5%) are absent from the sitemap. Examples: https://huggingface.co.

- Observed: 1 of 20 crawled pages (5%) are absent from the sitemap.
- Affected: https://huggingface.co

**Suggested action** (`medium` priority, low effort). Add the missing pages to the sitemap, or confirm their exclusion is deliberate.

1. Regenerate the sitemap from the live route table rather than by hand.
1. Confirm each omission is intentional.
1. Automate regeneration on deploy so it cannot drift again.

*Why this works.* Pages absent from the sitemap rely entirely on internal linking to be found. Deep pages with few inbound links are the ones most likely to be missed.

*Verify.* Re-run this skill; the sitemap problem must be absent.

---

### F-003 · Pages carry no machine-readable entity declaration

**Severity:** `medium` · **Layer:** `selection` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 12 of 18 sampled pages whose page type is expected to declare a schema.org entity carry no JSON-LD, microdata or RDFa at all; affected page types: docs, homepage, other, pricing (e.g. https://huggingface.co/, https://huggingface.co/docs/accelerate/index, https://huggingface.co/pricing and 9 more)

- Observed: 12/18 eligible pages with zero structured data
- Expected: each eligible page declares at least one schema.org type appropriate to its page type
- Affected: https://huggingface.co/, https://huggingface.co/docs/accelerate/index, https://huggingface.co/pricing, https://huggingface.co/models, https://huggingface.co/docs/autotrain/index (+7 more)

**Suggested action** (`medium` priority, medium effort). Add JSON-LD to the page template declaring the entity each page is about

1. Add one <script type="application/ld+json"> block per page template.
1. Homepage and about pages: an Organization node with name, url, logo, description and contactPoint.
1. Product and pricing pages: a Product node with name and offers.
1. Article and docs pages: an Article/TechArticle node with headline, datePublished and author.
1. Validate each template once with a schema validator before shipping.

*Why this works.* Official sources supply 34.22-46.35% of AI-assistant citations (arXiv:2604.25707 Fig. 3). Resolving the publisher to a known entity is an entry condition for that pool; an undeclared page competes only for the residual share.

*Verify.* Re-run this check; json_ld_coverage should reach 1.0 for eligible page types.

---

### F-004 · Article pages declare a schema type that does not describe them

**Severity:** `medium` · **Layer:** `selection` · **Claim level:** 1 · **Confidence:** medium

**Evidence.** 3 of 6 sampled article pages carry JSON-LD that declares Organization, SocialMediaPosting but none of Article, BlogPosting, NewsArticle, TechArticle (e.g. https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom, https://huggingface.co/blog/Hcompany/neomme)

- Observed: types present: Organization, SocialMediaPosting
- Expected: one of Article, BlogPosting, NewsArticle, TechArticle
- Affected: https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom, https://huggingface.co/blog/Hcompany/neomme

**Suggested action** (`medium` priority, low effort). Declare a Article node on article pages

1. Add a Article node to the article template, populated from the same fields the page already renders.
1. Keep the existing generic nodes (WebPage, BreadcrumbList) - they are complementary, not replacements.

*Why this works.* Generic wrapper types identify a document; they do not identify the thing the document is about. An engine matching a product or how-to intent has nothing typed to match against.

*Verify.* Each article URL exposes a Article node.

---

### F-005 · Pages carry too little text to supply an answer

**Severity:** `medium` · **Layer:** `absorption` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 2 of 18 sampled non-exempt pages fall below the word-count floor. Thinnest: https://huggingface.co/docs/bitsandbytes at 295 words against a floor of 395. Threshold provenance: docs pages floor at 200 words; max(corpus bottom quartile 170, half this site's median of 790 across 20 pages), strictness=balanced. Affected: https://huggingface.co/docs/bitsandbytes, https://huggingface.co/docs/chat-ui.

- Observed: 2 of 18 non-exempt pages below their word floor
- Expected: at least the derived floor (docs pages floor at 200 words; max(corpus bottom quartile 170, half this site's median of 790 across 20 pages), strictness=balanced)
- Affected: https://huggingface.co/docs/bitsandbytes, https://huggingface.co/docs/chat-ui

**Suggested action** (`medium` priority, medium effort). Expand thin pages with extractable evidence rather than filler prose.

1. For each thin page, state what the thing is in one plain sentence (a definition).
1. Add the specific figures a reader would ask for: price, capacity, timing, scale.
1. Add one comparison against the obvious alternative.
1. Do not pad with restated marketing copy — length without evidence does not help.

*Why this works.* arXiv:2604.25707 Fig. 6: mean influence rises from 0.055 for pages under 100 words to 0.113 in the 301-600 band and 0.146 above 3,000. The paper is explicit that length helps when it is coupled with structure and evidence density, not on its own.

*Verify.* Each affected page clears its word floor with genuine evidence content.

---

### F-006 · Pages contain no extractable evidence of any kind

**Severity:** `medium` · **Layer:** `absorption` · **Claim level:** 2 · **Confidence:** high

**Evidence.** 1 of 18 sampled pages carry none of the five evidence genres the paper associates with higher absorption. These pages offer an answer engine nothing quotable — no definition of what the thing is, no figures, no comparison, no procedure. Affected: https://huggingface.co/docs.

- Observed: 1 of 18 pages carry zero evidence genres
- Expected: at least one of: definition, numeric, comparison, how-to, code
- Affected: https://huggingface.co/docs

**Suggested action** (`medium` priority, medium effort). Add at least one evidence genre to each page, matched to its purpose.

1. Product and pricing pages: add concrete numbers (price, limits, dimensions).
1. Explanatory pages: add a plain-language definition in the first paragraph.
1. Category and solution pages: add a comparison table against alternatives.
1. Documentation: add a numbered procedure or a code example.

*Why this works.* arXiv:2604.25707 §8.2 measures mean influence uplift by genre: code +76.9%, numbers +61.6%, definitions +57.3%, comparisons +55.3%, how-to +41.2%. The proposed mechanism is that these genres create reusable support units an answer can build on.

*Verify.* Every non-exempt page carries at least one evidence genre.

---

### F-007 · No definition markers anywhere in the sampled pages

**Severity:** `medium` · **Layer:** `absorption` · **Claim level:** 3 · **Confidence:** medium

**Evidence.** None of the 18 sampled pages contain a plain statement of what the product or company is. arXiv:2604.25707 §8.2 measures pages carrying definition markers at 0.1252 mean influence against 0.0795 without (+57.3%); §8.3 places this semantic role at 0.1531 mean influence. Sampled: https://huggingface.co, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/docs/accelerate and 15 other page(s).

- Observed: 0 of 18 sampled pages carry definition markers
- Expected: a plain statement of what the product or company is on the pages that describe the offering
- Affected: https://huggingface.co, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/docs/accelerate, https://huggingface.co/pricing, https://huggingface.co/models (+3 more)

**Suggested action** (`medium` priority, medium effort). Add one plain-language sentence defining what the thing is, near the top of the key pages.

1. Add one plain-language sentence defining what the thing is, near the top of the key pages.
1. Put it in body text, not only in an image or a chart.
1. State it plainly enough to be quoted in one sentence.

*Why this works.* Pages carrying definition markers average 0.1252 influence versus 0.0795 without. This is an association measured across a 23,745-row citation feature table, not a demonstrated cause; the paper reserves causal claims for intervention experiments.

*Verify.* The key pages contain a plain statement of what the product or company is.

---

### F-008 · Article content has not been dated as reviewed in over 2 year(s)

**Severity:** `medium` · **Layer:** `trust` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 1 of 6 sampled article pages carry a most-recent datestamp older than 730 days; the oldest is 2023-09-05 (1101 days ago) (e.g. https://huggingface.co/blog/gradio-workflow-1111)

- Observed: oldest datestamp 2023-09-05 (1101 days)
- Expected: article content reviewed or re-dated inside 730 days
- Affected: https://huggingface.co/blog/gradio-workflow-1111

**Suggested action** (`medium` priority, medium effort). Review the stale article pages and re-date the ones that are still correct

1. Review each listed page for statements that are no longer true.
1. Where the content still holds, record a dateModified and say what was reviewed.
1. Where it does not, update it or redirect it to the page that supersedes it.
1. Do not bulk-touch dateModified without changing anything - see FR004.

*Why this works.* Age is not itself an error; an undated old page is the problem, because nothing distinguishes 'still true' from 'never revisited'.

*Verify.* Each listed URL carries a dateModified inside the window.

---

### F-009 · Docs pages carry no datestamp

**Severity:** `medium` · **Layer:** `trust` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 5 of 5 sampled docs pages carry no date at all - no <time datetime>, no JSON-LD datePublished/dateModified and no article:published_time meta tag (e.g. https://huggingface.co/docs/accelerate/index, https://huggingface.co/docs/autotrain/index, https://huggingface.co/docs/bitsandbytes/index and 2 more)

- Observed: 5/5 docs pages with zero date signal
- Expected: a visible date plus a machine-readable equivalent
- Affected: https://huggingface.co/docs/accelerate/index, https://huggingface.co/docs/autotrain/index, https://huggingface.co/docs/bitsandbytes/index, https://huggingface.co/docs/chat-ui/index, https://huggingface.co/docs/dataset-viewer/index

**Suggested action** (`medium` priority, low effort). Add published and modified dates to the docs template

1. Render the date visibly as <time datetime="YYYY-MM-DD">.
1. Mirror it in JSON-LD as datePublished, and dateModified when the page is edited.
1. Drive both from the CMS field so they cannot disagree.

*Why this works.* An undated claim cannot be placed in time, so a reader (human or machine) cannot tell whether it has been superseded. Handout appendix D: corroboration requires knowing what is being compared against what, and when.

*Verify.* date_coverage for this page type reaches 1.0 on a re-run.

---

### F-010 · dateModified is far newer than datePublished with no visible revision

**Severity:** `medium` · **Layer:** `trust` · **Claim level:** 3 · **Confidence:** medium

**Evidence.** 1 page(s) declare dateModified 696 days after datePublished (e.g. published 2024-10-11, modified 2026-09-08) while the body text contains no update, revision or changelog language a reader could check (e.g. https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom)

- Observed: published 2024-10-11, modified 2026-09-08
- Expected: a modification date accompanied by a visible statement of what changed
- Affected: https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom

**Suggested action** (`medium` priority, low effort). Say what changed when you bump dateModified

1. Add a short 'Updated <date>: <what changed>' line where the page is edited.
1. Only advance dateModified when the content actually changed.

*Why this works.* This is an interpretation, not a proven defect - the edit may have been real and invisible. It is worth checking because a modification date with no corresponding change is a freshness signal that cannot be corroborated from the page itself.

*Verify.* Each updated page states what was revised.

---

### F-011 · Deep pages do not stand on their own for a mid-funnel arrival

**Severity:** `medium` · **Layer:** `engagement` · **Claim level:** 3 · **Confidence:** medium

**Evidence.** 4 of 11 sampled deep pages (path depth 2+) lack two or more self-orientation signals - e.g. https://huggingface.co/blog/gradio-workflow-1111 has: no breadcrumb trail, the brand is never named in the page's own text or title. A visitor sent straight here by an AI answer has no way to place the page (e.g. https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom and 1 more)

- Observed: no breadcrumb trail; the brand is never named in the page's own text or title
- Expected: every deep page names the brand, states its subject in an h1, and shows where it sits
- Affected: https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom, https://huggingface.co/blog/grpo-with-trl-ifstruct

**Suggested action** (`medium` priority, medium effort). Make every deep page a valid entry point, not a continuation of the nav

1. Add a breadcrumb trail (and a BreadcrumbList node) so the page states its own context.
1. Name the brand and the product in the page's own copy, not only in the header logo.
1. Give every page exactly one h1 that answers 'what is this page'.

*Why this works.* Interpretation, hedged: AI referrals land mid-funnel on deep URLs rather than the homepage, so a page written as step three of a guided path is read cold. Self-orientation signals are what let a cold arrival continue rather than bounce.

*Verify.* Each listed page carries a breadcrumb, an h1 and its own brand context.

---

### F-012 · JavaScript render fault: lazy media unlabelled

**Severity:** `low` · **Layer:** `reachability` · **Claim level:** 2 · **Confidence:** high

**Evidence.** 7 of 20 sampled pages exhibit 'lazy_media_unlabelled'. 20 lazy-loading markers and 37 of 40 images without alt text. Deferred, unlabelled media is invisible to any non-executing reader. Observed on https://huggingface.co/blog/gradio-workflow-1111 and 6 other page(s).

- Observed: 7 of 20 sampled pages
- Expected: primary content present in the server response
- Affected: https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom, https://huggingface.co/blog/Hcompany/neomme, https://huggingface.co/blog/grpo-with-trl-ifstruct (+2 more)

**Suggested action** (`low` priority, medium effort). Add alt text so deferred images still carry meaning as text.

1. Confirm the fault with `curl -s <url> | wc -w` — that word count is what a non-executing reader receives.
1. Add alt text so deferred images still carry meaning as text.
1. Re-check that the primary claim of the page appears in the raw HTML.

*Why this works.* Handout appendix C: a fact that is plainly visible on screen can be invisible to a program that is not looking at it the same way. arXiv:2604.25707 §4.1 measures a 76.44% fetch success rate across already-cited pages, so retrieval-side failure is common enough to treat as a first-order risk.

*Verify.* curl -s <url> | grep -c '<p' returns a non-zero count of real paragraphs.

---

### F-013 · Sitemap entries carry no lastmod

**Severity:** `low` · **Layer:** `reachability` · **Claim level:** 1 · **Confidence:** high

**Evidence.** No entry carries a lastmod, so crawlers cannot prioritise what changed. Checked at https://huggingface.co/sitemap.xml and the robots.txt declarations.

- Observed: No entry carries a lastmod, so crawlers cannot prioritise what changed.
- Affected: https://huggingface.co/sitemap.xml

**Suggested action** (`low` priority, low effort). Correct the sitemap so it validates and lists only canonical, live, in-scope URLs.

1. Regenerate the sitemap from the live route table.
1. Validate it against the sitemaps.org 0.9 schema.

*Why this works.* A sitemap a crawler distrusts is a sitemap a crawler ignores.

*Verify.* Re-run this skill; the sitemap problem must be absent.

---

### F-014 · Sitemap lastmod values are not valid W3C datetimes

**Severity:** `low` · **Layer:** `reachability` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 913 entries carry a lastmod that is not a valid W3C datetime, so crawlers will ignore it. Examples: https://huggingface.co/docs/accelerate (lastmod='2026-09-09T13:06:45.000Z'); https://huggingface.co/learn/agents-course (lastmod='2026-09-09T15:06:44.000Z'); https://huggingface.co/learn/audio-course (lastmod='2026-04-15T10:21:30.000Z').

- Observed: 913 entries carry a lastmod that is not a valid W3C datetime, so crawlers will ignore it.
- Affected: https://huggingface.co/docs/accelerate, https://huggingface.co/learn/agents-course, https://huggingface.co/learn/audio-course, https://huggingface.co/docs/autotrain, https://huggingface.co/docs/bitsandbytes

**Suggested action** (`low` priority, low effort). Correct the sitemap so it validates and lists only canonical, live, in-scope URLs.

1. Regenerate the sitemap from the live route table.
1. Validate it against the sitemaps.org 0.9 schema.

*Why this works.* A sitemap a crawler distrusts is a sitemap a crawler ignores.

*Verify.* Re-run this skill; the sitemap problem must be absent.

---

### F-015 · Heading levels skip, so the page outline does not describe its structure

**Severity:** `low` · **Layer:** `engagement` · **Claim level:** 1 · **Confidence:** high

**Evidence.** 8 of 20 sampled pages skip heading levels two or more times (e.g. h2 followed directly by h4, 5 skips on that page) (e.g. https://huggingface.co/, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series and 5 more)

- Observed: h2 -> h4 and 4 further skip(s)
- Expected: heading levels descend one step at a time
- Affected: https://huggingface.co/, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series, https://huggingface.co/blog/MultiverseComputingCAI/safety-for-whom, https://huggingface.co/blog/Hcompany/neomme (+3 more)

**Suggested action** (`low` priority, low effort). Choose heading levels by document structure, not by font size

1. Pick the level from the nesting depth of the section.
1. Style headings with CSS classes when a smaller size is wanted at a higher level.

*Why this works.* The heading tree is the page's table of contents. A tree with missing levels reads as a flat list, which removes the grouping a scanner relies on.

*Verify.* No sampled page reports a heading-level skip.

---

### F-016 · Images declare no intrinsic dimensions (layout-shift proxy, not a measurement)

**Severity:** `low` · **Layer:** `engagement` · **Claim level:** 3 · **Confidence:** medium

**Evidence.** 11 of 20 sampled pages serve images without width/height attributes or a CSS aspect-ratio - worst is https://huggingface.co/blog with 77 of 77 images. Layout shift itself was not measured and is not being claimed (e.g. https://huggingface.co/, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/pricing and 8 more)

- Observed: 77/77 images with no declared dimensions
- Expected: width and height attributes, or an aspect-ratio, on every image
- Affected: https://huggingface.co/, https://huggingface.co/blog/gradio-workflow-1111, https://huggingface.co/pricing, https://huggingface.co/models, https://huggingface.co/blog/ibm-research/ibm-releases-sota-granite-time-series (+6 more)

**Suggested action** (`low` priority, low effort). Declare width and height on images so space is reserved before they load

1. Add the intrinsic width and height attributes to <img> tags; CSS can still resize them.
1. For responsive art direction, set an aspect-ratio in CSS instead.

*Why this works.* Structural proxy, hedged: without dimensions the browser cannot reserve space, so content moves as images arrive. Whether that produces a bad CLS score depends on the runtime, which this audit does not execute.

*Verify.* A real Core Web Vitals measurement, after the attributes are added.

---

### F-017 · Legal pages show a date only as prose

**Severity:** `info` · **Layer:** `trust` · **Claim level:** 1 · **Confidence:** low

**Evidence.** 2 of 2 sampled legal pages print a date in the body text (e.g. "March 28, 2023") but expose no machine-readable equivalent in <time datetime>, JSON-LD or meta tags (e.g. https://huggingface.co/privacy, https://huggingface.co/terms-of-service)

- Observed: visible date 'March 28, 2023' with no machine-readable stamp
- Expected: the same date in <time datetime> or JSON-LD datePublished
- Affected: https://huggingface.co/privacy, https://huggingface.co/terms-of-service

**Suggested action** (`low` priority, low effort). Wrap the printed date in <time datetime> and mirror it in JSON-LD

1. Change the rendered date to <time datetime="YYYY-MM-DD">the same text</time>.
1. Add datePublished/dateModified to the page's JSON-LD node.

*Why this works.* A prose date is ambiguous across locales (03/04/2025) and is not reliably extracted. The information already exists on the page; only its format is blocking it.

*Verify.* Re-run this check; FR002 should not report this page type.

---


## Proactive recommendations

_Improvements where no specific defect was found._

### P-001 · Seed explanatory corroboration off-site, not just press coverage

**Priority:** `medium` · **Claim level:** 3

arXiv:2604.25707 §8.4 measures a split between how often a source type is selected and how deeply it is absorbed. News media supplies a large share of the candidate pool yet averages only 0.0726 mean influence, while encyclopedia-style pages average 0.2144 — roughly three times higher. Press coverage gets a brand into the pool; explanatory reference pages are what answers actually draw on. A PR-led visibility strategy optimises the weaker of the two outcomes.

**Action.** Alongside press outreach, ensure accurate explanatory entries exist on reference-style properties (industry wikis, standards bodies, well-maintained directories, documentation of integrations on partners' sites). Prioritise pages that define what the company does over pages that announce what it did.

*Basis:* arXiv:2604.25707 §8.4 domain-type influence table; handout appendix D

### P-002 · Do not add FAQ sections as an AI-visibility tactic

**Priority:** `medium` · **Claim level:** 2

This is the most commonly recommended GEO tactic and the only evidence genre arXiv:2604.25707 measures in the negative direction. Q&A-formatted pages average 0.0947 mean influence against 0.1005 for pages without the format — a 5.74% relative decrease (§8.2). Every other genre studied showed uplift between +41% and +77%. The paper's interpretation is that evidence genres create reusable support units while Q&A formatting is a surface wrapper.

**Action.** If FAQ blocks are added for human readers, make each answer carry a specific figure, definition or comparison. Never add questions as a substitute for evidence, and do not convert existing explanatory pages into Q&A format expecting a visibility gain.

*Basis:* arXiv:2604.25707 §8.2, Fig. 8 (the negative case)

### P-003 · Publish an llms.txt to state which pages carry the canonical facts

**Priority:** `low` · **Claim level:** 3

An emerging convention that lets a site nominate its authoritative explanatory pages rather than leaving an assistant to infer them from link structure. Adoption is not universal and support is not guaranteed, so this is a low-cost hedge rather than a reliable mechanism.

**Action.** Add /llms.txt listing the canonical page for each core topic — what the company does, what each product is, pricing, and the primary documentation entry point — with a one-line description each.

*Basis:* Emerging convention; no measurement in the cited paper

### P-004 · Optimise for depth on a few pages rather than breadth across many

**Priority:** `medium` · **Claim level:** 3

Citation breadth and citation depth diverge sharply by platform: mean citations per prompt are 6.88 (ChatGPT), 12.06 (Google AIO) and 16.35 (Perplexity), while mean influence per fetched page runs the other way at 0.2713, 0.0584 and 0.0646 respectively (§6, §8). A site cannot be optimal for both objectives at once, and the platform that absorbs most deeply is the one that cites fewest sources. Concentrating evidence on a small number of genuinely strong pages targets the depth objective.

**Action.** Identify the five questions this brand should be the answer to. Build one unambiguous evidence container for each — definition, figures, comparison, clear structure — instead of spreading thin coverage across many pages.

*Basis:* arXiv:2604.25707 §6 and §8 platform breadth-versus-depth divergence

### P-005 · Add the missing fact — do not rewrite the page around it

**Priority:** `high` · **Claim level:** 3

The findings above all point at page content, and the obvious response is to rewrite. That is the intervention with the strongest measured evidence of harm. In a controlled arena, body-only rewrites reduced top-20 presence by about 9%, top-10 presence after reranking by about 16%, and final citation by about 6% (arXiv:2607.14035 §7.4, summarising SAGEO Arena). Across 54 tested method-domain combinations in C-SEO Bench only 3 were significantly positive, and none were positive in question answering (§7.3) — several transformations reduced rank. Rewriting prose does not add retrievable evidence, and it can disturb whatever the page already had working.

**Action.** Treat each content finding as an instruction to add one specific, verifiable, dated fact to a page that already ranks and explains — a price, a measurement, a named comparison, an update date with its source. Leave the surrounding prose in place. Change one page, re-measure, then continue; do not rewrite the whole site at once.

*Basis:* arXiv:2607.14035 §7.3 C-SEO Bench and §7.4 SAGEO Arena


## Limitations

- The audit fetches raw HTML and does not execute JavaScript. This is deliberate — it reproduces what a non-rendering crawler sees — but means client-rendered content is diagnosed by inference rather than observation.
- No live AI assistant was queried. Findings describe page properties the cited research associates with citation outcomes; they do not measure whether this brand is currently cited.
- 20 page(s) were analysed out of 300 discovered. Findings describe the sample, not necessarily every page on the site.

---

*Generated by brand-ai-readiness-audit v1.2.1. Grounding: Zhang K., He X., Yao J. (2026). From Citation Selection to Citation Absorption. arXiv:2604.25707v2.*
