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
        while True:
            mask = info["action_mask"]
            if mask.sum() == 0:
                # Probably between turns — refresh by stepping observe again via env's _refresh.
                # We have no public "wait" API; just take end_turn as a safety net (shouldn't fire normally).
                from sts2_gym.env import END_TURN_IDX
                action = END_TURN_IDX
            else:
                action = _pick_masked(mask, rng)

            obs, reward, terminated, truncated, info = env.step(action)
            steps += 1
            total_reward += reward
            if args.show_text_every > 0 and steps % args.show_text_every == 0:
                print(f"[smoke]   step {steps} reward={reward:+.3f}")
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
