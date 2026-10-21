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

    def _find(self, d: Any, path: Any, follow: bool = True) -> tuple[Doc, E.Loc]:
        toks = _tokens(path)
        doc = self._doc(d)
        if not doc.kind:
            raise KeyError("the document is empty")
        try:
            return doc, E.locate(doc, toks, True, follow)
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
        line, col = _linecol(before + doc.text[:pos])
        return style, tag, anchor, comment, line, col

    def locate(self, d: int, path: Any) -> tuple[int, int, int, int, str | None]:
        """``(line, column, end_line, end_column, comment)`` of the node: 1-based, the end just
        after its last character."""
        doc, loc = self._find(d, path)
        r = loc.row
        start = doc.ps[r] if doc.kind[r] != K_ALIAS else doc.st[r]
        before = "".join([x.text for x in self._p.docs[:d]])
        line, col = _linecol(before + doc.text[:start])
        end_line, end_col = _linecol(before + doc.text[: doc.en[r]])
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
                E.insert_member(doc, ploc, last, value)
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


def _comment(doc: Doc, loc: E.Loc) -> str | None:
    """The comment the node owns: the one on its line after it, else the comment lines above it.

    A mapping entry's comments belong to the entry, so its value reads them: the comment after
    the value (after the ``key:`` when the value is a block), else the lines above the key. A
    block collection at the root starts with its first entry, which owns the comments directly
    above it; the root reads the document's header instead: the comments before ``---`` and
    those followed by a blank line before the content."""
    r = loc.row
    text = doc.text
    k = doc.kind[r]
    root = loc.parent < 0 and loc.key < 0
    block = k in (K_MAP, K_SEQ) and doc.sty[r] == "b"
    found: str | None = None
    if not (block and root):
        if loc.key >= 0:
            found = _entry_trailing(doc, loc)
        elif not block:
            m = _TRAILING(text, doc.en[r])
            if m and (doc.st[r] != doc.en[r] or k == K_ALIAS):
                found = m.group(1)
        if found is None:
            start = (
                E.entry_start(doc, loc.key)
                if loc.key >= 0
                else doc.ps[r]
                if k != K_ALIAS
                else doc.st[r]
            )
            if loc.parent >= 0 and loc.key < 0 and doc.kind[loc.parent] == K_SEQ:
                start = E.dash_of(doc, loc.parent, loc.index, loc.prev_end)
            found = _leading(doc, start)
    if found is None and root:
        found = _header(text, doc.ps[r] if k != K_ALIAS else doc.st[r])
    return found


def _entry_trailing(doc: Doc, loc: E.Loc) -> str | None:
    """The comment after a mapping entry on its last line."""
    r = loc.row
    k = doc.kind[r]
    text = doc.text
    if k == K_ALIAS or (k == K_SCALAR and doc.sty[r] not in "|>" and doc.st[r] != doc.en[r]):
        m = _TRAILING(text, doc.en[r])
    elif k == K_SCALAR and doc.sty[r] in "|>":
        h = E._HEADER(text, doc.st[r])
        m = _TRAILING(text, h.end() if h else doc.st[r] + 1)
    elif k == K_SCALAR or doc.sty[r] == "b":
        # A null value or a block collection: the comment follows the colon on the key's line.
        colon = E.colon_after(doc, loc.key)
        m = _PROPS_TRAILING(text, colon + 1) if colon >= 0 else None
    else:
        m = _TRAILING(text, doc.en[r])
    return m.group(1) if m else None


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
    found: list[tuple[str, int, bool]] = []  # comment, its indent, a blank line below it
    a = E.line_start(text, start)
    gap = False
    top = False
    while True:
        if a <= 0:
            top = True
            break
        pls = E.prev_line(text, a)
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


def _header(text: str, start: int) -> str | None:
    """The document header: comments before ``---`` or the directives, a comment on the
    ``---`` line, and the comments above the content that have a blank line after them."""
    head = text[: E.line_start(text, start)].lstrip("\ufeff")
    out: list[str] = []
    pending: list[str] = []  # comments after the marker, kept if a blank line follows them
    rows = re.split(r"\r\n|\r|\n", head)
    rows.pop()  # what follows the last line break is not a line
    for raw in rows:
        line = raw.strip(" \t")
        if line.startswith("#"):
            pending.append(line)
        elif line == "---" or line.startswith("--- ") or line.startswith("---\t"):
            out += pending
            pending = []
            m = _TRAILING(line, 3)
            if m:
                out.append(m.group(1))
        elif not line or line.startswith("%"):  # a blank line or a directive
            out += pending
            pending = []
        else:
            pending = []
    return "\n".join(out) if out else None


def _linecol(head: str) -> tuple[int, int]:
    n = 0
    last = 0
    for m in re.finditer(r"\r\n|\r|\n", head):
        n += 1
        last = m.end()
    tail = head[last:]
    if tail.startswith("\ufeff"):
        tail = tail[1:]
    return n + 1, len(tail) + 1


def parse(data: Any, limits: Any) -> Handle:
    """Parse ``data`` into a lossless ``Handle`` (raises ``YAMLError``)."""
    return Handle(T.parse_stream(data, limits))
