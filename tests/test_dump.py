"""Writing Python values as YAML: layout, quoting, errors, and both backends agreeing."""

import io
import math
import os
import random
import struct
import subprocess
import sys
import textwrap
import threading

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
    assert text == "[" * 9001 + "]" * 9001 + "\n"
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


# ---- what is read of an object, and what happens when the object misbehaves --------------------


def _both(value, **kw):
    """The text each backend writes for ``value``, or the type of what it raises."""
    out = []
    for mod in (_native, _pure) if HAS_NATIVE else (_pure,):
        try:
            out.append(mod.dump([value], (2, kw.get("flow", False), False), False))
        except Exception as exc:
            out.append(type(exc))
    return out


def test_mutating_a_dict_while_it_is_written_is_a_runtime_error(d):
    def grows_when_asked_for_items(flow):
        outer = {}

        class D(dict):
            def items(self):
                outer[f"x{len(outer)}"] = 1
                return super().items()

        outer["a"] = D(b=1)
        outer["c"] = 2
        with pytest.raises(RuntimeError, match="changed size"):
            d.dumps(outer, flow=flow)

    grows_when_asked_for_items(False)
    grows_when_asked_for_items(True)


@pytest.mark.parametrize("kind", ["dict", "list"])
@pytest.mark.parametrize("flow", [False, True])
def test_a_value_changed_by_other_threads_ends_in_a_runtime_error_or_a_text(d, kind, flow):
    shared_d = {f"k{i}": [i, "x"] for i in range(500)}
    shared_l = [{"a": i} for i in range(500)]
    stop = threading.Event()

    def mutate():
        i = 0
        while not stop.is_set():
            i += 1
            if kind == "dict":
                shared_d[f"n{i % 50}"] = i
                shared_d.pop(f"n{(i + 25) % 50}", None)
            else:
                shared_l.append({"b": i})
                if len(shared_l) > 300:
                    del shared_l[-200:]

    threads = [threading.Thread(target=mutate) for _ in range(2)]
    for t in threads:
        t.start()
    seen = set()
    try:
        for _ in range(150):
            try:
                d.dumps(shared_d if kind == "dict" else shared_l, flow=flow)
                seen.add("ok")
            except RuntimeError:
                seen.add("RuntimeError")
    finally:
        stop.set()
        for t in threads:
            t.join()
    # Any other exception would have escaped the loop above.
    assert seen <= {"ok", "RuntimeError"}
    if kind == "list":
        assert seen == {"ok"}


def test_exceptions_from_the_values_own_code_propagate(d):
    class Boom(Exception):
        pass

    class D(dict):
        def items(self):
            raise Boom

    class L(list):
        def __iter__(self):
            raise Boom

    for value in (D(a=1), L([1]), {"k": D(a=1)}, [L([1])]):
        for flow in (False, True):
            with pytest.raises(Boom):
                d.dumps(value, flow=flow)


def test_the_user_has_no_say_in_what_is_a_tagged(d):
    # An object that answers differently every time it is asked what its class is.
    class Sneaky:
        calls = 0

        @property
        def __class__(self):
            Sneaky.calls += 1
            return Tagged if Sneaky.calls % 2 == 0 else Sneaky

        tag = "!t"
        value = "leaf"

    for flow in (False, True):
        for value in (Sneaky(), [Sneaky()], {"k": Sneaky()}):
            with pytest.raises(TypeError, match="not YAML serializable"):
                d.dumps(value, flow=flow)

    class Sub(Tagged):
        __slots__ = ()

        def __getattribute__(self, name):
            return "!wrong" if name == "tag" else super().__getattribute__(name)

    assert d.dumps(Sub("!right", "x")) == "!right x\n"


def test_tags_do_not_nest_and_nothing_recurses(d):
    inner = Tagged("!a", "x")
    for value in (Tagged("!b", inner), [Tagged("!b", inner)], {"k": Tagged("!b", inner)}):
        for flow in (False, True):
            with pytest.raises(ValueError, match="two tags"):
                d.dumps(value, flow=flow)
    deep = Tagged("!t", "leaf")
    for _ in range(100_000):
        deep = Tagged("!t", deep)
    with pytest.raises(ValueError, match="two tags"):
        d.dumps(deep)
    with pytest.raises(ValueError, match="two tags"):
        d.dumps(Tagged("!t", b"x"))
    with pytest.raises(ValueError, match="two tags"):
        d.dumps(Tagged("!t", memoryview(b"x")))


