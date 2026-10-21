"""Helpers for the comparison of the ruamel compat module with ruamel.yaml on the kit's cases.

The functions give, for each case, what happened, so that the tests can pin the exact list of
differences (``ruamel_differences.json``) and of edits the engine cannot make
(``ruamel_edit_gaps.json``).
"""

from __future__ import annotations

import datetime
import math
import pathlib
from typing import Any

import oneyaml
from oneyaml.compat.ruamel import YAML


def kit_cases(kit: str):
    """(case id, path) of every yaml-test-suite input and every kit case input."""
    suites = pathlib.Path(kit) / "suites"
    for p in sorted(suites.glob("yaml-test-suite/data-*/**/in.yaml")):
        rel = p.parent.relative_to(suites / "yaml-test-suite").as_posix().split("/", 1)[1]
        yield "yts/" + rel, p
    for p in sorted(suites.glob("own/cases/*/*/input.yaml")):
        yield "own/" + p.parent.relative_to(suites / "own/cases").as_posix(), p


def _kind(v: Any) -> Any:
    for base in (bool, int, float, str, dict, list):
        if isinstance(v, base):
            return base
    if isinstance(v, datetime.datetime):
        return datetime.datetime
    return type(v)


def same(a: Any, b: Any) -> bool:
    """Equal values of equal kinds (1 is not True; a wrapper type is its base), nan equals nan,
    mappings in the same order. No recursion."""
    work = [(a, b)]
    while work:
        x, y = work.pop()
        if isinstance(x, dict) and isinstance(y, dict):
            if len(x) != len(y):
                return False
            for (ka, va), (kb, vb) in zip(x.items(), y.items(), strict=True):
                work.append((ka, kb))
                work.append((va, vb))
        elif isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)):
            if len(x) != len(y):
                return False
            work.extend(zip(x, y, strict=True))
        else:
            if _kind(x) is not _kind(y):
                return False
            if isinstance(x, float) and math.isnan(x) and math.isnan(y):
                continue
            try:
                if x != y:
                    return False
            except Exception:
                return False
    return True


def load_difference(theirs_cls: Any, text: str, typ: str) -> str | None:
    """None when ruamel and the compat module read ``text`` alike in mode ``typ``, else a short
    description of how they differ. ``None`` is also returned when ruamel itself runs out of
    stack, which says nothing about the compat module."""
    try:
        ref, ref_err = theirs_cls(typ=typ).load(text), None
    except RecursionError:
        return None
    except Exception as exc:
        ref, ref_err = None, exc
    try:
        got, got_err = YAML(typ=typ).load(text), None
    except oneyaml.YAMLError as exc:
        got, got_err = None, exc
    if ref_err is not None and got_err is not None:
        return None
    if ref_err is not None:
        return "ruamel rejects input that YAML 1.2 accepts"
    if got_err is not None:
        return (
            "the limits refuse input that ruamel builds"
            if got_err.category == "limit"
            else "ruamel accepts input that YAML 1.2 forbids"
        )
    if same(ref, got):
        return None
    if typ == "rt" and "<<" in text:
        return "rt keeps a merge key as an ordinary key; ruamel merges it"
    if typ == "rt" and "!" in text:
        return "rt reads a tagged node as oneyaml.Tagged; ruamel wraps it in its own types"
    return "ruamel reads a different value"


# ---- edit scripts --------------------------------------------------------------------------------


def _paths(data: Any) -> list[tuple[tuple, Any]]:
    out = []
    stack = [((), data)]
    while stack:
        path, node = stack.pop()
        out.append((path, node))
        if isinstance(node, dict):
            stack.extend(((*path, k), v) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend(((*path, i), v) for i, v in enumerate(node))
    return out


def _get(data: Any, path: tuple) -> Any:
    for k in path:
        data = data[k]
    return data


def edit_scripts(data: Any) -> list[tuple[str, Any]]:
    """Edits that make sense for ``data``: (name, function that applies it in place)."""
    nodes = sorted(_paths(data), key=lambda t: (len(t[0]), str(t[0])))
    scripts: list[tuple[str, Any]] = []
    leaves = [p for p, n in nodes if not isinstance(n, (dict, list)) and p]
    if leaves:
        first, last = leaves[0], leaves[-1]
        scripts.append(
            (
                "set-first-leaf",
                lambda x: _get(x, first[:-1]).__setitem__(first[-1], "changed value"),
            )
        )
        scripts.append(
            ("set-last-leaf-int", lambda x: _get(x, last[:-1]).__setitem__(last[-1], 12345))
        )
    if isinstance(data, dict) and data:
        scripts.append(("add-key", lambda x: x.__setitem__("zz_new", {"a": [1, 2], "b": "t"})))
        scripts.append(("del-first-key", lambda x: x.__delitem__(next(iter(x)))))
    if isinstance(data, list):
        scripts.append(("append", lambda x: x.append("zz_new")))
        if data:
            scripts.append(("del-first-item", lambda x: x.__delitem__(0)))
    seqs = [p for p, n in nodes if isinstance(n, list) and n]
    if seqs:
        scripts.append(("insert-seq", lambda x: _get(x, seqs[0]).insert(1, "ins")))
        scripts.append(("pop-seq", lambda x: _get(x, seqs[-1]).pop()))
    maps = [p for p, n in nodes if isinstance(n, dict) and n]
    if maps:
        scripts.append(("add-key-deep", lambda x: _get(x, maps[-1]).__setitem__("zz_k", "v")))
    return scripts


def edit_outcome(text: str, name: str) -> str:
    """What the edit script ``name`` does to ``text`` through the compat module: ``ok`` (the
    output reads back as the edited data), ``refused`` (the library says it cannot make the edit),
    ``wrong`` or ``unreadable`` (it made an edit that changes other data, or broke the document).
    Any other exception is a bug and propagates."""
    expected = oneyaml.loads(text)
    fn = dict(edit_scripts(expected))[name]
    fn(expected)
    y = YAML()
    data = y.load(text)
    fn(data)
    import io

    out = io.StringIO()
    try:
        y.dump(data, out)
    except oneyaml.YAMLError:
        return "refused"
    try:
        back = oneyaml.loads(out.getvalue())
    except oneyaml.YAMLError:
        return "unreadable"
    return "ok" if same(back, expected) else "wrong"
