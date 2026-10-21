"""Reading YAML into Python objects: the type mapping, errors and limits (both backends)."""

import math

import pytest

from oneyaml import FrozenMapping, LimitError, Limits, ParseError, Tagged, YAMLError

# ---- scalars ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("~", None),
        ("null", None),
        ("Null", None),
        ("NULL", None),
        ("true", True),
        ("True", True),
        ("FALSE", False),
        ("42", 42),
        ("-7", -7),
        ("+7", 7),
        ("007", 7),
        ("0o17", 15),
        ("0x1F", 31),
        ("1.5", 1.5),
        ("-1.5e3", -1500.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("1e3", 1000.0),
        (".inf", math.inf),
        ("-.INF", -math.inf),
        # Not Core: these stay text.
        ("yes", "yes"),
        ("No", "No"),
        ("on", "on"),
        ("0b101", "0b101"),
        ("1_000", "1_000"),
        ("-0x1F", "-0x1F"),
        ("0o", "0o"),
        ("1e", "1e"),
        ("+.nan", "+.nan"),
        ("2024-01-15", "2024-01-15"),
        ("1:30", "1:30"),
        ("0755", 755),
        ("~x", "~x"),
        ("nulll", "nulll"),
    ],
)
def test_plain_scalars(api, text, expected):
    got = api.loads(f"v: {text}\n")["v"]
    assert got == expected
    assert type(got) is type(expected)


def test_nan(api):
    assert math.isnan(api.loads("v: .nan")["v"])
    assert math.isnan(api.loads("v: .NaN")["v"])


def test_negative_zero_float_keeps_its_sign(api):
    assert math.copysign(1, api.loads("-0.0")) == -1


def test_quoted_scalars_are_strings(api):
    assert api.loads("['1', \"true\", '', \"null\"]") == ["1", "true", "", "null"]


def test_block_scalars(api):
    assert api.loads("a: |\n  x\n  y\nb: >-\n  p\n  q\n") == {"a": "x\ny\n", "b": "p q"}


def test_escapes(api):
    assert api.loads(r'"\u00e9\t\x41\U0001F600\n"') == "é\tA😀\n"


def test_explicit_tags_force_the_type(api):
    got = api.loads("[!!str 1, !!int '12', !!float 3, !!null '', !!bool true, !!str ]")
    assert got == ["1", 12, 3.0, None, True, ""]
    assert type(got[2]) is float


@pytest.mark.parametrize("text", ["!!int abc", "!!float x", "!!bool maybe", "!!null 1"])
def test_explicit_tag_that_does_not_fit_is_an_error(api, text):
    with pytest.raises(YAMLError) as e:
        api.loads(text)
    assert e.value.line == 1


def test_binary_is_bytes(api):
    assert api.loads("!!binary aGVsbG8=") == b"hello"
    assert api.loads("!!binary |\n  aGVs\n  bG8=\n") == b"hello"
    with pytest.raises(YAMLError):
        api.loads("!!binary not-base64!")


def test_unknown_tags_are_kept_never_constructed(api):
    assert api.loads("!color red") == Tagged("!color", "red")
    assert api.loads("!!set {a}") == Tagged("tag:yaml.org,2002:set", {"a": None})
    assert api.loads("!<tag:example.com,2024:x> [1]") == Tagged("tag:example.com,2024:x", [1])
    got = api.loads("!!python/object/apply:os.system [echo]")
    assert got == Tagged("tag:yaml.org,2002:python/object/apply:os.system", ["echo"])
    assert api.loads("%TAG !e! tag:example.com,2024:\n---\n!e!x 1") == Tagged(
        "tag:example.com,2024:x", "1"
    )


def test_standard_collection_tags_are_transparent(api):
    assert api.loads("!!map {a: !!seq [1]}") == {"a": [1]}
    assert api.loads("! x") == "x"
    with pytest.raises(YAMLError):
        api.loads("!!seq {a: 1}")
    with pytest.raises(YAMLError):
        api.loads("!!map [1]")


# ---- integers --------------------------------------------------------------------------------


