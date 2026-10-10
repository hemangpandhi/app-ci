"""Javadoc HTML + PlantUML class diagrams (rendered to SVG)."""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

from .. import tools, util
from ..config import TOOL_ROOT
from ..javaenv import dependency_jars, generated_sources, module_jars, source_roots, write_argfile
from ..model import ERROR, PASSED, SKIPPED, WARNING
from . import Stage, register

DOCLET_SRC = TOOL_ROOT / "appci" / "java" / "PlantUmlDoclet.java"
UNRESOLVED_RE = re.compile(r"^(/\S+\.java):\d+: error: ", re.M)


_DOCLET: dict = {}


def _doclet_dir(ctx) -> Path:
    """Compile the doclet for this run in a private directory: javadoc loads the classes, and a
    shared cache (the tree's out/) is writable by other merge requests' build code."""
    key = str(ctx.cfg.java_home)
    if key not in _DOCLET:
        from .. import tools
        out = tools.private_dir("doclet")
        javac = ctx.cfg.java_home / "bin" / "javac"
        r = util.run([str(javac), "-d", str(out), str(DOCLET_SRC)], log=out / "javac.log")
        if not r.ok:
            raise RuntimeError(f"compiling PlantUmlDoclet failed: {r.tail(20)}")
        _DOCLET[key] = out
    return _DOCLET[key]


