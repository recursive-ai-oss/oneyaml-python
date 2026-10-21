//! Switches the tests use to reach paths a healthy machine never takes: a stage that panics, a
//! thread the OS refuses, a signal that arrives mid-load. All are off unless a test sets one.

use std::sync::atomic::{AtomicU8, Ordering};

pub const OFF: u8 = 0;
pub const PANIC_IN_PARSER: u8 = 1;
pub const PANIC_IN_TRANSLATOR: u8 = 2;
pub const REFUSE_THREADS: u8 = 3;
pub const INTERRUPT: u8 = 4;

static HOOK: AtomicU8 = AtomicU8::new(OFF);

pub fn set(what: u8) {
    HOOK.store(what, Ordering::SeqCst);
}

pub fn is(what: u8) -> bool {
    HOOK.load(Ordering::Relaxed) == what
}

/// True once, for the call that finds `what` set; the switch is then off.
pub fn take(what: u8) -> bool {
    HOOK.compare_exchange(what, OFF, Ordering::SeqCst, Ordering::SeqCst)
        .is_ok()
}
