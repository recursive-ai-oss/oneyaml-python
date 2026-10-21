"""The ``Handle`` of the pure backend: one parsed stream behind the backend interface.

Two kinds of token address a node. Calls that read or edit one place (``kind``, ``value``,
``set``, ...) take the tokens of a Python view: a ``str`` is a key that is that string (or, when
no string key fits, a key that is not a string and reads as that text), an ``int``, ``float``,
``bool`` or ``None`` is the key of exactly that value, a ``tuple`` or ``FrozenMapping`` is a
collection key, and an ``int`` or a string of digits is an index into a sequence. Pointers
(``pointer``, ``patch``, ``merge_patch``) are RFC 6901 text: a token is matched against the
text of the keys, and two keys with one text are ambiguous.
"""

from __future__ import annotations

import re
from typing import Any

from .._errors import PatchError, YAMLError, make_error
from .._limits import _NONE
from .._types import FrozenMapping, Tagged
from . import _construct, _prepare
from . import _edit as E
from . import _patch as P
from . import _tree as T
from . import _write as W
from ._prepare import Prepared
from ._tree import K_ALIAS, K_MAP, K_SCALAR, K_SEQ, KIND_NAMES, Doc, Parsed

_STYLE = {":": "plain", "'": "single-quoted", '"': "double-quoted", "|": "literal", ">": "folded"}
_SURROGATE = re.compile("[\ud800-\udfff]").search
_TRAILING = re.compile(r"[ \t]*(#[^\r\n]*)").match
_TOKEN_TYPES = (str, int, float, bool, tuple, FrozenMapping, type(None))


def _tokens(path: Any) -> list:
    """The tokens of ``path``, checked."""
    if isinstance(path, (str, bytes, bytearray)) or not hasattr(path, "__iter__"):
        raise TypeError(f"a path is a list of tokens, not {type(path).__name__}")
    out = list(path)
    for t in out:
        if not isinstance(t, _TOKEN_TYPES):
            raise TypeError("a path token is a str, int, float, bool, None, tuple or FrozenMapping")
        if type(t) is not str and isinstance(t, str):
            out[out.index(t)] = str(t)  # a str subclass is its text
    return out


def _fail(exc: PatchError) -> Exception:
    """An edit error of a view call: the plain library error with the edit's category."""
    return make_error(exc.message, exc.category or "other")


