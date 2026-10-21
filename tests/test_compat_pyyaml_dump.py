"""The PyYAML compatibility module, dump side: the same bytes as PyYAML, on both backends.

Comparisons run against PyYAML itself (a development dependency). Where this module writes
something else, the difference is named in ``test_intended_differences_*``: each one is a case
where PyYAML's own text does not read back as the value it was given, or is read differently by
a YAML 1.2 reader, and each is checked to be both present in PyYAML and gone here.
"""

from __future__ import annotations

import collections
import datetime
import importlib
import io
import re

import pytest

import oneyaml.compat.pyyaml as cy
from oneyaml import _api
from pyyaml_dump_corpus import case, has_tuple_key, normal

yaml = pytest.importorskip("yaml")

SEEDS = range(500)


def outcome(mod, dumper_name, value, kw, many=False):
    dumper = getattr(mod, dumper_name)
    try:
        if many:
            return "ok", mod.dump_all(value, Dumper=dumper, **kw)
        return "ok", mod.dump(value, Dumper=dumper, **kw)
    except Exception as exc:
        return "err", type(exc).__name__


def documents(value, many):
    return value if many else [value]


def reads_back(mod, text, value, many):
    try:
        return normal(list(mod.safe_load_all(text))) == normal(documents(value, many))
    except Exception:
        return False


def has_separator(x) -> bool:
    work = [x]
    while work:
        y = work.pop()
        if isinstance(y, str) and any(c in y for c in "\x85\u2028\u2029"):
            return True
        if isinstance(y, dict):
            work.extend(y.keys())
            work.extend(y.values())
        elif isinstance(y, (list, tuple, set)):
            work.extend(y)
    return False


def explain(value, kw, many, reference):
    """The named difference that accounts for ours not being PyYAML's text, or None."""
    docs = documents(value, many)
    broken = not (
        reads_back(yaml, reference, value, many) and reads_back(cy, reference, value, many)
    )
    if kw.get("allow_unicode") and broken and any(has_separator(d) for d in docs):
        return "NEL, LS and PS are escaped under allow_unicode"
    if broken and re.search(r"^\s*\\\\$", reference, re.M):
        return "a fold in a double-quoted scalar does not repeat the space marker"
    if (kw.get("version") or kw.get("tags")) and many and len(value) > 1:
        return "a directive after the first document follows a document end marker"
    if kw.get("default_style") in ("|", ">") and any(
        isinstance(d, str) and d[:1] in " \n" for d in docs
    ):
        return "a root block scalar with an indentation indicator is quoted"
    return None


# ---- surface -----------------------------------------------------------------------------------


def test_names_exist():
    for name in (
        "dump", "dump_all", "safe_dump", "safe_dump_all", "add_representer",
        "add_multi_representer", "Dumper", "SafeDumper", "BaseDumper", "CDumper", "CSafeDumper",
        "CBaseDumper", "Representer", "SafeRepresenter", "BaseRepresenter", "Emitter",
        "Serializer", "EmitterError", "RepresenterError", "SerializerError", "YAMLObject",
    ):  # fmt: skip
        assert name in cy.__all__ and hasattr(cy, name), name
    for cls in (cy.EmitterError, cy.RepresenterError, cy.SerializerError):
        assert issubclass(cls, cy.YAMLError)
    assert cy.safe_dump({"a": 1}) == "a: 1\n"


def test_dumpers_have_their_own_tables():
    # Registering on one dumper reaches no other, as in PyYAML.
    class Thing:
        pass

    cy.add_representer(Thing, lambda d, o: d.represent_scalar("!thing", "t"), Dumper=cy.SafeDumper)
    assert cy.safe_dump(Thing()) == "!thing 't'\n"
    with pytest.raises(cy.RepresenterError):
        cy.dump(Thing(), Dumper=cy.CSafeDumper)
    for cls in (cy.Dumper, cy.CDumper, cy.BaseDumper):
        assert Thing not in cls.yaml_representers
    assert cy.SafeDumper.yaml_representers is not cy.SafeRepresenter.yaml_representers
    assert cy.Dumper.yaml_representers is not cy.SafeDumper.yaml_representers


