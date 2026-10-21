"""Edits and lookups found to differ between the backends or from the specification."""

import pytest

import oneyaml
from oneyaml import LimitError, StaleViewError, YAMLError

# ---- block scalars that end in a line of spaces ----
# The line is content when it is wider than the content indent, so replacing the scalar takes it
# along; left behind it would trail the new value.


@pytest.mark.parametrize(
    ("text", "value", "expected"),
    [
        ("foo: |\n  x\n   \n", 5, "foo: 5\n"),
        ("foo: |\n  x\n   \n", "y", "foo: |-\n  y\n"),
        ("foo: |\n  x\n   ", 5, "foo: 5"),
        ("foo: >\n  x\n   \nbar: 1\n", 5, "foo: 5\nbar: 1\n"),
        ("- |\n  x\n    \n    \n- b\n", "z", "- |-\n  z\n- b\n"),
    ],
)
def test_replacing_a_block_scalar_takes_its_last_line_of_spaces(api, text, value, expected):
    doc = oneyaml.parse(text)
    doc[0 if text.startswith("-") else "foo"] = value
    assert bytes(doc) == expected.encode()
    assert oneyaml.loads(bytes(doc))


def test_a_block_scalar_without_such_a_line_is_replaced_as_before(api):
    doc = oneyaml.parse("foo: |\n  x\n\nbar: 1\n")
    doc["foo"] = 5
    assert bytes(doc) == b"foo: 5\n\nbar: 1\n"


# ---- a duplicate key written as an alias ----


@pytest.mark.parametrize("others", [0, 1, 45])
def test_a_duplicate_key_behind_an_alias_key_is_refused_for_editing(api, others):
    rows = "".join(f"k{i}: {i}\n" for i in range(others))
    text = f"&a foo: 1\n{rows}*a : 2\n"
    with pytest.raises(YAMLError) as e:
        oneyaml.loads(text)
    assert e.value.category == "duplicate-key"
    with pytest.raises(YAMLError) as e:
        oneyaml.parse(text)
    assert e.value.category == "duplicate-key"


def test_an_alias_key_that_is_not_a_duplicate_is_still_editable(api):
    doc = oneyaml.parse("a: &k key\n*k : 1\nz: 2\n")
    doc["zz"] = 1
    assert bytes(doc) == b"a: &k key\n*k : 1\nz: 2\nzz: 1\n"


# ---- a key with a local tag is another key ----


@pytest.mark.parametrize("key", ["foo", "yes"])
def test_a_plain_key_is_added_beside_a_locally_tagged_one(api, key):
    text = f"!local {key}: 1\nbar: 2\n"
    doc = oneyaml.parse(text)
    assert key not in doc
    with pytest.raises(KeyError):
        doc[key]
    doc[key] = "new"
    assert bytes(doc) == f"{text}{key}: new\n".encode()
    assert doc[key] == "new"


def test_pointers_still_match_a_tagged_key_by_its_text(api):
    doc = oneyaml.parse("!local foo: 1\nbar: 2\n")
    doc.patch([{"op": "replace", "path": "/foo", "value": 5}])
    assert bytes(doc) == b"!local foo: 5\nbar: 2\n"


# ---- reading a view takes the stream lock and checks the view ----


