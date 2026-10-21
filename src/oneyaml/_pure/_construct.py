"""Build Python values from parser events.

One pass over the event list with explicit stacks: no recursion follows the nesting of the
input, whatever its depth.

Aliases share: an alias to a collection yields the very object the anchor made, so a document
that expands exponentially when written out loads in time proportional to its size. A
self-referential alias (``&a [*a]``) cannot occur here: an anchor is only usable once its node is
complete, and the one event stream that would break that is refused as soon as the alias is met.
Copies happen only for a collection used as a mapping key (it must be frozen to be hashable),
and those are charged to the alias budget.
"""

from __future__ import annotations

import sys
from itertools import islice
from typing import Any

from .._errors import YAMLError
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
from ._types import FrozenMapping, Tagged

_OPEN = object()  # an anchor whose node is not finished yet
_MISSING = object()
_COLLECTIONS = (list, dict, Tagged)
# A key is hashed and compared by the interpreter, which recurses in C on nested tuples; a key
# this deep is never real data and would risk the interpreter's own stack.
KEY_DEPTH = 200
_EXPANDS = "a collection used as a mapping key expands past the alias limit"


def load(data: Any, limits: Any, multi: bool) -> Any:
    """The value of the single document in ``data`` (``multi=False``) or a list of every
    document (``multi=True``). ``limits`` carries the attributes ``depth``, ``alias_nodes``,
    ``alias_bytes``, ``input_bytes``, ``scalar_bytes``, ``documents`` and ``int_digits``, each an
    int or ``None`` for unlimited."""
    cap_input = limits.input_bytes
    binary = isinstance(data, (bytes, bytearray, memoryview))
    if cap_input is not None and binary and len(data) > cap_input:
        raise YAMLError(f"the input is over the limit of {cap_input} bytes", "limit")
    text = decode(data)
    if cap_input is not None and not binary:
        size = len(text.encode("utf-8", "surrogatepass"))
        if size > cap_input:
            raise YAMLError(f"the input is over the limit of {cap_input} bytes", "limit")
    depth = limits.depth
    try:
        events = parse_events(text, sys.maxsize if depth is None else depth)
    except YAMLError as exc:
        exc.__dict__.pop("raw_events", None)
        exc.__dict__.pop("offset", None)
        raise
    try:
        _check_sizes(events, limits)
        docs = _build(text, events, limits, multi)
    except Fail as exc:
        off = exc.offset
        line, col = linecol(text, 0 if off is None else off)
        raise YAMLError(exc.message, exc.category, line, col) from None
    if multi:
        return docs
    return docs[0] if docs else None


def _check_sizes(events: list[tuple], limits: Any) -> None:
    """The limits that need only a look at the events (most inputs set none of them)."""
    ceil = limits.scalar_bytes
    if ceil is not None:
        for e in events:
            if e[0] == SCALAR:
                v = e[7]
                if len(v) > ceil or (len(v) * 4 > ceil and _utf8_len(v) > ceil):
                    raise Fail(f"a scalar is over the limit of {ceil} bytes", "limit", e[1])
    ceil = limits.documents
    if ceil is not None:
        count = 0
        for e in events:
            if e[0] == DOC_START:
                count += 1
                if count > ceil:
                    raise Fail(f"the stream has more than {ceil} documents", "limit", e[1])


def _utf8_len(v: str) -> int:
    return len(v.encode("utf-8", "surrogatepass"))


