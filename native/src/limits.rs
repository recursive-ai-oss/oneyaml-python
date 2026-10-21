//! The limits a Python `Limits` object hands over.

use oneyaml::{Limits, Settings};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;

static DEFAULT_OBJECT: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

/// Limits as the binding enforces them. A bound of `usize::MAX` means "none".
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Lim {
    pub depth: usize,
    pub nodes_factor: usize,
    pub nodes_floor: usize,
    pub bytes_factor: usize,
    pub bytes_floor: usize,
    pub input: usize,
    pub scalar: usize,
    pub documents: usize,
    pub digits: usize,
}

fn size(v: u64) -> usize {
    usize::try_from(v).unwrap_or(usize::MAX)
}

impl Lim {
    /// The defaults of `oneyaml.Limits()`.
    pub const DEFAULT: Lim = Lim {
        depth: 10_000,
        nodes_factor: 10,
        nodes_floor: 1_000_000,
        bytes_factor: 10,
        bytes_floor: 64 * 1024 * 1024,
        input: usize::MAX,
        scalar: usize::MAX,
        documents: usize::MAX,
        digits: 4300,
    };

    /// The `Limits()` object that stands for the defaults, once the package has registered it.
    /// A call with that very object skips reading its fields: a small document is parsed in
    /// about a microsecond, and reading nine integers out of an object was a tenth of that.
    ///
    /// Only one object is ever held. `Limits` refuses attribute assignment, so its values are
    /// fixed once it is built; code that goes round that with `object.__setattr__` is outside
    /// what the class promises, and a load with the registered object would not see the change.
    ///
    /// The registration is per process, not per interpreter: the first interpreter to import the
    /// module registers its object, and a later interpreter's default object is simply read
    /// field by field as any other limits are. That is slower, never wrong.
    pub fn register_default(limits: &Bound<'_, PyAny>) -> PyResult<bool> {
        // Registered only if it really holds the defaults this build assumes.
        if Lim::from_py(limits)? != Lim::DEFAULT {
            return Ok(false);
        }
        let py = limits.py();
        Ok(DEFAULT_OBJECT
            .get_or_init(py, || limits.clone().unbind())
            .is(limits))
    }

    /// Like `from_py`, with the registered default object recognised by identity.
    #[inline]
    pub fn of(limits: &Bound<'_, PyAny>) -> PyResult<Self> {
        if let Some(d) = DEFAULT_OBJECT.get(limits.py())
            && d.as_ptr() == limits.as_ptr()
        {
            return Ok(Lim::DEFAULT);
        }
        Lim::from_py(limits)
    }

    /// Read the tuple a `Limits` object keeps in `_native`.
    pub fn from_py(limits: &Bound<'_, PyAny>) -> PyResult<Self> {
        let t: (u64, u64, u64, u64, u64, u64, u64, u64, u64) =
            limits.getattr("_native")?.extract()?;
        Ok(Lim {
            depth: size(t.0),
            nodes_factor: size(t.1),
            nodes_floor: size(t.2),
            bytes_factor: size(t.3),
            bytes_floor: size(t.4),
            input: size(t.5),
            scalar: size(t.6),
            documents: size(t.7),
            digits: size(t.8),
        })
    }

    /// The engine settings: Core schema, strict duplicate keys, our bounds for the parser.
    pub fn settings(&self) -> Settings {
        Settings::new().limits(
            Limits::new()
                .max_depth(self.depth)
                .max_document_size(self.input)
                .max_scalar_len(self.scalar)
                .max_documents(self.documents)
                .alias_budget_ratio(self.nodes_factor, self.nodes_floor)
                .alias_byte_budget_ratio(self.bytes_factor, self.bytes_floor),
        )
    }

    /// Nodes an alias expansion may copy in a document of `own` nodes.
    pub fn node_budget(&self, own: usize) -> usize {
        self.nodes_factor.saturating_mul(own).max(self.nodes_floor)
    }

    /// Bytes an alias expansion may copy once `read` bytes of the document are read.
    pub fn byte_budget(&self, read: usize) -> usize {
        self.bytes_factor.saturating_mul(read).max(self.bytes_floor)
    }
}
