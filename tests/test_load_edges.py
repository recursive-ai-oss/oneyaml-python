"""Loading values: Core schema corners, tags, keys, aliases and the characters quoted scalars
may hold. Every test runs on both backends."""

from __future__ import annotations

import math

import pytest

from oneyaml import FrozenMapping, Limits, Tagged, YAMLError

NONE = Limits.none()


def one(api, data, limits=None):
    return api.loads(data, limits=limits)


def many(api, data, limits=None):
    return api.loads_all(data, limits=limits)


def fails(api, data, category, limits=None, multi=False):
    with pytest.raises(YAMLError) as info:
        (api.loads_all if multi else api.loads)(data, limits=limits)
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
def test_plain_scalar_resolves(api, text, want):
    same(one(api, f"[{text}]\n" if text else "- \n")[0], want)
    same(one(api, f"k: {text}\n")["k"], want)
    same(one(api, text + "\n"), want)


@pytest.mark.parametrize("text", STRINGS)
def test_plain_scalar_stays_a_string(api, text):
    got = one(api, f"- {text}\n" if text.strip() == text else f"- '{text}'\n")[0]
    same(got, text)


def test_nan_is_nan(api):
    for t in (".nan", ".NaN", ".NAN"):
        assert math.isnan(one(api, t))


def test_negative_zero_keeps_its_sign(api):
    assert math.copysign(1, one(api, "-0.0")) == -1


def test_quoted_and_block_scalars_are_strings(api):
    got = one(api, '- \'1\'\n- "true"\n- ""\n- |\n  null\n- >\n  1.5\n')
    assert got == ["1", "true", "", "null\n", "1.5\n"]
    assert all(type(x) is str for x in got)


def test_values_are_exact_python_types(api):
    got = one(api, "[1, 1.0, true, null, x]")
    assert [type(x) for x in got] == [int, float, bool, type(None), str]


# -- tags ----------------------------------------------------------------------------------


def test_explicit_core_tags_decide_the_type(api):
    text = (
        "- !!float 1\n- !!int '3'\n- !!int 0x10\n- !!null ''\n- !!bool 'true'\n- !!str 0o10\n"
        "- !!float .inf\n- !!int -7\n- !!str\n- !!null\n- !!float '1e3'\n"
    )
    got = one(api, text)
    want = [1.0, 3, 16, None, True, "0o10", math.inf, -7, "", None, 1000.0]
    assert len(got) == len(want)
    for g, w in zip(got, want, strict=True):
        same(g, w)


def test_collection_tags_are_transparent(api):
    assert one(api, "!!map {a: !!seq [1, 2]}") == {"a": [1, 2]}
    assert one(api, "! [1]") == [1]


def test_nonspecific_tag_makes_a_string(api):
    same(one(api, "! 12"), "12")


@pytest.mark.parametrize(
    "text",
    ["!!int twelve", "!!int 1.5", "!!float abc", "!!bool yes", "!!null x", "!!int ''",
     "!!float 0x10", "!!map a", "!!seq a", "!!str [a]", "!!int {a: 1}", "!!map [a]",
     "!!seq {a: 1}", "!!binary !", "!!binary a"],
)  # fmt: skip
def test_tag_that_does_not_fit_is_an_error(api, text):
    err = fails(api, text + "\n", "syntax")
    assert err.line == 1


def test_unknown_tags_are_kept_not_constructed(api):
    text = (
        "- !color red\n- !!python/object/apply:os.system [echo]\n- !!set {a}\n"
        "- !<tag:example.com,2000:x> [1]\n- !local {a: 1}\n- !!timestamp 2001-12-14\n"
    )
    got = one(api, text)
    assert got[0] == Tagged("!color", "red")
    assert got[1] == Tagged("tag:yaml.org,2002:python/object/apply:os.system", ["echo"])
    assert got[2] == Tagged("tag:yaml.org,2002:set", {"a": None})
    assert got[3] == Tagged("tag:example.com,2000:x", [1])
    assert got[4] == Tagged("!local", {"a": 1})
    assert got[5] == Tagged("tag:yaml.org,2002:timestamp", "2001-12-14")


