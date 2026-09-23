# Brand AI-Readiness Audit

An Agent Skill Marketplace that audits any website for the two halves of the
problem: **why a brand isn't found or cited by AI assistants**, and **why the
visitors who do arrive don't stay**. It emits one evidence-backed report of
findings plus prioritised, mechanism-sound suggested actions.

Point it at a URL and it produces JSON, Markdown, HTML and PDF:

```bash
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com \
  --formats json,md,html,pdf --out ./audit-report
```

No installation. No dependencies. Python 3.10+ standard library only.

---

## Contents

- [What it does](#what-it-does)
- [The eight skills](#the-eight-skills)
- [The advisory agent](#the-advisory-agent)
- [Grounded facts](#grounded-facts)
- [Report format](#report-format)
- [Running it](#running-it)
- [How thresholds are derived](#how-thresholds-are-derived)
- [Tests](#tests)
- [Safety](#safety)
- [Layout](#layout)

---

## What it does

The design decision that shapes everything: **generative engine optimisation is
a staged pipeline, not a ranking event.** A page must clear every gate in order,
and the gate it fails determines which fix applies. A single "AI visibility
score" collapses these and loses the information that tells you what to do.

```
   URL
    |
    v
[1] preflight ........ is this target safe and in scope to audit?
    |                  SSRF defence, DNS, redirect revalidation, robots policy
    v
[2] reachability ..... can a crawler obtain the bytes and read them?
    |                  sitemap audit, render faults, indexability
    v
[3] selection ........ is the brand eligible and unambiguous?
    |                  structured data, entity identity, sameAs graph
    v
[4] absorption ....... does a cited page actually shape the answer?
    |                  evidence genres, structural legibility
    v
[5] trust ............ is the claim current and corroborated?
    |                  datestamps, staleness, single-source fragility
    v
[6] engagement ....... does the arriving human stay?
    |                  answer-first orientation, intent continuity
    v
[7] advisory ......... is each finding actually defensible?
    |                  drops, downgrades, merges, adds proactive layer
    v
[8] compose .......... one validated report, four formats
    v
[9] review loop ...... independent re-check; REGENERATE repairs, re-renders,
                       re-reviews (bounded); critic stays authoritative
```

Steps 3–6 are mutually independent and all read the same page inventory, so a
failure in one leaves the others valid.

---

## The eight skills

| Skill | Gate | What it owns |
|---|---|---|
| `audit-orchestrator` **(entrypoint)** | composition | Runs the pipeline, composes findings, validates, renders |
| `url-validation` | preflight | Normalisation, SSRF defence, redirect revalidation, robots policy, scope, budget |
| `crawl-reachability` | reachability | Discovery, deep sitemap.xml audit, JS render-fault diagnosis, indexability |
| `structured-data-identity` | selection | Schema validity and appropriateness, Organization completeness, entity disambiguation |
| `evidence-density` | absorption | Evidence genres, structural legibility, the Q&A trap |
| `freshness-corroboration` | trust | Datestamps, staleness, unsourced claims, corroboration shape |
| `engagement-audit` | engagement | Answer-first orientation, mid-funnel arrivals, interstitials, scannability |
| `advisory-review` | verification | The in-loop critic that every finding must survive |

Each folder follows the Agent Skills format (`SKILL.md` with YAML frontmatter,
plus `scripts/` and `references/`). The skills compose through the marketplace's
shared `lib/geo_audit` package and workspace, so keep and run the complete
marketplace tree. Invoke `audit-orchestrator` as the designated entrypoint.

---

## The advisory agent

Detectors are optimistic by construction: each looks for its own failure mode
and finds it. Nothing in a detector's own logic asks *"would a careful reviewer
accept this?"*

The advisory agent sits between detection and emission with the authority to
**drop and downgrade**, not merely annotate. Findings reach the report only
through it — there is no path around the critic, and if it fails to run the
orchestrator refuses to emit a report at all.

**Five gates:**

1. **Evidence sufficiency** — every finding must carry a concrete observed
   value *and* a real URL. "The site lacks structured data" is rejected; "8 of
   11 sampled pages (e.g. `https://…/products/x`) contain no JSON-LD" passes.
2. **Threshold coherence** — the observed value must actually breach the stated
   expectation. A finding reporting "median 1,400 ms" against "under 3,000 ms"
   is dropped, catching inverted comparisons at the report boundary.
3. **Sample sufficiency** — a population claim needs enough population. Below
   the minimum, findings are demoted to single-page observations or dropped.
4. **Claim-level discipline** — see below.
5. **False-positive traps** — a documented list of patterns that look like
   defects and are not. Full rationale in
   `skills/advisory-review/references/false-positive-traps.md`.

It also **merges** downstream symptoms into their root cause: a
JavaScript-rendered page produces a render fault, missing structured data and
thin content — three findings, one fix. And it adds the **proactive layer**,
held to a higher bar than findings: each recommendation must name its
evidentiary basis and be non-obvious.

Every decision is recorded in the report's `advisory` block, so you can see
exactly what was removed and why.

### Claim-level discipline

From the identification map in the source paper (§5.5):

| Level | Kind | Example | Severity ceiling |
|---|---|---|---|
| 1 | Direct observation | "0 of 12 product pages carry JSON-LD" | `critical` |
| 2 | Descriptive contrast | "Product pages are thinner than blog pages" | `high` |
| 3 | Mechanism interpretation | "Thin pages give an engine little to extract" | `medium` |
| 4 | Causal prescription | "Adding definitions will increase citations" | **never a finding** |

The paper is explicit that its dataset supports counting and contrast, and
supports almost no causal prescription:

> It is tempting to translate every correlation into a content tactic. That
> translation can be useful as a hypothesis-generation tool, yet it should not
> be marketed as a scientific law unless the feature has been manipulated under
> controlled conditions.

An audit report is exactly where that temptation does damage, because the
reader acts on it. The ceiling is enforced in three independent places
(construction, advisory re-levelling, schema validation) so no single oversight
lets a level-4 claim through. Level-4 material isn't discarded — it's relocated
to `proactive_recommendations` and labelled a hypothesis.

---

## Grounded facts

Every threshold and recommendation traces to a measured figure in:

> Zhang K., He X., Yao J. (2026). *From Citation Selection to Citation
> Absorption: A Measurement Framework for Generative Engine Optimization Across
> AI Search Platforms.* arXiv:2604.25707v2 — 602 prompts, 21,143 valid
> search-layer citations, 23,745 citation-level feature records, 18,151
> successfully fetched pages, 72 features, across ChatGPT, Google AI Overview
> and Perplexity.

A second source is cited for the levers the paper does not measure, and the two
are **never blended into one number**:

> *Generative Engine Optimization: A Survey of Methods, Evaluation, and
> Challenges* (2026). arXiv:2607.14035 — a synthesis of 45 GEO studies. It
> supplies the extractable-fact genres (§7.2), the graded lever table (§7.5,
> Table 4) used to label suggested actions, and the retrieval-backfire results
> (§7.4).

Where a lever has a **measured effect size**, the report quotes the figure and
names the paper. Where a lever is only **graded**, the report quotes the grade
and names the survey. A coverage grade and an effect size are different orders
of claim, which is why the two genre families are kept in separate report fields
(`scores.evidence_genres` and `scores.survey_genres`) and only one of them is
plotted against the uplift bars.

### Selection and absorption are different outcomes

The paper's central result. Being cited is not the same as mattering:

| Platform | Search trigger rate | Mean citations/prompt | Mean influence of fetched pages |
|---|---|---|---|
| ChatGPT | 98.64% | 6.88 | **0.2713** |
| Google AIO | 99.67% | 12.06 | 0.0584 |
| Perplexity | 100.00% | 16.35 | 0.0646 |

Perplexity cites ~2.4× as many sources as ChatGPT and absorbs each about a
quarter as deeply. **Search triggering is near-universal**, so triggering isn't
the frontier — selection and absorption are. Encoded as: the pipeline splits
selection (`structured-data-identity`) from absorption (`evidence-density`),
and they emit different fixes.

### Reachability is a measured failure mode

**Fetch success rate: 76.44%.** Roughly one in four pages that an engine had
*already chosen to cite* could not be retrieved. Encoded as: reachability is a
first-class gate (`crawl-reachability`) rather than a hygiene check, and render
faults are diagnosed per-fault-type because the fixes differ.

### Evidence genres — and the one that backfires (§8.2)

| Genre | Influence with | Influence without | Difference |
|---|---|---|---|
| Code / worked examples | 0.1747 | 0.0988 | **+76.88%** |
| Numbers / statistics | 0.1171 | 0.0725 | **+61.55%** |
| Definition markers | 0.1252 | 0.0795 | **+57.33%** |
| Comparison content | 0.1389 | 0.0894 | **+55.28%** |
| How-to content | 0.1296 | 0.0918 | **+41.20%** |
| **Q&A / FAQ format** | **0.0947** | **0.1005** | **−5.74%** |

**"Add an FAQ" is the standard GEO recommendation and the only genre the paper
measures in the negative direction.** The proposed mechanism: evidence genres
create reusable support units, while Q&A formatting is a surface wrapper —
rephrasing thin content as a question adds nothing to extract.

Encoded as: this marketplace **never recommends adding an FAQ**, flags
Q&A-wrapped pages carrying no evidence underneath, and surfaces the warning as
a standing proactive recommendation even when no defect fired. There is a
regression test asserting no actionable field ever advises it.

### Structure separates the quartiles (§8.1)

Top vs bottom quartile pages by influence score:

| Metric | Top 25% | Bottom 25% | Ratio |
|---|---|---|---|
| Word count | 1,943.30 | 169.82 | 11.44× |
| Heading total | 10.59 | 0.85 | 12.46× |
| Paragraph count | 47.49 | 8.34 | 5.69× |
| List density | 0.428 | 0.048 | 8.92× |

Thresholds are set at the **bottom** quartile (a floor to clear), never the
top — flagging everything below the top quartile would fire on most of the
legitimate web and destroy precision.

### Length helps, but only with structure (Fig. 6)

| Word count | Mean influence |
|---|---|
| ≤100 | 0.055 |
| 101–300 | 0.085 |
| 301–600 | 0.113 |
| 601–1,000 | 0.112 |
| 1,001–3,000 | 0.126 |
| >3,000 | 0.146 |

The paper is explicit that length helps *when coupled with usable structure and
evidence density*, not on its own. Encoded as: thin-content fixes say "expand
with extractable evidence, not filler prose".

### Press coverage optimises the weaker outcome (§8.4)

| Domain type | Mean influence |
|---|---|
| encyclopedia | **0.2144** |
| academic_publishing | 0.1118 |
| commercial | 0.1028 |
| nonprofit | 0.0971 |
| academic | 0.0815 |
| government | 0.0769 |
| **news_media** | **0.0726** |

News media is the most frequently *selected* non-official source type (16.07%–
31.17% of citations) and the least *absorbed*. Encyclopedia-style pages absorb
roughly 3× as deeply. Encoded as a proactive recommendation: seed explanatory
reference coverage alongside press, because a PR-led strategy optimises
selection while neglecting absorption.

### Identity gates pool entry (§7)

Official + news + vertical sources account for **79.12%–87.52%** of all
citations; official alone is 34.22% (ChatGPT), 46.35% (Google), 44.07%
(Perplexity). `Final_DR` medians of 526–592 indicate authority signals still
gate entry. Encoded as: `structured-data-identity` treats entity resolution as
an entry condition — while explicitly *not* claiming it improves absorption.

### Semantic role determines what a citation is worth (§8.3)

definition 0.1531 · comparison 0.1524 · evidence 0.1235 · statistical_data
0.1120 · example 0.1047 · opinion 0.0938 · background 0.0801 · procedure
0.0717 · reference 0.0529

Usage style: fact_source 0.1241 · synthesized 0.0964 · paraphrased 0.0955 ·
background_only 0.0775. Encoded as: promotional hedging without substantiation
is flagged because unfalsifiable superlatives cannot function as a fact source.

### What the absorption score is *not*

The paper's `influence_score` (Eq. 2) is:

```
Influence = 0.20·min(ref_count/3, 1) + 0.15·(1 − first_position_ratio)
          + 0.20·paragraph_coverage_ratio + 0.25·tfidf_cosine
          + 0.20·mean(bigram_overlap, trigram_overlap)
```

**Every term is measured against a generated answer.** An offline audit has no
generated answer, so none can be computed — and the paper explicitly forbids
reusing these components as independent predictors of the score they define
(§5.3).

This marketplace therefore computes a *different*, clearly-labelled proxy from
the features the paper reports as **independent** correlates (Fig. 7), using
those correlations as relative weights: page word count (r=0.200), definition
markers (0.193), numeric content (0.184), heading count (0.175), comparison
markers (0.174). It answers "does this page carry the properties high-absorption
pages tend to carry" — a level-3 mechanism claim, capped at `medium` severity,
never a prediction that the page will be cited.

The evidence is blunt about how far that extends. arXiv:2609.07559 finds
page-quality scores correlate with being cited at a within-query Spearman of
about **0.11** — real, but weak. So the proxy is documented and used as a
**quality filter**, never as a ranking forecast: it can say a page lacks what
top-absorption pages carry, and it is never used to promise a citation.

### Recommended levers are graded, and one backfires

arXiv:2607.14035 §7.5 Table 4 grades the support behind each common GEO lever,
and the report carries those grades through to the actions it recommends so the
reader can see how strong the evidence is before spending effort.

The same survey reports the result that constrains every content fix: §7.4
(SAGEO Arena) found that rewriting a page's body copy alone **reduced** top-20
presence by about 9%, top-10 (reranked) presence by about 16%, and citation by
about 6%; §7.3 (C-SEO Bench) found 3 of 54 method-and-metric combinations
positive, none of them on QA. Every content-rewrite recommendation in this
marketplace therefore carries that caution, and the advisory layer prefers
**add the missing fact** over **rewrite the page around it**.

That is also why extractable facts are framed as *verifiable, dated and
attributed* rather than as "add more numbers": the survey's own §7.2 reading is
that prices, dates and references lift a page because they are checkable, and a
fabricated statistic is worse than no statistic.

---

## Report format

The required shape is reproduced exactly; everything else is an addition.
Validated against `skills/audit-orchestrator/references/report-schema.json`
before emission — an invalid report is never written.

Two `scores` keys carry genre coverage, and they are deliberately separate.
`evidence_genres` holds the genres whose influence uplift arXiv:2604.25707 §8.2
*measures*, and it is the only one plotted against the uplift bars.
`survey_genres` holds the extractable-fact genres arXiv:2607.14035 §7.2 names
and §7.5 *grades* — prices and visible dates. A measured effect size and a
coverage grade are different orders of claim, so they never share a chart or a
sentence.

```json
{
  "site": "example.com",
  "audited_at": "2026-09-07T14:32:00Z",
  "summary": { "total_findings": 23, "critical": 1, "high": 9, "medium": 10, "low": 2, "info": 1 },
  "findings": [
    {
      "id": "F-001",
      "title": "robots.txt blocks AI crawlers that fetch pages during answer generation",
      "severity": "critical",
      "evidence": "https://example.com/robots.txt disallows PerplexityBot at '/'. These are live-retrieval agents that fetch pages while an answer is being written...",
      "suggested_action": {
        "summary": "Allow the live-retrieval agents on public content.",
        "priority": "critical",
        "steps": ["..."],
        "effort": "low",
        "mechanism": "Blocking retrieval and blocking training are different decisions...",
        "verification": "Re-run this skill; blocked_retrieval must be empty."
      },
      "layer": "preflight",
      "claim_level": 1,
      "confidence": "high",
      "observed": "blocked live-retrieval agents: PerplexityBot",
      "expected": "live-retrieval agents allowed on public content",
      "affected_urls": ["https://example.com/robots.txt"],
      "sample_size": 12
    }
  ],
  "scope": { "pages_analysed": 12, "javascript_executed": false, "robots_respected": true },
  "scores": {
    "absorption": {...},
    "evidence_genres": { "definition": 9, "numeric": 11, "code": 2, "qa_format": 3 },
    "survey_genres": { "price": 0, "dated": 7 }
  },
  "proactive_recommendations": [ ... ],
  "advisory": { "findings_in": 26, "findings_out": 23, "dropped": [...], "merged": [...] },
  "limitations": [ ... ]
}
```

### Output formats

| Format | Use |
|---|---|
| **JSON** | The canonical artefact |
| **Markdown** | Drop into a pull request or ticket |
| **HTML** | Self-contained, inline SVG charts, light/dark aware, no external assets |
| **PDF** | Vector charts, written with the standard library alone — no reportlab, no matplotlib |

Six charts in both HTML and PDF: findings by severity, findings by pipeline
layer, finding mix donut, **evidence-genre uplift with your coverage overlaid**
(the Q&A bar renders negative, in red), absorption-readiness gauge, and page
length distribution against the paper's expected influence per bin.

---

## Running it

```bash
# Full audit, all formats
python3 skills/audit-orchestrator/scripts/run_audit.py example.com \
  --formats json,md,html,pdf --out ./audit-report

# Tighter budget
python3 skills/audit-orchestrator/scripts/run_audit.py example.com \
  --max-pages 10 --max-seconds 120

# Stricter: the advisory drops what it would otherwise demote
python3 skills/audit-orchestrator/scripts/run_audit.py example.com --strict-advisory

# Review loop controls (step 9; on by default)
python3 skills/audit-orchestrator/scripts/run_audit.py example.com --max-review-passes 5
python3 skills/audit-orchestrator/scripts/run_audit.py example.com --no-review-loop  # not recommended

# Standalone second pass over any draft report
python3 skills/audit-orchestrator/scripts/review_report.py --report ./audit-report.json \
  --workspace .audit/<run-id> --formats json,md,html,pdf

# One stage on its own
python3 skills/url-validation/scripts/validate_url.py example.com
python3 skills/crawl-reachability/scripts/check_reachability.py --json
```

| Flag | Default | Meaning |
|---|---|---|
| `--max-pages` | 20 | Page budget for the whole run |
| `--max-seconds` | 150 | Detection budget: crawl + analysis stages |
| `--max-total-seconds` | 240 | Hard ceiling for the **whole** run, including compose, render and the review loop. Guarantees completion inside the 5-minute limit. |
| `--strictness` | `balanced` | `lenient` / `balanced` / `strict` — shifts derived thresholds only, never the safety gate |
| `--formats` | `json,md` | Any of `json`, `md`, `html`, `pdf` |
| `--skip` | — | Comma-separated layers to skip |
| `--strict-advisory` | off | Drop findings that would otherwise be demoted |
| `--max-review-passes` | 3 | Bounded repair loop: review passes before shipping the best draft |
| `--no-review-loop` | off | Skip the review loop (not recommended: the spec requires it) |

Exit codes: `0` report produced · `1` preflight refused the target · `2`
internal error.

---

## The review loop

After compose, an independent reviewer (`skills/audit-orchestrator/scripts/review_report.py`)
re-checks the draft and returns `VERDICT: ACCEPT` or `VERDICT: REGENERATE` with
numbered issues and concrete repairs. On `REGENERATE` the orchestrator applies
the repairs, re-renders, re-verifies, and re-reviews — bounded by
`--max-review-passes` (default 3), then ships the best draft with any residuals
recorded on stderr. Every pass leaves a trail in the workspace (`08-review.json`,
`09-review-summary.json`).

**What it checks:** schema errors mapped to repairs (stale summary counts,
duplicated ids, severity above the claim-level ceiling, invalid action
priority, Level-4 causal claims); **critic authority** (a finding reinstating a
critic-dropped `check_id`, or carrying a `check_id` found nowhere in the
advisory `reviewed_findings`, is removed — there is no path around the
critic); a second pass of the deterministic gates (evidence sufficiency,
threshold coherence, priority-vs-severity alignment); and artefact integrity
(missing, stub, or structurally broken formats demand re-render).

**What it may demand:** removal, demotion, rewording or re-rendering — never a
new finding. Unrepairable problems (e.g. a missing advisory trail) are reported,
not patched. Eight regression tests in `tests/test_marketplace.py`
(`TestReviewLoop`) pin every one of these behaviours.

---

## How thresholds are derived

A fixed threshold is a hidden assumption about the site being audited. Since
this is graded on **unseen sites**, baking in one site's shape is the fastest
way to fail. Thresholds are computed at runtime from three inputs:

1. **Paper anchors** — corpus-wide quartiles that don't depend on the site.
2. **Site self-calibration** — the site's own observed distribution. The thin
   threshold is `max(corpus bottom quartile 170, half this site's median)`,
   capped at 600. A site of 2,000-word guides gets a higher bar than one of
   200-word product pages, so "unusually thin *for this site*" stays detectable
   either way.
3. **Page-type expectations** — a legal page isn't expected to carry comparison
   content; a hub page's job is routing, not explaining.

Every derived threshold records **why** it holds its value, and that provenance
travels into the finding's evidence string:

```
thin_words=575 (site-calibrated: max(corpus bottom quartile 170,
half this site's median of 1150 across 6 pages), strictness=balanced)
```

Page types carrying `exempt_from_absorption` (legal) or `is_hub` (category) are
excluded from the relevant checks, and the exemption is recorded in the output
rather than applied silently.

---

## Tests

```bash
python3 tests/test_marketplace.py
```

146 tests, standard library only. Coverage:

- **25 SSRF attack vectors** — loopback by name and address, IPv4-mapped and
  6to4 IPv6, RFC1918, CGNAT, IPv6 ULA, AWS/GCP/Alibaba metadata endpoints,
  decimal/hex/octal IP obfuscation, `file:`/`gopher:`/`javascript:` schemes,
  embedded credentials, reserved suffixes, port allowlist — each asserted
  blocked, with legitimate URLs asserted accepted.
- **robots.txt semantics** — grouped agents, named-group precedence,
  longest-match, `$` anchoring, `*` wildcards, malformed-line recovery.
- **17 advisory-gate tests** — each gate is fed the exact defect it exists to
  catch, plus a control asserting the traps aren't a blanket off-switch.
- **8 review-loop tests** — clean drafts ACCEPT; stale summaries recompute;
  smuggled and reinstated findings are removed; the reviewer never invents a
  finding; priority contradictions cap; Level-4 findings remove rather than
  rewrite; every pass leaves its audit trail.
- **Claim-level enforcement** — caps, level-4 refusal, tampered-count detection.
- **Render-fault classification** — including that `state_blob` is preferred
  over `empty_shell` when the text is recoverable.
- **PDF validity** — xref offsets are re-parsed and asserted to point at their
  objects; Unicode and PDF delimiter escaping.
- **Chart degradation** — every chart on empty and all-zero data.
- **End-to-end** against a local fixture site with **planted defects and
  ground truth**, asserting both that the known defects are detected *and* that
  the exempt pages (legal, hub) are not falsely flagged.
- **Manifest hygiene** — exactly one entrypoint, every declared skill exists
  with valid frontmatter whose `name` matches its folder, no undeclared skill
  folders, and an AST walk asserting **no module imports anything outside the
  standard library**.
- **Cross-domain re-scoping** — a landing redirect onto another registrable
domain is followed and recorded, while the requested URL is never widened,
metadata targets stay blocked, and the address guard stays independent of the
scope guard.
- **HTTPS-only fallback** — a host that serves no HTTPS is audited over HTTP and
graded `critical`, and a plaintext host that merely does not *enforce* HTTPS is
still only `high`. The fallback preserves the port it was probing.
- **Redirect permanence** — a temporary (302) cross-domain move is graded and
fixed differently from a permanent (301) one.
- **Renderer output integrity** — the renderers must emit a whole document.
  Both accumulate output in a list joined at the end, so a shadowing local can
  publish a degenerate artefact with exit code 0 instead of failing; the tests
  pin document shape so that cannot recur silently.
- **Time-budget robustness** — three ways a run could produce nothing while
  reporting success: a dead address family consuming the whole request timeout
  before a working one was tried, unbounded sitemap discovery being killed by
  its parent, and a large `Crawl-delay` starving the crawl. Each is pinned, and
  each degradation is asserted to reach `limitations[]`.

---

## Safety

Recommend-only. **No skill in this marketplace modifies a live site.**

| Guarantee | How |
|---|---|
| Read-only | `GET` and `HEAD` only; never authenticates, never submits a form |
| Scope-confined | Never leaves the registrable domain, including across redirects |
| SSRF-hardened | Private, loopback, link-local, CGNAT, reserved and metadata space blocked; **every redirect hop revalidated** (DNS-rebinding aware) |
| robots.txt | Enforced per URL, not merely reported; crawl-delay honoured |
| Rate-limited | Per-host delay, bounded concurrency |
| Budgeted | Hard page, byte and wall-clock caps |
| No JS execution | Deliberate — it reproduces what a non-rendering crawler sees |
| Self-contained | No pip installs, no external services, no model weights |

The test suite includes a documented, test-only opt-in
(`GEO_AUDIT_ALLOW_PRIVATE_ADDRESSES`) so the local fixture on `127.0.0.1` can be
audited. It is never set in normal operation, `TestNetguard` verifies the guard
blocks everything without it, and **cloud metadata endpoints stay blocked even
with it set**.

---

## Layout

```
brand-ai-readiness-audit/
├── marketplace.json              manifest: 8 skills, one entrypoint
├── README.md
├── INSTALL.md                    install in any agent, 5 minutes
├── LICENSE                       MIT
├── lib/geo_audit/                shared library, stdlib only
│   ├── netguard.py               URL canonicalisation + SSRF defence
│   ├── robots.py                 robots.txt with per-agent group visibility
│   ├── fetcher.py                polite, budgeted, redirect-revalidating HTTP
│   ├── htmldoc.py                html.parser-based DOM-lite
│   ├── textstats.py              evidence genres, structure metrics
│   ├── influence.py              absorption-readiness proxy
│   ├── sitemapper.py             bounded, page-type-spread discovery
│   ├── sitemapaudit.py           deep sitemap.xml analysis
│   ├── renderfaults.py           JS render faults, without running JS
│   ├── config.py                 adaptive thresholds with provenance
│   ├── report.py                 finding model, severity, schema validation
│   ├── workspace.py              shared run directory
│   ├── pdfwrite.py               minimal PDF writer (no dependencies)
│   └── charts.py                 vector charts → SVG and PDF
├── skills/                       8 skill folders, each agentskills.io-valid
│   └── audit-orchestrator/scripts/
│       ├── run_audit.py          entrypoint: pipeline + review loop
│       ├── review_report.py      the review loop (ACCEPT / REGENERATE)
│       └── render_report.py      md / html / pdf renderer
├── examples/
│   └── sample-report-huggingface.(json|md|html|pdf)
└── tests/
    ├── fixture_site.py           flawed fixture + ground truth
    └── test_marketplace.py       146 tests
```

---

## Licence

MIT.
