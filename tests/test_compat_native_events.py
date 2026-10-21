"""The node composer on the native backend, and the load that composes only what callbacks take.

* The native backend feeds the composer its own events; the nodes must be those the pure
  parser's events give, down to the marks. Where they differ the difference is named.
* A loader with constructors registered builds the document natively and composes only the nodes
  a callback takes over; whatever that cannot do it leaves to the node-by-node reader, and the
  answer is the same.
* The pure backend parses a stream once, whichever way it builds the values.
"""

from __future__ import annotations

import contextlib
import glob
import os
import pathlib

import pytest

import oneyaml.compat.pyyaml as cy
from hook_corpus import tagged_documents
from oneyaml import _api
from oneyaml.compat import _engine, _nodes
from test_compat_pyyaml import outcome, same

yaml = pytest.importorskip("yaml")

KIT = os.environ.get("ONEYAML_KIT")


def _native():
    return pytest.importorskip("oneyaml._native")


# ---- the same nodes as the pure parser's -------------------------------------------------------


def dump(node, marks=True):
    """Everything a callback can see of a node tree, as a list (shared nodes by number)."""
    out = []
    stack = [node]
    seen = {}
    while stack:
        n = stack.pop()
        if id(n) in seen:
            out.append(("again", seen[id(n)]))
            continue
        seen[id(n)] = len(seen)
        where = None
        if marks:
            s, e = n.start_mark, n.end_mark
            where = (s.index, s.line, s.column, e.index, e.line, e.column)
        if n.id == "scalar":
            out.append(("scalar", n.tag, n.style, n.value, where))
        else:
            out.append((n.id, n.tag, n.flow_style, where))
            if n.id == "sequence":
                stack.extend(reversed(n.value))
            else:
                for k, v in reversed(n.value):
                    stack.extend((v, k))
    return out


def composed(text, impl, marks=True):
    old = _api._impl
    _api._impl = impl
    try:
        return "ok", [dump(d, marks) for d in cy.compose_all(text, cy.SafeLoader)]
    except cy.YAMLError as exc:
        return "err", exc.category
    finally:
        _api._impl = old


def has_empty_node(text):
    from oneyaml._pure._parser import parse_events

    try:
        events = parse_events(text, 10_000)
    except Exception:
        return False
    return any(e[0] == 8 and e[6] == ":" and e[7] == "" for e in events)


HAND_MADE = [
    "a: 1\nb: [x, {y: z}]\n",
    "- &a x\n- *a\n- !!str 12\n",
    "k: |\n  text\n  more\n\nl: >-\n  folded\n   more\n",
    "--- |\n  a\n...\n--- >\n b\n",
    "'é': \"ü\\n\"\nz: ß\n",
    "a: 1\r\nb: |\r\n  x\r\n  y\r\nc: [1,\r\n 2]\r\n",
    "\ufeffa: 1\n",
    "! a: !!seq [1]\n? [x, y]\n: !!map {z: 1}\n",
    "{a: 1, b: [2, 3], ? c : d}\n",
    "- - - deep\n    - er\n  - up\n- end\n",
    "a: 1 # c\n# lone\nb: 2\n",
    "%TAG !e! tag:example.com,2000:\n--- !e!x a\n",
    "text: 'it''s'\nother: \"a\\tb\"\n",
    "a: !t\nb: &x\nc: *x\n",
    "--- a\n--- b\n...\n--- c\n",
]


def _corpus():
    texts = [(f"hand-made {i}", t) for i, t in enumerate(HAND_MADE)]
    texts += [(f"generated {i}", t) for i, t in enumerate(tagged_documents(120))]
    if KIT:
        root = pathlib.Path(KIT) / "suites"
        files = sorted(glob.glob(f"{root}/yaml-test-suite/data-*/**/in.yaml", recursive=True))
        files += sorted(glob.glob(f"{root}/own/cases/*/*/input.yaml"))
        for f in files:
            if "/safety/" in f:
                continue
            with contextlib.suppress(UnicodeDecodeError):
                path = pathlib.Path(f)
                texts.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
    return texts


