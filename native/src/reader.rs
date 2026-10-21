//! `load`: Python objects built straight from the engine's events.
//!
//! There is no intermediate tree. The engine's events become `Op`s (see `ops`), and collections
//! are built from them as they arrive, on an explicit stack: nesting depth never becomes native
//! recursion. An alias to a collection is the same Python object as the anchored one, so an
//! alias bomb costs the size of the document.

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::mpsc::sync_channel;

use oneyaml::Settings;
use pyo3::exceptions::{PyRecursionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

use crate::classes;
use crate::errors::{Fail, panic_message, to_pyerr};
use crate::hook;
use crate::input::with_text;
use crate::limits::Lim;
use crate::ops::{self, Conv, Mode, Op, Prop};

/// Mapping keys longer than this are not cached: the cache is for the short, repeated names of
/// a config file, and must not grow with hostile input.
const KEY_CACHE_MAX_LEN: usize = 128;
/// Entries the key cache may hold per load.
const KEY_CACHE_MAX_ENTRIES: usize = 1 << 16;
/// From this many bytes of input the engine runs on a second thread while objects are built.
/// Below it, starting a thread costs more than it saves.
pub(crate) const PIPELINE_MIN: usize = 128 * 1024;
/// Batches the engine may run ahead of the builder.
pub(crate) const PIPELINE_DEPTH: usize = 4;
/// Events between looks for a pending signal on the single-thread path.
const SIGNAL_EVERY: usize = 4096;
/// How deep collections may nest inside one mapping key. A key is hashed by the interpreter,
/// which recurses in C once per level of nesting, and equality tests on a hash collision
/// recurse the same way, so an unbounded key can overflow the C stack. Real keys are a few
/// levels at most. Fixed: it does not follow `Limits`, and the pure backend uses the same number.
const KEY_DEPTH: usize = 200;

/// A node registered under an anchor.
struct Anchored<'py> {
    obj: Bound<'py, PyAny>,
    /// Collections nested in it, itself included; 0 for a scalar.
    height: u32,
    /// Whether it can be a mapping key as it is (no list or dict inside).
    hashable: bool,
    /// What a copy of it would hold: nodes, and bytes of scalar text. An alias is charged this
    /// much against the alias budgets even though the object is shared, so that a value which
    /// would be enormous to walk is refused when it is read.
    nodes: usize,
    bytes: usize,
}

