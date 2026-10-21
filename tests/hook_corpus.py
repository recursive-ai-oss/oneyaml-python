"""Documents and registrations for the comparison with PyYAML: callbacks, generated.

Documents come from a small grammar: collections in block and flow style, plain and quoted
scalars, tags a registration would take, anchors, aliases and merge keys. Registrations are
programs: functions that take a PyYAML-shaped module and a loader class and register on it.
The same program runs against PyYAML and against this library.
"""

from __future__ import annotations

import random
import re

TAGS_SCALAR = ["!s", "!n", "!x/a", "!x/b", "!y/c", "!!str", "!!int", "!!float", "!!bool", "!Y"]
TAGS_SEQ = ["!q", "!n", "!x/a", "!!seq", "!!set", "!!omap", "!!pairs"]
TAGS_MAP = ["!m", "!n", "!gen", "!x/b", "!Y", "!!map", "!!set"]

_WORDS = ["a", "b", "key", "name", "items", "list", "x1", "yes", "no", "on", "off", "null", "~"]
_SCALARS = [
    "1", "-5", "0x1F", "0o7", "017", "1_000", "1:30", "1.5", ".5", "1e3", "1.5e+3", ".inf", ".nan",
    "2001-12-14", "2001-12-14 21:59:43.10 -5", "true", "False", "NO", "Off", "null", "~", "word",
    "two words", "123-4567", "555-1234", "ABC", "XYZ", "abc", "${HOME}", "${X}", "$x",
    "https://x.org/a?b=c", "a#b", "- x", "0", "00", "1,2", "yes please",
]  # fmt: skip
_QUOTED = ["'1'", '"two\\nlines"', "'yes'", '"ABC"', "'${HOME}'", "''", "'a b'", '"\\t"']


class _Gen:
    def __init__(self, rng: random.Random, tagged: float = 0.35):
        self.rng = rng
        self.tagged = tagged
        self.n = 0
        self.anchors: list[tuple[str, str]] = []  # (name, kind) of finished anchored nodes

    def props(self, tags: list[str], kind: str) -> tuple[str, str | None]:
        """The tag and anchor text in front of a node, and the anchor name."""
        rng = self.rng
        out = []
        anchor = None
        if rng.random() < self.tagged:
            tag = rng.choice(tags)
            if rng.random() < 0.08:
                tag = rng.choice(TAGS_SCALAR + TAGS_SEQ + TAGS_MAP)  # sometimes the wrong kind
            out.append(tag)
        if rng.random() < 0.2:
            self.n += 1
            anchor = f"a{self.n}"
            out.append("&" + anchor)
        return " ".join(out), anchor

    def scalar(self) -> str:
        rng = self.rng
        if rng.random() < 0.2:
            return rng.choice(_QUOTED)
        return rng.choice(_SCALARS)

    def key(self) -> str:
        rng = self.rng
        return rng.choice(_WORDS) if rng.random() < 0.7 else rng.choice(_SCALARS[:14])

    def alias_for(self, kinds: tuple[str, ...]) -> str | None:
        ok = [n for n, k in self.anchors if k in kinds]
        if ok and self.rng.random() < 0.3:
            return "*" + self.rng.choice(ok)
        return None

    # A node is (text lines, is_collection). Block nodes return lines at indent 0.

    def block_value(self, depth: int) -> tuple[str, list[str]]:
        """A value: the text that follows ``key:`` on its line, and the lines under it."""
        rng = self.rng
        roll = rng.random()
        if depth <= 0 or roll < 0.45:
            alias = self.alias_for(("s", "q", "m"))
            if alias:
                return " " + alias, []
            tag, anchor = self.props(TAGS_SCALAR, "s")
            s = self.scalar()
            if anchor:
                self.anchors.append((anchor, "s"))
            return " " + " ".join(x for x in (tag, s) if x), []
        if roll < 0.7:
            tag, anchor = self.props(TAGS_SEQ, "q")
            lines = self.block_seq(depth - 1)
            if anchor:
                self.anchors.append((anchor, "q"))
            return (" " + tag if tag else ""), ["  " + x for x in lines]
        if roll < 0.85:
            tag, anchor = self.props(TAGS_MAP, "m")
            lines = self.block_map(depth - 1)
            if anchor:
                self.anchors.append((anchor, "m"))
            return (" " + tag if tag else ""), ["  " + x for x in lines]
        text = self.flow(depth)
        return " " + text, []

    def block_map(self, depth: int) -> list[str]:
        rng = self.rng
        lines: list[str] = []
        used: set[str] = set()
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.1:
                alias = self.alias_for(("m",))
                if alias:
                    lines.append(f"<<: {alias}")
                    continue
                if rng.random() < 0.5:
                    lines.append("<<: {x: 1, y: [1, 2]}")
                    continue
            key = self.key()
            if key in used and rng.random() < 0.8:
                key += "_"
            used.add(key)
            if rng.random() < 0.08:
                key = rng.choice(["!s ", "!!str ", "&k "]) + key
                if key.startswith("&k"):
                    key = "&k" + str(self.n) + key[2:]
                    self.n += 1
            head, under = self.block_value(depth)
            lines.append(f"{key}:{head}")
            lines.extend(under)
        return lines

    def block_seq(self, depth: int) -> list[str]:
        rng = self.rng
        lines: list[str] = []
        for _ in range(rng.randint(1, 4)):
            head, under = self.block_value(depth)
            if under:
                # A nested block starts on the same line as the dash, or under it.
                lines.append("-" + head)
                lines.extend(under)
            else:
                lines.append("-" + head)
        return lines

    def flow(self, depth: int) -> str:
        rng = self.rng
        roll = rng.random()
        if depth <= 0 or roll < 0.4:
            alias = self.alias_for(("s", "q", "m"))
            if alias:
                return alias
            tag, anchor = self.props(TAGS_SCALAR, "s")
            if anchor:
                self.anchors.append((anchor, "s"))
            s = rng.choice([x for x in _SCALARS + _QUOTED if not re.search(r"[,\[\]{}:]|#", x)])
            return " ".join(x for x in (tag, s) if x)
        if roll < 0.7:
            tag, anchor = self.props(TAGS_SEQ, "q")
            body = "[" + ", ".join(self.flow(depth - 1) for _ in range(rng.randint(0, 3))) + "]"
            if anchor:
                self.anchors.append((anchor, "q"))
            return " ".join(x for x in (tag, body) if x)
        tag, anchor = self.props(TAGS_MAP, "m")
        items = []
        used = set()
        for _ in range(rng.randint(0, 3)):
            k = self.key()
            if k in used:
                continue
            used.add(k)
            items.append(f"{k}: {self.flow(depth - 1)}")
        body = "{" + ", ".join(items) + "}"
        if anchor:
            self.anchors.append((anchor, "m"))
        return " ".join(x for x in (tag, body) if x)

    def document(self) -> str:
        rng = self.rng
        roll = rng.random()
        if roll < 0.55:
            tag, anchor = self.props(TAGS_MAP, "m")
            lines = self.block_map(rng.randint(1, 3))
            return (tag + "\n" if tag else "") + "\n".join(lines) + "\n"
        if roll < 0.75:
            tag, anchor = self.props(TAGS_SEQ, "q")
            lines = self.block_seq(rng.randint(1, 3))
            return (tag + "\n" if tag else "") + "\n".join(lines) + "\n"
        if roll < 0.9:
            return self.flow(rng.randint(1, 3)) + "\n"
        tag, _ = self.props(TAGS_SCALAR, "s")
        return " ".join(x for x in (tag, self.scalar()) if x) + "\n"


