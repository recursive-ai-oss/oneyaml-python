"""How a document talks to its backend: few calls, and the bytes it writes right.

The counts are deterministic (calls through a spy), never times.
"""

import pytest

import oneyaml
from oneyaml import Limits, PatchError, YAMLError


class Spy:
    """Wraps a backend handle and counts the calls made through it, by name."""

    def __init__(self, h):
        self._h, self.calls = h, {}

    def __getattr__(self, name):
        attr = getattr(self._h, name)
        if not callable(attr):
            return attr

        def call(*a):
            self.calls[name] = self.calls.get(name, 0) + 1
            return attr(*a)

        return call


def _spied(api, text):
    doc = oneyaml.parse(text)
    spy = Spy(doc._core.h)
    doc._core.h = spy
    return doc, spy


# ---- reading ----


def test_reading_a_scalar_through_a_view_is_one_backend_call(api):
    doc, spy = _spied(api, "a:\n  b: 1\n  c: [x, y]\n")
    inner = doc["a"]
    spy.calls.clear()
    assert inner["b"] == 1
    assert spy.calls == {"read": 1}
    spy.calls.clear()
    assert inner["c"][1] == "y"
    assert spy.calls == {"read": 2}


def test_the_root_kind_is_asked_once_until_something_changes_it(api):
    doc, spy = _spied(api, "a: 1\nb: 2\n")
    assert doc["a"] == 1 and doc["b"] == 2
    assert spy.calls.get("kind", 0) == 1


def test_a_patch_that_changes_the_root_is_noticed(api):
    doc = oneyaml.parse("1\n")
    with pytest.raises(TypeError):
        doc["a"]
    doc.patch([{"op": "replace", "path": "", "value": {"a": 1}}])
    assert doc["a"] == 1
    doc.patch([{"op": "replace", "path": "", "value": [5, 6]}])
    assert doc[1] == 6
    with pytest.raises(TypeError):
        doc["a"]
    doc.merge_patch(None)
    with pytest.raises(TypeError):
        doc[0]


def test_a_stream_with_no_document_has_no_items(api):
    doc = oneyaml.parse("# nothing\n")
    with pytest.raises(TypeError):
        doc["a"]
    with pytest.raises(TypeError):
        doc[0] = 1
    assert len(doc.documents) == 0


# ---- writing ----


def test_assigning_a_scalar_is_one_backend_call(api):
    doc, spy = _spied(api, "m:\n  a: 1\n")
    inner = doc["m"]
    spy.calls.clear()
    inner["a"] = "text"
    inner["b"] = 2.5
    assert spy.calls == {"set": 2}
    assert doc.to_python() == {"m": {"a": "text", "b": 2.5}}


def test_set_says_whether_a_collection_may_have_moved(api):
    h = api.impl.parse("a: 1\nb: [1, 2]\nc:\n  d: 1\n", Limits())
    assert h.set(0, ["a"], 2) is False  # a scalar for a scalar
    assert h.set(0, ["e"], 3) is False  # a new key
    assert h.set(0, ["b"], "x") is True  # a list replaced
    assert h.set(0, ["c"], [1]) is True  # a mapping replaced by a collection
    assert h.set(0, ["a"], {"k": 1}) is True  # a scalar replaced by a collection
    assert h.version() == 3


def test_replacing_a_collection_strands_the_views_of_it(api):
    doc = oneyaml.parse("a: [1, 2]\nb: 1\n")
    items = doc["a"]
    doc["b"] = 5
    assert items == [1, 2]
    doc["a"] = "gone"
    with pytest.raises(oneyaml.StaleViewError):
        items[0]


def test_read_agrees_with_kind_and_value(api):
    h = api.impl.parse("a: &x {k: 1}\nb: *x\nc: [1, null]\nd: text\ne:\n", Limits())
    for path in ([], ["a"], ["b"], ["c"], ["c", "1"], ["d"], ["e"], ["c", "0"]):
        kind = h.kind(0, path)
        got = h.read(0, path)
        assert got[0] == kind
        assert got[1] == (h.value(0, path) if kind == "scalar" else None), path
    with pytest.raises(KeyError):
        h.read(0, ["nope"])
    with pytest.raises(IndexError):
        h.read(0, ["c", "9"])
    with pytest.raises(IndexError):
        h.read(3, [])


