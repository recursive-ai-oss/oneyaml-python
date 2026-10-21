"""Fail when files or commit messages of this public repository carry internal text.

Usage:
    python tools/publictext.py --tree              every tracked and untracked, not-ignored file
    python tools/publictext.py --commits RANGE     commit messages in a range ("HEAD" = all)
    python tools/publictext.py PATH...             the named files

The private terms are not written in this repository. They are read from the file
named by the environment variable PUBLICTEXT_PATTERNS: one regular expression per
line, blank lines and lines starting with "#" ignored. A line starting with
"prose:" is matched only in comments, Markdown and commit messages (for short
names that are ordinary identifiers in code); other lines match whole lines.

Two generic checks need no file: a 40-hex commit hash outside a line that states
provenance, and the marker "publictext:ok", which excuses the line it is on.

Without the pattern file the check fails in CI. Set PUBLICTEXT_ALLOW_MISSING=true
to run the generic checks only (fork pull requests have no secrets). Outside CI a
missing file is a notice.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath

HASH = re.compile(r"\b[0-9a-f]{40}\b")
PROVENANCE = re.compile(r"revision|provenance|uses:|reverts commit|rev\s*=", re.I)
OK_MARKER = "publictext:ok"
MAX_FILE = 1 << 20
SKIP_NAMES = {"Cargo.lock", "LICENSE-APACHE", "LICENSE-MIT"}
SKIP_PREFIXES = ("tests/data/yaml-test-suite/",)

HASH_STYLE = {".yml", ".yaml", ".sh", ".toml", ".py", ".cfg", ".ini", ".gitignore"}
SLASH_STYLE = {".rs", ".c", ".h", ".js", ".ts"}
PROSE_STYLE = {".md", ".txt", ".rst"}


def style_of(name: str) -> str:
    p = PurePosixPath(name)
    ext = p.suffix.lower()
    if ext in PROSE_STYLE:
        return "prose"
    if ext in SLASH_STYLE:
        return "slash"
    if ext in HASH_STYLE or p.name == ".gitignore" or ext == "":
        return "hash"
    return "other"


def comment_part(line: str, style: str, state: dict) -> str:
    if style == "prose":
        return line
    if style == "hash":
        i = line.find("#")
        return line[i:] if i >= 0 else ""
    if style == "slash":
        if state["block"]:
            i = line.find("*/")
            if i >= 0:
                state["block"] = False
                return line[:i]
            return line
        for i in range(len(line) - 1):
            if line[i] != "/":
                continue
            if line[i + 1] == "/" and (i == 0 or line[i - 1] != ":"):
                return line[i:]
            if line[i + 1] == "*":
                rest = line[i + 2 :]
                j = rest.find("*/")
                if j >= 0:
                    return rest[:j]
                state["block"] = True
                return rest
    return ""


class Checker:
    def __init__(self, patterns: str = ""):
        general, prose = [], []
        for n, line in enumerate(patterns.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            target = general
            if line.startswith("prose:"):
                line, target = line[len("prose:") :].strip(), prose
            try:
                re.compile(line)
            except re.error as exc:
                raise ValueError(f"pattern line {n}: {exc}") from exc
            target.append(line)
        self.general = re.compile("|".join(general)) if general else None
        self.prose = re.compile("|".join(prose)) if prose else None

    def empty(self) -> bool:
        return self.general is None and self.prose is None

    def check_text(self, label, text, style, with_hash):
        out = []
        state = {"block": False}
        for n, line in enumerate(text.split("\n"), 1):
            comment = comment_part(line, style, state)
            if OK_MARKER in line:
                continue
            match = None
            hit = (self.general and self.general.search(line)) or (
                self.prose and self.prose.search(comment)
            )
            if hit:
                match = hit.group(0)
            elif with_hash and not PROVENANCE.search(line):
                target = line if style == "other" else comment
                if HASH.search(target):
                    match = "commit hash outside a provenance line"
            if match:
                out.append(f"{label}:{n}: {match!r}: {line.strip()[:100]}")
        return out

    def check_file(self, name: str, data: bytes):
        if name.startswith(SKIP_PREFIXES) or PurePosixPath(name).name in SKIP_NAMES:
            return []
        if len(data) > MAX_FILE or b"\0" in data[:4096]:
            return []
        with_hash = not (name.startswith("tests/data/") or "/testdata/" in name)
        text = data.decode("utf-8", errors="replace")
        return self.check_text(name, text, style_of(name), with_hash)

    def check_message(self, label, msg):
        return self.check_text(label, msg, "prose", True)


def git(root, *args):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode().strip()}")
    return r.stdout


def load_checker():
    file = os.environ.get("PUBLICTEXT_PATTERNS", "")
    if file:
        with open(file, encoding="utf-8") as f:
            checker = Checker(f.read())
        if checker.empty():
            sys.exit(f"publictext: {file} holds no patterns")
        return checker
    if os.environ.get("CI") == "true" and os.environ.get("PUBLICTEXT_ALLOW_MISSING") != "true":
        sys.exit(
            "publictext: PUBLICTEXT_PATTERNS is not set; "
            "the check cannot run in CI without its patterns"
        )
    print("publictext: skipping the pattern check: PUBLICTEXT_PATTERNS is not set", file=sys.stderr)
    return Checker()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="public-text check")
    ap.add_argument("--tree", action="store_true")
    ap.add_argument("--commits", metavar="RANGE")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args(argv)
    if not (args.tree or args.commits or args.paths):
        ap.print_usage(sys.stderr)
        return 2
    checker = load_checker()
    root = git(".", "rev-parse", "--show-toplevel").decode().strip()
    names = []
    if args.tree:
        out = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        names += [n for n in out.decode().split("\0") if n]
    for p in args.paths:
        names.append(os.path.relpath(os.path.abspath(p), root).replace(os.sep, "/"))
    findings, files, seen = [], 0, set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        try:
            with open(os.path.join(root, n), "rb") as f:
                data = f.read()
        except OSError:
            continue  # a directory, a submodule or a vanished file
        files += 1
        findings += checker.check_file(n, data)
    if args.commits:
        spec = args.commits
        shas = git(root, "rev-list", spec).decode().split()
        for sha in shas:
            msg = git(root, "show", "-s", "--format=%B", sha).decode("utf-8", "replace")
            findings += checker.check_message(f"commit {sha[:7]}", msg)
    for f in findings:
        print(f)
    if findings:
        print(f"publictext: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"publictext: {files} files checked, clean", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
