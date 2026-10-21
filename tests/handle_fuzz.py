"""A random-edit oracle for the pure lossless document.

Random documents (aliases, CRLF, several documents, explicit keys, comments, block scalars) get
random edits and patches (add, replace, remove, move, copy, test, merge patch, atomic failures).
After each the value is compared with a plain Python model, the row table with a fresh parse of
the text, and a failed patch must leave the bytes as they were.

Run by hand: python tests/handle_fuzz.py FROM TO [SHOW]
"""
# ruff: noqa: E501, UP031, E721, B905, B007

import copy
import json
import random
import sys
import traceback
from collections import Counter

from oneyaml import Limits, _pure
from oneyaml._errors import PatchError, YAMLError
from oneyaml._pure import _edit as E
from oneyaml._pure import _tree as T
from oneyaml._pure import _write as W
from oneyaml._pure._handle import parse
from oneyaml._pure._parser import parse_events

L = Limits()
WORDS = [
    "a",
    "b",
    "key",
    "name",
    "x y",
    "1",
    "true",
    "null",
    "~",
    "hello world",
    "yes",
    "0x1F",
    "2.5",
    "é",
    "it's",
    "a: b",
    "# c",
    "-",
    "- x",
    "[z]",
    "{q}",
    "",
    " lead",
    "trail ",
    "tab\tx",
    "multi\nline",
    "ends\n",
    "two\n\n",
    "  indented\nrest\n",
    'q"uote',
    "back\\slash",
]
KEYS = ["k%d" % i for i in range(8)] + ["a b", "x"]
REFUSALS = Counter()


class Ref:
    """A value that is written once with an anchor and then by alias."""

    n = 0

    def __init__(self, v):
        self.v = v
        self.name = None


def plain(v):
    if isinstance(v, Ref):
        return plain(v.v)
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


def rscalar(r):
    c = r.random()
    if c < 0.2:
        return r.randint(-5, 1000)
    if c < 0.3:
        return r.choice([True, False, None])
    if c < 0.4:
        return r.choice([0.5, 2.0, 1e20, -3.25])
    return r.choice(WORDS)


def rval(r, d=0, pool=None):
    if pool is not None and d > 0 and pool and r.random() < 0.15:
        return r.choice(pool)
    c = r.random()
    if d >= 3 or c < 0.5:
        v = rscalar(r)
    elif c < 0.75:
        v = {r.choice(KEYS): rval(r, d + 1, pool) for _ in range(r.randint(0, 4))}
    else:
        v = [rval(r, d + 1, pool) for _ in range(r.randint(0, 4))]
    if pool is not None and d > 0 and r.random() < 0.15:
        v = Ref(v)
        pool.append(v)
    return v


def qs(r, s, flow):
    c = r.random()
    if c < 0.3 and s and "\n" not in s and s == s.strip() and not flow and W.plain_ok(s):
        return s
    if c < 0.6 and "\n" not in s and "\x00" not in s:
        return "'" + s.replace("'", "''") + "'"
    return json.dumps(s)


def ws(r):
    return r.choice(["", " ", "  ", "\t"])


def sc(r, v, flow=False):
    if isinstance(v, str):
        return qs(r, v, flow)
    return W.scalar_inline(v)[0]


