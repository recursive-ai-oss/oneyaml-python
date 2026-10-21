//! The lossless document handle: a parsed stream that is read and edited in place.
//!
//! A `Handle` owns the engine's `Stream` behind its own lock, so it is safe to share between
//! threads (free-threaded builds included). Every entry runs under `guarded`, takes the lock
//! with `lock_py_attached` (a thread waiting for the lock lets go of the interpreter, so the
//! thread that holds it can always run Python code), and never calls Python code that could
//! reach back into the same handle while it holds the lock: values to assign are converted
//! before the lock is taken.
//!
//! Nodes are addressed by `(document index, token path)`, never by engine handles, so nothing
//! here goes stale. A token is a `str` (the text of a mapping key, or the decimal index into a
//! sequence), or a non-`str` scalar (`int`, `float`, `bool`, `None`) naming a mapping key of
//! exactly that value, which keeps `1` and `"1"` apart.

use std::borrow::Cow;
use std::collections::HashSet;
use std::ops::{Deref, DerefMut};
use std::sync::{Mutex, MutexGuard};
use std::thread::{self, ThreadId};

use oneyaml::{
    ColumnUnit, Document, DocumentMut, EditError, LineIndex, NodeKind, NodeRef, ScalarStyle,
    Stream, Value,
};
use oneyaml_syntax::decode_strict;
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyIndexError, PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::sync::MutexExt;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyList, PyString};

use crate::classes;
use crate::errors::{Fail, category_of, guarded, locate, raise, to_pyerr};
use crate::input::encoding_error;
use crate::limits::Lim;
use crate::ops::{self, Op, Tag};
use crate::pyvalue;
use crate::reader::{Builder, R};
use crate::scalar::{self, Plain};

/// How deep a collection used as a mapping key may nest (as in a load).
const KEY_DEPTH: usize = 200;

// ---- tokens and lookup -------------------------------------------------------------------------

enum Tok {
    Text(String),
    Int(i64),
    Key(Value),
}

impl Tok {
    fn shown(&self) -> String {
        match self {
            Tok::Text(s) => s.clone(),
            Tok::Int(i) => i.to_string(),
            Tok::Key(v) => format!("{v:?}"),
        }
    }

    fn key_value(&self) -> Value {
        match self {
            Tok::Text(s) => Value::String(s.clone()),
            Tok::Int(i) => Value::from(*i),
            Tok::Key(v) => v.clone(),
        }
    }
}

fn tokens(path: &Bound<'_, PyAny>) -> PyResult<Vec<Tok>> {
    let mut out = Vec::new();
    for item in path.try_iter()? {
        let item = item?;
        out.push(if let Ok(s) = item.cast::<PyString>() {
            Tok::Text(s.to_str()?.to_string())
        } else if let Ok(b) = item.cast::<PyBool>() {
            Tok::Key(Value::Bool(b.is_true()))
        } else if item.is_instance_of::<PyInt>() {
            match item.extract::<i64>() {
                Ok(i) => Tok::Int(i),
                Err(_) => Tok::Key(Value::from(item.extract::<u64>()?)),
            }
        } else if let Ok(f) = item.cast::<PyFloat>() {
            Tok::Key(Value::from(f.value()))
        } else if item.is_none() {
            Tok::Key(Value::Null)
        } else {
            // A mapping key that is itself a collection (a tuple, a `FrozenMapping`), as
            // `keys` hands it out.
            match pyvalue::to_value(item.py(), &item, KEY_DEPTH) {
                Ok(v @ (Value::Sequence(_) | Value::Mapping(_))) => Tok::Key(v),
                _ => {
                    return Err(PyTypeError::new_err(
                        "a path token is a str, int, float, bool, None, tuple or FrozenMapping",
                    ));
                }
            }
        });
    }
    Ok(out)
}

/// Why a path did not lead to a node.
enum Miss {
    Key(String),
    Index(String, usize),
    NotCollection(String),
    ThroughAlias,
    Ambiguous(String),
    NoRoot,
}

