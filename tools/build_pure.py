"""Build the pure-Python wheel (py3-none-any) from src/, without the native module.

Needs Python 3.11 or newer (tomllib).
Usage: python tools/build_pure.py [OUTDIR]     (default OUTDIR: dist)

The wheel is written by hand with the standard library: it is a zip of the Python
sources plus metadata taken from pyproject.toml, so no Rust or build backend runs.
This is what installs on platforms that have no native wheel.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def build(outdir: Path) -> Path:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    name, version = meta["name"], meta["version"]
    dist_info = f"{name}-{version}.dist-info"

    files: dict[str, bytes] = {}
    for path in sorted((ROOT / "src" / name).rglob("*")):
        rel = path.relative_to(ROOT / "src")
        if (
            not path.is_file()
            or "__pycache__" in rel.parts
            or path.suffix in {".pyc", ".so", ".pyd"}
        ):
            continue
        if path.stem.split(".")[0] == "_native" and path.suffix != ".pyi":
            continue
        files[rel.as_posix()] = path.read_bytes()

    licenses = meta.get("license-files", [])
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {meta['description']}",
        f"License-Expression: {meta['license']}",
        *[f"License-File: {lic}" for lic in licenses],
        *[f"Classifier: {c}" for c in meta.get("classifiers", [])],
        f"Requires-Python: {meta['requires-python']}",
        "Description-Content-Type: text/markdown",
    ]
    readme = (ROOT / meta["readme"]).read_text(encoding="utf-8")
    files[f"{dist_info}/METADATA"] = ("\n".join(lines) + "\n\n" + readme).encode()
    files[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: oneyaml tools/build_pure.py\n"
        b"Root-Is-Purelib: true\nTag: py3-none-any\n"
    )
    for lic in licenses:
        files[f"{dist_info}/licenses/{lic}"] = (ROOT / lic).read_bytes()
    record = [_record_line(n, d) for n, d in files.items()] + [f"{dist_info}/RECORD,,"]
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()

    outdir.mkdir(parents=True, exist_ok=True)
    wheel = outdir / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in files.items():
            zf.writestr(
                zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0)), d, zipfile.ZIP_DEFLATED
            )
    return wheel


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "dist")))