class Em:
    def __init__(self, r):
        self.r = r
        self.cm = True

    def deref(self, x):
        """(value, anchor prefix, alias text)"""
        if isinstance(x, Ref):
            if x.name is None:
                Ref.n += 1
                x.name = "a%d" % Ref.n
                return x.v, "&%s " % x.name, None
            return None, "", "*" + x.name
        return x, "", None

    def flow(self, x):
        r = self.r
        x, anc, al = self.deref(x)
        if al:
            return al
        if isinstance(x, dict):
            if not x:
                return anc + "{}"
            return (
                anc
                + "{"
                + ws(r)
                + ("," + ws(r) + " ").join(
                    "%s: %s" % (sc(r, k, True), self.flow(y)) for k, y in x.items()
                )
                + ws(r)
                + "}"
            )
        if isinstance(x, list):
            if not x:
                return anc + "[]"
            return (
                anc + "[" + ws(r) + ("," + ws(r) + " ").join(self.flow(y) for y in x) + ws(r) + "]"
            )
        return anc + sc(r, x, True)

    def block(self, v, ind, flowtop=False):
        r = self.r
        cm = self.cm
        if isinstance(v, dict):
            out = []
            for k, x in v.items():
                kt = sc(r, k)
                x, anc, al = self.deref(x)
                pre = " " * ind + kt + ":"
                if al:
                    e = pre + " " + al
                elif isinstance(x, (dict, list)) and x and r.random() < 0.9:
                    step = r.choice([2, 2, 4, 3])
                    if isinstance(x, list) and r.random() < 0.4:
                        sub = self.block(x, ind)
                    else:
                        sub = self.block(x, ind + step)
                    e = (
                        pre
                        + (" " + anc.strip() if anc else "")
                        + (" # c" if cm and r.random() < 0.2 else "")
                        + "\n"
                        + sub
                    )
                elif isinstance(x, str) and "\n" in x and r.random() < 0.7:
                    ch = (
                        ""
                        if x.endswith("\n") and not x.endswith("\n\n")
                        else ("-" if not x.endswith("\n") else "+")
                    )
                    body = x.rstrip("\n").split("\n")
                    if (
                        any(ln.strip() == "" and ln != "" for ln in body)
                        or x.startswith("\n")
                        or body[0][:1] == " "
                    ):
                        e = pre + " " + anc + sc(r, x)
                    else:
                        e = (
                            pre
                            + " "
                            + anc
                            + "|"
                            + ch
                            + "\n"
                            + "\n".join(" " * (ind + 2) + ln for ln in body)
                            + ("\n" if ch == "+" else "")
                        )
                else:
                    e = (
                        pre
                        + r.choice([" ", " ", "  "])
                        + (self.flow_of(x, anc))
                        + (
                            " # t"
                            if cm and r.random() < 0.2 and not isinstance(x, (dict, list))
                            else ""
                        )
                    )
                if r.random() < 0.1 and e.startswith(" " * ind + kt + ":") and "\n" not in kt:
                    # explicit key
                    e = (
                        " " * ind
                        + "? "
                        + kt
                        + "\n"
                        + " " * ind
                        + ":"
                        + e[len(" " * ind + kt) + 1 :]
                    )
                out.append(e)
                if cm and r.random() < 0.15:
                    out.append(" " * ind + "# note")
            return "\n".join(out)
        out = []
        for x in v:
            x, anc, al = self.deref(x)
            if al:
                out.append(" " * ind + "- " + al)
            elif isinstance(x, dict) and x and not anc and r.random() < 0.7:
                sub = self.block(x, ind + 2)
                out.append(" " * ind + "- " + sub[ind + 2 :])
            elif isinstance(x, (dict, list)) and x:
                out.append(
                    " " * ind
                    + "-"
                    + (" " + anc.strip() if anc else "")
                    + "\n"
                    + self.block(x, ind + 2)
                )
            else:
                out.append(
                    " " * ind
                    + "- "
                    + self.flow_of(x, anc)
                    + (" # t" if cm and r.random() < 0.2 else "")
                )
            if cm and r.random() < 0.1:
                out.append(" " * ind + "# note")
        return "\n".join(out)

    def flow_of(self, x, anc):
        r = self.r
        if isinstance(x, dict):
            return anc + (
                "{}"
                if not x
                else "{"
                + ws(r)
                + ("," + ws(r) + " ").join(
                    "%s: %s" % (sc(r, k, True), self.flow(y)) for k, y in x.items()
                )
                + ws(r)
                + "}"
            )
        if isinstance(x, list):
            return anc + (
                "[]"
                if not x
                else "[" + ws(r) + ("," + ws(r) + " ").join(self.flow(y) for y in x) + ws(r) + "]"
            )
        return anc + sc(r, x)


def gen_doc(r, marker, aliases):
    pool = [] if aliases else None
    while True:
        v = rval(r, 0, pool)
        v = v.v if isinstance(v, Ref) else v
        if isinstance(v, (dict, list)) and v:
            break
        pool = [] if aliases else None
    Ref.n = 0
    em = Em(r)
    t = em.block(v, 0)
    if marker:
        pre = r.choice(["---\n", "--- # m\n"])
        if pre.startswith("--- #") and isinstance(v, list):
            pre = "---\n"
    else:
        pre = r.choice(["", "# head\n", "%YAML 1.2\n---\n", "---\n"])
    t = pre + t + r.choice(["\n", "\n", "\n\n# end\n", ""])
    return t


