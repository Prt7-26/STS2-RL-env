"""Smoke probe: hit /health /version /observe, pretty-print combat state.

Usage::

    cd sts2-gym/py && python -m sts2_gym.probe
    python -m sts2_gym.probe --partial         # PartialObs view
    STS2GYM_PORT=8888 python -m sts2_gym.probe

Pre-condition: STS2 is running with the mod loaded.

Day-4 acceptance: combat phase should display the full hand / enemies /
intents, and ``--partial`` should mask draw_pile contents (count only).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import URLError

from sts2_gym.client import ModBridgeClient, PORT_LOCKFILE, read_port_lockfile


def _fmt_card(c: dict[str, Any]) -> str:
    """Compact one-line representation of a card snapshot."""
    cost = c.get("cost", "?")
    cid = c.get("id", "?")
    up = "+" * int(c.get("upgrade_level", 0))
    flags = []
    if c.get("can_play") is True:
        flags.append("play")
    elif c.get("can_play") is False:
        flags.append("BLOCK")
    return f"{cid}{up}(c={cost})" + ("[" + ",".join(flags) + "]" if flags else "")


def _fmt_creature(c: dict[str, Any]) -> str:
    """Compact one-line representation of a creature snapshot."""
    hp = f"{c.get('current_hp')}/{c.get('max_hp')}"
    block = c.get("block", 0)
    cid = c.get("monster_id") or c.get("character_id") or "?"
    slot = c.get("slot_name") or "?"
    side_marker = "P" if c.get("is_player") else ("E" if c.get("side") == "Enemy" else "A")
    powers = c.get("powers") or []
    pwr_str = (
        " powers=[" + ",".join(f"{p.get('id')}={p.get('amount')}" for p in powers) + "]"
        if powers
        else ""
    )
    nm = c.get("next_move")
    intent_str = ""
    if nm:
        intents = nm.get("intents") or []
        parts = []
        for i in intents:
            t = i.get("type")
            if t == "Attack":
                parts.append(f"Atk({i.get('total_damage')}x{i.get('repeats', 1)})")
            else:
                parts.append(t)
        intent_str = " intent=[" + ",".join(parts) + "]"
    return (
        f"[{side_marker} id={c.get('combat_id')} {cid}] hp={hp} block={block} slot={slot}"
        + pwr_str
        + intent_str
    )


def _print_combat(combat: dict[str, Any], partial: bool) -> None:
    print(
        f"  combat: encounter={combat.get('encounter')!r} round={combat.get('round')} "
        f"side={combat.get('current_side')} play_phase={combat.get('play_phase')}"
    )
    print(
        f"          creatures={combat.get('creature_count')} "
        f"hittable_enemies={combat.get('hittable_enemy_count')} "
        f"escaped={combat.get('escaped_count')}"
    )
    print("  creatures:")
    for c in combat.get("creatures") or []:
        print(f"    {_fmt_creature(c)}")

    for p in combat.get("players") or []:
        if not p.get("in_combat_state"):
            print(f"  player {p.get('net_id')}: no PlayerCombatState (combat tearing down?)")
            continue
        print(
            f"  player {p.get('net_id')}: "
            f"energy={p.get('energy')}/{p.get('max_energy')} stars={p.get('stars')}"
        )
        hand = p.get("hand") or []
        print(f"    hand ({len(hand)}):")
        for c in hand:
            print(f"      {_fmt_card(c)}")
        if partial:
            print(f"    draw_pile: count={p.get('draw_count')} (PartialObs — contents masked)")
        else:
            draws = p.get("draw_pile") or []
            print(f"    draw_pile ({len(draws)}): {[c.get('id') for c in draws[:8]]}{'...' if len(draws) > 8 else ''}")
        print(
            f"    discard={p.get('discard_count')} "
            f"exhaust={p.get('exhaust_count')} "
            f"play={p.get('play_count')}"
        )
        pets = p.get("pets") or []
        if pets:
            print(f"    pets: {[_fmt_creature(pet) for pet in pets]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym smoke probe")
    parser.add_argument(
        "--partial", action="store_true",
        help="Request PartialObs view (hides draw_pile contents, etc.)"
    )
    args = parser.parse_args(argv)

    lockfile_port = read_port_lockfile()
    if lockfile_port is not None:
        print(f"[probe] Port lockfile {PORT_LOCKFILE} -> {lockfile_port}")

    c = ModBridgeClient()
    print(f"[probe] Targeting {c.base} (partial={args.partial})")
    print()

    try:
        h = c.health()
    except (URLError, OSError) as e:
        print(f"[probe] ✗ /health failed: {e}")
        print("[probe]   Is STS2 running with the mod loaded? Tail the log:")
        print("[probe]     tail -F ~/Library/Application\\ Support/SlayTheSpire2/logs/godot.log \\")
        print("[probe]       | grep -E 'sts2gym|Loaded [0-9]+ mods|^ERROR:'")
        return 1
    print(f"[probe] ✓ /health   -> {h}")
    print(f"[probe] ✓ /version  -> {c.version()}")
    print()

    obs = c.observe(partial=args.partial)
    phase = obs.get("phase")
    in_run = obs.get("in_run")
    age = obs.get("snapshot_age_ms")
    print(
        f"[probe] ✓ /observe  -> phase={phase!r} in_run={in_run} "
        f"snapshot_age_ms={age} partial={obs.get('partial')}"
    )

    if in_run:
        run = obs.get("run") or {}
        rng = run.get("rng") or {}
        players = run.get("players") or []
        print(f"  run: schema={run.get('schema_version')} ascension={run.get('ascension')} "
              f"game_mode={run.get('game_mode')} act={run.get('current_act_index')}")
        print(f"  run: visited_map_coords={len(run.get('visited_map_coords') or [])} "
              f"rng_streams={len(rng.get('counters') or {})}")
        if players:
            p = players[0]
            print(
                f"  player[0]: character={p.get('character_id')!r} "
                f"hp={p.get('current_hp')}/{p.get('max_hp')} gold={p.get('gold')} "
                f"deck={len(p.get('deck') or [])} relics={len(p.get('relics') or [])} "
                f"potions={len(p.get('potions') or [])}"
            )

        combat = obs.get("combat")
        if combat:
            _print_combat(combat, partial=args.partial)
    else:
        print("  (not in run — main menu / boot / between runs)")

    out = "/tmp/sts2gym_observe.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obs, f, indent=2, ensure_ascii=False)
    print()
    print(f"[probe] Full observation dumped to {out}")
    print(f"[probe]   inspect: jq . {out} | less")

    return 0


if __name__ == "__main__":
    sys.exit(main())
