"""The edge between a document and the Python values handed to it, and how pointers and
views name keys."""

import threading
from collections.abc import Sequence

import pytest

import oneyaml
from oneyaml import LimitError, Limits, PatchError, StaleViewError, YAMLError

# ---- no code of the caller's runs while the stream is locked ------------------------------------


class Asks(Sequence):
    """A value that, when read, makes another thread read the same document and waits for it."""

    def __init__(self, doc):
        self.doc = doc
        self.finished = None

    def __len__(self):
        return 1

    def __getitem__(self, i):
        if i:
            raise IndexError(i)
        seen = []
        helper = threading.Thread(target=lambda: seen.append(self.doc["cfg"]["n"]))
        helper.start()
        helper.join(2)
        self.finished = not helper.is_alive()
        return seen[0] if seen else None


@pytest.mark.parametrize("how", ["set", "insert", "append", "patch", "merge_patch"])
def test_a_value_that_waits_for_another_thread_does_not_deadlock(api, how):
    doc = oneyaml.parse("cfg: {n: 3}\nl: []\n")
    value = Asks(doc)
    if how == "set":
        doc["x"] = value
    elif how == "insert":
        doc["l"].insert(0, value)
    elif how == "append":
        doc["l"].append(value)
    elif how == "patch":
        doc.patch([{"op": "add", "path": "/x", "value": value}])
    else:
        doc.merge_patch({"x": value})
    assert value.finished, "the other thread was kept waiting for the stream lock"
    got = doc.to_python()
    if how in ("set", "patch", "merge_patch"):
        assert got["x"] == [3]
    else:
        assert got["l"] == [[3]]


# ---- shared structure is charged as aliases are ------------------------------------------------


def shared(depth, leaf="x"):
    v = [leaf]
    for _ in range(depth):
        v = [v, v]
    return v


def assign(doc, how, value):
    if how == "set":
        doc["a"] = value
    elif how == "insert":
        doc["l"].insert(0, value)
    elif how == "append":
        doc["l"].append(value)
    elif how == "patch":
        doc.patch([{"op": "add", "path": "/b", "value": value}])
    else:
        doc.merge_patch({"a": value})


HOWS = ["set", "insert", "append", "patch", "merge_patch"]


@pytest.mark.parametrize("how", HOWS)
def test_a_value_that_shares_itself_into_a_huge_expansion_is_refused(api, how):
    doc = oneyaml.parse("a: 1\nl: []\n")
    before = str(doc)
    # 2**40 leaves as a tree; a few dozen objects as Python holds it.
    with pytest.raises(LimitError) as e:
        assign(doc, how, shared(40))
    assert e.value.category == "limit"
    assert str(doc) == before


@pytest.mark.parametrize("how", HOWS)
def test_shared_big_text_meets_the_byte_budget(api, how):
    doc = oneyaml.parse("a: 1\nl: []\n")
    before = str(doc)
    big = "x" * (1 << 20)
    with pytest.raises(LimitError):
        assign(doc, how, shared(8, big))  # 256 MiB of text written out
    assert str(doc) == before


def test_a_value_that_shares_a_little_is_written_out_in_full(api):
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = shared(6)
    assert oneyaml.loads(str(doc))["a"] == oneyaml.loads(str(doc))["a"]
    leaves = str(doc).count("- x")
    assert leaves == 2**6


def test_the_budget_is_the_documents_own(api):
    value = shared(12)  # 4096 leaves
    tight = oneyaml.parse("a: 1\n", limits=Limits(alias_nodes=(0, 100)))
    with pytest.raises(LimitError):
        tight["a"] = value
    loose = oneyaml.parse("a: 1\n", limits=Limits.none())
    loose["a"] = value
    assert str(loose).count("- x") == 2**12


def test_a_long_conversion_hears_ctrl_c(api):
    import _thread

    doc = oneyaml.parse("a: 1\n")
    big = [0] * 6_000_000
    timer = threading.Timer(0.05, _thread.interrupt_main)
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            doc.merge_patch({"a": big})
    finally:
        timer.cancel()
    assert str(doc) == "a: 1\n", "the edit must not go through after the interrupt"


# ---- a patch given as text obeys the document's limits -----------------------------------------


def bomb_patch_text():
    levels = ["&a [" + ",".join(["x"] * 10) + "]"]
    for name, prev in zip("bcd", "abc", strict=True):
        levels.append(f"&{name} [" + ",".join(["*" + prev] * 10) + "]")
    levels.append("[" + ",".join(["*d"] * 10) + "]")
    ops = [f'{{"op":"add","path":"/k{i}","value": {v}}}' for i, v in enumerate(levels)]
    return "[" + ",".join(ops) + "]"


def test_a_patch_text_is_read_under_the_documents_limits(api):
    text = bomb_patch_text()
    with pytest.raises(YAMLError) as read:
        oneyaml.loads(text, limits=Limits.untrusted())
    assert read.value.category == "limit"
    doc = oneyaml.parse("a: 1\n", limits=Limits.untrusted())
    with pytest.raises(PatchError) as e:
        doc.patch(text)
    assert e.value.category == "limit"
    assert str(doc) == "a: 1\n"


