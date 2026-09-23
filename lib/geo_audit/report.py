"""The finding model, severity discipline, and report assembly.

The load-bearing idea here is **claim-level discipline**, taken from the
identification map in arXiv:2604.25707 §5.5. The paper insists that a GEO
result state what kind of claim it is making, because the same dataset supports
confident counting and supports almost no causal prescription:

===== ============================ ================================
Level Kind                         Example
===== ============================ ================================
1     Direct observation           "0 of 12 product pages carry JSON-LD"
2     Descriptive contrast         "product pages are thinner than blog pages"
3     Mechanism interpretation     "thin pages give an answer engine little to extract"
4     Causal prescription          "adding definitions will increase citations"
===== ============================ ================================

An audit that emits Level 4 statements as findings is selling correlation as
causation. This module makes that structurally impossible: every finding
carries a claim level, severity is capped by it, and Level 4 cannot be a
finding at all — it may only appear as a hypothesis in the proactive section.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

SEVERITIES = ("critical", "high", "medium", "low", "info")
PRIORITIES = ("critical", "high", "medium", "low")
LAYERS = (
    "preflight", "reachability", "selection", "absorption",
    "trust", "engagement",
)

# The cap that enforces the identification map.
CLAIM_LEVEL_MAX_SEVERITY = {
    1: "critical",  # direct observation may be stated flatly
    2: "high",      # descriptive contrast is strong but comparative
    3: "medium",    # mechanism interpretation must stay hedged
    4: None,        # causal prescription is never a finding
}

_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}


def cap_severity(severity: str, claim_level: int) -> str:
    """Lower *severity* to whatever *claim_level* can support."""
    ceiling = CLAIM_LEVEL_MAX_SEVERITY.get(claim_level)
    if ceiling is None:
        return "info"
    if _SEVERITY_RANK[severity] < _SEVERITY_RANK[ceiling]:
        return ceiling
    return severity


@dataclass
class SuggestedAction:
    """What to change and how. ``summary`` and ``priority`` are the required floor."""

    summary: str
    priority: str = "medium"
    steps: list[str] = field(default_factory=list)
    effort: str = "medium"          # low | medium | high
    mechanism: str = ""             # why this works, in pipeline terms
    verification: str = ""          # how to confirm the fix landed

    def to_dict(self) -> dict:
        out = {"summary": self.summary, "priority": self.priority}
        if self.steps:
            out["steps"] = self.steps
        if self.effort:
            out["effort"] = self.effort
        if self.mechanism:
            out["mechanism"] = self.mechanism
        if self.verification:
            out["verification"] = self.verification
        return out


@dataclass
class Finding:
    """One evidence-backed problem."""

    title: str
    severity: str
    evidence: str
    suggested_action: SuggestedAction
    layer: str = "selection"
    claim_level: int = 1
    confidence: str = "high"        # high | medium | low
    check_id: str = ""              # stable identifier for dedupe
    observed: str = ""
    expected: str = ""
    affected_urls: list[str] = field(default_factory=list)
    sample_size: int = 0
    id: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"invalid severity {self.severity!r}")
        if self.layer not in LAYERS:
            raise ValueError(f"invalid layer {self.layer!r}")
        if self.claim_level not in CLAIM_LEVEL_MAX_SEVERITY:
            raise ValueError(f"invalid claim_level {self.claim_level!r}")
        if self.suggested_action.priority not in PRIORITIES:
            raise ValueError(f"invalid priority {self.suggested_action.priority!r}")
        self.severity = cap_severity(self.severity, self.claim_level)

    def to_dict(self) -> dict:
        out = {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action.to_dict(),
            "layer": self.layer,
            "claim_level": self.claim_level,
            "confidence": self.confidence,
        }
        if self.check_id:
            out["check_id"] = self.check_id
        if self.observed:
            out["observed"] = self.observed
        if self.expected:
            out["expected"] = self.expected
        if self.affected_urls:
            out["affected_urls"] = self.affected_urls[:12]
        if self.sample_size:
            out["sample_size"] = self.sample_size
        return out


@dataclass
class ProactiveRecommendation:
    """An improvement that strengthens the brand where no defect was found.

    This is where Level 4 material is allowed to live, phrased as a hypothesis
    with its evidentiary basis named.
    """

    title: str
    rationale: str
    action: str
    priority: str = "medium"
    basis: str = ""
    claim_level: int = 3
    id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "rationale": self.rationale,
            "action": self.action,
            "priority": self.priority,
            "basis": self.basis,
            "claim_level": self.claim_level,
        }


def allocate_ids(findings: list[Finding]) -> None:
    """Assign stable F-NNN ids in emission order."""
    for index, finding in enumerate(findings, start=1):
        finding.id = f"F-{index:03d}"


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Severity first, then pipeline order, then confidence.

    Pipeline order matters: a reachability defect upstream can be the cause of
    an absorption symptom downstream, so it should be read first.
    """
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_RANK[f.severity],
            LAYERS.index(f.layer),
            confidence_rank.get(f.confidence, 3),
            f.title,
        ),
    )


def summarise(findings: list[Finding]) -> dict:
    """Counts by severity. The required keys are always present, even at zero."""
    summary = {"total_findings": len(findings)}
    for name in SEVERITIES:
        summary[name] = sum(1 for f in findings if f.severity == name)
    return summary


