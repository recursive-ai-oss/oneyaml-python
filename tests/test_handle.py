"""The lossless document behind the backend interface: parse, read, edit, patch and emit.

Driven through ``parse(data, limits)`` and the ``Handle`` it returns, so the same tests run on
every backend that provides one.
"""

import importlib
import os

import pytest

from oneyaml import Limits, YAMLError
from oneyaml._errors import PatchError

BACKENDS = [
    b for b in os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(",") if b != "native"
]
# The native handle is checked by the tests of the public document once it exists there.


@pytest.fixture(params=BACKENDS)
def parse(request):
    mod = importlib.import_module(f"oneyaml._{request.param}")
    return lambda data, limits=None: mod.parse(data, limits or Limits())


def emit_of(parse, text, edit):
    h = parse(text)
    edit(h)
    return h.emit().decode()


# ---- round trip -------------------------------------------------------------------------------

HARD = [
    "",
    "a: 1\n",
    "a: 1",
    "# only a comment\n",
    "---\n",
    "--- # c\na: 1\n...\n",
    "a: 1\r\nb:\r\n  - x\r\n  - y # t\r\n",
    "a: 1\rb: 2\r",
    "\ufeffa: 1\n",
    "%YAML 1.2\n---\n- a\n- b\n",
    "a:   1   # spaced\n\n\nb:\t9\n",
    "k: |\n  one\n  two\n\nm: >-\n  folded\n  text\n",
    "k: |+\n  keep\n\n\nz: 1\n",
    "- &a x\n- *a\n- !!str 5\n- !t {a: 1}\n",
    "{a: 1, b: [x, y], c: {d: e}}\n",
    "? complex key\n: value\n? [a, b]\n: other\n",
    "a: 'it''s'\nb: \"tab\\there\"\n",
    "a: 1\n---\nb: 2\n...\n---\n- c\n",
    "x:\n  - a: 1\n    b: 2\n  - c\n\n# trailing\n",
    "a: &x\n  b: 1\nc: *x\n",
    "plain\n  multi line\n  scalar\n",
    "a: 1\n\n\n\n",
    "  - a\n  - b\n",
    "key:\n- a\n- b\n",
]


@pytest.mark.parametrize("text", HARD)
def test_round_trip_is_byte_exact(parse, text):
    assert parse(text).emit() == text.encode()
    assert parse(text.encode()).emit() == text.encode()


def test_round_trip_keeps_the_encoding(parse):
    h = parse(b"\xff\xfe" + "a: é\n".encode("utf-16-le"))
    assert h.encoding == "utf-16-le"
    assert h.emit() == b"\xff\xfe" + "a: é\n".encode("utf-16-le")
    assert parse(b"\xef\xbb\xbfa: 1\n").emit() == b"\xef\xbb\xbfa: 1\n"


@pytest.mark.parametrize(
    "bad", ["a: [1\n", "a: b: c\n", "- a\nb: 1\n", "*nope\n", "a: !!int abc\n"]
)
def test_parse_errors_are_library_errors(parse, bad):
    with pytest.raises(YAMLError) as info:
        parse(bad)
    assert info.value.category in ("syntax", "other")
    assert info.value.line >= 1


def test_deep_nesting_has_no_recursion_error(parse):
    n = 5000
    h = parse("[" * n + "]" * n, Limits.none())
    assert h.emit() == ("[" * n + "]" * n).encode()
    h.set(0, ["0"] * (n - 1), 1)
    assert h.emit().decode().count("[") == n - 1
    block = "".join(" " * i + "a:\n" for i in range(300)) + " " * 300 + "a: 1\n"
    h = parse(block, Limits.none())
    assert h.emit() == block.encode()
    assert h.value(0, ["a"] * 301) == 1


# ---- reads ------------------------------------------------------------------------------------

DOC = "a: 1\nb:\n  - x\n  - 2.5\n  - {k: v}\nc: &anc [1, 2]\nd: *anc\ne: 'q' # note\n"