def gen(r):
    aliases = r.random() < 0.5
    n = 1 if r.random() < 0.6 else r.randint(2, 3)
    parts = []
    for i in range(n):
        t = gen_doc(r, i > 0, aliases)
        if i > 0 and parts and not parts[-1].endswith("\n"):
            parts[-1] += "\n"
        if i > 0 and r.random() < 0.3:
            parts.append("...\n")
        parts.append(t)
    t = "".join(parts)
    # separate documents: a bare document needs the previous one closed
    if r.random() < 0.3:
        t = t.replace("\n", "\r\n")
    if r.random() < 0.05:
        t = "\ufeff" + t
    return t


# ---- the model --------------------------------------------------------------------------------
class ModelErr(Exception):
    """The model says the operation is invalid."""


def same(a, b):
    if type(a) != type(b):
        return False
    if isinstance(a, dict):
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and a != a:
        return b != b
    return a == b


def walk(v, toks):
    for t in toks:
        if isinstance(v, dict):
            if t not in v:
                raise ModelErr("key")
            v = v[t]
        elif isinstance(v, list):
            if not (t.isdigit() and (t == "0" or t[0] != "0")) or int(t) >= len(v):
                raise ModelErr("index")
            v = v[int(t)]
        else:
            raise ModelErr("scalar")
    return v


def m_add(root, toks, val):
    if not toks:
        return copy.deepcopy(val)
    par = walk(root, toks[:-1])
    t = toks[-1]
    if isinstance(par, dict):
        par[t] = copy.deepcopy(val)
    elif isinstance(par, list):
        if t == "-":
            par.append(copy.deepcopy(val))
        elif t.isdigit() and (t == "0" or t[0] != "0") and int(t) <= len(par):
            par.insert(int(t), copy.deepcopy(val))
        else:
            raise ModelErr("index")
    else:
        raise ModelErr("scalar")
    return root


def m_remove(root, toks):
    if not toks:
        raise ModelErr("root")
    par = walk(root, toks[:-1])
    walk(par, [toks[-1]])
    if isinstance(par, dict):
        del par[toks[-1]]
    else:
        del par[int(toks[-1])]
    return root


def m_replace(root, toks, val):
    if not toks:
        return copy.deepcopy(val)
    walk(root, toks)
    par = walk(root, toks[:-1])
    if isinstance(par, dict):
        par[toks[-1]] = copy.deepcopy(val)
    else:
        par[int(toks[-1])] = copy.deepcopy(val)
    return root


def m_apply(root, op):
    kind = op["op"]
    toks = ptr(op["path"])
    if kind == "add":
        return m_add(root, toks, op["value"])
    if kind == "remove":
        return m_remove(root, toks)
    if kind == "replace":
        return m_replace(root, toks, op["value"])
    if kind == "test":
        if not same(walk(root, toks), op["value"]):
            raise ModelErr("test")
        return root
    src = ptr(op["from"])
    if kind == "copy":
        val = copy.deepcopy(walk(root, src))
        return m_add(root, toks, val)
    if src == toks:
        walk(root, src)
        return root
    if len(toks) > len(src) and toks[: len(src)] == src:
        raise ModelErr("into itself")
    val = copy.deepcopy(walk(root, src))
    root = m_remove(root, src)
    return m_add(root, toks, val)


def m_merge(t, p):
    if not isinstance(p, dict):
        return copy.deepcopy(p)
    if not isinstance(t, dict):
        t = {}
    for k, v in p.items():
        if v is None:
            t.pop(k, None)
        else:
            t[k] = m_merge(t.get(k), v)
    return t


def ptr(s):
    if s == "":
        return []
    return [x.replace("~1", "/").replace("~0", "~") for x in s.split("/")[1:]]


def pstr(toks):
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in toks)


def paths(v, p=()):
    yield p
    if isinstance(v, dict):
        for k, x in v.items():
            yield from paths(x, p + (k,))
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield from paths(x, p + (str(i),))


def rows_diff(h):
    for d, doc in enumerate(h._p.docs):
        docs, _ = T.build(doc.text, parse_events(doc.text))
        fresh = docs[0]
        for name in ("kind", "ps", "st", "en", "sz", "sty", "anc", "tag", "val"):
            a, b = getattr(doc, name), getattr(fresh, name)
            if a != b:
                i = next(
                    (i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b))
                )
                return f"doc {d}: {name} differs at row {i}: have {a[i : i + 3]} fresh {b[i : i + 3]} (len {len(a)} vs {len(b)})"
    return None


def tainted(doc, toks):
    """True when the node, or an ancestor of it, carries an anchor (an edit changes its aliases)."""
    try:
        loc = E.locate(doc, toks)
    except PatchError:
        if not toks:
            return False
        return tainted(doc, toks[:-1])
    return any(doc.anc[x] is not None for x in [*loc.anc, loc.row]) or loc.alias


