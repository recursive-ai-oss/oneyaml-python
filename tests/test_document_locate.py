"""``Document.locate``, ``.comment`` on mapping values and the root, and the small surface fixes
(error classes, pointer errors, names kept private)."""

import importlib

import pytest

import oneyaml
from oneyaml import Document, Location, PatchError, YAMLError, _api


def test_locate_a_scalar_by_pointer_and_by_path(api):
    doc = oneyaml.parse("name: billing\nport: eighty\nitems:\n  - a: 1\n  - b: two\n")
    assert doc.locate("/port") == Location(2, 7, 2, 13)
    assert doc.locate(("port",)) == doc.locate("/port")
    assert doc.locate(["items", 1, "b"]) == Location(5, 8, 5, 11)
    assert doc.locate("/items/1/b") == doc.locate(("items", 1, "b"))
    assert doc.locate(("items", -1, "b")) == doc.locate("/items/1/b")
    assert doc.locate(()) == doc.locate("")
    assert doc.locate("").line == 1


def test_locate_gives_the_start_and_the_end(api):
    doc = oneyaml.parse("a:\n  b: [1, 2]\n  c: |\n    text\n    more\nd: 'q'\n")
    assert tuple(doc.locate("/a"))[:4] == (2, 3, 5, 9)
    assert tuple(doc.locate("/a/b"))[:4] == (2, 6, 2, 12)
    assert tuple(doc.locate("/a/c"))[:4] == (3, 6, 5, 9)
    assert tuple(doc.locate("/d"))[:4] == (6, 4, 6, 7)


def test_columns_count_characters_and_skip_the_byte_order_mark(api):
    assert oneyaml.parse("a: ünï\nb: x\n").locate("/b") == Location(2, 4, 2, 5)
    assert oneyaml.parse("\ufeffa: 1\nb: 2\n").locate("/a") == Location(1, 4, 1, 5)
    assert oneyaml.parse("a: 1\r\nb: 22\r\n").locate("/b") == Location(2, 4, 2, 6)


def test_locate_in_the_other_documents_of_a_stream(api):
    doc = oneyaml.parse("a: 1\n---\nb: 2\n")
    assert doc.locate("/b", document=1) == Location(3, 4, 3, 5)
    assert doc.documents[1].locate("/b") == Location(3, 4, 3, 5)
    with pytest.raises(IndexError):
        doc.locate("/b", document=2)


def test_locate_matches_the_position_of_a_view_and_follows_edits(api):
    doc = oneyaml.parse("a:\n  b: 1\nc: 2\n")
    assert doc.locate("/a").line == doc["a"].position.line
    doc["a"]["b"] = 100
    assert doc.locate("/c") == Location(3, 4, 3, 5)
    assert doc.locate("/a/b") == Location(2, 6, 2, 9)


def test_locate_names_an_alias_where_it_is_written(api):
    doc = oneyaml.parse("a: &x 1\nb: *x\n")
    assert doc.locate("/b") == Location(2, 4, 2, 6)


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/nope", KeyError),
        (("nope",), KeyError),
        (("items", 5), IndexError),
        (("items", -9), IndexError),
        (("items", "x"), KeyError),
        (("items", 0, "deeper"), KeyError),
        (("name", "deeper"), KeyError),
    ],
)
def test_locate_of_a_missing_path_is_a_lookup_error(api, path, error):
    doc = oneyaml.parse("name: n\nitems: [a, b]\n")
    with pytest.raises(error):
        doc.locate(path)


def test_locate_arguments(api):
    doc = oneyaml.parse("a: 1\n")
    with pytest.raises(TypeError):
        doc.locate(5)
    with pytest.raises(TypeError):
        doc.locate(b"/a")
    with pytest.raises(PatchError, match="must be empty or start with '/'"):
        doc.locate("a")
    with pytest.raises(TypeError):
        doc.locate("/a", document="0")


