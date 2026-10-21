"""Python values on their way into a lossless document.

``prepare`` turns whatever the caller hands over into plain dicts, lists, scalars, ``bytes`` and
``Tagged`` values, before the stream is touched. It reads the value one way: a subclass of
``str``, ``int`` or ``float`` by its base value (never an overridden method), a mapping by its
``items()``, any other sequence by iterating it, bytes-likes by their raw buffer. A value that
shares parts of itself is charged for the text it would write out, as an alias is charged in a
load, and refused before anything is copied when that is more than the limits allow. Nesting never
becomes recursion.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from itertools import islice
from typing import Any

from .._errors import make_error
from .._limits import _NONE
from .._types import FrozenMapping, Tagged

_SURROGATE = re.compile("[\ud800-\udfff]").search
_LEAVES = (str, int, float, bool, type(None))


class Prepared:
    """A value converted ahead of an edit (see ``prepare``)."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _utf8_len(s: str) -> int:
    return len(s) if s.isascii() else len(s.encode("utf-8", "surrogatepass"))


def _base_leaf(x: Any) -> Any:
    """A scalar subclass as its base value; other types are refused."""
    if isinstance(x, bool):
        return bool(x)
    if isinstance(x, str):
        return str.join("", [x])
    if isinstance(x, int):
        return int.__add__(x, 0)
    if isinstance(x, float):
        return float.__mul__(x, 1.0)
    raise TypeError(f"cannot represent an object of type {type(x).__name__} in YAML")


def prepare(value: Any, limits: Any) -> Prepared:
    """Convert ``value`` (see the module text); raises ``TypeError``, or a library error with
    category ``limit`` or ``other`` (a circular reference)."""
    max_depth, nf, nfl, bf, bfl, _isz, _ssz, _docs, digits = limits._native
    digit_cap = None if digits >= _NONE else digits
    on_path: set[int] = set()
    seen: dict[int, tuple[Any, int, int]] = {}  # id -> (converted, nodes, bytes)
    keep: list[Any] = []  # holds the containers, so no id is reused while we run
    own_nodes = own_bytes = 0
    ch_nodes = ch_bytes = 0
    # An open container: [kind, source, children iterator, converted children, nodes, bytes]
    stack: list[list] = []
    x: Any = value
    have_x = True
    done: Any = None
    done_n = done_b = 0
    while True:
        if have_x:
            have_x = False
            t = type(x)
            if t is str:
                _check_text(x)
                done, done_n, done_b = x, 1, _utf8_len(x)
            elif t is int or t is float or t is bool or x is None:
                done, done_n, done_b = x, 1, 0
                if t is int and digit_cap is not None:
                    _check_digits(x, digit_cap)
            else:
                kind = _classify(x)
                if kind == "leaf":
                    done = _base_leaf(x)
                    if type(done) is str:
                        _check_text(done)
                    done_n, done_b = 1, _utf8_len(done) if type(done) is str else 0
                    if type(done) is int and digit_cap is not None:
                        _check_digits(done, digit_cap)
                elif kind == "bytes":
                    done = x if t is bytes else bytes(memoryview(x))
                    done_n, done_b = 1, (len(done) * 4 + 2) // 3
                else:
                    oid = id(x)
                    if oid in on_path:
                        raise make_error("circular reference", "other")
                    again = seen.get(oid) if kind != "map" or not _key_position(stack) else None
                    if again is not None:
                        done, done_n, done_b = again
                        ch_nodes += done_n
                        ch_bytes += done_b
                        if ch_nodes > max(nf * own_nodes, nfl):
                            raise make_error(
                                "the value shares parts of itself so much that it expands past "
                                "the node limit",
                                "limit",
                            )
                        if ch_bytes > max(bf * own_bytes, bfl):
                            raise make_error(
                                "the value shares parts of itself so much that it expands past "
                                "the byte limit",
                                "limit",
                            )
                    else:
                        if len(stack) >= max_depth:
                            raise make_error(
                                f"the value nests deeper than the limit of {max_depth} levels",
                                "limit",
                            )
                        if kind == "map":
                            items = list(_flatten(x.items()))
                        elif kind == "tag":
                            items = [x.value]
                        else:
                            items = list(x) if type(x) in (list, tuple) else _drain(x)
                        runs = _leaf_run(items, digit_cap) if kind != "tag" else None
                        if runs is not None:
                            # Only plain scalars inside: no frame needed.
                            done_n, done_b = 1 + runs[0], runs[1]
                            if kind == "map":
                                done = dict(zip(items[0::2], items[1::2], strict=True))
                                if len(done) * 2 != len(items):
                                    raise make_error("the mapping has a key twice", "other")
                                if _key_position(stack):
                                    done = FrozenMapping(done)
                            elif type(x) is tuple and _key_position(stack):
                                done = tuple(items)
                            else:
                                done = items
                            own_nodes += done_n
                            own_bytes += done_b
                            if not (kind == "map" and _key_position(stack)):
                                seen[oid] = (done, done_n, done_b)
                            keep.append(x)
                        else:
                            on_path.add(oid)
                            keep.append(x)
                            stack.append([kind, x, iter(items), [], 1, 0])
                            continue
            if type(done) not in _CONTAINERS:
                own_nodes += done_n
                own_bytes += done_b
        else:
            frame = stack[-1]
            child = next(frame[2], _MISSING)
            if child is not _MISSING:
                x = child
                have_x = True
                continue
            stack.pop()
            src = frame[1]
            on_path.discard(id(src))
            kind = frame[0]
            out = frame[3]
            if kind == "map":
                if len(out) % 2:
                    raise TypeError("a mapping key has no value")
                pairs = dict(zip(out[0::2], out[1::2], strict=True))
                if len(pairs) * 2 != len(out):
                    raise make_error("the mapping has a key twice", "other")
                done = FrozenMapping(pairs) if _key_position(stack) else pairs
            elif kind == "tag":
                done = Tagged(src.tag, out[0])
            else:
                done = tuple(out) if _key_position(stack) else out
            done_n, done_b = frame[4], frame[5]
            if not (kind == "map" and _key_position(stack)):
                seen[id(src)] = (done, done_n, done_b)
            own_nodes += 1
        if not stack:
            return Prepared(done)
        top = stack[-1]
        top[3].append(done)
        top[4] += done_n
        top[5] += done_b


