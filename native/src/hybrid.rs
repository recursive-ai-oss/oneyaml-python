//! `load_hooked`: a YAML 1.1 load in which registered constructors build some of the nodes.
//!
//! Most of a document is read as `load` reads it, straight into Python objects. A node whose
//! tag has a constructor registered is different: that constructor wants a node, not a value.
//! Such a node (with everything below it) is handed to Python as a list of event tuples, and
//! Python composes it and calls the constructor. The list stands in the result until the whole
//! input has been read, so a malformed document never runs a callback.
//!
//! Anything this cannot do the way the node-by-node reader does gives up with `None` before
//! any callback has run: anchors and aliases touching such a node, such a node inside a
//! mapping key or inside the value of a merge key, or a stream of several documents.

use std::collections::HashMap;
use std::sync::mpsc::{Receiver, sync_channel};

use oneyaml::Settings;
use oneyaml_syntax::event::{Event, EventKind, Sourced, TagForm, TagKey};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

use crate::errors::{Fail, to_pyerr};
use crate::hook;
use crate::input::with_text;
use crate::limits::Lim;
use crate::ops::{self, Conv, Mode};
use crate::pyevents::Maker;
use crate::reader::{Builder, PIPELINE_DEPTH, PIPELINE_MIN, internal_error, poll_signals, spawn};

/// Events between looks for a pending signal.
const SIGNAL_EVERY: usize = 4096;

type Ev<'s> = Sourced<'s, Event<'s>>;

/// Events that arrive in batches from the parser thread; waiting for one lets other Python
/// threads run.
struct Batches<'py, 's> {
    py: Python<'py>,
    rx: Receiver<Vec<Ev<'s>>>,
    current: std::vec::IntoIter<Ev<'s>>,
}

impl<'s> Iterator for Batches<'_, 's> {
    type Item = Ev<'s>;

    fn next(&mut self) -> Option<Ev<'s>> {
        loop {
            if let Some(ev) = self.current.next() {
                return Some(ev);
            }
            let rx = &mut self.rx;
            match self.py.detach(move || rx.recv()) {
                Ok(batch) => self.current = batch.into_iter(),
                Err(_) => return None,
            }
        }
    }
}

/// The value, in a one-item tuple, or None when the caller must read the document node by node.
pub fn load_hooked<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    limits: &Bound<'py, PyAny>,
    hooks: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let lim = Lim::from_py(limits)?;
    with_text(py, data, lim.input, |text| {
        let settings = lim.settings();
        if text.len() >= PIPELINE_MIN
            && let Some(done) = piped(py, text, lim, &settings, hooks)
        {
            return done;
        }
        drive(
            py,
            text,
            lim,
            hooks,
            oneyaml_syntax::events_with(text, &settings),
        )
    })
}

/// The same, with the engine on a second thread while this one builds. None when the OS will
/// not start the thread.
fn piped<'py>(
    py: Python<'py>,
    text: &str,
    lim: Lim,
    settings: &Settings,
    hooks: &Bound<'py, PyAny>,
) -> Option<PyResult<Option<Bound<'py, PyTuple>>>> {
    std::thread::scope(|scope| {
        let (tx, rx) = sync_channel(PIPELINE_DEPTH);
        // The thread touches no Python object: it reads `text`, which the caller keeps alive
        // and unchanged, and hands plain data over.
        let parser = spawn(scope, "oneyaml-parse", move || {
            if hook::is(hook::PANIC_IN_PARSER) {
                panic!("deliberate panic in the parser stage");
            }
            ops::pull(text, settings, |events| tx.send(events).is_ok());
        })
        .ok()?;
        let batches = Batches {
            py,
            rx,
            current: Vec::new().into_iter(),
        };
        let result = drive(py, text, lim, hooks, batches);
        // Dropping the receiver (it went with the iterator) stops the parser at its next
        // hand-over.
        if let Err(payload) = parser.join() {
            let fail = internal_error(payload.as_ref());
            return Some(Err(to_pyerr(py, text, fail, None)));
        }
        Some(result)
    })
}