@pytest.mark.native_only
def test_locate_on_the_two_backends_agree():
    native = importlib.import_module("oneyaml._native")
    pure = importlib.import_module("oneyaml._pure")
    samples = [
        (
            "a:\n  b: [1, 2, 3]\n  c: |\n    text\n    more\nd: 'q'\n",
            ["/a", "/a/b/1", "/a/c", "/d"],
        ),
        ("a: >\n  folded\n  text\n\nb: 1\n", ["/a", "/b"]),
        ("a: &x {k: v}\nb: *x\nc: !!str 5\n", ["/a", "/b", "/c", "/a/k"]),
        ('\ufeffa: ünï\r\nb: "two words"\r\n', ["/a", "/b"]),
        ("- x: 1\n- y: 2\n", ["/0", "/1/y"]),
        ("a: 1\n---\nb: 2\n", ["/b"]),
        ("a:\nb: 1\n", ["/a"]),
        ("x\n", [""]),
    ]
    for text, pointers in samples:
        for pointer in pointers:
            seen = []
            for mod in (native, pure):
                old = _api._impl
                _api._impl = mod
                try:
                    doc = oneyaml.parse(text)
                    d = len(doc.documents) - 1 if "---" in text else 0
                    seen.append(tuple(doc.locate(pointer, document=d)))
                finally:
                    _api._impl = old
            assert seen[0] == seen[1], (text, pointer)


# ---- comments -----------------------------------------------------------------------------------


def comments(doc):
    """``{path: comment}`` for every node that has one."""
    out = {}
    h = doc._core.h

    def go(d, path):
        got = h.info(d, path)[3]
        if got is not None:
            out[(d, "/".join(path))] = got
        kind = h.kind(d, path)
        if kind == "mapping":
            for k in h.keys(d, path):
                go(d, [*path, k])
        elif kind == "sequence":
            for i in range(h.length(d, path)):
                go(d, [*path, str(i)])

    for d in range(len(doc.documents)):
        go(d, [])
    return out


def test_a_mapping_value_reads_the_comment_after_it(api):
    doc = oneyaml.parse("a: [1, 2]  # nums\nname: billing   # keep in sync\nport: 80\n")
    assert doc["a"].comment == "# nums"
    assert doc.locate("/name").comment == "# keep in sync"
    assert doc.locate("/port").comment is None
    assert comments(doc) == {(0, "a"): "# nums", (0, "name"): "# keep in sync"}


def test_a_mapping_value_reads_the_comment_above_its_key(api):
    doc = oneyaml.parse("a: 1\n# about b\nb:\n  x: 1\n\n# first\n\n# about c\nc: 3\n")
    assert doc["b"].comment == "# about b"
    assert doc.locate("/c").comment == "# first\n# about c"


def test_a_block_value_reads_the_comment_after_the_colon(api):
    doc = oneyaml.parse(
        "a:  # on the key line\n  x: 1\nb: |  # header\n  text\n"
        "c: &n  # anchored\n  y: 1\nd:  # empty\n"
    )
    assert doc["a"].comment == "# on the key line"
    assert doc.locate("/b").comment == "# header"
    assert doc["c"].comment == "# anchored"
    assert doc.locate("/d").comment == "# empty"


def test_the_comment_above_the_first_entry_belongs_to_it(api):
    doc = oneyaml.parse("# header\nx: 1\n")
    assert doc.locate("/x").comment == "# header"
    assert doc.comment is None


def test_the_root_reads_the_document_header(api):
    assert oneyaml.parse("# header\n\nx: 1\n").comment == "# header"
    assert oneyaml.parse("# one\n# two\n\nx: 1\ny: 2\n").comment == "# one\n# two"
    assert oneyaml.parse("--- # on the marker\na: 1\n").comment == "# on the marker"
    assert oneyaml.parse("# before\n---\na: 1\n").comment == "# before"
    assert oneyaml.parse("# above\n\nx\n").comment == "# above"
    assert oneyaml.parse("x # after\n").comment == "# after"
    assert oneyaml.parse("a: 1\n").comment is None


