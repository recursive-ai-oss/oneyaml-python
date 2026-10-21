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
