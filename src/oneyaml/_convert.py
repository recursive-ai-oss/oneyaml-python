"""Prepare a value for ``dumps`` when the caller asked for ``default`` or ``sort_keys``.

The backends write the types they know and refuse the rest. This step copies the value with
the two options applied: an object of an unknown type is replaced by what ``default`` returns
(as ``json.dumps`` does), and the entries of every mapping come out sorted by key. It runs
only when an option asks for it, and it keeps its own stack, so a deeply nested value costs
memory, not interpreter recursion.
"""

from __future__ import annotations

import builtins
from operator import itemgetter
from typing import Any

from ._errors import LimitError
from ._types import FrozenMapping, Tagged

# The same depth the writers refuse.
MAX_DEPTH = 10_000

# Python 3.15 and later have a built-in immutable mapping.
FROZENDICT: type | None = getattr(builtins, "frozendict", None)

_MAPPINGS: tuple[type, ...] = (dict, FrozenMapping) + ((FROZENDICT,) if FROZENDICT else ())
_SCALARS = (str, int, float, bytes, bytearray, memoryview)
_EXACT = frozenset({str, int, float, bool, bytes, type(None)})


def _key(k: Any) -> Any:
    """A mapping key with the built-in immutable mapping (Python 3.15) as the library's own, which
    the writers know as a complex key."""
    if FROZENDICT is not None and isinstance(k, FROZENDICT):
        return FrozenMapping({_key(a): _key(b) for a, b in k.items()})
    if isinstance(k, tuple):
        return tuple(_key(x) for x in k)
    return k


def convert(obj: Any, default: Any, sort_keys: bool, node_type: type | None = None) -> Any:  # noqa: C901
    """``obj`` with unknown objects replaced through ``default`` and mappings sorted by key if
    ``sort_keys``; values the writers know are copied as plain lists and dicts. A document or
    view (``node_type``) stands for its current value."""
    root: list[Any] = [None]
    # Work items: (value, holder, slot, depth) to convert and store, a Tagged to build once its
    # content is ready, or an id to take off the path when its subtree is done.
    stack: list[Any] = [(obj, root, 0, 1)]
    on_path: set[int] = set()
    done: dict[int, Any] = {}
    keep: list[Any] = []  # the originals, so an id stays theirs while a memo entry exists
    while stack:
        item = stack.pop()
        if item.__class__ is int:
            on_path.discard(item)
            continue
        if len(item) == 4:
            value, holder, slot, depth = item
        else:  # a Tagged waiting for its content
            _, tag, box, holder, slot, vid = item
            holder[slot] = done[vid] = Tagged(tag, box[0])
            continue
        t = type(value)
        if t in _EXACT:
            holder[slot] = value
            continue
        if node_type is not None and isinstance(value, node_type):
            stack.append((value.to_python(), holder, slot, depth))
            continue
        if isinstance(value, _SCALARS):
            holder[slot] = value  # a subclass is written as the type it extends
            continue
        is_seq = isinstance(value, (list, tuple))
        is_map = not is_seq and isinstance(value, _MAPPINGS)
        is_tag = not is_seq and not is_map and isinstance(value, Tagged)
        vid = id(value)
        if not (is_seq or is_map or is_tag):
            if default is None:
                holder[slot] = value  # the writer says what it cannot write
                continue
            if vid in on_path:
                raise ValueError("Circular reference detected")
            on_path.add(vid)
            stack.append(vid)
            keep.append(value)
            stack.append((default(value), holder, slot, depth))
            continue
        if vid in on_path:
            raise ValueError("Circular reference detected")
        if vid in done:
            holder[slot] = done[vid]
            continue
        if depth > MAX_DEPTH:
            raise LimitError(f"the value nests deeper than {MAX_DEPTH} levels", category="limit")
        on_path.add(vid)
        stack.append(vid)
        keep.append(value)
        if is_seq:
            walk = list.__iter__ if isinstance(value, list) else tuple.__iter__
            items = list(walk(value))
            new_list: list[Any] = [None] * len(items)
            done[vid] = holder[slot] = new_list
            for i, x in enumerate(items):
                stack.append((x, new_list, i, depth + 1))
        elif is_map:
            entries = []
            for entry in value.items():
                k, v = entry
                if FROZENDICT is not None and type(k) is not str:
                    try:
                        k = _key(k)
                    except RecursionError:
                        raise LimitError(
                            "a mapping key nests too deeply", category="limit"
                        ) from None
                entries.append((k, v))
            if sort_keys:
                try:
                    entries.sort(key=itemgetter(0))
                except TypeError:
                    kinds = sorted({type(k).__name__ for k, _ in entries})
                    raise TypeError(
                        "sort_keys cannot order keys of these types together: " + ", ".join(kinds)
                    ) from None
            new_map: dict[Any, Any] = dict.fromkeys(k for k, _ in entries)
            done[vid] = holder[slot] = new_map
            for k, v in entries:
                stack.append((v, new_map, k, depth + 1))
        else:
            box: list[Any] = [None]
            stack.append((None, value.tag, box, holder, slot, vid))
            stack.append((value.value, box, 0, depth + 1))
    return root[0]
