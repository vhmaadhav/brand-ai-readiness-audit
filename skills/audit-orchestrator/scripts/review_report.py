#!/usr/bin/env python3
"""The review loop: an independent second pass over the composed draft report.

The orchestrator runs this after compose + render + artefact verification. The
reviewer returns VERDICT: ACCEPT or VERDICT: REGENERATE with numbered issues
and concrete repairs. On REGENERATE the orchestrator applies the repairs
(``--apply``), re-renders, re-verifies, and re-reviews — bounded by
``--max-review-passes``.

Authority discipline (the critic stays authoritative):

- The reviewer may only demand removal, demotion, rewording or re-rendering —
  never a new finding.
- The reviewer can never reinstate anything the advisory critic dropped. A
  draft finding whose ``check_id`` appears in the advisory ``dropped`` list, or
  which appears nowhere in the advisory ``reviewed_findings``, is itself
  removed as an attempt to route around the critic.

Exit codes: 0 ACCEPT (or --apply applied cleanly), 1 REGENERATE, 2 error.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

_here = pathlib.Path(__file__).resolve()
MARKETPLACE_ROOT = None
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        MARKETPLACE_ROOT = _parent
        sys.path.insert(0, str(_parent / "lib"))
        break
if MARKETPLACE_ROOT is None:  # pragma: no cover
    print("cannot locate marketplace lib/geo_audit", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, str(MARKETPLACE_ROOT / "skills" / "advisory-review" / "scripts"))

from geo_audit.report import cap_severity, summarise, validate_report  # noqa: E402
from geo_audit.workspace import Workspace  # noqa: E402
import advise as _advise  # noqa: E402  # deterministic gates, reused: same rules, second pass

_FIDX_RE = re.compile(r"findings\[(\d+)\]")
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEVERITY_TO_PRIORITY_CAP = {
    "critical": "critical", "high": "high", "medium": "medium",
    "low": "low", "info": "low",
}


# ---------------------------------------------------------------------------
# Issue collection
# ---------------------------------------------------------------------------

class Review:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self._fix_summary_queued = False
        self._fix_ids_queued = False

    def add(self, kind: str, detail: str, repair: dict | None = None,
            finding: str | None = None) -> None:
        self.issues.append({
            "n": len(self.issues) + 1,
            "kind": kind,
            "detail": detail,
            "finding": finding,
            "repair": repair,
        })

    def queue_fix_summary(self) -> None:
        if not self._fix_summary_queued:
            self._fix_summary_queued = True
            self.add("summary_mismatch",
                     "summary counts do not match the findings (recomputed).",
                     {"op": "fix_summary"})

    def queue_fix_ids(self) -> None:
        if not self._fix_ids_queued:
            self._fix_ids_queued = True
            self.add("finding_ids",
                     "finding ids are duplicated or malformed; reallocated in order.",
                     {"op": "fix_ids"})


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_schema(report: dict, review: Review) -> None:
    """Map schema-validation errors onto repairs the loop can apply."""
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        review.add("report_shape", "'findings' must be an array.", None)
        return
    for err in validate_report(report):
        match = _FIDX_RE.search(err)
        low = err.lower()
        if match is None:
            if "summary" in low or "total_findings" in low:
                review.queue_fix_summary()
            elif "'site'" in low:
                review.add("missing_site", err, {"op": "set_field", "field": "site",
                                                "value": "__FROM_WORKSPACE__"})
            elif "audited_at" in low:
                review.add("missing_audited_at", err, {"op": "set_field",
                                                      "field": "audited_at",
                                                      "value": "__NOW__"})
            else:
                review.add("report_shape", err, None)
            continue
        idx = int(match.group(1))
        if idx >= len(findings) or not isinstance(findings[idx], dict):
            review.add("report_shape", err, None)
            continue
        finding = findings[idx]
        fid = finding.get("id", f"findings[{idx}]")
        if "level 4" in low or "causal" in low:
            review.add("causal_claim_as_finding",
                        f"{fid}: {err} The critic gate forbids causal "
                        "prescriptions as findings; removal is the only honest "
                        "repair (the reviewer may not rewrite it into a hypothesis).",
                        {"op": "remove_finding", "index": idx}, fid)
        elif "ceiling" in low or "above the" in low:
            level = finding.get("claim_level", 3)
            capped = cap_severity(finding.get("severity", "medium"), level)
            review.add("severity_above_claim_ceiling", f"{fid}: {err}",
                       {"op": "cap_severity", "index": idx, "to": capped}, fid)
        elif "priority" in low:
            cap = _SEVERITY_TO_PRIORITY_CAP.get(finding.get("severity", "medium"), "medium")
            review.add("action_priority_invalid", f"{fid}: {err}",
                       {"op": "align_priority", "index": idx, "to": cap}, fid)
        elif "duplicate id" in low:
            review.queue_fix_ids()
        elif "evidence" in low or "suggested_action" in low or "title" in low:
            review.add("finding_missing_required_content",
                        f"{fid}: {err} The reviewer may not invent evidence or "
                        "action text, so an unfixable finding is removed.",
                        {"op": "remove_finding", "index": idx}, fid)
        else:
            review.add("report_shape", f"{fid}: {err}", None)


def check_critic_authority(report: dict, advisory: dict | None, review: Review) -> None:
    """No path around the critic: every finding must come from the advisory layer."""
    if not advisory:
        review.add("advisory_trail_missing",
                    "the draft carries no advisory block, so provenance cannot be "
                    "verified. The orchestrator refuses reports the critic did not "
                    "clear; this needs a pipeline re-run, not a repair.", None)
        return
    reviewed = advisory.get("reviewed_findings", [])
    reviewed_checks = {f.get("check_id") for f in reviewed if f.get("check_id")}
    reviewed_titles = {f.get("title") for f in reviewed if f.get("title")}
    dropped_checks = {d.get("check_id") for d in advisory.get("dropped", []) if d.get("check_id")}
    absorbed: set[str] = set()
    for finding in reviewed:
        absorbed.update(finding.get("consequences", []) or [])
    merged_titles = {m.get("kept") for m in advisory.get("merged", []) if m.get("kept")}

    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        fid = finding.get("id", f"findings[{idx}]")
        check = finding.get("check_id", "")
        title = finding.get("title", "")
        if check and check in dropped_checks:
            review.add("reinstates_dropped_finding",
                        f"{fid} carries check_id {check!r}, which the advisory "
                        "critic dropped. The reviewer cannot reinstate it; removed.",
                        {"op": "remove_finding", "index": idx}, fid)
        elif check and check not in reviewed_checks:
            review.add("finding_outside_advisory",
                        f"{fid} carries check_id {check!r}, which appears nowhere "
                        "in the advisory reviewed_findings. Findings reach the "
                        "report only through the critic; removed.",
                        {"op": "remove_finding", "index": idx}, fid)
        elif not check and title not in reviewed_titles and title not in absorbed \
                and title not in merged_titles:
            review.add("finding_outside_advisory",
                        f"{fid} matches nothing in the advisory reviewed_findings "
                        "by check_id or title. Unverifiable provenance; removed.",
                        {"op": "remove_finding", "index": idx}, fid)


def check_gates_second_pass(report: dict, review: Review) -> None:
    """Re-apply the deterministic advisory gates to the survivors."""
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        fid = finding.get("id", f"findings[{idx}]")
        ok, reason = _advise.gate_evidence_sufficiency(finding)
        if not ok:
            review.add("evidence_gate_regression",
                        f"{fid} no longer passes evidence sufficiency: {reason}",
                        {"op": "remove_finding", "index": idx}, fid)
            continue
        ok, reason = _advise.gate_threshold_coherence(finding)
        if not ok:
            review.add("threshold_gate_regression",
                        f"{fid} no longer passes threshold coherence: {reason}",
                        {"op": "remove_finding", "index": idx}, fid)
            continue
        # Priority may sit below severity but never above it.
        action = finding.get("suggested_action") or {}
        priority = action.get("priority", "medium")
        cap = _SEVERITY_TO_PRIORITY_CAP.get(finding.get("severity", "medium"), "medium")
        if _PRIORITY_RANK.get(priority, 2) < _PRIORITY_RANK.get(cap, 2):
            review.add("priority_above_severity",
                        f"{fid} asserts severity {finding.get('severity')!r} beside "
                        f"a {priority!r}-priority action — a contradiction; priority "
                        f"capped at {cap!r}.",
                        {"op": "align_priority", "index": idx, "to": cap}, fid)


def check_artefacts(report: dict, stem: pathlib.Path | None, formats: set[str],
                    review: Review) -> None:
    """A promised format is never silently absent or structurally broken."""
    if stem is None or not formats:
        return
    advisory = report.get("advisory") or {}
    suffix = {"json": ".json", "md": ".md", "html": ".html", "pdf": ".pdf"}
    for fmt in sorted(formats & set(suffix)):
        path = stem.with_suffix(suffix[fmt])
        if not path.is_file():
            review.add("artefact_missing", f"{fmt}: {path} was not written.",
                       {"op": "re_render"})
        elif path.stat().st_size < 500:
            review.add("artefact_stub",
                        f"{fmt}: {path} is only {path.stat().st_size} bytes.",
                        {"op": "re_render"})
    if "html" in formats:
        html_path = stem.with_suffix(".html")
        if html_path.is_file():
            html_text = html_path.read_text(encoding="utf-8", errors="replace")
            for token in ("</html>", report.get("site", "")):
                if token and token not in html_text:
                    review.add("artefact_broken",
                                f"html: {html_path} is missing {token!r}.",
                                {"op": "re_render"})
            for item in advisory.get("dropped") or []:
                title = str(item.get("title", ""))
                if title and title not in html_text:
                    review.add("artefact_broken",
                                f"html: dropped-finding title {title[:60]!r}… not "
                                "found verbatim.",
                                {"op": "re_render"})
    if "md" in formats:
        md_path = stem.with_suffix(".md")
        if md_path.is_file():
            md_text = md_path.read_text(encoding="utf-8", errors="replace")
            for item in advisory.get("dropped") or []:
                title = str(item.get("title", ""))
                if title and title not in md_text:
                    review.add("artefact_broken",
                                f"md: dropped-finding title {title[:60]!r}… not "
                                "found verbatim.",
                                {"op": "re_render"})
    if "pdf" in formats:
        pdf_path = stem.with_suffix(".pdf")
        if pdf_path.is_file():
            pdf_bytes = pdf_path.read_bytes()
            if not pdf_bytes.startswith(b"%PDF-"):
                review.add("artefact_broken",
                            f"pdf: {pdf_path} does not start with %PDF-.",
                            {"op": "re_render"})
            if not pdf_bytes.rstrip().endswith(b"%%EOF"):
                review.add("artefact_broken",
                            f"pdf: {pdf_path} is truncated (no %%EOF).",
                            {"op": "re_render"})


# ---------------------------------------------------------------------------
# Repair application (--apply)
# ---------------------------------------------------------------------------

def _resolve_placeholders(report: dict, workspace: Workspace | None) -> dict:
    values: dict = {}
    site = ""
    if workspace is not None:
        preflight = workspace.read("preflight") or {}
        site = (preflight.get("target") or {}).get("host", "")
    values["__FROM_WORKSPACE__"] = site
    values["__NOW__"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return values


def apply_repairs(report: dict, issues: list[dict],
                  workspace: Workspace | None) -> list[str]:
    """Apply JSON-level repairs. Returns a human-readable applied list.

    ``re_render`` is a signal for the orchestrator, not a JSON edit, and is
    skipped here. Order matters: fields, removals (by descending index, so
    earlier indices stay valid), per-finding edits, id reallocation, summary.
    """
    applied: list[str] = []
    placeholders = _resolve_placeholders(report, workspace)
    findings = report.get("findings")
    if not isinstance(findings, list):
        return applied

    removed: set[int] = set()
    for issue in issues:
        repair = issue.get("repair") or {}
        if repair.get("op") == "set_field":
            value = repair.get("value", "")
            value = placeholders.get(value, value)
            if value:
                report[repair["field"]] = value
                applied.append(f"set {repair['field']}={value!r} (issue #{issue['n']})")

    removals = sorted(
        ((r["index"], i["n"]) for i in issues
         for r in [i.get("repair") or {}]
         if r.get("op") == "remove_finding"
         and isinstance(r.get("index"), int) and 0 <= r["index"] < len(findings)),
        reverse=True,
    )
    for idx, n in removals:
        if idx in removed:
            continue
        gone = findings.pop(idx)
        removed.add(idx)
        applied.append(f"removed {gone.get('id', f'findings[{idx}]')} (issue #{n})")

    for issue in issues:
        repair = issue.get("repair") or {}
        idx = repair.get("index")
        if not isinstance(idx, int) or idx in removed or idx >= len(findings):
            continue
        finding = findings[idx]
        if not isinstance(finding, dict):
            continue
        if repair.get("op") == "cap_severity":
            finding["severity"] = repair["to"]
            applied.append(f"capped {finding.get('id')} severity to {repair['to']} (issue #{issue['n']})")
        elif repair.get("op") == "align_priority":
            action = finding.get("suggested_action") or {}
            action["priority"] = repair["to"]
            finding["suggested_action"] = action
            applied.append(f"aligned {finding.get('id')} priority to {repair['to']} (issue #{issue['n']})")

    if any((i.get("repair") or {}).get("op") == "fix_ids" for i in issues):
        for pos, finding in enumerate(findings, start=1):
            if isinstance(finding, dict):
                finding["id"] = f"F-{pos:03d}"
        for pos, rec in enumerate(report.get("proactive_recommendations") or [], start=1):
            if isinstance(rec, dict):
                rec["id"] = f"P-{pos:03d}"
        applied.append("reallocated finding ids in emission order")

    # Removals stale the summary even when no explicit fix_summary issue was
    # queued (the mismatch only appears after the removals apply), so any
    # removal forces a recompute too.
    if removals or any((i.get("repair") or {}).get("op") == "fix_summary" for i in issues):
        report["summary"] = summarise([
            _rehydrate_lite(f) for f in findings if isinstance(f, dict)
        ])
        applied.append("recomputed summary counts from findings")

    return applied


def _rehydrate_lite(raw: dict):
    """A summary needs only severities; reuse the real model for one truth."""
    from geo_audit.report import Finding, SuggestedAction  # noqa: E402
    action = raw.get("suggested_action") or {}
    return Finding(
        title=raw.get("title", "t"), severity=raw.get("severity", "medium"),
        evidence=raw.get("evidence", "e"),
        suggested_action=SuggestedAction(
            summary=action.get("summary", "s"),
            priority=action.get("priority", "medium")),
        layer=raw.get("layer", "selection"),
        claim_level=raw.get("claim_level", 1),
        confidence=raw.get("confidence", "high"),
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review-loop second pass over a draft audit report.")
    parser.add_argument("--report", required=True, help="Draft report JSON path.")
    parser.add_argument("--workspace", default=None, help="Audit run directory (for advisory + preflight).")
    parser.add_argument("--stem", default=None, help="Output path stem (for artefact checks).")
    parser.add_argument("--formats", default="", help="Comma-separated formats to verify.")
    parser.add_argument("--apply", action="store_true", help="Apply JSON-level repairs in place.")
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = parser.parse_args(argv)

    try:
        report_path = pathlib.Path(args.report)
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"cannot read draft report: {exc}", file=sys.stderr)
        return 2

    workspace: Workspace | None = None
    advisory: dict | None = None
    if args.workspace:
        try:
            workspace = Workspace.open(args.workspace)
            advisory = workspace.read("advisory")
        except Exception as exc:  # noqa: BLE001
            print(f"cannot open workspace: {exc}", file=sys.stderr)
            return 2
    else:
        advisory = report.get("advisory")

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    stem = pathlib.Path(args.stem) if args.stem else None

    review = Review()
    check_schema(report, review)
    check_critic_authority(report, advisory, review)
    check_gates_second_pass(report, review)
    check_artefacts(report, stem, formats, review)

    verdict = "ACCEPT" if not review.issues else "REGENERATE"
    repairable = sum(1 for i in review.issues if i.get("repair"))
    needs_rerender = any((i.get("repair") or {}).get("op") == "re_render"
                         for i in review.issues)

    applied: list[str] = []
    if args.apply and verdict == "REGENERATE":
        applied = apply_repairs(report, review.issues, workspace)
        try:
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"cannot write repaired report: {exc}", file=sys.stderr)
            return 2
        if needs_rerender:
            applied.append("re-render required (orchestrator re-runs render + verify)")
        if any(i.get("repair") is None for i in review.issues):
            verdict = "REGENERATE"
        else:
            verdict = "REPAIRED"

    payload = {
        "verdict": verdict,
        "report": str(report_path),
        "workspace": str(workspace.root) if workspace else None,
        "issues": review.issues,
        "repairable": repairable,
        "applied": applied,
    }
    if workspace is not None:
        try:
            workspace.write("review", {
                "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "verdict": verdict,
                "issues": review.issues,
                "repairs_applied": applied,
            })
        except Exception:  # noqa: BLE001
            pass  # the trail is nice-to-have; the verdict is what matters

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"REVIEW LOOP  {report_path}" + (f"  ({workspace.run_id})" if workspace else ""))
        for issue in review.issues:
            repair = issue["repair"]
            how = (f"{repair['op']}" if repair else "no automatic repair — pipeline re-run needed")
            print(f"  issue #{issue['n']} [{issue['kind']}] {issue['detail'][:130]}")
            print(f"      -> {how}")
        if args.apply:
            for line in applied:
                print(f"  applied: {line}")
        print(f"VERDICT: {verdict}"
              + (f" ({len(review.issues)} issue(s), {repairable} repairable)" if review.issues else ""))

    if args.apply:
        return 0 if verdict in ("ACCEPT", "REPAIRED") else 1
    return 0 if verdict == "ACCEPT" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