# ---- the comparison with PyYAML ----------------------------------------------------------------


def _compare(seeds, api, counts):
    for seed in seeds:
        value, kw, many = case(seed)
        ref = outcome(yaml, "SafeDumper", value, kw, many)
        got = outcome(cy, "SafeDumper", value, kw, many)
        if ref == got:
            counts["same"] += 1
            continue
        assert ref[0] == "ok" and got[0] == "ok", (seed, ref, got)
        rule = explain(value, kw, many, ref[1])
        assert rule is not None, (seed, kw)
        counts[rule] += 1
        if not has_tuple_key(value):
            for mod in (yaml, cy):
                assert reads_back(mod, got[1], value, many), (seed, rule, mod.__name__)


def test_generated_values_dump_like_pyyaml(api):
    counts = collections.Counter()
    _compare(SEEDS, api, counts)
    assert counts["same"] > 400


@pytest.mark.slow
def test_generated_values_dump_like_pyyaml_widely(api):
    counts = collections.Counter()
    _compare(range(500, 12500), api, counts)
    assert counts["same"] > 11000


def test_every_output_reads_back(api):
    checked = 0
    for seed in SEEDS:
        value, kw, many = case(seed, tuples=False)
        got = outcome(cy, "SafeDumper", value, kw, many)
        if got[0] != "ok":
            continue
        checked += 1
        for mod in (yaml, cy):
            assert reads_back(mod, got[1], value, many), (seed, kw, mod.__name__)
    assert checked > 400


def test_backends_write_the_same_bytes(monkeypatch):
    try:
        native = importlib.import_module("oneyaml._native")
    except ImportError:
        pytest.skip("no native module")
    pure = importlib.import_module("oneyaml._pure")
    for dumper in ("SafeDumper", "Dumper"):
        for seed in range(400):
            value, kw, many = case(seed)
            monkeypatch.setattr(_api, "_impl", native)
            first = outcome(cy, dumper, value, kw, many)
            monkeypatch.setattr(_api, "_impl", pure)
            second = outcome(cy, dumper, value, kw, many)
            assert first == second, (dumper, seed, kw)


def test_the_plain_data_writer_agrees_with_the_resolvers(monkeypatch):
    # Which strings are quoted rests on the implicit resolvers; the native writer has its own
    # copy of their rules, checked here against the regular expressions.
    try:
        native = importlib.import_module("oneyaml._native")
    except ImportError:
        pytest.skip("no native module")
    pure = importlib.import_module("oneyaml._pure")
    import random

    r = random.Random(7)
    alphabet = "0123456789_-+.:eExXbBoOaAfF tTzZnNyY<=~"
    words = [
        "".join(r.choice(alphabet) for _ in range(r.randint(0, 9))) for _ in range(6000)
    ] + [
        "yes", "Yes", "on", "OFF", "null", "~", "<<", "=", "0", "-0", "+1", "1_0", "0b1", "0b_",
        "0x_", "0o7", "017", "08", "1:30", "1:60", "1:5:5", "190:20:30.15", ".5", "-.5", "1.",
        "1e3", "1.0e3", "1.0e+3", ".inf", "-.inf", "+.inf", ".nan", "-.nan", "2001-12-14",
        "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43 Z", "2001-1-1", "2001-1-1 1:00:00",
    ]  # fmt: skip
    for impl in (native, pure):
        monkeypatch.setattr(_api, "_impl", impl)
        texts = [cy.safe_dump(w) for w in words]
        if impl is native:
            first = texts
        else:
            assert first == texts
    # And every string the resolvers read as something else is quoted by the writer.
    for w, text in zip(words, first, strict=True):
        back = yaml.safe_load(text)
        assert back == w or (w == "" and back == ""), (w, text)


