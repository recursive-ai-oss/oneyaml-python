//! Python objects to engine `Value`s, for assignments into a lossless document.
//!
//! Collections are walked on an explicit stack, so nesting never becomes native recursion; a
//! dict or list is copied out before its members are looked at, so nothing here holds a borrow
//! of Python data across a call that can run Python code. Subclasses are read by their base
//! value and never through overridden methods, except that a dict subclass is read through
//! `items()`, as a dump does.

use std::collections::HashSet;

use oneyaml::{Mapping, Tag, TaggedValue, Value};
use pyo3::buffer::PyBuffer;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple,
};

use crate::classes;
use crate::errors::Fail;

const BINARY: &str = "tag:yaml.org,2002:binary";

enum Frame<'py> {
    Seq {
        items: Vec<Value>,
        rest: std::vec::IntoIter<Bound<'py, PyAny>>,
        id: usize,
    },
    Map {
        map: Mapping,
        rest: std::vec::IntoIter<(Bound<'py, PyAny>, Bound<'py, PyAny>)>,
        key: Option<Value>,
        /// The value of the entry whose key was just handed out.
        pending: Option<Bound<'py, PyAny>>,
        id: usize,
    },
    Tagged {
        tag: String,
        id: usize,
    },
}

enum Step<'py> {
    Leaf(Value),
    Open(Frame<'py>, Option<Bound<'py, PyAny>>),
}

fn internal(what: &str) -> Fail {
    Fail::Yaml {
        category: "other",
        message: format!("internal error converting a value ({what}); this is a bug"),
        at: None,
    }
}

fn not_yaml(obj: &Bound<'_, PyAny>) -> PyErr {
    let name = obj
        .get_type()
        .name()
        .map(|n| n.to_string())
        .unwrap_or_default();
    PyTypeError::new_err(format!("cannot represent an object of type {name} in YAML"))
}

