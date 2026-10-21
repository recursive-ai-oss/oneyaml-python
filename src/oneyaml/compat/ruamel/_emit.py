"""Writing data that has no document behind it, in ruamel.yaml's layout.

Block style by default, two spaces per level, a sequence under a key at the key's column (set
``indent(mapping=, sequence=, offset=)`` to change it). Scalars are decided by the library's own
writer, so every string that would read back as something else is quoted. Style types
(``LiteralScalarString`` and the others) and comments set on ``CommentedMap``/``CommentedSeq``
are honoured. Lines are never wrapped.

The walk uses an explicit stack: the nesting of the data never reaches the recursion limit.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping
from typing import Any

from ... import Tagged, dumps
from ..._types import FrozenMapping
from .error import RepresenterError
from .scalarstring import ScalarString

_END = object()
_SCALAR_TYPES = (str, int, float, bool, type(None))

_DQ = re.compile(r'[\x00-\x1f\\"\x7f-\x9f\u2028\u2029\ufeff]|[\ud800-\udfff]')
_ESC = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
    "\0": "\\0",
    "\x07": "\\a",
    "\x08": "\\b",
    "\x0b": "\\v",
    "\x0c": "\\f",
    "\x1b": "\\e",
    "\x85": "\\N",
    "\u2028": "\\L",
    "\u2029": "\\P",
}
# Characters a block scalar can carry as they are: not line breaks other than "\n", no controls.
_BLOCK_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x84\x86-\x9f\u2028\u2029\ufeff\ud800-\udfff]")
_PLAIN_BAD = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\ufeff\ud800-\udfff]")


class Layout:
    """How to write: set by the YAML instance."""

    __slots__ = (
        "explicit_end",
        "explicit_start",
        "flow",
        "map_indent",
        "null_empty",
        "offset",
        "seq_indent",
        "version",
    )

    def __init__(self) -> None:
        self.map_indent = 2
        self.seq_indent = 2
        self.offset = 0
        self.explicit_start = False
        self.explicit_end = False
        self.flow: bool | None = False
        self.null_empty = True
        self.version: str | None = None


def _escape(m: re.Match) -> str:
    ch = m.group()
    r = _ESC.get(ch)
    if r is not None:
        return r
    o = ord(ch)
    if 0xD800 <= o <= 0xDFFF:
        raise ValueError("a string with a lone surrogate cannot be written as YAML")
    return f"\\x{o:02X}" if o < 256 else f"\\u{o:04X}"


def double_quoted(s: str) -> str:
    return '"' + _DQ.sub(_escape, s) + '"'


def _is_collection(v: Any) -> bool:
    return isinstance(v, (Mapping, list, tuple))


def to_plain(value: Any) -> Any:
    """``value`` as something ``oneyaml.dumps`` takes: style types and dates become text,
    Commented containers plain ones. A value that contains itself is a ``ValueError``."""
    if not _is_collection(value):
        return _leaf(value)
    path: set[int] = set()
    stack: list[Any] = []

    def make(src: Any) -> Any:
        if id(src) in path:
            raise ValueError("circular reference")
        path.add(id(src))
        is_map = isinstance(src, Mapping)
        new: Any = {} if is_map else []
        stack.append((src, new, iter(src.items() if is_map else src), is_map))
        return new

    root = make(value)
    while stack:
        src, new, it, is_map = stack[-1]
        for item in it:
            if is_map:
                k, v = item
                if _is_collection(k):
                    k = to_plain_key(k)
                if _is_collection(v):
                    new[k] = make(v)
                    break
                new[k] = _leaf(v)
            elif _is_collection(item):
                new.append(make(item))
                break
            else:
                new.append(_leaf(item))
        else:
            path.discard(id(src))
            stack.pop()
    return root


def to_plain_key(k: Any) -> Any:
    p = to_plain(k)
    if isinstance(p, list):
        return tuple(p)
    return FrozenMapping(p)


_SCALARS = (str, int, float, bytes, bytearray, memoryview, Tagged, FrozenMapping)


def _leaf(v: Any) -> Any:
    if v is None or isinstance(v, _SCALARS):
        return str(v) if isinstance(v, ScalarString) else v
    if isinstance(v, datetime.datetime):
        return v.isoformat(" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, (set, frozenset)):
        return Tagged("tag:yaml.org,2002:set", dict.fromkeys(v))
    raise RepresenterError(f"cannot represent an object of type {type(v).__name__}")


# ---- scalars -------------------------------------------------------------------------------------


def _inline_str(s: str) -> str | None:
    """The one-line writing of ``s`` chosen by the library, or None for a block scalar."""
    t = dumps(str(s))
    if t[:1] in "|>":
        return None
    return t[:-1]


def literal(s: str, parent: int, body: int) -> tuple[str, list[str]] | None:
    """Header and body lines of ``s`` as a ``|`` block under a parent at column ``parent``, body
    at column ``body``; None when the text cannot be written that way."""
    if not s or _BLOCK_BAD.search(s):
        return None
    stripped = s.rstrip("\n")
    if not stripped:
        return None
    n = len(s) - len(stripped)
    chomp = "-" if n == 0 else ("" if n == 1 else "+")
    lines = stripped.split("\n")
    if n > 1:
        lines.extend([""] * (n - 1))
    first = lines[0]
    indicator = str(body - parent) if first[:1] in (" ", "") else ""
    pad = " " * body
    return "|" + indicator + chomp, [pad + ln if ln else "" for ln in lines]


def folded(s: str, parent: int, body: int) -> tuple[str, list[str]] | None:
    if not s or _BLOCK_BAD.search(s) or s[0] in " \t\n":
        return None
    stripped = s.rstrip("\n")
    if not stripped:
        return None
    n = len(s) - len(stripped)
    chomp = "-" if n == 0 else ("" if n == 1 else "+")
    out: list[str] = []
    empties = 0
    prev: str | None = None
    for line in stripped.split("\n"):
        if line == "":
            empties += 1
            continue
        if prev is not None:
            normal = prev[0] not in " \t" and line[0] not in " \t"
            out.extend([""] * (empties + 1 if normal else empties))
        out.append(line)
        prev = line
        empties = 0
    if n > 1:
        out.extend([""] * (n - 1))
    pad = " " * body
    return ">" + chomp, [pad + ln if ln else "" for ln in out]


def scalar(v: Any, lay: Layout, parent: int, body: int, key: bool = False) -> Any:
    """``str`` for a one-line scalar, or ``(header, lines)`` for a block scalar."""
    if v is None:
        return "" if (lay.null_empty and not key) else "null"
    if isinstance(v, str):
        style = v.style if isinstance(v, ScalarString) else None
        if style and not key:
            r = _styled(str(v), style, parent, body)
            if r is not None:
                return r
        t = _inline_str(v)
        if t is not None:
            return t
        if key:
            return double_quoted(str(v))
        return literal(str(v), parent, body) or double_quoted(str(v))
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        try:
            return int.__repr__(v)
        except ValueError:
            return dumps(int(v))[:-1]
    if isinstance(v, float):
        return dumps(float(v))[:-1]
    if isinstance(v, datetime.datetime):
        return v.isoformat(" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    # bytes, Tagged and the like: whatever the library writes for them.
    text = dumps(_leaf(v))[:-1]
    if "\n" not in text:
        return text
    head, *rest = text.split("\n")
    if head[-1:] in ("|", ">") or head.endswith(("|-", "|+", ">-", ">+")):
        # A block scalar (binary data): its lines sit two columns in.
        return head, [" " * body + ln[2:] if ln.startswith("  ") else ln for ln in rest]
    # A tagged collection: its entries go under the tag, in the library's own layout.
    shift = " " * (parent + lay.offset if rest[0].startswith("-") else body)
    return head, [shift + ln if ln else ln for ln in rest]


def _styled(s: str, style: str, parent: int, body: int) -> Any:
    if style == "|":
        return literal(s, parent, body)
    if style == ">":
        return folded(s, parent, body)
    if style == '"':
        return double_quoted(s)
    if style == "'":
        if _PLAIN_BAD.search(s) or "\n" in s:
            return double_quoted(s)
        return "'" + s.replace("'", "''") + "'"
    # Plain: only when the library would not quote it.
    t = _inline_str(s)
    return t


# ---- documents -----------------------------------------------------------------------------------


def _x(c: Any) -> Any:
    return getattr(c, "_x", None)


def _flow_wanted(c: Any, lay: Layout) -> bool:
    x = _x(c)
    if x is not None and x.flow is not None:
        return x.flow
    if lay.flow is None:
        items = c.values() if isinstance(c, Mapping) else c
        return not any(_is_collection(i) for i in items)
    return lay.flow


def _flow_leaf(v: Any) -> str:
    """One scalar as it may stand inside a flow collection."""
    if isinstance(v, datetime.datetime):
        return v.isoformat(" ")
    if isinstance(v, datetime.date):
        return v.isoformat()
    v = _leaf(v)
    # The library writes the scalar for a place where flow indicators matter.
    return dumps([v], flow=True)[1:-2]


def _flow_text(c: Any) -> str:
    """``c`` in flow style on one line, written with an explicit stack."""
    out: list[str] = []
    path: set[int] = set()
    stack: list[list] = []

    def open_(node: Any) -> None:
        if id(node) in path:
            raise ValueError("circular reference")
        path.add(id(node))
        if isinstance(node, Mapping):
            out.append("{")
            stack.append([iter(node.items()), "}", True, node, True])
        else:
            out.append("[")
            stack.append([iter(node), "]", False, node, True])

    open_(c)
    while stack:
        fr = stack[-1]
        item = next(fr[0], _END)
        if item is _END:
            out.append(fr[1])
            path.discard(id(fr[3]))
            stack.pop()
            continue
        if not fr[4]:
            out.append(", ")
        fr[4] = False
        if fr[2]:
            k, v = item
            out.append(_flow_text(k) if _is_collection(k) else _flow_leaf(k))
            out.append(": ")
        else:
            v = item
        if _is_collection(v):
            if v:
                open_(v)
            else:
                out.append("{}" if isinstance(v, Mapping) else "[]")
        else:
            out.append(_flow_leaf(v))
    return "".join(out)


def fast_ok(value: Any, lay: Layout) -> bool:
    """True when ``oneyaml.dumps`` writes exactly what this module would."""
    if lay.flow is not False or lay.seq_indent != 2 or lay.offset != 0:
        return False
    if lay.map_indent != 2 and not 1 <= lay.map_indent <= 16:
        return False
    seen = {id(value)}
    stack = [value]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            x = getattr(n, "_x", None)
            if x is not None and (x.pending() or x.flow is not None):
                return False
            kids: Any = n.values()
            for k in n:
                if type(k) not in (str, int, float, bool):
                    return False
        elif isinstance(n, list):
            x = getattr(n, "_x", None)
            if x is not None and (x.pending() or x.flow is not None):
                return False
            kids = n
        else:
            continue
        for v in kids:
            t = type(v)
            if t is dict or t is list or t.__name__ in ("CommentedMap", "CommentedSeq"):
                if id(v) not in seen:
                    seen.add(id(v))
                    stack.append(v)
            elif v is None:
                if lay.null_empty:
                    return False
            elif t is not str and t not in (int, float, bool):
                return False
    return True


def render_document(value: Any, lay: Layout, first: bool) -> str:
    """One document's text (without the stream-level ``---`` of the first, unless asked)."""
    head = ""
    if first and lay.version:
        head = f"%YAML {lay.version}\n"
    start = lay.explicit_start or not first or bool(head)
    marker = "---\n" if start else ""
    end = "...\n" if lay.explicit_end else ""
    if isinstance(value, (Mapping, list, tuple)) and value and not _flow_wanted(value, lay):
        body = _block(value, lay)
    else:
        if _is_collection(value):
            text = _flow_text(value) + "\n" if value else dumps(to_plain(value))
        else:
            r = scalar(value, lay, -1, 2)
            if isinstance(r, tuple):
                text = r[0] + "\n" + "".join(ln + "\n" for ln in r[1])
            else:
                text = (r if r != "" else "null") + "\n"
        if marker and not text.startswith(("|", ">")) and "\n" not in text[:-1]:
            marker = "--- "
        body = text
    return head + marker + body + end