# ---- the named differences ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "kw", "rule"),
    [
        ("a\x85b", {"allow_unicode": True}, "NEL, LS and PS are escaped under allow_unicode"),
        ({"k": ["x\u2028y"]}, {"allow_unicode": True}, "NEL, LS and PS are escaped under allow_unicode"),  # noqa: E501
        (
            {"n": {"n": {"k": "a\x85 b"}}},
            {"width": 5},
            "a fold in a double-quoted scalar does not repeat the space marker",
        ),
        (
            [{"k": 1}, {"k": 2}],
            {"version": (1, 1), "many": True},
            "a directive after the first document follows a document end marker",
        ),
        (
            "\n x",
            {"default_style": ">"},
            "a root block scalar with an indentation indicator is quoted",
        ),
    ],
)  # fmt: skip
def test_intended_differences_are_real_and_stay_listed(api, value, kw, rule):
    kw = dict(kw)  # the parameter is shared by every backend
    many = kw.pop("many", False)
    dump = cy.dump_all if many else cy.dump
    ref = (yaml.dump_all if many else yaml.dump)(value, Dumper=yaml.SafeDumper, **kw)
    got = dump(value, Dumper=cy.SafeDumper, **kw)
    assert ref != got  # a difference that went away must leave this list
    assert explain(value, kw, many, ref) == rule
    assert reads_back(yaml, got, value, many)
    assert reads_back(cy, got, value, many)


def test_a_deeply_folded_double_quoted_scalar_reads_back(api):
    for text in ("a\x85 b", "x\t y", "\xe9 \xe9 b", "a\x85 \x85 \x85 b c"):
        for depth in range(5):
            value = {"k": text}
            for _ in range(depth):
                value = {"n": value}
            out = cy.safe_dump(value, width=5)
            assert yaml.safe_load(out) == value
            assert cy.safe_load(out) == value


# ---- keywords ----------------------------------------------------------------------------------

SAMPLE = {
    "name": "x", "list": [1, 2, {"deep": [3, "a b", None]}], "text": "line one\nline two\n",
    "long": "word " * 40, "uni": "caf\xe9 \u65e5\u672c", "when": datetime.date(2020, 1, 2),
    "flags": [True, "yes", "on", "1.5", 1.5, float("inf")], "": "empty key", "t": (1, 2),
}  # fmt: skip


@pytest.mark.parametrize(
    "kw",
    [
        {}, {"sort_keys": False}, {"default_flow_style": None}, {"default_flow_style": True},
        {"indent": 4}, {"indent": 9}, {"indent": 1}, {"width": 30}, {"width": 200},
        {"allow_unicode": True}, {"explicit_start": True}, {"explicit_end": True},
        {"canonical": True}, {"default_style": '"'}, {"default_style": "'"},
        {"default_style": "|"}, {"default_style": ">"}, {"line_break": "\r\n"},
        {"version": (1, 1)}, {"tags": {"!e!": "tag:example.com,2000:"}},
        {"indent": 3, "width": 40, "allow_unicode": True, "explicit_start": True},
    ],
)  # fmt: skip
def test_keywords_match_pyyaml(api, kw):
    for dumper in ("SafeDumper", "Dumper"):
        sample = SAMPLE if dumper == "Dumper" else {**SAMPLE, "t": [1, 2]}
        assert outcome(cy, dumper, sample, kw) == outcome(yaml, dumper, sample, kw), (dumper, kw)


