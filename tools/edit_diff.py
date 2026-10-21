"""Seeded differential of the two backends' edits over the yaml-test-suite and the kit's own inputs.

Every input both backends parse gets edits made alike on each: set, insert, remove, RFC 6902
patches and RFC 7396 merge patches, once each on a fresh parse (``single``) and as runs of several
in a row (``run``). A disagreement is an edit one backend refused or both did with different
bytes. Disagreements are grouped by a signature (the operation and the shape of the first
differing lines) so that a class can be read once and counted.

Usage:
    python tools/edit_diff.py --kit PATH/TO/KIT [--seeds 3] [--run-length 4] [--classes]
                              [--show SIGNATURE] [--json OUT.json]

Needs both backends (the native module installed). Deterministic: the same arguments and
sources give the same numbers and the same digest.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import json
import random
import re
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from oneyaml import Limits

LIMITS = Limits()
CAP = 120  # nodes considered per document
SCALARS = [12, "n", "zé", True, None, "two\nlines\n", 3.5, "yes", ""]


def res(f, *args):
    try:
        return ("ok", f(*args))
    except Exception as exc:  # noqa: BLE001 - the two backends' failures are compared
        return ("err", f"{type(exc).__name__}:{getattr(exc, 'category', '')}")


def outcome(fn, h):
    """Make the edit on ``h`` and return the document's bytes, or the failure."""
    return res(lambda: (fn(h), bytes(h.emit()))[1])


def paths_of(h, d):
    """Paths of document ``d`` breadth first, aliases not followed, capped."""
    out = [[]]
    queue = [[]]
    while queue and len(out) < CAP:
        p = queue.pop(0)
        info = res(h.info, d, p)
        if info[0] != "ok" or info[1][0] == "alias":
            continue
        kind = res(h.kind, d, p)
        if kind[0] != "ok":
            continue
        if kind[1] == "mapping":
            keys = res(h.keys, d, p)
            if keys[0] != "ok":
                continue
            kids = [p + [k] for k in keys[1]]
        elif kind[1] == "sequence":
            kids = [p + [str(i)] for i in range(h.length(d, p))]
        else:
            continue
        out += kids
        queue += kids
    return out


def pointer(path):
    """An RFC 6901 pointer for a path of text tokens, or None for a collection key."""
    parts = []
    for t in path:
        if not isinstance(t, str):
            return None
        parts.append(t.replace("~", "~0").replace("/", "~1"))
    return "".join("/" + p for p in parts)


def make_op(rng, h, d):
    """One random edit as (name, function of a handle), chosen from the native handle's state."""
    ps = paths_of(h, d)
    p = rng.choice(ps)
    kind = res(h.kind, d, p)[1] if res(h.kind, d, p)[0] == "ok" else "scalar"
    ptr = pointer(p)
    choice = rng.choice(
        ["set", "set-map", "set-seq", "insert", "remove", "add", "replace", "p-remove",
         "move", "copy", "test", "merge"]
    )  # fmt: skip
    if choice == "set":
        v = rng.choice(SCALARS)
        return f"set {v!r}", lambda h: h.set(d, p, v)
    if choice == "set-map":
        return "set-map", lambda h: h.set(d, p, {"q": [1, "zé"]})
    if choice == "set-seq":
        return "set-seq", lambda h: h.set(d, p, [1, {"a": "b"}])
    if choice == "insert":
        if kind == "sequence":
            at = rng.choice([0, h.length(d, p)])
            return f"insert@{'0' if at == 0 else 'end'}", lambda h: h.insert(d, p, at, "iñ")
        if kind == "mapping":
            return "insert-key", lambda h: h.set(d, p + ["nk"], "n")
        return "set-n", lambda h: h.set(d, p, "n")
    if choice == "remove":
        return "remove", lambda h: h.remove(d, p) if p else None
    if ptr is None:
        return "skip", lambda h: None
    if choice == "add":
        if kind == "sequence":
            to = ptr + "/-"
        elif kind == "mapping":
            to = ptr + "/nk"
        else:
            to = ptr
        v = rng.choice(SCALARS)
        return f"p-add {v!r}", lambda h: h.patch(d, [{"op": "add", "path": to, "value": v}])
    if choice == "replace":
        return "p-replace", lambda h: h.patch(
            d, [{"op": "replace", "path": ptr, "value": {"k": [1, 2]}}]
        )
    if choice == "p-remove":
        return "p-remove", lambda h: h.patch(d, [{"op": "remove", "path": ptr}])
    if choice == "test":
        # A patch value is JSON: a document value with other keys or types cannot be written.
        try:
            want = json.loads(json.dumps(h.value(d, p), allow_nan=False))
        except Exception:  # noqa: BLE001 - any failure means: skip the operation
            return "skip", lambda h: None
        ops = [
            {"op": "test", "path": ptr, "value": want},
            {"op": "add", "path": ptr or "", "value": 1},
        ]
        return "p-test+add", lambda h: h.patch(d, ops)
    q = rng.choice(ps)
    qptr = pointer(q)
    if qptr is None:
        return "skip", lambda h: None
    if choice == "move":
        return "p-move", lambda h: h.patch(d, [{"op": "move", "from": ptr, "path": qptr + "/mv"}])
    if choice == "copy":
        return "p-copy", lambda h: h.patch(d, [{"op": "copy", "from": ptr, "path": qptr + "/cp"}])
    if kind == "mapping" and p == []:
        ks = res(h.keys, d, [])[1]
        gone = {str(ks[0]): None} if isinstance(ks, list) and ks and isinstance(ks[0], str) else {}
        return "merge", lambda h: h.merge_patch(d, {**gone, "mp": {"a": 1, "b": None}})
    return "merge-root", lambda h: h.merge_patch(d, {"mp": {"a": 1, "b": None}})