def test_integers_beyond_64_bits_are_exact(api):
    assert api.loads("v: 123456789012345678901234567890") == {"v": 123456789012345678901234567890}
    assert api.loads("-340282366920938463463374607431768211457") == -(2**128) - 1
    assert api.loads("0xFFFFFFFFFFFFFFFFFFFFFFFF") == 2**96 - 1
    assert api.loads("0o7777777777777777777777777") == 8**25 - 1
    assert api.loads("9223372036854775808") == 2**63
    assert api.loads("-9223372036854775808") == -(2**63)


def test_digit_cap_is_a_limit_error(api):
    ok = "1" * 4300
    assert api.loads(ok) == int(ok)  # exactly at the cap, which the interpreter allows too
    with pytest.raises(LimitError) as e:
        api.loads("1" * 4301)
    assert e.value.category == "limit"
    assert (e.value.line, e.value.column) == (1, 1)


def test_digit_cap_is_configurable_and_can_be_off(api):
    big = "9" * 5000
    expected = 10**5000 - 1  # the interpreter's own cap would refuse int(big)
    assert api.loads(big, limits=Limits(int_digits=6000)) == expected
    # Past the interpreter's own default cap the library still converts exactly.
    assert api.loads(big, limits=Limits.none()) == expected
    with pytest.raises(LimitError):
        api.loads("123456", limits=Limits(int_digits=5))


# ---- collections, keys, aliases ------------------------------------------------------------------


def test_mapping_order_is_document_order(api):
    d = api.loads("z: 1\na: 2\nm: 3\n")
    assert list(d) == ["z", "a", "m"]


def test_nested_and_flow(api):
    text = "a: [1, {b: c}, [d]]\ne: {f: [g, h]}\ni: []\nj: {}\n"
    assert api.loads(text) == {
        "a": [1, {"b": "c"}, ["d"]],
        "e": {"f": ["g", "h"]},
        "i": [],
        "j": {},
    }


def test_non_string_keys(api):
    assert api.loads("{null: a, 2: b, 1.5: c, x: d}") == {None: "a", 2: "b", 1.5: "c", "x": "d"}
    assert api.loads("? \n: v") == {None: "v"}
    assert api.loads("'1': a\n1.5: b") == {"1": "a", 1.5: "b"}


def test_complex_keys_are_hashable(api):
    d = api.loads("? [1, 2]\n: a\n? {x: 1}\n: b\n? [[3], {y: [4]}]\n: c\n")
    assert d[(1, 2)] == "a"
    assert d[FrozenMapping({"x": 1})] == "b"
    assert d[((3,), FrozenMapping({"y": (4,)}))] == "c"
    (key,) = (k for k in d if isinstance(k, FrozenMapping) and "x" in k)
    assert hash(key) == hash(FrozenMapping({"x": 1}))
    with pytest.raises(TypeError):
        key["x"] = 2  # type: ignore[index]


def test_tagged_key(api):
    d = api.loads("? !t [1]\n: v")
    assert d[Tagged("!t", (1,))] == "v"


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2",
        "{a: 1, a: 2}",
        "1: a\n0x1: b",
        "? [1]\n: a\n? [1]\n: b",
        "null: a\n~: b",
        "'a': 1\na: 2",
        "a: 1\nb:\n  c: 1\n  c: 2",
    ],
)
def test_duplicate_keys_are_an_error(api, text):
    with pytest.raises(ParseError) as e:
        api.loads(text)
    assert e.value.category == "duplicate-key"
    assert e.value.line is not None


def test_equal_in_python_but_not_in_yaml_is_refused_not_dropped(api):
    # YAML keeps 1, 1.0 and true apart; a dict cannot, so the load fails instead of losing one.
    with pytest.raises(YAMLError) as e:
        api.loads("1: a\ntrue: b")
    assert e.value.category == "other"


def test_merge_key_is_an_ordinary_key(api):
    assert api.loads("base: &b {x: 1}\nuse:\n  <<: *b\n  y: 2\n") == {
        "base": {"x": 1},
        "use": {"<<": {"x": 1}, "y": 2},
    }