def test_reads(parse):
    h = parse(DOC)
    assert h.count() == 1
    assert h.kind(0, []) == "mapping"
    assert h.kind(0, ["b"]) == "sequence"
    assert h.kind(0, ["a"]) == "scalar"
    assert h.value(0, ["b", 1]) == 2.5
    assert h.value(0, ["b", "2", "k"]) == "v"
    assert h.length(0, ["b"]) == 3
    assert h.length(0, []) == 5
    assert h.keys(0, []) == ["a", "b", "c", "d", "e"]
    assert h.keys(0, ["b"]) == ["0", "1", "2"]
    assert h.value(0, ["d"]) == [1, 2]  # an alias reads as what it names
    assert h.value(0, []) == {
        "a": 1,
        "b": ["x", 2.5, {"k": "v"}],
        "c": [1, 2],
        "d": [1, 2],
        "e": "q",
    }


def test_info(parse):
    h = parse(DOC)
    assert h.info(0, ["a"]) == ("plain", None, None, None, 1, 4)
    style, _tag, _anchor, comment, line, col = h.info(0, ["e"])
    assert (style, comment, line, col) == ("single", "# note", 8, 4)
    assert h.info(0, ["c"])[:3] == ("flow", None, "anc")
    assert h.info(0, ["d"])[0] == "alias"
    assert h.info(0, ["b"])[0] == "block"


def test_non_string_tokens(parse):
    h = parse("1: a\ntrue: b\n2.5: c\nl: [x, y]\n")
    assert h.value(0, [1]) == "a"
    assert h.value(0, ["1"]) == "a"
    assert h.value(0, [True]) == "b"
    assert h.value(0, [2.5]) == "c"
    assert h.value(0, ["l", 1]) == "y"


def test_multi_document_reads(parse):
    h = parse("a: 1\n---\nb: 2\n---\n- c\n")
    assert h.count() == 3
    assert [h.value(i, []) for i in range(3)] == [{"a": 1}, {"b": 2}, ["c"]]
    with pytest.raises(PatchError) as info:
        h.value(3, [])
    assert info.value.category == "patch-path"


@pytest.mark.parametrize(
    ("path", "category"),
    [(["zz"], "patch-path"), (["b", 9], "patch-path"), (["a", "x"], "patch-path")],
)
def test_read_errors(parse, path, category):
    with pytest.raises(PatchError) as info:
        parse(DOC).value(0, path)
    assert info.value.category == category


def test_duplicate_keys_are_found_when_read(parse):
    h = parse("a: 1\na: 2\n")
    assert h.emit() == b"a: 1\na: 2\n"
    with pytest.raises(YAMLError) as info:
        h.value(0, [])
    assert info.value.category == "duplicate-key"


def test_ambiguous_key_text_is_refused(parse):
    h = parse('80: a\n"80": c\n')
    with pytest.raises(PatchError) as info:
        h.set(0, ["80"], "x")
    assert info.value.category == "refused-ambiguous"
    assert h.emit() == b'80: a\n"80": c\n'


# ---- set / insert / remove --------------------------------------------------------------------

REFERENCE = "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n"


def test_literal_block_keeps_its_style_when_text_ends_in_a_newline(parse):
    out = emit_of(parse, REFERENCE, lambda h: h.set(0, ["b"], "this is a test2\n"))
    assert out == "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test2\nc: 13\n"


def test_literal_block_strips_when_text_has_no_trailing_newline(parse):
    out = emit_of(parse, REFERENCE, lambda h: h.set(0, ["b"], "this is a test2"))
    assert out == "#hello\na: 10\n\n#abc 123\nb: |-\n  this is a test2\nc: 13\n"


def test_set_changes_only_the_value(parse):
    src = "a:   1   # one\nb: 'x'\n# lead\nc:\n  - 1\n  - 2\n"
    assert emit_of(parse, src, lambda h: h.set(0, ["a"], 2)) == src.replace("1 ", "2 ", 1)
    assert emit_of(parse, src, lambda h: h.set(0, ["b"], "it's")) == src.replace("'x'", "'it''s'")
    out = emit_of(parse, src, lambda h: h.set(0, ["c", 1], [3, 4]))
    assert out == "a:   1   # one\nb: 'x'\n# lead\nc:\n  - 1\n  - - 3\n    - 4\n"


