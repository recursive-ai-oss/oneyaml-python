"""Reading for the ruamel compat layer: what ruamel's resolver reads that the Core schema does not,
merge keys, and wrapping plain values into ``CommentedMap``/``CommentedSeq`` tied to a document.

Every walk here uses an explicit stack, so the nesting of the input never reaches the Python
recursion limit.
"""

from __future__ import annotations

import re
from typing import Any

from ..._errors import make_error
from ..._limits import _NONE
from ..._types import Tagged
from ..._yaml11 import Reject, timestamp
from .comments import CommentedMap, CommentedSeq, _Source
from .error import ConstructorError

# ruamel reads these plain scalars as numbers and dates although the Core schema calls them
# text: digits grouped with "_", signed hex, and 0b binary, and YAML 1.1 timestamps.
_INT = re.compile(
    r"[-+]?(?:"
    r"0x[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*"
    r"|0o[0-7]+(?:_[0-7]+)*"
    r"|0b[01]+(?:_[01]+)*"
    r"|[0-9]+(?:_[0-9]+)*"
    r")\Z"
)
_FLOAT = re.compile(
    r"[-+]?(?:"
    r"[0-9]+(?:_[0-9]+)*\.(?:[0-9]+(?:_[0-9]+)*)?(?:[eE][-+]?[0-9]+)?"
    r"|\.[0-9]+(?:_[0-9]+)*(?:[eE][-+]?[0-9]+)?"
    r"|[0-9]+(?:_[0-9]+)*[eE][-+]?[0-9]+"
    r")\Z"
)
_STAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ \t]*Z|[ \t]*[-+][0-9]{1,2}(?::[0-9]{2})?)?\Z"
)
# A cheap test on the whole text: when it fails, no scalar needs the walk below.
_HINT = (
    r"[0-9]_[0-9]|0[bB][01]|[-+]0[xXoO]|[0-9]{4}-[0-9]|<<"
    r"|!!(?:set|omap|pairs)|yaml.org,2002:(?:set|omap|pairs)"
)
_HINT_STR = re.compile(_HINT)
_HINT_BYTES = re.compile(_HINT.encode())


def might_need_fixups(raw: Any) -> bool:
    """True when the text could hold something ruamel reads differently from the Core schema."""
    if isinstance(raw, str):
        return _HINT_STR.search(raw) is not None
    head = bytes(raw[:4])
    if b"\x00" in head or head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True  # UTF-16/32: cannot search the bytes
    return _HINT_BYTES.search(raw) is not None


def _number(text: str, digits_cap: int) -> int | float | None:
    """The value ruamel reads for ``text`` when it is a number the Core schema calls text."""
    if _INT.match(text):
        t = text.replace("_", "")
        neg = t.startswith("-")
        t = t.lstrip("+-")
        low = t[:2].lower()
        base = {"0x": 16, "0o": 8, "0b": 2}.get(low, 10)
        body = t[2:] if base != 10 else t
        if digits_cap != _NONE and len(body) > digits_cap:
            raise make_error(
                f"an integer of more than {digits_cap} digits", "limit"
            )  # same cap as every other integer
        n = int(body, base)
        return -n if neg else n
    if _FLOAT.match(text):
        return float(text.replace("_", ""))
    return None


def _stamp(text: str) -> Any:
    if _STAMP.match(text):
        try:
            return timestamp(text)
        except (Reject, ValueError):
            return None
    return None


_SET = "tag:yaml.org,2002:set"
_OMAP = "tag:yaml.org,2002:omap"
_PAIRS = "tag:yaml.org,2002:pairs"


def _yaml_type(t: Tagged) -> Any:
    """What ruamel's safe loader builds for ``!!set``, ``!!omap`` and ``!!pairs``, or ``t``."""
    v = t.value
    if t.tag == _SET and type(v) is dict:
        return set(v)
    if (
        t.tag in (_OMAP, _PAIRS)
        and type(v) is list
        and all(type(i) is dict and len(i) == 1 for i in v)
    ):
        pairs = [next(iter(i.items())) for i in v]
        return dict(pairs) if t.tag == _OMAP else pairs
    return t


