"""The PyYAML compat module: surface, errors, types, and a comparison with PyYAML itself.

The comparison tests need PyYAML (a development dependency) and run every document through
both of our backends. Where this library intentionally answers differently, the difference is
named: ``pyyaml_differences.json`` for the yaml-test-suite and the kit's own cases (read from
the kit checkout named by ``ONEYAML_KIT``), and the rules in ``_generated_difference`` for the
generated scalars.
"""

from __future__ import annotations

import datetime
import io
import json
import math
import os
import pathlib
import pickle

import pytest

import oneyaml
import oneyaml.compat.pyyaml as cy
from oneyaml import _api
from pyyaml_corpus import documents, scalars

yaml = pytest.importorskip("yaml")

HERE = pathlib.Path(__file__).parent
KIT = os.environ.get("ONEYAML_KIT")


def same(a, b) -> bool:
    """Equal values of the same types (1 is not True, nan equals nan), compared by an explicit
    stack so nothing here recurses."""
    work = [(a, b)]
    while work:
        x, y = work.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, float):
            if not ((math.isnan(x) and math.isnan(y)) or x == y):
                return False
        elif isinstance(x, dict):
            if len(x) != len(y):
                return False
            rest = list(y.items())
            for k, v in x.items():
                for i, (other_k, other_v) in enumerate(rest):
                    if same(k, other_k) and same(v, other_v):
                        del rest[i]
                        break
                else:
                    return False
        elif isinstance(x, (list, tuple)):
            if len(x) != len(y):
                return False
            work.extend(zip(x, y, strict=True))
        elif x != y:
            return False
    return True


def ours(text, loader=None):
    return list(cy.load_all(text, loader or cy.SafeLoader))


_STAND_INS = {}


def pyyaml_loader(name, module=None):
    """PyYAML's loader called ``name``; for a C loader the install lacks, a class built from the
    same parts as its pure counterpart. The C loaders differ only in the scanner and parser, so
    their constructor tables and the scoping of registrations behave alike."""
    module = module or yaml
    found = getattr(module, name, None)
    if found is not None:
        return found
    key = (module, name)
    if key not in _STAND_INS:
        pure = getattr(module, name[1:])
        _STAND_INS[key] = type(name, pure.__bases__, {"__init__": pure.__init__})
    return _STAND_INS[key]


def theirs(text, loader=None):
    return list(yaml.load_all(text, loader or yaml.SafeLoader))


def outcome(fn, text, error):
    try:
        return "ok", fn(text)
    except error as exc:
        return "err", type(exc).__name__
    except Exception as exc:  # PyYAML lets some raw exceptions out
        return "raw", type(exc).__name__


# ---- surface -----------------------------------------------------------------------------------


def test_names_exist():
    for name in cy.__all__:
        assert hasattr(cy, name), name
    assert getattr(cy, "CSafeLoader", cy.SafeLoader) is cy.CSafeLoader
    assert isinstance(cy.__version__, str) and cy.__with_libyaml__ is True
    for cls in (cy.ScannerError, cy.ParserError, cy.ComposerError, cy.ConstructorError):
        assert issubclass(cls, cy.MarkedYAMLError)
    assert issubclass(cy.MarkedYAMLError, cy.YAMLError)
    assert issubclass(cy.YAMLError, oneyaml.YAMLError)


def test_functions_take_the_same_streams(api):
    text = "a: 1\nb: [x, yes]\n"
    expected = {"a": 1, "b": ["x", True]}
    assert cy.safe_load(text) == expected
    assert cy.safe_load(text.encode()) == expected
    assert cy.safe_load(io.StringIO(text)) == expected
    assert cy.safe_load(io.BytesIO(text.encode("utf-16"))) == expected
    assert cy.load(text, Loader=cy.SafeLoader) == expected
    assert cy.load(text, cy.CSafeLoader) == expected
    assert cy.full_load(text) == expected
    assert cy.unsafe_load(text) == expected
    assert list(cy.safe_load_all("a\n---\nb\n")) == ["a", "b"]
    assert list(cy.load_all("--- 1\n--- 2\n", Loader=cy.Loader)) == [1, 2]
    assert list(cy.full_load_all("1\n---\n2\n")) == [1, 2]
    assert list(cy.unsafe_load_all("1\n---\n2\n")) == [1, 2]
    assert cy.safe_load("") is None
    assert list(cy.safe_load_all("")) == []
    with pytest.raises(TypeError):
        cy.safe_load(12)
    with pytest.raises(TypeError):
        cy.load("a")  # PyYAML 6 needs the Loader too


