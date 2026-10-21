# Security policy

## Supported versions

Security fixes go into the latest release. Older releases are not patched; upgrade to the latest.

## Reporting a vulnerability

Report it privately through GitHub: open the
[Security tab](https://github.com/recursive-ai-oss/oneyaml-python/security) of this repository
and choose **Report a vulnerability**. Please do not open a public issue or pull request for a
vulnerability.

Include the input or code that shows the problem, the version, the backend
(`python -c "import oneyaml; print(oneyaml.__version__, oneyaml.backend)"`) and what you
expected. A reproduction that is a few lines of YAML or Python is the most useful thing you can
send.

A crash of the interpreter, a hang, or memory use out of proportion to the input is a bug in this
library and a valid report, whether or not you can show it is exploitable.

## What the library promises

- **Nothing is executed.** No tag constructs a Python object or calls code on its own. An unknown
  tag is read as `oneyaml.Tagged`, data only; only a function you pass for that tag in `tags=` is
  called. The PyYAML and ruamel.yaml compat modules keep this: their
  `Loader`, `UnsafeLoader` and `typ="unsafe"` read the same safe types, and a `!!python/...` tag
  is an error.
- **Bounds by default.** Nesting depth, alias expansion and the digits of an integer are bounded
  on every read unless you pass `Limits.none()`. An alias is charged for what a copy of its target
  would hold, so a small input cannot describe an enormous value.
- **More bounds with `Limits.untrusted()`.** Input size, scalar size and documents per stream are
  bounded only when you select `Limits.untrusted()`, which also tightens the default bounds.
  Select it for any input you do not control, and adjust it to your data with `replace`
  ([limits](docs/guide.md#limits-and-untrusted-input)).
- **Errors, not crashes.** Every failure on an input is a `oneyaml.YAMLError`. The native backend
  returns errors from the Rust engine as exceptions, and the pure backend follows nesting with
  explicit stacks, so deep input does not reach Python's recursion limit. An input that crashes
  the interpreter or makes the native code panic is a security bug; report it as above.
- **Edits and patches are all or nothing.** A patch or edit that fails leaves the document
  exactly as it was.

## What it does not promise

These are known limits, not bugs.

- Code you register runs outside the library's guarantees: the functions you pass as `tags`,
  `parse_float` and `default`, constructors, representers and resolvers added through the PyYAML
  compat module, and the `__iter__` or `items` of your own
  subclasses during a dump.
- Without `Limits.untrusted()`, there is no cap on input size, documents per stream or scalar
  length, and memory grows with the input. A `Document` keeps the text and an index of it, which
  takes more memory than the text.
- `Limits.none()` removes every bound, alias expansion included. Never use it on input you do not
  trust.
- `dumps` writes a value that is shared in several places once per place, as `json.dumps` does,
  so its output can be far larger than the value in memory.
