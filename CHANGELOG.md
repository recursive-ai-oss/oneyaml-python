# Changelog

All notable changes are listed here, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Breaking changes are listed first, under Breaking,
each with what to change in your code.

## [Unreleased]

The first release.

### Added

- `loads`, `load`, `loads_all` and `load_all` read YAML 1.2 into plain Python data by the Core
  schema; `dumps`, `dump`, `dumps_all` and `dump_all` write it, quoting every string that a
  YAML 1.2 or YAML 1.1 reader would read as something else ([guide](docs/guide.md)).
- Load options `merge_keys` (YAML 1.1 `<<`, off by default), `parse_float` and `tags`, and dump
  options `default` and `sort_keys`, as in `json` ([load options](docs/guide.md#load-options),
  [writing](docs/guide.md#writing-yaml)). An error from `load` names the file in `source`.
- `parse` returns a lossless `Document`: it writes back byte for byte, and an edit through its
  `dict`- and `list`-like views changes only the edited value, in its own style. `Document.at`
  reads by JSON Pointer ([documents and edits](docs/guide.md#documents-and-edits)).
- `Document.locate(path)` gives the line, column, end and comment of any node, for a pointer or a
  path of keys as validation libraries report it; `.comment` reads a node's comment
  ([comments and locations](docs/guide.md#comments-and-locations)). `dumps` writes a document or
  a view as its current value, and `copy`/`deepcopy` give plain data.
- `Document.patch` and `Document.merge_patch` apply RFC 6902 and RFC 7396 patches, all or nothing
  ([patches](docs/guide.md#patches)).
- `Limits`, with defaults on every read, `Limits.untrusted()` for input you don't control and
  `Limits.none()` ([limits](docs/guide.md#limits-and-untrusted-input)).
- `YAMLError`, a `ValueError`, with `ParseError`, `LimitError`, `PatchError` and
  `StaleViewError`, each with `category`, `line` and `column` ([errors](docs/guide.md#errors)).
- A native backend built on oneyaml for Rust and a pure-Python backend, chosen at import or with
  `ONEYAML_BACKEND`; `oneyaml.backend` names the active one
  ([backends](docs/guide.md#install-and-backends)).
- `oneyaml.compat.pyyaml`, a drop-in for PyYAML 6.0.3, loading and dumping
  ([migration guide](docs/migrating-from-pyyaml.md)).
- `oneyaml.compat.ruamel`, a drop-in for the `YAML` class of ruamel.yaml 0.19 on lossless
  documents ([migration guide](docs/migrating-from-ruamel.md)).

[Unreleased]: https://github.com/recursive-ai-oss/oneyaml-python/commits/main