@register
class DocsStage(Stage):
    name = "docs"
    title = "Javadoc + class diagrams"

    def run(self, ctx, res):
        scfg = ctx.stage_cfg("docs")
        p = ctx.profile
        if not p.java_srcs and not p.kt_srcs:
            res.status, res.summary = SKIPPED, "no Java or Kotlin sources"
            return
        target = ctx.cfg.stage_cfg("build", ctx.entry)["target"]
        d = ctx.stage_dir("docs", clean=True)
        root = ctx.cfg.aosp_root
        gen = generated_sources(ctx.tree, target, p, d / "_gen")
        jars = dependency_jars(ctx.tree, target, p)
        roots = [str(root / r) for r in source_roots(root, p.java_srcs)]
        common = [
            "-sourcepath", ":".join(roots + [str(gen)]),
            "-classpath", ":".join(map(str, jars)),
            "-encoding", "UTF-8", "--ignore-source-errors", "-quiet",
            "-Xmaxerrs", "1", "-Xmaxwarns", "1",
            f"-{scfg.get('visibility', 'protected')}",
        ] + [str(root / f) for f in p.java_srcs]
        javadoc = str(ctx.cfg.java_home / "bin" / "javadoc")
        mem = "-J-Xmx6g"
        warnings = []

        if scfg.get("javadoc", True) and p.java_srcs:
            out = d / "javadoc"
            files = [str(root / f) for f in p.java_srcs]
            opts = [o for o in common if not o.endswith(".java")]
            excluded: list = []
            for attempt in range(3):
                argfile = write_argfile(d / "javadoc.args", opts + [
                    "-d", str(out), "-Xdoclint:none", "-notimestamp", "-use", "-author", "-version",
                    "-windowtitle", f"{p.name} API", "-doctitle", f"{p.name} ({p.path})",
                ] + [f for f in files if f not in excluded])
                r = util.run([javadoc, mem, argfile], log=d / "javadoc.log", timeout=scfg.get("timeout"),
                             append=attempt > 0)
                if (out / "index.html").is_file():
                    break
                # javadoc can crash on unresolvable symbols even with --ignore-source-errors:
                # retry without the files that reference them (they stay on the sourcepath).
                bad = {m.group(1) for m in UNRESOLVED_RE.finditer(r.output())} - set(excluded)
                if not bad:
                    break
                excluded.extend(sorted(bad))
            res.log = ctx.rel_report(d / "javadoc.log")
            if (out / "index.html").is_file():
                res.reports["javadoc"] = ctx.rel_report(out / "index.html")
                if excluded:
                    (d / "javadoc-excluded.txt").write_text("\n".join(excluded) + "\n")
                    res.reports["javadoc excluded files"] = ctx.rel_report(d / "javadoc-excluded.txt")
                    warnings.append(f"{len(excluded)} file(s) left out of javadoc (unresolved dependencies)")
            else:
                warnings.append(f"javadoc produced no output (exit {r.returncode})")

        diagrams = 0
        if scfg.get("uml", True) and p.java_srcs:
            uml = d / "uml"
            smetana = shutil.which("dot") is None
            argfile = write_argfile(d / "uml.args", common + [
                "-doclet", "PlantUmlDoclet", "-docletpath", str(_doclet_dir(ctx)),
                "-d", str(uml), "-visibility", scfg.get("visibility", "protected"),
            ] + (["-smetana"] if smetana else []))
            r = util.run([javadoc, mem, argfile], log=d / "uml.log", timeout=scfg.get("timeout"))
            pumls = sorted(uml.glob("*.puml"))
            if not pumls:
                warnings.append(f"no class diagrams generated (exit {r.returncode})")
            else:
                try:
                    jar = tools.ensure(ctx.cfg, "plantuml")
                    rr = util.run([ctx.cfg.java, "-Djava.awt.headless=true", "-DPLANTUML_LIMIT_SIZE=16384",
                                   "-jar", str(jar), "-tsvg", "-nometadata", "-charset", "UTF-8",
                                   *map(str, pumls)], log=d / "plantuml.log", timeout=scfg.get("timeout"))
                    if not rr.ok:
                        warnings.append("some diagrams failed to render (see plantuml.log)")
                except RuntimeError as exc:
                    warnings.append(f"PlantUML unavailable, .puml sources only: {exc}")
                diagrams = len(pumls)
                self.write_index(ctx, d, uml, pumls)
                res.reports["class diagrams"] = ctx.rel_report(d / "index.html")
        kotlin_docs = 0
        if scfg.get("kotlin", True) and p.kt_srcs:
            kotlin_docs = self.dokka(ctx, res, d, target, jars, warnings, scfg.get("timeout"))
        if kotlin_docs:   # index (written with the diagrams) gains the Kotlin link
            self.write_index(ctx, d, d / "uml", sorted((d / "uml").glob("*.puml")))
            res.reports.setdefault("class diagrams", ctx.rel_report(d / "index.html"))
        shutil.rmtree(gen, ignore_errors=True)
        res.metrics.update({"java_files": len(p.java_srcs), "diagrams": diagrams, "classpath_jars": len(jars),
                            "kotlin_files": len(p.kt_srcs),
                            "kotlin_files_not_documented": 0 if kotlin_docs else len(p.kt_srcs)})
        if not res.reports:
            res.status, res.summary = ERROR, "; ".join(warnings) or "no documentation produced"
            return
        res.status = WARNING if warnings else PASSED
        res.summary = (f"javadoc for {len(p.java_srcs)} files, {diagrams} diagram(s)"
                       + (f", Kotlin docs for {len(p.kt_srcs)} files" if kotlin_docs else "")) + (
            f" ({'; '.join(warnings)})" if warnings else "")

    @staticmethod
    def dokka(ctx, res, d: Path, target: str, jars, warnings, timeout) -> int:
        """Kotlin API docs with Dokka (Javadoc reads Java only). Returns the number of HTML pages."""
        p = ctx.profile
        try:
            cli = tools.ensure(ctx.cfg, "dokka-cli")
            plugins = [str(tools.ensure(ctx.cfg, t)) for t in tools.DOKKA_PLUGINS]
        except RuntimeError as exc:
            warnings.append(f"Dokka unavailable, Kotlin not documented: {exc}")
            return 0
        jdeps = ctx.tree.jdeps(target)
        own = [j for m in p.prod_modules for j in module_jars(ctx.tree, target, m, jdeps.get(m) or {})
               if j.suffix == ".jar" and j.is_file()]   # the app's Java types used from Kotlin
        out = d / "kotlin"
        root = ctx.cfg.aosp_root
        conf = {
            "moduleName": p.name, "outputDir": str(out), "offlineMode": True, "failOnWarning": False,
            "pluginsClasspath": plugins,
            "sourceSets": [{
                "displayName": "jvm", "sourceSetID": {"scopeId": p.name, "sourceSetName": "main"},
                "sourceRoots": [str(root / f) for f in p.kt_srcs],
                "classpath": [str(j) for j in own + list(jars)],
                "analysisPlatform": "jvm", "jdkVersion": 21, "skipEmptyPackages": True,
                "noStdlibLink": True, "noJdkLink": True, "noAndroidSdkLink": True,   # runners are offline
                "documentedVisibilities": ["PUBLIC", "PROTECTED"],
            }],
        }
        cfg_file = d / "dokka.json"
        cfg_file.write_text(json.dumps(conf, indent=1))
        r = util.run([ctx.cfg.java, "-Xmx4g", "-jar", str(cli), str(cfg_file)], log=d / "dokka.log",
                     timeout=timeout)
        pages = len(list(out.rglob("*.html"))) if (out / "index.html").is_file() else 0
        if pages:
            res.reports["kotlin docs"] = ctx.rel_report(out / "index.html")
        else:
            warnings.append(f"Dokka produced no output (exit {r.returncode}, see dokka.log)")
        return pages

    @staticmethod
    def write_index(ctx, d: Path, uml: Path, pumls):
        from ..reports.theme import page, theme_button
        rows = []
        for f in pumls:
            svg = f.with_suffix(".svg")
            overview = f.stem == "overview"
            name = "Package dependency overview" if overview else f.stem
            link = svg if svg.exists() else f
            rows.append((not overview, name,
                         f'<tr><td><a class="mono" href="{util.rel(link, d)}">{html.escape(name)}</a></td>'
                         f'<td class="nowrap"><a class="small" href="{util.rel(f, d)}">PlantUML source</a></td></tr>'))
        rows.sort(key=lambda r: r[:2])
        api = []
        if (d / "javadoc" / "index.html").exists():
            api.append('<a href="javadoc/index.html">Javadoc</a>')
        if (d / "kotlin" / "index.html").exists():
            api.append('<a href="kotlin/index.html">Kotlin API docs (Dokka)</a>')
        body = (f'<div class="top"><div><h1>{html.escape(ctx.profile.name)} — documentation</h1>'
                f'<div class="meta">{html.escape(ctx.profile.path)}</div></div>{theme_button()}</div>'
                + (f'<h2>API reference</h2><div class="links">{"".join(api)}</div>' if api else "")
                + (f'<h2>Class diagrams <span class="badge">{len(rows)}</span></h2><div class="card tablewrap">'
                   f'<table><thead><tr><th>Package</th><th></th></tr></thead><tbody>'
                   f'{"".join(r[2] for r in rows)}</tbody></table></div>' if rows else ""))
        (d / "index.html").write_text(page(f"{ctx.profile.name} documentation", body))
