"""Minimal Android.bp reading: module names and types.

Soong before Android 15 writes no `module_type` into out/soong/module_bp_java_deps.json; appci
then reads the type of each module from the Android.bp of its directory. Vendor module types
declared with `soong_config_module_type` (or imported with `soong_config_module_type_import`)
resolve to the Soong type they wrap, so an `xxx_car_app` counts as the `android_app` it is.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, Tuple

_NAME_RE = re.compile(r'(?m)^\s*name\s*:\s*"([^"]+)"')
_BASE_RE = re.compile(r'(?m)^\s*module_type\s*:\s*"([^"]+)"')
_FROM_RE = re.compile(r'(?m)^\s*from\s*:\s*"([^"]+)"')
_TYPES_RE = re.compile(r"(?ms)^\s*module_types\s*:\s*\[(.*?)\]")


def strip_comments(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def top_level_blocks(t: str) -> Iterator[Tuple[str, str]]:
    """Yield (module_type, body) for every `type { ... }` at brace depth 0 (strings skipped)."""
    i, n, depth = 0, len(t), 0
    ident = re.compile(r"[A-Za-z_][\w]*")
    while i < n:
        c = t[i]
        if c == '"':
            j = t.find('"', i + 1)
            while j > 0 and t[j - 1] == "\\":
                j = t.find('"', j + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and (c.isalpha() or c == "_"):
            m = ident.match(t, i)
            k = m.end()
            while k < n and t[k] in " \t\r\n":
                k += 1
            if k < n and t[k] == "{":
                d, j = 0, k
                while j < n:
                    if t[j] == '"':
                        j = t.find('"', j + 1)
                        if j < 0:
                            j = n
                            break
                    elif t[j] == "{":
                        d += 1
                    elif t[j] == "}":
                        d -= 1
                        if d == 0:
                            break
                    j += 1
                yield m.group(0), t[k + 1:j]
                i = j + 1
                continue
            i = k
            continue
        i += 1


# --- aconfig
def aconfig_flags(text: Optional[str]) -> Tuple[Dict[str, dict], dict]:
    flags: Dict[str, dict] = {}
    header = {}
    if not text:
        return flags, header
    t = re.sub(r"(?m)#.*$", "", text)
    for key in ("package", "container"):
        m = re.search(rf'(?m)^\s*{key}\s*:\s*"([^"]+)"', t)
        if m:
            header[key] = m.group(1)
    for m in re.finditer(r"\bflag\s*\{", t):
        depth, j = 0, m.end() - 1
        while j < len(t):
            depth += t[j] == "{"
            depth -= t[j] == "}"
            if depth == 0:
                break
            j += 1
        body = t[m.end():j]
        fields = {k: v for k, v in re.findall(r'(?m)^\s*(name|namespace|description|bug)\s*:\s*"([^"]*)"', body)}
        if fields.get("name"):
            flags[fields["name"]] = fields
    return flags, header


def _flat(body: str) -> str:
    """Top-level properties only: nested { ... } blocks dropped."""
    flat = re.sub(r"\{[^{}]*\}", "{}", body)
    return re.sub(r"\{[^{}]*\}", "{}", flat)


@lru_cache(maxsize=None)
def _custom_types(root: str, bp: str) -> Dict[str, str]:
    """soong_config_module_type definitions in an Android.bp: custom name -> wrapped Soong type."""
    out: Dict[str, str] = {}
    try:
        text = strip_comments(Path(bp).read_text())
    except OSError:
        return out
    for mtype, body in top_level_blocks(text):
        if mtype != "soong_config_module_type":
            continue
        name, base = _NAME_RE.search(_flat(body)), _BASE_RE.search(_flat(body))
        if name and base:
            out[name.group(1)] = base.group(1)
    return out


@lru_cache(maxsize=None)
def module_types(root: str, bp: str) -> Dict[str, str]:
    """Module name -> Soong type for every module an Android.bp declares, custom types resolved."""
    try:
        text = strip_comments(Path(bp).read_text())
    except OSError:
        return {}
    custom: Dict[str, str] = {}
    mods: Dict[str, str] = {}
    for mtype, body in top_level_blocks(text):
        flat = _flat(body)
        if mtype == "soong_config_module_type":
            name, base = _NAME_RE.search(flat), _BASE_RE.search(flat)
            if name and base:
                custom[name.group(1)] = base.group(1)
            continue
        if mtype == "soong_config_module_type_import":
            src, types = _FROM_RE.search(flat), _TYPES_RE.search(flat)
            if src and types:
                defs = _custom_types(root, str(Path(root) / src.group(1)))
                for t in re.findall(r'"([^"]+)"', types.group(1)):
                    if t in defs:
                        custom[t] = defs[t]
            continue
        name = _NAME_RE.search(flat)
        if name:
            mods[name.group(1)] = mtype
    return {n: custom.get(t, t) for n, t in mods.items()}


def fill_module_types(root: Path, jdeps: Dict[str, dict]) -> int:
    """Add `module_type` to jdeps entries that lack it, from the Android.bp of each module's
    directory. Returns the number filled in."""
    n = 0
    for name, info in jdeps.items():
        if info.get("module_type"):
            continue
        paths = info.get("path") or []
        if not paths:
            continue
        mtype = module_types(str(root), str(root / paths[0] / "Android.bp")).get(name)
        if mtype:
            info["module_type"] = mtype
            n += 1
    return n
