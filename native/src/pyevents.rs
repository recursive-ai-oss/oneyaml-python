//! `events`: the engine's event stream as the tuples the pure parser produces.
//!
//! The node composer behind the PyYAML loaders is written against those tuples (kind, offsets
//! into the decoded text, anchor, tag, style, value), so the native backend feeds it from here
//! and the pure parser is not needed. Offsets are character offsets of the text with its byte
//! order mark removed, as the pure parser counts them; the engine counts bytes.
//!
//! Block collections start and end at the engine's zero-width events; a flow collection ends at
//! its closing bracket. A consumer that wants "where the collection is over" skips white space
//! and commas from there, which gives the same place as skipping from the end of the last item.

use std::collections::HashMap;

use oneyaml_syntax::Span;
use oneyaml_syntax::event::{
    CollectionStyle, Event, EventKind, ScalarStyle, Sourced, TagForm, TagKey,
};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

use crate::errors::{Fail, to_pyerr};
use crate::input::with_text;
use crate::limits::Lim;

pub const STREAM_START: u8 = 0;
pub const STREAM_END: u8 = 1;
pub const DOC_START: u8 = 2;
pub const DOC_END: u8 = 3;
pub const SEQ_START: u8 = 4;
pub const SEQ_END: u8 = 5;
pub const MAP_START: u8 = 6;
pub const MAP_END: u8 = 7;
pub const SCALAR: u8 = 8;
pub const ALIAS: u8 = 9;

/// Events between looks for a pending signal.
const SIGNAL_EVERY: usize = 4096;
/// Bytes between the checkpoints that make a byte offset into a character offset cheap.
const CHECKPOINT: usize = 4096;

/// Byte offsets into the text of the engine, as character offsets into the text without its
/// byte order mark.
struct Offsets {
    /// Nothing to convert: ASCII text, no byte order mark.
    plain: bool,
    skip_chars: usize,
    /// (byte, characters before it), about one per `CHECKPOINT` bytes.
    marks: Vec<(usize, usize)>,
}

impl Offsets {
    fn new(text: &str) -> Self {
        if text.is_ascii() {
            return Offsets {
                plain: true,
                skip_chars: 0,
                marks: Vec::new(),
            };
        }
        let mut skip_bytes = 0;
        let mut skip_chars = 0;
        while text[skip_bytes..].starts_with('\u{feff}') {
            skip_bytes += '\u{feff}'.len_utf8();
            skip_chars += 1;
        }
        let mut marks = vec![(0, 0)];
        let mut next = CHECKPOINT;
        for (n, (b, _)) in text.char_indices().enumerate() {
            if b >= next {
                marks.push((b, n));
                next = b + CHECKPOINT;
            }
        }
        Offsets {
            plain: false,
            skip_chars,
            marks,
        }
    }

    fn at(&self, text: &str, byte: usize) -> usize {
        if self.plain {
            return byte;
        }
        let i = self
            .marks
            .partition_point(|m| m.0 <= byte)
            .saturating_sub(1);
        let (from, chars) = self.marks[i];
        let n = chars + text.get(from..byte).map_or(0, |s| s.chars().count());
        n.saturating_sub(self.skip_chars)
    }
}

/// Makes the tuples for one text; keeps what repeats (tags) so each is built once.
pub struct Maker<'py, 's> {
    py: Python<'py>,
    text: &'s str,
    offsets: Offsets,
    tags: HashMap<TagKey<'s>, Bound<'py, PyString>>,
    styles: [Bound<'py, PyString>; 5],
    /// Collections open now: a block scalar at the top level may start in column 0.
    depth: usize,
}

impl<'py, 's> Maker<'py, 's> {
    pub fn new(py: Python<'py>, text: &'s str) -> Self {
        Maker {
            py,
            text,
            offsets: Offsets::new(text),
            tags: HashMap::new(),
            styles: [":", "'", "\"", "|", ">"].map(|s| PyString::new(py, s)),
            depth: 0,
        }
    }

    fn at(&self, byte: usize) -> usize {
        self.offsets.at(self.text, byte)
    }

    /// The resolved tag of a node, as one shared string per distinct tag.
    pub fn tag(
        &mut self,
        ev: &Sourced<'s, Event<'s>>,
    ) -> Result<Option<Bound<'py, PyString>>, Fail> {
        let Some(src) = ev.tag_source() else {
            return Ok(None);
        };
        let key = src.key();
        if let Some(t) = self.tags.get(&key) {
            return Ok(Some(t.clone()));
        }
        let Some(tag) = src.resolve() else {
            return Err(Fail::new(
                "unresolved-tag",
                "the tag cannot be resolved",
                ev.span().start,
            ));
        };
        let text = if tag.form == TagForm::NonSpecific {
            "!"
        } else {
            &tag.resolved
        };
        let s = PyString::new(self.py, text);
        self.tags.insert(key, s.clone());
        Ok(Some(s))
    }

