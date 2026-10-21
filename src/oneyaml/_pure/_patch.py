"""RFC 6902 patches and RFC 7396 merge patches on a lossless document.

A patch is applied to a copy of the document's row table and text, and the document takes the
result only when every operation succeeded, so a failing patch leaves it exactly as it was. The
pointer of an operation is read here (RFC 6901); the edits themselves are in ``_edit``.
"""

from __future__ import annotations

import re
from typing import Any

from .._errors import PatchError
from .._limits import _NONE
from .._types import Tagged
from . import _edit as E
from . import _tree as T
from ._tree import K_ALIAS, K_MAP, K_SEQ, Doc

_SURROGATE = re.compile("[\ud800-\udfff]").search
_OPS = ("add", "remove", "replace", "move", "copy", "test")


def parse_pointer(s: Any) -> list[str]:
    """The tokens of an RFC 6901 pointer string."""
    if not isinstance(s, str):
        raise E.refuse("patch-invalid", "a path must be a string")
    if not s.isascii() and _SURROGATE(s):
        raise E.refuse("patch-invalid", "the pointer holds a lone surrogate, which is not text")
    if s == "":
        return []
    if s[0] != "/":
        raise E.refuse("patch-invalid", f"the pointer {s!r} does not start with '/'")
    toks = s.split("/")[1:]
    if "~" in s:
        out = []
        for t in toks:
            if "~" in t:
                i = 0
                parts = []
                while True:
                    j = t.find("~", i)
                    if j < 0:
                        parts.append(t[i:])
                        break
                    parts.append(t[i:j])
                    nxt = t[j + 1 : j + 2]
                    if nxt == "0":
                        parts.append("~")
                    elif nxt == "1":
                        parts.append("/")
                    else:
                        raise E.refuse("patch-invalid", f"bad escape in the pointer {s!r}")
                    i = j + 2
                t = "".join(parts)
            out.append(t)
        return out
    return toks


class Snapshot:
    __slots__ = ("doc", "lists", "text")

    def __init__(self, doc: Doc) -> None:
        self.doc = doc
        self.text = doc.text
        self.lists = (
            doc.kind[:], doc.ps[:], doc.st[:], doc.en[:], doc.sz[:], doc.sty[:], doc.anc[:],
            doc.tag[:], doc.val[:],
        )  # fmt: skip

    def restore(self) -> None:
        d = self.doc
        d.text = self.text
        (d.kind, d.ps, d.st, d.en, d.sz, d.sty, d.anc, d.tag, d.val) = self.lists
        d.amap = None
        d.exp = None