class Handle:
    """A lossless stream. Documents are addressed by index, nodes by a list of tokens."""

    __slots__ = ("_p", "_version")

    def __init__(self, parsed: Parsed) -> None:
        self._p = parsed
        self._version = 0

    # ---- identity -----------------------------------------------------------------------

    @property
    def encoding(self) -> str:
        return self._p.codec

    def version(self) -> int:
        """Bumps on every edit that can move or replace a collection."""
        return self._version

    def count(self) -> int:
        return len(self._p.docs)

    def emit(self) -> bytes:
        p = self._p
        text = "".join([d.text for d in p.docs]) if p.docs else p.tail
        return p.bom + text.encode(p.codec, "surrogatepass")

    # ---- reads --------------------------------------------------------------------------

    def _doc(self, d: Any) -> Doc:
        if not isinstance(d, int) or isinstance(d, bool):
            raise TypeError("the document index must be an int")
        docs = self._p.docs
        if not 0 <= d < len(docs):
            raise IndexError(f"no document {d} in the stream")
        return docs[d]

    def _find(
        self, d: Any, path: Any, follow: bool = True, from_end: bool = False
    ) -> tuple[Doc, E.Loc]:
        toks = _tokens(path)
        doc = self._doc(d)
        if not doc.kind:
            raise KeyError("the document is empty")
        try:
            return doc, E.locate(doc, toks, True, follow, from_end)
        except E.ThroughAlias:
            raise make_error(
                "the path goes through an alias; edit the anchored node instead", "refused-alias"
            ) from None
        except PatchError as exc:
            raise _fail(exc) from None

    def kind(self, d: int, path: Any) -> str:
        doc, loc = self._find(d, path)
        return KIND_NAMES[doc.kind[T.follow(doc, loc.row)]]

    def value(self, d: int, path: Any) -> Any:
        doc, loc = self._find(d, path)
        return T.value_of(doc, loc.row, self._p.limits)

    def read(self, d: int, path: Any) -> tuple[str, Any]:
        """``(kind, value)``: the value of a scalar, ``None`` for a collection (a view reads it
        by path). One walk where ``kind`` and ``value`` would make two."""
        doc, loc = self._find(d, path)
        k = doc.kind[T.follow(doc, loc.row)]
        if k in (K_MAP, K_SEQ):
            return KIND_NAMES[k], None
        return KIND_NAMES[k], T.value_of(doc, loc.row, self._p.limits)

    def length(self, d: int, path: Any) -> int:
        doc, loc = self._find(d, path)
        r = T.follow(doc, loc.row)
        if doc.kind[r] in (K_MAP, K_SEQ):
            return T.count_items(doc, r)
        raise TypeError("a scalar has no length")

    def keys(self, d: int, path: Any) -> list:
        """The keys of a mapping as Python values (collection keys frozen), in document order."""
        doc, loc = self._find(d, path)
        r = T.follow(doc, loc.row)
        if doc.kind[r] != K_MAP:
            raise TypeError("only a mapping has keys")
        digits = self._p.limits._native[8]
        cap = None if digits >= _NONE else digits
        try:
            return [T.key_value(doc, k, cap) for k, _v in E.members(doc, r)]
        except T._Fail as exc:
            raise self._error(doc, exc) from None

    def _error(self, doc: Doc, exc: T._Fail) -> Exception:
        line, col = T.linecol(doc.text, exc.offset)
        return make_error(exc.message, exc.category, line, col, exc.offset)

    def info(self, d: int, path: Any) -> tuple:
        """``(style, tag, anchor, comment, line, column)`` of the node itself."""
        doc, loc = self._find(d, path)
        r = loc.row
        k = doc.kind[r]
        if k == K_ALIAS:
            style = "alias"
        elif k == K_SCALAR:
            style = _STYLE[doc.sty[r]]
        else:
            style = "block" if doc.sty[r] == "b" else "flow"
        tag = doc.tag[r] if k != K_ALIAS else None
        anchor = doc.anc[r] if k != K_ALIAS else None
        comment = _comment(doc, loc)
        pos = doc.ps[r] if k != K_ALIAS else doc.st[r]
        before = "".join([x.text for x in self._p.docs[:d]])
        line, col = T.linecol(before + doc.text, len(before) + pos)
        return style, tag, anchor, comment, line, col

    def locate(self, d: int, path: Any) -> tuple[int, int, int, int, str | None]:
        """``(line, column, end_line, end_column, comment)`` of the node: 1-based, the end just
        after its last character. A negative integer counts a sequence from its end."""
        doc, loc = self._find(d, path, True, True)
        r = loc.row
        start = doc.ps[r] if doc.kind[r] != K_ALIAS else doc.st[r]
        end = doc.en[r]
        last = r + doc.sz[r] - 1
        if doc.kind[last] == K_SCALAR and doc.sty[last] in "|>":
            end = _block_scalar_end(doc.text, doc.st[last])
        before = "".join([x.text for x in self._p.docs[:d]])
        whole = before + doc.text
        line, col = T.linecol(whole, len(before) + start)
        end_line, end_col = T.linecol(whole, len(before) + end)
        return line, col, end_line, end_col, _comment(doc, loc)

    # ---- edits --------------------------------------------------------------------------

    def _edit(self, doc: Doc, work: Any) -> bool:
        """Run ``work`` on the document's rows; a failure leaves them as they were."""
        snap = P.Snapshot(doc)
        try:
            return work()
        except E.ThroughAlias:
            snap.restore()
            raise make_error(
                "the path goes through an alias; edit the anchored node instead", "refused-alias"
            ) from None
        except PatchError as exc:
            snap.restore()
            raise _fail(exc) from None
        except BaseException:
            snap.restore()
            raise

    def prepare(self, value: Any) -> Prepared:
        """Convert a value ahead of an edit, so the caller can do it outside any lock."""
        if isinstance(value, Prepared):
            return value
        return _prepare.prepare(value, self._p.limits)

    def set(self, d: int, path: Any, value: Any) -> bool:
        """Make the node at ``path`` hold ``value`` (a missing mapping key is added). True when
        the edit could move or replace a collection (the version moved)."""
        toks = _tokens(path)
        doc = self._doc(d)
        value = self.prepare(value).value

        def work() -> bool:
            coll = W.is_collection(value.value if isinstance(value, Tagged) else value)
            if not toks:
                loc = E.Loc()
                was = doc.kind[T.follow(doc, 0)] in (K_MAP, K_SEQ)
                E.replace_value(doc, loc, value)
                return was or coll
            ploc = E.locate(doc, toks[:-1], True, False)
            pr = ploc.row
            last = toks[-1]
            if doc.kind[pr] == K_ALIAS:
                raise E.ThroughAlias
            if doc.kind[pr] == K_MAP:
                v, kr = T.find_entry(doc, pr, last)
                if v == -2:
                    raise E.refuse("refused-ambiguous", f"more than one key reads {last!r}")
                if v >= 0:
                    loc = E.locate(doc, toks, True, False)
                    was = doc.kind[T.follow(doc, v)] in (K_MAP, K_SEQ)
                    E.replace_value(doc, loc, value)
                    return was or coll
                if isinstance(last, str) and not last.isascii() and _SURROGATE(last):
                    raise KeyError(last)
                E.insert_member(doc, ploc, last, value, False)
                return False
            if doc.kind[pr] == K_SEQ:
                loc = E.locate(doc, toks, True, False)
                was = doc.kind[T.follow(doc, loc.row)] in (K_MAP, K_SEQ)
                E.replace_value(doc, loc, value)
                return was or coll
            raise KeyError(f"{last!r}: the node is a scalar, not a collection")

        if not doc.kind:
            raise KeyError("the document is empty")
        if self._edit(doc, work):
            self._version += 1
            return True
        return False

    def insert(self, d: int, path: Any, index: int, value: Any) -> None:
        """Insert ``value`` before item ``index`` of the sequence at ``path``; an index past
        the end appends."""
        value = self.prepare(value).value
        doc, loc = self._find(d, path, False)
        r = loc.row
        if doc.kind[r] == K_ALIAS:
            raise make_error(
                "the path goes through an alias; edit the anchored node instead", "refused-alias"
            )
        if doc.kind[r] != K_SEQ:
            raise TypeError("only a sequence takes insert")
        n = T.count_items(doc, r)
        shifts = index < n

        def work() -> bool:
            E.insert_item(doc, loc, min(index, n), value)
            return shifts

        if self._edit(doc, work):
            self._version += 1

    def remove(self, d: int, path: Any) -> None:
        toks = _tokens(path)
        if not toks:
            self._doc(d)
            raise TypeError("the root of a document cannot be removed")
        doc, parent = self._find(d, toks[:-1], False)
        if doc.kind[parent.row] == K_ALIAS:
            raise make_error(
                "the path goes through an alias; edit the anchored node instead", "refused-alias"
            )
        doc, loc = self._find(d, toks, False)
        self._edit(doc, lambda: E.remove_node(doc, loc))
        self._version += 1

    # ---- patches ------------------------------------------------------------------------

    def _patch_doc(self, d: Any) -> Doc:
        if not isinstance(d, int) or isinstance(d, bool):
            raise TypeError("the document index must be an int")
        docs = self._p.docs
        if not 0 <= d < len(docs):
            raise E.refuse("patch-path", f"the stream has no document {d}")
        return docs[d]

    def resolve(self, d: int, pointer: str) -> list:
        """The exact tokens an RFC 6901 pointer leads through, keys read by their text (so the
        tokens name the keys themselves: ``/1`` on ``0x1: a`` gives ``[1]``). A malformed pointer
        or two keys that fit is a ``PatchError``; nothing there is a ``KeyError`` or
        ``IndexError``."""
        if not isinstance(pointer, str):
            raise TypeError("a pointer is a str")
        toks = P.parse_pointer(pointer)
        doc = self._doc(d)
        if not doc.kind:
            if toks:
                raise KeyError("the document is empty")
            return []
        out: list = []
        row = 0
        kind = doc.kind
        for tok in toks:
            row = T.follow(doc, row)
            k = kind[row]
            if k == K_MAP:
                v, kr = T.find_member(doc, row, tok)
                if v == -2:
                    raise E.refuse("refused-ambiguous", f"more than one key reads {tok!r}")
                if v == -1:
                    raise KeyError(tok)
                try:
                    out.append(T.key_value(doc, kr, None))
                except T._Fail as exc:
                    raise self._error(doc, exc) from None
                row = v
            elif k == K_SEQ:
                i = E.parse_index(tok)
                if i < 0:
                    if E.is_index_text(tok):
                        raise IndexError(f"index {tok} is out of range")
                    raise KeyError(tok)
                c, _pe = E.item_at(doc, row, i)
                if c < 0:
                    raise IndexError(f"index {i} is out of range")
                out.append(str(i))
                row = c
            else:
                raise KeyError(f"{tok}: the node is a scalar, not a collection")
        return out

    def _ops(self, ops: Any) -> list:
        """The operations of a patch given as text (read under the document's limits) or as
        data (converted)."""
        limits = self._p.limits
        if isinstance(ops, str):
            try:
                ops = _construct.load(ops, limits, False)
            except YAMLError as exc:
                if exc.category == "limit":
                    raise E.refuse("limit", exc.message) from None
                raise E.refuse(
                    "patch-invalid", f"the patch is not valid JSON: {exc.message}"
                ) from None
        elif isinstance(ops, Prepared):
            ops = ops.value
        else:
            ops = self.prepare(ops).value
        if not isinstance(ops, list):
            raise E.refuse("patch-invalid", "a patch is an array of operations")
        return ops

    def patch(self, d: int, ops: Any) -> None:
        """Apply an RFC 6902 patch (a list of operations, or JSON text), all or nothing."""
        ops = self._ops(ops)
        doc = self._patch_doc(d)
        if P.apply_patch(doc, ops, self._p.limits):
            self._version += 1

    def merge_patch(self, d: int, patch: Any) -> None:
        """Apply an RFC 7396 merge patch (a JSON-shaped value), all or nothing."""
        patch = patch.value if isinstance(patch, Prepared) else self.prepare(patch).value
        doc = self._patch_doc(d)
        P.apply_merge(doc, patch, self._p.limits)
        self._version += 1