    /// Where a node's properties begin (its own start when it has none), its anchor and tag.
    #[allow(clippy::type_complexity)]
    fn props(
        &mut self,
        ev: &Sourced<'s, Event<'s>>,
        start: usize,
    ) -> Result<
        (
            usize,
            Option<Bound<'py, PyString>>,
            Option<Bound<'py, PyString>>,
        ),
        Fail,
    > {
        let Some(p) = ev.properties() else {
            return Ok((self.at(start), None, None));
        };
        if p.is_empty() {
            return Ok((self.at(start), None, None));
        }
        let first = [p.anchor, p.tag]
            .into_iter()
            .flatten()
            .fold(start, usize::min);
        let anchor = match p.anchor {
            Some(_) => ev.anchor().map(|a| PyString::new(self.py, a.name)),
            None => None,
        };
        let tag = if p.tag.is_some() { self.tag(ev)? } else { None };
        Ok((self.at(first), anchor, tag))
    }

    /// Where a scalar begins and ends, as the pure parser reports it. A block scalar begins at
    /// its indicator (the engine's span is the body) and ends before the last line break of its
    /// body, trailing blank lines included. An empty plain scalar sits where the next token
    /// begins, except right after the `:`, `-` or `?` it belongs to.
    fn scalar_span(&self, ev: &Sourced<'s, Event<'s>>, span: Span) -> (usize, usize) {
        if let Some(h) = ev.block_header() {
            let end = block_end(self.text, &h, self.depth);
            return (h.span.start, end);
        }
        if span.is_empty()
            && matches!(
                ev.kind(),
                EventKind::Scalar {
                    style: ScalarStyle::Plain,
                    ..
                }
            )
        {
            // An empty node with properties ends where its last property does.
            let anchor_end = ev.anchor().map(|a| a.span.end);
            let tag_end = ev.tag_source().map(|t| t.span.end);
            if let Some(end) = anchor_end.max(tag_end) {
                return (end, end);
            }
            let at = empty_at(self.text, span.start);
            return (at, at);
        }
        (span.start, span.end)
    }

    /// The tuple for an event; None for the ones that carry nothing a consumer reads (white
    /// space, comments, directives). An error event is the error.
    pub fn tuple(
        &mut self,
        ev: &Sourced<'s, Event<'s>>,
    ) -> Result<Option<Bound<'py, PyAny>>, Fail> {
        let py = self.py;
        let span = ev.span();
        let t = match ev.kind() {
            EventKind::StreamStart => PyTuple::new(py, [STREAM_START])?,
            EventKind::StreamEnd => PyTuple::new(py, [STREAM_END as usize, self.at(span.start)])?,
            EventKind::DocumentStart { explicit, .. } => (
                DOC_START,
                self.at(span.start),
                self.at(span.end),
                *explicit,
                py.None(),
                py.None(),
            )
                .into_pyobject(py)?,
            EventKind::DocumentEnd { explicit, .. } => {
                (DOC_END, self.at(span.start), self.at(span.end), *explicit).into_pyobject(py)?
            }
            EventKind::MappingStart { style, .. } | EventKind::SequenceStart { style, .. } => {
                let kind = if matches!(ev.kind(), EventKind::MappingStart { .. }) {
                    MAP_START
                } else {
                    SEQ_START
                };
                let (props, anchor, tag) = self.props(ev, span.start)?;
                self.depth += 1;
                let flow = matches!(style, CollectionStyle::Flow | CollectionStyle::FlowPair);
                (kind, props, self.at(span.start), anchor, tag, flow).into_pyobject(py)?
            }
            EventKind::MappingEnd => {
                self.depth = self.depth.saturating_sub(1);
                (MAP_END, self.at(span.start)).into_pyobject(py)?
            }
            EventKind::SequenceEnd => {
                self.depth = self.depth.saturating_sub(1);
                (SEQ_END, self.at(span.start)).into_pyobject(py)?
            }
            EventKind::Scalar { style, .. } => {
                let Some(value) = ev.value() else {
                    return Err(Fail::new("other", "scalar text unavailable", span.start));
                };
                let (start, end) = self.scalar_span(ev, span);
                let (props, anchor, tag) = self.props(ev, start)?;
                let style = match style {
                    ScalarStyle::Plain => &self.styles[0],
                    ScalarStyle::SingleQuoted => &self.styles[1],
                    ScalarStyle::DoubleQuoted => &self.styles[2],
                    ScalarStyle::Literal => &self.styles[3],
                    ScalarStyle::Folded => &self.styles[4],
                    _ => return Err(Fail::new("other", "unknown scalar style", span.start)),
                };
                (
                    SCALAR,
                    props,
                    self.at(start),
                    self.at(end),
                    anchor,
                    tag,
                    style,
                    PyString::new(py, &value),
                )
                    .into_pyobject(py)?
            }
            EventKind::Alias => {
                let Some(name) = ev.alias_name() else {
                    return Err(Fail::new("other", "alias name unavailable", span.start));
                };
                (ALIAS, self.at(span.start), self.at(span.end), name).into_pyobject(py)?
            }
            EventKind::Error(_) => return Err(Fail::from_event(ev)),
            _ => return Ok(None),
        };
        Ok(Some(t.into_any()))
    }
}

