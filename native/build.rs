// Records the engine version from Cargo.lock so the module can report it.
use std::{env, fs, path::PathBuf};

fn main() {
    let dir = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap_or_default());
    let lock = dir.join("Cargo.lock");
    println!("cargo:rerun-if-changed={}", lock.display());
    let text = fs::read_to_string(&lock).unwrap_or_default();
    let mut version = String::from("unknown");
    let mut lines = text.lines();
    while let Some(line) = lines.next() {
        if line == "name = \"oneyaml\"" {
            if let Some(v) = lines.next().and_then(|l| l.strip_prefix("version = \"")) {
                version = v.trim_end_matches('"').to_string();
            }
            break;
        }
    }
    println!("cargo:rustc-env=ONEYAML_ENGINE_VERSION={version}");
}
