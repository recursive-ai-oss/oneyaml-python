//! `pyyaml_body`: the part of PyYAML's `dump` that plain data needs, written in one pass.
//!
//! The layout is PyYAML's (the Python writer in `compat/_pyemit.py` is the reference, and the
//! two are compared byte for byte): block style, plain, single-quoted and double-quoted scalars,
//! folding at the width, a sequence under a mapping key kept at the key's column.
//!
//! The walk handles `None`, `bool`, `int`, `float`, `str`, `list` and `dict` of exactly those
//! types. Anything else (a subclass, a tuple, bytes, a date, a key that is not a short
//! scalar), and any container that appears twice (PyYAML would write an anchor and an alias),
//! makes it give up before any output is used: the caller then runs the general writer, which
//! does everything. Collections are walked with an explicit stack.

use std::collections::HashSet;

use pyo3::exceptions::PyMemoryError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

use crate::scalar::is_timestamp;
use crate::writer::{poll_signals, push_float, reserve};

/// Output bytes between looks for a pending signal.
const SIGNAL_BYTES: usize = 1 << 15;
/// Nesting of collections that is written: the limit of the primary `dumps`. A deeper value
/// goes to the general writer, which refuses it with the module's own error.
const MAX_DEPTH: usize = 10_000;
const SPACES: &str = "                                                                ";
/// A key (its text and its tag) must be shorter than this to be written without `? `.
const KEY_LIMIT: usize = 128;

enum Stop {
    /// The general writer takes over.
    Bail,
    Py(PyErr),
}

impl From<PyErr> for Stop {
    fn from(e: PyErr) -> Self {
        Stop::Py(e)
    }
}

type R<T> = Result<T, Stop>;

// ---- what the implicit resolvers read as something other than a string -------------------------

fn run_us(b: &[u8]) -> usize {
    b.iter()
        .take_while(|&&c| c.is_ascii_digit() || c == b'_')
        .count()
}

/// `[eE][-+][0-9]+` and nothing else.
fn is_exp(b: &[u8]) -> bool {
    b.len() >= 3
        && matches!(b[0], b'e' | b'E')
        && matches!(b[1], b'+' | b'-')
        && b[2..].iter().all(u8::is_ascii_digit)
}

/// The int, float and timestamp rules of PyYAML's resolver, for text starting with a sign, a
/// digit or a dot.
fn numeric(b: &[u8]) -> bool {
    let (sign, r) = match b[0] {
        b'-' | b'+' => (true, &b[1..]),
        _ => (false, b),
    };
    let Some(&first) = r.first() else {
        return false;
    };
    if first == b'.' {
        if matches!(r, b".inf" | b".Inf" | b".INF") {
            return true;
        }
        if !sign && matches!(r, b".nan" | b".NaN" | b".NAN") {
            return true;
        }
        if !sign && r.len() > 1 && r[1].is_ascii_digit() {
            let i = 1 + run_us(&r[1..]);
            return i == r.len() || is_exp(&r[i..]);
        }
        return false;
    }
    if !first.is_ascii_digit() {
        return false;
    }
    if !sign && is_timestamp(b) {
        return true;
    }
    if first == b'0' && r.len() > 1 {
        match r[1] {
            b'b' if r.len() > 2 && r[2..].iter().all(|&c| matches!(c, b'0' | b'1' | b'_')) => {
                return true;
            }
            b'x' if r.len() > 2 && r[2..].iter().all(|&c| c.is_ascii_hexdigit() || c == b'_') => {
                return true;
            }
            _ => {}
        }
        if r[1..].iter().all(|&c| matches!(c, b'0'..=b'7' | b'_')) {
            return true;
        }
    }
    if r == b"0" || (first != b'0' && r.iter().all(|&c| c.is_ascii_digit() || c == b'_')) {
        return true;
    }
    let i = run_us(r);
    if i < r.len() && r[i] == b'.' {
        let j = i + 1 + run_us(&r[i + 1..]);
        if j == r.len() || is_exp(&r[j..]) {
            return true;
        }
    }
    if i < r.len() && r[i] == b':' {
        let mut k = i;
        while k < r.len() && r[k] == b':' {
            match (r.get(k + 1), r.get(k + 2)) {
                (Some(x), Some(y)) if (b'0'..=b'5').contains(x) && y.is_ascii_digit() => k += 3,
                (Some(x), _) if x.is_ascii_digit() => k += 2,
                _ => return false,
            }
        }
        if k == r.len() {
            return first != b'0';
        }
        return r[k] == b'.' && r[k + 1..].iter().all(|&c| c.is_ascii_digit() || c == b'_');
    }
    false
}