_PROPS_TRAILING = re.compile(r"[ \t]*(?:[&!][^ \t\r\n]*[ \t]*)*(#[^\r\n]*)").match
_COMMA_TRAILING = re.compile(r"[ \t]*,?[ \t]*(#[^\r\n]*)").match
_MARKER_LINE = re.compile(r"---(?:[ \t]|$)")
_QUESTION_LINE = re.compile(r"[ \t]*(?:[-?:][ \t]+)*\?[ \t]*(#[^\r\n]*)?\r?\n?$").match
_MARKER_END = re.compile(r"\.\.\.(?:[ \t]|$)").match
_DIRECTIVE_COMMENT = re.compile(r"(?:^|[ \t])(#.*)$")


def _comment(doc: Doc, loc: E.Loc) -> str | None:
    """The comment the node owns, as the parsing engine attaches it: comments that follow the
    node on its line (and those between an entry's key, colon and value), else the comment
    lines above it.

    A mapping entry's comments belong to the entry, so its value reads them, then the ones
    above its own value when that starts a line. A block collection at the root starts with its
    first entry, which owns the comments directly above it; the root reads the document's
    header instead: the comments before ``---`` or among the directives, and those followed by
    a blank line before the content."""
    r = loc.row
    k = doc.kind[r]
    root = loc.parent < 0 and loc.key < 0
    block = k in (K_MAP, K_SEQ) and doc.sty[r] == "b"
    found: str | None = None
    if loc.key >= 0 and loc.parent == 0 and _headed_key(doc):
        return _trailing(doc, loc)  # what is above the empty key is the document's header
    if not (block and root):
        found = _trailing(doc, loc)
        if found is None:
            start = (
                E.entry_start(doc, loc.key)
                if loc.key >= 0
                else doc.ps[r]
                if k != K_ALIAS
                else doc.st[r]
            )
            if (
                loc.parent >= 0
                and loc.key < 0
                and doc.kind[loc.parent] == K_SEQ
                and (doc.sty[loc.parent] == "b" or loc.index > 0)
            ):
                start = E.dash_of(doc, loc.parent, loc.index, loc.prev_end)
            found = _leading(doc, start)
        if found is None and loc.key >= 0:
            found = _value_leading(doc, loc, block)
    if found is None and root:
        found = _header(doc, r)
    return found


