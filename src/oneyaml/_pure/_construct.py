"""Build Python values from parser events.

One pass over the event list with explicit stacks: no recursion follows the nesting of the
input, whatever its depth.

Aliases share: an alias to a collection yields the very object the anchor made, so a document
that expands exponentially when written out loads in time proportional to its size. What the
sharing hides is charged instead: each alias adds the nodes and bytes a copy would hold to a
running total, and the load is refused when the total passes the alias budgets.

A self-referential alias (``&a [*a]``) cannot be built: an anchor only names a node once the
node is complete, and an alias met earlier is refused as soon as it is read.
"""

from __future__ import annotations

import sys
from itertools import islice
from typing import Any

from .._errors import YAMLError, make_error
from .._limits import _NONE
from .._types import FrozenMapping, Tagged, freeze
from ._decode import decode
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
from ._resolve import _WRONG_FOR_COLLECTION as _WRONG_COLLECTION
from ._resolve import (
    COLLECTION_OK,
    KEYWORDS,
    NUMBER_START,
    WORD_START,
    Fail,
    number,
    tagged_scalar,
)
from ._resolve11 import (
    KEYWORDS11,
    MERGE,
    NUMBER_START11,
    WORD_START11,
    number11,
    tag_collection11,
    tag_collection_text,
    tagged_scalar11,
    tagged_scalar_text,
)

_OPEN = object()  # an anchor whose node is not finished yet
_MISSING = object()
# How deep collections may nest inside one mapping key. Keys are hashed and compared by the
# interpreter, which recurses in C once per level; real keys are a few levels at most. Fixed:
# it does not follow ``Limits``, and the native backend uses the same number.
KEY_DEPTH = 200
_COLLECTIONS = (list, tuple, dict, FrozenMapping, Tagged, set, frozenset)
_UNFROZEN = (list, dict, Tagged)


class Parsed:
    """A parsed stream: its text, its events and what building values from them needs.

    Parsing and building are separate steps so that a caller whose build fails can compose the
    same events another way without reading the text again."""

    __slots__ = ("args", "events", "text")

    def __init__(self, text: str, events: list[tuple], args: tuple) -> None:
        self.text = text
        self.events = events
        self.args = args


def parse(data: Any, limits: Any, schema: str = "core", multi: bool = True) -> Parsed:
    """Read ``data`` into events, checking the limits that need only the events. ``schema`` and
    ``multi`` are as for ``load``: a duplicate key or an alias bomb that comes before a syntax
    error is reported first, which takes a build of the events read so far."""
    if schema not in _SCHEMAS:
        raise ValueError(f"unknown schema {schema!r}")
    depth, nf, nfloor, bf, bfloor, input_size, scalar_size, documents, digits = limits._native
    binary = isinstance(data, (bytes, bytearray, memoryview))
    if input_size < _NONE and binary and len(data) > input_size:
        raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
    text = decode(data)
    if input_size < _NONE and not binary and _utf8_len(text) > input_size:
        raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
    args = (
        None if digits >= _NONE else digits,
        None if depth >= _NONE else depth,
        (nf, nfloor, bf, bfloor),
    )
    try:
        try:
            events = parse_events(text, sys.maxsize if depth >= _NONE else depth)
        except YAMLError as exc:
            # The event list the parser kept is not part of the error. It is read once more
            # because a reader that builds as it goes meets a duplicate key or an alias bomb
            # that comes before the syntax error first.
            raw = exc.__dict__.pop("raw_events", None)
            if raw:
                _build(text, raw, multi, *args, partial=True, schema=schema)
            raise
        if scalar_size < _NONE or documents < _NONE:
            _check_sizes(events, scalar_size, documents)
    except Fail as exc:
        raise _fail_error(text, exc) from None
    return Parsed(text, events, args)


def build(parsed: Parsed, multi: bool, schema: str = "core") -> Any:
    """The value of the single document of ``parsed`` (``multi=False``) or a list of every
    document (``multi=True``)."""
    try:
        docs = _build(parsed.text, parsed.events, multi, *parsed.args, schema=schema)
    except Fail as exc:
        raise _fail_error(parsed.text, exc) from None
    if multi:
        return docs
    return docs[0] if docs else None