def test_load_all_is_a_generator(api):
    gen = cy.safe_load_all("a: 1\n---\nb: [\n")
    # Nothing is read until the first document is asked for, as with PyYAML.
    with pytest.raises(cy.YAMLError):
        next(gen)


def test_loader_classes_work_as_instances(api):
    loader = cy.SafeLoader("a: 1\n---\nb: 2\n")
    assert loader.check_data()
    assert loader.get_data() == {"a": 1}
    assert loader.get_data() == {"b": 2}
    assert not loader.check_data()
    loader.dispose()

    class Mine(cy.SafeLoader):
        pass

    assert cy.load("x: on", Loader=Mine) == {"x": True}


# ---- errors ------------------------------------------------------------------------------------


def test_errors_carry_zero_based_marks(api):
    with pytest.raises(cy.YAMLError) as info:
        cy.safe_load("a: 1\nb: [1, 2\nc: 3\n")
    exc = info.value
    assert isinstance(exc, cy.MarkedYAMLError)
    assert exc.problem_mark.name == "<unicode string>"
    assert exc.problem_mark.line >= 1  # 0-based: the problem is past the first line
    assert exc.problem_mark.column >= 0
    assert exc.problem
    assert 'in "<unicode string>", line ' in str(exc)
    with pytest.raises(cy.YAMLError) as info:
        cy.safe_load(b"a: [")
    assert info.value.problem_mark.name == "<byte string>"
    buf = io.StringIO("a: [")
    buf.name = "x.yaml"
    with pytest.raises(cy.YAMLError) as info:
        cy.safe_load(buf)
    assert info.value.problem_mark.name == "x.yaml"


def test_every_load_error_is_a_yaml_error(api):
    bad = [
        "a: [",
        "a: *nope\n",
        "a: &x 1\nb: *y\n",
        "- a\n- - b\n c: d\n",
        "a: !!int abc\n",
        "a: !!python/object/apply:os.system ['echo hi']\n",
        "<<: 1\n",
        "a: !unknown x\n",
        "x: 1\n---\ny: 2\n",
        "a: " + "1" * 5000 + "\n",
        "[" * 20000 + "]" * 20000,
    ]
    for text in bad:
        with pytest.raises(cy.YAMLError):
            cy.safe_load(text)


def test_error_classes_by_phase(api):
    with pytest.raises(cy.ComposerError):
        cy.safe_load("a: *nope\n")
    with pytest.raises(cy.ComposerError):
        cy.safe_load("x: 1\n---\ny: 2\n")
    with pytest.raises(cy.ConstructorError):
        cy.safe_load("a: !!int abc\n")
    with pytest.raises(cy.ConstructorError):
        cy.safe_load("<<: 1\n")
    with pytest.raises(cy.ParserError):
        cy.safe_load("a: [1, 2\n")
    try:
        cy.safe_load("a: [1, 2\n")
    except cy.YAMLError as exc:
        again = pickle.loads(pickle.dumps(exc))
        assert str(again) == str(exc) and type(again) is type(exc)


def test_limits_stay_on(api):
    with pytest.raises(cy.ConstructorError) as info:
        cy.safe_load("[" * 20000 + "]" * 20000)
    assert info.value.category == "limit"
    with pytest.raises(cy.YAMLError) as info:
        cy.safe_load("a: " + "7" * 5000 + "\n")
    assert info.value.category == "limit"


def test_code_is_never_run(api, tmp_path):
    marker = tmp_path / "ran"
    path = f"!!python/object/apply:pathlib.Path [{marker}]"
    payload = f"!!python/object/apply:pathlib.Path.touch [{path}]\n"
    for loader in (cy.UnsafeLoader, cy.Loader, cy.FullLoader, cy.CLoader, cy.SafeLoader):
        with pytest.raises(cy.ConstructorError) as info:
            cy.load(payload, Loader=loader)
        assert "python" in str(info.value)
        assert not marker.exists()
    with pytest.raises(cy.ConstructorError):
        cy.unsafe_load("!!python/name:os.system\n")


def test_unknown_tags_are_refused_by_every_constructing_loader(api):
    for loader in (cy.SafeLoader, cy.FullLoader):
        with pytest.raises(cy.ConstructorError):
            cy.load("a: !mine 1\n", Loader=loader)
    assert cy.load("a: !mine 1\n", Loader=cy.BaseLoader) == {"a": "1"}


