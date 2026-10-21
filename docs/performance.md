# Performance

Time and memory of oneyaml, on each backend, against the other Python YAML libraries, on the same
tasks and inputs, measured with the benchmark of
[oneyaml-kit](https://github.com/recursive-ai-oss/oneyaml-kit). Every task and every loss is on
this page.

## Results at a glance

<!-- generated:bench-hero -->
<!-- end generated -->

## All results

Every workload, with time and then memory.

<!-- generated:bench-full -->
<!-- end generated -->

## Method

### About this run

The library revision, the competitors and their versions, the machine, the operating system, the
Python version and the date are in the method line under each table above, written by the kit
from the run itself.

### Tasks

Each competitor does the same work through its own API: loading into plain Python data, dumping
it, and, for the libraries that keep a document, parsing it losslessly, writing it back unchanged,
an edit script and a patch. A library that cannot do a task has no cell for it, and a cell is
compared only with libraries that did the same work. The kit's
[task table](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/benchmarks.md#tasks)
defines each one: the starting state, the timed work and what the answer must be.

The native and pure backends are measured as two libraries.

### Workloads

Real files, vendored byte for byte with their source and licence, and generated inputs labelled as
synthetic. The kit's
[workload list](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/benchmarks.md#workloads)
names each one.

### The correctness check before timing

Each answer is checked before it is timed, and only a correct answer gets a number: the loaded
value, the dumped text read back by two independent parsers, the exact bytes of an edit or patch.
A wrong answer is marked "differs" and a refused input "rejects", with what the check found
([correctness guard](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/benchmarks.md#correctness-guard)).

### Time and memory

Time is pyperf's mean time per run over its worker processes. Memory is the peak growth of the
resident set during one run, in a fresh process where Python, C and Rust allocations share one
heap, because a Python allocation tracer does not see memory allocated by native code. The same
method applies to every library ([metrics](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/benchmarks.md#metrics)).

### Reproducing the run

From a checkout of [oneyaml-kit](https://github.com/recursive-ai-oss/oneyaml-kit) at the revision
in the method line, with this package installed, its
[running guide](https://github.com/recursive-ai-oss/oneyaml-kit/blob/main/docs/running.md) gives
the command for a full Python run, and `bin/bench readme` writes the tables and charts of this page
and the chart in the README from the result.