def _headed_key(doc: Doc) -> bool:
    """True when the document is a block mapping whose first key is empty and written as a bare
    ``:``; the engine reads the comments above that as the document's header."""
    return (
        bool(doc.kind)
        and doc.kind[0] == K_MAP
        and doc.sty[0] == "b"
        and len(doc.kind) > 1
        and doc.st[1] == doc.en[1]
        and doc.ps[1] == doc.st[1]
        and doc.text[doc.st[1] : doc.st[1] + 1] == ":"
    )


def _trailing(doc: Doc, loc: E.Loc) -> str | None:
    """The comments that follow the node, or its entry's parts, on their lines, in text order."""
    r = loc.row
    k = doc.kind[r]
    text = doc.text
    found: dict[int, str] = {}

    def take(m: Any) -> None:
        if m:
            found[m.start(1)] = m.group(1)

    flow_parent = loc.parent >= 0 and doc.sty[loc.parent] in "fp"
    after = _COMMA_TRAILING if flow_parent else _TRAILING
    indicator = -1
    if loc.key >= 0:
        kr = loc.key
        start = E.entry_start(doc, kr)
        if text[start] == "?":
            take(_TRAILING(text, start + 1))
        elif E.only_blanks_before(text, start) and start > 0:
            # A key that starts its own line under a ``?`` whose line ends in a comment.
            above = text[E.prev_line(text, E.line_start(text, start)) : E.line_start(text, start)]
            m = _QUESTION_LINE(above)
            if m and m.group(1):
                found[-1 - start] = m.group(1)
        take(_TRAILING(text, doc.en[kr]))
        indicator = E.colon_after(doc, kr)
    elif loc.parent >= 0 and doc.kind[loc.parent] == K_SEQ and doc.sty[loc.parent] == "b":
        indicator = E.dash_of(doc, loc.parent, loc.index, loc.prev_end)
    if indicator >= 0:
        m = _PROPS_TRAILING(text, indicator + 1)
        if m and (k == K_ALIAS or doc.ps[r] >= m.start(1) or doc.st[r] == doc.en[r]):
            take(m)
    if k != K_ALIAS and doc.ps[r] < doc.st[r]:
        m = _PROPS_TRAILING(text, doc.ps[r])
        if m and m.start(1) < doc.st[r]:
            take(m)
    if k == K_SCALAR and doc.sty[r] in "|>":
        h = E._HEADER(text, doc.st[r])
        take(_TRAILING(text, h.end() if h else doc.st[r] + 1))
    elif k == K_ALIAS or (k == K_SCALAR and doc.st[r] != doc.en[r]):
        take(after(text, doc.en[r]))
    elif k in (K_MAP, K_SEQ) and doc.sty[r] in "fp":
        take(_TRAILING(text, doc.st[r] + 1))
        take(after(text, doc.en[r]))
    return "\n".join(found[at] for at in sorted(found)) if found else None