impl Miss {
    fn into_err(self, py: Python<'_>) -> PyErr {
        match self {
            Miss::Key(k) => PyKeyError::new_err(k),
            Miss::Index(i, len) => PyIndexError::new_err(format!(
                "index {i} out of range for a sequence of {len} items"
            )),
            Miss::NotCollection(k) => {
                PyKeyError::new_err(format!("{k}: the node is a scalar, not a collection"))
            }
            Miss::NoRoot => PyKeyError::new_err("the document is empty"),
            Miss::ThroughAlias => raise(
                py,
                "the path goes through an alias; edit the anchored node instead",
                "refused-alias",
                None,
                None,
            ),
            Miss::Ambiguous(k) => raise(
                py,
                &format!("more than one key reads as {k}"),
                "refused-ambiguous",
                None,
                None,
            ),
        }
    }
}

type Entry<'d> = (Value, NodeRef<'d>);

/// The entry of a mapping a token names: its key as a value, and the value node.
fn find_entry<'d>(
    doc: &'d Document,
    map: NodeRef<'d>,
    tok: &Tok,
) -> Result<Option<Entry<'d>>, Miss> {
    let _ = doc;
    let Some(m) = map.as_mapping() else {
        return Ok(None);
    };
    match tok {
        Tok::Text(s) => {
            if let Some(v) = m.get(s.as_str()) {
                return Ok(Some((Value::String(s.clone()), v)));
            }
            // A key that is not a string (`1`, `true`, `~`) is named by its text too.
            if matches!(scalar::classify(s), Plain::Str) {
                return Ok(None);
            }
            let want = oneyaml::from_str::<Value>(s).ok();
            let mut hit: Option<Entry<'d>> = None;
            for (k, v) in m.iter() {
                let Some(sc) = k.as_scalar() else { continue };
                let same = sc.value() == s.as_str()
                    || want
                        .as_ref()
                        .is_some_and(|w| sc.resolved().is_ok_and(|r| &r == w));
                if same {
                    if hit.is_some() {
                        return Err(Miss::Ambiguous(s.clone()));
                    }
                    let key = sc.resolved().unwrap_or_else(|_| Value::String(s.clone()));
                    hit = Some((key, v));
                }
            }
            Ok(hit)
        }
        other => {
            let key = other.key_value();
            if matches!(key, Value::Sequence(_) | Value::Mapping(_)) {
                // The engine's index holds scalar keys; a collection key is found by reading.
                for (k, v) in m.iter() {
                    if matches!(k.kind(), NodeKind::Mapping | NodeKind::Sequence)
                        && k.deserialize::<Value>().is_ok_and(|r| r == key)
                    {
                        return Ok(Some((key, v)));
                    }
                }
                return Ok(None);
            }
            Ok(m.get(&key).map(|v| (key, v)))
        }
    }
}

fn seq_index(tok: &Tok) -> Option<usize> {
    match tok {
        Tok::Int(i) => usize::try_from(*i).ok(),
        Tok::Text(s) => {
            let ok = !s.is_empty()
                && s.bytes().all(|b| b.is_ascii_digit())
                && (s == "0" || !s.starts_with('0'));
            if ok { s.parse().ok() } else { None }
        }
        Tok::Key(_) => None,
    }
}

/// Walk `toks` from the root. Reads follow aliases; an edit refuses to pass through one.
fn walk<'d>(doc: &'d Document, toks: &[Tok], follow: bool) -> Result<NodeRef<'d>, Miss> {
    let mut node = doc.root().ok_or(Miss::NoRoot)?;
    for tok in toks {
        if node.kind() == NodeKind::Alias {
            if !follow {
                return Err(Miss::ThroughAlias);
            }
            node = node.resolved();
        }
        node = match node.kind() {
            NodeKind::Mapping => match find_entry(doc, node, tok)? {
                Some((_, v)) => v,
                None => return Err(Miss::Key(tok.shown())),
            },
            NodeKind::Sequence => {
                let s = node.as_sequence().ok_or_else(|| Miss::Key(tok.shown()))?;
                let Some(i) = seq_index(tok) else {
                    return Err(Miss::Key(tok.shown()));
                };
                match s.get(i) {
                    Some(n) => n,
                    None => return Err(Miss::Index(tok.shown(), s.len())),
                }
            }
            _ => return Err(Miss::NotCollection(tok.shown())),
        };
    }
    Ok(node)
}

// ---- engine errors -----------------------------------------------------------------------------

fn edit_fail(e: &EditError) -> Fail {
    let category = match e {
        EditError::AnchorInUse { .. } => "refused-alias",
        EditError::NotFound { .. }
        | EditError::NotAMapping { .. }
        | EditError::NotASequence { .. }
        | EditError::IndexOutOfBounds { .. } => "patch-path",
        EditError::Rejected { diagnostic, .. } => category_of(diagnostic),
        _ => "other",
    };
    Fail::Yaml {
        category,
        message: e.to_string(),
        at: None,
    }
}