fn base64(bytes: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        out.push(T[(n >> 18) as usize & 63] as char);
        out.push(T[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            T[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            T[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

fn binary(bytes: &[u8]) -> Value {
    Value::Tagged(Box::new(TaggedValue {
        tag: Tag::new(BINARY),
        value: Value::String(base64(bytes)),
    }))
}

pub fn big_int(text: &str) -> Result<Value, Fail> {
    oneyaml::from_str::<Value>(text).map_err(|e| Fail::Yaml {
        category: "limit",
        message: format!("integer cannot be written: {e}"),
        at: None,
    })
}

fn yaml_fail(category: &'static str, message: impl Into<String>) -> Fail {
    Fail::Yaml {
        category,
        message: message.into(),
        at: None,
    }
}

/// The decimal digits of an `int`, read through `int` itself: a subclass's own `__repr__`,
/// `__str__` or `__format__` is never asked, since its answer would be parsed as YAML.
pub fn int_text(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    let py = obj.py();
    let text: String = py
        .get_type::<pyo3::types::PyInt>()
        .call_method1("__repr__", (obj,))?
        .extract()?;
    let digits = text.strip_prefix('-').unwrap_or(&text);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(PyTypeError::new_err("an integer has no decimal digits"));
    }
    Ok(text)
}

/// A scalar, or the frame to build a collection with.
fn step<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>, digits: usize) -> Result<Step<'py>, Fail> {
    if obj.is_none() {
        return Ok(Step::Leaf(Value::Null));
    }
    if let Ok(b) = obj.cast::<PyBool>() {
        return Ok(Step::Leaf(Value::Bool(b.is_true())));
    }
    if obj.is_instance_of::<PyInt>() {
        if obj.is_exact_instance_of::<PyInt>()
            && let Ok(i) = obj.extract::<i64>()
        {
            return Ok(Step::Leaf(Value::from(i)));
        }
        // Wider than 64 bits (or a subclass): written from its decimal digits, within the cap.
        let bits: u64 = obj.call_method0("bit_length")?.extract()?;
        if bits as f64 * std::f64::consts::LOG10_2 > digits as f64 + 1.0 {
            return Err(yaml_fail(
                "limit",
                format!("an integer of more than {digits} digits cannot be written"),
            ));
        }
        let text = match int_text(obj) {
            Ok(t) => t,
            Err(e) if e.is_instance_of::<PyValueError>(py) => {
                return Err(yaml_fail(
                    "limit",
                    "the integer has too many digits to write",
                ));
            }
            Err(e) => return Err(e.into()),
        };
        if let Ok(i) = text.parse::<i64>() {
            return Ok(Step::Leaf(Value::from(i)));
        }
        if text.trim_start_matches('-').len() > digits {
            return Err(yaml_fail(
                "limit",
                format!("an integer of more than {digits} digits cannot be written"),
            ));
        }
        return Ok(Step::Leaf(big_int(&text)?));
    }
    if let Ok(f) = obj.cast::<PyFloat>() {
        return Ok(Step::Leaf(Value::from(f.value())));
    }
    if let Ok(s) = obj.cast::<PyString>() {
        return match s.to_str() {
            Ok(t) => Ok(Step::Leaf(Value::String(t.to_string()))),
            Err(_) => Err(yaml_fail(
                "encoding",
                "the string holds a lone surrogate, which is not text",
            )),
        };
    }
    let id = obj.as_ptr() as usize;
    if let Ok(l) = obj.cast::<PyList>() {
        let items: Vec<_> = l.iter().collect();
        return Ok(open_seq(items, id));
    }
    if let Ok(t) = obj.cast::<PyTuple>() {
        let items: Vec<_> = t.iter().collect();
        return Ok(open_seq(items, id));
    }
    if let Ok(d) = obj.cast::<PyDict>() {
        let entries: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> =
            if obj.is_exact_instance_of::<PyDict>() {
                d.items().iter().map(pair).collect::<PyResult<_>>()?
            } else {
                collect(obj.call_method0("items")?.try_iter()?, |e| pair(e))?
            };
        return Ok(open_map(entries, id));
    }
    if obj.is_instance(classes::frozen_mapping(py)?)? {
        let inner = obj.getattr("_items")?;
        let d = inner.cast::<PyDict>().map_err(PyErr::from)?;
        let entries = d.items().iter().map(pair).collect::<PyResult<_>>()?;
        return Ok(open_map(entries, id));
    }
    if obj.is_instance(classes::tagged(py)?)? {
        let tag: String = obj.getattr("tag")?.extract()?;
        let value = obj.getattr("value")?;
        return Ok(Step::Open(Frame::Tagged { tag, id }, Some(value)));
    }
    if let Ok(b) = obj.cast::<PyBytes>() {
        return Ok(Step::Leaf(binary(b.as_bytes())));
    }
    if obj.is_instance_of::<PyByteArray>() || obj.is_instance_of::<pyo3::types::PyMemoryView>() {
        let buf = PyBuffer::<u8>::get(obj)?;
        let v = buf.to_vec(py)?;
        return Ok(Step::Leaf(binary(&v)));
    }
    // Any other mapping or sequence (a view of another document, say) reads as plain data.
    let abc = py.import("collections.abc")?;
    if obj.is_instance(&abc.getattr("Mapping")?)? {
        let v = collect(obj.call_method0("items")?.try_iter()?, |e| pair(e))?;
        return Ok(open_map(v, id));
    }
    if obj.is_instance(&abc.getattr("Sequence")?)? {
        let v = collect(obj.try_iter()?, Ok)?;
        return Ok(open_seq(v, id));
    }
    Err(not_yaml(obj).into())
}

/// Drain a user iterator into a vector. An iterator that never ends (a `Sequence` whose
/// `__getitem__` never raises `IndexError`) runs out of memory: that is a `MemoryError`, not an
/// abort, and Ctrl-C is honoured between items.
fn collect<'py, T>(
    iter: Bound<'py, pyo3::types::PyIterator>,
    mut each: impl FnMut(Bound<'py, PyAny>) -> PyResult<T>,
) -> PyResult<Vec<T>> {
    let py = iter.py();
    let mut v = Vec::new();
    for item in iter {
        if v.len() % 4096 == 0 {
            py.check_signals()?;
        }
        v.try_reserve(1)
            .map_err(|_| pyo3::exceptions::PyMemoryError::new_err("out of memory"))?;
        v.push(each(item?)?);
    }
    Ok(v)
}

fn pair<'py>(entry: Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let (k, v): (Bound<'py, PyAny>, Bound<'py, PyAny>) = entry.extract()?;
    Ok((k, v))
}

fn open_seq<'py>(items: Vec<Bound<'py, PyAny>>, id: usize) -> Step<'py> {
    Step::Open(
        Frame::Seq {
            items: Vec::with_capacity(items.len()),
            rest: items.into_iter(),
            id,
        },
        None,
    )
}

