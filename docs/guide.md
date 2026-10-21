# Guide

How to read and write YAML with oneyaml, edit a file in place, apply patches, handle untrusted
input, choose a backend and deal with errors. Every sample on this page runs as shown, and the
block under it is what it prints.

- [Install and backends](#install-and-backends)
- [Reading YAML](#reading-yaml)
- [Writing YAML](#writing-yaml)
- [Limits and untrusted input](#limits-and-untrusted-input)
- [Documents and edits](#documents-and-edits)
- [Patches](#patches)
- [Errors](#errors)

## Install and backends

```sh
pip install oneyaml
```

oneyaml needs Python 3.10 or later. CI tests CPython 3.10 to 3.14, the free-threaded build of
3.14, and PyPy 3.11 with the pure-Python backend.

The package has two backends behind one API:

- **native**, a compiled module built on [oneyaml for Rust](https://github.com/recursive-ai-oss/oneyaml-rust),
  shipped in the platform wheels;
- **pure**, written in Python, shipped in every wheel.

pip picks a native wheel when one exists for your platform and interpreter (the files on the
[PyPI page](https://pypi.org/project/oneyaml/#files) list them), and the pure-Python wheel
otherwise. Nothing is compiled at install, and the package has no dependencies.

At import, oneyaml uses the native backend if its module loads and the pure one otherwise.
`oneyaml.backend` says which is active, and `oneyaml.engine_version()` gives the version of the
Rust engine (None on the pure backend):

```sh
python -c "import oneyaml; print(oneyaml.backend, oneyaml.engine_version())"
```

Set `ONEYAML_BACKEND` before the import to choose:

| `ONEYAML_BACKEND` | Effect |
|---|---|
| `auto` (or unset) | native if it imports, else pure |
| `native` | native, or an `ImportError` that says why it could not load |
| `pure` | pure, even where the native module is installed |

```sh
ONEYAML_BACKEND=pure python app.py
```

Both backends run the same test suite and are meant to give the same values and write the same
text; a difference between them is a bug, and worth [reporting](../CONTRIBUTING.md). The native
one exists for speed ([performance](performance.md)). Error messages are worded differently
between them, so match on the error's class and `category`, never on its text (see
[Errors](#errors)).

## Reading YAML

`loads` reads one document from a `str` or `bytes`; `load` reads one from an open text or binary
file. Bytes may be UTF-8, UTF-16 or UTF-32, with or without a byte order mark.

```python
import io

import oneyaml

config = oneyaml.loads("name: api\nports: [80, 443]\ndebug: false\n")
print(config)

with io.BytesIO(b"name: worker\n") as fp:  # any file opened in "rb" or "r" mode
    print(oneyaml.load(fp))
```

```text
{'name': 'api', 'ports': [80, 443], 'debug': False}
{'name': 'worker'}
```

A stream of several documents is an error for `loads`, whose message points to `loads_all`.
`loads_all` and `load_all` return a list with one item per document. An empty stream gives `None`
from `loads` and `[]` from `loads_all`.

```python
import oneyaml

print(oneyaml.loads_all("kind: Service\n---\nkind: Deployment\n"))
print(oneyaml.loads(""), oneyaml.loads_all(""))
```

```text
[{'kind': 'Service'}, {'kind': 'Deployment'}]
None []
```

Values are resolved by the YAML 1.2 Core schema and become these Python types:

| YAML | Python |
|---|---|
| mapping | `dict`, in document order |
| sequence | `list` |
| `true`, `false` (any case) | `bool` |
| `null`, `~`, empty | `None` |
| integer (`12`, `0o17`, `0x1F`) | `int`, any size |
| float (`1.5`, `1e3`, `.inf`, `.nan`) | `float` |
| anything else, including `yes`, `on`, `1:30` and `2024-05-01` | `str` |
| `!!binary` | `bytes` |
| any other tag, such as `!color` | `oneyaml.Tagged(tag, value)`, unless you pass a function for it in `tags=` |
| a sequence as a mapping key | `tuple` |
| a mapping as a mapping key | `oneyaml.FrozenMapping`, read-only and hashable |

[YAML 1.2 for PyYAML users](yaml-1.2-for-pyyaml-users.md) lists what this changes for a file
written for PyYAML.

An alias to a collection gives the same Python object as its anchor:

```python
import oneyaml

data = oneyaml.loads("""\
defaults: &defaults [lint, test]
ci: *defaults
logo: !!binary aGk=
color: !rgb ff8800
? [x, y]
: point
""")
print(data["ci"] is data["defaults"])
print(data)
```

```text
True
{'defaults': ['lint', 'test'], 'ci': ['lint', 'test'], 'logo': b'hi', 'color': Tagged('!rgb', 'ff8800'), ('x', 'y'): 'point'}
```

A duplicate key is an error, never a silent overwrite. So is a mapping whose keys are different in
YAML but equal as Python `dict` keys, such as `1` and `true`: an entry is never dropped.

A file read with `load` or `load_all` names itself in its errors: `YAMLError.source` holds the
file's name, and the message includes it.

### Load options

`loads`, `loads_all`, `load` and `load_all` take three options besides `limits`. Without them a
read is spec-correct YAML 1.2; each one is something you ask for.

| Option | Default | Effect |
|---|---|---|
| `merge_keys` | `False` | apply YAML 1.1 `<<` merge keys, for files written for PyYAML, Kubernetes or Compose |
| `parse_float` | `None` | called with the text of each finite float in place of `float`, as in `json.loads`; `decimal.Decimal` keeps the digits |
| `tags` | `None` | a dict from a tag to a function that gets the node's plain content and returns the value to use; only the tags you name are touched |

```python
import datetime
import decimal

import oneyaml

text = """\
base: &base {retries: 3, verbose: false}
prod:
  <<: *base
  verbose: true
price: 1.10
release: !date 2024-05-01
"""
print(oneyaml.loads(text))
print(oneyaml.loads(
    text,
    merge_keys=True,
    parse_float=decimal.Decimal,
    tags={"!date": datetime.date.fromisoformat},
))
```

```text
{'base': {'retries': 3, 'verbose': False}, 'prod': {'<<': {'retries': 3, 'verbose': False}, 'verbose': True}, 'price': 1.1, 'release': Tagged('!date', '2024-05-01')}
{'base': {'retries': 3, 'verbose': False}, 'prod': {'retries': 3, 'verbose': True}, 'price': Decimal('1.10'), 'release': datetime.date(2024, 5, 1)}
```

With `merge_keys`, a mapping's own entries win over merged ones, and in a list of mappings
(`<<: [*a, *b]`) the first wins. Only a plain `<<` merges; a quoted `"<<"` is an ordinary key. A
repeated key is still an error, and merged entries count against the alias limits. For a `!!`
tag, `tags` takes the full form, such as `"tag:yaml.org,2002:timestamp"`. A read with any of the
three options takes a slower path than a plain one.

The [PyYAML compat module](migrating-from-pyyaml.md) and the `typ="safe"` mode of the
[ruamel.yaml compat module](migrating-from-ruamel.md) apply merge keys as those libraries do.

## Writing YAML

`dumps` returns the text of one document, `dumps_all` a stream of several, and `dump` and
`dump_all` write to an open file (UTF-8 for a binary file).

```python
import oneyaml

data = {
    "name": "api",
    "enabled": "yes",
    "mode": "0755",
    "ports": [80, 443],
    "env": {},
    "script": "make build\nmake test\n",
}
print(oneyaml.dumps(data), end="")
print(oneyaml.dumps({"a": {"b": [1, 2]}}, flow=True), end="")
print(oneyaml.dumps_all([{"kind": "Service"}, {"kind": "Deployment"}]), end="")
```

```text
name: api
enabled: 'yes'
mode: '0755'
ports:
- 80
- 443
env: {}
script: |
  make build
  make test
{a: {b: [1, 2]}}
kind: Service
---
kind: Deployment
```

Keys keep their insertion order. A string that a YAML 1.2 or a YAML 1.1 reader would read as
something else (`'yes'`, `'0755'`) is quoted, so the text reads back as the same data in either.
The options change the layout and never the data:

| Option | Default | Effect |
|---|---|---|
| `indent` | `2` | spaces per nesting level, 1 to 16 |
| `flow` | `False` | every collection in flow style, on one line |
| `explicit_start` | `False` | begin each document with `---` |
| `sort_keys` | `False` | write the entries of every mapping in key order |
| `default` | `None` | a function called for an object that cannot otherwise be written, as in `json.dumps` |

`None`, `bool`, `int`, `float`, `str`, `list`, `tuple`, `dict`, `FrozenMapping`, `bytes` and
`Tagged` can be written, and so can a `Document` or a view of one, as its current value. Any
other type is a `TypeError` unless `default` turns it into one of these, as with `json.dumps`.
Subclasses are written as the type they extend. A value that contains itself is a `ValueError`.

```python
import datetime

import oneyaml


def default(obj):
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"cannot write {type(obj).__name__}")


data = {"tags": {"web", "api"}, "released": datetime.date(2024, 5, 1), "name": "api"}
print(oneyaml.dumps(data, default=default, sort_keys=True), end="")
```

```text
name: api
released: '2024-05-01'
tags:
- api
- web
```

## Limits and untrusted input

Every read runs under resource limits, so a small input cannot make a huge value or a deep
recursion. `oneyaml.Limits()` holds the defaults: nesting depth, alias expansion and the digits of
an integer are bounded. For input you don't control, pass `Limits.untrusted()`, which also bounds
the input size, the size of one scalar and the number of documents:

```python
import oneyaml

# Nine levels of nine aliases each: a short text that expands to an enormous list.
lines = ["l0: &l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
for i in range(1, 9):
    lines.append(f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 9) + "]")
bomb = "\n".join(lines) + "\n"

try:
    oneyaml.loads(bomb, limits=oneyaml.Limits.untrusted())
except oneyaml.LimitError as e:
    print(type(e).__name__, e.category)
```

```text
LimitError limit
```

`Limits` is immutable. Build one with the fields you want, or change some fields of another with
`replace`. A field set to `None` has no limit; `Limits.none()` turns every limit off and is only
for input you trust completely.

```python
import oneyaml

print(oneyaml.Limits())
print(oneyaml.Limits.untrusted())

strict = oneyaml.Limits.untrusted().replace(depth=20, documents=1)
print(oneyaml.loads("a: [1, 2]\n", limits=strict))
```

```text
Limits(depth=10000, alias_nodes=(10, 1000000), alias_bytes=(10, 67108864), input_size=None, scalar_size=None, documents=None, int_digits=4300)
Limits(depth=1000, alias_nodes=(2, 100000), alias_bytes=(2, 8388608), input_size=67108864, scalar_size=16777216, documents=10000, int_digits=4300)
{'a': [1, 2]}
```

| Field | Bounds |
|---|---|
| `depth` | nesting of collections; an alias counts at the depth where it is used |
| `alias_nodes` | nodes that aliases stand for in one document: a number, or `(factor, floor)` for the larger of `factor` × the document's nodes and `floor` |
| `alias_bytes` | the same for bytes of scalar text |
| `input_size` | bytes of input, after decoding to UTF-8 |
| `scalar_size` | bytes in one scalar |
| `documents` | documents in a stream |
| `int_digits` | decimal digits of an integer (CPython's own default) |

`loads`, `loads_all`, `load`, `load_all` and `parse` take `limits=`. An alias shares its object
with its anchor but is charged for what a copy would hold, so the alias budgets bound the data
however it is shared. The compat modules never loosen these limits. [SECURITY.md](../SECURITY.md)
says what the library promises and how to report a problem.

## Documents and edits

`oneyaml.parse` reads a lossless `Document`. `str(doc)` gives the input back byte for byte until
you edit it; after an edit, only the edited value's text changes. Assignment is exact: reading
the value back gives exactly what you set, in the old value's style when that style can hold it.
Here a literal block gets a value with no final line break, so its header becomes `|-`:

```python
import oneyaml

text = """\
#hello
a: 10

#abc 123
b: |
  this is a test
c: 13
"""
doc = oneyaml.parse(text)
print(str(doc) == text)

doc["b"] = "this is a test2"
print(doc, end="")
print(repr(doc["b"]))
```

```text
True
#hello
a: 10

#abc 123
b: |-
  this is a test2
c: 13
'this is a test2'
```

A `Document` behaves as the root of its first document. Collections read from it are live views,
`MappingView` (a `MutableMapping`) and `SequenceView` (a `MutableSequence`), and edits through
them write into the text. Scalars read as plain Python values, and `to_python()` gives a view or
the whole document as plain data, the same as `loads` would.

```python
import oneyaml

doc = oneyaml.parse("""\
spec:
  containers:
    - name: web
      image: nginx:1.27  # pinned
      ports: [80, 443]
""")
web = doc["spec"]["containers"][0]
web["image"] = "nginx:1.28"
web["ports"].append(8080)
web["env"] = {"MODE": "prod"}
print(doc, end="")
print(doc.to_python())
```

```text
spec:
  containers:
    - name: web
      image: nginx:1.28  # pinned
      ports: [80, 443, 8080]
      env:
        MODE: prod
{'spec': {'containers': [{'name': 'web', 'image': 'nginx:1.28', 'ports': [80, 443, 8080], 'env': {'MODE': 'prod'}}]}}
```

A new entry copies the layout of its siblings, block or flow and indentation. `del`,
`pop`, `insert`, `update` and the other methods of `dict` and `list` work on views too.

Views hold no data of their own: each is a path from the root, read again on every use. After an
edit removes or replaces the node a view stands for, using the view raises `StaleViewError`
instead of reading whatever is there now:

```python
import oneyaml

doc = oneyaml.parse("jobs:\n  - name: lint\n  - name: test\n")
first = doc["jobs"][0]
del doc["jobs"][0]
try:
    first["name"]
except oneyaml.StaleViewError:
    print("stale")
print(doc, end="")
```

```text
stale
jobs:
  - name: test
```

Keys are matched by their exact value, as in a `dict`: `doc["80"]` does not find the key `80`.
`doc.at(pointer)` reads by an RFC 6901 JSON Pointer instead, which names keys by their text, as a
patch does:

```python
import oneyaml

doc = oneyaml.parse("80: http\nports:\n  - 8080\n")
print(doc[80], doc.at("/80"), doc.at("/ports/0"))
try:
    doc["80"]
except KeyError:
    print("no key '80'")
```

```text
http http 8080
no key '80'
```

A missing path is a `KeyError` or an `IndexError`. Two keys with the same text, such as `1` and
`"1"`, make a pointer ambiguous, and it is refused with a `PatchError` of category
`refused-ambiguous`.

A stream of several documents is one `Document`; `doc.documents` gives each of them as a
`Document` that shares the stream, and writing any of them writes the whole stream. A document
read from bytes keeps its encoding and byte order mark:

```python
import io

import oneyaml

stream = oneyaml.parse("name: a\n---\nname: b\n")
stream.documents[1]["name"] = "c"
print(len(stream.documents))
print(stream, end="")

utf16 = oneyaml.parse("port: 1\n".encode("utf-16"))
utf16["port"] = 2
print(utf16.encoding, bytes(utf16) == "port: 2\n".encode("utf-16"))

out = io.StringIO()
stream.dump(out)  # text to a text file, bytes to a binary one
print(out.getvalue() == str(stream))
```

```text
2
name: a
---
name: c
utf-16-le True
True
```

Views and documents also report how a node is written: `style` (`"block"` or `"flow"`), `tag`,
`anchor` and `position` (1-based line and column where the node starts, its anchor or tag
included).

```python
import oneyaml

doc = oneyaml.parse("base: &base !settings {retries: 3}\n")
node = doc["base"]
print(node.style, node.tag, node.anchor, node.position)
```

```text
flow !settings base Position(line=1, column=7)
```

### Comments and locations

`.comment` on a document or a view gives the comment that belongs to the node, as written, or
`None`: the comment at the end of its line, else the comment lines just above it. A mapping entry
owns the comments around its key, so a collection under a key reads the comment after the colon.
Comments are read-only.

A scalar reads as plain data and has no `.comment`. `doc.locate(path)` reports where any node is,
scalars included, as a `Location`: the 1-based `line` and `column` where it starts, `end_line` and
`end_column` just after it, and its `comment`. `path` is a JSON Pointer or a sequence of keys and
indices, the form in which validation libraries such as pydantic report errors, so you can point
at the line of a value that failed validation:

```python
import oneyaml

doc = oneyaml.parse("""\
# deploy settings

# the image to run
image: api:1.4
ports:  # public
  - 80
env:
  MODE: prod  # or staging
""")
print(repr(doc.comment))
print(repr(doc["ports"].comment))
print(doc.locate("/image"))

loc = doc.locate(("env", "MODE"))  # a path as a validation error reports it
print(f"line {loc.line}, column {loc.column}: {loc.comment}")
```

```text
'# deploy settings'
'# public'
Location(line=4, column=8, end_line=4, end_column=15, comment='# the image to run')
line 8, column 9: # or staging
```

The comment above the first entry of a document belongs to that entry; the document itself reads
the comments before `---` and those set apart from the content by a blank line, as
`# deploy settings` is here.

### Writing a document as data

`str(doc)` is the text, comments and layout included. `oneyaml.dumps(doc)` or
`oneyaml.dumps(view)` writes the current value in the library's default layout instead, as
`dumps(doc.to_python())` would. `copy.copy` and `copy.deepcopy` of a document or a view give that
plain data too, since a copy that kept writing into the same text would not be a copy; for an
independent document, use `oneyaml.parse(bytes(doc))`.

```python
import copy

import oneyaml

doc = oneyaml.parse("name: api  # service\nenv: {MODE: prod}\n")
print(oneyaml.dumps(doc["env"]), end="")
print(copy.deepcopy(doc))

twin = oneyaml.parse(bytes(doc))
twin["name"] = "worker"
print(doc["name"], twin["name"])
```

```text
MODE: prod
{'name': 'api', 'env': {'MODE': 'prod'}}
api worker
```

A document always reads `<<` as an ordinary key: `merge_keys` is an option of `loads`, not of
`parse`.

## Patches

`doc.patch(ops)` applies an RFC 6902 JSON Patch: a list of operation dicts, or the same as JSON
text. Pointers name keys by their text. A patch is all or nothing: if any operation fails, the
document is left exactly as it was and a `PatchError` names the operation by its `index`.

```python
import oneyaml

doc = oneyaml.parse("""\
# service
image: api:1.4  # bumped by CI
replicas: 2
""")
before = str(doc)
try:
    doc.patch([
        {"op": "replace", "path": "/image", "value": "api:1.5"},
        {"op": "test", "path": "/replicas", "value": 3},
    ])
except oneyaml.PatchError as e:
    print(type(e).__name__, e.index, e.category)
print(str(doc) == before)

doc.patch('[{"op": "replace", "path": "/image", "value": "api:1.5"}]')
print(doc, end="")
```

```text
PatchError 1 patch-test
True
# service
image: api:1.5  # bumped by CI
replicas: 2
```

All six operations are supported: `add`, `remove`, `replace`, `move`, `copy` and `test`. `test`
compares by the YAML data model, so `1` does not equal `1.0`.

`doc.merge_patch(value)` applies an RFC 7396 merge patch: members set to `None` are removed,
mappings merge, and anything else replaces the old value whole. It is all or nothing too.

```python
import oneyaml

doc = oneyaml.parse("""\
server:
  host: 0.0.0.0
  port: 8080  # public
  debug: true
""")
doc.merge_patch({"server": {"port": 9090, "debug": None, "workers": 4}})
print(doc, end="")
```

```text
server:
  host: 0.0.0.0
  port: 9090  # public
  workers: 4
```

Both take `document=` to patch another document of a stream (`doc.patch(ops, document=1)`).
A write through an alias, by a patch or through a view, is refused with category `refused-alias`;
write at the anchor instead, and every alias shows the change. After a patch that changed anything, views taken before it are
stale; the `Document` itself stays valid.

## Errors

Every problem with the input raises `oneyaml.YAMLError`, a subclass of `ValueError` (as
`json.JSONDecodeError` is), so `except ValueError` catches it too. Its subclasses say what kind
of problem it is:

| Class | Raised when |
|---|---|
| `ParseError` | the text is not valid YAML: a syntax or encoding error, a duplicate key, a tag that cannot apply |
| `LimitError` | the input goes past a [limit](#limits-and-untrusted-input) |
| `PatchError` | a patch or an edit is refused, or a pointer is ambiguous or malformed; the document is unchanged |
| `StaleViewError` | a view is used after an edit removed or replaced its node |
| `YAMLError` itself | anything else, such as several documents given to `loads` |

Each error has `category` (`syntax`, `encoding`, `duplicate-key`, `unresolved-tag`, `limit`,
`other`, and for patches `patch-path`, `patch-test`, `patch-invalid`, `refused-alias`,
`refused-ambiguous`), `line` and `column` (1-based; `None` when the problem has no place in the
text), `offset` and `message`, and `source`, the file's name, when `load` read a named file. A
`PatchError` also has `index`, the failing operation of a patch (`None` for a merge patch or an
edit).

```python
import oneyaml

for text in ["a: [1, 2\n", "a: 1\na: 2\n", "a: 1\n---\nb: 2\n"]:
    try:
        oneyaml.loads(text)
    except oneyaml.YAMLError as e:
        print(type(e).__name__, e.category, e.line, e.column)
```

```text
ParseError syntax 2 1
ParseError duplicate-key 2 1
YAMLError other 2 1
```

Wrong argument types raise `TypeError`, as with `json`: a value `dumps` cannot write, a `limits`
that is not a `Limits`, input that is neither text nor bytes.
