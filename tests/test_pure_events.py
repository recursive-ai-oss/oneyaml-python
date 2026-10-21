"""Event stream tests: every backend that can produce events, plus the pure parser's details."""

from __future__ import annotations

import importlib
import random
import re

import pytest

from conftest import BACKENDS
from oneyaml import _pure
from oneyaml._errors import YAMLError
from oneyaml._pure import _parser as parser
from oneyaml._pure._decode import decode


def _event_backends():
    mods = [_pure]
    if "native" in BACKENDS:
        try:
            native = importlib.import_module("oneyaml._native")
        except ImportError:
            native = None
        if native is not None and hasattr(native, "events_text"):
            mods.append(native)
    return mods


@pytest.fixture(params=_event_backends(), ids=lambda m: m.BACKEND)
def ev(request):
    """``events_text`` of each backend that has one."""
    return request.param.events_text


HEAD = ["+STR", "+DOC"]
TAIL = ["-DOC", "-STR"]


def doc(*lines, explicit=False):
    return ["+STR", "+DOC ---" if explicit else "+DOC", *lines, "-DOC", "-STR"]


# ---- accepted input -------------------------------------------------------------------------

GOOD = [
    ("", ["+STR", "-STR"]),
    ("# only a comment\n", ["+STR", "-STR"]),
    (
        "a: 1\nb: [x, {y: z}]\n",
        doc(
            "+MAP",
            "=VAL :a",
            "=VAL :1",
            "=VAL :b",
            "+SEQ []",
            "=VAL :x",
            "+MAP {}",
            "=VAL :y",
            "=VAL :z",
            "-MAP",
            "-SEQ",
            "-MAP",
        ),
    ),
    ("? a\n: b\n", doc("+MAP", "=VAL :a", "=VAL :b", "-MAP")),
    (
        "- &x !!str foo\n- *x\n",
        doc("+SEQ", "=VAL &x <tag:yaml.org,2002:str> :foo", "=ALI *x", "-SEQ"),
    ),
    ("--- !<tag:x,2000:t> a\n", doc("=VAL <tag:x,2000:t> :a", explicit=True)),
    ("--- !local a\n", doc("=VAL <!local> :a", explicit=True)),
    ("--- ! a\n", doc("=VAL <!> :a", explicit=True)),
    (
        "%TAG !e! tag:e.com,2000:\n--- !e!t%21 a\n...\n--- |-\n  lit\n",
        [
            "+STR",
            "+DOC ---",
            "=VAL <tag:e.com,2000:t!> :a",
            "-DOC ...",
            "+DOC ---",
            "=VAL |lit",
            "-DOC",
            "-STR",
        ],
    ),
    # A %TAG applies to its own document only.
    (
        "%TAG !e! tag:one:\n--- !e!a x\n...\n%TAG !e! tag:two:\n--- !e!a y\n",
        [
            "+STR",
            "+DOC ---",
            "=VAL <tag:one:a> :x",
            "-DOC ...",
            "+DOC ---",
            "=VAL <tag:two:a> :y",
            "-DOC",
            "-STR",
        ],
    ),
    ("k: >\n a\n b\n\n c\n", doc("+MAP", "=VAL :k", "=VAL >a b\\nc\\n", "-MAP")),
    ("- |+\n  a\n\n- b", doc("+SEQ", "=VAL |a\\n\\n", "=VAL :b", "-SEQ")),
    ("- |-\n  a\n- >\n  b\n", doc("+SEQ", "=VAL |a", "=VAL >b\\n", "-SEQ")),
    ("a: |\n  x\n   ", doc("+MAP", "=VAL :a", "=VAL |x\\n \\n", "-MAP")),
    ("--- |1\n foo\n", doc("=VAL | foo\\n", explicit=True)),
    (
        "[a: b, ? c, d]",
        doc(
            "+SEQ []",
            "+MAP {}",
            "=VAL :a",
            "=VAL :b",
            "-MAP",
            "+MAP {}",
            "=VAL :c",
            "=VAL :",
            "-MAP",
            "=VAL :d",
            "-SEQ",
        ),
    ),
    ('{"a":1, b}', doc("+MAP {}", '=VAL "a', "=VAL :1", "=VAL :b", "=VAL :", "-MAP")),
    (
        "{a: , : b, ? c}",
        doc("+MAP {}", "=VAL :a", "=VAL :", "=VAL :", "=VAL :b", "=VAL :c", "=VAL :", "-MAP"),
    ),
    ('a: "x\\u00e9\\\n   y"\n', doc("+MAP", "=VAL :a", '=VAL "xéy', "-MAP")),
    ('"\\ud83d\\ude00 \\x41 \\U0001F600 \\N\\_"\n', doc('=VAL "😀 A 😀 \x85\xa0')),
    ("'it''s'\n", doc("=VAL 'it's")),
    ("'a'''''\n", doc("=VAL 'a''")),
    ("a\n b\n\n c\n", doc("=VAL :a b\\nc")),
    ("a: b\r\n  c\r\nd: e\r\n", doc("+MAP", "=VAL :a", "=VAL :b c", "=VAL :d", "=VAL :e", "-MAP")),
    (
        "a:\n- b\n- c\nd: e\n",
        doc("+MAP", "=VAL :a", "+SEQ", "=VAL :b", "=VAL :c", "-SEQ", "=VAL :d", "=VAL :e", "-MAP"),
    ),
    (
        "- - a\n  - b\n- c: d\n  e: f\n",
        doc(
            "+SEQ",
            "+SEQ",
            "=VAL :a",
            "=VAL :b",
            "-SEQ",
            "+MAP",
            "=VAL :c",
            "=VAL :d",
            "=VAL :e",
            "=VAL :f",
            "-MAP",
            "-SEQ",
        ),
    ),
    ("&m\na: &k b\n", doc("+MAP &m", "=VAL :a", "=VAL &k :b", "-MAP")),
    ("&a [b]: c\n", doc("+MAP", "+SEQ [] &a", "=VAL :b", "-SEQ", "=VAL :c", "-MAP")),
    ("a: b # c\n# d\ne: f", doc("+MAP", "=VAL :a", "=VAL :b", "=VAL :e", "=VAL :f", "-MAP")),
    (
        "a: 1\n? b\n&k : 3\n",
        doc("+MAP", "=VAL :a", "=VAL :1", "=VAL :b", "=VAL :", "=VAL &k :", "=VAL :3", "-MAP"),
    ),
    (
        "---\n...\n---\na\n",
        ["+STR", "+DOC ---", "=VAL :", "-DOC ...", "+DOC ---", "=VAL :a", "-DOC", "-STR"],
    ),
    (
        "a: 1\n--- b\n",
        [
            "+STR",
            "+DOC",
            "+MAP",
            "=VAL :a",
            "=VAL :1",
            "-MAP",
            "-DOC",
            "+DOC ---",
            "=VAL :b",
            "-DOC",
            "-STR",
        ],
    ),
    ("%FOO bar baz\n--- a\n", doc("=VAL :a", explicit=True)),
    (
        "\ufeffa\n...\n\ufeff--- b\n",
        ["+STR", "+DOC", "=VAL :a", "-DOC ...", "+DOC ---", "=VAL :b", "-DOC", "-STR"],
    ),
    ("a: &x 1\nb: *x\n", doc("+MAP", "=VAL :a", "=VAL &x :1", "=VAL :b", "=ALI *x", "-MAP")),
    (
        "a: :b\nc: -d\ne: ?f\n",
        doc("+MAP", "=VAL :a", "=VAL ::b", "=VAL :c", "=VAL :-d", "=VAL :e", "=VAL :?f", "-MAP"),
    ),
    (
        "url: http://x.org/a#b\nh: a#b\n",
        doc("+MAP", "=VAL :url", "=VAL :http://x.org/a#b", "=VAL :h", "=VAL :a#b", "-MAP"),
    ),
]


