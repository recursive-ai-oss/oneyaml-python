"""The lossless document of the pure backend: a row table per YAML document over its own text.

Every node of a document is a *row*. Rows sit in document order (a node comes before its
children, so a node's subtree is the contiguous range ``row .. row + size``) in parallel lists,
which is far smaller and quicker to build than one object per node. Offsets are into the text of the
document's region of the stream, so documents of a stream never share state: an edit rewrites one
region and the other regions are untouched.

The text itself is the only copy of the bytes the library keeps; nothing is ever re-serialised from
the table. An edit splices a short piece of new text into the document text, replaces the rows of
the edited node with the rows of the new text, and shifts the offsets behind it. Reading never
re-parses.

A mapping's children are alternating key and value rows, a sequence's are its items; an empty node
(``key:`` with nothing after it) is a scalar row whose ``start == end``.
"""

from __future__ import annotations

import re
from typing import Any

from .._errors import YAMLError, make_error
from .._limits import _NONE
from .._types import FrozenMapping, Tagged, freeze
from ._decode import detect
from ._parser import (
    ALIAS,
    DOC_END,
    DOC_START,
    MAP_END,
    MAP_START,
    SCALAR,
    SEQ_END,
    SEQ_START,
    linecol,
    parse_events,
)
from ._resolve import _WRONG_FOR_COLLECTION, COLLECTION_OK, KEYWORDS, NUMBER_START, WORD_START, Fail
from ._resolve import number as _number
from ._resolve import tagged_scalar as _tagged_scalar

K_SCALAR, K_MAP, K_SEQ, K_ALIAS = range(4)
KIND_NAMES = ("scalar", "mapping", "sequence", "scalar")

_SURROGATE = re.compile("[\ud800-\udfff]").search
_BREAK = re.compile(r"\r\n|\r|\n")
_YAML = "tag:yaml.org,2002:"


class Doc:
    """The rows of one document and the text of its region of the stream."""

    __slots__ = ("amap", "anc", "en", "exp", "kind", "ps", "st", "sty", "sz", "tag", "text", "val")

    def __init__(self, text: str) -> None:
        self.text = text
        self.kind: list[int] = []
        self.ps: list[int] = []  # where the node's properties (anchor, tag) begin
        self.st: list[int] = []  # where the node's own text begins
        self.en: list[int] = []  # where it ends
        self.sz: list[int] = []  # rows in its subtree, itself included
        self.sty: list[str] = []  # scalar: : ' " | >   collection: b (block) f (flow)
        self.anc: list[str | None] = []
        self.tag: list[str | None] = []
        self.val: list[str | None] = []  # scalar text, or the name an alias uses
        self.amap: dict[int, int] | None = None  # alias row -> the row it stands for
        self.exp: tuple[list[int], list[int]] | None = None  # nodes and bytes a copy would hold


def _err(text: str, off: int, message: str, category: str = "syntax") -> YAMLError:
    line, col = linecol(text, off)
    return make_error(message, category, line, col, off)


def _region_end(text: str, pos: int) -> int:
    """Where the region of a document that ended at ``pos`` ends: after the rest of its line."""
    if pos <= 0 or text[pos - 1] in "\r\n":
        return pos
    m = _BREAK.search(text, pos)
    return m.end() if m else len(text)


_CLOSE = re.compile(r"(?:[ \t\r\n,\ufeff]+|#[^\r\n]*)*[\]}]").match
_SKIP_B = re.compile(r"(?:[ \t\r\n\ufeff]+|#[^\r\n]*)*").match
_NL = re.compile(r"[\r\n]")
_SKIP_F = re.compile(r"(?:[ \t\r\n,\ufeff]+|#[^\r\n]*)*").match


