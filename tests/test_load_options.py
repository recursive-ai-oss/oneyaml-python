"""``parse_float`` and ``tags`` on the readers, on both backends."""

import decimal
import io

import pytest

from oneyaml import Tagged, YAMLError


def test_parse_float_gets_the_text(api):
    got = api.loads("p: 1.10\nq: [2.50, 3e2, .5]\nn: 7\ns: '4.0'\n", parse_float=decimal.Decimal)
    assert got == {
        "p": decimal.Decimal("1.10"),
        "q": [decimal.Decimal("2.50"), decimal.Decimal("3e2"), decimal.Decimal(".5")],
        "n": 7,
        "s": "4.0",
    }
    assert str(got["p"]) == "1.10"


def test_parse_float_leaves_infinity_and_nan_alone(api):
    got = api.loads("[.inf, -.inf, .nan, 1.5]", parse_float=lambda t: ("f", t))
    assert got[0] == float("inf") and got[1] == float("-inf")
    assert got[2] != got[2]
    assert got[3] == ("f", "1.5")


def test_parse_float_applies_to_keys_and_every_document(api):
    got = api.loads_all("1.5: a\n---\n2.5\n", parse_float=str)
    assert got == [{"1.5": "a"}, "2.5"]


def test_parse_float_reads_a_file(api):
    assert api.load(io.StringIO("x: 0.1\n"), parse_float=decimal.Decimal) == {
        "x": decimal.Decimal("0.1")
    }


def test_parse_float_errors_of_the_caller_pass_through(api):
    def boom(text):
        raise ArithmeticError(text)

    with pytest.raises(ArithmeticError):
        api.loads("x: 1.5\n", parse_float=boom)


def test_tags_build_values_from_the_plain_content(api):
    seen = []

    def point(value):
        seen.append(value)
        return tuple(value)

    got = api.loads(
        "a: !env HOME\nb: !point [1, 2]\nc: !point2 {x: 1}\nd: !other 5\ne: !!str 5\n",
        tags={"!env": lambda v: f"<{v}>", "!point": point},
    )
    assert got["a"] == "<HOME>"
    assert got["b"] == (1, 2) and seen == [[1, 2]]
    assert got["c"] == Tagged("!point2", {"x": 1})
    assert got["d"] == Tagged("!other", "5")
    assert got["e"] == "5"


def test_tags_nest_from_the_inside_out(api):
    got = api.loads(
        "!outer [!inner a, !inner b]\n",
        tags={"!inner": str.upper, "!outer": lambda v: "+".join(v)},
    )
    assert got == "A+B"


def test_tags_work_on_anchored_nodes_keys_and_full_tags(api):
    got = api.loads(
        "a: &x !n [1]\nb: *x\n? !n k\n: v\nc: !!python/thing 1\n",
        tags={"!n": lambda v: ("n", v), "tag:yaml.org,2002:python/thing": int},
    )
    assert got["a"] is got["b"] and got["a"] == ("n", [1])
    assert got[("n", "k")] == "v"
    assert got["c"] == 1


def test_a_tag_function_that_raises_reaches_the_caller(api):
    with pytest.raises(KeyError):
        api.loads("a: !env NOPE_NOT_SET\n", tags={"!env": lambda v: {}[v]})


def test_without_tags_an_unknown_tag_stays_tagged(api):
    assert api.loads("a: !env HOME\n") == {"a": Tagged("!env", "HOME")}
    assert api.loads("a: !env HOME\n", tags={"!other": str}) == {"a": Tagged("!env", "HOME")}


@pytest.mark.parametrize(
    "kw",
    [
        {"parse_float": 5},
        {"tags": 5},
        {"tags": {"!a": 5}},
        {"tags": {5: str}},
    ],
)
def test_bad_options_are_a_type_error(api, kw):
    assert api.loads("a: 1\n", parse_float=str, tags={"!t": str}) == {"a": 1}  # the names exist
    with pytest.raises(TypeError, match="parse_float|tags"):
        api.loads("a: 1\n", **kw)


def test_the_options_leave_syntax_errors_as_they_are(api):
    with pytest.raises(YAMLError) as info:
        api.loads("a: [1,\n", parse_float=float, tags={"!a": str})
    assert info.value.category == "syntax"


def test_a_load_names_its_file(api, tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("a: 1\nb: [\n", encoding="utf-8")
    with path.open("rb") as fp, pytest.raises(YAMLError) as info:
        api.load(fp)
    assert info.value.source == str(path)
    assert str(path) in str(info.value)
    assert info.value.line is not None
    with pytest.raises(YAMLError) as info2:
        api.loads(path.read_text())
    assert info2.value.source is None