def via_alias(doc, toks):
    try:
        return E.locate(doc, toks).alias
    except PatchError:
        return False


def randval(r):
    return plain(rval(r))


def make_op(r, v):
    ps = list(paths(v))
    p = r.choice(ps)
    node = walk(v, p)
    kinds = ["replace", "replace", "add", "add", "remove", "copy", "move", "test", "merge"]
    k = r.choice(kinds)
    if k == "merge":
        return ("merge", randmerge(r, v, 0))
    path = list(p)
    if k == "add":
        if isinstance(node, dict):
            path = list(p) + [r.choice(["n1", "kx", "new key", "z"])]
        elif isinstance(node, list):
            path = list(p) + [r.choice(["-"] + [str(i) for i in range(len(node) + 1)] + ["9"])]
        return ("op", {"op": "add", "path": pstr(path), "value": randval(r)})
    if k == "replace":
        return ("op", {"op": "replace", "path": pstr(path), "value": randval(r)})
    if k == "remove":
        return ("op", {"op": "remove", "path": pstr(path)})
    if k == "test":
        val = copy.deepcopy(node)
        if r.random() < 0.3:
            val = [val]
        return ("op", {"op": "test", "path": pstr(path), "value": val})
    q = r.choice(ps)
    dst = list(q)
    tn = walk(v, q)
    if r.random() < 0.6:
        if isinstance(tn, dict):
            dst = dst + [r.choice(["n1", "kx", "z"])]
        elif isinstance(tn, list):
            dst = dst + [r.choice(["-", "0"])]
    return ("op", {"op": k, "from": pstr(path), "path": pstr(dst)})


def randmerge(r, v, d):
    c = r.random()
    if d >= 2 or c < 0.4 or not isinstance(v, dict):
        if d == 0 and not isinstance(v, dict):
            return randval(r)
        return r.choice([None, None, randval(r)])
    out = {}
    for _ in range(r.randint(1, 3)):
        k = r.choice(list(v) + ["n1", "z"])
        out[k] = (
            randmerge(r, v.get(k), d + 1)
            if isinstance(v.get(k), dict)
            else r.choice([None, randval(r)])
        )
    return out


def docs_values(h):
    return [h.value(d, []) for d in range(h.count())]


