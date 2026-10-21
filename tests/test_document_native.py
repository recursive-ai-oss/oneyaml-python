"""What only the native document handle has: token types, the lock, hostile callbacks."""

import pytest

import oneyaml
from oneyaml import Limits, YAMLError

pytestmark = pytest.mark.native_only


@pytest.fixture
def native(monkeypatch):
    from oneyaml import _api, _native

    monkeypatch.setattr(_api, "_impl", _native)
    return _native


def test_the_handle_takes_text_and_bytes_and_reports_its_encoding(native):
    h = native.parse("a: 1\n".encode("utf-16"), Limits())
    assert h.encoding == "utf-16-le" and h.count() == 1
    assert bytes(h.emit()) == "a: 1\n".encode("utf-16")


def test_tokens_are_text_or_exact_scalar_keys(native):
    h = native.parse('1: a\n"1": b\ntrue: c\n', Limits())
    assert h.value(0, [1]) == "a" and h.value(0, ["1"]) == "b" and h.value(0, [True]) == "c"
    with pytest.raises(TypeError):
        h.value(0, [object()])
    with pytest.raises(TypeError):
        h.value(0, [{"a": 1}.keys()])
    with pytest.raises(IndexError):
        h.value(5, [])
    with pytest.raises(TypeError):
        h.value(0, 5)


def test_a_text_token_finds_a_key_that_is_not_a_string(native):
    h = native.parse("1: a\n0x10: b\n~: c\n", Limits())
    assert h.value(0, ["1"]) == "a" and h.value(0, ["16"]) == "b" and h.value(0, ["null"]) == "c"
    with pytest.raises(KeyError):
        h.value(0, ["2"])


def test_version_moves_when_nodes_move(native):
    h = native.parse("a: [1, 2]\nb: 1\n", Limits())
    v = h.version()
    h.set(0, ["b"], 2)
    h.set(0, ["c"], 3)
    h.insert(0, ["a"], 2, 3)  # appends: nothing moves
    assert h.version() == v
    h.insert(0, ["a"], 0, 0)
    assert h.version() > v
    v = h.version()
    h.set(0, ["a"], [1])
    assert h.version() > v
    v = h.version()
    h.remove(0, ["c"])
    assert h.version() > v


def test_a_failed_edit_leaves_the_text_and_version_alone(native):
    text = "a: &x [1]\nb: *x\n"
    h = native.parse(text, Limits())
    v = h.version()
    for call in (
        lambda: h.insert(0, ["b"], 0, 1),
        lambda: h.remove(0, ["a"]),
        lambda: h.set(0, ["b", "0"], 1),
        lambda: h.set(0, ["zz", "y"], 1),
        lambda: h.set(0, ["a"], object()),
    ):
        with pytest.raises((YAMLError, KeyError, TypeError)):
            call()
    assert bytes(h.emit()) == text.encode() and h.version() == v


def test_callbacks_run_by_a_value_can_use_the_document(native):
    """The value is converted before the document is locked, so it may read or edit it."""
    doc = oneyaml.parse("a: 1\nlog: []\n")

    class Spy(dict):
        def items(self):
            doc["log"].append("seen")
            doc["touched"] = True
            return super().items()

    doc["a"] = Spy(k=1)
    assert doc.to_python() == {"a": {"k": 1}, "log": ["seen"], "touched": True}


def test_a_dict_that_changes_while_it_is_read_is_read_as_it_was(native):
    doc = oneyaml.parse("a: 1\n")

    class Shifty(dict):
        def items(self):
            snapshot = list(super().items())
            self["late"] = 1
            return iter(snapshot)

    doc["a"] = Shifty(k=1)
    assert doc["a"] == {"k": 1}


def test_a_hostile_value_cannot_break_the_handle(native):
    class Boom:
        def __hash__(self):
            raise RuntimeError("no")

    doc = oneyaml.parse("a: 1\n")
    for bad in (Boom(), [Boom()], {"k": Boom()}):
        with pytest.raises((TypeError, RuntimeError, ValueError)):
            doc["a"] = bad
    assert str(doc) == "a: 1\n"


def test_a_call_made_while_the_document_is_being_read_is_refused_not_waited_for(
    native, monkeypatch
):
    """Building a value runs Python code (here, a tagged value's constructor). If that code uses
    the same document on the same thread, it gets an error; it must not wait for itself."""
    doc = oneyaml.parse("a: !t x\nb: 1\n")
    seen = []
    real = oneyaml.Tagged.__init__

    def spy(self, tag, value):
        try:
            doc["b"]
        except YAMLError as e:
            seen.append(e)
        real(self, tag, value)

    monkeypatch.setattr(oneyaml.Tagged, "__init__", spy)
    assert doc.to_python()["a"] == oneyaml.Tagged("!t", "x")
    assert len(seen) == 1 and "already in use" in seen[0].message
    monkeypatch.undo()
    assert doc["b"] == 1


def test_insert_past_the_end_appends_in_one_call(native):
    h = native.parse("l: [1]\n", Limits())
    h.insert(0, ["l"], 2**62, 2)
    h.insert(0, ["l"], 1, 9)
    assert h.value(0, ["l"]) == [1, 9, 2]