def test_set_a_collection_in_block_style(parse):
    out = emit_of(parse, "a: 1\nb: 2\n", lambda h: h.set(0, ["a"], {"x": [1, 2], "y": "z"}))
    assert out == "a:\n  x:\n    - 1\n    - 2\n  y: z\nb: 2\n"


def test_set_and_insert_keep_the_line_break_style(parse):
    h = parse("a: 1\r\nb: 2\r\n")
    h.insert(0, [], "c", [1, 2])
    h.set(0, ["a"], "multi\nline")
    out = h.emit().decode()
    assert out.count("\r\n") == out.count("\n")
    assert h.value(0, []) == {"a": "multi\nline", "b": 2, "c": [1, 2]}


def test_insert_member_and_item(parse):
    h = parse("a: 1\nl:\n  - x\n")
    h.insert(0, [], "b", 2)
    h.insert(0, ["l"], 1, "y")
    h.insert(0, ["l"], 0, "w")
    h.insert(0, ["l"], 99, "end")  # past the end appends
    assert h.emit() == b"a: 1\nl:\n  - w\n  - x\n  - y\n  - end\nb: 2\n"


def test_insert_into_flow_and_empty_collections(parse):
    h = parse("a: {x: 1}\nb: [1]\nc: []\nd: {}\n")
    h.insert(0, ["a"], "y", 2)
    h.insert(0, ["b"], None, 2)
    h.insert(0, ["c"], 0, "k")
    h.insert(0, ["d"], "z", 1)
    assert h.value(0, []) == {"a": {"x": 1, "y": 2}, "b": [1, 2], "c": ["k"], "d": {"z": 1}}
    assert h.emit().startswith(b"a: {x: 1, y: 2}\nb: [1, 2]\n")


def test_remove(parse):
    src = "a: 1\n# about b\nb: 2\nc:\n  - x\n  - y\n"
    assert emit_of(parse, src, lambda h: h.remove(0, ["b"])) == "a: 1\nc:\n  - x\n  - y\n"
    assert emit_of(parse, src, lambda h: h.remove(0, ["c", 0])) == (
        "a: 1\n# about b\nb: 2\nc:\n  - y\n"
    )
    assert emit_of(parse, src, lambda h: h.remove(0, ["a"])) == (
        "# about b\nb: 2\nc:\n  - x\n  - y\n"
    )


def test_removing_the_last_member_leaves_an_empty_collection(parse):
    assert emit_of(parse, "a: {x: 1}\n", lambda h: h.remove(0, ["a", "x"])) == "a: {}\n"
    assert emit_of(parse, "a:\n  x: 1\n", lambda h: h.remove(0, ["a", "x"])) == "a: {}\n"
    assert emit_of(parse, "- 1\n", lambda h: h.remove(0, [0])) == "[]\n"


def test_removing_after_a_keep_scalar_does_not_grow_it(parse):
    h = parse("x: |+\n  two\n\ny: 1\n\n# end\n")
    h.remove(0, ["y"])
    assert h.value(0, ["x"]) == "two\n\n"
    assert h.emit() == b"x: |+\n  two\n\n# end\n"


def test_setting_over_a_keep_scalar_before_a_comment(parse):
    h = parse("- - |+\n    two\n\n# end\n")
    h.set(0, [0, 0], {"k": 1})
    assert h.emit().endswith(b"# end\n")
    assert h.value(0, [0, 0]) == {"k": 1}


def test_a_keep_scalar_ending_a_document_counts_the_blank_lines_before_the_next(parse):
    h = parse("a: |+\n  x\n\n---\nb: 1\n")
    assert h.value(0, ["a"]) == "x\n\n"
    h.set(0, [], {"k": "two\n\n"})
    assert h.value(0, []) == {"k": "two\n\n"}
    assert h.value(1, []) == {"b": 1}


def test_a_comment_after_the_last_entry_stays_after_a_new_block_scalar(parse):
    h = parse("a: 1\nb:\n  - x\n  # note\n")
    h.insert(0, [], "c", "multi\nline")
    assert h.value(0, ["c"]) == "multi\nline"
    assert b"# note" in h.emit()


