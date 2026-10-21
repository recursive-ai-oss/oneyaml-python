//! `dump`: YAML text written straight from Python objects.
//!
//! There is no intermediate tree: the walk reads each object once and appends to one `String`.
//! Collections are walked with an explicit stack of frames, so nesting depth never becomes
//! native recursion. The rules (layout, quoting, escapes) are the ones the pure backend's
//! writer follows, and the two are checked against each other byte for byte.
//!
//! What is read of an object, the same on both backends: mapping entries from `items()` are
//! unpacked as `k, v = entry` is; byte strings are the raw buffer; `str`, `int` and `float`
//! subclasses are their base value; whether a value is a `Tagged` is decided once, from its
//! real type. Mutating a dict while it is written is a `RuntimeError`, as in `json`; the user's
//! own methods (`items`, `__iter__`, ...) raise what they raise. Growing the output is fallible:
//! running out of memory is a `MemoryError`, never an abort.

use std::collections::HashSet;
use std::sync::atomic::{AtomicUsize, Ordering};

use pyo3::exceptions::{PyMemoryError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::critical_section::with_critical_section;
use pyo3::types::list::BoundListIterator;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyIterator, PyList, PyMemoryView,
    PyString, PyTuple,
};

use crate::classes;
use crate::errors::raise;
use crate::hook;

/// Nesting of collections that is written (the pure backend uses the same number).
const MAX_DEPTH: usize = 10_000;
/// From this depth the containers on the current path are tracked, to refuse a value that
/// contains itself. Shallower values, which are nearly all of them, pay nothing.
const TRACK_FROM: usize = 64;
const MAX_IMPLICIT_KEY: usize = 1024;
const BASE64_LINE: usize = 76;
const YAML_TAG: &str = "tag:yaml.org,2002:";
/// Output bytes between looks for a pending signal: checked between entries and inside long
/// double-quoted and literal scalars. Copying one huge single-quoted or binary scalar is not
/// interrupted part way, nor is a single call into the interpreter.
const SIGNAL_BYTES: usize = 1 << 15;

/// Operations on the on-path set, counted so a test can check the work is linear.
static PATH_OPS: AtomicUsize = AtomicUsize::new(0);

