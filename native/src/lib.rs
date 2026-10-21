//! Native backend: binds the oneyaml engine to Python.
//!
//! Entry points run under `guarded`, so a Rust panic becomes a `YAMLError` instead of
//! unwinding into the interpreter. The module keeps no mutable global state: every load owns
//! its builder, and the few cached Python classes are write-once cells.

use pyo3::prelude::*;

mod classes;
mod errors;
mod events;
mod input;
mod limits;
mod ops;
mod reader;
mod scalar;

use errors::guarded;

/// Parse a document with the engine and drop it; true when it parsed.
#[pyfunction]
fn probe(data: &[u8]) -> PyResult<bool> {
    let Ok(text) = std::str::from_utf8(data) else {
        return Ok(false);
    };
    Ok(oneyaml::Document::parse(text).is_ok())
}

/// Read YAML into Python objects: one document, or a list of all of them when `multi`.
#[pyfunction]
fn load<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    limits: &Bound<'py, PyAny>,
    multi: bool,
) -> PyResult<Bound<'py, PyAny>> {
    guarded(py, || reader::load(py, data, limits, multi))
}

/// The event stream as yaml-test-suite notation lines.
#[pyfunction]
fn events_text<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, pyo3::types::PyList>> {
    guarded(py, || events::events_text(py, data))
}

/// Run the engine over `data` and count its events, building nothing. This is what a load costs
/// before any Python object exists; the benchmarks use it to size the cost of building them.
#[pyfunction]
fn _parse_only<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<usize> {
    guarded(py, || events::count(py, data))
}

/// Panics on purpose, to show that the guard turns it into a `YAMLError`.
#[pyfunction]
fn _panic_probe(py: Python<'_>) -> PyResult<()> {
    guarded(py, || -> PyResult<()> {
        panic!("deliberate panic from _panic_probe");
    })
}

// The module holds no shared mutable state, so it declares itself safe to use
// without the GIL (free-threaded builds keep the GIL off).
#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("BACKEND", "native")?;
    m.add("ENGINE_VERSION", env!("ONEYAML_ENGINE_VERSION"))?;
    m.add_function(wrap_pyfunction!(probe, m)?)?;
    m.add_function(wrap_pyfunction!(load, m)?)?;
    m.add_function(wrap_pyfunction!(events_text, m)?)?;
    m.add_function(wrap_pyfunction!(_parse_only, m)?)?;
    m.add_function(wrap_pyfunction!(_panic_probe, m)?)?;
    Ok(())
}
