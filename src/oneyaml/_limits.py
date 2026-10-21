"""Resource limits for reading untrusted input."""

from __future__ import annotations

from typing import Any, TypeAlias

__all__ = ["Limits"]

_MIB = 1024 * 1024
# Stands for "no limit" when the limits are handed to the native module.
_NONE = (1 << 64) - 1

# A fixed amount, ``(factor, floor)`` for ``max(factor x measured size, floor)``, or None.
Budget: TypeAlias = "int | tuple[int, int] | None"


# Marks a field that ``replace`` leaves as it is (None already means "no limit").
_KEEP: Any = object()


def _pair(value: Budget) -> tuple[int, int]:
    """Alias budgets as (factor, floor); a disabled budget is (0, no limit)."""
    if value is None:
        return 0, _NONE
    if isinstance(value, tuple):
        factor, floor = value
        return int(factor), int(floor)
    return 0, int(value)


class Limits:
    """Immutable resource limits; ``Limits()`` holds the defaults.

    Every field is a number, or None for no limit.

    Args:
        depth: nesting of collections; an alias to a collection counts at the depth it is used.
        alias_nodes: nodes that aliases may stand for in one document. An alias shares the
            object it names, but is charged what a copy would hold, so a small input cannot
            describe an enormous value. An int, or ``(factor, floor)`` for
            ``max(factor x nodes of the document, floor)``.
        alias_bytes: the same for bytes of scalar text; an int, or ``(factor, floor)`` of the
            bytes of the document read so far.
        input_size: bytes of input (after decoding to UTF-8).
        scalar_size: bytes in one scalar value.
        documents: documents in a stream.
        int_digits: decimal digits of an integer read from text (CPython's own default).

    Use :meth:`untrusted` for input you do not control and :meth:`none` to turn everything off.
    Compat modules never loosen these.
    """

    __slots__ = (
        "_native",
        "alias_bytes",
        "alias_nodes",
        "depth",
        "documents",
        "input_size",
        "int_digits",
        "scalar_size",
    )

    depth: int | None
    alias_nodes: Budget
    alias_bytes: Budget
    input_size: int | None
    scalar_size: int | None
    documents: int | None
    int_digits: int | None

    def __init__(
        self,
        depth: int | None = 10_000,
        alias_nodes: Budget = (10, 1_000_000),
        alias_bytes: Budget = (10, 64 * _MIB),
        input_size: int | None = None,
        scalar_size: int | None = None,
        documents: int | None = None,
        int_digits: int | None = 4300,
    ) -> None:
        for name, value in (
            ("depth", depth),
            ("input_size", input_size),
            ("scalar_size", scalar_size),
            ("documents", documents),
            ("int_digits", int_digits),
        ):
            _check(name, value)
        for name, budget in (("alias_nodes", alias_nodes), ("alias_bytes", alias_bytes)):
            for part in budget if isinstance(budget, tuple) else (budget,):
                _check(name, part)
        values = {
            "depth": depth,
            "alias_nodes": alias_nodes,
            "alias_bytes": alias_bytes,
            "input_size": input_size,
            "scalar_size": scalar_size,
            "documents": documents,
            "int_digits": int_digits,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        nf, nfloor = _pair(alias_nodes)
        bf, bfloor = _pair(alias_bytes)
        none = lambda v: _NONE if v is None else v  # noqa: E731
        # The tuple the native module reads, built once.
        object.__setattr__(
            self,
            "_native",
            (
                none(depth),
                nf,
                nfloor,
                bf,
                bfloor,
                none(input_size),
                none(scalar_size),
                none(documents),
                none(int_digits),
            ),  # fmt: skip
        )

    @classmethod
    def untrusted(cls) -> Limits:
        """Tighter limits for input from the network or other untrusted sources."""
        return cls(
            depth=1_000,
            alias_nodes=(2, 100_000),
            alias_bytes=(2, 8 * _MIB),
            input_size=64 * _MIB,
            scalar_size=16 * _MIB,
            documents=10_000,
        )

    @classmethod
    def none(cls) -> Limits:
        """No limits at all. Only for input you trust completely."""
        return cls(None, None, None, None, None, None, None)

    def replace(
        self,
        *,
        depth: int | None = _KEEP,
        alias_nodes: Budget = _KEEP,
        alias_bytes: Budget = _KEEP,
        input_size: int | None = _KEEP,
        scalar_size: int | None = _KEEP,
        documents: int | None = _KEEP,
        int_digits: int | None = _KEEP,
    ) -> Limits:
        """A copy with the fields you name changed; the others keep their values."""
        changes = {
            "depth": depth,
            "alias_nodes": alias_nodes,
            "alias_bytes": alias_bytes,
            "input_size": input_size,
            "scalar_size": scalar_size,
            "documents": documents,
            "int_digits": int_digits,
        }
        fields = {name: getattr(self, name) for name in self._FIELDS}
        fields.update({k: v for k, v in changes.items() if v is not _KEEP})
        return Limits(**fields)

    _FIELDS = (
        "depth",
        "alias_nodes",
        "alias_bytes",
        "input_size",
        "scalar_size",
        "documents",
        "int_digits",
    )

    def __setattr__(self, name, value):
        raise AttributeError("Limits is immutable; use replace()")

    def __reduce__(self):
        return (_rebuild, tuple(getattr(self, n) for n in self._FIELDS))

    def __eq__(self, other):
        if not isinstance(other, Limits):
            return NotImplemented
        return self._native == other._native

    def __hash__(self):
        return hash(self._native)

    def __repr__(self):
        fields = ", ".join(f"{n}={getattr(self, n)!r}" for n in self._FIELDS)
        return f"Limits({fields})"


def _rebuild(*fields) -> Limits:
    return Limits(*fields)


def _check(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"limit {name} must be an int or None, not {type(value).__name__}")
    if value < 0:
        raise ValueError(f"limit {name} must not be negative")
    if value >= _NONE:
        raise ValueError(f"limit {name} is too large; use None for no limit")
