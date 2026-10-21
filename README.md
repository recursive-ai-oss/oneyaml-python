# oneyaml

**`json`-style YAML 1.2 and comment-preserving editing for Python, in one
package.** Edit a file or apply a JSON Patch to it and only what you touch
changes; a native backend and a pure-Python one sit behind the same API.

[![PyPI](https://img.shields.io/pypi/v/oneyaml.svg)](https://pypi.org/project/oneyaml/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/guide.md#install-and-backends)
[![CI](https://github.com/recursive-ai-oss/oneyaml-python/actions/workflows/ci.yml/badge.svg)](https://github.com/recursive-ai-oss/oneyaml-python/actions/workflows/ci.yml)
[![License: MIT OR Apache-2.0](https://img.shields.io/badge/license-MIT%20OR%20Apache--2.0-blue.svg)](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/LICENSE-MIT)

<!-- generated:bench-chart -->
[Full results, with memory and the method →](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/performance.md)
<!-- end generated -->

## Why oneyaml

- **Edits that keep your file.** An unedited document writes back byte for
  byte. Setting a value changes only that value, in its own style, and
  comments stay where they were
  ([documents and edits](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/guide.md#documents-and-edits)).
- **JSON Patch and merge patch on YAML.** RFC 6902 and RFC 7396 patches apply
  to a YAML file all or nothing, and keep its layout and comments
  ([patches](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/guide.md#patches)).
- **YAML 1.2 by default.** `yes` is a string and `0755` is the integer 755;
  `<<` merge keys are there when you ask for them. Checked against the yaml-test-suite
  ([conformance](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/conformance.md);
  [what changes from YAML 1.1](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/yaml-1.2-for-pyyaml-users.md)).
- **The `json` API you know.** `loads`, `load`, `dumps` and `dump` with plain
  `dict`, `list` and `str`, `parse_float`, `default` and `sort_keys`, and one
  `YAMLError` that is a `ValueError`.
- **Safe on untrusted input.** No tag constructs anything unless you hand it
  a function. Depth
  and alias expansion are bounded by default, and `Limits.untrusted()` adds
  caps on sizes for network input
  ([limits](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/guide.md#limits-and-untrusted-input)).
- **Drop-in modules for PyYAML and ruamel.yaml.** Change one import and keep
  your calls; the guides for
  [PyYAML](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/migrating-from-pyyaml.md)
  and [ruamel.yaml](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/migrating-from-ruamel.md)
  list what differs.

## Install

```sh
pip install oneyaml
```

Where a native wheel exists for your platform, pip installs it; anywhere
else it installs the pure-Python wheel. Nothing is compiled at install.

## Quick start

Read a file into plain Python data, then edit it in place: a literal block
gets a new line, a patch changes one value and adds a key, and the comments
and every other byte stay.

```python
import oneyaml

text = """\
# deploy settings
name: api
script: |
  make build
replicas: 2  # raised by the release job
"""

config = oneyaml.loads(text)  # plain Python data, as json.loads gives
print(config)

doc = oneyaml.parse(text)  # a document that keeps every byte you don't change
doc["script"] = "make build\nmake test\n"
doc.patch([
    {"op": "replace", "path": "/replicas", "value": 3},
    {"op": "add", "path": "/region", "value": "eu-west-1"},
])
print(doc, end="")
```

```text
{'name': 'api', 'script': 'make build\n', 'replicas': 2}
# deploy settings
name: api
script: |
  make build
  make test
replicas: 3  # raised by the release job
region: eu-west-1
```

Coming from PyYAML? `import oneyaml.compat.pyyaml as yaml` keeps your code
working, and
[YAML 1.2 for PyYAML users](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/yaml-1.2-for-pyyaml-users.md)
shows what changes when you move to `oneyaml.loads`. For input you don't
control, pass `limits=oneyaml.Limits.untrusted()`.

## Where to go

- **Using oneyaml:** [the guide](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/guide.md):
  reading and writing, limits and untrusted input, documents, edits and
  patches, backends, errors.
- **Moving from another library:**
  [PyYAML](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/migrating-from-pyyaml.md),
  [ruamel.yaml](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/migrating-from-ruamel.md),
  and [YAML 1.2 for PyYAML users](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/yaml-1.2-for-pyyaml-users.md)
  (`yes`, octal, base-60 numbers, dates, merge keys).
- **Choosing a library:** [how oneyaml compares](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/comparison.md),
  including where the others win.
- **Evidence:** [performance](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/performance.md),
  [conformance](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/docs/conformance.md).
- **Releases:** [changelog](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/CHANGELOG.md).
- **Contributing and security:** [CONTRIBUTING.md](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/CONTRIBUTING.md),
  [SECURITY.md](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/SECURITY.md),
  [CODE_OF_CONDUCT.md](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/CODE_OF_CONDUCT.md).
- **The oneyaml family:** [oneyaml-kit](https://github.com/recursive-ai-oss/oneyaml-kit),
  the shared conformance and benchmark kit, and oneyaml for
  [Rust](https://github.com/recursive-ai-oss/oneyaml-rust) (the parsing
  engine behind the native backend), [Go](https://github.com/recursive-ai-oss/oneyaml-go)
  and [C#](https://github.com/recursive-ai-oss/oneyaml-csharp).

## License

Licensed under either of [Apache License, Version 2.0](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/LICENSE-APACHE)
or [MIT license](https://github.com/recursive-ai-oss/oneyaml-python/blob/main/LICENSE-MIT) at your option.
