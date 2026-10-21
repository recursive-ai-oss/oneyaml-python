"""Writing Python values as YAML: layout, quoting, errors, and both backends agreeing."""

import io
import math
import os
import random
import sys

import pytest

import oneyaml
from oneyaml import FrozenMapping, LimitError, Tagged, _api, _pure

BACKENDS = tuple(os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(","))
HAS_NATIVE = "native" in BACKENDS
if HAS_NATIVE:
    from oneyaml import _native


@pytest.fixture(params=BACKENDS)
def d(request, monkeypatch):
    """The public dump functions on each backend in turn."""
    mod = _native if request.param == "native" else _pure
    monkeypatch.setattr(_api, "_impl", mod)
    return oneyaml


def same(a, b):
    """Equal in type and value, nan equal to nan, a mapping's keys compared as keys."""
    work = [(a, b)]
    while work:
        x, y = work.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, float):
            if not (x == y and math.copysign(1, x) == math.copysign(1, y)) and not (
                x != x and y != y
            ):
                return False
        elif isinstance(x, dict):
            if list(x) != list(y) and set(x) != set(y):
                return False
            work.extend((v, y[k]) for k, v in x.items())
        elif isinstance(x, (list, tuple)):
            if len(x) != len(y):
                return False
            work.extend(zip(x, y, strict=True))
        elif isinstance(x, Tagged):
            if x.tag != y.tag:
                return False
            work.append((x.value, y.value))
        elif isinstance(x, FrozenMapping):
            work.append((dict(x), dict(y)))
        elif x != y:
            return False
    return True


def roundtrip(d, value, **kw):
    text = d.dumps(value, **kw)
    back = d.loads(text)
    assert same(back, value), (value, text, back)
    return text


# ---- layout ----------------------------------------------------------------------------------


def test_default_layout(d):
    value = {
        "name": "app",
        "ports": [80, 443],
        "env": {"DEBUG": True, "LEVEL": None},
        "matrix": [[1, 2], {"a": 1, "b": [3]}],
        "empty": {},
        "none": [],
    }
    assert d.dumps(value) == (
        "name: app\n"
        "ports:\n"
        "- 80\n"
        "- 443\n"
        "env:\n"
        "  DEBUG: true\n"
        "  LEVEL: null\n"
        "matrix:\n"
        "- - 1\n"
        "  - 2\n"
        "- a: 1\n"
        "  b:\n"
        "  - 3\n"
        "empty: {}\n"
        "none: []\n"
    )


def test_indent_option(d):
    value = {"a": {"b": {"c": [1, {"d": "x"}]}}}
    assert d.dumps(value, indent=4) == "a:\n    b:\n        c:\n        - 1\n        - d: x\n"
    assert d.dumps(value, indent=1) == "a:\n b:\n  c:\n  - 1\n  - d: x\n"


def test_flow_option(d):
    value = {"a": [1, 2, {"b": None}], "c": {}, "d": [], "e": "x, y", "f": "multi\nline"}
    assert d.dumps(value, flow=True) == (
        "{a: [1, 2, {b: null}], c: {}, d: [], e: 'x, y', f: \"multi\\nline\"}\n"
    )
    assert d.dumps([], flow=True) == "[]\n"
    assert d.dumps("x", flow=True) == "x\n"
    assert d.dumps({(1, 2): "v"}, flow=True) == "{[1, 2]: v}\n"


def test_explicit_start_and_streams(d):
    assert d.dumps({"a": 1}, explicit_start=True) == "---\na: 1\n"
    assert d.dumps_all([1, "two", [3]]) == "1\n---\ntwo\n---\n- 3\n"
    assert d.dumps_all([1, 2], explicit_start=True) == "---\n1\n---\n2\n"
    assert d.dumps_all([]) == ""
    assert d.dumps_all(x for x in [None]) == "null\n"


def test_root_scalars_and_block_strings(d):
    assert d.dumps(None) == "null\n"
    assert d.dumps("a\nb") == "|-\n  a\n  b\n"
    assert d.dumps("a\n") == "|\n  a\n"
    assert d.dumps("a\n\n") == "|+\n  a\n\n"
    assert d.dumps({"k": "a\nb\n"}) == "k: |\n  a\n  b\n"
    assert d.dumps(["a\nb"]) == "- |-\n  a\n  b\n"
    assert d.dumps({"a": {"k": "x\ny"}}, indent=3) == "a:\n   k: |-\n      x\n      y\n"
    # What a block scalar cannot hold exactly is double-quoted.
    assert d.dumps(" a\nb") == '" a\\nb"\n'
    assert d.dumps("a \nb") == '"a \\nb"\n'
    assert d.dumps("\n") == '"\\n"\n'


