# Migrating from PyYAML

How to move a program from [PyYAML](https://pypi.org/project/PyYAML/) to oneyaml: first by
changing one import, then, when you are ready, to oneyaml's own API and YAML 1.2.

## Change one import

`oneyaml.compat.pyyaml` keeps PyYAML's names, arguments and results, YAML 1.1 included, so most
programs need only this:

```diff
-import yaml
+import oneyaml.compat.pyyaml as yaml
```

```python
import oneyaml.compat.pyyaml as yaml

text = """\
defaults: &defaults
  retries: 3
  verbose: no
prod:
  <<: *defaults
  mode: 0755
  released: 2024-05-01
"""
data = yaml.safe_load(text)
print(data)
print(yaml.safe_dump(data), end="")
```

```text
{'defaults': {'retries': 3, 'verbose': False}, 'prod': {'retries': 3, 'verbose': False, 'mode': 493, 'released': datetime.date(2024, 5, 1)}}
defaults:
  retries: 3
  verbose: false
prod:
  mode: 493
  released: 2024-05-01
  retries: 3
  verbose: false
```

The module follows PyYAML 6.0.3, and its differential tests compare it with that release.

## What the module covers

- **Loading:** `safe_load`, `safe_load_all`, `load` and `load_all` with `Loader=`, `full_load`,
  `unsafe_load`, `compose` and `compose_all`, and every loader class name, `CSafeLoader` and the
  other `C*` names included. YAML 1.1 scalars (`yes`, octal without `0o`, base-60 numbers, dates),
  `<<` merge keys, the last of a repeated key winning, and `!!set`, `!!omap`, `!!pairs` and
  `!!binary`.
- **Callbacks:** `add_constructor`, `add_multi_constructor`, `add_implicit_resolver`,
  `add_path_resolver`, `YAMLObject` and subclasses of the loaders, each registered on the class
  it is called on, as in PyYAML. A callback gets PyYAML-shaped nodes (`ScalarNode`,
  `SequenceNode`, `MappingNode`, with `tag`, `value`, `start_mark` and `end_mark`) and can call
  `construct_scalar`, `construct_mapping` and the other `construct_*` methods.
- **Dumping:** `dump`, `dump_all`, `safe_dump` and `safe_dump_all` with every PyYAML keyword, in
  PyYAML's layout byte for byte; the `Dumper` classes, `add_representer`,
  `add_multi_representer` and `YAMLObject.to_yaml`.
- **Errors:** `YAMLError` and its subclasses, with marks (`problem_mark.line` is 0-based, as in
  PyYAML). Each is also a `oneyaml.YAMLError`, so `except oneyaml.YAMLError` catches errors from
  both APIs.

```python
import oneyaml.compat.pyyaml as yaml


class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y

    def __repr__(self):
        return f"Point({self.x}, {self.y})"


def point(loader, node):
    return Point(**loader.construct_mapping(node))


yaml.add_constructor("!point", point, Loader=yaml.SafeLoader)
print(yaml.safe_load("origin: !point {x: 0, y: 0}\n"))
```

```text
{'origin': Point(0, 0)}
```

## What differs

### Safety

- **Nothing is ever constructed by running code.** `Loader`, `FullLoader` and `UnsafeLoader` read
  the same safe types as `SafeLoader`, and a `!!python/...` tag is a `ConstructorError`. A
  constructor your program registers for such a tag is still called.
- **Limits stay on.** The limits of [`oneyaml.Limits`](guide.md#limits-and-untrusted-input)
  apply, and merge keys count against the alias budget, so a document cannot expand without
  bound. A document that goes past a limit raises a `YAMLError` whose `category` is `limit`.

```python
import oneyaml.compat.pyyaml as yaml

try:
    yaml.load("!!python/object/apply:os.system [echo hi]\n", Loader=yaml.UnsafeLoader)
except yaml.YAMLError as e:
    print(type(e).__name__)
```

```text
ConstructorError
```

### Loading

- `scan`, `parse`, `emit` and `serialize`, PyYAML's token and event functions, are not
  provided.
- `safe_load_all` and `load_all` read the whole stream before they yield the first document.
- A mapping key that is a list or a mapping becomes a `tuple` or a `oneyaml.FrozenMapping`; PyYAML
  refuses it as unhashable.
- A merge key `<<` or a `=` that is not in a key position stays the text it spells; PyYAML fails
  there.
- Input is read as YAML 1.2 syntax, with YAML 1.1 values. So some text that PyYAML refuses is read,
  and some that PyYAML reads is an error, for example:
  - a byte order mark inside a scalar or a comment is an error, and one before a later document
    of the stream is accepted;
  - in a flow collection, `?foo` is a plain scalar, not an explicit key;
  - an anchor name may contain a colon;
  - the non-specific tag `!` makes a scalar a string (an empty one when the node is empty), where
    PyYAML resolves it as if untagged or reads null;
  - an escaped surrogate pair in double quotes is one character, where PyYAML keeps two halves;
  - a keep-chomped (`|+`) block scalar of only line breaks keeps them, and a block scalar keeps a
    last line that holds only spaces;
  - an indentation indicator on a top-level block scalar counts from the document's own
    indentation.

### Callbacks

- A loader with no registrations, or with constructors only for tags the document does not use,
  reads through the active backend. A document that uses a registered tag is parsed by the
  pure-Python parser on either backend.
- Standard collections are complete when a callback gets them, whatever `deep` says, and a
  callback that returns a generator is run to its end at once. Callbacks run in document order.
- Replacing `construct_object`, `construct_sequence` or `construct_mapping` in a subclass works,
  but nested calls through them stop at 100 levels with a `limit` error. Representer callbacks
  that call back into `represent_data` have the same bound.

### Dumping

Where PyYAML's output changes the data or cannot be read as YAML 1.2, the module writes something
else:

- With `allow_unicode`, U+0085, U+2028 and U+2029 are escaped in double quotes; PyYAML writes them
  raw in single quotes, and a reader folds them to spaces.
- A `width` smaller than the indentation folds once after an escape followed by a space; PyYAML
  folds twice and writes a `\` line that reads back as a backslash.
- A root block scalar (`default_style` `|` or `>`) that starts with white space is quoted.
- `version` or `tags` on a document after the first is preceded by `...`, so that a conforming
  reader accepts the directive.
- `encoding='utf-16'` writes one byte order mark, where PyYAML repeats it on every write.
- `CDumper` and `CSafeDumper` use the layout of `Dumper` and `SafeDumper`, not libyaml's.
- `Dumper` writes `!!python/...` objects as PyYAML does, but no loader of this package reads them
  back.

## Moving to the oneyaml API

The compat module keeps YAML 1.1. `oneyaml`'s own functions read YAML 1.2 and add lossless
documents and patches. The usual calls map like
this:

| PyYAML | oneyaml |
|---|---|
| `yaml.safe_load(text)` | `oneyaml.loads(text)` |
| `yaml.safe_load(fp)` | `oneyaml.load(fp)` |
| `yaml.safe_load_all(text)` (a generator) | `oneyaml.loads_all(text)` (a list) |
| `yaml.safe_dump(data)` (keys sorted) | `oneyaml.dumps(data, sort_keys=True)` |
| `yaml.safe_dump(data, sort_keys=False)` | `oneyaml.dumps(data)` |
| `yaml.safe_dump(data, fp)` | `oneyaml.dump(data, fp)` |
| `add_representer` for your type | `oneyaml.dumps(data, default=fn)`, as in `json.dumps` |
| `yaml.YAMLError`, `problem_mark` (0-based) | `oneyaml.YAMLError`, `.line` and `.column` (1-based), `.category` |
| `add_constructor("!point", fn)` | `oneyaml.loads(text, tags={"!point": fn})`; `fn` gets the node's plain content, not a node |
| `<<` merge keys | `oneyaml.loads(text, merge_keys=True)` |
| editing with comments kept | `oneyaml.parse(text)`, then [edit or patch](guide.md#documents-and-edits) |

What you will notice, in values:

- **YAML 1.2 Core schema.** `yes`, `no`, `on` and `off` are strings, `0755` is the integer 755,
  `1:30` and `2024-05-01` are strings. [YAML 1.2 for PyYAML users](yaml-1.2-for-pyyaml-users.md)
  lists every change, with ways to keep the old meaning.
- **Merge keys are opt-in.** `<<` is an ordinary key unless you pass `merge_keys=True`.
- **Duplicate keys are an error**, where PyYAML keeps the last.
- **Unknown tags are kept**, as `oneyaml.Tagged`, where PyYAML's safe loader raises; `tags=`
  builds the ones you name.
- **Order.** `dumps` keeps insertion order unless `sort_keys=True`; `safe_dump` sorts keys unless
  `sort_keys=False`.

```python
import oneyaml
import oneyaml.compat.pyyaml as yaml

text = "verbose: no\nmode: 0755\nb: 1\na: 2\n"
print(yaml.safe_load(text))
print(oneyaml.loads(text))
print(yaml.safe_dump({"b": 1, "a": "no"}), end="")
print(oneyaml.dumps({"b": 1, "a": "no"}), end="")
```

```text
{'verbose': False, 'mode': 493, 'b': 1, 'a': 2}
{'verbose': 'no', 'mode': 755, 'b': 1, 'a': 2}
a: 'no'
b: 1
b: 1
a: 'no'
```

Both write `'no'` quoted, so the text reads back the same under YAML 1.1 and 1.2.