def _value_leading(doc: Doc, loc: E.Loc, block: bool) -> str | None:
    """The comment lines the engine attaches to an entry's value itself: above a value that
    starts its own line, or above the ``:`` of an explicit entry."""
    r = loc.row
    text = doc.text
    colon = E.colon_after(doc, loc.key)
    if colon >= 0 and E.only_blanks_before(text, colon):
        got = _leading(doc, colon)
        if got is not None:
            return got
    k = doc.kind[r]
    has_props = k != K_ALIAS and doc.ps[r] < doc.st[r]
    start = doc.ps[r] if k != K_ALIAS else doc.st[r]
    if doc.st[r] == doc.en[r] and not has_props and k != K_ALIAS:
        return None  # an empty value has no place of its own
    if block and not has_props:
        return None  # a block collection starts with its first entry, which owns them
    if start > colon >= 0 and E.only_blanks_before(text, start):
        return _leading(doc, start)
    return None


_BLOCK_HEADER = re.compile(r"[|>](?:[1-9][+-]?|[+-][1-9]?)?").match


def _block_parent_indent(text: str, at: int) -> int:
    """The indentation of the block that holds the block scalar whose indicator is at ``at``: the
    column of the mapping key or of the ``-`` it follows, -1 at the top of a document. Read from
    the header line, which is all it takes."""
    ls = E.line_start(text, at)
    if ls == 0 and text.startswith("\ufeff"):
        ls = 1
    line = text[ls:at]
    if line.startswith("---") and (len(line) == 3 or line[3] in " \t"):
        return -1
    p = 0
    last = -1
    n = len(line)
    while True:
        while p < n and line[p] == " ":
            p += 1
        if p < n and line[p] in "-?:" and (p + 1 == n or line[p + 1] in " \t"):
            last = p
            p += 1
            continue
        break
    # Only properties between the indicators and the scalar: the scalar is the item itself.
    if all(t[0] in "&!" for t in line[p:].split()):
        return last
    return p


