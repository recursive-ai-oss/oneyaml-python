"""The machinery behind the PyYAML loaders: resolvers, constructors and the node composer.

``pyyaml.py`` keeps the public surface; this module holds what a registered callback meets.

* A **resolver** decides the tag of a plain scalar from regular expressions, the way PyYAML's
  does, and from node paths. Its tables are class attributes that ``add_implicit_resolver``
  copies on first change, so registering on one loader class never reaches another.
* A **constructor** keeps the same kind of tables, for constructors and prefix constructors.
* The **composer** turns the parser's events into nodes. It reads the whole document with
  explicit stacks, charges every alias to the same budgets a plain load uses, and resolves tags.
* Building values walks the nodes with an explicit stack too. A callback runs when its node is
  reached and may ask the loader for the value of any node below it.

A loader that registers nothing never comes here (``pyyaml.py`` hands its text straight to the
backend). One that only registers constructors tries the backend first and comes here when the
document uses a tag the backend cannot build.
"""

from __future__ import annotations

import re
import sys
import types
from typing import Any

from .._errors import YAMLError as _BaseError
from .._errors import make_error
from .._limits import _NONE
from .._pure._construct import _OPEN, _check_sizes, _map_last_wins, _map_merging, _utf8_len
from .._pure._decode import decode
from .._pure._parser import (
    ALIAS,
    DOC_END,
    DOC_START,
    MAP_END,
    MAP_START,
    SCALAR,
    SEQ_END,
    SEQ_START,
    linecol,
    parse_events,
)
from .._pure._resolve import Fail
from .._pure._resolve11 import MERGE
from .._yaml11 import Reject, construct
from ._nodes import MappingNode, Node, ScalarNode, SequenceNode

_YAML = "tag:yaml.org,2002:"
TAG_STR = _YAML + "str"
TAG_SEQ = _YAML + "seq"
TAG_MAP = _YAML + "map"
TAG_MERGE = _YAML + "merge"
TAG_VALUE = _YAML + "value"

# How many times a callback may ask for a value inside a callback inside a callback. The standard
# types are built without nesting, so only chains of callbacks (and loaders that override the
# collection constructors) count.
MAX_NESTING = 100


class ResolverError(ValueError):
    """A resolver was registered with a path or kind that cannot be matched."""


# ---- resolver ----------------------------------------------------------------------------------


