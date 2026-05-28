"""Day-10.D: full-run random agent — loops combat + non-combat phases.

Loops a full single-player STS2 run with a uniform-random policy. Two
start-up modes:

  Natural (default, when --encounter not given):
    start_run → choose_map_node(first node) → game's natural progression.
    Map visited-history populated correctly; first combat is whatever the
    procedural map placed first.

  Debug-jump (when --encounter is given):
    start_run → /reset encounter=X → fight that specific combat.
    CurrentMapCoord stays null for that first combat — visual map shows
    no progress for that room — but agent state is still consistent.
    Use this when you need to repeat a specific encounter for a smoke test.

Phase dispatch:
  * combat        — combat actions + in-combat selectors
  * card_select   — selector resolution (Day-8.1)
  * map           — random reachable node
  * event         — random option
  * reward        — claim gold/potion/relic items; card-type slots are
                    SKIPPED (the engine's card-reward sub-screen path
                    bypasses our ICardSelector hook — supporting it
                    needs the dedicated card_reward_pick action, deferred
                    to Day-10.E). Skipping a card slot is legal: the
                    game lets us leave the reward screen with it
                    unclaimed.
  * shop / rest   — random buy / option pick
  * game_over     — loop-click any enabled button until phase changes
                    (death → unlock-history → main menu is 2-3 screens)

Usage::

    cd sts2-gym/py
    python -m sts2_gym.full_run_agent --character IRONCLAD --max-steps 800
    python -m sts2_gym.full_run_agent --character IRONCLAD --encounter TOADPOLES_WEAK
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from typing import Any

from sts2_gym.client import ModBridgeClient, StepError
from sts2_gym.random_agent import _pick_random_action


def _progress_marker(obs: dict[str, Any]) -> tuple:
    """Reduce the obs to a compact "did anything change?" fingerprint. Used by
    the outer loop's stuck detector — if this marker is identical for many
    consecutive iterations, we're not making progress and should bail."""
    phase = obs.get("phase")
    sel = obs.get("selector") or {}
    combat = obs.get("combat") or {}
    p0 = (combat.get("players") or [{}])[0]
    return (
        phase,
        bool(sel.get("active")),
        len(sel.get("accumulator") or []),
        combat.get("round"),
        p0.get("hand_count"),
        p0.get("energy"),
        len((obs.get("map") or {}).get("reachable") or []),
        len((obs.get("event") or {}).get("options") or []),
        len((obs.get("reward") or {}).get("items") or []),
        len((obs.get("shop") or {}).get("items") or []),
        len((obs.get("rest") or {}).get("options") or []),
    )


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
    consecutive_same_phase = 0
    last_progress_marker: tuple | None = None
    stuck_marker_count = 0
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
            consecutive_same_phase = 0
            last_progress_marker = None
            stuck_marker_count = 0
            if verbose:
                print(f"[full-run] step={steps:>3} {_summarize_phase(obs)}")
        else:
            consecutive_same_phase += 1

        # Stuck detection: if a phase makes no observable progress for N steps,
        # abort cleanly. Marker = (phase, round, hand_count, selector_state...)
        # — any meaningful state change resets the counter.
        marker = _progress_marker(obs)
        if marker == last_progress_marker:
            stuck_marker_count += 1
        else:
            last_progress_marker = marker
            stuck_marker_count = 0
        if stuck_marker_count >= 100:  # ~20s of no observable change
            summary["stopped"] = f"stuck in {effective!r} — no progress for 100 ticks (marker={marker})"
            break

        try:
            if effective == "card_select":
                _do_selector_step(c, obs, rng, verbose=verbose)
            elif effective == "combat":
                if not _do_combat_step(c, obs, rng, verbose=verbose):
                    time.sleep(0.05)  # Day-14: was 0.2; enemy-turn settles fast in Instant
            elif effective == "map":
                _do_map_step(c, obs, rng, verbose=verbose)
            elif effective == "event":
                _do_event_step(c, obs, rng, verbose=verbose)
            elif effective == "reward":
                _do_reward_step(c, obs, verbose=verbose)
            elif effective == "card_reward_select":
                _do_card_reward_select_step(c, obs, rng, verbose=verbose)
            elif effective == "shop":
                _do_shop_step(c, obs, rng, verbose=verbose)
            elif effective == "rest":
                _do_rest_step(c, obs, rng, verbose=verbose)
            elif effective == "relic_select":
                _do_relic_select_step(c, obs, rng, verbose=verbose)
            elif effective == "bundle_select":
                _do_bundle_select_step(c, obs, rng, verbose=verbose)
            elif effective == "treasure":
                _do_treasure_step(c, obs, rng, verbose=verbose)
            elif effective == "game_over":
                _do_game_over_step(c, verbose=verbose)
                summary["stopped"] = "game_over"
                break
            elif effective in ("combat_pending", "between_rooms"):
                time.sleep(0.08)  # Day-14: was 0.3; Instant transitions complete in <1 frame
            else:
                consecutive_unknown += 1
                if verbose:
                    print(f"[full-run]   unhandled phase={effective!r} (#{consecutive_unknown})")
                if consecutive_unknown >= 10:
                    summary["stopped"] = f"unhandled phase {effective!r} ×10 — Day-10.B needed (shop/rest/etc.)"
                    break
                time.sleep(0.1)
                continue
            consecutive_unknown = 0
        except StepError as e:
            summary["errors"].append({"step": steps, "phase": effective, "status": e.status, "payload": e.payload})
            print(f"[full-run]   STEP ERROR @ {effective}: {e.status} {e.payload}")
            if len(summary["errors"]) >= 5:
                summary["stopped"] = "too many step errors"
                break
            time.sleep(0.1)
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
        time.sleep(0.08)
        return
    pick = rng.choice(reachable)
    if verbose: print(f"[full-run]   map → [{pick['col']},{pick['row']}] {pick['point_type']}")
    c.choose_map_node(pick["col"], pick["row"])