pub fn path_ops(reset: bool) -> usize {
    if reset {
        PATH_OPS.swap(0, Ordering::Relaxed)
    } else {
        PATH_OPS.load(Ordering::Relaxed)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Map,
    Seq,
}

/// Where a frame takes its next entry from.
enum Src<'py> {
    /// The `items()` iterator of a mapping.
    Pairs(Bound<'py, PyIterator>),
    /// A plain dict on a build with a GIL: walked directly, with its size checked before
    /// every step. Entries yielded so far and the size at the start.
    Dict(
        pyo3::types::dict::BoundDictIterator<'py>,
        Bound<'py, PyDict>,
        usize,
        usize,
    ),
    List(BoundListIterator<'py>),
    Tuple(Bound<'py, PyTuple>, usize),
    /// A subclass of a sequence: read through its own iterator.
    Seq(Bound<'py, PyIterator>),
    /// A source whose first entry was already taken (to tell whether it is empty).
    Peek(Option<Item<'py>>, Box<Src<'py>>),
    /// An explicit entry: a key and a value, written after `? ` and `: `.
    Expl([Option<Bound<'py, PyAny>>; 2], usize),
}

struct Frame<'py> {
    src: Src<'py>,
    col: usize,
    /// Whether the frame's first line is already started (after `- `, `? `, `: `).
    inline: bool,
    /// A subclass container, kept alive while it is open. The exact builtin types are kept
    /// alive by their own source.
    _keep: Option<Bound<'py, PyAny>>,
    /// The container's address, which identifies it on the path; 0 for an explicit entry.
    addr: usize,
    /// Flow style: items written so far (a key and its value count as two).
    n: usize,
    /// Flow style: the value of a mapping entry whose key is being written.
    pending: Option<Bound<'py, PyAny>>,
}

enum Item<'py> {
    Pair(Bound<'py, PyAny>, Bound<'py, PyAny>),
    One(Bound<'py, PyAny>),
    /// One half of an explicit entry; true for the key.
    Half(Bound<'py, PyAny>, bool),
}

/// `k, v = entry`: any iterable of exactly two items.
fn unpack_pair<'py>(entry: Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    if let Ok(t) = entry.cast_exact::<PyTuple>() {
        return match t.as_slice() {
            [k, v] => Ok((k.clone(), v.clone())),
            _ => Err(PyValueError::new_err("expected a (key, value) pair")),
        };
    }
    let mut it = entry.try_iter()?;
    let wrong = || PyValueError::new_err("expected a (key, value) pair");
    let k = it.next().transpose()?.ok_or_else(wrong)?;
    let v = it.next().transpose()?.ok_or_else(wrong)?;
    if it.next().transpose()?.is_some() {
        return Err(wrong());
    }
    Ok((k, v))
}

impl Src<'_> {
    /// A placeholder source with nothing in it, for moving a source out of a frame.
    fn empty() -> Self {
        Src::Expl([None, None], 2)
    }
}

impl<'py> Frame<'py> {
    fn new(src: Src<'py>, col: usize, inline: bool, obj: Option<Bound<'py, PyAny>>) -> Self {
        Frame {
            src,
            col,
            inline,
            addr: obj.as_ref().map_or(0, |o| o.as_ptr() as usize),
            _keep: obj,
            n: 0,
            pending: None,
        }
    }

    fn is_map(&self) -> bool {
        match &self.src {
            Src::Pairs(_) | Src::Dict(..) => true,
            Src::Peek(_, inner) => matches!(**inner, Src::Pairs(_)),
            _ => false,
        }
    }

    fn next(&mut self, flow: bool) -> PyResult<Option<Item<'py>>> {
        if let Some(v) = self.pending.take() {
            return Ok(Some(Item::One(v)));
        }
        let item = self.next_raw()?;
        // Flow style writes a mapping as a flat run of keys and values.
        Ok(match item {
            Some(Item::Pair(k, v)) if flow => {
                self.pending = Some(v);
                Some(Item::One(k))
            }
            other => other,
        })
    }

    fn next_raw(&mut self) -> PyResult<Option<Item<'py>>> {
        if let Src::Peek(first, _) = &mut self.src {
            if let Some(i) = first.take() {
                return Ok(Some(i));
            }
            if let Src::Peek(_, inner) = std::mem::replace(&mut self.src, Src::empty()) {
                self.src = *inner;
            }
        }
        Ok(match &mut self.src {
            Src::Peek(..) => None,
            Src::Pairs(it) => match it.next() {
                Some(entry) => {
                    let (k, v) = unpack_pair(entry?)?;
                    Some(Item::Pair(k, v))
                }
                None => None,
            },
            Src::Dict(it, d, done, size) => {
                // The iterator panics when the dict changed size. Look first, and look and
                // step under one critical section so no other thread can change it between.
                let step = with_critical_section(d.as_any(), || {
                    if d.len() != *size {
                        return Err(PyRuntimeError::new_err(
                            "dictionary changed size during iteration",
                        ));
                    }
                    if *done >= *size {
                        return Ok(None);
                    }
                    *done += 1;
                    Ok(it.next().map(|(k, v)| Item::Pair(k, v)))
                });
                step?
            }
            Src::List(it) => it.next().map(Item::One),
            Src::Tuple(t, i) => {
                if *i < t.len() {
                    *i += 1;
                    Some(Item::One(t.get_item(*i - 1)?))
                } else {
                    None
                }
            }
            Src::Seq(it) => match it.next() {
                Some(v) => Some(Item::One(v?)),
                None => None,
            },
            Src::Expl(parts, i) => {
                if *i < 2 {
                    *i += 1;
                    let v = parts[*i - 1].take();
                    v.map(|v| Item::Half(v, *i == 1))
                } else {
                    None
                }
            }
        })
    }
}

/// What a value is, for writing.
enum Class<'py> {
    Null,
    Bool(bool),
    Int(Bound<'py, PyAny>),
    /// The value and an exact `float` object holding it (its repr gives the digits).
    Float(f64, Bound<'py, PyAny>),
    Str(Bound<'py, PyString>),
    Bytes(Bound<'py, PyBytes>),
    Coll(Coll<'py>),
}

struct Coll<'py> {
    kind: Kind,
    /// The mapping (a dict) or sequence itself.
    obj: Bound<'py, PyAny>,
}

struct Node<'py> {
    tag: Option<String>,
    class: Class<'py>,
}

struct Writer<'py> {
    py: Python<'py>,
    out: String,
    indent: usize,
    /// Scratch for rendering a key before it is known to fit on one line.
    key: String,
    /// Containers open on the current path, once it is deeper than `TRACK_FROM`.
    path: HashSet<usize>,
    /// Output length at which to look for a pending signal next.
    next_poll: usize,
}

pub fn dump<'py>(
    py: Python<'py>,
    objs: &Bound<'py, PyList>,
    opts: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyString>> {
    let (indent, flow, start): (Bound<'py, PyAny>, bool, bool) = opts.extract()?;
    if !indent.is_exact_instance_of::<PyInt>() {
        return Err(PyTypeError::new_err("indent must be an int"));
    }
    let indent = match indent.extract::<usize>() {
        Ok(n) if (1..=16).contains(&n) => n,
        _ => return Err(PyValueError::new_err("indent must be between 1 and 16")),
    };
    let mut w = Writer {
        py,
        out: String::new(),
        indent,
        key: String::new(),
        path: HashSet::new(),
        next_poll: SIGNAL_BYTES,
    };
    for (i, obj) in objs.iter().enumerate() {
        if i > 0 || start {
            w.ensure(8)?;
            w.out.push_str("---\n");
        }
        if flow {
            w.flow_doc(&obj)?;
        } else {
            w.block_doc(&obj)?;
        }
    }
    // The fallible constructor: a huge document is a MemoryError here, not a panic.
    PyString::from_bytes(py, w.out.as_bytes())
}

fn no_memory() -> PyErr {
    PyMemoryError::new_err("out of memory while writing the document")
}

/// Make room for `n` more bytes; the pushes that follow cannot reallocate.
#[inline(always)]
pub(crate) fn reserve(out: &mut String, n: usize) -> PyResult<()> {
    if out.capacity() - out.len() >= n {
        return Ok(());
    }
    grow(out, n)
}

#[cold]
#[inline(never)]
fn grow(out: &mut String, n: usize) -> PyResult<()> {
    out.try_reserve(n).map_err(|_| no_memory())
}

fn spaces(out: &mut String, n: usize) {
    const S: &str = "                                                                ";
    let mut n = n;
    while n > S.len() {
        out.push_str(S);
        n -= S.len();
    }
    out.push_str(&S[..n]);
}

// ---------------------------------------------------------------------------------------------
// Scalars
// ---------------------------------------------------------------------------------------------

fn is_reserved(b: &[u8]) -> bool {
    if b.len() > 5 {
        return false;
    }
    let mut buf = [0u8; 5];
    for (i, &c) in b.iter().enumerate() {
        buf[i] = c.to_ascii_lowercase();
    }
    matches!(
        &buf[..b.len()],
        b"y" | b"n" | b"yes" | b"no" | b"true" | b"false" | b"on" | b"off" | b"null" | b"~"
    )
}

const F_ESC: u8 = 1; // a control character, DEL, or a carriage return
const F_NL: u8 = 2;
const F_COLON: u8 = 4;
const F_HASH: u8 = 8;
const F_FLOW: u8 = 16; // , [ ] { }
const F_HIGH: u8 = 32; // a byte of a multi-byte character
const F_PUNCT: u8 = 64; // other punctuation, which a simple word never holds
const F_SPACE: u8 = 128; // space or tab

const fn byte_flags() -> [u8; 256] {
    let mut t = [0u8; 256];
    let mut i = 0;
    while i < 256 {
        let c = i as u8;
        t[i] = match c {
            0..=8 | 0x0b..=0x1f | 0x7f => F_ESC,
            b'\n' => F_NL,
            b':' => F_COLON,
            b'#' => F_HASH,
            b',' | b'[' | b']' | b'{' | b'}' => F_FLOW,
            b' ' | b'\t' => F_SPACE,
            0x80..=0xff => F_HIGH,
            b'_' | b'.' | b'/' | b'-' => 0,
            c if c.is_ascii_alphanumeric() => 0,
            _ => F_PUNCT,
        };
        i += 1;
    }
    t
}

static BYTE_FLAGS: [u8; 256] = byte_flags();

/// What one pass over a string finds out about it.
struct Scan {
    flags: u8,
    /// `:` followed by a space or tab.
    colon_space: bool,
    /// A space or tab followed by `#`.
    space_hash: bool,
    tab: bool,
    double_space: bool,
}

impl Scan {
    fn of(b: &[u8]) -> Scan {
        let mut sc = Scan {
            flags: 0,
            colon_space: false,
            space_hash: false,
            tab: false,
            double_space: false,
        };
        let mut prev = 0u8;
        let mut i = 0;
        while i < b.len() {
            let c = b[i];
            let f = BYTE_FLAGS[c as usize];
            sc.flags |= f;
            if f & (F_SPACE | F_HASH | F_HIGH) != 0 {
                if f & F_SPACE != 0 {
                    sc.colon_space |= prev == b':';
                    sc.double_space |= prev == b' ' && c == b' ';
                    sc.tab |= c == b'\t';
                } else if f & F_HASH != 0 {
                    sc.space_hash |= prev == b' ' || prev == b'\t';
                } else if high_escape(b, i) {
                    sc.flags |= F_ESC;
                }
            }
            prev = c;
            i += 1;
        }
        sc
    }

    /// Words and single spaces only, starting with a letter: nothing to decide about it.
    fn simple(&self, b: &[u8]) -> bool {
        self.flags & (F_ESC | F_NL | F_COLON | F_HASH | F_FLOW | F_HIGH | F_PUNCT) == 0
            && !self.tab
            && !self.double_space
            && b[0].is_ascii_alphabetic()
            && b[b.len() - 1] != b' '
    }
}

fn numberish_char(c: u8) -> bool {
    c.is_ascii_digit()
        || matches!(
            c,
            b'-' | b'+' | b'_' | b'.' | b':' | b',' | b' ' | b'a'..=b'f' | b'A'..=b'F'
                | b'x' | b'X' | b'o' | b'O' | b'T' | b't' | b'Z' | b'z'
        )
}

fn numberish(b: &[u8]) -> bool {
    let mut i = 0;
    while i < b.len() && matches!(b[i], b'-' | b'+' | b'.' | b'_') {
        i += 1;
    }
    if i < b.len() && b[i].is_ascii_digit() && b[i + 1..].iter().all(|&c| numberish_char(c)) {
        return true;
    }
    let mut j = 0;
    while j < b.len() && matches!(b[j], b'-' | b'+') {
        j += 1;
    }
    if j < b.len() && b[j] == b'.' && b.len() >= j + 4 {
        let w = [
            b[j + 1].to_ascii_lowercase(),
            b[j + 2].to_ascii_lowercase(),
            b[j + 3].to_ascii_lowercase(),
        ];
        return &w == b"inf" || &w == b"nan";
    }
    false
}

fn plain_ok(s: &str, sc: &Scan, flow: bool) -> bool {
    let b = s.as_bytes();
    if matches!(
        b[0],
        b' ' | b'\t'
            | b'-'
            | b'?'
            | b':'
            | b','
            | b'['
            | b']'
            | b'{'
            | b'}'
            | b'#'
            | b'&'
            | b'*'
            | b'!'
            | b'|'
            | b'>'
            | b'\''
            | b'"'
            | b'%'
            | b'@'
            | b'`'
    ) || matches!(b[b.len() - 1], b' ' | b'\t' | b':')
    {
        return false;
    }
    if sc.colon_space || sc.space_hash {
        return false;
    }
    if s.starts_with("---") || s.starts_with("...") || s == "<<" || s == "=" {
        return false;
    }
    if flow && sc.flags & (F_FLOW | F_COLON) != 0 {
        return false;
    }
    !(numberish(b) || is_reserved(b))
}

/// Whether the multi-byte character starting at `b[i]` is one that is written as an escape:
/// NEL and the other C1 controls, the line and paragraph separators, the byte order mark and
/// U+FFFE/U+FFFF, as their UTF-8 bytes.
fn high_escape(b: &[u8], i: usize) -> bool {
    let n1 = b.get(i + 1).copied();
    let n2 = b.get(i + 2).copied();
    match b[i] {
        0xC2 => matches!(n1, Some(0x80..=0x9F)),
        0xE2 => n1 == Some(0x80) && matches!(n2, Some(0xA8 | 0xA9)),
        0xEF => {
            (n1 == Some(0xBB) && n2 == Some(0xBF))
                || (n1 == Some(0xBF) && matches!(n2, Some(0xBE | 0xBF)))
        }
        _ => false,
    }
}

fn push_hex(out: &mut String, v: u32, digits: usize) {
    const H: &[u8; 16] = b"0123456789ABCDEF";
    for shift in (0..digits).rev() {
        out.push(H[(v >> (4 * shift)) as usize & 15] as char);
    }
}

/// An escape sequence is at most 6 bytes; the caller has reserved room.
fn push_escape(out: &mut String, c: char) {
    let u = c as u32;
    let named = match c {
        '\0' => "\\0",
        '\u{7}' => "\\a",
        '\u{8}' => "\\b",
        '\t' => "\\t",
        '\n' => "\\n",
        '\u{b}' => "\\v",
        '\u{c}' => "\\f",
        '\r' => "\\r",
        '\u{1b}' => "\\e",
        '"' => "\\\"",
        '\\' => "\\\\",
        '\u{85}' => "\\N",
        '\u{2028}' => "\\L",
        '\u{2029}' => "\\P",
        _ => "",
    };
    if !named.is_empty() {
        out.push_str(named);
    } else if u < 0x100 {
        out.push_str("\\x");
        push_hex(out, u, 2);
    } else {
        out.push_str("\\u");
        push_hex(out, u, 4);
    }
}

/// A double-quoted scalar. Runs of characters that need no escape are copied whole.
fn push_double(py: Python<'_>, out: &mut String, s: &str) -> PyResult<()> {
    reserve(out, 2)?;
    out.push('"');
    let mut next_poll = out.len() + SIGNAL_BYTES;
    let b = s.as_bytes();
    let mut start = 0;
    let mut i = 0;
    while i < b.len() {
        let c = b[i];
        let f = BYTE_FLAGS[c as usize];
        let special = matches!(c, b'"' | b'\\' | b'\t')
            || f & (F_ESC | F_NL) != 0
            || (f & F_HIGH != 0 && high_escape(b, i));
        if !special {
            i += 1;
            continue;
        }
        reserve(out, i - start + 8)?;
        out.push_str(&s[start..i]);
        if out.len() >= next_poll {
            next_poll = out.len() + SIGNAL_BYTES;
            poll_signals(py)?;
        }
        let Some(ch) = s[i..].chars().next() else {
            break;
        };
        push_escape(out, ch);
        i += ch.len_utf8();
        start = i;
    }
    reserve(out, b.len() - start + 2)?;
    out.push_str(&s[start..]);
    out.push('"');
    Ok(())
}

/// A literal block scalar without its final line break.
fn push_literal(
    py: Python<'_>,
    out: &mut String,
    header: &str,
    s: &str,
    cpad: usize,
) -> PyResult<()> {
    reserve(out, header.len() + 4)?;
    out.push_str(header);
    let body = if let Some(rest) = s.strip_suffix('\n') {
        if s.ends_with("\n\n") {
            out.push('+');
        }
        rest
    } else {
        out.push('-');
        s
    };
    out.push('\n');
    let mut next_poll = out.len() + SIGNAL_BYTES;
    for (i, line) in body.split('\n').enumerate() {
        if out.len() >= next_poll {
            next_poll = out.len() + SIGNAL_BYTES;
            poll_signals(py)?;
        }
        // Room for the line, its padding, the break before it and one spare byte.
        reserve(out, line.len() + cpad + 2)?;
        if i > 0 {
            out.push('\n');
        }
        if !line.is_empty() {
            spaces(out, cpad);
            out.push_str(line);
        }
    }
    Ok(())
}

/// A string as scalar text. `cpad` is the column of a block scalar's lines, or None where the
/// place cannot hold one (a key, flow style, an explicit value).
fn push_string(
    py: Python<'_>,
    out: &mut String,
    s: &str,
    flow: bool,
    cpad: Option<usize>,
) -> PyResult<()> {
    if s.is_empty() {
        reserve(out, 3)?;
        out.push_str("''");
        return Ok(());
    }
    let b = s.as_bytes();
    let sc = Scan::of(b);
    if sc.simple(b) {
        reserve(out, b.len() + 3)?;
        if b.len() > 5 || !is_reserved(b) {
            out.push_str(s);
        } else {
            out.push('\'');
            out.push_str(s);
            out.push('\'');
        }
        return Ok(());
    }
    if sc.flags & F_ESC != 0 {
        return push_double(py, out, s);
    }
    if sc.flags & F_NL != 0 {
        let lines_ok = !s.contains(" \n") && !s.contains("\t\n");
        return match cpad {
            Some(c)
                if lines_ok
                    && !matches!(b[0], b' ' | b'\t' | b'\n')
                    && !matches!(b[b.len() - 1], b' ' | b'\t') =>
            {
                push_literal(py, out, "|", s, c)
            }
            _ => push_double(py, out, s),
        };
    }
    if plain_ok(s, &sc, flow) {
        reserve(out, b.len() + 1)?;
        out.push_str(s);
        return Ok(());
    }
    let quotes = b.iter().filter(|&&c| c == b'\'').count();
    reserve(out, b.len() + quotes + 3)?;
    out.push('\'');
    for c in s.chars() {
        if c == '\'' {
            out.push('\'');
        }
        out.push(c);
    }
    out.push('\'');
    Ok(())
}

fn push_u64(out: &mut String, mut n: u64) {
    let mut buf = [0u8; 20];
    let mut i = buf.len();
    loop {
        i -= 1;
        buf[i] = b'0' + (n % 10) as u8;
        n /= 10;
        if n == 0 {
            break;
        }
    }
    out.push_str(std::str::from_utf8(&buf[i..]).unwrap_or("0"));
}

fn push_int(out: &mut String, v: &Bound<'_, PyAny>) -> PyResult<()> {
    reserve(out, 25)?;
    if let Ok(n) = v.extract::<i64>() {
        if n < 0 {
            out.push('-');
        }
        push_u64(out, n.unsigned_abs());
        return Ok(());
    }
    if let Ok(n) = v.extract::<u64>() {
        push_u64(out, n);
        return Ok(());
    }
    // `int.__repr__`, so a subclass that changes its own repr does not change the text.
    let py = v.py();
    let repr = py.get_type::<PyInt>().getattr("__repr__")?;
    let text = match repr.call1((v,)) {
        Ok(t) => t,
        // More digits than the interpreter converts to text.
        Err(e) if e.is_instance_of::<PyValueError>(py) => classes::int_text(py)?.call1((v,))?,
        Err(e) => return Err(e),
    };
    let text = text.cast::<PyString>()?.to_str()?;
    reserve(out, text.len() + 1)?;
    out.push_str(text);
    Ok(())
}

/// A small fixed buffer to format a number into without allocating.
struct StackBuf {
    bytes: [u8; 40],
    len: usize,
}

impl std::fmt::Write for StackBuf {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        let end = self.len + s.len();
        if end > self.bytes.len() {
            return Err(std::fmt::Error);
        }
        self.bytes[self.len..end].copy_from_slice(s.as_bytes());
        self.len = end;
        Ok(())
    }
}

/// Whether `f` is exactly half way between two `n`-digit decimals, the one case where Rust's
/// shortest digits can differ from Python's (Python takes the even last digit; Rust may take
/// the other). Such a value has an exact decimal expansion of `n + 1` digits ending in 5.
///
/// A double is `m * 2**q` with `m` odd; with `q < 0` its expansion is `m * 5**-q` over
/// `10**-q`, so the digit count is that of the integer `m * 5**-q`. Values that are whole
/// numbers cannot be such a midpoint, and neither can ones needing more than 27 factors of 5.
fn maybe_tie(f: f64, n: usize) -> bool {
    let bits = f.to_bits();
    let biased = ((bits >> 52) & 0x7ff) as i32;
    let frac = bits & ((1u64 << 52) - 1);
    if biased == 0 {
        return false; // subnormal: -q is far above 27
    }
    let mut m = frac | (1u64 << 52);
    let mut q = biased - 1075;
    let tz = m.trailing_zeros() as i32;
    m >>= tz;
    q += tz;
    if !(-27..0).contains(&q) {
        return false;
    }
    let exact = u128::from(m) * 5u128.pow(q.unsigned_abs());
    exact.ilog10() as usize + 1 == n + 1
}

/// A float as Python's `repr` writes it, with a point in the mantissa when the exponent form is
/// used, so every reader sees a float.
///
/// The digits are Rust's shortest ones, which are Python's too except where two shortest forms
/// are equally near the value (an exact tie: the value is exactly half way between them) and
/// Rust picks the other one. `maybe_tie` finds exactly those values (it needs a shortest form
/// of 15 or more digits); for them the interpreter itself writes the digits, so the two
/// writers cannot differ.
pub(crate) fn push_float(out: &mut String, f: f64, obj: &Bound<'_, PyAny>) -> PyResult<()> {
    use std::fmt::Write;
    reserve(out, 33)?;
    if f.is_nan() {
        out.push_str(".nan");
        return Ok(());
    }
    if f.is_infinite() {
        out.push_str(if f > 0.0 { ".inf" } else { "-.inf" });
        return Ok(());
    }
    if f == 0.0 {
        out.push_str(if f.is_sign_negative() { "-0.0" } else { "0.0" });
        return Ok(());
    }
    if f.fract() == 0.0 && f.abs() < 9_007_199_254_740_992.0 {
        // An integral value below 2**53 prints as its digits and ".0".
        if f < 0.0 {
            out.push('-');
        }
        push_u64(out, f.abs() as u64);
        out.push_str(".0");
        return Ok(());
    }
    let mut buf = StackBuf {
        bytes: [0; 40],
        len: 0,
    };
    if write!(buf, "{:e}", f.abs()).is_err() {
        return push_float_repr(out, obj);
    }
    let text = &buf.bytes[..buf.len];
    let Some(epos) = text.iter().position(|&c| c == b'e') else {
        return push_float_repr(out, obj);
    };
    let mut digits = [0u8; 24];
    let mut n = 0;
    for &c in &text[..epos] {
        if c != b'.' {
            if n == digits.len() {
                return push_float_repr(out, obj);
            }
            digits[n] = c;
            n += 1;
        }
    }
    let exp: i32 = std::str::from_utf8(&text[epos + 1..])
        .ok()
        .and_then(|e| e.parse().ok())
        .unwrap_or(0);
    if n >= 15 && maybe_tie(f, n) {
        return push_float_repr(out, obj);
    }
    let digits = std::str::from_utf8(&digits[..n]).unwrap_or("0");
    if f < 0.0 {
        out.push('-');
    }
    if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp as usize + 1;
            if n <= int_len {
                out.push_str(digits);
                for _ in n..int_len {
                    out.push('0');
                }
                out.push_str(".0");
            } else {
                out.push_str(&digits[..int_len]);
                out.push('.');
                out.push_str(&digits[int_len..]);
            }
        } else {
            out.push_str("0.");
            for _ in 0..(-exp - 1) {
                out.push('0');
            }
            out.push_str(digits);
        }
    } else {
        out.push_str(&digits[..1]);
        out.push('.');
        out.push_str(if n > 1 { &digits[1..] } else { "0" });
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        let m = exp.unsigned_abs();
        if m < 10 {
            out.push('0');
        }
        push_u64(out, m as u64);
    }
    Ok(())
}

