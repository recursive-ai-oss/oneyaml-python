"""Make a document match the data that was loaded from it, touching only what differs.

``edit_document`` compares a tree (what the program holds now) with the document's own value and
turns the differences into edits: a changed scalar is set, a new key added at the end of its
mapping, a removed key or item deleted, a new sequence item inserted. The document picks the
layout of every new piece from its neighbours, and everything else keeps its bytes.

Two cases cost more than they would with a position-aware insert: a key put in the middle of a
mapping, and a mapping whose keys were reordered. The keys from the first difference on are
removed and written again at the end, in the new order, so the comments attached to those keys
are not kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..._errors import make_error
from ..._types import FrozenMapping

_MISSING = object()


def _kind(v: Any) -> str:
    """The kind of a scalar for comparing a tree with a document: ``1`` and ``True`` differ."""
    if v is None:
        return "n"
    if isinstance(v, bool):
        return "b"
    if isinstance(v, int):
        return "i"
    if isinstance(v, float):
        return "f"
    if isinstance(v, str):
        return "s"
    return type(v).__name__


def same_scalar(a: Any, b: Any) -> bool:
    if a is b:
        return True
    if _kind(a) != _kind(b):
        return False
    if a == b:
        return True
    return a != a and b != b  # two NaNs


def _coll(v: Any) -> str | None:
    if isinstance(v, Mapping):
        return "m"
    if isinstance(v, (list, tuple)):
        return "s"
    return None


def _writable(value: Any) -> Any:
    """``value`` in a form the document writer takes (dates as text, style types as str)."""
    from ._emit import to_plain

    return to_plain(value)


def plan(cur: Any, orig: Any) -> list[tuple]:
    """The edits that turn ``orig`` (the document's value) into ``cur``, in an order that keeps
    every path valid: scalar sets first, then structure edits from the deepest level up.

    An edit is ``("set", path, value)``, ``("insert", path, index, value)`` or
    ``("remove", path)``; a path is a list of mapping keys and sequence indexes as text.
    """
    sets: list[tuple] = []
    structure: list[tuple[int, list[tuple]]] = []  # (depth, ops of one container)
    ck, ok = _coll(cur), _coll(orig)
    if ck != ok or ck is None:
        if not same_scalar(cur, orig) or ck is not None:
            sets.append(("set", [], cur))
        return sets
    seen: set[int] = set()
    same = _Same()
    stack: list[tuple[Any, Any, list]] = [(cur, orig, [])]
    while stack:
        c, o, path = stack.pop()
        if id(c) in seen:
            continue  # reached again through an alias: already compared
        seen.add(id(c))
        ops: list[tuple] = []
        kids: list[tuple] = []
        if isinstance(c, Mapping):
            _map(c, o, path, sets, ops, kids)
        else:
            _seq(c, o, path, sets, ops, kids, same)
        # Document order: an anchored node comes before its aliases, so it is the one edited.
        stack.extend(reversed(kids))
        if ops:
            structure.append((len(path), ops))
    structure.sort(key=lambda t: -t[0])
    out = sets
    for _, ops in structure:
        out.extend(ops)
    return out


def _compare(cv: Any, ov: Any, path: list, sets: list, stack: list) -> None:
    """One paired value: recurse into collections of the same kind, set anything else that
    differs."""
    kc, ko = _coll(cv), _coll(ov)
    if kc is not None:
        if kc == ko:
            stack.append((cv, ov, path))
        else:
            sets.append(("set", path, cv))
    elif ko is not None or not same_scalar(cv, ov):
        sets.append(("set", path, cv))


def _map(c: Mapping, o: dict, path: list, sets: list, ops: list, stack: list) -> None:
    ckeys = list(c)
    okeys = list(o)
    if ckeys == okeys:
        for k in ckeys:
            _compare(c[k], o[k], [*path, k], sets, stack)
        return
    keep = [k for k in okeys if k in c]  # old keys that survive, in old order
    n = 0
    while n < len(keep) and n < len(ckeys) and keep[n] == ckeys[n]:
        n += 1
    stable = set(keep[:n])
    for k in okeys:
        if k not in c:
            ops.append(("remove", [*path, k]))
    # Keys from the first difference on are written again at the end in the new order.
    for k in keep[n:]:
        ops.append(("remove", [*path, k]))
    for k in ckeys:
        if k in stable:
            _compare(c[k], o[k], [*path, k], sets, stack)
    for k in ckeys[n:]:
        ops.append(("set", [*path, k], c[k]))


def _seq(c: Any, o: list, path: list, sets: list, ops: list, stack: list, same: Any) -> None:
    cl, ol = len(c), len(o)

    eq = same

    p = 0
    top = min(cl, ol)
    while p < top and eq(c[p], o[p]):
        p += 1
    s = 0
    while s < top - p and eq(c[cl - 1 - s], o[ol - 1 - s]):
        s += 1
    cm, om = cl - p - s, ol - p - s  # sizes of the differing middle
    pair = min(cm, om)
    for i in range(pair):
        _compare(c[p + i], o[p + i], [*path, str(p + i)], sets, stack)
    for i in range(ol - s - 1, p + pair - 1, -1):
        ops.append(("remove", [*path, str(i)]))
    for i in range(p + pair, cl - s):
        ops.append(("insert", path, i, c[i]))


class _Same:
    """Equality of two trees as YAML values, remembered per pair of nodes so that comparing the
    children of a chain of nested nodes again and again stays linear in the size of the data."""

    def __init__(self) -> None:
        self.memo: dict[tuple[int, int], bool] = {}

    def __call__(self, a: Any, b: Any) -> bool:
        ka, kb = _coll(a), _coll(b)
        if ka is None and kb is None:
            return same_scalar(a, b)
        if ka != kb:
            return False
        memo = self.memo
        root = (id(a), id(b))
        known = memo.get(root)
        if known is not None:
            return known
        # Post-order walk: a pair is decided once every child pair is.
        stack: list[list] = [[a, b, None]]
        memo[root] = True  # a pair met again while it is open is equal so far (cycles)
        while stack:
            fr = stack[-1]
            x, y, kids = fr
            if kids is None:
                if len(x) != len(y) or (ka_ := _coll(x)) != _coll(y):
                    memo[(id(x), id(y))] = False
                    stack.pop()
                    continue
                if ka_ == "m":
                    if list(x) != list(y):
                        memo[(id(x), id(y))] = False
                        stack.pop()
                        continue
                    pairs = [(x[k], y[k]) for k in x]
                else:
                    pairs = list(zip(x, y, strict=True))
                kids = fr[2] = []
                for p, q in pairs:
                    kp, kq = _coll(p), _coll(q)
                    if kp is None and kq is None:
                        if not same_scalar(p, q):
                            kids.append(False)
                    elif kp != kq:
                        kids.append(False)
                    else:
                        r = memo.get((id(p), id(q)))
                        if r is None:
                            kids.append((p, q))
                        elif not r:
                            kids.append(False)
            # Resolve the children one at a time.
            while kids:
                k = kids[-1]
                if k is False:
                    kids.clear()
                    memo[(id(x), id(y))] = False
                    break
                r = memo.get((id(k[0]), id(k[1])))
                if r is None:
                    memo[(id(k[0]), id(k[1]))] = True
                    stack.append([k[0], k[1], None])
                    break
                kids.pop()
                if not r:
                    kids.clear()
                    memo[(id(x), id(y))] = False
                    break
            else:
                # every child pair equal
                stack.pop()
                continue
            if memo[(id(x), id(y))] is False:
                stack.pop()
        return memo[root]


def _check_path(path: list) -> None:
    for tok in path:
        if not (tok is None or isinstance(tok, (str, int, float, tuple, FrozenMapping))):
            raise make_error(
                f"an entry whose key is a {type(tok).__name__} cannot be edited in a document"
            )


def edit_document(handle: Any, d: int, cur: Any, orig: Any) -> int:
    """Apply the plan to document ``d`` of ``handle``; returns the number of edits.

    On a failure the document may hold some of the edits: the caller marks it dirty.
    """
    ops = plan(cur, orig)
    for op in ops:
        _check_path(op[1])
        kind = op[0]
        if kind == "set":
            handle.set(d, op[1], _writable(op[2]))
        elif kind == "insert":
            handle.insert(d, op[1], op[2], _writable(op[3]))
        else:
            handle.remove(d, op[1])
    return len(ops)


__all__ = ["edit_document", "plan", "same_scalar"]
