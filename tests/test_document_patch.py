"""Patches and pointers on a lossless document: RFC 6902 and RFC 7396, all or nothing."""

import json
import pickle

import pytest

import oneyaml
from oneyaml import PatchError, StaleViewError, YAMLError

TEXT = "# deploy settings\nname: api\nreplicas: 2  # raised by the release job\n"


def failing(doc, call, *args, **kw):
    before = str(doc)
    with pytest.raises(PatchError) as e:
        call(*args, **kw)
    assert str(doc) == before, "a failed patch must leave the text as it was"
    return e.value


# ---- the quick start -------------------------------------------------------------------------


def test_the_quick_start_runs_as_written(api):
    config = oneyaml.loads(TEXT)
    assert config == {"name": "api", "replicas": 2}
    doc = oneyaml.parse(TEXT)
    doc["replicas"] = config["replicas"] + 1
    doc.patch([{"op": "add", "path": "/region", "value": "eu-west-1"}])
    assert str(doc) == (
        "# deploy settings\nname: api\nreplicas: 3  # raised by the release job\n"
        "region: eu-west-1\n"
    )


# ---- the six operations ----------------------------------------------------------------------

DOC = "# top\nname: api  # the service\nports: [80, 443]\nenv:\n  - A=1\n  - B=2\nmeta: {k: v}\n"

OPERATIONS = [
    ([{"op": "add", "path": "/zone", "value": "b"}], {"zone": "b"}),
    ([{"op": "add", "path": "/ports/1", "value": 8080}], {"ports": [80, 8080, 443]}),
    ([{"op": "add", "path": "/env/-", "value": "C=3"}], {"env": ["A=1", "B=2", "C=3"]}),
    ([{"op": "add", "path": "/name", "value": "web"}], {"name": "web"}),
    ([{"op": "remove", "path": "/meta"}], {"meta": ...}),
    ([{"op": "remove", "path": "/env/0"}], {"env": ["B=2"]}),
    ([{"op": "replace", "path": "/ports/0", "value": 8}], {"ports": [8, 443]}),
    ([{"op": "move", "from": "/name", "path": "/title"}], {"name": ..., "title": "api"}),
    ([{"op": "copy", "from": "/ports", "path": "/ports2"}], {"ports2": [80, 443]}),
    ([{"op": "test", "path": "/ports/0", "value": 80}], {}),
    ([{"op": "test", "path": "/meta", "value": {"k": "v"}}], {}),
]


@pytest.mark.parametrize(("ops", "changes"), OPERATIONS)
def test_an_operation_gives_the_document_a_fresh_parse_agrees_with(api, ops, changes):
    expected = oneyaml.loads(DOC)
    for key, value in changes.items():
        if value is ...:
            del expected[key]
        else:
            expected[key] = value
    doc = oneyaml.parse(DOC)
    doc.patch(ops)
    out = str(doc)
    assert oneyaml.loads(out) == expected
    assert oneyaml.parse(out).to_python() == expected
    if not changes:  # a test changes nothing
        assert out == DOC
    # The header comment and the line comment on a value the patch left alone survive.
    if "name" not in changes or changes["name"] is not ...:
        assert "# top\n" in out
    if ops[0]["op"] in ("add", "remove", "replace") and ops[0]["path"].startswith("/ports"):
        assert "name: api  # the service\n" in out


def test_edits_outside_the_changed_value_are_byte_identical(api):
    doc = oneyaml.parse(DOC)
    doc.patch([{"op": "replace", "path": "/ports/1", "value": 8443}])
    assert str(doc) == DOC.replace("[80, 443]", "[80, 8443]")


def test_operations_see_the_results_of_the_ones_before(api):
    doc = oneyaml.parse("a: 1\n")
    doc.patch(
        [
            {"op": "add", "path": "/l", "value": []},
            {"op": "add", "path": "/l/-", "value": 1},
            {"op": "copy", "from": "/l/0", "path": "/l/-"},
            {"op": "test", "path": "/l", "value": [1, 1]},
        ]
    )
    assert doc.to_python() == {"a": 1, "l": [1, 1]}


def test_the_whole_document_can_be_replaced(api):
    doc = oneyaml.parse("a: 1\n")
    doc.patch([{"op": "replace", "path": "", "value": {"b": 2}}])
    assert doc.to_python() == {"b": 2}


def test_a_patch_may_be_json_text(api):
    doc = oneyaml.parse("a: 1\n")
    doc.patch(json.dumps([{"op": "add", "path": "/b", "value": [1, 2.5, None, True]}]))
    assert doc.to_python() == {"a": 1, "b": [1, 2.5, None, True]}


def test_values_keep_their_types(api):
    doc = oneyaml.parse("a: 1\n")
    doc.patch(
        [
            {"op": "add", "path": "/i", "value": 7},
            {"op": "add", "path": "/f", "value": 7.0},
            {"op": "add", "path": "/s", "value": "7"},
            {"op": "add", "path": "/n", "value": None},
            {"op": "add", "path": "/t", "value": True},
        ]
    )
    got = doc.to_python()
    assert [type(got[k]) for k in "ifsnt"] == [int, float, str, type(None), bool]


