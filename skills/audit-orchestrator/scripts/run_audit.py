#!/usr/bin/env python3
"""Marketplace entrypoint: run the full pipeline and emit one audit report.

Exit codes: 0 report produced, 1 preflight refused, 2 internal error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

_here = pathlib.Path(__file__).resolve()
MARKETPLACE_ROOT = None
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        MARKETPLACE_ROOT = _parent
        sys.path.insert(0, str(_parent / "lib"))
        break

from geo_audit.report import (  # noqa: E402
    Finding,
    ProactiveRecommendation,
    SuggestedAction,
    build_report,
    dump,
    validate_report,
)
from geo_audit.workspace import Workspace  # noqa: E402

# ---------------------------------------------------------------------------
# Time budget
# ---------------------------------------------------------------------------
#
# The marketplace documents a 5-minute ceiling, but --max-seconds only ever
# governed the detection stages. Everything after them -- the advisory critic,
# rendering, and the review/repair loop -- ran unbudgeted, so a slow site could
# take three times the advertised limit. One deadline now covers the whole run
# and every subprocess timeout is derived from what is left of it.

# Hard ceiling for the entire process, comfortably inside the documented five
# minutes. Anything still running when this expires is cut short in a way that
# still produces a report.
DEFAULT_TOTAL_SECONDS = 240.0

# Detection budget (crawl + analysis). The remainder pays for compose, render
# and review, which are CPU-bound and finish in seconds on a normal site.
DEFAULT_DETECTION_SECONDS = 150.0

# Per-subprocess ceilings, each additionally clamped to the time remaining.
RENDER_CAP = 45.0
REVIEW_CAP = 45.0

# A review pass costs a render plus a review. Below this the loop stops and
# ships the draft it already has rather than starting a pass it cannot finish.
REVIEW_PASS_RESERVE = 30.0

# Detection and the advisory critic must hand over with at least this much left,
# so the first compose/render/review pass -- the one that actually produces the
# report, and therefore cannot be skipped -- always fits inside the ceiling
# without relying on its floors.
FINALISE_RESERVE = 50.0

# Rendering is the one step that must never be skipped: without it there is no
# report at all. It gets this floor even if the deadline has already passed.
RENDER_FLOOR = 20.0


class Deadline:
    """One wall-clock ceiling for the whole run.

    Every timeout in the pipeline is ``deadline.slice(cap)`` rather than a bare
    constant, so no stage can spend time the run as a whole does not have.
    """

    def __init__(self, total_seconds: float) -> None:
        self.total = float(total_seconds)
        self.started = time.monotonic()
        # The reserves are a *share* of the budget, not fixed seconds. A flat
        # 50s reserve is right at the 240s default and absurd at 60s, where it
        # would starve detection of every second it has.
        self.finalise_reserve = min(FINALISE_RESERVE, self.total * 0.25)
        self.pass_reserve = min(REVIEW_PASS_RESERVE, self.total * 0.125)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed())

    def slice(self, cap: float, floor: float = 5.0) -> float:
        """The time to allow one subprocess: *cap*, trimmed to what is left."""
        return max(floor, min(cap, self.remaining()))

    def has(self, needed: float) -> bool:
        return self.remaining() >= needed


# (workspace layer, skill folder, script) in pipeline order.
STAGES = [
    ("reachability", "crawl-reachability", "check_reachability.py"),
    ("selection", "structured-data-identity", "check_structured_data.py"),
    ("absorption", "evidence-density", "score_evidence.py"),
    ("trust", "freshness-corroboration", "check_freshness.py"),
    ("engagement", "engagement-audit", "check_engagement.py"),
]


def run_stage(skill: str, script: str, workspace: Workspace, extra: list[str], timeout: float) -> dict:
    """Run one stage as a subprocess so a crash cannot take down the pipeline."""
    path = MARKETPLACE_ROOT / "skills" / skill / "scripts" / script
    if not path.is_file():
        return {"ok": False, "skill": skill, "reason": f"script not found at {path}"}

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [sys.executable, str(path), "--workspace", str(workspace.root), *extra],
            capture_output=True, text=True, timeout=max(10.0, timeout),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "skill": skill, "reason": f"timed out after {timeout:.0f}s"}

    return {
        "ok": completed.returncode == 0,
        "skill": skill,
        "returncode": completed.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "stdout": completed.stdout.strip(),
        "reason": completed.stderr.strip()[-400:] if completed.returncode else "",
    }


def rehydrate(raw: dict) -> Finding:
    """Rebuild a Finding from the advisory layer's reviewed dict."""
    action = raw.get("suggested_action") or {}
    return Finding(
        title=raw["title"],
        severity=raw["severity"],
        evidence=raw["evidence"],
        suggested_action=SuggestedAction(
            summary=action.get("summary", ""),
            priority=action.get("priority", "medium"),
            steps=action.get("steps", []),
            effort=action.get("effort", "medium"),
            mechanism=action.get("mechanism", ""),
            verification=action.get("verification", ""),
        ),
        layer=raw.get("layer", "selection"),
        claim_level=raw.get("claim_level", 1),
        confidence=raw.get("confidence", "high"),
        check_id=raw.get("check_id", ""),
        observed=raw.get("observed", ""),
        expected=raw.get("expected", ""),
        affected_urls=raw.get("affected_urls", []),
        sample_size=raw.get("sample_size", 0),
    )


