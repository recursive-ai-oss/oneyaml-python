"""Value types that loading produces besides the builtin ones."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

__all__ = ["FrozenMapping", "Tagged"]


class Tagged:
    """A node whose tag the Core schema does not resolve, kept as it was written.

    ``tag`` is the full tag (``"!color"``, ``"tag:example.com,2024:point"``) and ``value`` the
    node's content: the text for a scalar, a list or dict for a collection. Nothing is ever
    constructed from a tag.
    """

    __slots__ = ("tag", "value")
    tag: str
    value: Any

    def __init__(self, tag: str, value: Any) -> None:
        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "value", value)

    def __setattr__(self, name, value):
        raise AttributeError("Tagged is immutable")

    def __eq__(self, other):
        if not isinstance(other, Tagged):
            return NotImplemented
        return self.tag == other.tag and self.value == other.value

    def __hash__(self):
        return hash((self.tag, self.value))

    def __repr__(self):
        return f"Tagged({self.tag!r}, {self.value!r})"

    def __reduce__(self):
        return (Tagged, (self.tag, self.value))


class FrozenMapping(Mapping):
    """A read-only, hashable mapping: what a YAML mapping becomes when it is a mapping key."""

    __slots__ = ("_hash", "_items")

    def __init__(self, items: Mapping | None = None) -> None:
        # The dict is taken over, not copied: the loader hands over one it has just built.
        self._items = items if type(items) is dict else dict(items or {})
        self._hash: int | None = None

    def __getitem__(self, key):
        return self._items[key]

    def __iter__(self) -> Iterator:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key) -> bool:
        return key in self._items

    def __hash__(self) -> int:
        if self._hash is None:
            self._hash = hash(frozenset(self._items.items()))
        return self._hash

    def __repr__(self):
        return f"FrozenMapping({self._items!r})"

    def __reduce__(self):
        return (FrozenMapping, (self._items,))


class _Budget(ValueError):
    """Raised by freeze when the expansion is larger than allowed."""


def freeze(obj: Any, max_nodes: int, max_bytes: int) -> tuple[Any, int, int]:
    """Copy ``obj`` into hashable form: lists become tuples, dicts ``FrozenMapping``.

    Used by the loader when an alias to a collection is a mapping key. Returns the frozen value
    and the nodes and bytes of text it had to copy; raises ``_Budget`` past either bound. Walks
    with an explicit stack, so nesting depth never matters.
    """
    nodes = 0
    size = 0
    # Post-order: a work item is (value, state). State None = visit; otherwise the parts to join.
    out: list[Any] = []
    work: list[Any] = [(obj, None)]
    while work:
        item, state = work.pop()
        if state is None:
            nodes += 1
            if nodes > max_nodes:
                raise _Budget("alias expansion exceeds the node limit")
            t = type(item)
            if t is list or t is tuple:
                work.append((item, ("seq", len(item))))
                work.extend((child, None) for child in reversed(item))
            elif t is dict:
                keys = list(item)
                work.append((item, ("map", keys)))
                work.extend((item[k], None) for k in reversed(keys))
            elif t is Tagged:
                work.append((item, ("tag", None)))
                work.append((item.value, None))
            elif t is FrozenMapping:
                work.append((item, ("map", list(item._items))))
                work.extend((item._items[k], None) for k in reversed(list(item._items)))
            else:
                if t is str or t is bytes:
                    size += len(item)
                    if size > max_bytes:
                        raise _Budget("alias expansion exceeds the byte limit")
                out.append(item)
        else:
            kind, extra = state
            if kind == "seq":
                parts = out[len(out) - extra :]
                del out[len(out) - extra :]
                out.append(tuple(parts))
            elif kind == "map":
                n = len(extra)
                parts = out[len(out) - n :]
                del out[len(out) - n :]
                out.append(FrozenMapping(dict(zip(extra, parts, strict=True))))
            else:
                out.append(Tagged(item.tag, out.pop()))
    return out[0], nodes, size


def big_int(text: str) -> int:
    """Convert a decimal integer too long for ``int(text)`` under CPython's digit cap."""
    sign = -1 if text.startswith("-") else 1
    digits = text.lstrip("+-")
    # Pieces of 1000 digits are under any cap CPython sets by default; join them in base 10**1000.
    step = 1000
    head = len(digits) % step
    chunks = [digits[:head]] if head else []
    chunks += [digits[i : i + step] for i in range(head, len(digits), step)]
    result = 0
    base = 10**step
    for chunk in chunks:
        result = result * base + int(chunk)
    return sign * result