# ---- types -------------------------------------------------------------------------------------

SCALARS = [
    ("yes", True), ("No", False), ("ON", True), ("off", False), ("y", "y"), ("n", "n"),
    ("true", True), ("False", False), ("TRUE", True), ("tRUE", "tRUE"),
    ("~", None), ("null", None), ("Null", None), ("", None),
    ("0o17", "0o17"), ("017", 15), ("0b101", 5), ("-0b11", -3), ("0x1F", 31), ("-0xff", -255),
    ("1_000", 1000), ("+12", 12), ("0", 0), ("-0", 0), ("1:30", 90), ("1:30:15", 5415),
    ("-1:30", -90), ("08", "08"), ("0b_", "0b_"), ("1_000.5", 1000.5), ("1.5", 1.5),
    (".5", 0.5), ("-.5", "-.5"), ("1.", 1.0), ("1e3", "1e3"), ("1.0e+3", 1000.0),
    ("1.0e3", "1.0e3"), ("1:30.5", 90.5), (".inf", math.inf), ("-.INF", -math.inf),
    ("2001-12-14", datetime.date(2001, 12, 14)),
    ("2001-12-14t21:59:43.10-05:00", datetime.datetime(
        2001, 12, 14, 21, 59, 43, 100000, tzinfo=datetime.timezone(-datetime.timedelta(hours=5)))),
    ("2001-12-14 21:59:43", datetime.datetime(2001, 12, 14, 21, 59, 43)),
    ("2001-12-14 21:59:43 Z", datetime.datetime(
        2001, 12, 14, 21, 59, 43, tzinfo=datetime.timezone.utc)),
]  # fmt: skip


@pytest.mark.parametrize(("text", "value"), SCALARS)
def test_yaml11_scalars(api, text, value):
    got = cy.safe_load(f"k: {text}\n")["k"]
    assert same(got, value)
    assert same(cy.safe_load(f"- {text}\n")[0], value)
    # Quoting makes any of them text.
    assert cy.safe_load(f"k: '{text}'\n")["k"] == text


def test_invalid_date_is_a_constructor_error(api):
    with pytest.raises(cy.ConstructorError):
        cy.safe_load("k: 2001-13-14\n")


def test_nan_and_big_numbers(api):
    assert math.isnan(cy.safe_load("k: .nan\n")["k"])
    assert cy.safe_load("k: " + "9" * 40 + "\n")["k"] == int("9" * 40)
    assert cy.safe_load("k: 0x" + "f" * 40 + "\n")["k"] == 16**40 - 1
    assert cy.safe_load("k: 0b" + "1" * 70 + "\n")["k"] == 2**70 - 1
    assert cy.safe_load("k: 1" + ":59" * 20 + "\n")["k"] == _base60([1] + [59] * 20)


def _base60(parts):
    n = 0
    for p in parts:
        n = n * 60 + p
    return n


def test_explicit_tags(api):
    got = cy.safe_load(
        "- !!str 5\n- !!int '0x1f'\n- !!float '1e3'\n- !!bool YES\n- !!null x\n"
        "- !!binary aGk=\n- !!timestamp 2001-01-02\n- !!map {a: 1}\n- !!seq [1]\n"
    )
    assert same(
        got,
        ["5", 31, 1000.0, True, None, b"hi", datetime.date(2001, 1, 2), {"a": 1}, [1]],
    )


def test_sets_omaps_pairs_binary(api):
    got = cy.safe_load(
        "s: !!set {a, b}\no: !!omap [{x: 1}, {y: 2}]\np: !!pairs [{k: 1}, {k: 2}]\n"
        "b: !!binary |\n  aGVs\n  bG8=\n"
    )
    assert got == {
        "s": {"a", "b"},
        "o": [("x", 1), ("y", 2)],
        "p": [("k", 1), ("k", 2)],
        "b": b"hello",
    }
    assert type(got["s"]) is set and type(got["o"][0]) is tuple
    for bad in (
        "!!set [a]",
        "!!omap {a: 1}",
        "!!omap [a]",
        "!!omap [{a: 1, b: 2}]",
        "!!binary abc",
    ):
        with pytest.raises(cy.ConstructorError):
            cy.safe_load(bad)


