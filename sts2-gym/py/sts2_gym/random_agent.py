"""Random-policy agent — Day-5 P0 closed-loop acceptance test.

Loops:
    observe -> action_mask -> pick random legal action -> step
until combat ends, max-turns reached, or HTTP bridge becomes unresponsive.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.random_agent              # play one combat then exit
    python -m sts2_gym.random_agent --max-turns 50
    python -m sts2_gym.random_agent --seed 42 --verbose

Pre-condition: STS2 is running with the mod loaded AND the player has entered
combat (the agent waits up to a few seconds for play_phase=True but does not
itself drive map navigation — that's a Day-6 milestone).
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient, StepError


def _summarize_action(action: dict[str, Any]) -> str:
    t = action.get("type", "?")
    if t == "play_card":
        # Defensive: use .get() everywhere — fields beyond card_idx are advisory
        # (server doesn't require them, agent uses them for logging only).
        s = f"play_card[{action.get('card_idx')}] {action.get('card_id', '?')} cost={action.get('cost', '?')}"
        if action.get("target_combat_id") is not None:
            s += f" -> target_combat_id={action['target_combat_id']}"
        return s
    return t


def _pick_random_action(mask: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Pick a uniformly-random legal action from the mask.

    Strategy:
      1. From all actions, pick one uniformly. For play_card with required
         target, pick a uniformly-random legal_target.
      2. Heuristic tweak: avoid end_turn until we have no other choice (let
         the agent burn energy first — makes combats actually progress).
    """
    actions = mask["actions"]
    if not actions:
        raise RuntimeError("action_mask returned empty actions list during play_phase")

    playables = [a for a in actions if a["type"] == "play_card"]
    chosen = rng.choice(playables) if playables else next(a for a in actions if a["type"] == "end_turn")

    if chosen["type"] == "play_card":
        # Wire-protocol fields:  type + card_idx + optional target_combat_id.
        # Carry card_id / cost along too — server ignores unknown fields, and
        # the agent loop uses them for human-readable logging.
        out: dict[str, Any] = {
            "type": "play_card",
            "card_idx": chosen["card_idx"],
            "card_id": chosen.get("card_id"),
            "cost": chosen.get("cost"),
        }
        if chosen.get("requires_target"):
            targets = chosen.get("legal_targets") or []
            if not targets:
                # Card requires a target but no legal target — fall back to
                # end_turn (avoids "card cannot target" 409). This shouldn't
                # happen often because mask should filter out such cards.
                return {"type": "end_turn"}
            out["target_combat_id"] = rng.choice(targets)["combat_id"]
        return out
    return {"type": "end_turn"}


def run_one_combat(
    c: ModBridgeClient,
    rng: random.Random,
    max_turns: int = 50,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run random policy until combat ends or max_turns reached. Returns summary dict."""
    turns_observed = 0
    cards_played = 0
    end_turns = 0
    errors = 0
    stuck_actions = 0
    start = time.monotonic()

    # Wait briefly for play_phase to be true (player turn).
    deadline_wait = time.monotonic() + 10.0
    while time.monotonic() < deadline_wait:
        obs = c.observe()
        if obs.get("phase") != "combat":
            print(f"[agent] not in combat (phase={obs.get('phase')!r}) — aborting")
            return {"outcome": "not_in_combat", "phase": obs.get("phase")}
        combat = obs.get("combat", {})
        if combat.get("play_phase"):
            break
        time.sleep(0.3)
    else:
        return {"outcome": "timeout_waiting_for_play_phase"}

    print(f"[agent] combat started: encounter={combat.get('encounter')!r} round={combat.get('round')}")

    last_round_seen = -1
    while True:
        if turns_observed >= max_turns:
            print(f"[agent] max_turns={max_turns} reached, stopping")
            break

        obs = c.observe()
        phase = obs.get("phase")
        if phase != "combat":
            print(f"[agent] phase changed to {phase!r} — combat ended")
            break
        combat = obs["combat"]
        round_num = combat.get("round", -1)
        if round_num != last_round_seen:
            player_state = (combat.get("players") or [{}])[0]
            hp = combat.get("creatures", [{}])[0]
            print(
                f"[agent] round {round_num}: "
                f"player_hp={hp.get('current_hp')}/{hp.get('max_hp')} "
                f"energy={player_state.get('energy')}/{player_state.get('max_energy')} "
                f"hand={player_state.get('hand_count')} "
                f"hittable_enemies={combat.get('hittable_enemy_count')}"
            )
            last_round_seen = round_num

        if not combat.get("play_phase"):
            # Enemy turn or animations — wait for state to come back to us.
            time.sleep(0.2)
            continue

        mask = c.action_mask()
        if not mask.get("play_phase"):
            time.sleep(0.2)
            continue

        action = _pick_random_action(mask, rng)
        if verbose:
            print(f"[agent]   chose: {_summarize_action(action)}")

        try:
            resp = c.step(action)
        except StepError as e:
            errors += 1
            print(f"[agent]   STEP ERROR: status={e.status} payload={e.payload}")
            if errors >= 3:
                print("[agent]   too many errors, stopping")
                break
            stuck_actions += 1
            time.sleep(0.3)
            continue

        if not resp.get("ok"):
            print(f"[agent]   step returned ok=false: {resp}")
            break

        turns_observed += 1
        if action["type"] == "play_card":
            cards_played += 1
            if verbose:
                # Always-show energy_after now that TryManualPlay properly deducts.
                bits = []
                if resp.get("energy_after") is not None:
                    bits.append(f"energy {resp.get('energy_before')}->{resp.get('energy_after')}")
                if resp.get("hp_delta") != 0:
                    bits.append(f"hp_delta={resp.get('hp_delta')}")
                if bits:
                    print(f"[agent]     {' '.join(bits)}")
        elif action["type"] == "end_turn":
            end_turns += 1

        if not resp.get("still_in_combat", True):
            print("[agent] combat ended via still_in_combat=false in step response")
            break

    elapsed = time.monotonic() - start
    final = c.observe()
    final_phase = final.get("phase")
    summary = {
        "outcome": "ended" if final_phase != "combat" else "max_turns",
        "final_phase": final_phase,
        "turns_observed": turns_observed,
        "cards_played": cards_played,
        "end_turns": end_turns,
        "errors": errors,
        "elapsed_s": round(elapsed, 2),
        "step_per_s": round(turns_observed / elapsed, 2) if elapsed > 0 else None,
    }
    if final.get("in_run"):
        run = final.get("run") or {}
        players = run.get("players") or []
        if players:
            summary["final_hp"] = f"{players[0].get('current_hp')}/{players[0].get('max_hp')}"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2-Gym random-policy agent")
    parser.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")
    parser.add_argument("--max-turns", type=int, default=50, help="Max step() calls before stopping")
    parser.add_argument("--verbose", action="store_true", help="Print every action")
    parser.add_argument("--port", type=int, default=None, help="Override mod HTTP port")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    c = ModBridgeClient(port=args.port) if args.port is not None else ModBridgeClient()
    try:
        c.health()
    except Exception as e:
        print(f"[agent] ✗ /health failed: {e}")
        print("[agent]   Is STS2 running with the mod loaded?")
        return 1

    print(f"[agent] connected to {c.base}, seed={args.seed}, max_turns={args.max_turns}")
    summary = run_one_combat(c, rng, max_turns=args.max_turns, verbose=args.verbose)
    print()
    print("[agent] === SUMMARY ===")
    for k, v in summary.items():
        print(f"[agent]   {k} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