# Inputs on which the engine's events differ from the pure parser's: it reads them the other
# way round (the kit lists the same case for the native backend). Strict: when the engine
# changes and the backends agree, the entry has to go.
ENGINE_DEVIATIONS = [
    "own:diagnostics/c1-control-in-a-comment-is-an-error",
    "own:diagnostics/del-in-a-comment-is-an-error",
    "own:diagnostics/empty-key-with-adjacent-flow-value-is-invalid",
    "own:diagnostics/explicit-key-with-adjacent-flow-value-is-invalid",
    "own:diagnostics/plain-key-with-adjacent-flow-value-is-invalid",
    "own:diagnostics/property-only-key-with-adjacent-flow-value-is-invalid",
    "own:diagnostics/verbatim-tag-of-a-lone-bang-is-invalid",
    "own:diagnostics/verbatim-tag-that-is-not-a-uri-is-invalid",
    "own:parse/tab-only-line-after-block-scalar-at-end",
    "own:parse/tag-suffix-utf-8-percent-escape-is-kept",
    "own:schema/escaped-surrogate-pair-is-one-character",
]
# Not a kit case: the engine says an undeclared tag handle is unresolved, the pure parser calls it
# a syntax error (both reject the stream).
CATEGORY_DIFFERENCES = ["yaml-test-suite/data-2022-01-17/QLJ7"]
KNOWN = {c.replace("own:", "own/cases/", 1) for c in ENGINE_DEVIATIONS} | set(CATEGORY_DIFFERENCES)


def _key(name):
    return pathlib.PurePosixPath(name).parent.as_posix()


@pytest.mark.native_only
def test_native_events_compose_the_nodes_the_pure_parser_does():
    from oneyaml import _pure

    native = _native()
    mismatched = []
    listed_seen = set()
    for name, text in _corpus():
        a = composed(text, native)
        b = composed(text, _pure)
        key = _key(name)
        if key in KNOWN:
            listed_seen.add(key)
            if a == b:
                mismatched.append((name, "listed, but the backends agree now"))
            continue
        if a == b:
            continue
        # The two parsers put an empty node (`key:` with nothing after it) in different places,
        # and a collection ends after its last node, so marks near one differ. Everything else
        # about the tree has to agree.
        if has_empty_node(text) and composed(text, native, marks=False) == composed(
            text, _pure, marks=False
        ):
            continue
        mismatched.append((name, a, b))
    assert not mismatched, mismatched[:3]
    if KIT:
        assert listed_seen == KNOWN


# Where PyYAML puts a node that is not written: right after the indicator that promised it
# (`:`, `-` or `?`), else where the next token begins. The native events follow it.
EMPTY_NODES = [
    "a:\nb: 1\n",
    "a: # c\nb: 1\n",
    "- \n- x\n",
    "-\n",
    "? k\n",
    "---\n---\n",
    "a: [x, ]\nb: {c: , d: }\n",
    "k: !t\nj: &a\n",
    "a:\n  b:\n  c:\nd:\n",
]


@pytest.mark.native_only
@pytest.mark.parametrize("text", EMPTY_NODES)
def test_the_native_backend_places_empty_nodes_as_pyyaml_does(text):
    native = _native()
    old = _api._impl
    _api._impl = native
    try:
        ours = [d for d in cy.compose_all(text, cy.SafeLoader)]
    finally:
        _api._impl = old

    def empties(root, attr):
        out, stack = [], [root]
        while stack:
            n = stack.pop()
            if n is None:
                continue
            if isinstance(n.value, list):
                for item in reversed(n.value):
                    stack.extend(reversed(item) if isinstance(item, tuple) else [item])
            elif (
                n.value == ""
                and getattr(n, attr) is None
                or n.value == ""
                and n.tag.endswith(":null")
            ):
                out.append((n.start_mark.index, n.end_mark.index))
        return sorted(out)

    ref = list(yaml.compose_all(text, yaml.SafeLoader))
    # PyYAML does not mark an unwritten node's end apart from its start.
    got = sorted((s, s) for d in ours for s, _ in empties(d, "style"))
    want = sorted((s, s) for d in ref for s, _ in empties(d, "style"))
    assert got == want


