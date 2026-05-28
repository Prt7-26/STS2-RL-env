"""Day-13: ascension-scaling correctness test (dev plan §6 "Ascension 缩放正确性").

Spawns three runs at ascension 0 / 5 / 10 with the same character and seed,
then asserts the observable invariants against the code-layer ground truth
documented in dev plan §3.6:

    A4 (TightBelt)    : start with 1 less potion slot
    A5 (AscendersBane): start each run with 1 Ascender's Bane curse in deck

These are observable from /observe immediately after /start_run, without
having to enter combat. Things that need an actual combat to verify (A8/A9
enemy HP/damage scaling, A3 gold rewards) are out of scope for this fast test;
see dev plan §3.6 for the full code-layer ground-truth table.

Pre-condition: STS2 is running on the main menu with the mod loaded.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.ascension_test
    python -m sts2_gym.ascension_test --character SILENT --levels 0,3,5,10

The test calls /start_run three (or more) times, sleeping between each. Each
restart takes ~5s, so the whole test is ~15-20s end-to-end.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient


def _entry(model_id_str: Any) -> str | None:
    """Extract the Entry portion of a 'category.ENTRY' ModelId string."""
    if not isinstance(model_id_str, str):
        return None
    return model_id_str.rsplit(".", 1)[-1] if "." in model_id_str else model_id_str


def _summarize(obs: dict[str, Any]) -> dict[str, Any]:
    run = obs.get("run") or {}
    players = run.get("players") or []
    if not players:
        return {"err": "no players in obs"}
    p = players[0]
    deck_entries = [_entry(c.get("id")) for c in (p.get("deck") or [])]
    return {
        "ascension": run.get("ascension"),
        "current_act_index": run.get("current_act_index"),
        "max_hp": p.get("max_hp"),
        "current_hp": p.get("current_hp"),
        "max_energy": p.get("max_energy"),
        "max_potion_slot_count": p.get("max_potion_slot_count"),
        "gold": p.get("gold"),
        "deck_size": len(deck_entries),
        "deck": sorted(deck_entries),
        "ascenders_bane_count": deck_entries.count("ASCENDERS_BANE"),
    }


def run_one(c: ModBridgeClient, character: str, ascension: int, seed: str) -> dict[str, Any]:
    print(f"[asc] starting run: char={character} asc={ascension} seed={seed!r}")
    resp = c.start_run(character, ascension=ascension, seed=seed)
    if not resp.get("ok"):
        raise RuntimeError(f"start_run failed: {resp}")
    # Settle so Neow / Act 0 transition completes and player init runs.
    time.sleep(2.5)
    obs = c.observe()
    return _summarize(obs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym ascension-scaling test")
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--seed", default="ASC_TEST")
    parser.add_argument("--levels", default="0,5,10",
                        help="comma-separated ascension levels to compare (default 0,5,10)")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    levels = [int(x.strip()) for x in args.levels.split(",")]
    for lv in levels:
        if lv < 0 or lv > 10:
            print(f"[asc] ✗ invalid ascension {lv}; must be 0..10")
            return 1

    c = ModBridgeClient(port=args.port) if args.port is not None else ModBridgeClient()
    try:
        c.health()
    except Exception as e:
        print(f"[asc] ✗ /health failed: {e}")
        return 1

    # Run all levels with the same seed, abandoning the previous run between
    # each. /start_run rejects with 409 if a run is in progress, so we call
    # /abandon_run (RunManager.CleanUp) first; it's a no-op when nothing is active.

    snapshots: dict[int, dict[str, Any]] = {}
    for i, lv in enumerate(levels):
        try:
            abandon_resp = c.abandon_run()
            if abandon_resp.get("was_active"):
                print(f"[asc] abandoned previous run before starting A{lv}")
                time.sleep(1.0)  # let CleanUp settle before LoadRun
            snapshots[lv] = run_one(c, args.character, lv, args.seed)
        except Exception as e:
            print(f"[asc] ✗ failed at ascension={lv}: {e}")
            return 1
        print(f"[asc] obs(A{lv}): max_potion_slot={snapshots[lv]['max_potion_slot_count']} "
              f"deck_size={snapshots[lv]['deck_size']} "
              f"ascenders_bane_count={snapshots[lv]['ascenders_bane_count']} "
              f"max_hp={snapshots[lv]['max_hp']}")

    # ---- Assertions vs dev plan §3.6 code-layer ground truth ----
    print()
    print("[asc] === ASSERTIONS ===")
    errors: list[str] = []

    base = snapshots[levels[0]]

    # (1) MaxHp is invariant across ascensions for the same character.
    #     (StartingHp is on CharacterModel and not ascension-scaled.)
    for lv, snap in snapshots.items():
        if snap["max_hp"] != base["max_hp"]:
            errors.append(f"A{lv} max_hp={snap['max_hp']} != A{levels[0]} max_hp={base['max_hp']} (HP should not depend on ascension)")
    print(f"  ✓ max_hp invariant: {base['max_hp']}" if not errors else f"  ✗ max_hp variant!")

    # (2) A4 TightBelt: max_potion_slot_count == default - 1 once ascension ≥ 4.
    #     Default is 3 for the standard characters (AscensionManager.cs:29-32:
    #     player.SubtractFromMaxPotionCount(1)).
    default_potion = base["max_potion_slot_count"]  # at A0 (or whatever the lowest level was)
    for lv, snap in snapshots.items():
        expected = (default_potion - 1) if lv >= 4 else default_potion
        actual = snap["max_potion_slot_count"]
        if actual != expected:
            errors.append(f"A{lv} max_potion_slot_count={actual}, expected {expected} (A4+ should be -1)")
        else:
            print(f"  ✓ A{lv} max_potion_slot_count={actual} matches A4+ rule (expected {expected})")

    # (3) A5 AscendersBane: deck includes +1 ASCENDERS_BANE once ascension ≥ 5.
    for lv, snap in snapshots.items():
        expected = 1 if lv >= 5 else 0
        actual = snap["ascenders_bane_count"]
        if actual != expected:
            errors.append(f"A{lv} ascenders_bane_count={actual}, expected {expected} (A5+ adds 1 curse to deck)")
        else:
            print(f"  ✓ A{lv} ascenders_bane_count={actual} matches A5+ rule (expected {expected})")

    # (4) Non-curse deck is identical across ascensions for same character + seed.
    #     (Ascension affects the deck only via A5's added curse — base deck
    #     comes from the character's StartingDeck.)
    base_no_curse = sorted(c for c in base["deck"] if c != "ASCENDERS_BANE")
    for lv, snap in snapshots.items():
        no_curse = sorted(c for c in snap["deck"] if c != "ASCENDERS_BANE")
        if no_curse != base_no_curse:
            errors.append(f"A{lv} deck (minus curses) {no_curse} != A{levels[0]} deck {base_no_curse}")
        else:
            print(f"  ✓ A{lv} deck (curses removed) matches A{levels[0]}: {len(no_curse)} cards")

    # ---- Result ----
    print()
    if errors:
        print(f"[asc] ✗ {len(errors)} assertion failure(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"[asc] ✓ all assertions passed across ascensions {levels}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