/// Whether a plain scalar with this text would be read as something other than a string by the
/// standard implicit resolvers (so a string like it must be quoted).
fn resolves_non_str(s: &str) -> bool {
    let b = s.as_bytes();
    let Some(&c) = b.first() else {
        return true; // the empty string is null
    };
    match c {
        b'y' | b'Y' | b'n' | b'N' | b't' | b'T' | b'f' | b'F' | b'o' | b'O' => matches!(
            s,
            "yes"
                | "Yes"
                | "YES"
                | "no"
                | "No"
                | "NO"
                | "true"
                | "True"
                | "TRUE"
                | "false"
                | "False"
                | "FALSE"
                | "on"
                | "On"
                | "ON"
                | "off"
                | "Off"
                | "OFF"
                | "null"
                | "Null"
                | "NULL"
        ),
        b'~' => s == "~",
        b'<' => s == "<<",
        b'=' => s == "=",
        b'-' | b'+' | b'.' | b'0'..=b'9' => numeric(b),
        _ => false,
    }
}

// ---- scalar analysis ---------------------------------------------------------------------------

struct Analysis {
    empty: bool,
    multiline: bool,
    block_plain: bool,
    single: bool,
}

fn is_ws(c: char) -> bool {
    matches!(
        c,
        '\0' | ' ' | '\t' | '\r' | '\n' | '\u{85}' | '\u{2028}' | '\u{2029}'
    )
}

fn is_break(c: char) -> bool {
    matches!(c, '\n' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

fn analyze(s: &str, allow_unicode: bool) -> Analysis {
    if s.is_empty() {
        return Analysis {
            empty: true,
            multiline: false,
            block_plain: true,
            single: true,
        };
    }
    let mut block_indicators = s.starts_with("---") || s.starts_with("...");
    let (mut line_breaks, mut special) = (false, false);
    let (mut leading_space, mut leading_break) = (false, false);
    let (mut trailing_space, mut trailing_break) = (false, false);
    let (mut break_space, mut space_break) = (false, false);
    let mut preceded_by_whitespace = true;
    let (mut previous_space, mut previous_break) = (false, false);
    let mut chars = s.chars().peekable();
    let mut index = 0usize;
    while let Some(ch) = chars.next() {
        let next = chars.peek().copied();
        let last = next.is_none();
        let followed_by_whitespace = next.is_none_or(is_ws);
        if index == 0 {
            if "#,[]{}&*!|>'\"%@`".contains(ch) {
                block_indicators = true;
            }
            if (ch == '?' || ch == ':' || ch == '-') && followed_by_whitespace {
                block_indicators = true;
            }
        } else {
            if ch == ':' && followed_by_whitespace {
                block_indicators = true;
            }
            if ch == '#' && preceded_by_whitespace {
                block_indicators = true;
            }
        }
        if is_break(ch) {
            line_breaks = true;
            if ch != '\n' {
                special = true;
            }
        }
        if !(ch == '\n' || (' '..='~').contains(&ch)) {
            let unicode = (ch == '\u{85}'
                || ('\u{a0}'..='\u{d7ff}').contains(&ch)
                || ('\u{e000}'..='\u{fffd}').contains(&ch)
                || ('\u{10000}'..'\u{10ffff}').contains(&ch))
                && ch != '\u{feff}';
            if !unicode || !allow_unicode {
                special = true;
            }
        }
        if ch == ' ' {
            leading_space |= index == 0;
            trailing_space |= last;
            break_space |= previous_break;
            previous_space = true;
            previous_break = false;
        } else if is_break(ch) {
            leading_break |= index == 0;
            trailing_break |= last;
            space_break |= previous_space;
            previous_space = false;
            previous_break = true;
        } else {
            previous_space = false;
            previous_break = false;
        }
        index += 1;
        preceded_by_whitespace = is_ws(ch);
    }
    let mut block_plain = true;
    let mut single = true;
    if leading_space || leading_break || trailing_space || trailing_break {
        block_plain = false;
    }
    if break_space {
        block_plain = false;
        single = false;
    }
    if space_break || special {
        block_plain = false;
        single = false;
    }
    if line_breaks || block_indicators {
        block_plain = false;
    }
    Analysis {
        empty: false,
        multiline: line_breaks,
        block_plain,
        single,
    }
}

/// Length in characters, which is how PyYAML counts columns.
fn clen(s: &str) -> usize {
    if s.is_ascii() {
        s.len()
    } else {
        s.chars().count()
    }
}

fn no_memory() -> Stop {
    Stop::Py(PyMemoryError::new_err("out of memory while writing"))
}

/// A vector with room for `n` items, or a `MemoryError` (never an abort).
fn vec_with_capacity<T>(n: usize) -> R<Vec<T>> {
    let mut v = Vec::new();
    v.try_reserve_exact(n).map_err(|_| no_memory())?;
    Ok(v)
}

/// Opens a collection's frame; a value nested past the limit is left to the general writer.
fn open_frame<'py>(stack: &mut Vec<Frame<'py>>, frame: Frame<'py>) -> R<()> {
    if stack.len() >= MAX_DEPTH {
        return Err(Stop::Bail);
    }
    stack.try_reserve(1).map_err(|_| no_memory())?;
    stack.push(frame);
    Ok(())
}

// ---- the writer --------------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Ctx {
    Root,
    Seq,
    MapValue,
    SimpleKey,
}

