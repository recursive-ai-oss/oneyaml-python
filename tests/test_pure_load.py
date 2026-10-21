"""Loading values: the Core schema, tags, keys, aliases and limits, through the pure backend."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import pytest

from oneyaml import _pure
from oneyaml._errors import YAMLError

# The backends that have ``load``. The native module joins this list once it has one.
IMPLS = [_pure]


@pytest.fixture(params=IMPLS, ids=lambda m: m.BACKEND)
def impl(request):
    return request.param


@dataclass(frozen=True)
class Limits:
    """A stand-in with the attributes the loader reads."""

    depth: int | None = 10_000
    alias_nodes: int | None = 1_000_000
    alias_bytes: int | None = 64 * 1024 * 1024
    input_bytes: int | None = None
    scalar_bytes: int | None = None
    documents: int | None = None
    int_digits: int | None = 4_300


NONE = Limits(None, None, None, None, None, None, None)


DEFAULT = Limits()


def one(impl, data, limits=DEFAULT):
    return impl.load(data, limits, False)


def many(impl, data, limits=DEFAULT):
    return impl.load(data, limits, True)


def fails(impl, data, category, limits=DEFAULT, multi=False):
    with pytest.raises(YAMLError) as info:
        impl.load(data, limits, multi)
    assert info.value.category == category, info.value
    return info.value


def same(a, b):
    """Equal in value and in type (``1 == 1.0 == True`` in Python)."""
    assert type(a) is type(b), (a, b)
    assert a == b


# -- the Core schema -----------------------------------------------------------------------

CORE = [
    ("null", None), ("Null", None), ("NULL", None), ("~", None), ("", None),
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False), ("FALSE", False),
    ("0", 0), ("-12", -12), ("+7", 7), ("007", 7), ("0755", 755), ("-0", 0),
    ("0o17", 15), ("0x1F", 31), ("0xff", 255),
    ("12345678901234567890123", 12345678901234567890123),
    ("0x" + "f" * 40, 16**40 - 1),
    ("1.5", 1.5), ("-1.5", -1.5), (".5", 0.5), ("5.", 5.0), ("1e3", 1000.0), ("1E-2", 0.01),
    ("-.5e1", -5.0), ("0.0", 0.0), ("1.0", 1.0), (".inf", math.inf), ("-.inf", -math.inf),
    ("+.inf", math.inf), (".Inf", math.inf), (".INF", math.inf), ("1e400", math.inf),
]  # fmt: skip

STRINGS = [
    "yes", "No", "on", "OFF", "y", "n", "nulL", "TrUe",
    "0b101", "0x_1F", "1_000.5", "1_000", "1:30",
    "0o",
    "0x", "0o8", "0xg", "-0x1", "+0x1", "-0o7", "+.nan", ".nan.", "..5", ".", "+", "1e",
    "e5", "1.2.3", "2001-12-14", "2001-12-14t21:59:43.10-05:00", "= ", "<<", "~x", "Infinity",
    "inf", "nan", ".infinity", "1 000", "０", "١٢",
]  # fmt: skip


@pytest.mark.parametrize(("text", "want"), CORE)
def test_plain_scalar_resolves(impl, text, want):
    same(one(impl, f"[{text}]\n" if text else "- \n")[0], want)
    same(one(impl, f"k: {text}\n")["k"], want)
    same(one(impl, text + "\n"), want)


@pytest.mark.parametrize("text", STRINGS)
def test_plain_scalar_stays_a_string(impl, text):
    got = one(impl, f"- {text}\n" if text.strip() == text else f"- '{text}'\n")[0]
    same(got, text)


def test_nan_is_nan(impl):
    for t in (".nan", ".NaN", ".NAN"):
        assert math.isnan(one(impl, t))


def test_negative_zero_keeps_its_sign(impl):
    assert math.copysign(1, one(impl, "-0.0")) == -1


def test_quoted_and_block_scalars_are_strings(impl):
    got = one(impl, '- \'1\'\n- "true"\n- ""\n- |\n  null\n- >\n  1.5\n')
    assert got == ["1", "true", "", "null\n", "1.5\n"]
    assert all(type(x) is str for x in got)


def test_values_are_exact_python_types(impl):
    got = one(impl, "[1, 1.0, true, null, x]")
    assert [type(x) for x in got] == [int, float, bool, type(None), str]


# -- tags ----------------------------------------------------------------------------------


def test_explicit_core_tags_decide_the_type(impl):
    text = (
        "- !!float 1\n- !!int '3'\n- !!int 0x10\n- !!null ''\n- !!bool 'true'\n- !!str 0o10\n"
        "- !!float .inf\n- !!int -7\n- !!str\n- !!null\n- !!float '1e3'\n"
    )
    got = one(impl, text)
    want = [1.0, 3, 16, None, True, "0o10", math.inf, -7, "", None, 1000.0]
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        same(g, w)


def test_collection_tags_are_transparent(impl):
    assert one(impl, "!!map {a: !!seq [1, 2]}") == {"a": [1, 2]}
    assert one(impl, "! [1]") == [1]


def test_nonspecific_tag_makes_a_string(impl):
    same(one(impl, "! 12"), "12")


@pytest.mark.parametrize(
    "text",
    ["!!int twelve", "!!int 1.5", "!!float abc", "!!bool yes", "!!null x", "!!int ''",
     "!!float 0x10", "!!map a", "!!seq a", "!!str [a]", "!!int {a: 1}", "!!map [a]",
     "!!seq {a: 1}", "!!binary !", "!!binary a"],
)  # fmt: skip
def test_tag_that_does_not_fit_is_an_error(impl, text):
    err = fails(impl, text + "\n", "syntax")
    assert err.line == 1


def test_unknown_tags_are_kept_not_constructed(impl):
    text = (
        "- !color red\n- !!python/object/apply:os.system [echo]\n- !!set {a}\n"
        "- !<tag:example.com,2000:x> [1]\n- !local {a: 1}\n- !!timestamp 2001-12-14\n"
    )
    got = one(impl, text)
    assert got[0] == _pure.Tagged("!color", "red")
    assert got[1] == _pure.Tagged("tag:yaml.org,2002:python/object/apply:os.system", ["echo"])
    assert got[2] == _pure.Tagged("tag:yaml.org,2002:set", {"a": None})
    assert got[3] == _pure.Tagged("tag:example.com,2000:x", [1])
    assert got[4] == _pure.Tagged("!local", {"a": 1})
    assert got[5] == _pure.Tagged("tag:yaml.org,2002:timestamp", "2001-12-14")


def test_text_under_an_unknown_tag_is_not_resolved(impl):
    got = one(impl, "!x 12")
    assert got.value == "12"
    assert type(got.value) is str


def test_binary_is_bytes(impl):
    got = one(impl, "!!binary |\n  aGVs\n  bG8=\n")
    same(got, b"hello")
    same(one(impl, "!!binary 'aGk='"), b"hi")
    same(one(impl, "!!binary ''"), b"")


def test_tagged_is_immutable_and_hashable():
    t = _pure.Tagged("!a", "x")
    assert {t: 1}[_pure.Tagged("!a", "x")] == 1
    assert t != _pure.Tagged("!b", "x")
    with pytest.raises(AttributeError):
        t.tag = "!c"  # type: ignore[misc]


# -- mappings and keys ---------------------------------------------------------------------


def test_mapping_keeps_document_order(impl):
    got = one(impl, "z: 1\na: 2\nm: 3\n")
    assert list(got) == ["z", "a", "m"]


def test_merge_key_is_an_ordinary_key(impl):
    got = one(impl, "base: &b {x: 1}\nuse:\n  <<: *b\n  y: 2\nflow: {<<: *b}\n")
    assert got["use"] == {"<<": {"x": 1}, "y": 2}
    assert got["flow"] == {"<<": {"x": 1}}


def test_non_string_keys(impl):
    got = one(impl, "1: a\nnull: b\n2.5: c\n'1': d\n")
    assert got == {1: "a", None: "b", 2.5: "c", "1": "d"}


def test_empty_key_is_null(impl):
    assert one(impl, "? \n: v\n") == {None: "v"}


@pytest.mark.parametrize(
    "text",
    ["a: 1\na: 2\n", "{a: 1, a: 2}", "1: a\n0x1: b\n", "1: a\n01: b\n",
     "null: a\n~: b\n", "true: a\nTrue: b\n", ".nan: a\n.NaN: b\n",
     "? [1, 2]\n: a\n? [1, 2]\n: b\n",
     "? {a: 1}\n: x\n? {a: 1}\n: y\n", "1.0: a\n1e0: b\n", "? a\n: 1\n? 'a'\n: 2\n"],
)  # fmt: skip
def test_duplicate_keys_are_an_error(impl, text):
    fails(impl, text, "duplicate-key")


def test_duplicate_key_points_at_the_second_key(impl):
    err = fails(impl, "a: 1\nb: 2\nc: 3\nb: 4\n", "duplicate-key")
    assert (err.line, err.column) == (4, 1)
    err = fails(impl, "x:\n  {p: 1, q: 2, p: 3}\n", "duplicate-key")
    assert (err.line, err.column) == (2, 16)


def test_same_text_different_type_is_not_a_duplicate(impl):
    assert one(impl, "'1': a\n1: b\n") == {"1": "a", 1: "b"}


@pytest.mark.parametrize("text", ["1: a\n1.0: b\n", "1: a\ntrue: b\n", "0: a\nfalse: b\n"])
def test_keys_python_cannot_tell_apart_are_a_limit(impl, text):
    # A dict holds 1, 1.0 and True under one key; losing one silently would be worse.
    fails(impl, text, "limit")


@pytest.mark.parametrize(
    "text", ["1e400: a\n.inf: b\n", "1e400: a\n2e400: b\n", "1e-400: a\n2e-400: b\n"]
)
def test_float_keys_outside_a_double_are_a_limit(impl, text):
    fails(impl, text, "limit")


def test_float_values_outside_a_double_are_fine(impl):
    assert one(impl, "[1e400, -1e400, 1e-400]") == [math.inf, -math.inf, 0.0]
    assert one(impl, "0.0: a\n1e0: b\n") == {0.0: "a", 1.0: "b"}


def test_sequence_key_is_a_tuple(impl):
    got = one(impl, "? [a, [b, 1]]\n: v\n")
    assert got == {("a", ("b", 1)): "v"}


def test_mapping_key_is_a_frozen_mapping(impl):
    got = one(impl, "? {a: 1, b: [x]}\n: v\n")
    (key,) = got
    assert isinstance(key, _pure.FrozenMapping)
    assert key == {"a": 1, "b": ("x",)}
    assert hash(key) == hash(_pure.FrozenMapping({"b": ("x",), "a": 1}))
    assert list(key) == ["a", "b"]
    with pytest.raises(TypeError):
        key["c"] = 1  # type: ignore[index]
    assert got[_pure.FrozenMapping({"a": 1, "b": ("x",)})] == "v"


def test_tagged_collection_key(impl):
    got = one(impl, "? !t [1, 2]\n: v\n")
    assert got == {_pure.Tagged("!t", (1, 2)): "v"}


def test_key_is_a_mapping_inside_a_key(impl):
    got = one(impl, "? [{a: [1]}, {b: {c: 2}}]\n: v\n")
    ((key, _),) = got.items()
    assert key[0]["a"] == (1,)
    assert key[1]["b"]["c"] == 2


def test_key_nesting_is_capped_not_recursed(impl):
    deep = "? " + "[" * 100_000 + "]" * 100_000 + "\n: v\n"
    err = fails(impl, deep, "limit", NONE)
    assert err.line == 1
    fails(impl, "? " + "{a: " * 300 + "1" + "}" * 300 + "\n: v\n", "limit", NONE)


# -- aliases -------------------------------------------------------------------------------


def test_alias_to_a_collection_is_the_same_object(impl):
    got = one(impl, "a: &a [1, 2]\nb: *a\nc: [*a, *a]\nd: &d {k: v}\ne: *d\n")
    assert got["a"] is got["b"] is got["c"][0] is got["c"][1]
    assert got["d"] is got["e"]


def test_alias_to_a_scalar_keeps_the_resolved_value(impl):
    got = one(impl, "a: &x 12\nb: *x\nc: &y 'true'\nd: *y\n")
    same(got["b"], 12)
    same(got["d"], "true")


def test_anchor_on_a_tagged_node_keeps_the_result(impl):
    got = one(impl, "a: &x !!float 1\nb: *x\nc: &t !k [1]\nd: *t\n")
    same(got["b"], 1.0)
    assert got["d"] is got["c"]
    assert isinstance(got["d"], _pure.Tagged)


def test_anchor_may_be_redefined(impl):
    got = one(impl, "- &a 1\n- *a\n- &a 2\n- *a\n")
    assert got == [1, 1, 2, 2]


def test_alias_means_the_latest_anchor_even_inside_its_own_node(impl):
    got = one(impl, "a: &b\n  se: &b\n    1\ncopy: *b\n")
    assert got == {"a": {"se": 1}, "copy": 1}


def test_alias_inside_its_own_node_is_refused(impl):
    err = fails(impl, "a: &b 1\nc: &b [ *b ]\n", "syntax")
    assert (err.line, err.column) == (2, 9)
    fails(impl, "&a {x: *a}", "syntax")


def test_anchors_do_not_cross_documents(impl):
    assert many(impl, "--- &a [1]\n--- [2]\n") == [[1], [2]]
    fails(impl, "--- &a [1]\n--- *a\n", "syntax", multi=True)


def nine_by_nine(levels=9):
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, levels):
        prev, name = "abcdefghi"[i - 1], "abcdefghi"[i]
        lines.append(f"{name}: &{name} [{', '.join('*' + prev for _ in range(9))}]")
    return "\n".join(lines) + "\n"


def distinct_objects(root):
    seen, stack = set(), [root]
    while stack:
        o = stack.pop()
        if isinstance(o, (list, dict)) and id(o) not in seen:
            seen.add(id(o))
            stack.extend(o.values() if isinstance(o, dict) else o)
    return len(seen)


def test_alias_bomb_loads_in_proportion_to_the_document(impl):
    got = one(impl, nine_by_nine())
    # 9 levels of 9 aliases would be 387 million nodes written out; here they are 10 objects.
    assert distinct_objects(got) == 10
    assert got["i"][0] is got["h"]


def test_walking_a_bomb_is_charged_to_the_alias_budget(impl):
    got = one(impl, nine_by_nine())
    with pytest.raises(YAMLError) as info:
        impl.check_expansion(got, 1_000_000, 64 * 1024 * 1024)
    assert info.value.category == "limit"
    nodes, _ = impl.check_expansion(one(impl, nine_by_nine(3)), None, None)
    # a: 1 list + 9 scalars; b: 1 + 9 * 10; c: 1 + 9 * 91; plus the mapping and its 3 keys.
    assert nodes == 1 + 3 + 10 + 91 + 820


def test_alias_used_as_a_key_is_charged(impl):
    # Freezing a collection for use as a key copies it: that is where aliases cost.
    doc = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
    big = doc + "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\nk: {*c : 1}\n"
    assert len(one(impl, big, replace(Limits(), alias_nodes=1_000_000))["k"]) == 1
    fails(impl, big, "limit", replace(Limits(), alias_nodes=500))
    fails(impl, big, "limit", replace(Limits(), alias_bytes=500))
    assert len(one(impl, big, NONE)["k"]) == 1


def test_key_charges_add_up(impl):
    doc = "a: &a [x, x, x, x, x, x, x, x, x, x]\nm1: {*a : 1}\nm2: {*a : 1}\nm3: {*a : 1}\n"
    lim = replace(Limits(), alias_nodes=25)
    fails(impl, doc, "limit", lim)
    assert one(impl, doc, replace(lim, alias_nodes=40))["m3"]


def test_alias_key_is_frozen_once_and_equal_to_its_source(impl):
    got = one(impl, "a: &a [1, 2]\nm: {*a : 1}\n? [1, 2]\n: 2\n")
    assert got["m"] == {(1, 2): 1}
    assert got[(1, 2)] == 2


# -- limits and depth ----------------------------------------------------------------------


def test_depth_limit(impl):
    deep = "[" * 100_000 + "]" * 100_000
    err = fails(impl, deep, "limit")
    assert err.line == 1
    fails(impl, "- " * 20 + "x\n", "limit", replace(Limits(), depth=10))
    assert one(impl, "[[[x]]]", replace(Limits(), depth=3)) == [[["x"]]]
    fails(impl, "[[[[x]]]]", "limit", replace(Limits(), depth=3))


def test_one_hundred_thousand_levels_without_a_limit(impl):
    n = 100_000
    got = one(impl, "[" * n + "]" * n, NONE)
    depth = 0
    while got:
        got = got[0]
        depth += 1
    assert depth == n - 1


def test_deep_block_mappings_without_a_limit(impl):
    n = 20_000
    text = "".join(" " * i + "k:\n" for i in range(n)) + " " * n + "x\n"
    got = one(impl, text, NONE)
    depth = 0
    while isinstance(got, dict):
        got = got["k"]
        depth += 1
    assert depth == n and got == "x"


def test_integer_digit_cap(impl):
    assert one(impl, "9" * 4300) == int("9" * 4300)
    err = fails(impl, "x: " + "9" * 4301 + "\n", "limit")
    assert (err.line, err.column) == (1, 4)
    fails(impl, "-" + "9" * 4301, "limit")
    fails(impl, "0x" + "f" * 4301, "limit")
    fails(impl, "0o" + "7" * 4301, "limit")
    fails(impl, "12345", "limit", replace(Limits(), int_digits=4))
    assert one(impl, "1234", replace(Limits(), int_digits=4)) == 1234


def test_integers_beyond_the_cap_are_exact_when_uncapped(impl):
    digits = "7" + "0" * 9_999 + "1"
    got = one(impl, digits, NONE)
    assert got % 1_000_000_007 == pow(10, 10_000, 1_000_000_007) * 7 % 1_000_000_007 + 1
    assert one(impl, "-" + digits, NONE) == -got
    assert one(impl, "+" + digits, NONE) == got
    assert one(impl, "0x" + "f" * 5000, NONE) == 16**5000 - 1
    assert one(impl, digits, replace(Limits(), int_digits=20_000)) == got


def test_float_with_a_huge_exponent_is_not_a_digit_problem(impl):
    assert one(impl, "1e" + "9" * 100_000) == math.inf
    assert one(impl, "0." + "0" * 100_000 + "1") == 0.0


def test_input_size_limit(impl):
    lim = replace(Limits(), input_bytes=10)
    assert one(impl, b"a: 1234\n", lim) == {"a": 1234}
    fails(impl, b"a: 12345678\n", "limit", lim)
    fails(impl, "a: 12345678\n", "limit", lim)
    assert one(impl, "é: 1234\n", lim) == {"é": 1234}
    fails(impl, "é: 123456\n", "limit", lim)


def test_scalar_size_limit(impl):
    lim = replace(Limits(), scalar_bytes=8)
    assert one(impl, "abcdefgh", lim) == "abcdefgh"
    err = fails(impl, "k: abcdefghi\n", "limit", lim)
    assert (err.line, err.column) == (1, 4)
    fails(impl, "'ééééé'", "limit", lim)
    assert one(impl, "'éééé'", lim) == "éééé"


def test_document_count_limit(impl):
    lim = replace(Limits(), documents=2)
    assert many(impl, "--- 1\n--- 2\n", lim) == [1, 2]
    fails(impl, "--- 1\n--- 2\n--- 3\n", "limit", lim, multi=True)


# -- streams -------------------------------------------------------------------------------


def test_empty_stream(impl):
    assert one(impl, "") is None
    assert one(impl, b"") is None
    assert one(impl, "# only a comment\n") is None
    assert many(impl, "") == []
    assert many(impl, "# only a comment\n") == []


def test_empty_document(impl):
    assert one(impl, "---\n") is None
    assert many(impl, "---\n---\n") == [None, None]


def test_several_documents(impl):
    assert many(impl, "a\n---\nb\n...\n---\n[c]\n") == ["a", "b", ["c"]]
    assert many(impl, "x") == ["x"]


def test_loading_one_of_several_documents_names_loads_all(impl):
    err = fails(impl, "a\n---\nb\n", "other")
    assert "loads_all" in err.message
    assert err.line == 2
    fails(impl, "a: 1\n...\n---\nb: 2\n", "other")


def test_a_second_document_that_is_empty_still_counts(impl):
    fails(impl, "a\n---\n", "other")


def test_bytes_are_decoded_by_the_byte_order_mark(impl):
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32"):
        assert one(impl, "k: é\n".encode(enc)) == {"k": "é"}
    assert one(impl, "\ufeffk: 1\n") == {"k": 1}
    assert one(impl, bytearray(b"k: 1")) == {"k": 1}


def test_malformed_bytes_are_an_encoding_error(impl):
    err = fails(impl, b"a: \xff\n", "encoding")
    assert (err.line, err.column) == (1, 4)


def test_wrong_argument_type(impl):
    with pytest.raises(TypeError):
        impl.load(12, Limits(), False)


def test_syntax_error_has_a_position_and_no_internals(impl):
    err = fails(impl, "a: [1, 2\nb: 3\n", "syntax")
    assert err.line is not None and err.column is not None
    assert not hasattr(err, "raw_events") and not hasattr(err, "offset")


# -- characters only quoted scalars may hold -------------------------------------------------


def test_quoted_scalars_take_every_character_from_space_up(impl):
    got = many(impl, 'a: "x\x7fy"\n---\na: \'x\x80y\'\n---\n["\ufffe", "\x9f", \'\uffff\']\n')
    assert got == [{"a": "x\x7fy"}, {"a": "x\x80y"}, ["\ufffe", "\x9f", "\uffff"]]


def test_those_characters_are_still_refused_elsewhere(impl):
    err = fails(impl, "a: x\x7fy\n", "syntax")
    assert (err.line, err.column) == (1, 5)
    err = fails(impl, 'a: "x\x7fy"\nb: # c\x80\n', "syntax")
    assert (err.line, err.column) == (2, 7)
    err = fails(impl, 'a: "\x7f"\nb: z\ufffe\n', "syntax")
    assert (err.line, err.column) == (2, 5)
    fails(impl, '"a\x01b"', "syntax")


def test_stand_in_characters_are_not_confused_with_real_ones(impl):
    got = one(impl, '["\U000f0000\x7f", "\U000f0001\x80"]')
    assert got == ["\U000f0000\x7f", "\U000f0001\x80"]


def test_the_event_stream_agrees(impl):
    events = impl.events_text('"a\x7fb"')
    assert events == ["+STR", "+DOC", '=VAL "a\x7fb', "-DOC", "-STR"]


def test_byte_order_mark_then_comment_after_a_document_end_marker(impl):
    text = "--- a\n...\n\ufeff# note\n--- b\n"
    assert impl.events_text(text) == [
        "+STR", "+DOC ---", "=VAL :a", "-DOC ...", "+DOC ---", "=VAL :b", "-DOC", "-STR",
    ]  # fmt: skip
    assert many(impl, text) == ["a", "b"]
    # A mark in the middle of a line stays an error.
    fails(impl, "a\ufeff# x\n", "syntax")
