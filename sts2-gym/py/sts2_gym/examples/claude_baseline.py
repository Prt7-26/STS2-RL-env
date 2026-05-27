"""Day-11.B: minimal Claude-driven LLM agent over STS2-Gym.

A from-zero LLM baseline — shows how to wire the env's text obs into the
Anthropic SDK and how to parse Claude's response back into a step action.
Intentionally small (~80 lines of agent logic) so it can serve as the
starting template referenced in the dev plan §11 P0 milestone "LLM
baseline 示例 — 几十行能跑通".

Requirements (not part of the core install):
    pip install anthropic
    export ANTHROPIC_API_KEY=...

Usage::

    cd sts2-gym/py
    python -m sts2_gym.examples.claude_baseline \
        --character IRONCLAD --encounter CHOMPERS_NORMAL --max-turns 20

Design notes:
  * Uses ``info["text_obs"]`` (rendered by HumanRenderer) as the observation
    in the prompt. The same prose a debug human would read.
  * Asks for canonical text actions, NOT constrained JSON. Dev plan §8 #10:
    "不强制 LLM 严格 JSON 输出".
  * Parses replies via :class:`~sts2_gym.llm_parser.LLMActionParser` which
    tolerates surrounding prose and minor synonym variations.
  * Falls back to a random masked action on parse failure so the run keeps
    moving even when Claude returns garbage. The fallback rate is reported
    in the final summary — it's a key metric for "is my prompt working".

Token tracking: Anthropic SDK returns input/output token counts on every
response. We accumulate them per-episode (dev plan §8 #11: token cost is
a first-class metric for LLM eval).
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from typing import Any

import numpy as np

from sts2_gym import STS2CombatEnv
from sts2_gym.action_codec import ParseError, to_text
from sts2_gym.client import StepError
from sts2_gym.env import decode_action
from sts2_gym.llm_parser import LLMActionParser

SYSTEM = """You are an expert Slay the Spire 2 player. Each turn I will show you
the current observation in human-readable text. You must respond with EXACTLY
ONE action in canonical form, optionally preceded by brief reasoning. Examples:

  play Strike on A
  play Defend
  end turn
  select pick 0
  choose option 1
  leave reward
  choose map 3,5