def test_a_container_freed_during_the_walk_is_not_a_cycle(d):
    # Each level is a new object that is gone once the walk has moved on; a reused address must
    # not read as the same container.
    def gen(n):
        if n == 0:
            yield "leaf"
            return
        yield make(n - 1)

    class L(list):
        def __iter__(self):
            return gen(self.n)

    def make(n):
        x = L([0])
        x.n = n
        return x

    for depth in (10, 70, 200, 1000):
        want = "- " * (depth + 1) + "leaf\n"
        assert d.dumps(make(depth)) == want
        assert (
            d.dumps(make(depth), flow=True) == "[" * (depth + 1) + "leaf" + "]" * (depth + 1) + "\n"
        )


def test_the_cycle_check_costs_work_in_proportion_to_the_value(d):
    ops = _api._impl._dump_path_ops
    shallow = [[[[1]]], {"a": [{"b": []}]}]
    ops(True)
    d.dumps(shallow)
    assert ops(True) == 0
    # A deep chain ending in a wide level: each small container is entered once, so the work
    # is a few operations per container, not a pass over the whole path for each.
    depth, width = 200, 2000
    leaf = [[0] for _ in range(width)]
    chain = leaf
    for _ in range(depth - 2):
        chain = [chain]
    for flow in (False, True):
        ops(True)
        d.dumps(chain, flow=flow)
        assert ops(True) <= 2 * (depth + width)


def test_a_cycle_through_a_subclass_or_a_mapping_is_found(d):
    class L(list):
        pass

    a = L()
    a.append(a)
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(a)
    m = {}
    m["k"] = {"j": [m]}
    with pytest.raises(ValueError, match="Circular"):
        d.dumps(m, flow=True)


def _in_a_memory_limited_process(backend, extra, limit_mb):
    code = textwrap.dedent(
        f"""
        import resource, oneyaml
        def vm():
            with open("/proc/self/status") as f:
                return next(int(l.split()[1]) for l in f if l.startswith("VmSize")) * 1024
        x = list(range(8))
        for _ in range({extra}):
            x = [x, x]
        limit = vm() + {limit_mb} * 2**20
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        try:
            oneyaml.dumps(x, flow=True)
            print("written")
        except MemoryError:
            print("MemoryError")
        """
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "ONEYAML_BACKEND": backend},
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc and RLIMIT_AS")
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("limit_mb", [40, 150, 400])
def test_running_out_of_memory_is_a_memory_error(backend, limit_mb):
    # 2**24 leaves written to one line: gigabytes, far more than the limit.
    done = _in_a_memory_limited_process(backend, 24, limit_mb)
    assert done.returncode == 0, done.stderr[-400:]
    assert done.stdout.strip() == "MemoryError"


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
def test_a_pending_signal_ends_a_native_dump():
    big = [[i, "x"] for i in range(20_000)]
    _native._test_hook(4)
    try:
        with pytest.raises(KeyboardInterrupt):
            _native.dump([big], (2, False, False), False)
        _native._test_hook(4)
        with pytest.raises(KeyboardInterrupt):
            _native.dump([big], (2, True, False), False)
    finally:
        _native._test_hook(0)


def test_indent_is_checked_by_the_backend_itself(d):
    impl = _api._impl
    for bad in (0, 17, -1, 10**30, -(10**30)):
        with pytest.raises(ValueError):
            impl.dump([1], (bad, False, False), False)
    for bad in ("2", 2.0, None, True):
        with pytest.raises(TypeError):
            impl.dump([1], (bad, False, False), False)


def test_a_surrogate_in_a_tag_is_the_same_error_as_in_a_string(d):
    with pytest.raises(ValueError) as in_tag:
        d.dumps(Tagged("!a\ud800", 1))
    with pytest.raises(ValueError) as in_text:
        d.dumps("a\ud800")
    assert str(in_tag.value) == str(in_text.value)


# ---- the two backends read subclasses the same way ----------------------------------------


class _Pairs(dict):
    def items(self):
        return [[k, v] for k, v in dict.items(self)]


class _Triples(dict):
    def items(self):
        return [(k, v, "extra") for k, v in dict.items(self)]


class _Singles(dict):
    def items(self):
        return [("only",)]


class _NotPairs(dict):
    def items(self):
        return [5]


class _ViaIter(tuple):
    __slots__ = ()

    def __iter__(self):
        return iter(["via-iter", "value"])


class _ViaIterItems(dict):
    def items(self):
        return [_ViaIter(("k", "v"))]


class _Bytes1(bytes):
    def __bytes__(self):
        return b"other"


class _Bytes2(bytes):
    def __iter__(self):
        return iter([300])


class _ByteArray(bytearray):
    def __bytes__(self):
        return b"other"


class _Lying(list):
    def __len__(self):
        return 0


class _LyingMap(dict):
    def __len__(self):
        return 0


class _Str(str):
    def startswith(self, *a):
        return False

    def __len__(self):
        return 0

    def __getitem__(self, i):
        return "?"