def tagged_documents(count: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    return [_Gen(rng, rng.choice((0.15, 0.35, 0.6))).document() for _ in range(count)]


# ---- registrations -----------------------------------------------------------------------------


def _describe(node):
    """A value that tells the callback what it was given."""
    if node.id == "scalar":
        value = node.value
    elif node.id == "sequence":
        value = [c.tag for c in node.value]
    else:
        value = [(k.tag, v.tag) for k, v in node.value]
    style = getattr(node, "style", None) if node.id == "scalar" else node.flow_style
    return (
        node.tag, node.id, style, node.start_mark.line, node.start_mark.column,
        node.end_mark.line, node.end_mark.column, value,
    )  # fmt: skip


def _plain_ctors(y, loader):
    def s_ctor(ld, node):
        return ("S", ld.construct_scalar(node))

    def q_ctor(ld, node):
        return ("Q", ld.construct_sequence(node, deep=True))

    def m_ctor(ld, node):
        return ("M", ld.construct_mapping(node, deep=True))

    def n_ctor(ld, node):
        return ("N", _describe(node))

    def gen_ctor(ld, node):
        data = {}
        yield data
        data.update(ld.construct_mapping(node))

    class Y(y.YAMLObject):
        yaml_tag = "!Y"
        yaml_loader = loader

        def __eq__(self, other):
            return type(self) is type(other) and self.__dict__ == other.__dict__

        __hash__ = None

    loader.add_constructor("!s", s_ctor)
    loader.add_constructor("!q", q_ctor)
    loader.add_constructor("!m", m_ctor)
    loader.add_constructor("!n", n_ctor)
    loader.add_constructor("!gen", gen_ctor)
    return loader


def _multi(y, loader):
    def x_multi(ld, suffix, node):
        if node.id == "scalar":
            return ("X", suffix, ld.construct_scalar(node))
        if node.id == "sequence":
            return ("X", suffix, ld.construct_sequence(node, deep=True))
        return ("X", suffix, ld.construct_mapping(node, deep=True))

    def bang(ld, suffix, node):
        return ("B", suffix, node.id)

    loader.add_multi_constructor("!x/", x_multi)
    loader.add_multi_constructor("!y", bang)
    return loader


def _multi_catch_all(y, loader):
    def anything(ld, tag, node):
        return ("ANY", tag, node.id)

    loader.add_multi_constructor("!", anything)
    loader.add_multi_constructor("!x/", lambda ld, suffix, node: ("X", suffix))
    loader.add_multi_constructor(None, lambda ld, tag, node: ("NONE", tag))
    return loader


def _resolvers(y, loader):
    loader.add_implicit_resolver("!phone", re.compile(r"^\d{3}-\d{4}$"), list("0123456789"))
    loader.add_implicit_resolver("!caps", re.compile(r"^[A-Z]{3}$"), None)
    loader.add_implicit_resolver("!envv", re.compile(r"^\$\{\w+\}$"), ["$"])
    loader.add_implicit_resolver("!digits", re.compile(r"^\d+$"), list("0123456789"))
    loader.add_implicit_resolver("!yes", re.compile(r"^yes please$"), ["y"])
    loader.add_constructor("!phone", lambda ld, n: ("P", ld.construct_scalar(n)))
    loader.add_constructor("!caps", lambda ld, n: ("C", ld.construct_scalar(n)))
    loader.add_constructor("!envv", lambda ld, n: ("E", ld.construct_scalar(n)[2:-1]))
    loader.add_constructor("!digits", lambda ld, n: ("D", ld.construct_scalar(n)))
    return loader


def _resolver_without_constructor(y, loader):
    loader.add_implicit_resolver("!lost", re.compile(r"^word$"), list("w"))
    return loader


def _overrides(y, loader):
    base = y.SafeLoader

    def up(ld, node):
        return base.construct_yaml_str(ld, node).upper()

    def twice(ld, node):
        return base.construct_yaml_int(ld, node) * 2

    def neg(ld, node):
        return not base.construct_yaml_bool(ld, node)

    loader.add_constructor("tag:yaml.org,2002:str", up)
    loader.add_constructor("tag:yaml.org,2002:int", twice)
    loader.add_constructor("tag:yaml.org,2002:bool", neg)
    return loader


def _no_bool(y, loader):
    loader.yaml_implicit_resolvers = {
        k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:bool"]
        for k, v in loader.yaml_implicit_resolvers.items()
    }
    return loader


def _catch_unknown(y, loader):
    loader.add_constructor(None, lambda ld, n: ("U", n.tag, n.id))
    return loader


def _tag_as_std(y, loader):
    base = y.SafeLoader
    loader.add_constructor("!s", base.construct_yaml_str)
    loader.add_constructor("!q", base.construct_yaml_seq)
    loader.add_constructor("!m", base.construct_yaml_map)
    loader.add_constructor("!x/a", base.construct_yaml_int)
    loader.add_constructor("!x/b", base.construct_yaml_set)
    loader.add_constructor("!n", base.construct_yaml_omap)
    return loader


def _paths(y, loader):
    loader.add_path_resolver("!s", ["a"], str)
    loader.add_path_resolver("!q", ["list"], list)
    loader.add_path_resolver("!m", ["items", 0], dict)
    loader.add_path_resolver("!s", ["key", None], str)
    loader.add_constructor("!s", lambda ld, n: ("S", ld.construct_scalar(n)))
    loader.add_constructor("!q", lambda ld, n: ("Q", ld.construct_sequence(n, deep=True)))
    loader.add_constructor("!m", lambda ld, n: ("M", ld.construct_mapping(n, deep=True)))
    return loader


def _unique_keys(y, base):
    class Unique(base):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise y.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
            return super().construct_mapping(node, deep)

    return Unique


PROGRAMS = {
    "ctors": _plain_ctors,
    "multi": _multi,
    "multi-catch-all": _multi_catch_all,
    "resolvers": lambda y, ld: _resolvers(y, _plain_ctors(y, ld)),
    "resolver-no-constructor": _resolver_without_constructor,
    "overrides": _overrides,
    "no-bool": _no_bool,
    "catch-unknown": lambda y, ld: _catch_unknown(y, _plain_ctors(y, ld)),
    "tag-as-std": _tag_as_std,
    "paths": _paths,
    "everything": lambda y, ld: _multi(y, _resolvers(y, _plain_ctors(y, ld))),
}
SUBCLASS_PROGRAMS = {"unique-keys": _unique_keys}