/// All events of `data` as tuples, or the error the engine found.
pub fn events<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyAny>,
    limits: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyList>> {
    let lim = Lim::from_py(limits)?;
    with_text(py, data, lim.input, |text| {
        let settings = lim.settings();
        let mut maker = Maker::new(py, text);
        let out = PyList::empty(py);
        for (n, ev) in oneyaml_syntax::events_with(text, &settings).enumerate() {
            if n % SIGNAL_EVERY == SIGNAL_EVERY - 1 {
                py.check_signals()?;
            }
            match maker.tuple(&ev) {
                Ok(Some(t)) => out.append(t)?,
                Ok(None) => {}
                Err(f) => return Err(to_pyerr(py, text, f, None)),
            }
        }
        Ok(out)
    })
}

/// The end of the line a block scalar header is on: after the header, any blanks and a comment.
fn header_end(text: &str, from: usize) -> usize {
    let rest = &text[from.min(text.len())..];
    let line = rest.find(['\r', '\n']).unwrap_or(rest.len());
    from + line
}

/// Where a non-empty block scalar ends: before the last line break of its body, the body being
/// every following line that is indented to the content or holds only spaces.
fn block_end(text: &str, h: &oneyaml_syntax::event::BlockHeader, depth: usize) -> usize {
    let k = header_end(text, h.span.end);
    if k >= text.len() {
        return k;
    }
    let bytes = text.as_bytes();
    let mut pos = if bytes[k] == b'\r' && bytes.get(k + 1) == Some(&b'\n') {
        k + 2
    } else {
        k + 1
    };
    let body = pos;
    // Below the top level a body is always indented at least one space.
    let indent = h.content_indent.max(usize::from(depth > 0));
    let mut end = pos;
    let mut unterminated = false;
    while pos < bytes.len() {
        let line_end = text[pos..]
            .find(['\r', '\n'])
            .map_or(bytes.len(), |i| pos + i);
        let line = &bytes[pos..line_end];
        let spaces = line.iter().take_while(|&&c| c == b' ').count();
        let indented = spaces >= indent && !(indent == 0 && is_marker(&bytes[pos..]));
        if line_end == bytes.len() {
            // The last line has no break; it belongs to the body when it has content or is blank.
            if (indented && line.len() > indent) || spaces == line.len() {
                end = line_end;
                unterminated = true;
            }
            break;
        }
        if !(indented || spaces == line.len()) {
            break;
        }
        pos = if bytes[line_end] == b'\r' && bytes.get(line_end + 1) == Some(&b'\n') {
            line_end + 2
        } else {
            line_end + 1
        };
        end = pos;
    }
    if end == body {
        return k;
    }
    if unterminated {
        return end;
    }
    end - if text[..end].ends_with("\r\n") { 2 } else { 1 }
}

/// Whether a document marker (`---` or `...`) starts `rest`.
fn is_marker(rest: &[u8]) -> bool {
    (rest.starts_with(b"---") || rest.starts_with(b"..."))
        && rest
            .get(3)
            .is_none_or(|c| matches!(c, b' ' | b'\t' | b'\r' | b'\n'))
}

/// The position of an empty plain scalar that the engine puts at `at`. Right after a `:`, `?`
/// or `-` indicator that is where it stays; anywhere else it is the start of the next token,
/// which is where a reader that works token by token puts a missing node.
fn empty_at(text: &str, at: usize) -> usize {
    let before = &text.as_bytes()[..at.min(text.len())];
    let after_indicator = match before.last() {
        Some(b':' | b'?') => true,
        Some(b'-') => !before.ends_with(b"---"),
        _ => false,
    };
    if after_indicator {
        return at;
    }
    let rest = &text[at..];
    let mut i = 0;
    let b = rest.as_bytes();
    while i < b.len() {
        match b[i] {
            b' ' | b'\t' | b'\r' | b'\n' => i += 1,
            b'#' => {
                while i < b.len() && b[i] != b'\r' && b[i] != b'\n' {
                    i += 1;
                }
            }
            _ => break,
        }
    }
    at + i
}