def _marketplace_version() -> str:
    """Read the version from the marketplace manifest.

    Single source of truth. The version used to be hardcoded here, which meant
    a released manifest and the reports it produced could disagree -- exactly
    the kind of drift a reader comparing the two artefacts would trip over.

    This file lives at ``<marketplace root>/lib/geo_audit/report.py``, so three
    levels up from the module file is the manifest.
    """
    manifest = pathlib.Path(__file__).resolve().parents[2] / "marketplace.json"
    try:
        with open(manifest, encoding="utf-8") as fh:
            version = json.load(fh).get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except Exception:  # noqa: BLE001 - a missing manifest must not break a report
        pass
    return "unknown"


def build_report(
    *,
    site: str,
    findings: list[Finding],
    proactive: list[ProactiveRecommendation] | None = None,
    scope: dict | None = None,
    scores: dict | None = None,
    advisory: dict | None = None,
    limitations: list[str] | None = None,
) -> dict:
    """Assemble the final audit report."""
    ordered = sort_findings(findings)
    allocate_ids(ordered)

    proactive = proactive or []
    for index, rec in enumerate(proactive, start=1):
        rec.id = f"P-{index:03d}"

    report = {
        "site": site,
        "audited_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summarise(ordered),
        "findings": [f.to_dict() for f in ordered],
    }
    if scope:
        report["scope"] = scope
    if scores:
        report["scores"] = scores
    if proactive:
        report["proactive_recommendations"] = [r.to_dict() for r in proactive]
    if advisory:
        report["advisory"] = advisory
    if limitations:
        report["limitations"] = limitations
    report["generator"] = {
        "marketplace": "brand-ai-readiness-audit",
        "version": _marketplace_version(),
        "grounding": (
            "Zhang K., He X., Yao J. (2026). From Citation Selection to Citation "
            "Absorption. arXiv:2604.25707v2."
        ),
    }
    return report


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def validate_report(report: dict) -> list[str]:
    """Check the report against the required schema. Returns a list of errors.

    Hand-rolled rather than pulling in ``jsonschema``, because the marketplace
    must run with the standard library alone.
    """
    errors: list[str] = []

    for key in ("site", "audited_at", "summary", "findings"):
        if key not in report:
            errors.append(f"missing required top-level key {key!r}")

    if not isinstance(report.get("site"), str) or not report.get("site"):
        errors.append("'site' must be a non-empty string")

    audited_at = report.get("audited_at", "")
    if not isinstance(audited_at, str) or not _ISO_RE.match(audited_at):
        errors.append("'audited_at' must be an ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("'summary' must be an object")
    else:
        for key in ("total_findings", "critical", "high", "medium"):
            if key not in summary:
                errors.append(f"summary is missing required key {key!r}")
            elif not isinstance(summary[key], int):
                errors.append(f"summary.{key} must be an integer")

    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("'findings' must be an array")
        return errors

    if isinstance(summary, dict) and summary.get("total_findings") != len(findings):
        errors.append(
            f"summary.total_findings ({summary.get('total_findings')}) does not "
            f"match len(findings) ({len(findings)})"
        )

    seen_ids: set[str] = set()
    for index, finding in enumerate(findings):
        where = f"findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{where} must be an object")
            continue
        for key in ("id", "title", "severity", "evidence", "suggested_action"):
            if key not in finding:
                errors.append(f"{where} is missing required key {key!r}")
        fid = finding.get("id")
        if fid in seen_ids:
            errors.append(f"{where} has duplicate id {fid!r}")
        seen_ids.add(fid)
        if finding.get("severity") not in SEVERITIES:
            errors.append(f"{where}.severity {finding.get('severity')!r} is not one of {SEVERITIES}")
        if not str(finding.get("evidence", "")).strip():
            errors.append(f"{where}.evidence must be a non-empty string")

        action = finding.get("suggested_action")
        if not isinstance(action, dict):
            errors.append(f"{where}.suggested_action must be an object")
        else:
            if not str(action.get("summary", "")).strip():
                errors.append(f"{where}.suggested_action.summary must be non-empty")
            if action.get("priority") not in PRIORITIES:
                errors.append(
                    f"{where}.suggested_action.priority {action.get('priority')!r} "
                    f"is not one of {PRIORITIES}"
                )

        level = finding.get("claim_level")
        if level is not None:
            if level not in CLAIM_LEVEL_MAX_SEVERITY:
                errors.append(f"{where}.claim_level {level!r} is invalid")
            elif level == 4:
                errors.append(
                    f"{where} is a Level 4 causal claim and must not be emitted "
                    "as a finding; move it to proactive_recommendations"
                )
            else:
                ceiling = CLAIM_LEVEL_MAX_SEVERITY[level]
                if _SEVERITY_RANK.get(finding.get("severity"), 99) < _SEVERITY_RANK[ceiling]:
                    errors.append(
                        f"{where} claims level {level} but asserts severity "
                        f"{finding.get('severity')!r}, above the {ceiling!r} ceiling"
                    )

    for name in SEVERITIES:
        if isinstance(summary, dict) and name in summary:
            actual = sum(1 for f in findings if isinstance(f, dict) and f.get("severity") == name)
            if summary[name] != actual:
                errors.append(f"summary.{name} ({summary[name]}) does not match findings ({actual})")

    return errors


def dump(report: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
