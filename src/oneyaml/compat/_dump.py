"""The dump side of the PyYAML compatibility module: representers, serializer and dumpers.

``pyyaml.py`` keeps the public surface. A value becomes a node tree through the representers
(``represent_data`` and the ``represent_*`` methods a callback calls), and the tree is written
by :mod:`._pyemit` in PyYAML's layout. The standard collections (list, tuple, dict, set) are
walked with an explicit stack, so a deeply nested value never recurses; only callbacks that
call back into ``represent_data`` nest, and they are limited to 100 levels.
"""

from __future__ import annotations

import base64
import codecs
import collections
import copyreg
import datetime
import types
from typing import Any

from .. import _api
from . import _pyemit
from ._engine import BaseResolver, Resolver
from ._nodes import MappingNode, Node, ScalarNode, SequenceNode

_YAML = "tag:yaml.org,2002:"
TAG_NULL = _YAML + "null"
TAG_BOOL = _YAML + "bool"
TAG_INT = _YAML + "int"
TAG_FLOAT = _YAML + "float"
TAG_STR = _YAML + "str"
TAG_BINARY = _YAML + "binary"
TAG_SET = _YAML + "set"
TAG_TIMESTAMP = _YAML + "timestamp"
TAG_SEQ = _YAML + "seq"
TAG_MAP = _YAML + "map"

# How deep representers that call ``represent_data`` from inside a representer may nest.
MAX_CALLBACK_DEPTH = 100
# How deep collections may nest in a dump (the limit of the primary ``dumps``): the text grows
# with the square of the depth, so an unbounded value could use all the memory.
MAX_DEPTH = 10_000


def _error(name: str, message: str, *args: Any, category: str = "other") -> Exception:
    from . import pyyaml

    exc = getattr(pyyaml, name)(message, *args)
    exc.category = category
    return exc


# ---- representers ------------------------------------------------------------------------------

_SEQ, _MAP, _SET = 0, 1, 2