/// The kit category for a problem found while parsing a document. The parser reports a few
/// problems of meaning (an alias with no anchor, a tag that does not fit its value) in the
/// range of its late checks; to a caller they are syntax errors, as they are in a load.
fn engine_fail(e: &oneyaml::Error) -> Fail {
    let d = e.diagnostic();
    let category = match d.code.get() {
        1007 | 1008 | 1009 | 1015 => "syntax",
        _ => category_of(d),
    };
    Fail::Yaml {
        category,
        message: d.message.clone(),
        at: Some(d.primary.span.start),
    }
}

// ---- building Python values from nodes ---------------------------------------------------------

enum Work<'d> {
    Node(NodeRef<'d>),
    Close,
}

fn tag_of<'d>(n: NodeRef<'d>) -> R<Tag<'d>> {
    match n.tag() {
        Err(e) => Err(engine_fail(&e)),
        Ok(None) => Ok(Tag::None),
        Ok(Some(t)) => {
            let s = t.to_string();
            Ok(if s == "!" {
                Tag::NonSpecific
            } else if let Some(c) = ops::core_tag(&s) {
                Tag::Core(c)
            } else {
                Tag::Custom(Cow::Owned(s))
            })
        }
    }
}

/// Feed the Python builder the ops for the tree under `start`. An alias to an anchor outside
/// the walk is expanded where it stands (the first time; later ones share it), so a view of a
/// part of a document reads as it would inside the whole.
fn feed<'py, 'd>(
    b: &mut Builder<'py, 'd>,
    defined: &mut HashSet<&'d str>,
    start: NodeRef<'d>,
    cap: usize,
) -> R<()> {
    let mut work = vec![Work::Node(start)];
    while let Some(w) = work.pop() {
        let n = match w {
            Work::Close => {
                b.feed(Op::Close)?;
                continue;
            }
            Work::Node(n) => n,
        };
        let at = n.span().start;
        match n.kind() {
            NodeKind::Alias => {
                let Some(name) = n.alias_name() else {
                    return Err(Fail::new("other", "alias name unavailable", at));
                };
                if defined.contains(name) {
                    b.feed(Op::Alias(name, at))?;
                } else if let Some(t) = n.alias_target() {
                    work.push(Work::Node(t));
                } else {
                    return Err(Fail::new(
                        "syntax",
                        format!("alias *{name} has no anchor"),
                        at,
                    ));
                }
            }
            NodeKind::Scalar => {
                let Some(sc) = n.as_scalar() else {
                    return Err(Fail::new("other", "scalar unavailable", at));
                };
                let anchor = n.anchor();
                if let Some(a) = anchor {
                    defined.insert(a);
                }
                let op = ops::scalar_parts(
                    sc.value(),
                    sc.style() == ScalarStyle::Plain,
                    anchor,
                    tag_of(n)?,
                    at,
                    cap,
                );
                b.feed(op)?;
            }
            kind @ (NodeKind::Mapping | NodeKind::Sequence) => {
                let anchor = n.anchor();
                if let Some(a) = anchor {
                    defined.insert(a);
                }
                let map = kind == NodeKind::Mapping;
                b.feed(ops::open_parts(map, anchor, tag_of(n)?, at))?;
                work.push(Work::Close);
                // Children go on the stack last-first so they are read in document order; a
                // mapping's children alternate key, value.
                let mut children: Vec<NodeRef<'d>> = Vec::new();
                if map {
                    if let Some(m) = n.as_mapping() {
                        for (k, v) in m.iter() {
                            children.push(k);
                            children.push(v);
                        }
                    }
                } else if let Some(s) = n.as_sequence() {
                    children.extend(s.iter());
                }
                work.extend(children.into_iter().rev().map(Work::Node));
            }
            _ => {
                return Err(Fail::new(
                    "syntax",
                    "the node is inside a region the parser rejected",
                    at,
                ));
            }
        }
    }
    Ok(())
}

// ---- the handle --------------------------------------------------------------------------------

struct State {
    stream: Stream,
    version: u64,
    /// The stream's text and its line index, built when a position is first asked for and
    /// dropped by the next edit.
    lines: Option<(String, LineIndex)>,
}

