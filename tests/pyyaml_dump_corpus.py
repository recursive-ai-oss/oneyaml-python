"""Generated values and dump options for comparing the PyYAML dump side with PyYAML itself.

Everything is derived from a seed, so a failure names the seed that reproduces it.
"""

from __future__ import annotations

import datetime
import random

# Strings that have gone wrong in some emitter somewhere: YAML 1.1 words and numbers, indicator
# characters at every position, line breaks of every kind, control and non-ASCII characters, and
# lengths around the width and the simple-key limit.
TRICKY = [
    "", " ", "  ", "a", "yes", "No", "on", "OFF", "y", "n", "true", "null", "~", "Null", "123",
    "0x1f", "1e3", "1.5", "-1", "+1", "0o7", "017", "1_000", "1:30", ".inf", ".nan",
    "2020-01-02", "2020-01-02 10:00:00", "<<", "=", "#", "# c", "a #b", "a: b", "a:b", ":",
    ": a", "- a", "-a", "-", "?", "? a", "?a", "[a]", "{a}", "a, b", "@a", "`a", "!a", "&a",
    "*a", "|", ">", "'", '"', "it's", 'say "hi"', "%a", "---", "...", "--- a", "... a",
    "a\nb", "a\nb\n", "\n", "\n\n", "a\n\nb", " a", "a ", " a ", "a  b", "a\n b", "a \nb",
    "tab\tx", "\tx", "x\ty", "a\rb", "a\r\nb", "\x00", "\x07", "\x1b", "\x7f", "\x85", "\xa0",
    "caf\u00e9", "\u2028", "\u2029", "a\u2028b", "\ufeff", "x\ufeffy", "\U0001f600", "\ud7ff",
    "\ue000", "\uffff", "\ufffd", "\u65e5\u672c\u8a9e", "a" * 79, "a" * 80, "a" * 81, "a" * 200,
    " ".join(["word"] * 30), " ".join(["w\u00f6rd"] * 30), ("x" * 60 + " ") * 3,
    "a  " + "b" * 100, ("ab " * 40).strip(), "ab\n" * 40, "it's " * 30, 'say "hi" ' * 20,
    "x" * 127, "x" * 128, "x" * 123, "x" * 122, "\u00e9" * 130,
]  # fmt: skip

_ALPHABET = list(" abc:#-?,[]{}&*!|>'\"%@`\\\n\t\x85\u2028\u00e90189.+_~=<yYnN")


def random_string(r: random.Random) -> str:
    if r.random() < 0.5:
        return r.choice(TRICKY)
    return "".join(r.choice(_ALPHABET) for _ in range(r.randint(0, 40)))


def scalar(r: random.Random):
    k = r.random()
    if k < 0.45:
        return random_string(r)
    if k < 0.55:
        return r.choice([0, 1, -1, 10**20, -(10**30), 255, 12345678901234567890])
    if k < 0.65:
        return r.choice([
            0.0, -0.0, 1.5, -2.25, 1e17, 1e-7, 1e300, 5e-324, float("inf"), float("-inf"),
            float("nan"), 0.1, 100.0, 1e16, 1.0e22, 123456789.123456789,
        ])  # fmt: skip
    if k < 0.72:
        return r.choice([True, False])
    if k < 0.80:
        return None
    if k < 0.84:
        return r.choice([b"", b"abc", bytes(range(40)), b"x" * 100])
    if k < 0.88:
        utc = datetime.timezone.utc
        return r.choice([
            datetime.date(2020, 1, 2), datetime.datetime(2020, 1, 2, 3, 4, 5),
            datetime.datetime(2001, 12, 14, 21, 59, 43, 100000),
            datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=utc), datetime.date(1, 1, 1),
        ])  # fmt: skip
    return random_string(r)


def key(r: random.Random, tuples: bool = True):
    k = r.random()
    if k < 0.75:
        return random_string(r)
    if k < 0.85:
        return r.choice([1, 2, 10, -3, 1.5, True, None])
    if k < 0.9 and tuples:
        return r.choice([(1, 2), (), ("a", "b"), ("x",)])
    return random_string(r)


def value(r: random.Random, depth: int = 0, tuples: bool = True):
    k = r.random()
    if depth >= 4 or k < 0.4:
        return scalar(r)
    if k < 0.65:
        return [value(r, depth + 1, tuples) for _ in range(r.randint(0, 5))]
    if k < 0.9:
        d = {}
        for _ in range(r.randint(0, 5)):
            d[key(r, tuples)] = value(r, depth + 1, tuples)
        return d
    if k < 0.94 and tuples:
        return tuple(value(r, depth + 1, tuples) for _ in range(r.randint(0, 3)))
    if k < 0.97 and tuples:
        return {r.choice([1, 2, 3, "a", "b", "c", "yes", (1, 2)]) for _ in range(r.randint(0, 4))}
    return scalar(r)


KEYWORDS = [
    {}, {"sort_keys": False}, {"default_flow_style": None}, {"default_flow_style": True},
    {"indent": 4}, {"indent": 3, "width": 30}, {"width": 20}, {"width": 1000},
    {"allow_unicode": True}, {"explicit_start": True}, {"explicit_end": True},
    {"explicit_start": True, "explicit_end": True}, {"canonical": True}, {"default_style": '"'},
    {"default_style": "'"}, {"default_style": "|"}, {"default_style": ">"},
    {"line_break": "\r\n"}, {"line_break": "\r"}, {"version": (1, 1)},
    {"tags": {"!e!": "tag:example.com,2000:"}},
    {"indent": 9, "width": 40, "allow_unicode": True, "default_flow_style": None},
    {"indent": 1}, {"indent": 10}, {"width": 5},
]  # fmt: skip


def case(seed: int, tuples: bool = True):
    """The value, the keywords and whether it is a stream of documents, for ``seed``."""
    r = random.Random(seed)
    v = value(r, 0, tuples)
    kw = r.choice(KEYWORDS)
    if r.random() < 0.15:
        return [v, value(r, 0, tuples), value(r, 0, tuples)], kw, True
    return v, kw, False


def has_tuple_key(x) -> bool:
    """Whether a tuple is a mapping key or a set member anywhere (PyYAML cannot read it back)."""
    work = [x]
    while work:
        y = work.pop()
        if isinstance(y, dict):
            if any(isinstance(k, tuple) for k in y):
                return True
            work.extend(y.values())
        elif isinstance(y, (list, tuple)):
            work.extend(y)
        elif isinstance(y, set) and any(isinstance(k, tuple) for k in y):
            return True
    return False


def normal(x):
    """A form in which a value and what is read back from its text compare equal: tuples read
    back as lists, nan equals nan, 1 and True stay apart."""
    if isinstance(x, (list, tuple)):
        return ("L", [normal(i) for i in x])
    if isinstance(x, dict):
        return ("D", sorted((repr(normal(k)), normal(v)) for k, v in x.items()))
    if isinstance(x, (set, frozenset)):
        return ("S", sorted(repr(normal(k)) for k in x))
    if isinstance(x, float) and x != x:
        return ("nan",)
    return (type(x).__name__, x)