enum Body<'py> {
    Seq(Bound<'py, PyList>),
    /// A sequence used as a mapping key (or inside one): becomes a tuple.
    KeySeq(Vec<Bound<'py, PyAny>>),
    Map {
        dict: Bound<'py, PyDict>,
        /// The key waiting for its value, with the byte offset it was read at.
        key: Option<(Bound<'py, PyAny>, usize)>,
    },
}

struct Frame<'py, 's> {
    body: Body<'py>,
    /// Inside a mapping key: everything built here is immutable and hashable.
    key_mode: bool,
    /// Height of the tallest child so far.
    tallest: u32,
    tag: Option<Cow<'s, str>>,
    /// Byte offset of the collection's first event.
    start: usize,
    /// Its place in `slots` when it has an anchor.
    slot: Option<usize>,
    /// Expanded size of the children so far.
    nodes: usize,
    bytes: usize,
}

/// A node a registered constructor builds. Its events wait in a list, which stands in the
/// result until the whole input has been read, so that no callback runs on a document that
/// turns out to be malformed.
enum Hole<'py> {
    Root(Bound<'py, PyList>),
    Item(Bound<'py, PyList>, usize, Bound<'py, PyList>),
    Entry(Bound<'py, PyDict>, Bound<'py, PyAny>, Bound<'py, PyList>),
}

pub struct Builder<'py, 's> {
    py: Python<'py>,
    lim: Lim,
    mode: Mode,
    multi: bool,
    /// The key object that stands for a `<<` until its value arrives.
    merge_marker: Option<Bound<'py, PyAny>>,
    /// The values of `<<` keys met in the mappings now open, by the depth of the mapping; they
    /// are checked and applied when the mapping closes. Always empty outside the YAML 1.1 mode.
    pending_merges: Vec<(usize, Vec<MergeValue<'py>>)>,
    stack: Vec<Frame<'py, 's>>,
    root: Option<Bound<'py, PyAny>>,
    docs: Vec<Bound<'py, PyAny>>,
    /// Anchored nodes in the order their anchors were met. An anchor names the latest node that
    /// began before the alias, so a node is registered when it begins and filled in when it
    /// ends; an alias to an unfilled slot refers to a node that contains it.
    slots: Vec<Option<Anchored<'py>>>,
    /// Anchor names, with the scope they are read in: always 0 for a load; a walk over a lossless
    /// document gives every anchored node a scope of its own, so that an alias finds its target by
    /// identity and never by a name another anchor shares.
    anchors: HashMap<&'s str, usize>,
    scoped: HashMap<(&'s str, u32), usize>,
    scope: u32,
    /// An alias that shares an object already built, and is not charged (see `share`).
    free_alias: bool,
    /// Floors for the own-node and bytes-read figures the alias budgets are sized from: a view
    /// reads part of a document, but its aliases are judged as the whole read would judge them.
    min_nodes: usize,
    min_read: usize,
    /// Builds set aside while an outside anchor is built where it is first needed.
    detached: Vec<(Vec<Frame<'py, 's>>, Option<Bound<'py, PyAny>>, usize)>,
    keys: HashMap<&'s str, Bound<'py, PyString>>,
    nan: Option<Bound<'py, PyAny>>,
    /// Nodes of the current document so far, and what aliases have been charged.
    nodes: usize,
    charged_nodes: usize,
    charged_bytes: usize,
    doc_start: usize,
    documents: usize,
    /// Collections open inside the mapping key being read (0 outside a key), and where that key
    /// began.
    key_depth: usize,
    key_start: usize,
    holes: Vec<Hole<'py>>,
}

pub type R<T> = Result<T, Fail>;

/// The value of a `<<` key, and where the key was.
type MergeValue<'py> = (Bound<'py, PyAny>, usize);

impl<'py, 's> Builder<'py, 's> {
    /// A builder for objects made from a walk over a lossless document (see `document`), whose
    /// positions are byte offsets into the stream and whose own document begins at `doc_start`.
    pub fn for_walk(
        py: Python<'py>,
        lim: Lim,
        doc_start: usize,
        doc_nodes: usize,
        doc_bytes: usize,
    ) -> Self {
        let mut b = Self::new(py, lim, false, Mode::Core);
        b.doc_start = doc_start;
        b.min_nodes = doc_nodes;
        b.min_read = doc_bytes;
        b
    }

    /// The node count the alias budgets are sized from, when it was only worked out late.
    pub fn set_min_nodes(&mut self, nodes: usize) {
        self.min_nodes = nodes;
    }

    /// Note that the next slot is the node anchored `name`. A load keys anchors by name alone (its
    /// hot path); a walk over a document adds the scope.
    fn register(&mut self, name: &'s str) {
        let slot = self.slots.len();
        if self.scope == 0 {
            self.anchors.insert(name, slot);
        } else {
            self.scoped.insert((name, self.scope), slot);
        }
    }

    /// The scope anchors are registered and found in (see `anchors`).
    pub fn set_scope(&mut self, scope: u32) {
        self.scope = scope;
    }

    /// Whether the next alias shares an object without being charged for it.
    pub fn set_free(&mut self, free: bool) {
        self.free_alias = free;
    }

    /// Set the build in progress aside, to build something else whose result stays registered
    /// under its anchor and is shared afterwards.
    pub fn detach(&mut self) {
        let saved = (
            std::mem::take(&mut self.stack),
            self.root.take(),
            std::mem::take(&mut self.key_depth),
        );
        self.detached.push(saved);
    }

    /// Resume the build set aside by the last `detach`.
    pub fn reattach(&mut self) {
        if let Some((stack, root, key_depth)) = self.detached.pop() {
            self.stack = stack;
            self.root = root;
            self.key_depth = key_depth;
        }
    }

    /// Feed one op of the walk.
    pub fn feed(&mut self, op: Op<'s>) -> R<()> {
        self.op(op)
    }

    /// The finished top-level object, if one is complete.
    pub fn take_root(&mut self) -> Option<Bound<'py, PyAny>> {
        self.root.take()
    }

    fn new(py: Python<'py>, lim: Lim, multi: bool, mode: Mode) -> Self {
        Builder {
            py,
            lim,
            mode,
            merge_marker: None,
            pending_merges: Vec::new(),
            multi,
            stack: Vec::new(),
            root: None,
            docs: Vec::new(),
            slots: Vec::new(),
            anchors: HashMap::new(),
            scoped: HashMap::new(),
            scope: 0,
            free_alias: false,
            min_nodes: 0,
            min_read: 0,
            detached: Vec::new(),
            keys: HashMap::new(),
            nan: None,
            nodes: 0,
            charged_nodes: 0,
            charged_bytes: 0,
            doc_start: 0,
            documents: 0,
            key_depth: 0,
            key_start: 0,
            holes: Vec::new(),
        }
    }

    /// A builder for the YAML 1.1 rules that leaves nodes to registered constructors (see
    /// `defer`).
    pub fn hooked(py: Python<'py>, lim: Lim, multi: bool) -> Self {
        Self::new(py, lim, multi, Mode::Yaml11)
    }

    /// Whether any node has been left to a constructor so far.
    pub fn has_holes(&self) -> bool {
        !self.holes.is_empty()
    }

    /// Leave the node whose events are in `events` to a constructor and put the list in its
    /// place. False when the node is where that cannot work (in a mapping key, or in the
    /// value of a merge key): the caller falls back to building every node.
    pub fn defer(&mut self, events: Bound<'py, PyList>, at: usize, nodes: usize) -> R<bool> {
        let hole = match self.stack.last() {
            None => Hole::Root(events.clone()),
            Some(f) => {
                if f.key_mode {
                    return Ok(false);
                }
                if let Some(marker) = &self.merge_marker {
                    // Inside the value of a `<<` key some enclosing mapping waits on it.
                    if self.stack.len() > 64 {
                        return Ok(false);
                    }
                    for frame in &self.stack {
                        if let Body::Map {
                            key: Some((k, _)), ..
                        } = &frame.body
                            && k.is(marker)
                        {
                            return Ok(false);
                        }
                    }
                }
                match &f.body {
                    Body::KeySeq(_) | Body::Map { key: None, .. } => return Ok(false),
                    Body::Seq(l) => Hole::Item(l.clone(), l.len(), events.clone()),
                    Body::Map {
                        dict,
                        key: Some((k, _)),
                    } => Hole::Entry(dict.clone(), k.clone(), events.clone()),
                }
            }
        };
        self.holes.push(hole);
        self.nodes += 1;
        self.place(events.into_any(), 1, at, nodes, 0)?;
        Ok(true)
    }

    /// Build the values left to constructors, in document order, with `build(events)`.
    pub fn resolve_holes(&mut self, build: &Bound<'py, PyAny>) -> R<()> {
        for hole in std::mem::take(&mut self.holes) {
            let events = match &hole {
                Hole::Root(e) | Hole::Item(_, _, e) | Hole::Entry(_, _, e) => e.clone(),
            };
            let value = build.call1((events.clone(),))?;
            // A later duplicate key may have replaced the placeholder; its value stands.
            match hole {
                Hole::Root(_) => {
                    if let Some(d) = self.docs.iter_mut().find(|d| d.is(&events)) {
                        *d = value;
                    }
                }
                Hole::Item(l, i, _) => {
                    if l.get_item(i)?.is(&events) {
                        l.set_item(i, value)?;
                    }
                }
                Hole::Entry(d, k, _) => {
                    if d.get_item(&k)?.is_some_and(|v| v.is(&events)) {
                        d.set_item(&k, value)?;
                    }
                }
            }
        }
        Ok(())
    }

    /// Whether the next node is a mapping key, or is inside one.
    #[inline]
    fn at_key(&self) -> bool {
        match self.stack.last() {
            Some(f) => f.key_mode || matches!(&f.body, Body::Map { key, .. } if key.is_none()),
            None => false,
        }
    }

    fn consume(&mut self, batch: Vec<Op<'s>>) -> R<()> {
        for op in batch {
            self.op(op)?;
        }
        poll_signals(self.py)
    }

    fn op(&mut self, op: Op<'s>) -> R<()> {
        match op {
            Op::Open { map, at, props } => self.open(map, at, props.map(|p| *p)),
            Op::Close => self.close(),
            Op::Alias(name, at) => self.alias(name, at),
            Op::DocStart(at) => {
                if !self.multi && self.documents > 0 {
                    return Err(Fail::new(
                        "other",
                        "the stream holds more than one document; use loads_all",
                        at,
                    ));
                }
                self.documents += 1;
                self.anchors.clear();
                self.scoped.clear();
                self.slots.clear();
                self.nodes = 0;
                self.charged_nodes = 0;
                self.charged_bytes = 0;
                self.doc_start = at;
                Ok(())
            }
            Op::DocEnd => {
                let root = match self.root.take() {
                    Some(r) => r,
                    None => self.py.None().into_bound(self.py),
                };
                self.docs.push(root);
                Ok(())
            }
            Op::Fail(f) => Err(Fail::new(f.category, f.message, f.at)),
            Op::Prop(p, at) => {
                let Prop {
                    inner,
                    anchor,
                    custom,
                } = *p;
                self.nodes += 1;
                let (mut obj, bytes) = self.scalar(inner)?;
                if let Some(tag) = custom {
                    obj = self.tagged(&tag, obj, at)?;
                }
                if let Some(name) = anchor {
                    self.register(name);
                    self.slots.push(Some(Anchored {
                        obj: obj.clone(),
                        height: 0,
                        hashable: true,
                        nodes: 1,
                        bytes,
                    }));
                }
                self.place(obj, 0, at, 1, bytes)
            }
            scalar => {
                self.nodes += 1;
                let at = match &scalar {
                    Op::Null(at)
                    | Op::Bool(_, at)
                    | Op::Int(_, at)
                    | Op::Float(_, at)
                    | Op::Str(_, at)
                    | Op::Num(_, _, at)
                    | Op::Merge(at, _)
                    | Op::Bytes(_, at) => *at,
                    _ => 0,
                };
                let (obj, bytes) = self.scalar(scalar)?;
                self.place(obj, 0, at, 1, bytes)
            }
        }
    }

    /// The Python object for a scalar op, and the bytes of text it stands for.
    fn scalar(&mut self, op: Op<'s>) -> R<(Bound<'py, PyAny>, usize)> {
        let py = self.py;
        Ok(match op {
            Op::Str(text, _) => {
                let n = text.len();
                let at_key = self.at_key();
                (self.string(text, at_key), n)
            }
            Op::Null(_) => (py.None().into_bound(py), 0),
            Op::Merge(at, explicit) => self.merge_scalar(at, explicit)?,
            Op::Bool(b, _) => (PyBool::new(py, b).to_owned().into_any(), 0),
            Op::Int(i, _) => {
                let Ok(v) = i.into_pyobject(py);
                (v.into_any(), 0)
            }
            Op::Float(f, _) if f.is_nan() => {
                // One object for every `.nan` of the load, so two of them as keys collide in
                // the dict as they do in YAML (a fresh NaN never equals another).
                let nan = self
                    .nan
                    .get_or_insert_with(|| PyFloat::new(py, f).into_any());
                (nan.clone(), 0)
            }
            Op::Float(f, _) => (PyFloat::new(py, f).into_any(), 0),
            Op::Num(text, base, _) => {
                let n = text.len();
                (self.big_int(&text, base)?, n)
            }
            Op::Bytes(b, _) => {
                let n = b.len();
                (PyBytes::new(py, &b).into_any(), n)
            }
            _ => return Err(Fail::new("other", "unexpected node", self.doc_start)),
        })
    }

    /// Whether the next node is a key of a mapping (block, flow or inside a complex key).
    fn at_merge_key(&self) -> bool {
        match self.stack.last() {
            Some(f) => matches!(&f.body, Body::Map { key, .. } if key.is_none()),
            None => false,
        }
    }

    /// A plain `<<` is a merge key only as the key of a mapping; elsewhere it is the text it
    /// spells. The tag `!!merge` has no meaning anywhere else.
    #[inline(never)]
    fn merge_scalar(&mut self, at: usize, explicit: bool) -> R<(Bound<'py, PyAny>, usize)> {
        let py = self.py;
        if self.at_merge_key() {
            let marker = self
                .merge_marker
                .get_or_insert_with(|| PyList::empty(py).into_any());
            Ok((marker.clone(), 0))
        } else if explicit {
            Err(Fail::new(
                "construct",
                "the merge tag is only meaningful on a mapping key",
                at,
            ))
        } else {
            Ok((PyString::new(py, "<<").into_any(), 2))
        }
    }

    /// Apply a tag: the Core schema keeps what it does not know as `Tagged`; the YAML 1.1 mode
    /// lets a Python function build the value.
    #[inline(never)]
    fn tagged(&self, tag: &str, obj: Bound<'py, PyAny>, at: usize) -> R<Bound<'py, PyAny>> {
        let py = self.py;
        if self.mode == Mode::Core {
            return Ok(classes::tagged(py)?.call1((tag, obj))?);
        }
        let cap = (self.lim.digits != usize::MAX).then_some(self.lim.digits);
        match classes::construct(py)?.call1((tag, obj, cap)) {
            Ok(v) => Ok(v),
            Err(e) if e.is_instance(py, classes::reject(py)?) => {
                let v = e.value(py);
                let message: String = v.getattr("message")?.extract()?;
                let category: String = v.getattr("category")?.extract()?;
                let category = if category == "limit" {
                    "limit"
                } else {
                    "construct"
                };
                Err(Fail::new(category, message, at))
            }
            Err(e) => Err(e.into()),
        }
    }

    /// A `str`, shared with earlier equal keys of this load when it is a key.
    fn string(&mut self, text: Cow<'s, str>, at_key: bool) -> Bound<'py, PyAny> {
        if at_key && let Cow::Borrowed(s) = &text {
            let s: &'s str = s;
            if s.len() > KEY_CACHE_MAX_LEN {
                return PyString::new(self.py, s).into_any();
            }
            if let Some(o) = self.keys.get(s) {
                return o.clone().into_any();
            }
            let o = PyString::new(self.py, s);
            if self.keys.len() < KEY_CACHE_MAX_ENTRIES {
                self.keys.insert(s, o.clone());
            }
            return o.into_any();
        }
        PyString::new(self.py, &text).into_any()
    }

    /// An integer from decimal text (base 10, sign included) or from radix digits.
    fn big_int(&self, text: &str, base: u32) -> R<Bound<'py, PyAny>> {
        let py = self.py;
        let int = py.get_type::<PyInt>();
        if base == 60 {
            return Ok(classes::sexagesimal(py)?.call1((text,))?);
        }
        if base != 10 {
            return Ok(int.call1((text, base))?);
        }
        match int.call1((text,)) {
            Ok(o) => Ok(o),
            // The interpreter has its own, smaller, cap on text to integer conversion. Ours
            // is the one that applies (the digits were counted already), so convert in pieces.
            Err(e) if e.is_instance_of::<PyValueError>(py) => {
                Ok(classes::big_int(py)?.call1((text,))?)
            }
            Err(e) => Err(e.into()),
        }
    }

    fn open(&mut self, map: bool, at: usize, props: Option<ops::NodeProps<'s>>) -> R<()> {
        self.nodes += 1;
        let (anchor, tag) = match props {
            Some(p) => (p.anchor, p.custom),
            None => (None, None),
        };
        let key_mode = self.at_key();
        let py = self.py;
        if key_mode {
            if self.key_depth == 0 {
                self.key_start = at;
            }
            if self.key_depth >= KEY_DEPTH {
                return Err(key_too_deep(self.key_start));
            }
            self.key_depth += 1;
        }
        let body = if map {
            Body::Map {
                dict: PyDict::new(py),
                key: None,
            }
        } else if key_mode {
            Body::KeySeq(Vec::new())
        } else {
            Body::Seq(PyList::empty(py))
        };
        let slot = anchor.map(|name| {
            self.register(name);
            self.slots.push(None);
            self.slots.len() - 1
        });
        self.stack.push(Frame {
            body,
            key_mode,
            tallest: 0,
            tag,
            start: at,
            slot,
            nodes: 0,
            bytes: 0,
        });
        Ok(())
    }

    fn close(&mut self) -> R<()> {
        let py = self.py;
        let depth = self.stack.len();
        let Some(f) = self.stack.pop() else {
            return Err(Fail::new("other", "unbalanced collection", self.doc_start));
        };
        let mut obj: Bound<'py, PyAny> = match f.body {
            Body::Seq(l) => l.into_any(),
            Body::KeySeq(v) => PyTuple::new(py, v)?.into_any(),
            Body::Map { dict, .. } => {
                let dict = if matches!(self.pending_merges.last(), Some((d, _)) if *d == depth) {
                    let sources = self
                        .pending_merges
                        .pop()
                        .map(|(_, v)| v)
                        .unwrap_or_default();
                    // Merged entries first, then the mapping's own: its keys keep the merged
                    // keys' places and replace their values.
                    let merged = PyDict::new(py);
                    for m in merge_sources(&sources)? {
                        merged.update(m.as_mapping())?;
                    }
                    merged.update(dict.as_mapping())?;
                    merged
                } else {
                    dict
                };
                if f.key_mode {
                    classes::frozen_mapping(py)?.call1((dict,))?
                } else {
                    dict.into_any()
                }
            }
        };
        if let Some(t) = &f.tag {
            obj = self.tagged(t, obj, f.start)?;
        }
        if f.key_mode {
            self.key_depth -= 1;
        }
        let height = f.tallest.saturating_add(1);
        let nodes = f.nodes.saturating_add(1);
        if let Some(slot) = f.slot.and_then(|i| self.slots.get_mut(i)) {
            *slot = Some(Anchored {
                obj: obj.clone(),
                height,
                hashable: f.key_mode,
                nodes,
                bytes: f.bytes,
            });
        }
        self.place(obj, height, f.start, nodes, f.bytes)
    }

    fn alias(&mut self, name: &'s str, at: usize) -> R<()> {
        self.nodes += 1;
        let found = if self.scope == 0 {
            self.anchors.get(name)
        } else {
            self.scoped.get(&(name, self.scope))
        };
        let Some(&index) = found else {
            return Err(Fail::new(
                "syntax",
                format!("alias *{name} has no anchor"),
                at,
            ));
        };
        let Some(Some(a)) = self.slots.get(index) else {
            return Err(Fail::new(
                "syntax",
                format!("alias *{name} refers to a node that contains it"),
                at,
            ));
        };
        // The shared object nests as deep as the anchored one does, wherever it is used.
        let depth = self.stack.len().saturating_add(a.height as usize);
        if depth > self.lim.depth {
            return Err(Fail::new(
                "limit",
                format!("alias nests deeper than the limit of {}", self.lim.depth),
                at,
            ));
        }
        let (height, nodes, bytes) = (a.height, a.nodes, a.bytes);
        let mut obj = a.obj.clone();
        let hashable = a.hashable;
        if let Some(m) = &self.merge_marker
            && obj.is(m)
        {
            return Err(Fail::new(
                "construct",
                "an alias to a merge key cannot be constructed",
                at,
            ));
        }
        // Charge what a copy would hold. Sharing makes loading cheap, but whoever walks the
        // result (a dump, a comparison, `json.dumps`) pays for the expansion.
        if !self.free_alias {
            self.charged_nodes = self.charged_nodes.saturating_add(nodes);
            self.charged_bytes = self.charged_bytes.saturating_add(bytes);
        }
        let read = at.saturating_sub(self.doc_start).max(self.min_read);
        if self.charged_nodes > self.lim.node_budget(self.nodes.max(self.min_nodes)) {
            return Err(Fail::new(
                "limit",
                "alias expansion exceeds the node limit",
                at,
            ));
        }
        if self.charged_bytes > self.lim.byte_budget(read) {
            return Err(Fail::new(
                "limit",
                "alias expansion exceeds the byte limit",
                at,
            ));
        }
        if self.at_key() {
            // The shared object counts as nested here too.
            if self.key_depth.saturating_add(height as usize) > KEY_DEPTH {
                let start = if self.key_depth == 0 {
                    at
                } else {
                    self.key_start
                };
                return Err(key_too_deep(start));
            }
            if !hashable {
                obj = self.freeze(obj, at)?;
            }
        }
        self.place(obj, height, at, nodes, bytes)
    }

    /// An aliased list or dict used as a mapping key has to become a tuple or `FrozenMapping`.
    /// The copy is as large as the expansion, which the alias has just been charged for.
    fn freeze(&mut self, obj: Bound<'py, PyAny>, at: usize) -> R<Bound<'py, PyAny>> {
        let py = self.py;
        match classes::freeze(py)?.call1((obj, i64::MAX, i64::MAX)) {
            Ok(r) => Ok(r.get_item(0)?),
            Err(e) if e.is_instance_of::<PyValueError>(py) => Err(Fail::new(
                "limit",
                "alias expansion exceeds the alias budget",
                at,
            )),
            Err(e) => Err(e.into()),
        }
    }

    /// Add a finished node to the collection being built, or make it the document's root.
    fn place(
        &mut self,
        obj: Bound<'py, PyAny>,
        height: u32,
        at: usize,
        nodes: usize,
        bytes: usize,
    ) -> R<()> {
        let Builder {
            stack,
            merge_marker,
            pending_merges,
            mode,
            root,
            py,
            ..
        } = self;
        let Some(f) = stack.last_mut() else {
            *root = Some(obj);
            return Ok(());
        };
        f.tallest = f.tallest.max(height);
        f.nodes = f.nodes.saturating_add(nodes);
        f.bytes = f.bytes.saturating_add(bytes);
        match &mut f.body {
            Body::Seq(l) => l.append(obj)?,
            Body::KeySeq(v) => v.push(obj),
            Body::Map { dict, key } => match key.take() {
                None => *key = Some((obj, at)),
                Some((k, kat)) => {
                    if let Some(m) = merge_marker
                        && k.is(m)
                    {
                        // Checked when the mapping closes, so that an error met later in the
                        // mapping is the one reported, as the pure backend does.
                        let depth = stack.len();
                        match pending_merges.last_mut() {
                            Some((d, v)) if *d == depth => v.push((obj, kat)),
                            _ => pending_merges.push((depth, vec![(obj, kat)])),
                        }
                        return Ok(());
                    }
                    let before = dict.len();
                    if let Err(e) = dict.set_item(&k, &obj) {
                        // Comparing two equal deep keys can run out of interpreter stack; that
                        // is a limit of this load, not a failure of the caller's code.
                        if e.is_instance_of::<PyRecursionError>(*py) {
                            return Err(Fail::new(
                                "limit",
                                "a mapping key is nested too deeply to compare",
                                kat,
                            ));
                        }
                        return Err(e.into());
                    }
                    // A repeated key is an error in YAML 1.2; the compat modes keep the last.
                    if dict.len() == before && *mode == Mode::Core {
                        return Err(collision(dict, &k, kat));
                    }
                }
            },
        }
        Ok(())
    }

    pub fn finish(self) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        if self.multi {
            Ok(PyList::new(py, self.docs)?.into_any())
        } else {
            Ok(match self.docs.into_iter().next() {
                Some(d) => d,
                None => py.None().into_bound(py),
            })
        }
    }
}

/// The mappings the values of a mapping's `<<` keys name, in the order they are applied: for a
/// list the last mapping first, so that the first one listed wins.
fn merge_sources<'py>(values: &[(Bound<'py, PyAny>, usize)]) -> R<Vec<Bound<'py, PyDict>>> {
    let as_dict = |o: &Bound<'py, PyAny>| -> Option<Bound<'py, PyDict>> {
        if let Ok(d) = o.cast::<PyDict>() {
            return Some(d.clone());
        }
        // A mapping inside a key is a FrozenMapping, which wraps a dict.
        o.getattr("_items").ok()?.cast_into::<PyDict>().ok()
    };
    let mut out = Vec::new();
    for (value, at) in values {
        let bad = || {
            Fail::new(
                "construct",
                "expected a mapping or list of mappings for merging",
                *at,
            )
        };
        let items: Vec<Bound<'py, PyAny>> = if let Ok(list) = value.cast::<PyList>() {
            list.iter().collect()
        } else if let Ok(t) = value.cast::<PyTuple>() {
            t.iter().collect()
        } else {
            out.push(as_dict(value).ok_or_else(bad)?);
            continue;
        };
        let mut found = Vec::with_capacity(items.len());
        for item in &items {
            found.push(as_dict(item).ok_or_else(bad)?);
        }
        out.extend(found.into_iter().rev());
    }
    Ok(out)
}

