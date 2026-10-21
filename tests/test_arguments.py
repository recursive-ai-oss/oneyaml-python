"""Arguments and the Limits class: checks that do not depend on a backend."""

import pickle

import pytest

import oneyaml
from oneyaml import Limits, _api


def test_limits_defaults():
    lim = Limits()
    assert lim.depth == 10_000
    assert lim.alias_nodes == (10, 1_000_000)
    assert lim.alias_bytes == (10, 64 * 1024 * 1024)
    assert lim.int_digits == 4300
    assert lim.input_size is None
    assert lim == Limits()
    assert hash(lim) == hash(Limits())


def test_limits_profiles():
    u = Limits.untrusted()
    assert (u.depth, u.documents, u.input_size) == (1000, 10_000, 64 * 1024 * 1024)
    assert u.int_digits == 4300
    n = Limits.none()
    assert all(getattr(n, f) is None for f in Limits._FIELDS)
    assert n != Limits()


def test_limits_are_immutable_and_replaceable():
    lim = Limits()
    with pytest.raises(AttributeError):
        lim.depth = 5  # type: ignore[misc]
    assert lim.replace(depth=5).depth == 5
    assert lim.depth == 10_000
    with pytest.raises(TypeError):
        lim.replace(depht=5)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"depth": -1}, ValueError),
        ({"depth": 1.5}, TypeError),
        ({"depth": True}, TypeError),
        ({"documents": "3"}, TypeError),
        ({"alias_nodes": (1, -1)}, ValueError),
        ({"scalar_size": 1 << 64}, ValueError),
    ],
)
def test_bad_limits(kwargs, error):
    with pytest.raises(error):
        Limits(**kwargs)


def test_limits_repr_and_pickle():
    lim = Limits(depth=7)
    assert "depth=7" in repr(lim)
    assert pickle.loads(pickle.dumps(lim)) == lim


@pytest.fixture
def seen(monkeypatch):
    calls = []

    class Fake:
        @staticmethod
        def load(data, limits, multi):
            calls.append((data, limits, multi))
            return "ok"

    monkeypatch.setattr(_api, "_impl", Fake)
    return calls


def test_bad_arguments_are_type_errors(seen):
    for bad in (None, 1, [], {"a": 1}, object()):
        with pytest.raises(TypeError):
            oneyaml.loads(bad)
        with pytest.raises(TypeError):
            oneyaml.loads_all(bad)
    with pytest.raises(TypeError):
        oneyaml.loads("a", limits={"depth": 1})
    with pytest.raises(TypeError):
        oneyaml.loads("a", {})  # limits is keyword-only
    assert seen == []


def test_arguments_reach_the_backend(seen):
    lim = Limits(depth=3)
    assert oneyaml.loads("a", limits=lim) == "ok"
    oneyaml.loads_all(b"a")
    oneyaml.loads(bytearray(b"x"))
    assert seen[0] == ("a", lim, False)
    assert seen[1][0] == b"a" and seen[1][2] is True
    assert seen[1][1] == Limits()
    assert seen[2][0] == b"x" and type(seen[2][0]) is bytes


def test_load_reads_the_file(seen, tmp_path):
    p = tmp_path / "f.yaml"
    p.write_text("a: 1\n")
    with open(p) as f:
        oneyaml.load(f)
    with open(p, "rb") as f:
        oneyaml.load_all(f)
    assert seen[0] == ("a: 1\n", Limits(), False)
    assert seen[1] == (b"a: 1\n", Limits(), True)
    with pytest.raises(TypeError):
        oneyaml.load(object())  # not a file


def test_public_names():
    for name in oneyaml.__all__:
        assert hasattr(oneyaml, name)
    assert issubclass(oneyaml.YAMLError, ValueError)
    assert issubclass(oneyaml.LimitError, oneyaml.YAMLError)
    assert issubclass(oneyaml.ParseError, oneyaml.YAMLError)


def test_tagged_and_frozen_mapping():
    t = oneyaml.Tagged("!x", [1])
    assert t == oneyaml.Tagged("!x", [1])
    assert t != oneyaml.Tagged("!y", [1])
    assert repr(t) == "Tagged('!x', [1])"
    with pytest.raises(AttributeError):
        t.tag = "!z"  # type: ignore[misc]
    f = oneyaml.FrozenMapping({"a": 1})
    assert f == {"a": 1} and dict(f) == {"a": 1} and len(f) == 1 and "a" in f
    assert hash(f) == hash(oneyaml.FrozenMapping({"a": 1}))
    assert pickle.loads(pickle.dumps(f)) == f
    assert pickle.loads(pickle.dumps(t)) == t
