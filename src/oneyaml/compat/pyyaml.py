"""A drop-in for PyYAML's loading functions: ``import oneyaml.compat.pyyaml as yaml``.

The names, arguments and results are PyYAML's: YAML 1.1 scalars (``yes``, ``0o``-less octal,
base-60 numbers, dates), merge keys, the last of a repeated key, ``!!set``/``!!omap``/``!!binary``,
exceptions with marks. What is not kept:

* Nothing is ever constructed by running code. ``Loader``, ``FullLoader`` and ``UnsafeLoader``
  read the same safe types as ``SafeLoader``; a ``!!python/...`` tag is an error.
* The limits of :class:`oneyaml.Limits` stay on, and merge keys count against the alias budget,
  so a document cannot expand without bound.
* ``safe_load_all`` reads the whole stream before it yields the first document.

Callbacks work as in PyYAML: ``add_constructor``, ``add_multi_constructor``,
``add_implicit_resolver``, ``add_path_resolver``, ``YAMLObject``, and subclasses of the loaders,
registered on the class they are called on and nowhere else (``add_constructor`` without a
``Loader`` reaches ``Loader``, ``FullLoader`` and ``UnsafeLoader``, not ``SafeLoader``). A callback
receives the loader and a node (``ScalarNode``, ``SequenceNode``, ``MappingNode`` with ``tag``,
``value``, ``style`` / ``flow_style``, ``start_mark``, ``end_mark``) and may call
``construct_scalar``, ``construct_sequence``, ``construct_mapping``, ``construct_pairs``,
``construct_object``, ``construct_yaml_*`` and ``flatten_mapping`` on the loader. Exceptions a
callback raises reach the caller as they are.

Where callbacks differ from PyYAML:

* A loader with no registrations, or with constructors for tags the document does not use, reads
  through the active backend. A document that uses a registered tag is parsed by the pure Python
  parser on either backend, and its nodes are built from that.
* Standard collections are complete when a callback gets them, whatever ``deep`` says, and a
  callback that returns a generator is run to its end at once. Callbacks run in document order
  (a nested node before the node that holds it unless the outer callback asks for it first).
* A merge key or ``=`` that is not a key stays the text it spells; PyYAML fails there. A merge
  source that a callback turned into something else is merged from its node, as PyYAML does.
* A mapping key that is a list or a mapping is frozen (tuple, ``FrozenMapping``), not refused.
* Replacing ``construct_object``, ``construct_sequence`` or ``construct_mapping`` in a subclass
  works, but nested calls through them are limited to 100 levels (a ``limit`` error).
* ``add_representer`` and the ``Dumper`` arguments of the registration functions have no effect
  until the dump side exists; ``YAMLObject.to_yaml`` raises ``NotImplementedError``.
* ``!!python/...`` tags are never built, whatever the loader; a constructor registered for one
  by the program is called.
"""

from __future__ import annotations

import re
import sys
import types
from typing import Any

from .. import _api
from .._errors import YAMLError as _BaseError
from .._limits import _NONE
from . import _engine
from ._engine import (
    BaseConstructor,
    BaseResolver,
    Constructor,
    FullConstructor,
    Resolver,
    ResolverError,
    SafeConstructor,
    UnsafeConstructor,
)
from ._nodes import CollectionNode, MappingNode, Node, ScalarNode, SequenceNode

__all__ = [
    "BaseConstructor",
    "BaseLoader",
    "BaseResolver",
    "CBaseLoader",
    "CFullLoader",
    "CLoader",
    "CSafeLoader",
    "CUnsafeLoader",
    "CollectionNode",
    "ComposerError",
    "Constructor",
    "ConstructorError",
    "FullConstructor",
    "FullLoader",
    "Loader",
    "MappingNode",
    "Mark",
    "MarkedYAMLError",
    "Node",
    "ParserError",
    "ReaderError",
    "Resolver",
    "ResolverError",
    "SafeConstructor",
    "SafeLoader",
    "ScalarNode",
    "ScannerError",
    "SequenceNode",
    "UnsafeConstructor",
    "UnsafeLoader",
    "YAMLError",
    "YAMLObject",
    "YAMLObjectMetaclass",
    "__version__",
    "__with_libyaml__",
    "add_constructor",
    "add_implicit_resolver",
    "add_multi_constructor",
    "add_path_resolver",
    "compose",
    "compose_all",
    "full_load",
    "full_load_all",
    "load",
    "load_all",
    "safe_load",
    "safe_load_all",
    "unsafe_load",
    "unsafe_load_all",
]