class BaseRepresenter:
    yaml_representers: dict = {}  # noqa: RUF012 (PyYAML's own shape: copied on first change)
    yaml_multi_representers: dict = {}  # noqa: RUF012

    def __init__(self, default_style=None, default_flow_style=False, sort_keys=True):
        self.default_style = default_style
        self.sort_keys = sort_keys
        self.default_flow_style = default_flow_style
        self.represented_objects = {}
        self.object_keeper = []
        self.alias_key = None
        self._depth = 0

    def represent(self, data):
        node = self.represent_data(data)
        self.serialize(node)
        self.represented_objects = {}
        self.object_keeper = []
        self.alias_key = None

    def represent_data(self, data):
        if self._depth >= MAX_CALLBACK_DEPTH:
            raise _error(
                "RepresenterError",
                f"representers call each other more than {MAX_CALLBACK_DEPTH} levels deep",
                category="limit",
            )
        self._depth += 1
        try:
            return self._represent(data)
        finally:
            self._depth -= 1

    def _by_table(self, data):
        """The node a registered representer makes of ``data``: its exact type first, then the
        multi-representers along its bases, then the catch-all."""
        data_types = type(data).__mro__
        reps = self.yaml_representers
        if data_types[0] in reps:
            return reps[data_types[0]](self, data)
        multi = self.yaml_multi_representers
        for data_type in data_types:
            if data_type in multi:
                return multi[data_type](self, data)
        if None in multi:
            return multi[None](self, data)
        if None in reps:
            return reps[None](self, data)
        return ScalarNode(None, str(data))

    def _inline_ok(self) -> bool:
        return (
            _is(self, "represent_sequence", BaseRepresenter.represent_sequence)
            and _is(self, "represent_mapping", BaseRepresenter.represent_mapping)
            and _is(self, "represent_scalar", BaseRepresenter.represent_scalar)
        )

    def _represent(self, root):  # noqa: C901
        stack: list[list] = []
        reps = self.yaml_representers
        represented = self.represented_objects
        inline = self._inline_ok()
        default_flow = self.default_flow_style
        ignore_aliases = self.ignore_aliases
        data = root
        while True:
            frame = None
            alias_key = None if ignore_aliases(data) else id(data)
            self.alias_key = alias_key
            if alias_key is not None and alias_key in represented:
                node = represented[alias_key]
            else:
                if alias_key is not None:
                    self.object_keeper.append(data)
                rep = reps.get(type(data)) if inline else None
                spec = _INLINE.get(rep) if rep is not None else None
                if spec is None:
                    node = self._by_table(data)
                else:
                    kind, tag = spec
                    if kind == _SEQ:
                        node = SequenceNode(tag, [], flow_style=None)
                        # A copy: a list that shrinks while it is written ends the walk of it.
                        flat = data if type(data) is tuple else list(data)
                    elif kind == _MAP:
                        node = MappingNode(tag, [], flow_style=None)
                        flat = self._flat_items(list(data.items()))
                    else:
                        node = MappingNode(tag, [], flow_style=None)
                        flat = self._flat_items([(key, None) for key in data])
                    if alias_key is not None:
                        represented[alias_key] = node
                    # node, is a mapping, children, how many are done, all plain scalars so far,
                    # the key waiting for its value
                    frame = [node, kind != _SEQ, flat, 0, True, None]
            while True:
                if frame is not None:
                    if len(frame[2]) > 0:
                        if len(stack) >= MAX_DEPTH:
                            raise _error(
                                "RepresenterError",
                                f"the value nests deeper than {MAX_DEPTH} levels",
                                category="limit",
                            )
                        stack.append(frame)
                        data = frame[2][0]
                        break
                    # An empty collection: nothing to wait for.
                    node.flow_style = default_flow if default_flow is not None else True
                    frame = None
                if not stack:
                    return node
                f = stack[-1]
                if not (isinstance(node, ScalarNode) and not node.style):
                    f[4] = False
                done = f[3]
                if f[1]:
                    if done % 2 == 0:
                        f[5] = node
                    else:
                        f[0].value.append((f[5], node))
                else:
                    f[0].value.append(node)
                done += 1
                f[3] = done
                if done < len(f[2]):
                    data = f[2][done]
                    break
                stack.pop()
                node = f[0]
                node.flow_style = default_flow if default_flow is not None else f[4]

    def _flat_items(self, items: list) -> list:
        """Key, value, key, value ... of ``items``, sorted when ``sort_keys`` asks for it and the
        keys can be compared."""
        if self.sort_keys:
            try:  # noqa: SIM105
                items = sorted(items)
            except TypeError:
                pass
        flat: list = []
        for pair in items:
            flat.extend(pair)
        return flat

    @classmethod
    def add_representer(cls, data_type, representer):
        if "yaml_representers" not in cls.__dict__:
            cls.yaml_representers = cls.yaml_representers.copy()
        cls.yaml_representers[data_type] = representer

    @classmethod
    def add_multi_representer(cls, data_type, representer):
        if "yaml_multi_representers" not in cls.__dict__:
            cls.yaml_multi_representers = cls.yaml_multi_representers.copy()
        cls.yaml_multi_representers[data_type] = representer

    def represent_scalar(self, tag, value, style=None):
        if style is None:
            style = self.default_style
        node = ScalarNode(tag, value, style=style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        return node

    def represent_sequence(self, tag, sequence, flow_style=None):
        value: list = []
        node = SequenceNode(tag, value, flow_style=flow_style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        best_style = True
        for item in sequence:
            node_item = self.represent_data(item)
            if not (isinstance(node_item, ScalarNode) and not node_item.style):
                best_style = False
            value.append(node_item)
        if flow_style is None:
            if self.default_flow_style is not None:
                node.flow_style = self.default_flow_style
            else:
                node.flow_style = best_style
        return node

    def represent_mapping(self, tag, mapping, flow_style=None):
        value: list = []
        node = MappingNode(tag, value, flow_style=flow_style)
        if self.alias_key is not None:
            self.represented_objects[self.alias_key] = node
        best_style = True
        if hasattr(mapping, "items"):
            mapping = list(mapping.items())
            if self.sort_keys:
                try:  # noqa: SIM105
                    mapping = sorted(mapping)
                except TypeError:
                    pass
        for item_key, item_value in mapping:
            node_key = self.represent_data(item_key)
            node_value = self.represent_data(item_value)
            if not (isinstance(node_key, ScalarNode) and not node_key.style):
                best_style = False
            if not (isinstance(node_value, ScalarNode) and not node_value.style):
                best_style = False
            value.append((node_key, node_value))
        if flow_style is None:
            if self.default_flow_style is not None:
                node.flow_style = self.default_flow_style
            else:
                node.flow_style = best_style
        return node

    def ignore_aliases(self, data):
        return False


def _inf_value() -> float:
    value = 1e300
    while repr(value) != repr(value * value):
        value *= value
    return value


class SafeRepresenter(BaseRepresenter):
    def ignore_aliases(self, data):
        if data is None:
            return True
        if isinstance(data, tuple) and data == ():
            return True
        if isinstance(data, (str, bytes, bool, int, float)):
            return True
        return None

    def represent_none(self, data):
        return self.represent_scalar(TAG_NULL, "null")

    def represent_str(self, data):
        return self.represent_scalar(TAG_STR, data)

    def represent_binary(self, data):
        text = base64.encodebytes(data).decode("ascii")
        return self.represent_scalar(TAG_BINARY, text, style="|")

    def represent_bool(self, data):
        return self.represent_scalar(TAG_BOOL, "true" if data else "false")

    def represent_int(self, data):
        return self.represent_scalar(TAG_INT, str(data))

    inf_value = _inf_value()

    def represent_float(self, data):
        if data != data or (data == 0.0 and data == 1.0):
            value = ".nan"
        elif data == self.inf_value:
            value = ".inf"
        elif data == -self.inf_value:
            value = "-.inf"
        else:
            value = repr(data).lower()
            # repr(1e17) is '1e+17', which is no float in YAML: the dot is added before the e.
            if "." not in value and "e" in value:
                value = value.replace("e", ".0e", 1)
        return self.represent_scalar(TAG_FLOAT, value)

    def represent_list(self, data):
        return self.represent_sequence(TAG_SEQ, data)

    def represent_dict(self, data):
        return self.represent_mapping(TAG_MAP, data)

    def represent_set(self, data):
        value = {}
        for key in data:
            value[key] = None
        return self.represent_mapping(TAG_SET, value)

    def represent_date(self, data):
        return self.represent_scalar(TAG_TIMESTAMP, data.isoformat())

    def represent_datetime(self, data):
        return self.represent_scalar(TAG_TIMESTAMP, data.isoformat(" "))

    def represent_yaml_object(self, tag, data, cls, flow_style=None):
        if hasattr(data, "__getstate__"):  # noqa: SIM108
            state = data.__getstate__()
        else:
            state = data.__dict__.copy()
        return self.represent_mapping(tag, state, flow_style=flow_style)

    def represent_undefined(self, data):
        raise _error("RepresenterError", "cannot represent an object", data)


SafeRepresenter.add_representer(type(None), SafeRepresenter.represent_none)
SafeRepresenter.add_representer(str, SafeRepresenter.represent_str)
SafeRepresenter.add_representer(bytes, SafeRepresenter.represent_binary)
SafeRepresenter.add_representer(bool, SafeRepresenter.represent_bool)
SafeRepresenter.add_representer(int, SafeRepresenter.represent_int)
SafeRepresenter.add_representer(float, SafeRepresenter.represent_float)
SafeRepresenter.add_representer(list, SafeRepresenter.represent_list)
SafeRepresenter.add_representer(tuple, SafeRepresenter.represent_list)
SafeRepresenter.add_representer(dict, SafeRepresenter.represent_dict)
SafeRepresenter.add_representer(set, SafeRepresenter.represent_set)
SafeRepresenter.add_representer(datetime.date, SafeRepresenter.represent_date)
SafeRepresenter.add_representer(datetime.datetime, SafeRepresenter.represent_datetime)
SafeRepresenter.add_representer(None, SafeRepresenter.represent_undefined)


class Representer(SafeRepresenter):
    """Also writes ``!!python/...`` tags, as PyYAML's does. Writing is all this does: reading
    those tags back is refused by every loader here."""

    def represent_complex(self, data):
        if data.imag == 0.0:
            text = f"{data.real!r}"
        elif data.real == 0.0:
            text = f"{data.imag!r}j"
        elif data.imag > 0:
            text = f"{data.real!r}+{data.imag!r}j"
        else:
            text = f"{data.real!r}{data.imag!r}j"
        return self.represent_scalar(_YAML + "python/complex", text)

    def represent_tuple(self, data):
        return self.represent_sequence(_YAML + "python/tuple", data)

    def represent_name(self, data):
        name = f"{data.__module__}.{data.__name__}"
        return self.represent_scalar(_YAML + "python/name:" + name, "")

    def represent_module(self, data):
        return self.represent_scalar(_YAML + "python/module:" + data.__name__, "")

    def represent_object(self, data):
        # The object's __reduce_ex__ gives (function, args, state, listitems, dictitems);
        # what is written is that description, so the object can be rebuilt by a loader that
        # runs code. Nothing is called here but the object's own reduce.
        cls = type(data)
        if cls in copyreg.dispatch_table:
            reduce = copyreg.dispatch_table[cls](data)
        elif hasattr(data, "__reduce_ex__"):
            reduce = data.__reduce_ex__(2)
        elif hasattr(data, "__reduce__"):
            reduce = data.__reduce__()
        else:
            raise _error("RepresenterError", "cannot represent an object", data)
        reduce = (list(reduce) + [None] * 5)[:5]
        function, args, state, listitems, dictitems = reduce
        args = list(args)
        if state is None:
            state = {}
        if listitems is not None:
            listitems = list(listitems)
        if dictitems is not None:
            dictitems = dict(dictitems)
        if function.__name__ == "__newobj__":
            function = args[0]
            args = args[1:]
            tag = _YAML + "python/object/new:"
            newobj = True
        else:
            tag = _YAML + "python/object/apply:"
            newobj = False
        function_name = f"{function.__module__}.{function.__name__}"
        if not args and not listitems and not dictitems and isinstance(state, dict) and newobj:
            return self.represent_mapping(_YAML + "python/object:" + function_name, state)
        if not listitems and not dictitems and isinstance(state, dict) and not state:
            return self.represent_sequence(tag + function_name, args)
        value = {}
        if args:
            value["args"] = args
        if state or not isinstance(state, dict):
            value["state"] = state
        if listitems:
            value["listitems"] = listitems
        if dictitems:
            value["dictitems"] = dictitems
        return self.represent_mapping(tag + function_name, value)

    def represent_ordered_dict(self, data):
        data_type = type(data)
        tag = f"{_YAML}python/object/apply:{data_type.__module__}.{data_type.__name__}"
        items = [[key, value] for key, value in data.items()]
        return self.represent_sequence(tag, [items])


Representer.add_representer(complex, Representer.represent_complex)
Representer.add_representer(tuple, Representer.represent_tuple)
Representer.add_multi_representer(type, Representer.represent_name)
Representer.add_representer(collections.OrderedDict, Representer.represent_ordered_dict)
Representer.add_representer(types.FunctionType, Representer.represent_name)
Representer.add_representer(types.BuiltinFunctionType, Representer.represent_name)
Representer.add_representer(types.ModuleType, Representer.represent_module)
Representer.add_multi_representer(object, Representer.represent_object)

# The standard collection representers, which the walk builds without a call per element.
_INLINE = {
    SafeRepresenter.represent_list: (_SEQ, TAG_SEQ),
    SafeRepresenter.represent_dict: (_MAP, TAG_MAP),
    SafeRepresenter.represent_set: (_SET, TAG_SET),
    Representer.represent_tuple: (_SEQ, _YAML + "python/tuple"),
}


# What the backend's writer assumes about a dumper: these representers for these types, and the
# standard implicit resolvers.
_PLAIN_REPRESENTERS = tuple(
    (t, SafeRepresenter.yaml_representers[t])
    for t in (type(None), str, bool, int, float, list, dict)
)
_STANDARD_IMPLICIT = {k: list(v) for k, v in Resolver.yaml_implicit_resolvers.items()}


# Columns the native writer counts in; a wider setting is read by the Python writer.
_MAX_COLUMN = 1 << 40


def _is(obj, name, func) -> bool:
    """Whether ``obj.name`` is ``func`` itself: not overridden by a subclass or the instance."""
    return getattr(getattr(obj, name, None), "__func__", None) is func


def _backend_body():
    """The active backend's writer for plain data, or None (the pure backend has none)."""
    return getattr(_api._impl, "pyyaml_body", None)


# ---- emitter and serializer --------------------------------------------------------------------


class Emitter:
    """The options of the writer, and the stream it writes to."""

    def increase_indent(self, flow=False, indentless=False):
        """One level deeper; a subclass may replace it (``indentless=False`` always indents a
        list under its key)."""
        w = self._writer
        w.indents.append(w.indent)
        if w.indent is None:
            w.indent = w.best_indent if flow else 0
        elif not indentless:
            w.indent += w.best_indent

    def __init__(
        self, stream, canonical=None, indent=None, width=None, allow_unicode=None, line_break=None
    ):
        self.stream = stream
        self.canonical = canonical
        self.allow_unicode = allow_unicode
        self._opt_indent = indent
        self._opt_width = width
        self._opt_line_break = line_break
        self._writer: _pyemit.Writer | None = None
        self._encoder = None


class Serializer:
    ANCHOR_TEMPLATE = "id%03d"

    def __init__(
        self, encoding=None, explicit_start=None, explicit_end=None, version=None, tags=None
    ):
        self.use_encoding = encoding
        self.use_explicit_start = explicit_start
        self.use_explicit_end = explicit_end
        self.use_version = version
        self.use_tags = tags
        self.serialized_nodes = {}
        self.anchors = {}
        self.last_anchor_id = 0
        self.closed = None
        self._first = True

    def open(self):
        if self.closed is None:
            self._writer = _pyemit.Writer(
                self.canonical, self._opt_indent, self._opt_width, self.allow_unicode,
                self._opt_line_break,
            )  # fmt: skip
            self._encoder = None
            encoding = self.use_encoding
            if encoding and not hasattr(self.stream, "encoding"):
                try:
                    self._encoder = codecs.getincrementalencoder(encoding)()
                except LookupError:
                    raise ValueError(f"unknown encoding: {encoding!r}") from None
                if encoding.lower().replace("_", "-") in ("utf-16-le", "utf-16-be"):
                    self._writer.out.append("\ufeff")
            self.closed = False
            self._first = True
        elif self.closed:
            raise _error("SerializerError", "serializer is closed")
        else:
            raise _error("SerializerError", "serializer is already opened")

    def close(self):
        if self.closed is None:
            raise _error("SerializerError", "serializer is not opened")
        if not self.closed:
            w = self._writer
            if w.open_ended:
                w.write_indicator("...", True)
                w.write_indent()
            self._flush(final=True)
            self.closed = True

    def _flush(self, final: bool = False) -> None:
        w = self._writer
        text = "".join(w.out)
        w.out.clear()
        enc = self._encoder
        if enc is not None:
            data = enc.encode(text, final)
            if data:
                self.stream.write(data)
        elif text:
            self.stream.write(text)
        if final and hasattr(self.stream, "flush"):
            self.stream.flush()

    def _needs_marker(self, empty: bool = False) -> bool:
        """Whether the next document starts with ``---``."""
        return not (
            self._first
            and not self.use_explicit_start
            and not self._writer.canonical
            and not self.use_version
            and not self.use_tags
            and not empty
        )

    def _document_start(self, empty: bool = False) -> None:
        """Write what comes before a document's content: directives and the ``---`` line."""
        if self.closed is None:
            raise _error("SerializerError", "serializer is not opened")
        if self.closed:
            raise _error("SerializerError", "serializer is closed")
        w = self._writer
        version = self.use_version
        tags = self.use_tags
        # A directive may only follow the end of a document. PyYAML writes the ``...`` only after
        # a bare scalar; a reader that follows the YAML 1.2 grammar needs it after any document.
        if (version or tags) and (w.open_ended or (not self._first and not self.use_explicit_end)):
            w.write_indicator("...", True)
            w.write_indent()
        if version:
            w.out.append("%YAML " + _pyemit.prepare_version(version))
            w.write_line_break()
        w.reset_tags()
        if tags:
            for handle in sorted(tags):
                prefix = tags[handle]
                w.tag_prefixes[prefix] = handle
                handle_text = _pyemit.prepare_tag_handle(handle)
                prefix_text = _pyemit.prepare_tag_prefix(prefix)
                w.out.append(f"%TAG {handle_text} {prefix_text}")
                w.write_line_break()
        marker = self._needs_marker(empty)
        self._first = False
        if marker:
            w.write_indent()
            w.write_indicator("---", True)
            if w.canonical:
                w.write_indent()

    def _document_end(self) -> None:
        w = self._writer
        w.write_indent()
        if self.use_explicit_end:
            w.write_indicator("...", True)
            w.write_indent()
        self._flush()

    def serialize(self, node):
        if self.closed is None:
            raise _error("SerializerError", "serializer is not opened")
        if self.closed:
            raise _error("SerializerError", "serializer is closed")
        self._document_start(isinstance(node, ScalarNode) and node.tag is None and node.value == "")
        self.anchors = {}
        self.last_anchor_id = 0
        self.anchor_node(node)
        try:
            _pyemit.Walk(self._writer, self, self.anchors).run(node)
        except BaseException:
            # The half-written document is useless, and after running out of memory the caller
            # needs the room back (the traceback keeps this buffer alive) to report the error.
            self._writer.out.clear()
            raise
        finally:
            self.anchors = {}
            self.serialized_nodes = {}
            self.last_anchor_id = 0
        self._document_end()

    def anchor_node(self, node):
        """Give an anchor to every node met a second time, numbered in the order the second
        meetings happen (a walk with an explicit stack, so depth never recurses)."""
        anchors = self.anchors
        work = [node]
        while work:
            current = work.pop()
            if current in anchors:
                if anchors[current] is None:
                    anchors[current] = self.generate_anchor(current)
            else:
                anchors[current] = None
                if isinstance(current, SequenceNode):
                    work.extend(reversed(current.value))
                elif isinstance(current, MappingNode):
                    for key, value in reversed(current.value):
                        work.append(value)
                        work.append(key)

    def generate_anchor(self, node):
        self.last_anchor_id += 1
        return self.ANCHOR_TEMPLATE % self.last_anchor_id


_pyemit.DEFAULT_INCREASE = Emitter.increase_indent


# ---- the dumpers -------------------------------------------------------------------------------


class _DumperCore:
    def __init__(
        self,
        stream,
        default_style=None,
        default_flow_style=False,
        canonical=None,
        indent=None,
        width=None,
        allow_unicode=None,
        line_break=None,
        encoding=None,
        explicit_start=None,
        explicit_end=None,
        version=None,
        tags=None,
        sort_keys=True,
    ):
        Emitter.__init__(
            self, stream, canonical=canonical, indent=indent, width=width,
            allow_unicode=allow_unicode, line_break=line_break,
        )  # fmt: skip
        Serializer.__init__(
            self, encoding=encoding, explicit_start=explicit_start, explicit_end=explicit_end,
            version=version, tags=tags,
        )  # fmt: skip
        BaseRepresenter.__init__(
            self, default_style=default_style, default_flow_style=default_flow_style,
            sort_keys=sort_keys,
        )  # fmt: skip
        BaseResolver.__init__(self)

    def dispose(self):
        pass

    def represent(self, data):
        body = _backend_body()
        if body is not None and self._fast_ok():
            w = self._writer
            got = body(
                data, self._needs_marker(), w.best_indent, w.best_width, w.allow_unicode,
                bool(self.sort_keys), w.best_line_break,
            )  # fmt: skip
            if got is not None:
                text, open_ended = got
                self._document_start()
                # The cursor stands where the text ends; the document end moves it to a new line.
                w.out.append(text)
                w.column = 1
                w.whitespace = w.indention = False
                w.indent = None
                w.open_ended = open_ended
                self._document_end()
                return
        BaseRepresenter.represent(self, data)

    def _fast_ok(self) -> bool:
        """Whether the backend can write this dumper's documents: plain data, block style, the
        standard tables, nothing replaced. Everything is read from the instance, as the Python
        writer reads it, so an override set in ``__init__`` counts."""
        flow = self.default_flow_style
        w = self._writer
        return (
            self.closed is False
            and self.default_style is None
            and flow is not None
            and not flow
            and not w.canonical
            and not self.use_version
            and not self.use_tags
            # Only whole numbers in range: a float width ("never fold") or a huge one is the
            # Python writer's business.
            and type(w.best_indent) is int
            and type(w.best_width) is int
            and 0 < w.best_indent < _MAX_COLUMN
            and 0 < w.best_width < _MAX_COLUMN
            and _is(self, "represent_data", BaseRepresenter.represent_data)
            and _is(self, "serialize", Serializer.serialize)
            and _is(self, "increase_indent", Emitter.increase_indent)
            and _is(self, "ignore_aliases", SafeRepresenter.ignore_aliases)
            and _is(self, "resolve", BaseResolver.resolve)
            and not self.yaml_path_resolvers
            and self.yaml_implicit_resolvers == _STANDARD_IMPLICIT
            and self.DEFAULT_SCALAR_TAG == TAG_STR
            and self.DEFAULT_SEQUENCE_TAG == TAG_SEQ
            and self.DEFAULT_MAPPING_TAG == TAG_MAP
            and self._inline_ok()
            and all(self.yaml_representers.get(t) is f for t, f in _PLAIN_REPRESENTERS)
        )


class BaseDumper(_DumperCore, Emitter, Serializer, BaseRepresenter, BaseResolver):
    pass


class SafeDumper(_DumperCore, Emitter, Serializer, SafeRepresenter, Resolver):
    pass


class Dumper(_DumperCore, Emitter, Serializer, Representer, Resolver):
    pass


# PyYAML's C dumpers are separate classes with tables of their own; the same writer serves them.
class CBaseDumper(_DumperCore, Emitter, Serializer, BaseRepresenter, BaseResolver):
    pass


class CSafeDumper(_DumperCore, Emitter, Serializer, SafeRepresenter, Resolver):
    pass


class CDumper(_DumperCore, Emitter, Serializer, Representer, Resolver):
    pass


__all__ = [
    "BaseDumper",
    "BaseRepresenter",
    "CBaseDumper",
    "CDumper",
    "CSafeDumper",
    "Dumper",
    "Emitter",
    "MappingNode",
    "Node",
    "Representer",
    "SafeDumper",
    "SafeRepresenter",
    "ScalarNode",
    "SequenceNode",
    "Serializer",
]
