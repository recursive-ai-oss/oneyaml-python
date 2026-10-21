"""Interleaved A/B timing of two installed builds of oneyaml on named workloads.

A developer tool, not a test: it prints numbers and asserts nothing. Two interpreters (or
virtual environments), each with its own build of the package installed, take turns running
the same task on the same input, so a slow moment of the machine lands on both sides. Each side
keeps one worker process alive for the whole run.

    python tools/ab.py --a /path/to/venv-a --b /path/to/venv-b --workloads /path/to/workloads \\
        --workload records-4k --workload helm-ingress-nginx --task emit --task edit

``--a`` and ``--b`` take a virtual environment directory or a Python executable. ``--workloads``
is a directory of workload folders, one per name, each with ``input.yaml`` and, for the tasks that
need them, ``edit.json`` (``{"ops": [...]}`` of replace and remove steps) and ``patch.json``
(``{"ops": [...]}`` of RFC 6902 operations); the kit's benchmark workloads have this layout.

Tasks:
    parse   oneyaml.parse(bytes)
    emit    bytes(doc) on a freshly parsed document
    edit    the edit steps through the document's views, then bytes(doc), on a fresh document
    patch   doc.patch(ops), then bytes(doc), on a fresh document
    load    oneyaml.loads(bytes)
    dump    oneyaml.dumps(value) of the loaded input

State a task needs (a parsed document) is built outside the clock, one copy per timed call.
The collector is off while timing. Each row shows the median wall time per call of the rounds,
the spread of the rounds ((max - min) / median), and the CPU time per call (all threads of the
process, so a threaded parse shows more CPU than wall). The last column is B / A on wall time:
below 1 means B is faster.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

TASKS = ("parse", "emit", "edit", "patch", "load", "dump")
DOCUMENT_TASKS = ("parse", "emit", "edit", "patch")

# ---- worker: runs inside the interpreter under test ------------------------------------------


def _steps(ops):
    """Edit steps as (op, tokens, value); the tokens are pointer text split into pieces."""
    out = []
    for o in ops:
        path = o["path"]
        toks = (
            []
            if path == ""
            else [t.replace("~1", "/").replace("~0", "~") for t in path[1:].split("/")]
        )
        out.append((o["op"], toks, o.get("value")))
    return out


def _edit(doc, steps):
    def key(node, tok):
        # Only a sequence (a view or a document whose root is one) has append.
        return int(tok) if hasattr(node, "append") else tok

    for op, toks, value in steps:
        cur = doc
        for t in toks[:-1]:
            cur = cur[key(cur, t)]
        k = key(cur, toks[-1])
        if op == "replace":
            cur[k] = value
        elif op == "remove":
            del cur[k]
        else:
            raise ValueError(f"edit step {op!r} is not supported")


def _make(task, data, folder, oneyaml):
    """(setup, run): setup() builds the state of one call, run(state) is what is timed."""
    if task == "parse":
        return (lambda: None), (lambda _: oneyaml.parse(data))
    if task == "load":
        return (lambda: None), (lambda _: oneyaml.loads(data))
    if task == "dump":
        value = oneyaml.loads(data)
        return (lambda: None), (lambda _: oneyaml.dumps(value))
    if task == "emit":
        return (lambda: oneyaml.parse(data)), (lambda doc: bytes(doc))
    if task == "edit":
        steps = _steps(json.loads((folder / "edit.json").read_text(encoding="utf-8"))["ops"])

        def run(doc):
            _edit(doc, steps)
            return bytes(doc)

        return (lambda: oneyaml.parse(data)), run
    if task == "patch":
        ops = json.loads((folder / "patch.json").read_text(encoding="utf-8"))["ops"]

        def run(doc):
            doc.patch(ops)
            return bytes(doc)

        return (lambda: oneyaml.parse(data)), run
    raise ValueError(task)


def _measure(setup, run, target_s, budget_bytes, size):
    """Wall and CPU nanoseconds per call over batches that last about ``target_s`` in all."""
    import gc

    per_state = max(size * 12, 1)  # a rough size of one parsed document
    cap = max(1, min(4096, budget_bytes // per_state))
    n = 1
    wall = cpu = 0
    calls = 0
    gc.collect()
    gc.disable()
    try:
        deadline = time.perf_counter() + target_s
        while True:
            states = [setup() for _ in range(n)]
            w0, c0 = time.perf_counter_ns(), time.process_time_ns()
            for s in states:
                run(s)
            w1, c1 = time.perf_counter_ns(), time.process_time_ns()
            del states
            wall += w1 - w0
            cpu += c1 - c0
            calls += n
            if time.perf_counter() >= deadline:
                break
            # Grow the batch until one batch is long enough to time, within the memory budget.
            if w1 - w0 < 2_000_000:
                n = min(cap, n * 2)
    finally:
        gc.enable()
    return wall / calls, cpu / calls


def worker() -> int:
    import oneyaml

    for line in sys.stdin:
        req = json.loads(line)
        if req.get("quit"):
            break
        try:
            folder = Path(req["folder"])
            data = (folder / "input.yaml").read_bytes()
            setup, run = _make(req["task"], data, folder, oneyaml)
            run(setup())  # one untimed call: caches, lazy imports
            wall, cpu = _measure(setup, run, req["seconds"], req["budget"], len(data))
            reply = {"wall": wall, "cpu": cpu}
        except Exception as e:  # noqa: BLE001 - the tool reports what a side could not do
            reply = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(reply), flush=True)
    return 0


# ---- driver ----------------------------------------------------------------------------------


class Side:
    def __init__(self, label: str, where: str, backend: str | None) -> None:
        p = Path(where)
        py = p / ("Scripts" if os.name == "nt" else "bin") / "python" if p.is_dir() else p
        env = {k: v for k, v in os.environ.items() if k != "ONEYAML_BACKEND"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if backend:
            env["ONEYAML_BACKEND"] = backend
        self.label = label
        self.proc = subprocess.Popen(
            [str(py), str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )

    def ask(self, **req):
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"side {self.label} died")
        return json.loads(line)

    def close(self) -> None:
        try:
            assert self.proc.stdin
            self.proc.stdin.write('{"quit": true}\n')
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        self.proc.wait(timeout=10)


def _fmt(ns: float) -> str:
    if ns >= 1e6:
        return f"{ns / 1e6:.2f}ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.1f}us"
    return f"{ns:.0f}ns"


def _summary(rounds: list[dict]) -> tuple[float, float, float]:
    walls = [r["wall"] for r in rounds]
    med = statistics.median(walls)
    spread = (max(walls) - min(walls)) / med if med else 0.0
    return med, spread, statistics.median(r["cpu"] for r in rounds)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--a", required=True, help="virtual environment or Python of build A")
    ap.add_argument("--b", required=True, help="virtual environment or Python of build B")
    ap.add_argument("--workloads", required=True, help="directory of workload folders")
    ap.add_argument(
        "--workload", action="append", default=[], help="workload name (repeatable; default all)"
    )
    ap.add_argument(
        "--task", action="append", choices=TASKS, default=[], help="task (repeatable; default all)"
    )
    ap.add_argument("--backend", choices=("native", "pure"), help="force the backend on both sides")
    ap.add_argument("--rounds", type=int, default=9, help="interleaved rounds per row (default 9)")
    ap.add_argument(
        "--seconds", type=float, default=0.05, help="time per side per round (default 0.05)"
    )
    ap.add_argument(
        "--budget-mb", type=int, default=256, help="most memory for one batch of states"
    )
    a = ap.parse_args(argv)

    root = Path(a.workloads)
    names = a.workload or sorted(p.name for p in root.iterdir() if (p / "input.yaml").is_file())
    tasks = a.task or list(TASKS)
    sides = [Side("A", a.a, a.backend), Side("B", a.b, a.backend)]
    head = f"{'A median':>10}{'spread':>8}{'cpu':>10}   {'B median':>10}{'spread':>8}{'cpu':>10}"
    print(f"{'workload':<26}{'task':<7}{head}   B/A")
    try:
        for name in names:
            folder = root / name
            for task in tasks:
                if task == "edit" and not (folder / "edit.json").is_file():
                    continue
                if task == "patch" and not (folder / "patch.json").is_file():
                    continue
                got: list[list[dict]] = [[], []]
                err = None
                for r in range(a.rounds):
                    # Alternate who goes first, so neither side always runs on a warm machine.
                    order = (0, 1) if r % 2 == 0 else (1, 0)
                    for i in order:
                        reply = sides[i].ask(
                            folder=str(folder),
                            task=task,
                            seconds=a.seconds,
                            budget=a.budget_mb * 1024 * 1024,
                        )
                        if "error" in reply:
                            err = f"{sides[i].label}: {reply['error']}"
                            break
                        got[i].append(reply)
                    if err:
                        break
                if err:
                    print(f"{name:<26}{task:<7}  {err}")
                    continue
                (ma, sa, ca), (mb, sb, cb) = _summary(got[0]), _summary(got[1])
                print(
                    f"{name:<26}{task:<7}{_fmt(ma):>10}{sa:>7.0%} {_fmt(ca):>10}   "
                    f"{_fmt(mb):>10}{sb:>7.0%} {_fmt(cb):>10}   {mb / ma:.2f}",
                    flush=True,
                )
    finally:
        for s in sides:
            s.close()
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv[1:]:
        raise SystemExit(worker())
    raise SystemExit(main(sys.argv[1:]))
