# Install — Brand AI-Readiness Audit

5 minutes, no dependencies. Python 3.10+ standard library only — no `pip install`,
no external services, no model weights.

## 1. Unzip

```bash
unzip brand-ai-readiness-audit-v1.2.1.zip
cd brand-ai-readiness-audit
```

## 2. Verify (30 seconds)

```bash
python3 tests/test_marketplace.py
```

Expect `Ran 146 tests … OK`. This runs the whole stack against a local fixture
site, including the 8 review-loop tests. It needs no network: the fixture site
is served from loopback.

## 3. Run your first audit

```bash
python3 skills/audit-orchestrator/scripts/run_audit.py example.com \
  --formats json,md,html,pdf --out ./audit-report
```

You get `audit-report.json` (canonical), `.md` (drop into a PR or ticket),
`.html` (self-contained, inline SVG charts), and `.pdf` (vector charts, no
dependencies). The run ends with a review-loop verdict:

```
[8/8] compose + review (pass 1/3)  draft (9 findings)  VERDICT: ACCEPT (clean)
```

## 4. Use it as an agent skill

Every folder under `skills/` follows the Agent Skills format (`SKILL.md` with
YAML frontmatter, plus `scripts/` and `references/`). The eight skills share
`lib/geo_audit` and a workspace, so copy the complete marketplace tree and keep
its relative layout intact:

| Install target | What to copy |
|---|---|
| **Agent skill marketplace** | the whole `brand-ai-readiness-audit/` directory; invoke the designated `audit-orchestrator` entrypoint |

The zip also carries `examples/sample-report-huggingface.(json|md|html|pdf)` —
open the `.html` in a browser for what a finished report looks like (17
findings on huggingface.co: 0 critical, 1 high, 10 medium, 5 low, 1 info).

Invoke the entrypoint for anything shaped like *"audit this site for AI
visibility"*, *"why isn't this brand cited by ChatGPT"*, *"why do visitors
bounce"*, or *"give me a brand AI-readiness report"*.

## 5. Knobs you'll actually use

| Flag | Default | Meaning |
|---|---|---|
| `--max-pages` | 20 | Page budget for the whole run |
| `--max-seconds` | 150 | Detection budget: crawl + analysis stages |
| `--max-total-seconds` | 240 | Hard ceiling for the **whole** run, including compose, render and the review loop. Guarantees completion inside the 5-minute limit. |
| `--strictness` | `balanced` | `lenient` / `balanced` / `strict` — shifts derived thresholds only, never the safety gate |
| `--formats` | `json,md` | Any of `json`, `md`, `html`, `pdf` |
| `--skip` | — | Comma-separated layers to skip |
| `--strict-advisory` | off | Drop findings that would otherwise be demoted |
| `--max-review-passes` | 3 | Review-loop passes before shipping the best draft |
| `--no-review-loop` | off | Skip the loop (not recommended: the spec requires it) |

Exit codes: `0` report produced · `1` preflight refused the target · `2` internal error.

## 6. The review loop, in one paragraph

After compose, `scripts/review_report.py` re-checks the draft against the
report schema and the stage artefacts and returns `VERDICT: ACCEPT` or
`VERDICT: REGENERATE` with numbered issues and concrete repairs. On
`REGENERATE` the orchestrator applies the repairs, re-renders, re-verifies, and
re-reviews (bounded, default 3 passes). The reviewer may only demand removal,
demotion, rewording or re-rendering — never a new finding — and it cannot
reinstate anything the advisory critic dropped. Run it standalone over any
draft:

```bash
python3 skills/audit-orchestrator/scripts/review_report.py \
  --report ./audit-report.json --workspace .audit/<run-id> --formats json,md,html,pdf
```

## Safety

Recommend-only. No skill modifies a live site: read-only `GET`/`HEAD`,
unauthenticated, `robots.txt` respected, rate-limited, scope-confined to the
registrable domain, SSRF-hardened with every redirect hop revalidated, hard
page and time budgets, no JavaScript execution (deliberate — it reproduces what
a non-rendering crawler sees).
