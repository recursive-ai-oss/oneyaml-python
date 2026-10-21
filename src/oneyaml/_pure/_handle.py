"""The ``Handle`` of the pure backend: one parsed stream behind the backend interface."""

from __future__ import annotations

import re
from typing import Any

from .._errors import PatchError
from . import _edit as E
from . import _patch as P
from . import _tree as T
from ._tree import K_ALIAS, K_MAP, K_SCALAR, K_SEQ, KIND_NAMES, Doc, Parsed

_STYLE = {":": "plain", "'": "single", '"': "double", "|": "literal", ">": "folded"}
_COMMENT = re.compile(r"[ \t]*(#[^\r\n]*)").match


class Handle:
    """A lossless stream. Documents are addressed by index, nodes by a list of pointer tokens."""

    __slots__ = ("_p", "_version")

    def __init__(self, parsed: Parsed) -> None:
        self._p = parsed
        self._version = 0

    # ---- identity -----------------------------------------------------------------------

    @property
    def encoding(self) -> str:
        return self._p.codec

    def version(self) -> int:
        return self._version

    def count(self) -> int:
        return len(self._p.docs)

    def emit(self) -> bytes:
        p = self._p
        text = "".join([d.text for d in p.docs]) if p.docs else p.tail
        return p.bom + text.encode(p.codec, "surrogatepass")

    # ---- reads --------------------------------------------------------------------------

    def _doc(self, d: int) -> Doc:
        docs = self._p.docs
        if not isinstance(d, int) or isinstance(d, bool):
            raise TypeError("the document index must be an int")
        if not 0 <= d < len(docs):
            raise E.refuse("patch-path", f"the stream has no document {d}")
        return docs[d]

    def _node(self, d: int, path: list) -> tuple[Doc, int]:
        doc = self._doc(d)
        loc = E.locate(doc, list(path))
        return doc, T.follow(doc, loc.row)

    def kind(self, d: int, path: list) -> str:
        doc, r = self._node(d, path)
        return KIND_NAMES[doc.kind[r]]

    def value(self, d: int, path: list) -> Any:
        doc, r = self._node(d, path)
        return T.value_of(doc, r, self._p.limits)

    def length(self, d: int, path: list) -> int:
        doc, r = self._node(d, path)
        if doc.kind[r] in (K_MAP, K_SEQ):
            return T.count_items(doc, r)
        raise E.refuse("patch-invalid", "a scalar has no length")

    def keys(self, d: int, path: list) -> list[str]:
        """The tokens that address the members of a collection: key texts of a mapping (members
        whose key is a collection cannot be addressed and are left out), indices of a sequence."""
        doc, r = self._node(d, path)
        if doc.kind[r] == K_MAP:
            out = []
            for k, _v in E.members(doc, r):
                t = T.key_text(doc, k)
                if t is not None:
                    out.append(t)
            return out
        if doc.kind[r] == K_SEQ:
            return [str(i) for i in range(T.count_items(doc, r))]
        raise E.refuse("patch-invalid", "a scalar has no members")

    def info(self, d: int, path: list) -> tuple[str, str | None, str | None, str | None, int, int]:
        """``(style, tag, anchor, comment, line, column)`` of the node at ``path``."""
        doc = self._doc(d)
        loc = E.locate(doc, list(path))
        r = loc.row
        k = doc.kind[r]
        if k == K_ALIAS:
            style = "alias"
        elif k == K_SCALAR:
            style = _STYLE[doc.sty[r]]
        else:
            style = "block" if doc.sty[r] == "b" else "flow"
        comment = None
        m = _COMMENT(doc.text, doc.en[r])
        if m and doc.st[r] != doc.en[r]:
            comment = m.group(1)
        before = "".join([x.text for x in self._p.docs[:d]])
        pos = doc.ps[r] if k != K_ALIAS else doc.st[r]
        head = before + doc.text[:pos]
        line, col = _linecol(head)
        return style, doc.tag[r], doc.anc[r], comment, line, col

    # ---- edits --------------------------------------------------------------------------

    def set(self, d: int, path: list, value: Any) -> None:
        doc = self._doc(d)
        loc = E.locate(doc, list(path))
        snap = P.Snapshot(doc)
        try:
            E.replace_value(doc, loc, value)
        except BaseException:
            snap.restore()
            raise
        self._version += 1

    def insert(self, d: int, path: list, index: Any, value: Any) -> None:
        """Add a member to the collection at ``path``: ``index`` is a key for a mapping, an
        int (``len`` appends) or None (the end) for a sequence."""
        doc = self._doc(d)
        loc = E.locate(doc, list(path))
        r = loc.row
        if loc.alias or doc.kind[r] == K_ALIAS:
            raise E.refuse("refused-alias", "cannot add below an alias")
        snap = P.Snapshot(doc)
        try:
            if doc.kind[r] == K_MAP:
                E.insert_member(doc, loc, str(index), value)
            elif doc.kind[r] == K_SEQ:
                # like list.insert, an index past the end appends
                n = T.count_items(doc, r)
                at = None if index is None else int(index)
                E.insert_item(doc, loc, None if at is not None and at > n else at, value)
            else:
                raise E.refuse("patch-path", "cannot add below a scalar")
        except BaseException:
            snap.restore()
            raise
        self._version += 1

    def remove(self, d: int, path: list) -> None:
        doc = self._doc(d)
        loc = E.locate(doc, list(path))
        snap = P.Snapshot(doc)
        try:
            E.remove_node(doc, loc)
        except BaseException:
            snap.restore()
            raise
        self._version += 1

    def patch(self, d: int, ops: list[dict]) -> None:
        doc = self._doc(d)
        if not isinstance(ops, (list, tuple)):
            raise TypeError("a patch is a list of operations")
        P.apply_patch(doc, list(ops), self._p.limits)
        self._version += 1

    def merge_patch(self, d: int, patch: Any) -> None:
        doc = self._doc(d)
        P.apply_merge(doc, patch, self._p.limits)
        self._version += 1


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


__all__ = ["PatchError", "Handle", "parse"]
