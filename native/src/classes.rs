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
static CONSTRUCT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static REJECT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static SEXAGESIMAL: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
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

/// The function that builds the values of the YAML 1.1 mode's tags.
pub fn construct<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &CONSTRUCT, "oneyaml._yaml11", "construct")
}

/// The exception that function raises for a value that does not fit its tag.
pub fn reject<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &REJECT, "oneyaml._yaml11", "Reject")
}

/// Base-60 integers too long for 64 bits.
pub fn sexagesimal<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &SEXAGESIMAL, "oneyaml._yaml11", "sexagesimal_int")
}