def test_base_loader_is_all_strings(api):
    got = cy.load(
        "a: 1\nb: [yes, ~, 2001-01-01]\nc: !!int 5\nd: &x 1\ne: *x\n<<: {z: 1}\n", cy.BaseLoader
    )
    assert got == {
        "a": "1",
        "b": ["yes", "~", "2001-01-01"],
        "c": "5",
        "d": "1",
        "e": "1",
        "<<": {"z": "1"},
    }
    assert cy.load("a: 1\na: 2\n", cy.CBaseLoader) == {"a": "2"}


def test_repeated_key_keeps_the_last(api):
    assert cy.safe_load("a: 1\nb: 2\na: 3\n") == {"a": 3, "b": 2}
    assert list(cy.safe_load("a: 1\nb: 2\na: 3\n")) == ["a", "b"]
    # 1, 1.0 and true are one key to a Python dict, as in PyYAML.
    assert cy.safe_load("1: a\n1.0: b\ntrue: c\n") == {1: "c"}


# ---- merge keys --------------------------------------------------------------------------------


def test_merge_keys(api):
    doc = (
        "base: &b {x: 1, y: 2}\nmore: &m {y: 9, z: 3}\n"
        "one: {<<: *b, y: 5}\n"
        "two: {<<: [*b, *m]}\n"
        "three:\n  <<: *b\n  w: 0\n"
        "late: {y: 7, <<: *b}\n"
        "inline: {<<: {q: 1}}\n"
    )
    got = cy.safe_load(doc)
    assert got["one"] == {"x": 1, "y": 5}
    assert got["two"] == {"x": 1, "y": 2, "z": 3}  # the first mapping listed wins
    assert got["three"] == {"x": 1, "y": 2, "w": 0}
    assert got["late"] == {"x": 1, "y": 7}  # an own key beats a merged one wherever it is
    assert got["inline"] == {"q": 1}
    assert list(got["two"]) == ["y", "z", "x"] or list(got["two"]) == list(
        yaml.safe_load(doc)["two"]
    )
    assert list(got["one"]) == list(yaml.safe_load(doc)["one"])


def test_merge_only_in_key_position(api):
    assert cy.safe_load("[<<, a]") == ["<<", "a"]
    assert cy.safe_load("{a: <<}") == {"a": "<<"}
    assert cy.safe_load("'<<': 1\n") == {"<<": 1}
    assert cy.safe_load("<<\n") == "<<"


def test_merge_of_a_non_mapping_is_an_error(api):
    for bad in ("<<: 1\n", "<<: [1]\n", "<<: [{a: 1}, 2]\n", "<<:\n", "{<<: x}"):
        with pytest.raises(cy.ConstructorError):
            cy.safe_load(bad)


def test_merge_chain_ends_in_a_limit_error(api):
    # Each level merges the one before it twice: written out it doubles every level.
    lines = ["a0: &a0 {x0: 1}"]
    for i in range(1, 26):
        lines.append(f"a{i}: &a{i}\n  <<: [*a{i - 1}, *a{i - 1}]\n  x{i}: 1")
    with pytest.raises(cy.ConstructorError) as info:
        cy.safe_load("\n".join(lines) + "\n")
    assert info.value.category == "limit"
    # A short chain is fine and shares what it merges.
    short = "\n".join(lines[:8]) + "\n"
    assert len(cy.safe_load(short)["a7"]) == 8


def test_merge_in_a_complex_key(api):
    got = cy.safe_load("? {<<: {a: 1}, b: 2}\n: v\n")
    (key,) = got
    assert dict(key) == {"a": 1, "b": 2} and got[key] == "v"


def test_deep_and_large_inputs_do_not_recurse(api):
    depth = 9000
    got = cy.safe_load("[" * depth + "]" * depth)
    for _ in range(depth - 1):
        got = got[0]
    assert got == []
    got = cy.safe_load("".join(f"{' ' * i}k{i}:\n" for i in range(1500)) + " " * 1500 + "v: yes")
    assert got is not None


@pytest.mark.slow
def test_large_input_reads_the_same(api):
    # Big enough for the native backend to read on several threads.
    rows = []
    for i in range(4000):
        rows.append(
            f"- {{id: 0x{i:x}, flag: {'yes' if i % 2 else 'off'}, when: 2001-12-{i % 28 + 1:02d}, "
            f"t: 1:{i % 60:02d}:00, <<: {{base: {i}}}, tag: !!set {{a, b}}}}\n"
        )
    text = "".join(rows)
    assert len(text) > 300_000
    got = cy.safe_load(text)
    assert len(got) == 4000
    assert got[3] == {
        "id": 3, "flag": True, "when": datetime.date(2001, 12, 4), "t": 3600 + 3 * 60,
        "base": 3, "tag": {"a", "b"},
    }  # fmt: skip
    assert same(got, yaml.safe_load(text))