def _build(text: str, events: list[tuple], limits: Any, multi: bool) -> list[Any]:  # noqa: C901
    cap = limits.int_digits
    wide: dict[int, float] = {}
    # Freezing state: finished copies by id (the originals are kept alive here so ids stay
    # unique), and what is left of the alias budget.
    st: list[Any] = [{}, limits.alias_nodes, limits.alias_bytes]
    keys_seen: dict[str, str] = {}
    intern_key = keys_seen.setdefault
    anchors: dict[str, Any] = {}
    docs: list[Any] = []
    stack: list[tuple[list, tuple]] = []
    push = stack.append
    pop = stack.pop
    cur: list[Any] = []
    add = cur.append
    keywords = KEYWORDS
    number_start = NUMBER_START
    word_start = WORD_START
    missing = _MISSING
    e: tuple = ()
    try:
        for e in events:
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
                else:
                    v = tagged_scalar(tag, v, cap, wide)
                add(v)
                a = e[4]
                if a is not None:
                    anchors[a] = v
            elif k == MAP_END or k == SEQ_END:  # noqa: SIM109 (hot loop)
                buf = cur
                cur, e0 = pop()
                add = cur.append
                v = _map(buf, e0, e, events, st, wide, intern_key, text) if k == MAP_END else buf
                tag = e0[4]
                if tag is not None:
                    v = _tag_collection(tag, v, k == MAP_END, e0)
                add(v)
                a = e0[3]
                # An alias means the latest node of that name; one defined inside this node
                # (a redefinition) already took the name over.
                if a is not None and anchors[a] is _OPEN:
                    anchors[a] = v
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
                    raise Fail(f"the alias {e[3]} refers to a node that contains it", "syntax")
                if v is missing:
                    raise Fail(f"the alias {e[3]} has no anchor", "syntax")
                add(v)
            elif k == DOC_START:
                if docs and not multi:
                    raise Fail(
                        "the input holds more than one document; use loads_all to read them",
                        "other",
                        e[1],
                    )
                anchors = {}
                cur = []
                add = cur.append
            elif k == DOC_END:
                docs.append(cur[0] if cur else None)
    except Fail as exc:
        if exc.offset is None:
            exc.offset = _offset(e)
        raise
    return docs


def _offset(e: tuple) -> int:
    return e[1] if len(e) > 1 and isinstance(e[1], int) else 0


def _tag_collection(tag: str, v: Any, is_map: bool, e0: tuple) -> Any:
    """Apply the tag of a collection: the Core collection tags are transparent."""
    if tag == "!":
        return v
    want = COLLECTION_OK.get(tag)
    if want is not None:
        if want == (1 if is_map else 0):
            return v
        raise Fail(
            f"the tag {tag} does not fit a {'mapping' if is_map else 'sequence'}", "syntax", e0[1]
        )
    if tag in _WRONG_COLLECTION:
        raise Fail(f"the tag {tag} cannot be put on a collection", "syntax", e0[1])
    return Tagged(tag, v)


def _map(buf, e0, e_end, events, st, wide, intern_key, text):
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
    return _map_slow(ks, vs, e0, events, st, wide)


def _map_slow(ks, vs, e0, events, st, wide):
    """Mapping with keys that need care: collections to freeze, and equal-looking keys."""
    for i, k in enumerate(ks):
        t = type(k)
        if t is list or t is dict or t is Tagged:
            ks[i] = _freeze(k, st, (events, e0, i))
        elif t is float and id(k) in wide:
            raise _key_fail(
                "a float key is outside the range of a double and cannot be told apart "
                "from its neighbours there",
                "limit",
                events,
                e0,
                i,
            )
    d: dict[Any, Any] = {}
    for i, k in enumerate(ks):
        if k in d:
            prev = next(p for p in d if p is k or p == k)
            if type(prev) is type(k):
                raise _key_fail("the mapping has a duplicate key", "duplicate-key", events, e0, i)
            raise _key_fail(
                f"the keys {prev!r} and {k!r} differ in YAML but are one key in a Python dict",
                "limit",
                events,
                e0,
                i,
            )
        d[k] = vs[i]
    return d


def _key_fail(message: str, category: str, events, e0, i: int) -> Fail:
    return Fail(message, category, _key_offset(events, e0, i))


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


def _scalar_size(x: Any) -> int:
    return len(x) if type(x) is str or type(x) is bytes else 8


