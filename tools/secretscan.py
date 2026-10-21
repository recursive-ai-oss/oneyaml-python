"""Look for secrets and unwanted blobs in what a push would send, or in the tree.

Usage:
    python tools/secretscan.py --range RANGE   blobs reachable from RANGE ("HEAD" = all)
    python tools/secretscan.py --tree          tracked and untracked, not-ignored files

Findings name the path and kind, never the matched value. Flags: secret shapes,
ELF / Mach-O binaries and core dumps, blobs over 1 MiB, and tracked files with the
executable bit that are not listed in EXECUTABLE_OK.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

MAX_BLOB = 1 << 20
# Files that are meant to carry the executable bit. None: tools run as `python tools/x.py`.
EXECUTABLE_OK: frozenset[str] = frozenset()


def _frag(*parts: bytes) -> bytes:
    return b"".join(parts)


# Shapes only: each needs a long run of key characters, so these lines do not match themselves.
SHAPES = {
    "anthropic key": rb"sk-ant-[A-Za-z0-9_-]{20,}",
    "openai key": rb"sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{40,}",
    "aws access key id": rb"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "google api key": rb"AIza[0-9A-Za-z_-]{35}",
    "github token": rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}",
    "gitlab token": rb"glpat-[A-Za-z0-9_-]{20,}",
    "slack token": rb"xox[abprs]-[A-Za-z0-9-]{10,}",
    "crates.io token": rb"cio[A-Za-z0-9]{28,}",
    "elevenlabs key": rb"sk_[a-f0-9]{40,}",
    "firecrawl key": rb"fc-[a-f0-9]{30,}",
    "private key": rb"-----BEGIN [A-Z ]*" + _frag(b"PRI", b"VATE KEY-----"),
    "secret assignment": (
        rb"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN)\s*=\s*['\"]?[A-Za-z0-9/+_.-]{16,}"
    ),
}
COMPILED = {k: re.compile(v) for k, v in SHAPES.items()}
DOCUMENTED_EXAMPLES = (b"AKIAIOSFODNN7EXAMPLE", b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
MAGIC = {
    "ELF binary or core dump": (b"\x7fELF",),
    "Mach-O binary": (
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
    ),
}


def inspect(size: int, body: bytes) -> list[str]:
    if size > MAX_BLOB:
        return [f"larger than 1 MiB ({size} bytes)"]
    for kind, magics in MAGIC.items():
        if body.startswith(magics):
            return [kind]
    for ex in DOCUMENTED_EXAMPLES:
        body = body.replace(ex, b"")
    return [kind for kind, rx in COMPILED.items() if rx.search(body)]


def git(*args: str, data: bytes | None = None) -> bytes:
    r = subprocess.run(["git", *args], input=data, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode().strip()}")
    return r.stdout


def scan_range(rev: str) -> tuple[list[str], int]:
    paths: dict[str, str] = {}
    for line in git("rev-list", "--objects", rev).decode().splitlines():
        sha, _, p = line.partition(" ")
        if sha:
            paths.setdefault(sha, p)
    if not paths:
        return [], 0
    info = git(
        "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        data="\n".join(paths).encode(),
    ).decode().splitlines()  # fmt: skip
    findings, n = [], 0
    for sha, typ, size in (line.split() for line in info):
        if typ != "blob":
            continue
        n += 1
        size = int(size)
        body = b"" if size > MAX_BLOB else git("cat-file", "blob", sha)
        findings += [f"{paths[sha]} ({sha[:12]}): {k}" for k in inspect(size, body)]
    # Executable bits live in trees, not blobs: check the tip of the range.
    tip = rev.split("..")[-1]
    for line in git("ls-tree", "-r", tip).decode().splitlines():
        mode, _, rest = line.partition(" ")
        name = rest.split("\t", 1)[1]
        if mode == "100755" and name not in EXECUTABLE_OK:
            findings.append(f"{name}: executable bit set")
    return findings, n


def scan_tree() -> tuple[list[str], int]:
    root = git("rev-parse", "--show-toplevel").decode().strip()
    out = git("-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    findings, n = [], 0
    for name in filter(None, out.decode().split("\0")):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        n += 1
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            body = f.read(MAX_BLOB + 1)
        findings += [f"{name}: {k}" for k in inspect(size, body)]
        if os.access(path, os.X_OK) and name not in EXECUTABLE_OK:
            findings.append(f"{name}: executable bit set")
    return findings, n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="secret and blob scan")
    ap.add_argument("--range", dest="rev")
    ap.add_argument("--tree", action="store_true")
    args = ap.parse_args(argv)
    if not (args.rev or args.tree):
        ap.print_usage(sys.stderr)
        return 2
    findings, n = [], 0
    if args.tree:
        f, c = scan_tree()
        findings, n = findings + f, n + c
    if args.rev:
        f, c = scan_range(args.rev)
        findings, n = findings + f, n + c
    for f in findings:
        print(f)
    if findings:
        print(f"secretscan: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"secretscan: {n} files/blobs scanned, clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