# ---- the bytes a document writes ----

NATIVE = pytest.mark.native_only


@pytest.fixture
def native(monkeypatch):
    from oneyaml import _api, _native

    monkeypatch.setattr(_api, "_impl", _native)
    return _native


SAMPLES = [
    b"a: 1\nb: [x, y]\n",
    b"",
    b"# only a comment\n",
    b"\xef\xbb\xbfa: 1\n",
    "a: \u00e9\n".encode("utf-16"),
    "a: 1\n".encode("utf-32-le"),
    b"a: 1\r\nb: 2\r\n",
    b"a: 1\n---\nb: 2\n...\n---\nc: 3\n",
    b"a: \xff\xfe\nb: 2\n",  # bytes that are not text
    b"a: caf\xc3\xa9 \xed\xa0\x80\n",
]


@NATIVE
@pytest.mark.parametrize("data", SAMPLES)
def test_the_fast_writer_agrees_with_the_engines(native, data):
    for given in (data, bytearray(data), data.decode("utf-8", "surrogateescape")):
        if isinstance(given, str):
            try:
                given.encode("utf-8")
            except UnicodeEncodeError:
                continue
        try:
            doc = oneyaml.parse(given)
        except YAMLError:
            continue
        h = doc._core.h
        assert bytes(h.emit()) == bytes(h._emit_engine())
        if not isinstance(given, str):
            assert bytes(h.emit()) == data  # an unedited document writes back what it read
        if doc._kind() == "mapping":
            key = next(iter(doc.keys()))
            doc[key] = "edited"
            assert bytes(h.emit()) == bytes(h._emit_engine())
            assert doc.to_python()[key] == "edited"
            assert oneyaml.parse(bytes(h.emit())).to_python() == doc.to_python()


@NATIVE
def test_text_input_is_written_back_exactly_too(native):
    text = "a: 1\n# c\nb: \u00e9\n"
    doc = oneyaml.parse(text)
    assert bytes(doc) == text.encode()


@NATIVE
def test_every_kind_of_change_changes_the_written_bytes(native):
    data = b"a: 1\nl: [1, 2]\nm: {x: 1}\n"
    doc = oneyaml.parse(data)
    seen = [bytes(doc)]

    def changed():
        now = bytes(doc)
        assert now != seen[-1]
        assert now == bytes(doc._core.h._emit_engine())
        assert oneyaml.loads(now) == doc.to_python()
        seen.append(now)

    doc["a"] = 2
    changed()
    doc["l"].append(3)
    changed()
    doc["l"].insert(0, 0)
    changed()
    del doc["m"]["x"]
    changed()
    doc.patch([{"op": "add", "path": "/n", "value": True}])
    changed()
    doc.merge_patch({"a": None})
    changed()
    assert seen[0] == data


@NATIVE
def test_a_refused_change_leaves_the_written_bytes_right(native):
    doc = oneyaml.parse("a: &x [1]\nb: *x\n")
    before = bytes(doc)
    with pytest.raises(YAMLError):
        doc["b"][0] = 2
    with pytest.raises(PatchError):
        doc.patch([{"op": "remove", "path": "/zzz"}])
    with pytest.raises(PatchError):
        doc.patch(
            [
                {"op": "replace", "path": "/a/0", "value": 5},
                {"op": "test", "path": "/a/0", "value": 9},
            ]
        )
    assert bytes(doc) == before == bytes(doc._core.h._emit_engine())


@NATIVE
def test_bytes_written_while_another_thread_edits_are_never_out_of_date(native):
    import threading

    doc = oneyaml.parse(b"n: 0\n")
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            out = bytes(doc)
            if not out.startswith(b"n: ") or not out.endswith(b"\n"):
                bad.append(out)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(1, 300):
            doc["n"] = i
            assert bytes(doc) == b"n: %d\n" % i
    finally:
        stop.set()
        t.join()
    assert not bad