def test_the_test_operation_compares_by_the_data_model(api):
    doc = oneyaml.parse("a: 0x10\nb: 1.0\nc: 'x'\n")
    doc.patch([{"op": "test", "path": "/a", "value": 16}])
    e = failing(doc, doc.patch, [{"op": "test", "path": "/b", "value": "1.0"}])
    assert e.category == "patch-test"
    assert e.index == 0


# ---- all or nothing, and what the error says -------------------------------------------------


def test_a_patch_that_fails_midway_changes_nothing(api):
    doc = oneyaml.parse(DOC)
    e = failing(
        doc,
        doc.patch,
        [
            {"op": "replace", "path": "/name", "value": "x"},
            {"op": "remove", "path": "/env/0"},
            {"op": "remove", "path": "/missing"},
        ],
    )
    assert (e.category, e.index) == ("patch-path", 2)
    assert isinstance(e, YAMLError)
    assert isinstance(e, ValueError)
    assert "missing" in str(e)


FAILURES = [
    ([{"op": "remove", "path": "/zz"}], "patch-path"),
    ([{"op": "add", "path": "/zz/yy", "value": 1}], "patch-path"),
    ([{"op": "replace", "path": "/ports/7", "value": 1}], "patch-path"),
    ([{"op": "test", "path": "/name", "value": "web"}], "patch-test"),
    ([{"op": "frob", "path": "/name"}], "patch-invalid"),
    ([{"op": "add", "path": "name", "value": 1}], "patch-invalid"),
    ([{"op": "add", "path": "/name"}], "patch-invalid"),
    ([{"op": "move", "from": "/env", "path": "/env/0/x"}], "patch-invalid"),
    ("[{", "patch-invalid"),
    ({"op": "remove", "path": "/name"}, "patch-invalid"),
]


@pytest.mark.parametrize(("ops", "category"), FAILURES)
def test_a_bad_patch_is_a_patch_error_with_its_category(api, ops, category):
    doc = oneyaml.parse(DOC)
    e = failing(doc, doc.patch, ops)
    assert e.category == category


def test_the_error_survives_pickling(api):
    doc = oneyaml.parse(DOC)
    e = failing(doc, doc.patch, [{"op": "remove", "path": "/zz"}])
    again = pickle.loads(pickle.dumps(e))
    assert (type(again), again.category, again.index) == (PatchError, "patch-path", 0)


ALIASED = "base: &b [1, 2]\nuse: *b\nother: &o {k: 1}\nmore: *o\n"


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add", "path": "/use/-", "value": 3},
        {"op": "replace", "path": "/use/0", "value": 9},
        {"op": "remove", "path": "/use/0"},
        {"op": "remove", "path": "/base"},
        {"op": "move", "from": "/other", "path": "/elsewhere"},
    ],
)
def test_nothing_is_written_through_an_alias_or_pulled_from_under_one(api, op):
    doc = oneyaml.parse(ALIASED)
    e = failing(doc, doc.patch, [op])
    assert e.category == "refused-alias"


def test_a_copy_out_of_an_alias_is_written_out_in_full(api):
    doc = oneyaml.parse(ALIASED)
    doc.patch([{"op": "copy", "from": "/use", "path": "/mine"}])
    assert doc.to_python()["mine"] == [1, 2]
    assert "mine:\n  - 1\n  - 2\n" in str(doc)


def test_two_keys_with_one_text_are_ambiguous(api):
    doc = oneyaml.parse('1: int\n"1": text\nother: 3\n')
    e = failing(doc, doc.patch, [{"op": "replace", "path": "/1", "value": 0}])
    assert e.category == "refused-ambiguous"
    assert doc.patch([{"op": "replace", "path": "/other", "value": 4}]) is None


def test_a_patch_that_copies_without_bound_meets_the_alias_budget(api):
    levels = ["l0: &l0 [a, b, c, d, e, f, g, h, i, j]"]
    for n in range(1, 9):
        levels.append(f"l{n}: &l{n} [" + ", ".join([f"*l{n - 1}"] * 10) + "]")
    doc = oneyaml.parse("\n".join(levels) + "\n")
    # Read as whole, the expansion is 10**9 nodes: refused. The copy writes it out, so the
    # same budget applies to it.
    with pytest.raises(YAMLError) as read:
        doc.to_python()
    assert read.value.category == "limit"
    e = failing(doc, doc.patch, [{"op": "copy", "from": "/l8", "path": "/copy"}])
    assert e.category == "limit"
    assert e.index == 0


# ---- RFC 7396 ---------------------------------------------------------------------------------