def test_text_under_an_unknown_tag_is_not_resolved(api):
    got = one(api, "!x 12")
    assert got.value == "12"
    assert type(got.value) is str


def test_binary_is_bytes(api):
    got = one(api, "!!binary |\n  aGVs\n  bG8=\n")
    same(got, b"hello")
    same(one(api, "!!binary 'aGk='"), b"hi")
    same(one(api, "!!binary ''"), b"")


def test_tagged_is_immutable_and_hashable():
    t = Tagged("!a", "x")
    assert {t: 1}[Tagged("!a", "x")] == 1
    assert t != Tagged("!b", "x")
    with pytest.raises(AttributeError):
        t.tag = "!c"  # type: ignore[misc]


# -- mappings and keys ---------------------------------------------------------------------


def test_mapping_keeps_document_order(api):
    got = one(api, "z: 1\na: 2\nm: 3\n")
    assert list(got) == ["z", "a", "m"]


def test_merge_key_is_an_ordinary_key(api):
    got = one(api, "base: &b {x: 1}\nuse:\n  <<: *b\n  y: 2\nflow: {<<: *b}\n")
    assert got["use"] == {"<<": {"x": 1}, "y": 2}
    assert got["flow"] == {"<<": {"x": 1}}


def test_non_string_keys(api):
    got = one(api, "1: a\nnull: b\n2.5: c\n'1': d\n")
    assert got == {1: "a", None: "b", 2.5: "c", "1": "d"}


def test_empty_key_is_null(api):
    assert one(api, "? \n: v\n") == {None: "v"}


@pytest.mark.parametrize(
    "text",
    ["a: 1\na: 2\n", "{a: 1, a: 2}", "1: a\n0x1: b\n", "1: a\n01: b\n",
     "null: a\n~: b\n", "true: a\nTrue: b\n", ".nan: a\n.NaN: b\n",
     "? [1, 2]\n: a\n? [1, 2]\n: b\n",
     "? {a: 1}\n: x\n? {a: 1}\n: y\n", "1.0: a\n1e0: b\n", "? a\n: 1\n? 'a'\n: 2\n"],
)  # fmt: skip
def test_duplicate_keys_are_an_error(api, text):
    fails(api, text, "duplicate-key")


def test_duplicate_key_points_at_the_second_key(api):
    err = fails(api, "a: 1\nb: 2\nc: 3\nb: 4\n", "duplicate-key")
    assert (err.line, err.column) == (4, 1)
    err = fails(api, "x:\n  {p: 1, q: 2, p: 3}\n", "duplicate-key")
    assert (err.line, err.column) == (2, 16)


def test_same_text_different_type_is_not_a_duplicate(api):
    assert one(api, "'1': a\n1: b\n") == {"1": "a", 1: "b"}


@pytest.mark.parametrize("text", ["1: a\n1.0: b\n", "1: a\ntrue: b\n", "0: a\nfalse: b\n"])
def test_keys_python_cannot_tell_apart_are_refused(api, text):
    # A dict holds 1, 1.0 and True under one key; losing one silently would be worse.
    fails(api, text, "other")


@pytest.mark.parametrize(
    "text", ["1e400: a\n.inf: b\n", "1e400: a\n2e400: b\n", "1e-400: a\n2e-400: b\n"]
)
def test_float_keys_outside_a_double_are_a_limit(api, text):
    fails(api, text, "limit")


def test_float_values_outside_a_double_are_fine(api):
    assert one(api, "[1e400, -1e400, 1e-400]") == [math.inf, -math.inf, 0.0]
    assert one(api, "0.0: a\n1e0: b\n") == {0.0: "a", 1.0: "b"}


def test_sequence_key_is_a_tuple(api):
    got = one(api, "? [a, [b, 1]]\n: v\n")
    assert got == {("a", ("b", 1)): "v"}


