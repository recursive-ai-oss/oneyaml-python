//! The engine side of a load: turns the engine's events into `Op`s, which hold everything the
//! Python side needs and nothing from Python.
//!
//! Scalars are resolved here (Core schema, tags, the digit cap), so that the thread building
//! Python objects only has to create them. For large inputs this runs on a second thread while
//! the first builds objects from the ops already produced.

use std::borrow::Cow;

use oneyaml::Settings;
use oneyaml_syntax::event::{Event, EventKind, ScalarStyle, Sourced, TagForm};

use crate::scalar::{self, Plain};

/// Ops handed over at a time.
const BATCH: usize = 2048;

pub struct Failure {
    pub category: &'static str,
    pub message: String,
    pub at: usize,
}

pub fn fail<'s>(category: &'static str, message: impl Into<String>, at: usize) -> Op<'s> {
    Op::Fail(Box::new(Failure {
        category,
        message: message.into(),
        at,
    }))
}

/// A scalar with an anchor or a tag the schema does not resolve.
pub struct Prop<'s> {
    /// The value without the tag: one of `Null`..`Bytes`.
    pub inner: Op<'s>,
    pub anchor: Option<&'s str>,
    /// Kept as `Tagged(tag, value)`.
    pub custom: Option<Cow<'s, str>>,
}

/// A collection with an anchor or an unresolved tag.
pub struct NodeProps<'s> {
    pub anchor: Option<&'s str>,
    pub custom: Option<Cow<'s, str>>,
}