def signature(op, a, b):
    """The operation and the shape of the first differing lines, words and digits abstracted."""
    if a[0] != b[0]:
        return f"{op.split()[0]} | {'native' if a[0] == 'err' else 'pure'} refuses"
    if a[0] == "err":
        return f"{op.split()[0]} | errors {a[1]} / {b[1]}"
    la = a[1].decode("utf-8", "replace").splitlines()
    lb = b[1].decode("utf-8", "replace").splitlines()
    out = []
    for line in difflib.unified_diff(la, lb, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(re.sub(r"\d+", "9", re.sub(r"[^\W\d_]+", "w", line)))
        if len(out) == 2:
            break
    if not out:
        out = ["only line breaks differ"]
    return f"{op.split()[0]} | " + " / ".join(out)


def first_diff(a: bytes, b: bytes):
    """The first differing line of two outputs, as a pair of byte strings (None when equal)."""
    la, lb = a.splitlines(), b.splitlines()
    for x, y in zip(la, lb, strict=False):
        if x != y:
            return x, y
    if len(la) != len(lb):
        return (
            la[len(lb)] if len(la) > len(lb) else b"",
            lb[len(la)] if len(lb) > len(la) else b"",
        )
    return None


def bucket(op, before, a, b):
    """A named cause for a disagreement, by rules read off the examples; "other" if none fits.
    The rules are checks on the two outputs, not a statement of which side is right."""
    if a[0] == "err" and b[0] == "ok":
        return (
            "native refuses, tab-led or empty-marker root"
            if (before[:1] in (b"\t", b"-") or before.lstrip().startswith((b"---", b"\t")))
            else "native refuses"
        )
    if a[0] == "ok" and b[0] == "err":
        return "pure refuses (" + b[1] + ")"
    if a[0] == "err":
        return "both refuse, other category"
    na, nb = a[1], b[1]
    d = first_diff(na, nb)
    if d is None:
        return "line breaks only" if na != nb else "same"
    x, y = d
    if na.count(b"\n\n") > nb.count(b"\n\n") and na.replace(b"\n\n", b"\n") == nb.replace(
        b"\n\n", b"\n"
    ):
        return "native adds a blank line between entries"
    if (
        (b"{" in y or b"[" in y)
        and not (b"{" in x or b"[" in x)
        and op.split()[0] in ("set-map", "set-seq", "p-replace", "merge-root", "merge")
    ):
        return "flow collection replaced: native block, pure flow"
    if y.lstrip().startswith((b"|", b">", b"--- |", b"--- >")) and not x.lstrip().startswith(
        (b"|", b">", b"--- |", b"--- >")
    ):
        return "block scalar set to text: native plain, pure keeps block"
    if b"!" in y and b"!" not in x:
        return "tag: native drops, pure keeps"
    if x.lstrip().startswith(b"#") or y.lstrip().startswith(b"#") or b" #" in x + y:
        return "comment placement"
    if len(x) - len(x.lstrip(b" ")) != len(y) - len(y.lstrip(b" ")):
        return "indentation of new block text"
    if x.strip() in (b"{ }", b"}", b"]", b"{", b"[") or y.strip() in (b"{}", b"[]"):
        return "empty collection after removal: spacing"
    if (b"'" in x) != (b"'" in y) or (b'"' in x) != (b'"' in y):
        return "quote style"
    if x.strip() == b"" or y.strip() == b"":
        return "whitespace-only line"
    return "other"


def inputs(kit: Path):
    out = []
    for f in sorted((kit / "suites" / "yaml-test-suite").glob("data-*/**/in.yaml")):
        out.append(("yts/" + f.parent.relative_to(f.parents[1]).as_posix(), f.read_bytes()))
    for f in sorted((kit / "suites" / "own" / "cases").glob("**/input.yaml")):
        out.append(("own/" + f.parent.name, f.read_bytes()))
    return out


def run_input(tag, data, seeds, run_length, native, pure, found, digest, stats):
    try:
        hn, hp = native.parse(data, LIMITS), pure.parse(data, LIMITS)
    except Exception:  # noqa: BLE001 - parse differences belong to the parser tests
        return
    stats["inputs"] += 1
    if hn.count() == 0:
        return
    for mode in ("single", "run"):
        # Single edits are the cleaner evidence, so they get more seeds than the runs.
        for seed in range(seeds * (4 if mode == "single" else 1)):
            rng = random.Random(zlib.crc32(f"{tag}/{mode}".encode()) ^ seed)
            hn, hp = native.parse(data, LIMITS), pure.parse(data, LIMITS)
            for step in range(1 if mode == "single" else run_length):
                if mode == "single":
                    hn, hp = native.parse(data, LIMITS), pure.parse(data, LIMITS)
                before = bytes(hn.emit())
                d = rng.randrange(hn.count())
                state = rng.getstate()
                op, fn = make_op(rng, hn, d)
                if op == "skip":
                    continue
                stats[mode] += 1
                a = outcome(fn, hn)
                rng.setstate(state)
                op2, fn2 = make_op(rng, hp, d)
                b = outcome(fn2, hp)
                digest.update(repr((tag, mode, seed, step, op, a, b)).encode("utf-8", "replace"))
                if a != b:
                    sig = signature(op, a, b)
                    found[(mode, sig)].append((tag, seed, step, op, before, a, b))
                    stats[mode + "-differ"] += 1
                    break  # the states differ from here on


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kit", required=True, help="the kit checkout")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--run-length", type=int, default=4)
    ap.add_argument("--classes", action="store_true", help="list the classes with counts")
    ap.add_argument("--show", help="print examples of the classes whose signature contains this")
    ap.add_argument("--bucket-show", help="print examples of the bucket with this name")
    ap.add_argument("--only", help="only inputs whose name contains this")
    ap.add_argument("--buckets", action="store_true", help="count single edits by named cause")
    ap.add_argument(
        "--compact",
        type=int,
        metavar="N",
        help="one short example per class of N or more single edits",
    )
    ap.add_argument("--json", help="write every disagreement's smallest example per class")
    args = ap.parse_args()
    native = importlib.import_module("oneyaml._native")
    pure = importlib.import_module("oneyaml._pure")
    found: dict = defaultdict(list)
    stats: Counter = Counter()
    digest = hashlib.sha256()
    for tag, data in inputs(Path(args.kit)):
        if args.only and args.only not in tag:
            continue
        run_input(tag, data, args.seeds, args.run_length, native, pure, found, digest, stats)
    print(dict(stats))
    print("digest", digest.hexdigest()[:16])
    ranked = sorted(found.items(), key=lambda kv: -len(kv[1]))
    if args.bucket_show:
        seen = set()
        for (mode, _sig), items in found.items():
            if mode != "single":
                continue
            for tag, _seed, _step, op, before, a, b in sorted(items, key=lambda i: len(i[4])):
                if bucket(op, before, a, b) == args.bucket_show and (tag, op) not in seen:
                    seen.add((tag, op))
                    if len(seen) <= 14:
                        print(f"-- {tag} {op}\n   in  {before[:130]!r}")
                        print(f"   nat {(a[1] if a[0] == 'ok' else a)!r:.130}")
                        print(f"   pur {(b[1] if b[0] == 'ok' else b)!r:.130}")
    if args.buckets:
        counts: Counter = Counter()
        for (mode, _sig), items in found.items():
            if mode == "single":
                for _tag, _seed, _step, op, before, a, b in items:
                    counts[bucket(op, before, a, b)] += 1
        for name, n in counts.most_common():
            print(f"{n:5d} {name}")
    if args.classes or not (args.show or args.compact or args.buckets or args.bucket_show):
        for (mode, sig), items in ranked:
            print(f"{len(items):5d} {mode:6s} {sig}")
    if args.compact:
        for (mode, sig), items in ranked:
            if mode != "single" or len(items) < args.compact:
                continue
            tag, seed, step, op, data, a, b = min(items, key=lambda i: len(i[4]))
            short = lambda v: repr(v if v[0] != "ok" else v[1])[:150]  # noqa: E731
            print(f"{len(items)} {sig!r}\n  {tag} {op}\n  in  {data[:150]!r}")
            print(f"  nat {short(a)}\n  pur {short(b)}")
    if args.show:
        for (mode, sig), items in ranked:
            if args.show not in sig:
                continue
            tag, seed, step, op, data, a, b = min(items, key=lambda i: len(i[4]))
            print(f"\n== {len(items)} x {mode} {sig}\n   {tag} seed {seed} step {step} {op}")
            print("   input   ", data[:300])  # the document the edit was made on
            print("   native  ", a[1] if a[0] == "ok" else a)
            print("   pure    ", b[1] if b[0] == "ok" else b)
    if args.json:
        out = {
            f"{mode} | {sig}": {
                "count": len(items),
                "example": min(items, key=lambda i: len(i[4]))[:4]
                + (min(items, key=lambda i: len(i[4]))[4].decode("utf-8", "replace"),),
            }
            for (mode, sig), items in ranked
        }
        Path(args.json).write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