def _fail_error(text: str, exc: Fail) -> YAMLError:
    off = exc.offset
    line, col = linecol(text, 0 if off is None else off)
    return make_error(exc.message, exc.category, line, col, off)


def load(data: Any, limits: Any, multi: bool, schema: str = "core") -> Any:
    """The value of the single document in ``data`` (``multi=False``) or a list of every
    document (``multi=True``). ``limits`` is a ``Limits``. ``schema`` is ``"core"`` (YAML 1.2),
    ``"yaml11"`` (the rules of PyYAML's safe loader) or ``"text"`` (every scalar a string,
    tags ignored)."""
    return build(parse(data, limits, schema, multi), multi, schema)


def _check_sizes(events: list[tuple], scalar_size: int, documents: int) -> None:
    """The limits that need only a look at the events (most callers set neither)."""
    if scalar_size < _NONE:
        for e in events:
            if e[0] == SCALAR:
                v = e[7]
                if len(v) > scalar_size or (
                    len(v) * 4 > scalar_size and _utf8_len(v) > scalar_size
                ):
                    raise Fail(f"a scalar is over the limit of {scalar_size} bytes", "limit", e[1])
    if documents < _NONE:
        count = 0
        for e in events:
            if e[0] == DOC_START:
                count += 1
                if count > documents:
                    raise Fail(f"the stream has more than {documents} documents", "limit", e[1])


def _byte_budget(text: str, start: int, end: int, factor: int, floor: int) -> int:
    """The alias byte budget once the document has been read up to ``end``, in UTF-8 bytes."""
    return max(factor * _utf8_len(text[start:end]), floor)


def _utf8_len(v: str) -> int:
    return len(v) if v.isascii() else len(v.encode("utf-8", "surrogatepass"))


