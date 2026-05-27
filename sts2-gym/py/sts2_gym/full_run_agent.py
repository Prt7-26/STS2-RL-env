"""Day-10.A: full-run random agent — loops combat + non-combat phases.

The earlier ``random_agent`` only handled combat; once combat ended on a reward
screen / map / event / game-over, it gave up. This agent handles all four
non-combat phases shipped in Day-10.A as well, so a fresh run can be driven
end-to-end with a uniform-random policy.

Phase dispatch:
  * combat            → existing combat + selector logic
  * card_select       → selector resolution (Day-8.1)
  * map               → choose random reachable map node
  * event             → choose random event option
  * reward            → leave_reward_screen (card picks already routed through
                        the ICardSelector → selector phase)
  * game_over         → proceed_after_game_over, then stop
  * combat_pending    → wait briefly; combat will materialize
  * shop / rest / ... → not supported yet (Day-10.B). Agent stops with a clear
                        log line so you can switch encounters or restart.

Usage::

    cd sts2-gym/py
    python -m sts2_gym.full_run_agent --character IRONCLAD --ascension 0 --seed 7
    python -m sts2_gym.full_run_agent --max-steps 500 --verbose
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient, StepError
from sts2_gym.random_agent import _pick_random_action


def _summarize_phase(obs: dict[str, Any]) -> str:
    phase = obs.get("phase", "?")
    bits = [f"phase={phase}"]
    if (obs.get("selector") or {}).get("active"):
        s = obs["selector"]
        bits.append(f"selector(min={s['min_select']},max={s['max_select']},opts={len(s.get('options') or [])})")
    if phase == "combat":
        c = obs.get("combat") or {}
        bits.append(f"round={c.get('round')} play_phase={c.get('play_phase')}")
    elif phase == "map":
        m = obs.get("map") or {}
        bits.append(f"reachable={len(m.get('reachable') or [])}")
    elif phase == "event":
        e = obs.get("event") or {}
        bits.append(f"event={e.get('id')} opts={len(e.get('options') or [])}")
    return " ".join(bits)


def run_one_full_run(
    c: ModBridgeClient,
    rng: random.Random,
    max_steps: int = 500,
    verbose: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "steps_per_phase": {},
        "phases_visited": [],
        "errors": [],
        "stopped": None,
    }
    steps = 0
    last_phase = None
    consecutive_unknown = 0
    t0 = time.monotonic()

    while steps < max_steps:
        steps += 1
        try:
            obs = c.observe()
        except Exception as e:
            print(f"[full-run] ✗ /observe failed: {e}")
            summary["stopped"] = f"observe failed: {e!r}"
            break

        phase = obs.get("phase")
        selector_active = (obs.get("selector") or {}).get("active")
        effective = "card_select" if selector_active else phase

        # Track phase transitions for the summary.
        summary["steps_per_phase"][effective] = summary["steps_per_phase"].get(effective, 0) + 1
        if effective != last_phase:
            summary["phases_visited"].append(effective)
            last_phase = effective
            if verbose:
                print(f"[full-run] step={steps:>3} {_summarize_phase(obs)}")

        try:
            if effective == "card_select":
                _do_selector_step(c, obs, rng, verbose=verbose)
            elif effective == "combat":
                if not _do_combat_step(c, obs, rng, verbose=verbose):
                    time.sleep(0.2)
            elif effective == "map":
                _do_map_step(c, obs, rng, verbose=verbose)
            elif effective == "event":
                _do_event_step(c, obs, rng, verbose=verbose)
            elif effective == "reward":
                _do_reward_step(c, obs, verbose=verbose)
            elif effective == "shop":
                _do_shop_step(c, obs, rng, verbose=verbose)
            elif effective == "rest":
                _do_rest_step(c, obs, rng, verbose=verbose)
            elif effective == "game_over":
                _do_game_over_step(c, verbose=verbose)
                summary["stopped"] = "game_over"
                break
            elif effective in ("combat_pending", "between_rooms"):
                time.sleep(0.3)  # wait for transition
            else:
                consecutive_unknown += 1
                if verbose:
                    print(f"[full-run]   unhandled phase={effective!r} (#{consecutive_unknown})")
                if consecutive_unknown >= 10:
                    summary["stopped"] = f"unhandled phase {effective!r} ×10 — Day-10.B needed (shop/rest/etc.)"
                    break
                time.sleep(0.5)
                continue
            consecutive_unknown = 0
        except StepError as e:
            summary["errors"].append({"step": steps, "phase": effective, "status": e.status, "payload": e.payload})
            print(f"[full-run]   STEP ERROR @ {effective}: {e.status} {e.payload}")
            if len(summary["errors"]) >= 5:
                summary["stopped"] = "too many step errors"
                break
            time.sleep(0.5)
    else:
        summary["stopped"] = "max_steps"

    summary["total_steps"] = steps
    summary["elapsed_s"] = round(time.monotonic() - t0, 2)
    return summary


def _do_selector_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    mask = c.action_mask()
    a = _pick_random_action(mask, rng)
    if verbose: print(f"[full-run]   selector → {a.get('type')} {a.get('option_idx', '')}")
    c.step(a)


def _do_combat_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> bool:
    """Returns True if an action was taken, False if we should wait + retry."""
    combat = obs.get("combat") or {}
    if not combat.get("play_phase"):
        return False
    mask = c.action_mask()
    if not mask.get("play_phase") and not mask.get("selector_active"):
        return False
    a = _pick_random_action(mask, rng)
    if verbose: print(f"[full-run]   combat → {a.get('type')} {a.get('card_idx', a.get('option_idx', ''))}")
    c.step(a)
    return True


def _do_map_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    reachable = (obs.get("map") or {}).get("reachable") or []
    if not reachable:
        time.sleep(0.3)
        return
    pick = rng.choice(reachable)
    if verbose: print(f"[full-run]   map → [{pick['col']},{pick['row']}] {pick['point_type']}")
    c.choose_map_node(pick["col"], pick["row"])


def _do_event_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    options = (obs.get("event") or {}).get("options") or []
    if not options:
        time.sleep(0.3)
        return
    pick = rng.choice(options)
    if verbose: print(f"[full-run]   event → option_idx={pick['option_idx']} ({pick.get('text_key')})")
    c.choose_event_option(pick["option_idx"])


def _do_reward_step(c: ModBridgeClient, obs: dict[str, Any] | None = None, *, verbose: bool) -> None:
    """Day-10.C: greedily take any enabled reward items, then leave.

    Each take_reward_item may activate the ICardSelector (card reward sub-
    screen) — in that case the outer loop sees selector_active=true on the
    next /observe and dispatches to the selector branch first. We just take
    one item per call; the outer loop drives the cycle.
    """
    reward = (obs or {}).get("reward") or {}
    items = reward.get("items") or []
    enabled = [it for it in items if it.get("is_enabled")]
    if enabled:
        pick = enabled[0]
        if verbose: print(f"[full-run]   reward → take idx={pick['idx']} type={pick.get('reward_type')}")
        c.take_reward_item(pick["idx"])
        return
    # Nothing more to claim — leave the screen.
    if verbose: print("[full-run]   reward → leave (nothing left)")
    c.leave_reward_screen()


def _do_shop_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    """Simple shop policy: buy a random affordable item with 50% chance, else leave."""
    shop = obs.get("shop") or {}
    items = shop.get("items") or []
    affordable = [it for it in items if it.get("is_stocked") and it.get("enough_gold")]
    if affordable and rng.random() < 0.5:
        pick = rng.choice(affordable)
        if verbose: print(f"[full-run]   shop → buy entry_idx={pick['entry_idx']} kind={pick['kind']} id={pick.get('id')} cost={pick['cost']}")
        c.shop_buy(pick["entry_idx"])
    else:
        if verbose: print(f"[full-run]   shop → leave (gold={shop.get('player_gold')}, affordable={len(affordable)}/{len(items)})")
        c.shop_leave()


def _do_rest_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    options = (obs.get("rest") or {}).get("options") or []
    enabled = [o for o in options if o.get("is_enabled")]
    if not enabled:
        time.sleep(0.3)
        return
    pick = rng.choice(enabled)
    if verbose: print(f"[full-run]   rest → option_idx={pick['option_idx']} ({pick.get('option_id')})")
    c.rest_choose(pick["option_idx"])


def _do_game_over_step(c: ModBridgeClient, *, verbose: bool) -> None:
    if verbose: print("[full-run]   game_over → proceed (back to main menu)")
    try:
        c.proceed_after_game_over()
    except StepError as e:
        # Game-over screen might not have a NProceedButton; fall through.
        print(f"[full-run]   game_over proceed errored: {e.payload}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-10.A full-run random agent")
    parser.add_argument("--character", default=None, help="auto-start a new run with this character (else use current)")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--run-seed", default=None)
    parser.add_argument("--encounter", default=None, help="initial encounter to jump to after start_run")
    parser.add_argument("--seed", type=int, default=42, help="agent RNG seed (action randomness)")
    parser.add_argument("--max-steps", type=int, default=500)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--enable-selector", action="store_true", default=True)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    c = ModBridgeClient()
    try:
        c.health()
    except Exception as e:
        print(f"[full-run] ✗ /health failed: {e}")
        return 1

    if args.enable_selector:
        try: c.enable_selector()
        except Exception as e: print(f"[full-run] warn: enable_selector failed: {e}")

    if args.character is not None:
        print(f"[full-run] start_run character={args.character} ascension={args.ascension} seed={args.run_seed}")
        try:
            c.start_run(args.character, ascension=args.ascension, seed=args.run_seed)
        except StepError as e:
            print(f"[full-run] start_run failed: {e.payload}")
            return 1
        if args.encounter is not None:
            print(f"[full-run] jump to initial encounter={args.encounter}")
            c.reset(encounter=args.encounter)

    try:
        summary = run_one_full_run(c, rng, max_steps=args.max_steps, verbose=args.verbose)
    finally:
        try: c.disable_selector()
        except Exception: pass

    print()
    print("[full-run] === SUMMARY ===")
    for k, v in summary.items():
        if k == "errors" and not v: continue
        print(f"  {k} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