def _block_end(text: str, st: int, en: int) -> int:
    """The end of the last content line of a block scalar: its breaks, and the white-space-only
    lines after the content, are not part of it (the parser's own end depends on what follows)."""
    e = en
    while True:
        j = e
        while j > st and text[j - 1] in " \t":
            j -= 1
        if j > st and text[j - 1] in "\r\n":
            e = j - 1
            if text[e] == "\n" and e > st and text[e - 1] == "\r":
                e -= 1
        else:
            return e


def _blank_end(text: str, end: int, comments: bool = True) -> int:
    """Extend a region over the blank lines and, unless ``comments`` is false, the indented
    comment lines after it. A keep block scalar at the end of the document counts the blank lines
    in its value, so an edit there has to see them; at the end of the stream an indented comment
    belongs to the last entry as well."""
    n = len(text)
    while end < n:
        m = _BREAK.search(text, end)
        stop = m.start() if m else n
        seg = text[end:stop]
        if seg.strip(" \t") and not (comments and seg[0] in " \t" and seg.lstrip(" \t")[:1] == "#"):
            break
        if not m:
            return n
        end = m.end()
    return end


def build(text: str, events: list[tuple]) -> tuple[list[Doc], str]:  # noqa: C901
    """The documents of a stream from its parser events; and the text when it has none.

    Positions the parser leaves vague are made exact here: an empty node sits right after the
    indicator it follows (``:``, ``-``, ``?``), a flow collection ends after its bracket and a
    block scalar ends with its last content line. Every row then has an exact span."""
    docs: list[Doc] = []
    prev_end = 0
    d: Doc | None = None
    b = 0
    anchors: dict[str, int] = {}
    open_anchors: set[str] = set()
    stack: list[int] = []
    counts: list[int] = []
    last_end = 0
    marker_end = 0
    open_end = False
    kind = ps = st = en = sz = sty = anc = tag = val = None  # type: ignore[assignment]
    for e in events:
        k = e[0]
        if k == SCALAR:
            if counts:
                c = counts[-1]
                counts[-1] = c + 1
            a_ps = e[1]
            a_st = e[2]
            a_en = e[3]
            style = e[6]
            if a_en == a_st:
                if a_ps == a_st:
                    a_ps = a_st = a_en = _empty_pos(
                        text, kind, sty, stack, c if counts else 0, last_end, marker_end,
                        st[stack[-1]] + b if stack else 0,
                    )  # fmt: skip
            elif style == "|" or style == ">":
                a_en = _block_end(text, a_st, a_en)
            r = len(kind)
            kind.append(K_SCALAR)
            ps.append(a_ps - b)
            st.append(a_st - b)
            en.append(a_en - b)
            sz.append(1)
            sty.append(style)
            a = e[4]
            anc.append(a)
            tag.append(e[5])
            val.append(e[7])
            if a is not None:
                anchors[a] = r
            last_end = a_en
        elif k in (MAP_START, SEQ_START):
            if counts:
                counts[-1] += 1
            r = len(kind)
            s0 = e[2]
            if e[5]:
                brace = text[s0] in "[{"
                style = "f" if brace else "p"
                last_end = s0 + 1 if brace else s0
            else:
                style = "b"
                last_end = s0
            kind.append(K_MAP if k == MAP_START else K_SEQ)
            ps.append(e[1] - b)
            st.append(s0 - b)
            en.append(0)
            sz.append(0)
            sty.append(style)
            a = e[3]
            anc.append(a)
            tag.append(e[4])
            val.append(None)
            stack.append(r)
            counts.append(0)
            if a is not None:
                anchors[a] = r
                open_anchors.add(a)
        elif k in (MAP_END, SEQ_END):
            r = stack.pop()
            counts.pop()
            sz[r] = len(kind) - r
            if sty[r] == "f" and k == MAP_END and text[e[1] : e[1] + 1] != "}":
                # A single pair in a flow sequence whose key is a flow collection (`[a]: 1`)
                # starts with a bracket too; only a real mapping ends with a brace.
                sty[r] = "p"
            if sty[r] == "f":
                j = e[1]
                if text[j : j + 1] in ("]", "}") and text[j : j + 1] != "":
                    last_end = j + 1
                else:
                    m = _CLOSE(text, last_end)
                    last_end = m.end() if m else j
            en[r] = last_end - b
            a = anc[r]
            if a is not None:
                open_anchors.discard(a)
        elif k == ALIAS:
            if counts:
                counts[-1] += 1
            name = e[3]
            if name in open_anchors:
                raise _err(text, e[1], f"the alias {name} refers to a node that contains it")
            if name not in anchors:
                raise _err(text, e[1], f"the alias {name} has no anchor")
            kind.append(K_ALIAS)
            ps.append(e[1] - b)
            st.append(e[1] - b)
            en.append(e[2] - b)
            sz.append(1)
            sty.append(":")
            anc.append(None)
            tag.append(None)
            val.append(name)
            last_end = e[2]
        elif k == DOC_START:
            b = prev_end
            d = Doc("")
            kind, ps, st, en, sz, sty, anc, tag, val = (
                d.kind, d.ps, d.st, d.en, d.sz, d.sty, d.anc, d.tag, d.val,
            )  # fmt: skip
            anchors = {}
            open_anchors = set()
            stack = []
            counts = []
            marker_end = e[2]
            last_end = e[2]
        elif k == DOC_END:
            end = _region_end(text, max(e[2] if e[3] else e[1], last_end, marker_end))
            # Comment lines before the next document's marker belong to that document's header
            # whatever their indent; only a document that runs to the end of the stream owns
            # the indented comments after its last entry (extended after the loop).
            open_end = not e[3]
            if open_end:
                end = _blank_end(text, end, False)
            d.text = text[b:end]  # type: ignore[union-attr]
            docs.append(d)  # type: ignore[arg-type]
            prev_end = end
    if not docs:
        return docs, text
    if open_end:
        end = _blank_end(text, prev_end)
        docs[-1].text += text[prev_end:end]
        prev_end = end
    if prev_end < len(text):
        docs[-1].text += text[prev_end:]
    return docs, ""


