"""A new block sequence copies a sibling sequence's indentation; every byte order mark that
starts a line of a document prefix is zero-width for positions."""

import pytest

import oneyaml
from oneyaml import YAMLError

# ---- indentation of a new nested sequence ----


@pytest.mark.parametrize(
    ("text", "path", "value", "expected"),
    [
        ("k:\n- a\n", "n", ["x"], "k:\n- a\nn:\n- x\n"),
        ("k:\n- a\nm: 1\n", "n", ["x", "y"], "k:\n- a\nm: 1\nn:\n- x\n- y\n"),
        # nested deeper: the sequence inside the new mapping copies the sibling too
        ("k:\n- a\n", "n", {"m": ["x"]}, "k:\n- a\nn:\n  m:\n  - x\n"),
        # no sibling sequence: the block step applies
        ("k:\n    m: 1\n", "n", ["x"], "k:\n    m: 1\nn:\n    - x\n"),
        ("k: 1\n", "n", ["x"], "k: 1\nn:\n  - x\n"),
    ],
)
def test_a_new_sequence_copies_a_sibling_sequence_indent(api, text, path, value, expected):
    doc = oneyaml.parse(text)
    target = doc
    *head, last = path.split(".")
    for part in head:
        target = target[part]
    target[last] = value
    out = bytes(doc).decode()
    assert oneyaml.loads(out) == _expected_value(text, path, value)
    if expected is not None:
        assert out == expected


def _expected_value(text, path, value):
    data = oneyaml.loads(text)
    target = data
    *head, last = path.split(".")
    for part in head:
        target = target[part]
    target[last] = value
    return data


def test_a_new_sequence_copies_an_indented_sibling_sequence(api):
    doc = oneyaml.parse("k:\n    - a\n")
    doc["n"] = ["x"]
    assert bytes(doc) == b"k:\n    - a\nn:\n    - x\n"


def test_a_sequence_in_an_item_of_a_flush_sequence_is_flush_too(api):
    doc = oneyaml.parse("one:\n- 2\n- 3\nfour: 5\n")
    doc["one"][1] = {"k": [1, 2]}
    assert bytes(doc) == b"one:\n- 2\n- k:\n  - 1\n  - 2\nfour: 5\n"


# ---- byte order marks inside a document prefix ----


def test_a_mark_that_starts_a_prefix_line_is_zero_width_for_locate(api):
    for text in [
        "\ufeff\ufeff# c\n\ufeffa: 1\n",
        "\ufeff# c\n\ufeff\ufeffa: 1\n",
        "a: 1\n...\n\ufeff# c\n\ufeffb: 1\n",
    ]:
        doc = oneyaml.parse(text)
        last = len(oneyaml.loads_all(text)) - 1
        path = "/a" if last == 0 else "/b"
        loc = doc.locate(path, document=last)
        assert loc.column == 4, text


def test_a_mark_that_starts_a_prefix_line_is_zero_width_for_errors(api):
    with pytest.raises(YAMLError) as e:
        oneyaml.loads("\ufeff# c\n\ufeff@\n")
    assert (e.value.line, e.value.column) == (2, 1)
    with pytest.raises(YAMLError) as e:
        oneyaml.loads_all("a: 1\n...\n\ufeff# c\n\ufeff@x\n")
    assert (e.value.line, e.value.column) == (4, 1)