def _do_event_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    event = obs.get("event") or {}
    options = event.get("options") or []
    is_finished = event.get("is_finished")
    # Day-10.F: when event.is_finished, the UI synthesizes a PROCEED button
    # but evt.CurrentOptions is empty. Drive choose_event_option anyway —
    # the mod-side handler finds and clicks the synthetic button.
    if is_finished:
        if verbose: print(f"[full-run]   event → is_finished=true, clicking synthetic PROCEED")
        c.choose_event_option(0)
        return
    if not options:
        if verbose: print("[full-run]   event → no options yet; waiting")
        time.sleep(0.08)
        return
    # Filter out options that the engine has already marked as chosen (locked).
    available = [o for o in options if not o.get("was_chosen") and not o.get("is_locked")]
    if not available:
        if verbose: print(f"[full-run]   event → all {len(options)} options chosen/locked; waiting")
        time.sleep(0.08)
        return
    pick = rng.choice(available)
    if verbose:
        opt_summary = ", ".join(
            f"[{o['option_idx']}]{o.get('text_key', '')}"
            f"{'*chosen' if o.get('was_chosen') else ''}"
            f"{'*locked' if o.get('is_locked') else ''}"
            f"{'*proc' if o.get('is_proceed') else ''}"
            for o in options
        )
        print(f"[full-run]   event opts=[{opt_summary}] → pick {pick['option_idx']}")
    c.choose_event_option(pick["option_idx"])


_reward_empty_strikes = {"count": 0}  # module-level: consecutive empty observations
_reward_stuck_tries: dict[int, int] = {}  # idx → consecutive attempts (skip after N)
_REWARD_STUCK_LIMIT = 3
_REWARD_EMPTY_LIMIT = 2  # Day-14 speed-tune: was 5. Under FastMode.Instant the
                        # CombatEnded → NRewardsScreen push race window is < 1
                        # frame. 5 × 200ms wall-clock was burning ~1s per
                        # reward screen for nothing.
_REWARD_EMPTY_SLEEP = 0.05  # 200ms -> 50ms; Instant-mode animations are 0
_reward_skipped_idx: set[int] = set()  # idx we've given up on this reward screen


