# Round 4 — Video Script (≤ 5:00)

Part 1 is a **silent, auto-playing visual** (`round4/methodology-video.html`,
3:00, screen-record it full-screen). A presenter talks over it live; the lines
below are their **talking points**, one scene at a time. Every claim names the
file and line that implements it, and the same anchor is on screen.

Part 2 is a **live Claude Code session** (2:00 after trimming idle waits).

> Integrity note: nothing in `round4/` is imported by the engine. The engine
> shown is the unchanged Round 3 tree (`skills/`, `lib/`, `marketplace.json` v1.2.1).

---

## PART 1 — Methodology (0:00 – 3:00)

| # | Time | On screen (auto) | Presenter says | Code anchor |
|---|---|---|---|---|
| 1 | 0:00–0:12 | Title: *Brand AI-Readiness Audit* | "We audit why AI assistants don't cite a brand, and why visitors who arrive don't stay. Eight skills, one entrypoint, Python stdlib only." | `marketplace.json` (`"entrypoint": "audit-orchestrator"`) |
| 2 | 0:12–0:32 | Pipeline builds gate by gate: preflight → reachability → selection → absorption → trust → engagement → advisory → compose | "Our core insight: GEO is a staged pipeline, not a ranking. A page must clear every gate in order, and *the gate it fails decides the fix*. One 'AI score' throws that away." | `README.md:34`, `skills/audit-orchestrator/scripts/run_audit.py:112` (`run_stage`) |
| 3 | 0:32–0:52 | "Field research": 602 prompts · 21,143 citations · 3 engines. Stat: **76.44 % fetch success** | "Signals come from measured data — arXiv 2604.25707. One in four pages an engine *already chose to cite* couldn't be fetched. So reachability is a gate, not hygiene: sitemap audit plus per-type JavaScript render-fault diagnosis." | `lib/geo_audit/renderfaults.py:160` (`diagnose`), fault codes `empty_shell`, `noscript_gap` (`:232`, `:330`) |
| 4 | 0:52–1:10 | Selection ≠ absorption table: ChatGPT 0.2713 vs Perplexity 0.0646 influence | "Being cited isn't mattering. Perplexity cites 2.4× more sources but absorbs each a quarter as deeply. So we split selection (structured data, entity identity, sameAs) from absorption (evidence density). They emit different fixes." | `skills/structured-data-identity/scripts/check_structured_data.py:812` (sameAs finding), `lib/geo_audit/influence.py:56` (weights) |
| 5 | 1:10–1:30 | Genre bars: code +77 %, numbers +62 %, definitions +57 %… **FAQ −5.74 % in red** | "The non-obvious one: 'add an FAQ' is standard GEO advice, and the only genre measured *negative*. We never recommend it — there is a regression test for that." | `skills/advisory-review/scripts/advise.py:413`, `tests/test_marketplace.py:959` |
| 6 | 1:30–1:45 | Quartile floor: 169.82 words / 0.85 headings (bottom) vs 1,943 / 10.6 (top) | "Thresholds sit at the *bottom* quartile — a floor to clear. Flag everything below the top and you fire on most of the web and lose precision." | `lib/geo_audit/config.py:142–150` |
| 7 | 1:45–2:10 | Two axes: **Severity** (how much it costs) × **Claim level** (how much we know). Ceiling: L1→critical, L2→high, L3→medium, L4→never a finding | "Severity: critical means the brand can't enter the candidate pool. Claim level caps it: a mechanism guess can never be critical, a causal promise is never a finding. Enforced three times: construction, advisory, schema validation." | `skills/audit-orchestrator/references/severity-model.md`, `lib/geo_audit/report.py:101` (`__post_init__` caps), `lib/geo_audit/report.py:269` (`validate_report`) |
| 8 | 2:10–2:35 | Advisory critic: 5 gates filter a stream of findings — evidence, threshold, sample, claim level, false-positive traps; merge symptoms → root cause | "Detectors are optimistic. Every finding must survive a critic that can *drop and downgrade*: needs a real URL and observed value, must actually breach the threshold, needs enough sample. JS-rendered page → three symptoms, merged into one fix. No path around it." | `skills/advisory-review/scripts/advise.py:78, 92, 126, 191, 225, 293` |
| 9 | 2:35–2:52 | Anatomy of a suggested action: summary · steps · effort · **mechanism** · verification · priority. Sort key shown | "Each fix names its mechanism and how to verify it. Priority ≠ severity: a one-line medium fix can go first. Findings sort by severity, then pipeline order — upstream causes read first. We prefer *add the missing fact* over *rewrite the page*: rewrites measurably backfired." | `lib/geo_audit/report.py:170` (`sort_findings`), `severity-model.md` "Priority is not severity" |
| 10 | 2:52–3:00 | "Now live →" | "Now live, on a site it has never seen." | — |

---

## PART 2 — Live trial run (3:00 – 5:00)

Record continuously. Trim only idle waits. Do not cut between typing the URL and the findings.

| Time | Action on screen | Presenter says |
|---|---|---|
| 3:00–3:12 | Terminal: `cd brand-ai-readiness-audit` → `git log --oneline -1` (shows `a6f9325`) → `claude` | "Harness: **Claude Code 2.1.132**. Model: **Claude Opus 5.5** (`claude-opus-5-5`). This is our Round 3 commit, unchanged." |
| 3:12–3:25 | Type the prompt (see `REPLAY_TEAMNAME.txt` step 2) with the **unseen URL** | "Entering the URL on camera." |
| 3:25–3:45 | Agent reads `skills/audit-orchestrator/SKILL.md`, runs `run_audit.py`; stage progress `[1/8]…[8/8]` streams | "Wiring: the agent is invoking our entrypoint script — you can see the command it runs. No hardcoded demo." *(trim waits)* |
| 3:45–4:05 | Summary block: counts by severity, top findings | "N findings. Sorted by severity then pipeline layer." |
| 4:05–4:35 | Ask agent: *"Show the evidence for the top finding: observed vs expected, affected URLs, claim level, and the fix."* Then `curl -s <affected URL> \| head` or open the JSON | **Drill-down**: "Here's the raw evidence — e.g. JSON-LD missing / no sitemap / blocked AI crawler — with observed vs expected and the URL. Claim level 1, so critical is allowed." |
| 4:35–4:50 | Ask: *"List suggested actions sorted by priority, with effort."* | "Fixes are prioritised by impact; low-effort high-impact first." |
| 4:50–5:00 | Show `advisory` block (dropped/merged) | "And here's what the critic removed and why. Replay steps are in our REPLAY file." |

### Pre-flight checklist (do before recording)
- Pick 2–3 candidate URLs and dry-run each with the fallback command; keep the
  best, but **record a fresh run** on it. Avoid sites behind aggressive bot
  protection (Cloudflare challenge) — they return no bytes to a stdlib client
  and produce reachability-only findings. Check with `curl -sI <url>`.
- Put the chosen URL(s) in `REPLAY_TEAMNAME.txt` **before** submitting; rename
  the file with your team name.
- `claude --version` on camera must match the REPLAY file.