/// The lock on a handle's state, which says who holds it for as long as it does.
struct Held<'a> {
    guard: MutexGuard<'a, State>,
    owner: &'a Mutex<Option<ThreadId>>,
}

impl Deref for Held<'_> {
    type Target = State;
    fn deref(&self) -> &State {
        &self.guard
    }
}

impl DerefMut for Held<'_> {
    fn deref_mut(&mut self) -> &mut State {
        &mut self.guard
    }
}

impl Drop for Held<'_> {
    fn drop(&mut self) {
        *self.owner.lock().unwrap_or_else(|e| e.into_inner()) = None;
    }
}

#[pyclass(frozen, module = "oneyaml._native")]
pub struct Handle {
    state: Mutex<State>,
    /// The thread that holds `state`, so a call that comes back in on that same thread (a
    /// finalizer or callback run while objects are built) is refused instead of waiting for
    /// itself. Held only for an instant, never across a call into Python.
    owner: Mutex<Option<ThreadId>>,
    lim: Lim,
}

fn input_bytes(py: Python<'_>, data: &Bound<'_, PyAny>, max: usize) -> PyResult<Vec<u8>> {
    let bytes: Vec<u8> = if let Ok(s) = data.cast::<PyString>() {
        match s.to_str() {
            Ok(t) => t.as_bytes().to_vec(),
            Err(_) => {
                return Err(raise(
                    py,
                    "the string holds a lone surrogate, which is not text",
                    "encoding",
                    None,
                    None,
                ));
            }
        }
    } else if let Ok(b) = data.cast::<PyBytes>() {
        b.as_bytes().to_vec()
    } else if let Ok(buf) = PyBuffer::<u8>::get(data) {
        buf.to_vec(py)?
    } else {
        return Err(PyTypeError::new_err(format!(
            "expected str or bytes, not {}",
            data.get_type().name()?
        )));
    };
    if bytes.len() > max {
        return Err(raise(
            py,
            &format!("the input is {} bytes; the limit is {max}", bytes.len()),
            "limit",
            None,
            None,
        ));
    }
    Ok(bytes)
}

/// Parse `data` into a lossless document.
#[pyfunction]
pub fn parse(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    limits: &Bound<'_, PyAny>,
) -> PyResult<Handle> {
    guarded(py, || {
        let lim = Lim::from_py(limits)?;
        let bytes = input_bytes(py, data, lim.input)?;
        let settings = lim.settings();
        // The parse touches no Python object, so other threads may run meanwhile.
        let parsed = py.detach(|| Stream::from_slice_with(&bytes, &settings));
        match parsed {
            Ok(stream) => Ok(Handle {
                owner: Mutex::new(None),
                state: Mutex::new(State {
                    stream,
                    version: 0,
                    lines: None,
                }),
                lim,
            }),
            Err(e) => Err(match decode_strict(&bytes) {
                Err(bad) => {
                    let (diag, so_far) = *bad;
                    encoding_error(py, &diag.message, diag.primary.span.start, &so_far, None)
                }
                Ok(d) => to_pyerr(py, d.text(), engine_fail(&e), None),
            }),
        }
    })
}

impl Handle {
    fn lock(&self, py: Python<'_>) -> PyResult<Held<'_>> {
        let me = thread::current().id();
        if *self.owner.lock().unwrap_or_else(|e| e.into_inner()) == Some(me) {
            return Err(raise(
                py,
                "the document is already in use by this thread (a call made from code that runs \
                 while the document is being read)",
                "other",
                None,
                None,
            ));
        }
        let guard = self.state.lock_py_attached(py).map_err(|_| {
            raise(
                py,
                "the document was left inconsistent by an internal error; this is a bug",
                "other",
                None,
                None,
            )
        })?;
        *self.owner.lock().unwrap_or_else(|e| e.into_inner()) = Some(me);
        Ok(Held {
            guard,
            owner: &self.owner,
        })
    }

    fn doc<'a>(st: &'a mut State, d: usize) -> PyResult<DocumentMut<'a>> {
        st.stream
            .document_mut(d)
            .ok_or_else(|| PyIndexError::new_err(format!("no document {d} in the stream")))
    }

    fn fail(py: Python<'_>, f: Fail) -> PyErr {
        to_pyerr(py, "", f, None)
    }

    fn to_value<'py>(&self, py: Python<'py>, v: &Bound<'py, PyAny>) -> PyResult<Value> {
        pyvalue::to_value(py, v, self.lim.depth).map_err(|f| Self::fail(py, f))
    }
}

