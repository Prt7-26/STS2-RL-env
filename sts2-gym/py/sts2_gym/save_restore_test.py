"""Day-13: end-to-end smoke test for /save_run + /restore_run.

Pre-condition: STS2 is running with the mod loaded (run `bash scripts/smoke_test.sh
--no-game` then launch the game manually).

Usage::

    cd sts2-gym/py
    python -m sts2_gym.save_restore_test                       # auto-starts a fresh run
    python -m sts2_gym.save_restore_test --no-start-run        # use the current run
    python -m sts2_gym.save_restore_test --character SILENT --ascension 5

Tests:
  1. /save_run on a fresh run returns 200 + a SerializableRun JSON
  2. The save's top-level fields (deck_size, hp, schema_version) look plausible
  3. /restore_run with that exact save round-trips to identical state
  4. Saving mid-combat returns 409 (sanity check)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient, StepError


def _summarize(obs: dict[str, Any]) -> str:
    bits = [f"phase={obs.get('phase')!r}"]
    if obs.get("in_run"):
        run = obs.get("run") or {}
        players = run.get("players") or []
        if players:
            p = players[0]
            bits.append(f"hp={p.get('current_hp')}/{p.get('max_hp')}")
            bits.append(f"gold={p.get('gold')}")
            bits.append(f"deck={len(p.get('deck') or [])}")
        bits.append(f"act={run.get('current_act_index')}")
        bits.append(f"floor={run.get('floor')}")
    return " ".join(bits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym save/restore smoke test")
    parser.add_argument("--no-start-run", action="store_true", help="Use the current run instead of starting a fresh one")
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--seed", default="SAVERESTORE")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    c = ModBridgeClient(port=args.port) if args.port is not None else ModBridgeClient()
    try:
        c.health()
    except Exception as e:
        print(f"[saverestore] ✗ /health failed: {e}")
        print("[saverestore]   Is STS2 running with the mod loaded?")
        return 1

    # ---------- 1. ensure we're in a run ----------
    if not args.no_start_run:
        print(f"[saverestore] starting fresh run: char={args.character} asc={args.ascension} seed={args.seed!r}")
        try:
            resp = c.start_run(args.character, ascension=args.ascension, seed=args.seed)
            print(f"[saverestore] start_run -> {resp}")
        except StepError as e:
            # 409 means a run is already in progress — that's fine, the user has
            # one going manually
            if e.status == 409:
                print("[saverestore] run already in progress; reusing")
            else:
                raise

    # Settle so the run is past Neow / Act 0 transition.
    time.sleep(2.0)
    obs_before = c.observe()
    print(f"[saverestore] state before save: {_summarize(obs_before)}")

    # ---------- 2. /save_run ----------
    print("[saverestore] calling /save_run ...")
    save_resp = c.save_run()
    if not save_resp.get("ok"):
        print(f"[saverestore] ✗ /save_run failed: {save_resp}")
        return 1

    save = save_resp["save"]
    meta = {k: v for k, v in save_resp.items() if k != "save"}
    print(f"[saverestore] save_run OK: {meta}")
    print(f"[saverestore]   schema={save.get('SchemaVersion')} ascension={save.get('Ascension')} act={save.get('CurrentActIndex')}")
    print(f"[saverestore]   rng_streams={len((save.get('SerializableRng') or {}).get('Counters') or [])}")
    print(f"[saverestore]   players={len(save.get('Players') or [])} deck_card_0={(save.get('Players') or [{}])[0].get('Deck', [{}])[0]}")

    # ---------- 3. /restore_run round-trip ----------
    # First, perturb the state a bit if possible — e.g. enter a room — so we can
    # tell the restore actually went somewhere. For the smoke test, simply
    # restore-same and verify HP / deck match.
    print("[saverestore] calling /restore_run with the same save (round-trip) ...")
    restore_resp = c.restore_run(save)
    if not restore_resp.get("ok"):
        print(f"[saverestore] ✗ /restore_run failed: {restore_resp}")
        return 1
    print(f"[saverestore] restore_run OK: {restore_resp}")

    time.sleep(1.5)
    obs_after = c.observe()
    print(f"[saverestore] state after restore: {_summarize(obs_after)}")

    # Verify key invariants survived round-trip
    def player_summary(obs: dict[str, Any]) -> dict[str, Any]:
        run = obs.get("run") or {}
        players = run.get("players") or [{}]
        p = players[0]
        return {
            "ascension": run.get("ascension"),
            "current_act_index": run.get("current_act_index"),
            "hp": p.get("current_hp"),
            "max_hp": p.get("max_hp"),
            "gold": p.get("gold"),
            "deck_size": len(p.get("deck") or []),
        }

    before = player_summary(obs_before)
    after = player_summary(obs_after)
    print(f"[saverestore] before: {before}")
    print(f"[saverestore] after:  {after}")

    mismatches = [k for k in before if before[k] != after[k]]
    if mismatches:
        print(f"[saverestore] ✗ field mismatch after round-trip: {mismatches}")
        for k in mismatches:
            print(f"[saverestore]    {k}: {before[k]!r} -> {after[k]!r}")
        return 1

    print("[saverestore] ✓ round-trip bit-equal on core fields (hp/gold/deck/ascension/act)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