def collect_scores(workspace: Workspace) -> dict:
    """Site-level scores for the report header and the charts."""
    scores: dict = {}
    absorption = workspace.read("absorption")
    if absorption:
        scores["absorption"] = absorption.get("site_absorption", {})
        scores["evidence_genres"] = absorption.get("genre_matrix", {})
        # Kept out of evidence_genres on purpose: that chart plots the paper's
        # measured influence uplift, and the survey-graded genres have no
        # comparable effect size to plot. Mixing them would imply the same
        # evidence backs both.
        scores["survey_genres"] = absorption.get("survey_genre_matrix", {})
    reachability = workspace.read("reachability")
    if reachability:
        render = reachability.get("render", {})
        scores["reachability"] = {
            "pages_analysed": render.get("pages_analysed", 0),
            "pages_with_blocking_faults": render.get("pages_with_blocking_faults", 0),
            "blocking_rate": render.get("blocking_rate", 0),
            "sitemap_exists": (reachability.get("sitemap") or {}).get("exists", False),
            "sitemap_problems": len((reachability.get("sitemap") or {}).get("problems", [])),
        }
        scores["word_counts"] = [
            p.get("structure", {}).get("word_count", 0)
            for p in reachability.get("pages", []) if p.get("structure")
        ]
    return scores


def discovery_limitations(reachability: dict) -> list[str]:
    """Limitations the discovery walk recorded about its own sample.

    Each note describes a way the sample is narrower than "the site": the
    candidate pool was cut short, or politeness forced a smaller sample. Both
    change what the report can be read as being about, so both are stated
    rather than left implicit in the page counts. The notes are written by the
    walk itself, so this only translates them into reader-facing sentences.
    """
    limitations: list[str] = []
    for note in (reachability.get("discovery") or {}).get("notes", []):
        if "stopped at its time limit" in note:
            limitations.append(
                "Sitemap discovery stopped at its time limit on a large sitemap, "
                "so the candidate pool is a partial sample and the site may "
                "publish pages this audit never considered."
            )
        elif "crawl delay" in note:
            limitations.append(note)
    return limitations


def compose_report(advisory: dict, preflight_payload: dict,
                   reachability: dict, scores: dict, limitations: list[str],
                   site_fallback: str, started: float) -> dict:
    """Assemble the report object from the advisory layer's reviewed findings.

    Pure composition (no rendering, no verification), so the review loop can
    re-run it after repairs without re-crawling anything.
    """
    # Findings come from the advisory layer, never from the detectors directly.
    findings = [rehydrate(f) for f in advisory.get("reviewed_findings", [])]
    proactive = [
        ProactiveRecommendation(
            title=r["title"], rationale=r["rationale"], action=r["action"],
            priority=r.get("priority", "medium"), basis=r.get("basis", ""),
            claim_level=r.get("claim_level", 3),
        )
        for r in advisory.get("proactive_recommendations", [])
    ]

    target = preflight_payload.get("target", {})
    return build_report(
        site=target.get("host") or site_fallback,
        findings=findings,
        proactive=proactive,
        scope={
            "target_url": target.get("url"),
            "registrable_domain": target.get("registrable_domain"),
            "rehomed_from": preflight_payload.get("rescoped_from") or None,
            "pages_discovered": (reachability.get("discovery") or {}).get("candidates", 0),
            "pages_analysed": reachability.get("pages_analysed", 0),
            "page_types": (reachability.get("discovery") or {}).get("page_type_spread", {}),
            "robots_respected": True,
            "javascript_executed": False,
            "runtime_seconds": round(time.monotonic() - started, 1),
        },
        scores=scores,
        advisory={
            "findings_in": advisory["input_findings"],
            "findings_out": advisory["output_findings"],
            "rejection_rate": advisory["verdict"]["rejection_rate"],
            "dropped": advisory["dropped"],
            "downgraded": advisory["downgraded"],
            "merged": advisory["merged"],
            "policy": advisory["verdict"]["notes"],
        },
        limitations=limitations,
    )


