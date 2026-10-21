# Comparison

How oneyaml compares with the Python YAML libraries it is most often weighed against: PyYAML,
ruamel.yaml, yamlrocks, the other Rust-backed loaders (yaml-rs, py-yaml12, ryaml), strictyaml and
rapidyaml. We wrote oneyaml, so we chose the criteria; correct us with an issue where this page is
wrong or out of date. Speed and memory are on the [performance page](performance.md), measured,
with every loss.

## What each library is

Surveyed on 2026-09-30, at PyYAML 6.0.3, ruamel.yaml 0.19.1, yamlrocks 0.6.1, yaml-rs 0.1.6,
py-yaml12 0.2.0, ryaml 0.5.1, strictyaml 1.7.3 and rapidyaml 0.15.2.

| | oneyaml | PyYAML | ruamel.yaml | yamlrocks | yaml-rs, py-yaml12, ryaml | strictyaml |
|---|---|---|---|---|---|---|
| Implementation | Rust engine, or pure Python where no wheel exists | Python, optional libyaml C extension | Python; its C extension is opt-in | Rust | Rust | Python, on a vendored ruamel.yaml parser |
| Default YAML version | 1.2, Core schema; merge keys opt-in | 1.1 | 1.2, with dates and `1_000` still resolved | 1.2-like, merge keys applied | 1.2 (ryaml: mixed) | 1.2 parser; values are strings unless a schema types them |
| Edits that keep the untouched text | yes | no | comments, order and styles kept; quotes, indentation and flow spacing normalised | round-trip mode re-formats untouched comments and flow collections after an edit | no | through ruamel.yaml's engine |
| JSON Patch and merge patch | built in, all or nothing | no | with the `jsonpatch` package on a round-trip tree | no | no | no |
| Resource limits on by default | depth, alias expansion, integer digits | no | no (`max_depth` defaults to unlimited) | alias and depth limits | alias limits (py-yaml12: none effective) | anchors and tags refused |
| Drop-in for another API | PyYAML and ruamel.yaml | — | — | PyYAML shim | — | — |

## Where oneyaml is the better fit

- **You edit configuration files that people also edit.** Setting a value or applying a patch
  changes that value's text only, in its style; comments, quoting, indentation and blank lines
  elsewhere stay byte for byte ([documents and edits](guide.md#documents-and-edits)).
- **You patch YAML.** RFC 6902 and RFC 7396 patches apply to the text, all or nothing, without
  turning the file into JSON and back ([patches](guide.md#patches)).
- **You want YAML 1.2 values.** `country: NO` is a string and `0755` the integer 755, as other
  YAML 1.2 readers see them ([YAML 1.2 for PyYAML users](yaml-1.2-for-pyyaml-users.md)).
- **You read input you don't control.** Limits are on by default, `Limits.untrusted()` tightens
  them, and no tag ever constructs an object ([limits](guide.md#limits-and-untrusted-input)).
- **You need one package everywhere.** The same API runs on the native backend and on the
  pure-Python one, which needs no compiler and runs on PyPy and on platforms without a native
  wheel.
- **You are moving off PyYAML or ruamel.yaml step by step.** The compat modules keep your calls
  working while you change them ([PyYAML](migrating-from-pyyaml.md),
  [ruamel.yaml](migrating-from-ruamel.md)).

## Where the others are the better fit

- **PyYAML** is what most projects already depend on, and many tools and frameworks expect its
  objects and exceptions. It constructs arbitrary Python objects from `!!python/...` tags for
  trusted input, which oneyaml never does, and it has a token and event API (`scan`, `parse`,
  `emit`) that oneyaml's compat module does not provide. If your files are written for YAML 1.1
  and nothing needs editing, PyYAML reads them as they were meant.
- **ruamel.yaml** can add and move comments in a loaded tree, register your classes for round
  trip, and wrap long lines. oneyaml's documents read comments and report where every node is,
  but cannot add or change comments yet. ruamel.yaml has been the lossless editor of the
  Python ecosystem for years.
- **yamlrocks** is a Rust-backed YAML 1.2 library with a PyYAML shim, a YAML 1.1 mode, a
  round-trip mode and options such as include-path confinement and duplicate-key policies. If
  you need those options and don't need edits that keep the untouched text, it is worth
  measuring on your own files.
- **yaml-rs, py-yaml12 and ryaml** are Rust-backed loaders with a small API; py-yaml12 keeps tags
  as wrappers with opt-in handlers. If all you do is load, compare them on the
  [performance page](performance.md) and on your own data.
- **strictyaml** validates against a schema and refuses tags, anchors and flow style, a
  restricted YAML that is easier to review. oneyaml has no schema validation: validate the output
  of `oneyaml.loads` with a library such as pydantic, and map an error's path back to the file
  with `Document.locate`.
- **rapidyaml** gives a low-level tree over the text without building Python objects, for very
  large files that you want to walk rather than load.
- **Typed loading** into dataclasses or models is not built in: oneyaml returns plain data for
  your validation library to take.

## Choosing

- To read configuration as plain data: `oneyaml.loads`, or whichever loader the
  [performance page](performance.md) shows fits your files.
- To change a file in place, or apply a patch to it: `oneyaml.parse`.
- To keep a program on PyYAML's or ruamel.yaml's API while you move: the compat modules.
- To build objects from tags with your own functions: `oneyaml.loads(text, tags=...)`; to have
  `!!python/...` tags build them, PyYAML, on trusted input only.
- To add comments, or validate against a schema: ruamel.yaml or strictyaml, as above.
