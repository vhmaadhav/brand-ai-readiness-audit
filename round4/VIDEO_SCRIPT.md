# Round 4 — Video Script (≤ 5:00)

Part 1 is a **silent 3:00 video with burned-in subtitles**: `round4/part1-methodology.mp4`
(1920x1080, 24 fps), rendered frame-exact from `round4/methodology-video.html` by
`python round4/render_video.py` (`--nosubs` for a clean cut; soft subtitles in
`round4/part1-methodology.srt`). A presenter talks over it live; the lines
below are their **talking points**, one scene at a time. Every claim names the
file and line that implements it, and the same anchor is on screen.

Part 2 is a **live Claude Code session** (2:00 after trimming idle waits).

> Integrity note: nothing in `round4/` is imported by the engine. The engine
> shown is the unchanged Round 3 tree (`skills/`, `lib/`, `marketplace.json` v1.2.1).

---

## PART 1 — Methodology (0:00 – 3:00)

The presenter can read the subtitles as written or paraphrase them. Every
scene shows its source file in the top-left corner.

| # | Time | On screen | Presenter says | Code anchor |
|---|---|---|---|---|
| 1 | 0:00–0:12 | Title, then the eight skill names | "This is Brand AI-Readiness Audit, a skill marketplace for AI agents. Give it a URL, and it explains why AI assistants don't cite that brand, and what to fix first." | `marketplace.json` (`entrypoint`) |
| 2 | 0:12–0:34 | **Architecture diagram** draws itself: Claude Code → audit-orchestrator → url-validation → crawl-reachability → 4 parallel detectors → advisory-review → Report, with the review loop back. Dots flow through it. | "Claude Code loads one entrypoint skill. It checks the URL is safe to audit, crawls the site once into a shared page inventory, and four detectors read it independently. Every finding passes a critic before it's composed into one report." | `run_audit.py:112` (`run_stage`), `README.md:34` |
| 3 | 0:34–0:54 | **Gate flow**: 5 gates with a fix branch under each; a page stops at Selection and that fix lights up | "We treat AI visibility as a series of gates, not a single score. Each gate has its own fix. A page stopped at selection needs an entity declaration, not more content." | `README.md:34`, `check_structured_data.py:812` |
| 4 | 0:54–1:12 | **Research → fact → check** flow: arXiv 2604.25707 fans out to 4 measured facts, each wired to the check that implements it | "Every check traces back to measured data: 602 prompts, 21,143 citations. Only 76% of cited pages could be fetched. Each fact maps to a specific check." | `renderfaults.py:160`, `config.py:142`, `advise.py:413` |
| 5 | 1:12–1:28 | Evidence-type bars count up; the FAQ bar goes negative in red | "Code, numbers, definitions and comparisons shape answers more. FAQ formatting is the one pattern that measured negative, so we never recommend it, and a test enforces that." | `advise.py:413`, `tests/test_marketplace.py:959` |
| 6 | 1:28–1:40 | Word-count scale: floor at 170 vs top quartile at 1,943; 2 of 8 pages flagged | "Thresholds sit at the bottom quartile, a floor to clear. That keeps the audit precise." | `config.py:142–150` |
| 7 | 1:40–2:04 | **Severity decision flow**: Finding → Claim level? → 4 ceilings; then 3 enforcement points → Report | "Severity says how much it costs; claim level says how sure we are, and caps severity. A causal promise is never a finding. The cap is checked three times." | `report.py:101`, `advise.py:191`, `report.py:269` |
| 8 | 2:04–2:30 | **Critic flow**: findings travel past 5 gates; two drop out, one is downgraded, two reach the report; three symptoms merge into one root cause | "Detectors are optimistic, so every finding has to get past a critic: a real URL, a real breach, enough pages. Weak ones are dropped with the reason logged. Symptoms of one cause are merged." | `advise.py:78, 92, 126, 191, 225, 293` |
| 9 | 2:30–2:50 | Finding card → Suggested action card → prioritised list that sorts itself | "Each finding becomes an action with steps, effort, mechanism and a verification test. Sorted by severity, then pipeline order, so root causes come first." | `report.py:170` (`sort_findings`) |
| 10 | 2:50–3:00 | "Now, live." | "Now let's run it live, with Claude Code and Claude Opus 5.5, on a site it hasn't seen." | — |

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
