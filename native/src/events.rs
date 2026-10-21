//! `events_text`: the event stream in the yaml-test-suite tree notation.

use oneyaml_syntax::event::{CollectionStyle, Event, EventKind, ScalarStyle, Sourced};
use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::PyList;

use crate::errors::{Fail, to_pyerr};
use crate::input::with_text;
use crate::limits::Lim;

fn escape(out: &mut String, value: &str) {
    for c in value.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\0' => out.push_str("\\0"),
            '\u{8}' => out.push_str("\\b"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            other => out.push(other),
        }
    }
}

fn props(out: &mut String, ev: &Sourced<'_, Event<'_>>) -> Option<()> {
    let p = ev.properties()?;
    if p.anchor.is_some() {
        out.push_str(" &");
        out.push_str(ev.anchor()?.name);
    }
    if p.tag.is_some() {
        out.push_str(" <");
        out.push_str(&ev.tag()?.resolved);
        out.push('>');
    }
    Some(())
}

/// The notation line of an event, or None for events that have none (trivia, directives).
fn line(ev: &Sourced<'_, Event<'_>>) -> Option<String> {
    let mut l = String::new();
    match ev.kind() {
        EventKind::StreamStart => l.push_str("+STR"),
        EventKind::StreamEnd => l.push_str("-STR"),
        EventKind::DocumentStart { explicit, .. } => {
            l.push_str(if *explicit { "+DOC ---" } else { "+DOC" });
        }
        EventKind::DocumentEnd { explicit, .. } => {
            l.push_str(if *explicit { "-DOC ..." } else { "-DOC" });
        }
        EventKind::MappingEnd => l.push_str("-MAP"),
        EventKind::SequenceEnd => l.push_str("-SEQ"),
        EventKind::MappingStart { style, .. } | EventKind::SequenceStart { style, .. } => {
            let is_map = matches!(ev.kind(), EventKind::MappingStart { .. });
            l.push_str(if is_map { "+MAP" } else { "+SEQ" });
            if matches!(style, CollectionStyle::Flow | CollectionStyle::FlowPair) {
                l.push_str(if is_map { " {}" } else { " []" });
            }
            props(&mut l, ev)?;
        }
        EventKind::Scalar { style, .. } => {
            l.push_str("=VAL");
            props(&mut l, ev)?;
            l.push(' ');
            l.push(match style {
                ScalarStyle::Plain => ':',
                ScalarStyle::SingleQuoted => '\'',
                ScalarStyle::DoubleQuoted => '"',
                ScalarStyle::Literal => '|',
                ScalarStyle::Folded => '>',
                _ => return None,
            });
            escape(&mut l, &ev.value()?);
        }
        EventKind::Alias => {
            l.push_str("=ALI *");
            l.push_str(ev.alias_name()?);
        }
        _ => return None,
    }
    Some(l)
}

pub fn events_text<'py>(py: Python<'py>, data: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let lim = Lim::DEFAULT;
    let lines = with_text(py, data, lim.input, |text| {
        let settings = lim.settings();
        let mut lines: Vec<String> = Vec::new();
        // The engine's events do not check that an alias has an anchor; an alias to a name not
        // met earlier in its document is an error in the text, as in the other backend.
        let mut anchors: HashSet<&str> = HashSet::new();
        for ev in oneyaml_syntax::events_with(text, &settings) {
            if matches!(ev.kind(), EventKind::Error(_)) {
                return Err(to_pyerr(py, text, Fail::from_event(&ev), Some(lines)));
            }
            match ev.kind() {
                EventKind::DocumentStart { .. } => anchors.clear(),
                EventKind::Alias => {
                    if let Some(name) = ev.alias_name()
                        && !anchors.contains(name)
                    {
                        let fail = Fail::new(
                            "syntax",
                            format!("alias *{name} has no anchor"),
                            ev.span().start,
                        );
                        return Err(to_pyerr(py, text, fail, Some(lines)));
                    }
                }
                EventKind::Scalar { .. }
                | EventKind::MappingStart { .. }
                | EventKind::SequenceStart { .. } => {
                    if let Some(a) = ev.anchor() {
                        anchors.insert(a.name);
                    }
                }
                _ => {}
            }
            if let Some(l) = line(&ev) {
                lines.push(l);
            }
        }
        Ok(lines)
    })?;
    PyList::new(py, lines)
}

/// Events the engine produces for `data`, reading each one's text as a load would but keeping
/// nothing. Errors are not raised: this measures the parser.
pub fn count(py: Python<'_>, data: &Bound<'_, PyAny>) -> PyResult<usize> {
    let lim = Lim::DEFAULT;
    with_text(py, data, lim.input, |text| {
        let settings = lim.settings();
        let mut n = 0usize;
        for ev in oneyaml_syntax::events_with(text, &settings) {
            if matches!(ev.kind(), EventKind::Scalar { .. }) {
                n += ev.value().map_or(0, |v| v.len().min(1));
            }
            n += 1;
        }
        Ok(n)
    })
}