# The PyYAML release whose documented behaviour this follows.
__version__ = "6.0.3"
# The C-named loaders exist (they are the same loaders), so code that picks them can.
__with_libyaml__ = True


# ---- errors ------------------------------------------------------------------------------------


class Mark:
    """A position in the input; ``line`` and ``column`` count from 0, as in PyYAML."""

    def __init__(
        self,
        name: str,
        index: int,
        line: int,
        column: int,
        buffer: str | None = None,
        pointer: int | None = None,
    ) -> None:
        self.name = name
        self.index = index
        self.line = line
        self.column = column
        self.buffer = buffer
        self.pointer = pointer

    def get_snippet(self, indent: int = 4, max_length: int = 75) -> None:
        return None

    def __str__(self) -> str:
        return f'  in "{self.name}", line {self.line + 1}, column {self.column + 1}'


class YAMLError(_BaseError):
    """Base class of every error this module raises (it is also ``oneyaml.YAMLError``)."""


class MarkedYAMLError(YAMLError):
    def __init__(
        self,
        context: str | None = None,
        context_mark: Mark | None = None,
        problem: str | None = None,
        problem_mark: Mark | None = None,
        note: str | None = None,
    ) -> None:
        self.context = context
        self.context_mark = context_mark
        self.problem = problem
        self.problem_mark = problem_mark
        self.note = note
        super().__init__(problem or context or "")

    def __str__(self) -> str:
        lines = []
        if self.context is not None:
            lines.append(self.context)
        mark = self.context_mark
        if mark is not None and (
            self.problem is None
            or self.problem_mark is None
            or mark.name != self.problem_mark.name
            or mark.line != self.problem_mark.line
            or mark.column != self.problem_mark.column
        ):
            lines.append(str(mark))
        if self.problem is not None:
            lines.append(self.problem)
        if self.problem_mark is not None:
            lines.append(str(self.problem_mark))
        if self.note is not None and self.note:
            lines.append(self.note)
        return "\n".join(lines)

    def __reduce__(self):
        return (
            type(self),
            (self.context, self.context_mark, self.problem, self.problem_mark, self.note),
        )


class ReaderError(YAMLError):
    def __init__(
        self, name: str, position: int, character: Any, encoding: str, reason: str
    ) -> None:
        self.name = name
        self.character = character
        self.position = position
        self.encoding = encoding
        self.reason = reason
        super().__init__(reason)

    def __str__(self) -> str:
        return f'{self.reason}\n  in "{self.name}", position {self.position}'

    def __reduce__(self):
        return (
            type(self),
            (self.name, self.position, self.character, self.encoding, self.reason),
        )


class ScannerError(MarkedYAMLError):
    pass


class ParserError(MarkedYAMLError):
    pass


class ComposerError(MarkedYAMLError):
    pass


class ConstructorError(MarkedYAMLError):
    pass


# Both backends word an unknown or self-containing alias their own way.
_ALIAS_PROBLEM = re.compile(r"(?:the )?alias ").match


