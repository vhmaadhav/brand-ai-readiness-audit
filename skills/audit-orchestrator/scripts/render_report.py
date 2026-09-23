#!/usr/bin/env python3
"""Render a composed audit report to Markdown, HTML and PDF.

Reads the report JSON from a path or stdin. Prints the paths it wrote.
Exit codes: 0 success, 2 internal error.
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import pathlib
import sys

_here = pathlib.Path(__file__).resolve()
for _parent in _here.parents:
    if (_parent / "lib" / "geo_audit" / "__init__.py").is_file():
        sys.path.insert(0, str(_parent / "lib"))
        break

from geo_audit import charts  # noqa: E402
from geo_audit.pdfwrite import PdfDocument  # noqa: E402

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
SEVERITY_COLOR = {
    "critical": "#B4232C", "high": "#D9722A", "medium": "#C79A1E",
    "low": "#3E7CB1", "info": "#7A7A7A",
}


def build_charts(report: dict) -> dict:
    """The chart set, derived from the report alone."""
    summary = report.get("summary", {})
    findings = report.get("findings", [])
    scores = report.get("scores", {})

    severity_counts = {s: summary.get(s, 0) for s in SEVERITY_ORDER}
    layer_counts: dict[str, int] = {}
    for finding in findings:
        layer = finding.get("layer", "selection")
        layer_counts[layer] = layer_counts.get(layer, 0) + 1

    genre_matrix = scores.get("evidence_genres") or {}
    present = {k: bool(v) for k, v in genre_matrix.items()} if genre_matrix else None

    # Genres the survey grades rather than the paper measuring them. Reported as
    # coverage counts on their own line, because there is no effect size to plot
    # beside the paper's uplift bars.

    absorption = (scores.get("absorption") or {}).get("median")

    return {
        "severity": charts.severity_bar(severity_counts),
        "layers": charts.layer_bar(layer_counts),
        "mix": charts.severity_donut(severity_counts),
        "genres": charts.evidence_genre_chart(present),
        "absorption": charts.absorption_gauge(absorption),
        "lengths": charts.word_count_distribution(scores.get("word_counts") or []),
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    summary = report.get("summary", {})
    scope = report.get("scope", {})
    advisory = report.get("advisory", {})
    survey_genres = report.get("scores", {}).get("survey_genres") or {}
    out: list[str] = []
    add = out.append

    add(f"# Brand AI-Readiness Audit — {report['site']}\n")
    add(f"*Audited {report['audited_at']}*")
    if scope.get("runtime_seconds") is not None:
        add(f" · {scope['pages_analysed']} of {scope.get('pages_discovered', 0)} "
            f"discovered pages analysed in {scope['runtime_seconds']}s\n")
    add("")

    counts = " · ".join(
        f"**{summary.get(s, 0)}** {s}" for s in SEVERITY_ORDER if summary.get(s, 0)
    ) or "no findings"
    add(f"**{summary.get('total_findings', 0)} findings** — {counts}\n")

    if survey_genres:
        surveyed = sum(1 for v in survey_genres.values() if v)
        add(
            f"**Extractable facts** — {surveyed} of {len(survey_genres)} survey-graded "
            "genre(s) present (prices and visible dates; arXiv:2607.14035 §7.2, "
            "graded `Moderate` in its §7.5 lever table).\n"
        )
    if advisory:
        add(
            f"> The advisory critic reviewed {advisory.get('findings_in', 0)} candidate "
            f"findings and emitted {advisory.get('findings_out', 0)}: "
            f"{len(advisory.get('dropped', []))} dropped, "
            f"{len(advisory.get('downgraded', []))} downgraded, "
            f"{len(advisory.get('merged', []))} merged into a root cause.\n"
        )

    add("\n## Findings\n")
    if not report.get("findings"):
        add("_No defects were detected in the audited sample._\n")

    for finding in report.get("findings", []):
        action = finding.get("suggested_action", {})
        add(f"### {finding['id']} · {finding['title']}\n")
        add(f"**Severity:** `{finding['severity']}` · "
            f"**Layer:** `{finding.get('layer', '-')}` · "
            f"**Claim level:** {finding.get('claim_level', '-')} · "
            f"**Confidence:** {finding.get('confidence', '-')}\n")
        add(f"**Evidence.** {finding['evidence']}\n")
        if finding.get("observed"):
            add(f"- Observed: {finding['observed']}")
        if finding.get("expected"):
            add(f"- Expected: {finding['expected']}")
        if finding.get("affected_urls"):
            shown = finding["affected_urls"][:5]
            add(f"- Affected: {', '.join(shown)}"
                + (f" (+{len(finding['affected_urls']) - len(shown)} more)"
                   if len(finding["affected_urls"]) > len(shown) else ""))
        if finding.get("consequences"):
            add(f"- Also explains: {'; '.join(finding['consequences'])}")
        add("")
        add(f"**Suggested action** (`{action.get('priority', 'medium')}` priority, "
            f"{action.get('effort', 'medium')} effort). {action.get('summary', '')}\n")
        for step in action.get("steps", []):
            add(f"1. {step}")
        if action.get("mechanism"):
            add(f"\n*Why this works.* {action['mechanism']}")
        if action.get("verification"):
            add(f"\n*Verify.* {action['verification']}")
        add("\n---\n")

    if report.get("proactive_recommendations"):
        add("\n## Proactive recommendations\n")
        add("_Improvements where no specific defect was found._\n")
        for rec in report["proactive_recommendations"]:
            add(f"### {rec['id']} · {rec['title']}\n")
            add(f"**Priority:** `{rec.get('priority', 'medium')}` · "
                f"**Claim level:** {rec.get('claim_level', 3)}\n")
            add(f"{rec['rationale']}\n")
            add(f"**Action.** {rec['action']}\n")
            if rec.get("basis"):
                add(f"*Basis:* {rec['basis']}\n")

    if report.get("limitations"):
        add("\n## Limitations\n")
        for item in report["limitations"]:
            add(f"- {item}")

    if advisory.get("dropped"):
        add("\n## Advisory audit trail\n")
        add("Candidate findings the critic removed, and why:\n")
        add("| Finding | Gate | Reason |")
        add("|---|---|---|")
        for item in advisory["dropped"]:
            # Full titles and reasons: truncating mid-word garbles the audit
            # trail in a published artefact (agent-review 2026-09-09). Tables
            # wrap; they must never slice.
            title = str(item.get("title", "")).replace("|", "\\|").replace("\n", " ")
            reason = str(item.get("reason", "")).replace("|", "\\|").replace("\n", " ")
            add(f"| {title} | `{item.get('gate', '')}` | {reason} |")

    generator = report.get("generator", {})
    add(f"\n---\n\n*Generated by {generator.get('marketplace', 'brand-ai-readiness-audit')} "
        f"v{generator.get('version', '1.0.0')}. Grounding: {generator.get('grounding', '')}*")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def render_html(report: dict, chart_set: dict) -> str:
    e = htmlmod.escape
    summary = report.get("summary", {})
    scope = report.get("scope", {})
    advisory = report.get("advisory", {})
    survey_genres = report.get("scores", {}).get("survey_genres") or {}

    parts: list[str] = []
    add = parts.append

    add("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Brand AI-Readiness Audit — """ + e(report["site"]) + """</title>
<style>
:root{--ink:#1a1a1a;--muted:#5f5f5f;--line:#e0ddd8;--bg:#fbfaf8;--card:#fff;
--critical:#B4232C;--high:#D9722A;--medium:#C79A1E;--low:#3E7CB1;--info:#7A7A7A;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:30px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:21px;margin:44px 0 14px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:0 0 10px}
.sub{color:var(--muted);font-size:14px;margin:0 0 24px}
.pills{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 8px}
.pill{padding:5px 12px;border-radius:999px;color:#fff;font-size:12.5px;font-weight:600}
.note{background:#f3f1ec;border-left:3px solid #b9b3a7;padding:12px 16px;
margin:18px 0;font-size:14px;color:#3d3a35;border-radius:0 4px 4px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:20px 22px;margin:16px 0}
.card.critical{border-left:4px solid var(--critical)}
.card.high{border-left:4px solid var(--high)}
.card.medium{border-left:4px solid var(--medium)}
.card.low{border-left:4px solid var(--low)}
.card.info{border-left:4px solid var(--info)}
.meta{font-size:12.5px;color:var(--muted);margin:0 0 12px}
.meta code{background:#f0eeea;padding:2px 6px;border-radius:3px;font-size:12px}
.ev{background:#f7f6f3;border-radius:5px;padding:12px 14px;margin:10px 0;font-size:14px}
.act{border-top:1px dashed var(--line);margin-top:14px;padding-top:14px}
.act ol{margin:8px 0 0;padding-left:22px}.act li{margin:5px 0}
.why{font-size:13.5px;color:var(--muted);margin-top:10px;font-style:italic}
ul.urls{margin:8px 0;padding-left:20px;font-size:13px;color:var(--muted);
word-break:break-all}
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px;margin:16px 0;overflow-x:auto}
.chart svg{display:block;max-width:100%;height:auto}
table{border-collapse:collapse;width:100%;font-size:13px;margin:12px 0}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
vertical-align:top}
th{background:#f3f1ec;font-weight:600}
footer{margin-top:56px;padding-top:18px;border-top:1px solid var(--line);
font-size:12.5px;color:var(--muted)}
@media(prefers-color-scheme:dark){
:root{--ink:#e8e6e3;--muted:#a09c96;--line:#3a3735;--bg:#1b1a19;--card:#242322}
.note{background:#2a2826;color:#d4d0cb;border-left-color:#5a554e}
.ev{background:#2a2827}.meta code{background:#332f2d}
th{background:#2c2a28}}
</style></head><body><div class="wrap">""")

    add("<h1>Brand AI-Readiness Audit</h1>")
    add(f'<p class="sub">{e(report["site"])} · audited {e(report["audited_at"])}'
        + (f' · {scope.get("pages_analysed", 0)} of {scope.get("pages_discovered", 0)} '
           f'discovered pages analysed in {scope.get("runtime_seconds", 0)}s'
           if scope else "") + "</p>")

    add('<div class="pills">')
    for sev in SEVERITY_ORDER:
        if summary.get(sev):
            add(f'<span class="pill" style="background:{SEVERITY_COLOR[sev]}">'
                f'{summary[sev]} {sev}</span>')
    if not summary.get("total_findings"):
        add('<span class="pill" style="background:#4a8a5c">no defects found</span>')
    add("</div>")

    if advisory:
        add(f'<div class="note"><strong>Advisory review.</strong> '
            f'{advisory.get("findings_in", 0)} candidate findings were reviewed and '
            f'{advisory.get("findings_out", 0)} emitted — '
            f'{len(advisory.get("dropped", []))} dropped for insufficient evidence, '
            f'threshold incoherence, small sample or a known false-positive pattern; '
            f'{len(advisory.get("downgraded", []))} downgraded; '
            f'{len(advisory.get("merged", []))} merged into an upstream root cause.</div>')

    add("<h2>Overview</h2>")
    if survey_genres:
        labels = {"price": "prices or rates", "dated": "visible dates"}
        genre_summary = " · ".join(
            f'{e(labels.get(k, k))}: <strong>{"present" if v else "absent"}</strong>'
            for k, v in survey_genres.items()
        )
        add(
            '<div class="note"><strong>Extractable facts.</strong> ' + genre_summary
            + " — genres an engine can lift verbatim, graded "
            "<code>Moderate</code> in arXiv:2607.14035 §7.5 Table 4 rather than "
            "measured with an effect size. Useful only when specific, current and "
            "attributed (§7.2): the goal is not to add numbers.</div>"
        )
    for key in ("severity", "layers", "mix", "absorption", "genres", "lengths"):
        chart = chart_set.get(key)
        if chart is not None:
            add(f'<div class="chart">{chart.to_svg()}</div>')

    add("<h2>Findings</h2>")
    if not report.get("findings"):
        add("<p>No defects were detected in the audited sample.</p>")
    for finding in report.get("findings", []):
        action = finding.get("suggested_action", {})
        add(f'<div class="card {e(finding["severity"])}">')
        add(f'<h3>{e(finding["id"])} · {e(finding["title"])}</h3>')
        add(f'<p class="meta"><code>{e(finding["severity"])}</code> '
            f'<code>{e(str(finding.get("layer", "-")))}</code> '
            f'claim level {finding.get("claim_level", "-")} · '
            f'confidence {e(str(finding.get("confidence", "-")))}</p>')
        add(f'<div class="ev">{e(finding["evidence"])}</div>')
        if finding.get("affected_urls"):
            add("<ul class='urls'>" + "".join(
                f"<li>{e(u)}</li>" for u in finding["affected_urls"][:6]) + "</ul>")
        add('<div class="act">')
        add(f'<strong>{e(action.get("summary", ""))}</strong> '
            f'<span class="meta">({e(action.get("priority", "medium"))} priority, '
            f'{e(action.get("effort", "medium"))} effort)</span>')
        if action.get("steps"):
            add("<ol>" + "".join(f"<li>{e(s)}</li>" for s in action["steps"]) + "</ol>")
        if action.get("mechanism"):
            add(f'<p class="why">Why this works: {e(action["mechanism"])}</p>')
        if action.get("verification"):
            add(f'<p class="why">Verify: {e(action["verification"])}</p>')
        add("</div></div>")

    if report.get("proactive_recommendations"):
        add("<h2>Proactive recommendations</h2>")
        add('<p class="sub">Improvements where no specific defect was found.</p>')
        for rec in report["proactive_recommendations"]:
            add('<div class="card low">')
            add(f'<h3>{e(rec["id"])} · {e(rec["title"])}</h3>')
            add(f'<p class="meta"><code>{e(rec.get("priority", "medium"))}</code> '
                f'claim level {rec.get("claim_level", 3)}</p>')
            add(f"<p>{e(rec['rationale'])}</p>")
            add(f'<div class="act"><strong>{e(rec["action"])}</strong>')
            if rec.get("basis"):
                add(f'<p class="why">Basis: {e(rec["basis"])}</p>')
            add("</div></div>")

    if report.get("limitations"):
        add("<h2>Limitations</h2><ul>")
        for item in report["limitations"]:
            add(f"<li>{e(item)}</li>")
        add("</ul>")

    if advisory.get("dropped"):
        add("<h2>Advisory audit trail</h2>")
        add("<p class='sub'>Candidate findings the critic removed, and why.</p>")
        add("<table><tr><th>Finding</th><th>Gate</th><th>Reason</th></tr>")
        # Full titles and reasons: table cells wrap in the browser, so slicing
        # them mid-word garbles the audit trail (agent-review 2026-09-09).
        for item in advisory["dropped"]:
            add(f"<tr><td>{e(str(item.get('title', '')))}</td>"
                f"<td><code>{e(str(item.get('gate', '')))}</code></td>"
                f"<td>{e(str(item.get('reason', '')))}</td></tr>")
        add("</table>")

    generator = report.get("generator", {})
    add(f"<footer>Generated by {e(generator.get('marketplace', ''))} "
        f"v{e(generator.get('version', ''))}. Recommend-only: no skill in this "
        f"marketplace modifies a live site.<br>Grounding: "
        f"{e(generator.get('grounding', ''))}</footer>")
    add("</div></body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def render_pdf(report: dict, chart_set: dict, path: str) -> str:
    summary = report.get("summary", {})
    scope = report.get("scope", {})
    advisory = report.get("advisory", {})

    doc = PdfDocument(
        title=f"Brand AI-Readiness Audit - {report['site']}",
        subject="AI discoverability and on-site engagement audit",
        footer_text=f"{report['site']} - brand AI-readiness audit",
    )
    doc.heading(1, "Brand AI-Readiness Audit")
    doc.paragraph(
        f"{report['site']} — audited {report['audited_at']}"
        + (f". {scope.get('pages_analysed', 0)} of {scope.get('pages_discovered', 0)} "
           f"discovered pages analysed in {scope.get('runtime_seconds', 0)}s."
           if scope else ".")
    )

    counts = ", ".join(f"{summary.get(s, 0)} {s}" for s in SEVERITY_ORDER if summary.get(s, 0))
    doc.key_value("Findings", f"{summary.get('total_findings', 0)} total"
                  + (f" ({counts})" if counts else ""))
    if scope:
        doc.key_value("Scope", str(scope.get("registrable_domain", "")))
        doc.key_value("Method", "raw HTML, no JavaScript executed; robots.txt respected")
    if advisory:
        doc.key_value(
            "Advisory review",
            f"{advisory.get('findings_in', 0)} reviewed, {advisory.get('findings_out', 0)} emitted, "
            f"{len(advisory.get('dropped', []))} dropped, "
            f"{len(advisory.get('downgraded', []))} downgraded, "
            f"{len(advisory.get('merged', []))} merged",
        )

    doc.heading(2, "Overview")
    for key in ("severity", "layers", "mix", "absorption", "genres", "lengths"):
        chart = chart_set.get(key)
        if chart is not None:
            chart.flow(doc)

    doc.heading(2, "Findings")
    if not report.get("findings"):
        doc.paragraph("No defects were detected in the audited sample.")
    for finding in report.get("findings", []):
        action = finding.get("suggested_action", {})
        doc.heading(3, f"{finding['id']}  {finding['title']}")
        doc.paragraph(
            f"Severity {finding['severity']}  |  layer {finding.get('layer', '-')}  |  "
            f"claim level {finding.get('claim_level', '-')}  |  "
            f"confidence {finding.get('confidence', '-')}",
            size=8.5,
        )
        doc.paragraph(finding["evidence"])
        for url in finding.get("affected_urls", [])[:4]:
            doc.bullet(url, size=8.5)
        doc.paragraph(
            f"Suggested action ({action.get('priority', 'medium')} priority, "
            f"{action.get('effort', 'medium')} effort): {action.get('summary', '')}"
        )
        for step in action.get("steps", []):
            doc.bullet(step, size=9)
        if action.get("mechanism"):
            doc.paragraph(f"Why this works: {action['mechanism']}", size=8.5)
        if action.get("verification"):
            doc.paragraph(f"Verify: {action['verification']}", size=8.5)
        doc.spacer(6)

    if report.get("proactive_recommendations"):
        doc.heading(2, "Proactive recommendations")
        doc.paragraph("Improvements where no specific defect was found.")
        for rec in report["proactive_recommendations"]:
            doc.heading(3, f"{rec['id']}  {rec['title']}")
            doc.paragraph(rec["rationale"])
            doc.paragraph(f"Action: {rec['action']}")
            if rec.get("basis"):
                doc.paragraph(f"Basis: {rec['basis']}", size=8.5)
            doc.spacer(4)

    if report.get("limitations"):
        doc.heading(2, "Limitations")
        for item in report["limitations"]:
            doc.bullet(item, size=9)

    if advisory.get("dropped"):
        doc.heading(2, "Advisory audit trail")
        doc.paragraph("Candidate findings the critic removed, and why.")
        # Full titles and reasons: the PDF table wraps text itself, so slicing
        # them mid-word garbles the audit trail (agent-review 2026-09-09).
        doc.table(
            ["Finding", "Gate", "Reason"],
            [[str(i.get("title", "")), str(i.get("gate", "")),
               str(i.get("reason", ""))] for i in advisory["dropped"]],
            col_widths=[150, 96, 222], size=8,
        )

    return doc.save(path)


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render an audit report.")
    parser.add_argument("--report", default="-", help="Report JSON path, or - for stdin.")
    parser.add_argument("--formats", default="md,html,pdf")
    parser.add_argument("--out", default="./audit-report", help="Output path stem.")
    args = parser.parse_args(argv)

    if args.report == "-":
        report = json.load(sys.stdin)
    else:
        report = json.loads(pathlib.Path(args.report).read_text(encoding="utf-8"))

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    stem = pathlib.Path(args.out)
    stem.parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    chart_set = build_charts(report) if formats & {"html", "pdf"} else {}

    if "md" in formats:
        path = stem.with_suffix(".md")
        path.write_text(render_markdown(report), encoding="utf-8")
        written.append(str(path))
    if "html" in formats:
        path = stem.with_suffix(".html")
        path.write_text(render_html(report, chart_set), encoding="utf-8")
        written.append(str(path))
    if "pdf" in formats:
        written.append(render_pdf(report, chart_set, str(stem.with_suffix(".pdf"))))

    print("\n".join(written))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