def test_complex_keys(d):
    value = {(1, 2): "a", FrozenMapping({"x": 1, "z": (2,)}): [3], (): "e"}
    assert d.dumps(value) == ("? - 1\n  - 2\n: a\n? x: 1\n  z:\n  - 2\n: - 3\n? []\n: e\n")
    assert d.dumps([{(1,): 2}]) == "- ? - 1\n  : 2\n"
    roundtrip(d, value)


def test_long_key_is_explicit(d):
    key = "k" * 1025
    assert d.dumps({key: 1}) == f"? {key}\n: 1\n"
    assert d.dumps({"k" * 1024: 1}) == f"{'k' * 1024}: 1\n"
    # Counted in characters, as written (the quotes count).
    assert d.dumps({" " + "k" * 1022: 1}).startswith("? ")
    assert d.dumps({" " + "k" * 1021: 1}).startswith("' ")


def test_tags_and_bytes(d):
    assert d.dumps(Tagged("!color", "red")) == "!color red\n"
    assert d.dumps({"p": Tagged("tag:example.com,2000:point", {"x": 1})}) == (
        "p: !<tag:example.com,2000:point>\n  x: 1\n"
    )
    assert d.dumps([Tagged("!t", [1, 2])]) == "- !t\n  - 1\n  - 2\n"
    assert d.dumps(Tagged("!t", [])) == "!t []\n"
    assert d.dumps(Tagged("tag:yaml.org,2002:set", {"a": None})) == "!!set\na: null\n"
    assert d.dumps(Tagged("!t", "123")) == "!t '123'\n"
    assert d.dumps(Tagged("!a b", "x")) == "!a%20b x\n"
    assert d.dumps(b"\xff\x00") == "!!binary /wA=\n"
    assert d.dumps(b"") == "!!binary ''\n"
    long = d.dumps({"k": bytes(range(256))})
    lines = long.splitlines()
    assert lines[0] == "k: !!binary |"
    assert [len(x) for x in lines[1:-1]] == [2 + 76] * (len(lines) - 2)
    assert len(lines[-1]) <= 2 + 76
    assert lines[1] == "  " + (
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4"
    )
    assert d.dumps([bytes(range(256))], flow=True).count("\n") == 1
    for value in (Tagged("!t", "x"), Tagged("!t", {"a": [1]}), [b"\x00\xff"], {b"k": 1}):
        roundtrip(d, value)


def test_binary_reads_back_as_bytes(d):
    value = {"a": bytes(range(256)), "b": [b"", b"\xff"]}
    assert same(d.loads(d.dumps(value)), value)


# ---- scalars ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "", " ", " a", "a ", "\ta", "a\t", "true", "True", "NULL", "null", "~", "yes", "No", "on",
        "OFF", "y", "N", "1", "-1", "+1", "007", "0x1F", "0X1f", "0o7", "0O7", "0b101", "+0x1F",
        "0x+1F", "0o+7", "+-1", "--1", "1_000", "+_1", "1e3_", "0_x123", "1.5", ".5", "5.", "1e3",
        ".inf", "-.inf", ".NaN", "1:30", "1:30:00", "2001-12-14", "2001-12-14t21:59:43.10-05:00",
        "<<", "=", "---", "...", "--- x", "- a", "? a", ": a", "a: b", "a:", "a #b", "a\tb #c",
        "#c", "&a", "*a", "!t", "|", ">", "'q", '"q', "%d", "@a", "`a", "[a]", "{a}",
        ",", "1,000", "0755", "0.0", "1e-5", "- ", "-", "?",
    ],
)  # fmt: skip
def test_strings_that_could_be_read_as_something_else_are_quoted(d, text):
    out = d.dumps([text])
    assert out[2] in "'\"", out
    assert same(d.loads(out), [text])