# ---- the load that composes only what callbacks take --------------------------------------------


class Point:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return type(other) is Point and same(self.value, other.value)

    def __hash__(self):
        return hash(repr(self.value))

    def __repr__(self):
        return f"Point({self.value!r})"


def _register(cls):
    def scalar(loader, node):
        return Point(("scalar", loader.construct_scalar(node)))

    def mapping(loader, node):
        return Point(("map", loader.construct_mapping(node, deep=True)))

    def sequence(loader, node):
        return Point(("seq", loader.construct_sequence(node, deep=True)))

    cls.add_constructor("!t", scalar)
    cls.add_constructor("!m", mapping)
    cls.add_constructor("!s", sequence)
    return cls


def _loaders():
    class Y(yaml.SafeLoader):
        pass

    class C(cy.SafeLoader):
        pass

    return _register(Y), _register(C)


HYBRID_DOCS = {
    "root scalar": "!t x\n",
    "root mapping": "!m {a: 1}\n",
    "sequence item": "- 1\n- !t x\n- [!t y]\n",
    "mapping value": "a: !m {b: 1}\nc: !t z\nd: [1, 2]\n",
    "nested tags": "a: !m {b: !t x, c: !s [!t y]}\n",
    "flow": "{a: !t x, b: [!t y, !t z]}\n",
    "block scalar": "a: !t |\n  text\n  more\nb: 1\n",
    "empty node": "a: !t\nb: !t\nc: 1\n",
    "duplicate key, the tagged one first": "a: !t x\na: 2\n",
    "duplicate key, the tagged one last": "a: 2\na: !t x\n",
    "anchor on a tagged node": "a: &x !t v\nb: *x\n",
    "anchor inside a tagged node": "a: !m {b: &y 1}\nc: *y\n",
    "alias after a tagged node": "a: &x [1, 2]\nb: !t q\nc: *x\n",
    "alias inside a tagged node": "a: &x [1]\nb: !m {c: *x}\n",
    "alias to an earlier plain node": "a: &x [1]\nb: *x\nc: !t q\n",
    "tagged key": "!t k: v\n",
    "tagged node as a merge value": "base: &b {x: 1}\nm:\n  <<: !m {y: 2}\n  z: 3\n",
    "tagged node inside a merge list": "m:\n  <<: [!m {y: 2}]\n  z: 1\n",
    "merge inside a tagged node": "b: &b {x: 1}\nm: !m\n  <<: *b\n  y: 2\n",
    "hole in a mapping that merges": "m:\n  <<: {x: 1}\n  a: !t v\n",
    "hole before an inline merge": "m: {a: !t v, <<: {a: 1}}\n",
    "hole in the root mapping that merges": "<<: {x: 1}\na: !t v\n",
    "hole in a mapping that merges an anchor": "b: &b {x: 1}\nm:\n  <<: *b\n  a: !t v\n",
    "hole in an omap": "!!omap [ {a: !t v} ]\n",
    "hole in an omap value": "x: !!omap\n  - a: !t v\n  - b: 2\n",
    "hole in pairs": "!!pairs [ {a: !t v} ]\n",
    "hole in a set": "!!set {a: !t v}\n",
    "several documents": "!t a\n---\n!t b\n",
    "non-ASCII text": "é: !t ü\nz: !t ß\n",
    "carriage returns": "a: !t x\r\nb: !t y\r\n",
    "an unknown tag": "a: !t x\nb: !nobody y\n",
    "a syntax error after a tagged node": "a: !t x\nb: [1\n",
    "plain document": "a: 1\nb: [x, yes]\n",
}