def _empty_pos(text, kind, sty, stack, idx, last_end, marker_end, pstart) -> int:
    """Where an empty node (no properties) sits: right after the indicator it follows."""
    if not stack:
        return marker_end
    p = stack[-1]
    skip = _SKIP_B if sty[p] == "b" else _SKIP_F
    q = skip(text, last_end).end()
    ch = text[q : q + 1]
    if kind[p] == K_SEQ:
        return q + 1 if ch == "-" else last_end
    if idx % 2 == 0:  # a key: after its "?", or at its ":"
        return q + 1 if ch == "?" else q
    # a value: after the ":" when there is one (on a later line, a ":" in the mapping's column)
    q = _SKIP_B(text, last_end).end()
    if text[q : q + 1] == ":":
        if sty[p] != "b" or q == last_end or not _NL.search(text, last_end, q):
            return q + 1
        ls = max(text.rfind("\n", 0, q), text.rfind("\r", 0, q)) + 1
        s0 = pstart
        if q - ls == s0 - (max(text.rfind("\n", 0, s0), text.rfind("\r", 0, s0)) + 1):
            return q + 1
    return last_end


class Parsed:
    """What ``parse`` keeps of the stream besides the documents."""

    __slots__ = ("bom", "codec", "docs", "limits", "tail", "ver")

    def __init__(self, docs: list[Doc], tail: str, codec: str, bom: bytes, limits: Any) -> None:
        self.docs = docs
        self.tail = tail
        self.codec = codec
        self.bom = bom
        self.limits = limits
        self.ver = 0


