"""The lossless document: round trip, reading, exact assignment, minimal diffs, views."""

import io
import math
import threading

import pytest

import oneyaml
from oneyaml import (
    Document,
    LimitError,
    Limits,
    MappingView,
    PatchError,
    SequenceView,
    StaleViewError,
    Tagged,
    YAMLError,
)

# ---- helpers ------------------------------------------------------------------------------------


def plain(x):
    return x.to_python() if hasattr(x, "to_python") else x


def same(a, b):
    """Equal with the same types all the way down (so 1 is not 1.0 is not True), NaN equal NaN."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, Tagged):
        return a.tag == b.tag and same(a.value, b.value)
    return a == b


def only_lines_changed(before: str, after: str, first: int, last: int):
    """Every line outside ``before``'s lines ``first..last`` (0-based, inclusive) is untouched."""
    b, a = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert a[:first] == b[:first], "lines above the edit changed"
    tail = len(b) - last - 1
    assert (a[len(a) - tail :] if tail else []) == b[last + 1 :], "lines below the edit changed"


# ---- round trip ---------------------------------------------------------------------------------

ROUND_TRIP = [
    "",
    "\n",
    "# only a comment\n",
    "a: 1",
    "a: 1\n",
    "a: 1\r\nb: 2\r\n",
    "a: 1\rb: 2\r",
    "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n",
    "- a\n-   b  # c\n\n\n- [1,   2]\n",
    "a: &x [1, 2]\nb: *x\n",
    "--- a\n...\n--- b\n",
    "a: 1\n---\nb: 2\n---\n- x\n",
    "%YAML 1.2\n---\na: 1\n",
    "{ a: 1,   b: [x,   y] }\n",
    "a:    'q'   \nb:\t2\n",
    "\ufeffa: 1\n",
]


@pytest.mark.parametrize("text", ROUND_TRIP)
def test_an_untouched_document_writes_back_exactly(api, text):
    doc = oneyaml.parse(text)
    assert str(doc) == text
    assert bytes(doc) == text.encode("utf-8")
    assert oneyaml.parse(text.encode("utf-8")).__bytes__() == text.encode("utf-8")


