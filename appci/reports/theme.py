"""One look for every page appci renders (dashboard, change risk, merged coverage): self-contained
HTML/CSS (offline runners), light/dark, phone width, print. Tool reports keep their own design and
get a small link back to the dashboard."""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#5f6670;--line:#e2e5ea;--soft:#f0f2f5;--accent:#2f6fde;
--passed:#1a7f37;--passed-bg:#e7f5ea;--warning:#9a6700;--warning-bg:#fff4d6;--failed:#cf222e;--failed-bg:#ffebe9;
--error:#8250df;--error-bg:#f3ecff;--skipped:#6e7781;--skipped-bg:#eef0f3;--shadow:0 1px 2px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1117;--card:#161b22;--fg:#e6edf3;
--muted:#9198a1;--line:#2d333b;--soft:#1c222b;--accent:#58a6ff;--passed:#3fb950;--passed-bg:#12261a;--warning:#d29922;
--warning-bg:#2a2112;--failed:#f85149;--failed-bg:#2d1415;--error:#a371f7;--error-bg:#221a33;--skipped:#8b949e;
--skipped-bg:#1f242c;--shadow:none}}
:root[data-theme=dark]{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#9198a1;--line:#2d333b;--soft:#1c222b;
--accent:#58a6ff;--passed:#3fb950;--passed-bg:#12261a;--warning:#d29922;--warning-bg:#2a2112;--failed:#f85149;
--failed-bg:#2d1415;--error:#a371f7;--error-bg:#221a33;--skipped:#8b949e;--skipped-bg:#1f242c;--shadow:none}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:22px;line-height:1.25;margin:0}h2{font-size:16px;margin:28px 0 10px;display:flex;align-items:center;gap:8px}
h3{font-size:14px;margin:0}code,.mono{font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.muted{color:var(--muted)}.small{font-size:12px}.nowrap{white-space:nowrap}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
.meta{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:6px;color:var(--muted);font-size:13px}
.meta b{color:var(--fg);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
.pad{padding:14px 16px}
.verdict{margin-top:16px;padding:16px 18px;border-radius:12px;border:1px solid;display:flex;gap:14px;align-items:flex-start}
.verdict .icon{font-size:22px;line-height:1}.verdict h2{margin:0;font-size:18px}
.verdict.passed{background:var(--passed-bg);border-color:var(--passed);color:var(--fg)}
.verdict.warning{background:var(--warning-bg);border-color:var(--warning)}
.verdict.failed,.verdict.error{background:var(--failed-bg);border-color:var(--failed)}
.verdict.skipped{background:var(--skipped-bg);border-color:var(--skipped)}
.verdict ul{margin:6px 0 0;padding-left:18px}.verdict li{overflow-wrap:anywhere}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.kpi{padding:12px 14px}.kpi .v{font-size:22px;font-weight:650;line-height:1.2}.kpi .l{font-size:12px;color:var(--muted)}
.stages{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.stage{display:block;padding:12px 14px;border-left:4px solid var(--skipped);color:var(--fg)}
.stage:hover{text-decoration:none;border-color:var(--accent)}
.stage.passed{border-left-color:var(--passed)}.stage.warning{border-left-color:var(--warning)}
.stage.failed{border-left-color:var(--failed)}.stage.error{border-left-color:var(--error)}
.stage .name{display:flex;justify-content:space-between;gap:8px;font-weight:600}
.stage .what{font-size:12.5px;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.badge{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:999px;font-size:11.5px;font-weight:600;
white-space:nowrap;background:var(--skipped-bg);color:var(--skipped)}
.badge.passed{background:var(--passed-bg);color:var(--passed)}.badge.warning{background:var(--warning-bg);color:var(--warning)}
.badge.failed{background:var(--failed-bg);color:var(--failed)}.badge.error{background:var(--error-bg);color:var(--error)}
.sev{display:inline-block;min-width:62px;text-align:center;padding:1px 6px;border-radius:6px;font-size:11px;font-weight:600}
.sev.blocker,.sev.critical{background:var(--failed-bg);color:var(--failed)}.sev.major{background:var(--warning-bg);color:var(--warning)}
.sev.minor,.sev.info{background:var(--skipped-bg);color:var(--skipped)}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;background:var(--soft)}
tr:last-child td{border-bottom:0}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td .mono,td a.mono{overflow-wrap:anywhere;word-break:break-word}td{overflow-wrap:anywhere}
table.fixed{table-layout:fixed}col.c-sev{width:100px}col.c-tool{width:130px}col.c-where{width:38%}
.bar{display:flex;height:10px;border-radius:999px;overflow:hidden;background:var(--soft);min-width:120px}
.bar span{display:block;height:100%}.bar .p{background:var(--passed)}.bar .f{background:var(--failed)}.bar .s{background:var(--skipped)}
.gauge{height:8px;border-radius:999px;background:var(--soft);overflow:hidden}.gauge span{display:block;height:100%;background:var(--accent)}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.filters button{font:inherit;font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);
color:var(--fg);cursor:pointer}.filters button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.links{display:flex;flex-wrap:wrap;gap:6px}.links a{padding:3px 9px;border:1px solid var(--line);border-radius:8px;font-size:12.5px;background:var(--card)}
details{border-top:1px solid var(--line)}details:first-of-type{border-top:0}summary{cursor:pointer;padding:10px 16px;list-style:none;
display:flex;justify-content:space-between;gap:10px;align-items:center}summary::-webkit-details-marker{display:none}
summary>span:first-child{flex:1;min-width:0}summary::before{content:"\\25B8";color:var(--muted)}
details[open]>summary::before{content:"\\25BE"}
.detail{padding:0 16px 14px}.empty{padding:18px 16px;color:var(--muted)}
.themebtn{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:4px 10px;cursor:pointer}
footer{margin-top:32px;color:var(--muted);font-size:12px}
@media (max-width:640px){h1{font-size:19px}.kpi .v{font-size:19px}th,td{padding:7px 9px}table.stack thead{display:none}table.stack,table.stack tbody,table.stack tr,table.stack td{display:block;width:auto}table.stack tr{padding:8px 0;border-bottom:1px solid var(--line)}table.stack tr:last-child{border-bottom:0}table.stack td{border:0;padding:3px 12px}}
@media print{body{background:#fff}.themebtn,.filters{display:none}.card{box-shadow:none}details:not([open])>.detail{display:block}}
"""

THEME_JS = """<script>
(function(){var k="appci-theme",r=document.documentElement;try{var t=localStorage.getItem(k);if(t)r.dataset.theme=t}catch(e){}
window.appciToggleTheme=function(){var d=r.dataset.theme||(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
r.dataset.theme=d==="dark"?"light":"dark";try{localStorage.setItem(k,r.dataset.theme)}catch(e){}};})();
</script>"""


def page(title: str, body: str, script: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style>{THEME_JS}</head>"
            f"<body><main>{body}</main>{script}</body></html>")


def theme_button() -> str:
    return '<button class="themebtn" type="button" onclick="appciToggleTheme()">Light / dark</button>'


BACKLINK_MARK = "<!-- appci-backlink -->"


def add_backlink(report: Path, dashboard: Path) -> None:
    """Fixed "back to dashboard" link in a tool's own HTML report (its design is left alone)."""
    try:
        text = report.read_text(errors="replace")
    except OSError:
        return
    if BACKLINK_MARK in text:
        return
    href = html.escape(os.path.relpath(dashboard, report.parent))
    link = (f'{BACKLINK_MARK}<a href="{href}" style="position:fixed;right:12px;bottom:12px;z-index:99999;'
            'font:600 12px system-ui,sans-serif;background:#2f6fde;color:#fff;padding:6px 12px;border-radius:999px;'
            'text-decoration:none;box-shadow:0 2px 6px rgba(0,0,0,.25)">&larr; appci dashboard</a>')
    new, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + link, text, count=1, flags=re.I)
    if n:
        report.write_text(new)