def test_edits_in_one_document_leave_the_others_alone(parse):
    h = parse("a: 1\n---\nb: 2\n# keep\n---\nc: 3\n")
    h.set(1, ["b"], 20)
    assert h.emit() == b"a: 1\n---\nb: 20\n# keep\n---\nc: 3\n"


def test_tags_and_binary_values(parse):
    h = parse("a: !!str 5\nb: !t x\nc: 1\n")
    h.set(0, ["a"], "six")
    assert h.emit().startswith(b"a: !!str six\n")  # the tag still describes it
    h.set(0, ["b"], 7)
    h.set(0, ["c"], b"\x00\x01")
    assert h.value(0, ["c"]) == b"\x00\x01"
    assert b"b: !t 7\n" in h.emit()  # a custom tag stays on a value it can describe


def test_edit_refusals(parse):
    src = "a: &x [1, 2]\nb: *x\nc: &y 1\nd:\n  - *y\ne: 3\n"
    h = parse(src)
    cases = [
        (lambda: h.set(0, ["b", 0], 9), "refused-alias"),  # a write through an alias
        (lambda: h.insert(0, ["b"], None, 9), "refused-alias"),
        (lambda: h.remove(0, ["a"]), "refused-alias"),  # the anchor is used elsewhere
        (lambda: h.remove(0, ["c"]), "refused-alias"),
        (lambda: h.set(0, ["zz"], 1), "patch-path"),
        (lambda: h.remove(0, []), "patch-invalid"),
        (lambda: h.insert(0, ["e"], "k", 1), "patch-path"),
    ]
    for call, category in cases:
        with pytest.raises(PatchError) as info:
            call()
        assert info.value.category == category
        assert h.emit() == src.encode()  # a refused edit changes nothing
    h.remove(0, ["b"])  # the alias itself can go
    assert h.value(0, ["a"]) == [1, 2]


def test_version_changes_with_every_edit(parse):
    h = parse("a: [1, 2]\nb: 1\n")
    seen = [h.version()]
    for edit in (
        lambda: h.set(0, ["b"], 2),
        lambda: h.insert(0, ["a"], 0, 0),
        lambda: h.remove(0, ["a", 0]),
        lambda: h.patch(0, [{"op": "add", "path": "/c", "value": 1}]),
        lambda: h.merge_patch(0, {"c": None}),
    ):
        edit()
        seen.append(h.version())
    assert len(set(seen)) == len(seen)


def test_str_of_an_edited_stream_is_its_text(parse):
    h = parse("a: 1\n")
    h.set(0, ["a"], 2)
    assert h.emit().decode(h.encoding) == "a: 2\n"


# ---- patches ----------------------------------------------------------------------------------


def test_patch_operations(parse):
    h = parse("a: 1\nb: [1, 2, 3]\nc: {x: 1}\n")
    h.patch(
        0,
        [
            {"op": "add", "path": "/b/-", "value": 4},
            {"op": "replace", "path": "/a", "value": "one"},
            {"op": "remove", "path": "/b/0"},
            {"op": "copy", "from": "/c", "path": "/d"},
            {"op": "move", "from": "/b/0", "path": "/e"},
            {"op": "test", "path": "/e", "value": 2},
        ],
    )
    assert h.value(0, []) == {"a": "one", "b": [3, 4], "c": {"x": 1}, "d": {"x": 1}, "e": 2}


def test_pointer_escapes(parse):
    h = parse("'a/b': 1\n'c~d': 2\n")
    h.patch(0, [{"op": "replace", "path": "/a~1b", "value": 10}, {"op": "remove", "path": "/c~0d"}])
    assert h.emit() == b"'a/b': 10\n"


def test_failing_patch_changes_nothing(parse):
    src = "a: 1\nb: [1, 2]\n# end\n"
    h = parse(src)
    v = h.version()
    bad = [
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "add", "path": "/b/-", "value": 3},
        {"op": "remove", "path": "/zz"},
    ]
    with pytest.raises(PatchError) as info:
        h.patch(0, bad)
    assert (info.value.category, info.value.index) == ("patch-path", 2)
    assert h.emit() == src.encode()
    assert h.value(0, []) == {"a": 1, "b": [1, 2]}
    assert h.version() == v