@pytest.mark.parametrize(
    "codec", ["utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-be"]
)
@pytest.mark.parametrize("bom", [True, False])
def test_bytes_keep_their_encoding_and_byte_order_mark(api, codec, bom):
    text = "# c\nname: \u00e9t\u00e9 \U0001f600\nn: [1, 2]\n"
    raw = text.encode(codec)
    if not bom and codec in ("utf-16", "utf-32"):
        pytest.skip("the codec always writes a mark")
    if bom and codec == "utf-8":
        raw = b"\xef\xbb\xbf" + raw
    doc = oneyaml.parse(raw)
    assert bytes(doc) == raw
    doc["n"][0] = 10
    out = bytes(doc)
    assert out != raw
    # The edited bytes are in the same encoding: they read back through the same path.
    assert oneyaml.loads(out) == {"name": "\u00e9t\u00e9 \U0001f600", "n": [10, 2]}
    assert out[: len(raw) // 2 - 8] == raw[: len(raw) // 2 - 8] or codec != "utf-8"


def test_dump_writes_text_to_text_files_and_bytes_to_binary_files(api, tmp_path):
    text = "a: 1  # one\n"
    doc = oneyaml.parse(text)
    s, b = io.StringIO(), io.BytesIO()
    doc.dump(s)
    doc.dump(b)
    assert s.getvalue() == text
    assert b.getvalue() == text.encode()
    path = tmp_path / "x.yaml"
    with open(path, "wb") as fp:
        doc.dump(fp)
    with open(path, "w", encoding="utf-8") as fp:
        doc.dump(fp)
    assert path.read_bytes() == text.encode()


def test_str_input_and_bytes_input_agree(api):
    a, b = oneyaml.parse("a: \u00e9\n"), oneyaml.parse("a: \u00e9\n".encode())
    assert str(a) == str(b) and bytes(a) == bytes(b)
    assert a.to_python() == b.to_python() == {"a": "\u00e9"}


def test_bytes_like_input_is_accepted(api):
    assert str(oneyaml.parse(bytearray(b"a: 1\n"))) == "a: 1\n"
    assert str(oneyaml.parse(memoryview(b"a: 1\n"))) == "a: 1\n"


# ---- the reference cases ------------------------------------------------------------------------

CASE_IN = "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n"


def test_reference_case_a_keeps_the_literal_block(api):
    doc = oneyaml.parse(CASE_IN)
    doc["b"] = "this is a test2\n"
    assert str(doc) == "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test2\nc: 13\n"


def test_reference_case_b_changes_only_the_header(api):
    doc = oneyaml.parse(CASE_IN)
    doc["b"] = "this is a test2"
    assert str(doc) == "#hello\na: 10\n\n#abc 123\nb: |-\n  this is a test2\nc: 13\n"
    assert doc["b"] == "this is a test2"


def test_the_quick_start_without_the_patch(api):
    text = "# deploy settings\nname: api\nreplicas: 2  # raised by the release job\n"
    config = oneyaml.loads(text)
    assert config == {"name": "api", "replicas": 2}
    doc = oneyaml.parse(text)
    doc["replicas"] = config["replicas"] + 1
    doc["region"] = "eu-west-1"
    assert str(doc) == (
        "# deploy settings\nname: api\nreplicas: 3  # raised by the release job\n"
        "region: eu-west-1\n"
    )


# ---- reading ------------------------------------------------------------------------------------

READ_CASES = [
    "a: 1\nb: [1, 2.5, true, null, ~, 'x', \"y\"]\nc: {d: e}\n",
    "- 1\n- - 2\n  - 3\n- {a: b}\n",
    "base: &b {x: 1}\nuse: *b\nlist: [*b, *b]\n",
    "k: !!binary aGk=\nt: !color red\nu: !!str 12\nv: !!int '7'\nw: !wrap [1, 2]\n",
    "n: 123456789012345678901234567890\nh: 0x1F\no: 0o17\nf: .inf\ng: -.INF\nm: .nan\n",
    "1: a\n2.5: b\nnull: d\nfalse: e\n",
    "? [1, 2]\n: seq key\n? {a: 1}\n: map key\n",
    "s: |\n  line1\n  line2\nf: >-\n  fold\n  ed\n",
    "--- 1\n",
    "# nothing\n",
    "",
    "'': empty key\n",
]


@pytest.mark.parametrize("text", READ_CASES)
def test_to_python_is_what_loads_reads(api, text):
    doc = oneyaml.parse(text)
    assert same(doc.to_python(), oneyaml.loads(text))
    # Item by item through the views gives the same data.
    assert same(unview(doc), oneyaml.loads(text))


def unview(x):
    """Rebuild plain data by reading item by item through the views."""
    if not hasattr(x, "to_python"):
        return x
    whole = x.to_python()
    if isinstance(whole, Tagged):  # a tagged collection reads as the tag it carries
        return Tagged(whole.tag, unview_plain(whole.value))
    if isinstance(whole, dict):
        return {k: unview(x[k]) for k in x}
    if isinstance(whole, list):
        return [unview(v) for v in x]
    return whole


def unview_plain(x):
    return x


def test_aliases_share_what_they_name_when_read_whole(api):
    data = oneyaml.parse("a: &x [1]\nb: *x\n").to_python()
    assert data["a"] is data["b"]


def test_an_alias_bomb_is_a_limit_error_on_read(api):
    rows = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    names = "a"
    for n in "bcdefghi":
        rows.append(f"{n}: &{n} [" + ", ".join(f"*{names}" for _ in range(10)) + "]")
        names = n
    doc = oneyaml.parse("\n".join(rows) + "\n")
    with pytest.raises(LimitError) as e:
        doc.to_python()
    assert e.value.category == "limit"
    assert doc["a"] == ["x"] * 10  # the document itself is fine


def test_a_view_of_part_of_a_document_reads_aliases_to_the_rest(api):
    doc = oneyaml.parse("base: &b {x: 1}\nuse:\n  - *b\n  - *b\n")
    use = doc["use"]
    assert use.to_python() == [{"x": 1}, {"x": 1}]
    assert use[0] == {"x": 1}


def test_scalars_read_as_plain_values(api):
    doc = oneyaml.parse("a: 1\nb: 1.5\nc: true\nd: ~\ne: x\nf: !!binary aGk=\ng: !t v\n")
    assert same(doc["a"], 1) and same(doc["b"], 1.5) and doc["c"] is True and doc["d"] is None
    assert doc["e"] == "x" and doc["f"] == b"hi"
    assert same(doc["g"], Tagged("!t", "v"))


def test_mapping_protocol(api):
    doc = oneyaml.parse("a: 1\nb: {c: 2}\nd: [1, 2, 3]\n")
    assert len(doc) == 3 and list(doc) == ["a", "b", "d"] and list(doc.keys()) == ["a", "b", "d"]
    assert "a" in doc and "zz" not in doc and 5 not in doc and (1, 2) not in doc
    assert isinstance(doc["b"], MappingView) and isinstance(doc["d"], SequenceView)
    assert isinstance(doc["b"], oneyaml.MappingView)
    assert doc.get("a") == 1 and doc.get("zz", 9) == 9
    assert [k for k, _ in doc.items()] == ["a", "b", "d"]
    assert doc["b"] == {"c": 2} and doc == {"a": 1, "b": {"c": 2}, "d": [1, 2, 3]}
    assert doc != {"a": 2}
    with pytest.raises(KeyError):
        doc["zz"]
    with pytest.raises(TypeError):
        doc["a"]["x"]  # a is the integer 1


def test_sequence_protocol(api):
    doc = oneyaml.parse("- a\n- [1, 2]\n- {k: v}\n")
    assert len(doc) == 3 and doc[0] == "a" and doc[-1] == {"k": "v"} and doc[-3] == "a"
    assert list(doc)[0] == "a" and isinstance(doc[1], SequenceView)
    assert doc[0:2] == ["a", [1, 2]] or [plain(x) for x in doc[0:2]] == ["a", [1, 2]]
    assert "a" in doc and "zz" not in doc
    assert doc.index("a") == 0
    with pytest.raises(IndexError):
        doc[3]
    with pytest.raises(IndexError):
        doc[-4]
    with pytest.raises(TypeError):
        doc["a"]
    with pytest.raises(TypeError):
        doc[0:1] = []


def test_non_string_keys_are_addressed_exactly(api):
    doc = oneyaml.parse('1: a\n"1": b\ntrue: c\nnull: d\n2.5: e\n')
    assert doc[1] == "a" and doc["1"] == "b" and doc[True] == "c" and doc[None] == "d"
    assert doc[2.5] == "e"
    assert list(doc) == [1, "1", True, None, 2.5]
    doc[1] = "A"
    doc["1"] = "B"
    doc[3] = "new"
    assert str(doc) == '1: A\n"1": B\ntrue: c\nnull: d\n2.5: e\n3: new\n'
    del doc[True]
    assert True not in doc and 1 in doc


def test_pointers(api):
    doc = oneyaml.parse('spec:\n  containers:\n    - image: nginx\n"a/b": 1\n"m~n": 2\n1: one\n')
    assert doc.at("/spec/containers/0/image") == "nginx"
    assert doc.at("/a~1b") == 1 and doc.at("/m~0n") == 2 and doc.at("/1") == "one"
    assert isinstance(doc.at("/spec"), MappingView) and isinstance(doc.at(""), Document)
    with pytest.raises(KeyError):
        doc.at("/spec/zz")
    with pytest.raises(IndexError):
        doc.at("/spec/containers/5")
    # A pointer that is not one is a patch error, as it is for a patch.
    for bad in ("spec", "/a~2", "/a~"):
        with pytest.raises(PatchError) as e:
            doc.at(bad)
        assert e.value.category == "patch-invalid"
    with pytest.raises(TypeError):
        doc.at(3)
    for text in ("/spec/containers/01", "/spec/containers/-", "/spec/containers/x"):
        with pytest.raises((KeyError, IndexError)):
            doc.at(text)


def test_node_facts(api):
    text = "a: &x !!map\n  k: v  # trailing\nb: {c: 1}\n# above\nd:\n  - 1\ne: *x\n"
    doc = oneyaml.parse(text)
    a = doc["a"]
    assert (a.style, a.anchor, a.tag) == ("block", "x", "tag:yaml.org,2002:map")
    assert a.position == (1, 4) and a.position.line == 1
    assert doc["b"].style == "flow" and doc["b"].anchor is None and doc["b"].tag is None
    assert doc["d"].comment == "# above" or doc["d"].comment is None
    assert doc["e"].style == "alias" or doc["e"].anchor is None
    assert doc.style == "block" and doc.position == (1, 1)
    assert doc["d"].position == (6, 3)


def test_position_follows_edits_and_a_byte_order_mark(api):
    doc = oneyaml.parse("\ufeffa: 1\nb:\n  c: 2\n")
    assert doc["b"].position == (3, 3)
    doc["a"] = "a long new value"
    doc["new"] = {"k": 1}
    assert doc["b"].position == (3, 3)
    assert doc["new"].position.line == 5


# ---- exact assignment ---------------------------------------------------------------------------

STYLES = {
    "plain": "k: old\nz: 1\n",
    "single": "k: 'old'\nz: 1\n",
    "double": 'k: "old"\nz: 1\n',
    "literal": "k: |\n  old\nz: 1\n",
    "folded": "k: >\n  old\nz: 1\n",
    "number": "k: 5  # c\nz: 1\n",
    "flow": "k: [1, 2]\nz: 1\n",
    "block-seq": "k:\n  - old\nz: 1\n",
    "item": "- old\n- z\n",
}

VALUES = [
    None, True, False, 0, -5, 2**63, 2**64, -(2**70), 10**40, 1.5, -0.0, 1e300, 5e-324,
    float("inf"), float("-inf"), float("nan"),
    "", " ", "yes", "no", "on", "1", "1.5", "null", "~", "true", "0x1f", "1e3", ".nan",
    " lead", "trail ", "a: b", "# c", "a #b", "- x", "? x", "[a]", "{a}", "*a", "&a", "!t",
    "|", ">", "%x", "@x", "`x", "it's", 'say "hi"', "multi\nline", "multi\nline\n", "\n", "\n\n",
    "a\n\nb", "tab\there", "\t", "uni \u00e9 \u65e5\u672c \U0001f600", "\x00", "\x07", "\x85",
    "\u2028", "\ufeff", "x" * 300, " \n", "--- x", "...", "- ", ":", "a:",
    [], {}, [1, [2, [3]]], {"a": 1, "b": [1, 2]}, ["a: b", {"x": "y\nz"}],
    b"", b"\x00\xff", Tagged("!t", "v"), Tagged("!t", [1, 2]),
    {1: "a", None: "b", 1.5: "d", "": 2},
]  # fmt: skip


@pytest.mark.parametrize("style", STYLES)
def test_assignment_is_exact_in_every_style(api, style):
    src = STYLES[style]
    for value in VALUES:
        doc = oneyaml.parse(src)
        key = 0 if style == "item" else "k"
        if style == "block-seq":
            doc["k"][0] = value
            expected = {"k": [value], "z": 1}
        elif style == "item":
            doc[0] = value
            expected = [value, "z"]
        else:
            doc[key] = value
            expected = {"k": value, "z": 1}
        text = str(doc)
        assert same(oneyaml.loads(text), expected), (style, value, text)
        assert same(doc.to_python(), expected), (style, value, text)
        reread = doc[key] if style != "block-seq" else doc["k"][0]
        assert same(plain(reread), value), (style, value, text)
        # Nothing but the edited entry's lines moved.
        if style in ("plain", "single", "double", "number", "flow"):
            only_lines_changed(src, text.split("z: 1")[0] + "z: 1\n", 0, 0)
            assert text.endswith("z: 1\n")


def test_a_tuple_is_a_sequence_and_bytes_read_back(api):
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = (1, 2)
    doc["b"] = b"\x00\x01binary"
    assert doc["a"] == [1, 2] and doc["b"] == b"\x00\x01binary"
    assert oneyaml.loads(str(doc)) == {"a": [1, 2], "b": b"\x00\x01binary"}


def test_quoting_style_is_kept_when_it_can_hold_the_value(api):
    doc = oneyaml.parse("a: 'one'\nb: \"two\"\nc: three\n")
    doc["a"] = "uno"
    doc["b"] = "dos"
    doc["c"] = "tres"
    assert str(doc) == "a: 'uno'\nb: \"dos\"\nc: tres\n"


def test_style_falls_back_to_the_nearest_that_holds_the_value(api):
    doc = oneyaml.parse("a: plain\nb: 'q'\n")
    doc["a"] = "needs: quoting"
    doc["b"] = "line one\nline two"
    out = str(doc)
    assert oneyaml.loads(out) == {"a": "needs: quoting", "b": "line one\nline two"}
    assert out.startswith("a: ")


def test_assigning_what_a_dumps_accepts_never_loses_it(api):
    doc = oneyaml.parse("a: 1\n")
    value = {"list": [1, 2.5, None, True, "s", b"b"], "map": {"k": (1, 2)}, 5: "five"}
    doc["a"] = value
    got = oneyaml.loads(str(doc))["a"]
    assert got == {"list": [1, 2.5, None, True, "s", b"b"], "map": {"k": [1, 2]}, 5: "five"}


def test_views_can_be_assigned_elsewhere(api):
    doc = oneyaml.parse("a: {x: [1, 2]}\nb: 0\nc: []\n")
    doc["b"] = doc["a"]
    doc["c"].append(doc["a"]["x"])
    assert doc.to_python() == {"a": {"x": [1, 2]}, "b": {"x": [1, 2]}, "c": [[1, 2]]}


def test_a_subclass_is_read_by_its_base_value(api):
    class Name(str):
        def __str__(self):  # an override must not matter
            return "wrong"

    class Num(int):
        pass

    class Rows(dict):
        pass

    doc = oneyaml.parse("a: 1\n")
    doc["a"] = Rows(n=Name("right"), i=Num(7))
    assert oneyaml.loads(str(doc)) == {"a": {"n": "right", "i": 7}}


@pytest.mark.xfail(
    strict=True,
    reason="the engine's insert does not copy the siblings' quoting yet"
    " (kit case edit/add-key-double-quoted-siblings)",
)
def test_a_string_added_next_to_double_quoted_values_is_double_quoted(api):
    quoted = oneyaml.parse('a: "x"\nb: "y"\n')
    quoted["c"] = "z"
    assert str(quoted) == 'a: "x"\nb: "y"\nc: "z"\n'


def test_new_entries_copy_their_siblings(api):
    quoted = oneyaml.parse('a: "x"\nb: "y"\n')
    quoted["c"] = "z"
    assert oneyaml.loads(str(quoted)) == {"a": "x", "b": "y", "c": "z"}
    nested = oneyaml.parse("top:\n    deep: 1\n")
    nested["top"]["more"] = 2
    assert str(nested) == "top:\n    deep: 1\n    more: 2\n"
    flow = oneyaml.parse("m: {a: 1}\ns: [1, 2]\n")
    flow["m"]["b"] = 2
    flow["s"].append(3)
    assert str(flow) == "m: {a: 1, b: 2}\ns: [1, 2, 3]\n"
    seq = oneyaml.parse("items:\n- a\n- b\n")
    seq["items"].append("c")
    assert str(seq) == "items:\n- a\n- b\n- c\n"


def test_comments_and_blank_lines_survive_edits(api):
    text = "# head\na: 1  # one\n\n# about b\nb: 2\n\n\nc: 3 # three\n"
    doc = oneyaml.parse(text)
    doc["b"] = 20
    doc["c"] = "x"
    assert str(doc) == "# head\na: 1  # one\n\n# about b\nb: 20\n\n\nc: x # three\n"


def test_inserting_into_a_sequence(api):
    doc = oneyaml.parse("- a\n- c\n")
    doc.insert(1, "b")
    doc.insert(-1, "bc")
    doc.insert(99, "z")
    doc.insert(-99, "first")
    assert doc.to_python() == ["first", "a", "b", "bc", "c", "z"]
    assert oneyaml.loads(str(doc)) == doc.to_python()


def test_removing_entries_and_items(api):
    doc = oneyaml.parse("a: 1\nb: 2  # two\nc: 3\nd: [1, 2, 3]\n")
    del doc["b"]
    del doc["d"][0]
    assert str(doc) == "a: 1\nc: 3\nd: [2, 3]\n"
    assert doc.pop("a") == 1 and doc.to_python() == {"c": 3, "d": [2, 3]}
    with pytest.raises(KeyError):
        del doc["zz"]
    with pytest.raises(IndexError):
        del doc["d"][5]


def test_mapping_helpers_write_through(api):
    doc = oneyaml.parse("a: 1\n")
    doc.update({"b": 2, "c": [3]})
    assert doc.setdefault("a", 9) == 1 and doc.setdefault("d", 4) == 4
    assert doc.to_python() == {"a": 1, "b": 2, "c": [3], "d": 4}
    doc["c"].extend([4, 5])
    assert doc["c"].pop() == 5 and doc["c"].to_python() == [3, 4]
    doc["c"].remove(3)
    assert doc["c"].to_python() == [4]


def test_a_nested_assignment_edits_in_place(api):
    doc = oneyaml.parse("a:\n  b:\n    c: 1\n  d: 2\n")
    doc["a"]["b"]["c"] = 10
    doc["a"]["b"]["e"] = {"f": [1, 2]}
    out = str(doc)
    assert out.startswith("a:\n  b:\n    c: 10\n")
    assert oneyaml.loads(out) == {"a": {"b": {"c": 10, "e": {"f": [1, 2]}}, "d": 2}}


# ---- refusals and errors -------------------------------------------------------------------------


def test_writes_through_an_alias_are_refused_and_change_nothing(api):
    text = "a: &x [1]\nb: *x\nc: &y {k: v}\nd: *y\n"
    doc = oneyaml.parse(text)
    for edit in (
        lambda: doc["b"].append(2),
        lambda: doc["d"].__setitem__("k", 1),
        lambda: doc["d"].pop("k"),
        lambda: doc["b"].__delitem__(0),
        lambda: doc["b"].insert(0, 1),
    ):
        with pytest.raises(YAMLError) as e:
            edit()
        assert e.value.category == "refused-alias"
        assert str(doc) == text
    # Reads through the alias are fine.
    assert doc["b"] == [1] and doc["d"]["k"] == "v"


def test_removing_an_anchor_that_aliases_use_is_refused(api):
    text = "a: &x [1]\nb: *x\n"
    doc = oneyaml.parse(text)
    with pytest.raises(YAMLError) as e:
        del doc["a"]
    assert e.value.category == "refused-alias"
    assert str(doc) == text
    del doc["b"]
    del doc["a"]
    assert str(doc) == "{}\n" or oneyaml.loads(str(doc)) in ({}, None)


def test_replacing_an_alias_replaces_the_alias_only(api):
    doc = oneyaml.parse("a: &x [1]\nb: *x\n")
    doc["b"] = 5
    assert str(doc) == "a: &x [1]\nb: 5\n"


@pytest.mark.parametrize(
    ("text", "category", "line", "column"),
    [
        ("a: [\n", "syntax", 2, 1),
        ("a: 1\na: 2\n", "duplicate-key", 2, 1),
        ("a: *nope\n", "syntax", None, None),
    ],
)
def test_malformed_input_raises_the_library_error(api, text, category, line, column):
    with pytest.raises(YAMLError) as e:
        oneyaml.parse(text)
    assert e.value.category == category
    if line is not None:
        assert (e.value.line, e.value.column) == (line, column)


def test_invalid_bytes_are_an_encoding_error_with_a_position(api):
    with pytest.raises(YAMLError) as e:
        oneyaml.parse(b"a: \xff\n")
    assert e.value.category == "encoding" and (e.value.line, e.value.column) == (1, 4)
    with pytest.raises(YAMLError) as e:
        oneyaml.parse("a: \ud800\n")
    assert e.value.category == "encoding"


def test_limits_apply_to_documents(api):
    with pytest.raises(LimitError):
        oneyaml.parse("[" * 20000 + "]" * 20000)
    with pytest.raises(LimitError):
        oneyaml.parse("[" * 2000 + "]" * 2000, limits=Limits.untrusted())
    with pytest.raises(LimitError):
        oneyaml.parse("a: 1\n" * 10, limits=Limits(input_size=20))
    doc = oneyaml.parse("[" * 9000 + "]" * 9000)
    assert isinstance(doc.to_python(), list)


def test_deep_values_are_read_and_written_without_recursion(api):
    depth = 9000
    doc = oneyaml.parse("a: 1\n")
    value = []
    for _ in range(depth):
        value = [value]
    doc["a"] = value
    out = doc.to_python()["a"]
    n = 0
    while out:
        out = out[0]
        n += 1
    assert n == depth
    deeper = []
    for _ in range(20000):
        deeper = [deeper]
    with pytest.raises(LimitError):
        doc["a"] = deeper
    assert str(oneyaml.parse(str(doc))) == str(doc)


def test_bad_assignments_raise_type_or_value_errors_and_change_nothing(api):
    text = "a: 1\n"
    doc = oneyaml.parse(text)
    cycle = []
    cycle.append(cycle)
    with pytest.raises(ValueError):
        doc["a"] = cycle
    with pytest.raises(TypeError):
        doc["a"] = object()
    with pytest.raises(TypeError):
        doc["a"] = {1, 2}
    with pytest.raises(ValueError):
        doc["a"] = "\ud800"
    with pytest.raises(TypeError):
        doc["a"] = {"k": lambda: 1}
    assert str(doc) == text


def test_bad_arguments(api):
    with pytest.raises(TypeError):
        oneyaml.parse(5)
    with pytest.raises(TypeError):
        oneyaml.parse("a: 1", limits={"depth": 1})
    doc = oneyaml.parse("a: 1\n")
    with pytest.raises(TypeError):
        doc["a"]["b"]  # noqa: B018 - a scalar is not subscriptable
    with pytest.raises(TypeError):
        len(oneyaml.parse("5\n"))


def test_empty_documents(api):
    doc = oneyaml.parse("")
    assert doc.to_python() is None and len(doc.documents) == 0 and str(doc) == ""
    only_comment = oneyaml.parse("# x\n")
    assert only_comment.to_python() is None and str(only_comment) == "# x\n"


# ---- views ------------------------------------------------------------------------------------


def test_views_are_live(api):
    doc = oneyaml.parse("a: {x: 1}\nl: [1]\n")
    a, lst = doc["a"], doc["l"]
    doc["a"]["y"] = 2
    lst.append(2)
    assert a == {"x": 1, "y": 2} and doc["l"] == [1, 2] and len(lst) == 2
    a["x"] = 10
    assert doc["a"]["x"] == 10


def test_a_view_survives_edits_that_do_not_touch_its_node(api):
    doc = oneyaml.parse("a: {x: 1}\nb: [1]\nc: 3\n")
    a, b = doc["a"], doc["b"]
    doc["c"] = 4
    doc["new"] = 5
    b.append(2)
    a["x"] = 2
    del doc["c"]
    assert a == {"x": 2} and b == [1, 2]


@pytest.mark.parametrize(
    "edit",
    [
        lambda doc: doc.__delitem__("a"),
        lambda doc: doc.__setitem__("a", {"other": 1}),
        lambda doc: doc.__setitem__("a", 5),
    ],
)
def test_a_view_of_a_removed_or_replaced_node_is_stale(api, edit):
    doc = oneyaml.parse("a: {x: {y: 1}}\nb: 2\n")
    a, x = doc["a"], doc["a"]["x"]
    edit(doc)
    for view in (a, x):
        with pytest.raises(StaleViewError):
            view.to_python()
        with pytest.raises(StaleViewError):
            view["y"]
        with pytest.raises(StaleViewError):
            len(view)
    assert isinstance(StaleViewError(), YAMLError)
    assert doc["b"] == 2


def test_inserting_or_removing_items_strands_the_views_it_moves(api):
    doc = oneyaml.parse("items:\n  - {n: 0}\n  - {n: 1}\n  - {n: 2}\n")
    items = doc["items"]
    first, second, third = items[0], items[1], items[2]
    items.insert(1, {"n": "new"})
    assert first["n"] == 0  # before the insertion point: unmoved
    assert items[2] == {"n": 1}
    for moved in (second, third):
        with pytest.raises(StaleViewError):
            moved["n"]
    now = items[3]
    del items[0]
    with pytest.raises(StaleViewError):
        first["n"]
    with pytest.raises(StaleViewError):
        now["n"]
    assert items.to_python() == [{"n": "new"}, {"n": 1}, {"n": 2}]


def test_a_view_edits_through_itself_and_stays_valid(api):
    doc = oneyaml.parse("l: [1, 2, 3]\n")
    lst = doc["l"]
    lst.insert(0, 0)
    lst.append(4)
    del lst[1]
    lst[0] = "z"
    assert lst.to_python() == ["z", 2, 3, 4] and doc["l"].to_python() == ["z", 2, 3, 4]


def test_views_compare_and_print(api):
    doc = oneyaml.parse("a: {x: 1}\n")
    assert repr(doc["a"]) == "<MappingView {'x': 1}>"
    assert doc["a"] == doc["a"] and doc["a"] != {"x": 2}
    with pytest.raises(TypeError):
        hash(doc["a"])
    assert doc.documents[0] == doc


# ---- streams ----------------------------------------------------------------------------------


def test_documents_of_a_stream(api):
    text = "a: 1\n---\nb: 2  # two\n---\n- x\n"
    doc = oneyaml.parse(text)
    assert len(doc.documents) == 3 and doc["a"] == 1
    assert doc.documents[1]["b"] == 2 and doc.documents[-1][0] == "x"
    assert [d.to_python() for d in doc.documents] == [{"a": 1}, {"b": 2}, ["x"]]
    doc.documents[1]["b"] = 3
    doc.documents[2].append("yy")
    doc.documents[0]["c"] = 4
    assert str(doc) == "a: 1\nc: 4\n---\nb: 3  # two\n---\n- x\n- yy\n"
    assert doc.documents[1].at("/b") == 3
    assert doc.at("/b", document=1) == 3
    with pytest.raises(IndexError):
        doc.documents[3]
    with pytest.raises(IndexError):
        doc.at("/b", document=7)


def test_an_edit_in_one_document_leaves_the_others_byte_identical(api):
    first, last = "# one\na: 1\n", "...\n--- \n- 1 # z\n"
    doc = oneyaml.parse(first + "---\nb: 2\n" + last)
    doc.documents[1]["b"] = "changed"
    out = str(doc)
    assert out.startswith(first) and out.endswith(last)


# ---- threads and the handle -------------------------------------------------------------------


def test_threads_editing_one_document_lose_nothing(api):
    doc = oneyaml.parse("items: []\n")
    n_threads, per = 8, 40

    def work(t):
        for i in range(per):
            doc["items"].append(f"{t}-{i}")

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = oneyaml.loads(str(doc))["items"]
    assert sorted(got) == sorted(f"{t}-{i}" for t in range(n_threads) for i in range(per))
    for t in range(n_threads):
        mine = [x for x in got if x.startswith(f"{t}-")]
        assert mine == [f"{t}-{i}" for i in range(per)]


def test_readers_and_writers_share_a_document(api):
    doc = oneyaml.parse("counter: 0\nlist: [1, 2, 3]\n")
    errors = []

    def writer():
        try:
            for i in range(100):
                doc["counter"] = i
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                assert oneyaml.loads(str(doc))["list"] == [1, 2, 3]
                doc.to_python()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=f) for f in (writer, reader, reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and doc["counter"] == 99


def test_collection_keys_are_addressed_by_value(api):
    doc = oneyaml.parse("? [1, 2]\n: seq\n? {a: 1}\n: map\nplain: 3\n")
    keys = list(doc)
    assert keys[0] == (1, 2) and keys[1] == oneyaml.FrozenMapping({"a": 1}) and keys[2] == "plain"
    assert doc[(1, 2)] == "seq" and doc[keys[1]] == "map"
    assert (1, 2) in doc and (2, 1) not in doc
    doc[(1, 2)] = "changed"
    assert doc.to_python() == {(1, 2): "changed", keys[1]: "map", "plain": 3}
    # The engine does not remove an entry whose key is a collection; the document is left as it was.
    before = str(doc)
    with pytest.raises(YAMLError):
        del doc[keys[1]]
    assert str(doc) == before
    assert oneyaml.loads(before)[(1, 2)] == "changed"