def data_equal(a: Any, b: Any) -> bool:
    """Equality by the YAML data model under the Core schema: types must match, mappings ignore
    order, sequences do not. Keys of a mapping match only when they are strings."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        tx, ty = type(x), type(y)
        if tx is dict or ty is dict:
            if tx is not dict or ty is not dict or len(x) != len(y):
                return False
            for k, v in x.items():
                if type(k) is not str or k not in y:
                    return False
                stack.append((v, y[k]))
        elif tx is list or ty is list:
            if tx is not list or ty is not list or len(x) != len(y):
                return False
            stack.extend(zip(x, y, strict=True))
        elif tx is not ty:
            return False
        elif tx is float:
            if not (x == y or (x != x and y != y)):
                return False
        elif tx is Tagged or x != y:
            return False
    return True


def strip_nulls(v: Any) -> Any:
    """A merge patch value as the data it creates: members that are null are left out."""
    if type(v) is not dict:
        return v
    out: dict = {}
    work = [(v, out)]
    while work:
        src, dst = work.pop()
        for k, x in src.items():
            if x is None:
                continue
            if type(x) is dict:
                d2: dict = {}
                dst[k] = d2
                work.append((x, d2))
            else:
                dst[k] = x
    return out


class Patcher:
    def __init__(self, doc: Doc, limits: Any) -> None:
        self.doc = doc
        self.limits = limits
        self.copied = 0
        _d, nf, nfl, bf, bfl, input_size, *_ = limits._native
        self.copy_budget = max(bf * len(doc.text), bfl)
        self.copied_nodes = 0
        self.node_budget = max(nf * len(doc.kind), nfl)
        self.size_cap = None if input_size >= _NONE else input_size

    # ---- reading -------------------------------------------------------------------------

    def read(self, tokens: list[str]) -> Any:
        loc = E.locate(self.doc, tokens)
        return T.value_of(self.doc, loc.row, self.limits)

    def charge(self, loc: E.Loc) -> None:
        """A copy writes out what aliases stand for: charge it to the byte budget."""
        doc = self.doc
        r = loc.row
        if K_ALIAS in doc.kind:
            if doc.kind[r] == K_ALIAS:
                r = T.alias_map(doc)[r]
            nodes, size = T.expansion(doc)
            b = size[r]
            n = nodes[r]
        else:
            b = doc.en[r] - doc.st[r]
            n = doc.sz[r]
        self.copied += b
        self.copied_nodes += n
        if self.copied > self.copy_budget or self.copied_nodes > self.node_budget:
            raise E.refuse("limit", "the patch copies more than the alias limits allow")

    # ---- the operations ------------------------------------------------------------------

    def add(self, tokens: list[str], value: Any, hint_row: int | None = None) -> None:
        doc = self.doc
        if not tokens:
            self.replace([], value)
            return
        ploc = E.locate(doc, tokens[:-1])
        if ploc.alias:
            raise E.refuse("refused-alias", "the path goes through an alias")
        pr = ploc.row
        k = doc.kind[pr]
        if k == K_ALIAS:
            raise E.refuse("refused-alias", "cannot add below an alias")
        last = tokens[-1]
        if k == K_MAP:
            E.insert_member(doc, ploc, last, value)
        elif k == K_SEQ:
            if last == "-":
                E.insert_item(doc, ploc, None, value)
            else:
                i = E.parse_index(last)
                if i < 0:
                    raise E.refuse("patch-path", f"{last!r} is not an index of a sequence")
                E.insert_item(doc, ploc, i, value)
        else:
            raise E.refuse("patch-path", "cannot add below a scalar")

    def replace(self, tokens: list[str], value: Any) -> None:
        loc = E.locate(self.doc, tokens)
        E.replace_value(self.doc, loc, value)

    def remove(self, tokens: list[str]) -> None:
        loc = E.locate(self.doc, tokens)
        E.remove_node(self.doc, loc)

    def test(self, tokens: list[str], value: Any) -> None:
        got = self.read(tokens)
        if not data_equal(got, value):
            raise E.refuse("patch-test", "the value is not the one expected")

    def copy(self, src: list[str], dst: list[str]) -> None:
        loc = E.locate(self.doc, src)
        self.charge(loc)
        value = T.value_of(self.doc, loc.row, self.limits)
        self.add(dst, value)

    def move(self, src: list[str], dst: list[str]) -> None:
        doc = self.doc
        if src == dst:
            E.locate(doc, src)
            return
        if len(dst) > len(src) and dst[: len(src)] == src:
            raise E.refuse("patch-invalid", "a value cannot be moved into itself")
        loc = E.locate(doc, src)
        if loc.alias:
            raise E.refuse("refused-alias", "the path goes through an alias")
        self.charge(loc)
        value = T.value_of(doc, loc.row, self.limits)
        first = loc.key if loc.key >= 0 else loc.row
        E.check_anchors_free(doc, first, loc.row + doc.sz[loc.row], None)
        # both parents mappings, and the destination not above the source: write first, so the
        # mapping never collapses to {} midway
        both_maps = (
            loc.parent >= 0
            and doc.kind[loc.parent] == K_MAP
            and dst
            and not (len(dst) < len(src) and src[: len(dst)] == dst)
            and self._parent_is_map(dst)
        )
        if both_maps:
            self.add(dst, value)
            self.remove(src)
        else:
            self.remove(src)
            self.add(dst, value)

    def _parent_is_map(self, dst: list[str]) -> bool:
        loc = E.locate(self.doc, dst[:-1])
        r = loc.row
        if self.doc.kind[r] == K_ALIAS:
            return False
        return self.doc.kind[r] == K_MAP

    # ---- merge patch ---------------------------------------------------------------------

    def merge(self, patch: Any) -> None:
        doc = self.doc
        if type(patch) is not dict:
            self.replace([], patch)
            return
        work: list[tuple[list[str], dict]] = [([], patch)]
        while work:
            toks, pd = work.pop()
            loc = E.locate(doc, toks)
            r = loc.row
            if doc.kind[r] == K_ALIAS:
                raise E.refuse("refused-alias", "cannot merge into an alias")
            if loc.alias:
                raise E.refuse("refused-alias", "the path goes through an alias")
            if doc.kind[r] != K_MAP:
                # a target that is no mapping is treated as an empty one (RFC 7396)
                self.replace(toks, strip_nulls(pd))
                continue
            if not pd:
                continue
            deletes = []
            nested = []
            for name, val in pd.items():
                if val is None:
                    deletes.append(name)
                    continue
                v, kr = T.find_member(doc, r, name)
                if v == -2:
                    raise E.refuse("refused-ambiguous", f"more than one key reads {name!r}")
                if v == -1:
                    self.add([*toks, name], strip_nulls(val))
                elif type(val) is dict:
                    nested.append(([*toks, name], val))
                else:
                    self.replace([*toks, name], val)
                # rows shift after every edit: look the mapping up again
                loc = E.locate(doc, toks)
                r = loc.row
            for name in deletes:
                v, kr = T.find_member(doc, r, name)
                if v == -2:
                    raise E.refuse("refused-ambiguous", f"more than one key reads {name!r}")
                if v >= 0:
                    self.remove([*toks, name])
                    loc = E.locate(doc, toks)
                    r = loc.row
            work.extend(reversed(nested))


def check_op(i: int, op: Any) -> tuple[str, dict]:
    if not isinstance(op, dict):
        raise E.refuse("patch-invalid", "an operation must be an object")
    name = op.get("op")
    if not isinstance(name, str) or name not in _OPS:
        raise E.refuse("patch-invalid", f"unknown operation {name!r}")
    if "path" not in op:
        raise E.refuse("patch-invalid", "an operation needs a path")
    if name in ("add", "replace", "test") and "value" not in op:
        raise E.refuse("patch-invalid", f"{name} needs a value")
    if name in ("move", "copy") and "from" not in op:
        raise E.refuse("patch-invalid", f"{name} needs a from")
    return name, op


def apply_patch(doc: Doc, ops: list[dict], limits: Any) -> bool:
    """Apply ``ops`` to ``doc``; on any failure the document is as it was and ``PatchError``
    names the operation. True when an operation other than ``test`` ran."""
    snap = Snapshot(doc)
    p = Patcher(doc, limits)
    i = 0
    try:
        for i, op in enumerate(ops):
            name, op = check_op(i, op)
            toks = parse_pointer(op["path"])
            if name == "add":
                p.add(toks, op["value"])
            elif name == "replace":
                p.replace(toks, op["value"])
            elif name == "remove":
                p.remove(toks)
            elif name == "test":
                p.test(toks, op["value"])
            elif name == "copy":
                p.copy(parse_pointer(op["from"]), toks)
            else:
                p.move(parse_pointer(op["from"]), toks)
        if p.size_cap is not None and len(doc.text.encode("utf-8", "surrogatepass")) > p.size_cap:
            raise E.refuse("limit", "the result is over the input size limit")
    except BaseException as exc:
        snap.restore()
        if isinstance(exc, PatchError) and exc.index is None:
            exc.index = i
        raise
    return any(isinstance(op, dict) and op.get("op") != "test" for op in ops)


def apply_merge(doc: Doc, patch: Any, limits: Any) -> None:
    snap = Snapshot(doc)
    p = Patcher(doc, limits)
    try:
        p.merge(patch)
        if p.size_cap is not None and len(doc.text.encode("utf-8", "surrogatepass")) > p.size_cap:
            raise E.refuse("limit", "the result is over the input size limit")
    except BaseException:
        snap.restore()
        raise