# ---- both backends, same answers ---------------------------------------------------------------


def _backends():
    import oneyaml._pure as pure

    mods = [pure]
    try:
        import oneyaml._native as native

        mods.append(native)
    except ImportError:
        pass
    return mods


def detail(mod, text, schema, multi=False, limits=None):
    """What a backend answers, with everything an error says about where."""
    try:
        return "ok", mod.load(text, limits or _api._DEFAULT_LIMITS, multi, schema)
    except oneyaml.YAMLError as exc:
        return "err", (exc.category, exc.line, exc.column, getattr(exc, "_construct", False))


def assert_same_answer(text, schema, multi=False, limits=None, parser_positions=True):
    """Both backends answer alike. The positions of errors the parser finds are the engine's
    own (some are listed as known differences), so a caller going through many inputs can leave
    them out: everything else, including where a value is refused, must agree."""
    first, *rest = (detail(m, text, schema, multi, limits) for m in _backends())
    for other in rest:
        assert other[0] == first[0], (schema, text, first, other)
        if first[0] == "ok":
            assert same(first[1], other[1]), (schema, text)
        elif parser_positions or first[1][0] != "syntax" or first[1][3]:
            assert first[1] == other[1], (schema, text, first, other)
        else:
            assert first[1][0] == other[1][0] and first[1][3] == other[1][3], (text, first, other)
    return first


# Each of these once differed between the backends, or escaped as a bare exception.
REPRO_DOCS = [
    "&m <<: {a: 1}\nx: *m\n",
    "&m <<: {a: 1}\n? [*m]\n: 1\n",
    "&m <<: {a: 1}\nb: {<<: *m}\n",
    "&m <<: {a: 1}\nb: 2\n",
    "!!merge <<: {a: 1}\nb: 2\n",
    "a: !!merge x\n",
    "<<: x\nb: !!seq Y\n",
    "<<: x\nb: *nope\n",
    "<<: x\nb: [\n",
    "<<: x\n",
    "a: 1\na: 2\nb: [\n",
    "a: 1\nb: 2\nb: 3\nc: [\n",
    "k: 1" + ":01" * 40 + ".5\n",
    "k: 1" + ":00" * 40 + ".5\n",
    "k: 1" + ":30" * 21 + ".25\n",
    "k: 1" + ":30" * 22 + ".25\n",
    "k: 1" + ":59" * 300 + "\n",
    "k: 1" + ":01" * 20000 + "\n",
    "k: 1" + ":01" * 20000 + ".5\n",
    "k: !!int 1" + ":01" * 20000 + "\n",
    "k: !!int 12345678\n",
    "k: !!int 0x" + "f" * 50 + "\n",
    "k: 0x" + "f" * 50 + "\n",
    "k: 0b" + "1" * 200 + "\n",
    "k: " + "7" * 5000 + "\n",
    "k: 0" + "7" * 5000 + "\n",
    "k: 0x1F\n",
    "k: 1_000\n",
    "k: 2001-13-45\n",
    "k: !!timestamp x\n",
]


@pytest.mark.parametrize("text", REPRO_DOCS)
@pytest.mark.parametrize("schema", ["yaml11", "text", "core"])
def test_backends_answer_alike_in_every_detail(text, schema):
    assert_same_answer(text, schema)
    assert_same_answer(text, schema, multi=True, limits=oneyaml.Limits.untrusted())
    if schema != "core":  # the Core schema counts digits of 0x and 0o numbers differently
        for digits in (3, 4, 30):
            assert_same_answer(text, schema, limits=oneyaml.Limits(int_digits=digits))


def test_backends_agree_on_the_yaml11_documents():
    docs = [
        "a: 1\nb: [yes, 0b11, 017, 1_0, 1:30, 2001-01-01, .5e+2]\n",
        "base: &b {x: 1}\nd: {<<: [*b, {y: 2}], x: 3}\n",
        "s: !!set {a, b}\no: !!omap [{a: 1}]\nb: !!binary aGk=\n",
        "k: 1\nk: 2\n",
        "? [a, b]\n: !!set {c}\n? !!omap [{a: 1}]\n: 2\n",
        "a: " + "9" * 30 + "\nb: 0x" + "f" * 30 + "\nc: 1" + ":59" * 12 + "\n",
    ]
    for text in docs:
        for schema in ("yaml11", "text", "core"):
            assert_same_answer(text, schema)


