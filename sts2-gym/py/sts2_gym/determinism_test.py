"""Determinism test — dev plan §6 / §11 P0 milestone.

Drives the same encounter twice under the same RunRngSet state and the same
agent seed, hashes each trajectory, and asserts they're identical.

Day-6 Level-A constraint: must already be in a run (we don't drive main-menu
UI to start fresh runs yet). The script:

  1. Snapshots the current RunRngSet via /observe.
  2. Snapshots encounter target — uses the current encounter if in combat,
     else expects --encounter argument.
  3. Round 1:
     a. /reset with rng_counters=snapshot + encounter=target
     b. Run RandomAgent (same --seed), record trajectory hash.
  4. Round 2:
     a. /reset with same payload
     b. Run RandomAgent (same --seed), record trajectory hash.
  5. Compare hashes; exit 0 if equal, 1 if not.

Trajectory definition (for hashing): tuples of
    (round, side, hand_count, energy_after_each_play, player_hp_after, encounter_creature_hps)
across the full combat. Excludes wall-clock-y fields (snapshot_age_ms,
deck size from acts, etc.) and only includes signals that should be
bit-exact deterministic.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.determinism_test                       # uses current encounter, seed=42
    python -m sts2_gym.determinism_test --seed 42 --max-turns 30
    python -m sts2_gym.determinism_test --encounter EXOSKELETONS_WEAK --seed 7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient, StepError
from sts2_gym.random_agent import _pick_random_action


def _trajectory_hash(events: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Return (sha256 hex, normalized events)."""
    blob = json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), events


def _normalize_obs_for_trajectory(obs: dict[str, Any]) -> dict[str, Any]:
    """Strip wall-clock-y fields from /observe so trajectories can be hash-compared."""
    combat = obs.get("combat") or {}
    creatures = combat.get("creatures") or []
    players = combat.get("players") or []
    p0 = players[0] if players else {}

    return {
        "phase": obs.get("phase"),
        "in_run": obs.get("in_run"),
        "round": combat.get("round"),
        "side": combat.get("current_side"),
        "play_phase": combat.get("play_phase"),
        "encounter": combat.get("encounter"),
        "creatures": [
            {
                "combat_id": c.get("combat_id"),
                "side": c.get("side"),
                "is_player": c.get("is_player"),
                "current_hp": c.get("current_hp"),
                "max_hp": c.get("max_hp"),
                "block": c.get("block"),
                "monster_id": c.get("monster_id"),
                "slot_name": c.get("slot_name"),
                "powers": sorted(
                    [(p.get("id"), p.get("amount")) for p in (c.get("powers") or [])]
                ),
            }
            for c in creatures
        ],
        "player": {
            "energy": p0.get("energy"),
            "stars": p0.get("stars"),
            "hand_count": p0.get("hand_count"),
            "draw_count": p0.get("draw_count"),
            "discard_count": p0.get("discard_count"),
            "exhaust_count": p0.get("exhaust_count"),
            "play_count": p0.get("play_count"),
            "hand_ids": [c.get("id") for c in (p0.get("hand") or [])],
        },
    }