_CONTAINERS = (dict, list, tuple, Tagged, FrozenMapping)
_MISSING = object()


def _key_position(stack: list[list]) -> bool:
    """Whether the value being converted is a key of the mapping on top of the stack."""
    return bool(stack) and stack[-1][0] == "map" and len(stack[-1][3]) % 2 == 0


_PLAIN = frozenset({str, int, float, bool, type(None)})


def _leaf_run(items: list, digit_cap: int | None) -> tuple[int, int] | None:
    """``(nodes, bytes)`` of ``items`` when every one is a plain scalar of an exact type, else
    None. One pass in C for the common case of a long flat list."""
    if not set(map(type, items)) <= _PLAIN:
        return None
    strs = [s for s in items if type(s) is str]
    size = 0
    if strs:
        joined = "".join(strs)
        if not joined.isascii():
            if _SURROGATE(joined):
                raise make_error("the string holds a lone surrogate, which is not text", "encoding")
            size = len(joined.encode("utf-8"))
        else:
            size = len(joined)
    if digit_cap is not None:
        for n in items:
            if type(n) is int:
                _check_digits(n, digit_cap)
    return len(items), size


def _drain(seq: Any) -> list:
    """The items of a sequence other than a list, read a chunk at a time: ``list()`` of a long
    iterator is one call that cannot hear Ctrl-C, and the user's own sequence may be endless or
    huge."""
    it = iter(seq)
    out: list = []
    while True:
        chunk = list(islice(it, 4096))
        if not chunk:
            return out
        out += chunk


def _flatten(items: Any) -> Any:
    for entry in items:
        try:
            k, v = entry
        except (TypeError, ValueError):
            raise TypeError("a mapping's items() must give (key, value) pairs") from None
        yield k
        yield v


def _check_digits(x: int, cap: int) -> None:
    bits = x.bit_length()
    if bits > 3.33 * cap + 64:
        too_long = True
    elif bits > 3.32 * cap - 64:
        try:
            too_long = len(str(abs(x))) > cap
        except ValueError:  # beyond what Python prints
            too_long = True
    else:
        too_long = False
    if too_long:
        raise make_error(f"an integer is over the limit of {cap} digits", "limit")


def _check_text(s: str) -> None:
    if not s.isascii() and _SURROGATE(s):
        raise make_error("the string holds a lone surrogate, which is not text", "encoding")


def _classify(x: Any) -> str:
    if isinstance(x, _LEAVES):
        return "leaf"
    if isinstance(x, (bytes, bytearray, memoryview)):
        return "bytes"
    if isinstance(x, Tagged):
        return "tag"
    if isinstance(x, (Mapping, FrozenMapping)):
        return "map"
    if isinstance(x, (list, tuple, Sequence)) and not isinstance(x, (str, bytes)):
        return "seq"
    raise TypeError(f"cannot represent an object of type {type(x).__name__} in YAML")
