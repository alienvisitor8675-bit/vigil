"""The black-box rule for hidden tests (hotfix 5, round 2; owner ruling).

A hidden test runs in the verifier's test process, so code it imports runs in
that process and can rewrite the verdict (a `conftest.py`, or a module that
registers a pytest hook, was reproduced). So a hidden test never imports,
requires or loads in-scope code. It reaches the worker's code only through
vigild's helper: `run_subject` (a command) and `call_subject` (a function, run
by a separate vigild-owned driver process), both against a read-only checkout.

`problems` is the static check, run at authoring and again in the verifier. It
refuses any import (Python `import`/`from`, JS `import`/`require`/`import()`)
that resolves into the task's scope, and any construct that loads code by a name
it can't see (`importlib`, `__import__`, `exec`, `eval`, `sys.path`, a computed
`require`/`import()` argument). It is a check on what the author wrote, not a
sandbox: the real boundary is that the subject only ever runs in another
process."""

from __future__ import annotations

import ast
import posixpath
import re
from collections.abc import Mapping, Sequence

from vigild.intake.paths import in_scope

PY_BANNED_MODULES = {"importlib", "runpy", "pkgutil", "imp", "zipimport", "ctypes", "gc"}
PY_BANNED_NAMES = {"__import__", "exec", "eval", "compile"}
JS_EXTS = (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".tsx", ".jsx", ".json")


def _py_candidates(module: str, base_dir: str, level: int) -> list[str]:
    parts = module.split(".") if module else []
    if level:
        up = base_dir
        for _ in range(level - 1):
            up = posixpath.dirname(up)
        root = [up] if up else []
    else:
        root = []
    out: list[str] = []
    for k in range(1, len(parts) + 1):
        stem = "/".join([*root, *parts[:k]])
        out += [f"{stem}.py", f"{stem}/__init__.py"]
    if not parts and root:
        out.append(f"{root[0]}/__init__.py")
    return out


def _py_problems(path: str, text: str, scope: Sequence[str]) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"{path}: not valid Python ({e.msg})"]
    out: list[str] = []
    base_dir = posixpath.dirname(path)
    for node in ast.walk(tree):
        mods: list[tuple[str, int]] = []
        if isinstance(node, ast.Import):
            mods = [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "", node.level)]
            if node.module is None or node.level:
                # `from . import x` names a sibling module too
                mods += [
                    ((node.module + "." if node.module else "") + a.name, node.level)
                    for a in node.names
                ]
            else:
                mods += [(f"{node.module}.{a.name}", 0) for a in node.names]
        for module, level in mods:
            top = module.split(".")[0]
            if top == "vigil_hidden" and not level:
                continue
            if top in PY_BANNED_MODULES:
                out.append(
                    f"{path}:{getattr(node, 'lineno', 0)}: imports {top}, which loads code by name"
                )
            for cand in _py_candidates(module, base_dir, level):
                if in_scope(cand, scope):
                    out.append(
                        f"{path}:{getattr(node, 'lineno', 0)}: imports in-scope code ({cand})"
                    )
                    break
        if isinstance(node, ast.Name) and node.id in PY_BANNED_NAMES:
            out.append(f"{path}:{node.lineno}: uses {node.id}, which loads or runs code by value")
        if (
            isinstance(node, ast.Attribute)
            and node.attr in ("path", "modules", "meta_path")
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            out.append(f"{path}:{node.lineno}: touches sys.{node.attr}")
    return out


_JS_STATIC = re.compile(
    r"""\b(?:import|export)\b[^'"`;]*?\bfrom\s*(['"])(.+?)\1|\bimport\s*(['"])(.+?)\3"""
)
_JS_CALL = re.compile(r"""\b(import|require)\s*\(\s*([^)]*?)\s*\)""")
_JS_LITERAL = re.compile(r"""^(['"])(.*)\1$""")


def _js_resolve(spec: str, base_dir: str) -> list[str]:
    if not spec.startswith((".", "/")):
        return []  # a package or node: builtin; the base's copy at most
    stem = posixpath.normpath(posixpath.join(base_dir, spec.lstrip("/")))
    out = [stem]
    out += [stem + e for e in JS_EXTS]
    out += [f"{stem}/index{e}" for e in JS_EXTS]
    root, ext = posixpath.splitext(stem)
    if ext in (".js", ".mjs", ".cjs"):
        out += [root + e for e in (".ts", ".mts", ".cts")]
    return out


def _js_problems(path: str, text: str, scope: Sequence[str]) -> list[str]:
    out: list[str] = []
    base_dir = posixpath.dirname(path)
    specs: list[str] = []
    for m in _JS_STATIC.finditer(text):
        specs.append(m.group(2) or m.group(4))
    for m in _JS_CALL.finditer(text):
        lit = _JS_LITERAL.match(m.group(2))
        if lit is None:
            out.append(f"{path}: {m.group(1)}() with a computed argument loads code by value")
        else:
            specs.append(lit.group(2))
    if re.search(r"\bcreateRequire\b|\bprocess\.binding\b|\bnew\s+Function\b|\beval\s*\(", text):
        out.append(f"{path}: loads code by value (createRequire, Function or eval)")
    for spec in specs:
        if posixpath.basename(spec).startswith("vigil_hidden"):
            continue
        for cand in _js_resolve(spec, base_dir):
            if in_scope(cand, scope):
                out.append(f"{path}: imports in-scope code ({spec})")
                break
    return out


def problems(files: Mapping[str, str], scope: Sequence[str]) -> list[str]:
    """Why these hidden test files (path -> text) are not black-box, or []."""
    out: list[str] = []
    for path, text in sorted(files.items()):
        if path.endswith(".py"):
            out += _py_problems(path, text, scope)
        elif path.endswith((".ts", ".mts", ".cts", ".js", ".mjs", ".cjs", ".tsx", ".jsx")):
            out += _js_problems(path, text, scope)
    return out