def _convert(exc: _BaseError, name: str) -> YAMLError:
    """The PyYAML exception that stands for one of ours, with its position as a mark."""
    category = exc.category
    message = exc.message
    if category == "encoding":
        out: YAMLError = ReaderError(name, exc.offset or 0, "?", "utf-8", message)
    else:
        mark = None
        if exc.line is not None and exc.column is not None:
            mark = Mark(name, exc.offset or 0, exc.line - 1, exc.column - 1)
        if getattr(exc, "construct", False):
            out = ConstructorError(None, None, message, mark)
        elif category == "other" and "more than one document" in message:
            out = ComposerError(
                "expected a single document in the stream", mark, "but found another document", mark
            )
        elif category in ("syntax", "unresolved-tag", "duplicate-key"):
            cls = ComposerError if _ALIAS_PROBLEM(message) else ParserError
            out = cls(None, None, message, mark)
        else:
            # construct, limit, and anything else met while building values
            out = ConstructorError(None, None, message, mark)
    out.category = category
    out.message = message
    out.line = exc.line
    out.column = exc.column
    out.offset = exc.offset
    return out


# ---- loaders -----------------------------------------------------------------------------------


def _read(stream: Any) -> tuple[str | bytes, str]:
    """The text or bytes of a stream and the name to show in marks."""
    if isinstance(stream, str):
        return stream, "<unicode string>"
    if isinstance(stream, (bytes, bytearray, memoryview)):
        return bytes(stream), "<byte string>"
    try:
        read = stream.read
    except AttributeError:
        raise TypeError(
            f"expected a str, bytes or a file object, not {type(stream).__name__}"
        ) from None
    data = read()
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    elif not isinstance(data, (str, bytes)):
        raise TypeError(f"the stream gave {type(data).__name__}, not str or bytes")
    return data, getattr(stream, "name", "<file>")


def _stream_name(stream: Any) -> str:
    if isinstance(stream, str):
        return "<unicode string>"
    if isinstance(stream, (bytes, bytearray, memoryview)):
        return "<byte string>"
    return getattr(stream, "name", "<file>")


