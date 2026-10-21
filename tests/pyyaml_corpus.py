"""Documents for the comparison with PyYAML: YAML 1.1 scalar forms, generated.

One function, deterministic: random strings over the characters that make up numbers and
dates find the near misses of every resolver rule, and the lists name the forms outright.
"""

from __future__ import annotations

import random

_BOOL_WORDS = [
    w for base in ("yes", "no", "true", "false", "on", "off", "y", "n", "null")
    for w in (base, base.capitalize(), base.upper(), base.swapcase(), base + " ", "x" + base)
]  # fmt: skip

_FORMS = [
    # integers
    "0", "-0", "+0", "00", "007", "-017", "+0_7", "08", "0_8", "09.5", "0_", "0__", "_1", "1_",
    "1__000", "1_000", "-1_000", "+1_0", "0b1010", "-0b11", "+0b1_1", "0b", "0b_", "0b2", "0B1",
    "0x1F", "0x_1f", "-0xff", "+0xFF", "0x", "0x_", "0xg", "0X1f", "0o17", "-0o17", "0o", "1:30",
    "1:30:00", "-1:30", "+190:20:30", "1:60", "1:5", "1:05", "0:30", "01:30", "1_0:30", "1:3_0",
    "1:", ":1", "1::2", "1:30:", "123456789012345678", "1234567890123456789",
    "-1234567890123456789", "9223372036854775807", "9223372036854775808", "-9223372036854775808",
    "0x7fffffffffffffff", "0x8000000000000000", "0xffffffffffffffffff", "0b" + "1" * 64,
    "07777777777777777777777", "59:59:59:59:59:59:59:59:59:59:59:59:59:59:59:59:59",
    "1" + "0" * 40, "-" + "7" * 30,
    # floats
    "1.5", "-1.5", "+1.5", "1.", "-1.", ".5", "-.5", "+.5", "1.e3", "1.5e3", "1.5e+3", "1.5e-3",
    "1.5E+3", "1e3", "1e+3", ".5e+3", ".5e3", "1_0.5", "1.5_5", "1._5", "._5", ".", "..5", "1.5.5",
    "1:30.5", "-1:30.5", "1:30:00.25", "0:30.5", "1:60.5", "1_0:30.5", ".inf", "-.inf", "+.inf",
    ".Inf", ".INF", ".iNf", "inf", ".nan", ".NaN", ".NAN", "-.nan", "+.nan", ".nAn", "nan",
    "1e400", "1.0e+400", "-1.0e+400", "1.0e-400", "0.0", "-0.0", "00.5", "0.5", "1.5e", "1.5e+",
    "0.1", "3.14159", "1.7976931348623157e+308", "5e-324" , "1.0e+999",
    # dates
    "2001-12-14", "2001-1-1", "2001-12-4", "0000-00-00", "2001-13-01", "2001-02-30",
    "2001-12-14t21:59:43.10-05:00",
    "2001-12-14T21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "2001-12-14 21:59:43.10 Z",
    "2001-12-14 21:59:43Z", "2001-12-14 21:59:43", "2001-12-14T21:59:43", "2001-12-14T1:59:43",
    "2001-12-14T21:59:43.", "2001-12-14T21:59:43.123456789", "2001-12-14T21:59:43+5",
    "2001-12-14T21:59:43+05:30", "2001-12-14T21:59:43 +05:30", "2001-12-14T21:59:43-0530",
    "2001-12-14T24:00:00", "2001-12-14T21:60:00", "2001-12-14T21:59:60", "2001-12-14 ",
    "2001-12-14T", "2001-12-14T21:59", "+2001-12-14", "12001-12-14", "2001-12-14x",
    "2001-12-14T21:59:43Zx",
    "2001-12-14T21:59:43 Z", "2001-12-14T21:59:43\tZ", "2001-12-14\t21:59:43",
    "2001-12-14  21:59:43",
    "2001-12-14T21:59:43.5+14:00", "2001-12-14T21:59:43+25", "2001-12-14T21:59:43+99:99",
    # merge, value, other
    "<<", "<<x", "<", "=", "~", "~~", "Null", "nulL", "-", "+", "?",
] + _BOOL_WORDS  # fmt: skip

_ALPHABET = "0123456789__::..--++eExXbBoOaAfFTZ "


def scalars(count: int = 6000, seed: int = 11) -> list[str]:
    """Plain-scalar texts: the named forms first, then random near misses."""
    rng = random.Random(seed)
    out = list(_FORMS)
    seen = set(out)
    while len(out) < count:
        n = rng.randint(1, 9)
        s = "".join(rng.choice(_ALPHABET) for _ in range(n)).strip()
        # Keep to what a plain scalar can start with and end on.
        if s and s not in seen and s[0] not in "-?:" or s[:1] == "-" and len(s) > 1 and s[1] != " ":
            seen.add(s)
            out.append(s)
    return out


def documents(scalar: str) -> list[str]:
    """The documents one scalar is tried in: as a value, quoted, and under standard tags."""
    return [
        f"k: {scalar}\n",
        f"k: '{scalar}'\n",
        f"k: !!str {scalar}\n",
        f"k: !!int {scalar}\n",
        f"k: !!float {scalar}\n",
        f"k: !!bool {scalar}\n",
        f"k: !!null {scalar}\n",
        f"k: !!timestamp {scalar}\n",
    ]
