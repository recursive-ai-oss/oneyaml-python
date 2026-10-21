"""``default`` and ``sort_keys`` on the writers, and documents written by ``dumps``."""

import copy
import datetime as dt
import decimal
import fractions
import pathlib
import sys

import pytest

import oneyaml
from oneyaml import FrozenMapping, LimitError, Tagged


def test_default_is_called_for_unknown_objects(api):
    out = oneyaml.dumps(
        {"d": dt.date(2026, 10, 1), "p": pathlib.PurePosixPath("/a/b"), "n": 1},
        default=str,
    )
    assert api.loads(out) == {"d": "2026-10-01", "p": "/a/b", "n": 1}


def test_without_default_an_unknown_object_is_a_type_error(api):
    assert oneyaml.dumps({"d": dt.date(2026, 10, 1)}, default=str) == "d: '2026-10-01'\n"
    with pytest.raises(TypeError, match="date"):
        oneyaml.dumps({"d": dt.date(2026, 10, 1)})


def test_default_may_return_more_objects_for_default(api):
    def default(o):
        if isinstance(o, dt.date):
            return {"year": o.year, "when": decimal.Decimal("1.5")}
        if isinstance(o, decimal.Decimal):
            return str(o)
        raise TypeError(f"no {type(o).__name__}")

    out = oneyaml.dumps([dt.date(2026, 1, 2), {"k": dt.date(2027, 1, 1)}], default=default)
    assert api.loads(out) == [
        {"year": 2026, "when": "1.5"},
        {"k": {"year": 2027, "when": "1.5"}},
    ]


def test_default_raising_type_error_reaches_the_caller(api):
    def refuse(o):
        raise TypeError(f"cannot write {type(o).__name__}")

    with pytest.raises(TypeError, match="cannot write set"):
        oneyaml.dumps({"s": {1, 2}}, default=refuse)


def test_default_that_returns_its_argument_is_a_circular_reference(api):
    with pytest.raises(ValueError, match="Circular"):
        oneyaml.dumps([object()], default=lambda o: o)


def test_default_must_be_callable(api):
    with pytest.raises(TypeError, match="callable"):
        oneyaml.dumps({}, default="str")


def test_default_is_not_called_for_what_can_be_written(api):
    calls = []
    value = {"a": [1, 2.5, "s", None, True, b"x", (1, 2)], "t": Tagged("!t", "v")}
    out = oneyaml.dumps(value, default=lambda o: calls.append(o))
    assert not calls
    assert out == oneyaml.dumps(value)


def test_sort_keys_orders_every_mapping(api):
    value = {"b": {"z": 1, "w": [{"q": 1, "p": 2}]}, "a": FrozenMapping({"d": 1, "c": 2})}
    out = oneyaml.dumps(value, sort_keys=True)
    assert out == "a:\n  c: 2\n  d: 1\nb:\n  w:\n  - p: 2\n    q: 1\n  z: 1\n"
    assert list(api.loads(out)) == ["a", "b"]
    assert oneyaml.dumps({"b": 1, "a": 2}) == "b: 1\na: 2\n"  # off by default


def test_sort_keys_with_keys_that_do_not_compare_is_a_type_error(api):
    assert oneyaml.dumps({"b": 1, "a": 2}, sort_keys=True) == "a: 2\nb: 1\n"
    with pytest.raises(TypeError, match="sort_keys"):
        oneyaml.dumps({1: "a", "b": 2}, sort_keys=True)


def test_sort_keys_keeps_tagged_content_sorted_too(api):
    out = oneyaml.dumps(Tagged("!t", {"b": 1, "a": 2}), sort_keys=True)
    assert out.index("a: 2") < out.index("b: 1")


def test_default_and_sort_keys_in_the_all_and_file_forms(api, tmp_path):
    docs = [{"b": dt.date(2026, 1, 1), "a": 1}, {"z": 0, "y": 1}]
    text = oneyaml.dumps_all(docs, default=str, sort_keys=True)
    assert api.loads_all(text) == [{"a": 1, "b": "2026-01-01"}, {"y": 1, "z": 0}]
    path = tmp_path / "out.yaml"
    with path.open("w") as fp:
        oneyaml.dump(docs[0], fp, default=str, sort_keys=True)
    assert path.read_text() == "a: 1\nb: '2026-01-01'\n"
    with path.open("wb") as fp:
        oneyaml.dump_all(docs, fp, default=str, sort_keys=True)
    assert api.loads_all(path.read_text()) == [{"a": 1, "b": "2026-01-01"}, {"y": 1, "z": 0}]