/// The interpreter's own text for the float, with a point added to a bare mantissa.
fn push_float_repr(out: &mut String, obj: &Bound<'_, PyAny>) -> PyResult<()> {
    let r = obj.repr()?;
    let t = r.to_str()?;
    reserve(out, t.len() + 4)?;
    match t.find('e') {
        Some(i) if !t[..i].contains('.') => {
            out.push_str(&t[..i]);
            out.push_str(".0");
            out.push_str(&t[i..]);
        }
        _ => out.push_str(t),
    }
    Ok(())
}

fn push_base64(out: &mut String, bytes: &[u8]) {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for chunk in bytes.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
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
}

fn push_binary(out: &mut String, bytes: &[u8], cpad: Option<usize>) -> PyResult<()> {
    // 57 bytes make exactly one line of 76 base64 characters.
    const LINE_BYTES: usize = BASE64_LINE / 4 * 3;
    let lines = bytes.len() / LINE_BYTES + 1;
    reserve(
        out,
        bytes.len() / 3 * 4 + 8 + lines.saturating_mul(cpad.unwrap_or(0) + 2) + 13,
    )?;
    out.push_str("!!binary ");
    if bytes.is_empty() {
        out.push_str("''");
    } else if let Some(c) = cpad.filter(|_| bytes.len() > LINE_BYTES) {
        out.push('|');
        for chunk in bytes.chunks(LINE_BYTES) {
            out.push('\n');
            spaces(out, c);
            push_base64(out, chunk);
        }
    } else {
        push_base64(out, bytes);
    }
    Ok(())
}

