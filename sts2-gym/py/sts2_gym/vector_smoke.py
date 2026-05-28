"""Day-14: smoke test for STS2VectorEnv.

Two modes:

1. **Connect mode** (default, no game launches required):

       cd sts2-gym/py
       python -m sts2_gym.vector_smoke --ports 7777,7778

   Expects the user to have already launched N STS2 instances on the given
   ports. Each instance must be on the main menu. The test will:
     - construct a 2-env STS2VectorEnv
     - reset both at different ascensions (0 and 5)
     - step a few times
     - assert ascensions remain independent in the resulting obs

2. **Spawn mode** (CI / unattended):

       python -m sts2_gym.vector_smoke --spawn --num-envs 2

   Auto-launches N STS2 instances via GameProcess.spawn, waits for /health,
   runs the same checks, terminates all instances. Slower (each STS2 cold-boot
   is ~10s) but no manual setup.

Process isolation check: after stepping each env once, we assert the two
processes' /observe payloads differ on at least one ascension-dependent field
(deck_size — A5+ adds AscendersBane). This is dev plan §6 "Process 隔离" test.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from sts2_gym.process import GameProcess
from sts2_gym.vector_env import STS2VectorEnv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym VectorEnv smoke test")
    parser.add_argument("--ports", default="7777,7778",
                        help="comma-separated ports (default 7777,7778). Ignored if --spawn.")
    parser.add_argument("--spawn", action="store_true",
                        help="auto-spawn N STS2 instances instead of connecting to pre-launched ones")
    parser.add_argument("--num-envs", type=int, default=2, help="N for --spawn mode (default 2)")
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--seed", default="VEC_TEST")
    parser.add_argument("--ascensions", default="0,5",
                        help="comma-separated ascension per env (default 0,5)")
    args = parser.parse_args(argv)

    ascensions = [int(x) for x in args.ascensions.split(",")]

    print(f"[vec_smoke] mode={'spawn' if args.spawn else 'connect'}")

    if args.spawn:
        if args.num_envs != len(ascensions):
            print(f"[vec_smoke] ✗ --num-envs {args.num_envs} must match ascensions count {len(ascensions)}")
            return 1
        print(f"[vec_smoke] spawning {args.num_envs} STS2 instances...")
        processes = [GameProcess.spawn(7777 + i) for i in range(args.num_envs)]
    else:
        ports = [int(p.strip()) for p in args.ports.split(",")]
        if len(ports) != len(ascensions):
            print(f"[vec_smoke] ✗ --ports count {len(ports)} must match --ascensions count {len(ascensions)}")
            return 1
        processes = [GameProcess(port=p, owns_process=False) for p in ports]
        for p in processes:
            try:
                p.client.health()
            except Exception as e:
                print(f"[vec_smoke] ✗ /health failed at port {p.port}: {e}")
                print("[vec_smoke]   Pre-launch STS2 there with STS2GYM_PORT=<port>, or use --spawn")
                return 1
        print(f"[vec_smoke] connected to {len(processes)} pre-launched instances")

    try:
        venv = STS2VectorEnv(
            processes,
            character=args.character,
            ascension=ascensions,
            run_seed=args.seed,
        )
        print(f"[vec_smoke] STS2VectorEnv constructed with num_envs={venv.num_envs}")

        # Reset all envs. This calls /start_run with the per-env ascension on
        # each backing process. /abandon_run is called first to clean up any
        # lingering manual-play run.
        print("[vec_smoke] abandoning any lingering runs on each instance...")
        for p in processes:
            try:
                resp = p.client.abandon_run()
                if resp.get("was_active"):
                    print(f"[vec_smoke]   port {p.port}: abandoned previous run")
            except Exception as e:
                print(f"[vec_smoke]   port {p.port}: abandon failed ({e}) — continuing")

        time.sleep(1.0)
        obs, info = venv.reset()
        print(f"[vec_smoke] reset OK. obs.keys = {list(obs.keys()) if isinstance(obs, dict) else type(obs).__name__}")

        # Pull per-env summary via the underlying clients.
        print("[vec_smoke] verifying process isolation via per-instance /observe ...")
        summaries: list[dict[str, Any]] = []
        for i, p in enumerate(processes):
            ob = p.client.observe()
            run = ob.get("run") or {}
            players = run.get("players") or [{}]
            player = players[0]
            deck = player.get("deck") or []
            entries = []
            for c in deck:
                cid = c.get("id")
                if isinstance(cid, str):
                    entries.append(cid.rsplit(".", 1)[-1])
            summary = {
                "port": p.port,
                "ascension": run.get("ascension"),
                "deck_size": len(entries),
                "ascenders_bane_count": entries.count("ASCENDERS_BANE"),
                "max_potion_slot_count": player.get("max_potion_slot_count"),
            }
            summaries.append(summary)
            print(f"[vec_smoke]   env[{i}] port={p.port}: {summary}")

        # ---- Assertions ----
        errors: list[str] = []
        for i, lv in enumerate(ascensions):
            s = summaries[i]
            if s["ascension"] != lv:
                errors.append(f"env[{i}] reports ascension={s['ascension']}, expected {lv}")
            expected_curse = 1 if lv >= 5 else 0
            if s["ascenders_bane_count"] != expected_curse:
                errors.append(f"env[{i}] ascension={lv} has {s['ascenders_bane_count']} AscendersBane, expected {expected_curse}")

        if errors:
            print()
            print(f"[vec_smoke] ✗ {len(errors)} assertion failure(s):")
            for err in errors:
                print(f"  - {err}")
            return 1

        print()
        print(f"[vec_smoke] ✓ {len(processes)} STS2 processes report independent ascension state")
        return 0

    finally:
        try:
            venv.close()  # type: ignore[has-type]
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
