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
use std::collections::HashMap;
use std::ops::{Deref, DerefMut};
use std::sync::{Mutex, MutexGuard};
use std::thread::{self, ThreadId};

use oneyaml::{
    ColumnUnit, Document, DocumentMut, EditError, LineIndex, MappingRef, MergePatch, NodeId,
    NodeKind, NodeRef, Patch, PatchErrorKind, PatchOp, ScalarStyle, Stream, Value,
};
use oneyaml_syntax::decode_strict;
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyIndexError, PyKeyError, PyTypeError};
use pyo3::prelude::*;
use pyo3::sync::MutexExt;
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyList, PyString};

use crate::classes;
use crate::errors::{Fail, category_of, guarded, raise, to_pyerr};
use crate::input::encoding_error;
use crate::limits::Lim;
use crate::ops::{self, Op, Tag};
use crate::pyvalue;
use crate::reader::{Builder, R};

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
            Tok::Key(v) => match v {
                Value::Null => "None".to_string(),
                Value::Bool(true) => "True".to_string(),
                Value::Bool(false) => "False".to_string(),
                Value::Number(n) => n.to_string(),
                Value::String(s) => s.clone(),
                _ => "a collection".to_string(),
            },
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
            match s.to_str() {
                Ok(t) => Tok::Text(t.to_string()),
                // No key holds a lone surrogate, so no key is named by it.
                Err(_) => return Err(PyKeyError::new_err(s.to_string_lossy().into_owned())),
            }
        } else if let Ok(b) = item.cast::<PyBool>() {
            Tok::Key(Value::Bool(b.is_true()))
        } else if item.is_instance_of::<PyInt>() {
            match pyvalue::int_text(&item) {
                Ok(text) => match text.parse::<i64>() {
                    Ok(i) => Tok::Int(i),
                    // Wider than 64 bits: the key is built from the decimal digits.
                    Err(_) => match pyvalue::big_int(&text) {
                        Ok(v) => Tok::Key(v),
                        Err(_) => return Err(PyKeyError::new_err(text)),
                    },
                },
                // Beyond the digit cap Python itself sets: no key is that long.
                Err(e) if e.is_instance_of::<pyo3::exceptions::PyValueError>(item.py()) => {
                    return Err(PyKeyError::new_err("an integer with too many digits"));
                }
                Err(_) => return Err(PyTypeError::new_err("an integer token has no digits")),
            }
        } else if let Ok(f) = item.cast::<PyFloat>() {
            Tok::Key(Value::from(f.value()))
        } else if item.is_none() {
            Tok::Key(Value::Null)
        } else {
            // A mapping key that is itself a collection (a tuple, a `FrozenMapping`), as
            // `keys` hands it out.
            match pyvalue::to_value(
                item.py(),
                &item,
                &Lim {
                    depth: KEY_DEPTH,
                    ..Lim::DEFAULT
                },
            ) {
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
        }
    }
}

/// The longest source text, in bytes, of a mapping this reads itself. An entry takes at least
/// five bytes (`a: 1` and a line break), so a mapping this short has fewer than 32 entries,
/// which is as many as the engine reads by scanning: from 64 nodes on it keeps a key index that
/// survives edits, and a lookup through that is cheaper than any scan of ours. Counting entries
/// to tell the two apart would cost more than the scan saves; the length is known without
/// reading any.
const SHORT: usize = 120;

/// Whether `s`, written as a plain scalar, always reads as that very string: it starts with a
/// letter, has only letters, digits and underscores, and is not one of the words either schema
/// reads as null, a boolean or a number.
fn plain_word(s: &str) -> bool {
    let b = s.as_bytes();
    b.first().is_some_and(u8::is_ascii_alphabetic)
        && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'_')
        && ["null", "true", "false", "yes", "no", "on", "off", "y", "n"]
            .iter()
            .all(|w| !s.eq_ignore_ascii_case(w))
}

/// A lookup by text in a short mapping that reads the keys itself instead of asking the
/// engine, whose scan of a mapping resolves every key it passes to a value (a few
/// thousand instructions per key). `Some(answer)` is the engine's answer; `None` means this
/// could not tell and the engine must be asked.
///
/// It answers only for a word that is the same string however it is written, and only where
/// that is plain to see: every key before the one that reads as the word is a scalar and not
/// the merge key, and that one carries no tag. The first such key is the answer (a stream with
/// duplicate keys is refused when it is read, and an edit never makes one). A mapping
/// too long to be sure of is left to the engine, so no key read here is large and no lookup in
/// a mapping the engine indexes pays for a scan.
fn near_get<'d>(m: MappingRef<'d>, s: &str) -> Option<Option<NodeRef<'d>>> {
    let span = m.node().span();
    if span.end - span.start > SHORT || !plain_word(s) {
        return None;
    }
    for (k, v) in m.iter() {
        let sc = if k.kind() == NodeKind::Scalar {
            k.as_scalar()?
        } else {
            return None; // an alias, a collection: leave it to the engine
        };
        let text = sc.value();
        if text == s {
            let plain_or_quoted = matches!(
                sc.style(),
                ScalarStyle::Plain | ScalarStyle::SingleQuoted | ScalarStyle::DoubleQuoted
            );
            return (plain_or_quoted && matches!(k.tag(), Ok(None))).then_some(Some(v));
        } else if text == "<<" {
            return None;
        }
    }
    Some(None)
}