fn key_too_deep(at: usize) -> Fail {
    Fail::new(
        "limit",
        format!("a mapping key nests deeper than {KEY_DEPTH} levels"),
        at,
    )
}

/// `k` was already in `dict`. Either it is a duplicate key, or YAML keeps it apart from an
/// earlier key that Python's equality merges with it (`1`, `1.0` and `true`).
fn collision(dict: &Bound<'_, PyDict>, k: &Bound<'_, PyAny>, at: usize) -> Fail {
    for (other, _) in dict.iter() {
        if other.eq(k).unwrap_or(false) {
            if other.get_type().is(k.get_type()) {
                return Fail::new("duplicate-key", "duplicate mapping key", at);
            }
            return Fail::new(
                "other",
                "two keys are different in YAML but equal in Python (1, 1.0 and true), \
                 so they cannot share a dict",
                at,
            );
        }
    }
    Fail::new("duplicate-key", "duplicate mapping key", at)
}

/// The failure for a panic in one of the stages: a bug in this library, never the input's fault.
pub(crate) fn internal_error(payload: &(dyn std::any::Any + Send)) -> Fail {
    Fail::Yaml {
        category: "other",
        message: format!(
            "internal error in the native backend ({}); this is a bug",
            panic_message(payload)
        ),
        at: None,
    }
}