def test_the_merge_patch_example_of_the_rfc(api):
    doc = oneyaml.parse(
        "# a\ntitle: Goodbye!\nauthor:\n  givenName: John\n  familyName: Doe\n"
        "tags:\n  - example\n  - sample\ncontent: This will be unchanged\n"
    )
    doc.merge_patch(
        {
            "title": "Hello!",
            "phoneNumber": "+01-123-456-7890",
            "author": {"familyName": None},
            "tags": ["example"],
        }
    )
    assert doc.to_python() == {
        "title": "Hello!",
        "author": {"givenName": "John"},
        "tags": ["example"],
        "content": "This will be unchanged",
        "phoneNumber": "+01-123-456-7890",
    }
    assert str(doc).startswith("# a\ntitle: Hello!\n")
    assert "content: This will be unchanged\n" in str(doc)


def test_a_merge_patch_that_is_not_an_object_replaces_the_document(api):
    doc = oneyaml.parse("a: 1\n")
    doc.merge_patch([1, 2])
    assert doc.to_python() == [1, 2]
    doc.merge_patch("text")
    assert doc.to_python() == "text"


def test_deleting_what_is_not_there_is_not_an_error(api):
    doc = oneyaml.parse("a: 1\n")
    doc.merge_patch({"nothing": None})
    assert str(doc) == "a: 1\n"


def test_a_merge_patch_into_an_alias_is_refused_and_changes_nothing(api):
    doc = oneyaml.parse("a: &x {b: 1}\nc: *x\n")
    e = failing(doc, doc.merge_patch, {"c": {"z": 1}})
    assert e.category == "refused-alias"
    assert e.index is None


def test_a_merge_patch_value_that_cannot_be_yaml_is_a_type_error(api):
    doc = oneyaml.parse("a: 1\n")
    with pytest.raises(TypeError):
        doc.merge_patch({"a": object()})
    with pytest.raises(TypeError):
        doc.patch([{"op": "add", "path": "/b", "value": object()}])
    assert str(doc) == "a: 1\n"


# ---- documents of a stream --------------------------------------------------------------------

STREAM = "a: 1\n---\nb: 2  # two\n---\nc: 3\n"


def test_the_document_argument_picks_the_document(api):
    doc = oneyaml.parse(STREAM)
    doc.patch([{"op": "replace", "path": "/b", "value": 9}], document=1)
    assert str(doc) == "a: 1\n---\nb: 9  # two\n---\nc: 3\n"
    doc.merge_patch({"c": None, "d": 4}, document=2)
    assert doc.documents[2].to_python() == {"d": 4}
    assert doc.documents[0].to_python() == {"a": 1}


def test_a_handle_from_documents_patches_its_own_document(api):
    doc = oneyaml.parse(STREAM)
    doc.documents[1].patch([{"op": "add", "path": "/x", "value": 1}])
    assert doc.documents[1].to_python() == {"b": 2, "x": 1}
    assert doc.to_python() == {"a": 1}


def test_a_document_that_is_not_there_is_a_patch_path_error(api):
    doc = oneyaml.parse(STREAM)
    e = failing(doc, doc.patch, [{"op": "add", "path": "/x", "value": 1}], document=7)
    assert e.category == "patch-path"
    e = failing(doc, doc.merge_patch, {"x": 1}, document=2**80)
    assert e.category == "patch-path"


def test_bad_document_arguments(api):
    doc = oneyaml.parse(STREAM)
    with pytest.raises(TypeError):
        doc.patch([], document="1")
    with pytest.raises(TypeError):
        doc.patch([], document=True)
    with pytest.raises(PatchError) as e:
        doc.patch([], document=-1)
    assert e.value.category == "patch-path"


# ---- views after a patch ----------------------------------------------------------------------


def test_views_taken_before_a_patch_are_stale_and_the_document_is_not(api):
    doc = oneyaml.parse("a: {x: 1}\nl: [1, 2]\n")
    view = doc["a"]
    items = doc["l"]
    doc.patch([{"op": "replace", "path": "/a/x", "value": 5}])
    with pytest.raises(StaleViewError):
        view["x"]
    with pytest.raises(StaleViewError):
        len(items)
    assert doc["a"]["x"] == 5
    assert len(doc) == 2


def test_a_failed_patch_leaves_views_good(api):
    doc = oneyaml.parse("a: {x: 1}\n")
    view = doc["a"]
    failing(doc, doc.patch, [{"op": "remove", "path": "/a/x"}, {"op": "remove", "path": "/q"}])
    assert view["x"] == 1
    view["x"] = 2
    assert doc["a"]["x"] == 2


def test_a_patch_of_tests_only_leaves_views_good(api):
    doc = oneyaml.parse("a: {x: 1}\n")
    view = doc["a"]
    doc.patch([{"op": "test", "path": "/a/x", "value": 1}])
    assert view["x"] == 1


# ---- pointers ---------------------------------------------------------------------------------


def test_at_refuses_a_pointer_two_keys_match(api):
    doc = oneyaml.parse('1: int\n"1": text\n')
    with pytest.raises(PatchError) as e:
        doc.at("/1")
    assert e.value.category == "refused-ambiguous"
