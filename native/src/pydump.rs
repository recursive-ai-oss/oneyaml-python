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

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

use crate::scalar::is_timestamp;
use crate::writer::{poll_signals, push_float, reserve};

/// Output bytes between looks for a pending signal.
const SIGNAL_BYTES: usize = 1 << 15;
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
            reserve(&mut self.s, n)?;
            for _ in 0..n {
                self.s.push(' ');
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
                let doubled = text.replace('\'', "''");
                self.push(&doubled)?;
                self.column += clen(&doubled);
            } else {
                self.push(text)?;
                self.column += clen(text);
            }
            return self.indicator("'", false, false, false);
        }
        let chars: Vec<char> = text.chars().collect();
        let length = chars.len();
        let (mut spaces, mut breaks) = (false, false);
        let (mut start, mut end) = (0usize, 0usize);
        while end <= length {
            let ch = chars.get(end).copied();
            if spaces {
                if ch != Some(' ') {
                    if start + 1 == end
                        && self.column > self.best_width
                        && split
                        && start != 0
                        && end != length
                    {
                        self.write_indent()?;
                    } else {
                        let data: String = chars[start..end].iter().collect();
                        self.column += end - start;
                        self.push(&data)?;
                    }
                    start = end;
                }
            } else if breaks {
                if ch.is_none_or(|c| !is_break(c)) {
                    if chars[start] == '\n' {
                        self.line_break()?;
                    }
                    for &br in &chars[start..end] {
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
                let data: String = chars[start..end].iter().collect();
                self.column += end - start;
                self.push(&data)?;
                start = end;
            }
            if ch == Some('\'') {
                self.column += 2;
                self.push("''")?;
                start = end + 1;
            }
            if let Some(c) = ch {
                spaces = c == ' ';
                breaks = is_break(c);
            }
            end += 1;
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
        let chars: Vec<char> = text.chars().collect();
        let length = chars.len();
        let (mut start, mut end) = (0usize, 0usize);
        let mut marker = false;
        while end <= length {
            let ch = chars.get(end).copied();
            let needs = ch.is_none_or(needs_escape);
            if needs {
                if start < end {
                    let data: String = chars[start..end].iter().collect();
                    self.column += end - start;
                    self.push(&data)?;
                    start = end;
                    marker = false;
                }
                if let Some(c) = ch {
                    marker = false;
                    let data = escape(c);
                    self.column += data.len();
                    self.push(&data)?;
                    start = end + 1;
                }
            }
            if 0 < end
                && end + 1 < length
                && (ch == Some(' ') || start >= end)
                && self.column as i64 + (end as i64 - start as i64) > self.best_width as i64
                && split
                && !(marker && start >= end)
            {
                let mut data: String = if start < end {
                    chars[start..end].iter().collect()
                } else {
                    String::new()
                };
                data.push('\\');
                if start < end {
                    start = end;
                }
                self.column += clen(&data);
                self.push(&data)?;
                self.write_indent()?;
                self.whitespace = false;
                self.indention = false;
                marker = chars[start] == ' ';
                if marker {
                    self.column += 1;
                    self.push("\\")?;
                }
            }
            end += 1;
        }
        self.indicator("\"", false, false, false)
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
                stack.push(Frame::Seq {
                    list: l.clone(),
                    i: 0,
                    prev,
                });
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
                stack.push(Frame::Map { items, i: 0, prev });
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
        let mut items = Vec::with_capacity(list.len());
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
            let mut keyed: Vec<(String, usize)> = Vec::with_capacity(items.len());
            for (i, (k, _)) in items.iter().enumerate() {
                let s = k.cast::<PyString>().map_err(PyErr::from)?;
                keyed.push((s.to_str().map_err(|_| Stop::Bail)?.to_owned(), i));
            }
            keyed.sort_by(|a, b| a.0.cmp(&b.0));
            let mut slots: Vec<Option<(Bound<'py, PyAny>, Bound<'py, PyAny>)>> =
                items.into_iter().map(Some).collect();
            items = keyed
                .into_iter()
                .filter_map(|(_, i)| slots[i].take())
                .collect();
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

fn escape(c: char) -> String {
    let named = match c {
        '\0' => "0",
        '\u{7}' => "a",
        '\u{8}' => "b",
        '\t' => "t",
        '\n' => "n",
        '\u{b}' => "v",
        '\u{c}' => "f",
        '\r' => "r",
        '\u{1b}' => "e",
        '"' => "\"",
        '\\' => "\\",
        '\u{85}' => "N",
        '\u{a0}' => "_",
        '\u{2028}' => "L",
        '\u{2029}' => "P",
        _ => "",
    };
    if !named.is_empty() {
        return format!("\\{named}");
    }
    let u = c as u32;
    if u <= 0xff {
        format!("\\x{u:02X}")
    } else if u <= 0xffff {
        format!("\\u{u:04X}")
    } else {
        format!("\\U{u:08X}")
    }
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