@pytest.mark.parametrize(("text", "expected"), GOOD, ids=[repr(t[0])[:40] for t in GOOD])
def test_events(ev, text, expected):
    assert ev(text) == expected
    assert ev(text.encode("utf-8")) == expected


# ---- rejected input: category and position ----------------------------------------------------

BAD = [
    ("]\n", "syntax", 1, 1),
    ("a: @b\n", "syntax", 1, 4),
    ("a: ,b\n", "syntax", 1, 4),
    ("[a,, b]\n", "syntax", 1, 4),
    ("{a: 1}}\n", "syntax", 1, 7),
    ("[a, b\n", "syntax", 2, 1),
    ("{a: 1", "syntax", 1, 6),
    ('a: "abc\n', "syntax", 2, 1),
    ("a: 'abc", "syntax", 1, 8),
    ("'a''b", "syntax", 1, 6),
    ("a: b: c\n", "syntax", 1, 6),
    ("a: b\n c: d\n", "syntax", 2, 4),
    ("a: b\r\n c: d\r\n", "syntax", 2, 4),
    ("a: b\r c: d\r", "syntax", 2, 4),
    ("a: 1\nb\nc: 2\n", "syntax", 2, 2),
    ("a: 1\n- b\n", "syntax", 2, 2),
    ("- a\nb: 1\n", "syntax", 2, 1),
    ('a: "x\\qy"\n', "syntax", 1, 7),
    ('a: "\\xZZ"\n', "syntax", 1, 7),
    ('a: "\\u12"\n', "syntax", 1, 9),
    ("a: |0\n x\n", "syntax", 1, 5),
    ("&a[b]\n", "syntax", 1, 3),
    ("{a:{}}\n", "syntax", 1, 4),
    ("{:{0}}\n", "syntax", 1, 3),
    ("[\U0001f600]]\n", "syntax", 1, 4),
    ("k: [a\n\tb]\n", "syntax", 2, 2),
    ("k: a\n\tb\n", "syntax", 2, 2),
    ("!a%2 x\n", "syntax", 1, 5),
    ("- !<!> foo\n", "syntax", 1, 6),
    ("a: *y\n", "syntax", None, None),
    ("a: &x 1\nb: &x 2\nc: *z\n", "syntax", None, None),
    ('\ufeffa: "x"y\n', "syntax", 1, 7),
    ("a\ufeffb: c\n", "syntax", 1, 2),
    ("a: 1\n\ufeffb: 2\n", "syntax", 2, 1),
    ("a: \x01\n", "syntax", 1, 4),
    ("%YAML 1.2\n...\n", "syntax", 2, 1),
    ("%YAML 1.2\n%YAML 1.2\n---\n", "syntax", 2, 1),
    ("a: &x &y b\n", "syntax", 1, 7),
    ("[a: b: c]\n", "syntax", None, None),
    ("a: 1\n!\n", "syntax", None, None),
    ("ke: &x\n!!map\n ? a: b{\n", "syntax", None, None),
]