def test_mapping_key_is_a_frozen_mapping(api):
    got = one(api, "? {a: 1, b: [x]}\n: v\n")
    (key,) = got
    assert isinstance(key, FrozenMapping)
    assert key == {"a": 1, "b": ("x",)}
    assert hash(key) == hash(FrozenMapping({"b": ("x",), "a": 1}))
    assert list(key) == ["a", "b"]
    with pytest.raises(TypeError):
        key["c"] = 1  # type: ignore[index]
    assert got[FrozenMapping({"a": 1, "b": ("x",)})] == "v"


def test_tagged_collection_key(api):
    got = one(api, "? !t [1, 2]\n: v\n")
    assert got == {Tagged("!t", (1, 2)): "v"}


def test_key_is_a_mapping_inside_a_key(api):
    got = one(api, "? [{a: [1]}, {b: {c: 2}}]\n: v\n")
    ((key, _),) = got.items()
    assert key[0]["a"] == (1,)
    assert key[1]["b"]["c"] == 2


def test_key_nesting_is_capped_not_recursed(api):
    deep = "? " + "[" * 100_000 + "]" * 100_000 + "\n: v\n"
    err = fails(api, deep, "limit", NONE)
    assert err.line == 1
    fails(api, "? " + "{a: " * 300 + "1" + "}" * 300 + "\n: v\n", "limit", NONE)


# -- aliases -------------------------------------------------------------------------------


def test_alias_to_a_collection_is_the_same_object(api):
    got = one(api, "a: &a [1, 2]\nb: *a\nc: [*a, *a]\nd: &d {k: v}\ne: *d\n")
    assert got["a"] is got["b"] is got["c"][0] is got["c"][1]
    assert got["d"] is got["e"]


def test_alias_to_a_scalar_keeps_the_resolved_value(api):
    got = one(api, "a: &x 12\nb: *x\nc: &y 'true'\nd: *y\n")
    same(got["b"], 12)
    same(got["d"], "true")


def test_anchor_on_a_tagged_node_keeps_the_result(api):
    got = one(api, "a: &x !!float 1\nb: *x\nc: &t !k [1]\nd: *t\n")
    same(got["b"], 1.0)
    assert got["d"] is got["c"]
    assert isinstance(got["d"], Tagged)


def test_anchor_may_be_redefined(api):
    got = one(api, "- &a 1\n- *a\n- &a 2\n- *a\n")
    assert got == [1, 1, 2, 2]


def test_alias_means_the_latest_anchor_even_inside_its_own_node(api):
    got = one(api, "a: &b\n  se: &b\n    1\ncopy: *b\n")
    assert got == {"a": {"se": 1}, "copy": 1}


def test_alias_inside_its_own_node_is_refused(api):
    err = fails(api, "a: &b 1\nc: &b [ *b ]\n", "syntax")
    assert (err.line, err.column) == (2, 9)
    fails(api, "&a {x: *a}", "syntax")


def test_anchors_do_not_cross_documents(api):
    assert many(api, "--- &a [1]\n--- [2]\n") == [[1], [2]]
    fails(api, "--- &a [1]\n--- *a\n", "syntax", multi=True)


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


def test_alias_bomb_loads_in_proportion_to_the_document(api):
    # Five levels of nine aliases would be 66,430 nodes written out; here they are 6 objects.
    got = one(api, nine_by_nine(5))
    assert distinct_objects(got) == 6
    assert got["e"][0] is got["d"]


def test_alias_used_as_a_key_is_charged(api):
    # Freezing a collection for use as a key copies it: that is where aliases cost.
    doc = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
    big = doc + "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\nk: {*c : 1}\n"
    assert len(one(api, big, Limits(alias_nodes=1_000_000))["k"]) == 1
    fails(api, big, "limit", Limits(alias_nodes=500))
    fails(api, big, "limit", Limits(alias_bytes=500))
    assert len(one(api, big, NONE)["k"]) == 1


