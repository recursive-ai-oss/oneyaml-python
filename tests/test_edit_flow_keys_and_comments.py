"""Edits next to flow keys, block scalars and comments, and flow keys on consecutive lines.

Every case runs on both backends and has to give the same exact bytes. After an edit the whole
document is read again from the new text and compared with the edited document, so the row
table the edit repaired is checked as well as the text.
"""

import importlib

import pytest

import oneyaml
from oneyaml import _api

# (text, patch, expected bytes)
AGREED = {
    "remove the only entry, an indented comment ends the stream": (
        "k:\n  - a\n  # note\n",
        [{"op": "remove", "path": "/k"}],
        "{}\n",
    ),
    "remove the only entry, an indented comment before a document end marker": (
        "k:\n  - a\n  # note\n...\n",
        [{"op": "remove", "path": "/k"}],
        "{}\n...\n",
    ),
    "remove the only root entry, the comment directly above goes": (
        "# head\na: 1\n",
        [{"op": "remove", "path": "/a"}],
        "{}\n",
    ),
    "set a flow key that has no colon": (
        "{a: 1, b, c: 3}\n",
        [{"op": "replace", "path": "/b", "value": 2}],
        "{a: 1, b: 2, c: 3}\n",
    ),
    "set a flow key that has no colon to a collection": (
        "{a: 1, b, c: 3}\n",
        [{"op": "replace", "path": "/b", "value": [1, 2]}],
        "{a: 1, b: [1, 2], c: 3}\n",
    ),
    "set a plain flow key that runs over two lines": (
        "---\n- { single line, a: b}\n- { multi\n  line, a: b}\n",
        [{"op": "replace", "path": "/1/multi line", "value": 2}],
        "---\n- { single line, a: b}\n- { multi\n  line: 2, a: b}\n",
    ),
    "append after a flow-keyed last entry of a flow sequence": (
        "---\n[\n  [a]: 1\n]\n",
        [{"op": "add", "path": "/-", "value": "zz"}],
        "---\n[\n  [a]: 1, zz\n]\n",
    ),
    "append to a flow sequence of pairs with flow keys": (
        "[{a}: 1, [b]: 2]\n",
        [{"op": "add", "path": "/-", "value": "zz"}],
        "[{a}: 1, [b]: 2, zz]\n",
    ),
    "add a key after a block scalar that ends in a line of spaces": (
        "foo: |\n  x\n   \n",
        [{"op": "add", "path": "/zz", "value": "v"}],
        "foo: |\n  x\n   \nzz: v\n",
    ),
    "add an item after a block scalar that ends in a line of spaces": (
        "- |\n  x\n   \n",
        [{"op": "add", "path": "/-", "value": "v"}],
        "- |\n  x\n   \n- v\n",
    ),
}

# Cases the native backend gets wrong today (the engine refuses the edit as one that would not
# read back). Each is listed in NATIVE_DOCUMENT_DEVIATIONS in conftest.py with its kit case.
PURE_FIRST = {
    "set a flow key that has a colon and no value": (
        "{a: 1, b:, c: 3}\n",
        [{"op": "replace", "path": "/b", "value": 2}],
        "{a: 1, b: 2, c: 3}\n",
    ),
    "set a flow key that has a colon, a space and no value": (
        "{a: 1, b: , c: 3}\n",
        [{"op": "replace", "path": "/b", "value": 2}],
        "{a: 1, b: 2, c: 3}\n",
    ),
    "add a key after a block scalar ending in spaces, with no final line break": (
        "foo: |\n  x\n   ",
        [{"op": "add", "path": "/zz", "value": "v"}],
        "foo: |\n  x\n   \nzz: v",
    ),
}


@pytest.fixture
def pure(monkeypatch):
    monkeypatch.setattr(_api, "_impl", importlib.import_module("oneyaml._pure"))


def _edit(text, ops):
    doc = oneyaml.parse(text)
    doc.patch(ops)
    out = str(doc)
    # The repaired row table reads as the new text does.
    assert doc.to_python() == oneyaml.loads(out)
    return out


@pytest.mark.parametrize("name", AGREED)
def test_edit_writes_the_exact_bytes(api, name):
    text, ops, expected = AGREED[name]
    assert _edit(text, ops) == expected


@pytest.mark.parametrize("name", PURE_FIRST)
def test_edit_writes_the_exact_bytes_pure_first(api, name):
    text, ops, expected = PURE_FIRST[name]
    assert _edit(text, ops) == expected


# The pure backend takes the comments a removed entry owns: those below it indented deeper than
# its key, and those directly above it when the collection is the document's root. A footer in
# the key's column stays.
@pytest.mark.parametrize(
    ("text", "ops", "expected"),
    [
        (
            "x:\n  # c\n  a: 1\n    # d\n  # e\nz: 2\n",
            [{"op": "remove", "path": "/x/a"}],
            "x: {}\n  # e\nz: 2\n",
        ),
        ("# h\n\n# g\n- a\n  # f\n", [{"op": "remove", "path": "/0"}], "# h\n\n[]\n"),
        ("k:\n  - a\n  # note", [{"op": "remove", "path": "/k"}], "{}"),
    ],
)
def test_removing_the_last_entry_takes_the_comments_it_owns(pure, text, ops, expected):
    assert _edit(text, ops) == expected


def test_flow_sequence_keys_on_consecutive_lines(api):
    text = "[1, 2]: a\n[3]: b\n"
    assert oneyaml.loads(text) == {(1, 2): "a", (3,): "b"}
    assert api.events_text(text.encode()) == [
        "+STR",
        "+DOC",
        "+MAP",
        "+SEQ []",
        "=VAL :1",
        "=VAL :2",
        "-SEQ",
        "=VAL :a",
        "+SEQ []",
        "=VAL :3",
        "-SEQ",
        "=VAL :b",
        "-MAP",
        "-DOC",
        "-STR",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\n[b]: 2\n{c: d}: 3\ne: 4\n",
        "- k: 1\n  [a]: 2\n  {b: 1}: 3\n",
    ],
)
def test_flow_keys_among_other_keys(api, text):
    value = oneyaml.loads(text)
    assert oneyaml.loads(str(oneyaml.parse(text))) == value