class _Frame:
    """One collection being written: its entries, where they sit, and what was set on it."""

    __slots__ = ("content", "dash", "ind", "is_map", "it", "node", "pend", "x")

    def __init__(self, node: Any, col: int, pend: str, content: int = 0) -> None:
        self.node = node
        self.is_map = isinstance(node, Mapping)
        self.it = iter(node.items()) if self.is_map else enumerate(node)
        self.ind = " " * col  # indentation of the entries
        self.dash = col
        self.content = content  # column of what follows a "- "
        self.pend = pend  # what the first entry's line starts with ("" when a "- " is open)
        self.x = _x(node)


def _lines(out: list[str], lines: list[str] | None) -> None:
    if lines:
        out.extend(ln + "\n" for ln in lines)


def _block(root: Any, lay: Layout) -> str:
    out: list[str] = []
    x = _x(root)
    if x is not None:
        _lines(out, x.start)
    mi, si, off = lay.map_indent, max(lay.seq_indent, 2), lay.offset
    stack: list[_Frame] = []
    onpath: set[int] = set()

    def push_seq(node: Any, dash: int, content: int, pend: str) -> None:
        stack.append(_Frame(node, dash, pend, max(content, dash + 2)))

    if isinstance(root, Mapping):
        stack.append(_Frame(root, 0, ""))
    else:
        push_seq(root, off, si, " " * off)
    onpath.add(id(root))
    while stack:
        f = stack[-1]
        entry = next(f.it, _END)
        if entry is _END:
            stack.pop()
            onpath.discard(id(f.node))
            continue
        key, v = entry
        if _is_collection(v) and id(v) in onpath:
            raise ValueError("circular reference")
        x, ind = f.x, f.ind
        prefix = ind if f.pend is None else f.pend
        f.pend = None
        if x is not None:
            before = x.before.get(key)
            if before:
                if prefix != ind:  # a "- " is open: the comment goes under it
                    out.append("\n")
                    prefix = ind
                _lines(out, before)
            eol = x.eol.get(key)
            after = x.after.get(key)
        else:
            eol = after = None
        eol = f"  {eol}" if eol else ""
        col = f.dash
        if f.is_map:
            lead = prefix + _key_text(key, lay) + ":"
            sep = " "
            parent, body = col, col + mi
        else:
            lead = prefix + "-"
            sep = " " * (f.content - col - 1)
            parent, body = col, f.content
        if _is_collection(v) and v and not _flow_wanted(v, lay):
            cx = _x(v)
            is_map = isinstance(v, Mapping)
            if f.is_map:
                out.append(lead + eol + "\n")
                _lines(out, after)
                after = None
                if cx is not None:
                    _lines(out, cx.start)
                onpath.add(id(v))
                if is_map:
                    stack.append(_Frame(v, col + mi, " " * (col + mi)))
                else:
                    push_seq(v, col + off, col + si, " " * (col + off))
            else:
                # The child starts on the line of the dash.
                if (
                    not is_map
                ):  # an inner sequence: its dash sits at the outer content column + offset
                    sep = " " * max(f.content + off - col - 1, 1)
                out.append(lead + sep + eol)
                pend = ""
                if (cx is not None and cx.start) or after:
                    out.append("\n")
                    if cx is not None:
                        _lines(out, cx.start)
                    _lines(out, after)
                    after = None
                    pend = " " * f.content
                onpath.add(id(v))
                if is_map:
                    stack.append(_Frame(v, f.content, pend))
                else:
                    push_seq(v, f.content + off, f.content + si, pend)
            continue
        if _is_collection(v):
            text = ("{}" if isinstance(v, Mapping) else "[]") if not v else _flow_text(v)
            out.append(lead + sep + text + eol + "\n")
        else:
            r = scalar(v, lay, parent, body)
            if isinstance(r, tuple):
                out.append(lead + sep + r[0] + eol + "\n")
                out.extend(r[1][i] + "\n" for i in range(len(r[1])))
            elif r == "":
                out.append(lead + eol + "\n")
            else:
                out.append(lead + sep + r + eol + "\n")
        _lines(out, after)
    return "".join(out)


def _key_text(k: Any, lay: Layout) -> str:
    if _is_collection(k):
        return _flow_text(k)
    r = scalar(k, lay, 0, 0, key=True)
    return r if isinstance(r, str) else double_quoted(str(k))