def run(seed, nops=6):
    """One random stream and ``nops`` random edits; returns a description of a failure or None."""
    r = random.Random(seed)
    text = gen(r)
    try:
        h = parse(text, L)
        vals = [plain_load(x) for x in _pure.load(text, L, True)]
    except YAMLError:
        return None
    if len(vals) != h.count():
        return f"seed {seed}: doc count {len(vals)} vs {h.count()}\n{text!r}"
    for n in range(nops):
        d = r.randrange(len(vals))
        v = vals[d]
        before = h.emit()
        doc = h._p.docs[d]
        texts = [x.text for x in h._p.docs]
        what, spec = make_op(r, v)
        if what == "merge":
            ops = None
        elif r.random() < 0.35:
            ops = [spec]
            for _ in range(r.randint(1, 3)):
                ops.append(more_op(r, v))
        else:
            ops = [spec]
        try:
            # model
            exp = copy.deepcopy(v)
            err = None
            try:
                if what == "merge":
                    exp = m_merge(exp, spec)
                else:
                    for op in ops:
                        exp = m_apply(exp, op)
            except ModelErr as e:
                err = e
            # the sequence of single ops on a copy, for the one-patch comparison
            tw = False
            if what == "merge":
                tw = any(a is not None for a in doc.anc)
            else:
                for op in ops:
                    if op["op"] in ("test",):
                        continue
                    if op["op"] == "move" and ptr(op["from"]) == ptr(op["path"]):
                        continue
                    for key in ("path", "from"):
                        if (
                            key in op
                            and tainted(doc, ptr(op[key]))
                            and (key == "path" or op["op"] == "move")
                        ):
                            tw = True
            alias_write = False
            if what == "op":
                for op in ops[:1]:
                    if op["op"] == "move" and ptr(op["from"]) == ptr(op["path"]):
                        continue
                    if op["op"] in ("add", "replace", "remove", "move"):
                        toks = ptr(op["path"])
                        if op["op"] == "add":
                            toks = toks[:-1]
                        if via_alias(doc, toks):
                            alias_write = True
            try:
                if what == "merge":
                    h.merge_patch(d, spec)
                else:
                    h.patch(d, ops)
                res = None
            except PatchError as e:
                res = e
            if res is not None and tw:
                if h.emit() != before:
                    return f"seed {seed}: failed patch changed the text"
                continue
            if res is not None:
                if h.emit() != before:
                    return f"seed {seed}: refused/failed patch changed the text ({res})\nops {ops or spec}\nBEFORE {before.decode()!r}\nAFTER {h.emit().decode()!r}"
                if (
                    err is None
                    and res.category not in ("refused-alias", "refused-ambiguous", "limit")
                    and not (res.category == "patch-invalid" and "implicit pair" in str(res))
                ):
                    return f"seed {seed}: patch failed but the model succeeded: {res.category} {res}\nops {ops or spec}\nDOC {before.decode()!r}"
                REFUSALS[
                    (res.category, str(res)[:40] if res.category == "patch-invalid" else "")
                ] += 1
                if (
                    res.category == "refused-alias"
                    and err is None
                    and not (alias_write or tw or doc_has_alias(doc))
                ):
                    return f"seed {seed}: refused-alias without aliases: {res}\nops {ops or spec}\nDOC {before.decode()!r}"
                continue
            if err is not None:
                return f"seed {seed}: patch succeeded but the model failed ({err})\nops {ops or spec}\nBEFORE {before.decode()!r}\nAFTER {h.emit().decode()!r}"
            if alias_write:
                return f"seed {seed}: write through an alias was not refused\nops {ops}\nBEFORE {before.decode()!r}\nAFTER {h.emit().decode()!r}"
            out = h.emit()
            try:
                got = [plain_load(x) for x in _pure.load(out.decode(), L, True)]
            except Exception as e:
                return f"seed {seed}: output does not parse: {e}\nops {ops or spec}\nBEFORE {before.decode()!r}\nAFTER {out.decode()!r}"
            if len(got) != len(vals):
                return f"seed {seed}: document count changed\nBEFORE {before.decode()!r}\nAFTER {out.decode()!r}"
            if tw:
                vals = got
            else:
                want = list(vals)
                want[d] = exp
                for i, (a, b) in enumerate(zip(got, want)):
                    if not same(a, b):
                        return f"seed {seed}: value mismatch doc {i} after {ops or spec}\nBEFORE {before.decode()!r}\nAFTER {out.decode()!r}\nwant {b!r}\ngot  {a!r}"
                vals = want
            # other documents' text must be untouched
            for i, old_text in enumerate(texts):
                if i != d and h._p.docs[i].text != old_text:
                    return f"seed {seed}: an edit of document {d} changed document {i}"
            c = rows_diff(h)
            if c:
                return f"seed {seed}: rows inconsistent after {ops or spec}: {c}\nBEFORE {before.decode()!r}\nAFTER {out.decode()!r}"
            # one patch equals the same operations one by one
            if what == "op" and len(ops) > 1:
                h2 = parse(before, L)
                for op in ops:
                    h2.patch(d, [op])
                if h2.emit() != out:
                    return f"seed {seed}: patch differs from one-by-one\nops {ops}\nBEFORE {before.decode()!r}\none  {out.decode()!r}\nsteps {h2.emit().decode()!r}"
            # an atomic failure: the same ops plus one that fails
            if what == "op":
                bad = ops + [{"op": "test", "path": "", "value": {"__nope__": 1}}]
                for target, last in ((h, None), (parse(before, L), len(bad) - 1)):
                    snap = target.emit()
                    try:
                        target.patch(d, bad)
                        return f"seed {seed}: failing patch succeeded"
                    except PatchError as e:
                        if last is not None and e.index != last:
                            return f"seed {seed}: the failure names operation {e.index}, not {last}"
                    if target.emit() != snap:
                        return f"seed {seed}: failing patch changed the text"
        except Exception:
            return (
                f"seed {seed}: CRASH ops={ops or spec}\n"
                + traceback.format_exc()
                + "\n"
                + repr(h.emit().decode())
            )
    return None


def doc_has_alias(doc):
    return any(a is not None for a in doc.anc)


def more_op(r, v):
    while True:
        w, s = make_op(r, v)
        if w == "op":
            return s


def plain_load(x):
    return plain(x)


if __name__ == "__main__":
    a = int(sys.argv[1])
    b = int(sys.argv[2])
    show = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    bad = 0
    for s in range(a, b):
        m = run(s)
        if m:
            bad += 1
            if bad <= show:
                print(m[:2000])
                print("---")
    print("bad", bad, "of", b - a)
    if "-v" in sys.argv:
        print(REFUSALS)