class BaseResolver:
    DEFAULT_SCALAR_TAG = TAG_STR
    DEFAULT_SEQUENCE_TAG = TAG_SEQ
    DEFAULT_MAPPING_TAG = TAG_MAP

    yaml_implicit_resolvers: dict[Any, list] = {}
    yaml_path_resolvers: dict[Any, str] = {}

    def __init__(self) -> None:
        self.resolver_exact_paths: list[dict] = []
        self.resolver_prefix_paths: list[list] = []

    @classmethod
    def add_implicit_resolver(cls, tag, regexp, first):
        if "yaml_implicit_resolvers" not in cls.__dict__:
            cls.yaml_implicit_resolvers = {k: v[:] for k, v in cls.yaml_implicit_resolvers.items()}
        if first is None:
            first = [None]
        for ch in first:
            cls.yaml_implicit_resolvers.setdefault(ch, []).append((tag, regexp))

    @classmethod
    def add_path_resolver(cls, tag, path, kind=None):
        """Give the nodes at ``path`` (a list of keys and indexes) the tag ``tag``."""
        if "yaml_path_resolvers" not in cls.__dict__:
            cls.yaml_path_resolvers = cls.yaml_path_resolvers.copy()
        new_path = []
        for element in path:
            if isinstance(element, (list, tuple)):
                if len(element) == 2:
                    node_check, index_check = element
                elif len(element) == 1:
                    node_check = element[0]
                    index_check = True
                else:
                    raise ResolverError(f"Invalid path element: {element}")
            else:
                node_check = None
                index_check = element
            if node_check is str:
                node_check = ScalarNode
            elif node_check is list:
                node_check = SequenceNode
            elif node_check is dict:
                node_check = MappingNode
            elif (
                node_check not in (ScalarNode, SequenceNode, MappingNode)
                and not isinstance(node_check, str)
                and node_check is not None
            ):
                raise ResolverError(f"Invalid node checker: {node_check}")
            if not isinstance(index_check, (str, int)) and index_check is not None:
                raise ResolverError(f"Invalid index checker: {index_check}")
            new_path.append((node_check, index_check))
        if kind is str:
            kind = ScalarNode
        elif kind is list:
            kind = SequenceNode
        elif kind is dict:
            kind = MappingNode
        elif kind not in (ScalarNode, SequenceNode, MappingNode) and kind is not None:
            raise ResolverError(f"Invalid node kind: {kind}")
        cls.yaml_path_resolvers[tuple(new_path), kind] = tag

    def descend_resolver(self, current_node, current_index):
        if not self.yaml_path_resolvers:
            return
        exact_paths: dict = {}
        prefix_paths: list = []
        if current_node:
            depth = len(self.resolver_prefix_paths)
            for path, kind in self.resolver_prefix_paths[-1]:
                if self.check_resolver_prefix(depth, path, kind, current_node, current_index):
                    if len(path) > depth:
                        prefix_paths.append((path, kind))
                    else:
                        exact_paths[kind] = self.yaml_path_resolvers[path, kind]
        else:
            for path, kind in self.yaml_path_resolvers:
                if not path:
                    exact_paths[kind] = self.yaml_path_resolvers[path, kind]
                else:
                    prefix_paths.append((path, kind))
        self.resolver_exact_paths.append(exact_paths)
        self.resolver_prefix_paths.append(prefix_paths)

    def ascend_resolver(self):
        if not self.yaml_path_resolvers:
            return
        self.resolver_exact_paths.pop()
        self.resolver_prefix_paths.pop()

    def check_resolver_prefix(self, depth, path, kind, current_node, current_index):
        node_check, index_check = path[depth - 1]
        if isinstance(node_check, str):
            if current_node.tag != node_check:
                return None
        elif node_check is not None and not isinstance(current_node, node_check):
            return None
        if index_check is True and current_index is not None:
            return None
        if (index_check is False or index_check is None) and current_index is None:
            return None
        if isinstance(index_check, str):
            if not (isinstance(current_index, ScalarNode) and index_check == current_index.value):
                return None
        elif (
            isinstance(index_check, int)
            and not isinstance(index_check, bool)
            and index_check != current_index
        ):
            return None
        return True

    def resolve(self, kind, value, implicit):
        if kind is ScalarNode and implicit[0]:
            resolvers = self.yaml_implicit_resolvers.get(value[0] if value else "", [])
            wildcard = self.yaml_implicit_resolvers.get(None, [])
            for tag, regexp in resolvers + wildcard:
                if regexp.match(value):
                    return tag
        if self.yaml_path_resolvers:
            exact_paths = self.resolver_exact_paths[-1]
            if kind in exact_paths:
                return exact_paths[kind]
            if None in exact_paths:
                return exact_paths[None]
        if kind is ScalarNode:
            return self.DEFAULT_SCALAR_TAG
        if kind is SequenceNode:
            return self.DEFAULT_SEQUENCE_TAG
        return self.DEFAULT_MAPPING_TAG


class Resolver(BaseResolver):
    """The resolver of the YAML 1.1 loaders: PyYAML's implicit resolvers."""


