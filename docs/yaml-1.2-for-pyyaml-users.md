# YAML 1.2 for PyYAML users

`oneyaml.loads` and `oneyaml.parse` read values by the YAML 1.2 Core schema. PyYAML reads them by
YAML 1.1, so the same file can give different Python values. This page lists what changes and how
to keep the old meaning where you need it.

## What reads differently

```python
import oneyaml
import oneyaml.compat.pyyaml as yaml  # reads as PyYAML does

text = """\
enabled: yes
debug: off
country: NO
mode: 0755
mode_12: 0o755
mask: 0b101
duration: 1:30
released: 2024-05-01
size: 1_000
ratio: 1e3
"""
print(yaml.safe_load(text))
print(oneyaml.loads(text))
```

```text
{'enabled': True, 'debug': False, 'country': False, 'mode': 493, 'mode_12': '0o755', 'mask': 5, 'duration': 90, 'released': datetime.date(2024, 5, 1), 'size': 1000, 'ratio': '1e3'}
{'enabled': 'yes', 'debug': 'off', 'country': 'NO', 'mode': 755, 'mode_12': 493, 'mask': '0b101', 'duration': '1:30', 'released': '2024-05-01', 'size': '1_000', 'ratio': 1000.0}
```

| Text | YAML 1.1 (PyYAML) | YAML 1.2 Core (oneyaml) |
|---|---|---|
| `yes`, `no`, `on`, `off`, in lower case, capitalised or upper case | `bool` | `str` |
| `0755` | octal: 493 | decimal: 755 |
| `0o755` | `str` | octal: 493 |
| `0b101` | binary: 5 | `str` |
| `1:30` (base 60) | 90 | `str` |
| `2024-05-01`, `2024-05-01T10:00:00Z` | `datetime.date`, `datetime.datetime` | `str` |
| `1_000` | 1000 | `str` |
| `1e3` | `str` (YAML 1.1 floats need a `.`) | `float` |
| `<<` in a mapping | merge key | ordinary key; a merge key with `merge_keys=True` |
| a repeated key | the last one wins | `ParseError`, category `duplicate-key` |
| `!!set`, `!!omap`, `!!pairs`, `!!timestamp` | `set`, list of pairs, `datetime` | `oneyaml.Tagged`, or what your function in `tags=` returns |
| an unknown tag such as `!point` | `ConstructorError` (safe loader) | `oneyaml.Tagged`, or what your function in `tags=` returns |

The same in both: `true`, `false`, `null`, `~`, decimal and `0x` integers, `.inf` and `.nan`,
`!!binary`. A trap in both: `version: 1.10` is the float `1.1`; quote it as `'1.10'`.

## Keeping the YAML 1.1 meaning

- **Keep using PyYAML's rules** for a file written for them, with
  [`oneyaml.compat.pyyaml`](migrating-from-pyyaml.md).
- **Say what you mean in the file.** Quote a string (`country: 'NO'`) and write `true` and
  `false` for booleans; every YAML reader then agrees. Write octal as `0o755`, or a file mode as a
  string and convert it with `int(mode, 8)`.
- **Convert after loading.** A date stays a string; `datetime.date.fromisoformat(value)` turns it
  into a date.
- **Turn on merge keys** with `oneyaml.loads(text, merge_keys=True)` for files that use
  `<<: *anchor`, as Compose files and many Kubernetes and CI files do. The default stays YAML 1.2,
  where `<<` is an ordinary key.
- **Build tagged values yourself** with `tags=`: a dict from a tag to a function that gets the
  node's content, such as `{"tag:yaml.org,2002:timestamp": datetime.date.fromisoformat}` for
  values written `!!timestamp 2024-05-01`. A plain `2024-05-01` has no tag and stays a string.

```python
import oneyaml

text = """\
x-defaults: &defaults
  restart: always
services:
  web:
    <<: *defaults
    image: nginx
"""
print(oneyaml.loads(text)["services"])
print(oneyaml.loads(text, merge_keys=True)["services"])
```

```text
{'web': {'<<': {'restart': 'always'}, 'image': 'nginx'}}
{'web': {'restart': 'always', 'image': 'nginx'}}
```

A lossless document (`oneyaml.parse`) always reads `<<` as an ordinary key: you edit the entries
as they are written.

## Writing for both

`oneyaml.dumps` quotes every string that YAML 1.2 or YAML 1.1 would read as something else, so
its output reads back as the same data under both:

```python
import oneyaml
import oneyaml.compat.pyyaml as yaml

text = oneyaml.dumps({"enabled": "yes", "mode": "0755", "duration": "1:30", "count": 3})
print(text, end="")
print(yaml.safe_load(text) == oneyaml.loads(text))
```

```text
enabled: 'yes'
mode: '0755'
duration: '1:30'
count: 3
True
```

An edit to a [document](guide.md#documents-and-edits) judges by YAML 1.2 alone: setting a plain
`edit: yes` to `"no"` writes `edit: no`, which a YAML 1.1 reader takes for `False`. Quote such
values in the file first if YAML 1.1 readers also read it: a quoted `edit: 'yes'` set to `"no"`
stays quoted, `edit: 'no'`.
