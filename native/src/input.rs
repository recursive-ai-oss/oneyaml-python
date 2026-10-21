//! The input of a load: text, or bytes decoded per YAML 1.2.2 section 5.2.

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

use crate::errors::{locate, raise};

fn too_large(py: Python<'_>, n: usize, max_input: usize) -> PyErr {
    raise(
        py,
        &format!("the input is {n} bytes; the limit is {max_input}"),
        "limit",
        None,
        None,
    )
}

/// The text of the input, whichever way it came in.
pub fn with_text<'py, T>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    max_input: usize,
    f: impl FnOnce(&str) -> PyResult<T>,
) -> PyResult<T> {
    if let Ok(s) = data.cast::<PyString>() {
        let Ok(text) = s.to_str() else {
            return Err(raise(
                py,
                "the string holds a lone surrogate, which is not text",
                "encoding",
                None,
                None,
            ));
        };
        if text.len() > max_input {
            return Err(too_large(py, text.len(), max_input));
        }
        return f(text);
    }
    if let Ok(b) = data.cast::<PyBytes>() {
        return from_bytes(py, b.as_bytes(), max_input, f);
    }
    // Any other object with a byte buffer (bytearray, memoryview, array of bytes) is read as
    // bytes; the copy leaves the buffer free while the load runs.
    if let Ok(buf) = PyBuffer::<u8>::get(data) {
        let bytes = buf.to_vec(py)?;
        return from_bytes(py, &bytes, max_input, f);
    }
    Err(PyTypeError::new_err(format!(
        "expected str or bytes, not {}",
        data.get_type().name()?
    )))
}

fn from_bytes<T>(
    py: Python<'_>,
    bytes: &[u8],
    max_input: usize,
    f: impl FnOnce(&str) -> PyResult<T>,
) -> PyResult<T> {
    if bytes.len() > max_input {
        return Err(too_large(py, bytes.len(), max_input));
    }
    match oneyaml_syntax::decode_strict(bytes) {
        Ok(d) => f(d.text()),
        Err(e) => {
            let (diag, so_far) = *e;
            Err(encoding_error(
                py,
                &diag.message,
                diag.primary.span.start,
                &so_far,
                None,
            ))
        }
    }
}

/// A decoding error. The engine puts the text decoded up to the bad byte in `so_far`; the error
/// is at the first ill-formed byte, and the column counts the characters before it.
pub fn encoding_error(
    py: Python<'_>,
    message: &str,
    at: usize,
    so_far: &str,
    events: Option<Vec<String>>,
) -> PyErr {
    raise(py, message, "encoding", locate(so_far, at), events)
}