def test_the_comment_of_a_deeper_block_end_is_not_the_next_entrys(api):
    doc = oneyaml.parse("a:\n  x: 1\n  # closes a\nb: 2\n")
    assert doc.locate("/b").comment is None
    assert doc.locate("/a/x").comment is None
    assert doc["a"].comment is None


def test_a_sequence_item_reads_its_own_comments(api):
    doc = oneyaml.parse("- a  # one\n- b\n  # under b\n- c\n# above d\n- d\n")
    got = comments(doc)
    assert got == {(0, "0"): "# one", (0, "2"): "# under b", (0, "3"): "# above d"}


@pytest.mark.native_only
def test_comments_agree_between_the_backends():
    native = importlib.import_module("oneyaml._native")
    pure = importlib.import_module("oneyaml._pure")
    texts = [
        "a: [1, 2]  # nums\n",
        "a:\n  # above\n  x: 1\n",
        "# header\n\nx: 1\n",
        "# first\n# second\n\nx: 1\ny: 2\n",
        "a: 1\n\n# about b\n\nb: 2\n",
        "a:\n  x: 1\n  # footer of a\nb: 2\n",
        "a: |  # block header\n  text\nb: 2\n",
        "a: &anc  # after anchor\n  x: 1\n",
        "--- # on marker\na: 1\n",
        "# before marker\n---\n# after\n\na: 1\n",
        "x # root scalar\n",
        "- a  # one\n- b\n# above c\n- c\n",
        "- a\n  # under a\n- b\n",
        "a: 1\n# between\n---\nb: 2  # t\n",
        "a:\n    # deep above\n    b:\n        # deeper\n        c: 1 # eol\n",
        "m:\n  k: v # c1\n  l: w\n# at root col\nn: 1\n",
        "? complex\n: value  # c\n",
    ]
    for text in texts:
        seen = []
        for mod in (native, pure):
            old = _api._impl
            _api._impl = mod
            try:
                seen.append(comments(oneyaml.parse(text)))
            finally:
                _api._impl = old
        assert seen[0] == seen[1], text


# ---- errors and surface ------------------------------------------------------------------------


def test_an_edit_that_is_refused_is_a_patch_error(api):
    doc = oneyaml.parse("defaults: &d {a: 1}\njob: *d\n")
    with pytest.raises(PatchError) as info:
        del doc["defaults"]
    assert info.value.category == "refused-alias"
    assert isinstance(info.value, YAMLError) and isinstance(info.value, ValueError)
    assert str(doc) == "defaults: &d {a: 1}\njob: *d\n"


def test_a_pointer_that_does_not_start_with_a_slash_says_so(api):
    doc = oneyaml.parse("spec: {x: 1}\n")
    with pytest.raises(ValueError, match="must be empty or start with '/'") as info:
        doc.at("spec/x")
    assert isinstance(info.value, PatchError) and info.value.category == "patch-invalid"
    with pytest.raises(TypeError):
        doc.at(3)
    assert doc.at("/spec/x") == 1


def test_the_documents_of_a_stream_have_a_readable_repr(api):
    doc = oneyaml.parse("a: 1\n---\nb: 2\n")
    assert repr(doc.documents) == "<documents of a stream: 2>"
    assert "0x" not in repr(doc.documents)


def test_the_package_namespace_holds_no_imported_helpers():
    public = [n for n in dir(oneyaml) if not n.startswith("_")]
    assert sorted(set(public) - set(oneyaml.__all__) - {"compat"}) == []
    assert not hasattr(oneyaml, "version")
    assert "Location" in oneyaml.__all__ and Document is oneyaml.Document


def test_limits_replace_takes_named_limits():
    base = oneyaml.Limits()
    assert base.replace(depth=5).depth == 5
    assert base.replace(depth=5).int_digits == base.int_digits
    assert base.replace(input_size=None).input_size is None
    assert base.replace() == base
    with pytest.raises(TypeError):
        base.replace(deep=5)
    with pytest.raises(TypeError):
        base.replace(depth="x")