@pytest.mark.parametrize(
    "text",
    [
        "a", "hello world", "a-b", "a.b", "a/b", "x1", "e5", "_private", ".hidden", "path/to/file",
        "5 apples", "version 2", "1st", "a b c", "it's", 'say "hi"', "a:b", "a#b", "é", "日本語",
        "naïve café", "emoji 😀", "a\tb", "yesterday", "nullable", "nope", "none", "Infinity",
        "NaN", "inf", "~x", "+x", "x+", "a - b",
    ],
)  # fmt: skip
def test_ordinary_strings_stay_plain(d, text):
    assert d.dumps([text]) == f"- {text}\n"


def test_escapes(d):
    cases = {
        "a\x00b": '"a\\0b"', "\x07\x08\x0b\x0c\x1b": '"\\a\\b\\v\\f\\e"', "a\rb": '"a\\rb"',
        "\x01": '"\\x01"', "\x7f": '"\\x7F"', "\x85": '"\\N"', "\x9f": '"\\x9F"',
        "\u2028\u2029": '"\\L\\P"', "\ufeff": '"\\uFEFF"', "\ufffe\uffff": '"\\uFFFE\\uFFFF"',
        'a"b\\': 'a"b\\',
    }  # fmt: skip
    for text, want in cases.items():
        assert d.dumps(text) == want + "\n", text
        roundtrip(d, text)
    # Quote and backslash are escaped only when the string is double-quoted anyway.
    assert d.dumps('\x01"\\') == '"\\x01\\"\\\\"\n'


def test_floats(d):
    assert d.dumps([1.0, 0.5, -0.0, 1e17, 1e-5, 1.5e-7, 5e-324, 1e100, 1e16, 1e15, 123456.789]) == (
        "- 1.0\n- 0.5\n- -0.0\n- 1.0e+17\n- 1.0e-05\n- 1.5e-07\n- 5.0e-324\n- 1.0e+100\n"
        "- 1.0e+16\n- 1000000000000000.0\n- 123456.789\n"
    )
    assert d.dumps([math.inf, -math.inf, math.nan]) == "- .inf\n- -.inf\n- .nan\n"
    for x in (0.1, 1 / 3, 2.5e-300, 1.7976931348623157e308, 2**53 + 0.0):
        roundtrip(d, x)


def test_integers(d):
    assert d.dumps([0, -1, 2**63, -(2**63) - 1, 2**64 - 1, True, False]) == (
        "- 0\n- -1\n- 9223372036854775808\n- -9223372036854775809\n"
        "- 18446744073709551615\n- true\n- false\n"
    )
    roundtrip(d, 10**300)


def test_integer_beyond_the_conversion_cap(d):
    big = 10**5000
    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        assert d.dumps(big) == "1" + "0" * 5000 + "\n"
        assert d.dumps([-big], flow=True) == "[-1" + "0" * 5000 + "]\n"
    finally:
        sys.set_int_max_str_digits(old)


def test_subclasses_write_as_what_they_extend(d):
    import collections
    import enum

    class Color(enum.IntEnum):
        RED = 3

    class S(str):
        pass

    class F(float):
        pass

    class L(list):
        pass

    od = collections.OrderedDict([("b", 1), ("a", 2)])
    od.move_to_end("b")
    assert d.dumps({"c": Color.RED, "s": S("x y"), "f": F(1.5), "l": L([1]), "od": od}) == (
        "c: 3\ns: x y\nf: 1.5\nl:\n- 1\nod:\n  a: 2\n  b: 1\n"
    )
    assert d.dumps(collections.defaultdict(list, {"k": [1]})) == "k:\n- 1\n"


# ---- arguments and errors --------------------------------------------------------------------


@pytest.mark.parametrize("value", [{1, 2}, object(), 1 + 2j, {"k": {1}}, [frozenset()], range(3)])
def test_unsupported_types_are_a_type_error(d, value):
    with pytest.raises(TypeError, match="not YAML serializable"):
        d.dumps(value)


def test_unsupported_key_type(d):
    with pytest.raises(TypeError):
        d.dumps({object(): 1})
    with pytest.raises(TypeError):
        d.dumps([object()], flow=True)


def test_cycles_are_an_error(d):
    a = []
    a.append(a)
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(a)
    m = {}
    m["self"] = [m]
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(m)
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(m, flow=True)
    # A long cycle is found too, and a shared (not circular) value is written twice.
    ring = []
    node = ring
    for _ in range(300):
        nxt = []
        node.append(nxt)
        node = nxt
    node.append(ring)
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(ring, flow=True)
    shared = [1, 2]
    assert d.dumps({"a": shared, "b": shared}, flow=True) == "{a: [1, 2], b: [1, 2]}\n"


