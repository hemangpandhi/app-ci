"""Aggregate every app's stage results into summary.json, Code Quality JSON and an HTML dashboard."""
from __future__ import annotations

import datetime as _dt
import html
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List

from .. import util
from ..config import TOOL_ROOT
from ..model import ERROR, FAILED, PASSED, WARNING, Finding, StageResult, is_new, load_findings, worst
from .issues import write_gitlab_codequality
from .theme import add_backlink, page, theme_button

STAGE_ORDER = ["preupload", "build", "risk", "lint", "checkstyle", "ktlint", "detekt", "unit_test", "device_test",
               "coverage", "docs", "sonar", "image", "deploy"]
LABELS = {"unit_test": "unit tests", "device_test": "device tests", "preupload": "pre-upload hooks",
          "risk": "change risk"}


NUMERIC_METRICS = {"tests", "passed", "failed", "skipped", "errors", "warnings", "total", "new_total",
                   "new_errors", "new_warnings", "line", "branch", "new_line", "lines_valid", "lines_covered",
                   "new_lines_valid", "new_lines_covered", "blockers", "review_items", "not_run"}


def _clean(r: StageResult) -> StageResult:
    """Results files travel between jobs as artifacts, and earlier jobs run merge-request code:
    metrics the page shows as numbers must be numbers, report links must stay inside the report."""
    for k in list(r.metrics):
        if k in NUMERIC_METRICS or k.endswith("_line"):
            try:
                v = float(r.metrics[k])
                r.metrics[k] = int(v) if v.is_integer() and k not in ("line", "branch", "new_line") else v
            except (TypeError, ValueError):
                del r.metrics[k]
    r.reports = {k: v for k, v in r.reports.items() if _safe_link(v)}
    if r.log and not _safe_link(r.log):
        r.log = ""
    return r


def _safe_link(path: str) -> bool:
    if re.match(r"^https?://", path):
        return True
    return ":" not in path.split("/")[0] and not path.startswith("/") and ".." not in Path(path).parts


def load_results(report_root: Path) -> Dict[str, Dict[str, StageResult]]:
    apps: Dict[str, Dict[str, StageResult]] = {}
    for f in sorted(report_root.glob("*/results/*.json")):
        d = util.read_json(f)
        if d:
            r = _clean(StageResult.from_dict(d))
            apps.setdefault(f.parent.parent.name, {})[r.stage] = r
    return apps


def write_aggregates(report_root: Path, aosp_root: Path) -> Dict[str, object]:
    apps = load_results(report_root)
    summary = {"generated": _dt.datetime.now().isoformat(timespec="seconds"), "apps": {}, "ci": ci_context(),
               "appci_version": subprocess.run(["git", "-C", str(TOOL_ROOT), "describe", "--tags", "--always"],
                                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                               text=True).stdout.strip()}
    for slug, stages in apps.items():
        profile = util.read_json(report_root / slug / "profile.json", {}) or {}
        findings: List[Finding] = []
        for s in ("preupload", "risk", "lint", "checkstyle", "ktlint", "detekt"):
            fp = report_root / slug / s / "findings.json"
            if fp.exists():
                findings.extend(load_findings(fp))
        write_gitlab_codequality(findings, report_root / slug / "gl-code-quality-report.json",
                                 profile.get("path", ""))
        summary["apps"][slug] = {
            "name": profile.get("name", slug), "path": profile.get("path"),
            "status": worst(r.status for r in stages.values()),
            "stages": {k: {"status": v.status, "summary": v.summary, "metrics": v.metrics,
                           "gate": v.gate_violations, "duration": round(v.duration, 1)}
                       for k, v in stages.items()},
        }
    summary["status"] = worst(a["status"] for a in summary["apps"].values()) if apps else "skipped"
    util.write_json(report_root / "summary.json", summary)
    write_html(report_root, apps, summary)
    return summary