def test_stream_and_encoding(api):
    buf = io.StringIO()
    assert cy.safe_dump({"a": [1, 2]}, buf) is None
    assert buf.getvalue() == yaml.safe_dump({"a": [1, 2]})
    for encoding in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
        got = cy.safe_dump({"k": "caf\xe9"}, encoding=encoding, allow_unicode=True)
        assert got == yaml.safe_dump({"k": "caf\xe9"}, encoding=encoding, allow_unicode=True)
        assert isinstance(got, bytes)
    # PyYAML repeats the byte order mark of utf-16 with every piece it writes.
    got = cy.safe_dump({"k": [1, 2]}, encoding="utf-16")
    assert yaml.safe_load(got) == {"k": [1, 2]} and got.count(b"\xff\xfe") == 1
    raw = io.BytesIO()
    cy.safe_dump_all([1, "two", [3]], raw, encoding="utf-8")
    assert raw.getvalue() == yaml.safe_dump_all([1, "two", [3]], encoding="utf-8")
    with pytest.raises(ValueError, match="encoding"):
        cy.safe_dump(1, encoding="no-such-codec")


def test_documents_and_open_ended_roots(api):
    for docs in ([1, 2, 3], ["a", "b"], ["a", [1], {"k": "v"}, "z"], [], [None], ["x", ""]):
        for kw in ({}, {"explicit_start": True}, {"explicit_end": True}):
            assert cy.safe_dump_all(docs, **kw) == yaml.safe_dump_all(docs, **kw), (docs, kw)
    assert cy.safe_dump("foo") == "foo\n...\n"
    assert cy.safe_dump(["foo"]) == "- foo\n"


def test_key_length_limit_counts_the_tag(api):
    for n in (120, 121, 122, 123, 124, 126, 127, 128):
        value = {"k" * n: 1, 5: "x"}
        assert cy.safe_dump(value) == yaml.safe_dump(value), n
        value = {"k" * n: {"a": 1}}
        assert cy.safe_dump(value) == yaml.safe_dump(value), n
    assert cy.safe_dump({"a\nb": 1, "": 2}) == yaml.safe_dump({"a\nb": 1, "": 2})


def test_mixed_key_types_keep_their_order_when_unsortable(api):
    value = {"b": 1, 2: "x", None: 3, "a": 4, 1.5: 5, True: 6}
    assert cy.safe_dump(value) == yaml.safe_dump(value)
    assert cy.safe_dump({3: 1, 1: 2, 2: 3}) == yaml.safe_dump({3: 1, 1: 2, 2: 3})
    assert cy.safe_dump({"b": 1, "a": 2}, sort_keys=False) == "b: 1\na: 2\n"


def test_floats_ints_and_special_values(api):
    value = [0.0, -0.0, 1e17, 1e-7, 5e-324, 1.7976931348623157e308, float("nan"), float("-inf"),
             10**40, -(10**25), 0.1, 100.0, 2.5e22, 123456789.123456789, True, None]  # fmt: skip
    assert cy.safe_dump(value) == yaml.safe_dump(value)
    assert cy.safe_dump(value, default_flow_style=None) == yaml.safe_dump(
        value, default_flow_style=None
    )


# ---- sharing, recursion and depth --------------------------------------------------------------


def test_shared_containers_get_anchors(api):
    a = [1, 2]
    b = {"k": a, "again": a}
    value = {"x": b, "y": b, "z": [a, "s"], "e": [], "f": []}
    assert cy.safe_dump(value) == yaml.safe_dump(value)
    empty = []
    assert cy.safe_dump([empty, empty]) == yaml.safe_dump([empty, empty])
    # Strings and numbers are never anchored, however often they are the same object.
    s = "text"
    assert cy.safe_dump([s, s, 5, 5]) == "- text\n- text\n- 5\n- 5\n"


def test_recursive_values(api):
    a = []
    a.append(a)
    d = {}
    d["me"] = d
    d["l"] = [a]
    for value in (a, d, [d, a]):
        assert cy.dump(value, Dumper=cy.Dumper) == yaml.dump(value, Dumper=yaml.Dumper)


