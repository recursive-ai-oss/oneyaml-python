"""The public functions: ``loads``, ``loads_all``, ``load`` and ``load_all`` read YAML;
``dumps``, ``dumps_all``, ``dump`` and ``dump_all`` write it.

They are thin: they check the arguments and hand them to the active backend, which builds the
Python values or the text (the type mapping is described in the package documentation).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable, Mapping
from typing import IO, Any

from ._convert import FROZENDICT, convert
from ._errors import YAMLError
from ._limits import Limits

__all__ = ["dump", "dump_all", "dumps", "dumps_all", "load", "load_all", "loads", "loads_all"]

# The backend module in use; the package sets it once at import (tests point it elsewhere).
_impl: Any = None

# The class of documents and their views; the document module sets it when it is imported (it
# imports this module, so this one cannot import it).
_NODE: type | None = None

_DEFAULT_LIMITS = Limits()


def _call(
    data: Any,
    limits: Limits | None,
    multi: bool,
    merge_keys: bool = False,
    parse_float: Callable[[str], Any] | None = None,
    tags: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Any:
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
    if not merge_keys and parse_float is None and tags is None:
        return _impl.load(data, limits, multi)
    return _load_with(data, limits, multi, merge_keys, parse_float, tags)


def _load_with(
    data: Any,
    limits: Limits,
    multi: bool,
    merge_keys: bool,
    parse_float: Any,
    tags: Any,
) -> Any:
    """A load that uses one of the options. Both backends build the values the same way here:
    the pure builder, from the pure parser's events or the native backend's own."""
    if parse_float is not None and not callable(parse_float):
        raise TypeError(f"parse_float must be callable, not {type(parse_float).__name__}")
    if tags is not None:
        if not isinstance(tags, Mapping):
            raise TypeError(f"tags must be a mapping, not {type(tags).__name__}")
        for tag, make in tags.items():
            if not isinstance(tag, str):
                raise TypeError(f"tags has a key that is not a str: {tag!r}")
            if not callable(make):
                raise TypeError(f"tags[{tag!r}] must be callable, not {type(make).__name__}")
        tags = dict(tags)
    from ._pure._construct import load_with

    return load_with(
        data,
        limits,
        multi,
        merge_keys=bool(merge_keys),
        parse_float=parse_float,
        tags=tags,
        native_events=getattr(_impl, "events", None),
    )


def loads(
    data: str | bytes,
    *,
    limits: Limits | None = None,
    merge_keys: bool = False,
    parse_float: Callable[[str], Any] | None = None,
    tags: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Any:
    """Read the single YAML document in ``data`` into Python objects.

    ``data`` is text, or bytes in UTF-8, UTF-16 or UTF-32 (a byte order mark is honoured). An
    empty stream gives None. A stream of several documents is an error that points to
    :func:`loads_all`. The value follows the YAML 1.2 Core schema (``yes`` is a string, ``0755``
    is 755, a timestamp is text).

    Args:
        limits: resource limits (see :class:`Limits`); the defaults refuse hostile input.
        merge_keys: apply YAML 1.1 merge keys. A ``<<`` key whose value is a mapping (or a
            list of mappings) adds that mapping's entries to the one that holds it; the
            mapping's own entries win, and in a list the first mapping wins. YAML 1.2 has no
            merge keys, so by default ``<<`` is an ordinary key; turn this on for files written
            for PyYAML, Kubernetes or Compose that rely on ``<<: *anchor``. Only a plain ``<<``
            merges (a quoted ``"<<"`` is a key), and a repeated key is still an error. Aliases
            count against the alias limits as they do everywhere.
        parse_float: called with the text of each finite float (``"1.10"``) in place of
            ``float``, as in :func:`json.loads`: ``parse_float=decimal.Decimal`` keeps the
            digits. ``.inf`` and ``.nan`` stay floats.
        tags: maps a tag (``"!env"``; the full form for ``!!`` tags) to a function that gets the
            node's plain content (text, list or dict) and returns the value to put in place of
            the :class:`Tagged`. Only the tags named are touched; nothing is constructed
            otherwise. Exceptions raised by the functions reach the caller as they are.

    Reading with ``merge_keys``, ``parse_float`` or ``tags`` takes a slower path than a plain
    ``loads``.

    Raises:
        YAMLError: the text is not valid YAML or goes past a limit.
        TypeError: ``data`` is neither text nor bytes, ``limits`` is not a ``Limits``, or an
            option has the wrong type.
    """
    # The common call, straight to the backend: one function call less per load.
    if (
        limits is None
        and not merge_keys
        and parse_float is None
        and tags is None
        and (type(data) is str or type(data) is bytes)
    ):
        return _impl.load(data, _DEFAULT_LIMITS, False)
    return _call(data, limits, False, merge_keys, parse_float, tags)


def loads_all(
    data: str | bytes,
    *,
    limits: Limits | None = None,
    merge_keys: bool = False,
    parse_float: Callable[[str], Any] | None = None,
    tags: Mapping[str, Callable[[Any], Any]] | None = None,
) -> list[Any]:
    """Read every document in ``data``; an empty stream gives an empty list. The options are
    those of :func:`loads`."""
    if (
        limits is None
        and not merge_keys
        and parse_float is None
        and tags is None
        and (type(data) is str or type(data) is bytes)
    ):
        return _impl.load(data, _DEFAULT_LIMITS, True)
    return _call(data, limits, True, merge_keys, parse_float, tags)


def _read(fp: Any) -> Any:
    try:
        read = fp.read
    except AttributeError:
        raise TypeError(f"expected a file object, not {type(fp).__name__}") from None
    return read()


def _name(fp: Any) -> str | None:
    name = getattr(fp, "name", None)
    return name if isinstance(name, str) else None


def _from_file(fp: Any, multi: bool, *args: Any) -> Any:
    data = _read(fp)
    try:
        return _call(data, args[0], multi, *args[1:])
    except YAMLError as exc:
        # Say which file, when the file has a name: a message about line 4 is little help
        # among several files.
        name = _name(fp)
        if name is not None:
            exc.set_source(name)
        raise


def load(
    fp: IO[str] | IO[bytes],
    *,
    limits: Limits | None = None,
    merge_keys: bool = False,
    parse_float: Callable[[str], Any] | None = None,
    tags: Mapping[str, Callable[[Any], Any]] | None = None,
) -> Any:
    """Read the single document in an open text or binary file; the options are those of
    :func:`loads`. An error names the file (``YAMLError.source``) when it has a name."""
    return _from_file(fp, False, limits, merge_keys, parse_float, tags)


def load_all(
    fp: IO[str] | IO[bytes],
    *,
    limits: Limits | None = None,
    merge_keys: bool = False,
    parse_float: Callable[[str], Any] | None = None,
    tags: Mapping[str, Callable[[Any], Any]] | None = None,
) -> list[Any]:
    """Read every document in an open text or binary file; the options are those of
    :func:`loads`."""
    return _from_file(fp, True, limits, merge_keys, parse_float, tags)


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


def _write_docs(
    objs: list[Any],
    opts: tuple[int, bool, bool],
    multi: bool,
    default: Callable[[Any], Any] | None,
    sort_keys: bool,
) -> str:
    if default is not None and not callable(default):
        raise TypeError(f"default must be callable, not {type(default).__name__}")
    node = _NODE
    if default is not None or sort_keys:
        objs = [convert(o, default, bool(sort_keys), node) for o in objs]
    elif node is not None:
        # A document or a view stands for its current value.
        for o in objs:
            if isinstance(o, node):
                objs = [x.to_python() if isinstance(x, node) else x for x in objs]
                break
    if FROZENDICT is None:
        return _impl.dump(objs, opts, multi)
    try:
        return _impl.dump(objs, opts, multi)
    except TypeError as exc:
        # The writers know the library's own immutable mapping; the built-in one of newer
        # Pythons is copied to a dict only when one is found, so other values pay nothing.
        if "frozendict" not in str(exc):
            raise
    return _impl.dump([convert(o, None, False, node) for o in objs], opts, multi)


def dumps(
    obj: Any,
    *,
    indent: int = 2,
    flow: bool = False,
    explicit_start: bool = False,
    default: Callable[[Any], Any] | None = None,
    sort_keys: bool = False,
) -> str:
    """Write ``obj`` as one YAML document.

    The text reads back as the same value under the YAML 1.2 Core schema (and under YAML 1.1:
    a string that either schema would read as something else is quoted). ``None``, ``bool``,
    ``int`` (any size), ``float`` (``.inf``, ``-.inf`` and ``.nan`` for the non-finite ones),
    ``str``, ``list`` and ``tuple`` (sequences), ``dict``, ``FrozenMapping`` and ``frozendict``
    (mappings, in insertion order), ``bytes`` (``!!binary``) and ``Tagged`` are written; a tuple
    or a ``FrozenMapping`` as a mapping key becomes a complex key. Any other type is a
    ``TypeError``, unless ``default`` turns it into something that can be written. Subclasses
    are written as the type they extend, from its own data and never through methods they
    override (a ``str`` subclass as its text, ``bytes``, ``bytearray`` and ``memoryview`` as
    their raw bytes, a mapping's ``items()`` entries unpacked as ``key, value = entry`` does).
    A :class:`Document` or a view of one is written as its current value (comments and layout
    are not kept; ``str(doc)`` is the text with them); inside other values, a document or view
    is written only when ``default`` or ``sort_keys`` is given. Layout only changes with the
    options:

    Args:
        indent: spaces per nesting level in block style, 1 to 16.
        flow: write every collection in flow style (``{a: [1, 2]}``) on one line.
        explicit_start: begin the document with ``---``.
        default: a function called for an object that cannot otherwise be written. It returns
            a value that can (which may itself hold objects for ``default``), or raises
            ``TypeError``, as in :func:`json.dumps`. It is not called for mapping keys.
        sort_keys: write the entries of every mapping in key order (keys of types that do
            not compare, such as ``1`` and ``"a"``, are a ``TypeError``).

    Raises:
        TypeError: a value of a type that cannot be written, or an option of the wrong type.
        ValueError: a value that contains itself, a string with a lone surrogate, a tag that
            cannot be written, or an option out of range.
        LimitError: the value nests more than 10,000 levels.
        MemoryError: the text does not fit in memory.
        RuntimeError: a dict changed size while it was being written.

    Exceptions raised by the value's own code (an ``items`` or ``__iter__`` of a subclass, or
    ``default``) reach the caller unchanged, as with ``json.dumps``.
    """
    return _write_docs([obj], _options(indent, flow, explicit_start), False, default, sort_keys)


def dumps_all(
    objs: Iterable[Any],
    *,
    indent: int = 2,
    flow: bool = False,
    explicit_start: bool = False,
    default: Callable[[Any], Any] | None = None,
    sort_keys: bool = False,
) -> str:
    """Write each of ``objs`` as a document of one stream, separated by ``---``; the options
    are those of :func:`dumps`."""
    return _write_docs(
        _documents(objs), _options(indent, flow, explicit_start), True, default, sort_keys
    )


def _write(fp: Any, text: str) -> None:
    try:
        write = fp.write
    except AttributeError:
        raise TypeError(f"expected a file object, not {type(fp).__name__}") from None
    # A file opened in binary mode takes UTF-8 bytes.
    write(text.encode("utf-8") if isinstance(fp, (io.RawIOBase, io.BufferedIOBase)) else text)


def dump(
    obj: Any, fp: IO[str] | IO[bytes], *, indent: int = 2, flow: bool = False,
    explicit_start: bool = False, default: Callable[[Any], Any] | None = None,
    sort_keys: bool = False,
) -> None:  # fmt: skip
    """Write ``obj`` as one YAML document to an open file (see :func:`dumps`)."""
    _write(fp, dumps(obj, indent=indent, flow=flow, explicit_start=explicit_start,
                     default=default, sort_keys=sort_keys))  # fmt: skip


def dump_all(
    objs: Iterable[Any], fp: IO[str] | IO[bytes], *, indent: int = 2, flow: bool = False,
    explicit_start: bool = False, default: Callable[[Any], Any] | None = None,
    sort_keys: bool = False,
) -> None:  # fmt: skip
    """Write each of ``objs`` as a document of one stream to an open file."""
    _write(fp, dumps_all(objs, indent=indent, flow=flow, explicit_start=explicit_start,
                         default=default, sort_keys=sort_keys))  # fmt: skip