/// Let a pending signal (Ctrl-C) end the load: it surfaces as the usual `KeyboardInterrupt`.
pub fn poll_signals(py: Python<'_>) -> R<()> {
    if hook::take(hook::INTERRUPT) {
        // The test switch stands in for a Ctrl-C: `interrupt_main` raises SIGINT in this
        // process as the interpreter's own signal handling would.
        py.import("_thread")
            .and_then(|m| m.call_method0("interrupt_main"))
            .map_err(Fail::Py)?;
    }
    py.check_signals().map_err(Fail::Py)
}

/// Start a helper thread, or say why not. The OS may refuse (a process limit, no memory).
pub(crate) fn spawn<'scope, 'env, T: Send + 'scope>(
    scope: &'scope std::thread::Scope<'scope, 'env>,
    name: &str,
    f: impl FnOnce() -> T + Send + 'scope,
) -> std::io::Result<std::thread::ScopedJoinHandle<'scope, T>> {
    if hook::is(hook::REFUSE_THREADS)
        || (name == "oneyaml-translate" && hook::is(hook::PANIC_THEN_REFUSE_TRANSLATOR))
    {
        return Err(std::io::Error::other("thread refused by test switch"));
    }
    std::thread::Builder::new()
        .name(name.to_string())
        .spawn_scoped(scope, f)
}