@pytest.mark.parametrize(
    ("text", "category", "line", "column"), BAD, ids=[repr(t[0])[:40] for t in BAD]
)
def test_errors(ev, text, category, line, column):
    with pytest.raises(YAMLError) as info:
        ev(text)
    err = info.value
    assert err.category == category
    assert isinstance(err.events, list)
    assert isinstance(err.message, str)
    if line is not None:
        assert (err.line, err.column) == (line, column)


def test_events_before_the_error(ev):
    with pytest.raises(YAMLError) as info:
        ev("a: [x, y\nz: 1\n")
    assert info.value.events == ["+STR", "+DOC", "+MAP", "=VAL :a", "+SEQ []", "=VAL :x", "=VAL :y"]
    # A document ends only at a marker or the end of the input, so no -DOC before an error.
    with pytest.raises(YAMLError) as info:
        ev("--- [a, b]]\n")
    assert info.value.events == ["+STR", "+DOC ---", "+SEQ []", "=VAL :a", "=VAL :b", "-SEQ"]


# ---- the backends agree ------------------------------------------------------------------------

HARD = [
    # properties on two lines, a flow collection as an implicit key, explicit keys
    "&m\n&k [ &i a, b ]: v\n? - x\n  - y\n: !!set {a, b: }\n",
    # block scalars at their edges, tabs as separation, comments in odd places
    "a: |+\n  x\n\n\nb: >-\n\n  y\n   z\n\n  w\nc:\t# c\n  - \tq\n",
    # several documents, directives, end markers, a byte order mark in the prefix
    "%YAML 1.2\n%TAG !t! tag:t,2000:\n--- !t!a\n...\n\ufeff# note\n--- 'x''y'\n--- |\n z\n",
    # double-quoted escapes and folding, plain scalars with colons and hashes
    '"a\\tb\\\n   c\n\n  d\\u00e9\\U0001F600"\n',
    "u: http://x/y#z\nv: a:b\nw: -1\nx: ?q\n? : k\n",
    # inputs that must fail, with the events read first
    "a: [x, {y: z}\nb: 1\n",
    "- a\n -b: c\n",
    "{a: 1, a: 2, ]",
    'a: "\\x4"\n',
    "? a\n? b\nc\n",
]