enum Frame<'py> {
    Seq {
        list: Bound<'py, PyList>,
        i: usize,
        prev: i64,
    },
    Map {
        items: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>,
        i: usize,
        prev: i64,
    },
}

enum Step<'py> {
    Pop(i64),
    Item(Bound<'py, PyAny>),
    Entry(Bound<'py, PyAny>, Bound<'py, PyAny>),
}

struct Body<'py> {
    py: Python<'py>,
    s: String,
    column: usize,
    whitespace: bool,
    indention: bool,
    open_ended: bool,
    /// -1 stands for PyYAML's `None`.
    indent: i64,
    best_indent: i64,
    best_width: usize,
    allow_unicode: bool,
    sort_keys: bool,
    line_break: String,
    root_context: bool,
    mapping_context: bool,
    simple_key_context: bool,
    seen: HashSet<usize>,
    next_poll: usize,
}

/// What a value is, as far as this writer goes.
enum Value<'py> {
    Null,
    Bool(bool),
    Int(Bound<'py, PyAny>),
    Float(f64, Bound<'py, PyAny>),
    Str(Bound<'py, PyString>),
    List(Bound<'py, PyList>),
    Dict(Bound<'py, PyDict>),
}

fn classify<'py>(v: &Bound<'py, PyAny>) -> R<Value<'py>> {
    if v.is_exact_instance_of::<PyString>() {
        return Ok(Value::Str(
            v.cast::<PyString>().map_err(PyErr::from)?.clone(),
        ));
    }
    if v.is_none() {
        return Ok(Value::Null);
    }
    if v.is_exact_instance_of::<PyDict>() {
        return Ok(Value::Dict(
            v.cast::<PyDict>().map_err(PyErr::from)?.clone(),
        ));
    }
    if v.is_exact_instance_of::<PyList>() {
        return Ok(Value::List(
            v.cast::<PyList>().map_err(PyErr::from)?.clone(),
        ));
    }
    if v.is_exact_instance_of::<PyInt>() {
        return Ok(Value::Int(v.clone()));
    }
    if v.is_exact_instance_of::<PyBool>() {
        return Ok(Value::Bool(v.is_truthy()?));
    }
    if v.is_exact_instance_of::<PyFloat>() {
        return Ok(Value::Float(v.extract::<f64>()?, v.clone()));
    }
    Err(Stop::Bail)
}

impl<'py> Body<'py> {
    // -- cursor moves, as in PyYAML's emitter --

    fn push(&mut self, t: &str) -> R<()> {
        reserve(&mut self.s, t.len())?;
        self.s.push_str(t);
        if self.s.len() >= self.next_poll {
            self.next_poll = self.s.len() + SIGNAL_BYTES;
            poll_signals(self.py)?;
        }
        Ok(())
    }

    fn indicator(
        &mut self,
        ind: &str,
        need_whitespace: bool,
        whitespace: bool,
        indention: bool,
    ) -> R<()> {
        if self.whitespace || !need_whitespace {
            self.push(ind)?;
            self.column += ind.len();
        } else {
            self.push(" ")?;
            self.push(ind)?;
            self.column += ind.len() + 1;
        }
        self.whitespace = whitespace;
        self.indention = self.indention && indention;
        self.open_ended = false;
        Ok(())
    }

    fn line_break(&mut self) -> R<()> {
        self.whitespace = true;
        self.indention = true;
        self.column = 0;
        let nl = std::mem::take(&mut self.line_break);
        let r = self.push(&nl);
        self.line_break = nl;
        r
    }

    fn write_indent(&mut self) -> R<()> {
        let indent = self.indent.max(0) as usize;
        if !self.indention || self.column > indent || (self.column == indent && !self.whitespace) {
            self.line_break()?;
        }
        if self.column < indent {
            self.whitespace = true;
            let n = indent - self.column;
            // In chunks of a constant string, not a character at a time.
            let mut left = n;
            while left > 0 {
                let k = left.min(SPACES.len());
                self.push(&SPACES[..k])?;
                left -= k;
            }
            self.column = indent;
        }
        Ok(())
    }

    // -- scalars --

    fn write_plain(&mut self, text: &str, split: bool) -> R<()> {
        if self.root_context {
            self.open_ended = true;
        }
        if text.is_empty() {
            return Ok(());
        }
        if !self.whitespace {
            self.push(" ")?;
            self.column += 1;
        }
        self.whitespace = false;
        self.indention = false;
        let n = clen(text);
        if !split || !text.contains(' ') || self.column + n <= self.best_width {
            self.push(text)?;
            self.column += n;
            return Ok(());
        }
        // Tokens: runs of spaces and runs of other characters.
        let mut rest = text;
        while !rest.is_empty() {
            let space = rest.as_bytes()[0] == b' ';
            let end = rest
                .find(|c: char| (c == ' ') != space)
                .unwrap_or(rest.len());
            let token = &rest[..end];
            rest = &rest[end..];
            if space && token.len() == 1 && self.column > self.best_width {
                self.write_indent()?;
                self.whitespace = false;
                self.indention = false;
            } else {
                self.push(token)?;
                self.column += clen(token);
            }
        }
        Ok(())
    }

    fn write_single(&mut self, text: &str, split: bool) -> R<()> {
        self.indicator("'", true, false, false)?;
        let quotes = text.matches('\'').count();
        if !text.chars().any(is_break)
            && (!text.contains(' ')
                || !split
                || self.column + clen(text) + quotes <= self.best_width)
        {
            if quotes > 0 {
                // Doubled quotes, written piece by piece: no copy of the text.
                let mut first = true;
                for piece in text.split('\'') {
                    if !first {
                        self.push("''")?;
                    }
                    first = false;
                    self.push(piece)?;
                }
                self.column += clen(text) + quotes;
            } else {
                self.push(text)?;
                self.column += clen(text);
            }
            return self.indicator("'", false, false, false);
        }
        // `start` and `end` are byte offsets; PyYAML's are character offsets, and the columns
        // are counted in characters.
        let length = text.len();
        let (mut spaces, mut breaks) = (false, false);
        let (mut start, mut end) = (0usize, 0usize);
        loop {
            let ch = text[end..].chars().next();
            if spaces {
                if ch != Some(' ') {
                    // Spaces are one byte each, so one byte is one space.
                    if start + 1 == end
                        && self.column > self.best_width
                        && split
                        && start != 0
                        && end != length
                    {
                        self.write_indent()?;
                    } else {
                        self.column += end - start;
                        self.push(&text[start..end])?;
                    }
                    start = end;
                }
            } else if breaks {
                if ch.is_none_or(|c| !is_break(c)) {
                    if text[start..].starts_with('\n') {
                        self.line_break()?;
                    }
                    for br in text[start..end].chars() {
                        if br == '\n' {
                            self.line_break()?;
                        } else {
                            self.raw_break(br)?;
                        }
                    }
                    self.write_indent()?;
                    start = end;
                }
            } else if ch.is_none_or(|c| c == ' ' || is_break(c) || c == '\'') && start < end {
                self.column += clen(&text[start..end]);
                self.push(&text[start..end])?;
                start = end;
            }
            if ch == Some('\'') {
                self.column += 2;
                self.push("''")?;
                start = end + 1;
            }
            let Some(c) = ch else {
                break;
            };
            spaces = c == ' ';
            breaks = is_break(c);
            end += c.len_utf8();
        }
        self.indicator("'", false, false, false)
    }

    /// A line break other than the configured one (NEL, LS, PS): never reached by data that
    /// goes through the analysis, which sends those to double quotes; kept for the algorithm.
    fn raw_break(&mut self, br: char) -> R<()> {
        self.whitespace = true;
        self.indention = true;
        self.column = 0;
        let mut buf = [0u8; 4];
        self.push(br.encode_utf8(&mut buf))
    }

    fn write_double(&mut self, text: &str, split: bool) -> R<()> {
        self.indicator("\"", true, false, false)?;
        let allow_unicode = self.allow_unicode;
        let needs_escape = |c: char| {
            matches!(
                c,
                '"' | '\\' | '\u{85}' | '\u{2028}' | '\u{2029}' | '\u{feff}'
            ) || !((' '..='~').contains(&c)
                || (allow_unicode
                    && (('\u{a0}'..='\u{d7ff}').contains(&c)
                        || ('\u{e000}'..='\u{fffd}').contains(&c))))
        };
        let n = clen(text);
        if !text.chars().any(needs_escape)
            && (!split || !text.contains(' ') || self.column + n <= self.best_width)
        {
            self.push(text)?;
            self.column += n;
            return self.indicator("\"", false, false, false);
        }
        // Byte offsets again (see `write_single`); a column is a number of characters.
        let length = text.len();
        let (mut start, mut end) = (0usize, 0usize);
        let mut marker = false;
        loop {
            let ch = text[end..].chars().next();
            let needs = ch.is_none_or(needs_escape);
            if needs {
                if start < end {
                    self.column += clen(&text[start..end]);
                    self.push(&text[start..end])?;
                    start = end;
                    marker = false;
                }
                if let Some(c) = ch {
                    marker = false;
                    let n = self.push_escape(c)?;
                    self.column += n;
                    start = end + c.len_utf8();
                }
            }
            // Characters from `start` to `end`; one less than none when an escape ended at `end`.
            let pending: i64 = match start.cmp(&end) {
                std::cmp::Ordering::Less => clen(&text[start..end]) as i64,
                std::cmp::Ordering::Equal => 0,
                std::cmp::Ordering::Greater => -1,
            };
            let more_than_one_left = ch.is_some_and(|c| end + c.len_utf8() < length);
            if 0 < end
                && more_than_one_left
                && (ch == Some(' ') || start >= end)
                && self.column as i64 + pending > self.best_width as i64
                && split
                && !(marker && start >= end)
            {
                if start < end {
                    self.column += clen(&text[start..end]);
                    self.push(&text[start..end])?;
                    start = end;
                }
                self.column += 1;
                self.push("\\")?;
                self.write_indent()?;
                self.whitespace = false;
                self.indention = false;
                marker = text[start..].starts_with(' ');
                if marker {
                    self.column += 1;
                    self.push("\\")?;
                }
            }
            let Some(c) = ch else {
                break;
            };
            end += c.len_utf8();
        }
        self.indicator("\"", false, false, false)
    }

    /// The escape of `c` (`\n`, `\x1B`, `\u2028`, ...) appended to the output; returns its
    /// length in characters.
    fn push_escape(&mut self, c: char) -> R<usize> {
        let named: Option<&str> = match c {
            '\0' => Some("\\0"),
            '\u{7}' => Some("\\a"),
            '\u{8}' => Some("\\b"),
            '\t' => Some("\\t"),
            '\n' => Some("\\n"),
            '\u{b}' => Some("\\v"),
            '\u{c}' => Some("\\f"),
            '\r' => Some("\\r"),
            '\u{1b}' => Some("\\e"),
            '"' => Some("\\\""),
            '\\' => Some("\\\\"),
            '\u{85}' => Some("\\N"),
            '\u{a0}' => Some("\\_"),
            '\u{2028}' => Some("\\L"),
            '\u{2029}' => Some("\\P"),
            _ => None,
        };
        if let Some(t) = named {
            self.push(t)?;
            return Ok(t.len());
        }
        let u = c as u32;
        let (marker, digits) = if u <= 0xff {
            (b'x', 2)
        } else if u <= 0xffff {
            (b'u', 4)
        } else {
            (b'U', 8)
        };
        let mut buf = [b'\\'; 10];
        buf[1] = marker;
        for i in 0..digits {
            let nibble = ((u >> (4 * (digits - 1 - i))) & 0xf) as usize;
            buf[2 + i] = b"0123456789ABCDEF"[nibble];
        }
        let len = 2 + digits;
        // The bytes are ASCII.
        self.push(std::str::from_utf8(&buf[..len]).unwrap_or("?"))?;
        Ok(len)
    }

    /// A string scalar in the current context.
    fn write_str(&mut self, text: &str) -> R<()> {
        let a = analyze(text, self.allow_unicode);
        let simple = self.simple_key_context;
        let plain =
            !resolves_non_str(text) && !(simple && (a.empty || a.multiline)) && a.block_plain;
        let split = !simple;
        let prev = self.indent;
        self.indent = if prev < 0 {
            self.best_indent
        } else {
            prev + self.best_indent
        };
        let r = if plain {
            self.write_plain(text, split)
        } else if a.single && !(simple && a.multiline) {
            self.write_single(text, split)
        } else {
            self.write_double(text, split)
        };
        self.indent = prev;
        r
    }

    /// A scalar the resolvers read back as its own type: always plain.
    fn write_bare(&mut self, text: &str) -> R<()> {
        let prev = self.indent;
        self.indent = if prev < 0 {
            self.best_indent
        } else {
            prev + self.best_indent
        };
        let r = self.write_plain(text, !self.simple_key_context);
        self.indent = prev;
        r
    }

    fn set_ctx(&mut self, ctx: Ctx) {
        self.root_context = ctx == Ctx::Root;
        self.mapping_context = matches!(ctx, Ctx::MapValue | Ctx::SimpleKey);
        self.simple_key_context = ctx == Ctx::SimpleKey;
    }

    fn write_scalar(&mut self, v: &Value<'py>) -> R<()> {
        match v {
            Value::Null => self.write_bare("null"),
            Value::Bool(b) => self.write_bare(if *b { "true" } else { "false" }),
            Value::Int(o) => {
                let t = int_text(o)?;
                self.write_bare(&t)
            }
            Value::Float(f, o) => {
                let mut t = String::new();
                push_float(&mut t, *f, o)?;
                self.write_bare(&t)
            }
            Value::Str(s) => {
                let text = s.to_str().map_err(|_| Stop::Bail)?;
                self.write_str(text)
            }
            _ => Err(Stop::Bail),
        }
    }

    // -- the walk --

    fn track(&mut self, o: &Bound<'py, PyAny>) -> R<()> {
        // A container met twice would be written as an anchor and an alias.
        self.seen.try_reserve(1).map_err(|_| no_memory())?;
        if self.seen.insert(o.as_ptr() as usize) {
            Ok(())
        } else {
            Err(Stop::Bail)
        }
    }

    fn begin(&mut self, v: &Bound<'py, PyAny>, ctx: Ctx, stack: &mut Vec<Frame<'py>>) -> R<()> {
        self.set_ctx(ctx);
        let value = classify(v)?;
        match &value {
            Value::List(l) => {
                self.track(v)?;
                if l.is_empty() {
                    return self.empty("[", "]");
                }
                let prev = self.indent;
                if prev < 0 {
                    self.indent = 0;
                } else if !(self.mapping_context && !self.indention) {
                    self.indent = prev + self.best_indent;
                }
                open_frame(
                    stack,
                    Frame::Seq {
                        list: l.clone(),
                        i: 0,
                        prev,
                    },
                )?;
                Ok(())
            }
            Value::Dict(d) => {
                self.track(v)?;
                if d.is_empty() {
                    return self.empty("{", "}");
                }
                let items = self.entries(d)?;
                let prev = self.indent;
                self.indent = if prev < 0 { 0 } else { prev + self.best_indent };
                open_frame(stack, Frame::Map { items, i: 0, prev })?;
                Ok(())
            }
            scalar => self.write_scalar(scalar),
        }
    }

    fn empty(&mut self, open: &str, close: &str) -> R<()> {
        self.indicator(open, true, true, false)?;
        self.indicator(close, false, false, false)
    }

    /// The entries of a dict, in the order PyYAML writes them: sorted by key when asked and
    /// when the keys can be compared (all strings here), as they are otherwise.
    fn entries(&self, d: &Bound<'py, PyDict>) -> R<Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> {
        // `items()` copies the entries under the dict's own lock, so a dict that changes while
        // the copy is written (another thread, a signal handler) cannot be seen half-way.
        let list = d.items();
        let mut items = vec_with_capacity(list.len())?;
        let (mut strs, mut others) = (0usize, 0usize);
        for entry in list.iter() {
            let k = entry.get_item(0)?;
            let v = entry.get_item(1)?;
            if k.is_exact_instance_of::<PyString>() {
                strs += 1;
            } else {
                others += 1;
            }
            items.push((k, v));
        }
        // Python's sort of (key, value) pairs: strings sort among themselves; a string and a
        // key of another kind cannot be compared, which leaves the order as it was; other
        // mixes are left to the general writer.
        if self.sort_keys && items.len() > 1 && others == 0 {
            // The keys are borrowed from the Python strings, not copied; the sort is in place
            // (a stable sort would allocate, and keys are unique, so stability is moot).
            let mut keyed: Vec<(&str, usize)> = vec_with_capacity(items.len())?;
            for (i, (k, _)) in items.iter().enumerate() {
                let s = k.cast::<PyString>().map_err(PyErr::from)?;
                keyed.push((s.to_str().map_err(|_| Stop::Bail)?, i));
            }
            keyed.sort_unstable();
            let mut sorted = vec_with_capacity(items.len())?;
            for &(_, i) in &keyed {
                sorted.push(items[i].clone());
            }
            items = sorted;
        } else if self.sort_keys && items.len() > 1 && strs == 0 {
            return Err(Stop::Bail);
        }
        Ok(items)
    }

    /// A key written as `key:` on one line, or the walk gives up.
    fn write_key(&mut self, k: &Bound<'py, PyAny>) -> R<()> {
        self.set_ctx(Ctx::SimpleKey);
        let value = classify(k)?;
        // PyYAML counts the key's tag (`!!str`, `!!int`, ...) against the limit as well.
        let (text, tag_len) = match &value {
            Value::Null => ("null".to_owned(), 6),
            Value::Bool(b) => ((if *b { "true" } else { "false" }).to_owned(), 6),
            Value::Int(o) => (int_text(o)?, 5),
            Value::Float(f, o) => {
                let mut t = String::new();
                push_float(&mut t, *f, o)?;
                (t, 7)
            }
            Value::Str(s) => {
                let text = s.to_str().map_err(|_| Stop::Bail)?;
                if text.is_empty() || text.chars().any(is_break) {
                    return Err(Stop::Bail);
                }
                if clen(text) + 5 >= KEY_LIMIT {
                    return Err(Stop::Bail);
                }
                return self.write_str(text);
            }
            _ => return Err(Stop::Bail),
        };
        if clen(&text) + tag_len >= KEY_LIMIT {
            return Err(Stop::Bail);
        }
        self.write_bare(&text)
    }

    fn run(&mut self, root: &Bound<'py, PyAny>) -> R<()> {
        let mut stack: Vec<Frame<'py>> = Vec::new();
        self.begin(root, Ctx::Root, &mut stack)?;
        while !stack.is_empty() {
            let step = match stack.last_mut() {
                Some(Frame::Seq { list, i, prev }) => {
                    // A list that shrinks while it is written ends the walk of it, as a list
                    // iterator does; reading by index never fails otherwise.
                    let item = if *i < list.len() {
                        list.get_item(*i).ok()
                    } else {
                        None
                    };
                    match item {
                        Some(item) => {
                            *i += 1;
                            Step::Item(item)
                        }
                        None => Step::Pop(*prev),
                    }
                }
                Some(Frame::Map { items, i, prev }) => {
                    if *i >= items.len() {
                        Step::Pop(*prev)
                    } else {
                        *i += 1;
                        let (k, v) = &items[*i - 1];
                        Step::Entry(k.clone(), v.clone())
                    }
                }
                None => break,
            };
            match step {
                Step::Pop(prev) => {
                    self.indent = prev;
                    stack.pop();
                }
                Step::Item(item) => {
                    self.write_indent()?;
                    self.indicator("-", true, false, true)?;
                    self.begin(&item, Ctx::Seq, &mut stack)?;
                }
                Step::Entry(k, v) => {
                    self.write_indent()?;
                    self.write_key(&k)?;
                    self.indicator(":", false, false, false)?;
                    self.begin(&v, Ctx::MapValue, &mut stack)?;
                }
            }
        }
        Ok(())
    }
}

/// The digits of an int. A number past the interpreter's limit on digits is its `ValueError`,
/// as in the Python writer and in PyYAML.
fn int_text(o: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(n) = o.extract::<i64>() {
        return Ok(n.to_string());
    }
    Ok(o.str()?.to_str()?.to_owned())
}

/// The text of one document body and whether a bare root scalar left it open-ended, or None
/// when the value needs the general writer. `marker` says the `---` line start was written.
#[allow(clippy::too_many_arguments)]
pub fn body<'py>(
    py: Python<'py>,
    obj: &Bound<'py, PyAny>,
    marker: bool,
    best_indent: usize,
    best_width: usize,
    allow_unicode: bool,
    sort_keys: bool,
    line_break: &str,
) -> PyResult<Option<(Bound<'py, PyString>, bool)>> {
    let mut w = Body {
        py,
        s: String::new(),
        column: if marker { 3 } else { 0 },
        whitespace: !marker,
        indention: !marker,
        open_ended: false,
        indent: -1,
        best_indent: best_indent as i64,
        best_width,
        allow_unicode,
        sort_keys,
        line_break: line_break.to_owned(),
        root_context: false,
        mapping_context: false,
        simple_key_context: false,
        seen: HashSet::new(),
        next_poll: SIGNAL_BYTES,
    };
    match w.run(obj) {
        Ok(()) => {
            // The fallible constructor: a huge document is a MemoryError, not a panic.
            let text = PyString::from_bytes(py, w.s.as_bytes())?;
            Ok(Some((text, w.open_ended)))
        }
        Err(Stop::Bail) => Ok(None),
        Err(Stop::Py(e)) => Err(e),
    }
}
