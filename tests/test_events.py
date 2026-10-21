"""The event stream in the yaml-test-suite notation (internal interface both backends offer)."""

import pytest

from oneyaml import YAMLError

CASES = [
    ("a: 1\n", ["+STR", "+DOC", "+MAP", "=VAL :a", "=VAL :1", "-MAP", "-DOC", "-STR"]),
    ("", ["+STR", "-STR"]),
    (
        "---\n- &x 'q'\n- *x\n- !!str |\n  t\n...\n",
        [
            "+STR",
            "+DOC ---",
            "+SEQ",
            "=VAL &x 'q",
            "=ALI *x",
            "=VAL <tag:yaml.org,2002:str> |t\\n",
            "-SEQ",
            "-DOC ...",
            "-STR",
        ],
    ),  # fmt: skip
    (
        "{a: [b, c]}\n",
        [
            "+STR",
            "+DOC",
            "+MAP {}",
            "=VAL :a",
            "+SEQ []",
            "=VAL :b",
            "=VAL :c",
            "-SEQ",
            "-MAP",
            "-DOC",
            "-STR",
        ],
    ),  # fmt: skip
    (
        '"a\\tb\\\\"\n',
        ["+STR", "+DOC", '=VAL "a\\tb\\\\', "-DOC", "-STR"],
    ),
    (
        "%TAG !e! tag:example.com,2024:\n---\n!e!x [!local a]\n",
        [
            "+STR",
            "+DOC ---",
            "+SEQ [] <tag:example.com,2024:x>",
            "=VAL <!local> :a",
            "-SEQ",
            "-DOC",
            "-STR",
        ],
    ),  # fmt: skip
]


@pytest.mark.parametrize(("text", "lines"), CASES)
def test_events(api, text, lines):
    assert api.events_text(text) == lines
    assert api.events_text(text.encode()) == lines


def test_error_carries_the_events_before_it(api):
    with pytest.raises(YAMLError) as e:
        api.events_text("a: [1, 2\nb: 3\n")
    err = e.value
    assert err.category == "syntax"
    assert err.events[:5] == ["+STR", "+DOC", "+MAP", "=VAL :a", "+SEQ []"]
    assert (err.line, err.column) == (2, 1)


def test_encoding_error_has_empty_events(api):
    with pytest.raises(YAMLError) as e:
        api.events_text(b"\xff\xfe\xfd")
    assert e.value.category == "encoding"
    assert e.value.events == []
    assert (e.value.line, e.value.column) == (1, 1)