def test_key_charges_add_up(api):
    doc = "a: &a [x, x, x, x, x, x, x, x, x, x]\nm1: {*a : 1}\nm2: {*a : 1}\nm3: {*a : 1}\n"
    lim = Limits(alias_nodes=25)
    fails(api, doc, "limit", lim)
    assert one(api, doc, Limits(alias_nodes=40))["m3"]


# -- limits and depth ----------------------------------------------------------------------


def test_float_with_a_huge_exponent_is_not_a_digit_problem(api):
    assert one(api, "1e" + "9" * 100_000) == math.inf
    assert one(api, "0." + "0" * 100_000 + "1") == 0.0


# -- streams -------------------------------------------------------------------------------


def test_a_second_document_that_is_empty_still_counts(api):
    fails(api, "a\n---\n", "other")


# -- characters only quoted scalars may hold -------------------------------------------------


def test_quoted_scalars_take_every_character_from_space_up(api):
    got = many(api, 'a: "x\x7fy"\n---\na: \'x\x80y\'\n---\n["\ufffe", "\x9f", \'\uffff\']\n')
    assert got == [{"a": "x\x7fy"}, {"a": "x\x80y"}, ["\ufffe", "\x9f", "\uffff"]]


def test_those_characters_are_still_refused_elsewhere(api):
    err = fails(api, "a: x\x7fy\n", "syntax")
    assert (err.line, err.column) == (1, 5)
    err = fails(api, 'a: "\x7f"\nb: z\ufffe\n', "syntax")
    assert (err.line, err.column) == (2, 5)
    fails(api, '"a\x01b"', "syntax")


def test_stand_in_characters_are_not_confused_with_real_ones(api):
    got = one(api, '["\U000f0000\x7f", "\U000f0001\x80"]')
    assert got == ["\U000f0000\x7f", "\U000f0001\x80"]


def test_the_event_stream_agrees(api):
    events = api.events_text('"a\x7fb"')
    assert events == ["+STR", "+DOC", '=VAL "a\x7fb', "-DOC", "-STR"]


def test_byte_order_mark_then_comment_after_a_document_end_marker(api):
    text = "--- a\n...\n\ufeff# note\n--- b\n"
    assert api.events_text(text) == [
        "+STR", "+DOC ---", "=VAL :a", "-DOC ...", "+DOC ---", "=VAL :b", "-DOC", "-STR",
    ]  # fmt: skip
    assert many(api, text) == ["a", "b"]
    # A mark in the middle of a line stays an error.
    fails(api, "a\ufeff# x\n", "syntax")


# -- deep keys that are compared, from a caller that is already deep -----------------------------

DEEP_KEYS = {
    "tagged mappings": "!t {a: " * 200 + "x" + "}" * 200,
    "sequences": "[" * 200 + "x" + "]" * 200,
    "mappings": "{a: " * 200 + "x" + "}" * 200,
    "mappings as keys": "{" * 200 + "x" + ": 1}" * 200,
}


def _from_depth(n, fn):
    if n:
        return _from_depth(n - 1, fn)
    return fn()


@pytest.mark.parametrize("key", DEEP_KEYS.values(), ids=DEEP_KEYS.keys())
def test_equal_deep_keys_never_end_in_a_recursion_error(api, key):
    # Finding the second key equal to the first compares them level by level; that must not
    # use up the Python frames the caller has left.
    text = f"? {key}\n: 1\n? {key}\n: 2\n"

    def load():
        with pytest.raises(YAMLError) as info:
            api.loads(text)
        return info.value

    err = _from_depth(600, load)
    assert err.category in ("duplicate-key", "limit")


def test_equal_deep_keys_are_found_as_duplicates_when_the_caller_is_shallow(api):
    key = DEEP_KEYS["tagged mappings"]
    fails(api, f"? {key}\n: 1\n? {key}\n: 2\n", "duplicate-key")
    other = key.replace("x}", "y}", 1)
    assert len(one(api, f"? {key}\n: 1\n? {other}\n: 2\n")) == 2


