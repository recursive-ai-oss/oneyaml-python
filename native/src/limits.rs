//! The limits a Python `Limits` object hands over.

use oneyaml::{Limits, Settings};
use pyo3::prelude::*;

/// Limits as the binding enforces them. A bound of `usize::MAX` means "none".
#[derive(Clone, Copy, Debug)]
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
                .max_documents(self.documents),
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