Do not invent cards that aren't in your hand. Do not target enemies that
aren't listed. If you're unsure, prefer end turn over an illegal action."""


def _legal_actions_hint(mask: np.ndarray, env: STS2CombatEnv) -> str:
    """Build a one-line summary of legal actions so the model doesn't have to
    re-derive legality from the text observation."""
    legal_idx = np.flatnonzero(mask).tolist()
    if not legal_idx:
        return "(no legal actions in mask)"
    examples = []
    for idx in legal_idx[:20]:
        try:
            a = decode_action(idx, env._last_mask_payload, env._last_obs_payload.get("combat") or {})
            examples.append(to_text(a, context=env._last_obs_payload))
        except Exception:  # noqa: BLE001
            continue
    return "Legal actions: " + " | ".join(examples)


def _pick_random_masked(mask: np.ndarray, rng: random.Random) -> int:
    legal = np.flatnonzero(mask)
    if len(legal) == 0:
        raise RuntimeError("mask is all-False")
    return int(rng.choice(legal.tolist()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude baseline driver for STS2-Gym")
    parser.add_argument("--character", default="IRONCLAD")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--encounter", default="CHOMPERS_NORMAL")
    parser.add_argument("--run-seed", default=None)
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--model", default="claude-opus-4-7")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        import anthropic
    except ImportError:
        print("[claude] anthropic SDK not installed. Run: pip install anthropic")
        return 1
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[claude] ANTHROPIC_API_KEY env var not set.")
        return 1

    rng = random.Random(42)
    client = anthropic.Anthropic()
    parser_robust = LLMActionParser(on_ambiguity="last")

    env = STS2CombatEnv(
        character=args.character,
        ascension=args.ascension,
        run_seed=args.run_seed,
        encounter=args.encounter,
    )
    print(f"[claude] env: character={args.character} encounter={args.encounter} model={args.model}")

    try:
        obs, info = env.reset()
        total_in, total_out, parse_fails, steps = 0, 0, 0, 0
        t0 = time.monotonic()
        for step in range(args.max_turns):
            mask = info["action_mask"]
            text_obs = info.get("text_obs") or "(no text_obs)"
            hint = _legal_actions_hint(mask, env)

            user_msg = f"{text_obs}\n\n{hint}\n\nYour move:"
            resp = client.messages.create(
                model=args.model,
                max_tokens=128,
                system=SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens
            reply_text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            if args.verbose:
                print(f"\n[claude] step {step+1} reply: {reply_text!r}")

            parser_robust.context = env._last_obs_payload  # refresh context each step
            try:
                action_dict = parser_robust.parse(reply_text)
            except ParseError as e:
                parse_fails += 1
                print(f"[claude]   PARSE FAIL ({e}); falling back to random masked")
                action_idx = _pick_random_masked(mask, rng)
            else:
                # Translate structured → Discrete index by reusing the env's mapping.
                # If the structured action doesn't map cleanly to a Discrete slot,
                # post it raw via the client.
                action_idx = _structured_to_discrete(action_dict, env)
                if action_idx is None:
                    if args.verbose: print(f"[claude]   raw-post structured action: {action_dict}")
                    try:
                        env.client.step(action_dict)
                    except StepError as e:
                        print(f"[claude]   STEP ERROR: {e.payload}")
                        parse_fails += 1
                    env._refresh_caches()
                    # synthesize step return
                    obs, _, terminated, truncated, info = env._build_step_return(
                        terminated=env._last_obs_payload.get("phase") != "combat",
                        truncated=False,
                        reward=0.0,
                    )
                    steps += 1
                    if terminated or truncated: break
                    continue

            obs, reward, terminated, truncated, info = env.step(action_idx)
            steps += 1
            if args.verbose:
                print(f"[claude]   reward={reward:+.2f} term={int(terminated)} trunc={int(truncated)}")
            if terminated or truncated:
                break

        elapsed = time.monotonic() - t0
        print("\n[claude] === SUMMARY ===")
        print(f"  steps              = {steps}")
        print(f"  tokens (in/out)    = {total_in} / {total_out}")
        print(f"  parse_fails        = {parse_fails} / {steps} ({100*parse_fails/max(steps,1):.1f}%)")
        print(f"  elapsed_s          = {elapsed:.1f}")
        print(f"  step_per_s         = {steps/elapsed:.2f}" if elapsed > 0 else "  step_per_s         = inf")
    finally:
        env.close()
    return 0


def _structured_to_discrete(action: dict[str, Any], env: STS2CombatEnv) -> int | None:
    """Map a structured action back to a Discrete index in the env's action space.
    Returns None if the action type isn't covered by the combat / selector
    Discrete encoding (e.g. choose_map_node, shop_buy — those go via direct /step)."""
    from sts2_gym.env import (
        ENEMY_MAX, END_TURN_IDX, SELECTOR_PICK_BASE, SELECTOR_UNPICK_BASE,
        SELECTOR_CONFIRM_IDX, SELECTOR_SKIP_IDX,
    )
    t = action.get("type")
    if t == "end_turn": return END_TURN_IDX
    if t == "select_pick": return SELECTOR_PICK_BASE + int(action["option_idx"])
    if t == "select_unpick": return SELECTOR_UNPICK_BASE + int(action["option_idx"])
    if t == "select_confirm": return SELECTOR_CONFIRM_IDX
    if t == "select_skip": return SELECTOR_SKIP_IDX
    if t == "play_card":
        card_idx = action.get("card_idx")
        if card_idx is None: return None
        if "target_combat_id" in action:
            # Find which enemy slot this combat_id is in.
            combat = env._last_obs_payload.get("combat") or {}
            enemies = [c for c in (combat.get("creatures") or [])
                       if not c.get("is_player") and c.get("is_hittable")]
            enemies.sort(key=lambda c: c.get("combat_id") or 0)
            for i, e in enumerate(enemies):
                if e.get("combat_id") == action["target_combat_id"]:
                    return card_idx * (ENEMY_MAX + 1) + (i + 1)
            return None
        return card_idx * (ENEMY_MAX + 1) + 0
    return None  # non-combat phases fall through to client.step


if __name__ == "__main__":
    sys.exit(main())