FINDING_STAGES = ("preupload", "risk", "lint", "checkstyle", "ktlint", "detekt")
TOOL_LABEL = {"preupload": "Pre-upload hooks", "build": "Build", "risk": "Change risk", "lint": "Android Lint",
              "checkstyle": "Checkstyle", "ktlint": "ktlint", "detekt": "Detekt", "unit_test": "Unit tests",
              "device_test": "Device tests", "coverage": "Coverage", "docs": "Documentation", "sonar": "SonarQube",
              "image": "Emulator image", "deploy": "Prebuilt delivery"}
STATUS_TEXT = {"passed": "Passed", "warning": "Passed with warnings", "failed": "Failed", "error": "Error",
               "skipped": "Skipped"}
SEV_ORDER = {"blocker": 0, "critical": 1, "major": 2, "minor": 3, "info": 4}
GATE_WORDS = {"new_errors": "new error(s) on changed lines", "new_total": "new issue(s) on changed lines",
              "new_warnings": "new warning(s) on changed lines", "failed": "failing test(s)",
              "blockers": "risk blocker(s)", "review_items": "risk review item(s)",
              "new_line": "% coverage of changed lines", "line": "% line coverage",
              "apk_growth_pct": "% APK size growth", "hooks_failed": "failed pre-upload hook(s)", "errors": "error(s)"}
CI_VARS = ("CI_PIPELINE_URL", "CI_PIPELINE_ID", "CI_MERGE_REQUEST_IID", "CI_MERGE_REQUEST_TITLE",
           "CI_MERGE_REQUEST_SOURCE_BRANCH_NAME", "CI_MERGE_REQUEST_TARGET_BRANCH_NAME", "CI_COMMIT_SHA",
           "CI_COMMIT_SHORT_SHA", "CI_COMMIT_REF_NAME", "CI_PROJECT_URL", "CI_PROJECT_PATH", "CI_JOB_URL",
           "APPCI_PLATFORM", "APPCI_APP")


def ci_context() -> Dict[str, str]:
    return {k: os.environ[k] for k in CI_VARS if os.environ.get(k)}


def _e(x) -> str:
    return html.escape(str(x))


def _badge(status: str) -> str:
    return f'<span class="badge {_e(status)}">{_e(STATUS_TEXT.get(status, status))}</span>'


def _stage_badge(r: StageResult) -> str:
    """Like _badge, but a stage that could not run says so instead of "Passed with warnings"."""
    if r.metrics.get("not_run"):
        return '<span class="badge warning">Not run</span>'
    return _badge(r.status)


def _plural(n, word) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(r: StageResult) -> str:
    """One plain sentence per stage for the stage cards."""
    m = r.metrics
    if m.get("not_run"):
        why = r.summary.split(";")[0].replace("NOT RUN: ", "")
        return f"Not run ({why.split(' (')[0]}) · {_plural(m['not_run'], 'impacted test module')}"
    if r.stage in ("lint", "checkstyle", "ktlint", "detekt") and "total" in m:
        if "new_total" in m:
            if m.get("new_errors"):
                head = f"{_plural(m['new_errors'], 'new error')} on changed lines"
            elif m.get("new_total"):
                head = f"No new errors · {_plural(m['new_total'], 'new warning')}"
            else:
                head = "Nothing new on changed lines"
            return f"{head} · {m['total']} existing"
        return f"{_plural(m['total'], 'issue')} ({m.get('errors', 0)} errors) · full scan"
    if r.stage in ("unit_test", "device_test") and "tests" in m:
        s = f"{m.get('passed', 0)} of {m['tests']} passed"
        if m.get("failed"):
            s += f" · {m['failed']} failed"
        if m.get("skipped"):
            s += f" · {m['skipped']} skipped"
        return s
    if r.stage == "coverage" and "line" in m:
        s = f"{m['line']}% of lines covered"
        if "new_line" in m:
            s += f" · changed lines {m['new_line']}%"
        elif "new_lines_valid" in m:
            s += f" · {m['new_lines_valid']} changed code line(s)"
        return s
    if r.stage == "risk" and "blockers" in m:
        if m["blockers"]:
            return f"{_plural(m['blockers'], 'blocker')} · {m.get('review_items', 0)} for review"
        return f"No blockers · {m.get('review_items', 0)} for review"
    if r.stage == "preupload" and "hooks_failed" in m:
        return "All hooks passed" if not m["hooks_failed"] else f"{_plural(m['hooks_failed'], 'hook')} failed"
    return r.summary or ""