def test_depth_without_recursion(d):
    deep = []
    node = deep
    for _ in range(9000):
        node.append([])
        node = node[0]
    text = d.dumps(deep)
    assert text.startswith("- - - - ")
    assert d.loads(text, limits=oneyaml.Limits.none()) is not None
    text = d.dumps(deep, flow=True)
    assert text == "[" * 9000 + "[" + "]" * 9000 + "]\n" or text.count("[") == 9000
    deep_map = {}
    node = deep_map
    for _ in range(3000):
        node["k"] = {}
        node = node["k"]
    assert d.dumps(deep_map).count("\n") == 3000


def test_too_deep_is_a_limit_error(d):
    deep = []
    node = deep
    for _ in range(10_001):
        node.append([])
        node = node[0]
    with pytest.raises(LimitError) as info:
        d.dumps(deep)
    assert info.value.category == "limit"
    with pytest.raises(LimitError):
        d.dumps(deep, flow=True)
    ok = []
    node = ok
    for _ in range(9_999):
        node.append([])
        node = node[0]
    d.dumps(ok)


def test_bad_strings_and_tags(d):
    with pytest.raises(ValueError, match="surrogate"):
        d.dumps("a\ud800b")
    with pytest.raises(ValueError, match="surrogate"):
        d.dumps({"k\udc00": 1})
    with pytest.raises(ValueError):
        d.dumps(Tagged("", 1))
    with pytest.raises(ValueError):
        d.dumps(Tagged("!é", 1))
    with pytest.raises(ValueError):
        d.dumps(Tagged("!a\nb", 1))
    with pytest.raises(ValueError):
        d.dumps(Tagged("!a", Tagged("!b", 1)))
    with pytest.raises(ValueError):
        d.dumps(Tagged("!a", b"x"))
    with pytest.raises(TypeError):
        d.dumps(Tagged(5, 1))


def test_option_errors(d):
    for bad in (0, 17, -1):
        with pytest.raises(ValueError):
            d.dumps(1, indent=bad)
    for bad in ("2", 2.0, None, True):
        with pytest.raises(TypeError):
            d.dumps(1, indent=bad)
    with pytest.raises(TypeError):
        d.dumps(1, width=80)
    with pytest.raises(TypeError):
        d.dumps_all("abc")
    with pytest.raises(TypeError):
        d.dumps_all({"a": 1})
    with pytest.raises(TypeError):
        d.dumps_all(5)


def test_files(d):
    text = io.StringIO()
    d.dump({"a": [1]}, text)
    assert text.getvalue() == "a:\n- 1\n"
    raw = io.BytesIO()
    d.dump_all(["é", 2], raw, explicit_start=True)
    assert raw.getvalue() == "---\né\n---\n2\n".encode()
    with pytest.raises(TypeError):
        d.dump(1, object())


def test_failed_dump_writes_nothing(d):
    text = io.StringIO()
    with pytest.raises(TypeError):
        d.dump({"a": object()}, text)
    assert text.getvalue() == ""


# ---- round trips over many values, and the backends against each other ------------------------

_WORDS = [
    "a", "b", "key", "hello world", "yes", "no", "null", "true", "False", "~", "", " ", "1", "-1",
    "1.5", "1e3", ".inf", "0x1F", "0o7", "1_000", "1:30", "2001-12-14", "<<", "=", "---", "...",
    "a: b", "a #b", "#", "- x", "? x", "x:", "[x]", "{x}", "x,y", "'", '"', "\\", "\n", "a\nb",
    "a\n", "a\n\n", " a\n", "a \n", "\ta", "a\t", "é", "日本", "😀", "\x00", "\x07", "\x1b", "\x7f",
    "\x85", "\u2028", "\u2029", "\ufeff", "\ufffe", "\r", "a\r\nb", "tab\there", "k" * 40,
]  # fmt: skip