class _Int(int):
    def __repr__(self):
        return "bogus"

    __str__ = __repr__


class _Float(float):
    def __repr__(self):
        return "bogus"

    def __float__(self):
        return 9.0


_PROTOCOL_CASES = {
    "pairs as lists": _Pairs(a=1),
    "pairs as triples": _Triples(a=1),
    "pairs of one": _Singles(a=1),
    "entries that are not iterable": _NotPairs(a=1),
    "tuple subclass entries": _ViaIterItems(a=1),
    "bytes subclass with __bytes__": _Bytes1(b"xyz"),
    "bytes subclass with __iter__": _Bytes2(b"xyz"),
    "bytearray subclass": _ByteArray(b"xyz"),
    "memoryview": memoryview(b"ab"),
    "memoryview of ints": memoryview(__import__("array").array("i", [1, 2])),
    "memoryview slice": memoryview(b"abcdef")[::2],
    "sequence that says it is empty": {"k": _Lying([1, 2])},
    "mapping that says it is empty": {"k": _LyingMap(a=1)},
    "empty subclasses": {"k": _Lying(), "j": _LyingMap()},
    "str subclass": [_Str("a b"), _Str(" x"), {_Str("key"): _Str("1")}],
    "str subclass tag": Tagged(_Str("tag:yaml.org,2002:str"), "x"),
    "str subclass tag, local": Tagged(_Str("!local"), _Str("x")),
    "int subclass": [_Int(7), {_Int(3): _Int(2**70)}],
    "float subclass": [_Float(1.5), _Float(1e17)],
}


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
@pytest.mark.parametrize("flow", [False, True])
@pytest.mark.parametrize("name", list(_PROTOCOL_CASES))
def test_backends_read_subclasses_the_same_way(name, flow):
    native, pure = _both(_PROTOCOL_CASES[name], flow=flow)
    if isinstance(native, type) or isinstance(pure, type):
        assert native is pure, (name, native, pure)
    else:
        assert native == pure, (name, native, pure)


@pytest.mark.parametrize("flow", [False, True])
def test_subclass_reading(d, flow):
    def dump(v):
        return d.dumps(v, flow=flow)

    for value in (_Triples(a=1), _Singles(a=1), _NotPairs(a=1)):
        with pytest.raises((ValueError, TypeError)):
            dump(value)
    for value in (_Triples(a=1), _Singles(a=1)):
        with pytest.raises(ValueError):
            dump(value)
    with pytest.raises(TypeError):
        dump(_NotPairs(a=1))
    # Entries unpack as `k, v = entry` does.
    assert dump(_Pairs(a=1)) == ("{a: 1}\n" if flow else "a: 1\n")
    assert dump(_ViaIterItems(a=1)) == ("{via-iter: value}\n" if flow else "via-iter: value\n")
    # Byte strings are the raw buffer.
    raw = "!!binary eHl6\n"
    assert dump(_Bytes1(b"xyz")) == raw
    assert dump(_Bytes2(b"xyz")) == raw
    assert dump(_ByteArray(b"xyz")) == raw
    assert dump(memoryview(b"xyz")) == raw
    assert dump(memoryview(b"abcdef")[::2]) == "!!binary YWNl\n"
    # str, int and float subclasses are their base value.
    assert dump(_Str("a b")) == "a b\n"
    assert dump(_Int(7)) == "7\n"
    assert dump(_Float(1.5)) == "1.5\n"
    assert dump(Tagged(_Str("!local"), "x")) == "!local x\n"
    # Emptiness is what iteration says, not what __len__ says.
    assert dump({"k": _Lying([1, 2])}) == ("{k: [1, 2]}\n" if flow else "k:\n- 1\n- 2\n")
    assert dump({"k": _LyingMap(a=1)}) == ("{k: {a: 1}}\n" if flow else "k:\n  a: 1\n")
    assert dump({"k": _Lying()}) == ("{k: []}\n" if flow else "k: []\n")


# ---- floats whose shortest form is not unique ---------------------------------------------------

# The two shortest decimal forms of these differ in the last digit, and the interpreter's
# repr picks one: -861927274670212.2 and -861927274670212.3 are both nearest-shortest.
_TIES = [
    -861927274670212.2, 1801514316094494.2, -673136162761606.2, 1273812973436447.2,
    -2000723712327223.2, -1438096285768600.2, -1107780618034936.2, 1956873983951424.2,
    -2244273033561874.2, 1720805954758130.2, -1858551618583540.2, -73091527877058.12,
    -1072361184576926.2, 899725719067368.2, 94688732081205.62,
]  # fmt: skip


def _expected_float(x):
    r = repr(x)
    i = r.find("e")
    return r[:i] + ".0" + r[i:] if i > 0 and "." not in r[:i] else r


