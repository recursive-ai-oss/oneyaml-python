"""Hand-over gate: lint, native build, tests on both backends, guards. Prints step times.

Usage: python tools/gate.py

Environment:
    GATE_WORK   scratch directory for the venv, build output and wheels (required: nothing
                is written into the repository)
    GATE_PYTHON interpreter version for the venv (default 3.13; e.g. 3.14t)
    PUBLICTEXT_PATTERNS  pattern file for the public-text step (generic checks only if unset)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    work = os.environ.get("GATE_WORK")
    if not work:
        print("gate: set GATE_WORK to a scratch directory outside the repository", file=sys.stderr)
        return 2
    work = Path(work).resolve()
    if ROOT in work.parents or work == ROOT:
        print("gate: GATE_WORK must be outside the repository", file=sys.stderr)
        return 2
    pyver = os.environ.get("GATE_PYTHON", "3.13")
    venv = work / "venv"
    wheels = work / "wheels-dev"
    bin_ = venv / ("Scripts" if os.name == "nt" else "bin")
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(work / "target"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "VIRTUAL_ENV": str(venv),
    }
    env.pop("ONEYAML_BACKEND", None)
    py = str(bin_ / "python")
    tool = lambda n: str(bin_ / n)  # noqa: E731

    def tests(backend: str | None):
        e = dict(env)
        if backend:
            e["ONEYAML_BACKEND"] = backend
        return lambda: run([py, "-m", "pytest", "-q", "-n", "auto", "-p", "no:cacheprovider"], e)

    def run(cmd, e=env, cwd=ROOT):
        return subprocess.run(cmd, env=e, cwd=cwd).returncode == 0

    def venv_setup():
        if not (venv / "pyvenv.cfg").exists() and not run(
            ["uv", "venv", "-q", "-p", pyver, str(venv)]
        ):
            return False
        return run(["uv", "pip", "install", "-q", "--group", "dev"])

    def build():
        shutil.rmtree(wheels, ignore_errors=True)
        ok = run([tool("maturin"), "build", "-q", "--profile", "dev", "-o", str(wheels),
                  "-i", py])  # fmt: skip
        whl = next(wheels.glob("*.whl"), None)
        return (
            ok
            and whl is not None
            and run(["uv", "pip", "install", "-q", "--force-reinstall", "--no-deps", str(whl)])
        )

    def guards_env():
        e = dict(env)
        if not e.get("PUBLICTEXT_PATTERNS"):
            e["PUBLICTEXT_ALLOW_MISSING"] = "true"
        return e

    steps = [
        ("venv + dev tools", venv_setup),
        ("ruff check", lambda: run([tool("ruff"), "check", "--no-cache", "."])),
        ("ruff format", lambda: run([tool("ruff"), "format", "--check", "--no-cache", "."])),
        ("cargo fmt", lambda: run(["cargo", "fmt", "--check"], cwd=ROOT / "native")),
        (
            "cargo clippy",
            lambda: run(
                ["cargo", "clippy", "-q", "--profile", "dev", "--", "-D", "warnings"],
                cwd=ROOT / "native",
            ),
        ),  # fmt: skip
        ("native build + install", build),
        ("tests, native backend", tests("native")),
        ("tests, pure backend", tests("pure")),
        (
            "pure wheel builds",
            lambda: run([sys.executable, "tools/build_pure.py", str(work / "wheels")]),
        ),
        ("public text (tree)", lambda: run([py, "tools/publictext.py", "--tree"], guards_env())),
        ("secret scan (tree)", lambda: run([py, "tools/secretscan.py", "--tree"])),
    ]
    total = time.perf_counter()
    failed = []
    for name, fn in steps:
        t = time.perf_counter()
        print(f"\n== {name}", flush=True)
        ok = fn()
        print(
            f"-- {name}: {'ok' if ok else 'FAILED'} in {time.perf_counter() - t:.1f}s", flush=True
        )
        if not ok:
            failed.append(name)
            if name in {"venv + dev tools", "native build + install"}:
                break
    print(f"\ngate: {'FAILED: ' + ', '.join(failed) if failed else 'passed'} "
          f"in {time.perf_counter() - total:.1f}s")  # fmt: skip
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
