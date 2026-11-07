"""Hot-path cost of rxplus.reacting handles (median of 5 runs).

Run: python benchmarks/bench_reacting_core.py [--n N]

"no session" is the spec's "no observer connected" case: one check per call.
"""

import argparse
import statistics
import time
from collections.abc import Callable

from rxplus.reacting import MemorySink, Reporter, SystemClock


def _per_call_ns(fn: Callable[[], object], n: int) -> float:
    runs = []
    for _ in range(5):
        start = time.perf_counter()
        for _ in range(n):
            fn()
        runs.append((time.perf_counter() - start) / n * 1e9)
    return statistics.median(runs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=500_000)
    n = parser.parse_args().n

    base = _per_call_ns(lambda: None, n)

    idle_sink = MemorySink()
    idle_sink.disconnect()
    idle = Reporter("bench", idle_sink, timer=False)
    idle.poll()  # no session
    idle_link = idle.link(idle.element("A"), idle.element("B"))

    live = Reporter("bench", MemorySink(), timer=False, clock=SystemClock())
    live_link = live.link(live.element("A"), live.element("B"))
    worker = live.element("W")

    rows = [
        ("Link.deliver, no session", idle_link.deliver),
        ("Link.deliver, live", live_link.deliver),
        ("Link.enter + leave, live", lambda: live_link.leave(live_link.enter())),
        ("Element.begin + end, live", lambda: worker.end(worker.begin())),
    ]
    print(f"{'empty call':<30} {base:8.1f} ns")
    for name, fn in rows:
        print(f"{name:<30} {_per_call_ns(fn, n) - base:8.1f} ns over an empty call")


if __name__ == "__main__":
    main()