def render_formats(report: dict, stem: pathlib.Path, formats: set[str],
                   deadline: "Deadline") -> tuple[list[str], str]:
    """Write the requested formats from one composed object. Returns (written, error).

    Rendering keeps a floor even past the deadline: a late report beats no
    report, and this step is measured in seconds on any input.
    """
    written: list[str] = []
    if "json" in formats:
        dump(report, str(stem.with_suffix(".json")))
        written.append(str(stem.with_suffix(".json")))

    if formats & {"md", "html", "pdf"}:
        render_script = _here.parent / "render_report.py"
        render_timeout = deadline.slice(RENDER_CAP, floor=RENDER_FLOOR)
        try:
            rendered = subprocess.run(
                [sys.executable, str(render_script),
                 "--report", str(stem.with_suffix(".json")) if "json" in formats else "-",
                 "--formats", ",".join(sorted(formats & {"md", "html", "pdf"})),
                 "--out", str(stem)],
                capture_output=True, text=True,
                input=json.dumps(report) if "json" not in formats else None,
                timeout=render_timeout,
            )
        except subprocess.TimeoutExpired:
            # Returning the error lets the caller record a limitation and still
            # ship the JSON, instead of taking the whole run down with it.
            return written, (f"rendering timed out after {render_timeout:.0f}s; "
                             f"the machine is slower than this budget assumes")
        except OSError as exc:
            return written, f"rendering could not start: {exc}"
        if rendered.returncode == 0:
            written.extend(line for line in rendered.stdout.split() if line)
        else:
            return written, f"rendering failed: {rendered.stderr.strip()[:300]}"
    return written, ""


def verify_artefacts(report: dict, stem: pathlib.Path, formats: set[str],
                     advisory: dict) -> list[str]:
    """Check every promised artefact exists and is structurally sound.

    A claimed artefact that is missing, empty or broken is a pipeline failure,
    not a cosmetic detail. Returns a list of error strings (empty = clean).
    """
    render_errors: list[str] = []
    expected_suffix = {"json": ".json", "md": ".md", "html": ".html", "pdf": ".pdf"}
    for fmt in sorted(formats & set(expected_suffix)):
        path = stem.with_suffix(expected_suffix[fmt])
        if not path.is_file():
            render_errors.append(f"{fmt}: {path} was not written")
        elif path.stat().st_size < 500:
            render_errors.append(
                f"{fmt}: {path} is only {path.stat().st_size} bytes; refusing a stub artefact")
    if "html" in formats:
        html_path = stem.with_suffix(".html")
        if html_path.is_file():
            html_text = html_path.read_text(encoding="utf-8", errors="replace")
            for token in ("</html>", report["site"]):
                if token not in html_text:
                    render_errors.append(f"html: {html_path} is missing {token!r}")
            for item in (advisory.get("dropped") or []):
                # Advisory titles must survive rendering whole, never sliced
                # mid-word by a width limit.
                title = str(item.get("title", ""))
                if title and title not in html_text:
                    render_errors.append(
                        f"html: dropped-finding title {title[:60]!r}… not found verbatim")
    if "md" in formats:
        md_path = stem.with_suffix(".md")
        if md_path.is_file():
            md_text = md_path.read_text(encoding="utf-8", errors="replace")
            for item in (advisory.get("dropped") or []):
                title = str(item.get("title", ""))
                if title and title not in md_text:
                    render_errors.append(
                        f"md: dropped-finding title {title[:60]!r}… not found verbatim")
    if "pdf" in formats:
        pdf_path = stem.with_suffix(".pdf")
        if pdf_path.is_file():
            pdf_bytes = pdf_path.read_bytes()
            if not pdf_bytes.startswith(b"%PDF-"):
                render_errors.append(f"pdf: {pdf_path} does not start with %PDF-")
            if not pdf_bytes.rstrip().endswith(b"%%EOF"):
                render_errors.append(f"pdf: {pdf_path} is truncated (no %%EOF)")
    return render_errors


