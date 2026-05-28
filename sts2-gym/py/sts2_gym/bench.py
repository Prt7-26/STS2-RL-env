"""Day-14 speed-tune: micro-benchmark to localize the per-step bottleneck.

Measures the latency of each piece of the agent loop in isolation:

  1. /observe (no mask)              — pure HTTP RTT + cache string copy
  2. /observe?with_mask=1             — same + ~2KB extra body (mask)
  3. /observe + /action_mask separate — what SPEED2 was doing
  4. /step noop                       — cheapest possible /step round-trip
                                         (no game-thread work)
  5. /step end_turn (combat only)     — full mod-side polling cost

Each measurement: 50 iterations, reports min / median / p95 / mean.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.bench                  # all sections, runs that don't need combat
    python -m sts2_gym.bench --combat         # also exercises /step end_turn (needs to be mid-combat)

Pre-condition: STS2 launched, mod loaded, game on main menu (or in a run).
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from typing import Callable

from sts2_gym.client import ModBridgeClient, StepError


def _measure(label: str, fn: Callable[[], object], iters: int = 50) -> None:
    samples: list[float] = []
    # Warm-up so the first sample isn't TCP-handshake-biased.
    try: fn()
    except Exception: pass
    for _ in range(iters):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception as e:
            print(f"  [{label}] iteration raised: {e}")
            return
        samples.append((time.perf_counter() - t0) * 1000)  # ms
    samples.sort()
    n = len(samples)
    p95 = samples[int(n * 0.95)] if n > 1 else samples[0]
    print(f"  {label:35s}  min={samples[0]:6.2f}  med={statistics.median(samples):6.2f}  "
          f"p95={p95:6.2f}  mean={statistics.mean(samples):6.2f}  ms (n={n})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym HTTP/mod latency micro-benchmark")
    parser.add_argument("--combat", action="store_true", help="also measure /step end_turn (must be mid-combat)")
    parser.add_argument("--iters", type=int, default=50)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    c = ModBridgeClient(port=args.port) if args.port is not None else ModBridgeClient()
    try:
        c.health()
    except Exception as e:
        print(f"[bench] ✗ /health failed: {e}")
        return 1

    print("[bench] === HTTP / cache-read latency ===")
    _measure("GET /observe",                      lambda: c.observe(),                              iters=args.iters)
    _measure("GET /observe?with_mask=1",          lambda: c.observe(with_mask=True),                iters=args.iters)
    _measure("GET /observe + /action_mask",       lambda: (c.observe(), c.action_mask()),           iters=args.iters)
    _measure("GET /action_mask",                  lambda: c.action_mask(),                          iters=args.iters)
    _measure("GET /health",                       lambda: c.health(),                               iters=args.iters)

    print()
    print("[bench] === /step (game-thread) latency ===")
    _measure("POST /step noop",                   lambda: c.step({"type": "noop"}),                 iters=args.iters)

    if args.combat:
        print()
        print("[bench] === /step end_turn (mid-combat) ===")
        # 5 iters only — each end_turn advances state and the player will die fast.
        try:
            for i in range(5):
                t0 = time.perf_counter()
                resp = c.step({"type": "end_turn"})
                dt = (time.perf_counter() - t0) * 1000
                print(f"  iter {i}: {dt:6.1f}ms  still_in_combat={resp.get('still_in_combat')}")
                if not resp.get("still_in_combat"):
                    print("  (combat ended)")
                    break
        except StepError as e:
            print(f"  end_turn raised: status={e.status} payload={e.payload}")

    print()
    print("[bench] === interpretation ===")
    print("  /observe vs /observe?with_mask=1: mask inlining cost (should be ~0).")
    print("  /observe alone vs /observe+/action_mask: the SPEED2->SPEED3 saving per agent loop.")
    print("  /step noop is the floor cost of a server round-trip (HTTP + main-thread marshal + 0 work).")
    print("  /step end_turn vs /step noop: time spent inside mod-side polling loops + game logic.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
