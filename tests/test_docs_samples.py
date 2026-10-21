"""The code samples in README.md and docs/ run, and print what the page shows.

Every ```python block is run as a script of its own in a fresh interpreter, once per backend
(ONEYAML_BACKEND=native and pure), so a sample sees exactly what a reader's program would. When
a ```text block follows it, with only blank lines between, its content is the expected output and
must match stdout exactly. Shell (```sh) and diff (```diff) blocks are not run.

The same files are checked for links that lead nowhere (a relative path, or a link to this
repository on GitHub, whose file or #heading does not exist) and for generated blocks that are
not closed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
# Pages that are not run for samples, but whose links are checked.
EXTRA_LINKED = [
    ROOT / name for name in ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", "CODE_OF_CONDUCT.md")
]
REPO_URL = "https://github.com/recursive-ai-oss/oneyaml-python/blob/main/"
BACKENDS = tuple(os.environ.get("ONEYAML_TEST_BACKENDS", "native,pure").split(","))

FENCE = re.compile(r"^```([\w-]*)\s*$")


def blocks(path: Path) -> list[tuple[int, str, str, int]]:
    """``(line, info, content, end)`` of each fenced block, in order: the 1-based line of the
    opening fence, its info string, the text inside, and the 0-based index of the closing fence."""
    out = []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    i = 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if m is None:
            i += 1
            continue
        start, info, body = i + 1, m.group(1), []
        i += 1
        while i < len(lines) and lines[i].rstrip("\n") != "```":
            body.append(lines[i])
            i += 1
        assert i < len(lines), f"{path.name}:{start}: code block is not closed"
        out.append((start, info, "".join(body), i))
        i += 1
    return out


def samples() -> list:
    params = []
    for page in PAGES:
        found = blocks(page)
        text_lines = page.read_text(encoding="utf-8").splitlines()
        for n, (start, info, body, end) in enumerate(found):
            if info != "python":
                continue
            expected = None
            if n + 1 < len(found) and found[n + 1][1] == "text":
                between = text_lines[end + 1 : found[n + 1][0] - 1]
                if all(not line.strip() for line in between):
                    expected = found[n + 1][2]
            name = f"{page.relative_to(ROOT)}:{start}"
            for backend in BACKENDS:
                params.append(pytest.param(body, expected, backend, id=f"{name}-{backend}"))
    return params


@pytest.mark.parametrize(("code", "expected", "backend"), samples())
def test_sample_runs_and_prints_what_the_page_shows(code, expected, backend, tmp_path):
    env = {**os.environ, "ONEYAML_BACKEND": backend, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    script = tmp_path / "sample.py"
    script.write_text(code, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    if expected is not None:
        assert run.stdout == expected


def test_the_runner_finds_the_samples():
    # A change to the fence format would otherwise skip every sample and pass.
    assert any(info == "python" for page in PAGES for _, info, _, _ in blocks(page))


# ---- links and generated blocks ----------------------------------------------------------------


def page_id(page: Path) -> str:
    return str(page.relative_to(ROOT))


LINK = re.compile(r"\]\(([^)\s]+)\)|\b(?:href|src|srcset)=\"([^\"]+)\"")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).lower()
    kept = []
    for ch in text:
        if ch in " -_" or unicodedata.category(ch)[0] in "LN":
            kept.append("-" if ch == " " else ch)
    return "".join(kept)


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else HEADING.match(line)
        if m:
            base = slug(m.group(1))
            n = seen.get(base, 0)
            seen[base] = n + 1
            out.add(base if n == 0 else f"{base}-{n}")
    return out


def links(path: Path):
    in_code = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in LINK.finditer(line):
            yield number, m.group(1) or m.group(2)


@pytest.mark.parametrize("page", PAGES + [p for p in EXTRA_LINKED if p.exists()], ids=page_id)
def test_links_lead_somewhere(page):
    problems = []
    for number, target in links(page):
        if target.startswith(REPO_URL):
            local = ROOT / target[len(REPO_URL) :]
        elif target.startswith(("http://", "https://", "mailto:")):
            continue
        elif target.startswith("#"):
            local = page.parent / (page.name + target)
        else:
            local = page.parent / target
        file_part, _, anchor = str(local).partition("#")
        file_path = Path(unquote(file_part))
        if not file_path.exists():
            problems.append(f"{page.name}:{number}: {target}: no such file")
        elif anchor and file_path.suffix == ".md" and anchor not in anchors(file_path):
            problems.append(f"{page.name}:{number}: {target}: no such heading")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("page", PAGES, ids=page_id)
def test_generated_blocks_are_closed(page):
    open_kind = None
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("<!-- generated:"):
            assert open_kind is None, f"{page.name}:{number}: block inside {open_kind}"
            open_kind = stripped
        elif stripped == "<!-- end generated -->":
            assert open_kind is not None, f"{page.name}:{number}: end without a start"
            open_kind = None
    assert open_kind is None, f"{page.name}: {open_kind} is not closed"