def review_once(report_path: pathlib.Path, workspace: Workspace, stem: pathlib.Path,
                formats: set[str], apply: bool, deadline: "Deadline") -> dict:
    """Run one pass of the review loop. Returns the review payload (with verdict)."""
    review_script = _here.parent / "review_report.py"
    cmd = [sys.executable, str(review_script),
           "--report", str(report_path),
           "--workspace", str(workspace.root),
           "--stem", str(stem),
           "--formats", ",".join(sorted(formats)),
           "--json"]
    if apply:
        cmd.append("--apply")
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=deadline.slice(REVIEW_CAP),
        )
    except subprocess.TimeoutExpired:
        # A reviewer that runs out of clock is not a failure of the report --
        # the draft stands on its own. Report it as an unreviewed pass.
        return {"verdict": "TIMEOUT", "issues": [], "applied": [],
                "_returncode": None, "_stderr": "review timed out"}
    try:
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    payload["_returncode"] = completed.returncode
    payload["_stderr"] = completed.stderr.strip()[-300:] if completed.returncode not in (0, 1) else ""
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Brand AI-readiness audit: discoverability + engagement."
    )
    parser.add_argument("url", help="Target site: bare domain or full URL.")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-seconds", type=float, default=DEFAULT_DETECTION_SECONDS,
                        help="Detection budget: crawl and analysis stages. "
                             f"Default {DEFAULT_DETECTION_SECONDS:.0f}.")
    parser.add_argument("--max-total-seconds", type=float, default=None,
                        help="Hard ceiling for the WHOLE run, including compose, "
                             "render and the review loop. Default "
                             f"{DEFAULT_TOTAL_SECONDS:.0f} (inside the documented "
                             "5-minute limit); raised automatically if "
                             "--max-seconds is set higher.")
    parser.add_argument("--strictness", choices=["lenient", "balanced", "strict"], default="balanced")
    parser.add_argument("--formats", default="json,md",
                        help="Comma-separated: json, md, html, pdf.")
    parser.add_argument("--out", default="./audit-report", help="Output path stem.")
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--skip", default="", help="Comma-separated layers to skip.")
    parser.add_argument("--strict-advisory", action="store_true",
                        help="Advisory drops findings it would otherwise downgrade.")
    parser.add_argument("--max-review-passes", type=int, default=3,
                        help="Bounded repair loop: how many review passes (1 initial + "
                             "re-reviews after repairs) before shipping the best draft.")
    parser.add_argument("--no-review-loop", action="store_true",
                        help="Skip the review loop and ship the first verified draft. "
                             "Not recommended: the spec requires the loop.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # One clock for the entire run. If the operator widened the detection
    # budget past the default ceiling, the ceiling moves with it rather than
    # silently truncating the crawl they asked for.
    total_budget = args.max_total_seconds
    if total_budget is None:
        total_budget = max(DEFAULT_TOTAL_SECONDS,
                           args.max_seconds + (DEFAULT_TOTAL_SECONDS
                                               - DEFAULT_DETECTION_SECONDS))

    deadline = Deadline(total_budget)
    started = deadline.started
    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    log = (lambda *a, **k: None) if args.quiet else print

    # -- stage 1: preflight (hard gate) --------------------------------
    log(f"[1/8] url-validation      {args.url}")
    preflight_script = MARKETPLACE_ROOT / "skills" / "url-validation" / "scripts" / "validate_url.py"
    preflight_cmd = [
        sys.executable, str(preflight_script), args.url,
        "--max-pages", str(args.max_pages),
        "--max-seconds", str(args.max_seconds),
        "--strictness", args.strictness,
    ]
    if args.workspace:
        preflight_cmd += ["--workspace", args.workspace]

    # The preflight is the safety gate, so a timeout here has to fail closed.
    # A subprocess.TimeoutExpired escaping this call aborted the whole run with
    # exit 2 (internal error) and no report at all, on nothing worse than a slow
    # DNS or TLS handshake -- and it did so intermittently, which is the worst
    # kind of failure to debug. Treat "could not finish checking" as "did not
    # clear the check" and refuse, like any other preflight failure.
    preflight_timeout = deadline.slice(max(30.0, args.max_seconds / 3))
    try:
        preflight = subprocess.run(preflight_cmd, capture_output=True, text=True,
                                   timeout=preflight_timeout)
    except subprocess.TimeoutExpired:
        print(f"REFUSED: preflight did not finish within {preflight_timeout:.0f}s "
              f"(transport was too slow to clear the safety gate)", file=sys.stderr)
        print("\nPreflight refused the target. No audit was performed.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"REFUSED: could not run the preflight ({exc})", file=sys.stderr)
        print("\nPreflight refused the target. No audit was performed.", file=sys.stderr)
        return 1

    if preflight.returncode != 0:
        print(preflight.stdout or "", end="")
        print(preflight.stderr.strip(), file=sys.stderr)
        print("\nPreflight refused the target. No audit was performed.", file=sys.stderr)
        return 1
    if not args.quiet:
        print("      " + "\n      ".join(preflight.stdout.strip().splitlines()))

    # --workspace names the *base* directory; url-validation creates a
    # timestamped run directory inside it, which is what the stages operate on.
    workspace = Workspace.latest(args.workspace)

    # -- stages 2-6: detection -----------------------------------------
    limitations: list[str] = []
    stage_log: list[dict] = []

    for index, (layer, skill, script) in enumerate(STAGES, start=2):
        if layer in skip:
            limitations.append(f"The {skill} stage was skipped by request; {layer}-layer checks were not run.")
            continue
        # Whichever runs out first -- the detection budget or the run's own
        # ceiling -- ends the detection phase.
        remaining = min(args.max_seconds - (time.monotonic() - started),
                        deadline.remaining() - deadline.finalise_reserve)
        if remaining < 15:
            limitations.append(
                f"The {skill} stage was not run because the time budget was exhausted; "
                f"{layer}-layer checks are absent from this report."
            )
            continue

        log(f"[{index}/8] {skill:22s}", end=" ")
        outcome = run_stage(skill, script, workspace, [], min(remaining, args.max_seconds))
        stage_log.append(outcome)
        if outcome["ok"]:
            first = (outcome.get("stdout") or "").splitlines()
            log(f"ok ({outcome.get('seconds', 0)}s)"
                + (f"  {first[-1].strip()}" if first and "findings" in first[-1] else ""))
        else:
            log(f"FAILED: {outcome['reason'][:80]}")
            limitations.append(
                f"The {skill} stage failed ({outcome['reason'][:160]}); {layer}-layer "
                "checks are absent from this report."
            )

    # -- stage 7: advisory critic --------------------------------------
    log("[7/8] advisory-review     ", end=" ")
    advisory_extra = ["--strict"] if args.strict_advisory else []
    advisory_outcome = run_stage(
        "advisory-review", "advise.py", workspace, advisory_extra,
        # Previously max(20, ...), which could only ever *extend* past the
        # budget. The critic is CPU-bound and quick; cap it at what is left.
        max(10.0, min(40.0, deadline.remaining() - deadline.finalise_reserve)),
    )
    stage_log.append(advisory_outcome)

    advisory = workspace.read("advisory")
    if not advisory_outcome["ok"] or not advisory:
        log(f"FAILED: {advisory_outcome['reason'][:80]}")
        print(
            "\nThe advisory critic did not complete. Refusing to emit a report that "
            "has not been verified — unreviewed findings are exactly what this "
            "marketplace is designed not to publish.",
            file=sys.stderr,
        )
        return 2
    log(f"ok ({advisory_outcome.get('seconds', 0)}s)  "
        f"{advisory['input_findings']} in -> {advisory['output_findings']} out, "
        f"{len(advisory['dropped'])} dropped")

    # -- stage 8: compose ----------------------------------------------
    # Composition stays pure (no rendering) so the review loop below can
    # re-run it after repairs without re-crawling anything.
    preflight_payload = workspace.read("preflight") or {}
    reachability = workspace.read("reachability") or {}
    target_registrable = (
        (preflight_payload.get("target") or {}).get("registrable_domain") or args.url
    )

    limitations.extend([
        "The audit fetches raw HTML and does not execute JavaScript. This is "
        "deliberate — it reproduces what a non-rendering crawler sees — but means "
        "client-rendered content is diagnosed by inference rather than observation.",
        "No live AI assistant was queried. Findings describe page properties the "
        "cited research associates with citation outcomes; they do not measure "
        "whether this brand is currently cited.",
        f"{reachability.get('pages_analysed', 0)} page(s) were analysed out of "
        f"{(reachability.get('discovery') or {}).get('candidates', 0)} discovered. "
        "Findings describe the sample, not necessarily every page on the site.",
    ])

    # A candidate pool that was cut short is a partial view of the site's size,
    # not just of its pages, so it is disclosed rather than left implicit in the
    # page counts.
    limitations.extend(discovery_limitations(reachability))

    # A re-anchored scope changes what the report is *about*, so it must be
    # stated in the report itself and not only in the preflight artefact.
    rehomed_from = preflight_payload.get("rescoped_from")
    if rehomed_from:
        limitations.append(
            f"The requested domain {rehomed_from} redirected to "
            f"{target_registrable}, so the audit re-anchored to "
            f"{target_registrable} and reports on that entity. Findings describe "
            f"{target_registrable}; they do not describe {rehomed_from} as a "
            "separate site."
        )

    stem = pathlib.Path(args.out)
    stem.parent.mkdir(parents=True, exist_ok=True)
    scores = collect_scores(workspace)
    review_trail: list[dict] = []
    written: list[str] = []
    report: dict = {}
    max_passes = max(1, args.max_review_passes)

    for review_pass in range(1, max_passes + 1):
        # A pass costs a render plus a review. Starting one we cannot finish
        # would blow the ceiling to buy nothing, so stop and ship the draft
        # that already passed schema validation and artefact verification.
        if review_pass > 1 and not deadline.has(deadline.pass_reserve):
            log(f"      review loop stopped after pass {review_pass - 1}: "
                f"{deadline.remaining():.0f}s left of the "
                f"{deadline.total:.0f}s budget")
            limitations.append(
                f"The review loop stopped after pass {review_pass - 1} of "
                f"{max_passes} because the {deadline.total:.0f}s run budget was "
                "nearly spent; the report shipped is the last verified draft."
            )
            break

        # -- compose --------------------------------------------------
        log(f"[8/8] compose + review (pass {review_pass}/{max_passes})", end=" ")
        report = compose_report(advisory, preflight_payload, reachability, scores,
                                limitations, args.url, started)
        errors = validate_report(report)
        if errors:
            print("\nThe composed report failed schema validation:", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            return 2

        # -- render + verify ------------------------------------------
        written, render_error = render_formats(report, stem, formats, deadline)
        if render_error:
            print(f"\n{render_error}", file=sys.stderr)
            return 2
        render_errors = verify_artefacts(report, stem, formats, advisory)
        if render_errors:
            for error in render_errors:
                print(f"artefact verification failed: {error}", file=sys.stderr)
            return 2
        if "json" not in formats:
            # The reviewer reads the draft JSON; with no JSON format requested,
            # stage it in the workspace (never in the user's output dir).
            draft_path = workspace.root / "08-review-draft.json"
            draft_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
        else:
            draft_path = stem.with_suffix(".json")
        log(f"draft ({len(report['findings'])} findings)", end="  ")

        if args.no_review_loop:
            log("review skipped (--no-review-loop)")
            break

        # -- review ---------------------------------------------------
        # The reviewer's repairs apply in place (--apply); JSON-level repairs
        # land in the draft, re_render repairs are satisfied by the next pass's
        # render. The critic stays authoritative: the reviewer can never
        # reinstate a dropped finding (enforced in review_report.py).
        review = review_once(draft_path, workspace, stem, formats,
                             apply=True, deadline=deadline)
        verdict = review.get("verdict", "REGENERATE")
        issues = review.get("issues", [])
        review_trail.append({"pass": review_pass, "verdict": verdict,
                             "issues": len(issues),
                             "applied": review.get("applied", [])})
        report = json.loads(draft_path.read_text(encoding="utf-8"))
        if not args.quiet:
            for line in review.get("applied", []):
                print(f"\n      repair: {line}", end="")

        # The reviewer ran out of clock. The draft is already schema-valid
        # and artefact-verified, so it ships as-is rather than looping into
        # a pass there is no time for.
        if verdict == "TIMEOUT":
            log(("" if args.quiet else "\n      ")
                + f"VERDICT: TIMEOUT (reviewer exceeded its slice of the "
                  f"{deadline.total:.0f}s budget) - shipping the verified draft")
            limitations.append(
                "The review pass timed out inside the "
                f"{deadline.total:.0f}s run budget; the report shipped is the "
                "last draft that passed schema validation and artefact "
                "verification, but it was not critic-reviewed."
            )
            break

        if verdict in ("ACCEPT", "REPAIRED"):
            log(("" if args.quiet else "\n      ")
                + f"VERDICT: {verdict}"
                + (f" ({len(issues)} issue(s) repaired)" if issues else " (clean)"))
            # Repairs changed the JSON, so re-render + re-verify once more from
            # the repaired object, then confirm with a read-only final check.
            if review.get("applied"):
                # Re-rendering is not optional: the repairs live in the JSON
                # and have to reach the Markdown/HTML/PDF the user reads.
                written, render_error = render_formats(report, stem, formats, deadline)
                if render_error:
                    print(f"\n{render_error}", file=sys.stderr)
                    return 2
                render_errors = verify_artefacts(report, stem, formats, advisory)
                if render_errors:
                    for error in render_errors:
                        print(f"artefact verification failed: {error}", file=sys.stderr)
                    return 2

                # The read-only confirmation pass *is* optional -- the report is
                # already rendered and verified. Skip it rather than overrun.
                if not deadline.has(deadline.pass_reserve):
                    log(f"      final re-review skipped: "
                        f"{deadline.remaining():.0f}s left of the "
                        f"{deadline.total:.0f}s budget")
                    limitations.append(
                        "The final read-only re-review was skipped because the "
                        f"{deadline.total:.0f}s run budget was nearly spent; the "
                        "report was still rendered and artefact-verified."
                    )
                    break

                final = review_once(draft_path, workspace, stem, formats,
                                    apply=False, deadline=deadline)
                review_trail.append({"pass": review_pass,
                                     "verdict": final.get("verdict", "?"),
                                     "issues": len(final.get("issues", [])),
                                     "applied": [], "final_check": True})
                if not args.quiet:
                    print(f"      final re-review: VERDICT: {final.get('verdict', '?')}"
                          + ("" if final.get("verdict") == "ACCEPT"
                             else f" — {len(final.get('issues', []))} residual issue(s) recorded"))
                    for issue in final.get("issues", [])[:5]:
                        print(f"        issue #{issue.get('n')} [{issue.get('kind')}] "
                              f"{str(issue.get('detail', ''))[:110]}")
            break

        # VERDICT: REGENERATE with unrepairable issues — the repairs that
        # *could* apply already did; loop so artefact-level repairs get
        # re-rendered and re-verified.
        log(f"VERDICT: REGENERATE ({len(issues)} issue(s)) — repairing and re-rendering")
        if review_pass == max_passes:
            print("\nReview loop exhausted its passes with residual issues:", file=sys.stderr)
            for issue in issues[:8]:
                print(f"  - issue #{issue.get('n')} [{issue.get('kind')}] "
                      f"{str(issue.get('detail', ''))[:140]}", file=sys.stderr)
            print("Shipping the best draft; residuals are recorded above. "
                  "Re-run with --max-review-passes N to allow more passes.",
                  file=sys.stderr)

    try:
        workspace.write("review_summary", {
            "passes": len(review_trail),
            "trail": review_trail,
            "skipped": bool(args.no_review_loop),
        })
    except Exception:  # noqa: BLE001
        pass  # the trail is nice-to-have; the verdict is what matters

    summary = report["summary"]
    if not args.quiet:
        print()
        print("=" * 68)
        print(f"  {report['site']}  —  {summary['total_findings']} findings "
              f"in {report['scope']['runtime_seconds']}s")
        print(f"  critical {summary['critical']}   high {summary['high']}   "
              f"medium {summary['medium']}   low {summary['low']}   info {summary['info']}")
        print(f"  advisory dropped {len(advisory['dropped'])}, "
              f"downgraded {len(advisory['downgraded'])}, merged {len(advisory['merged'])}")
        if review_trail:
            last = review_trail[-1]
            print(f"  review loop: {len(review_trail)} pass(es), final VERDICT: {last['verdict']}")
        print("=" * 68)
        for finding in report["findings"][:10]:
            print(f"  [{finding['severity']:8s}] {finding['title']}")
            print(f"             -> {finding['suggested_action']['summary']}")
        if len(report["findings"]) > 10:
            print(f"  ... and {len(report['findings']) - 10} more")
        if report.get("proactive_recommendations"):
            print(f"\n  {len(report['proactive_recommendations'])} proactive recommendation(s):")
            for rec in report["proactive_recommendations"]:
                print(f"    - {rec['title']}")
        print()
        for path in written:
            print(f"  wrote {path}")
        print(f"  workspace {workspace.root}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