def humanize_gate(v: str) -> str:
    m = re.match(r"^(\w+)=([\d.]+) ([<>]) ([\d.]+)$", v.strip())
    if not m:
        return v
    name, val, op, lim = m.groups()
    words = GATE_WORDS.get(name, name)
    if words.startswith("%"):
        return f"{val}{words} (minimum {lim}%)" if op == "<" else f"{val}{words} (maximum {lim}%)"
    return f"{val} {words} (allowed {'at least' if op == '<' else 'at most'} {lim})"


def _new_findings(root: Path, slug: str) -> List[Finding]:
    cs = util.read_json(root / slug / "changeset.json", {}) or {}
    lines = {p: set(v) for p, v in (cs.get("lines") or {}).items()}
    if not lines:
        return []
    out = []
    for s in FINDING_STAGES:
        fp = root / slug / s / "findings.json"
        if fp.exists():
            out += [f for f in load_findings(fp) if is_new(f, lines)]
    return sorted(out, key=lambda f: (SEV_ORDER.get(f.severity, 9), f.path, f.line))


def _failed_tests(root: Path, slug: str, stage: str, limit: int = 50):
    junit = root / slug / stage / "junit.xml"
    if not junit.exists():
        return []
    try:
        tree = util.xml_root(junit)
    except ET.ParseError:
        return []
    out = []
    for case in tree.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is not None:
            out.append((case.get("classname", ""), case.get("name", ""),
                        " ".join((bad.get("message") or bad.text or "").split())[:300]))
    return out[:limit]


def _source_link(ci: Dict[str, str], app_path: str, path: str, line: int) -> str:
    """Link findings to the MR's file view in GitLab (app projects: repo root = app directory)."""
    base, sha, app = ci.get("CI_PROJECT_URL"), ci.get("CI_COMMIT_SHA"), ci.get("APPCI_APP") or ""
    prefix = (app or app_path or "").rstrip("/") + "/"
    rel = path[len(prefix):] if prefix != "/" and path.startswith(prefix) else path
    label = f"{rel}:{line}" if line else rel
    if base and sha and app and path.startswith(app.rstrip("/") + "/"):
        href = f"{base}/-/blob/{sha}/{rel}" + (f"#L{line}" if line else "")
        return f'<a class="mono" title="{_e(path)}" href="{_e(href)}">{_e(label)}</a>'
    return f'<span class="mono" title="{_e(path)}">{_e(label)}</span>'


def _href(slug: str, path: str) -> str:
    if not _safe_link(path):
        return "#"
    return path if re.match(r"^https?://", path) else f"{slug}/{path}"