fn lone_surrogate() -> PyErr {
    PyValueError::new_err("a string holds a lone surrogate, which YAML cannot represent")
}

fn tag_text(tag: &str) -> PyResult<String> {
    if tag.is_empty() {
        return Err(PyValueError::new_err("a tag cannot be empty"));
    }
    let shorthand = tag.starts_with(YAML_TAG) && tag.len() > YAML_TAG.len();
    let local = tag.starts_with('!') && tag.len() > 1;
    let body = if shorthand {
        &tag[YAML_TAG.len()..]
    } else if local {
        &tag[1..]
    } else {
        tag
    };
    let verbatim = !shorthand && !local;
    let mut out = String::new();
    out.try_reserve(body.len() + 4).map_err(|_| no_memory())?;
    out.push_str(if shorthand {
        "!!"
    } else if local || tag == "!" {
        "!"
    } else {
        "!<"
    });
    if tag == "!" {
        return Ok(out);
    }
    for c in body.chars() {
        let safe =
            c.is_ascii_alphanumeric() || "-#;/?:@&=+$_.~*'()".contains(c) || (verbatim && c == ',');
        if safe {
            reserve(&mut out, 1)?;
            out.push(c);
        } else if (' '..='~').contains(&c) {
            reserve(&mut out, 3)?;
            out.push('%');
            push_hex(&mut out, c as u32, 2);
        } else {
            return Err(PyValueError::new_err(format!(
                "a tag cannot hold the character {c:?}"
            )));
        }
    }
    if verbatim {
        out.push('>');
    }
    Ok(out)
}