class _LoaderCore:
    """Reading a stream and handing out its documents; the loaders add the tables to it.

    A loader whose tables are the standard ones reads straight through the backend. One that
    registered constructors has the backend build the document, and nodes are made only for
    what a callback takes over (the pure backend parses once and composes the whole document when
    it meets such a tag). One that registered resolvers, or replaced a standard constructor,
    always builds nodes.
    """

    _schema = "yaml11"

    def __init__(self, stream: Any) -> None:
        BaseConstructor.__init__(self)
        BaseResolver.__init__(self)
        self.stream = stream
        self.name = _stream_name(stream)
        self._input: str | bytes | None = None
        self._docs: list[Any] | None = None
        self._lazy = False  # _docs holds composed documents, not yet built values
        self._next = 0

    # -- reading --

    def _data(self) -> str | bytes:
        # The stream is read once, whatever is asked of it afterwards.
        if self._input is None:
            self._input = _read(self.stream)[0]
        return self._input

    def _compose(self, multi: bool, parsed: Any = None) -> list[_engine._Doc]:
        try:
            return _engine.compose_documents(
                self, self._data(), self.name, multi, _api._DEFAULT_LIMITS, parsed
            )
        except _BaseError as exc:
            raise _convert(exc, self.name) from None

    def _read_values(self, multi: bool) -> Any:
        """The values by the backend, or the composed documents when callbacks must build them."""
        data = self._data()
        mode = _mode(type(self))
        impl = _api._impl
        parsed = None
        if mode < 2:
            try:
                if mode == 1 and not multi and hasattr(impl, "load_hooked"):
                    # The backend builds the document, except the nodes a callback takes over.
                    hooks = _engine._Hooks(self, data, self.name, _api._DEFAULT_LIMITS)
                    got = impl.load_hooked(data, _api._DEFAULT_LIMITS, hooks)
                    if got is not None:
                        return got[0]
                    return self._read_nodes(multi, None)
                if mode == 1 and hasattr(impl, "parse_stream"):
                    # Parsed once: a node the backend cannot build is composed from the same events.
                    parsed = impl.parse_stream(data, _api._DEFAULT_LIMITS, self._schema, multi)
                    return impl.build_parsed(parsed, multi, self._schema)
                return impl.load(data, _api._DEFAULT_LIMITS, multi, self._schema)
            except _engine.Passthrough as carried:
                raise carried.error from None
            except _BaseError as exc:
                # An error a callback raised is PyYAML's already and stands as it is.
                if isinstance(exc, YAMLError):
                    raise
                # A value the backend could not build (a tag it does not know, a tag that does
                # not fit) may be one a callback takes over; every other error stands.
                if mode == 0 or not getattr(exc, "construct", False):
                    raise _convert(exc, self.name) from None
        return self._read_nodes(multi, parsed)

    def _read_nodes(self, multi: bool, parsed: Any) -> list[_engine._Doc]:
        self._lazy = True
        return self._compose(multi, parsed)

    # -- the loader interface --

    def _build(self, doc: _engine._Doc) -> Any:
        self._doc = doc
        self.anchors = doc.anchors
        self.constructed_objects = {}
        self.recursive_objects = {}
        self.state_generators = []
        digits = _api._DEFAULT_LIMITS._native[8]
        self._cap = None if digits >= _NONE else digits
        self._found = {}
        self._slow = _replaced(type(self))
        try:
            return self.construct_document(doc.root)
        except RecursionError as exc:
            # Only a callback that calls itself through the nesting of the input gets here: the
            # library's own walk keeps an explicit stack.
            raise self._error(
                "the document nests too deeply for the constructors that were registered",
                doc.root,
                "limit",
            ) from exc

    def _documents(self) -> list[Any]:
        if self._docs is None:
            self._docs = self._read_values(True)
        return self._docs

    def check_data(self) -> bool:
        return self._next < len(self._documents())

    def get_data(self) -> Any:
        docs = self._documents()
        if self._next >= len(docs):
            return None
        self._next += 1
        item = docs[self._next - 1]
        return self._build(item) if self._lazy else item

    def get_single_data(self) -> Any:
        # A stream of several documents is refused by the load itself, at the second one.
        result = self._read_values(False)
        if not self._lazy:
            return result
        return self._build(result[0]) if result else None

    def check_node(self) -> bool:
        return self._next < len(self._node_documents())

    def get_node(self) -> Any:
        docs = self._node_documents()
        if self._next >= len(docs):
            return None
        self._next += 1
        return docs[self._next - 1].root

    def get_single_node(self) -> Any:
        docs = self._compose(False)
        return docs[0].root if docs else None

    def _node_documents(self) -> list[_engine._Doc]:
        if self._docs is None or not self._lazy:
            self._lazy = True
            self._docs = self._compose(True)
        return self._docs

    def dispose(self) -> None:
        self._docs = None
        self._input = None


class BaseLoader(_LoaderCore, BaseConstructor, BaseResolver):
    """Reads every scalar as a string and ignores tags, as PyYAML's ``BaseLoader`` does."""

    _schema = "text"


class SafeLoader(_LoaderCore, SafeConstructor, Resolver):
    """YAML 1.1 scalars and the standard tags, as PyYAML's ``SafeLoader``."""


class FullLoader(_LoaderCore, FullConstructor, Resolver):
    """Reads the same types as ``SafeLoader``; ``!!python/...`` tags are refused."""


class UnsafeLoader(_LoaderCore, UnsafeConstructor, Resolver):
    """Kept for its name. It builds nothing by running code: ``!!python/...`` tags are refused."""


class Loader(_LoaderCore, Constructor, Resolver):
    """Kept for its name; the same as :class:`UnsafeLoader`."""


# PyYAML's C loaders are separate classes with their own tables, so they are here.
class CBaseLoader(_LoaderCore, BaseConstructor, BaseResolver):
    _schema = "text"


class CSafeLoader(_LoaderCore, SafeConstructor, Resolver):
    pass


class CFullLoader(_LoaderCore, FullConstructor, Resolver):
    pass


class CUnsafeLoader(_LoaderCore, UnsafeConstructor, Resolver):
    pass