def _run_one_pass(
    c: ModBridgeClient,
    rng_counters_snapshot: dict[str, Any],
    player_snapshot: dict[str, Any],
    encounter: str,
    agent_seed: int,
    max_turns: int,
) -> list[dict[str, Any]]:
    """One determinism-test pass: reset (player+rng+encounter), run agent, return trajectory."""
    print(f"  /reset player+rng+encounter={encounter}", end="", flush=True)
    resp = c.reset(
        encounter=encounter,
        rng_counters=rng_counters_snapshot,
        player_snapshot=player_snapshot,
    )
    print(f" -> ok={resp.get('ok')} player_restored={resp.get('player_restored')} "
          f"rng_restored={resp.get('rng_restored')} phase_after={resp.get('phase_after')}")

    # Wait for combat to actually start.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        obs = c.observe()
        if obs.get("phase") == "combat" and (obs.get("combat") or {}).get("play_phase"):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("combat play_phase did not become True after reset")

    trajectory: list[dict[str, Any]] = []
    agent_rng = random.Random(agent_seed)

    for step_idx in range(max_turns):
        obs = c.observe()
        trajectory.append({"step": step_idx, **_normalize_obs_for_trajectory(obs)})

        if obs.get("phase") != "combat":
            break
        combat = obs.get("combat") or {}
        if not combat.get("play_phase"):
            time.sleep(0.1)
            continue

        mask = c.action_mask()
        if not mask.get("play_phase"):
            time.sleep(0.1)
            continue

        action = _pick_random_action(mask, agent_rng)
        try:
            resp = c.step(action)
        except StepError as e:
            trajectory.append({"step": step_idx, "step_error": e.payload})
            break

        if not resp.get("still_in_combat", True):
            obs_final = c.observe()
            trajectory.append({"step": step_idx + 1, **_normalize_obs_for_trajectory(obs_final), "ended": True})
            break

    return trajectory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym determinism test")
    parser.add_argument("--seed", type=int, default=42, help="Agent RNG seed (controls action choice)")
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--encounter", type=str, default=None,
                        help="Encounter id to jump to. Defaults to current encounter if in combat.")
    parser.add_argument("--diff", action="store_true",
                        help="Print first divergence point on mismatch (verbose)")
    args = parser.parse_args(argv)

    c = ModBridgeClient()
    print(f"[det] target {c.base} agent_seed={args.seed} max_turns={args.max_turns}")

    try:
        c.health()
    except Exception as e:
        print(f"[det] /health failed: {e}")
        return 1

    obs0 = c.observe()
    if not obs0.get("in_run"):
        print(f"[det] not in a run (phase={obs0.get('phase')!r}). Start a run first.")
        return 1

    # Snapshot full restorable state (we'll restore this before each pass).
    # Day-6.1: includes player state (HP/deck/relics/PlayerRng) — without it,
    # combat damage and deck changes leak between passes and break determinism.
    rng_snapshot = c.snapshot_run_rng()
    player_snapshot = c.snapshot_player()
    encounter = args.encounter or (obs0.get("combat") or {}).get("encounter")
    if encounter is None:
        print("[det] no --encounter given and not currently in a combat — please pass --encounter <ID>")
        return 1
    print(f"[det] encounter={encounter!r} run_rng_seed={rng_snapshot.get('seed')!r} "
          f"rng_streams={len(rng_snapshot.get('counters') or {})}")
    print(f"[det] player snapshot: net_id={player_snapshot.get('net_id')} "
          f"hp={player_snapshot.get('current_hp')}/{player_snapshot.get('max_hp')} "
          f"deck={len(player_snapshot.get('deck') or [])} "
          f"relics={len(player_snapshot.get('relics') or [])}")
    print()

    print("[det] === pass A ===")
    traj_a = _run_one_pass(c, rng_snapshot, player_snapshot, encounter, args.seed, args.max_turns)
    hash_a, _ = _trajectory_hash(traj_a)
    print(f"[det]   trajectory: {len(traj_a)} steps, sha256 = {hash_a[:16]}…")
    print()

    print("[det] === pass B ===")
    traj_b = _run_one_pass(c, rng_snapshot, player_snapshot, encounter, args.seed, args.max_turns)
    hash_b, _ = _trajectory_hash(traj_b)
    print(f"[det]   trajectory: {len(traj_b)} steps, sha256 = {hash_b[:16]}…")
    print()

    if hash_a == hash_b:
        print(f"[det] ✓ DETERMINISTIC — both passes produced the same {len(traj_a)}-step trajectory")
        return 0

    print(f"[det] ✗ DIVERGED — passes produced different trajectories")
    print(f"[det]   A: {hash_a}")
    print(f"[det]   B: {hash_b}")
    if args.diff:
        # Find first divergent step.
        for i, (a, b) in enumerate(zip(traj_a, traj_b)):
            if a != b:
                print(f"[det]   first diff at step {i}:")
                print(f"[det]     A: {a}")
                print(f"[det]     B: {b}")
                break
        else:
            print(f"[det]   no per-step diff in overlapping prefix; len(A)={len(traj_a)} len(B)={len(traj_b)}")
    else:
        print("[det]   rerun with --diff for first-divergence detail")
    return 1


if __name__ == "__main__":
    sys.exit(main())