class OwnedOnly:
    """Wraps the backend handle and fails any call made without the stream lock."""

    def __init__(self, h, lock):
        self._h, self._lock = h, lock

    def __getattr__(self, name):
        attr = getattr(self._h, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            if name in {"keys", "kind", "length", "read"}:
                assert self._lock._is_owned(), f"{name} was called without the stream lock"
            return attr(*a, **k)

        return call


def watched(text):
    doc = oneyaml.parse(text)
    core = doc._core
    core.h = OwnedOnly(core.h, core.lock)
    return doc


def test_iterating_and_testing_membership_hold_the_stream_lock(api):
    doc = watched("m: {a: 1, b: 2}\nl: [1, 2, 3]\n")
    assert list(doc["m"]) == ["a", "b"]
    assert "a" in doc["m"] and "c" not in doc["m"]
    assert list(doc["l"]) == [1, 2, 3]
    assert 2 in doc["l"]
    assert list(doc) == ["m", "l"]


def test_a_sequence_iterator_notices_that_its_sequence_was_replaced(api):
    doc = oneyaml.parse("l: [[1], [2], [3]]\n")
    it = iter(doc["l"])
    first = next(it)
    assert first == [1]
    doc["l"] = [[7]]
    with pytest.raises(StaleViewError):
        next(it)


def test_a_sequence_iterator_stops_when_its_sequence_shrinks(api):
    doc = oneyaml.parse("l: [1, 2, 3, 4]\n")
    seq = doc["l"]
    it = iter(seq)
    assert next(it) == 1
    del seq[3]
    del seq[2]
    assert list(it) == [2]


# ---- keys that are collections ----


def test_a_key_nested_deeper_than_the_limit_is_a_limit_error_when_read_back(api):
    doc = oneyaml.parse("? " * 2000 + "a\n")
    with pytest.raises(LimitError) as e:
        list(doc)
    assert e.value.category == "limit"


def test_a_patch_copies_a_node_that_has_collection_keys(api):
    doc = oneyaml.parse("? [a, b]\n: x\nq: 1\n")
    doc.patch([{"op": "copy", "from": "", "path": "/cp"}])
    want = oneyaml.loads("? [a, b]\n: x\nq: 1\ncp:\n  ? [a, b]\n  : x\n  q: 1\n")
    assert oneyaml.loads(bytes(doc)) == want


# ---- what a collection that is replaced takes with it ----


@pytest.mark.parametrize(
    ("text", "path", "value", "expected"),
    [
        ("- a\n  # f\n", [], {"q": 1}, "q: 1\n"),
        ("x:\n  - a   # t\n  # f\ny: 1\n", ["x"], 5, "x: 5\ny: 1\n"),
        ("a: 1   # one\nb: 2\t# two\n", [], True, "true\n"),
        (" - a\n - b\n", [], {"q": 1}, " q: 1\n"),
        ("a:\n  b: 1\nc: 2\n", ["a"], "x\ny", "a: |-\n  x\n  y\nc: 2\n"),
        ("a: {b: 1}\n", ["a"], "x\ny\n", "a: |\n  x\n  y\n"),
        ("- |+\n   \n", [], 5, "5\n"),
        ("? k\n: - one # a\n  - two # b\n", ["k"], {"q": [1]}, "? k\n:\n  q:\n    - 1\n"),
    ],
)
def test_replacing_a_collection_takes_the_comments_it_owns(api, text, path, value, expected):
    doc = oneyaml.parse(text)
    if path:
        doc[path[0]] = value
    else:
        doc.patch([{"op": "replace", "path": "", "value": value}])
    assert bytes(doc) == expected.encode()


def test_new_content_does_not_copy_the_quoting_of_the_value_it_replaces(api):
    doc = oneyaml.parse('a: "old"\n')
    doc["a"] = {"q": ["zé"]}
    assert bytes(doc) == "a:\n  q:\n    - zé\n".encode()


def test_a_string_with_a_line_of_spaces_is_written_as_a_literal_block(api):
    doc = oneyaml.parse("k: 1\n")
    doc["z"] = "a\n \n"
    assert bytes(doc) == b"k: 1\nz: |\n  a\n   \n"
    assert oneyaml.loads(bytes(doc))["z"] == "a\n \n"


def test_a_member_added_to_a_pair_in_a_flow_sequence_gives_the_pair_braces(api):
    doc = oneyaml.parse("x: [1, a: 0, 2]  # c\ny: 1\n")
    doc["x"][1]["nk"] = "z"
    assert bytes(doc) == b"x: [1, {a: 0, nk: z}, 2]  # c\ny: 1\n"
    doc.patch([{"op": "add", "path": "/x/1/o", "value": 4}])
    assert oneyaml.loads(bytes(doc)) == {"x": [1, {"a": 0, "nk": "z", "o": 4}, 2], "y": 1}