def parse_stream(data: Any, limits: Any) -> Parsed:
    """Decode and parse ``data`` into a lossless stream (raises ``YAMLError``)."""
    depth, _nf, _nfl, _bf, _bfl, input_size, scalar_size, documents, _digits = limits._native
    bom = b""
    if isinstance(data, str):
        codec = "utf-8"
        if data.startswith("\ufeff"):
            bom = b"\xef\xbb\xbf"
            data = data[1:]
        if not data.isascii() and _SURROGATE(data):
            raise make_error("the string holds a lone surrogate, which is not text", "encoding")
        text = data
        if input_size < _NONE and len(text.encode("utf-8")) > input_size:
            raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if input_size < _NONE and len(raw) > input_size:
            raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
        codec, skip = detect(raw)
        bom = raw[:skip]
        body = raw[skip:]
        try:
            text = body.decode(codec)
        except UnicodeDecodeError as exc:
            before = body[: exc.start].decode(codec, errors="ignore")
            line, col = linecol(before, len(before))
            raise make_error(
                f"invalid {codec} input: {exc.reason}", "encoding", line, col
            ) from None
    else:
        raise TypeError(f"YAML input must be bytes or str, not {type(data).__name__}")
    events = parse_events(text, 1 << 62 if depth >= _NONE else depth)
    if scalar_size < _NONE or documents < _NONE:
        count = 0
        for e in events:
            if e[0] == SCALAR and scalar_size < _NONE:
                v = e[7]
                if len(v) > scalar_size or (
                    len(v) * 4 > scalar_size
                    and len(v.encode("utf-8", "surrogatepass")) > scalar_size
                ):
                    raise _err(
                        text, e[2], f"a scalar is over the limit of {scalar_size} bytes", "limit"
                    )
            elif e[0] == DOC_START:
                count += 1
                if count > documents:
                    raise _err(
                        text, e[1], f"the stream has more than {documents} documents", "limit"
                    )
    docs, tail = build(text, events)
    _check_tags(text, docs)
    _check_keys(text, docs)
    return Parsed(docs, tail, codec, bom, limits)


def _check_tags(text: str, docs: list[Doc]) -> None:
    """Refuse tags that cannot describe their node (``!!int abc``, ``!!str [a]``) when the stream
    is read, as ``loads`` does, so a document that parses can be edited and read later."""
    base = 0
    for doc in docs:
        tag = doc.tag
        for r in range(len(tag)):
            if tag[r] is None or doc.kind[r] == K_ALIAS:
                continue
            try:
                if doc.kind[r] == K_SCALAR:
                    scalar_value(doc, r, None)
                else:
                    collection_tag(tag[r], None, doc.kind[r] == K_MAP, doc, r)  # type: ignore[arg-type]
            except _Fail as exc:
                raise _err(text, base + exc.offset, exc.message, exc.category) from None
            except ValueError:
                pass  # a number too long for int(): the read reports it under the limits
        base += len(doc.text)


def _check_keys(text: str, docs: list[Doc]) -> None:
    """Refuse a mapping that has one key twice (``duplicate-key``) or two keys a Python mapping
    cannot tell apart (``1``, ``1.0``, ``true``), as ``loads`` does."""
    base = 0
    for doc in docs:
        kind = doc.kind
        val = doc.val
        sz = doc.sz
        sty = doc.sty
        tag = doc.tag
        for row in range(len(kind)):
            if kind[row] != K_MAP or sz[row] < 5:  # fewer than two entries
                continue
            end = row + sz[row]
            j = row + 1
            texts = []
            odd = False
            while j < end:
                if kind[j] == K_SCALAR:
                    t = val[j]
                    texts.append(t)
                    if sty[j] == ":" or tag[j] is not None:
                        c = t[:1]
                        if c in NUMBER_START or c in WORD_START or not c or tag[j] is not None:
                            odd = True
                else:
                    texts.append(None)
                    if kind[j] == K_ALIAS:
                        odd = True  # named by what it points at, which the texts do not hold
                v = j + sz[j]
                j = v + sz[v]
            if not odd and len(set(texts)) == len(texts):
                continue
            seen: dict[Any, tuple[Any, int]] = {}
            wide: dict[int, float] = {}
            j = row + 1
            while j < end:
                v = j + sz[j]
                at = follow(doc, j) if kind[j] == K_ALIAS else j
                if kind[at] == K_SCALAR:
                    try:
                        kv = scalar_value(doc, at, None, wide)
                    except (_Fail, Fail, ValueError):
                        kv = _NOPE
                    if wide and type(kv) is float and id(kv) in wide:
                        raise _err(
                            text, base + doc.st[j],
                            "a float key is outside the range of a double and cannot be told "
                            "apart from its neighbours there", "limit",
                        )  # fmt: skip
                    if kv is not _NOPE and kv == kv and not isinstance(kv, (list, dict)):
                        # 1, 1.0 and true are three keys in a document: the type is part of it
                        ident = (type(kv), kv)
                        if ident in seen:
                            raise _err(
                                text, base + doc.st[j], "the mapping has a duplicate key",
                                "duplicate-key",
                            )  # fmt: skip
                        seen[ident] = (kv, j)
                j = v + sz[v]
        base += len(doc.text)