class CLoader(_LoaderCore, Constructor, Resolver):
    pass


# The tables as they are now; a loader whose tables are still these needs no callbacks.
def _copy_resolvers(table: dict) -> dict:
    return {k: v[:] for k, v in table.items()}


_STANDARD = {
    "yaml11": (
        dict(SafeLoader.yaml_constructors),
        _copy_resolvers(Resolver.yaml_implicit_resolvers),
    ),
    "text": ({}, {}),
}


def _replaced(cls: type) -> bool:
    """True when a subclass replaced a method that builds collections."""
    return any(getattr(cls, name) is not _engine._ORIGINAL[name] for name in _engine._REPLACEABLE)


def _mode(cls: type) -> int:
    """0: the backend reads alone. 1: the backend first, nodes if the document needs a callback.
    2: nodes."""
    constructors, resolvers = _STANDARD[cls._schema]
    if (
        cls.yaml_implicit_resolvers != resolvers
        or cls.yaml_path_resolvers
        or _replaced(cls)
        or cls.construct_scalar is not _engine._ORIGINAL["construct_scalar"]
        or cls.resolve is not BaseResolver.resolve
    ):
        return 2
    own = cls.yaml_constructors
    if own == constructors and not cls.yaml_multi_constructors:
        return 0
    if cls._schema == "text":
        return 2
    # A standard tag with another builder is for the nodes; a new tag is for the backend to
    # report as unknown.
    for tag, fn in constructors.items():
        if tag is not None and own.get(tag) is not fn:
            return 2
    return 1


# ---- functions ---------------------------------------------------------------------------------