fn open_map<'py>(entries: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>, id: usize) -> Step<'py> {
    Step::Open(
        Frame::Map {
            map: Mapping::with_capacity(entries.len()),
            rest: entries.into_iter(),
            key: None,
            pending: None,
            id,
        },
        None,
    )
}

/// Convert `obj`. Nesting deeper than `max_depth` is a `limit` failure.
pub fn to_value<'py>(
    py: Python<'py>,
    obj: &Bound<'py, PyAny>,
    max_depth: usize,
    digits: usize,
) -> Result<Value, Fail> {
    let mut stack: Vec<Frame<'py>> = Vec::new();
    let mut on_path: HashSet<usize> = HashSet::new();
    let mut alive: Vec<Bound<'py, PyAny>> = Vec::new();
    // The objects of the frames on the path stay referenced by the caller's data; a container
    // that is dropped and reallocated while we run would need Python code, which we never run.
    let mut next: Option<Bound<'py, PyAny>> = Some(obj.clone());
    let mut done: Option<Value> = None;
    loop {
        if let Some(o) = next.take() {
            match step(py, &o, digits)? {
                Step::Leaf(v) => done = Some(v),
                Step::Open(frame, first) => {
                    // Keep the container alive for the whole conversion: a freed one's address
                    // could be handed to a new container, which would look like a cycle.
                    alive.push(o.clone());
                    let id = match &frame {
                        Frame::Seq { id, .. }
                        | Frame::Map { id, .. }
                        | Frame::Tagged { id, .. } => *id,
                    };
                    if !on_path.insert(id) {
                        return Err(yaml_fail("other", "circular reference"));
                    }
                    if stack.len() >= max_depth {
                        return Err(Fail::Yaml {
                            category: "limit",
                            message: format!(
                                "the value nests deeper than the limit of {max_depth}"
                            ),
                            at: None,
                        });
                    }
                    stack.push(frame);
                    next = first;
                    if next.is_some() {
                        continue;
                    }
                }
            }
        }
        // Hand a finished value to the frame that waits for it, then ask that frame for its
        // next member; a frame with none left is finished in turn.
        loop {
            if let Some(v) = done.take() {
                match stack.last_mut() {
                    None => return Ok(v),
                    Some(Frame::Seq { items, .. }) => items.push(v),
                    Some(Frame::Map { map, key, .. }) => match key.take() {
                        None => *key = Some(v),
                        Some(k) => {
                            if map.insert(k, v).is_some() {
                                return Err(yaml_fail("other", "duplicate mapping key"));
                            }
                        }
                    },
                    Some(Frame::Tagged { tag, id }) => {
                        let (tag, id) = (std::mem::take(tag), *id);
                        stack.pop();
                        on_path.remove(&id);
                        done = Some(Value::Tagged(Box::new(TaggedValue {
                            tag: Tag::new(tag),
                            value: v,
                        })));
                        continue;
                    }
                }
            }
            let Some(top) = stack.last_mut() else {
                return Err(internal("empty conversion stack"));
            };
            match top {
                Frame::Seq { rest, items, id } => match rest.next() {
                    Some(o) => {
                        next = Some(o);
                        break;
                    }
                    None => {
                        let (items, id) = (std::mem::take(items), *id);
                        stack.pop();
                        on_path.remove(&id);
                        done = Some(Value::from(items));
                    }
                },
                Frame::Map {
                    rest,
                    pending,
                    map,
                    id,
                    ..
                } => {
                    let following = match pending.take() {
                        Some(v) => Some(v),
                        None => rest.next().map(|(k, v)| {
                            *pending = Some(v);
                            k
                        }),
                    };
                    match following {
                        Some(o) => {
                            next = Some(o);
                            break;
                        }
                        None => {
                            let (map, id) = (std::mem::take(map), *id);
                            stack.pop();
                            on_path.remove(&id);
                            done = Some(Value::Mapping(map));
                        }
                    }
                }
                Frame::Tagged { .. } => {
                    // A tag frame is finished the moment its value arrives.
                    return Err(internal("tag frame without value"));
                }
            }
        }
    }
}
