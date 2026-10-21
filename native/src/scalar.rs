//! Core-schema resolution of scalars (YAML 1.2.2 section 10.3.2), written for speed: the first
//! byte settles most plain scalars without reading the rest.

/// What a plain scalar resolves to. Borrowed text is the scalar itself.
#[derive(Debug, PartialEq)]
pub enum Plain<'a> {
    Null,
    Bool(bool),
    Int(i64),
    /// A decimal integer too long for `i64`; the text keeps its sign.
    BigDec(&'a str),
    /// `0x` (16) or `0o` (8) digits, without the prefix.
    Radix(u32, &'a str),
    Float(f64),
    Str,
}

pub fn classify(s: &str) -> Plain<'_> {
    let b = s.as_bytes();
    let Some(&c) = b.first() else {
        return Plain::Null;
    };
    match c {
        b'~' => {
            if b.len() == 1 {
                Plain::Null
            } else {
                Plain::Str
            }
        }
        b'n' | b'N' => {
            if matches!(s, "null" | "Null" | "NULL") {
                Plain::Null
            } else {
                Plain::Str
            }
        }
        b't' | b'T' | b'f' | b'F' => match s {
            "true" | "True" | "TRUE" => Plain::Bool(true),
            "false" | "False" | "FALSE" => Plain::Bool(false),
            _ => Plain::Str,
        },
        b'0'..=b'9' | b'-' | b'+' | b'.' => number(s),
        _ => Plain::Str,
    }
}

fn number(s: &str) -> Plain<'_> {
    if let Some(d) = s.strip_prefix("0x") {
        return if !d.is_empty() && d.bytes().all(|c| c.is_ascii_hexdigit()) {
            Plain::Radix(16, d)
        } else {
            Plain::Str
        };
    }
    if let Some(d) = s.strip_prefix("0o") {
        return if !d.is_empty() && d.bytes().all(|c| matches!(c, b'0'..=b'7')) {
            Plain::Radix(8, d)
        } else {
            Plain::Str
        };
    }
    match int(s) {
        Some(p) => p,
        None => match float(s) {
            Some(f) => Plain::Float(f),
            None => Plain::Str,
        },
    }
}

/// `[-+]?[0-9]+`
pub fn int(s: &str) -> Option<Plain<'_>> {
    let b = s.as_bytes();
    let (neg, digits) = match b.first() {
        Some(b'-') => (true, &b[1..]),
        Some(b'+') => (false, &b[1..]),
        _ => (false, b),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    // Up to 18 digits always fit an i64.
    if digits.len() <= 18 {
        let mut v: i64 = 0;
        for &d in digits {
            v = v * 10 + i64::from(d - b'0');
        }
        return Some(Plain::Int(if neg { -v } else { v }));
    }
    Some(Plain::BigDec(s))
}

