# Contributing

Thanks for helping. Bug reports, reproductions, documentation fixes and pull requests are all
welcome.

## Before you start

- **Found a vulnerability?** Do not open an issue. Follow [SECURITY.md](SECURITY.md).
- **Found a bug?** Open an issue with the smallest YAML and Python code that show it, the version
  (`python -c "import oneyaml; print(oneyaml.__version__, oneyaml.backend)"`), and what you
  expected. A difference between the native and the pure backend is always a bug. A failing test
  is better still.
- **Want a feature?** Open an issue first and describe the problem you are solving. The library
  keeps a small API on purpose, so a feature that has not been discussed may not be accepted.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

You need Python 3.10 or later, [uv](https://docs.astral.sh/uv/), and a Rust toolchain for the
native backend (the version is in `native/Cargo.toml` as `rust-version`). The pure backend needs
no Rust.

```sh
uv venv
uv pip install --group dev
.venv/bin/maturin build --profile dev -o wheels -i .venv/bin/python
uv pip install --no-deps --force-reinstall wheels/*.whl
.venv/bin/python -m pytest -q -n auto
```

The tests run on both backends. Set `ONEYAML_TEST_BACKENDS=pure` on an interpreter that has no
native module, such as PyPy.

The repository has the Python package in `src/oneyaml` (the API, the pure backend in `_pure`, the
compat modules in `compat`), the native module's Rust crate in `native/`, tests in `tests/` and
repository checks in `tools/`. The package has no runtime dependencies, and a pull request that
adds one will be declined unless there is a strong reason.

## Checks

Before you send a pull request, run the local pre-merge check with a scratch directory outside the
repository:

```sh
GATE_WORK=../oneyaml-gate python tools/gate.py
```

It runs `ruff check`, `ruff format --check`, `cargo fmt` and `cargo clippy`, builds and installs the
native module, runs the tests on the native and then the pure backend, builds the pure-Python
wheel, and checks the text and secrets of the tree. CI runs the same checks, with the tests on
every Python version it supports. Its text check also reads a pattern list that CI supplies
through `PUBLICTEXT_PATTERNS`; you do not need that list to run the gate.

## What a good pull request has

- **One change.** Keep a pull request to one fix or one feature.
- **A test that fails without it**, run on both backends. There is no coverage target.
- **The same behaviour on both backends.** A change to one backend that the other does not make
  needs a test showing both agree.
- **Documentation.** A public function or class has a docstring whose first line stands alone.
  Python code blocks in `README.md` and `docs/` are run by `tests/test_docs_samples.py` on both
  backends, and a ```` ```text ```` block right after one is its expected output; the same test
  checks the links in those pages.
- **No hand-typed numbers.** Benchmark and conformance figures in the documentation are
  generated, between `<!-- generated:... -->` and `<!-- end generated -->` markers. Do not edit
  inside them; the oneyaml-kit `bin/bench readme` command writes them from a benchmark and
  conformance result.
- **A changelog line** under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user
  can see. Mark breaking changes.
- **No crash on any input.** Input must never crash the interpreter, hang, or make the native
  code panic; such a bug is treated as a security bug. The pure backend follows nesting with
  explicit stacks, never with recursion that depends on the input.

## Licence

The project is dual licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE). Unless you
state otherwise, your contribution is licensed the same way, without additional terms.