def _block_scalar_end(text: str, at: int) -> int:
    """Where a block scalar ends: just after its last content character. The spaces that make
    up the indentation of a trailing line, and the line breaks, are not content; the header is
    the end when the scalar has no content. ``at`` is the position of its indicator."""
    m = _BLOCK_HEADER(text, at)
    header_end = m.end() if m else at + 1
    digit = next((c for c in (m.group() if m else "") if c.isdigit()), None)
    n = len(text)
    first_line = E.line_end(text, header_end)
    if first_line >= n:
        return header_end
    body = E.next_line(text, first_line)
    parent = _block_parent_indent(text, at)
    if digit:
        indent = parent + int(digit)
    else:
        indent = -1
        j = body
        while j < n:
            e = E.line_end(text, j)
            seg = text[j:e]
            if seg.strip(" "):
                indent = len(seg) - len(seg.lstrip(" "))
                break
            if e >= n:
                break
            j = E.next_line(text, e)
        if indent <= parent:
            return header_end
    indent = max(indent, 0)
    pad = " " * indent
    last = None
    j = body
    while j < n:
        e = E.line_end(text, j)
        seg = text[j:e]
        if indent == 0 and seg.startswith(("---", "...")) and (len(seg) == 3 or seg[3] in " \t"):
            break
        if seg.strip(" ") and not seg.startswith(pad):
            break
        if len(seg) > indent:
            last = e
        if e >= n:
            break
        j = E.next_line(text, e)
    return header_end if last is None else last