def _do_reward_step(c: ModBridgeClient, obs: dict[str, Any] | None = None, *, verbose: bool) -> None:
    """Day-10.K + Day-14 hotfix: claim every enabled reward item.

    Race guard: CombatEnded → NRewardsScreen pushes → BUT reward items take
    a few frames to populate. Require 5 consecutive empty observations before
    accepting "nothing to claim".

    Stuck-loop guard: a reward item can be ``is_enabled=true`` in the UI but
    silently fail to consume (e.g. PotionReward when all 3 potion slots are
    full; NRewardButton stays clickable but click is no-op). After
    ``_REWARD_STUCK_LIMIT`` attempts on the same idx with no state change we
    add it to a per-screen skip set and try the next eligible idx. Leaving
    the screen flushes the skip set.
    """
    reward = (obs or {}).get("reward") or {}
    items = reward.get("items") or []
    eligible = [it for it in items if it.get("is_enabled") and it.get("idx") not in _reward_skipped_idx]
    if eligible:
        _reward_empty_strikes["count"] = 0
        pick = eligible[0]
        idx = pick["idx"]
        tries = _reward_stuck_tries.get(idx, 0) + 1
        _reward_stuck_tries[idx] = tries
        if tries > _REWARD_STUCK_LIMIT:
            if verbose:
                print(f"[full-run]   reward → idx={idx} type={pick.get('reward_type')} "
                      f"unclaimable after {tries-1} tries (full slot? skipping)")
            _reward_skipped_idx.add(idx)
            _reward_stuck_tries.pop(idx, None)
            return
        if verbose: print(f"[full-run]   reward → take idx={idx} type={pick.get('reward_type')} (try {tries})")
        c.take_reward_item(idx)
        return
    # Empty items list — could be transient (screen still loading) or real
    # (everything claimed). Wait a few polls to disambiguate.
    _reward_empty_strikes["count"] += 1
    if _reward_empty_strikes["count"] < _REWARD_EMPTY_LIMIT:
        if verbose: print(f"[full-run]   reward → empty items (strike {_reward_empty_strikes['count']}/{_REWARD_EMPTY_LIMIT}); waiting for populate")
        time.sleep(_REWARD_EMPTY_SLEEP)
        return
    # Confirmed empty — leave. Reset the per-screen stuck-skip set so the next
    # reward screen starts fresh.
    _reward_empty_strikes["count"] = 0
    _reward_skipped_idx.clear()
    _reward_stuck_tries.clear()
    if verbose: print(f"[full-run]   reward → leave (confirmed empty after {_REWARD_EMPTY_LIMIT} polls)")
    c.leave_reward_screen()


def _do_card_reward_select_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    """Day-10.G: NCardRewardSelectionScreen — pick one of the 3 card holders."""
    crs = obs.get("card_reward_select") or {}
    cards = crs.get("cards") or []
    if not cards:
        if verbose: print("[full-run]   card_reward_select → no cards yet; waiting")
        time.sleep(0.08)
        return
    pick = rng.choice(cards)
    if verbose:
        opts = ", ".join(f"[{c['idx']}]{c.get('card_id')}" for c in cards)
        print(f"[full-run]   card_reward_select [{opts}] → pick {pick['idx']}")
    c.card_reward_pick(pick["idx"])


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


def _do_bundle_select_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    """Day-10.O: NChooseABundleSelectionScreen — pick 1 random bundle."""
    bs = obs.get("bundle_select") or {}
    bundles = bs.get("bundles") or []
    if not bundles:
        time.sleep(0.08)
        return
    pick = rng.choice(bundles)
    if verbose:
        summary = ", ".join(f"[{b['idx']}]{b.get('cards', [])}" for b in bundles)
        print(f"[full-run]   bundle_select [{summary}] → pick {pick['idx']}")
    c.bundle_pick(pick["idx"])


