# Conformance

How oneyaml scores against the [yaml-test-suite](https://github.com/yaml/yaml-test-suite) and the
oneyaml family's own cases, on each backend, and every case it does not pass.

The cases run through [oneyaml-kit](https://github.com/recursive-ai-oss/oneyaml-kit), which drives
the library through its event stream, `loads_all`, `dumps`, `parse`, `Document.patch` and
`Document.merge_patch`. Each backend is run and scored as a library of its own. The kit's own cases
cover what the suite does not: the Core schema, edits and their exact bytes, patches, limits and
error positions.

## Results

<!-- generated:conformance-line -->
<!-- end generated -->

- **Loose** counts the outcome: the expected events, value or bytes for valid input, an error for
  invalid input.
- **Strict** also checks the error's category and position, and the events produced before it.

## Known deviations

Each case the library does not pass is listed with its reason. The kit fails the run if a case
not on this list fails, or if a listed case starts to pass.

<!-- generated:conformance-deviations -->
<!-- end generated -->

## What the compat modules change

`oneyaml.compat.pyyaml` and `oneyaml.compat.ruamel` read and write as the libraries they stand for,
not as YAML 1.2 Core, so they are not scored here. Their differences from PyYAML and ruamel.yaml
are listed in the migration guides ([PyYAML](migrating-from-pyyaml.md#what-differs),
[ruamel.yaml](migrating-from-ruamel.md#what-differs)) and checked by differential tests against
the real libraries.

## Reproducing the results

From a checkout of [oneyaml-kit](https://github.com/recursive-ai-oss/oneyaml-kit), with this
package installed in the kit's Python environment, its
[running guide](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/running.md) shows
how to run the conformance cases for the `oneyaml-python` and `oneyaml-python-pure` adapters.
