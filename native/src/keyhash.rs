//! A fast, seeded hasher for the per-load cache of mapping keys.
//!
//! The standard hasher is built to resist crafted input and pays for it on every short key; a
//! load looks up one key per mapping entry, so that cost showed up as a tenth of a document of
//! many short keys. This one multiplies and folds eight bytes at a time. It is seeded afresh for
//! every load, so input cannot be crafted to collide ahead of time, and the cache that uses it
//! is bounded anyway (see `reader::KEY_CACHE_MAX_ENTRIES`).

use std::hash::{BuildHasher, Hasher, RandomState};

const MUL_A: u64 = 0x9E37_79B9_7F4A_7C15;
const MUL_B: u64 = 0xD6E8_FEB8_6659_FD93;

#[inline(always)]
fn fold(a: u64, b: u64) -> u64 {
    let r = u128::from(a) * u128::from(b);
    (r as u64) ^ ((r >> 64) as u64)
}

#[derive(Clone, Copy)]
pub struct SeededState {
    seed: u64,
}

impl SeededState {
    pub fn new() -> Self {
        // The standard library's random keys, drawn once per load.
        SeededState {
            seed: RandomState::new().hash_one(0x6f6e_6579_616d_6c21_u64),
        }
    }
}

impl BuildHasher for SeededState {
    type Hasher = KeyHasher;

    #[inline]
    fn build_hasher(&self) -> KeyHasher {
        KeyHasher { h: self.seed }
    }
}

pub struct KeyHasher {
    h: u64,
}

#[inline(always)]
fn le64(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

#[inline(always)]
fn le32(b: &[u8]) -> u64 {
    u64::from(u32::from_le_bytes([b[0], b[1], b[2], b[3]]))
}

impl Hasher for KeyHasher {
    #[inline]
    fn write(&mut self, bytes: &[u8]) {
        let mut h = self.h ^ (bytes.len() as u64).wrapping_mul(MUL_B);
        let mut rest = bytes;
        while rest.len() >= 8 {
            h = fold(h ^ le64(rest), MUL_A);
            rest = &rest[8..];
        }
        // The last 1..7 bytes, read as overlapping pieces so that no loop is needed.
        let n = rest.len();
        let tail = if n >= 4 {
            le32(rest) | (le32(&rest[n - 4..]) << 32)
        } else if n > 0 {
            u64::from(rest[0]) | (u64::from(rest[n / 2]) << 8) | (u64::from(rest[n - 1]) << 16)
        } else {
            0
        };
        self.h = fold(h ^ tail, MUL_B);
    }

    #[inline]
    fn write_u8(&mut self, i: u8) {
        // `str` ends its bytes with 0xff; a lengthy multiply is not needed for that.
        self.h = (self.h ^ u64::from(i)).rotate_left(23);
    }

    #[inline]
    fn finish(&self) -> u64 {
        self.h
    }
}