def test_aliases_share_the_object(api):
    d = api.loads("a: &x [1, 2]\nb: *x\nc: &s hello\nd: *s\ne: {k: *x}\n")
    assert d["a"] is d["b"] is d["e"]["k"]
    assert d["c"] == d["d"] == "hello"
    d["a"].append(3)
    assert d["b"] == [1, 2, 3]


def test_alias_to_a_scalar_keeps_its_type(api):
    assert api.loads("[&a 12, *a, &b 1.5, *b]") == [12, 12, 1.5, 1.5]


def test_alias_as_a_key_is_frozen(api):
    d = api.loads("a: &x [1, 2]\n? *x\n: v\nb: &m {p: [q]}\n? *m\n: w\n")
    assert d[(1, 2)] == "v"
    assert d[FrozenMapping({"p": ("q",)})] == "w"
    assert d["a"] == [1, 2]


def test_redefined_anchor_names_the_latest_node(api):
    assert api.loads("a: &b\n se: &b\n   1\ncopy: *b\n") == {"a": {"se": 1}, "copy": 1}
    assert api.loads("[&a 1, &a 2, *a]") == [1, 2, 2]


def test_anchors_do_not_cross_documents(api):
    with pytest.raises(YAMLError):
        api.loads_all("--- &a 1\n--- *a\n")


def test_alias_errors(api):
    with pytest.raises(YAMLError) as e:
        api.loads("a: *nope")
    assert (e.value.line, e.value.column) == (1, 4)
    # An alias inside the node that defines its anchor is a cycle, never an earlier node.
    with pytest.raises(YAMLError):
        api.loads("a: &b 1\nc: &b [ *b ]")
    with pytest.raises(YAMLError):
        api.loads("&a [*a]")


# ---- documents and input ---------------------------------------------------------------------


def test_documents(api):
    assert api.loads("") is None
    assert api.loads("# only a comment\n") is None
    assert api.loads("---\n") is None
    assert api.loads_all("") == []
    assert api.loads_all("---\na\n---\nb\n...\n---\n") == ["a", "b", None]
    assert api.loads_all("a: 1") == [{"a": 1}]


def test_loads_of_several_documents_names_loads_all(api):
    with pytest.raises(YAMLError) as e:
        api.loads("a\n---\nb\n")
    assert "loads_all" in e.value.message
    assert e.value.line == 2


def test_syntax_error_after_the_first_document_is_still_reported(api):
    with pytest.raises(YAMLError) as e:
        api.loads("a: [1,\n")
    assert e.value.category == "syntax"


def test_file_functions(api, tmp_path):
    p = tmp_path / "a.yaml"
    p.write_bytes(b"a: 1\n---\nb: 2\n")
    with open(p, "rb") as f:
        assert api.load_all(f) == [{"a": 1}, {"b": 2}]
    with open(p, encoding="utf-8") as f:
        assert api.load_all(f) == [{"a": 1}, {"b": 2}]
    p.write_text("x: [1, 2]\n")
    with open(p, "rb") as f:
        assert api.load(f) == {"x": [1, 2]}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be",
                                      "utf-32", "utf-32-le", "utf-32-be"])  # fmt: skip
def test_bytes_in_every_encoding(api, encoding):
    text = "k: é😀\nl: [1, 2]\n"
    data = text.encode(encoding)
    if encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
        # No byte order mark: the encoding is told from the first characters (section 5.2).
        pass
    assert api.loads(data) == {"k": "é😀", "l": [1, 2]}


def test_str_and_bytes_agree(api):
    text = "a: [1, é]\n"
    assert api.loads(text) == api.loads(text.encode()) == {"a": [1, "é"]}


def test_byte_order_mark_in_str_is_ignored_like_in_bytes(api):
    assert api.loads("\ufeffa: 1") == {"a": 1}


def test_bad_encoding_is_an_encoding_error_with_a_position(api):
    with pytest.raises(ParseError) as e:
        api.loads(b"a: 1\nb: \xff\xfe\n")
    assert e.value.category == "encoding"
    assert (e.value.line, e.value.column) == (2, 4)
    with pytest.raises(YAMLError) as e:
        api.loads(b"a: \xc3")
    assert e.value.category == "encoding"


