"""The public reading functions: ``loads``, ``loads_all``, ``load`` and ``load_all``.

They are thin: they check the argument and hand it to the active backend, which builds the
Python values (the type mapping is described in the package documentation).
"""

from __future__ import annotations

from typing import IO, Any

from ._limits import Limits

__all__ = ["load", "load_all", "loads", "loads_all"]

# The backend module in use; the package sets it once at import (tests point it elsewhere).
_impl: Any = None

_DEFAULT_LIMITS = Limits()


def _call(data: Any, limits: Limits | None, multi: bool) -> Any:
    if limits is None:
        limits = _DEFAULT_LIMITS
    elif not isinstance(limits, Limits):
        raise TypeError(f"limits must be a Limits, not {type(limits).__name__}")
    t = type(data)
    if t is not str and t is not bytes:
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        elif not isinstance(data, (str, bytes)):
            raise TypeError(f"expected str or bytes, not {t.__name__}")
    try:
        load = _impl.load
    except AttributeError:
        raise NotImplementedError("loading is not yet implemented on the pure backend") from None
    return load(data, limits, multi)


def loads(data: str | bytes, *, limits: Limits | None = None) -> Any:
    """Read the single YAML document in ``data`` into Python objects.

    ``data`` is text, or bytes in UTF-8, UTF-16 or UTF-32 (a byte order mark is honoured). An
    empty stream gives None. A stream of several documents is an error that points to
    :func:`loads_all`.

    Raises:
        YAMLError: the text is not valid YAML or goes past a limit.
        TypeError: ``data`` is neither text nor bytes, or ``limits`` is not a ``Limits``.
    """
    return _call(data, limits, False)


def loads_all(data: str | bytes, *, limits: Limits | None = None) -> list[Any]:
    """Read every document in ``data``; an empty stream gives an empty list."""
    return _call(data, limits, True)


def _read(fp: Any) -> Any:
    try:
        read = fp.read
    except AttributeError:
        raise TypeError(f"expected a file object, not {type(fp).__name__}") from None
    return read()


def load(fp: IO[str] | IO[bytes], *, limits: Limits | None = None) -> Any:
    """Read the single document in an open text or binary file."""
    return _call(_read(fp), limits, False)


def load_all(fp: IO[str] | IO[bytes], *, limits: Limits | None = None) -> list[Any]:
    """Read every document in an open text or binary file."""
    return _call(_read(fp), limits, True)