def test_a_shared_object_is_written_where_it_is_used(api):
    shared = {"k": dt.date(2026, 1, 1)}
    out = oneyaml.dumps({"a": shared, "b": shared}, default=str)
    assert api.loads(out) == {"a": {"k": "2026-01-01"}, "b": {"k": "2026-01-01"}}


def test_a_value_that_contains_itself_is_refused_with_the_options(api):
    loop = []
    loop.append(loop)
    with pytest.raises(ValueError, match="Circular"):
        oneyaml.dumps(loop, sort_keys=True)
    d = {}
    d["self"] = d
    with pytest.raises(ValueError, match="Circular"):
        oneyaml.dumps(d, default=str)


def test_deep_values_do_not_recurse(api):
    deep = []
    for _ in range(sys.getrecursionlimit() * 3):
        deep = [deep]
    out = oneyaml.dumps(deep, sort_keys=True, flow=True)
    assert out.startswith("[[[[")
    too_deep = []
    for _ in range(10_001):
        too_deep = [too_deep]
    with pytest.raises(LimitError):
        oneyaml.dumps(too_deep, sort_keys=True)


# ---- documents and views -----------------------------------------------------------------------


def test_dumps_writes_a_document_and_its_views(api):
    doc = oneyaml.parse("a:\n  b: 1  # c\nl: [1, 2]\n")
    assert oneyaml.dumps(doc) == "a:\n  b: 1\nl:\n- 1\n- 2\n"
    assert oneyaml.dumps(doc["a"]) == "b: 1\n"
    assert oneyaml.dumps(doc["l"], flow=True) == "[1, 2]\n"
    assert oneyaml.dumps_all([doc["a"], doc["l"]]) == "b: 1\n---\n- 1\n- 2\n"


def test_dumps_writes_a_document_as_it_is_now(api):
    doc = oneyaml.parse("a: 1\n")
    doc["a"] = 2
    doc["b"] = [3]
    assert api.loads(oneyaml.dumps(doc)) == {"a": 2, "b": [3]}


def test_views_inside_a_converted_value_are_written(api):
    doc = oneyaml.parse("a: {x: 1}\nb: {y: 2}\n")
    out = oneyaml.dumps({"first": doc["a"], "second": doc["b"]}, sort_keys=True)
    assert api.loads(out) == {"first": {"x": 1}, "second": {"y": 2}}


def test_copying_a_document_or_a_view_gives_plain_data(api):
    doc = oneyaml.parse("a: {b: [1, 2]}  # c\nd: 1\n")
    assert copy.deepcopy(doc) == {"a": {"b": [1, 2]}, "d": 1}
    assert type(copy.deepcopy(doc)) is dict
    view = copy.deepcopy(doc["a"])
    assert view == {"b": [1, 2]} and type(view) is dict
    shallow = copy.copy(doc["a"]["b"])
    assert shallow == [1, 2] and type(shallow) is list
    view["b"].append(3)  # the copy is independent of the document
    assert doc["a"]["b"] == [1, 2]
    assert copy.deepcopy({"k": doc["a"]}) == {"k": {"b": [1, 2]}}


@pytest.mark.skipif(sys.version_info < (3, 15), reason="frozendict is built in from Python 3.15")
def test_frozendict_is_written_as_a_mapping(api):
    frozendict = frozendict_type()
    value = {"a": frozendict(x=1, y=[frozendict(z=2)])}
    assert api.loads(oneyaml.dumps(value)) == {"a": {"x": 1, "y": [{"z": 2}]}}
    assert api.loads(oneyaml.dumps(frozendict(b=1, a=2), sort_keys=True)) == {"a": 2, "b": 1}
    out = oneyaml.dumps({frozendict(k=1): "v"})
    assert api.loads(out) == {FrozenMapping({"k": 1}): "v"}


def frozendict_type():
    import builtins

    return builtins.frozendict


def test_the_error_for_an_unknown_type_still_names_it(api):
    with pytest.raises(TypeError, match="Fraction"):
        oneyaml.dumps(fractions.Fraction(1, 3), sort_keys=True)