fn style_name(n: NodeRef<'_>) -> &'static str {
    match n.kind() {
        NodeKind::Alias => "alias",
        NodeKind::Scalar => match n.as_scalar().map(|s| s.style()) {
            Some(ScalarStyle::SingleQuoted) => "single-quoted",
            Some(ScalarStyle::DoubleQuoted) => "double-quoted",
            Some(ScalarStyle::Literal) => "literal",
            Some(ScalarStyle::Folded) => "folded",
            _ => "plain",
        },
        _ => {
            // Skip the properties (`&a`, `!t`) to the first character of the collection.
            let text = n.text();
            let mut rest: &str = text.trim_start();
            while rest.starts_with(['&', '!']) {
                rest = rest
                    .split_once(char::is_whitespace)
                    .map_or("", |(_, r)| r.trim_start());
            }
            if rest.starts_with(['{', '[']) {
                "flow"
            } else {
                "block"
            }
        }
    }
}

fn kind_name(n: NodeRef<'_>) -> &'static str {
    match n.resolved().kind() {
        NodeKind::Mapping => "mapping",
        NodeKind::Sequence => "sequence",
        _ => "scalar",
    }
}

#[pymethods]
impl Handle {
    /// The stream's bytes, in the encoding it was read in.
    fn emit<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        guarded(py, || {
            let st = self.lock(py)?;
            let bytes = st.stream.to_bytes();
            drop(st);
            Ok(PyBytes::new(py, &bytes))
        })
    }

    /// The codec name of the encoding the input used.
    #[getter]
    fn encoding(&self, py: Python<'_>) -> PyResult<&'static str> {
        guarded(py, || {
            let st = self.lock(py)?;
            Ok(match st.stream.document(0).map(|d| d.encoding()) {
                Some(oneyaml::Encoding::Utf16Le) => "utf-16-le",
                Some(oneyaml::Encoding::Utf16Be) => "utf-16-be",
                Some(oneyaml::Encoding::Utf32Le) => "utf-32-le",
                Some(oneyaml::Encoding::Utf32Be) => "utf-32-be",
                _ => "utf-8",
            })
        })
    }

    /// Number of documents in the stream.
    fn count(&self, py: Python<'_>) -> PyResult<usize> {
        guarded(py, || Ok(self.lock(py)?.stream.len()))
    }

    /// Bumps on every edit that can move or replace a collection.
    fn version(&self, py: Python<'_>) -> PyResult<u64> {
        guarded(py, || Ok(self.lock(py)?.version))
    }

    /// `"mapping"`, `"sequence"` or `"scalar"` (aliases read as what they stand for).
    fn kind(&self, py: Python<'_>, d: usize, path: &Bound<'_, PyAny>) -> PyResult<&'static str> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let doc = Self::doc(&mut st, d)?;
            let n = walk(&doc, &toks, true).map_err(|m| m.into_err(py))?;
            Ok(kind_name(n))
        })
    }

    /// The node as plain Python data, as `loads` reads it.
    fn value<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        path: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let doc = Self::doc(&mut st, d)?;
            let n = walk(&doc, &toks, true).map_err(|m| m.into_err(py))?;
            let mut b = Builder::for_walk(py, self.lim, doc.span().start);
            let mut defined = HashSet::new();
            let r = feed(&mut b, &mut defined, n, self.lim.digits);
            if let Err(f) = r {
                let text = st.stream.to_string();
                return Err(to_pyerr(py, &text, f, None));
            }
            match b.take_root() {
                Some(o) => Ok(o),
                None => Ok(py.None().into_bound(py)),
            }
        })
    }

    /// Items of a sequence, entries of a mapping.
    fn length(&self, py: Python<'_>, d: usize, path: &Bound<'_, PyAny>) -> PyResult<usize> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let doc = Self::doc(&mut st, d)?;
            let n = walk(&doc, &toks, true)
                .map_err(|m| m.into_err(py))?
                .resolved();
            if let Some(m) = n.as_mapping() {
                Ok(m.len())
            } else if let Some(s) = n.as_sequence() {
                Ok(s.len())
            } else {
                Err(PyTypeError::new_err("a scalar has no length"))
            }
        })
    }

    /// The keys of a mapping, as Python values (complex keys frozen), in document order.
    fn keys<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        path: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyList>> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let doc = Self::doc(&mut st, d)?;
            let n = walk(&doc, &toks, true)
                .map_err(|m| m.into_err(py))?
                .resolved();
            let Some(m) = n.as_mapping() else {
                return Err(PyTypeError::new_err("only a mapping has keys"));
            };
            let out = PyList::empty(py);
            let mut b = Builder::for_walk(py, self.lim, doc.span().start);
            let mut defined = HashSet::new();
            for (k, _) in m.iter() {
                if let Err(f) = feed(&mut b, &mut defined, k, self.lim.digits) {
                    return Err(Self::fail(py, f));
                }
                let Some(mut key) = b.take_root() else {
                    continue;
                };
                if key.hash().is_err() {
                    key = classes::freeze(py)?
                        .call1((key, i64::MAX, i64::MAX))?
                        .get_item(0)?;
                }
                out.append(key)?;
            }
            Ok(out)
        })
    }

    /// `(style, tag, anchor, comment, line, column)` of the node itself (an alias is not
    /// followed).
    fn info<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        path: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let (style, tag, anchor, comment, at) = {
                let doc = Self::doc(&mut st, d)?;
                let n = walk(&doc, &toks, true).map_err(|m| m.into_err(py))?;
                let tag = match n.tag() {
                    Ok(t) => t.map(|t| t.to_string()),
                    Err(e) => return Err(Self::fail(py, engine_fail(&e))),
                };
                let mut comment = None;
                if let Ok(c) = doc.comments(n.id()) {
                    let pick = |list: &[oneyaml::Comment]| {
                        let v: Vec<&str> = list.iter().map(|c| c.raw()).collect();
                        (!v.is_empty()).then(|| v.join("\n"))
                    };
                    comment = pick(c.trailing()).or_else(|| pick(c.leading()));
                }
                (
                    style_name(n),
                    tag,
                    n.anchor().map(str::to_string),
                    comment,
                    n.span().start,
                )
            };
            if st.lines.is_none() {
                let text = st.stream.to_string();
                let bare = text.trim_start_matches('\u{feff}').len();
                let skip = text.len() - bare;
                let index = LineIndex::new(&text[skip..]);
                st.lines = Some((text, index));
            }
            let pos = st.lines.as_ref().and_then(|(text, index)| {
                let skip = text.len() - text.trim_start_matches('\u{feff}').len();
                let rel = at.saturating_sub(skip);
                index
                    .line_col(rel, ColumnUnit::Char)
                    .map(|lc| (lc.line + 1, lc.column + 1))
            });
            let _ = locate;
            let (line, column) = pos.unzip();
            Ok((style, tag, anchor, comment, line, column)
                .into_pyobject(py)?
                .into_any())
        })
    }

    /// Assign `value` at `path` (an existing node is replaced, a missing mapping key added).
    fn set(
        &self,
        py: Python<'_>,
        d: usize,
        path: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let toks = tokens(path)?;
        let v = self.to_value(py, value)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let structural = {
                let mut doc = Self::doc(&mut st, d)?;
                set_at(&mut doc, &toks, &v, py)?
            };
            changed(&mut st, structural);
            Ok(())
        })
    }

    /// Insert `value` before item `index` of the sequence at `path`; an index past the end
    /// appends (so an append is one atomic call).
    fn insert(
        &self,
        py: Python<'_>,
        d: usize,
        path: &Bound<'_, PyAny>,
        index: usize,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let toks = tokens(path)?;
        let v = self.to_value(py, value)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let structural = {
                let mut doc = Self::doc(&mut st, d)?;
                let (id, len) = {
                    let n = walk(&doc, &toks, false).map_err(|m| m.into_err(py))?;
                    if n.kind() == NodeKind::Alias {
                        return Err(Miss::ThroughAlias.into_err(py));
                    }
                    let Some(s) = n.as_sequence() else {
                        return Err(PyTypeError::new_err("only a sequence takes insert"));
                    };
                    (n.id(), s.len())
                };
                // As `list.insert`: a position past the end appends.
                let shifts = index < len;
                let index = index.min(len);
                let mut node = doc
                    .node_mut(id)
                    .map_err(|e| Self::fail(py, edit_fail(&e)))?;
                node.insert_at(index, &v)
                    .map_err(|e| Self::fail(py, edit_fail(&e)))?;
                shifts
            };
            changed(&mut st, structural);
            Ok(())
        })
    }

    /// Remove the node at `path` from its mapping or sequence.
    fn remove(&self, py: Python<'_>, d: usize, path: &Bound<'_, PyAny>) -> PyResult<()> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            {
                let mut doc = Self::doc(&mut st, d)?;
                let Some((last, parents)) = toks.split_last() else {
                    return Err(PyTypeError::new_err(
                        "the root of a document cannot be removed",
                    ));
                };
                let parent = walk(&doc, parents, false).map_err(|m| m.into_err(py))?;
                if parent.kind() == NodeKind::Alias {
                    return Err(Miss::ThroughAlias.into_err(py));
                }
                if parent.kind() == NodeKind::Mapping {
                    let Some((_, value)) =
                        find_entry(&doc, parent, last).map_err(|m| m.into_err(py))?
                    else {
                        return Err(Miss::Key(last.shown()).into_err(py));
                    };
                    // By the value's node: that works for every kind of key.
                    let id = value.id();
                    let _ = doc
                        .remove_node(id)
                        .map_err(|e| Self::fail(py, edit_fail(&e)))?;
                } else if let Some(s) = parent.as_sequence() {
                    let len = s.len();
                    let Some(i) = seq_index(last) else {
                        return Err(Miss::Key(last.shown()).into_err(py));
                    };
                    if i >= len {
                        return Err(Miss::Index(last.shown(), len).into_err(py));
                    }
                    let id = s
                        .get(i)
                        .map(|n| n.id())
                        .ok_or_else(|| Miss::Index(last.shown(), len).into_err(py))?;
                    let _ = doc
                        .remove_node(id)
                        .map_err(|e| Self::fail(py, edit_fail(&e)))?;
                } else {
                    return Err(Miss::NotCollection(last.shown()).into_err(py));
                }
            }
            changed(&mut st, true);
            Ok(())
        })
    }
}

