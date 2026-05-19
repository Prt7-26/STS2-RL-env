"""STS2-Gym: RL/LLM environment bridge for Slay the Spire 2.

Day-7 milestone — Combat-phase gymnasium.Env + HumanRenderer text view,
the "Dual First-Class Citizens" interface (dev plan §0). Earlier-day
components (HTTP client, RandomAgent, determinism test) remain exposed.

Quick start (assumes STS2 is running with the mod loaded and the player
has manually started a run + entered the desired encounter at least once):

    import gymnasium as gym
    from sts2_gym import STS2CombatEnv  # registered as "STS2-Combat-v0" below

    env = STS2CombatEnv(encounter="CHOMPERS_NORMAL")
    obs, info = env.reset()
    print(info["text_obs"])  # LLM-readable view
    mask = info["action_mask"]  # np.bool array, len = env.action_space.n
"""
from __future__ import annotations

from sts2_gym.client import DEFAULT_PORT, ModBridgeClient, StepError
from sts2_gym.env import (
    ACTION_DIM,
    ENEMY_MAX,
    END_TURN_IDX,
    HAND_MAX,
    STS2CombatEnv,
    build_action_mask,
    decode_action,
    encode_observation,
)
from sts2_gym.renderer import render_combat, render_text, strip_bbcode

# Register the env with Gymnasium so users can `gym.make("STS2-Combat-v0")`.
try:
    from gymnasium.envs.registration import register

    register(
        id="STS2-Combat-v0",
        entry_point="sts2_gym.env:STS2CombatEnv",
    )
except ImportError:  # gymnasium missing — keep the lower layers usable
    pass

__all__ = [
    "ModBridgeClient",
    "StepError",
    "DEFAULT_PORT",
    "STS2CombatEnv",
    "ACTION_DIM",
    "END_TURN_IDX",
    "HAND_MAX",
    "ENEMY_MAX",
    "encode_observation",
    "build_action_mask",
    "decode_action",
    "render_text",
    "render_combat",
    "strip_bbcode",
]
__version__ = "0.0.2"
