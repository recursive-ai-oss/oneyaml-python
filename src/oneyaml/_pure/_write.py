"""Writing values as YAML text for edits: scalars, block and flow collections.

This is the small writer an edit needs to put a new value into a document. It takes the layout
(indentation step, line break, quote style) from the surrounding text, writes strings the way
the document writes them when it can, and the nearest style that works when it cannot. Nothing
here recurses on the value's nesting: collections are written with an explicit stack.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Mapping
from typing import Any

from .._types import Tagged
from ._resolve import KEYWORDS, NUMBER_START, WORD_START
from ._resolve import number as _number

_YAML = "tag:yaml.org,2002:"

# What a plain scalar must not hold or start with.
_PLAIN_BAD = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]"  # controls, breaks, tab
    r"|: |:$| #|^[ ]|[ ]$"  # key, comment, edge blanks
    r"|^[,\[\]{}#&*!|>'\"%@`]"  # indicators that open another construct
    r"|^[-?:](?:[ ]|$)"  # block indicators
    r"|^(?:---|\.\.\.)"  # document markers
)
_FLOW_BAD = re.compile(r"[,\[\]{}:]")
_SQ_BAD = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
_DQ_NEEDS = re.compile('[\\\\"\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]')
_DQ_NAMES = {
    "\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r", "\0": "\\0",
    "\x07": "\\a", "\x08": "\\b", "\x0b": "\\v", "\x0c": "\\f", "\x1b": "\\e",
}  # fmt: skip
_BLOCK_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")


def _dq_escape(m: re.Match[str]) -> str:
    c = m.group()
    r = _DQ_NAMES.get(c)
    if r is not None:
        return r
    o = ord(c)
    return f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}"


def double_quoted(s: str) -> str:
    return '"' + _DQ_NEEDS.sub(_dq_escape, s) + '"'


def single_quoted(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def single_ok(s: str) -> bool:
    return _SQ_BAD.search(s) is None


def resolves_to_str(s: str) -> bool:
    """True when the plain scalar ``s`` reads as a string under the Core schema."""
    c = s[:1]
    if c in NUMBER_START:
        try:
            return type(_number(s, None, {})) is str
        except Exception:  # noqa: BLE001 - a number too long to read is a number
            return False
    if c in WORD_START or not c:
        return s not in KEYWORDS
    return True


def plain_ok(s: str, flow: bool = False, key: bool = False, tagged: bool = False) -> bool:
    """Whether the string ``s`` can be written plain and read back as exactly ``s``."""
    if not s or _PLAIN_BAD.search(s):
        return False
    if flow and _FLOW_BAD.search(s):
        return False
    if key and len(s) > 1024:
        return False
    return tagged or resolves_to_str(s)


def tag_text(tag: str) -> str:
    """A tag as it is written in a document."""
    if tag.startswith(_YAML):
        return "!!" + tag[len(_YAML) :]
    if tag.startswith("!"):
        return tag
    return "!<" + tag + ">"


def float_text(v: float) -> str:
    if v != v:
        return ".nan"
    if math.isinf(v):
        return ".inf" if v > 0 else "-.inf"
    return repr(v)


def scalar_inline(
    v: Any, hint: str | None = None, flow: bool = False, key: bool = False, tagged: bool = False
) -> tuple[str, str, str]:
    """``(text, style, value text)`` of a scalar written on one line.

    ``hint`` is the style the surroundings use for strings (``:`` ``'`` ``"``)."""
    if v is None:
        return "null", ":", "null"
    t = type(v)
    if t is bool:
        s = "true" if v else "false"
        return s, ":", s
    if t is int:
        s = str(v)
        return s, ":", s
    if t is float:
        s = float_text(v)
        return s, ":", s
    if isinstance(v, bool):
        s = "true" if v else "false"
        return s, ":", s
    if isinstance(v, int):
        s = str(int(v))
        return s, ":", s
    if isinstance(v, float):
        s = float_text(float(v))
        return s, ":", s
    if isinstance(v, (bytes, bytearray, memoryview)):
        s = base64.b64encode(bytes(v)).decode("ascii")
        return "!!binary " + s, ":", s
    if isinstance(v, str):
        s = str(v)
        if hint == '"':
            return double_quoted(s), '"', s
        if hint == "'":
            if single_ok(s):
                return single_quoted(s), "'", s
            return double_quoted(s), '"', s
        if plain_ok(s, flow, key, tagged):
            return s, ":", s
        if single_ok(s):
            return single_quoted(s), "'", s
        return double_quoted(s), '"', s
    raise TypeError(f"cannot write a {type(v).__name__} as YAML")


def block_scalar(
    s: str,
    style: str,
    ci: int,
    nl: str,
    chomp: str | None,
    par: int,
    indicator: bool,
) -> tuple[str, int] | None:
    """A literal (``|``) or folded (``>``) scalar: its header and content lines, without the
    break that ends the last line. Returns ``(text, trailing newline count)`` or None when the
    style cannot hold the string.

    ``ci`` is the content indentation, ``par`` the indentation of the parent collection,
    ``chomp`` the old header's chomping indicator (a ``+`` stays while the value allows it) and
    ``indicator`` whether the old header carried an indentation indicator."""
    if _BLOCK_BAD.search(s) or "\r" in s:
        return None
    body = s.rstrip("\n")
    t = len(s) - len(body)
    if not body:
        if s:
            return None
        head = style + "-"
        return head, 0
    if t == 0:
        ch = "-"
    elif t == 1:
        ch = "+" if chomp == "+" else ""
    else:
        ch = "+"
    lines = body.split("\n")
    for line in lines:
        if line and line.strip(" \t") == "":
            return None
    first = next(line for line in lines if line)
    need = first[0] in " \t"
    head = style
    if need or indicator:
        n = ci - par
        if not 1 <= n <= 9:
            return None
        head += str(n)
    head += ch
    pad = " " * ci
    out = [head]
    if style == "|":
        for line in lines:
            out.append(nl + (pad + line if line else ""))
    else:
        if any(line[:1] in " \t" for line in lines if line) or lines[0] == "":
            return None
        parts = re.split(r"(\n+)", body)
        out.append(nl + pad + parts[0])
        for i in range(1, len(parts), 2):
            out.append(nl * (len(parts[i]) + 1) + pad + parts[i + 1])
    return "".join(out), t


def _is_map(v: Any) -> bool:
    return isinstance(v, Mapping)


def _is_seq(v: Any) -> bool:
    return isinstance(v, (list, tuple))


def is_collection(v: Any) -> bool:
    if isinstance(v, Tagged):
        v = v.value
    return isinstance(v, (list, tuple, Mapping))


def _is_tagged_scalar_wrapper(v: Any) -> bool:
    return isinstance(v, Tagged) and not is_collection(v)


class Layout:
    """How new block text is laid out."""

    __slots__ = ("hint", "key_hint", "nl", "seq_off", "step")

    def __init__(
        self,
        step: int = 2,
        seq_off: int | None = None,
        nl: str = "\n",
        hint: str | None = None,
        key_hint: str | None = None,
    ) -> None:
        self.step = step
        self.seq_off = step if seq_off is None else seq_off
        self.nl = nl
        self.hint = hint
        self.key_hint = key_hint


def _key_text(k: Any, lay: Layout) -> tuple[str, bool]:
    """A mapping key as text, and whether it needs the explicit ``? `` form (it is too long to
    be an implicit key)."""
    if isinstance(k, str) and len(k) > 1024:
        return scalar_inline(k, lay.key_hint, False, False)[0], True
    if isinstance(k, (str, int, float, bool)) or k is None:
        return scalar_inline(k, lay.key_hint, False, True)[0], False
    raise TypeError(f"cannot write a {type(k).__name__} as a mapping key")


def flow_text(v: Any, hint: str | None = None) -> str:
    """A value in flow style on one line."""
    out: list[str] = []
    # Work items: strings are output as they are; ("v", value) are still to be written; a
    # tuple ("close", text) ends a collection.
    work: list[Any] = [("v", v)]
    while work:
        w = work.pop()
        if type(w) is str:
            out.append(w)
            continue
        val = w[1]
        tag = ""
        if isinstance(val, Tagged):
            tag = tag_text(val.tag) + " "
            val = val.value
        if _is_map(val):
            if not val:
                out.append(tag + "{}")
                continue
            out.append(tag + "{")
            items = list(val.items())
            seq: list[Any] = []
            for i, (k, x) in enumerate(items):
                seq.append(("k", k, i > 0))
                seq.append(("v", x))
            seq.append("}")
            work.extend(reversed(_flow_map_items(seq, hint)))
        elif _is_seq(val):
            if not val:
                out.append(tag + "[]")
                continue
            out.append(tag + "[")
            seq2: list[Any] = []
            for i, x in enumerate(val):
                if i:
                    seq2.append(", ")
                seq2.append(("v", x))
            seq2.append("]")
            work.extend(reversed(seq2))
        else:
            text = scalar_inline(val, hint, True)[0]
            out.append(tag + text if tag else text)
    return "".join(out)


def _flow_map_items(seq: list[Any], hint: str | None) -> list[Any]:
    out: list[Any] = []
    for w in seq:
        if type(w) is tuple and w[0] == "k":
            if w[2]:
                out.append(", ")
            out.append(scalar_inline(w[1], hint, True, True)[0] + ": ")
        else:
            out.append(w)
    return out


def block_text(
    v: Any,
    indent: int,
    lay: Layout,
    inline_first: bool = False,
    following: int = 1,
    after: int | None = None,
) -> str:
    """A non-empty collection in block style; lines are joined with the layout's break and
    the last line has none. With ``inline_first`` the first line carries no indentation (it
    follows a ``- `` on the same line).

    ``following`` is how many line breaks the text after this one supplies at once: a keep
    scalar (``|+``) ending the text writes the rest of the blank lines it needs. ``after`` is the
    indentation of a comment line that follows: a block scalar that ends the text and is not
    indented deeper than it would swallow the comment."""
    nl = lay.nl
    step = lay.step
    out: list[str] = []
    # A frame is [entries, next index, indent, is_map]
    stack: list[list[Any]] = []
    cur: str | None = None  # the text the next entry's line starts with, instead of indentation
    top = v
    if isinstance(top, Tagged):
        top = top.value
    stack.append([_entries(top), 0, indent, _is_map(top)])
    if inline_first:
        cur = ""
    while stack:
        fr = stack[-1]
        ents, i, ind, is_map = fr
        if i >= len(ents):
            stack.pop()
            continue
        fr[1] = i + 1
        ent = ents[i]
        lead = cur if cur is not None else " " * ind
        cur = None
        if is_map:
            k, x = ent
            ktxt, explicit = _key_text(k, lay)
            if explicit:
                out.append(lead + "? " + ktxt)
                line = " " * ind + ":"
            else:
                line = lead + ktxt + ":"
        else:
            x = ent
            line = lead + "-"
        tag = ""
        if isinstance(x, Tagged):
            tag = tag_text(x.tag)
            x = x.value
        if _is_map(x) and x:
            if is_map or tag:
                out.append(line + (" " + tag if tag else ""))
            else:
                cur = line + " "
            stack.append([_entries(x), 0, ind + (step if is_map else 2), True])
            continue
        if _is_seq(x) and x:
            if is_map or tag:
                out.append(line + (" " + tag if tag else ""))
            else:
                cur = line + " "
            stack.append([_entries(x), 0, ind + (lay.seq_off if is_map else 2), False])
            continue
        if _is_map(x) or _is_seq(x):
            out.append(line + " " + (tag + " " if tag else "") + ("{}" if _is_map(x) else "[]"))
            continue
        if isinstance(x, str) and "\n" in x and not tag:
            ci = ind + (step if is_map else 2)
            blk = block_scalar(x, "|", ci, nl, None, ind, False)
            if (
                blk is not None
                and after is not None
                and after >= ci
                and all(f[1] >= len(f[0]) for f in stack)
            ):
                blk = None
            if blk is not None:
                text, t = blk
                extra = t - 1 if t > 1 else 0
                if t > 1 and all(f[1] >= len(f[0]) for f in stack):
                    extra = t - following  # this scalar ends the text
                if extra >= 0:
                    out.append(line + " " + text + nl * extra)
                    continue
        text, _style, _val = scalar_inline(x, lay.hint)
        out.append(line + " " + (tag + " " if tag else "") + text)
    return nl.join(out)


def _entries(v: Any) -> list:
    if _is_map(v):
        return list(v.items())
    return list(v)
