## What this changes

<!-- One or two sentences: the problem and the fix. Link the issue if there is one. -->

## Checks

- [ ] `GATE_WORK=<scratch dir> python tools/gate.py` passes
- [ ] A test fails without this change, and it runs on both backends (or the change is documentation only)
- [ ] New or changed public functions and classes have docstrings; new or changed samples in `README.md` and `docs/` pass `tests/test_docs_samples.py`
- [ ] `CHANGELOG.md` has a line under Unreleased for anything a user can see (breaking changes marked)
- [ ] No new runtime dependency

## Performance

<!-- For a change to a hot path: timings before and after, with the command you ran. Otherwise delete this section. -->