fn drive<'py, 's>(
    py: Python<'py>,
    text: &'s str,
    lim: Lim,
    hooks: &Bound<'py, PyAny>,
    mut events: impl Iterator<Item = Ev<'s>>,
) -> PyResult<Option<Bound<'py, PyTuple>>> {
    let wants = hooks.getattr("wants")?;
    let build = hooks.getattr("build")?;
    let conv = Conv {
        cap: lim.digits,
        mode: Mode::Yaml11,
    };
    let mut builder = Builder::hooked(py, lim, false);
    let mut maker = Maker::new(py, text);
    let mut wanted: HashMap<TagKey<'s>, bool> = HashMap::new();
    let mut documents = 0usize;
    let mut n = 0usize;
    let fail = |f: Fail| to_pyerr(py, text, f, None);
    while let Some(ev) = events.next() {
        n += 1;
        if n % SIGNAL_EVERY == 0 {
            poll_signals(py).map_err(fail)?;
        }
        match ev.kind() {
            EventKind::Trivia(_)
            | EventKind::Warning(_)
            | EventKind::Directive(_)
            | EventKind::StreamStart
            | EventKind::StreamEnd => continue,
            EventKind::DocumentStart { .. } => {
                documents += 1;
                // A second document is an error of the load, unless a node was left to a
                // constructor: that stream is for the node-by-node reader to report.
                if documents > 1 && builder.has_holes() {
                    return Ok(None);
                }
            }
            EventKind::Alias if builder.has_holes() => return Ok(None),
            _ => {}
        }
        let tagged = matches!(
            ev.kind(),
            EventKind::Scalar { .. }
                | EventKind::MappingStart { .. }
                | EventKind::SequenceStart { .. }
        ) && ev.properties().is_some_and(|p| p.tag.is_some());
        if tagged && let Some(src) = ev.tag_source() {
            let key = src.key();
            let wanted_tag = match wanted.get(&key) {
                Some(w) => *w,
                None => {
                    let w = if src.form == TagForm::NonSpecific {
                        false
                    } else {
                        let tag = maker.tag(&ev).map_err(fail)?;
                        wants.call1((tag,))?.is_truthy()?
                    };
                    wanted.insert(key, w);
                    w
                }
            };
            if wanted_tag {
                let list = PyList::empty(py);
                let mut count = 0usize;
                let mut depth = 0usize;
                let mut stop = false;
                let at = ev.span().start;
                let mut current = Some(ev);
                // The node and everything below it, up to its own end.
                while let Some(e) = current.take() {
                    match e.kind() {
                        EventKind::Trivia(_) | EventKind::Warning(_) | EventKind::Directive(_) => {}
                        EventKind::Alias => stop = true,
                        EventKind::Error(_) => return Err(fail(Fail::from_event(&e))),
                        kind => {
                            if matches!(
                                kind,
                                EventKind::Scalar { .. }
                                    | EventKind::MappingStart { .. }
                                    | EventKind::SequenceStart { .. }
                            ) && e.properties().is_some_and(|p| p.anchor.is_some())
                            {
                                stop = true;
                            }
                            match kind {
                                EventKind::MappingStart { .. }
                                | EventKind::SequenceStart { .. } => {
                                    depth += 1;
                                }
                                EventKind::MappingEnd | EventKind::SequenceEnd => {
                                    depth = depth.saturating_sub(1);
                                }
                                _ => {}
                            }
                            if let Some(t) = maker.tuple(&e).map_err(fail)? {
                                list.append(t)?;
                                count += 1;
                            }
                        }
                    }
                    if depth > 0 {
                        current = events.next();
                    }
                }
                if stop || depth > 0 {
                    return Ok(None);
                }
                if !builder.defer(list, at, count).map_err(fail)? {
                    return Ok(None);
                }
                continue;
            }
        }
        if let Some(op) = ops::translate(&ev, conv) {
            builder.feed(op).map_err(fail)?;
        }
    }
    builder.resolve_holes(&build).map_err(fail)?;
    let value = builder.finish()?;
    Ok(Some(PyTuple::new(py, [value])?))
}