def _app_section(root: Path, slug: str, res: Dict[str, StageResult], app: Dict, ci: Dict[str, str],
                 multi: bool) -> str:
    order = sorted(res, key=lambda x: STAGE_ORDER.index(x) if x in STAGE_ORDER else 99)
    out = []
    if multi:
        out.append(f'<h2 id="{_e(slug)}">{_e(app["name"])} {_badge(app["status"])} '
                   f'<span class="muted small">{_e(app.get("path") or "")}</span></h2>')

    # Verdict: what blocks the merge, in words.
    gates = [(s, humanize_gate(v)) for s in order for v in res[s].gate_violations]
    crashed = [s for s in order if res[s].status == ERROR]
    not_run = [s for s in order if res[s].metrics.get("not_run")]
    status = app["status"]
    icon = {"passed": "&#10004;", "warning": "&#9888;", "failed": "&#10006;", "error": "&#10006;"}.get(status, "&#8226;")
    if gates or crashed:
        title = "Merge blocked"
    elif status == WARNING:
        title = "Passed with warnings"
    elif status == PASSED:
        title = "All quality gates passed"
    else:
        title = STATUS_TEXT.get(status, status)
    items = [f"<li><b>{_e(TOOL_LABEL.get(s, s))}:</b> {_e(g)}</li>" for s, g in gates]
    items += [f"<li><b>{_e(TOOL_LABEL.get(s, s))}:</b> did not complete — {_e(res[s].summary)}</li>" for s in crashed]
    # Not run (e.g. no emulator image) never blocks: listed apart from the blocking gates.
    if not_run and items:
        items.append('<li class="muted" style="list-style:none;margin-left:-18px;margin-top:6px">'
                     'Not run (does not block):</li>')
    items += [f"<li><b>{_e(TOOL_LABEL.get(s, s))}:</b> {_e(res[s].summary.replace('NOT RUN: ', 'not run — '))}</li>"
              for s in not_run]
    out.append(f'<div class="verdict {_e(status)}"><div class="icon">{icon}</div><div><h2>{_e(title)}</h2>'
               + (f"<ul>{''.join(items)}</ul>" if items else
                  '<div class="muted">No new problems on the changed lines and every test passed.</div>')
               + "</div></div>")

    # Key numbers.
    new = _new_findings(root, slug)
    kp = []
    if any(s in res for s in FINDING_STAGES):
        errs = sum(1 for f in new if f.is_error)
        kp.append((f"{len(new)}", f"new findings ({errs} errors)" if new else "new findings on changed lines"))
    for s in ("unit_test", "device_test"):
        m = res[s].metrics if s in res else {}
        if "tests" in m:
            kp.append((f"{m.get('passed', 0)}/{m['tests']}", f"{TOOL_LABEL[s].lower()} passed"))
    cov = res.get("coverage")
    if cov and "line" in cov.metrics:
        kp.append((f"{cov.metrics['line']}%", "line coverage"))
        if "new_line" in cov.metrics:
            kp.append((f"{cov.metrics['new_line']}%", "changed lines covered"))
    total = sum(r.duration for r in res.values())
    if total:
        kp.append((util.fmt_duration(total), "stage time"))
    if kp:
        out.append('<div class="kpis">' + "".join(
            f'<div class="card kpi"><div class="v">{_e(v)}</div><div class="l">{_e(l)}</div></div>' for v, l in kp)
            + "</div>")

    # Stage cards.
    out.append("<h2>Stages</h2><div class=\"stages\">" + "".join(
        f'<a class="card stage {_e(res[s].status)}" href="#{_e(slug)}-{_e(s)}"><div class="name">'
        f'<span>{_e(TOOL_LABEL.get(s, s))}</span><span class="muted small">{_e(util.fmt_duration(res[s].duration))}'
        f'</span></div><div class="what">{_e(describe(res[s]))}</div></a>' for s in order) + "</div>")

    # Everything this change introduced, across tools.
    if (root / slug / "changeset.json").exists():
        tools = sorted({f.tool for f in new})
        rows = "".join(
            f'<tr data-tool="{_e(f.tool)}"><td><span class="sev {_e(f.severity)}">{_e(f.severity)}</span></td>'
            f'<td class="nowrap">{_e(TOOL_LABEL.get(f.tool, f.tool))}</td><td>{_source_link(ci, app.get("path") or "", f.path, f.line)}'
            f'<div class="small muted">{_e(f.rule)}</div></td><td>{_e(" ".join(f.message.split())[:400])}</td></tr>'
            for f in new)
        filters = ""
        if len(tools) > 1:
            filters = ('<div class="filters" data-for="nf-' + _e(slug) + '"><button type="button" aria-pressed="true" '
                       'data-tool="">All</button>' + "".join(
                           f'<button type="button" aria-pressed="false" data-tool="{_e(t)}">{_e(TOOL_LABEL.get(t, t))}</button>'
                           for t in tools) + "</div>")
        body = (f'{filters}<div class="card tablewrap"><table class="fixed stack" id="nf-{_e(slug)}"><colgroup><col class="c-sev">'
                f'<col class="c-tool"><col class="c-where"><col></colgroup><thead><tr><th>Severity</th><th>Tool</th>'
                f'<th>Where</th><th>Message</th></tr></thead><tbody>{rows}</tbody></table></div>' if new else
                '<div class="card empty">Nothing new: no tool reports anything on the lines this change touches.</div>')
        out.append(f"<h2>Introduced by this change <span class=\"badge\">{len(new)}</span></h2>{body}")

    # Tests.
    tparts = []
    for s in ("unit_test", "device_test"):
        if s not in res:
            continue
        m = res[s].metrics
        if "tests" not in m:
            tparts.append(f'<div class="card pad"><h3>{_e(TOOL_LABEL[s])} {_stage_badge(res[s])}</h3>'
                          f'<p class="muted">{_e(res[s].summary)}</p></div>')
            continue
        t = max(m["tests"], 1)
        p, f, sk = m.get("passed", 0), m.get("failed", 0) + m.get("errors", 0), m.get("skipped", 0)
        fails = _failed_tests(root, slug, s)
        flist = "".join(f'<tr><td class="mono">{_e(c)}<br><b>{_e(n)}</b></td><td>{_e(msg)}</td></tr>'
                        for c, n, msg in fails)
        tparts.append(
            f'<div class="card pad"><h3>{_e(TOOL_LABEL[s])} {_badge(res[s].status)}</h3>'
            f'<div class="bar" style="margin:10px 0 6px"><span class="p" style="width:{100*p/t:.2f}%"></span>'
            f'<span class="f" style="width:{100*f/t:.2f}%"></span><span class="s" style="width:{100*sk/t:.2f}%"></span></div>'
            f'<div class="small muted">{p} passed · {f} failed · {sk} skipped · {m["tests"]} total</div>'
            + (f'<div class="tablewrap" style="margin-top:10px"><table><thead><tr><th>Failed test</th><th>Reason</th>'
               f'</tr></thead><tbody>{flist}</tbody></table></div>' if fails else "") + "</div>")
    if tparts:
        out.append(f'<h2>Tests</h2><div class="grid2">{"".join(tparts)}</div>')

    # Coverage.
    if cov and "line" in cov.metrics:
        m = cov.metrics
        suites = [(k[:-5], v) for k, v in m.items() if k.endswith("_line") and k not in ("new_line",)]
        rows = "".join(f'<tr><td>{_e(TOOL_LABEL.get(n, n))}</td><td class="num">{v}%</td><td style="width:50%">'
                       f'<div class="gauge"><span style="width:{min(float(v), 100):.1f}%"></span></div></td></tr>'
                       for n, v in suites)
        unc = root / slug / "coverage" / "uncovered-new-lines.txt"
        unc_n = len(unc.read_text().split()) if unc.exists() else 0
        link = cov.reports.get("coverage.html")
        out.append(
            '<h2>Coverage</h2><div class="card pad"><div class="kpis" style="margin-top:0">'
            f'<div><div class="kpi" style="padding:0"><div class="v">{m["line"]}%</div><div class="l">lines '
            f'({m.get("lines_covered", "?")}/{m.get("lines_valid", "?")})</div></div></div>'
            f'<div><div class="kpi" style="padding:0"><div class="v">{m.get("branch", "–")}%</div><div class="l">branches</div></div></div>'
            + (f'<div><div class="kpi" style="padding:0"><div class="v">{m["new_line"]}%</div><div class="l">changed lines'
               f' ({m.get("new_lines_covered")}/{m.get("new_lines_valid")})</div></div></div>' if "new_line" in m else "")
            + "</div>"
            + (f'<div class="tablewrap" style="margin-top:12px"><table><thead><tr><th>Suite</th><th class="num">Lines</th>'
               f'<th></th></tr></thead><tbody>{rows}</tbody></table></div>' if rows else "")
            + (f'<p class="small">{unc_n} changed line(s) not covered — see the coverage report.</p>' if unc_n else "")
            + (f'<div class="links"><a href="{_e(_href(slug, link))}">Coverage report</a></div>' if link else "")
            + "</div>")

    # Static analysis.
    srows = []
    for s in ("lint", "checkstyle", "ktlint", "detekt"):
        if s not in res:
            continue
        m = res[s].metrics
        rep = next((v for k, v in res[s].reports.items() if v.endswith(".html")), None)
        srows.append(f'<tr><td>{_e(TOOL_LABEL[s])}</td><td>{_badge(res[s].status)}</td>'
                     f'<td class="num">{m.get("new_errors", "–")}</td><td class="num">{m.get("new_warnings", "–")}</td>'
                     f'<td class="num">{m.get("total", "–")}</td><td class="num">{m.get("errors", "–")}</td>'
                     f'<td>{f"<a href={chr(34)}{_e(_href(slug, rep))}{chr(34)}>report</a>" if rep else ""}</td></tr>')
    if srows:
        out.append('<h2>Static analysis</h2><div class="card tablewrap"><table><thead><tr><th>Tool</th><th>Status</th>'
                   '<th class="num">New errors</th><th class="num">New warnings</th><th class="num">All issues</th>'
                   f'<th class="num">of which errors</th><th></th></tr></thead><tbody>{"".join(srows)}</tbody></table></div>'
                   '<p class="small muted">Only new errors on changed lines block a merge; existing issues are shown '
                   'for reference.</p>')

    # Change risk.
    if "risk" in res:
        checks = util.read_json(root / slug / "risk" / "checks.json", {}) or {}
        crow = "".join(f"<tr><td class=\"nowrap\">{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in checks.items())
        rf = root / slug / "risk" / "findings.json"
        rfind = load_findings(rf) if rf.exists() else []
        frow = "".join(f'<tr><td><span class="sev {_e(f.severity)}">{_e(f.severity)}</span></td>'
                       f'<td>{_source_link(ci, app.get("path") or "", f.path, f.line)}</td><td>{_e(f.message)}</td></tr>'
                       for f in rfind)
        out.append(f'<h2>Change risk {_badge(res["risk"].status)}</h2><div class="grid2">'
                   + (f'<div class="card tablewrap"><table><thead><tr><th>Check</th><th>Result</th></tr></thead>'
                      f'<tbody>{crow}</tbody></table></div>' if crow else "")
                   + (f'<div class="card tablewrap"><table class="fixed stack"><colgroup><col class="c-sev"><col class="c-where">'
                      f'<col></colgroup><thead><tr><th>Level</th><th>Where</th><th>What</th></tr>'
                      f'</thead><tbody>{frow}</tbody></table></div>' if frow else
                      '<div class="card empty">No blockers and nothing flagged for review.</div>')
                   + "</div>")

    # Every stage: summary, gate, reports, log.
    det = []
    for s in order:
        r = res[s]
        reps = sorted(r.reports.items(), key=lambda kv: (not kv[1].endswith(".html"), kv[0]))
        links = "".join(f'<a href="{_e(_href(slug, v))}">{_e(k)}</a>' for k, v in reps)
        if r.log:
            links += f'<a href="{_e(_href(slug, r.log))}">log</a>'
        gate = "".join(f'<div class="small" style="color:var(--failed)">Gate: {_e(humanize_gate(g))}</div>'
                       for g in r.gate_violations)
        det.append(f'<details id="{_e(slug)}-{_e(s)}"{" open" if r.status in (FAILED, ERROR) else ""}><summary>'
                   f'<span><b>{_e(TOOL_LABEL.get(s, s))}</b> <span class="muted small">{_e(r.summary)}</span></span>'
                   f'{_stage_badge(r)}</summary><div class="detail">{gate}'
                   f'<div class="links" style="margin-top:6px">{links or "<span class=muted>no reports</span>"}</div>'
                   f'</div></details>')
    out.append(f'<h2>Reports</h2><div class="card">{"".join(det)}</div>')
    return "".join(out)


FILTER_JS = """<script>
document.querySelectorAll('.filters').forEach(function(f){var t=document.getElementById(f.dataset.for);
f.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;
f.querySelectorAll('button').forEach(function(x){x.setAttribute('aria-pressed',x===b)});
t.querySelectorAll('tbody tr').forEach(function(r){r.style.display=!b.dataset.tool||r.dataset.tool===b.dataset.tool?'':'none'})})});
</script>"""


def write_html(report_root: Path, apps: Dict[str, Dict[str, StageResult]], summary) -> None:
    ci = summary.get("ci") or {}
    multi = len(apps) > 1
    names = [summary["apps"][s]["name"] for s in apps]
    title = names[0] if len(names) == 1 else f"{len(names)} apps"
    mr = ci.get("CI_MERGE_REQUEST_IID")
    heading = f"{title} — " + (f"merge request !{mr}" if mr else (ci.get("CI_COMMIT_REF_NAME") or "local run"))
    meta = []
    if ci.get("CI_MERGE_REQUEST_TITLE"):
        meta.append(f"<span><b>{_e(ci['CI_MERGE_REQUEST_TITLE'])}</b></span>")
    if ci.get("CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"):
        meta.append(f"<span>{_e(ci['CI_MERGE_REQUEST_SOURCE_BRANCH_NAME'])} &rarr; "
                    f"{_e(ci.get('CI_MERGE_REQUEST_TARGET_BRANCH_NAME', ''))}</span>")
    if ci.get("CI_COMMIT_SHORT_SHA"):
        meta.append(f"<span>commit <code>{_e(ci['CI_COMMIT_SHORT_SHA'])}</code></span>")
    if ci.get("APPCI_PLATFORM"):
        meta.append(f"<span>platform {_e(ci['APPCI_PLATFORM'])}</span>")
    if ci.get("CI_PIPELINE_URL"):
        meta.append(f'<span><a href="{_e(ci["CI_PIPELINE_URL"])}">pipeline #{_e(ci.get("CI_PIPELINE_ID", ""))}</a></span>')
    if not multi and apps:
        meta.append(f"<span>{_e(summary['apps'][next(iter(apps))].get('path') or '')}</span>")
    body = [f'<div class="top"><div><h1>{_e(heading)} {_badge(summary["status"])}</h1>'
            f'<div class="meta">{"".join(meta)}</div></div>{theme_button()}</div>']
    if multi:
        stages = [s for s in STAGE_ORDER if any(s in v for v in apps.values())]
        head = "".join(f"<th>{_e(TOOL_LABEL.get(s, s))}</th>" for s in stages)
        rows = "".join(
            f'<tr><td><a href="#{_e(slug)}"><b>{_e(summary["apps"][slug]["name"])}</b></a></td>'
            f'<td>{_badge(summary["apps"][slug]["status"])}</td>'
            + "".join(f'<td>{_stage_badge(res[s]) if s in res else "<span class=muted>–</span>"}</td>' for s in stages)
            + "</tr>" for slug, res in apps.items())
        body.append(f'<h2>Apps</h2><div class="card tablewrap"><table><thead><tr><th>App</th><th>Overall</th>{head}'
                    f'</tr></thead><tbody>{rows}</tbody></table></div>')
    for slug, res in apps.items():
        body.append(_app_section(report_root, slug, res, summary["apps"][slug], ci, multi))
    body.append(f'<footer>Generated {_e(summary["generated"])} on {_e(os.uname().nodename)} by appci '
                f'{_e(summary.get("appci_version", ""))}</footer>')
    (report_root / "index.html").write_text(page(f"{title} · AOSP App CI", "".join(body), FILTER_JS))
    for slug, res in apps.items():          # tool reports: their own design + a way back
        for r in res.values():
            for v in r.reports.values():
                target = (report_root / slug / v).resolve()
                if v.endswith(".html") and _safe_link(v) and "://" not in v \
                        and str(target).startswith(str(report_root.resolve()) + os.sep):
                    add_backlink(target, report_root / "index.html")
