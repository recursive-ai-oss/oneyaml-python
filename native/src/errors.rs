//! Failures: what the binding reports for a bad input, and the one place that turns them into
//! Python exceptions.

use std::panic::{AssertUnwindSafe, catch_unwind};

use oneyaml::{ColumnUnit, Diagnostic, LineIndex, code};
use oneyaml_syntax::event::{Event, Sourced};
use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::classes;

/// A failure while reading. `Yaml` is a problem with the input; `Py` is a Python exception
/// that was already raised (a type error, memory exhaustion) and passes through untouched.
pub enum Fail {
    Yaml {
        category: &'static str,
        message: String,
        /// Byte offset into the text being read, when the problem has a place.
        at: Option<usize>,
    },
    Py(PyErr),
}

impl From<PyErr> for Fail {
    fn from(e: PyErr) -> Self {
        Fail::Py(e)
    }
}

impl Fail {
    pub fn new(category: &'static str, message: impl Into<String>, at: usize) -> Self {
        Fail::Yaml {
            category,
            message: message.into(),
            at: Some(at),
        }
    }

    pub fn from_diagnostic(d: &Diagnostic) -> Self {
        Fail::Yaml {
            category: category_of(d),
            message: d.message.clone(),
            at: Some(d.primary.span.start),
        }
    }

    /// The failure an `Error` event stands for.
    pub fn from_event(ev: &Sourced<'_, Event<'_>>) -> Self {
        match ev.diagnostic() {
            Some(d) => Self::from_diagnostic(d),
            None => Fail::new("other", "invalid input", ev.span().start),
        }
    }
}

/// The kit's category for an engine diagnostic.
pub fn category_of(d: &Diagnostic) -> &'static str {
    let c = d.code;
    if c == code::DUPLICATE_KEY {
        return "duplicate-key";
    }
    if c == code::UNDEFINED_TAG_HANDLE {
        return "unresolved-tag";
    }
    match c.get() {
        // Directive problems (a repeated %YAML, say) are errors in the text.
        1..=999 | 1001..=1004 => "syntax",
        2000..=2999 => "limit",
        _ => "other",
    }
}

/// 1-based line and column (columns count characters) and the 0-based character offset of a
/// byte offset into `text`.
pub fn locate(text: &str, at: usize) -> Option<(usize, usize, usize)> {
    // A byte order mark is not a character of the text: columns on its line start after it.
    // A loop, not recursion: the input may open with any number of them.
    let mut text = text;
    let mut at = at;
    while let Some(rest) = text.strip_prefix('\u{feff}') {
        at = at.saturating_sub(text.len() - rest.len());
        text = rest;
    }
    let lc = LineIndex::new(text).line_col(at, ColumnUnit::Char)?;
    let offset = text.get(..at)?.chars().count();
    Some((lc.line + 1, lc.column + 1, offset))
}

/// Build the Python exception for a failure.
pub fn to_pyerr(py: Python<'_>, text: &str, fail: Fail, events: Option<Vec<String>>) -> PyErr {
    match fail {
        Fail::Py(e) => e,
        Fail::Yaml {
            category,
            message,
            at,
        } => {
            let pos = at.and_then(|a| locate(text, a));
            raise(py, &message, category, pos, events)
        }
    }
}

/// The exception class for `category`, built by the Python side so it stays in one place.
pub fn raise(
    py: Python<'_>,
    message: &str,
    category: &str,
    pos: Option<(usize, usize, usize)>,
    events: Option<Vec<String>>,
) -> PyErr {
    let build = || -> PyResult<PyErr> {
        let make = classes::make_error(py)?;
        let (line, column, offset) = match pos {
            Some((l, c, o)) => (Some(l), Some(c), Some(o)),
            None => (None, None, None),
        };
        let events = events.map(|e| PyList::new(py, e)).transpose()?;
        let err = make.call1((message, category, line, column, offset, events))?;
        Ok(PyErr::from_value(err))
    };
    build().unwrap_or_else(|e| e)
}

/// Run a binding entry point so that a Rust panic becomes a `YAMLError` and never unwinds
/// into the interpreter. A panic is a bug in this library; the message says so.
pub fn guarded<T>(py: Python<'_>, f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(r) => r,
        Err(payload) => {
            let what = panic_message(payload.as_ref());
            Err(raise(
                py,
                &format!("internal error in the native backend ({what}); this is a bug"),
                "other",
                None,
                None,
            ))
        }
    }
}

/// The text a panic carried.
pub fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<&str>()
        .map(|s| (*s).to_string())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "unknown panic".to_string())
}