# ---- navigation -----------------------------------------------------------------------------


def alias_map(doc: Doc) -> dict[int, int]:
    """For every alias row, the row of the latest node of that name before it."""
    m = doc.amap
    if m is None:
        m = {}
        names: dict[str, int] = {}
        anc = doc.anc
        kind = doc.kind
        for r in range(len(kind)):
            a = anc[r]
            if a is not None:
                names[a] = r
            if kind[r] == K_ALIAS:
                m[r] = names[doc.val[r]]  # type: ignore[index]
        doc.amap = m
    return m


def follow(doc: Doc, row: int) -> int:
    """The node an alias row stands for (a row that is no alias stands for itself)."""
    if doc.kind[row] == K_ALIAS:
        return alias_map(doc)[row]
    return row


def children(doc: Doc, row: int) -> list[int]:
    """The child rows of a collection row (keys and values alternate in a mapping)."""
    out = []
    j = row + 1
    end = row + doc.sz[row]
    sz = doc.sz
    while j < end:
        out.append(j)
        j += sz[j]
    return out


def key_text(doc: Doc, row: int) -> str | None:
    """The text a pointer token is matched against, or None for a collection key."""
    row = follow(doc, row)
    if doc.kind[row] == K_SCALAR:
        return doc.val[row]
    return None


def find_member(doc: Doc, row: int, token: str) -> tuple[int, int]:
    """The value row (and key row) of the mapping member whose key reads as the text ``token``
    (an RFC 6901 pointer token): the text as written, so ``1`` and ``"1"`` both read as ``1``.

    Returns ``(-1, -1)`` when there is none and ``(-2, -2)`` when two keys read as that text."""
    sz = doc.sz
    kind = doc.kind
    val = doc.val
    j = row + 1
    end = row + sz[row]
    hit = (-1, -1)
    has_alias = False
    while j < end:
        v = j + sz[j]
        if kind[j] == K_SCALAR:
            if val[j] == token:
                if hit[0] != -1:
                    return -2, -2
                hit = (v, j)
        elif kind[j] == K_ALIAS:
            has_alias = True
        j = v + sz[v]
    if has_alias:
        # An alias used as a key is matched under the text of what it names.
        j = row + 1
        hit = (-1, -1)
        while j < end:
            v = j + sz[j]
            if kind[j] != K_MAP and kind[j] != K_SEQ and key_text(doc, j) == token:
                if hit[0] != -1:
                    return -2, -2
                hit = (v, j)
            j = v + sz[v]
    return hit