# ---- merge keys, digit limits and long numbers: cases that once went wrong ------------------


@pytest.mark.parametrize(
    "text",
    [
        "&m <<: {a: 1}\nx: *m\n",
        "&m <<: {a: 1}\n? [*m]\n: 1\n",
        "&m <<: {a: 1}\nb: {<<: *m}\n",
        "&m <<: {a: 1}\nb: [*m]\n",
    ],
)
def test_alias_to_a_merge_key_is_a_construction_error(api, text):
    with pytest.raises(cy.ConstructorError):
        cy.safe_load(text)
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load(text)


def test_an_anchor_on_a_merge_key_is_harmless_unless_used(api):
    assert cy.safe_load("&m <<: {a: 1}\nb: 2\n") == {"a": 1, "b": 2}
    assert yaml.safe_load("&m <<: {a: 1}\nb: 2\n") == {"a": 1, "b": 2}


def test_explicit_merge_tag_merges(api):
    text = "base: &b {x: 1, y: 2}\nd:\n  !!merge <<: *b\n  y: 3\ne: {!!merge m: [*b]}\n"
    assert cy.safe_load(text) == yaml.safe_load(text)
    assert cy.safe_load(text)["d"] == {"x": 1, "y": 3}
    with pytest.raises(cy.ConstructorError):
        cy.safe_load("a: !!merge x\n")
    with pytest.raises(cy.ConstructorError):
        cy.safe_load("- !!merge x\n")


def test_construction_errors_are_syntax_errors_with_the_construct_class(api):
    for text in ("<<: 1\n", "a: !!int abc\n", "a: !!set [1]\n", "a: !tag x\n", "a: 2001-13-45\n"):
        with pytest.raises(cy.ConstructorError) as info:
            cy.safe_load(text)
        assert info.value.category == "syntax", text
        assert info.value.problem_mark is not None, text


def test_the_unknown_alias_message_is_one_wording(api):
    with pytest.raises(cy.ComposerError) as info:
        cy.safe_load("a: *nope\n")
    assert info.value.problem == "alias *nope has no anchor"
    with pytest.raises(oneyaml.YAMLError) as info2:
        api.loads("a: *nope\n")
    assert info2.value.message == "alias *nope has no anchor"


def test_long_sexagesimal_floats_never_overflow_or_make_nan(api):
    for parts in (2, 5, 21, 22, 23, 30, 100, 170, 180, 400):
        text = "k: 1" + ":30" * (parts - 1) + ".25\n"
        ours_value = cy.safe_load(text)["k"]
        assert not math.isnan(ours_value), parts
        try:
            theirs_value = yaml.safe_load(text)["k"]
        except OverflowError:
            assert ours_value == math.inf, parts  # PyYAML itself fails here
        else:
            assert ours_value == theirs_value, parts
    assert cy.safe_load("k: -1" + ":00" * 400 + ".5\n")["k"] == -math.inf
    assert cy.safe_load("k: 0" + ":00" * 400 + ".5\n")["k"] == 0.5


def test_long_base60_integers_obey_the_digit_cap(api):
    text = "k: 1" + ":01" * 300_000 + "\n"
    for limits in (oneyaml.Limits(), oneyaml.Limits.untrusted(), oneyaml.Limits(int_digits=50)):
        with pytest.raises(oneyaml.YAMLError) as info:
            _api._impl.load(text, limits, False, "yaml11")
        assert info.value.category == "limit"
        assert info.value.line == 1 and info.value.column == 4
    with pytest.raises(cy.ConstructorError) as again:
        cy.safe_load("k: !!int 1" + ":01" * 300_000 + "\n")
    assert again.value.category == "limit"
    assert cy.safe_load("k: 1" + ":01" * 20)["k"] == _base60([1] + [1] * 20)


def test_the_digit_cap_counts_the_decimal_digits_of_the_value(api):
    # (text, fits under a cap of 3): hex, binary and octal are counted by bit length,
    # underscores, prefixes and colons are not digits.
    cases = [
        ("0xff", True), ("0xfff", False), ("1_00", True), ("1_000", False), ("0b111111111", True),
        ("0b1111111111", False), ("0777", True), ("07777", False), ("-1_00", True),
        ("1:30", True), ("1:30:00", False), ("0x_f", True), ("0001", True),
    ]  # fmt: skip
    for text, fits in cases:
        got = assert_same_answer(f"k: {text}\n", "yaml11", limits=oneyaml.Limits(int_digits=3))
        assert (got[0] == "ok") is fits, (text, got)
        assert fits or got[1][0] == "limit", text


