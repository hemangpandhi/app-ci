"""HTML reports: the tools' official stylesheets for their XML (Checkstyle's audit report, Ant's
JUnit report) and appci's own pages where no tool renders one (change risk, merged coverage)."""
from __future__ import annotations

import html
import os
import re
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .. import tools, util
from ..config import TOOL_ROOT
from .theme import page, theme_button

BULLET = re.compile(r"^\s*[-*]\s+")
XSLT_SRC = TOOL_ROOT / "appci" / "java" / "Xslt.java"


def _stylesheet(cfg, kind: str) -> Path:
    if kind == "checkstyle":
        return tools.ensure(cfg, "checkstyle-xsl")
    jar = tools.ensure(cfg, "ant-junit")                  # verified against its pinned SHA-256
    xsl = tools.private_dir("junit-xsl") / "junit-noframes.xsl"
    with zipfile.ZipFile(jar) as z:
        xsl.write_bytes(z.read("org/apache/tools/ant/taskdefs/optional/junit/xsl/junit-noframes.xsl"))
    return xsl


def render_official(cfg, kind: str, xml: Path, out: Path, params: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Apply the tool's stylesheet ('checkstyle' | 'junit'). Returns an error text, None when done."""
    try:
        xsl = _stylesheet(cfg, kind)
    except (RuntimeError, OSError, KeyError, zipfile.BadZipFile) as exc:
        return f"{kind} stylesheet unavailable: {exc}"
    args = [cfg.java, str(XSLT_SRC), str(xml), str(xsl), str(out)]
    args += [f"{k}={v}" for k, v in (params or {}).items()]
    r = util.run(args, log=out.with_suffix(".xslt.log"))
    return None if r.ok and out.is_file() else f"{kind} HTML failed: {r.tail(5)}"


def _inline(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)


def markdown_page(md: str, title: str) -> str:
    """Enough Markdown for appci's own reports: headings, tables, lists, paragraphs."""
    out: List[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif BULLET.match(line):
            items = []
            while i < len(lines) and BULLET.match(lines[i]):
                items.append("<li>" + _inline(BULLET.sub("", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
        i += 1
    body = "\n".join(out).replace("<table>", '<div class="card tablewrap" style="margin:12px 0"><table>', ) \
        .replace("</table>", "</table></div>")
    return page(title, f'<div class="top"><div></div>{theme_button()}</div>' + body)


def coverage_page(title: str, total: Dict, suites: Dict[str, float], rows: Iterable[Tuple[str, int, int]],
                  uncovered: List[str], suite_links: Dict[str, str]) -> str:
    """Merged line coverage of all suites (per-suite pages are JaCoCo's own HTML)."""
    def pct(c, n):
        return 100.0 * c / n if n else 0.0
    e = html.escape
    rows = list(rows)
    paths = [r[0] for r in rows] + [u.rsplit(":", 1)[0] for u in uncovered]
    prefix = os.path.commonpath(paths) if len(paths) > 1 else ""
    prefix = prefix + "/" if prefix and not any(p == prefix for p in paths) else ""
    rows = [(r[0][len(prefix):], r[1], r[2]) for r in rows]
    uncovered = [u[len(prefix):] if u.startswith(prefix) else u for u in uncovered]
    head = (f'<div class="top"><div><h1>{e(title)}</h1><div class="meta">merged from '
            + ", ".join(f"{e(s)} {v}%" for s, v in suites.items())
            + (f" · paths relative to <code>{e(prefix)}</code>" if prefix else "") + f"</div></div>{theme_button()}</div>"
            '<div class="kpis">'
            f'<div class="card kpi"><div class="v">{total["line"]}%</div><div class="l">lines '
            f'({total["lines_covered"]}/{total["lines_valid"]})</div></div>'
            f'<div class="card kpi"><div class="v">{total["branch"]}%</div><div class="l">branches</div></div></div>')
    if suite_links:
        head += ('<h2>JaCoCo reports per suite</h2><div class="links">' + "".join(
            f'<a href="{e(link)}">{e(name)}</a>' for name, link in suite_links.items()) + "</div>")
    if uncovered:
        head += (f'<h2>Changed lines not covered <span class="badge failed">{len(uncovered)}</span></h2>'
                 '<div class="card pad mono" style="white-space:pre-wrap">' + e("\n".join(uncovered)) + "</div>")
    body = "".join(
        f'<tr><td><span class="mono">{e(path)}</span></td><td class="num">{cov}/{n}</td>'
        f'<td class="num">{pct(cov, n):.1f}%</td><td style="width:22%"><div class="gauge">'
        f'<span style="width:{pct(cov, n):.1f}%"></span></div></td></tr>'
        for path, n, cov in sorted(rows, key=lambda r: (pct(r[2], r[1]), r[0])))
    return page(title, head + '<h2>Files, least covered first</h2><div class="card tablewrap"><table><thead><tr>'
                '<th>File</th><th class="num">Lines</th><th class="num">Covered</th><th></th></tr></thead>'
                f"<tbody>{body}</tbody></table></div>")


LINK_RE = re.compile(r'(?P<attr>href|src)="(?P<url>[^"#?]*)(?P<frag>[#?][^"]*)?"')


def relocate_links(text: str, origin: Path, tree: Path, app_path: str) -> str:
    """Fix relative links of a tool report copied out of its build directory (Android Lint links
    sources and merged manifests relative to out/.../lint/). Sources of the app point to the MR's
    file view in GitLab (CI) or the file in the tree (local runs); anything else loses its link."""
    base, sha = os.environ.get("CI_PROJECT_URL"), os.environ.get("CI_COMMIT_SHA")
    app = str(tree / app_path.strip("/")) + os.sep

    out_root = next((a for a in [origin, *origin.parents] if a.name.startswith("out")), tree / "out")

    def _resolve(url: str) -> str:
        # Lint runs in a sandbox: links are relative to it ("../../packages/...", "soong/...")
        direct = os.path.normpath(os.path.join(str(origin), url))
        if os.path.exists(direct):
            return direct
        rest = re.sub(r"^(\.\./)+", "", url)
        for root in (tree, out_root):
            cand = os.path.normpath(os.path.join(str(root), rest))
            if os.path.exists(cand):
                return cand
        return direct

    def fix(m):
        url = m.group("url")
        if not url or re.match(r"^[a-zA-Z][\w+.-]*:", url) or url.startswith(("/", "data:")):
            return m.group(0)
        attr = m.group("attr")
        if attr == "src" and os.path.exists(os.path.join(str(origin), url)):
            return m.group(0)        # images / scripts the tool writes next to the report
        target = _resolve(url)
        if base and sha and target.startswith(app):
            rel = target[len(app):]
            view = "raw" if attr == "src" else "blob"     # images of the app (e.g. drawables) shown raw
            return f'{attr}="{html.escape(base)}/-/{view}/{html.escape(sha)}/{html.escape(rel)}"'
        if not base and os.path.exists(target):
            return f'{attr}="{html.escape(Path(target).as_uri())}"'
        return 'data-unavailable="1"'
    return LINK_RE.sub(fix, text)