def _build(  # noqa: C901
    text: str,
    events: list[tuple],
    multi: bool,
    cap: int | None,
    depth: int | None,
    budget: tuple[int, int, int, int],
    partial: bool = False,
    schema: str = "core",
) -> list[Any]:
    nf, nfloor, bf, bfloor = budget
    wide: dict[int, float] = {}
    keys_seen: dict[str, str] = {}
    intern_key = keys_seen.setdefault
    anchors: dict[str, Any] = {}
    # Collections that have been measured: id -> (nodes, bytes, height, the object itself). The
    # object is kept so its id cannot be reused while the entry exists.
    info: dict[int, tuple[int, int, int, Any]] = {}
    docs: list[Any] = []
    stack: list[tuple[list, tuple]] = []
    push = stack.append
    pop = stack.pop
    cur: list[Any] = []
    add = cur.append
    keywords, number_start, word_start, number, tagged_scalar, tag_collection, make_map = _SCHEMAS[
        schema
    ]
    merge_seen = False
    missing = _MISSING
    # Where the document began (event index and offset), the collections closed since, and what
    # aliases have been charged so far.
    doc_i = doc_start = closed = 0
    charged_nodes = charged_bytes = 0
    ascii_text = text.isascii()
    e: tuple = ()
    try:
        for i, e in enumerate(events):
            k = e[0]
            if k == SCALAR:
                v = e[7]
                tag = e[5]
                if tag is None:
                    if e[6] == ":":
                        c = v[:1]
                        if c in number_start:
                            v = number(v, cap, wide)
                        elif c in word_start or not c:
                            v = keywords.get(v, v)
                            if v is MERGE:
                                # Only a key of a mapping merges; anywhere else it is text.
                                if stack and stack[-1][1][0] == MAP_START and not len(cur) & 1:
                                    merge_seen = True
                                else:
                                    v = "<<"
                else:
                    v = tagged_scalar(tag, v, cap, wide)
                    if v is MERGE:  # the tag !!merge
                        if stack and stack[-1][1][0] == MAP_START and not len(cur) & 1:
                            merge_seen = True
                        else:
                            raise Fail(
                                "the merge tag is only meaningful on a mapping key", "construct"
                            )
                add(v)
                a = e[4]
                if a is not None:
                    anchors[a] = v
            elif k == MAP_END or k == SEQ_END:  # noqa: SIM109 (hot loop)
                closed += 1
                buf = cur
                cur, e0 = pop()
                add = cur.append
                if k == MAP_END:
                    if merge_seen:
                        v = _map_merging(buf, e0, e, events, info, wide, intern_key)
                    else:
                        v = make_map(buf, e0, e, events, info, wide, intern_key)
                else:
                    v = buf
                tag = e0[4]
                if tag is not None:
                    v = tag_collection(tag, v, k == MAP_END, e0)
                add(v)
                a = e0[3]
                # An alias means the latest node of that name; one defined inside this node
                # (a redefinition) already took the name over.
                if a is not None and anchors[a] is _OPEN:
                    anchors[a] = v
                    _measure(v, info)
            elif k == MAP_START or k == SEQ_START:  # noqa: SIM109 (hot loop)
                push((cur, e))
                cur = []
                add = cur.append
                a = e[3]
                if a is not None:
                    anchors[a] = _OPEN
            elif k == ALIAS:
                v = anchors.get(e[3], missing)
                if v is _OPEN:
                    raise Fail(f"alias *{e[3]} refers to a node that contains it", "syntax")
                if v is missing:
                    raise Fail(f"alias *{e[3]} has no anchor", "syntax")
                if v is MERGE:
                    raise Fail("an alias to a merge key cannot be constructed", "construct")
                ent = info.get(id(v))
                if ent is None:
                    nodes, size, height = 1, _bytes(v), 0
                else:
                    nodes, size, height = ent[0], ent[1], ent[2]
                # The shared object nests as deep as the anchored one, wherever it is used.
                if depth is not None and len(stack) + height > depth:
                    raise Fail(f"alias nests deeper than the limit of {depth}", "limit")
                # What a copy would hold: loading is cheap because of the sharing, but whoever
                # walks the result (a dump, a comparison) pays for the expansion.
                charged_nodes += nodes
                charged_bytes += size
                if charged_nodes > max(nf * (i - doc_i - closed), nfloor):
                    raise Fail("alias expansion exceeds the node limit", "limit")
                if charged_bytes > max(bf * (e[1] - doc_start), bfloor) and (
                    ascii_text
                    or charged_bytes > max(bf * _utf8_len(text[doc_start : e[1]]), bfloor)
                ):
                    raise Fail("alias expansion exceeds the byte limit", "limit")
                add(v)
            elif k == DOC_START:
                if docs and not multi:
                    raise Fail(
                        "the stream holds more than one document; use loads_all", "other", e[1]
                    )
                anchors = {}
                info = {}
                wide.clear()
                cur = []
                add = cur.append
                doc_i, doc_start, closed = i, e[1], 0
                charged_nodes = charged_bytes = 0
            elif k == DOC_END:
                docs.append(cur[0] if cur else None)
        while partial and stack:
            # The input broke off inside collections: check what they hold so far.
            buf = cur
            cur, e0 = pop()
            if e0[0] == MAP_START:
                make_map(buf[: len(buf) // 2 * 2], e0, e0, events, info, wide, intern_key)
    except Fail as exc:
        if exc.offset is None:
            exc.offset = _offset(e)
        raise
    return docs


def _offset(e: tuple) -> int:
    """Where a node's own text starts (after its tag and anchor)."""
    if e[0] == SCALAR:
        return e[2]
    return e[1] if len(e) > 1 and isinstance(e[1], int) else 0


def _bytes(x: Any) -> int:
    """The bytes of text a scalar value stands for (what a copy of it would hold)."""
    t = type(x)
    if t is str:
        return len(x) if x.isascii() else len(x.encode("utf-8", "surrogatepass"))
    if t is bytes:
        return len(x)
    if t is int:
        # Past 64 bits the loader keeps the text; estimate its length in decimal digits.
        return 0 if -(1 << 63) <= x < (1 << 63) else x.bit_length() * 30103 // 100000 + 1
    if t is Tagged:
        return _bytes(x.value)
    return 0


def _measure(root: Any, info: dict[int, tuple[int, int, int, Any]]) -> tuple[int, int, int, Any]:
    """Nodes, bytes of text and height of ``root`` as a copy would hold them.

    Shared parts count each time they are used, but are measured once (``info`` remembers every
    collection it has seen), and the walk keeps its own stack, so a bomb costs its document's
    size and depth is no limit."""
    ent = info.get(id(root))
    if ent is not None:
        return ent
    work = [root]
    while work:
        obj = work[-1]
        if id(obj) in info:
            work.pop()
            continue
        t = type(obj)
        if t is list or t is tuple:
            kids: Any = obj
        elif t is dict:
            kids = [*obj, *obj.values()]
        elif t is FrozenMapping:
            kids = [*obj._items, *obj._items.values()]
        elif t is set or t is frozenset:
            kids = list(obj)
        else:  # Tagged: the tag is no node of its own
            kids = (obj.value,)
        waiting = [c for c in kids if type(c) in _COLLECTIONS and id(c) not in info]
        if waiting:
            work.extend(waiting)
            continue
        work.pop()
        if t is Tagged:
            c = obj.value
            sub = info.get(id(c)) if type(c) in _COLLECTIONS else None
            info[id(obj)] = (1, _bytes(c), 0, obj) if sub is None else (*sub[:3], obj)
            continue
        nodes = 1
        size = 0
        height = 0
        for c in kids:
            sub = info.get(id(c)) if type(c) in _COLLECTIONS else None
            if sub is None:
                nodes += 1
                size += _bytes(c)
            else:
                nodes += sub[0]
                size += sub[1]
                if sub[2] > height:
                    height = sub[2]
        info[id(obj)] = (nodes, size, height + 1, obj)
    return info[id(root)]


def _tag_collection(tag: str, v: Any, is_map: bool, e0: tuple) -> Any:
    """Apply the tag of a collection: the Core collection tags are transparent."""
    if tag == "!":
        return v
    want = COLLECTION_OK.get(tag)
    if want is not None:
        if want == (1 if is_map else 0):
            return v
        kind = "mapping" if is_map else "sequence"
        raise Fail(f"the tag {tag} does not fit a {kind}", "syntax", e0[2])
    if tag in _WRONG_COLLECTION:
        raise Fail(f"the tag {tag} cannot be put on a collection", "syntax", e0[2])
    return Tagged(tag, v)


def _map(buf, e0, e_end, events, info, wide, intern_key):
    """The dict of a mapping from its alternating keys and values."""
    ks = buf[0::2]
    vs = buf[1::2]
    if len(ks) != len(vs):
        raise Fail("a mapping key has no value", "syntax", e_end[1])
    try:
        if not wide:
            if {*map(type, ks)} == {str}:
                ks = list(map(intern_key, ks, ks))
            d = dict(zip(ks, vs, strict=True))
            if len(d) == len(ks):
                return d
    except TypeError:
        pass  # an unhashable key: a collection, frozen below
    except RecursionError:
        raise _too_deep(events, e0, 0) from None
    return _map_slow(ks, vs, e0, events, info, wide)


def _too_deep(events, e0, index: int) -> Fail:
    return Fail(
        "a mapping key is nested too deeply to compare",
        "limit",
        _key_offset(events, e0, index),
    )


def _map_slow(ks, vs, e0, events, info, wide, strict=True):
    """A mapping with keys that need care: collections to freeze, and equal-looking keys."""
    for i, k in enumerate(ks):
        t = type(k)
        if t is set:
            ks[i] = frozenset(k)
        elif t is list or t is dict or (t is Tagged and k._hash is None):
            # Aliases were charged when they were read, so the copy is not charged again.
            if _measure(k, info)[2] > KEY_DEPTH:
                raise Fail(
                    f"a mapping key nests deeper than {KEY_DEPTH} levels",
                    "limit",
                    _key_offset(events, e0, i),
                )
            ks[i] = freeze(k, sys.maxsize, sys.maxsize)[0]
        elif t is float and id(k) in wide:
            raise Fail(
                "a float key is outside the range of a double and cannot be told apart "
                "from its neighbours there",
                "limit",
                _key_offset(events, e0, i),
            )
    d: dict[Any, Any] = {}
    for i, k in enumerate(ks):
        if not strict:
            try:
                d[k] = vs[i]
            except RecursionError:
                raise _too_deep(events, e0, i) from None
            continue
        try:
            seen = k in d
            if seen:
                prev = next(p for p in d if p is k or p == k)
        except RecursionError:
            raise _too_deep(events, e0, i) from None
        if seen:
            where = _key_offset(events, e0, i)
            if type(prev) is type(k):
                raise Fail("the mapping has a duplicate key", "duplicate-key", where)
            raise Fail(
                "two keys are different in YAML but equal in Python (1, 1.0 and true), "
                "so they cannot share a dict",
                "other",
                where,
            )
        try:
            d[k] = vs[i]
        except RecursionError:
            raise _too_deep(events, e0, i) from None
    return d


def _key_offset(events: list[tuple], e0: tuple, index: int) -> int:
    """The offset of the ``index``-th key of the mapping that began with event ``e0``.

    Only an error path asks, so it finds the mapping by walking the events again."""
    pos = next(i for i, ev in enumerate(events) if ev is e0)
    depth = 0
    item = 0
    for ev in islice(events, pos + 1, None):
        kind = ev[0]
        if kind in (MAP_END, SEQ_END):
            if depth == 0:
                break
            depth -= 1
            continue
        if depth == 0:
            if item == 2 * index:
                return ev[1]
            item += 1
        if kind in (MAP_START, SEQ_START):
            depth += 1
    return e0[1]


def _map_last_wins(buf, e0, e_end, events, info, wide, intern_key):
    """The dict of a mapping where a repeated key keeps its last value (PyYAML's behaviour)."""
    ks = buf[0::2]
    vs = buf[1::2]
    if len(ks) != len(vs):
        raise Fail("a mapping key has no value", "syntax", e_end[1])
    try:
        if {*map(type, ks)} == {str}:
            ks = list(map(intern_key, ks, ks))
        return dict(zip(ks, vs, strict=True))
    except TypeError:
        pass  # an unhashable key: a collection, frozen below
    except RecursionError:
        raise _too_deep(events, e0, 0) from None
    return _map_slow(ks, vs, e0, events, info, wide, False)


def _map_merging(buf, e0, e_end, events, info, wide, intern_key):
    """A mapping that holds ``<<`` keys: the merged entries come first, the mapping's own
    entries replace theirs. In a list of mappings the first one wins."""
    if len(buf) % 2:
        raise Fail("a mapping key has no value", "syntax", e_end[1])
    merges: list[dict] = []
    own: list[Any] = []
    for i in range(0, len(buf), 2):
        if buf[i] is not MERGE:
            own.append(buf[i])
            own.append(buf[i + 1])
            continue
        v = buf[i + 1]
        found = reversed(v) if type(v) is list else (v,)
        for m in found:
            if type(m) is not dict:
                raise Fail(
                    "expected a mapping or list of mappings for merging",
                    "construct",
                    _key_offset(events, e0, i // 2),
                )
            merges.append(m)
    merged: dict = {}
    for m in merges:
        merged.update(m)
    merged.update(_map_last_wins(own, e0, e_end, events, info, wide, intern_key))
    # What an alias to this mapping stands for is the mapping as written, merged sources
    # included: that is what a copy of the text would hold, and what the budget must charge.
    nodes, size, height = 1, 0, 0
    for c in buf:
        if c is MERGE:
            nodes += 1
            continue
        sub = _measure(c, info) if type(c) in _COLLECTIONS else None
        if sub is None:
            nodes += 1
            size += _bytes(c)
        else:
            nodes += sub[0]
            size += sub[1]
            height = max(height, sub[2])
    info[id(merged)] = (nodes, size, height + 1, merged)
    return merged


_SCHEMAS = {
    "core": (KEYWORDS, NUMBER_START, WORD_START, number, tagged_scalar, _tag_collection, _map),
    "yaml11": (
        KEYWORDS11, NUMBER_START11, WORD_START11, number11, tagged_scalar11, tag_collection11,
        _map_last_wins,
    ),
    "text": (
        {}, frozenset(), frozenset(), number, tagged_scalar_text, tag_collection_text,
        _map_last_wins,
    ),
}  # fmt: skip