/// `[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?`, `[-+]?\.(inf|Inf|INF)`,
/// `\.(nan|NaN|NAN)`.
pub fn float(s: &str) -> Option<f64> {
    if matches!(s, ".nan" | ".NaN" | ".NAN") {
        return Some(f64::NAN);
    }
    let (neg, rest) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    if matches!(rest, ".inf" | ".Inf" | ".INF") {
        return Some(if neg {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    if !float_syntax(rest.as_bytes()) {
        return None;
    }
    s.parse::<f64>().ok()
}

fn float_syntax(b: &[u8]) -> bool {
    let mut i = 0;
    let digits = |i: &mut usize| {
        let start = *i;
        while *i < b.len() && b[*i].is_ascii_digit() {
            *i += 1;
        }
        *i - start
    };
    if b.first() == Some(&b'.') {
        i = 1;
        if digits(&mut i) == 0 {
            return false;
        }
    } else {
        if digits(&mut i) == 0 {
            return false;
        }
        if b.get(i) == Some(&b'.') {
            i += 1;
            digits(&mut i);
        }
    }
    if matches!(b.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(b.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        if digits(&mut i) == 0 {
            return false;
        }
    }
    i == b.len()
}

/// Decode `!!binary` text (base64, white space ignored, padding required).
pub fn base64(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let mut acc: u32 = 0;
    let mut bits = 0;
    let mut pad = 0;
    for c in text.bytes() {
        let v = match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            b'=' => {
                pad += 1;
                continue;
            }
            b' ' | b'\t' | b'\n' | b'\r' => continue,
            _ => return None,
        };
        if pad > 0 {
            return None;
        }
        acc = (acc << 6) | u32::from(v);
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    // Whole groups only: the data length plus padding is a multiple of four, and what is left
    // over holds no stray bits.
    let data_chars = text
        .bytes()
        .filter(|c| !matches!(c, b'=' | b' ' | b'\t' | b'\n' | b'\r'))
        .count();
    if (data_chars + pad) % 4 != 0 || pad > 2 || acc != 0 {
        return None;
    }
    Some(out)
}

/// What a plain scalar resolves to under the YAML 1.1 rules of PyYAML's resolver (the compat
/// mode): `yes`/`no`, `0b`/`0x`/leading-zero integers, `_` separators, base-60 numbers, dates
/// and the `<<` merge key.
#[derive(Debug, PartialEq)]
pub enum Plain11 {
    Null,
    Bool(bool),
    Int(i64),
    /// An integer that does not fit `i64`: sign and digits (underscores removed) in the given
    /// base; base 60 keeps its colons.
    Big(String, u32),
    Float(f64),
    Merge,
    Timestamp,
    Str,
}

pub fn classify11(s: &str) -> Plain11 {
    let b = s.as_bytes();
    let Some(&c) = b.first() else {
        return Plain11::Null;
    };
    match c {
        b'~' => {
            if b.len() == 1 {
                Plain11::Null
            } else {
                Plain11::Str
            }
        }
        b'<' => {
            if s == "<<" {
                Plain11::Merge
            } else {
                Plain11::Str
            }
        }
        b'n' | b'N' | b'y' | b'Y' | b't' | b'T' | b'f' | b'F' | b'o' | b'O' => match s {
            "null" | "Null" | "NULL" => Plain11::Null,
            "yes" | "Yes" | "YES" | "true" | "True" | "TRUE" | "on" | "On" | "ON" => {
                Plain11::Bool(true)
            }
            "no" | "No" | "NO" | "false" | "False" | "FALSE" | "off" | "Off" | "OFF" => {
                Plain11::Bool(false)
            }
            _ => Plain11::Str,
        },
        b'0'..=b'9' | b'-' | b'+' | b'.' => number11(s),
        _ => Plain11::Str,
    }
}

fn signed(neg: bool, v: i64) -> i64 {
    if neg { -v } else { v }
}

fn sign_text(neg: bool) -> &'static str {
    if neg { "-" } else { "" }
}

/// `digits` (no sign) in `base` as an integer, or as text when it does not fit.
fn radix11(neg: bool, digits: &str, base: u32) -> Plain11 {
    match i64::from_str_radix(digits, base) {
        Ok(v) => Plain11::Int(signed(neg, v)),
        Err(_) => Plain11::Big(format!("{}{digits}", sign_text(neg)), base),
    }
}

fn strip_us(s: &str) -> String {
    s.chars().filter(|&c| c != '_').collect()
}

/// Length of the run of bytes at the start of `b` that satisfy `class`.
fn run(b: &[u8], class: fn(u8) -> bool) -> usize {
    b.iter().take_while(|&&c| class(c)).count()
}

fn dig_us(c: u8) -> bool {
    c.is_ascii_digit() || c == b'_'
}

fn dig(c: u8) -> bool {
    c.is_ascii_digit()
}

/// The length of the exponent `[eE][-+][0-9]+` at the start of `b`, if there is one.
fn exponent(b: &[u8]) -> Option<usize> {
    if matches!(b.first(), Some(b'e' | b'E')) && matches!(b.get(1), Some(b'+' | b'-')) {
        let n = run(&b[2..], dig);
        if n > 0 {
            return Some(2 + n);
        }
    }
    None
}

fn number11(s: &str) -> Plain11 {
    let (neg, rest) = match s.as_bytes()[0] {
        b'-' => (true, &s[1..]),
        b'+' => (false, &s[1..]),
        _ => (false, s),
    };
    let signed_text = rest.len() != s.len();
    let b = rest.as_bytes();
    let Some(&first) = b.first() else {
        return Plain11::Str;
    };
    if first == b'.' {
        if matches!(rest, ".inf" | ".Inf" | ".INF") {
            return Plain11::Float(if neg {
                f64::NEG_INFINITY
            } else {
                f64::INFINITY
            });
        }
        if !signed_text && matches!(rest, ".nan" | ".NaN" | ".NAN") {
            return Plain11::Float(f64::NAN);
        }
        // `\.[0-9][0-9_]*` with an optional exponent; this form takes no sign.
        if signed_text || !b.get(1).is_some_and(u8::is_ascii_digit) {
            return Plain11::Str;
        }
        let i = 1 + run(&b[1..], dig_us);
        let ok = i == b.len() || exponent(&b[i..]).is_some_and(|n| i + n == b.len());
        return if ok {
            float11(false, rest)
        } else {
            Plain11::Str
        };
    }
    if !first.is_ascii_digit() {
        return Plain11::Str;
    }
    if !signed_text && is_timestamp(b) {
        return Plain11::Timestamp;
    }
    // 0b..., 0x..., and the leading-zero octal.
    if first == b'0' && b.len() > 1 {
        let (digits, base, ok) = match b[1] {
            b'b' => (
                &rest[2..],
                2,
                rest[2..].bytes().all(|c| matches!(c, b'0' | b'1' | b'_')),
            ),
            b'x' => (
                &rest[2..],
                16,
                rest[2..]
                    .bytes()
                    .all(|c| c.is_ascii_hexdigit() || c == b'_'),
            ),
            _ => (
                rest,
                8,
                rest.bytes().all(|c| matches!(c, b'0'..=b'7' | b'_')),
            ),
        };
        if ok {
            let d = strip_us(digits);
            // `0b_` has no digits at all; PyYAML fails on it, here it stays text.
            if d.is_empty() {
                return Plain11::Str;
            }
            return radix11(neg, &d, base);
        }
        if base != 8 {
            return Plain11::Str;
        }
    }
    let mut i = run(b, dig_us);
    if i == b.len() {
        // A decimal integer: `0`, or a non-zero digit first.
        if first == b'0' && i > 1 {
            return Plain11::Str;
        }
        let d = strip_us(rest);
        return if d.len() <= 18 {
            Plain11::Int(signed(neg, d.parse().unwrap_or(0)))
        } else {
            Plain11::Big(format!("{}{d}", sign_text(neg)), 10)
        };
    }
    match b[i] {
        b'.' => {
            i += 1 + run(&b[i + 1..], dig_us);
            let ok = i == b.len() || exponent(&b[i..]).is_some_and(|n| i + n == b.len());
            if ok { float11(neg, rest) } else { Plain11::Str }
        }
        b':' => sexagesimal(neg, rest, first),
        _ => Plain11::Str,
    }
}

fn float11(neg: bool, text: &str) -> Plain11 {
    match strip_us(text).parse::<f64>() {
        Ok(v) => Plain11::Float(if neg { -v } else { v }),
        Err(_) => Plain11::Str,
    }
}

/// `[1-9][0-9_]*(:[0-5]?[0-9])+` (an integer) or `[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*`.
fn sexagesimal(neg: bool, rest: &str, first: u8) -> Plain11 {
    let b = rest.as_bytes();
    let mut i = run(b, dig_us);
    let mut parts: Vec<&str> = vec![&rest[..i]];
    while b.get(i) == Some(&b':') {
        i += 1;
        let start = i;
        match (b.get(i), b.get(i + 1)) {
            (Some(&x), Some(y)) if (b'0'..=b'5').contains(&x) && y.is_ascii_digit() => i += 2,
            (Some(x), _) if x.is_ascii_digit() => i += 1,
            _ => return Plain11::Str,
        }
        parts.push(&rest[start..i]);
    }
    if i == b.len() {
        if first == b'0' {
            return Plain11::Str;
        }
        let mut total: u128 = 0;
        let mut fits = true;
        for p in &parts {
            let v: Option<u128> = strip_us(p).parse().ok();
            match v.and_then(|v| total.checked_mul(60).and_then(|t| t.checked_add(v))) {
                Some(t) => total = t,
                None => {
                    fits = false;
                    break;
                }
            }
        }
        if fits && let Ok(v) = i64::try_from(total) {
            return Plain11::Int(signed(neg, v));
        }
        let text = parts
            .iter()
            .map(|p| strip_us(p))
            .collect::<Vec<_>>()
            .join(":");
        return Plain11::Big(format!("{}{text}", sign_text(neg)), 60);
    }
    if b[i] != b'.' || !b[i + 1..].iter().all(|&c| dig_us(c)) {
        return Plain11::Str;
    }
    // The same order of operations as PyYAML, so the sums round the same way.
    let mut values: Vec<f64> = Vec::new();
    for (n, p) in parts.iter().enumerate() {
        let t = if n + 1 == parts.len() {
            format!("{}{}", strip_us(p), strip_us(&rest[i..]))
        } else {
            strip_us(p)
        };
        match t.parse::<f64>() {
            Ok(v) => values.push(v),
            Err(_) => return Plain11::Str,
        }
    }
    let (mut value, mut base) = (0.0_f64, 1.0_f64);
    for d in values.iter().rev() {
        value += d * base;
        base *= 60.0;
    }
    Plain11::Float(if neg { -value } else { value })
}

/// `dddd-dd-dd`, or `dddd-d?d-d?d` then `T`/`t`/blanks, `d?d:dd:dd`, an optional fraction and an
/// optional zone (`Z` or `+h?h[:mm]`, blanks allowed before it).
fn is_timestamp(b: &[u8]) -> bool {
    let d = |i: usize| b.get(i).is_some_and(u8::is_ascii_digit);
    if !(d(0) && d(1) && d(2) && d(3) && b.get(4) == Some(&b'-')) {
        return false;
    }
    let one_or_two = |i: &mut usize| -> bool {
        if !d(*i) {
            return false;
        }
        *i += 1;
        if d(*i) {
            *i += 1;
        }
        true
    };
    let mut i = 5;
    if !one_or_two(&mut i) || b.get(i) != Some(&b'-') {
        return false;
    }
    i += 1;
    if !one_or_two(&mut i) {
        return false;
    }
    if b.len() == 10 && i == 10 && d(5) && d(6) && b[7] == b'-' {
        return true;
    }
    match b.get(i) {
        Some(b'T' | b't') => i += 1,
        Some(b' ' | b'\t') => i += run(&b[i..], |c| c == b' ' || c == b'\t'),
        _ => return false,
    }
    if !one_or_two(&mut i) || b.get(i) != Some(&b':') {
        return false;
    }
    i += 1;
    if !(d(i) && d(i + 1) && b.get(i + 2) == Some(&b':') && d(i + 3) && d(i + 4)) {
        return false;
    }
    i += 5;
    if b.get(i) == Some(&b'.') {
        i += 1 + run(&b[i + 1..], dig);
    }
    if i == b.len() {
        return true;
    }
    i += run(&b[i..], |c| c == b' ' || c == b'\t');
    match b.get(i) {
        Some(b'Z') => i + 1 == b.len(),
        Some(b'-' | b'+') => {
            i += 1;
            if !one_or_two(&mut i) {
                return false;
            }
            if b.get(i) == Some(&b':') {
                if !(d(i + 1) && d(i + 2)) {
                    return false;
                }
                i += 3;
            }
            i == b.len()
        }
        _ => false,
    }
}
