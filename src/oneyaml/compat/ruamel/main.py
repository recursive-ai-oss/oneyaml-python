"""The ``YAML`` class of ruamel.yaml 0.19, on oneyaml's lossless documents.

Use it as ruamel's::

    from oneyaml.compat.ruamel import YAML          # was: from ruamel.yaml import YAML

    yaml = YAML()                                    # round trip; typ="safe" for plain dicts
    data = yaml.load(path_or_text_or_file)
    data["version"] = "2.0"
    yaml.dump(data, path_or_file)                    # only the edited values change

How a dump decides what to write (round-trip mode):

* **Data loaded from a stream** (the object ``load`` returned, or ``load_all``'s, in the same
  order and number) is written by editing the stream it was read from. A value that differs is
  set, a new key goes after the last key of its mapping, a removed key or item is deleted, a
  new item is inserted. The document picks the layout of new pieces from the pieces around
  them; the ``indent()`` settings only matter where the document gives no example. Everything
  else, comments and quoting and spacing included, is written as it was read. A tree nobody
  changed is written byte for byte. A tree can be dumped any number of times.
* **Anything else** (data built in the program, a part of a loaded tree, a loaded tree whose
  root was replaced or that is dumped with only some of its documents) is written from scratch
  in ruamel's default layout: block style, two spaces per level, ``indent()`` and the other
  settings honoured, comments set with ``yaml_set_*`` written. Lines are not wrapped.

Where this differs from ruamel.yaml 0.19.1 (intended; the differential test pins the list):

* A round-trip load returns plain ``str``, ``int`` and ``float`` values: there are no
  ``ScalarInt``/``ScalarFloat``/``TimeStamp``/``LiteralScalarString`` wrappers, because the
  document already remembers how each value was written. ``preserve_quotes`` is accepted and
  always on in effect. Style types given to new values choose the style only in data written from
  scratch; a value set in a loaded document gets the style of its neighbours.
* ``<<`` merge keys are applied in ``typ="safe"`` and left as an ordinary key in round-trip
  mode, where the merged keys are not visible through the mapping. ``!!set``, ``!!omap`` and
  ``!!pairs`` become a set, a dict and a list of pairs in ``typ="safe"``; round-trip mode
  reads them as ``oneyaml.Tagged``. Timestamp fractions are cut at microseconds, not rounded.
* Plain scalars that ruamel's resolver half-matches (``1__0``, ``1_``, ``._5``, ``2001-02-30``)
  are text here; ruamel reads some as numbers and fails on others with a ``ValueError``.
* A document whose root is a scalar, or that is empty, has nothing to remember its text by (the
  loaded value is a plain ``str``, ``int`` or ``None``): it is written from scratch.
* An edit the document cannot make (removing an anchor an alias still uses, adding a key to
  an implicit pair in a flow sequence) raises a ``oneyaml.YAMLError`` with a ``category`` and
  writes nothing; ruamel would write the document with the alias expanded or reshaped.
* Comments cannot be added to a loaded tree (``NotImplementedError`` at dump); the document's
  own comments are always kept. ``.lc`` (line and column) is not provided.
* ``typ="unsafe"`` is the safe loader; a ``!!python/...`` tag is an error. An unknown tag loads
  as ``oneyaml.Tagged`` instead of raising. Nothing is ever constructed by running code.
* Limits (depth, alias expansion, sizes) are on; ``max_depth`` can only lower them, and
  ``YAML().limits`` takes a :class:`oneyaml.Limits`. Duplicate keys are an error;
  ``allow_duplicate_keys=True`` is not supported.
* ``width`` is accepted and ignored (no wrapping); ``representer``/``constructor`` hooks,
  ``register_class`` and the scanner/parser/emitter access (``scan``, ``parse``, ``compose``,
  ``emit``, ``serialize``) are not provided and say so when used.
* ``load_all`` reads the whole stream before yielding the first document.
* Strings that ruamel's resolver would read as a number or date (``2024-05-01``, ``1_000``) are
  written by the library's own rules when set into a loaded document.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from typing import Any

from ... import Limits, YAMLError, _api, parse
from ..._limits import _NONE
from . import _apply, _emit, _load
from .comments import _Source
from .error import ComposerError, ConstructorError, convert

__all__ = ["YAML"]

_PYTHON_TAG = re.compile(r"!!python/|tag:yaml\.org,2002:python/")
_TYPES = {"rt", "safe", "base", "unsafe", "full"}


class _Plug:
    """What ``yaml.representer``, ``yaml.constructor`` and ``yaml.resolver`` stand for here:
    attributes can be set, but the hooks that would change how objects are built or written
    say that they are not provided."""

    def __init__(self, name: str) -> None:
        self.__dict__["_name"] = name

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("add_") or attr.startswith("represent") or attr.startswith("construct"):

            def unsupported(*args: Any, **kw: Any) -> None:
                raise NotImplementedError(
                    f"yaml.{self._name}.{attr} is not provided by oneyaml.compat.ruamel"
                )

            return unsupported
        raise AttributeError(attr)


def _read(stream: Any) -> str | bytes:
    """The text or bytes behind what ``load`` was given."""
    if isinstance(stream, (str, bytes)):
        return stream
    if isinstance(stream, (bytearray, memoryview)):
        return bytes(stream)
    if isinstance(stream, os.PathLike):
        with open(stream, "rb") as fp:
            return fp.read()
    read = getattr(stream, "read", None)
    if read is None:
        raise TypeError(f"expected a string, bytes, a path or a file, not {type(stream).__name__}")
    data = read()
    if not isinstance(data, (str, bytes)):
        raise TypeError("the file's read() returned neither text nor bytes")
    return data


def _is_binary(fp: Any) -> bool:
    import io

    if isinstance(fp, (io.RawIOBase, io.BufferedIOBase)):
        return True
    if isinstance(fp, io.TextIOBase):
        return False
    return "b" in getattr(fp, "mode", "")


class YAML:
    """ruamel.yaml's ``YAML`` object: load and dump YAML documents (see the module docstring)."""

    def __init__(
        self,
        *,
        typ: str | list | tuple | None = None,
        pure: bool = False,
        output: Any = None,
        plug_ins: Any = None,
    ) -> None:
        if typ is None:
            typ = "rt"
        elif isinstance(typ, (list, tuple)):
            typ = typ[0] if typ else "rt"
        if not isinstance(typ, str) or typ not in _TYPES:
            raise NotImplementedError(f"typ={typ!r} is not provided; use rt, safe, base or unsafe")
        self.typ = "safe" if typ == "full" else typ
        self.pure = pure
        self._output = output
        self.plug_ins = plug_ins
        # Settings of ruamel's YAML object. Those that matter for data written from scratch are
        # used; the rest are kept so that programs which set them keep working.
        self.preserve_quotes: bool | None = None
        self.width: int | None = 80
        self.explicit_start: bool | None = None
        self.explicit_end: bool | None = None
        self.default_flow_style: bool | None = False if self.typ == "rt" else None
        self.default_style: str | None = None
        self.allow_unicode = True
        self.allow_duplicate_keys = False
        self.canonical: bool | None = None
        self.encoding = "utf-8"
        self.version: Any = None
        self.map_indent: int | None = None
        self.sequence_indent: int | None = None
        self.sequence_dash_offset: int | None = None
        self.top_level_colon_align: Any = None
        self.prefix_colon: Any = None
        self.line_break: str | None = None
        self.sort_base_mapping_type_on_output: bool | None = None
        self.boolean_representation: Any = None
        self.tags: dict = {}
        self.max_depth = 0
        self.limits: Limits | None = None
        self.representer = _Plug("representer")
        self.constructor = _Plug("constructor")
        self.resolver = _Plug("resolver")

    # -- settings

    def indent(
        self, mapping: int | None = None, sequence: int | None = None, offset: int | None = None
    ) -> None:
        """Columns per level for mappings, the column of a sequence's items from its parent's
        key, and where the ``-`` sits (data written from scratch; a loaded document keeps its)."""
        self.map_indent = mapping
        self.sequence_indent = sequence
        self.sequence_dash_offset = offset

    @property
    def block_seq_indent(self) -> int | None:
        return self.sequence_dash_offset

    @block_seq_indent.setter
    def block_seq_indent(self, value: int | None) -> None:
        self.sequence_dash_offset = value

    def register_class(self, cls: Any) -> Any:
        raise NotImplementedError("register_class is not provided by oneyaml.compat.ruamel")

    def _unsupported(self, name: str) -> Any:
        raise NotImplementedError(f"YAML.{name} is not provided by oneyaml.compat.ruamel")

    def scan(self, stream: Any) -> Any:
        return self._unsupported("scan")

    def parse(self, stream: Any) -> Any:
        return self._unsupported("parse")

    def compose(self, stream: Any) -> Any:
        return self._unsupported("compose")

    def compose_all(self, stream: Any) -> Any:
        return self._unsupported("compose_all")

    def emit(self, events: Any, stream: Any) -> Any:
        return self._unsupported("emit")

    def serialize(self, node: Any, stream: Any) -> Any:
        return self._unsupported("serialize")

    # -- loading

    def _limits(self) -> Limits:
        lim = self.limits if self.limits is not None else Limits()
        if self.max_depth and (lim.depth is None or self.max_depth < lim.depth):
            lim = lim.replace(depth=self.max_depth)
        return lim

    def load(self, stream: Any) -> Any:
        """The single document of ``stream`` (text, bytes, a path or an open file); ``None``
        for an empty stream."""
        docs = self._load(stream)
        if len(docs) > 1:
            raise convert(
                ComposerError(
                    "expected a single document in the stream; use load_all",
                    category="syntax",
                )
            )
        return docs[0] if docs else None

    def load_all(self, stream: Any) -> Iterator[Any]:
        """Every document of ``stream``, one at a time."""
        return iter(self._load(stream))

    def _load(self, stream: Any) -> list[Any]:
        raw = _read(stream)
        lim = self._limits()
        try:
            if self.typ == "rt":
                return self._load_rt(raw, lim)
            if self.typ == "base":
                return list(_api._impl.load(raw, lim, True, "text"))
            return self._load_safe(raw, lim)
        except YAMLError as exc:
            if self.allow_duplicate_keys and exc.category == "duplicate-key":
                exc.message += " (allow_duplicate_keys is not supported)"
            raise convert(exc) from None

    def _load_safe(self, raw: Any, lim: Limits) -> list[Any]:
        if self.typ == "unsafe":
            probe = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
            m = _PYTHON_TAG.search(probe)
            if m:
                raise convert(
                    ConstructorError(
                        'typ="unsafe" would build Python objects from the tag at offset '
                        f"{m.start()}; oneyaml never constructs objects from YAML",
                        category="syntax",
                    )
                )
        if _load.might_need_fixups(raw):
            doc = parse(raw, limits=lim)
            h = doc._core.h
            digits = lim.int_digits if lim.int_digits is not None else _NONE
            out = []
            for d in range(h.count()):
                plain = _load.fix_scalars(h.value(d, []), h, d, digits, types=True)
                out.append(_load.merge_keys(plain))
            return out
        return list(_api._impl.load(raw, lim, True))

    def _load_rt(self, raw: Any, lim: Limits) -> list[Any]:
        doc = parse(raw, limits=lim)
        h = doc._core.h
        src = _Source(raw, doc, lim)
        fix = _load.might_need_fixups(raw)
        digits = lim.int_digits if lim.int_digits is not None else _NONE
        out = []
        for d in range(h.count()):
            plain = h.value(d, [])
            if fix:
                plain = _load.fix_scalars(plain, h, d, digits)
            out.append(_load.bind(plain, src, d))
        return out

    # -- dumping

    def dump(self, data: Any, stream: Any = None, *, transform: Any = None) -> None:
        """Write ``data`` as one document to ``stream`` (a path, or a text or binary file)."""
        self.dump_all([data], stream, transform=transform)

    def dump_all(self, documents: Iterable[Any], stream: Any = None, *, transform: Any = None):
        """Write each of ``documents`` as a document of one stream."""
        if stream is None:
            stream = self._output
        if stream is None:
            raise TypeError("dump needs a stream (a path or an open file) to write to")
        docs = list(documents)
        text, raw = self._render(docs)
        if transform is not None:
            text = transform(text)
            raw = None
        self._write(stream, text, raw)

    def _write(self, stream: Any, text: str, raw: bytes | None) -> None:
        if isinstance(stream, os.PathLike):
            with open(stream, "wb") as fp:
                fp.write(raw if raw is not None else text.encode("utf-8"))
            return
        write = getattr(stream, "write", None)
        if write is None:
            raise TypeError(f"expected a path or a file, not {type(stream).__name__}")
        if _is_binary(stream):
            write(raw if raw is not None else text.encode(self.encoding))
        else:
            write(text)

    def _layout(self) -> _emit.Layout:
        lay = _emit.Layout()
        lay.map_indent = self.map_indent or 2
        lay.seq_indent = self.sequence_indent or 2
        lay.offset = self.sequence_dash_offset or 0
        lay.explicit_start = bool(self.explicit_start)
        lay.explicit_end = bool(self.explicit_end)
        lay.flow = self.default_flow_style
        lay.null_empty = self.typ == "rt"
        v = self.version
        if v:
            lay.version = ".".join(map(str, v)) if isinstance(v, (tuple, list)) else str(v)
        return lay

    def _render(self, docs: list[Any]) -> tuple[str, bytes | None]:
        """The text to write (and, for an edited stream, its exact bytes)."""
        if self.typ == "rt" and docs:
            edited = self._edit_loaded(docs)
            if edited is not None:
                return edited
        lay = self._layout()
        if (
            len(docs) == 1
            and isinstance(docs[0], (dict, list))
            and not lay.explicit_end
            and not lay.version
            and _emit.fast_ok(docs[0], lay)
        ):
            from ... import dumps

            return dumps(docs[0], indent=lay.map_indent, explicit_start=lay.explicit_start), None
        parts = [_emit.render_document(v, lay, i == 0) for i, v in enumerate(docs)]
        return "".join(parts), None

    def _edit_loaded(self, docs: list[Any]) -> tuple[str, bytes] | None:
        first = docs[0]
        src = getattr(first, "_src", None)
        if src is None:
            return None
        h = src.doc._core.h
        if h.count() != len(docs):
            return None
        for i, root in enumerate(docs):
            if getattr(root, "_src", None) is not src or getattr(root, "_d", None) != i:
                return None
        if src.dirty:
            # An earlier dump edited the document; start again from what was read, so that this
            # dump is exactly "the input with the differences from the tree".
            src.doc = parse(src.raw, limits=src.limits)
            src.dirty = False
            h = src.doc._core.h
        if src.changed:
            self._apply(src, docs, h)
        return str(src.doc), bytes(src.doc)

    def _apply(self, src: _Source, docs: list[Any], h: Any) -> None:
        if src.comments:
            raise NotImplementedError(
                "comments cannot be added to data loaded from a document yet; "
                "the document's own comments are kept"
            )
        fix = _load.might_need_fixups(src.raw)
        digits = src.limits.int_digits if src.limits.int_digits is not None else _NONE
        try:
            for d, root in enumerate(docs):
                orig = h.value(d, [])
                if fix:
                    orig = _load.fix_scalars(orig, h, d, digits)
                if _apply.edit_document(h, d, root, orig):
                    src.dirty = True
        except BaseException:
            src.dirty = True  # some edits may have been made
            raise
