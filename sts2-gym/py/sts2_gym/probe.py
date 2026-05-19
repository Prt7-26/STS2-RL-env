"""Smoke probe: hit /health, /version, /observe, dump the full snapshot.

Usage::

    cd sts2-gym/py && python -m sts2_gym.probe
    STS2GYM_PORT=8888 python -m sts2_gym.probe

Pre-condition: STS2 is running with the mod loaded (run ``smoke_test.sh``
first to deploy + launch).

This is the Day-3 acceptance script. If it prints non-zero exit code or
the observation looks empty, the bridge is not yet validated.
"""
from __future__ import annotations

import json
import sys
from urllib.error import URLError

from sts2_gym.client import ModBridgeClient, PORT_LOCKFILE, read_port_lockfile


def main() -> int:
    lockfile_port = read_port_lockfile()
    if lockfile_port is not None:
        print(f"[probe] Port lockfile {PORT_LOCKFILE} -> {lockfile_port}")

    c = ModBridgeClient()
    print(f"[probe] Targeting {c.base}")
    print()

    # ---------- /health ----------
    try:
        h = c.health()
    except (URLError, OSError) as e:
        print(f"[probe] ✗ /health failed: {e}")
        print("[probe]   Is STS2 running with the mod loaded?")
        print("[probe]   Tail the log to verify:")
        print("[probe]     tail -F ~/Library/Application\\ Support/SlayTheSpire2/logs/godot.log \\")
        print("[probe]       | grep -E 'sts2gym|Loaded [0-9]+ mods|^ERROR:'")
        return 1
    print(f"[probe] ✓ /health   -> {h}")

    # ---------- /version ----------
    v = c.version()
    print(f"[probe] ✓ /version  -> {v}")
    print()

    # ---------- /observe ----------
    obs = c.observe()
    phase = obs.get("phase")
    in_run = obs.get("in_run")
    age = obs.get("snapshot_age_ms")
    print(f"[probe] ✓ /observe  -> phase={phase!r} in_run={in_run} snapshot_age_ms={age}")

    if in_run:
        run = obs.get("run") or {}
        rng = run.get("rng") or {}
        players = run.get("players") or []
        print(f"[probe]   schema_version={run.get('schema_version')}")
        print(
            f"[probe]   ascension={run.get('ascension')} "
            f"game_mode={run.get('game_mode')} "
            f"act={run.get('current_act_index')}"
        )
        print(
            f"[probe]   players={len(players)} acts={len(run.get('acts') or [])} "
            f"visited_map_coords={len(run.get('visited_map_coords') or [])}"
        )
        print(
            f"[probe]   rng: seed={rng.get('seed')!r} "
            f"streams={len(rng.get('counters') or {})}"
        )
        if players:
            p = players[0]
            print(
                f"[probe]   player[0]: character={p.get('character_id', {}).get('entry') if isinstance(p.get('character_id'), dict) else p.get('character_id')!r} "
                f"hp={p.get('current_hp')}/{p.get('max_hp')} "
                f"gold={p.get('gold')} "
                f"deck={len(p.get('deck') or [])}"
            )

        combat = obs.get("combat")
        if combat:
            print(
                f"[probe]   combat: encounter={combat.get('encounter')!r} "
                f"round={combat.get('round')} "
                f"side={combat.get('current_side')} "
                f"play_phase={combat.get('play_phase')} "
                f"enemies={combat.get('enemy_count')}"
            )
    else:
        print("[probe]   (not in run — main menu / boot phase / between runs)")

    # ---------- dump for human inspection ----------
    out = "/tmp/sts2gym_observe.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obs, f, indent=2, ensure_ascii=False)
    size = sum(1 for _ in open(out, encoding="utf-8"))
    print()
    print(f"[probe] Full observation dumped to {out} ({size} lines)")
    print(f"[probe]   inspect with:  jq . {out}  |  less")
    print(f"[probe]   top-level keys: {sorted(obs.keys())}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