def _scalar(rng):
    r = rng.random()
    if r < 0.45:
        w = rng.choice(_WORDS)
        return w if rng.random() < 0.7 else w + rng.choice(_WORDS)
    if r < 0.6:
        return rng.randint(-(10 ** rng.randint(1, 30)), 10 ** rng.randint(1, 30))
    if r < 0.75:
        return rng.choice(
            [rng.uniform(-1e5, 1e5), rng.uniform(-1, 1) * 10.0 ** rng.randint(-320, 300), 0.0, -0.0,
             1e16, 1e-5, 0.1, math.inf, -math.inf, math.nan, 5e-324, 1.0, 123456789.0]
        )  # fmt: skip
    if r < 0.82:
        return rng.choice([None, True, False])
    if r < 0.9:
        return bytes(rng.randrange(256) for _ in range(rng.choice([0, 1, 2, 3, 50, 100])))
    tag = rng.choice(["!t", "tag:example.com,2000:x", "!my%20tag", "tag:yaml.org,2002:str"])
    return Tagged(tag, rng.choice(_WORDS))


def _key(rng):
    r = rng.random()
    if r < 0.8:
        return rng.choice(_WORDS) + str(rng.randrange(5))
    if r < 0.9:
        return rng.choice([1, 2, 3.5, None, True, False, 10**20])
    if r < 0.95:
        return tuple(rng.randrange(3) for _ in range(rng.randrange(3)))
    return FrozenMapping({rng.choice("xyz"): rng.randrange(3)})


def _value(rng, depth):
    if depth <= 0 or rng.random() < 0.4:
        return _scalar(rng)
    r = rng.random()
    n = rng.choice([0, 1, 1, 2, 3, 5])
    if r < 0.45:
        return [_value(rng, depth - 1) for _ in range(n)]
    if r < 0.9:
        return {_key(rng): _value(rng, depth - 1) for _ in range(n)}
    return Tagged("!node", {"a": _value(rng, depth - 1)} if rng.random() < 0.5 else [1, [2]])


def _values(seed, count, depth=4):
    rng = random.Random(seed)
    return [_value(rng, depth) for _ in range(count)]


def _fixable(value):
    """A generated value the reader cannot give back as written: a key that equals another
    key once loaded (`1` and `True` and `1.0` are one dict key) never happens here, but a
    Tagged scalar loads as its text."""
    work = [value]
    while work:
        v = work.pop()
        if isinstance(v, Tagged):
            if not isinstance(v.value, (str, list, dict)):
                return False
            if isinstance(v.value, str) and v.tag == "tag:yaml.org,2002:str":
                return False
            work.append(v.value)
        elif isinstance(v, (list, tuple)):
            work.extend(v)
        elif isinstance(v, dict):
            work.extend(v.values())
            work.extend(k for k in v if isinstance(k, tuple | FrozenMapping))
        elif isinstance(v, FrozenMapping):
            work.extend(dict(v).values())
    return True


@pytest.mark.parametrize("seed", range(6))
def test_generated_values_read_back(d, seed):
    checked = 0
    for value in _values(seed, 150):
        if not _fixable(value):
            continue
        for kw in ({}, {"flow": True}, {"indent": 3, "explicit_start": True}):
            try:
                text = d.dumps(value, **kw)
            except ValueError as exc:  # a 1.1 style float key that is also an int key, say
                raise AssertionError(value) from exc
            back = d.loads(text)
            assert same(back, value), (value, kw, text, back)
        checked += 1
    assert checked > 100


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
@pytest.mark.parametrize("seed", range(6))
def test_backends_write_the_same_bytes(seed):
    for value in _values(100 + seed, 250, depth=5):
        for opts in ((2, False, False), (4, False, True), (1, True, False), (2, True, True)):
            assert _native.dump([value], opts, False) == _pure.dump([value], opts, False), (
                value,
                opts,
            )
    docs = _values(200 + seed, 5)
    assert _native.dump(docs, (2, False, False), True) == _pure.dump(docs, (2, False, False), True)


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
def test_backends_raise_the_same_errors():
    cyc = []
    cyc.append(cyc)
    deep = []
    node = deep
    for _ in range(10_001):
        node.append([])
        node = node[0]
    bad = [
        object(), {1, 2}, cyc, deep, "\ud800", Tagged("", 1), Tagged("!a", Tagged("!b", 1)),
        Tagged(1, 1), [Tagged("!é", 1)], {"k": b"x", "j": [1j]},
    ]  # fmt: skip
    for value in bad:
        for flow in (False, True):
            errs = []
            for mod in (_native, _pure):
                with pytest.raises(Exception) as info:
                    mod.dump([value], (2, flow, False), False)
                errs.append(type(info.value))
            assert errs[0] is errs[1], (value, errs)
