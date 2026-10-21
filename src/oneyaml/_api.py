"""The public functions: ``loads``, ``loads_all``, ``load`` and ``load_all`` read YAML;
``dumps``, ``dumps_all``, ``dump`` and ``dump_all`` write it.

They are thin: they check the arguments and hand them to the active backend, which builds the
Python values or the text (the type mapping is described in the package documentation).
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping
from typing import IO, Any

from ._limits import Limits

__all__ = ["dump", "dump_all", "dumps", "dumps_all", "load", "load_all", "loads", "loads_all"]

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
    return _impl.load(data, limits, multi)


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


_MAX_INDENT = 16


def _options(indent: int, flow: bool, explicit_start: bool) -> tuple[int, bool, bool]:
    if type(indent) is not int:
        raise TypeError(f"indent must be an int, not {type(indent).__name__}")
    if not 1 <= indent <= _MAX_INDENT:
        raise ValueError(f"indent must be between 1 and {_MAX_INDENT}, not {indent}")
    return indent, bool(flow), bool(explicit_start)


def _documents(objs: Iterable[Any]) -> list[Any]:
    # A string or a mapping is almost certainly one document passed to the wrong function.
    if isinstance(objs, (str, bytes, bytearray, Mapping)):
        raise TypeError(f"dumps_all expects an iterable of documents, not {type(objs).__name__}")
    return list(objs)


def dumps(obj: Any, *, indent: int = 2, flow: bool = False, explicit_start: bool = False) -> str:
    """Write ``obj`` as one YAML document.

    The text reads back as the same value under the YAML 1.2 Core schema (and under YAML 1.1:
    a string that either schema would read as something else is quoted). ``None``, ``bool``,
    ``int`` (any size), ``float`` (``.inf``, ``-.inf`` and ``.nan`` for the non-finite ones),
    ``str``, ``list`` and ``tuple`` (sequences), ``dict`` and ``FrozenMapping`` (mappings, in
    insertion order), ``bytes`` (``!!binary``) and ``Tagged`` are written; a tuple or a
    ``FrozenMapping`` as a mapping key becomes a complex key. Any other type is a
    ``TypeError``. Layout only changes with the options:

    Args:
        indent: spaces per nesting level in block style, 1 to 16.
        flow: write every collection in flow style (``{a: [1, 2]}``) on one line.
        explicit_start: begin the document with ``---``.

    Raises:
        TypeError: a value of a type that cannot be written.
        ValueError: a value that contains itself, a string with a lone surrogate, a tag that
            cannot be written, or an option out of range.
        LimitError: the value nests more than 10,000 levels.
    """
    return _impl.dump([obj], _options(indent, flow, explicit_start), False)


def dumps_all(
    objs: Iterable[Any], *, indent: int = 2, flow: bool = False, explicit_start: bool = False
) -> str:
    """Write each of ``objs`` as a document of one stream, separated by ``---``."""
    return _impl.dump(_documents(objs), _options(indent, flow, explicit_start), True)


def _write(fp: Any, text: str) -> None:
    try:
        write = fp.write
    except AttributeError:
        raise TypeError(f"expected a file object, not {type(fp).__name__}") from None
    # A file opened in binary mode takes UTF-8 bytes.
    write(text.encode("utf-8") if isinstance(fp, (io.RawIOBase, io.BufferedIOBase)) else text)


def dump(
    obj: Any, fp: IO[str] | IO[bytes], *, indent: int = 2, flow: bool = False,
    explicit_start: bool = False,
) -> None:  # fmt: skip
    """Write ``obj`` as one YAML document to an open file (see :func:`dumps`)."""
    _write(fp, dumps(obj, indent=indent, flow=flow, explicit_start=explicit_start))


def dump_all(
    objs: Iterable[Any], fp: IO[str] | IO[bytes], *, indent: int = 2, flow: bool = False,
    explicit_start: bool = False,
) -> None:  # fmt: skip
    """Write each of ``objs`` as a document of one stream to an open file."""
    _write(fp, dumps_all(objs, indent=indent, flow=flow, explicit_start=explicit_start))
