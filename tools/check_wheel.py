"""Install a built wheel into a fresh virtual environment and test it.

Usage: python tools/check_wheel.py WHEEL PYTHON

PYTHON is the interpreter the wheel is for. The wheel goes into a new venv with pytest,
the backend is checked (a native wheel must load the native module, the pure wheel must
not), and a subset of the test suite runs against the installed package on every backend
the wheel has. Needs network access for pytest.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Tests that need nothing beyond pytest: loading, dumping, documents, patches, the backends.
TESTS = [
    "test_backends.py",
    "test_load.py",
    "test_dump.py",
    "test_document.py",
    "test_document_patch.py",
    "test_events.py",
    "test_native.py",
]


# Build-machine paths that must not end up in a compiled module.
HOST_PATHS = re.compile(rb"/home/|/Users/|/root/|/runner/|/__w/|[A-Za-z]:\\(?:Users|a|work|home)\\")


def host_paths(wheel: Path) -> list[str]:
    found = []
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if name.endswith((".so", ".pyd", ".dylib")):
                found += [
                    f"{name}: {m.group().decode()}" for m in HOST_PATHS.finditer(z.read(name))
                ]
    return sorted(set(found))


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def main() -> int:
    wheel, python = Path(sys.argv[1]).resolve(), sys.argv[2]
    pure = wheel.name.endswith("-py3-none-any.whl")
    leaks = host_paths(wheel)
    if leaks:
        print("the wheel embeds build-machine paths:", *leaks, sep="\n  ")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        run([python, "-m", "venv", str(venv)])
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        vpy = str(bindir / "python")
        run([vpy, "-m", "pip", "install", "-q", "--no-deps", str(wheel)])
        run([vpy, "-m", "pip", "install", "-q", "pytest"])

        expected = "pure" if pure else "native"
        # Away from the repository so the installed package is the one imported.
        got = run(
            [vpy, "-c", "import oneyaml; print(oneyaml.backend)"],
            cwd=tmp,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if got != expected:
            print(f"expected the {expected} backend, got {got!r}")
            return 1

        if pure:
            # Without the native module, asking for it must fail rather than fall back.
            forced = subprocess.run(
                [vpy, "-c", "import oneyaml"],
                cwd=tmp,
                env=dict(os.environ, ONEYAML_BACKEND="native"),
                capture_output=True,
            )
            if forced.returncode == 0:
                print("forcing the native backend should fail in the pure wheel")
                return 1

        env = dict(os.environ, ONEYAML_TEST_BACKENDS="pure" if pure else "native,pure")
        env.pop("ONEYAML_BACKEND", None)
        tests = [str(ROOT / "tests" / t) for t in TESTS]
        run(
            [vpy, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(ROOT), *tests],
            cwd=tmp,
            env=env,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
