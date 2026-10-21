"""Callbacks registered on the PyYAML-compatible loaders: constructors, prefix constructors,
resolvers, ``YAMLObject``, and a comparison with PyYAML itself.

The comparison runs every registration program of ``hook_corpus`` against generated documents
(and, where the program changes how every scalar reads, the whole corpus), once through PyYAML
and once through this library on each backend. Where the library answers differently on purpose
the case is named in ``INTENDED``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re

import pytest

import oneyaml.compat.pyyaml as cy
from hook_corpus import PROGRAMS, SUBCLASS_PROGRAMS, tagged_documents
from oneyaml import _api
from test_compat_pyyaml import KIT, outcome, pyyaml_loader, same

yaml = pytest.importorskip("yaml")

HERE = pathlib.Path(__file__).parent


_ATTRS = (
    "yaml_constructors", "yaml_multi_constructors", "yaml_implicit_resolvers",
    "yaml_path_resolvers",
)  # fmt: skip


def _registries(*modules):
    """Every class that holds registrations, once."""
    seen = {}
    for module in modules:
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and any(a in vars(obj) for a in _ATTRS):
                seen[obj] = None
    return list(seen)


@pytest.fixture(autouse=True)
def keep_registrations_local():
    """A test registers on the classes the library ships; the next test must not see it."""
    import yaml.constructor
    import yaml.loader
    import yaml.resolver

    classes = _registries(cy, cy.constructor, cy.resolver, yaml, yaml.constructor, yaml.resolver)
    saved = []
    for cls in classes:
        own = {}
        for attr in _ATTRS:
            if attr in vars(cls):
                value = vars(cls)[attr]
                own[attr] = (
                    {k: v[:] for k, v in value.items()}
                    if attr == "yaml_implicit_resolvers"
                    else dict(value)
                )
        saved.append((cls, own))
    yield
    for cls, own in saved:
        for attr in _ATTRS:
            if attr in own:
                setattr(cls, attr, own[attr])
            elif attr in vars(cls):
                delattr(cls, attr)


def canon(value):
    """The value with instances of the test classes turned into tuples, and NaN into text, so
    that the two libraries' own classes and NaN-holding sets compare."""
    if isinstance(value, (list, tuple)):
        out = [canon(v) for v in value]
        return tuple(out) if isinstance(value, tuple) else out
    if isinstance(value, dict):
        return {canon(k): canon(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return frozenset(canon(v) for v in value)
    if isinstance(value, float) and value != value:
        return "NaN"
    if type(value).__name__ == "Y" and hasattr(value, "__dict__"):
        return ("Y", canon(dict(value.__dict__)))
    return value


def build(module, program, base_name="SafeLoader"):
    """A fresh loader class of ``module`` with ``program`` registered on it."""
    base = getattr(module, base_name)
    if program in SUBCLASS_PROGRAMS:
        return SUBCLASS_PROGRAMS[program](module, base)
    loader = type("L", (base,), {})
    return PROGRAMS[program](module, loader)


def run(module, error, loader, text, single=False):
    if single:
        return outcome(lambda t: canon(module.load(t, loader)), text, error)
    return outcome(lambda t: canon(list(module.load_all(t, loader))), text, error)


def answers(program, texts, base_name="SafeLoader", single=False):
    """The texts on which the library and PyYAML answer differently: (text, ours, theirs)."""
    theirs_loader = build(yaml, program, base_name)
    ours_loader = build(cy, program, base_name)
    bad = []
    for text in texts:
        ref = run(yaml, yaml.YAMLError, theirs_loader, text, single)
        got = run(cy, cy.YAMLError, ours_loader, text, single)
        ok = (ref[0] == got[0] == "ok" and same(ref[1], got[1])) or (
            ref[0] != "ok" and got[0] == ref[0] == "err"
        )
        if not ok and ref[0] == "raw" and got[0] == "err":
            ok = True  # PyYAML lets a KeyError or ValueError out for a bad !!bool, !!int, ...
        if not ok:
            bad.append((text, got, ref))
    return bad


# What differs on purpose, by program: a predicate on (text, ours, theirs) and the reason.
INTENDED: dict[str, list] = {
    "*": [
        (
            # The tag of a node keeps %XX escapes that spell non-ASCII characters; PyYAML decodes
            # them (the parsers of both backends agree on this).
            lambda text, ours, theirs: re.search(r"%[89A-Fa-f][0-9A-Fa-f]", text) is not None,
            "a tag keeps non-ASCII percent escapes",
        )
    ],
    # A loader that reads every key of a mapping itself meets the merge key. PyYAML has no
    # constructor for the merge tag and fails; here a << that is not merging is the text it spells.
    "unique-keys": [
        (
            lambda text, ours, theirs: ours[0] == "ok" and theirs[0] == "err" and "<<" in text,
            "the merge key reads as text in a loader that constructs its keys",
        )
    ],
}


def unexplained(program, bad):
    rules = INTENDED.get(program, []) + INTENDED.get("*", [])
    return [b for b in bad if not any(rule(*b) for rule, _why in rules)]


# ---- registering and scoping -------------------------------------------------------------------


def test_a_constructor_on_a_subclass_reaches_only_that_subclass(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!twice", lambda ld, node: ld.construct_scalar(node) * 2)
    assert cy.load("a: !twice ab", Mine) == {"a": "abab"}
    for plain in (cy.SafeLoader, cy.FullLoader, cy.UnsafeLoader, cy.Loader, cy.CSafeLoader):
        with pytest.raises(cy.ConstructorError):
            cy.load("a: !twice ab", plain)


def test_a_constructor_on_safe_loader_reaches_safe_load_and_its_subclasses(api):
    class Child(cy.SafeLoader):
        pass

    cy.SafeLoader.add_constructor("!shout", lambda ld, node: ld.construct_scalar(node).upper())
    try:
        assert cy.safe_load("!shout hi") == "HI"
        assert cy.load("!shout hi", Child) == "HI"
        for other in (cy.FullLoader, cy.UnsafeLoader, cy.Loader, cy.CSafeLoader, cy.BaseLoader):
            if other is cy.BaseLoader:
                continue
            with pytest.raises(cy.ConstructorError):
                cy.load("!shout hi", other)
    finally:
        del cy.SafeLoader.yaml_constructors["!shout"]


def test_add_constructor_without_a_loader_skips_the_safe_loader(api):
    cy.add_constructor("!plain-scope", lambda ld, node: "hit")
    try:
        for loader in (cy.Loader, cy.FullLoader, cy.UnsafeLoader):
            assert cy.load("!plain-scope x", loader) == "hit"
        for loader in (cy.SafeLoader, cy.CSafeLoader, cy.CLoader):
            with pytest.raises(cy.ConstructorError):
                cy.load("!plain-scope x", loader)
        assert cy.full_load("!plain-scope x") == "hit"
    finally:
        for loader in (cy.Loader, cy.FullLoader, cy.UnsafeLoader):
            del loader.yaml_constructors["!plain-scope"]


def test_registration_scoping_matches_pyyaml():
    names = [
        "BaseLoader", "SafeLoader", "FullLoader", "UnsafeLoader", "Loader", "CBaseLoader",
        "CSafeLoader", "CFullLoader", "CUnsafeLoader", "CLoader",
    ]  # fmt: skip

    def table(module, tag, register):
        register(module, tag)
        out = {}
        for name in names:
            try:
                out[name] = module.load(f"{tag} v", pyyaml_loader(name, module))
            except module.YAMLError:
                out[name] = "error"
        return out

    def hit(ld, node):
        return "hit"

    cases = {
        "-load": lambda m, t: m.add_constructor(t, hit),
        "-safe": lambda m, t: m.add_constructor(t, hit, Loader=m.SafeLoader),
        "-csafe": lambda m, t: m.add_constructor(t, hit, Loader=pyyaml_loader("CSafeLoader", m)),
        "-full": lambda m, t: m.FullLoader.add_constructor(t, hit),
        "-safe-cls": lambda m, t: m.SafeLoader.add_constructor(t, hit),
        "-safeconstructor": lambda m, t: m.constructor.SafeConstructor.add_constructor(t, hit),
        "-base": lambda m, t: m.BaseLoader.add_constructor(t, hit),
        "-multi": lambda m, t: m.add_multi_constructor(t, lambda ld, s, n: "hit"),
        "-multi-safe": lambda m, t: m.add_multi_constructor(
            t, lambda ld, s, n: "hit", Loader=m.SafeLoader
        ),
    }
    for case, register in cases.items():
        tag = f"!scope{case}"
        ours = table(cy, tag, register)
        theirs = table(yaml, tag, register)
        assert ours == theirs, (case, ours, theirs)


def test_base_loader_takes_constructors_for_tags_and_reads_the_rest_as_text(api):
    class Mine(cy.BaseLoader):
        pass

    Mine.add_constructor("!n", lambda ld, node: len(ld.construct_scalar(node)))
    assert cy.load("a: !n abc\nb: 12\nc: !!int 3\n", Mine) == {"a": 3, "b": "12", "c": "3"}


# ---- what a callback is given ------------------------------------------------------------------


def test_nodes_carry_tag_value_style_and_marks(api):
    seen = []

    def grab(loader, node):
        seen.append(node)
        return None

    class Mine(cy.SafeLoader):
        pass

    for tag in ("!s", "!q", "!m"):
        Mine.add_constructor(tag, grab)
    cy.load("a: !s 'x y'\nb: !q [1, 2]\nc: !m\n  k: v\n", Mine)
    s, q, m = seen
    assert isinstance(s, cy.ScalarNode) and (s.tag, s.value, s.style) == ("!s", "x y", "'")
    assert (s.start_mark.line, s.start_mark.column) == (0, 3)
    assert (s.end_mark.line, s.end_mark.column) == (0, 11)
    assert isinstance(q, cy.SequenceNode) and q.flow_style is True
    assert [n.value for n in q.value] == ["1", "2"]
    assert [n.tag for n in q.value] == ["tag:yaml.org,2002:int"] * 2
    assert isinstance(m, cy.MappingNode) and m.flow_style is False
    ((key, value),) = m.value
    assert (key.value, value.value, key.tag) == ("k", "v", "tag:yaml.org,2002:str")
    assert str(q.start_mark).strip() == 'in "<unicode string>", line 2, column 4'
    assert m.start_mark.name == "<unicode string>"


def test_compose_returns_the_node_tree(api):
    node = cy.compose("a: [1, x]\n", cy.SafeLoader)
    assert isinstance(node, cy.MappingNode)
    ((k, v),) = node.value
    assert (k.tag, v.tag) == ("tag:yaml.org,2002:str", "tag:yaml.org,2002:seq")
    assert [list(cy.compose_all("1\n---\n2\n"))[1].value] == ["2"]
    assert cy.compose("") is None


def test_the_loader_knows_its_stream_and_name(api, tmp_path):
    names = []

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!here", lambda ld, n: names.append((ld.name, ld.stream)) or 1)
    path = tmp_path / "f.yml"
    path.write_text("a: !here x\n")
    with open(path) as handle:
        cy.load(handle, Mine)
        assert names[-1][0] == str(path) and names[-1][1] is handle
    cy.load("a: !here x", Mine)
    assert names[-1][0] == "<unicode string>"
    cy.load(b"a: !here x", Mine)
    assert names[-1][0] == "<byte string>"


def test_construct_helpers_check_the_kind_of_node(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!seq", lambda ld, n: ld.construct_scalar(n))
    Mine.add_constructor("!map", lambda ld, n: ld.construct_sequence(n))
    Mine.add_constructor("!str", lambda ld, n: ld.construct_mapping(n))
    for text in ("!seq [a]", "!map {a: b}", "!str x"):
        with pytest.raises(cy.ConstructorError) as err:
            cy.load(text, Mine)
        assert "expected a" in err.value.problem and err.value.problem_mark.line == 0


def test_construct_object_and_pairs_and_flatten(api):
    class Mine(cy.SafeLoader):
        pass

    def pairs(ld, node):
        return ld.construct_pairs(node)

    def each(ld, node):
        return [(ld.construct_object(k), ld.construct_object(v)) for k, v in node.value]

    Mine.add_constructor("!pairs", pairs)
    Mine.add_constructor("!each", each)
    assert cy.load("!pairs {a: 1, a: 2, 3: x}", Mine) == [("a", 1), ("a", 2), (3, "x")]
    assert cy.load("!each {a: [1, yes], b: 2001-01-01}", Mine)[0] == ("a", [1, True])


# ---- generators, identity, errors --------------------------------------------------------------


def test_a_generator_constructor_fills_in_its_object(api):
    class Point:
        pass

    def point(loader, node):
        data = Point()
        yield data
        data.__dict__.update(loader.construct_mapping(node))

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!point", point)
    got = cy.load("p: !point {x: 1, y: [2, 3]}", Mine)["p"]
    assert got.__dict__ == {"x": 1, "y": [2, 3]}


def test_an_alias_to_a_built_node_is_the_same_object(api):
    class Mine(cy.SafeLoader):
        pass

    made = []
    Mine.add_constructor("!obj", lambda ld, n: made.append(object()) or made[-1])
    got = cy.load("a: &x !obj 1\nb: *x\nc: [*x, *x]\n", Mine)
    assert len(made) == 1
    assert got["a"] is got["b"] is got["c"][0] is got["c"][1]


def test_callback_exceptions_pass_through_unchanged(api):
    class Boom(Exception):
        pass

    def explode(loader, node):
        raise Boom("from the callback")

    def key_error(loader, node):
        return {}["missing"]

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!boom", explode)
    Mine.add_constructor("!key", key_error)
    with pytest.raises(Boom, match="from the callback"):
        cy.load("a: [1, !boom x]", Mine)
    with pytest.raises(KeyError):
        cy.load("!key x", Mine)


def test_a_callback_raising_a_constructor_error_keeps_the_mark(api):
    def refuse(loader, node):
        raise cy.ConstructorError(None, None, "no way", node.start_mark)

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!no", refuse)
    with pytest.raises(cy.ConstructorError) as err:
        cy.load("a: 1\nb: !no x\n", Mine)
    assert (err.value.problem, err.value.problem_mark.line, err.value.problem_mark.column) == (
        "no way", 1, 3,
    )  # fmt: skip


def test_a_recursive_request_is_an_error_not_a_loop(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!again", lambda ld, node: ld.construct_object(node))
    with pytest.raises(cy.ConstructorError, match="recursive"):
        cy.load("!again x", Mine)


def test_python_tags_stay_refused_whatever_is_registered(api):
    with pytest.raises(cy.ConstructorError, match="Python code"):
        cy.load("!!python/object/apply:os.getcwd []", cy.UnsafeLoader)
    cy.UnsafeLoader.add_constructor("!other", lambda ld, n: 1)
    try:
        with pytest.raises(cy.ConstructorError, match="Python code"):
            cy.load("[!other x, !!python/name:os.system]", cy.UnsafeLoader)
    finally:
        del cy.UnsafeLoader.yaml_constructors["!other"]

    # A tag the program registered itself is its own business.
    class Mine(cy.FullLoader):
        pass

    Mine.add_constructor(
        "tag:yaml.org,2002:python/tuple", lambda ld, n: tuple(ld.construct_sequence(n))
    )
    assert cy.load("!!python/tuple [1, 2]", Mine) == (1, 2)


# ---- the idioms real code uses -----------------------------------------------------------------


def test_include_constructor_reads_files_relative_to_the_including_file(api, tmp_path):
    import os

    class IncludeLoader(cy.SafeLoader):
        def __init__(self, stream):
            self._root = os.path.dirname(stream.name)
            super().__init__(stream)

    def include(loader, node):
        path = os.path.join(loader._root, loader.construct_scalar(node))
        with open(path) as handle:
            return cy.load(handle, IncludeLoader)

    IncludeLoader.add_constructor("!include", include)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "leaf.yml").write_text("leaf: [1, 2]\n")
    (tmp_path / "sub" / "mid.yml").write_text("mid: !include leaf.yml\nv: yes\n")
    (tmp_path / "main.yml").write_text("top: !include sub/mid.yml\nother: 1\n")
    with open(tmp_path / "main.yml") as handle:
        got = cy.load(handle, IncludeLoader)
    assert got == {"top": {"mid": {"leaf": [1, 2]}, "v": True}, "other": 1}


def test_cloudformation_tags_through_a_multi_constructor(api):
    def construct(loader, suffix, node):
        if isinstance(node, cy.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, cy.SequenceNode):
            value = loader.construct_sequence(node)
        else:
            value = loader.construct_mapping(node)
        name = suffix if suffix == "Ref" else f"Fn::{suffix}"
        return {name: value}

    class Template(cy.SafeLoader):
        pass

    Template.add_multi_constructor("!", construct)
    text = (
        "Resources:\n  Bucket:\n    Properties:\n      Name: !Sub '${Env}-bucket'\n"
        "      Arn: !GetAtt Bucket.Arn\n      Id: !Ref Other\n"
        "      Joined: !Join ['-', [!Ref A, b]]\n      Cond: !If {c: 1}\n"
    )
    props = cy.load(text, Template)["Resources"]["Bucket"]["Properties"]
    assert props == {
        "Name": {"Fn::Sub": "${Env}-bucket"},
        "Arn": {"Fn::GetAtt": "Bucket.Arn"},
        "Id": {"Ref": "Other"},
        "Joined": {"Fn::Join": ["-", [{"Ref": "A"}, "b"]]},
        "Cond": {"Fn::If": {"c": 1}},
    }


def test_environment_variable_resolver_and_constructor(api, monkeypatch):
    monkeypatch.setenv("ONEYAML_TEST_VAR", "found")
    pattern = re.compile(r"^.*\$\{([A-Za-z_]+)\}.*$")

    def env(loader, node):
        value = loader.construct_scalar(node)
        return re.sub(r"\$\{([A-Za-z_]+)\}", lambda m: os.environ.get(m[1], ""), value)

    class Config(cy.SafeLoader):
        pass

    Config.add_implicit_resolver("!env", pattern, None)
    Config.add_constructor("!env", env)
    got = cy.load("a: x-${ONEYAML_TEST_VAR}-y\nb: plain\nc: 12\nd: '${ONEYAML_TEST_VAR}'\n", Config)
    # A quoted scalar is not resolved implicitly, so it stays text.
    assert got == {"a": "x-found-y", "b": "plain", "c": 12, "d": "${ONEYAML_TEST_VAR}"}


def test_the_old_bool_resolver_removal_idiom(api):
    class Strict(cy.SafeLoader):
        pass

    Strict.yaml_implicit_resolvers = {
        key: [(tag, rx) for tag, rx in value if tag != "tag:yaml.org,2002:bool"]
        for key, value in cy.SafeLoader.yaml_implicit_resolvers.items()
    }
    assert cy.load("[yes, no, on, true, 1]", Strict) == ["yes", "no", "on", "true", 1]
    assert cy.safe_load("[yes, no]") == [True, False]


def test_duplicate_key_check_by_overriding_construct_mapping(api):
    class Unique(cy.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise cy.ConstructorError(None, None, f"duplicate {key!r}", key_node.start_mark)
                seen.add(key)
            return super().construct_mapping(node, deep)

    assert cy.load("a: {b: 1}\nc: [{d: 2}]\n", Unique) == {"a": {"b": 1}, "c": [{"d": 2}]}
    with pytest.raises(cy.ConstructorError, match="duplicate 'k'"):
        cy.load("a: 1\nb:\n  k: 1\n  k: 2\n", Unique)


def test_ordered_dict_loader_idiom(api):
    import collections

    class Ordered(cy.SafeLoader):
        pass

    def ordered(loader, node):
        loader.flatten_mapping(node)
        return collections.OrderedDict(loader.construct_pairs(node))

    Ordered.add_constructor("tag:yaml.org,2002:map", ordered)
    got = cy.load("b: 1\na: {y: 1, x: 2}\n", Ordered)
    assert type(got) is collections.OrderedDict and list(got) == ["b", "a"]
    assert type(got["a"]) is collections.OrderedDict and list(got["a"]) == ["y", "x"]


# ---- YAMLObject --------------------------------------------------------------------------------


def test_yaml_object_registers_itself_and_builds_instances(api):
    class Monster(cy.YAMLObject):
        yaml_loader = cy.SafeLoader
        yaml_tag = "!Monster"

        def __init__(self, name, hp):
            self.name, self.hp = name, hp

    try:
        got = cy.safe_load(
            "- !Monster {name: Dragon, hp: [1, 2]}\n- !Monster\n  name: Orc\n  hp: 3\n"
        )
        assert [type(m) for m in got] == [Monster, Monster]
        assert got[0].__dict__ == {"name": "Dragon", "hp": [1, 2]}
        assert got[1].name == "Orc"
    finally:
        del cy.SafeLoader.yaml_constructors["!Monster"]


def test_yaml_object_defaults_to_the_unsafe_loaders_and_uses_from_yaml_and_setstate(api):
    class Boss(cy.YAMLObject):
        yaml_tag = "!Boss"

        def __setstate__(self, state):
            self.state = state

    class Custom(cy.YAMLObject):
        yaml_tag = "!Custom"

        @classmethod
        def from_yaml(cls, loader, node):
            return ("custom", loader.construct_scalar(node))

    try:
        assert cy.load("!Boss {a: 1}", cy.Loader).state == {"a": 1}
        assert cy.full_load("!Custom z") == ("custom", "z")
        assert cy.unsafe_load("!Custom z") == ("custom", "z")
        with pytest.raises(cy.ConstructorError):
            cy.safe_load("!Custom z")
    finally:
        for tag in ("!Boss", "!Custom"):
            for loader in (cy.Loader, cy.FullLoader, cy.UnsafeLoader):
                del loader.yaml_constructors[tag]


def test_yaml_object_without_a_tag_registers_nothing(api):
    before = dict(cy.Loader.yaml_constructors)

    class Quiet(cy.YAMLObject):
        pass

    assert cy.Loader.yaml_constructors == before


# ---- paths -------------------------------------------------------------------------------------


def test_path_resolvers_tag_the_node_at_a_path(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_path_resolver("!secret", ["db", "password"], str)
    Mine.add_path_resolver("!first", ["hosts", 0], str)
    Mine.add_constructor("!secret", lambda ld, n: "***")
    Mine.add_constructor("!first", lambda ld, n: "FIRST:" + ld.construct_scalar(n))
    text = "db: {user: u, password: p}\nhosts: [a, b]\nother: {password: p}\n"
    assert cy.load(text, Mine) == {
        "db": {"user": "u", "password": "***"},
        "hosts": ["FIRST:a", "b"],
        "other": {"password": "p"},
    }
    with pytest.raises(cy.ResolverError):
        Mine.add_path_resolver("!x", [object()])


# ---- backend and node paths --------------------------------------------------------------------


def test_a_loader_with_unused_constructors_reads_through_the_backend(api, monkeypatch):
    from oneyaml.compat import _engine

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!s", lambda ld, n: "S")
    calls = []
    real = _engine.compose_documents
    monkeypatch.setattr(_engine, "compose_documents", lambda *a: calls.append(1) or real(*a))
    assert cy.load("a: [1, yes]\n", Mine) == {"a": [1, True]}
    assert not calls
    assert cy.load("a: !s x\n", Mine) == {"a": "S"}
    # The native backend builds the document and composes only the node a callback takes over;
    # the pure one composes the document from the events it already parsed.
    assert bool(calls) == (api.name == "pure")
    # A syntax error is the backend's, and is reported without building nodes.
    calls.clear()
    with pytest.raises(cy.ParserError):
        cy.load("a: [\n", Mine)
    assert not calls


@pytest.mark.parametrize(
    "text",
    [
        "&m <<: {a: 1}\nx: *m\n",
        "&m <<: {a: 1}\n? [*m]\n: 1\n",
        "&m <<: {a: 1}\nb: [*m]\n",
        "a: !!int abc\n",
        "a: !!merge <<\n",
    ],
)
def test_standard_errors_hold_whichever_way_the_loader_reads(api, text):
    class Unused(cy.SafeLoader):
        pass

    Unused.add_constructor("!never", lambda ld, n: 1)
    nodes = type("Nodes", (cy.SafeLoader,), {})
    nodes.add_implicit_resolver("!never", re.compile("^never$"), ["n"])
    for loader in (Unused, nodes):
        with pytest.raises(cy.ConstructorError):
            cy.load(text, loader)


def test_the_node_path_reads_the_corpus_as_the_pure_backend_does(monkeypatch):
    # The same loader through nodes instead of the backend: nothing should change. Nodes are
    # always composed from the pure parser's events, so the pure backend is the reference.
    from oneyaml import _pure

    monkeypatch.setattr(_api, "_impl", _pure)
    texts = _corpus_texts()
    assert len(texts) > 300
    reference = {}
    for text in texts:
        reference[text] = outcome(lambda t: list(cy.load_all(t, cy.SafeLoader)), text, cy.YAMLError)
    monkeypatch.setattr(cy, "_mode", lambda cls: 2)
    for text in texts:
        ref = reference[text]
        got = outcome(lambda t: list(cy.load_all(t, cy.SafeLoader)), text, cy.YAMLError)
        assert (ref[0] == got[0] == "ok" and same(ref[1], got[1])) or (
            ref[0] != "ok" and got[0] != "ok"
        ), (text, ref, got)


def test_limits_hold_when_callbacks_build_the_values(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!q", lambda ld, n: ld.construct_sequence(n, deep=True))
    bomb = "a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"a{i + 1}: &a{i + 1} !q [{', '.join([f'*a{i}'] * 10)}]\n" for i in range(12)
    )
    with pytest.raises(cy.ConstructorError) as err:
        cy.load(bomb, Mine)
    assert err.value.category == "limit"
    with pytest.raises(cy.ConstructorError) as err:
        cy.load("!q " + "[" * 20000 + "]" * 20000, Mine)
    assert err.value.category == "limit"


def test_deep_input_never_recurses(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!m", lambda ld, n: ld.construct_mapping(n, deep=True))
    deep = "{a: " * 9000 + "1" + "}" * 9000
    got = cy.load("!m " + deep, Mine)
    depth = 0
    while isinstance(got, dict):
        got = got["a"]
        depth += 1
    assert depth == 9000

    class Careful(cy.SafeLoader):
        def construct_mapping(self, node, deep=False):
            return super().construct_mapping(node, deep)

    with pytest.raises(cy.ConstructorError) as err:
        cy.load(deep, Careful)
    assert err.value.category == "limit"


def test_a_user_recursion_error_becomes_a_limit_error(api):
    class Mine(cy.SafeLoader):
        pass

    def walk(node):
        return walk(node)

    Mine.add_constructor("!walk", lambda ld, n: walk(n))
    with pytest.raises(cy.ConstructorError) as err:
        cy.load("!walk x", Mine)
    assert err.value.category == "limit"


def test_multi_document_streams_build_one_document_at_a_time(api):
    order = []

    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!t", lambda ld, n: order.append(ld.construct_scalar(n)) or 1)
    gen = cy.load_all("!t a\n---\n!t b\n---\n!t c\n", Mine)
    assert next(gen) == 1 and order == ["a"]
    assert next(gen) == 1 and order == ["a", "b"]
    assert list(gen) == [1] and order == ["a", "b", "c"]
    with pytest.raises(cy.ComposerError):
        cy.load("!t a\n---\n!t b\n", Mine)
    assert order == ["a", "b", "c"]


def test_merge_keys_work_with_callbacks_and_on_callback_built_sources(api):
    class Mine(cy.SafeLoader):
        pass

    Mine.add_constructor("!wrap", lambda ld, n: ("wrapped", ld.construct_mapping(n, deep=True)))
    text = "base: &b !wrap {x: 1, y: 2}\nuse:\n  <<: *b\n  y: 3\n  z: !wrap {q: 1}\n"
    got = cy.load(text, Mine)
    assert got["base"] == ("wrapped", {"x": 1, "y": 2})
    assert got["use"] == {"x": 1, "y": 3, "z": ("wrapped", {"q": 1})}


# ---- the comparison ----------------------------------------------------------------------------

DOCS = tagged_documents(400)


@pytest.mark.parametrize("program", [*PROGRAMS, *SUBCLASS_PROGRAMS])
def test_generated_documents_match_pyyaml(api, program):
    bad = unexplained(program, answers(program, DOCS))
    assert not bad, bad[:3]


def has_events(value):
    """Whether a list of event tuples (what stands in for a node a callback has not built yet)
    is left anywhere in the value."""
    stack, seen = [value], set()
    while stack:
        x = stack.pop()
        if id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, list) and x and isinstance(x[0], tuple) and type(x[0][0]) is int:
            return True
        if isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, (list, tuple, set, frozenset)):
            stack.extend(x)
        elif hasattr(x, "__dict__") and not isinstance(x, type):
            stack.extend(vars(x).values())
    return False


@pytest.mark.parametrize("program", [*PROGRAMS, *SUBCLASS_PROGRAMS])
def test_generated_documents_match_pyyaml_through_load(api, program):
    """A single document goes through the load that builds only the tagged nodes by callback."""
    bad = unexplained(program, answers(program, DOCS, single=True))
    assert not bad, bad[:3]
    loader = build(cy, program)
    for text in DOCS:
        with contextlib.suppress(cy.YAMLError):
            assert not has_events(cy.load(text, loader)), text


# ---- the whole corpus --------------------------------------------------------------------------


def _agreeing(texts):
    """The texts that PyYAML and the plain library read the same way, compose included: any
    difference left after a registration is then the registration's."""
    keep = []
    for text in texts:
        c_ref = outcome(lambda t: list(yaml.compose_all(t, yaml.SafeLoader)), text, yaml.YAMLError)
        c_got = outcome(lambda t: list(cy.compose_all(t, cy.SafeLoader)), text, cy.YAMLError)
        if (c_ref[0] == "ok") != (c_got[0] == "ok"):
            continue
        ref = outcome(lambda t: list(yaml.load_all(t, yaml.SafeLoader)), text, yaml.YAMLError)
        got = outcome(lambda t: list(cy.load_all(t, cy.SafeLoader)), text, cy.YAMLError)
        if (ref[0] == got[0] == "ok" and same(ref[1], got[1])) or (
            ref[0] != "ok" and got[0] == "err"
        ):
            keep.append(text)
    return keep


def _corpus_texts():
    from pyyaml_corpus import documents, scalars

    out = []
    if KIT:
        root = pathlib.Path(KIT) / "suites"
        paths = sorted(root.glob("yaml-test-suite/data-*/**/in.yaml"))
        paths += [
            p
            for p in sorted(root.glob("own/cases/*/*/input.yaml"))
            if "/safety/" not in p.as_posix()
        ]
        for p in paths:
            with contextlib.suppress(UnicodeDecodeError):
                out.append(p.read_text(encoding="utf-8"))
    for s in scalars(400):
        out.extend(documents(s))
    return out


WHOLE_CORPUS = [
    "resolvers", "overrides", "no-bool", "catch-unknown", "tag-as-std", "paths", "everything",
    "unique-keys", "multi-catch-all",
]  # fmt: skip


@pytest.mark.slow
@pytest.mark.parametrize("program", WHOLE_CORPUS)
def test_corpus_matches_pyyaml_under_registrations(api, program):
    texts = _agreeing(_corpus_texts())
    assert len(texts) > 500
    bad = unexplained(program, answers(program, texts))
    assert not bad, bad[:3]