def fix_scalars(plain: Any, handle: Any, d: int, digits_cap: int, types: bool = False) -> Any:
    """Replace the text values ruamel reads as numbers or dates; only plain (unquoted, untagged)
    scalars change. With ``types``, ``!!set``, ``!!omap`` and ``!!pairs`` become a set, a dict
    and a list of pairs. Collections are changed in place; the (possibly new) root is returned."""
    if types and type(plain) is Tagged:
        plain = _yaml_type(plain)
    if type(plain) is not dict and type(plain) is not list:
        return plain
    seen = {id(plain)}
    stack: list[tuple[Any, tuple]] = [(plain, ())]
    while stack:
        node, path = stack.pop()
        if type(node) is dict:
            slots: Any = list(node.items())
            step: Any = None
        else:
            slots = list(enumerate(node))
            step = str
        for k, v in slots:
            t = type(v)
            if t is str:
                new = _convert(v, digits_cap)
                if new is not None:
                    tok = k if step is None else str(k)
                    try:
                        info = handle.info(d, [*path, tok])
                    except (KeyError, IndexError, ValueError):
                        continue
                    if info[0] == "plain" and info[1] is None:
                        node[k] = new
            elif t is Tagged and types:
                new = _yaml_type(v)
                if new is not v:
                    node[k] = new
                    if type(new) is dict and id(new) not in seen:
                        seen.add(id(new))
                        stack.append((new, (*path, k if step is None else str(k))))
            elif t is dict or t is list:
                if id(v) not in seen:
                    seen.add(id(v))
                    stack.append((v, (*path, k if step is None else str(k))))
    return plain


def _convert(text: str, digits_cap: int) -> Any:
    n = len(text)
    if n < 3:
        return None
    if n >= 8 and text[4] == "-" and text[:4].isdigit():
        return _stamp(text)
    head = text[:3].lower()
    # Cheap test before the regular expressions: grouping, a sign before 0x/0o, or 0b.
    if "_" in text or head[:2] == "0b" or head[1:3] in ("0x", "0o", "0b"):
        return _number(text, digits_cap)
    return None


def merge_keys(plain: Any) -> Any:
    """Apply ``<<`` merges as ruamel's safe loader does: keys of the merged mapping(s) are added
    unless the mapping has them itself, earlier sources before later ones."""
    if type(plain) is not dict and type(plain) is not list:
        return plain
    # Collect the mappings that merge, then merge each after the mappings it merges from.
    seen = {id(plain)}
    todo: list[dict] = []
    stack = [plain]
    while stack:
        node = stack.pop()
        if type(node) is dict:
            if "<<" in node:
                todo.append(node)
            kids = node.values()
        else:
            kids = node
        for v in kids:
            t = type(v)
            if (t is dict or t is list) and id(v) not in seen:
                seen.add(id(v))
                stack.append(v)
    if not todo:
        return plain
    pending = {id(n) for n in todo}
    state: dict[int, int] = {}
    for top in todo:
        work = [top]
        while work:
            node = work[-1]
            nid = id(node)
            if state.get(nid) == 2 or nid not in pending:
                work.pop()
                continue
            sources = _sources(node["<<"])
            if state.get(nid) is None:
                state[nid] = 1
                deps = [s for s in sources if id(s) in pending and state.get(id(s)) != 2]
                if any(state.get(id(s)) == 1 for s in deps):
                    raise ConstructorError("a mapping merges itself", category="syntax")
                if deps:
                    work.extend(deps)
                    continue
            elif state[nid] == 1:
                deps = [s for s in sources if id(s) in pending and state.get(id(s)) != 2]
                if deps:
                    work.extend(deps)
                    continue
            merged: dict = {}
            for s in sources:
                for k, v in s.items():
                    merged.setdefault(k, v)
            own = {k: v for k, v in node.items() if k != "<<"}
            merged.update(own)
            node.clear()
            node.update(merged)
            state[nid] = 2
            work.pop()
    return plain


def _sources(value: Any) -> list[dict]:
    if type(value) is dict:
        return [value]
    if type(value) is list and all(type(s) is dict for s in value):
        return value
    raise ConstructorError("expected a mapping or list of mappings for merging", category="syntax")


def bind(plain: Any, src: _Source, d: int) -> Any:
    """``plain`` with every dict and list made a CommentedMap/CommentedSeq tied to ``src``.
    A collection that is reached twice (an alias) stays one object."""
    t = type(plain)
    if t is not dict and t is not list:
        return plain
    made: dict[int, Any] = {}
    new_map, new_seq = CommentedMap, CommentedSeq

    def shell(node: Any) -> Any:
        c = new_map(node) if type(node) is dict else new_seq(node)
        c._src = src
        made[id(node)] = c
        return c

    root = shell(plain)
    root._d = d
    stack = [(plain, root)]
    while stack:
        node, new = stack.pop()
        if type(node) is dict:
            for k, v in node.items():
                t = type(v)
                if t is dict or t is list:
                    c = made.get(id(v))
                    if c is None:
                        c = shell(v)
                        stack.append((v, c))
                    dict.__setitem__(new, k, c)
        else:
            for i, v in enumerate(node):
                t = type(v)
                if t is dict or t is list:
                    c = made.get(id(v))
                    if c is None:
                        c = shell(v)
                        stack.append((v, c))
                    list.__setitem__(new, i, c)
    return root