# Where this library answers differently on purpose: a key that is a list becomes a tuple (PyYAML
# refuses it), and a value that does not fit its tag is a ConstructorError (PyYAML lets a raw
# ValueError out).
OWN_ANSWERS = {
    "tagged node inside a complex key": (
        "? [!t a]\n: v\n",
        {(Point(("scalar", "a")),): "v"},
    ),
    "a tag of another kind": ("a: !t x\nb: !!int abc\n", cy.ConstructorError),
}


@pytest.mark.parametrize("name", OWN_ANSWERS)
def test_registered_constructors_give_these_answers_of_their_own(api, name):
    text, expected = OWN_ANSWERS[name]
    _, ours_cls = _loaders()
    if isinstance(expected, type):
        with pytest.raises(expected):
            cy.load(text, ours_cls)
    else:
        assert same(cy.load(text, ours_cls), expected)


@pytest.mark.parametrize("name", list(HYBRID_DOCS))
def test_registered_constructors_give_the_answer_pyyaml_gives(api, name):
    text = HYBRID_DOCS[name]
    theirs_cls, ours_cls = _loaders()
    ref = outcome(lambda t: yaml.load(t, theirs_cls), text, yaml.YAMLError)
    got = outcome(lambda t: cy.load(t, ours_cls), text, cy.YAMLError)
    assert ref[0] == got[0], (ref, got)
    if ref[0] == "ok":
        assert same(ref[1], got[1]), (ref, got)
    else:
        assert ref[1] == got[1]


def _marks_of_tagged_nodes(text, impl):
    seen = []

    def record(loader, node):
        s, e = node.start_mark, node.end_mark
        seen.append((node.tag, node.id, (s.index, s.line, s.column), (e.index, e.line, e.column)))
        return len(seen)

    class Mine(cy.SafeLoader):
        pass

    for tag in ("!t", "!m", "!s"):
        Mine.add_constructor(tag, record)
    old = _api._impl
    _api._impl = impl
    try:
        cy.load(text, Mine)
    finally:
        _api._impl = old
    return seen


@pytest.mark.native_only
@pytest.mark.parametrize(
    "name",
    [
        n
        for n in [*HYBRID_DOCS, "tagged node inside a complex key"]
        if n
        not in {
            "several documents",
            "an unknown tag",
            "a syntax error after a tagged node",
            "plain document",
        }
    ],
)
def test_a_callback_sees_the_same_marks_on_both_backends(name):
    from oneyaml import _pure

    text = HYBRID_DOCS[name] if name in HYBRID_DOCS else OWN_ANSWERS[name][0]
    assert _marks_of_tagged_nodes(text, _native()) == _marks_of_tagged_nodes(text, _pure)
    assert _marks_of_tagged_nodes("\ufeff" + text, _native()) == _marks_of_tagged_nodes(
        "\ufeff" + text, _pure
    )
    data = text.encode("utf-16")
    assert _marks_of_tagged_nodes(data, _native()) == _marks_of_tagged_nodes(data, _pure)


def _count_composed(monkeypatch):
    """The events the composer is given, in all."""
    total = []
    real = _engine._compose
    monkeypatch.setattr(
        _engine,
        "_compose",
        lambda loader, events, *a: total.append(len(events)) or real(loader, events, *a),
    )
    return total


@pytest.mark.native_only
def test_only_the_nodes_a_callback_takes_are_composed(monkeypatch):
    monkeypatch.setattr(_api, "_impl", _native())
    _, ours_cls = _loaders()
    text = "".join(f"- {{id: {i}, p: !m {{x: {i}}}, t: [a, b]}}\n" for i in range(50))
    total = _count_composed(monkeypatch)
    got = cy.load(text, ours_cls)
    assert got[7]["p"] == Point(("map", {"x": 7}))
    assert len(got) == 50
    # Each tagged mapping is a start, an end, a key and a value, plus the document's own
    # start and end that every composition gets.
    assert sum(total) == 50 * (4 + 2)