/// The input read on this thread alone.
fn run_single<'py, 's>(
    py: Python<'py>,
    text: &'s str,
    cap: Conv,
    settings: &Settings,
    builder: &mut Builder<'py, 's>,
) -> R<()> {
    for (n, ev) in oneyaml_syntax::events_with(text, settings).enumerate() {
        if n % SIGNAL_EVERY == SIGNAL_EVERY - 1 {
            poll_signals(py)?;
        }
        if let Some(op) = ops::translate(&ev, cap) {
            builder.op(op)?;
        }
    }
    Ok(())
}

/// Build the objects from `text`. Small input is read on this thread. For big input the engine
/// runs on a second thread and turns its events into ops on a third, while this one builds
/// objects from the ops already made: the three stages overlap. If the OS will not start the
/// threads, the input is read on this one.
fn run<'py, 's>(
    py: Python<'py>,
    text: &'s str,
    lim: Lim,
    settings: &Settings,
    builder: &mut Builder<'py, 's>,
) -> R<()> {
    let cap = Conv {
        cap: lim.digits,
        mode: builder.mode,
    };
    if text.len() < PIPELINE_MIN {
        // No batches: for a small input they would cost more than the work they hand over.
        return run_single(py, text, cap, settings, builder);
    }
    // The other two threads touch no Python object: they read `text` (which the caller keeps
    // alive and unchanged) and hand plain data over. Objects are built here, under the
    // interpreter lock, as batches arrive.
    let piped = std::thread::scope(|scope| -> Option<R<()>> {
        let (events_tx, events_rx) = sync_channel(PIPELINE_DEPTH);
        let (ops_tx, ops_rx) = sync_channel(PIPELINE_DEPTH);
        let parser = spawn(scope, "oneyaml-parse", move || {
            if hook::is(hook::PANIC_IN_PARSER) || hook::is(hook::PANIC_THEN_REFUSE_TRANSLATOR) {
                panic!("deliberate panic in the parser stage");
            }
            ops::pull(text, settings, |events| events_tx.send(events).is_ok());
        })
        .ok()?;
        let translator = spawn(scope, "oneyaml-translate", move || {
            for events in events_rx {
                if hook::is(hook::PANIC_IN_TRANSLATOR) {
                    panic!("deliberate panic in the translator stage");
                }
                if ops_tx.send(ops::translate_batch(events, cap)).is_err() {
                    break;
                }
            }
        });
        let Ok(translator) = translator else {
            // The refused closure took the parser's receiver with it, so the parser stops at its
            // next hand-over; wait for it, then read on this thread instead.
            drop(ops_rx);
            if let Err(payload) = parser.join() {
                return Some(Err(internal_error(payload.as_ref())));
            }
            return None;
        };
        let mut result = Ok(());
        let mut incoming = ops_rx;
        // While the next batch is on its way nothing here needs the interpreter, so let other
        // Python threads run.
        loop {
            let rx = &mut incoming;
            let Ok(batch) = py.detach(move || rx.recv()) else {
                break;
            };
            result = builder.consume(batch);
            if result.is_err() {
                break;
            }
        }
        // Closing the channels stops the other stages at their next hand-over.
        drop(incoming);
        for stage in [parser, translator] {
            if let Err(payload) = stage.join() {
                return Some(Err(internal_error(payload.as_ref())));
            }
        }
        Some(result)
    });
    match piped {
        Some(r) => r,
        None => run_single(py, text, cap, settings, builder),
    }
}

pub fn load<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    limits: &Bound<'py, PyAny>,
    multi: bool,
    mode: Mode,
) -> PyResult<Bound<'py, PyAny>> {
    let lim = Lim::from_py(limits)?;
    with_text(py, data, lim.input, |text| {
        let settings = lim.settings();
        let mut builder = Builder::new(py, lim, multi, mode);
        if let Err(f) = run(py, text, lim, &settings, &mut builder) {
            return Err(to_pyerr(py, text, f, None));
        }
        builder.finish()
    })
}
