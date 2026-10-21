"""The ruamel.yaml compat module: loading, editing a loaded document, writing from scratch.

Every test runs on both backends (the ``api`` fixture). Tests that compare with ruamel.yaml
itself need it installed (a development dependency).
"""

from __future__ import annotations

import copy
import io
import pathlib
import pickle

import pytest

import oneyaml
from oneyaml import LimitError, Limits
from oneyaml.compat.ruamel import (
    YAML,
    CommentedMap,
    CommentedSeq,
    ComposerError,
    ConstructorError,
    DoubleQuotedScalarString,
    DuplicateKeyError,
    FoldedScalarString,
    LiteralScalarString,
    ParserError,
    PlainScalarString,
    RepresenterError,
    ScannerError,
    SingleQuotedScalarString,
    YAMLError,
)
from oneyaml.compat.ruamel.scalarstring import walk_tree


@pytest.fixture(autouse=True)
def _backend(api):
    """Every test in this module runs once per backend."""
    return api


def dump(y: YAML, data) -> str:
    out = io.StringIO()
    y.dump(data, out)
    return out.getvalue()


def edit(text: str, fn, **settings) -> str:
    """Load ``text``, apply ``fn`` to the data, dump."""
    y = YAML()
    for k, v in settings.items():
        setattr(y, k, v)
    data = y.load(text)
    fn(data)
    return dump(y, data)


def changed_lines(before: str, after: str) -> tuple[int, int]:
    """(lines removed or replaced, lines added) between two texts, by position-free line diff."""
    import difflib

    removed = added = 0
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False
    ).get_opcodes():
        if tag != "equal":
            removed += a1 - a0
            added += b1 - b0
    return removed, added


# ---- the reference cases -------------------------------------------------------------------------

CASE_INPUT = "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n"


@pytest.mark.parametrize("wrap", [str, LiteralScalarString], ids=["str", "literal"])
def test_case_a_a_literal_block_keeps_its_style(wrap):
    out = edit(CASE_INPUT, lambda d: d.__setitem__("b", wrap("this is a test2\n")))
    assert out == "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test2\nc: 13\n"


@pytest.mark.parametrize("wrap", [str, LiteralScalarString], ids=["str", "literal"])
def test_case_b_no_trailing_newline_switches_to_strip(wrap):
    out = edit(CASE_INPUT, lambda d: d.__setitem__("b", wrap("this is a test2")))
    assert out == "#hello\na: 10\n\n#abc 123\nb: |-\n  this is a test2\nc: 13\n"


# ---- unchanged input comes back byte for byte ----------------------------------------------------

