//! Python-side classes the binding builds values from, looked up once.
//!
//! Each cell is written at most once, under the interpreter's own initialisation lock
//! (`PyOnceLock`), and only read afterwards; that is safe with the GIL off.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;

static MAKE_ERROR: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TAGGED: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static FROZEN_MAPPING: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static FREEZE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static BIG_INT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn lookup<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    module: &str,
    name: &str,
) -> PyResult<&'py Bound<'py, PyAny>> {
    let obj = cell.get_or_try_init(py, || {
        PyResult::Ok(py.import(module)?.getattr(name)?.unbind())
    })?;
    Ok(obj.bind(py))
}

pub fn make_error<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &MAKE_ERROR, "oneyaml._errors", "make_error")
}

pub fn tagged<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &TAGGED, "oneyaml._types", "Tagged")
}

pub fn frozen_mapping<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &FROZEN_MAPPING, "oneyaml._types", "FrozenMapping")
}

pub fn freeze<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &FREEZE, "oneyaml._types", "freeze")
}

pub fn big_int<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &BIG_INT, "oneyaml._types", "big_int")
}