def test_ignore_aliases_override_turns_anchors_off(api):
    class Plain(cy.SafeDumper):
        def ignore_aliases(self, data):
            return True

    class PlainRef(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    shared = [1, 2]
    assert cy.dump([shared, shared], Dumper=Plain) == yaml.dump([shared, shared], Dumper=PlainRef)
    assert cy.dump([shared, shared], Dumper=Plain) == "- - 1\n  - 2\n- - 1\n  - 2\n"


def test_very_deep_values_do_not_recurse(api):
    depth = 4000  # the text grows with the square of the depth: keep it small
    value = []
    for _ in range(depth):
        value = [value]
    text = cy.safe_dump(value)
    assert text.startswith("- - - - ") and text.rstrip().endswith("[]")
    # With a shared leaf the writer cannot take the plain-data route: still no recursion.
    leaf = []
    deep = {"leaf": leaf, "again": leaf}
    for _ in range(depth):
        deep = {"n": deep}
    text = cy.safe_dump(deep)
    assert sum(1 for line in text.splitlines() if line.strip() == "n:") == depth
    assert "*id001" in text


def test_representers_calling_each_other_end_in_a_limit_error(api):
    class Node:
        def __init__(self, child):
            self.child = child

    cy.add_representer(
        Node, lambda d, o: d.represent_mapping("!node", {"c": o.child}), Dumper=cy.SafeDumper
    )
    value = None
    for _ in range(400):
        value = Node(value)
    with pytest.raises(cy.RepresenterError) as info:
        cy.safe_dump(value)
    assert info.value.category == "limit"
    shallow = None
    for _ in range(30):
        shallow = Node(shallow)
    assert cy.safe_dump(shallow).count("!node") == 30


# ---- representer programs ----------------------------------------------------------------------


def _program_custom(m):
    class Point:
        def __init__(self, x, y):
            self.x, self.y = x, y

    class Money:
        def __init__(self, cents):
            self.cents = cents

    class Bag:
        def __init__(self, *items):
            self.items = items

    def point(d, o):
        return d.represent_mapping("!point", {"y": o.y, "x": o.x})

    def point_flow(d, o):
        return d.represent_mapping("!point", {"y": o.y, "x": o.x}, flow_style=True)

    def money(d, o):
        return d.represent_scalar("!money", f"{o.cents / 100:.2f} EUR")

    def money_quoted(d, o):
        return d.represent_scalar("!money", f"{o.cents / 100:.2f} EUR", style='"')

    def bag(d, o):
        return d.represent_sequence("!bag", list(o.items))

    def bag_flow(d, o):
        return d.represent_sequence("!bag", list(o.items), flow_style=True)

    class Safe(m.SafeDumper):
        pass

    class Fancy(m.SafeDumper):
        pass

    Safe.add_representer(Point, point)
    Safe.add_representer(Money, money)
    Safe.add_representer(Bag, bag)
    Fancy.add_representer(Point, point_flow)
    Fancy.add_representer(Money, money_quoted)
    Fancy.add_representer(Bag, bag_flow)
    shared = Point(1, [2, 3])
    values = [
        Point(1, 2), Money(1999), Bag(1, "two", Point(3, 4)), [Point(1, 2), Money(5), Bag()],
        {"p": Point("a b", None), "m": Money(0), "b": Bag(Bag(1), Bag(Bag(2)))},
        [shared, shared], {"nested": [{"p": Point(1, {"k": [1, 2]})}]},
    ]  # fmt: skip
    out = []
    for dumper in (Safe, Fancy):
        for v in values:
            for kw in ({}, {"default_flow_style": None}, {"indent": 4}, {"sort_keys": False}):
                out.append(m.dump(v, Dumper=dumper, **kw))
    return out


def _program_multi(m):
    class Base:
        pass

    class Child(Base):
        def __init__(self, n):
            self.n = n

    class Grand(Child):
        pass

    def base(d, o):
        return d.represent_scalar("!base", type(o).__name__)

    def exact(d, o):
        return d.represent_scalar("!exact", "yes")

    class D(m.SafeDumper):
        pass

    D.add_multi_representer(Base, base)
    out = [m.dump([Base(), Child(1), Grand(2)], Dumper=D)]
    D.add_representer(Child, exact)
    out.append(m.dump([Base(), Child(1), Grand(2)], Dumper=D))
    D.add_multi_representer(object, lambda d, o: d.represent_scalar("!any", "x"))
    out.append(m.dump([Base(), object(), 5, "s"], Dumper=D))
    return out


def _program_yaml_object(m):
    class Monster(m.YAMLObject):
        yaml_tag = "!Monster"

        def __init__(self, name, hp):
            self.name, self.hp = name, hp

    class Flow(m.YAMLObject):
        yaml_tag = "!Flow"
        yaml_flow_style = True

        def __init__(self, a):
            self.a = a

    class OnlySafe(m.YAMLObject):
        yaml_tag = "!OnlySafe"
        yaml_dumper = m.SafeDumper

        def __init__(self, a):
            self.a = a

    out = [
        m.dump(Monster("Goblin", 12)),
        m.dump([Monster("a", 1), Flow([1, 2])], default_flow_style=False),
        m.dump({"k": Flow({"z": 1, "a": 2})}),
        m.dump(OnlySafe(3), Dumper=m.SafeDumper),
        m.safe_dump(OnlySafe([1])),
    ]
    try:
        m.safe_dump(Monster("x", 1))
    except Exception as exc:
        out.append(type(exc).__name__)
    return out


def _program_resolvers(m):
    class D(m.SafeDumper):
        pass

    class Version:
        def __init__(self, s):
            self.s = s

    m.add_implicit_resolver("!ver", re.compile(r"^v\d+\.\d+$"), list("v"), Dumper=D)
    m.add_implicit_resolver("!pair", re.compile(r"^\d+-\d+$"), None, Dumper=D)
    D.add_representer(Version, lambda d, o: d.represent_scalar("!ver", o.s))
    values = ["v1.2", "v1", "1-2", ["v3.4", "x", "9-9"], {"v1.0": "1-1", "k": Version("v2.0")}]
    out = [m.dump(v, Dumper=D) for v in values]
    out.append(m.dump(Version("v9.9"), Dumper=D))
    out.append(m.dump("v1.2", Dumper=m.SafeDumper))
    # A path resolver gives the node at a path a tag.
    m.add_path_resolver("!special", ["config", "mode"], str, Dumper=D)
    out.append(m.dump({"config": {"mode": "fast", "other": "x"}, "mode": "y"}, Dumper=D))
    return out


def _program_idioms(m):
    out = []

    class Literal(m.SafeDumper):
        pass

    def str_presenter(d, data):
        if "\n" in data:
            return d.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return d.represent_scalar("tag:yaml.org,2002:str", data)

    Literal.add_representer(str, str_presenter)
    out.append(m.dump({"a": "one\ntwo\n", "b": "plain", "c": ["x\ny", "z"]}, Dumper=Literal))
    out.append(m.dump("multi\nline", Dumper=Literal))

    class EmptyNone(m.SafeDumper):
        pass

    EmptyNone.add_representer(
        type(None), lambda d, v: d.represent_scalar("tag:yaml.org,2002:null", "")
    )
    out.append(m.dump({"a": None, "b": [None, 1]}, Dumper=EmptyNone))

    class Ordered(m.SafeDumper):
        pass

    Ordered.add_representer(collections.OrderedDict, lambda d, data: d.represent_dict(data.items()))
    od = collections.OrderedDict([("z", 1), ("a", {"y": 2, "b": 3})])
    out.append(m.dump(od, Dumper=Ordered))
    out.append(m.dump(od, Dumper=m.Dumper))

    class Indented(m.Dumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    out.append(m.dump({"a": [1, 2, {"b": [3, [4]]}], "c": {"d": ["x"]}}, Dumper=Indented))

    class NoAlias(m.SafeDumper):
        ignore_aliases = lambda self, data: True  # noqa: E731

    s = [1]
    out.append(m.dump({"a": s, "b": s}, Dumper=NoAlias))
    out.append(m.dump({"a": s, "b": s}, Dumper=m.SafeDumper))
    return out


def _program_python_tags(m):
    class Plain:
        def __init__(self):
            self.a = 1
            self.b = [1, 2]

    class Slots:
        __slots__ = ("x",)

        def __init__(self):
            self.x = 1

    values = [
        (1, (2, 3)), {"t": (), "c": 1 + 2j, "d": 3j, "e": 2 - 1j}, collections.OrderedDict(a=1),
        Plain(), [Plain(), Plain], len, collections, set(), {1, 2}, frozenset([3]), Slots(),
        bytearray(b"ab"), datetime.timedelta(days=1), range(3),
    ]  # fmt: skip
    out = []
    for v in values:
        for dumper in (m.Dumper, m.SafeDumper):
            try:
                text = m.dump(v, Dumper=dumper)
                out.append(re.sub(r"0x[0-9a-f]+", "0x", text))
            except Exception as exc:
                out.append(type(exc).__name__)
    return out


@pytest.mark.parametrize(
    "program",
    [
        _program_custom, _program_multi, _program_yaml_object, _program_resolvers,
        _program_idioms, _program_python_tags,
    ],
)  # fmt: skip
def test_representer_programs_give_pyyaml_bytes(api, program):
    assert program(cy) == program(yaml)


def test_safe_dumpers_refuse_what_only_the_full_dumper_writes(api):
    class Thing:
        pass

    for dumper in (cy.SafeDumper, cy.CSafeDumper):
        with pytest.raises(cy.RepresenterError):
            cy.dump(Thing(), Dumper=dumper)
        with pytest.raises(cy.RepresenterError):
            cy.dump(collections.OrderedDict(a=1), Dumper=dumper)
    assert cy.dump((1, 2), Dumper=cy.Dumper).startswith("!!python/tuple")
    with pytest.raises(cy.EmitterError):
        cy.dump(1, Dumper=cy.BaseDumper)
    # Writing the tags is all that is done with them: they are never read back as objects.
    with pytest.raises(cy.ConstructorError):
        cy.unsafe_load(cy.dump(Thing(), Dumper=cy.Dumper))


def test_serializer_states_and_bad_options(api):
    dumper = cy.SafeDumper(io.StringIO())
    with pytest.raises(cy.SerializerError):
        dumper.close()
    with pytest.raises(cy.SerializerError):
        dumper.serialize(cy.ScalarNode("tag:yaml.org,2002:str", "x"))
    dumper.open()
    with pytest.raises(cy.SerializerError):
        dumper.open()
    dumper.represent("x")
    dumper.close()
    with pytest.raises(cy.SerializerError):
        dumper.open()
    with pytest.raises(cy.EmitterError):
        cy.safe_dump(1, version=(2, 0))
    with pytest.raises(cy.EmitterError):
        cy.safe_dump(1, tags={"bad": "tag:x,2000:"})
    with pytest.raises(TypeError):
        cy.safe_dump(1, indent="x")


def test_dumping_hostile_values_ends_in_the_modules_errors(api):
    class Boom:
        def __reduce_ex__(self, protocol):
            raise ZeroDivisionError("from the object")

    with pytest.raises(ZeroDivisionError):
        cy.dump(Boom(), Dumper=cy.Dumper)  # the object's own exception reaches the caller
    for bad in (object(), Boom(), lambda: 1, type, 1j):
        with pytest.raises(cy.RepresenterError):
            cy.safe_dump(bad)
    # A string a YAML text cannot carry is written as an escape, as PyYAML does.
    assert cy.safe_dump("a\ud800b") == yaml.safe_dump("a\ud800b")


def test_numbers_of_any_size_are_written(api):
    big = 10**3000
    assert cy.safe_dump(big) == yaml.safe_dump(big)
    with pytest.raises(ValueError):  # the interpreter's own limit on digits, as for PyYAML
        cy.safe_dump(10**5000)