/// Let a pending signal (Ctrl-C) end the dump: it surfaces as the usual `KeyboardInterrupt`.
pub(crate) fn poll_signals(py: Python<'_>) -> PyResult<()> {
    if hook::take(hook::INTERRUPT) {
        // The test switch stands in for a Ctrl-C: `interrupt_main` raises SIGINT in this
        // process as the interpreter's own signal handling would.
        py.import("_thread")?.call_method0("interrupt_main")?;
    }
    py.check_signals()
}

// ---------------------------------------------------------------------------------------------
// The walk
// ---------------------------------------------------------------------------------------------

impl<'py> Writer<'py> {
    fn ensure(&mut self, n: usize) -> PyResult<()> {
        reserve(&mut self.out, n)
    }

    /// Called once per entry: now and then, let a pending signal end the dump.
    #[inline(always)]
    fn tick(&mut self) -> PyResult<()> {
        if self.out.len() >= self.next_poll {
            self.next_poll = self.out.len() + SIGNAL_BYTES;
            poll_signals(self.py)?;
        }
        Ok(())
    }

    fn unsupported(v: &Bound<'py, PyAny>) -> PyErr {
        let name = v
            .get_type()
            .name()
            .map(|n| n.to_string())
            .unwrap_or_else(|_| "object".into());
        PyTypeError::new_err(format!("Object of type {name} is not YAML serializable"))
    }