def _add_standard_resolvers() -> None:
    table = [
        (
            "bool",
            r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$",
            "yYnNtTfFoO",
        ),
        (
            "float",
            r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?"
            r"|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
            r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*"
            r"|[-+]?\.(?:inf|Inf|INF)"
            r"|\.(?:nan|NaN|NAN))$",
            "-+0123456789.",
        ),
        (
            "int",
            r"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+"
            r"|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$",
            "-+0123456789",
        ),
        ("merge", r"^(?:<<)$", "<"),
        ("null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
        (
            "timestamp",
            r"^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
            r"|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?"
            r"(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?"
            r"(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$",
            "0123456789",
        ),
        ("value", r"^(?:=)$", "="),
    ]
    for name, pattern, first in table:
        Resolver.add_implicit_resolver(_YAML + name, re.compile(pattern), list(first))


_add_standard_resolvers()


# ---- source text and marks ---------------------------------------------------------------------


_BLOCK_GAP = re.compile(r"(?:[ \t\r\n\ufeff]|#[^\r\n]*)*").match
_FLOW_GAP = re.compile(r"(?:[ \t\r\n,]|#[^\r\n]*)*").match


class _Source:
    """The text of a stream and its name, to turn offsets into marks when someone asks."""

    __slots__ = ("name", "text")

    def __init__(self, text: str, name: str) -> None:
        self.text = text
        self.name = name

    def collection_end(self, offset: int, flow: bool) -> int:
        """Where a collection that ends at ``offset`` is over for PyYAML: after the closing
        bracket of a flow collection, at the next token (or the end) for a block one."""
        if flow:
            return _FLOW_GAP(self.text, offset).end() + 1
        return _BLOCK_GAP(self.text, offset).end()

    def mark(self, offset: int) -> Any:
        from . import pyyaml

        line, column = linecol(self.text, offset)
        return pyyaml.Mark(self.name, offset, line - 1, column - 1, self.text, offset)


class _Doc:
    """One composed document and what building its values needs."""

    __slots__ = ("anchors", "events", "info", "keys", "root", "source", "wide")

    def __init__(self, source: _Source, events: list[tuple]) -> None:
        self.source = source
        self.events = events
        self.root: Node | None = None
        self.anchors: dict[str, Node] = {}
        # Collections measured so far (the key-depth check reads it) and floats too wide for
        # a double (a key may not be one), and keys shared across the document.
        self.info: dict[int, Any] = {}
        self.wide: dict[int, float] = {}
        self.keys: dict[str, str] = {}


# ---- composer ----------------------------------------------------------------------------------


def compose_documents(
    loader: BaseResolver, data: str | bytes, name: str, multi: bool, limits: Any
) -> list[_Doc]:
    """Parse ``data`` and compose its documents into nodes.

    Raises the library's own errors (``YAMLError``) for bad input and for a document that goes
    past a limit; the caller turns them into PyYAML's."""
    depth, nf, nfloor, bf, bfloor, input_size, scalar_size, documents, _digits = limits._native
    binary = isinstance(data, (bytes, bytearray, memoryview))
    if input_size < _NONE and binary and len(data) > input_size:
        raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
    text = decode(data)
    if input_size < _NONE and not binary and _utf8_len(text) > input_size:
        raise make_error(f"the input is over the limit of {input_size} bytes", "limit")
    events = parse_events(text, sys.maxsize if depth >= _NONE else depth)
    if scalar_size < _NONE or documents < _NONE:
        try:
            _check_sizes(events, scalar_size, documents)
        except Fail as exc:
            raise _error_at(text, exc) from None
    source = _Source(text, name)
    try:
        return _compose(
            loader,
            events,
            source,
            multi,
            None if depth >= _NONE else depth,
            (nf, nfloor, bf, bfloor),
        )
    except Fail as exc:
        raise _error_at(text, exc) from None


def _error_at(text: str, exc: Fail) -> _BaseError:
    off = exc.offset
    line, col = linecol(text, 0 if off is None else off)
    return make_error(exc.message, exc.category, line, col, off)


def _compose(  # noqa: C901
    loader: BaseResolver,
    events: list[tuple],
    source: _Source,
    multi: bool,
    depth: int | None,
    budget: tuple[int, int, int, int],
) -> list[_Doc]:
    nf, nfloor, bf, bfloor = budget
    text = source.text
    ascii_text = text.isascii()
    paths = bool(loader.yaml_path_resolvers)
    merges = loader._merges
    custom = type(loader).resolve is not BaseResolver.resolve
    table = loader.yaml_implicit_resolvers
    wild = table.get(None, [])
    resolve = loader.resolve
    docs: list[_Doc] = []
    doc: _Doc | None = None
    anchors: dict[str, Any] = {}
    # A frame per open collection: the node, its items so far, whether it is a mapping, the
    # nodes / bytes / height of what it holds, its anchor and the key waiting for its value.
    stack: list[list] = []
    root: Node | None = None
    doc_i = doc_start = closed = 0
    charged_nodes = charged_bytes = 0

    for i, e in enumerate(events):
        k = e[0]
        if k == SCALAR:
            tag = e[5]
            v = e[7]
            style = e[6]
            if paths:
                loader.descend_resolver(*_parent_of(stack))
            implicit = False
            if tag is None or tag == "!":
                if tag is None and style == ":":
                    implicit = True
                    if paths or custom:
                        tag = resolve(ScalarNode, v, (True, False))
                    else:
                        tag = TAG_STR
                        found = (table.get(v[:1], []) if v else table.get("", [])) + wild
                        for t, rx in found:
                            if rx.match(v):
                                tag = t
                                break
                elif paths or custom:
                    tag = resolve(ScalarNode, v, (False, True))
                else:
                    tag = TAG_STR
            node = ScalarNode(tag, v, None, None, None if style == ":" else style)
            node._source = source
            node._event = e
            node._end = e[3]
            node._implicit = implicit
            if paths:
                loader.ascend_resolver()
            a = e[4]
            if a is not None:
                anchors[a] = node
            size = len(v) if v.isascii() else len(v.encode("utf-8", "surrogatepass"))
            measure = (1, size, 0)
        elif k == MAP_START or k == SEQ_START:  # noqa: SIM109
            is_map = k == MAP_START
            if paths:
                loader.descend_resolver(*_parent_of(stack))
            tag = e[4]
            kind = MappingNode if is_map else SequenceNode
            if tag is None or tag == "!":
                tag = resolve(kind, None, (True, False))
            node = kind(tag, [], None, None, e[5])
            node._source = source
            node._event = e
            node._end = e[1]
            node._implicit = False
            a = e[3]
            if a is not None:
                anchors[a] = _OPEN
            stack.append([node, node.value, is_map, 0, 0, 0, a, None])
            continue
        elif k == MAP_END or k == SEQ_END:  # noqa: SIM109
            closed += 1
            node, items, _m, n, b, h, a, pending = stack.pop()
            node._end = e[1]
            measure = (1 + n, b, h + 1)
            if paths:
                loader.ascend_resolver()
            if a is not None and anchors[a] is _OPEN:
                anchors[a] = node
        elif k == ALIAS:
            node = anchors.get(e[3])
            if node is _OPEN:
                raise Fail(f"alias *{e[3]} refers to a node that contains it", "syntax", e[1])
            if node is not None and node._merge_key:
                raise Fail("an alias to a merge key cannot be constructed", "construct", e[1])
            if node is None:
                raise Fail(f"alias *{e[3]} has no anchor", "syntax", e[1])
            nodes, size, height = node._measure
            if depth is not None and len(stack) + height > depth:
                raise Fail(f"alias nests deeper than the limit of {depth}", "limit", e[1])
            charged_nodes += nodes
            charged_bytes += size
            if charged_nodes > max(nf * (i - doc_i - closed), nfloor):
                raise Fail("alias expansion exceeds the node limit", "limit", e[1])
            if charged_bytes > max(bf * (e[1] - doc_start), bfloor) and (
                ascii_text or charged_bytes > max(bf * _utf8_len(text[doc_start : e[1]]), bfloor)
            ):
                raise Fail("alias expansion exceeds the byte limit", "limit", e[1])
            measure = (nodes, size, height)
        elif k == DOC_START:
            if docs and not multi:
                raise Fail("the stream holds more than one document; use loads_all", "other", e[1])
            anchors = {}
            doc = _Doc(source, events)
            doc.anchors = anchors
            root = None
            doc_i, doc_start, closed = i, e[1], 0
            charged_nodes = charged_bytes = 0
            if paths:
                loader.resolver_exact_paths = []
                loader.resolver_prefix_paths = []
            continue
        elif k == DOC_END:
            doc.root = root
            docs.append(doc)
            continue
        else:
            continue

        # A node is complete: record its size and hand it to the collection it belongs to.
        if k != ALIAS:
            node._measure = measure
            if (
                k == SCALAR
                and node.tag == TAG_MERGE
                and merges
                and stack
                and stack[-1][2]
                and stack[-1][7] is None
            ):
                # The key that merges; an alias to it would have no value to stand for.
                node._merge_key = True
        if stack:
            frame = stack[-1]
            if frame[2]:
                if frame[7] is None:
                    frame[7] = node
                else:
                    frame[1].append((frame[7], node))
                    frame[7] = None
            else:
                frame[1].append(node)
            frame[3] += measure[0]
            frame[4] += measure[1]
            if measure[2] > frame[5]:
                frame[5] = measure[2]
        else:
            root = node
    return docs


def _parent_of(stack: list[list]) -> tuple[Any, Any]:
    """The collection a new node is in and its place there, for node-path resolvers."""
    if not stack:
        return None, None
    node, items, is_map, _n, _b, _h, _a, pending = stack[-1]
    if is_map:
        return node, pending  # a key has no place; a value is found by its key
    return node, len(items)


# ---- constructor -------------------------------------------------------------------------------

_NO_DIGITS = re.compile(r"[-+]?0[bx]_*\Z").match


class BaseConstructor:
    """Builds values from nodes. Registered callbacks are ``f(loader, node)`` for a tag and
    ``f(loader, suffix, node)`` for a tag prefix."""

    yaml_constructors: dict[Any, Any] = {}
    yaml_multi_constructors: dict[Any, Any] = {}
    # Merge keys belong to the YAML 1.1 loaders.
    _merges = False

    def __init__(self) -> None:
        self.constructed_objects: dict[Node, Any] = {}
        self.recursive_objects: dict[Node, None] = {}
        self.state_generators: list = []
        self.deep_construct = False
        self.anchors: dict[str, Node] = {}
        self._doc: _Doc | None = None
        self._nesting = 0
        self._cap: int | None = None
        self._slow = False

    @classmethod
    def add_constructor(cls, tag, constructor):
        if "yaml_constructors" not in cls.__dict__:
            cls.yaml_constructors = cls.yaml_constructors.copy()
        cls.yaml_constructors[tag] = constructor

    @classmethod
    def add_multi_constructor(cls, tag_prefix, multi_constructor):
        if "yaml_multi_constructors" not in cls.__dict__:
            cls.yaml_multi_constructors = cls.yaml_multi_constructors.copy()
        cls.yaml_multi_constructors[tag_prefix] = multi_constructor

    # -- errors --

    def _error(self, message: str, node: Node | None, category: str = "construct") -> Any:
        """The PyYAML construction error for ``message`` at ``node``."""
        from . import pyyaml

        mark = node.start_mark if node is not None else None
        exc = pyyaml.ConstructorError(None, None, message, mark)
        exc.category = category
        exc.message = message
        exc.line = None if mark is None else mark.line + 1
        exc.column = None if mark is None else mark.column + 1
        exc.offset = None if mark is None else mark.index
        return exc

    def _fail(self, fail: Fail, node: Node | None) -> Any:
        exc = self._error(fail.message, node, fail.category)
        if fail.offset is not None and node is not None and node._source is not None:
            from . import pyyaml

            line, column = linecol(node._source.text, fail.offset)
            mark = pyyaml.Mark(node._source.name, fail.offset, line - 1, column - 1)
            exc.problem_mark = mark
            exc.line, exc.column, exc.offset = line, column, fail.offset
        if fail.category in ("syntax", "duplicate-key", "unresolved-tag"):
            exc.category = fail.category
        return exc

    # -- the public construct_* methods --

    def construct_document(self, node):
        data = None if node is None else self.construct_object(node)
        while self.state_generators:
            generators, self.state_generators = self.state_generators, []
            for generator in generators:
                for _ in generator:
                    pass
        self.constructed_objects = {}
        self.recursive_objects = {}
        self.deep_construct = False
        return data

    def construct_object(self, node, deep=False):
        done = self.constructed_objects
        if node in done:
            return done[node]
        return self._construct(node)

    def construct_scalar(self, node):
        if not isinstance(node, ScalarNode):
            raise self._error(f"expected a scalar node, but found {node.id}", node)
        return node.value

    def construct_sequence(self, node, deep=False):
        if not isinstance(node, SequenceNode):
            raise self._error(f"expected a sequence node, but found {node.id}", node)
        return [self.construct_object(child, deep=deep) for child in node.value]

    def construct_mapping(self, node, deep=False):
        if not isinstance(node, MappingNode):
            raise self._error(f"expected a mapping node, but found {node.id}", node)
        buf: list = []
        merging = False
        for key_node, value_node in node.value:
            if self._is_merge_key(key_node):
                merging = True
                buf.append(MERGE)
            else:
                buf.append(self.construct_object(key_node, deep=deep))
            buf.append(self.construct_object(value_node, deep=deep))
        return self._assemble_map(node, buf, merging)

    def construct_pairs(self, node, deep=False):
        if not isinstance(node, MappingNode):
            raise self._error(f"expected a mapping node, but found {node.id}", node)
        return [
            (self.construct_object(k, deep=deep), self.construct_object(v, deep=deep))
            for k, v in node.value
        ]

    def construct_undefined(self, node):
        try:
            construct(node.tag, "")
        except Reject as exc:
            raise self._error(exc.message, node) from None
        raise self._error(f"could not determine a constructor for the tag {node.tag!r}", node)

    def construct_yaml_object(self, node, cls):
        data = cls.__new__(cls)
        yield data
        if hasattr(data, "__setstate__"):
            state = self.construct_mapping(node, deep=True)
            data.__setstate__(state)
        else:
            state = self.construct_mapping(node)
            data.__dict__.update(state)

    def flatten_mapping(self, node):
        """Replace the merge keys of a mapping node by the entries they bring (PyYAML's own
        in-place rewrite of the node)."""
        if not isinstance(node, MappingNode):
            raise self._error(f"expected a mapping node, but found {node.id}", node)
        work = 0
        merge: list = []
        index = 0
        while index < len(node.value):
            key_node, value_node = node.value[index]
            if key_node.tag == TAG_MERGE:
                del node.value[index]
                if isinstance(value_node, MappingNode):
                    self.flatten_mapping(value_node)
                    merge.extend(value_node.value)
                elif isinstance(value_node, SequenceNode):
                    submerge = []
                    for subnode in value_node.value:
                        if not isinstance(subnode, MappingNode):
                            raise self._error(
                                f"expected a mapping for merging, but found {subnode.id}", subnode
                            )
                        self.flatten_mapping(subnode)
                        submerge.append(subnode.value)
                    submerge.reverse()
                    for value in submerge:
                        merge.extend(value)
                else:
                    raise self._error(
                        "expected a mapping or list of mappings for merging, "
                        f"but found {value_node.id}",
                        value_node,
                    )
                work += len(merge)
                if work > 1_000_000:
                    raise self._error("the merged mapping is too large", node, "limit")
            elif key_node.tag == TAG_VALUE:
                key_node.tag = TAG_STR
                index += 1
            else:
                index += 1
        if merge:
            node.value = merge + node.value

    def _is_merge_key(self, key_node: Node) -> bool:
        return self._merges and key_node.tag == TAG_MERGE and isinstance(key_node, ScalarNode)

    # -- building --

    def _context(self) -> _Doc:
        doc = self._doc
        if doc is None:
            doc = self._doc = _Doc(_Source("", getattr(self, "name", "<unknown>")), [])
        return doc

    def _plain_map(self, node: Node) -> dict:
        """The entries of a mapping node as a dict, whatever its tag makes of it."""
        buf: list = []
        merging = False
        for key_node, value_node in node.value:
            if self._is_merge_key(key_node):
                merging = True
                buf.append(MERGE)
            else:
                buf.append(self.construct_object(key_node))
            buf.append(self.construct_object(value_node))
        return self._assemble_map(node, buf, merging)

    def _merge_sources(self, node: Node, buf: list) -> None:
        """A merge brings in the entries a source is written with. When a callback turned the
        source into something else, they are read from its node, as PyYAML does."""
        for i, (key_node, value_node) in enumerate(node.value):
            if not self._is_merge_key(key_node):
                continue
            got = buf[2 * i + 1]
            if type(got) is dict or (type(got) is list and all(type(m) is dict for m in got)):
                continue
            if isinstance(value_node, MappingNode):
                buf[2 * i + 1] = self._plain_map(value_node)
            elif isinstance(value_node, SequenceNode) and all(
                isinstance(m, MappingNode) for m in value_node.value
            ):
                buf[2 * i + 1] = [self._plain_map(m) for m in value_node.value]

    def _assemble_map(self, node: Node, buf: list, merging: bool) -> Any:
        if merging and node.value:
            self._merge_sources(node, buf)
        doc = self._context()
        e0 = node._event
        events = doc.events
        if e0 is None:
            # A node a callback made has no place in the document; the offset of an error is 0.
            e0 = (MAP_START, 0, 0, None, None, False)
            events = [e0]
        e_end = (MAP_END, node._end)
        build = _map_merging if merging else _map_last_wins
        try:
            return build(buf, e0, e_end, events, doc.info, doc.wide, doc.keys.setdefault)
        except Fail as exc:
            raise self._fail(exc, node) from None
        except TypeError:
            raise self._error("found unhashable key", node) from None

    def _finish(self, kind: str, node: Node, built: Any) -> Any:
        """The value of ``node`` as the standard type ``kind``, given its text or its parts."""
        if (
            (kind == "str" and node.id == "scalar")
            or (kind == "seq" and node.id == "sequence")
            or (kind == "map" and node.id == "mapping")
        ):
            return built
        try:
            return construct(_YAML + kind, built, self._cap)
        except Reject as exc:
            raise self._fail(Fail(exc.message, exc.category), node) from None

    def _lookup(self, node: Node) -> tuple[Any, str | None]:
        """The function that builds ``node`` and the tag suffix when a prefix matched. None for
        no function at all: the node is read as the plain type of its kind."""
        tag = node.tag
        ctors = self.yaml_constructors
        fn = ctors.get(tag)
        if fn is not None:
            return fn, None
        for prefix, multi in self.yaml_multi_constructors.items():
            if prefix is not None and tag.startswith(prefix):
                return multi, tag[len(prefix) :]
        if None in self.yaml_multi_constructors:
            return self.yaml_multi_constructors[None], tag
        if (
            self._merges
            and tag in (TAG_MERGE, TAG_VALUE)
            and getattr(node, "_implicit", False)
            and ctors.get(None) is SafeConstructor.construct_undefined
        ):
            # Where it is no merge key, a plain << (or =) is the text it spells.
            return SafeConstructor.construct_yaml_str, None
        return ctors.get(None), None

    def _construct(self, root: Node) -> Any:  # noqa: C901
        done = self.constructed_objects
        if root in self.recursive_objects:
            raise self._error("found unconstructable recursive node", root)
        if self._nesting >= MAX_NESTING:
            raise self._error(
                f"constructors call each other more than {MAX_NESTING} levels deep", root, "limit"
            )
        self._nesting += 1
        try:
            work = [root]
            while work:
                node = work[-1]
                if node in done:
                    work.pop()
                    continue
                fn, suffix = self._lookup(node)
                try:
                    kind = _BUILTIN.get(fn)
                except TypeError:  # a callable that cannot be hashed is a callback
                    kind = None
                if fn is not None and kind is None:
                    self._call(fn, suffix, node)
                    work.pop()
                    continue
                if kind == "undefined":
                    self.construct_undefined(node)  # always raises
                scalar = node.id == "scalar"
                if scalar:
                    text = self.construct_scalar(node)
                    if kind is None or kind == "str":
                        done[node] = text
                    elif kind == "int" and node._implicit and _NO_DIGITS(text):
                        # 0b_ and 0x_ match the int rule but hold no digits (PyYAML fails with a
                        # bare ValueError); they stay the text they spell.
                        done[node] = text
                    else:
                        done[node] = self._finish(kind, node, text)
                    work.pop()
                    continue
                if self._slow:
                    if node.id == "sequence":
                        built = self.construct_sequence(node)
                    else:
                        built = self.construct_mapping(node)
                    done[node] = built if kind is None else self._finish(kind, node, built)
                    work.pop()
                    continue
                if node.id == "sequence":
                    kids = node.value
                else:
                    merge_key = self._is_merge_key
                    kids = []
                    for k, v in node.value:
                        if not merge_key(k):
                            kids.append(k)
                        kids.append(v)
                missing = [c for c in kids if c not in done]
                if missing:
                    missing.reverse()
                    work.extend(missing)
                    continue
                if node.id == "sequence":
                    built = [done[c] for c in kids]
                else:
                    buf = []
                    merging = False
                    for k, v in node.value:
                        if merge_key(k):
                            merging = True
                            buf.append(MERGE)
                        else:
                            buf.append(done[k])
                        buf.append(done[v])
                    built = self._assemble_map(node, buf, merging)
                done[node] = built if kind is None else self._finish(kind, node, built)
                work.pop()
            return done[root]
        finally:
            self._nesting -= 1

    def _call(self, fn: Any, suffix: str | None, node: Node) -> None:
        rec = self.recursive_objects
        rec[node] = None
        try:
            data = fn(self, node) if suffix is None else fn(self, suffix, node)
            if isinstance(data, types.GeneratorType):
                # The value exists as soon as the generator has produced it; the rest of its
                # body fills it in. Nothing refers back to a node under construction, so the
                # body can run to the end now.
                generator = data
                data = next(generator)
                self.constructed_objects[node] = data
                for _ in generator:
                    pass
        finally:
            del rec[node]
        self.constructed_objects[node] = data


class SafeConstructor(BaseConstructor):
    """The standard YAML 1.1 types."""

    _merges = True

    def construct_yaml_null(self, node):
        return self._std("null", node)

    def construct_yaml_bool(self, node):
        return self._std("bool", node)

    def construct_yaml_int(self, node):
        return self._std("int", node)

    def construct_yaml_float(self, node):
        return self._std("float", node)

    def construct_yaml_binary(self, node):
        return self._std("binary", node)

    def construct_yaml_timestamp(self, node):
        return self._std("timestamp", node)

    def construct_yaml_omap(self, node):
        return self._std("omap", node)

    def construct_yaml_pairs(self, node):
        return self._std("pairs", node)

    def construct_yaml_set(self, node):
        return self._std("set", node)

    def construct_yaml_str(self, node):
        return self._std("str", node)

    def construct_yaml_seq(self, node):
        return self._std("seq", node)

    def construct_yaml_map(self, node):
        return self._std("map", node)

    def _std(self, kind: str, node: Node) -> Any:
        """What a standard constructor makes of ``node``, called from a callback."""
        if node.id == "scalar":
            built = self.construct_scalar(node)
        elif node.id == "sequence":
            built = self.construct_sequence(node)
        else:
            built = self.construct_mapping(node)
        return self._finish(kind, node, built)


class FullConstructor(SafeConstructor):
    """Reads what ``SafeConstructor`` reads; a ``!!python/...`` tag is refused."""


class UnsafeConstructor(FullConstructor):
    """Reads what ``SafeConstructor`` reads; nothing here runs code from a tag."""


class Constructor(SafeConstructor):
    """The same as ``UnsafeConstructor``."""


_STANDARD_KINDS = (
    "null", "bool", "int", "float", "binary", "timestamp", "omap", "pairs", "set", "str", "seq",
    "map",
)  # fmt: skip

# The functions that stand for the library's own constructors: a node they are registered for
# is built here, in the loop, instead of by a call.
_BUILTIN: dict[Any, str] = {
    getattr(SafeConstructor, f"construct_yaml_{kind}"): kind for kind in _STANDARD_KINDS
}
_BUILTIN[SafeConstructor.construct_undefined] = "undefined"


def _register_standard() -> None:
    for kind in _STANDARD_KINDS:
        SafeConstructor.add_constructor(
            _YAML + kind, getattr(SafeConstructor, f"construct_yaml_{kind}")
        )
    SafeConstructor.add_constructor(None, SafeConstructor.construct_undefined)
    # PyYAML's other constructors register their own tags, so each has tables of its own.
    for cls in (FullConstructor, UnsafeConstructor, Constructor):
        cls.yaml_constructors = SafeConstructor.yaml_constructors.copy()
        cls.yaml_multi_constructors = {}


_register_standard()

# The constructor methods a subclass may replace and the loader still honours: each one makes
# it read collections through the methods themselves.
_REPLACEABLE = ("construct_object", "construct_sequence", "construct_mapping")
_ORIGINAL = {name: getattr(BaseConstructor, name) for name in (*_REPLACEABLE, "construct_scalar")}
