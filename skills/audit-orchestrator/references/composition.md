# How the entrypoint composes the other skills

## Ordering is causal, not cosmetic

The pipeline runs in gate order because the gates genuinely depend on one
another:

```
url-validation  ->  crawl-reachability  ->  +-- structured-data-identity --+
   (preflight)        (reachability)        |   evidence-density           |
                                            |   freshness-corroboration    |
                                            +-- engagement-audit ----------+
                                                          |
                                                  advisory-review
                                                          |
                                                  audit-orchestrator
```

Running out of order produces findings that are artefacts of the missing
upstream stage. A page that returned 404 will look like it has no structured
data, no evidence density and no engagement affordances — three findings that
say nothing about the site and everything about the pipeline.

Stages 3 through 6 are mutually independent. They all read the same page
inventory, none writes what another reads, so a failure in one leaves the
others valid.

## Failure isolation

Each stage runs as a subprocess. A stage that crashes, hangs or exceeds the
remaining time budget is recorded in `limitations` and the run continues.

The rationale: a partial audit that says what it could not check is more useful
than no audit. The exception is `advisory-review` — if the critic does not
complete, the orchestrator **refuses to emit a report at all**. Unverified
findings are precisely what this marketplace exists not to publish, so there is
no degraded mode that skips verification.

## The single composition rule

> The orchestrator takes findings from the advisory layer, never from the
> detectors.

There is no path around the critic. A detector's output reaches the report only
after passing all five gates. This is what makes the advisory agent an actual
gate rather than a commentator.

## What the orchestrator contributes

It runs **no checks of its own**. Its contributions are:

- **Sequencing** and shared budget enforcement across every stage.
- **Failure isolation** with documented gaps.
- **Global prioritisation** — severity, then pipeline order, then confidence.
  Pipeline order is the tiebreaker so an upstream cause is read before its
  downstream symptoms.
- **Schema validation** before emission; an invalid report is not written.
- **Rendering** to JSON, Markdown, HTML and PDF from one composed object.
- **Artefact verification** after rendering: every requested format must exist
  on disk, exceed a stub size, and pass a structural check (HTML contains
  `</html>`, the site name and every advisory title verbatim; PDF starts with
  `%PDF-` and ends with `%%EOF`). A failed check fails the run with exit 2.
- **Agent review loop** after compose (`scripts/review_report.py`): an
  independent reviewer re-checks the draft report against the schema, the
  advisory provenance (every finding must trace to the critic's
  `reviewed_findings`; a reinstated drop or an unreviewed finding is removed),
  the deterministic gates (evidence sufficiency, threshold coherence,
  priority-vs-severity alignment), and the rendered artefacts, and returns
  ACCEPT or REGENERATE with numbered issues and concrete repairs. Repairs
  apply in place (`--apply`), artefacts re-render and re-verify, and the loop
  re-reviews — bounded by `--max-review-passes` (default 3), then the best
  draft ships with residuals recorded. The reviewer may only demand removal,
  demotion, rewording or re-rendering — never a new finding — and it cannot
  reinstate anything the critic dropped. Each pass leaves a trail in the
  workspace (`08-review.json`, `09-review-summary.json`).

## Why not one skill

A single skill doing all of this would be a 3,000-line file with six unrelated
reasons to change, and the advisory gate would be a function call inside the
thing it is supposed to audit rather than a separate stage with its own
artefact. The decomposition also means each stage can be run and debugged
alone: `check_reachability.py --json` answers "can a crawler read this site"
without any of the rest of the pipeline existing.

The split is by *failure gate*, which is the axis along which the fixes differ.
Splitting by page type or by check count would have produced skills that all
recommend the same things.