def _do_treasure_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    """Day-14: NTreasureRoom — open chest → pick enabled relic holders → leave.

    Order matters: chest must be open before relics are visible/clickable;
    proceed only enables once relics are claimed (or none available).
    """
    t = obs.get("treasure") or {}
    if not t.get("chest_open"):
        if verbose: print("[full-run]   treasure → open chest")
        try:
            c.treasure_open()
        except StepError as e:
            if verbose: print(f"[full-run]   treasure_open failed: {e.payload}")
            time.sleep(0.08)
        return

    relics = t.get("relics") or []
    enabled = [r for r in relics if r.get("is_enabled")]
    if enabled:
        pick = enabled[0]  # could rng.choice; first works fine for run-throughs
        if verbose: print(f"[full-run]   treasure → pick relic idx={pick['idx']} id={pick.get('id')} ({pick.get('rarity')})")
        try:
            c.treasure_pick(pick["idx"])
        except StepError as e:
            if verbose: print(f"[full-run]   treasure_pick {pick['idx']} failed: {e.payload}")
            time.sleep(0.08)
        return

    if t.get("can_proceed"):
        if verbose: print("[full-run]   treasure → leave")
        try:
            c.treasure_leave()
        except StepError as e:
            if verbose: print(f"[full-run]   treasure_leave failed: {e.payload}")
            time.sleep(0.08)
        return

    # No enabled relics and proceed not yet enabled — wait for animations.
    time.sleep(0.08)


def _do_relic_select_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    """Day-10.E: NChooseARelicSelection — pick a random enabled relic button."""
    items = (obs.get("relic_select") or {}).get("items") or []
    enabled = [it for it in items if it.get("is_enabled")]
    if not enabled:
        time.sleep(0.08)
        return
    pick = rng.choice(enabled)
    if verbose: print(f"[full-run]   relic_select → pick idx={pick['idx']}")
    c.relic_pick(pick["idx"])


def _do_rest_step(c: ModBridgeClient, obs: dict[str, Any], rng: random.Random, *, verbose: bool) -> None:
    options = (obs.get("rest") or {}).get("options") or []
    enabled = [o for o in options if o.get("is_enabled")]
    if not enabled:
        # Day-10.N: after the option resolves the room shows a "前进" proceed
        # button. Without rest_leave we'd just spin forever in phase=rest with
        # empty options.
        if verbose: print("[full-run]   rest → no enabled options, clicking proceed")
        try:
            c.rest_leave()
        except StepError as e:
            # Button not yet enabled (e.g. SMITH still resolving). Wait + retry.
            if verbose: print(f"[full-run]   rest_leave 409 ({e.payload.get('error')}); waiting")
            time.sleep(0.08)
        return
    pick = rng.choice(enabled)
    if verbose: print(f"[full-run]   rest → option_idx={pick['option_idx']} ({pick.get('option_id')})")
    c.rest_choose(pick["option_idx"])


def _do_game_over_step(c: ModBridgeClient, *, verbose: bool) -> None:
    """Day-10.D: game-over → main menu can have multiple screens (death
    summary → unlock history → main menu). Loop-click until phase changes
    away from game_over, up to 5 attempts."""
    for attempt in range(5):
        if verbose: print(f"[full-run]   game_over → proceed (attempt {attempt+1})")
        try:
            c.proceed_after_game_over()
        except StepError as e:
            if verbose: print(f"[full-run]   game_over proceed errored: {e.payload}")
            break
        time.sleep(0.1)
        try:
            phase = c.observe().get("phase")
        except Exception:
            break
        if phase != "game_over":
            if verbose: print(f"[full-run]   game_over cleared → phase={phase!r}")
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Day-10.D full-run random agent")
    parser.add_argument("--character", default=None, help="auto-start a new run with this character (else use current)")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--run-seed", default=None)
    parser.add_argument("--encounter", default=None,
                        help="DEBUG: force first combat to this encounter via /reset. Bypasses map "
                             "(CurrentMapCoord stays null for first room). Omit for natural map progression.")
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
            # Debug-jump mode: force the first combat to a specific encounter
            # by reusing /reset. Skips the natural "first map node" navigation.
            print(f"[full-run] DEBUG: jump to encounter={args.encounter} via /reset")
            c.reset(encounter=args.encounter)
        else:
            # Natural mode: a fresh run normally starts with the Neow event,
            # not the map. The outer loop dispatches whatever phase the game
            # is in — no special handling needed.
            print(f"[full-run] natural mode: outer loop will dispatch first phase")
            time.sleep(0.8)  # let StartRun's EnterAct settle

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