pub enum Op<'s> {
    DocStart(usize),
    DocEnd,
    Null(usize),
    Bool(bool, usize),
    Int(i64, usize),
    Float(f64, usize),
    Str(Cow<'s, str>, usize),
    /// An integer too long for `i64`: decimal text with its sign, or radix digits (base 16/8).
    Num(Cow<'s, str>, u32, usize),
    Bytes(Vec<u8>, usize),
    Prop(Box<Prop<'s>>, usize),
    Open {
        map: bool,
        at: usize,
        props: Option<Box<NodeProps<'s>>>,
    },
    Close,
    Alias(&'s str, usize),
    Fail(Box<Failure>),
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum CoreTag {
    Null,
    Bool,
    Int,
    Float,
    Str,
    Seq,
    Map,
    Binary,
}

pub fn core_tag(resolved: &str) -> Option<CoreTag> {
    Some(match resolved.strip_prefix("tag:yaml.org,2002:")? {
        "null" => CoreTag::Null,
        "bool" => CoreTag::Bool,
        "int" => CoreTag::Int,
        "float" => CoreTag::Float,
        "str" => CoreTag::Str,
        "seq" => CoreTag::Seq,
        "map" => CoreTag::Map,
        "binary" => CoreTag::Binary,
        _ => return None,
    })
}

pub enum Tag<'s> {
    None,
    /// `!` alone.
    NonSpecific,
    Core(CoreTag),
    Custom(Cow<'s, str>),
}

fn digit_count(text: &str) -> usize {
    text.trim_start_matches(['+', '-']).len()
}

fn digit_fail<'s>(digits: usize, cap: usize, at: usize) -> Op<'s> {
    fail(
        "limit",
        format!("integer of {digits} digits exceeds the limit of {cap}"),
        at,
    )
}

/// Drop the `0x` / `0o` prefix, keeping a borrow when the text is one.
fn after_prefix(text: Cow<'_, str>) -> Cow<'_, str> {
    match text {
        Cow::Borrowed(s) => Cow::Borrowed(s.get(2..).unwrap_or("")),
        Cow::Owned(s) => Cow::Owned(s.get(2..).unwrap_or("").to_string()),
    }
}

fn plain<'s>(text: Cow<'s, str>, cap: usize, at: usize) -> Op<'s> {
    match scalar::classify(&text) {
        Plain::Str => Op::Str(text, at),
        Plain::Null => Op::Null(at),
        Plain::Bool(b) => Op::Bool(b, at),
        Plain::Float(f) => Op::Float(f, at),
        Plain::Int(i) => {
            if cap < 18 && digit_count(&text) > cap {
                return digit_fail(digit_count(&text), cap, at);
            }
            Op::Int(i, at)
        }
        Plain::BigDec(_) => {
            let digits = digit_count(&text);
            if digits > cap {
                return digit_fail(digits, cap, at);
            }
            Op::Num(text, 10, at)
        }
        Plain::Radix(base, _) => Op::Num(after_prefix(text), base, at),
    }
}

/// A scalar under a `!!` tag of the Core schema: the text must fit the type.
fn core_scalar<'s>(tag: CoreTag, text: Cow<'s, str>, cap: usize, at: usize) -> Op<'s> {
    let bad = |what: &str| {
        let mut shown: String = text.chars().take(40).collect();
        if shown.len() < text.len() {
            shown.push('…');
        }
        fail("syntax", format!("{shown:?} is not a valid {what}"), at)
    };
    match tag {
        CoreTag::Str => Op::Str(text, at),
        CoreTag::Null => match scalar::classify(&text) {
            Plain::Null => Op::Null(at),
            _ => bad("!!null"),
        },
        CoreTag::Bool => match scalar::classify(&text) {
            Plain::Bool(b) => Op::Bool(b, at),
            _ => bad("!!bool"),
        },
        CoreTag::Int => match scalar::classify(&text) {
            Plain::Int(_) | Plain::BigDec(_) | Plain::Radix(..) => plain(text, cap, at),
            _ => bad("!!int"),
        },
        CoreTag::Float => match scalar::float(&text) {
            Some(f) => Op::Float(f, at),
            None => bad("!!float"),
        },
        CoreTag::Binary => match scalar::base64(&text) {
            Some(b) => Op::Bytes(b, at),
            None => bad("!!binary"),
        },
        CoreTag::Seq | CoreTag::Map => fail("syntax", "a collection tag on a scalar", at),
    }
}

/// The anchor and tag written on a node.
fn props<'s>(ev: &Sourced<'s, Event<'s>>) -> Result<(Option<&'s str>, Tag<'s>), Box<Failure>> {
    let Some(p) = ev.properties() else {
        return Ok((None, Tag::None));
    };
    if p.is_empty() {
        return Ok((None, Tag::None));
    }
    let anchor = if p.anchor.is_some() {
        ev.anchor().map(|a| a.name)
    } else {
        None
    };
    let tag = if p.tag.is_some() {
        match ev.tag() {
            None => {
                return Err(Box::new(Failure {
                    category: "unresolved-tag",
                    message: "the tag cannot be resolved".into(),
                    at: ev.span().start,
                }));
            }
            Some(t) if t.form == TagForm::NonSpecific => Tag::NonSpecific,
            Some(t) => match core_tag(&t.resolved) {
                Some(c) => Tag::Core(c),
                None => Tag::Custom(t.resolved),
            },
        }
    } else {
        Tag::None
    };
    Ok((anchor, tag))
}

fn scalar_op<'s>(ev: &Sourced<'s, Event<'s>>, style: ScalarStyle, cap: usize) -> Op<'s> {
    let at = ev.span().start;
    let Some(text) = ev.value() else {
        return fail("other", "scalar text unavailable", at);
    };
    let (anchor, tag) = match props(ev) {
        Ok(p) => p,
        Err(f) => return Op::Fail(f),
    };
    scalar_parts(text, style == ScalarStyle::Plain, anchor, tag, at, cap)
}

/// The op for a scalar whose text, style and properties are known, from an event or from a
/// node of a lossless document.
pub fn scalar_parts<'s>(
    text: Cow<'s, str>,
    is_plain: bool,
    anchor: Option<&'s str>,
    tag: Tag<'s>,
    at: usize,
    cap: usize,
) -> Op<'s> {
    let wrap = |inner: Op<'s>, custom: Option<Cow<'s, str>>| {
        if anchor.is_none() && custom.is_none() {
            inner
        } else {
            Op::Prop(
                Box::new(Prop {
                    inner,
                    anchor,
                    custom,
                }),
                at,
            )
        }
    };
    match tag {
        Tag::None if is_plain => wrap(plain(text, cap, at), None),
        Tag::None | Tag::NonSpecific => wrap(Op::Str(text, at), None),
        Tag::Core(c) => {
            let op = core_scalar(c, text, cap, at);
            if matches!(op, Op::Fail(_)) {
                op
            } else {
                wrap(op, None)
            }
        }
        Tag::Custom(t) => wrap(Op::Str(text, at), Some(t)),
    }
}