def _closing_column(doc: Doc, start: int) -> int:
    """The column of the innermost block collection that ends just before ``start`` (the one
    holding the last content there), or -1."""
    last = -1
    for e in doc.en:
        if last < e <= start:
            last = e
    if last <= 0:
        return -1
    best = -1
    for r, k in enumerate(doc.kind):
        if k in (K_MAP, K_SEQ) and doc.sty[r] == "b" and doc.st[r] <= last - 1 < doc.en[r]:
            best = r  # rows come in document order, so a later match is a deeper one
    return E.col(doc.text, doc.st[best]) if best >= 0 else -1


def _leading(doc: Doc, start: int) -> str | None:
    """The comment lines directly above the node that starts at ``start``, blank lines between
    comments allowed. A comment indented deeper than the node, below a deeper block that ends
    here, closes that block instead. Where the node is the first thing in the document, a blank
    line ends the comments (what is above it is the document header)."""
    text = doc.text
    if not E.only_blanks_before(text, start):
        return None
    column = E.col(text, start)
    # Lines inside a block scalar are its content, whatever they look like.
    inside = max(
        (
            doc.en[x]
            for x in range(len(doc.kind))
            if doc.kind[x] == K_SCALAR and doc.sty[x] in "|>" and doc.en[x] <= start
        ),
        default=-1,
    )
    found: list[tuple[str, int, bool]] = []  # comment, its indent, a blank line below it
    a = E.line_start(text, start)
    gap = False
    top = False
    while True:
        if a <= 0:
            top = True
            break
        pls = E.prev_line(text, a)
        if pls < inside:
            break
        raw = text[pls:a]
        line = raw.strip(" \t\r\n\ufeff")
        if not line:
            gap = True
        elif line.startswith("#"):
            found.append((line, len(raw) - len(raw.lstrip(" \t")), gap))
        else:
            top = line == "---" or line.startswith(("--- ", "---\t", "%"))
            break
        a = pls
    closing = None
    lines = []
    for line, indent, gap_below in found:
        if indent > column and not top:
            if closing is None:
                closing = _closing_column(doc, start)
            if closing > column:
                break
        lines.append((line, gap_below))
    if top:
        lines = [x for x in lines if not x[1]]
    return "\n".join(x[0] for x in reversed(lines)) if lines else None


def _header(doc: Doc, r: int) -> str | None:
    """The document header: comments before ``---`` or among the directives (a comment on a
    directive's line too), a comment on the ``---`` line, and the comments above the content that
    have a blank line after them. A document with no content has every comment of its own as
    header."""
    text = doc.text
    start = doc.ps[r] if doc.kind[r] != K_ALIAS else doc.st[r]
    empty = doc.kind[r] == K_SCALAR and doc.st[r] == doc.en[r] == start
    headed = r == 0 and _headed_key(doc)
    head = text if empty else text[:start]
    rows = re.split(r"\r\n|\r|\n", head.lstrip("\ufeff"))
    if not empty:
        # What precedes the content on its own line is not a line of the header, unless it is
        # the ``---`` the content follows.
        last = rows.pop().strip(" \t")
        if _MARKER_LINE.match(last + " "):
            rows.append(last)
    out: list[str] = []
    pending: list[str] = []  # comments after the marker, kept if a blank line follows them
    for raw in rows:
        line = raw.strip(" \t")
        if line.startswith("#"):
            pending.append(line)
        elif _MARKER_LINE.match(line + " "):
            out += pending
            pending = []
            m = _TRAILING(line, 3)
            if m:
                out.append(m.group(1))
        elif not line or (not empty and _MARKER_END(line + " ")):
            out += pending
            pending = []
        elif line.startswith("%"):  # a directive
            out += pending
            pending = []
            m = _DIRECTIVE_COMMENT.search(line)
            if m:
                out.append(m.group(1))
        elif empty and line.startswith("..."):
            break
        else:
            pending = []
    if empty or headed:
        out += pending
    return "\n".join(out) if out else None


def parse(data: Any, limits: Any) -> Handle:
    """Parse ``data`` into a lossless ``Handle`` (raises ``YAMLError``)."""
    return Handle(T.parse_stream(data, limits))