def test_tagged_and_frozen_mapping_compare_without_recursion():
    inner = "x"
    a = b = None
    for _ in range(5000):
        a = Tagged("!t", FrozenMapping({"a": a if a is not None else inner}))
        b = Tagged("!t", FrozenMapping({"a": b if b is not None else inner}))
    assert a == b
    assert hash(a) == hash(b)
    c = Tagged("!t", FrozenMapping({"a": "y"}))
    assert a != c


def test_a_tagged_collection_is_unhashable_until_frozen():
    t = Tagged("!t", [1])
    with pytest.raises(TypeError):
        hash(t)
    assert t == Tagged("!t", [1])
    assert hash(Tagged("!t", (1,))) == hash(Tagged("!t", (1,)))


# -- block and quoted scalars at the edges of their indentation -----------------------------------


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("k: >\n \n  \n   \n# c\n", {"k": ""}),
        ("k: |\n   \nb: c\n", {"k": "", "b": "c"}),
        ("- |\n    \n- a\n", ["", "a"]),
        ("k: |+\n \n  \n   \n# c\n", {"k": "\n\n\n"}),
        ("k: |\n  a\n   \nb: c\n", {"k": "a\n \n", "b": "c"}),
    ],
)
def test_a_block_scalar_of_only_blank_lines_is_empty(api, text, want):
    # With no content line the widest blank line sets the indentation, so none of them is text.
    assert one(api, text) == want


@pytest.mark.parametrize(
    "text",
    ["a: 'old: 1\n'\nb: 1\n", 'a: "x\n"\nb: 1\n', "- 'x\n'\n", "k:\n  - 'x\n  '\n"],
)
def test_the_closing_quote_on_a_new_line_must_be_indented(api, text):
    err = fails(api, text, "syntax")
    assert err.line in (1, 2, 3)


def test_the_closing_quote_may_be_at_the_margin_where_nothing_is_nested(api):
    assert one(api, "'x\n'\n") == "x "
    assert one(api, "[a, 'x\n']") == ["a", "x "]
    assert one(api, "a: 'x\n  '\n") == {"a": "x "}


def test_a_tab_before_properties_does_not_indent_what_follows(api):
    assert one(api, "a: \t&b\n se: 1\n") == {"a": {"se": 1}}
    fails(api, "a: \t&b\n\tse: 1\n", "syntax")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("a: 1\n...\n\t--- b\n", [{"a": 1}, "--- b"]),
        ("a: 1\n---\n\t--- b\n", [{"a": 1}, "--- b"]),
        ("---\n\t...\n", ["..."]),
    ],
)
def test_a_marker_after_a_tab_is_text_not_a_marker(api, text, want):
    assert many(api, text) == want


def test_a_marker_after_a_tab_cannot_end_a_block_collection(api):
    fails(api, "a: 1\n\t--- b\n", "syntax")


@pytest.mark.parametrize(
    "text", ["a: 1\n\ufeff\n--- b\n", "a: 1\n\ufeff# c\n\ufeff\n--- b\n", "- 1\n\ufeff--- b\n"]
)
def test_a_byte_order_mark_may_open_the_prefix_of_the_next_document(api, text):
    assert len(many(api, text)) == 2


def test_a_byte_order_mark_inside_a_document_is_refused(api):
    fails(api, "a: 1\n\ufeff\nb: 2\n", "syntax")


def test_three_dashes_before_a_closing_quote_are_no_marker(api):
    assert many(api, '---\n"x\n---"\n') == ["x ---"]
    assert one(api, "'\n...'\n") == " ..."


def test_the_first_error_in_the_text_wins_over_a_later_one(api):
    # A reader that builds as it parses meets these before the syntax error that follows them.
    err = fails(api, "{b: x, b: y\n", "duplicate-key")
    assert (err.line, err.column) == (1, 8)
    fails(api, "a: 1\nb: 2\na: 3\n- x\n", "duplicate-key")
    fails(api, nine_by_nine() + "x: [\n", "limit")
    assert fails(api, "x: [\n", "syntax").line is not None
