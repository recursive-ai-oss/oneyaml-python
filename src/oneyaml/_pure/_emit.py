"""Write Python values as YAML text.

One walk with an explicit stack of frames, so no recursion follows the nesting of the value.
The native backend writes the same bytes by the same rules; the rules below are the contract.

Layout (block style): two-space indentation by default, ``key: value`` lines, a sequence under
a mapping key at the key's own column (``key:`` then ``- a``), nested sequences and mappings
inside a sequence entry on the entry's line (``- - a``, ``- a: 1``). Empty collections are
``[]`` and ``{}``. A key that is not a short scalar uses the explicit ``? key`` / ``: value``
form. A multi-line string is a literal block scalar where one reads back exactly, otherwise
double-quoted.

Strings are written plain only when every common reader reads the same string back: no
reading as null, boolean or number under the Core schema or YAML 1.1 (nor the forms readers
accept beyond either schema), and valid plain syntax for the place.
"""

from __future__ import annotations

import base64
import re
from itertools import chain
from typing import Any

from .._errors import LimitError
from .._types import FrozenMapping, Tagged, int_text

__all__ = ["MAX_DEPTH", "PATH_OPS", "dump"]

# Nesting of collections that is written. Output of a mapping chain grows with the square of
# its depth, and the reader's default depth limit is the same number.
MAX_DEPTH = 10_000

_MAP, _SEQ, _EXPL = 0, 1, 2
_BIN_TAG = "!!binary"
_MAX_IMPLICIT_KEY = 1024
_BASE64_LINE = 76

_FAST = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*(?: [A-Za-z0-9_./-]+)*").fullmatch
# Words that read as null, a boolean (either schema) or a 1.1 value; compared in lower case.
_RESERVED = frozenset(("y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"))
# Scalars that begin like a number (after signs, dots and underscores) and use only the
# characters numbers, timestamps and their variants use, or an infinity or nan.
_NUMBERISH = re.compile(
    r"[-+._]*[0-9][-+_.:,0-9a-fA-FxXoOTtZz ]*\Z|[-+]*\.(?:[iI][nN][fF]|[nN][aA][nN])"
).match
_INDICATOR = frozenset(" \t-?:,[]{}#&*!|>'\"%@`")
_FLOW_BAD = re.compile(r"[,\[\]{}:]").search
_NEEDS_ESCAPE = re.compile(
    "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029\\ufeff\\ufffe\\uffff\\ud800-\\udfff]"
).search
_ESCAPE_CHARS = re.compile('["\\\\\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029\\ufeff\\ufffe\\uffff]')
_SURROGATE = re.compile("[\\ud800-\\udfff]").search
_SPACE_BEFORE_NEWLINE = re.compile(r"[ \t]\n").search
_LINE_START = re.compile(r"^(?=[^\n])", re.M)
_NAMED_ESCAPES = {
    0: "\\0", 7: "\\a", 8: "\\b", 9: "\\t", 10: "\\n", 11: "\\v", 12: "\\f", 13: "\\r",
    27: "\\e", 34: '\\"', 92: "\\\\", 0x85: "\\N", 0x2028: "\\L", 0x2029: "\\P",
}  # fmt: skip
_TAG_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-#;/?:@&=+$_.~*'()"
)
_YAML_TAG = "tag:yaml.org,2002:"
# From this depth the containers on the current path are tracked, to refuse a value that
# contains itself. Shallower values, which are nearly all of them, pay nothing.
_TRACK_FROM = 64
_END = object()
# Reads of the stored slots of a Tagged and a FrozenMapping: not affected by what a subclass
# does with attribute lookup.
_TAG_GET = Tagged.tag.__get__
_VALUE_GET = Tagged.value.__get__
_ITEMS_GET = FrozenMapping._items.__get__
_LONE_SURROGATE = "a string holds a lone surrogate, which YAML cannot represent"
# Operations on the on-path set, counted so a test can check the work is linear.
PATH_OPS = 0