def test_lone_surrogate_in_str_is_an_encoding_error(api):
    with pytest.raises(YAMLError) as e:
        api.loads("a: \ud800")
    assert e.value.category == "encoding"


def test_bytearray_and_memoryview(api):
    assert api.loads(bytearray(b"a: 1")) == {"a": 1}
    assert api.loads(memoryview(b"a: 1")) == {"a": 1}


# ---- errors ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "line", "column"),
    [
        ("a: [1, 2\n", 2, 1),
        ("a: [1,\n  2\n", 3, 1),
        ("- a\nb: 1\n", 2, 1),
        ("a: 1\n- b\n", 2, 1),
    ],
)
def test_syntax_errors_have_one_based_positions(api, text, line, column):
    with pytest.raises(ParseError) as e:
        api.loads(text)
    err = e.value
    assert err.category == "syntax"
    assert (err.line, err.column) == (line, column)
    assert isinstance(err.message, str)
    assert isinstance(err, ValueError)
    assert f"line {line}" in str(err)


def test_position_counts_characters_not_bytes(api):
    with pytest.raises(YAMLError) as e:
        api.loads("é😀: [a\n")
    assert e.value.category == "syntax"
    assert e.value.line == 2


def test_error_offset_is_a_character_offset(api):
    with pytest.raises(YAMLError) as e:
        api.loads("é: 1\nx: 'y\n")
    assert e.value.offset is not None
    assert "é: 1\nx: 'y\n"[e.value.offset - 1 : e.value.offset + 1] != ""


def test_error_pickles(api):
    import pickle

    with pytest.raises(YAMLError) as e:
        api.loads("a: [")
    again = pickle.loads(pickle.dumps(e.value))
    assert type(again) is type(e.value)
    assert (again.category, again.line, again.column) == (
        e.value.category,
        e.value.line,
        e.value.column,
    )


# ---- limits ----------------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [100_000, 1_000_000])
def test_deep_flow_nesting_is_a_limit_error(api, depth):
    with pytest.raises(LimitError) as e:
        api.loads("[" * depth)
    assert e.value.category == "limit"


def test_deep_block_nesting_is_a_limit_error(api):
    text = "".join(" " * i + "- \n" for i in range(0, 4000)) + " " * 4000 + "- x\n"
    with pytest.raises(LimitError):
        api.loads(text, limits=Limits(depth=1000))


def test_deep_nesting_within_the_limit_loads_without_recursion(api):
    depth = 9000
    got = api.loads("[" * depth + "]" * depth)
    n = 0
    while got:
        got = got[0]
        n += 1
    assert n == depth - 1
    deep_map = api.loads("{a: " * 5000 + "1" + "}" * 5000)
    assert deep_map is not None


def test_depth_limit_is_exact(api):
    api.loads("[[[]]]", limits=Limits(depth=3))
    with pytest.raises(LimitError):
        api.loads("[[[[]]]]", limits=Limits(depth=3))


def test_untrusted_limits(api):
    with pytest.raises(LimitError):
        api.loads("[" * 2000, limits=Limits.untrusted())
    with pytest.raises(LimitError):
        api.loads_all("---\n" * 10_001, limits=Limits.untrusted())


def test_size_limits(api):
    with pytest.raises(LimitError) as e:
        api.loads("a: " + "x" * 100, limits=Limits(input_size=50))
    assert e.value.category == "limit"
    with pytest.raises(LimitError):
        api.loads(b"a: " + b"x" * 100, limits=Limits(input_size=50))
    with pytest.raises(LimitError):
        api.loads("a: " + "x" * 100, limits=Limits(scalar_size=50))
    with pytest.raises(LimitError):
        api.loads_all("a\n---\nb\n---\nc\n", limits=Limits(documents=2))
    assert api.loads_all("a\n---\nb\n", limits=Limits(documents=2)) == ["a", "b"]


BOMB = "a: &a [x, x, x, x, x, x, x, x, x]\n" + "".join(
    f"{n}: &{n} [{', '.join(['*' + p] * 9)}]\n" for p, n in zip("abcdefgh", "bcdefghi", strict=True)
)