@pytest.mark.native_only
@pytest.mark.parametrize(
    "name",
    [
        "anchor on a tagged node",
        "anchor inside a tagged node",
        "alias after a tagged node",
        "alias inside a tagged node",
        "tagged key",
        "tagged node inside a complex key",
        "tagged node as a merge value",
        "tagged node inside a merge list",
    ],
)
def test_what_the_native_load_cannot_do_is_composed_whole(monkeypatch, name):
    monkeypatch.setattr(_api, "_impl", _native())
    text = HYBRID_DOCS[name] if name in HYBRID_DOCS else OWN_ANSWERS[name][0]
    _, ours_cls = _loaders()
    total = _count_composed(monkeypatch)
    outcome(lambda t: cy.load(t, ours_cls), text, cy.YAMLError)
    assert total == [len(list(_pure_events(text)))]


def _pure_events(text):
    from oneyaml._pure._parser import parse_events

    return parse_events(text, 10_000)


@pytest.mark.native_only
def test_no_callback_runs_for_a_document_that_does_not_parse(monkeypatch):
    monkeypatch.setattr(_api, "_impl", _native())
    ran = []

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!t", lambda ld, n: ran.append(1))
    with pytest.raises(cy.YAMLError):
        cy.load("a: !t x\nb: [1\n", Mine)
    assert not ran


@pytest.mark.native_only
def test_a_callback_raising_ends_the_load_with_its_own_error(monkeypatch):
    monkeypatch.setattr(_api, "_impl", _native())

    class Mine(cy.SafeLoader):
        pass

    def boom(loader, node):
        raise KeyError("from the callback")

    Mine.add_constructor("!t", boom)
    with pytest.raises(KeyError, match="from the callback"):
        cy.load("a: 1\nb: !t x\n", Mine)


# ---- the pure backend parses once --------------------------------------------------------------


def test_a_registered_tag_costs_the_pure_backend_one_parse(monkeypatch):
    from oneyaml import _pure
    from oneyaml._pure import _construct

    monkeypatch.setattr(_api, "_impl", _pure)
    calls = []
    real = _construct.parse_events
    monkeypatch.setattr(_construct, "parse_events", lambda *a: calls.append(1) or real(*a))
    monkeypatch.setattr(_engine, "parse_events", lambda *a: calls.append(1) or real(*a))
    _, ours_cls = _loaders()
    assert cy.load("a: !t x\nb: [1, 2]\n", ours_cls) == {"a": Point(("scalar", "x")), "b": [1, 2]}
    assert len(calls) == 1
    calls.clear()
    assert cy.load("a: 1\n", ours_cls) == {"a": 1}
    assert len(calls) == 1


# ---- nodes -------------------------------------------------------------------------------------


def test_nodes_keep_what_a_callback_may_hang_on_them():
    node = _nodes.ScalarNode("tag:yaml.org,2002:str", "x")
    node.note = "mine"
    assert node.note == "mine"
    import weakref

    assert weakref.ref(node)() is node

    class Mine(_nodes.MappingNode):
        pass

    m = Mine("tag:yaml.org,2002:map", [], flow_style=True)
    assert m.flow_style is True and m.start_mark is None


def test_a_composed_node_holds_no_dict_of_its_own():
    loader = cy.SafeLoader("a: [1, 2]\nb: c\n")
    root = loader.get_single_node()
    nodes = [root]
    for _, v in root.value:
        nodes.append(v)
    assert all(not n.__dict__ for n in nodes)