def _escape_one(m: re.Match) -> str:
    c = ord(m[0])
    r = _NAMED_ESCAPES.get(c)
    if r is not None:
        return r
    return f"\\x{c:02X}" if c < 0x100 else f"\\u{c:04X}"


def _double(s: str) -> str:
    return '"' + _ESCAPE_CHARS.sub(_escape_one, s) + '"'


def _plain_ok(s: str, flow: bool) -> bool:
    if s[0] in _INDICATOR or s[-1] in " \t:":
        return False
    if ": " in s or " #" in s or ":\t" in s or "\t#" in s:
        return False
    if s.startswith(("---", "...")) or s in ("<<", "="):
        return False
    if flow and _FLOW_BAD(s):
        return False
    return not (_NUMBERISH(s) or s.lower() in _RESERVED)


def _string(s: str, flow: bool, cpad: str | None) -> str:
    """A string as scalar text. ``cpad`` is where a block scalar's lines would go, or None
    when the place cannot hold one (a key, flow style, an explicit value)."""
    if _FAST(s) is not None:
        if len(s) > 5 or s.lower() not in _RESERVED:
            return s
        return "'" + s + "'"
    if not s:
        return "''"
    if _NEEDS_ESCAPE(s):
        if _SURROGATE(s):
            raise ValueError(_LONE_SURROGATE)
        return _double(s)
    if "\n" in s:
        if cpad is None or s[0] in " \t\n" or s[-1] in " \t" or _SPACE_BEFORE_NEWLINE(s):
            return _double(s)
        return _literal("|", s, cpad)
    if _plain_ok(s, flow):
        return s
    return "'" + s.replace("'", "''") + "'"


def _literal(header: str, s: str, cpad: str) -> str:
    """A literal block scalar without its final line break: the header, then each line at
    ``cpad``. The chomping indicator keeps exactly the string's trailing line breaks."""
    if s.endswith("\n"):
        body = s
        if s.endswith("\n\n"):
            header += "+"
    else:
        body = s + "\n"
        header += "-"
    return header + "\n" + _LINE_START.sub(cpad, body[:-1])


def _float(x: float) -> str:
    if x != x:
        return ".nan"
    if x == float("inf"):
        return ".inf"
    if x == float("-inf"):
        return "-.inf"
    r = repr(x)
    # A point in the mantissa and a signed exponent, so every reader sees a float.
    i = r.find("e")
    if i > 0 and "." not in r[:i]:
        r = r[:i] + ".0" + r[i:]
    return r


def _int(v: int) -> str:
    try:
        return int.__repr__(v)
    except ValueError:  # more digits than the interpreter converts
        return int_text(v)


def _tag(tag: Any) -> str:
    if not issubclass(type(tag), str):
        raise TypeError(f"a tag must be a str, not {type(tag).__name__}")
    tag = str.__str__(tag)
    if _SURROGATE(tag):
        raise ValueError(_LONE_SURROGATE)
    if not tag:
        raise ValueError("a tag cannot be empty")
    shorthand = tag.startswith(_YAML_TAG) and len(tag) > len(_YAML_TAG)
    local = tag.startswith("!") and len(tag) > 1
    if shorthand:
        body = tag[len(_YAML_TAG) :]
    elif local:
        body = tag[1:]
    else:
        body = tag
    # A comma is a URI character, but ends a shorthand tag in flow style: only the verbatim
    # form (global tags such as ``tag:example.com,2000:x``) keeps it.
    verbatim = not shorthand and not local
    out = []
    for ch in body:
        if ch in _TAG_SAFE or (verbatim and ch == ","):
            out.append(ch)
        elif ord(ch) < 0x80 and ch.isprintable():
            out.append(f"%{ord(ch):02X}")
        else:
            raise ValueError(f"a tag cannot hold the character {ch!r}")
    body = "".join(out)
    if shorthand:
        return "!!" + body
    if local:
        return "!" + body
    if tag == "!":
        return "!"
    return "!<" + body + ">"


