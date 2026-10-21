"""``Document.locate`` and ``.comment`` on the edges: comments that belong to a value, user code
that runs while a path is read, one walk per call, block scalar ends, byte order marks,
anchored roots after an edit, and huge sequence steps."""

import threading

import pytest

import oneyaml
from oneyaml import Limits, Location

# ---- comments the value owns -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path", "comment"),
    [
        ("---\n? key\n# comment\n: value\n", "/key", "# comment"),
        ("? key\n# c\n: [1]\n", "/key", "# c"),
        ("a:\n  # c\n  [1, 2]\n", "/a", "# c"),
        ("a:\n  # c\n  [1, 2]\n", "/a/0", None),
        ("a:\n  # c\n  b\n", "/a", "# c"),
        ("? # q\n - s\n: # c\n - t\n", [("s",)], "# q\n# c"),
    ],
)
def test_a_value_reads_the_comments_the_entry_gives_it(api, text, path, comment):
    doc = oneyaml.parse(text)
    assert doc.locate(path).comment == comment


# ---- user code never runs under the stream's lock ----------------------------------------------


def test_a_path_is_read_before_the_lock_is_taken(api):
    doc = oneyaml.parse("items: [a, b]\nk: v\n")
    blocked = []

    def edit():
        doc["k"] = "w"

    def path():
        yield "items"
        t = threading.Thread(target=edit)
        t.start()
        t.join(5)
        blocked.append(t.is_alive())
        yield 0

    assert doc.locate(path()) == doc.locate("/items/0")
    assert blocked == [False]
    assert doc["k"] == "w"


# ---- one walk per call -------------------------------------------------------------------------


class Counting:
    """Wraps a backend handle and counts the calls made through it."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def counted(*args, **kwargs):
            self.calls.append(name)
            return attr(*args, **kwargs)

        return counted


@pytest.mark.parametrize("depth", [50, 500])
def test_locating_a_deep_path_makes_one_backend_call(api, depth):
    src = "[" * depth + "]" * depth
    doc = oneyaml.parse(src, limits=Limits().replace(depth=5000))
    counting = Counting(doc._core.h)
    doc._core.h = counting
    where = doc.locate((0,) * (depth - 1))
    assert where.column == depth
    assert counting.calls == ["count", "locate"] or counting.calls == ["locate"]
    assert counting.calls.count("locate") == 1
    assert not {"kind", "length", "keys", "info"} & set(counting.calls)


def test_a_negative_step_counts_from_the_end_in_the_same_walk(api):
    doc = oneyaml.parse("a:\n  - x\n  - y\n  - z\n")
    counting = Counting(doc._core.h)
    doc._core.h = counting
    assert doc.locate(("a", -1)) == Location(4, 5, 4, 6)
    assert counting.calls.count("locate") == 1
    assert "length" not in counting.calls


# ---- where a block scalar ends -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "path", "span"),
    [
        ("- |+\n   \n", ("0",), (1, 3, 1, 5)),
        ("foo: |\n  x\n   \n", ("foo",), (1, 6, 3, 4)),
        ("foo: |\n  x\n   ", ("foo",), (1, 6, 3, 4)),
        ("--- |+\n ab\n \n  \n...\n", (), (1, 5, 4, 3)),
        ("a: |\n  x\n  \n    \nb: 1\n", ("a",), (1, 4, 4, 5)),
        ("a: |\n  \n  \nb: 1\n", ("a",), (1, 4, 1, 5)),
        ("a: >-\n  x y\n  z   \nb: 1\n", ("a",), (1, 4, 3, 7)),
    ],
)
def test_a_block_scalar_ends_after_its_last_content_character(api, text, path, span):
    doc = oneyaml.parse(text)
    assert tuple(doc.locate(path))[:4] == span


def test_a_collection_ends_where_its_last_block_scalar_does(api):
    doc = oneyaml.parse("a:\n  b: |\n    x\n     \n")
    assert tuple(doc.locate("/a"))[2:4] == tuple(doc.locate("/a/b"))[2:4]


# ---- byte order marks and anchored roots -------------------------------------------------------


def test_a_byte_order_mark_that_begins_a_later_document_is_no_column(api):
    doc = oneyaml.parse("a: 1\n...\n\ufeffb: 2\n")
    assert doc.locate(("b",), document=1) == Location(3, 4, 3, 5)


def test_a_byte_order_mark_at_the_start_of_the_stream_is_no_column(api):
    assert oneyaml.parse("\ufeffa: 1\n").locate("/a") == Location(1, 4, 1, 5)


def test_an_anchored_root_replaced_by_a_collection_starts_at_its_anchor(impl):
    h = impl.parse(b"&sequence\n- a\n", Limits())
    assert h.locate(0, [])[:2] == (1, 1)
    h.set(0, [], {"q": [1, "z"]})
    assert h.locate(0, [])[:2] == (1, 1)
    assert bytes(h.emit()).startswith(b"&sequence")


# ---- huge steps --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step", ["9" * 5000, 10**5000, -(10**5000)], ids=["text", "int", "negative"]
)
def test_a_sequence_step_past_any_length_is_a_lookup_error(api, step):
    doc = oneyaml.parse("items: [a, b]\n")
    with pytest.raises((IndexError, KeyError)):
        doc.locate(("items", step))