def test_explicit_int_tag_obeys_the_digit_cap(api):
    for text, fits in [("12", True), ("1234", False), ("0x1F", True), ("0xffffff", False),
                       ("1:30", True), ("1:30:00:00", False), ("0b101", True)]:  # fmt: skip
        got = assert_same_answer(
            f"k: !!int {text}\n", "yaml11", limits=oneyaml.Limits(int_digits=3)
        )
        assert (got[0] == "ok") is fits, (text, got)
        assert fits or got[1][0] == "limit", text
    assert cy.safe_load("k: !!int " + "9" * 4300 + "\n")["k"] == int("9" * 4300)
    with pytest.raises(cy.ConstructorError) as info:
        cy.safe_load("k: !!int " + "9" * 4301 + "\n")
    assert info.value.category == "limit"


def test_a_stream_is_read_once(api):
    with pytest.raises(cy.ComposerError):
        cy.safe_load(io.StringIO("a: 1\n---\nb: 2\n"))
    with pytest.raises(cy.ComposerError):
        cy.load(io.BytesIO(b"a: 1\n---\nb: 2\n"), Loader=cy.SafeLoader)

    class Once(io.StringIO):
        reads = 0

        def read(self, *args):
            Once.reads += 1
            return super().read(*args)

    loader = cy.SafeLoader(Once("a: 1\n"))
    assert loader.check_data() and loader.get_data() == {"a": 1}
    assert Once.reads == 1


def test_errors_before_a_syntax_error_use_the_schema_rules(api):
    # A repeated key is not an error here, so the syntax error is the one reported.
    with pytest.raises(cy.YAMLError) as info:
        cy.safe_load("a: 1\na: 2\nb: [\n")
    assert info.value.category == "syntax"


# ---- the comparison with PyYAML ----------------------------------------------------------------

INLINE = [
    "a: 1\n",
    "- {<<: {a: 1}}\n",
    "base: &b {x: 1, y: 2}\nd: {<<: *b, y: 3}\ne: {<<: [*b, {z: 1, y: 0}]}\n",
    "base: &b {x: 1}\nd: {y: 2, <<: *b}\n",
    "a: !!set {x, y}\n",
    "a: !!omap [{x: 1}, {y: 2}]\n",
    "a: !!pairs [{x: 1}, {x: 2}]\n",
    "a: !!binary |\n  R0lGODlhDAAM\n  AIQAAP//\n",
    "a: 2002-12-14\nb: 2001-12-14t21:59:43.10-05:00\nc: 2001-12-14 21:59:43.10 -5\n"
    "d: 2001-12-15 2:59:43.10\n",
    "- 0o14\n- 014\n- 0b1010\n- 0xFF\n- 190:20:30\n- 1_000\n- +685_230\n",
    "- 6.8523015e+5\n- 685.230_15e+03\n- 685_230.15\n- 190:20:30.15\n- -.inf\n- .NaN\n",
    "- y\n- Y\n- yes\n- No\n- NO\n- on\n- Off\n- OFF\n- n\n",
    "? a\n: b\n",
    "a: &x [1, 2]\nb: *x\n",
    "a: 1\na: 2\n",
    "1: a\n1.0: b\n",
    "---\n- 1\n---\n- 2\n",
    "# comment only\n",
    "a: |\n  text\n  more\nb: >\n  folded\n  text\n",
    "'quoted': \"dq\\ttab\"\n",
    "- !!str 5\n- !!int '12'\n- !!float '1.5'\n- !!null ''\n",
]


@pytest.mark.parametrize("text", INLINE)
def test_inline_documents_match_pyyaml(api, text):
    for loader in (yaml.SafeLoader, pyyaml_loader("CSafeLoader")):
        ref = outcome(lambda t, loader=loader: theirs(t, loader), text, yaml.YAMLError)
        got = outcome(ours, text, cy.YAMLError)
        assert ref[0] == got[0] == "ok", (ref, got)
        assert same(ref[1], got[1])
    for ours_loader, theirs_loader in (
        (cy.FullLoader, yaml.FullLoader),
        (cy.BaseLoader, yaml.BaseLoader),
    ):
        assert same(ours(text, ours_loader), theirs(text, theirs_loader)), ours_loader


