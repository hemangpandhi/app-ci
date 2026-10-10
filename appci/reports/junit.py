"""Convert atest console output into JUnit XML (consumed by GitLab, Sonar and the dashboard)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from ..util import strip_ansi

RESULT_RE = re.compile(
    r"^\[(?P<idx>\d+)/(?P<total>\d+)\]\s+(?P<test>\S+?)(?::\s+(?P<status>PASSED|FAILED|IGNORED|ASSUMPTION_FAILED)"
    r"(?:\s+\((?P<time>[\d.]+)\s*(?P<unit>ms|s)\))?)?\s*$"
)
# "CarSettingsRoboTests (3 Tests)", newer atest: "x86_64 CarSettingsRoboTests (3 Tests)" (ABI first)
RUN_RE = re.compile(r"^(?:(?:x86_64|x86|arm64-v8a|armeabi-v7a|riscv64)\s+)?"
                    r"(?P<run>[\w.\-:]+(?:\s\(\S+\))?)\s+\((?P<count>\d+)\s+Tests?\)\s*$")
RUNNER_ERROR_RE = re.compile(r"^RUNNER ERROR:\s*(?P<msg>.*)$")
SUMMARY_RE = re.compile(r"^(Summary|-{3,}|Pretty printed|Test Result|Atest results|Run '|ITERATIONS RESULT)")


@dataclass
class TestCase:
    suite: str
    classname: str
    name: str
    status: str          # passed | failed | skipped | error
    time: float = 0.0
    details: str = ""


def parse_atest_output(text: str) -> List[TestCase]:
    cases: List[TestCase] = []
    run = "unknown"
    current: TestCase | None = None
    trace: List[str] = []
    in_trace = False

    def flush_trace():
        nonlocal in_trace, trace
        if current is not None and trace:
            current.details = "\n".join(trace).strip()
        trace = []
        in_trace = False

    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        m = RESULT_RE.match(line.strip())
        if m:
            flush_trace()
            test = m.group("test")
            cls, _, meth = test.partition("#")
            status = {"PASSED": "passed", "FAILED": "failed", "IGNORED": "skipped",
                      "ASSUMPTION_FAILED": "skipped", None: "passed"}[m.group("status")]
            t = float(m.group("time") or 0)
            if m.group("unit") == "ms":
                t /= 1000.0
            current = TestCase(run, cls, meth or cls, status, t)
            cases.append(current)
            continue
        m = RUN_RE.match(line.strip())
        if m:
            flush_trace()
            run = m.group("run")
            current = None
            continue
        m = RUNNER_ERROR_RE.match(line.strip())
        if m:
            flush_trace()
            cases.append(TestCase(run, run, "runner", "error", 0, m.group("msg")))
            current = None
            continue
        if line.strip() == "STACKTRACE:":
            in_trace = True
            continue
        if in_trace:
            if SUMMARY_RE.match(line.strip()):
                flush_trace()
            else:
                trace.append(line)
    flush_trace()
    return cases


def summarize(cases: List[TestCase]) -> Dict[str, int]:
    return {
        "tests": len(cases),
        "passed": sum(c.status == "passed" for c in cases),
        "failed": sum(c.status in ("failed", "error") for c in cases),
        "skipped": sum(c.status == "skipped" for c in cases),
    }


def write_junit(cases: List[TestCase], path: Path, extra_error: str | None = None) -> None:
    """One <testsuite> per test class (Surefire / Ant layout: name = class, package attribute,
    the test module as a property), so report tools group results by package and class."""
    root = ET.Element("testsuites")
    suites: Dict[tuple, List[TestCase]] = {}
    for c in cases:
        suites.setdefault((c.suite, c.classname), []).append(c)
    if extra_error and not cases:
        suites[("infrastructure", "atest")] = [TestCase("infrastructure", "atest", "invocation", "error", 0,
                                                        extra_error)]
    for (module, cls), items in suites.items():
        s = summarize(items)
        package = cls.rpartition(".")[0] if "." in cls else ""
        el = ET.SubElement(root, "testsuite", name=cls, package=package, tests=str(s["tests"]),
                           failures=str(sum(c.status == "failed" for c in items)),
                           errors=str(sum(c.status == "error" for c in items)),
                           skipped=str(s["skipped"]), time=f"{sum(c.time for c in items):.3f}")
        ET.SubElement(ET.SubElement(el, "properties"), "property", name="module", value=module)
        for c in items:
            tc = ET.SubElement(el, "testcase", classname=c.classname, name=c.name, time=f"{c.time:.3f}")
            if c.status == "failed":
                first = c.details.splitlines()[0][:500] if c.details else "failed"
                ET.SubElement(tc, "failure", message=first).text = c.details
            elif c.status == "error":
                ET.SubElement(tc, "error", message=(c.details or "error")[:500]).text = c.details
            elif c.status == "skipped":
                ET.SubElement(tc, "skipped")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
