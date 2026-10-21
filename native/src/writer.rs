//! `dump`: YAML text written straight from Python objects.
//!
//! There is no intermediate tree: the walk reads each object once and appends to one `String`.
//! Collections are walked with an explicit stack of frames, so nesting depth never becomes
//! native recursion. The rules (layout, quoting, escapes) are the ones the pure backend's
//! writer follows, and the two are checked against each other byte for byte.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::dict::BoundDictIterator;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyInt, PyIterator, PyList, PyString, PyTuple,
};

use crate::classes;
use crate::errors::raise;

/// Nesting of collections that is written (the pure backend uses the same number).
const MAX_DEPTH: usize = 10_000;
const MAX_IMPLICIT_KEY: usize = 1024;
const BASE64_LINE: usize = 76;
const YAML_TAG: &str = "tag:yaml.org,2002:";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Map,
    Seq,
}

/// Where a frame takes its next entry from.
enum Src<'py> {
    Dict(BoundDictIterator<'py>),
    List(Bound<'py, PyList>, usize),
    Tuple(Bound<'py, PyTuple>, usize),
    /// A subclass of a collection: read through its own `items()` / iterator.
    Gen(Bound<'py, PyIterator>, Kind),
    /// An explicit entry: a key and a value, written after `? ` and `: `.
    Expl([Option<Bound<'py, PyAny>>; 2], usize),
}

struct Frame<'py> {
    src: Src<'py>,
    col: usize,
    /// Whether the frame's first line is already started (after `- `, `? `, `: `).
    inline: bool,
    /// Address of the container, for the cycle check; 0 for an explicit entry.
    obj: usize,
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

impl<'py> Frame<'py> {
    fn is_map(&self) -> bool {
        matches!(self.src, Src::Dict(_) | Src::Gen(_, Kind::Map))
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
        Ok(match &mut self.src {
            Src::Dict(it) => it.next().map(|(k, v)| Item::Pair(k, v)),
            Src::List(l, i) => {
                if *i < l.len() {
                    *i += 1;
                    Some(Item::One(l.get_item(*i - 1)?))
                } else {
                    None
                }
            }
            Src::Tuple(t, i) => {
                if *i < t.len() {
                    *i += 1;
                    Some(Item::One(t.get_item(*i - 1)?))
                } else {
                    None
                }
            }
            Src::Gen(it, Kind::Seq) => match it.next() {
                Some(v) => Some(Item::One(v?)),
                None => None,
            },
            Src::Gen(it, Kind::Map) => match it.next() {
                Some(pair) => {
                    let pair = pair?;
                    let t = pair.cast::<PyTuple>().map_err(|_| {
                        PyTypeError::new_err("items() of a mapping must give (key, value) pairs")
                    })?;
                    if t.len() != 2 {
                        return Err(PyTypeError::new_err(
                            "items() of a mapping must give (key, value) pairs",
                        ));
                    }
                    Some(Item::Pair(t.get_item(0)?, t.get_item(1)?))
                }
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
    Float(f64),
    Str(Bound<'py, PyAny>),
    Bytes(Vec<u8>),
    Coll(Coll<'py>),
}

struct Coll<'py> {
    kind: Kind,
    obj: Bound<'py, PyAny>,
    len: usize,
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
}

pub fn dump<'py>(
    py: Python<'py>,
    objs: &Bound<'py, PyList>,
    opts: (usize, bool, bool),
) -> PyResult<Bound<'py, PyString>> {
    let (indent, flow, start) = opts;
    let mut w = Writer {
        py,
        out: String::new(),
        indent,
        key: String::new(),
    };
    for (i, obj) in objs.iter().enumerate() {
        if i > 0 || start {
            w.out.push_str("---\n");
        }
        if flow {
            w.flow_doc(&obj)?;
        } else {
            w.block_doc(&obj)?;
        }
    }
    Ok(PyString::new(py, &w.out))
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

fn push_escape(out: &mut String, c: char) {
    let u = c as u32;
    match c {
        '\0' => out.push_str("\\0"),
        '\u{7}' => out.push_str("\\a"),
        '\u{8}' => out.push_str("\\b"),
        '\t' => out.push_str("\\t"),
        '\n' => out.push_str("\\n"),
        '\u{b}' => out.push_str("\\v"),
        '\u{c}' => out.push_str("\\f"),
        '\r' => out.push_str("\\r"),
        '\u{1b}' => out.push_str("\\e"),
        '"' => out.push_str("\\\""),
        '\\' => out.push_str("\\\\"),
        '\u{85}' => out.push_str("\\N"),
        '\u{2028}' => out.push_str("\\L"),
        '\u{2029}' => out.push_str("\\P"),
        _ if u < 0x100 => out.push_str(&format!("\\x{u:02X}")),
        _ => out.push_str(&format!("\\u{u:04X}")),
    }
}

/// A double-quoted scalar. Runs of characters that need no escape are copied whole.
fn push_double(out: &mut String, s: &str) {
    out.push('"');
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
        out.push_str(&s[start..i]);
        let Some(ch) = s[i..].chars().next() else {
            break;
        };
        push_escape(out, ch);
        i += ch.len_utf8();
        start = i;
    }
    out.push_str(&s[start..]);
    out.push('"');
}

/// A literal block scalar without its final line break.
fn push_literal(out: &mut String, header: &str, s: &str, cpad: usize) {
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
    for (i, line) in body.split('\n').enumerate() {
        if i > 0 {
            out.push('\n');
        }
        if !line.is_empty() {
            spaces(out, cpad);
            out.push_str(line);
        }
    }
}

/// A string as scalar text. `cpad` is the column of a block scalar's lines, or None where the
/// place cannot hold one (a key, flow style, an explicit value).
fn push_string(out: &mut String, s: &str, flow: bool, cpad: Option<usize>) {
    if s.is_empty() {
        out.push_str("''");
        return;
    }
    let b = s.as_bytes();
    let sc = Scan::of(b);
    if sc.simple(b) {
        if b.len() > 5 || !is_reserved(b) {
            out.push_str(s);
        } else {
            out.push('\'');
            out.push_str(s);
            out.push('\'');
        }
        return;
    }
    if sc.flags & F_ESC != 0 {
        push_double(out, s);
        return;
    }
    if sc.flags & F_NL != 0 {
        let lines_ok = !s.contains(" \n") && !s.contains("\t\n");
        match cpad {
            Some(c)
                if lines_ok
                    && !matches!(b[0], b' ' | b'\t' | b'\n')
                    && !matches!(b[b.len() - 1], b' ' | b'\t') =>
            {
                push_literal(out, "|", s, c);
            }
            _ => push_double(out, s),
        }
        return;
    }
    if plain_ok(s, &sc, flow) {
        out.push_str(s);
        return;
    }
    out.push('\'');
    for c in s.chars() {
        if c == '\'' {
            out.push('\'');
        }
        out.push(c);
    }
    out.push('\'');
}

fn push_float(out: &mut String, x: f64) {
    if x.is_nan() {
        out.push_str(".nan");
        return;
    }
    if x.is_infinite() {
        out.push_str(if x > 0.0 { ".inf" } else { "-.inf" });
        return;
    }
    // Shortest digits that read back, laid out as Python's repr does, with a point in the
    // mantissa and a signed exponent so every reader sees a float.
    let e = format!("{:e}", x.abs());
    let (mant, exp) = e.split_once('e').unwrap_or((&e, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    if x.is_sign_negative() {
        out.push('-');
    }
    if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                out.push_str(&digits);
                for _ in digits.len()..int_len {
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
            out.push_str(&digits);
        }
    } else {
        out.push_str(&digits[..1]);
        out.push('.');
        if digits.len() > 1 {
            out.push_str(&digits[1..]);
        } else {
            out.push('0');
        }
        out.push('e');
        out.push(if exp < 0 { '-' } else { '+' });
        let m = exp.unsigned_abs();
        if m < 10 {
            out.push('0');
        }
        out.push_str(&m.to_string());
    }
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

fn push_binary(out: &mut String, bytes: &[u8], cpad: Option<usize>) {
    out.push_str("!!binary ");
    if bytes.is_empty() {
        out.push_str("''");
        return;
    }
    let mut text = String::with_capacity(bytes.len() / 3 * 4 + 4);
    push_base64(&mut text, bytes);
    match cpad {
        Some(c) if text.len() > BASE64_LINE => {
            out.push('|');
            for line in text.as_bytes().chunks(BASE64_LINE) {
                out.push('\n');
                spaces(out, c);
                out.push_str(std::str::from_utf8(line).unwrap_or(""));
            }
        }
        _ => out.push_str(&text),
    }
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
    let mut out = String::with_capacity(body.len() + 4);
    for c in body.chars() {
        let safe =
            c.is_ascii_alphanumeric() || "-#;/?:@&=+$_.~*'()".contains(c) || (verbatim && c == ',');
        if safe {
            out.push(c);
        } else if (' '..='~').contains(&c) {
            out.push_str(&format!("%{:02X}", c as u32));
        } else {
            return Err(PyValueError::new_err(format!(
                "a tag cannot hold the character {c:?}"
            )));
        }
    }
    Ok(if shorthand {
        format!("!!{out}")
    } else if local {
        format!("!{out}")
    } else if tag == "!" {
        "!".to_string()
    } else {
        format!("!<{out}>")
    })
}

// ---------------------------------------------------------------------------------------------
// The walk
// ---------------------------------------------------------------------------------------------

fn lone_surrogate() -> PyErr {
    PyValueError::new_err("a string holds a lone surrogate, which YAML cannot represent")
}

impl<'py> Writer<'py> {
    fn unsupported(v: &Bound<'py, PyAny>) -> PyErr {
        let name = v
            .get_type()
            .name()
            .map(|n| n.to_string())
            .unwrap_or_else(|_| "object".into());
        PyTypeError::new_err(format!("Object of type {name} is not YAML serializable"))
    }

    fn classify(&self, v: &Bound<'py, PyAny>) -> PyResult<Node<'py>> {
        let py = self.py;
        let plain = |class| Ok(Node { tag: None, class });
        if v.is_none() {
            return plain(Class::Null);
        }
        if v.is_exact_instance_of::<PyString>() {
            return plain(Class::Str(v.clone()));
        }
        if v.is_exact_instance_of::<PyBool>() {
            return plain(Class::Bool(v.is_truthy()?));
        }
        if v.is_exact_instance_of::<PyInt>() {
            return plain(Class::Int(v.clone()));
        }
        if v.is_exact_instance_of::<PyDict>() {
            return plain(Class::Coll(Coll {
                kind: Kind::Map,
                obj: v.clone(),
                len: v.cast::<PyDict>()?.len(),
            }));
        }
        if v.is_exact_instance_of::<PyList>() {
            return plain(Class::Coll(Coll {
                kind: Kind::Seq,
                obj: v.clone(),
                len: v.cast::<PyList>()?.len(),
            }));
        }
        if v.is_exact_instance_of::<PyFloat>() {
            return plain(Class::Float(v.extract::<f64>()?));
        }
        let tagged = classes::tagged(py)?;
        let ty = v.get_type();
        if ty.is(tagged) || v.is_instance(tagged)? {
            let tag_obj = v.getattr("tag")?;
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
            let inner = v.getattr("value")?;
            if inner.is_instance(tagged)?
                || inner.is_instance_of::<PyBytes>()
                || inner.is_instance_of::<PyByteArray>()
            {
                return Err(PyValueError::new_err("a node cannot carry two tags"));
            }
            let mut n = self.classify(&inner)?;
            n.tag = Some(tag);
            return Ok(n);
        }
        if v.is_exact_instance_of::<PyTuple>() {
            return plain(Class::Coll(Coll {
                kind: Kind::Seq,
                obj: v.clone(),
                len: v.cast::<PyTuple>()?.len(),
            }));
        }
        if v.is_exact_instance_of::<PyBytes>() {
            return plain(Class::Bytes(v.cast::<PyBytes>()?.as_bytes().to_vec()));
        }
        if v.is_exact_instance_of::<PyByteArray>() {
            return plain(Class::Bytes(v.cast::<PyByteArray>()?.to_vec()));
        }
        if ty.is(classes::frozen_mapping(py)?) {
            let items = v.getattr("_items")?;
            let len = items.cast::<PyDict>()?.len();
            return plain(Class::Coll(Coll {
                kind: Kind::Map,
                obj: items,
                len,
            }));
        }
        // Subclasses behave as what they extend, as in the json module.
        if v.is_instance_of::<PyString>() {
            return plain(Class::Str(v.clone()));
        }
        if v.is_instance_of::<PyInt>() {
            return plain(Class::Int(v.clone()));
        }
        if let Ok(f) = v.cast::<PyFloat>() {
            return plain(Class::Float(f.value()));
        }
        if v.is_instance_of::<PyDict>() {
            return plain(Class::Coll(Coll {
                kind: Kind::Map,
                obj: v.clone(),
                len: v.len()?,
            }));
        }
        if v.is_instance_of::<PyList>() || v.is_instance_of::<PyTuple>() {
            return plain(Class::Coll(Coll {
                kind: Kind::Seq,
                obj: v.clone(),
                len: v.len()?,
            }));
        }
        if v.is_instance(classes::frozen_mapping(py)?)? {
            let items = v.getattr("_items")?;
            let len = items.cast::<PyDict>()?.len();
            return plain(Class::Coll(Coll {
                kind: Kind::Map,
                obj: items,
                len,
            }));
        }
        if v.is_instance_of::<PyBytes>() || v.is_instance_of::<PyByteArray>() {
            let b: Vec<u8> = v.extract()?;
            return plain(Class::Bytes(b));
        }
        Err(Self::unsupported(v))
    }

    /// Write a scalar node's text (with its tag).
    fn scalar(
        out: &mut String,
        py: Python<'py>,
        n: &Node<'py>,
        flow: bool,
        cpad: Option<usize>,
    ) -> PyResult<()> {
        if let Some(t) = &n.tag {
            out.push_str(t);
            out.push(' ');
        }
        match &n.class {
            Class::Null => out.push_str("null"),
            Class::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Class::Int(v) => push_int(out, py, v)?,
            Class::Float(f) => push_float(out, *f),
            Class::Str(v) => {
                let s = v.cast::<PyString>()?;
                let s = s.to_str().map_err(|_| lone_surrogate())?;
                push_string(out, s, flow, cpad);
            }
            Class::Bytes(b) => push_binary(out, b, cpad),
            Class::Coll(_) => unreachable!("a collection is not a scalar"),
        }
        Ok(())
    }

    fn check_path(&self, frames: &[Frame<'py>], obj: usize) -> PyResult<()> {
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
        if depth >= 64 && depth & (depth - 1) == 0 {
            let mut ids: Vec<usize> = frames.iter().map(|f| f.obj).filter(|&o| o != 0).collect();
            ids.push(obj);
            ids.sort_unstable();
            if ids.windows(2).any(|w| w[0] == w[1]) {
                return Err(PyValueError::new_err("Circular reference detected"));
            }
        }
        Ok(())
    }

    fn make_frame(&self, c: &Coll<'py>, col: usize, inline: bool) -> PyResult<Frame<'py>> {
        let o = &c.obj;
        let src = if o.is_exact_instance_of::<PyDict>() {
            Src::Dict(o.cast::<PyDict>()?.iter())
        } else if o.is_exact_instance_of::<PyList>() {
            Src::List(o.cast::<PyList>()?.clone(), 0)
        } else if o.is_exact_instance_of::<PyTuple>() {
            Src::Tuple(o.cast::<PyTuple>()?.clone(), 0)
        } else if c.kind == Kind::Map {
            Src::Gen(o.call_method0("items")?.try_iter()?, Kind::Map)
        } else {
            Src::Gen(o.try_iter()?, Kind::Seq)
        };
        Ok(Frame {
            src,
            col,
            inline,
            obj: o.as_ptr() as usize,
            n: 0,
            pending: None,
        })
    }

    /// Start a collection after the head text already written (`key:`, `-`, `?`): the rest of
    /// the head, and the frame for the entries. None for an empty collection, written whole.
    fn open(
        &mut self,
        c: &Coll<'py>,
        tag: &Option<String>,
        keyed: bool,
        col: usize,
        frames: &[Frame<'py>],
    ) -> PyResult<Option<Frame<'py>>> {
        if c.len == 0 {
            self.out.push(' ');
            self.push_empty(c.kind, tag);
            self.out.push('\n');
            return Ok(None);
        }
        self.check_path(frames, c.obj.as_ptr() as usize)?;
        let (ncol, inline);
        if keyed {
            if let Some(t) = tag {
                self.out.push(' ');
                self.out.push_str(t);
                ncol = col + self.indent;
            } else {
                ncol = if c.kind == Kind::Map {
                    col + self.indent
                } else {
                    col
                };
            }
            self.out.push('\n');
            inline = false;
        } else {
            ncol = col + 2;
            if let Some(t) = tag {
                self.out.push(' ');
                self.out.push_str(t);
                self.out.push('\n');
                inline = false;
            } else {
                self.out.push(' ');
                inline = true;
            }
        }
        Ok(Some(self.make_frame(c, ncol, inline)?))
    }

    fn push_empty(&mut self, kind: Kind, tag: &Option<String>) {
        if let Some(t) = tag {
            self.out.push_str(t);
            self.out.push(' ');
        }
        self.out
            .push_str(if kind == Kind::Map { "{}" } else { "[]" });
    }

    /// Whether a key is a scalar that fits on one line (its text is then left in `self.key`).
    fn key_text(&mut self, k: &Bound<'py, PyAny>) -> PyResult<bool> {
        let n = self.classify(k)?;
        if matches!(n.class, Class::Coll(_)) {
            return Ok(false);
        }
        self.key.clear();
        Self::scalar(&mut self.key, self.py, &n, false, None)?;
        let k = &self.key;
        Ok(k.len() <= MAX_IMPLICIT_KEY || k.chars().count() <= MAX_IMPLICIT_KEY)
    }

    fn block_doc(&mut self, root: &Bound<'py, PyAny>) -> PyResult<()> {
        let n = self.classify(root)?;
        let Class::Coll(c) = &n.class else {
            Self::scalar(&mut self.out, self.py, &n, false, Some(self.indent))?;
            self.out.push('\n');
            return Ok(());
        };
        if c.len == 0 {
            self.push_empty(c.kind, &n.tag);
            self.out.push('\n');
            return Ok(());
        }
        let mut frames: Vec<Frame<'py>> = Vec::new();
        self.check_path(&frames, c.obj.as_ptr() as usize)?;
        if let Some(t) = &n.tag {
            self.out.push_str(t);
            self.out.push('\n');
        }
        frames.push(self.make_frame(c, 0, true)?);
        let indent = self.indent;
        while let Some(top) = frames.last() {
            let col = top.col;
            let cpad = Some(col + indent);
            let mut need_pad = !top.inline;
            let mut descend: Option<Frame<'py>> = None;
            while let Some(item) = frames.last_mut().map_or(Ok(None), |f| f.next(false))? {
                match item {
                    Item::Half(v, is_key) => {
                        let node = self.classify(&v)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push(if is_key { '?' } else { ':' });
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, false, col, &frames)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, self.py, &node, false, None)?;
                            self.out.push('\n');
                        }
                    }
                    Item::Pair(k, v) => {
                        if !self.key_text(&k)? {
                            // An explicit entry stands in for this one, at the same column.
                            descend = Some(Frame {
                                src: Src::Expl([Some(k), Some(v)], 0),
                                col,
                                inline: !need_pad,
                                obj: 0,
                                n: 0,
                                pending: None,
                            });
                            break;
                        }
                        let node = self.classify(&v)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push_str(&self.key);
                        self.out.push(':');
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, true, col, &frames)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, self.py, &node, false, cpad)?;
                            self.out.push('\n');
                        }
                    }
                    Item::One(v) => {
                        let node = self.classify(&v)?;
                        if need_pad {
                            spaces(&mut self.out, col);
                        }
                        need_pad = true;
                        self.out.push('-');
                        if let Class::Coll(c) = &node.class {
                            descend = self.open(c, &node.tag, false, col, &frames)?;
                            if descend.is_some() {
                                break;
                            }
                        } else {
                            self.out.push(' ');
                            Self::scalar(&mut self.out, self.py, &node, false, cpad)?;
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
                    frames.push(d);
                }
                None => {
                    frames.pop();
                }
            }
        }
        Ok(())
    }

    fn flow_doc(&mut self, root: &Bound<'py, PyAny>) -> PyResult<()> {
        let n = self.classify(root)?;
        let Class::Coll(c) = &n.class else {
            Self::scalar(&mut self.out, self.py, &n, true, None)?;
            self.out.push('\n');
            return Ok(());
        };
        if c.len == 0 {
            self.push_empty(c.kind, &n.tag);
            self.out.push('\n');
            return Ok(());
        }
        let mut frames: Vec<Frame<'py>> = Vec::new();
        self.check_path(&frames, c.obj.as_ptr() as usize)?;
        self.open_flow(c, &n.tag, &mut frames)?;
        while let Some(top) = frames.last() {
            let is_map = top.is_map();
            let mut descend = false;
            while let Some(item) = frames.last_mut().map_or(Ok(None), |f| f.next(true))? {
                let Item::One(v) = item else { unreachable!() };
                let f = frames.last_mut().expect("a frame is open");
                // Between entries ", "; between a key and its value ": ".
                if f.n > 0 {
                    self.out
                        .push_str(if !is_map || f.n & 1 == 0 { ", " } else { ": " });
                }
                f.n += 1;
                let node = self.classify(&v)?;
                if let Class::Coll(c) = &node.class {
                    if c.len == 0 {
                        self.push_empty(c.kind, &node.tag);
                    } else {
                        self.check_path(&frames, c.obj.as_ptr() as usize)?;
                        self.open_flow(c, &node.tag, &mut frames)?;
                        descend = true;
                        break;
                    }
                } else {
                    Self::scalar(&mut self.out, self.py, &node, true, None)?;
                }
            }
            if !descend {
                frames.pop();
                self.out.push(if is_map { '}' } else { ']' });
            }
        }
        self.out.push('\n');
        Ok(())
    }

    fn open_flow(
        &mut self,
        c: &Coll<'py>,
        tag: &Option<String>,
        frames: &mut Vec<Frame<'py>>,
    ) -> PyResult<()> {
        if let Some(t) = tag {
            self.out.push_str(t);
            self.out.push(' ');
        }
        self.out.push(if c.kind == Kind::Map { '{' } else { '[' });
        frames.push(self.make_frame(c, 0, false)?);
        Ok(())
    }
}

fn push_int(out: &mut String, py: Python<'_>, v: &Bound<'_, PyAny>) -> PyResult<()> {
    if let Ok(n) = v.extract::<i64>() {
        out.push_str(&n.to_string());
        return Ok(());
    }
    if let Ok(n) = v.extract::<u64>() {
        out.push_str(&n.to_string());
        return Ok(());
    }
    // `int.__repr__`, so a subclass that changes its own repr does not change the text.
    let repr = py.get_type::<PyInt>().getattr("__repr__")?;
    let text = match repr.call1((v,)) {
        Ok(t) => t,
        // More digits than the interpreter converts to text.
        Err(e) if e.is_instance_of::<PyValueError>(py) => classes::int_text(py)?.call1((v,))?,
        Err(e) => return Err(e),
    };
    out.push_str(text.cast::<PyString>()?.to_str()?);
    Ok(())
}
