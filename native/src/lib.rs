//! Native backend: binds the oneyaml engine to Python.

use pyo3::prelude::*;

/// Parse a document with the engine and drop it; true when it parsed.
#[pyfunction]
fn probe(data: &[u8]) -> PyResult<bool> {
    let Ok(text) = std::str::from_utf8(data) else {
        return Ok(false);
    };
    Ok(oneyaml::Document::parse(text).is_ok())
}

// The module holds no shared mutable state, so it declares itself safe to use
// without the GIL (free-threaded builds keep the GIL off).
#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("BACKEND", "native")?;
    m.add("ENGINE_VERSION", env!("ONEYAML_ENGINE_VERSION"))?;
    m.add_function(wrap_pyfunction!(probe, m)?)?;
    Ok(())
}