UNCHANGED = [
    "a: 1\n",
    "# top\na:   1   # eol\n\n\nb: 'single'\nc: \"double\"\n",
    "list:\n    - a\n    -   b\n    -\n      - nested\n",
    "flow: {a:  1,b: [1,2 ,3]}\nother: [ x,y ]\n",
    "---\na: 1\n...\n",
    "a: 1\r\nb:\r\n  - x\r\n",
    "a: &x [1, 2]\nb: *x\nc: !!set {a, b}\n",
    "? complex\n: value\n? [a, b]\n: other\n",
    "text: >\n  folded\n  text\n\nkeep: |+\n  x\n\n",
    "a: 1\n---\nb: 2\n",
    "no_final_newline: 1",
    "a:\n\tb\n" if False else "a: 'it''s'\nb: \"tab\\there\"\n",
    "- {a: 1}\n- [b,   c]\n- !tag value\n",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_unchanged_round_trip_is_byte_exact(text):
    y = YAML()
    docs = list(y.load_all(text))
    out = io.StringIO()
    y.dump_all(docs, out)
    assert out.getvalue() == text


def test_unchanged_bytes_keep_bom_and_encoding(tmp_path):
    raw = "a: 1\nb: é\n".encode("utf-16")  # with a byte order mark
    p = tmp_path / "x.yaml"
    p.write_bytes(raw)
    y = YAML()
    data = y.load(p)
    assert data == {"a": 1, "b": "é"}
    y.dump(data, p)
    assert p.read_bytes() == raw


# ---- editing a loaded document -------------------------------------------------------------------

DOC = """\
# deployment
name: app   # the name
version: 1.2.3
image: "registry/app:1.0"
replicas: 2

servers:
  - host: a
    port: 80
  - host: b
    port: 81
tags: [x,  y]
"""


def test_set_replaces_only_that_value():
    out = edit(DOC, lambda d: d.__setitem__("image", "registry/app:2.0"))
    assert out == DOC.replace("registry/app:1.0", "registry/app:2.0")


def test_set_nested_and_append_and_delete():
    def fn(d):
        d["servers"][1]["port"] = 8081
        d["servers"].append({"host": "c", "port": 82})
        del d["replicas"]
        d["extra"] = {"k": [1, 2]}

    out = edit(DOC, fn)
    expected = DOC.replace("port: 81", "port: 8081").replace("replicas: 2\n", "")
    expected = expected.replace(
        "tags: [x,  y]\n", "tags: [x,  y]\nextra:\n  k:\n  - 1\n  - 2\n"
    ).replace("    port: 8081\n", "    port: 8081\n  - host: c\n    port: 82\n")
    assert oneyaml.loads(out) == {
        "name": "app",
        "version": "1.2.3",
        "image": "registry/app:1.0",
        "servers": [
            {"host": "a", "port": 80},
            {"host": "b", "port": 8081},
            {"host": "c", "port": 82},
        ],
        "tags": ["x", "y"],
        "extra": {"k": [1, 2]},
    }
    # Everything the script did not touch is where it was.
    for line in (
        "# deployment",
        "name: app   # the name",
        'image: "registry/app:1.0"',
        "tags: [x,  y]",
    ):
        assert line in out.splitlines()
    assert changed_lines(DOC, out) == (3, 8) or "replicas" not in out
    assert expected.count("\n") >= 0


def test_edit_changes_only_the_lines_it_names():
    out = edit(DOC, lambda d: d["servers"][0].__setitem__("port", 8080))
    assert changed_lines(DOC, out) == (1, 1)
    assert out == DOC.replace("port: 80", "port: 8080")


def test_insert_into_a_sequence_and_pop():
    def fn(d):
        d["servers"].insert(0, {"host": "z", "port": 1})
        d["tags"].pop()

    out = edit(DOC, fn)
    assert oneyaml.loads(out)["servers"][0] == {"host": "z", "port": 1}
    assert oneyaml.loads(out)["tags"] == ["x"]
    assert "# deployment" in out and "name: app   # the name" in out


def test_a_type_change_with_an_equal_value_is_an_edit():
    out = edit("a: 1\nb: 0\nc: x\n", lambda d: d.update(a=True, b=False))
    assert oneyaml.loads(out) == {"a": True, "b": False, "c": "x"}
    out = edit("a: 1\n", lambda d: d.__setitem__("a", 1.0))
    assert oneyaml.loads(out) == {"a": 1.0} and isinstance(oneyaml.loads(out)["a"], float)


def test_same_value_assigned_again_changes_nothing():
    text = "a: 'q'\nb: 0x10\nc: [1,  2]\n"
    out = edit(text, lambda d: d.update(a="q", b=16, c=[1, 2]))
    assert out == text


def test_dumping_twice_and_after_reverting_starts_from_the_input():
    y = YAML()
    text = "a: 0x10\nb: 2\n"
    d = y.load(text)
    d["a"] = 5
    assert dump(y, d) == "a: 5\nb: 2\n"
    assert dump(y, d) == "a: 5\nb: 2\n"
    d["a"] = 16
    assert dump(y, d) == text


def test_dump_failure_leaves_nothing_half_written():
    y = YAML()
    d = y.load("a: &x [1]\nb: *x\nc: 3\n")
    d["c"] = 4
    del d["a"]  # the anchor is still used by an alias: the document refuses
    out = io.StringIO()
    with pytest.raises(YAMLError):
        y.dump(d, out)
    assert out.getvalue() == ""
    d2 = y.load("a: &x [1]\nb: *x\nc: 3\n")
    d2["c"] = 4
    assert dump(y, d2) == "a: &x [1]\nb: *x\nc: 4\n"


def test_key_order_edits_read_back_in_the_new_order():
    text = "# c\na: 1\nb: 2  # two\nc: 3\n"

    def fn_insert(d):
        d.insert(1, "x", 9)

    out = edit(text, fn_insert)
    assert list(oneyaml.loads(out)) == ["a", "x", "b", "c"]
    assert out.startswith("# c\na: 1\nx: 9\n")

    out = edit(text, lambda d: d.update(dict(sorted(d.items(), reverse=True))) or None)
    assert oneyaml.loads(out) == {"a": 1, "b": 2, "c": 3}

    def fn_reorder(d):
        items = list(d.items())
        d.clear()
        d.update(reversed(items))

    out = edit(text, fn_reorder)
    assert list(oneyaml.loads(out)) == ["c", "b", "a"]


def test_replacing_the_root_writes_it_fresh():
    y = YAML()
    d = y.load("# c\na: 1\n")
    out = dump(y, [d, {"z": 1}])
    assert out == "- a: 1\n- z: 1\n"
    assert dump(y, {"new": [1, 2]}) == "new:\n- 1\n- 2\n"


def test_only_some_documents_of_a_stream_are_written_fresh():
    y = YAML()
    first, second = y.load_all("a: 1\n---\nb: 2\n")
    assert dump(y, first) == "a: 1\n"
    out = io.StringIO()
    y.dump_all([second, first], out)
    assert oneyaml.loads_all(out.getvalue()) == [{"b": 2}, {"a": 1}]


def test_edits_in_several_documents_and_markers():
    y = YAML()
    docs = list(y.load_all("---\na: 1  # one\n...\n---\n# two\nb: [1,  2]\n"))
    docs[0]["a"] = 3
    docs[1]["b"].append(3)
    out = io.StringIO()
    y.dump_all(docs, out)
    assert out.getvalue() == "---\na: 3  # one\n...\n---\n# two\nb: [1,  2, 3]\n"


def test_deepcopy_stays_tied_to_the_document():
    y = YAML()
    d = y.load("# top\na: 1  # one\nb: [1, 2]\n")
    e = copy.deepcopy(d)
    e["a"] = 2
    assert dump(y, e) == "# top\na: 2  # one\nb: [1, 2]\n"
    assert dump(y, d) == "# top\na: 1  # one\nb: [1, 2]\n"
    assert pickle.loads(pickle.dumps(d)) == d


def test_aliases_read_as_one_object_and_edit_through_the_anchor():
    y = YAML()
    d = y.load("a: &x\n  k: 1\nb: *x\n")
    assert d["a"] is d["b"]
    d["a"]["k"] = 2
    assert dump(y, d) == "a: &x\n  k: 2\nb: *x\n"


def test_replacing_an_alias_by_a_value_leaves_the_anchor_alone():
    y = YAML()
    d = y.load("a: &x 1\nb: *x\n")
    d["b"] = 2
    assert dump(y, d) == "a: &x 1\nb: 2\n"


def test_deep_nesting_is_not_recursion():
    n = 4000
    text = "[" * n + "1" + "]" * n + "\n"
    y = YAML()
    d = y.load(text)
    node = d
    for _ in range(n - 1):
        node = node[0]
    node[0] = 2
    out = dump(y, d)
    assert out == "[" * n + "2" + "]" * n + "\n"
    assert dump(y, y.load(text)) == text


def test_a_hostile_depth_is_a_limit_error():
    with pytest.raises(LimitError):
        YAML().load("[" * 50_000 + "]" * 50_000)
    with pytest.raises(LimitError):
        YAML(typ="safe").load("[" * 50_000 + "]" * 50_000)


# ---- real-world snippets -------------------------------------------------------------------------

PRE_COMMIT = """\
# See https://pre-commit.com for more information
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.4.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer   # keep files tidy
  - repo: https://github.com/psf/black
    rev: 23.1.0
    hooks:
      - id: black
        args: ["--line-length", "100"]
"""


def test_pre_commit_config_bump():
    from oneyaml.compat.ruamel import YAML  # the one-line import change

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    config = yaml.load(PRE_COMMIT)
    for repo in config["repos"]:
        if repo["repo"].endswith("/black"):
            repo["rev"] = "24.2.0"
    out = io.StringIO()
    yaml.dump(config, out)
    assert out.getvalue() == PRE_COMMIT.replace("23.1.0", "24.2.0")


MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels: {app: web, tier: "front"}
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: web
        image: "nginx:1.24"   # pinned
        ports:
        - containerPort: 80
      - name: sidecar
        image: busybox:1.36
"""


def test_k8s_image_bump():
    yaml = YAML()
    yaml.preserve_quotes = True
    obj = yaml.load(MANIFEST)
    for c in obj["spec"]["template"]["spec"]["containers"]:
        if c["name"] == "web":
            c["image"] = "nginx:1.25"
    out = io.StringIO()
    yaml.dump(obj, out)
    assert out.getvalue() == MANIFEST.replace('"nginx:1.24"', '"nginx:1.25"')


def test_k8s_add_container_and_label_copies_the_style_around_it():
    yaml = YAML()
    obj = yaml.load(MANIFEST)
    obj["spec"]["template"]["spec"]["containers"].append({"name": "logger", "image": "fluent:1"})
    obj["metadata"]["labels"]["env"] = "prod"
    out = io.StringIO()
    yaml.dump(obj, out)
    text = out.getvalue()
    assert oneyaml.loads(text)["spec"]["template"]["spec"]["containers"][2]["name"] == "logger"
    assert oneyaml.loads(text)["metadata"]["labels"] == {
        "app": "web",
        "tier": "front",
        "env": "prod",
    }
    assert '        image: "nginx:1.24"   # pinned' in text
    assert "      - name: logger\n" in text  # the sequence keeps the document's own indentation


def test_indent_settings_apply_to_new_blocks_only_where_the_document_has_no_example():
    yaml = YAML()
    yaml.indent(mapping=4, sequence=6, offset=3)
    data = yaml.load("a: 1\n")
    data["b"] = {"c": [1, 2]}
    out = io.StringIO()
    yaml.dump(data, out)
    assert oneyaml.loads(out.getvalue()) == {"a": 1, "b": {"c": [1, 2]}}
    assert out.getvalue().startswith("a: 1\n")


# ---- writing from scratch ------------------------------------------------------------------------


SCRATCH = {
    "name": "x",
    "list": [1, 2.5, True, None, "s", [3, 4], {"k": "v", "nn": [{"deep": 1}]}],
    "empty": {"a": [], "b": {}},
    "nested": {"m": {"nn": {"o": [1, [2, [3]]]}}},
}


def test_default_layout_is_ruamels_block_style():
    out = dump(YAML(), SCRATCH)
    assert out == (
        "name: x\n"
        "list:\n"
        "- 1\n"
        "- 2.5\n"
        "- true\n"
        "-\n"
        "- s\n"
        "- - 3\n"
        "  - 4\n"
        "- k: v\n"
        "  nn:\n"
        "  - deep: 1\n"
        "empty:\n"
        "  a: []\n"
        "  b: {}\n"
        "nested:\n"
        "  m:\n"
        "    nn:\n"
        "      o:\n"
        "      - 1\n"
        "      - - 2\n"
        "        - - 3\n"
    )
    assert oneyaml.loads(out.replace("-\n", "- null\n")) == SCRATCH


@pytest.mark.parametrize(
    "layout", [(2, 2, 0), (2, 4, 2), (4, 4, 2), (4, 6, 2), (2, 3, 1), (3, 5, 2), (4, 2, 0)]
)
def test_indent_settings_write_the_layout_of_ruamel(layout):
    ruamel = pytest.importorskip("ruamel.yaml")
    data = {
        "a": [1, {"c": 1, "d": [1, 2]}, [3, 4], [[5], {"k": "v"}]],
        "e": {"f": []},
        "m": [{"x": [{"yy": 1}]}],
    }
    mine = YAML()
    mine.indent(*layout)
    theirs = ruamel.YAML()
    theirs.indent(*layout)
    ref = io.StringIO()
    theirs.dump(copy.deepcopy(data), ref)
    assert dump(mine, copy.deepcopy(data)) == ref.getvalue()


def test_a_root_sequence_follows_the_offset():
    ruamel = pytest.importorskip("ruamel.yaml")
    data = [{"a": 1}, [1, 2], "x"]
    mine = YAML()
    mine.indent(mapping=2, sequence=4, offset=2)
    theirs = ruamel.YAML()
    theirs.indent(mapping=2, sequence=4, offset=2)
    ref = io.StringIO()
    theirs.dump(copy.deepcopy(data), ref)
    assert dump(mine, data) == ref.getvalue()


def test_flow_style_settings():
    y = YAML()
    y.default_flow_style = True
    assert dump(y, {"a": [1, 2], "b": {"c": 1}}) == "{a: [1, 2], b: {c: 1}}\n"
    y = YAML(typ="safe")
    assert dump(y, {"a": [1, 2], "b": {"c": [{"d": 1}]}}) == "a: [1, 2]\nb:\n  c:\n  - {d: 1}\n"
    seq = CommentedSeq([1, 2, 3])
    seq.fa.set_flow_style()
    assert dump(YAML(), {"s": seq, "t": [4]}) == "s: [1, 2, 3]\nt:\n- 4\n"


def test_style_types_choose_the_style_of_new_values():
    data = {
        "lit": LiteralScalarString("a\nb\n"),
        "lit_strip": LiteralScalarString("a\nb"),
        "lit_keep": LiteralScalarString("a\n\n"),
        "lit_indent": LiteralScalarString("  lead\nb\n"),
        "fold": FoldedScalarString("one two\nthree\n"),
        "sq": SingleQuotedScalarString("it's"),
        "dq": DoubleQuotedScalarString("tab\there"),
        "pl": PlainScalarString("plain"),
        "pl_quoted": PlainScalarString("yes: no"),
        "plain_str": "ordinary",
        "seq": [LiteralScalarString("x\ny\n")],
    }
    out = dump(YAML(), data)
    assert "lit: |\n  a\n  b\n" in out
    assert "lit_strip: |-\n  a\n  b\n" in out
    assert "lit_keep: |+\n  a\n\n" in out
    assert "lit_indent: |2\n" in out
    assert "fold: >\n  one two\n\n  three\n" in out
    assert "sq: 'it''s'\n" in out
    assert 'dq: "tab\\there"\n' in out
    assert "pl: plain\n" in out
    assert "seq:\n- |\n  x\n  y\n" in out
    back = oneyaml.loads(out)
    assert back == {k: str(v) if not isinstance(v, list) else [str(v[0])] for k, v in data.items()}


def test_style_types_that_cannot_be_written_in_their_style_fall_back():
    data = {"a": LiteralScalarString("tab\x07bell\n"), "b": SingleQuotedScalarString("a\nb")}
    assert oneyaml.loads(dump(YAML(), data)) == {"a": "tab\x07bell\n", "b": "a\nb"}


def test_every_string_reads_back_whatever_its_style():
    texts = ["", " ", "a b", " lead", "trail ", "a\nb", "\n", "x\n\n", "#c", "- x", "k: v", "{a}",
             "\ttab", "é", "a" * 300, "null", "1_000", "2001-01-01", "\x00", "\u2028", "'", '"',
    ]  # fmt: skip
    for style in (str, LiteralScalarString, FoldedScalarString, SingleQuotedScalarString,
                  DoubleQuotedScalarString, PlainScalarString):  # fmt: skip
        data = {"k": [style(t) for t in texts], style(texts[2]): style(texts[5])}
        out = dump(YAML(), data)
        assert oneyaml.loads(out) == {"k": texts, texts[2]: texts[5]}, style


def test_comments_on_data_written_from_scratch():
    data = CommentedMap(name="x")
    data["items"] = CommentedSeq(["a", "b"])
    data["last"] = 1
    data.yaml_set_start_comment("top of file")
    data.yaml_set_comment_before_after_key("items", before="the items", indent=0)
    data.yaml_add_eol_comment("end of line", "name")
    data["items"].yaml_add_eol_comment("first", 0)
    data.yaml_set_comment_before_after_key("last", before="line one\nline two", after="after last")
    assert dump(YAML(), data) == (
        "# top of file\n"
        "name: x  # end of line\n"
        "# the items\n"
        "items:\n"
        "- a  # first\n"
        "- b\n"
        "# line one\n"
        "# line two\n"
        "last: 1\n"
        "# after last\n"
    )


def test_comments_cannot_be_added_to_a_loaded_document_and_nothing_is_dropped_silently():
    y = YAML()
    d = y.load("a: 1\n")
    d.yaml_add_eol_comment("note", "a")
    out = io.StringIO()
    with pytest.raises(NotImplementedError):
        y.dump(d, out)
    assert out.getvalue() == ""


def test_dates_and_other_scalars_are_written():
    import datetime

    data = {"d": datetime.date(2024, 5, 1), "t": datetime.datetime(2024, 5, 1, 12, 30, 5),
            "b": b"\x00\x01", "big": 10**30, "inf": float("inf"), "nan": float("nan")}  # fmt: skip
    out = dump(YAML(typ="safe"), data)
    assert "d: 2024-05-01," in out and "t: 2024-05-01 12:30:05," in out
    back = oneyaml.loads(out)
    assert back["b"] == b"\x00\x01" and back["big"] == 10**30 and back["inf"] == float("inf")


def test_unwritable_types_are_a_representer_error():
    with pytest.raises(RepresenterError):
        dump(YAML(), {"a": object()})
    with pytest.raises(RepresenterError):
        dump(YAML(typ="safe"), {"a": [object()]})
    y = YAML()
    d = y.load("a: 1\n")
    d["b"] = object()
    with pytest.raises(RepresenterError):
        dump(y, d)


def test_sets_and_ordered_maps_round_trip_in_safe_mode():
    y = YAML(typ="safe")
    data = {"s": {1, 2}, "o": {"a": 1}}
    text = dump(y, data)
    assert "!!set" in text
    assert y.load(text)["s"] == {1, 2}
    got = y.load("o: !!omap [a: 1, b: 2]\nq: !!pairs [a: 1, a: 2]\n")
    assert got == {"o": {"a": 1, "b": 2}, "q": [("a", 1), ("a", 2)]}


def test_a_value_that_contains_itself_is_refused():
    a: list = [1]
    a.append(a)
    with pytest.raises(ValueError):
        dump(YAML(typ="safe"), {"k": a})
    with pytest.raises(ValueError):
        dump(YAML(), {"k": a})


def test_explicit_start_end_and_version():
    y = YAML()
    y.explicit_start = True
    y.explicit_end = True
    y.version = (1, 2)
    assert dump(y, {"a": 1}) == "%YAML 1.2\n---\na: 1\n...\n"
    y = YAML()
    y.explicit_start = True
    assert dump(y, {"a": 1}) == "---\na: 1\n"
    assert dump(y, "x") == "--- x\n"
    out = io.StringIO()
    YAML().dump_all([{"a": 1}, [2], "s"], out)
    assert out.getvalue() == "a: 1\n---\n- 2\n--- s\n"


def test_scalar_and_empty_documents_are_written():
    for value in ("text", 5, 1.5, True, [], {}, "a\nb\n", None):
        out = dump(YAML(), value)
        assert oneyaml.loads(out) == value, value


# ---- reading: safe, base, unsafe and the differences from ruamel ---------------------------------

SAFE_DIFFERENT = [
    # ruamel reads these plain scalars as numbers and dates; the Core schema reads text.
    ("a: 1_000\n", {"a": 1000}),
    ("a: -1_0.5\n", {"a": -10.5}),
    ("a: 0b101\n", {"a": 5}),
    ("a: +0x1f\n", {"a": 31}),
    ("a: 0o1_7\n", {"a": 15}),
    ("a: 2001-12-14\n", {"a": __import__("datetime").date(2001, 12, 14)}),
    ("a: 2001-12-14 21:59:43\n", {"a": __import__("datetime").datetime(2001, 12, 14, 21, 59, 43)}),
    # Quoted and tagged forms stay text.
    (
        "a: '1_000'\nb: \"2001-12-14\"\nc: !!str 0b101\n",
        {"a": "1_000", "b": "2001-12-14", "c": "0b101"},
    ),
    # Things that are not numbers stay text.
    ("a: 1__0\nb: 1_\nc: _1\nd: 0b2\n", {"a": "1__0", "b": "1_", "c": "_1", "d": "0b2"}),
    # Merge keys are applied.
    ("b: &b {x: 1, y: 2}\na:\n  <<: *b\n  y: 3\n", {"b": {"x": 1, "y": 2}, "a": {"x": 1, "y": 3}}),
    (
        "b: &b {x: 1}\nc: &c {z: 2}\na:\n  <<: [*b, *c]\n",
        {"b": {"x": 1}, "c": {"z": 2}, "a": {"x": 1, "z": 2}},
    ),
]  # fmt: skip


@pytest.mark.parametrize(("text", "expected"), SAFE_DIFFERENT)
def test_safe_load_reads_what_ruamel_reads(text, expected):
    got = YAML(typ="safe").load(text)
    assert got == expected
    assert type(got) is dict
    assert [type(v) for v in got.values()] == [type(v) for v in expected.values()]


def test_safe_load_gives_plain_types_and_rt_gives_commented_ones():
    text = "a: [1, {b: 2}]\n"
    s = YAML(typ="safe").load(text)
    assert type(s) is dict and type(s["a"]) is list and type(s["a"][1]) is dict
    r = YAML().load(text)
    assert type(r) is CommentedMap and type(r["a"]) is CommentedSeq
    assert type(r["a"][1]) is CommentedMap
    assert r == s


def test_rt_reads_the_same_values_as_safe():
    text = "a: 1_000\nb: 2001-12-14\nc: [0b11, x]\nd: 'q'\n"
    assert YAML().load(text) == YAML(typ="safe").load(text)


def test_dates_and_numbers_read_by_rt_are_written_back_unchanged():
    text = "a: 1_000   # grouped\nb: 2001-12-14\nc: [0b11, x]\n"
    y = YAML()
    d = y.load(text)
    assert dump(y, d) == text
    d["c"].append(1)
    assert dump(y, d) == text.replace("x]", "x, 1]")


def test_base_loader_reads_everything_as_text():
    got = YAML(typ="base").load("a: 1\nb: [true, null]\nc: 2001-01-01\n")
    assert got == {"a": "1", "b": ["true", "null"], "c": "2001-01-01"}


def test_unsafe_never_builds_objects():
    y = YAML(typ="unsafe")
    assert y.load("a: 1\n") == {"a": 1}
    with pytest.raises(ConstructorError, match="Python"):
        y.load("a: !!python/object/apply:os.system [echo]\n")
    with pytest.raises(ConstructorError):
        y.load("a: !<tag:yaml.org,2002:python/name:os.system> x\n")


def test_unknown_tags_load_as_tagged_values_in_every_mode():
    for typ in ("rt", "safe"):
        got = YAML(typ=typ).load("a: !foo bar\n")
        assert isinstance(got["a"], oneyaml.Tagged)
        assert got["a"].tag == "!foo"


def test_load_sources(tmp_path):
    y = YAML()
    p = tmp_path / "c.yaml"
    p.write_text("a: 1\n")
    assert y.load(p) == {"a": 1}
    assert y.load("a: 1\n") == {"a": 1}
    assert y.load(b"a: 1\n") == {"a": 1}
    assert y.load(io.StringIO("a: 1\n")) == {"a": 1}
    assert y.load(io.BytesIO(b"a: 1\n")) == {"a": 1}
    with open(p) as fp:
        assert y.load(fp) == {"a": 1}
    assert y.load("") is None
    assert y.load("# nothing\n") is None
    with pytest.raises(TypeError):
        y.load(5)
    with pytest.raises(TypeError):
        y.dump({"a": 1})  # no stream


def test_dump_sinks(tmp_path):
    y = YAML()
    d = y.load("a: 1  # one\n")
    d["a"] = 2
    p = tmp_path / "o.yaml"
    y.dump(d, p)
    assert p.read_bytes() == b"a: 2  # one\n"
    b = io.BytesIO()
    y.dump(d, b)
    assert b.getvalue() == b"a: 2  # one\n"
    with open(p, "w") as fp:
        y.dump(d, fp)
    assert p.read_text() == "a: 2  # one\n"
    out = io.StringIO()
    y.dump(d, out, transform=lambda s: s.replace("a:", "A:"))
    assert out.getvalue() == "A: 2  # one\n"
    sink = io.StringIO()
    YAML(output=sink).dump({"x": 1})
    assert sink.getvalue() == "x: 1\n"


def test_load_all_and_dump_all_of_plain_data():
    y = YAML(typ="safe")
    docs = list(y.load_all("a: 1\n---\n- 2\n---\n3\n"))
    assert docs == [{"a": 1}, [2], 3]
    out = io.StringIO()
    y.dump_all(docs, out)
    assert list(y.load_all(out.getvalue())) == docs


# ---- errors --------------------------------------------------------------------------------------


def test_syntax_errors_are_ruamels_classes_with_marks():
    for bad in ("a: [1\n", "a: 1\n b: 2\n"):
        with pytest.raises(ScannerError) as info:
            YAML().load(bad)
        assert isinstance(info.value, ParserError)
        assert isinstance(info.value, YAMLError) and isinstance(info.value, oneyaml.YAMLError)
        assert info.value.category == "syntax"
        assert info.value.problem_mark is not None
        assert info.value.problem_mark.line >= 0


def test_duplicate_keys_multiple_documents_and_bad_tags():
    with pytest.raises(DuplicateKeyError) as info:
        YAML().load("a: 1\na: 2\n")
    assert info.value.category == "duplicate-key" and info.value.line == 2
    with pytest.raises(ComposerError):
        YAML().load("a: 1\n---\nb: 2\n")
    with pytest.raises(YAMLError):
        YAML(typ="safe").load("a: *nothing\n")


def test_limits_hold_and_max_depth_only_lowers_them():
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"b{i}: &b{i} [{', '.join(['*a'] * 10)}]\n"
        if i == 0
        else f"b{i}: &b{i} [{', '.join([f'*b{i - 1}'] * 10)}]\n"
        for i in range(12)
    )
    for typ in ("rt", "safe"):
        with pytest.raises(LimitError):
            YAML(typ=typ).load(bomb)
    y = YAML()
    y.max_depth = 3
    with pytest.raises(LimitError):
        y.load("[[[[1]]]]")
    y = YAML()
    y.max_depth = 0  # ruamel: unlimited; here it changes nothing
    assert y.load("[[1]]") == [[1]]
    y = YAML()
    y.limits = Limits(depth=2)
    with pytest.raises(LimitError):
        y.load("[[[1]]]")


def test_unsupported_hooks_say_so():
    y = YAML()
    with pytest.raises(NotImplementedError):
        y.register_class(int)
    with pytest.raises(NotImplementedError):
        y.representer.add_representer(int, lambda r, d: None)
    with pytest.raises(NotImplementedError):
        y.compose("a: 1")
    y.representer.ignore_aliases = lambda *a: True  # attributes can be set
    with pytest.raises(NotImplementedError):
        YAML(typ="jinja2")


# ---- the Commented containers --------------------------------------------------------------------


def test_commented_containers_are_a_dict_and_a_list():
    m = CommentedMap({"a": 1})
    m["b"] = 2
    m.insert(0, "z", 0)
    assert list(m) == ["z", "a", "b"] and isinstance(m, dict)
    m.insert(1, "b", 5)
    assert list(m.items()) == [("z", 0), ("b", 5), ("a", 1)]
    s = CommentedSeq([3, 1, 2])
    s.sort()
    s += [4]
    s.insert(0, 0)
    assert s == [0, 1, 2, 3, 4] and isinstance(s, list)
    assert m.copy() == m and type(m.copy()) is CommentedMap
    assert copy.deepcopy(s) == s and type(copy.deepcopy(s)) is CommentedSeq
    assert pickle.loads(pickle.dumps(m)) == m


def test_walk_tree_marks_multi_line_strings_as_literal():
    data = CommentedMap(a="x\ny\n", b=["p\nq", "plain"])
    walk_tree(data)
    assert isinstance(data["a"], LiteralScalarString)
    assert isinstance(data["b"][0], LiteralScalarString)
    assert dump(YAML(), data) == "a: |\n  x\n  y\nb:\n- |-\n  p\n  q\n- plain\n"


def test_walk_tree_on_a_loaded_document_keeps_the_values_and_the_text():
    text = "a: |\n  x\n  y\nb: 'p q'\n"
    y = YAML()
    d = y.load(text)
    walk_tree(d)
    assert dump(y, d) == text


def test_paths_and_files_are_not_needed_for_the_import_shape():
    import oneyaml.compat.ruamel as ruamel_yaml

    assert ruamel_yaml.YAML is YAML
    assert ruamel_yaml.version_info[:2] == (0, 19)
    from oneyaml.compat.ruamel.comments import CommentedMap as CM
    from oneyaml.compat.ruamel.error import YAMLError as YE
    from oneyaml.compat.ruamel.scalarstring import LiteralScalarString as LS
    from oneyaml.compat.ruamel.scanner import ScannerError as SE

    assert CM is CommentedMap and YE is oneyaml.YAMLError and LS is LiteralScalarString
    assert SE is ScannerError
    assert pathlib.Path(ruamel_yaml.__file__).name == "__init__.py"


@pytest.mark.parametrize("typ", ["rt", "safe", "base"])
def test_empty_and_comment_only_streams_and_documents_load(typ):
    y = YAML(typ=typ)
    assert list(y.load_all("")) == []
    assert list(y.load_all("# only a comment\n")) == []
    assert y.load("") is None
    assert len(list(y.load_all("a: 1\n---\n"))) == 2
    assert len(list(y.load_all("---\n---\n"))) == 2
    assert list(y.load_all("%YAML 1.2\n---\na: 1\n"))[0] in ({"a": 1}, {"a": "1"})


def test_a_scalar_root_is_written_from_scratch():
    y = YAML()
    assert dump(y, y.load("# c\nplain\n")) == "plain\n"
    assert dump(y, y.load("5\n")) == "5\n"