def _binary(b: bytes, cpad: str | None) -> str:
    text = base64.b64encode(b).decode("ascii")
    if not text:
        return _BIN_TAG + " ''"
    if cpad is None or len(text) <= _BASE64_LINE:
        return _BIN_TAG + " " + text
    lines = [text[i : i + _BASE64_LINE] for i in range(0, len(text), _BASE64_LINE)]
    return _BIN_TAG + " |\n" + "\n".join(cpad + ln for ln in lines)


def _unsupported(v: Any) -> TypeError:
    return TypeError(f"Object of type {type(v).__name__} is not YAML serializable")


_BYTES_LIKE = (bytes, bytearray, memoryview)


def _tagged(v: Any, flow: bool, cpad: str | None) -> Any:
    """A value whose real type is a Tagged. Its content is read as a plain node: tags do not
    nest, and nothing here recurses."""
    tag = _tag(_TAG_GET(v))
    inner = _VALUE_GET(v)
    t = type(inner)
    if issubclass(t, Tagged) or issubclass(t, _BYTES_LIKE):
        raise ValueError("a node cannot carry two tags")
    n = _plain(inner, flow, cpad)
    return tag + " " + n if type(n) is str else (n[0], n[1], tag)


def _node(v: Any, flow: bool, cpad: str | None) -> Any:
    """``v`` as scalar text, or ``(kind, obj, tag)`` for a collection: ``obj`` is a dict or a
    sequence, ``tag`` the tag text or None."""
    t = type(v)
    if t is str:
        return _string(v, flow, cpad)
    if t is int:
        return _int(v)
    if t is dict:
        return (_MAP, v, None)
    if t is list or t is tuple:
        return (_SEQ, v, None)
    if v is None:
        return "null"
    if t is bool:
        return "true" if v else "false"
    if t is float:
        return _float(v)
    # Whether it is a Tagged is decided here, from the real type, once.
    if t is Tagged or issubclass(t, Tagged):
        return _tagged(v, flow, cpad)
    return _plain(v, flow, cpad)


def _plain(v: Any, flow: bool, cpad: str | None) -> Any:
    t = type(v)
    if v is None:
        return "null"
    if t is bool:
        return "true" if v else "false"
    if t is int:
        return _int(v)
    if t is str:
        return _string(v, flow, cpad)
    if t is float:
        return _float(v)
    if t is dict:
        return (_MAP, v, None)
    if t is list or t is tuple:
        return (_SEQ, v, None)
    if t is bytes:
        return _binary(v, cpad)
    if t is FrozenMapping:
        return (_MAP, _ITEMS_GET(v), None)
    # Subclasses are read as their base type's data, never through overridden methods, as in
    # the native backend.
    if issubclass(t, str):
        return _string(str.__str__(v), flow, cpad)
    if issubclass(t, int):
        return _int(v)
    if issubclass(t, float):
        return _float(float.__float__(v))
    if issubclass(t, dict):
        return (_MAP, v, None)
    if issubclass(t, (list, tuple)):
        return (_SEQ, v, None)
    if issubclass(t, FrozenMapping):
        return (_MAP, _ITEMS_GET(v), None)
    if issubclass(t, _BYTES_LIKE):
        # Through the buffer protocol: the raw bytes, whatever the object's own methods say.
        return _binary(memoryview(v).tobytes(), cpad)
    raise _unsupported(v)


def _push(frames: list, path: set, frame: list) -> None:
    """Open a frame: check the depth, and the path once it is deep enough to be worth tracking."""
    global PATH_OPS
    depth = len(frames) + 1
    if depth > MAX_DEPTH:
        raise LimitError(f"the value nests deeper than {MAX_DEPTH} levels", category="limit")
    if depth >= _TRACK_FROM:
        if depth == _TRACK_FROM:
            # The path so far was not tracked: take it in now.
            path.clear()
            for f in frames:
                o = f[5]
                if o is not None:
                    PATH_OPS += 1
                    if id(o) in path:
                        raise ValueError("Circular reference detected")
                    path.add(id(o))
        o = frame[5]
        if o is not None:
            PATH_OPS += 1
            if id(o) in path:
                raise ValueError("Circular reference detected")
            path.add(id(o))
    frames.append(frame)