def test_building_values_hangs_nothing_on_a_node():
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!t", lambda ld, n: ld.construct_scalar(n))
    loader = Mine("a: !t x\nb: [1, 2]\nc: {d: yes}\n")
    root = loader.get_single_node()
    nodes = [root]
    for _, v in root.value:
        nodes.append(v)
    built = loader.construct_document(root)
    assert built == {"a": "x", "b": [1, 2], "c": {"d": True}}
    assert all(not n.__dict__ for n in nodes)


# ---- big input: the engine on a second thread ---------------------------------------------------

OFF, PANIC_IN_PARSER, _TRANSLATOR, REFUSE_THREADS, INTERRUPT = range(5)


def _big_text():
    text = "".join(f"- {{id: {i}, p: !m {{x: {i}}}, t: [a, b]}}\n" for i in range(6000))
    assert len(text) > 128 * 1024
    return text


@pytest.fixture
def big(monkeypatch):
    native = _native()
    monkeypatch.setattr(_api, "_impl", native)
    yield native
    native._test_hook(OFF)


def test_a_big_document_builds_what_a_small_one_does(big):
    _, ours_cls = _loaders()
    text = _big_text()
    got = cy.load(text, ours_cls)
    assert len(got) == 6000 and got[5999] == {
        "id": 5999,
        "p": Point(("map", {"x": 5999})),
        "t": ["a", "b"],
    }
    assert got[:50] == cy.load("".join(text.splitlines(True)[:50]), ours_cls)
    big._test_hook(REFUSE_THREADS)
    assert cy.load(text, ours_cls) == got


def test_a_panic_while_the_engine_runs_is_a_yaml_error(big):
    _, ours_cls = _loaders()
    text = _big_text()
    big._test_hook(PANIC_IN_PARSER)
    ran = []
    ours_cls.add_constructor("!m", lambda ld, n: ran.append(1))
    with pytest.raises(cy.YAMLError) as e:
        cy.load(text, ours_cls)
    assert "deliberate panic" in e.value.message
    assert not ran


def test_a_pending_signal_ends_a_hooked_load(big):
    import signal

    _, ours_cls = _loaders()
    big._test_hook(INTERRUPT)
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            cy.load(_big_text(), ours_cls)
    finally:
        signal.signal(signal.SIGINT, old)
    assert cy.load("a: !t x\n", ours_cls) == {"a": Point(("scalar", "x"))}


def _errors_from_callbacks(impl, category):
    from oneyaml._errors import make_error

    calls = []

    class M(cy.SafeLoader):
        pass

    def first(ld, node):
        calls.append("first")
        return 1

    def bad(ld, node):
        calls.append("bad")
        raise make_error("inner failure", category, 1, 1, 0)

    M.add_constructor("!first", first)
    M.add_constructor("!bad", bad)
    old = _api._impl
    _api._impl = impl
    try:
        with pytest.raises(Exception) as e:
            cy.load("a: !first x\nb: !bad y\n", M)
    finally:
        _api._impl = old
    return type(e.value), e.value.message, calls


@pytest.mark.native_only
@pytest.mark.parametrize("category", ["syntax", "construct"])
def test_an_error_a_callback_raises_reaches_the_caller_as_it_is(category):
    from oneyaml import _pure

    native = _errors_from_callbacks(_native(), category)
    assert native == _errors_from_callbacks(_pure, category)
    assert native[2] == ["first", "bad"]
    assert not issubclass(native[0], cy.YAMLError)


@pytest.mark.native_only
def test_a_pending_signal_ends_the_read_of_one_big_tagged_node(big):
    import signal

    _, ours_cls = _loaders()
    ran = []
    ours_cls.add_constructor("!m", lambda ld, n: ran.append(1))
    text = "!m\n" + "".join(f"k{i}: v\n" for i in range(20000))
    big._test_hook(INTERRUPT)
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            cy.load(text, ours_cls)
    finally:
        signal.signal(signal.SIGINT, old)
    assert not ran