@pytest.mark.parametrize(
    ("ops", "category", "index"),
    [
        ([{"op": "test", "path": "/a", "value": 2}], "patch-test", 0),
        ([{"op": "bogus", "path": "/a"}], "patch-invalid", 0),
        ([{"op": "add", "path": "/a"}], "patch-invalid", 0),
        ([{"op": "add", "path": "a", "value": 1}], "patch-invalid", 0),
        ([{"op": "move", "from": "/b", "path": "/b/0"}], "patch-invalid", 0),
        ([{"op": "add", "path": "/b/5", "value": 1}], "patch-path", 0),
        (
            [{"op": "replace", "path": "/a", "value": 1}, {"op": "remove", "path": ""}],
            "patch-invalid",
            1,
        ),
    ],
)
def test_patch_refusals(parse, ops, category, index):
    src = "a: 1\nb: [1]\n"
    h = parse(src)
    with pytest.raises(PatchError) as info:
        h.patch(0, ops)
    assert (info.value.category, info.value.index) == (category, index)
    assert h.emit() == src.encode()


def test_patch_through_an_alias_is_refused_and_atomic(parse):
    src = "a: &x {k: 1}\nb: *x\n"
    h = parse(src)
    with pytest.raises(PatchError) as info:
        h.patch(
            0,
            [
                {"op": "add", "path": "/c", "value": 1},
                {"op": "replace", "path": "/b/k", "value": 2},
            ],
        )
    assert (info.value.category, info.value.index) == ("refused-alias", 1)
    assert h.emit() == src.encode()
    h.patch(0, [{"op": "copy", "from": "/b/k", "path": "/c"}])  # reading through one is fine
    assert h.value(0, ["c"]) == 1


def test_patch_copy_is_limited(parse):
    src = (
        "a: &a [x, x, x, x]\nb: &b [*a, *a, *a, *a]\n"
        "c: &c [*b, *b, *b, *b]\nd: &d [*c, *c, *c, *c]\n"
    )
    limits = Limits(alias_nodes=50, alias_bytes=10_000)
    h = parse(src, limits)
    with pytest.raises(PatchError) as info:
        h.patch(0, [{"op": "copy", "from": "/d", "path": "/e"}])
    assert info.value.category == "limit"
    assert h.emit() == src.encode()


def test_patch_on_the_second_document(parse):
    h = parse("a: 1\n---\nb: 2\n")
    h.patch(1, [{"op": "replace", "path": "/b", "value": 3}])
    assert h.emit() == b"a: 1\n---\nb: 3\n"


def test_merge_patch(parse):
    h = parse("a: 1\nb:\n  c: 2\n  d: 3\ne: [1]\n")
    h.merge_patch(0, {"a": None, "b": {"c": 20, "f": {"g": 1, "h": None}}, "e": [2], "n": 1})
    assert h.value(0, []) == {"b": {"c": 20, "d": 3, "f": {"g": 1}}, "e": [2], "n": 1}


def test_merge_patch_onto_a_scalar_and_with_a_scalar(parse):
    h = parse("a: 1\nb: x\n")
    h.merge_patch(0, {"b": {"k": 1}, "a": {}})
    assert h.value(0, []) == {"a": {}, "b": {"k": 1}}
    h.merge_patch(0, [1, 2])
    assert h.value(0, []) == [1, 2]
    h.merge_patch(0, {})
    assert h.value(0, []) == {}


def test_merge_patch_is_atomic(parse):
    src = "a: &x {k: 1}\nb: *x\nc: 1\n"
    h = parse(src)
    with pytest.raises(PatchError):
        h.merge_patch(0, {"c": 2, "b": {"k": 5}})
    assert h.emit() == src.encode()


# ---- the row table and a random-edit oracle ---------------------------------------------------


def test_random_edits_agree_with_a_model():
    pytest.importorskip("oneyaml._pure")
    from handle_fuzz import run

    for seed in range(120):
        assert run(seed) is None, run(seed)


@pytest.mark.slow
def test_random_edits_agree_with_a_model_long():
    from handle_fuzz import run

    for seed in range(120, 3000):
        assert run(seed) is None, run(seed)