def _outcome(events_text, text):
    try:
        return events_text(text)
    except YAMLError as err:
        return (err.category, err.line, err.column, err.events)


def test_backends_agree_on_hard_input():
    backends = _event_backends()
    for text in HARD:
        outcomes = [_outcome(m.events_text, text) for m in backends]
        assert all(o == outcomes[0] for o in outcomes), text
    # Pin the pure backend to known answers as well, so agreement is not agreement on nonsense.
    assert _outcome(_pure.events_text, HARD[5])[:3] == ("syntax", 2, 1)
    assert _outcome(_pure.events_text, HARD[6])[:3] == ("syntax", 2, 5)
    assert _outcome(_pure.events_text, HARD[0]) == doc(
        "+MAP &m", "+SEQ [] &k", "=VAL &i :a", "=VAL :b", "-SEQ", "=VAL :v", "+SEQ", "=VAL :x",
        "=VAL :y", "-SEQ", "+MAP {} <tag:yaml.org,2002:set>", "=VAL :a", "=VAL :", "=VAL :b",
        "=VAL :", "-MAP", "-MAP",
    )  # fmt: skip


# ---- limits and hostile shapes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[" * 100_000,
        "{a: " * 100_000,
        "- " * 100_000,
        "? " * 100_000,
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=["flow-seq", "flow-map", "block-seq", "block-key", "closed-flow-seq"],
)
def test_deep_nesting_is_a_limit_error(ev, text):
    with pytest.raises(YAMLError) as info:
        ev(text)
    assert info.value.category == "limit"
    assert info.value.line == 1


def test_depth_at_the_limit_is_accepted(ev):
    lines = ev("[" * 9_999 + "]" * 9_999)
    assert lines.count("+SEQ []") == 9_999
    assert lines[-3:] == ["-SEQ", "-DOC", "-STR"]


def test_depth_limit_can_be_lowered():
    with pytest.raises(YAMLError) as info:
        _pure.events_text("[[[a]]]", max_depth=2)
    assert info.value.category == "limit"
    assert _pure.events_text("[[a]]", max_depth=2)[2] == "+SEQ []"


def test_many_anchors_and_aliases_are_only_events():
    # Expansion is a later stage's concern: here an alias is one event however large it expands.
    text = "a: &a [x, x, x, x]\n" + "".join(f"b{i}: &b{i} [*a, *a, *a, *a]\n" for i in range(50))
    lines = _pure.events_text(text)
    assert lines.count("=ALI *a") == 200


def test_long_runs_of_blanks_and_marks():
    run = " " * 300_000
    assert _pure.events_text('"a' + run + "\n" + run + 'b"')[2] == '=VAL "a b'
    assert _pure.events_text("k: 'a" + run + "\n  b'\n")[4] == "=VAL 'a b"
    assert _pure.events_text("k: a" + run + "\n  b\n")[4] == "=VAL :a b"
    assert _pure.events_text('"x\\\t' + run + '\n y"')[2] == '=VAL "x\\t y'
    with pytest.raises(YAMLError) as info:
        _pure.events_text("'" + "a''" * 100_000)
    assert (info.value.line, info.value.column) == (1, 300_002)
    with pytest.raises(YAMLError):
        _pure.events_text("a:\n" + "&" * 300_000 + "\nb\n")
    with pytest.raises(YAMLError):
        _pure.events_text("a:\n" + "&a " * 100_000 + "\nb\n")