def load(stream: Any, Loader: Any) -> Any:  # noqa: N803 (PyYAML's own parameter name)
    """Read the single document in ``stream`` with ``Loader``."""
    loader = Loader(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_all(stream: Any, Loader: Any):  # noqa: N803
    """Read every document in ``stream``; the whole stream is read before the first is given."""
    loader = Loader(stream)
    try:
        while loader.check_data():
            yield loader.get_data()
    finally:
        loader.dispose()


def full_load(stream: Any) -> Any:
    return load(stream, FullLoader)


def full_load_all(stream: Any):
    return load_all(stream, FullLoader)


def safe_load(stream: Any) -> Any:
    return load(stream, SafeLoader)


def safe_load_all(stream: Any):
    return load_all(stream, SafeLoader)


def unsafe_load(stream: Any) -> Any:
    return load(stream, UnsafeLoader)


def unsafe_load_all(stream: Any):
    return load_all(stream, UnsafeLoader)


# ---- registering callbacks ---------------------------------------------------------------------

# What ``add_constructor`` and the others reach when no loader is named: PyYAML's three loaders
# that are not the safe one.
_DEFAULT_LOADERS = (Loader, FullLoader, UnsafeLoader)


def add_constructor(tag: Any, constructor: Any, Loader: Any = None) -> None:  # noqa: N803
    """Read nodes tagged ``tag`` with ``constructor(loader, node)``.

    Without ``Loader`` this reaches ``Loader``, ``FullLoader`` and ``UnsafeLoader``, as in
    PyYAML, and not ``SafeLoader``."""
    for cls in _DEFAULT_LOADERS if Loader is None else (Loader,):
        cls.add_constructor(tag, constructor)


def add_multi_constructor(  # noqa: N803
    tag_prefix: Any, multi_constructor: Any, Loader: Any = None
) -> None:
    """Read nodes whose tag starts with ``tag_prefix`` with
    ``multi_constructor(loader, tag_suffix, node)``."""
    for cls in _DEFAULT_LOADERS if Loader is None else (Loader,):
        cls.add_multi_constructor(tag_prefix, multi_constructor)


def add_implicit_resolver(  # noqa: N803
    tag: str, regexp: Any, first: Any = None, Loader: Any = None, Dumper: Any = None
) -> None:
    """Give plain scalars that match ``regexp`` (and begin with a character of ``first``) the
    tag ``tag``. ``Dumper`` is accepted for PyYAML's signature and has no effect yet."""
    for cls in _DEFAULT_LOADERS if Loader is None else (Loader,):
        cls.add_implicit_resolver(tag, regexp, first)


def add_path_resolver(  # noqa: N803
    tag: str, path: Any, kind: Any = None, Loader: Any = None, Dumper: Any = None
) -> None:
    """Give the node at ``path`` the tag ``tag``. ``Dumper`` has no effect yet."""
    for cls in _DEFAULT_LOADERS if Loader is None else (Loader,):
        cls.add_path_resolver(tag, path, kind)


def compose(stream: Any, Loader: Any = Loader) -> Any:  # noqa: N803
    """The node tree of the single document in ``stream``."""
    loader = Loader(stream)
    try:
        return loader.get_single_node()
    finally:
        loader.dispose()


def compose_all(stream: Any, Loader: Any = Loader):  # noqa: N803
    """The node tree of every document in ``stream``."""
    loader = Loader(stream)
    try:
        while loader.check_node():
            yield loader.get_node()
    finally:
        loader.dispose()


class YAMLObjectMetaclass(type):
    """Registers a ``YAMLObject`` class with the loaders it names when it is defined."""

    def __init__(cls, name: str, bases: tuple, kwds: dict) -> None:
        super().__init__(name, bases, kwds)
        if kwds.get("yaml_tag") is not None:
            loaders = cls.yaml_loader if isinstance(cls.yaml_loader, list) else [cls.yaml_loader]
            for loader in loaders:
                loader.add_constructor(cls.yaml_tag, cls.from_yaml)
            # Writing objects waits for the dump side; a dumper that exists takes the class.
            dumper = cls.yaml_dumper
            if dumper is not None and hasattr(dumper, "add_representer"):
                dumper.add_representer(cls, cls.to_yaml)


class YAMLObject(metaclass=YAMLObjectMetaclass):
    """A class that reads itself from the tag ``yaml_tag``: subclass it and set the tag."""

    yaml_loader: Any = [Loader, FullLoader, UnsafeLoader]  # noqa: RUF012 (PyYAML's own shape)
    yaml_dumper: Any = None
    yaml_tag: str | None = None
    yaml_flow_style: bool | None = None

    @classmethod
    def from_yaml(cls, loader: Any, node: Node) -> Any:
        """Make an instance from ``node``."""
        return loader.construct_yaml_object(node, cls)

    @classmethod
    def to_yaml(cls, dumper: Any, data: Any) -> Any:
        raise NotImplementedError("writing objects is not available in this module yet")


# PyYAML spreads these over submodules; code reaches them as ``yaml.nodes.ScalarNode`` and the
# like, so the same names are here.
def _namespace(name: str, **members: Any) -> types.ModuleType:
    module = types.ModuleType(f"{__name__}.{name}")
    module.__dict__.update(members)
    sys.modules[module.__name__] = module
    return module


nodes = _namespace(
    "nodes",
    Node=Node,
    ScalarNode=ScalarNode,
    CollectionNode=CollectionNode,
    SequenceNode=SequenceNode,
    MappingNode=MappingNode,
)
constructor = _namespace(
    "constructor",
    BaseConstructor=BaseConstructor,
    SafeConstructor=SafeConstructor,
    FullConstructor=FullConstructor,
    UnsafeConstructor=UnsafeConstructor,
    Constructor=Constructor,
    ConstructorError=ConstructorError,
)
resolver = _namespace(
    "resolver", BaseResolver=BaseResolver, Resolver=Resolver, ResolverError=ResolverError
)
error = _namespace("error", Mark=Mark, YAMLError=YAMLError, MarkedYAMLError=MarkedYAMLError)
loader = _namespace(
    "loader",
    BaseLoader=BaseLoader,
    SafeLoader=SafeLoader,
    FullLoader=FullLoader,
    UnsafeLoader=UnsafeLoader,
    Loader=Loader,
)
