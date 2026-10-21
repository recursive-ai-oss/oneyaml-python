//! The input of a load: text, or bytes decoded per YAML 1.2.2 section 5.2.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

use crate::errors::{locate, raise};

/// The text of the input, whichever way it came in.
pub fn with_text<'py, T>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    max_input: usize,
    f: impl FnOnce(&str) -> PyResult<T>,
) -> PyResult<T> {
    let too_large = |n: usize| {
        raise(
            py,
            &format!("the input is {n} bytes; the limit is {max_input}"),
            "limit",
            None,
            None,
        )
    };
    if let Ok(s) = data.cast::<PyString>() {
        let text = match s.to_str() {
            Ok(t) => t,
            Err(_) => {
                return Err(raise(
                    py,
                    "the string holds a lone surrogate, which is not text",
                    "encoding",
                    None,
                    None,
                ));
            }
        };
        if text.len() > max_input {
            return Err(too_large(text.len()));
        }
        return f(text);
    }
    if let Ok(b) = data.cast::<PyBytes>() {
        let bytes = b.as_bytes();
        if bytes.len() > max_input {
            return Err(too_large(bytes.len()));
        }
        return match oneyaml_syntax::decode_strict(bytes) {
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
        };
    }
    Err(PyTypeError::new_err(format!(
        "expected str or bytes, not {}",
        data.get_type().name()?
    )))
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
    // A leading byte order mark is not a character of the text.
    let bom = so_far
        .strip_prefix('\u{feff}')
        .map_or(0, |_| '\u{feff}'.len_utf8());
    let pos = so_far
        .get(bom..)
        .and_then(|text| locate(text, at.checked_sub(bom)?));
    raise(py, message, "encoding", pos, events)
}