def test_long_inputs_are_linear_enough():
    # Several megabytes of one scalar, one line and many lines parse without trouble.
    assert _pure.events_text("a" * 3_000_000)[2].startswith("=VAL :aaa")
    assert len(_pure.events_text("- x\n" * 200_000)) == 200_000 + 6
    assert _pure.events_text("k: |\n" + "  line\n" * 200_000)[4].startswith("=VAL |line")


# ---- encodings --------------------------------------------------------------------------------

TEXT = "k: é€😀\n"
EXPECT = doc("+MAP", "=VAL :k", "=VAL :é€😀", "-MAP")


@pytest.mark.parametrize(
    "data",
    [
        TEXT.encode("utf-8"),
        b"\xef\xbb\xbf" + TEXT.encode("utf-8"),
        b"\xff\xfe" + TEXT.encode("utf-16-le"),
        b"\xfe\xff" + TEXT.encode("utf-16-be"),
        TEXT.encode("utf-16-le"),
        TEXT.encode("utf-16-be"),
        b"\xff\xfe\x00\x00" + TEXT.encode("utf-32-le"),
        b"\x00\x00\xfe\xff" + TEXT.encode("utf-32-be"),
        TEXT.encode("utf-32-le"),
        TEXT.encode("utf-32-be"),
        bytearray(TEXT.encode("utf-8")),
        "\ufeff" + TEXT,
    ],
    ids=[
        "utf8",
        "utf8-bom",
        "utf16le-bom",
        "utf16be-bom",
        "utf16le",
        "utf16be",
        "utf32le-bom",
        "utf32be-bom",
        "utf32le",
        "utf32be",
        "bytearray",
        "str-bom",
    ],
)
def test_encodings(ev, data):
    assert ev(data) == EXPECT


@pytest.mark.parametrize(
    ("data", "line", "column"),
    [
        (b"a: \xff\n", 1, 4),
        (b"\xef\xbb\xbf\xc3\xa9: \xff\n", 1, 4),
        (b"a\nb: \xc3", 2, 4),
        (b"a: \xed\xa0\x80\n", 1, 4),  # a surrogate encoded in UTF-8
        (b"a: \xc0\x80\n", 1, 4),  # overlong
        ("a: x\n".encode("utf-16-le") + b"\x00\xd8" + b"b\x00", 2, 1),
        (b"\xff\xfe\xe9\x00:\x00 \x00\x00\xd8b\x00\n\x00", 1, 4),
        (b"\xff\xfe\x00\x00" + "a".encode("utf-32-le") + b"\x00\x00\x11\x00", 1, 2),
    ],
)
def test_malformed_encodings(ev, data, line, column):
    with pytest.raises(YAMLError) as info:
        ev(data)
    assert info.value.category == "encoding"
    assert (info.value.line, info.value.column) == (line, column)


# ---- the event tuples a later stage builds on ---------------------------------------------------


def test_event_spans_point_back_into_the_text():
    text = "--- &a !!str 'q'\n...\n---\nk: [x, \"y\"]\nl: |-\n  lit\n"
    events = parser.parse_events(text)
    scalars = {e[7]: e for e in events if e[0] == parser.SCALAR}
    for value, e in scalars.items():
        _, props, start, end, *_ = e
        assert props <= start <= end
        if e[6] == ":":
            assert text[start:end] == value
    q = scalars["q"]
    assert text[q[1] : q[2]] == "&a !!str " and text[q[2] : q[3]] == "'q'"
    assert text[scalars["y"][2] : scalars["y"][3]] == '"y"'
    lit = scalars["lit"]
    assert text[lit[2]] == "|" and text[lit[3] - 3 : lit[3]] == "lit"
    flow = next(e for e in events if e[0] == parser.SEQ_START)
    assert text[flow[2]] == "[" and flow[5] is True
    start, marker = events[1], text.index("---")
    assert (start[0], start[1], start[2], start[3]) == (parser.DOC_START, marker, marker + 3, True)


