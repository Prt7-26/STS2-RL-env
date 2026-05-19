"""End-to-end smoke test for STS2CombatEnv — Day-7 P0.

Requires a running STS2 with the mod loaded AND the player already in a run
(Day-7 doesn't drive main-menu UI). Runs N episodes with a uniformly-random
mask-respecting policy and asserts:

  * env.reset() returns a valid Dict obs + non-empty action_mask
  * action_mask matches action_space.n
  * Every step's chosen action is masked-legal
  * Episodes terminate within max_steps
  * info["text_obs"] is non-empty

Usage::

    cd sts2-gym/py
    python -m sts2_gym.env_smoke                                # 2 episodes, current encounter
    python -m sts2_gym.env_smoke --encounter CHOMPERS_NORMAL --episodes 3 --seed 7
"""
from __future__ import annotations

import argparse
import random
import sys
import time

import numpy as np

from sts2_gym import STS2CombatEnv
from sts2_gym.client import ModBridgeClient


def _pick_masked(mask: np.ndarray, rng: random.Random) -> int:
    legal = np.flatnonzero(mask)
    if len(legal) == 0:
        raise RuntimeError("no legal actions — mask is all False during play_phase?")
    return int(rng.choice(legal.tolist()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="STS2CombatEnv smoke test")
    parser.add_argument("--encounter", type=str, default=None)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=60)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reward-mode", choices=["sparse", "shaped"], default="sparse")
    parser.add_argument("--show-text-every", type=int, default=0,
                        help="Print info['text_obs'] every N steps (0=never)")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    client = ModBridgeClient()
    try:
        client.health()
    except Exception as e:
        print(f"[smoke] ✗ /health failed: {e} — is STS2 running with the mod?")
        return 1

    env = STS2CombatEnv(
        encounter=args.encounter,
        client=client,
        max_steps=args.max_steps,
        reward_mode=args.reward_mode,
        render_mode="ansi",
    )
    print(f"[smoke] env: action_space={env.action_space} obs keys={list(env.observation_space.spaces)}")
    print(f"[smoke] encounter={args.encounter or '(current)'} episodes={args.episodes} seed={args.seed}")

    for ep in range(args.episodes):
        print(f"\n[smoke] === episode {ep + 1}/{args.episodes} ===")
        t0 = time.monotonic()
        obs, info = env.reset()
        assert "action_mask" in info, "missing info[action_mask]"
        assert info["action_mask"].shape == (env.action_space.n,), info["action_mask"].shape
        assert info.get("text_obs"), "info[text_obs] empty"
        print(f"[smoke]   reset OK: encounter={env.encounter} initial_legal={int(info['action_mask'].sum())}")
        if args.show_text_every > 0:
            print(info["text_obs"])

        total_reward = 0.0
        steps = 0
        zero_mask_strikes = 0
        while True:
            mask = info["action_mask"]
            if mask.sum() == 0:
                # After the Day-7.1 EndTurnAsync fix this should be rare — mod waits
                # for IsPlayPhase before returning. If we still see all-zero masks for
                # 5 consecutive observations, the episode is effectively stuck; break
                # rather than spin into a 409 storm.
                zero_mask_strikes += 1
                if zero_mask_strikes >= 5:
                    print(f"[smoke]   abort: mask all-zero × {zero_mask_strikes} — bailing out")
                    break
                time.sleep(0.2)
                # Force a fresh poll via env internals.
                env._refresh_caches()
                from sts2_gym.env import encode_observation, build_action_mask
                info["action_mask"] = build_action_mask(env._last_mask_payload, env._last_obs_payload.get("combat") or {})
                continue

            zero_mask_strikes = 0
            action = _pick_masked(mask, rng)
            from sts2_gym.env import decode_action, END_TURN_IDX
            decoded = decode_action(action, env._last_mask_payload, env._last_obs_payload.get("combat") or {})
            obs, reward, terminated, truncated, info = env.step(action)
            steps += 1
            total_reward += reward
            # Compact one-line trace per step so a hung loop is immediately visible.
            tag = "end_turn" if action == END_TURN_IDX else f"play[{decoded.get('card_idx')}]"
            err = info.get("step_error")
            err_str = f" ERR={err['payload'].get('error', '?')}" if err else ""
            hp_delta = info.get('hp_delta')
            hp_str = f" hp_delta={hp_delta:+d}" if isinstance(hp_delta, int) else ""
            print(
                f"[smoke]   step {steps:>3} {tag:<14} r={reward:+.2f}"
                f"{hp_str}"
                f" term={int(terminated)} trunc={int(truncated)}{err_str}"
            )
            if args.show_text_every > 0 and steps % args.show_text_every == 0:
                print(info["text_obs"])

            if terminated or truncated:
                break

        elapsed = time.monotonic() - t0
        final_phase = info.get("phase")
        sps = steps / max(elapsed, 1e-6)
        print(
            f"[smoke]   episode done: steps={steps} reward={total_reward:+.3f} "
            f"terminated={terminated} truncated={truncated} final_phase={final_phase!r} "
            f"({elapsed:.1f}s, {sps:.1f} step/s)"
        )

    env.close()
    print(f"\n[smoke] ✓ {args.episodes} episode(s) completed without error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