def _pop(frames: list, path: set) -> None:
    global PATH_OPS
    if len(frames) >= _TRACK_FROM:
        o = frames[-1][5]
        if o is not None:
            PATH_OPS += 1
            path.discard(id(o))
    frames.pop()


def _empty(kind: int, tag: str | None) -> str:
    text = "{}" if kind == _MAP else "[]"
    return f"{tag} {text}" if tag else text


_BAD_PAIR = "expected a (key, value) pair"


def _pairs(entries):
    """The entries of a mapping other than a dict, each checked to be exactly a key and a value
    (any iterable of two items, as ``k, v = entry`` takes)."""
    for e in entries:
        if type(e) is tuple:
            if len(e) != 2:
                raise ValueError(_BAD_PAIR)
            yield e
            continue
        it = iter(e)
        try:
            k = next(it)
            v = next(it)
        except StopIteration:
            raise ValueError(_BAD_PAIR) from None
        for _ in it:
            raise ValueError(_BAD_PAIR)
        yield k, v


def _frame(kind: int, obj: Any, col: int, inline: bool) -> list | None:
    """The frame for a collection's entries, or None when it has none. A dict, list or tuple
    is empty by its own length; a subclass by its first entry, since its ``__len__`` may say
    anything."""
    t = type(obj)
    it = iter(obj) if kind != _MAP else (iter(obj.items()) if t is dict else _pairs(obj.items()))
    if not (t is dict or t is list or t is tuple):
        first = next(it, _END)
        if first is _END:
            return None
        it = chain((first,), it)
    elif not len(obj):
        return None
    # Frame: kind, iterator, padding of its lines, column, whether its first line is already
    # started, the container (kept alive while open; its identity is checked on the path).
    return [kind, it, " " * col, col, inline, obj]


def _open(c, out, head, keyed, col, indent):
    """Start a collection after ``head`` (the text already on the line: ``key:``, ``-``,
    ``?``). Returns the new frame, or None for an empty collection (written whole)."""
    kind, obj, tag = c
    if keyed:
        ncol = col + indent if tag or kind == _MAP else col
        inline = False
    else:
        ncol = col + 2
        inline = not tag
    frame = _frame(kind, obj, ncol, inline)
    if frame is None:
        out.append(f"{head} {_empty(kind, tag)}\n")
        return None
    if tag:
        out.append(f"{head} {tag}\n")
    elif keyed:
        out.append(head + "\n")
    else:
        out.append(head + " ")
    return frame