fn open_op<'s>(ev: &Sourced<'s, Event<'s>>, map: bool) -> Op<'s> {
    let at = ev.span().start;
    let (anchor, tag) = match props(ev) {
        Ok(p) => p,
        Err(f) => return Op::Fail(f),
    };
    open_parts(map, anchor, tag, at)
}

/// The op that opens a collection with the given properties.
pub fn open_parts<'s>(map: bool, anchor: Option<&'s str>, tag: Tag<'s>, at: usize) -> Op<'s> {
    let custom = match tag {
        Tag::None | Tag::NonSpecific => None,
        Tag::Core(CoreTag::Seq) if !map => None,
        Tag::Core(CoreTag::Map) if map => None,
        Tag::Core(_) => {
            return fail(
                "syntax",
                "the tag does not fit a collection of this kind",
                at,
            );
        }
        Tag::Custom(t) => Some(t),
    };
    let props = if anchor.is_none() && custom.is_none() {
        None
    } else {
        Some(Box::new(NodeProps { anchor, custom }))
    };
    Op::Open { map, at, props }
}

/// What an event means for a load; None for events that carry nothing (white space, comments,
/// directives, the stream's ends).
pub fn translate<'s>(ev: &Sourced<'s, Event<'s>>, cap: usize) -> Option<Op<'s>> {
    Some(match ev.kind() {
        EventKind::Scalar { style, .. } => scalar_op(ev, *style, cap),
        EventKind::SequenceStart { .. } => open_op(ev, false),
        EventKind::MappingStart { .. } => open_op(ev, true),
        EventKind::SequenceEnd | EventKind::MappingEnd => Op::Close,
        EventKind::Alias => match ev.alias_name() {
            Some(name) => Op::Alias(name, ev.span().start),
            None => fail("other", "alias name unavailable", ev.span().start),
        },
        EventKind::DocumentStart { .. } => Op::DocStart(ev.span().start),
        EventKind::DocumentEnd { .. } => Op::DocEnd,
        EventKind::Error(_) => match ev.diagnostic() {
            Some(d) => Op::Fail(Box::new(Failure {
                category: crate::errors::category_of(d),
                message: d.message.clone(),
                at: d.primary.span.start,
            })),
            None => fail("other", "invalid input", ev.span().start),
        },
        _ => return None,
    })
}

/// Run the parser over `text`, handing its events over in batches. White space and comments
/// are left out; everything a load reads is kept. `emit` returns false to say that nothing more
/// is wanted. The stream ends after an error event.
pub fn pull<'s>(
    text: &'s str,
    settings: &Settings,
    mut emit: impl FnMut(Vec<Sourced<'s, Event<'s>>>) -> bool,
) {
    let mut batch: Vec<Sourced<'s, Event<'s>>> = Vec::with_capacity(BATCH);
    for ev in oneyaml_syntax::events_with(text, settings) {
        match ev.kind() {
            EventKind::Trivia(_)
            | EventKind::Warning(_)
            | EventKind::Directive(_)
            | EventKind::StreamStart
            | EventKind::StreamEnd => continue,
            _ => {}
        }
        let stop = ev.is_error();
        batch.push(ev);
        if stop {
            break;
        }
        if batch.len() >= BATCH && !emit(std::mem::replace(&mut batch, Vec::with_capacity(BATCH))) {
            return;
        }
    }
    if !batch.is_empty() {
        emit(batch);
    }
}

/// The ops for a batch of events. The batch ends at the first failure.
pub fn translate_batch<'s>(events: Vec<Sourced<'s, Event<'s>>>, digit_cap: usize) -> Vec<Op<'s>> {
    let mut ops = Vec::with_capacity(events.len());
    for ev in &events {
        let Some(op) = translate(ev, digit_cap) else {
            continue;
        };
        let stop = matches!(op, Op::Fail(_));
        ops.push(op);
        if stop {
            break;
        }
    }
    ops
}