def _generated_difference(text, ref, got):
    """Why a generated document may differ from PyYAML, or None when it may not."""
    kind, detail = ref
    if kind == "raw":
        # PyYAML lets a ValueError out for 0b_ and 0x_ (no digits); here they stay text.
        return "PyYAML crashes, text kept"
    if kind == "err" and got[0] == "ok" and "\t" in text and detail == "ScannerError":
        return "PyYAML's scanner rejects a tab inside a plain scalar"
    if text in ("k: <<\n", "k: =\n") and kind == "err":
        return "PyYAML has no constructor for the merge and value tags, so a bare << or = fails"
    return None


def _check_generated(items):
    count = 0
    rules: dict[str, int] = {}
    for text in items:
        ref = outcome(lambda t: theirs(t), text, yaml.YAMLError)
        got = outcome(ours, text, cy.YAMLError)
        count += 1
        if ref[0] == "ok" and got[0] == "ok" and same(ref[1], got[1]):
            continue
        if ref[0] != "ok" and got[0] == "err":
            continue
        why = _generated_difference(text, ref, got)
        assert why is not None, (text, ref, got[0], got[1] if got[0] == "err" else got[1])
        rules[why] = rules.get(why, 0) + 1
    return count, rules


def _corpus(n):
    return [doc for s in scalars(n) for doc in documents(s)]


def test_generated_scalars_match_pyyaml(api):
    count, rules = _check_generated(_corpus(1500))
    assert count > 10000
    # The one raw-exception rule that leaves ours a value: no digits at all in 0b_ and 0x_.
    assert rules.get("PyYAML crashes, text kept", 0) == 2


@pytest.mark.slow
def test_generated_scalars_match_pyyaml_widely(api):
    count, rules = _check_generated(_corpus(12000))
    assert count > 90000


def test_generated_scalars_agree_between_backends():
    if len(_backends()) < 2:
        pytest.skip("no native module")
    for text in _corpus(1500):
        assert_same_answer(text, "yaml11", multi=True, parser_positions=False)
    # The digit limit counts the same on both.
    for text in _corpus(300):
        assert_same_answer(
            text, "yaml11", multi=True, limits=oneyaml.Limits(int_digits=3), parser_positions=False
        )


def _kit_cases():
    root = pathlib.Path(KIT)
    suites = root / "suites"
    for p in sorted(suites.glob("yaml-test-suite/data-*/**/in.yaml")):
        rel = p.parent.relative_to(suites / "yaml-test-suite").as_posix().split("/", 1)[1]
        yield "yts/" + rel, p
    for p in sorted(suites.glob("own/cases/*/*/input.yaml")):
        if "/safety/" in p.as_posix():
            continue  # alias bombs: PyYAML would build what the limits exist to stop
        yield "own/" + p.parent.relative_to(suites / "own/cases").as_posix(), p


@pytest.mark.skipif(not KIT, reason="set ONEYAML_KIT to the kit checkout to run the corpus")
def test_kit_corpus_matches_pyyaml(api):
    listed = json.loads((HERE / "pyyaml_differences.json").read_text())
    seen = set()
    compared = equal = both_error = 0
    for cid, path in _kit_cases():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        ref = outcome(theirs, text, yaml.YAMLError)
        got = outcome(ours, text, cy.YAMLError)
        compared += 1
        same_answer = (ref[0] == "ok" and got[0] == "ok" and same(ref[1], got[1])) or (
            ref[0] != "ok" and got[0] == "err"
        )
        entry = listed.get(cid)
        applies = entry is not None and api.name in entry["backends"]
        if applies:
            seen.add(cid)
        # A listed difference must still be one, and every other case must agree.
        assert same_answer != applies, (cid, ref[0], got[0], entry)
        if same_answer:
            equal += 1
            both_error += ref[0] != "ok"
    assert compared > 900
    assert seen == {k for k, v in listed.items() if api.name in v["backends"]}
    print(
        f"[{api.name}] compared {compared}: same {equal} (both errors {both_error}), "
        f"intended differences {len(seen)}"
    )  # noqa: T201


def test_intended_differences_are_all_explained():
    listed = json.loads((HERE / "pyyaml_differences.json").read_text())
    assert listed
    for cid, entry in listed.items():
        assert entry["reason"].strip() and entry["backends"], cid
        assert set(entry["backends"]) <= {"pure", "native"}, cid