def test_alias_bomb_is_a_limit_error_and_fast(api):
    with pytest.raises(LimitError) as e:
        api.loads(BOMB)
    assert "alias" in e.value.message
    assert e.value.line is not None


def test_alias_budget_is_configurable(api):
    text = "a: &a [x, x, x]\nb: [*a, *a, *a]\n"
    assert api.loads(text, limits=Limits(alias_nodes=100))["b"][0] is not None
    with pytest.raises(LimitError):
        api.loads(text, limits=Limits(alias_nodes=5))
    # None turns the budget off: the object graph is shared, so even the bomb loads.
    got = api.loads(BOMB, limits=Limits(alias_nodes=None, alias_bytes=None))
    assert got["i"][0] is got["h"]


def test_alias_byte_budget(api):
    text = "a: &a " + "x" * 1000 + "\nb: [" + ", ".join(["*a"] * 100) + "]\n"
    with pytest.raises(LimitError):
        api.loads(text, limits=Limits(alias_bytes=5000))
    assert len(api.loads(text, limits=Limits(alias_bytes=500_000))["b"]) == 100


def test_alias_bomb_as_a_key_cannot_expand(api):
    text = (
        "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n? *b\n: v\n"
    )
    assert len(api.loads(text, limits=Limits())) == 3
    with pytest.raises(LimitError):
        api.loads(text, limits=Limits(alias_nodes=20))


def test_alias_nesting_counts_toward_depth(api):
    text = "a: &a [[[[x]]]]\nb: [[[*a]]]\n"
    with pytest.raises(LimitError):
        api.loads(text, limits=Limits(depth=6))
    api.loads(text, limits=Limits(depth=8))


# ---- big input -----------------------------------------------------------------------------
# Past a certain size the native backend reads in stages on several threads; the answer must
# not depend on it.


def _records(n):
    text = []
    value = []
    for i in range(n):
        text.append(
            f"- id: {i}\n  name: item {i}\n  tags: [a{i % 7}, b, {{k: {i}}}]\n"
            f"  anchor: &a{i} [x, {i}]\n  again: *a{i}\n  note: |\n    line one\n    line {i}\n"
        )
        shared = ["x", i]
        value.append(
            {
                "id": i,
                "name": f"item {i}",
                "tags": [f"a{i % 7}", "b", {"k": i}],
                "anchor": shared,
                "again": shared,
                "note": f"line one\nline {i}\n",
            }
        )
    return "".join(text), value


def test_big_input_matches_the_expected_value(api):
    text, expected = _records(3000)
    assert len(text) > 300_000
    got = api.loads(text)
    assert got == expected
    assert got[1234]["anchor"] is got[1234]["again"]
    assert api.loads(text.encode()) == expected
    assert api.loads_all(text + "---\n" + text) == [expected, expected]


def test_big_input_error_position(api):
    text, _ = _records(3000)
    lines = text.count("\n")
    with pytest.raises(ParseError) as e:
        api.loads(text + "bad: [\n")
    assert (e.value.category, e.value.line) == ("syntax", lines + 1)
    with pytest.raises(ParseError) as e:
        api.loads("a: [\n" + text)
    assert e.value.category == "syntax"
    with pytest.raises(ParseError) as e:
        api.loads(text + "- id: 1\n  id: 2\n")
    assert e.value.category == "duplicate-key"
    assert e.value.line == lines + 2


def test_big_input_aliases_across_batches(api):
    body = "".join(f"k{i}: *top\n" for i in range(40_000))
    got = api.loads("top: &top [1, 2]\n" + body)
    assert got["k39999"] is got["top"]
    with pytest.raises(LimitError):
        api.loads("top: &top [1, 2]\n" + body, limits=Limits(alias_nodes=1000))


def test_big_input_with_deep_nesting(api):
    text, _ = _records(2000)
    deep = "[" * 5000 + "]" * 5000
    got = api.loads(text + "- " + deep + "\n")
    assert len(got) == 2001
    with pytest.raises(LimitError):
        api.loads(text + "- " + "[" * 20_000 + "\n", limits=Limits(depth=10_000))