    /// What `v` is. Whether it is a `Tagged` is decided here, once, from its real type; the
    /// tagged value is then read as a plain node, so tags never nest and nothing recurses.
    fn classify(&self, v: &Bound<'py, PyAny>) -> PyResult<Node<'py>> {
        let py = self.py;
        // The builtin types themselves cannot be a Tagged: no need to ask.
        if let Some(class) = Self::exact(v)? {
            return Ok(Node { tag: None, class });
        }
        let tagged = classes::tagged(py)?;
        if !v.get_type().is_subclass(tagged)? {
            return self.plain(v);
        }
        let tag_obj = classes::tagged_slot(py, 0)?.call1((v,))?;
        let tag = tag_obj.cast::<PyString>().map_err(|_| {
            PyTypeError::new_err(format!(
                "a tag must be a str, not {}",
                tag_obj
                    .get_type()
                    .name()
                    .map(|n| n.to_string())
                    .unwrap_or_default()
            ))
        })?;
        let tag = tag_text(tag.to_str().map_err(|_| lone_surrogate())?)?;
        let inner = classes::tagged_slot(py, 1)?.call1((v,))?;
        if inner.get_type().is_subclass(tagged)?
            || inner.is_instance_of::<PyBytes>()
            || inner.is_instance_of::<PyByteArray>()
            || inner.is_instance_of::<PyMemoryView>()
        {
            return Err(PyValueError::new_err("a node cannot carry two tags"));
        }
        let mut n = self.plain(&inner)?;
        n.tag = Some(tag);
        Ok(n)
    }

    /// A value of exactly one of the builtin types the writer knows.
    #[inline(always)]
    fn exact(v: &Bound<'py, PyAny>) -> PyResult<Option<Class<'py>>> {
        let coll = |kind| {
            Ok(Some(Class::Coll(Coll {
                kind,
                obj: v.clone(),
            })))
        };
        if v.is_exact_instance_of::<PyString>() {
            return Ok(Some(Class::Str(v.cast::<PyString>()?.clone())));
        }
        if v.is_none() {
            return Ok(Some(Class::Null));
        }
        if v.is_exact_instance_of::<PyInt>() {
            return Ok(Some(Class::Int(v.clone())));
        }
        if v.is_exact_instance_of::<PyDict>() {
            return coll(Kind::Map);
        }
        if v.is_exact_instance_of::<PyList>() || v.is_exact_instance_of::<PyTuple>() {
            return coll(Kind::Seq);
        }
        if v.is_exact_instance_of::<PyBool>() {
            return Ok(Some(Class::Bool(v.is_truthy()?)));
        }
        if v.is_exact_instance_of::<PyFloat>() {
            return Ok(Some(Class::Float(v.extract::<f64>()?, v.clone())));
        }
        Ok(None)
    }

    fn plain(&self, v: &Bound<'py, PyAny>) -> PyResult<Node<'py>> {
        let py = self.py;
        if let Some(class) = Self::exact(v)? {
            return Ok(Node { tag: None, class });
        }
        let node = |class| Ok(Node { tag: None, class });
        let coll = |kind, obj: &Bound<'py, PyAny>| {
            Ok(Node {
                tag: None,
                class: Class::Coll(Coll {
                    kind,
                    obj: obj.clone(),
                }),
            })
        };
        // Subclasses are read as their base type's data, never through overridden methods.
        if v.is_instance_of::<PyString>() {
            return node(Class::Str(v.cast::<PyString>()?.clone()));
        }
        if v.is_instance_of::<PyInt>() {
            return node(Class::Int(v.clone()));
        }
        if let Ok(f) = v.cast::<PyFloat>() {
            let x = f.value();
            return node(Class::Float(x, PyFloat::new(py, x).into_any()));
        }
        if v.is_instance_of::<PyDict>() {
            return coll(Kind::Map, v);
        }
        if v.is_instance_of::<PyList>() || v.is_instance_of::<PyTuple>() {
            return coll(Kind::Seq, v);
        }
        if v.get_type().is_subclass(classes::frozen_mapping(py)?)? {
            let items = classes::frozen_items(py)?.call1((v,))?;
            return coll(Kind::Map, &items);
        }
        if let Ok(b) = v.cast::<PyBytes>() {
            return node(Class::Bytes(b.clone()));
        }
        if v.is_instance_of::<PyByteArray>() || v.is_instance_of::<PyMemoryView>() {
            // Through the buffer protocol: the raw bytes, whatever the object's own methods say.
            let raw = PyMemoryView::from(v)?.call_method0("tobytes")?;
            return node(Class::Bytes(raw.cast_into::<PyBytes>()?));
        }
        Err(Self::unsupported(v))
    }

    /// Write a scalar node's text (with its tag).
    fn scalar(out: &mut String, n: &Node<'py>, flow: bool, cpad: Option<usize>) -> PyResult<()> {
        if let Some(t) = &n.tag {
            reserve(out, t.len() + 1)?;
            out.push_str(t);
            out.push(' ');
        }
        match &n.class {
            Class::Null => {
                reserve(out, 5)?;
                out.push_str("null");
            }
            Class::Bool(b) => {
                reserve(out, 6)?;
                out.push_str(if *b { "true" } else { "false" });
            }
            Class::Int(v) => push_int(out, v)?,
            Class::Float(f, obj) => push_float(out, *f, obj)?,
            Class::Str(v) => {
                let s = v.to_str().map_err(|_| lone_surrogate())?;
                push_string(v.py(), out, s, flow, cpad)?;
            }
            Class::Bytes(b) => push_binary(out, b.as_bytes(), cpad)?,
            Class::Coll(_) => unreachable!("a collection is not a scalar"),
        }
        Ok(())
    }

    fn circular() -> PyErr {
        PyValueError::new_err("Circular reference detected")
    }

    /// Open a frame: check the depth, and the path once it is deep enough to be worth tracking.
    fn push_frame(&mut self, frames: &mut Vec<Frame<'py>>, frame: Frame<'py>) -> PyResult<()> {
        let depth = frames.len() + 1;
        if depth > MAX_DEPTH {
            return Err(raise(
                self.py,
                &format!("the value nests deeper than {MAX_DEPTH} levels"),
                "limit",
                None,
                None,
            ));
        }
        if depth >= TRACK_FROM {
            if depth == TRACK_FROM {
                // The path so far was not tracked: take it in now.
                self.path.clear();
                for f in frames.iter() {
                    if f.addr != 0 {
                        PATH_OPS.fetch_add(1, Ordering::Relaxed);
                        if !self.path.insert(f.addr) {
                            return Err(Self::circular());
                        }
                    }
                }
            }
            if frame.addr != 0 {
                PATH_OPS.fetch_add(1, Ordering::Relaxed);
                if !self.path.insert(frame.addr) {
                    return Err(Self::circular());
                }
            }
        }
        frames.push(frame);
        Ok(())
    }

    fn pop_frame(&mut self, frames: &mut Vec<Frame<'py>>) {
        if frames.len() >= TRACK_FROM {
            if let Some(f) = frames.last().filter(|f| f.addr != 0) {
                PATH_OPS.fetch_add(1, Ordering::Relaxed);
                self.path.remove(&f.addr);
            }
        }
        frames.pop();
    }

    /// The frame for a collection's entries, or None when it has none. A dict, list or tuple
    /// is empty by its own length; a subclass by its first entry, since its `__len__` may say
    /// anything.
    fn make_frame(&self, c: &Coll<'py>, col: usize, inline: bool) -> PyResult<Option<Frame<'py>>> {
        let o = &c.obj;
        let addr = o.as_ptr() as usize;
        let src = if let Ok(d) = o.cast_exact::<PyDict>() {
            if d.is_empty() {
                return Ok(None);
            }
            // The iterator and the size it is checked against are taken together.
            let (it, size) = with_critical_section(d.as_any(), || (d.iter(), d.len()));
            Src::Dict(it, d.clone(), 0, size)
        } else if let Ok(l) = o.cast_exact::<PyList>() {
            if l.is_empty() {
                return Ok(None);
            }
            Src::List(l.iter())
        } else if let Ok(t) = o.cast_exact::<PyTuple>() {
            if t.is_empty() {
                return Ok(None);
            }
            Src::Tuple(t.clone(), 0)
        } else {
            let src = if c.kind == Kind::Map {
                Src::Pairs(o.call_method0("items")?.try_iter()?)
            } else {
                Src::Seq(o.try_iter()?)
            };
            let mut f = Frame::new(src, col, inline, Some(o.clone()));
            return Ok(match f.next_raw()? {
                None => None,
                Some(first) => {
                    let inner = std::mem::replace(&mut f.src, Src::empty());
                    f.src = Src::Peek(Some(first), Box::new(inner));
                    Some(f)
                }
            });
        };
        let mut f = Frame::new(src, col, inline, None);
        f.addr = addr;
        Ok(Some(f))
    }

    fn push_empty(&mut self, kind: Kind, tag: &Option<String>) -> PyResult<()> {
        self.ensure(tag.as_ref().map_or(0, |t| t.len() + 1) + 2)?;
        if let Some(t) = tag {
            self.out.push_str(t);
            self.out.push(' ');
        }
        self.out
            .push_str(if kind == Kind::Map { "{}" } else { "[]" });
        Ok(())
    }

    /// Start a collection after the head text already written (`key:`, `-`, `?`): the rest of
    /// the head, and the frame for the entries. None for an empty collection, written whole.
    fn open(
        &mut self,
        c: &Coll<'py>,
        tag: &Option<String>,
        keyed: bool,
        col: usize,
    ) -> PyResult<Option<Frame<'py>>> {
        let (ncol, inline) = if keyed {
            let deeper = tag.is_some() || c.kind == Kind::Map;
            (if deeper { col + self.indent } else { col }, false)
        } else {
            (col + 2, tag.is_none())
        };
        let Some(frame) = self.make_frame(c, ncol, inline)? else {
            self.ensure(1)?;
            self.out.push(' ');
            self.push_empty(c.kind, tag)?;
            self.out.push('\n');
            return Ok(None);
        };
        self.ensure(tag.as_ref().map_or(0, |t| t.len() + 1) + 2)?;
        if let Some(t) = tag {
            self.out.push(' ');
            self.out.push_str(t);
            self.out.push('\n');
        } else if keyed {
            self.out.push('\n');
        } else {
            self.out.push(' ');
        }
        Ok(Some(frame))
    }

    /// Whether a key is a scalar that fits on one line (its text is then left in `self.key`).
    fn key_text(&mut self, k: &Bound<'py, PyAny>) -> PyResult<bool> {
        let n = self.classify(k)?;
        if matches!(n.class, Class::Coll(_)) {
            return Ok(false);
        }
        self.key.clear();
        Self::scalar(&mut self.key, &n, false, None)?;
        let k = &self.key;
        Ok(k.len() <= MAX_IMPLICIT_KEY || k.chars().count() <= MAX_IMPLICIT_KEY)
    }

    fn block_doc(&mut self, root: &Bound<'py, PyAny>) -> PyResult<()> {
        let n = self.classify(root)?;
        let Class::Coll(c) = &n.class else {
            Self::scalar(&mut self.out, &n, false, Some(self.indent))?;
            self.ensure(1)?;
            self.out.push('\n');
            return Ok(());
        };
        let mut frames: Vec<Frame<'py>> = Vec::new();
        let Some(root_frame) = self.make_frame(c, 0, true)? else {
            self.push_empty(c.kind, &n.tag)?;
            self.ensure(1)?;
            self.out.push('\n');
            return Ok(());
        };
        if let Some(t) = &n.tag {
            self.ensure(t.len() + 1)?;
            self.out.push_str(t);
            self.out.push('\n');
        }
        self.push_frame(&mut frames, root_frame)?;
        let indent = self.indent;
        while let Some(top) = frames.last() {
            let col = top.col;
            let cpad = Some(col + indent);
            let mut need_pad = !top.inline;
            let mut descend: Option<Frame<'py>> = None;
            while let Some(item) = frames.last_mut().map_or(Ok(None), |f| f.next(false))? {
                self.tick()?;
                match item {
                    Item::Half(v, is_key) => {
                        let node = self.classify(&v)?;
                        self.ensure(col + 8)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push(if is_key { '?' } else { ':' });
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, false, col)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, &node, false, None)?;
                            self.out.push('\n');
                        }
                    }
                    Item::Pair(k, v) => {
                        if !self.key_text(&k)? {
                            // An explicit entry stands in for this one, at the same column.
                            descend = Some(Frame::new(
                                Src::Expl([Some(k), Some(v)], 0),
                                col,
                                !need_pad,
                                None,
                            ));
                            break;
                        }
                        let node = self.classify(&v)?;
                        self.ensure(col + self.key.len() + 4)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push_str(&self.key);
                        self.out.push(':');
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, true, col)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, &node, false, cpad)?;
                            self.out.push('\n');
                        }
                    }
                    Item::One(v) => {
                        let node = self.classify(&v)?;
                        self.ensure(col + 8)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push('-');
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, false, col)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, &node, false, cpad)?;
                            self.out.push('\n');
                        }
                    }
                }
            }
            match descend {
                Some(d) => {
                    if let Some(f) = frames.last_mut() {
                        f.inline = false; // the line it started is finished
                    }
                    self.push_frame(&mut frames, d)?;
                }
                None => self.pop_frame(&mut frames),
            }
        }
        Ok(())
    }

    fn flow_doc(&mut self, root: &Bound<'py, PyAny>) -> PyResult<()> {
        let n = self.classify(root)?;
        let Class::Coll(c) = &n.class else {
            Self::scalar(&mut self.out, &n, true, None)?;
            self.ensure(1)?;
            self.out.push('\n');
            return Ok(());
        };
        let mut frames: Vec<Frame<'py>> = Vec::new();
        let Some(root_frame) = self.make_frame(c, 0, false)? else {
            self.push_empty(c.kind, &n.tag)?;
            self.ensure(1)?;
            self.out.push('\n');
            return Ok(());
        };
        self.open_flow(c.kind, &n.tag)?;
        self.push_frame(&mut frames, root_frame)?;
        while let Some(top) = frames.last() {
            let is_map = top.is_map();
            let mut descend: Option<Frame<'py>> = None;
            while let Some(item) = frames.last_mut().map_or(Ok(None), |f| f.next(true))? {
                self.tick()?;
                let Item::One(v) = item else {
                    unreachable!("flow style reads entries one at a time")
                };
                let f = frames.last_mut().expect("a frame is open");
                // Between entries ", "; between a key and its value ": ".
                let sep = f.n > 0;
                let colon = is_map && f.n & 1 == 1;
                f.n += 1;
                self.ensure(4)?;
                if sep {
                    self.out.push_str(if colon { ": " } else { ", " });
                }
                let node = self.classify(&v)?;
                if let Class::Coll(c) = &node.class {
                    match self.make_frame(c, 0, false)? {
                        None => self.push_empty(c.kind, &node.tag)?,
                        Some(frame) => {
                            self.open_flow(c.kind, &node.tag)?;
                            descend = Some(frame);
                            break;
                        }
                    }
                } else {
                    Self::scalar(&mut self.out, &node, true, None)?;
                }
            }
            match descend {
                Some(d) => self.push_frame(&mut frames, d)?,
                None => {
                    self.pop_frame(&mut frames);
                    self.ensure(1)?;
                    self.out.push(if is_map { '}' } else { ']' });
                }
            }
        }
        self.ensure(1)?;
        self.out.push('\n');
        Ok(())
    }

    fn open_flow(&mut self, kind: Kind, tag: &Option<String>) -> PyResult<()> {
        self.ensure(tag.as_ref().map_or(0, |t| t.len() + 1) + 1)?;
        if let Some(t) = tag {
            self.out.push_str(t);
            self.out.push(' ');
        }
        self.out.push(if kind == Kind::Map { '{' } else { '[' });
        Ok(())
    }
}