fn changed(st: &mut State, structural: bool) {
    st.lines = None;
    if structural {
        st.version += 1;
    }
}

/// Whether a node is, or holds, a collection: replacing it can strand views of it.
fn is_collection(n: NodeRef<'_>) -> bool {
    matches!(n.kind(), NodeKind::Mapping | NodeKind::Sequence)
}

/// Returns whether the edit replaced a collection.
fn set_at(doc: &mut DocumentMut<'_>, toks: &[Tok], v: &Value, py: Python<'_>) -> PyResult<bool> {
    let efail = |e: EditError| Handle::fail(py, edit_fail(&e));
    let collection_value = matches!(v, Value::Mapping(_) | Value::Sequence(_));
    let Some((last, parents)) = toks.split_last() else {
        // The root.
        return match doc.root().map(|r| (r.id(), is_collection(r))) {
            Some((id, was)) => {
                doc.node_mut(id).map_err(efail)?.set(v).map_err(efail)?;
                Ok(was || collection_value)
            }
            None => {
                doc.set_root(v).map_err(efail)?;
                Ok(false)
            }
        };
    };
    let parent = walk(doc, parents, false).map_err(|m| m.into_err(py))?;
    let pid = parent.id();
    match parent.kind() {
        NodeKind::Mapping => {
            let found = find_entry(doc, parent, last).map_err(|m| m.into_err(py))?;
            match found {
                Some((_, node)) => {
                    let (id, was) = (node.id(), is_collection(node));
                    doc.node_mut(id).map_err(efail)?.set(v).map_err(efail)?;
                    Ok(was || collection_value)
                }
                None => {
                    let mut p = doc.node_mut(pid).map_err(efail)?;
                    match last {
                        Tok::Text(s) => p.insert(s.as_str(), v),
                        other => p.insert(other.key_value(), v),
                    }
                    .map_err(efail)?;
                    Ok(false)
                }
            }
        }
        NodeKind::Sequence => {
            let s = parent
                .as_sequence()
                .ok_or_else(|| Miss::Key(last.shown()).into_err(py))?;
            let len = s.len();
            let Some(i) = seq_index(last) else {
                return Err(Miss::Key(last.shown()).into_err(py));
            };
            let Some(item) = s.get(i) else {
                return Err(Miss::Index(last.shown(), len).into_err(py));
            };
            let was = is_collection(item);
            doc.node_mut(pid)
                .map_err(efail)?
                .replace_at(i, v)
                .map_err(efail)?;
            Ok(was || collection_value)
        }
        NodeKind::Alias => Err(Miss::ThroughAlias.into_err(py)),
        _ => Err(Miss::NotCollection(last.shown()).into_err(py)),
    }
}