def _freeze(root: Any, st: list[Any], where: tuple) -> Any:
    """A hashable copy of a collection (``list`` to ``tuple``, ``dict`` to ``FrozenMapping``).

    Shared parts are copied once, but the cost charged is what writing the key out in full would
    take (nodes, and bytes of text): hashing and comparing a key walk it in full. An explicit
    stack keeps depth from becoming recursion."""
    memo = st[0]
    stack = [root]
    while stack:
        obj = stack[-1]
        if id(obj) in memo:
            stack.pop()
            continue
        t = type(obj)
        if t is list:
            kids = obj
        elif t is dict:
            kids = list(obj.values())
        else:
            kids = [obj.value]
        waiting = [c for c in kids if type(c) in _COLLECTIONS and id(c) not in memo]
        if waiting:
            stack.extend(waiting)
            continue
        stack.pop()
        nodes = 1
        size = 0
        depth = 0
        if t is dict:
            items = []
            for key, c in obj.items():
                for part in (key, c):
                    ent = memo.get(id(part))
                    if ent is not None:
                        _, f, n, b, d = ent
                    else:
                        f, n, b, d = part, 1, _scalar_size(part), 0
                    nodes += n
                    size += b
                    depth = max(depth, d)
                    items.append(f)
            frozen: Any = FrozenMapping._wrap(dict(zip(items[0::2], items[1::2], strict=True)))
        else:
            items = []
            for c in kids:
                ent = memo.get(id(c))
                if ent is not None:
                    _, f, n, b, d = ent
                else:
                    f, n, b, d = c, 1, _scalar_size(c), 0
                nodes += n
                size += b
                depth = max(depth, d)
                items.append(f)
            frozen = tuple(items) if t is list else Tagged(obj.tag, items[0])
        depth += 1
        if depth > KEY_DEPTH:
            raise Fail(
                f"a mapping key nests deeper than {KEY_DEPTH} levels", "limit", _key_offset(*where)
            )
        left = st[1]
        if left is not None and nodes > left:
            raise Fail(_EXPANDS, "limit", _key_offset(*where))
        left = st[2]
        if left is not None and size > left:
            raise Fail(_EXPANDS, "limit", _key_offset(*where))
        memo[id(obj)] = (obj, frozen, nodes, size, depth)
        memo[id(frozen)] = (frozen, frozen, nodes, size, depth)
    _, frozen, nodes, size, _ = memo[id(root)]
    # A shared copy is charged on every use, so the check is on what is left.
    if st[1] is not None:
        st[1] -= nodes
    if st[2] is not None:
        st[2] -= size
    if (st[1] is not None and st[1] < 0) or (st[2] is not None and st[2] < 0):
        raise Fail(_EXPANDS, "limit", _key_offset(*where))
    return frozen


def check_expansion(root: Any, max_nodes: int | None, max_bytes: int | None) -> tuple[int, int]:
    """The nodes and bytes of ``root`` written out in full, shared parts counted each time.

    Raises a ``limit`` error as soon as a part passes ``max_nodes`` or ``max_bytes``, without
    expanding anything: sizes are worked out once per shared object. Whatever walks a loaded value
    in full (dumping, converting, comparing) calls this first."""
    done: dict[int, tuple[Any, int, int]] = {}
    stack = [root]
    while stack:
        obj = stack[-1]
        if id(obj) in done:
            stack.pop()
            continue
        t = type(obj)
        if t is list or t is tuple:
            kids: Any = obj
        elif t is dict or t is FrozenMapping:
            kids = [x for kv in obj.items() for x in kv]
        elif t is Tagged:
            kids = (obj.value,)
        else:
            stack.pop()
            continue
        waiting = [c for c in kids if type(c) in _WALKED and id(c) not in done]
        if waiting:
            stack.extend(waiting)
            continue
        stack.pop()
        nodes = 1
        size = 0
        for c in kids:
            ent = done.get(id(c)) if type(c) in _WALKED else None
            if ent is None:
                nodes += 1
                size += _scalar_size(c)
            else:
                nodes += ent[1]
                size += ent[2]
        if (max_nodes is not None and nodes > max_nodes) or (
            max_bytes is not None and size > max_bytes
        ):
            raise YAMLError("the value expands past the alias limit when written out", "limit")
        done[id(obj)] = (obj, nodes, size)
    ent = done.get(id(root))
    if ent is None:
        return 1, _scalar_size(root)
    return ent[1], ent[2]


_WALKED = (list, tuple, dict, FrozenMapping, Tagged)
