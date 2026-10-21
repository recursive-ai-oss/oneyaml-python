# Migrating from ruamel.yaml

How to move a program from [ruamel.yaml](https://pypi.org/project/ruamel.yaml/) to oneyaml:
first by changing its imports, then, when you are ready, to oneyaml's own documents and patches.

## Change the imports

`oneyaml.compat.ruamel` mirrors ruamel.yaml 0.19: the `YAML` class, and the `comments`,
`scalarstring` and `error` modules (with the `scanner`, `parser`, `constructor`, `composer` and
`reader` error modules) under the same names. Replace `ruamel.yaml` with `oneyaml.compat.ruamel`
in each import:

```diff
-from ruamel.yaml import YAML
-from ruamel.yaml.comments import CommentedMap
+from oneyaml.compat.ruamel import YAML
+from oneyaml.compat.ruamel.comments import CommentedMap
```

A round trip works as it does with ruamel.yaml, and keeps the file's comments, quoting and
spacing without any settings:

```python
import sys

from oneyaml.compat.ruamel import YAML

src = """\
# service settings
name: api  # public name
script: |
  make build
ports: [80, 443]
"""
yaml = YAML()  # round trip; YAML(typ="safe") gives plain dicts and lists
data = yaml.load(src)
data["script"] = "make build\nmake test\n"
data["ports"].append(8080)
data["replicas"] = 2
yaml.dump(data, sys.stdout)
```

```text
# service settings
name: api  # public name
script: |
  make build
  make test
ports: [80, 443, 8080]
replicas: 2
```

## How a dump decides what to write

In round-trip mode, the module is backed by oneyaml's lossless document:

- **Data loaded from a stream** (what `load` returned, or all of what `load_all` returned, in
  order) is written by editing the text it was read from. A changed value is set, a new key goes
  after the last key of its mapping, a removed key or item is deleted, and a new item is
  inserted. New pieces copy the layout of the pieces around them; the `indent()` settings apply
  only where the document has no example to copy. Everything else is written as it was read, and
  a tree nobody changed is written byte for byte. A tree can be dumped any number of times.
- **Anything else** (data built in the program, part of a loaded tree, a loaded tree whose root
  was replaced, or some of the documents of a stream) is written from scratch in ruamel.yaml's
  default layout: block style, two spaces per level, with `indent()` and the other settings
  honoured and comments set with `yaml_set_*` written. Lines are not wrapped.

## What differs

From ruamel.yaml 0.19.1; the module's differential tests compare it with that release.

### Values

- A round-trip load returns plain `str`, `int` and `float`, not `ScalarInt`, `ScalarFloat`,
  `TimeStamp` or `LiteralScalarString`: the document already remembers how each value was
  written. `preserve_quotes` is accepted, and is always on in effect.
- Style types such as `LiteralScalarString` choose the style only in data written from scratch;
  a value set in a loaded document takes the style of the value it replaces, or of its
  neighbours.
- `<<` merge keys are applied with `typ="safe"`, and left as an ordinary key in round-trip mode,
  where the merged keys are not visible through the mapping.
- `!!set`, `!!omap` and `!!pairs` become a `set`, a `dict` and a list of pairs with `typ="safe"`;
  round-trip mode reads them, and any other tagged node, as `oneyaml.Tagged`.
- Timestamp fractions are cut at microseconds, not rounded.
- Plain scalars that ruamel.yaml's resolver half-matches (`1__0`, `1_`, `._5`, `2001-02-30`) are
  strings; ruamel.yaml reads some as numbers and fails on others.
- A string that ruamel.yaml would read as a number or a date (`2024-05-01`, `1_000`), set into a
  loaded document, is written by oneyaml's own rules.
- Input is read as YAML 1.2 by oneyaml's parser, so some text that ruamel.yaml refuses is read,
  and some that it reads is an error.

### Edits and comments

- A document whose root is a scalar, or that is empty, has nothing to remember its text by (the
  loaded value is a plain `str`, `int` or `None`), so it is written from scratch.
- An edit the document cannot make, such as removing an anchor that an alias still uses, raises a
  `oneyaml.YAMLError` with a `category` and writes nothing, where ruamel.yaml would write the
  document with the alias expanded or reshaped.
- Comments cannot be added to a loaded tree (`NotImplementedError` at dump); the document's own
  comments are always kept. `.lc` (line and column) is not provided; `oneyaml.parse` and
  `Document.locate` give positions (below).

### Safety and limits

- `typ="unsafe"` is the safe loader: a `!!python/...` tag is an error, and nothing is ever
  constructed by running code. An unknown tag loads as `oneyaml.Tagged` instead of raising.
- [Limits](guide.md#limits-and-untrusted-input) are on. `max_depth` can only lower them, and
  `YAML().limits` takes a `oneyaml.Limits`.
- Duplicate keys are an error; `allow_duplicate_keys=True` is not supported.

### Not provided

- `width` is accepted and ignored: lines are not wrapped.
- `representer` and `constructor` hooks, `register_class`, and the scanner, parser and emitter
  calls (`scan`, `parse`, `compose`, `emit`, `serialize`) are not provided, and say so when used.
- `load_all` reads the whole stream before it yields the first document.

```python
from oneyaml.compat.ruamel import YAML

import oneyaml

yaml = YAML()
yaml.limits = oneyaml.Limits.untrusted()
try:
    yaml.load("name: a\nname: b\n")
except oneyaml.YAMLError as e:
    print(type(e).__name__, e.category)
```

```text
DuplicateKeyError duplicate-key
```

## Moving to the oneyaml API

`oneyaml.parse` gives the same lossless round trip without the compatibility layer, adds RFC 6902
and RFC 7396 patches, and reads values as plain Python data:

| ruamel.yaml | oneyaml |
|---|---|
| `YAML().load(text)` (round trip) | `doc = oneyaml.parse(text)` |
| `data["k"] = v` | `doc["k"] = v` |
| `YAML().dump(data, fp)` | `doc.dump(fp)`, or `str(doc)` |
| `jsonpatch.apply_patch(data, ops)` on the loaded tree | `doc.patch(ops)`, all or nothing |
| `YAML(typ="safe").load(text)` | `oneyaml.loads(text)` |
| `YAML(typ="safe").dump(data, fp)` | `oneyaml.dump(data, fp)` |
| `yaml.max_depth = n` | `limits=oneyaml.Limits(depth=n)` |
| `CommentedMap`, `CommentedSeq` | `oneyaml.MappingView`, `oneyaml.SequenceView` (live views of the document) |
| `data.lc.line`, `data.lc.key(k)` (0-based) | `doc.locate(path)` → `Location(line, column, end_line, end_column, comment)` (1-based), scalars included |
| `data.ca` (comment attributes) | `view.comment`, `doc.locate(path).comment` (read-only) |

```python
import oneyaml

doc = oneyaml.parse("""\
# service settings
name: api  # public name
ports: [80, 443]
""")
doc["ports"].append(8080)
doc.patch([{"op": "add", "path": "/replicas", "value": 2}])
print(doc, end="")
```

```text
# service settings
name: api  # public name
ports: [80, 443, 8080]
replicas: 2
```

ruamel.yaml reads YAML 1.2 by default too, so values such as `yes` and `0755` read the same in
both. Two things change: ruamel.yaml reads `2024-05-01` as a `datetime.date`, where
`oneyaml.loads` and `oneyaml.parse` give the string, and it applies `<<` merge keys, which
`oneyaml.loads` applies only with `merge_keys=True` and `oneyaml.parse` reads as an ordinary key.
The [guide](guide.md#documents-and-edits) covers views, pointers, stale views, comments and
locations.