def test_a_deep_patch_text_is_read_under_the_limits_the_document_allows(api):
    deep = "[" * 3000 + "]" * 3000
    text = '[{"op": "add", "path": "/k", "value": ' + deep + "}]"
    doc = oneyaml.parse("a: 1\n")
    doc.patch(text)
    assert "k:" in str(doc)
    strict = oneyaml.parse("a: 1\n", limits=Limits.untrusted())
    with pytest.raises(PatchError) as e:
        strict.patch(text)
    assert e.value.category == "limit"


# ---- a pointer is read by the engine, for views as for patches ---------------------------------

POINTERS = [
    ("0x1: a\n", "/1"),
    ("1: a\n", "/0x1"),
    ("true: a\n", "/True"),
    ("~: a\n", "/null"),
    ("1.0: a\n", "/1.00"),
    ("a: [x, y]\n", "/a/01"),
    ("a: 1\n", "/a/0"),
]


@pytest.mark.parametrize(("src", "pointer"), POINTERS)
def test_a_pointer_that_names_no_key_text_finds_nothing(api, src, pointer):
    doc = oneyaml.parse(src)
    with pytest.raises(KeyError):
        doc.at(pointer)
    with pytest.raises(PatchError) as e:
        doc.patch([{"op": "test", "path": pointer, "value": "a"}])
    assert e.value.category == "patch-path"


def test_a_pointer_finds_a_key_by_its_written_text(api):
    doc = oneyaml.parse("0x1: {x: 1}\ntrue: {y: 2}\n")
    assert doc.at("/0x1").to_python() == {"x": 1}
    assert doc.at("/true/y") == 2


def test_a_view_from_a_pointer_goes_stale_with_the_key_it_names(api):
    doc = oneyaml.parse("0x1: {x: 1}\nother: {y: 2}\n")
    view = doc.at("/0x1")
    keep = doc.at("/other")
    del doc[1]  # the key 0x1 is the integer 1
    with pytest.raises(StaleViewError):
        view["x"]
    assert keep["y"] == 2


def test_a_pointer_through_a_sequence_names_the_item(api):
    doc = oneyaml.parse("a:\n  - {n: 1}\n  - {n: 2}\n")
    view = doc.at("/a/1")
    del doc["a"][0]
    with pytest.raises(StaleViewError):
        view["n"]


# ---- the cost of a staleness check is the depth of the view --------------------------------------


class CountingDict(dict):
    gets = 0

    def get(self, key, default=None):
        CountingDict.gets += 1
        return super().get(key, default)


def lookups_for_one_check(depth, monkeypatch):
    from oneyaml import _document

    monkeypatch.setattr(_document, "_KIDS", CountingDict)
    text = "r: " + "[" * depth + "1, 0]" + ", 0]" * (depth - 1) + "\n"
    doc = oneyaml.parse(text)
    view = doc["r"]
    parents = [view]
    for _ in range(depth - 1):
        view = view[0]
        parents.append(view)
    del parents[-1][1]  # records the path to the bottom: one book per level
    other = parents[-2]
    core = other._core
    CountingDict.gets = 0
    assert not core.stale(other._d, other._path, other._tick)
    return CountingDict.gets


def test_a_staleness_check_walks_the_path_once(api, monkeypatch):
    small = lookups_for_one_check(1000, monkeypatch)
    large = lookups_for_one_check(4000, monkeypatch)
    assert 900 <= small <= 1100
    assert 3900 <= large <= 4100


# ---- ints and keys that are not what they seem -------------------------------------------------


def test_an_int_subclass_is_converted_without_asking_it_anything(api):
    class Sneaky(int):
        def bit_length(self):
            raise RuntimeError("asked")

    doc = oneyaml.parse("a: 1\n")
    doc["a"] = Sneaky(2**70)
    assert doc.to_python() == {"a": 2**70}


def test_a_huge_integer_key_is_a_missing_key(api):
    doc = oneyaml.parse("a: 1\n")
    with pytest.raises(KeyError):
        doc[10**5000]
    assert 10**5000 not in doc


def test_a_missing_key_is_shown_as_python_shows_it(api):
    doc = oneyaml.parse("a: 1\n")
    for key, shown in ((-(2**70), "-1180591620717411303424"), (True, "True"), (None, "None")):
        with pytest.raises(KeyError) as e:
            doc[key]
        assert e.value.args == (shown,)


def test_a_lone_surrogate_names_no_key(api):
    doc = oneyaml.parse("a: 1\n")
    with pytest.raises(KeyError):
        doc["\ud800"]
    assert "\ud800" not in doc
    with pytest.raises(KeyError):
        doc["\ud800"] = 1
    with pytest.raises(PatchError) as e:
        doc.at("/\ud800")
    assert e.value.category == "patch-invalid"