def test_collection_spans_cover_their_items():
    text = "top:\n  - a\n  - b\nnext: 1\n"
    events = parser.parse_events(text)
    seq = next(e for e in events if e[0] == parser.SEQ_START)
    end = next(e for e in events if e[0] == parser.SEQ_END)
    assert text[seq[2]] == "-"
    assert text[seq[2] : end[1]] == "- a\n  - b"


# ---- the exception contract under mutation -------------------------------------------------------

SEEDS = [
    "a: 1\nb: [x, {y: z}]\nc: |\n  lit\nd: &x !!str 'q'\ne: *x\n",
    '? a\n: b\n--- [a: b, ? c]\n...\n%TAG !e! t:\n--- !e!x "s\\u0041\\\n  t"\n',
    "- - a\n  - b\n- &m\n  k: >-\n    f\n\n    g\n- {a: , : b}\n",
    "\ufeffa: 1\r\n  b\r\n# c\r\n---\r\n- |+\r\n  x\r\n\r\n",
]
ALPHABET = list("-?:,[]{}#&*!|>'\"%@` \t\n\r.\\0aZ1\ufeff\x01\xe9\U0001f600")


def _assert_well_formed(events, text):
    """Events nest properly, mappings hold whole pairs and spans stay inside the text."""
    stack, kids = [], []
    assert events[0][0] == parser.STREAM_START and events[-1][0] == parser.STREAM_END
    for e in events[1:-1]:
        kind = e[0]
        if kind == parser.DOC_START:
            assert not stack
            stack.append(kind)
            kids.append(0)
        elif kind == parser.DOC_END:
            assert stack == [parser.DOC_START] and kids == [1], text
            stack.pop()
            kids.pop()
        elif kind in (parser.SEQ_START, parser.MAP_START):
            kids[-1] += 1
            stack.append(kind)
            kids.append(0)
        elif kind in (parser.SEQ_END, parser.MAP_END):
            opened, count = stack.pop(), kids.pop()
            assert opened == (parser.SEQ_START if kind == parser.SEQ_END else parser.MAP_START)
            assert opened == parser.SEQ_START or count % 2 == 0, text
        else:
            kids[-1] += 1
            if kind == parser.SCALAR:
                assert 0 <= e[1] <= e[2] <= e[3] <= len(text), text
    assert not stack, text


def test_mutated_documents_end_in_events_or_yaml_error():
    rnd = random.Random(20260)
    for _ in range(4_000):
        chars = list(rnd.choice(SEEDS))
        for _ in range(rnd.randint(1, 4)):
            pos = rnd.randrange(len(chars) + 1)
            op = rnd.random()
            if op < 0.4:
                chars.insert(pos, rnd.choice(ALPHABET))
            elif op < 0.7 and chars:
                del chars[min(pos, len(chars) - 1)]
            elif chars:
                chars[min(pos, len(chars) - 1)] = rnd.choice(ALPHABET)
        text = "".join(chars)
        try:
            lines = _pure.events_text(text)
        except YAMLError as err:
            assert err.category in {"syntax", "limit"}
            assert err.line is not None and err.line >= 1 and err.column >= 1
            assert err.events[:1] in ([], ["+STR"])
            assert err.line <= len(re.split(r"\r\n|\r|\n", text))
        else:
            decoded = decode(text)
            _assert_well_formed(parser.parse_events(decoded), decoded)
            assert lines[0] == "+STR" and lines[-1] == "-STR"
            assert lines.count("+DOC") + lines.count("+DOC ---") == (
                lines.count("-DOC") + lines.count("-DOC ...")
            )


def test_invalid_arguments_are_type_errors():
    with pytest.raises(TypeError):
        _pure.events_text(12)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _pure.events_text(None)  # type: ignore[arg-type]