def strict_eq(a: Any, b: Any) -> bool:
    """Equality of two key values by YAML identity: types must match (``1``, ``1.0`` and ``true``
    differ), mappings ignore order. Walks with its own stack."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, tuple):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y, strict=True))
        elif isinstance(x, FrozenMapping):
            if len(x) != len(y):
                return False
            for k, v in x.items():
                if k not in y:
                    return False
                stack.append((v, y[k]))
        elif isinstance(x, float):
            if not (x == y or (x != x and y != y)):
                return False
        elif x != y:
            return False
    return True


def key_value(doc: Doc, row: int, cap: int | None = None) -> Any:
    """The Python value of a key row: scalars as they read, collections frozen."""
    row = follow(doc, row)
    if doc.kind[row] == K_SCALAR:
        return scalar_value(doc, row, cap)
    v = _walk(doc, row, cap, {}, {}, None)
    # The same bound as a load: a key nested deeper could not be compared or hashed safely.
    from ._construct import KEY_DEPTH, _measure

    if _measure(v, {})[2] > KEY_DEPTH:
        raise _Fail(f"a mapping key nests deeper than {KEY_DEPTH} levels", "limit", doc.st[row])
    return freeze(v, 1 << 62, 1 << 62)[0]


def find_entry(doc: Doc, row: int, tok: Any) -> tuple[int, int]:
    """The value row (and key row) of the mapping entry a view token names.

    A ``str`` names the key that is that string (``"16"`` does not find ``0x10``). Any other
    token names the key of exactly that value and type: ``1``, ``1.0`` and ``True`` are three
    keys, a ``tuple`` or ``FrozenMapping`` names a collection key. ``(-1, -1)`` for no entry,
    ``(-2, -2)`` for two that fit."""
    sz = doc.sz
    kind = doc.kind
    j = row + 1
    end = row + sz[row]
    if type(tok) is str:
        while j < end:
            v = j + sz[j]
            t = follow(doc, j)
            if kind[t] == K_SCALAR:
                try:
                    kv = scalar_value(doc, t, None)
                except (Fail, _Fail, ValueError):
                    kv = _NOPE
                if type(kv) is str and kv == tok:
                    return v, j
            j = v + sz[v]
        return -1, -1
    hits = []
    collection = isinstance(tok, (tuple, FrozenMapping))
    while j < end:
        v = j + sz[j]
        t = follow(doc, j)
        k = kind[t]
        if collection:
            if k != K_SCALAR:
                try:
                    ok = strict_eq(key_value(doc, t), tok)
                except (Fail, _Fail, ValueError):
                    ok = False
                if ok:
                    hits.append((v, j))
        elif k == K_SCALAR:
            try:
                kv = scalar_value(doc, t, None)
            except (Fail, _Fail, ValueError):
                kv = _NOPE
            if type(kv) is type(tok) and strict_eq(kv, tok):
                hits.append((v, j))
        j = v + sz[v]
    if len(hits) > 1:
        return -2, -2
    return hits[0] if hits else (-1, -1)


_NOPE = object()


_INDEX = re.compile(r"0|[1-9][0-9]*\Z").match


def find_item(doc: Doc, row: int, index: int) -> int:
    """The row of the ``index``-th item of a sequence row, or -1."""
    sz = doc.sz
    j = row + 1
    end = row + sz[row]
    while j < end:
        if index == 0:
            return j
        index -= 1
        j += sz[j]
    return -1


def count_items(doc: Doc, row: int) -> int:
    """How many items a collection row holds (mapping entries are counted by their pairs)."""
    sz = doc.sz
    j = row + 1
    end = row + sz[row]
    n = 0
    while j < end:
        n += 1
        j += sz[j]
    return n // 2 if doc.kind[row] == K_MAP else n


def tag_of(doc: Doc, row: int) -> str | None:
    return doc.tag[row]


# ---- values ---------------------------------------------------------------------------------


def scalar_value(doc: Doc, row: int, cap: int | None, wide: dict | None = None) -> Any:
    """The Core-schema value of a scalar row. A float outside the range of a double is noted in
    ``wide`` when one is given."""
    if wide is None:
        wide = {}
    v = doc.val[row]
    tag = doc.tag[row]
    try:
        if tag is None:
            if doc.sty[row] == ":":
                c = v[:1]  # type: ignore[index]
                if c in NUMBER_START:
                    return _number(v, cap, wide)  # type: ignore[arg-type]
                if c in WORD_START or not c:
                    return KEYWORDS.get(v, v)  # type: ignore[arg-type]
            return v
        return _tagged_scalar(tag, v, cap, wide)  # type: ignore[arg-type]
    except Fail as exc:
        raise _Fail(exc.message, exc.category, doc.st[row]) from None


def collection_tag(tag: str, v: Any, is_map: bool, doc: Doc, row: int) -> Any:
    if tag == "!":
        return v
    want = COLLECTION_OK.get(tag)
    if want is not None:
        if want == (1 if is_map else 0):
            return v
        kind = "mapping" if is_map else "sequence"
        raise _Fail(f"the tag {tag} does not fit a {kind}", "syntax", doc.st[row])
    if tag in _WRONG_FOR_COLLECTION:
        raise _Fail(f"the tag {tag} cannot be put on a collection", "syntax", doc.st[row])
    return Tagged(tag, v)


class _Fail(Exception):
    def __init__(self, message: str, category: str, offset: int) -> None:
        super().__init__(message)
        self.message = message
        self.category = category
        self.offset = offset


def _utf8_len(v: str) -> int:
    return len(v) if v.isascii() else len(v.encode("utf-8", "surrogatepass"))


def expansion(doc: Doc) -> tuple[list[int], list[int]]:
    """For every row, the nodes and the bytes of text a copy of it would hold: an alias counts as
    what it stands for. Shared parts count each time they are used but are measured once."""
    ex = doc.exp
    if ex is not None:
        return ex
    kind = doc.kind
    n = len(kind)
    nodes = [1] * n
    size = [0] * n
    am = alias_map(doc) if K_ALIAS in kind else {}
    st = doc.st
    en = doc.en
    sz = doc.sz
    for r in range(n):
        if kind[r] == K_SCALAR:
            size[r] = en[r] - st[r]
    # An alias weighs what its target weighs, and a target comes before its aliases, so one
    # forward pass with an explicit stack finishes every subtree in time.
    stack: list[int] = []
    totals_n = [0] * n
    totals_b = [0] * n
    ends: list[int] = []
    for r in range(n):
        while ends and r >= ends[-1]:
            ends.pop()
            c = stack.pop()
            totals_n[c] += 1
            if stack:
                totals_n[stack[-1]] += totals_n[c]
                totals_b[stack[-1]] += totals_b[c]
            nodes[c] = totals_n[c]
            size[c] = totals_b[c]
        k = kind[r]
        if k in (K_SCALAR, K_ALIAS):
            if k == K_ALIAS:
                t = am[r]
                nn, bb = nodes[t], size[t]
                nodes[r], size[r] = nn, bb
            else:
                nn, bb = 1, size[r]
            if stack:
                totals_n[stack[-1]] += nn
                totals_b[stack[-1]] += bb
        else:
            stack.append(r)
            ends.append(r + sz[r])
    while stack:
        ends.pop()
        c = stack.pop()
        totals_n[c] += 1
        if stack:
            totals_n[stack[-1]] += totals_n[c]
            totals_b[stack[-1]] += totals_b[c]
        nodes[c] = totals_n[c]
        size[c] = totals_b[c]
    doc.exp = (nodes, size)
    return doc.exp


def value_of(doc: Doc, row: int, limits: Any) -> Any:
    """The plain data a node stands for; an alias to a collection shares the object it names.

    Each alias is charged to the alias budgets for what a copy of it would hold, before anything
    is built. The walk keeps its own stack, so nesting never becomes recursion."""
    _depth, nf, nfl, bf, bfl, _isz, _ssz, _docs, digits = limits._native
    cap = None if digits >= _NONE else digits
    kind = doc.kind
    try:
        if kind[row] == K_ALIAS:
            row = alias_map(doc)[row]
        if K_ALIAS not in kind:
            return _walk(doc, row, cap, {}, {}, None)
        am = alias_map(doc)
        nodes, size = expansion(doc)
        budget = (max(nf * len(kind), nfl), max(bf * len(doc.text), bfl), nodes, size)
        # the collections an alias outside the walked range names are built first, in document
        # order (a target comes before its aliases)
        built: dict[int, Any] = {}
        end = row + doc.sz[row]
        need: set[int] = set()
        todo = [row]
        seen = {row}
        while todo:
            top = todo.pop()
            for r in range(top, top + doc.sz[top]):
                if kind[r] == K_ALIAS:
                    t = am[r]
                    if kind[t] != K_SCALAR and t not in seen and not row <= t < end:
                        seen.add(t)
                        need.add(t)
                        todo.append(t)
        charged = [0, 0]
        for t in sorted(need):
            if t not in built:  # an earlier target held it: keep that one, so sharing holds
                built[t] = _walk(doc, t, cap, built, am, budget, charged)
        return _walk(doc, row, cap, built, am, budget, charged)
    except _Fail as exc:
        line, col = linecol(doc.text, exc.offset)
        raise make_error(exc.message, exc.category, line, col, exc.offset) from None


def _walk(doc, root, cap, built, am, budget, charged=None):  # noqa: C901
    kind = doc.kind
    sz = doc.sz
    tags = doc.tag
    if charged is None:
        charged = [0, 0]
    stack: list[list] = []  # [row, end, is_map, items]
    result: list[Any] = []
    out = result
    stop = root + sz[root]
    cur_end = stop
    r = root
    while True:
        while r >= cur_end and stack:
            row, _end, is_map, its = stack.pop()
            v = _make_map(doc, row, its) if is_map else its
            t = tags[row]
            if t is not None:
                v = collection_tag(t, v, bool(is_map), doc, row)
            built[row] = v
            if stack:
                out = stack[-1][3]
                cur_end = stack[-1][1]
            else:
                out = result
                cur_end = stop
            out.append(v)
        if r >= stop:
            break
        k = kind[r]
        if k == K_SCALAR:
            out.append(scalar_value(doc, r, cap))
            r += 1
        elif k == K_ALIAS:
            t = am[r]
            charged[0] += budget[2][t]
            charged[1] += budget[3][t]
            if charged[0] > budget[0]:
                raise _Fail("alias expansion exceeds the node limit", "limit", doc.st[r])
            if charged[1] > budget[1]:
                raise _Fail("alias expansion exceeds the byte limit", "limit", doc.st[r])
            if kind[t] == K_SCALAR:
                out.append(scalar_value(doc, t, cap))
            else:
                out.append(built[t])
            r += 1
        else:
            its = []
            stack.append([r, r + sz[r], k == K_MAP, its])
            out = its
            cur_end = r + sz[r]
            r += 1
    return result[0]


def _make_map(doc: Doc, row: int, items: list) -> dict:
    ks = items[0::2]
    vs = items[1::2]
    if len(ks) != len(vs):
        raise _Fail("a mapping key has no value", "syntax", doc.en[row])
    try:
        d = dict(zip(ks, vs, strict=True))
        if len(d) == len(ks):
            return d
    except TypeError:
        pass
    ks = [freeze(k, 1 << 62, 1 << 62)[0] if isinstance(k, (list, dict, Tagged)) else k for k in ks]
    d = {}
    for i, k in enumerate(ks):
        if k in d:
            prev = next(p for p in d if p is k or p == k)
            if type(prev) is type(k):
                raise _Fail("the mapping has a duplicate key", "duplicate-key", doc.st[row])
            raise _Fail(
                "two keys are different in YAML but equal in Python (1, 1.0 and true), "
                "so they cannot share a dict",
                "other",
                doc.st[row],
            )
        d[k] = vs[i]
    return d
