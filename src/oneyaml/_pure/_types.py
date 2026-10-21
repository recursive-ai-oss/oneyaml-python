"""Value types the loader builds besides the builtin ones."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


class Tagged:
    """A node whose tag the Core schema does not resolve.

    The node is kept as it was read (a string for a scalar, a list or dict for a collection)
    and is never turned into an object, whatever the tag says."""

    __slots__ = ("tag", "value")

    tag: str
    value: Any

    def __init__(self, tag: str, value: Any) -> None:
        object.__setattr__(self, "tag", tag)
        object.__setattr__(self, "value", value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Tagged is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Tagged is immutable")

    def __eq__(self, other: object) -> bool:
        if type(other) is not Tagged:
            return NotImplemented
        return self.tag == other.tag and self.value == other.value

    def __hash__(self) -> int:
        return hash((self.tag, self.value))

    def __repr__(self) -> str:
        return f"Tagged({self.tag!r}, {self.value!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (Tagged, (self.tag, self.value))


class FrozenMapping(Mapping[Any, Any]):
    """A read-only, hashable mapping: how a mapping that is itself a mapping key is read."""

    __slots__ = ("_d", "_h")

    def __init__(self, items: Any = ()) -> None:
        self._d = dict(items)
        self._h: int | None = None

    @classmethod
    def _wrap(cls, d: dict[Any, Any]) -> FrozenMapping:
        # Takes ownership of ``d``: the loader builds it for this purpose and drops it.
        self = object.__new__(cls)
        self._d = d
        self._h = None
        return self

    def __getitem__(self, key: Any) -> Any:
        return self._d[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        return key in self._d

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenMapping):
            return self._d == other._d
        if isinstance(other, Mapping):
            return self._d == dict(other.items())
        return NotImplemented

    def __hash__(self) -> int:
        h = self._h
        if h is None:
            h = self._h = hash(frozenset(self._d.items()))
        return h

    def __repr__(self) -> str:
        return f"FrozenMapping({self._d!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenMapping, (list(self._d.items()),))
