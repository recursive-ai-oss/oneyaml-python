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
static INT_TEXT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TAGGED_TAG: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static TAGGED_VALUE: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
static FROZEN_ITEMS: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
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

pub fn int_text<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    lookup(py, &INT_TEXT, "oneyaml._types", "int_text")
}

/// `Tagged.tag.__get__` (0) or `Tagged.value.__get__` (1): reads the stored slot of a value
/// whose real type is a `Tagged`, whatever a subclass does with attribute lookup.
pub fn tagged_slot<'py>(py: Python<'py>, which: u8) -> PyResult<&'py Bound<'py, PyAny>> {
    let (cell, name) = if which == 0 {
        (&TAGGED_TAG, "tag")
    } else {
        (&TAGGED_VALUE, "value")
    };
    slot_getter(py, cell, "Tagged", name)
}

/// `FrozenMapping._items.__get__`: the dict behind a frozen mapping.
pub fn frozen_items<'py>(py: Python<'py>) -> PyResult<&'py Bound<'py, PyAny>> {
    slot_getter(py, &FROZEN_ITEMS, "FrozenMapping", "_items")
}

fn slot_getter<'py>(
    py: Python<'py>,
    cell: &'static PyOnceLock<Py<PyAny>>,
    class: &str,
    name: &str,
) -> PyResult<&'py Bound<'py, PyAny>> {
    let obj = cell.get_or_try_init(py, || {
        let cls = py.import("oneyaml._types")?.getattr(class)?;
        PyResult::Ok(cls.getattr(name)?.getattr("__get__")?.unbind())
    })?;
    Ok(obj.bind(py))
}