def _block(root: Any, out: list, indent: int) -> None:
    """Write one document's root node in block style."""
    c = _node(root, False, " " * indent)
    if type(c) is str:
        out.append(c + "\n")
        return
    kind, obj, tag = c
    root_frame = _frame(kind, obj, 0, True)
    if root_frame is None:
        out.append(_empty(kind, tag) + "\n")
        return
    if tag:
        out.append(tag + "\n")
    frames: list = []
    path: set = set()
    _push(frames, path, root_frame)
    kcache: dict = {}
    while frames:
        cur = frames[-1]
        kind, it, pad, col, inline, _ = cur
        lead = "" if inline else pad
        cpad = " " * (col + indent)
        descend = None
        if kind == _MAP:
            for k, v in it:
                if type(k) is str:
                    kt = kcache.get(k)
                    if kt is None:
                        kt = _string(k, False, None)
                        if len(kt) > _MAX_IMPLICIT_KEY:
                            kt = False
                        kcache[k] = kt
                else:
                    kt = _node(k, False, None)
                    if type(kt) is not str or len(kt) > _MAX_IMPLICIT_KEY:
                        kt = False
                if kt is False:
                    # An explicit entry stands in for this one, at the same column.
                    descend = [_EXPL, iter((("?", k), (":", v))), pad, col, not lead, None]
                    break
                t = type(v)
                if t is str:
                    out.append(f"{lead}{kt}: {_string(v, False, cpad)}\n")
                    lead = pad
                    continue
                if t is int:
                    out.append(f"{lead}{kt}: {_int(v)}\n")
                    lead = pad
                    continue
                n = _node(v, False, cpad)
                if type(n) is str:
                    out.append(f"{lead}{kt}: {n}\n")
                    lead = pad
                    continue
                descend = _open(n, out, f"{lead}{kt}:", True, col, indent)
                lead = pad
                if descend is not None:
                    break
        elif kind == _SEQ:
            for v in it:
                if type(v) is str:
                    out.append(f"{lead}- {_string(v, False, cpad)}\n")
                    lead = pad
                    continue
                n = _node(v, False, cpad)
                if type(n) is str:
                    out.append(f"{lead}- {n}\n")
                    lead = pad
                    continue
                descend = _open(n, out, f"{lead}-", False, col, indent)
                lead = pad
                if descend is not None:
                    break
        else:
            for pfx, v in it:
                n = _node(v, False, None)
                if type(n) is str:
                    out.append(f"{lead}{pfx} {n}\n")
                    lead = pad
                    continue
                descend = _open(n, out, f"{lead}{pfx}", False, col, indent)
                lead = pad
                if descend is not None:
                    break
        if descend is not None:
            cur[4] = False  # the line it started is finished
            _push(frames, path, descend)
        else:
            _pop(frames, path)


def _flat(it):
    """A mapping's entries as a flat run of keys and values; each entry unpacks as ``k, v``."""
    for k, v in it:
        yield k
        yield v


def _flow(root: Any, out: list) -> None:
    """Write one document's root node in flow style, on one line."""
    c = _node(root, True, None)
    if type(c) is str:
        out.append(c + "\n")
        return
    parts: list = []
    frames: list = []
    path: set = set()

    def begin(c):
        kind, obj, tag = c
        frame = _frame(kind, obj, 0, False)
        if frame is None:
            parts.append(_empty(kind, tag))
            return None
        if kind == _MAP:
            frame[1] = _flat(frame[1])
        if tag:
            parts.append(tag + " ")
        parts.append("{" if kind == _MAP else "[")
        return frame

    first = begin(c)
    if first is None:
        out.append(parts[0] + "\n")
        return
    _push(frames, path, first)
    # Per frame, the items written so far: between entries ", ", between a key and its value ": ".
    counts = [0]
    while frames:
        cur = frames[-1]
        kind, it = cur[0], cur[1]
        n = counts[-1]
        descend = None
        for v in it:
            if n:
                parts.append(", " if kind == _SEQ or not n & 1 else ": ")
            n += 1
            c = _node(v, True, None)
            if type(c) is str:
                parts.append(c)
                continue
            descend = begin(c)
            if descend is not None:
                break
        counts[-1] = n
        if descend is not None:
            _push(frames, path, descend)
            counts.append(0)
        else:
            _pop(frames, path)
            counts.pop()
            parts.append("}" if kind == _MAP else "]")
    parts.append("\n")
    out.append("".join(parts))


def dump(objs: list, opts: tuple, multi: bool) -> str:
    """Write ``objs`` as YAML, one document each. ``opts`` is ``(indent, flow, explicit_start)``.

    ``multi`` only tells a stream of documents from one; the text is the same.
    """
    indent, flow, start = opts
    if type(indent) is not int:
        raise TypeError("indent must be an int")
    if not 1 <= indent <= 16:
        raise ValueError("indent must be between 1 and 16")
    out: list = []
    for i, obj in enumerate(objs):
        if i or start:
            out.append("---\n")
        if flow:
            _flow(obj, out)
        else:
            _block(obj, out, indent)
    return "".join(out)