type Entry<'d> = (Value, NodeRef<'d>);

/// The entry of a mapping a token names: its key as a value, and the value node.
fn find_entry<'d>(map: NodeRef<'d>, tok: &Tok) -> Result<Option<Entry<'d>>, Miss> {
    let Some(m) = map.as_mapping() else {
        return Ok(None);
    };
    match tok {
        Tok::Text(s) => {
            if let Some(found) = near_get(m, s) {
                return Ok(found.map(|v| (Value::String(s.clone()), v)));
            }
            if let Some(v) = m.get(s.as_str()) {
                return Ok(Some((Value::String(s.clone()), v)));
            }
            // A view token is exact, as a dict key is: `"1"` does not name the key `1`.
            Ok(None)
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

/// Why a token found no item of a sequence of `len` items: an integer (however large) is a
/// position past the end, anything else is not a position.
fn seq_miss(tok: &Tok, len: usize) -> Miss {
    let integer = match tok {
        Tok::Int(_) => true,
        Tok::Key(Value::Number(n)) => n.is_integer(),
        Tok::Text(s) => {
            !s.is_empty()
                && s.bytes().all(|b| b.is_ascii_digit())
                && (s == "0" || !s.starts_with('0'))
        }
        Tok::Key(_) => false,
    };
    if integer {
        Miss::Index(tok.shown(), len)
    } else {
        Miss::Key(tok.shown())
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
    walk_from_end(doc, toks, follow, false)
}

/// `walk`, where `from_end` lets a negative integer count a sequence from its end (as a Python
/// list does).
fn walk_from_end<'d>(
    doc: &'d Document,
    toks: &[Tok],
    follow: bool,
    from_end: bool,
) -> Result<NodeRef<'d>, Miss> {
    let mut node = doc.root().ok_or(Miss::NoRoot)?;
    for tok in toks {
        if node.kind() == NodeKind::Alias {
            if !follow {
                return Err(Miss::ThroughAlias);
            }
            node = node.resolved();
        }
        node = match node.kind() {
            NodeKind::Mapping => match find_entry(node, tok)? {
                Some((_, v)) => v,
                None => return Err(Miss::Key(tok.shown())),
            },
            NodeKind::Sequence => {
                let s = node.as_sequence().ok_or_else(|| Miss::Key(tok.shown()))?;
                let counted_back = match tok {
                    Tok::Int(i) if from_end && *i < 0 => s
                        .len()
                        .checked_sub(usize::try_from(i.unsigned_abs()).unwrap_or(usize::MAX)),
                    _ => None,
                };
                let Some(i) = seq_index(tok).or(counted_back) else {
                    return Err(seq_miss(tok, s.len()));
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

/// The indentation of the block that holds the block scalar whose indicator (`|` or `>`) is at
/// `at`: the column of the mapping key or of the `-` the scalar follows, -1 at the top of a
/// document. Read from the text of the header line, which is all it takes.
fn block_parent_indent(text: &str, at: usize) -> i64 {
    let mut ls = text[..at].rfind(['\n', '\r']).map_or(0, |i| i + 1);
    if ls == 0 && text.starts_with('\u{feff}') {
        ls = '\u{feff}'.len_utf8();
    }
    let line = &text[ls..at];
    let lb = line.as_bytes();
    if line.starts_with("---") && lb.get(3).is_none_or(|c| matches!(c, b' ' | b'\t')) {
        return -1;
    }
    let mut p = 0;
    let mut last = -1;
    loop {
        while lb.get(p) == Some(&b' ') {
            p += 1;
        }
        match lb.get(p) {
            Some(b'-' | b'?' | b':') if lb.get(p + 1).is_none_or(|c| matches!(c, b' ' | b'\t')) => {
                last = line[..p].chars().count() as i64;
                p += 1;
            }
            _ => break,
        }
    }
    // Only properties between the indicators and the scalar: the scalar is the item itself.
    if line[p..]
        .split_whitespace()
        .all(|t| t.starts_with('&') || t.starts_with('!'))
    {
        return last;
    }
    line[..p].chars().count() as i64
}

/// Where a block scalar ends: just after its last content character. The spaces that make up the
/// indentation of a trailing line, and the line breaks, are not content; the scalar's own
/// header is its end when it has no content at all. `at` is the position of its indicator.
fn block_scalar_end(text: &str, at: usize) -> usize {
    let b = text.as_bytes();
    let mut i = at + 1;
    let mut digit = 0usize;
    let mut taken = 0;
    while taken < 2 {
        match b.get(i) {
            Some(c @ b'1'..=b'9') if digit == 0 => digit = usize::from(c - b'0'),
            Some(b'+' | b'-') => {}
            _ => break,
        }
        i += 1;
        taken += 1;
    }
    let header_end = i;
    let line_end = |from: usize| {
        text[from..]
            .find(['\n', '\r'])
            .map_or(text.len(), |k| from + k)
    };
    let next_line = |end: usize| {
        if b.get(end) == Some(&b'\r') && b.get(end + 1) == Some(&b'\n') {
            end + 2
        } else {
            (end + 1).min(b.len())
        }
    };
    let first_line = line_end(header_end);
    if first_line >= b.len() {
        return header_end;
    }
    let body = next_line(first_line);
    let parent = block_parent_indent(text, at);
    let indent = if digit > 0 {
        parent + digit as i64
    } else {
        // The first line with something other than spaces sets the indentation.
        let mut j = body;
        let mut first = None;
        while j < b.len() {
            let e = line_end(j);
            let seg = &text[j..e];
            if !seg.bytes().all(|c| c == b' ') {
                first = Some((seg.len() - seg.trim_start_matches(' ').len()) as i64);
                break;
            }
            j = next_line(e);
        }
        match first {
            Some(f) if f > parent => f,
            _ => return header_end,
        }
    };
    let indent = usize::try_from(indent).unwrap_or(0);
    let mut last = None;
    let mut j = body;
    while j < b.len() {
        let e = line_end(j);
        let seg = &text[j..e];
        let marker = indent == 0
            && (seg.starts_with("---") || seg.starts_with("..."))
            && seg
                .as_bytes()
                .get(3)
                .is_none_or(|c| matches!(c, b' ' | b'\t'));
        let blank = seg.bytes().all(|c| c == b' ');
        if marker || !(blank || seg.bytes().take(indent).filter(|c| *c == b' ').count() == indent) {
            break;
        }
        if seg.len() > indent {
            last = Some(e);
        }
        if e >= b.len() {
            break;
        }
        j = next_line(e);
    }
    last.unwrap_or(header_end)
}

/// The position of the `|` or `>` of a block scalar whose node starts at `start` (properties
/// such as `&a` or `!t` may come first).
fn block_indicator(text: &str, start: usize) -> Option<usize> {
    let b = text.as_bytes();
    let mut i = start;
    loop {
        while matches!(b.get(i), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            i += 1;
        }
        match b.get(i) {
            Some(b'|' | b'>') => return Some(i),
            Some(b'&' | b'!') => {
                while b
                    .get(i)
                    .is_some_and(|c| !matches!(c, b' ' | b'\t' | b'\r' | b'\n'))
                {
                    i += 1;
                }
            }
            _ => return None,
        }
    }
}

/// The last scalar a node ends with, following the last entry or item of collections.
fn last_leaf(mut n: NodeRef<'_>) -> NodeRef<'_> {
    loop {
        let next = match n.kind() {
            NodeKind::Mapping => n.as_mapping().and_then(|m| m.iter().last()).map(|(_, v)| v),
            NodeKind::Sequence => n.as_sequence().and_then(|s| s.iter().last()),
            _ => None,
        };
        match next {
            Some(c) => n = c,
            None => return n,
        }
    }
}

/// The comment a node owns, as written: trailing comments, else leading ones. A mapping entry
/// keeps its comments with its key, so the value of an entry reads those first, then the ones
/// the engine attached to the value itself (a comment above a value on its own line, or above
/// the `:` of an explicit entry). The root also has the document's header.
fn comment_of(doc: &Document, n: NodeRef<'_>, is_root: bool) -> Option<String> {
    let pick = |list: &[oneyaml::Comment]| {
        let v: Vec<&str> = list.iter().map(|c| c.raw()).collect();
        (!v.is_empty()).then(|| v.join("\n"))
    };
    let of = |id| {
        doc.comments(id)
            .ok()
            .and_then(|c| pick(c.trailing()).or_else(|| pick(c.leading())))
    };
    let mut comment = n.key().and_then(|k| of(k.id()));
    if comment.is_none() {
        comment = of(n.id());
    }
    if comment.is_none()
        && is_root
        && let Ok(h) = doc.header()
    {
        comment = pick(&h);
    }
    comment
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
    /// A node whose scope is already chosen: an outside anchor built where it is first needed.
    Build(NodeRef<'d>, u32),
    Close,
    /// Resume what was set aside to build an outside anchor.
    Reattach,
    /// Share a node that is already built.
    Share {
        name: &'d str,
        scope: u32,
        at: usize,
        charged: bool,
    },
}

/// What a walk has built so far: every anchored node gets a scope of its own, and an alias
/// finds its target through it, so the name an anchor happens to share with another never
/// matters.
#[derive(Default)]
struct Walk {
    scopes: HashMap<NodeId, u32>,
    last: u32,
    /// Nodes in the document, once counted. Only an alias needs the figure (its budget is sized
    /// from it), so a walk without aliases never pays for the count.
    nodes: Option<usize>,
}

impl Walk {
    fn fresh(&mut self, id: NodeId) -> u32 {
        self.last += 1;
        self.scopes.insert(id, self.last);
        self.last
    }
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

/// Feed the Python builder the ops for the tree under `start`.
///
/// An alias to an anchor outside the walk (a view of part of a document) is answered as `loads`
/// answers it: the anchored node is built once, where it is first needed, and every alias to it
/// shares that object and is charged for what a copy would hold.
fn feed<'py, 'd>(
    b: &mut Builder<'py, 'd>,
    w: &mut Walk,
    start: NodeRef<'d>,
    cap: usize,
    count: &dyn Fn() -> usize,
) -> R<()> {
    let mut work = vec![Work::Node(start)];
    while let Some(item) = work.pop() {
        let (n, forced) = match item {
            Work::Close => {
                b.feed(Op::Close)?;
                continue;
            }
            Work::Reattach => {
                b.reattach();
                continue;
            }
            Work::Share {
                name,
                scope,
                at,
                charged,
            } => {
                if w.nodes.is_none() {
                    let total = count();
                    w.nodes = Some(total);
                    b.set_min_nodes(total);
                }
                b.set_scope(scope);
                b.set_free(!charged);
                let r = b.feed(Op::Alias(name, at));
                b.set_free(false);
                r?;
                continue;
            }
            Work::Node(n) => (n, None),
            Work::Build(n, scope) => (n, Some(scope)),
        };
        let at = n.span().start;
        if n.kind() == NodeKind::Alias {
            let Some(name) = n.alias_name() else {
                return Err(Fail::new("other", "alias name unavailable", at));
            };
            let Some(target) = n.alias_target() else {
                return Err(Fail::new(
                    "syntax",
                    format!("alias *{name} has no anchor"),
                    at,
                ));
            };
            let tid = target.id();
            if let Some(&scope) = w.scopes.get(&tid) {
                work.push(Work::Share {
                    name,
                    scope,
                    at,
                    charged: true,
                });
            } else {
                let scope = w.fresh(tid);
                work.push(Work::Share {
                    name,
                    scope,
                    at,
                    charged: true,
                });
                work.push(Work::Reattach);
                work.push(Work::Build(target, scope));
                b.detach();
            }
            continue;
        }
        // A node that carries an anchor is registered under a scope of its own. One met a second
        // time (an outside anchor reached again inside another) is shared, not built again.
        let anchor = n.anchor();
        let scope = match (anchor, forced) {
            (None, _) => None,
            (Some(_), Some(scope)) => Some(scope),
            (Some(name), None) => {
                if let Some(&scope) = w.scopes.get(&n.id()) {
                    work.push(Work::Share {
                        name,
                        scope,
                        at,
                        charged: false,
                    });
                    continue;
                }
                Some(w.fresh(n.id()))
            }
        };
        if let Some(scope) = scope {
            b.set_scope(scope);
        }
        match n.kind() {
            NodeKind::Scalar => {
                let Some(sc) = n.as_scalar() else {
                    return Err(Fail::new("other", "scalar unavailable", at));
                };
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

/// Nodes in the document: the figure its alias budgets are sized from.
fn count_nodes(doc: &Document) -> usize {
    let Some(root) = doc.root() else { return 0 };
    let mut count = 0usize;
    let mut stack = vec![root];
    while let Some(n) = stack.pop() {
        count += 1;
        // An alias answers `as_mapping` for what it names; it is one node here.
        if n.kind() == NodeKind::Alias {
            continue;
        }
        if let Some(m) = n.as_mapping() {
            for (k, v) in m.iter() {
                stack.push(k);
                stack.push(v);
            }
        } else if let Some(s) = n.as_sequence() {
            stack.extend(s.iter());
        }
    }
    count
}

// ---- the handle --------------------------------------------------------------------------------

struct State {
    stream: Stream,
    version: u64,
    /// The stream's text and its line index, built when a position is first asked for and
    /// dropped by the next edit.
    lines: Option<(String, LineIndex)>,
    /// Nodes per document, counted when first needed and dropped by the next edit.
    nodes: HashMap<usize, usize>,
    /// The codec the input was read in, fixed at parse time (a stream may have no documents).
    encoding: &'static str,
    /// The input was UTF-8, which a stream is only built from when it is valid: its bytes are
    /// then exactly its text, which the syntax tree can write out in runs. Any other encoding
    /// goes through the engine's own writer.
    text_is_bytes: bool,
    /// Whether an edit has ever been tried: until then the syntax tree's source is the text.
    touched: bool,
}

impl State {
    /// The stream's current bytes, built from its text.
    #[inline(never)]
    fn written_bytes(&self) -> Vec<u8> {
        if !self.text_is_bytes {
            return self.stream.to_bytes();
        }
        let tree = self.stream.syntax();
        // Never touched: the text is the source itself. Touched: the tree copies its runs.
        if !self.touched {
            tree.source().as_bytes().to_vec()
        } else {
            tree.emit().into_bytes()
        }
    }
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
            Ok(stream) => {
                let encoding = match stream.document(0).map(|d| d.encoding()) {
                    Some(oneyaml::Encoding::Utf16Le) => "utf-16-le",
                    Some(oneyaml::Encoding::Utf16Be) => "utf-16-be",
                    Some(oneyaml::Encoding::Utf32Le) => "utf-32-le",
                    Some(oneyaml::Encoding::Utf32Be) => "utf-32-be",
                    Some(_) => "utf-8",
                    // No document to ask: read it off the first bytes, as YAML 1.2.2 section 5.2.
                    None => detect_encoding(&bytes),
                };
                Ok(Handle {
                    owner: Mutex::new(None),
                    state: Mutex::new(State {
                        stream,
                        version: 0,
                        lines: None,
                        nodes: HashMap::new(),
                        encoding,
                        text_is_bytes: encoding == "utf-8",
                        touched: false,
                    }),
                    lim,
                })
            }
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

/// The encoding of a stream from its first bytes (YAML 1.2.2 section 5.2).
fn detect_encoding(bytes: &[u8]) -> &'static str {
    match bytes {
        [0, 0, 0xFE, 0xFF, ..] | [0, 0, 0, _, ..] => "utf-32-be",
        [0xFF, 0xFE, 0, 0, ..] | [_, 0, 0, 0, ..] => "utf-32-le",
        [0xFE, 0xFF, ..] | [0, _, ..] => "utf-16-be",
        [0xFF, 0xFE, ..] | [_, 0, ..] => "utf-16-le",
        _ => "utf-8",
    }
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

    /// The kind of the node at `toks` and, for a scalar (or any node when `whole`), its value.
    fn value_at<'py>(
        py: Python<'py>,
        lim: Lim,
        st: &mut State,
        d: usize,
        toks: &[Tok],
        whole: bool,
    ) -> PyResult<(&'static str, Option<Bound<'py, PyAny>>)> {
        let cached = st.nodes.get(&d).copied();
        let doc = Self::doc(st, d)?;
        let n = walk(&doc, toks, true).map_err(|m| m.into_err(py))?;
        let kind = kind_name(n);
        if !whole && kind != "scalar" {
            return Ok((kind, None));
        }
        let span = doc.span();
        let mut b = Builder::for_walk(
            py,
            lim,
            span.start,
            cached.unwrap_or(0),
            span.end - span.start,
        );
        let mut w = Walk {
            nodes: cached,
            ..Walk::default()
        };
        let r = feed(&mut b, &mut w, n, lim.digits, &|| count_nodes(&doc));
        let root = b.take_root();
        drop(b);
        let counted = w.nodes;
        if cached.is_none()
            && let Some(total) = counted
        {
            st.nodes.insert(d, total);
        }
        if let Err(f) = r {
            let text = st.stream.to_string();
            return Err(to_pyerr(py, &text, f, None));
        }
        Ok((kind, root))
    }

    fn fail(py: Python<'_>, f: Fail) -> PyErr {
        to_pyerr(py, "", f, None)
    }

    fn to_value<'py>(&self, py: Python<'py>, v: &Bound<'py, PyAny>) -> PyResult<Value> {
        if let Ok(p) = v.cast::<Prepared>() {
            return Ok(p.get().value.clone());
        }
        pyvalue::to_value(py, v, &self.lim).map_err(|f| Self::fail(py, f))
    }
}

/// A value converted ahead of an edit, so that no user code runs while the document's own
/// lock is held: the conversion may call any Python code (a lazy sequence, a `__getitem__`).
#[pyclass(frozen, module = "oneyaml._native")]
pub struct Prepared {
    value: Value,
}

/// The Python exception for a failed patch: its kind is the category (the edit layer's own
/// refusals are `other`), and the failing operation's index rides along.
fn raise_patch(py: Python<'_>, message: &str, category: &str) -> PyErr {
    let build = || -> PyResult<PyErr> {
        let err = classes::make_patch_error(py)?.call1((message, category, py.None()))?;
        Ok(PyErr::from_value(err))
    };
    build().unwrap_or_else(|e| e)
}

fn patch_pyerr(py: Python<'_>, e: &oneyaml::PatchError) -> PyErr {
    let category = match e.kind() {
        PatchErrorKind::Edit => "other",
        kind => kind.name(),
    };
    let build = || -> PyResult<PyErr> {
        let make = classes::make_patch_error(py)?;
        let err = make.call1((e.to_string(), category, e.op_index()))?;
        Ok(PyErr::from_value(err))
    };
    build().unwrap_or_else(|e| e)
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
            let bytes = {
                let st = self.lock(py)?;
                if st.text_is_bytes && !st.touched {
                    // Untouched: the text is the source, copied once straight into the result.
                    // A `bytes` object is not tracked by the collector, so making one cannot
                    // run a finalizer while the lock is held.
                    let src = st.stream.syntax().source().as_bytes();
                    return PyBytes::new_with(py, src.len(), |out| {
                        out.copy_from_slice(src);
                        Ok(())
                    });
                }
                st.written_bytes()
            };
            // Built with the lock released: allocating can run a finalizer, which must be
            // able to use the document.
            Ok(PyBytes::new(py, &bytes))
        })
    }

    /// A test hook, not part of the backend interface (the pure backend has none): the stream's
    /// bytes as the engine's own writer makes them, so that tests can compare the two writers.
    fn _emit_engine<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        guarded(py, || {
            let bytes = self.lock(py)?.stream.to_bytes();
            Ok(PyBytes::new(py, &bytes))
        })
    }

    /// The codec name of the encoding the input used.
    #[getter]
    fn encoding(&self, py: Python<'_>) -> PyResult<&'static str> {
        guarded(py, || Ok(self.lock(py)?.encoding))
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
            let (_, value) = Self::value_at(py, self.lim, &mut st, d, &toks, true)?;
            Ok(value.unwrap_or_else(|| py.None().into_bound(py)))
        })
    }

    /// `(kind, value)` of the node: the value is built for a scalar and is `None` for a
    /// collection, which a view reads by path. One walk and one call where a view needs both.
    fn read<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        path: &Bound<'py, PyAny>,
    ) -> PyResult<(&'static str, Option<Bound<'py, PyAny>>)> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            Self::value_at(py, self.lim, &mut st, d, &toks, false)
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
            let cached = st.nodes.get(&d).copied();
            let doc = Self::doc(&mut st, d)?;
            let n = walk(&doc, &toks, true)
                .map_err(|m| m.into_err(py))?
                .resolved();
            let Some(m) = n.as_mapping() else {
                return Err(PyTypeError::new_err("only a mapping has keys"));
            };
            let out = PyList::empty(py);
            let span = doc.span();
            let mut b = Builder::for_walk(
                py,
                self.lim,
                span.start,
                cached.unwrap_or(0),
                span.end - span.start,
            );
            let mut w = Walk {
                nodes: cached,
                ..Walk::default()
            };
            let count = || count_nodes(&doc);
            for (k, _) in m.iter() {
                if let Err(f) = feed(&mut b, &mut w, k, self.lim.digits, &count) {
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
            let counted = w.nodes;
            drop(b);
            if cached.is_none()
                && let Some(total) = counted
            {
                st.nodes.insert(d, total);
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
                let comment = comment_of(&doc, n, toks.is_empty());
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
            let (line, column) = pos.unzip();
            Ok((style, tag, anchor, comment, line, column)
                .into_pyobject(py)?
                .into_any())
        })
    }

    /// `(line, column, end_line, end_column, comment)` of the node at `path`, 1-based, columns in
    /// characters; the end is the position just after the node's last character (an alias is
    /// not followed).
    fn locate<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        path: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let toks = tokens(path)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            let (start, end, comment, block) = {
                let doc = Self::doc(&mut st, d)?;
                let n = walk_from_end(&doc, &toks, true, true).map_err(|m| m.into_err(py))?;
                let span = n.span();
                let leaf = last_leaf(n);
                let block = matches!(
                    leaf.as_scalar().map(|s| s.style()),
                    Some(ScalarStyle::Literal | ScalarStyle::Folded)
                )
                .then(|| leaf.span().start);
                (
                    span.start,
                    span.end,
                    comment_of(&doc, n, toks.is_empty()),
                    block,
                )
            };
            if st.lines.is_none() {
                let text = st.stream.to_string();
                let bare = text.trim_start_matches('\u{feff}').len();
                let skip = text.len() - bare;
                let index = LineIndex::new(&text[skip..]);
                st.lines = Some((text, index));
            }
            // The span of a block scalar runs through its trailing lines; the node ends after
            // its last content character. A collection that ends with one ends where it does.
            let end = st.lines.as_ref().map_or(end, |(text, _)| {
                if let Some(at) = block.and_then(|s| block_indicator(text, s)) {
                    return block_scalar_end(text, at);
                }
                let bytes = text.as_bytes();
                let mut e = end.min(bytes.len());
                while e > start && matches!(bytes[e - 1], b'\n' | b'\r') {
                    e -= 1;
                }
                e
            });
            let place = |at: usize| {
                st.lines.as_ref().and_then(|(text, index)| {
                    let skip = text.len() - text.trim_start_matches('\u{feff}').len();
                    index
                        .line_col(at.saturating_sub(skip), ColumnUnit::Char)
                        .map(|lc| (lc.line + 1, lc.column + 1))
                })
            };
            let (Some((line, column)), Some((end_line, end_column))) = (place(start), place(end))
            else {
                return Err(Self::fail(
                    py,
                    Fail::Yaml {
                        category: "other",
                        message: "the node has no place in the text".to_string(),
                        at: None,
                    },
                ));
            };
            Ok((line, column, end_line, end_column, comment)
                .into_pyobject(py)?
                .into_any())
        })
    }

    /// Apply an RFC 6902 patch to document `d`, all or nothing. `ops` is a list of operation
    /// objects, or JSON text. Values are converted before the lock is taken.
    fn patch(&self, py: Python<'_>, d: usize, ops: &Bound<'_, PyAny>) -> PyResult<()> {
        guarded(py, || {
            let patch = if let Ok(text) = ops.cast::<PyString>() {
                let text = text.to_str().map_err(|_| {
                    raise(
                        py,
                        "the patch holds a lone surrogate, which is not text",
                        "encoding",
                        None,
                        None,
                    )
                })?;
                // Read with this document's limits, as a load would read it.
                oneyaml::from_str_with::<Value>(text, &self.lim.settings())
                    .map_err(|e| {
                        let category = if e.kind() == oneyaml::ErrorKind::Limit {
                            "limit"
                        } else {
                            "patch-invalid"
                        };
                        raise_patch(py, &format!("the patch text cannot be read: {e}"), category)
                    })
                    .and_then(|v| Patch::from_value(v).map_err(|e| patch_pyerr(py, &e)))
            } else {
                Patch::from_value(self.to_value(py, ops)?).map_err(|e| patch_pyerr(py, &e))
            }?;
            let writes = patch
                .ops()
                .iter()
                .any(|op| !matches!(op, PatchOp::Test { .. }));
            let mut st = self.lock(py)?;
            touch(&mut st);
            st.stream
                .apply_patch(d, &patch)
                .map_err(|e| patch_pyerr(py, &e))?;
            if writes {
                changed(&mut st, true);
            }
            Ok(())
        })
    }

    /// Apply an RFC 7396 merge patch (a value) to document `d`, all or nothing.
    fn merge_patch(&self, py: Python<'_>, d: usize, patch: &Bound<'_, PyAny>) -> PyResult<()> {
        guarded(py, || {
            let patch = MergePatch::new(self.to_value(py, patch)?);
            let mut st = self.lock(py)?;
            touch(&mut st);
            st.stream
                .apply_merge_patch(d, &patch)
                .map_err(|e| patch_pyerr(py, &e))?;
            changed(&mut st, true);
            Ok(())
        })
    }

    /// Convert a value ahead of an edit (see `Prepared`). Takes no lock.
    fn prepare(&self, py: Python<'_>, value: &Bound<'_, PyAny>) -> PyResult<Prepared> {
        guarded(py, || {
            Ok(Prepared {
                value: self.to_value(py, value)?,
            })
        })
    }

    /// The path of exact key tokens a pointer leads to, read as the engine reads it: a key is
    /// named by its text. A pointer two keys match, or that is not a pointer, is a `PatchError`;
    /// a path to nothing is a `KeyError` or `IndexError`.
    fn resolve<'py>(
        &self,
        py: Python<'py>,
        d: usize,
        pointer: &Bound<'py, PyString>,
    ) -> PyResult<Bound<'py, PyList>> {
        guarded(py, || {
            let Ok(text) = pointer.to_str() else {
                return Err(raise_patch(
                    py,
                    "the pointer holds a lone surrogate, which is not text",
                    "patch-invalid",
                ));
            };
            let parsed = oneyaml::Pointer::parse(text).map_err(|e| patch_pyerr(py, &e))?;
            let mut st = self.lock(py)?;
            let cached = st.nodes.get(&d).copied();
            let doc = Self::doc(&mut st, d)?;
            let engine = doc.get_pointer_tokens(&parsed).map(|_| ());
            let found = path_of(&doc, parsed.tokens());
            match (engine, found) {
                (Err(e), Err(miss)) if e.kind() == PatchErrorKind::Path => Err(miss.into_err(py)),
                (Err(e), _) => Err(patch_pyerr(py, &e)),
                (Ok(()), Err(_)) => Err(raise(
                    py,
                    "internal error reading a pointer; this is a bug",
                    "other",
                    None,
                    None,
                )),
                (Ok(()), Ok(keys)) => {
                    let out = PyList::empty(py);
                    let span = doc.span();
                    let mut b = Builder::for_walk(
                        py,
                        self.lim,
                        span.start,
                        cached.unwrap_or(0),
                        span.end - span.start,
                    );
                    let mut w = Walk {
                        nodes: cached,
                        ..Walk::default()
                    };
                    let count = || count_nodes(&doc);
                    for step in keys {
                        match step {
                            Step::Index(i) => out.append(i.to_string())?,
                            Step::Key(k) => {
                                if let Err(f) = feed(&mut b, &mut w, k, self.lim.digits, &count) {
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
                        }
                    }
                    let counted = w.nodes;
                    drop(b);
                    if cached.is_none()
                        && let Some(total) = counted
                    {
                        st.nodes.insert(d, total);
                    }
                    Ok(out)
                }
            }
        })
    }

    /// Assign `value` at `path` (an existing node is replaced, a missing mapping key added).
    /// True when the edit could move or replace a collection (the version moved).
    fn set(
        &self,
        py: Python<'_>,
        d: usize,
        path: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let toks = tokens(path)?;
        let v = self.to_value(py, value)?;
        guarded(py, || {
            let mut st = self.lock(py)?;
            touch(&mut st);
            let structural = {
                let mut doc = Self::doc(&mut st, d)?;
                set_at(&mut doc, &toks, &v, py)?
            };
            changed(&mut st, structural);
            Ok(structural)
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
            touch(&mut st);
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
            touch(&mut st);
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
                    let Some((_, value)) = find_entry(parent, last).map_err(|m| m.into_err(py))?
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
                        return Err(seq_miss(last, len).into_err(py));
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

/// One step of a pointer's path: the key node of a mapping entry, or an index.
enum Step<'d> {
    Key(NodeRef<'d>),
    Index(usize),
}

/// The entries a pointer's tokens lead through, naming a key by its text (what the engine
/// does), following aliases on the way.
fn path_of<'d>(doc: &'d Document, tokens: &[String]) -> Result<Vec<Step<'d>>, Miss> {
    let mut node = doc.root().ok_or(Miss::NoRoot)?;
    let mut out = Vec::with_capacity(tokens.len());
    for tok in tokens {
        node = node.resolved();
        node = match node.kind() {
            NodeKind::Mapping => {
                let m = node.as_mapping().ok_or_else(|| Miss::Key(tok.clone()))?;
                let mut hit = None;
                for (k, v) in m.iter() {
                    let named = k
                        .resolved()
                        .as_scalar()
                        .is_some_and(|sc| sc.value() == tok.as_str());
                    if named {
                        hit = Some((k, v));
                    }
                }
                let Some((k, v)) = hit else {
                    return Err(Miss::Key(tok.clone()));
                };
                out.push(Step::Key(k));
                v
            }
            NodeKind::Sequence => {
                let s = node.as_sequence().ok_or_else(|| Miss::Key(tok.clone()))?;
                let Some(i) = seq_index(&Tok::Text(tok.clone())) else {
                    return Err(seq_miss(&Tok::Text(tok.clone()), s.len()));
                };
                let Some(n) = s.get(i) else {
                    return Err(Miss::Index(tok.clone(), s.len()));
                };
                out.push(Step::Index(i));
                n
            }
            _ => return Err(Miss::NotCollection(tok.clone())),
        };
    }
    Ok(out)
}

/// Called before every attempt to change the stream, so that its source is never taken for the
/// text once that may have moved on (an attempt that fails is covered too).
fn touch(st: &mut State) {
    st.touched = true;
}

fn changed(st: &mut State, structural: bool) {
    st.lines = None;
    st.nodes.clear();
    if structural {
        st.version += 1;
    }
}

/// Whether a node is, or holds, a collection: replacing it can strand views of it.
fn is_collection(n: NodeRef<'_>) -> bool {
    // An alias counts as what it stands for: views reach through it.
    matches!(n.resolved().kind(), NodeKind::Mapping | NodeKind::Sequence)
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
            let found = find_entry(parent, last).map_err(|m| m.into_err(py))?;
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
                return Err(seq_miss(last, len).into_err(py));
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