def test_floats_are_written_with_the_interpreters_digits(d):
    for x in _TIES:
        assert d.dumps(x) == _expected_float(x) + "\n", x
        assert d.dumps([x], flow=True) == f"[{_expected_float(x)}]\n"
        assert d.loads(d.dumps(x)) == x


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
def test_backends_write_the_same_float_digits():
    rng = random.Random(1234)
    values = list(_TIES)
    for _ in range(30_000):
        x = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if x == x and abs(x) != math.inf:
            values.append(x)
        values.append(rng.uniform(-1, 1) * 10.0 ** rng.randint(-20, 20))
        values.append(float(rng.randint(-(10**17), 10**17)))
    opts = (2, False, False)
    assert _native.dump([values], opts, False) == _pure.dump([values], opts, False)
    assert _native.dump([values], (2, True, False), False) == _pure.dump(
        [values], (2, True, False), False
    )


# ---- exact ties, malformed entries, free-threaded runs ---------------------------------------


def test_floats_that_are_exact_midpoints_are_written_as_repr_gives_them(d):
    # Each of these is exactly half way between two equally short decimals.
    for x in (2.0**-25, 2.0**-24, 3 * 2.0**-25, 0.0010004043579101562, 5.960464477539062e-07):
        assert d.dumps(x) == _expected_float(x) + "\n", x
    for k in range(1, 40):
        for j in range(1, 400, 2):
            for x in (j * 2.0**-k, -j * 2.0**-k):
                assert d.dumps(x) == _expected_float(x) + "\n", (j, k)


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
def test_backends_write_the_same_digits_for_dyadic_floats():
    rng = random.Random(77)
    values = []
    for k in range(0, 75):
        for _ in range(60):
            j = rng.randrange(1, 1 << rng.randint(1, 53)) | 1
            for x in (j * 2.0**-k, j * 2.0**k, j * 2.0**-k * 10 ** (k % 7)):
                if math.isfinite(x) and x:
                    values += [x, math.nextafter(x, 0.0), math.nextafter(x, math.inf)]
    opts = (2, True, False)
    bad = [
        x
        for x in values
        if _native.dump([x], opts, False) != _pure.dump([x], opts, False)
        or _pure.dump([x], opts, False) != _expected_float(x) + "\n"
    ]
    assert len(bad) == 0, bad[:5]


def _message(mod, value, flow):
    with pytest.raises(ValueError) as info:
        mod.dump([value], (2, flow, False), False)
    return str(info.value)


@pytest.mark.skipif(not HAS_NATIVE, reason="needs the native backend")
@pytest.mark.parametrize("flow", [False, True])
@pytest.mark.parametrize("name", ["pairs as triples", "pairs of one", "tuple subclass entries"])
def test_malformed_items_entries_have_one_message_on_both_backends(name, flow):
    value = _PROTOCOL_CASES[name]
    if name == "tuple subclass entries":
        value = _Singles(a=1)
    assert _message(_native, value, flow) == _message(_pure, value, flow)
    assert "(key, value)" in _message(_pure, value, flow)


FREE_THREADED = bool(__import__("sysconfig").get_config_var("Py_GIL_DISABLED"))

_THREAD_SCRIPT = """
import sys, threading
import oneyaml
shared = {f'k{i}': [i, 'x'] for i in range(500)}
stop = threading.Event()
def mutate():
    i = 0
    while not stop.is_set():
        i += 1
        shared[f'n{i % 50}'] = i
        shared.pop(f'n{(i + 25) % 50}', None)
ts = [threading.Thread(target=mutate) for _ in range(2)]
for t in ts: t.start()
seen = set()
try:
    for _ in range(300):
        try:
            oneyaml.dumps(shared); seen.add('ok')
        except RuntimeError:
            seen.add('RuntimeError')
finally:
    stop.set()
    for t in ts: t.join()
print(sorted(seen))
"""


@pytest.mark.skipif(not (HAS_NATIVE and FREE_THREADED), reason="needs a free-threaded build")
@pytest.mark.parametrize("gil", ["0", "1"])
def test_dicts_changed_by_threads_never_panic_with_the_gil_on_or_off(gil):
    # The way a dict is walked does not depend on whether the GIL is on at the moment the
    # dump starts (an extension import can switch it on for a while on a free-threaded build).
    env = {**os.environ, "PYTHON_GIL": gil, "ONEYAML_BACKEND": "native"}
    run = subprocess.run(
        [sys.executable, "-c", _THREAD_SCRIPT], env=env, capture_output=True, text=True, timeout=120
    )
    assert run.returncode == 0, run.stderr[-500:]
    assert set(eval(run.stdout)) <= {"ok", "RuntimeError"}
